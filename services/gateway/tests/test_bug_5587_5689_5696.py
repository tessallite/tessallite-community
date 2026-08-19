"""Tests for Bug-5587, Bug-5689, and Bug-5696 gateway fixes.

Bug-5587 (SUPERSEDED by Bug-7227): the original fix blanked the ENTIRE KPI list
          for any persona with a populated included_measure_ids allow-list, so a
          measure-restricted persona saw ZERO KPIs in Excel/Power BI. Bug-7227
          replaces the blanket exclusion with a transitive-lineage filter: a KPI
          is advertised iff every measure in its lineage is in the allow-list;
          fail closed on unverifiable lineage. The persona sees the KPI subset it
          IS allowed — not empty, not all. The Bug-5587 classes below now assert
          that lineage-filtered behavior against the REAL production helper
          (filter_kpis_for_persona), the same one the XMLA + JDBC paths call.
Bug-5689 (superseded by Bug-6608, then un-gated 2026-07-21): the gateway does not
          derive its OWN band verdict. MDSCHEMA_KPIS advertises an addressable
          status MEMBER (value member, or authored expression); the LIVE KPIStatus
          value is the governed −1/0/1 from the model-service /evaluate authority
          (covered in test_kpi_member_functions.py). The metadata resolver
          (mdx_execute.resolve_kpi_property_expr) still returns the value member.
Bug-5696: _inline_named_sets closure must capture loop variables by value,
          not by reference.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.dax import mdschema
from src.dax.kpi_persona_filter import filter_kpis_for_persona
from src.dax.mdx_execute import resolve_kpi_property_expr
from src.dax.xmla_server import _inline_named_sets


# ---------------------------------------------------------------------------
# Shared test data
# ---------------------------------------------------------------------------

_MEASURES = [
    {"id": "m1", "name": "Revenue", "default_agg": "sum", "display_name": "Revenue"},
    {"id": "m2", "name": "Cost", "default_agg": "sum", "display_name": "Cost"},
]

_KPIS = [
    {
        "id": "k1",
        "name": "margin_pct",
        "display_name": "Margin %",
        "description": "Gross margin",
        "display_folder": "Finance",
        "value_measure_id": "m1",
        "goal_measure_id": "m2",
        "expression": "",
        "target_type": "static",
        "target_value": 100.0,
        "target_expression": None,
        "status_expression": "",
        "trend_expression": "",
        "weight": None,
        "parent_kpi_id": None,
        "presentation_type": None,
        "direction": "higher_is_better",
    },
]


# ===================================================================
# Bug-5587: persona KPI filtering
# ===================================================================


class TestBug5587_JDBCPersonaKPIFiltering:
    """JDBC inline KPI columns (router_client._prepare_model_data) are filtered
    BY LINEAGE for a measure-restricted persona (Bug-7227). The base model / an
    unrestricted persona sees every KPI; a persona whose allow-list covers a
    KPI's full lineage sees that KPI; a persona missing any lineage measure does
    not. Exercises the REAL production helper the path calls."""

    def test_base_model_sees_inline_kpis(self):
        # No allow-list (None or empty) -> unrestricted -> all KPIs kept.
        assert len(filter_kpis_for_persona(_KPIS, _MEASURES, None)) == 1
        assert len(filter_kpis_for_persona(_KPIS, _MEASURES, set())) == 1

    def test_restricted_persona_with_full_lineage_sees_kpi(self):
        # margin_pct lineage = {m1 (value), m2 (goal)}; both allowed -> kept.
        result = filter_kpis_for_persona(_KPIS, _MEASURES, {"m1", "m2"})
        assert [k["name"] for k in result] == ["margin_pct"]

    def test_partial_lineage_restriction_hides_kpi(self):
        # Only m1 allowed; goal_measure_id m2 outside allow-list -> withheld.
        assert filter_kpis_for_persona(_KPIS, _MEASURES, {"m1"}) == []


class TestBug5587_XMLAPersonaKPIFiltering:
    """XMLA Discover MDSCHEMA_KPIS is filtered BY LINEAGE for a measure-restricted
    persona (Bug-7227) — the persona sees the KPI subset it is allowed, not an
    empty set. Same production helper as the JDBC path."""

    def test_no_persona_keeps_kpis(self):
        result = filter_kpis_for_persona(list(_KPIS), _MEASURES, None)
        assert len(result) == 1

    def test_unrestricted_persona_keeps_kpis(self):
        # Empty allow-list == unrestricted.
        result = filter_kpis_for_persona(list(_KPIS), _MEASURES, set())
        assert len(result) == 1

    def test_full_lineage_allow_list_keeps_kpi(self):
        result = filter_kpis_for_persona(list(_KPIS), _MEASURES, {"m1", "m2"})
        assert [k["name"] for k in result] == ["margin_pct"]

    def test_partial_lineage_allow_list_withholds_kpi(self):
        # m2 (goal) not allowed -> KPI withheld (not the whole list blanked —
        # this asserts the subset semantics, not the old blanket behaviour).
        result = filter_kpis_for_persona(list(_KPIS), _MEASURES, {"m1"})
        assert result == []


# ===================================================================
# Bug-6608 (supersedes Bug-5689 band verdicts): the gateway serves the RAW
# KPI value for status, not a direction-derived band verdict. closer_is_better
# (and every other direction) no longer changes the served status.
# ===================================================================


_KPI_CLOSER = {
    "name": "csat",
    "display_name": "CSAT",
    "value_measure_id": "m1",
    "target_type": "static",
    "target_value": 90.0,
    "direction": "closer_is_better",
    "status_expression": None,
    "trend_expression": None,
    "goal_measure_id": None,
}


class TestBug6608_StatusIsRawValue_Mdschema:
    """Bug-8288: MDSCHEMA_KPIS KPI_STATUS is the synthetic GOVERNED status member
    (never a CASE/ABS band verdict) for a KPI with a verdict basis, for any
    direction; the authored direction/bands are still published as an annotation.
    The Execute path resolves the synthetic member to the governed -1/0/1."""

    def test_closer_is_better_status_is_governed_member(self):
        row = mdschema._rows_kpis("cat", [dict(_KPI_CLOSER)], _MEASURES)[0]
        assert row["KPI_STATUS"] == "[Measures].[CSAT Status]"
        # Never the raw value member, never a gateway CASE/ABS band verdict.
        assert row["KPI_STATUS"] != "[Measures].[Revenue]"
        assert not row["KPI_STATUS"].upper().startswith("CASE")
        assert "ABS(" not in row["KPI_STATUS"]

    def test_direction_published_in_annotation(self):
        row = mdschema._rows_kpis("cat", [dict(_KPI_CLOSER)], _MEASURES)[0]
        assert "closer is better" in row["ANNOTATIONS"]

    def test_every_direction_serves_governed_member(self):
        for direction in ("higher_is_better", "lower_is_better", "closer_is_better"):
            kpi = dict(_KPI_CLOSER, direction=direction)
            row = mdschema._rows_kpis("cat", [kpi], _MEASURES)[0]
            # Direction does not change the advertised member (the verdict is
            # governed live, not encoded in the metadata member).
            assert row["KPI_STATUS"] == "[Measures].[CSAT Status]"


class TestBug6608_StatusMetadataMember_MdxExecute:
    """resolve_kpi_property_expr advertises the ADDRESSABLE status member (the
    value member) for every direction — no ABS/CASE band verdict in the metadata.
    The live −1/0/1 verdict is governed (test_kpi_member_functions.py)."""

    def test_closer_is_better_status_is_value_member(self):
        expr = resolve_kpi_property_expr(_KPI_CLOSER, "KPIStatus", _MEASURES)
        assert expr == "[Measures].[Revenue]"
        assert "ABS(" not in expr
        assert not expr.strip().upper().startswith("CASE")

    def test_all_directions_resolve_to_value_member(self):
        for direction in ("higher_is_better", "lower_is_better", "closer_is_better"):
            kpi = dict(_KPI_CLOSER, direction=direction)
            assert resolve_kpi_property_expr(kpi, "KPIStatus", _MEASURES) == \
                "[Measures].[Revenue]"


# ===================================================================
# Bug-5696: _inline_named_sets closure captures stale variable
# ===================================================================


class TestBug5696_NamedSetClosure:
    """_inline_named_sets must replace each named set with its OWN
    expression, not the last loop iteration's expression."""

    def test_single_set_replaced(self):
        mdx = "SELECT {[TopProducts]} ON COLUMNS FROM [Model]"
        sets = [{"name": "TopProducts", "expression": "TopCount([Product].Members, 5)"}]
        result = _inline_named_sets(mdx, sets)
        assert "TopCount([Product].Members, 5)" in result
        assert "[TopProducts]" not in result

    def test_multiple_sets_each_get_own_expression(self):
        """The core regression test: with two sets, each must be replaced
        by its own expression, not the other's."""
        mdx = (
            "SELECT {[TopProducts]} ON COLUMNS, "
            "{[TopCustomers]} ON ROWS FROM [Model]"
        )
        sets = [
            {"name": "TopProducts", "expression": "TopCount([Product].Members, 5)"},
            {"name": "TopCustomers", "expression": "TopCount([Customer].Members, 10)"},
        ]
        result = _inline_named_sets(mdx, sets)
        # Each set must have its own expression.
        assert "TopCount([Product].Members, 5)" in result
        assert "TopCount([Customer].Members, 10)" in result
        # Neither bracket reference should remain.
        assert "[TopProducts]" not in result
        assert "[TopCustomers]" not in result

    def test_from_clause_cube_name_not_replaced(self):
        """A set name that happens to match the cube name after FROM must
        NOT be replaced (the callback protects the cube name)."""
        mdx = "SELECT {[Sales]} ON COLUMNS FROM [Sales]"
        sets = [{"name": "Sales", "expression": "TopCount([Product].Members, 3)"}]
        result = _inline_named_sets(mdx, sets)
        # The FROM [Sales] cube name must survive.
        assert "FROM [Sales]" in result or "FROM  [Sales]" in result

    def test_empty_expression_skipped(self):
        mdx = "SELECT {[EmptySet]} ON COLUMNS FROM [Model]"
        sets = [{"name": "EmptySet", "expression": ""}]
        result = _inline_named_sets(mdx, sets)
        # No replacement — original bracket stays.
        assert "[EmptySet]" in result

    def test_empty_named_sets_list(self):
        mdx = "SELECT {[Measures].[Revenue]} ON COLUMNS FROM [Model]"
        result = _inline_named_sets(mdx, [])
        assert result == mdx

    def test_three_sets_correct_binding(self):
        """Stress test: three sets, each must bind to its own expression."""
        mdx = (
            "SELECT {[SetA], [SetB], [SetC]} ON COLUMNS FROM [Model]"
        )
        sets = [
            {"name": "SetA", "expression": "EXPR_A"},
            {"name": "SetB", "expression": "EXPR_B"},
            {"name": "SetC", "expression": "EXPR_C"},
        ]
        result = _inline_named_sets(mdx, sets)
        assert "EXPR_A" in result
        assert "EXPR_B" in result
        assert "EXPR_C" in result
        assert "[SetA]" not in result
        assert "[SetB]" not in result
        assert "[SetC]" not in result


