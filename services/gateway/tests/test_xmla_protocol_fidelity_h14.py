"""H14 — XMLA/MDX gateway protocol fidelity.

Bug-1060: an unresolvable / unapplied WHERE slicer must fail loud, never run
          the query unfiltered.
Bug-1067: a persona-excluded measure must fail with a clean fault, not forward
          raw MDX downstream.
F-002-02: subtotal / grand-total queries must honour label filters.
"""
from __future__ import annotations

import pytest

from src.dax.xmla_server import (
    _mdx_to_sql,
    _assert_where_members_applied,
    _collapse_flat_lne_rows,
    _extract_label_filter_specs,
    _label_filter_to_sql,
)


_MEASURES = [{"name": "transaction_amount", "default_agg": "sum"}]
_DIMS = [
    {"name": "country"},
    {"name": "business_date_month"},
    {"name": "business_date_year"},
]


# ---------------------------------------------------------------------------
# Bug-1060 — fail loud on an unresolvable / dropped WHERE slicer
# ---------------------------------------------------------------------------


def test_where_on_nonexistent_dimension_raises():
    mdx = (
        "SELECT {[Measures].[transaction_amount]} ON COLUMNS "
        "FROM [ModelX] WHERE ([nonexistent_dim].[nonexistent_dim].&[99])"
    )
    with pytest.raises(ValueError, match="unknown dimension or hierarchy"):
        _mdx_to_sql(mdx, _MEASURES, _DIMS, model_slug="modelx")


def test_canonical_two_part_where_slicer_still_filters():
    """The well-formed two-part form must continue to apply (no false reject)."""
    mdx = (
        "SELECT {[Measures].[transaction_amount]} ON COLUMNS "
        "FROM [ModelX] WHERE ([business_date_month].[business_date_month].&[4])"
    )
    sql, protocol = _mdx_to_sql(mdx, _MEASURES, _DIMS, model_slug="modelx")
    assert '"business_date_month"' in sql
    assert "= '4'" in sql or "= '4'" in sql.replace('"', "")


def test_single_bracket_attribute_form_fails_loud_not_silent():
    """The single-bracket form the extractor cannot capture must fail loud."""
    mdx = (
        "SELECT {[Measures].[transaction_amount]} ON COLUMNS "
        "FROM [ModelX] WHERE ([business_date_month].&[4])"
    )
    with pytest.raises(ValueError):
        _mdx_to_sql(mdx, _MEASURES, _DIMS, model_slug="modelx")


def test_all_member_where_does_not_raise():
    where_filters: dict[str, list[str]] = {}
    # An [All] member produces no filter and must not be flagged.
    _assert_where_members_applied(
        "[country].[country].[All]", where_filters, {"country"},
    )


# ---------------------------------------------------------------------------
# Bug-1067 — persona-excluded measure fails loud, no raw-MDX passthrough
# ---------------------------------------------------------------------------


def test_persona_excluded_measure_raises_clean_fault():
    """transaction_amount is NOT in the persona's measure metadata."""
    persona_measures = [{"name": "avg_base_amount", "default_agg": "avg"}]
    mdx = "SELECT {[Measures].[transaction_amount]} ON COLUMNS FROM [modely]"
    with pytest.raises(ValueError, match="not available to this persona"):
        _mdx_to_sql(mdx, persona_measures, _DIMS, model_slug="modely")


def test_persona_allowed_measure_translates():
    persona_measures = [{"name": "avg_base_amount", "default_agg": "avg"}]
    mdx = "SELECT {[Measures].[avg_base_amount]} ON COLUMNS FROM [modely]"
    sql, protocol = _mdx_to_sql(mdx, persona_measures, _DIMS, model_slug="modely")
    assert protocol == "jdbc"
    assert "avg_base_amount" in sql.lower()


# ---------------------------------------------------------------------------
# F-002-02 — label filters reach the subtotal/grain queries
# ---------------------------------------------------------------------------


def test_label_filter_extracted_and_compiled():
    axis = (
        '{Filter([country].[country].Members, '
        'Left([country].[country].CurrentMember.Name, 1) = "N")}'
    )
    specs = _extract_label_filter_specs(
        axis, {"country"}, {}, {},
    )
    assert len(specs) == 1
    sql = _label_filter_to_sql(specs[0], lambda n: f'"{n}"')
    assert 'LOWER("country")' in sql
    assert "LIKE 'n%'" in sql


