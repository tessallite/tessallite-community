"""Bug-3657 — KPI member-function recognition and resolution.

Covers token detection (dax_parser.find_kpi_member_functions) and property
resolution to executable MDX scalar expressions (mdx_execute.resolve_kpi_property_expr).

Bug-6608 (un-gated 2026-07-21): the LIVE KPI member-function path
(``_maybe_resolve_kpi_members``) now resolves value/goal/status/trend through the
SINGLE governed model-service ``/evaluate`` authority (same as the SPA scorecard and
the Excel custom function). KPIStatus returns the governed −1/0/1 RAG verdict; the
gateway does NOT re-derive a band verdict of its own. MDSCHEMA_KPIS still advertises
an ADDRESSABLE member for KPI_STATUS (authored expression, else the value member).
These tests assert that contract.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src.dax.dax_parser import find_kpi_member_functions, KPI_MEMBER_FUNCTIONS
from src.dax.mdx_execute import resolve_kpi_property_expr
from src.dax import xmla_server


_MEASURES = [
    {"id": "m-fee", "name": "fee_amount", "default_agg": "sum"},
    {"id": "m-base", "name": "base_amount", "default_agg": "sum"},
]

# KPI "aa": simple_measure, value=fee_amount, static goal 10000, higher is better.
_KPI_AA = {
    "name": "aa",
    "display_name": "aa",
    "value_measure_id": "m-fee",
    "target_type": "static",
    "target_value": 10000.0,
    "direction": "higher_is_better",
    "status_expression": None,
    "trend_expression": None,
}


# ---------------------------------------------------------------------------
# Token recognition
# ---------------------------------------------------------------------------

def test_find_all_four_functions():
    for fn in KPI_MEMBER_FUNCTIONS:
        stmt = f'SELECT FROM [modely] WHERE ({fn}("aa"))'
        found = find_kpi_member_functions(stmt)
        assert len(found) == 1
        _, name, caption = found[0]
        assert name == fn
        assert caption == "aa"


def test_find_none_when_absent():
    assert find_kpi_member_functions(
        "SELECT {[Measures].[fee_amount]} ON COLUMNS FROM [modely]"
    ) == []


def test_find_case_insensitive_and_single_quotes():
    found = find_kpi_member_functions("WHERE (kpistatus('aa'))")
    assert len(found) == 1
    assert found[0][1] == "KPIStatus"
    assert found[0][2] == "aa"


def test_find_multiple_in_order():
    stmt = '{KPIValue("aa"), KPIGoal("aa")} ON COLUMNS'
    found = find_kpi_member_functions(stmt)
    assert [f[1] for f in found] == ["KPIValue", "KPIGoal"]


def test_bug8383_range_union_is_refused_before_batch():
    """Bug-8383/L1-R1-005: range-plus-member cannot become an AND filter."""
    where_expr = (
        "[Calendar].[Calendar].[Month].&[4]:"
        "[Calendar].[Calendar].[Month].&[6], "
        "[Calendar].[Calendar].[Month].&[8]"
    )
    where_filters = xmla_server._mdx_extract_where_filters(
        where_expr, {"Calendar"},
    )
    with pytest.raises(ValueError, match="range union"):
        xmla_server._translate_kpi_slicer_filters(
            where_expr,
            where_filters,
            [{"id": "dim-calendar", "name": "Calendar"}],
            {"Calendar"},
        )


# ---------------------------------------------------------------------------
# Property resolution
# ---------------------------------------------------------------------------

def test_resolve_value_to_measure_ref():
    assert resolve_kpi_property_expr(_KPI_AA, "KPIValue", _MEASURES) == \
        "[Measures].[fee_amount]"


def test_resolve_goal_static_literal():
    assert resolve_kpi_property_expr(_KPI_AA, "KPIGoal", _MEASURES) == "10000.0"


def test_resolve_status_metadata_member_not_case():
    # The KPI_STATUS metadata member is an ADDRESSABLE value member, never a band
    # CASE. The live −1/0/1 verdict comes from the governed authority, not this
    # string (Bug-6608 un-gated).
    expr = resolve_kpi_property_expr(_KPI_AA, "KPIStatus", _MEASURES)
    assert expr == "[Measures].[fee_amount]"
    assert not expr.strip().upper().startswith("CASE")


def test_resolve_status_metadata_member_ignores_direction():
    # Direction does not change the advertised metadata member (the verdict is
    # governed live, not encoded in the metadata string).
    kpi = dict(_KPI_AA, direction="lower_is_better")
    assert resolve_kpi_property_expr(kpi, "KPIStatus", _MEASURES) == \
        "[Measures].[fee_amount]"


# ---------------------------------------------------------------------------
# Bug-6259 / Bug-5695 — goal must be executable MDX (or absent), never DSL
# ---------------------------------------------------------------------------

def test_bug6259_measure_target_resolves_by_id():
    # A measure target is identified by target_measure_id, not target_expression.
    kpi = {
        "name": "bb", "value_measure_id": "m-fee",
        "target_type": "measure", "target_measure_id": "m-base",
        "direction": "higher_is_better",
    }
    assert resolve_kpi_property_expr(kpi, "KPIGoal", _MEASURES) == \
        "[Measures].[base_amount]"
    # Status is the raw value member (independent of the goal now).
    assert resolve_kpi_property_expr(kpi, "KPIStatus", _MEASURES) == \
        "[Measures].[fee_amount]"


def test_bug6259_expression_target_not_advertised_as_mdx():
    # An expression target holds Tessallite DSL, not executable MDX -> KPIGoal
    # reports undefined (None) instead of emitting non-executable content. Status
    # is still the raw value member — it no longer depends on the (absent) goal.
    kpi = {
        "name": "cc", "value_measure_id": "m-fee",
        "target_type": "expression",
        "target_expression": "prior_period(measure(fee_amount), \"month\")",
        "direction": "higher_is_better",
    }
    assert resolve_kpi_property_expr(kpi, "KPIGoal", _MEASURES) is None
    assert resolve_kpi_property_expr(kpi, "KPIStatus", _MEASURES) == \
        "[Measures].[fee_amount]"


def test_bug5695_empty_goal_still_serves_value_status():
    # A measure target whose measure is missing -> empty goal, but KPI_STATUS is
    # the raw value member (no CASE built against a blank goal).
    from src.dax.mdschema import _rows_kpis
    kpi = {
        "name": "dd", "value_measure_id": "m-fee",
        "target_type": "measure", "target_measure_id": "m-missing",
        "direction": "higher_is_better",
    }
    rows = _rows_kpis("cat", [kpi], _MEASURES)
    assert rows[0]["KPI_GOAL"] == ""
    assert rows[0]["KPI_STATUS"] == "[Measures].[fee_amount]"
    assert not rows[0]["KPI_STATUS"].upper().startswith("CASE")


def test_resolve_trend_empty_returns_none():
    assert resolve_kpi_property_expr(_KPI_AA, "KPITrend", _MEASURES) is None


def test_resolve_legacy_status_expression_preferred():
    kpi = dict(_KPI_AA, status_expression="CASE WHEN 1=1 THEN 1 ELSE -1 END")
    assert resolve_kpi_property_expr(kpi, "KPIStatus", _MEASURES) == \
        "CASE WHEN 1=1 THEN 1 ELSE -1 END"


# ---------------------------------------------------------------------------
# Live governed KPI member-function path (_maybe_resolve_kpi_members).
# Bug-6608 un-gated: value/goal/status/trend resolve through the model-service
# /evaluate authority; KPIStatus is the governed −1/0/1.
# ---------------------------------------------------------------------------

_KPI_AA_ID = "kpi-aa"


def _kpi_with_id(**overrides):
    return dict(_KPI_AA, id=_KPI_AA_ID, **overrides)


async def _resolve_member(fn: str, governed: dict, kpi_overrides=None):
    """Drive ``_maybe_resolve_kpi_members`` for one KPI function, with the governed
    ``/evaluate`` response and ``get_model_kpis`` mocked. Returns the single cell
    value the live path produced."""
    kpi = _kpi_with_id(**(kpi_overrides or {}))
    statement = f'SELECT FROM [modely] WHERE ({fn}("aa"))'
    with patch.object(
        xmla_server, "get_model_kpis", new=AsyncMock(return_value=[kpi]),
    ), patch.object(
        xmla_server, "evaluate_kpi_governed",
        new=AsyncMock(return_value=governed),
    ) as mock_eval:
        result = await xmla_server._maybe_resolve_kpi_members(
            statement=statement,
            model_id="model-1",
            project_id="proj-1",
            tenant_slug="acme",
            jwt_token="jwt",
            measures_meta=_MEASURES,
            dimensions_meta=[],
            hierarchy_level_dim_map={},
            hierarchy_default_dim_map={},
            dim_names=set(),
            model_slug="modely",
            persona_id=None,
            is_technical_view=True,
        )
    assert result is not None
    columns, rows = result
    return rows[0][columns[0]], mock_eval


@pytest.mark.asyncio
async def test_live_status_is_governed_minus_one_zero_one():
    """KPIStatus returns the governed −1/0/1 RAG verdict from /evaluate, NOT the
    raw value. This is the F-025-01 fix: the same integer the scorecard and the
    Excel custom function return, so the report-builder traffic-light iconSet
    (calibrated for −1/0/1) colours correctly."""
    for governed_status in (-1, 0, 1):
        cell, _ = await _resolve_member(
            "KPIStatus", {"value": 2753735.21, "status": governed_status},
        )
        assert cell == governed_status
        # The raw business value must NEVER be served as the status.
        assert cell != 2753735.21


@pytest.mark.asyncio
async def test_live_status_no_data_is_blank():
    """A governed status of None (no target / no data) stays blank, not coerced."""
    cell, _ = await _resolve_member(
        "KPIStatus", {"value": None, "status": None},
    )
    assert cell is None


@pytest.mark.asyncio
async def test_live_value_resolves_composite_via_authority():
    """KPIValue serves the governed value — a composite/ratio expression the
    gateway's own single-measure resolution could not compute (G-002-01)."""
    cell, mock_eval = await _resolve_member(
        "KPIValue",
        {"value": 0.42, "status": 1},
        kpi_overrides={
            "value_measure_id": None,
            "expression": 'safe_div(measure("fee_amount"), measure("base_amount"))',
        },
    )
    assert cell == 0.42
    mock_eval.assert_awaited_once()


