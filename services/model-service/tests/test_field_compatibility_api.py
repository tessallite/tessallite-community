from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from .result_fakes import FakeScalarResult

from src.auth.middleware import CurrentEmbedUser, CurrentUser, get_current_user
from src.main import app
from shared.db.models import Model, ModelVersion


pytestmark = pytest.mark.unit


PROJECT_ID = uuid.uuid4()
MODEL_ID = uuid.uuid4()


class _ScalarResult:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return FakeScalarResult(self._items)

    def all(self):
        return self._items


async def _yield_db(db):
    yield db


def _field(field_id, name, display_name, column_id):
    return types.SimpleNamespace(
        id=field_id,
        name=name,
        display_name=display_name,
        source_column_id=column_id,
        user_defined_attribute_id=None,
        measure_type="standard",
        expression=None,
        is_additive=True,
        semi_additive_behavior=None,
        variant_kind=None,
        calc_agg_mode=None,
        default_agg="sum",
    )


def _fixture_rows():
    fact_id = uuid.uuid4()
    school_id = uuid.uuid4()
    teacher_id = uuid.uuid4()
    measure_col_id = uuid.uuid4()
    school_col_id = uuid.uuid4()
    teacher_col_id = uuid.uuid4()
    measure_id = uuid.uuid4()
    school_dim_id = uuid.uuid4()
    teacher_dim_id = uuid.uuid4()
    tables = [
        types.SimpleNamespace(id=fact_id, table_type="fact"),
        types.SimpleNamespace(id=school_id, table_type="dim_aggregate"),
        types.SimpleNamespace(id=teacher_id, table_type="dim_aggregate"),
    ]
    columns = [
        types.SimpleNamespace(id=measure_col_id, model_table_id=fact_id, is_hidden=False),
        types.SimpleNamespace(id=school_col_id, model_table_id=school_id, is_hidden=False),
        types.SimpleNamespace(id=teacher_col_id, model_table_id=teacher_id, is_hidden=True),
    ]
    joins = [
        types.SimpleNamespace(
            id=uuid.uuid4(),
            left_table_id=fact_id,
            right_table_id=school_id,
            join_type="many_to_one",
        )
    ]
    measures = [
        _field(measure_id, "average_student_age", "Average Student Age", measure_col_id)
    ]
    dimensions = [
        _field(school_dim_id, "school", "School", school_col_id),
        _field(teacher_dim_id, "teacher_name", "Teacher Name", teacher_col_id),
    ]
    return {
        "tables": tables,
        "columns": columns,
        "joins": joins,
        "dimensions": dimensions,
        "measures": measures,
        "udas": [],
        "aggregates": [],
        "aggregate_columns": [],
        "measure_id": measure_id,
        "school_dim_id": school_dim_id,
        "teacher_dim_id": teacher_dim_id,
    }


def _snapshot_from_rows(rows, *, joins=None):
    def _dump(row):
        return dict(vars(row))

    return {
        "tables": [_dump(row) for row in rows["tables"]],
        "columns": [_dump(row) for row in rows["columns"]],
        "joins": [_dump(row) for row in (joins if joins is not None else rows["joins"])],
        "dimensions": [_dump(row) for row in rows["dimensions"]],
        "measures": [_dump(row) for row in rows["measures"]],
        "user_defined_attributes": [_dump(row) for row in rows["udas"]],
        "aggregates": [
            {**_dump(aggregate), "columns": []}
            for aggregate in rows["aggregates"]
        ],
    }


def _mock_db(rows, *, model=None, version=None):
    db = AsyncMock()
    db.add = lambda _obj: None
    model = model or types.SimpleNamespace(
            id=MODEL_ID,
            project_id=PROJECT_ID,
            deployed_version_id=None,
        )

    async def _get(model_cls, row_id):
        if model_cls is Model and row_id == MODEL_ID:
            return model
        if model_cls is ModelVersion and version is not None and row_id == version.id:
            return version
        return None

    db.get = AsyncMock(side_effect=_get)
    sequence = [
        rows["tables"],
        rows["columns"],
        rows["joins"],
        rows["dimensions"],
        rows["measures"],
        rows["udas"],
        rows["aggregates"],
    ]

    async def _execute(_stmt):
        items = sequence.pop(0) if sequence else rows["aggregate_columns"]
        return _ScalarResult(items)

    db.execute = _execute
    return db


