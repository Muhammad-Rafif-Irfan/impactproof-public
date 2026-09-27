#!/usr/bin/env python3
"""
ImpactProof EXPLAIN → FIX → VERIFY tools.

Three sub-commands, each prints a single JSON object to stdout.

    python regression_tools.py explain  <repo_path> <scenario_name>
    python regression_tools.py apply_fix <repo_path> <file_rel> <new_source>
    python regression_tools.py verify   <repo_path> <scenario_name>

All diagnostic output goes to stderr. Exit 0 always (errors are returned as JSON).
"""

from __future__ import annotations

import ast
import difflib
import fcntl
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

# Locate the proof engine relative to this file:
#   impactproof/regression_tools.py  →  root is ../
HERE = Path(__file__).parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))

from proof.engine import run_named_scenario, load_scenario  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers shared by all sub-commands
# ---------------------------------------------------------------------------

def _validate_repo(repo_path: str) -> str | None:
    """Return None if valid git repo, else an error message."""
    if not os.path.isdir(repo_path):
        return f"repo_path is not a directory: {repo_path}"
    r = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=repo_path, capture_output=True, text=True,
    )
    if r.returncode != 0:
        return f"repo_path is not a Git repository: {repo_path}"
    return None


def _git_changed_files(repo_path: str) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for extra in ([], ["--cached"]):
        r = subprocess.run(
            ["git", "diff", "--name-only", "HEAD"] + extra,
            cwd=repo_path, capture_output=True, text=True,
        )
        if r.returncode == 0:
            for line in r.stdout.splitlines():
                line = line.strip()
                if line and line not in seen:
                    seen.add(line)
                    unique.append(line)
    return unique


