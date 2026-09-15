"""Bug-9837 -- a subtotal grain may not have a narrower relation set than the
detail query it aggregates, on real PostgreSQL.

The reported failure: an Excel pivot of Account Type x Channel Name succeeded at
detail grain, but the ``Account Type x All`` grain drops ``channel_name`` from
the projection and the GROUP BY.  The source planner then built a FROM clause
without the channel relation -- which is the relation that OWNS the active
row-security column -- so the injector could not bind the predicate to a proven
scan and refused the grain (``security_column_owner_not_scanned``).  Under SQL
rule 4 a subtotal grain is a derived query layered ON TOP of the persona-visible
model query; it cannot see a narrower set of relations than that query.

Why this file and not the unit suite.  ``tests/test_bug_9837_rls_owner_join.py``
asserts the rendered AST carries the owner relation, against fabricated model
rows.  That proves the shape but not the OUTCOME: an AST assertion still passes
if the predicate lands somewhere that does not actually remove rows, or if the
grain sums a different population from the detail rows beneath it.  This test
therefore executes every grain of the real lattice against a real database and
asserts the numbers a restricted principal is served:

* relation set -- every grain, including the ones that project no channel
  column at all, scans the RLS owner relation;
* values -- each subtotal equals the sum of the SAME principal's own detail
  rows (an arithmetic identity, not a spot check);
* restriction -- the restricted principal's grand total is strictly smaller
  than the unrestricted one, and the excluded members are absent.  Without this
  last assertion the whole file would still pass with the persona's filter
  silently missing.

The security column (``channel_tier``) exists ONLY on the channel dimension.  A
planner that drops that relation cannot fall back to a same-named fact column,
so the defect cannot hide behind a coincidence.

Requires a PostgreSQL URL::

    TESSALLITE_VERSIONING_DB_URL=postgresql+asyncpg://user:pw@host:5432/db \
      python -m pytest tests/integration/test_bug_9837_subtotal_rls_owner_db.py

``scripts/run-like-ci.sh query-router`` provides it.  SQLite cannot stand in:
the point is a real join plan executed by a real engine.

Test escape: no test executed a rolled-up grain for a row-secured principal
against a database, so a grain that silently dropped the owner relation (or
kept it without constraining rows) had no failing assertion anywhere.
Guard: this file.  Tier: T3 (row-level security and wrong numbers).
"""
from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest
import sqlglot
from sqlglot import exp
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from shared.db.models import (
    DataSource,
    Dimension,
    Join,
    Measure,
    Model,
    ModelColumn,
    ModelTable,
    ModelVersion,
    Project,
    ProjectConnection,
    RowSecurityRule,
    TenantBase,
)
from shared.model_snapshot.serialiser import snapshot_model
from shared.security.credential_crypto import encrypt_json
from shared.security.predicate_compiler import Principal

from src.execution.dispatcher import execute_on_connection
from src.parsing.sql_parser import parse_sql_to_ir
from src.routing.router import route_query
from src.semantic.binder import bind_query_to_model

# This file drives the REAL binder, router, rewriter and dispatcher against a
# real model and a real database, so it opts out of the two conftest
# conveniences that stand in for those resolvers: the always-succeeds empty
# DeployedShape and the synthetic one-relation population world.  With either in
# place the star below would collapse to a single relation and the defect under
# test could not exist.
pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.real_snapshot_resolver,
    pytest.mark.real_population_resolvers,
]

_DB_URL = os.environ.get("TESSALLITE_VERSIONING_DB_URL") or os.environ.get(
    "TESSALLITE_IMPORTER_REHYDRATION_DB_URL"
)

MODEL_SLUG = "bug9837model"

# One fact row per (account type, channel) so every rollup grain has a
# hand-checkable sum.  DIGITAL/PHYSICAL is the row-security axis: a "member"
# principal sees the three DIGITAL channels and none of the two PHYSICAL ones.
_CHANNELS = [
    ("API", "API", "DIGITAL"),
    ("WEB", "Web", "DIGITAL"),
    ("MOBILE", "Mobile", "DIGITAL"),
    ("ATM", "ATM", "PHYSICAL"),
    ("BRANCH", "Branch", "PHYSICAL"),
]
_ACCOUNT_TYPES = [
    ("CRD", "Credit"),
    ("CUR", "Current"),
    ("SAV", "Savings"),
]
# transaction_count per (account_type_code, channel_code); deliberately all
# distinct so a mis-grouped rollup cannot coincidentally add up.
_FACTS = {
    ("CRD", "API"): 11, ("CRD", "WEB"): 12, ("CRD", "MOBILE"): 13,
    ("CRD", "ATM"): 14, ("CRD", "BRANCH"): 15,
    ("CUR", "API"): 21, ("CUR", "WEB"): 22, ("CUR", "MOBILE"): 23,
    ("CUR", "ATM"): 24, ("CUR", "BRANCH"): 25,
    ("SAV", "API"): 31, ("SAV", "WEB"): 32, ("SAV", "MOBILE"): 33,
    ("SAV", "ATM"): 34, ("SAV", "BRANCH"): 35,
}
_DIGITAL_CODES = {code for code, _name, tier in _CHANNELS if tier == "DIGITAL"}

