"""G4/B02 lifecycle proof at the real matcher/refresh persistence boundary.

This is the counterpart to the model-service definition-writer probe: it uses
the same PostgreSQL tenant-schema shape the production query-router reads, so a
physical slice cannot be admitted merely because an in-memory test object says
it is fresh.  The test records a known slice-A cardinality, proves the real
population mismatch, exercises the shared refresh refusal, edits the persisted
definition to B, and only admits the known slice-B cardinality after the
definition is explicitly rebuilt and the graph proof returns.

Test escape: the previous guard called ``_match`` on a hand-built stale object,
so it could not catch a status/eligibility assignment that was lost at commit
or an old physical row becoming servable after a definition edit.  Guard: this
file's matcher, refresh refusal, and new-session known-value assertions. Tier:
T3.
"""
from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from shared.db.models import (
    DataSource,
    DataTarget,
    Join,
    Model,
    ModelColumn,
    ModelTable,
    PocketDefinition,
    PocketPredicate,
    PocketRefreshRun,
    Project,
    ProjectConnection,
    TenantBase,
)
from shared.pocket import refresh as pocket_refresh
from shared.pocket import row_manifest as pocket_row_manifest
from shared.pocket.refresh import refresh_pocket_definition

from src.execution.dispatcher import execute_on_connection
from src.ir.logical_query import BoundQuery, LogicalQuery
from src.routing import pocket_matcher
from src.routing.pocket_matcher import PocketSkipReason, find_best_pocket
from src.rewrite.pocket import rewrite_for_pocket

pytestmark = pytest.mark.integration

_DB_URL = os.environ.get("TESSALLITE_VERSIONING_DB_URL") or os.environ.get(
    "TESSALLITE_IMPORTER_REHYDRATION_DB_URL"
)


@asynccontextmanager
async def _isolated_schema():
    if not _DB_URL:
        pytest.skip("no versioning/importer Postgres URL configured")
    schema = f"g4b02_{uuid.uuid4().hex}"
    boot = create_async_engine(_DB_URL, future=True)
    async with boot.begin() as conn:
        await conn.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        await conn.exec_driver_sql(f'SET search_path TO "{schema}"')
        await conn.run_sync(TenantBase.metadata.create_all)
    await boot.dispose()

    engine = create_async_engine(_DB_URL, future=True)

    @event.listens_for(engine.sync_engine, "connect")
    def _set_search_path(dbapi_connection, _record):  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute(f'SET search_path TO "{schema}"')
        cursor.close()

    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory, schema
    finally:
        drop = create_async_engine(_DB_URL, future=True)
        async with drop.begin() as conn:
            await conn.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        await drop.dispose()
        await engine.dispose()


def _real_encrypted_credentials() -> bytes:
    from urllib.parse import urlparse

    from shared.security.credential_crypto import encrypt_json

    parsed = urlparse((_DB_URL or "").replace("+asyncpg", ""))
    return encrypt_json({
        "host": parsed.hostname or "localhost",
        "port": parsed.port or 5432,
        "database": (parsed.path or "/").lstrip("/"),
        "user": parsed.username or "tessallite",
        "password": parsed.password or "",
    })


def _target_schema(schema: str) -> str:
    return f"{schema}_pockets"


