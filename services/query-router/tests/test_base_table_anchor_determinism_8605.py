"""Bug-8605 — the router's base-table choice must not depend on row order.

``resolve_base_table`` is the read-path twin of the CTAS builders' FROM anchor.
It decides which physical table a persona star projects against
(``source_sql.py`` ~line 501) and which table ``COUNT(*)`` counts
(``source_sql.py`` ~line 1029), and ``resolve_all_tables`` feeds the positional
``all_tables[0]`` that picks the physical name substituted for the model slug
(``source_sql.py`` ~line 879).

The live (undeployed) branch used to be two ``sa_select(ModelTable)...limit(1)``
reads with NO ``ORDER BY``. ``LIMIT 1`` without ``ORDER BY`` returns an
arbitrary row, so on a zero-fact model — legal, ``_assert_at_most_one_fact``
imposes no minimum — the same unedited model could project against a different
table between two identical requests. That is the same wrong-number class as
the CTAS anchor, on the serving side, which is why the shared-primitive rule
required it to move with ``sql_builder.py`` and ``creator.py`` rather than
being left as "the bug was filed against the builders".

The deployed branch and the live branch must also agree WITH EACH OTHER: a
model queried before and after a deploy, and a graph rehydrated from a snapshot
by a revert, all have to anchor on the same table. That is why canonical order
is ``id`` -- the only key the snapshot carries verbatim and
``rehydrate_into_live`` preserves. Both properties are asserted here.
"""
from __future__ import annotations

import types
import uuid
from datetime import datetime, timezone

import pytest

pytestmark = pytest.mark.unit

# FIXED ids: canonical order IS id order, so uuid4() would make the resolved
# base table a coin flip between runs.
MODEL_ID = uuid.UUID(int=0xDE5)
T_FIRST = uuid.UUID(int=0xA1)
T_SECOND = uuid.UUID(int=0xB2)


class _ScalarResult:
    def __init__(self, items):
        self._items = list(items)

    def scalars(self):
        return self

    def all(self):
        return list(self._items)

    def scalar_one_or_none(self):
        return self._items[0] if self._items else None


class _RecordingDb:
    """Returns the model's tables in a caller-chosen order and records the
    ``ORDER BY`` each statement carried."""

    def __init__(self, rows):
        self._rows = rows
        self.order_by_seen: list[list[str]] = []
        self.limits_seen: list = []

    async def execute(self, stmt):
        names = []
        for clause in getattr(stmt, "_order_by_clauses", ()) or ():
            element = getattr(clause, "element", clause)
            name = getattr(element, "name", None) or getattr(element, "key", None)
            if name:
                names.append(str(name))
        self.order_by_seen.append(names)
        self.limits_seen.append(getattr(stmt, "_limit", None))
        if not names:
            return _ScalarResult(self._rows)
        return _ScalarResult(
            sorted(
                self._rows,
                key=lambda r: tuple(str(getattr(r, n, "")) for n in names),
            )
        )


def _undeployed_model():
    return types.SimpleNamespace(id=MODEL_ID, deployed_version_id=None,
                                 deploy_epoch=0, slug="modelz")


