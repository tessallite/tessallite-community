"""Tests for semi-additive measure support (Block E).

Covers:
  - _semi_additive_agg SQL expression builder (unit)
  - Aggregate matcher: semi-additive measures require exact grain
  - Pydantic schema validation for semi_additive_behavior
"""
import pytest

from conftest import attach_fixture_deployed_shape
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
# Bug-6222 (F-015-27): semi-additive + cumulation/window variant guard
# ---------------------------------------------------------------------------

def _guard_fires(variant_kind: str, semi_additive_behavior: str | None) -> bool:
    """Execute the same guard logic source_sql.py uses.  Returns True when
    the guard WOULD raise SemanticBindingError.  Imports from source_sql.py
    so the test fails if the guard is removed or the constants diverge."""
    from src.rewrite.source_sql import (
        SEMI_ADDITIVE_INELIGIBLE_FAMILIES as _INELIGIBLE,
        TIME_VARIANT_FAMILY as _FAMILY,
    )
    family = _FAMILY.get(variant_kind)
    return bool(semi_additive_behavior and family in _INELIGIBLE)


def test_semi_additive_with_ytd_variant_guard_fires():
    """Bug-6222: a semi-additive measure (last_non_empty) combined with a
    period_to_date family variant (ytd) must trigger the execution guard.
    Exercises the actual constants imported into source_sql.py."""
    assert _guard_fires("ytd", "last_non_empty")


def test_semi_additive_with_qtd_variant_guard_fires():
    """Bug-6222: qtd is also in the period_to_date family."""
    assert _guard_fires("qtd", "first_non_empty")


def test_semi_additive_with_trailing_n_variant_guard_fires():
    """Bug-6222: trailing_n is in the moving_window family."""
    assert _guard_fires("trailing_n", "last_non_empty")


def test_semi_additive_with_moving_avg_n_variant_guard_fires():
    """Bug-6222: moving_avg_n is also in the moving_window family."""
    assert _guard_fires("moving_avg_n", "last_non_empty")


def test_semi_additive_with_lag_variant_allowed():
    """Bug-6222: lag is NOT in the ineligible set -- the guard must NOT
    fire for lag (prior-period balance comparison is valid)."""
    assert not _guard_fires("lag", "last_non_empty")


def test_semi_additive_with_prior_year_variant_allowed():
    """Bug-6222: parallel_period variants (prior_year) are allowed."""
    assert not _guard_fires("prior_year", "last_non_empty")


def test_non_semi_additive_with_ytd_variant_allowed():
    """Bug-6222: a normal (additive) measure with ytd must not trigger
    the guard (only semi-additive measures are guarded)."""
    assert not _guard_fires("ytd", None)


# ---------------------------------------------------------------------------
# Bug-6220 (F-015-25): time_grain threading to VariantBinding
# ---------------------------------------------------------------------------

def test_variant_binding_receives_time_grain():
    """Bug-6220: VariantBinding must accept and expose time_grain so the
    grain-aware partition key logic activates at aggregated grains."""
    from shared.semantic.time_variants_sql import VariantBinding

    b = VariantBinding(
        base_expression="SUM(amount)",
        fact_date_column="MIN(f.order_date)",
        time_grain="month",
    )
    assert b.time_grain == "month"


def test_variant_binding_time_grain_defaults_to_none():
    """Bug-6220: backward compatibility -- time_grain defaults to None
    (day grain semantics)."""
    from shared.semantic.time_variants_sql import VariantBinding

    b = VariantBinding(
        base_expression="SUM(amount)",
        fact_date_column="MIN(f.order_date)",
    )
    assert b.time_grain is None


def test_prior_partition_keys_omit_day_at_month_grain():
    """Bug-6220: at month grain, the day-level partition key
    (EXTRACT(DAY FROM ...)) must be omitted from prior-period partitions
    because MIN(date) varies across months, causing NULL returns."""
    from shared.semantic.time_variants_sql import (
        VariantBinding,
        _prior_partition_keys,
    )

    # Day grain: day-level key IS present
    b_day = VariantBinding(
        base_expression="SUM(amount)",
        fact_date_column="MIN(f.order_date)",
        calendar_type="standard",
        time_grain="day",
    )
    parts_day, _, _ = _prior_partition_keys(b_day, "year")
    day_present = any("DAY" in p for p in parts_day)
    assert day_present, "Day key should be present at day grain"

    # Month grain: day-level key must be OMITTED
    b_month = VariantBinding(
        base_expression="SUM(amount)",
        fact_date_column="MIN(f.order_date)",
        calendar_type="standard",
        time_grain="month",
    )
    parts_month, _, _ = _prior_partition_keys(b_month, "year")
    day_present_month = any("DAY" in p for p in parts_month)
    assert not day_present_month, (
        "Day key should be omitted at month grain to prevent spurious NULLs"
    )


def test_prior_partition_keys_omit_month_in_quarter_at_quarter_grain():
    """Bug-6220: at quarter grain, the month-in-quarter position must be
    omitted from prior-period partitions for the quarter unit."""
    from shared.semantic.time_variants_sql import (
        VariantBinding,
        _prior_partition_keys,
    )

    # Quarter grain: month-in-quarter key must be OMITTED
    b_quarter = VariantBinding(
        base_expression="SUM(amount)",
        fact_date_column="MIN(f.order_date)",
        calendar_type="standard",
        time_grain="quarter",
    )
    parts, _, _ = _prior_partition_keys(b_quarter, "quarter")
    # At quarter grain, neither month-in-quarter nor day should appear
    assert len(parts) == 0, (
        f"No sub-period partition keys at quarter grain, got: {parts}"
    )


