"""Guards for the body-parameter foreign-key scope primitives in ``_scope``.

These cover the defect class "unscoped body foreign key": a route validates the
PATH (project_id / model_id) and then writes an id taken from the REQUEST BODY
into a persisted row without proving the referenced entity belongs to that
model.

This file is the NO-DATABASE layer. It proves three things a mocked session can
prove honestly:

  * the ownership predicate is actually IN the emitted SQL, with the caller's
    values bound to it (a neutered or deleted predicate turns these red without
    needing Postgres);
  * the fail-closed decisions that happen before any query runs (unknown entity
    class, unknown polymorphic target_type, malformed id, empty collection);
  * the error CONTRACT — status, error_code, and the anti-oracle property that
    an unknown id and a foreign id are reported identically.

The behavioural half — "a real foreign row is really rejected and a real
in-scope row is really accepted" — lives in
``tests/integration/test_scope_body_fk_db.py`` against real Postgres, because a
fake session cannot evaluate a WHERE clause and would pass an accept-everything
guard.
"""
from __future__ import annotations

import inspect
import uuid

import pytest
from .result_fakes import FakeScalarResult
from fastapi import HTTPException
from sqlalchemy.exc import MultipleResultsFound

from shared.db.models import (
    CalendarTable,
    Dimension,
    LLMProviderConfig,
    Measure,
    Model,
    ModelColumn,
    ProjectConnection,
    QueryLog,
    UserAccessBinding,
)
from src.api._scope import (
    _OWNERSHIP_PARENT,
    _entity_label,
    _not_in_model,
    scoped_select,
    ensure_calendar_table_in_model,
    ensure_ref_in_model,
    ensure_ref_in_project,
    ensure_refs_in_model,
    ensure_target_in_model,
)


# ---------------------------------------------------------------------------
# Fake session: records the statement, returns whatever rows it was given.
# It deliberately does NOT filter — that is exactly why the accept paths are
# re-proved against real Postgres in the integration file.
# ---------------------------------------------------------------------------


