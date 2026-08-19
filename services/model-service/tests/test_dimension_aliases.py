"""Phase RA — dimension aliases.

Two layers under test:

* ``create_table`` in ``src.api.tables`` — alias auto-sequencing when
  multiple ModelTable rows share a ``physical_name`` (the same physical
  dimension reused under different roles).

* ``_resolve_calendar_alias_for_measure`` in ``src.api.measures`` — the
  per-measure calendar-alias resolver. Base measures store
  ``calendar_model_table_id`` directly; variants inherit it from the base
  and are rejected when the base has no calendar bound and the variant
  kind needs one.

Both areas back the user-visible flows described in
``docs/execution/execution_dimension-aliases.md`` (Q1, Q3, Q4, Q5).
"""
from __future__ import annotations

import contextlib
import types
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from src.api.measures import _resolve_calendar_alias_for_measure
from src.api.tables import create_table

from .scope_fake_db import ScopedFakeDB

pytestmark = pytest.mark.unit

# Bug-8862: ``create_table`` now proves the path project owns the path model
# before it touches the source, so these fixtures must model a real
# project -> model chain instead of a random project id.
PROJECT_ID = uuid.uuid4()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_source(model_id: uuid.UUID) -> types.SimpleNamespace:
    return types.SimpleNamespace(id=uuid.uuid4(), model_id=model_id)


def _fake_modeltable(
    *,
    model_id: uuid.UUID,
    calendar_table_id: uuid.UUID | None = None,
) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        model_id=model_id,
        calendar_table_id=calendar_table_id,
    )


def _fake_measure(
    *,
    model_id: uuid.UUID,
    calendar_model_table_id: uuid.UUID | None = None,
) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        model_id=model_id,
        calendar_model_table_id=calendar_model_table_id,
    )


# ---------------------------------------------------------------------------
# create_table — alias auto-sequencing
# ---------------------------------------------------------------------------


def _patch_create_table_db(
    monkeypatch,
    *,
    source: types.SimpleNamespace,
    fact_count: int,
    physical_name_count: int = 0,
    alias_unique_count: int = 0,
    existing_aliases: list[str] | None = None,
    path: str = "auto",
    sibling_columns: list | None = None,
) -> AsyncMock:
    """Wire up the dependencies create_table touches.

    Per non-fact create the endpoint issues:
      * One SELECT — either alias list fallback (``path="auto"``)
        or alias-uniqueness count (``path="explicit"``).
      * One SELECT ModelTable for sibling-alias lookup. Returns a sibling
        when ``sibling_columns`` is provided, ``None`` otherwise.
      * One SELECT ModelColumn for sibling columns (only when a sibling
        exists). Returns ``sibling_columns`` verbatim.
    """
    db = AsyncMock()
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def _add(obj):
        if not getattr(obj, "id", None):
            obj.id = uuid.uuid4()
        obj.description = getattr(obj, "description", None)
        obj.row_count_estimate = getattr(obj, "row_count_estimate", None)
        obj.last_stats_at = getattr(obj, "last_stats_at", None)
        obj.calendar_table_id = getattr(obj, "calendar_table_id", None)
        obj.created_at = getattr(obj, "created_at", None) or now
        obj.updated_at = getattr(obj, "updated_at", None) or now

    db.add = MagicMock(side_effect=_add)
    db.commit = AsyncMock()
    db.flush = AsyncMock()
    db.refresh = AsyncMock()

    # Bug-5426: create_table now uses ``async with db.begin_nested()`` for
    # savepoint-protected alias retries.  Provide a no-op async CM.
    @contextlib.asynccontextmanager
    async def _fake_begin_nested():
        yield

    db.begin_nested = _fake_begin_nested

    async def _get(model, key):
        if model.__name__ == "DataSource":
            return source
        if model.__name__ == "Model":
            # Bug-8862: the project -> model proof that now precedes the source
            # lookup. Returning None here would make every create 404.
            return types.SimpleNamespace(id=key, project_id=PROJECT_ID)
        return None

    db.get = AsyncMock(side_effect=_get)

    # Build the first execute result based on path:
    # - "auto": _derive_auto_alias now queries SELECT alias WHERE model_id
    #   and accesses .scalars().all() to get the alias list.
    # - "explicit": _assert_alias_unique_in_model queries SELECT count()
    #   and accesses .scalar().
    first_result = MagicMock()
    if path == "auto":
        # _derive_auto_alias calls .scalars().all() -- return existing aliases
        # For backward compat: if existing_aliases not given, derive from
        # physical_name_count (0 → empty, 1+ → base alias present).
        if existing_aliases is not None:
            alias_list = existing_aliases
        elif physical_name_count == 0:
            alias_list = []
        else:
            # Simulate base alias already existing
            alias_list = ["cities"]
        first_result.scalars.return_value.all.return_value = alias_list
    elif path == "explicit":
        first_result.scalar.return_value = alias_unique_count
    else:
        raise ValueError(f"Unknown path {path!r}")

    sibling_result = MagicMock()
    if sibling_columns is not None:
        sibling = types.SimpleNamespace(id=uuid.uuid4())
        sibling_result.scalar_one_or_none.return_value = sibling
    else:
        sibling_result.scalar_one_or_none.return_value = None

    columns_result = MagicMock()
    columns_result.scalars.return_value.all.return_value = sibling_columns or []

    side_effects = [first_result, sibling_result]
    if sibling_columns is not None:
        side_effects.append(columns_result)
    db.execute = AsyncMock(side_effect=side_effects)

    async def _gen(*args, **kwargs):
        yield db

    monkeypatch.setattr("src.api.tables.get_tenant_db", _gen)
    return db


