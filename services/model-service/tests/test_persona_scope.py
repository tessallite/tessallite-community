"""Tests for persona-scoped metadata filtering on measures, dimensions, and hierarchies.

Covers Phase 2 + Phase 7 of the persona-scoped API plan:
  - persona_id filtering on list endpoints
  - empty allow-list semantics (unrestricted)
  - nonexistent / wrong-model persona rejection
  - locked-persona enforcement (embed users)
  - malformed allow-list UUIDs fail closed
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from .result_fakes import FakeScalarResult

from fastapi import HTTPException
from shared.auth.middleware import CurrentEmbedUser, CurrentUser
from src.auth.middleware import get_current_user
from src.api._persona_scope import resolve_effective_persona
from shared.security.persona_resolver import get_assigned_personas, is_in_audience

from .conftest import (
    NOW,
    TEST_MODEL_ID,
    TEST_PROJECT_ID,
    TEST_TENANT,
    async_gen_from,
    client,
    make_mock_db,
    make_model,
)

pytestmark = pytest.mark.unit

MEASURES_URL = f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/measures"
DIMENSIONS_URL = f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/dimensions"
HIERARCHIES_URL = f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/hierarchies"


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------

def _persona(
    *,
    persona_id: uuid.UUID | None = None,
    model_id: uuid.UUID = TEST_MODEL_ID,
    included_measure_ids: list | None = None,
    included_dimension_ids: list | None = None,
    included_hierarchy_ids: list | None = None,
    audience_roles: list | None = None,
    includes_hidden_columns: bool = False,
    bypass_row_security: bool = False,
) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=persona_id or uuid.uuid4(),
        model_id=model_id,
        name="Test Persona",
        slug="test-persona",
        description=None,
        included_measure_ids=included_measure_ids or [],
        included_dimension_ids=included_dimension_ids or [],
        included_hierarchy_ids=included_hierarchy_ids or [],
        audience_roles=audience_roles if audience_roles is not None else [],
        default_filters={},
        includes_hidden_columns=includes_hidden_columns,
        bypass_row_security=bypass_row_security,
        created_at=NOW,
        updated_at=NOW,
    )


def _measure(measure_id: uuid.UUID | None = None, name: str = "Revenue") -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=measure_id or uuid.uuid4(),
        model_id=TEST_MODEL_ID,
        name=name,
        display_name=name,
        description=None,
        source_column_id=None,
        expression=None,
        measure_type="standard",
        display_folder=None,
        base_measure_id=None,
        semi_additive_behavior=None,
        is_invalid=False,
        invalid_reason=None,
        cross_model_source=None,
        user_defined_attribute_id=None,
        data_type="numeric",
        default_agg="sum",
        format=None,
        variant_kind=None,
        variant_of_measure_id=None,
        variant_n=None,
        is_additive=True,
        created_at=NOW,
        updated_at=NOW,
    )


def _dimension(dim_id: uuid.UUID | None = None, name: str = "Country") -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=dim_id or uuid.uuid4(),
        model_id=TEST_MODEL_ID,
        name=name,
        display_name=name,
        description=None,
        source_column_id=None,
        user_defined_attribute_id=None,
        is_time_dim=False,
        time_grain=None,
        display_folder=None,
        is_invalid=False,
        invalid_reason=None,
        created_at=NOW,
        updated_at=NOW,
    )


def _hierarchy(hier_id: uuid.UUID | None = None, name: str = "Geo") -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=hier_id or uuid.uuid4(),
        model_id=TEST_MODEL_ID,
        name=name,
        type="explicit",
        dimension_kind="regular",
        description=None,
        segment_config=None,
        date_config=None,
        calendar_type=None,
        fiscal_year_start_month=None,
        created_at=NOW,
        updated_at=NOW,
    )


class _ScalarResult:
    def __init__(self, items):
        self._items = list(items)

    def scalars(self):
        return FakeScalarResult(self._items)

    def all(self):
        return list(self._items)

    def scalar_one_or_none(self):
        return self._items[0] if self._items else None

    def fetchall(self):
        return list(self._items)


_EMPTY = _ScalarResult([])


def _persona_resolve_results(persona_obj, *, caller_roles: set[str] | None = None):
    """Execute results for ``get_assigned_personas`` plus a load when the
    caller is not in audience (F-008-03 tag lookup + voluntary pick).
    """
    roles = caller_roles if caller_roles is not None else set()
    seq = [_ScalarResult([persona_obj]), _EMPTY]
    if not is_in_audience(persona_obj, roles):
        seq.append(_ScalarResult([persona_obj]))
    return seq


def _assigned(*personas):
    """``get_assigned_personas``: all personas, then tag-id lookup if any."""
    seq = [_ScalarResult(list(personas))]
    if personas:
        seq.append(_EMPTY)
    return seq


def _base_db():
    """Return a mock DB with model lookup wired."""
    db = make_mock_db()
    model = make_model()

    async def _get(cls, obj_id):
        if obj_id == TEST_MODEL_ID:
            return model
        return None
    db.get = AsyncMock(side_effect=_get)
    return db


# ---------------------------------------------------------------------------
# Measures — persona filtering
#
# Call order for list_measures (non-privileged user):
#   no persona:   [assignment_query, redundant_tables, redundant_joins, measures_query]
#   with persona: [assignment_query(returns persona), redundant_tables, redundant_joins, measures_query]
# ---------------------------------------------------------------------------

class TestMeasuresPersonaScope:

    @pytest.fixture(autouse=True)
    def _setup(self):
        self.m1 = _measure(name="Revenue")
        self.m2 = _measure(name="Cost")
        self.persona = _persona(included_measure_ids=[str(self.m1.id)])

    def _mock_db(self, *, persona_obj=None, measures=None):
        db = _base_db()
        responses = []
        if persona_obj is not None:
            responses.extend(_persona_resolve_results(persona_obj))
        else:
            responses.append(_EMPTY)  # persona assignment query (no assignments)
        responses.append(_EMPTY)  # redundant partners: tables
        responses.append(_EMPTY)  # redundant partners: joins
        responses.append(_ScalarResult(measures or []))  # main query
        responses.extend([_EMPTY] * 5)
        db.execute = AsyncMock(side_effect=responses)
        return db

    @pytest.mark.asyncio
    async def test_no_persona_returns_all(self, client):
        db = self._mock_db(measures=[self.m1, self.m2])
        with patch("src.api.measures.get_tenant_db", async_gen_from(db)):
            resp = await client.get(MEASURES_URL)
        assert resp.status_code == 200
        names = {m["name"] for m in resp.json()}
        assert names == {"Revenue", "Cost"}

    @pytest.mark.asyncio
    async def test_valid_persona_filters(self, client):
        db = self._mock_db(persona_obj=self.persona, measures=[self.m1])
        with patch("src.api.measures.get_tenant_db", async_gen_from(db)):
            resp = await client.get(f"{MEASURES_URL}?persona_id={self.persona.id}")
        assert resp.status_code == 200
        names = [m["name"] for m in resp.json()]
        assert names == ["Revenue"]

    @pytest.mark.asyncio
    async def test_empty_allow_list_unrestricted(self, client):
        persona = _persona(included_measure_ids=[])
        db = self._mock_db(persona_obj=persona, measures=[self.m1, self.m2])
        with patch("src.api.measures.get_tenant_db", async_gen_from(db)):
            resp = await client.get(f"{MEASURES_URL}?persona_id={persona.id}")
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    @pytest.mark.asyncio
    async def test_nonexistent_persona_404(self, client):
        db = _base_db()
        db.execute = AsyncMock(return_value=_EMPTY)
        with patch("src.api.measures.get_tenant_db", async_gen_from(db)):
            resp = await client.get(f"{MEASURES_URL}?persona_id={uuid.uuid4()}")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_wrong_model_persona_404(self, client):
        db = _base_db()
        db.execute = AsyncMock(return_value=_EMPTY)
        other_persona = _persona(model_id=uuid.uuid4())
        with patch("src.api.measures.get_tenant_db", async_gen_from(db)):
            resp = await client.get(f"{MEASURES_URL}?persona_id={other_persona.id}")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Dimensions — persona filtering
#
# Call order for list_dimensions (non-privileged user):
#   no persona:   [assignment_query, redundant_tables, redundant_joins, dimensions_query]
#   with persona: [assignment_query(returns persona), redundant_tables, redundant_joins, dimensions_query]
# ---------------------------------------------------------------------------

class TestDimensionsPersonaScope:

    @pytest.fixture(autouse=True)
    def _setup(self):
        self.d1 = _dimension(name="Country")
        self.d2 = _dimension(name="City")
        self.persona = _persona(included_dimension_ids=[str(self.d1.id)])

    def _mock_db(self, *, persona_obj=None, dimensions=None):
        db = _base_db()
        responses = []
        if persona_obj is not None:
            responses.extend(_persona_resolve_results(persona_obj))
        else:
            responses.append(_EMPTY)  # persona assignment query (no assignments)
        responses.append(_EMPTY)  # redundant partners: tables
        responses.append(_EMPTY)  # redundant partners: joins
        responses.append(_ScalarResult(dimensions or []))  # main query
        responses.extend([_EMPTY] * 5)
        db.execute = AsyncMock(side_effect=responses)
        return db

    @pytest.mark.asyncio
    async def test_no_persona_returns_all(self, client):
        db = self._mock_db(dimensions=[self.d1, self.d2])
        with patch("src.api.dimensions.get_tenant_db", async_gen_from(db)):
            resp = await client.get(DIMENSIONS_URL)
        assert resp.status_code == 200
        names = {d["name"] for d in resp.json()}
        assert names == {"Country", "City"}

    @pytest.mark.asyncio
    async def test_valid_persona_filters(self, client):
        db = self._mock_db(persona_obj=self.persona, dimensions=[self.d1])
        with patch("src.api.dimensions.get_tenant_db", async_gen_from(db)):
            resp = await client.get(f"{DIMENSIONS_URL}?persona_id={self.persona.id}")
        assert resp.status_code == 200
        names = [d["name"] for d in resp.json()]
        assert names == ["Country"]

    @pytest.mark.asyncio
    async def test_empty_allow_list_unrestricted(self, client):
        persona = _persona(included_dimension_ids=[])
        db = self._mock_db(persona_obj=persona, dimensions=[self.d1, self.d2])
        with patch("src.api.dimensions.get_tenant_db", async_gen_from(db)):
            resp = await client.get(f"{DIMENSIONS_URL}?persona_id={persona.id}")
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    @pytest.mark.asyncio
    async def test_nonexistent_persona_404(self, client):
        db = _base_db()
        db.execute = AsyncMock(return_value=_EMPTY)
        with patch("src.api.dimensions.get_tenant_db", async_gen_from(db)):
            resp = await client.get(f"{DIMENSIONS_URL}?persona_id={uuid.uuid4()}")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Hierarchies — persona filtering
#
# Call order for list_hierarchies (non-privileged user):
#   no persona:   [assignment_query, hierarchies_query, level_query_per_hierarchy...]
#   with persona: [assignment_query(returns persona), hierarchies_query, level_query_per_hierarchy...]
# ---------------------------------------------------------------------------

class TestHierarchiesPersonaScope:

    @pytest.fixture(autouse=True)
    def _setup(self):
        self.h1 = _hierarchy(name="Geo")
        self.h2 = _hierarchy(name="Time")
        self.persona = _persona(included_hierarchy_ids=[str(self.h1.id)])

    def _mock_db(self, *, persona_obj=None, hierarchies=None):
        db = _base_db()
        responses = []
        if persona_obj is not None:
            responses.extend(_persona_resolve_results(persona_obj))
        else:
            responses.append(_EMPTY)  # persona assignment query (no assignments)
        responses.append(_ScalarResult(hierarchies or []))  # main query
        for _ in (hierarchies or []):
            responses.append(_EMPTY)  # level query per hierarchy
        responses.extend([_EMPTY] * 5)
        db.execute = AsyncMock(side_effect=responses)
        return db

    @pytest.mark.asyncio
    async def test_no_persona_returns_all(self, client):
        db = self._mock_db(hierarchies=[self.h1, self.h2])
        with patch("src.api.hierarchies.get_tenant_db", async_gen_from(db)):
            resp = await client.get(HIERARCHIES_URL)
        assert resp.status_code == 200
        names = {h["name"] for h in resp.json()}
        assert names == {"Geo", "Time"}

    @pytest.mark.asyncio
    async def test_valid_persona_filters(self, client):
        db = self._mock_db(persona_obj=self.persona, hierarchies=[self.h1])
        with patch("src.api.hierarchies.get_tenant_db", async_gen_from(db)):
            resp = await client.get(f"{HIERARCHIES_URL}?persona_id={self.persona.id}")
        assert resp.status_code == 200
        names = [h["name"] for h in resp.json()]
        assert names == ["Geo"]

    @pytest.mark.asyncio
    async def test_empty_allow_list_unrestricted(self, client):
        persona = _persona(included_hierarchy_ids=[])
        db = self._mock_db(persona_obj=persona, hierarchies=[self.h1, self.h2])
        with patch("src.api.hierarchies.get_tenant_db", async_gen_from(db)):
            resp = await client.get(f"{HIERARCHIES_URL}?persona_id={persona.id}")
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    @pytest.mark.asyncio
    async def test_nonexistent_persona_404(self, client):
        db = _base_db()
        db.execute = AsyncMock(return_value=_EMPTY)
        with patch("src.api.hierarchies.get_tenant_db", async_gen_from(db)):
            resp = await client.get(f"{HIERARCHIES_URL}?persona_id={uuid.uuid4()}")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Locked persona (embed user) — cross-endpoint
# ---------------------------------------------------------------------------

class TestLockedPersonaEnforcement:

    @pytest.fixture(autouse=True)
    def _setup(self):
        self.locked_persona = _persona()
        self.embed_user = CurrentEmbedUser(
            user_id="embed-token",
            tenant_id=TEST_TENANT,
            email="embed@example.com",
            model_ids=[str(TEST_MODEL_ID)],
            persona_id=str(self.locked_persona.id),
            capabilities=["query"],
        )

    def _mock_db_measures(self, *, persona_obj=None, items=None):
        db = _base_db()
        responses = []
        if persona_obj is not None:
            responses.append(_ScalarResult([persona_obj]))
        responses.append(_EMPTY)  # redundant partners: tables
        responses.append(_EMPTY)  # redundant partners: joins
        responses.append(_ScalarResult(items or []))
        responses.extend([_EMPTY] * 5)
        db.execute = AsyncMock(side_effect=responses)
        return db

    @pytest.mark.asyncio
    async def test_locked_persona_applies_on_measures(self):
        import httpx
        from src.main import app
        app.dependency_overrides[get_current_user] = lambda: self.embed_user
        try:
            m = _measure(name="Revenue")
            db = self._mock_db_measures(persona_obj=self.locked_persona, items=[m])
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
            ) as ac:
                with patch("src.api.measures.get_tenant_db", async_gen_from(db)):
                    resp = await ac.get(MEASURES_URL)
            assert resp.status_code == 200

        finally:
            app.dependency_overrides.pop(get_current_user, None)

    @pytest.mark.asyncio
    async def test_locked_persona_conflict_403(self):
        import httpx
        from src.main import app
        app.dependency_overrides[get_current_user] = lambda: self.embed_user
        try:
            db = _base_db()
            db.execute = AsyncMock(return_value=_EMPTY)
            other_id = uuid.uuid4()
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
            ) as ac:
                with patch("src.api.measures.get_tenant_db", async_gen_from(db)):
                    resp = await ac.get(f"{MEASURES_URL}?persona_id={other_id}")
            assert resp.status_code == 403

        finally:
            app.dependency_overrides.pop(get_current_user, None)


# ---------------------------------------------------------------------------
# Malformed allow-list UUID — fail closed
# ---------------------------------------------------------------------------

class TestMalformedAllowList:

    @pytest.mark.asyncio
    async def test_malformed_uuid_in_allow_list_500(self, client):
        persona = _persona(included_measure_ids=["not-a-uuid"])
        db = _base_db()
        db.execute = AsyncMock(return_value=_ScalarResult([persona]))
        with patch("src.api.measures.get_tenant_db", async_gen_from(db)):
            resp = await client.get(f"{MEASURES_URL}?persona_id={persona.id}")
        assert resp.status_code == 500
        assert "Persona configuration error" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Detail endpoint persona scoping — H4 from review
# ---------------------------------------------------------------------------

class TestDetailEndpointPersonaScope:

    @pytest.fixture(autouse=True)
    def _setup(self):
        self.m_allowed = _measure(name="Revenue")
        self.m_hidden = _measure(name="Secret Measure")
        self.d_allowed = _dimension(name="Country")
        self.d_hidden = _dimension(name="Secret Dim")
        self.h_allowed = _hierarchy(name="Geo")
        self.h_hidden = _hierarchy(name="Secret Hier")
        self.persona = _persona(
            included_measure_ids=[str(self.m_allowed.id)],
            included_dimension_ids=[str(self.d_allowed.id)],
            included_hierarchy_ids=[str(self.h_allowed.id)],
        )

    def _db_for_detail(self, *, persona_obj, item):
        db = make_mock_db()
        model = make_model()

        async def _get(cls, obj_id):
            if obj_id == TEST_MODEL_ID:
                return model
            if obj_id == item.id:
                return item
            return None
        db.get = AsyncMock(side_effect=_get)
        db.execute = AsyncMock(return_value=_ScalarResult([persona_obj]))
        return db

    @pytest.mark.asyncio
    async def test_measure_hidden_by_persona_404(self, client):
        db = self._db_for_detail(persona_obj=self.persona, item=self.m_hidden)
        with patch("src.api.measures.get_tenant_db", async_gen_from(db)):
            resp = await client.get(
                f"{MEASURES_URL}/{self.m_hidden.id}?persona_id={self.persona.id}"
            )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_dimension_hidden_by_persona_404(self, client):
        db = self._db_for_detail(persona_obj=self.persona, item=self.d_hidden)
        with patch("src.api.dimensions.get_tenant_db", async_gen_from(db)):
            resp = await client.get(
                f"{DIMENSIONS_URL}/{self.d_hidden.id}?persona_id={self.persona.id}"
            )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_hierarchy_hidden_by_persona_404(self, client):
        db = make_mock_db()
        model = make_model()

        async def _get(cls, obj_id):
            if obj_id == TEST_MODEL_ID:
                return model
            if obj_id == self.h_hidden.id:
                return self.h_hidden
            return None
        db.get = AsyncMock(side_effect=_get)
        db.execute = AsyncMock(return_value=_ScalarResult([self.persona]))
        with patch("src.api.hierarchies.get_tenant_db", async_gen_from(db)):
            resp = await client.get(
                f"{HIERARCHIES_URL}/{self.h_hidden.id}?persona_id={self.persona.id}"
            )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_hierarchy_levels_hidden_parent_404(self, client):
        db = make_mock_db()
        model = make_model()

        async def _get(cls, obj_id):
            if obj_id == TEST_MODEL_ID:
                return model
            if obj_id == self.h_hidden.id:
                return self.h_hidden
            return None
        db.get = AsyncMock(side_effect=_get)
        db.execute = AsyncMock(return_value=_ScalarResult([self.persona]))
        with patch("src.api.hierarchies.get_tenant_db", async_gen_from(db)):
            resp = await client.get(
                f"{HIERARCHIES_URL}/{self.h_hidden.id}/levels?persona_id={self.persona.id}"
            )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Q2: Persona audience enforcement — resolve_effective_persona
# ---------------------------------------------------------------------------

def _level(
    *,
    level_id: uuid.UUID | None = None,
    name: str = "Level",
    ordinal: int = 0,
    key_attribute_id: uuid.UUID | None = None,
    key_attribute_source: str = "physical_column",
    hierarchy_id: uuid.UUID | None = None,
) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=level_id or uuid.uuid4(),
        name=name,
        ordinal=ordinal,
        key_attribute_id=key_attribute_id or uuid.uuid4(),
        key_attribute_source=key_attribute_source,
        hierarchy_id=hierarchy_id or uuid.uuid4(),
        description=None,
        time_unit=None,
        allowed_time_calcs=[],
    )


def _column(
    *,
    col_id: uuid.UUID | None = None,
    column_name: str = "col",
    model_table_id: uuid.UUID | None = None,
) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=col_id or uuid.uuid4(),
        column_name=column_name,
        data_type="string",
        model_table_id=model_table_id or uuid.uuid4(),
    )


def _table(
    *,
    table_id: uuid.UUID | None = None,
    physical_name: str = "dim_table",
) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=table_id or uuid.uuid4(),
        model_id=TEST_MODEL_ID,
        alias=None,
        display_name=physical_name,
        physical_name=physical_name,
        table_type="dimension",
    )


class TestAudienceEnforcement:

    @pytest.mark.asyncio
    async def test_single_assignment_auto_resolves(self):
        p = _persona(audience_roles=["viewer"])
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=_assigned(p))
        user = CurrentUser(user_id="u@test", tenant_id=TEST_TENANT, email="u@test", role="viewer")
        result = await resolve_effective_persona(
            db, current_user=user, model_id=TEST_MODEL_ID, requested_persona_id=None,
        )
        assert result is not None
        assert result.id == p.id

    @pytest.mark.asyncio
    async def test_single_assignment_rejects_other(self):
        p = _persona(audience_roles=["viewer"])
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=_assigned(p))
        user = CurrentUser(user_id="u@test", tenant_id=TEST_TENANT, email="u@test", role="viewer")
        with pytest.raises(HTTPException) as exc_info:
            await resolve_effective_persona(
                db, current_user=user, model_id=TEST_MODEL_ID,
                requested_persona_id=uuid.uuid4(),
            )
        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_multi_assignment_without_persona_rejected(self):
        p1 = _persona(audience_roles=["viewer"])
        p2 = _persona(audience_roles=["viewer"])
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=_assigned(p1, p2))
        user = CurrentUser(user_id="u@test", tenant_id=TEST_TENANT, email="u@test", role="viewer")
        with pytest.raises(HTTPException) as exc_info:
            await resolve_effective_persona(
                db, current_user=user, model_id=TEST_MODEL_ID,
                requested_persona_id=None,
            )
        assert exc_info.value.status_code == 403
        assert "multiple" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_multi_assignment_picks_valid(self):
        p1 = _persona(audience_roles=["viewer"])
        p2 = _persona(audience_roles=["viewer"])
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=_assigned(p1, p2))
        user = CurrentUser(user_id="u@test", tenant_id=TEST_TENANT, email="u@test", role="viewer")
        result = await resolve_effective_persona(
            db, current_user=user, model_id=TEST_MODEL_ID,
            requested_persona_id=p1.id,
        )
        assert result.id == p1.id

    @pytest.mark.asyncio
    async def test_unassigned_defaults_to_base(self):
        p_analyst = _persona(audience_roles=["analyst"])
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=_assigned(p_analyst))
        user = CurrentUser(user_id="u@test", tenant_id=TEST_TENANT, email="u@test", role="viewer")
        result = await resolve_effective_persona(
            db, current_user=user, model_id=TEST_MODEL_ID,
            requested_persona_id=None,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_unassigned_can_select_any(self):
        p_analyst = _persona(audience_roles=["analyst"])
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[
            *_assigned(p_analyst),
            _ScalarResult([p_analyst]),
        ])
        user = CurrentUser(user_id="u@test", tenant_id=TEST_TENANT, email="u@test", role="viewer")
        result = await resolve_effective_persona(
            db, current_user=user, model_id=TEST_MODEL_ID,
            requested_persona_id=p_analyst.id,
        )
        assert result.id == p_analyst.id

    @pytest.mark.asyncio
    async def test_privileged_admin_can_omit(self):
        db = AsyncMock()
        user = CurrentUser(user_id="a@test", tenant_id=TEST_TENANT, email="a@test", role="tenant_admin")
        result = await resolve_effective_persona(
            db, current_user=user, model_id=TEST_MODEL_ID,
            requested_persona_id=None,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_privileged_modeler_can_select_any(self):
        p = _persona(audience_roles=["viewer"])
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[_ScalarResult([p])])
        user = CurrentUser(user_id="m@test", tenant_id=TEST_TENANT, email="m@test", role="modeler")
        result = await resolve_effective_persona(
            db, current_user=user, model_id=TEST_MODEL_ID,
            requested_persona_id=p.id,
        )
        assert result.id == p.id

    @pytest.mark.asyncio
    async def test_multi_assignment_rejects_unassigned_persona(self):
        """Bug-619: user assigned to p1+p2 picks p3 (not in list) → 403."""
        p1 = _persona(audience_roles=["viewer"])
        p2 = _persona(audience_roles=["viewer"])
        p3 = _persona(audience_roles=["analyst"])
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=_assigned(p1, p2))
        user = CurrentUser(user_id="u@test", tenant_id=TEST_TENANT, email="u@test", role="viewer")
        with pytest.raises(HTTPException) as exc_info:
            await resolve_effective_persona(
                db, current_user=user, model_id=TEST_MODEL_ID,
                requested_persona_id=p3.id,
            )
        assert exc_info.value.status_code == 403
        assert "not assigned" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_role_none_user_unassigned_gets_base(self):
        """Bug-620: CurrentUser with role=None has no audience match → unassigned → base model."""
        p_viewer = _persona(audience_roles=["viewer"])
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=_assigned(p_viewer))
        user = CurrentUser(user_id="u@test", tenant_id=TEST_TENANT, email="u@test")
        result = await resolve_effective_persona(
            db, current_user=user, model_id=TEST_MODEL_ID,
            requested_persona_id=None,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_technical_persona_holder_auto_resolves(self):
        """Technical persona HOLDER (explicit audience-role match)
        auto-resolves to the technical persona (F-008-04: holding now
        requires a non-empty audience intersection, not an empty list)."""
        p_tech = _persona(
            includes_hidden_columns=True, audience_roles=["model_technical"],
        )
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=_assigned(p_tech))
        user = CurrentUser(user_id="t@test", tenant_id=TEST_TENANT, email="t@test", role="model_technical")
        result = await resolve_effective_persona(
            db, current_user=user, model_id=TEST_MODEL_ID,
            requested_persona_id=None,
        )
        assert result is not None
        assert result.id == p_tech.id

    @pytest.mark.asyncio
    async def test_technical_persona_holder_rejects_other_persona(self):
        """Technical persona holder cannot select a different persona."""
        p_tech = _persona(
            includes_hidden_columns=True, audience_roles=["model_technical"],
        )
        other_id = uuid.uuid4()
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=_assigned(p_tech))
        user = CurrentUser(user_id="t@test", tenant_id=TEST_TENANT, email="t@test", role="model_technical")
        with pytest.raises(HTTPException) as exc_info:
            await resolve_effective_persona(
                db, current_user=user, model_id=TEST_MODEL_ID,
                requested_persona_id=other_id,
            )
        assert exc_info.value.status_code == 403

    # -- F-008-04: seeded Technical persona must not capture regular users --

    @pytest.mark.asyncio
    async def test_seeded_technical_does_not_force_lock_business_user(self):
        """The realistic seeded shape: Technical (empty audience, hidden
        columns) next to a business persona assigned to viewers. A viewer
        must auto-resolve to the BUSINESS persona, not be force-locked to
        the technical (hidden-columns) view."""
        p_tech = _persona(includes_hidden_columns=True, audience_roles=[])
        p_partner = _persona(audience_roles=["viewer"])
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=_assigned(p_tech, p_partner))
        user = CurrentUser(user_id="u@test", tenant_id=TEST_TENANT, email="u@test", role="viewer")
        result = await resolve_effective_persona(
            db, current_user=user, model_id=TEST_MODEL_ID,
            requested_persona_id=None,
        )
        assert result is not None
        assert result.id == p_partner.id

    @pytest.mark.asyncio
    async def test_seeded_technical_business_persona_pick_succeeds(self):
        """A viewer picking their assigned business persona must get it,
        not a 403 'Technical persona holders must use the technical
        persona' (F-008-04)."""
        p_tech = _persona(includes_hidden_columns=True, audience_roles=[])
        p_partner = _persona(audience_roles=["viewer"])
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=_assigned(p_tech, p_partner))
        user = CurrentUser(user_id="u@test", tenant_id=TEST_TENANT, email="u@test", role="viewer")
        result = await resolve_effective_persona(
            db, current_user=user, model_id=TEST_MODEL_ID,
            requested_persona_id=p_partner.id,
        )
        assert result.id == p_partner.id

    @pytest.mark.asyncio
    async def test_unassigned_viewer_cannot_pick_hidden_persona(self):
        """A hidden-columns persona widens visibility, so it is never a
        legitimate voluntary pick for a non-privileged caller without an
        explicit audience-role grant (F-008-04)."""
        p_tech = _persona(includes_hidden_columns=True, audience_roles=[])
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[
            *_assigned(p_tech),
            _ScalarResult([p_tech]),
        ])
        user = CurrentUser(user_id="u@test", tenant_id=TEST_TENANT, email="u@test", role="viewer")
        with pytest.raises(HTTPException) as exc_info:
            await resolve_effective_persona(
                db, current_user=user, model_id=TEST_MODEL_ID,
                requested_persona_id=p_tech.id,
            )
        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_seeded_technical_alone_resolves_to_base(self):
        """A viewer on a model that only has the seeded Technical persona
        (empty audience) gets the unrestricted business base view, not the
        hidden-columns surface."""
        p_tech = _persona(includes_hidden_columns=True, audience_roles=[])
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=_assigned(p_tech))
        user = CurrentUser(user_id="u@test", tenant_id=TEST_TENANT, email="u@test", role="viewer")
        result = await resolve_effective_persona(
            db, current_user=user, model_id=TEST_MODEL_ID,
            requested_persona_id=None,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_privileged_admin_can_still_pick_technical(self):
        """Admins/modelers impersonate any persona, including technical."""
        p_tech = _persona(includes_hidden_columns=True, audience_roles=["model_technical"])
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=_assigned(p_tech))
        user = CurrentUser(user_id="a@test", tenant_id=TEST_TENANT, email="a@test", role="tenant_admin")
        result = await resolve_effective_persona(
            db, current_user=user, model_id=TEST_MODEL_ID,
            requested_persona_id=p_tech.id,
        )
        assert result.id == p_tech.id

    # -- Bug-6136 / F-008-30: bypass_row_security is a widening surface --

    @pytest.mark.asyncio
    async def test_unassigned_viewer_cannot_pick_bypass_persona(self):
        """A ``bypass_row_security`` persona skips RLS, so it is never a
        legitimate voluntary pick for a non-privileged, unassigned caller
        (Bug-6136). The resolver must DENY the escalation."""
        p_bypass = _persona(bypass_row_security=True, audience_roles=["dashboard_service"])
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[
            _ScalarResult([]),          # get_assigned_personas — analyst not in audience
            _ScalarResult([p_bypass]),  # load_persona_or_fail
        ])
        user = CurrentUser(user_id="u@test", tenant_id=TEST_TENANT, email="u@test", role="viewer")
        with pytest.raises(HTTPException) as exc_info:
            await resolve_effective_persona(
                db, current_user=user, model_id=TEST_MODEL_ID,
                requested_persona_id=p_bypass.id,
            )
        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_unassigned_viewer_cannot_pick_empty_audience_bypass_persona(self):
        """Bug-6136: empty audience does not make a bypass persona public.

        A regular unassigned caller may voluntarily select a narrowing persona,
        but a bypass persona widens row access and must be refused even when its
        audience list is empty.
        """
        p_bypass = _persona(bypass_row_security=True, audience_roles=[])
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[
            _ScalarResult([]),          # get_assigned_personas
            _ScalarResult([p_bypass]),  # load_persona_or_fail
        ])
        user = CurrentUser(user_id="u@test", tenant_id=TEST_TENANT, email="u@test", role="viewer")
        with pytest.raises(HTTPException) as exc_info:
            await resolve_effective_persona(
                db, current_user=user, model_id=TEST_MODEL_ID,
                requested_persona_id=p_bypass.id,
            )
        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_empty_audience_bypass_persona_not_auto_assigned(self):
        """An empty-audience ``bypass_row_security`` persona must NOT be
        treated as available-to-everyone; a regular user with no other
        assignment gets the RLS-enforced base model, not the bypass
        (Bug-6136 — the tenant-wide auto-assign path)."""
        p_bypass = _persona(bypass_row_security=True, audience_roles=[])
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=_assigned(p_bypass))
        user = CurrentUser(user_id="u@test", tenant_id=TEST_TENANT, email="u@test", role="viewer")
        result = await resolve_effective_persona(
            db, current_user=user, model_id=TEST_MODEL_ID,
            requested_persona_id=None,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_empty_audience_bypass_persona_not_in_assigned_list(self):
        """``get_assigned_personas`` must exclude an empty-audience bypass
        persona for a regular user (Bug-6136)."""
        p_bypass = _persona(bypass_row_security=True, audience_roles=[])
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=_assigned(p_bypass))
        user = CurrentUser(user_id="u@test", tenant_id=TEST_TENANT, email="u@test", role="viewer")
        assigned = await get_assigned_personas(db, user, TEST_MODEL_ID)
        assert assigned == []

    @pytest.mark.asyncio
    async def test_bypass_persona_granted_via_explicit_audience(self):
        """A user whose role IS in the bypass persona's audience list is a
        legitimate holder and auto-resolves to it (grant, not escalation)."""
        p_bypass = _persona(bypass_row_security=True, audience_roles=["dashboard_service"])
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=_assigned(p_bypass))
        user = CurrentUser(
            user_id="svc@test", tenant_id=TEST_TENANT, email="svc@test",
            role="dashboard_service",
        )
        result = await resolve_effective_persona(
            db, current_user=user, model_id=TEST_MODEL_ID,
            requested_persona_id=None,
        )
        assert result is not None
        assert result.id == p_bypass.id

    @pytest.mark.asyncio
    async def test_privileged_admin_can_still_pick_bypass(self):
        """Admins/modelers may impersonate a bypass persona (Bug-6136 must
        not over-restrict the privileged path)."""
        p_bypass = _persona(bypass_row_security=True, audience_roles=["dashboard_service"])
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=_assigned(p_bypass))
        user = CurrentUser(user_id="a@test", tenant_id=TEST_TENANT, email="a@test", role="tenant_admin")
        result = await resolve_effective_persona(
            db, current_user=user, model_id=TEST_MODEL_ID,
            requested_persona_id=p_bypass.id,
        )
        assert result.id == p_bypass.id


