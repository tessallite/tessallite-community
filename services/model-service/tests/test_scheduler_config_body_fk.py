"""Body-supplied foreign keys on PUT /scheduler-config are project-scoped.

Defect class: "unscoped body foreign key". ``require_tenant_admin`` proves the
CALLER may act in the tenant and ``ensure_model_in_project`` proves the PATH
model belongs to the PATH project. Neither proves that
``llm_config_id`` / ``glossary_llm_config_id`` — applied by a blanket
``setattr`` loop straight from the request body — name an ``LLMProviderConfig``
row belonging to that project. A foreign id bound this model's AI scheduler to
another project's provider row, which carries a Fernet-encrypted API key and a
base_url, so every scheduled aggregate/glossary creation run would have
prompted and billed through another project's provider account.

These tests are the ROUTE layer: is the guard wired in, does it sit ahead of
the handler's side effects, and does a PRESENT-but-null value still unbind.
The accept/reject truth of the predicate itself is proved against real
Postgres in ``tests/integration/test_scope_body_fk_db.py``; a mocked session
cannot evaluate a WHERE clause, so the session here evaluates the emitted
predicate explicitly rather than pretending to.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from .result_fakes import FakeScalarResult

from shared.db.models import LLMProviderConfig, Model, ModelAISchedulerConfig
from src.auth.middleware import CurrentUser, get_current_user
from src.main import app

from .conftest import TEST_MODEL_ID, TEST_PROJECT_ID, TEST_TENANT

pytestmark = pytest.mark.unit

PREFIX = (
    f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/scheduler-config"
)

# An LLM provider config that belongs to ANOTHER project in the same tenant —
# a real row, not a dangling id, because "a row that exists somewhere else" is
# precisely the case a get-then-forget-to-compare guard waves through.
OTHER_PROJECT_ID = uuid.uuid4()
OWN_LLM_ID = uuid.uuid4()
FOREIGN_LLM_ID = uuid.uuid4()


class _Result:
    def __init__(self, rows):
        self._rows = list(rows)

    def scalars(self):
        return FakeScalarResult(self._rows)

    def one_or_none(self):
        return self._rows[0] if self._rows else None

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return list(self._rows)


class _PredicateSession:
    """A session that really evaluates the emitted equality predicates.

    ``execute`` compiles the statement, reads the bound parameters, and keeps
    only the in-memory rows whose attributes match ALL of them. That makes the
    session a genuine oracle for this lane: delete the project predicate from
    the guard and only the id remains bound, so the foreign row matches and the
    denial test below goes red. A plain AsyncMock returning a canned row could
    not tell an applied guard from an absent one.
    """

    def __init__(self, tables: dict[type, list]):
        self.tables = tables
        self.added = []
        self.flushed = 0
        self.committed = 0
        self.executed = []

    async def get(self, entity, pk):
        for row in self.tables.get(entity, []):
            if row.id == pk:
                return row
        return None

    async def execute(self, stmt):
        self.executed.append(stmt)
        compiled = stmt.compile()
        params = dict(compiled.params)
        entity = stmt.column_descriptions[0]["entity"]
        rows = self.tables.get(entity, [])
        for name, value in params.items():
            # SQLAlchemy names binds "<column>_1"; strip the disambiguator.
            column = name.rsplit("_", 1)[0]
            rows = [r for r in rows if getattr(r, column, None) == value]
        return _Result(rows)

    def add(self, row):
        self.added.append(row)

    async def flush(self):
        self.flushed += 1

    async def commit(self):
        self.committed += 1

    async def refresh(self, _row):
        return None


def _existing_config() -> ModelAISchedulerConfig:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return ModelAISchedulerConfig(
        id=uuid.uuid4(),
        model_id=TEST_MODEL_ID,
        ai_enabled=False,
        cron_expression="0 5 * * *",
        lookback_hours=168,
        max_creates_per_run=3,
        min_confidence=0.5,
        dry_run=False,
        enable_ai_aggregation=True,
        llm_config_id=None,
        glossary_llm_config_id=None,
        created_at=now,
        updated_at=now,
    )


def _session() -> _PredicateSession:
    return _PredicateSession(
        {
            Model: [
                Model(
                    id=TEST_MODEL_ID, project_id=TEST_PROJECT_ID,
                    slug="m", display_name="M",
                )
            ],
            LLMProviderConfig: [
                LLMProviderConfig(
                    id=OWN_LLM_ID, project_id=TEST_PROJECT_ID,
                    provider="anthropic", display_name="own",
                    model_name="claude",
                ),
                LLMProviderConfig(
                    id=FOREIGN_LLM_ID, project_id=OTHER_PROJECT_ID,
                    provider="anthropic", display_name="foreign",
                    model_name="claude",
                ),
            ],
            ModelAISchedulerConfig: [_existing_config()],
        }
    )


async def _put(body: dict, session: _PredicateSession, lock: AsyncMock):
    user = CurrentUser(
        user_id="admin@acme.test", tenant_id=TEST_TENANT,
        email="admin@acme.test", role="tenant_admin",
    )
    app.dependency_overrides[get_current_user] = lambda: user

    async def _db(_tenant_id):
        yield session

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as ac:
            with (
                patch("src.api.scheduler_config.get_tenant_db", _db),
                patch(
                    "src.api.scheduler_config.acquire_model_definition_lock", lock
                ),
                patch(
                    "src.api.scheduler_config._notify_optimizer_reload",
                    new_callable=AsyncMock,
                ),
            ):
                return await ac.put(PREFIX, json=body)
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_foreign_llm_config_id_is_refused_for_the_stated_reason():
    """Cross-project denial. The assertion is on the REJECTION REASON, not the
    status alone: this route can answer 422 for an unrelated reason (an invalid
    cron expression is a schema-level 422), so a bare status check would pass
    against the pre-fix handler for the wrong cause."""
    session, lock = _session(), AsyncMock()

    resp = await _put({"llm_config_id": str(FOREIGN_LLM_ID)}, session, lock)

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error_code"] == "LLM_CONFIG_NOT_IN_PROJECT"
    assert detail["field"] == "llm_config_id"
    assert detail["ids"] == [str(FOREIGN_LLM_ID)]
    assert session.committed == 0


@pytest.mark.asyncio
async def test_foreign_glossary_llm_config_id_is_refused_too():
    """The sibling field on the same body. Guarding only the first one would
    leave the same exposure one line away."""
    session, lock = _session(), AsyncMock()

    resp = await _put(
        {"glossary_llm_config_id": str(FOREIGN_LLM_ID)}, session, lock
    )

    assert resp.status_code == 422
    assert resp.json()["detail"]["field"] == "glossary_llm_config_id"
    assert session.committed == 0


@pytest.mark.asyncio
async def test_the_guard_runs_before_the_lock_and_before_any_insert():
    """Ordering, not merely presence. ``acquire_model_definition_lock`` is a
    cross-family advisory lock every model-scoped writer contends on, and the
    handler INSERTs a scheduler-config row for a model that has never had one.
    A request that is about to be refused must do neither."""
    session = _PredicateSession(
        {
            Model: [
                Model(
                    id=TEST_MODEL_ID, project_id=TEST_PROJECT_ID,
                    slug="m", display_name="M",
                )
            ],
            LLMProviderConfig: [
                LLMProviderConfig(
                    id=FOREIGN_LLM_ID, project_id=OTHER_PROJECT_ID,
                    provider="anthropic", display_name="foreign",
                    model_name="claude",
                ),
            ],
            # No existing config row: the handler would create one.
            ModelAISchedulerConfig: [],
        }
    )
    lock = AsyncMock()

    resp = await _put({"llm_config_id": str(FOREIGN_LLM_ID)}, session, lock)

    assert resp.status_code == 422
    lock.assert_not_awaited()
    assert session.added == []
    assert session.flushed == 0
    assert session.committed == 0


@pytest.mark.asyncio
async def test_an_llm_config_in_this_project_is_accepted():
    """The guard must not be an over-broad denial. Without this, a guard that
    rejected every request — or raised a 500 on the happy path — would look
    identical to a correct one in the denial tests above."""
    session, lock = _session(), AsyncMock()

    resp = await _put({"llm_config_id": str(OWN_LLM_ID)}, session, lock)

    assert resp.status_code == 200, resp.text
    assert resp.json()["llm_config_id"] == str(OWN_LLM_ID)
    lock.assert_awaited_once()
    assert session.committed == 1


@pytest.mark.asyncio
async def test_an_explicit_null_still_unbinds_the_override():
    """PRESENT, not truthy. Clearing the per-model override back to "inherit
    the project default" is a supported operation and must stay legal; a guard
    keyed on truthiness would have been skipped here — and would equally have
    skipped a real id of UUID(int=0)."""
    session, lock = _session(), AsyncMock()

    resp = await _put({"llm_config_id": None}, session, lock)

    assert resp.status_code == 200, resp.text
    assert resp.json()["llm_config_id"] is None
    assert session.committed == 1


def test_every_uuid_field_on_the_update_schema_is_guarded_or_excluded():
    """P3-d review F3. The handler names the guarded fields in a hard-coded
    tuple, which is a coverage mechanism and therefore somewhere an enumeration
    blind spot can hide: a UUID field added to ``ModelAISchedulerConfigUpdate``
    tomorrow would be applied by the same blanket ``setattr`` loop and simply
    go unguarded, silently — which is exactly how these two fields went
    unguarded for their whole life.

    This enumerates the SCHEMA rather than the tuple, so the tuple cannot
    certify itself. A new UUID field must either join the guarded tuple or be
    named below as deliberately not a foreign key."""
    import ast
    import typing
    from pathlib import Path

    from shared.schemas.pydantic_models import ModelAISchedulerConfigUpdate

    # UUID-typed fields that are NOT references to another owned row.
    # Empty today; an entry here is a reviewable claim, not an omission.
    not_a_foreign_key: set[str] = set()

    # Read the guarded names out of the handler's own source, so this test
    # tracks the shipped tuple rather than a copy that could drift from it.
    source = Path("src/api/scheduler_config.py").read_text(encoding="utf-8")
    guarded: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.For)
            and isinstance(node.target, ast.Name)
            and node.target.id == "_fk_field"
            and isinstance(node.iter, ast.Tuple)
        ):
            guarded = {
                el.value for el in node.iter.elts
                if isinstance(el, ast.Constant) and isinstance(el.value, str)
            }
    assert guarded, (
        "could not locate the body-FK guard loop in scheduler_config.py; if it "
        "was restructured, update this test to read the new shape rather than "
        "deleting it"
    )

    def _mentions_uuid(annotation) -> bool:
        if annotation is uuid.UUID:
            return True
        return any(_mentions_uuid(a) for a in typing.get_args(annotation))

    for name, field in ModelAISchedulerConfigUpdate.model_fields.items():
        if not _mentions_uuid(field.annotation):
            continue
        assert name in guarded or name in not_a_foreign_key, (
            f"ModelAISchedulerConfigUpdate.{name} is a UUID body field that "
            "reaches a persisted row through the handler's setattr loop with "
            "no project-scope guard. Add it to the guarded tuple in "
            "upsert_scheduler_config, or to this test's not_a_foreign_key set "
            "with a reason."
        )


@pytest.mark.asyncio
async def test_an_omitted_fk_field_is_not_validated_at_all():
    """PATCH-style semantics: a field absent from the body means "leave it
    alone", so the guard must issue no lookup for it. Validating an omitted
    field against the row's CURRENT value would break every unrelated edit on a
    config whose override was set before this guard existed."""
    session, lock = _session(), AsyncMock()

    resp = await _put({"ai_enabled": True}, session, lock)

    assert resp.status_code == 200, resp.text
    llm_queries = [
        s for s in session.executed
        if s.column_descriptions[0]["entity"] is LLMProviderConfig
    ]
    assert llm_queries == []
