FROM python:3.14-slim AS base
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /code/
COPY pyproject.toml uv.lock ./

ENV UV_PROJECT_ENVIRONMENT="/usr/local/"
RUN uv sync --no-dev --frozen

COPY src/ src/
COPY scripts/ scripts/

FROM base AS test
# Authoritative native types: the component emits the `schema` manifest form and
# the recorded expected/ fixtures assume it — without this the SDK auto-detects
# legacy mode and produces columns+column_metadata instead.
ENV KBC_DATA_TYPE_SUPPORT=authoritative
RUN uv sync --all-groups --frozen
COPY tests/ tests/
RUN uv run ruff check src/ tests/
CMD ["uv", "run", "pytest", "tests/", "-v"]

FROM base AS production
CMD ["python", "-u", "/code/src/component.py"]
