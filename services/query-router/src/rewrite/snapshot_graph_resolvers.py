"""Deployed-snapshot-sourced object resolution for the source-SQL builder.

F-013-01 residual (Bug-7979 follow-up, Lane A2): the source-SQL builder had five
sites that independently loaded ``Dimension``/``Measure``/``ModelTable`` rows from
the LIVE ORM even for a DEPLOYED model. A draft edit to any of those objects
could then change production SQL before the next deploy — the exact fail-open the
deployed-snapshot authority forbids.

These helpers resolve the same objects from the model's DEPLOYED snapshot when
the model is deployed, and fall back to live ORM ONLY for a genuinely undeployed
model (no ``deployed_version_id``). For a deployed model whose snapshot cannot be
resolved, they FAIL CLOSED (return nothing) — never a silent live-table read.

All returned objects are transient (un-persisted) ORM instances hydrated from the
immutable snapshot, carrying the same field names / ids the live rows had, so
every downstream consumer is unchanged.
"""
from __future__ import annotations

from typing import Any

from src.ir.logical_query import DeployedSnapshotUnavailableError


def _is_deployed(model: Any) -> bool:
    return getattr(model, "deployed_version_id", None) is not None


async def _deployed_shape_or_none(
    model: Any, db: Any, deployed_shape: Any = None,
):
    """Return the model's DeployedShape, or None on any resolution failure.

    Only meaningful for a deployed model; callers gate on ``_is_deployed`` first.
    """
    if deployed_shape is not None:
        return deployed_shape
    try:
        from src.semantic.snapshot_resolver import resolve_deployed_shape
        return await resolve_deployed_shape(model, db)
    except Exception:
        return None


async def resolve_dimensions_by_name(
    model: Any, db: Any, names: set[str], *, case_insensitive: bool = False,
    deployed_shape: Any = None,
) -> dict[str, Any]:
    """Resolve dimensions by name from the deployed snapshot (deployed model) or
    live ORM (undeployed model). Returns ``{requested_name: Dimension}``.

    For a deployed model with an unresolvable snapshot, returns ``{}`` (fail
    closed). ``case_insensitive`` maps a matched snapshot/live dimension back to
    the caller's requested-case name.
    """
    if not names:
        return {}
    if _is_deployed(model):
        shape = await _deployed_shape_or_none(model, db, deployed_shape)
        if shape is None:
            return {}
        out: dict[str, Any] = {}
        if case_insensitive:
            lower_lookup = {n.lower(): n for n in names}
            for d in shape.dimensions:
                dn = getattr(d, "name", None)
                if dn is None:
                    continue
                user_name = lower_lookup.get(dn.lower())
                if user_name is not None:
                    out[user_name] = d
        else:
            wanted = set(names)
            for d in shape.dimensions:
                dn = getattr(d, "name", None)
                if dn in wanted:
                    out[dn] = d
        return out
    # Undeployed model: live ORM is the authority.
    from sqlalchemy import func as sa_func, select as sa_select
    from shared.db.models import Dimension
    out2: dict[str, Any] = {}
    if case_insensitive:
        lower_lookup = {n.lower(): n for n in names}
        result = await db.execute(
            sa_select(Dimension).where(
                Dimension.model_id == model.id,
                sa_func.lower(Dimension.name).in_(list(lower_lookup)),
            )
        )
        for d in result.scalars().all():
            user_name = lower_lookup.get(d.name.lower())
            if user_name:
                out2[user_name] = d
    else:
        result = await db.execute(
            sa_select(Dimension).where(
                Dimension.model_id == model.id,
                Dimension.name.in_(list(names)),
            )
        )
        for d in result.scalars().all():
            out2[d.name] = d
    return out2


async def resolve_measures_by_name(
    model: Any, db: Any, names: set[str], *, deployed_shape: Any = None,
) -> list[Any]:
    """Resolve measures by name from the deployed snapshot / live ORM.

    Returns a list of Measure objects (order not significant). Fail closed for a
    deployed model with an unresolvable snapshot.
    """
    if not names:
        return []
    if _is_deployed(model):
        shape = await _deployed_shape_or_none(model, db, deployed_shape)
        if shape is None:
            return []
        wanted = set(names)
        return [m for m in shape.measures if getattr(m, "name", None) in wanted]
    from sqlalchemy import select as sa_select
    from shared.db.models import Measure
    result = await db.execute(
        sa_select(Measure).where(
            Measure.model_id == model.id,
            Measure.name.in_(list(names)),
        )
    )
    return list(result.scalars().all())


async def resolve_measures_by_id(
    model: Any, db: Any, ids: set[str], *, deployed_shape: Any = None,
) -> dict[str, Any]:
    """Resolve measures by (stringified) id from the deployed snapshot / live ORM.

    Returns ``{str(id): Measure}``. Fail closed for a deployed model with an
    unresolvable snapshot. Used for variant base-measure resolution
    (``variant_of_measure_id``).
    """
    if not ids:
        return {}
    wanted = {str(i) for i in ids}
    if _is_deployed(model):
        shape = await _deployed_shape_or_none(model, db, deployed_shape)
        if shape is None:
            return {}
        return {
            str(m.id): m for m in shape.measures
            if str(getattr(m, "id", "")) in wanted
        }
    from sqlalchemy import select as sa_select
    from shared.db.models import Measure
    result = await db.execute(
        sa_select(Measure).where(Measure.id.in_(list(ids)))
    )
    return {str(m.id): m for m in result.scalars().all()}


