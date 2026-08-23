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

The deployed snapshot is loaded once per
``(model_id, deployed_version_id, deploy_epoch)`` and cached in-process.
A Deploy moves the pointer and bumps the epoch, which changes the cache
key, so the next query naturally rehydrates — no explicit invalidation hook
is required. The epoch ensures that undeploy (pointer to NULL then back)
and revert-to-same-version (pointer unchanged, content changed) also
invalidate deterministically across replicas. A short TTL bounds staleness
from any out-of-band change.
"""
from __future__ import annotations

import contextvars
import enum
import time
import types
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import Dimension, Measure, ModelVersion


class SnapshotAuthority(enum.Enum):
    """Authoritative classification of a model's serving semantic source.

    F-013-05 / F-003-03 / F-001-02 (Bug-7979): the deployed snapshot is the sole
    runtime semantic authority. A serving consumer must distinguish three cases
    and NEVER conflate the last two (the historic fail-open bug):

    * ``UNDEPLOYED`` — the model has no ``deployed_version_id``. Live/draft
      metadata is the legitimate authority (authoring / undeployed execution).
      Binder/gateway normally reject serving these anyway (409 not-deployed).
    * ``DEPLOYED`` — the model has a deploy pointer AND a usable snapshot. The
      pinned ``DeployedShape`` is the authority.
    * ``DEPLOYED_SNAPSHOT_INVALID`` — the model has a deploy pointer but its
      snapshot is missing / non-dict / empty-shape. This MUST fail closed with a
      typed unavailable/corrupt-deployment error; live/draft metadata must never
      be served here (that leaks undeployed fields to BI clients).
    """

    UNDEPLOYED = "undeployed"
    DEPLOYED = "deployed"
    DEPLOYED_SNAPSHOT_INVALID = "deployed_snapshot_invalid"

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
    # Lowercase physical column name -> stable ModelColumn id (str), from the
    # pinned snapshot. Feeds the binder's derived-expression leaf binding (spec
    # §7.1): a derived-grain leaf resolves its physical column NAME to this stable
    # id so the §7.3 cond. 2 lineage-collision guard can distinguish same-named
    # columns in different relations. Additive — every other consumer of this
    # shape reads only the name sets / hidden ids above and is unaffected.
    physical_column_ids: dict[str, str] = field(default_factory=dict)
    # --- Stage-4 relabel serving snapshot surface (spec §2.3 / §3.2) ----------
    # All pinned from the same immutable snapshot, so the resolver never queries
    # source data or reads editable draft declarations.
    #
    # Deployed attribute-relationship declaration dicts (key/detail column ids,
    # cardinality, null_policy, enabled, declaration_hash, dimension_id).
    attribute_relationships: list[dict[str, Any]] = field(default_factory=list)
    # Stable-id indices (id stringified to match the manifest id vocabulary).
    dimensions_by_id: dict[str, Any] = field(default_factory=dict)
    columns_by_id: dict[str, dict[str, Any]] = field(default_factory=dict)
    tables_by_id: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Raw physical-graph families retained with the same cached snapshot
    # authority as measures/dimensions. ``table_resolution._load_model_graph``
    # consumes these so an in-flight request cannot bind against one deployed
    # version, then independently re-fetch a version row that a concurrent
    # backward revert has deleted and fall through to post-revert live rows
    # (Bug-7981).
    join_rows: list[dict[str, Any]] = field(default_factory=list)
    user_defined_attribute_rows: list[dict[str, Any]] = field(default_factory=list)
    # Qualified column index: (table_id, casefold physical column name) -> column
    # id. The ONLY safe way to resolve a qualified leaf — an unqualified name may
    # be ambiguous across tables, but (table_id, name) is unique.
    qualified_column_ids: dict[tuple[str, str], str] = field(default_factory=dict)
    # Table-name index: casefold alias / full physical_name / terminal identifier
    # -> table_id. Ambiguous names (>1 table_id) are POISONED (omitted) rather
    # than assigned an arbitrary winner. ``display_name`` is NEVER table identity.
    table_name_ids: dict[str, str] = field(default_factory=dict)
    # --- Calendar serving surface (F-013-01 / F-016-02 / F-101-03) ------------
    # Raw ``calendar_tables`` snapshot rows and a stable-id index. Period-aware
    # measures pin ``resolved_calendar_id``, but the calendar ROW (type,
    # fiscal_year_start_month, physical column map, physical table name) used to
    # be read LIVE at serve time, so a draft fiscal-start / column-remap edit
    # moved deployed YTD/QTD numbers before the next Deploy. These are pinned
    # from the same immutable snapshot so ``calendar_support`` resolves the
    # calendar identity from here, not from ``db.get(CalendarTable, ...)``.
    calendar_tables: list[dict[str, Any]] = field(default_factory=list)
    calendar_tables_by_id: dict[str, dict[str, Any]] = field(default_factory=dict)
    # --- Parameter serving surface (F-029-01 / Bug-9397) ----------------------
    # The deployed ``model_parameters`` rows and the set of declared ``@name``s
    # they define. BOTH the parameter VALUES that get bound into the SQL and the
    # query-time ``@``-namespace collision check must read this ONE pinned copy.
    # They used to disagree: the values came from the snapshot while the
    # collision check ran ``select(ModelParameter)`` against the LIVE ORM, so a
    # draft parameter that collided with a deployed named list 400'd production
    # queries that should have expanded the deployed list. Pinning both here
    # makes the disagreement unrepresentable inside one request.
    model_parameters: list[dict[str, Any]] = field(default_factory=list)
    model_parameter_names: set[str] = field(default_factory=set)


# key -> (expires_at, DeployedShape)
# Key is (model_id, deployed_version_id, deploy_epoch).
_CACHE: dict[tuple[str, str, int], tuple[float, DeployedShape]] = {}


# ---------------------------------------------------------------------------
# Request-scoped deployment pin (Bug-7981)
# ---------------------------------------------------------------------------
#
# STRUCTURAL CONTRACT — read before adding any new consumer of a DeployedShape.
#
# A ``DeployedShape`` is an AMBIENT property of a request ("the deployment this
# request was bound against"), not a per-call argument. Many independent serving
# consumers resolve it: the binder, the aggregate matcher (3 sites), the pocket
# matcher (2 sites), the router, ``rewrite/table_resolution``,
# ``rewrite/snapshot_graph_resolvers``, ``resolve_calc_dependency_measures``
# (reached from both the source and raw rewrite paths) and the headless
# catalogue. Every one of them calls ``resolve_deployed_shape(model, db)``.
#
# Those independent calls used to be able to DISAGREE inside a single request:
#
#   * ``DELETE /cache/models/{id}`` (issued by model-service immediately BEFORE
#     a deploy/revert commit) clears ``_CACHE`` mid-request;
#   * ``_CACHE_TTL_SECONDS`` can lapse mid-request;
#   * a backward revert DELETES the newer ``ModelVersion`` rows at commit.
#
# So a second call for the SAME ``(model_id, version_id, epoch)`` could return
# ``None`` where the first returned a shape. A ``None`` there is exactly what
# let consumers fall through to live/draft ORM rows and mix an old semantic
# shape with a post-revert physical graph — the Bug-7981 wrong-numbers path.
#
# Threading the binder's shape through each consumer (rounds 1-3 of Bug-7981)
# cannot close this class: it is O(number of call sites), it is invisible when a
# new consumer forgets it, and each review round simply found the next site.
# Instead the RESOLVER is made idempotent within a request. The first resolution
# of a deployment identity is pinned in a ``contextvars``-scoped registry, and
# every later call in the same request returns that identical object. There is
# no bypass: a consumer cannot obtain a shape except through this function.
#
# Lifetime and isolation: ``contextvars`` are copied per asyncio Task and
# Starlette runs each HTTP request in its own Task, so the registry is
# per-request by construction and dies with the request's context. Even a reused
# context is safe: a pin is keyed by the FULL deployment identity and
# ``ModelVersion.snapshot_json`` is immutable, so a reused pin can only return
# the same immutable snapshot — a new deployment always has a new key and can
# never hit an old pin. ``_MAX_REQUEST_PINS`` bounds the registry regardless
# (Bug-8503 retention discipline). ``reset_request_pins()`` is the explicit
# boundary hook for tests and for any caller that reuses one context across
# logical requests.
_MAX_REQUEST_PINS = 32

_REQUEST_PINS: contextvars.ContextVar[
    "dict[tuple[str, str, int], DeployedShape] | None"
] = contextvars.ContextVar("tessallite_deployed_shape_pins", default=None)


def reset_request_pins() -> None:
    """Drop this context's deployment pins (request boundary / test hook)."""
    _REQUEST_PINS.set(None)