# ---------------------------------------------------------------------------
# F-002-03 — Show Values As computed over the correct (detail) grain
# ---------------------------------------------------------------------------


def test_pct_grand_total_ignores_subtotal_rows():
    from src.dax.mdx_calc_members import CalcMember, evaluate_calc_members
    from src.dax.subtotal_engine import SUBTOTAL_LEVEL_KEY, SUBTOTAL_GRAIN_KEY

    calc = CalcMember(
        name="PctTotal", expression="x", calc_type="pct_grand_total",
        base_measure="Amount",
    )
    # detail leaves France 300, Germany 500, UK 200 (true total 1000), plus a
    # region subtotal (1000) and a grand total (1000) — denominator would be
    # 3000 without grain partitioning, halving every percentage.
    rows = [
        {"Region": "France", "Amount": 300, SUBTOTAL_LEVEL_KEY: "detail", SUBTOTAL_GRAIN_KEY: 2},
        {"Region": "Germany", "Amount": 500, SUBTOTAL_LEVEL_KEY: "detail", SUBTOTAL_GRAIN_KEY: 2},
        {"Region": "UK", "Amount": 200, SUBTOTAL_LEVEL_KEY: "detail", SUBTOTAL_GRAIN_KEY: 2},
        {"Region": "EU", "Amount": 1000, SUBTOTAL_LEVEL_KEY: "region", SUBTOTAL_GRAIN_KEY: 0},
        {"Region": "", "Amount": 1000, SUBTOTAL_LEVEL_KEY: "grand_total", SUBTOTAL_GRAIN_KEY: -1},
    ]
    evaluate_calc_members([calc], rows, ["Amount"], ["Region"])

    assert rows[0]["PctTotal"] == pytest.approx(0.30)
    assert rows[1]["PctTotal"] == pytest.approx(0.50)
    assert rows[2]["PctTotal"] == pytest.approx(0.20)
    # Subtotal / grand-total rows carry no Show-Values-As value.
    assert rows[3]["PctTotal"] is None
    assert rows[4]["PctTotal"] is None


# ---------------------------------------------------------------------------
# F-002-04 — LAST_NON_EMPTY returns the last NON-EMPTY value, not the SUM
# ---------------------------------------------------------------------------


def test_lne_skips_null_latest_period():
    from src.dax.subtotal_engine import _last_non_empty_value

    # Latest period (Feb) is empty -> must fall back to Jan's 100, not SUM.
    group = [
        {"period": "2024-01", "Balance": 100},
        {"period": "2024-02", "Balance": None},
    ]
    val = _last_non_empty_value(group, "Balance", "period")
    assert val == 100


def test_lne_returns_latest_when_present():
    from src.dax.subtotal_engine import _last_non_empty_value

    group = [
        {"period": "2024-01", "Balance": 100},
        {"period": "2024-02", "Balance": 120},
    ]
    val = _last_non_empty_value(group, "Balance", "period")
    assert val == 120


# ---------------------------------------------------------------------------
# Bug-3707 (consequence #1) — FLAT pivot of an LNE measure (no time dim on an
# axis) must return the last-non-empty period value, NOT the SUM across periods.
# Additive-measure flat pivots are left completely unchanged.
# ---------------------------------------------------------------------------


_BALANCE_META = [{
    "name": "Balance",
    "default_agg": "last_non_empty",
    "semi_additive_behavior": "last_non_empty",
}]


def test_flat_lne_collapses_to_last_non_empty_per_grain():
    """A flat pivot Country x Balance (no period on an axis): the gateway
    injects the hidden time grain (``period``), then collapses each country
    group to its latest non-empty period — 250 for France (Feb), not
    100+250=350. Driven through the canonical ``_collapse_flat_lne_rows``."""
    period_rows = [
        {"country": "France", "period": "2024-01", "Balance": 100},
        {"country": "France", "period": "2024-02", "Balance": 250},
        {"country": "Germany", "period": "2024-01", "Balance": 80},
        {"country": "Germany", "period": "2024-02", "Balance": None},
    ]
    columns, collapsed = _collapse_flat_lne_rows(
        columns=["country", "period", "Balance"],
        rows=period_rows,
        hidden_time_dim="period",
        lne_measures=["Balance"],
        measures_meta=_BALANCE_META,
    )
    by_country = {r["country"]: r["Balance"] for r in collapsed}
    assert by_country["France"] == 250   # latest period present
    assert by_country["Germany"] == 80   # latest period empty -> previous
    # The temporal dim is dropped from the collapsed grain.
    assert "period" not in columns
    assert all("period" not in r for r in collapsed)


