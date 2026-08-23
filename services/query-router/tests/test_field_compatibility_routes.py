from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from src.api.routes import ExecuteRequest, _handle_execute, _handle_explain, _handle_validate
from src.ir.logical_query import BoundQuery, LogicalQuery
from src.semantic.snapshot_resolver import DeployedShape


@dataclass
class _Table:
    id: UUID
    name: str
    table_type: str = "dim_aggregate"


@dataclass
class _Column:
    id: UUID
    model_table_id: UUID
    column_name: str
    is_hidden: bool = False


@dataclass
class _Join:
    id: UUID
    left_table_id: UUID
    right_table_id: UUID
    join_type: str = "many_to_one"


@dataclass
class _Field:
    id: UUID
    name: str
    display_name: str
    source_column_id: UUID | None = None
    user_defined_attribute_id: UUID | None = None
    measure_type: str = "standard"
    expression: str | None = None
    is_additive: bool = True
    default_agg: str = "sum"


def _fixture():
    model_id = uuid4()
    version_id = uuid4()
    fact = _Table(uuid4(), "fact_students", "fact")
    school = _Table(uuid4(), "dim_school")
    teacher = _Table(uuid4(), "dim_teacher")
    age_col = _Column(uuid4(), fact.id, "student_age")
    school_col = _Column(uuid4(), school.id, "school_name")
    teacher_col = _Column(uuid4(), teacher.id, "teacher_name")
    measure = _Field(
        uuid4(),
        "average_student_age",
        "Average Student Age",
        age_col.id,
        default_agg="avg",
    )
    school_dim = _Field(uuid4(), "school", "School", school_col.id)
    teacher_dim = _Field(uuid4(), "teacher_name", "Teacher Name", teacher_col.id)
    model = SimpleNamespace(
        id=model_id,
        deployed_version_id=version_id,
        display_name="Student Model",
        slug="student_model",
    )
    logical_query = LogicalQuery(
        model_id=str(model_id),
        protocol="jdbc",
        raw_query=(
            "SELECT teacher_name, AVG(average_student_age) "
            "FROM student_model GROUP BY teacher_name"
        ),
        requested_measures=[measure.name],
        requested_dimensions=[teacher_dim.name],
        filters=[],
        grain=[teacher_dim.name],
        order_by=[],
        limit=None,
        offset=None,
        query_fingerprint="field-compatibility-test",
    )
    bound = BoundQuery(
        logical_query=logical_query,
        model=model,
        resolved_measures=[measure],
        resolved_dimensions=[teacher_dim],
        resolved_filters=[],
        resolved_dimensions_by_name={teacher_dim.name: teacher_dim},
    )
    metadata = (
        [measure],
        [school_dim, teacher_dim],
        [fact, school, teacher],
        [age_col, school_col, teacher_col],
        [_Join(uuid4(), fact.id, school.id)],
        [],
        [],
        [],
    )
    return logical_query, bound, metadata


def _request(model_id: UUID) -> ExecuteRequest:
    return ExecuteRequest(
        model_id=str(model_id),
        raw_query=(
            "SELECT teacher_name, AVG(average_student_age) "
            "FROM student_model GROUP BY teacher_name"
        ),
        protocol="jdbc",
    )


def _patch_parse_bind_and_metadata(monkeypatch, logical_query, bound, metadata):
    from src.api import routes as routes_mod

    monkeypatch.setattr(routes_mod, "_parse", lambda body: logical_query)

    async def _bind(*args, **kwargs):
        return bound

    monkeypatch.setattr(routes_mod, "bind_query_to_model", _bind)

    async def _metadata(*args, **kwargs):
        return metadata

    monkeypatch.setattr(routes_mod, "_load_field_compatibility_metadata", _metadata)

    async def _persona_gate(*args, **kwargs):
        return None

    monkeypatch.setattr(routes_mod, "apply_persona_gate", _persona_gate)


class _EmptyResult:
    def __init__(self, rows=None):
        self._rows = list(rows or [])

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _EmptyDB:
    async def execute(self, *args, **kwargs):
        return _EmptyResult()


