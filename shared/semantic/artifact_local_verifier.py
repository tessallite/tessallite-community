"""Artifact-LOCAL re-verification of a carried attribute edge over BUILT rows.

Spec: architecture_derived-grain-aggregate-routing.md §7.6.3. This is the piece
Phase 2's source-side deploy/build check deliberately deferred: after an artifact
is materialised, the strict/forward relationship checks are re-run over the
COMPLETE built artifact's key/passenger pairs. This proves exactly what was built
— including a cross-database copy that the source-side check could not observe
(pitfall 18: check/build race).

The built artifact carries the detail as a typed passenger BESIDE its key
(§7.6.3): the checks group the passenger by the artifact grain key column, so
they run against the materialised table, not the source relation. For an
aggregate that grouped measures by the real grain key, ``COUNT(DISTINCT
passenger) > 1`` per key key can only happen if the build merged two source
details into one key row — an activation-blocking condition.

Same generation discipline as ``attribute_relationship_verifier.py``:
  - SQLGlot ASTs in canonical PostgreSQL;
  - every identifier quoted through ``shared/connector_qualify``;
  - single transpile to the TARGET dialect (the artifact lives on the target);
  - execution ONLY through ``shared/source_executor`` (gateway boundary);
  - NO per-connector ``if`` branches.

Fail-closed: a failed/timed-out/counterexample check yields BROKEN/ERROR and never
VERIFIED, so the caller never activates the edge on a doubtful result.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import sqlglot

from shared.connector_qualify import quote_identifier, quote_table_ref
from shared.semantic.attribute_relationship_verifier import (
    BIJECTION,
    BROKEN,
    ERROR,
    ERR_EXECUTION,
    ERR_FORWARD_VIOLATION,
    ERR_NULL_ENDPOINT,
    ERR_REVERSE_VIOLATION,
    ERR_TIMEOUT,
    VERIFIED,
    VerificationEvidence,
)


@dataclass
class ArtifactRelation:
    """Physical binding of the BUILT artifact table for one carried edge.

    ``table_ref`` is the materialised artifact table (schema.table on the
    target); ``key_grain_column`` and ``detail_passenger_column`` are its
    physical column names.

    ``detail_ndistinct_column`` / ``detail_nullcount_column`` are the per-grain-key
    forward-dependency diagnostics the build materialised beside the passenger
    (``COUNT(DISTINCT detail)`` and the NULL-endpoint count, computed on the SOURCE
    side of the SAME keyed statement before the passenger's ``MIN`` collapses
    them). They are OPTIONAL; when both are present ``verify_artifact_edge`` runs
    the diagnostic check that catches a forward violation / partial NULL the
    ``MIN``-collapsed passenger would otherwise hide (Bug-7898).
    """
    table_ref: str
    key_grain_column: str
    detail_passenger_column: str
    detail_ndistinct_column: Optional[str] = None
    detail_nullcount_column: Optional[str] = None


def _col(connector: str, name: str) -> str:
    return quote_identifier(connector, name)


def _transpile(canonical_sql: str, connector: str) -> str:
    """Transpile a canonical-postgres statement ONCE to the target dialect.

    Mirrors the verifier's transpile: identifiers are already connector-quoted;
    this handles dialect syntax with no per-connector branch. Fails closed to
    the canonical SQL (never silently runs a wrong dialect) on a transpile error.
    """
    from shared.connector_qualify import CONNECTOR_TO_SQLGLOT

    target = CONNECTOR_TO_SQLGLOT.get(connector, "postgres")
    if target == "postgres":
        return canonical_sql
    try:
        return sqlglot.transpile(canonical_sql, read="postgres", write=target)[0]
    except Exception:
        return canonical_sql


def build_artifact_forward_check_sql(rel: ArtifactRelation, connector: str) -> str:
    """`SELECT key FROM artifact GROUP BY key HAVING COUNT(DISTINCT passenger) > 1`.

    Empty result proves each built key row carries at most one distinct detail.
    """
    key = _col(connector, rel.key_grain_column)
    passenger = _col(connector, rel.detail_passenger_column)
    table = quote_table_ref(connector, rel.table_ref)
    sql = (
        f"SELECT {key} FROM {table} "
        f"GROUP BY {key} HAVING COUNT(DISTINCT {passenger}) > 1 LIMIT 1"
    )
    return _transpile(sql, connector)


def build_artifact_reverse_check_sql(rel: ArtifactRelation, connector: str) -> str:
    """`SELECT passenger ... GROUP BY passenger HAVING COUNT(DISTINCT key) > 1`.

    Reverse strict check over the BUILT rows — BIJECTION only. This is the check
    that catches a target collation folding two source-distinct labels together
    across different keys (spec §7.6.2 text-collation note): the fold is visible
    on the target artifact even though the source forward check could not see it.
    """
    key = _col(connector, rel.key_grain_column)
    passenger = _col(connector, rel.detail_passenger_column)
    table = quote_table_ref(connector, rel.table_ref)
    sql = (
        f"SELECT {passenger} FROM {table} "
        f"GROUP BY {passenger} HAVING COUNT(DISTINCT {key}) > 1 LIMIT 1"
    )
    return _transpile(sql, connector)


def build_artifact_null_check_sql(rel: ArtifactRelation, connector: str) -> str:
    """`SELECT 1 FROM artifact WHERE key IS NULL OR passenger IS NULL LIMIT 1`.

    Explicit NULL-endpoint check over the built rows: v1 rejects any NULL key or
    passenger, matching the source-side rule (spec §7.6.2, I16).
    """
    key = _col(connector, rel.key_grain_column)
    passenger = _col(connector, rel.detail_passenger_column)
    table = quote_table_ref(connector, rel.table_ref)
    sql = (
        f"SELECT 1 FROM {table} "
        f"WHERE {key} IS NULL OR {passenger} IS NULL LIMIT 1"
    )
    return _transpile(sql, connector)


def build_artifact_diagnostic_check_sql(rel: ArtifactRelation, connector: str) -> str:
    """`SELECT 1 ... WHERE ndistinct > 1 OR nullcount > 0 LIMIT 1`.

    Bug-7898: the passenger column is ``MIN(detail)`` per grain key, so on a
    single-key-grain artifact the forward check (GROUP BY key HAVING
    COUNT(DISTINCT passenger) > 1) is a tautology and the NULL check only fires
    when EVERY source detail for a key was NULL (``MIN`` skips NULLs). A key that
    mapped to two distinct source details, or one with a PARTIAL NULL detail,
    would be silently collapsed to a single non-NULL passenger and pass both
    checks -> serve wrong numbers.

    The build already carried the per-key forward-dependency diagnostics beside
    the passenger: ``COUNT(DISTINCT detail)`` (ndistinct) and the NULL-endpoint
    count (nullcount), both computed on the SOURCE side of the SAME keyed
    statement BEFORE the ``MIN`` collapse. A non-empty result here proves some
    grain key had >1 distinct detail or a NULL detail -> the edge is NOT a clean
    functional relabel and must not activate. Requires both diagnostic columns.
    """
    ndistinct = _col(connector, rel.detail_ndistinct_column)
    nullcount = _col(connector, rel.detail_nullcount_column)
    table = quote_table_ref(connector, rel.table_ref)
    sql = (
        f"SELECT 1 FROM {table} "
        f"WHERE {ndistinct} > 1 OR {nullcount} > 0 LIMIT 1"
    )
    return _transpile(sql, connector)


async def verify_artifact_edge(
    *,
    rel: ArtifactRelation,
    cardinality: str,
    connector: str,
    conn_obj: Any,
    tenant_session: Any = None,
) -> VerificationEvidence:
    """Run the artifact-local diagnostic/NULL/forward/reverse checks over the built rows.

    Returns typed evidence. VERIFIED only when every applicable check is empty.
    A counterexample -> BROKEN; an execution error/timeout -> ERROR. Executes
    ONLY through ``shared/source_executor.execute_source_sql`` against the target
    connection that holds the artifact (never a source-side lookup).
    """
    from shared.source_executor import execute_source_sql, QueryTimeoutError

    async def _rows(sql: str) -> list[dict]:
        rows, _cols = await execute_source_sql(
            conn_obj, sql, tenant_session=tenant_session,
        )
        return rows

    try:
        # Bug-7898: the forward-dependency diagnostic check runs FIRST and is the
        # authoritative forward + NULL guard for a MIN()-collapsed passenger. The
        # forward/null checks below are structurally blind on a single-key-grain
        # artifact (the passenger is one value per key), so a forward violation /
        # partial NULL is caught here or nowhere. Fail closed when the build did
        # not carry the diagnostics (cannot prove the relabel is clean).
        if rel.detail_ndistinct_column and rel.detail_nullcount_column:
            if await _rows(build_artifact_diagnostic_check_sql(rel, connector)):
                return VerificationEvidence(
                    status=BROKEN, cardinality=cardinality, violation_count=1,
                    error_code=ERR_FORWARD_VIOLATION, failed_direction="forward",
                )
        else:
            return VerificationEvidence(
                status=ERROR, cardinality=cardinality,
                error_code=ERR_EXECUTION, failed_direction="forward",
            )
        if await _rows(build_artifact_null_check_sql(rel, connector)):
            return VerificationEvidence(
                status=BROKEN, cardinality=cardinality, violation_count=1,
                error_code=ERR_NULL_ENDPOINT, failed_direction="null",
            )
        if await _rows(build_artifact_forward_check_sql(rel, connector)):
            return VerificationEvidence(
                status=BROKEN, cardinality=cardinality, violation_count=1,
                error_code=ERR_FORWARD_VIOLATION, failed_direction="forward",
            )
        if cardinality == BIJECTION:
            if await _rows(build_artifact_reverse_check_sql(rel, connector)):
                return VerificationEvidence(
                    status=BROKEN, cardinality=cardinality, violation_count=1,
                    error_code=ERR_REVERSE_VIOLATION, failed_direction="reverse",
                )
    except QueryTimeoutError:
        return VerificationEvidence(
            status=ERROR, cardinality=cardinality, error_code=ERR_TIMEOUT,
        )
    except Exception:
        return VerificationEvidence(
            status=ERROR, cardinality=cardinality, error_code=ERR_EXECUTION,
        )

    return VerificationEvidence(status=VERIFIED, cardinality=cardinality)
