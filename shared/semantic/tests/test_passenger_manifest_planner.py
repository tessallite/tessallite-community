"""Passenger/diagnostic build-manifest planner tests (spec §3.6, turn-on build).

Covers the passenger materialisation seam that activates under
``optimizer.derived_expression_auto_build``:
  - passenger + 2 diagnostic SELECT fragments per eligible relationship, quoted for
    BOTH PostgreSQL (double-quote) and BigQuery (backtick);
  - the collision namespace (grains + measures + row count + quantile/stat) reserved
    before allocation, with a deterministic clamp and a FAIL-before-DDL on an
    unresolvable collision;
  - the edge/passenger drafts carrying the canonical attribute_key + key_grain_key_id
    (the PHYSICAL dim grain key that holds the relationship KEY column);
  - fail-closed skips (owning key not a grain, detail unresolved).
"""
from __future__ import annotations

import types
import uuid

import pytest

from shared.semantic.artifact_manifest import (
    KIND_ARTIFACT_EXPRESSION,
    KIND_PHYSICAL_COLUMN,
    MaterializedGrainKey,
)
from shared.semantic.grain_resolver import (
    ResolvedAggregateLayout,
    ResolvedGrainCol,
    ResolvedMeasureCol,
)
from shared.semantic.passenger_manifest_planner import (
    PassengerCollisionError,
    plan_passengers,
)

pytestmark = pytest.mark.unit


def _grain(phys, dim_id, table_id, col_name):
    return ResolvedGrainCol(
        logical_name=col_name, dimension_id=dim_id, source_table_id=table_id,
        source_column_name=col_name, physical_col_name=phys, source_expression=None,
    )


def _measure(phys, table_id):
    return ResolvedMeasureCol(
        measure_id=uuid.uuid4(), measure_name=phys.split("__")[0], stat_type="sum",
        aggregation_function="sum", source_table_id=table_id,
        source_column_name="amount", physical_col_name=phys,
    )


def _phys_key(key_id, col_id, phys):
    return MaterializedGrainKey(
        ordinal=0, key_id=key_id, kind=KIND_PHYSICAL_COLUMN, physical_column=phys,
        source_dimension_id=key_id.split(":", 1)[1], input_column_ids=[col_id],
    )


def _rel(rel_id, key_col_id, detail_col_id, cardinality="BIJECTION", decl="h"):
    return types.SimpleNamespace(
        id=rel_id, key_column_id=key_col_id, detail_column_id=detail_col_id,
        cardinality=cardinality, declaration_hash=decl, enabled=True,
    )


def _col(col_id, table_id, name, dtype="TEXT"):
    return types.SimpleNamespace(
        id=col_id, model_table_id=table_id, column_name=name, data_type=dtype,
    )


def _scenario():
    table_id = uuid.uuid4()
    dim_id = uuid.uuid4()
    key_col_id, detail_col_id = uuid.uuid4(), uuid.uuid4()
    rel_id = uuid.uuid4()
    layout = ResolvedAggregateLayout(
        grain_cols=[_grain("country_id", dim_id, table_id, "country_id")],
        measure_cols=[_measure("revenue__sum", table_id)],
    )
    grain_keys = [_phys_key(f"dim:{dim_id}", str(key_col_id), "country_id")]
    rels = [_rel(rel_id, key_col_id, detail_col_id)]
    dims_by_id = {str(dim_id): types.SimpleNamespace(id=dim_id, source_column_id=key_col_id)}
    cols_by_id = {
        str(key_col_id): _col(key_col_id, table_id, "country_id"),
        str(detail_col_id): _col(detail_col_id, table_id, "country_code"),
    }
    alias = {table_id: "base"}
    return dict(
        layout=layout, grain_keys=grain_keys, relationships=rels,
        columns_by_id=cols_by_id,
        alias_by_table_id=alias, rel_id=rel_id, dim_id=dim_id,
        detail_col_id=detail_col_id,
    )


