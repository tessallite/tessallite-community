"""Bug-8250 re-gate — the live-vs-deployed build-closure comparison.

The Codex cross-family gate FAILED the first Bug-8250 fix with two CRITICAL
findings of the same class: an aggregate CTAS is built from the MUTABLE live
model graph, and the only guard against that was a measure-only comparison. A
draft-side change to a **grain-dimension binding** or to the **join graph** was
therefore invisible, and the artifact was then stamped as compatible with the
currently-deployed pointer — wrong numbers served as the deployed version.

These tests pin the comparison shape itself: whole-row equality over both sides,
which entities are bidirectional, and what a stale snapshot schema must NOT do.
They are deliberately pure (no DB) so they state the rule rather than a wiring.
"""
from __future__ import annotations

import pytest

from shared.definition_closure import (
    ClosureSpec,
    DefinitionClosure,
    closure_digest,
    closure_from_snapshot,
    compare_closures,
)

_T1 = "t-1"
_T2 = "t-2"
_C1 = "c-1"
_J1 = "j-1"


def _snapshot(**overrides):
    """A minimal, internally consistent deployed snapshot."""
    snap = {
        "measures": [
            {
                "id": "m-1",
                "name": "revenue",
                "source_column_id": _C1,
                "expression": None,
                "default_agg": "sum",
                "user_defined_attribute_id": None,
            }
        ],
        "dimensions": [
            {
                "id": "d-1",
                "name": "country",
                "source_column_id": _C1,
                "is_time_dim": False,
                "calc_expression": None,
            }
        ],
        "tables": [
            {"id": _T1, "physical_name": "fact_sales", "table_type": "fact",
             "source_id": "s-1", "calendar_table_id": None},
        ],
        "joins": [],
        "columns": [
            {"id": _C1, "model_table_id": _T1, "column_name": "amount",
             "data_type": "numeric", "is_hidden": False},
        ],
        "user_defined_attributes": [],
        "uda_column_refs": [],
        "hierarchies": [],
    }
    snap.update(overrides)
    return snap


def _spec():
    return ClosureSpec.of(["revenue"], ["country"])


def _live_from(snap):
    """The live side, built from the same shape (so it starts identical)."""
    import copy

    return closure_from_snapshot(copy.deepcopy(snap), _spec())


def test_identical_sides_report_no_drift():
    snap = _snapshot()
    assert compare_closures(_live_from(snap), closure_from_snapshot(snap, _spec())) == []


# ---------------------------------------------------------------------------
# The two CRITICAL findings: grain-dimension bindings and the join graph
# ---------------------------------------------------------------------------

def test_grain_dimension_rebound_to_another_column_is_drift():
    """CRITICAL finding 2: the predecessor guard compared measures ONLY.

    Re-pointing a grain dimension at a different source column changes the
    bucketing of every row the CTAS writes, with no measure edit at all.
    """
    snap = _snapshot()
    live = _live_from(snap)
    live.dimensions[0]["source_column_id"] = "c-other"
    reasons = compare_closures(live, closure_from_snapshot(snap, _spec()))
    assert any("dimension" in r and "source_column_id" in r for r in reasons), reasons


def test_grain_dimension_deleted_live_is_drift():
    snap = _snapshot()
    live = _live_from(snap)
    live.dimensions.clear()
    reasons = compare_closures(live, closure_from_snapshot(snap, _spec()))
    assert any("missing live" in r for r in reasons), reasons


def test_new_join_edge_is_drift():
    """CRITICAL finding 2: a new edge changes ``build_from_clause`` path-finding."""
    snap = _snapshot()
    live = _live_from(snap)
    live.joins.append(
        {"id": _J1, "left_table_id": _T1, "right_table_id": _T2,
         "join_type": "left", "left_column_id": _C1, "right_column_id": "c-2"}
    )
    reasons = compare_closures(live, closure_from_snapshot(snap, _spec()))
    assert any("join" in r and "not in the deployed snapshot" in r for r in reasons), reasons


