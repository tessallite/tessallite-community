"""
Bug-6965 — Quoted semantic names containing spaces must be resolved to their
physical columns, not treated as raw passthrough.

Previously, ``SELECT "Net Revenue" FROM modelx`` set
``has_passthrough_expressions=True`` because the regex excluded whitespace
from the quoted identifier match.  The name was then preserved verbatim in
the source query instead of being mapped to its physical column.

Run from tessallite/services/query-router/:
    pytest tests/test_bug_6965_quoted_spaced_names.py -v
"""
from __future__ import annotations

import types
from unittest.mock import AsyncMock, patch

import pytest

from src.ir.logical_query import (
    LogicalQuery,
    SelectExpression,
)
from src.semantic.snapshot_resolver import DeployedShape


def _make_shape(dims=None, measures=None):
    """Build a DeployedShape from test fixtures (Bug-7979 fail-closed)."""
    return DeployedShape(
        measures=list(measures or []),
        dimensions=list(dims or []),
        hidden_column_ids=set(),
        physical_columns_all=set(),
        physical_columns_visible=set(),
        hierarchy_rows=[],
    )


@pytest.fixture
def _mock_model():
    return types.SimpleNamespace(
        id="model-1",
        slug="modelx",
        display_name="ModelX",
        deployed_version_id="v1",
    )


def _make_dim(name):
    return types.SimpleNamespace(
        id=f"d-{name}",
        name=name,
        source_column_id=f"sc-{name}",
    )


def _make_measure(name):
    return types.SimpleNamespace(
        id=f"m-{name}",
        name=name,
        default_agg="sum",
        is_additive=True,
        source_column_id=f"sc-{name}",
    )


class TestBug6965QuotedSpacedDimension:
    """A quoted dimension name with spaces must not trigger passthrough."""

    async def test_quoted_spaced_dimension_no_passthrough(self, _mock_model):
        """SELECT "Net Revenue" FROM modelx — the dimension has a space in its
        semantic name.  The binder must NOT set has_passthrough_expressions."""
        from src.semantic.binder import bind_query_to_model

        net_rev_dim = _make_dim("Net Revenue")

        query = LogicalQuery(
            model_id="model-1",
            protocol="jdbc",
            raw_query='SELECT "Net Revenue" FROM modelx',
            requested_measures=[],
            requested_dimensions=["Net Revenue"],
            filters=[],
            grain=[],
            order_by=[],
            limit=None,
            offset=None,
            query_fingerprint="fp-6965-1",
            from_tables=["modelx"],
            select_star=False,
            select_expressions=[
                SelectExpression(
                    raw_text='"Net Revenue"',
                    alias=None,
                    classification="passthrough",
                    agg_function=None,
                    inner_column="Net Revenue",
                    inner_literal=None,
                ),
            ],
        )

        db = AsyncMock()
        with (
            patch("src.semantic.binder._load_model", return_value=_mock_model),
            patch("src.semantic.binder.resolve_deployed_shape", return_value=_make_shape(dims=[net_rev_dim])),
        ):
            bound = await bind_query_to_model(query, db)
            assert bound is not None
            # The critical assertion: has_passthrough_expressions must be False
            # so the rewriter maps the semantic name to the physical column.
            assert bound.has_passthrough_expressions is False

    async def test_quoted_no_space_still_no_passthrough(self, _mock_model):
        """SELECT "revenue" FROM modelx — a quoted name without spaces must
        still not trigger passthrough (regression guard)."""
        from src.semantic.binder import bind_query_to_model

        rev_dim = _make_dim("revenue")

        query = LogicalQuery(
            model_id="model-1",
            protocol="jdbc",
            raw_query='SELECT "revenue" FROM modelx',
            requested_measures=[],
            requested_dimensions=["revenue"],
            filters=[],
            grain=[],
            order_by=[],
            limit=None,
            offset=None,
            query_fingerprint="fp-6965-2",
            from_tables=["modelx"],
            select_star=False,
            select_expressions=[
                SelectExpression(
                    raw_text='"revenue"',
                    alias=None,
                    classification="passthrough",
                    agg_function=None,
                    inner_column="revenue",
                    inner_literal=None,
                ),
            ],
        )

        db = AsyncMock()
        with (
            patch("src.semantic.binder._load_model", return_value=_mock_model),
            patch("src.semantic.binder.resolve_deployed_shape", return_value=_make_shape(dims=[rev_dim])),
        ):
            bound = await bind_query_to_model(query, db)
            assert bound.has_passthrough_expressions is False

    async def test_complex_expression_still_passthrough(self, _mock_model):
        """CASE WHEN "x" > 0 THEN 1 END — a complex expression must still
        trigger passthrough (it is not a bare quoted identifier)."""
        from src.semantic.binder import bind_query_to_model

        x_dim = _make_dim("x")

        query = LogicalQuery(
            model_id="model-1",
            protocol="jdbc",
            raw_query='SELECT CASE WHEN "x" > 0 THEN 1 END FROM modelx',
            requested_measures=[],
            requested_dimensions=["x"],
            filters=[],
            grain=[],
            order_by=[],
            limit=None,
            offset=None,
            query_fingerprint="fp-6965-3",
            from_tables=["modelx"],
            select_star=False,
            select_expressions=[
                SelectExpression(
                    raw_text='CASE WHEN "x" > 0 THEN 1 END',
                    alias=None,
                    classification="passthrough",
                    agg_function=None,
                    inner_column="x",
                    inner_literal=None,
                ),
            ],
        )

        db = AsyncMock()
        with (
            patch("src.semantic.binder._load_model", return_value=_mock_model),
            patch("src.semantic.binder.resolve_deployed_shape", return_value=_make_shape(dims=[x_dim])),
        ):
            bound = await bind_query_to_model(query, db)
            # Complex expression raw_text differs from inner_column — passthrough
            assert bound.has_passthrough_expressions is True


class TestBug6965BacktickQuoted:
    """BigQuery-style backtick-quoted identifiers with spaces."""

    async def test_backtick_spaced_name_no_passthrough(self, _mock_model):
        """SELECT `Order Amount` FROM modelx — backtick quoted with space."""
        from src.semantic.binder import bind_query_to_model

        dim = _make_dim("Order Amount")

        query = LogicalQuery(
            model_id="model-1",
            protocol="jdbc",
            raw_query='SELECT `Order Amount` FROM modelx',
            requested_measures=[],
            requested_dimensions=["Order Amount"],
            filters=[],
            grain=[],
            order_by=[],
            limit=None,
            offset=None,
            query_fingerprint="fp-6965-4",
            from_tables=["modelx"],
            select_star=False,
            select_expressions=[
                SelectExpression(
                    raw_text='`Order Amount`',
                    alias=None,
                    classification="passthrough",
                    agg_function=None,
                    inner_column="Order Amount",
                    inner_literal=None,
                ),
            ],
        )

        db = AsyncMock()
        with (
            patch("src.semantic.binder._load_model", return_value=_mock_model),
            patch("src.semantic.binder.resolve_deployed_shape", return_value=_make_shape(dims=[dim])),
        ):
            bound = await bind_query_to_model(query, db)
            assert bound.has_passthrough_expressions is False