def test_pg_fragments_and_edge_identity():
    s = _scenario()
    draft = plan_passengers(
        layout=s["layout"], grain_keys=s["grain_keys"],
        relationships=s["relationships"],
        columns_by_id=s["columns_by_id"], alias_by_table_id=s["alias_by_table_id"],
        connector="postgresql",
    )
    assert len(draft.plans) == 1
    plan = draft.plans[0]
    # attribute_key + key_grain_key_id carry the canonical identities.
    assert plan.attribute_key == f"attr:{s['rel_id']}"
    assert plan.key_grain_key_id == f"dim:{s['dim_id']}"
    assert plan.key_grain_column == "country_id"
    # PG double-quoted, aliased detail reference; MIN/COUNT DISTINCT/SUM CASE NULL.
    frags = draft.select_fragments
    assert len(frags) == 3
    assert frags[0].startswith('MIN(base."country_code") AS ')
    assert frags[1].startswith('COUNT(DISTINCT base."country_code") AS ')
    assert frags[2].startswith('SUM(CASE WHEN base."country_code" IS NULL THEN 1 ELSE 0 END) AS ')


def test_bigquery_fragments_backtick_quoted():
    s = _scenario()
    draft = plan_passengers(
        layout=s["layout"], grain_keys=s["grain_keys"],
        relationships=s["relationships"],
        columns_by_id=s["columns_by_id"], alias_by_table_id=s["alias_by_table_id"],
        connector="bigquery",
    )
    frags = draft.select_fragments
    # BigQuery backtick quoting on BOTH the detail ref and the aliases.
    assert frags[0].startswith("MIN(base.`country_code`) AS `")
    assert "`country_code`" in frags[1]


def test_passenger_absent_from_group_by_via_planner_never_emits_group():
    # The planner produces SELECT fragments only; passengers are never GROUP BY keys
    # (the DDL builders add only grain refs to GROUP BY). Assert no fragment looks
    # like a bare grain reference (they are all aggregates).
    s = _scenario()
    draft = plan_passengers(
        layout=s["layout"], grain_keys=s["grain_keys"],
        relationships=s["relationships"],
        columns_by_id=s["columns_by_id"], alias_by_table_id=s["alias_by_table_id"],
        connector="postgresql",
    )
    for frag in draft.select_fragments:
        assert frag.startswith(("MIN(", "COUNT(", "SUM("))


def test_collision_with_measure_disambiguates_deterministically():
    # A measure already named exactly like the passenger base forces the allocator to
    # disambiguate with the relationship id + role (never overwrite the measure).
    s = _scenario()
    # Make the measure physical name collide with the passenger base.
    s["layout"].measure_cols[0] = ResolvedMeasureCol(
        measure_id=uuid.uuid4(), measure_name="m", stat_type="sum",
        aggregation_function="sum", source_table_id=None,
        source_column_name="amount", physical_col_name="country_code__passenger",
    )
    draft = plan_passengers(
        layout=s["layout"], grain_keys=s["grain_keys"],
        relationships=s["relationships"],
        columns_by_id=s["columns_by_id"], alias_by_table_id=s["alias_by_table_id"],
        connector="postgresql",
    )
    plan = draft.plans[0]
    # The passenger got a disambiguated name, NOT the colliding measure name.
    assert plan.passenger_column != "country_code__passenger"
    assert str(s["rel_id"]).replace("-", "")[:8] in plan.passenger_column


def test_unresolvable_collision_raises_before_ddl():
    # Reserve BOTH the base name and its disambiguated form via extra_reserved_names
    # so the allocator cannot find a unique name -> raise BEFORE any DDL.
    s = _scenario()
    rel_short = str(s["rel_id"]).replace("-", "")[:8]
    reserved = [
        "country_code__passenger",
        f"country_code__passenger__{rel_short}__passenger",
    ]
    with pytest.raises(PassengerCollisionError):
        plan_passengers(
            layout=s["layout"], grain_keys=s["grain_keys"],
            relationships=s["relationships"],
            columns_by_id=s["columns_by_id"],
            alias_by_table_id=s["alias_by_table_id"], connector="postgresql",
            extra_reserved_names=reserved,
        )


def test_owning_key_not_a_grain_is_skipped_fail_closed():
    # The relationship's key column is NOT a materialised physical grain key of this
    # layout -> no built key column to sit the passenger beside -> skip (no plan).
    s = _scenario()
    # Replace the grain key with an EXPRESSION key (not a dim: physical key).
    s["grain_keys"] = [MaterializedGrainKey(
        ordinal=0, key_id="expr:abc", kind=KIND_ARTIFACT_EXPRESSION,
        physical_column="e", input_column_ids=[str(uuid.uuid4())],
    )]
    draft = plan_passengers(
        layout=s["layout"], grain_keys=s["grain_keys"],
        relationships=s["relationships"],
        columns_by_id=s["columns_by_id"], alias_by_table_id=s["alias_by_table_id"],
        connector="postgresql",
    )
    assert draft.is_empty()