def _live_rows(order):
    # created_at INVERSE to the id order, so a regression to creation order
    # would resolve dim_region and fail.
    old = types.SimpleNamespace(
        id=T_FIRST, model_id=MODEL_ID, table_type="dim_aggregate",
        physical_name="public.dim_customer",
        created_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
    )
    new = types.SimpleNamespace(
        id=T_SECOND, model_id=MODEL_ID, table_type="dim_detail",
        physical_name="public.dim_region",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    by_id = {T_FIRST: old, T_SECOND: new}
    return [by_id[t] for t in order]


async def test_live_base_table_does_not_depend_on_row_order():
    from src.rewrite.snapshot_graph_resolvers import resolve_base_table

    model = _undeployed_model()
    first = await resolve_base_table(model, _RecordingDb(_live_rows([T_FIRST, T_SECOND])))
    second = await resolve_base_table(model, _RecordingDb(_live_rows([T_SECOND, T_FIRST])))

    assert first.physical_name == second.physical_name == "public.dim_customer", (
        "the router's base table moved with nothing but the row order: "
        f"{first.physical_name!r} vs {second.physical_name!r}. This table is "
        "what a persona star projects against and what COUNT(*) counts."
    )


async def test_live_base_table_read_is_ordered_and_unlimited():
    """The read must be canonically ordered, and must not be a bare LIMIT 1.

    ``LIMIT 1`` with no ``ORDER BY`` is the exact shape that made this
    nondeterministic; asserting only the resolved value would let it come back
    as long as the fixture happened to return rows in the lucky order.
    """
    from src.rewrite.snapshot_graph_resolvers import resolve_base_table

    db = _RecordingDb(_live_rows([T_SECOND, T_FIRST]))
    await resolve_base_table(_undeployed_model(), db)
    assert db.order_by_seen == [["id"]], db.order_by_seen
    assert db.limits_seen == [None], (
        f"a LIMIT without a total ORDER BY returns an arbitrary row: "
        f"{db.limits_seen!r}"
    )


async def test_live_base_table_still_prefers_the_fact_table():
    """The fact preference must survive: COUNT(*) must count fact rows (Bug-119)."""
    from src.rewrite.snapshot_graph_resolvers import resolve_base_table

    dim = types.SimpleNamespace(
        id=T_FIRST, model_id=MODEL_ID, table_type="dim_detail",
        physical_name="public.dim_region",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    fact = types.SimpleNamespace(
        id=T_SECOND, model_id=MODEL_ID, table_type="fact",
        physical_name="public.fact_sales",
        created_at=datetime(2026, 9, 9, tzinfo=timezone.utc),
    )
    # The dim sorts FIRST by id, so an ordering-only rule would wrongly pick it.
    resolved = await resolve_base_table(
        _undeployed_model(), _RecordingDb([dim, fact])
    )
    assert resolved.physical_name == "public.fact_sales"


async def test_live_and_snapshot_hydrated_rows_resolve_the_same_base_table():
    """The live branch and the deployed branch must not disagree.

    Snapshot-hydrated rows carry no ``created_at`` (the serialiser drops it) but
    DO carry the same ``id``, and ``rehydrate_into_live`` preserves that id
    verbatim across a revert. Ordering on ``id`` is what makes the CTAS built
    from live rows, the source SQL built from the snapshot, and a rehydrated
    live graph all anchor on the same table. An earlier version of this fix
    ordered by ``(created_at, id)`` and had to leave hydrated rows in their
    caller order; a revert then re-stamped ``created_at`` on the live side and
    the two authorities silently diverged with no drift reported.
    """
    from shared.semantic.graph_order import pick_anchor_table

    live = _live_rows([T_SECOND, T_FIRST])
    hydrated = [
        types.SimpleNamespace(id=T_SECOND, table_type="dim_detail",
                              physical_name="public.dim_region",
                              created_at=None),
        types.SimpleNamespace(id=T_FIRST, table_type="dim_aggregate",
                              physical_name="public.dim_customer",
                              created_at=None),
    ]
    assert (
        pick_anchor_table(live).physical_name
        == pick_anchor_table(hydrated).physical_name
        == "public.dim_customer"
    )


async def test_empty_graph_yields_no_base_table():
    """Fail closed, as before: callers reject rather than emit a raw star."""
    from src.rewrite.snapshot_graph_resolvers import resolve_base_table

    assert await resolve_base_table(_undeployed_model(), _RecordingDb([])) is None


# ---------------------------------------------------------------------------
# Bug-8605 round-3 review: the DEPLOYED graph must not inherit the snapshot's
# stored list order.
# ---------------------------------------------------------------------------

_T_FACT = uuid.UUID(int=0x01)
_T_A = uuid.UUID(int=0x02)
_T_B = uuid.UUID(int=0x03)
_T_SH = uuid.UUID(int=0x04)
_C_F_A = uuid.UUID(int=0x101)
_C_A_ID = uuid.UUID(int=0x102)
_C_F_B = uuid.UUID(int=0x103)
_C_B_ID = uuid.UUID(int=0x104)
_C_A_SH = uuid.UUID(int=0x105)
_C_SH_ID = uuid.UUID(int=0x106)
_C_B_SH = uuid.UUID(int=0x107)
_C_SH_ID2 = uuid.UUID(int=0x108)


def _snap_tbl(tid, phys, alias, ttype="dim_detail"):
    return {"id": str(tid), "physical_name": phys, "alias": alias,
            "table_type": ttype, "display_name": alias}


def _snap_col(cid, tid, name):
    return {"id": str(cid), "model_table_id": str(tid), "column_name": name,
            "data_type": "uuid"}


def _snap_join(jid, lt, lc, rt, rc, jtype):
    return {"id": str(jid), "left_table_id": str(lt), "left_column_id": str(lc),
            "right_table_id": str(rt), "right_column_id": str(rc),
            "join_type": jtype}


_SNAP_TABLES = [
    _snap_tbl(_T_FACT, "public.fact_sales", "fact_sales", "fact"),
    _snap_tbl(_T_A, "public.dim_a", "dim_a"),
    _snap_tbl(_T_B, "public.dim_b", "dim_b"),
    _snap_tbl(_T_SH, "public.dim_shared", "dim_shared"),
]
_SNAP_COLUMNS = [
    _snap_col(_C_F_A, _T_FACT, "a_id"), _snap_col(_C_A_ID, _T_A, "id"),
    _snap_col(_C_F_B, _T_FACT, "b_id"), _snap_col(_C_B_ID, _T_B, "id"),
    _snap_col(_C_A_SH, _T_A, "sh_id"), _snap_col(_C_SH_ID, _T_SH, "id"),
    _snap_col(_C_B_SH, _T_B, "sh_id"), _snap_col(_C_SH_ID2, _T_SH, "id2"),
]
# Two equally short paths from the fact table to dim_shared. Canonical id order
# is J1,J2,J3,J4; a snapshot written before the serialiser emitted canonical
# order stores them in (created_at, id) order, which for random UUIDs is a
# different order in the general case.
_SJ1 = _snap_join(uuid.UUID(int=0xAA1), _T_FACT, _C_F_A, _T_A, _C_A_ID, "inner")
_SJ2 = _snap_join(uuid.UUID(int=0xAA2), _T_FACT, _C_F_B, _T_B, _C_B_ID, "left")
_SJ3 = _snap_join(uuid.UUID(int=0xAA3), _T_A, _C_A_SH, _T_SH, _C_SH_ID, "inner")
_SJ4 = _snap_join(uuid.UUID(int=0xAA4), _T_B, _C_B_SH, _T_SH, _C_SH_ID2, "left")


def _from_clause_from_snapshot(join_rows):
    from src.rewrite.joins import _build_joined_from_clause
    from src.rewrite.table_resolution import _build_graph_from_snapshot

    graph = _build_graph_from_snapshot(
        {"tables": list(_SNAP_TABLES), "columns": list(_SNAP_COLUMNS),
         "joins": list(join_rows), "user_defined_attributes": []},
        MODEL_ID,
    )
    assert graph is not None
    tables_by_id, joins, columns_by_id, _udas = graph
    return _build_joined_from_clause(
        base_table_id=_T_FACT,
        required_table_ids={_T_FACT, _T_SH},
        joins=joins,
        tables_by_id=tables_by_id,
        columns_by_id=columns_by_id,
        alias_by_table_id={_T_FACT: "base"},
        connector="postgresql",
    )


def test_deployed_snapshot_join_list_order_cannot_change_the_from_clause():
    """The hydrated deployed graph must be canonically ordered at the CONSUMER.

    Bug-8605 R3 fixed this on the PRODUCER side only -- the serialiser now
    writes joins in canonical id order -- and concluded there was "no second
    order for a consumer to disagree with". Every snapshot written before that
    commit, and every version snapshot rehydrated from an imported bundle,
    stores joins in a different order, and ``_build_graph_from_snapshot``
    never re-sorted. ``shared/semantic/graph_order`` names "a snapshot-hydrated
    graph" as one of the three shapes its Python layer exists to normalise, and
    this was the one place it was not applied: the undeployed branch of
    ``_load_model_graph`` calls ``canonical_join_order`` and
    ``resolve_all_tables`` calls ``canonical_table_order``, but the deployed
    joins list was passed through untouched into ``_build_joined_from_clause``.

    Consequence: the CTAS (``sql_builder.build_from_clause``, canonical order)
    and the served source SQL joined through DIFFERENT intermediate tables with
    different join keywords, so an aggregate-routed query and a source-routed
    query answered the same question differently with no staleness signal.
    """
    canonical = _from_clause_from_snapshot([_SJ1, _SJ2, _SJ3, _SJ4])
    reordered = _from_clause_from_snapshot([_SJ2, _SJ4, _SJ1, _SJ3])
    assert canonical == reordered, (
        "the router built a DIFFERENT FROM clause from the same rows in a "
        f"different stored order.\n  canonical-id order: {canonical}\n"
        f"  creation order:     {reordered}\n"
        "Apply canonical_join_order (and canonical_table_order) inside "
        "_build_graph_from_snapshot so the deployed graph is a pure function "
        "of the rows, not of the order they were serialised in."
    )