def test_flat_lne_no_other_dimension_collapses_to_single_value():
    """A pure flat LNE pivot (Balance only) collapses to one last-non-empty
    value, not the SUM across all periods."""
    period_rows = [
        {"period": "2024-01", "Balance": 100},
        {"period": "2024-02", "Balance": 250},
        {"period": "2024-03", "Balance": None},
    ]
    _columns, collapsed = _collapse_flat_lne_rows(
        columns=["period", "Balance"],
        rows=period_rows,
        hidden_time_dim="period",
        lne_measures=["Balance"],
        measures_meta=_BALANCE_META,
    )
    assert len(collapsed) == 1
    assert collapsed[0]["Balance"] == 250


def test_flat_additive_measure_is_untouched_by_lne_collapse():
    """Strict scoping: an additive measure (no LNE measure in the query) must
    not be collapsed — with no hidden time dim and no LNE measure,
    ``_collapse_flat_lne_rows`` is a no-op."""
    rows = [
        {"country": "France", "Amount": 100},
        {"country": "Germany", "Amount": 200},
    ]
    columns, out = _collapse_flat_lne_rows(
        columns=["country", "Amount"],
        rows=rows,
        hidden_time_dim=None,
        lne_measures=[],
        measures_meta=[{"name": "Amount", "default_agg": "sum"}],
    )
    assert out == rows
    assert columns == ["country", "Amount"]


def test_flat_lne_injects_hidden_time_grain_into_sql():
    """The canonical engine repairs a flat LNE pivot by injecting the hidden
    time-grain dimension into the GROUP BY of the detail SQL (replacing the
    retired re-grain query builder). Country x Balance with a time dimension
    ``period`` in metadata groups by both, so per-period values are available
    for the Python collapse."""
    measures = [{
        "name": "Balance",
        "default_agg": "last_non_empty",
        "semi_additive_behavior": "last_non_empty",
    }]
    dims = [
        {"name": "country"},
        {
            "name": "period",
            "dimension_kind": "time",
            "is_time_dim": True,
            "time_grain": "month",
        },
    ]
    mdx = (
        "SELECT {[Measures].[Balance]} ON COLUMNS, "
        "{[country].[country].Members} ON ROWS FROM [modelx]"
    )
    sql, _protocol = _mdx_to_sql(mdx, measures, dims, model_slug="modelx")
    assert '"country"' in sql
    assert '"period"' in sql
    assert "GROUP BY" in sql
    # The hidden time grain is grouped so per-period values are available.
    assert 'GROUP BY "country", "period"' in sql


# ---------------------------------------------------------------------------
# Branch-level scoping + fail-loud, driven through _handle_execute.
#
# The helper-function tests above pin the collapse + SQL injection in isolation.
# These three pin the actual DECISION SEAM in xmla_server._handle_execute: that
# an LNE flat pivot triggers the hidden-time-grain repair, an additive flat
# pivot does NOT, and an LNE flat pivot with no time/date dimension FAILS LOUD
# with a SOAP fault rather than silently SUMming.
# ---------------------------------------------------------------------------


import asyncio  # noqa: E402

from defusedxml import ElementTree as _ET  # noqa: E402

from src.dax import xmla_server as _xmla  # noqa: E402


def _exec_method(stmt: str, catalog: str = "modely"):
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">'
        '<soap:Body>'
        '<Execute xmlns="urn:schemas-microsoft-com:xml-analysis">'
        f'<Command><Statement>{stmt}</Statement></Command>'
        f'<Properties><PropertyList><Catalog>{catalog}</Catalog>'
        '</PropertyList></Properties>'
        '</Execute></soap:Body></soap:Envelope>'
    )
    root = _ET.fromstring(body)
    el = _xmla._find_method(root)
    assert el is not None
    return el


