"""Internal PostgreSQL dialect contract for Looker Cloud Core SQL shapes."""
from __future__ import annotations

import types
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from src.api.routes import ExecuteRequest, _handle_execute
from src.ir.logical_query import BoundQuery, UnsupportedSQL
from src.parsing.sql_parser import parse_sql_to_ir
from src.routing.router import _validate_complex_passthrough
from src.semantic.binder import bind_query_to_model


def _bound(sql: str) -> BoundQuery:
    query = parse_sql_to_ir(sql, "model-1", protocol="jdbc")
    return BoundQuery(
        logical_query=query,
        model=types.SimpleNamespace(id="model-1", slug="modelx", deployed_version_id="v1"),
        resolved_measures=[],
        resolved_dimensions=[],
        resolved_filters=[],
    )


async def test_cte_binds_with_model_relation_and_cte_alias() -> None:
    query = parse_sql_to_ir(
        "WITH base AS (SELECT payment_status FROM modelx) "
        "SELECT payment_status FROM base",
        "model-1",
        protocol="jdbc",
    )
    model = types.SimpleNamespace(id="model-1", slug="modelx", display_name="Model X", deployed_version_id="v1")
    # F-003-02: the CTE body references physical column ``payment_status`` over the
    # model relation, so the deployed shape must declare it; the new complex-SQL
    # column-containment gate fails closed on an empty/unavailable vocabulary.
    from src.semantic.snapshot_resolver import DeployedShape
    _shape = DeployedShape(
        measures=[], dimensions=[], hidden_column_ids=set(),
        physical_columns_all={"payment_status"},
        physical_columns_visible={"payment_status"},
        hierarchy_rows=[],
    )
    with (
        patch("src.semantic.binder._load_model", new=AsyncMock(return_value=model)),
        patch("src.semantic.binder.resolve_deployed_shape", new=AsyncMock(return_value=_shape)),
        patch("src.semantic.binder._load_measures", new=AsyncMock(return_value=[])),
        patch("src.semantic.binder._load_dimensions", new=AsyncMock(return_value=[])),
        patch("src.semantic.binder._load_hierarchy_level_dimensions", new=AsyncMock(return_value=[])),
        patch("src.semantic.binder._load_hidden_column_ids", new=AsyncMock(return_value=set())),
    ):
        bound = await bind_query_to_model(query, AsyncMock(), include_hidden=True)

    assert query.has_complex_sql is True
    assert query.cte_aliases == ["base"]
    assert bound.model is model


def test_having_alias_and_limit_offset_are_preserved_in_ir() -> None:
    query = parse_sql_to_ir(
        "SELECT payment_status, SUM(amount) AS total FROM modelx "
        "GROUP BY payment_status HAVING total > 100 LIMIT 25 OFFSET 10",
        "model-1",
        protocol="jdbc",
    )
    assert query.having_raw == "HAVING total > 100"
    assert query.limit == 25
    assert query.offset == 10


def test_single_table_dimension_window_is_allowed_for_passthrough() -> None:
    bound = _bound(
        "SELECT payment_status, ROW_NUMBER() OVER (ORDER BY payment_status) AS rn "
        "FROM modelx"
    )
    assert bound.logical_query.has_window_functions is True
    _validate_complex_passthrough(bound)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT SUM(amount) OVER (PARTITION BY payment_status) FROM modelx",
        "WITH ranked AS (SELECT SUM(amount) OVER () AS total FROM modelx) "
        "SELECT total FROM ranked",
    ],
)
def test_single_table_window_aggregate_is_allowed(sql: str) -> None:
    """Window aggregates on a single semantic relation pass through."""
    bound = _bound(sql)
    assert bound.logical_query.has_window_functions is True
    _validate_complex_passthrough(bound)  # must not raise


def test_multi_table_window_raises_feature_not_supported() -> None:
    """Window functions across a multi-table JOIN are rejected."""
    sql = (
        "SELECT ROW_NUMBER() OVER (ORDER BY f.payment_id) "
        "FROM modelx__payment_transaction f JOIN modelx__dim_account_type d "
        "ON f.account_type_code = d.account_type_code"
    )
    with pytest.raises(UnsupportedSQL) as exc:
        _validate_complex_passthrough(_bound(sql))
    assert exc.value.sqlstate == "0A000"
    assert "multiple semantic relations" in str(exc.value)


