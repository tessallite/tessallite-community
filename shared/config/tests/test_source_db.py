"""Tests for the source-DB endpoint helper.

The helper sits between the project_connection's stored credentials/config
and the resolver. Precedence is creds → config → system setting → registry default.

Post 2026-04 admin-config restructure, ``source_db.*`` fallback keys live
at the system level (surfaced=False) — so an admin-set system override
beats the registry default but creds/config still beat both.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptography.fernet import Fernet

from shared.config.resolver import clear_cache
from shared.config.source_db import (
    BigQueryProjectResolutionError,
    MissingConnectionHostError,
    resolve_aggregate_target_defaults,
    resolve_connection_bq_project,
    resolve_source_db_endpoint,
    resolve_spark_thrift_defaults,
    resolve_target_schema,
    target_connection_authority_is_provable,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    clear_cache()
    yield
    clear_cache()


def _session_returning(value):
    s = AsyncMock()
    r = MagicMock()
    r.scalar_one_or_none.return_value = value
    s.execute = AsyncMock(return_value=r)
    return s


@pytest.mark.asyncio
async def test_creds_win_over_everything():
    host, port, db = await resolve_source_db_endpoint(
        {"host": "explicit.example.com", "port": 9999, "database": "live"},
        {"host": "config.example.com"},
    )
    assert (host, port, db) == ("explicit.example.com", 9999, "live")


@pytest.mark.asyncio
async def test_config_fills_in_when_creds_missing():
    host, port, db = await resolve_source_db_endpoint(
        {},
        {"host": "config.example.com", "port": 5433, "database": "cfg"},
    )
    assert (host, port, db) == ("config.example.com", 5433, "cfg")


@pytest.mark.asyncio
async def test_falls_through_to_registry_default_when_nothing_set():
    """Design change (Bug-7172): a HOST that resolves to nothing but the
    registry's compiled-in default ("localhost") now fails loudly instead of
    silently. Before this fix, ``resolve_source_db_endpoint({}, {})`` (no
    creds, no config, no session) returned ``("localhost", 5432, "postgres")``
    — a connection string that looked valid and masked the real
    misconfiguration (no host anywhere). It now raises
    ``MissingConnectionHostError`` at config-resolution time, per CLAUDE.md's
    "fail clearly and early". Port/database keep the original silent-default
    behaviour (see ``test_port_and_database_still_fall_through_with_a_real_host``);
    only host is fail-closed, because a wrong default port/database is
    comparatively benign next to a wrong default HOST.
    """
    with pytest.raises(MissingConnectionHostError):
        await resolve_source_db_endpoint({}, {})


@pytest.mark.asyncio
async def test_missing_host_raises_error():
    """Bug-7172: a host that cannot be PROVEN — not merely a session that
    happens to be absent — must also raise. This is the production-relevant
    shape: every real call site opens a genuine system session
    (``_resolve_source_db_endpoint_scoped`` in shared/source_executor.py), so
    the meaningful guard is "no admin override row exists", not "no session
    was passed". A ``system_session`` that answers the fallback-host query
    with ``None`` (no row) must raise exactly like the no-session case.
    """
    system_session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    system_session.execute = AsyncMock(return_value=result)

    with pytest.raises(MissingConnectionHostError, match="No source DB host"):
        await resolve_source_db_endpoint({}, {}, system_session=system_session)


@pytest.mark.asyncio
async def test_missing_host_does_not_raise_when_creds_supply_one():
    """Sanity control: an explicit creds/config host never triggers the
    fail-closed path, even with no session at all."""
    host, port, db = await resolve_source_db_endpoint(
        {"host": "explicit.example.com"}, {}
    )
    assert host == "explicit.example.com"
    assert port == 5432
    assert db == "postgres"


@pytest.mark.asyncio
async def test_system_setting_overrides_registry_default():
    """System-stored values supersede the registry default for the relevant keys.

    Pre-restructure ``source_db.fallback_*`` lived at tenant level; after
    the 2026-04 restructure these are system-level (surfaced=False), so
    the override now flows through ``system_session``."""
    sequence = ["system-host.example.com", 6543, "systemdb"]

    async def fake_execute(*args, **kwargs):
        result = MagicMock()
        result.scalar_one_or_none.return_value = sequence.pop(0)
        return result

    system_session = AsyncMock()
    system_session.execute = AsyncMock(side_effect=fake_execute)

    host, port, db = await resolve_source_db_endpoint(
        {}, {}, system_session=system_session,
    )
    assert (host, port, db) == ("system-host.example.com", 6543, "systemdb")


@pytest.mark.asyncio
async def test_aggregate_target_defaults_returns_all_three():
    out = await resolve_aggregate_target_defaults()
    assert set(out.keys()) == {"schema", "dataset", "database"}
    assert out["schema"] == "aggregates"
    assert out["dataset"] == "default"
    assert out["database"] == "public"


@pytest.mark.asyncio
async def test_spark_thrift_defaults_returns_typed_values():
    out = await resolve_spark_thrift_defaults()
    assert out["port"] == 10000
    assert out["database"] == "default"
    assert out["auth_mode"] == "NOSASL"


# ---------------------------------------------------------------------------
# resolve_target_schema — direct, synchronous unit tests.
#
# These pin the per-connector fallback chain so the BigQuery behaviour change
# (default_dataset before default_schema) and the unchanged PG-family branches
# are both locked in against regression.
# ---------------------------------------------------------------------------

# A defaults dict shaped like resolve_aggregate_target_defaults() output, with
# distinct values per key so any cross-wiring between schema/dataset/database is
# caught by an assertion.
_DEFAULTS = {"schema": "agg_schema", "dataset": "agg_dataset", "database": "agg_db"}


def test_bigquery_config_dataset_wins():
    """BigQuery: config.dataset beats schema_override, config.schema, and all defaults."""
    ref = resolve_target_schema(
        "bigquery",
        {"dataset": "cfg_dataset", "schema": "cfg_schema", "project_id": "proj-1"},
        _DEFAULTS,
        schema_override="override_ds",
        connection_bq_project="proj-1",
    )
    assert ref.schema == "cfg_dataset"
    # Bug-8790: the connection project is authoritative, not the target config.
    assert ref.bq_project == "proj-1"
    assert ref.qualified_table("agg_sales") == "proj-1.cfg_dataset.agg_sales"


def test_bigquery_schema_override_only():
    """BigQuery: with no config.dataset, schema_override wins over config.schema/defaults."""
    ref = resolve_target_schema(
        "bigquery",
        {"schema": "cfg_schema"},
        _DEFAULTS,
        schema_override="override_ds",
    )
    assert ref.schema == "override_ds"
    assert ref.bq_project == ""
    assert ref.qualified_table("agg_sales") == "override_ds.agg_sales"


def test_bigquery_config_schema_used_when_no_dataset_or_override():
    """BigQuery: config.schema is used when neither config.dataset nor override is set."""
    ref = resolve_target_schema("bigquery", {"schema": "cfg_schema"}, _DEFAULTS)
    assert ref.schema == "cfg_schema"


def test_bigquery_last_resort_default_dataset_beats_default_schema():
    """BigQuery (the changed fallback): with NO config and NO override, the last-resort
    fallback is target_defaults['dataset'] BEFORE target_defaults['schema']."""
    ref = resolve_target_schema("bigquery", {}, _DEFAULTS)
    # default_dataset, not the PG-shaped default_schema.
    assert ref.schema == "agg_dataset"
    assert ref.schema != _DEFAULTS["schema"]
    assert ref.bq_project == ""


def test_bigquery_default_dataset_falls_to_default_schema_when_dataset_empty():
    """BigQuery: if default_dataset is empty, the chain still reaches default_schema."""
    ref = resolve_target_schema(
        "bigquery", {}, {"schema": "agg_schema", "dataset": "", "database": "agg_db"}
    )
    assert ref.schema == "agg_schema"


def test_postgresql_config_schema_wins_then_default_database():
    """PostgreSQL (unchanged): config.schema wins; absent it, fallback is default_database."""
    # config.schema present -> used verbatim.
    ref = resolve_target_schema("postgresql", {"schema": "cfg_schema"}, _DEFAULTS)
    assert ref.schema == "cfg_schema"
    assert ref.bq_project == ""

    # no config.schema, no override -> falls back to target_defaults['database'].
    ref_fallback = resolve_target_schema("postgresql", {}, _DEFAULTS)
    assert ref_fallback.schema == "agg_db"


def test_postgresql_schema_override_wins():
    """PostgreSQL (unchanged): explicit schema_override beats config.schema."""
    ref = resolve_target_schema(
        "postgresql", {"schema": "cfg_schema"}, _DEFAULTS, schema_override="override_s"
    )
    assert ref.schema == "override_s"


def test_redshift_matches_postgresql_branch():
    """Redshift shares the PG branch: fallback is default_database."""
    assert resolve_target_schema("redshift", {}, _DEFAULTS).schema == "agg_db"
    assert (
        resolve_target_schema("redshift", {"schema": "cfg_schema"}, _DEFAULTS).schema
        == "cfg_schema"
    )


def test_snowflake_fallback_to_default_schema_then_public():
    """Snowflake: fallback is default_schema; when absent the hard default is PUBLIC."""
    # default_schema present -> used.
    assert resolve_target_schema("snowflake", {}, _DEFAULTS).schema == "agg_schema"
    # config.schema wins over the fallback.
    assert (
        resolve_target_schema("snowflake", {"schema": "SALES"}, _DEFAULTS).schema
        == "SALES"
    )
    # no default_schema -> hard default PUBLIC.
    assert (
        resolve_target_schema("snowflake", {}, {"database": "agg_db"}).schema == "PUBLIC"
    )


def test_sqlserver_fallback_is_dbo():
    """SQL Server: hard fallback is dbo; config.schema still wins."""
    assert resolve_target_schema("sqlserver", {}, _DEFAULTS).schema == "dbo"
    assert (
        resolve_target_schema("sqlserver", {"schema": "sales"}, _DEFAULTS).schema
        == "sales"
    )


def test_hadoop_spark_fallback_is_default_dataset():
    """hadoop_spark: fallback is default_dataset; config.schema still wins."""
    assert resolve_target_schema("hadoop_spark", {}, _DEFAULTS).schema == "agg_dataset"
    assert (
        resolve_target_schema("hadoop_spark", {"schema": "warehouse_x"}, _DEFAULTS).schema
        == "warehouse_x"
    )


def test_unsupported_connector_raises_value_error():
    """Unknown connector raises a clear ValueError naming the connector."""
    with pytest.raises(ValueError, match="unsupported connector 'mysql'"):
        resolve_target_schema("mysql", {}, _DEFAULTS)


# ---------------------------------------------------------------------------
# BigQuery connection project resolution. The target write API and every
# aggregate/pocket DDL caller use this single resolver.
# ---------------------------------------------------------------------------


def _connection(connection_type="bigquery", *, config=None, credentials=None):
    return SimpleNamespace(
        id="connection-under-test",
        connection_type=connection_type,
        config={} if config is None else config,
        encrypted_credentials=credentials,
    )


def _encrypt_credentials(monkeypatch, credentials):
    from shared.security import credential_crypto

    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY_PREVIOUS", "")
    credential_crypto._multifernet_cached.cache_clear()
    return credential_crypto.encrypt_json(credentials)


def test_bq_project_uses_explicit_config_without_reading_credentials():
    conn = _connection(config={"project_id": " config-project "}, credentials=b"bad")
    assert resolve_connection_bq_project(conn) == "config-project"


def test_bq_project_uses_encrypted_credential_project(monkeypatch):
    encrypted = _encrypt_credentials(monkeypatch, {"project_id": "credential-project"})
    assert resolve_connection_bq_project(_connection(credentials=encrypted)) == "credential-project"


def test_bq_project_uses_encrypted_service_account_project(monkeypatch):
    encrypted = _encrypt_credentials(
        monkeypatch,
        {"service_account_json": {"project_id": "service-account-project"}},
    )
    assert (
        resolve_connection_bq_project(_connection(credentials=encrypted))
        == "service-account-project"
    )


def test_bq_project_allows_only_a_genuine_adc_empty_connection():
    assert resolve_connection_bq_project(_connection(config={}, credentials=None)) is None


def test_non_bigquery_connection_never_reads_its_config_or_credentials():
    assert (
        resolve_connection_bq_project(
            _connection("postgresql", config="malformed", credentials=b"bad")
        )
        is None
    )


def test_bq_project_rejects_unreadable_encrypted_credentials():
    with pytest.raises(BigQueryProjectResolutionError, match="credentials could not be read"):
        resolve_connection_bq_project(_connection(credentials=b"not-a-fernet-token"))


def test_bq_target_qualification_uses_the_connection_project_only():
    ref = resolve_target_schema(
        "bigquery",
        {"dataset": "aggregate_dataset", "project_id": "ignored-target-project"},
        _DEFAULTS,
        connection_bq_project="connection-project",
    )
    assert ref.qualified_table("daily_sales") == "connection-project.aggregate_dataset.daily_sales"


@pytest.mark.parametrize(
    ("target_type", "config", "expected"),
    [
        ("bigquery", {"dataset": "analytics"}, True),
        ("bigquery", {"dataset": "analytics", "project_id": "connection-project"}, True),
        ("bigquery", {"dataset": "other.analytics"}, False),
        ("bigquery", {"dataset": "analytics", "project_id": "other-project"}, False),
        ("postgresql", {"dataset": "analytics"}, False),
    ],
)
def test_target_connection_authority_is_provable_for_bigquery_legacy_state(
    target_type, config, expected
):
    target = SimpleNamespace(target_type=target_type, config=config)
    conn = _connection(config={"project_id": "connection-project"})
    assert target_connection_authority_is_provable(target, conn) is expected


def test_target_connection_authority_refuses_adc_and_non_bigquery_legacy_mismatch():
    target = SimpleNamespace(target_type="bigquery", config={"dataset": "analytics"})
    assert target_connection_authority_is_provable(target, _connection(config={})) is False
    assert target_connection_authority_is_provable(
        SimpleNamespace(target_type="bigquery", config={}),
        _connection("postgresql", config={}),
    ) is False