def _pinned_shape(
    key: tuple[str, str, int],
) -> Optional[DeployedShape]:
    """Return the shape already resolved for ``key`` in this request, if any."""
    pins = _REQUEST_PINS.get()
    if not pins:
        return None
    return pins.get(key)


def _pin_shape(key: tuple[str, str, int], shape: DeployedShape) -> None:
    """Pin ``shape`` as this request's authority for ``key`` (first writer wins).

    First-writer-wins matters: re-pinning would let a later resolution of the
    same identity replace the object earlier consumers already used, which is
    the divergence this registry exists to prevent.
    """
    pins = _REQUEST_PINS.get()
    if pins is None:
        pins = {}
        _REQUEST_PINS.set(pins)
    if key in pins:
        return
    if len(pins) >= _MAX_REQUEST_PINS:
        # A request touches a handful of models at most; exceeding the bound
        # means a context is being reused across logical requests. Drop the
        # oldest pin rather than grow without limit.
        pins.pop(next(iter(pins)), None)
    pins[key] = shape


def invalidate(model_id: object | None = None) -> None:
    """Drop cached shapes. With no argument, clears everything (test hook).

    This clears the PROCESS cache only. It deliberately does NOT clear the
    request-scoped pins: model-service issues this eviction immediately before
    a deploy/revert commit, and an already-bound in-flight request must keep
    finishing against the deployment it bound to (Bug-7981). Pins are keyed by
    the full deployment identity, so they can never satisfy a request that
    observes the new deployment.
    """
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
    # name -> {ids} across ALL tables. ModelColumn names are unique only per table
    # (UniqueConstraint(model_table_id, column_name)), so the SAME lowercased name
    # can name DIFFERENT physical columns in different tables. A derived-expression
    # leaf is recorded UNQUALIFIED (canonicaliser keeps ``Column.name`` only), so an
    # ambiguous bare name cannot be disambiguated to one table's id — assigning an
    # arbitrary "winner" would let a query over relation A bind to relation B's id
    # and fake the §7.3 cond. 2 lineage match (wrong-number serve). Collect the id
    # SET per name and poison any name that resolves to >1 id (final map below).
    _ids_by_name: dict[str, set[str]] = {}
    # Stage-4 relabel snapshot surface (spec §2.3 / §3.2).
    columns_by_id: dict[str, dict[str, Any]] = {}
    _qids_by_key: dict[tuple[str, str], set[str]] = {}
    for c in snapshot.get("columns", []) or []:
        cid = _coerce(c.get("id"))
        name = (c.get("column_name") or "").lower()
        is_hidden = bool(c.get("is_hidden"))
        raw_id = c.get("id")
        if name:
            all_phys.add(name)
            if not is_hidden:
                visible_phys.add(name)
            # Stable snapshot column id for the derived-expression leaf binding
            # (§7.1). The raw snapshot value is preserved as ``str`` so it matches
            # the manifest's ``input_column_ids`` id vocabulary byte-for-byte.
            # Recorded for ALL columns (hidden included) so the vocabulary matches
            # the manifest's full-column lineage; hidden ACCESS is enforced by CLS,
            # not by this id map.
            if raw_id:
                _ids_by_name.setdefault(name, set()).add(str(raw_id))
        if raw_id:
            columns_by_id[str(raw_id)] = c
            # Qualified index: (table_id, casefold name). The ORM
            # UniqueConstraint(model_table_id, column_name) is CASE-SENSITIVE, so a
            # table CAN hold ``"Col"`` and ``"col"`` as distinct columns that fold to
            # one key here. POISON such a collision (omit) rather than last-writer-
            # wins, matching the other index maps (spec §6 "poison ambiguous names").
            tbl_id = c.get("model_table_id")
            if tbl_id and name:
                _qk = (str(tbl_id), name)
                _qids_by_key.setdefault(_qk, set()).add(str(raw_id))
        if is_hidden and isinstance(cid, uuid.UUID):
            hidden_ids.add(cid)
    # Only names that resolve to EXACTLY ONE id are trustworthy leaf ids; an
    # ambiguous name is omitted -> its leaf binds column_id="" -> the lineage gate
    # fails closed (source), deterministically regardless of iteration order.
    phys_ids: dict[str, str] = {
        n: next(iter(ids)) for n, ids in _ids_by_name.items() if len(ids) == 1
    }
    # Qualified index: keep only (table_id, casefold name) keys that resolve to
    # EXACTLY ONE column id — a case-collision within one table is poisoned.
    qualified_column_ids: dict[tuple[str, str], str] = {
        k: next(iter(ids)) for k, ids in _qids_by_key.items() if len(ids) == 1
    }

    # Table-name index (spec §2.3): casefold alias / full physical_name /
    # terminal identifier -> table_id. Ambiguous names are POISONED. display_name
    # is never table identity.
    tables_by_id: dict[str, dict[str, Any]] = {}
    _table_name_candidates: dict[str, set[str]] = {}
    for t in snapshot.get("tables", []) or []:
        raw_id = t.get("id")
        if not raw_id:
            continue
        tid = str(raw_id)
        tables_by_id[tid] = t
        for token in (t.get("alias"), t.get("physical_name")):
            if not token:
                continue
            tok = str(token).lower()
            _table_name_candidates.setdefault(tok, set()).add(tid)
            terminal = tok.split(".")[-1]
            if terminal and terminal != tok:
                _table_name_candidates.setdefault(terminal, set()).add(tid)
    table_name_ids: dict[str, str] = {
        n: next(iter(ids)) for n, ids in _table_name_candidates.items() if len(ids) == 1
    }

    dimensions_by_id: dict[str, Any] = {
        str(getattr(d, "id", "")): d for d in dimensions if getattr(d, "id", None)
    }

    # Calendar family (F-013-01 / F-016-02): raw rows + stable-id index. Keyed by
    # str(id) so calendar_support can look a CalendarTable up by its pinned
    # ``resolved_calendar_id`` without any live DB read.
    calendar_tables = list(snapshot.get("calendar_tables", []) or [])
    calendar_tables_by_id: dict[str, dict[str, Any]] = {}
    for cal in calendar_tables:
        raw_id = cal.get("id")
        if raw_id:
            calendar_tables_by_id[str(raw_id)] = cal

    # Parameter family (F-029-01 / Bug-9397). Non-dict rows are dropped rather
    # than allowed to reach the resolver as an untyped value.
    model_parameters = [
        p for p in (snapshot.get("model_parameters", []) or [])
        if isinstance(p, dict)
    ]
    model_parameter_names = {
        str(p["name"]) for p in model_parameters if p.get("name")
    }

    return DeployedShape(
        measures=measures,
        dimensions=dimensions,
        hidden_column_ids=hidden_ids,
        physical_columns_all=all_phys,
        physical_columns_visible=visible_phys,
        hierarchy_rows=snapshot.get("hierarchies", []) or [],
        physical_column_ids=phys_ids,
        attribute_relationships=snapshot.get("attribute_relationships", []) or [],
        dimensions_by_id=dimensions_by_id,
        columns_by_id=columns_by_id,
        tables_by_id=tables_by_id,
        join_rows=snapshot.get("joins", []) or [],
        user_defined_attribute_rows=(
            snapshot.get("user_defined_attributes", []) or []
        ),
        qualified_column_ids=qualified_column_ids,
        table_name_ids=table_name_ids,
        calendar_tables=calendar_tables,
        calendar_tables_by_id=calendar_tables_by_id,
        model_parameters=model_parameters,
        model_parameter_names=model_parameter_names,
    )


