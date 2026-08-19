"""Row-security rule CRUD + simulate-as-user preview (Phase 5.1.D).

Two rule shapes are stored in a single table (see
``shared/db/models.py::RowSecurityRule``); the shape invariant is
enforced by the pydantic validator + a DB CHECK constraint.

The simulate endpoint shares its compilation path with the query router
via :mod:`shared.security` — a single source of truth means the preview
is what the user will actually get at query time.
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

import logging
import re

from shared.audit.logger import audit_required
from shared.webhooks.dispatcher import emit_webhook_logged as emit_webhook
from shared.db.models import (
    DataSource,
    Dimension,
    Model,
    ModelColumn,
    ModelTable,
    ProjectConnection,
    RowSecurityRule,
)
from shared.db.session import get_tenant_db
from shared.schemas.connection_type import normalize_connection_type
from shared.schemas.pydantic_models import (
    RowSecurityRuleCreate,
    RowSecurityRuleResponse,
    RowSecurityRuleUpdate,
    RowSecuritySimulateRequest,
    RowSecuritySimulateResponse,
)
from shared.security import (
    Principal,
    RowSecurityCompileError,
    compile_row_security,
)
from shared.security.predicate_compiler import _compile_dsl_expression
from src.api._model_lock import acquire_model_definition_lock
from src.auth.middleware import CurrentUser, forbid_embed_user
from src.auth.rbac import require_role
from src.api.versions import _evict_query_router_cache

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/projects/{project_id}/models/{model_id}/row-security",
    tags=["row-security"],
)


async def _get_scoped_model(db, project_id: UUID, model_id: UUID) -> Model:
    model = await db.get(Model, model_id)
    if model is None or model.project_id != project_id:
        raise HTTPException(status_code=404, detail="Model not found")
    return model


async def _get_scoped_rule(
    db, project_id: UUID, model_id: UUID, rule_id: UUID
) -> RowSecurityRule:
    await _get_scoped_model(db, project_id, model_id)
    rule = await db.get(RowSecurityRule, rule_id)
    if rule is None or rule.model_id != model_id:
        raise HTTPException(status_code=404, detail="Row-security rule not found")
    return rule


async def _validate_mapping_table_in_model(
    db, model_id: UUID, mapping_table_id: UUID
) -> None:
    """user_mapping rules must point at a table registered on the same model.

    This closes the door on a modeler smuggling in a table from another
    model (or another tenant schema) as a stealth data-exfil surface.
    """
    table = await db.get(ModelTable, mapping_table_id)
    if table is None or table.model_id != model_id:
        raise HTTPException(
            status_code=400,
            detail="mapping_table_id must reference a table on this model",
        )


async def _validate_mapping_columns(
    db, mapping_table_id: UUID | None,
    mapping_user_column: str | None,
    mapping_value_column: str | None,
) -> None:
    """Bug-5207: verify mapping_user_column and mapping_value_column exist
    on the mapping table. Without this, a modeler could reference a
    nonexistent column, producing a runtime SQL error on every matched query.
    """
    if mapping_table_id is None:
        return
    cols_to_check = []
    if mapping_user_column:
        cols_to_check.append(mapping_user_column)
    if mapping_value_column:
        cols_to_check.append(mapping_value_column)
    if not cols_to_check:
        return
    result = await db.execute(
        select(ModelColumn.column_name).where(
            ModelColumn.model_table_id == mapping_table_id,
            ModelColumn.column_name.in_(cols_to_check),
        )
    )
    found = set(result.scalars().all())
    missing = [c for c in cols_to_check if c not in found]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Mapping column(s) {', '.join(repr(c) for c in missing)} "
                f"not found on the mapping table."
            ),
        )


async def _connector_for_source(db, source: DataSource | None) -> str | None:
    """Return the normalized connector string for a resolved source, or None."""
    if source is None:
        return None
    conn = await db.get(ProjectConnection, source.project_connection_id)
    if conn is None:
        return None
    return normalize_connection_type((conn.connection_type or "").lower()) or None


_CONNECTOR_NOTE_AMBIGUOUS = (
    "The protected dimensions in this model span more than one source connector "
    "({connectors}). The preview below was compiled for {chosen} — the model's "
    "primary source — so identifier quoting may differ from the connector that "
    "actually runs the query."
)
_CONNECTOR_NOTE_UNRESOLVED = (
    "The connector for this model's protected dimensions could not be resolved. "
    "The preview below was compiled for {chosen} — the model's primary source — "
    "so identifier quoting may differ from the connector that actually runs the "
    "query."
)
_CONNECTOR_NOTE_DEFAULT = (
    "This model has no reachable source connection, so the preview below was "
    "compiled with the default {chosen} dialect. Identifier quoting may differ "
    "from the connector that actually runs the query."
)

_DEFAULT_CONNECTOR = "postgresql"


async def _resolve_model_connector(db, model_id: UUID) -> tuple[str, str | None]:
    """Resolve the connector string for a model's RLS-protected source so the
    simulate preview compiles the predicate with the SAME quoting the runtime
    path uses (F-007-07, Bug-7035).

    Returns ``(connector, note)``. ``note`` is non-None exactly when the
    resolution was NOT definitive — i.e. the protected dimensions were ambiguous
    or unresolvable and a fallback was used (Bug-8904, Bug-7027). The note is
    surfaced to the modeller as ``RowSecuritySimulateResponse.connector_note`` so
    a preview compiled with the wrong quoting is never mistaken for the runtime
    predicate. Previously the fallback was silent at BOTH ends: nothing populated
    the field and nothing rendered it.

    The one fallback that also indicates a broken *deployment* rather than a
    merely ambiguous model — the model has a source but that source's connector
    cannot be resolved at all — is additionally logged, so an operator sees it
    without having to open the preview.

    At query time the router resolves the dialect from the *source the query
    actually touches* (``resolve_target_dialect_for_bound``), i.e. the source
    hosting the column being filtered — NOT an arbitrary first source. The
    simulate preview has no bound query, so it must resolve the equivalent
    source from the row-security configuration itself: the protected
    dimensions the model's enabled rules filter on.

    Resolution (Bug-7035 — was ``DataSource.limit(1)`` unordered, which on a
    multi-source model could pick a source whose dialect differs from the one
    the protected column actually lives on, previewing wrong quoting):

    1. Collect the protected dimension names from every enabled rule's
       ``dimension_path`` (last path segment == dimension/column name).
    2. Map those dimensions -> ``source_column_id`` -> ModelColumn ->
       ModelTable -> DataSource, and collect the distinct connectors.
    3. If the protected dimensions resolve to exactly one connector, use it —
       this is correct regardless of which rule fires for the principal.
    4. If they span multiple connectors (a genuinely ambiguous multi-source
       RLS model), or none resolve, fall back to the model's *earliest* source
       (``created_at`` order — the same primary-source default the runtime
       ``_resolve_target_dialect`` uses), never an arbitrary unordered row.
    5. If the model has no source at all, default to ``"postgresql"`` — the
       compiler's own default — so preview still works for a fresh model.
    """
    # 1. Protected dimension names from enabled rules.
    rule_rows = (
        await db.execute(
            select(RowSecurityRule.dimension_path).where(
                RowSecurityRule.model_id == model_id,
                RowSecurityRule.is_enabled.is_(True),
            )
        )
    ).all()
    protected_names: set[str] = set()
    for row in rule_rows:
        # Real single-column Row unpacks as a 1-tuple; be tolerant of a bare
        # scalar too so the resolver never raises during preview.
        path = row[0] if isinstance(row, (tuple, list)) else getattr(row, "dimension_path", row)
        if path:
            protected_names.add(str(path).rsplit(".", 1)[-1])
    protected_names.discard("")

    # 2. Map protected dimensions -> touched sources -> distinct connectors.
    if protected_names:
        source_rows = (
            await db.execute(
                select(DataSource)
                .join(ModelTable, ModelTable.source_id == DataSource.id)
                .join(ModelColumn, ModelColumn.model_table_id == ModelTable.id)
                .join(Dimension, Dimension.source_column_id == ModelColumn.id)
                .where(
                    Dimension.model_id == model_id,
                    Dimension.name.in_(protected_names),
                )
                .distinct()
            )
        ).scalars().all()
        connectors: set[str] = set()
        for src in source_rows:
            connector = await _connector_for_source(db, src)
            if connector:
                connectors.add(connector)
        # 3. Unambiguous protected-source dialect — use it. This is the ONLY
        #    definitive outcome, so it is the only one that carries no note.
        if len(connectors) == 1:
            return connectors.pop(), None
        ambiguous = sorted(connectors)
    else:
        ambiguous = []

    # A model with no ENABLED rules has no protected dimension to quote, so
    # ``compile_row_security`` returns no predicate at all. Warning about the
    # dialect of a preview that is empty by construction would be pure noise —
    # the note exists to flag a predicate that MIGHT be quoted wrongly, not the
    # absence of one.
    warn = bool(protected_names)

    # 4. Ambiguous / unresolved -> model's earliest (primary) source, matching
    #    the runtime's _resolve_target_dialect fallback (ordered, not arbitrary).
    primary_source = (
        await db.execute(
            select(DataSource)
            .where(DataSource.model_id == model_id)
            .order_by(DataSource.created_at)
            .limit(1)
        )
    ).scalar_one_or_none()
    connector = await _connector_for_source(db, primary_source)

    # 5. No source / no connection -> compiler default.
    if connector is None:
        if primary_source is not None:
            # Bug-7027: the model HAS a source but its dialect could not be
            # resolved (missing connection row, empty connection_type). That is
            # a broken connection, not merely an ambiguous model, so it is
            # logged as well as disclosed — an operator should not have to open
            # the simulate preview to find out. Logged unconditionally of
            # ``warn``: the connection is broken whether or not this particular
            # model happens to have enabled rules.
            logger.warning(
                "_resolve_model_connector: falling back to %s for model %s — "
                "model has a source (DataSource %s) but its connector could "
                "not be resolved",
                _DEFAULT_CONNECTOR,
                model_id,
                # Defensive: the ORM row always carries ``id``, but this is a
                # logging argument evaluated eagerly inside an error path — it
                # must never be the thing that raises.
                getattr(primary_source, "id", None),
            )
        note = _CONNECTOR_NOTE_DEFAULT.format(chosen=_DEFAULT_CONNECTOR)
        return _DEFAULT_CONNECTOR, (note if warn else None)
    if ambiguous:
        # Ambiguity is always worth reporting: it means the model genuinely has
        # protected dimensions on more than one dialect and the preview can only
        # be right for one of them.
        return connector, _CONNECTOR_NOTE_AMBIGUOUS.format(
            connectors=", ".join(ambiguous), chosen=connector
        )
    note = _CONNECTOR_NOTE_UNRESOLVED.format(chosen=connector)
    return connector, (note if warn else None)


def _extract_predicate_paths(expr: str) -> list[str]:
    """Extract all dimension path strings from a DSL expression.

    The DSL functions ``dimension_equals('path', ...)`` and ``in('path', ...)``
    both take the path as a single-quoted first argument. This extracts all
    such paths so the API can verify they match the declared dimension_path.
    """
    paths: list[str] = []
    # Match dimension_equals('path', ...) and in('path', ...)
    for m in re.finditer(
        r"(?:dimension_equals|in)\s*\(\s*'([^']+)'", expr, re.IGNORECASE
    ):
        paths.append(m.group(1))
    return paths


async def _validate_dimension_path_exists(
    db, model_id: UUID, dimension_path: str
) -> None:
    """Bug-5206: verify the declared protected dimension exists in the model.

    The dimension_path's last segment is the column name used at query time.
    We check that a dimension with a matching name exists in this model.
    """
    dim_name = dimension_path.rsplit(".", 1)[-1]
    result = await db.execute(
        select(Dimension.id).where(
            Dimension.model_id == model_id,
            Dimension.name == dim_name,
        )
    )
    if result.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"dimension_path {dimension_path!r} does not match any "
                f"dimension in this model (looked for dimension named "
                f"{dim_name!r})."
            ),
        )


def _validate_predicate_matches_dimension(
    predicate_expression: str | None, dimension_path: str
) -> None:
    """Bug-5206: ensure the predicate's columns match the declared
    dimension_path. A rule that declares one protected dimension but
    filters on a different column would silently mis-filter at query time.
    """
    if not predicate_expression:
        return
    paths = _extract_predicate_paths(predicate_expression)
    if not paths:
        # The expression uses only boolean combinators (and/or/not) around
        # sub-expressions that do reference paths. Those sub-expressions are
        # validated recursively, but at the top level there may not be a
        # direct path. Skip the check in this case; the expression has
        # already been compiled successfully.
        return
    expected_col = dimension_path.rsplit(".", 1)[-1]
    for path in paths:
        actual_col = path.rsplit(".", 1)[-1]
        if actual_col != expected_col:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Predicate references column {actual_col!r} (from path "
                    f"{path!r}) but the rule's dimension_path resolves to "
                    f"{expected_col!r}. The predicate must filter on the "
                    f"declared protected dimension."
                ),
            )


def _redact_rule_field(value):
    """Render a rule field for the audit before/after record without dumping
    raw predicate values (F-007-05).

    The predicate EXPRESSION is a policy shape, not a secret, so it is kept
    verbatim (compliance needs "France -> Germany"). Everything else is
    stringified; ``None`` stays ``None`` so an added/removed field is visible.
    """
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return str(value)


def _validate_predicate_compiles(predicate_expression: str | None) -> None:
    """F-007-04: compile the DSL at save time so a malformed expression is
    rejected with a 422 here, not a generic 500 to every matched caller at
    query time.

    The compiler is the single source of truth for the DSL grammar; reusing
    it guarantees the save-time check and the runtime path agree (no rule
    that saves can fail to compile later). The check is dialect-agnostic for
    grammar purposes — connector quoting cannot change whether the
    expression parses — so the default connector is used.
    """
    if not predicate_expression:
        return
    try:
        _compile_dsl_expression(predicate_expression)
    except RowSecurityCompileError as exc:
        raise HTTPException(
            status_code=422,
            detail=(
                "row-security predicate is not a valid expression: "
                f"{exc}. Use the restricted DSL (dimension_equals, in, "
                "and, or, not) with single-quoted string values."
            ),
        )


# ---------------------------------------------------------------------------
# List / get
# ---------------------------------------------------------------------------


@router.get("", response_model=list[RowSecurityRuleResponse])
async def list_rules(
    project_id: UUID,
    model_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
    _: None = require_role("modeler"),
) -> list[RowSecurityRuleResponse]:
    # Bug-7807 [SECURITY]: row-security rule definitions disclose the tenant's
    # access-control policy — predicate_expression, applies_to_roles, and
    # attribute_claim_name reveal exactly who is restricted from what. A plain
    # viewer must not read them. Read is raised to modeler to match every
    # write/simulate route on these rules, which already require modeler.
    async for db in get_tenant_db(current_user.tenant_id):
        await _get_scoped_model(db, project_id, model_id)
        result = await db.execute(
            select(RowSecurityRule)
            .where(RowSecurityRule.model_id == model_id)
            .order_by(RowSecurityRule.created_at.asc())
        )
        return [
            RowSecurityRuleResponse.model_validate(r) for r in result.scalars().all()
        ]


@router.get("/{rule_id}", response_model=RowSecurityRuleResponse)
async def get_rule(
    project_id: UUID,
    model_id: UUID,
    rule_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
    _: None = require_role("modeler"),
) -> RowSecurityRuleResponse:
    # Bug-7807 [SECURITY]: see list_rules — a single rule's predicate and
    # applies_to_roles are the same access-control disclosure, so GET is
    # gated at modeler, not viewer.
    async for db in get_tenant_db(current_user.tenant_id):
        rule = await _get_scoped_rule(db, project_id, model_id, rule_id)
        return RowSecurityRuleResponse.model_validate(rule)


# ---------------------------------------------------------------------------
# Create / update / delete
# ---------------------------------------------------------------------------


@router.post(
    "",
    response_model=RowSecurityRuleResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[require_role("modeler")],
)
async def create_rule(
    project_id: UUID,
    model_id: UUID,
    body: RowSecurityRuleCreate,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> RowSecurityRuleResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await _get_scoped_model(db, project_id, model_id)
        await acquire_model_definition_lock(db, model_id)  # Bug-7982 cross-family lock

        # Bug-5206: validate dimension_path exists in the model.
        await _validate_dimension_path_exists(db, model_id, body.dimension_path)

        if body.rule_type == "user_mapping":
            await _validate_mapping_table_in_model(
                db, model_id, body.mapping_table_id
            )
            # Bug-5207: validate mapping columns exist on the mapping table.
            await _validate_mapping_columns(
                db, body.mapping_table_id,
                body.mapping_user_column, body.mapping_value_column,
            )
        else:  # role_predicate
            _validate_predicate_compiles(body.predicate_expression)
            # Bug-5206: validate predicate columns match dimension_path.
            _validate_predicate_matches_dimension(
                body.predicate_expression, body.dimension_path,
            )

        rule = RowSecurityRule(
            model_id=model_id,
            name=body.name,
            dimension_path=body.dimension_path,
            rule_type=body.rule_type,
            predicate_expression=body.predicate_expression,
            applies_to_roles=body.applies_to_roles,
            mapping_table_id=body.mapping_table_id,
            mapping_user_column=body.mapping_user_column,
            mapping_value_column=body.mapping_value_column,
            is_enabled=body.is_enabled,
            attribute_source=body.attribute_source,
            attribute_claim_name=body.attribute_claim_name,
        )
        db.add(rule)
        try:
            await db.flush()
            # F-007-05: fail-closed audit before commit — a create whose audit
            # cannot be persisted rolls the mutation back rather than losing the
            # only durable record of who granted an access rule.
            await audit_required(
                db, action="security.rule_create", severity="critical",
                actor_email=current_user.email,
                target_type="row_security_rule", target_id=rule.id,
                target_name=rule.name,
                detail={"rule_type": rule.rule_type, "dimension": rule.dimension_path},
            )
            await db.commit()
            await emit_webhook(current_user.tenant_id, "security.rule_create", {
                "rule_id": str(rule.id),
                "name": rule.name,
                "model_id": str(model_id),
                "actor": current_user.email,
            })
            await _evict_query_router_cache(model_id, current_user.tenant_id)
        except IntegrityError:
            await db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"A row-security rule named {body.name!r} already exists for this model",
            )
        await db.refresh(rule)
        return RowSecurityRuleResponse.model_validate(rule)


@router.patch(
    "/{rule_id}",
    response_model=RowSecurityRuleResponse,
    dependencies=[require_role("modeler")],
)
async def update_rule(
    project_id: UUID,
    model_id: UUID,
    rule_id: UUID,
    body: RowSecurityRuleUpdate,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> RowSecurityRuleResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        # Read-modify-write: the rule (write target) must be read UNDER the lock so
        # a concurrent revert/writer cannot make this operate on stale rule state
        # (Bug-7980 mixed-time-state). _get_scoped_rule also enforces ownership.
        await acquire_model_definition_lock(db, model_id)  # Bug-7982 cross-family lock
        rule = await _get_scoped_rule(db, project_id, model_id, rule_id)

        updates = body.model_dump(exclude_unset=True)

        # Bug-5206: if dimension_path is being updated, validate it exists.
        if "dimension_path" in updates:
            await _validate_dimension_path_exists(db, model_id, updates["dimension_path"])

        # F-007-04: validate the DSL on update too. The runtime error path
        # is the same (a malformed expression 500s every matched caller),
        # so the same save-time gate must guard the update path. Only run it
        # for role_predicate rules — rule_type is immutable, so the stored
        # type is authoritative.
        if (
            rule.rule_type == "role_predicate"
            and "predicate_expression" in updates
        ):
            _validate_predicate_compiles(updates["predicate_expression"])
            # Bug-5206: validate predicate columns match dimension_path
            # (use the updated dimension_path if supplied, else the stored one).
            eff_dim_path = updates.get("dimension_path", rule.dimension_path)
            _validate_predicate_matches_dimension(
                updates["predicate_expression"], eff_dim_path,
            )
        elif (
            rule.rule_type == "role_predicate"
            and "dimension_path" in updates
            and rule.predicate_expression
        ):
            # dimension_path changed but predicate_expression did not —
            # still need to check the existing predicate matches the new path.
            _validate_predicate_matches_dimension(
                rule.predicate_expression, updates["dimension_path"],
            )

        # Bug-5904: claim/scope-sourced role_predicate rules must have a
        # non-empty attribute_claim_name, or the rule silently never matches
        # at query time (predicate_compiler._resolve_principal_attribute
        # returns an empty set for saml_claim/oidc_scope when
        # attribute_claim_name is falsy). The create schema's
        # _shape_consistency validator enforces this for POST, but
        # RowSecurityRuleUpdate is a partial PATCH body — attribute_source
        # or attribute_claim_name may each be omitted, changed independently,
        # or explicitly blanked, so the schema alone cannot see the
        # post-merge state. Compute the effective (merged) values here and
        # fail closed the same way, on every update to a role_predicate rule
        # — not only when attribute_source/attribute_claim_name are the
        # fields being changed — so a prior attempt to blank the claim name
        # can never linger unrejected.
        if rule.rule_type == "role_predicate":
            eff_source = updates.get("attribute_source", rule.attribute_source)
            eff_claim_name = updates.get("attribute_claim_name", rule.attribute_claim_name)
            if eff_source in ("saml_claim", "oidc_scope") and not (
                eff_claim_name or ""
            ).strip():
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "attribute_claim_name is required and cannot be blank "
                        f"when attribute_source={eff_source!r}; a claim/scope-"
                        "sourced rule without a claim name will never match "
                        "any principal at query time"
                    ),
                )

        # Bug-5207: validate mapping columns on update if they are being changed.
        if rule.rule_type == "user_mapping":
            eff_table = updates.get("mapping_table_id", rule.mapping_table_id)
            if "mapping_table_id" in updates:
                await _validate_mapping_table_in_model(db, model_id, eff_table)
            eff_user_col = updates.get("mapping_user_column", rule.mapping_user_column)
            eff_val_col = updates.get("mapping_value_column", rule.mapping_value_column)
            if any(k in updates for k in ("mapping_table_id", "mapping_user_column", "mapping_value_column")):
                await _validate_mapping_columns(db, eff_table, eff_user_col, eff_val_col)

            # Bug-5905: user_mapping rules always key by user_identity at
            # runtime — attribute_source/attribute_claim_name are not
            # consumed for this rule type (see the matching create-time
            # guard in RowSecurityRuleBase._shape_consistency). Reject an
            # attempt to set them to a non-default value on update instead
            # of silently accepting a control that never takes effect.
            if "attribute_source" in updates and updates["attribute_source"] not in (None, "jwt_role"):
                raise HTTPException(
                    status_code=400,
                    detail="attribute_source must be left at its default ('jwt_role') "
                    "when rule_type='user_mapping'; this rule type always keys by "
                    "user_identity and does not consume attribute_source",
                )
            if updates.get("attribute_claim_name") is not None:
                raise HTTPException(
                    status_code=400,
                    detail="attribute_claim_name must be null when rule_type='user_mapping'",
                )

        # F-007-05: capture the redacted before-state so the update audit is
        # reconstructive (who changed what). Policy fields (predicate, roles,
        # mapping) are the security-relevant surface; values are recorded as
        # identity/shape markers, not raw secrets.
        _audited_fields = (
            "dimension_path", "predicate_expression", "applies_to_roles",
            "is_enabled", "attribute_source", "attribute_claim_name",
            "mapping_table_id", "mapping_user_column", "mapping_value_column",
        )
        _changed = [f for f in _audited_fields if f in updates]
        _before = {f: _redact_rule_field(getattr(rule, f, None)) for f in _changed}

        # rule_type is immutable — switching shapes would leave the row
        # violating the shape invariant until every field was updated in
        # the same request. The update schema omits it for this reason.
        for k, v in updates.items():
            setattr(rule, k, v)

        _after = {f: _redact_rule_field(getattr(rule, f, None)) for f in _changed}

        # F-007-05: durable, fail-closed update audit BEFORE commit so the
        # mutation and its evidence commit atomically (regression of Bug-7052 —
        # create/delete were audited but update silently committed).
        await audit_required(
            db, action="security.rule_update", severity="critical",
            actor_email=current_user.email,
            target_type="row_security_rule", target_id=rule.id,
            target_name=rule.name,
            detail={
                "rule_type": rule.rule_type,
                "dimension": rule.dimension_path,
                "changed_fields": _changed,
                "before": _before,
                "after": _after,
            },
        )

        try:
            await db.commit()
            await emit_webhook(current_user.tenant_id, "security.rule_update", {
                "rule_id": str(rule.id),
                "name": rule.name,
                "model_id": str(model_id),
                "changed_fields": _changed,
                "actor": current_user.email,
            })
            await _evict_query_router_cache(model_id, current_user.tenant_id)
        except IntegrityError:
            await db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Update would violate row-security shape or uniqueness invariants",
            )
        await db.refresh(rule)
        return RowSecurityRuleResponse.model_validate(rule)


@router.delete(
    "/{rule_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[require_role("modeler")],
)
async def delete_rule(
    project_id: UUID,
    model_id: UUID,
    rule_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> None:
    async for db in get_tenant_db(current_user.tenant_id):
        # Read-modify-write: the rule (write target) must be read UNDER the lock so
        # a concurrent revert/writer cannot make this operate on stale rule state
        # (Bug-7980 mixed-time-state). _get_scoped_rule also enforces ownership.
        await acquire_model_definition_lock(db, model_id)  # Bug-7982 cross-family lock
        rule = await _get_scoped_rule(db, project_id, model_id, rule_id)
        rule_name = rule.name
        _rule_type = rule.rule_type
        _dimension = rule.dimension_path
        await db.delete(rule)
        # F-007-05: fail-closed audit in the SAME transaction, BEFORE commit, so
        # the delete and its evidence are atomic (an audit-store failure rolls
        # the delete back rather than dropping a rule with no record of who).
        await audit_required(
            db, action="security.rule_delete", severity="critical",
            actor_email=current_user.email,
            target_type="row_security_rule", target_id=rule_id,
            target_name=rule_name,
            detail={"rule_type": _rule_type, "dimension": _dimension},
        )
        await db.commit()
        await emit_webhook(current_user.tenant_id, "security.rule_delete", {
            "rule_id": str(rule_id),
            "name": rule_name,
            "model_id": str(model_id),
            "actor": current_user.email,
        })
        await _evict_query_router_cache(model_id, current_user.tenant_id)
        return None


# ---------------------------------------------------------------------------
# Simulate-as-user
# ---------------------------------------------------------------------------


_SIMULATE_ADMIN_ROLES = frozenset({"system_admin", "tenant_admin"})


def _valid_rule_uuids(active_rule_ids) -> list[UUID]:
    """Convert active rule ids to UUIDs, dropping non-UUID sentinels.

    The fail-closed coverage path (F-007-01) uses a synthetic
    ``"__deny_all__"`` rule id that is not a real rule row, so it cannot be
    a ``uuid.UUID`` — filter it out of ``active_rule_ids`` (the deny is still
    reflected in ``compiled_predicate``).
    """
    out: list[UUID] = []
    for r in active_rule_ids or ():
        try:
            out.append(UUID(str(r)))
        except (TypeError, ValueError):
            continue
    return out


async def _run_probe_as_principal(
    *,
    model_id: UUID,
    probe_query: str,
    persona_id: UUID | None,
    principal: Principal,
    bearer: str,
) -> dict:
    """Execute *probe_query* through the query-router AS the simulated principal.

    F-007-02: routes through the query-router ``/execute`` endpoint with the
    admin-gated ``X-Tessallite-Simulate-*`` headers, so the real enforcement
    spine (bind -> route -> RLS inject -> source execution) runs for the
    simulated ``(user_identity, roles, groups, claims)`` and the rows returned
    are exactly what that principal would see. All source I/O stays behind the
    gateway/query-router boundary — model-service never touches the source DB.
    """
    import httpx as _httpx
    from shared.config.settings import get_settings

    _settings = get_settings()
    url = f"{_settings.QUERY_ROUTER_URL}/api/v1/execute"
    headers = {
        "Authorization": f"Bearer {bearer}",
        "X-Tessallite-Simulate-Principal": principal.user_identity,
        "X-Tessallite-Simulate-Roles": ",".join(sorted(principal.roles)),
        "X-Tessallite-Simulate-Groups": ",".join(sorted(principal.groups)),
        "X-Tessallite-Simulate-Claims": ";".join(
            f"{k}={v}" for k, v in (principal.claims or {}).items()
        ),
    }
    payload: dict = {
        "model_id": str(model_id),
        "raw_query": probe_query,
        "protocol": "jdbc",
    }
    if persona_id is not None:
        payload["persona_id"] = str(persona_id)
    async with _httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(url, json=payload, headers=headers)
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail")
        except Exception:
            detail = None
        raise HTTPException(
            status_code=resp.status_code if resp.status_code < 500 else 502,
            detail=(
                "Row-security simulation probe failed: "
                f"{detail or resp.text or f'HTTP {resp.status_code}'}"
            ),
        )
    return resp.json()


@router.post(
    "/simulate",
    response_model=RowSecuritySimulateResponse,
    dependencies=[require_role("modeler")],
)
async def simulate_as_user(
    project_id: UUID,
    model_id: UUID,
    body: RowSecuritySimulateRequest,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> RowSecuritySimulateResponse:
    """Simulate row-security for a hypothetical ``(user_identity, roles, ...)``.

    Two levels of fidelity:

    * Compiled preview (any modeller): the exact WHERE fragment that would wrap
      the query, via :func:`shared.security.compile_row_security` — the same
      shared path the runtime uses. Diagnostics, not proof.
    * Real-result simulation (F-007-02, admin only): when ``probe_query`` is
      supplied and the caller holds an admin role, the probe is EXECUTED as the
      simulated principal through the query-router's enforcement spine, and the
      response carries the rows that principal would actually see — the only
      thing that proves what a France manager gets, across route/dialect/CLS.
    """
    async for db in get_tenant_db(current_user.tenant_id):
        await _get_scoped_model(db, project_id, model_id)
        principal = Principal(
            user_identity=body.user_identity,
            roles=frozenset(body.roles),
            groups=frozenset(body.groups),
            claims=dict(body.claims),
        )
        # F-007-07: compile with the model's real target connector so the
        # previewed predicate is byte-identical to the runtime predicate
        # (BigQuery backticks vs PostgreSQL double-quotes).
        connector, connector_note = await _resolve_model_connector(db, model_id)
        try:
            compiled = await compile_row_security(
                model_id, principal, db, connector=connector,
            )
        except RowSecurityCompileError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Row-security compilation failed: {exc}",
            )

        active_rule_ids = _valid_rule_uuids(
            compiled.active_rule_ids if compiled is not None else ()
        )
        compiled_predicate = compiled.sql_expression if compiled is not None else None
        applied_rules = (
            [dict(r) for r in compiled.applied_rules] if compiled is not None else None
        )

        response = RowSecuritySimulateResponse(
            user_identity=body.user_identity,
            roles=list(body.roles),
            active_rule_ids=active_rule_ids,
            compiled_predicate=compiled_predicate,
            executed=False,
            applied_rules=applied_rules,
            # Bug-8904: a non-definitive connector resolution used to be silent.
            # Carry it to the modeller instead of previewing possibly-wrong
            # identifier quoting as if it were the runtime predicate.
            connector_note=connector_note,
        )

        # F-007-02: real-result simulation is admin-gated to match the
        # query-router simulate-as boundary (only system_admin / tenant_admin
        # may impersonate an arbitrary principal). A non-admin modeller who
        # asks for a probe gets a clear 403, never a silent compiled-only
        # response that could be mistaken for a real result.
        if body.probe_query:
            if (getattr(current_user, "role", None) or "") not in _SIMULATE_ADMIN_ROLES:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=(
                        "Real-result row-security simulation (probe_query) "
                        "requires an admin role (system_admin or tenant_admin). "
                        "The compiled-predicate preview is available to modellers."
                    ),
                )
            bearer = getattr(current_user, "raw_token", None)
            if not bearer:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=(
                        "Cannot run a row-security probe without the caller's "
                        "bearer token."
                    ),
                )
            result = await _run_probe_as_principal(
                model_id=model_id,
                probe_query=body.probe_query,
                persona_id=body.persona_id,
                principal=principal,
                bearer=bearer,
            )
            columns = list(result.get("columns") or [])
            raw_rows = result.get("rows") or []
            rows = [[row.get(c) for c in columns] for row in raw_rows]
            response.executed = True
            response.route_type = result.get("route_type")
            response.columns = columns
            response.rows = rows
            response.row_count = len(rows)

        return response