# ---------------------------------------------------------------------------
# Q3: Hierarchy-dimension filtering — levels backed by excluded dimensions
# ---------------------------------------------------------------------------

class TestHierarchyDimensionFiltering:

    @pytest.fixture(autouse=True)
    def _setup(self):
        self.attr_allowed = uuid.uuid4()
        self.attr_excluded = uuid.uuid4()
        self.dim_allowed = uuid.uuid4()
        self.dim_excluded = uuid.uuid4()
        self.h = _hierarchy(name="Geo")
        self.lv_allowed = _level(
            name="Country", ordinal=0, key_attribute_id=self.attr_allowed,
            hierarchy_id=self.h.id,
        )
        self.lv_excluded = _level(
            name="City", ordinal=1, key_attribute_id=self.attr_excluded,
            hierarchy_id=self.h.id,
        )
        self.persona = _persona(
            included_hierarchy_ids=[str(self.h.id)],
            included_dimension_ids=[str(self.dim_allowed)],
        )

    def _mock_db(self, *, persona_obj, hierarchies, all_levels, dimensions):
        db = _base_db()
        responses = []
        responses.extend(_persona_resolve_results(persona_obj))
        responses.append(_ScalarResult(hierarchies))
        responses.append(_ScalarResult(dimensions))
        responses.append(_ScalarResult(all_levels))
        responses.extend([_EMPTY] * 5)
        db.execute = AsyncMock(side_effect=responses)
        return db

    @pytest.mark.asyncio
    async def test_excluded_dimension_hides_level(self, client):
        dims = [
            (self.dim_allowed, self.attr_allowed, None),
            (self.dim_excluded, self.attr_excluded, None),
        ]
        db = self._mock_db(
            persona_obj=self.persona,
            hierarchies=[self.h],
            all_levels=[self.lv_allowed, self.lv_excluded],
            dimensions=dims,
        )
        with patch("src.api.hierarchies.get_tenant_db", async_gen_from(db)):
            resp = await client.get(
                f"{HIERARCHIES_URL}?persona_id={self.persona.id}"
            )
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) == 1
        assert items[0]["level_count"] == 1
        assert items[0]["level_names"] == ["Country"]

    @pytest.mark.asyncio
    async def test_uda_backed_level_excluded_by_dimension(self, client):
        """Bug-618: user_defined_attribute_id (third tuple element) must also be
        added to the excluded set when its dimension is not in the allow-list."""
        uda_id = uuid.uuid4()
        dims = [
            (self.dim_allowed, self.attr_allowed, None),
            (self.dim_excluded, None, uda_id),
        ]
        lv_uda = _level(
            name="CustomCalc", ordinal=1, key_attribute_id=uda_id,
            hierarchy_id=self.h.id,
        )
        db = self._mock_db(
            persona_obj=self.persona,
            hierarchies=[self.h],
            all_levels=[self.lv_allowed, lv_uda],
            dimensions=dims,
        )
        with patch("src.api.hierarchies.get_tenant_db", async_gen_from(db)):
            resp = await client.get(
                f"{HIERARCHIES_URL}?persona_id={self.persona.id}"
            )
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) == 1
        assert items[0]["level_count"] == 1
        assert items[0]["level_names"] == ["Country"]

    @pytest.mark.asyncio
    async def test_all_levels_excluded_hides_hierarchy(self, client):
        dims = [
            (self.dim_excluded, self.attr_allowed, None),
            (self.dim_excluded, self.attr_excluded, None),
        ]
        persona = _persona(
            included_hierarchy_ids=[str(self.h.id)],
            included_dimension_ids=[str(uuid.uuid4())],
        )
        db = self._mock_db(
            persona_obj=persona,
            hierarchies=[self.h],
            all_levels=[self.lv_allowed, self.lv_excluded],
            dimensions=dims,
        )
        with patch("src.api.hierarchies.get_tenant_db", async_gen_from(db)):
            resp = await client.get(
                f"{HIERARCHIES_URL}?persona_id={persona.id}"
            )
        assert resp.status_code == 200
        assert len(resp.json()) == 0