# The four grains an ``Account Type x Channel Name`` pivot asks for.  The SQL
# shape mirrors the gateway's producer, ``dax/subtotal_engine.py::
# _build_grain_sql``: ``SELECT <grain dims>, AGG(<measure>) FROM <model slug>
# GROUP BY <grain dims>`` against the MODEL relation, with the grand total
# carrying no dimension at all.  Only the grain dimensions change between them,
# which is exactly the variable the defect turned on.
_GRAINS: list[tuple[str, tuple[str, ...], str]] = [
    (
        "detail",
        ("account_type_name", "channel_name"),
        f'SELECT "account_type_name", "channel_name", '
        f'SUM("transaction_count") AS "transaction_count" '
        f'FROM "{MODEL_SLUG}" GROUP BY "account_type_name", "channel_name"',
    ),
    (
        "account_type x All channel",
        ("account_type_name",),
        f'SELECT "account_type_name", '
        f'SUM("transaction_count") AS "transaction_count" '
        f'FROM "{MODEL_SLUG}" GROUP BY "account_type_name"',
    ),
    (
        "All account_type x channel",
        ("channel_name",),
        f'SELECT "channel_name", '
        f'SUM("transaction_count") AS "transaction_count" '
        f'FROM "{MODEL_SLUG}" GROUP BY "channel_name"',
    ),
    (
        "Grand Total",
        (),
        f'SELECT SUM("transaction_count") AS "transaction_count" '
        f'FROM "{MODEL_SLUG}"',
    ),
]

_OWNER_RELATION = "dim_channel"


@asynccontextmanager
async def _isolated_schema():
    if not _DB_URL:
        pytest.skip("no versioning/importer Postgres URL configured")
    schema = f"bug9837_{uuid.uuid4().hex}"
    boot = create_async_engine(_DB_URL, future=True)
    async with boot.begin() as conn:
        await conn.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
        await conn.exec_driver_sql(f'SET search_path TO "{schema}"')
        await conn.run_sync(TenantBase.metadata.create_all)
    await boot.dispose()

    engine = create_async_engine(_DB_URL, future=True)

    @event.listens_for(engine.sync_engine, "checkout")
    def _set_search_path(dbapi_connection, _record, _proxy):  # noqa: ANN001
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
    parsed = urlparse((_DB_URL or "").replace("+asyncpg", ""))
    return encrypt_json({
        "host": parsed.hostname or "localhost",
        "port": parsed.port or 5432,
        "database": (parsed.path or "/").lstrip("/"),
        "user": parsed.username or "tessallite",
        "password": parsed.password or "",
    })


