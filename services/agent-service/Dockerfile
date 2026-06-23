FROM python:3.12-slim AS builder

WORKDIR /app

RUN pip install uv --no-cache-dir

COPY services/agent-service/pyproject.toml /app/pyproject.toml
COPY services/agent-service/uv.lock /app/uv.lock
COPY shared/ /app/shared/
COPY services/agent-service/config/ /app/config/
COPY services/agent-service/src/ /app/src/

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# Drop the closed license-policy source (Phase 6). Only model-service enforces
# caps (calls load_manager); this service never does, so no closed source ships
# here — load_manager would fall back to the open UnactivatedManager stub.
RUN rm -f /app/shared/licensing/closed/manager_impl.py

# ── runtime stage ──
FROM python:3.12-slim

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/shared /app/shared
COPY --from=builder /app/config /app/config
COPY --from=builder /app/src /app/src

ENV PATH="/app/.venv/bin:$PATH"
ENV VIRTUAL_ENV="/app/.venv"
ENV PYTHONPATH="/app"

RUN addgroup --system --gid 1001 appgroup && \
    adduser --system --uid 1001 --ingroup appgroup appuser
USER appuser

EXPOSE 8000

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
