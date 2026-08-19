"""Body-supplied foreign keys on the measures / dimensions surfaces must be
proven to belong to the PATH project + model before they are persisted.

The defect class: ``require_role`` (``src/auth/rbac.py``) reads ``project_id``
from the URL PATH and checks the caller's binding for it. It never looks at the
request BODY. A modeller bound to project A could therefore name project B's
``model_tables.id`` / ``model_columns.id`` / ``measures.id`` in the body and
bind their own entity to it — and, through ``resolve_column``, could make the
server CREATE a ``ModelColumn`` inside a foreign project's table.

Three properties are asserted for every guarded site, because each one catches
a different regression:

* DENIAL, by REASON not merely by status. A bare "assert 422" passes for the
  wrong reason whenever the pre-fix handler already rejected the payload for
  something unrelated, so every denial case pins ``detail["error_code"]`` and
  ``detail["field"]``.
* POSITIVE. A guard that denies everything is also a defect (that is what
  Bug-8864 was), and only a positive case can see it.
* NO EXISTENCE ORACLE. An id that does not exist anywhere and an id that
  exists in another project must be indistinguishable — same status, same
  error_code, same message shape. This is Bug-7253's lesson: ``glossary.py``
  had to be patched separately because its two branches answered differently
  and thereby confirmed which foreign UUIDs were real.

Near misses are included deliberately: a sibling row in the SAME project is
tested alongside a row in a foreign project, because a guard that checks only
the project (and not the model) passes the far case and fails the near one.

The session double (``tests/scope_fake_db.ScopedFakeDB``) really evaluates the
ownership predicate from the values the route bound into the statement, and
REFUSES any ownership SELECT that arrives without both a model and a project
predicate — so neutering ``scoped_select`` turns these red rather than quietly
widening them.
"""
from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest

from src.api._column_helpers import resolve_column
from .conftest import TEST_MODEL_ID, TEST_PROJECT_ID, async_gen_from
from .scope_fake_db import ScopedFakeDB

pytestmark = pytest.mark.unit

MEASURES = f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/measures"
DIMENSIONS = f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/dimensions"


def _db() -> ScopedFakeDB:
    """A store holding three worlds: the caller's model, a SIBLING model in the
    same project (the near miss), and a model in a FOREIGN project."""
    db = ScopedFakeDB(project_id=TEST_PROJECT_ID, model_id=TEST_MODEL_ID)
    db.sibling_model_id = uuid.uuid4()
    db.register_model(db.sibling_model_id, TEST_PROJECT_ID)
    db.foreign_project_id = uuid.uuid4()
    db.foreign_model_id = uuid.uuid4()
    db.register_model(db.foreign_model_id, db.foreign_project_id)

    db.own_table = db.add_table(model_id=TEST_MODEL_ID, alias="own")
    db.sibling_table = db.add_table(model_id=db.sibling_model_id, alias="sib")
    db.foreign_table = db.add_table(model_id=db.foreign_model_id, alias="fgn")
    db.own_column = db.add_column(table_id=db.own_table.id, column_name="amount")
    db.own_date_column = db.add_column(
        table_id=db.own_table.id, column_name="order_date", data_type="date",
    )
    db.foreign_column = db.add_column(
        table_id=db.foreign_table.id, column_name="secret",
    )
    return db


def _patch(module: str, db):
    return patch(f"src.api.{module}.get_tenant_db", async_gen_from(db))


def _detail(response) -> dict:
    body = response.json()["detail"]
    assert isinstance(body, dict), body
    return body


def _shape(detail: dict, echoed_id) -> dict:
    """The parts of a rejection that must NOT vary between an unknown id and a
    foreign one. The echo of the caller's own id is removed — echoing back what
    the client sent discloses nothing; a differing status/code/message would."""
    return {
        "error_code": detail["error_code"],
        "field": detail["field"],
        "message": detail["message"].replace(str(echoed_id), "<ID>"),
    }


