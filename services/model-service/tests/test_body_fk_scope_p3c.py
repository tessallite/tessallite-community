"""P3-c: body-supplied foreign keys must be bound to the path project + model.

``require_role`` (``src/auth/rbac.py:126``) reads ``project_id`` from the URL
PATH and checks the CALLER'S binding for that project. It never proves that a
resource named in the REQUEST BODY belongs to that project or model, so a
modeler holding a legitimate binding in project A could submit project B's UUID
in a body field and have it persisted. Path parameters were closed by Bug-8862;
this file covers the body-FK half for four route modules:

* ``kpis.py``       — ``time_dimension_id`` on create and PATCH, plus the
                      revert path that is the same column's second writer
* ``models.py``     — ``target_id`` on PATCH (the model's DEFAULT materialisation
                      destination, inherited by aggregates and pockets)
* ``data_quality.py`` — the polymorphic ``(target_type, target_id)`` pair
* ``downstream_assets.py`` — the ``column_ids`` collection on create and PUT

Every denial test asserts the rejection REASON — ``error_code``, ``field`` and
the echoed ``ids`` — not merely the status code. A bare status assertion can
pass against the pre-fix handler for an unrelated reason, which is the trap the
Bug-8862 suite was written to avoid.

Every guarded site also has a POSITIVE test. Bug-8864 shipped a scope guard that
denied essentially every table; only a positive test catches that direction.

A mocked session does not evaluate a WHERE clause, so these tests prove that the
HANDLER calls the guard, in the right order, and surfaces its error — not that
the ownership predicate itself is correct. That second half is proved against
real Postgres in ``tests/integration/test_body_fk_route_adoption_db.py``.

Guard: this file + that one. Tier: T3 (cross-project isolation).
Test escape: no test ever sent ``time_dimension_id``, ``target_id`` or a foreign
``column_ids`` entry on any of these bodies, so the fields were written by splat
and setattr loops that nothing inspected.
"""
from __future__ import annotations

import importlib
import types
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy.sql.dml import Delete, Insert

from shared.db.models import DataQualityRule, DownstreamAsset, KPI, Model
from shared.schemas.pydantic_models import (
    DataQualityRuleCreate,
    DownstreamAssetCreate,
    DownstreamAssetUpdate,
    KPICreate,
    KPIUpdate,
    ModelUpdate,
)

from .conftest import make_model

pytestmark = pytest.mark.unit

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
TENANT = types.SimpleNamespace(
    tenant_id="acme", email="u@test", role="modeler", user_id="u@test",
)

PROJECT_ID = uuid.uuid4()
MODEL_ID = uuid.uuid4()
FOREIGN_ID = uuid.uuid4()
OWNED_ID = uuid.uuid4()


def _scoped_db(*, resolves, model_project_id=PROJECT_ID):
    """A session double whose FIRST ``execute`` is the body-FK guard's SELECT.

    Used by the handlers that reach their guard before issuing any other query
    (``create_kpi``, ``update_kpi``, ``update_model``, ``create_rule``,
    ``create_downstream_asset``). ``update_downstream_asset`` loads its asset
    first and uses ``_asset_db`` instead.

    ``resolves`` is what that scoped query finds — a row for "owned by this
    project+model", ``None`` for "no such row inside this project+model", which
    is the single outcome the primitive produces for an unknown id and a foreign
    id alike (its anti-oracle property).

    Every later ``execute`` falls through to a permissive default so the
    handler's own machinery (slug checks, cache invalidation, response
    decoration) can run on the accept path.
    """
    db = AsyncMock()
    default = MagicMock()
    default.scalar_one_or_none.return_value = None
    default.scalar.return_value = 0
    default.scalars.return_value.all.return_value = []
    default.scalars.return_value.one_or_none.return_value = None
    default.all.return_value = []

    calls = {"n": 0}

    async def _execute(*_a, **_kw):
        calls["n"] += 1
        if calls["n"] == 1:
            found = MagicMock()
            found.scalars.return_value.one_or_none.return_value = resolves
            found.scalars.return_value.all.return_value = (
                list(resolves) if isinstance(resolves, list)
                else ([resolves] if resolves is not None else [])
            )
            return found
        return default

    db.execute = AsyncMock(side_effect=_execute)
    db.add = MagicMock()  # Session.add is sync; an AsyncMock child leaks a coroutine
    db.commit = AsyncMock()
    db.flush = AsyncMock()
    db.refresh = AsyncMock()
    db.delete = AsyncMock()

    async def _get(entity, entity_id):
        if entity is Model:
            if model_project_id is None:
                return None
            # The shared factory, so a new Model column does not turn these
            # tests into a ValidationError on ModelResponse.
            return make_model(model_id=entity_id, project_id=model_project_id)
        return None

    db.get = AsyncMock(side_effect=_get)
    return db


def _tenant_patch(module_path, db):
    async def _tenant_db(_tenant_id):
        yield db

    return patch(f"{module_path}.get_tenant_db", new=_tenant_db)


