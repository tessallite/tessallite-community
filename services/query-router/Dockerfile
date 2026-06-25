# SQL Server ODBC driver requires amd64 — arm64 builds must omit msodbcsql17
FROM python:3.12-slim AS builder

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

# ── compile closed modules to native .so, drop source (Phase 6) ──
# query-router's closed IP (aggregate/pocket matchers + exactness validator +
# rewrite-to-aggregate/pocket) and the shared closed license policy ship compiled,
# not as readable Python. Compile-only (no logic change). Image layout is flattened
# (src -> /app/src). Kept in sync with build_closed.py CLOSED_MODULES (leak-check).
RUN pip install --no-cache-dir cython && \
    inc="$(python3 -c 'import sysconfig; print(sysconfig.get_path("include"))')" && \
    for m in /app/shared/licensing/closed/manager_impl \
             /app/src/routing/aggregate_matcher \
             /app/src/routing/pocket_matcher \
             /app/src/routing/exactness_validator \
             /app/src/rewrite/aggregate \
             /app/src/rewrite/pocket; do \
        cython -3 -X annotation_typing=False "$m.py" -o "$m.c" && \
        gcc -shared -fPIC -I"$inc" "$m.c" -o "$m.so" && \
        rm -f "$m.py" "$m.c"; \
    done

# ── runtime stage ──
FROM python:3.12-slim

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

ENV PATH="/app/.venv/bin:$PATH"
ENV VIRTUAL_ENV="/app/.venv"
ENV PYTHONPATH="/app"

RUN addgroup --system --gid 1001 appgroup && \
    adduser --system --uid 1001 --ingroup appgroup appuser
USER appuser

EXPOSE 8000

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
