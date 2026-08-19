"""Named Set CRUD + validation + preview + refresh routes."""
from __future__ import annotations

import logging
import math
import re
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func as sa_func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import ObjectDeletedError

from shared.config.settings import get_settings
# Bug-8453: the single definition of the /execute row-security contract.
from shared.security.execute_contract import (
    RowSecurityDeniedError,
    execute_response_denied_all,
)
from shared.connector_qualify import safe_ident as _safe_ident
from shared.db.models import (
    DataSource,
    Dimension,
    Measure,
    ModelParameter,
    NamedSet,
    NamedSetUsage,
    NamedSetVersion,
    ProjectConnection,
)
from shared.db.session import get_tenant_db
from shared.named_list_compiler import CompilationError, compile_definition, explain_definition
from shared.schemas.pydantic_models import (
    CertifyRequest,
    DeprecateRequest,
    EntityUsageCreate,
    EntityUsageResponse,
    NamedSetCreate,
    NamedSetPreviewResponse,
    NamedSetResponse,
    NamedSetUpdate,
    NamedSetValidateRequest,
    NamedSetValidateResponse,
    VersionResponse,
)
from src.api._model_lock import acquire_model_definition_lock
from src.named_set_deploy_resolver import (
    NamedSetSnapshotInvalidError,
    resolve_served_named_sets,
)
from src.api._persona_scope import parse_allowed_ids, resolve_effective_persona
from src.api._scope import ensure_model_in_project, purge_entity_soft_references
from src.auth.middleware import CurrentUser, get_current_user
from src.auth.rbac import caller_has_role, require_role

log = logging.getLogger(__name__)
_settings = get_settings()

router = APIRouter(
    prefix="/projects/{project_id}/models/{model_id}/named-sets",
    tags=["named-sets"],
)


def _auto_compile(body_dict: dict, model_metadata: dict | None = None) -> dict:
    """If builder_definition is present, compile it to expression.

    For ``sql_fixed`` lists the expression stays empty — the query-router
    resolves these from the snapshot's builder_definition members, never via
    an MDX expression.
    """
    if body_dict.get("list_type") == "sql_fixed":
        # sql_fixed lists do not compile to MDX; expression stays empty.
        body_dict["expression"] = ""
        return body_dict

    builder_def = body_dict.get("builder_definition")
    if builder_def:
        try:
            body_dict["expression"] = compile_definition(builder_def, model_metadata)
        except CompilationError as exc:
            raise HTTPException(
                status_code=422,
                detail=f"Builder definition compilation failed: {exc}",
            )
    if not body_dict.get("expression"):
        raise HTTPException(
            status_code=422,
            detail="Either 'expression' or a valid 'builder_definition' is required",
        )
    return body_dict


async def _model_source_system(db, model_id: UUID) -> str | None:
    """Return the source connection type for named-set trust metadata.

    Named-list refreshes execute through the model's source binding. Keep the
    existing trust_meta field names, while treating the source label as
    best-effort metadata so a catalogue decoration failure cannot turn a
    successful named-set read into an API failure.
    """
    try:
        result = await db.execute(
            select(
                ProjectConnection.connection_type,
                DataSource.source_type,
            )
            .join(
                DataSource,
                DataSource.project_connection_id == ProjectConnection.id,
            )
            .where(DataSource.model_id == model_id)
            .order_by(DataSource.created_at, DataSource.id)
            .limit(1)
        )
        if hasattr(result, "first"):
            row = result.first()
        else:
            # A few lightweight API tests provide a scalar-only result stub.
            # Its first selected column is the same connection-type value we
            # prefer from a real two-column Row.
            row = result.scalar_one_or_none()
        if row is None:
            return None
        values = getattr(row, "_mapping", None)
        if isinstance(values, Mapping):
            return values.get("connection_type") or values.get("source_type")
        if isinstance(row, (tuple, list)):
            return row[0] or (row[1] if len(row) > 1 else None)
    except Exception:
        log.warning(
            "named-set trust metadata could not resolve source system for model %s",
            model_id,
            exc_info=True,
        )
    return None


def _named_set_trust_meta(
    named_set: NamedSet,
    source_system: str | None,
) -> dict[str, Any]:
    """Build the common trust metadata shape for a named-set response."""
    builder_definition = named_set.builder_definition
    last_refreshed_at = (
        builder_definition.get("last_refreshed_at")
        if isinstance(builder_definition, dict)
        else None
    )
    if isinstance(last_refreshed_at, datetime):
        last_refreshed_at = last_refreshed_at.isoformat()
    elif last_refreshed_at is not None:
        last_refreshed_at = str(last_refreshed_at)
    return {
        "last_refreshed_at": last_refreshed_at,
        "source_system": source_system,
        "owner": getattr(named_set, "owner_user_id", None) or "",
    }


def _decorate_named_set_response(
    named_set: NamedSet,
    source_system: str | None,
) -> NamedSetResponse:
    """Attach trust metadata without changing the persisted named-set shape."""
    response = NamedSetResponse.model_validate(named_set)
    response.trust_meta = _named_set_trust_meta(named_set, source_system)
    return response


# ---------------------------------------------------------------------------
# sql_fixed builder_definition validation
# ---------------------------------------------------------------------------

# Import the control-character pattern from named_list_compiler.py so the
# create-time gate and the MDX compiler stay in lockstep if one is tightened.
from shared.named_list_compiler import _CONTROL_CHARS  # noqa: E402

# Per-member string length cap.
_MEMBER_VALUE_MAX_LEN = 1000


_VALID_BUILDER_TYPES = {"fixedMembers", "topN", "filter", "sql_query"}
_DYNAMIC_BUILDER_TYPES = {"topN", "filter", "sql_query"}

_DML_KEYWORDS_RE = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE)\b",
    re.IGNORECASE,
)

_SELECT_STAR_RE = re.compile(r"\bSELECT\s+\*", re.IGNORECASE)


def _validate_sql_fixed_builder(
    builder_def: dict | None,
    *,
    member_cap: int,
) -> None:
    """Validate a ``sql_fixed`` builder_definition at create/update time.

    Accepts four definition types:
    - ``fixedMembers``: manual member entry (requires non-empty members).
    - ``topN``: dimension + measure + count + direction (members may be empty).
    - ``filter``: dimension + conditions (members may be empty).
    - ``sql_query``: free-hand SQL returning one column (members may be empty).

    Dynamic types (topN, filter, sql_query) are validly created with empty
    members — the modeler must refresh to compute them from the source data.

    Raises ``HTTPException`` (422) on any validation failure.
    """
    if not isinstance(builder_def, dict):
        raise HTTPException(
            status_code=422,
            detail="sql_fixed named lists require a builder_definition.",
        )

    btype = builder_def.get("type")
    if btype not in _VALID_BUILDER_TYPES:
        raise HTTPException(
            status_code=422,
            detail=(
                f"sql_fixed builder_definition type must be one of "
                f"{sorted(_VALID_BUILDER_TYPES)}; got {btype!r}."
            ),
        )

    # data_type field (required for all types)
    data_type = builder_def.get("data_type")
    if data_type not in ("string", "number"):
        raise HTTPException(
            status_code=422,
            detail=(
                "sql_fixed builder_definition requires 'data_type' to be "
                f"'string' or 'number'; got {data_type!r}."
            ),
        )

    # --- Type-specific validation ---

    if btype == "topN":
        _validate_topn_builder(builder_def)
        _validate_members_if_present(builder_def, member_cap)
        return

    if btype == "filter":
        _validate_filter_builder(builder_def)
        _validate_members_if_present(builder_def, member_cap)
        return

    if btype == "sql_query":
        _validate_sql_query_builder(builder_def)
        _validate_members_if_present(builder_def, member_cap)
        return

    # --- fixedMembers: original validation path ---

    # Dimension field
    dimension = builder_def.get("dimension")
    if not dimension or not isinstance(dimension, str) or not dimension.strip():
        raise HTTPException(
            status_code=422,
            detail="sql_fixed builder_definition requires a non-empty 'dimension' field.",
        )

    # Members list (required non-empty for fixedMembers)
    members = builder_def.get("members")
    if not isinstance(members, list) or len(members) == 0:
        raise HTTPException(
            status_code=422,
            detail="sql_fixed lists require a non-empty 'members' list.",
        )

    # Member count cap (config-driven)
    if len(members) > member_cap:
        raise HTTPException(
            status_code=422,
            detail=(
                f"sql_fixed member count ({len(members)}) exceeds the "
                f"configured cap ({member_cap})."
            ),
        )

    # Normalize and validate each member value.
    # The SQL-path resolver renders members as typed literals
    # (exp.Literal.string / .number). Dict-form members ({key, caption})
    # are an MDX-path construct; for sql_fixed, normalize them to plain
    # values (the key) so the persisted shape is resolver-compatible.
    normalized: list = []
    for i, member in enumerate(members, start=1):
        if isinstance(member, dict):
            value = member.get("key", member.get("caption", ""))
        else:
            value = member

        text = str(value) if value is not None else ""

        if not text:
            raise HTTPException(
                status_code=422,
                detail=f"sql_fixed member {i} is empty.",
            )

        # Per-member length cap
        if len(text) > _MEMBER_VALUE_MAX_LEN:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"sql_fixed member {i} exceeds the {_MEMBER_VALUE_MAX_LEN}-character limit."
                ),
            )

        # Control-character rejection
        if _CONTROL_CHARS.search(text):
            raise HTTPException(
                status_code=422,
                detail=f"sql_fixed member {i} contains control characters.",
            )

        # Homogeneous type check and normalization
        if data_type == "number":
            if isinstance(value, bool):
                raise HTTPException(
                    status_code=422,
                    detail=f"sql_fixed member {i} must be numeric (data_type is 'number').",
                )
            if isinstance(value, int):
                # Preserve native int verbatim — a float() round-trip
                # silently corrupts integers beyond 2^53 (wrong-numbers).
                normalized.append(value)
                continue
            if isinstance(value, float):
                if math.isnan(value) or math.isinf(value):
                    raise HTTPException(
                        status_code=422,
                        detail=(
                            f"sql_fixed member {i} ({value!r}) is not a finite number."
                        ),
                    )
                normalized.append(value)
                continue
            # String value — attempt numeric parse.
            try:
                # Try int first to preserve precision for large integers.
                normalized.append(int(str(value)))
                continue
            except (ValueError, TypeError):
                pass
            try:
                num = float(str(value))
            except (ValueError, TypeError, OverflowError):
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"sql_fixed member {i} ({value!r}) is not numeric "
                        f"(data_type is 'number')."
                    ),
                )
            if math.isnan(num) or math.isinf(num):
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"sql_fixed member {i} ({value!r}) is not a finite number."
                    ),
                )
            normalized.append(num)
        else:
            # String members: store as plain string.
            normalized.append(text)

    # Replace the members list with the validated, normalized form
    # so the persisted builder_definition is resolver-compatible.
    builder_def["members"] = normalized