async def _seed(factory, schema):
    ids = SimpleNamespace(
        project=uuid.uuid4(), model=uuid.uuid4(), connection=uuid.uuid4(),
        source=uuid.uuid4(), target=uuid.uuid4(), fact=uuid.uuid4(),
        dim=uuid.uuid4(), amount=uuid.uuid4(), region=uuid.uuid4(),
        join=uuid.uuid4(), pocket=uuid.uuid4(), predicate=uuid.uuid4(),
        refresh_run=uuid.uuid4(),
    )
    async with factory() as db:
        # The disposable tenant schema also owns the source rows and target
        # cache.  The refresh test below uses the production CTAS/finalisation
        # path against this real PostgreSQL table, so the known A/B values are
        # not synthesized in ORM metadata.
        target_schema = _target_schema(schema)
        await db.execute(text(f'CREATE SCHEMA "{target_schema}"'))
        await db.execute(text(
            f'CREATE TABLE "{schema}"."g4b02_source" '
            "(generation text NOT NULL, id integer NOT NULL, amount integer NOT NULL)"
        ))
        await db.execute(text(
            f'INSERT INTO "{schema}"."g4b02_source" '
            "(generation, id, amount) VALUES "
            "('A', 1, 10), ('A', 2, 20), ('A', 3, 30), "
            "('B', 101, 100), ('B', 102, 200), ('B', 103, 300), "
            "('B', 104, 400), ('B', 105, 500), ('B', 106, 600), "
            "('B', 107, 700)"
        ))
        await db.execute(text(
            f'CREATE TABLE "{target_schema}"."slice_a" AS '
            f'SELECT id, amount FROM "{schema}"."g4b02_source" '
            "WHERE generation = 'A'"
        ))
        db.add(Project(
            id=ids.project, slug="g4b02-project", display_name="G4 B02",
        ))
        db.add(ProjectConnection(
            id=ids.connection, project_id=ids.project,
            display_name="same-db", connection_type="postgresql",
            encrypted_credentials=_real_encrypted_credentials(),
            config={"schema": schema},
        ))
        model = Model(
            id=ids.model, project_id=ids.project, slug="g4b02model",
            display_name="G4 B02 model", seed="g4b02seed",
        )
        db.add(model)
        db.add(DataSource(
            id=ids.source, model_id=ids.model,
            project_connection_id=ids.connection, source_type="postgresql",
            display_name="source", default_schema="public", config={},
        ))
        db.add(DataTarget(
            id=ids.target, model_id=ids.model,
            project_connection_id=ids.connection, target_type="postgresql",
            display_name="target", config={"schema": target_schema},
        ))
        # ``models.target_id`` and the child rows form a cycle; persist the
        # project/connection/model/source/target roots before the graph and
        # artifact rows that reference them.
        await db.flush()
        model.target_id = ids.target
        await db.flush()
        db.add_all([
            ModelTable(
                id=ids.fact, model_id=ids.model, source_id=ids.source,
                table_type="fact", physical_name="fact_sales", alias="fact",
                display_name="Fact",
            ),
            ModelTable(
                id=ids.dim, model_id=ids.model, source_id=ids.source,
                table_type="dim_detail", physical_name="dim_region", alias="region",
                display_name="Region",
            ),
        ])
        await db.flush()
        db.add_all([
            ModelColumn(
                id=ids.amount, model_table_id=ids.fact,
                column_name="amount", data_type="numeric", is_nullable=False,
            ),
            ModelColumn(
                id=ids.region, model_table_id=ids.dim,
                column_name="region", data_type="text", is_nullable=False,
                is_primary_key=True,
            ),
        ])
        await db.flush()
        db.add_all([
            # An INNER edge makes a fact-only query's pocket population
            # unprovable, which is the durable G4 mismatch path.
            Join(
                id=ids.join, model_id=ids.model,
                left_table_id=ids.fact, right_table_id=ids.dim,
                join_type="inner", cardinality="many_to_one",
                left_column_id=ids.amount, right_column_id=ids.region,
                population_participation="preserve_base_rows",
                population_participation_source="manual",
            ),
            PocketDefinition(
                id=ids.pocket, model_id=ids.model, target_id=ids.target,
                physical_table_name="slice_a", target_schema=target_schema,
                defining_sql="SELECT * FROM g4b02model /* slice A */",
                query_fingerprint="slice-a-fingerprint",
                predicate_set_hash="slice-a-predicates", status="fresh",
                row_count=3, storage_bytes=300,
                last_refresh_at=datetime.now(timezone.utc),
                population_eligibility="unknown",
            ),
        ])
        await db.flush()
        db.add(PocketRefreshRun(
            id=ids.refresh_run, pocket_definition_id=ids.pocket,
            refresh_mode="full", status="completed", triggered_by="g4-b02",
            completed_at=datetime.now(timezone.utc), rows_written=3,
            bytes_processed=300,
        ))
        await db.flush()
        pocket = await db.get(PocketDefinition, ids.pocket)
        pocket.active_refresh_run_id = ids.refresh_run
        await db.commit()
    return ids


class _LiveSourceConnection:
    """The connector-neutral surface over the test's real asyncpg session."""

    def __init__(self, conn):
        self._conn = conn

    async def execute(self, sql: str, *args) -> None:
        await self._conn.execute(sql, *args)

    async def fetch(self, sql: str, *args) -> list[dict]:
        return [dict(row) for row in await self._conn.fetch(sql, *args)]

    async def fetch_one(self, sql: str, *args) -> dict | None:
        row = await self._conn.fetchrow(sql, *args)
        return dict(row) if row is not None else None


@asynccontextmanager
async def _live_source(schema: str):
    import asyncpg

    conn = await asyncpg.connect((_DB_URL or "").replace("+asyncpg", ""))
    try:
        await conn.execute(f'SET search_path TO "{schema}"')
        yield _LiveSourceConnection(conn)
    finally:
        await conn.close()


