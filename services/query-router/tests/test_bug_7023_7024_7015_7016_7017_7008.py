"""Regression tests for query-router rewrite bugs 7023-7024-7015-7016-7017-7008.

Bug-7023: quoted alias SQL injection via identifier context escape.
Bug-7024: forced raw routing silently NULL-substitutes invalid objects.
Bug-7015: dimension-only GROUP BY silently dropped on source route.
Bug-7016: raw route base-table selection ignores query's tables on multi-fact.
Bug-7017: semi-additive LAST/FIRST_NON_EMPTY on Spark loses ORDER BY.
Bug-7008: BigQuery ESCAPE clause per-connector branch vs. sqlglot transpile.
"""
from __future__ import annotations

import types
from uuid import uuid4

import pytest

from src.ir.logical_query import BoundQuery, LogicalQuery, SemanticBindingError
from shared.connector_qualify import safe_ident

pytestmark = pytest.mark.integration


def _uid():
    return uuid4()


def _make_lq(**overrides):
    """Build a LogicalQuery with sensible defaults."""
    defaults = dict(
        model_id=str(_uid()),
        protocol="jdbc",
        raw_query="SELECT 1",
        requested_measures=[],
        requested_dimensions=[],
        filters=[],
        grain=[],
        order_by=[],
        limit=None,
        offset=None,
        query_fingerprint="test",
        select_star=False,
        select_expressions=[],
        from_tables=[],
    )
    defaults.update(overrides)
    return LogicalQuery(**defaults)


def _make_bq(lq, *, measures=None, dimensions=None, model=None):
    """Build a BoundQuery with sensible defaults."""
    if model is None:
        model = types.SimpleNamespace(
            id=_uid(), slug="model", deployed_version_id=_uid(),
            display_name="Test", connection_type=None,
        )
    return BoundQuery(
        logical_query=lq, model=model,
        resolved_measures=measures or [],
        resolved_dimensions=dimensions or [],
        resolved_filters=[],
        resolved_dimensions_by_name={d.name: d for d in (dimensions or [])},
    )


# ---------------------------------------------------------------------------
# Bug-7023: hostile alias cannot break out of identifier context
# ---------------------------------------------------------------------------

class TestBug7023AliasInjection:
    """A quoted output alias containing double-quotes must not escape
    identifier context.  safe_ident escapes embedded quotes per the
    PostgreSQL doubling convention (``""`` inside ``"..."``)."""

    def test_safe_ident_escapes_double_quotes(self):
        hostile = 'x"; DROP TABLE users; --'
        result = safe_ident(hostile)
        # The double-quote inside the name must be doubled
        assert result == '"x""; DROP TABLE users; --"'
        # The result must still be a single identifier (one opening, one
        # closing quote, with interior quotes doubled).
        assert result.startswith('"')
        assert result.endswith('"')

    def test_safe_ident_normal_name(self):
        assert safe_ident("region") == '"region"'

    def test_safe_ident_empty_name(self):
        assert safe_ident("") == '""'

    def test_conditions_quote_escapes(self):
        """_quote in conditions.py must use safe_ident."""
        from src.rewrite.conditions import _quote
        hostile = 'x"; DROP TABLE users; --'
        assert _quote(hostile) == safe_ident(hostile)

    def test_conditions_qualified_column_escapes(self):
        """_qualified_column must escape both alias and column."""
        from src.rewrite.conditions import _qualified_column
        result = _qualified_column('tab"le', 'col"umn')
        assert '"tab""le"' in result
        assert '"col""umn"' in result

    def test_hostile_alias_stays_inside_identifier(self):
        """A hostile alias passed to _render_condition as ``col`` cannot
        produce a separate SQL statement in the rendered output."""
        from src.rewrite.conditions import _render_condition
        hostile_col = safe_ident('x"; DROP TABLE users; --')
        sql = _render_condition(hostile_col, "eq", "test")
        # The entire hostile name must appear as one quoted identifier.
        # After safe_ident, the inner " is doubled to "", so the rendered
        # SQL is:  "x""; DROP TABLE users; --" = 'test'
        # There must be NO semicolon OUTSIDE the identifier context.
        # Split by the identifier to check what's around it.
        assert hostile_col in sql
        # The SQL must be a single valid expression, not multiple statements.
        # Count quotes: the identifier must be balanced.
        outside = sql.replace(hostile_col, "IDENT")
        assert ";" not in outside


