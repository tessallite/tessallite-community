"""Body-supplied foreign keys on the agent surfaces are project-scoped.

Defect class: "unscoped body foreign key". The route gates
(``src/auth/project_access.py``) prove the CALLER may act in the project named
in the URL PATH. Nothing proved that a UUID arriving in the REQUEST BODY names
a row belonging to that project, so a modeller bound only to project A could
submit project B's id and have it persisted.

What was open, and what each one buys an attacker:

* ``PUT``/``PATCH /projects/{p}/agent/config`` — six fields applied by a
  blanket ``setattr`` loop. Four are ``LLMProviderConfig`` ids; that row
  carries a Fernet-encrypted provider API key and a base_url, so every answer,
  judge, aggregate and glossary call for project A would run on, and bill to,
  project B's provider account. ``judge_rubric_id`` puts project B's rubric
  TEXT into project A's judge prompt (``src/judge/judge.py`` loads the rubric
  by id with no scope predicate). ``primary_model_id`` persists a
  cross-project model reference.
* ``PATCH /projects/{p}/conversations/{id}`` — ``persona_id`` was assigned
  straight through while the CREATE handler validated the identical field.
  ``_resolve_persona_name`` then loads ``ProjectPersona`` by id with no
  project predicate and returns its NAME on every turn response, so the
  asymmetry was a cross-project persona-name disclosure.

The sessions below EVALUATE the emitted predicate rather than returning a
canned row, so deleting the project predicate makes the denial tests red
instead of leaving them passing against a guard that proves nothing.
"""
from __future__ import annotations

import inspect
import typing
import uuid
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import HTTPException

from shared.db.models import (
    AgentConversation,
    AgentJudgeRubric,
    Dimension,
    LLMProviderConfig,
    Measure,
    Model,
    ProjectAgentConfig,
    ProjectAgentModel,
    ProjectPersona,
    ProjectPersonaModelScope,
    UserAccessBinding,
)
from src.api._body_scope import ensure_ref_in_project
from src.auth.middleware import CurrentUser, get_current_user
from src.main import app

from .conftest import TEST_PROJECT_ID, TEST_TENANT, TEST_USER_ID

OTHER_PROJECT_ID = uuid.uuid4()


# ---------------------------------------------------------------------------
# A session that really evaluates the emitted equality predicates
# ---------------------------------------------------------------------------


class _Result:
    def __init__(self, rows):
        self._rows = list(rows)

    def scalars(self):
        return self

    def one_or_none(self):
        return self._rows[0] if self._rows else None

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None

    def scalar_one(self):
        if len(self._rows) != 1:
            raise AssertionError(f"expected exactly one row, got {len(self._rows)}")
        return self._rows[0]

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None


class _PredicateSession:
    """Filters in-memory rows by the statement's BOUND PARAMETERS.

    This is what makes the tests below an oracle rather than theatre: an
    AsyncMock returning a canned row cannot distinguish an applied guard from
    an absent one, because it answers the same either way. Here, removing
    ``.where(project_id == ...)`` leaves only the id bound, the foreign row
    matches, and the denial tests fail — fast, with an assertion, not a hang.
    """

    def __init__(self, tables: dict[type, list]):
        self.tables = tables
        self.added = []
        self.committed = 0
        self.executed = []

    async def get(self, entity, pk):
        for row in self.tables.get(entity, []):
            if row.id == pk:
                return row
        return None

    async def execute(self, stmt):
        self.executed.append(stmt)
        params = dict(stmt.compile().params)
        entity = stmt.column_descriptions[0]["entity"]
        rows = self.tables.get(entity, [])
        for name, value in params.items():
            column = name.rsplit("_", 1)[0]
            if isinstance(value, (list, tuple, set, frozenset)):
                rows = [r for r in rows if getattr(r, column, None) in value]
            else:
                rows = [r for r in rows if getattr(r, column, None) == value]
        return _Result(rows)

    def add(self, row):
        self.added.append(row)

    async def flush(self):
        return None

    async def commit(self):
        self.committed += 1

    async def refresh(self, _row):
        return None

    async def delete(self, _row):
        return None


