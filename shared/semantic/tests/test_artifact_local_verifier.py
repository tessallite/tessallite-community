"""T0 tests for the artifact-LOCAL edge verifier + manifest (spec §7.6.3, §14.2).

Phase 3. These pin that the artifact-local check asks the RIGHT questions over the
BUILT artifact rows, never returns VERIFIED on a violation/NULL/error, and that
the immutable manifest hash is deterministic and build-truth only. They include
the mandatory §14.2 adversarial known-value cases translated to the artifact-local
surface:
  - a two-ids->one-detail (false strict) artifact is BROKEN, never VERIFIED;
  - the SAME rows declared FUNCTIONAL_N_TO_1 VERIFY;
  - a NULL key/passenger endpoint fails closed;
  - a collation-folded label (two keys share one passenger) is caught by the
    artifact-local REVERSE strict check on the built target.
"""
from __future__ import annotations

import pytest

from shared.semantic.artifact_local_verifier import (
    ArtifactRelation,
    build_artifact_forward_check_sql,
    build_artifact_null_check_sql,
    build_artifact_reverse_check_sql,
    verify_artifact_edge,
)
from shared.semantic.artifact_manifest import (
    MANIFEST_VERSION,
    MaterializedAttributeEdge,
    MaterializedGrainKey,
    PassengerColumn,
    RowManifest,
    compute_manifest_hash,
    compute_row_manifest_hash,
)
from shared.semantic.attribute_relationship_verifier import (
    BIJECTION,
    BROKEN,
    ERR_FORWARD_VIOLATION,
    ERR_NULL_ENDPOINT,
    ERR_REVERSE_VIOLATION,
    ERROR,
    FUNCTIONAL_N_TO_1,
    VERIFIED,
)
from shared.semantic.passenger_build import (
    PassengerSpec,
    build_passenger_select_fragments,
    edge_activatable,
)

pytestmark = pytest.mark.unit


_REL = ArtifactRelation(
    table_ref="agg_meta.agg_abc123",
    key_grain_column="country_id",
    detail_passenger_column="country_name__passenger",
    # Bug-7898: the built forward-dependency diagnostics carried beside the
    # passenger. verify_artifact_edge now runs a diagnostic check over these
    # (ndistinct > 1 OR nullcount > 0) as the authoritative forward/NULL guard for
    # the MIN()-collapsed passenger; an edge without them fails closed to ERROR.
    detail_ndistinct_column="country_name__detail_ndistinct",
    detail_nullcount_column="country_name__detail_nullcount",
)


# ---------------------------------------------------------------------------
# Check-shape assertions over the BUILT artifact (right direction)
# ---------------------------------------------------------------------------


def test_forward_check_groups_by_key_over_passenger():
    sql = build_artifact_forward_check_sql(_REL, "postgresql")
    assert 'GROUP BY "country_id"' in sql
    assert 'COUNT(DISTINCT "country_name__passenger") > 1' in sql
    assert '"agg_meta"."agg_abc123"' in sql


def test_reverse_check_groups_by_passenger_over_key():
    # Reverse strict on the BUILT target: this is what catches a target collation
    # folding two source-distinct labels across keys (spec §7.6.2 text note).
    sql = build_artifact_reverse_check_sql(_REL, "postgresql")
    assert 'GROUP BY "country_name__passenger"' in sql
    assert 'COUNT(DISTINCT "country_id") > 1' in sql


def test_null_check_rejects_either_endpoint():
    sql = build_artifact_null_check_sql(_REL, "postgresql")
    assert '"country_id" IS NULL OR "country_name__passenger" IS NULL' in sql


def test_bigquery_backticks_no_connector_branch():
    sql = build_artifact_forward_check_sql(_REL, "bigquery")
    assert "`country_id`" in sql and "`country_name__passenger`" in sql


# ---------------------------------------------------------------------------
# Driver fail-closed behaviour over the BUILT rows (§14.2 known-value cases)
# ---------------------------------------------------------------------------


class _FakeConn:
    connection_type = "postgresql"


def _fake_executor(row_map):
    async def _exec(conn_obj, sql, *, tenant_session=None):
        for needle, rows in row_map.items():
            if needle in sql:
                return rows, []
        return [], []
    return _exec


@pytest.mark.asyncio
async def test_verified_when_all_checks_empty(monkeypatch):
    import shared.source_executor as se
    monkeypatch.setattr(se, "execute_source_sql", _fake_executor({}))
    ev = await verify_artifact_edge(
        rel=_REL, cardinality=BIJECTION, connector="postgresql",
        conn_obj=_FakeConn(),
    )
    assert ev.status == VERIFIED