def _bound(
    model, column_id, fingerprint: str, *,
    raw_query: str = "SELECT SUM(amount) FROM g4b02model",
) -> BoundQuery:
    measure = SimpleNamespace(
        id="measure-amount", name="amount", source_column_id=column_id,
        default_agg="sum", is_additive=True, measure_type="standard",
        expression=None, calc_agg_mode=None, semi_additive_behavior=None,
        variant_kind=None,
    )
    logical = LogicalQuery(
        model_id=str(model.id), protocol="jdbc", raw_query=raw_query,
        requested_measures=["amount"], requested_dimensions=[], filters=[], grain=[],
        order_by=[], limit=None, offset=None, query_fingerprint=fingerprint,
    )
    return BoundQuery(
        logical_query=logical, model=model, resolved_measures=[measure],
        resolved_dimensions=[], resolved_filters=[],
        resolved_dimensions_by_name={},
    )


async def _execute_pocket_values(factory, ids, fingerprint: str):
    """Execute a matched pocket query through the production rewrite/dispatcher seam."""
    async with factory() as db:
        db.info["tenant_id"] = "g4b02-tenant"
        model = await db.get(Model, ids.model)
        bound = _bound(
            model,
            ids.amount,
            fingerprint,
            raw_query="SELECT amount FROM g4b02model",
        )
        result = await find_best_pocket(bound, db)
        assert result.pocket is not None
        # The production dialect resolver uses sqlglot's canonical ``postgres``
        # name for the PostgreSQL connector.
        rewritten = rewrite_for_pocket(bound, result.pocket, "postgres")
        connection = await db.get(ProjectConnection, ids.connection)
        rows, _bytes_processed, columns = await execute_on_connection(
            rewritten, connection, db,
        )
        await db.commit()
        return result, rows, columns, rewritten


