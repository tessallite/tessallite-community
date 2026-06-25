"""Deployed-snapshot resolution layer (B15 / F-013-01, gate G1 Option A).

When a model is deployed, the semantic binder must resolve the model's
*semantic shape* — measures, dimensions, hidden-column flags, physical
column names, and hierarchy-level virtual dimensions — from the deployed
version's immutable ``snapshot_json``, NOT from the live editable tables.
This pins what BI tools see to the deployed contract: draft edits to a
deployed model do not leak to BI clients until the next Deploy.

What is always-live (resolved from live tables elsewhere, NOT here):
row security rules, personas, data-tag column security, aggregates and
pockets. See docs/architecture/architecture_b15-deploy-snapshot-pinning-design.md.

The deployed snapshot is loaded once per ``(model_id, deployed_version_id)``
and cached in-process. A Deploy moves the pointer, which changes the cache
key, so the next query naturally rehydrates — no explicit invalidation hook
is required. A short TTL bounds staleness from any out-of-band change.
"""
from __future__ import annotations

import time
import types
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import Dimension, Measure, ModelVersion

# Cache TTL: bounds staleness if a snapshot_json were ever mutated in place
# (it never is today — versions are immutable). 300s matches the KPI cache.
_CACHE_TTL_SECONDS = 300
_MAX_CACHE_ENTRIES = 256


@dataclass
class DeployedShape:
    """The pinned semantic shape resolved from a deployed snapshot."""

    measures: list[Any]
    dimensions: list[Any]
    hidden_column_ids: set[uuid.UUID]
    physical_columns_all: set[str]  # lowercase
    physical_columns_visible: set[str]  # lowercase, is_hidden=False
    hierarchy_rows: list[dict[str, Any]] = field(default_factory=list)


# key -> (expires_at, DeployedShape)
_CACHE: dict[tuple[str, str], tuple[float, DeployedShape]] = {}


def invalidate(model_id: object | None = None) -> None:
    """Drop cached shapes. With no argument, clears everything (test hook)."""
    if model_id is None:
        _CACHE.clear()
        return
    mid = str(model_id)
    for key in [k for k in _CACHE if k[0] == mid]:
        _CACHE.pop(key, None)


def _coerce(value: Any) -> Any:
    """Coerce a serialised snapshot scalar back to a native Python type.

    Mirrors the rehydrator: 36-char dashed strings -> UUID, ISO datetimes
    -> datetime. Everything else passes through untouched.
    """
    if isinstance(value, str):
        if len(value) == 36 and value.count("-") == 4:
            try:
                return uuid.UUID(value)
            except ValueError:
                pass
        if len(value) >= 19 and value[4] == "-" and value[7] == "-" and value[10] in ("T", " "):
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                pass
    return value


def _hydrate(model_cls: type, row: dict[str, Any], model_id: uuid.UUID) -> Any:
    """Build a transient (un-persisted) ORM instance from a snapshot row.

    Only keys that map to real ORM columns are passed, so a snapshot that
    carries an extra serialised key (e.g. a dropped column on an older
    snapshot) does not blow up construction. ``model_id`` is re-stamped so
    every object is anchored to the live model even though it is detached.
    """
    valid_cols = {c.name for c in model_cls.__table__.columns}
    kwargs: dict[str, Any] = {}
    for k, v in row.items():
        if k not in valid_cols:
            continue
        kwargs[k] = _coerce(v)
    kwargs["model_id"] = model_id
    return model_cls(**kwargs)