async def test_create_table_first_use_alias_defaults_to_base_name(monkeypatch):
    model_id = uuid.uuid4()
    source = _fake_source(model_id)
    db = _patch_create_table_db(
        monkeypatch,
        source=source,
        fact_count=0,
        physical_name_count=0,
    )

    body = types.SimpleNamespace(
        source_id=source.id,
        table_type="dim_aggregate",
        physical_name="public.cities",
        alias=None,
        display_name="Cities",
    )
    user = types.SimpleNamespace(tenant_id="t", user_id="u", email="u")
    response = await create_table(
        project_id=PROJECT_ID,
        model_id=model_id,
        source_id=source.id,
        body=body,  # type: ignore[arg-type]
        current_user=user,  # type: ignore[arg-type]
    )

    added = db.add.call_args.args[0]
    assert added.alias == "cities"
    # Schema prefix is stripped when deriving the default alias.
    assert "public" not in added.alias
    assert response.alias == "cities"


async def test_create_table_second_use_auto_sequences_alias(monkeypatch):
    model_id = uuid.uuid4()
    source = _fake_source(model_id)
    db = _patch_create_table_db(
        monkeypatch,
        source=source,
        fact_count=0,
        physical_name_count=1,
    )

    body = types.SimpleNamespace(
        source_id=source.id,
        table_type="dim_aggregate",
        physical_name="public.cities",
        alias=None,
        display_name="Cities",
    )
    user = types.SimpleNamespace(tenant_id="t", user_id="u", email="u")
    await create_table(
        project_id=PROJECT_ID,
        model_id=model_id,
        source_id=source.id,
        body=body,  # type: ignore[arg-type]
        current_user=user,  # type: ignore[arg-type]
    )

    added = db.add.call_args.args[0]
    assert added.alias == "cities_2"


async def test_create_table_explicit_alias_is_respected(monkeypatch):
    model_id = uuid.uuid4()
    source = _fake_source(model_id)
    db = _patch_create_table_db(
        monkeypatch,
        source=source,
        fact_count=0,
        path="explicit",
        alias_unique_count=0,
    )

    body = types.SimpleNamespace(
        source_id=source.id,
        table_type="dim_aggregate",
        physical_name="public.cities",
        alias="payment_city",
        display_name="Payment City",
    )
    user = types.SimpleNamespace(tenant_id="t", user_id="u", email="u")
    await create_table(
        project_id=PROJECT_ID,
        model_id=model_id,
        source_id=source.id,
        body=body,  # type: ignore[arg-type]
        current_user=user,  # type: ignore[arg-type]
    )

    added = db.add.call_args.args[0]
    assert added.alias == "payment_city"


