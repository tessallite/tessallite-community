"""Bug-8392 — the pocket admission-to-scan generation race, and the Bug-8393
producer/consumer contract that activates the RLS pocket route.

A pocket's physical table is reused in place across refreshes, so the route
decision names a table, not the generation of it the router proved admissible.
Everything the RLS gate proved — row-preserving shape, every security column
materialised, built for the deployed version — was proved against the row read at
admission. If a refresh completes before the scan, the query reads a different
generation: a narrower slice (missing rows), a table without the security column
(fail-closed 502), or a same-named column with different content (rows the
principal must not see).

The guard re-proves admissibility against LIVE state immediately before the scan,
stamps the generation, scans, re-stamps, and requires equality. Both halves
matter, and both are pinned here.

The second half of the file pins the PRODUCER/CONSUMER contract: a manifest
produced by the real ``shared/pocket/row_manifest`` writer must be accepted by the
real ``_pocket_is_rls_safe`` consumer. That is the leg that proves the activation
actually activates — a producer whose column names or run binding did not match
what the consumer reads would leave the gate permanently closed, which is exactly
the state Bug-8393 was raised for.

Test escape: no test executed a pocket route against a pocket whose live row had
moved on since admission, and no test crossed the producer/consumer seam.
Guard: this file. Tier: T1.
"""
from __future__ import annotations

import types
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.artifact_target_binding import (
    resolve_target_binding_dict,
    target_binding_dict,
)
from shared.config.resolver import clear_cache
from shared.security.predicate_compiler import CompiledPredicate
from shared.semantic.artifact_manifest import MANIFEST_VERSION

from src.api import routes as routes_mod
from src.routing.pocket_generation_guard import (
    PocketGeneration,
    PocketGenerationChangedError,
    _targets_live_location,
    assert_generation_unchanged,
    assert_pocket_route_admissible,
    read_pocket_generation,
)
from src.routing.router import _pocket_is_rls_safe

_DEFINING_SQL = "SELECT * FROM payments WHERE region_code = 'NORTH'"
_VERSION = uuid.uuid4()


def _compiled(columns=("region_code",), mapping=()):
    return CompiledPredicate(
        sql_expression="region_code = 'NORTH'",
        active_rule_ids=("rule-1",),
        security_dimension_columns=tuple(columns),
        mapping_source_ids=tuple(mapping),
    )


_CONN_ID = uuid.uuid4()
_TARGET_ID = uuid.uuid4()
_MODEL_ID = uuid.uuid4()
_SOURCE_CONN_ID = uuid.uuid4()
_SOURCE_CONN_PROJECT_ID = uuid.uuid4()


def _system_session_factory(settings: dict[str, object]):
    session = types.SimpleNamespace()

    async def _execute(statement):
        params = statement.compile().params
        key = next(
            value
            for value in params.values()
            if isinstance(value, str) and value.startswith("source_db.fallback_")
        )
        result = MagicMock()
        result.scalar_one_or_none.return_value = settings.get(key)
        return result

    session.execute = _execute

    @asynccontextmanager
    async def _session_context():
        yield session

    return _session_context


def _conn(config=None):
    """A ProjectConnection-shaped fake. No encrypted_credentials, so the
    routing fingerprint is computed over the connector type + config alone —
    enough to distinguish two storage locations in these tests."""
    return types.SimpleNamespace(
        id=_CONN_ID,
        project_id=_SOURCE_CONN_PROJECT_ID,
        connection_type="postgresql",
        config=(
            config
            if config is not None
            else {"host": "db-a", "port": 5432, "database": "warehouse"}
        ),
        encrypted_credentials=None,
    )


def _source_binding(model_id=None):
    """A minimal source binding dict matching the ArtifactSourceBuildBinding
    shape the producer records under row_manifest["source_binding"]."""
    from shared.artifact_target_binding import (
        routing_fingerprint,
    )
    return {
        "model_id": str(model_id or _MODEL_ID),
        "source_connection_id": str(_SOURCE_CONN_ID),
        "source_connection_project_id": str(_SOURCE_CONN_PROJECT_ID),
        "routing_fingerprint": routing_fingerprint(
            connection_type="postgresql",
            config={"host": "db-a", "port": 5432, "database": "warehouse"},
            resolved_endpoint={"host": "db-a", "port": 5432, "database": "warehouse"},
        ),
    }


