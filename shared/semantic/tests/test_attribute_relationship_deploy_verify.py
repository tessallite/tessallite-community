"""Tests for the deploy/artifact verification drivers (spec §7.6.2 / §7.6.3).

These assert that the drivers stage the RIGHT evidence rows (status, scope,
artifact tagging, declaration-hash binding) and are fail-open, using fake DB +
source stubs. The end-to-end deploy/build wiring is exercised by the
model-service / optimizer suites; here we pin the driver contract.
"""
from __future__ import annotations

import types
import uuid

import pytest

import shared.semantic.attribute_relationship_deploy_verify as dv
from shared.semantic.attribute_relationship_verifier import (
    BROKEN,
    ERR_UNCERTIFIED_COLLATION,
    ERROR,
    PENDING,
    VERIFIED,
)

pytestmark = pytest.mark.unit


class _FakeResultScalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeExecResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return _FakeResultScalars(self._rows)


class _FakeDB:
    """Fake session: execute() returns registered relationship rows; get()
    returns registered columns/tables; add() collects staged rows."""

    def __init__(self, rels, columns, tables):
        self._rels = rels
        self._columns = columns
        self._tables = tables
        self.staged = []

    async def execute(self, stmt):
        return _FakeExecResult(self._rels)

    async def get(self, model, pk):
        name = getattr(model, "__name__", "")
        if name == "ModelColumn":
            return self._columns.get(pk)
        if name == "ModelTable":
            return self._tables.get(pk)
        return None

    def add(self, row):
        self.staged.append(row)


def _rel(cardinality="BIJECTION", key_id=None, detail_id=None, decl_hash="h"):
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        cardinality=cardinality,
        key_column_id=key_id,
        detail_column_id=detail_id,
        declaration_hash=decl_hash,
        enabled=True,
    )


def _col(col_id, table_id, name, dtype):
    return types.SimpleNamespace(
        id=col_id, model_table_id=table_id, column_name=name, data_type=dtype,
    )


def _patch_source(monkeypatch, row_map):
    import shared.source_executor as se

    async def _exec(conn_obj, sql, *, tenant_session=None):
        for needle, rows in row_map.items():
            if needle in sql:
                return rows, []
        return [], []

    monkeypatch.setattr(se, "execute_source_sql", _exec)


@pytest.mark.asyncio
async def test_deploy_verify_stages_verified_evidence(monkeypatch):
    table_id = uuid.uuid4()
    key_id, detail_id = uuid.uuid4(), uuid.uuid4()
    rel = _rel(key_id=key_id, detail_id=detail_id)
    columns = {
        key_id: _col(key_id, table_id, "country_id", "INT"),
        detail_id: _col(detail_id, table_id, "country_code", "INT"),
    }
    tables = {table_id: types.SimpleNamespace(physical_name="public.geo")}
    db = _FakeDB([rel], columns, tables)
    _patch_source(monkeypatch, {})  # all checks empty -> VERIFIED

    staged = await dv.verify_model_relationships_on_deploy(
        db=db, model_id=uuid.uuid4(), deployed_version_id=uuid.uuid4(),
        deploy_epoch=3, verifier_version="v0",
        conn_obj=types.SimpleNamespace(), connector="postgresql",
    )
    assert len(staged) == 1
    row = staged[0]
    assert row.status == VERIFIED
    assert row.artifact_kind == "DEPLOY_CHECK"
    assert row.scope_kind == "TENANT_GLOBAL"
    assert row.deploy_epoch == 3
    assert row.declaration_hash == "h"


@pytest.mark.asyncio
async def test_deploy_verify_reverse_violation_broken(monkeypatch):
    table_id = uuid.uuid4()
    key_id, detail_id = uuid.uuid4(), uuid.uuid4()
    rel = _rel(cardinality="BIJECTION", key_id=key_id, detail_id=detail_id)
    columns = {
        key_id: _col(key_id, table_id, "country_id", "INT"),
        detail_id: _col(detail_id, table_id, "country_code", "INT"),
    }
    tables = {table_id: types.SimpleNamespace(physical_name="public.geo")}
    db = _FakeDB([rel], columns, tables)
    # Reverse strict check returns a row -> BROKEN.
    _patch_source(monkeypatch, {'COUNT(DISTINCT "country_id")': [{"country_code": 1}]})

    staged = await dv.verify_model_relationships_on_deploy(
        db=db, model_id=uuid.uuid4(), deployed_version_id=uuid.uuid4(),
        deploy_epoch=1, verifier_version="v0",
        conn_obj=types.SimpleNamespace(), connector="postgresql",
    )
    assert staged[0].status == BROKEN


@pytest.mark.asyncio
async def test_deploy_verify_unresolved_column_is_error(monkeypatch):
    rel = _rel(key_id=None, detail_id=None)  # SET NULL endpoints
    db = _FakeDB([rel], {}, {})
    _patch_source(monkeypatch, {})
    staged = await dv.verify_model_relationships_on_deploy(
        db=db, model_id=uuid.uuid4(), deployed_version_id=uuid.uuid4(),
        deploy_epoch=1, verifier_version="v0",
        conn_obj=types.SimpleNamespace(), connector="postgresql",
    )
    assert staged[0].status == ERROR
    assert staged[0].error_code == "UNRESOLVED_COLUMN"


# ---------------------------------------------------------------------------
# Phase 3: artifact-LOCAL manifest advance (spec §7.6.3)
# ---------------------------------------------------------------------------


class _ModelStub:
    __name__ = "Model"


class _ManifestDB(_FakeDB):
    """Fake DB whose get() also resolves the Model row for the advance path."""

    def __init__(self, rels, columns, tables, model=None):
        super().__init__(rels, columns, tables)
        self._model = model or types.SimpleNamespace(
            deployed_version_id=uuid.uuid4(), deploy_epoch=5,
        )

    async def get(self, model, pk):
        name = getattr(model, "__name__", "")
        if name == "Model":
            return self._model
        return await super().get(model, pk)


def _patch_settings(monkeypatch, build_mode):
    """Patch get_setting so build_mode / verifier_version resolve deterministically."""
    async def _get_setting(key, *, tenant_session=None, model_id=None):
        if key == "optimizer.derived_expression_auto_build":
            return build_mode
        if key == "model.attribute_relationship_verifier_version":
            return "v0"
        return None

    import shared.config.resolver as res
    monkeypatch.setattr(res, "get_setting", _get_setting)


def _patch_target_connector(monkeypatch, connector="postgresql"):
    async def _resolve(target_conn):
        return connector
    monkeypatch.setattr(dv, "_resolve_target_connector", _resolve)


def _patch_collation_check(monkeypatch, deterministic=True):
    """Patch the per-column collation determinism check to return a known result.

    Bug-7892: the deploy/artifact paths now check per-column collation
    determinism from the catalog. In unit tests that use fake connections, the
    check must be stubbed to a known value: True (deterministic/binary, the
    common case) or False (non-deterministic/ci/ai, the wrong-numbers case).
    """
    import shared.semantic.collation_profiler as cp

    async def _check(*, conn_obj, connector, schema, table, column,
                     tenant_session=None):
        return deterministic

    monkeypatch.setattr(cp, "check_column_collation_deterministic", _check)



def _artifact():
    return types.SimpleNamespace(
        id=uuid.uuid4(), grain_keys=None, attribute_edges=None,
        passenger_columns=None, row_manifest=None, active_refresh_run_id=None,
        query_fingerprint="fp1",
    )


def _fake_source_conn():
    """A fake source connection with a connection_type attribute for
    resolve_connector_type (required by the artifact all-columns rule)."""
    return types.SimpleNamespace(connection_type="postgresql")


def _grain_key_draft(key_id, col_id, phys):
    """A MaterializedGrainKey draft the producer would supply (spec §3.6)."""
    from shared.semantic.artifact_manifest import (
        KIND_PHYSICAL_COLUMN, MaterializedGrainKey,
    )
    return MaterializedGrainKey(
        ordinal=0, key_id=key_id, kind=KIND_PHYSICAL_COLUMN,
        physical_column=phys, source_dimension_id=key_id.split(":", 1)[1],
        input_column_ids=[col_id],
    )


