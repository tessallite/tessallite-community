"""F-016-02: hierarchy health must detect broken-drill member conditions.

The metadata health checker validates the hierarchy DEFINITION; it never reads
member data, so it cannot see orphan children or many-to-many parentage. The
member-integrity probe runs bounded, audited SQL through the source-execution
boundary and flags those conditions with sampled offending keys.
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.api.hierarchy_member_integrity import probe_hierarchy_member_integrity

pytestmark = pytest.mark.unit


def _level(name, ordinal, key_attr_id):
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        name=name,
        ordinal=ordinal,
        key_attribute_id=key_attr_id,
        key_attribute_source="physical_column",
    )


def _col(col_id, table_id, name):
    return types.SimpleNamespace(
        id=col_id, model_table_id=table_id, column_name=name, data_type="text",
    )


TABLE_ID = uuid.uuid4()
PARENT_COL = uuid.uuid4()
CHILD_COL = uuid.uuid4()


def _make_db(levels):
    """DB returning the two-level hierarchy with both columns on one table."""
    db = MagicMock()

    lvl_res = MagicMock()
    lvl_res.scalars.return_value.all.return_value = levels
    db.execute = AsyncMock(return_value=lvl_res)

    table = types.SimpleNamespace(
        id=TABLE_ID, source_id=uuid.uuid4(),
        physical_name="public.dim_geo",
    )
    cols = {
        PARENT_COL: _col(PARENT_COL, TABLE_ID, "country"),
        CHILD_COL: _col(CHILD_COL, TABLE_ID, "city"),
    }
    source = types.SimpleNamespace(id=table.source_id, config={}, default_schema="public")

    async def _get(entity, entity_id):
        if entity_id in cols:
            return cols[entity_id]
        if entity_id == TABLE_ID:
            return table
        if entity_id == table.source_id:
            return source
        return None

    db.get = AsyncMock(side_effect=_get)
    return db


def _hier():
    return types.SimpleNamespace(id=uuid.uuid4())


CONN = types.SimpleNamespace(connection_type="postgresql", config={}, encrypted_credentials=None)


@pytest.mark.parametrize(
    "connector", ["postgresql", "sqlserver", "bigquery", "hadoop_spark", "snowflake", "redshift"],
)
def test_probe_sql_valid_on_every_connector(connector):
    """Bug-8294 [SQL rule 1]: probe SQL is transpiled per dialect, so the row
    limit is valid on EVERY connector — SQL Server must emit TOP, not the
    invalid LIMIT the hand-written builder produced."""
    from src.api.hierarchy_member_integrity import (
        _probe_child_multiple_parents_sql,
        _probe_orphan_children_sql,
    )
    mp = _probe_child_multiple_parents_sql(connector, "public.dim_geo", "city", "country")
    orphan = _probe_orphan_children_sql(connector, "public.dim_geo", "city", "country")
    for sql in (mp, orphan):
        if connector == "sqlserver":
            assert "TOP" in sql and "LIMIT" not in sql
        else:
            assert "LIMIT" in sql
    # Semantics preserved after transpile.
    assert "COUNT(DISTINCT" in mp.upper() or "COUNT_BIG(DISTINCT" in mp.upper()
    assert "IS NULL" in orphan.upper()


@pytest.mark.parametrize(
    "connector", ["bigquery", "hadoop_spark", "sqlserver", "postgresql"],
)
def test_probe_sql_handles_hyphenated_qualified_name(connector):
    """Bug-8294 follow-up (Fable re-gate): a hyphenated dotted table reference —
    e.g. a BigQuery GCP project id like 'tessallite-io' (a live target) — must
    NOT raise a sqlglot ParseError. Quoting each dotted part makes it parse as a
    quoted multi-part identifier and render per dialect."""
    from src.api.hierarchy_member_integrity import (
        _probe_child_multiple_parents_sql,
        _probe_orphan_children_sql,
    )
    # Must not raise, and must carry the hyphenated project id intact.
    mp = _probe_child_multiple_parents_sql(
        connector, "tessallite-io.demo_ds.dim_geo", "city", "country"
    )
    orphan = _probe_orphan_children_sql(
        connector, "tessallite-io.demo_ds.dim_geo", "city", "country"
    )
    assert "tessallite-io" in mp
    assert "tessallite-io" in orphan


@pytest.mark.asyncio
async def test_detects_many_to_many_parentage():
    levels = [_level("Country", 0, PARENT_COL), _level("City", 1, CHILD_COL)]
    db = _make_db(levels)

    async def _exec_sql(conn, sql, **kw):
        # multiple-parents probe returns an offending child; orphan probe empty.
        if "COUNT(DISTINCT" in sql:
            return ([{"child_key": "Springfield", "parent_count": 2}], ["child_key", "parent_count"])
        return ([], [])

    with (
        patch("src.api.hierarchy_member_integrity.resolve_source_connection",
              new=AsyncMock(return_value=CONN)),
        patch("src.api.hierarchy_member_integrity.execute_source_sql",
              new=AsyncMock(side_effect=_exec_sql)),
    ):
        result = await probe_hierarchy_member_integrity(
            db, _hier(), project_id=uuid.uuid4(),
        )
    issues = result.issues

    kinds = {i["issue_type"] for i in issues}
    assert "member_multiple_parents" in kinds
    mp = next(i for i in issues if i["issue_type"] == "member_multiple_parents")
    assert mp["severity"] == "error"
    assert "Springfield" in mp["detail"]["sample_keys"]
    # Bug-8510: FINDING a defect is still full coverage — the pair was read.
    assert (result.pairs_total, result.pairs_scanned) == (1, 1)
    assert result.fully_scanned is True


@pytest.mark.asyncio
async def test_detects_orphan_children():
    levels = [_level("Country", 0, PARENT_COL), _level("City", 1, CHILD_COL)]
    db = _make_db(levels)

    async def _exec_sql(conn, sql, **kw):
        # Orphan probe is the SELECT DISTINCT ... parent IS NULL query; the
        # multiple-parents probe uses COUNT(DISTINCT ...). Dispatch on the
        # aggregate to disambiguate (both contain "IS NULL" after transpile).
        if "COUNT(DISTINCT" in sql:
            return ([], [])
        return ([{"child_key": "Atlantis"}], ["child_key"])

    with (
        patch("src.api.hierarchy_member_integrity.resolve_source_connection",
              new=AsyncMock(return_value=CONN)),
        patch("src.api.hierarchy_member_integrity.execute_source_sql",
              new=AsyncMock(side_effect=_exec_sql)),
    ):
        result = await probe_hierarchy_member_integrity(
            db, _hier(), project_id=uuid.uuid4(),
        )
    issues = result.issues

    kinds = {i["issue_type"] for i in issues}
    assert "member_orphan_children" in kinds
    orphan = next(i for i in issues if i["issue_type"] == "member_orphan_children")
    assert orphan["severity"] == "error"
    assert "Atlantis" in orphan["detail"]["sample_keys"]


@pytest.mark.asyncio
async def test_clean_hierarchy_reports_no_member_issues():
    levels = [_level("Country", 0, PARENT_COL), _level("City", 1, CHILD_COL)]
    db = _make_db(levels)

    with (
        patch("src.api.hierarchy_member_integrity.resolve_source_connection",
              new=AsyncMock(return_value=CONN)),
        patch("src.api.hierarchy_member_integrity.execute_source_sql",
              new=AsyncMock(return_value=([], []))),
    ):
        result = await probe_hierarchy_member_integrity(
            db, _hier(), project_id=uuid.uuid4(),
        )
    issues = result.issues

    # No error issues (only possibly info notes); clean data => no member errors.
    assert not any(i["severity"] == "error" for i in issues)
    # Bug-8510: and this is the ONLY shape that may be reported as healthy.
    assert result.fully_scanned is True


@pytest.mark.asyncio
async def test_probe_build_failure_degrades_not_500():
    """Bug-8294 follow-up: a probe-SQL BUILD failure (e.g. an unparseable table
    reference) must degrade to a member_integrity_probe_failed warning, never
    propagate and 500 the health endpoint. The SQL is built inside the try."""
    import src.api.hierarchy_member_integrity as mod
    levels = [_level("Country", 0, PARENT_COL), _level("City", 1, CHILD_COL)]
    db = _make_db(levels)

    with (
        patch("src.api.hierarchy_member_integrity.resolve_source_connection",
              new=AsyncMock(return_value=CONN)),
        patch.object(mod, "_probe_child_multiple_parents_sql",
                     side_effect=ValueError("unparseable table ref")),
        patch.object(mod, "_probe_orphan_children_sql",
                     side_effect=ValueError("unparseable table ref")),
    ):
        # Must NOT raise.
        result = await mod.probe_hierarchy_member_integrity(
            db, _hier(), project_id=uuid.uuid4(),
        )
    issues = result.issues

    kinds = {i["issue_type"] for i in issues}
    assert "member_integrity_probe_failed" in kinds
    # No error-severity member finding (we could not scan).
    assert not any(i["issue_type"] == "member_multiple_parents" for i in issues)
    # Bug-8510: a failed probe is NOT coverage. Reporting this hierarchy as
    # member-checked would be a false-healthy verdict.
    assert (result.pairs_total, result.pairs_scanned) == (1, 0)
    assert result.fully_scanned is False


@pytest.mark.asyncio
async def test_uda_backed_level_is_reported_unprobed_not_healthy():
    # A UDA-backed child level cannot be probed; must be flagged unprobed, never
    # silently reported as member-clean.
    parent = _level("Country", 0, PARENT_COL)
    child = types.SimpleNamespace(
        id=uuid.uuid4(), name="Region", ordinal=1,
        key_attribute_id=uuid.uuid4(), key_attribute_source="user_defined_attribute",
    )
    db = _make_db([parent, child])

    with (
        patch("src.api.hierarchy_member_integrity.resolve_source_connection",
              new=AsyncMock(return_value=CONN)),
        patch("src.api.hierarchy_member_integrity.execute_source_sql",
              new=AsyncMock(return_value=([], []))),
    ):
        result = await probe_hierarchy_member_integrity(
            db, _hier(), project_id=uuid.uuid4(),
        )
    issues = result.issues

    kinds = {i["issue_type"] for i in issues}
    assert "member_integrity_unprobed" in kinds
    assert (result.pairs_total, result.pairs_scanned) == (1, 0)
    assert result.fully_scanned is False


@pytest.mark.asyncio
async def test_partial_coverage_is_not_reported_as_scanned():
    """Bug-8510: one scannable pair plus one unscannable pair is NOT coverage.

    Three levels -> two adjacent pairs. Country/City sit on one physical table
    and are read; City/Segment is UDA-backed and is skipped. The probe must
    report 1 of 2, so the endpoint cannot claim the hierarchy was checked.
    """
    parent = _level("Country", 0, PARENT_COL)
    child = _level("City", 1, CHILD_COL)
    grandchild = types.SimpleNamespace(
        id=uuid.uuid4(), name="Segment", ordinal=2,
        key_attribute_id=uuid.uuid4(), key_attribute_source="user_defined_attribute",
    )
    db = _make_db([parent, child, grandchild])

    with (
        patch("src.api.hierarchy_member_integrity.resolve_source_connection",
              new=AsyncMock(return_value=CONN)),
        patch("src.api.hierarchy_member_integrity.execute_source_sql",
              new=AsyncMock(return_value=([], []))),
    ):
        result = await probe_hierarchy_member_integrity(
            db, _hier(), project_id=uuid.uuid4(),
        )

    assert (result.pairs_total, result.pairs_scanned) == (2, 1)
    assert result.fully_scanned is False
    # Clean data on the scanned pair, so nothing but the skip note.
    assert {i["issue_type"] for i in result.issues} == {"member_integrity_unprobed"}


def test_miscounted_coverage_is_not_reported_as_scanned():
    """R1-1: the coverage mechanism must fail closed on its OWN bookkeeping bug.

    A count higher than the number of pairs can only mean the increment ran
    twice, which would otherwise let a genuinely skipped pair hide behind an
    over-counted one and restore the false-healthy verdict.
    """
    from src.api.hierarchy_member_integrity import MemberIntegrityProbeResult

    assert MemberIntegrityProbeResult(pairs_total=2, pairs_scanned=3).fully_scanned is False
    assert MemberIntegrityProbeResult(pairs_total=2, pairs_scanned=2).fully_scanned is True


@pytest.mark.asyncio
async def test_single_level_hierarchy_is_vacuously_covered():
    """A hierarchy with no adjacent pair has no member relationship to break,
    so 0-of-0 counts as scanned rather than as an unexplained coverage gap."""
    db = _make_db([_level("Country", 0, PARENT_COL)])

    result = await probe_hierarchy_member_integrity(
        db, _hier(), project_id=uuid.uuid4(),
    )

    assert result.issues == []
    assert (result.pairs_total, result.pairs_scanned) == (0, 0)
    assert result.fully_scanned is True