def _target(conn_id=None, config=None, target_type="postgresql"):
    return types.SimpleNamespace(
        id=_TARGET_ID,
        project_connection_id=conn_id or _CONN_ID,
        target_type=target_type,
        config=config if config is not None else {"schema": "public"},
    )


def _binding(target=None, conn=None):
    return target_binding_dict(target or _target(), conn or _conn())


def _manifest(columns, run_id, *, version=MANIFEST_VERSION, binding=...,
              source_binding=...):
    return {
        "columns": [
            {"logical_name": c, "physical_column": c} for c in columns
        ],
        "build_refresh_run_id": str(run_id),
        "manifest_version": version,
        "target_binding": _binding() if binding is ... else binding,
        "source_binding": _source_binding() if source_binding is ... else source_binding,
    }


def _row(
    *,
    run_id,
    status="fresh",
    table="pocket_cache",
    schema="public",
    manifest=...,
    manifest_run=None,
    built_version=_VERSION,
    built_epoch=3,
    binding=...,
    source_binding=...,
    target_id=None,
    model_id=None,
):
    return types.SimpleNamespace(
        status=status,
        active_refresh_run_id=run_id,
        physical_table_name=table,
        target_schema=schema,
        target_id=target_id or _TARGET_ID,
        defining_sql=_DEFINING_SQL,
        model_id=model_id or _MODEL_ID,
        row_manifest=(
            _manifest(
                ("region_code", "amount"), manifest_run or run_id,
                binding=binding, source_binding=source_binding,
            )
            if manifest is ...
            else manifest
        ),
        built_for_version_id=built_version,
        built_for_epoch=built_epoch,
    )


class _FakeResult:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class _FakeDb:
    """Returns the queued rows in order, one per ``execute`` — the guard issues
    exactly one statement per read."""

    def __init__(self, rows):
        self._rows = list(rows)
        self.executes = 0

    async def execute(self, _stmt):
        self.executes += 1
        row = self._rows.pop(0) if self._rows else None
        return _FakeResult(row)


def _bound(deployed=_VERSION, epoch=3):
    return types.SimpleNamespace(
        model=types.SimpleNamespace(
            id=uuid.uuid4(), project_id=uuid.uuid4(), slug="m",
            deployed_version_id=deployed, deploy_epoch=epoch,
        )
    )


def _decision(pocket_id, *, compiled=None):
    return types.SimpleNamespace(
        route_type="pocket",
        pocket_id=pocket_id,
        rewritten_query="SELECT * FROM public.pocket_cache WHERE region_code = 'NORTH'",
        security_compiled=compiled,
        aggregate_id=None,
    )


# ---------------------------------------------------------------------------
# Pre-scan re-proof
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_admissible_pocket_returns_its_generation():
    run_id = uuid.uuid4()
    pid = uuid.uuid4()
    db = _FakeDb([_row(run_id=run_id)])

    gen = await assert_pocket_route_admissible(
        db, bound=_bound(), decision=_decision(pid, compiled=_compiled())
    )

    assert gen == PocketGeneration(
        status="fresh",
        active_refresh_run_id=str(run_id),
        physical_table_name="pocket_cache",
        target_schema="public",
        target_id=str(_TARGET_ID),
    )


