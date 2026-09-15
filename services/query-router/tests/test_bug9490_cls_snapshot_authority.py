"""Bug-9490 — the CLS closure must read the DEPLOYED measure lineage.

The query is compiled from the deployed snapshot, but the closure that decides
whether an object reaches a restricted column walked LIVE draft rows. So a
modeller editing a calculated measure in DRAFT changed what the security gate
checked for an ALREADY-DEPLOYED model, before redeployment.

The dangerous direction is the first test below: drop a restricted reference in
draft, and the deployed measure was checked against the clean draft lineage and
SERVED, while the deployed definition still read the restricted column.

No new failure mode is introduced by pinning. The binder already refuses a
deployed model whose snapshot cannot be resolved (DeployedSnapshotUnavailableError
-> 503) and never falls back to draft, so a bound query for a deployed model
always carries a shape. ``deployed_shape is None`` means genuinely undeployed,
where the live tables ARE the authority — mirroring the binder's own discipline.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.routing.router import _build_cls_closure, _restricted_uda_ids_from_shape

RESTRICTED_COL = "col-salary-restricted"
CLEAN_COL = "col-allowance-clean"


def _measure(name, **kw):
    base = dict(
        id=f"id-{name}", name=name, source_column_id=None, display_column_id=None,
        user_defined_attribute_id=None, variant_of_measure_id=None,
        measure_type="base", expression=None, calc_expression=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _calc(name, ref):
    return _measure(name, measure_type="calculated", expression=f'measure("{ref}")')


def _bound(measures, shape):
    return SimpleNamespace(
        model=SimpleNamespace(id="model-1"),
        resolved_measures=list(measures),
        resolved_dimensions=[],
        resolved_dimensions_by_name={},
        deployed_shape=shape,
    )


def _shape(measures, uda_refs=None):
    return SimpleNamespace(
        measures=list(measures),
        uda_column_ref_rows=list(uda_refs or []),
    )


@pytest.mark.asyncio
async def test_bug9490_deployed_lineage_wins_over_a_cleaned_draft():
    """THE bypass. Deployed calc reads the restricted base; draft does not."""
    deployed_base = _measure("base", source_column_id=RESTRICTED_COL)
    deployed_calc = _calc("net_pay", "base")
    shape = _shape([deployed_base, deployed_calc])

    db = AsyncMock()  # any live read would be a defect; the shape must be used
    ctx = await _build_cls_closure(_bound([deployed_calc], shape), [RESTRICTED_COL], db)

    assert ctx.measures_by_name["base"].source_column_id == RESTRICTED_COL
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_bug9490_a_restricted_draft_edit_does_not_over_block_production():
    """The mirror. Deployed lineage is clean; a draft edit must not block it."""
    deployed_base = _measure("base", source_column_id=CLEAN_COL)
    deployed_calc = _calc("net_pay", "base")
    shape = _shape([deployed_base, deployed_calc])

    db = AsyncMock()
    ctx = await _build_cls_closure(_bound([deployed_calc], shape), [RESTRICTED_COL], db)

    assert ctx.measures_by_name["base"].source_column_id == CLEAN_COL


@pytest.mark.asyncio
async def test_bug9490_undeployed_model_still_reads_live():
    """No deployed pointer means the live tables are the legitimate authority."""
    live = _measure("base", source_column_id=RESTRICTED_COL)
    result = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [live]))
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)

    calc = _calc("net_pay", "base")
    ctx = await _build_cls_closure(_bound([calc], None), [RESTRICTED_COL], db)

    assert "base" in ctx.measures_by_name
    db.execute.assert_awaited()


@pytest.mark.asyncio
async def test_bug9490_variant_base_comes_from_the_snapshot():
    deployed_base = _measure("base", source_column_id=RESTRICTED_COL)
    variant = _measure("base_ytd", variant_of_measure_id="id-base")
    shape = _shape([deployed_base, variant])

    db = AsyncMock()
    ctx = await _build_cls_closure(_bound([variant], shape), [RESTRICTED_COL], db)

    assert ctx.measures_by_id["id-base"].source_column_id == RESTRICTED_COL
    db.execute.assert_not_awaited()


def test_bug9490_uda_lineage_is_pinned_but_policy_stays_live():
    """The persona restriction is live; only the UDA lineage is pinned."""
    shape = _shape([], uda_refs=[
        {"attribute_id": "uda-1", "column_id": RESTRICTED_COL},
        {"attribute_id": "uda-2", "column_id": CLEAN_COL},
    ])
    assert _restricted_uda_ids_from_shape(shape, [RESTRICTED_COL]) == {"uda-1"}
    # A different live restriction set selects a different UDA from the SAME
    # pinned lineage — policy is live, lineage is not.
    assert _restricted_uda_ids_from_shape(shape, [CLEAN_COL]) == {"uda-2"}
    assert _restricted_uda_ids_from_shape(shape, []) == set()


@pytest.mark.asyncio
async def test_bug9490_uda_backed_measure_uses_the_pinned_refs():
    uda_measure = _measure("headcount", user_defined_attribute_id="uda-1")
    calc = _calc("net_pay", "headcount")
    shape = _shape(
        [uda_measure, calc],
        uda_refs=[{"attribute_id": "uda-1", "column_id": RESTRICTED_COL}],
    )
    db = AsyncMock()
    ctx = await _build_cls_closure(_bound([calc], shape), [RESTRICTED_COL], db)

    assert ctx.restricted_uda_ids == {"uda-1"}
    assert ctx.uda_restrictions_loaded is True
    db.execute.assert_not_awaited()


# ---------------------------------------------------------------------------
# $KPIs — the blocked set and the measure name map must share ONE universe.
#
# If the blocked set is built from the deployed universe while the name map is
# built from the live one, a measure present in draft but not deployed resolves
# to an id that is absent from the blocked set, and the KPI is served. Fixing
# only one of the two reads recreates the same defect in a different shape.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bug9490_kpi_blocked_set_uses_the_pinned_universe():
    """The KPI CLS gate reads measures and UDA lineage from the shape."""
    from src.api.routes import _kpi_cls_blocked_measure_ids

    restricted = _measure("salary", source_column_id=RESTRICTED_COL)
    clean = _measure("allowance", source_column_id=CLEAN_COL)
    shape = SimpleNamespace(
        measures=[restricted, clean],
        uda_column_ref_rows=[],
        columns_by_id={RESTRICTED_COL: {"column_name": "salary_amount"}},
    )

    # Live reads still supply the persona -> data-tag -> column POLICY.
    tag_rows = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: ["tag-1"]))
    col_rows = SimpleNamespace(
        scalars=lambda: SimpleNamespace(all=lambda: [RESTRICTED_COL])
    )
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[tag_rows, col_rows])

    blocked = await _kpi_cls_blocked_measure_ids(
        db, "model-1", SimpleNamespace(id="persona-1"), deployed_shape=shape,
    )

    assert blocked == frozenset({"id-salary"})
    # Exactly two live reads: the two restriction-policy queries. The measure
    # universe and UDA lineage came from the shape, not the database.
    assert db.execute.await_count == 2


@pytest.mark.asyncio
async def test_bug9490_kpi_gate_is_inert_without_a_persona():
    """No persona means no CLS policy, so no authority resolution is needed."""
    from src.api.routes import _kpi_cls_blocked_measure_ids

    db = AsyncMock()
    assert await _kpi_cls_blocked_measure_ids(db, "model-1", None) == frozenset()
    db.execute.assert_not_awaited()


# ---------------------------------------------------------------------------
# Review F1 — the $KPIs scorecard must authorise the DEPLOYED KPI definition.
#
# The value served comes from an evaluation performed under the deployed
# definition. Authorising it against the LIVE definition let a draft edit decide
# what a deployed value had been checked for: change a KPI in draft so it no
# longer references a restricted measure, and the deployed value — still derived
# from that restricted measure — was served.
# ---------------------------------------------------------------------------


def _kpi(kid, name, expression=None, parent=None):
    return {
        "id": kid, "name": name, "expression": expression,
        "parent_kpi_id": parent, "value_measure_id": None,
        "goal_measure_id": None, "target_measure_id": None,
    }


def _allowed(kpi_definition, blocked, name_to_id, kpi_by_name):
    from src.api.routes import _kpi_allowed_by_persona

    return _kpi_allowed_by_persona(
        kpi_definition, None, name_to_id, frozenset(blocked), kpi_by_name, {},
    )


def test_bug9490_f1_deployed_kpi_definition_decides_withholding():
    """THE bypass. Deployed KPI reads a restricted measure; the draft does not."""
    deployed = SimpleNamespace(**_kpi("k1", "Payroll", 'measure("Salary")'))
    draft = SimpleNamespace(**_kpi("k1", "Payroll", 'measure("Headcount")'))
    name_to_id = {"Salary": "id-salary", "Headcount": "id-headcount"}
    blocked = {"id-salary"}

    assert _allowed(deployed, blocked, name_to_id, {}) is False, (
        "the deployed definition reaches a restricted measure and must be withheld"
    )
    # Authorising the DRAFT instead is what served the restricted value.
    assert _allowed(draft, blocked, name_to_id, {}) is True


def test_bug9490_f1_a_restricted_draft_does_not_withhold_a_clean_deployed_kpi():
    """The mirror: a draft edit must not withhold a legitimately clean KPI."""
    deployed = SimpleNamespace(**_kpi("k1", "Payroll", 'measure("Headcount")'))
    name_to_id = {"Salary": "id-salary", "Headcount": "id-headcount"}
    assert _allowed(deployed, {"id-salary"}, name_to_id, {}) is True


def test_bug9490_f1_nested_kpi_absent_from_the_deployment_fails_closed():
    """A nested reference that does not resolve in the deployed universe.

    Bug-6139 built the KPI name map from ALL live rows so a nested ``kpi()``
    reference would resolve. Pinning to the deployment narrows that universe on
    purpose: an undeployed nested KPI now does not resolve, and the existing
    fail-closed path withholds the parent rather than authorising it from a
    draft definition.
    """
    deployed = SimpleNamespace(**_kpi("k1", "Rollup", 'kpi("DraftOnly")'))
    assert _allowed(deployed, {"id-salary"}, {}, {}) is False
