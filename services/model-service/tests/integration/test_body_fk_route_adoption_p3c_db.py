"""Real-Postgres proof that the P3-c routes adopted the body-FK guards.

Sibling of ``test_body_fk_route_adoption_db.py`` (P3-a: ``create_aggregate`` and
``update_table``); same suite, same role, split by lane so neither file grows
past the module size this repo targets. ``test_scope_body_fk_db.py`` proves the
``_scope`` primitives themselves; these two files prove that the HANDLERS call
them — the difference between "the helper works" and "the route uses it".

The five body foreign keys covered here, and what each one reaches:

* ``kpis.py::create_kpi`` / ``update_kpi`` — ``time_dimension_id``. The third FK
  on KPICreate/KPIUpdate and the only unchecked one (``parent_kpi_id`` and
  ``target_measure_id`` each have a hand-rolled guard).
  ``_resolve_time_column`` dereferences it with a bare
  ``db.get(Dimension, ...)`` and the result becomes the time column of the
  compiled KPI SQL — a wrong number, not only a leak.
* ``kpis.py::revert_kpi_version`` — the SAME column's second writer. It
  validated its two siblings and restored the time dimension unconditionally, so
  the create/update guard was bypassable by reverting to a version that predates
  it.
* ``models.py::update_model`` — ``target_id``, the model's DEFAULT
  materialisation destination, inherited by aggregates and pockets created
  afterwards. A DataTarget carries ``project_connection_id``: live,
  Fernet-encrypted source credentials (Bug-5325).
* ``data_quality.py::create_rule`` — the polymorphic ``(target_type,
  target_id)`` pair. ``shared/data_quality/validator.py::_resolve_column_ref``
  walks ``rule.target_id`` to its ModelTable's ``physical_name`` with no
  ownership re-check and queries that table through /introspect on a
  ``system_admin`` service token.
* ``downstream_assets.py::create_downstream_asset`` / ``update_downstream_asset``
  — the ``column_ids`` collection. ``governance_exporter.py`` walks the
  resulting association into the Collibra / Solidatus governance graph.

Every case runs all three near-misses — ``sibling_model`` (same project, other
model), ``other_project``, and ``unknown`` — because a guard that checks only
the project passes the obvious cross-project test and still leaks to a sibling
model. Every site also has a positive test: Bug-8864 shipped a scope guard that
denied essentially everything and only a positive test caught it.

A mocked session cannot prove any of this — it does not evaluate a WHERE clause,
so an accept-everything guard and a deny-everything guard both pass under a
fake. Hence real Postgres, real handlers, real rows.

Skipped unless ``TESSALLITE_VERSIONING_DB_URL`` (or the importer-harness URL)
points at a reachable Postgres.

Run:
    cd tessallite/services/model-service
    TESSALLITE_VERSIONING_DB_URL=postgresql+asyncpg://user:pw@localhost:5432/db \
      pytest tests/integration/test_body_fk_route_adoption_p3c_db.py -v
"""
from __future__ import annotations

import types
import uuid
from contextlib import asynccontextmanager
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from shared.db.models import (
    DataQualityRule,
    DownstreamAsset,
    KPI,
    Model,
    downstream_asset_columns,
)
from shared.schemas.pydantic_models import (
    DataQualityRuleCreate,
    DownstreamAssetCreate,
    DownstreamAssetUpdate,
    KPICreate,
    KPIUpdate,
    ModelUpdate,
)
from src.api.data_quality import create_rule
from src.api.downstream_assets import (
    create_downstream_asset,
    update_downstream_asset,
)
from src.api.kpis import create_kpi, revert_kpi_version, update_kpi
from src.api.models import update_model

from tests.integration.test_body_fk_route_adoption_db import _seed_targets
from tests.integration.test_scope_body_fk_db import _Fixture, _seed
from tests.integration.test_versioning_consistency_db import _DB_URL, _isolated_schema

pytestmark = [pytest.mark.integration]

_TENANT = types.SimpleNamespace(
    tenant_id="acme", raw_token="t", email="u@test", role="modeler",
    user_id="u@test",
)

@asynccontextmanager
async def _routes_on(db):
    """Point every touched router's tenant-session dependency at the isolated
    session. The advisory lock is NOT stubbed — it is real SQL and works against
    a real database, so the guards are exercised under the same locking the
    production path uses."""
    async def _tenant_db(_tenant_id):
        yield db

    with (
        patch("src.api.kpis.get_tenant_db", new=_tenant_db),
        patch("src.api.models.get_tenant_db", new=_tenant_db),
        patch("src.api.data_quality.get_tenant_db", new=_tenant_db),
        patch("src.api.downstream_assets.get_tenant_db", new=_tenant_db),
    ):
        yield


