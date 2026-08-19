"""Bug-8712: the agent's prompt catalogue must list PUBLISHED definitions.

The conversational agent is a consumption surface. Its prompt tells the LLM which
KPIs and named sets exist, and the LLM picks tool arguments from that list, so an
entry there is a promise the rest of the system has to keep.

Before this fix the assembler read LIVE rows: a modeller's rename reached every
user's agent with no Deploy, and a set created since the last deploy was
advertised even though the preview route withholds it — so every tool call
against it was guaranteed to fail.

The adapter under test routes through ``shared.deploy_resolver_core``, the same
authority the model-service resolvers use, rather than re-implementing the
fail-closed rules a third time (Bug-8384).
"""
from __future__ import annotations

import logging
import types
import uuid
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.prompt import assembler as A
from src.prompt.deployed_catalogue import pin_catalogue_to_deployed

MODEL_ID = uuid.uuid4()
VERSION_ID = uuid.uuid4()
PROJECT_ID = uuid.uuid4()

KPI_ID = uuid.uuid4()
NS_ID = uuid.uuid4()
UNPUBLISHED_NS_ID = uuid.uuid4()


@dataclass
class _Row:
    """Stands in for _KpiInfo / _NamedSetInfo, which share this shape."""

    id: uuid.UUID
    name: str
    display_name: str | None
    description: str | None
    certification_status: str


def _model(deployed: bool = True):
    return types.SimpleNamespace(
        id=MODEL_ID,
        deployed_version_id=VERSION_ID if deployed else None,
        deploy_epoch=1,
    )


def _version(snapshot: dict):
    return types.SimpleNamespace(
        id=VERSION_ID, model_id=MODEL_ID, snapshot_json=snapshot,
    )


def _snapshot(family_rows: dict) -> dict:
    return {
        "schema_version": "1.0",
        "measures": [{"id": str(uuid.uuid4()), "name": "revenue"}],
        **family_rows,
    }


class _FakeDb:
    def __init__(self, version):
        self._version = version

    async def get(self, _cls, _id):
        return self._version


# ---------------------------------------------------------------------------
# The adapter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_catalogue_serves_the_deployed_name_not_the_draft_rename():
    db = _FakeDb(
        _version(
            _snapshot(
                {
                    "named_sets": [
                        {
                            "id": str(NS_ID),
                            "name": "Top Accounts",
                            "display_name": "Top Accounts",
                            "description": "The published description.",
                        }
                    ]
                }
            )
        )
    )
    rows = [
        _Row(NS_ID, "Top Accounts DRAFT", "Top Accounts DRAFT", "Half-written.", "certified"),
    ]

    served = await pin_catalogue_to_deployed(
        db, _model(), family="named_sets", rows=rows,
    )

    assert [r.name for r in served] == ["Top Accounts"]
    assert served[0].description == "The published description."
    # Governance stays live so a deprecation still lands without a redeploy.
    assert served[0].certification_status == "certified"


@pytest.mark.asyncio
async def test_entity_created_since_last_deploy_is_not_advertised():
    db = _FakeDb(
        _version(
            _snapshot({"named_sets": [{"id": str(NS_ID), "name": "Top Accounts"}]})
        )
    )
    rows = [
        _Row(NS_ID, "Top Accounts", None, None, "certified"),
        _Row(UNPUBLISHED_NS_ID, "Brand New", None, None, "certified"),
    ]

    served = await pin_catalogue_to_deployed(
        db, _model(), family="named_sets", rows=rows,
    )

    assert [r.id for r in served] == [NS_ID]


@pytest.mark.asyncio
async def test_invalid_deployed_snapshot_withholds_everything_and_warns(caplog):
    """Fail closed. A prompt has no 409 to raise, but it must not serve drafts."""
    db = _FakeDb(_version({"schema_version": "1.0"}))
    rows = [_Row(NS_ID, "Top Accounts", None, None, "certified")]

    with caplog.at_level(logging.WARNING, logger="src.prompt.deployed_catalogue"):
        served = await pin_catalogue_to_deployed(
            db, _model(), family="named_sets", rows=rows,
        )

    assert served == []
    assert any(
        rec.name == "src.prompt.deployed_catalogue"
        and rec.levelno == logging.WARNING
        and "DEPLOYED_SNAPSHOT_INVALID" in rec.getMessage()
        for rec in caplog.records
    )


@pytest.mark.asyncio
async def test_undeployed_model_has_no_catalogue_for_a_consumption_surface():
    db = _FakeDb(None)
    rows = [_Row(KPI_ID, "Net Revenue", None, None, "certified")]

    served = await pin_catalogue_to_deployed(
        db, _model(deployed=False), family="kpis", rows=rows,
    )

    assert served == []


