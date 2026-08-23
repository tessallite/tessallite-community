"""Bug-3607: variant date-anchor resolution.

The time-variant dispatch must anchor a period-aware measure on a real
DATE/TIMESTAMP column, never a derived numeric grain dimension (e.g.
``business_date_month`` = ``EXTRACT(MONTH FROM business_date)``, an INTEGER).
Anchoring on the numeric expression rendered ``EXTRACT(YEAR/QUARTER/... FROM
<numeric>)`` — a source-side ``extract(unknown, numeric) does not exist`` error.

These tests pin the pure resolver (``_resolve_variant_date_anchor`` /
``_dim_anchors_a_date``) that the dispatch sites call.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from src.ir.logical_query import SemanticBindingError
from src.rewrite.source_sql import (
    _dim_anchors_a_date,
    _resolve_variant_date_anchor,
)

pytestmark = pytest.mark.unit


@dataclass
class _Dim:
    name: str
    is_time_dim: bool = True
    source_column_id: object = None


@dataclass
class _Col:
    data_type: str


def _phys(name, *, pg_canonical=False):
    # Stand-in physical expression resolver: a date dim resolves to its
    # column, a derived dim resolves to its (numeric) expression.
    return {
        "business_date": "MIN(f.business_date)",
        "business_date_month": "EXTRACT(MONTH FROM f.business_date)",
        "order_date": "MIN(f.order_date)",
    }.get(name)


class TestDimAnchorsADate:
    def test_date_column_is_an_anchor(self) -> None:
        d = _Dim("business_date", source_column_id="c1")
        cols = {"c1": _Col(data_type="date")}
        assert _dim_anchors_a_date(d, cols) is True

    def test_timestamp_column_is_an_anchor(self) -> None:
        d = _Dim("ts", source_column_id="c1")
        cols = {"c1": _Col(data_type="TIMESTAMP")}
        assert _dim_anchors_a_date(d, cols) is True

    def test_derived_dim_with_no_source_column_is_not_an_anchor(self) -> None:
        # business_date_month is UDA-backed: source_column_id is None.
        d = _Dim("business_date_month", source_column_id=None)
        assert _dim_anchors_a_date(d, {}) is False

    def test_numeric_column_is_not_an_anchor(self) -> None:
        d = _Dim("month_no", source_column_id="c1")
        cols = {"c1": _Col(data_type="integer")}
        assert _dim_anchors_a_date(d, cols) is False

    # Intake 2026-07-07 (variant-date-anchor type recognition): PostgreSQL's
    # information_schema reports verbose spellings. The exact-token match
    # rejected them, so a genuine timestamp anchor fell through to a sibling
    # scan that picked an unrelated, unresolvable time dimension and raised
    # a spurious "Cannot resolve physical column" error.
    @pytest.mark.parametrize("dtype", [
        "timestamp without time zone",
        "timestamp with time zone",
        "TIMESTAMP WITHOUT TIME ZONE",
        "timestamp(3)",
        "timestamp(3) with time zone",
        "timestamptz",
        "datetime",
    ])
    def test_verbose_timestamp_spellings_are_anchors(self, dtype: str) -> None:
        d = _Dim("settlement_ts", source_column_id="c1")
        cols = {"c1": _Col(data_type=dtype)}
        assert _dim_anchors_a_date(d, cols) is True

    def test_time_with_time_zone_is_not_an_anchor(self) -> None:
        # Normalisation must not over-match: a bare TIME column carries no
        # date part and cannot anchor period math.
        d = _Dim("event_time", source_column_id="c1")
        cols = {"c1": _Col(data_type="time without time zone")}
        assert _dim_anchors_a_date(d, cols) is False


class TestResolveVariantDateAnchor:
    def test_date_time_dim_resolves_to_itself(self) -> None:
        d = _Dim("business_date", source_column_id="c1")
        cols = {"c1": _Col(data_type="date")}
        expr = _resolve_variant_date_anchor(
            time_dim=d,
            resolved_dimensions=[d],
            columns_by_id=cols,
            get_phys_expr=_phys,
            measure_name="revenue_ytd",
            pg_canonical=True,
        )
        # The resolved anchor must be the DATE column expression, with no
        # EXTRACT(... FROM <numeric>) wrapping.
        assert expr == "MIN(f.business_date)"

    def test_derived_dim_falls_back_to_sibling_date_dim(self) -> None:
        # Query grouped by the derived numeric month dim, but a sibling DATE
        # time dim is present in the grain — anchor on the sibling.
        derived = _Dim("business_date_month", source_column_id=None)
        date_dim = _Dim("business_date", source_column_id="c1")
        cols = {"c1": _Col(data_type="date")}
        expr = _resolve_variant_date_anchor(
            time_dim=derived,
            resolved_dimensions=[derived, date_dim],
            columns_by_id=cols,
            get_phys_expr=_phys,
            measure_name="revenue_ytd",
            pg_canonical=True,
        )
        assert expr == "MIN(f.business_date)"
        assert "EXTRACT(MONTH" not in expr

    def test_derived_dim_with_no_date_sibling_raises_typed_error(self) -> None:
        # Bug-3607 repro shape: grouped ONLY by the derived numeric month dim,
        # no DATE sibling in the grain. Must fail loud with a typed error
        # naming the offending dimension — never wrap a number in EXTRACT.
        derived = _Dim("business_date_month", source_column_id=None)
        with pytest.raises(SemanticBindingError) as exc:
            _resolve_variant_date_anchor(
                time_dim=derived,
                resolved_dimensions=[derived],
                columns_by_id={},
                get_phys_expr=_phys,
                measure_name="base_amount_ytd",
                pg_canonical=True,
            )
        msg = str(exc.value)
        assert "business_date_month" in msg
        assert "base_amount_ytd" in msg


@dataclass
class _ColT:
    """Column with a table id + name for window-anchor resolution."""
    data_type: str
    model_table_id: str
    column_name: str


class TestWindowVariantDateColumn:
    """F-015-01: a pure-window variant (lag / trailing_n / moving_avg_n) orders
    by the modeller-selected ``date_dimension_column_id``, NOT the
    hierarchy-derived ``resolved_date_col_id``. Selecting a different date
    column MUST change the resolved ORDER BY anchor, and a misconfigured
    selection MUST fail loud rather than silently order by another date."""

    def test_window_uses_selected_date_column(self) -> None:
        time_dim = _Dim("some_grain", source_column_id=None)
        cols = {
            "ship": _ColT("date", "t1", "ship_date"),
            "order": _ColT("date", "t1", "order_date"),
        }
        alias_by_table = {"t1": "f"}
        expr = _resolve_variant_date_anchor(
            time_dim=time_dim,
            resolved_dimensions=[time_dim],
            columns_by_id=cols,
            get_phys_expr=_phys,
            measure_name="revenue_trailing_3",
            pg_canonical=True,
            resolved_date_col_id="order",  # hierarchy-derived — must be ignored
            alias_by_table_id=alias_by_table,
            is_window=True,
            window_anchor_col_id="ship",
        )
        assert expr == '"f"."ship_date"'

    def test_changing_selected_date_column_changes_anchor(self) -> None:
        """The whole point of F-015-01: a different selection => different
        anchor column (silent-wrong-date guard)."""
        time_dim = _Dim("some_grain", source_column_id=None)
        cols = {
            "ship": _ColT("date", "t1", "ship_date"),
            "order": _ColT("date", "t1", "order_date"),
        }
        alias_by_table = {"t1": "f"}
        base_kwargs = dict(
            time_dim=time_dim,
            resolved_dimensions=[time_dim],
            columns_by_id=cols,
            get_phys_expr=_phys,
            measure_name="rev_trailing",
            pg_canonical=True,
            resolved_date_col_id="order",
            alias_by_table_id=alias_by_table,
            is_window=True,
        )
        anchor_ship = _resolve_variant_date_anchor(
            **base_kwargs, window_anchor_col_id="ship"
        )
        anchor_order = _resolve_variant_date_anchor(
            **base_kwargs, window_anchor_col_id="order"
        )
        assert anchor_ship == '"f"."ship_date"'
        assert anchor_order == '"f"."order_date"'
        assert anchor_ship != anchor_order

    def test_window_ignores_hierarchy_resolved_date_col(self) -> None:
        """Even when resolved_date_col_id points at a valid date column, a
        window variant must NOT use it — the selected column wins."""
        time_dim = _Dim("some_grain", source_column_id=None)
        cols = {
            "ship": _ColT("timestamp", "t1", "ship_ts"),
            "order": _ColT("date", "t1", "order_date"),
        }
        expr = _resolve_variant_date_anchor(
            time_dim=time_dim,
            resolved_dimensions=[time_dim],
            columns_by_id=cols,
            get_phys_expr=_phys,
            measure_name="rev_lag",
            pg_canonical=True,
            resolved_date_col_id="order",
            alias_by_table_id={"t1": "f"},
            is_window=True,
            window_anchor_col_id="ship",
        )
        assert expr == '"f"."ship_ts"'

    def test_window_selected_column_wrong_type_fails_loud(self) -> None:
        time_dim = _Dim("some_grain", source_column_id=None)
        cols = {"bad": _ColT("integer", "t1", "region_id")}
        with pytest.raises(SemanticBindingError) as exc:
            _resolve_variant_date_anchor(
                time_dim=time_dim,
                resolved_dimensions=[time_dim],
                columns_by_id=cols,
                get_phys_expr=_phys,
                measure_name="rev_trailing",
                pg_canonical=True,
                alias_by_table_id={"t1": "f"},
                is_window=True,
                window_anchor_col_id="bad",
            )
        assert "not a DATE/TIMESTAMP" in str(exc.value)

    def test_window_selected_column_missing_fails_loud(self) -> None:
        time_dim = _Dim("some_grain", source_column_id=None)
        with pytest.raises(SemanticBindingError) as exc:
            _resolve_variant_date_anchor(
                time_dim=time_dim,
                resolved_dimensions=[time_dim],
                columns_by_id={},
                get_phys_expr=_phys,
                measure_name="rev_trailing",
                pg_canonical=True,
                alias_by_table_id={"t1": "f"},
                is_window=True,
                window_anchor_col_id="ghost",
            )
        assert "not present in the model snapshot" in str(exc.value)