def _admin_binding() -> UserAccessBinding:
    """A real project binding so the fixture models a POPULATED project (not a
    binding-less one).

    The request helpers below authorize via a tenant admin (the admin-bypass
    branch that returns before any binding lookup), so the body-FK guard is
    exercised without depending on the in-memory session evaluating the
    caller-gate's ``func.lower(user_identity)`` predicate — which it cannot, so
    a bound-modeller caller would MISS here. The binding-lookup caller tier has
    its own oracle-quality suite (``test_bug_8445_8446_project_gate_consolidation``).
    This row keeps the project non-empty so the test is not accidentally a
    zero-binding scenario (which F-021-04 now denies outright, Bug-9442)."""
    return UserAccessBinding(
        id=uuid.uuid4(),
        project_id=TEST_PROJECT_ID,
        user_identity=TEST_USER_ID,
        role="admin",
    )


def _llm(project_id: uuid.UUID, config_id: uuid.UUID) -> LLMProviderConfig:
    return LLMProviderConfig(
        id=config_id, project_id=project_id, provider="anthropic",
        display_name=f"llm-{config_id.hex[:6]}", model_name="claude",
    )


def _agent_config() -> ProjectAgentConfig:
    """A persisted-looking config row.

    Every non-nullable column is populated from the request schema's own
    defaults: a bare ``ProjectAgentConfig()`` leaves them None (column defaults
    only apply on INSERT) and the response model then rejects the row, which
    would turn a working guard into a red positive test for the wrong reason.
    """
    from src.api.agent_config import AgentConfigUpsert

    defaults = AgentConfigUpsert().model_dump()
    defaults.pop("webhook_event_filters", None)
    return ProjectAgentConfig(
        id=uuid.uuid4(), project_id=TEST_PROJECT_ID, **defaults
    )


def _as_tenant_admin():
    """Authorize the request through the admin-bypass branch, before any
    binding lookup, so these tests exercise the BODY-FK guard rather than the
    caller gate.

    Every agent-config / persona caller gate (``_require_project_modeller`` ->
    ``require_project_role``, and the conversation routes'
    ``ensure_project_model_access``) resolves a ``UserAccessBinding`` with a
    SQL-side identity-normalising comparison (``func.lower(user_identity)``)
    that this in-memory ``_PredicateSession`` cannot evaluate — a bound-modeller
    caller MISSES the lookup here, and F-021-04 (Bug-9442) removed the
    zero-binding bootstrap that used to admit it anyway. A tenant admin takes
    the admin-bypass branch before any binding query, so the caller gate is
    satisfied deterministically and the caller tier itself is covered by its
    own suite (``test_bug_8445_8446_project_gate_consolidation.py``)."""
    user = CurrentUser(
        user_id=TEST_USER_ID, tenant_id=TEST_TENANT, email=TEST_USER_ID,
        role="tenant_admin",
    )
    app.dependency_overrides[get_current_user] = lambda: user
    return user


# ---------------------------------------------------------------------------
# The primitive
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_primitive_binds_the_project_predicate():
    """The emitted SQL must carry BOTH the id and the PROJECT predicate with
    the caller's values bound. Without the project hop the guard accepts any
    row in the tenant — which is the whole defect."""
    config_id = uuid.uuid4()
    db = _PredicateSession({LLMProviderConfig: [_llm(TEST_PROJECT_ID, config_id)]})

    got = await ensure_ref_in_project(
        db, LLMProviderConfig, ref_id=config_id, project_id=TEST_PROJECT_ID,
        field_name="answer_llm_config_id",
    )

    assert got.id == config_id
    sql = str(db.executed[0].compile())
    assert "llm_provider_configs.project_id =" in sql
    assert "llm_provider_configs.id =" in sql


@pytest.mark.asyncio
async def test_primitive_rejects_a_row_that_exists_in_another_project():
    config_id = uuid.uuid4()
    db = _PredicateSession({LLMProviderConfig: [_llm(OTHER_PROJECT_ID, config_id)]})

    with pytest.raises(HTTPException) as exc:
        await ensure_ref_in_project(
            db, LLMProviderConfig, ref_id=config_id,
            project_id=TEST_PROJECT_ID, field_name="answer_llm_config_id",
        )
    assert exc.value.status_code == 422
    assert "answer_llm_config_id" in exc.value.detail
    assert "in this project" in exc.value.detail