def _passenger_draft_for(rel, *, key_grain_key_id, detail_column_id,
                         passenger_column, key_grain_column="country_id"):
    """A PassengerBuildDraft the producer would supply when the flag is on (§3.5).

    ``advance_artifact_manifest`` reads the BUILT passenger/diagnostic names from
    this draft (never source names). The plan's ``passenger_column`` is what the
    artifact-local reverse check reads, so it must match the source stub key.
    """
    from shared.semantic.passenger_manifest_planner import (
        PassengerBuildDraft, PassengerPlan,
    )
    plan = PassengerPlan(
        relationship_id=str(rel.id),
        attribute_key=f"attr:{rel.id}",
        key_grain_key_id=key_grain_key_id,
        key_grain_column=key_grain_column,
        passenger_column=passenger_column,
        detail_ndistinct_column=f"{passenger_column}__nd",
        detail_nullcount_column=f"{passenger_column}__nc",
        detail_column_id=str(detail_column_id),
        detail_type="INT",
        cardinality=str(rel.cardinality),
        declaration_hash=str(rel.declaration_hash),
        select_fragments=[],
    )
    return PassengerBuildDraft(plans=[plan])


@pytest.mark.asyncio
async def test_advance_off_persists_no_passenger_and_no_trust(monkeypatch):
    # Build flag OFF (default) / no passenger_draft: the passenger physical column
    # does NOT exist in the built table, so NO passenger/edge record is persisted
    # (Fable R1 #5 — a descriptive source-name record would LIE about the physical
    # shape and mislead the incremental shape guard). The live trust pointer STAYS
    # NULL and no artifact-local evidence is staged.
    table_id = uuid.uuid4()
    key_id, detail_id = uuid.uuid4(), uuid.uuid4()
    rel = _rel(key_id=key_id, detail_id=detail_id)
    columns = {
        key_id: _col(key_id, table_id, "country_id", "INT"),
        detail_id: _col(detail_id, table_id, "country_code", "INT"),
    }
    tables = {table_id: types.SimpleNamespace(physical_name="public.geo")}
    db = _ManifestDB([rel], columns, tables)
    _patch_settings(monkeypatch, "off")
    art = _artifact()
    gk = _grain_key_draft("dim:owning", str(key_id), "country_id")

    await dv.advance_artifact_manifest(
        db=db, model_id=uuid.uuid4(), artifact=art, artifact_kind="AGGREGATE",
        artifact_refresh_run_id=uuid.uuid4(), target_conn=types.SimpleNamespace(),
        target_schema="agg_meta", physical_table_name="agg_x",
        grain_keys=[gk],  # passenger_draft=None (materialisation off)
    )
    # Real grain manifest persisted; NO descriptive passenger/edge lie.
    assert art.grain_keys and art.grain_keys[0]["key_id"] == "dim:owning"
    assert art.attribute_edges == []
    assert art.passenger_columns == []
    assert art.active_refresh_run_id is None  # trust NOT earned
    assert db.staged == []  # no artifact-local evidence when nothing was materialised


@pytest.mark.asyncio
async def test_advance_on_all_pass_sets_pointer(monkeypatch):
    # Build flag ON + every artifact-local check empty -> pointer set to the run,
    # VERIFIED evidence bound to the manifest hash.
    table_id = uuid.uuid4()
    key_id, detail_id = uuid.uuid4(), uuid.uuid4()
    rel = _rel(cardinality="BIJECTION", key_id=key_id, detail_id=detail_id)
    columns = {
        key_id: _col(key_id, table_id, "country_id", "INT"),
        detail_id: _col(detail_id, table_id, "country_code", "INT"),
    }
    tables = {table_id: types.SimpleNamespace(physical_name="public.geo")}
    db = _ManifestDB([rel], columns, tables)
    _patch_settings(monkeypatch, "automatic")
    _patch_target_connector(monkeypatch)
    _patch_source(monkeypatch, {})  # all artifact-local checks empty
    run_id = uuid.uuid4()
    art = _artifact()

    # §3.5: with the flag on, the producer supplies the BUILT grain-key + passenger
    # draft; advance reads/verifies the BUILT names from them (never source names).
    gk = _grain_key_draft("dim:owning", str(key_id), "country_id")
    draft = _passenger_draft_for(
        rel, key_grain_key_id="dim:owning", detail_column_id=detail_id,
        passenger_column="country_code__passenger",
    )
    await dv.advance_artifact_manifest(
        db=db, model_id=uuid.uuid4(), artifact=art, artifact_kind="AGGREGATE",
        artifact_refresh_run_id=run_id, target_conn=types.SimpleNamespace(),
        target_schema="agg_meta", physical_table_name="agg_x",
        grain_keys=[gk], passenger_draft=draft,
    )
    assert art.active_refresh_run_id == run_id
    assert len(db.staged) == 1
    ev = db.staged[0]
    assert ev.status == VERIFIED
    assert ev.artifact_kind == "AGGREGATE"
    assert ev.scope_kind == "PERSONA_ARTIFACT"
    assert ev.artifact_manifest_hash is not None


@pytest.mark.asyncio
async def test_advance_on_broken_edge_leaves_pointer_null(monkeypatch):
    # Build flag ON but the artifact-local BIJECTION reverse check finds a folded
    # label (two keys share one passenger) -> BROKEN -> pointer STAYS NULL.
    table_id = uuid.uuid4()
    key_id, detail_id = uuid.uuid4(), uuid.uuid4()
    rel = _rel(cardinality="BIJECTION", key_id=key_id, detail_id=detail_id)
    columns = {
        key_id: _col(key_id, table_id, "country_id", "INT"),
        detail_id: _col(detail_id, table_id, "country_code", "INT"),
    }
    tables = {table_id: types.SimpleNamespace(physical_name="public.geo")}
    db = _ManifestDB([rel], columns, tables)
    _patch_settings(monkeypatch, "automatic")
    _patch_target_connector(monkeypatch)
    # Reverse strict check over the BUILT passenger returns a row -> BROKEN. The
    # check reads the BUILT key grain column + passenger column from the draft.
    _patch_source(monkeypatch, {
        'COUNT(DISTINCT "country_id")': [{"country_code__passenger": 1}],
    })
    art = _artifact()
    gk = _grain_key_draft("dim:owning", str(key_id), "country_id")
    draft = _passenger_draft_for(
        rel, key_grain_key_id="dim:owning", detail_column_id=detail_id,
        passenger_column="country_code__passenger",
    )

    await dv.advance_artifact_manifest(
        db=db, model_id=uuid.uuid4(), artifact=art, artifact_kind="AGGREGATE",
        artifact_refresh_run_id=uuid.uuid4(), target_conn=types.SimpleNamespace(),
        target_schema="agg_meta", physical_table_name="agg_x",
        grain_keys=[gk], passenger_draft=draft,
    )
    assert art.active_refresh_run_id is None
    assert db.staged and db.staged[0].status == BROKEN


@pytest.mark.asyncio
async def test_advance_pocket_writes_row_manifest(monkeypatch):
    # A pocket build writes the versioned row_manifest (not aggregate grain_keys).
    table_id = uuid.uuid4()
    key_id, detail_id = uuid.uuid4(), uuid.uuid4()
    rel = _rel(cardinality="BIJECTION", key_id=key_id, detail_id=detail_id)
    columns = {
        key_id: _col(key_id, table_id, "k", "INT"),
        detail_id: _col(detail_id, table_id, "d", "INT"),
    }
    tables = {table_id: types.SimpleNamespace(physical_name="t")}
    db = _ManifestDB([rel], columns, tables)
    _patch_settings(monkeypatch, "off")  # descriptive only
    art = _artifact()

    await dv.advance_artifact_manifest(
        db=db, model_id=uuid.uuid4(), artifact=art, artifact_kind="POCKET",
        artifact_refresh_run_id=uuid.uuid4(), target_conn=types.SimpleNamespace(),
        target_schema="pocket_meta", physical_table_name="pk_x",
    )
    assert art.row_manifest is not None
    assert art.attribute_edges is None  # aggregate fields untouched for a pocket
    assert art.active_refresh_run_id is None