def _near_misses(f: _Fixture, owned_kind: str) -> dict[str, uuid.UUID]:
    """The three ids a guard must refuse for a given entity family."""
    return {
        "dimension": {
            "sibling_model": f.dimension_a2,
            "other_project": f.dimension_b,
            "unknown": uuid.uuid4(),
        },
        "column": {
            "sibling_model": f.column_a2,
            "other_project": f.column_b,
            "unknown": uuid.uuid4(),
        },
        "measure": {
            "sibling_model": f.measure_a2,
            "other_project": f.measure_b,
            "unknown": uuid.uuid4(),
        },
    }[owned_kind]


def _assert_rejection(exc, *, error_code, field, bad_id):
    assert exc.status_code == 422
    detail = exc.detail
    assert isinstance(detail, dict)
    assert detail["error_code"] == error_code
    assert detail["field"] == field
    assert detail["ids"] == [str(bad_id)]


# ---------------------------------------------------------------------------
# kpis.py — time_dimension_id on create
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _DB_URL, reason="no DB URL configured")
async def test_create_kpi_accepts_a_time_dimension_owned_by_the_path_model():
    async with _isolated_schema() as (factory, _schema):
        async with factory() as db:
            f = await _seed(db)

            async with _routes_on(db):
                resp = await create_kpi(
                    f.project_a, f.model_a,
                    KPICreate(name="Revenue", time_dimension_id=f.dimension_a),
                    current_user=_TENANT,
                )

            assert resp.time_dimension_id == f.dimension_a
            row = (
                await db.execute(select(KPI).where(KPI.model_id == f.model_a))
            ).scalars().one()
            assert row.time_dimension_id == f.dimension_a


@pytest.mark.skipif(not _DB_URL, reason="no DB URL configured")
@pytest.mark.parametrize("foreign", ["sibling_model", "other_project", "unknown"])
async def test_create_kpi_refuses_a_time_dimension_it_does_not_own(foreign):
    """The sibling_model case is the load-bearing one: a guard that proved only
    ``dimension.model.project_id == project_id`` would accept ``dimension_a2``
    (same project, other model) and still bind this KPI's periods to a date
    column the modeller never chose."""
    async with _isolated_schema() as (factory, _schema):
        async with factory() as db:
            f = await _seed(db)
            bad = _near_misses(f, "dimension")[foreign]

            async with _routes_on(db):
                with pytest.raises(HTTPException) as exc:
                    await create_kpi(
                        f.project_a, f.model_a,
                        KPICreate(name="Revenue", time_dimension_id=bad),
                        current_user=_TENANT,
                    )

            _assert_rejection(
                exc.value, error_code="REF_NOT_IN_MODEL",
                field="time_dimension_id", bad_id=bad,
            )
            await db.rollback()
            assert (await db.execute(select(KPI))).scalars().all() == [], (
                "no KPI may survive a refused create"
            )


@pytest.mark.skipif(not _DB_URL, reason="no DB URL configured")
async def test_create_kpi_reports_foreign_and_unknown_dimensions_identically():
    """Anti-oracle: the response must not confirm that a dimension exists
    elsewhere in the tenant. Only the echoed (caller-supplied) id may differ."""
    async with _isolated_schema() as (factory, _schema):
        async with factory() as db:
            f = await _seed(db)

            async with _routes_on(db):
                with pytest.raises(HTTPException) as foreign:
                    await create_kpi(
                        f.project_a, f.model_a,
                        KPICreate(name="A", time_dimension_id=f.dimension_b),
                        current_user=_TENANT,
                    )
                await db.rollback()
                with pytest.raises(HTTPException) as unknown:
                    await create_kpi(
                        f.project_a, f.model_a,
                        KPICreate(name="B", time_dimension_id=uuid.uuid4()),
                        current_user=_TENANT,
                    )

            assert foreign.value.status_code == unknown.value.status_code
            a, b = dict(foreign.value.detail), dict(unknown.value.detail)
            assert a.pop("ids") != b.pop("ids")
            a.pop("message"), b.pop("message")
            assert a == b


# ---------------------------------------------------------------------------
# kpis.py — time_dimension_id on PATCH, and the revert that rewrites it
# ---------------------------------------------------------------------------