def _validate_topn_builder(builder_def: dict) -> None:
    """Validate topN-specific fields."""
    entity = builder_def.get("entity")
    if not entity or not isinstance(entity, str) or not entity.strip():
        raise HTTPException(
            status_code=422,
            detail="topN builder_definition requires a non-empty 'entity' (dimension name).",
        )
    measure = builder_def.get("measure")
    if not measure or not isinstance(measure, str) or not measure.strip():
        raise HTTPException(
            status_code=422,
            detail="topN builder_definition requires a non-empty 'measure' field.",
        )
    count = builder_def.get("count")
    if not isinstance(count, (int, float)) or count < 1:
        raise HTTPException(
            status_code=422,
            detail="topN builder_definition requires 'count' >= 1.",
        )
    direction = builder_def.get("direction", "top")
    if direction not in ("top", "bottom"):
        raise HTTPException(
            status_code=422,
            detail="topN builder_definition 'direction' must be 'top' or 'bottom'.",
        )


def _validate_filter_builder(builder_def: dict) -> None:
    """Validate filter-specific fields."""
    entity = builder_def.get("entity")
    if not entity or not isinstance(entity, str) or not entity.strip():
        raise HTTPException(
            status_code=422,
            detail="filter builder_definition requires a non-empty 'entity' (dimension name).",
        )
    conditions = builder_def.get("conditions")
    if not isinstance(conditions, list) or len(conditions) == 0:
        raise HTTPException(
            status_code=422,
            detail="filter builder_definition requires at least one condition.",
        )
    for i, cond in enumerate(conditions, start=1):
        if not isinstance(cond, dict):
            raise HTTPException(status_code=422, detail=f"Condition {i} must be an object.")
        if not cond.get("field"):
            raise HTTPException(status_code=422, detail=f"Condition {i} requires a 'field'.")
        if not cond.get("operator"):
            raise HTTPException(status_code=422, detail=f"Condition {i} requires an 'operator'.")
        if cond.get("value") is None or cond.get("value") == "":
            raise HTTPException(status_code=422, detail=f"Condition {i} requires a 'value'.")


def _validate_sql_query_builder(builder_def: dict) -> None:
    """Validate sql_query-specific fields (defense-in-depth DML rejection)."""
    query = builder_def.get("query")
    if not query or not isinstance(query, str) or not query.strip():
        raise HTTPException(
            status_code=422,
            detail="sql_query builder_definition requires a non-empty 'query' field.",
        )
    if _DML_KEYWORDS_RE.search(query):
        raise HTTPException(
            status_code=422,
            detail="sql_query must be a read-only SELECT statement. DML keywords are not allowed.",
        )
    if _SELECT_STAR_RE.search(query):
        raise HTTPException(
            status_code=422,
            detail="sql_query must name specific columns (SELECT * is not allowed).",
        )


def _validate_members_if_present(builder_def: dict, member_cap: int) -> None:
    """For dynamic types, members are optional (empty until refreshed).

    If present and non-empty, validate count does not exceed the cap.
    """
    members = builder_def.get("members")
    if not isinstance(members, list) or len(members) == 0:
        builder_def.setdefault("members", [])
        return
    if len(members) > member_cap:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Member count ({len(members)}) exceeds the configured cap ({member_cap})."
            ),
        )


# Bug-7960: reference-name validity pattern — same as parameter names minus
# the leading @, so @Name is a valid sqlglot placeholder.
_REFERENCE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _validate_reference_name(name: str) -> None:
    """Reject names that cannot be used as @Name placeholders in SQL.

    Bug-7960: display_name keeps free text; the stored 'name' field must be
    a valid identifier so ``@Name`` tokenizes as a single placeholder span.
    """
    if not _REFERENCE_NAME_RE.match(name):
        raise HTTPException(
            status_code=422,
            detail=(
                f"Named list reference name must start with a letter or underscore "
                f"and contain only letters, digits, or underscores (got '{name}'). "
                f"Use display_name for a human-readable label."
            ),
        )


def _canonical_ns_identity(name: str) -> str:
    """Return the canonical lowercase, unprefixed identity for namespace checks.

    Bug-7944/Bug-7961/Bug-7928: parameters are stored as ``@name``, named lists
    as ``name`` — strip the leading ``@`` and lowercase so both resolve to the
    same identity for collision detection.
    """
    stripped = name.lstrip("@") if name.startswith("@") else name
    return stripped.lower()


async def _check_parameter_namespace_collision(
    db,
    model_id: UUID,
    name: str,
) -> None:
    """Reject if a ModelParameter with the same canonical name exists.

    Named lists and model parameters share the ``@`` namespace. A collision
    at query time would be ambiguous (see spec section 5.2).

    Bug-7944: uses the canonical identity function so ``@Region`` parameter
    collides with ``region`` named list.
    """
    canonical = _canonical_ns_identity(name)
    # DB-side filter: parameters store names as @Name, so strip the leading
    # @ and lowercase for comparison. sa_func.ltrim + sa_func.lower.
    result = await db.execute(
        select(ModelParameter.id, ModelParameter.name).where(
            ModelParameter.model_id == model_id,
            sa_func.lower(
                sa_func.ltrim(ModelParameter.name, "@")
            ) == canonical,
        ).limit(1)
    )
    row = result.first()
    if row is not None:
        _, param_name = row
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"A model parameter called '{param_name}' already exists in this model. "
                f"Named lists and parameters share the @ namespace and must have "
                f"unique names (case-insensitive)."
            ),
        )


# Bug-5963: mirrors ``_validate_expression``'s own bracket-token
# extraction below -- member keys are written as ``&[key]`` and must be
# stripped first, or arbitrary source values would be mistaken for
# dimension-name references.
_MEMBER_KEY_RE = re.compile(r"&\[[^\]]*\]")
_BRACKET_REF_RE = re.compile(r"\[([^\]]+)\]")
_NON_DIMENSION_REF_TOKENS = {"measures", "model", "members", "all"}


def _named_set_referenced_dimension_names(expression: str | None) -> set[str]:
    """Extract candidate dimension-name references from an MDX expression."""
    if not expression:
        return set()
    expr_without_keys = _MEMBER_KEY_RE.sub("", expression)
    refs = _BRACKET_REF_RE.findall(expr_without_keys)
    return {r for r in refs if r.lower() not in _NON_DIMENSION_REF_TOKENS}


async def _named_set_visible_to_persona(
    db,
    ns: NamedSet,
    model_id: UUID,
    allowed_dim_ids: list[UUID] | None,
) -> bool:
    """Return True if the named set's dimension is within the persona scope.

    Mirrors ``_kpi_visible_to_persona`` in ``kpis.py``. If
    *allowed_dim_ids* is ``None`` the persona is unrestricted.

    Unlike the KPI expression language (which only ever references known
    measure names), an MDX set expression's bracket tokens also cover
    hierarchy names, level names, and literal member captions that will
    never match a dimension name -- exactly why
    ``_validate_expression`` below only *warns* on an unmatched ref
    instead of rejecting it. So this check only restricts on a positive,
    confident match to a real model dimension that is outside the
    persona's allow-list; an expression this extraction cannot
    confidently resolve to any dimension is left visible rather than
    silently hidden.
    """
    if allowed_dim_ids is None:
        return True
    referenced = _named_set_referenced_dimension_names(ns.expression)
    # Bug-6329: also include the authoritative persisted dimensions field
    # (comma/semicolon-separated dimension names set by the builder).
    # Mirrors agent-service ``_named_set_outside_persona_scope``.
    raw_dims = getattr(ns, "dimensions", None)
    if raw_dims:
        for part in re.split(r"[;,]", str(raw_dims)):
            token = part.strip()
            if token:
                referenced.add(token)
    if not referenced:
        return True
    result = await db.execute(
        select(Dimension.id, Dimension.name).where(Dimension.model_id == model_id)
    )
    name_to_id = {name.lower(): dim_id for dim_id, name in result.all()}
    allowed_set = set(allowed_dim_ids)
    for ref in referenced:
        dim_id = name_to_id.get(ref.lower())
        if dim_id is not None and dim_id not in allowed_set:
            return False
    return True


def _named_set_visible_to_persona_fast(
    ns: NamedSet,
    dim_name_to_id: dict[str, UUID],
    allowed_set: set[UUID],
) -> bool:
    """Bug-7255: synchronous persona-visibility check using a pre-built
    dimension map. Identical logic to ``_named_set_visible_to_persona``
    but avoids the N+1 dimension query by accepting the map as a parameter.
    """
    referenced = _named_set_referenced_dimension_names(ns.expression)
    raw_dims = getattr(ns, "dimensions", None)
    if raw_dims:
        for part in re.split(r"[;,]", str(raw_dims)):
            token = part.strip()
            if token:
                referenced.add(token)
    if not referenced:
        return True
    for ref in referenced:
        dim_id = dim_name_to_id.get(ref.lower())
        if dim_id is not None and dim_id not in allowed_set:
            return False
    return True


from fastapi import Response as FastAPIResponse  # noqa: E402