@pytest.mark.asyncio
async def test_live_goal_prefers_target_then_goal_alias():
    """KPIGoal reads the v2 ``target`` field, falling back to the v1 ``goal``
    alias."""
    cell, _ = await _resolve_member("KPIGoal", {"value": 5.0, "target": 10000.0})
    assert cell == 10000.0
    cell2, _ = await _resolve_member("KPIGoal", {"value": 5.0, "goal": 250.0})
    assert cell2 == 250.0


@pytest.mark.asyncio
async def test_live_slicer_uses_governed_batch_filters():
    """Bug-8383: a supported dimension slicer reaches evaluate-batch with its
    dimension ID and is never sent to the single unsliced route."""
    kpi = _kpi_with_id()
    statement = (
        'SELECT FROM [modely] '
        'WHERE (KPIStatus("aa"), [Region].[Region].[EMEA])'
    )
    with patch.object(
        xmla_server, "get_model_kpis", new=AsyncMock(return_value=[kpi]),
    ), patch.object(
        xmla_server, "evaluate_kpi_governed", new=AsyncMock(),
    ), patch.object(
        xmla_server,
        "evaluate_kpi_batch",
        new=AsyncMock(return_value={_KPI_AA_ID: {"status": 1, "value": 42}}),
    ) as mock_batch:
        result = await xmla_server._maybe_resolve_kpi_members(
            statement=statement,
            model_id="model-1",
            project_id="proj-1",
            tenant_slug="acme",
            jwt_token="jwt",
            measures_meta=_MEASURES,
            dimensions_meta=[
                {"id": "dim-region", "name": "Region", "type": "text"},
            ],
            hierarchy_level_dim_map={},
            hierarchy_default_dim_map={"[Region].[Region]": "Region"},
            dim_names={"Region"},
            model_slug="modely",
            persona_id=None,
            is_technical_view=True,
        )
    assert result[1][0][result[0][0]] == 1
    mock_batch.assert_awaited_once()
    assert mock_batch.await_args.kwargs["filters"] == [{
        "dimension_id": "dim-region",
        "operator": "eq",
        "value": "EMEA",
    }]