def _snapshot_has_shape(snapshot: dict[str, Any]) -> bool:
    """Return True when a snapshot carries at least one semantic-shape family.

    An empty/placeholder snapshot (legacy seed v1) carries none of these. The
    ONE authoritative usability test — shared by ``resolve_deployed_shape`` and
    ``resolve_snapshot_authority`` so the fail-closed decision can never drift
    from what the shape builder would accept.

    Bug-8306 (narrowed F-013-01 re-open): a snapshot that carries ``columns``
    but NO ``tables`` is malformed — a physical column cannot exist without its
    table, and the serialiser always writes both together (columns are selected
    from the same tables). Such a snapshot would pass the semantic-shape gate on
    ``columns`` alone, letting the binder serve a DEPLOYED model whose source SQL
    then reads the LIVE physical graph (``_load_model_graph`` falls back to live
    ORM tables when the snapshot yields no graph). That leaks unpublished draft
    table/join edits into production SQL. Fail closed: when ``columns`` is present
    the snapshot MUST also carry ``tables`` to count as a usable shape, so a
    ``columns``-without-``tables`` deployed snapshot resolves to
    DEPLOYED_SNAPSHOT_INVALID (503) instead of reaching source SQL.
    """
    if snapshot.get("columns") and not snapshot.get("tables"):
        # Malformed/legacy snapshot: physical columns with no physical-table
        # graph. A columns-bearing snapshot without tables is never usable — the
        # source-SQL graph loader has no snapshot tables to build from and would
        # fall back to the LIVE physical graph. Fail closed unconditionally so a
        # DEPLOYED model here resolves to DEPLOYED_SNAPSHOT_INVALID (503) and the
        # query never reaches source SQL. (A genuine seed-v1 empty snapshot
        # carries neither columns nor tables and is handled by the branch below.)
        return False
    return bool(
        snapshot.get("measures")
        or snapshot.get("dimensions")
        or snapshot.get("columns")
        or snapshot.get("hierarchies")
    )