class _QueuedDB:
    def __init__(self, *scalar_results):
        self._scalar_results = list(scalar_results)

    async def execute(self, *args, **kwargs):
        rows = self._scalar_results.pop(0) if self._scalar_results else []
        return _EmptyResult(rows)


def _patch_real_binder_metadata(monkeypatch, bound, metadata):
    from src.api import routes as routes_mod
    from src.semantic import binder as binder_mod

    async def _load_model(*args, **kwargs):
        return bound.model

    async def _deployed_shape(*args, **kwargs):
        return DeployedShape(
            measures=metadata[0],
            dimensions=metadata[1],
            hidden_column_ids=set(),
            physical_columns_all={
                getattr(column, "column_name", "").lower()
                for column in metadata[3]
                if getattr(column, "column_name", None)
            },
            physical_columns_visible={
                getattr(column, "column_name", "").lower()
                for column in metadata[3]
                if getattr(column, "column_name", None)
                and not getattr(column, "is_hidden", False)
            },
            hierarchy_rows=[],
        )

    async def _metadata(*args, **kwargs):
        return metadata

    monkeypatch.setattr(binder_mod, "_load_model", _load_model)
    monkeypatch.setattr(binder_mod, "resolve_deployed_shape", _deployed_shape)
    monkeypatch.setattr(routes_mod, "_load_field_compatibility_metadata", _metadata)


@pytest.mark.asyncio
async def test_validate_returns_structured_field_compatibility_for_freeform_sql(monkeypatch):
    logical_query, bound, metadata = _fixture()
    _patch_parse_bind_and_metadata(monkeypatch, logical_query, bound, metadata)

    response = await _handle_validate(_request(bound.model.id), SimpleNamespace())

    assert response.ok is False
    assert response.field_compatibility is not None
    assert response.field_compatibility.status == "incompatible"
    issue = response.field_compatibility.issues[0]
    assert issue.code == "NO_JOIN_PATH"
    assert issue.measure_name == "average_student_age"
    assert issue.dimension_name == "teacher_name"
    assert issue.compatible_dimension_names == ["School"]
    assert "aggregation path" in issue.message
    assert "join path" not in issue.message.lower()
    assert response.errors == [issue.message]


@pytest.mark.asyncio
async def test_validate_skips_field_compatibility_for_force_route_raw(monkeypatch):
    logical_query, bound, metadata = _fixture()
    _patch_parse_bind_and_metadata(monkeypatch, logical_query, bound, metadata)

    req = _request(bound.model.id)
    req.force_route = "raw"
    response = await _handle_validate(req, _EmptyDB())

    assert response.ok is True
    assert response.field_compatibility is None


@pytest.mark.asyncio
async def test_validate_warns_without_blocking_for_ambiguous_aggregation_path(monkeypatch):
    logical_query, bound, metadata = _fixture()
    measures, dimensions, tables, columns, joins, udas, aggregates, aggregate_columns = metadata
    fact = tables[0]
    school = tables[1]
    teacher_dim = dimensions[1]
    bridge = _Table(uuid4(), "bridge_school")
    tables.append(bridge)
    joins.extend(
        [
            _Join(uuid4(), fact.id, bridge.id),
            _Join(uuid4(), bridge.id, school.id),
        ]
    )
    bound.resolved_dimensions = [dimensions[0]]
    bound.resolved_dimensions_by_name = {dimensions[0].name: dimensions[0]}
    logical_query.requested_dimensions = [dimensions[0].name]
    logical_query.grain = [dimensions[0].name]
    _patch_parse_bind_and_metadata(
        monkeypatch,
        logical_query,
        bound,
        (measures, dimensions, tables, columns, joins, udas, aggregates, aggregate_columns),
    )

    response = await _handle_validate(_request(bound.model.id), _EmptyDB())

    assert response.ok is True
    assert response.errors == []
    assert response.field_compatibility is not None
    assert response.field_compatibility.status == "compatible"
    issue = response.field_compatibility.issues[0]
    assert issue.code == "AMBIGUOUS_JOIN_PATH"
    assert issue.severity == "warning"
    assert issue.message in response.warnings
    assert "teacher" not in str(response.model_dump()).lower()
    assert teacher_dim.name == "teacher_name"


