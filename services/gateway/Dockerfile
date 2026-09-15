FROM python:3.13-slim AS builder

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        g++ libsasl2-dev python3-dev && \
    rm -rf /var/lib/apt/lists/*

RUN pip install uv --no-cache-dir

COPY services/gateway/pyproject.toml /app/pyproject.toml
COPY services/gateway/uv.lock /app/uv.lock
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# Bug-10014: source edits must reuse the installed dependency layer.
COPY shared/ /app/shared/
COPY services/gateway/src/ /app/src/

# Bug-9960: shared tenant-readiness code is copied into this image, so prove the
# production venv can import its Alembic dependency and resolve the shipped head.
RUN PYTHONPATH=/app /app/.venv/bin/python -c \
    "from shared.db.tenant_readiness import required_tenant_revision; assert required_tenant_revision()"

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

# Security: the python:3.x-slim base lags Debian security updates (it still ships
# perl-base 5.40.1-6 while trixie carries 5.40.1-6+deb13u1). Nothing here installs
# perl, so a rebuild alone never picks the fix up. Apply the available upgrades in
# the runtime stage so inherited base-layer CVEs do not reach a release artifact.
RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*

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

# Bug-9834: the JDBC watchdog's exit tally lives here when a deployment mounts a
# volume at this path, so that the recurrence count survives container
# RECREATION and not merely a restart. Created and owned in the IMAGE because a
# named volume inherits the ownership of the image directory it is initialised
# from — mounted onto a path that does not exist, it would be root-owned, the
# tally write would fail, and recurrence detection would silently under-report
# rather than error.
RUN mkdir -p /var/lib/tessallite/gateway && \
    chown -R appuser:appgroup /var/lib/tessallite

USER appuser

EXPOSE 5433 8080

# Bug-7759: embed the source-tree commit SHA so release_bundle provenance
# checks (Bug-7483/7495) can verify image-to-source alignment.
ARG GIT_REVISION=""
LABEL org.opencontainers.image.revision=$GIT_REVISION

CMD ["python", "-m", "src.main"]