@router.get("", response_model=list[NamedSetResponse])
async def list_named_sets(
    project_id: UUID,
    model_id: UUID,
    response: FastAPIResponse,
    persona_id: UUID | None = Query(default=None),
    deployed_only: bool = Query(
        default=False,
        description=(
            "When true, serve each named set's DEFINITION from the model's "
            "deployed snapshot and withhold any set absent from it. The gateway "
            "passes this for BI surfaces (XMLA MDSCHEMA_SETS and Execute-time "
            "MDX inlining) so an undeployed draft edit never changes production "
            "query semantics before Deploy; the model builder omits it so "
            "modellers keep seeing their drafts (Bug-8384 / F-013-08)."
        ),
    ),
    current_user: CurrentUser = Depends(get_current_user),
    _: None = require_role("viewer"),
) -> list[NamedSetResponse]:
    async for db in get_tenant_db(current_user.tenant_id):
        # F-018-04: verify the model belongs to the project in the path.
        model = await ensure_model_in_project(
            db, project_id=project_id, model_id=model_id,
        )
        query = select(NamedSet).where(NamedSet.model_id == model_id).order_by(NamedSet.name)

        source_system = await _model_source_system(db, model_id)

        # Bug-8767: Draft named sets are only visible to modelers/admins
        # (Section 13.1) — mirrors list_kpis's identical gate.
        is_privileged = await caller_has_role(
            db,
            current_user,
            project_id,
            "modeler",
            model_id,
        )
        if not is_privileged:
            query = query.where(NamedSet.certification_status != "draft")

        result = await db.execute(query)
        named_sets = list(result.scalars().all())

        # Bug-8384: BI surfaces request deployed-only. Resolve each set's served
        # DEFINITION from the deployed snapshot and withhold any set the
        # deployed version does not contain, so an in-progress expression edit
        # cannot reach a connected BI client before Deploy. This runs BEFORE the
        # persona filter below so the persona gate judges the definition that is
        # actually served, not a draft the client will never see.
        if deployed_only:
            try:
                resolved, withheld = await resolve_served_named_sets(
                    db, model, named_sets,
                )
            except NamedSetSnapshotInvalidError as exc:
                raise HTTPException(
                    status_code=409,
                    detail=f"{NamedSetSnapshotInvalidError.error_code}: {exc}",
                )
            if withheld:
                # Otherwise a set the modeller just created simply never appears
                # in Excel and nothing anywhere says why.
                log.info(
                    "Named sets withheld from BI for model %s: %s not present in "
                    "deployed version %s (create + Deploy the model to publish "
                    "them).",
                    model_id, [str(x) for x in withheld],
                    model.deployed_version_id,
                )
            named_sets = [r.named_set for r in resolved]

        # Bug-5963: named-set listing predates the Excel persona switcher
        # and, unlike measures/dimensions/KPIs, was never scoped to the
        # active persona -- filter out sets built on a dimension the
        # persona cannot see.
        # Bug-7255: hoist the dimension-name-to-id map outside the loop so
        # it is built once per request, not once per named set (N+1 fix).
        persona = await resolve_effective_persona(
            db, current_user=current_user, model_id=model_id,
            requested_persona_id=persona_id,
        )
        if persona:
            allowed = parse_allowed_ids(persona.included_dimension_ids)
            if allowed is not None:
                # Single query: build name->id map once for the model
                dim_result = await db.execute(
                    select(Dimension.id, Dimension.name)
                    .where(Dimension.model_id == model_id)
                )
                dim_name_to_id = {
                    name.lower(): dim_id for dim_id, name in dim_result.all()
                }
                allowed_set = set(allowed)
                named_sets = [
                    ns for ns in named_sets
                    if _named_set_visible_to_persona_fast(
                        ns, dim_name_to_id, allowed_set,
                    )
                ]

        # Bug-7949: expose the effective member cap to the frontend via a
        # response header, so it doesn't need to hardcode 1000.
        member_cap = min(
            _settings.NAMED_LIST_MEMBER_CAP,
            _settings.NAMED_LIST_MEMBER_CAP_CEILING,
        )
        response.headers["X-Named-List-Member-Cap"] = str(member_cap)
        return [_decorate_named_set_response(ns, source_system) for ns in named_sets]
    return []


@router.get("/{named_set_id}", response_model=NamedSetResponse)
async def get_named_set(
    project_id: UUID,
    model_id: UUID,
    named_set_id: UUID,
    persona_id: UUID | None = Query(default=None),
    current_user: CurrentUser = Depends(get_current_user),
    _: None = require_role("viewer"),
) -> NamedSetResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        ns = await db.get(NamedSet, named_set_id)
        if ns is None or ns.model_id != model_id:
            raise HTTPException(status_code=404, detail="Named set not found")

        # Bug-8767: Draft named sets are only visible to modelers/admins
        # (Section 13.1) — mirrors get_kpi's identical gate.
        is_privileged = await caller_has_role(
            db,
            current_user,
            project_id,
            "modeler",
            model_id,
        )
        if ns.certification_status == "draft" and not is_privileged:
            raise HTTPException(status_code=404, detail="Named set not found")

        # Bug-6329: persona dimension scope gate -- mirrors get_kpi and
        # list_named_sets.  A restricted persona must not fetch a named
        # set whose dimension lineage falls outside their scope.
        persona = await resolve_effective_persona(
            db, current_user=current_user, model_id=model_id,
            requested_persona_id=persona_id,
        )
        if persona:
            allowed = parse_allowed_ids(
                getattr(persona, "included_dimension_ids", None),
            )
            if allowed is not None and not await _named_set_visible_to_persona(
                db, ns, model_id, allowed,
            ):
                raise HTTPException(status_code=404, detail="Named set not found")

        return _decorate_named_set_response(
            ns, await _model_source_system(db, model_id),
        )
    raise HTTPException(status_code=500, detail="DB session exhausted")


@router.post(
    "",
    response_model=NamedSetResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[require_role("modeler")],
)
async def create_named_set(
    project_id: UUID,
    model_id: UUID,
    body: NamedSetCreate,
    current_user: CurrentUser = Depends(get_current_user),
) -> NamedSetResponse:
    data = body.model_dump()

    # --- sql_fixed-specific validation (before compilation) ---
    if data.get("list_type") == "sql_fixed":
        # Bug-7960: validate reference name before anything else.
        _validate_reference_name(data.get("name", ""))
        member_cap = min(
            _settings.NAMED_LIST_MEMBER_CAP,
            _settings.NAMED_LIST_MEMBER_CAP_CEILING,
        )
        _validate_sql_fixed_builder(
            data.get("builder_definition"),
            member_cap=member_cap,
        )

    data = _auto_compile(data)

    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        # Bug-7982: serialise definition/governance writes against Save/revert.
        await acquire_model_definition_lock(db, model_id)

        # Bug-7927: explicit case-insensitive uniqueness check — the DB
        # index enforces it too, but this gives a clearer error message.
        existing_result = await db.execute(
            select(NamedSet.id).where(
                NamedSet.model_id == model_id,
                sa_func.lower(NamedSet.name) == data["name"].lower(),
            ).limit(1)
        )
        if existing_result.scalar_one_or_none() is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"A named set with the name '{data['name']}' (case-insensitive) "
                    f"already exists in this model."
                ),
            )

        # Cross-namespace uniqueness: named lists and model parameters
        # share the @ namespace (spec section 5.2).
        if data.get("list_type") == "sql_fixed":
            await _check_parameter_namespace_collision(db, model_id, data["name"])

        ns = NamedSet(model_id=model_id, **data)
        db.add(ns)
        try:
            await db.flush()
            await _create_version(db, ns, current_user.email, "Created")
            await db.commit()
        except IntegrityError:
            # F-018-19: `UniqueConstraint(model_id, name)` collision — surface a
            # 409 with a clear message instead of leaking a raw 500.
            await db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"A named set called '{data.get('name')}' already exists in this model.",
            )
        await db.refresh(ns)
        return _decorate_named_set_response(
            ns, await _model_source_system(db, model_id),
        )
    raise HTTPException(status_code=500, detail="DB session exhausted")


_DEFINITION_FIELDS = {"expression", "builder_definition", "list_type", "name", "display_name", "description", "display_folder", "scope", "dimensions"}

# Bug-6264: certification is a controlled enum; "shared" renders as a
# [Certified] marker in the XMLA catalogue and is certified-equivalent, so it
# requires admin authority like "certified"/"deprecated".
_ALLOWED_CERTIFICATION_STATUSES = ("draft", "shared", "certified", "deprecated")
_PRIVILEGED_CERTIFICATION_STATUSES = ("certified", "deprecated", "shared")


async def _create_version(db, ns: NamedSet, changed_by: str | None, summary: str | None) -> None:
    snap = {
        "name": ns.name,
        "display_name": ns.display_name,
        "description": ns.description,
        "display_folder": ns.display_folder,
        "expression": ns.expression,
        "builder_definition": ns.builder_definition,
        "list_type": ns.list_type,
        "scope": ns.scope,
        "dimensions": ns.dimensions,
        "certification_status": ns.certification_status,
    }
    # F-018-18: `max(version_number)+1` races between two concurrent updates.
    # The unique constraint `uq_named_set_version_number` makes the collision
    # loud; retry under a savepoint with a freshly recomputed max so the second
    # writer lands on the next free number instead of duplicating one.
    for _attempt in range(5):
        max_ver = await db.scalar(
            select(sa_func.max(NamedSetVersion.version_number))
            .where(NamedSetVersion.named_set_id == ns.id)
        )
        try:
            async with db.begin_nested():
                db.add(NamedSetVersion(
                    named_set_id=ns.id,
                    version_number=(max_ver or 0) + 1,
                    changed_by=changed_by,
                    change_summary=summary,
                    snapshot=snap,
                ))
            return
        except IntegrityError:
            # Another transaction took this version number first — recompute.
            continue
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="Could not allocate a version number; please retry.",
    )