@pytest.mark.asyncio
async def test_primitive_reports_foreign_and_unknown_identically():
    """Anti-oracle: the response must not tell a caller whether an id exists
    somewhere else in the tenant."""
    config_id = uuid.uuid4()
    foreign_db = _PredicateSession(
        {LLMProviderConfig: [_llm(OTHER_PROJECT_ID, config_id)]}
    )
    unknown_db = _PredicateSession({LLMProviderConfig: []})

    errors = []
    for db in (foreign_db, unknown_db):
        with pytest.raises(HTTPException) as exc:
            await ensure_ref_in_project(
                db, LLMProviderConfig, ref_id=config_id,
                project_id=TEST_PROJECT_ID, field_name="answer_llm_config_id",
            )
        errors.append((exc.value.status_code, exc.value.detail))
    assert errors[0] == errors[1]


@pytest.mark.asyncio
async def test_primitive_absent_optional_fk_issues_no_query():
    db = _PredicateSession({})
    assert await ensure_ref_in_project(
        db, LLMProviderConfig, ref_id=None, project_id=TEST_PROJECT_ID,
        field_name="answer_llm_config_id",
    ) is None
    assert db.executed == []


@pytest.mark.asyncio
async def test_primitive_rejects_a_malformed_id_without_querying():
    """A non-UUID must never reach a UUID(as_uuid=True) bind: the driver would
    raise and the caller would see a 500 instead of this 422."""
    db = _PredicateSession({LLMProviderConfig: []})
    with pytest.raises(HTTPException) as exc:
        await ensure_ref_in_project(
            db, LLMProviderConfig, ref_id="not-a-uuid",
            project_id=TEST_PROJECT_ID, field_name="answer_llm_config_id",
        )
    assert exc.value.status_code == 422
    assert db.executed == []


@pytest.mark.asyncio
async def test_primitive_fails_closed_on_a_nullable_project_id():
    """UserAccessBinding.project_id is nullable — tenant-wide bindings carry
    NULL. Scoping on it would silently EXCLUDE those rows: the
    deny-everything failure direction, just as much a defect as the other."""
    with pytest.raises(TypeError) as exc:
        await ensure_ref_in_project(
            _PredicateSession({}), UserAccessBinding, ref_id=uuid.uuid4(),
            project_id=TEST_PROJECT_ID, field_name="x",
        )
    assert "nullable" in str(exc.value)


@pytest.mark.asyncio
async def test_primitive_fails_closed_on_an_entity_with_no_project_id():
    """ProjectAgentModel is keyed on (project_id, model_id) and DOES have a
    project_id; AgentConversation is the shape that must be checked here —
    every entity reachable from a body must either prove ownership or raise,
    never emit an unrestricted SELECT."""
    class _NoProject:
        __name__ = "_NoProject"
        id = 1

    with pytest.raises(TypeError) as exc:
        await ensure_ref_in_project(
            _PredicateSession({}), _NoProject, ref_id=uuid.uuid4(),
            project_id=TEST_PROJECT_ID, field_name="x",
        )
    assert "project_id" in str(exc.value)


def test_primitive_id_arguments_are_keyword_only():
    """A transposed (ref_id, project_id) pair would be a silently-passing
    authorization check; the signature must make it unexpressible."""
    params = inspect.signature(ensure_ref_in_project).parameters
    for name in ("ref_id", "project_id"):
        assert params[name].kind is inspect.Parameter.KEYWORD_ONLY, name
        assert params[name].default is inspect.Parameter.empty, name


# ---------------------------------------------------------------------------
# Coverage-tool blind spot: the registry must not silently miss a new field
# ---------------------------------------------------------------------------


def _mentions_uuid(annotation) -> bool:
    if annotation is uuid.UUID:
        return True
    return any(_mentions_uuid(arg) for arg in typing.get_args(annotation))


