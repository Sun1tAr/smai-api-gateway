# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:0.12.22 AS uv

FROM python:3.12-slim AS builder
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev --no-editable

FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH="/app/.venv/bin:$PATH"
WORKDIR /app
RUN groupadd --gid 10001 gateway \
    && useradd --uid 10001 --gid gateway --create-home gateway
COPY --from=builder /app/.venv /app/.venv
COPY config ./config
COPY contracts ./contracts
USER gateway
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=10s --start-period=10s --retries=3 \
    CMD ["python", "-c", "from urllib.request import ProxyHandler, build_opener; build_opener(ProxyHandler({})).open('http://127.0.0.1:8000/health', timeout=8).close()"]
# Один процесс: счётчики и пул соединений живут в памяти процесса.
CMD ["uvicorn", "gateway.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log", "--no-server-header", "--no-proxy-headers"]

FROM runtime AS test
USER root
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never RUFF_CACHE_DIR=/tmp/ruff-cache
COPY pyproject.toml uv.lock ./
COPY src ./src
COPY tests ./tests
COPY .env.example ./
RUN uv sync --locked --no-editable
USER gateway
HEALTHCHECK NONE
CMD ["pytest", "-p", "no:cacheprovider", "-q"]

FROM runtime AS production