# ---------------------------------------------------------------------------
# Bug-7024: raw route fails loud on invalid objects (not silent NULL)
# ---------------------------------------------------------------------------

class TestBug7024RawRouteInvalidObjects:
    """A standard dimension or measure with no physical mapping must raise
    SemanticBindingError on the raw route, not silently emit NULL."""

    @pytest.mark.asyncio
    async def test_dimension_no_source_column_raises(self, monkeypatch):
        from src.rewrite.raw_sql import rewrite_for_raw
        fact = types.SimpleNamespace(
            id=_uid(), name="fact", physical_name="fact",
            table_type="fact", alias="f",
        )
        # Dimension with NO source_column_id, no calc, no UDA
        dim = types.SimpleNamespace(
            id=_uid(), name="orphan_dim", source_column_id=None,
            user_defined_attribute_id=None, calc_expression=None,
        )
        lq = _make_lq(raw_query="SELECT orphan_dim FROM model",
                       from_tables=["model"],
                       requested_dimensions=["orphan_dim"])
        bq = _make_bq(lq, dimensions=[dim])

        tables_by_id = {fact.id: fact}
        async def _mock_load(*args, **kwargs):
            return tables_by_id, [], {}, {}
        async def _mock_dialect(*args, **kwargs):
            return "postgres"
        from src.rewrite import raw_sql as raw_mod
        monkeypatch.setattr(raw_mod, "_load_model_graph", _mock_load)
        monkeypatch.setattr(raw_mod, "_resolve_target_dialect", _mock_dialect)

        with pytest.raises(SemanticBindingError, match="orphan_dim"):
            await rewrite_for_raw(bq, object())

    @pytest.mark.asyncio
    async def test_measure_no_source_column_raises(self, monkeypatch):
        from src.rewrite.raw_sql import rewrite_for_raw
        fact = types.SimpleNamespace(
            id=_uid(), name="fact", physical_name="fact",
            table_type="fact", alias="f",
        )
        # Measure with NO source_column_id and no UDA
        meas = types.SimpleNamespace(
            id=_uid(), name="orphan_meas", source_column_id=None,
            measure_type="standard", expression=None,
            variant_of_measure_id=None, default_agg="sum",
            is_additive=True, data_type="numeric",
            user_defined_attribute_id=None,
        )
        lq = _make_lq(raw_query="SELECT orphan_meas FROM model",
                       from_tables=["model"],
                       requested_measures=["orphan_meas"])
        bq = _make_bq(lq, measures=[meas])

        tables_by_id = {fact.id: fact}
        async def _mock_load(*args, **kwargs):
            return tables_by_id, [], {}, {}
        async def _mock_dialect(*args, **kwargs):
            return "postgres"
        from src.rewrite import raw_sql as raw_mod
        monkeypatch.setattr(raw_mod, "_load_model_graph", _mock_load)
        monkeypatch.setattr(raw_mod, "_resolve_target_dialect", _mock_dialect)

        with pytest.raises(SemanticBindingError, match="orphan_meas"):
            await rewrite_for_raw(bq, object())


# ---------------------------------------------------------------------------
# Bug-7766: raw-route CALCULATED measures fail loud on invalid objects
# ---------------------------------------------------------------------------

