#!/usr/bin/env python3
"""
Aggregate routing validator — end-to-end semantic comparison.

Creates a disposable copy of modely, builds 5 aggregates with specific grains,
runs queries designed to hit full-match, partial-match (re-aggregation), and
miss (fallback to source) scenarios, then compares JDBC gateway results against
direct PostgreSQL to verify aggregate routing preserves result correctness.

Lifecycle:
  1. Export modely → import as modely_aggregate_testing
  2. Deploy the copy
  3. Create 5 aggregates + trigger refresh (CTAS build)
  4. Run 73 queries (13 categories) through both paths, validate with Claude CLI
  5. Delete the test model (cascade-deletes everything)

Usage:
    cd tessallite/services/query-router
    set -a && source ../../.env && set +a
    python tests/validate_rewrite_aggregate.py

Environment variables:
    MODEL_SERVICE_URL    Model service base URL (default: http://localhost:8001)
    SCHEDULER_CONTAINER  Docker container name for scheduler (default: infra-scheduler-1)
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
    BATCH_SIZE           Queries per validation batch (default: 10)
    PHYSICAL_TABLE       Physical table name (default: demo_data.payment_transaction)
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import textwrap
import time
import urllib.request
import urllib.error
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Load .env (for DB credentials only — service URLs use localhost defaults
# since this script runs outside Docker)
# ---------------------------------------------------------------------------
# Keys that should NOT be overridden by .env (Docker-internal hostnames)
_IGNORE_FROM_ENV = {"MODEL_SERVICE_URL", "QUERY_ROUTER_URL",
                    "OPTIMIZER_URL", "AGENT_SERVICE_URL"}

_ENV_FILE = Path(__file__).resolve().parents[3] / ".env"
if _ENV_FILE.exists():
    for line in _ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            k = k.strip()
            if k not in _IGNORE_FROM_ENV:
                os.environ.setdefault(k, v.strip())

import psycopg2

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
MODEL_SERVICE_URL = os.environ.get("MODEL_SERVICE_URL", "http://localhost:8001")
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
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "10"))
PHYSICAL_TABLE = os.environ.get("PHYSICAL_TABLE", "demo_data.payment_transaction")

TEST_MODEL_SLUG = "modely_aggregate_testing"
SOURCE_MODEL_SLUG = "modely"

# Aggregate definitions to create
AGGREGATE_SPECS = [
    {
        "name": "A1",
        "grain": ["payment_status"],
        "measure_names": ["transaction_amount", "fee_amount", "base_amount", "net_amount"],
    },
    {
        "name": "A2",
        "grain": ["payment_status", "event_type"],
        "measure_names": ["transaction_amount", "fee_amount", "base_amount", "net_amount"],
    },
    {
        "name": "A3",
        "grain": ["country_code", "payment_method", "business_date"],
        "measure_names": ["transaction_amount", "fee_amount", "commission_amount"],
    },
    {
        "name": "A4",
        "grain": ["customer_type", "payment_status"],
        "measure_names": ["transaction_amount", "fee_amount", "transaction_count", "discount_amount"],
    },
    {
        "name": "A5",
        "grain": ["business_date"],
        "measure_names": ["transaction_amount", "fee_amount", "base_amount", "settlement_amount", "commission_amount"],
    },
]


# ---------------------------------------------------------------------------
# Test queries
# ---------------------------------------------------------------------------
@dataclass
class SQLQuery:
    label: str
    description: str
    sql: str


TEST_QUERIES = [
    # --- Category 1: Full aggregate match ---
    SQLQuery("AF01", "Full match A1: single dim, single measure",
             f"SELECT payment_status, SUM(transaction_amount) FROM {TEST_MODEL_SLUG} GROUP BY payment_status"),
    SQLQuery("AF02", "Full match A1: single dim, multiple measures",
             f"SELECT payment_status, SUM(transaction_amount), SUM(fee_amount) FROM {TEST_MODEL_SLUG} GROUP BY payment_status"),
    SQLQuery("AF03", "Full match A2: two dim, single measure",
             f"SELECT payment_status, event_type, SUM(transaction_amount) FROM {TEST_MODEL_SLUG} GROUP BY payment_status, event_type"),
    SQLQuery("AF04", "Full match A2: two dim, all measures",
             f"SELECT payment_status, event_type, SUM(transaction_amount), SUM(fee_amount), SUM(base_amount), SUM(net_amount) FROM {TEST_MODEL_SLUG} GROUP BY payment_status, event_type"),
    SQLQuery("AF05", "Full match A1 with WHERE filter",
             f"SELECT payment_status, SUM(transaction_amount) FROM {TEST_MODEL_SLUG} WHERE payment_status = 'SUCCESS' GROUP BY payment_status"),
    SQLQuery("AF06", "Full match A1 with HAVING",
             f"SELECT payment_status, SUM(transaction_amount) FROM {TEST_MODEL_SLUG} GROUP BY payment_status HAVING SUM(transaction_amount) > 1000"),
    SQLQuery("AF07", "Full match A1 with ORDER BY",
             f"SELECT payment_status, SUM(transaction_amount) AS total FROM {TEST_MODEL_SLUG} GROUP BY payment_status ORDER BY total DESC"),
    SQLQuery("AF08", "Full match A3: three dim",
             f"SELECT country_code, payment_method, business_date, SUM(transaction_amount) FROM {TEST_MODEL_SLUG} GROUP BY country_code, payment_method, business_date ORDER BY country_code, payment_method, business_date"),

    # --- Category 2: Partial match (re-aggregation) ---
    SQLQuery("AP01", "Re-agg A3 to country_code only",
             f"SELECT country_code, SUM(transaction_amount) FROM {TEST_MODEL_SLUG} GROUP BY country_code"),
    SQLQuery("AP02", "Re-agg A3 to country_code + payment_method",
             f"SELECT country_code, payment_method, SUM(transaction_amount) FROM {TEST_MODEL_SLUG} GROUP BY country_code, payment_method"),
    SQLQuery("AP03", "Re-agg A3 to business_date only",
             f"SELECT business_date, SUM(fee_amount) FROM {TEST_MODEL_SLUG} GROUP BY business_date ORDER BY business_date"),
    SQLQuery("AP04", "Re-agg with WHERE on non-grain dim (falls to source)",
             f"SELECT payment_status, SUM(transaction_amount) FROM {TEST_MODEL_SLUG} WHERE customer_type = 'CORPORATE' GROUP BY payment_status"),
    SQLQuery("AP05", "Re-agg A2 with HAVING AVG (AVG decomposition)",
             f"SELECT payment_status, AVG(transaction_amount) FROM {TEST_MODEL_SLUG} GROUP BY payment_status HAVING AVG(transaction_amount) > 100"),
    SQLQuery("AP06", "Global aggregate (no GROUP BY)",
             f"SELECT SUM(transaction_amount) FROM {TEST_MODEL_SLUG}"),
    SQLQuery("AP07", "COUNT(*) re-aggregation",
             f"SELECT payment_status, COUNT(*) FROM {TEST_MODEL_SLUG} GROUP BY payment_status"),
    SQLQuery("AP08", "Re-agg with multiple measures",
             f"SELECT country_code, SUM(transaction_amount), SUM(fee_amount) FROM {TEST_MODEL_SLUG} GROUP BY country_code"),

    # --- Category 3: Miss (falls to source) ---
    SQLQuery("AM01", "Miss: grain dim not in any aggregate",
             f"SELECT lifecycle_stage, SUM(transaction_amount) FROM {TEST_MODEL_SLUG} GROUP BY lifecycle_stage"),
    SQLQuery("AM02", "Miss: measure not in any aggregate",
             f"SELECT payment_status, SUM(discount_amount) FROM {TEST_MODEL_SLUG} GROUP BY payment_status"),
    SQLQuery("AM03", "Miss: compound expression (passthrough)",
             f"SELECT SUM(fee_amount)/SUM(base_amount) FROM {TEST_MODEL_SLUG}"),
    SQLQuery("AM04", "Miss: CASE expression (passthrough)",
             f"SELECT CASE WHEN payment_status = 'SUCCESS' THEN 'OK' ELSE 'PENDING' END AS status_group, SUM(transaction_amount) FROM {TEST_MODEL_SLUG} GROUP BY 1"),
    SQLQuery("AM05", "Miss: detail query (no GROUP BY)",
             f"SELECT payment_status, transaction_amount FROM {TEST_MODEL_SLUG} LIMIT 20"),

    # --- Category 4: Edge cases ---
    SQLQuery("AE01", "MIN/MAX measures",
             f"SELECT payment_status, MIN(transaction_amount), MAX(transaction_amount) FROM {TEST_MODEL_SLUG} GROUP BY payment_status"),
    SQLQuery("AE02", "Mixed SUM + COUNT",
             f"SELECT payment_status, SUM(transaction_amount), COUNT(*) FROM {TEST_MODEL_SLUG} GROUP BY payment_status"),
    SQLQuery("AE03", "DISTINCT on aggregate grain dim",
             f"SELECT DISTINCT payment_status FROM {TEST_MODEL_SLUG}"),
    SQLQuery("AE04", "Subquery wrapping aggregate query",
             f"SELECT * FROM (SELECT payment_status, SUM(transaction_amount) AS total FROM {TEST_MODEL_SLUG} GROUP BY payment_status) t ORDER BY total DESC"),

    # --- Category 5: Multi-function on same measure (Bug-AGG-001 stress) ---
    SQLQuery("AF10", "SUM + AVG on same measure",
             f"SELECT payment_status, SUM(transaction_amount), AVG(transaction_amount) FROM {TEST_MODEL_SLUG} GROUP BY payment_status"),
    SQLQuery("AF11", "Triple agg: SUM + MIN + MAX on same measure",
             f"SELECT payment_status, SUM(transaction_amount), MIN(transaction_amount), MAX(transaction_amount) FROM {TEST_MODEL_SLUG} GROUP BY payment_status"),
    SQLQuery("AF12", "COUNT(*) + SUM + AVG in one query",
             f"SELECT payment_status, COUNT(*), SUM(transaction_amount), AVG(transaction_amount) FROM {TEST_MODEL_SLUG} GROUP BY payment_status"),
    SQLQuery("AF13", "Explicit aliases on multi-function",
             f"SELECT payment_status, SUM(transaction_amount) AS sum_ta, AVG(transaction_amount) AS avg_ta FROM {TEST_MODEL_SLUG} GROUP BY payment_status"),
    SQLQuery("AF14", "Four distinct measures all SUM",
             f"SELECT payment_status, SUM(fee_amount), SUM(base_amount), SUM(net_amount), SUM(transaction_amount) FROM {TEST_MODEL_SLUG} GROUP BY payment_status"),

    # --- Category 6: Complex WHERE / filter patterns ---
    SQLQuery("AW01", "IN list filter on grain dimension",
             f"SELECT payment_status, SUM(transaction_amount) FROM {TEST_MODEL_SLUG} WHERE payment_status IN ('SUCCESS', 'CANCELLED') GROUP BY payment_status"),
    SQLQuery("AW02", "LIKE filter on grain dimension",
             f"SELECT country_code, SUM(transaction_amount) FROM {TEST_MODEL_SLUG} WHERE country_code LIKE 'U%' GROUP BY country_code"),
    SQLQuery("AW03", "NOT EQUAL filter",
             f"SELECT payment_status, SUM(transaction_amount) FROM {TEST_MODEL_SLUG} WHERE payment_status != 'REVERSED' GROUP BY payment_status"),
    SQLQuery("AW04", "BETWEEN on time dimension (A5 grain)",
             f"SELECT business_date, SUM(transaction_amount) FROM {TEST_MODEL_SLUG} WHERE business_date BETWEEN '2025-04-18' AND '2025-04-25' GROUP BY business_date ORDER BY business_date"),
    SQLQuery("AW05", "Two-dim filter matching A2 grain",
             f"SELECT payment_status, event_type, SUM(transaction_amount) FROM {TEST_MODEL_SLUG} WHERE payment_status = 'SUCCESS' AND event_type = 'SALE' GROUP BY payment_status, event_type"),
    SQLQuery("AW06", "Filter on A3 grain dims, group by one (re-agg with filter)",
             f"SELECT country_code, SUM(transaction_amount) FROM {TEST_MODEL_SLUG} WHERE payment_method = 'CARD' GROUP BY country_code"),
    SQLQuery("AW07", "IS NOT NULL filter",
             f"SELECT payment_status, SUM(transaction_amount) FROM {TEST_MODEL_SLUG} WHERE payment_status IS NOT NULL GROUP BY payment_status"),
    SQLQuery("AW08", "Greater-than filter on grain dimension",
             f"SELECT payment_status, SUM(transaction_amount) FROM {TEST_MODEL_SLUG} WHERE payment_status > 'P' GROUP BY payment_status"),

    # --- Category 7: Complex GROUP BY & HAVING ---
    SQLQuery("AG01", "HAVING with cross-measure comparison",
             f"SELECT payment_status, SUM(transaction_amount) AS total, SUM(fee_amount) AS fees FROM {TEST_MODEL_SLUG} GROUP BY payment_status HAVING SUM(transaction_amount) > SUM(fee_amount)"),
    SQLQuery("AG02", "HAVING on COUNT(*)",
             f"SELECT payment_status, COUNT(*), SUM(transaction_amount) FROM {TEST_MODEL_SLUG} GROUP BY payment_status HAVING COUNT(*) > 100"),
    SQLQuery("AG03", "HAVING + ORDER BY on same aggregate",
             f"SELECT payment_status, SUM(transaction_amount) AS total FROM {TEST_MODEL_SLUG} GROUP BY payment_status HAVING SUM(transaction_amount) > 1000 ORDER BY total DESC"),
    SQLQuery("AG04", "HAVING + ORDER BY + LIMIT on re-agg",
             f"SELECT country_code, payment_method, SUM(transaction_amount) AS total FROM {TEST_MODEL_SLUG} GROUP BY country_code, payment_method HAVING SUM(transaction_amount) > 500 ORDER BY total DESC LIMIT 10"),
    SQLQuery("AG05", "Arithmetic on multiple aggregated measures",
             f"SELECT payment_status, SUM(transaction_amount) + SUM(fee_amount) AS combined FROM {TEST_MODEL_SLUG} GROUP BY payment_status ORDER BY combined DESC"),
    SQLQuery("AG06", "Scalar function wrapping aggregate",
             f"SELECT payment_status, ROUND(SUM(transaction_amount), 2) AS rounded_total FROM {TEST_MODEL_SLUG} GROUP BY payment_status"),

    # --- Category 8: Passthrough functions (should fall to source) ---
    SQLQuery("AT01", "COALESCE passthrough",
             f"SELECT COALESCE(payment_status, 'UNKNOWN') AS status, SUM(transaction_amount) FROM {TEST_MODEL_SLUG} GROUP BY 1"),
    SQLQuery("AT02", "UPPER() function in GROUP BY",
             f"SELECT UPPER(payment_status) AS status_upper, SUM(transaction_amount) FROM {TEST_MODEL_SLUG} GROUP BY 1"),
    SQLQuery("AT03", "EXTRACT on time dim (function grain)",
             f"SELECT EXTRACT(MONTH FROM business_date) AS month_num, SUM(transaction_amount) FROM {TEST_MODEL_SLUG} GROUP BY 1 ORDER BY 1"),
    SQLQuery("AT04", "String concatenation passthrough",
             f"SELECT payment_status || ' - ' || event_type AS combo, SUM(transaction_amount) FROM {TEST_MODEL_SLUG} GROUP BY 1"),
    SQLQuery("AT05", "Arithmetic on aggregated result",
             f"SELECT payment_status, SUM(transaction_amount) * 1.1 AS with_vat FROM {TEST_MODEL_SLUG} GROUP BY payment_status"),
    SQLQuery("AT06", "DATE_TRUNC on time dim",
             f"SELECT DATE_TRUNC('month', business_date) AS month_start, SUM(transaction_amount) FROM {TEST_MODEL_SLUG} GROUP BY 1 ORDER BY 1"),

    # --- Category 9: Non-additive measures & exact grain ---
    SQLQuery("AN01", "MIN on non-default-agg measure",
             f"SELECT payment_status, MIN(fee_amount) FROM {TEST_MODEL_SLUG} GROUP BY payment_status"),
    SQLQuery("AN02", "MAX + MIN on same measure (disambiguation + exact grain)",
             f"SELECT payment_status, MAX(base_amount), MIN(base_amount) FROM {TEST_MODEL_SLUG} GROUP BY payment_status"),
    SQLQuery("AN03", "Mixed SUM (mappable) + MIN (exact grain)",
             f"SELECT payment_status, SUM(transaction_amount), MIN(transaction_amount) FROM {TEST_MODEL_SLUG} GROUP BY payment_status"),
    SQLQuery("AN04", "MIN/MAX at A2 grain (exact match)",
             f"SELECT payment_status, event_type, MIN(transaction_amount), MAX(transaction_amount) FROM {TEST_MODEL_SLUG} GROUP BY payment_status, event_type"),
    SQLQuery("AN05", "MIN at coarser grain than any agg (falls to source)",
             f"SELECT country_code, MIN(transaction_amount) FROM {TEST_MODEL_SLUG} GROUP BY country_code"),

    # --- Category 10: Subqueries, CTEs, nesting ---
    SQLQuery("AS01", "Subquery with outer WHERE filter",
             f"SELECT * FROM (SELECT payment_status, SUM(transaction_amount) AS total, SUM(fee_amount) AS fees FROM {TEST_MODEL_SLUG} GROUP BY payment_status) sub WHERE total > 1000"),
    SQLQuery("AS02", "Aggregate of aggregated subquery",
             f"SELECT COUNT(*) FROM (SELECT payment_status, SUM(transaction_amount) AS total FROM {TEST_MODEL_SLUG} GROUP BY payment_status) sub"),
    SQLQuery("AS03", "Outer ORDER BY on computed expression",
             f"SELECT * FROM (SELECT payment_status, SUM(transaction_amount) AS total, SUM(fee_amount) AS fees FROM {TEST_MODEL_SLUG} GROUP BY payment_status) t ORDER BY total - fees DESC"),
    SQLQuery("AS04", "Subquery in WHERE (IN subselect)",
             f"SELECT payment_status, SUM(transaction_amount) FROM {TEST_MODEL_SLUG} WHERE payment_status IN (SELECT DISTINCT payment_status FROM {TEST_MODEL_SLUG} WHERE event_type = 'SALE') GROUP BY payment_status"),
    SQLQuery("AS05", "CTE (Common Table Expression)",
             f"WITH base AS (SELECT payment_status, SUM(transaction_amount) AS total FROM {TEST_MODEL_SLUG} GROUP BY payment_status) SELECT * FROM base WHERE total > 1000"),

    # --- Category 11: LIMIT, OFFSET, TOP-N ---
    SQLQuery("AL01", "TOP-N pattern",
             f"SELECT payment_status, SUM(transaction_amount) AS total FROM {TEST_MODEL_SLUG} GROUP BY payment_status ORDER BY total DESC LIMIT 3"),
    SQLQuery("AL02", "LIMIT with OFFSET",
             f"SELECT payment_status, SUM(transaction_amount) AS total FROM {TEST_MODEL_SLUG} GROUP BY payment_status ORDER BY total DESC LIMIT 5 OFFSET 2"),
    SQLQuery("AL03", "Latest-N dates pattern",
             f"SELECT business_date, SUM(transaction_amount) AS total FROM {TEST_MODEL_SLUG} GROUP BY business_date ORDER BY business_date DESC LIMIT 10"),
    SQLQuery("AL04", "Bottom-1 pattern",
             f"SELECT payment_status, SUM(transaction_amount) AS total FROM {TEST_MODEL_SLUG} GROUP BY payment_status ORDER BY total ASC LIMIT 1"),

    # --- Category 12: Cross-aggregate & no-match ---
    SQLQuery("AX01", "A4 grain, measure from A4 only",
             f"SELECT customer_type, SUM(transaction_amount), SUM(transaction_count) FROM {TEST_MODEL_SLUG} GROUP BY customer_type"),
    SQLQuery("AX02", "Full match on A4 (two dims, four measures)",
             f"SELECT customer_type, payment_status, SUM(fee_amount), SUM(discount_amount) FROM {TEST_MODEL_SLUG} GROUP BY customer_type, payment_status"),
    SQLQuery("AX03", "Measure in A5 only, grain in A1 only (no match)",
             f"SELECT payment_status, SUM(settlement_amount) FROM {TEST_MODEL_SLUG} GROUP BY payment_status"),
    SQLQuery("AX04", "Full match on A5 (single time dim)",
             f"SELECT business_date, SUM(settlement_amount) FROM {TEST_MODEL_SLUG} GROUP BY business_date ORDER BY business_date"),

    # --- Category 13: Additional stress patterns ---
    SQLQuery("AZ01", "Global MIN + MAX (no GROUP BY, Bug-880 regression)",
             f"SELECT MIN(transaction_amount), MAX(transaction_amount) FROM {TEST_MODEL_SLUG}"),
    SQLQuery("AZ02", "COUNT DISTINCT (non-additive, falls to source)",
             f"SELECT payment_status, COUNT(DISTINCT event_type) FROM {TEST_MODEL_SLUG} GROUP BY payment_status"),
    SQLQuery("AZ03", "Mixed aggs across two measures",
             f"SELECT payment_status, SUM(transaction_amount), AVG(fee_amount) FROM {TEST_MODEL_SLUG} GROUP BY payment_status"),
    SQLQuery("AZ04", "A4 re-agg to single dim with HAVING",
             f"SELECT customer_type, SUM(transaction_count) AS total_txns FROM {TEST_MODEL_SLUG} GROUP BY customer_type HAVING SUM(transaction_count) > 50 ORDER BY total_txns DESC"),
    SQLQuery("AZ05", "A5 time series with multiple measures and ORDER BY",
             f"SELECT business_date, SUM(transaction_amount) AS total, SUM(commission_amount) AS comm, SUM(settlement_amount) AS settled FROM {TEST_MODEL_SLUG} GROUP BY business_date ORDER BY business_date"),
]


# ---------------------------------------------------------------------------
# HTTP helpers (stdlib only — no requests dependency)
# ---------------------------------------------------------------------------
def _api(method: str, url: str, token: str | None = None,
         body: dict | None = None, timeout: int = 30) -> dict:
    """Make an HTTP request and return the JSON response."""
    data = json.dumps(body).encode() if body else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            if not raw:
                return {}
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        detail = e.read().decode() if e.fp else str(e)
        raise RuntimeError(f"HTTP {e.code} {method} {url}: {detail}") from e


def _login() -> str:
    """Login and return JWT token."""
    resp = _api("POST", f"{MODEL_SERVICE_URL}/api/v1/auth/login", body={
        "tenant_id": TENANT_SLUG,
        "email": TENANT_EMAIL,
        "password": TENANT_PASSWORD,
    })
    return resp["access_token"]


SCHEDULER_CONTAINER = os.environ.get("SCHEDULER_CONTAINER", "infra-scheduler-1")
SCHEDULER_INTERNAL_URL = os.environ.get(
    "SCHEDULER_INTERNAL_URL", "http://localhost:8000"
)


def _trigger_refresh_via_docker(aggregate_id: str, token: str) -> dict:
    """Trigger an aggregate refresh by calling the scheduler inside Docker.

    The scheduler container is not port-mapped to the host, so we use
    ``docker exec`` to run a Python one-liner inside the container
    (curl is not installed in the slim image).
    """
    py_script = (
        "import json,urllib.request as u;"
        f"r=u.urlopen(u.Request('{SCHEDULER_INTERNAL_URL}/api/v1/scheduler/trigger/refresh',"
        f"data=json.dumps({{'aggregate_id':'{aggregate_id}','mode':'full'}}).encode(),"
        f"headers={{'Content-Type':'application/json','Authorization':'Bearer {token}'}},"
        "method='POST'),timeout=120);"
        "print(r.read().decode())"
    )
    cmd = ["docker", "exec", SCHEDULER_CONTAINER, "python", "-c", py_script]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if result.returncode != 0:
        raise RuntimeError(
            f"docker exec python failed (rc={result.returncode}): "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    if not result.stdout.strip():
        return {}
    return json.loads(result.stdout)


# ---------------------------------------------------------------------------
# SQL substitution for direct source execution
# ---------------------------------------------------------------------------
def _to_direct_sql(sql: str) -> str:
    """Replace the test model slug with the physical table reference."""
    pattern = re.compile(
        r'((?:FROM|JOIN|,)\s+)'
        r'(?:"?\w+"?\s*\.\s*)?'
        r'"?' + re.escape(TEST_MODEL_SLUG) + r'"?'
        r'(?=[\s,);]|$)',
        re.IGNORECASE | re.DOTALL,
    )
    schema, table = PHYSICAL_TABLE.split(".")
    return pattern.sub(lambda m: m.group(1) + f'"{schema}"."{table}"', sql)


# ---------------------------------------------------------------------------
# Value normalization (shared with validate_rewrite_semantic.py)
# ---------------------------------------------------------------------------
def _normalize_value(v):
    """Normalize a value to a canonical string for comparison."""
    if v is None:
        return None
    s = str(v).strip()
    if s.lower() in ("true", "false"):
        return s.lower()
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
    try:
        f = float(s)
        if f == int(f) and "." not in s and "e" not in s.lower():
            return str(int(f))
        return s
    except (ValueError, OverflowError):
        pass
    return s


# ---------------------------------------------------------------------------
# Query execution
# ---------------------------------------------------------------------------
@dataclass
class QueryResult:
    label: str
    columns: list[str]
    rows: list[list]
    row_count: int
    error: str | None = None


def _run_query(conn, sql: str, label: str) -> QueryResult:
    """Execute SQL and return result with columns and rows."""
    cur = conn.cursor()
    try:
        cur.execute(sql)
        if cur.description:
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
            clean_rows = []
            for row in rows[:50]:
                clean_rows.append([_normalize_value(v) for v in row])
            return QueryResult(label=label, columns=cols, rows=clean_rows, row_count=len(rows))
        return QueryResult(label=label, columns=[], rows=[], row_count=0)
    except Exception as e:
        conn.rollback()
        return QueryResult(label=label, columns=[], rows=[], row_count=0, error=str(e).strip())
    finally:
        cur.close()


# ---------------------------------------------------------------------------
# Claude CLI validation
# ---------------------------------------------------------------------------
def _validate_batch_with_claude(comparisons: list[dict]) -> tuple[str, bool]:
    """Call claude CLI to validate a batch of query comparisons."""
    prompt = textwrap.dedent("""\
    You are a SQL semantic rewrite validator. For each query pair below,
    compare the DIRECT result (original SQL run against source table) with
    the GATEWAY result (same SQL routed through the semantic query rewriter,
    which may use aggregate tables for acceleration).

    All values have been pre-normalized to the same string representation
    (types already coerced) so you can compare values directly.

    Rules:
    1. Column COUNT must match (same number of columns).
    2. Column NAMES: when the original SQL uses an explicit alias (e.g.
       "AS total"), both sides must use that alias. When there is NO
       explicit alias (bare SUM(x), AVG(x), etc.), the direct side will
       show the PostgreSQL default ("sum", "avg", "count", etc.) while
       the gateway may show the measure name (e.g. "transaction_amount",
       "fee_amount"). This is expected semantic-layer behaviour and is
       NOT a failure.  When the same measure appears with multiple
       aggregate functions (e.g. MIN(x) and MAX(x)), the gateway may
       use the measure name for the first occurrence and the aggregate
       function name for the second (e.g. "transaction_amount" and
       "max" instead of "min" and "max") — this disambiguation is also
       expected and NOT a failure. Only flag a column-name mismatch
       when an explicit alias is present and different, or when columns
       are in the wrong positional order.
    3. Column ORDER must match (same sequence of columns).
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
        PASS <label> - <brief reason>
    or:
        FAIL <label> - <brief reason explaining the discrepancy>
    or:
        SKIP <label> - <reason>

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
        # compares the same subset from both sides (non-deterministic
        # row order otherwise causes false negatives).
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
            capture_output=True, text=True, timeout=120,
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
# Setup: export → import → deploy → create aggregates → build
# ---------------------------------------------------------------------------
class TestModelContext:
    """Holds IDs for the test model lifecycle."""
    token: str = ""
    project_id: str = ""
    source_model_id: str = ""
    test_model_id: str = ""
    target_id: str = ""
    aggregate_ids: list[str] = []

    def __init__(self):
        self.aggregate_ids = []