@router.patch(
    "/{named_set_id}",
    response_model=NamedSetResponse,
    dependencies=[require_role("modeler")],
)
async def update_named_set(
    project_id: UUID,
    model_id: UUID,
    named_set_id: UUID,
    body: NamedSetUpdate,
    current_user: CurrentUser = Depends(get_current_user),
) -> NamedSetResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        # Bug-7982: serialise definition/governance writes against Save/revert.
        await acquire_model_definition_lock(db, model_id)
        ns = await db.get(NamedSet, named_set_id)
        if ns is None or ns.model_id != model_id:
            raise HTTPException(status_code=404, detail="Named set not found")
        updates = body.model_dump(exclude_unset=True)

        is_admin = current_user.role in ("admin", "tenant_admin", "system_admin")
        # Bug-6262 (F-018-02): only enforce the admin guard when the
        # requested certification_status is actually different from the
        # current value.  The frontend echoes back the current status on
        # every save; gating on key-presence alone causes a modeler
        # editing a certified set's description to hit a 403 even though
        # the modeler is not trying to change certification state.
        if "certification_status" in updates:
            requested = updates["certification_status"]
            # Bug-6264: reject arbitrary strings AND an explicit null —
            # certification_status is a NOT NULL controlled enum. Skipping null
            # would fail open (bypass the privileged-status gate) and hit a
            # NOT NULL 500 on commit; fail closed with 422 instead.
            if requested not in _ALLOWED_CERTIFICATION_STATUSES:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "certification_status must be one of: "
                        + ", ".join(_ALLOWED_CERTIFICATION_STATUSES)
                    ),
                )
            if requested != ns.certification_status:
                # Bug-6264: "shared" is admin-only alongside certified/deprecated.
                if not is_admin and requested in _PRIVILEGED_CERTIFICATION_STATUSES:
                    raise HTTPException(status_code=403, detail="Only admins can set certified, shared, or deprecated status")
            else:
                # Status unchanged — drop it from updates so it does not
                # interfere with the definition-change or demote logic.
                del updates["certification_status"]

        # Bug-7930: construct the effective post-update object and run the
        # same complete create-time validation before persisting. This prevents
        # a PATCH that sends partial fields or changes list_type/name from
        # bypassing member/type/namespace checks.
        effective_list_type = updates.get("list_type", ns.list_type)
        effective_name = updates.get("name", ns.name)

        # Bug-7944/Bug-7928: namespace collision check on rename.
        if "name" in updates and updates["name"] != ns.name and effective_list_type == "sql_fixed":
            await _check_parameter_namespace_collision(db, model_id, effective_name)

        if effective_list_type == "sql_fixed":
            # Construct effective builder_definition from current + updates.
            effective_bd = updates.get("builder_definition") or (
                dict(ns.builder_definition) if ns.builder_definition else None
            )
            member_cap = min(
                _settings.NAMED_LIST_MEMBER_CAP,
                _settings.NAMED_LIST_MEMBER_CAP_CEILING,
            )
            _validate_sql_fixed_builder(effective_bd, member_cap=member_cap)
            if "builder_definition" in updates or effective_bd:
                updates["expression"] = ""
        elif "builder_definition" in updates and updates["builder_definition"]:
            try:
                updates["expression"] = compile_definition(updates["builder_definition"])
            except CompilationError as exc:
                raise HTTPException(status_code=422, detail=str(exc))

        # Bug-7960: reference-name validity on rename for sql_fixed lists.
        if "name" in updates and effective_list_type == "sql_fixed":
            _validate_reference_name(effective_name)

        # Bug-6262 (F-018-02): detect definition changes by comparing
        # actual values against the current state, not by key presence.
        # The frontend serialises the full form (including unchanged
        # fields) on every save; key-presence detection treats a no-op
        # save as a definition change, silently decertifying certified
        # sets and writing junk version rows.
        changed_def_keys = [
            k for k, v in updates.items()
            if k in _DEFINITION_FIELDS and getattr(ns, k) != v
        ]
        changed_definition = bool(changed_def_keys)
        was_certified = ns.certification_status in ("certified", "shared")
        prev_cert_status = ns.certification_status

        for k, v in updates.items():
            setattr(ns, k, v)

        if changed_definition and was_certified:
            ns.certification_status = "draft"

        if changed_definition:
            await _create_version(db, ns, current_user.email, f"Updated: {', '.join(changed_def_keys)}")
        elif ns.certification_status != prev_cert_status:
            # Bug-6264: record a version row for a PATCH-driven certification
            # transition so the governance change is traceable.
            await _create_version(
                db, ns, current_user.email,
                f"Certification: {prev_cert_status} -> {ns.certification_status}",
            )

        try:
            await db.commit()
        except IntegrityError:
            # F-018-19: a rename can collide with the (model_id, name) unique
            # constraint just like create — return a 409 instead of a raw 500.
            await db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"A named set called '{updates.get('name')}' already exists in this model.",
            )
        await db.refresh(ns)
        return _decorate_named_set_response(
            ns, await _model_source_system(db, model_id),
        )
    raise HTTPException(status_code=500, detail="DB session exhausted")


@router.delete(
    "/{named_set_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[require_role("modeler")],
)
async def delete_named_set(
    project_id: UUID,
    model_id: UUID,
    named_set_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
) -> None:
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        # Bug-7982: serialise definition/governance writes against Save/revert.
        await acquire_model_definition_lock(db, model_id)
        ns = await db.get(NamedSet, named_set_id)
        if ns is None or ns.model_id != model_id:
            raise HTTPException(status_code=404, detail="Named set not found")
        # Purge the soft-referencing translation/preference rows that have no
        # FK back to the named set and would otherwise linger forever (F-029-15).
        await purge_entity_soft_references(db, model_id=model_id, entity_id=named_set_id)
        await db.delete(ns)
        await db.commit()


# ---- Validation endpoint (1C) ----

async def _get_model_metadata(db, model_id: UUID) -> dict:
    """Fetch dimensions and measures for validation context."""
    dims_q = await db.execute(
        select(Dimension).where(Dimension.model_id == model_id)
    )
    dims = [d.name for d in dims_q.scalars().all()]

    meas_q = await db.execute(
        select(Measure).where(Measure.model_id == model_id)
    )
    measures = [m.name for m in meas_q.scalars().all()]

    return {"dimensions": dims, "measures": measures}


_BLOCKED_MDX_FUNCTIONS = {
    "DRILLDOWNLEVEL", "DRILLUPLEVEL", "DRILLDOWNMEMBER",
    "DRILLDOWNMEMBERBOTTOM", "DRILLDOWNMEMBERTOP",
}


def _validate_expression(expression: str, model_metadata: dict) -> tuple[list[str], list[str]]:
    """Return (errors, warnings) for an MDX expression."""
    errors: list[str] = []
    warnings: list[str] = []

    if not expression or not expression.strip():
        errors.append("Expression is empty")
        return errors, warnings

    upper = expression.upper()
    for blocked in _BLOCKED_MDX_FUNCTIONS:
        if blocked in upper:
            errors.append(f"Function {blocked} is not allowed in named sets")

    if upper.strip().startswith("SELECT"):
        warnings.append("Expression looks like a SELECT query rather than a set expression")

    dim_names = {d.lower() for d in model_metadata.get("dimensions", [])}
    measure_names = {m.lower() for m in model_metadata.get("measures", [])}

    # F-018-11: member keys are written as `&[key]`. Strip those tokens first —
    # `re.findall(r"\[([^\]]+)\]")` captures only the text inside the brackets,
    # so the `&` never reaches the per-ref loop and every fixed-member key
    # previously produced a false "not found" warning. Member keys are arbitrary
    # source values and are not part of the model's dimension/measure metadata,
    # so they must not be validated as references.
    expr_without_keys = re.sub(r"&\[[^\]]*\]", "", expression)

    refs = re.findall(r"\[([^\]]+)\]", expr_without_keys)
    for ref in refs:
        if ref.lower() == "measures":
            continue
        if ref.lower() in measure_names or ref.lower() in dim_names:
            continue
        if ref.lower() in ("model", "members", "all"):
            continue
        warnings.append(f"Reference [{ref}] not found in model metadata")

    return errors, warnings


@router.post(
    "/validate",
    response_model=NamedSetValidateResponse,
    dependencies=[require_role("viewer")],
)
async def validate_named_set(
    project_id: UUID,
    model_id: UUID,
    body: NamedSetValidateRequest,
    current_user: CurrentUser = Depends(get_current_user),
) -> NamedSetValidateResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        model_metadata = await _get_model_metadata(db, model_id)
        expression = body.expression
        explanation = None
        compiled = None

        if body.builder_definition:
            bd_type = body.builder_definition.get("type") if isinstance(body.builder_definition, dict) else None
            # Bug-7940: sql_query is a sql_fixed-only type with no MDX compiler
            # entry. Handle it gracefully instead of erroring with
            # "Unsupported builder type". topN and filter work for both MDX
            # and sql_fixed, so let them through to the normal compile path.
            if bd_type == "sql_query":
                return NamedSetValidateResponse(
                    is_valid=True,
                    errors=[],
                    warnings=["Free-hand SQL definitions are validated at save time."],
                    explanation="Tessallite Named List (sql_query)",
                )
            try:
                expression = compile_definition(body.builder_definition, model_metadata)
                compiled = expression
                explanation = explain_definition(body.builder_definition)
            except CompilationError as exc:
                # Bug-7940: sql_fixed fixedMembers may fail compilation when
                # data_type is present but no hierarchy — handle gracefully.
                err_str = str(exc)
                if "Unsupported" in err_str or "data_type" in err_str:
                    return NamedSetValidateResponse(
                        is_valid=True,
                        errors=[],
                        warnings=[],
                        explanation="Tessallite Named List definition",
                    )
                return NamedSetValidateResponse(
                    is_valid=False,
                    errors=[err_str],
                )

        if not expression:
            return NamedSetValidateResponse(
                is_valid=False,
                errors=["No expression or builder_definition provided"],
            )

        errors, warnings = _validate_expression(expression, model_metadata)

        cost_band = "low"
        upper = expression.upper()
        if "TOPCOUNT" in upper or "BOTTOMCOUNT" in upper or "FILTER" in upper:
            cost_band = "medium"
        if "CROSSJOIN" in upper or "GENERATE" in upper:
            cost_band = "high"

        return NamedSetValidateResponse(
            is_valid=len(errors) == 0,
            errors=errors,
            warnings=warnings,
            explanation=explanation,
            compiled_expression=compiled,
            estimated_cost_band=cost_band,
        )
    raise HTTPException(status_code=500, detail="DB session exhausted")


# ---- Preview endpoints (1D) ----

_FILTER_OPS = {"=": "=", "!=": "!=", ">": ">", "<": "<", ">=": ">=", "<=": "<="}




async def _resolve_dim_name(db, model_id: UUID, raw: str) -> str | None:
    result = await db.execute(
        select(Dimension.name).where(Dimension.model_id == model_id, Dimension.name == raw)
    )
    return result.scalar_one_or_none()


async def _resolve_measure(db, model_id: UUID, raw: str) -> tuple[str, str] | None:
    result = await db.execute(
        select(Measure.name, Measure.default_agg).where(
            Measure.model_id == model_id, Measure.name == raw
        )
    )
    row = result.one_or_none()
    return (row[0], row[1]) if row else None