@pytest.mark.asyncio
async def test_bug8383_bare_dimension_where_is_not_discarded():
    """Bug-8383/L1-R1-004: bare single-member WHERE still reaches the batch.

    Some clients omit tuple parentheses for one slicer. The gateway must not
    turn that supported member into an empty WHERE and fall through to the
    unsliced single-evaluate route.
    """
    bare_statement = "SELECT FROM [modely] WHERE [Region].[Region].[EMEA]"
    assert xmla_server._mdx_where_expr(bare_statement) == (
        "[Region].[Region].[EMEA]"
    )
    kpi = _kpi_with_id()
    statement = (
        'SELECT FROM [modely] '
        'WHERE (KPIStatus("aa"), [Region].[Region].[EMEA])'
    )
    with patch.object(
        xmla_server, "get_model_kpis", new=AsyncMock(return_value=[kpi]),
    ), patch.object(
        xmla_server, "evaluate_kpi_governed", new=AsyncMock(),
    ), patch.object(
        xmla_server,
        "evaluate_kpi_batch",
        new=AsyncMock(return_value={_KPI_AA_ID: {"status": 1, "value": 42}}),
    ) as mock_batch:
        result = await xmla_server._maybe_resolve_kpi_members(
            statement=statement,
            model_id="model-1",
            project_id="proj-1",
            tenant_slug="acme",
            jwt_token="jwt",
            measures_meta=_MEASURES,
            dimensions_meta=[
                {"id": "dim-region", "name": "Region", "type": "text"},
            ],
            hierarchy_level_dim_map={},
            hierarchy_default_dim_map={"[Region].[Region]": "Region"},
            dim_names={"Region"},
            model_slug="modely",
            persona_id=None,
            is_technical_view=True,
        )
    assert result[1][0][result[0][0]] == 1
    mock_batch.assert_awaited_once()
    assert mock_batch.await_args.kwargs["filters"] == [{
        "dimension_id": "dim-region",
        "operator": "eq",
        "value": "EMEA",
    }]