async def resolve_deployed_shape(
    model: Any, db: AsyncSession
) -> Optional[DeployedShape]:
    """Return the pinned shape for a deployed model, or None if not deployable.

    Returns None when the model has no deploy pointer (the binder's 409 gate
    handles that case) OR when the deployed version row / its snapshot is
    missing/empty.

    IMPORTANT (F-013-05 / Bug-7979): a ``None`` here does NOT authorise serving
    live/draft metadata for a DEPLOYED model. Serving consumers (binder, params,
    gateway catalogue) must call ``resolve_snapshot_authority`` to distinguish
    UNDEPLOYED (live OK) from DEPLOYED_SNAPSHOT_INVALID (fail closed). This
    function is kept ``Optional`` because non-serving / already-fail-closed-on-None
    consumers (aggregate/pocket matchers, calc-dependency loader) correctly treat
    ``None`` as "no pinned shape -> route to source / leave unresolved".
    """
    deployed_version_id = getattr(model, "deployed_version_id", None)
    if deployed_version_id is None:
        return None

    epoch = getattr(model, "deploy_epoch", 0) or 0
    key = (str(model.id), str(deployed_version_id), epoch)

    # Bug-7981: a deployment identity already resolved in THIS request is
    # pinned and immune to process-cache eviction, TTL lapse, and a concurrent
    # backward revert deleting the version row. See the contract above.
    pinned = _pinned_shape(key)
    if pinned is not None:
        return pinned

    now = time.monotonic()
    cached = _CACHE.get(key)
    if cached is not None and cached[0] > now:
        _pin_shape(key, cached[1])
        return cached[1]

    version = await db.get(ModelVersion, deployed_version_id)
    if version is None or not isinstance(version.snapshot_json, dict):
        return None
    snapshot = version.snapshot_json
    if not _snapshot_has_shape(snapshot):
        # Empty/placeholder snapshot — no pinned shape to return.
        return None

    shape = _build_shape(model.id, snapshot)

    # Evict the oldest entry if the cache is full (simple bound).
    if len(_CACHE) >= _MAX_CACHE_ENTRIES:
        oldest_key = min(_CACHE, key=lambda k: _CACHE[k][0])
        _CACHE.pop(oldest_key, None)
    _CACHE[key] = (now + _CACHE_TTL_SECONDS, shape)
    _pin_shape(key, shape)
    return shape