_AGG_FUNCS = {"sum", "count", "avg", "min", "max", "count_distinct"}


def _agg_expr(measure_name: str, default_agg: str) -> str:
    agg = default_agg.lower() if default_agg.lower() in _AGG_FUNCS else "sum"
    ident = _safe_ident(measure_name)
    if agg == "count_distinct":
        return f"COUNT(DISTINCT {ident})"
    return f"{agg.upper()}({ident})"


def _format_having_value(value: Any) -> str:
    """Render a HAVING literal whose value typing matches the MDX compiler
    (`named_list_compiler._compile_filter`) exactly.

    Bug-7256: the compiler quotes EVERY ``str`` value as a string literal —
    including a numeric-looking one like ``"1000"`` — and renders only genuine
    numeric (``int``/``float``) values bare. This preview formatter previously
    diverged: it ran ``float(text)`` on a string and, when that parsed, emitted
    the value BARE. That made the preview treat ``"1000"`` as a number while the
    deployed set treated it as a string, so the preview could silently
    contradict the deployed set on value typing. Mirror the compiler: bool →
    TRUE/FALSE, real numeric → bare, any string → quoted string literal.
    """
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    # Any string is a string literal, matching the compiler which quotes every
    # str value regardless of whether it looks numeric. Escape embedded quotes.
    text = str(value)
    return "'" + text.replace("'", "''") + "'"


def _build_filter_having(
    conditions: list[dict],
    measure_aggs: dict[str, str],
    logic: str = "AND",
) -> tuple[str, list[str], list[str]]:
    """Build a HAVING clause whose semantics match the compiled MDX
    ``Filter([entity].Members, [Measures].[field] op value)``.

    The MDX `Filter` over a measure threshold is an aggregate test per member,
    so the preview must aggregate the measure per entity and apply the test in
    HAVING (not a row-level WHERE). Conditions whose field is not a known
    measure are dropped and reported as warnings so the preview never silently
    contradicts the deployed set (F-018-02).

    Returns ``(having_clause, dropped_field_warnings, used_agg_exprs)``. The
    aggregate expressions are surfaced so the caller can also project them in
    the SELECT list — the query-router's semantic binder requires an aggregated
    GROUP BY query to carry its aggregate in SELECT (the working topN preview
    does the same), otherwise it rejects the bare grouped dimension.
    """
    clauses: list[str] = []
    dropped: list[str] = []
    used_aggs: list[str] = []
    for cond in conditions:
        field = cond.get("field", "")
        op = _FILTER_OPS.get(cond.get("operator", ""), None)
        value = cond.get("value", "")
        if not field or op is None or value == "":
            if field:
                dropped.append(field)
            continue
        agg = measure_aggs.get(field)
        if agg is None:
            dropped.append(field)
            continue
        clauses.append(f"{agg} {op} {_format_having_value(value)}")
        if agg not in used_aggs:
            used_aggs.append(agg)
    if not clauses:
        return "", dropped, used_aggs
    joiner = f" {logic} " if logic in ("AND", "OR") else " AND "
    return " HAVING " + joiner.join(clauses), dropped, used_aggs


async def _resolve_measure_aggs(db, model_id: UUID, fields: set[str]) -> dict[str, str]:
    """Map each requested field that is a real measure to its aggregate SQL
    expression (e.g. ``SUM("total_sales")``), used to build the HAVING clause
    so the preview's aggregate semantics match the compiled MDX `Filter`."""
    fields = {f for f in fields if f}
    if not fields:
        return {}
    result = await db.execute(
        select(Measure.name, Measure.default_agg).where(
            Measure.model_id == model_id, Measure.name.in_(fields)
        )
    )
    return {name: _agg_expr(name, default_agg or "sum") for name, default_agg in result.all()}


async def _preview_from_builder(
    bd: dict | None,
    expression: str | None,
    model_id: UUID,
    model_slug: str,
    bearer: str,
    db,
    persona_id: str | None = None,
) -> tuple[list[dict], str | None, list[str], int, bool]:
    explanation = None
    items: list[dict] = []
    warnings: list[str] = []
    # Bug-7257: count/truncation MUST be computed over the FULL member set (or
    # the full row set the server-capped query returned), NOT over the 100-row
    # preview slice below. Deriving them from the slice loses any total above
    # 100 and mis-flags an exactly-100 set as truncated.
    total_count = 0
    truncated = False

    if bd:
        try:
            explanation = explain_definition(bd)
        except Exception:
            pass

    if bd and bd.get("type") == "fixedMembers":
        members = bd.get("members", [])
        # Bug-7257: full declared count/truncation, computed BEFORE the
        # members[:100] preview slice below.
        total_count = len(members)
        truncated = len(members) > 100
        # Bug-5257: a member can be a plain string or a dict with key/caption
        # fields. Extract the string values so both fields are always strings,
        # not a whole dict.
        resolved: list[dict] = []
        for i, m in enumerate(members[:100]):
            if isinstance(m, dict):
                key = str(m.get("key", ""))
                caption = str(m.get("caption", key))
            else:
                key = str(m)
                caption = key
            resolved.append({"ordinal": i + 1, "caption": caption, "key": key})
        items = resolved
    elif bd and bd.get("type") == "topN":
        dim = await _resolve_dim_name(db, model_id, bd.get("entity", ""))
        measure_info = await _resolve_measure(db, model_id, bd.get("measure", ""))
        count = bd.get("count", 10)
        direction = "DESC" if bd.get("direction", "top") == "top" else "ASC"
        if dim and measure_info:
            measure_name, default_agg = measure_info
            agg = _agg_expr(measure_name, default_agg)
            sql = (
                f"SELECT {_safe_ident(dim)}, {agg} AS _rank_val "
                f"FROM {_safe_ident(model_slug)} "
                f"GROUP BY {_safe_ident(dim)} "
                f"ORDER BY _rank_val {direction} LIMIT {int(count)}"
            )
            try:
                result = await _execute_via_router(model_id, sql, bearer, persona_id=persona_id)
                rows = result.get("rows", [])
                # Bug-7257: count/truncation over the rows the server-capped
                # query actually returned, BEFORE the rows[:100] preview slice.
                total_count = len(rows)
                truncated = len(rows) > 100
                for i, row in enumerate(rows[:100]):
                    if isinstance(row, dict):
                        val = next(iter(row.values()), "")
                        items.append({"ordinal": i + 1, "caption": str(val), "key": str(val)})
            except RowSecurityDeniedError:
                # Bug-8453: a governance denial is not a broken expression.
                # Must be caught BEFORE the generic handler below, which would
                # tell the modeller to "fix the underlying expression".
                warnings.append(
                    "Row-level security denied you access to every row, so no "
                    "members could be previewed. This does NOT mean the set is "
                    "empty for other users — it reflects your own row-security "
                    "permissions on this model. Ask an administrator if you "
                    "believe you should have access."
                )
            except Exception as exc:
                # Bug-5927/Bug-5702 (F-018-GPT-02): a router execution
                # failure must surface as a visible preview warning, not a
                # silent "0 members" that looks like a legitimately empty
                # set. Swallowing this let a broken dynamic set be saved,
                # certified, or published with false confidence.
                log.warning("Preview query failed: %s", exc)
                warnings.append(
                    "Preview could not run the top-N query against the source: "
                    f"{exc}. This does not mean the set has zero members — "
                    "fix the underlying expression and preview again."
                )
    elif bd and bd.get("type") == "filter":
        dim = await _resolve_dim_name(db, model_id, bd.get("entity", ""))
        if dim:
            conditions = bd.get("conditions", [])
            cond_fields = {c.get("field", "") for c in conditions}
            # The compiler emits each condition field as `[Measures].[field]`
            # with aggregate-threshold semantics, so resolve fields against
            # measures and apply the test in HAVING over a GROUP BY entity —
            # the same membership the deployed MDX `Filter` produces (F-018-02).
            measure_aggs = await _resolve_measure_aggs(db, model_id, cond_fields)
            having, dropped, used_aggs = _build_filter_having(
                conditions, measure_aggs, bd.get("logic", "AND")
            )
            if dropped:
                warnings.append(
                    "These condition field(s) are not measures and were ignored "
                    "in the preview, which may differ from the deployed set: "
                    + ", ".join(sorted(set(dropped)))
                )
            entity_ident = _safe_ident(dim)
            if having:
                # Project the aggregate(s) alongside the entity so the binder
                # accepts the grouped query (same shape as the topN preview);
                # only the first column (the entity) is read into members.
                select_aggs = "".join(
                    f", {agg} AS _agg_{i}" for i, agg in enumerate(used_aggs)
                )
                sql = (
                    f"SELECT {entity_ident}{select_aggs} "
                    f"FROM {_safe_ident(model_slug)} "
                    f"GROUP BY {entity_ident}{having} LIMIT 100"
                )
            else:
                # No applicable condition survived — fall back to the unfiltered
                # member list, but the warning above tells the modeller why.
                sql = (
                    f"SELECT DISTINCT {entity_ident} "
                    f"FROM {_safe_ident(model_slug)} LIMIT 100"
                )
            try:
                result = await _execute_via_router(model_id, sql, bearer, persona_id=persona_id)
                rows = result.get("rows", [])
                # Bug-7257: count/truncation over the rows the server-capped
                # query actually returned, BEFORE the rows[:100] preview slice.
                total_count = len(rows)
                truncated = len(rows) > 100
                for i, row in enumerate(rows[:100]):
                    if isinstance(row, dict):
                        val = next(iter(row.values()), "")
                        items.append({"ordinal": i + 1, "caption": str(val), "key": str(val)})
            except RowSecurityDeniedError:
                # Bug-8453: same rationale as the topN branch — a permissions
                # denial must not be reported as a broken expression.
                warnings.append(
                    "Row-level security denied you access to every row, so no "
                    "members could be previewed. This does NOT mean the set is "
                    "empty for other users — it reflects your own row-security "
                    "permissions on this model. Ask an administrator if you "
                    "believe you should have access."
                )
            except Exception as exc:
                # Bug-5927/Bug-5702 (F-018-GPT-02): same rationale as the
                # topN branch above — surface the failure as a visible
                # warning instead of a silent empty-set preview.
                log.warning("Preview query failed: %s", exc)
                warnings.append(
                    "Preview could not run the filter query against the source: "
                    f"{exc}. This does not mean the set has zero members — "
                    "fix the underlying expression and preview again."
                )
    else:
        if not explanation and expression:
            explanation = "Preview is not supported for raw/advanced MDX expressions. Deploy the model and use a BI tool (Excel, Power BI) with an XMLA connection to see member results."

    return items, explanation, warnings, total_count, truncated


