FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8000
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
RUN addgroup --system impactproof && adduser --system --ingroup impactproof impactproof
COPY --chown=impactproof:impactproof . /app
USER impactproof
EXPOSE 8000
CMD ["python", "graph/public_server.py"]