@pytest.mark.asyncio
async def test_field_compatibility_endpoint_returns_business_messages():
    rows = _fixture_rows()
    db = _mock_db(rows)
    user = CurrentUser(
        user_id="modeler@example.com",
        tenant_id="tenant",
        email="modeler@example.com",
        role="modeler",
    )
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            with (
                patch(
                    "src.api.field_compatibility.get_tenant_db",
                    lambda tenant_id: _yield_db(db),
                ),
            ):
                response = await client.get(
                    f"/api/v1/projects/{PROJECT_ID}/models/{MODEL_ID}/field-compatibility",
                    params=[
                        ("measure_ids", str(rows["measure_id"])),
                        ("dimension_ids", str(rows["school_dim_id"])),
                        ("dimension_ids", str(rows["teacher_dim_id"])),
                    ],
                )
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert response.status_code == 200
    body = response.json()
    measure = body["measures"][str(rows["measure_id"])]
    hidden_issue = measure["incompatible_dimensions"][str(rows["teacher_dim_id"])]
    assert hidden_issue["code"] == "HIDDEN_FIELD_UNAVAILABLE"
    assert hidden_issue["compatible_dimension_names"] == ["School"]
    assert "Teacher Name" not in hidden_issue["message"]
    assert "join path" not in hidden_issue["message"].lower()


@pytest.mark.asyncio
async def test_field_compatibility_endpoint_uses_deployed_snapshot_not_live_draft():
    rows = _fixture_rows()
    teacher_column = next(
        col
        for col in rows["columns"]
        if col.id == rows["dimensions"][1].source_column_id
    )
    teacher_column.is_hidden = False
    live_teacher_join = types.SimpleNamespace(
        id=uuid.uuid4(),
        left_table_id=rows["tables"][0].id,
        right_table_id=rows["tables"][2].id,
        join_type="many_to_one",
    )
    live_rows = {**rows, "joins": [*rows["joins"], live_teacher_join]}
    version_id = uuid.uuid4()
    version = types.SimpleNamespace(
        id=version_id,
        model_id=MODEL_ID,
        snapshot_json=_snapshot_from_rows(rows),
    )
    model = types.SimpleNamespace(
        id=MODEL_ID,
        project_id=PROJECT_ID,
        deployed_version_id=version_id,
    )
    db = _mock_db(live_rows, model=model, version=version)
    user = CurrentUser(
        user_id="modeler@example.com",
        tenant_id="tenant",
        email="modeler@example.com",
        role="modeler",
    )
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            with (
                patch(
                    "src.api.field_compatibility.get_tenant_db",
                    lambda tenant_id: _yield_db(db),
                ),
            ):
                response = await client.get(
                    f"/api/v1/projects/{PROJECT_ID}/models/{MODEL_ID}/field-compatibility",
                    params=[
                        ("measure_ids", str(rows["measure_id"])),
                        ("dimension_ids", str(rows["teacher_dim_id"])),
                    ],
                )
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert response.status_code == 200
    body = response.json()
    assert body["version_id"] == str(version_id)
    measure = body["measures"][str(rows["measure_id"])]
    issue = measure["incompatible_dimensions"][str(rows["teacher_dim_id"])]
    assert issue["code"] == "NO_JOIN_PATH"


