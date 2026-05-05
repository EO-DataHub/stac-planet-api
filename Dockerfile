# syntax=docker/dockerfile:1@sha256:2780b5c3bab67f1f76c781860de469442999ed1a0d7992a5efdf2cffc0e3d769
FROM ghcr.io/astral-sh/uv:python3.13-trixie-slim@sha256:de2a45b93999c5156f65fa8cd0228e3973eccf81fe42cfa02bd6e70fe7006b9b

ENV UV_NO_DEV=1

WORKDIR /app

# Install dependencies
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-install-project

# Copy project files
COPY . /app

# Sync the project
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen

EXPOSE 8000

ENTRYPOINT ["uv", "run", "--no-sync", "uvicorn", "stac_planet_api.api:app", "--host", "0.0.0.0", "--port", "8000"]