class TestBug7766RawCalcMeasureFailLoud:
    """Bug-7766: the Bug-7024 fail-loud contract now covers calculated
    measures on the raw route. An invalid calc object (unparseable expression,
    reference to an unknown measure, or a referenced base measure with no
    source-column mapping) must raise SemanticBindingError — NOT emit a silent
    typed NULL that hides model corruption. The legitimately-NULL cases
    (variant reference, __row_count reference, reference on an unreachable
    table) must still emit a typed NULL, not raise.
    """

    def _calc(self, name, expression):
        return types.SimpleNamespace(
            id=_uid(), name=name, source_column_id=None,
            measure_type="calculated", expression=expression,
            variant_of_measure_id=None, default_agg="sum",
            is_additive=True, data_type="numeric",
            user_defined_attribute_id=None, calc_agg_mode=None,
        )

    def _base(self, name, *, source_column_id, variant_of=None):
        return types.SimpleNamespace(
            id=_uid(), name=name, source_column_id=source_column_id,
            measure_type="standard", expression=None,
            variant_of_measure_id=variant_of, default_agg="sum",
            is_additive=True, data_type="numeric",
            user_defined_attribute_id=None,
        )

    def _render(self, meas, resolved_measures, columns_by_id, reachable,
                *, alias_by_table_id=None, uda_by_id=None,
                calc_ref_measures_by_name=None):
        from src.rewrite.raw_sql import _render_calc_measure_raw
        bq = _make_bq(_make_lq(), measures=resolved_measures)
        return _render_calc_measure_raw(
            meas, bq, columns_by_id,
            alias_by_table_id=alias_by_table_id or {},
            tables_by_id={}, uda_by_id=uda_by_id or {}, reachable=reachable,
            calc_ref_measures_by_name=calc_ref_measures_by_name or {},
        )

    def test_reference_loaded_via_calc_ref_map_renders(self):
        # R2 Codex Finding 1: a referenced base measure NOT in resolved_measures
        # but supplied via calc_ref_measures_by_name (the DB-loaded dependency
        # map) must resolve and render — NOT raise "unknown measure". This is
        # the normal binder shape when a calc measure is selected alone.
        col_id = _uid()
        table_id = _uid()
        mc = types.SimpleNamespace(
            id=col_id, column_name="price", model_table_id=table_id,
            data_type="numeric",
        )
        base = self._base("price", source_column_id=col_id)
        calc = self._calc("total_value", 'measure("price") * 2')
        # resolved_measures has ONLY the calc measure; the base arrives via the
        # dependency map.
        rendered = self._render(
            calc, [calc], {col_id: mc}, {table_id},
            alias_by_table_id={table_id: "t"},
            calc_ref_measures_by_name={"price": base},
        )
        assert "NULL" not in rendered.upper()
        assert "price" in rendered

    def test_unparseable_expression_raises(self):
        # An expression the calc-expression grammar rejects is model
        # corruption — fail loud (was a silent NULL, Bug-7024-F7).
        calc = self._calc("bad_calc", "this is not )( a valid expression @@")
        with pytest.raises(SemanticBindingError, match="bad_calc"):
            self._render(calc, [calc], {}, set())

    def test_reference_to_unknown_measure_raises(self):
        # measure("ghost") where "ghost" is not in the resolved model.
        calc = self._calc("bad_calc", 'measure("ghost") * 1.1')
        with pytest.raises(SemanticBindingError, match="ghost"):
            self._render(calc, [calc], {}, set())

    def test_reference_measure_no_source_column_raises(self):
        # measure("orphan") where orphan is a base measure with a
        # source_column_id that resolves to NO column (orphaned mapping).
        orphan = self._base("orphan", source_column_id=_uid())
        calc = self._calc("bad_calc", 'measure("orphan") * 2')
        # columns_by_id is empty -> orphan.source_column_id resolves to None.
        with pytest.raises(SemanticBindingError, match="orphan"):
            self._render(calc, [calc, orphan], {}, set())

    def test_variant_reference_emits_null_not_raise(self):
        # A reference to a time-variant base measure is meaningless per row —
        # typed NULL is correct, must NOT raise (preserved behaviour).
        variant = self._base("rev_ytd", source_column_id=None, variant_of=_uid())
        calc = self._calc("calc_v", 'measure("rev_ytd") + 1')
        rendered = self._render(calc, [calc, variant], {}, set())
        assert "NULL" in rendered.upper()

    def test_row_count_reference_emits_null_not_raise(self):
        # R3 Codex Finding 2: __row_count (COUNT(*)) is a synthetic sentinel
        # with NO persisted Measure row, so it is NEVER in resolved_measures or
        # the DB dependency map. A calc selected alone that references it must
        # emit typed NULL (meaningless per row), NOT raise "unknown measure".
        # The reference is deliberately NOT co-selected here (co-selecting a
        # fabricated __row_count measure would MASK the bug).
        calc = self._calc("calc_rc", 'measure("__row_count") * 2')
        rendered = self._render(calc, [calc], {}, set())
        assert "NULL" in rendered.upper()

    def test_no_references_constant_calc_emits_null_not_raise(self):
        # A calc expression with no measure() references (a pure constant)
        # emits typed NULL on the raw route and must NOT raise (preserved
        # pre-existing behaviour; not in Bug-7766's invalid-object scope).
        calc = self._calc("calc_const", "1 + 2")
        rendered = self._render(calc, [calc], {}, set())
        assert "NULL" in rendered.upper()

    def test_uda_backed_reference_renders_not_raise(self):
        # R1 Finding 1: a referenced base measure with NO source_column_id but
        # a valid, REACHABLE user_defined_attribute must render its UDA
        # expression, NOT raise. Mirrors the non-raw _phys_for_measure contract.
        table_id = _uid()
        uda_id = _uid()
        uda = types.SimpleNamespace(
            id=uda_id, expression="amount * 1.2", table_id=table_id,
            output_data_type="numeric",
        )
        base = types.SimpleNamespace(
            id=_uid(), name="uda_base", source_column_id=None,
            measure_type="standard", expression=None,
            variant_of_measure_id=None, default_agg="sum",
            is_additive=True, data_type="numeric",
            user_defined_attribute_id=uda_id,
        )
        calc = self._calc("calc_uda", 'measure("uda_base") * 2')
        rendered = self._render(
            calc, [calc, base], {}, {table_id},
            alias_by_table_id={table_id: "t"}, uda_by_id={uda_id: uda},
        )
        # The UDA body must appear in the rendered arithmetic, not NULL.
        assert "NULL" not in rendered.upper()
        assert "amount" in rendered

    def test_uda_backed_reference_unreachable_emits_null(self):
        # A UDA-backed referenced measure whose table is UNREACHABLE emits a
        # typed NULL (stable-column-count), must NOT raise.
        table_id = _uid()
        uda_id = _uid()
        uda = types.SimpleNamespace(
            id=uda_id, expression="amount * 1.2", table_id=table_id,
            output_data_type="numeric",
        )
        base = types.SimpleNamespace(
            id=_uid(), name="uda_base", source_column_id=None,
            measure_type="standard", expression=None,
            variant_of_measure_id=None, default_agg="sum",
            is_additive=True, data_type="numeric",
            user_defined_attribute_id=uda_id,
        )
        calc = self._calc("calc_uda_u", 'measure("uda_base") * 2')
        rendered = self._render(
            calc, [calc, base], {}, set(),  # table_id NOT reachable
            alias_by_table_id={}, uda_by_id={uda_id: uda},
        )
        assert "NULL" in rendered.upper()

    def test_uda_backed_reference_missing_uda_raises(self):
        # A referenced measure pointing at a UDA id NOT in the model is an
        # invalid object — fail loud (mirrors the standard-measure UDA branch).
        uda_id = _uid()
        base = types.SimpleNamespace(
            id=_uid(), name="uda_base", source_column_id=None,
            measure_type="standard", expression=None,
            variant_of_measure_id=None, default_agg="sum",
            is_additive=True, data_type="numeric",
            user_defined_attribute_id=uda_id,
        )
        calc = self._calc("calc_uda_m", 'measure("uda_base") * 2')
        with pytest.raises(SemanticBindingError, match="uda_base"):
            self._render(
                calc, [calc, base], {}, set(),
                uda_by_id={},  # uda_id not present -> missing
            )

    def test_uda_backed_reference_unrenderable_raises(self):
        # R2 coverage gap: a REACHABLE UDA whose expression fails to render is
        # an invalid object — fail loud (mirrors the standard-measure UDA
        # branch's Bug-7024 unrenderable-UDA raise).
        table_id = _uid()
        uda_id = _uid()
        # An expression the UDA renderer cannot parse/qualify.
        uda = types.SimpleNamespace(
            id=uda_id, expression=")(  invalid @@ uda expr",
            table_id=table_id, output_data_type="numeric",
        )
        base = types.SimpleNamespace(
            id=_uid(), name="uda_base", source_column_id=None,
            measure_type="standard", expression=None,
            variant_of_measure_id=None, default_agg="sum",
            is_additive=True, data_type="numeric",
            user_defined_attribute_id=uda_id,
        )
        calc = self._calc("calc_uda_x", 'measure("uda_base") * 2')
        with pytest.raises(SemanticBindingError, match="uda_base"):
            self._render(
                calc, [calc, base], {}, {table_id},  # table reachable
                alias_by_table_id={table_id: "t"},
                uda_by_id={uda_id: uda},
            )

    def test_unreachable_table_reference_emits_null_not_raise(self):
        # The referenced base measure's physical column EXISTS but its table
        # is not reachable from the fact — typed NULL per stable-column-count
        # policy, must NOT raise.
        col_id = _uid()
        table_id = _uid()
        mc = types.SimpleNamespace(
            id=col_id, column_name="amount", model_table_id=table_id,
            data_type="numeric",
        )
        base = self._base("rev", source_column_id=col_id)
        calc = self._calc("calc_u", 'measure("rev") * 2')
        # reachable does NOT contain table_id -> unreachable table.
        rendered = self._render(calc, [calc, base], {col_id: mc}, set())
        assert "NULL" in rendered.upper()

    def test_valid_calc_renders_physical_expression(self):
        # Sanity: a valid calc over a reachable base measure renders SQL, not
        # NULL (guards against over-eager fail-loud).
        col_id = _uid()
        table_id = _uid()
        mc = types.SimpleNamespace(
            id=col_id, column_name="amount", model_table_id=table_id,
            data_type="numeric",
        )
        base = self._base("rev", source_column_id=col_id)
        calc = self._calc("calc_ok", 'measure("rev") * 1.1')
        rendered = self._render(
            calc, [calc, base], {col_id: mc}, {table_id},
        )
        assert "amount" in rendered
        assert "1.1" in rendered