@contextmanager
def _spy(module_path, name):
    """Record every call to a ``_scope`` guard while still running the real one.

    "Was the guard invoked at all?" cannot be answered by counting ``execute``
    calls: these handlers issue several unrelated queries (slug uniqueness,
    response decoration, cache invalidation), so a query counter says 3 whether
    or not the guard ran. Wrapping the guard itself makes "not supplied ⇒ never
    looked up" and "explicit null ⇒ looked up, resolved to None" separately
    assertable, which is the distinction ``exclude_unset`` exists to carry.
    """
    module = importlib.import_module(module_path)
    real = getattr(module, name)
    calls: list[dict] = []

    async def _wrapper(*args, **kwargs):
        calls.append(kwargs)
        return await real(*args, **kwargs)

    with patch(f"{module_path}.{name}", new=_wrapper):
        yield calls


def _assert_body_fk_rejection(exc, *, error_code, field, bad_id):
    """Assert the REASON, not just the status.

    The ``detail`` dict is the ``data_tags.py`` error shape the whole ``_scope``
    body-FK family raises; a client branches on ``error_code`` and several
    frontend panels render ``detail.message``.
    """
    assert exc.status_code == 422, (
        "body FKs answer 422 — the addressed PATH resource is fine and already "
        "proven to belong to the caller; the submitted PAYLOAD is not"
    )
    detail = exc.detail
    assert isinstance(detail, dict), (
        "must be the body-FK error shape, not a bare string or FastAPI's own "
        "422 validation list"
    )
    assert detail["error_code"] == error_code
    assert detail["field"] == field
    assert detail["ids"] == [str(bad_id)]
    assert detail["message"]


# ---------------------------------------------------------------------------
# downstream_assets.py — the ``column_ids`` COLLECTION
# ---------------------------------------------------------------------------
#
# The replaced lookup was ``select(ModelColumn).where(ModelColumn.id.in_(ids))``
# with no ownership predicate at all. ``governance_exporter.py`` walks the
# resulting ``downstream_asset_columns`` rows into the Collibra / Solidatus
# governance graph, so a foreign column's identity was publishable to an
# external catalogue as a consumer of THIS model.


def _asset(columns=None):
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        model_id=MODEL_ID,
        asset_type="dashboard",
        asset_name="Sales Dashboard",
        asset_url=None,
        owner=None,
        notes=None,
        created_at=NOW,
        updated_at=NOW,
        columns=columns or [],
    )


def _asset_db(asset, *, resolves):
    """A session double for ``update_downstream_asset``.

    That handler issues THREE kinds of query and which ones run depends on
    whether ``column_ids`` was sent. Rather than hard-code a call ORDER the
    tests would then silently depend on, one result double answers every shape:

    * ``scalars().one_or_none()`` — the SCOPED asset SELECT. It is a scoped
      select, not ``db.get``, because the ownership predicate has to be in the
      query: loading by id and comparing ``asset.model_id`` afterwards has
      already read another project's asset row.
    * ``scalars().all()`` — whatever the body-FK guard is meant to resolve.
    * ``.all()`` — ``_owned_column_ids``, which reports the stored associations
      this project and model own. Modelled as the asset's own columns, since a
      fake cannot evaluate the ownership join; that half is proved against real
      Postgres in ``tests/integration/test_read_path_project_scope_db.py``.

    Whether the guard ran at all is asserted through ``_spy``, not a counter.
    """
    db = _scoped_db(resolves=resolves)
    result = MagicMock()
    result.scalars.return_value.one_or_none.return_value = asset
    result.scalars.return_value.all.return_value = (
        list(resolves) if isinstance(resolves, list)
        else ([resolves] if resolves is not None else [])
    )
    result.all.return_value = [(c, asset.id) for c in asset.columns]
    db.execute = AsyncMock(return_value=result)

    async def _get(entity, entity_id):
        if entity is Model:
            return make_model(model_id=entity_id, project_id=PROJECT_ID)
        return None

    db.get = AsyncMock(side_effect=_get)
    return db


def _association_dml(db):
    """``(delete_count, inserted_column_ids)`` on ``downstream_asset_columns``.

    The handler rewrites the association rows with explicit DML rather than
    assigning to ``asset.columns``: assigning makes SQLAlchemy load the
    EXISTING collection to compute the delta, which reads every associated
    ``ModelColumn`` — including a legacy one belonging to another project — and
    on a fresh async session that load is lazy (the MissingGreenlet that
    answered HTTP 500 for every PUT carrying column_ids).
    """
    deletes = 0
    inserted: list = []
    for call in db.execute.await_args_list:
        stmt = call.args[0]
        table = getattr(stmt, "table", None)
        if getattr(table, "name", None) != "downstream_asset_columns":
            continue
        if isinstance(stmt, Delete):
            deletes += 1
        elif isinstance(stmt, Insert):
            params = dict(stmt.compile().params)
            inserted.extend(
                v for k, v in sorted(params.items())
                if k.startswith("model_column_id")
            )
    return deletes, inserted