@pytest.mark.asyncio
async def test_validate_returns_security_aware_compatibility_for_persona_denial(monkeypatch):
    logical_query, bound, metadata = _fixture()
    _patch_parse_bind_and_metadata(monkeypatch, logical_query, bound, metadata)
    school_dim = metadata[1][0]
    persona = SimpleNamespace(
        id=uuid4(),
        model_id=bound.model.id,
        name="School analyst",
        included_measure_ids=[],
        included_dimension_ids=[str(school_dim.id)],
        included_hierarchy_ids=[],
        default_filters={},
    )

    response = await _handle_validate(
        _request(bound.model.id),
        _EmptyDB(),
        persona_id=str(persona.id),
        persona=persona,
    )

    assert response.ok is False
    assert response.field_compatibility is not None
    issue = response.field_compatibility.issues[0]
    assert issue.code == "PERSONA_FIELD_UNAVAILABLE"
    assert issue.dimension_name is None
    assert "Teacher Name" not in issue.message
    assert issue.compatible_dimension_names == ["School"]
    assert response.requested_measures == []
    assert response.requested_dimensions == []
    serialized = str(response.model_dump())
    assert "Teacher Name" not in serialized
    assert "teacher_name" not in serialized


@pytest.mark.asyncio
async def test_validate_redacts_cls_restricted_dimension_from_entire_response(monkeypatch):
    logical_query, bound, metadata = _fixture()
    _patch_parse_bind_and_metadata(monkeypatch, logical_query, bound, metadata)
    from src.api import routes as routes_mod

    async def _persona_gate_allows_select_list(*args, **kwargs):
        return None

    monkeypatch.setattr(routes_mod, "enforce_persona_gate", _persona_gate_allows_select_list)
    school_dim = metadata[1][0]
    teacher_dim = metadata[1][1]
    teacher_col = metadata[3][2]
    restricted_tag_id = uuid4()
    persona = SimpleNamespace(
        id=uuid4(),
        model_id=bound.model.id,
        name="Teacher-safe analyst",
        included_measure_ids=[],
        included_dimension_ids=[str(school_dim.id), str(teacher_dim.id)],
        included_hierarchy_ids=[],
        default_filters={},
    )

    response = await _handle_validate(
        _request(bound.model.id),
        _QueuedDB([restricted_tag_id], [teacher_col.id]),
        persona_id=str(persona.id),
        persona=persona,
    )

    assert response.ok is False
    assert response.requested_measures == []
    assert response.requested_dimensions == []
    assert response.field_compatibility is not None
    issue = response.field_compatibility.issues[0]
    assert issue.code == "PERSONA_FIELD_UNAVAILABLE"
    assert issue.dimension_name is None
    assert issue.compatible_dimension_names == ["School"]
    serialized = str(response.model_dump())
    assert "Teacher Name" not in serialized
    assert "teacher_name" not in serialized


@pytest.mark.asyncio
async def test_validate_uses_real_parser_and_binder_for_quoted_builder_sql(monkeypatch):
    _logical_query, bound, metadata = _fixture()
    _patch_real_binder_metadata(monkeypatch, bound, metadata)
    request = ExecuteRequest(
        model_id=str(bound.model.id),
        raw_query=(
            'SELECT "teacher_name", AVG("average_student_age") '
            'FROM "student_model" GROUP BY "teacher_name"'
        ),
        protocol="jdbc",
    )

    response = await _handle_validate(request, _EmptyDB())

    assert response.ok is False
    assert response.requested_measures == ["average_student_age"]
    assert response.requested_dimensions == ["teacher_name"]
    assert response.field_compatibility is not None
    issue = response.field_compatibility.issues[0]
    assert issue.code == "NO_JOIN_PATH"
    assert issue.compatible_dimension_names == ["School"]
    assert "aggregation path" in issue.message