@router.post(
    "/preview-by-definition",
    response_model=NamedSetPreviewResponse,
    dependencies=[require_role("viewer")],
)
async def preview_named_set_by_definition(
    project_id: UUID,
    model_id: UUID,
    body: NamedSetValidateRequest,
    request: Request,
    persona_id: UUID | None = Query(default=None),
    current_user: CurrentUser = Depends(get_current_user),
) -> NamedSetPreviewResponse:
    """Preview a named set from expression/builder_definition without saving."""
    async for db in get_tenant_db(current_user.tenant_id):
        model = await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        if body.builder_definition:
            bd_type = body.builder_definition.get("type") if isinstance(body.builder_definition, dict) else None
            # Bug-7940: sql_query is sql_fixed-only with no MDX compiler entry.
            # topN and filter work for both MDX and sql_fixed, so compile them.
            if bd_type != "sql_query":
                try:
                    compile_definition(body.builder_definition)
                except CompilationError as exc:
                    # If the error is about unsupported type (sql_fixed-only
                    # builder shape like fixedMembers with data_type), suppress it.
                    err_str = str(exc)
                    if "Unsupported" not in err_str:
                        raise HTTPException(status_code=422, detail=err_str)
        bearer = request.headers.get("Authorization", "").replace("Bearer ", "")
        # Bug-5963: thread persona through the dynamic (topN/filter) preview
        # query so it runs under the same row-level security as a saved
        # named-set preview and KPI/plugin execution.
        items, explanation, warnings, total_count, truncated = await _preview_from_builder(
            body.builder_definition, body.expression, model_id, model.slug, bearer, db,
            persona_id=str(persona_id) if persona_id else None,
        )
        return NamedSetPreviewResponse(
            items=items,
            total_count=total_count,
            truncated=truncated,
            explanation=explanation,
            warnings=warnings,
        )
    raise HTTPException(status_code=500, detail="DB session exhausted")


@router.post(
    "/{named_set_id}/preview",
    response_model=NamedSetPreviewResponse,
    dependencies=[require_role("viewer")],
)
async def preview_named_set(
    project_id: UUID,
    model_id: UUID,
    named_set_id: UUID,
    request: Request,
    persona_id: UUID | None = Query(default=None),
    deployed_only: bool = Query(
        default=False,
        description=(
            "When true, compute the preview from the named set's DEPLOYED "
            "snapshot definition instead of the live row, and withhold a set "
            "the deployed version does not contain. Consumption surfaces pass "
            "this (the Excel task pane, TESSALLITE.LISTBYID, the conversational "
            "agent); the model builder omits it so modellers can preview the "
            "draft they are editing (Bug-8712, same contract as F-017-05)."
        ),
    ),
    current_user: CurrentUser = Depends(get_current_user),
) -> NamedSetPreviewResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        model = await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        ns = await db.get(NamedSet, named_set_id)
        if ns is None or ns.model_id != model_id:
            raise HTTPException(status_code=404, detail="Named set not found")

        # Bug-8712: the preview endpoint writes MEMBERS straight into worksheet
        # cells (TESSALLITE.LISTBYID) and into the agent's answer, so an
        # unpinned read is not a catalogue-metadata leak — it changes the values
        # a consumer sees the instant a modeller edits an expression, with no
        # Deploy. The list route's `deployed_only` flag cannot reach this path:
        # it resolves a DEFINITION, and this route reads `builder_definition` /
        # `expression` off the live row to build its query. So resolve the same
        # way the list route does, through the one shared authority, before any
        # SQL/MDX is built. Root contract: "the deployed snapshot is the
        # contract; the live state is editor-only" (F-013-01,
        # architecture_model-versioning-and-deploy.md).
        if deployed_only:
            try:
                resolved, _withheld = await resolve_served_named_sets(db, model, [ns])
            except NamedSetSnapshotInvalidError as exc:
                # Fail closed. A fallback to the live row IS the leak being
                # closed here (Bug-8384 made this deliberate).
                raise HTTPException(
                    status_code=409,
                    detail=f"{NamedSetSnapshotInvalidError.error_code}: {exc}",
                )
            if not resolved:
                log.info(
                    "Named set %s withheld from a consumption preview for model "
                    "%s: not present in deployed version %s (Deploy the model to "
                    "publish it).",
                    named_set_id, model_id, model.deployed_version_id,
                )
                raise HTTPException(status_code=404, detail="Named set not found")
            ns = resolved[0].named_set

        # Bug-5963: same 404-on-out-of-scope gate KPI evaluation uses --
        # a persona-scoped user cannot preview a named set built on a
        # dimension outside their persona.
        persona = await resolve_effective_persona(
            db, current_user=current_user, model_id=model_id,
            requested_persona_id=persona_id,
        )
        if persona:
            allowed = parse_allowed_ids(persona.included_dimension_ids)
            if allowed is not None and not await _named_set_visible_to_persona(
                db, ns, model_id, allowed
            ):
                raise HTTPException(status_code=404, detail="Named set not found")

        bearer = request.headers.get("Authorization", "").replace("Bearer ", "")
        # Bug-5963: thread persona through the dynamic (topN/filter) preview
        # query so it runs under the same row-level security as KPI and
        # plugin execution instead of the unrestricted default context.
        items, explanation, warnings, total_count, truncated = await _preview_from_builder(
            ns.builder_definition, ns.expression, model_id, model.slug, bearer, db,
            persona_id=str(persona_id) if persona_id else None,
        )
        return NamedSetPreviewResponse(
            items=items,
            total_count=total_count,
            truncated=truncated,
            explanation=explanation,
            warnings=warnings,
        )
    raise HTTPException(status_code=500, detail="DB session exhausted")


# ---- Refresh endpoint (v1b: compute-and-store) ----

# Bug-7938/Bug-7926: removed _REFRESH_MEMBER_CAP = 5000 — refresh now uses the
# same unified cap as create/update: min(NAMED_LIST_MEMBER_CAP, CEILING).


async def _build_refresh_sql(
    bd: dict,
    model_id: UUID,
    model_slug: str,
    db,
    *,
    member_cap: int = 1000,
) -> str:
    """Build the SQL query for a dynamic definition type's refresh.

    Reuses the same SQL construction logic as ``_preview_from_builder`` but
    without the 100-row truncation — refresh needs all qualifying members.
    """
    btype = bd.get("type")

    if btype == "topN":
        dim = await _resolve_dim_name(db, model_id, bd.get("entity", ""))
        measure_info = await _resolve_measure(db, model_id, bd.get("measure", ""))
        count = int(bd.get("count", 10))
        # Bug-7945: validate topN count upper bound.
        if count > member_cap:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"topN count ({count}) exceeds the member cap ({member_cap}). "
                    f"Reduce the count or increase the configured cap."
                ),
            )
        direction = "DESC" if bd.get("direction", "top") == "top" else "ASC"
        if not dim:
            raise HTTPException(status_code=422, detail="Dimension not found in this model.")
        if not measure_info:
            raise HTTPException(status_code=422, detail="Measure not found in this model.")
        measure_name, default_agg = measure_info
        agg = _agg_expr(measure_name, default_agg)
        return (
            f"SELECT {_safe_ident(dim)}, {agg} AS _rank_val "
            f"FROM {_safe_ident(model_slug)} "
            f"GROUP BY {_safe_ident(dim)} "
            f"ORDER BY _rank_val {direction} LIMIT {count}"
        )

    if btype == "filter":
        dim = await _resolve_dim_name(db, model_id, bd.get("entity", ""))
        if not dim:
            raise HTTPException(status_code=422, detail="Dimension not found in this model.")
        conditions = bd.get("conditions", [])
        cond_fields = {c.get("field", "") for c in conditions}
        measure_aggs = await _resolve_measure_aggs(db, model_id, cond_fields)
        having, dropped, used_aggs = _build_filter_having(
            conditions, measure_aggs, bd.get("logic", "AND")
        )
        # Bug-7939: fail closed — if ANY condition was dropped or unknown,
        # reject the refresh rather than silently broadening membership.
        if dropped:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Refresh cannot proceed: condition field(s) "
                    f"{', '.join(sorted(set(dropped)))} are not recognised measures. "
                    f"Fix the filter definition and retry."
                ),
            )
        entity_ident = _safe_ident(dim)
        if not having:
            # Bug-7939: zero surviving conditions = forbidden fallback.
            raise HTTPException(
                status_code=422,
                detail=(
                    "Refresh cannot proceed: no filter conditions survived validation. "
                    "At least one condition with a valid measure and operator is required."
                ),
            )
        select_aggs = "".join(
            f", {agg} AS _agg_{i}" for i, agg in enumerate(used_aggs)
        )
        # Bug-7945: LIMIT pushdown — fetch at most cap+1 so over-cap
        # rejection is preserved without streaming unbounded results.
        return (
            f"SELECT {entity_ident}{select_aggs} "
            f"FROM {_safe_ident(model_slug)} "
            f"GROUP BY {entity_ident}{having} "
            f"LIMIT {member_cap + 1}"
        )

    if btype == "sql_query":
        query = bd.get("query", "").strip()
        if not query:
            raise HTTPException(status_code=422, detail="sql_query has no query defined.")
        # Bug-7947: defense-in-depth DML/SELECT* re-scan at refresh time
        # (imported/legacy definitions may bypass create-time validation).
        if _DML_KEYWORDS_RE.search(query):
            raise HTTPException(
                status_code=422,
                detail="sql_query must be a read-only SELECT statement. DML keywords are not allowed.",
            )
        if _SELECT_STAR_RE.search(query):
            raise HTTPException(
                status_code=422,
                detail="sql_query must name specific columns (SELECT * is not allowed).",
            )
        # Bug-7945: wrap in a LIMIT for sql_query too.
        # Always wrap in a subquery with LIMIT to bound result set size,
        # regardless of whether the user wrote a LIMIT — the outer cap
        # is the defense-in-depth bound.
        query = f"SELECT * FROM ({query}) AS _sq LIMIT {member_cap + 1}"
        return query

    raise HTTPException(status_code=400, detail=f"Cannot refresh definition type '{btype}'.")