@pytest.mark.asyncio
async def test_order_is_re_derived_from_the_published_names():
    """A draft rename must not reach the prompt through the ORDER either."""
    other_id = uuid.uuid4()
    db = _FakeDb(
        _version(
            _snapshot(
                {
                    "kpis": [
                        {"id": str(KPI_ID), "name": "Zulu"},
                        {"id": str(other_id), "name": "Alpha"},
                    ]
                }
            )
        )
    )
    # Live order (by live name) is the reverse of the published order.
    rows = [
        _Row(KPI_ID, "Aardvark draft rename", None, None, "certified"),
        _Row(other_id, "Beta draft rename", None, None, "certified"),
    ]

    served = await pin_catalogue_to_deployed(db, _model(), family="kpis", rows=rows)

    assert [r.name for r in served] == ["Alpha", "Zulu"]


# ---------------------------------------------------------------------------
# The wiring: the assembler actually reaches the adapter
# ---------------------------------------------------------------------------


def _routed_execute(*, kpis, named_sets, models, ctxs):
    """Answer per SELECTED-FROM table, not by call order."""

    def _result(rows, scalar=False):
        res = MagicMock()
        res.all.return_value = rows
        res.scalars.return_value.all.return_value = rows
        return res

    async def _execute(stmt, *args, **kwargs):
        sql = str(stmt)
        if "FROM models" in sql:
            return _result(models)
        if "project_agent_model_contexts" in sql:
            return _result(ctxs)
        if "FROM kpis" in sql:
            return _result(kpis)
        if "FROM named_sets" in sql:
            return _result(named_sets)
        return _result([])

    return _execute


@pytest.mark.asyncio
async def test_assembler_profile_lists_the_published_catalogue():
    model = MagicMock()
    model.id = MODEL_ID
    model.slug = "modely"
    model.display_name = "ModelY"
    model.glossary_max_distinct = 20
    model.deployed_version_id = VERSION_ID
    model.deploy_epoch = 1

    version = _version(
        _snapshot(
            {
                "kpis": [{"id": str(KPI_ID), "name": "Net Revenue"}],
                "named_sets": [{"id": str(NS_ID), "name": "Top Accounts"}],
            }
        )
    )

    db = AsyncMock()
    db.execute = _routed_execute(
        kpis=[(KPI_ID, "Net Revenue DRAFT", None, None, "certified")],
        named_sets=[
            (NS_ID, "Top Accounts DRAFT", None, None, "certified"),
            (UNPUBLISHED_NS_ID, "Never Deployed", None, None, "certified"),
        ],
        models=[model],
        ctxs=[],
    )
    db.get = AsyncMock(return_value=version)

    with patch.object(
        A, "list_model_attributes", AsyncMock(return_value=(["revenue"], ["country"])),
    ):
        profiles = await A._load_model_profiles(db, PROJECT_ID, [MODEL_ID])

    assert len(profiles) == 1
    profile = profiles[0]
    assert [k.name for k in profile.kpis] == ["Net Revenue"]
    assert [n.name for n in profile.named_sets] == ["Top Accounts"]
    assert "Never Deployed" not in [n.name for n in profile.named_sets]


# ---------------------------------------------------------------------------
# The agent's preview transport
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_preview_requests_the_deployed_definition():
    """The members the agent quotes back come from the published expression.

    The parameter IS the mechanism on this side; the server-side proof that it
    changes which definition is computed lives in the model-service guard
    ``test_bug_8712_named_set_preview_deploy_authority``.
    """
    from src.pipeline import _run_preview_named_set_branch
    from src.tools.spec import PreviewNamedSetToolCall

    from .test_read_tool_project_path import _cfg, _mock_client

    captured: dict = {}
    payload = {"items": [{"caption": "North"}], "total_count": 1, "truncated": False}
    with (
        patch("src.pipeline.httpx.AsyncClient", _mock_client(captured, payload)),
        patch(
            "src.pipeline.apply_output_guardrails",
            lambda cfg, text: types.SimpleNamespace(text=text, actions=[]),
        ),
        patch("src.pipeline._narration_publisher", lambda cfg, pub: None),
    ):
        outcome = await _run_preview_named_set_branch(
            _cfg(),
            PreviewNamedSetToolCall(model_id=str(MODEL_ID), named_set_id=str(NS_ID)),
            "jwt", None, project_id=PROJECT_ID,
        )

    assert "deployed_only=true" in captured["url"]
    assert outcome.status == "ok"
