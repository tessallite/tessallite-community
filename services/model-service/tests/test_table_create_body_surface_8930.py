"""Bug-8930 / Bug-8876: the create-table body must not advertise inputs the
handler never reads.

``ModelTableCreate`` used to declare BOTH ``source_id`` and
``calendar_table_id``. ``create_table`` builds its ``ModelTable`` from the PATH
``source_id`` and never referenced either body value, so a client could send a
``source_id`` for a different source, or a ``calendar_table_id`` for any
calendar at all, receive a 201, and get a row bound to neither. An advertised
input that is silently ignored is worse than no input: the caller has no way to
learn its request did something other than what it asked for.

Both fields were REMOVED rather than honoured. Honouring them would have meant
adding an ownership guard for a capability nobody used: the calendar binding is
DERIVED by ``calendar.py`` (``auto_register_calendar_from_classification``), and
the only client-driven binding path is the PATCH, which is already guarded by
``ensure_calendar_table_in_model`` (Bug-8878).

Test escape: no test ever asserted what the create body was ALLOWED to contain,
only that a well-formed body produced a table. A field could therefore be added
to the schema and never wired to anything without a single test noticing.

Guard: these tests. Tier: T1 (producer/consumer contract).
"""
from __future__ import annotations

import types
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.db.models import DataSource, Model, ModelTable
from shared.schemas.pydantic_models import ModelTableCreate, ModelTableUpdate
from src.api.tables import ApplyClassificationRequest, create_table

pytestmark = pytest.mark.unit

TENANT = types.SimpleNamespace(tenant_id="acme")
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
PROJECT_ID = uuid.uuid4()
MODEL_ID = uuid.uuid4()
PATH_SOURCE_ID = uuid.uuid4()
# A source and a calendar the caller might try to smuggle in through the body.
OTHER_SOURCE_ID = uuid.uuid4()
SOME_CALENDAR_ID = uuid.uuid4()


# ---------------------------------------------------------------------------
# Schema surface — the contract clients read
# ---------------------------------------------------------------------------

def test_create_body_does_not_declare_source_id_or_calendar_table_id():
    """The removed fields must be gone from the schema, not merely unused.

    While they were declared, generated clients and the OpenAPI document told
    callers these were meaningful inputs.
    """
    fields = set(ModelTableCreate.model_fields)
    assert "source_id" not in fields
    assert "calendar_table_id" not in fields
    # The fields the handler actually reads are still required/available.
    assert {"table_type", "physical_name", "display_name"} <= fields


def test_update_body_still_declares_calendar_table_id():
    """The PATCH is the SUPPORTED calendar-binding path and is guarded
    (Bug-8878). Removing the create-side field must not remove it."""
    assert "calendar_table_id" in ModelTableUpdate.model_fields


def test_table_type_enum_is_validated_on_create():
    """F-013-08 (Bug-9118): a non-canonical table_type must 422 at the boundary.

    The one-fact-per-model cap compares EXACTLY to "fact", so a raw-str "Fact"
    would persist a second fact-like table the cap cannot see.
    """
    from pydantic import ValidationError

    for good in ("fact", "dim_aggregate", "dim_detail"):
        ModelTableCreate.model_validate(
            {"table_type": good, "physical_name": "public.t", "display_name": "T"}
        )
    for bad in ("Fact", "FACT", "measure", "dimension", ""):
        with pytest.raises(ValidationError):
            ModelTableCreate.model_validate(
                {"table_type": bad, "physical_name": "public.t", "display_name": "T"}
            )


def test_table_type_enum_is_validated_on_update():
    """F-013-08: the PATCH body enforces the same enum; None leaves it unchanged."""
    from pydantic import ValidationError

    ModelTableUpdate.model_validate({})  # all-optional, table_type omitted
    ModelTableUpdate.model_validate({"table_type": None})
    ModelTableUpdate.model_validate({"table_type": "dim_aggregate"})
    with pytest.raises(ValidationError):
        ModelTableUpdate.model_validate({"table_type": "Fact"})


def test_bug_8626_apply_classification_reuses_table_type_domain():
    """The second public table-type writer cannot reintroduce a free string."""
    from pydantic import ValidationError

    for good in ("fact", "dim_aggregate", "dim_detail", None):
        ApplyClassificationRequest.model_validate(
            {"table_type": good, "overrides": []}
        )
    for bad in ("Fact", "FACT", "dimension", "calendar", "unclassified", ""):
        with pytest.raises(ValidationError):
            ApplyClassificationRequest.model_validate(
                {"table_type": bad, "overrides": []}
            )


