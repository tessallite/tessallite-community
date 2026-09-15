"""Bug-9930 / F-R2-02 -- a row-security owner that is another fact must not
multiply the queried fact's rows, on real PostgreSQL.

The model has two measure-bearing relations, ``refunds`` (the model's one
``fact`` row -- ``uq_model_tables_one_fact_per_model`` caps a persisted model
at one) and ``payment_transaction`` (typed like any other non-fact table),
sharing the conformed ``dim_channel_code``.  The row-security rule is defined
on ``channel_code``, a dimension sourced from ``payment_transaction.
channel_code``; the owner list therefore ranks the payment relation first and
the channel dimension (whose ``channel`` dimension carries the same physical
column) second.  A refunds
query that joins the payment relation through the shared dimension repeats
every refund once per payment in the same channel -- the AST guards in
``tests/test_review_1_1_7_r1_rls_owner_binding.py`` pin the plan shape, this
file pins the NUMBER a principal is served, for a model whose joins declare
their cardinality and for one that relies on introspected primary keys alone:

* a principal the rule admits fully receives exactly the unrestricted refund
  total (the rule changes nothing, so the number must not change);
* a principal the rule restricts receives exactly the sum of the refunds in
  the admitted channel (the predicate still filters when bound to the
  dimension owner instead of the fact owner);
* neither served plan scans ``payment_transaction`` at all;
* the same holds when the gateway forces the raw route, as it does for every
  ungrouped JDBC query.

The payment fact carries THREE rows per channel so a fan-out is a x3, never a
coincidental match.

Requires a PostgreSQL URL::

    TESSALLITE_VERSIONING_DB_URL=postgresql+asyncpg://user:pw@host:5432/db \\
      python -m pytest tests/integration/test_bug_9930_two_fact_rls_owner_db.py

``scripts/run-like-ci.sh query-router`` provides it.

Test escape: every row-security integration test used a single-fact star, so
an owner join that multiplied the base could not be observed as a number.
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

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.real_snapshot_resolver,
    pytest.mark.real_population_resolvers,
]

_DB_URL = os.environ.get("TESSALLITE_VERSIONING_DB_URL") or os.environ.get(
    "TESSALLITE_IMPORTER_REHYDRATION_DB_URL"
)

MODEL_SLUG = "bug9930model"

_CHANNELS = [("WEB", "Web"), ("MOBILE", "Mobile"), ("BRANCH", "Branch")]
# Three payments per channel: a fan-out through the shared dimension is x3.
_PAYMENTS = {"WEB": [100, 110, 120], "MOBILE": [200, 210, 220], "BRANCH": [300, 310, 320]}
# Refunds exist in WEB and MOBILE only, all distinct amounts.
_REFUNDS = {"WEB": [Decimal("7.50"), Decimal("8.25")], "MOBILE": [Decimal("9.75")]}
_REFUND_TOTAL = sum((a for amounts in _REFUNDS.values() for a in amounts), Decimal(0))
_WEB_TOTAL = sum(_REFUNDS["WEB"], Decimal(0))

_TOTAL_SQL = f'SELECT SUM("refund_amount") AS "refund_amount" FROM "{MODEL_SLUG}"'
_BY_CHANNEL_SQL = (
    f'SELECT "channel_name", SUM("refund_amount") AS "refund_amount" '
    f'FROM "{MODEL_SLUG}" GROUP BY "channel_name"'
)
_CHANNEL_MEMBERS_SQL = (
    f'SELECT "channel_name" FROM "{MODEL_SLUG}" GROUP BY "channel_name"'
)


@asynccontextmanager
async def _isolated_schema():
    if not _DB_URL:
        pytest.skip("no versioning/importer Postgres URL configured")
    schema = f"bug9930_{uuid.uuid4().hex}"
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


async def _seed(factory, schema: str, *, declare_cardinality: bool):
    cardinality = "many_to_one" if declare_cardinality else None
    ids = SimpleNamespace(
        project=uuid.uuid4(), model=uuid.uuid4(), connection=uuid.uuid4(),
        source=uuid.uuid4(),
        payments=uuid.uuid4(), refunds=uuid.uuid4(), dim=uuid.uuid4(),
        c_pay_channel=uuid.uuid4(), c_pay_amount=uuid.uuid4(),
        c_ref_channel=uuid.uuid4(), c_ref_amount=uuid.uuid4(),
        c_dim_code=uuid.uuid4(), c_dim_name=uuid.uuid4(),
        rule_admit_both=uuid.uuid4(), rule_admit_web=uuid.uuid4(),
        version=uuid.uuid4(),
    )
    async with factory() as db:
        await db.execute(text(
            f'CREATE TABLE "{schema}"."payment_transaction" ('
            "channel_code text NOT NULL, amount numeric NOT NULL)"
        ))
        await db.execute(text(
            f'CREATE TABLE "{schema}"."refunds" ('
            "channel_code text NOT NULL, refund_amount numeric NOT NULL)"
        ))
        await db.execute(text(
            f'CREATE TABLE "{schema}"."dim_channel_code" ('
            "channel_code text PRIMARY KEY, channel_name text NOT NULL)"
        ))
        for code, name in _CHANNELS:
            await db.execute(
                text(f'INSERT INTO "{schema}"."dim_channel_code" VALUES (:c, :n)'),
                {"c": code, "n": name},
            )
        for code, amounts in _PAYMENTS.items():
            for amount in amounts:
                await db.execute(
                    text(f'INSERT INTO "{schema}"."payment_transaction" VALUES (:c, :a)'),
                    {"c": code, "a": amount},
                )
        for code, amounts in _REFUNDS.items():
            for amount in amounts:
                await db.execute(
                    text(f'INSERT INTO "{schema}"."refunds" VALUES (:c, :a)'),
                    {"c": code, "a": amount},
                )

        db.add(Project(id=ids.project, slug="bug9930-project", display_name="Bug 9930"))
        db.add(ProjectConnection(
            id=ids.connection, project_id=ids.project,
            display_name="same-db", connection_type="postgresql",
            encrypted_credentials=_real_encrypted_credentials(),
            config={"schema": schema},
        ))
        db.add(Model(
            id=ids.model, project_id=ids.project, slug=MODEL_SLUG,
            display_name="Bug 9930 model", seed="bug9930seed",
        ))
        db.add(DataSource(
            id=ids.source, model_id=ids.model,
            project_connection_id=ids.connection, source_type="postgresql",
            display_name="source", default_schema=schema, config={},
        ))
        await db.flush()
        db.add_all([
            ModelTable(
                id=ids.payments, model_id=ids.model, source_id=ids.source,
                table_type="dim_aggregate",
                physical_name=f"{schema}.payment_transaction",
                alias="payment_transaction", display_name="Payments",
            ),
            ModelTable(
                id=ids.refunds, model_id=ids.model, source_id=ids.source,
                table_type="fact", physical_name=f"{schema}.refunds",
                alias="refunds", display_name="Refunds",
            ),
            ModelTable(
                id=ids.dim, model_id=ids.model, source_id=ids.source,
                table_type="dim_aggregate",
                physical_name=f"{schema}.dim_channel_code",
                alias="dim_channel_code", display_name="Channel",
            ),
        ])
        await db.flush()
        db.add_all([
            ModelColumn(id=ids.c_pay_channel, model_table_id=ids.payments,
                        column_name="channel_code", data_type="text", is_nullable=False),
            ModelColumn(id=ids.c_pay_amount, model_table_id=ids.payments,
                        column_name="amount", data_type="numeric", is_nullable=False),
            ModelColumn(id=ids.c_ref_channel, model_table_id=ids.refunds,
                        column_name="channel_code", data_type="text", is_nullable=False),
            ModelColumn(id=ids.c_ref_amount, model_table_id=ids.refunds,
                        column_name="refund_amount", data_type="numeric", is_nullable=False),
            ModelColumn(id=ids.c_dim_code, model_table_id=ids.dim,
                        column_name="channel_code", data_type="text",
                        is_nullable=False, is_primary_key=True),
            ModelColumn(id=ids.c_dim_name, model_table_id=ids.dim,
                        column_name="channel_name", data_type="text", is_nullable=False),
        ])
        await db.flush()
        db.add_all([
            Join(
                model_id=ids.model,
                left_table_id=ids.payments, right_table_id=ids.dim,
                join_type="left", cardinality=cardinality,
                left_column_id=ids.c_pay_channel, right_column_id=ids.c_dim_code,
                population_participation="preserve_base_rows",
                population_participation_source="manual",
            ),
            Join(
                model_id=ids.model,
                left_table_id=ids.refunds, right_table_id=ids.dim,
                join_type="left", cardinality=cardinality,
                left_column_id=ids.c_ref_channel, right_column_id=ids.c_dim_code,
                population_participation="preserve_base_rows",
                population_participation_source="manual",
            ),
            # The rule's dimension: sourced from the PAYMENT relation's column.
            # The compiler names the physical column after the rule's path, so
            # the dimension carries the column's own name.
            Dimension(model_id=ids.model, name="channel_code",
                      source_column_id=ids.c_pay_channel),
            # The conformed dimension carries the same physical column under
            # another dimension name: the ordered fallback owner the resolver
            # must choose for a refunds query.
            Dimension(model_id=ids.model, name="channel",
                      source_column_id=ids.c_dim_code),
            Dimension(model_id=ids.model, name="channel_name",
                      source_column_id=ids.c_dim_name),
            Measure(model_id=ids.model, name="amount", source_column_id=ids.c_pay_amount,
                    measure_type="physical", data_type="numeric", default_agg="sum"),
            Measure(model_id=ids.model, name="refund_amount",
                    source_column_id=ids.c_ref_amount,
                    measure_type="physical", data_type="numeric", default_agg="sum"),
            RowSecurityRule(
                id=ids.rule_admit_both, model_id=ids.model,
                name="Bug-9930 web and mobile",
                dimension_path="channel_code", rule_type="role_predicate",
                predicate_expression="in('channel_code', 'WEB', 'MOBILE')",
                applies_to_roles=["digital"], is_enabled=True,
            ),
            RowSecurityRule(
                id=ids.rule_admit_web, model_id=ids.model,
                name="Bug-9930 web only",
                dimension_path="channel_code", rule_type="role_predicate",
                predicate_expression="in('channel_code', 'WEB')",
                applies_to_roles=["web"], is_enabled=True,
            ),
        ])
        await db.commit()

    async with factory() as db:
        snapshot = await snapshot_model(ids.model, db)
        db.add(ModelVersion(
            id=ids.version, model_id=ids.model, version_number=1,
            snapshot_json=snapshot, summary="bug-9930 fixture deploy",
            created_by="bug-9930-test",
        ))
        await db.flush()
        model = await db.get(Model, ids.model)
        model.deployed_version_id = ids.version
        model.deploy_epoch = 1
        await db.commit()
    return ids


def _scanned_relations(sql: str) -> set[str]:
    tree = sqlglot.parse_one(sql, read="postgres")
    return {(t.name or "").lower() for t in tree.find_all(exp.Table) if t.name}


async def _serve(factory, ids, sql: str, principal: Principal | None, *, force_route=None):
    """Route and execute ONE query exactly as the product does."""
    async with factory() as db:
        db.info["tenant_id"] = "bug9930-tenant"
        logical = parse_sql_to_ir(sql, str(ids.model))
        bound = await bind_query_to_model(logical, db, include_hidden=False)
        decision = await route_query(bound, db, principal=principal, force_route=force_route)
        connection = await db.get(ProjectConnection, ids.connection)
        rows, _bytes, _columns = await execute_on_connection(
            decision.rewritten_query, connection, db,
        )
        await db.commit()
        return decision, rows


def _num(value) -> Decimal:
    return Decimal(str(value))


_ADMIN = Principal(user_identity="admin@bug9930.test", roles=frozenset({"tenant_admin"}))
_DIGITAL = Principal(user_identity="digital@bug9930.test", roles=frozenset({"digital"}))
_WEB = Principal(user_identity="web@bug9930.test", roles=frozenset({"web"}))


_SEED_VARIANTS = pytest.mark.parametrize(
    "declare_cardinality", [True, False], ids=["declared-cardinality", "keys-only"],
)


@_SEED_VARIANTS
async def test_bug_9930_fully_admitted_principal_gets_the_unmultiplied_refund_total(
    declare_cardinality,
):
    """The rule admits every refund channel, so the governed total must equal
    the unrestricted total and the hand sum -- not three times it."""
    async with _isolated_schema() as (factory, schema):
        ids = await _seed(factory, schema, declare_cardinality=declare_cardinality)
        unrestricted, u_rows = await _serve(factory, ids, _TOTAL_SQL, _ADMIN)
        governed, g_rows = await _serve(factory, ids, _TOTAL_SQL, _DIGITAL)

    assert _num(u_rows[0]["refund_amount"]) == _REFUND_TOTAL
    assert _num(g_rows[0]["refund_amount"]) == _REFUND_TOTAL, (
        "the row-security owner join multiplied the refund total: "
        f"{g_rows[0]['refund_amount']} vs {_REFUND_TOTAL}\nSQL: {governed.rewritten_query}"
    )
    assert "payment_transaction" not in _scanned_relations(governed.rewritten_query), (
        governed.rewritten_query
    )
    assert "dim_channel_code" in _scanned_relations(governed.rewritten_query)


@_SEED_VARIANTS
async def test_bug_9930_restricted_principal_still_gets_only_the_admitted_channel(
    declare_cardinality,
):
    """Binding the predicate to the dimension owner must still filter: the
    web-only principal receives the WEB refunds, per channel and in total."""
    async with _isolated_schema() as (factory, schema):
        ids = await _seed(factory, schema, declare_cardinality=declare_cardinality)
        total, t_rows = await _serve(factory, ids, _TOTAL_SQL, _WEB)
        by_channel, c_rows = await _serve(factory, ids, _BY_CHANNEL_SQL, _WEB)

    assert _num(t_rows[0]["refund_amount"]) == _WEB_TOTAL, total.rewritten_query
    assert {r["channel_name"]: _num(r["refund_amount"]) for r in c_rows} == {
        "Web": _WEB_TOTAL,
    }, by_channel.rewritten_query
    assert "payment_transaction" not in _scanned_relations(by_channel.rewritten_query)


@_SEED_VARIANTS
async def test_bug_9978_dimension_members_start_at_the_security_owner_fact(
    declare_cardinality,
):
    """A measureless member query can start at the security owner fact.

    Its GROUP BY collapses repeated fact rows, so the owner-to-dimension join
    filters the visible member set without changing an aggregate value.
    """
    async with _isolated_schema() as (factory, schema):
        ids = await _seed(factory, schema, declare_cardinality=declare_cardinality)
        decision, rows = await _serve(
            factory,
            ids,
            _CHANNEL_MEMBERS_SQL,
            _WEB,
        )

    assert [row["channel_name"] for row in rows] == ["Web"]
    assert _scanned_relations(decision.rewritten_query) == {
        "payment_transaction",
        "dim_channel_code",
    }


@_SEED_VARIANTS
async def test_bug_9930_forced_raw_route_serves_the_same_unmultiplied_total(
    declare_cardinality,
):
    """The gateway forces ``raw`` for ungrouped JDBC queries; that route must
    keep refunds as its base and never scan the owner relation either. The
    raw route serves detail rows, so the proof is the ROW COUNT (a fan-out
    through three payments per channel would return nine rows, not three)
    and the sum of those rows."""
    async with _isolated_schema() as (factory, schema):
        ids = await _seed(factory, schema, declare_cardinality=declare_cardinality)
        decision, rows = await _serve(
            factory, ids, _TOTAL_SQL, _DIGITAL, force_route="raw",
        )

    expected_rows = sum(len(a) for a in _REFUNDS.values())
    assert len(rows) == expected_rows, (
        f"{len(rows)} detail rows served for {expected_rows} refunds: "
        f"{decision.rewritten_query}"
    )
    assert sum((_num(r["refund_amount"]) for r in rows), Decimal(0)) == _REFUND_TOTAL, (
        decision.rewritten_query
    )
    assert "payment_transaction" not in _scanned_relations(decision.rewritten_query), (
        decision.rewritten_query
    )