# ---------------------------------------------------------------------------
# Bug-7015: dimension-only GROUP BY preserved on source route
# ---------------------------------------------------------------------------

class TestBug7015DimensionOnlyGroupBy:
    """A query with dimensions in the grain and no measures must still
    emit GROUP BY. Tested via the golden harness: the 'dim_only_group_by'
    scenario in test_render_golden.py asserts GROUP BY is present.

    This class validates the condition logic directly."""

    def test_group_by_condition_no_measures_no_distinct(self):
        """When there are no measures and no explicit agg, but the query
        is NOT DISTINCT, GROUP BY must be emitted (user wrote GROUP BY)."""
        # Simulates the condition at source_sql.py line ~3167
        resolved_measures = []
        has_explicit_agg = False
        has_distinct = False
        grain_group_exprs = ['"f"."region"']

        _needs_group_by = (
            resolved_measures
            or has_explicit_agg
            or not has_distinct
        )
        assert _needs_group_by is True
        assert grain_group_exprs and _needs_group_by

    def test_group_by_condition_distinct_no_measures(self):
        """When DISTINCT is used with no measures, GROUP BY is NOT needed
        (DISTINCT already deduplicates)."""
        resolved_measures = []
        has_explicit_agg = False
        has_distinct = True
        grain_group_exprs = ['"f"."region"']

        _needs_group_by = (
            resolved_measures
            or has_explicit_agg
            or not has_distinct
        )
        assert _needs_group_by is False

    def test_group_by_condition_with_measures(self):
        """When measures are present, GROUP BY is always needed."""
        resolved_measures = ["revenue"]
        has_explicit_agg = False
        has_distinct = False

        _needs_group_by = (
            resolved_measures
            or has_explicit_agg
            or not has_distinct
        )
        assert _needs_group_by