def test_removed_fields_are_not_silently_accepted_as_attributes():
    """Pydantic ignores unknown keys, so a stale client still gets a 201 — but
    the parsed body must carry no trace of them, so no future handler edit can
    accidentally start reading a value the API never promised to honour."""
    body = ModelTableCreate.model_validate(
        {
            "source_id": str(OTHER_SOURCE_ID),
            "calendar_table_id": str(SOME_CALENDAR_ID),
            "table_type": "dim_detail",
            "physical_name": "public.customers",
            "display_name": "Customers",
        }
    )
    assert not hasattr(body, "source_id")
    assert not hasattr(body, "calendar_table_id")
    assert body.model_dump() == {
        "table_type": "dim_detail",
        "physical_name": "public.customers",
        "alias": None,
        "display_name": "Customers",
        "description": None,
    }


# ---------------------------------------------------------------------------
# Behaviour — the supported path still works, and the stale keys change nothing
# ---------------------------------------------------------------------------

def _db():
    """A session whose model/source are consistent with the path."""
    db = MagicMock()
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    async def _get(entity, entity_id):
        if entity is Model:
            return types.SimpleNamespace(id=entity_id, project_id=PROJECT_ID)
        if entity is DataSource:
            return types.SimpleNamespace(id=entity_id, model_id=MODEL_ID)
        return None

    db.get = AsyncMock(side_effect=_get)

    # Every SELECT in create_table (fact check, alias uniqueness, auto-alias
    # derivation, sibling-column clone) resolves to "nothing found".
    empty = types.SimpleNamespace(
        scalar_one_or_none=lambda: None,
        scalars=lambda: types.SimpleNamespace(all=lambda: []),
        scalar=lambda: 0,
        all=lambda: [],
    )
    db.execute = AsyncMock(return_value=empty)

    class _Nested:
        async def __aenter__(self_inner):
            return self_inner

        async def __aexit__(self_inner, *exc):
            return False

    db.begin_nested = MagicMock(return_value=_Nested())
    return db


async def _run_create(body: dict) -> ModelTable:
    """Invoke the real handler with a raw body dict and return the built row."""
    db = _db()
    created: list[ModelTable] = []
    real_add = db.add

    def _capture(obj):
        if isinstance(obj, ModelTable):
            # Stand in for the server-generated columns the real INSERT fills,
            # so the handler's closing ModelTableResponse.model_validate(...)
            # succeeds and the test exercises the WHOLE handler, not a prefix.
            obj.id = uuid.uuid4()
            obj.created_at = NOW
            obj.updated_at = NOW
            obj.row_count_estimate = None
            obj.last_stats_at = None
            created.append(obj)
        return real_add(obj)

    db.add = MagicMock(side_effect=_capture)

    async def _gen(_tenant):
        yield db

    with patch("src.api.tables.get_tenant_db", _gen), patch(
        "src.api.tables.acquire_model_definition_lock", AsyncMock()
    ):
        await create_table(
            PROJECT_ID,
            MODEL_ID,
            PATH_SOURCE_ID,
            ModelTableCreate.model_validate(body),
            current_user=TENANT,
        )

    assert created, "create_table did not build a ModelTable"
    return created[0]


@pytest.mark.asyncio
async def test_create_binds_the_path_source_not_a_body_source_id():
    """A stale client still sending `source_id` must not be able to steer the
    binding. This is the exact divergence the removal closes: before, the field
    was accepted and ignored, so the caller's stated intent and the persisted
    row disagreed with no error."""
    table = await _run_create(
        {
            "source_id": str(OTHER_SOURCE_ID),  # ignored
            "table_type": "dim_detail",
            "physical_name": "public.customers",
            "display_name": "Customers",
        }
    )
    assert table.source_id == PATH_SOURCE_ID
    assert table.source_id != OTHER_SOURCE_ID
    assert table.model_id == MODEL_ID


@pytest.mark.asyncio
async def test_create_never_binds_a_body_calendar_table_id():
    """A create must never establish a calendar binding from the request body.
    The binding is derived (calendar.py) or set through the guarded PATCH."""
    table = await _run_create(
        {
            "calendar_table_id": str(SOME_CALENDAR_ID),  # ignored
            "table_type": "dim_detail",
            "physical_name": "public.customers",
            "display_name": "Customers",
        }
    )
    assert getattr(table, "calendar_table_id", None) is None


@pytest.mark.asyncio
async def test_create_supported_path_still_works():
    """The fields the handler does read are still honoured end to end."""
    table = await _run_create(
        {
            "table_type": "dim_detail",
            "physical_name": "public.customers",
            "display_name": "Customers",
            "alias": "customers",
        }
    )
    assert table.table_type == "dim_detail"
    assert table.physical_name == "public.customers"
    assert table.display_name == "Customers"
    assert table.alias == "customers"
