"""Bug-6702: MDSCHEMA_KPIS must advertise an EXECUTABLE KPI_VALUE member so the
catalogue surface and the XMLA Execute measure set can never disagree.

Live defect (acme-demo modely, gateway log 2026-07-07 22:10:07 UTC):
MDSCHEMA_KPIS advertised the deployed "Net Revenue" KPI as
``KPI_VALUE = [Measures].[[KPI] Net Revenue]`` (a synthetic inline column that is
NEVER present in the XMLA measure set — inline KPI columns are a JDBC-catalogue
construct only). When Excel queried that advertised member, Execute extracted the
truncated token ``[KPI`` and failed loud:
``Measure not available to this persona: [KPI``.

Root fix: both surfaces resolve KPI_VALUE through the SAME resolver
(``resolve_kpi_property_expr``) against the SAME executable measure set:
- ``value_measure_id`` -> ``[Measures].[<measure>]``
- a single-measure v2 expression (``measure("net_amount")``) -> ``[Measures].[net_amount]``
- a composite expression (no single executable measure) -> ``""`` (undefined),
  never a member Execute cannot resolve.

The modely "Net Revenue" KPI is ``measure("net_amount")`` (value_measure_id NULL,
expose_kpis_inline false) — the single-measure case.
"""
from __future__ import annotations

import pytest

from src.dax import mdschema
from src.dax import xmla_server as xs
from src.dax.mdx_execute import resolve_kpi_property_expr
from src.dax.xmla_server import _maybe_resolve_kpi_members, _mdx_to_sql

CATALOG = "modely"

# The executable measure set the XMLA Execute path sees for modely.
MEASURES = [
    {"id": "m_net", "name": "net_amount", "default_agg": "sum", "display_name": "net amount"},
    {"id": "m_gross", "name": "gross_amount", "default_agg": "sum"},
]

# The deployed acme-demo modely "Net Revenue" KPI: v2 expression, no value
# measure id, single-measure expression, no inline exposure.
NET_REVENUE_KPI = {
    "id": "kpi_net_rev",
    "name": "Net Revenue",
    "display_name": "Net Revenue",
    "description": "",
    "display_folder": "",
    "expression": 'measure("net_amount")',
    "value_measure_id": None,
    "goal_measure_id": None,
    "target_type": "static",
    "target_value": None,
    "target_expression": None,
    "status_expression": "",
    "trend_expression": "",
    "weight": None,
    "parent_kpi_id": None,
    "presentation_type": None,
    "kpi_type": "custom",
}

# A composite KPI whose value has no single executable measure member.
COMPOSITE_KPI = {
    **NET_REVENUE_KPI,
    "id": "kpi_margin",
    "name": "Gross Margin %",
    "display_name": "Gross Margin %",
    "expression": 'safe_div(measure("net_amount"), measure("gross_amount"))',
}


def test_mdschema_advertises_executable_value_member():
    row = mdschema._rows_kpis(CATALOG, [NET_REVENUE_KPI], MEASURES)[0]
    # NOT the old synthetic [Measures].[[KPI] Net Revenue].
    assert row["KPI_VALUE"] == "[Measures].[net_amount]"
    assert "[KPI]" not in row["KPI_VALUE"]
    # KPI_STATUS (raw-value mode) mirrors the same executable member.
    assert row["KPI_STATUS"] == "[Measures].[net_amount]"


def test_catalogue_and_execute_use_the_same_resolver():
    """The advertised member must be exactly what the Execute resolver produces
    from the same executable measure set — they can never disagree."""
    advertised = mdschema._rows_kpis(CATALOG, [NET_REVENUE_KPI], MEASURES)[0]["KPI_VALUE"]
    executed = resolve_kpi_property_expr(NET_REVENUE_KPI, "KPIValue", MEASURES)
    assert advertised == executed == "[Measures].[net_amount]"


def test_advertised_member_is_executable_by_the_execute_path():
    """A query against the advertised KPI value member translates to SQL and does
    NOT raise the 'Measure not available to this persona' fault."""
    advertised = mdschema._rows_kpis(CATALOG, [NET_REVENUE_KPI], MEASURES)[0]["KPI_VALUE"]
    mdx = (
        f"SELECT {{{advertised}}} ON COLUMNS, "
        "{[account_type].[account_type].[account_type].Members} ON ROWS "
        "FROM [modely]"
    )
    sql, protocol = _mdx_to_sql(mdx, MEASURES, [{"name": "account_type"}])
    assert protocol == "jdbc"
    assert 'SUM("net_amount")' in sql