@pytest.mark.asyncio
async def test_advance_never_raises(monkeypatch):
    # A DB whose execute() raises must be swallowed — never propagate into refresh.
    class _BoomDB:
        staged = []

        async def execute(self, stmt):
            raise RuntimeError("db down")

        def add(self, row):
            self.staged.append(row)

        async def get(self, model, pk):
            return None

    _patch_settings(monkeypatch, "automatic")
    art = _artifact()
    await dv.advance_artifact_manifest(
        db=_BoomDB(), model_id=uuid.uuid4(), artifact=art, artifact_kind="AGGREGATE",
        artifact_refresh_run_id=uuid.uuid4(), target_conn=types.SimpleNamespace(),
        target_schema="s", physical_table_name="t",
    )
    # Fail-closed: on any error the pointer is not advanced.
    assert art.active_refresh_run_id is None


# ---------------------------------------------------------------------------
# Gap-6 / Bug-7806 build tests (spec §3.5/§3.6): real grain manifest + edge ids.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_advance_materialised_persists_grain_keys_and_edge_identity(monkeypatch):
    """Spec §3.5/§3.6/§2.4: with the flag ON + a passenger_draft, the supplied
    grain-key draft is persisted, and each edge carries the BUILT passenger name +
    attribute_key (attr:<uuid>) + key_grain_key_id (the dim:<uuid> of the grain key
    that holds the relationship KEY column) — all resolved from the draft, not from
    source names."""
    table_id = uuid.uuid4()
    key_id, detail_id = uuid.uuid4(), uuid.uuid4()
    rel = _rel(cardinality="BIJECTION", key_id=key_id, detail_id=detail_id)
    columns = {
        key_id: _col(key_id, table_id, "country_id", "INT"),
        detail_id: _col(detail_id, table_id, "country_code", "INT"),
    }
    tables = {table_id: types.SimpleNamespace(physical_name="public.geo")}
    db = _ManifestDB([rel], columns, tables)
    _patch_settings(monkeypatch, "automatic")
    _patch_target_connector(monkeypatch)
    _patch_source(monkeypatch, {})  # all checks empty -> VERIFIED
    art = _artifact()

    # The dimension grain key holds the KEY column -> the edge's key_grain_key_id
    # must equal this key's key_id; the passenger draft supplies the BUILT names.
    gk = _grain_key_draft("dim:owning-dim", str(key_id), "country_id")
    draft = _passenger_draft_for(
        rel, key_grain_key_id="dim:owning-dim", detail_column_id=detail_id,
        passenger_column="country_code__passenger",
    )

    await dv.advance_artifact_manifest(
        db=db, model_id=uuid.uuid4(), artifact=art, artifact_kind="AGGREGATE",
        artifact_refresh_run_id=uuid.uuid4(), target_conn=types.SimpleNamespace(),
        target_schema="agg_meta", physical_table_name="agg_x",
        grain_keys=[gk], passenger_draft=draft,
    )
    # Real grain manifest persisted (never []).
    assert art.grain_keys and art.grain_keys[0]["key_id"] == "dim:owning-dim"
    edge = art.attribute_edges[0]
    assert edge["attribute_key"] == f"attr:{rel.id}"
    assert edge["key_grain_key_id"] == "dim:owning-dim"
    assert edge["detail_passenger_column"] == "country_code__passenger"  # BUILT name
    passenger = art.passenger_columns[0]
    assert passenger["attribute_key"] == f"attr:{rel.id}"
    assert passenger["detail_ndistinct_column"]
    assert passenger["detail_nullcount_column"]


@pytest.mark.asyncio
async def test_advance_no_relationship_still_persists_grain_and_pointer(monkeypatch):
    """Spec §3.6: a grained physical/expression artifact with NO relationships still
    persists real grain_keys and sets the active pointer (the physical build is the
    proof) — it must NEVER hash grain_keys=[] or skip the manifest."""
    db = _ManifestDB([], {}, {})  # no relationships
    _patch_settings(monkeypatch, "off")
    run_id = uuid.uuid4()
    art = _artifact()
    gk = _grain_key_draft("dim:d1", str(uuid.uuid4()), "region")

    await dv.advance_artifact_manifest(
        db=db, model_id=uuid.uuid4(), artifact=art, artifact_kind="AGGREGATE",
        artifact_refresh_run_id=run_id, target_conn=types.SimpleNamespace(),
        target_schema="agg_meta", physical_table_name="agg_x",
        grain_keys=[gk],
    )
    assert art.grain_keys and art.grain_keys[0]["key_id"] == "dim:d1"
    assert art.attribute_edges == []
    assert art.active_refresh_run_id == run_id  # physical build proven


@pytest.mark.asyncio
async def test_advance_hashes_real_grain_list_not_empty(monkeypatch):
    """Spec §3.5: compute_manifest_hash receives the actual ordered grain manifest;
    a grained artifact never hashes grain_keys=[]. Prove it by asserting the hash
    changes when the grain key differs (an empty-grain hash would be constant)."""
    from shared.semantic.artifact_manifest import compute_manifest_hash
    gk1 = _grain_key_draft("dim:a", "col-a", "a")
    gk2 = _grain_key_draft("dim:b", "col-b", "b")
    h_empty = compute_manifest_hash(grain_keys=[], attribute_edges=[], passenger_columns=[])
    h1 = compute_manifest_hash(grain_keys=[gk1], attribute_edges=[], passenger_columns=[])
    h2 = compute_manifest_hash(grain_keys=[gk2], attribute_edges=[], passenger_columns=[])
    assert h1 != h_empty and h2 != h_empty and h1 != h2


@pytest.mark.asyncio
async def test_advance_exception_clears_pointer_for_edge_artifact(monkeypatch):
    """Fable R1 #7: an internal error mid-advance (after the physical swap) must
    NOT leave an edge-carrying artifact pointing at a PREVIOUS run's trust — clear
    active_refresh_run_id so no route serves stale trust over fresh rows."""
    table_id = uuid.uuid4()
    key_id, detail_id = uuid.uuid4(), uuid.uuid4()
    rel = _rel(key_id=key_id, detail_id=detail_id)
    columns = {
        key_id: _col(key_id, table_id, "country_id", "INT"),
        detail_id: _col(detail_id, table_id, "country_code", "INT"),
    }
    tables = {table_id: types.SimpleNamespace(physical_name="public.geo")}
    db = _ManifestDB([rel], columns, tables)
    _patch_settings(monkeypatch, "off")
    art = _artifact()
    art.active_refresh_run_id = "PREVIOUS-RUN"  # a stale pointer from run N
    art.attribute_edges = [{"relationship_id": str(rel.id)}]  # artifact carries edges

    # Force an internal error AFTER edges are established: make compute_manifest_hash
    # raise so the advance body throws mid-way.
    import shared.semantic.artifact_manifest as am
    def _boom(**kw):
        raise RuntimeError("hash boom")
    monkeypatch.setattr(am, "compute_manifest_hash", _boom)

    gk = _grain_key_draft("dim:owning", str(key_id), "country_id")
    await dv.advance_artifact_manifest(
        db=db, model_id=uuid.uuid4(), artifact=art, artifact_kind="AGGREGATE",
        artifact_refresh_run_id=uuid.uuid4(), target_conn=types.SimpleNamespace(),
        target_schema="agg_meta", physical_table_name="agg_x", grain_keys=[gk],
    )
    # Never raises; the stale pointer is cleared (fail-closed for serving).
    assert art.active_refresh_run_id is None