def test_every_uuid_body_field_in_agent_config_is_declared_or_excluded():
    """``_PROJECT_SCOPED_BODY_REFS`` is a coverage mechanism, so it is itself
    somewhere an enumeration blind spot can hide.

    P3-d review F2: the first cut of this test enumerated only
    ``AgentConfigUpsert`` and ``AgentConfigPatch`` — the two schemas the
    registry was built for. That is structurally blind to the shape the module
    ALREADY contains: a different request schema on a different route
    (``AllowListReplace`` on ``PUT /models``) carrying its own UUID body field.
    A new route added tomorrow with a new schema would have been invisible to
    the guard that exists to notice exactly that, which is the recurring
    "the verification tool has the blind spot, not just the code" failure.

    So this derives the schemas from the ROUTER — every route registered on
    ``agent_config.router``, every parameter of its handler annotated with a
    Pydantic model, i.e. exactly the models that carry client input into this
    module. A new route with a new body schema is therefore covered the moment
    it is registered, and nothing has to remember to add it here.

    Deriving from routes rather than from module-level class names also avoids
    a second blind spot the first attempt had: it excluded response models by
    the name suffix ``Response``, which silently mis-classified
    ``SelectableModel`` (a response-only model that does not use the suffix).
    A name convention is not a discovery mechanism."""
    import inspect as _inspect

    from pydantic import BaseModel

    from src.api import agent_config
    from src.api.agent_config import _PROJECT_SCOPED_BODY_REFS

    # UUID body fields covered by something OTHER than the shared registry.
    # Each entry is a reviewable claim naming the guard, not an omission.
    guarded_elsewhere: dict[str, str] = {
        # replace_allow_list validates every id against the path project by
        # hand before writing ProjectAgentModel rows. Logged for consolidation
        # onto the shared primitive in
        # docs/execution/issue-intake/2026-08-11-agent-service-body-fk-status-code-divergence.md
        "AllowListReplace.model_ids": "replace_allow_list explicit per-id check",
    }

    body_models: dict[str, type[BaseModel]] = {}
    for route in agent_config.router.routes:
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None:
            continue
        # get_type_hints, not signature().parameters: the module declares
        # ``from __future__ import annotations``, so raw annotations are
        # STRINGS and an isclass() test against them silently matches nothing.
        # That is the fail-OPEN direction — the guard would have reported
        # "no offenders" forever while inspecting an empty set. The vacuity
        # assertion below is what turned that into a visible failure.
        hints = typing.get_type_hints(endpoint)
        for param_name, annotation in hints.items():
            if param_name == "return":
                continue
            if (
                _inspect.isclass(annotation)
                and issubclass(annotation, BaseModel)
            ):
                body_models[annotation.__name__] = annotation

    assert body_models, (
        "no request body models were discovered on agent_config.router; the "
        "discovery mechanism has broken and this guard is now vacuous"
    )

    offenders = []
    for name, model in sorted(body_models.items()):
        for field_name, field in model.model_fields.items():
            if not _mentions_uuid(field.annotation):
                continue
            if field_name in _PROJECT_SCOPED_BODY_REFS:
                continue
            if f"{name}.{field_name}" in guarded_elsewhere:
                continue
            offenders.append(f"{name}.{field_name}")

    assert not offenders, (
        "UUID body fields on agent_config request schemas with no "
        f"project-scope guard: {offenders}. Add each to "
        "_PROJECT_SCOPED_BODY_REFS, or to this test's guarded_elsewhere map "
        "naming the guard that covers it."
    )


def test_the_registry_names_only_real_project_owned_entities():
    """The other half of the same blind spot: a declaration pointing at an
    entity that is not project-owned would produce a query that looks scoped
    and proves nothing. The primitive fails closed on such an entity, so this
    asserts every declared entry actually resolves."""
    from src.api._body_scope import _project_scope_column
    from src.api.agent_config import _PROJECT_SCOPED_BODY_REFS

    for field_name, (entity, _noun) in _PROJECT_SCOPED_BODY_REFS.items():
        # Raises TypeError if project_id is missing, nullable, or not a real
        # foreign key to projects.id.
        _project_scope_column(entity)


# ---------------------------------------------------------------------------
# PATCH /agent/config
# ---------------------------------------------------------------------------


async def _patch_config(body: dict, db: _PredicateSession) -> httpx.Response:
    _as_tenant_admin()

    async def _db(_tenant_id):
        yield db

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as ac:
            with patch("src.api.agent_config.get_tenant_db", _db):
                return await ac.patch(
                    f"/api/v1/projects/{TEST_PROJECT_ID}/agent/config", json=body
                )
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def _config_session(llm_rows=(), rubric_rows=(), model_rows=()):
    return _PredicateSession(
        {
            UserAccessBinding: [_admin_binding()],
            ProjectAgentConfig: [_agent_config()],
            ProjectAgentModel: [],
            LLMProviderConfig: list(llm_rows),
            AgentJudgeRubric: list(rubric_rows),
            Model: list(model_rows),
        }
    )