async def resolve_snapshot_authority(
    model: Any, db: AsyncSession
) -> tuple[SnapshotAuthority, Optional[DeployedShape]]:
    """Classify a model's serving semantic authority, fail-closed (Bug-7979).

    Returns ``(SnapshotAuthority, shape)`` where ``shape`` is the pinned
    ``DeployedShape`` only for the ``DEPLOYED`` case (None otherwise). Serving
    consumers use the enum to decide:

    * ``UNDEPLOYED``            -> live/draft metadata is the authority.
    * ``DEPLOYED``             -> use ``shape`` (never live).
    * ``DEPLOYED_SNAPSHOT_INVALID`` -> raise a typed 503; NEVER read live.

    The distinction the historic bug missed: a DEPLOYED model whose snapshot is
    missing/empty/corrupt is NOT "undeployed" — serving its live draft would leak
    unpublished edits. Reuses ``resolve_deployed_shape`` (and its cache) so a
    ``DEPLOYED`` result shares one code path with the rest of the binder.
    """
    deployed_version_id = getattr(model, "deployed_version_id", None)
    if deployed_version_id is None:
        return SnapshotAuthority.UNDEPLOYED, None

    shape = await resolve_deployed_shape(model, db)
    if shape is not None:
        return SnapshotAuthority.DEPLOYED, shape
    # Deploy pointer present but no usable pinned shape -> corrupt/unavailable
    # deployment. Fail closed; do NOT let the caller fall back to live metadata.
    return SnapshotAuthority.DEPLOYED_SNAPSHOT_INVALID, None


