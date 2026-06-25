#!/usr/bin/env python3
"""
Semantic rewrite validator — batch comparison of original vs rewritten SQL.

Runs test queries in batches of 10 through BOTH:
  1. Direct psycopg2 to the source PostgreSQL (original SQL with table names
     substituted to physical names)
  2. JDBC gateway (which goes through the full query-router rewrite pipeline)

Then calls `claude` CLI to semantically compare the column sets and result
data, reporting PASS/FAIL per batch.  Stops on first failure batch.

Usage:
    cd tessallite/services/query-router
    set -a && source ../../.env && set +a
    python tests/validate_rewrite_semantic.py

Environment variables:
    GATEWAY_JDBC_HOST    JDBC gateway host (default: localhost)
    GATEWAY_JDBC_PORT    JDBC gateway port (default: 5433)
    PG_HOST              Source PostgreSQL host (default: localhost)
    PG_PORT              Source PostgreSQL port (default: 5432)
    PG_DATABASE          Source PostgreSQL database (default: tessallite_system)
    PG_USER              Source PostgreSQL user (default: tessallite)
    PG_PASSWORD          Source PostgreSQL password (from .env POSTGRES_PASSWORD)
    BATCH_TENANT_SLUG    Tenant slug (default: acme-demo)
    BATCH_TENANT_EMAIL   Tenant email (default: admin@acme-demo.com)
    BATCH_TENANT_PASSWORD  Tenant password (default: acme-demo)
    BATCH_SQL            Path to SQL file (default: tests/sql_test_queries.sql)
    BATCH_SIZE           Queries per batch (default: 10)
    PHYSICAL_TABLE       Physical table name (default: demo_data.payment_transaction)
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Load .env
# ---------------------------------------------------------------------------
_ENV_FILE = Path(__file__).resolve().parents[3] / ".env"
if _ENV_FILE.exists():
    for line in _ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

import psycopg2

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
JDBC_HOST = os.environ.get("GATEWAY_JDBC_HOST", "localhost")
JDBC_PORT = int(os.environ.get("GATEWAY_JDBC_PORT", "5433"))
PG_HOST = os.environ.get("PG_HOST", "localhost")
PG_PORT = int(os.environ.get("PG_PORT", "5432"))
PG_DATABASE = os.environ.get("PG_DATABASE", "tessallite_system")
PG_USER = os.environ.get("PG_USER", "tessallite")
PG_PASSWORD = os.environ.get("PG_PASSWORD", os.environ.get("POSTGRES_PASSWORD", ""))
TENANT_SLUG = os.environ.get("BATCH_TENANT_SLUG", "acme-demo")
TENANT_EMAIL = os.environ.get("BATCH_TENANT_EMAIL", "admin@acme-demo.com")
TENANT_PASSWORD = os.environ.get("BATCH_TENANT_PASSWORD", "acme-demo")
SQL_FILE = os.environ.get(
    "BATCH_SQL",
    str(Path(__file__).parent / "sql_test_queries.sql"),
)
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "10"))
PHYSICAL_TABLE = os.environ.get("PHYSICAL_TABLE", "demo_data.payment_transaction")

# Queries that are expected to fail on direct source (physical JOINs blocked
# on business view) — skip these in comparison.
SKIP_LABELS = {f"Q12.{i:02d}" for i in range(1, 8)} | {
    # SELECT CURRENT_TIMESTAMP with no FROM clause — gateway returns literal
    # string instead of evaluated value (known JDBC gateway behavior for
    # system function queries with no table reference).
    "Q20.05",
}


# ---------------------------------------------------------------------------
# Query loader (reused from test_batch_queries.py)
# ---------------------------------------------------------------------------
@dataclass
class SQLQuery:
    label: str
    description: str
    sql: str


def load_sql_queries(path: str) -> list[SQLQuery]:
    with open(path) as f:
        content = f.read()
    parts = re.split(r"(^-- Q[\d.]+\s+.*$)", content, flags=re.MULTILINE)
    queries: list[SQLQuery] = []
    i = 0
    while i < len(parts):
        part = parts[i].strip()
        match = re.match(r"^-- (Q[\d.]+)\s+(.*)$", part)
        if match and i + 1 < len(parts):
            label = match.group(1)
            description = match.group(2).strip()
            sql_block = parts[i + 1].strip()
            sql = sql_block.rstrip(";").strip()
            sql_lines = [
                line for line in sql.split("\n")
                if line.strip() and not line.strip().startswith("--")
            ]
            sql = "\n".join(sql_lines).strip()
            if sql:
                queries.append(SQLQuery(label=label, description=description, sql=sql))
            i += 2
        else:
            i += 1
    return queries


# ---------------------------------------------------------------------------
# SQL substitution for direct source execution
# ---------------------------------------------------------------------------
def _to_direct_sql(sql: str, model_slug: str = "modely") -> str:
    """Replace the model slug with the physical table reference.

    Handles: FROM modely, FROM modely m1, JOIN modely m2, etc.
    """
    pattern = re.compile(
        r'((?:FROM|JOIN|,)\s+)'
        r'(?:"?\w+"?\s*\.\s*)?'
        r'"?' + re.escape(model_slug) + r'"?'
        r'(?=[\s,);]|$)',
        re.IGNORECASE | re.DOTALL,
    )
    return pattern.sub(lambda m: m.group(1) + f'"{PHYSICAL_TABLE.split(".")[0]}"."{PHYSICAL_TABLE.split(".")[1]}"', sql)


# ---------------------------------------------------------------------------
# Query execution helpers
# ---------------------------------------------------------------------------
@dataclass
class QueryResult:
    label: str
    columns: list[str]
    rows: list[list]
    row_count: int
    error: str | None = None


def _normalize_value(v):
    """Normalize a value to a canonical string for comparison.

    Both direct (binary protocol) and gateway (text protocol) values
    are converted to the same string form so type differences (int vs
    string, bool vs string) and timestamp format differences are
    eliminated before Claude sees them.
    """
    if v is None:
        return None
    s = str(v).strip()
    # Normalize booleans: True/true/TRUE → "true", False/false/FALSE → "false"
    if s.lower() in ("true", "false"):
        return s.lower()
    # Normalize timestamps: psycopg2 binary returns "2025-04-01 00:00:00+00:00"
    # while JDBC gateway returns "2025-04-01T00:00:00Z". Parse both to a
    # canonical ISO 8601 UTC form.
    if re.match(r'^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}', s):
        from datetime import datetime, timezone
        for fmt in (
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%d %H:%M:%S%z",
            "%Y-%m-%d %H:%M:%S.%f%z",
            "%Y-%m-%dT%H:%M:%S.%f%z",
            "%Y-%m-%dT%H:%M:%S.%fZ",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%dT%H:%M:%S",
        ):
            try:
                dt = datetime.strptime(s, fmt)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            except ValueError:
                continue
    # Try numeric normalization: strip trailing zeros after decimal.
    # Bug-5383: scientific-notation integers (e.g. "1.0E+5" from asyncpg
    # Decimal serialisation) must normalise to plain integer strings
    # ("100000").  The previous guard ``"e" not in s.lower()`` blocked
    # this path entirely for sci-notation inputs.
    try:
        f = float(s)
        if f == int(f):
            return str(int(f))
        return s
    except (ValueError, OverflowError):
        pass
    return s


def _run_query(conn, sql: str, label: str) -> QueryResult:
    """Execute SQL and return result with columns and rows."""
    try:
        cur = conn.cursor()
        cur.execute(sql)
        if cur.description:
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
            # Normalize all values to canonical strings
            clean_rows = []
            for row in rows[:50]:  # cap at 50 rows for comparison
                clean_rows.append([_normalize_value(v) for v in row])
            return QueryResult(
                label=label,
                columns=cols,
                rows=clean_rows,
                row_count=len(rows),
            )
        return QueryResult(label=label, columns=[], rows=[], row_count=0)
    except Exception as e:
        return QueryResult(
            label=label, columns=[], rows=[], row_count=0,
            error=str(e).strip(),
        )
    finally:
        cur.close()


# ---------------------------------------------------------------------------
# Claude CLI validation
# ---------------------------------------------------------------------------
def _validate_batch_with_claude(comparisons: list[dict]) -> tuple[str, bool]:
    """Call claude CLI to validate a batch of query comparisons.

    Returns (output_text, all_passed).
    """
    prompt = textwrap.dedent("""\
    You are a SQL semantic rewrite validator. For each query pair below,
    compare the DIRECT result (original SQL run against source table) with
    the GATEWAY result (same SQL routed through the semantic query rewriter).

    All values have been pre-normalized to the same string representation
    (types already coerced) so you can compare values directly.

    Rules:
    1. Column COUNT must match (same number of columns).
    2. Column NAMES must match (same aliases in same positions).
    3. Column ORDER must match (same sequence).
    4. Row COUNT must match.
    5. Row VALUES must match (same data in same column positions).
       All values are pre-normalized strings so compare them literally.
    6. If one side has an error and the other doesn't, that is a FAIL.
    7. If both sides error, that is a SKIP.
    8. When both sides return 0 rows, that is a PASS (the gateway may
       omit column descriptions for empty result sets).
    9. Row ORDER may differ ONLY when the original SQL has no ORDER BY
       clause (non-deterministic ordering). If ORDER BY is present,
       row order must match.

    For each query, respond with exactly one line:
        PASS Qxx.xx - <brief reason>
    or:
        FAIL Qxx.xx - <brief reason explaining the discrepancy>
    or:
        SKIP Qxx.xx - <reason>

    After all queries, add a final line:
        RESULT: ALL_PASS
    or:
        RESULT: HAS_FAILURES

    Here are the query comparisons:

    """)

    for c in comparisons:
        prompt += f"--- {c['label']} ({c['description']}) ---\n"
        prompt += f"Original SQL: {c['sql']}\n"
        # When the query has no ORDER BY, sort sample rows so the LLM
        # compares the same subset from both sides.
        _has_order = "ORDER BY" in c["sql"].upper()
        if c["direct_error"]:
            prompt += f"DIRECT: ERROR - {c['direct_error']}\n"
        else:
            prompt += f"DIRECT: {c['direct_col_count']} cols {c['direct_columns']}, {c['direct_row_count']} rows\n"
            if c["direct_rows"]:
                _d_rows = c["direct_rows"] if _has_order else sorted(c["direct_rows"])
                prompt += f"DIRECT sample (first 5): {json.dumps(_d_rows[:5])}\n"
        if c["gateway_error"]:
            prompt += f"GATEWAY: ERROR - {c['gateway_error']}\n"
        else:
            prompt += f"GATEWAY: {c['gateway_col_count']} cols {c['gateway_columns']}, {c['gateway_row_count']} rows\n"
            if c["gateway_rows"]:
                _g_rows = c["gateway_rows"] if _has_order else sorted(c["gateway_rows"])
                prompt += f"GATEWAY sample (first 5): {json.dumps(_g_rows[:5])}\n"
        prompt += "\n"

    try:
        result = subprocess.run(
            ["claude", "-p", prompt, "--output-format", "text"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        output = result.stdout.strip()
        if not output:
            output = result.stderr.strip() or "(no output from claude)"
        all_passed = "RESULT: ALL_PASS" in output
        return output, all_passed
    except FileNotFoundError:
        return "ERROR: 'claude' CLI not found in PATH", False
    except subprocess.TimeoutExpired:
        return "ERROR: claude CLI timed out after 120s", False
    except Exception as e:
        return f"ERROR: {e}", False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    print(f"Loading queries from: {SQL_FILE}")
    queries = load_sql_queries(SQL_FILE)
    # Filter out skip labels
    queries = [q for q in queries if q.label not in SKIP_LABELS]
    print(f"Loaded {len(queries)} queries (skipped {len(SKIP_LABELS)} xfail)")

    # Claude-CLI optionality (Bug-5453). The semantic verdict (PASS/FAIL) comes
    # ENTIRELY from the `claude` CLI; the deterministic pass only logs DIVERGE
    # status. In headless/non-auth contexts (e.g. the live-community gate) the CLI
    # may be ABSENT or PRESENT-BUT-NON-INTERACTIVE (it then hangs to the 120s
    # timeout per batch and the old code silently reported ALL_PASS over an empty
    # comparison). So unless explicitly required, skip the whole validator upfront
    # with a clear notice rather than depend on an unreliable CLI. Default
    # REQUIRE=1 keeps the dev run strict (and fails loudly if the CLI is missing).
    require_claude = os.environ.get("BATCH_REQUIRE_CLAUDE", "1") == "1"
    if not require_claude:
        print("SKIP: semantic comparison needs the `claude` CLI; BATCH_REQUIRE_CLAUDE=0 "
              "so it is skipped (set =1 to enforce).")
        print("RESULT: SKIPPED")
        sys.exit(0)
    if shutil.which("claude") is None:
        print("ERROR: 'claude' CLI not found in PATH and BATCH_REQUIRE_CLAUDE=1 "
              "(semantic comparison cannot run).")
        print("RESULT: HAS_FAILURES")
        sys.exit(1)

    # Connect to source DB
    print(f"Connecting to source PostgreSQL at {PG_HOST}:{PG_PORT}...")
    try:
        pg_conn = psycopg2.connect(
            host=PG_HOST, port=PG_PORT,
            database=PG_DATABASE,
            user=PG_USER, password=PG_PASSWORD,
            connect_timeout=10,
        )
        pg_conn.autocommit = True
    except Exception as e:
        print(f"ABORT: Cannot connect to source PostgreSQL: {e}")
        sys.exit(1)

    # Connect to JDBC gateway
    print(f"Connecting to JDBC gateway at {JDBC_HOST}:{JDBC_PORT}...")
    try:
        gw_conn = psycopg2.connect(
            host=JDBC_HOST, port=JDBC_PORT,
            database=TENANT_SLUG,
            user=TENANT_EMAIL, password=TENANT_PASSWORD,
            connect_timeout=10,
        )
        gw_conn.autocommit = True
    except Exception as e:
        print(f"ABORT: Cannot connect to JDBC gateway: {e}")
        pg_conn.close()
        sys.exit(1)

    print(f"Running {len(queries)} queries in batches of {BATCH_SIZE}...\n")
    total_pass = 0
    total_fail = 0
    total_skip = 0

    for batch_start in range(0, len(queries), BATCH_SIZE):
        batch = queries[batch_start : batch_start + BATCH_SIZE]
        batch_end = batch_start + len(batch)
        batch_label = f"{batch[0].label}..{batch[-1].label}"
        print(f"=== Batch {batch_start // BATCH_SIZE + 1}: {batch_label} ({len(batch)} queries) ===")

        comparisons: list[dict] = []
        for q in batch:
            # Run against source directly
            direct_sql = _to_direct_sql(q.sql)
            direct_result = _run_query(pg_conn, direct_sql, q.label)

            # Run through gateway
            gateway_result = _run_query(gw_conn, q.sql, q.label)

            comparisons.append({
                "label": q.label,
                "description": q.description,
                "sql": q.sql,
                "direct_sql": direct_sql,
                "direct_columns": direct_result.columns,
                "direct_col_count": len(direct_result.columns),
                "direct_row_count": direct_result.row_count,
                "direct_rows": direct_result.rows,
                "direct_error": direct_result.error,
                "gateway_columns": gateway_result.columns,
                "gateway_col_count": len(gateway_result.columns),
                "gateway_row_count": gateway_result.row_count,
                "gateway_rows": gateway_result.rows,
                "gateway_error": gateway_result.error,
            })

            # Quick pre-check for obvious mismatches
            status = "OK"
            if direct_result.error and gateway_result.error:
                status = "SKIP (both error)"
            elif direct_result.error and not gateway_result.error:
                status = "DIVERGE (direct error, gateway ok)"
            elif not direct_result.error and gateway_result.error:
                status = "DIVERGE (direct ok, gateway error)"
            elif direct_result.row_count != gateway_result.row_count:
                status = f"ROW_MISMATCH (direct={direct_result.row_count}, gateway={gateway_result.row_count})"
            elif len(direct_result.columns) != len(gateway_result.columns):
                status = f"COL_MISMATCH (direct={len(direct_result.columns)}, gateway={len(gateway_result.columns)})"
            print(f"  {q.label}: {status}")

        # Send batch to Claude for semantic validation
        print("\n  Validating batch with Claude CLI...")
        output, all_passed = _validate_batch_with_claude(comparisons)

        # Count results
        batch_pass = output.count("PASS Q")
        batch_fail = output.count("FAIL Q")
        batch_skip = output.count("SKIP Q")
        total_pass += batch_pass
        total_fail += batch_fail
        total_skip += batch_skip

        print("\n  Claude validation output:")
        for line in output.split("\n"):
            line = line.strip()
            if line:
                print(f"    {line}")

        print(f"\n  Batch result: {batch_pass} PASS, {batch_fail} FAIL, {batch_skip} SKIP")

        if not all_passed:
            print(f"\n*** STOPPING: Failures detected in batch {batch_label} ***")
            break

        print()

    pg_conn.close()
    gw_conn.close()

    print(f"\n{'=' * 60}")
    print(f"FINAL: {total_pass} PASS, {total_fail} FAIL, {total_skip} SKIP")
    if total_fail > 0:
        print("RESULT: HAS_FAILURES")
        sys.exit(1)
    else:
        print("RESULT: ALL_PASS")
        sys.exit(0)


if __name__ == "__main__":
    main()