@pytest.mark.asyncio
async def test_false_strict_bijection_is_broken_never_verified(monkeypatch):
    # Two ids share one built passenger 'Shared': reverse strict fails. A BIJECTION
    # artifact MUST be BROKEN — never VERIFIED (§14.2 mandatory guard).
    import shared.source_executor as se
    monkeypatch.setattr(
        se, "execute_source_sql",
        _fake_executor({'COUNT(DISTINCT "country_id")': [{"country_name__passenger": "Shared"}]}),
    )
    ev = await verify_artifact_edge(
        rel=_REL, cardinality=BIJECTION, connector="postgresql",
        conn_obj=_FakeConn(),
    )
    assert ev.status == BROKEN
    assert ev.error_code == ERR_REVERSE_VIOLATION


@pytest.mark.asyncio
async def test_same_rows_as_functional_n_to_1_verify(monkeypatch):
    # The SAME shared-label built rows declared N:1 VERIFY: only forward runs and
    # each built key row carries one passenger.
    import shared.source_executor as se
    monkeypatch.setattr(se, "execute_source_sql", _fake_executor({}))
    ev = await verify_artifact_edge(
        rel=_REL, cardinality=FUNCTIONAL_N_TO_1, connector="postgresql",
        conn_obj=_FakeConn(),
    )
    assert ev.status == VERIFIED


@pytest.mark.asyncio
async def test_forward_violation_breaks_both_cardinalities(monkeypatch):
    # A built key row carrying two distinct passengers is impossible for a real
    # coarsening -> BROKEN even for N:1.
    import shared.source_executor as se
    monkeypatch.setattr(
        se, "execute_source_sql",
        _fake_executor({'COUNT(DISTINCT "country_name__passenger")': [{"country_id": 7}]}),
    )
    ev = await verify_artifact_edge(
        rel=_REL, cardinality=FUNCTIONAL_N_TO_1, connector="postgresql",
        conn_obj=_FakeConn(),
    )
    assert ev.status == BROKEN
    assert ev.error_code == ERR_FORWARD_VIOLATION


@pytest.mark.asyncio
async def test_null_endpoint_on_built_rows_fails_closed(monkeypatch):
    import shared.source_executor as se
    monkeypatch.setattr(
        se, "execute_source_sql",
        _fake_executor({"IS NULL OR": [{"x": 1}]}),
    )
    ev = await verify_artifact_edge(
        rel=_REL, cardinality=BIJECTION, connector="postgresql",
        conn_obj=_FakeConn(),
    )
    assert ev.status == BROKEN
    assert ev.error_code == ERR_NULL_ENDPOINT


@pytest.mark.asyncio
async def test_diagnostic_ndistinct_gt_one_is_broken(monkeypatch):
    # Bug-7898: a key with >1 distinct source detail is masked by the MIN()
    # passenger (forward/null checks pass), but the built ndistinct diagnostic
    # catches it -> BROKEN. The diagnostic check runs FIRST.
    import shared.source_executor as se
    monkeypatch.setattr(
        se, "execute_source_sql",
        _fake_executor({'"country_name__detail_ndistinct" > 1': [{"x": 1}]}),
    )
    ev = await verify_artifact_edge(
        rel=_REL, cardinality=BIJECTION, connector="postgresql",
        conn_obj=_FakeConn(),
    )
    assert ev.status == BROKEN
    assert ev.error_code == ERR_FORWARD_VIOLATION


@pytest.mark.asyncio
async def test_diagnostic_nullcount_gt_zero_is_broken(monkeypatch):
    # Bug-7898: a PARTIAL NULL detail (some rows NULL for a key) is skipped by
    # MIN() and by the artifact null check (which only sees all-NULL keys), but
    # the built nullcount diagnostic catches it -> BROKEN (I16 NULL-endpoint rule).
    import shared.source_executor as se
    monkeypatch.setattr(
        se, "execute_source_sql",
        _fake_executor({'"country_name__detail_nullcount" > 0': [{"x": 1}]}),
    )
    ev = await verify_artifact_edge(
        rel=_REL, cardinality=BIJECTION, connector="postgresql",
        conn_obj=_FakeConn(),
    )
    assert ev.status == BROKEN
    assert ev.error_code == ERR_FORWARD_VIOLATION


@pytest.mark.asyncio
async def test_missing_diagnostics_fail_closed_to_error(monkeypatch):
    # Bug-7898: an edge whose build did NOT carry the forward diagnostics cannot
    # be proven clean (the MIN() passenger hides forward violations), so it fails
    # closed to ERROR -> no trust. Never VERIFIED on absent proof.
    import shared.source_executor as se
    monkeypatch.setattr(se, "execute_source_sql", _fake_executor({}))
    rel_no_diag = ArtifactRelation(
        table_ref="agg_meta.agg_abc123",
        key_grain_column="country_id",
        detail_passenger_column="country_name__passenger",
    )
    ev = await verify_artifact_edge(
        rel=rel_no_diag, cardinality=BIJECTION, connector="postgresql",
        conn_obj=_FakeConn(),
    )
    assert ev.status == ERROR