# ---------------------------------------------------------------------------
# Q3 detail endpoints: hierarchy detail + levels with dimension filtering
# ---------------------------------------------------------------------------

class TestHierarchyDetailDimensionFiltering:
    """Bug-621 / Bug-622: verify that get_hierarchy and list_hierarchy_levels
    filter out levels whose key_attribute_id maps to an excluded dimension."""

    HIER_DETAIL_URL = f"{HIERARCHIES_URL}/{{hierarchy_id}}"
    HIER_LEVELS_URL = f"{HIERARCHIES_URL}/{{hierarchy_id}}/levels"

    @pytest.fixture(autouse=True)
    def _setup(self):
        self.table_id = uuid.uuid4()
        self.attr_allowed = uuid.uuid4()
        self.attr_excluded = uuid.uuid4()
        self.dim_allowed = uuid.uuid4()
        self.dim_excluded = uuid.uuid4()
        self.h = _hierarchy(name="Geo")
        self.lv_allowed = _level(
            name="Country", ordinal=0, key_attribute_id=self.attr_allowed,
            hierarchy_id=self.h.id,
        )
        self.lv_excluded = _level(
            name="City", ordinal=1, key_attribute_id=self.attr_excluded,
            hierarchy_id=self.h.id,
        )
        self.col_allowed = _column(
            col_id=self.attr_allowed, column_name="country",
            model_table_id=self.table_id,
        )
        self.tbl = _table(table_id=self.table_id, physical_name="dim_geo")
        self.persona = _persona(
            included_hierarchy_ids=[str(self.h.id)],
            included_dimension_ids=[str(self.dim_allowed)],
        )

    def _detail_db(self):
        """Mock DB for get_hierarchy detail endpoint.

        db.execute call order:
          1. resolve_effective_persona → persona lookup
          2. get_excluded_level_attribute_ids → dimension data
          3. _levels_for_hierarchy → level objects
          4. _level_attributes_response → empty (per surviving level)
        """
        db = _base_db()
        dims = [
            (self.dim_allowed, self.attr_allowed, None),
            (self.dim_excluded, self.attr_excluded, None),
        ]
        responses = [
            *_persona_resolve_results(self.persona),
            _ScalarResult(dims),
            _ScalarResult([self.lv_allowed, self.lv_excluded]),
            _EMPTY,
        ]
        responses.extend([_EMPTY] * 5)
        db.execute = AsyncMock(side_effect=responses)

        model = make_model()
        objects = {
            TEST_MODEL_ID: model,
            self.h.id: self.h,
            self.attr_allowed: self.col_allowed,
            self.table_id: self.tbl,
        }

        async def _get(cls, obj_id):
            return objects.get(obj_id)
        db.get = AsyncMock(side_effect=_get)
        return db

    def _levels_db(self):
        """Mock DB for list_hierarchy_levels endpoint.

        db.execute call order:
          1. resolve_effective_persona → persona lookup
          2. _levels_for_hierarchy → level objects
          3. get_excluded_level_attribute_ids → dimension data
          4. _level_attributes_response → empty (per surviving level)
        """
        db = _base_db()
        dims = [
            (self.dim_allowed, self.attr_allowed, None),
            (self.dim_excluded, self.attr_excluded, None),
        ]
        responses = [
            *_persona_resolve_results(self.persona),
            _ScalarResult([self.lv_allowed, self.lv_excluded]),
            _ScalarResult(dims),
            _EMPTY,
        ]
        responses.extend([_EMPTY] * 5)
        db.execute = AsyncMock(side_effect=responses)

        model = make_model()
        objects = {
            TEST_MODEL_ID: model,
            self.h.id: self.h,
            self.attr_allowed: self.col_allowed,
            self.table_id: self.tbl,
        }

        async def _get(cls, obj_id):
            return objects.get(obj_id)
        db.get = AsyncMock(side_effect=_get)
        return db

    @pytest.mark.asyncio
    async def test_get_hierarchy_detail_filters_excluded_level(self, client):
        """Bug-621: get_hierarchy detail should omit levels backed by excluded dims."""
        db = self._detail_db()
        url = self.HIER_DETAIL_URL.format(hierarchy_id=self.h.id)
        with patch("src.api.hierarchies.get_tenant_db", async_gen_from(db)):
            resp = await client.get(f"{url}?persona_id={self.persona.id}")
        assert resp.status_code == 200
        data = resp.json()
        level_names = [lv["name"] for lv in data["levels"]]
        assert "Country" in level_names
        assert "City" not in level_names

    @pytest.mark.asyncio
    async def test_list_hierarchy_levels_filters_excluded_level(self, client):
        """Bug-622: list_hierarchy_levels should omit levels backed by excluded dims."""
        db = self._levels_db()
        url = self.HIER_LEVELS_URL.format(hierarchy_id=self.h.id)
        with patch("src.api.hierarchies.get_tenant_db", async_gen_from(db)):
            resp = await client.get(f"{url}?persona_id={self.persona.id}")
        assert resp.status_code == 200
        level_names = [lv["name"] for lv in resp.json()]
        assert "Country" in level_names
        assert "City" not in level_names