@pytest.mark.parametrize(
    "bad_alias",
    [
        "Payment_City",  # uppercase
        "1city",          # leading digit
        "payment-city",   # hyphen
        "payment city",   # space
    ],
)
async def test_create_table_rejects_sql_unsafe_alias(monkeypatch, bad_alias):
    model_id = uuid.uuid4()
    source = _fake_source(model_id)
    _patch_create_table_db(
        monkeypatch,
        source=source,
        fact_count=0,
        path="explicit",
        alias_unique_count=0,
    )

    body = types.SimpleNamespace(
        source_id=source.id,
        table_type="dim_aggregate",
        physical_name="public.cities",
        alias=bad_alias,
        display_name="Cities",
    )
    user = types.SimpleNamespace(tenant_id="t", user_id="u", email="u")
    with pytest.raises(HTTPException) as exc:
        await create_table(
            project_id=PROJECT_ID,
            model_id=model_id,
            source_id=source.id,
            body=body,  # type: ignore[arg-type]
            current_user=user,  # type: ignore[arg-type]
        )
    assert exc.value.status_code == 400


async def test_create_table_alias_inherits_sibling_columns(monkeypatch):
    """Bug-106 fix: a second-use alias must clone the sibling alias's
    ModelColumn rows so the new alias is not empty in the canvas, dim
    picker, or columns tab."""
    from shared.db.models import ModelColumn

    model_id = uuid.uuid4()
    source = _fake_source(model_id)
    sibling_columns = [
        types.SimpleNamespace(
            column_name="city_id",
            display_name=None,
            description=None,
            is_hidden=False,
            is_primary_key=True,
            data_type="integer",
            is_nullable=False,
            cardinality_estimate=None,
        ),
        types.SimpleNamespace(
            column_name="city_name",
            display_name="City",
            description="City label",
            is_hidden=False,
            is_primary_key=False,
            data_type="varchar",
            is_nullable=True,
            cardinality_estimate=None,
        ),
    ]
    db = _patch_create_table_db(
        monkeypatch,
        source=source,
        fact_count=0,
        path="explicit",
        alias_unique_count=0,
        sibling_columns=sibling_columns,
    )

    body = types.SimpleNamespace(
        source_id=source.id,
        table_type="dim_aggregate",
        physical_name="public.cities",
        alias="payment_city",
        display_name="Payment City",
    )
    user = types.SimpleNamespace(tenant_id="t", user_id="u", email="u")
    await create_table(
        project_id=PROJECT_ID,
        model_id=model_id,
        source_id=source.id,
        body=body,  # type: ignore[arg-type]
        current_user=user,  # type: ignore[arg-type]
    )

    cloned = [
        c.args[0]
        for c in db.add.call_args_list
        if isinstance(c.args[0], ModelColumn)
    ]
    assert len(cloned) == 2
    cloned_by_name = {c.column_name: c for c in cloned}
    assert cloned_by_name["city_name"].display_name == "City"
    assert cloned_by_name["city_name"].description == "City label"
    assert cloned_by_name["city_id"].data_type == "integer"
    assert cloned_by_name["city_id"].is_primary_key is True
    # The cloned columns must point at the new alias, not the sibling.
    new_table = next(
        c.args[0]
        for c in db.add.call_args_list
        if c.args[0].__class__.__name__ == "ModelTable"
    )
    for col in cloned:
        assert col.model_table_id == new_table.id


async def test_create_table_rejects_duplicate_alias_in_model(monkeypatch):
    model_id = uuid.uuid4()
    source = _fake_source(model_id)
    _patch_create_table_db(
        monkeypatch,
        source=source,
        fact_count=0,
        path="explicit",
        alias_unique_count=1,  # the alias already exists in this model
    )

    body = types.SimpleNamespace(
        source_id=source.id,
        table_type="dim_aggregate",
        physical_name="public.cities",
        alias="payment_city",
        display_name="Payment City",
    )
    user = types.SimpleNamespace(tenant_id="t", user_id="u", email="u")
    with pytest.raises(HTTPException) as exc:
        await create_table(
            project_id=PROJECT_ID,
            model_id=model_id,
            source_id=source.id,
            body=body,  # type: ignore[arg-type]
            current_user=user,  # type: ignore[arg-type]
        )
    assert exc.value.status_code == 400
    assert "already in use" in exc.value.detail




