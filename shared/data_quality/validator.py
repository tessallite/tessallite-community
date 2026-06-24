"""Data quality rule validator.

Routes all source-database queries through the query-router /introspect
endpoint. Uses a short-lived service token for authentication.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import UUID

import httpx
from jose import jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.config.settings import get_settings
from shared.connector_qualify import quote_identifier, quote_table_ref, transpile_preview_sql
from shared.db.models import (
    DataQualityRule,
    DataQualityViolation,
    DataSource,
    ModelAlert,
    ModelColumn,
    ModelTable,
    ProjectConnection,
)
from shared.schemas.connection_type import normalize_connection_type

log = logging.getLogger(__name__)

_settings = get_settings()

_ALLOWED_RULE_TYPES = frozenset({"not_null", "unique", "range", "regex", "custom_sql"})
_ALLOWED_TARGET_TYPES = frozenset({"dimension", "measure", "column"})
_ALLOWED_SEVERITIES = frozenset({"info", "warn", "error"})


@dataclass
class _Violation:
    rule: DataQualityRule
    count: int
    sample_values: Optional[list]


def _mint_service_token(tenant_id: str) -> str:
    """Mint a short-lived JWT for service-to-service /introspect calls."""
    payload = {
        "sub": "service:data-quality-validator",
        "tenant_id": tenant_id,
        "role": "system_admin",
        "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
    }
    return jwt.encode(payload, _settings.JWT_SECRET_KEY, algorithm=_settings.JWT_ALGORITHM)


def _escape_sql_string(value: str) -> str:
    """Escape a string for safe use in a SQL literal (single-quote delimited)."""
    return value.replace("'", "''")


async def _introspect(
    model_id: str, sql: str, bearer: str, *, source_id: str | None = None,
) -> list[dict[str, Any]]:
    """Execute a read-only query via the query-router /introspect endpoint."""
    url = f"{_settings.QUERY_ROUTER_URL}/api/v1/introspect"
    headers = {"Authorization": f"Bearer {bearer}"}
    body: dict[str, Any] = {"model_id": model_id, "raw_sql": sql}
    if source_id:
        body["source_id"] = source_id
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(url, json=body, headers=headers)
        if resp.status_code >= 400:
            detail = resp.text
            try:
                payload = resp.json()
                if isinstance(payload, dict):
                    detail = payload.get("detail", detail)
            except Exception:
                pass
            raise RuntimeError(f"Introspect HTTP {resp.status_code}: {detail}")
        return resp.json()["rows"]


async def _resolve_column_ref(
    rule: DataQualityRule, db: AsyncSession,
) -> Optional[tuple[str, str, UUID | None]]:
    """Return (qualified_table_name, column_name, source_id) for the rule's target column."""
    col = await db.get(ModelColumn, rule.target_id)
    if col is None:
        return None
    tbl = await db.get(ModelTable, col.model_table_id)
    if tbl is None:
        return None
    return tbl.physical_name, col.column_name, tbl.source_id


async def _count_violations(
    model_id: str,
    bearer: str,
    rule: DataQualityRule,
    table_name: str,
    col_name: str,
    connector: str,
    source_id: str | None = None,
) -> tuple[int, Optional[list]]:
    """Build canonical PostgreSQL SQL, transpile to target dialect, execute via /introspect."""
    cfg = rule.rule_config or {}
    qt = quote_table_ref("postgresql", table_name)
    qc = quote_identifier("postgresql", col_name)

    def _t(sql: str) -> str:
        return transpile_preview_sql(connector, sql)

    async def _run(sql: str) -> list[dict[str, Any]]:
        return await _introspect(model_id, _t(sql), bearer, source_id=source_id)

    if rule.rule_type == "not_null":
        rows = await _run(f"SELECT COUNT(*) AS n FROM {qt} WHERE {qc} IS NULL")
        return int(rows[0]["n"]), None

    if rule.rule_type == "unique":
        rows = await _run(
            f"SELECT COUNT(*) AS n FROM (SELECT {qc} FROM {qt} GROUP BY {qc} HAVING COUNT(*) > 1) sub",
        )
        n = int(rows[0]["n"])
        sample = None
        if n > 0:
            sample_rows = await _run(
                f"SELECT CAST({qc} AS TEXT) AS v FROM {qt} GROUP BY {qc} HAVING COUNT(*) > 1 LIMIT 10",
            )
            sample = [r["v"] for r in sample_rows]
        return n, sample

    if rule.rule_type == "range":
        min_val = cfg.get("min")
        max_val = cfg.get("max")
        conditions = []
        if min_val is not None:
            try:
                conditions.append(f"{qc} < {float(min_val)}")
            except (TypeError, ValueError):
                return 0, None
        if max_val is not None:
            try:
                conditions.append(f"{qc} > {float(max_val)}")
            except (TypeError, ValueError):
                return 0, None
        if not conditions:
            return 0, None
        where = " OR ".join(conditions)
        rows = await _run(f"SELECT COUNT(*) AS n FROM {qt} WHERE {where}")
        return int(rows[0]["n"]), None

    if rule.rule_type == "regex":
        pattern = cfg.get("pattern", "")
        if not pattern:
            return 0, None
        safe_pattern = _escape_sql_string(pattern)
        rows = await _run(
            f"SELECT COUNT(*) AS n FROM {qt} WHERE {qc} !~ '{safe_pattern}'",
        )
        return int(rows[0]["n"]), None

    if rule.rule_type == "custom_sql":
        sql = cfg.get("sql", "").strip()
        if not sql:
            return 0, None
        rows = await _introspect(model_id, _t(sql), bearer, source_id=source_id)
        if rows:
            val = next(iter(rows[0].values()))
            return int(val) if val is not None else 0, None
        return 0, None

    return 0, None