@pytest.mark.asyncio
async def test_verification_off_persists_grain_but_earns_no_edge_trust(monkeypatch):
    """Fable R3 #2: model.attribute_relationship_verification=off must earn NO edge
    trust (pointer stays NULL for an edge-carrying artifact) even when auto_build is
    on — but the real grain manifest still persists (spec §3.5)."""
    table_id = uuid.uuid4()
    key_id, detail_id = uuid.uuid4(), uuid.uuid4()
    rel = _rel(key_id=key_id, detail_id=detail_id)
    columns = {
        key_id: _col(key_id, table_id, "country_id", "INT"),
        detail_id: _col(detail_id, table_id, "country_code", "INT"),
    }
    tables = {table_id: types.SimpleNamespace(physical_name="public.geo")}
    db = _ManifestDB([rel], columns, tables)

    # auto_build ON but verification OFF.
    async def _get_setting(key, *, tenant_session=None, model_id=None):
        if key == "optimizer.derived_expression_auto_build":
            return "automatic"
        if key == "model.attribute_relationship_verification":
            return "off"
        if key == "model.attribute_relationship_verifier_version":
            return "v0"
        return None
    import shared.config.resolver as res
    monkeypatch.setattr(res, "get_setting", _get_setting)
    _patch_target_connector(monkeypatch)
    _patch_source(monkeypatch, {})  # would pass if it ran
    run_id = uuid.uuid4()
    art = _artifact()
    gk = _grain_key_draft("dim:owning", str(key_id), "country_id")

    await dv.advance_artifact_manifest(
        db=db, model_id=uuid.uuid4(), artifact=art, artifact_kind="AGGREGATE",
        artifact_refresh_run_id=run_id, target_conn=types.SimpleNamespace(),
        target_schema="agg_meta", physical_table_name="agg_x", grain_keys=[gk],
    )
    # Verification OFF -> no artifact-local check ran -> no trust earned.
    assert art.active_refresh_run_id is None
    assert db.staged == []
    # But the real grain manifest still persisted.
    assert art.grain_keys and art.grain_keys[0]["key_id"] == "dim:owning"


# ---------------------------------------------------------------------------
# §3.5 built-name validation (turn-on build): advance reads/verifies BUILT names
# from the draft, never source names.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_advance_verifies_built_grain_name_differing_from_source(monkeypatch):
    """§3.5 / §7.5: a built grain column whose NAME differs from the source key
    column proves verification reads grain_keys[].physical_column — the artifact-
    local check queries the BUILT name, not the source name."""
    table_id = uuid.uuid4()
    key_id, detail_id = uuid.uuid4(), uuid.uuid4()
    rel = _rel(cardinality="BIJECTION", key_id=key_id, detail_id=detail_id)
    columns = {
        key_id: _col(key_id, table_id, "country_id", "INT"),      # SOURCE name
        detail_id: _col(detail_id, table_id, "country_code", "INT"),
    }
    tables = {table_id: types.SimpleNamespace(physical_name="public.geo")}
    db = _ManifestDB([rel], columns, tables)
    _patch_settings(monkeypatch, "automatic")
    _patch_target_connector(monkeypatch)

    # The check queries by the BUILT grain name (dim_country_pk), not the source
    # name (country_id). Only a probe against the BUILT name returns empty (VERIFIED);
    # if the code queried the source name, this stub would not match and the empty
    # default still returns VERIFIED — so we assert the emitted edge carries the
    # BUILT name to prove it was used.
    _patch_source(monkeypatch, {})
    run_id = uuid.uuid4()
    art = _artifact()
    gk = _grain_key_draft("dim:owning", str(key_id), "dim_country_pk")  # BUILT != source
    draft = _passenger_draft_for(
        rel, key_grain_key_id="dim:owning", detail_column_id=detail_id,
        passenger_column="country_code__passenger", key_grain_column="dim_country_pk",
    )

    await dv.advance_artifact_manifest(
        db=db, model_id=uuid.uuid4(), artifact=art, artifact_kind="AGGREGATE",
        artifact_refresh_run_id=run_id, target_conn=types.SimpleNamespace(),
        target_schema="agg_meta", physical_table_name="agg_x",
        grain_keys=[gk], passenger_draft=draft,
    )
    assert art.active_refresh_run_id == run_id
    # The edge's key_grain_column is the BUILT name from grain_keys[].physical_column,
    # NOT the source column name.
    assert art.attribute_edges[0]["key_grain_column"] == "dim_country_pk"
    assert art.attribute_edges[0]["detail_passenger_column"] == "country_code__passenger"


@pytest.mark.asyncio
async def test_advance_unresolved_built_grain_key_earns_no_trust(monkeypatch):
    """§3.5 fail-closed: if the edge's key_grain_key_id does not resolve to a grain
    entry in the draft, no trust is earned (source-routes)."""
    table_id = uuid.uuid4()
    key_id, detail_id = uuid.uuid4(), uuid.uuid4()
    rel = _rel(cardinality="BIJECTION", key_id=key_id, detail_id=detail_id)
    columns = {
        key_id: _col(key_id, table_id, "country_id", "INT"),
        detail_id: _col(detail_id, table_id, "country_code", "INT"),
    }
    tables = {table_id: types.SimpleNamespace(physical_name="public.geo")}
    db = _ManifestDB([rel], columns, tables)
    _patch_settings(monkeypatch, "automatic")
    _patch_target_connector(monkeypatch)
    _patch_source(monkeypatch, {})
    art = _artifact()
    # grain key id "dim:owning" but the passenger plan points at a DIFFERENT id.
    gk = _grain_key_draft("dim:owning", str(key_id), "country_id")
    draft = _passenger_draft_for(
        rel, key_grain_key_id="dim:NOT-A-REAL-KEY", detail_column_id=detail_id,
        passenger_column="country_code__passenger",
    )

    await dv.advance_artifact_manifest(
        db=db, model_id=uuid.uuid4(), artifact=art, artifact_kind="AGGREGATE",
        artifact_refresh_run_id=uuid.uuid4(), target_conn=types.SimpleNamespace(),
        target_schema="agg_meta", physical_table_name="agg_x",
        grain_keys=[gk], passenger_draft=draft,
    )
    # Unresolved built grain -> no trust; edge omitted (not verified).
    assert art.active_refresh_run_id is None


# ---------------------------------------------------------------------------
# Bug-7894: text (VARCHAR/CHAR/STRING) BIJECTION relabel certification.
# These four tests ARE the wrong-numbers guard for the text relabel path.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deploy_text_bijection_is_pending_not_error(monkeypatch):
    """Bug-7894 (test 3): a text BIJECTION detail at DEPLOY scope — before any
    serve-side artifact exists — records a NON-SERVING PENDING row, NOT a permanent
    ERROR. The relationship is genuinely 1:1 by data; the block is the missing
    artifact-time collation certification, not a defect. PENDING never serves (the
    router trust predicate admits only VERIFIED) and clears to VERIFIED once the
    passenger aggregate is built and re-verified."""
    table_id = uuid.uuid4()
    key_id, detail_id = uuid.uuid4(), uuid.uuid4()
    rel = _rel(cardinality="BIJECTION", key_id=key_id, detail_id=detail_id)
    columns = {
        key_id: _col(key_id, table_id, "country_id", "INT"),
        detail_id: _col(detail_id, table_id, "country_name", "VARCHAR"),
    }
    tables = {table_id: types.SimpleNamespace(physical_name="public.geo")}
    db = _FakeDB([rel], columns, tables)
    _patch_source(monkeypatch, {})  # source checks would pass, but text defers
    # Bug-7892/7900: source detail collation must be deterministic.
    _patch_collation_check(monkeypatch, deterministic=True)

    staged = await dv.verify_model_relationships_on_deploy(
        db=db, model_id=uuid.uuid4(), deployed_version_id=uuid.uuid4(),
        deploy_epoch=2, verifier_version="v0",
        conn_obj=types.SimpleNamespace(), connector="postgresql",
    )
    assert len(staged) == 1
    row = staged[0]
    # The critical assertion: NOT a permanent ERROR (the bug); a pending state.
    assert row.status == PENDING
    assert row.status != ERROR
    assert row.error_code == ERR_UNCERTIFIED_COLLATION
    assert row.artifact_kind == "DEPLOY_CHECK"