def _patch_flat_lne_model(
    monkeypatch,
    *,
    measures,
    with_time_unit: bool,
    captured: dict,
):
    """Stub the model-metadata fetches + execute_query for a flat pivot.

    Dimensions: ``country`` (pivot grain) and ``txn_date`` (the date column).
    ``txn_date`` is tagged as a TIME dimension only when *with_time_unit* is
    True — that is what the canonical engine reads to identify the hidden time
    grain. ``execute_query`` records the SQL it is asked to run so the test can
    assert whether the hidden-time-grain repair was applied.
    """

    async def fake_resolve_model_id(catalog, tenant_slug, jwt_token):
        return "model-1", "project-1", None

    async def fake_get_model_measures(model_id, tenant_slug, jwt_token, project_id=""):
        return measures

    async def fake_get_model_dimensions(model_id, tenant_slug, jwt_token, project_id=""):
        date_dim = {"name": "txn_date", "source_column_id": "col-date"}
        if with_time_unit:
            date_dim.update({
                "dimension_kind": "time",
                "is_time_dim": True,
                "time_grain": "month",
            })
        return [
            {"name": "country", "source_column_id": "col-country"},
            date_dim,
        ]

    async def fake_get_model_hierarchies(
        model_id, tenant_slug, jwt_token, project_id="", include_details=True,
    ):
        day_level = {
            "ordinal": 0,
            "name": "Day",
            "key_attribute": {"id": "col-date", "source": "physical_column"},
        }
        if with_time_unit:
            day_level["time_unit"] = "day"
        return [{"id": "h-date", "name": "DateHier", "levels": [day_level]}]

    async def fake_execute_query(
        model_id, sql, tenant_slug, jwt_token, protocol="dax", **kwargs,
    ):
        captured.setdefault("sql", []).append(sql)
        # A flat Country pivot of the measure(s); one period column appears only
        # when the LNE re-grain widened the grain with txn_date.
        if '"txn_date"' in sql:
            return {
                "columns": ["country", "txn_date", "account_balance"],
                "rows": [
                    {"country": "FR", "txn_date": "2024-01", "account_balance": 100},
                    {"country": "FR", "txn_date": "2024-02", "account_balance": 250},
                ],
            }
        return {
            "columns": ["country", measures[0]["name"]],
            "rows": [{"country": "FR", measures[0]["name"]: 350}],
        }

    monkeypatch.setattr(_xmla, "_resolve_model_id", fake_resolve_model_id)
    monkeypatch.setattr(_xmla, "get_model_measures", fake_get_model_measures)
    monkeypatch.setattr(_xmla, "get_model_dimensions", fake_get_model_dimensions)
    monkeypatch.setattr(_xmla, "get_model_hierarchies", fake_get_model_hierarchies)
    monkeypatch.setattr(_xmla, "execute_query", fake_execute_query)


def test_flat_lne_pivot_enters_regrain_branch(monkeypatch):
    """An LNE measure on a flat pivot (no date on an axis) triggers the
    hidden-time-grain repair: ``_mdx_to_sql`` injects the time dimension
    (``txn_date``) into the GROUP BY, execute_query is asked for that grouped
    query, and the response collapses to the last-non-empty value (250, the
    Feb balance) — NOT the additive SUM (350)."""
    captured: dict = {}
    _patch_flat_lne_model(
        monkeypatch,
        measures=[{
            "name": "account_balance",
            "default_agg": "sum",
            "semi_additive_behavior": "last_non_empty",
        }],
        with_time_unit=True,
        captured=captured,
    )
    stmt = (
        "SELECT {[Measures].[account_balance]} ON COLUMNS, "
        "{[country].[country].Members} ON ROWS FROM [modely]"
    )
    resp = asyncio.run(
        _xmla._handle_execute(
            _exec_method(stmt), tenant_slug="demo",
            jwt_token="tok", session_id="lne-1",
        )
    )
    body = resp.body.decode("utf-8")
    assert resp.status_code == 200
    assert "<soap11env:Fault>" not in body and "<Fault>" not in body
    # The detail query that groups by the hidden time grain was issued.
    assert any('"txn_date"' in s for s in captured["sql"]), captured["sql"]
    # Last-non-empty collapse, not the additive SUM (values render as doubles,
    # e.g. 250.0; assert on the numeric prefix, format-tolerant).
    assert "<Value" in body
    assert ">250" in body
    assert ">350" not in body