@pytest.mark.asyncio
async def test_live_slicer_without_filter_equivalent_fails_loud():
    """Bug-8383: metadata without a dimension ID cannot be translated safely."""
    kpi = _kpi_with_id()
    statement = (
        'SELECT FROM [modely] '
        'WHERE (KPIStatus("aa"), [Region].[Region].[EMEA])'
    )
    with patch.object(
        xmla_server, "get_model_kpis", new=AsyncMock(return_value=[kpi]),
    ):
        with pytest.raises(ValueError, match="no model filter equivalent"):
            await xmla_server._maybe_resolve_kpi_members(
                statement=statement,
                model_id="model-1",
                project_id="proj-1",
                tenant_slug="acme",
                jwt_token="jwt",
                measures_meta=_MEASURES,
                dimensions_meta=[{"name": "Region", "type": "text"}],
                hierarchy_level_dim_map={},
                hierarchy_default_dim_map={"[Region].[Region]": "Region"},
                dim_names={"Region"},
                model_slug="modely",
                persona_id=None,
                is_technical_view=True,
            )


@pytest.mark.asyncio
async def test_live_unsupported_set_slicer_fails_before_batch_translation():
    """Bug-8383: Except() must not be reduced to an incorrect IN filter."""
    kpi = _kpi_with_id()
    statement = (
        'SELECT FROM [modely] WHERE '
        '(KPIStatus("aa"), Except({[Region].[Region].[EMEA]}, '
        '{[Region].[Region].[AMER]}))'
    )
    with patch.object(
        xmla_server, "get_model_kpis", new=AsyncMock(return_value=[kpi]),
    ), patch.object(
        xmla_server, "evaluate_kpi_batch", new=AsyncMock(),
    ) as mock_batch:
        with pytest.raises(ValueError, match=r"Unsupported MDX function 'Except\(\)'"):
            await xmla_server._maybe_resolve_kpi_members(
                statement=statement,
                model_id="model-1",
                project_id="proj-1",
                tenant_slug="acme",
                jwt_token="jwt",
                measures_meta=_MEASURES,
                dimensions_meta=[
                    {"id": "dim-region", "name": "Region", "type": "text"},
                ],
                hierarchy_level_dim_map={},
                hierarchy_default_dim_map={"[Region].[Region]": "Region"},
                dim_names={"Region"},
                model_slug="modely",
                persona_id=None,
                is_technical_view=True,
            )
    mock_batch.assert_not_awaited()