@pytest.mark.parametrize(
    "relationship_state",
    [
        pytest.param({"enabled": False, "cardinality": "BIJECTION"}, id="disabled"),
        pytest.param({"enabled": True, "cardinality": "MANY_TO_ONE"}, id="non-bijection"),
    ],
)
def test_live_only_inert_attribute_relationship_does_not_refuse_refresh(
    relationship_state,
):
    """Only relationships the passenger planner can consume belong in Tier A."""
    snap = _snapshot(attribute_relationships=[])
    deployed = closure_from_snapshot(snap, _spec())
    live = _live_from(snap)
    live.attribute_relationships.append(
        {
            "id": "rel-live-inert",
            "dimension_id": "d-1",
            "key_column_id": "country-key",
            "detail_column_id": "city-inert",
            **relationship_state,
        }
    )

    assert compare_closures(live, deployed) == []
    assert closure_digest(live) == closure_digest(deployed)


def test_bug_8608_live_added_enabled_bijection_attribute_relationship_is_drift():
    """A genuine draft-only passenger declaration still refuses the refresh."""
    snap = _snapshot(attribute_relationships=[
        {
            "id": "rel-deployed",
            "dimension_id": "d-1",
            "key_column_id": "country-key",
            "detail_column_id": "city-deployed",
            "cardinality": "BIJECTION",
            "enabled": True,
        }
    ])
    live = _live_from(snap)
    live.attribute_relationships.insert(
        0,
        {
            "id": "rel-live-earlier",
            "dimension_id": "d-1",
            "key_column_id": "country-key",
            "detail_column_id": "city-other-table",
            "cardinality": "BIJECTION",
            "enabled": True,
        },
    )

    reasons = compare_closures(live, closure_from_snapshot(snap, _spec()))

    assert reasons == [
        "attribute relationship 'rel-live-earlier': present live but not in the "
        "deployed snapshot"
    ]


def test_join_type_change_is_drift():
    """inner -> left changes which fact rows survive, i.e. every total."""
    snap = _snapshot(joins=[
        {"id": _J1, "left_table_id": _T1, "right_table_id": _T2,
         "join_type": "inner", "left_column_id": _C1, "right_column_id": "c-2"}
    ])
    live = _live_from(snap)
    live.joins[0]["join_type"] = "left"
    reasons = compare_closures(live, closure_from_snapshot(snap, _spec()))
    assert any("join_type changed" in r for r in reasons), reasons


def test_removed_join_is_drift():
    snap = _snapshot(joins=[
        {"id": _J1, "left_table_id": _T1, "right_table_id": _T2,
         "join_type": "inner", "left_column_id": _C1, "right_column_id": "c-2"}
    ])
    live = _live_from(snap)
    live.joins.clear()
    reasons = compare_closures(live, closure_from_snapshot(snap, _spec()))
    assert any("join" in r and "missing live" in r for r in reasons), reasons


def test_new_fact_table_is_drift():
    """``build_from_clause`` anchors on the FIRST fact table; a new one can move it."""
    snap = _snapshot()
    live = _live_from(snap)
    live.tables.append(
        {"id": _T2, "physical_name": "fact_other", "table_type": "fact",
         "source_id": "s-1", "calendar_table_id": None}
    )
    reasons = compare_closures(live, closure_from_snapshot(snap, _spec()))
    assert any("table" in r and "not in the deployed snapshot" in r for r in reasons), reasons


def test_table_repointed_to_another_physical_name_is_drift():
    snap = _snapshot()
    live = _live_from(snap)
    live.tables[0]["physical_name"] = "fact_sales_v2"
    reasons = compare_closures(live, closure_from_snapshot(snap, _spec()))
    assert any("physical_name changed" in r for r in reasons), reasons


def test_measure_aggregation_change_is_drift():
    snap = _snapshot()
    live = _live_from(snap)
    live.measures[0]["default_agg"] = "avg"
    reasons = compare_closures(live, closure_from_snapshot(snap, _spec()))
    assert any("default_agg changed" in r for r in reasons), reasons


def test_column_renamed_live_is_drift():
    snap = _snapshot()
    live = _live_from(snap)
    live.columns[0]["column_name"] = "amount_net"
    reasons = compare_closures(live, closure_from_snapshot(snap, _spec()))
    assert any("column_name changed" in r for r in reasons), reasons


