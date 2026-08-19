"""Tests for hierarchy health monitor (Block D)."""
import types
import pytest
from uuid import uuid4
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException


@pytest.mark.asyncio
async def test_probe_members_denied_for_non_modeler():
    """Bug-8292 [SECURITY]: the live member-integrity probe reads raw source
    member keys (bypassing a viewer's persona/RLS), so it must require the
    modeler role. A viewer requesting probe_members gets 403, not leaked keys."""
    from src.api import hierarchy_health as hh

    project_id = uuid4()
    model_id = uuid4()
    db = AsyncMock()

    async def _gen(_tenant):
        yield db

    with (
        patch.object(hh, "get_tenant_db", _gen),
        patch.object(hh, "enforce_model_scope", lambda *a, **k: None),
        patch.object(hh, "_ensure_model_in_project", new=AsyncMock()),
        # Caller is only a viewer -> not a modeler.
        patch.object(hh, "caller_has_role", new=AsyncMock(return_value=False)),
    ):
        with pytest.raises(HTTPException) as exc:
            await hh.get_hierarchy_health(
                project_id=project_id,
                model_id=model_id,
                probe_members=True,
                current_user=types.SimpleNamespace(tenant_id="t1"),
            )
    assert exc.value.status_code == 403
    assert "modeler" in str(exc.value.detail).lower()


@pytest.mark.asyncio
async def test_probe_members_allowed_for_modeler():
    """A modeler may run the member-integrity probe."""
    from src.api import hierarchy_health as hh

    project_id = uuid4()
    model_id = uuid4()
    db = AsyncMock()

    async def _gen(_tenant):
        yield db

    with (
        patch.object(hh, "get_tenant_db", _gen),
        patch.object(hh, "enforce_model_scope", lambda *a, **k: None),
        patch.object(hh, "_ensure_model_in_project", new=AsyncMock()),
        patch.object(hh, "caller_has_role", new=AsyncMock(return_value=True)),
        patch.object(
            hh, "_get_model_hierarchy_health", new=AsyncMock(return_value=[]),
        ) as get_health,
    ):
        result = await hh.get_hierarchy_health(
            project_id=project_id,
            model_id=model_id,
            probe_members=True,
            current_user=types.SimpleNamespace(tenant_id="t1"),
        )
    assert result == []
    # The probe was passed through to the health computation.
    assert get_health.await_args.kwargs["probe_members"] is True


@pytest.mark.asyncio
async def test_health_check_empty_levels_is_error():
    from src.api.hierarchy_health import _check_hierarchy_health

    db = AsyncMock()
    hier = MagicMock()
    hier.id = uuid4()
    hier.model_id = uuid4()
    hier.type = "explicit"

    levels_result = MagicMock()
    levels_result.scalars.return_value.all.return_value = []

    join_result = MagicMock()
    join_result.all.return_value = []

    fact_result = MagicMock()
    fact_result.scalar_one_or_none.return_value = None

    links_result = MagicMock()
    links_result.scalars.return_value.all.return_value = []

    db.execute = AsyncMock(side_effect=[levels_result, join_result, fact_result, links_result])
    issues = await _check_hierarchy_health(db, hier)
    assert any(i["issue_type"] == "empty_levels" for i in issues)


@pytest.mark.asyncio
async def test_health_check_valid_hierarchy_no_issues():
    from src.api.hierarchy_health import _check_hierarchy_health

    db = AsyncMock()
    hier = MagicMock()
    hier.id = uuid4()
    hier.model_id = uuid4()
    hier.type = "date_embedded"
    hier.date_config = {"source_attribute_id": str(uuid4())}
    level = MagicMock()
    level.ordinal = 1
    level.name = "Year"
    level.key_attribute_id = uuid4()
    level.key_attribute_source = "physical_column"

    levels_result = MagicMock()
    levels_result.scalars.return_value.all.return_value = [level]

    table_id = uuid4()
    fact_id = uuid4()
    join_result = MagicMock()
    join_result.all.return_value = [(fact_id, table_id)]

    fact_result = MagicMock()
    fact_result.scalar_one_or_none.return_value = fact_id

    links_result = MagicMock()
    links_result.scalars.return_value.all.return_value = []

    db.execute = AsyncMock(side_effect=[levels_result, join_result, fact_result, links_result])
    col_mock = MagicMock()
    col_mock.model_table_id = table_id
    db.get = AsyncMock(return_value=col_mock)
    issues = await _check_hierarchy_health(db, hier)
    assert issues == []


