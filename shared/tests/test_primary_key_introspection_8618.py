"""Source primary-key discovery stamping (Bug-8618).

``ModelColumn.is_primary_key`` had no producer: no connector's column
discovery read the source catalogue's key metadata, so the flag was only ever
true if a human ticked it. Every consumer that reasons from it — the pocket
row-population proof's non-duplication leg, the JDBC catalogue's index
metadata, the LookML export's ``primary_key`` — was reading a field that was
almost always False. Contract invariant 5 in
``docs/architecture/architecture_join-orientation-and-cardinality.md``.

The discovery itself is per-connector SQL that needs a live source, so it is
not unit-testable here. What IS unit-testable, and is the part a mistake would
silently corrupt, is the STAMPING contract at the single public boundary:

* a successful read writes an EXPLICIT True/False onto every column, so a
  consumer can act on "the source says this is not a key";
* a FAILED read writes nothing at all, so "we could not look" is never
  recorded as "there is no key" — ``sync_columns`` keys its
  leave-the-stored-value-alone branch on the field being absent, and a
  ``False`` written by a failed catalogue read would silently CLEAR a
  correctly-declared key on every re-sync.
"""
from __future__ import annotations

import asyncio
import sys
import types

import pytest

import shared.source_introspection as si

pytestmark = pytest.mark.unit


def _columns() -> list[dict]:
    return [{"column_name": "a"}, {"column_name": "b"}]


def _stamp(monkeypatch, reader, connector: str = "postgresql") -> list[dict]:
    monkeypatch.setitem(si._PK_COLUMNS_DISPATCH, connector, reader)
    return asyncio.run(
        si._stamp_primary_keys(
            _columns(), connector=connector, creds={}, config={},
            schema="s", table="t",
        )
    )


def test_a_successful_read_stamps_true_and_false_explicitly(monkeypatch):
    async def reader(creds, config, **kw):
        return {"a"}

    out = _stamp(monkeypatch, reader)
    assert out == [
        {"column_name": "a", "is_primary_key": True},
        {"column_name": "b", "is_primary_key": False},
    ]


def test_a_table_with_no_primary_key_is_stated_not_left_unknown(monkeypatch):
    async def reader(creds, config, **kw):
        return set()

    out = _stamp(monkeypatch, reader)
    assert all(c["is_primary_key"] is False for c in out), (
        "a source that genuinely has no primary key must be recorded as such, "
        "not left indistinguishable from an unread catalogue"
    )


def test_a_failed_read_omits_the_field_entirely(monkeypatch, caplog):
    """The load-bearing case: unknown must not be written as False.

    ``sync_columns`` reconciles the stored flag against this payload and only
    leaves it alone when the field is ABSENT. If a failed catalogue read wrote
    ``False``, every re-sync during a source outage would clear every declared
    primary key on the model — and the pocket population proof would then
    refuse acceleration it had previously proven, with no visible cause.
    """
    async def reader(creds, config, **kw):
        raise RuntimeError("catalogue unavailable")

    out = _stamp(monkeypatch, reader)
    assert out == [{"column_name": "a"}, {"column_name": "b"}]
    assert all("is_primary_key" not in c for c in out)


def test_an_unsupported_connector_omits_the_field(monkeypatch):
    """A connector with no PK reader must not be read as 'no keys exist'."""
    out = asyncio.run(
        si._stamp_primary_keys(
            _columns(), connector="unknown_connector", creds={}, config={},
            schema="s", table="t",
        )
    )
    assert all("is_primary_key" not in c for c in out)


def test_every_supported_connector_has_a_reader():
    """A connector present in column discovery but absent here would silently
    never populate the flag — the exact shape of the original defect."""
    assert (
        set(si._DISCOVER_COLUMNS_DISPATCH)
        == set(si._PK_COLUMNS_DISPATCH)
        == set(si._PROFILE_DISPATCH)
    ), (
        "a connector can discover or profile columns but not primary keys, so "
        "is_primary_key would stay unpopulated for it with no signal — "
        "profile_table is the second public boundary that stamps keys"
    )


def test_spark_reports_cannot_know_rather_than_no_keys():
    """Hive/Spark has no portable key catalogue.

    It is present in the dispatch — so the parity guard above stays meaningful
    — but returns ``None`` ("cannot know"), NOT an empty set. An empty set is a
    positive claim that the source declares no key, and ``sync_columns`` acts
    on that claim by CLEARING the stored flag: on a connector that cannot be
    asked at all, that would wipe a modeller's hand-declared keys on every
    schema re-sync and silently withdraw pocket acceleration the population
    proof had already granted.
    """
    assert asyncio.run(si._pk_columns_spark({}, {}, schema="s", table="t")) is None


def test_a_cannot_know_reader_omits_the_field(monkeypatch):
    """End of that chain: ``None`` must reach the payload as an ABSENT field,
    identically to a failed read — not as ``False``."""
    async def reader(creds, config, **kw):
        return None

    out = _stamp(monkeypatch, reader)
    assert out == [{"column_name": "a"}, {"column_name": "b"}]


def test_spark_end_to_end_never_stamps_a_key_verdict():
    """The real Spark reader, through the real stamping boundary."""
    out = asyncio.run(
        si._stamp_primary_keys(
            _columns(), connector="hadoop_spark", creds={}, config={},
            schema="s", table="t",
        )
    )
    assert all("is_primary_key" not in c for c in out), (
        "Spark cannot be asked for primary keys, so it must not answer"
    )


def test_bigquery_qualified_dataset_is_not_prefixed_twice(monkeypatch):
    captured: dict[str, object] = {}

    class _Client:
        def query(self, sql, *, job_config):
            captured["sql"] = sql
            captured["job_config"] = job_config
            return [{"column_name": "id"}]

        def close(self):
            captured["closed"] = True

    class _QueryJobConfig:
        def __init__(self, *, query_parameters):
            self.query_parameters = query_parameters

    fake_bigquery = types.ModuleType("google.cloud.bigquery")
    fake_bigquery.QueryJobConfig = _QueryJobConfig
    fake_bigquery.ScalarQueryParameter = lambda *args: args
    fake_cloud = types.ModuleType("google.cloud")
    fake_cloud.bigquery = fake_bigquery
    fake_google = types.ModuleType("google")
    fake_google.cloud = fake_cloud
    monkeypatch.setitem(sys.modules, "google", fake_google)
    monkeypatch.setitem(sys.modules, "google.cloud", fake_cloud)
    monkeypatch.setitem(sys.modules, "google.cloud.bigquery", fake_bigquery)
    monkeypatch.setattr(
        si, "_open_bq_client", lambda creds, config: (_Client(), "project-a")
    )

    result = asyncio.run(
        si._pk_columns_bq(
            {}, {}, schema="project-b.dataset", table="orders",
        )
    )

    sql = str(captured["sql"])
    assert result == {"id"}
    assert "`project-b`.`dataset`.`INFORMATION_SCHEMA`.`TABLE_CONSTRAINTS`" in sql
    assert "`project-a`.`project-b`.`dataset`" not in sql
    assert captured["closed"] is True
