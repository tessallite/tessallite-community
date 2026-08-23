"""Route-level contract for the join population surface (Bug-8615 G5).

The classifier's own rules are unit-tested in
``shared/semantic/tests/test_join_population_validator.py``. This file covers
what only the real ASGI route can prove:

* ``GET .../join-population-health`` projects the persisted verdicts, with the
  rollup, the ``evaluated`` flag and per-row staleness;
* a model with joins but no verdicts reports ``evaluated: false`` rather than a
  clean ``OK`` — the distinction phase G5's block mode depends on;
* ``BLOCKED`` is reported by a route that still answers 200.
"""
from __future__ import annotations

import types
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from shared.db.models import (
    Join,
    JoinPopulationCheck,
    Model,
    ModelColumn,
    ModelTable,
    ModelVersion,
)

from .conftest import (
    TEST_MODEL_ID,
    TEST_PROJECT_ID,
    async_gen_from,
    client,
    make_mock_db,
)

_URL = (
    f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}"
    "/join-population-health"
)


class _Scalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return _Scalars(self._rows)

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


def _fixture(*, checks, deploy_epoch=3, deployed_snapshot=None, check_rows=None):
    """A one-join model plus whatever verdict rows the test wants."""
    fact_id, dim_id = uuid.uuid4(), uuid.uuid4()
    fk_col, pk_col = uuid.uuid4(), uuid.uuid4()
    join = types.SimpleNamespace(
        id=uuid.uuid4(),
        model_id=TEST_MODEL_ID,
        left_table_id=fact_id, right_table_id=dim_id,
        left_column_id=fk_col, right_column_id=pk_col,
        join_type="inner", population_participation="undeclared",
    )
    # The rows are built AFTER the join so a check can fingerprint it.
    rows_by_entity = {
        "joins": [join],
        "join_population_checks": (
            check_rows if check_rows is not None
            else [c(join.id, join) for c in checks]
        ),
        "model_tables": [
            types.SimpleNamespace(
                id=fact_id, alias="sales", display_name="Sales",
                physical_name="demo.fact",
            ),
            types.SimpleNamespace(
                id=dim_id, alias="customer", display_name="Customer",
                physical_name="demo.dim",
            ),
        ],
        "model_columns": [
            types.SimpleNamespace(id=fk_col, column_name="customer_id"),
            types.SimpleNamespace(id=pk_col, column_name="id"),
        ],
    }

    db = make_mock_db()
    model = types.SimpleNamespace(
        id=TEST_MODEL_ID, project_id=TEST_PROJECT_ID, deploy_epoch=deploy_epoch,
    )
    version = None
    if deployed_snapshot is not None:
        version = types.SimpleNamespace(
            id=uuid.uuid4(), snapshot_json=deployed_snapshot,
        )
        model.deployed_version_id = version.id
    db.get = AsyncMock(
        side_effect=lambda entity, pk: (
            model if entity is Model
            else version if entity is ModelVersion else None
        )
    )

    async def _execute(stmt):
        froms = list(stmt.get_final_froms() or [])
        key = froms[0].name if froms else ""
        return _Result(rows_by_entity.get(key, []))

    db.execute = AsyncMock(side_effect=_execute)
    return db, join


def _check(measured_join=None, **kwargs):
    """A stored verdict row.

    ``measured_join`` is the join AS IT WAS when the verdict was measured; its
    classification-input fingerprint is stamped on the row exactly as the
    deploy-time classifier stamps it. Defaulting to the live join means an
    unchanged join produces a matching fingerprint, so only tests that
    deliberately move an input see ``stale``.
    """
    from shared.semantic.join_population_validator import (
        join_definition_fingerprint,
    )

    def _build(join_id, live_join):
        source = measured_join if measured_join is not None else live_join
        return JoinPopulationCheck(
            join_id=join_id, model_id=TEST_MODEL_ID,
            checked_at=datetime.now(UTC),
            inputs_fingerprint=join_definition_fingerprint(source),
            **kwargs,
        )
    return _build


@pytest.mark.asyncio
async def test_health_reports_a_blocked_rollup(client) -> None:
    db, join = _fixture(checks=[_check(
        classification="filtering", population_participation="undeclared",
        status="BLOCKED", measured=True, deploy_epoch=3,
        row_loss_ratio=0.83, row_mult_ratio=0.0, row_effect_ratio=0.83,
        reason="measured",
    )])
    with patch("src.api.join_population_health.get_tenant_db", async_gen_from(db)):
        response = await client.get(_URL)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "BLOCKED"
    assert payload["evaluated"] is True
    assert payload["warn_only"] is False
    assert (payload["join_count"], payload["blocked_count"]) == (1, 1)
    item = payload["items"][0]
    assert item["join_id"] == str(join.id)
    assert item["classification"] == "filtering"
    assert item["row_effect_ratio"] == pytest.approx(0.83)
    assert item["left_table_name"] == "sales"
    assert item["left_column_name"] == "customer_id"
    assert item["stale"] is False