def _cleanup_existing(ctx: TestModelContext) -> None:
    """Delete the test model if it exists from a prior failed run."""
    try:
        models = _api("GET", f"{MODEL_SERVICE_URL}/api/v1/projects/{ctx.project_id}/models",
                       token=ctx.token)
        for m in models:
            if m.get("slug") == TEST_MODEL_SLUG:
                mid = m["id"]
                print(f"  Cleaning up existing {TEST_MODEL_SLUG} (id: {mid})")
                _api("DELETE",
                     f"{MODEL_SERVICE_URL}/api/v1/projects/{ctx.project_id}/models/{mid}",
                     token=ctx.token)
                break
    except Exception as e:
        print(f"  Warning: cleanup check failed: {e}")


def setup(ctx: TestModelContext) -> bool:
    """Set up the test model with aggregates. Returns True on success."""
    print("=== SETUP ===")

    # 1. Login
    try:
        ctx.token = _login()
        print(f"  Logged in as {TENANT_EMAIL}")
    except Exception as e:
        print(f"  ABORT: Login failed: {e}")
        return False

    # 2. Discover project and source model
    try:
        projects = _api("GET", f"{MODEL_SERVICE_URL}/api/v1/projects", token=ctx.token)
        project = next((p for p in projects if p.get("slug") == "project1"), None)
        if not project:
            print("  ABORT: project1 not found")
            return False
        ctx.project_id = project["id"]

        models = _api("GET",
                       f"{MODEL_SERVICE_URL}/api/v1/projects/{ctx.project_id}/models",
                       token=ctx.token)
        source = next((m for m in models if m.get("slug") == SOURCE_MODEL_SLUG), None)
        if not source:
            print(f"  ABORT: {SOURCE_MODEL_SLUG} not found")
            return False
        ctx.source_model_id = source["id"]
        print(f"  Found project1 ({ctx.project_id}) and {SOURCE_MODEL_SLUG} ({ctx.source_model_id})")
    except Exception as e:
        print(f"  ABORT: Discovery failed: {e}")
        return False

    # 3. Clean up any prior test model
    _cleanup_existing(ctx)

    # 4. Export source model
    try:
        export = _api("GET",
                       f"{MODEL_SERVICE_URL}/api/v1/projects/{ctx.project_id}"
                       f"/models/{ctx.source_model_id}/snapshot-export",
                       token=ctx.token)
        bundle = export["bundle"]
        connections = export.get("connections_required", [])
        print(f"  Exported {SOURCE_MODEL_SLUG} snapshot")
    except Exception as e:
        print(f"  ABORT: Export failed: {e}")
        return False

    # 5. Import as test model
    try:
        conn_mapping = {}
        for c in connections:
            conn_mapping[str(c["id"])] = str(c["id"])

        # Strip entities that would cause UUID collisions within the same
        # tenant (KPIs, named sets, glossary entries retain their original
        # UUIDs across import). We don't need them for aggregate testing.
        snap = bundle.get("snapshot", {})
        for key in ("kpis", "named_sets", "glossary_entries",
                     "kpi_presentation_meta", "kpi_threshold_bands"):
            snap.pop(key, None)
        # Strip existing aggregates — we create our own
        snap.pop("aggregates", None)
        snap.pop("aggregate_columns", None)

        import_resp = _api("POST",
                           f"{MODEL_SERVICE_URL}/api/v1/projects/{ctx.project_id}"
                           f"/models/snapshot-import",
                           token=ctx.token,
                           body={
                               "bundle": bundle,
                               "target_project_id": ctx.project_id,
                               "target_slug": TEST_MODEL_SLUG,
                               "target_display_name": "Modely Aggregate Testing",
                               "connection_mapping": conn_mapping,
                               "deploy_immediately": False,
                           },
                           timeout=60)
        ctx.test_model_id = import_resp["model_id"]
        print(f"  Imported as {TEST_MODEL_SLUG} (model_id: {ctx.test_model_id})")
    except Exception as e:
        print(f"  ABORT: Import failed: {e}")
        return False

    # 6. Save a version, then deploy
    try:
        _api("POST",
             f"{MODEL_SERVICE_URL}/api/v1/projects/{ctx.project_id}"
             f"/models/{ctx.test_model_id}/versions",
             token=ctx.token,
             body={"summary": "Initial version for aggregate testing"})
        _api("POST",
             f"{MODEL_SERVICE_URL}/api/v1/projects/{ctx.project_id}"
             f"/models/{ctx.test_model_id}/deploy",
             token=ctx.token, timeout=30)
        print(f"  Deployed {TEST_MODEL_SLUG}")
    except Exception as e:
        print(f"  ABORT: Deploy failed: {e}")
        return False

    # 7. Discover the data target for aggregate creation
    try:
        targets = _api("GET",
                        f"{MODEL_SERVICE_URL}/api/v1/projects/{ctx.project_id}"
                        f"/models/{ctx.test_model_id}/targets",
                        token=ctx.token)
        if not targets:
            # Fall back to model.target_id from models list
            models = _api("GET",
                          f"{MODEL_SERVICE_URL}/api/v1/projects/{ctx.project_id}/models",
                          token=ctx.token)
            test_model = next((m for m in models if m["id"] == ctx.test_model_id), None)
            if test_model and test_model.get("target_id"):
                ctx.target_id = test_model["target_id"]
            else:
                print("  ABORT: No data target found for test model")
                return False
        else:
            ctx.target_id = targets[0]["id"]
        print(f"  Found data target: {ctx.target_id}")
    except Exception as e:
        print(f"  ABORT: Target discovery failed: {e}")
        return False

    # 8. Create aggregates and trigger refresh
    for spec in AGGREGATE_SPECS:
        try:
            agg_resp = _api("POST",
                            f"{MODEL_SERVICE_URL}/api/v1/projects/{ctx.project_id}"
                            f"/models/{ctx.test_model_id}/aggregates",
                            token=ctx.token,
                            body={
                                "target_id": ctx.target_id,
                                "grain": spec["grain"],
                                "measure_names": spec["measure_names"],
                            })
            agg_id = agg_resp["id"]
            ctx.aggregate_ids.append(agg_id)
            print(f"  Created aggregate {spec['name']} {spec['grain']} (id: {agg_id})")
        except Exception as e:
            print(f"  ABORT: Create aggregate {spec['name']} failed: {e}")
            return False

        # Trigger refresh (CTAS build).
        # The scheduler service is not port-mapped to the host, so we call
        # it via docker exec into the scheduler container.
        try:
            _trigger_refresh_via_docker(agg_id, ctx.token)
            print(f"  Built aggregate {spec['name']} (refresh complete)")
        except Exception as e:
            print(f"  ABORT: Refresh aggregate {spec['name']} failed: {e}")
            return False

    # Brief pause to let the gateway pick up the new model metadata
    print("  Waiting 3s for gateway metadata refresh...")
    time.sleep(3)

    print()
    return True