@pytest.mark.asyncio
async def test_deploy_text_detail_with_text_key_folding_collation_is_error(monkeypatch):
    """Bug-7899 (wrong numbers): a text detail beside a TEXT key under a FOLDING
    (case-insensitive) collation is ERROR -- the artifact reverse check's
    COUNT(DISTINCT key) would fold together with the labels and mask the fold.
    Fail-closed to source forever when the collation is not group-stable."""
    table_id = uuid.uuid4()
    key_id, detail_id = uuid.uuid4(), uuid.uuid4()
    rel = _rel(cardinality="BIJECTION", key_id=key_id, detail_id=detail_id)
    columns = {
        key_id: _col(key_id, table_id, "country_code", "VARCHAR"),   # TEXT key
        detail_id: _col(detail_id, table_id, "country_name", "VARCHAR"),
    }
    tables = {table_id: types.SimpleNamespace(physical_name="public.geo")}
    db = _FakeDB([rel], columns, tables)
    _patch_source(monkeypatch, {})
    # Bug-7892: the collation probe returns FOLDING -> text key is uncertifiable.
    _patch_collation_check(monkeypatch, deterministic=False)

    staged = await dv.verify_model_relationships_on_deploy(
        db=db, model_id=uuid.uuid4(), deployed_version_id=uuid.uuid4(),
        deploy_epoch=2, verifier_version="v0",
        conn_obj=types.SimpleNamespace(), connector="postgresql",
    )
    assert staged[0].status == ERROR
    assert staged[0].status != PENDING
    assert staged[0].error_code == ERR_UNCERTIFIED_COLLATION


@pytest.mark.asyncio
async def test_deploy_text_bijection_source_forward_violation_is_broken(monkeypatch):
    """Bug-7898 deploy visibility: a text BIJECTION whose SOURCE data is NOT 1:1
    (a key maps to two names) must record BROKEN health evidence at deploy, not a
    reassuring PENDING. The source-side forward check runs even for text (with a
    collation-stable key) so a bad declaration is surfaced honestly."""
    table_id = uuid.uuid4()
    key_id, detail_id = uuid.uuid4(), uuid.uuid4()
    rel = _rel(cardinality="BIJECTION", key_id=key_id, detail_id=detail_id)
    columns = {
        key_id: _col(key_id, table_id, "country_id", "INT"),          # stable key
        detail_id: _col(detail_id, table_id, "country_name", "VARCHAR"),
    }
    tables = {table_id: types.SimpleNamespace(physical_name="public.geo")}
    db = _FakeDB([rel], columns, tables)
    # Forward check (GROUP BY key HAVING COUNT(DISTINCT detail) > 1) returns a row.
    _patch_source(monkeypatch, {'COUNT(DISTINCT "country_name")': [{"country_id": 1}]})
    _patch_collation_check(monkeypatch, deterministic=True)

    staged = await dv.verify_model_relationships_on_deploy(
        db=db, model_id=uuid.uuid4(), deployed_version_id=uuid.uuid4(),
        deploy_epoch=2, verifier_version="v0",
        conn_obj=types.SimpleNamespace(), connector="postgresql",
    )
    assert staged[0].status == BROKEN
    assert staged[0].status != PENDING


@pytest.mark.asyncio
async def test_advance_text_bijection_with_text_key_folding_collation_no_trust(monkeypatch):
    """Bug-7899 at artifact build under a FOLDING collation: a text detail beside
    a TEXT key is refused by the type gate when the target collation folds (case-
    insensitive), so it earns NO trust -- a text key could mask a fold."""
    table_id = uuid.uuid4()
    key_id, detail_id = uuid.uuid4(), uuid.uuid4()
    rel = _rel(cardinality="BIJECTION", key_id=key_id, detail_id=detail_id)
    columns = {
        key_id: _col(key_id, table_id, "country_code", "VARCHAR"),   # TEXT key
        detail_id: _col(detail_id, table_id, "country_name", "VARCHAR"),
    }
    tables = {table_id: types.SimpleNamespace(physical_name="public.geo")}
    db = _ManifestDB([rel], columns, tables)
    _patch_settings(monkeypatch, "automatic")
    _patch_target_connector(monkeypatch)
    _patch_source(monkeypatch, {})  # would pass every check if it ran
    # Bug-7892: collation probe returns FOLDING -> text key is uncertifiable.
    _patch_collation_check(monkeypatch, deterministic=False)
    art = _artifact()
    gk = _grain_key_draft("dim:owning", str(key_id), "country_code")
    draft = _passenger_draft_for(
        rel, key_grain_key_id="dim:owning", detail_column_id=detail_id,
        passenger_column="country_name__passenger", key_grain_column="country_code",
    )

    await dv.advance_artifact_manifest(
        db=db, model_id=uuid.uuid4(), artifact=art, artifact_kind="AGGREGATE",
        artifact_refresh_run_id=uuid.uuid4(), target_conn=types.SimpleNamespace(),
        target_schema="agg_meta", physical_table_name="agg_x",
        grain_keys=[gk], passenger_draft=draft,
    )
    # Folding collation + text key -> refused by the gate -> no trust.
    assert art.active_refresh_run_id is None


@pytest.mark.asyncio
async def test_advance_text_bijection_composite_grain_earns_no_trust(monkeypatch):
    """Bug-7894 R2 finding 1 (wrong numbers): a text relabel on a COMPOSITE-grain
    artifact (e.g. (country_id, year)) must NOT be certified. The per-group
    source-collation ndistinct diagnostic cannot see a key that carries two label
    variants split across grain groups ('Usa' in 2023, 'USA' in 2024); the
    serve-collation forward/reverse checks then fold those variants and self-mask
    into a false VERIFIED. Only an artifact grained EXACTLY on the edge key is
    certifiable. Here a second grain key (year) makes the grain composite -> no
    trust, even though every mocked built check would pass."""
    table_id = uuid.uuid4()
    key_id, detail_id = uuid.uuid4(), uuid.uuid4()
    year_id = uuid.uuid4()
    rel = _rel(cardinality="BIJECTION", key_id=key_id, detail_id=detail_id)
    columns = {
        key_id: _col(key_id, table_id, "country_id", "INT"),          # stable key
        detail_id: _col(detail_id, table_id, "country_name", "VARCHAR"),
    }
    tables = {table_id: types.SimpleNamespace(physical_name="public.geo")}
    db = _ManifestDB([rel], columns, tables)
    _patch_settings(monkeypatch, "automatic")
    _patch_target_connector(monkeypatch)
    _patch_source(monkeypatch, {})  # every built check would pass if it ran
    art = _artifact()
    # COMPOSITE grain: the edge key grain PLUS a second (year) grain key.
    gk_key = _grain_key_draft("dim:owning", str(key_id), "country_id")
    gk_year = _grain_key_draft("dim:year", str(year_id), "year")
    draft = _passenger_draft_for(
        rel, key_grain_key_id="dim:owning", detail_column_id=detail_id,
        passenger_column="country_name__passenger",
    )

    await dv.advance_artifact_manifest(
        db=db, model_id=uuid.uuid4(), artifact=art, artifact_kind="AGGREGATE",
        artifact_refresh_run_id=uuid.uuid4(), target_conn=types.SimpleNamespace(),
        target_schema="agg_meta", physical_table_name="agg_x",
        grain_keys=[gk_key, gk_year], passenger_draft=draft,
    )
    # Composite grain -> text relabel not certifiable -> no trust.
    assert art.active_refresh_run_id is None


