"""Shared DDL / materialisation timeout resolution (Bug-8041 Finding 5).

This module is the SINGLE resolver for the production DDL timeout bound.
Both ``shared/source_pool.py`` (for checkout-grace derivation) and
``shared/source_executor.py`` (for statement-level timeout application)
import from here via a normal ``from shared.config.ddl_timeout import ...``
-- no dynamic ``try/except`` import fallback, no divergent-fallback failure
mode.

Previously the resolver lived in ``source_executor.py`` and
``source_pool.py`` imported it dynamically (with a conservative-default
fallback on import failure).  That fallback silently discarded a configured
DDL-timeout floor in any build where the import genuinely failed (e.g. the
Community/Cython-compiled build path), permanently latching a smaller
checkout grace and recreating the "healthy long DDL killed early" defect
the floor mechanism exists to prevent.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

DDL_TIMEOUT_DEFAULT_SECONDS = 3600
# Documented production ceiling for the DDL/materialisation bound (24h).
DDL_TIMEOUT_MAX_SECONDS = 86400

# Bug-8041 residual review round 3 (finding 5): keys already warned about,
# so a persistent misconfiguration logs ONCE per process instead of flooding.
_warned_ddl_config_issues: set[str] = set()


def _warn_once_ddl(key: str, msg: str, *args: object) -> None:
    """Emit ``msg`` at WARNING only the first time ``key`` is seen this process."""
    if key in _warned_ddl_config_issues:
        return
    _warned_ddl_config_issues.add(key)
    logger.warning(msg, *args)


def reset_ddl_config_warning_state() -> None:
    """Test-only: clear the warn-once dedup state so a test asserting on one of
    these warnings is not silently short-circuited by an earlier test in the
    same process that already tripped the same key."""
    _warned_ddl_config_issues.clear()


def ddl_unbounded_unsafe_enabled() -> bool:
    """Whether the DDL safety bound may be DISABLED (unbounded).

    Gated behind an explicit non-production opt-in so a plain ``<=0`` sentinel
    can never silently leave remote execution/billing unbounded in production
    (Bug-8041 hardening)."""
    return os.getenv(
        "SOURCE_DDL_TIMEOUT_ALLOW_UNBOUNDED_UNSAFE", "",
    ).strip().lower() in ("1", "true", "yes", "on")


def get_ddl_timeout() -> int:
    """Deadline (seconds) for a DDL / materialisation statement (Bug-8041).

    DDL and CTAS aggregate builds legitimately run far longer than an
    interactive query, so they are bounded by their own (larger) knob rather
    than the query timeout -- otherwise a large but healthy
    ``CREATE TABLE ... AS SELECT`` would be cancelled and the aggregate left
    permanently un-buildable.

    Hardening: the production bound is ALWAYS finite and positive. Override via
    ``SOURCE_DDL_TIMEOUT_SECONDS`` (default 3600s), clamped to
    ``DDL_TIMEOUT_MAX_SECONDS`` (24h). Disabling the bound (``<=0`` -- unbounded
    remote execution + billing) is honoured ONLY behind the explicit
    non-production ``SOURCE_DDL_TIMEOUT_ALLOW_UNBOUNDED_UNSAFE`` flag; otherwise
    a ``<=0`` value is rejected and the finite default is used.
    """
    raw = os.getenv("SOURCE_DDL_TIMEOUT_SECONDS")
    if raw is None or raw.strip() == "":
        return DDL_TIMEOUT_DEFAULT_SECONDS
    try:
        val = int(raw)
    except (TypeError, ValueError):
        _warn_once_ddl(
            "ddl_timeout_invalid",
            "Invalid SOURCE_DDL_TIMEOUT_SECONDS=%r; using default %ds",
            raw, DDL_TIMEOUT_DEFAULT_SECONDS,
        )
        return DDL_TIMEOUT_DEFAULT_SECONDS
    if val <= 0:
        if ddl_unbounded_unsafe_enabled():
            return 0  # unbounded -- DEV ONLY, explicit unsafe opt-in
        _warn_once_ddl(
            "ddl_timeout_nonpositive_refused",
            "SOURCE_DDL_TIMEOUT_SECONDS<=0 (unbounded remote execution) refused "
            "without SOURCE_DDL_TIMEOUT_ALLOW_UNBOUNDED_UNSAFE; using default %ds",
            DDL_TIMEOUT_DEFAULT_SECONDS,
        )
        return DDL_TIMEOUT_DEFAULT_SECONDS
    return min(val, DDL_TIMEOUT_MAX_SECONDS)


def get_effective_ddl_timeout_seconds() -> int:
    """Public accessor for the production DDL/materialisation timeout bound.

    Bug-8041 residual 2: the source pool's security-age checkout grace
    (``shared/source_pool.py``) must never independently invent a value shorter
    than this bound, or security-age retirement force-terminates a healthy,
    still-within-budget DDL/materialisation before its own deadline. Both
    ``source_pool.py`` and ``source_executor.py`` import this from here
    (a neutral config module) so there is exactly one resolver with no
    divergent-fallback failure mode.
    """
    return get_ddl_timeout()