def test_flat_additive_pivot_does_not_enter_regrain_branch(monkeypatch):
    """Strict scoping at the decision seam: an ADDITIVE-only flat pivot never
    triggers the hidden-time-grain repair — execute_query is never asked for a
    time-dim-grouped query, and the additive aggregate is returned
    unchanged."""
    captured: dict = {}
    _patch_flat_lne_model(
        monkeypatch,
        measures=[{"name": "base_amount", "default_agg": "sum"}],
        with_time_unit=True,
        captured=captured,
    )
    stmt = (
        "SELECT {[Measures].[base_amount]} ON COLUMNS, "
        "{[country].[country].Members} ON ROWS FROM [modely]"
    )
    resp = asyncio.run(
        _xmla._handle_execute(
            _exec_method(stmt), tenant_slug="demo",
            jwt_token="tok", session_id="add-1",
        )
    )
    body = resp.body.decode("utf-8")
    assert resp.status_code == 200
    assert "<soap11env:Fault>" not in body and "<Fault>" not in body
    # No re-grain query: the temporal dim was never injected into any SQL.
    assert not any('"txn_date"' in s for s in captured["sql"]), captured["sql"]


def test_flat_lne_pivot_without_time_unit_fails_loud(monkeypatch):
    """An LNE flat pivot whose model carries NO time/date dimension cannot
    identify the period to take "last non-empty" over. The canonical engine's
    ``_mdx_to_sql`` guard must return a Client SOAP fault, NOT silently SUM
    across periods."""
    captured: dict = {}
    _patch_flat_lne_model(
        monkeypatch,
        measures=[{
            "name": "account_balance",
            "default_agg": "sum",
            "semi_additive_behavior": "last_non_empty",
        }],
        with_time_unit=False,
        captured=captured,
    )
    stmt = (
        "SELECT {[Measures].[account_balance]} ON COLUMNS, "
        "{[country].[country].Members} ON ROWS FROM [modely]"
    )
    resp = asyncio.run(
        _xmla._handle_execute(
            _exec_method(stmt), tenant_slug="demo",
            jwt_token="tok", session_id="lne-fail-1",
        )
    )
    body = resp.body.decode("utf-8")
    # Fail loud — a SOAP fault, not a silently-SUMmed 200 result.
    assert "Fault" in body
    lowered = body.lower()
    assert (
        "last non empty" in lowered
        or "last-non-empty" in lowered
        or "non-empty period" in lowered
        or "date/time grain" in lowered
    )
    # And it failed BEFORE running any detail query.
    assert not any('"txn_date"' in s for s in captured.get("sql", []))


def test_flat_lne_with_count_distinct_companion_fails_loud():
    measures = [
        {
            "name": "ending_balance",
            "default_agg": "last_non_empty",
            "semi_additive_behavior": "last_non_empty",
        },
        {"name": "customers", "default_agg": "count_distinct"},
    ]
    dims = [
        {"name": "region"},
        {
            "name": "business_month",
            "dimension_kind": "time",
            "is_time_dim": True,
            "time_grain": "month",
        },
    ]
    mdx = (
        "SELECT {[Measures].[ending_balance], [Measures].[customers]} ON COLUMNS, "
        "{[region].[region].Members} ON ROWS FROM [modelx]"
    )

    with pytest.raises(ValueError, match="cannot be combined"):
        _mdx_to_sql(mdx, measures, dims, model_slug="modelx")


def test_flat_lne_empty_result_strips_hidden_time_grain():
    columns, rows = _collapse_flat_lne_rows(
        columns=["region", "business_month", "ending_balance"],
        rows=[],
        hidden_time_dim="business_month",
        lne_measures=["ending_balance"],
        measures_meta=[
            {
                "name": "ending_balance",
                "default_agg": "last_non_empty",
                "semi_additive_behavior": "last_non_empty",
            }
        ],
    )

    assert columns == ["region", "ending_balance"]
    assert rows == []