# ---------------------------------------------------------------------------
# _resolve_calendar_alias_for_measure — body-FK ownership
# ---------------------------------------------------------------------------
#
# These used to drive the resolver with ``AsyncMock.get`` because the resolver
# hand-rolled its own ``db.get(...) ... != model_id`` pair. It now proves
# ownership through the canonical body-FK primitive in ``src/api/_scope.py``,
# which resolves the whole project -> model -> row chain inside ONE scoped
# SELECT and answers 422 with the family's uniform detail. The intent of every
# case below is unchanged (accept in-model, reject foreign, reject a plain
# table, reject an unknown base); what changed is that the double now really
# evaluates the ownership predicate instead of returning a canned row, so an
# accept-everything OR a deny-everything regression is visible.


def _alias_db():
    return ScopedFakeDB(project_id=PROJECT_ID, model_id=uuid.uuid4())


async def test_calendar_alias_none_passes_through_for_base():
    db = _alias_db()

    out = await _resolve_calendar_alias_for_measure(
        db,
        model_id=db.model_id,
        project_id=db.project_id,
        variant_kind=None,
        variant_of_measure_id=None,
        calendar_model_table_id=None,
    )
    assert out is None


async def test_calendar_alias_valid_returns_id():
    db = _alias_db()
    alias_table = db.add_table(
        model_id=db.model_id, calendar_table_id=uuid.uuid4(),
    )

    out = await _resolve_calendar_alias_for_measure(
        db,
        model_id=db.model_id,
        project_id=db.project_id,
        variant_kind=None,
        variant_of_measure_id=None,
        calendar_model_table_id=alias_table.id,
    )
    assert out == alias_table.id