async def test_create_downstream_asset_refuses_a_column_outside_the_model():
    from src.api.downstream_assets import create_downstream_asset

    db = _scoped_db(resolves=None)
    body = DownstreamAssetCreate(
        asset_type="dashboard", asset_name="D", column_ids=[FOREIGN_ID],
    )
    with _tenant_patch("src.api.downstream_assets", db):
        with pytest.raises(HTTPException) as exc:
            await create_downstream_asset(
                PROJECT_ID, MODEL_ID, body, current_user=TENANT,
            )

    _assert_body_fk_rejection(
        exc.value, error_code="REFS_NOT_IN_MODEL", field="column_ids",
        bad_id=FOREIGN_ID,
    )
    # Ordering: the guard runs before the row is built and added, not merely
    # before commit. The helper's SELECT autoflushes, so a guard placed after
    # ``db.add`` would already have sent the association to the database.
    db.add.assert_not_called()
    db.commit.assert_not_awaited()


async def test_create_downstream_asset_accepts_a_column_owned_by_the_model():
    """The guard is an ownership check, not a blanket denial, and the accepted
    column is the one that actually lands on the association."""
    from src.api.downstream_assets import create_downstream_asset

    column = types.SimpleNamespace(id=OWNED_ID)
    db = _scoped_db(resolves=[column])

    async def _refresh(obj):
        obj.id = uuid.uuid4()
        obj.created_at = NOW
        obj.updated_at = NOW

    db.refresh = AsyncMock(side_effect=_refresh)
    body = DownstreamAssetCreate(
        asset_type="dashboard", asset_name="D", column_ids=[OWNED_ID],
    )
    with _tenant_patch("src.api.downstream_assets", db):
        resp = await create_downstream_asset(
            PROJECT_ID, MODEL_ID, body, current_user=TENANT,
        )

    assert resp.column_ids == [OWNED_ID]
    db.add.assert_called_once()
    assert db.add.call_args[0][0].columns == [column]
    db.commit.assert_awaited()


async def test_create_downstream_asset_with_no_columns_issues_no_lookup():
    """``column_ids`` defaults to ``[]``; an empty collection is not a scope
    violation and must not cost a query or a rejection."""
    from src.api.downstream_assets import create_downstream_asset

    db = _scoped_db(resolves=None)

    async def _refresh(obj):
        obj.id = uuid.uuid4()
        obj.created_at = NOW
        obj.updated_at = NOW

    db.refresh = AsyncMock(side_effect=_refresh)
    spy = _spy("src.api.downstream_assets", "ensure_refs_in_model")
    with _tenant_patch("src.api.downstream_assets", db), spy as guard:
        resp = await create_downstream_asset(
            PROJECT_ID, MODEL_ID,
            DownstreamAssetCreate(asset_type="report", asset_name="R"),
            current_user=TENANT,
        )

    assert resp.column_ids == []
    # Called unconditionally — there is no ``if body.column_ids:`` branch a
    # future edit could forget to re-add the guard to — and an empty collection
    # is simply not a scope violation.
    assert [c["ref_ids"] for c in guard] == [[]]
    db.commit.assert_awaited()


async def test_create_downstream_asset_refuses_the_whole_partly_foreign_batch():
    """All-or-nothing. The replaced query silently DROPPED unresolvable ids, so
    a request naming two columns could persist one and still answer 201 — the
    modeller was never told which reference was wrong."""
    from src.api.downstream_assets import create_downstream_asset

    owned = types.SimpleNamespace(id=OWNED_ID)
    db = _scoped_db(resolves=[owned])  # only ONE of the two ids resolves
    body = DownstreamAssetCreate(
        asset_type="dashboard", asset_name="D",
        column_ids=[OWNED_ID, FOREIGN_ID],
    )
    with _tenant_patch("src.api.downstream_assets", db):
        with pytest.raises(HTTPException) as exc:
            await create_downstream_asset(
                PROJECT_ID, MODEL_ID, body, current_user=TENANT,
            )

    _assert_body_fk_rejection(
        exc.value, error_code="REFS_NOT_IN_MODEL", field="column_ids",
        bad_id=FOREIGN_ID,
    )
    db.add.assert_not_called()
    db.commit.assert_not_awaited()


async def test_update_downstream_asset_refuses_a_column_outside_the_model():
    from src.api.downstream_assets import update_downstream_asset

    asset = _asset()
    db = _asset_db(asset, resolves=None)
    with _tenant_patch("src.api.downstream_assets", db):
        with pytest.raises(HTTPException) as exc:
            await update_downstream_asset(
                PROJECT_ID, MODEL_ID, asset.id,
                DownstreamAssetUpdate(asset_name="Renamed", column_ids=[FOREIGN_ID]),
                current_user=TENANT,
            )

    _assert_body_fk_rejection(
        exc.value, error_code="REFS_NOT_IN_MODEL", field="column_ids",
        bad_id=FOREIGN_ID,
    )
    # Ordering: the guard is hoisted above the scalar setattr loop, because the
    # helper's SELECT autoflushes and would otherwise carry this request's
    # rename to the database before the columns could be refused.
    assert asset.asset_name == "Sales Dashboard"
    assert asset.columns == []
    db.commit.assert_not_awaited()


async def test_update_downstream_asset_accepts_a_column_owned_by_the_model():
    from src.api.downstream_assets import update_downstream_asset

    asset = _asset()
    column = types.SimpleNamespace(id=OWNED_ID)
    db = _asset_db(asset, resolves=[column])
    with _tenant_patch("src.api.downstream_assets", db):
        resp = await update_downstream_asset(
            PROJECT_ID, MODEL_ID, asset.id,
            DownstreamAssetUpdate(asset_name="Renamed", column_ids=[OWNED_ID]),
            current_user=TENANT,
        )

    assert resp.column_ids == [OWNED_ID]
    assert asset.asset_name == "Renamed"
    assert _association_dml(db) == (1, [OWNED_ID]), (
        "the accepted column is the one that lands on the association, and "
        "the old rows are cleared first"
    )
    db.commit.assert_awaited()


