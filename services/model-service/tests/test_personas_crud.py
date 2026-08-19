"""CRUD coverage for personas — Phase 8.B.1.

Locks the contract for:
  * POST   /projects/{pid}/models/{mid}/personas
  * GET    /projects/{pid}/models/{mid}/personas
  * GET    /projects/{pid}/models/{mid}/personas/{id}
  * PATCH  /projects/{pid}/models/{mid}/personas/{id}
  * DELETE /projects/{pid}/models/{mid}/personas/{id}
  * GET    /projects/{pid}/models/{mid}/personas/{id}/resolution

Empty-list semantics covered separately in
``test_personas_empty_list_semantics.py``.
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from .result_fakes import FakeScalarResult

from .conftest import (
    NOW,
    TEST_MODEL_ID,
    TEST_PROJECT_ID,
    async_gen_from,
    client,
    make_mock_db,
    make_model,
)

pytestmark = pytest.mark.unit

PREFIX = f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/personas"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _ScalarResult:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return FakeScalarResult(self._items)

    def all(self):
        return list(self._items)

    def scalar_one_or_none(self):
        return self._items[0] if self._items else None

    def __iter__(self):
        return iter(self._items)


def _persona(
    *,
    persona_id: uuid.UUID | None = None,
    name: str = "Sales",
    slug: str = "sales",
    description: str | None = None,
    included_measure_ids: list[str] | None = None,
    included_dimension_ids: list[str] | None = None,
    included_hierarchy_ids: list[str] | None = None,
    audience_roles: list[str] | None = None,
    default_filters: dict | None = None,
    includes_hidden_columns: bool = False,
) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=persona_id or uuid.uuid4(),
        model_id=TEST_MODEL_ID,
        name=name,
        slug=slug,
        description=description,
        included_measure_ids=included_measure_ids or [],
        included_dimension_ids=included_dimension_ids or [],
        included_hierarchy_ids=included_hierarchy_ids or [],
        audience_roles=(
            audience_roles
            if audience_roles is not None
            else (
                ["sales_analyst"]
                if (
                    included_measure_ids
                    or included_dimension_ids
                    or included_hierarchy_ids
                    or default_filters
                )
                else []
            )
        ),
        default_filters=default_filters or {},
        bypass_row_security=False,
        includes_hidden_columns=includes_hidden_columns,
        created_at=NOW,
        updated_at=NOW,
    )


def _scripted_get(*, model, persona=None, measure=None):
    async def _get(cls, key):
        name = cls.__name__
        if name == "Model":
            return model
        if name == "Persona":
            return persona
        if name == "Measure":
            return measure
        return None

    return AsyncMock(side_effect=_get)


def _execute_script(*results):
    queue = list(results)

    async def _side(*_a, **_kw):
        if queue:
            return queue.pop(0)
        empty = MagicMock()
        empty.scalar_one_or_none.return_value = None
        empty.scalars.return_value.all.return_value = []
        return empty

    return AsyncMock(side_effect=_side)


# ---------------------------------------------------------------------------
# POST
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_persona_round_trip(client):
    model = make_model()
    measure_id = uuid.uuid4()
    db = make_mock_db()
    db.get = _scripted_get(model=model)
    # F-008-21: create now validates include ids against the model — the
    # measure-existence query must report the requested measure as present.
    db.execute = _execute_script(_ScalarResult([measure_id]))

    captured: dict = {}

    def _add(obj):
        captured["obj"] = obj
        if not hasattr(obj, "id") or obj.id is None:
            obj.id = uuid.uuid4()
        obj.created_at = NOW
        obj.updated_at = NOW

    db.add = _add

    async def _refresh(obj):
        return None

    db.refresh = _refresh

    with (
        patch("src.api.personas.get_tenant_db", async_gen_from(db)),
        patch("src.api.personas.audit_required", new=AsyncMock()),
    ):
        resp = await client.post(
            PREFIX,
            json={
                "name": "Sales",
                "slug": "sales",
                "description": "Sales analyst scope",
                "included_measure_ids": [str(measure_id)],
                "audience_roles": ["sales_analyst"],
            },
        )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "Sales"
    assert body["slug"] == "sales"
    assert body["included_measure_ids"] == [str(measure_id)]
    assert body["included_dimension_ids"] == []
    assert body["audience_roles"] == ["sales_analyst"]
    assert body["default_filters"] == {}


@pytest.mark.asyncio
async def test_f008_03_create_narrowing_empty_audience_is_422(client):
    """F-008-03: a measure allow-list with empty audience_roles must not
    save — that would assign a locking persona to everyone.
    """
    model = make_model()
    measure_id = uuid.uuid4()
    db = make_mock_db()
    db.get = _scripted_get(model=model)
    db.execute = _execute_script(_ScalarResult([measure_id]))

    with (
        patch("src.api.personas.get_tenant_db", async_gen_from(db)),
        patch("src.api.personas.audit_required", new=AsyncMock()),
    ):
        resp = await client.post(
            PREFIX,
            json={
                "name": "Locked",
                "slug": "locked",
                "included_measure_ids": [str(measure_id)],
                "audience_roles": [],
            },
        )

    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["error_code"] == "PERSONA_EMPTY_AUDIENCE_NARROWING"


@pytest.mark.asyncio
async def test_create_persona_name_conflict_returns_409(client):
    from sqlalchemy.exc import IntegrityError

    model = make_model()
    db = make_mock_db()
    db.get = _scripted_get(model=model)
    db.commit = AsyncMock(side_effect=IntegrityError("dup", None, None))
    db.rollback = AsyncMock()

    with patch("src.api.personas.get_tenant_db", async_gen_from(db)):
        resp = await client.post(PREFIX, json={"name": "Sales", "slug": "sales"})

    assert resp.status_code == 409
    body = resp.json()
    assert body["detail"]["error_code"] == "PERSONA_NAME_CONFLICT"


# ---------------------------------------------------------------------------
# GET (list + detail)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_personas(client):
    model = make_model()
    p1 = _persona(name="A", slug="a")
    p2 = _persona(name="B", slug="b")
    db = make_mock_db()
    db.get = _scripted_get(model=model)
    db.execute = _execute_script(_ScalarResult([p1, p2]))

    with patch("src.api.personas.get_tenant_db", async_gen_from(db)):
        resp = await client.get(PREFIX)

    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 2
    assert {i["name"] for i in items} == {"A", "B"}


@pytest.mark.asyncio
async def test_list_personas_for_audience_filters(client):
    model = make_model()
    sales = _persona(name="Sales", slug="sales", audience_roles=["sales_analyst"])
    finance = _persona(name="Finance", slug="finance", audience_roles=["finance_analyst"])
    unrestricted = _persona(name="All", slug="all")
    db = make_mock_db()
    db.get = _scripted_get(model=model)
    db.execute = _execute_script(_ScalarResult([sales, finance, unrestricted]))

    from src.auth.middleware import CurrentUser, get_current_user
    from src.main import app

    sales_user = CurrentUser(
        user_id="alice",
        tenant_id="test-tenant",
        email="alice@example.com",
        role="sales_analyst",
    )
    app.dependency_overrides[get_current_user] = lambda: sales_user

    with patch("src.api.personas.get_tenant_db", async_gen_from(db)):
        resp = await client.get(f"{PREFIX}?for_audience=true")

    app.dependency_overrides.pop(get_current_user, None)

    assert resp.status_code == 200
    names = {i["name"] for i in resp.json()}
    assert names == {"Sales", "All"}


@pytest.mark.asyncio
async def test_get_persona_detail(client):
    model = make_model()
    p = _persona(name="Sales")
    db = make_mock_db()
    db.get = _scripted_get(model=model, persona=p)

    with patch("src.api.personas.get_tenant_db", async_gen_from(db)):
        resp = await client.get(f"{PREFIX}/{p.id}")

    assert resp.status_code == 200
    assert resp.json()["name"] == "Sales"


@pytest.mark.asyncio
async def test_get_persona_404_when_other_model(client):
    other_model_id = uuid.uuid4()
    model = make_model()
    p = _persona()
    p.model_id = other_model_id
    db = make_mock_db()
    db.get = _scripted_get(model=model, persona=p)

    with patch("src.api.personas.get_tenant_db", async_gen_from(db)):
        resp = await client.get(f"{PREFIX}/{p.id}")

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# PATCH
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_patch_persona_replaces_include_lists(client):
    model = make_model()
    p = _persona(included_measure_ids=[str(uuid.uuid4())])
    new_ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    db = make_mock_db()
    db.get = _scripted_get(model=model, persona=p)
    # F-008-16 / F-008-21: the patch now validates the EFFECTIVE scope. Script
    # the measure-existence query (both new ids present) and the dimension-name
    # query (``year`` is a real dimension, so the default filter is valid);
    # the third query is the restricted-columns lookup in the response build.
    db.execute = _execute_script(
        _ScalarResult([uuid.UUID(i) for i in new_ids]),  # include-id existence
        _ScalarResult(["year"]),                          # dimension names
        _ScalarResult([]),                                # restricted columns
    )

    async def _refresh(obj):
        return None

    db.refresh = _refresh

    with (
        patch("src.api.personas.get_tenant_db", async_gen_from(db)),
        patch("src.api.personas.audit_required", new=AsyncMock()),
    ):
        resp = await client.patch(
            f"{PREFIX}/{p.id}",
            json={"included_measure_ids": new_ids, "default_filters": {"year": 2026}},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["included_measure_ids"] == new_ids
    assert body["default_filters"] == {"year": 2026}


# ---------------------------------------------------------------------------
# DELETE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_persona(client):
    model = make_model()
    p = _persona()
    db = make_mock_db()
    db.get = _scripted_get(model=model, persona=p)

    with (
        patch("src.api.personas.get_tenant_db", async_gen_from(db)),
        patch("src.api.personas.audit_required", new=AsyncMock()),
    ):
        resp = await client.delete(f"{PREFIX}/{p.id}")

    assert resp.status_code == 204
    db.delete.assert_awaited_once_with(p)


# ---------------------------------------------------------------------------
# Resolution helper
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolution_allowed_when_measure_in_list(client):
    model = make_model()
    measure_id = uuid.uuid4()
    p = _persona(included_measure_ids=[str(measure_id)])
    db = make_mock_db()
    db.get = _scripted_get(model=model, persona=p)

    with patch("src.api.personas.get_tenant_db", async_gen_from(db)):
        resp = await client.get(
            f"{PREFIX}/{p.id}/resolution?measure_id={measure_id}"
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["measure_allowed"] is True


@pytest.mark.asyncio
async def test_resolution_denied_when_measure_not_in_populated_list(client):
    model = make_model()
    in_list = uuid.uuid4()
    out_of_list = uuid.uuid4()
    p = _persona(included_measure_ids=[str(in_list)])
    measure = types.SimpleNamespace(id=out_of_list, name="gl_expense_sum")
    db = make_mock_db()
    db.get = _scripted_get(model=model, persona=p, measure=measure)

    with patch("src.api.personas.get_tenant_db", async_gen_from(db)):
        resp = await client.get(
            f"{PREFIX}/{p.id}/resolution?measure_id={out_of_list}"
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["measure_allowed"] is False
    assert "gl_expense_sum" in (body["reason"] or "")


# ---------------------------------------------------------------------------
# Cascade helper — strip_id_from_personas
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_strip_id_from_personas_removes_target():
    from src.api.personas import strip_id_from_personas

    model_id = TEST_MODEL_ID
    target = uuid.uuid4()
    other = uuid.uuid4()
    p1 = _persona(name="A", included_measure_ids=[str(target), str(other)])
    p2 = _persona(name="B", included_measure_ids=[str(other)])
    db = make_mock_db()
    # Bug-7793: strip_id_from_personas now also queries ProjectPersonaModelScope,
    # so we need a second result in the queue (empty scopes for this test).
    db.execute = _execute_script(
        _ScalarResult([p1, p2]),  # model-level Persona query
        _ScalarResult([]),        # ProjectPersonaModelScope query
    )

    touched = await strip_id_from_personas(
        db, model_id=model_id, object_id=target, object_class="measure"
    )

    assert touched == ["A"]
    assert p1.included_measure_ids == [str(other)]
    assert p2.included_measure_ids == [str(other)]


@pytest.mark.asyncio
async def test_strip_id_from_personas_cleans_project_persona_scopes_bug7793():
    """Bug-7793: deleting a measure/dimension must also strip its UUID from
    ProjectPersonaModelScope.included_measure_ids / included_dimension_ids.
    Without this sweep, dangling UUIDs accumulate in agent persona scopes,
    silently shrinking the agent's grounded attribute set."""
    from src.api.personas import strip_id_from_personas

    model_id = TEST_MODEL_ID
    target = uuid.uuid4()
    other = uuid.uuid4()

    # Model-level persona: does NOT include the target (nothing to strip there).
    p1 = _persona(name="A", included_measure_ids=[str(other)])

    # Project-persona scope: includes the target measure id.
    scope = types.SimpleNamespace(
        id=uuid.uuid4(),
        project_persona_id=uuid.uuid4(),
        model_id=model_id,
        included_measure_ids=[str(target), str(other)],
        included_dimension_ids=[],
    )

    db = make_mock_db()
    db.execute = _execute_script(
        _ScalarResult([p1]),      # model-level Persona query
        _ScalarResult([scope]),   # ProjectPersonaModelScope query
    )

    touched = await strip_id_from_personas(
        db, model_id=model_id, object_id=target, object_class="measure"
    )

    # Model-level persona was not touched (did not contain the target).
    assert touched == []
    assert p1.included_measure_ids == [str(other)]

    # Bug-7793 guard: the project-persona scope's array was cleaned.
    assert scope.included_measure_ids == [str(other)]