# ---------------------------------------------------------------------------
# Bug-8473: the build's storage identity must still hold
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize(
    "live_target, live_conn, why",
    [
        # The target was re-pointed at a DIFFERENT connection.
        (_target(conn_id=uuid.uuid4()), _conn(), "target rebound to another connection"),
        # The connection itself now addresses a different database.
        (
            _target(),
            _conn({"host": "db-b", "port": 5432, "database": "warehouse"}),
            "connection endpoint moved",
        ),
        # The target's own config (schema / BigQuery dataset) moved.
        (_target(config={"schema": "other"}), _conn(), "target config re-pointed"),
    ],
)
async def test_route_refused_when_the_build_storage_no_longer_matches(
    live_target, live_conn, why
):
    """A pocket's (schema, table) names a table only WITHIN a database. If the
    target or its connection now addresses a different database, the cached
    pocket would serve a same-named foreign table's rows — and the injected
    row-security predicate would be evaluated against that foreign column, so a
    row the principal's policy never covered could come back."""
    db = _FakeDb([_row(run_id=uuid.uuid4())])
    with pytest.raises(PocketGenerationChangedError):
        await assert_pocket_route_admissible(
            db, bound=_bound(),
            decision=_decision(uuid.uuid4(), compiled=_compiled()),
            target=live_target, conn=live_conn,
        )


@pytest.mark.asyncio
async def test_route_allowed_when_the_build_storage_still_matches():
    db = _FakeDb([_row(run_id=uuid.uuid4())])
    gen = await assert_pocket_route_admissible(
        db, bound=_bound(), decision=_decision(uuid.uuid4(), compiled=_compiled()),
        target=_target(), conn=_conn(),
    )
    assert gen.status == "fresh"


@pytest.mark.asyncio
async def test_legacy_bigquery_target_is_refused_before_manifest_or_binding_proofs():
    """Bug-8761: historical dotted BQ targets cannot serve either RLS mode."""
    db = _FakeDb([_row(run_id=uuid.uuid4())])
    target = _target(
        config={"dataset": "foreign_project.analytics"}, target_type="bigquery"
    )
    conn = types.SimpleNamespace(
        connection_type="bigquery", config={"project_id": "connection-project"},
        encrypted_credentials=None,
    )
    with pytest.raises(PocketGenerationChangedError, match="unsafe legacy"):
        await assert_pocket_route_admissible(
            db, bound=_bound(), decision=_decision(uuid.uuid4()),
            target=target, conn=conn,
        )


@pytest.mark.asyncio
async def test_route_refused_when_fallback_endpoint_moves(monkeypatch):
    """Bug-8482: a setting-only re-point must fail closed before the scan."""
    settings = {
        "source_db.fallback_host": "db-a",
        "source_db.fallback_port": 5432,
        "source_db.fallback_database": "warehouse-a",
    }

    monkeypatch.setattr(
        "shared.db.session.SystemSessionLocal",
        _system_session_factory(settings),
    )
    conn = _conn({})
    clear_cache()
    recorded = await resolve_target_binding_dict(_target(), conn)
    row = _row(run_id=uuid.uuid4(), binding=recorded)

    settings["source_db.fallback_database"] = "warehouse-b"
    clear_cache()

    with pytest.raises(PocketGenerationChangedError):
        await assert_pocket_route_admissible(
            _FakeDb([row]),
            bound=_bound(),
            decision=_decision(uuid.uuid4(), compiled=_compiled()),
            target=_target(),
            conn=conn,
        )
    clear_cache()


@pytest.mark.asyncio
async def test_manifest_without_a_recorded_binding_is_refused():
    """A manifest that exists but does not say which database it describes is
    exactly the artifact the RLS gate would otherwise trust."""
    db = _FakeDb([_row(run_id=uuid.uuid4(), binding=None)])
    with pytest.raises(PocketGenerationChangedError):
        await assert_pocket_route_admissible(
            db, bound=_bound(),
            decision=_decision(uuid.uuid4(), compiled=_compiled()),
            target=_target(), conn=_conn(),
        )