async def test_update_downstream_asset_still_detaches_every_column():
    """``[]`` is falsy but is NOT "not supplied" — it means "detach them all",
    and the guard keys on ``is not None`` so it stays legal."""
    from src.api.downstream_assets import update_downstream_asset

    asset = _asset(columns=[types.SimpleNamespace(id=OWNED_ID)])
    db = _asset_db(asset, resolves=None)
    spy = _spy("src.api.downstream_assets", "ensure_refs_in_model")
    with _tenant_patch("src.api.downstream_assets", db), spy as guard:
        resp = await update_downstream_asset(
            PROJECT_ID, MODEL_ID, asset.id,
            DownstreamAssetUpdate(column_ids=[]), current_user=TENANT,
        )

    assert resp.column_ids == []
    assert _association_dml(db) == (1, []), (
        "every association row is deleted and none is written back"
    )
    assert [c["ref_ids"] for c in guard] == [[]]
    db.commit.assert_awaited()


async def test_update_downstream_asset_without_the_field_leaves_columns_alone():
    """``None`` means the field was not sent: the existing associations survive
    and the guard is never consulted. That is this schema's current contract and
    the fix does not change it."""
    from src.api.downstream_assets import update_downstream_asset

    existing = types.SimpleNamespace(id=OWNED_ID)
    asset = _asset(columns=[existing])
    db = _asset_db(asset, resolves=None)
    spy = _spy("src.api.downstream_assets", "ensure_refs_in_model")
    with _tenant_patch("src.api.downstream_assets", db), spy as guard:
        resp = await update_downstream_asset(
            PROJECT_ID, MODEL_ID, asset.id,
            DownstreamAssetUpdate(asset_name="Renamed"), current_user=TENANT,
        )

    assert resp.column_ids == [OWNED_ID]
    assert _association_dml(db) == (0, []), (
        "a PATCH that does not mention the columns must not touch the "
        "association rows at all"
    )
    assert guard == [], "a PATCH that does not mention the columns needs no lookup"
    db.commit.assert_awaited()


# ---------------------------------------------------------------------------
# data_quality.py — the POLYMORPHIC ``(target_type, target_id)`` pair
# ---------------------------------------------------------------------------
#
# ``shared/data_quality/validator.py::_resolve_column_ref`` dereferences
# ``rule.target_id`` with a bare ``db.get(ModelColumn, ...)`` and NO ownership
# re-check, walks it to its ModelTable's ``physical_name``, and issues a
# COUNT/GROUP BY against that table through /introspect using a ``system_admin``
# service token. A foreign target turned "add a not-null rule" into a query over
# another project's physical table, with the row count and up to ten sample
# VALUES persisted into this model's violation rows.


def _dq_body(target_type="column", target_id=None):
    return DataQualityRuleCreate(
        name="not-null customer id",
        target_type=target_type,
        target_id=target_id or FOREIGN_ID,
        rule_type="not_null",
    )


def _dq_patches(db):
    return (
        _tenant_patch("src.api.data_quality", db),
        patch(
            "src.api.data_quality.acquire_model_definition_lock", new=AsyncMock()
        ),
    )


@pytest.mark.parametrize("target_type", ["column", "dimension", "measure"])
async def test_create_rule_refuses_a_target_outside_the_model(target_type):
    """Every declared target type is scoped, not just the column one the
    validator happens to dereference today."""
    from src.api.data_quality import create_rule

    db = _scoped_db(resolves=None)
    tenant, lock = _dq_patches(db)
    with tenant, lock, pytest.raises(HTTPException) as exc:
        await create_rule(
            PROJECT_ID, MODEL_ID, _dq_body(target_type), current_user=TENANT,
        )

    _assert_body_fk_rejection(
        exc.value, error_code="TARGET_NOT_IN_MODEL", field="target_id",
        bad_id=FOREIGN_ID,
    )
    db.add.assert_not_called()
    db.commit.assert_not_awaited()


async def test_create_rule_accepts_a_target_owned_by_the_model():
    from src.api.data_quality import create_rule

    db = _scoped_db(resolves=types.SimpleNamespace(id=OWNED_ID))

    async def _refresh(obj):
        obj.id = uuid.uuid4()
        obj.created_at = NOW
        obj.updated_at = NOW
        obj.last_validated_at = None
        obj.last_violation_count = None

    db.refresh = AsyncMock(side_effect=_refresh)
    tenant, lock = _dq_patches(db)
    with tenant, lock:
        resp = await create_rule(
            PROJECT_ID, MODEL_ID, _dq_body(target_id=OWNED_ID),
            current_user=TENANT,
        )

    assert resp.target_id == OWNED_ID
    assert resp.target_type == "column"
    db.add.assert_called_once()
    assert isinstance(db.add.call_args[0][0], DataQualityRule)
    db.commit.assert_awaited()


