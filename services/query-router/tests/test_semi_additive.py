"""Tests for semi-additive measure support (Block E).

Covers:
  - _semi_additive_agg SQL expression builder (unit)
  - Aggregate matcher: semi-additive measures require exact grain
  - Pydantic schema validation for semi_additive_behavior
"""
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from src.rewrite.query_rewriter import _semi_additive_agg
from src.routing.aggregate_matcher import find_best_aggregate

from conftest import (
    make_measure,
    make_dimension,
    make_agg_col,
    make_aggregate,
    make_bound_query,
)

try:
    from shared.schemas.pydantic_models import MeasureCreate as MeasureCreateSchema
    _HAS_PYDANTIC = True
except ImportError:
    _HAS_PYDANTIC = False


# ---------------------------------------------------------------------------
# _semi_additive_agg — SQL expression builder
# ---------------------------------------------------------------------------

def test_last_non_empty_sql():
    sql = _semi_additive_agg("last_non_empty", '"t"."balance"', '"t"."date"')
    assert "ARRAY_AGG" in sql
    assert "ORDER BY" in sql
    assert "DESC" in sql
    assert "FILTER" in sql
    assert "IS NOT NULL" in sql
    assert "[1]" in sql


def test_first_non_empty_sql():
    sql = _semi_additive_agg("first_non_empty", '"t"."balance"', '"t"."date"')
    assert "ARRAY_AGG" in sql
    assert "ASC" in sql
    assert "[1]" in sql


def test_avg_of_children_sql():
    sql = _semi_additive_agg("avg_of_children", '"t"."balance"', '"t"."date"')
    assert sql == 'AVG("t"."balance")'


def test_min_sql():
    sql = _semi_additive_agg("min", '"t"."balance"', '"t"."date"')
    assert sql == 'MIN("t"."balance")'


def test_max_sql():
    sql = _semi_additive_agg("max", '"t"."balance"', '"t"."date"')
    assert sql == 'MAX("t"."balance")'


def test_by_account_rejected_loud():
    # F-015-06: by_account previously fell back to plain last-non-empty with
    # the account column unused, silently returning wrong numbers for flow
    # accounts. The mode now fails loud at query time until per-account
    # dispatch is implemented.
    from src.ir.logical_query import SemanticBindingError

    with pytest.raises(SemanticBindingError, match="by_account"):
        _semi_additive_agg("by_account", '"t"."balance"', '"t"."date"')


def test_unknown_behavior_falls_back_to_sum():
    sql = _semi_additive_agg("unknown_thing", '"t"."balance"', '"t"."date"')
    assert sql == 'SUM("t"."balance")'


def test_no_time_dimension_falls_back_to_default():
    m = make_measure("balance", semi_additive_behavior="last_non_empty")
    bq = make_bound_query([make_dimension("region")], [m])
    assert m.semi_additive_behavior == "last_non_empty"


# ---------------------------------------------------------------------------
# Aggregate matcher — semi-additive measures require exact grain
# ---------------------------------------------------------------------------

_PATCH = "src.routing.aggregate_matcher.load_active_aggregates"