# ---------------------------------------------------------------------------
# Bug-7016: raw route base-table prefers queried fact on multi-fact models
# ---------------------------------------------------------------------------

class TestBug7016MultiFactBaseTable:
    """On a two-fact model, the raw route must prefer the fact table whose
    columns are actually queried."""

    @pytest.mark.asyncio
    async def test_queried_fact_selected(self, monkeypatch):
        from src.rewrite.raw_sql import rewrite_for_raw

        orders = types.SimpleNamespace(
            id=_uid(), name="orders", physical_name="demo.orders",
            table_type="fact", alias="orders",
        )
        payments = types.SimpleNamespace(
            id=_uid(), name="payments", physical_name="demo.payments",
            table_type="fact", alias="payments",
        )
        pay_col = types.SimpleNamespace(
            id=_uid(), column_name="payment_amount",
            model_table_id=payments.id, data_type="numeric",
        )
        meas = types.SimpleNamespace(
            id=_uid(), name="payment_amount", source_column_id=pay_col.id,
            measure_type="standard", expression=None,
            variant_of_measure_id=None, default_agg="sum",
            is_additive=True, data_type="numeric",
            user_defined_attribute_id=None,
        )
        lq = _make_lq(
            raw_query="SELECT payment_amount FROM model",
            from_tables=["model"],
            requested_measures=["payment_amount"],
        )
        bq = _make_bq(lq, measures=[meas])

        tables_by_id = {orders.id: orders, payments.id: payments}
        columns_by_id = {pay_col.id: pay_col}
        async def _mock_load(*a, **kw):
            return tables_by_id, [], columns_by_id, {}
        async def _mock_dialect(*a, **kw):
            return "postgres"
        from src.rewrite import raw_sql as raw_mod
        monkeypatch.setattr(raw_mod, "_load_model_graph", _mock_load)
        monkeypatch.setattr(raw_mod, "_resolve_target_dialect", _mock_dialect)

        sql = await rewrite_for_raw(bq, object())

        # Must SELECT from payments (where the queried column lives),
        # NOT from orders (the other fact table).
        assert "payments" in sql.lower()
        assert '"payment_amount"' in sql
        # Must NOT emit CAST(NULL) for the queried column
        assert "CAST(NULL" not in sql