@pytest.mark.asyncio
async def test_execution_error_is_error_not_verified(monkeypatch):
    import shared.source_executor as se

    async def _boom(conn_obj, sql, *, tenant_session=None):
        raise RuntimeError("connector lost")

    monkeypatch.setattr(se, "execute_source_sql", _boom)
    ev = await verify_artifact_edge(
        rel=_REL, cardinality=BIJECTION, connector="postgresql",
        conn_obj=_FakeConn(),
    )
    assert ev.status == ERROR


# ---------------------------------------------------------------------------
# Passenger build fragments + activation gate (pitfall 18, §7.6.3)
# ---------------------------------------------------------------------------


def test_passenger_fragments_come_from_one_keyed_statement():
    spec = PassengerSpec(
        relationship_id="r1", detail_physical="country_name", base_name="country_name",
    )
    frags = build_passenger_select_fragments(spec, "postgresql")
    # Passenger value + the two forward-dependency diagnostics, all from the same
    # grouped statement (no separate scan) — pitfall 18.
    assert any('MIN("country_name")' in f for f in frags)
    assert any('COUNT(DISTINCT "country_name")' in f for f in frags)
    assert any("CASE WHEN" in f and "IS NULL" in f for f in frags)


def test_edge_activatable_only_on_one_distinct_non_null():
    assert edge_activatable(1, 0) is True
    assert edge_activatable(2, 0) is False   # two details per key -> not a function
    assert edge_activatable(1, 3) is False   # NULL endpoints present
    assert edge_activatable(None, 0) is False  # unknown diagnostic fails closed
    assert edge_activatable(1, None) is False


# ---------------------------------------------------------------------------
# Manifest hash: deterministic + build-truth only (I8/I12)
# ---------------------------------------------------------------------------


def _sample_edge(dh="dh1"):
    return MaterializedAttributeEdge(
        relationship_id="r1", key_grain_column="country_id",
        detail_passenger_column="country_name__passenger",
        cardinality=BIJECTION, declaration_hash=dh,
        artifact_refresh_run_id="run-1",
    )


def _sample_passenger():
    return PassengerColumn(
        passenger_column="country_name__passenger", source_column_id="col-1",
        output_type="TEXT", nullable=False, relationship_id="r1",
    )


def test_manifest_hash_deterministic():
    h1 = compute_manifest_hash(
        grain_keys=[], attribute_edges=[_sample_edge()],
        passenger_columns=[_sample_passenger()],
    )
    h2 = compute_manifest_hash(
        grain_keys=[], attribute_edges=[_sample_edge()],
        passenger_columns=[_sample_passenger()],
    )
    assert h1 == h2 and len(h1) == 64


def test_manifest_hash_changes_on_declaration_change():
    base = compute_manifest_hash(
        grain_keys=[], attribute_edges=[_sample_edge("dh1")],
        passenger_columns=[_sample_passenger()],
    )
    changed = compute_manifest_hash(
        grain_keys=[], attribute_edges=[_sample_edge("dh2")],
        passenger_columns=[_sample_passenger()],
    )
    assert base != changed


def test_manifest_hash_ignores_edge_backref():
    # The edge's own manifest_hash back-reference must NOT feed the hash (it is
    # filled FROM it), so setting it does not change the computed value.
    e_no = _sample_edge()
    e_with = _sample_edge()
    e_with.manifest_hash = "deadbeef"
    assert compute_manifest_hash(
        grain_keys=[], attribute_edges=[e_no], passenger_columns=[],
    ) == compute_manifest_hash(
        grain_keys=[], attribute_edges=[e_with], passenger_columns=[],
    )


def test_grain_key_kinds_and_version_round_trip():
    k = MaterializedGrainKey(
        ordinal=0, key_id="dim:abc", kind="PHYSICAL_COLUMN",
        physical_column="country_id",
    )
    d = k.to_dict()
    assert d["manifest_version"] == MANIFEST_VERSION
    assert d["kind"] == "PHYSICAL_COLUMN"


def test_row_manifest_hash_excludes_run_binding():
    # The run id is a live binding, not identity — two builds of the same rows on
    # different runs hash the same row manifest.
    rm1 = RowManifest(
        deployed_version_id="v1", row_definition_fingerprint="fp1",
        build_refresh_run_id="run-a",
    )
    rm2 = RowManifest(
        deployed_version_id="v1", row_definition_fingerprint="fp1",
        build_refresh_run_id="run-b",
    )
    assert compute_row_manifest_hash(rm1) == compute_row_manifest_hash(rm2)