def test_dq_rule_target_map_covers_exactly_the_schemas_vocabulary():
    """Coverage-tool blind-spot guard.

    ``_DQ_RULE_TARGETS`` is a small enumeration mechanism, so it is itself a
    place a blind spot can hide: a target type added to the request schema but
    not to the map would reach ``ensure_target_in_model`` as an unrecognised
    type. That fails CLOSED — but as a 422 the modeller cannot act on, for a
    target type the product says is legal. Pinning the two sets equal turns that
    into a failing test at the moment the vocabulary is widened.
    """
    from shared.schemas.domains.governance_advanced import _DQ_TARGET_TYPES
    from src.api.data_quality import _DQ_RULE_TARGETS

    assert set(_DQ_RULE_TARGETS) == set(_DQ_TARGET_TYPES)
    assert all(entity is not None for entity in _DQ_RULE_TARGETS.values()), (
        "a data-quality rule always names a row; unlike glossary attachments "
        "there is no id-less target type here"
    )


# ---------------------------------------------------------------------------
# models.py — ``target_id`` on PATCH
# ---------------------------------------------------------------------------
#
# ``models.target_id`` is the model's DEFAULT materialisation destination, so it
# is inherited by aggregates and pockets created afterwards rather than scoped
# to one definition. A DataTarget carries ``project_connection_id`` — live,
# Fernet-encrypted source credentials (Bug-5325).


def _models_patches(db):
    return (
        _tenant_patch("src.api.models", db),
        patch("src.api.models.acquire_model_definition_lock", new=AsyncMock()),
        patch("src.api.models.audit", new=AsyncMock()),
    )


async def test_bug_8955_update_model_refuses_a_target_outside_the_project():
    from src.api.models import update_model

    db = _scoped_db(resolves=None)
    tenant, lock, aud = _models_patches(db)
    with tenant, lock, aud, pytest.raises(HTTPException) as exc:
        await update_model(
            PROJECT_ID, MODEL_ID,
            ModelUpdate(target_id=FOREIGN_ID, display_name="Renamed"),
            current_user=TENANT,
        )

    _assert_body_fk_rejection(
        exc.value, error_code="REF_NOT_IN_MODEL", field="target_id",
        bad_id=FOREIGN_ID,
    )
    db.commit.assert_not_awaited()


async def test_bug_8955_update_model_accepts_a_target_owned_by_the_model():
    from src.api.models import update_model

    target = types.SimpleNamespace(id=OWNED_ID)
    db = _scoped_db(resolves=target)
    tenant, lock, aud = _models_patches(db)
    with tenant, lock, aud:
        resp = await update_model(
            PROJECT_ID, MODEL_ID, ModelUpdate(target_id=OWNED_ID),
            current_user=TENANT,
        )

    assert resp.target_id == OWNED_ID
    db.commit.assert_awaited()


async def test_update_model_still_clears_the_default_target():
    """An explicit ``null`` means "no default target" and stays legal —
    ``targets.py``'s delete path sets exactly this. The guard keys on the field
    being PRESENT, not on it being truthy."""
    from src.api.models import update_model

    db = _scoped_db(resolves=None)
    body = ModelUpdate(target_id=None)
    assert "target_id" in body.model_dump(exclude_unset=True)
    tenant, lock, aud = _models_patches(db)
    with tenant, lock, aud, _spy("src.api.models", "ensure_ref_in_model") as guard:
        resp = await update_model(
            PROJECT_ID, MODEL_ID, body, current_user=TENANT,
        )

    assert resp.target_id is None
    # The guard IS consulted — the field was sent — and returns None for an
    # absent optional reference rather than raising. ``None`` never means "not
    # validated" in this family; every failure raises.
    assert [c["ref_id"] for c in guard] == [None]
    db.commit.assert_awaited()


async def test_update_model_without_the_field_issues_no_target_lookup():
    """A PATCH that never mentions the target must not be validated against it —
    ``exclude_unset`` is what separates "clear it" from "not mentioned"."""
    from src.api.models import update_model

    db = _scoped_db(resolves=None)
    tenant, lock, aud = _models_patches(db)
    with tenant, lock, aud, _spy("src.api.models", "ensure_ref_in_model") as guard:
        resp = await update_model(
            PROJECT_ID, MODEL_ID, ModelUpdate(display_name="Renamed"),
            current_user=TENANT,
        )

    assert resp.display_name == "Renamed"
    assert guard == []
    db.commit.assert_awaited()


# ---------------------------------------------------------------------------
# kpis.py — ``time_dimension_id``
# ---------------------------------------------------------------------------
#
# The third body foreign key on KPICreate/KPIUpdate and the only one that was
# never checked: ``parent_kpi_id`` and ``target_measure_id`` each have a
# hand-rolled guard. ``_resolve_time_column`` dereferences it with a bare
# ``db.get(Dimension, kpi.time_dimension_id)`` and the result becomes the time
# column of the compiled KPI SQL, so a foreign binding is a wrong number as well
# as a cross-project leak.


def _kpi_patches(db):
    return (
        _tenant_patch("src.api.kpis", db),
        patch("src.api.kpis.acquire_model_definition_lock", new=AsyncMock()),
        patch("src.api.kpis.audit", new=AsyncMock()),
        patch("src.api.kpis._create_kpi_version", new=AsyncMock()),
        patch("src.api.kpis._invalidate_kpi_and_dependents", new=AsyncMock()),
    )