@pytest.mark.asyncio
async def test_strip_id_from_personas_skips_project_scope_for_hierarchy_bug7793():
    """Bug-7793: ProjectPersonaModelScope has no included_hierarchy_ids column,
    so strip_id_from_personas for object_class='hierarchy' must not attempt
    to sweep project-persona scopes (it would fail with AttributeError)."""
    from src.api.personas import strip_id_from_personas

    model_id = TEST_MODEL_ID
    target = uuid.uuid4()

    p1 = _persona(name="A", included_hierarchy_ids=[str(target)])

    db = make_mock_db()
    # Only ONE result in queue: the model-level Persona query. No second
    # query should happen for hierarchy class.
    db.execute = _execute_script(
        _ScalarResult([p1]),
    )

    touched = await strip_id_from_personas(
        db, model_id=model_id, object_id=target, object_class="hierarchy"
    )

    assert touched == ["A"]
    assert p1.included_hierarchy_ids == []


# ---------------------------------------------------------------------------
# F-008-05 residual — persona responses expose restricted_column_ids so the
# gateway can drop restricted column names from persona catalogue metadata
# ---------------------------------------------------------------------------


class _RowsResult:
    """Mock for queries consumed via ``result.all()`` returning row tuples."""

    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


@pytest.mark.asyncio
async def test_list_personas_includes_restricted_column_ids(client):
    model = make_model()
    persona = _persona(name="Partner", slug="partner")
    restricted_col = uuid.uuid4()
    db = make_mock_db()
    db.get = _scripted_get(model=model)
    db.execute = _execute_script(
        _ScalarResult([persona]),                    # personas select
        _RowsResult([(persona.id, restricted_col)]),  # restriction join
    )

    with patch("src.api.personas.get_tenant_db", async_gen_from(db)):
        resp = await client.get(PREFIX)

    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 1
    assert items[0]["restricted_column_ids"] == [str(restricted_col)]