def test_misaligned_day_prior_year_month_grain_known_value():
    """Bug-6220: production month-grain prior-year keys omit MIN(date)'s day.

    Known fixture: February 2023 has its first transaction on day 1, while
    February 2024 has its first transaction on day 3. The prior-year value for
    February 2024 is still February 2023's 100. A day-position partition would
    return NULL.
    """
    from shared.semantic.time_variants_sql import (
        VariantBinding,
        _prior_partition_keys,
    )

    rows = [
        {"year": 2023, "month": 2, "day": 1, "value": 100},
        {"year": 2024, "month": 2, "day": 3, "value": 150},
    ]

    def partition_key(part_exprs: list[str], row: dict) -> tuple[int, ...]:
        key = []
        for expr in part_exprs:
            upper = expr.upper()
            if "DAY" in upper:
                key.append(row["day"])
            elif "MONTH" in upper:
                key.append(row["month"])
            else:
                raise AssertionError(f"Unexpected prior-year partition key: {expr}")
        return tuple(key)

    def prior_year_value(part_exprs: list[str]) -> int | None:
        by_partition: dict[tuple[int, ...], list[dict]] = {}
        for row in rows:
            key = partition_key(part_exprs, row)
            by_partition.setdefault(key, []).append(row)

        current = rows[1]
        partition = partition_key(part_exprs, current)
        peers = sorted(by_partition.get(partition, []), key=lambda r: r["year"])
        idx = peers.index(current)
        if idx == 0:
            return None
        return peers[idx - 1]["value"]

    b_day = VariantBinding(
        base_expression="SUM(amount)",
        fact_date_column="MIN(f.order_date)",
        calendar_type="standard",
        time_grain="day",
    )
    day_parts, _, _ = _prior_partition_keys(b_day, "year")
    assert any("DAY" in p.upper() for p in day_parts)
    assert prior_year_value(day_parts) is None

    b_month = VariantBinding(
        base_expression="SUM(amount)",
        fact_date_column="MIN(f.order_date)",
        calendar_type="standard",
        time_grain="month",
    )
    month_parts, _, _ = _prior_partition_keys(b_month, "year")
    assert not any("DAY" in p.upper() for p in month_parts)
    assert prior_year_value(month_parts) == 100


def test_semi_additive_period_to_date_wrong_number_fixture():
    """Bug-6222: cumulating a semi-additive balance produces a wrong number.

    Known fixture: month-end balances of 100, 110, 120 have a QTD balance of
    120 (last non-empty), not 330. The semantic guard must keep period-to-date
    variants off semi-additive measures.
    """
    balances = [100, 110, 120]
    wrong_cumulation = sum(balances)
    correct_last_non_empty = balances[-1]

    assert wrong_cumulation == 330
    assert correct_last_non_empty == 120
    assert _guard_fires("qtd", "last_non_empty")


@pytest.mark.asyncio
async def test_count_distinct_cumulation_wrong_number_fixture():
    """Bug-6229: real rewriter guard rejects the inflated cumulation case.

    Known fixture: day 1 has A/B and day 2 has B/C. Summing daily distinct
    counts gives 4, but the period distinct count is 3. The production rewriter
    must reject the count_distinct cumulation path before it can emit that
    inflated SUM-of-distinct-counts shape.
    """
    from src.ir.logical_query import SemanticBindingError
    from src.rewrite.query_rewriter import rewrite_for_source
    from test_dax_time_variants import _make_dax_bound_query, _variant_render_db

    daily_customers = [
        {"A", "B"},
        {"B", "C"},
    ]
    summed_daily_distinct = sum(len(day) for day in daily_customers)
    period_distinct = len(set().union(*daily_customers))

    assert summed_daily_distinct == 4
    assert period_distinct == 3

    bound = _make_dax_bound_query(
        measure_name="Distinct Customers",
        dim_name="Year",
        variant_hints={"Distinct Customers": "ytd"},
    )
    bound.resolved_measures[0].default_agg = "count_distinct"

    with pytest.raises(SemanticBindingError) as exc:
        await attach_fixture_deployed_shape(bound, _variant_render_db())
        await rewrite_for_source(bound, _variant_render_db())
    assert "count_distinct" in str(exc.value).lower()


# ---------------------------------------------------------------------------
# Pydantic schema validation
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _HAS_PYDANTIC, reason="shared.schemas import conflict")
def test_pydantic_by_account_is_rejected():
    """#10: ``by_account`` is no longer a supported behaviour, so authoring it via
    MeasureCreate is rejected as an invalid enum — with OR without an account
    column. (Previously it was accepted-with-column; now it is never authorable.)"""
    import uuid

    with pytest.raises(Exception):
        MeasureCreateSchema(
            name="balance",
            default_agg="sum",
            semi_additive_behavior="by_account",
        )
    with pytest.raises(Exception):
        MeasureCreateSchema(
            name="balance",
            default_agg="sum",
            semi_additive_behavior="by_account",
            semi_additive_account_column_id=uuid.uuid4(),
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