@pytest.mark.asyncio
async def test_pocket_with_no_manifest_is_refused_on_source_binding_grounds():
    """Bug-8780: a pocket whose manifest has no source_binding (or no manifest
    at all) cannot prove which database its rows were read from, and the source
    binding must fail closed — the same pocket would serve rows from an
    unverifiable source. A pocket with no manifest can never serve under row
    security, but it must also refuse the non-RLS route here."""
    db = _FakeDb([_row(run_id=uuid.uuid4(), manifest=None)])
    with pytest.raises(PocketGenerationChangedError):
        await assert_pocket_route_admissible(
            db, bound=_bound(), decision=_decision(uuid.uuid4(), compiled=None),
            target=_target(), conn=_conn(),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "row_kwargs, why",
    [
        ({"status": "invalidating"}, "a refresh is already mutating the table"),
        ({"status": "stale"}, "a deploy/revert or definition edit invalidated it"),
        ({"status": "failed"}, "the last refresh failed"),
        ({"built_epoch": 4}, "built for a superseded deploy epoch"),
        ({"built_version": uuid.uuid4()}, "built for a different model version"),
        ({"built_version": None}, "no build binding at all"),
    ],
)
async def test_inadmissible_live_state_is_refused(row_kwargs, why):
    db = _FakeDb([_row(run_id=uuid.uuid4(), **row_kwargs)])
    with pytest.raises(PocketGenerationChangedError):
        await assert_pocket_route_admissible(
            db, bound=_bound(), decision=_decision(uuid.uuid4(), compiled=_compiled())
        )


@pytest.mark.asyncio
async def test_deleted_pocket_is_refused():
    db = _FakeDb([None])
    with pytest.raises(PocketGenerationChangedError):
        await assert_pocket_route_admissible(
            db, bound=_bound(), decision=_decision(uuid.uuid4(), compiled=_compiled())
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "row_kwargs, why",
    [
        # The live manifest no longer proves the security column is materialised.
        ({"manifest": None}, "manifest gone"),
        (
            {"manifest": {"columns": [], "build_refresh_run_id": "x",
                          "manifest_version": MANIFEST_VERSION}},
            "no columns recorded",
        ),
        # The manifest describes a DIFFERENT build than the live pointer.
        ({"manifest_run": uuid.uuid4()}, "manifest bound to another run"),
    ],
)
async def test_rls_route_refused_when_live_row_no_longer_proves_safety(row_kwargs, why):
    db = _FakeDb([_row(run_id=uuid.uuid4(), **row_kwargs)])
    with pytest.raises(PocketGenerationChangedError):
        await assert_pocket_route_admissible(
            db, bound=_bound(), decision=_decision(uuid.uuid4(), compiled=_compiled())
        )


@pytest.mark.asyncio
async def test_non_rls_route_with_no_source_binding_is_refused():
    """Bug-8780: even a non-RLS route must refuse when the source binding
    cannot be verified — the pocket is serving rows from an unknown source
    database, and the source-route fallback reads from the current one."""
    run_id = uuid.uuid4()
    db = _FakeDb([_row(run_id=run_id, manifest=None)])

    with pytest.raises(PocketGenerationChangedError):
        await assert_pocket_route_admissible(
            db, bound=_bound(), decision=_decision(uuid.uuid4(), compiled=None)
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "row_kwargs",
    [
        {"schema": "other_dataset"},        # BigQuery dataset rebind
        {"table": "pocket_cache_rebuilt"},  # relocated table
    ],
)
async def test_route_refused_when_the_sql_points_at_a_stale_location(row_kwargs):
    """A refresh that re-bound the physical location BEFORE the pre-check would
    otherwise leave us proving the new table and scanning the old one."""
    db = _FakeDb([_row(run_id=uuid.uuid4(), **row_kwargs)])
    with pytest.raises(PocketGenerationChangedError):
        await assert_pocket_route_admissible(
            db, bound=_bound(), decision=_decision(uuid.uuid4(), compiled=_compiled())
        )