def test_complex_sql_over_multiple_semantic_relations_is_rejected() -> None:
    bound = _bound(
        "WITH base AS (SELECT f.payment_id FROM modelx__payment AS f "
        "JOIN modelx__dimension AS d ON f.payment_id = d.payment_id) "
        "SELECT payment_id FROM base"
    )

    with pytest.raises(UnsupportedSQL) as exc:
        _validate_complex_passthrough(bound)

    assert exc.value.sqlstate == "0A000"
    assert "multiple semantic relations" in str(exc.value)


def test_cte_alias_colliding_with_physical_relation_still_counts_both() -> None:
    """Bug-3641: a CTE alias that collides with a physical relation name must
    NOT hide a real second relation from the multi-relation gate. The old
    bare-name (from_tables minus cte_aliases) count excluded the physical
    ``sales`` scan because it matched the CTE alias ``sales``, leaving only
    ``other`` (count 1) — so a genuinely multi-relation complex query slipped
    past the gate. Scope analysis counts both physical scans and rejects."""
    bound = _bound(
        "WITH sales AS (SELECT account_type_code FROM modelx__dim_account_type) "
        "SELECT s.account_type_code "
        "FROM sales s JOIN modelx__payment_transaction other "
        "ON s.account_type_code = other.account_type_code"
    )
    assert bound.logical_query.has_complex_sql is True
    with pytest.raises(UnsupportedSQL) as exc:
        _validate_complex_passthrough(bound)
    assert exc.value.sqlstate == "0A000"
    assert "multiple semantic relations" in str(exc.value)


def test_self_join_counts_as_single_physical_relation_and_passes() -> None:
    """F-P4ac-02 (Bug-3641 follow-up): a self-join references the SAME physical
    relation under two aliases. The scope-aware relation count is a *set* of
    physical names, so a self-join collapses to one relation (count 1) and must
    PASS the complex-passthrough gate — the rewriter substitutes that single
    physical name for both aliases coherently. A genuine two-relation join
    (distinct physical tables) is still rejected. This pins the set-vs-list
    behaviour the Bug-3641 fix introduced so it can't silently regress to either
    rejecting valid self-joins or admitting real multi-relation joins."""
    from src.routing.router import _distinct_physical_relation_count

    self_join = _bound(
        "SELECT a.payment_id "
        "FROM modelx__payment_transaction a "
        "JOIN modelx__payment_transaction b "
        "ON a.account_type_code = b.account_type_code"
    )
    assert self_join.logical_query.has_complex_sql is True
    assert _distinct_physical_relation_count(self_join.logical_query) == 1
    # Must NOT raise — a self-join is a single physical relation.
    _validate_complex_passthrough(self_join)

    # Contrast: a genuine two-relation join is still rejected.
    two_relation = _bound(
        "SELECT a.payment_id "
        "FROM modelx__payment_transaction a "
        "JOIN modelx__dim_account_type b "
        "ON a.account_type_code = b.account_type_code"
    )
    assert _distinct_physical_relation_count(two_relation.logical_query) == 2
    with pytest.raises(UnsupportedSQL):
        _validate_complex_passthrough(two_relation)


async def test_execute_exposes_sqlstate_for_unsupported_window_sql() -> None:
    body = ExecuteRequest(model_id="model-1", raw_query="SELECT 1", protocol="jdbc")
    bound = _bound("SELECT SUM(amount) OVER () FROM modelx")
    with (
        patch("src.api.routes.bind_query_to_model", new=AsyncMock(return_value=bound)),
        patch("src.api.routes.apply_persona_gate", new=AsyncMock(return_value=None)),
        patch("src.api.routes.route_query", new=AsyncMock(side_effect=UnsupportedSQL("unsupported"))),
        patch("src.api.routes._cache.get", return_value=None),
    ):
        with pytest.raises(HTTPException) as exc:
            await _handle_execute(body, AsyncMock(), user_identity="user@example.com")

    assert exc.value.status_code == 422
    assert exc.value.detail["sqlstate"] == "0A000"
    assert exc.value.detail["error_type"] == "feature_not_supported"