@pytest.mark.asyncio
async def test_get_persona_includes_restricted_column_ids(client):
    model = make_model()
    persona = _persona(name="Partner", slug="partner")
    restricted_col = uuid.uuid4()
    db = make_mock_db()
    db.get = _scripted_get(model=model, persona=persona)
    db.execute = _execute_script(
        _RowsResult([(persona.id, restricted_col)]),  # restriction join
    )

    with patch("src.api.personas.get_tenant_db", async_gen_from(db)):
        resp = await client.get(f"{PREFIX}/{persona.id}")

    assert resp.status_code == 200
    assert resp.json()["restricted_column_ids"] == [str(restricted_col)]


# ---------------------------------------------------------------------------
# Bug-7051: persona + tag restriction atomicity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_persona_with_restrictions_atomic_success(client):
    """Persona creation with restricted_tag_ids persists both persona and
    restrictions in a single transaction (Bug-7051 fix)."""
    model = make_model()
    tag_id = uuid.uuid4()
    persona_id = uuid.uuid4()
    restricted_col = uuid.uuid4()
    db = make_mock_db()
    db.get = _scripted_get(model=model)

    # Execute sequence:
    # 1. _validate_restricted_tags: tag existence check
    # 2. _persist_tag_restrictions: delete existing (none) -> noop result
    # 3. _restricted_columns_by_persona: post-commit restricted columns
    db.execute = _execute_script(
        _ScalarResult([tag_id]),                            # tag validation
        MagicMock(),                                       # delete restrictions
        _RowsResult([(persona_id, restricted_col)]),       # restricted columns
    )

    captured_adds: list = []

    def _capturing_add(obj):
        captured_adds.append(obj)
        # Simulate flush assigning an id to persona objects
        if not hasattr(obj, "id") or obj.id is None:
            obj.id = persona_id
        if not hasattr(obj, "created_at") or obj.created_at is None:
            obj.created_at = NOW
        if not hasattr(obj, "updated_at") or obj.updated_at is None:
            obj.updated_at = NOW

    db.add = _capturing_add

    async def _refresh(obj):
        return None

    db.refresh = _refresh

    with (
        patch("src.api.personas.get_tenant_db", async_gen_from(db)),
        patch("src.api.personas.audit_required", new=AsyncMock()),
    ):
        resp = await client.post(
            PREFIX,
            json={
                "name": "Restricted",
                "slug": "restricted",
                "restricted_tag_ids": [str(tag_id)],
                "audience_roles": ["sales_analyst"],
            },
        )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "Restricted"
    assert body["restricted_column_ids"] == [str(restricted_col)]

    # Verify persona and restriction were both added
    assert len(captured_adds) == 2, (
        f"Expected 2 db.add calls (persona + restriction), got {len(captured_adds)}"
    )

    # First add is the Persona, second is PersonaTagRestriction
    persona_obj = captured_adds[0]
    restriction_obj = captured_adds[1]
    assert hasattr(persona_obj, "name") and persona_obj.name == "Restricted"
    assert hasattr(restriction_obj, "data_tag_id")
    assert restriction_obj.data_tag_id == tag_id

    # flush was called (before commit) to get the persona id
    db.flush.assert_awaited()
    # commit was called exactly once (atomic commit of both)
    db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_create_persona_rollback_on_restriction_commit_failure(client):
    """Bug-7051 SECURITY: if the commit fails after restrictions are staged,
    the persona row must NOT be persisted — the entire transaction rolls back.
    This prevents a persona from existing without its restrictions."""
    from sqlalchemy.exc import IntegrityError

    model = make_model()
    tag_id = uuid.uuid4()
    db = make_mock_db()
    db.get = _scripted_get(model=model)

    # Execute sequence: tag validation -> delete restrictions (noop)
    db.execute = _execute_script(
        _ScalarResult([tag_id]),  # tag validation
        MagicMock(),             # delete restrictions
    )

    captured_adds: list = []

    def _capturing_add(obj):
        captured_adds.append(obj)
        if not hasattr(obj, "id") or obj.id is None:
            obj.id = uuid.uuid4()
        obj.created_at = NOW
        obj.updated_at = NOW

    db.add = _capturing_add

    # flush succeeds (persona gets its id), but commit fails
    # simulating a constraint violation on the restriction insert
    db.flush = AsyncMock()
    db.commit = AsyncMock(side_effect=IntegrityError("fk violation", None, None))
    db.rollback = AsyncMock()

    with patch("src.api.personas.get_tenant_db", async_gen_from(db)):
        resp = await client.post(
            PREFIX,
            json={
                "name": "Dangerous",
                "slug": "dangerous",
                "restricted_tag_ids": [str(tag_id)],
                "audience_roles": ["sales_analyst"],
            },
        )

    # The request must fail — the persona must NOT be created
    assert resp.status_code == 409, (
        f"Expected 409 on commit failure, got {resp.status_code}: {resp.text}"
    )

    # rollback must have been called — proving the transaction was aborted
    db.rollback.assert_awaited_once()

    # The persona row that was flushed must be rolled back by the DB,
    # not committed. Commit was called once and failed.
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_persona_rollback_on_flush_failure(client):
    """Bug-7051: if flush fails (persona name conflict), no restriction
    inserts are attempted and the transaction rolls back."""
    from sqlalchemy.exc import IntegrityError

    model = make_model()
    tag_id = uuid.uuid4()
    db = make_mock_db()
    db.get = _scripted_get(model=model)

    # Tag validation succeeds
    db.execute = _execute_script(_ScalarResult([tag_id]))

    db.flush = AsyncMock(side_effect=IntegrityError("dup name", None, None))
    db.rollback = AsyncMock()

    with patch("src.api.personas.get_tenant_db", async_gen_from(db)):
        resp = await client.post(
            PREFIX,
            json={
                "name": "Duplicate",
                "slug": "duplicate",
                "restricted_tag_ids": [str(tag_id)],
                "audience_roles": ["sales_analyst"],
            },
        )

    assert resp.status_code == 409
    assert resp.json()["detail"]["error_code"] == "PERSONA_NAME_CONFLICT"
    db.rollback.assert_awaited_once()
    # commit must NOT have been called — flush failed first
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_persona_with_restrictions_atomic(client):
    """Bug-7051: update persona with restricted_tag_ids replaces restrictions
    in the same transaction as the persona field update."""
    model = make_model()
    persona = _persona(name="Partner", slug="partner")
    new_tag_id = uuid.uuid4()
    restricted_col = uuid.uuid4()
    db = make_mock_db()
    db.get = _scripted_get(model=model, persona=persona)

    # Execute sequence:
    # 1. _validate_restricted_tags: tag existence
    # 2. _persist_tag_restrictions: delete existing
    # 3. _restricted_columns_by_persona: post-commit
    db.execute = _execute_script(
        _ScalarResult([new_tag_id]),                       # tag validation
        MagicMock(),                                       # delete restrictions
        _RowsResult([(persona.id, restricted_col)]),       # restricted columns
    )

    captured_adds: list = []

    def _capturing_add(obj):
        captured_adds.append(obj)

    db.add = _capturing_add

    async def _refresh(obj):
        return None

    db.refresh = _refresh

    with (
        patch("src.api.personas.get_tenant_db", async_gen_from(db)),
        patch("src.api.personas.audit_required", new=AsyncMock()),
    ):
        resp = await client.patch(
            f"{PREFIX}/{persona.id}",
            json={
                "description": "Updated with restrictions",
                "restricted_tag_ids": [str(new_tag_id)],
                "audience_roles": ["sales_analyst"],
            },
        )

    assert resp.status_code == 200, resp.text
    assert persona.description == "Updated with restrictions"

    # Verify restriction was added
    assert len(captured_adds) == 1  # only restriction (persona updated in-place)
    assert captured_adds[0].data_tag_id == new_tag_id

    # commit was called once (atomic)
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_update_persona_rollback_on_commit_failure(client):
    """Bug-7051 SECURITY: if commit fails during update with restrictions,
    both persona changes and restriction changes roll back."""
    from sqlalchemy.exc import IntegrityError

    model = make_model()
    persona = _persona(name="Partner", slug="partner")
    new_tag_id = uuid.uuid4()
    db = make_mock_db()
    db.get = _scripted_get(model=model, persona=persona)

    # Tag validation + delete restrictions
    db.execute = _execute_script(
        _ScalarResult([new_tag_id]),
        MagicMock(),
    )

    db.commit = AsyncMock(side_effect=IntegrityError("fk violation", None, None))
    db.rollback = AsyncMock()

    async def _refresh(obj):
        return None

    db.refresh = _refresh

    with patch("src.api.personas.get_tenant_db", async_gen_from(db)):
        resp = await client.patch(
            f"{PREFIX}/{persona.id}",
            json={
                "name": "Colliding Name",
                "slug": "colliding",
                "restricted_tag_ids": [str(new_tag_id)],
                "audience_roles": ["sales_analyst"],
            },
        )

    assert resp.status_code == 409
    db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_persona_without_restrictions_backward_compatible(client):
    """Bug-7051: creating a persona WITHOUT restricted_tag_ids still works
    identically to the pre-fix behavior (no restriction queries)."""
    model = make_model()
    measure_id = uuid.uuid4()
    db = make_mock_db()
    db.get = _scripted_get(model=model)
    # F-008-21 include-id existence + restricted columns post-commit
    db.execute = _execute_script(
        _ScalarResult([measure_id]),  # include-id existence
        _RowsResult([]),             # restricted columns (empty)
    )

    def _add(obj):
        if not hasattr(obj, "id") or obj.id is None:
            obj.id = uuid.uuid4()
        obj.created_at = NOW
        obj.updated_at = NOW

    db.add = _add

    async def _refresh(obj):
        return None

    db.refresh = _refresh

    with (
        patch("src.api.personas.get_tenant_db", async_gen_from(db)),
        patch("src.api.personas.audit_required", new=AsyncMock()),
    ):
        resp = await client.post(
            PREFIX,
            json={
                "name": "NoRestrictions",
                "slug": "norestrictions",
                "included_measure_ids": [str(measure_id)],
                "audience_roles": ["sales_analyst"],
            },
        )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "NoRestrictions"
    assert body["restricted_column_ids"] == []