@pytest.mark.asyncio
async def test_patch_config_refuses_a_foreign_llm_config():
    """Cross-project denial, asserted on the REASON. A bare status check would
    also pass for an unrelated 422 (a pattern-constrained enum field), so it
    could not tell an applied guard from a coincidence."""
    foreign_id = uuid.uuid4()
    db = _config_session(llm_rows=[_llm(OTHER_PROJECT_ID, foreign_id)])

    resp = await _patch_config({"answer_llm_config_id": str(foreign_id)}, db)

    assert resp.status_code == 422
    assert "answer_llm_config_id" in resp.json()["detail"]
    assert "in this project" in resp.json()["detail"]
    assert db.committed == 0


@pytest.mark.asyncio
async def test_patch_config_refuses_a_foreign_judge_rubric():
    """The rubric's TEXT is rendered into this project's judge prompt, so a
    foreign rubric is a content disclosure, not only a dangling reference."""
    foreign_id = uuid.uuid4()
    db = _config_session(
        rubric_rows=[
            AgentJudgeRubric(
                id=foreign_id, project_id=OTHER_PROJECT_ID, name="B's rubric",
                sections=[],
            )
        ]
    )

    resp = await _patch_config({"judge_rubric_id": str(foreign_id)}, db)

    assert resp.status_code == 422
    assert "judge_rubric_id" in resp.json()["detail"]
    assert db.committed == 0


@pytest.mark.asyncio
async def test_patch_config_refuses_a_foreign_primary_model():
    foreign_id = uuid.uuid4()
    db = _config_session(
        model_rows=[
            Model(
                id=foreign_id, project_id=OTHER_PROJECT_ID, slug="b",
                display_name="B",
            )
        ]
    )

    resp = await _patch_config({"primary_model_id": str(foreign_id)}, db)

    assert resp.status_code == 422
    assert "primary_model_id" in resp.json()["detail"]
    assert db.committed == 0


@pytest.mark.asyncio
async def test_patch_config_accepts_a_config_in_this_project():
    """The guard must not be an over-broad denial. Without a positive case, a
    guard that rejected everything — or 500'd on the happy path — would look
    identical to a correct one."""
    own_id = uuid.uuid4()
    db = _config_session(llm_rows=[_llm(TEST_PROJECT_ID, own_id)])

    resp = await _patch_config({"answer_llm_config_id": str(own_id)}, db)

    assert resp.status_code == 200, resp.text
    assert resp.json()["answer_llm_config_id"] == str(own_id)
    assert db.committed == 1


@pytest.mark.asyncio
async def test_patch_config_explicit_null_still_unbinds():
    """PRESENT, not truthy. Clearing a binding is a supported operation; a
    guard keyed on truthiness would skip it — and would equally skip a real id
    of UUID(int=0)."""
    db = _config_session()

    resp = await _patch_config({"answer_llm_config_id": None}, db)

    assert resp.status_code == 200, resp.text
    assert resp.json()["answer_llm_config_id"] is None
    assert db.committed == 1


@pytest.mark.asyncio
async def test_patch_config_omitted_fields_are_not_validated():
    """A field absent from a PATCH means "leave it alone", so no lookup may be
    issued for it — otherwise every unrelated edit on a config whose binding
    predates this guard would start failing."""
    db = _config_session()

    resp = await _patch_config({"display_name": "Assistant"}, db)

    assert resp.status_code == 200, resp.text
    assert [
        s for s in db.executed
        if s.column_descriptions[0]["entity"] is LLMProviderConfig
    ] == []


# ---------------------------------------------------------------------------
# PUT /agent/config
# ---------------------------------------------------------------------------


async def _put_config(body: dict, db: _PredicateSession) -> httpx.Response:
    _as_tenant_admin()

    async def _db(_tenant_id):
        yield db

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as ac:
            with patch("src.api.agent_config.get_tenant_db", _db):
                return await ac.put(
                    f"/api/v1/projects/{TEST_PROJECT_ID}/agent/config", json=body
                )
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def _put_session(llm_rows=(), existing_config=True):
    from shared.db.models import Project

    return _PredicateSession(
        {
            UserAccessBinding: [_admin_binding()],
            Project: [
                Project(id=TEST_PROJECT_ID, slug="p", display_name="P")
            ],
            ProjectAgentConfig: [_agent_config()] if existing_config else [],
            ProjectAgentModel: [],
            LLMProviderConfig: list(llm_rows),
            AgentJudgeRubric: [],
            Model: [],
        }
    )


