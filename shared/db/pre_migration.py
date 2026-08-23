"""Missing-table detection for pre-migration bootstrap paths (Bug-9552).

Some privileged flows — system-admin login, login lockout, system audit —
touch tables created by later migrations. A system whose schema is behind
those migrations (the migrate-then-serve deploy gate, first-install
post-deploy, or a stale environment) must still be able to authenticate so
migrations can run. Callers use this predicate to fail OPEN on an
undefined-table error and to re-raise everything else.
"""
from __future__ import annotations

# Postgres SQLSTATE for undefined_table. asyncpg exposes it as `.sqlstate`
# on the wrapped driver exception (SQLAlchemy `.orig`).
_MISSING_TABLE_SQLSTATE = "42P01"


def is_missing_table_error(exc: BaseException) -> bool:
    """True only for undefined-table errors (a schema-behind system)."""
    orig = getattr(exc, "orig", None) or exc
    sqlstate = getattr(orig, "sqlstate", None)
    if sqlstate is not None:
        return str(sqlstate) == _MISSING_TABLE_SQLSTATE
    cls = type(orig).__name__
    if "UndefinedTable" in cls:
        return True
    # SQLite (test environments): no such table: <name>
    return "no such table" in str(orig).lower()
