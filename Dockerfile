# syntax=docker/dockerfile:1.7
# ---- build: resolve dependencies into a virtualenv -------------------------
FROM python:3.12-slim AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
RUN python -m venv /venv
COPY pyproject.toml README.md ./
COPY atlas ./atlas
RUN /venv/bin/pip install ".[ai]"

# ---- runtime: small, non-root ---------------------------------------------
FROM python:3.12-slim AS runtime
ENV PATH="/venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    ATLAS_DB_PATH=/data/atlas.db
RUN useradd --create-home --uid 10001 atlas && mkdir /data && chown atlas /data
WORKDIR /app
COPY --from=build /venv /venv
COPY atlas ./atlas
COPY web ./web
USER atlas
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz')" || exit 1
CMD ["uvicorn", "atlas.api:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