@pytest.mark.asyncio
async def test_g4_r2_b02_definition_edit_refresh_refusal_and_known_values(monkeypatch):
    """B02: old slice-A rows never re-enter until a real B materialisation."""
    monkeypatch.setattr(
        pocket_matcher, "system_snapshot_get",
        lambda key: {
            "pocket.enabled": True,
            "pocket.require_tenant_filter": False,
        }.get(key),
    )
    monkeypatch.setattr(
        pocket_matcher, "get_setting",
        lambda *args, **kwargs: _true_setting(),
    )
    async with _isolated_schema() as (factory, schema):
        ids = await _seed(factory, schema)

        # Keep the production refresh/finalisation protocol intact.  Only the
        # external router validation, system setting, and connector checkout
        # seams are replaced: the CTAS runs against a real PostgreSQL table,
        # and the real run/finalisation/manifest writers persist its result.
        @asynccontextmanager
        async def _open_live(_conn_obj, **_kwargs):
            async with _live_source(schema) as source:
                yield source

        async def _rewritten_sql(*_args, **_kwargs):
            return (
                f'SELECT id, amount FROM "{schema}"."g4b02_source" '
                "WHERE generation = 'B'"
            )

        async def _describe_materialised_columns(
            _target_conn, *, schema: str | None, table: str, **_kwargs,
        ) -> list[dict]:
            async with _live_source(schema or "") as source:
                return await source.fetch(
                    "SELECT column_name, data_type, is_nullable "
                    "FROM information_schema.columns "
                    "WHERE table_schema = $1 AND table_name = $2 "
                    "ORDER BY ordinal_position",
                    schema, table,
                )

        async def _no_max_rows(*_args, **_kwargs):
            return 0

        class _SystemSession:
            async def __aenter__(self):
                return object()

            async def __aexit__(self, *_args):
                return False

        def _system_session():
            return _SystemSession()

        import shared.db.session as db_session

        monkeypatch.setattr(pocket_refresh, "open_source_connection", _open_live)
        monkeypatch.setattr(pocket_refresh, "_get_rewritten_sql", _rewritten_sql)
        monkeypatch.setattr(
            pocket_refresh, "_validate_pocket_via_router", AsyncMock(return_value=None),
        )
        monkeypatch.setattr(pocket_refresh, "get_setting", _no_max_rows)
        monkeypatch.setattr(pocket_refresh, "SystemSessionLocal", _system_session)
        monkeypatch.setattr(db_session, "SystemSessionLocal", _system_session)
        monkeypatch.setattr(
            pocket_row_manifest,
            "resolve_materialised_columns",
            _describe_materialised_columns,
        )

        # Establish the provable A population, then execute through the real
        # matcher -> pocket rewriter -> shared dispatcher boundary.  A direct
        # target-table read would not catch a rewrite/dispatcher regression.
        async with factory() as db:
            join = await db.get(Join, ids.join)
            join.join_type = "left"
            await db.commit()
        pocket_matcher.invalidate_model_join_graph_cache(ids.model)
        a_result, a_rows, a_columns, a_sql = await _execute_pocket_values(
            factory, ids, "slice-a-fingerprint",
        )
        assert a_result.pocket.id == ids.pocket
        assert a_columns == ["amount"]
        assert [int(row["amount"]) for row in a_rows] == [10, 20, 30]
        assert f'"{_target_schema(schema)}"."slice_a"' in a_sql

        # Return to the real mismatching graph before proving the durable
        # ineligible/refusal state below.
        async with factory() as db:
            join = await db.get(Join, ids.join)
            join.join_type = "inner"
            await db.commit()
        pocket_matcher.invalidate_model_join_graph_cache(ids.model)

        async with factory() as db:
            model = await db.get(Model, ids.model)
            result = await find_best_pocket(
                _bound(model, ids.amount, "slice-a-fingerprint"), db,
            )
            assert result.pocket is None
            assert result.skipped_reason == PocketSkipReason.JOIN_POPULATION_MISMATCH
            await db.commit()

        # A new session sees the mismatch observation and the exact old,
        # physically-built cardinality.  The proof never changes status to
        # fresh/stale as a substitute for the separate eligibility axis.
        async with factory() as db:
            pocket = await db.get(PocketDefinition, ids.pocket)
            assert (
                pocket.status,
                pocket.population_eligibility,
                pocket.population_eligibility_reason,
                pocket.row_count,
            ) == (
                "fresh", "ineligible", "Ineligible: population mismatch", 3,
            )
            refused_run = await refresh_pocket_definition(
                ids.pocket, db, triggered_by="g4-r2", refresh_mode="full",
            )
            assert (
                refused_run.status,
                refused_run.error_message,
                pocket.row_count,
            ) == ("failed", "Ineligible: population mismatch", 3)

        # This is the persisted state written by the production definition
        # editor: changing the slice to B withdraws the old physical generation
        # and resets only the proof/build axes.  Keep the old row_count so the
        # next matcher call can prove it was not accidentally served.
        async with factory() as db:
            pocket = await db.get(PocketDefinition, ids.pocket)
            pocket.defining_sql = "SELECT * FROM g4b02model /* slice B */"
            pocket.query_fingerprint = "slice-b-fingerprint"
            pocket.predicate_set_hash = "slice-b-predicates"
            pocket.status = "stale"
            pocket.population_eligibility = "unknown"
            pocket.population_eligibility_reason = None
            pocket.population_proof_fingerprint = None
            pocket.built_for_version_id = None
            pocket.built_for_epoch = None
            pocket.row_manifest = None
            pocket.active_refresh_run_id = None
            await db.commit()

        # The old A table remains on disk, but the committed B definition is
        # stale and therefore cannot expose it: the real matcher refuses the
        # route, so no user-visible A rows can be returned.
        async with factory() as db:
            model = await db.get(Model, ids.model)
            result = await find_best_pocket(
                _bound(model, ids.amount, "slice-b-fingerprint"), db,
            )
            assert result.pocket is None
            assert result.skipped_reason == PocketSkipReason.NO_CANDIDATES
            await db.commit()
        async with factory() as db:
            pocket = await db.get(PocketDefinition, ids.pocket)
            assert (pocket.status, pocket.row_count) == ("stale", 3)

        # Restore population eligibility through the real graph state, then run
        # the production refresh producer.  Do not assign successful status,
        # table, fingerprint, or row-count fields here: the real CTAS,
        # finalisation guard, and row-manifest writer must make those writes.
        async with factory() as db:
            join = await db.get(Join, ids.join)
            join.join_type = "left"
            await db.commit()
        pocket_matcher.invalidate_model_join_graph_cache(ids.model)

        async with factory() as db:
            rebuilt_run = await refresh_pocket_definition(
                ids.pocket, db, triggered_by="g4-r2", refresh_mode="full",
            )
            assert (rebuilt_run.status, rebuilt_run.rows_written) == (
                "completed", 7,
            )

        # Execute the matched B pocket through the production rewrite and
        # dispatcher.  This is a user-visible known-value assertion and fails
        # if either seam returns the source/A generation instead.
        b_result, b_rows, b_columns, b_sql = await _execute_pocket_values(
            factory, ids, "slice-b-fingerprint",
        )
        assert (
            b_result.pocket.id,
            b_result.pocket.physical_table_name,
            b_result.pocket.row_count,
        ) == (ids.pocket, "slice_a", 7)
        assert b_columns == ["amount"]
        assert [int(row["amount"]) for row in b_rows] == [
            100, 200, 300, 400, 500, 600, 700,
        ]
        assert f'"{_target_schema(schema)}"."slice_a"' in b_sql

        async with factory() as db:
            pocket = await db.get(PocketDefinition, ids.pocket)
            assert (
                pocket.status, pocket.population_eligibility,
                pocket.population_proof_fingerprint,
                pocket.row_count, pocket.active_refresh_run_id,
            ) == ("fresh", "eligible", "slice-b-fingerprint", 7, rebuilt_run.id)
            assert pocket.row_manifest["build_refresh_run_id"] == str(rebuilt_run.id)


async def _true_setting():
    return True
