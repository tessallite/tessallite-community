"""Unit tests for Named Query resolution (``src/routing/named_query_resolver.py``).

Covers, all deterministic and DB-free:
  - Reference-shape recognition: the exact ``SELECT * FROM @name`` shape is
    accepted; every decoration is not (invariant 5).
  - FROM-position detection for the specific unsupported-shape error.
  - Definition extraction from snapshots (case-insensitive keys, duplicate
    fail-closed, artifact pointer pass-through).
  - Projection security proof (pocket §5.1 consumed): row-preserving shape
    legs, manifest liveness binding, case-sensitive column coverage,
    user_mapping refusal, empty security columns refusal.
"""
from __future__ import annotations

import pytest

from src.routing.named_query_resolver import (
    NamedQueryDefinition,
    NamedQueryError,
    _definition_is_row_preserving_projection,
    _extract_named_queries_from_snapshot,
    manifest_has_security_columns,
    named_query_reference_name,
    projection_security_proof_holds,
    sql_references_named_query_position,
)


# ---------------------------------------------------------------------------
# Shape recognition
# ---------------------------------------------------------------------------

EXACT_SHAPES = [
    "SELECT * FROM @branch_3279863",
    "select * from @branch_3279863",
    "SELECT * FROM @branch_3279863;",
    "  SELECT * FROM @branch_3279863  ",
    "SELECT * FROM @x_1",
    "SELECT * FROM @camelCase",
]


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT * FROM @branch_3279863", "branch_3279863"),
        ("select * from @branch_3279863", "branch_3279863"),
        ("SELECT * FROM @branch_3279863;", "branch_3279863"),
        ("  SELECT * FROM @branch_3279863  ", "branch_3279863"),
        ("SELECT * FROM @x_1", "x_1"),
        ("SELECT * FROM @camelCase", "camelCase"),
    ],
)
def test_exact_reference_shape_recognised(sql: str, expected: str) -> None:
    assert named_query_reference_name(sql) == expected


@pytest.mark.parametrize(
    "sql",
    [
        # Projection subset
        "SELECT branch_id FROM @branch_3279863",
        "SELECT t.* FROM @branch_3279863 t",
        # WHERE against the reference
        "SELECT * FROM @branch_3279863 WHERE x = 1",
        # Joins / nested / set ops
        "SELECT * FROM @a JOIN model ON 1 = 1",
        "SELECT * FROM (SELECT * FROM @a) t",
        "SELECT * FROM @a, @b",
        "SELECT * FROM @a UNION SELECT * FROM @b",
        # Decoration of the projection
        "SELECT DISTINCT * FROM @a",
        "SELECT * FROM @a LIMIT 10",
        "SELECT * FROM @a ORDER BY 1",
        # A real table / literal, not a reference at all
        "SELECT * FROM modely",
        "SELECT * FROM '@not_a_ref'",
        "SELECT * FROM modely WHERE x = '@a'",
        # Multi-statement
        "SELECT * FROM @a; SELECT 1",
    ],
)
def test_decorated_shapes_are_not_exact(sql: str) -> None:
    assert named_query_reference_name(sql) is None


def test_from_position_detection_for_unsupported_shapes() -> None:
    """Decorated references still yield the FROM-position name so the
    interceptor can raise NQ_UNSUPPORTED_SHAPE instead of a generic error."""
    assert sql_references_named_query_position("SELECT branch_id FROM @nq") == "nq"
    assert sql_references_named_query_position("SELECT * FROM @nq WHERE x = 1") == "nq"
    assert (
        sql_references_named_query_position("SELECT * FROM @a JOIN t2 ON 1=1") == "a"
    )
    assert sql_references_named_query_position("SELECT * FROM modely") is None
    assert sql_references_named_query_position("SELECT * FROM '@not'") is None


# ---------------------------------------------------------------------------
# Snapshot definition extraction
# ---------------------------------------------------------------------------

def _snapshot_row(name: str, **overrides) -> dict:
    row = {
        "id": "00000000-0000-0000-0000-000000000001",
        "name": name,
        "definition_sql": "SELECT * FROM modely WHERE branch_id = '1'",
        "output_columns": [{"name": "*", "type": "string"}],
        "shape": "projection",
        "row_cap": None,
        "column_cap": None,
        "artifact": None,
    }
    row.update(overrides)
    return row


def test_extract_definitions_case_insensitive_keys() -> None:
    definitions = _extract_named_queries_from_snapshot(
        {"named_queries": [_snapshot_row("TopCities")]}
    )
    assert "@topcities" in definitions
    assert definitions["@topcities"].name == "TopCities"
    assert definitions["@topcities"].shape == "projection"


