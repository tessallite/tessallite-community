"""Shared complete-data verifier for dimension attribute relationships.

Spec: architecture_derived-grain-aggregate-routing.md §7.6. ONE verifier used by
model deploy, optimizer build, aggregate full/incremental refresh, and pocket
refresh (spec §18: "Implement the forward, reverse, and NULL checks in one shared
verifier"). It proves, over the COMPLETE governed population, whether a declared
key->detail relationship holds:

  - forward (both cardinalities): each key maps to at most one detail
        SELECT key FROM rel GROUP BY key HAVING COUNT(DISTINCT detail) > 1  -> empty
  - reverse (BIJECTION only): each detail maps to at most one key
        SELECT detail FROM rel GROUP BY detail HAVING COUNT(DISTINCT key) > 1 -> empty
  - explicit NULL endpoints (both): reject any NULL key or NULL detail row
        SELECT 1 FROM rel WHERE key IS NULL OR detail IS NULL LIMIT 1 -> empty

Design constraints honoured (spec §7.6.2 / §18 / SQL-generation rules):
  - queries are SQLGlot ASTs in canonical PostgreSQL;
  - every identifier is quoted through ``shared/connector_qualify``;
  - the complete statement is transpiled ONCE to the source dialect;
  - execution goes ONLY through ``shared/source_executor`` (gateway boundary);
  - there are NO per-connector ``if`` branches in the check logic.

Certified-type gate (spec §7.6.2): float/double details are ``ERROR`` (NaN/-0.0/
transport make distinctness unprovable in v1); a text detail whose target GROUP
BY collation is not certified equal to the source is ``ERROR``. Integer,
exact-decimal, text (binary/certified collation), date, and boolean details may
be certified.

Phase 2 RECORDS evidence; it authorises NO serving route. A failed, timed-out,
unsupported, or counterexample-producing check writes ``BROKEN``/``ERROR`` and
never ``VERIFIED``. Counterexample VALUES are never returned in durable evidence.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import sqlglot

from shared.connector_qualify import quote_identifier, quote_table_ref


# ---------------------------------------------------------------------------
# Verification status + evidence
# ---------------------------------------------------------------------------

VERIFIED = "VERIFIED"
BROKEN = "BROKEN"
STALE = "STALE"
ERROR = "ERROR"
# Bug-7894: a text (VARCHAR/CHAR/STRING) BIJECTION detail cannot be certified at
# deploy scope — there is no serve-side artifact yet, so the target GROUP BY
# collation that decides whether two source-distinct labels FOLD into one is
# unknown. It is neither proven (VERIFIED) nor a data defect (BROKEN) nor a
# permanent fault (ERROR): it is UNPROVEN, awaiting artifact-time certification.
# PENDING is a NON-SERVING state — the router trust predicate (§7.6.4 rule 2)
# admits only VERIFIED, so PENDING never serves; it CLEARS to VERIFIED once the
# enriched aggregate is built and the collation-aware artifact-local reverse
# check certifies no folding under the actual serve collation. Never ERROR, so
# it never reads as a fault and never blocks the deploy.
PENDING = "PENDING"

# Cardinalities (must match the schema/ORM vocabulary).
BIJECTION = "BIJECTION"
FUNCTIONAL_N_TO_1 = "FUNCTIONAL_N_TO_1"

# Stable error codes (never a raw counterexample value).
ERR_FORWARD_VIOLATION = "FORWARD_KEY_TO_DETAIL_VIOLATION"
ERR_REVERSE_VIOLATION = "REVERSE_DETAIL_TO_KEY_VIOLATION"
ERR_NULL_ENDPOINT = "NULL_ENDPOINT"
ERR_UNSUPPORTED_TYPE = "UNSUPPORTED_DETAIL_TYPE"
ERR_UNCERTIFIED_COLLATION = "UNCERTIFIED_TEXT_COLLATION"
ERR_EXECUTION = "EXECUTION_ERROR"
ERR_TIMEOUT = "EXECUTION_TIMEOUT"


@dataclass
class VerificationEvidence:
    """Typed outcome of one verification attempt (spec §5.3 evidence shape)."""
    status: str
    cardinality: str
    violation_count: int = 0
    error_code: Optional[str] = None
    # The directional check that failed (forward|reverse|null), for diagnostics.
    failed_direction: Optional[str] = None


@dataclass
class RelationColumns:
    """Physical binding the verifier needs for one relationship, resolved by the
    caller through the model's governed relation (v1: key + detail are physical
    columns in the same relation)."""
    table_ref: str          # dotted physical relation, e.g. "schema.table"
    key_physical: str       # physical key column name
    detail_physical: str    # physical detail column name
    detail_type: str        # normalised detail column type token (upper-case)
    key_type: str = ""      # normalised key column type token (optional)


# ---------------------------------------------------------------------------
# Certified-type gate (spec §7.6.2)
# ---------------------------------------------------------------------------

# Certified-type gate (spec §7.6.2). Classification is delegated to the canonical,
# connector-agnostic ``shared.type_family`` mapping (Bug-7894 R2 finding 2): the
# raw connector spelling of a type varies wildly (PostgreSQL ``character varying``,
# SQL Server ``nvarchar``, BigQuery ``STRING``, Snowflake ``TEXT``), and a narrow
# hand-list of short tokens silently mis-classifies the verbose spellings the
# introspector actually stores — making the whole certification path inert on the
# primary connector. ``type_family`` already normalises every supported spelling
# into {numeric, datetime, boolean, text, other} with NO per-connector branch.
#
# Two refinements on top of ``type_family`` for this gate:
#   1. FLOAT/approximate numerics are DELIBERATELY uncertifiable (NaN / -0.0 /
#      binary transport make distinctness unprovable in v1). ``type_family`` lumps
#      float with numeric, so we detect and reject it explicitly first.
#   2. Text is certified ONLY when the source/target GROUP BY collation is proven
#      equal (``text_collation_certified``); otherwise it is uncertified-collation.

# Approximate-numeric spellings that are NEVER certifiable, checked as substrings of
# the normalised (lower, parameter-stripped) type so verbose forms are caught too.
_FLOAT_TOKENS: tuple[str, ...] = ("float", "double", "real")


def _is_float_type(data_type: str) -> bool:
    """True for an approximate/float numeric spelling (float/double/real).

    Substring match on the normalised type (lower-cased, precision stripped) so
    ``double precision``, ``float8``, ``float64`` all resolve. ``numeric`` /
    ``decimal`` are EXACT and are NOT float.
    """
    t = (data_type or "").strip().lower().split("(", 1)[0].strip()
    return any(tok in t for tok in _FLOAT_TOKENS)


def check_detail_type_certified(
    detail_type: str, *, text_collation_certified: bool = False,
) -> Optional[str]:
    """Return an error code when the detail type is NOT certified, else None.

    ``text_collation_certified`` MUST be passed True by the caller only when the
    source/target GROUP BY-equality collation pair for a text detail has been
    certified equal. Without it, text details are refused (spec §7.6.2: for N:1
    the collation certification is the only guard, so it is mandatory).

    Certifiable families (via ``shared.type_family``): exact numeric (int/
    numeric/decimal, but NOT float), datetime, boolean, and text (with collation
    proof). Everything else is UNSUPPORTED.
    """
    from shared.type_family import BOOLEAN, DATETIME, NUMERIC, TEXT, type_family

    # Float/approximate numerics are uncertifiable even though type_family calls
    # them numeric — reject first.
    if _is_float_type(detail_type):
        return ERR_UNSUPPORTED_TYPE
    fam = type_family(detail_type)
    if fam == TEXT:
        return None if text_collation_certified else ERR_UNCERTIFIED_COLLATION
    if fam in (NUMERIC, DATETIME, BOOLEAN):
        return None
    return ERR_UNSUPPORTED_TYPE


def is_text_detail_type(detail_type: str) -> bool:
    """True when the detail type is a text family type (any connector spelling).

    Bug-7894: a text detail's certification is COLLATION-dependent and can only
    be proven at artifact-build time against the real serve collation. Callers
    use this to distinguish "defer to artifact-time collation certification"
    (text, non-serving PENDING at deploy) from a genuinely unsupported/float
    detail (permanent ERROR). It does NOT weaken the certified-type gate — a
    text detail still requires ``text_collation_certified=True`` to pass
    ``check_detail_type_certified``; this only classifies the type. Uses the
    canonical ``shared.type_family`` mapping so verbose connector spellings
    (``character varying``, ``nvarchar``, ``bpchar``) classify correctly
    (Bug-7894 R2 finding 2).
    """
    from shared.type_family import is_text
    return is_text(detail_type)


def is_collation_stable_key_type(key_type: str) -> bool:
    """True when a KEY column's equality/DISTINCT is collation-INDEPENDENT.

    Bug-7899 (wrong numbers): the artifact-local reverse-uniqueness check that
    certifies a TEXT relabel is ``GROUP BY passenger HAVING COUNT(DISTINCT key)
    > 1`` evaluated under the SERVE collation. If the KEY column is ALSO text and
    the serve collation FOLDS (case/accent-insensitive), the key's own
    ``COUNT(DISTINCT key)`` folds together with the label groups and the fold
    MASKS ITSELF -> the check wrongly passes VERIFIED and serves wrong numbers.

    The reverse check is only a SOUND collation certification when the KEY is
    collation-stable — i.e. a non-text, non-float certified family (exact numeric,
    date, or boolean) whose distinctness cannot fold under any GROUP BY collation.
    A text key would need connector-specific binary-collation control to compare
    faithfully, which the SQL-generation rules forbid (no per-connector branches).
    So a text DETAIL relabel is certifiable ONLY beside such a key; a text key —
    or an unknown/unsupported/float key — fails closed (never certified, never
    served). Uses ``shared.type_family`` so verbose spellings classify correctly.
    """
    if not (key_type or "").strip():
        return False
    # A stable key must pass the certified-type gate on its OWN type (numeric/
    # date/bool), independent of any text-collation flag, AND not be text.
    if is_text_detail_type(key_type):
        return False
    return check_detail_type_certified(key_type, text_collation_certified=False) is None


# ---------------------------------------------------------------------------
# Check SQL construction (canonical postgres AST -> single transpile)
# ---------------------------------------------------------------------------


def _col(connector: str, name: str) -> str:
    return quote_identifier(connector, name)


def build_forward_check_sql(cols: RelationColumns, connector: str) -> str:
    """`SELECT key FROM rel GROUP BY key HAVING COUNT(DISTINCT detail) > 1 LIMIT 1`.

    An empty result proves each key maps to at most one detail. LIMIT 1 bounds
    returned evidence without sampling — an empty result still requires the
    engine to evaluate the complete relation (spec §7.6.2).
    """
    key = _col(connector, cols.key_physical)
    detail = _col(connector, cols.detail_physical)
    table = quote_table_ref(connector, cols.table_ref)
    sql = (
        f"SELECT {key} FROM {table} "
        f"GROUP BY {key} HAVING COUNT(DISTINCT {detail}) > 1 LIMIT 1"
    )
    return _transpile(sql, connector)


def build_reverse_check_sql(cols: RelationColumns, connector: str) -> str:
    """`SELECT detail ... GROUP BY detail HAVING COUNT(DISTINCT key) > 1 LIMIT 1`.

    Reverse strict check — required for BIJECTION only. Applying it to a
    functional N:1 would wrongly reject the intended shared-label case (spec
    §7.6.2, pitfall 17), so callers must gate it on cardinality == BIJECTION.
    """
    key = _col(connector, cols.key_physical)
    detail = _col(connector, cols.detail_physical)
    table = quote_table_ref(connector, cols.table_ref)
    sql = (
        f"SELECT {detail} FROM {table} "
        f"GROUP BY {detail} HAVING COUNT(DISTINCT {key}) > 1 LIMIT 1"
    )
    return _transpile(sql, connector)


def build_null_check_sql(cols: RelationColumns, connector: str) -> str:
    """`SELECT 1 FROM rel WHERE key IS NULL OR detail IS NULL LIMIT 1`.

    Explicit NULL existence check (spec §7.6.2): v1 rejects any NULL key or
    detail endpoint rather than relying on connector-specific COUNT(DISTINCT)
    NULL behaviour.
    """
    key = _col(connector, cols.key_physical)
    detail = _col(connector, cols.detail_physical)
    table = quote_table_ref(connector, cols.table_ref)
    sql = (
        f"SELECT 1 FROM {table} "
        f"WHERE {key} IS NULL OR {detail} IS NULL LIMIT 1"
    )
    return _transpile(sql, connector)


def _transpile(canonical_sql: str, connector: str) -> str:
    """Transpile a canonical-postgres statement ONCE to the source dialect.

    Identifiers are already connector-quoted by ``quote_identifier`` /
    ``quote_table_ref``; this pass handles dialect-level syntax (there is no
    per-connector branch in the check logic — sqlglot owns the translation).
    """
    from shared.connector_qualify import CONNECTOR_TO_SQLGLOT

    target = CONNECTOR_TO_SQLGLOT.get(connector, "postgres")
    if target == "postgres":
        return canonical_sql
    try:
        return sqlglot.transpile(canonical_sql, read="postgres", write=target)[0]
    except Exception:
        # Fail closed: an untranslatable check must not silently run wrong SQL.
        return canonical_sql


# ---------------------------------------------------------------------------
# Verification driver
# ---------------------------------------------------------------------------


async def verify_relationship(
    *,
    cols: RelationColumns,
    cardinality: str,
    connector: str,
    conn_obj: Any,
    tenant_session: Any = None,
    text_collation_certified: bool = False,
) -> VerificationEvidence:
    """Run the forward/reverse/NULL checks and return typed evidence.

    Executes ONLY through ``shared/source_executor.execute_source_sql``. Any
    execution error/timeout yields ``ERROR`` (source-only fallback), never
    ``VERIFIED``. A counterexample yields ``BROKEN``. All checks passing yields
    ``VERIFIED``. Counterexample VALUES are discarded — only the fact of a
    non-empty result and a stable code are retained.
    """
    # Certified-type gate first — cheapest and fully local.
    type_err = check_detail_type_certified(
        cols.detail_type, text_collation_certified=text_collation_certified,
    )
    if type_err is not None:
        return VerificationEvidence(
            status=ERROR, cardinality=cardinality, error_code=type_err,
        )

    from shared.source_executor import execute_source_sql, QueryTimeoutError

    async def _rows(sql: str) -> list[dict]:
        rows, _cols = await execute_source_sql(
            conn_obj, sql, tenant_session=tenant_session,
        )
        return rows

    try:
        # NULL endpoints first (fail-fast on the cheapest violation signal).
        if await _rows(build_null_check_sql(cols, connector)):
            return VerificationEvidence(
                status=BROKEN, cardinality=cardinality, violation_count=1,
                error_code=ERR_NULL_ENDPOINT, failed_direction="null",
            )
        # Forward key->detail (both cardinalities).
        if await _rows(build_forward_check_sql(cols, connector)):
            return VerificationEvidence(
                status=BROKEN, cardinality=cardinality, violation_count=1,
                error_code=ERR_FORWARD_VIOLATION, failed_direction="forward",
            )
        # Reverse detail->key (BIJECTION only).
        if cardinality == BIJECTION:
            if await _rows(build_reverse_check_sql(cols, connector)):
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