@pytest.mark.asyncio
async def test_field_compatibility_endpoint_include_hidden_does_not_reveal_for_plain_viewer():
    rows = _fixture_rows()
    db = _mock_db(rows)
    user = CurrentUser(
        user_id="viewer@example.com",
        tenant_id="tenant",
        email="viewer@example.com",
        role="viewer",
    )
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            with (
                patch(
                    "src.api.field_compatibility.get_tenant_db",
                    lambda tenant_id: _yield_db(db),
                ),
                patch(
                    "src.api.field_compatibility.resolve_effective_persona",
                    AsyncMock(return_value=None),
                ),
            ):
                response = await client.get(
                    f"/api/v1/projects/{PROJECT_ID}/models/{MODEL_ID}/field-compatibility",
                    params=[
                        ("include_hidden", "true"),
                        ("measure_ids", str(rows["measure_id"])),
                        ("dimension_ids", str(rows["teacher_dim_id"])),
                    ],
                )
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert response.status_code == 200
    body = response.json()
    measure = body["measures"][str(rows["measure_id"])]
    issue = measure["incompatible_dimensions"][str(rows["teacher_dim_id"])]
    assert issue["code"] == "HIDDEN_FIELD_UNAVAILABLE"
    assert measure["compatible_dimension_ids"] == [str(rows["school_dim_id"])]
    assert "Teacher Name" not in issue["message"]
    assert issue["compatible_dimension_names"] == ["School"]


@pytest.mark.asyncio
async def test_field_compatibility_endpoint_hidden_persona_must_opt_in_to_hidden_fields():
    rows = _fixture_rows()
    rows["joins"].append(
        types.SimpleNamespace(
            id=uuid.uuid4(),
            left_table_id=rows["tables"][0].id,
            right_table_id=rows["tables"][2].id,
            join_type="many_to_one",
        )
    )
    persona = types.SimpleNamespace(
        id=uuid.uuid4(),
        included_measure_ids=[],
        included_dimension_ids=[],
        includes_hidden_columns=True,
    )
    user = CurrentUser(
        user_id="technical@example.com",
        tenant_id="tenant",
        email="technical@example.com",
        role="viewer",
    )

    async def _request(include_hidden: bool):
        db = _mock_db(rows)
        app.dependency_overrides[get_current_user] = lambda: user
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
            ) as client:
                with (
                    patch(
                        "src.api.field_compatibility.get_tenant_db",
                        lambda tenant_id: _yield_db(db),
                    ),
                    patch(
                        "src.api.field_compatibility.resolve_effective_persona",
                        AsyncMock(return_value=persona),
                    ),
                    patch(
                        "src.api.field_compatibility._restricted_column_ids",
                        AsyncMock(return_value=set()),
                    ),
                ):
                    return await client.get(
                        f"/api/v1/projects/{PROJECT_ID}/models/{MODEL_ID}/field-compatibility",
                        params=[
                            ("include_hidden", "true" if include_hidden else "false"),
                            ("measure_ids", str(rows["measure_id"])),
                            ("dimension_ids", str(rows["teacher_dim_id"])),
                        ],
                    )
        finally:
            app.dependency_overrides.pop(get_current_user, None)

    default_response = await _request(False)
    assert default_response.status_code == 200
    default_measure = default_response.json()["measures"][str(rows["measure_id"])]
    default_issue = default_measure["incompatible_dimensions"][str(rows["teacher_dim_id"])]
    assert default_issue["code"] == "HIDDEN_FIELD_UNAVAILABLE"
    assert "Teacher Name" not in default_issue["message"]

    opted_in_response = await _request(True)
    assert opted_in_response.status_code == 200
    opted_in_measure = opted_in_response.json()["measures"][str(rows["measure_id"])]
    assert str(rows["teacher_dim_id"]) in opted_in_measure["compatible_dimension_ids"]
    assert opted_in_measure["incompatible_dimensions"] == {}


@pytest.mark.asyncio
async def test_field_compatibility_endpoint_enforces_embed_model_scope():
    user = CurrentEmbedUser(
        user_id="embed",
        tenant_id="tenant",
        email="embed@example.com",
        model_ids=[str(uuid.uuid4())],
    )
    app.dependency_overrides[get_current_user] = lambda: user
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            response = await client.get(
                f"/api/v1/projects/{PROJECT_ID}/models/{MODEL_ID}/field-compatibility"
            )
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert response.status_code == 403