@pytest.mark.asyncio
async def test_dotted_table_name_without_a_schema_is_accepted():
    """``physical_table_name`` may already carry the schema; the location check
    must compare identifier PARTS, not a dotted string the SQL never contains."""
    run_id = uuid.uuid4()
    row = _row(run_id=run_id, table="analytics.pocket_cache", schema=None)
    db = _FakeDb([row])
    decision = _decision(uuid.uuid4(), compiled=_compiled())
    decision.rewritten_query = (
        'SELECT * FROM "analytics"."pocket_cache" WHERE region_code = \'NORTH\''
    )

    gen = await assert_pocket_route_admissible(db, bound=_bound(), decision=decision)
    assert gen.physical_table_name == "analytics.pocket_cache"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "schema,table,old_location_sql",
    [
        # Live schema is a PREFIX of the schema the SQL was rewritten against
        # (the BigQuery dataset-rebind shape: analytics_eu -> analytics).
        ("analytics", "pkt_sales",
         'SELECT * FROM "analytics_eu"."pkt_sales" WHERE region_code = \'NORTH\''),
        # Live table is a PREFIX of the table the SQL scans.
        ("public", "pkt_sales",
         'SELECT * FROM "public"."pkt_sales_v2" WHERE region_code = \'NORTH\''),
        # Live parts appear only inside a user-supplied filter literal.
        ("analytics", "pkt_sales",
         "SELECT * FROM \"other\".\"tbl\" WHERE note = 'analytics pkt_sales'"),
        # Live DOTTED location appears only inside a single-quoted filter
        # literal (round-3 finding 1: the R2 structural match must not be
        # satisfiable by literal text).
        ("analytics", "pkt_sales",
         "SELECT * FROM \"analytics_old\".\"pkt_sales\" WHERE note = 'analytics.pkt_sales'"),
        # Live DOTTED location appears only inside a comment.
        ("analytics", "pkt_sales",
         'SELECT * FROM "analytics_old"."pkt_sales" /* analytics.pkt_sales */'),
    ],
)
async def test_location_reproof_rejects_prefix_colliding_rebinds(
    schema, table, old_location_sql
):
    """Bug-8392 R2 finding 1: the live-location re-proof must be an exact
    structural match of the emitted table reference, not independent substring
    membership. A refresh that re-bound the pocket to a location whose
    identifier parts are substrings of the SQL (schema/table prefixes, or text
    inside a filter literal) previously false-passed, so the pre-check proved
    the NEW build while the scan read the OLD table — a superseded generation
    served under a stable stamp, invisible to the post-check.
    """
    run = uuid.uuid4()
    row = _row(run_id=run, schema=schema, table=table)
    db = _FakeDb([row])
    decision = _decision(uuid.uuid4())
    decision.rewritten_query = old_location_sql
    with pytest.raises(PocketGenerationChangedError):
        await assert_pocket_route_admissible(db, bound=_bound(), decision=decision)


def test_location_reproof_ignores_non_scan_target_nodes():
    row = types.SimpleNamespace(
        target_schema="analytics", physical_table_name="pkt_new"
    )
    sql = (
        'SELECT a FROM "analytics"."pkt_old" '
        'WHERE x IN (SELECT y FROM "analytics"."pkt_new")'
    )
    assert _targets_live_location(sql, row, "postgres") is False


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ('SELECT a FROM "analytics"."pkt_new"', True),
        ('SELECT a FROM "other"."table" JOIN "analytics"."pkt_new" ON 1 = 1', True),
        ('WITH p AS (SELECT a FROM "analytics"."pkt_new") SELECT a FROM p', True),
        ('SELECT a FROM (SELECT a FROM "analytics"."pkt_new") AS p', True),
        ('WITH p AS (SELECT a FROM "analytics"."pkt_old" WHERE x IN (SELECT y FROM "analytics"."pkt_new")) SELECT a FROM p', False),
        ('WITH p AS (SELECT a FROM "analytics"."pkt_new"), q AS (SELECT a FROM "analytics"."pkt_new") SELECT p.a FROM p JOIN q ON 1 = 1', False),
        ('SELECT a FROM "analytics"."pkt_new" JOIN "analytics"."pkt_new" AS p2 ON 1 = 1', False),
    ],
)
def test_location_reproof_only_accepts_one_direct_outer_relation(sql, expected):
    """Bug-8781: nested/derived/ambiguous matching names cannot prove the scan."""
    row = types.SimpleNamespace(
        target_schema="analytics", physical_table_name="pkt_new"
    )
    assert _targets_live_location(sql, row, "postgres") is expected