def test_old_synthetic_member_would_not_be_executable():
    """Guard: prove WHY the old advertising was broken — the synthetic inline
    member fails loud on Execute, which is exactly why it must never be
    advertised on the XMLA surface."""
    mdx = (
        "SELECT {[Measures].[[KPI] Net Revenue]} ON COLUMNS "
        "FROM [modely]"
    )
    with pytest.raises(ValueError, match="not available to this persona"):
        _mdx_to_sql(mdx, MEASURES, [{"name": "account_type"}])


def test_composite_kpi_advertises_no_value_member():
    """A composite expression has no single executable measure member, so the
    catalogue advertises "" — never a member Execute cannot resolve."""
    row = mdschema._rows_kpis(CATALOG, [COMPOSITE_KPI], MEASURES)[0]
    assert row["KPI_VALUE"] == ""
    assert row["KPI_STATUS"] == ""


# ---------------------------------------------------------------------------
# Codex R2 finding 2: hidden-backed KPIs must not diverge between surfaces.
# The Discover path trims is_hidden measures before _rows_kpis (catalogue
# advertises KPI_VALUE="" for a hidden-backed KPI on a non-technical catalog);
# the Execute KPI member-function path must apply the SAME visibility rule so
# KPIValue() fails loud exactly where the catalogue advertises no value member,
# and a technical-view catalog keeps the KPI working on BOTH surfaces.
# ---------------------------------------------------------------------------

HIDDEN_MEASURES = [
    {"id": "m_net", "name": "net_amount", "default_agg": "sum", "is_hidden": True},
    {"id": "m_gross", "name": "gross_amount", "default_agg": "sum"},
]


def _kpi_call_kwargs(*, is_technical_view: bool):
    return dict(
        statement='SELECT FROM [modely] WHERE (KPIValue("Net Revenue"))',
        model_id="mid", project_id="pid", tenant_slug="acme-demo",
        jwt_token="jwt", measures_meta=list(HIDDEN_MEASURES),
        dimensions_meta=[{"name": "account_type"}],
        hierarchy_level_dim_map={}, hierarchy_default_dim_map={},
        dim_names={"account_type"}, model_slug="modely",
        persona_id=None, is_technical_view=is_technical_view,
        persona_included_measure_ids=None,
    )


def test_catalogue_advertises_no_value_for_hidden_backed_kpi():
    """Discover half: with the is_hidden trim applied (as _handle_discover does
    before _rows_kpis), a hidden-backed KPI advertises no value member."""
    visible = [m for m in HIDDEN_MEASURES if not m.get("is_hidden")]
    row = mdschema._rows_kpis(CATALOG, [NET_REVENUE_KPI], visible)[0]
    assert row["KPI_VALUE"] == ""


async def test_execute_kpivalue_refuses_hidden_backed_kpi(monkeypatch):
    """Execute half: KPIValue() on a hidden-backed KPI must fail loud on a
    non-technical catalog — the same 'no value member' the catalogue shows —
    never silently resolve and run the hidden backing measure."""
    async def _fake_kpis(*a, **k):
        return [dict(NET_REVENUE_KPI)]
    monkeypatch.setattr(xs, "get_model_kpis", _fake_kpis)

    with pytest.raises(ValueError, match="no resolvable value measure"):
        await _maybe_resolve_kpi_members(**_kpi_call_kwargs(is_technical_view=False))


async def test_execute_kpivalue_resolves_hidden_backed_kpi_for_technical_view(monkeypatch):
    """A technical-view catalog keeps hidden measures on BOTH surfaces, so the
    same KPI resolves through the governed authority there (surfaces stay aligned,
    not blanket-off). Bug-6608 un-gated: the live value comes from
    ``evaluate_kpi_governed``, not the gateway's own measure SQL."""
    async def _fake_kpis(*a, **k):
        return [dict(NET_REVENUE_KPI)]

    from unittest.mock import AsyncMock

    governed = AsyncMock(return_value={"value": 42.5, "status": 1})
    monkeypatch.setattr(xs, "get_model_kpis", _fake_kpis)
    monkeypatch.setattr(xs, "evaluate_kpi_governed", governed)

    result = await _maybe_resolve_kpi_members(**_kpi_call_kwargs(is_technical_view=True))
    assert result is not None
    columns, rows = result
    assert columns == ["Net Revenue (Value)"]
    assert rows[0]["Net Revenue (Value)"] == 42.5
    governed.assert_awaited_once()
