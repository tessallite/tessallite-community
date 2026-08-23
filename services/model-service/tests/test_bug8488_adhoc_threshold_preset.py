"""Bug-8488 — ``KPIAdhocRequest.threshold_preset`` was accepted but ignored.

``/kpis/evaluate-adhoc`` exposes ``threshold_preset``, so clients can submit
``standard_4_band``. The endpoint never read it: it evaluated explicit
``presentation_meta.bands`` when supplied and otherwise used
``evaluate_threshold``'s DEFAULT preset. A live gateway-backed request with
value 1, target 2 and ``threshold_preset=standard_4_band`` returned
"Off Target" instead of the requested preset's "Critical" — an accepted but
unwired API contract, and a builder preview showing a band the saved KPI would
not use.

Known values (hand-checked against ``BAND_PRESETS`` in ``kpi_threshold.py``):

  ratio = value / target = 1 / 2 = 0.5

  standard_3_band (the DEFAULT):  < 0.80          -> "Off Target"
  standard_4_band (REQUESTED):    < 0.70          -> "Critical"

The two presets therefore disagree on exactly this ratio, which is what makes
the assertion evidence rather than a shape check.

Test escape: existing tests covered preset CALCULATION but never the public
ad-hoc request FIELD. Guard: this module. Tier: T1.
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.kpi_threshold import evaluate_threshold, get_preset_bands, list_presets
from shared.schemas.domains.governance_advanced import KPIAdhocRequest

from .conftest import (  # noqa: F401
    TEST_MODEL_ID,
    TEST_PROJECT_ID,
    async_gen_from,
    client,
    make_mock_db,
)

pytestmark = pytest.mark.unit

_VALUE = 1.0
_TARGET = 2.0

URL = (
    f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}"
    f"/kpis/evaluate-adhoc"
)


class TestPresetsDisagreeAtTheProbedRatio:
    """The premise the endpoint assertion rests on."""

    def test_default_preset_says_off_target(self):
        result = evaluate_threshold(value=_VALUE, target=_TARGET)
        assert result.status_label == "Off Target"

    def test_requested_preset_says_critical(self):
        result = evaluate_threshold(
            value=_VALUE, target=_TARGET,
            bands=[
                {"label": b.label, "color": b.color, "min": b.min, "max": b.max}
                for b in get_preset_bands("standard_4_band")
            ],
        )
        assert result.status_label == "Critical"


class TestAdhocRequestCarriesThePreset:
    def test_field_is_accepted(self):
        body = KPIAdhocRequest(threshold_preset="standard_4_band")
        assert body.threshold_preset == "standard_4_band"

    def test_known_presets_include_the_documented_names(self):
        names = list_presets()
        for expected in ("standard_3_band", "standard_4_band", "tight_tolerance"):
            assert expected in names


class TestEndpointConsumesThePreset:
    """The endpoint must resolve the preset to bands and reject unknown names.

    L7-R9: this class used to say "driving the whole ``evaluate_adhoc``
    coroutine needs a live DB session" and assert the WIRING instead. That
    reason was wrong — ``tests/conftest.py`` provides an httpx harness that four
    sibling modules already drive against this exact route — and it left a
    wrong-numbers bug (a preview scored against bands the caller never asked
    for) guarded only by module-wide AST checks that pass whether or not
    ``evaluate_adhoc`` is the function reading the field.

    ``TestEndpointReturnsTheRequestedPresetsVerdict`` below now drives the real
    endpoint and asserts the VERDICT. The AST checks are kept as the cheaper
    structural backstop, no longer as the only evidence.
    """

    def test_preset_bands_are_passed_as_bands_at_the_adhoc_threshold_call(self):
        import ast
        import inspect

        from src.api import kpis as kpis_mod

        tree = ast.parse(inspect.getsource(kpis_mod))
        preset_reads = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and node.attr == "threshold_preset"
        ]
        assert preset_reads, (
            "evaluate_adhoc never reads body.threshold_preset — the field is "
            "accepted by the schema and ignored by the endpoint (Bug-8488)"
        )

        get_preset_calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and (getattr(node.func, "id", None) or getattr(node.func, "attr", None))
            == "get_preset_bands"
        ]
        assert get_preset_calls, (
            "the requested preset is read but never resolved to bands"
        )

    def test_unknown_preset_names_are_rejected_not_silently_defaulted(self):
        """``get_preset_bands`` substitutes standard_3_band for an unknown name.

        Scoring a preview against bands the caller never asked for is the same
        defect in a different disguise, so the endpoint must refuse.
        """
        import ast
        import inspect

        from src.api import kpis as kpis_mod

        source = inspect.getsource(kpis_mod)
        tree = ast.parse(source)
        assert any(
            isinstance(node, ast.Call)
            and (getattr(node.func, "id", None) or getattr(node.func, "attr", None))
            == "list_presets"
            for node in ast.walk(tree)
        ), (
            "no membership check against list_presets(): an unknown "
            "threshold_preset would silently score against standard_3_band"
        )
        # And the fallback it guards against really is silent.
        assert get_preset_bands("no_such_preset") == get_preset_bands("standard_3_band")


# ---------------------------------------------------------------------------
# The endpoint itself (L7-R9)
# ---------------------------------------------------------------------------

def _measure(name: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=uuid.uuid4(), model_id=TEST_MODEL_ID, name=name, default_agg="sum",
        measure_type="standard", expression=None, calc_agg_mode=None,
        variant_kind=None, is_additive=True, semi_additive_behavior=None,
    )


async def _post_adhoc(client, body, *, value, target):  # noqa: F811
    """Drive POST /kpis/evaluate-adhoc with the router pinned to known numbers.

    ``value`` is served for the value expression and ``target`` for the target
    expression, so the ratio the threshold scores is exactly value/target.
    """
    measures = [_measure("Revenue"), _measure("Goal")]
    db = make_mock_db()

    async def _exec(stmt, *_a, **_kw):
        text = str(stmt)
        rows = measures if "measures" in text else []
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

    async def _router(_model_id, sql, _bearer, **_kw):
        return {"rows": [{"value": target if "Goal" in sql else value}]}

    async def _ensure(_db, *, project_id, model_id):  # noqa: ARG001
        return model

    async def _persona(_db, **_kw):
        return None

    with patch.object(kpis_mod, "get_tenant_db", async_gen_from(db)), \
            patch.object(kpis_mod, "ensure_model_in_project", _ensure), \
            patch.object(kpis_mod, "resolve_effective_persona", _persona), \
            patch.object(kpis_mod, "_execute_via_router", _router):
        return await client.post(URL, json=body)


class TestEndpointReturnsTheRequestedPresetsVerdict:
    """Bug-8488 through the REAL route, asserting the user-visible verdict.

    ratio = 1 / 2 = 0.5, where standard_3_band says "Off Target" and the
    REQUESTED standard_4_band says "Critical". Pre-fix the response carried
    "Off Target" — the default preset's verdict for a request that named a
    different one.

    Test escape: the field was covered by schema and calculation tests plus a
    module-wide AST check; nothing drove the endpoint, which is the only place
    the field is consumed. Guard: this class. Tier: T1.
    """

    @pytest.mark.asyncio
    async def test_the_requested_preset_decides_the_status_label(self, client):  # noqa: F811
        response = await _post_adhoc(
            client,
            {
                "expression": 'measure("Revenue")',
                "target_expression": 'measure("Goal")',
                "threshold_preset": "standard_4_band",
            },
            value=_VALUE, target=_TARGET,
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["value"] == _VALUE and body["target"] == _TARGET, body
        assert body["status_label"] == "Critical", (
            "the ad-hoc preview scored value 1 against target 2 with the "
            "DEFAULT preset's bands and answered "
            f"{body['status_label']!r}; the request asked for "
            "standard_4_band, whose verdict at ratio 0.5 is 'Critical' "
            "(Bug-8488)"
        )

    @pytest.mark.asyncio
    async def test_without_the_preset_the_default_verdict_is_returned(self, client):  # noqa: F811
        """Control: the same inputs with no preset must still say 'Off Target',
        so the assertion above is evidence about the PRESET and not about the
        arithmetic."""
        response = await _post_adhoc(
            client,
            {
                "expression": 'measure("Revenue")',
                "target_expression": 'measure("Goal")',
            },
            value=_VALUE, target=_TARGET,
        )

        assert response.status_code == 200, response.text
        assert response.json()["status_label"] == "Off Target"

    @pytest.mark.asyncio
    async def test_an_unknown_preset_is_rejected_with_400(self, client):  # noqa: F811
        """``get_preset_bands`` substitutes standard_3_band for an unknown name.
        Scoring a preview against bands the caller never asked for is the same
        defect in a different disguise, so the endpoint must refuse."""
        response = await _post_adhoc(
            client,
            {
                "expression": 'measure("Revenue")',
                "target_expression": 'measure("Goal")',
                "threshold_preset": "no_such_preset",
            },
            value=_VALUE, target=_TARGET,
        )

        assert response.status_code == 400, response.text
        assert "no_such_preset" in response.json()["detail"]