@pytest.mark.asyncio
async def test_live_dynamic_slicer_fails_without_batch_filter_equivalent():
    """Bug-8383: parameter/dynamic sets cannot silently become unsliced KPIs."""
    kpi = _kpi_with_id()
    statement = (
        'SELECT FROM [modely] WHERE '
        '(KPIStatus("aa"), STRTOSET(@Region, CONSTRAINED))'
    )
    with patch.object(
        xmla_server, "get_model_kpis", new=AsyncMock(return_value=[kpi]),
    ), patch.object(
        xmla_server, "evaluate_kpi_batch", new=AsyncMock(),
    ) as mock_batch:
        with pytest.raises(ValueError, match="no request-level filter equivalent"):
            await xmla_server._maybe_resolve_kpi_members(
                statement=statement,
                model_id="model-1",
                project_id="proj-1",
                tenant_slug="acme",
                jwt_token="jwt",
                measures_meta=_MEASURES,
                dimensions_meta=[
                    {"id": "dim-region", "name": "Region", "type": "text"},
                ],
                hierarchy_level_dim_map={},
                hierarchy_default_dim_map={"[Region].[Region]": "Region"},
                dim_names={"Region"},
                model_slug="modely",
                persona_id=None,
                is_technical_view=True,
            )
    mock_batch.assert_not_awaited()


# ---------------------------------------------------------------------------
# Single-authority fence — the gateway must NOT re-derive a KPI band verdict.
#
# Bug-6782/6786/6787/6788/6789 (Fable deep-review 2026-07-06) reported divergent
# gateway-side KPI-status computation (hardcoded tolerance literals, a +-10%
# heuristic, an MDX empty==zero CASE, unguarded variance division). Bug-6608 removed
# the gateway band verdict; the un-gated fix routes the LIVE status through the
# model-service authority (kpi_threshold.evaluate_threshold). The gateway still owns
# no threshold constants. This fence keeps it that way.
# ---------------------------------------------------------------------------


def _mdschema_kpi_status(kpi: dict) -> str:
    """The KPI_STATUS cell MDSCHEMA_KPIS advertises for *kpi*."""
    from src.dax.mdschema import _rows_kpis

    rows = _rows_kpis("modely", [kpi], _MEASURES)
    return rows[0]["KPI_STATUS"]


def test_mdschema_status_is_governed_synthetic_member():
    """Bug-8288: with no authored status but a verdict basis (target/bands),
    MDSCHEMA_KPIS advertises the synthetic GOVERNED status member
    ``[Measures].[<caption> Status]`` (not the raw value member), so a native pivot
    "Status" checkbox binds a member the Execute path resolves to the governed
    −1/0/1. The metadata-function resolver (``resolve_kpi_property_expr``) still
    returns the value member — it is the advisory member-function resolver, not the
    advertised catalogue member."""
    kpi = dict(_KPI_AA)
    assert _mdschema_kpi_status(kpi) == "[Measures].[aa Status]"
    # The raw value member must NOT be the advertised status member any more.
    assert _mdschema_kpi_status(kpi) != "[Measures].[fee_amount]"
    assert resolve_kpi_property_expr(kpi, "KPIStatus", _MEASURES) == \
        "[Measures].[fee_amount]"


def test_mdschema_status_falls_back_to_value_without_verdict_basis():
    """A KPI with a resolvable value but NO target/bands has no governed verdict
    basis, so no synthetic status member is advertised — it keeps the raw value
    member (unchanged behaviour, no misleading blank-status member)."""
    kpi = {"name": "noverdict", "value_measure_id": "m-fee"}
    assert _mdschema_kpi_status(kpi) == "[Measures].[fee_amount]"


def test_mdschema_status_authored_expression_verbatim():
    """When the modeller pins a status_expression, MDSCHEMA and the metadata
    resolver both serve it verbatim."""
    expr = "-1"
    kpi = dict(_KPI_AA, status_expression=expr)
    assert _mdschema_kpi_status(kpi) == expr
    assert resolve_kpi_property_expr(kpi, "KPIStatus", _MEASURES) == expr