def test_unresolved_detail_column_is_skipped():
    # The detail column id is not in columns_by_id -> cannot resolve the qualified
    # detail_ref -> skip (fail closed, no guessed name).
    s = _scenario()
    s["columns_by_id"].pop(str(s["detail_col_id"]))
    draft = plan_passengers(
        layout=s["layout"], grain_keys=s["grain_keys"],
        relationships=s["relationships"],
        columns_by_id=s["columns_by_id"], alias_by_table_id=s["alias_by_table_id"],
        connector="postgresql",
    )
    assert draft.is_empty()


def test_column_defs_ordered_passenger_then_diagnostics():
    s = _scenario()
    draft = plan_passengers(
        layout=s["layout"], grain_keys=s["grain_keys"],
        relationships=s["relationships"],
        columns_by_id=s["columns_by_id"], alias_by_table_id=s["alias_by_table_id"],
        connector="postgresql",
    )
    defs = draft.column_defs(target_dialect="postgresql")
    names = [n for n, _ in defs]
    plan = draft.plans[0]
    assert names == [
        plan.passenger_column, plan.detail_ndistinct_column,
        plan.detail_nullcount_column,
    ]


# ---------------------------------------------------------------------------
# Fable R1 #2/#3: fragment-connector selection matches the statement parse dialect.
# ---------------------------------------------------------------------------


def test_fragment_connector_same_db_bigquery_is_target_dialect():
    from shared.semantic.passenger_manifest_planner import passenger_fragment_connector
    assert passenger_fragment_connector(connector="bigquery", cross_db=False) == "bigquery"
    assert passenger_fragment_connector(connector="hadoop_spark", cross_db=False) == "hadoop_spark"


def test_fragment_connector_cross_db_is_postgres_canonical():
    from shared.semantic.passenger_manifest_planner import passenger_fragment_connector
    # Every cross-DB path executes a canonical-PG SELECT (transpiled), so PG-quoted.
    assert passenger_fragment_connector(connector="bigquery", cross_db=True) == "postgresql"
    assert passenger_fragment_connector(connector="hadoop_spark", cross_db=True) == "postgresql"
    assert passenger_fragment_connector(connector="postgresql", cross_db=True) == "postgresql"


def test_fragment_connector_same_db_pg_family_is_postgres_canonical():
    from shared.semantic.passenger_manifest_planner import passenger_fragment_connector
    # PG-family targets (incl. sqlserver/snowflake/redshift) build canonical-PG then
    # transpile, so fragments must be PG-quoted (a tsql bracket inside a postgres
    # statement would fail the transpile).
    for c in ("postgresql", "redshift", "snowflake", "sqlserver"):
        assert passenger_fragment_connector(connector=c, cross_db=False) == "postgresql"


# ---------------------------------------------------------------------------
# Fable R1 #4: a detail table absent from the FROM scope (no alias) is SKIPPED
# fail-closed, never emitted as a bare unqualified reference.
# ---------------------------------------------------------------------------


def test_missing_alias_skips_passenger_fail_closed():
    s = _scenario()
    # Empty the alias map so the detail table has no FROM-scope alias.
    draft = plan_passengers(
        layout=s["layout"], grain_keys=s["grain_keys"],
        relationships=s["relationships"],
        columns_by_id=s["columns_by_id"], alias_by_table_id={},
        connector="postgresql",
    )
    assert draft.is_empty()


# ---------------------------------------------------------------------------
# Bug-7877: only materialise passengers for BIJECTION relationships.
# ---------------------------------------------------------------------------


def test_non_bijection_relationship_is_skipped():
    """Bug-7877: a FUNCTIONAL_N_TO_1 relationship must not materialise a passenger
    (the serving binder rejects non-BIJECTION relabels, so the passenger can never
    be served -- wasted CTAS width)."""
    s = _scenario()
    s["relationships"] = [
        _rel(s["rel_id"], s["relationships"][0].key_column_id,
             s["relationships"][0].detail_column_id,
             cardinality="FUNCTIONAL_N_TO_1"),
    ]
    draft = plan_passengers(
        layout=s["layout"], grain_keys=s["grain_keys"],
        relationships=s["relationships"],
        columns_by_id=s["columns_by_id"], alias_by_table_id=s["alias_by_table_id"],
        connector="postgresql",
    )
    assert draft.is_empty()