def test_duplicate_grain_dimension_name_live_is_drift():
    """An ambiguous name means the layout resolver's choice is not determined."""
    snap = _snapshot()
    live = _live_from(snap)
    live.dimensions.append(dict(live.dimensions[0], id="d-2", source_column_id="c-9"))
    reasons = compare_closures(live, closure_from_snapshot(snap, _spec()))
    assert any("duplicated in the live model" in r for r in reasons), reasons


# ---------------------------------------------------------------------------
# Deliberate non-drift: the rules that keep this from refusing everything
# ---------------------------------------------------------------------------

def test_added_column_is_not_drift():
    """A source re-scan ADDS columns with no modelling act; an unreferenced new
    column cannot change a CTAS that names its columns."""
    snap = _snapshot()
    live = _live_from(snap)
    live.columns.append(
        {"id": "c-new", "model_table_id": _T1, "column_name": "note",
         "data_type": "text", "is_hidden": False}
    )
    assert compare_closures(live, closure_from_snapshot(snap, _spec())) == []


def test_unrelated_measure_edit_is_not_drift():
    """Scoping measures to the aggregate's closure is what keeps an unrelated
    draft edit from refusing every refresh on the model."""
    snap = _snapshot()
    snap["measures"].append(
        {"id": "m-2", "name": "cost", "source_column_id": _C1,
         "expression": None, "default_agg": "sum", "user_defined_attribute_id": None}
    )
    live = _live_from(snap)
    # ``cost`` is not in the spec, so neither side carries it.
    assert live.measures == closure_from_snapshot(snap, _spec()).measures
    assert compare_closures(live, closure_from_snapshot(snap, _spec())) == []


def test_display_only_field_change_is_not_drift():
    snap = _snapshot()
    snap["measures"][0]["display_name"] = "Revenue"
    snap["measures"][0]["description"] = "old"
    deployed = closure_from_snapshot(snap, _spec())
    import copy

    live_snap = copy.deepcopy(snap)
    live_snap["measures"][0]["display_name"] = "Total Revenue"
    live_snap["measures"][0]["description"] = "new"
    live = closure_from_snapshot(live_snap, _spec())
    assert compare_closures(live, deployed) == []


def test_stats_telemetry_change_is_not_drift():
    """The stats collector writes these asynchronously; refusing on them would
    stale every aggregate on a cadence nobody asked for."""
    import copy

    snap = _snapshot()
    snap["columns"][0]["cardinality_estimate"] = 100
    snap["tables"][0]["row_count_estimate"] = 1000
    live_snap = copy.deepcopy(snap)
    live_snap["columns"][0]["cardinality_estimate"] = 250
    live_snap["tables"][0]["row_count_estimate"] = 5000
    assert compare_closures(
        closure_from_snapshot(live_snap, _spec()),
        closure_from_snapshot(snap, _spec()),
    ) == []


def test_field_added_to_orm_after_the_snapshot_is_not_drift():
    """A model deployed BEFORE a schema addition must keep refreshing.

    The router rehydrates that same snapshot, so it never sees the new field
    either. Comparing only the fields the snapshot carries is what stops every
    pre-existing tenant's refreshes from being refused forever after a migration.
    """
    snap = _snapshot()
    live = _live_from(snap)
    live.measures[0]["a_column_added_later"] = "whatever"
    assert compare_closures(live, closure_from_snapshot(snap, _spec())) == []


def test_field_dropped_live_but_present_in_snapshot_is_drift():
    """The inverse is NOT tolerated: the snapshot is the authority."""
    snap = _snapshot()
    live = _live_from(snap)
    del live.measures[0]["default_agg"]
    reasons = compare_closures(live, closure_from_snapshot(snap, _spec()))
    assert any("absent live" in r for r in reasons), reasons


# ---------------------------------------------------------------------------
# Digest — the mid-build TOCTOU detector
# ---------------------------------------------------------------------------

