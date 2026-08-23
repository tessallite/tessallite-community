"""Hierarchy member-integrity probes (F-016-02).

The metadata health checker (``hierarchy_health.py``) validates the hierarchy's
*definition* — level chains, column references, join reachability, calendar
bindings. It deliberately never reads member DATA, so it cannot see the
broken-drill conditions that damage an Excel/BI pivot. This module probes the
two drill-breaking member-relationship conditions between adjacent levels:

  1. child-without-parent (orphan)— a child key whose parent key is NULL, so it
                                     drills up into nothing (disappears from
                                     grouped totals);
  2. child-to-multiple-parents    — a child key that maps to more than one
                                     distinct parent key (many-to-many parentage),
                                     so the drill path is ambiguous (the member
                                     double-counts or splits).

(A same-key-conflicting-label "duplicate level key" check needs a per-level
display/label column, which the hierarchy level schema does not currently
carry; it is out of scope here and left as a future enhancement.)

This module adds bounded, audited member-integrity probes that run through the
sanctioned source-execution boundary (``shared.source_executor.execute_source_sql``)
— never a raw driver, never a gateway bypass. Every probe:

  - resolves adjacent level pairs to PHYSICAL columns on the SAME source table
    (the common denormalised drill shape; cross-table parent/child integrity is
    a future enhancement, reported as an ``unprobed`` note rather than a false
    "healthy");
  - qualifies identifiers through ``shared.connector_qualify``;
  - caps the scan with ``LIMIT`` so a huge dimension cannot produce a runaway
    statement or an unbounded result set;
  - returns sampled offending keys and counts so the caller can render an
    actionable diagnostic.

The probe is opt-in (the health endpoint runs it only when explicitly asked)
because it touches the live source; the default health call stays metadata-only
and cheap.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

import sqlglot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import (
    DataSource,
    HierarchyLevel,
    ModelColumn,
    ModelTable,
    ProjectConnection,
)
from shared.schemas.connection_type import normalize_connection_type
from shared.source_executor import execute_source_sql
from src.api._scope import resolve_source_connection
from src.api._table_qualify import qualify_physical_name

# Cap the number of offending keys sampled so a large dimension cannot produce
# a runaway result set. The GROUP BY / DISTINCT probes are aggregate-bounded on
# the source and further capped with LIMIT.
_SAMPLE_LIMIT = 20

# Bug-8294 [SQL rule 1]: connector -> sqlglot dialect token. Probe SQL is
# authored in canonical PostgreSQL and transpiled to the target dialect via
# sqlglot (never hand-written per-connector clauses), so the row-limit and
# identifier quoting are correct on every connector — notably ``LIMIT`` ->
# ``TOP`` on SQL Server, which the hand-written ``LIMIT`` broke.
_SQLGLOT_DIALECT = {
    "postgresql": "postgres",
    "redshift": "redshift",
    "bigquery": "bigquery",
    "hadoop_spark": "spark",
    "snowflake": "snowflake",
    "sqlserver": "tsql",
}


@dataclass(frozen=True)
class MemberIntegrityProbeResult:
    """Outcome of a member-integrity probe run for ONE hierarchy.

    Bug-8510: the issue list alone cannot answer "was member integrity actually
    checked?". Callers used to have to infer it by string-matching issue types,
    which fails OPEN — a future skip branch that forgets to emit the expected
    marker would silently be read as "covered". So the probe reports its own
    coverage bookkeeping instead: ``pairs_total`` adjacent level pairs exist and
    ``pairs_scanned`` of them were actually read from the source. A pair counts
    as scanned only on the path where BOTH probe statements executed, so any new
    early-``continue`` (unresolvable column, cross-table pair, dead connection)
    or any probe exception leaves it unscanned by construction — fail closed.
    """

    issues: list[dict[str, Any]] = field(default_factory=list)
    pairs_total: int = 0
    pairs_scanned: int = 0

    @property
    def fully_scanned(self) -> bool:
        """True when every adjacent level pair was actually read.

        A hierarchy with fewer than two levels has no parent/child pair, so
        member integrity is vacuously complete (0 of 0 scanned).

        Exact equality, not ``>=``: over-counting is a bookkeeping bug, and a
        bug in the mechanism that decides "may I show this as healthy" must
        resolve to NOT healthy. ``>=`` would let a future double-increment on
        the success path hide a genuinely skipped pair (2 counted of 2 total
        with only 1 read) — silently reintroducing the false-healthy verdict
        this class exists to prevent.
        """
        return self.pairs_scanned == self.pairs_total


def _canonical_ident(name: str) -> str:
    """Double-quote a single identifier for canonical PostgreSQL, escaping any
    embedded double-quote (defence-in-depth)."""
    return '"' + str(name).replace('"', '""') + '"'


def _canonical_table_ref(qualified_table: str) -> str:
    """Quote each dotted part of an UNQUOTED qualified table reference for
    canonical PostgreSQL.

    Bug-8294 follow-up (Fable re-gate): ``qualify_physical_name`` returns an
    unquoted dotted reference, and for BigQuery it prepends the GCP project id
    (e.g. ``tessallite-io.ds.sales``). Embedding that raw into a statement that
    ``sqlglot.parse_one`` then parses raises a ParseError on the hyphen. Quoting
    each part makes it parse as a quoted multi-part identifier, which sqlglot
    re-renders per dialect (backticks on BigQuery/Spark, brackets on T-SQL,
    double quotes elsewhere).
    """
    return ".".join(_canonical_ident(p) for p in qualified_table.split("."))


def _transpile(canonical_pg_sql: str, connector: str) -> str:
    """Transpile a canonical-PostgreSQL probe statement to the target dialect."""
    dialect = _SQLGLOT_DIALECT.get(connector, "postgres")
    return sqlglot.parse_one(canonical_pg_sql, read="postgres").sql(dialect=dialect)


async def _resolve_physical_column(
    db: AsyncSession, level: HierarchyLevel
) -> tuple[ModelColumn, ModelTable] | None:
    """Resolve a physical-column level to (ModelColumn, ModelTable).

    Returns None when the level is not backed by a physical column (e.g. a UDA
    level) or the column/table cannot be loaded — such levels are reported as
    ``unprobed`` rather than probed, so we never claim a UDA-backed level is
    member-clean when we did not read it.
    """
    if getattr(level, "key_attribute_source", None) != "physical_column":
        return None
    attr_id = getattr(level, "key_attribute_id", None)
    if not attr_id:
        return None
    col = await db.get(ModelColumn, attr_id)
    if col is None:
        return None
    table = await db.get(ModelTable, col.model_table_id)
    if table is None:
        return None
    return col, table


async def _connection_for_table(
    db: AsyncSession, table: ModelTable, *, project_id: UUID
) -> tuple[ProjectConnection, DataSource] | None:
    source = await db.get(DataSource, table.source_id)
    if source is None:
        return None
    try:
        connection = await resolve_source_connection(
            db, source, expected_project_id=project_id
        )
    except Exception:
        # A misconfigured/cross-project connection is a metadata problem the
        # definition health check already surfaces; do not crash the probe.
        return None
    return connection, source


def _probe_child_multiple_parents_sql(
    connector: str,
    qualified_table: str,
    child_col: str,
    parent_col: str,
) -> str:
    """SQL: child keys that map to more than one distinct parent key."""
    qt = _canonical_table_ref(qualified_table)
    qc = _canonical_ident(child_col)
    qp = _canonical_ident(parent_col)
    canonical = (
        f'SELECT {qc} AS child_key, '
        f'COUNT(DISTINCT {qp}) AS parent_count '
        f'FROM {qt} '
        f'WHERE {qc} IS NOT NULL '
        f'GROUP BY {qc} '
        f'HAVING COUNT(DISTINCT {qp}) > 1 '
        f'LIMIT {_SAMPLE_LIMIT}'
    )
    return _transpile(canonical, connector)


def _probe_orphan_children_sql(
    connector: str,
    qualified_table: str,
    child_col: str,
    parent_col: str,
) -> str:
    """SQL: distinct child keys whose parent key is NULL (orphans)."""
    qt = _canonical_table_ref(qualified_table)
    qc = _canonical_ident(child_col)
    qp = _canonical_ident(parent_col)
    canonical = (
        f'SELECT DISTINCT {qc} AS child_key '
        f'FROM {qt} '
        f'WHERE {qc} IS NOT NULL AND {qp} IS NULL '
        f'LIMIT {_SAMPLE_LIMIT}'
    )
    return _transpile(canonical, connector)


async def probe_hierarchy_member_integrity(
    db: AsyncSession,
    hier: Any,
    *,
    project_id: UUID,
    tenant_slug: str | None = None,
) -> MemberIntegrityProbeResult:
    """Run bounded, audited member-integrity probes for a hierarchy.

    Returns a :class:`MemberIntegrityProbeResult`: issue dicts using the same
    shape as the metadata health checker (``issue_type`` / ``severity`` /
    ``detail``), plus the coverage bookkeeping the caller needs to state
    authoritatively whether member integrity was checked (Bug-8510). An empty
    issue list means every SCANNED adjacent level pair passed — it does not on
    its own mean the hierarchy was fully scanned, which is why coverage is
    reported separately. Levels that could not be probed (non physical-column,
    unresolvable table/connection) also contribute an ``unprobed`` note so a
    human reader sees why.
    """
    issues: list[dict[str, Any]] = []

    levels_result = await db.execute(
        select(HierarchyLevel)
        .where(HierarchyLevel.hierarchy_id == hier.id)
        .order_by(HierarchyLevel.ordinal)
    )
    levels = list(levels_result.scalars().all())
    if len(levels) < 2:
        # No adjacent pair exists, so there is no member relationship to break.
        return MemberIntegrityProbeResult(issues=issues)

    pairs_total = len(levels) - 1
    pairs_scanned = 0

    # Resolve each level's physical column + table once.
    resolved: list[tuple[HierarchyLevel, ModelColumn, ModelTable] | None] = []
    for lvl in levels:
        r = await _resolve_physical_column(db, lvl)
        resolved.append((lvl, r[0], r[1]) if r else None)

    for idx in range(len(levels) - 1):
        parent = resolved[idx]
        child = resolved[idx + 1]
        parent_lvl = levels[idx]
        child_lvl = levels[idx + 1]

        if parent is None or child is None:
            issues.append({
                "issue_type": "member_integrity_unprobed",
                "severity": "info",
                "detail": {
                    "parent_level": parent_lvl.name,
                    "child_level": child_lvl.name,
                    "reason": (
                        "One or both levels are not backed by a physical "
                        "column on a source table (e.g. a user-defined "
                        "attribute), so member data was not scanned for this "
                        "parent/child pair."
                    ),
                },
            })
            continue

        _, parent_col, parent_tbl = parent
        _, child_col, child_tbl = child

        # Same-table (denormalised) drill only: cross-table parent/child
        # integrity needs a JOIN scan and is a future enhancement. Report it as
        # unprobed rather than claim health.
        if parent_tbl.id != child_tbl.id:
            issues.append({
                "issue_type": "member_integrity_unprobed",
                "severity": "info",
                "detail": {
                    "parent_level": parent_lvl.name,
                    "child_level": child_lvl.name,
                    "reason": (
                        "Parent and child levels resolve to different source "
                        "tables; cross-table member integrity is not scanned "
                        "yet."
                    ),
                },
            })
            continue

        conn_pair = await _connection_for_table(db, child_tbl, project_id=project_id)
        if conn_pair is None:
            issues.append({
                "issue_type": "member_integrity_unprobed",
                "severity": "info",
                "detail": {
                    "parent_level": parent_lvl.name,
                    "child_level": child_lvl.name,
                    "reason": "Source connection could not be resolved.",
                },
            })
            continue
        connection, source = conn_pair
        connector = normalize_connection_type(
            (connection.connection_type or "").lower()
        )
        qualified = qualify_physical_name(child_tbl.physical_name, connection, source)

        child_name = child_col.column_name
        parent_name = parent_col.column_name

        # (1) child-to-multiple-parents (many-to-many parentage). Build the SQL
        # INSIDE the try so a transpile/parse failure (e.g. an exotic table
        # reference) degrades to a probe_failed note instead of 500ing the
        # health endpoint (Fable re-gate: hyphenated BigQuery project ids).
        rows: list[dict[str, Any]] = []
        try:
            mp_sql = _probe_child_multiple_parents_sql(
                connector, qualified, child_name, parent_name
            )
            rows, _cols = await execute_source_sql(
                connection, mp_sql,
                tenant_session=db,
                purpose="hierarchy_member_integrity",
                tenant_slug=tenant_slug,
            )
            _mp_ok = True
        except Exception as exc:
            _mp_ok = False
            issues.append({
                "issue_type": "member_integrity_probe_failed",
                "severity": "warning",
                "detail": {
                    "parent_level": parent_lvl.name,
                    "child_level": child_lvl.name,
                    "check": "child_multiple_parents",
                    "error": type(exc).__name__,
                },
            })
        if _mp_ok and rows:
            issues.append({
                "issue_type": "member_multiple_parents",
                "severity": "error",
                "detail": {
                    "parent_level": parent_lvl.name,
                    "child_level": child_lvl.name,
                    "sample_keys": [
                        str(r.get("child_key")) for r in rows
                    ],
                    "sampled": len(rows),
                    "truncated": len(rows) >= _SAMPLE_LIMIT,
                    "reason": (
                        f"{len(rows)}+ child key(s) at level "
                        f"'{child_lvl.name}' map to more than one parent at "
                        f"'{parent_lvl.name}'. Drill-up is ambiguous; a pivot "
                        "will double-count or split these members."
                    ),
                },
            })

        # (2) child-without-parent (orphans). SQL built inside the try (same
        # 500-safety rationale as the multiple-parents probe above).
        _orphan_ok = False
        try:
            orphan_sql = _probe_orphan_children_sql(
                connector, qualified, child_name, parent_name
            )
            rows, _cols = await execute_source_sql(
                connection, orphan_sql,
                tenant_session=db,
                purpose="hierarchy_member_integrity",
                tenant_slug=tenant_slug,
            )
        except Exception as exc:
            issues.append({
                "issue_type": "member_integrity_probe_failed",
                "severity": "warning",
                "detail": {
                    "parent_level": parent_lvl.name,
                    "child_level": child_lvl.name,
                    "check": "orphan_children",
                    "error": type(exc).__name__,
                },
            })
        else:
            _orphan_ok = True
            if rows:
                issues.append({
                    "issue_type": "member_orphan_children",
                    "severity": "error",
                    "detail": {
                        "parent_level": parent_lvl.name,
                        "child_level": child_lvl.name,
                        "sample_keys": [
                            str(r.get("child_key")) for r in rows
                        ],
                        "sampled": len(rows),
                        "truncated": len(rows) >= _SAMPLE_LIMIT,
                        "reason": (
                            f"{len(rows)}+ child key(s) at level "
                            f"'{child_lvl.name}' have no parent value at "
                            f"'{parent_lvl.name}'. These members drill up into "
                            "nothing and disappear from grouped totals."
                        ),
                    },
                })

        # Bug-8510: this pair counts as scanned ONLY when both statements
        # actually ran against the source. Every other route out of the loop
        # body (unresolvable column, cross-table pair, dead connection, probe
        # exception) skips this line, so coverage fails closed by construction.
        if _mp_ok and _orphan_ok:
            pairs_scanned += 1

    return MemberIntegrityProbeResult(
        issues=issues, pairs_total=pairs_total, pairs_scanned=pairs_scanned,
    )