async def _kpi_in(db, f: _Fixture) -> uuid.UUID:
    async with _routes_on(db):
        resp = await create_kpi(
            f.project_a, f.model_a, KPICreate(name="Revenue"),
            current_user=_TENANT,
        )
    return resp.id


@pytest.mark.skipif(not _DB_URL, reason="no DB URL configured")
async def test_update_kpi_binds_and_unbinds_a_time_dimension_it_owns():
    """Three shapes must stay legal: bind an owned dimension, leave it alone
    when the field is not sent, and unbind it with an explicit ``null``.
    ``exclude_unset`` is what separates the last two."""
    async with _isolated_schema() as (factory, _schema):
        async with factory() as db:
            f = await _seed(db)
            kpi_id = await _kpi_in(db, f)

            async with _routes_on(db):
                bound = await update_kpi(
                    f.project_a, f.model_a, kpi_id,
                    KPIUpdate(time_dimension_id=f.dimension_a),
                    current_user=_TENANT,
                )
                assert bound.time_dimension_id == f.dimension_a

                untouched = await update_kpi(
                    f.project_a, f.model_a, kpi_id,
                    KPIUpdate(display_name="Rev"), current_user=_TENANT,
                )
                assert untouched.time_dimension_id == f.dimension_a, (
                    "a PATCH that does not mention the time dimension must not "
                    "silently unbind it"
                )

                unbound = await update_kpi(
                    f.project_a, f.model_a, kpi_id,
                    KPIUpdate(time_dimension_id=None), current_user=_TENANT,
                )
                assert unbound.time_dimension_id is None

            row = await db.get(KPI, kpi_id)
            await db.refresh(row)
            assert row.time_dimension_id is None


@pytest.mark.skipif(not _DB_URL, reason="no DB URL configured")
@pytest.mark.parametrize("foreign", ["sibling_model", "other_project", "unknown"])
async def test_update_kpi_refuses_a_time_dimension_it_does_not_own(foreign):
    async with _isolated_schema() as (factory, _schema):
        async with factory() as db:
            f = await _seed(db)
            kpi_id = await _kpi_in(db, f)
            bad = _near_misses(f, "dimension")[foreign]

            async with _routes_on(db):
                with pytest.raises(HTTPException) as exc:
                    await update_kpi(
                        f.project_a, f.model_a, kpi_id,
                        KPIUpdate(time_dimension_id=bad, display_name="Rev"),
                        current_user=_TENANT,
                    )

            _assert_rejection(
                exc.value, error_code="REF_NOT_IN_MODEL",
                field="time_dimension_id", bad_id=bad,
            )
            await db.rollback()
            row = await db.get(KPI, kpi_id)
            await db.refresh(row)
            assert row.time_dimension_id is None
            assert row.display_name is None, (
                "the guard runs above the blanket setattr loop, so the rest of "
                "a refused PATCH must not land either"
            )


@pytest.mark.skipif(not _DB_URL, reason="no DB URL configured")
async def test_revert_drops_a_time_dimension_that_is_not_in_this_model():
    """The create/update guard must not be bypassable by reverting.

    A KPIVersion snapshot is written from whatever the live row held, so a
    binding persisted before the guard landed survives in the version history.
    This test writes exactly that state — a version snapshot naming another
    model's dimension — and proves the revert drops it rather than reinstating
    it, the way the loop's ``target_measure_id`` and ``parent_kpi_id`` branches
    already do.
    """
    from shared.db.models import KPIVersion

    async with _isolated_schema() as (factory, _schema):
        async with factory() as db:
            f = await _seed(db)
            kpi_id = await _kpi_in(db, f)

            # A pre-guard version: the snapshot names a sibling model's
            # dimension, which is exactly what the old create path allowed.
            version = (
                await db.execute(
                    select(KPIVersion).where(KPIVersion.kpi_id == kpi_id)
                )
            ).scalars().one()
            snapshot = dict(version.snapshot)
            snapshot["time_dimension_id"] = str(f.dimension_a2)
            version.snapshot = snapshot
            await db.commit()

            async with _routes_on(db):
                resp = await revert_kpi_version(
                    f.model_a, kpi_id, version.version_number,
                    current_user=_TENANT,
                )

            assert resp.time_dimension_id is None
            row = await db.get(KPI, kpi_id)
            await db.refresh(row)
            assert row.time_dimension_id is None


