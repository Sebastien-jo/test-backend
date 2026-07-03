# Single multi-purpose image, shared by the `api` and `worker` services.
FROM python:3.14-slim

# Pull the uv binary from its official image (pinned major.minor).
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    # Install dependencies straight into the system interpreter so that
    UV_PROJECT_ENVIRONMENT=/usr/local \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies first, from the lockfile, for reproducible cached layers.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen

# Application code. In development, docker-compose bind-mounts ./app over this
# so hot reload picks up local edits.
COPY app ./app
COPY tests ./tests

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
