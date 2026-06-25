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

COPY services/model-service/pyproject.toml /app/pyproject.toml
COPY services/model-service/uv.lock /app/uv.lock
COPY shared/ /app/shared/
COPY scripts/__init__.py /app/scripts/__init__.py
COPY scripts/lookml_export/ /app/scripts/lookml_export/
COPY services/model-service/src/ /app/src/

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# ── compile closed modules to native .so, drop source (Phase 6) ──
# Only the shared closed license policy is present in this image (query-router /
# optimizer closed modules ship compiled in their own images). Kept in sync with
# scripts/community_release/build_closed.py:CLOSED_MODULES (leak-check enforces the
# canonical set). cythonize is inlined because build_closed.py sits outside this
# image's build context (tessallite/).
RUN pip install --no-cache-dir cython && \
    cd /app/shared/licensing/closed && \
    cython -3 -X annotation_typing=False manager_impl.py -o manager_impl.c && \
    gcc -shared -fPIC \
        -I"$(python3 -c 'import sysconfig; print(sysconfig.get_path("include"))')" \
        manager_impl.c -o manager_impl.so && \
    rm -f manager_impl.py manager_impl.c

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
COPY --from=builder /app/scripts /app/scripts
COPY --from=builder /app/src /app/src

ENV PATH="/app/.venv/bin:$PATH"
ENV VIRTUAL_ENV="/app/.venv"
ENV PYTHONPATH="/app"

RUN addgroup --system --gid 1001 appgroup && \
    adduser --system --uid 1001 --ingroup appgroup appuser
USER appuser

EXPOSE 8001

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8001"]