def _top_level_symbols(source: str, filepath: Path) -> list[str]:
    try:
        tree = ast.parse(source, filename=str(filepath))
    except SyntaxError:
        return []
    return [
        n.name for n in ast.iter_child_nodes(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]


def _state_paths(repo_root: Path) -> tuple[Path, Path]:
    """Keep undo metadata in this worktree's Git metadata, outside the tree."""
    result = subprocess.run(
        ["git", "rev-parse", "--absolute-git-dir"],
        cwd=repo_root, capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise OSError("Could not locate repository metadata for undo state.")
    state_dir = Path(result.stdout.strip()) / "impactproof"
    return state_dir / "undo_state.json", state_dir / "undo_state.lock"


@contextmanager
def _undo_state_lock(lock_path: Path) -> Iterator[None]:
    """Serialize apply/undo state changes for this repository."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _load_undo_state(state_path: Path) -> dict:
    if not state_path.exists():
        return {"version": 1, "history": []}
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Undo state is unreadable: {exc}") from exc
    if (not isinstance(state, dict) or state.get("version") != 1
            or not isinstance(state.get("history"), list)):
        raise ValueError("Undo state is invalid or unsupported.")
    return state


def _save_undo_state(state_path: Path, state: dict) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix="undo_state.", suffix=".tmp", dir=state_path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            json.dump(state, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, state_path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else None
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if mode is not None:
            os.chmod(temp_name, mode)
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _read_text_exact(path: Path) -> str:
    """Read UTF-8 without universal-newline conversion so undo preserves bytes."""
    return path.read_bytes().decode("utf-8")


def _make_undo_hunks(before: str, after: str, context_size: int = 3) -> list[dict]:
    """Describe only changed line ranges, plus nearby post-fix anchors."""
    before_lines = before.splitlines(keepends=True)
    after_lines = after.splitlines(keepends=True)
    matcher = difflib.SequenceMatcher(a=before_lines, b=after_lines, autojunk=False)
    hunks: list[dict] = []
    for tag, before_start, before_end, after_start, after_end in matcher.get_opcodes():
        if tag == "equal":
            continue
        hunks.append({
            "before": before_lines[before_start:before_end],
            "after": after_lines[after_start:after_end],
            "context_before": after_lines[max(0, after_start - context_size):after_start],
            "context_after": after_lines[after_end:after_end + context_size],
        })
    return hunks


def _find_undo_hunk(lines: list[str], hunk: dict) -> int | None:
    """Find one exact fix-written span; context disambiguates repeated text."""
    expected = hunk["after"]
    prefix = hunk["context_before"]
    suffix = hunk["context_after"]
    candidates: list[int] = []

    if expected:
        width = len(expected)
        candidates = [
            pos for pos in range(0, len(lines) - width + 1)
            if lines[pos:pos + width] == expected
        ]
        if len(candidates) > 1:
            contextual = [
                pos for pos in candidates
                if lines[max(0, pos - len(prefix)):pos] == prefix
                and lines[pos + width:pos + width + len(suffix)] == suffix
            ]
            if contextual:
                candidates = contextual
    else:
        # the fix deleted this span. Its post-fix insertion point is identified by
        # exact adjacent context; no guessed offset is used.
        for pos in range(len(lines) + 1):
            if (lines[max(0, pos - len(prefix)):pos] == prefix
                    and lines[pos:pos + len(suffix)] == suffix):
                candidates.append(pos)

    return candidates[0] if len(candidates) == 1 else None


def _reverse_record(current: str, record: dict) -> str:
    """Reverse recorded edits only when every fix-written span is unambiguous."""
    hunks = record.get("hunks")
    if not isinstance(hunks, list) or not hunks:
        raise ValueError("The stored demo fix has no reversible change data.")
    lines = current.splitlines(keepends=True)
    located: list[tuple[int, dict]] = []
    for hunk in hunks:
        if (not isinstance(hunk, dict)
                or not all(isinstance(hunk.get(key), list)
                           and all(isinstance(line, str) for line in hunk[key])
                           for key in ("before", "after", "context_before", "context_after"))):
            raise ValueError("The stored demo fix state is malformed.")
        pos = _find_undo_hunk(lines, hunk)
        if pos is None:
            raise ValueError("The target file no longer contains an unambiguous demo-fix span.")
        located.append((pos, hunk))

    # Refuse overlapping hunks rather than risk applying a partial inverse.
    spans = sorted((pos, pos + len(hunk["after"])) for pos, hunk in located)
    for left, right in zip(spans, spans[1:]):
        if left[1] > right[0] or left == right:
            raise ValueError("Stored demo-fix spans overlap or are ambiguous.")

    for pos, hunk in sorted(located, key=lambda item: item[0], reverse=True):
        lines[pos:pos + len(hunk["after"])] = hunk["before"]
    return "".join(lines)


# ---------------------------------------------------------------------------
# Sub-command: explain
# ---------------------------------------------------------------------------

def cmd_explain(repo_path: str, scenario_name: str) -> dict:
    """
    Gather deterministic regression evidence and return structured JSON.

    Runs the proof scenario to get expected/actual/mismatches, then enriches
    the evidence with:
      - changed files and their symbols (from git diff + AST)
      - regression node (from scenario spec + mismatch keys)
      - call-chain relationships (from symbol-level AST analysis)
      - relevant source snippets (function bodies of regression + changed symbols)
    """
    err = _validate_repo(repo_path)
    if err:
        return {"error": err}

    # 1. Load scenario spec
    try:
        spec = load_scenario(scenario_name)
    except FileNotFoundError as exc:
        return {"error": str(exc)}

    # 2. Run proof to get expected/actual/mismatches
    from proof.engine import run_scenario  # noqa: E402
    proof = run_scenario(repo_path, spec)

    # 3. Git diff — changed files
    changed_rel = _git_changed_files(repo_path)
    repo_root = Path(repo_path)
    changed_details: list[dict] = []
    for rel in changed_rel:
        abs_p = repo_root / rel
        syms: list[str] = []
        if abs_p.exists():
            try:
                src = abs_p.read_text(encoding="utf-8", errors="replace")
                syms = _top_level_symbols(src, abs_p)
            except OSError:
                pass
        changed_details.append({"file": rel, "symbols": syms})

    # 4. Identify regression nodes from mismatch keys → scenario steps
    capture_to_step: dict[str, dict] = {
        s["capture"]: s for s in spec.get("steps", [])
    }
    extract = spec.get("extract", {})
    regression_nodes: list[dict] = []
    for m in proof.get("mismatches", []):
        key = m["key"]
        espec = extract.get(key, {})
        cap = espec.get("from_capture", key)
        step = capture_to_step.get(cap)
        if step:
            regression_nodes.append({
                "file": f"{step['module']}.py",
                "symbol": step["function"],
                "node_id": f"{step['module']}.py::{step['function']}",
            })

    # 5. Call-chain — walk ALL Python files in repo to find symbol call paths
    #    between changed symbols and regression symbols. Emit a simple chain list.
    py_files = [
        p for p in repo_root.rglob("*.py")
        if "__pycache__" not in p.parts
    ]

    # Build a lightweight symbol→callee map (cross-file only)
    sym_calls: dict[str, list[str]] = {}  # "file::sym" -> ["file2::sym2", ...]

    def _resolve_call_simple(call_node: ast.Call, import_map: dict, module_map: dict, all_defs: dict) -> str | None:
        func = call_node.func
        if isinstance(func, ast.Name):
            ref = import_map.get(func.id)
            if ref:
                return ref
        elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            tfile = module_map.get(func.value.id)
            if tfile and func.attr in all_defs.get(tfile, {}):
                return f"{tfile}::{func.attr}"
        return None

    # Parse all files once
    file_trees: dict[str, tuple[dict, dict, set]] = {}  # rel -> (import_map, module_map, defined_syms)
    for py_file in py_files:
        rel = str(py_file.relative_to(repo_root))
        try:
            src = py_file.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(src, filename=str(py_file))
        except Exception:
            continue

        import_map: dict[str, str] = {}   # local_name -> "file::sym"
        module_map: dict[str, str] = {}   # local_alias -> file_rel
        defined: set[str] = set()

        for node in ast.iter_child_nodes(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                defined.add(node.name)

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    parts = alias.name.split(".")
                    candidate = repo_root.joinpath(*parts).with_suffix(".py")
                    if candidate.exists():
                        local = alias.asname if alias.asname else parts[0]
                        module_map[local] = str(candidate.relative_to(repo_root))

            elif isinstance(node, ast.ImportFrom) and node.module:
                full_mod = node.module
                parts = full_mod.split(".")
                candidate = repo_root.joinpath(*parts).with_suffix(".py")
                if candidate.exists():
                    trel = str(candidate.relative_to(repo_root))
                    for alias in node.names:
                        if alias.name == "*":
                            continue
                        local = alias.asname if alias.asname else alias.name
                        import_map[local] = f"{trel}::{alias.name}"

        file_trees[rel] = (import_map, module_map, defined)

    all_defs: dict[str, set] = {rel: info[2] for rel, info in file_trees.items()}

    for py_file in py_files:
        rel = str(py_file.relative_to(repo_root))
        info = file_trees.get(rel)
        if not info:
            continue
        import_map, module_map, defined = info
        try:
            src = py_file.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(src, filename=str(py_file))
        except Exception:
            continue

        for node in ast.iter_child_nodes(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            caller_id = f"{rel}::{node.name}"
            for call_node in ast.walk(node):
                if not isinstance(call_node, ast.Call):
                    continue
                target = _resolve_call_simple(call_node, import_map, module_map, all_defs)
                if target and target != caller_id:
                    sym_calls.setdefault(caller_id, [])
                    if target not in sym_calls[caller_id]:
                        sym_calls[caller_id].append(target)

    # BFS from each changed symbol to find path to each regression node
    def _find_path(start: str, goal: str, calls: dict) -> list[str] | None:
        from collections import deque
        visited: set[str] = {start}
        queue: deque[list[str]] = deque([[start]])
        while queue:
            path = queue.popleft()
            cur = path[-1]
            if cur == goal:
                return path
            for nxt in calls.get(cur, []):
                if nxt not in visited:
                    visited.add(nxt)
                    queue.append(path + [nxt])
        return None

    call_chains: list[list[str]] = []
    for cd in changed_details:
        for sym in cd["symbols"]:
            start = f"{cd['file']}::{sym}"
            for rn in regression_nodes:
                goal = rn["node_id"]
                path = _find_path(start, goal, sym_calls)
                if path and path not in call_chains:
                    call_chains.append(path)

    # 6. Relevant source snippets (function bodies of regression + changed symbols)
    source_snippets: list[dict] = []

    def _extract_function_source(abs_path: Path, sym_name: str) -> str | None:
        try:
            src = abs_path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(src)
        except Exception:
            return None
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == sym_name:
                lines = src.splitlines()
                body_lines = lines[node.lineno - 1: node.end_lineno]
                return "\n".join(body_lines)
        return None

    # Changed symbols
    for cd in changed_details:
        for sym in cd["symbols"]:
            abs_p = repo_root / cd["file"]
            snippet = _extract_function_source(abs_p, sym)
            if snippet:
                source_snippets.append({
                    "role": "changed",
                    "file": cd["file"],
                    "symbol": sym,
                    "source": snippet,
                })

    # Regression symbols
    for rn in regression_nodes:
        abs_p = repo_root / rn["file"]
        if abs_p.exists():
            snippet = _extract_function_source(abs_p, rn["symbol"])
            if snippet:
                source_snippets.append({
                    "role": "regression",
                    "file": rn["file"],
                    "symbol": rn["symbol"],
                    "source": snippet,
                })

    return {
        "scenario": scenario_name,
        "proof_status": proof.get("status"),
        "changed": changed_details,
        "regression_nodes": regression_nodes,
        "expected": proof.get("expected", {}),
        "actual": proof.get("actual"),
        "mismatches": proof.get("mismatches", []),
        "call_chains": call_chains,
        "source_snippets": source_snippets,
        "scenario_description": spec.get("description", ""),
        "error": proof.get("error"),
    }


# ---------------------------------------------------------------------------
# Sub-command: apply_fix
# ---------------------------------------------------------------------------

def cmd_apply_fix(repo_path: str, file_rel: str, new_source: str) -> dict:
    """
    Write new_source to file_rel inside repo_path.

    Safety rules enforced here:
    - file_rel must be inside repo_path (no path traversal).
    - file_rel must end in .py.
    - new_source must be valid Python (AST parse check).
    - The proof scenario files (proof/scenarios/) are never touched.
    - No test file modifications (files matching test_*.py or *_test.py).

    Returns {"status": "applied", "file": rel_path, "backup": backup_path,
    "undo_id": id}
    or {"error": "..."}
    """
    err = _validate_repo(repo_path)
    if err:
        return {"error": err}

    repo_root = Path(repo_path).resolve()
    target = (repo_root / file_rel).resolve()

    # Path traversal guard
    try:
        target.relative_to(repo_root)
    except ValueError:
        return {"error": f"file_rel escapes repo_path: {file_rel}"}

    if not file_rel.endswith(".py"):
        return {"error": "Only .py files may be modified."}

    # Block test files
    base = target.name
    if base.startswith("test_") or base.endswith("_test.py"):
        return {"error": f"Modifying test files is not permitted: {file_rel}"}

    # Block scenario files
    scenarios_dir = repo_root / "proof" / "scenarios"
    try:
        target.relative_to(scenarios_dir)
        return {"error": "Modifying proof scenario files is not permitted."}
    except ValueError:
        pass

    # Syntax check
    try:
        ast.parse(new_source)
    except SyntaxError as exc:
        return {"error": f"Proposed fix has a syntax error: {exc}"}

    normalized_rel = target.relative_to(repo_root).as_posix()
    try:
        state_path, lock_path = _state_paths(repo_root)
        with _undo_state_lock(lock_path):
            state = _load_undo_state(state_path)
            existed_before = target.is_file()
            before = _read_text_exact(target) if existed_before else ""
            hunks = _make_undo_hunks(before, new_source)
            if not hunks:
                return {"error": "Proposed fix does not change the target file."}

            # Preserve the existing .py.bak behavior for compatibility. The
            # backup is informational; undo uses only the exact recorded hunks.
            backup_path: str | None = None
            if existed_before:
                backup = target.with_suffix(".py.bak")
                backup.write_text(before, encoding="utf-8")
                backup_path = backup.relative_to(repo_root).as_posix()

            undo_id = uuid.uuid4().hex
            record = {
                "id": undo_id,
                "status": "pending",
                "file": normalized_rel,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "existed_before": existed_before,
                "before_sha256": _sha256(before),
                "after_sha256": _sha256(new_source),
                "hunks": hunks,
            }
            state["history"].append(record)
            _save_undo_state(state_path, state)
            try:
                _atomic_write_text(target, new_source)
            except OSError:
                state["history"].pop()
                _save_undo_state(state_path, state)
                raise

            record["status"] = "applied"
            # The pending record already contains the complete inverse patch.
            # If this metadata update fails, the successful file mutation is
            # still safely recoverable from that pending record.
            try:
                _save_undo_state(state_path, state)
            except OSError:
                pass

        return {
            "status": "applied",
            "file": normalized_rel,
            "backup": backup_path,
            "undo_id": undo_id,
        }
    except (OSError, ValueError) as exc:
        return {"error": f"Could not safely record/apply fix: {exc}"}


def cmd_undo_fix(repo_path: str) -> dict:
    """Undo only the exact line edits from the most recent successful apply_fix."""
    err = _validate_repo(repo_path)
    if err:
        return {"status": "error", "error": err}

    repo_root = Path(repo_path).resolve()
    try:
        state_path, lock_path = _state_paths(repo_root)
        with _undo_state_lock(lock_path):
            state = _load_undo_state(state_path)
            history = state["history"]
            if any(not isinstance(item, dict) for item in history):
                return {"status": "error", "error": "Undo history is ambiguous; no files were changed."}
            active = [item for item in history if item.get("status") != "undone"]
            if not active:
                return {"status": "error", "error": "No stored demo fix is available to undo."}

            record = active[-1]
            if record.get("status") not in {"applied", "pending"}:
                return {"status": "error", "error": "Latest demo fix state is ambiguous; no files were changed."}
            file_rel = record.get("file")
            if not isinstance(file_rel, str) or not file_rel:
                return {"status": "error", "error": "Stored demo fix target is invalid."}
            target = (repo_root / file_rel).resolve()
            try:
                target.relative_to(repo_root)
            except ValueError:
                return {"status": "error", "error": "Stored demo fix target escapes the repository."}
            if not target.is_file():
                return {"status": "error", "error": "Stored demo fix target is missing; no files were changed."}

            try:
                current = _read_text_exact(target)
                reversed_source = _reverse_record(current, record)
            except (OSError, UnicodeError, ValueError) as exc:
                return {"status": "error", "error": f"Undo refused safely: {exc}"}

            # If the fix created the file, remove it only if undo leaves it empty.
            # Any later developer content is retained as an existing file.
            if record.get("existed_before") is False and not reversed_source:
                target.unlink()
            else:
                _atomic_write_text(target, reversed_source)

            record["status"] = "undone"
            record["undone_at"] = datetime.now(timezone.utc).isoformat()
            try:
                _save_undo_state(state_path, state)
            except OSError as exc:
                # The source has already been safely reversed. Report this
                # metadata failure so the caller does not mistake it for a
                # fully recorded undo; a retry will fail closed on the absent
                # fix span rather than reverting any other change.
                return {
                    "status": "error",
                    "file": file_rel,
                    "error": f"demo fix was reversed, but undo state could not be updated: {exc}",
                }

            return {
                "status": "undone",
                "file": file_rel,
                "undo_id": record.get("id"),
                "description": "Reversed only the recorded demo-applied line changes.",
            }
    except (OSError, ValueError) as exc:
        return {"status": "error", "error": f"Undo refused safely: {exc}"}


# ---------------------------------------------------------------------------
# Sub-command: verify
# ---------------------------------------------------------------------------

def cmd_verify(repo_path: str, scenario_name: str) -> dict:
    """
    Re-run the named proof scenario against repo_path and return the result.

    This is the deterministic verification step — it never uses AI inference.
    Returns the full proof result dict with status PASS / REGRESSION / ERROR.
    """
    err = _validate_repo(repo_path)
    if err:
        return {"error": err, "status": "ERROR"}

    try:
        result = run_named_scenario(repo_path, scenario_name)
    except FileNotFoundError as exc:
        return {"error": str(exc), "status": "ERROR"}

    return result


# ---------------------------------------------------------------------------
# CLI dispatch
# ---------------------------------------------------------------------------

def main() -> None:
    if len(sys.argv) < 2:
        print(json.dumps({"error": "Usage: regression_tools.py <explain|apply_fix|undo_fix|verify> ..."}))
        sys.exit(0)

    sub = sys.argv[1]

    if sub == "explain":
        if len(sys.argv) < 4:
            print(json.dumps({"error": "Usage: regression_tools.py explain <repo_path> <scenario_name>"}))
            sys.exit(0)
        result = cmd_explain(sys.argv[2], sys.argv[3])

    elif sub == "apply_fix":
        if len(sys.argv) < 5:
            print(json.dumps({"error": "Usage: regression_tools.py apply_fix <repo_path> <file_rel> <new_source>"}))
            sys.exit(0)
        # new_source is the 5th arg (may contain newlines — caller must pass as single arg)
        result = cmd_apply_fix(sys.argv[2], sys.argv[3], sys.argv[4])

    elif sub == "verify":
        if len(sys.argv) < 4:
            print(json.dumps({"error": "Usage: regression_tools.py verify <repo_path> <scenario_name>"}))
            sys.exit(0)
        result = cmd_verify(sys.argv[2], sys.argv[3])

    elif sub == "undo_fix":
        if len(sys.argv) < 3:
            result = {"status": "error", "error": "Usage: regression_tools.py undo_fix <repo_path>"}
        else:
            result = cmd_undo_fix(sys.argv[2])

    else:
        result = {"error": f"Unknown sub-command: {sub}"}

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