def test_no_hardcoded_kpi_band_constants_in_gateway_dax():
    """Regression fence for Bug-6782/6786/6787/6788/6789 + Bug-6608.

    The gateway must NOT reintroduce a KPI band/threshold verdict. Assert that
    the KPI-status code sites carry no hardcoded tolerance literals
    (``goal*1.1``, ``abs(goal)*0.1``), no MDX empty/zero CASE
    (``IIF({kpi_value} = 0``), and no percentage_variance raw-division band math.
    If a future change re-adds gateway-side band derivation, this fails loudly so
    the single-authority contract is re-affirmed deliberately.
    """
    import pathlib

    dax_dir = pathlib.Path(__file__).resolve().parents[1] / "src" / "dax"
    # Unambiguous band-verdict markers: flag anywhere in src/dax (these tokens have
    # no legitimate non-KPI use), so an evasive reintroduction on a line lacking
    # KPI keywords cannot slip past.
    forbidden_anywhere = [
        "KPI_STATUS_GOOD_TOLERANCE",
        "KPI_STATUS_WARN_TOLERANCE",
        "1000000",     # MDX "best band" sentinel from the empty/zero CASE
    ]
    # Ambiguous arithmetic tokens: only flag when the line also references KPI /
    # goal / kpi_value / status, since bare "* 1.1" could appear in unrelated math.
    forbidden_near_kpi = [
        "* 1.1",       # goal * 1.1 good-tolerance literal
        ") * 0.1",     # abs(goal) * 0.1 warn-tolerance literal
    ]
    offenders: list[str] = []
    for path in dax_dir.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            low = line.lower()
            for token in forbidden_anywhere:
                if token.lower() in low:
                    offenders.append(f"{path.name}: {line.strip()}")
            near_kpi = "kpi" in low or "goal" in low or "status" in low or "kpi_value" in low
            if near_kpi:
                for token in forbidden_near_kpi:
                    if token.lower() in low:
                        offenders.append(f"{path.name}: {line.strip()}")
    assert not offenders, (
        "Gateway dax reintroduced a hardcoded KPI band/threshold verdict "
        "(Bug-6608 removed it; the single authority is model-service "
        "kpi_threshold.evaluate_threshold). Offending lines:\n"
        + "\n".join(offenders)
    )


# ---------------------------------------------------------------------------
# MDSCHEMA_KPIS status graphic restored (Bug-6608 un-gated).
# ---------------------------------------------------------------------------


def _mdschema_row(kpi: dict) -> dict:
    from src.dax.mdschema import _rows_kpis

    return _rows_kpis("modely", [kpi], _MEASURES)[0]


def test_mdschema_graphic_restored_for_governed_synthetic_status():
    """Bug-8288: now that KPI_STATUS advertises the synthetic GOVERNED status
    member (which the Execute path resolves to the −1/0/1 verdict), the status
    graphic is advertised again over that real verdict domain. Previously it was
    suppressed because the member was the raw value."""
    kpi = dict(_KPI_AA, presentation_type="traffic_light")
    assert _mdschema_row(kpi)["KPI_STATUS"] == "[Measures].[aa Status]"
    assert _mdschema_row(kpi)["KPI_STATUS_GRAPHIC"] == "Traffic Light"
    # Banded KPI (verdict basis via bands): synthetic member + graphic restored.
    kpi2 = {
        "name": "banded", "value_measure_id": "m-fee",
        "target_type": "static", "target_value": 100.0,
        "presentation_type": "gauge",
        "presentation_meta": {"bands": [{"min": None, "max": 1.0, "label": "Bad"}]},
    }
    assert _mdschema_row(kpi2)["KPI_STATUS"] == "[Measures].[banded Status]"
    assert _mdschema_row(kpi2)["KPI_STATUS_GRAPHIC"] == "Gauge"


def test_mdschema_graphic_suppressed_without_verdict_basis():
    """A KPI with no authored status and no verdict basis keeps the raw value
    member and NO graphic (the synthetic governed member is not advertised, so a
    business number is never clamped onto the -1/0/1 icon domain)."""
    kpi = {"name": "noverdict", "value_measure_id": "m-fee",
           "presentation_type": "traffic_light"}
    assert _mdschema_row(kpi)["KPI_STATUS"] == "[Measures].[fee_amount]"
    assert _mdschema_row(kpi)["KPI_STATUS_GRAPHIC"] == ""


def test_mdschema_graphic_restored_for_authored_status():
    """An authored status_expression IS a real verdict, so the graphic is
    advertised (unchanged from the Bug-6608 suppression rules)."""
    kpi = dict(_KPI_AA, status_expression="-1", presentation_type="traffic_light")
    assert _mdschema_row(kpi)["KPI_STATUS_GRAPHIC"] == "Traffic Light"


def test_mdschema_no_graphic_for_bare_kpi_without_verdict():
    """A bare KPI with no authored status has no graphic."""
    kpi = {"name": "bare", "value_measure_id": "m-fee"}
    assert _mdschema_row(kpi)["KPI_STATUS_GRAPHIC"] == ""