# ---------------------------------------------------------------------------
# Bug-7052: audit assertions for security-policy mutations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_persona_emits_security_audit(client):
    """Bug-7052: persona creation must emit a security.persona_create audit event
    with critical severity, the actor's email, and security-relevant detail."""
    model = make_model()
    db = make_mock_db()
    db.get = _scripted_get(model=model)
    db.execute = _execute_script(_ScalarResult([]))

    def _add(obj):
        if not hasattr(obj, "id") or obj.id is None:
            obj.id = uuid.uuid4()
        obj.created_at = NOW
        obj.updated_at = NOW

    db.add = _add

    async def _refresh(obj):
        return None

    db.refresh = _refresh

    audit_mock = AsyncMock()
    with (
        patch("src.api.personas.get_tenant_db", async_gen_from(db)),
        patch("src.api.personas.audit_required", audit_mock),
    ):
        resp = await client.post(
            PREFIX,
            json={"name": "Audited", "slug": "audited", "bypass_row_security": True},
        )

    assert resp.status_code == 201, resp.text
    audit_mock.assert_awaited_once()
    call_kw = audit_mock.call_args.kwargs
    assert call_kw["action"] == "security.persona_create"
    assert call_kw["severity"] == "critical"
    assert call_kw["target_type"] == "persona"
    assert call_kw["target_name"] == "Audited"
    assert call_kw["detail"]["bypass_row_security"] is True