def test_f008_03_empty_audience_narrowing_is_not_everyone():
    """F-008-03: allow-list + empty audience must NOT match a random viewer."""
    p = _persona(
        included_measure_ids=[str(uuid.uuid4())],
        audience_roles=[],
    )
    assert is_in_audience(p, {"viewer", "member"}) is False


def test_f008_03_empty_audience_unrestricted_stays_everyone():
    """Filter-only empty everything + empty audience remains everyone."""
    p = _persona(audience_roles=[])
    assert is_in_audience(p, {"viewer"}) is True


@pytest.mark.asyncio
async def test_bug_9263_tag_restriction_query_failure_does_not_treat_personas_as_untagged():
    """Bug-9263: PersonaTagRestriction lookup failure must not silently
    treat every persona as untagged (which assigns a CLS-only empty-audience
    persona to everyone and hides the failure).
    """
    p = _persona(audience_roles=[])
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _ScalarResult([p]),
        RuntimeError("simulated timeout"),
    ])
    user = CurrentUser(
        user_id="u@test", tenant_id=TEST_TENANT, email="u@test", role="viewer",
    )
    with pytest.raises(HTTPException) as exc:
        await get_assigned_personas(db, user, TEST_MODEL_ID)
    assert exc.value.status_code == 503
    assert exc.value.detail["error_code"] == "PERSONA_ASSIGNMENT_UNAVAILABLE"
