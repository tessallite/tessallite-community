"""Per-user scratchpad measures — ephemeral calculated expressions."""
from __future__ import annotations

import logging
from uuid import UUID

import httpx
import sqlglot
from sqlglot import exp
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy import select

from shared.config.settings import get_settings
from shared.connector_qualify import quote_identifier
from shared.db.models import ScratchpadMeasure
from shared.db.session import get_tenant_db
from src.auth.middleware import CurrentUser, forbid_embed_user, get_current_user
from src.auth.rbac import require_role
from src.api._scope import ensure_model_in_project
from src.api._validator_unavailable import validator_unavailable

_settings = get_settings()
_logger = logging.getLogger(__name__)

# The supported scratchpad data types. The UI offers exactly these; an unknown
# value would render a raw i18n key (``scratchpad.dataType.<value>``) in the
# panel, so the set is validated server-side as well (F-029-14).
SCRATCHPAD_DATA_TYPES = {"numeric", "string", "boolean", "date", "timestamp", "integer"}


def _validator_unavailable(reason: object) -> HTTPException:
    """Bug-8162: the 503 meaning "unknown — retry", not "your expression is wrong"."""
    return validator_unavailable("scratchpad measure", reason)


def _validate_expression(expr: str) -> None:
    """Validate that expression is a parseable, single *scalar* SQL expression.

    sqlglot raises ``ParseError`` on malformed SQL, but a syntactically valid
    *multi-statement* input (e.g. ``1; SELECT 2``) parses into several
    statements and is rejected by the ``len(parsed) != 1`` guard as a
    ``ValueError``. Both must surface to the user as a 400, not an unhandled
    500 (F-029-08), so both exception types are mapped here.

    Bug-7446: statement count alone is not sufficient — ``1 UNION SELECT 2``
    and ``(SELECT MAX(secret) FROM other)`` both parse into one statement but
    are not scalar expressions. After confirming a single statement, walk the
    AST projection and reject query-producing nodes (Select, Subquery, Union,
    Intersect, Except) and comment nodes beneath the scalar root.
    """
    try:
        parsed = sqlglot.parse(f"SELECT {expr} AS _v")
    except sqlglot.errors.ParseError as e:
        raise HTTPException(status_code=400, detail=f"Invalid expression: {e}")
    if not parsed or len(parsed) != 1:
        raise HTTPException(
            status_code=400,
            detail="Expression must be a single SQL expression, not multiple statements",
        )

    stmt = parsed[0]
    # Bug-7446: the wrapper ``SELECT {expr} AS _v`` must produce a Select
    # root (not a Union/Intersect/Except, which indicates set operations).
    _QUERY_NODES = (exp.Select, exp.Subquery, exp.Union, exp.Intersect, exp.Except)
    if not isinstance(stmt, exp.Select):
        raise HTTPException(
            status_code=400,
            detail="Expression must be a scalar expression, not a query or set operation",
        )

    # The projection is the expression node under the single Alias in the SELECT.
    projections = stmt.expressions
    if len(projections) != 1:
        raise HTTPException(
            status_code=400,
            detail="Expression must be a single scalar expression",
        )

    # Walk all descendants of the projection expression and reject any
    # query-producing node. This catches subqueries like ``(SELECT ...)``
    # and set operations like ``1 UNION SELECT 2`` that survived as a
    # single AST node.
    projection = projections[0]
    # The projection is wrapped in an Alias (AS _v); walk its child.
    inner = projection.this if isinstance(projection, exp.Alias) else projection
    for node in inner.walk():
        if isinstance(node, _QUERY_NODES):
            raise HTTPException(
                status_code=400,
                detail="Expression must be a scalar expression; subqueries and "
                "set operations (UNION/INTERSECT/EXCEPT) are not allowed",
            )