def test_digest_is_stable_across_row_order():
    snap = _snapshot(joins=[
        {"id": "j-a", "left_table_id": _T1, "right_table_id": _T2,
         "join_type": "inner", "left_column_id": _C1, "right_column_id": "c-2"},
        {"id": "j-b", "left_table_id": _T1, "right_table_id": "t-3",
         "join_type": "left", "left_column_id": _C1, "right_column_id": "c-3"},
    ])
    a = closure_from_snapshot(snap, _spec())
    b = closure_from_snapshot(snap, _spec())
    b.joins.reverse()
    assert closure_digest(a) == closure_digest(b)


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda c: c.measures[0].__setitem__("default_agg", "avg"), id="measure"),
        pytest.param(lambda c: c.dimensions[0].__setitem__("source_column_id", "x"), id="dimension"),
        pytest.param(lambda c: c.tables[0].__setitem__("physical_name", "x"), id="table"),
        pytest.param(lambda c: c.columns[0].__setitem__("column_name", "x"), id="column"),
        pytest.param(
            lambda c: c.joins.append({"id": "j-z", "join_type": "left"}), id="join"
        ),
    ],
)
def test_digest_changes_when_any_build_input_moves(mutate):
    """Every group the comparison covers must also move the digest.

    A group present in ``compare_closures`` but absent from ``closure_digest``
    would be checked up front and then invisible to the mid-build re-check —
    exactly the partial-coverage shape this whole fix exists to remove.
    """
    snap = _snapshot()
    before = closure_from_snapshot(snap, _spec())
    after = closure_from_snapshot(snap, _spec())
    mutate(after)
    assert closure_digest(before) != closure_digest(after)


def test_empty_snapshot_groups_do_not_crash():
    assert compare_closures(
        DefinitionClosure(), closure_from_snapshot({}, _spec())
    ) == []


def test_the_closure_fact_test_agrees_with_the_canonical_anchor_rule():
    """The drift guard and the anchor rule must apply the SAME fact test.

    Bug-8605 R2 made ``pick_anchor_table`` compare ``table_type == "fact"``
    case-SENSITIVELY while ``_fact_anchor_additions`` still lowercases. A model
    whose table_type was written ``"Fact"`` (the API accepts it: table_type is
    an unvalidated free string and both the one-fact assertion and the partial
    unique index test the literal 'fact') is then a fact table to the guard and
    NOT a fact table to the anchor rule, so the guard skips a live-only
    addition that provably DOES take the anchor -- Bug-8600's fail-open,
    re-opened. The guard must report drift exactly when the anchor moves.
    """
    import uuid as _uuid

    from shared.db.models import ModelTable
    from shared.definition_closure import _fact_anchor_additions
    from shared.semantic.graph_order import pick_anchor_table

    t_fact = _uuid.UUID(int=0xF000)
    t_dim = _uuid.UUID(int=0xD000)
    t_add = _uuid.UUID(int=0x0001)  # lowest id -> takes a zero-fact anchor

    def _row(tid, name, ttype):
        return {"id": str(tid), "physical_name": name, "table_type": ttype}

    def _orm(tid, name, ttype):
        return ModelTable(id=tid, physical_name=name, table_type=ttype)

    for fact_casing in ("fact", "Fact", "FACT"):
        deployed = [
            _row(t_fact, "fact_sales", fact_casing),
            _row(t_dim, "dim_region", "dim_detail"),
        ]
        live = deployed + [_row(t_add, "staging_customer", "dim_detail")]
        deployed_orm = [
            _orm(t_fact, "fact_sales", fact_casing),
            _orm(t_dim, "dim_region", "dim_detail"),
        ]
        live_orm = deployed_orm + [_orm(t_add, "staging_customer", "dim_detail")]

        anchor_moved = (
            pick_anchor_table(deployed_orm).id != pick_anchor_table(live_orm).id
        )
        drift_reported = bool(_fact_anchor_additions(live, deployed))
        assert drift_reported == anchor_moved, (
            f"table_type={fact_casing!r}: pick_anchor_table says the anchor "
            f"moved={anchor_moved} but the closure reported "
            f"drift={drift_reported}. The two use different fact tests, so "
            "the refresh is permitted onto a different FROM base and the "
            "artifact is still stamped version-compatible."
        )