@pytest.mark.asyncio
async def test_undeployed_model_skips_the_version_binding_check():
    """Mirrors the matcher: the build-binding gate only applies once the model
    has a deployed pointer."""
    run_id = uuid.uuid4()
    db = _FakeDb([_row(run_id=run_id, built_version=None, built_epoch=None)])

    gen = await assert_pocket_route_admissible(
        db, bound=_bound(deployed=None, epoch=0),
        decision=_decision(uuid.uuid4(), compiled=None),
    )
    assert gen.status == "fresh"


# ---------------------------------------------------------------------------
# Post-scan stamp comparison
# ---------------------------------------------------------------------------

def _gen(run_id, *, status="fresh", table="pocket_cache", schema="public",
         target_id=None):
    return PocketGeneration(
        status=status,
        active_refresh_run_id=str(run_id),
        physical_table_name=table,
        target_schema=schema,
        target_id=str(target_id or _TARGET_ID),
    )


def test_unchanged_generation_passes():
    run_id = uuid.uuid4()
    assert_generation_unchanged(_gen(run_id), _gen(run_id), pocket_id="p")


@pytest.mark.parametrize(
    "after_factory",
    [
        lambda run_id: _gen(uuid.uuid4()),                       # refreshed
        lambda run_id: _gen(run_id, status="invalidating"),      # refresh started
        lambda run_id: _gen(run_id, table="pocket_cache_v2"),    # relocated
        lambda run_id: _gen(run_id, schema="other_dataset"),     # dataset rebind
        lambda run_id: _gen(run_id, target_id=uuid.uuid4()),     # re-pointed target
        lambda run_id: None,                                     # pocket deleted
    ],
)
def test_changed_generation_is_refused(after_factory):
    run_id = uuid.uuid4()
    with pytest.raises(PocketGenerationChangedError):
        assert_generation_unchanged(
            _gen(run_id), after_factory(run_id), pocket_id="p"
        )


@pytest.mark.asyncio
async def test_read_pocket_generation_normalises_uuids():
    run_id = uuid.uuid4()
    db = _FakeDb([_row(run_id=run_id)])
    gen = await read_pocket_generation(db, uuid.uuid4())
    assert gen.active_refresh_run_id == str(run_id)


@pytest.mark.asyncio
async def test_read_pocket_generation_returns_none_for_a_deleted_pocket():
    assert await read_pocket_generation(_FakeDb([None]), uuid.uuid4()) is None


# ---------------------------------------------------------------------------
# Through the real execution chokepoint
# ---------------------------------------------------------------------------

class _ExecDb(_FakeDb):
    """Adds the ORM ``get`` calls ``execute_routed_query`` makes."""

    def __init__(self, rows, pocket, target):
        super().__init__(rows)
        self._pocket = pocket
        self._target = target

    async def get(self, cls, key):
        if cls.__name__ == "PocketDefinition":
            return self._pocket
        if cls.__name__ == "DataTarget":
            return self._target
        return None


def _exec_pocket(run_id):
    return types.SimpleNamespace(
        id=uuid.uuid4(), target_id=_TARGET_ID,
        physical_table_name="pocket_cache", target_schema="public",
        active_refresh_run_id=run_id,
    )


@pytest.mark.asyncio
async def test_execution_returns_rows_when_the_generation_holds():
    run_id = uuid.uuid4()
    pocket = _exec_pocket(run_id)
    target = _target()
    db = _ExecDb([_row(run_id=run_id), _row(run_id=run_id)], pocket, target)
    decision = _decision(pocket.id, compiled=_compiled())

    with patch.object(
        routes_mod, "resolve_endpoint_connection", AsyncMock(return_value=_conn())
    ), patch.object(
        routes_mod, "execute_on_connection",
        AsyncMock(return_value=([{"region_code": "NORTH"}], 10, ["region_code"])),
    ):
        rows, _bytes, cols, endpoint = await routes_mod.execute_routed_query(
            _bound(), decision, db
        )

    assert rows == [{"region_code": "NORTH"}]
    assert cols == ["region_code"]
    assert endpoint is target