async def _validate_expression_against_model(
    model_id: UUID,
    model_slug: str,
    expression: str,
    bearer: str | None,
    *,
    timeout_s: float = 30.0,
) -> None:
    """Bug-7279: validate the scratchpad expression BINDS against the model.

    ``_validate_expression`` only proves the string is a single scalar SQL
    expression — it does not prove the columns/measures it references actually
    exist in the model. A structurally-valid-but-unbindable expression (a typo,
    a dropped column, an unknown function) used to be persisted silently and
    then, at render time, the pivot builder emitted ``NULL AS <alias>`` with the
    error buried in a SQL comment (Bug-7279 / frontend ``sql.ts``). The user saw
    a column of NULLs, never an error.

    Root cause: nothing rejected the expression at authoring time. This probes
    the query-router ``/validate`` with the SAME grouped-query shape the panel
    renders (``SELECT (<expr>) AS _v FROM "<slug>"``) so a rejected expression
    fails LOUD with a 400 here and is never persisted — it can therefore never
    render as a silent all-NULL column. Mirrors the pocket create/update
    validation contract (pockets.py ``_validate_via_router``).

    Bug-8162: fails CLOSED on router UNAVAILABILITY — a network error OR a 5xx
    from the router/proxy. This REVERSES the previous contract, which failed
    open on the reasoning that a create/update must not be blocked by a
    transient outage (a router deploy). That trade was decided the other way on
    2026-08-11 (``work/porting-remediation-lane-plan.md`` D-7) because it is not
    symmetric:

    * Failing open costs CORRECTNESS, silently and permanently. The unvalidated
      expression is persisted, the outage passes, nobody revisits it, and it
      renders as an all-NULL column — a wrong answer presented as a real one.
      That is the exact defect this validation exists to prevent, so failing
      open defeats the control at the one moment it is needed.
    * Failing closed costs AVAILABILITY, briefly and visibly. A modeller cannot
      save a scratchpad measure while the router is down; they see an error and
      retry. Authoring is not a serving path — no end user is blocked and no
      query fails.

    During a router outage this endpoint now REFUSES rather than admits. That
    is the intended behaviour, not a regression — do not "fix" it back.

    Refusing is not the same as blaming, so the two outcomes carry different
    statuses (the visibility condition of D-7):

    * a real verdict — a 4xx client-level rejection, or a 200 with ``ok=false``
      — is a **400**: your expression is wrong.
    * unavailability is a **503**: we could not reach the validator, the
      measure was NOT saved, and the expression has NOT been rejected. 503 is
      the standard "temporarily unable, retry" signal; a 400 here would assert
      the caller's input was faulty and tell a modeller their CORRECT
      expression is wrong.
    """
    quoted_slug = quote_identifier("postgresql", model_slug)
    probe_sql = f"SELECT ({expression}) AS _v FROM {quoted_slug}"
    url = f"{_settings.QUERY_ROUTER_URL}/api/v1/validate"
    headers = {"Authorization": f"Bearer {bearer}"} if bearer else {}
    payload = {
        "model_id": str(model_id),
        "raw_query": probe_sql,
        "protocol": "jdbc",
        "dialect": "postgres",
    }
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.post(url, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        # Bug-8162: router unreachable — the expression is UNKNOWN, not wrong.
        # Refuse the write (an unvalidated expression must never persist) and
        # say so with 503, never a 400.
        _logger.warning(
            "scratchpad expression model-validation refused (router "
            "unreachable) for model=%s: %s",
            model_id, exc,
        )
        raise _validator_unavailable(f"{type(exc).__name__}: {exc}") from exc

    if resp.status_code >= 500:
        # Bug-8162: a router/proxy 5xx is unavailability, not a verdict. Same
        # refusal, same 503 — never blame the user's expression for the router
        # being down.
        _logger.warning(
            "scratchpad expression model-validation refused (router HTTP %s) "
            "for model=%s",
            resp.status_code, model_id,
        )
        raise _validator_unavailable(f"router returned HTTP {resp.status_code}")

    if resp.status_code >= 400:
        # A 4xx from /validate is a client-level rejection of the probe query
        # (the expression did not validate against the model).
        try:
            body = resp.json()
            detail = body.get("detail") if isinstance(body, dict) else None
            if not isinstance(detail, str) or not detail:
                detail = resp.text
        except Exception:
            detail = resp.text or f"Query router returned HTTP {resp.status_code}"
        raise HTTPException(
            status_code=400,
            detail=f"Expression is not valid for this model: {detail}",
        )
    try:
        result = resp.json()
    except Exception:
        result = {}
    if isinstance(result, dict) and result.get("ok") is False:
        errors = result.get("errors") or []
        msg = "; ".join(str(e) for e in errors) if errors else "expression rejected"
        raise HTTPException(
            status_code=400,
            detail=f"Expression is not valid for this model: {msg}",
        )


router = APIRouter(
    prefix="/projects/{project_id}/models/{model_id}/scratchpad-measures",
    tags=["scratchpad-measures"],
)


def _validate_data_type(v: str) -> str:
    if v not in SCRATCHPAD_DATA_TYPES:
        raise ValueError(
            f"data_type must be one of {sorted(SCRATCHPAD_DATA_TYPES)}"
        )
    return v


class ScratchpadCreate(BaseModel):
    name: str
    display_name: str | None = None
    expression: str
    data_type: str = "numeric"
    format: str | None = None

    @field_validator("data_type")
    @classmethod
    def _check_data_type(cls, v: str) -> str:
        return _validate_data_type(v)


class ScratchpadUpdate(BaseModel):
    name: str | None = None
    display_name: str | None = None
    expression: str | None = None
    data_type: str | None = None
    format: str | None = None

    @field_validator("data_type")
    @classmethod
    def _check_data_type(cls, v: str | None) -> str | None:
        if v is not None:
            return _validate_data_type(v)
        return v


class ScratchpadResponse(BaseModel):
    id: UUID
    model_id: UUID
    name: str
    display_name: str | None
    expression: str
    data_type: str
    format: str | None
    created_by: str
    created_at: str
    updated_at: str


def _to_response(row: ScratchpadMeasure) -> ScratchpadResponse:
    return ScratchpadResponse(
        id=row.id,
        model_id=row.model_id,
        name=row.name,
        display_name=row.display_name,
        expression=row.expression,
        data_type=row.data_type,
        format=row.format,
        created_by=row.created_by,
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


@router.get("", response_model=list[ScratchpadResponse], dependencies=[require_role("viewer")])
async def list_scratchpad_measures(
    project_id: UUID,
    model_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
) -> list[ScratchpadResponse]:
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        user_id = current_user.email or current_user.user_id
        rows = (await db.execute(
            select(ScratchpadMeasure)
            .where(
                ScratchpadMeasure.model_id == model_id,
                ScratchpadMeasure.created_by == user_id,
            )
            .order_by(ScratchpadMeasure.name)
        )).scalars().all()
        return [_to_response(r) for r in rows]
    raise HTTPException(status_code=500, detail="DB session exhausted")


@router.post("", response_model=ScratchpadResponse, status_code=201, dependencies=[require_role("viewer")])
async def create_scratchpad_measure(
    project_id: UUID,
    model_id: UUID,
    body: ScratchpadCreate,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> ScratchpadResponse:
    _validate_expression(body.expression)
    async for db in get_tenant_db(current_user.tenant_id):
        model = await ensure_model_in_project(
            db, project_id=project_id, model_id=model_id
        )
        # Bug-7279: reject an expression that does not bind against the model
        # BEFORE persisting, so it can never later render as a silent all-NULL
        # column in the pivot panel.
        await _validate_expression_against_model(
            model_id,
            (getattr(model, "slug", "") or ""),
            body.expression,
            current_user.raw_token,
        )
        user_id = current_user.email or current_user.user_id
        # Pre-check the (model_id, created_by, name) uniqueness constraint and
        # return a clean 409 instead of letting the DB IntegrityError escape as
        # a 500 (F-029-08), mirroring parameters.py.
        existing = await db.execute(
            select(ScratchpadMeasure).where(
                ScratchpadMeasure.model_id == model_id,
                ScratchpadMeasure.created_by == user_id,
                ScratchpadMeasure.name == body.name,
            )
        )
        if existing.scalar_one_or_none():
            raise HTTPException(
                status_code=409,
                detail=f"Scratchpad measure '{body.name}' already exists on this model",
            )
        row = ScratchpadMeasure(
            model_id=model_id,
            name=body.name,
            display_name=body.display_name,
            expression=body.expression,
            data_type=body.data_type,
            format=body.format,
            created_by=user_id,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return _to_response(row)
    raise HTTPException(status_code=500, detail="DB session exhausted")


@router.patch("/{measure_id}", response_model=ScratchpadResponse, dependencies=[require_role("viewer")])
async def update_scratchpad_measure(
    project_id: UUID,
    model_id: UUID,
    measure_id: UUID,
    body: ScratchpadUpdate,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> ScratchpadResponse:
    # Distinguish "field absent" from "field set to null": only fields the
    # caller actually supplied are applied, so an explicit null clears an
    # optional column (display_name / format) rather than being silently
    # skipped (F-029-11).
    supplied = body.model_dump(exclude_unset=True)
    async for db in get_tenant_db(current_user.tenant_id):
        model = await ensure_model_in_project(
            db, project_id=project_id, model_id=model_id
        )
        row = await db.get(ScratchpadMeasure, measure_id)
        if not row or row.model_id != model_id:
            raise HTTPException(status_code=404, detail="Scratchpad measure not found")
        user_id = current_user.email or current_user.user_id
        if row.created_by != user_id:
            raise HTTPException(status_code=403, detail="Not your scratchpad measure")
        if "name" in supplied and supplied["name"] is not None:
            row.name = supplied["name"]
        if "display_name" in supplied:
            row.display_name = supplied["display_name"]
        if "expression" in supplied and supplied["expression"] is not None:
            _validate_expression(supplied["expression"])
            # Bug-7279: same model-binding validation as create — a rejected
            # expression must fail loud here, never persist to render NULL.
            await _validate_expression_against_model(
                model_id,
                (getattr(model, "slug", "") or ""),
                supplied["expression"],
                current_user.raw_token,
            )
            row.expression = supplied["expression"]
        if "data_type" in supplied and supplied["data_type"] is not None:
            row.data_type = supplied["data_type"]
        if "format" in supplied:
            row.format = supplied["format"]
        await db.commit()
        await db.refresh(row)
        return _to_response(row)
    raise HTTPException(status_code=500, detail="DB session exhausted")


@router.delete("/{measure_id}", status_code=204, dependencies=[require_role("viewer")])
async def delete_scratchpad_measure(
    project_id: UUID,
    model_id: UUID,
    measure_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> None:
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        row = await db.get(ScratchpadMeasure, measure_id)
        if not row or row.model_id != model_id:
            raise HTTPException(status_code=404, detail="Scratchpad measure not found")
        user_id = current_user.email or current_user.user_id
        if row.created_by != user_id:
            raise HTTPException(status_code=403, detail="Not your scratchpad measure")
        await db.delete(row)
        await db.commit()
        return
    raise HTTPException(status_code=500, detail="DB session exhausted")
