FROM python:3.14-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.12.14 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PYTHONUNBUFFERED=1 \
    CATALOG_DUCKDB_EXTENSION_DIR=/opt/duckdb-extensions \
    CATALOG_WORK_DIR=/work

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev

# Bake in the httpfs extension so we don't download it at runtime
RUN uv run python -c "import duckdb; duckdb.connect(config={'extension_directory': '/opt/duckdb-extensions'}).install_extension('httpfs')" \
    && chmod -R a+rX /opt/duckdb-extensions

RUN useradd --create-home app && mkdir /work && chown app /work
USER app

ENTRYPOINT ["/app/.venv/bin/smithsonian-open-access-catalog"]
CMD []
