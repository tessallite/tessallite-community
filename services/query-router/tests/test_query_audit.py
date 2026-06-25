"""
Tests for the pre-execute and post-execute security guardrails.

Layer 1 — audit_filters_present: every resolved filter must appear in the
          rewritten SQL's WHERE clause.
Layer 2 — audit_result_columns: every result column must be in the binder's
          authorised set.
"""
from __future__ import annotations

import types
from dataclasses import dataclass, field
from typing import Any, Optional

import pytest

from src.ir.logical_query import BoundQuery, LogicalFilter, LogicalQuery
from src.security.query_audit import (
    SecurityAuditError,
    audit_filters_present,
    audit_result_columns,
    build_anchor_keys,
    resolve_filter_anchors,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _lq(
    *,
    filters: list[LogicalFilter] | None = None,
    has_complex_sql: bool = False,
    has_unresolvable_where: bool = False,
    select_star: bool = False,
    select_expressions: list | None = None,
    raw_query: str = "SELECT 1",
) -> LogicalQuery:
    lq = LogicalQuery(
        model_id="m-1",
        protocol="jdbc",
        raw_query=raw_query,
        requested_measures=[],
        requested_dimensions=[],
        filters=filters or [],
        grain=[],
        order_by=[],
        limit=None,
        offset=None,
        query_fingerprint="fp",
        select_star=select_star,
    )
    lq.has_complex_sql = has_complex_sql
    lq.has_unresolvable_where = has_unresolvable_where
    if select_expressions:
        lq.select_expressions = select_expressions
    return lq


def _bound(
    *,
    filters: list[LogicalFilter] | None = None,
    dims: list[str] | None = None,
    measures: list[str] | None = None,
    has_passthrough: bool = False,
    lq_kwargs: dict | None = None,
) -> BoundQuery:
    lq_kw = lq_kwargs or {}
    model = types.SimpleNamespace(id="m-1", slug="test")
    resolved_dims = [
        types.SimpleNamespace(id=f"d-{n}", name=n) for n in (dims or [])
    ]
    resolved_meas = [
        types.SimpleNamespace(
            id=f"m-{n}", name=n, default_agg="sum",
        )
        for n in (measures or [])
    ]
    return BoundQuery(
        logical_query=_lq(filters=filters, **lq_kw),
        model=model,
        resolved_measures=resolved_meas,
        resolved_dimensions=resolved_dims,
        resolved_filters=filters or [],
        has_passthrough_expressions=has_passthrough,
    )


def _anchors(spec: dict[str, str | None]) -> dict[str, set[str]]:
    """Anchor map for tests: dimension name → derivation expression (or
    None for name-only anchors).  Mirrors what ``resolve_filter_anchors``
    produces from model metadata in production."""
    out: dict[str, set[str]] = {}
    for name, expr in spec.items():
        out[name.lower()] = build_anchor_keys(
            column_names=[name],
            expressions=[expr] if expr else [],
        )
    return out


# The acme-demo derivation shapes (UDA expressions as stored in the model).
_DEMO_ANCHORS = _anchors({
    "business_date_month": 'EXTRACT(MONTH FROM ("business_date"))',
    "business_date_year": 'EXTRACT(YEAR FROM ("business_date"))',
    "business_date_day": 'EXTRACT(DAY FROM ("business_date"))',
    "posting_date": "CAST(posting_ts AS DATE)",
    "posting_date_year": "EXTRACT(YEAR FROM CAST(posting_ts AS DATE))",
    "country_group": "UPPER(country_code)",
    "sec_region": None,
    "country_code": None,
})


# ===========================================================================
# Layer 1 — audit_filters_present
# ===========================================================================

class TestAuditFiltersPresent:

    def test_no_filters_passes(self):
        bq = _bound(filters=[])
        audit_filters_present(bq, "SELECT 1", "source")

    def test_filter_present_in_where(self):
        bq = _bound(filters=[LogicalFilter("city_name", "eq", "Cairo")])
        sql = (
            'SELECT `city_name` FROM `t` AS `t` '
            "WHERE `t`.`city_name` = 'Cairo'"
        )
        audit_filters_present(bq, sql, "source")

    def test_filter_missing_from_where_raises(self):
        bq = _bound(filters=[LogicalFilter("city_name", "eq", "Cairo")])
        sql = "SELECT `city_name` FROM `t` AS `t`"
        with pytest.raises(SecurityAuditError, match="no WHERE clause"):
            audit_filters_present(bq, sql, "source")

    def test_filter_column_absent_in_where_raises(self):
        bq = _bound(filters=[
            LogicalFilter("city_name", "eq", "Cairo"),
            LogicalFilter("region", "eq", "EMEA"),
        ])
        sql = (
            "SELECT `city_name` FROM `t` "
            "WHERE `t`.`city_name` = 'Cairo'"
        )
        with pytest.raises(SecurityAuditError, match="region"):
            audit_filters_present(bq, sql, "source")

    def test_multiple_filters_all_present(self):
        bq = _bound(filters=[
            LogicalFilter("city_name", "eq", "Cairo"),
            LogicalFilter("account_type", "eq", "Credit"),
        ])
        sql = (
            "SELECT 1 FROM t "
            "WHERE t.city_name = 'Cairo' AND t.account_type = 'Credit'"
        )
        audit_filters_present(bq, sql, "source")

    def test_row_security_wrapped_sql(self):
        bq = _bound(filters=[
            LogicalFilter("city_name", "eq", "Cairo"),
            LogicalFilter("sec_region", "eq", "EMEA"),
        ])
        sql = (
            "SELECT * FROM ("
            "SELECT city_name, COUNT(*) FROM t "
            "WHERE t.city_name = 'Cairo'"
            ") __rls WHERE sec_region = 'EMEA'"
        )
        audit_filters_present(bq, sql, "source")

    def test_uda_expression_filter_matched_by_anchor(self):
        """Round-3: a UDA-derived filter passes via the ANCHORED AST check
        (LHS = the dimension's derivation expression), not by value alone."""
        bq = _bound(filters=[
            LogicalFilter("posting_date_year", "eq", 2025),
        ])
        sql = (
            "SELECT 1 FROM t "
            "WHERE (EXTRACT(YEAR FROM CAST(posting_ts AS DATE))) = 2025"
        )
        audit_filters_present(bq, sql, "source", filter_anchors=_DEMO_ANCHORS)

    def test_uda_expression_filter_without_anchor_blocks(self):
        """Round-3 fail-closed: without an anchor for the derived dimension,
        a bare value match must NOT prove presence — the audit blocks."""
        bq = _bound(filters=[
            LogicalFilter("posting_date_year", "eq", 2025),
        ])
        sql = (
            "SELECT 1 FROM t "
            "WHERE (EXTRACT(YEAR FROM CAST(posting_ts AS DATE))) = 2025"
        )
        with pytest.raises(SecurityAuditError, match="posting_date_year"):
            audit_filters_present(bq, sql, "source", filter_anchors={})

    def test_complex_sql_skips_audit(self):
        bq = _bound(
            filters=[LogicalFilter("missing_col", "eq", "X")],
            lq_kwargs={"has_complex_sql": True},
        )
        audit_filters_present(bq, "SELECT 1", "source")

    def test_passthrough_skips_audit(self):
        bq = _bound(
            filters=[LogicalFilter("missing_col", "eq", "X")],
            has_passthrough=True,
        )
        audit_filters_present(bq, "SELECT 1", "source")

    def test_aggregate_route_filter_present(self):
        bq = _bound(filters=[LogicalFilter("city_name", "eq", "Cairo")])
        sql = (
            'SELECT "city_name", SUM("revenue__sum") FROM agg_table '
            "WHERE \"city_name\" = 'Cairo' GROUP BY \"city_name\""
        )
        audit_filters_present(bq, sql, "aggregate")

    def test_case_insensitive_match(self):
        bq = _bound(filters=[LogicalFilter("City_Name", "eq", "Cairo")])
        sql = "SELECT 1 FROM t WHERE t.city_name = 'Cairo'"
        audit_filters_present(bq, sql, "source")

    def test_in_filter_present(self):
        bq = _bound(filters=[
            LogicalFilter("status", "in", ["active", "pending"]),
        ])
        sql = "SELECT 1 FROM t WHERE status IN ('active', 'pending')"
        audit_filters_present(bq, sql, "source")

    def test_bare_object_bound_query_skips(self):
        audit_filters_present(object(), "SELECT 1", "source")

    def test_unresolvable_where_conjunct_drop_raises(self):
        """Double-quoted values cause Column=Column comparisons that
        the parser can't extract. The rewriter may drop them silently.
        The guardrail must detect the conjunct count mismatch."""
        raw = (
            "SELECT city_name FROM t "
            "WHERE city_name = \"Cairo\" AND account_type = \"CREDIT\" "
            "AND channel_name = 'Web' AND aml_flag = \"X\" "
            "GROUP BY city_name"
        )
        bq = _bound(
            filters=[LogicalFilter("channel_name", "eq", "Web")],
            lq_kwargs={
                "has_unresolvable_where": True,
                "raw_query": raw,
            },
        )
        rewritten = (
            "SELECT `t`.`city_name` FROM `t` "
            "WHERE `t`.`channel_name` = 'Web' "
            "GROUP BY `t`.`city_name`"
        )
        with pytest.raises(SecurityAuditError, match="4.*WHERE.*1"):
            audit_filters_present(bq, rewritten, "source")

    def test_unresolvable_where_no_filters_conjunct_drop_raises(self):
        """When NO filters are extractable (all double-quoted), the
        guardrail must still detect the conjunct count mismatch."""
        raw = (
            "SELECT city_name FROM t "
            "WHERE city_name = \"Cairo\" AND account_type = \"CREDIT\" "
            "GROUP BY city_name"
        )
        bq = _bound(
            filters=[],
            lq_kwargs={
                "has_unresolvable_where": True,
                "raw_query": raw,
            },
        )
        rewritten = "SELECT `t`.`city_name` FROM `t` GROUP BY `t`.`city_name`"
        with pytest.raises(SecurityAuditError, match="2.*WHERE.*0"):
            audit_filters_present(bq, rewritten, "source")

    def test_unresolvable_where_all_preserved_passes(self):
        """When raw-WHERE preservation works correctly, all conjuncts
        survive and the guardrail passes."""
        raw = (
            "SELECT city_name FROM t "
            "WHERE city_name = \"Cairo\" AND channel_name = 'Web' "
            "GROUP BY city_name"
        )
        bq = _bound(
            filters=[LogicalFilter("channel_name", "eq", "Web")],
            lq_kwargs={
                "has_unresolvable_where": True,
                "raw_query": raw,
            },
        )
        rewritten = (
            "SELECT `t`.`city_name` FROM `t` "
            "WHERE `t`.`city_name` = `Cairo` AND `t`.`channel_name` = 'Web' "
            "GROUP BY `t`.`city_name`"
        )
        audit_filters_present(bq, rewritten, "source")


# ===========================================================================
# Bug-1045 — derived-dimension filters whose identifier the rewrite
# legitimately replaced (filter-only shapes carry no alias).  The audit
# must prove presence via the AST predicate check (operator class +
# element-wise values) and still block genuine drops fail-closed.
# ===========================================================================

# The exact live shape from the adjudication: filter-only derived dim,
# identifier expanded to EXTRACT(...), string values coerced to numerics.
_DERIVED_IN_SQL = (
    'SELECT "country_code", SUM("transaction_amount") '
    'FROM "payment_transaction" '
    'WHERE (EXTRACT(MONTH FROM ("payment_transaction"."business_date"))) IN (4, 5) '
    'GROUP BY "country_code"'
)


class TestAuditFiltersDerivedDims:

    # --- the Excel keep-only / timeline shapes that must PASS -------------

    def test_in_filter_only_derived_dim_passes(self):
        """Excel multi-member keep-only: month IN ('4','5'), dim NOT
        projected — was blocked by the str(value) substring fallback."""
        bq = _bound(filters=[
            LogicalFilter("business_date_month", "in", ["4", "5"]),
        ])
        audit_filters_present(bq, _DERIVED_IN_SQL, "source", filter_anchors=_DEMO_ANCHORS)

    def test_in_filter_combined_with_eq_year_passes(self):
        """The reviewer's exact probe: month IN ('4','5') AND year = '2025'."""
        bq = _bound(filters=[
            LogicalFilter("business_date_month", "in", ["4", "5"]),
            LogicalFilter("business_date_year", "eq", "2025"),
        ])
        sql = (
            'SELECT "country_code", SUM("transaction_amount") '
            'FROM "payment_transaction" '
            'WHERE (EXTRACT(MONTH FROM ("payment_transaction"."business_date"))) IN (4, 5) '
            'AND (EXTRACT(YEAR FROM ("payment_transaction"."business_date"))) = 2025 '
            'GROUP BY "country_code"'
        )
        audit_filters_present(bq, sql, "source", filter_anchors=_DEMO_ANCHORS)

    def test_not_in_filter_only_derived_dim_passes(self):
        bq = _bound(filters=[
            LogicalFilter("business_date_month", "not_in", ["4", "5"]),
        ])
        sql = (
            "SELECT SUM(amount) FROM t "
            "WHERE EXTRACT(MONTH FROM t.business_date) NOT IN (4, 5)"
        )
        audit_filters_present(bq, sql, "source", filter_anchors=_DEMO_ANCHORS)

    def test_between_filter_only_derived_dim_passes(self):
        """Timeline slicer shape: BETWEEN on a non-projected derived dim."""
        bq = _bound(filters=[
            LogicalFilter("business_date_month", "between", ("4", "5")),
        ])
        sql = (
            "SELECT SUM(amount) FROM t "
            "WHERE EXTRACT(MONTH FROM t.business_date) BETWEEN 4 AND 5"
        )
        audit_filters_present(bq, sql, "source", filter_anchors=_DEMO_ANCHORS)

    def test_between_date_strings_with_casts_passes(self):
        bq = _bound(filters=[
            LogicalFilter("posting_date", "between", ("2025-01-01", "2025-03-31")),
        ])
        sql = (
            "SELECT SUM(amount) FROM t "
            "WHERE CAST(t.posting_ts AS DATE) "
            "BETWEEN CAST('2025-01-01' AS DATE) AND CAST('2025-03-31' AS DATE)"
        )
        audit_filters_present(bq, sql, "source", filter_anchors=_DEMO_ANCHORS)

    def test_range_operators_filter_only_derived_dim_pass(self):
        for op, sql_op in (("gt", ">"), ("gte", ">="), ("lt", "<"), ("lte", "<=")):
            bq = _bound(filters=[
                LogicalFilter("business_date_year", op, "2024"),
            ])
            sql = (
                "SELECT SUM(amount) FROM t "
                f"WHERE EXTRACT(YEAR FROM t.business_date) {sql_op} 2024"
            )
            audit_filters_present(bq, sql, "source", filter_anchors=_DEMO_ANCHORS)

    def test_in_filter_string_members_passes(self):
        """String IN-list members keep their text form (case-insensitive)."""
        bq = _bound(filters=[
            LogicalFilter("country_group", "in", ["US", "DE"]),
        ])
        sql = (
            "SELECT SUM(amount) FROM t "
            "WHERE UPPER(t.country_code) IN ('us', 'de')"
        )
        audit_filters_present(bq, sql, "source", filter_anchors=_DEMO_ANCHORS)

    def test_projected_derived_dim_alias_still_passes(self):
        """Projected variant: since round 4 the AS alias alone no longer
        suffices (projection is not filtering) — the query passes because
        the anchored WHERE predicate proves the filter via the AST check."""
        bq = _bound(filters=[
            LogicalFilter("business_date_month", "in", ["4", "5"]),
        ])
        sql = (
            'SELECT EXTRACT(MONTH FROM t.business_date) AS "business_date_month", '
            "SUM(amount) FROM t "
            "WHERE EXTRACT(MONTH FROM t.business_date) IN (4, 5) "
            'GROUP BY "business_date_month"'
        )
        audit_filters_present(bq, sql, "source", filter_anchors=_DEMO_ANCHORS)

    def test_physical_column_in_filter_still_passes(self):
        """Physical columns keep passing — since round 4 via the anchored
        AST check (name anchor matches the column LHS, exact values)."""
        bq = _bound(filters=[
            LogicalFilter("country_code", "in", ["US", "DE"]),
        ])
        sql = "SELECT SUM(amount) FROM t WHERE t.country_code IN ('US', 'DE')"
        audit_filters_present(bq, sql, "source", filter_anchors=_DEMO_ANCHORS)

    def test_persona_default_filter_on_derived_dim_passes(self):
        """Persona / RLS default filters merge into resolved_filters and
        are audited through the same machinery."""
        bq = _bound(filters=[
            LogicalFilter("business_date_year", "eq", "2025"),   # persona default
            LogicalFilter("sec_region", "eq", "EMEA"),           # RLS predicate
        ])
        sql = (
            "SELECT * FROM ("
            "SELECT SUM(amount) FROM t "
            "WHERE EXTRACT(YEAR FROM t.business_date) = 2025"
            ") __rls WHERE sec_region = 'EMEA'"
        )
        audit_filters_present(bq, sql, "source", filter_anchors=_DEMO_ANCHORS)

    def test_is_not_null_filter_only_derived_dim_passes(self):
        bq = _bound(filters=[
            LogicalFilter("business_date_month", "is_not_null", None),
        ])
        sql = (
            "SELECT SUM(amount) FROM t "
            "WHERE EXTRACT(MONTH FROM t.business_date) IS NOT NULL"
        )
        audit_filters_present(bq, sql, "source", filter_anchors=_DEMO_ANCHORS)

    # --- genuine drops that must still BLOCK (fail-closed proof) ----------

    def test_dropped_in_filter_still_blocks(self):
        """A genuinely dropped IN filter has no predicate carrying its
        values — the audit must still block."""
        bq = _bound(filters=[
            LogicalFilter("business_date_month", "in", ["4", "5"]),
            LogicalFilter("country_code", "eq", "US"),
        ])
        sql = "SELECT SUM(amount) FROM t WHERE t.country_code = 'US'"
        with pytest.raises(SecurityAuditError, match="business_date_month"):
            audit_filters_present(bq, sql, "source", filter_anchors=_DEMO_ANCHORS)

    def test_dropped_between_filter_still_blocks(self):
        bq = _bound(filters=[
            LogicalFilter("business_date_month", "between", ("4", "5")),
            LogicalFilter("country_code", "eq", "US"),
        ])
        sql = "SELECT SUM(amount) FROM t WHERE t.country_code = 'US'"
        with pytest.raises(SecurityAuditError, match="business_date_month"):
            audit_filters_present(bq, sql, "source", filter_anchors=_DEMO_ANCHORS)

    def test_predicate_with_wrong_values_blocks(self):
        """An IN predicate exists but does not cover the filter's members
        — presence is not proven, so the audit blocks."""
        bq = _bound(filters=[
            LogicalFilter("business_date_month", "in", ["4", "5"]),
        ])
        sql = (
            "SELECT SUM(amount) FROM t "
            "WHERE EXTRACT(MONTH FROM t.business_date) IN (1, 2)"
        )
        with pytest.raises(SecurityAuditError, match="business_date_month"):
            audit_filters_present(bq, sql, "source", filter_anchors=_DEMO_ANCHORS)

    def test_values_in_select_list_do_not_satisfy_audit(self):
        """Literals outside WHERE/HAVING must not count — only predicate
        scopes are searched."""
        bq = _bound(filters=[
            LogicalFilter("business_date_month", "in", ["4", "5"]),
        ])
        sql = "SELECT 4, 5, SUM(amount) FROM t WHERE t.country_code = 'US'"
        with pytest.raises(SecurityAuditError, match="business_date_month"):
            audit_filters_present(bq, sql, "source", filter_anchors=_DEMO_ANCHORS)

    def test_is_null_polarity_mismatch_blocks(self):
        """An IS NOT NULL node must not satisfy an is_null filter."""
        bq = _bound(filters=[
            LogicalFilter("business_date_month", "is_null", None),
        ])
        sql = (
            "SELECT SUM(amount) FROM t "
            "WHERE EXTRACT(MONTH FROM t.business_date) IS NOT NULL"
        )
        with pytest.raises(SecurityAuditError, match="business_date_month"):
            audit_filters_present(bq, sql, "source", filter_anchors=_DEMO_ANCHORS)

    def test_operator_class_mismatch_blocks(self):
        """A BETWEEN filter is not satisfied by an unrelated equality node
        even when one bound's literal appears."""
        bq = _bound(filters=[
            LogicalFilter("business_date_month", "between", ("4", "5")),
        ])
        sql = (
            "SELECT SUM(amount) FROM t "
            "WHERE EXTRACT(MONTH FROM t.business_date) = 4"
        )
        with pytest.raises(SecurityAuditError, match="business_date_month"):
            audit_filters_present(bq, sql, "source", filter_anchors=_DEMO_ANCHORS)


# ===========================================================================
# Bug-1045 round 3 — dimension ANCHORING (B10 deep review round 2, HIGH).
# A predicate carrying the filter's exact values on a DIFFERENT column /
# expression must never satisfy the filter.  These are the reviewer's
# adversarial value-collision probes (A1/A3/C1/C3/D1/E1/F1) plus the
# scalar-EQ, subquery-scope and CASE-expression collisions — permanent
# regression tests for the wrong-pass class.
# ===========================================================================

class TestAuditFilterAnchoring:

    # --- the 7 round-2 adversarial probes: right values, wrong dimension --

    def test_a1_sibling_dim_value_collision_blocks(self):
        """A1: dropped day IN [4,5] must not be rescued by the KEPT
        month IN (4,5) predicate carrying the same values."""
        bq = _bound(filters=[
            LogicalFilter("business_date_day", "in", ["4", "5"]),
            LogicalFilter("business_date_month", "in", ["4", "5"]),
        ])
        sql = (
            "SELECT SUM(amount) FROM t "
            "WHERE EXTRACT(MONTH FROM t.business_date) IN (4, 5)"
        )
        with pytest.raises(SecurityAuditError, match="business_date_day"):
            audit_filters_present(bq, sql, "source", filter_anchors=_DEMO_ANCHORS)

    def test_a3_rls_wrapper_value_collision_blocks(self):
        """A3: dropped month IN [4,5] must not be rescued by a row-security
        wrapper predicate on sec_region carrying ('4','5')."""
        bq = _bound(filters=[
            LogicalFilter("business_date_month", "in", ["4", "5"]),
        ])
        sql = (
            "SELECT * FROM (SELECT SUM(amount) FROM t) __rls "
            "WHERE sec_region IN ('4', '5')"
        )
        with pytest.raises(SecurityAuditError, match="business_date_month"):
            audit_filters_present(bq, sql, "source", filter_anchors=_DEMO_ANCHORS)

    def test_c1_case_expression_literals_block(self):
        """C1: literals inside a CASE expression are not a filter predicate."""
        bq = _bound(filters=[
            LogicalFilter("business_date_month", "in", ["4", "5"]),
        ])
        sql = (
            "SELECT SUM(amount) FROM t "
            "WHERE t.flag = CASE WHEN t.y = 4 THEN 5 END"
        )
        with pytest.raises(SecurityAuditError, match="business_date_month"):
            audit_filters_present(bq, sql, "source", filter_anchors=_DEMO_ANCHORS)

    def test_c3_arithmetic_literals_block(self):
        """C3: literals inside arithmetic (a = b + 4 - 5) are not a filter."""
        bq = _bound(filters=[
            LogicalFilter("business_date_month", "in", ["4", "5"]),
        ])
        sql = "SELECT SUM(amount) FROM t WHERE t.a = t.b + 4 - 5"
        with pytest.raises(SecurityAuditError, match="business_date_month"):
            audit_filters_present(bq, sql, "source", filter_anchors=_DEMO_ANCHORS)

    def test_d1_exists_subquery_value_collision_blocks(self):
        """D1: a value-colliding IN inside an EXISTS subquery does not
        constrain the outer rows."""
        bq = _bound(filters=[
            LogicalFilter("business_date_month", "in", ["4", "5"]),
        ])
        sql = (
            "SELECT SUM(amount) FROM t "
            "WHERE EXISTS (SELECT 1 FROM u WHERE u.k IN (4, 5))"
        )
        with pytest.raises(SecurityAuditError, match="business_date_month"):
            audit_filters_present(bq, sql, "source", filter_anchors=_DEMO_ANCHORS)

    def test_e1_cross_type_other_column_blocks(self):
        """E1: string members ['4','5'] vs numeric IN (4,5) on another
        column — the type coercion must not bridge the wrong dimension."""
        bq = _bound(filters=[
            LogicalFilter("business_date_month", "in", ["4", "5"]),
        ])
        sql = "SELECT SUM(amount) FROM t WHERE t.other_thing IN (4, 5)"
        with pytest.raises(SecurityAuditError, match="business_date_month"):
            audit_filters_present(bq, sql, "source", filter_anchors=_DEMO_ANCHORS)

    def test_f1_duplicate_member_scalar_collision_blocks(self):
        """F1: IN [4,4] must not be rescued by region_id = 4."""
        bq = _bound(filters=[
            LogicalFilter("business_date_month", "in", ["4", "4"]),
        ])
        sql = "SELECT SUM(amount) FROM t WHERE t.region_id = 4"
        with pytest.raises(SecurityAuditError, match="business_date_month"):
            audit_filters_present(bq, sql, "source", filter_anchors=_DEMO_ANCHORS)

    # --- reviewer-mandated additional collision shapes ---------------------

    def test_scalar_eq_collision_blocks(self):
        """Round-2 LOW (legacy substring fallback removed): a dropped
        scalar eq filter is NOT rescued by an incidental literal on an
        unrelated column."""
        bq = _bound(filters=[
            LogicalFilter("business_date_month", "eq", "4"),
        ])
        sql = "SELECT SUM(amount) FROM t WHERE t.region_id = 4"
        with pytest.raises(SecurityAuditError, match="business_date_month"):
            audit_filters_present(bq, sql, "source", filter_anchors=_DEMO_ANCHORS)

    def test_scalar_substring_in_identifier_blocks(self):
        """The old str(value)-substring fallback would pass when the value
        text appeared inside an identifier or table name.  Removed."""
        bq = _bound(filters=[
            LogicalFilter("business_date_month", "eq", "4"),
        ])
        sql = "SELECT SUM(amount) FROM warehouse4 WHERE warehouse4.zone = 'A'"
        with pytest.raises(SecurityAuditError, match="business_date_month"):
            audit_filters_present(bq, sql, "source", filter_anchors=_DEMO_ANCHORS)

    def test_anchored_predicate_inside_exists_blocks(self):
        """Subquery-scope collision: even a predicate on the RIGHT
        dimension expression does not count when it lives inside a
        predicate-side subquery (it does not constrain outer rows)."""
        bq = _bound(filters=[
            LogicalFilter("business_date_month", "in", ["4", "5"]),
        ])
        sql = (
            "SELECT SUM(amount) FROM t WHERE EXISTS ("
            "SELECT 1 FROM t2 WHERE EXTRACT(MONTH FROM t2.business_date) IN (4, 5))"
        )
        with pytest.raises(SecurityAuditError, match="business_date_month"):
            audit_filters_present(bq, sql, "source", filter_anchors=_DEMO_ANCHORS)

    def test_left_joined_subquery_scope_blocks(self):
        """A predicate inside a LEFT-joined source does not constrain the
        outer rows, so it must not satisfy the filter."""
        bq = _bound(filters=[
            LogicalFilter("business_date_month", "in", ["4", "5"]),
        ])
        sql = (
            "SELECT SUM(amount) FROM t LEFT JOIN ("
            "SELECT id FROM c WHERE EXTRACT(MONTH FROM c.business_date) IN (4, 5)"
            ") cal ON cal.id = t.id WHERE t.z = 1"
        )
        with pytest.raises(SecurityAuditError, match="business_date_month"):
            audit_filters_present(bq, sql, "source", filter_anchors=_DEMO_ANCHORS)

    def test_superset_in_list_blocks(self):
        """An IN-list covering MORE members than the filter is a different
        (leakier) predicate — exact value equality is required."""
        bq = _bound(filters=[
            LogicalFilter("business_date_month", "in", ["4", "5"]),
        ])
        sql = (
            "SELECT SUM(amount) FROM t "
            "WHERE EXTRACT(MONTH FROM t.business_date) IN (4, 5, 6)"
        )
        with pytest.raises(SecurityAuditError, match="business_date_month"):
            audit_filters_present(bq, sql, "source", filter_anchors=_DEMO_ANCHORS)

    def test_not_in_polarity_blocks_in_filter(self):
        """NOT IN on the anchored dimension must not satisfy an `in`
        filter (sqlglot parses NOT IN as Not(In))."""
        bq = _bound(filters=[
            LogicalFilter("business_date_month", "in", ["4", "5"]),
        ])
        sql = (
            "SELECT SUM(amount) FROM t "
            "WHERE EXTRACT(MONTH FROM t.business_date) NOT IN (4, 5)"
        )
        with pytest.raises(SecurityAuditError, match="business_date_month"):
            audit_filters_present(bq, sql, "source", filter_anchors=_DEMO_ANCHORS)

    def test_is_null_on_wrong_column_blocks(self):
        """IS NULL must be anchored too — an IS NOT NULL on another column
        must not satisfy an is_not_null filter."""
        bq = _bound(filters=[
            LogicalFilter("business_date_month", "is_not_null", None),
        ])
        sql = "SELECT SUM(amount) FROM t WHERE t.other IS NOT NULL"
        with pytest.raises(SecurityAuditError, match="business_date_month"):
            audit_filters_present(bq, sql, "source", filter_anchors=_DEMO_ANCHORS)

    def test_in_subquery_membership_blocks(self):
        """IN (SELECT …) on the anchored dimension is not provable
        membership — fail-closed."""
        bq = _bound(filters=[
            LogicalFilter("business_date_month", "in", ["4", "5"]),
        ])
        sql = (
            "SELECT SUM(amount) FROM t "
            "WHERE EXTRACT(MONTH FROM t.business_date) IN (SELECT m FROM allowed)"
        )
        with pytest.raises(SecurityAuditError, match="business_date_month"):
            audit_filters_present(bq, sql, "source", filter_anchors=_DEMO_ANCHORS)

    # --- INFO finding: NULL member handling --------------------------------

    def test_null_member_in_in_list_passes(self):
        """Round-2 INFO: a NULL member in an IN-list (rendered IN (4, NULL))
        is matched via the exp.Null node, not wrongly blocked."""
        bq = _bound(filters=[
            LogicalFilter("business_date_month", "in", ["4", None]),
        ])
        sql = (
            "SELECT SUM(amount) FROM t "
            "WHERE EXTRACT(MONTH FROM t.business_date) IN (4, NULL)"
        )
        audit_filters_present(bq, sql, "source", filter_anchors=_DEMO_ANCHORS)

    # --- anchored legit shapes keep passing --------------------------------

    def test_cte_scope_passes(self):
        """A filter rendered inside a CTE that feeds the FROM chain
        constrains the result and passes."""
        bq = _bound(filters=[
            LogicalFilter("business_date_month", "in", ["4", "5"]),
        ])
        sql = (
            "WITH base AS (SELECT amount FROM t "
            "WHERE EXTRACT(MONTH FROM t.business_date) IN (4, 5)) "
            "SELECT SUM(amount) FROM base"
        )
        audit_filters_present(bq, sql, "source", filter_anchors=_DEMO_ANCHORS)

    def test_physical_column_anchor_from_model_column(self):
        """A dimension whose semantic name differs from its physical column
        is anchored via the ModelColumn name."""
        anchors = {"order_country": build_anchor_keys(
            column_names=["order_country", "country_code"],
        )}
        bq = _bound(filters=[
            LogicalFilter("order_country", "in", ["US", "DE"]),
        ])
        sql = "SELECT SUM(amount) FROM t WHERE t.country_code IN ('US', 'DE')"
        audit_filters_present(bq, sql, "source", filter_anchors=anchors)


# ===========================================================================
# Bug-1045 round 4 — residual wrong-pass surfaces (B10 deep review round 3).
# Three axes the round-3 anchoring left open: predicate POSITION (an
# anchored predicate in a non-constraining position — OR-disjunct / CASE
# condition — must not prove the filter), polarity NESTING (NOT through
# Paren wrappers), and the projection-alias identifier SHORTCUT (projection
# is not filtering).  These are the reviewer's probes as permanent
# regression tests.
# ===========================================================================

class TestAuditRound4ResidualSurfaces:

    # --- Finding 1 (MEDIUM): non-constraining predicate position -----------

    def test_p6_or_disjunct_anchored_predicate_blocks(self):
        """P6: the anchored month IN (4,5) sits under an OR-disjunct, so
        rows with country = 'ZZ' bypass it — the filter is NOT enforced."""
        bq = _bound(filters=[
            LogicalFilter("business_date_month", "in", ["4", "5"]),
        ])
        sql = (
            "SELECT SUM(amount) FROM t "
            "WHERE EXTRACT(MONTH FROM t.business_date) IN (4, 5) "
            "OR t.country = 'ZZ'"
        )
        with pytest.raises(SecurityAuditError, match="business_date_month"):
            audit_filters_present(bq, sql, "source", filter_anchors=_DEMO_ANCHORS)

    def test_p5b_case_condition_in_where_blocks(self):
        """P5b: the anchored IN lives inside a CASE condition within WHERE
        — any flag-matching row passes, so the filter is NOT enforced."""
        bq = _bound(filters=[
            LogicalFilter("business_date_month", "in", ["4", "5"]),
        ])
        sql = (
            "SELECT SUM(amount) FROM t "
            "WHERE t.flag = CASE WHEN "
            "EXTRACT(MONTH FROM t.business_date) IN (4, 5) "
            "THEN 1 ELSE 0 END"
        )
        with pytest.raises(SecurityAuditError, match="business_date_month"):
            audit_filters_present(bq, sql, "source", filter_anchors=_DEMO_ANCHORS)

    def test_paren_and_conjunct_still_passes(self):
        """Pass-guard for the conjunct-level restriction: parenthesised
        top-level AND conjuncts still flatten and the anchored predicate
        still proves the filter (no false block)."""
        bq = _bound(filters=[
            LogicalFilter("business_date_month", "in", ["4", "5"]),
        ])
        sql = (
            "SELECT SUM(amount) FROM t "
            "WHERE (EXTRACT(MONTH FROM t.business_date) IN (4, 5) "
            "AND t.country_code = 'US')"
        )
        audit_filters_present(bq, sql, "source", filter_anchors=_DEMO_ANCHORS)

    # --- Finding 2 (LOW): polarity through Paren wrappers -------------------

    def test_p7_paren_wrapped_negated_in_blocks(self):
        """P7: NOT ((x IN (4,5))) parses as Not(Paren(Paren(In))) — the
        negation must be seen through the Paren wrappers and the negated
        IN must not satisfy an `in` filter."""
        bq = _bound(filters=[
            LogicalFilter("business_date_month", "in", ["4", "5"]),
        ])
        sql = (
            "SELECT SUM(amount) FROM t "
            "WHERE NOT ((EXTRACT(MONTH FROM t.business_date) IN (4, 5)))"
        )
        with pytest.raises(SecurityAuditError, match="business_date_month"):
            audit_filters_present(bq, sql, "source", filter_anchors=_DEMO_ANCHORS)

    def test_double_negation_parity_passes(self):
        """NOT (NOT (x IN (4,5))) is semantically x IN (4,5) — even
        parity reads positive, so the anchored predicate proves the
        filter (no false block)."""
        bq = _bound(filters=[
            LogicalFilter("business_date_month", "in", ["4", "5"]),
        ])
        sql = (
            "SELECT SUM(amount) FROM t "
            "WHERE NOT (NOT (EXTRACT(MONTH FROM t.business_date) IN (4, 5)))"
        )
        audit_filters_present(bq, sql, "source", filter_anchors=_DEMO_ANCHORS)

    def test_triple_negation_parity_blocks(self):
        """NOT (NOT (NOT (x IN (4,5)))) — odd parity reads negative, so
        the negated IN must not satisfy an `in` filter."""
        bq = _bound(filters=[
            LogicalFilter("business_date_month", "in", ["4", "5"]),
        ])
        sql = (
            "SELECT SUM(amount) FROM t "
            "WHERE NOT (NOT (NOT (EXTRACT(MONTH FROM t.business_date) IN (4, 5))))"
        )
        with pytest.raises(SecurityAuditError, match="business_date_month"):
            audit_filters_present(bq, sql, "source", filter_anchors=_DEMO_ANCHORS)

    def test_paren_wrapped_negated_is_null_blocks(self):
        """Same polarity surface on the IS NULL branch: NOT ((x IS NULL))
        must not satisfy an is_null filter."""
        bq = _bound(filters=[
            LogicalFilter("business_date_month", "is_null", None),
        ])
        sql = (
            "SELECT SUM(amount) FROM t "
            "WHERE NOT ((EXTRACT(MONTH FROM t.business_date) IS NULL))"
        )
        with pytest.raises(SecurityAuditError, match="business_date_month"):
            audit_filters_present(bq, sql, "source", filter_anchors=_DEMO_ANCHORS)

    # --- Finding 3 (LOW): projection alias is not filtering -----------------

    def test_p2_projected_dim_dropped_filter_blocks(self):
        """P2: the derived dim is PROJECTED (AS alias satisfies the
        identifier check) but its WHERE predicate was dropped — the alias
        must not rescue the missing filter."""
        bq = _bound(filters=[
            LogicalFilter("business_date_month", "in", ["4", "5"]),
            LogicalFilter("country_code", "eq", "US"),
        ])
        sql = (
            "SELECT EXTRACT(MONTH FROM t.business_date) AS \"business_date_month\", "
            "SUM(amount) FROM t "
            "WHERE t.country_code = 'US' "
            "GROUP BY \"business_date_month\""
        )
        with pytest.raises(SecurityAuditError, match="business_date_month"):
            audit_filters_present(bq, sql, "source", filter_anchors=_DEMO_ANCHORS)

    def test_p2b_projected_dim_dropped_scalar_filter_blocks(self):
        """P2b: scalar-eq variant — the projected alias (also in GROUP BY)
        must not satisfy a dropped eq filter on the same dimension."""
        bq = _bound(filters=[
            LogicalFilter("business_date_month", "eq", "4"),
            LogicalFilter("country_code", "eq", "US"),
        ])
        sql = (
            "SELECT EXTRACT(MONTH FROM t.business_date) AS \"business_date_month\", "
            "SUM(amount) FROM t "
            "WHERE t.country_code = 'US' "
            "GROUP BY \"business_date_month\""
        )
        with pytest.raises(SecurityAuditError, match="business_date_month"):
            audit_filters_present(bq, sql, "source", filter_anchors=_DEMO_ANCHORS)


# ===========================================================================
# Bug-5326 — NOT LIKE (not_like) filter-presence audit.
#
# The structured filter-contract path (notContains → not_like, and the
# canonical not_like operator) renders ``col NOT LIKE '%x%'``.  The audit
# must MATCH a genuinely-present NOT LIKE (so "Not Contains" returns 200,
# not 403) while STILL BLOCKING when the negated predicate was dropped or
# its polarity flipped — and must keep a positive ``like`` distinct from a
# ``not_like``.  sqlglot encodes NOT LIKE as EITHER Like(negate=True)
# (30.8.x, the SHIPPING container shape) OR Not(Like) (30.4.x, the host CI
# shape); both must be proven.
# ===========================================================================

class TestAuditNotLike:

    # --- positive path: a present NOT LIKE proves a not_like filter --------

    def test_not_like_filter_present_passes(self):
        """notContains/not_like with a matching NOT LIKE in the WHERE → PASS
        (no 403).  This is the live Bug-5326 repro: the audit used to count
        the filter missing because it had no not_like vocabulary."""
        bq = _bound(filters=[LogicalFilter("payment_status", "not_like", "%FAIL%")])
        sql = (
            'SELECT "payment_status" FROM "t" AS "t" '
            "WHERE \"t\".\"payment_status\" NOT LIKE '%FAIL%'"
        )
        audit_filters_present(bq, sql, "source")

    def test_not_ilike_filter_present_passes(self):
        bq = _bound(filters=[LogicalFilter("payment_status", "not_like", "%fail%")])
        sql = (
            'SELECT "payment_status" FROM "t" AS "t" '
            "WHERE \"t\".\"payment_status\" NOT ILIKE '%fail%'"
        )
        audit_filters_present(bq, sql, "source")

    # --- GUARDRAIL: a dropped not_like must STILL block --------------------

    def test_not_like_filter_dropped_blocks(self):
        """The not_like predicate is entirely absent from the rewritten SQL
        (a genuine silent drop) — the audit must still fail-closed-block."""
        bq = _bound(filters=[LogicalFilter("payment_status", "not_like", "%FAIL%")])
        sql = (
            'SELECT "payment_status" FROM "t" AS "t" '
            "WHERE \"t\".\"channel\" = 'web'"
        )
        with pytest.raises(SecurityAuditError, match="payment_status"):
            audit_filters_present(bq, sql, "source")

    def test_not_like_no_where_blocks(self):
        bq = _bound(filters=[LogicalFilter("payment_status", "not_like", "%FAIL%")])
        sql = 'SELECT "payment_status" FROM "t" AS "t"'
        with pytest.raises(SecurityAuditError):
            audit_filters_present(bq, sql, "source")

    # --- GUARDRAIL: polarity flip must STILL block -------------------------

    def test_not_like_filter_rendered_as_positive_like_blocks(self):
        """The exact Bug-5325 inversion class: the user asked for NOT LIKE
        (the complement) but the rewritten SQL carries a positive LIKE.  A
        not_like filter must NOT be satisfied by a positive LIKE — block."""
        bq = _bound(filters=[LogicalFilter("payment_status", "not_like", "%FAIL%")])
        sql = (
            'SELECT "payment_status" FROM "t" AS "t" '
            "WHERE \"t\".\"payment_status\" LIKE '%FAIL%'"
        )
        with pytest.raises(SecurityAuditError, match="payment_status"):
            audit_filters_present(bq, sql, "source")

    def test_positive_like_filter_rendered_as_not_like_blocks(self):
        """Symmetric: a positive ``like`` filter (contains) must NOT be
        satisfied by a NOT LIKE predicate — the rewriter flipped polarity."""
        bq = _bound(filters=[LogicalFilter("payment_status", "like", "%FAIL%")])
        sql = (
            'SELECT "payment_status" FROM "t" AS "t" '
            "WHERE \"t\".\"payment_status\" NOT LIKE '%FAIL%'"
        )
        with pytest.raises(SecurityAuditError, match="payment_status"):
            audit_filters_present(bq, sql, "source")

    # --- positive like unchanged ------------------------------------------

    def test_positive_like_filter_present_passes(self):
        """Positive ``like`` (contains) behaviour is unchanged: a matching
        LIKE proves the filter present."""
        bq = _bound(filters=[LogicalFilter("payment_status", "like", "%FAIL%")])
        sql = (
            'SELECT "payment_status" FROM "t" AS "t" '
            "WHERE \"t\".\"payment_status\" LIKE '%FAIL%'"
        )
        audit_filters_present(bq, sql, "source")

    # --- GUARDRAIL: wrong value must STILL block ---------------------------

    def test_not_like_filter_wrong_value_blocks(self):
        """A NOT LIKE on the right dimension but a DIFFERENT pattern is a
        different predicate — it does not prove this filter."""
        bq = _bound(filters=[LogicalFilter("payment_status", "not_like", "%FAIL%")])
        sql = (
            'SELECT "payment_status" FROM "t" AS "t" '
            "WHERE \"t\".\"payment_status\" NOT LIKE '%SUCCESS%'"
        )
        with pytest.raises(SecurityAuditError, match="payment_status"):
            audit_filters_present(bq, sql, "source")

    def test_not_like_filter_wrong_column_blocks(self):
        """A NOT LIKE with the right pattern but on a DIFFERENT column does
        not prove a not_like filter on payment_status — the LHS anchor must
        resolve to THIS dimension.  (Deep-review enhancement: pins the
        wrong-column guardrail that ``_lhs_matches_anchor`` enforces, which
        was previously only verified live.)"""
        bq = _bound(filters=[LogicalFilter("payment_status", "not_like", "%FAIL%")])
        sql = (
            'SELECT "payment_status" FROM "t" AS "t" '
            "WHERE \"t\".\"channel\" NOT LIKE '%FAIL%'"
        )
        with pytest.raises(SecurityAuditError, match="payment_status"):
            audit_filters_present(bq, sql, "source")

    # --- REAL-SHAPE UNIT (F-PF-02): construct Like(negate=True) explicitly,
    #     shape-agnostic, so the SHIPPING sqlglot 30.8.x encoding is proven
    #     regardless of the host's installed sqlglot version ----------------

    def test_negation_parity_reads_like_negate_attribute(self):
        """Directly construct an ``exp.Like(negate=True)`` node (the sqlglot
        30.8.x SHIPPING shape) and assert the audit's polarity machinery
        treats it as negated, independent of the host sqlglot version."""
        from sqlglot import exp
        from src.security.query_audit import (
            _like_node_negate,
            _negation_parity,
            _polarity_ok,
        )

        # Like(negate=True) — the collapsed NOT LIKE shape, with NO enclosing
        # exp.Not (so the parent-walk alone would read it as positive).
        neg_like = exp.Like(
            this=exp.column("payment_status"),
            expression=exp.Literal.string("%FAIL%"),
            negate=True,
        )
        assert _like_node_negate(neg_like) is True
        assert _negation_parity(neg_like) is True
        # not_like filter matches a negated Like; positive like does not.
        assert _polarity_ok("not_like", neg_like) is True
        assert _polarity_ok("like", neg_like) is False

        pos_like = exp.Like(
            this=exp.column("payment_status"),
            expression=exp.Literal.string("%FAIL%"),
        )
        assert _like_node_negate(pos_like) is False
        assert _negation_parity(pos_like) is False
        assert _polarity_ok("like", pos_like) is True
        assert _polarity_ok("not_like", pos_like) is False

    def test_not_filter_predicate_in_ast_negate_attr_shape(self):
        """End-to-end through ``_filter_predicate_in_ast`` using a hand-built
        AST whose Like carries ``negate=True`` (no Not wrapper) — proves the
        per-filter proof under the SHIPPING shape without relying on the host
        parser collapsing NOT LIKE."""
        from sqlglot import exp
        from src.security.query_audit import _filter_predicate_in_ast

        neg_like = exp.Like(
            this=exp.column("payment_status"),
            expression=exp.Literal.string("%FAIL%"),
            negate=True,
        )
        where = exp.Where(this=neg_like)
        f = LogicalFilter("payment_status", "not_like", "%FAIL%")
        assert _filter_predicate_in_ast(f, [where], {"payment_status"}) is True
        # A positive like filter must NOT prove against this negated Like.
        f_pos = LogicalFilter("payment_status", "like", "%FAIL%")
        assert _filter_predicate_in_ast(f_pos, [where], {"payment_status"}) is False

    # --- PRODUCER↔CONSUMER seam (F-PF-03): the real structured filter-contract
    #     producer (notContains → not_like) + the real condition renderer must
    #     pass the audit; a deliberately-dropped one must still block. --------

    def test_structured_notcontains_seam_passes_audit(self):
        """notContains goes through the REAL filter-contract producer and the
        REAL condition renderer, then through ``audit_filters_present`` — the
        exact seam that produced the live 403 (F-PF-01/F-PF-03)."""
        from src.api.filter_contract import SemanticFilter, build_logical_filters
        from src.rewrite.conditions import _render_condition

        lf = build_logical_filters([
            SemanticFilter(dimension="payment_status",
                           operator="notContains", value="FAIL"),
        ])
        assert len(lf) == 1 and lf[0].operator == "not_like"
        where = _render_condition("\"payment_status\"", lf[0].operator, lf[0].value)
        sql = f'SELECT "payment_status" FROM "t" AS "t" WHERE {where}'
        assert "NOT LIKE" in sql
        bq = _bound(filters=lf)
        audit_filters_present(bq, sql, "source")  # must NOT raise

    def test_structured_notcontains_seam_dropped_blocks(self):
        """Same producer, but the rendered predicate is dropped from the SQL —
        the audit must still fail-closed (no bypass via the new not_like path)."""
        from src.api.filter_contract import SemanticFilter, build_logical_filters

        lf = build_logical_filters([
            SemanticFilter(dimension="payment_status",
                           operator="notContains", value="FAIL"),
        ])
        bq = _bound(filters=lf)
        sql = 'SELECT "payment_status" FROM "t" AS "t" WHERE "t"."channel" = \'web\''
        with pytest.raises(SecurityAuditError, match="payment_status"):
            audit_filters_present(bq, sql, "source")


# ===========================================================================
# resolve_filter_anchors — anchor resolution from model metadata
# ===========================================================================

class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _FakeDb:
    """Returns Dimension rows first, then ModelColumn rows, then UDA rows
    (the resolver's query order)."""

    def __init__(self, dims=(), cols=(), udas=()):
        self._batches = []
        self.calls = 0
        self._dims, self._cols, self._udas = list(dims), list(cols), list(udas)

    async def execute(self, stmt):
        self.calls += 1
        desc = str(stmt).lower()
        if "from dimensions" in desc:
            return _FakeResult(self._dims)
        if "from model_columns" in desc:
            return _FakeResult(self._cols)
        if "from user_defined_attributes" in desc:
            return _FakeResult(self._udas)
        return _FakeResult([])


class TestResolveFilterAnchors:

    @pytest.mark.asyncio
    async def test_no_filters_returns_empty_without_db(self):
        bq = _bound(filters=[])
        db = _FakeDb()
        assert await resolve_filter_anchors(bq, db) == {}
        assert db.calls == 0

    @pytest.mark.asyncio
    async def test_uda_dimension_anchored_via_db(self):
        """A filter-only UDA dimension (not in resolved_dimensions) is
        loaded from the DB and anchored by its expression."""
        uda_id = "uda-1"
        dim = types.SimpleNamespace(
            id="d-1", name="business_date_month",
            source_column_id=None, calc_expression=None,
            user_defined_attribute_id=uda_id,
        )
        uda = types.SimpleNamespace(
            id=uda_id, expression='EXTRACT(MONTH FROM ("business_date"))',
        )
        bq = _bound(filters=[
            LogicalFilter("business_date_month", "in", ["4", "5"]),
        ])
        db = _FakeDb(dims=[dim], udas=[uda])
        anchors = await resolve_filter_anchors(bq, db)
        assert "extract(month from business_date)" in anchors["business_date_month"]
        assert "business_date_month" in anchors["business_date_month"]

    @pytest.mark.asyncio
    async def test_physical_column_anchored_via_model_column(self):
        col_id = "c-1"
        dim = types.SimpleNamespace(
            id="d-1", name="order_country",
            source_column_id=col_id, calc_expression=None,
            user_defined_attribute_id=None,
        )
        col = types.SimpleNamespace(id=col_id, column_name="country_code")
        bq = _bound(filters=[LogicalFilter("order_country", "eq", "US")])
        bq.resolved_dimensions = [dim]
        db = _FakeDb(cols=[col])
        anchors = await resolve_filter_anchors(bq, db)
        assert "country_code" in anchors["order_country"]

    @pytest.mark.asyncio
    async def test_calc_expression_anchored_without_db(self):
        dim = types.SimpleNamespace(
            id="d-1", name="amount_band",
            source_column_id=None,
            calc_expression="CASE WHEN amount > 100 THEN 'high' ELSE 'low' END",
            user_defined_attribute_id=None,
        )
        bq = _bound(filters=[LogicalFilter("amount_band", "eq", "high")])
        bq.resolved_dimensions = [dim]
        anchors = await resolve_filter_anchors(bq, None)
        assert any("case when" in k for k in anchors["amount_band"])

    @pytest.mark.asyncio
    async def test_db_failure_degrades_to_name_anchor(self):
        """Resolution failure must yield FEWER anchors (fail-closed),
        never an exception that aborts the audit path."""
        class _BrokenDb:
            async def execute(self, stmt):
                raise RuntimeError("db down")

        bq = _bound(filters=[
            LogicalFilter("business_date_month", "in", ["4", "5"]),
        ])
        anchors = await resolve_filter_anchors(bq, _BrokenDb())
        assert anchors["business_date_month"] == {"business_date_month"}


# ===========================================================================
# Layer 2 — audit_result_columns
# ===========================================================================

class TestAuditResultColumns:

    def test_matching_columns_pass(self):
        bq = _bound(dims=["city_name"], measures=["revenue"])
        audit_result_columns(bq, ["city_name", "revenue"], None)

    def test_extra_column_raises(self):
        bq = _bound(dims=["city_name"], measures=["revenue"])
        with pytest.raises(SecurityAuditError, match="secret_col"):
            audit_result_columns(bq, ["city_name", "revenue", "secret_col"], None)

    def test_aggregate_stat_column_accepted(self):
        bq = _bound(dims=["city_name"], measures=["revenue"])
        audit_result_columns(bq, ["city_name", "revenue__sum"], None)

    def test_multiple_stat_types_accepted(self):
        bq = _bound(dims=["city_name"], measures=["revenue"])
        audit_result_columns(
            bq,
            ["city_name", "revenue__sum", "revenue__count", "revenue__min"],
            None,
        )

    def test_count_synthetic_accepted(self):
        bq = _bound(dims=["city_name"], measures=["__row_count"])
        audit_result_columns(bq, ["city_name", "count"], None)

    def test_select_star_persona_narrowed(self):
        bq = _bound(dims=["city_name", "region"], measures=["revenue"])
        bq.persona_narrowed_star = True
        bq.resolved_dimensions = [
            types.SimpleNamespace(id="d-city_name", name="city_name"),
        ]
        audit_result_columns(bq, ["city_name", "revenue"], None)

    def test_select_star_persona_leak_raises(self):
        bq = _bound(dims=["city_name"], measures=["revenue"])
        bq.persona_narrowed_star = True
        with pytest.raises(SecurityAuditError, match="region"):
            audit_result_columns(bq, ["city_name", "revenue", "region"], None)

    def test_synthetic_question_mark_column(self):
        bq = _bound(dims=[], measures=[])
        audit_result_columns(bq, ["?column?"], None)

    def test_bigquery_f0_column(self):
        bq = _bound(dims=[], measures=[])
        audit_result_columns(bq, ["f0_"], None)

    def test_user_alias_accepted(self):
        from src.ir.logical_query import SelectExpression
        se = SelectExpression(
            raw_text="COUNT(1)",
            alias="total_count",
            classification="analytical",
            agg_function="count",
            inner_column=None,
            inner_literal="1",
        )
        bq = _bound(
            dims=["city_name"],
            measures=[],
            lq_kwargs={"select_expressions": [se]},
        )
        audit_result_columns(bq, ["city_name", "total_count"], None)

    def test_passthrough_skips_audit(self):
        bq = _bound(dims=[], measures=[], has_passthrough=True)
        audit_result_columns(bq, ["any_col", "secret_col"], None)

    def test_complex_sql_skips_audit(self):
        bq = _bound(
            dims=[], measures=[],
            lq_kwargs={"has_complex_sql": True},
        )
        audit_result_columns(bq, ["any_col", "secret_col"], None)

    def test_empty_result_columns_passes(self):
        bq = _bound(dims=["city_name"], measures=["revenue"])
        audit_result_columns(bq, [], None)

    def test_case_insensitive_column_match(self):
        bq = _bound(dims=["city_name"], measures=["Revenue"])
        audit_result_columns(bq, ["City_Name", "REVENUE"], None)

    def test_bare_object_bound_query_skips(self):
        audit_result_columns(object(), ["any_col"], None)

    def test_hidden_column_not_in_resolved_raises(self):
        bq = _bound(dims=["city_name"], measures=["revenue"])
        with pytest.raises(SecurityAuditError, match="hidden_secret"):
            audit_result_columns(bq, ["city_name", "revenue", "hidden_secret"], None)