def _kpi_row(**overrides):
    row = dict(
        id=uuid.uuid4(),
        model_id=MODEL_ID,
        name="Revenue",
        display_name=None,
        description=None,
        display_folder=None,
        kpi_type=None,
        expression=None,
        calc_agg_mode="automatic",
        inner_agg=None,
        inner_grain=None,
        outer_agg=None,
        at_grain=None,
        non_additive_agg=None,
        carry_forward=False,
        target_type=None,
        target_value=None,
        target_measure_id=None,
        target_expression=None,
        target_period=None,
        direction="higher_is_better",
        presentation_type=None,
        presentation_meta=None,
        trend_period="month",
        trend_threshold=0.01,
        trend_sparkline_periods=12,
        format_token=None,
        format_custom=None,
        unit_label=None,
        null_display_value="N/A",
        weight=None,
        parent_kpi_id=None,
        indicator_type="none",
        time_dimension_id=None,
        business_definition=None,
        certification_status="draft",
        owner_user_id=None,
        replacement_id=None,
        is_deployed=False,
        deployed_at=None,
        snapshot_frequency=None,
        snapshot_retention=90,
        status_graphic="Traffic Light",
        trend_graphic="Standard Arrow",
        created_by="u@test",
        created_at=NOW,
        updated_at=NOW,
    )
    row.update(overrides)
    return types.SimpleNamespace(**row)


async def test_create_kpi_refuses_a_time_dimension_outside_the_model():
    from src.api.kpis import create_kpi

    db = _scoped_db(resolves=None)
    patches = _kpi_patches(db)
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        with pytest.raises(HTTPException) as exc:
            await create_kpi(
                PROJECT_ID, MODEL_ID,
                KPICreate(name="Revenue", time_dimension_id=FOREIGN_ID),
                current_user=TENANT,
            )

    _assert_body_fk_rejection(
        exc.value, error_code="REF_NOT_IN_MODEL", field="time_dimension_id",
        bad_id=FOREIGN_ID,
    )
    db.add.assert_not_called()
    db.commit.assert_not_awaited()


async def test_create_kpi_accepts_a_time_dimension_owned_by_the_model():
    from src.api.kpis import create_kpi

    db = _scoped_db(resolves=types.SimpleNamespace(id=OWNED_ID, name="Date"))

    async def _refresh(obj):
        obj.id = getattr(obj, "id", None) or uuid.uuid4()
        obj.created_at = NOW
        obj.updated_at = NOW

    db.refresh = AsyncMock(side_effect=_refresh)
    patches = _kpi_patches(db)
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        resp = await create_kpi(
            PROJECT_ID, MODEL_ID,
            KPICreate(name="Revenue", time_dimension_id=OWNED_ID),
            current_user=TENANT,
        )

    assert resp.time_dimension_id == OWNED_ID
    db.add.assert_called_once()
    assert isinstance(db.add.call_args[0][0], KPI)
    assert db.add.call_args[0][0].time_dimension_id == OWNED_ID
    db.commit.assert_awaited()


async def test_create_kpi_without_a_time_dimension_issues_no_lookup():
    from src.api.kpis import create_kpi

    db = _scoped_db(resolves=None)

    async def _refresh(obj):
        obj.id = getattr(obj, "id", None) or uuid.uuid4()
        obj.created_at = NOW
        obj.updated_at = NOW

    db.refresh = AsyncMock(side_effect=_refresh)
    patches = _kpi_patches(db)
    spy = _spy("src.api.kpis", "ensure_ref_in_model")
    with patches[0], patches[1], patches[2], patches[3], patches[4], spy as guard:
        resp = await create_kpi(
            PROJECT_ID, MODEL_ID, KPICreate(name="Revenue"),
            current_user=TENANT,
        )

    assert resp.time_dimension_id is None
    # Consulted unconditionally on create (``model_dump()`` always carries the
    # key), and returns None for the absent optional reference.
    assert [c["ref_id"] for c in guard] == [None]
    db.commit.assert_awaited()


async def test_update_kpi_refuses_a_time_dimension_outside_the_model():
    from src.api.kpis import update_kpi

    kpi = _kpi_row()
    db = _scoped_db(resolves=None)

    async def _get(entity, entity_id):
        if entity is KPI:
            return kpi
        if entity is Model:
            return types.SimpleNamespace(id=entity_id, project_id=PROJECT_ID)
        return None

    db.get = AsyncMock(side_effect=_get)
    patches = _kpi_patches(db)
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        with pytest.raises(HTTPException) as exc:
            await update_kpi(
                PROJECT_ID, MODEL_ID, kpi.id,
                KPIUpdate(time_dimension_id=FOREIGN_ID, display_name="Rev"),
                current_user=TENANT,
            )

    _assert_body_fk_rejection(
        exc.value, error_code="REF_NOT_IN_MODEL", field="time_dimension_id",
        bad_id=FOREIGN_ID,
    )
    # Ordering: the guard sits above the blanket ``setattr`` loop, so neither
    # the foreign binding nor this request's other edits reach the row.
    assert kpi.time_dimension_id is None
    assert kpi.display_name is None
    db.commit.assert_not_awaited()


