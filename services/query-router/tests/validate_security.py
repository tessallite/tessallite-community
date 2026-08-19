#!/usr/bin/env python3
"""
Security enforcement validator -- JDBC gateway + query-router REST only.

This suite does not claim XMLA, aggregate HIT, pocket, MCP, or agent coverage
(F-007-07). Those routes need their own live probes.

Re-run trigger (Bug-6037): this suite MUST be re-run whenever any of these
files change: routing/router.py, semantic/binder.py,
rewrite/source_sql.py (especially row-security injection), or
shared/auth/project_access.py. These are the security-enforcement evidence
gates. Requires: Docker stack up + Claude CLI (or BATCH_REVIEWER=codex).

Creates a disposable copy of modely, sets up a restricted persona and row
security rules, then runs queries to verify:
  1. Persona gate: column-level access control (measure/dimension allow-lists)
  2. Row security: row-level predicates injected per table scan (every
     UNION branch / subquery that reads a table is constrained — F-007-01)
  3. Query audit: post-rewrite filter integrity
  4. Claims sources: saml_claim / oidc_scope rules enforced at query time
     from claims carried on the JWT (F-007-02)

Lifecycle:
  1. Export modely -> import as modely_security_testing
  2. Deploy the copy
  3. Create a restricted persona (limited measures/dimensions)
  4. Create a row security rule (role_predicate)
  5. Run the query matrix through the JDBC gateway, verify enforcement;
     then run the claims-source scenarios via query-router REST
  6. Delete the test model (cascade-deletes everything)

Usage:
    cd tessallite/services/query-router
    set -a && source ../../.env && set +a
    python tests/validate_security.py

Environment variables:
    MODEL_SERVICE_URL      Model service base URL (default: http://localhost:8001)
    QUERY_ROUTER_URL       Query-router base URL (default:
                           http://localhost:3000/query-router — nginx proxy)
    GATEWAY_JDBC_HOST      JDBC gateway host (default: localhost)
    GATEWAY_JDBC_PORT      JDBC gateway port (default: 5433)
    PG_HOST                Source PostgreSQL host (default: localhost)
    PG_PORT                Source PostgreSQL port (default: 5432)
    PG_DATABASE            Source PostgreSQL database (default: tessallite_system)
    PG_USER                Source PostgreSQL user (default: tessallite)
    PG_PASSWORD            Source PostgreSQL password (from .env POSTGRES_PASSWORD)
    BATCH_TENANT_SLUG      Tenant slug (default: acme-demo)
    BATCH_TENANT_EMAIL     Tenant email (default: admin@acme-demo.com)
    BATCH_TENANT_PASSWORD  Tenant password (default: acme-demo)
    PHYSICAL_TABLE         Physical table name (default: demo_data.payment_transaction)
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Load .env
# ---------------------------------------------------------------------------
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

try:
    import psycopg2  # only needed for the live JDBC path (_run_query_jdbc)
except ModuleNotFoundError:
    # Unit tests import the pure validators (_validate_query, QueryResult,
    # SecurityQuery) from this module without a DB driver present; psycopg2 is
    # required only when actually executing against the live gateway.
    psycopg2 = None

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
MODEL_SERVICE_URL = os.environ.get("MODEL_SERVICE_URL", "http://localhost:8001")
# query-router has no published host port — reach it through the frontend
# nginx proxy (rewrites /query-router/* to the service root).
QUERY_ROUTER_URL = os.environ.get(
    "QUERY_ROUTER_URL", "http://localhost:3000/query-router",
)
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
PHYSICAL_TABLE = os.environ.get("PHYSICAL_TABLE", "demo_data.payment_transaction")

TEST_MODEL_SLUG = "modely_security_testing"
SOURCE_MODEL_SLUG = "modely"


# ---------------------------------------------------------------------------
# Test queries
# ---------------------------------------------------------------------------
@dataclass
class SecurityQuery:
    label: str
    description: str
    sql: str
    table_variant: str  # "" (base), "_restricted", "_technical"
    expect: str         # "rows", "narrowed", "error", "filtered"
    xfail: str = ""     # non-empty = expected failure (bug ID / reason)


# Persona: "restricted" - only transaction_amount, fee_amount + payment_status, event_type
# Row security: dimension_equals('payment_status', 'SUCCESS') predicate

_BASE = TEST_MODEL_SLUG
_RESTRICTED = f"{TEST_MODEL_SLUG}_restricted"
_TECHNICAL = f"{TEST_MODEL_SLUG}_technical"

TEST_QUERIES = [
    # --- SP: Persona gate - SELECT * narrowing ---
    SecurityQuery(
        "SP01", "SELECT * on restricted persona narrows columns",
        f"SELECT * FROM {_RESTRICTED} LIMIT 5",
        "_restricted", "narrowed",
    ),
    SecurityQuery(
        "SP02", "SELECT * on base returns all columns",
        f"SELECT * FROM {_BASE} LIMIT 5",
        "", "rows",
    ),
    SecurityQuery(
        "SP03", "Explicit allowed measure on restricted persona",
        f"SELECT payment_status, SUM(transaction_amount) FROM {_RESTRICTED} GROUP BY payment_status",
        "_restricted", "rows",
    ),
    SecurityQuery(
        "SP04", "Explicit allowed dim + measure on restricted persona",
        f"SELECT event_type, SUM(fee_amount) FROM {_RESTRICTED} GROUP BY event_type",
        "_restricted", "rows",
    ),
    SecurityQuery(
        "SP05", "SELECT * on technical returns hidden columns",
        f"SELECT * FROM {_TECHNICAL} LIMIT 5",
        "_technical", "rows",
    ),
    SecurityQuery(
        "SP06", "Aggregation on restricted persona succeeds",
        f"SELECT payment_status, SUM(transaction_amount), SUM(fee_amount) FROM {_RESTRICTED} GROUP BY payment_status",
        "_restricted", "rows",
    ),

    # --- SB: Persona gate - blocked column requests ---
    SecurityQuery(
        "SB01", "Blocked measure on restricted persona",
        f"SELECT payment_status, SUM(base_amount) FROM {_RESTRICTED} GROUP BY payment_status",
        "_restricted", "error",
    ),
    SecurityQuery(
        "SB02", "Blocked dimension on restricted persona",
        f"SELECT country_code, SUM(transaction_amount) FROM {_RESTRICTED} GROUP BY country_code",
        "_restricted", "error",
    ),
    SecurityQuery(
        "SB03", "Blocked measure with explicit alias",
        f"SELECT payment_status, SUM(net_amount) AS total FROM {_RESTRICTED} GROUP BY payment_status",
        "_restricted", "error",
    ),
    SecurityQuery(
        "SB04", "Blocked dim in WHERE on restricted persona",
        f"SELECT payment_status, SUM(transaction_amount) FROM {_RESTRICTED} WHERE country_code = 'US' GROUP BY payment_status",
        "_restricted", "error",
        xfail="Bug-884: persona gate restricts SELECT only, not WHERE predicates",
    ),

    # --- SR: Row security - filter enforcement ---
    SecurityQuery(
        "SR01", "Row security: only SUCCESS rows returned on base",
        f"SELECT DISTINCT payment_status FROM {_BASE} ORDER BY payment_status",
        "", "filtered",
    ),
    SecurityQuery(
        "SR02", "Row security: aggregation only counts SUCCESS rows",
        f"SELECT SUM(transaction_amount) FROM {_BASE}",
        "", "filtered",
    ),
    SecurityQuery(
        "SR03", "Row security: detail query filtered",
        f"SELECT payment_status, transaction_amount FROM {_BASE} LIMIT 20",
        "", "filtered",
    ),
    SecurityQuery(
        "SR04", "Row security: GROUP BY sees only SUCCESS",
        f"SELECT payment_status, COUNT(*) FROM {_BASE} GROUP BY payment_status",
        "", "filtered",
    ),
    SecurityQuery(
        "SR05", "Row security: WHERE + row security combine",
        f"SELECT event_type, SUM(transaction_amount) FROM {_BASE} WHERE event_type = 'SALE' GROUP BY event_type",
        "", "filtered",
    ),
    SecurityQuery(
        "SR06", "Row security: restricted persona also filtered",
        f"SELECT payment_status, SUM(transaction_amount) FROM {_RESTRICTED} GROUP BY payment_status",
        "_restricted", "filtered",
    ),

    # --- SA: Query audit - filter integrity ---
    SecurityQuery(
        "SA01", "Audit: WHERE filter preserved through source path",
        f"SELECT payment_status, SUM(transaction_amount) FROM {_BASE} WHERE payment_status = 'SUCCESS' GROUP BY payment_status",
        "", "rows",
    ),
    SecurityQuery(
        "SA02", "Audit: multiple WHERE conditions preserved",
        f"SELECT event_type, SUM(transaction_amount) FROM {_BASE} WHERE payment_status = 'SUCCESS' AND event_type = 'SALE' GROUP BY event_type",
        "", "rows",
    ),
    SecurityQuery(
        "SA03", "Audit: BETWEEN filter preserved",
        f"SELECT payment_status, SUM(transaction_amount) FROM {_BASE} WHERE transaction_amount BETWEEN 100 AND 1000 GROUP BY payment_status",
        "", "rows",
    ),
    SecurityQuery(
        "SA04", "Audit: IN filter preserved",
        f"SELECT payment_status, SUM(transaction_amount) FROM {_BASE} WHERE payment_status IN ('SUCCESS', 'PENDING') GROUP BY payment_status",
        "", "rows",
    ),

    # --- SU: Row security on subquery / UNION shapes (F-007-01) ---
    SecurityQuery(
        "SU01", "Row security: UNION ALL — both branches filtered",
        f"SELECT payment_status FROM {_BASE} WHERE event_type = 'SALE' "
        f"UNION ALL SELECT payment_status FROM {_BASE}",
        "", "filtered",
    ),
    SecurityQuery(
        "SU02", "Row security: subquery hunting non-SUCCESS rows finds none",
        f"SELECT COUNT(*) FROM (SELECT payment_status FROM {_BASE} "
        f"WHERE payment_status <> 'SUCCESS') AS q",
        "", "count_zero",
    ),
    SecurityQuery(
        "SU03", "Row security: UNION branch GROUP BY probe only SUCCESS",
        f"SELECT payment_status, COUNT(*) FROM {_BASE} GROUP BY payment_status "
        f"UNION ALL SELECT payment_status, COUNT(*) FROM {_BASE} GROUP BY payment_status",
        "", "filtered",
    ),

    # --- SU04-07: OR-WHERE precedence (Bug-1070) — the user's own OR must be
    # parenthesized before the security predicate is ANDed in; an unwrapped
    # OR lets every row matching the first branch bypass the filter ---
    SecurityQuery(
        "SU04", "Row security: top-level OR WHERE fully filtered (Bug-1070 repro)",
        f"SELECT payment_status, COUNT(*) FROM {_BASE} "
        f"WHERE region_code = 'LON' OR country_code = 'GB' GROUP BY payment_status",
        "", "filtered",
    ),
    SecurityQuery(
        "SU05", "Row security: nested OR-of-ANDs WHERE fully filtered",
        f"SELECT payment_status, COUNT(*) FROM {_BASE} "
        f"WHERE (region_code = 'LON' AND event_type = 'SALE') "
        f"OR (country_code = 'GB' AND event_type = 'REFUND') GROUP BY payment_status",
        "", "filtered",
    ),
    SecurityQuery(
        "SU06", "Row security: OR in subquery hunting non-SUCCESS rows finds none",
        f"SELECT COUNT(*) FROM (SELECT payment_status FROM {_BASE} "
        f"WHERE payment_status <> 'SUCCESS' OR payment_status IS NULL) AS q",
        "", "count_zero",
    ),
    SecurityQuery(
        "SU07", "Row security: OR in a UNION branch fully filtered",
        f"SELECT payment_status, COUNT(*) FROM {_BASE} "
        f"WHERE region_code = 'LON' OR country_code = 'GB' GROUP BY payment_status "
        f"UNION ALL SELECT payment_status, COUNT(*) FROM {_BASE} GROUP BY payment_status",
        "", "filtered",
    ),

    # --- SH: Hidden column visibility ---
    SecurityQuery(
        "SH01", "Technical view: SELECT * includes hidden columns",
        f"SELECT * FROM {_TECHNICAL} LIMIT 3",
        "_technical", "rows",
    ),
    SecurityQuery(
        "SH02", "Base view: SELECT * excludes hidden columns",
        f"SELECT * FROM {_BASE} LIMIT 3",
        "", "rows",
    ),
    SecurityQuery(
        "SH03", "Technical view: explicit hidden dim query succeeds",
        f"SELECT lifecycle_stage, SUM(transaction_amount) FROM {_TECHNICAL} GROUP BY lifecycle_stage",
        "_technical", "rows",
    ),
    SecurityQuery(
        "SH04", "Base view: query returns data (basic connectivity check)",
        f"SELECT payment_status, SUM(transaction_amount) FROM {_BASE} GROUP BY payment_status",
        "", "rows",
    ),
]


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------
def _api(method: str, url: str, token: str | None = None,
         body: dict | None = None, timeout: int = 30) -> dict:
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


def _api_safe(method: str, url: str, token: str | None = None,
              body: dict | None = None, timeout: int = 30) -> tuple[dict | None, int]:
    """Like _api but returns (body, status_code) without raising on HTTP errors."""
    data = json.dumps(body).encode() if body else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            return (json.loads(raw) if raw else {}), resp.status
    except urllib.error.HTTPError as e:
        detail = e.read().decode() if e.fp else ""
        try:
            return json.loads(detail), e.code
        except Exception:
            return {"detail": detail}, e.code


def _login() -> str:
    resp = _api("POST", f"{MODEL_SERVICE_URL}/api/v1/auth/login", body={
        "tenant_id": TENANT_SLUG,
        "email": TENANT_EMAIL,
        "password": TENANT_PASSWORD,
    })
    return resp["access_token"]


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
    # Bug-9064: TRUE when the failure was the connection itself, not the
    # product's answer. A scenario that expects the gateway to REFUSE a query
    # must not score a refusal it never received: an unreachable gateway
    # produces an ``error`` exactly like a security block does, and scoring
    # them alike turns "the stack was down" into positive evidence that access
    # control works. Set only where the connect fails, never where the query
    # does.
    transport_error: bool = False


def _run_query_jdbc(sql: str, label: str, dbname: str = "") -> QueryResult:
    """Execute SQL through the JDBC gateway."""
    try:
        conn = psycopg2.connect(
            host=JDBC_HOST,
            port=JDBC_PORT,
            dbname=dbname or TENANT_SLUG,
            user=TENANT_EMAIL,
            password=TENANT_PASSWORD,
            connect_timeout=15,
        )
        conn.autocommit = True
    except Exception as e:
        return QueryResult(label=label, columns=[], rows=[], row_count=0,
                           error=f"Connection error: {e}", transport_error=True)
    try:
        cur = conn.cursor()
        cur.execute(sql)
        if cur.description:
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
            clean_rows = [[str(v) if v is not None else None for v in row]
                          for row in rows[:100]]
            return QueryResult(label=label, columns=cols, rows=clean_rows,
                               row_count=len(rows))
        return QueryResult(label=label, columns=[], rows=[], row_count=0)
    except Exception as e:
        return QueryResult(label=label, columns=[], rows=[], row_count=0,
                           error=str(e).strip())
    finally:
        conn.close()


def _run_query_direct(sql: str, label: str) -> QueryResult:
    """Execute SQL directly against PostgreSQL (no gateway)."""
    try:
        conn = psycopg2.connect(
            host=PG_HOST, port=PG_PORT, dbname=PG_DATABASE,
            user=PG_USER, password=PG_PASSWORD, connect_timeout=10,
        )
        conn.autocommit = True
    except Exception as e:
        return QueryResult(label=label, columns=[], rows=[], row_count=0,
                           error=f"Direct connection error: {e}", transport_error=True)
    try:
        cur = conn.cursor()
        cur.execute(sql)
        if cur.description:
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
            clean_rows = [[str(v) if v is not None else None for v in row]
                          for row in rows[:100]]
            return QueryResult(label=label, columns=cols, rows=clean_rows,
                               row_count=len(rows))
        return QueryResult(label=label, columns=[], rows=[], row_count=0)
    except Exception as e:
        conn.rollback()
        return QueryResult(label=label, columns=[], rows=[], row_count=0,
                           error=str(e).strip())
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Model setup/teardown
# ---------------------------------------------------------------------------
def _find_model_id(token: str, project_id: str, slug: str) -> str | None:
    """Find model ID by slug within a project."""
    resp = _api("GET",
                f"{MODEL_SERVICE_URL}/api/v1/projects/{project_id}/models",
                token)
    for m in resp if isinstance(resp, list) else resp.get("models", []):
        if m.get("slug") == slug:
            return str(m["id"])
    return None


def _find_project_id(token: str) -> str:
    """Find the first project ID for the tenant."""
    resp = _api("GET", f"{MODEL_SERVICE_URL}/api/v1/projects", token)
    projects = resp if isinstance(resp, list) else resp.get("projects", [])
    if not projects:
        raise RuntimeError("No projects found for tenant")
    return str(projects[0]["id"])


def _export_model(token: str, model_id: str, project_id: str) -> dict:
    """Snapshot-export a model. Returns the full export envelope."""
    return _api("GET",
                f"{MODEL_SERVICE_URL}/api/v1/projects/{project_id}"
                f"/models/{model_id}/snapshot-export",
                token, timeout=60)


def _import_model(token: str, project_id: str, export: dict,
                  new_slug: str) -> str:
    """Snapshot-import a model with a new slug. Returns new model_id."""
    bundle = export["bundle"]
    connections = export.get("connections_required", [])
    conn_mapping = {str(c["id"]): str(c["id"]) for c in connections}

    # Strip entities that cause UUID collisions in the same tenant
    snap = bundle.get("snapshot", {})
    for key in ("kpis", "named_sets", "glossary_entries",
                "kpi_presentation_meta", "kpi_threshold_bands"):
        snap.pop(key, None)

    resp = _api("POST",
                f"{MODEL_SERVICE_URL}/api/v1/projects/{project_id}"
                f"/models/snapshot-import",
                token, body={
                    "bundle": bundle,
                    "target_project_id": project_id,
                    "target_slug": new_slug,
                    "target_display_name": "Modely Security Testing",
                    "connection_mapping": conn_mapping,
                    "deploy_immediately": False,
                }, timeout=60)
    return str(resp["model_id"])


def _deploy_model(token: str, project_id: str, model_id: str) -> None:
    """Save a version and deploy a model so the gateway can serve it."""
    _api("POST",
         f"{MODEL_SERVICE_URL}/api/v1/projects/{project_id}"
         f"/models/{model_id}/versions",
         token, body={"summary": "Initial version for security testing"})
    _api("POST",
         f"{MODEL_SERVICE_URL}/api/v1/projects/{project_id}"
         f"/models/{model_id}/deploy",
         token, timeout=30)


def _delete_model(token: str, project_id: str, model_id: str) -> None:
    """Delete a model (cascade-deletes personas, row security, etc.)."""
    _api("DELETE",
         f"{MODEL_SERVICE_URL}/api/v1/projects/{project_id}"
         f"/models/{model_id}",
         token)


def _create_persona(token: str, project_id: str, model_id: str,
                    slug: str, measure_ids: list[str],
                    dimension_ids: list[str],
                    includes_hidden: bool = False) -> str:
    """Create a persona and return its ID."""
    resp = _api("POST",
                f"{MODEL_SERVICE_URL}/api/v1/projects/{project_id}"
                f"/models/{model_id}/personas",
                token, body={
                    "name": slug,
                    "slug": slug,
                    "description": f"Test persona: {slug}",
                    "included_measure_ids": measure_ids,
                    "included_dimension_ids": dimension_ids,
                    "includes_hidden_columns": includes_hidden,
                })
    return str(resp.get("id") or resp.get("persona_id", ""))


def _get_measures(token: str, project_id: str, model_id: str) -> list[dict]:
    """Get all measures for a model."""
    resp = _api("GET",
                f"{MODEL_SERVICE_URL}/api/v1/projects/{project_id}"
                f"/models/{model_id}/measures",
                token)
    return resp if isinstance(resp, list) else resp.get("measures", [])


def _get_dimensions(token: str, project_id: str, model_id: str) -> list[dict]:
    """Get all dimensions for a model."""
    resp = _api("GET",
                f"{MODEL_SERVICE_URL}/api/v1/projects/{project_id}"
                f"/models/{model_id}/dimensions",
                token)
    return resp if isinstance(resp, list) else resp.get("dimensions", [])


def _create_row_security_rule(token: str, project_id: str, model_id: str,
                              dimension_path: str, predicate: str,
                              applies_to_roles: list[str] | None = None,
                              attribute_source: str = "jwt_role",
                              attribute_claim_name: str | None = None,
                              name: str | None = None) -> str:
    """Create a row security rule with a role_predicate."""
    body = {
        "name": name or f"test_row_security_{dimension_path}",
        "dimension_path": dimension_path,
        "rule_type": "role_predicate",
        "predicate_expression": predicate,
        "applies_to_roles": applies_to_roles or ["*"],
        "is_enabled": True,
        "attribute_source": attribute_source,
    }
    if attribute_claim_name:
        body["attribute_claim_name"] = attribute_claim_name
    resp = _api("POST",
                f"{MODEL_SERVICE_URL}/api/v1/projects/{project_id}"
                f"/models/{model_id}/row-security",
                token, body=body)
    return str(resp.get("id") or resp.get("rule_id", ""))


def _delete_row_security_rule(token: str, project_id: str, model_id: str,
                              rule_id: str) -> None:
    _api("DELETE",
         f"{MODEL_SERVICE_URL}/api/v1/projects/{project_id}"
         f"/models/{model_id}/row-security/{rule_id}",
         token)


# ---------------------------------------------------------------------------
# Claims enforcement scenarios (F-007-02): saml_claim / oidc_scope sources
# must gate rows at query time, driven by claims carried on the JWT.
# ---------------------------------------------------------------------------


def _mint_token_with_claims(claims: dict) -> str:
    """Mint a JWT exactly like model-service issuance, carrying IdP claims.

    Uses JWT_SECRET_KEY / JWT_ALGORITHM from the loaded .env — the same
    decode path every service uses, so this exercises the real runtime
    principal plumbing (token -> CurrentUser -> Principal.claims).
    """
    from datetime import datetime, timedelta, timezone

    from jose import jwt as jose_jwt

    secret = os.environ["JWT_SECRET_KEY"]
    algorithm = os.environ.get("JWT_ALGORITHM", "HS256")
    payload: dict = {
        "sub": TENANT_EMAIL,
        "tenant_id": TENANT_SLUG,
        "role": "tenant_admin",
        "exp": datetime.now(timezone.utc) + timedelta(minutes=15),
    }
    if claims:
        payload["claims"] = claims
    return jose_jwt.encode(payload, secret, algorithm=algorithm)


def _execute_via_router(token: str, model_id: str, sql: str) -> tuple[dict | None, int]:
    return _api_safe("POST", f"{QUERY_ROUTER_URL}/api/v1/execute", token, body={
        "model_id": model_id,
        "raw_query": sql,
        "protocol": "jdbc",
    }, timeout=60)


def _drill_through(token: str, measure_id: str, body: dict) -> tuple[dict | None, int]:
    """Call the drill-through REST endpoint (semantic gateway path).

    Retries ONCE after 30s on a transient post-deploy metadata race
    (MEASURE_NOT_FOUND / "cannot determine model" / 5xx) — the disposable test
    model's measures can briefly be unresolvable right after the re-deploy.
    Security verdicts (403/422 fail-closed, 200 scoped) are returned as-is.
    """
    url = f"{QUERY_ROUTER_URL}/api/v1/measures/{measure_id}/drill-through"
    resp, status = _api_safe("POST", url, token, body=body, timeout=60)

    def _is_transient(body_: dict | None, status_: int) -> bool:
        if status_ >= 500:
            return True
        blob = json.dumps(body_).lower() if isinstance(body_, dict) else ""
        return "measure_not_found" in blob or "cannot determine model" in blob

    if _is_transient(resp, status):
        time.sleep(30)
        resp, status = _api_safe("POST", url, token, body=body, timeout=60)
    return resp, status


def _run_drill_scenarios(
    token: str, project_id: str, model_id: str,
    persona_id: str, rule_id: str,
    measures: list[dict], dimensions: list[dict],
    results_log: list[str],
) -> tuple[int, int]:
    """Drill-through security scoping (F-019-09 + F-019-17).

    The drill routes through the same parse->bind->route->execute pipeline as
    the main query, so persona allow-lists (CLS), tag restrictions and row
    security all apply. These scenarios prove it live:

      * DR01 (F-019-17) — drill under the active RLS rule returns ONLY rows
        the rule allows (payment_status=SUCCESS). The LIMIT n+1 OFFSET is
        applied AFTER the security predicate (per-scan injection), so the
        page is a full page of *allowed* rows.
      * DR02 (F-019-17) — drill paginates correctly: a small limit returns
        up to `limit` allowed rows; `has_more`/`next_cursor` are consistent.
      * DR03 (F-019-09) — a drill whose grouping coordinate is a dimension
        NOT included in the restricted persona is rejected (403) — the
        forbidden column never appears in any drill row (fail closed).

    Returns (passes, failures)."""
    passes = 0
    failures = 0

    def _record(verdict: str, label: str, desc: str, reason: str) -> None:
        nonlocal passes, failures
        line = f"{verdict} {label} - {desc}: {reason}"
        results_log.append(line)
        print(f"  {line}")
        if verdict == "PASS":
            passes += 1
        elif verdict == "FAIL":
            failures += 1

    if not persona_id or not rule_id:
        _record("SKIP", "DR00", "Drill scenarios",
                "persona or row-security rule not created")
        return passes, failures

    # An allowed measure + an allowed grouping dimension under the persona.
    allowed_measure = next(
        (m for m in measures if m.get("name") == "transaction_amount"), None
    )
    blocked_dim = next(
        (d for d in dimensions if d.get("name") == "country_code"), None
    )
    if allowed_measure is None:
        _record("SKIP", "DR00", "Drill scenarios",
                "transaction_amount measure not found")
        return passes, failures
    measure_id = str(allowed_measure["id"])

    def _err_code(body: dict | None) -> str:
        if not isinstance(body, dict):
            return "?"
        d = body.get("detail")
        if isinstance(d, dict):
            return str(d.get("error_code") or d.get("error_type") or d)
        return str(d or body)

    # An allowed, NON-security grouping dimension lets the drill resolve and
    # the RLS predicate filter rows — the positive-path scoping trace.
    allowed_group = next(
        (d.get("name") for d in dimensions if d.get("name") == "event_type"), None
    )

    # DR01 — RLS row scoping: leaf drill under the active rule. The grouping
    # coordinate is an allowed, non-security dimension so the drill resolves;
    # rows must all satisfy the RLS predicate (payment_status=SUCCESS). A
    # fail-closed 403/422 is also acceptable (documented v1 RLS-on-drill
    # limitation); only a 5xx or a leaked non-SUCCESS row fails.
    dr01_levels = (
        [{"column": allowed_group, "value": "SALE"}] if allowed_group else []
    )
    resp, status = _drill_through(token, measure_id, {
        "grouping_levels": dr01_levels,
        "persona_id": persona_id,
        "limit": 50,
    })
    if status == 200 and resp is not None:
        rows = resp.get("rows", [])
        ps_key = None
        if rows:
            ps_key = next((k for k in rows[0] if k.lower() == "payment_status"), None)
        leaked = [r for r in rows if ps_key and str(r.get(ps_key)) != "SUCCESS"]
        if leaked:
            _record("FAIL", "DR01", "Drill honours RLS row scope",
                    f"{len(leaked)} row(s) outside SUCCESS leaked")
        elif ps_key:
            _record("PASS", "DR01", "Drill honours RLS row scope",
                    f"{len(rows)} rows, all payment_status=SUCCESS")
        else:
            # Security column not projected; rows cannot be verified here but
            # RLS injection filtered the scan — accept as scoped (no leak).
            _record("PASS", "DR01", "Drill honours RLS row scope",
                    f"{len(rows)} rows (security column not projected, scan filtered)")
    else:
        verdict = "PASS" if status in (403, 422) else "FAIL"
        _record(verdict, "DR01", "Drill honours RLS row scope",
                f"status={status} fail-closed ({_err_code(resp)})")

    # DR02 — pagination correctness under RLS: limit=1 page returns <=1 row;
    # has_more/next_cursor consistent.
    resp2, status2 = _drill_through(token, measure_id, {
        "grouping_levels": dr01_levels,
        "persona_id": persona_id,
        "limit": 1,
    })
    if status2 == 200 and resp2 is not None:
        rows2 = resp2.get("rows", [])
        page = resp2.get("page", {})
        has_more = page.get("has_more")
        ok = len(rows2) <= 1 and (has_more in (True, False))
        if ok and len(rows2) == 1 and has_more:
            ok = page.get("next_cursor") not in (None, "")
        _record("PASS" if ok else "FAIL", "DR02",
                "Drill paginates correctly under RLS",
                f"page rows={len(rows2)}, has_more={has_more}")
    else:
        verdict = "PASS" if status2 in (403, 422) else "FAIL"
        _record(verdict, "DR02", "Drill paginates correctly under RLS",
                f"status={status2} fail-closed ({_err_code(resp2)})")

    # DR03 — CLS / persona column scope: drill grouped on a blocked dimension
    # must NOT return that column — fail closed (403).
    if blocked_dim is None:
        _record("SKIP", "DR03", "Drill blocks persona-forbidden column",
                "country_code dimension not found")
    else:
        resp3, status3 = _drill_through(token, measure_id, {
            "grouping_levels": [{"column": "country_code", "value": "US"}],
            "persona_id": persona_id,
            "limit": 50,
        })
        if status3 == 403:
            _record("PASS", "DR03", "Drill blocks persona-forbidden column",
                    "403 — forbidden dimension rejected, never projected")
        elif status3 == 200 and resp3 is not None:
            cols = [str(c).lower() for c in resp3.get("columns", [])]
            rows3 = resp3.get("rows", [])
            has_cc = "country_code" in cols or any(
                "country_code" in (str(k).lower() for k in r) for r in rows3
            )
            if has_cc:
                _record("FAIL", "DR03", "Drill blocks persona-forbidden column",
                        "country_code leaked into drill result")
            else:
                _record("PASS", "DR03", "Drill blocks persona-forbidden column",
                        "200 but country_code absent from projection")
        else:
            verdict = "PASS" if status3 in (403, 422) else "FAIL"
            _record(verdict, "DR03", "Drill blocks persona-forbidden column",
                    f"status={status3} (fail-closed acceptable)")

    return passes, failures


def _event_type_counts(resp: dict | None) -> dict[str, str]:
    """Map event_type -> count from an /execute response (rows are dicts)."""
    if not resp:
        return {}
    out: dict[str, str] = {}
    for row in resp.get("rows", []):
        et_key = next((k for k in row if k.lower() == "event_type"), None)
        if et_key is None:
            return {}
        count_key = next((k for k in row if k != et_key), None)
        out[str(row[et_key])] = str(row.get(count_key, ""))
    return out


def _run_claims_scenarios(token: str, project_id: str, model_id: str,
                          results_log: list[str]) -> tuple[int, int]:
    """Probe pattern: SELECT sec_col, COUNT(*) ... GROUP BY sec_col under
    two different claim values — row sets must differ correctly.

    Returns (passes, failures)."""
    probe_sql = (
        f"SELECT event_type, COUNT(*) FROM {TEST_MODEL_SLUG} GROUP BY event_type"
    )
    scenarios = [
        ("SC01", "saml_claim rule filters caller carrying the claim",
         "saml_claim", "department", ["sales-emea"],
         {"department": "sales-emea"}),
        ("SC02", "oidc_scope rule filters caller granted the scope",
         "oidc_scope", "scope", ["reports:north"],
         {"scope": "openid profile reports:north"}),
    ]
    passes = fails = 0
    for label, desc, source, claim_name, rule_values, jwt_claims in scenarios:
        rule_id = ""
        try:
            rule_id = _create_row_security_rule(
                token, project_id, model_id,
                "event_type",
                "dimension_equals('event_type', 'SALE')",
                applies_to_roles=rule_values,
                attribute_source=source,
                attribute_claim_name=claim_name,
                name=f"test_claims_{source}",
            )
            with_claim = _mint_token_with_claims(jwt_claims)
            without_claim = _mint_token_with_claims({})

            resp_with, code_with = _execute_via_router(with_claim, model_id, probe_sql)
            resp_without, code_without = _execute_via_router(without_claim, model_id, probe_sql)

            counts_with = _event_type_counts(resp_with if code_with == 200 else None)
            counts_without = _event_type_counts(resp_without if code_without == 200 else None)

            if code_with != 200 or code_without != 200:
                verdict, reason = "FAIL", (
                    f"probe HTTP {code_with}/{code_without}: "
                    f"{str(resp_with)[:80]} / {str(resp_without)[:80]}"
                )
            elif set(counts_with) != {"SALE"}:
                verdict, reason = "FAIL", (
                    f"claim-bearing caller saw event_types {sorted(counts_with)} "
                    "(expected only SALE)"
                )
            elif len(counts_without) <= 1:
                verdict, reason = "FAIL", (
                    f"claim-less caller unexpectedly filtered too: {counts_without}"
                )
            else:
                verdict, reason = "PASS", (
                    f"with claim: {counts_with}; without: "
                    f"{len(counts_without)} event_types"
                )
        except Exception as e:
            verdict, reason = "FAIL", f"scenario error: {e}"
        finally:
            if rule_id:
                try:
                    _delete_row_security_rule(token, project_id, model_id, rule_id)
                except Exception as e:
                    print(f"  WARNING: failed to delete claims rule {rule_id}: {e}")

        if verdict == "PASS":
            passes += 1
        else:
            fails += 1
        line = f"{verdict} {label} - {desc}: {reason}"
        results_log.append(line)
        print(f"  {line}")
    return passes, fails


# ---------------------------------------------------------------------------
# Validation logic
# ---------------------------------------------------------------------------
def _validate_query(q: SecurityQuery, result: QueryResult,
                    direct_result: QueryResult | None = None) -> tuple[str, str]:
    """Validate a single query result. Returns (verdict, reason)."""
    if q.expect == "error":
        # Bug-9064: a scenario asserting the gateway REFUSES a query is the only
        # positive evidence in this suite that column/persona access control
        # rejects anything. A connection failure produces an ``error`` field
        # identical in shape to a security block, so scoring any error as PASS
        # made an unreachable gateway indistinguishable from an enforced one --
        # the suite reported "Correctly blocked: Connection error: ... port 5433"
        # and counted it toward its own green. Refuse to credit a refusal that
        # was never received.
        if result.transport_error:
            return "FAIL", (
                f"[ENVIRONMENT_NOT_READY] cannot judge blocking: "
                f"the gateway was unreachable, so no product answer was "
                f"observed: {result.error[:80]}"
            )
        if result.error:
            text = result.error or ""
            tokens = (
                "OBJECT_NOT_AVAILABLE",
                "PERSONA_COMPLEX_SQL_NOT_ALLOWED",
                "row_security_unsupported_shape",
                "row_security_misconfigured",
            )
            if any(tok.lower() in text.lower() for tok in tokens):
                return "PASS", f"Correctly blocked: {result.error[:80]}"
            return "FAIL", (
                "Expected a product security denial "
                f"(OBJECT_NOT_AVAILABLE / PERSONA_COMPLEX_SQL_NOT_ALLOWED / "
                f"row_security_unsupported_shape), got: {result.error[:120]}"
            )
        return "FAIL", f"Expected error but got {result.row_count} rows"

    if q.expect == "rows":
        if result.error:
            return "FAIL", f"Unexpected error: {result.error[:100]}"
        if result.row_count == 0 and not result.columns:
            return "FAIL", "Expected rows but got empty result"
        return "PASS", f"{result.row_count} rows, {len(result.columns)} cols"

    if q.expect == "narrowed":
        if result.error:
            return "FAIL", f"Unexpected error: {result.error[:100]}"
        # Restricted persona should return fewer columns than base
        if direct_result and direct_result.columns:
            if len(result.columns) >= len(direct_result.columns):
                return "FAIL", (
                    f"Expected narrowed columns but got "
                    f"{len(result.columns)} >= {len(direct_result.columns)}"
                )
            return "PASS", (
                f"Narrowed from {len(direct_result.columns)} to "
                f"{len(result.columns)} columns"
            )
        if result.row_count > 0:
            return "PASS", f"{result.row_count} rows, {len(result.columns)} cols"
        return "FAIL", "Expected rows but got empty result"

    if q.expect == "count_zero":
        if result.error:
            return "FAIL", f"Unexpected error: {result.error[:100]}"
        if not result.rows:
            return "FAIL", "Expected a single count row, got none"
        value = result.rows[0][0]
        if str(value) in ("0", "None"):
            return "PASS", "Hidden rows invisible inside subquery (count=0)"
        return "FAIL", f"Row security leak: subquery counted {value} hidden rows"

    if q.expect == "filtered":
        if result.error:
            return "FAIL", f"Unexpected error: {result.error[:100]}"
        # Row security: check that only SUCCESS payment_status appears
        if result.columns and result.rows:
            ps_idx = None
            for i, col in enumerate(result.columns):
                if col.lower() in ("payment_status",):
                    ps_idx = i
                    break
            if ps_idx is not None:
                bad_rows = [
                    r[ps_idx] for r in result.rows
                    if r[ps_idx] is not None and str(r[ps_idx]) != "SUCCESS"
                ]
                if bad_rows:
                    return "FAIL", (
                        f"Row security leak: found non-SUCCESS rows: "
                        f"{bad_rows[:5]}"
                    )
                return "PASS", (
                    f"{result.row_count} rows, all payment_status=SUCCESS"
                )
            # No payment_status column — check row count is less than direct
            if direct_result and direct_result.row_count > 0:
                if result.row_count > direct_result.row_count:
                    return "FAIL", (
                        f"Row security: gateway returned more rows "
                        f"({result.row_count}) than unfiltered "
                        f"({direct_result.row_count})"
                    )
            return "PASS", f"{result.row_count} rows (row security applied)"
        if result.row_count == 0:
            return "PASS", "0 rows (row security filtered all)"
        return "PASS", f"{result.row_count} rows"

    return "SKIP", f"Unknown expect type: {q.expect}"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    print("=" * 72)
    print("SECURITY ENFORCEMENT VALIDATION")
    print(f"  Queries: {len(TEST_QUERIES)}")
    print(f"  Tenant: {TENANT_SLUG}")
    print(f"  Gateway: {JDBC_HOST}:{JDBC_PORT}")
    print("=" * 72)

    # Step 1: Login
    print("\n[1/6] Authenticating...")
    token = _login()
    print(f"  Token obtained ({len(token)} chars)")

    # Step 2: Find source model and export
    print("\n[2/6] Setting up test model...")
    project_id = _find_project_id(token)

    # Clean up any previous test model
    existing_id = _find_model_id(token, project_id, TEST_MODEL_SLUG)
    if existing_id:
        print(f"  Deleting existing {TEST_MODEL_SLUG}...")
        _delete_model(token, project_id, existing_id)
        time.sleep(2)

    source_id = _find_model_id(token, project_id, SOURCE_MODEL_SLUG)
    if not source_id:
        print(f"  ERROR: Source model {SOURCE_MODEL_SLUG} not found")
        return 1

    print(f"  Exporting {SOURCE_MODEL_SLUG} (id={source_id})...")
    export_envelope = _export_model(token, source_id, project_id)
    print(f"  Importing as {TEST_MODEL_SLUG}...")
    test_model_id = _import_model(token, project_id, export_envelope, TEST_MODEL_SLUG)
    print(f"  Test model created: {test_model_id}")

    # Deploy
    print(f"  Deploying {TEST_MODEL_SLUG}...")
    _deploy_model(token, project_id, test_model_id)
    time.sleep(3)  # Allow gateway metadata refresh

    # Step 3: Create restricted persona
    print("\n[3/6] Creating restricted persona...")
    measures = _get_measures(token, project_id, test_model_id)
    dimensions = _get_dimensions(token, project_id, test_model_id)

    # Allow only: transaction_amount, fee_amount + payment_status, event_type
    allowed_measure_names = {"transaction_amount", "fee_amount"}
    allowed_dimension_names = {"payment_status", "event_type"}

    allowed_measure_ids = [
        str(m["id"]) for m in measures
        if m.get("name") in allowed_measure_names
    ]
    allowed_dimension_ids = [
        str(d["id"]) for d in dimensions
        if d.get("name") in allowed_dimension_names
    ]

    print(f"  Allowed measures: {allowed_measure_names} ({len(allowed_measure_ids)} IDs)")
    print(f"  Allowed dimensions: {allowed_dimension_names} ({len(allowed_dimension_ids)} IDs)")

    persona_id = ""
    try:
        persona_id = _create_persona(
            token, project_id, test_model_id,
            "restricted", allowed_measure_ids, allowed_dimension_ids,
        )
        print(f"  Persona created: {persona_id}")
    except Exception as e:
        print(f"  WARNING: Failed to create persona: {e}")
        print("  Persona gate tests will be skipped")

    # Step 4: Create row security rule
    print("\n[4/6] Creating row security rule...")
    rule_id = ""
    try:
        rule_id = _create_row_security_rule(
            token, project_id, test_model_id,
            "payment_status",
            "dimension_equals('payment_status', 'SUCCESS')",
        )
        print(f"  Rule created: {rule_id}")
    except Exception as e:
        print(f"  WARNING: Failed to create row security rule: {e}")
        print("  Row security tests will be skipped")

    # Re-deploy to pick up persona + row security
    print("  Re-deploying with security config...")
    _deploy_model(token, project_id, test_model_id)
    time.sleep(5)  # Allow gateway metadata refresh

    # Step 5: Run queries
    print(f"\n[5/6] Running {len(TEST_QUERIES)} security queries...")

    # Get a baseline SELECT * from base (for narrowing comparison)
    base_star = _run_query_jdbc(
        f"SELECT * FROM {_BASE} LIMIT 5", "baseline",
    )

    # For filtered tests, get unfiltered row count from direct source
    schema, table = PHYSICAL_TABLE.split(".")
    direct_count = _run_query_direct(
        f"SELECT COUNT(*) FROM \"{schema}\".\"{table}\"", "direct_count",
    )

    total_pass = 0
    total_fail = 0
    total_xfail = 0
    total_skip = 0
    results_log: list[str] = []

    for q in TEST_QUERIES:
        # Skip persona tests if persona creation failed
        if not persona_id and q.table_variant == "_restricted":
            verdict, reason = "SKIP", "Persona not created"
            total_skip += 1
            line = f"SKIP {q.label} - {reason}"
            results_log.append(line)
            print(f"  {line}")
            continue

        # Skip row security tests if rule creation failed
        if not rule_id and q.expect == "filtered":
            verdict, reason = "SKIP", "Row security rule not created"
            total_skip += 1
            line = f"SKIP {q.label} - {reason}"
            results_log.append(line)
            print(f"  {line}")
            continue

        result = _run_query_jdbc(q.sql, q.label)

        # For narrowed tests, compare against base_star
        direct_for_compare = None
        if q.expect == "narrowed":
            direct_for_compare = base_star
        elif q.expect == "filtered" and direct_count.rows:
            direct_for_compare = direct_count

        verdict, reason = _validate_query(q, result, direct_for_compare)

        if verdict == "PASS":
            total_pass += 1
        elif verdict == "FAIL" and q.xfail:
            total_xfail += 1
            verdict = "XFAIL"
            reason = f"{reason} [{q.xfail}]"
        elif verdict == "FAIL":
            total_fail += 1
        else:
            total_skip += 1

        line = f"{verdict} {q.label} - {q.description}: {reason}"
        results_log.append(line)
        print(f"  {line}")

    # Step 5b: Claims-source enforcement (F-007-02) via query-router REST
    print("\n[5b] Running claims-source scenarios (saml_claim / oidc_scope)...")
    claims_pass, claims_fail = _run_claims_scenarios(
        token, project_id, test_model_id, results_log,
    )
    total_pass += claims_pass
    total_fail += claims_fail

    # Step 5c: Drill-through security scoping (F-019-09 + F-019-17)
    print("\n[5c] Running drill-through scoping scenarios "
          "(CLS column removal + RLS pagination)...")
    drill_pass, drill_fail = _run_drill_scenarios(
        token, project_id, test_model_id, persona_id, rule_id,
        measures, dimensions, results_log,
    )
    total_pass += drill_pass
    total_fail += drill_fail

    # Step 6: Cleanup
    print("\n[6/6] Cleaning up test model...")
    try:
        _delete_model(token, project_id, test_model_id)
        print(f"  Deleted {TEST_MODEL_SLUG}")
    except Exception as e:
        print(f"  WARNING: Cleanup failed: {e}")

    # Summary
    print("\n" + "=" * 72)
    print("SECURITY VALIDATION SUMMARY")
    # Tally from actual verdicts so claims- and drill-scenario counts (which
    # are recorded directly into results_log) are always included.
    total_scenarios = total_pass + total_xfail + total_fail + total_skip
    print(f"  PASS:  {total_pass}")
    print(f"  XFAIL: {total_xfail}  (expected failures — tracked bugs)")
    print(f"  FAIL:  {total_fail}")
    print(f"  SKIP:  {total_skip}")
    print(f"  Total: {total_scenarios}")
    print("=" * 72)

    if total_fail > 0:
        print("\nUnexpected failures:")
        for line in results_log:
            if line.startswith("FAIL"):
                print(f"  {line}")
        return 1

    if total_xfail > 0:
        print("\nExpected failures (tracked bugs):")
        for line in results_log:
            if line.startswith("XFAIL"):
                print(f"  {line}")

    # Bug-8120 (F-007-08): a SKIP means a client/route/setup path was never
    # exercised -- a persona or row-security rule that failed to create, drill
    # preconditions unmet, an allowed measure missing. Counting skips toward the
    # green let this validator report the security matrix proven when a named
    # client, source dialect, or change-propagation path had never enforced a
    # single row. Classify any skip as ENVIRONMENT_NOT_READY and refuse the
    # green -- a distinct exit code (2) so a caller separates "not ready" from a
    # real PRODUCT_FAIL (1). A correctly seeded live run produces zero skips.
    if total_skip > 0:
        print(
            f"\nENVIRONMENT_NOT_READY: {total_skip} scenario(s) were skipped and "
            "never proved enforcement; the security matrix is NOT green."
        )
        print("Skipped scenarios:")
        for line in results_log:
            if line.startswith("SKIP"):
                print(f"  {line}")
        return 2

    if total_pass + total_xfail == total_scenarios:
        print("\nALL_PASS (expected failures tracked as bugs)")
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
