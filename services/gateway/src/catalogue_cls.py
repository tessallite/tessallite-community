"""Gateway catalogue transitive CLS closure (F-008-04).

The JDBC/XMLA catalogue advertises a persona's field list. A tag-restricted
column must be hidden not only when an object binds it DIRECTLY, but also when
the object reaches it TRANSITIVELY — a calculated measure over a restricted
column, a UDA-backed dimension, a variant of a restricted base measure, or a
calculated dimension whose expression references a restricted column.

Runtime enforcement (query-router) already computes that transitive closure via
``shared.security.restricted_column_closure.object_touches_restricted``. Before
this module the catalogue used DIRECT ``source_column_id`` membership only, so a
calculated salary measure was advertised in JDBC metadata even though execution
rejects it — an existence oracle and a catalogue/runtime mismatch.

This module reuses the SAME shared closure algorithm, driven by the deployed (or
live) snapshot the catalogue already holds. Personas / tag restrictions are
always-live (not deploy-pinned), so the restricted-column SET is live while the
object list is the deployed snapshot — the closure is computed over
(deployed objects x live restricted set), exactly what runtime enforces.

No SQL, no DB session: everything comes from the snapshot dicts + the persona's
``restricted_column_ids``. Fail-closed is inherited from the shared algorithm
(an object whose closure cannot be enumerated is treated as restricted).
"""
from __future__ import annotations

from typing import Any, Iterable

from shared.security.restricted_column_closure import (
    ClosureContext,
    normalise_id_set,
    object_touches_restricted,
)


class _AttrView:
    """Expose a plain dict's keys as attributes for the duck-typed closure.

    The shared closure reads ``obj.source_column_id`` / ``obj.measure_type`` /
    ``obj.expression`` etc. via ``getattr``; snapshot rows are dicts, so this
    thin view maps attribute access to dict lookup (missing key -> ``None``,
    matching the closure's ``getattr(obj, name, None)`` contract).
    """

    __slots__ = ("_d",)

    def __init__(self, d: dict[str, Any]):
        self._d = d

    def __getattr__(self, name: str) -> Any:
        return self._d.get(name)


def build_closure_context(
    *,
    measures: Iterable[dict[str, Any]],
    snapshot: dict[str, Any],
    restricted_column_ids: set[str],
) -> ClosureContext:
    """Build a :class:`ClosureContext` from catalogue snapshot dicts.

    * ``measures_by_id`` / ``measures_by_name`` — every catalogue measure, so
      variant-base and calc-measure references resolve without a DB round-trip.
    * ``restricted_uda_ids`` — UDA ids whose ``uda_column_refs`` include a
      restricted column (a UDA-backed object is then restricted).
    * ``restricted_physical_names`` / ``known_physical_names`` /
      ``table_identifiers`` — physical-name sets for the calc-dimension gate,
      derived from the snapshot's ``columns`` and ``tables``.
    """
    ctx = ClosureContext()
    for m in measures:
        mv = _AttrView(m)
        mid = m.get("id")
        if mid is not None:
            ctx.measures_by_id[str(mid)] = mv
        name = m.get("name")
        if name:
            ctx.measures_by_name[str(name)] = mv

    columns = snapshot.get("columns") or []
    col_name_by_id: dict[str, str] = {}
    known_names: set[str] = set()
    for c in columns:
        cid = c.get("id")
        cname = (c.get("column_name") or "").lower()
        if cname:
            known_names.add(cname)
        if cid is not None and cname:
            col_name_by_id[str(cid)] = cname
    ctx.known_physical_names = known_names
    ctx.restricted_physical_names = {
        col_name_by_id[cid] for cid in restricted_column_ids if cid in col_name_by_id
    }

    # UDA ids that reference a restricted column.
    restricted_uda_ids: set[str] = set()
    for ref in snapshot.get("uda_column_refs") or []:
        col_id = ref.get("column_id")
        attr_id = ref.get("attribute_id")
        if col_id is not None and attr_id is not None and str(col_id) in restricted_column_ids:
            restricted_uda_ids.add(str(attr_id))
    ctx.restricted_uda_ids = restricted_uda_ids

    # Table physical names + aliases (whole-row reference guard).
    table_idents: set[str] = set()
    for t in snapshot.get("tables") or []:
        for key in ("physical_name", "table_name", "alias", "name"):
            val = t.get(key)
            if val:
                table_idents.add(str(val).lower())
    ctx.table_identifiers = table_idents
    return ctx


def object_hidden_by_cls(
    obj: dict[str, Any],
    restricted_column_ids: set[str],
    ctx: ClosureContext,
) -> bool:
    """True when a catalogue measure/dimension dict transitively touches a
    restricted column and must be hidden from the persona's field list.

    Delegates to the shared ``object_touches_restricted`` so the catalogue and
    the query-router serving gate hide/serve on ONE algorithm (no drift).
    """
    if not restricted_column_ids:
        return False
    return object_touches_restricted(
        _AttrView(obj), normalise_id_set(restricted_column_ids), ctx,
    )