@pytest.mark.skipif(not _DB_URL, reason="no DB URL configured")
async def test_revert_restores_a_time_dimension_that_is_in_this_model():
    """The revert check is a membership test, not a blanket drop — an owned
    binding must survive the round trip."""
    from shared.db.models import KPIVersion

    async with _isolated_schema() as (factory, _schema):
        async with factory() as db:
            f = await _seed(db)
            kpi_id = await _kpi_in(db, f)

            async with _routes_on(db):
                await update_kpi(
                    f.project_a, f.model_a, kpi_id,
                    KPIUpdate(time_dimension_id=f.dimension_a),
                    current_user=_TENANT,
                )
                bound_version = max(
                    (
                        await db.execute(
                            select(KPIVersion).where(KPIVersion.kpi_id == kpi_id)
                        )
                    ).scalars().all(),
                    key=lambda v: v.version_number,
                ).version_number
                await update_kpi(
                    f.project_a, f.model_a, kpi_id,
                    KPIUpdate(time_dimension_id=None), current_user=_TENANT,
                )
                resp = await revert_kpi_version(
                    f.model_a, kpi_id, bound_version, current_user=_TENANT,
                )

            assert resp.time_dimension_id == f.dimension_a
            row = await db.get(KPI, kpi_id)
            await db.refresh(row)
            assert row.time_dimension_id == f.dimension_a


# ---------------------------------------------------------------------------
# models.py — target_id
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _DB_URL, reason="no DB URL configured")
async def test_update_model_accepts_a_target_owned_by_the_path_model():
    async with _isolated_schema() as (factory, _schema):
        async with factory() as db:
            f = await _seed(db)
            targets = await _seed_targets(db, f)

            async with _routes_on(db):
                resp = await update_model(
                    f.project_a, f.model_a,
                    ModelUpdate(target_id=targets[f.model_a]),
                    current_user=_TENANT,
                )

            assert resp.target_id == targets[f.model_a]
            row = await db.get(Model, f.model_a)
            await db.refresh(row)
            assert row.target_id == targets[f.model_a]


@pytest.mark.skipif(not _DB_URL, reason="no DB URL configured")
@pytest.mark.parametrize("foreign", ["sibling_model", "other_project", "unknown"])
async def test_update_model_refuses_a_target_it_does_not_own(foreign):
    """The foreign target must never land on the row: ``models.target_id`` is
    the DEFAULT materialisation destination inherited by aggregates and pockets
    created afterwards, so a value that survives here re-points future CTAS work
    at another project's source connection."""
    async with _isolated_schema() as (factory, _schema):
        async with factory() as db:
            f = await _seed(db)
            targets = await _seed_targets(db, f)
            bad = {
                "sibling_model": targets[f.model_a2],
                "other_project": targets[f.model_b],
                "unknown": uuid.uuid4(),
            }[foreign]

            async with _routes_on(db):
                with pytest.raises(HTTPException) as exc:
                    await update_model(
                        f.project_a, f.model_a,
                        ModelUpdate(target_id=bad, display_name="Renamed"),
                        current_user=_TENANT,
                    )

            _assert_rejection(
                exc.value, error_code="REF_NOT_IN_MODEL", field="target_id",
                bad_id=bad,
            )
            await db.rollback()
            row = await db.get(Model, f.model_a)
            await db.refresh(row)
            assert row.target_id is None
            assert row.display_name == "M", (
                "the guard runs above the blanket setattr loop"
            )


@pytest.mark.skipif(not _DB_URL, reason="no DB URL configured")
async def test_update_model_still_clears_the_default_target():
    """An explicit ``null`` means "no default target" and stays legal — that is
    exactly what ``targets.py``'s delete path writes."""
    async with _isolated_schema() as (factory, _schema):
        async with factory() as db:
            f = await _seed(db)
            targets = await _seed_targets(db, f)

            async with _routes_on(db):
                await update_model(
                    f.project_a, f.model_a,
                    ModelUpdate(target_id=targets[f.model_a]),
                    current_user=_TENANT,
                )
                renamed = await update_model(
                    f.project_a, f.model_a, ModelUpdate(display_name="Renamed"),
                    current_user=_TENANT,
                )
                assert renamed.target_id == targets[f.model_a], (
                    "a PATCH that does not mention the target must not clear it"
                )
                cleared = await update_model(
                    f.project_a, f.model_a, ModelUpdate(target_id=None),
                    current_user=_TENANT,
                )
                assert cleared.target_id is None

            row = await db.get(Model, f.model_a)
            await db.refresh(row)
            assert row.target_id is None