async def test_update_kpi_accepts_a_time_dimension_owned_by_the_model():
    from src.api.kpis import update_kpi

    kpi = _kpi_row()
    db = _scoped_db(resolves=types.SimpleNamespace(id=OWNED_ID, name="Date"))

    async def _get(entity, entity_id):
        if entity is KPI:
            return kpi
        if entity is Model:
            return types.SimpleNamespace(id=entity_id, project_id=PROJECT_ID)
        return None

    db.get = AsyncMock(side_effect=_get)
    patches = _kpi_patches(db)
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        resp = await update_kpi(
            PROJECT_ID, MODEL_ID, kpi.id,
            KPIUpdate(time_dimension_id=OWNED_ID), current_user=TENANT,
        )

    assert resp.time_dimension_id == OWNED_ID
    assert kpi.time_dimension_id == OWNED_ID
    db.commit.assert_awaited()


async def test_update_kpi_still_unbinds_the_time_dimension():
    """An explicit ``null`` unbinds and stays legal. The guard keys on the field
    being PRESENT, which is why the two sibling guards' truthy ``updates.get``
    idiom was not copied."""
    from src.api.kpis import update_kpi

    kpi = _kpi_row(time_dimension_id=OWNED_ID)
    db = _scoped_db(resolves=None)

    async def _get(entity, entity_id):
        if entity is KPI:
            return kpi
        if entity is Model:
            return types.SimpleNamespace(id=entity_id, project_id=PROJECT_ID)
        return None

    db.get = AsyncMock(side_effect=_get)
    body = KPIUpdate(time_dimension_id=None)
    assert "time_dimension_id" in body.model_dump(exclude_unset=True)
    patches = _kpi_patches(db)
    spy = _spy("src.api.kpis", "ensure_ref_in_model")
    with patches[0], patches[1], patches[2], patches[3], patches[4], spy as guard:
        resp = await update_kpi(
            PROJECT_ID, MODEL_ID, kpi.id, body, current_user=TENANT,
        )

    assert resp.time_dimension_id is None
    assert kpi.time_dimension_id is None
    assert [c["ref_id"] for c in guard] == [None]
    db.commit.assert_awaited()


async def test_update_kpi_without_the_field_leaves_the_binding_alone():
    from src.api.kpis import update_kpi

    kpi = _kpi_row(time_dimension_id=OWNED_ID)
    db = _scoped_db(resolves=None)

    async def _get(entity, entity_id):
        if entity is KPI:
            return kpi
        if entity is Model:
            return types.SimpleNamespace(id=entity_id, project_id=PROJECT_ID)
        return None

    db.get = AsyncMock(side_effect=_get)
    patches = _kpi_patches(db)
    spy = _spy("src.api.kpis", "ensure_ref_in_model")
    with patches[0], patches[1], patches[2], patches[3], patches[4], spy as guard:
        resp = await update_kpi(
            PROJECT_ID, MODEL_ID, kpi.id, KPIUpdate(display_name="Rev"),
            current_user=TENANT,
        )

    assert resp.time_dimension_id == OWNED_ID
    assert guard == []
    db.commit.assert_awaited()


# ---------------------------------------------------------------------------
# kpis.py — revert is the SECOND writer of ``time_dimension_id``
# ---------------------------------------------------------------------------
#
# ``revert_kpi_version`` restores the KPI's UUID foreign keys from a KPIVersion
# snapshot. It validated ``target_measure_id`` and ``parent_kpi_id`` against the
# model and restored ``time_dimension_id`` unconditionally. A snapshot is
# written from whatever the live row held, so any foreign or since-deleted
# binding persisted before the create/update guards landed survives in the
# version history and was reinstated verbatim — the guard was bypassable by
# reverting to a version that predates it.


async def test_revert_drops_a_time_dimension_that_left_the_model():
    from src.api.kpis import revert_kpi_version

    kpi = _kpi_row()
    version = types.SimpleNamespace(
        version_number=1,
        snapshot={"name": "Revenue", "time_dimension_id": str(FOREIGN_ID)},
    )
    db = _scoped_db(resolves=None)
    result = MagicMock()
    result.scalar_one_or_none.return_value = version
    result.scalars.return_value.all.return_value = []
    db.execute = AsyncMock(return_value=result)

    async def _get(entity, entity_id):
        if entity is KPI:
            return kpi
        if entity is Model:
            return types.SimpleNamespace(id=entity_id, project_id=PROJECT_ID)
        return None  # Dimension lookup: the referenced dimension is gone

    db.get = AsyncMock(side_effect=_get)
    patches = _kpi_patches(db)
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        resp = await revert_kpi_version(
            MODEL_ID, kpi.id, 1, current_user=TENANT,
        )

    assert resp.time_dimension_id is None
    assert kpi.time_dimension_id is None, (
        "a dangling / foreign time dimension must be dropped on revert, the "
        "way target_measure_id and parent_kpi_id already are"
    )
    db.commit.assert_awaited()


