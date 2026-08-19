"""Bug-8411 — per-project agent webhook event subscriptions (backend half).

The architecture doc for the conversational agent has always specified an
"event subscription checkboxes" surface on the agent webhook settings tab,
but no backend for it existed: ``ProjectAgentConfig`` had ``webhook_url`` and
``webhook_signing_secret`` and nothing else, so every configured receiver got
all five agent events unconditionally.

This suite covers the backend contract the UI depends on:
  - one catalogue, and it matches what the dispatcher actually emits;
  - the subscription filter's fail-open/fail-closed semantics;
  - write-time validation (the Bug-7330 empty-list trap in particular);
  - the dispatcher honouring the subscription, and NOT honouring it for an
    explicit manual DLQ retry.

Test escape: the feature did not exist. Guard: this file. Tier: T1
(producer/consumer contract between the config API, the dispatcher and the
Settings UI).
"""
from __future__ import annotations

import ast
import pathlib
import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch as _patch

import httpx
import pytest

from shared.webhooks.agent_event_types import (
    AGENT_WEBHOOK_EVENT_LABELS,
    AGENT_WEBHOOK_EVENT_TYPES,
    DEFAULT_AGENT_EVENT_FILTERS,
    WILDCARD,
    agent_event_catalogue,
    agent_event_subscribed,
    is_valid_agent_filter,
)
from src.auth.middleware import CurrentUser, get_current_user
from src.main import app

SRC_ROOT = pathlib.Path(__file__).resolve().parents[1] / "src"


# ---------------------------------------------------------------------------
# The catalogue must equal what is actually dispatched
# ---------------------------------------------------------------------------


class _EmitCollector(ast.NodeVisitor):
    """Collect every ``event_type=`` value passed to ``dispatch_event``.

    Resolves a variable argument (``event_type=event``) back to the string
    constants assigned to that name in the enclosing function, which is how
    ``_emit_turn_webhook`` picks between ``turn.refused`` and
    ``turn.completed``. Anything it cannot resolve to concrete strings raises
    -- a coverage tool that silently skips a call site it does not understand
    would let a new, uncatalogued event ship unnoticed, which is precisely
    the failure mode this test exists to prevent.
    """

    def __init__(self) -> None:
        self.events: set[str] = set()
        self._assignments: dict[str, set[str]] = {}

    # Collect name -> {string constants} first, per module. Function-local
    # shadowing is not modelled; if two functions bind the same name to
    # different event strings the union is used, which can only ever make
    # this test stricter, never laxer.
    def visit_Assign(self, node: ast.Assign) -> None:
        strings = _string_constants(node.value)
        if strings:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self._assignments.setdefault(target.id, set()).update(strings)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        name = getattr(func, "id", None) or getattr(func, "attr", None)
        if name == "dispatch_event":
            for kw in node.keywords:
                if kw.arg != "event_type":
                    continue
                strings = _string_constants(kw.value)
                if not strings and isinstance(kw.value, ast.Name):
                    strings = self._assignments.get(kw.value.id, set())
                if (
                    not strings
                    and isinstance(kw.value, ast.Attribute)
                    and kw.value.attr == "event_type"
                ):
                    # A REPLAY, not a new event source: `retry_dlq` passes the
                    # stored `row.event_type` back into dispatch_event. Its
                    # value can only ever be a name some real emit site
                    # already produced, so it contributes nothing new to the
                    # catalogue. Narrow on purpose -- any other unresolvable
                    # shape still raises below.
                    continue
                if not strings:
                    raise AssertionError(
                        "could not statically resolve the event_type passed to "
                        f"dispatch_event at line {node.lineno}; extend this "
                        "collector rather than letting the event go "
                        "uncatalogued"
                    )
                self.events.update(strings)
        self.generic_visit(node)


def _string_constants(node: ast.AST) -> set[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.IfExp):
        return _string_constants(node.body) | _string_constants(node.orelse)
    return set()