@pytest.mark.asyncio
async def test_get_model_health_returns_per_hierarchy_status():
    from src.api.hierarchy_health import _get_model_hierarchy_health

    db = AsyncMock()
    hier = MagicMock()
    hier.id = uuid4()
    hier.name = "Date Hierarchy"
    hier.type = "explicit"

    hier_result = MagicMock()
    hier_result.scalars.return_value.all.return_value = [hier]

    levels_result = MagicMock()
    levels_result.scalars.return_value.all.return_value = []

    join_result = MagicMock()
    join_result.all.return_value = []

    fact_result = MagicMock()
    fact_result.scalar_one_or_none.return_value = None

    links_result = MagicMock()
    links_result.scalars.return_value.all.return_value = []

    db.execute = AsyncMock(side_effect=[hier_result, levels_result, join_result, fact_result, links_result])

    health = await _get_model_hierarchy_health(db, model_id=uuid4())
    assert len(health) == 1
    assert health[0]["status"] == "error"


# ---------------------------------------------------------------------------
# Bug-8510 — the response must SAY whether member integrity was checked.
#
# `status` reports only what the checks that RAN found. With probe_members
# false the member probe never runs, so a hierarchy whose source data is full
# of orphan members still comes back `status: "ok", issues: []`. Every consumer
# used to have to reconstruct the probe state from the request it sent (the
# frontend got that wrong twice), so the API now states it per hierarchy.
# Tier: T1 (producer/consumer contract).
# ---------------------------------------------------------------------------


def _clean_hierarchy():
    """A hierarchy whose METADATA checks all pass, so status is 'ok'."""
    return types.SimpleNamespace(
        id=uuid4(),
        name="Geography",
        model_id=uuid4(),
        type="date_embedded",
        date_config={"source_attribute_id": str(uuid4())},
        dimension_kind="time",
        calendar_type=None,
    )


def _clean_metadata_db(hier):
    """DB serving the four queries a level-less metadata check makes."""
    db = AsyncMock()
    hier_result = MagicMock()
    hier_result.scalars.return_value.all.return_value = [hier]
    levels_result = MagicMock()
    levels_result.scalars.return_value.all.return_value = []
    join_result = MagicMock()
    join_result.all.return_value = []
    fact_result = MagicMock()
    fact_result.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(
        side_effect=[hier_result, levels_result, join_result, fact_result]
    )
    return db


@pytest.mark.asyncio
async def test_metadata_only_result_declares_member_integrity_unchecked():
    """The exact probe_members=false shape: clean metadata, no member issues —
    and an explicit statement that members were never looked at."""
    from src.api.hierarchy_health import _get_model_hierarchy_health

    hier = _clean_hierarchy()
    db = _clean_metadata_db(hier)

    with patch(
        "src.api.hierarchy_member_integrity.probe_hierarchy_member_integrity",
        new=AsyncMock(),
    ) as probe:
        health = await _get_model_hierarchy_health(
            db, hier.model_id, project_id=uuid4(), probe_members=False,
        )

    probe.assert_not_awaited()
    # F-016-08: clean metadata with NO member probe reports "unverified_members",
    # not "ok" — the member checks never ran, so orphans are not ruled out.
    assert health == [{
        "hierarchy_id": str(hier.id),
        "hierarchy_name": "Geography",
        "status": "unverified_members",
        "members_probed": False,
        "issues": [],
    }]


@pytest.mark.asyncio
async def test_fully_scanned_probe_declares_member_integrity_checked():
    from src.api.hierarchy_health import _get_model_hierarchy_health
    from src.api.hierarchy_member_integrity import MemberIntegrityProbeResult

    hier = _clean_hierarchy()
    db = _clean_metadata_db(hier)

    with patch(
        "src.api.hierarchy_member_integrity.probe_hierarchy_member_integrity",
        new=AsyncMock(return_value=MemberIntegrityProbeResult(
            issues=[], pairs_total=2, pairs_scanned=2,
        )),
    ):
        health = await _get_model_hierarchy_health(
            db, hier.model_id, project_id=uuid4(), probe_members=True,
        )

    assert health[0]["status"] == "ok"
    assert health[0]["members_probed"] is True


@pytest.mark.asyncio
async def test_partially_scanned_probe_does_not_claim_member_coverage():
    """The subtle false-healthy case: the probe RAN but skipped a level pair.

    The skip note is info severity, so there is no error/warning finding — but
    because coverage was partial (members_probed False), F-016-08 reports
    "unverified_members", not "ok". A consumer must not read a partial probe as
    a full clean bill of health.
    """
    from src.api.hierarchy_health import _get_model_hierarchy_health
    from src.api.hierarchy_member_integrity import MemberIntegrityProbeResult

    hier = _clean_hierarchy()
    db = _clean_metadata_db(hier)
    skip_note = {
        "issue_type": "member_integrity_unprobed",
        "severity": "info",
        "detail": {"parent_level": "Country", "child_level": "City"},
    }

    with patch(
        "src.api.hierarchy_member_integrity.probe_hierarchy_member_integrity",
        new=AsyncMock(return_value=MemberIntegrityProbeResult(
            issues=[skip_note], pairs_total=2, pairs_scanned=1,
        )),
    ):
        health = await _get_model_hierarchy_health(
            db, hier.model_id, project_id=uuid4(), probe_members=True,
        )

    assert health[0]["status"] == "unverified_members"
    assert health[0]["members_probed"] is False


