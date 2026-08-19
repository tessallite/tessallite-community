# SQL Server ODBC driver requires amd64 — arm64 builds must omit msodbcsql17
FROM python:3.13-slim AS builder

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        g++ libsasl2-dev python3-dev unixodbc-dev curl gnupg && \
    curl -fsSL https://packages.microsoft.com/keys/microsoft.asc \
        | gpg --dearmor -o /usr/share/keyrings/microsoft.gpg && \
    echo "deb [arch=amd64 signed-by=/usr/share/keyrings/microsoft.gpg] https://packages.microsoft.com/debian/12/prod bookworm main" \
        > /etc/apt/sources.list.d/mssql-release.list && \
    apt-get update && \
    ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql17 && \
    rm -rf /var/lib/apt/lists/*

RUN pip install uv --no-cache-dir

COPY services/query-router/pyproject.toml /app/pyproject.toml
COPY services/query-router/uv.lock /app/uv.lock
COPY shared/ /app/shared/
COPY services/query-router/src/ /app/src/

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# calendar_type="hijri" needs hijri-converter (shared/semantic/calendar_dialects.py
# imports `from hijri_converter import Gregorian`). It is not in uv.lock because
# `shared/` is copied onto the pythonpath rather than installed as a locked package,
# so install it into the synced venv explicitly. Pinned to the 2.x line whose import
# name is `hijri_converter` (3.x renames it to `hijridate`).
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --python /app/.venv/bin/python "hijri-converter==2.3.1"

# Source-only export: closed modules removed; native compilation is not required.

# ── runtime stage ──
FROM python:3.13-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        libsasl2-2 unixodbc curl gnupg && \
    curl -fsSL https://packages.microsoft.com/keys/microsoft.asc \
        | gpg --dearmor -o /usr/share/keyrings/microsoft.gpg && \
    echo "deb [arch=amd64 signed-by=/usr/share/keyrings/microsoft.gpg] https://packages.microsoft.com/debian/12/prod bookworm main" \
        > /etc/apt/sources.list.d/mssql-release.list && \
    apt-get update && \
    ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql17 && \
    apt-get purge -y --auto-remove curl gnupg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/shared /app/shared
COPY --from=builder /app/src /app/src

# Bug-6441: remove private license-issuer source (PRIVATE_REMOVE_PATHS)
RUN rm -rf /app/shared/licensing/issuer

ENV PATH="/app/.venv/bin:$PATH"
ENV VIRTUAL_ENV="/app/.venv"
ENV PYTHONPATH="/app"

RUN addgroup --system --gid 1001 appgroup && \
    adduser --system --uid 1001 --ingroup appgroup appuser
USER appuser

EXPOSE 8000

# Bug-7759: embed the source-tree commit SHA so release_bundle provenance
# checks (Bug-7483/7495) can verify image-to-source alignment.
ARG GIT_REVISION=""
LABEL org.opencontainers.image.revision=$GIT_REVISION

# Bug-8041 R7 HIGH-2: uvicorn MUST be given an explicit graceful-shutdown
# timeout. With the default (unset) uvicorn's shutdown does
# `asyncio.wait_for(self._wait_tasks_to_complete(), timeout=None)` -- it waits
# INDEFINITELY for in-flight requests/connections and only AFTER that runs
# `self.lifespan.shutdown()`, the hook that calls close_all_pools(). One
# long-running query therefore meant the pool manager's whole shutdown budget
# never started before the platform SIGKILLed the container.
# The value is DERIVED from the single shutdown-budget source of truth
# (shared/config/shutdown_budget.py) so this flag, the compose/Cloud-Run
# stop grace and SOURCE_POOL_SHUTDOWN_GRACE_SECONDS cannot drift apart.
# `exec` keeps uvicorn as PID 1 so it still receives SIGTERM directly.
CMD ["sh", "-c", "python -m shared.config.shutdown_budget --check; exec uvicorn src.main:app --host 0.0.0.0 --port 8000 --timeout-graceful-shutdown \"$(python -m shared.config.shutdown_budget request_drain_seconds 2>/dev/null || echo 6)\""]