async def test_calendar_alias_in_other_model_rejected():
    """A calendar alias owned by a SIBLING model in the SAME project is still
    refused — the near miss, not just the far one."""
    db = _alias_db()
    sibling_model = uuid.uuid4()
    db.register_model(sibling_model, db.project_id)
    foreign_alias = db.add_table(
        model_id=sibling_model, calendar_table_id=uuid.uuid4(),
    )

    with pytest.raises(HTTPException) as exc:
        await _resolve_calendar_alias_for_measure(
            db,
            model_id=db.model_id,
            project_id=db.project_id,
            variant_kind=None,
            variant_of_measure_id=None,
            calendar_model_table_id=foreign_alias.id,
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["error_code"] == "CALENDAR_ALIAS_TABLE_NOT_IN_MODEL"
    assert exc.value.detail["field"] == "calendar_model_table_id"


async def test_calendar_alias_unknown_and_foreign_project_are_indistinguishable():
    """A calendar alias in ANOTHER PROJECT is refused, and refused IDENTICALLY
    to an id that does not exist at all — the response must not tell the caller
    which of the two it was (Bug-7253's existence-oracle lesson)."""
    db = _alias_db()
    other_project = uuid.uuid4()
    other_model = uuid.uuid4()
    db.register_model(other_model, other_project)
    foreign_alias = db.add_table(
        model_id=other_model, calendar_table_id=uuid.uuid4(),
    )
    unknown_id = uuid.uuid4()

    details = []
    for candidate in (foreign_alias.id, unknown_id):
        with pytest.raises(HTTPException) as exc:
            await _resolve_calendar_alias_for_measure(
                db,
                model_id=db.model_id,
                project_id=db.project_id,
                variant_kind=None,
                variant_of_measure_id=None,
                calendar_model_table_id=candidate,
            )
        assert exc.value.status_code == 422
        details.append(dict(exc.value.detail))

    foreign, unknown = details
    # Everything except the echo of the caller's own id must be identical.
    assert foreign["error_code"] == unknown["error_code"]
    assert foreign["field"] == unknown["field"]
    assert foreign["message"].replace(str(foreign_alias.id), "X") == (
        unknown["message"].replace(str(unknown_id), "X")
    )


async def test_calendar_alias_without_calendar_binding_rejected():
    """A ModelTable that is not a calendar alias (calendar_table_id IS NULL)
    cannot be used as a measure's calendar — covers Q5/Q3.

    Distinct from the ownership rejection on purpose: this table IS in the
    model, so the ownership guard must ACCEPT it and the state check must be
    what refuses it. One merged error would hide an over-broad guard.
    """
    db = _alias_db()
    plain_table = db.add_table(model_id=db.model_id, calendar_table_id=None)

    with pytest.raises(HTTPException) as exc:
        await _resolve_calendar_alias_for_measure(
            db,
            model_id=db.model_id,
            project_id=db.project_id,
            variant_kind=None,
            variant_of_measure_id=None,
            calendar_model_table_id=plain_table.id,
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["error_code"] == "NOT_A_CALENDAR_ALIAS"
    assert "not a calendar alias" in exc.value.detail["message"]


# ---------------------------------------------------------------------------
# _resolve_calendar_alias_for_measure — variants inherit from base
# ---------------------------------------------------------------------------


async def test_variant_inherits_calendar_from_base():
    """Variant rows ignore any calendar_model_table_id they're handed and
    pick up the base measure's binding instead — Q4 (one calendar per
    measure, applied to every enabled variant)."""
    db = _alias_db()
    base_cal = uuid.uuid4()
    base = db.add_measure(
        model_id=db.model_id, calendar_model_table_id=base_cal,
    )

    out = await _resolve_calendar_alias_for_measure(
        db,
        model_id=db.model_id,
        project_id=db.project_id,
        variant_kind="ytd",
        variant_of_measure_id=base.id,
        calendar_model_table_id=uuid.uuid4(),  # ignored on variant rows
    )
    assert out == base_cal


async def test_period_aware_variant_allowed_without_calendar_on_base():
    """Period-aware variants no longer require calendar_model_table_id;
    period boundaries are computed from expressions on the hierarchy."""
    db = _alias_db()
    base = db.add_measure(model_id=db.model_id, calendar_model_table_id=None)

    out = await _resolve_calendar_alias_for_measure(
        db,
        model_id=db.model_id,
        project_id=db.project_id,
        variant_kind="ytd",
        variant_of_measure_id=base.id,
        calendar_model_table_id=None,
    )
    assert out is None


async def test_pure_window_variant_does_not_require_calendar_on_base():
    """``lag`` / ``trailing_n`` / ``moving_avg_n`` are pure-window — they
    can be created against a base that has no calendar bound."""
    db = _alias_db()
    base = db.add_measure(model_id=db.model_id, calendar_model_table_id=None)

    out = await _resolve_calendar_alias_for_measure(
        db,
        model_id=db.model_id,
        project_id=db.project_id,
        variant_kind="lag",
        variant_of_measure_id=base.id,
        calendar_model_table_id=None,
    )
    assert out is None


async def test_variant_against_unknown_base_is_rejected():
    db = _alias_db()

    with pytest.raises(HTTPException) as exc:
        await _resolve_calendar_alias_for_measure(
            db,
            model_id=db.model_id,
            project_id=db.project_id,
            variant_kind="ytd",
            variant_of_measure_id=uuid.uuid4(),
            calendar_model_table_id=None,
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["error_code"] == "VARIANT_BASE_NOT_IN_MODEL"
    assert exc.value.detail["field"] == "variant_of_measure_id"


async def test_variant_base_in_another_project_is_rejected():
    """The defect this lane closes: a base measure in a project the caller has
    no binding for must not become a variant's parent."""
    db = _alias_db()
    other_project = uuid.uuid4()
    other_model = uuid.uuid4()
    db.register_model(other_model, other_project)
    foreign_base = db.add_measure(
        model_id=other_model, calendar_model_table_id=uuid.uuid4(),
    )

    with pytest.raises(HTTPException) as exc:
        await _resolve_calendar_alias_for_measure(
            db,
            model_id=db.model_id,
            project_id=db.project_id,
            variant_kind="ytd",
            variant_of_measure_id=foreign_base.id,
            calendar_model_table_id=None,
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["error_code"] == "VARIANT_BASE_NOT_IN_MODEL"