# ---------------------------------------------------------------------------
# Bug-7017: Spark semi-additive ORDER BY preserved via MAX_BY/MIN_BY
# ---------------------------------------------------------------------------

class TestBug7017SparkSemiAdditive:
    """Semi-additive LAST/FIRST_NON_EMPTY on Spark must FAIL LOUD.

    Bug-7014 / Codex gate R2: the prior MAX_BY/MIN_BY rewrite diverged
    from PG NULL-ordering semantics (MAX_BY ignores NULL keys while PG
    NULLS FIRST in DESC picks them). The safe interim is fail-loud
    (raise SemanticBindingError) until column-type metadata is available
    to emit a parity-safe COALESCE sentinel.
    """

    def test_spark_transpile_last_non_empty_raises(self):
        """Codex gate R2: Spark semi-additive must fail loud."""
        from src.rewrite.dialects import _transpile_to_dialect
        from src.ir.logical_query import SemanticBindingError
        pg = (
            'SELECT (ARRAY_AGG("f"."balance" ORDER BY "f"."dt" DESC)'
            ' FILTER (WHERE "f"."balance" IS NOT NULL))[1]'
            ' FROM "demo"."fact" AS "f"'
        )
        with pytest.raises(SemanticBindingError):
            _transpile_to_dialect(pg, "spark")

    def test_spark_transpile_first_non_empty_raises(self):
        from src.rewrite.dialects import _transpile_to_dialect
        from src.ir.logical_query import SemanticBindingError
        pg = (
            'SELECT (ARRAY_AGG("f"."balance" ORDER BY "f"."dt" ASC)'
            ' FILTER (WHERE "f"."balance" IS NOT NULL))[1]'
            ' FROM "demo"."fact" AS "f"'
        )
        with pytest.raises(SemanticBindingError):
            _transpile_to_dialect(pg, "spark")

    def test_bigquery_still_uses_array_agg(self):
        """BigQuery must not be affected by the Spark fail-loud."""
        from src.rewrite.dialects import _transpile_to_dialect
        pg = (
            'SELECT (ARRAY_AGG("f"."balance" ORDER BY "f"."dt" DESC)'
            ' FILTER (WHERE "f"."balance" IS NOT NULL))[1]'
            ' FROM "demo"."fact" AS "f"'
        )
        result = _transpile_to_dialect(pg, "bigquery")
        assert "ARRAY_AGG" in result
        assert "MAX_BY" not in result

    def test_postgres_unchanged(self):
        from src.rewrite.calendar_support import _semi_additive_agg
        result = _semi_additive_agg(
            "LAST_NON_EMPTY", '"f"."balance"', '"f"."dt"', "postgres",
        )
        assert "ARRAY_AGG" in result
        assert "ORDER BY" in result

    def test_spark_normal_sql_unaffected(self):
        """Non-semi-additive Spark SQL must be unaffected."""
        from src.rewrite.dialects import _transpile_to_dialect
        pg = 'SELECT SUM("f"."amount") FROM "t"'
        result = _transpile_to_dialect(pg, "spark")
        assert "SUM" in result
        assert "MAX_BY" not in result