async def validate_rules(
    model_id: UUID,
    db: AsyncSession,
    aggregate_id: Optional[UUID] = None,
    tenant_id: Optional[str] = None,
) -> list[_Violation]:
    """Run all enabled rules for the model. Returns list of violations found."""
    rules_result = await db.execute(
        select(DataQualityRule).where(
            DataQualityRule.model_id == model_id,
            DataQualityRule.is_enabled.is_(True),
        )
    )
    rules = list(rules_result.scalars().all())
    if not rules:
        return []

    resolved_tenant = tenant_id or db.info.get("tenant_id") or "system"
    bearer = _mint_service_token(resolved_tenant)
    now = datetime.now(tz=timezone.utc)
    violations: list[_Violation] = []

    _source_cache: dict[UUID, tuple[str, UUID]] = {}

    async def _resolve_source(source_id: UUID | None) -> tuple[str, str | None]:
        """Return (connector, source_id_str) for the given source, with caching."""
        if source_id is None:
            result = await db.execute(
                select(DataSource).where(DataSource.model_id == model_id).limit(1)
            )
            src = result.scalar_one_or_none()
            if src is None:
                return "postgresql", None
            source_id = src.id
        if source_id not in _source_cache:
            src_obj = await db.get(DataSource, source_id)
            if src_obj is None:
                return "postgresql", str(source_id)
            conn_obj = await db.get(ProjectConnection, src_obj.project_connection_id)
            if conn_obj is None:
                return "postgresql", str(source_id)
            _source_cache[source_id] = (
                normalize_connection_type(conn_obj.connection_type),
                source_id,
            )
        connector, sid = _source_cache[source_id]
        return connector, str(sid)

    for rule in rules:
        try:
            col_ref = await _resolve_column_ref(rule, db)
            if col_ref is None:
                continue
            table_name, col_name, rule_source_id = col_ref
            connector, source_id_str = await _resolve_source(rule_source_id)
            count, sample = await _count_violations(
                str(model_id), bearer, rule, table_name, col_name, connector,
                source_id=source_id_str,
            )
        except Exception as exc:
            log.error("validate_rules: rule %s failed: %s", rule.id, exc)
            count, sample = 0, None

        rule.last_checked_at = now
        rule.last_violation_count = count

        if count > 0:
            db.add(DataQualityViolation(
                rule_id=rule.id,
                detected_at=now,
                violation_count=count,
                sample_values={"values": sample} if sample else None,
                aggregate_id=aggregate_id,
            ))
            alert_severity = "error" if (rule.severity == "error" or rule.block_on_failure) else "warning"
            if rule.severity in ("warn", "error") or rule.block_on_failure:
                db.add(ModelAlert(
                    model_id=model_id,
                    severity=alert_severity,
                    category="data_quality",
                    title=f"Data quality rule '{rule.name}' failed",
                    detail=f"{count} violation(s) detected."
                           + (" Queries blocked until resolved." if rule.block_on_failure else ""),
                    related_object_type="data_quality_rule",
                    related_object_id=rule.id,
                ))
            violations.append(_Violation(rule=rule, count=count, sample_values=sample))

    await db.flush()
    return violations