@pytest.mark.asyncio
async def test_execution_discards_rows_when_a_refresh_lands_mid_scan():
    """The pre-scan proof passed, the scan ran, and the pocket was refreshed
    underneath it. The rows must NEVER be returned — they may describe a
    different row population than the one proved to be row-security-safe."""
    run_id = uuid.uuid4()
    pocket = _exec_pocket(run_id)
    target = _target()
    # Second read reports a NEW run: a refresh completed across the scan.
    db = _ExecDb([_row(run_id=run_id), _row(run_id=uuid.uuid4())], pocket, target)
    decision = _decision(pocket.id, compiled=_compiled())

    leaked = [{"region_code": "SOUTH"}]
    with patch.object(
        routes_mod, "resolve_endpoint_connection", AsyncMock(return_value=_conn())
    ), patch.object(
        routes_mod, "execute_on_connection",
        AsyncMock(return_value=(leaked, 10, ["region_code"])),
    ):
        with pytest.raises(PocketGenerationChangedError):
            await routes_mod.execute_routed_query(_bound(), decision, db)


@pytest.mark.asyncio
async def test_execution_refuses_before_scanning_an_unproven_generation():
    """A refresh that completed BEFORE the scan started is caught by the
    pre-check, so no query is ever sent to the cache table."""
    pocket = _exec_pocket(uuid.uuid4())
    target = _target()
    db = _ExecDb([_row(run_id=uuid.uuid4(), status="invalidating")], pocket, target)
    decision = _decision(pocket.id, compiled=_compiled())

    exec_mock = AsyncMock(return_value=([], 0, []))
    with patch.object(
        routes_mod, "resolve_endpoint_connection", AsyncMock(return_value=_conn())
    ), patch.object(routes_mod, "execute_on_connection", exec_mock):
        with pytest.raises(PocketGenerationChangedError):
            await routes_mod.execute_routed_query(_bound(), decision, db)

    exec_mock.assert_not_awaited()


@pytest.fixture(autouse=True)
def _mock_source_binding_matches_live():
    """All tests in this module exercise pocket guard logic, not the source
    binding resolution machinery (which needs a real database).  The source
    binding is verified by the shared artefact binding test suite."""
    target = "shared.artifact_target_binding.source_build_binding_matches_live"
    with patch(target, AsyncMock(return_value=True)):
        yield


# ---------------------------------------------------------------------------
# Bug-8393 producer -> Bug-8018 consumer contract
# ---------------------------------------------------------------------------

async def _produce(pocket, run_id, catalogue):
    from shared.pocket.row_manifest import write_pocket_row_manifest

    with patch(
        "shared.source_introspection.discover_columns",
        AsyncMock(return_value=catalogue),
    ):
        return await write_pocket_row_manifest(
            pocket=pocket, run_id=run_id, target_conn=_conn(),
            target_schema="public", target_table="pocket_cache",
            target=_target(), deployed_version_id=_VERSION,
            source_binding_dict=_source_binding(),
        )


@pytest.mark.asyncio
async def test_real_producer_output_is_accepted_by_the_real_rls_gate():
    """The activation proof: a manifest written by the refresh producer must
    satisfy the router's RLS gate for a security column the table really has."""
    run_id = uuid.uuid4()
    pocket = types.SimpleNamespace(
        id=uuid.uuid4(), defining_sql=_DEFINING_SQL, query_fingerprint="fp",
        row_manifest=None, active_refresh_run_id=None,
    )
    written = await _produce(pocket, run_id, [
        {"column_name": "region_code", "data_type": "text", "is_nullable": True},
        {"column_name": "amount", "data_type": "numeric", "is_nullable": True},
    ])

    assert written is True
    assert _pocket_is_rls_safe(pocket, _compiled(("region_code",))) is True