@pytest.mark.asyncio
async def test_update_persona_security_fields_emits_audit(client):
    """Bug-7052: updating security-relevant fields emits security.persona_update."""
    model = make_model()
    p = _persona(included_measure_ids=[])
    db = make_mock_db()
    db.get = _scripted_get(model=model, persona=p)
    db.execute = _execute_script(_ScalarResult([]))

    async def _refresh(obj):
        return None

    db.refresh = _refresh

    audit_mock = AsyncMock()
    with (
        patch("src.api.personas.get_tenant_db", async_gen_from(db)),
        patch("src.api.personas.audit_required", audit_mock),
    ):
        resp = await client.patch(
            f"{PREFIX}/{p.id}",
            json={"bypass_row_security": True},
        )

    assert resp.status_code == 200, resp.text
    audit_mock.assert_awaited_once()
    call_kw = audit_mock.call_args.kwargs
    assert call_kw["action"] == "security.persona_update"
    assert call_kw["severity"] == "critical"
    assert "bypass_row_security" in call_kw["detail"]["changed_fields"]
    assert call_kw["detail"]["widens_access"] is True


@pytest.mark.asyncio
async def test_update_persona_nonsecurity_fields_skips_audit(client):
    """Bug-7052: updating non-security fields (name, description) does NOT
    emit a security audit event."""
    model = make_model()
    p = _persona(name="OldName")
    db = make_mock_db()
    db.get = _scripted_get(model=model, persona=p)
    db.execute = _execute_script(_ScalarResult([]))

    async def _refresh(obj):
        return None

    db.refresh = _refresh

    audit_mock = AsyncMock()
    with (
        patch("src.api.personas.get_tenant_db", async_gen_from(db)),
        patch("src.api.personas.audit_required", audit_mock),
    ):
        resp = await client.patch(
            f"{PREFIX}/{p.id}",
            json={"description": "Just a description change"},
        )

    assert resp.status_code == 200, resp.text
    audit_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_persona_emits_security_audit(client):
    """Bug-7052: persona deletion must emit a security.persona_delete audit event."""
    model = make_model()
    p = _persona(name="ToDelete")
    p.bypass_row_security = True
    db = make_mock_db()
    db.get = _scripted_get(model=model, persona=p)

    audit_mock = AsyncMock()
    with (
        patch("src.api.personas.get_tenant_db", async_gen_from(db)),
        patch("src.api.personas.audit_required", audit_mock),
    ):
        resp = await client.delete(f"{PREFIX}/{p.id}")

    assert resp.status_code == 204
    audit_mock.assert_awaited_once()
    call_kw = audit_mock.call_args.kwargs
    assert call_kw["action"] == "security.persona_delete"
    assert call_kw["severity"] == "critical"
    assert call_kw["target_name"] == "ToDelete"
    assert call_kw["detail"]["had_bypass_row_security"] is True
