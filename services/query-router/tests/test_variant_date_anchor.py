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