@pytest.mark.asyncio
async def test_probe_requested_without_project_context_fails_closed():
    """No project context means the probe cannot resolve a connection and is
    skipped. The response must not imply it ran."""
    from src.api.hierarchy_health import _get_model_hierarchy_health

    hier = _clean_hierarchy()
    db = _clean_metadata_db(hier)

    health = await _get_model_hierarchy_health(
        db, hier.model_id, project_id=None, probe_members=True,
    )

    assert health[0]["members_probed"] is False


@pytest.mark.asyncio
async def test_endpoint_serialises_members_probed_to_the_client():
    """Producer/consumer alignment: the field must survive the response model,
    not be computed and then dropped on the way out."""
    from src.api import hierarchy_health as hh

    db = AsyncMock()

    async def _gen(_tenant):
        yield db

    entry = {
        "hierarchy_id": str(uuid4()),
        "hierarchy_name": "Geography",
        "status": "ok",
        "members_probed": False,
        "issues": [],
    }
    with (
        patch.object(hh, "get_tenant_db", _gen),
        patch.object(hh, "enforce_model_scope", lambda *a, **k: None),
        patch.object(hh, "_ensure_model_in_project", new=AsyncMock()),
        patch.object(
            hh, "_get_model_hierarchy_health", new=AsyncMock(return_value=[entry]),
        ),
    ):
        result = await hh.get_hierarchy_health(
            project_id=uuid4(),
            model_id=uuid4(),
            probe_members=False,
            current_user=types.SimpleNamespace(tenant_id="t1"),
        )

    assert result[0].members_probed is False
    assert result[0].model_dump()["members_probed"] is False


@pytest.mark.asyncio
async def test_unreachable_table_in_disconnected_join_component():
    """A table in a disconnected component (not reachable from fact) is flagged."""
    from src.api.hierarchy_health import _check_hierarchy_health

    db = AsyncMock()
    hier = MagicMock()
    hier.id = uuid4()
    hier.model_id = uuid4()
    hier.type = "explicit"

    fact_id = uuid4()
    dim_connected = uuid4()
    dim_disconnected = uuid4()
    dim_other = uuid4()

    level = MagicMock()
    level.ordinal = 1
    level.name = "Disconnected Level"
    level.key_attribute_id = uuid4()
    level.key_attribute_source = "physical_column"

    levels_result = MagicMock()
    levels_result.scalars.return_value.all.return_value = [level]

    # Join graph: fact--dim_connected, dim_disconnected--dim_other (disconnected)
    join_result = MagicMock()
    join_result.all.return_value = [
        (fact_id, dim_connected),
        (dim_disconnected, dim_other),
    ]

    fact_result = MagicMock()
    fact_result.scalar_one_or_none.return_value = fact_id

    links_result = MagicMock()
    links_result.scalars.return_value.all.return_value = []

    db.execute = AsyncMock(side_effect=[levels_result, join_result, fact_result, links_result])
    col_mock = MagicMock()
    col_mock.model_table_id = dim_disconnected
    db.get = AsyncMock(return_value=col_mock)

    issues = await _check_hierarchy_health(db, hier)
    assert any(i["issue_type"] == "unreachable_level_table" for i in issues)


@pytest.mark.asyncio
async def test_level_on_fact_table_is_reachable():
    """A level on the fact table itself should not be flagged as unreachable."""
    from src.api.hierarchy_health import _check_hierarchy_health

    db = AsyncMock()
    hier = MagicMock()
    hier.id = uuid4()
    hier.model_id = uuid4()
    hier.type = "explicit"

    fact_id = uuid4()

    level = MagicMock()
    level.ordinal = 1
    level.name = "Fact Level"
    level.key_attribute_id = uuid4()
    level.key_attribute_source = "physical_column"

    levels_result = MagicMock()
    levels_result.scalars.return_value.all.return_value = [level]

    # No joins at all
    join_result = MagicMock()
    join_result.all.return_value = []

    fact_result = MagicMock()
    fact_result.scalar_one_or_none.return_value = fact_id

    links_result = MagicMock()
    links_result.scalars.return_value.all.return_value = []

    db.execute = AsyncMock(side_effect=[levels_result, join_result, fact_result, links_result])
    col_mock = MagicMock()
    col_mock.model_table_id = fact_id
    db.get = AsyncMock(return_value=col_mock)

    issues = await _check_hierarchy_health(db, hier)
    assert not any(i["issue_type"] == "unreachable_level_table" for i in issues)