@router.post(
    "/{named_set_id}/refresh",
    response_model=NamedSetResponse,
    dependencies=[require_role("modeler")],
)
async def refresh_named_list(
    project_id: UUID,
    model_id: UUID,
    named_set_id: UUID,
    request: Request,
    current_user: CurrentUser = Depends(get_current_user),
) -> NamedSetResponse:
    """Refresh a dynamic named list by executing its definition query.

    Computes members from the source data and stores them. Only valid for
    dynamic definition types (topN, filter, sql_query). Fixed-member lists
    cannot be refreshed.
    """
    async for db in get_tenant_db(current_user.tenant_id):
        model = await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        ns = await db.get(NamedSet, named_set_id)
        if ns is None or ns.model_id != model_id:
            raise HTTPException(status_code=404, detail="Named set not found")

        if ns.list_type != "sql_fixed":
            raise HTTPException(
                status_code=400,
                detail="Only Tessallite Named Lists (sql_fixed) support refresh.",
            )

        bd = ns.builder_definition
        if not isinstance(bd, dict):
            raise HTTPException(status_code=400, detail="Named list has no builder definition.")

        btype = bd.get("type")
        if btype not in _DYNAMIC_BUILDER_TYPES:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Refresh is only available for dynamic definition types "
                    f"(topN, filter, sql_query). This list uses '{btype}'."
                ),
            )

        # Bug-7938/Bug-7926: unified cap — same as create/update.
        member_cap = min(
            _settings.NAMED_LIST_MEMBER_CAP,
            _settings.NAMED_LIST_MEMBER_CAP_CEILING,
        )

        bearer = request.headers.get("Authorization", "").replace("Bearer ", "")
        sql = await _build_refresh_sql(
            bd, model_id, model.slug, db, member_cap=member_cap,
        )

        try:
            result = await _execute_via_router(model_id, sql, bearer, timeout_s=60.0)
        except RowSecurityDeniedError:
            # Bug-8453 [fail closed]: a deny-all returns HTTP 200 with zero
            # rows. Treating that as the refresh result would OVERWRITE a good
            # named list with an empty membership and persist it for every
            # user — a destructive write caused purely by the refreshing
            # caller's own permissions. Same "preserve members" contract as
            # Bug-7939, but reported as the permissions problem it is.
            raise HTTPException(
                status_code=403,
                detail=(
                    "Row-level security denies you access to every row of this "
                    "model, so the member list cannot be refreshed from your "
                    "account. The existing members have been left unchanged. "
                    "Ask an administrator to refresh it, or to grant you access."
                ),
            )
        except Exception as exc:
            # Bug-7939: preserve members on failure.
            raise HTTPException(
                status_code=502,
                detail=f"Refresh query failed: {exc}",
            )

        rows = result.get("rows", [])
        raw_values: list[Any] = []
        for row in rows:
            if isinstance(row, dict):
                val = next(iter(row.values()), None)
                if val is not None:
                    raw_values.append(val)
            elif isinstance(row, (list, tuple)) and row:
                if row[0] is not None:
                    raw_values.append(row[0])

        data_type = bd.get("data_type", "string")
        # Bug-7939/Bug-7958: fail closed — reject non-numeric values
        # instead of silently skipping them (wrong-numbers class).
        if data_type == "number":
            normalized: list[Any] = []
            for i, v in enumerate(raw_values, start=1):
                if isinstance(v, bool):
                    raise HTTPException(
                        status_code=422,
                        detail=(
                            f"Refresh returned a boolean value at row {i}. "
                            f"All values must be numeric for a number-typed list."
                        ),
                    )
                if isinstance(v, (int, float)):
                    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                        raise HTTPException(
                            status_code=422,
                            detail=(
                                f"Refresh returned a non-finite value ({v!r}) at row {i}. "
                                f"NaN and Infinity are not valid numeric members."
                            ),
                        )
                    normalized.append(v)
                    continue
                # String value — attempt numeric parse.
                try:
                    normalized.append(int(str(v)))
                    continue
                except (ValueError, TypeError):
                    pass
                try:
                    num = float(str(v))
                except (ValueError, TypeError, OverflowError):
                    raise HTTPException(
                        status_code=422,
                        detail=(
                            f"Refresh returned a non-numeric value ({v!r}) at row {i}. "
                            f"All values must be numeric for a number-typed list."
                        ),
                    )
                if not math.isfinite(num):
                    raise HTTPException(
                        status_code=422,
                        detail=(
                            f"Refresh returned a non-finite value ({v!r}) at row {i}. "
                            f"NaN and Infinity are not valid numeric members."
                        ),
                    )
                normalized.append(num)
            raw_values = normalized

        # Bug-7946: per-member validation (control chars, length cap) —
        # same hygiene as fixedMembers create-time validation.
        for i, v in enumerate(raw_values, start=1):
            text = str(v) if v is not None else ""
            if len(text) > _MEMBER_VALUE_MAX_LEN:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"Refresh returned a value at row {i} exceeding the "
                        f"{_MEMBER_VALUE_MAX_LEN}-character limit."
                    ),
                )
            if _CONTROL_CHARS.search(text):
                raise HTTPException(
                    status_code=422,
                    detail=f"Refresh returned a value at row {i} containing control characters.",
                )

        seen: set = set()
        distinct: list[Any] = []
        for v in raw_values:
            key = str(v)
            if key not in seen:
                seen.add(key)
                distinct.append(v)
        members = distinct

        if len(members) > member_cap:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Refresh returned {len(members)} distinct values, exceeding "
                    f"the {member_cap} member cap. Narrow the definition to reduce results."
                ),
            )

        # Bug-7943: zero-row refresh succeeds but surfaces a warning in the
        # version summary so the UI / history can distinguish "definition
        # legitimately matches nothing" from "never refreshed".
        summary = f"Refreshed: {len(members)} members computed"
        if len(members) == 0:
            summary = "Refreshed: 0 members computed (definition returned no matching rows)"

        # Bug-7982 (opus5 finding 4): the refresh rewrites the list members
        # (definition), so it must serialise with Save/revert. Acquire the lock
        # ONLY now — AFTER the up-to-60s source query has returned — never across
        # that external call, so a slow refresh cannot pin the per-model lock and
        # block every other writer on the model for up to a minute. (The pooled
        # connection is held for the whole request regardless; the lock is the
        # cross-request serialiser this defers.)
        await acquire_model_definition_lock(db, model_id)
        # Re-read the committed definition under the lock. If the set was edited
        # (or a model revert restored a different definition) DURING the refresh
        # query, the members we just computed are against a stale definition —
        # writing them would silently clobber the concurrent change and persist
        # members matching neither definition (the Bug-7982 lost-write class).
        try:
            await db.refresh(ns)
        except ObjectDeletedError:
            # The set was deleted (e.g. dropped by a concurrent revert) — nothing
            # to refresh into. This is a real conflict, not an infra fault.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "The named list was removed while the refresh was running. "
                    "Re-run the refresh."
                ),
            )
        except Exception:
            # An infrastructure fault (connection reset, timeout, ...) must NOT be
            # mislabelled as a conflict that tells the user to retry forever.
            log.exception(
                "named-set refresh: re-reading %s under the lock failed", named_set_id
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Could not finalise the refresh due to a backend error.",
            )
        current_bd = ns.builder_definition if isinstance(ns.builder_definition, dict) else None
        if current_bd != bd:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "The named list definition changed while the refresh was "
                    "running. Re-run the refresh so the members match the current "
                    "definition."
                ),
            )

        # Build the new definition from the RE-READ committed value, never the
        # pre-call copy, so nothing is silently overwritten.
        updated_bd = dict(current_bd)
        updated_bd["members"] = members
        updated_bd["last_refreshed_at"] = datetime.now(timezone.utc).isoformat()
        ns.builder_definition = updated_bd

        await _create_version(db, ns, current_user.email, summary)
        await db.commit()
        await db.refresh(ns)
        return _decorate_named_set_response(
            ns, await _model_source_system(db, model_id),
        )
    raise HTTPException(status_code=500, detail="DB session exhausted")


# ---- Version History endpoints (Phase 4) ----

@router.get(
    "/{named_set_id}/versions",
    response_model=list[VersionResponse],
    dependencies=[require_role("viewer")],
)
async def list_named_set_versions(
    project_id: UUID,
    model_id: UUID,
    named_set_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
) -> list[VersionResponse]:
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        ns = await db.get(NamedSet, named_set_id)
        if ns is None or ns.model_id != model_id:
            raise HTTPException(status_code=404, detail="Named set not found")
        result = await db.execute(
            select(NamedSetVersion)
            .where(NamedSetVersion.named_set_id == named_set_id)
            .order_by(NamedSetVersion.version_number.desc())
        )
        return [VersionResponse.model_validate(v, from_attributes=True) for v in result.scalars().all()]
    return []