@pytest.mark.asyncio
async def test_producer_case_is_preserved_end_to_end():
    """The consumer matches case-sensitively, so a producer that folded case
    would silently disable the route (or name an absent column at the DB)."""
    run_id = uuid.uuid4()
    pocket = types.SimpleNamespace(
        id=uuid.uuid4(), defining_sql=_DEFINING_SQL, query_fingerprint="fp",
        row_manifest=None, active_refresh_run_id=None,
    )
    await _produce(pocket, run_id, [
        {"column_name": "RegionCode", "data_type": "text", "is_nullable": True},
    ])

    assert _pocket_is_rls_safe(pocket, _compiled(("RegionCode",))) is True
    assert _pocket_is_rls_safe(pocket, _compiled(("regioncode",))) is False


@pytest.mark.asyncio
async def test_security_column_absent_from_the_build_is_refused():
    run_id = uuid.uuid4()
    pocket = types.SimpleNamespace(
        id=uuid.uuid4(), defining_sql=_DEFINING_SQL, query_fingerprint="fp",
        row_manifest=None, active_refresh_run_id=None,
    )
    await _produce(pocket, run_id, [
        {"column_name": "amount", "data_type": "numeric", "is_nullable": True},
    ])

    assert _pocket_is_rls_safe(pocket, _compiled(("region_code",))) is False


@pytest.mark.asyncio
async def test_real_producer_binding_is_accepted_and_a_moved_connection_is_not():
    """Bug-8473 producer -> guard contract. The binding the REAL producer records
    must be the one the REAL guard recomputes, or the two halves of the storage
    proof silently disagree: a producer that recorded nothing (or something the
    guard cannot reproduce) would either kill every pocket route or, worse, be
    waved through."""
    run_id = uuid.uuid4()
    pocket = types.SimpleNamespace(
        id=uuid.uuid4(), defining_sql=_DEFINING_SQL, query_fingerprint="fp",
        row_manifest=None, active_refresh_run_id=None,
    )
    await _produce(pocket, run_id, [
        {"column_name": "region_code", "data_type": "text", "is_nullable": True},
    ])
    assert pocket.row_manifest["target_binding"]["routing_fingerprint"]
    # The real producer also writes source_binding now (Bug-8780).
    assert pocket.row_manifest.get("source_binding")

    built = _row(run_id=run_id, manifest=pocket.row_manifest)

    # Same storage -> admitted.
    gen = await assert_pocket_route_admissible(
        _FakeDb([built]), bound=_bound(),
        decision=_decision(pocket.id, compiled=_compiled()),
        target=_target(), conn=_conn(),
    )
    assert gen.status == "fresh"

    # The connection now addresses a different database -> refused.
    with pytest.raises(PocketGenerationChangedError):
        await assert_pocket_route_admissible(
            _FakeDb([_row(run_id=run_id, manifest=pocket.row_manifest)]),
            bound=_bound(),
            decision=_decision(pocket.id, compiled=_compiled()),
            target=_target(), conn=_conn({"host": "db-b", "database": "warehouse"}),
        )


@pytest.mark.asyncio
async def test_manifest_left_by_a_previous_build_is_not_trusted():
    """The producer binds the manifest to the run it describes; a pointer that
    has since moved on must make the consumer fall back to source."""
    pocket = types.SimpleNamespace(
        id=uuid.uuid4(), defining_sql=_DEFINING_SQL, query_fingerprint="fp",
        row_manifest=None, active_refresh_run_id=None,
    )
    await _produce(pocket, uuid.uuid4(), [
        {"column_name": "region_code", "data_type": "text", "is_nullable": True},
    ])
    assert _pocket_is_rls_safe(pocket, _compiled()) is True

    # A later refresh advanced the pointer without rewriting the manifest.
    pocket.active_refresh_run_id = uuid.uuid4()
    assert _pocket_is_rls_safe(pocket, _compiled()) is False