@pytest.mark.asyncio
async def test_deployed_health_and_summary_use_selected_snapshot_after_live_delete(client) -> None:
    """A deployed historical join remains visible after its draft row is gone."""
    ids = types.SimpleNamespace(
        fact=uuid.uuid4(), dim=uuid.uuid4(), fact_col=uuid.uuid4(), dim_col=uuid.uuid4(),
        join=uuid.uuid4(),
    )
    snapshot = {
        "tables": [
            {"id": str(ids.fact), "table_type": "fact", "display_name": "Fact"},
            {"id": str(ids.dim), "table_type": "dim_detail", "display_name": "Customer"},
        ],
        "columns": [
            {"id": str(ids.fact_col), "model_table_id": str(ids.fact), "column_name": "customer_id"},
            {"id": str(ids.dim_col), "model_table_id": str(ids.dim), "column_name": "id", "is_primary_key": True},
        ],
        "joins": [{
            "id": str(ids.join), "left_table_id": str(ids.fact),
            "right_table_id": str(ids.dim), "left_column_id": str(ids.fact_col),
            "right_column_id": str(ids.dim_col), "join_type": "inner",
            "population_participation": "preserve_base_rows",
        }],
    }
    from shared.semantic.join_population_validator import _snapshot_graph, join_definition_fingerprint

    selected_join = _snapshot_graph(snapshot)[0][0]
    check = JoinPopulationCheck(
        join_id=ids.join, model_id=TEST_MODEL_ID, deployed_version_id=uuid.uuid4(),
        deploy_epoch=3, classification="neutral", population_participation="preserve_base_rows",
        status="OK", measured=True, row_loss_ratio=0.0, row_mult_ratio=0.0,
        row_effect_ratio=0.0, reason="measured",
        inputs_fingerprint=join_definition_fingerprint(selected_join),
        join_label="Fact.customer_id ↔ Customer.id",
        left_table_name="Fact", right_table_name="Customer",
        left_column_name="customer_id", right_column_name="id",
        checked_at=datetime.now(UTC),
    )
    db, live_join = _fixture(
        checks=[], deployed_snapshot=snapshot, check_rows=[check],
    )
    assert live_join.id != ids.join, "the live draft deliberately has a different join"
    with patch("src.api.join_population_health.get_tenant_db", async_gen_from(db)):
        response = await client.get(_URL)

    payload = response.json()
    assert [item["join_id"] for item in payload["items"]] == [str(ids.join)]
    item = payload["items"][0]
    assert item["left_table_name"] == "Fact"
    assert item["right_table_name"] == "Customer"
    assert item["population_participation"] == "preserve_base_rows"
    assert item["stale"] is False

    from src.api.join_population_health import summarise_join_population

    summary = await summarise_join_population(db, TEST_MODEL_ID, snapshot=snapshot)
    assert [item["join_id"] for item in summary["items"]] == [str(ids.join)]
    assert summary["items"][0]["left_table_name"] == "Fact"


@pytest.mark.asyncio
async def test_a_model_with_joins_and_no_verdicts_is_not_evaluated(client) -> None:
    """"Nothing is wrong" and "nothing was checked" must not both read as a
    clean OK — phase G5's block mode has to be able to tell them apart."""
    db, _join = _fixture(checks=[])
    with patch("src.api.join_population_health.get_tenant_db", async_gen_from(db)):
        response = await client.get(_URL)

    payload = response.json()
    assert payload["status"] == "OK"
    assert payload["evaluated"] is False
    assert payload["evaluated_count"] == 0
    assert payload["items"][0]["status"] is None
    assert payload["items"][0]["measured"] is False


@pytest.mark.asyncio
async def test_a_verdict_from_an_older_epoch_is_flagged_stale(client) -> None:
    db, _join = _fixture(
        checks=[_check(
            classification="neutral", population_participation="preserve_base_rows",
            status="OK", measured=True, deploy_epoch=1,
        )],
        deploy_epoch=4,
    )
    with patch("src.api.join_population_health.get_tenant_db", async_gen_from(db)):
        response = await client.get(_URL)

    assert response.json()["items"][0]["stale"] is True


@pytest.mark.asyncio
async def test_health_404s_for_a_model_outside_the_project(client) -> None:
    db = make_mock_db()
    db.get = AsyncMock(return_value=types.SimpleNamespace(
        id=TEST_MODEL_ID, project_id=uuid.uuid4(), deploy_epoch=0,
    ))
    with patch("src.api.join_population_health.get_tenant_db", async_gen_from(db)):
        response = await client.get(_URL)
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_the_deploy_summary_never_raises_on_a_broken_session() -> None:
    """It decorates a deploy response, so it must not be able to fail one."""
    from src.api.join_population_health import summarise_join_population

    class _BrokenDB:
        async def execute(self, stmt):
            raise RuntimeError("session gone")

    assert await summarise_join_population(_BrokenDB(), TEST_MODEL_ID) == {}