async def resolve_serving_authority(
    model_id: Any, db: AsyncSession
) -> tuple[SnapshotAuthority, Optional[DeployedShape]]:
    """Classify a model's serving semantic authority *by id*, fail-closed.

    The by-id sibling of ``resolve_snapshot_authority`` for the serving
    consumers that hold a ``model_id`` rather than a loaded ``Model`` (the
    calendar rewrite consumers, the pre-parse parameter binder, the deployed
    named-object catalogue). It loads the ``Model`` and delegates, so every
    caller shares the SAME request-pinned ``DeployedShape`` the binder resolved
    — one authority per request, no second snapshot rebuild, no divergence.

    A missing ``model_id``, or a model row that does not exist, is
    ``UNDEPLOYED``: there is no deploy pointer to pin against. A DB failure
    PROPAGATES rather than being coerced to ``UNDEPLOYED`` — "we could not
    read the model" is not "the model is undeployed", and conflating the two
    is precisely the fail-open this classification exists to prevent.
    """
    if model_id is None:
        return SnapshotAuthority.UNDEPLOYED, None
    from shared.db.models import Model

    model = await db.get(Model, model_id)
    if model is None:
        return SnapshotAuthority.UNDEPLOYED, None
    return await resolve_snapshot_authority(model, db)


async def resolve_calendar_serving_shape(
    model_id: Any, db: AsyncSession
) -> tuple[SnapshotAuthority, Optional[DeployedShape]]:
    """Classify calendar serving authority for a model *by id* (F-013-01).

    The calendar rewrite consumers (``calendar_support._resolve_calendar_binding``
    / ``_resolve_hierarchy_calendar_rules``) only hold ``db`` and a ``model_id``
    (or period-aware measures that carry ``model_id``). They must pin the calendar
    ROW — type, ``fiscal_year_start_month``, physical column map, physical table
    name — and the hierarchy calendar rules from the deployed snapshot, not from
    live ORM rows.

    Loads the ``Model`` and reuses ``resolve_snapshot_authority`` so this shares
    the SAME request-pinned ``DeployedShape`` the binder already resolved (no
    second snapshot rebuild, no divergence):

    * ``UNDEPLOYED``               -> live/draft calendar rows are the authority
      (undeployed authoring / editor preview).
    * ``DEPLOYED``                -> use the pinned shape's ``calendar_tables`` /
      ``hierarchy_rows``; the caller fails closed if the pinned id is absent.
    * ``DEPLOYED_SNAPSHOT_INVALID`` -> fail closed; the caller must NOT read live.

    A missing ``model_id`` or a model row that does not exist is treated as
    ``UNDEPLOYED`` (no deploy pointer to pin against). Thin alias of
    ``resolve_serving_authority`` — kept as the calendar consumers' named entry
    point (and as the marker the calendar serving-surface enumeration guard
    looks for in ``calendar_support.py``).
    """
    return await resolve_serving_authority(model_id, db)