async def _seed(factory, schema: str):
    """Create the physical star, then the model graph that describes it."""
    ids = SimpleNamespace(
        project=uuid.uuid4(), model=uuid.uuid4(), connection=uuid.uuid4(),
        source=uuid.uuid4(),
        fact=uuid.uuid4(), dim_account=uuid.uuid4(), dim_channel=uuid.uuid4(),
        c_fact_account=uuid.uuid4(), c_fact_channel=uuid.uuid4(),
        c_fact_count=uuid.uuid4(),
        c_acc_code=uuid.uuid4(), c_acc_name=uuid.uuid4(),
        c_chan_code=uuid.uuid4(), c_chan_name=uuid.uuid4(),
        c_chan_tier=uuid.uuid4(),
        rls_rule=uuid.uuid4(), version=uuid.uuid4(),
    )
    async with factory() as db:
        await db.execute(text(
            f'CREATE TABLE "{schema}"."fact_payment" ('
            "account_type_code text NOT NULL, channel_code text NOT NULL, "
            "transaction_count integer NOT NULL)"
        ))
        await db.execute(text(
            f'CREATE TABLE "{schema}"."dim_account_type" ('
            "account_type_code text PRIMARY KEY, account_type_name text NOT NULL)"
        ))
        # ``channel_tier`` lives here and NOWHERE else: no fact column of that
        # name exists for a dropped join to accidentally bind to.
        await db.execute(text(
            f'CREATE TABLE "{schema}"."dim_channel" ('
            "channel_code text PRIMARY KEY, channel_name text NOT NULL, "
            "channel_tier text NOT NULL)"
        ))
        for code, name in _ACCOUNT_TYPES:
            await db.execute(
                text(
                    f'INSERT INTO "{schema}"."dim_account_type" VALUES '
                    "(:code, :name)"
                ),
                {"code": code, "name": name},
            )
        for code, name, tier in _CHANNELS:
            await db.execute(
                text(
                    f'INSERT INTO "{schema}"."dim_channel" VALUES '
                    "(:code, :name, :tier)"
                ),
                {"code": code, "name": name, "tier": tier},
            )
        for (acc, chan), count in _FACTS.items():
            await db.execute(
                text(
                    f'INSERT INTO "{schema}"."fact_payment" VALUES '
                    "(:acc, :chan, :count)"
                ),
                {"acc": acc, "chan": chan, "count": count},
            )

        db.add(Project(
            id=ids.project, slug="bug9837-project", display_name="Bug 9837",
        ))
        db.add(ProjectConnection(
            id=ids.connection, project_id=ids.project,
            display_name="same-db", connection_type="postgresql",
            encrypted_credentials=_real_encrypted_credentials(),
            config={"schema": schema},
        ))
        db.add(Model(
            id=ids.model, project_id=ids.project, slug=MODEL_SLUG,
            display_name="Bug 9837 model", seed="bug9837seed",
        ))
        db.add(DataSource(
            id=ids.source, model_id=ids.model,
            project_connection_id=ids.connection, source_type="postgresql",
            display_name="source", default_schema=schema, config={},
        ))
        await db.flush()
        db.add_all([
            ModelTable(
                id=ids.fact, model_id=ids.model, source_id=ids.source,
                table_type="fact", physical_name=f"{schema}.fact_payment",
                alias="fact_payment", display_name="Payments",
            ),
            ModelTable(
                id=ids.dim_account, model_id=ids.model, source_id=ids.source,
                table_type="dim_aggregate",
                physical_name=f"{schema}.dim_account_type",
                alias="dim_account_type", display_name="Account type",
            ),
            ModelTable(
                id=ids.dim_channel, model_id=ids.model, source_id=ids.source,
                table_type="dim_aggregate",
                physical_name=f"{schema}.{_OWNER_RELATION}",
                alias=_OWNER_RELATION, display_name="Channel",
            ),
        ])
        await db.flush()
        db.add_all([
            ModelColumn(
                id=ids.c_fact_account, model_table_id=ids.fact,
                column_name="account_type_code", data_type="text",
                is_nullable=False,
            ),
            ModelColumn(
                id=ids.c_fact_channel, model_table_id=ids.fact,
                column_name="channel_code", data_type="text",
                is_nullable=False,
            ),
            ModelColumn(
                id=ids.c_fact_count, model_table_id=ids.fact,
                column_name="transaction_count", data_type="integer",
                is_nullable=False,
            ),
            ModelColumn(
                id=ids.c_acc_code, model_table_id=ids.dim_account,
                column_name="account_type_code", data_type="text",
                is_nullable=False, is_primary_key=True,
            ),
            ModelColumn(
                id=ids.c_acc_name, model_table_id=ids.dim_account,
                column_name="account_type_name", data_type="text",
                is_nullable=False,
            ),
            ModelColumn(
                id=ids.c_chan_code, model_table_id=ids.dim_channel,
                column_name="channel_code", data_type="text",
                is_nullable=False, is_primary_key=True,
            ),
            ModelColumn(
                id=ids.c_chan_name, model_table_id=ids.dim_channel,
                column_name="channel_name", data_type="text",
                is_nullable=False,
            ),
            ModelColumn(
                id=ids.c_chan_tier, model_table_id=ids.dim_channel,
                column_name="channel_tier", data_type="text",
                is_nullable=False,
            ),
        ])
        await db.flush()
        db.add_all([
            Join(
                model_id=ids.model,
                left_table_id=ids.fact, right_table_id=ids.dim_account,
                join_type="left", cardinality="many_to_one",
                left_column_id=ids.c_fact_account,
                right_column_id=ids.c_acc_code,
                population_participation="preserve_base_rows",
                population_participation_source="manual",
            ),
            Join(
                model_id=ids.model,
                left_table_id=ids.fact, right_table_id=ids.dim_channel,
                join_type="left", cardinality="many_to_one",
                left_column_id=ids.c_fact_channel,
                right_column_id=ids.c_chan_code,
                population_participation="preserve_base_rows",
                population_participation_source="manual",
            ),
            Dimension(
                model_id=ids.model, name="account_type_name",
                source_column_id=ids.c_acc_name,
            ),
            Dimension(
                model_id=ids.model, name="channel_name",
                source_column_id=ids.c_chan_name,
            ),
            Dimension(
                model_id=ids.model, name="channel_tier",
                source_column_id=ids.c_chan_tier,
            ),
            Measure(
                model_id=ids.model, name="transaction_count",
                source_column_id=ids.c_fact_count,
                measure_type="physical", data_type="integer",
                default_agg="sum",
            ),
            RowSecurityRule(
                id=ids.rls_rule, model_id=ids.model,
                name="Bug-9837 digital-channel scope",
                dimension_path="channel_tier", rule_type="role_predicate",
                predicate_expression="in('channel_tier', 'DIGITAL')",
                applies_to_roles=["member"], is_enabled=True,
            ),
        ])
        await db.commit()

    # Deploy it. A deployed model has no live-ORM graph fallback (Bug-7981):
    # its physical graph comes from the pinned snapshot or the query is
    # blocked, so the guard must exercise the same snapshot authority the
    # product serves from. Build that snapshot with the production serialiser.
    async with factory() as db:
        snapshot = await snapshot_model(ids.model, db)
        version = ModelVersion(
            id=ids.version, model_id=ids.model, version_number=1,
            snapshot_json=snapshot, summary="bug-9837 fixture deploy",
            created_by="bug-9837-test",
        )
        db.add(version)
        await db.flush()
        model = await db.get(Model, ids.model)
        model.deployed_version_id = ids.version
        model.deploy_epoch = 1
        await db.commit()
    return ids


