"""Bug-6888: static KPI targets must be addable from Excel's KPI field list.

Live defect (acme-demo modely, 2026-07-10): MDSCHEMA_KPIS advertised
``KPI_GOAL = 188914000.0`` — a bare scalar, not a member unique name. Excel can
only add real members to a pivot, so the KPI's Target checkbox was unusable
(Value worked because it resolves to ``[Measures].[net_amount]``).

Root fix (SSAS convention): a static goal is advertised as the synthetic
support member ``[Measures].[<KPI caption> Goal]``; MDSCHEMA_MEASURES carries a
matching hidden support-measure row; the Execute path drops the member from
SQL resolution and post-joins the constant (same contract as Info measures).
Measure-typed goals already resolve to a real member and are unchanged.

Test escape: rowset content was pinned, but no test asserted the ADVERTISED
goal is a member unique name. Guard: assertions below. Tier: T1.
"""
from __future__ import annotations

from src.dax import mdschema
from src.dax.xmla_server import _mdx_to_sql

CATALOG = "modely"

MEASURES = [
    {"id": "m_net", "name": "net_amount", "default_agg": "sum"},
    {"id": "m_target", "name": "revenue_target", "default_agg": "sum"},
]

STATIC_GOAL_KPI = {
    "id": "kpi_net_rev",
    "name": "Net Revenue",
    "display_name": "Net Revenue",
    "expression": 'measure("net_amount")',
    "value_measure_id": None,
    "goal_measure_id": None,
    "target_type": "static",
    "target_value": 188914000.0,
    "status_expression": "",
    "trend_expression": "",
    "parent_kpi_id": None,
    "presentation_type": None,
}

MEASURE_GOAL_KPI = {
    **STATIC_GOAL_KPI,
    "id": "kpi_measure_goal",
    "name": "Net vs Target",
    "display_name": "Net vs Target",
    "target_type": "measure",
    "target_measure_id": "m_target",
    "target_value": None,
}


def test_static_goal_advertised_as_support_member():
    row = mdschema._rows_kpis(CATALOG, [STATIC_GOAL_KPI], MEASURES)[0]
    assert row["KPI_GOAL"] == "[Measures].[Net Revenue Goal]"


def test_measure_goal_still_advertises_real_member():
    row = mdschema._rows_kpis(CATALOG, [MEASURE_GOAL_KPI], MEASURES)[0]
    assert row["KPI_GOAL"] == "[Measures].[revenue_target]"


def test_synthetic_support_measures_built_for_static_goal_only():
    synth = mdschema.kpi_goal_synthetic_measures(
        [STATIC_GOAL_KPI, MEASURE_GOAL_KPI], MEASURES,
    )
    assert [m["name"] for m in synth] == ["Net Revenue Goal"]
    assert synth[0]["xmla_support_measure"] is True


def test_support_measure_row_emitted_invisible():
    synth = mdschema.kpi_goal_synthetic_measures([STATIC_GOAL_KPI], MEASURES)
    rows = mdschema._rows_measures(CATALOG, MEASURES + synth)
    by_name = {r["MEASURE_NAME"]: r for r in rows}
    assert "Net Revenue Goal" in by_name
    assert by_name["Net Revenue Goal"]["MEASURE_IS_VISIBLE"] == "false"
    assert by_name["net_amount"]["MEASURE_IS_VISIBLE"] == "true"


def test_goal_static_value_resolves_scalar():
    assert mdschema.kpi_goal_static_value(
        STATIC_GOAL_KPI, {m["id"]: m for m in MEASURES},
    ) == "188914000.0"
    assert mdschema.kpi_goal_static_value(
        MEASURE_GOAL_KPI, {m["id"]: m for m in MEASURES},
    ) == ""


def test_mdx_translator_drops_constant_goal_member():
    """Mixed MDX with the support member must translate without fault; the
    goal column is post-joined by the caller."""
    mdx = (
        "SELECT {[Measures].[Net Revenue Goal],[Measures].[net_amount]} "
        "ON COLUMNS FROM [modely]"
    )
    sql, _ = _mdx_to_sql(
        mdx, MEASURES, [{"name": "customer_segment"}],
        model_slug="modely",
        constant_measure_names={"Net Revenue Goal"},
    )
    assert "net_amount" in sql
    assert "Net Revenue Goal" not in sql