@pytest.mark.asyncio
async def test_put_config_refuses_a_foreign_llm_config():
    """The PUT path is the same defect as the PATCH path and needs its own
    test: it applies a DIFFERENT dict (a full ``model_dump()``, not
    ``exclude_unset``) through a different branch that also creates the record
    and a default rubric."""
    foreign_id = uuid.uuid4()
    db = _put_session(llm_rows=[_llm(OTHER_PROJECT_ID, foreign_id)])

    resp = await _put_config(
        {"enabled": False, "answer_llm_config_id": str(foreign_id)}, db
    )

    assert resp.status_code == 422
    assert "answer_llm_config_id" in resp.json()["detail"]
    assert db.committed == 0


@pytest.mark.asyncio
async def test_put_config_guard_runs_before_the_record_and_rubric_are_created():
    """Ordering, not merely presence: on first creation this handler adds a
    ProjectAgentConfig AND a default AgentJudgeRubric before committing. A
    request about to be refused must add neither."""
    foreign_id = uuid.uuid4()
    db = _put_session(
        llm_rows=[_llm(OTHER_PROJECT_ID, foreign_id)], existing_config=False
    )

    resp = await _put_config(
        {"enabled": False, "answer_llm_config_id": str(foreign_id)}, db
    )

    assert resp.status_code == 422
    assert db.added == []
    assert db.committed == 0