# ---------------------------------------------------------------------------
# data_quality.py — the polymorphic (target_type, target_id) pair
# ---------------------------------------------------------------------------


def _dq_body(target_type, target_id):
    return DataQualityRuleCreate(
        name="not-null check",
        target_type=target_type,
        target_id=target_id,
        rule_type="not_null",
    )


@pytest.mark.skipif(not _DB_URL, reason="no DB URL configured")
@pytest.mark.parametrize(
    "target_type,owned_kind",
    [("column", "column"), ("dimension", "dimension"), ("measure", "measure")],
)
async def test_create_rule_accepts_a_target_owned_by_the_path_model(
    target_type, owned_kind,
):
    async with _isolated_schema() as (factory, _schema):
        async with factory() as db:
            f = await _seed(db)
            owned = {
                "column": f.column_a,
                "dimension": f.dimension_a,
                "measure": f.measure_a,
            }[owned_kind]

            async with _routes_on(db):
                resp = await create_rule(
                    f.project_a, f.model_a, _dq_body(target_type, owned),
                    current_user=_TENANT,
                )

            assert resp.target_id == owned
            assert resp.target_type == target_type
            row = (
                await db.execute(
                    select(DataQualityRule).where(
                        DataQualityRule.model_id == f.model_a
                    )
                )
            ).scalars().one()
            assert row.target_id == owned


@pytest.mark.skipif(not _DB_URL, reason="no DB URL configured")
@pytest.mark.parametrize("target_type", ["column", "dimension", "measure"])
@pytest.mark.parametrize("foreign", ["sibling_model", "other_project", "unknown"])
async def test_create_rule_refuses_a_target_it_does_not_own(target_type, foreign):
    """Every declared target type is scoped, not just the ``column`` one the
    validator dereferences today — a rule pointing at another project's column
    turns "add a not-null rule" into a source query over that project's physical
    table, executed on a ``system_admin`` service token."""
    async with _isolated_schema() as (factory, _schema):
        async with factory() as db:
            f = await _seed(db)
            bad = _near_misses(f, target_type)[foreign]

            async with _routes_on(db):
                with pytest.raises(HTTPException) as exc:
                    await create_rule(
                        f.project_a, f.model_a, _dq_body(target_type, bad),
                        current_user=_TENANT,
                    )

            _assert_rejection(
                exc.value, error_code="TARGET_NOT_IN_MODEL", field="target_id",
                bad_id=bad,
            )
            await db.rollback()
            assert (
                await db.execute(select(DataQualityRule))
            ).scalars().all() == [], "no rule may survive a refused create"


# ---------------------------------------------------------------------------
# downstream_assets.py — the column_ids collection
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _DB_URL, reason="no DB URL configured")
async def test_create_downstream_asset_accepts_columns_owned_by_the_path_model():
    async with _isolated_schema() as (factory, _schema):
        async with factory() as db:
            f = await _seed(db)

            async with _routes_on(db):
                resp = await create_downstream_asset(
                    f.project_a, f.model_a,
                    DownstreamAssetCreate(
                        asset_type="dashboard", asset_name="Sales",
                        column_ids=[f.column_a, f.column_a_other],
                    ),
                    current_user=_TENANT,
                )

            assert sorted(map(str, resp.column_ids)) == sorted(
                map(str, [f.column_a, f.column_a_other])
            )
            linked = (
                await db.execute(
                    select(downstream_asset_columns.c.model_column_id)
                )
            ).scalars().all()
            assert sorted(map(str, linked)) == sorted(
                map(str, [f.column_a, f.column_a_other])
            )


@pytest.mark.skipif(not _DB_URL, reason="no DB URL configured")
@pytest.mark.parametrize("foreign", ["sibling_model", "other_project", "unknown"])
async def test_create_downstream_asset_refuses_a_column_it_does_not_own(foreign):
    """All-or-nothing, and the OWNED id in the same request must not slip
    through either. The replaced query silently DROPPED unresolvable ids, so a
    request naming two columns persisted one and still answered 201."""
    async with _isolated_schema() as (factory, _schema):
        async with factory() as db:
            f = await _seed(db)
            bad = _near_misses(f, "column")[foreign]

            async with _routes_on(db):
                with pytest.raises(HTTPException) as exc:
                    await create_downstream_asset(
                        f.project_a, f.model_a,
                        DownstreamAssetCreate(
                            asset_type="dashboard", asset_name="Sales",
                            column_ids=[f.column_a, bad],
                        ),
                        current_user=_TENANT,
                    )

            _assert_rejection(
                exc.value, error_code="REFS_NOT_IN_MODEL", field="column_ids",
                bad_id=bad,
            )
            await db.rollback()
            assert (
                await db.execute(select(DownstreamAsset))
            ).scalars().all() == [], "no asset may survive a refused create"
            assert (
                await db.execute(
                    select(downstream_asset_columns.c.model_column_id)
                )
            ).scalars().all() == [], "and no association either"