@pytest.mark.asyncio
async def test_advance_numeric_bijection_composite_grain_still_trusts(monkeypatch):
    """A NUMERIC detail relabel is collation-independent, so a composite grain does
    not threaten it — the composite-grain restriction is text-only. A numeric edge
    on a composite grain still earns trust when its built checks pass. (Guards
    against over-restricting the non-text path.)"""
    table_id = uuid.uuid4()
    key_id, detail_id = uuid.uuid4(), uuid.uuid4()
    year_id = uuid.uuid4()
    rel = _rel(cardinality="BIJECTION", key_id=key_id, detail_id=detail_id)
    columns = {
        key_id: _col(key_id, table_id, "country_id", "INT"),
        detail_id: _col(detail_id, table_id, "country_code", "INT"),  # numeric detail
    }
    tables = {table_id: types.SimpleNamespace(physical_name="public.geo")}
    db = _ManifestDB([rel], columns, tables)
    _patch_settings(monkeypatch, "automatic")
    _patch_target_connector(monkeypatch)
    _patch_source(monkeypatch, {})
    run_id = uuid.uuid4()
    art = _artifact()
    gk_key = _grain_key_draft("dim:owning", str(key_id), "country_id")
    gk_year = _grain_key_draft("dim:year", str(year_id), "year")
    draft = _passenger_draft_for(
        rel, key_grain_key_id="dim:owning", detail_column_id=detail_id,
        passenger_column="country_code__passenger",
    )

    await dv.advance_artifact_manifest(
        db=db, model_id=uuid.uuid4(), artifact=art, artifact_kind="AGGREGATE",
        artifact_refresh_run_id=run_id, target_conn=types.SimpleNamespace(),
        target_schema="agg_meta", physical_table_name="agg_x",
        grain_keys=[gk_key, gk_year], passenger_draft=draft,
    )
    assert art.active_refresh_run_id == run_id


@pytest.mark.asyncio
async def test_deploy_numeric_bijection_still_verified(monkeypatch):
    """Bug-7894 (test 4): a numeric/date detail is collation-independent and still
    VERIFIES at deploy — the fix must not disturb the non-text path."""
    table_id = uuid.uuid4()
    key_id, detail_id = uuid.uuid4(), uuid.uuid4()
    rel = _rel(cardinality="BIJECTION", key_id=key_id, detail_id=detail_id)
    columns = {
        key_id: _col(key_id, table_id, "country_id", "INT"),
        detail_id: _col(detail_id, table_id, "country_code", "INT"),
    }
    tables = {table_id: types.SimpleNamespace(physical_name="public.geo")}
    db = _FakeDB([rel], columns, tables)
    _patch_source(monkeypatch, {})  # all checks empty -> VERIFIED

    staged = await dv.verify_model_relationships_on_deploy(
        db=db, model_id=uuid.uuid4(), deployed_version_id=uuid.uuid4(),
        deploy_epoch=2, verifier_version="v0",
        conn_obj=types.SimpleNamespace(), connector="postgresql",
    )
    assert staged[0].status == VERIFIED


@pytest.mark.asyncio
async def test_advance_text_bijection_non_folding_collation_verified(monkeypatch):
    """Bug-7894 (test 1): a text BIJECTION detail whose SERVE collation does NOT
    fold two source-distinct labels is CERTIFIED at artifact build — the
    collation-aware reverse-uniqueness check over the BUILT rows is empty -> the
    edge is VERIFIED and servable, bound to this exact run + manifest hash. This is
    the flagship 'serve sales by country_name from a country_code aggregate' case."""
    table_id = uuid.uuid4()
    key_id, detail_id = uuid.uuid4(), uuid.uuid4()
    rel = _rel(cardinality="BIJECTION", key_id=key_id, detail_id=detail_id)
    columns = {
        key_id: _col(key_id, table_id, "country_id", "INT"),
        detail_id: _col(detail_id, table_id, "country_name", "VARCHAR"),  # TEXT
    }
    tables = {table_id: types.SimpleNamespace(physical_name="public.geo")}
    db = _ManifestDB([rel], columns, tables)
    _patch_settings(monkeypatch, "automatic")
    _patch_target_connector(monkeypatch)
    # Bug-7900: BOTH source and target collations must be proven group-stable for
    # a text relabel to certify. Stub both stable (case-sensitive/binary).
    _patch_collation_check(monkeypatch, deterministic=True)
    _patch_source(monkeypatch, {})  # every artifact-local check empty -> VERIFIED
    run_id = uuid.uuid4()
    art = _artifact()
    gk = _grain_key_draft("dim:owning", str(key_id), "country_id")
    draft = _passenger_draft_for(
        rel, key_grain_key_id="dim:owning", detail_column_id=detail_id,
        passenger_column="country_name__passenger",
    )

    await dv.advance_artifact_manifest(
        db=db, model_id=uuid.uuid4(), artifact=art, artifact_kind="AGGREGATE",
        artifact_refresh_run_id=run_id, target_conn=types.SimpleNamespace(),
        target_schema="agg_meta", physical_table_name="agg_x",
        grain_keys=[gk], passenger_draft=draft,
        source_conn=_fake_source_conn(),
    )
    # Certified: the text edge earned trust for this exact run.
    assert art.active_refresh_run_id == run_id
    assert len(db.staged) == 1
    ev = db.staged[0]
    assert ev.status == VERIFIED
    assert ev.artifact_kind == "AGGREGATE"
    assert ev.scope_kind == "PERSONA_ARTIFACT"
    assert ev.artifact_manifest_hash is not None
    # The built edge carries the text passenger column and is emitted.
    assert art.attribute_edges[0]["detail_passenger_column"] == "country_name__passenger"


@pytest.mark.asyncio
async def test_advance_text_bijection_folding_collation_broken(monkeypatch):
    """Bug-7894 (test 2 — THE critical wrong-numbers case): a text BIJECTION detail
    whose SERVE collation FOLDS two source-distinct labels (e.g. case-insensitive
    'US' and 'us', or accent-insensitive names) into one GROUP BY group. The
    collation-aware reverse-uniqueness check over the BUILT rows finds the fold
    (one passenger label maps to more than one key) -> BROKEN -> the edge is NEVER
    served and the trust pointer stays NULL. Without this certification the fold
    would silently relabel the wrong groups and return WRONG NUMBERS."""
    table_id = uuid.uuid4()
    key_id, detail_id = uuid.uuid4(), uuid.uuid4()
    rel = _rel(cardinality="BIJECTION", key_id=key_id, detail_id=detail_id)
    columns = {
        key_id: _col(key_id, table_id, "country_id", "INT"),
        detail_id: _col(detail_id, table_id, "country_name", "VARCHAR"),  # TEXT
    }
    tables = {table_id: types.SimpleNamespace(physical_name="public.geo")}
    db = _ManifestDB([rel], columns, tables)
    _patch_settings(monkeypatch, "automatic")
    _patch_target_connector(monkeypatch)
    # Bug-7900: both collations proven group-stable so the source gate passes and
    # the DATA-level fold below is caught by the built reverse check (not the gate).
    _patch_collation_check(monkeypatch, deterministic=True)
    # Reverse strict check over the BUILT passenger (GROUP BY passenger HAVING
    # COUNT(DISTINCT key) > 1) returns a row: the serve collation folded two
    # distinct source labels under one built key group -> BROKEN. The reverse
    # check counts distinct KEY grain columns per passenger group.
    _patch_source(monkeypatch, {
        'COUNT(DISTINCT "country_id")': [{"country_name__passenger": "us"}],
    })
    art = _artifact()
    gk = _grain_key_draft("dim:owning", str(key_id), "country_id")
    draft = _passenger_draft_for(
        rel, key_grain_key_id="dim:owning", detail_column_id=detail_id,
        passenger_column="country_name__passenger",
    )

    await dv.advance_artifact_manifest(
        db=db, model_id=uuid.uuid4(), artifact=art, artifact_kind="AGGREGATE",
        artifact_refresh_run_id=uuid.uuid4(), target_conn=types.SimpleNamespace(),
        target_schema="agg_meta", physical_table_name="agg_x",
        grain_keys=[gk], passenger_draft=draft,
        source_conn=_fake_source_conn(),
    )
    # Fold detected under the serve collation -> never served.
    assert art.active_refresh_run_id is None
    assert db.staged and db.staged[0].status == BROKEN


