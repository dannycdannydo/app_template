# Backend image (blueprint §36, ADR-0008).
#
# One image serves CI, both production profiles, and the full-container
# `make dev-docker` path. The default command runs the API; the Dramatiq
# worker runs by overriding the command (for example
# `docker compose run --rm api dramatiq app.workers`), so a single image
# covers every runtime profile.

FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy

# Non-root runtime user (container hardening; the process never runs as root).
RUN groupadd --system --gid 10001 app && \
    useradd --system --uid 10001 --gid app --home-dir /app --shell /usr/sbin/nologin app

WORKDIR /app

# uv is pinned to the same major version used locally so the lock file and the
# containerised toolchain always agree.
COPY --from=ghcr.io/astral-sh/uv:0.12.1 /uv /uvx /bin/

# Install runtime dependencies first so image layers cache on lockfile changes
# only. Dev dependencies (Ruff, Pyright, pytest) never enter the image.
# The uv cache is mounted so repeated builds (and CI runs) reuse downloaded
# wheels instead of re-fetching them from PyPI.
#
# On networks where files.pythonhosted.org is slow or blocked, pass
# `--build-arg UV_VERBOSE=-v` to stream per-file download progress and
# `--build-arg UV_VERBOSE=-vv` for connection-level diagnostics.
ARG UV_VERBOSE=
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-group dev ${UV_VERBOSE} && \
    python -m pip uninstall --yes pip

# Runtime dependency installation is deliberately unavailable: uv installs the
# locked environment above, and removing the base image's otherwise-unused pip
# also removes its vendored packages from the production attack surface.

# Put the project virtualenv on PATH so runtime commands (uvicorn, alembic,
# and later the Dramatiq worker) run directly. Avoids invoking `uv run` at
# runtime, which would re-sync the intentionally-excluded dev group on every
# container start.
ENV PATH="/app/.venv/bin:$PATH"

# Application source and Alembic configuration.
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./

# Give the non-root user ownership of the project directory (uv may write
# cache/venv metadata there during runtime commands).
RUN chown -R app:app /app

USER app

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
