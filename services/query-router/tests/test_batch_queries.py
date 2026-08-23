"""
Batch SQL query integration test — JDBC gateway.

Parses SQL files containing labelled queries (``-- QXXX description``)
and sends each through the JDBC gateway (port 5433) via psycopg2.
Reports pass/fail for each query.

Requires a running JDBC gateway container on localhost:5433 and a seeded
tenant (default: acme-demo).

Usage::

    # Run all batch queries (requires Docker services up)
    cd tessallite/services/query-router
    set -a && source ../../.env && set +a
    pytest tests/test_batch_queries.py -m e2e -v

    # Run a single query by label
    pytest tests/test_batch_queries.py -m e2e -k "Q13.01" -v

    # Run against a custom SQL file
    BATCH_SQL=path/to/queries.sql pytest tests/test_batch_queries.py -m e2e -v

Query file format::

    -- Q11.10 CASE expression with string mapping
    SELECT payment_id, ...
    FROM modely
    LIMIT 50;

    -- Q11.11 COALESCE with fallback
    SELECT ...;

Each query is preceded by ``-- QXXX label`` on its own line.
"""
from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Load .env for credentials (same pattern as test_query_set1.py)
# ---------------------------------------------------------------------------

_ENV_FILE = Path(__file__).resolve().parents[3] / ".env"
if _ENV_FILE.exists():
    for line in _ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

try:
    import psycopg2
except ImportError:
    psycopg2 = None


# ---------------------------------------------------------------------------
# Configuration — override via environment variables
# ---------------------------------------------------------------------------

JDBC_HOST = os.environ.get("GATEWAY_JDBC_HOST", "localhost")
JDBC_PORT = int(os.environ.get("GATEWAY_JDBC_PORT", "5433"))
JDBC_SSLMODE = os.environ.get("GATEWAY_JDBC_SSLMODE", "prefer").strip() or None
TENANT_SLUG = os.environ.get("BATCH_TENANT_SLUG", "acme-demo")
TENANT_EMAIL = os.environ.get("BATCH_TENANT_EMAIL", "admin@acme-demo.com")
TENANT_PASSWORD = os.environ.get("BATCH_TENANT_PASSWORD", "acme-demo")
SQL_FILE = os.environ.get(
    "BATCH_SQL",
    str(Path(__file__).parent / "sql_test_queries.sql"),
)
TIMEOUT_S = int(os.environ.get("BATCH_TIMEOUT", "30"))


# ---------------------------------------------------------------------------
# Query loader
# ---------------------------------------------------------------------------

@dataclass
class SQLQuery:
    label: str
    description: str
    sql: str


def load_sql_queries(path: str) -> list[SQLQuery]:
    """Parse a SQL file into labelled query objects.

    Expected format: ``-- QXXX description`` followed by SQL ending with ``;``.
    """
    with open(path) as f:
        content = f.read()

    # Split on label lines
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
            # Remove trailing semicolon and leading/trailing whitespace
            sql = sql_block.rstrip(";").strip()
            # Skip empty or comment-only blocks
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
# Gateway connectivity check
# ---------------------------------------------------------------------------

# ``tessallite/tests`` is not a package; reach the shared suite gate by path so
# there is ONE definition of the three-state readiness taxonomy in the repo.
sys.path.append(str(Path(__file__).resolve().parents[3] / "tests"))
from suite_gate import Readiness, classify_jdbc_readiness  # noqa: E402


def _gateway_readiness():
    """Classify the gateway into STACK_ABSENT / ENVIRONMENT_NOT_READY / READY.

    Bug-8532: this used to be ``_gateway_available() -> bool`` wrapping the
    whole psycopg2 connect in ``except Exception: return False`` and feeding a
    module-level ``skipif``. Every distinguishable cause -- port unreachable,
    wedged accept loop, wrong credentials, an expired tenant password, a
    missing driver -- collapsed into the same silent skip, so all 174
    ``test_batch_query`` cases vanished from the run while the summary stayed
    green. Two runs of the same command over the same 3678 collected tests
    produced "3501 passed, 176 skipped" and "22 failed, 3646 passed" on the
    same day; only the totals matching revealed that 174 tests had disappeared
    rather than passed.

    The verdict now carries the real error text, and only a genuinely absent
    stack may skip.
    """
    return classify_jdbc_readiness(
        JDBC_HOST,
        JDBC_PORT,
        database=TENANT_SLUG,
        user=TENANT_EMAIL,
        password=TENANT_PASSWORD,
        timeout=5.0,
        sslmode=JDBC_SSLMODE,
    )