@pytest.mark.asyncio
async def test_validate_complex_sql_returns_not_analyzed_warning_without_false_pass(monkeypatch):
    _logical_query, bound, metadata = _fixture()
    _patch_real_binder_metadata(monkeypatch, bound, metadata)
    request = ExecuteRequest(
        model_id=str(bound.model.id),
        raw_query=(
            "WITH base AS (SELECT * FROM student_model) "
            "SELECT * FROM base"
        ),
        protocol="jdbc",
    )

    response = await _handle_validate(request, _EmptyDB())

    assert response.ok is True
    assert response.field_compatibility is not None
    assert response.field_compatibility.status == "not_analyzed"
    issue = response.field_compatibility.issues[0]
    assert issue.code == "SEMANTIC_COMPATIBILITY_NOT_ANALYZED"
    assert issue.severity == "warning"
    assert issue.message in response.warnings


@pytest.mark.asyncio
async def test_execute_skips_field_compatibility_for_force_route_raw(monkeypatch):
    logical_query, bound, metadata = _fixture()
    _patch_parse_bind_and_metadata(monkeypatch, logical_query, bound, metadata)

    from src.api import routes as routes_mod

    route_called = False

    async def _route_sentinel(*args, **kwargs):
        nonlocal route_called
        route_called = True
        raise HTTPException(status_code=422, detail="sentinel: routing reached")

    monkeypatch.setattr(routes_mod, "route_query", _route_sentinel)

    req = _request(bound.model.id)
    req.force_route = "raw"

    with pytest.raises(HTTPException) as exc_info:
        await _handle_execute(
            req,
            SimpleNamespace(),
            user_identity="analyst@example.com",
            tenant_id="tenant-1",
        )

    assert route_called, "route_query must be reached when force_route=raw"
    assert exc_info.value.detail == "sentinel: routing reached"


@pytest.mark.asyncio
async def test_explain_skips_field_compatibility_for_force_route_raw(monkeypatch):
    logical_query, bound, metadata = _fixture()
    _patch_parse_bind_and_metadata(monkeypatch, logical_query, bound, metadata)

    from src.api import routes as routes_mod

    route_called = False

    async def _route_sentinel(*args, **kwargs):
        nonlocal route_called
        route_called = True
        raise HTTPException(status_code=422, detail="sentinel: routing reached")

    monkeypatch.setattr(routes_mod, "route_query", _route_sentinel)

    req = _request(bound.model.id)
    req.force_route = "raw"

    with pytest.raises(HTTPException) as exc_info:
        await _handle_explain(req, SimpleNamespace())

    assert route_called, "route_query must be reached when force_route=raw"
    assert exc_info.value.detail == "sentinel: routing reached"


@pytest.mark.asyncio
async def test_execute_blocks_incompatible_fields_before_routing(monkeypatch):
    logical_query, bound, metadata = _fixture()
    _patch_parse_bind_and_metadata(monkeypatch, logical_query, bound, metadata)

    from src.api import routes as routes_mod

    async def _route_should_not_run(*args, **kwargs):
        raise AssertionError("route selection must not run for incompatible fields")

    monkeypatch.setattr(routes_mod, "route_query", _route_should_not_run)

    with pytest.raises(HTTPException) as exc_info:
        await _handle_execute(
            _request(bound.model.id),
            SimpleNamespace(),
            user_identity="analyst@example.com",
            tenant_id="tenant-1",
        )

    assert exc_info.value.status_code == 422
    detail = exc_info.value.detail
    assert detail["error_type"] == "field_compatibility"
    payload = detail["field_compatibility"]
    assert payload["status"] == "incompatible"
    assert payload["issues"][0]["code"] == "NO_JOIN_PATH"
    assert "aggregation path" in payload["issues"][0]["message"]


@pytest.mark.asyncio
async def test_explain_blocks_incompatible_fields_before_routing(monkeypatch):
    logical_query, bound, metadata = _fixture()
    _patch_parse_bind_and_metadata(monkeypatch, logical_query, bound, metadata)

    from src.api import routes as routes_mod

    async def _route_should_not_run(*args, **kwargs):
        raise AssertionError("route selection must not run for incompatible fields")

    monkeypatch.setattr(routes_mod, "route_query", _route_should_not_run)

    with pytest.raises(HTTPException) as exc_info:
        await _handle_explain(_request(bound.model.id), SimpleNamespace())

    assert exc_info.value.status_code == 422
    detail = exc_info.value.detail
    assert detail["error_type"] == "field_compatibility"
    assert detail["field_compatibility"]["issues"][0]["dimension_name"] == "teacher_name"
