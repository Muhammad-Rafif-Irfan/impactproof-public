#!/usr/bin/env python3
"""
ImpactProof static dependency analyzer.

Usage:
    python analyze.py <repo_path>

Prints a single JSON object to stdout describing the blast radius of the
current Git working-tree changes relative to HEAD.  All diagnostic output
goes to stderr so as not to corrupt the JSON stream.

Exit codes:
    0  success (even for validation errors — those are returned as JSON)
    1  unexpected internal error
"""

import ast
import json
import os
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path


# ---------------------------------------------------------------------------
# Structured error helper
# ---------------------------------------------------------------------------

def error_result(repo_path: str, message: str) -> dict:
    return {
        "repo_path": repo_path,
        "error": message,
        "changed_files": [],
        "directly_affected": [],
        "indirectly_affected": [],
        "relationships": [],
        "summary": {"changed_files": 0, "affected_files": 0},
    }


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_repo(repo_path: str) -> str | None:
    """
    Return None if repo_path is a valid git repository root, otherwise return
    an error message string.
    """
    if not os.path.exists(repo_path):
        return f"repo_path does not exist: {repo_path}"
    if not os.path.isdir(repo_path):
        return f"repo_path is not a directory: {repo_path}"
    # Check that git recognises this directory
    result = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return f"repo_path is not a Git repository: {repo_path}"
    # Ensure HEAD resolves (i.e. at least one commit exists)
    head_check = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    if head_check.returncode != 0:
        return (
            "Git repository has no commits yet — nothing to diff against. "
            "Make an initial commit first."
        )
    return None


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def git_changed_files(repo_path: str) -> list[str]:
    """
    Return de-duplicated relative paths of files changed vs HEAD
    (both staged and unstaged).
    """
    seen: set[str] = set()
    unique: list[str] = []

    for extra_args in ([], ["--cached"]):
        try:
            result = subprocess.run(
                ["git", "diff", "--name-only", "HEAD"] + extra_args,
                cwd=repo_path,
                capture_output=True,
                text=True,
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            print(f"git diff failed: {exc.stderr.strip()}", file=sys.stderr)
            continue

        for line in result.stdout.splitlines():
            line = line.strip()
            if line and line not in seen:
                seen.add(line)
                unique.append(line)

    return unique


# ---------------------------------------------------------------------------
# Python AST — symbol extraction
# ---------------------------------------------------------------------------

def extract_top_level_symbols(source: str, filepath: Path) -> list[str]:
    """
    Return the names of top-level functions and classes defined in *source*.
    Returns an empty list on syntax error (logged to stderr).
    """
    try:
        tree = ast.parse(source, filename=str(filepath))
    except SyntaxError as exc:
        print(f"SyntaxError in {filepath}: {exc}", file=sys.stderr)
        return []
    return [
        node.name
        for node in ast.iter_child_nodes(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]


# ---------------------------------------------------------------------------
# Python AST — import resolution
# ---------------------------------------------------------------------------

def file_for_module(module: str, repo_root: Path) -> Path | None:
    """
    Resolve a dotted module name to a .py file inside the repo.
    Returns None when the module is external / stdlib.
    """
    parts = module.split(".")
    candidate = repo_root.joinpath(*parts).with_suffix(".py")
    if candidate.exists():
        return candidate
    candidate2 = repo_root.joinpath(*parts, "__init__.py")
    if candidate2.exists():
        return candidate2
    return None


def parse_imports(source: str, filepath: Path, repo_root: Path) -> list[dict]:
    """
    Return a list of import dicts for each in-repo import found in *source*:
        {"module": str, "names": [str, ...], "resolved_file": str}
    Unresolvable (external/stdlib) imports are excluded.
    """
    try:
        tree = ast.parse(source, filename=str(filepath))
    except SyntaxError as exc:
        print(f"SyntaxError in {filepath}: {exc}", file=sys.stderr)
        return []

    imports: list[dict] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                resolved = file_for_module(alias.name, repo_root)
                if resolved:
                    imports.append({
                        "module": alias.name,
                        "names": [],
                        "resolved_file": str(resolved.relative_to(repo_root)),
                    })

        elif isinstance(node, ast.ImportFrom):
            if node.module is None:
                continue
            if node.level and node.level > 0:
                # Relative import — resolve from the file's package
                pkg_parts = list(filepath.relative_to(repo_root).parent.parts)
                up = node.level - 1
                base_parts = pkg_parts[: len(pkg_parts) - up] if up < len(pkg_parts) else []
                full_module = (
                    ".".join(base_parts + [node.module]) if node.module else ".".join(base_parts)
                )
            else:
                full_module = node.module

            resolved = file_for_module(full_module, repo_root)
            if resolved:
                names = [alias.name for alias in node.names if alias.name != "*"]
                imports.append({
                    "module": full_module,
                    "names": names,
                    "resolved_file": str(resolved.relative_to(repo_root)),
                })

    return imports


def build_dependency_graph(py_files: list[Path], repo_root: Path) -> dict[str, list[dict]]:
    """
    Build {relative_path -> [import_info, ...]} for every Python file that has
    at least one in-repo import.
    """
    graph: dict[str, list[dict]] = {}
    for py_file in py_files:
        rel = str(py_file.relative_to(repo_root))
        try:
            source = py_file.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(f"Cannot read {py_file}: {exc}", file=sys.stderr)
            continue
        imports = parse_imports(source, py_file, repo_root)
        if imports:
            graph[rel] = imports
    return graph


# ---------------------------------------------------------------------------
# Phase 2 — Symbol-level relationship graph
# ---------------------------------------------------------------------------

@dataclass
class SymbolRef:
    """A resolved reference to a concrete (file, symbol) pair."""
    file: str   # relative path from repo root
    symbol: str


@dataclass
class FileSymbolTable:
    """
    Per-file view built from AST analysis.

    import_map   : local_name -> SymbolRef (from in-repo imports)
    module_map   : local_module_alias -> resolved_file (from bare `import X`)
    defined      : symbol_name -> ast node (FunctionDef / AsyncFunctionDef / ClassDef)
    rel_path     : this file's path relative to repo root
    """
    rel_path: str
    import_map: dict[str, SymbolRef] = field(default_factory=dict)
    module_map: dict[str, str] = field(default_factory=dict)
    defined: dict[str, ast.AST] = field(default_factory=dict)


def build_file_symbol_tables(
    py_files: list[Path], repo_root: Path
) -> dict[str, FileSymbolTable]:
    """
    Parse every Python file and build a FileSymbolTable for it.
    Returns {relative_path: FileSymbolTable}.
    """
    tables: dict[str, FileSymbolTable] = {}

    for py_file in py_files:
        rel = str(py_file.relative_to(repo_root))
        try:
            source = py_file.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(f"Cannot read {py_file}: {exc}", file=sys.stderr)
            continue

        try:
            tree = ast.parse(source, filename=str(py_file))
        except SyntaxError as exc:
            print(f"SyntaxError in {py_file}: {exc}", file=sys.stderr)
            continue

        table = FileSymbolTable(rel_path=rel)

        # ---- collect defined top-level symbols ----
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                table.defined[node.name] = node

        # ---- collect in-repo imports ----
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    resolved = file_for_module(alias.name, repo_root)
                    if resolved:
                        local = alias.asname if alias.asname else alias.name.split(".")[0]
                        table.module_map[local] = str(resolved.relative_to(repo_root))

            elif isinstance(node, ast.ImportFrom):
                if node.module is None:
                    continue
                if node.level and node.level > 0:
                    pkg_parts = list(py_file.relative_to(repo_root).parent.parts)
                    up = node.level - 1
                    base_parts = pkg_parts[: len(pkg_parts) - up] if up < len(pkg_parts) else []
                    full_module = (
                        ".".join(base_parts + [node.module])
                        if node.module
                        else ".".join(base_parts)
                    )
                else:
                    full_module = node.module

                resolved = file_for_module(full_module, repo_root)
                if not resolved:
                    continue
                resolved_rel = str(resolved.relative_to(repo_root))

                for alias in node.names:
                    if alias.name == "*":
                        continue
                    local = alias.asname if alias.asname else alias.name
                    table.import_map[local] = SymbolRef(
                        file=resolved_rel, symbol=alias.name
                    )

        tables[rel] = table

    return tables


def _resolve_call_target(
    call_node: ast.Call,
    table: FileSymbolTable,
    all_tables: dict[str, FileSymbolTable],
) -> SymbolRef | None:
    """
    Try to resolve an ast.Call node to a concrete SymbolRef.

    Handles two patterns:
      - bare Name:     `foo()`   -> look up local import_map, then defined symbols
      - Attribute:     `mod.foo()` where `mod` is in module_map
    Returns None when the target cannot be confidently resolved.
    """
    func = call_node.func

    if isinstance(func, ast.Name):
        name = func.id
        # Check imported symbol
        if name in table.import_map:
            ref = table.import_map[name]
            # Verify the symbol actually exists in the target file
            target_table = all_tables.get(ref.file)
            if target_table and ref.symbol in target_table.defined:
                return ref
            # Symbol may be re-exported or is a class; keep it if the file exists
            if target_table:
                return ref
        # Locally defined — no cross-file relationship
        return None

    if isinstance(func, ast.Attribute):
        # Pattern: module_alias.symbol()
        if isinstance(func.value, ast.Name):
            mod_alias = func.value.id
            attr_name = func.attr
            if mod_alias in table.module_map:
                target_file = table.module_map[mod_alias]
                target_table = all_tables.get(target_file)
                if target_table and attr_name in target_table.defined:
                    return SymbolRef(file=target_file, symbol=attr_name)

    return None


def build_symbol_relationships(
    py_files: list[Path],
    repo_root: Path,
    all_tables: dict[str, FileSymbolTable],
    changed_py: list[str],
) -> list[dict]:
    """
    Emit symbol_relationship records for every (caller_symbol → callee_symbol) edge
    that crosses a file boundary AND involves a changed file on either end.

    We restrict to relationships that touch the blast radius to keep output focused.
    The set of relevant files is: changed files ∪ directly/indirectly affected files
    (computed cheaply from the import graph already built).
    """
    # Collect all files reachable from changed files via imports (blast-radius set)
    # — we emit relationships for any caller that lives in the blast-radius set
    # when the callee lives in the changed set, OR any caller in the changed set.
    changed_set = set(changed_py)

    # Build a quick reverse-reachability set: files that (transitively) import
    # a changed file, plus the changed files themselves.
    reverse: dict[str, set[str]] = defaultdict(set)
    for table in all_tables.values():
        for ref in table.import_map.values():
            reverse[ref.file].add(table.rel_path)
        for mod_alias, target_file in table.module_map.items():
            reverse[target_file].add(table.rel_path)

    blast: set[str] = set(changed_set)
    frontier = set(changed_set)
    while frontier:
        nxt: set[str] = set()
        for f in frontier:
            for importer in reverse.get(f, set()):
                if importer not in blast:
                    blast.add(importer)
                    nxt.add(importer)
        frontier = nxt

    # Deduplicate: (from_file, from_sym, to_file, to_sym) -> dict
    seen: set[tuple[str, str, str, str]] = set()
    rels: list[dict] = []

    for rel, table in sorted(all_tables.items()):
        # Only emit from files in the blast radius
        if rel not in blast:
            continue

        for sym_name, sym_node in sorted(table.defined.items()):
            # Walk the function/class body for Call nodes
            for call_node in ast.walk(sym_node):
                if not isinstance(call_node, ast.Call):
                    continue
                ref = _resolve_call_target(call_node, table, all_tables)
                if ref is None:
                    continue
                # Deduplicate
                key = (rel, sym_name, ref.file, ref.symbol)
                if key in seen:
                    continue
                seen.add(key)
                rels.append({
                    "from": {"file": rel, "symbol": sym_name},
                    "to": {"file": ref.file, "symbol": ref.symbol},
                    "type": "calls_or_uses",
                    "reason": f"{rel}.{sym_name} uses {ref.symbol} from {ref.file}",
                })

    return rels


# ---------------------------------------------------------------------------
# Blast-radius computation
# ---------------------------------------------------------------------------

def compute_blast_radius(
    changed_files: list[str],
    graph: dict[str, list[dict]],
) -> tuple[list[str], list[str], list[dict]]:
    """
    BFS over the reverse import graph starting from changed_files.
    Returns:
        directly_affected  — files that directly import a changed file (depth 1)
        indirectly_affected — files reachable at depth >= 2
        relationships       — one record per directed edge traversed
    """
    relationships: list[dict] = []

    # Build reverse map: target_file -> {importer_file, ...}
    reverse: dict[str, set[str]] = defaultdict(set)
    for importer, imports in graph.items():
        for imp in imports:
            reverse[imp["resolved_file"]].add(importer)

    directly_affected: set[str] = set()
    indirectly_affected: set[str] = set()
    visited: set[str] = set(changed_files)

    def record_edge(source_file: str, importer: str) -> None:
        for imp in graph.get(importer, []):
            if imp["resolved_file"] == source_file:
                names = imp["names"]
                reason = (
                    f"{importer} imports {', '.join(names)} from {source_file}"
                    if names
                    else f"{importer} imports {source_file}"
                )
                relationships.append({
                    "from": source_file,
                    "to": importer,
                    "type": "import",
                    "reason": reason,
                })

    # Level 1 — direct
    frontier: set[str] = set()
    for cf in changed_files:
        for importer in sorted(reverse.get(cf, set())):
            if importer not in visited:
                directly_affected.add(importer)
                frontier.add(importer)
                visited.add(importer)
                record_edge(cf, importer)

    # Level 2+ — indirect (BFS)
    while frontier:
        next_frontier: set[str] = set()
        for node in sorted(frontier):
            for importer in sorted(reverse.get(node, set())):
                if importer not in visited:
                    indirectly_affected.add(importer)
                    next_frontier.add(importer)
                    visited.add(importer)
                    record_edge(node, importer)
        frontier = next_frontier

    return sorted(directly_affected), sorted(indirectly_affected), relationships


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: analyze.py <repo_path>", file=sys.stderr)
        sys.exit(1)

    repo_path = os.path.abspath(sys.argv[1])

    # --- Validation ---
    err = validate_repo(repo_path)
    if err:
        print(json.dumps(error_result(repo_path, err), indent=2))
        return

    # --- Git diff ---
    all_changed = git_changed_files(repo_path)
    changed_py = [f for f in all_changed if f.endswith(".py")]

    if not changed_py:
        print(json.dumps({
            "repo_path": repo_path,
            "changed_files": [],
            "directly_affected": [],
            "indirectly_affected": [],
            "relationships": [],
            "symbol_relationships": [],
            "summary": {"changed_files": 0, "affected_files": 0, "symbol_relationships": 0},
        }, indent=2))
        return

    # --- Symbol extraction for changed files ---
    changed_files_detail: list[dict] = []
    repo_root = Path(repo_path)
    for rel_path in changed_py:
        abs_path = repo_root / rel_path
        symbols: list[str] = []
        if abs_path.exists():
            try:
                source = abs_path.read_text(encoding="utf-8", errors="replace")
                symbols = extract_top_level_symbols(source, abs_path)
            except OSError as exc:
                print(f"Cannot read {abs_path}: {exc}", file=sys.stderr)
        changed_files_detail.append({"file": rel_path, "symbols": symbols})

    # --- Build dependency graph and compute blast radius ---
    py_files = [p for p in repo_root.rglob("*.py") if "__pycache__" not in p.parts]
    graph = build_dependency_graph(py_files, repo_root)
    directly_affected, indirectly_affected, relationships = compute_blast_radius(
        changed_py, graph
    )

    # --- Phase 2: symbol-level relationships ---
    all_tables = build_file_symbol_tables(py_files, repo_root)
    symbol_relationships = build_symbol_relationships(
        py_files, repo_root, all_tables, changed_py
    )

    affected_files = len(set(directly_affected) | set(indirectly_affected))

    result = {
        "repo_path": repo_path,
        "changed_files": changed_files_detail,
        "directly_affected": directly_affected,
        "indirectly_affected": indirectly_affected,
        "relationships": relationships,
        "symbol_relationships": symbol_relationships,
        "summary": {
            "changed_files": len(changed_py),
            "affected_files": affected_files,
            "symbol_relationships": len(symbol_relationships),
        },
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