# ===========================================================================
# The shared engine: resolve_column
# ===========================================================================


@pytest.mark.asyncio
async def test_resolve_column_refuses_to_create_a_column_in_a_foreign_table():
    """The engine defect, isolated. ``resolve_column`` CREATES a ModelColumn in
    whatever table it is handed; without the model context that is a write into
    another project's table, reachable from five call sites."""
    db = _db()

    with pytest.raises(Exception) as exc:
        await resolve_column(
            db, db.foreign_table.id, "smuggled",
            model_id=TEST_MODEL_ID, project_id=TEST_PROJECT_ID,
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["error_code"] == "TABLE_NOT_IN_MODEL"
    # The point of the guard: nothing was fabricated in the foreign table.
    assert db.added == []


@pytest.mark.asyncio
async def test_resolve_column_refuses_a_sibling_model_in_the_same_project():
    """The near miss. A guard that proved only "same project" would accept
    this, and a modeller could still bind across models."""
    db = _db()

    with pytest.raises(Exception) as exc:
        await resolve_column(
            db, db.sibling_table.id, "smuggled",
            model_id=TEST_MODEL_ID, project_id=TEST_PROJECT_ID,
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["error_code"] == "TABLE_NOT_IN_MODEL"
    assert db.added == []


@pytest.mark.asyncio
async def test_resolve_column_still_resolves_and_creates_inside_the_model():
    """Positive: the guard must not have become a deny-everything (Bug-8864)."""
    db = _db()

    existing = await resolve_column(
        db, db.own_table.id, "amount",
        model_id=TEST_MODEL_ID, project_id=TEST_PROJECT_ID,
    )
    assert existing.id == db.own_column.id

    created = await resolve_column(
        db, db.own_table.id, "brand_new", "text",
        model_id=TEST_MODEL_ID, project_id=TEST_PROJECT_ID,
    )
    assert created.model_table_id == db.own_table.id
    assert created.column_name == "brand_new"
    assert db.added == [created]


@pytest.mark.asyncio
async def test_resolve_column_hides_whether_a_foreign_table_id_exists():
    """No existence oracle: an unknown table id and a real table in another
    project must be refused identically."""
    db = _db()
    unknown_id = uuid.uuid4()

    details = []
    for candidate in (db.foreign_table.id, unknown_id):
        with pytest.raises(Exception) as exc:
            await resolve_column(
                db, candidate, "probe",
                model_id=TEST_MODEL_ID, project_id=TEST_PROJECT_ID,
            )
        assert exc.value.status_code == 422
        details.append(dict(exc.value.detail))

    assert _shape(details[0], db.foreign_table.id) == _shape(
        details[1], unknown_id
    )


@pytest.mark.asyncio
async def test_resolve_column_cannot_be_called_without_the_model_context():
    """The signature is the guard. A caller that forgets the scope must fail
    loudly at call time rather than fall through to an unscoped write."""
    db = _db()
    with pytest.raises(TypeError):
        await resolve_column(db, db.own_table.id, "amount")


# ===========================================================================
# POST /measures
# ===========================================================================


def _measure_body(**over) -> dict:
    body = {
        "name": "revenue",
        "source_table_id": None,
        "source_column_name": "amount",
        "data_type": "numeric",
        "default_agg": "sum",
    }
    body.update(over)
    return body


@pytest.mark.asyncio
@pytest.mark.parametrize("which", ["foreign_table", "sibling_table"])
async def test_create_measure_rejects_a_source_table_outside_the_model(
    client, which,
):
    db = _db()
    body = _measure_body(source_table_id=str(getattr(db, which).id))
    with _patch("measures", db):
        resp = await client.post(MEASURES, json=body)

    assert resp.status_code == 422, resp.text
    detail = _detail(resp)
    assert detail["error_code"] == "TABLE_NOT_IN_MODEL"
    assert detail["field"] == "source_table_id"
    assert db.committed is False


@pytest.mark.asyncio
async def test_create_measure_accepts_a_source_table_inside_the_model(client):
    db = _db()
    body = _measure_body(source_table_id=str(db.own_table.id))
    with _patch("measures", db):
        resp = await client.post(MEASURES, json=body)

    assert resp.status_code == 201, resp.text
    assert resp.json()["source_column_id"] == str(db.own_column.id)


@pytest.mark.asyncio
async def test_create_measure_rejects_a_foreign_semi_additive_account_column(
    client,
):
    # #10: ``by_account`` is no longer authorable, but the body-FK scope guard on
    # ``semi_additive_account_column_id`` fires unconditionally (measures.py
    # scopes it BEFORE any branch), so a foreign column id must still be refused
    # regardless of behaviour. Set the account column WITHOUT by_account.
    db = _db()
    body = _measure_body(
        source_table_id=str(db.own_table.id),
        semi_additive_account_column_id=str(db.foreign_column.id),
    )
    with _patch("measures", db):
        resp = await client.post(MEASURES, json=body)

    assert resp.status_code == 422, resp.text
    detail = _detail(resp)
    assert detail["error_code"] == "REF_NOT_IN_MODEL"
    assert detail["field"] == "semi_additive_account_column_id"
    assert db.committed is False


@pytest.mark.asyncio
async def test_create_measure_accepts_an_in_model_semi_additive_account_column(
    client,
):
    # An in-model account column id is accepted by the scope guard. (by_account
    # itself is no longer authorable — that is proven separately in the enum
    # gate; here we only pin the FK scope behaviour.)
    db = _db()
    body = _measure_body(
        source_table_id=str(db.own_table.id),
        semi_additive_account_column_id=str(db.own_date_column.id),
    )
    with _patch("measures", db):
        resp = await client.post(MEASURES, json=body)

    assert resp.status_code == 201, resp.text
    assert resp.json()["semi_additive_account_column_id"] == str(
        db.own_date_column.id
    )


@pytest.mark.asyncio
async def test_create_measure_rejects_a_foreign_date_dimension_column(client):
    db = _db()
    body = _measure_body(
        source_table_id=str(db.own_table.id),
        date_dimension_column_id=str(db.foreign_column.id),
    )
    with _patch("measures", db):
        resp = await client.post(MEASURES, json=body)

    assert resp.status_code == 422, resp.text
    detail = _detail(resp)
    assert detail["error_code"] == "REF_NOT_IN_MODEL"
    assert detail["field"] == "date_dimension_column_id"


@pytest.mark.asyncio
async def test_create_measure_accepts_an_in_model_date_dimension_column(client):
    db = _db()
    body = _measure_body(
        source_table_id=str(db.own_table.id),
        date_dimension_column_id=str(db.own_date_column.id),
    )
    with _patch("measures", db):
        resp = await client.post(MEASURES, json=body)

    assert resp.status_code == 201, resp.text
    assert resp.json()["date_dimension_column_id"] == str(db.own_date_column.id)


@pytest.mark.asyncio
async def test_create_measure_hides_whether_a_foreign_column_id_exists(client):
    """No existence oracle on the measures surface either."""
    db = _db()
    unknown_id = uuid.uuid4()
    shapes = []
    for candidate in (db.foreign_column.id, unknown_id):
        body = _measure_body(
            source_table_id=str(db.own_table.id),
            date_dimension_column_id=str(candidate),
        )
        with _patch("measures", db):
            resp = await client.post(MEASURES, json=body)
        assert resp.status_code == 422, resp.text
        shapes.append(_shape(_detail(resp), candidate))

    assert shapes[0] == shapes[1]


@pytest.mark.asyncio
async def test_create_measure_rejects_a_cross_model_measure_from_another_model(
    client,
):
    """``cross_model_source_model_id`` is validated against the project, but the
    MEASURE it names was never checked to live in that model."""
    db = _db()
    stray = db.add_measure(model_id=db.foreign_model_id, name="foreign_total")
    body = _measure_body(
        source_table_id=str(db.own_table.id),
        cross_model_source_model_id=str(db.sibling_model_id),
        cross_model_source_measure_id=str(stray.id),
    )
    with _patch("measures", db):
        resp = await client.post(MEASURES, json=body)

    assert resp.status_code == 422, resp.text
    detail = _detail(resp)
    assert detail["error_code"] == "REF_NOT_IN_MODEL"
    assert detail["field"] == "cross_model_source_measure_id"


@pytest.mark.asyncio
async def test_create_measure_accepts_a_cross_model_measure_in_that_model(
    client,
):
    db = _db()
    sibling_measure = db.add_measure(
        model_id=db.sibling_model_id, name="sibling_total",
    )
    body = _measure_body(
        source_table_id=str(db.own_table.id),
        cross_model_source_model_id=str(db.sibling_model_id),
        cross_model_source_measure_id=str(sibling_measure.id),
    )
    with _patch("measures", db):
        resp = await client.post(MEASURES, json=body)

    assert resp.status_code == 201, resp.text
    assert resp.json()["cross_model_source_measure_id"] == str(
        sibling_measure.id
    )


# ===========================================================================
# PATCH /measures/{id}
# ===========================================================================


@pytest.mark.asyncio
async def test_update_measure_rejects_a_source_table_outside_the_model(client):
    db = _db()
    measure = db.add_measure(
        model_id=TEST_MODEL_ID, name="revenue",
        source_column_id=db.own_column.id,
    )
    with _patch("measures", db):
        resp = await client.patch(
            f"{MEASURES}/{measure.id}",
            json={
                "source_table_id": str(db.foreign_table.id),
                "source_column_name": "smuggled",
            },
        )

    assert resp.status_code == 422, resp.text
    detail = _detail(resp)
    assert detail["error_code"] == "TABLE_NOT_IN_MODEL"
    assert detail["field"] == "source_table_id"
    assert db.added == []


@pytest.mark.asyncio
async def test_update_measure_rejects_a_foreign_semi_additive_account_column(
    client,
):
    db = _db()
    measure = db.add_measure(
        model_id=TEST_MODEL_ID, name="balance",
        source_column_id=db.own_column.id,
    )
    # #10: by_account is no longer authorable; the account-column FK scope guard
    # on PATCH is behaviour-independent, so send the account column alone.
    with _patch("measures", db):
        resp = await client.patch(
            f"{MEASURES}/{measure.id}",
            json={
                "semi_additive_account_column_id": str(db.foreign_column.id),
            },
        )

    assert resp.status_code == 422, resp.text
    detail = _detail(resp)
    assert detail["error_code"] == "REF_NOT_IN_MODEL"
    assert detail["field"] == "semi_additive_account_column_id"


@pytest.mark.asyncio
async def test_update_measure_rejects_a_foreign_date_dimension_column(client):
    db = _db()
    measure = db.add_measure(
        model_id=TEST_MODEL_ID, name="revenue",
        source_column_id=db.own_column.id,
    )
    with _patch("measures", db):
        resp = await client.patch(
            f"{MEASURES}/{measure.id}",
            json={"date_dimension_column_id": str(db.foreign_column.id)},
        )

    assert resp.status_code == 422, resp.text
    detail = _detail(resp)
    assert detail["error_code"] == "REF_NOT_IN_MODEL"
    assert detail["field"] == "date_dimension_column_id"


@pytest.mark.asyncio
async def test_update_measure_accepts_an_in_model_date_dimension_column(client):
    db = _db()
    measure = db.add_measure(
        model_id=TEST_MODEL_ID, name="revenue",
        source_column_id=db.own_column.id,
    )
    with _patch("measures", db):
        resp = await client.patch(
            f"{MEASURES}/{measure.id}",
            json={"date_dimension_column_id": str(db.own_date_column.id)},
        )

    assert resp.status_code == 200, resp.text
    assert measure.date_dimension_column_id == db.own_date_column.id


@pytest.mark.asyncio
async def test_update_measure_rejects_a_cross_model_measure_from_another_model(
    client,
):
    db = _db()
    measure = db.add_measure(
        model_id=TEST_MODEL_ID, name="revenue",
        source_column_id=db.own_column.id,
        cross_model_source_model_id=None,
    )
    stray = db.add_measure(model_id=db.foreign_model_id, name="foreign_total")
    with _patch("measures", db):
        resp = await client.patch(
            f"{MEASURES}/{measure.id}",
            json={
                "cross_model_source_model_id": str(db.sibling_model_id),
                "cross_model_source_measure_id": str(stray.id),
            },
        )

    assert resp.status_code == 422, resp.text
    detail = _detail(resp)
    assert detail["error_code"] == "REF_NOT_IN_MODEL"
    assert detail["field"] == "cross_model_source_measure_id"


@pytest.mark.asyncio
async def test_update_measure_rejects_a_calendar_alias_outside_the_model(client):
    db = _db()
    measure = db.add_measure(
        model_id=TEST_MODEL_ID, name="revenue",
        source_column_id=db.own_column.id,
    )
    foreign_alias = db.add_table(
        model_id=db.foreign_model_id, calendar_table_id=uuid.uuid4(),
    )
    with _patch("measures", db):
        resp = await client.patch(
            f"{MEASURES}/{measure.id}",
            json={"calendar_model_table_id": str(foreign_alias.id)},
        )

    assert resp.status_code == 422, resp.text
    detail = _detail(resp)
    assert detail["error_code"] == "CALENDAR_ALIAS_TABLE_NOT_IN_MODEL"
    assert detail["field"] == "calendar_model_table_id"


@pytest.mark.asyncio
async def test_update_measure_accepts_a_calendar_alias_inside_the_model(client):
    db = _db()
    measure = db.add_measure(
        model_id=TEST_MODEL_ID, name="revenue",
        source_column_id=db.own_column.id,
    )
    own_alias = db.add_table(
        model_id=TEST_MODEL_ID, calendar_table_id=uuid.uuid4(), alias="cal",
    )
    with _patch("measures", db):
        resp = await client.patch(
            f"{MEASURES}/{measure.id}",
            json={"calendar_model_table_id": str(own_alias.id)},
        )

    assert resp.status_code == 200, resp.text
    assert measure.calendar_model_table_id == own_alias.id


# ===========================================================================
# POST / PATCH /dimensions
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize("which", ["foreign_table", "sibling_table"])
async def test_create_dimension_rejects_a_source_table_outside_the_model(
    client, which,
):
    db = _db()
    with _patch("dimensions", db):
        resp = await client.post(
            DIMENSIONS,
            json={
                "name": "customer",
                "source_table_id": str(getattr(db, which).id),
                "source_column_name": "smuggled",
            },
        )

    assert resp.status_code == 422, resp.text
    detail = _detail(resp)
    assert detail["error_code"] == "TABLE_NOT_IN_MODEL"
    assert detail["field"] == "source_table_id"
    assert db.added == []


@pytest.mark.asyncio
async def test_create_dimension_accepts_a_source_table_inside_the_model(client):
    db = _db()
    with _patch("dimensions", db):
        resp = await client.post(
            DIMENSIONS,
            json={
                "name": "customer",
                "source_table_id": str(db.own_table.id),
                "source_column_name": "amount",
            },
        )

    assert resp.status_code == 201, resp.text
    assert resp.json()["source_column_id"] == str(db.own_column.id)


@pytest.mark.asyncio
async def test_create_dimension_hides_whether_a_foreign_table_id_exists(client):
    db = _db()
    unknown_id = uuid.uuid4()
    shapes = []
    for candidate in (db.foreign_table.id, unknown_id):
        with _patch("dimensions", db):
            resp = await client.post(
                DIMENSIONS,
                json={
                    "name": "customer",
                    "source_table_id": str(candidate),
                    "source_column_name": "probe",
                },
            )
        assert resp.status_code == 422, resp.text
        shapes.append(_shape(_detail(resp), candidate))

    assert shapes[0] == shapes[1]


@pytest.mark.asyncio
async def test_update_dimension_rejects_a_source_table_outside_the_model(
    client,
):
    db = _db()
    dim = db.add_dimension(model_id=TEST_MODEL_ID, name="customer")
    with _patch("dimensions", db):
        resp = await client.patch(
            f"{DIMENSIONS}/{dim.id}",
            json={
                "source_table_id": str(db.foreign_table.id),
                "source_column_name": "smuggled",
            },
        )

    assert resp.status_code == 422, resp.text
    detail = _detail(resp)
    assert detail["error_code"] == "TABLE_NOT_IN_MODEL"
    assert detail["field"] == "source_table_id"
    assert db.added == []


# ===========================================================================
# POST /dimensions/{id}/attribute-relationships/validate
# ===========================================================================


@pytest.mark.asyncio
async def test_validate_detail_columns_rejects_a_table_outside_the_model(
    client,
):
    """Not a persisted write, but the same unscoped body FK: the foreign
    table's column NAME would otherwise be resolved and pushed into SQL run
    against this model's source connection, and the "column not found" answer
    is itself a cross-project probe."""
    db = _db()
    dim = db.add_dimension(
        model_id=TEST_MODEL_ID, name="customer",
        source_column_id=db.own_column.id,
    )
    with _patch("dimensions", db):
        resp = await client.post(
            f"{DIMENSIONS}/{dim.id}/attribute-relationships/validate",
            json={
                "detail_columns": [
                    {"name": "secret", "table_id": str(db.foreign_table.id)},
                ]
            },
        )

    assert resp.status_code == 422, resp.text
    detail = _detail(resp)
    assert detail["error_code"] == "REFS_NOT_IN_MODEL"
    assert detail["field"] == "detail_columns[].table_id"


@pytest.mark.asyncio
async def test_validate_detail_columns_rejects_the_whole_batch_on_one_offender(
    client,
):
    """Fail closed: an in-model table alongside a foreign one must not let the
    request through with the offender silently dropped."""
    db = _db()
    dim = db.add_dimension(
        model_id=TEST_MODEL_ID, name="customer",
        source_column_id=db.own_column.id,
    )
    with _patch("dimensions", db):
        resp = await client.post(
            f"{DIMENSIONS}/{dim.id}/attribute-relationships/validate",
            json={
                "detail_columns": [
                    {"name": "amount", "table_id": str(db.own_table.id)},
                    {"name": "secret", "table_id": str(db.foreign_table.id)},
                ]
            },
        )

    assert resp.status_code == 422, resp.text
    detail = _detail(resp)
    assert detail["ids"] == [str(db.foreign_table.id)]


@pytest.mark.asyncio
async def test_validate_detail_columns_rejects_a_malformed_table_id(client):
    """``ValidateDetailColumnSpec.table_id`` is typed ``str``, so a value that
    is not a UUID at all used to reach ``UUID(...)`` and raise an unhandled
    ValueError — HTTP 500, and a response shape trivially distinguishable from
    a well-formed-but-foreign id. The guard now runs on the raw strings and
    answers the same 422 for both."""
    db = _db()
    dim = db.add_dimension(
        model_id=TEST_MODEL_ID, name="customer",
        source_column_id=db.own_column.id,
    )
    with _patch("dimensions", db):
        resp = await client.post(
            f"{DIMENSIONS}/{dim.id}/attribute-relationships/validate",
            json={"detail_columns": [{"name": "x", "table_id": "not-a-uuid"}]},
        )

    assert resp.status_code == 422, resp.text
    detail = _detail(resp)
    assert detail["error_code"] == "REFS_NOT_IN_MODEL"
    assert detail["field"] == "detail_columns[].table_id"