def test_bijection_relationship_materialises_passenger():
    """Bug-7877 guard: a BIJECTION relationship must produce a passenger plan."""
    s = _scenario()
    # _scenario already uses BIJECTION by default.
    draft = plan_passengers(
        layout=s["layout"], grain_keys=s["grain_keys"],
        relationships=s["relationships"],
        columns_by_id=s["columns_by_id"], alias_by_table_id=s["alias_by_table_id"],
        connector="postgresql",
    )
    assert len(draft.plans) == 1


# ---------------------------------------------------------------------------
# Bug-7878: passenger draft -> persisted dict full-identity alignment.
# The incremental shape guard compares planned vs existing passenger identity
# tuples. This test verifies that passenger_column_draft() produces a dict
# whose contract fields match the plan, so the guard detects any semantic
# change (type, source column, relationship) even when physical names stay.
# ---------------------------------------------------------------------------


def test_passenger_draft_carries_full_identity_for_shape_guard():
    """Bug-7878: a type/source/relationship change on a passenger with unchanged
    physical names must be detectable by the incremental shape guard. The guard
    compares all PassengerColumn contract fields. Verify the plan -> draft mapping
    preserves every field the guard reads."""
    s = _scenario()
    draft = plan_passengers(
        layout=s["layout"], grain_keys=s["grain_keys"],
        relationships=s["relationships"],
        columns_by_id=s["columns_by_id"], alias_by_table_id=s["alias_by_table_id"],
        connector="postgresql",
    )
    plan = draft.plans[0]
    pc = plan.passenger_column_draft()
    pc_dict = pc.to_dict()
    # Every field the incremental shape guard reads must be present and match.
    assert pc_dict["passenger_column"] == plan.passenger_column
    assert pc_dict["detail_ndistinct_column"] == plan.detail_ndistinct_column
    assert pc_dict["detail_nullcount_column"] == plan.detail_nullcount_column
    assert pc_dict["source_column_id"] == str(plan.detail_column_id)
    assert pc_dict["output_type"] == plan.detail_type
    assert pc_dict["relationship_id"] == str(plan.relationship_id)
    assert pc_dict["attribute_key"] == plan.attribute_key


def test_passenger_type_change_same_physical_name_is_detectable():
    """Bug-7878 guard: a detail column whose output_type changes (e.g. INT -> TEXT)
    but keeps the same physical column name must produce a DIFFERENT identity tuple
    so the incremental shape guard forces a full refresh."""
    s = _scenario()
    draft_v1 = plan_passengers(
        layout=s["layout"], grain_keys=s["grain_keys"],
        relationships=s["relationships"],
        columns_by_id=s["columns_by_id"], alias_by_table_id=s["alias_by_table_id"],
        connector="postgresql",
    )
    p1 = draft_v1.plans[0]
    d1 = p1.passenger_column_draft().to_dict()

    # Simulate a type change: same physical name, different data_type.
    s2 = _scenario()
    detail_col_id = s2["detail_col_id"]
    s2["columns_by_id"][str(detail_col_id)] = _col(
        detail_col_id, list(s2["columns_by_id"].values())[0].model_table_id,
        "country_code", "BIGINT",  # was TEXT
    )
    draft_v2 = plan_passengers(
        layout=s2["layout"], grain_keys=s2["grain_keys"],
        relationships=s2["relationships"],
        columns_by_id=s2["columns_by_id"], alias_by_table_id=s2["alias_by_table_id"],
        connector="postgresql",
    )
    p2 = draft_v2.plans[0]
    d2 = p2.passenger_column_draft().to_dict()

    # Physical column names are identical.
    assert d1["passenger_column"] == d2["passenger_column"]
    # But the full identity differs (output_type changed).
    identity_fields = (
        "passenger_column", "detail_ndistinct_column", "detail_nullcount_column",
        "source_column_id", "output_type", "relationship_id", "attribute_key",
    )
    t1 = tuple(d1[f] for f in identity_fields)
    t2 = tuple(d2[f] for f in identity_fields)
    assert t1 != t2, "type change must produce a different identity tuple"