class _Result:
    def __init__(self, rows):
        self._rows = list(rows)

    def scalars(self):
        return FakeScalarResult(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None

    def one_or_none(self):
        if len(self._rows) > 1:
            raise AssertionError(
                "the ownership chain returned more than one row for one id"
            )
        return self._rows[0] if self._rows else None

    def all(self):
        return list(self._rows)


class _FakeSession:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.statements = []

    async def execute(self, stmt):
        self.statements.append(stmt)
        return _Result(self.rows)


def _sql(stmt) -> str:
    return str(stmt.compile())


def _params(stmt) -> dict:
    return dict(stmt.compile().params)


def _row(entity, **kw):
    """A transient ORM instance — only its id/model_id are ever read."""
    return entity(**kw)


# ---------------------------------------------------------------------------
# scoped_select — the single ownership predicate every helper shares
# ---------------------------------------------------------------------------


def test_scoped_select_closes_the_whole_project_model_row_chain():
    """The emitted SQL must carry BOTH the model predicate and the project
    predicate with the caller's values bound — not merely select by id. The
    project hop is what makes the guard independent of whether the caller
    remembered ensure_model_in_project."""
    model_id = uuid.uuid4()
    project_id = uuid.uuid4()
    stmt = scoped_select(Measure, model_id=model_id, project_id=project_id)
    sql = _sql(stmt)

    assert "measures.model_id = " in sql
    assert "models.project_id = " in sql
    assert {model_id, project_id} <= set(_params(stmt).values())


def test_scoped_select_scopes_a_column_through_its_table():
    """ModelColumn has no model_id; ownership runs through ModelTable and on to
    the project. Both hops must be present."""
    model_id = uuid.uuid4()
    project_id = uuid.uuid4()
    stmt = scoped_select(ModelColumn, model_id=model_id, project_id=project_id)
    sql = _sql(stmt)

    assert "JOIN model_tables ON model_columns.model_table_id = model_tables.id" in sql
    assert "JOIN models ON model_tables.model_id = models.id" in sql
    assert "model_tables.model_id = " in sql
    assert "models.project_id = " in sql
    assert {model_id, project_id} <= set(_params(stmt).values())


def test_scoped_select_walks_the_calendar_chain_to_the_model_and_project():
    """CalendarTable has no model_id at all: ownership is
    CalendarTable -> DataSource -> Model -> project. All of it must be in ONE
    statement, so no hop can be skipped or go stale between hops."""
    model_id = uuid.uuid4()
    project_id = uuid.uuid4()
    stmt = scoped_select(
        CalendarTable, model_id=model_id, project_id=project_id
    )
    sql = _sql(stmt)

    assert (
        "JOIN data_sources ON calendar_tables.data_source_id = data_sources.id"
        in sql
    )
    assert "JOIN models ON data_sources.model_id = models.id" in sql
    assert "data_sources.model_id = " in sql
    assert "models.project_id = " in sql
    assert {model_id, project_id} <= set(_params(stmt).values())


def test_scoped_select_refuses_the_model_itself():
    with pytest.raises(TypeError) as exc:
        scoped_select(Model, model_id=uuid.uuid4(), project_id=uuid.uuid4())

    assert "ensure_model_in_project" in str(exc.value)


def test_scoped_select_fails_closed_on_an_entity_with_no_ownership_path():
    """An entity whose ownership path is unknown must raise, never return an
    unrestricted SELECT — an unrestricted SELECT is the defect itself."""
    with pytest.raises(TypeError) as exc:
        scoped_select(
            ProjectConnection, model_id=uuid.uuid4(), project_id=uuid.uuid4()
        )

    assert "model_id" in str(exc.value)


# ---------------------------------------------------------------------------
# ensure_ref_in_model — single body FK
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_ref_in_model_queries_by_id_and_by_model():
    ref_id = uuid.uuid4()
    model_id = uuid.uuid4()
    project_id = uuid.uuid4()
    db = _FakeSession([_row(Measure, id=ref_id, model_id=model_id)])

    await ensure_ref_in_model(
        db, Measure, ref_id=ref_id, model_id=model_id, project_id=project_id,
        field_name="measure_id"
    )

    stmt = db.statements[0]
    values = set(_params(stmt).values())
    assert "measures.model_id = " in _sql(stmt)
    assert "measures.id = " in _sql(stmt)
    assert "models.project_id = " in _sql(stmt)
    assert {ref_id, model_id, project_id} <= values


@pytest.mark.asyncio
async def test_ensure_ref_in_model_returns_the_row_it_resolved():
    """Positive path. An over-broad guard is a defect, but so is a guard that
    denies everything — the helper must hand back the resolved row."""
    ref_id = uuid.uuid4()
    model_id = uuid.uuid4()
    project_id = uuid.uuid4()
    expected = _row(Measure, id=ref_id, model_id=model_id)
    db = _FakeSession([expected])

    got = await ensure_ref_in_model(
        db, Measure, ref_id=ref_id, model_id=model_id, project_id=project_id,
        field_name="measure_id"
    )

    assert got is expected


@pytest.mark.asyncio
async def test_ensure_ref_in_model_rejects_when_nothing_in_scope_matches():
    ref_id = uuid.uuid4()
    db = _FakeSession([])

    with pytest.raises(HTTPException) as exc:
        await ensure_ref_in_model(
            db, Measure, ref_id=ref_id, model_id=uuid.uuid4(), project_id=uuid.uuid4(),
            field_name="variant_of_measure_id",
        )

    assert exc.value.status_code == 422
    assert exc.value.detail["error_code"] == "REF_NOT_IN_MODEL"
    assert exc.value.detail["field"] == "variant_of_measure_id"
    assert exc.value.detail["ids"] == [str(ref_id)]


@pytest.mark.asyncio
async def test_ensure_ref_in_model_treats_absent_optional_fk_as_nothing_to_check():
    db = _FakeSession([])

    assert await ensure_ref_in_model(
        db, Measure, ref_id=None, model_id=uuid.uuid4(), project_id=uuid.uuid4(),
        field_name="measure_id"
    ) is None
    assert db.statements == []


@pytest.mark.asyncio
async def test_ensure_ref_in_model_rejects_a_malformed_id_the_same_way():
    """A malformed id must fail closed with the SAME status and error_code as
    an unknown one, so the id format cannot be probed."""
    db = _FakeSession([])

    with pytest.raises(HTTPException) as unknown:
        await ensure_ref_in_model(
            db, Measure, ref_id=uuid.uuid4(), model_id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            field_name="measure_id",
        )
    with pytest.raises(HTTPException) as malformed:
        await ensure_ref_in_model(
            db, Measure, ref_id="'; DROP TABLE measures; --",
            model_id=uuid.uuid4(), project_id=uuid.uuid4(), field_name="measure_id",
        )

    assert unknown.value.status_code == malformed.value.status_code
    assert (
        unknown.value.detail["error_code"] == malformed.value.detail["error_code"]
    )
    # Malformed input is never handed to the database.
    assert len(db.statements) == 1


# ---------------------------------------------------------------------------
# ensure_refs_in_model — list body FK
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_refs_in_model_returns_every_resolved_row():
    model_id = uuid.uuid4()
    project_id = uuid.uuid4()
    a, b = uuid.uuid4(), uuid.uuid4()
    rows = [_row(ModelColumn, id=a), _row(ModelColumn, id=b)]
    db = _FakeSession(rows)

    got = await ensure_refs_in_model(
        db, ModelColumn, ref_ids=[a, b], model_id=model_id, project_id=project_id,
        field_name="column_ids",
    )

    assert got == rows
    assert "model_tables.model_id = " in _sql(db.statements[0])


@pytest.mark.asyncio
async def test_ensure_refs_in_model_names_only_the_offending_ids():
    """The data_tags behaviour: an operator with fifty ids must be told which
    one was wrong. Safe — the ids are the caller's own input."""
    model_id = uuid.uuid4()
    project_id = uuid.uuid4()
    good, bad = uuid.uuid4(), uuid.uuid4()
    db = _FakeSession([_row(ModelColumn, id=good)])

    with pytest.raises(HTTPException) as exc:
        await ensure_refs_in_model(
            db, ModelColumn, ref_ids=[good, bad], model_id=model_id,
            project_id=project_id,
            field_name="column_ids",
        )

    assert exc.value.status_code == 422
    assert exc.value.detail["error_code"] == "REFS_NOT_IN_MODEL"
    assert exc.value.detail["ids"] == [str(bad)]


@pytest.mark.asyncio
async def test_ensure_refs_in_model_is_all_or_nothing():
    """Returning the in-scope subset would silently persist a shorter
    collection than the user asked for; the whole request must fail."""
    good, bad = uuid.uuid4(), uuid.uuid4()
    db = _FakeSession([_row(ModelColumn, id=good)])

    with pytest.raises(HTTPException):
        await ensure_refs_in_model(
            db, ModelColumn, ref_ids=[good, bad], model_id=uuid.uuid4(),
            project_id=uuid.uuid4(),
            field_name="column_ids",
        )


@pytest.mark.asyncio
async def test_ensure_refs_in_model_sends_each_distinct_id_once():
    """The de-duplication must be visible in the emitted IN list. Asserting
    len(result) == 1 would pass whether or not de-duplication happens, because
    the fake session returns its fixed row list regardless of the statement."""
    model_id = uuid.uuid4()
    project_id = uuid.uuid4()
    a = uuid.uuid4()
    db = _FakeSession([_row(ModelColumn, id=a)])

    got = await ensure_refs_in_model(
        db, ModelColumn, ref_ids=[a, a, str(a)], model_id=model_id,
        project_id=project_id, field_name="column_ids",
    )

    assert len(got) == 1
    assert db.statements[0].compile().params["id_1"] == [a]


@pytest.mark.asyncio
async def test_ensure_refs_in_model_empty_collection_issues_no_query():
    db = _FakeSession([])

    assert await ensure_refs_in_model(
        db, ModelColumn, ref_ids=None, model_id=uuid.uuid4(), project_id=uuid.uuid4(),
        field_name="column_ids",
    ) == []
    assert await ensure_refs_in_model(
        db, ModelColumn, ref_ids=[], model_id=uuid.uuid4(), project_id=uuid.uuid4(),
        field_name="column_ids",
    ) == []
    assert db.statements == []


@pytest.mark.asyncio
async def test_ensure_refs_in_model_rejects_a_malformed_id_without_querying():
    db = _FakeSession([])

    with pytest.raises(HTTPException) as exc:
        await ensure_refs_in_model(
            db, ModelColumn, ref_ids=[uuid.uuid4(), "nope"],
            model_id=uuid.uuid4(), project_id=uuid.uuid4(), field_name="column_ids",
        )

    assert exc.value.status_code == 422
    assert exc.value.detail["ids"] == ["nope"]
    assert db.statements == []


# ---------------------------------------------------------------------------
# ensure_target_in_model — polymorphic (target_type, target_id)
# ---------------------------------------------------------------------------


_GLOSSARY_TARGETS = {
    "dimension": Dimension,
    "measure": Measure,
    "column": ModelColumn,
    "concept": None,
}


@pytest.mark.asyncio
async def test_ensure_target_in_model_rejects_an_unknown_target_type():
    """glossary.py's hand-written if/elif chain falls THROUGH on an unknown
    type (safe there only because a Pydantic validator runs first). The
    primitive must fail closed on its own."""
    db = _FakeSession([])

    with pytest.raises(HTTPException) as exc:
        await ensure_target_in_model(
            db, target_type="hierarchy", target_id=uuid.uuid4(),
            model_id=uuid.uuid4(), project_id=uuid.uuid4(),
            allowed_targets=_GLOSSARY_TARGETS,
        )

    assert exc.value.status_code == 422
    assert exc.value.detail["error_code"] == "TARGET_TYPE_NOT_ALLOWED"
    assert db.statements == []


@pytest.mark.asyncio
async def test_ensure_target_in_model_rejects_a_missing_target_type():
    db = _FakeSession([])

    with pytest.raises(HTTPException):
        await ensure_target_in_model(
            db, target_type=None, target_id=uuid.uuid4(),
            model_id=uuid.uuid4(), project_id=uuid.uuid4(),
            allowed_targets=_GLOSSARY_TARGETS,
        )


@pytest.mark.asyncio
async def test_ensure_target_in_model_allows_an_idless_target_type():
    db = _FakeSession([])

    assert await ensure_target_in_model(
        db, target_type="concept", target_id=None, model_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        allowed_targets=_GLOSSARY_TARGETS,
    ) is None
    assert db.statements == []


@pytest.mark.asyncio
async def test_ensure_target_in_model_rejects_an_id_on_an_idless_type():
    db = _FakeSession([])

    with pytest.raises(HTTPException) as exc:
        await ensure_target_in_model(
            db, target_type="concept", target_id=uuid.uuid4(),
            model_id=uuid.uuid4(), project_id=uuid.uuid4(),
            allowed_targets=_GLOSSARY_TARGETS,
        )

    assert exc.value.detail["error_code"] == "TARGET_ID_NOT_ALLOWED"


@pytest.mark.asyncio
async def test_ensure_target_in_model_requires_an_id_for_an_id_bearing_type():
    """glossary.py returns early when target_id is None for ANY type, so a
    dimension attachment pointing at nothing slips through. Fail closed."""
    db = _FakeSession([])

    with pytest.raises(HTTPException) as exc:
        await ensure_target_in_model(
            db, target_type="dimension", target_id=None,
            model_id=uuid.uuid4(), project_id=uuid.uuid4(),
            allowed_targets=_GLOSSARY_TARGETS,
        )

    assert exc.value.detail["error_code"] == "TARGET_ID_REQUIRED"
    assert db.statements == []


@pytest.mark.asyncio
async def test_ensure_target_in_model_resolves_and_scopes_a_known_type():
    model_id = uuid.uuid4()
    project_id = uuid.uuid4()
    target_id = uuid.uuid4()
    expected = _row(Dimension, id=target_id, model_id=model_id)
    db = _FakeSession([expected])

    got = await ensure_target_in_model(
        db, target_type="dimension", target_id=target_id, model_id=model_id,
        project_id=project_id,
        allowed_targets=_GLOSSARY_TARGETS,
    )

    assert got is expected
    assert "dimensions.model_id = " in _sql(db.statements[0])


@pytest.mark.asyncio
async def test_ensure_target_in_model_column_branch_goes_through_its_table():
    """The branch Bug-7253 had to special-case by hand — a column's owner is
    its table, so the scope predicate must land on model_tables."""
    model_id = uuid.uuid4()
    project_id = uuid.uuid4()
    target_id = uuid.uuid4()
    db = _FakeSession([_row(ModelColumn, id=target_id)])

    await ensure_target_in_model(
        db, target_type="column", target_id=target_id, model_id=model_id,
        project_id=project_id,
        allowed_targets=_GLOSSARY_TARGETS,
    )

    assert "model_tables.model_id = " in _sql(db.statements[0])


@pytest.mark.asyncio
async def test_ensure_target_in_model_reports_unknown_and_foreign_identically():
    """Anti-oracle: the response must not let a caller distinguish "no such
    row" from "row in another project" (glossary.py:537-542)."""
    same_id = uuid.uuid4()
    unknown_db = _FakeSession([])
    foreign_db = _FakeSession([])

    with pytest.raises(HTTPException) as unknown:
        await ensure_target_in_model(
            unknown_db, target_type="dimension", target_id=same_id,
            model_id=uuid.uuid4(), project_id=uuid.uuid4(),
            allowed_targets=_GLOSSARY_TARGETS,
        )
    with pytest.raises(HTTPException) as foreign:
        await ensure_target_in_model(
            foreign_db, target_type="dimension", target_id=same_id,
            model_id=uuid.uuid4(), project_id=uuid.uuid4(),
            allowed_targets=_GLOSSARY_TARGETS,
        )

    assert unknown.value.status_code == foreign.value.status_code
    assert unknown.value.detail == foreign.value.detail


# ---------------------------------------------------------------------------
# ensure_calendar_table_in_model — the two-hop variant
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_calendar_table_walks_source_and_project_in_one_query():
    """All three hops must be in ONE statement, so no hop can be skipped and
    nothing can change between them."""
    calendar_id = uuid.uuid4()
    model_id = uuid.uuid4()
    project_id = uuid.uuid4()
    db = _FakeSession([_row(CalendarTable, id=calendar_id)])

    await ensure_calendar_table_in_model(
        db, calendar_table_id=calendar_id, model_id=model_id,
        project_id=project_id,
    )

    assert len(db.statements) == 1
    sql = _sql(db.statements[0])
    assert "JOIN data_sources ON calendar_tables.data_source_id = data_sources.id" in sql
    assert "JOIN models ON data_sources.model_id = models.id" in sql
    assert "calendar_tables.id = " in sql
    assert "data_sources.model_id = " in sql
    assert "models.project_id = " in sql
    values = set(_params(db.statements[0]).values())
    assert {calendar_id, model_id, project_id} <= values


@pytest.mark.asyncio
async def test_ensure_calendar_table_returns_the_resolved_calendar():
    calendar_id = uuid.uuid4()
    expected = _row(CalendarTable, id=calendar_id)
    db = _FakeSession([expected])

    got = await ensure_calendar_table_in_model(
        db, calendar_table_id=calendar_id, model_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
    )

    assert got is expected


@pytest.mark.asyncio
async def test_ensure_calendar_table_absent_id_is_not_a_violation():
    db = _FakeSession([])

    assert await ensure_calendar_table_in_model(
        db, calendar_table_id=None, model_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
    ) is None
    assert db.statements == []


@pytest.mark.asyncio
async def test_ensure_calendar_table_rejects_when_the_chain_does_not_resolve():
    calendar_id = uuid.uuid4()
    db = _FakeSession([])

    with pytest.raises(HTTPException) as exc:
        await ensure_calendar_table_in_model(
            db, calendar_table_id=calendar_id, model_id=uuid.uuid4(),
            project_id=uuid.uuid4(),
        )

    assert exc.value.status_code == 422
    assert exc.value.detail["error_code"] == "CALENDAR_TABLE_NOT_IN_MODEL"
    assert exc.value.detail["ids"] == [str(calendar_id)]


# ---------------------------------------------------------------------------
# API-shape guard — ids must be unexpressible in the wrong position
# ---------------------------------------------------------------------------


_HELPERS = [
    (ensure_ref_in_model, ["ref_id", "model_id", "project_id"]),
    (ensure_refs_in_model, ["ref_ids", "model_id", "project_id"]),
    (
        ensure_target_in_model,
        ["target_type", "target_id", "model_id", "project_id"],
    ),
    (
        ensure_calendar_table_in_model,
        ["calendar_table_id", "model_id", "project_id"],
    ),
]


@pytest.mark.parametrize("helper,id_params", _HELPERS)
def test_every_id_argument_is_keyword_only(helper, id_params):
    """Two pre-existing helpers (translations.py, scheduler sla.py) take
    (db, model_id, project_id) positionally in OPPOSITE orders — a transposed
    pair there is a silently-passing authorization check. These signatures must
    make that unexpressible."""
    params = inspect.signature(helper).parameters
    for name in id_params:
        assert params[name].kind is inspect.Parameter.KEYWORD_ONLY, name


@pytest.mark.parametrize("helper,id_params", _HELPERS)
def test_the_path_scope_arguments_have_no_defaults(helper, id_params):
    """project_id and model_id must be REQUIRED. A default would let a caller
    omit the project hop and silently fall back to a weaker guard — the exact
    "precondition nobody remembers" failure these helpers replace."""
    params = inspect.signature(helper).parameters
    for name in ("model_id", "project_id"):
        assert params[name].default is inspect.Parameter.empty, name


# ---------------------------------------------------------------------------
# Fail-closed guards added in review round 1
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_target_in_model_rejects_a_malformed_id_without_querying():
    """The _as_uuid guard is what keeps a non-UUID string away from a
    UUID(as_uuid=True) bind. Without it the driver raises and the modeller gets
    a 500 instead of a 422 — the Bug-8864 live-500 class."""
    db = _FakeSession([])

    with pytest.raises(HTTPException) as exc:
        await ensure_target_in_model(
            db, target_type="dimension", target_id="not-a-uuid",
            model_id=uuid.uuid4(), project_id=uuid.uuid4(),
            allowed_targets=_GLOSSARY_TARGETS,
        )

    assert exc.value.status_code == 422
    assert exc.value.detail["error_code"] == "TARGET_NOT_IN_MODEL"
    assert db.statements == []


@pytest.mark.asyncio
async def test_ensure_calendar_table_rejects_a_malformed_id_without_querying():
    db = _FakeSession([])

    with pytest.raises(HTTPException) as exc:
        await ensure_calendar_table_in_model(
            db, calendar_table_id="not-a-uuid",
            model_id=uuid.uuid4(), project_id=uuid.uuid4(),
        )

    assert exc.value.status_code == 422
    assert exc.value.detail["error_code"] == "CALENDAR_TABLE_NOT_IN_MODEL"
    assert db.statements == []


def test_scoped_select_refuses_an_entity_with_no_id_column():
    """ModelAliasMap carries model_id but has no id, so `.where(entity.id ==
    ...)` would raise AttributeError inside the caller. Refuse up front."""
    from shared.db.models import ModelAliasMap

    with pytest.raises(TypeError) as exc:
        scoped_select(
            ModelAliasMap, model_id=uuid.uuid4(), project_id=uuid.uuid4()
        )

    assert "no id column" in str(exc.value)


@pytest.mark.parametrize(
    "entity,reason",
    [
        (UserAccessBinding, "nullable"),
        (QueryLog, "not a foreign key"),
    ],
)
def test_scoped_select_refuses_a_model_id_that_is_not_an_ownership_axis(
    entity, reason
):
    """A model_id attribute is not proof of ownership, and the two ways it can
    fail are checked separately. UserAccessBinding's model_id is nullable, so
    project-level bindings would be silently EXCLUDED — the deny-everything
    direction that shipped as Bug-8864. QueryLog's is a loose telemetry column
    with no foreign key at all. Both must be refused rather than scoped by
    accident of attribute naming, and each must say which rule it broke."""
    with pytest.raises(TypeError) as exc:
        scoped_select(entity, model_id=uuid.uuid4(), project_id=uuid.uuid4())

    assert reason in str(exc.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["", "   ", 0])
async def test_ensure_ref_in_model_does_not_treat_falsy_input_as_absent(bad):
    """Only ``ref_id is None`` means "no FK supplied". An empty string or 0 is
    a malformed id and must be REJECTED — skipping the guard on falsy input
    would be a write with no ownership proof."""
    db = _FakeSession([])

    with pytest.raises(HTTPException) as exc:
        await ensure_ref_in_model(
            db, Measure, ref_id=bad, model_id=uuid.uuid4(),
            project_id=uuid.uuid4(), field_name="measure_id",
        )

    assert exc.value.detail["error_code"] == "REF_NOT_IN_MODEL"
    assert db.statements == []


def test_entity_label_is_readable_for_acronyms_and_vowels():
    """The label lands in the 422 message a modeller reads. "a k p i" is not a
    sentence."""
    from shared.db.models import AggregateDefinition, KPI, ModelColumn

    from shared.db.models import KPISnapshot, UserDefinedAttribute

    assert _entity_label(KPI) == "a KPI"
    assert _entity_label(ModelColumn) == "a model column"
    assert _entity_label(AggregateDefinition) == "an aggregate definition"
    # An acronym glued to a word must still split into words.
    assert _entity_label(KPISnapshot) == "a KPI snapshot"
    # "u" takes "a", not "an".
    assert _entity_label(UserDefinedAttribute) == "a user defined attribute"


def test_long_and_numerous_echoed_ids_are_bounded():
    """The malformed path reflects raw client input into the response and the
    logs. Cap the length of each id and the number listed."""
    ids = [f"{i}-" + "x" * 500 for i in range(50)]
    exc = _not_in_model(
        error_code="REFS_NOT_IN_MODEL", field_name="column_ids", ids=ids,
        noun="a model column",
    )

    assert len(exc.detail["ids"]) == 20
    assert all(len(i) <= 67 for i in exc.detail["ids"])
    assert "and 30 more" in exc.detail["message"]


# ---------------------------------------------------------------------------
# The ownership declaration is itself a coverage mechanism — audit it
# ---------------------------------------------------------------------------


def test_every_declared_ownership_chain_terminates_at_a_real_model_owner():
    """_OWNERSHIP_PARENT is the table that decides which join proves ownership.
    A stale or wrong entry would scope a query to nothing (deny-everything) or
    to the wrong axis. Walk every declared chain and require it to end at a
    NOT NULL model_id with a real foreign key to models.id — the same rule
    _direct_model_column enforces at runtime."""
    for entity in _OWNERSHIP_PARENT:
        stmt = scoped_select(
            entity, model_id=uuid.uuid4(), project_id=uuid.uuid4()
        )
        sql = str(stmt.compile())
        assert "JOIN models ON" in sql, entity.__name__
        assert "models.project_id = " in sql, entity.__name__


def test_every_declared_parent_fk_points_at_that_parents_table():
    """A copy-paste in the table (right parent class, wrong FK column) would
    join on an unrelated id and silently mis-scope. Check the declaration
    against the ORM metadata rather than trusting the literal."""
    for entity, (fk_column, parent) in _OWNERSHIP_PARENT.items():
        targets = {
            fk.target_fullname
            for fk in entity.__table__.c[fk_column.key].foreign_keys
        }
        assert f"{parent.__tablename__}.id" in targets, (
            f"{entity.__name__}.{fk_column.key} does not point at "
            f"{parent.__tablename__}"
        )


def test_every_declared_parent_fk_is_not_nullable():
    """scoped_select INNER-joins each declared parent, so a NULLABLE parent FK
    would silently EXCLUDE every row whose parent is NULL — the same
    deny-everything direction _direct_model_column already rejects for the
    terminal model_id, and the blind spot the FK-target audit does not cover."""
    for entity, (fk_column, _parent) in _OWNERSHIP_PARENT.items():
        column = entity.__table__.c[fk_column.key]
        assert not column.nullable, (
            f"{entity.__name__}.{fk_column.key} is nullable; joining through it "
            "would exclude rows owned at another level"
        )


def test_a_chain_of_exactly_the_hop_limit_is_still_accepted():
    """The for/else fires when the loop ends WITHOUT break, and the break needs
    one iteration past the last hop — so a bare range(_MAX_CHAIN_HOPS) would
    admit one hop fewer than declared. Pin the boundary against the real 2-hop
    HierarchyLevelAttribute chain."""
    import src.api._scope as scope_module
    from shared.db.models import HierarchyLevelAttribute

    saved = scope_module._MAX_CHAIN_HOPS
    try:
        scope_module._MAX_CHAIN_HOPS = 2  # == the declared chain length
        scoped_select(
            HierarchyLevelAttribute, model_id=uuid.uuid4(),
            project_id=uuid.uuid4(),
        )
    finally:
        scope_module._MAX_CHAIN_HOPS = saved


def test_a_runaway_ownership_chain_fails_closed(monkeypatch):
    """A circular or over-deep declaration must raise rather than loop or emit
    a query missing its ownership predicate. Driven by shrinking the hop limit
    against a REAL declared chain, so the guard is exercised with genuine
    foreign keys rather than a fabricated one the FK check would reject first."""
    import src.api._scope as scope_module

    monkeypatch.setattr(scope_module, "_MAX_CHAIN_HOPS", 0)

    with pytest.raises(TypeError) as exc:
        scoped_select(
            ModelColumn, model_id=uuid.uuid4(), project_id=uuid.uuid4()
        )

    assert "hop limit" in str(exc.value)


def test_a_declared_parent_that_is_not_a_real_foreign_key_is_refused(
    monkeypatch
):
    """The join target is read from the FK metadata, so a declaration naming a
    column that does not actually reference the declared parent is refused
    instead of producing a valid query joined on the wrong column."""
    import src.api._scope as scope_module

    bogus = dict(scope_module._OWNERSHIP_PARENT)
    bogus[ModelColumn] = (ModelColumn.model_table_id, Measure)
    monkeypatch.setattr(scope_module, "_OWNERSHIP_PARENT", bogus)

    with pytest.raises(TypeError) as exc:
        scoped_select(
            ModelColumn, model_id=uuid.uuid4(), project_id=uuid.uuid4()
        )

    assert "not a foreign key" in str(exc.value)


@pytest.mark.asyncio
async def test_an_ambiguous_ownership_resolution_raises_instead_of_guessing():
    """Two rows for one id would mean a broken schema invariant. Picking the
    first would resolve ownership ARBITRARILY; the lookup must surface it."""
    ref_id = uuid.uuid4()
    db = _FakeSession([_row(Measure, id=ref_id), _row(Measure, id=ref_id)])

    with pytest.raises(MultipleResultsFound):
        await ensure_ref_in_model(
            db, Measure, ref_id=ref_id, model_id=uuid.uuid4(),
            project_id=uuid.uuid4(), field_name="measure_id",
        )


@pytest.mark.asyncio
async def test_ensure_refs_in_model_refuses_an_absurdly_large_collection():
    """Batching removed the driver-level ceiling, so the primitive owns the
    total bound too: 100k ids would otherwise become 100 sequential queries
    inside one request instead of a 422."""
    db = _FakeSession([])

    with pytest.raises(HTTPException) as exc:
        await ensure_refs_in_model(
            db, ModelColumn, ref_ids=[uuid.uuid4() for _ in range(10_001)],
            model_id=uuid.uuid4(), project_id=uuid.uuid4(),
            field_name="column_ids",
        )

    assert exc.value.status_code == 422
    assert exc.value.detail["error_code"] == "TOO_MANY_IDS"
    assert db.statements == []


# ---------------------------------------------------------------------------
# T3 cross-family challenger findings
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("required", [False, True])
async def test_absent_ref_is_permissive_by_default_and_rejectable_on_demand(
    required,
):
    """None NEVER means "not validated" — the helper raises on every failure
    and returns None only when the caller supplied no id. required=True lets a
    call site state that the reference must be present, so a caller cannot
    mistake an absent required FK for a validated one."""
    db = _FakeSession([])

    if required:
        with pytest.raises(HTTPException) as exc:
            await ensure_ref_in_model(
                db, Measure, ref_id=None, model_id=uuid.uuid4(),
                project_id=uuid.uuid4(), field_name="measure_id",
                required=True,
            )
        assert exc.value.status_code == 422
        assert exc.value.detail["ids"] == []
        assert "required" in exc.value.detail["message"]
    else:
        assert await ensure_ref_in_model(
            db, Measure, ref_id=None, model_id=uuid.uuid4(),
            project_id=uuid.uuid4(), field_name="measure_id",
        ) is None
    assert db.statements == []


@pytest.mark.asyncio
async def test_an_empty_required_collection_is_rejected():
    db = _FakeSession([])

    with pytest.raises(HTTPException) as exc:
        await ensure_refs_in_model(
            db, ModelColumn, ref_ids=[], model_id=uuid.uuid4(),
            project_id=uuid.uuid4(), field_name="column_ids", required=True,
        )

    assert exc.value.status_code == 422
    assert db.statements == []


@pytest.mark.asyncio
async def test_a_required_calendar_reference_cannot_be_omitted():
    db = _FakeSession([])

    with pytest.raises(HTTPException) as exc:
        await ensure_calendar_table_in_model(
            db, calendar_table_id=None, model_id=uuid.uuid4(),
            project_id=uuid.uuid4(), required=True,
        )

    assert exc.value.detail["error_code"] == "CALENDAR_TABLE_NOT_IN_MODEL"


def test_every_declared_parent_fk_resolves_to_a_column_on_that_parent():
    """T3 challenger findings 2 and 5. The walk used to join on ``parent.id``
    by assumption; it now reads the real foreign-key target. Assert, for every
    declared entry, that the named column IS a foreign key and that its target
    lives on the declared parent's table — a column that is not an FK, or an FK
    to a different table, would otherwise produce a syntactically valid join on
    the wrong column and silently scope rows to the wrong owner."""
    from src.api._scope import _fk_target_column

    for entity, (fk_column, parent) in _OWNERSHIP_PARENT.items():
        source = entity.__table__.c[fk_column.key]
        assert source.foreign_keys, (
            f"{entity.__name__}.{fk_column.key} is not a foreign key at all"
        )
        target = _fk_target_column(source, parent)
        assert target.table is parent.__table__, (
            f"{entity.__name__}.{fk_column.key} resolves to {target.table.name}, "
            f"not {parent.__tablename__}"
        )


def test_no_handler_reassigns_an_ownership_id_on_an_existing_row():
    """T3 challenger finding 1 (TOCTOU), converted into a permanent gate.

    These guards prove ownership at READ time and take no row locks, which is
    safe only while no supported operation MOVES a row between models or
    projects. That premise is what makes the read-then-write window
    unexploitable; nothing else enforces it.

    Two halves, because there are two ways a handler could do it:
      * a direct ``row.model_id = x`` / ``setattr(row, "model_id", x)``;
      * a generic ``setattr(row, field, value)`` fed from an Update payload —
        harmless only while no Update schema carries an ownership id.

    If someone ships a cross-project move, this fires and forces the
    SELECT ... FOR UPDATE decision at the moment it becomes reachable, instead
    of leaving prose for a future reviewer to rediscover.
    """
    import ast
    import inspect as _inspect
    import pathlib

    import pydantic

    import shared.schemas.pydantic_models as schemas

    owner_fields = {"project_id", "model_id"}
    api_root = pathlib.Path(__file__).resolve().parent.parent / "src"

    offenders: list[str] = []
    for path in sorted(api_root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for node in ast.walk(tree):
            targets = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
            for target in targets:
                if (
                    isinstance(target, ast.Attribute)
                    and target.attr in owner_fields
                ):
                    offenders.append(f"{path.name}:{node.lineno} {ast.unparse(node)}")
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "setattr"
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value in owner_fields
            ):
                offenders.append(f"{path.name}:{node.lineno} {ast.unparse(node)}")

    assert not offenders, (
        "a handler reassigns an ownership id on a persisted row; the body-FK "
        "guards in _scope.py take no row locks and rely on rows never moving "
        f"between models/projects: {offenders}"
    )

    exposed = [
        f"{name}.{field}"
        for name in dir(schemas)
        for field in owner_fields
        if _inspect.isclass(getattr(schemas, name, None))
        and issubclass(getattr(schemas, name), pydantic.BaseModel)
        and name.endswith("Update")
        and field in getattr(schemas, name).model_fields
    ]
    assert not exposed, (
        "an Update schema exposes an ownership id, so a generic setattr loop "
        f"could move a row between models/projects: {exposed}"
    )


# ---------------------------------------------------------------------------
# ensure_ref_in_project — the project-owned half of the same defect class
#
# Some body foreign keys name entities that have no model_id at all and are
# owned by the PROJECT (LLMProviderConfig, which carries a Fernet-encrypted
# provider API key and a base_url). ensure_ref_in_model raises TypeError on
# those by design, so before this helper existed the only options at such a
# call site were a hand-rolled get-then-compare or no check at all.
#
# The behavioural half — a real foreign row is really rejected, a real
# in-scope row is really accepted — lives in
# tests/integration/test_scope_body_fk_db.py, because a fake session does not
# evaluate a WHERE clause and would wave an accept-everything guard through.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_ref_in_project_binds_the_project_predicate():
    """The emitted SQL must carry BOTH the id and the PROJECT predicate with
    the caller's values bound. Dropping the project hop leaves a guard that
    accepts any row in the tenant — which is the whole defect."""
    project_id = uuid.uuid4()
    ref_id = uuid.uuid4()
    db = _FakeSession([_row(LLMProviderConfig, id=ref_id)])

    await ensure_ref_in_project(
        db, LLMProviderConfig, ref_id=ref_id, project_id=project_id,
        field_name="llm_config_id",
    )

    sql = _sql(db.statements[0])
    assert "llm_provider_configs.project_id =" in sql
    assert "llm_provider_configs.id =" in sql
    assert set(_params(db.statements[0]).values()) == {project_id, ref_id}


@pytest.mark.asyncio
async def test_ensure_ref_in_project_rejects_when_nothing_in_scope_matches():
    """No row for the (id, project) pair — unknown id and foreign id alike."""
    ref_id = uuid.uuid4()
    db = _FakeSession([])

    with pytest.raises(HTTPException) as exc:
        await ensure_ref_in_project(
            db, LLMProviderConfig, ref_id=ref_id, project_id=uuid.uuid4(),
            field_name="llm_config_id",
        )

    assert exc.value.status_code == 422
    assert exc.value.detail["error_code"] == "REF_NOT_IN_PROJECT"
    assert exc.value.detail["field"] == "llm_config_id"
    assert exc.value.detail["ids"] == [str(ref_id)]
    # The message names the containment level actually checked. Saying "model"
    # here would misreport what the guard proved.
    assert "in this project" in exc.value.detail["message"]


@pytest.mark.asyncio
async def test_ensure_ref_in_project_absent_optional_fk_issues_no_query():
    db = _FakeSession([])
    assert await ensure_ref_in_project(
        db, LLMProviderConfig, ref_id=None, project_id=uuid.uuid4(),
        field_name="llm_config_id",
    ) is None
    assert db.statements == []


@pytest.mark.asyncio
async def test_ensure_ref_in_project_required_absent_fk_is_a_422():
    db = _FakeSession([])
    with pytest.raises(HTTPException) as exc:
        await ensure_ref_in_project(
            db, LLMProviderConfig, ref_id=None, project_id=uuid.uuid4(),
            field_name="llm_config_id", required=True,
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["ids"] == []
    assert db.statements == []


@pytest.mark.asyncio
async def test_ensure_ref_in_project_rejects_a_malformed_id_without_querying():
    """A non-UUID string must never reach a UUID(as_uuid=True) bind: the driver
    would raise and the caller would get a 500 instead of this 422."""
    db = _FakeSession([])
    with pytest.raises(HTTPException) as exc:
        await ensure_ref_in_project(
            db, LLMProviderConfig, ref_id="not-a-uuid",
            project_id=uuid.uuid4(), field_name="llm_config_id",
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["ids"] == ["not-a-uuid"]
    assert db.statements == []


@pytest.mark.asyncio
async def test_ensure_ref_in_project_reports_unknown_and_foreign_identically():
    """ORACLE POLICY holds for the project family too: the response must not
    let a caller distinguish 'no such row anywhere' from 'a row in another
    project' — same status, same detail, same number of queries."""
    ref_id = uuid.uuid4()
    seen = []
    for _ in range(2):
        db = _FakeSession([])
        with pytest.raises(HTTPException) as exc:
            await ensure_ref_in_project(
                db, LLMProviderConfig, ref_id=ref_id,
                project_id=uuid.uuid4(), field_name="llm_config_id",
            )
        seen.append((exc.value.status_code, exc.value.detail, len(db.statements)))
    assert seen[0] == seen[1]


@pytest.mark.asyncio
async def test_ensure_ref_in_project_fails_closed_on_a_model_owned_entity():
    """Measure has no project_id. Scoping it to a project is a category error,
    and an unrestricted SELECT is exactly the defect being prevented, so the
    helper must raise rather than emit one."""
    with pytest.raises(TypeError) as exc:
        await ensure_ref_in_project(
            _FakeSession([]), Measure, ref_id=uuid.uuid4(),
            project_id=uuid.uuid4(), field_name="x",
        )
    assert "project_id" in str(exc.value)


@pytest.mark.asyncio
async def test_ensure_ref_in_project_fails_closed_on_a_nullable_project_id():
    """UserAccessBinding.project_id is nullable — tenant-wide bindings carry
    NULL. Scoping on it would silently EXCLUDE those rows: the deny-everything
    direction that shipped as Bug-8864."""
    with pytest.raises(TypeError) as exc:
        await ensure_ref_in_project(
            _FakeSession([]), UserAccessBinding, ref_id=uuid.uuid4(),
            project_id=uuid.uuid4(), field_name="x",
        )
    assert "nullable" in str(exc.value)


@pytest.mark.asyncio
async def test_ensure_ref_in_project_refuses_the_model_itself():
    """A body-supplied model id is a different (and open) question from a body
    reference inside a model; conflating them here would let a caller believe
    this helper had authorised a model."""
    with pytest.raises(TypeError) as exc:
        await ensure_ref_in_project(
            _FakeSession([]), Model, ref_id=uuid.uuid4(),
            project_id=uuid.uuid4(), field_name="x",
        )
    assert "ensure_model_in_project" in str(exc.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_entity", [Measure, UserAccessBinding, Model])
async def test_ensure_ref_in_project_fails_closed_even_on_a_malformed_id(bad_entity):
    """P3-d review F1. The entity's fitness must be settled BEFORE the id is
    parsed.

    The first cut ordered the ``_as_uuid`` parse ahead of the declaration
    checks, so a malformed id short-circuited to the ordinary 422 and a
    mis-declared entity — one with no project_id, a nullable project_id, or
    Model itself — was never rejected as a programming error. The guard still
    DENIED, so nothing looked wrong; the broken declaration simply stopped
    announcing itself, which is exactly how a coverage mechanism rots. Whether
    an entity is project-owned is a property of the call site, not of the value
    a client happened to send."""
    with pytest.raises(TypeError):
        await ensure_ref_in_project(
            _FakeSession([]), bad_entity, ref_id="not-a-uuid",
            project_id=uuid.uuid4(), field_name="x",
        )


def test_ensure_ref_in_project_id_arguments_are_keyword_only():
    """Same rule as the model family: a transposed (ref_id, project_id) pair
    would be a silently-passing authorization check, so make it unexpressible."""
    params = inspect.signature(ensure_ref_in_project).parameters
    for name in ("ref_id", "project_id"):
        assert params[name].kind is inspect.Parameter.KEYWORD_ONLY, name
        assert params[name].default is inspect.Parameter.empty, name