class TestAgentEventCatalogueMatchesEmitters:
    def test_catalogue_is_exactly_what_is_dispatched(self):
        collector = _EmitCollector()
        scanned = 0
        for path in SRC_ROOT.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            collector.visit(tree)
            scanned += 1
        assert scanned > 0, "no source files scanned -- SRC_ROOT is wrong"
        assert collector.events, (
            "no dispatch_event call sites found -- the collector is broken, "
            "not the code"
        )
        assert collector.events == set(AGENT_WEBHOOK_EVENT_TYPES), (
            "the agent webhook event catalogue and the events actually "
            "dispatched have drifted; "
            f"emitted-not-catalogued={sorted(collector.events - AGENT_WEBHOOK_EVENT_TYPES)} "
            f"catalogued-not-emitted={sorted(AGENT_WEBHOOK_EVENT_TYPES - collector.events)}"
        )

    def test_catalogue_rows_are_labelled_and_ordered(self):
        rows = agent_event_catalogue()
        assert [r["value"] for r in rows] == list(AGENT_WEBHOOK_EVENT_LABELS)
        assert all(r["label"] for r in rows)


# ---------------------------------------------------------------------------
# Subscription semantics — fail OPEN on unknown, fail CLOSED on explicit
# ---------------------------------------------------------------------------


class TestAgentEventSubscribed:
    def test_null_filters_deliver_everything(self):
        """A row that predates the column must not silently stop delivering."""
        for event in AGENT_WEBHOOK_EVENT_TYPES:
            assert agent_event_subscribed(event, None) is True

    def test_wildcard_delivers_everything(self):
        for event in AGENT_WEBHOOK_EVENT_TYPES:
            assert agent_event_subscribed(event, [WILDCARD]) is True

    def test_default_is_the_wildcard(self):
        assert DEFAULT_AGENT_EVENT_FILTERS == [WILDCARD]

    def test_explicit_subset_excludes_the_rest(self):
        filters = ["turn.completed"]
        assert agent_event_subscribed("turn.completed", filters) is True
        assert agent_event_subscribed("turn.feedback", filters) is False
        assert agent_event_subscribed("conversation.started", filters) is False

    def test_empty_list_is_not_coerced_to_match_all(self):
        """Bug-7330 on the platform-wide sibling coerced [] to match-all, so a
        subscriber who deselected everything kept receiving everything. This
        must never be repeated here."""
        for event in AGENT_WEBHOOK_EVENT_TYPES:
            assert agent_event_subscribed(event, []) is False

    def test_corrupt_non_list_value_delivers_rather_than_dropping(self):
        for corrupt in ("turn.completed", {"turn.completed": True}, 7):
            assert agent_event_subscribed("turn.completed", corrupt) is True

    def test_is_valid_agent_filter(self):
        assert is_valid_agent_filter(WILDCARD)
        assert is_valid_agent_filter("turn.judge_blocked")
        assert not is_valid_agent_filter("model.published")  # platform event
        assert not is_valid_agent_filter("turn.nonexistent")


# ---------------------------------------------------------------------------
# Write-time validation
# ---------------------------------------------------------------------------