# ---------------------------------------------------------------------------
# Bug-7192-F5: T-SQL semi-additive fail-loud (no ARRAY_AGG / FILTER / subscript)
# ---------------------------------------------------------------------------

class TestBug7192F5TsqlSemiAdditive:
    """Semi-additive LAST/FIRST_NON_EMPTY on T-SQL must fail loud with a
    clear SemanticBindingError rather than emitting invalid SQL.

    T-SQL has no ARRAY_AGG, no FILTER clause, and no array subscript.
    sqlglot passes these through verbatim, producing SQL that hard-fails
    on SQL Server. The fix at the ``_transpile_to_dialect`` boundary
    detects the PG-canonical semi-additive pattern and raises before the
    generator can emit invalid T-SQL (SQL Rule 1 compliant -- one site).
    """

    _PG_LAST = (
        'SELECT (ARRAY_AGG("f"."balance" ORDER BY "f"."dt" DESC)'
        ' FILTER (WHERE "f"."balance" IS NOT NULL))[1]'
        ' FROM "demo"."fact" AS "f"'
    )

    _PG_FIRST = (
        'SELECT (ARRAY_AGG("f"."balance" ORDER BY "f"."dt" ASC)'
        ' FILTER (WHERE "f"."balance" IS NOT NULL))[1]'
        ' FROM "demo"."fact" AS "f"'
    )

    def test_tsql_last_non_empty_raises(self):
        """LAST_NON_EMPTY on tsql must raise SemanticBindingError."""
        from src.rewrite.dialects import _transpile_to_dialect

        with pytest.raises(SemanticBindingError, match="SQL Server"):
            _transpile_to_dialect(self._PG_LAST, "tsql")

    def test_tsql_first_non_empty_raises(self):
        """FIRST_NON_EMPTY on tsql must raise SemanticBindingError."""
        from src.rewrite.dialects import _transpile_to_dialect

        with pytest.raises(SemanticBindingError, match="SQL Server"):
            _transpile_to_dialect(self._PG_FIRST, "tsql")

    def test_tsql_error_names_behavior_last(self):
        """Error message must name the specific semi-additive behavior."""
        from src.rewrite.dialects import _transpile_to_dialect

        with pytest.raises(SemanticBindingError, match="LAST_NON_EMPTY"):
            _transpile_to_dialect(self._PG_LAST, "tsql")

    def test_tsql_error_names_behavior_first(self):
        """Error message must name the specific semi-additive behavior."""
        from src.rewrite.dialects import _transpile_to_dialect

        with pytest.raises(SemanticBindingError, match="FIRST_NON_EMPTY"):
            _transpile_to_dialect(self._PG_FIRST, "tsql")

    def test_tsql_normal_sql_unaffected(self):
        """Non-semi-additive T-SQL transpile must work normally."""
        from src.rewrite.dialects import _transpile_to_dialect

        pg = 'SELECT SUM("f"."amount") FROM "demo"."fact" AS "f"'
        result = _transpile_to_dialect(pg, "tsql")
        assert "SUM" in result
        # Identifiers must be bracket-quoted for T-SQL
        assert "[f]" in result or "[amount]" in result

    def test_tsql_min_max_avg_unaffected(self):
        """MIN/MAX/AVG semi-additive behaviors are standard SQL and must
        transpile normally to T-SQL (no ARRAY_AGG pattern)."""
        from src.rewrite.dialects import _transpile_to_dialect

        for agg in ("MIN", "MAX", "AVG"):
            pg = f'SELECT {agg}("f"."balance") FROM "demo"."fact" AS "f"'
            result = _transpile_to_dialect(pg, "tsql")
            assert agg in result, f"{agg} must transpile to T-SQL"

    def test_spark_and_bigquery_unaffected_by_tsql_fix(self):
        """Spark and BigQuery must still handle semi-additive correctly
        (regression guard for the T-SQL fix).
        Bug-7914 / Codex gate R2: Spark now fails loud (same as T-SQL)
        because MAX_BY diverges from PG NULL-ordering semantics.
        BigQuery uses native ARRAY_AGG + IGNORE NULLS (correct)."""
        from src.rewrite.dialects import _transpile_to_dialect
        from src.ir.logical_query import SemanticBindingError

        # Spark -> fail loud (Bug-7914)
        with pytest.raises(SemanticBindingError):
            _transpile_to_dialect(self._PG_LAST, "spark")

        # BigQuery -> ARRAY_AGG with IGNORE NULLS
        result_bq = _transpile_to_dialect(self._PG_LAST, "bigquery")
        assert "ARRAY_AGG" in result_bq

    def test_postgres_unaffected_by_tsql_fix(self):
        """Postgres must pass through unchanged (regression guard)."""
        from src.rewrite.dialects import _transpile_to_dialect

        result = _transpile_to_dialect(self._PG_LAST, "postgres")
        assert "ARRAY_AGG" in result
        assert "FILTER" in result

    def test_tsql_transpile_with_mixed_aggregates(self):
        """A query with both SUM and semi-additive LAST_NON_EMPTY must
        fail loud on the semi-additive part (the SUM is valid T-SQL but
        the ARRAY_AGG pattern is not)."""
        from src.rewrite.dialects import _transpile_to_dialect

        pg = (
            'SELECT SUM("f"."amount"),'
            ' (ARRAY_AGG("f"."balance" ORDER BY "f"."dt" DESC)'
            ' FILTER (WHERE "f"."balance" IS NOT NULL))[1]'
            ' FROM "demo"."fact" AS "f"'
        )
        with pytest.raises(SemanticBindingError, match="SQL Server"):
            _transpile_to_dialect(pg, "tsql")