def test_extract_definitions_artifact_pointer() -> None:
    pointer = {
        "physical_table_name": "nq_abc_1234",
        "target_schema": "public",
        "status": "fresh",
        "row_manifest": {"columns": []},
        "active_refresh_run_id": "00000000-0000-0000-0000-000000000099",
        "target_id": "00000000-0000-0000-0000-000000000042",
    }
    definitions = _extract_named_queries_from_snapshot(
        {"named_queries": [_snapshot_row("Slice", artifact=pointer)]}
    )
    assert definitions["@slice"].artifact == pointer


def test_extract_duplicate_lowercase_keys_fails_closed() -> None:
    with pytest.raises(NamedQueryError) as excinfo:
        _extract_named_queries_from_snapshot(
            {"named_queries": [_snapshot_row("X"), _snapshot_row("x")]}
        )
    assert "duplicate" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Projection security proof (pocket §5.1 consumed, never authored)
# ---------------------------------------------------------------------------

def _manifest(
    columns: list[dict] | None = None,
    *,
    build_refresh_run_id: str = "run-1",
    manifest_version: int = 2,
) -> dict:
    return {
        "columns": columns
        if columns is not None
        else [{"logical_name": "branch_id", "physical_column": "branch_id"}],
        "build_refresh_run_id": build_refresh_run_id,
        "manifest_version": manifest_version,
    }


def test_manifest_column_coverage_exact_case() -> None:
    manifest = _manifest(columns=[{"logical_name": "branch_id"}])
    assert manifest_has_security_columns(manifest, ["branch_id"], "run-1")
    # Case-sensitive: a case-only mismatch fails closed.
    assert not manifest_has_security_columns(manifest, ["Branch_ID"], "run-1")


def test_manifest_requires_liveness_binding() -> None:
    manifest = _manifest()
    # Pointer must be present AND match the manifest's own run id.
    assert not manifest_has_security_columns(manifest, ["branch_id"], None)
    assert not manifest_has_security_columns(manifest, ["branch_id"], "run-OTHER")
    assert manifest_has_security_columns(manifest, ["branch_id"], "run-1")


def test_manifest_missing_or_empty_fails_closed() -> None:
    assert not manifest_has_security_columns(None, ["branch_id"], "run-1")
    assert not manifest_has_security_columns({}, ["branch_id"], "run-1")
    assert not manifest_has_security_columns(
        _manifest(columns=[]), ["branch_id"], "run-1"
    )


def test_projection_proof_holds_for_row_preserving_definition() -> None:
    assert projection_security_proof_holds(
        definition_sql="SELECT * FROM modely WHERE branch_id = 'x'",
        manifest=_manifest(),
        active_refresh_run_id="run-1",
        security_columns=["branch_id"],
        user_mapping_active=False,
    )


@pytest.mark.parametrize(
    "definition_sql",
    [
        "SELECT * FROM modely WHERE branch_id = 'x' LIMIT 10",  # limit breaks filter-then-limit parity
        "SELECT * FROM modely WHERE branch_id = 'x' OFFSET 5",
        "SELECT DISTINCT * FROM modely",
        "SELECT * FROM modely GROUP BY branch_id",
        "SELECT * FROM a JOIN b ON a.id = b.id",
        "SELECT * FROM (SELECT * FROM modely) t",
        "SELECT branch_id, name FROM modely",  # explicit list, not SELECT *
        "WITH c AS (SELECT * FROM modely) SELECT * FROM c",
    ],
)
def test_projection_proof_requires_row_preserving_shape(definition_sql: str) -> None:
    assert not projection_security_proof_holds(
        definition_sql=definition_sql,
        manifest=_manifest(),
        active_refresh_run_id="run-1",
        security_columns=["branch_id"],
        user_mapping_active=False,
    )


def test_projection_proof_refuses_user_mapping() -> None:
    assert not projection_security_proof_holds(
        definition_sql="SELECT * FROM modely WHERE branch_id = 'x'",
        manifest=_manifest(),
        active_refresh_run_id="run-1",
        security_columns=["branch_id"],
        user_mapping_active=True,
    )


def test_projection_proof_refuses_empty_security_columns() -> None:
    assert not projection_security_proof_holds(
        definition_sql="SELECT * FROM modely WHERE branch_id = 'x'",
        manifest=_manifest(),
        active_refresh_run_id="run-1",
        security_columns=[],
        user_mapping_active=False,
    )


def test_projection_proof_refuses_missing_security_column() -> None:
    manifest = _manifest(columns=[{"logical_name": "other_col"}])
    assert not projection_security_proof_holds(
        definition_sql="SELECT * FROM modely WHERE branch_id = 'x'",
        manifest=manifest,
        active_refresh_run_id="run-1",
        security_columns=["branch_id"],
        user_mapping_active=False,
    )


def test_named_query_definition_frozen_dataclass() -> None:
    d = NamedQueryDefinition(
        id="1", name="n", definition_sql="SELECT 1",
        output_columns=[{"name": "a", "type": "number"}],
    )
    assert d.output_columns == ({"name": "a", "type": "number"},)