def _scanned_relations(sql: str) -> set[str]:
    """Physical relations the rendered SQL actually scans."""
    tree = sqlglot.parse_one(sql, read="postgres")
    return {(t.name or "").lower() for t in tree.find_all(exp.Table) if t.name}


async def _serve(factory, ids, grain_sql: str, principal: Principal | None):
    """Route and execute ONE grain exactly as the product does.

    Returns ``(rewritten_sql, rows)``.
    """
    async with factory() as db:
        # The pooled source executor refuses a connection without a canonical
        # tenant identity, exactly as it does in production.
        db.info["tenant_id"] = "bug9837-tenant"
        logical = parse_sql_to_ir(grain_sql, str(ids.model))
        bound = await bind_query_to_model(logical, db, include_hidden=False)
        decision = await route_query(bound, db, principal=principal)
        connection = await db.get(ProjectConnection, ids.connection)
        rows, _bytes, _columns = await execute_on_connection(
            decision.rewritten_query, connection, db,
        )
        await db.commit()
        return decision, rows


def _num(value) -> Decimal:
    return Decimal(str(value))


def _cells(grain_dims: tuple[str, ...], rows: list[dict]) -> dict[tuple, Decimal]:
    out: dict[tuple, Decimal] = {}
    for row in rows:
        key = tuple(row[d] for d in grain_dims)
        out[key] = _num(row["transaction_count"])
    return out


async def _serve_all_grains(factory, ids, principal):
    """Serve every grain of the pivot and return the rendered SQL and cells."""
    served: dict[str, dict] = {}
    for label, grain_dims, grain_sql in _GRAINS:
        decision, rows = await _serve(factory, ids, grain_sql, principal)
        served[label] = {
            "dims": grain_dims,
            "sql": decision.rewritten_query,
            "route": decision.route_type,
            "cells": _cells(grain_dims, rows),
        }
    return served