async def resolve_time_dimensions(
    model: Any, db: Any, *, deployed_shape: Any = None,
) -> list[Any]:
    """Return all is_time_dim dimensions from the deployed snapshot / live ORM.

    Fail closed (empty) for a deployed model with an unresolvable snapshot.
    """
    if _is_deployed(model):
        shape = await _deployed_shape_or_none(model, db, deployed_shape)
        if shape is None:
            return []
        return [d for d in shape.dimensions if getattr(d, "is_time_dim", False)]
    from sqlalchemy import select as sa_select
    from shared.db.models import Dimension
    result = await db.execute(
        sa_select(Dimension).where(
            Dimension.model_id == model.id,
            Dimension.is_time_dim.is_(True),
        )
    )
    return list(result.scalars().all())


async def resolve_base_table(
    model: Any, db: Any, *, deployed_shape: Any = None,
) -> Any | None:
    """Return the base (fact-preferred) ModelTable from the deployed snapshot /
    live ORM. Prefers a fact table, else the first table in canonical ``id``
    order. Fail closed (None) for a deployed model with an unresolvable
    snapshot.

    Returns a transient ModelTable ORM instance for a deployed model (hydrated
    from the snapshot graph) or the live row for an undeployed model.

    Bug-8605: the live branch used to be two ``.limit(1)`` reads with no
    ``ORDER BY``, which is the same unordered-positional-anchor defect the
    CTAS builders carried. A zero-fact model — legal — then substituted a
    different physical base table into the source SQL run to run, with no edit
    to the model. Both branches now share ``pick_anchor_table`` so the live
    preview path and the deployed path cannot disagree on the rule either.
    """
    from shared.semantic.graph_order import pick_anchor_table

    tables = await resolve_all_tables(model, db, deployed_shape=deployed_shape)
    return pick_anchor_table(tables)


async def resolve_all_tables(
    model: Any, db: Any, *, deployed_shape: Any = None,
) -> list[Any]:
    """Return every ModelTable from the deployed snapshot / live ORM.

    Fail closed (empty) for a deployed model with an unresolvable snapshot.
    Deployed-model rows are transient ORM instances hydrated from the snapshot.
    """
    from shared.semantic.graph_order import (
        canonical_table_order,
        select_model_tables,
    )

    if _is_deployed(model):
        from src.rewrite.table_resolution import _load_model_graph
        try:
            tables_by_id, _joins, _cols, _udas = await _load_model_graph(
                _BoundLike(model, deployed_shape), db, set(),
            )
        except DeployedSnapshotUnavailableError:
            # Bug-7981: a blocked deployment must stay LOUD. Swallowing it into
            # an empty result turns a typed 503 into a silently table-less
            # rewrite, which is the fail-open this module exists to prevent.
            raise
        except Exception:
            return []
        # Canonical order is ``id``, which snapshot-hydrated rows carry
        # verbatim, so the deployed graph and the live graph sort identically.
        return canonical_table_order(tables_by_id.values())
    # Bug-8605: canonically ordered so positional consumers (``all_tables[0]``
    # in source_sql's table substitution) cannot move with row order.
    result = await db.execute(select_model_tables(model.id))
    return canonical_table_order(result.scalars().all())


async def resolve_columns_by_id(
    model: Any, db: Any, col_ids: set, *, deployed_shape: Any = None,
) -> dict[Any, Any]:
    """Resolve ModelColumn rows by id from the deployed snapshot graph / live ORM.

    Fail closed (empty) for a deployed model with an unresolvable snapshot.
    """
    if not col_ids:
        return {}
    if _is_deployed(model):
        from src.rewrite.table_resolution import _load_model_graph
        try:
            _tables, _joins, columns_by_id, _udas = await _load_model_graph(
                _BoundLike(model, deployed_shape), db, set(),
            )
        except DeployedSnapshotUnavailableError:
            raise  # Bug-7981: keep a blocked deployment loud (see above).
        except Exception:
            return {}
        return {cid: columns_by_id[cid] for cid in col_ids if cid in columns_by_id}
    from sqlalchemy import select as sa_select
    from shared.db.models import ModelColumn
    result = await db.execute(
        sa_select(ModelColumn).where(ModelColumn.id.in_(list(col_ids)))
    )
    return {c.id: c for c in result.scalars().all()}


async def resolve_udas_by_id(
    model: Any, db: Any, uda_ids: set, *, deployed_shape: Any = None,
) -> dict[Any, Any]:
    """Resolve UserDefinedAttribute rows by id from the deployed snapshot graph /
    live ORM. Fail closed (empty) for a deployed model with an unresolvable
    snapshot.
    """
    if not uda_ids:
        return {}
    if _is_deployed(model):
        from src.rewrite.table_resolution import _load_model_graph
        try:
            _tables, _joins, _cols, uda_by_id = await _load_model_graph(
                _BoundLike(model, deployed_shape), db, set(uda_ids),
            )
        except DeployedSnapshotUnavailableError:
            raise  # Bug-7981: keep a blocked deployment loud (see above).
        except Exception:
            return {}
        return {uid: uda_by_id[uid] for uid in uda_ids if uid in uda_by_id}
    from sqlalchemy import select as sa_select
    from shared.db.models import UserDefinedAttribute
    result = await db.execute(
        sa_select(UserDefinedAttribute).where(
            UserDefinedAttribute.id.in_(list(uda_ids))
        )
    )
    return {a.id: a for a in result.scalars().all()}


class _BoundLike:
    """Minimal BoundQuery-shaped shim so ``_load_model_graph`` (which reads
    ``bound_query.model``) can be reused from these helpers without a real
    BoundQuery. ``_load_model_graph`` only touches ``.model``.
    """

    __slots__ = ("model", "deployed_shape")

    def __init__(self, model: Any, deployed_shape: Any = None) -> None:
        self.model = model
        self.deployed_shape = deployed_shape
