FROM python:3.12-slim AS builder

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        g++ libsasl2-dev python3-dev && \
    rm -rf /var/lib/apt/lists/*

RUN pip install uv --no-cache-dir

COPY services/gateway/pyproject.toml /app/pyproject.toml
COPY services/gateway/uv.lock /app/uv.lock
COPY shared/ /app/shared/
COPY services/gateway/src/ /app/src/

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# Drop the closed license-policy source (Phase 6). Only model-service enforces
# caps (calls load_manager); this service never does, so no closed source ships
# here — load_manager would fall back to the open UnactivatedManager stub.
RUN rm -f /app/shared/licensing/closed/manager_impl.py

# ── runtime stage ──
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        libsasl2-2 && \
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

EXPOSE 5433 8080

CMD ["python", "-m", "src.main"]