class TestConfigValidation:
    def _validate(self, filters):
        from src.api.agent_config import _validate_webhook_event_filters

        return _validate_webhook_event_filters(filters)

    def test_none_and_valid_values_accepted(self):
        self._validate(None)
        self._validate([WILDCARD])
        self._validate(["turn.completed", "turn.feedback"])

    def test_empty_list_rejected_with_actionable_detail(self):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            self._validate([])
        assert exc.value.status_code == 400
        assert "must not be empty" in exc.value.detail

    def test_unknown_event_rejected(self):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            self._validate(["turn.completed", "model.published"])
        assert exc.value.status_code == 400
        assert "model.published" in exc.value.detail

    def test_non_string_member_rejected(self):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            self._validate(["turn.completed", 3])
        assert exc.value.status_code == 400

    @pytest.mark.asyncio
    async def test_patch_config_rejects_unknown_event_before_touching_the_db(self):
        from src.api import agent_config

        async def _explode(*_a, **_kw):
            raise AssertionError("validation must run before any DB session")
            yield  # pragma: no cover

        from fastapi import HTTPException

        body = agent_config.AgentConfigPatch(
            webhook_event_filters=["not.an.event"]
        )
        with (
            _patch.object(agent_config, "get_tenant_db", _explode),
            _patch.object(agent_config, "_require_project_modeller", AsyncMock()),
        ):
            with pytest.raises(HTTPException) as exc:
                await agent_config.patch_agent_config(
                    uuid.uuid4(), body,
                    types.SimpleNamespace(
                        user_id="m@example.com", tenant_id="acme", role="modeler",
                    ),
                )
        assert exc.value.status_code == 400

    @pytest.mark.asyncio
    async def test_patch_config_persists_a_valid_subscription(self):
        from src.api import agent_config

        record = types.SimpleNamespace(
            id=uuid.uuid4(), project_id=uuid.uuid4(),
            webhook_url="https://hooks.example.com/agent",
            webhook_signing_secret=b"already-set",
            webhook_event_filters=[WILDCARD],
            enabled=False,
        )
        db = AsyncMock()
        db.commit = AsyncMock()
        db.refresh = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = record
        db.execute = AsyncMock(return_value=result)

        async def _db_gen(_tenant_id):
            yield db

        body = agent_config.AgentConfigPatch(
            webhook_event_filters=["turn.completed", "turn.refused"]
        )
        with (
            _patch.object(agent_config, "get_tenant_db", _db_gen),
            _patch.object(agent_config, "_require_project_modeller", AsyncMock()),
        ):
            await agent_config.patch_agent_config(
                record.project_id, body,
                types.SimpleNamespace(
                    user_id="m@example.com", tenant_id="acme", role="modeler",
                ),
            )
        assert record.webhook_event_filters == ["turn.completed", "turn.refused"]

    @pytest.mark.asyncio
    async def test_patch_without_the_field_leaves_the_subscription_alone(self):
        """PATCH is exclude_unset: omitting the field must not reset an
        operator's carefully chosen subset back to the wildcard."""
        from src.api import agent_config

        record = types.SimpleNamespace(
            id=uuid.uuid4(), project_id=uuid.uuid4(),
            webhook_url="https://hooks.example.com/agent",
            webhook_signing_secret=b"already-set",
            webhook_event_filters=["turn.feedback"],
            enabled=False,
            display_name=None,
        )
        db = AsyncMock()
        db.commit = AsyncMock()
        db.refresh = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = record
        db.execute = AsyncMock(return_value=result)

        async def _db_gen(_tenant_id):
            yield db

        body = agent_config.AgentConfigPatch(display_name="Renamed")
        with (
            _patch.object(agent_config, "get_tenant_db", _db_gen),
            _patch.object(agent_config, "_require_project_modeller", AsyncMock()),
        ):
            await agent_config.patch_agent_config(
                record.project_id, body,
                types.SimpleNamespace(
                    user_id="m@example.com", tenant_id="acme", role="modeler",
                ),
            )
        assert record.webhook_event_filters == ["turn.feedback"]


# ---------------------------------------------------------------------------
# The dispatcher honours the subscription
# ---------------------------------------------------------------------------


def _cfg(filters):
    return types.SimpleNamespace(
        enabled=True,
        webhook_url="https://receiver.example/hooks/abc",
        webhook_signing_secret=b"encrypted-bytes",
        webhook_event_filters=filters,
    )


def _db_gen_for(cfg, dlq_sink):
    async def _gen(_tenant_id):
        db = MagicMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = cfg
        db.execute = AsyncMock(return_value=result)
        db.add = lambda row: dlq_sink.append(row)
        db.commit = AsyncMock()
        db.get = AsyncMock(return_value=None)
        yield db

    return _gen


class TestDispatcherHonoursSubscription:
    async def _dispatch(self, filters, event_type, *, source_dlq_id=None):
        from src.webhooks import dispatcher as disp

        dlq_sink: list = []
        post = AsyncMock(return_value=(True, 204, None))
        with (
            _patch.object(disp, "get_tenant_db", _db_gen_for(_cfg(filters), dlq_sink)),
            _patch.object(disp, "_post_once", post),
            _patch.object(disp, "_decrypt_secret", lambda _b: "a" * 40),
        ):
            await disp.dispatch_event(
                tenant_id="acme",
                project_id=uuid.uuid4(),
                event_type=event_type,
                payload={"k": "v"},
                source_dlq_id=source_dlq_id,
            )
        return post, dlq_sink

    @pytest.mark.asyncio
    async def test_subscribed_event_is_delivered(self):
        post, dlq = await self._dispatch(["turn.completed"], "turn.completed")
        assert post.await_count == 1
        assert dlq == []

    @pytest.mark.asyncio
    async def test_unsubscribed_event_is_not_posted_and_not_dlqd(self):
        """Dropping an event the operator deselected is not a delivery
        failure: posting it would be wrong, and DLQ'ing it would fill the
        operator's queue with events they chose not to receive."""
        post, dlq = await self._dispatch(["turn.completed"], "turn.feedback")
        assert post.await_count == 0
        assert dlq == []

    @pytest.mark.asyncio
    async def test_wildcard_delivers_every_event(self):
        for event in AGENT_WEBHOOK_EVENT_TYPES:
            post, dlq = await self._dispatch([WILDCARD], event)
            assert post.await_count == 1, event

    @pytest.mark.asyncio
    async def test_null_filters_still_deliver(self):
        post, _ = await self._dispatch(None, "turn.feedback")
        assert post.await_count == 1

    @pytest.mark.asyncio
    async def test_manual_dlq_retry_is_not_filtered(self):
        """An operator clicking Retry on a specific queued row is an explicit
        per-row instruction. Filtering it would make the button a silent
        no-op on any row whose event type was deselected after it queued,
        leaving a row that neither delivers nor clears."""
        post, _ = await self._dispatch(
            ["turn.completed"], "turn.feedback", source_dlq_id=uuid.uuid4()
        )
        assert post.await_count == 1