_READINESS = _gateway_readiness()


# ---------------------------------------------------------------------------
# Markers and skip conditions
# ---------------------------------------------------------------------------

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        _READINESS.readiness is Readiness.STACK_ABSENT,
        reason=(
            f"[{Readiness.STACK_ABSENT.value}] no JDBC gateway at "
            f"{JDBC_HOST}:{JDBC_PORT}: {_READINESS.detail}"
        ),
    ),
]

# Physical JOINs to dimension tables — blocked on business view by design.
_XFAIL_JOINS_BLOCKED = {
    f"Q12.{i:02d}" for i in range(1, 8)
}

_XFAIL_MAP: dict[str, str] = {}
for qid in _XFAIL_JOINS_BLOCKED:
    _XFAIL_MAP[qid] = "By design: business view does not expose physical table JOINs"


# ---------------------------------------------------------------------------
# JDBC fixture
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def jdbc():
    """Module-scoped psycopg2 connection to the JDBC gateway.

    Bug-8532: a failure here is NOT a skip. The module-level marker already
    took the only defensible skip (nothing is listening at all). Anything that
    reaches this point has a live accept loop, so a failure means the stack is
    present and broken -- ENVIRONMENT_NOT_READY -- and must be loud, with the
    driver's own message attached.
    """
    if _READINESS.readiness is Readiness.ENVIRONMENT_NOT_READY:
        pytest.fail(
            f"[{Readiness.ENVIRONMENT_NOT_READY.value}] {_READINESS.detail}"
        )
    try:
        conn = psycopg2.connect(
            host=JDBC_HOST,
            port=JDBC_PORT,
            database=TENANT_SLUG,
            user=TENANT_EMAIL,
            password=TENANT_PASSWORD,
            connect_timeout=TIMEOUT_S,
            sslmode=JDBC_SSLMODE,
        )
        conn.autocommit = True
    except Exception as exc:
        pytest.fail(
            f"[{Readiness.ENVIRONMENT_NOT_READY.value}] the JDBC accept loop at "
            f"{JDBC_HOST}:{JDBC_PORT} answered the readiness probe but opening "
            f"the suite's session failed: {type(exc).__name__}: {exc}"
        )
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# Parametrised test
# ---------------------------------------------------------------------------

_QUERIES = load_sql_queries(SQL_FILE) if os.path.exists(SQL_FILE) else []


def _make_params():
    """Build parametrize args with xfail markers for known-blocked queries."""
    params = []
    for q in _QUERIES:
        reason = _XFAIL_MAP.get(q.label)
        if reason:
            params.append(pytest.param(
                q, id=q.label,
                marks=pytest.mark.xfail(
                    reason=reason,
                    raises=(psycopg2.Error if psycopg2 else Exception,),
                    strict=True,
                ),
            ))
        else:
            params.append(pytest.param(q, id=q.label))
    return params


@pytest.mark.parametrize("query", _make_params())
def test_batch_query(query: SQLQuery, jdbc):
    """Execute a SQL query against the live JDBC gateway and assert success.

    For xfail-marked queries the psycopg2 exception must propagate so
    pytest's xfail machinery can match it.  For normal queries we convert
    to pytest.fail for a readable message.
    """
    is_xfail = query.label in _XFAIL_MAP
    cur = jdbc.cursor()
    try:
        cur.execute(query.sql)
        if cur.description:
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
            assert len(cols) > 0, f"{query.label}: no columns returned"
            assert len(rows) >= 0, f"{query.label}: negative row count"
        # No description = DDL/utility statement — still a pass
    except psycopg2.Error as e:
        if is_xfail:
            raise  # let xfail detect the expected failure
        pytest.fail(
            f"{query.label} ({query.description}): "
            f"{e.pgerror or str(e)}"
        )
    finally:
        cur.close()
