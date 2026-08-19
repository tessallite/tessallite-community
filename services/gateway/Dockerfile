FROM python:3.13-slim AS builder

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
# Dir-exists guard: the public tessallite-community export omits
# shared/licensing/closed/ entirely, so a bare `find` on the absent path would
# exit 1 and fail community source builds. Guarding keeps both the private
# (dir present) and community (dir absent) source builds green.
RUN if [ -d /app/shared/licensing/closed ]; then \
        find /app/shared/licensing/closed -type f -name '*.py' -delete; \
    fi

# ── runtime stage ──
FROM python:3.13-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        libsasl2-2 && \
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

EXPOSE 5433 8080

# Bug-7759: embed the source-tree commit SHA so release_bundle provenance
# checks (Bug-7483/7495) can verify image-to-source alignment.
ARG GIT_REVISION=""
LABEL org.opencontainers.image.revision=$GIT_REVISION

CMD ["python", "-m", "src.main"]
