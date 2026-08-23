"""Bug-8257 — the model-service producers must persist a trustworthy
``is_additive``.

The schema validator covers CREATE, but a PATCH is partial: a caller can flip
``default_agg`` to ``avg`` without touching ``is_additive`` (leaving a persisted
True on a measure that can no longer be summed), or send ``is_additive: true``
on a measure whose persisted aggregation is already non-additive. Only the
MERGED row shows both halves, so the derivation has to run in the endpoint.

The cascade to variant rows is the second half: a variant is non-additive
across periods by construction, so it must never inherit an additive base's
flag verbatim.

Test escape: the PATCH path had no assertion on the persisted flag at all —
``test_measures_variant_immutability`` only checked which fields are REJECTED.
Guard: this module. Tier: T1 producer/consumer contract.
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from .conftest import (
    NOW,
    TEST_MODEL_ID,
    TEST_PROJECT_ID,
    async_gen_from,
    client,
    make_mock_db,
)

pytestmark = pytest.mark.unit

PREFIX = f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/measures"


def _patch_scope():
    async def _noop(db, *, project_id, model_id):
        return None
    return patch("src.api.measures.ensure_model_in_project", _noop)


def _plain_row(*, default_agg="sum", is_additive=True) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        model_id=TEST_MODEL_ID,
        name="revenue",
        display_name="Revenue",
        description=None,
        display_folder=None,
        is_hidden=False,
        source_column_id=uuid.uuid4(),
        source_table_id=None,
        user_defined_attribute_id=None,
        measure_type="standard",
        expression=None,
        calc_agg_mode=None,
        data_type="numeric",
        default_agg=default_agg,
        format=None,
        variant_kind=None,
        variant_of_measure_id=None,
        variant_n=None,
        is_additive=is_additive,
        semi_additive_behavior=None,
        semi_additive_account_column_id=None,
        is_invalid=False,
        invalid_reason=None,
        created_at=NOW,
        updated_at=NOW,
    )


def _db_for(measure):
    mock_db = make_mock_db()

    async def _get(entity, entity_id):
        return measure if entity_id == measure.id else None

    mock_db.get = AsyncMock(side_effect=_get)

    executed: list = []

    async def _exec(stmt, *a, **kw):
        executed.append(stmt)
        r = MagicMock()
        r.scalar_one_or_none.return_value = None
        r.scalars.return_value.all.return_value = []
        r.first.return_value = None
        return r

    mock_db.execute = AsyncMock(side_effect=_exec)
    mock_db.executed = executed
    return mock_db


async def _patch_measure(client, measure, body):
    mock_db = _db_for(measure)
    with (
        _patch_scope(),
        patch("src.api.measures.get_tenant_db", async_gen_from(mock_db)),
        patch("src.api.measures.acquire_model_definition_lock", AsyncMock()),
        patch("src.api.measures._glossary_text_for_target", AsyncMock(return_value=None)),
    ):
        resp = await client.patch(f"{PREFIX}/{measure.id}", json=body)
    return resp, mock_db


@pytest.mark.asyncio
async def test_patch_to_a_non_additive_agg_coerces_the_untouched_flag(client):
    """The exact shipped shape: an additive sum measure is re-pointed at ``avg``
    and the caller never mentions is_additive. Without the post-merge
    derivation the row keeps ``is_additive=True`` and the pivot happily SUMS a
    column of averages."""
    m = _plain_row(default_agg="sum", is_additive=True)
    resp, _ = await _patch_measure(client, m, {"default_agg": "avg"})

    assert resp.status_code == 200, resp.text
    assert m.default_agg == "avg"
    assert m.is_additive is False
    assert resp.json()["is_additive"] is False


@pytest.mark.asyncio
async def test_patch_cannot_declare_a_non_additive_measure_additive(client):
    m = _plain_row(default_agg="max", is_additive=False)
    resp, _ = await _patch_measure(client, m, {"is_additive": True})

    assert resp.status_code == 200, resp.text
    assert m.is_additive is False


@pytest.mark.asyncio
async def test_patch_leaves_a_plain_sum_measure_additive(client):
    """Control: the coercion must not pessimise a legitimately additive row."""
    m = _plain_row(default_agg="sum", is_additive=True)
    resp, _ = await _patch_measure(client, m, {"display_name": "Revenue (net)"})

    assert resp.status_code == 200, resp.text
    assert m.is_additive is True


@pytest.mark.asyncio
async def test_patch_can_still_declare_a_sum_measure_non_additive(client):
    """A modeller marking a plain sum non-additive is stating something the
    shape cannot prove; the safe direction is always preserved."""
    m = _plain_row(default_agg="sum", is_additive=True)
    resp, _ = await _patch_measure(client, m, {"is_additive": False})

    assert resp.status_code == 200, resp.text
    assert m.is_additive is False


@pytest.mark.asyncio
async def test_cascade_to_variants_never_writes_an_additive_flag(client):
    """The cascade targets are variant rows. Copying an additive base's flag
    verbatim would mark every PY/YTD/trailing variant summable across periods."""
    m = _plain_row(default_agg="sum", is_additive=True)
    resp, mock_db = await _patch_measure(client, m, {"is_additive": True})

    assert resp.status_code == 200, resp.text
    cascades = [
        s for s in mock_db.executed
        if "UPDATE measures" in str(s).upper().replace('"', "")
        or "UPDATE MEASURES" in str(s).upper()
    ]
    assert cascades, "expected a cascade UPDATE against the variant rows"
    values = {}
    for stmt in cascades:
        values.update(getattr(stmt, "_values", None) or {})
    assert "is_additive" in values, f"cascade did not carry is_additive: {values}"
    # SQLAlchemy wraps a literal in a BindParameter; compare the bound value.
    bound = values["is_additive"]
    assert getattr(bound, "value", bound) is False


@pytest.mark.asyncio
async def test_a_derived_false_is_not_sticky_when_the_shape_changes(client):
    """Deep-review R3 finding 9: after create-time coercion an ``avg`` measure
    holds a DERIVED False. Re-pointing it at ``sum`` must make it additive
    again — treating the derived False as a declaration would leave a plain sum
    measure permanently un-totalable with no way back through the UI."""
    m = _plain_row(default_agg="avg", is_additive=False)
    resp, _ = await _patch_measure(client, m, {"default_agg": "sum"})

    assert resp.status_code == 200, resp.text
    assert m.default_agg == "sum"
    assert m.is_additive is True


@pytest.mark.asyncio
async def test_a_false_declared_in_this_request_is_still_honoured(client):
    """Control: an explicit is_additive=False in the SAME payload is a real
    declaration (a semi-additive balance stored as a sum) and must survive."""
    m = _plain_row(default_agg="avg", is_additive=False)
    resp, _ = await _patch_measure(
        client, m, {"default_agg": "sum", "is_additive": False}
    )

    assert resp.status_code == 200, resp.text
    assert m.is_additive is False


@pytest.mark.asyncio
async def test_patch_setting_semi_additive_behavior_coerces_the_flag(client):
    """Bug-8257 (deep-review R6 finding 2) -- exercise the PRODUCER, not the
    helper.

    The R5 tests called ``derive_is_additive()`` directly, so removing all four
    ``semi_additive_behavior=`` threadings left 137 tests green. These go
    through the real PATCH endpoint.

    A last-non-empty balance is the textbook non-summable measure: the Measures
    panel defaults the Additive toggle to true and sends
    ``semi_additive_behavior`` independently, so without the threading the row
    persists additive and the Explorer pivot grand total sums the daily
    balances (100+120+90 = 310 where the answer is 90).
    """
    m = _plain_row(default_agg="sum", is_additive=True)
    m.semi_additive_behavior = None
    resp, _ = await _patch_measure(
        client, m, {"semi_additive_behavior": "last_non_empty"}
    )

    assert resp.status_code == 200, resp.text
    assert m.semi_additive_behavior == "last_non_empty"
    assert m.is_additive is False, (
        "the PATCH producer did not re-derive additivity from the newly-set "
        "semi_additive_behavior"
    )
    assert resp.json()["is_additive"] is False


@pytest.mark.asyncio
async def test_patch_clearing_semi_additive_behavior_restores_additivity(client):
    """Control + the sticky-derived-False rule: clearing the behaviour on a
    plain sum measure must make it summable again."""
    m = _plain_row(default_agg="sum", is_additive=False)
    m.semi_additive_behavior = "last_non_empty"
    resp, _ = await _patch_measure(
        client, m, {"semi_additive_behavior": None}
    )

    assert resp.status_code == 200, resp.text
    assert m.is_additive is True


@pytest.mark.asyncio
async def test_patch_on_a_semi_additive_measure_cannot_declare_it_additive(client):
    """An explicit ``is_additive: true`` must not beat the measure's shape."""
    m = _plain_row(default_agg="sum", is_additive=False)
    m.semi_additive_behavior = "last_non_empty"
    resp, _ = await _patch_measure(client, m, {"is_additive": True})

    assert resp.status_code == 200, resp.text
    assert m.is_additive is False
