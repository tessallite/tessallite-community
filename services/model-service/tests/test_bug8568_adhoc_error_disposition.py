"""Bug-8568 follow-up (deep-review R7 finding 1) -- an ad-hoc EXECUTION failure
must never be answered by the unfiltered Python evaluator.

R6 split ``_EVALUATION_ERROR`` into "a guard refused" (``_GUARD_REFUSED`` -> 400)
and "the router call raised" (``_EVALUATION_ERROR``). The refusal half is right.
The execution half now falls into the Python fallback, and that fallback fetches
measures through ``_batch_get_measure_values``, which emits
``SELECT SUM(...) FROM "<model>"`` with NO WHERE clause -- it never receives the
business definition's ``filter_where_clause`` / ``time_where_clause``.

So a filtered ratio preview whose router call merely times out is answered with
the MODEL-WIDE number, at HTTP 200, with no warning. Traced: EMEA-only
220/11 = 20.0 is served as all-region 310/31 = 10.0. Before R6 the same input
returned a 400.

Test escape: every Bug-8568 test asserted the SENTINEL returned by
``_evaluate_expression_via_sql``; nothing exercised the ad-hoc endpoint, which is
the only place the split actually changes behaviour.
Guard: this module. Tier: T1 (wrong-number, execution scope: isolated).
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from .conftest import (  # noqa: F401
    TEST_MODEL_ID,
    TEST_PROJECT_ID,
    async_gen_from,
    client,
    make_mock_db,
)

pytestmark = pytest.mark.unit

URL = (
    f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}"
    f"/kpis/evaluate-adhoc"
)


def _measure(name: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=uuid.uuid4(), model_id=TEST_MODEL_ID, name=name, default_agg="sum",
        measure_type="standard", expression=None, calc_agg_mode=None,
        variant_kind=None, is_additive=True, semi_additive_behavior=None,
    )


@pytest.mark.asyncio
async def test_a_router_failure_on_a_filtered_kpi_is_not_answered_unfiltered(
    client,  # noqa: F811
) -> None:
    measures = [_measure("Revenue"), _measure("Headcount")]
    dim = types.SimpleNamespace(
        id=uuid.uuid4(), model_id=TEST_MODEL_ID, name="region",
        is_time_dim=False, data_type="string", source_column_id=None,
    )
    db = make_mock_db()

    async def _exec(stmt, *_a, **_kw):
        text = str(stmt)
        rows = (
            measures if "measures" in text
            else ([dim] if "dimensions" in text else [])
        )
        result = MagicMock()
        scalars = MagicMock()
        scalars.all.return_value = rows
        result.scalars.return_value = scalars
        result.scalar_one_or_none.return_value = None
        result.scalar.return_value = 0
        return result

    db.execute = AsyncMock(side_effect=_exec)
    model = types.SimpleNamespace(
        id=TEST_MODEL_ID, slug="modelx", name="modelx",
        fiscal_year_start_month=1, calendar_type=None,
    )

    from src.api import kpis as kpis_mod

    issued: list[str] = []

    async def _router(_model_id, sql, _bearer, **_kw):
        issued.append(sql)
        if "WHERE" in sql.upper():
            # The real, correctly-filtered KPI query trips the 10s ad-hoc
            # timeout. This is transient and says nothing about the definition.
            raise TimeoutError("router timed out")
        # Anything without a WHERE is the fallback's unfiltered batch fetch:
        # all regions, 310/31 -- NOT the 220/11 = 20.0 the modeller asked for.
        return {"rows": [{"m0": 310, "m1": 31}]}

    async def _ensure(_db, *, project_id, model_id):  # noqa: ARG001
        return model

    async def _persona(_db, **_kw):
        return None

    business_definition = {
        "builder": "business_kpi",
        "version": 1,
        "formula": {
            "type": "ratio",
            "numerator_measure_id": str(measures[0].id),
            "denominator_measure_id": str(measures[1].id),
        },
        "filters": [{
            "dimension_id": str(dim.id), "operator": "in",
            "mode": "fixed", "values": ["EMEA"],
        }],
    }

    with patch.object(kpis_mod, "get_tenant_db", async_gen_from(db)), \
            patch.object(kpis_mod, "ensure_model_in_project", _ensure), \
            patch.object(kpis_mod, "resolve_effective_persona", _persona), \
            patch.object(kpis_mod, "_execute_via_router", _router):
        resp = await client.post(
            URL, json={"business_definition": business_definition},
        )

    unfiltered = [s for s in issued if "WHERE" not in s.upper()]
    assert not unfiltered, (
        "a filtered KPI whose SQL route failed was re-run WITHOUT its filter "
        f"predicates: {unfiltered}"
    )
    assert resp.status_code != 200 or resp.json().get("value") is None, (
        "a transient router failure returned a value: "
        f"{resp.status_code} {resp.text}. The correct EMEA answer is "
        "220/11 = 20.0; the unfiltered fallback serves 310/31 = 10.0."
    )