# ---------------------------------------------------------------------------
# Teardown
# ---------------------------------------------------------------------------
def teardown(ctx: TestModelContext) -> None:
    """Delete the test model and all its children."""
    print("\n=== TEARDOWN ===")
    if not ctx.test_model_id:
        print("  Nothing to clean up (no test model created)")
        return
    try:
        _api("DELETE",
             f"{MODEL_SERVICE_URL}/api/v1/projects/{ctx.project_id}"
             f"/models/{ctx.test_model_id}",
             token=ctx.token, timeout=30)
        print(f"  Deleted {TEST_MODEL_SLUG} ({ctx.test_model_id})")
    except Exception as e:
        print(f"  WARNING: Teardown failed: {e}")
        print(f"  Manual cleanup required: DELETE model {ctx.test_model_id}")


# ---------------------------------------------------------------------------
# Main validation loop
# ---------------------------------------------------------------------------
def run_validation(pg_conn, gw_conn) -> tuple[int, int, int]:
    """Run all test queries and validate. Returns (pass, fail, skip)."""
    print("=== QUERIES ===")
    print(f"Running {len(TEST_QUERIES)} queries in batches of {BATCH_SIZE}...\n")

    total_pass = 0
    total_fail = 0
    total_skip = 0

    for batch_start in range(0, len(TEST_QUERIES), BATCH_SIZE):
        batch = TEST_QUERIES[batch_start: batch_start + BATCH_SIZE]
        batch_label = f"{batch[0].label}..{batch[-1].label}"
        print(f"=== Batch {batch_start // BATCH_SIZE + 1}: {batch_label} ({len(batch)} queries) ===")

        comparisons: list[dict] = []
        for q in batch:
            direct_sql = _to_direct_sql(q.sql)
            direct_result = _run_query(pg_conn, direct_sql, q.label)
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

            status = "OK"
            if direct_result.error and gateway_result.error:
                status = "SKIP (both error)"
            elif direct_result.error and not gateway_result.error:
                status = "DIVERGE (direct error, gateway ok)"
            elif not direct_result.error and gateway_result.error:
                status = "DIVERGE (direct ok, gateway error)"
            elif direct_result.row_count != gateway_result.row_count:
                status = f"ROW_MISMATCH (direct={direct_result.row_count}, gw={gateway_result.row_count})"
            elif len(direct_result.columns) != len(gateway_result.columns):
                status = f"COL_MISMATCH (direct={len(direct_result.columns)}, gw={len(gateway_result.columns)})"
            print(f"  {q.label}: {status}")

        print("\n  Validating batch with Claude CLI...")
        output, all_passed = _validate_batch_with_claude(comparisons)

        # Count verdicts per label.  Claude sometimes self-corrects
        # (writes FAIL then reclassifies to PASS), so we keep only
        # the LAST verdict emitted for each label.
        _verdicts: dict[str, str] = {}  # label -> "PASS"/"FAIL"/"SKIP"
        _verdict_re = re.compile(
            r"^(PASS|FAIL|SKIP)\s+(A[A-Z]\d+)"
        )
        for _line in output.split("\n"):
            _line = _line.strip().lstrip("`").rstrip("`").strip()
            _m = _verdict_re.match(_line)
            if _m:
                _verdicts[_m.group(2)] = _m.group(1)
        batch_pass = sum(1 for v in _verdicts.values() if v == "PASS")
        batch_fail = sum(1 for v in _verdicts.values() if v == "FAIL")
        batch_skip = sum(1 for v in _verdicts.values() if v == "SKIP")
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

    return total_pass, total_fail, total_skip


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    ctx = TestModelContext()

    # Setup: export → import → deploy → create aggregates
    if not setup(ctx):
        teardown(ctx)
        sys.exit(1)

    # Connect to source DB
    print(f"Connecting to source PostgreSQL at {PG_HOST}:{PG_PORT}...")
    try:
        pg_conn = psycopg2.connect(
            host=PG_HOST, port=PG_PORT,
            database=PG_DATABASE, user=PG_USER, password=PG_PASSWORD,
            connect_timeout=10,
        )
        pg_conn.autocommit = True
    except Exception as e:
        print(f"ABORT: Cannot connect to source PostgreSQL: {e}")
        teardown(ctx)
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
        teardown(ctx)
        sys.exit(1)

    print()

    # Run validation
    total_pass, total_fail, total_skip = run_validation(pg_conn, gw_conn)

    pg_conn.close()
    gw_conn.close()

    # Teardown
    teardown(ctx)

    # Final report
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