def _build_shape(model_id: uuid.UUID, snapshot: dict[str, Any]) -> DeployedShape:
    measures = [_hydrate(Measure, m, model_id) for m in snapshot.get("measures", []) or []]
    dimensions = [_hydrate(Dimension, d, model_id) for d in snapshot.get("dimensions", []) or []]

    # Model columns are not anchored to model_id directly (they hang off
    # model_table_id), so hydrate them without re-stamping model_id.
    hidden_ids: set[uuid.UUID] = set()
    all_phys: set[str] = set()
    visible_phys: set[str] = set()
    for c in snapshot.get("columns", []) or []:
        cid = _coerce(c.get("id"))
        name = (c.get("column_name") or "").lower()
        is_hidden = bool(c.get("is_hidden"))
        if name:
            all_phys.add(name)
            if not is_hidden:
                visible_phys.add(name)
        if is_hidden and isinstance(cid, uuid.UUID):
            hidden_ids.add(cid)

    return DeployedShape(
        measures=measures,
        dimensions=dimensions,
        hidden_column_ids=hidden_ids,
        physical_columns_all=all_phys,
        physical_columns_visible=visible_phys,
        hierarchy_rows=snapshot.get("hierarchies", []) or [],
    )


async def resolve_deployed_shape(
    model: Any, db: AsyncSession
) -> Optional[DeployedShape]:
    """Return the pinned shape for a deployed model, or None if not deployable.

    Returns None when the model has no deploy pointer (the binder's 409 gate
    handles that case) or when the deployed version row / its snapshot is
    missing (binder falls back to the live-load path so the model is not
    silently emptied).
    """
    deployed_version_id = getattr(model, "deployed_version_id", None)
    if deployed_version_id is None:
        return None

    key = (str(model.id), str(deployed_version_id))
    now = time.monotonic()
    cached = _CACHE.get(key)
    if cached is not None and cached[0] > now:
        return cached[1]

    version = await db.get(ModelVersion, deployed_version_id)
    if version is None or not isinstance(version.snapshot_json, dict):
        return None
    snapshot = version.snapshot_json
    if not snapshot.get("measures") and not snapshot.get("dimensions") and not snapshot.get("columns"):
        # Empty/placeholder snapshot — do not pin to nothing; fall back live.
        return None

    shape = _build_shape(model.id, snapshot)

    # Evict the oldest entry if the cache is full (simple bound).
    if len(_CACHE) >= _MAX_CACHE_ENTRIES:
        oldest_key = min(_CACHE, key=lambda k: _CACHE[k][0])
        _CACHE.pop(oldest_key, None)
    _CACHE[key] = (now + _CACHE_TTL_SECONDS, shape)
    return shape


@dataclass
class LiveMetadataBundle:
    """The binder's live-load metadata, cached per deployed model version.

    F-003-14: when a deployed model has no usable version snapshot (seed v1 /
    empty snapshot), the binder falls back to loading measures, dimensions,
    hierarchy-level dimensions, hidden-column ids, and (for ``SELECT *``)
    physical column names from the live tables — up to five sequential
    queries on the hot path of every query. These are immutable per deployed
    model version, so they are cached keyed by ``(model_id,
    deployed_version_id)`` exactly like ``resolve_deployed_shape``; the key
    changes on re-deploy, making the cache multi-replica safe.
    """

    measures: list[Any]
    dimensions: list[Any]
    hierarchy_levels: list[Any]
    hidden_column_ids: set[Any]
    physical_columns_visible: set[str]
    physical_columns_all: set[str]


# key -> (expires_at, LiveMetadataBundle)
_LIVE_CACHE: dict[tuple[str, str], tuple[float, LiveMetadataBundle]] = {}


def invalidate_live_metadata(model_id: object | None = None) -> None:
    """Drop cached live-metadata bundles. No arg clears everything (test hook)."""
    if model_id is None:
        _LIVE_CACHE.clear()
        return
    mid = str(model_id)
    for key in [k for k in _LIVE_CACHE if k[0] == mid]:
        _LIVE_CACHE.pop(key, None)