# ---------------------------------------------------------------------------
# The catalogue endpoint the UI reads
# ---------------------------------------------------------------------------


class TestEventTypesEndpoint:
    @pytest.mark.asyncio
    async def test_endpoint_serves_the_shared_catalogue(self):
        project_id = uuid.uuid4()
        app.dependency_overrides[get_current_user] = lambda: CurrentUser(
            user_id="probe@example.com",
            tenant_id="acme",
            email="probe@example.com",
            role="tenant_admin",
        )
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
            ) as client:
                resp = await client.get(
                    f"/api/v1/projects/{project_id}/agent/webhook/event-types"
                )
        finally:
            app.dependency_overrides.pop(get_current_user, None)

        assert resp.status_code == 200
        assert resp.json() == agent_event_catalogue()

    @pytest.mark.asyncio
    async def test_endpoint_is_behind_the_project_gate(self):
        """Bug-8356 — the router-level gate covers this new route too, with
        no extra work by its author. That is the whole point of gating the
        router rather than each handler."""
        from tests.test_bug_8356_webhook_project_idor import (
            PROJECT_A, PROJECT_B, _binding, _request,
        )

        resp = await _request(
            "GET",
            f"/api/v1/projects/{PROJECT_A}/agent/webhook/event-types",
            bindings=[_binding(project_id=PROJECT_B, role="admin")],
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# External Codex cross-family gate finding (2026-07-29): the OTHER write path
# ---------------------------------------------------------------------------


class TestSharedFilterValidator:
    """Validation had to move out of the config API and into the shared
    catalogue module, because the API is not the only writer.

    Project import copies ``webhook_event_filters`` out of an untyped bundle
    straight into the ORM object. An imported JSON STRING like
    ``"turn.feedback"`` -- which reads to a human as "only feedback please" --
    is not a list, so ``agent_event_subscribed`` fell through to its
    deliver-everything branch and the receiver got every conversation event
    the operator had deselected. That is the Bug-7330 outcome reached through
    a door the Bug-7330 fix never covered.
    """

    def test_none_passes_through(self):
        from shared.webhooks.agent_event_types import validate_agent_event_filters

        assert validate_agent_event_filters(None) is None

    def test_valid_list_is_returned(self):
        from shared.webhooks.agent_event_types import validate_agent_event_filters

        assert validate_agent_event_filters(["turn.completed"]) == ["turn.completed"]
        assert validate_agent_event_filters([WILDCARD]) == [WILDCARD]

    @pytest.mark.parametrize(
        "bad",
        [
            "turn.feedback",          # the gate's exact repro: a JSON string
            {"turn.feedback": True},  # a dict
            7,                        # a number
            [],                       # empty -- must never mean "everything"
            ["model.published"],      # a platform event, not an agent event
            ["turn.completed", 3],    # a non-string member
        ],
    )
    def test_malformed_values_are_rejected(self, bad):
        from shared.webhooks.agent_event_types import (
            InvalidAgentEventFilters,
            validate_agent_event_filters,
        )

        with pytest.raises(InvalidAgentEventFilters):
            validate_agent_event_filters(bad)

    def test_the_api_validator_delegates_to_the_shared_one(self):
        """The two writers must not be able to drift again: the API's
        validator is an HTTP-status adapter over the shared rule, not a second
        copy of it."""
        import inspect

        from src.api.agent_config import _validate_webhook_event_filters

        source = inspect.getsource(_validate_webhook_event_filters)
        assert "validate_agent_event_filters(" in source, (
            "the config API re-implements the rule instead of calling the "
            "shared primitive -- that is exactly how the import path drifted"
        )


class TestImportRejectsMalformedSubscription:
    """Behavioural cover for the import write path.

    The first version of this cover inspected the rehydrator's SOURCE for a
    call to the validator, with a comment claiming a live database would be
    needed to do better. The external Codex re-gate disproved that by running
    the real ``import_project`` against a mock session, and was right to
    reject a source-text assertion: it cannot show that the imported value
    actually reaches the validator, that a rejection is handled rather than
    aborting the import, that nothing invalid is restored, or that the
    warning reaches the caller. These drive the real function.
    """

    @staticmethod
    def _bundle(filters):
        return {
            "schema_version": 1,
            "export_format": "tessallite-project/v1",
            "exported_at": "2026-07-29T00:00:00Z",
            "exported_from": {
                "tenant_slug": "src", "project_id": str(uuid.uuid4()),
            },
            "credentials_included": False,
            "credentials_envelope": None,
            "included_sections": ["agent_config"],
            "project": {
                "slug": "imported", "display_name": "Imported", "is_active": True,
            },
            "models": [],
            "agent_config": {
                "config": {
                    "enabled": False,
                    "webhook_url": "https://receiver.example/hooks/abc",
                    "webhook_event_filters": filters,
                },
                "models": [],
                "model_contexts": [],
                "rubrics": [],
                "recipes": [],
                "personas": [],
            },
            "test_metadata": None,
        }

    @staticmethod
    def _db():
        db = AsyncMock()
        db.add = MagicMock()
        db.flush = AsyncMock()
        db.commit = AsyncMock()
        db.delete = AsyncMock()
        db.refresh = AsyncMock()
        empty = MagicMock()
        empty.scalar_one_or_none.return_value = None
        empty.scalars.return_value.all.return_value = []
        empty.all.return_value = []
        db.execute = AsyncMock(return_value=empty)
        db.get = AsyncMock(return_value=None)
        return db

    async def _run(self, filters):
        from shared.db.models import ProjectAgentConfig
        from shared.model_snapshot.project_rehydrator import import_project

        db = self._db()
        result = await import_project(self._bundle(filters), db, mode="create")
        configs = [
            c.args[0] for c in db.add.call_args_list
            if isinstance(c.args[0], ProjectAgentConfig)
        ]
        assert len(configs) == 1, (
            f"expected exactly one ProjectAgentConfig to be added, got "
            f"{len(configs)}"
        )
        return configs[0], result

    @pytest.mark.asyncio
    async def test_valid_subset_is_restored_unchanged(self):
        record, result = await self._run(["turn.completed", "turn.feedback"])
        assert record.webhook_event_filters == ["turn.completed", "turn.feedback"]
        assert not [w for w in result.get("warnings", []) if "subscription" in w]

    @pytest.mark.asyncio
    async def test_malformed_string_is_not_restored_and_warns_the_admin(self):
        """The gate's exact repro. ``"turn.feedback"`` reads to a human as
        "only feedback please", but it is a JSON string, not a list, and the
        dispatcher's reader treats a non-list as deliver-everything -- so
        restoring it verbatim would over-deliver every conversation event the
        operator had deselected."""
        record, result = await self._run("turn.feedback")
        assert getattr(record, "webhook_event_filters", None) is None, (
            "a malformed imported subscription was written to the ORM object; "
            "it must not be restored at all"
        )
        assert any(
            "event subscription was not restored" in w.detail
            for w in result.get("warnings", [])
        ), (
            f"the importing admin got no warning; warnings={result.get('warnings')}"
        )

    @pytest.mark.asyncio
    async def test_empty_list_is_not_restored_and_warns(self):
        record, result = await self._run([])
        assert getattr(record, "webhook_event_filters", None) is None
        assert any(
            "event subscription was not restored" in w.detail
            for w in result.get("warnings", [])
        )

    @pytest.mark.asyncio
    async def test_unknown_event_name_is_not_restored_and_warns(self):
        record, result = await self._run(["model.published"])
        assert getattr(record, "webhook_event_filters", None) is None
        assert any(
            "event subscription was not restored" in w.detail
            for w in result.get("warnings", [])
        )

    @pytest.mark.asyncio
    async def test_a_bad_subscription_does_not_abort_the_whole_import(self):
        """Rejecting the field must degrade, not fail the import: the rest of
        the project still has to land."""
        _record, result = await self._run({"nope": True})
        assert result.get("project_id") or result.get("project"), (
            f"the import did not complete; result={result}"
        )