class TestBug7252_BackslashInExpression:
    r"""Bug-7252 (CF-018-Fable-F01801): named-set expressions containing
    backslashes must not crash ``re.sub`` via template interpretation.

    The bare-name replacement path previously passed the expression as a
    string template to ``re.sub``, causing ``re.error`` on any backslash
    that is not a valid group reference (e.g. ``\C``, ``\1``).
    """

    def test_backslash_c_in_expression_does_not_crash(self):
        r"""Backslash followed by a non-digit like ``\C`` (common in AD-style
        fixed-member keys like ``EMEA\Central``) must not raise re.error."""
        mdx = "SELECT {[Measures].[Revenue]} ON COLUMNS, {[Key Regions]} ON ROWS FROM [Model]"
        sets = [
            {
                "name": "Key Regions",
                "expression": "{ [region].[region].&[EMEA\\Central] }",
            },
        ]
        result = _inline_named_sets(mdx, sets)
        assert "EMEA\\Central" in result
        assert "[Key Regions]" not in result

    def test_backslash_digit_in_expression_is_literal(self):
        """``\\1`` in a set expression must appear literally in the output,
        not be interpreted as a regex group reference."""
        mdx = "SELECT {[Measures].[Rev]} ON COLUMNS, {[TestSet]} ON ROWS FROM [Model]"
        sets = [
            {
                "name": "TestSet",
                "expression": "{ [dim].[dim].&[code\\1value] }",
            },
        ]
        result = _inline_named_sets(mdx, sets)
        assert "code\\1value" in result
        assert "[TestSet]" not in result

    def test_bare_name_backslash_replacement(self):
        """The bare (unbracketed) name path must also handle backslashes."""
        mdx = "SELECT {[Measures].[Rev]} ON COLUMNS, MySet ON ROWS FROM [Model]"
        sets = [
            {
                "name": "MySet",
                "expression": "{ [dim].[dim].&[DOMAIN\\user] }",
            },
        ]
        result = _inline_named_sets(mdx, sets)
        assert "DOMAIN\\user" in result
        assert "MySet" not in result.replace("DOMAIN\\user", "")

    def test_no_match_with_backslash_expression_still_safe(self):
        """Even when the MDX contains NO reference to the set, the
        expression's backslashes must not cause a crash (the old code
        eagerly parsed the template regardless of match count)."""
        mdx = "SELECT {[Measures].[Revenue]} ON COLUMNS FROM [Model]"
        sets = [
            {
                "name": "Unused Set",
                "expression": "{ [region].[region].&[EMEA\\Central] }",
            },
        ]
        # Must not raise re.error
        result = _inline_named_sets(mdx, sets)
        assert result == mdx  # no replacement occurred

    def test_dollar_sign_in_expression(self):
        """Dollar signs must be treated literally, not as regex backrefs."""
        mdx = "SELECT {[Measures].[Rev]} ON COLUMNS, {[PriceSet]} ON ROWS FROM [Model]"
        sets = [
            {
                "name": "PriceSet",
                "expression": "Filter([Product].Members, [Measures].[Price] > $100)",
            },
        ]
        result = _inline_named_sets(mdx, sets)
        assert "$100" in result
        assert "[PriceSet]" not in result