async def resolve_live_metadata_bundle(
    model: Any,
    db: AsyncSession,
    *,
    loader,
) -> Optional[LiveMetadataBundle]:
    """Return the cached live-metadata bundle for a deployed model, loading once.

    ``loader`` is an async callable ``() -> LiveMetadataBundle`` that performs
    the live DB loads. It is invoked only on a cache miss. The bundle is keyed
    by ``(model_id, deployed_version_id)``; a model with no deploy pointer is
    never cached (returns None so the caller loads live every time — but the
    binder rejects undeployed models at its gate before reaching here).

    On a miss the loaded ORM objects are expunged from ``db`` so the cached
    instances are detached-but-loaded and never expire against a closed
    session — the same lifetime the deployed-snapshot path already relies on.
    """
    deployed_version_id = getattr(model, "deployed_version_id", None)
    if deployed_version_id is None:
        return None

    key = (str(model.id), str(deployed_version_id))
    now = time.monotonic()
    cached = _LIVE_CACHE.get(key)
    if cached is not None and cached[0] > now:
        return cached[1]

    bundle = await loader()

    # Detach the loaded ORM objects so the cached copies remain readable after
    # the originating request's session closes (read-only downstream use).
    for obj in (*bundle.measures, *bundle.dimensions):
        try:
            db.expunge(obj)
        except Exception:
            pass

    if len(_LIVE_CACHE) >= _MAX_CACHE_ENTRIES:
        oldest_key = min(_LIVE_CACHE, key=lambda k: _LIVE_CACHE[k][0])
        _LIVE_CACHE.pop(oldest_key, None)
    _LIVE_CACHE[key] = (now + _CACHE_TTL_SECONDS, bundle)
    return bundle


def hierarchy_level_dimensions_from_snapshot(
    shape: DeployedShape,
) -> list[types.SimpleNamespace]:
    """Build hierarchy-level virtual dimensions from the pinned snapshot.

    Mirrors ``shared.semantic.hierarchy_resolver.load_hierarchy_level_dimensions``
    but reads the snapshot's nested ``hierarchies`` rows instead of querying
    HierarchyDefinition/HierarchyLevel live, so a draft change to a hierarchy
    level does not leak to deployed BI.
    """
    raw: list[types.SimpleNamespace] = []
    bare_count: dict[str, int] = {}
    for hierarchy in sorted(
        shape.hierarchy_rows, key=lambda h: str(h.get("name") or "")
    ):
        hname = (hierarchy.get("name") or "").strip()
        if not hname:
            continue
        dimension_kind = hierarchy.get("dimension_kind")
        levels = sorted(
            hierarchy.get("levels", []) or [],
            key=lambda lvl: int(lvl.get("ordinal", 0)),
        )
        for level in levels:
            level_name = (level.get("name") or "").strip()
            if not level_name:
                continue
            source = (level.get("key_attribute_source") or "").strip()
            if source == "physical_column":
                source_column_id = _coerce(level.get("key_attribute_id"))
                uda_id = None
            elif source == "user_defined_attribute":
                source_column_id = None
                uda_id = _coerce(level.get("key_attribute_id"))
            else:
                continue
            qualified = f"{hname}.{level_name}"
            raw.append(
                types.SimpleNamespace(
                    id=f"hlevel-{level.get('id')}",
                    name=qualified,
                    bare_name=level_name,
                    source_column_id=source_column_id,
                    user_defined_attribute_id=uda_id,
                    hierarchy_id=_coerce(hierarchy.get("id")),
                    hierarchy_name=hname,
                    hierarchy_level_id=_coerce(level.get("id")),
                    hierarchy_level_ordinal=level.get("ordinal"),
                    is_hierarchy_level=True,
                    dimension_kind=dimension_kind,
                    is_time_dim=(dimension_kind == "time"),
                )
            )
            bare_count[level_name] = bare_count.get(level_name, 0) + 1

    out: list[types.SimpleNamespace] = []
    seen_qualified: set[str] = set()
    for dim in raw:
        if dim.name in seen_qualified:
            continue
        seen_qualified.add(dim.name)
        out.append(dim)
        if bare_count.get(dim.bare_name, 0) == 1 and dim.bare_name not in seen_qualified:
            alias = types.SimpleNamespace(**vars(dim))
            alias.name = dim.bare_name
            out.append(alias)
            seen_qualified.add(dim.bare_name)
    return out


__all__ = [
    "DeployedShape",
    "resolve_deployed_shape",
    "hierarchy_level_dimensions_from_snapshot",
    "invalidate",
    "LiveMetadataBundle",
    "resolve_live_metadata_bundle",
    "invalidate_live_metadata",
]