async def resolve_calc_dependency_measures(
    model: Any,
    db: AsyncSession,
    ref_names: "set[str] | list[str]",
) -> dict[str, Any]:
    """Resolve calc-measure DEPENDENCY base measures by name, deploy-pinned.

    A calculated measure's expression may reference base measures that are NOT
    themselves selected (so the binder never resolved them). The rewriter loads
    those referenced measures to drive join planning and physical-stat rendering.

    Bug-7803 / Bug-7784: the binder pins a SELECTED calculated measure to the
    immutable DEPLOYED SNAPSHOT, but the dependency load historically read LIVE
    draft ``Measure`` rows by ``(model_id, name)``. That split the semantic
    authority — after deploy, editing a REFERENCED base measure in the draft
    (its ``source_column_id`` / UDA / variant / ``default_agg``) changed the
    calc's emitted SQL before redeployment (wrong numbers under an active draft
    edit). This resolves the SAME deployed snapshot the binder uses, matched by
    name, so the calc measure and its dependencies share one authority.

    FAIL-CLOSED authority (mirrors the Bug-7784 aggregate_matcher precedent):

    * Model WITH a deploy pointer: the deployed snapshot is authority, full stop.
      Resolve dependencies from ``resolve_deployed_shape(model, db).measures``.
      If the snapshot cannot be resolved (transient DB error, cache eviction,
      empty/placeholder snapshot), DO NOT fall back to the live draft ORM —
      that recreates the bug. Leave the missing names unresolved; the caller
      then renders them fail-closed (typed NULL / route to source) under the
      deployed contract rather than silently using draft state.
    * Model WITHOUT a deploy pointer (undeployed): the live tables ARE the
      authority — load live ``Measure`` rows, mirroring the binder's own
      undeployed-fallback discipline.

    Returns ``{name: measure_object}`` for every name that resolved. Names that
    could not be resolved under the pinned authority are simply absent (the
    caller treats an absent dependency as fail-closed).
    """
    names = {n for n in ref_names if n}
    out: dict[str, Any] = {}
    if not names:
        return out

    has_deploy_pointer = getattr(model, "deployed_version_id", None) is not None

    if has_deploy_pointer:
        try:
            # Bug-7981: this resolves the SAME request-pinned shape the binder
            # used (see the request-scoped deployment pin contract above), so a
            # calc measure's dependencies cannot silently disappear part-way
            # through a request because a concurrent backward revert deleted the
            # version row or the eviction endpoint cleared the process cache.
            shape = await resolve_deployed_shape(model, db)
        except Exception:
            shape = None
        if shape is not None:
            snapshot_by_name = {
                getattr(m, "name", None): m for m in shape.measures
            }
            for name in names:
                m = snapshot_by_name.get(name)
                if m is not None:
                    out[name] = m
        # else: deployed model whose snapshot could not be resolved -> fail
        # closed (leave names unresolved; NEVER read live draft here).
        return out

    # Undeployed model: live tables are the authority.
    try:
        result = await db.execute(
            select(Measure).where(
                Measure.model_id == model.id,
                Measure.name.in_(list(names)),
            )
        )
        for m in result.scalars().all():
            out[m.name] = m
    except Exception:
        pass  # DB failure -> no resolution (caller fails closed)
    return out


@dataclass
class LiveMetadataBundle:
    """The binder's live-load metadata, cached per deployed model version.

    F-003-14: when a deployed model has no usable version snapshot (seed v1 /
    empty snapshot), the binder falls back to loading measures, dimensions,
    hierarchy-level dimensions, hidden-column ids, and (for ``SELECT *``)
    physical column names from the live tables — up to five sequential
    queries on the hot path of every query. These are immutable per deployed
    model version, so they are cached keyed by ``(model_id,
    deployed_version_id, deploy_epoch)`` exactly like
    ``resolve_deployed_shape``; the key changes on re-deploy or revert,
    making the cache multi-replica safe.
    """

    measures: list[Any]
    dimensions: list[Any]
    hierarchy_levels: list[Any]
    hidden_column_ids: set[Any]
    physical_columns_visible: set[str]
    physical_columns_all: set[str]
    # Lowercase physical column name -> stable ModelColumn id (str). Live-load
    # analogue of DeployedShape.physical_column_ids; feeds the binder's derived
    # leaf binding on the seed-v1 / empty-snapshot fallback path. Additive.
    physical_column_ids: dict[str, str] = field(default_factory=dict)


# key -> (expires_at, LiveMetadataBundle)
# Key is (model_id, deployed_version_id, deploy_epoch).
_LIVE_CACHE: dict[tuple[str, str, int], tuple[float, LiveMetadataBundle]] = {}


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
    by ``(model_id, deployed_version_id, deploy_epoch)``; a model with no
    deploy pointer is never cached (returns None so the caller loads live
    every time — but the binder rejects undeployed models at its gate before
    reaching here).

    On a miss the loaded ORM objects are expunged from ``db`` so the cached
    instances are detached-but-loaded and never expire against a closed
    session — the same lifetime the deployed-snapshot path already relies on.
    """
    deployed_version_id = getattr(model, "deployed_version_id", None)
    if deployed_version_id is None:
        return None

    epoch = getattr(model, "deploy_epoch", 0) or 0
    key = (str(model.id), str(deployed_version_id), epoch)
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
    "reset_request_pins",
    "LiveMetadataBundle",
    "resolve_live_metadata_bundle",
    "invalidate_live_metadata",
    "SnapshotAuthority",
    "resolve_snapshot_authority",
    "resolve_serving_authority",
    "resolve_calendar_serving_shape",
]