@pytest.mark.asyncio
async def test_a_redeclared_join_shows_what_the_verdict_was_computed_against(
    client,
) -> None:
    """Bug-8667. ``PATCH /joins/{id}`` does not bump ``deploy_epoch``, so a
    modeller who re-declares a BLOCKED join would otherwise see their NEW
    declaration sitting next to the OLD verdict with ``stale=False`` and no
    hint the two disagree. The check row already stored the declaration it was
    computed against; it just was not surfaced."""
    db, join = _fixture(checks=[_check(
        classification="filtering",
        population_participation="undeclared",   # what was checked
        status="BLOCKED", measured=True, deploy_epoch=3,
        row_loss_ratio=0.83, row_effect_ratio=0.83, reason="measured",
    )])
    join.population_participation = "population_defining"  # re-declared since

    with patch("src.api.join_population_health.get_tenant_db", async_gen_from(db)):
        response = await client.get(_URL)

    item = response.json()["items"][0]
    assert item["population_participation"] == "population_defining"
    assert item["checked_population_participation"] == "undeclared"
    assert item["declaration_changed_since_check"] is True
    # The verdict itself is untouched — it is still what was measured.
    assert item["status"] == "BLOCKED"


@pytest.mark.asyncio
async def test_an_unchanged_declaration_is_not_flagged_as_changed(client) -> None:
    """Mutation partner: the flag must not fire on every join."""
    db, join = _fixture(checks=[_check(
        classification="neutral", population_participation="preserve_base_rows",
        status="OK", measured=True, deploy_epoch=3,
    )])
    join.population_participation = "preserve_base_rows"   # unchanged since
    with patch("src.api.join_population_health.get_tenant_db", async_gen_from(db)):
        response = await client.get(_URL)
    item = response.json()["items"][0]
    assert item["checked_population_participation"] == "preserve_base_rows"
    assert item["declaration_changed_since_check"] is False


@pytest.mark.asyncio
async def test_a_changed_join_type_marks_the_verdict_stale(client) -> None:
    """R5-1, the generalisation of Bug-8667. ``join_type`` is a direct input to
    ``classify_edge`` and ``PATCH /joins/{id}`` does not bump ``deploy_epoch``,
    so a LEFT-measured ``neutral``/``OK`` verdict was still served with
    stale=false after the modeller switched the join to INNER — a clean bill of
    health for an edge that now drops rows. Staleness keys on a fingerprint of
    EVERY classifier input, not on one hand-picked field."""
    measured_as = types.SimpleNamespace(
        join_type="left", population_participation="preserve_base_rows",
        left_table_id=uuid.UUID(int=1), right_table_id=uuid.UUID(int=2),
        left_column_id=uuid.UUID(int=3), right_column_id=uuid.UUID(int=4),
    )
    db, join = _fixture(checks=[_check(
        measured_join=measured_as,
        classification="neutral", population_participation="preserve_base_rows",
        status="OK", measured=True, deploy_epoch=3,
        row_loss_ratio=0.0, row_mult_ratio=0.0, row_effect_ratio=0.0,
        reason="measured",
    )])
    # Same declaration, same epoch — only the join TYPE moved.
    join.join_type = "inner"
    join.population_participation = "preserve_base_rows"
    join.left_table_id = measured_as.left_table_id
    join.right_table_id = measured_as.right_table_id
    join.left_column_id = measured_as.left_column_id
    join.right_column_id = measured_as.right_column_id

    with patch("src.api.join_population_health.get_tenant_db", async_gen_from(db)):
        item = (await client.get(_URL)).json()["items"][0]

    assert item["inputs_changed_since_check"] is True
    assert item["stale"] is True, (
        "join_type changed since the verdict was measured, yet the OK/neutral "
        "verdict was still presented as current"
    )
    # The declaration itself did NOT change — proving staleness widened beyond
    # the one field Bug-8667 covered.
    assert item["declaration_changed_since_check"] is False


@pytest.mark.asyncio
async def test_a_changed_join_column_marks_the_verdict_stale(client) -> None:
    """The join columns are classifier inputs too — the probe counts them."""
    db, join = _fixture(checks=[_check(
        classification="neutral", population_participation="undeclared",
        status="OK", measured=True, deploy_epoch=3,
    )])
    join.right_column_id = uuid.uuid4()      # repointed since the verdict
    with patch("src.api.join_population_health.get_tenant_db", async_gen_from(db)):
        item = (await client.get(_URL)).json()["items"][0]
    assert item["inputs_changed_since_check"] is True
    assert item["stale"] is True


@pytest.mark.asyncio
async def test_an_untouched_join_is_not_flagged_stale(client) -> None:
    """Mutation partner: the widened rule must not fire on every join, or the
    surface becomes noise and stops being read."""
    db, _join = _fixture(checks=[_check(
        classification="neutral", population_participation="undeclared",
        status="OK", measured=True, deploy_epoch=3,
    )])
    with patch("src.api.join_population_health.get_tenant_db", async_gen_from(db)):
        item = (await client.get(_URL)).json()["items"][0]
    assert item["inputs_changed_since_check"] is False
    assert item["stale"] is False