@pytest.mark.asyncio
async def test_put_config_accepts_a_config_in_this_project():
    own_id = uuid.uuid4()
    db = _put_session(llm_rows=[_llm(TEST_PROJECT_ID, own_id)])

    resp = await _put_config(
        {"enabled": False, "answer_llm_config_id": str(own_id)}, db
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["answer_llm_config_id"] == str(own_id)
    assert db.committed == 1


# ---------------------------------------------------------------------------
# POST / PATCH conversations — the asymmetry IS the bug, so symmetry is the
# assertion
# ---------------------------------------------------------------------------


def _conversation_session(persona_rows=(), model_rows=()):
    conv = AgentConversation(
        id=uuid.uuid4(),
        project_id=TEST_PROJECT_ID,
        caller_kind="tenant_user",
        caller_ref=TEST_USER_ID,
        persona_id=None,
        pinned_model_id=None,
    )
    cfg = _agent_config()
    cfg.enabled = True
    db = _PredicateSession(
        {
            UserAccessBinding: [_admin_binding()],
            ProjectAgentConfig: [cfg],
            AgentConversation: [conv],
            ProjectPersona: list(persona_rows),
            Model: list(model_rows),
        }
    )
    return db, conv


async def _conversation_call(method: str, path: str, body: dict, db):
    _as_tenant_admin()

    async def _fake_db(_tenant_id):
        yield db

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as ac:
            with (
                patch("src.api.conversations.get_tenant_db", _fake_db),
                patch(
                    "src.api.conversations.dispatch_event", new_callable=AsyncMock
                ),
            ):
                return await ac.request(method, path, json=body)
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_create_and_patch_reject_the_same_foreign_persona_identically():
    """THE regression guard for this site. The CREATE handler validated
    persona_id and the PATCH handler did not, so one id was refused by POST and
    accepted by PATCH. Asserting the two responses MATCH — rather than
    asserting each separately — is what stops the pair drifting apart again."""
    foreign_id = uuid.uuid4()
    persona = ProjectPersona(
        id=foreign_id, project_id=OTHER_PROJECT_ID, name="B's analyst",
    )

    create_db, _ = _conversation_session(persona_rows=[persona])
    create_resp = await _conversation_call(
        "POST", f"/api/v1/projects/{TEST_PROJECT_ID}/agent/conversations",
        {"persona_id": str(foreign_id)}, create_db,
    )

    patch_db, conv = _conversation_session(persona_rows=[persona])
    patch_resp = await _conversation_call(
        "PATCH",
        f"/api/v1/projects/{TEST_PROJECT_ID}/agent/conversations/{conv.id}",
        {"persona_id": str(foreign_id)}, patch_db,
    )

    assert create_resp.status_code == 422, create_resp.text
    assert patch_resp.status_code == create_resp.status_code
    assert patch_resp.json()["detail"] == create_resp.json()["detail"]
    # And the foreign persona's NAME never appears in either response, which is
    # the disclosure the unguarded PATCH enabled via _resolve_persona_name.
    assert "B's analyst" not in patch_resp.text
    assert create_db.committed == 0
    assert patch_db.committed == 0


@pytest.mark.asyncio
async def test_patch_accepts_a_persona_in_this_project():
    """Positive case: the guard narrows nothing legitimate."""
    own_id = uuid.uuid4()
    db, conv = _conversation_session(
        persona_rows=[
            ProjectPersona(id=own_id, project_id=TEST_PROJECT_ID, name="Analyst")
        ]
    )

    resp = await _conversation_call(
        "PATCH",
        f"/api/v1/projects/{TEST_PROJECT_ID}/agent/conversations/{conv.id}",
        {"persona_id": str(own_id)}, db,
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["persona_id"] == str(own_id)
    assert db.committed == 1


@pytest.mark.asyncio
async def test_patch_explicit_null_persona_still_clears_it():
    """PRESENT, not truthy: clearing the persona back to the project default is
    a supported operation and must stay legal."""
    db, conv = _conversation_session()
    conv.persona_id = uuid.uuid4()

    resp = await _conversation_call(
        "PATCH",
        f"/api/v1/projects/{TEST_PROJECT_ID}/agent/conversations/{conv.id}",
        {"persona_id": None}, db,
    )

    assert resp.status_code == 200, resp.text
    assert conv.persona_id is None
    assert db.committed == 1


@pytest.mark.asyncio
async def test_patch_refuses_a_foreign_pinned_model():
    """The sibling field on the same handler, which WAS already guarded. Kept
    so the consolidation onto the shared primitive cannot quietly drop it."""
    foreign_id = uuid.uuid4()
    db, conv = _conversation_session(
        model_rows=[
            Model(
                id=foreign_id, project_id=OTHER_PROJECT_ID, slug="b",
                display_name="B",
            )
        ]
    )

    resp = await _conversation_call(
        "PATCH",
        f"/api/v1/projects/{TEST_PROJECT_ID}/agent/conversations/{conv.id}",
        {"pinned_model_id": str(foreign_id)}, db,
    )

    assert resp.status_code == 422
    assert "pinned_model_id" in resp.json()["detail"]
    assert db.committed == 0


@pytest.mark.asyncio
async def test_patch_writes_nothing_when_a_later_field_is_refused():
    """Ordering. The handler assigns title / pinned / pinned_model_id before it
    reaches persona_id, and the guard's SELECT runs on an autoflush session —
    so validating in-line would flush a refused request's edits to the database
    and leave correctness resting on session teardown. Every reference is
    proven before the first assignment."""
    foreign_persona = uuid.uuid4()
    db, conv = _conversation_session(
        persona_rows=[
            ProjectPersona(
                id=foreign_persona, project_id=OTHER_PROJECT_ID, name="B",
            )
        ]
    )

    resp = await _conversation_call(
        "PATCH",
        f"/api/v1/projects/{TEST_PROJECT_ID}/agent/conversations/{conv.id}",
        {"title": "renamed", "persona_id": str(foreign_persona)}, db,
    )

    assert resp.status_code == 422
    assert conv.title is None, (
        "the title was applied to the ORM row before the persona reference was "
        "refused; on an autoflush session the guard's own SELECT would have "
        "written it"
    )
    assert db.committed == 0


# ---------------------------------------------------------------------------
# Bug-8949 — persona model/field scope bodies are ownership-bearing writes
# ---------------------------------------------------------------------------


class _PersonaSession(_PredicateSession):
    """Persist just enough ORM identity/relationships for persona route tests."""

    def __init__(self, *, model_rows=(), persona_rows=(), measure_rows=(),
                 dimension_rows=()):
        super().__init__({
            UserAccessBinding: [_admin_binding()],
            Model: list(model_rows),
            Measure: list(measure_rows),
            Dimension: list(dimension_rows),
            ProjectPersona: list(persona_rows),
            ProjectPersonaModelScope: [],
        })
        self.deleted = []

    def add(self, row):
        super().add(row)
        if getattr(row, "id", None) is None:
            row.id = uuid.uuid4()
        self.tables.setdefault(type(row), []).append(row)
        if isinstance(row, ProjectPersonaModelScope):
            persona = next(
                p for p in self.tables[ProjectPersona]
                if p.id == row.project_persona_id
            )
            persona.model_scopes.append(row)

    async def delete(self, row):
        self.deleted.append(row)
        if row in self.tables.get(type(row), []):
            self.tables[type(row)].remove(row)


async def _persona_call(method: str, suffix: str, body: dict, db) -> httpx.Response:
    _as_tenant_admin()

    async def _fake_db(_tenant_id):
        yield db

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as ac:
            with patch("src.api.personas.get_tenant_db", _fake_db):
                return await ac.request(
                    method,
                    f"/api/v1/projects/{TEST_PROJECT_ID}/agent/personas{suffix}",
                    json=body,
                )
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["POST", "PATCH"])
async def test_bug_8949_persona_write_rejects_a_foreign_model(method: str):
    """Both writers must reject before adding/deleting any persona state."""
    foreign_model_id = uuid.uuid4()
    foreign_model = Model(
        id=foreign_model_id,
        project_id=OTHER_PROJECT_ID,
        slug="foreign",
        display_name="Foreign",
    )
    persona = ProjectPersona(
        id=uuid.uuid4(),
        project_id=TEST_PROJECT_ID,
        name="Analyst",
        slug="analyst",
        model_scopes=[],
    )
    db = _PersonaSession(
        model_rows=[foreign_model],
        persona_rows=[persona] if method == "PATCH" else [],
    )
    suffix = f"/{persona.id}" if method == "PATCH" else ""

    resp = await _persona_call(
        method,
        suffix,
        {
            "name": "Analyst" if method == "POST" else "Renamed",
            "model_scopes": [{"model_id": str(foreign_model_id)}],
        },
        db,
    )

    assert resp.status_code == 422, resp.text
    assert "model_scopes[0].model_id" in resp.json()["detail"]
    assert "in this project" in resp.json()["detail"]
    assert db.added == []
    assert db.deleted == []
    assert db.committed == 0
    assert persona.name == "Analyst"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field_name", "entity"),
    [
        ("included_measure_ids", Measure),
        ("included_dimension_ids", Dimension),
    ],
)
async def test_bug_8949_persona_write_rejects_field_ids_outside_scope_model(
    field_name: str, entity: type,
):
    scope_model_id = uuid.uuid4()
    foreign_field_id = uuid.uuid4()
    field_row = entity(
        id=foreign_field_id,
        model_id=uuid.uuid4(),
        name="Foreign field",
    )
    db = _PersonaSession(
        model_rows=[Model(
            id=scope_model_id,
            project_id=TEST_PROJECT_ID,
            slug="sales",
            display_name="Sales",
        )],
        measure_rows=[field_row] if entity is Measure else [],
        dimension_rows=[field_row] if entity is Dimension else [],
    )

    resp = await _persona_call(
        "POST",
        "",
        {
            "name": "Analyst",
            "model_scopes": [{
                "model_id": str(scope_model_id),
                field_name: [str(foreign_field_id)],
            }],
        },
        db,
    )

    assert resp.status_code == 422, resp.text
    assert f"model_scopes[0].{field_name}" in resp.json()["detail"]
    assert "in this model" in resp.json()["detail"]
    assert db.added == []
    assert db.committed == 0


@pytest.mark.asyncio
async def test_bug_8949_persona_write_accepts_model_owned_measure_and_dimension():
    model_id = uuid.uuid4()
    measure_id = uuid.uuid4()
    dimension_id = uuid.uuid4()
    db = _PersonaSession(
        model_rows=[Model(
            id=model_id,
            project_id=TEST_PROJECT_ID,
            slug="sales",
            display_name="Sales",
        )],
        measure_rows=[Measure(
            id=measure_id,
            model_id=model_id,
            name="Revenue",
        )],
        dimension_rows=[Dimension(
            id=dimension_id,
            model_id=model_id,
            name="Region",
        )],
    )

    resp = await _persona_call(
        "POST",
        "",
        {
            "name": "Analyst",
            "model_scopes": [{
                "model_id": str(model_id),
                "included_measure_ids": [str(measure_id)],
                "included_dimension_ids": [str(dimension_id)],
            }],
        },
        db,
    )

    assert resp.status_code == 201, resp.text
    assert resp.json()["model_scopes"] == [{
        "model_id": str(model_id),
        "included_measure_ids": [str(measure_id)],
        "included_dimension_ids": [str(dimension_id)],
    }]
    assert db.committed == 1