# ---------------------------------------------------------------------------
# Bug-7892: text key + group-stable collation certification.
# These tests prove that a text key + text detail BIJECTION relabel CAN be
# certified when the collation is proven group-stable by the behavioral probe.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deploy_text_key_group_stable_collation_is_pending(monkeypatch):
    """Bug-7892 (known-answer proof): a text detail beside a TEXT key under a
    GROUP-STABLE (binary/case-sensitive) collation records PENDING at deploy
    (not ERROR). The collation probe confirms the source collation is group-
    stable, so the text key's distinctness is guaranteed not to fold. PENDING
    defers to artifact-time certification and clears to VERIFIED once the
    artifact reverse check confirms no folding under the serve collation."""
    table_id = uuid.uuid4()
    key_id, detail_id = uuid.uuid4(), uuid.uuid4()
    rel = _rel(cardinality="BIJECTION", key_id=key_id, detail_id=detail_id)
    columns = {
        key_id: _col(key_id, table_id, "country_code", "VARCHAR"),   # TEXT key
        detail_id: _col(detail_id, table_id, "country_name", "VARCHAR"),
    }
    tables = {table_id: types.SimpleNamespace(physical_name="public.geo")}
    db = _FakeDB([rel], columns, tables)
    _patch_source(monkeypatch, {})
    # Bug-7892: per-column collation check returns deterministic -> text key
    # can proceed to artifact-time certification.
    _patch_collation_check(monkeypatch, deterministic=True)

    staged = await dv.verify_model_relationships_on_deploy(
        db=db, model_id=uuid.uuid4(), deployed_version_id=uuid.uuid4(),
        deploy_epoch=2, verifier_version="v0",
        conn_obj=types.SimpleNamespace(), connector="postgresql",
    )
    # Deterministic collation: NOT a permanent ERROR; defers to artifact-time.
    assert staged[0].status == PENDING
    assert staged[0].status != ERROR
    assert staged[0].error_code == ERR_UNCERTIFIED_COLLATION


@pytest.mark.asyncio
async def test_advance_text_key_group_stable_collation_verified(monkeypatch):
    """Bug-7892 (known-answer proof): a text detail beside a TEXT key under a
    GROUP-STABLE target collation is CERTIFIED at artifact build -- the artifact
    reverse check is SOUND because the target collation does not fold, so
    COUNT(DISTINCT key) cannot self-mask. The edge earns trust and can serve.
    This enables the canonical text->text BIJECTION relabel (e.g. flag_code ->
    flag_name) on databases with binary/case-sensitive default collation."""
    table_id = uuid.uuid4()
    key_id, detail_id = uuid.uuid4(), uuid.uuid4()
    rel = _rel(cardinality="BIJECTION", key_id=key_id, detail_id=detail_id)
    columns = {
        key_id: _col(key_id, table_id, "country_code", "VARCHAR"),   # TEXT key
        detail_id: _col(detail_id, table_id, "country_name", "VARCHAR"),
    }
    tables = {table_id: types.SimpleNamespace(physical_name="public.geo")}
    db = _ManifestDB([rel], columns, tables)
    _patch_settings(monkeypatch, "automatic")
    _patch_target_connector(monkeypatch)
    _patch_source(monkeypatch, {})  # all artifact-local checks empty -> VERIFIED
    # Bug-7892: target collation is group-stable -> text key is certifiable.
    _patch_collation_check(monkeypatch, deterministic=True)
    run_id = uuid.uuid4()
    art = _artifact()
    gk = _grain_key_draft("dim:owning", str(key_id), "country_code")
    draft = _passenger_draft_for(
        rel, key_grain_key_id="dim:owning", detail_column_id=detail_id,
        passenger_column="country_name__passenger", key_grain_column="country_code",
    )

    await dv.advance_artifact_manifest(
        db=db, model_id=uuid.uuid4(), artifact=art, artifact_kind="AGGREGATE",
        artifact_refresh_run_id=run_id, target_conn=types.SimpleNamespace(),
        target_schema="agg_meta", physical_table_name="agg_x",
        grain_keys=[gk], passenger_draft=draft,
        source_conn=_fake_source_conn(),
    )
    # Group-stable target collation -> reverse check is sound -> trust earned.
    assert art.active_refresh_run_id == run_id
    assert len(db.staged) == 1
    assert db.staged[0].status == VERIFIED


@pytest.mark.asyncio
async def test_advance_text_key_group_stable_folding_data_broken(monkeypatch):
    """Bug-7892 end-to-end correctness: even under a group-stable collation, if
    the source DATA is not 1:1 (the artifact reverse check finds two keys for
    one passenger), the edge is BROKEN. Collation certification enables the
    check; it does not bypass it."""
    table_id = uuid.uuid4()
    key_id, detail_id = uuid.uuid4(), uuid.uuid4()
    rel = _rel(cardinality="BIJECTION", key_id=key_id, detail_id=detail_id)
    columns = {
        key_id: _col(key_id, table_id, "country_code", "VARCHAR"),   # TEXT key
        detail_id: _col(detail_id, table_id, "country_name", "VARCHAR"),
    }
    tables = {table_id: types.SimpleNamespace(physical_name="public.geo")}
    db = _ManifestDB([rel], columns, tables)
    _patch_settings(monkeypatch, "automatic")
    _patch_target_connector(monkeypatch)
    # Reverse check finds two keys sharing one passenger -> BROKEN.
    _patch_source(monkeypatch, {
        'COUNT(DISTINCT "country_code")': [{"country_name__passenger": "shared"}],
    })
    _patch_collation_check(monkeypatch, deterministic=True)
    art = _artifact()
    gk = _grain_key_draft("dim:owning", str(key_id), "country_code")
    draft = _passenger_draft_for(
        rel, key_grain_key_id="dim:owning", detail_column_id=detail_id,
        passenger_column="country_name__passenger", key_grain_column="country_code",
    )

    await dv.advance_artifact_manifest(
        db=db, model_id=uuid.uuid4(), artifact=art, artifact_kind="AGGREGATE",
        artifact_refresh_run_id=uuid.uuid4(), target_conn=types.SimpleNamespace(),
        target_schema="agg_meta", physical_table_name="agg_x",
        grain_keys=[gk], passenger_draft=draft,
        source_conn=_fake_source_conn(),
    )
    # Data violation detected -> BROKEN even with group-stable collation.
    assert art.active_refresh_run_id is None
    assert db.staged and db.staged[0].status == BROKEN


# ---------------------------------------------------------------------------
# Bug-7900: folding SOURCE detail + deterministic target -> REFUSED.
# A folding source column merges distinct codes during materialization. Even
# if the target keeps them distinct, the data is already merged -> wrong numbers.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deploy_nondeterministic_source_detail_int_key_is_error(monkeypatch):
    """Bug-7900 (known-answer proof 1): a text BIJECTION detail with a non-text
    key (INT) where the SOURCE detail column has a NON-DETERMINISTIC collation
    -> ERROR at deploy. The source detail folds distinct codes before
    materialization, so even a deterministic target would serve merged data.
    The all-columns rule catches this: the source detail column's collation
    must be deterministic."""
    table_id = uuid.uuid4()
    key_id, detail_id = uuid.uuid4(), uuid.uuid4()
    rel = _rel(cardinality="BIJECTION", key_id=key_id, detail_id=detail_id)
    columns = {
        key_id: _col(key_id, table_id, "country_id", "INT"),
        detail_id: _col(detail_id, table_id, "country_name", "VARCHAR"),
    }
    tables = {table_id: types.SimpleNamespace(physical_name="public.geo")}
    db = _FakeDB([rel], columns, tables)
    _patch_source(monkeypatch, {})
    # Source detail has a NON-DETERMINISTIC collation.
    _patch_collation_check(monkeypatch, deterministic=False)

    staged = await dv.verify_model_relationships_on_deploy(
        db=db, model_id=uuid.uuid4(), deployed_version_id=uuid.uuid4(),
        deploy_epoch=2, verifier_version="v0",
        conn_obj=types.SimpleNamespace(), connector="postgresql",
    )
    # Non-deterministic source detail -> ERROR (never PENDING).
    assert staged[0].status == ERROR
    assert staged[0].error_code == ERR_UNCERTIFIED_COLLATION