async def test_revert_restores_a_time_dimension_still_in_the_model():
    """The revert guard is a membership check, not a blanket drop — a valid
    binding must survive the round trip."""
    from src.api.kpis import revert_kpi_version
    from shared.db.models import Dimension

    kpi = _kpi_row()
    version = types.SimpleNamespace(
        version_number=1,
        snapshot={"name": "Revenue", "time_dimension_id": str(OWNED_ID)},
    )
    db = _scoped_db(resolves=None)
    result = MagicMock()
    result.scalar_one_or_none.return_value = version
    result.scalars.return_value.all.return_value = []
    db.execute = AsyncMock(return_value=result)

    async def _get(entity, entity_id):
        if entity is KPI:
            return kpi
        if entity is Model:
            return types.SimpleNamespace(id=entity_id, project_id=PROJECT_ID)
        if entity is Dimension:
            return types.SimpleNamespace(id=entity_id, model_id=MODEL_ID)
        return None

    db.get = AsyncMock(side_effect=_get)
    patches = _kpi_patches(db)
    with patches[0], patches[1], patches[2], patches[3], patches[4]:
        resp = await revert_kpi_version(
            MODEL_ID, kpi.id, 1, current_user=TENANT,
        )

    assert resp.time_dimension_id == OWNED_ID
    assert kpi.time_dimension_id == OWNED_ID
    db.commit.assert_awaited()


# ---------------------------------------------------------------------------
# The guards are only sound if BOTH scope ids come from the URL PATH
# ---------------------------------------------------------------------------


def test_every_guarded_route_takes_its_scope_ids_from_the_path():
    """``_scope``'s hardest precondition, asserted against the real app.

    The primitives prove ``row.model_id == model_id AND model.project_id ==
    project_id``. That is only an authorization check when BOTH ids are
    path-derived: a BODY-supplied ``model_id`` would resolve any sibling model
    inside the caller's own project and the helper would accept it.
    ``_scope.py`` states this as a requirement rather than a convenience, and
    nothing enforced it.

    Three separate things have to hold, and they are asserted separately
    because two of them can be true while the one that matters is false:

    1. The route's OWN HANDLER declares both ids. This is the load-bearing
       assertion. ``create_kpi`` and ``update_kpi`` did not declare
       ``project_id`` at all before this lane, and a guard cannot be fed an id
       the handler never receives.
    2. FastAPI resolves them from the PATH, not the query string.
    3. No request body carries a scope id of its own.

    Checking only (2) is not enough, and that is not hypothetical: the KPI
    router's ``_ensure_kpi_model_scope`` dependency declares ``project_id``
    itself, so it appears as a path parameter in the generated OpenAPI whether
    or not the handler takes it. A first cut of this test asserted only the
    OpenAPI shape and a mutant that deleted ``project_id`` from ``create_kpi``'s
    signature survived it.
    """
    import inspect

    from fastapi.routing import APIRoute

    # Build a FRESH, fully-wired app rather than reading the process-global
    # ``src.main.app``. The global is shared by every test in the process and
    # can be mutated by a sibling (route table, dependency overrides, cached
    # openapi schema), which made this assertion fragile to collection order —
    # it passed locally but failed under CI's smaller, differently-ordered set.
    # A fresh instance is assembled from the current router modules on the spot,
    # so route registration is asserted against a clean table regardless of
    # anything a sibling test did to the global. See create_app() in src/main.py.
    from src.main import create_app

    app = create_app()

    spec = app.openapi()
    base = "/api/v1/projects/{project_id}/models/{model_id}"
    guarded = [
        (f"{base}/kpis", "post"),
        (f"{base}/kpis/{{kpi_id}}", "patch"),
        (f"{base}/data-quality-rules", "post"),
        (f"{base}/downstream-assets", "post"),
        (f"{base}/downstream-assets/{{asset_id}}", "put"),
        (base, "patch"),
    ]
    routes = {
        (r.path, method.lower()): r
        for r in app.routes
        if isinstance(r, APIRoute)
        for method in r.methods
    }

    for path, method in guarded:
        # (1) the handler itself receives both ids
        route = routes.get((path, method))
        assert route is not None, f"{method.upper()} {path} is not registered"
        declared = inspect.signature(route.endpoint).parameters
        for name in ("project_id", "model_id"):
            assert name in declared, (
                f"{method.upper()} {path}: handler "
                f"{route.endpoint.__name__}() does not declare {name}, so its "
                "body-FK guard cannot be given a path-derived scope id"
            )

        operation = spec["paths"][path][method]
        located = {p["name"]: p["in"] for p in operation.get("parameters", [])}
        # (2) and they come from the path, not the query string
        for name in ("project_id", "model_id"):
            assert located.get(name) == "path", (
                f"{method.upper()} {path}: {name} must be a PATH parameter, "
                f"got {located.get(name)!r}"
            )

        # (3) and the body does not carry a scope id of its own
        request_body = operation.get("requestBody")
        if not request_body:
            continue
        ref = request_body["content"]["application/json"]["schema"].get("$ref")
        if not ref:
            continue
        schema = spec["components"]["schemas"][ref.rsplit("/", 1)[-1]]
        smuggled = {"project_id", "model_id"} & set(schema.get("properties", {}))
        assert not smuggled, (
            f"{method.upper()} {path}: request body carries {sorted(smuggled)}; "
            "a body-supplied scope id defeats every body-FK guard on the route"
        )
