"""Tests for resolved_date_col_id read path wiring (Bug-5247).

Verifies that _resolve_variant_date_anchor uses Measure.resolved_date_col_id
as the primary resolution path when it is set and points to a valid
DATE/TIMESTAMP column, falling back to the dimension-scan heuristic only
when it is absent or invalid.
"""
from __future__ import annotations

import types
import uuid

import pytest

from src.ir.logical_query import SemanticBindingError
from src.rewrite.source_sql import _resolve_variant_date_anchor


def _make_model_column(*, col_id=None, table_id=None, column_name="order_date", data_type="DATE"):
    return types.SimpleNamespace(
        id=col_id or uuid.uuid4(),
        model_table_id=table_id or uuid.uuid4(),
        column_name=column_name,
        data_type=data_type,
    )


def _make_dim(name, *, is_time_dim=False, source_column_id=None, hierarchy_id=None):
    return types.SimpleNamespace(
        name=name,
        is_time_dim=is_time_dim,
        source_column_id=source_column_id or uuid.uuid4(),
        hierarchy_id=hierarchy_id,
    )


class TestResolvedDateColIdReadPath:
    """Bug-5247: resolved_date_col_id wiring tests."""

    def test_resolved_date_col_id_used_when_present(self):
        """When resolved_date_col_id points to a DATE column in the graph,
        it should be used directly without falling back to dimension scan."""
        date_col_id = uuid.uuid4()
        table_id = uuid.uuid4()
        mc = _make_model_column(col_id=date_col_id, table_id=table_id, data_type="DATE")
        columns_by_id = {date_col_id: mc}
        alias_by_table_id = {table_id: "base"}

        # The time_dim is numeric (not date-anchored) — without
        # resolved_date_col_id, this would require a sibling scan.
        time_dim = _make_dim("order_month", is_time_dim=True)
        numeric_col = _make_model_column(
            col_id=time_dim.source_column_id,
            data_type="INTEGER",
        )
        columns_by_id[time_dim.source_column_id] = numeric_col

        result = _resolve_variant_date_anchor(
            time_dim=time_dim,
            resolved_dimensions=[time_dim],
            columns_by_id=columns_by_id,
            get_phys_expr=lambda name, pg_canonical=False: None,
            measure_name="yoy_revenue",
            pg_canonical=True,
            resolved_date_col_id=date_col_id,
            alias_by_table_id=alias_by_table_id,
        )

        assert result == '"base"."order_date"'

    def test_falls_back_when_resolved_date_col_id_not_in_graph(self):
        """When resolved_date_col_id is not found in columns_by_id,
        the function falls back to the dimension-scan heuristic."""
        missing_col_id = uuid.uuid4()
        table_id = uuid.uuid4()

        # Set up a date-anchored time dim that the fallback will find.
        date_col_id = uuid.uuid4()
        date_col = _make_model_column(col_id=date_col_id, table_id=table_id, data_type="DATE")
        time_dim = _make_dim("order_date", is_time_dim=True, source_column_id=date_col_id)
        columns_by_id = {date_col_id: date_col}

        def _phys(name, pg_canonical=False):
            if name == "order_date":
                return '"base"."order_date"'
            return None

        result = _resolve_variant_date_anchor(
            time_dim=time_dim,
            resolved_dimensions=[time_dim],
            columns_by_id=columns_by_id,
            get_phys_expr=_phys,
            measure_name="yoy_revenue",
            pg_canonical=True,
            resolved_date_col_id=missing_col_id,
            alias_by_table_id={table_id: "base"},
        )
        assert result == '"base"."order_date"'

    def test_falls_back_when_resolved_date_col_not_date_type(self):
        """When resolved_date_col_id points to a non-date column,
        it is ignored and the fallback is used."""
        non_date_col_id = uuid.uuid4()
        table_id = uuid.uuid4()
        mc = _make_model_column(col_id=non_date_col_id, table_id=table_id, data_type="VARCHAR")
        columns_by_id = {non_date_col_id: mc}

        # Provide a date-anchored time dim for fallback.
        date_col_id = uuid.uuid4()
        date_col = _make_model_column(col_id=date_col_id, table_id=table_id, data_type="TIMESTAMP")
        time_dim = _make_dim("order_date", is_time_dim=True, source_column_id=date_col_id)
        columns_by_id[date_col_id] = date_col

        def _phys(name, pg_canonical=False):
            if name == "order_date":
                return '"base"."order_date"'
            return None

        result = _resolve_variant_date_anchor(
            time_dim=time_dim,
            resolved_dimensions=[time_dim],
            columns_by_id=columns_by_id,
            get_phys_expr=_phys,
            measure_name="yoy_revenue",
            pg_canonical=True,
            resolved_date_col_id=non_date_col_id,
            alias_by_table_id={table_id: "base"},
        )
        assert result == '"base"."order_date"'

    def test_no_resolved_date_col_id_uses_dimension_scan(self):
        """When resolved_date_col_id is None, the original dimension-scan
        heuristic is used (backward compat)."""
        table_id = uuid.uuid4()
        date_col_id = uuid.uuid4()
        date_col = _make_model_column(col_id=date_col_id, table_id=table_id, data_type="DATE")
        time_dim = _make_dim("order_date", is_time_dim=True, source_column_id=date_col_id)
        columns_by_id = {date_col_id: date_col}

        def _phys(name, pg_canonical=False):
            if name == "order_date":
                return '"base"."order_date"'
            return None

        result = _resolve_variant_date_anchor(
            time_dim=time_dim,
            resolved_dimensions=[time_dim],
            columns_by_id=columns_by_id,
            get_phys_expr=_phys,
            measure_name="yoy_revenue",
            pg_canonical=True,
            # resolved_date_col_id omitted (defaults to None)
        )
        assert result == '"base"."order_date"'

    def test_timestamp_type_accepted(self):
        """TIMESTAMP columns should also be accepted as date anchors."""
        col_id = uuid.uuid4()
        table_id = uuid.uuid4()
        mc = _make_model_column(col_id=col_id, table_id=table_id, data_type="TIMESTAMP")
        columns_by_id = {col_id: mc}

        time_dim = _make_dim("order_month", is_time_dim=True)
        numeric_col = _make_model_column(
            col_id=time_dim.source_column_id,
            data_type="INTEGER",
        )
        columns_by_id[time_dim.source_column_id] = numeric_col

        result = _resolve_variant_date_anchor(
            time_dim=time_dim,
            resolved_dimensions=[time_dim],
            columns_by_id=columns_by_id,
            get_phys_expr=lambda name, pg_canonical=False: None,
            measure_name="yoy_revenue",
            pg_canonical=True,
            resolved_date_col_id=col_id,
            alias_by_table_id={table_id: "fact"},
        )
        assert result == '"fact"."order_date"'