@router.post(
    "/{named_set_id}/versions/{version_number}/revert",
    response_model=NamedSetResponse,
    dependencies=[require_role("modeler")],
)
async def revert_named_set_version(
    project_id: UUID,
    model_id: UUID,
    named_set_id: UUID,
    version_number: int,
    current_user: CurrentUser = Depends(get_current_user),
) -> NamedSetResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        # Bug-7982: serialise definition/governance writes against Save/revert.
        await acquire_model_definition_lock(db, model_id)
        ns = await db.get(NamedSet, named_set_id)
        if ns is None or ns.model_id != model_id:
            raise HTTPException(status_code=404, detail="Named set not found")
        result = await db.execute(
            select(NamedSetVersion)
            .where(NamedSetVersion.named_set_id == named_set_id)
            .where(NamedSetVersion.version_number == version_number)
        )
        version = result.scalar_one_or_none()
        if version is None:
            raise HTTPException(status_code=404, detail=f"Version {version_number} not found")

        snap = version.snapshot

        # Bug-5931/Bug-5709 (F-018-GPT-07/F-018-R14): a historical snapshot
        # is not automatically valid against TODAY's model — dimensions,
        # hierarchies, or measures the snapshot referenced may since have
        # been renamed or removed. Run the same current-model validation
        # `validate_named_set` uses (not a parallel/stricter check) before
        # committing the restore, so a hard-invalid expression (a blocked
        # MDX function, or an empty expression) is rejected with a clear
        # error instead of silently becoming the set's live definition —
        # only discovered later by preview or a BI client.
        restored_expression = snap.get("expression")
        restored_builder_definition = snap.get("builder_definition")
        restored_list_type = snap.get("list_type", ns.list_type)
        model_metadata = await _get_model_metadata(db, model_id)
        # Bug-7940: sql_fixed lists do not compile to MDX — skip the
        # compile_definition call (same as _auto_compile and update path).
        if restored_list_type == "sql_fixed":
            restored_expression = ""
        elif restored_builder_definition:
            try:
                restored_expression = compile_definition(
                    restored_builder_definition, model_metadata,
                )
            except CompilationError as exc:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Cannot revert to version {version_number}: its builder "
                        f"definition no longer compiles against the current model: {exc}"
                    ),
                ) from exc
        revert_warnings: list[str] = []
        if restored_expression:
            revert_errors, revert_warnings = _validate_expression(
                restored_expression, model_metadata,
            )
            if revert_errors:
                # Plain-string detail, matching every other error response in
                # this file — the frontend's error state is string-typed and
                # renders `detail` directly, so a dict here would crash the
                # panel (React "objects are not valid as a child").
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Cannot revert to version {version_number}: its "
                        "expression is not valid against the current model: "
                        + "; ".join(revert_errors)
                    ),
                )

        # F-018-05: a revert restores the *definition*, never the governance
        # state. `certification_status` is deliberately excluded so a modeler
        # cannot re-certify a set by reverting to a snapshot that was certified
        # before an admin deprecated it (certify/deprecate require admin). The
        # set keeps its current certification status; if the reverted definition
        # differs from the certified one, the standard auto-demote in
        # `update_named_set` does not apply here, so we demote a certified set to
        # draft when the restored definition is not identical.
        prior_status = ns.certification_status
        restorable = (
            "name", "display_name", "description", "display_folder",
            "expression", "builder_definition", "list_type", "scope", "dimensions",
        )
        definition_changed = any(
            k in snap and getattr(ns, k) != snap[k] for k in restorable
        )
        for k in restorable:
            if k in snap:
                setattr(ns, k, snap[k])
        if definition_changed and prior_status in ("certified", "shared"):
            ns.certification_status = "draft"

        # Bug-5931: a passable-but-suspect restore (e.g. a dimension/measure
        # reference the current model no longer has, which _validate_expression
        # only warns on — see validate_named_set's own warning/error split)
        # must still be visible somewhere, since NamedSetResponse has no
        # warnings field. Record it in the version history summary, which the
        # UI already surfaces per version, rather than silently proceeding.
        summary = f"Reverted to version {version_number}"
        if revert_warnings:
            summary += " (warnings: " + "; ".join(revert_warnings) + ")"
        await _create_version(db, ns, current_user.email, summary)
        await db.commit()
        await db.refresh(ns)
        return _decorate_named_set_response(
            ns, await _model_source_system(db, model_id),
        )
    raise HTTPException(status_code=500, detail="DB session exhausted")


# ---- Certification endpoints (Phase 4) ----

@router.post(
    "/{named_set_id}/certify",
    response_model=NamedSetResponse,
    dependencies=[require_role("admin")],
)
async def certify_named_set(
    project_id: UUID,
    model_id: UUID,
    named_set_id: UUID,
    _body: CertifyRequest,
    current_user: CurrentUser = Depends(get_current_user),
) -> NamedSetResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        # Bug-7982: serialise governance writes against Save/revert.
        await acquire_model_definition_lock(db, model_id)
        ns = await db.get(NamedSet, named_set_id)
        if ns is None or ns.model_id != model_id:
            raise HTTPException(status_code=404, detail="Named set not found")
        if ns.certification_status == "certified":
            return _decorate_named_set_response(
                ns, await _model_source_system(db, model_id),
            )
        ns.certification_status = "certified"
        await _create_version(db, ns, current_user.email, "Certified")
        await db.commit()
        await db.refresh(ns)
        return _decorate_named_set_response(
            ns, await _model_source_system(db, model_id),
        )
    raise HTTPException(status_code=500, detail="DB session exhausted")


@router.post(
    "/{named_set_id}/deprecate",
    response_model=NamedSetResponse,
    dependencies=[require_role("admin")],
)
async def deprecate_named_set(
    project_id: UUID,
    model_id: UUID,
    named_set_id: UUID,
    body: DeprecateRequest,
    current_user: CurrentUser = Depends(get_current_user),
) -> NamedSetResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        # Bug-7982: serialise governance writes against Save/revert.
        await acquire_model_definition_lock(db, model_id)
        ns = await db.get(NamedSet, named_set_id)
        if ns is None or ns.model_id != model_id:
            raise HTTPException(status_code=404, detail="Named set not found")
        # Bug-5259: validate that replacement_id (if supplied) references
        # an existing named set in the same model. Without this check, a
        # caller could point the deprecation at a nonexistent or cross-model
        # named set, producing a dangling reference.
        if body.replacement_id is not None:
            replacement = await db.get(NamedSet, body.replacement_id)
            if replacement is None or replacement.model_id != model_id:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "replacement_id must reference an existing named set "
                        "in the same model."
                    ),
                )
        ns.certification_status = "deprecated"
        ns.replacement_id = body.replacement_id
        summary = "Deprecated"
        if body.replacement_id:
            summary += f" (replacement: {body.replacement_id})"
        await _create_version(db, ns, current_user.email, summary)
        await db.commit()
        await db.refresh(ns)
        return _decorate_named_set_response(
            ns, await _model_source_system(db, model_id),
        )
    raise HTTPException(status_code=500, detail="DB session exhausted")


# ---- Usage Tracking endpoint (Phase 5) ----

@router.post(
    "/{named_set_id}/usage",
    response_model=EntityUsageResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[require_role("viewer")],
)
async def report_named_set_usage(
    project_id: UUID,
    model_id: UUID,
    named_set_id: UUID,
    body: EntityUsageCreate,
    current_user: CurrentUser = Depends(get_current_user),
) -> EntityUsageResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        ns = await db.get(NamedSet, named_set_id)
        if ns is None or ns.model_id != model_id:
            raise HTTPException(status_code=404, detail="Named set not found")
        usage = NamedSetUsage(
            id=uuid4(),
            named_set_id=named_set_id,
            workbook_id=body.workbook_id,
            worksheet=body.worksheet,
            cell_reference=body.cell_reference,
            usage_type=body.usage_type,
            reported_by=current_user.email,
            reported_at=datetime.now(timezone.utc),
        )
        db.add(usage)
        try:
            await db.commit()
        except IntegrityError:
            # opus5 finding 8: usage is fire-and-forget telemetry. Its insert
            # takes FOR KEY SHARE on the parent set, so a concurrent model revert
            # (which deletes + reinserts the set) can make the FK check transiently
            # fail. Telemetry must NEVER surface a 500 to the Excel client — swallow
            # it and acknowledge the report from the in-memory record.
            await db.rollback()
            log.warning(
                "named-set usage insert for %s lost to a concurrent revert; "
                "acknowledged without persisting.", named_set_id,
            )
            return EntityUsageResponse.model_validate(usage, from_attributes=True)
        await db.refresh(usage)
        return EntityUsageResponse.model_validate(usage, from_attributes=True)
    raise HTTPException(status_code=500, detail="DB session exhausted")


@router.get(
    "/{named_set_id}/usage",
    response_model=list[EntityUsageResponse],
    dependencies=[require_role("viewer")],
)
async def list_named_set_usage(
    project_id: UUID,
    model_id: UUID,
    named_set_id: UUID,
    current_user: CurrentUser = Depends(get_current_user),
) -> list[EntityUsageResponse]:
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(db, project_id=project_id, model_id=model_id)
        ns = await db.get(NamedSet, named_set_id)
        if ns is None or ns.model_id != model_id:
            raise HTTPException(status_code=404, detail="Named set not found")
        result = await db.execute(
            select(NamedSetUsage)
            .where(NamedSetUsage.named_set_id == named_set_id)
            .order_by(NamedSetUsage.reported_at.desc())
        )
        return [EntityUsageResponse.model_validate(u, from_attributes=True) for u in result.scalars().all()]
    return []


async def _execute_via_router(
    model_id: UUID,
    query: str,
    bearer: str,
    timeout_s: float = 30.0,
    persona_id: str | None = None,
) -> dict:
    """POST a SQL query to the query-router's /execute endpoint.

    Bug-8453: raises :class:`RowSecurityDeniedError` when the router reports the
    deny-all row-security sentinel. Without this the caller sees an ordinary
    HTTP 200 with zero rows and cannot distinguish "this set genuinely has no
    members" from "you are not permitted to see any of them" — and on the
    refresh path it would PERSIST that empty membership over a good list.

    Bug-5963: *persona_id* is forwarded so a dynamic (topN/filter) named-set
    preview is executed through the same effective-persona path as KPI
    evaluation and plugin execution -- previously this always ran
    persona-blind, so a preview could surface a member universe the active
    persona's row-level security would otherwise exclude.
    """
    url = f"{_settings.QUERY_ROUTER_URL}/api/v1/execute"
    headers = {"Authorization": f"Bearer {bearer}"}
    body: dict = {
        "model_id": str(model_id),
        "raw_query": query,
        "protocol": "jdbc",
    }
    if persona_id is not None:
        body["persona_id"] = persona_id
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        resp = await client.post(url, json=body, headers=headers)
        if resp.status_code >= 400:
            try:
                payload = resp.json()
                detail = payload.get("detail") if isinstance(payload, dict) else None
                if not isinstance(detail, str) or not detail:
                    detail = resp.text
            except Exception:
                detail = resp.text or f"Query router returned HTTP {resp.status_code}"
            raise ValueError(detail)
        payload = resp.json()
        # Bug-8453: classify through the shared execute contract, never by
        # testing whether ``rows`` is empty.
        if execute_response_denied_all(payload):
            raise RowSecurityDeniedError(
                "Row-level security denied access to every row for this query."
            )
        return payload
