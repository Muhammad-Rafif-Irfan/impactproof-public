FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8000

RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN addgroup --system impactproof && adduser --system --ingroup impactproof impactproof

COPY --chown=impactproof:impactproof . /app

# Recreate the demo repository's Git baseline inside the image.
# The deployed demo intentionally keeps discount.py modified so
# ImpactProof can detect the change at runtime.
RUN cd /app/demo \
    && rm -rf .git \
    && chown -R impactproof:impactproof /app/demo \
    && su -s /bin/sh impactproof -c '\
        cd /app/demo && \
        cp discount.py /tmp/impactproof-current-discount.py && \
        printf "%s\n" \
          "def calculate_total(price: float, is_premium: bool = False) -> float:" \
          "    return price" \
          > discount.py && \
        git init && \
        git config user.name "ImpactProof Demo" && \
        git config user.email "demo@impactproof.local" && \
        git add . && \
        git commit -m "Baseline demo state" && \
        cp /tmp/impactproof-current-discount.py discount.py && \
        rm /tmp/impactproof-current-discount.py'

USER impactproof

EXPOSE 8000

CMD ["python", "graph/public_server.py"]
