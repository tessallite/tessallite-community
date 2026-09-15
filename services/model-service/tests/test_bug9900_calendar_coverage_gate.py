"""Bug-9900 — the calendar coverage probe is a MODELLING surface, and it may
only probe the model's own tables.

Rule-4 wave 0b, following the Bug-9896 precedent (audit row A38, decision
4.4c). ``check_calendar_coverage`` builds ``SELECT MIN(col), MAX(col) FROM
<table>`` and runs it through the query-router ``/introspect/batch`` route,
which applies NO persona, NO CLS and NO RLS. Two defects, both fixed here:

  1. the route was ``require_role("viewer")``, so any project viewer could
     drive a raw MIN/MAX read of the source; and
  2. ``fact_table`` / ``fact_date_column`` were FREE-FORM query parameters
     interpolated into that SQL, so the probe could be aimed at any table and
     column the source connection could reach — including tables outside the
     model. Role alone does not fix that: a modeller must not be able to read
     arbitrary source tables either.

These tests drive the REAL route so the whole dependency chain runs
(token -> forbid_embed_user -> require_role -> handler):

  * a project ``viewer`` binding is REJECTED with 403,
  * a project ``modeler`` binding probing a table OUTSIDE the model is
    REJECTED with 422 and a clear message,
  * a project ``modeler`` binding probing a column that is not in the model is
    REJECTED with 422,
  * a project ``modeler`` binding probing the model's own fact table and date
    column is ADMITTED and gets min/max (200),
  * an embed token is REJECTED (``forbid_embed_user`` stays),
  * the route's declared minimum role is the shared modeller constant, so a
    silent revert to "viewer" fails here too.

Run from tessallite/services/model-service/:
    pytest tests/test_bug9900_calendar_coverage_gate.py
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from shared.auth.roles import PROJECT_MODELER_ROLE
from src.api.calendar import COVERAGE_MIN_ROLE
from src.auth.middleware import CurrentEmbedUser, CurrentUser, get_current_user
from src.main import app

pytestmark = pytest.mark.unit

_PROJECT_ID = uuid.uuid4()
_MODEL_ID = uuid.uuid4()
_SOURCE_ID = uuid.uuid4()
_CALENDAR_ID = uuid.uuid4()
_FACT_TABLE_ID = uuid.uuid4()
_TENANT = "test-tenant"
_USER_ID = "u@acme.test"

_MODEL_FACT_TABLE = "public.sales_fact"
_MODEL_FACT_COLUMN = "order_date"

_URL = (
    f"/api/v1/projects/{_PROJECT_ID}/models/{_MODEL_ID}"
    f"/sources/{_SOURCE_ID}/calendars/{_CALENDAR_ID}/coverage"
)


def _user() -> CurrentUser:
    return CurrentUser(
        user_id=_USER_ID, tenant_id=_TENANT, email=_USER_ID, role="member",
    )


def _embed() -> CurrentEmbedUser:
    return CurrentEmbedUser(
        user_id="embed@acme.test", tenant_id=_TENANT, email="embed@acme.test",
    )


def _binding(role: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        user_identity=_USER_ID,
        project_id=_PROJECT_ID,
        model_id=None,
        role=role,
    )


def _rbac_db(binding_role: str | None) -> MagicMock:
    """Mock tenant DB answering require_role's binding lookups."""
    bindings = [_binding(binding_role)] if binding_role else []
    db = MagicMock()

    async def _execute(stmt):
        result = MagicMock()
        text = str(stmt)
        is_existence_probe = "user_identity" not in text
        result.scalar_one_or_none.return_value = bindings[0] if bindings else None
        result.scalars.return_value.all.return_value = bindings
        if is_existence_probe:
            result.first.return_value = (bindings[0],) if bindings else None
        return result

    db.execute = AsyncMock(side_effect=_execute)
    return db


def _handler_db() -> MagicMock:
    """Mock tenant DB for the handler body.

    ``db.get`` resolves the CalendarTable; ``db.execute`` answers the two
    model-boundness lookups added by Bug-9900 (ModelTable rows for this model
    and source, then that table's ModelColumn names).
    """
    db = MagicMock()
    calendar = types.SimpleNamespace(
        id=_CALENDAR_ID,
        data_source_id=_SOURCE_ID,
        table_name="public.dim_calendar",
        date_column="date_key",
    )
    db.get = AsyncMock(return_value=calendar)

    fact_table = types.SimpleNamespace(
        id=_FACT_TABLE_ID,
        model_id=_MODEL_ID,
        source_id=_SOURCE_ID,
        physical_name=_MODEL_FACT_TABLE,
    )

    async def _execute(stmt):
        result = MagicMock()
        text = str(stmt)
        if "model_columns" in text:
            result.scalars.return_value.all.return_value = [
                _MODEL_FACT_COLUMN, "amount",
            ]
        else:
            result.scalars.return_value.all.return_value = [fact_table]
        return result

    db.execute = AsyncMock(side_effect=_execute)
    return db