@pytest.mark.asyncio
async def test_advance_nondeterministic_source_detail_no_trust(monkeypatch):
    """Bug-7900 ARTIFACT-PATH proof: a text BIJECTION detail where the SOURCE
    detail column has a non-deterministic collation -> artifact trust does NOT
    advance, even when the TARGET columns are deterministic. This proves a
    folding source column can NEVER earn trust on the serving path."""
    table_id = uuid.uuid4()
    key_id, detail_id = uuid.uuid4(), uuid.uuid4()
    rel = _rel(cardinality="BIJECTION", key_id=key_id, detail_id=detail_id)
    columns = {
        key_id: _col(key_id, table_id, "country_id", "INT"),
        detail_id: _col(detail_id, table_id, "country_name", "VARCHAR"),
    }
    tables = {table_id: types.SimpleNamespace(physical_name="public.geo")}
    db = _ManifestDB([rel], columns, tables)
    _patch_settings(monkeypatch, "automatic")
    _patch_target_connector(monkeypatch)
    _patch_source(monkeypatch, {})  # would pass data checks
    # SOURCE detail has non-deterministic collation -> all-columns rule fails.
    _patch_collation_check(monkeypatch, deterministic=False)
    art = _artifact()
    gk = _grain_key_draft("dim:owning", str(key_id), "country_id")
    draft = _passenger_draft_for(
        rel, key_grain_key_id="dim:owning", detail_column_id=detail_id,
        passenger_column="country_name__passenger",
    )

    await dv.advance_artifact_manifest(
        db=db, model_id=uuid.uuid4(), artifact=art, artifact_kind="AGGREGATE",
        artifact_refresh_run_id=uuid.uuid4(), target_conn=types.SimpleNamespace(),
        target_schema="agg_meta", physical_table_name="agg_x",
        grain_keys=[gk], passenger_draft=draft,
        source_conn=_fake_source_conn(),
    )
    # Folding source detail -> no trust on the serving path.
    assert art.active_refresh_run_id is None


@pytest.mark.asyncio
async def test_advance_no_source_conn_text_detail_no_trust(monkeypatch):
    """Bug-7900 fail-closed: a legacy caller that does not supply source_conn
    cannot verify source collation -> text relabel trust is NOT advanced
    (source determinism unverifiable at artifact time -> fail closed)."""
    table_id = uuid.uuid4()
    key_id, detail_id = uuid.uuid4(), uuid.uuid4()
    rel = _rel(cardinality="BIJECTION", key_id=key_id, detail_id=detail_id)
    columns = {
        key_id: _col(key_id, table_id, "country_id", "INT"),
        detail_id: _col(detail_id, table_id, "country_name", "VARCHAR"),
    }
    tables = {table_id: types.SimpleNamespace(physical_name="public.geo")}
    db = _ManifestDB([rel], columns, tables)
    _patch_settings(monkeypatch, "automatic")
    _patch_target_connector(monkeypatch)
    _patch_source(monkeypatch, {})
    _patch_collation_check(monkeypatch, deterministic=True)
    art = _artifact()
    gk = _grain_key_draft("dim:owning", str(key_id), "country_id")
    draft = _passenger_draft_for(
        rel, key_grain_key_id="dim:owning", detail_column_id=detail_id,
        passenger_column="country_name__passenger",
    )

    await dv.advance_artifact_manifest(
        db=db, model_id=uuid.uuid4(), artifact=art, artifact_kind="AGGREGATE",
        artifact_refresh_run_id=uuid.uuid4(), target_conn=types.SimpleNamespace(),
        target_schema="agg_meta", physical_table_name="agg_x",
        grain_keys=[gk], passenger_draft=draft,
        # NO source_conn -> fail closed for text relabels.
    )
    assert art.active_refresh_run_id is None


# ---------------------------------------------------------------------------
# Fix 2: text KEY + non-text detail with non-deterministic key -> REFUSED.
# The branch guard includes `or is_text_detail_type(cols.key_type)` so a
# text key enters the collation check regardless of detail type.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deploy_text_key_nontext_detail_nondeterministic_key_is_error(monkeypatch):
    """Fix 2 DEPLOY proof: a VARCHAR key + INT detail BIJECTION where the KEY
    column has a non-deterministic collation -> ERROR. The text key folds
    distinct codes under GROUP BY, self-masking the reverse check (Bug-7899).
    Without the `or is_text_detail_type(cols.key_type)` guard the text key
    would bypass the collation check entirely."""
    table_id = uuid.uuid4()
    key_id, detail_id = uuid.uuid4(), uuid.uuid4()
    rel = _rel(cardinality="BIJECTION", key_id=key_id, detail_id=detail_id)
    columns = {
        key_id: _col(key_id, table_id, "country_code", "VARCHAR"),   # TEXT key
        detail_id: _col(detail_id, table_id, "country_rank", "INT"),  # non-text detail
    }
    tables = {table_id: types.SimpleNamespace(physical_name="public.geo")}
    db = _FakeDB([rel], columns, tables)
    _patch_source(monkeypatch, {})
    # Key has non-deterministic collation.
    _patch_collation_check(monkeypatch, deterministic=False)

    staged = await dv.verify_model_relationships_on_deploy(
        db=db, model_id=uuid.uuid4(), deployed_version_id=uuid.uuid4(),
        deploy_epoch=2, verifier_version="v0",
        conn_obj=types.SimpleNamespace(), connector="postgresql",
    )
    assert staged[0].status == ERROR
    assert staged[0].error_code == ERR_UNCERTIFIED_COLLATION


@pytest.mark.asyncio
async def test_advance_text_key_nontext_detail_nondeterministic_key_no_trust(monkeypatch):
    """Fix 2 ARTIFACT proof: a VARCHAR key + INT detail BIJECTION where the
    source KEY column has a non-deterministic collation -> artifact trust does
    NOT advance. The text key's folding makes COUNT(DISTINCT key) unreliable
    in the artifact reverse check, even though the detail is non-text."""
    table_id = uuid.uuid4()
    key_id, detail_id = uuid.uuid4(), uuid.uuid4()
    rel = _rel(cardinality="BIJECTION", key_id=key_id, detail_id=detail_id)
    columns = {
        key_id: _col(key_id, table_id, "country_code", "VARCHAR"),   # TEXT key
        detail_id: _col(detail_id, table_id, "country_rank", "INT"),  # non-text detail
    }
    tables = {table_id: types.SimpleNamespace(physical_name="public.geo")}
    db = _ManifestDB([rel], columns, tables)
    _patch_settings(monkeypatch, "automatic")
    _patch_target_connector(monkeypatch)
    _patch_source(monkeypatch, {})
    # Key has non-deterministic collation.
    _patch_collation_check(monkeypatch, deterministic=False)
    art = _artifact()
    gk = _grain_key_draft("dim:owning", str(key_id), "country_code")
    draft = _passenger_draft_for(
        rel, key_grain_key_id="dim:owning", detail_column_id=detail_id,
        passenger_column="country_rank__passenger", key_grain_column="country_code",
    )

    await dv.advance_artifact_manifest(
        db=db, model_id=uuid.uuid4(), artifact=art, artifact_kind="AGGREGATE",
        artifact_refresh_run_id=uuid.uuid4(), target_conn=types.SimpleNamespace(),
        target_schema="agg_meta", physical_table_name="agg_x",
        grain_keys=[gk], passenger_draft=draft,
        source_conn=_fake_source_conn(),
    )
    # Non-deterministic text key -> no trust even with non-text detail.
    assert art.active_refresh_run_id is None