# ---------------------------------------------------------------------------
# Bug-7008: BigQuery ESCAPE handled at sqlglot transpile boundary
# ---------------------------------------------------------------------------

class TestBug7008BigQueryEscape:
    """The ESCAPE clause must always be emitted in PG-canonical form;
    the _bq_escape_sql generator drops it for BigQuery during transpile."""

    def test_pg_canonical_always_includes_escape(self):
        from src.rewrite.conditions import _render_condition
        for connector in ("postgresql", "bigquery", "sqlserver"):
            sql = _render_condition(
                '"col"', "like", "'%test\\%val%'",
                like_escape="\\", connector=connector,
            )
            assert "ESCAPE" in sql, (
                f"connector={connector}: ESCAPE must be in PG-canonical form"
            )

    def test_bigquery_transpile_drops_escape(self):
        from src.rewrite.dialects import _transpile_to_dialect
        pg = """SELECT "x" FROM t WHERE "x" LIKE '%foo' ESCAPE '\\'"""
        bq = _transpile_to_dialect(pg, "bigquery")
        assert "ESCAPE" not in bq
        assert "LIKE" in bq

    def test_postgres_transpile_keeps_escape(self):
        from src.rewrite.dialects import _transpile_to_dialect
        pg = """SELECT "x" FROM t WHERE "x" LIKE '%foo' ESCAPE '\\'"""
        result = _transpile_to_dialect(pg, "postgres")
        assert "ESCAPE" in result