class TestResolvedDateColIdProducerConsumerContract:
    """Bug-1490: lock the producer/consumer alignment for resolved_date_col_id.

    model-service (``measures.py::_resolve_variant_calendar_snapshot``) WRITES
    the column id of the alias-table ModelColumn whose name matches the
    CalendarTable's ``date_column``; query-router (``_resolve_variant_date_anchor``)
    READS it back and accepts it only when its physical ``data_type`` belongs to
    the date/timestamp family. These tests assert the read contract so the two
    ends cannot drift apart silently.
    """

    def test_read_contract_type_family_is_connector_qualify_dates(self):
        """The read path's accepted anchor types must be exactly the
        connector_qualify date + timestamp families (the single source of
        truth shared with the join-coercion layer). If either side narrows
        this set independently, the denormalized snapshot would stop being
        honoured (silent fall-through to the heuristic)."""
        from src.rewrite.source_sql import _VARIANT_DATE_ANCHOR_TYPES
        from shared.connector_qualify import _DATE_TYPES, _TIMESTAMP_TYPES

        assert _VARIANT_DATE_ANCHOR_TYPES == (_DATE_TYPES | _TIMESTAMP_TYPES)
        # Sanity: the canonical names the model-service backfill produces.
        assert "DATE" in _VARIANT_DATE_ANCHOR_TYPES
        assert "TIMESTAMP" in _VARIANT_DATE_ANCHOR_TYPES

    def test_denormalized_shortcut_short_circuits_before_get_phys_expr(self):
        """When resolved_date_col_id resolves to a valid anchor, the read path
        must NOT invoke the dimension-scan heuristic at all (get_phys_expr is
        the single-hop FK read the architecture doc promises). Proven by making
        get_phys_expr raise if it is ever called."""
        date_col_id = uuid.uuid4()
        table_id = uuid.uuid4()
        mc = _make_model_column(col_id=date_col_id, table_id=table_id, data_type="DATE")
        columns_by_id = {date_col_id: mc}
        time_dim = _make_dim("order_month", is_time_dim=True)

        def _boom(name, pg_canonical=False):  # pragma: no cover - must not run
            raise AssertionError("dimension-scan heuristic was invoked despite a valid snapshot")

        result = _resolve_variant_date_anchor(
            time_dim=time_dim,
            resolved_dimensions=[time_dim],
            columns_by_id=columns_by_id,
            get_phys_expr=_boom,
            measure_name="yoy_revenue",
            pg_canonical=True,
            resolved_date_col_id=date_col_id,
            alias_by_table_id={table_id: "base"},
        )
        assert result == '"base"."order_date"'

    def test_missing_alias_map_falls_back_not_crashes(self):
        """The denormalized shortcut requires alias_by_table_id; when it is
        absent the read path must fall back to the heuristic rather than
        crash, so an older caller that omits the alias map stays correct."""
        date_col_id = uuid.uuid4()
        table_id = uuid.uuid4()
        date_col = _make_model_column(col_id=date_col_id, table_id=table_id, data_type="DATE")
        time_dim = _make_dim("order_date", is_time_dim=True, source_column_id=date_col_id)
        columns_by_id = {date_col_id: date_col}

        def _phys(name, pg_canonical=False):
            return '"base"."order_date"' if name == "order_date" else None

        result = _resolve_variant_date_anchor(
            time_dim=time_dim,
            resolved_dimensions=[time_dim],
            columns_by_id=columns_by_id,
            get_phys_expr=_phys,
            measure_name="yoy_revenue",
            pg_canonical=True,
            resolved_date_col_id=date_col_id,
            alias_by_table_id=None,
        )
        assert result == '"base"."order_date"'