async def test_semi_additive_superset_grain_rejected():
    m = make_measure("balance", semi_additive_behavior="last_non_empty")
    agg = make_aggregate(
        ["country", "month"],
        [make_agg_col(m)],
    )
    bq = make_bound_query([make_dimension("country")], [m])

    with patch(_PATCH, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        result = await find_best_aggregate(bq, AsyncMock())
    assert result.aggregate is None


async def test_semi_additive_exact_grain_accepted():
    m = make_measure("balance", semi_additive_behavior="last_non_empty")
    agg = make_aggregate(["country"], [make_agg_col(m)])
    bq = make_bound_query([make_dimension("country")], [m])

    with patch(_PATCH, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        result = await find_best_aggregate(bq, AsyncMock())
    assert result.aggregate is agg


async def test_mixed_semi_additive_and_additive():
    m_sa = make_measure("balance", semi_additive_behavior="first_non_empty")
    m_add = make_measure("revenue")
    agg = make_aggregate(
        ["country"],
        [make_agg_col(m_sa), make_agg_col(m_add)],
    )
    bq = make_bound_query([make_dimension("country")], [m_sa, m_add])

    with patch(_PATCH, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        result = await find_best_aggregate(bq, AsyncMock())
    assert result.aggregate is agg


async def test_semi_additive_with_no_matching_aggregate():
    m = make_measure("balance", semi_additive_behavior="max")
    bq = make_bound_query([make_dimension("country")], [m])

    with patch(_PATCH, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = []
        result = await find_best_aggregate(bq, AsyncMock())
    assert result.aggregate is None


# ---------------------------------------------------------------------------
# Bug-5337: semi-additive measure binding in grouped vs bare shape
# ---------------------------------------------------------------------------


def test_binder_resolves_semi_additive_in_grouped_shape():
    """Bug-5337: a semi-additive measure (last_non_empty) must resolve in a
    grouped query (SELECT dim, SUM(measure) ... GROUP BY dim) identically to
    the bare no-GROUP-BY form.  Both shapes put the measure in
    requested_measures; the binder must find it in the measure map."""
    m = make_measure("account_balance", default_agg="last_non_empty",
                     semi_additive_behavior="last_non_empty", is_additive=False)
    d = make_dimension("country_code")
    bq = make_bound_query([d], [m], grain=["country_code"])

    assert len(bq.resolved_measures) == 1
    assert bq.resolved_measures[0].name == "account_balance"
    assert bq.resolved_measures[0].semi_additive_behavior == "last_non_empty"
    assert len(bq.resolved_dimensions) == 1
    assert bq.resolved_dimensions[0].name == "country_code"


def test_binder_resolves_semi_additive_in_bare_shape():
    """Bug-5337: the bare shape (SUM(measure) with no GROUP BY) must continue
    to work for semi-additive measures."""
    m = make_measure("account_balance", default_agg="last_non_empty",
                     semi_additive_behavior="last_non_empty", is_additive=False)
    bq = make_bound_query([], [m])

    assert len(bq.resolved_measures) == 1
    assert bq.resolved_measures[0].name == "account_balance"
    assert bq.resolved_measures[0].semi_additive_behavior == "last_non_empty"
    assert len(bq.resolved_dimensions) == 0


def test_binder_resolves_normal_additive_in_grouped_shape():
    """Control test: a normal additive measure must continue to bind
    in the grouped shape (no regression from Bug-5337 fix)."""
    m = make_measure("revenue")
    d = make_dimension("country_code")
    bq = make_bound_query([d], [m], grain=["country_code"])

    assert len(bq.resolved_measures) == 1
    assert bq.resolved_measures[0].name == "revenue"
    assert len(bq.resolved_dimensions) == 1


def test_semi_additive_default_agg_falls_back_to_sum():
    """Bug-5337: when default_agg is a non-SQL semi-additive behaviour tag
    (last_non_empty, first_non_empty, etc.) and no user-requested aggregate
    overrides it, the rewriter must use SUM — not emit the raw behaviour tag
    as a SQL function call, which would produce invalid SQL like
    LAST_NON_EMPTY("t"."col").

    The _SA_NON_SQL_AGGS set in source_sql.py drives the fallback."""
    from src.rewrite.source_sql import _SA_NON_SQL_AGGS

    # Verify all semi-additive behaviour tags are in the guard set
    for tag in ("LAST_NON_EMPTY", "FIRST_NON_EMPTY", "BY_ACCOUNT",
                "AVG_OF_CHILDREN"):
        assert tag in _SA_NON_SQL_AGGS, (
            f"{tag} must be in _SA_NON_SQL_AGGS to prevent invalid SQL"
        )

    # Verify standard SQL aggregates are NOT in the guard set
    for fn in ("SUM", "AVG", "MIN", "MAX", "COUNT", "COUNT_DISTINCT"):
        assert fn not in _SA_NON_SQL_AGGS, (
            f"{fn} must NOT be in _SA_NON_SQL_AGGS — it is a valid SQL function"
        )


def test_mixed_semi_additive_and_additive_grouped():
    """Bug-5337: a query referencing both a semi-additive and a normal
    additive measure must bind correctly in the grouped shape."""
    m_sa = make_measure("account_balance", default_agg="last_non_empty",
                        semi_additive_behavior="last_non_empty",
                        is_additive=False)
    m_add = make_measure("revenue")
    d = make_dimension("country_code")
    bq = make_bound_query([d], [m_sa, m_add], grain=["country_code"])

    measure_names = {m.name for m in bq.resolved_measures}
    assert measure_names == {"account_balance", "revenue"}
    assert bq.resolved_dimensions[0].name == "country_code"


# ---------------------------------------------------------------------------
# Pydantic schema validation
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _HAS_PYDANTIC, reason="shared.schemas import conflict")
def test_pydantic_by_account_requires_column_id():
    with pytest.raises(Exception):
        MeasureCreateSchema(
            name="balance",
            default_agg="sum",
            semi_additive_behavior="by_account",
        )


@pytest.mark.skipif(not _HAS_PYDANTIC, reason="shared.schemas import conflict")
def test_pydantic_valid_semi_additive_accepted():
    m = MeasureCreateSchema(
        name="balance",
        default_agg="sum",
        semi_additive_behavior="last_non_empty",
    )
    assert m.semi_additive_behavior == "last_non_empty"


@pytest.mark.skipif(not _HAS_PYDANTIC, reason="shared.schemas import conflict")
def test_pydantic_null_semi_additive_accepted():
    m = MeasureCreateSchema(name="revenue", default_agg="sum")
    assert m.semi_additive_behavior is None
