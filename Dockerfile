# syntax=docker/dockerfile:1.7
# ---- build: resolve dependencies into a virtualenv -------------------------
FROM python:3.12-slim AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
RUN python -m venv /venv
COPY pyproject.toml README.md ./
COPY atlas ./atlas
RUN /venv/bin/pip install ".[ai,conectores]"

# ---- runtime: small, non-root ---------------------------------------------
FROM python:3.12-slim AS runtime
ENV PATH="/venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    ATLAS_DB_PATH=/data/atlas.db ATLAS_RULES_PATH=/data/reglas.json
RUN apt-get update && apt-get install -y --no-install-recommends tzdata && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 10001 atlas && mkdir /data && chown atlas /data
WORKDIR /app
COPY --from=build /venv /venv
COPY atlas ./atlas
COPY web ./web
USER atlas
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s \
  CMD python -c "import os, urllib.request; urllib.request.urlopen(f\"http://localhost:{os.getenv('PORT', '8000')}/healthz\")" || exit 1
# Render, Railway y Fly asignan el puerto en $PORT; en local queda en 8000.
CMD ["sh", "-c", "exec uvicorn atlas.api:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