_MINMAX = {
    "cal": ([{"lo": "2020-01-01", "hi": "2030-12-31"}], ["lo", "hi"], None),
    "fact": ([{"lo": "2021-01-01", "hi": "2021-12-31"}], ["lo", "hi"], None),
    "gap": ([{"cnt": 365}], ["cnt"], None),
}


def _patches(rbac_db_gen, handler_db_gen, batch, source, connection):
    return (
        patch("src.auth.rbac.get_tenant_db", rbac_db_gen),
        patch("src.api.calendar.get_tenant_db", handler_db_gen),
        patch(
            "src.api.calendar._ensure_model_in_project",
            AsyncMock(return_value=None),
        ),
        patch(
            "src.api.calendar._load_source_with_connection",
            AsyncMock(return_value=(source, connection)),
        ),
        patch(
            "src.api.calendar._introspect_batch_via_router",
            AsyncMock(side_effect=batch),
        ),
    )


async def _call(
    user,
    *,
    binding_role: str | None,
    fact_table: str = _MODEL_FACT_TABLE,
    fact_date_column: str = _MODEL_FACT_COLUMN,
    captured: list | None = None,
) -> httpx.Response:
    """GET the real coverage route with *user* holding *binding_role*.

    Everything BELOW the role gate and the model-boundness guard is stubbed so
    an admitted, in-model request reaches a clean 200 — a 403 is unambiguously
    the role gate and a 422 unambiguously the boundness guard.
    """
    rbac_db = _rbac_db(binding_role)
    handler_db = _handler_db()

    async def _rbac_db_gen(*a, **kw):
        yield rbac_db

    async def _handler_db_gen(*a, **kw):
        yield handler_db

    async def _batch(model_id, queries, bearer, *a, **kw):
        if captured is not None:
            captured.append(list(queries))
        return {key: _MINMAX[key] for key, _sql in queries}

    source = types.SimpleNamespace(id=_SOURCE_ID, config={}, default_schema=None)
    connection = types.SimpleNamespace(connection_type="postgresql", config={})

    app.dependency_overrides[get_current_user] = lambda: user
    try:
        p1, p2, p3, p4, p5 = _patches(
            _rbac_db_gen, _handler_db_gen, _batch, source, connection,
        )
        with p1, p2, p3, p4, p5:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
            ) as ac:
                return await ac.get(
                    _URL,
                    params={
                        "fact_table": fact_table,
                        "fact_date_column": fact_date_column,
                    },
                    headers={"Authorization": "Bearer test-token"},
                )
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_bug9900_coverage_min_role_is_modeller():
    """The route's declared gate is the shared modeller constant.

    Fails on the pre-fix code, where the dependency was require_role("viewer").
    """
    assert COVERAGE_MIN_ROLE == PROJECT_MODELER_ROLE


@pytest.mark.asyncio
async def test_bug9900_viewer_binding_is_denied():
    """A project viewer must NOT be able to drive a raw MIN/MAX source probe."""
    resp = await _call(_user(), binding_role="viewer")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_bug9900_model_viewer_binding_is_denied():
    resp = await _call(_user(), binding_role="model_viewer")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_bug9900_modeller_probing_table_outside_the_model_is_rejected():
    """The probe may not be aimed at a table that is not part of the model."""
    resp = await _call(
        _user(), binding_role="modeler", fact_table="public.user_credentials",
    )
    assert resp.status_code == 422
    assert "not a table of this model" in str(resp.json()["detail"])


@pytest.mark.asyncio
async def test_bug9900_modeller_probing_column_outside_the_model_is_rejected():
    """The probe may not be aimed at a column the model does not carry."""
    resp = await _call(_user(), binding_role="modeler", fact_date_column="ssn")
    assert resp.status_code == 422
    assert "not a column of" in str(resp.json()["detail"])


@pytest.mark.asyncio
async def test_bug9900_modeller_probing_a_model_bound_table_works():
    resp = await _call(_user(), binding_role="modeler")
    assert resp.status_code == 200
    body = resp.json()
    assert body["calendar_min"] == "2020-01-01"
    assert body["fact_min"] == "2021-01-01"
    assert body["fact_max"] == "2021-12-31"


@pytest.mark.asyncio
async def test_bug9900_admin_binding_is_admitted():
    resp = await _call(_user(), binding_role="admin")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_bug9900_unbound_user_is_denied():
    resp = await _call(_user(), binding_role=None)
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_bug9900_embed_token_is_forbidden():
    """forbid_embed_user stays: an embed token never reaches the probe."""
    resp = await _call(_embed(), binding_role="modeler")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_bug9900_probe_sql_uses_the_stored_physical_name():
    """The SQL is built from the STORED physical name, not the request string.

    A caller passing the bare leaf name still probes the model's stored
    ``public.sales_fact`` — the request string never reaches the SQL builder.
    """
    captured: list[list[tuple[str, str]]] = []
    resp = await _call(
        _user(),
        binding_role="modeler",
        fact_table="sales_fact",
        captured=captured,
    )
    assert resp.status_code == 200
    fact_sql = [sql for key, sql in captured[0] if key == "fact"][0]
    assert '"public"."sales_fact"' in fact_sql
