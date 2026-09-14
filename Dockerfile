# syntax=docker/dockerfile:1

# ---- build stage -------------------------------------------------------
FROM python:3.14-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.12.12 /uv /bin/uv

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first, in their own layer: they change far less often than the
# source, so edits to src/ do not invalidate the dependency install.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project --no-dev --no-editable

# README.md and LICENSE are build inputs, not documentation: pyproject names
# them in `readme` and `license-files`, and the build fails without them.
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src

# --no-editable bakes the package into the venv, so the runtime image needs
# only .venv and never sees src/.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# ---- runtime stage -----------------------------------------------------
FROM python:3.14-slim

RUN groupadd --system bot && useradd --system --gid bot --no-create-home bot

WORKDIR /app

COPY --from=builder --chown=bot:bot /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER bot

CMD ["python", "-m", "discord_bot_v3"]