@pytest.mark.skipif(not _DB_URL, reason="no DB URL configured")
@pytest.mark.parametrize("foreign", ["sibling_model", "other_project", "unknown"])
async def test_update_downstream_asset_refuses_a_column_it_does_not_own(foreign):
    async with _isolated_schema() as (factory, _schema):
        async with factory() as db:
            f = await _seed(db)
            bad = _near_misses(f, "column")[foreign]

            async with _routes_on(db):
                created = await create_downstream_asset(
                    f.project_a, f.model_a,
                    DownstreamAssetCreate(
                        asset_type="dashboard", asset_name="Sales",
                        column_ids=[f.column_a],
                    ),
                    current_user=_TENANT,
                )
                with pytest.raises(HTTPException) as exc:
                    await update_downstream_asset(
                        f.project_a, f.model_a, created.id,
                        DownstreamAssetUpdate(
                            asset_name="Renamed", column_ids=[bad],
                        ),
                        current_user=_TENANT,
                    )

            _assert_rejection(
                exc.value, error_code="REFS_NOT_IN_MODEL", field="column_ids",
                bad_id=bad,
            )
            await db.rollback()
            row = await db.get(DownstreamAsset, created.id)
            await db.refresh(row)
            assert row.asset_name == "Sales", (
                "the guard runs above the scalar setattr loop, so a refused "
                "PATCH does not land its rename either"
            )
            linked = (
                await db.execute(
                    select(downstream_asset_columns.c.model_column_id)
                )
            ).scalars().all()
            assert linked == [f.column_a], "the original association survives"


@pytest.mark.skipif(not _DB_URL, reason="no DB URL configured")
async def test_update_downstream_asset_rebinds_and_detaches_owned_columns():
    """The guard is not a wall: an owned column rebinds, ``[]`` detaches every
    column, and a PATCH that omits ``column_ids`` leaves them alone.

    Also the regression guard for TMP-20260811012933050: this positive test is
    what exposed that ``update_downstream_asset`` answered HTTP 500 for EVERY
    PUT carrying ``column_ids``. The handler fetched the asset with ``db.get``
    and then assigned to ``asset.columns``; SQLAlchemy loads the existing
    collection to compute the delta, and on a fresh per-request session that is
    a LAZY load, which asyncio cannot perform (``MissingGreenlet``). Reproduced
    on the lane's base SHA 4a02a7b5 with one session per request, so it is
    pre-existing — but it is a live defect on the exact path this lane modifies,
    so the lane owns it. Fixed by loading the asset with
    ``selectinload(DownstreamAsset.columns)``.

    A mocked session cannot catch this: it never performs a lazy load. Only the
    real-database round trip below distinguishes the two.
    """
    async with _isolated_schema() as (factory, _schema):
        async with factory() as db:
            f = await _seed(db)

            async with _routes_on(db):
                created = await create_downstream_asset(
                    f.project_a, f.model_a,
                    DownstreamAssetCreate(
                        asset_type="dashboard", asset_name="Sales",
                        column_ids=[f.column_a],
                    ),
                    current_user=_TENANT,
                )
                rebound = await update_downstream_asset(
                    f.project_a, f.model_a, created.id,
                    DownstreamAssetUpdate(column_ids=[f.column_a_other]),
                    current_user=_TENANT,
                )
                assert rebound.column_ids == [f.column_a_other]

                renamed = await update_downstream_asset(
                    f.project_a, f.model_a, created.id,
                    DownstreamAssetUpdate(asset_name="Renamed"),
                    current_user=_TENANT,
                )
                assert renamed.asset_name == "Renamed"
                assert renamed.column_ids == [f.column_a_other], (
                    "omitting column_ids must not detach them"
                )

                detached = await update_downstream_asset(
                    f.project_a, f.model_a, created.id,
                    DownstreamAssetUpdate(column_ids=[]), current_user=_TENANT,
                )
                assert detached.column_ids == []

            assert (
                await db.execute(
                    select(downstream_asset_columns.c.model_column_id)
                )
            ).scalars().all() == []