async def test_bug_9837_every_subtotal_grain_keeps_the_rls_owner_relation():
    """The rolled-up grains scan the owner relation the detail grain does."""
    async with _isolated_schema() as (factory, schema):
        ids = await _seed(factory, schema)
        member = Principal(
            user_identity="member@bug9837.test", roles=frozenset({"member"}),
        )
        served = await _serve_all_grains(factory, ids, member)

    for label, info in served.items():
        relations = _scanned_relations(info["sql"])
        assert _OWNER_RELATION in relations, (
            f"grain {label!r} lost the RLS owner relation "
            f"{_OWNER_RELATION!r}; it scans {sorted(relations)}.\n"
            f"SQL: {info['sql']}"
        )
        # The owner relation is joined for the PREDICATE, not for the result:
        # a rolled-up grain must not start projecting or grouping a channel
        # column the client did not ask for.
        if "channel_name" not in info["dims"]:
            tree = sqlglot.parse_one(info["sql"], read="postgres")
            projected = {
                (a.alias_or_name or "").lower()
                for a in tree.expressions
            }
            assert "channel_name" not in projected, (
                f"grain {label!r} projected channel_name it was not asked for: "
                f"{info['sql']}"
            )
        assert "channel_tier" in info["sql"], (
            f"grain {label!r} carries no row-security predicate at all: "
            f"{info['sql']}"
        )


async def test_bug_9837_every_subtotal_equals_the_persona_own_detail_sum():
    """Each rolled-up cell is the sum of the SAME principal's detail cells."""
    async with _isolated_schema() as (factory, schema):
        ids = await _seed(factory, schema)
        member = Principal(
            user_identity="member@bug9837.test", roles=frozenset({"member"}),
        )
        served = await _serve_all_grains(factory, ids, member)

    detail = served["detail"]["cells"]
    account_names = {name for _code, name in _ACCOUNT_TYPES}
    digital_names = {
        name for code, name, _tier in _CHANNELS if code in _DIGITAL_CODES
    }

    # The restriction itself: the detail grain holds the digital channels only.
    assert {chan for _acc, chan in detail} == digital_names
    assert {acc for acc, _chan in detail} == account_names

    account_grain = served["account_type x All channel"]["cells"]
    for account in sorted(account_names):
        want = sum(
            (detail.get((account, chan), Decimal(0)) for chan in digital_names),
            Decimal(0),
        )
        assert account_grain[(account,)] == want, (
            f"{account} x All channel = {account_grain[(account,)]}, but the "
            f"principal's own detail rows sum to {want}"
        )

    channel_grain = served["All account_type x channel"]["cells"]
    assert set(channel_grain) == {(name,) for name in digital_names}
    for channel in sorted(digital_names):
        want = sum(
            (detail.get((acc, channel), Decimal(0)) for acc in account_names),
            Decimal(0),
        )
        assert channel_grain[(channel,)] == want, (
            f"All account_type x {channel} = {channel_grain[(channel,)]}, but "
            f"the principal's own detail rows sum to {want}"
        )

    grand = served["Grand Total"]["cells"][()]
    assert grand == sum(detail.values(), Decimal(0))


async def test_bug_9837_restricted_totals_are_strictly_below_unrestricted():
    """A grain that lost the predicate would match the admin's numbers."""
    async with _isolated_schema() as (factory, schema):
        ids = await _seed(factory, schema)
        member = Principal(
            user_identity="member@bug9837.test", roles=frozenset({"member"}),
        )
        admin = Principal(
            user_identity="admin@bug9837.test",
            roles=frozenset({"tenant_admin"}),
        )
        restricted = await _serve_all_grains(factory, ids, member)
        unrestricted = await _serve_all_grains(factory, ids, admin)

    expected_all = sum(_FACTS.values())
    expected_digital = sum(
        count for (_acc, chan), count in _FACTS.items()
        if chan in _DIGITAL_CODES
    )
    assert expected_digital < expected_all  # the fixture is actually restrictive

    assert unrestricted["Grand Total"]["cells"][()] == Decimal(expected_all)
    assert restricted["Grand Total"]["cells"][()] == Decimal(expected_digital)

    # Every rolled-up grain, not only the grand total, must be narrower.
    account_names = {name for _code, name in _ACCOUNT_TYPES}
    r_account = restricted["account_type x All channel"]["cells"]
    u_account = unrestricted["account_type x All channel"]["cells"]
    for account in sorted(account_names):
        assert r_account[(account,)] < u_account[(account,)], (
            f"{account} x All channel is identical for a restricted and an "
            "unrestricted principal -- the grain is not row-secured"
        )

    # The excluded members are absent from the restricted channel grain, and
    # present for the unrestricted one.
    physical_names = {
        name for code, name, _tier in _CHANNELS if code not in _DIGITAL_CODES
    }
    r_channels = {k[0] for k in restricted["All account_type x channel"]["cells"]}
    u_channels = {
        k[0] for k in unrestricted["All account_type x channel"]["cells"]
    }
    assert not (r_channels & physical_names)
    assert physical_names <= u_channels
