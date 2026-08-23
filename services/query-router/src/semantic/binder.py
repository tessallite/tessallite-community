"""
Semantic Binder — resolves measure and dimension names in a LogicalQuery
against the semantic model stored in the metadata DB.

Also loads the model's active aggregates with their columns so the matcher
can work without additional DB calls.
"""
from __future__ import annotations

import dataclasses
import logging
import re
import types
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from shared.db.models import (
    AggregateColumn,
    AggregateDefinition,
    Dimension,
    Measure,
    Model,
    ModelColumn,
    ModelTable,
    Persona,
)
from shared.semantic.hierarchy_resolver import load_hierarchy_level_dimensions as _load_hierarchy_level_dimensions
from src.ir.logical_query import (
    BoundColumnRef,
    BoundDerivedExpression,
    BoundQuery,
    DeployedSnapshotUnavailableError,
    ExpressionOccurrence,
    LogicalQuery,
    ModelNotDeployedError,
    SemanticBindingError,
)
from src.semantic.snapshot_resolver import (
    LiveMetadataBundle,
    hierarchy_level_dimensions_from_snapshot,
    resolve_deployed_shape,
    resolve_live_metadata_bundle,  # noqa: F401 — kept for test-patch compatibility
)

logger = logging.getLogger(__name__)

# Variant suffixes the FROM-table allow-list recognises as legitimate model
# views without a DB lookup: the technical view. Persona-scoped catalogue
# names (``<slug>_<persona.slug>``) are validated dynamically against the
# model's real personas in ``_load_persona_slugs`` (Bug-6089). Bug-5193:
# unrecognised ``<slug>_*`` suffixes are still REJECTED with
# SemanticBindingError (previously they were only logged and silently bound to
# base-model data, allowing fabricated names to return real data).
_KNOWN_VARIANT_SUFFIXES = ("_technical",)


def _node_from_ast_json(ast_json: Any):
    """Rebuild the sqlglot node from a captured ``ExpressionOccurrence.ast_json``.

    ``ast_json`` is ``node.dump()`` (spec §5.1); ``exp.Expression.load`` round-trips
    it back to a node WITHOUT re-parsing ``raw_query`` (spec §3.1). Returns None on
    any malformed payload (caller falls back to the raw_sql canonicaliser).
    """
    if ast_json is None:
        return None
    try:
        from sqlglot import exp as _exp
        return _exp.Expression.load(ast_json)
    except Exception:
        return None


def _build_bound_derived_expressions(
    occurrences: list[ExpressionOccurrence],
    *,
    model_id: str,
    physical_columns: set[str],
    physical_column_ids: dict[str, str] | None = None,
    deployed_shape: Any = None,
    from_scope_alias_map: dict[str, str] | None = None,
) -> list[BoundDerivedExpression]:
    """Build diagnostic BoundDerivedExpression objects (spec §5.1, Phase 1).

    Phase 1 is capture + diagnostics only. This groups the parser's expression
    occurrences by their canonical fingerprint (repeated SELECT/GROUP BY share
    one bound expression but keep their occurrence ids), canonicalises each via
    the ONE shared canonicaliser, and records a ``proof_rejection`` reason when
    the expression is bindable but not acceleratable (e.g. an unknown function).

    Crucially it does NOT influence ``has_passthrough_expressions`` — an ordinary
    function-grain query still source-routes exactly as before. These objects are
    for explain/telemetry until the proof engine (later phases) consumes them.

    Column lineage (spec §7.1): each leaf column NAME is resolved to its stable
    ``ModelColumn.id`` against the deployed snapshot via ``physical_column_ids``
    (lowercase name -> id). This populates ``BoundColumnRef.column_id`` so the
    §7.3 cond. 2 lineage-collision guard (``_exact_lineage_matches``) can
    distinguish same-named columns in different relations and authorise an EXACT
    serve. A leaf whose name is not in the map keeps ``column_id=""`` — the guard
    then fails closed for that expression (no id-collision serve), so an
    unresolved leaf is safe, never a wrong serve. ``physical_column_ids`` empty
    (map unavailable) keeps every leaf at ``column_id=""``, i.e. the prior
    diagnostic-only behaviour.
    """
    if not occurrences:
        return []

    from shared.semantic.derived_expression import canonicalise_ast, canonicalise_sql
    from shared.semantic.derived_grain_reasons import DerivedReasonCode
    from src.semantic.derived_relabel_binder import bind_expression_leaves

    grouped: dict[str, BoundDerivedExpression] = {}
    for occ in occurrences:
        # Spec §3.1: bind from the captured AST node (dump()->load()), NOT a
        # re-parse of raw_query. Fall back to the raw_sql text canonicaliser only
        # when the ast_json cannot be rebuilt (older captures / malformed payload).
        _node = _node_from_ast_json(getattr(occ, "ast_json", None))
        if _node is not None:
            ce = canonicalise_ast(_node, input_dialect=occ.input_dialect)
        else:
            ce = canonicalise_sql(occ.raw_sql, input_dialect=occ.input_dialect)
        if ce is None:
            # Unparseable in isolation — record a stable, non-colliding key so the
            # occurrence still surfaces in diagnostics.
            key = f"raw:{occ.raw_sql}"
            canonical_sql = occ.raw_sql
            fingerprint = key
            inputs: list[BoundColumnRef] = []
            rejection: str | None = DerivedReasonCode.DERIVED_FUNCTION_UNKNOWN.value
        else:
            key = ce.fingerprint
            canonical_sql = ce.canonical_sql
            fingerprint = ce.fingerprint
            # Physical column names load lowercased; unquoted SQL identifiers
            # fold to lower-case in PostgreSQL. Compare case-insensitively so an
            # unquoted ``UPPER(Foo)`` leaf matches physical ``foo`` instead of a
            # spurious miss. (Quoted mixed-case identifiers keep their exact
            # spelling and are correctly resolved at the proof-engine bind step;
            # this diagnostic errs toward "found" to avoid a false rejection,
            # spec §7.1 — a bindable-but-unproved expression is still source-valid.)
            _physical_lc = {c.lower() for c in physical_columns}
            _id_by_name = physical_column_ids or {}
            # Compute the rejection FIRST — the leaf-id binding depends on it.
            # An unknown function or an input column not present among the model's
            # known physical columns makes the expression source-only.
            # ``physical_columns`` empty means we could not build the reference set
            # (e.g. SELECT * narrowed away), so we do not fabricate a missing-key
            # rejection in that case.
            if ce.has_unknown_function:
                rejection = DerivedReasonCode.DERIVED_FUNCTION_UNKNOWN.value
            elif physical_columns and any(
                col.lower() not in _physical_lc for col in ce.input_columns
            ):
                rejection = DerivedReasonCode.DERIVED_INPUT_KEY_MISSING.value
            else:
                rejection = None

            # §7.1 stable leaf binding — ALL-OR-NOTHING, and only for an
            # acceleratable expression. Two safety rules the serve path relies on:
            #  (1) An expression the binder itself marked source-only
            #      (``rejection`` set: unknown function, missing input column) must
            #      NOT carry serving lineage — an unknown/volatile function serving
            #      by exact identity would violate the "unknown function ->
            #      source-only" law. Withhold every leaf id in that case.
            #  (2) Partial lineage is forbidden: if ANY leaf name is unresolved
            #      (ambiguous/poisoned name, or absent from the id map), withhold
            #      EVERY leaf id. Otherwise ``build_query_key_requests`` would drop
            #      the empty leaf and present a NON-EMPTY partial lineage set, which
            #      a same-name-derived manifest could match on the partial set (a
            #      cross-relation wrong-number vector). All-or-nothing keeps the
            #      §7.3 cond. 2 comparison whole: the query asserts either the
            #      COMPLETE leaf-id tuple or none at all (empty -> fails closed).
            inputs = None
            # §3.1 preferred path: bind qualifier-aware leaves to stable
            # (table_id, column_id) against the deployed snapshot + parsed FROM
            # scope, ALL-OR-NOTHING. Only for an acceleratable expression (an
            # unknown function / missing input already set ``rejection``).
            if deployed_shape is not None and rejection is None and ce.leaves:
                bound = bind_expression_leaves(
                    ce.leaves,
                    model_id=str(model_id),
                    shape=deployed_shape,
                    alias_map=from_scope_alias_map or {},
                )
                if bound is not None:
                    inputs = bound
                else:
                    # Unresolved/ambiguous/out-of-scope leaf -> withhold ALL
                    # serving lineage: emit leaves with empty column_id so the
                    # §3.4 lineage gate fails closed for this expression.
                    inputs = [
                        BoundColumnRef(
                            model_id=str(model_id), table_id="",
                            column_id="", physical_column=lf.name,
                            logical_name=lf.name,
                        )
                        for lf in ce.leaves
                    ]
            if inputs is None:
                # Fallback (undeployed / seed-v1 / malformed AST): resolve leaf
                # ids by the lowercase name->id map, all-or-nothing.
                _resolved_ids = [_id_by_name.get(col.lower(), "") for col in ce.input_columns]
                # NB the id map is keyed by lowercased name, which folds a quoted
                # "Created_At" and an unquoted created_at together. That is safe here
                # because (1) quoted vs unquoted leaves produce DIFFERENT fingerprints
                # (the fingerprint gate separates them before lineage), and (2) if a
                # model genuinely held both as distinct columns, the shared lowercased
                # key would carry >1 id and be POISONED -> unresolved -> fail closed.
                _bind_ids = (
                    rejection is None
                    and bool(ce.input_columns)
                    and all(_resolved_ids)
                )
                inputs = [
                    BoundColumnRef(
                        model_id=str(model_id),
                        table_id="",
                        # Stable ModelColumn id when the whole expression binds cleanly;
                        # "" otherwise -> the §3.4 lineage gate fails closed (no serve),
                        # never a wrong-column serve.
                        column_id=(cid if _bind_ids else ""),
                        physical_column=col,
                        logical_name=col if col.lower() in _physical_lc else None,
                    )
                    for col, cid in zip(ce.input_columns, _resolved_ids)
                ]

        existing = grouped.get(key)
        if existing is not None:
            existing.occurrence_ids.append(occ.occurrence_id)
            existing.supported_roles.add(occ.role)
            continue

        grouped[key] = BoundDerivedExpression(
            occurrence_ids=[occ.occurrence_id],
            model_id=str(model_id),
            canonical_sql=canonical_sql,
            expression_fingerprint=fingerprint,
            inputs=inputs,
            supported_roles={occ.role},
            proof_rejection=rejection,
        )

    return list(grouped.values())


def _select_ordinals_for_name(query: LogicalQuery, names: set[str]) -> tuple[list[int], Optional[str]]:
    """SELECT ordinals (incl. duplicates) that project a bare column in *names*.

    Returns the ordered ordinals and the FIRST explicit alias seen. A projection
    matches when the item is a bare passthrough whose raw column text (qualifier
    stripped, alias stripped) case-folds to one of *names* (the query dimension's
    semantic name or its source column name). Used to bind relabel-key SELECT
    projections including duplicates (spec §3.4).
    """
    exact_names = {n for n in names if n}
    lc_names = {n.lower() for n in exact_names}
    ordinals: list[int] = []
    first_alias: Optional[str] = None
    for idx, expr in enumerate(getattr(query, "select_expressions", []) or []):
        if getattr(expr, "classification", None) != "passthrough":
            continue
        _rt = getattr(expr, "raw_text", None) or ""
        # Case-insensitive ``AS`` strip (a client may write ``as``/``As``); the alias
        # is carried separately on ``expr.alias`` so this only trims the core text.
        _as = _rt.lower().find(" as ")
        raw = (_rt[:_as] if _as != -1 else _rt).strip()
        # Terminal identifier (drop a single table qualifier). A QUOTED terminal is
        # case-SENSITIVE (a quoted "Customer_Name" must not match an unrelated
        # customer_name dimension, Fable R1 #10b); an UNQUOTED terminal case-folds
        # (PostgreSQL identifier folding).
        terminal = raw.rsplit(".", 1)[-1].strip()
        inner = (getattr(expr, "inner_column", None) or "")
        matched = False
        if terminal.startswith('"') and terminal.endswith('"') and terminal.count('"') == 2:
            matched = terminal[1:-1] in exact_names
        else:
            core = terminal.strip('"`').lower()
            matched = core in lc_names or (inner and inner.lower() in lc_names)
        if matched:
            ordinals.append(idx)
            if first_alias is None and getattr(expr, "alias", None):
                first_alias = expr.alias
    return ordinals, first_alias


def _bind_attribute_relabels(
    *,
    query: LogicalQuery,
    deployed_shape: Any,
    dimension_map: dict,
    dimension_map_lower: dict,
) -> tuple[list, list]:
    """Bind stage-4 relabels + group-key SELECT projections (spec §3.2/§3.4).

    Returns ``(bound_attribute_relabels, bound_group_key_projections)``. Only
    resolves against a deployed snapshot (the pinned relationship declarations);
    returns empty lists otherwise (undeployed model routes ordinary). Fail-closed:
    any dimension that does not resolve to exactly one enabled BIJECTION
    relationship simply produces no relabel record.
    """
    from src.ir.logical_query import BoundGroupKeyProjection
    from src.semantic.derived_relabel_binder import resolve_dimension_relabel

    relabels: list = []
    projections: list = []
    if deployed_shape is None:
        return relabels, projections
    # No declared relationships => no bare-detail dimension can become a relabel,
    # so skip the per-group-key resolution entirely (hot-path guard, Fable R1 #6).
    if not getattr(deployed_shape, "attribute_relationships", None):
        return relabels, projections

    grain_names = list(getattr(query, "grain", None) or [])
    if not grain_names:
        return relabels, projections

    # group_ordinal follows the GROUP BY tuple order. The parser puts bare group
    # columns in ``grain``; expression group keys are captured separately as
    # occurrences and are handled by the derived-expression path.
    for group_ordinal, gname in enumerate(grain_names):
        dim = dimension_map.get(gname) or dimension_map_lower.get(gname.lower())
        if dim is None:
            continue
        # Candidate name set for locating the SELECT projections of this key.
        src_col_id = getattr(dim, "source_column_id", None)
        name_set = {gname, getattr(dim, "name", "") or ""}
        col = deployed_shape.columns_by_id.get(str(src_col_id)) if src_col_id else None
        if col and col.get("column_name"):
            name_set.add(str(col.get("column_name")))
        select_ordinals, first_alias = _select_ordinals_for_name(query, name_set)
        # The reproducible output name is the parsed terminal spelling; prefer the
        # query dimension's semantic name (what the user typed) for the label rule.
        requested_name = gname
        relabel = resolve_dimension_relabel(
            query_dimension=dim,
            shape=deployed_shape,
            group_ordinal=group_ordinal,
            select_ordinals=select_ordinals,
            requested_name=requested_name,
            output_alias=first_alias,
        )
        if relabel is not None:
            relabels.append(relabel)
            for so in (select_ordinals or []):
                projections.append(BoundGroupKeyProjection(
                    select_ordinal=so, group_ordinal=group_ordinal,
                    kind="ATTRIBUTE", query_dimension_id=relabel.query_dimension_id,
                    column_id=relabel.detail_column_id,
                    attribute_key=relabel.attribute_key,
                    output_alias=first_alias,
                ))
        elif src_col_id is not None:
            # An unchanged PHYSICAL group key (spec §2.4/§3.4): dim:<dimension-uuid>
            # keyed by (source_column_id,). The GROUP KEY drives identity — so it is
            # recorded per group ordinal EVEN WHEN UNPROJECTED (a ``GROUP BY region``
            # with region absent from SELECT still contributes its identity to the
            # proof tuple, Fable R1 #9). When projected, each SELECT ordinal also
            # gets a projection so the rewrite emits one output column per ordinal.
            _phys = [
                BoundGroupKeyProjection(
                    select_ordinal=so, group_ordinal=group_ordinal,
                    kind="PHYSICAL",
                    query_dimension_id=str(getattr(dim, "id", "")),
                    column_id=str(src_col_id),
                    key_id=f"dim:{getattr(dim, 'id', '')}",
                    output_alias=first_alias,
                )
                for so in (select_ordinals or [])
            ]
            if not _phys:
                # Unprojected group key: sentinel ordinal -1 carries identity only.
                _phys.append(BoundGroupKeyProjection(
                    select_ordinal=-1, group_ordinal=group_ordinal,
                    kind="PHYSICAL",
                    query_dimension_id=str(getattr(dim, "id", "")),
                    column_id=str(src_col_id),
                    key_id=f"dim:{getattr(dim, 'id', '')}",
                ))
            projections.extend(_phys)

    # §3.4 "multiply matched item raises": a SELECT ordinal claimed by MORE THAN
    # ONE group key (overlapping name sets — e.g. dim A named ``customer`` with
    # source column ``customer_name`` vs dim B named ``customer_name``) would be
    # silently collapsed last-writer-wins in the rewrite's ordinal map and could
    # project the WRONG key's column under the item's label (a wrong-numbers serve,
    # Fable R2 #1). POISON the whole relabel binding (return empty) so every bare
    # grain falls through to an unresolved PHYSICAL request -> the candidate rejects
    # to source. Only real (>=0) ordinals are checked; the -1 identity sentinel is
    # not a projection. (This also fires for a single group key that claims one
    # ordinal twice, which cannot happen from distinct SELECT items but is caught
    # defensively.)
    _seen_ords: set[int] = set()
    for pj in projections:
        so = pj.select_ordinal
        if so < 0:
            continue
        if so in _seen_ords:
            return [], []  # multiply-matched ordinal -> fail closed to source
        _seen_ords.add(so)
    return relabels, projections


async def bind_query_to_model(
    query: LogicalQuery,
    db: AsyncSession,
    *,
    include_hidden: bool = False,
) -> BoundQuery:
    """
    Resolve the LogicalQuery against the semantic model.

    Raises SemanticBindingError if any measure, dimension, or filter column name
    cannot be resolved. Unresolvable WHERE shapes (OR, EXISTS, subqueries) are
    handled by raw-WHERE preservation in the rewriter, which validates unknown
    column references at rewrite time.

    Phase 2 of the semantic-layer plan: when ``include_hidden`` is False
    (the default, matching the business view), dimensions and measures
    whose cascaded ``is_hidden`` flag is true are excluded from the
    resolved lists. Passing ``include_hidden=True`` (the ``*_technical``
    view) keeps them.

    F-003-13: ``is_hidden`` is CURATION, not access control. It keeps a
    column out of the default business view (``SELECT *`` and discovery),
    but the column remains reachable: it is valid as a WHERE predicate, and
    any caller with the ``query`` capability can request the ``*_technical``
    view (``include_hidden=True``) to read it. The actual access boundary is
    persona allow-lists and row-level security, not ``is_hidden``.
    """
    model = await _load_model(query.model_id, db)
    if model is None:
        raise SemanticBindingError(f"Model {query.model_id} not found")
    # Per F-2: only deployed models accept queries. An undeployed model is
    # metadata-only — BI tools and the gateway should see it as not
    # available (the route handler turns this into HTTP 409).
    if getattr(model, "deployed_version_id", None) is None:
        model_label = getattr(model, "display_name", None) or query.model_id
        raise ModelNotDeployedError(
            f"Model \"{model_label}\" is not deployed; click Deploy in the "
            "Model Builder to make it available to BI tools."
        )

    # Validate FROM tables: every table referenced in the FROM clause must
    # resolve to the model slug, display name, or a persona-suffixed name.
    # Queries like SELECT 1 FROM does_not_exist should be rejected here
    # rather than forwarded to the source DB with silent table substitution.
    from_tables = getattr(query, "from_tables", None) or []
    if from_tables:
        slug = (getattr(model, "slug", "") or "").lower()
        display = (getattr(model, "display_name", "") or "").lower()
        allowed = {slug, display}
        allowed.discard("")
        persona_slugs: set[str] | None = None
        if slug:
            for ft in from_tables:
                ft_lower = ft.lower()
                if ft_lower == slug:
                    allowed.add(ft_lower)
                elif ft_lower.startswith(slug + "_"):
                    # Bug-5193: only KNOWN variant suffixes (_technical) and
                    # real persona-catalogue names are legitimate. Previously
                    # ANY ``<slug>_*`` suffix was silently accepted and bound to
                    # base-model data, so fabricated names like ``modely_fake``
                    # returned real data instead of failing.
                    suffix = ft_lower[len(slug):]  # includes the leading "_"
                    if suffix in _KNOWN_VARIANT_SUFFIXES:
                        allowed.add(ft_lower)
                    else:
                        # Bug-6089: the JDBC gateway publishes each persona as a
                        # sibling catalogue ``<slug>_<persona.slug>``. Direct API
                        # callers may address those same names, so accept a
                        # suffix that names a persona REALLY defined on this
                        # model. Load the persona slugs lazily (only when a
                        # non-technical variant appears — the rare case) and once
                        # per bind. An unknown suffix is still REJECTED, so the
                        # Bug-5193 guard against arbitrary FROM tables holds.
                        if persona_slugs is None:
                            persona_slugs = await _load_persona_slugs(model.id, db)
                        persona_suffix = suffix[1:]  # strip the leading "_"
                        if persona_suffix in persona_slugs:
                            allowed.add(ft_lower)
                        else:
                            raise SemanticBindingError(
                                f"Unknown model variant {ft!r}. The model "
                                f"{model.slug!r} does not have a variant "
                                f"with suffix {suffix!r}."
                            )
        cte_names = {a.lower() for a in getattr(query, "cte_aliases", []) or []}
        # Bug-6964 (SECURITY): every physical table in the query — including
        # tables referenced inside CTE bodies — must belong to the semantic
        # model.  CTE ALIAS names (e.g. ``stolen`` in
        # ``WITH stolen AS (...) SELECT * FROM stolen``) are intermediate
        # result names and are exempt, but the physical tables they scan
        # (e.g. ``secret_table`` inside the CTE body) are NOT exempt.
        #
        # Previous code (Bug-5192 fix) built a ``cte_body_tables`` exclusion
        # set and allowed ANY physical table found inside a CTE body through
        # the FROM validation.  This created a model/tenant containment
        # breach: ``WITH s AS (SELECT * FROM secret_table) SELECT * FROM s``
        # passed validation because ``secret_table`` was in the exclusion set,
        # and the passthrough path executed it against the source unchanged.
        #
        # The fix removes the ``cte_body_tables`` exemption entirely.  CTE
        # body tables that ARE the model table pass via ``allowed``.  CTE body
        # tables that are NOT in the model are rejected with
        # ``SemanticBindingError``, closing the containment breach.
        #
        # On parse failure with CTEs present, fail CLOSED (reject) rather
        # than the previous fail-open posture, because an unparseable CTE
        # could hide an arbitrary table reference.
        if cte_names:
            try:
                import sqlglot as _sg
                _input_dialect = getattr(query, "input_dialect", "postgres") or "postgres"
                _sg.parse_one(query.raw_query, read=_input_dialect)
                # Parse succeeded — validation of individual tables happens
                # in the loop below (no cte_body_tables exemption).
            except Exception:
                # Parse failure with CTEs: fail CLOSED. An unparseable CTE
                # could contain an arbitrary physical table reference, and we
                # cannot verify it belongs to the model.
                raise SemanticBindingError(
                    "Cannot validate CTE table references for model "
                    f"{model.slug!r}; query rejected for safety."
                )
        for ft in from_tables:
            ft_lower = ft.lower().split(".")[-1]
            if ft_lower not in allowed and ft_lower not in cte_names:
                raise SemanticBindingError(
                    f"Unknown table {ft!r} in FROM clause. "
                    f"Use the model name {model.slug!r} instead."
                )

    # B15 / F-013-01 / F-013-05 / Bug-7979 (fail-closed gate): when the model
    # is deployed, resolve the semantic SHAPE from the deployed version's
    # immutable snapshot. Draft edits do not leak until the next Deploy.
    # Row security, personas, data-tags, aggregates, pockets stay live.
    # See docs/architecture/architecture_b15-deploy-snapshot-pinning-design.md.
    #
    # FAIL-CLOSED (Bug-7979): a DEPLOYED model whose snapshot is unusable
    # (missing version row, non-dict, empty shape) must NOT fall back to
    # live/draft metadata. That would expose unpublished field edits to BI
    # clients. Raise DeployedSnapshotUnavailableError (-> HTTP 503) so the
    # operator knows the deployment needs repair (re-deploy / migrate legacy
    # placeholders). Live metadata is the authority ONLY for genuinely
    # undeployed models (no deployed_version_id).
    deployed_shape = await resolve_deployed_shape(model, db)

    # F-013-05 / Bug-7979 fail-closed check: if the model HAS a deploy pointer
    # but resolve_deployed_shape returned None (missing/corrupt/empty snapshot),
    # it must NOT fall back to live/draft metadata — that leaks unpublished
    # edits to BI clients. Raise DeployedSnapshotUnavailableError (-> HTTP 503).
    # Live metadata is the authority ONLY for genuinely undeployed models.
    if deployed_shape is None and getattr(model, "deployed_version_id", None) is not None:
        model_label = getattr(model, "display_name", None) or query.model_id
        raise DeployedSnapshotUnavailableError(
            f"Model \"{model_label}\" is deployed but its deployed snapshot is "
            "unavailable or corrupt. Re-deploy the model to repair, or contact "
            "an administrator. Query serving is suspended until the deployment "
            "is restored."
        )

    if deployed_shape is not None:
        measures = list(deployed_shape.measures)
        dimensions = list(deployed_shape.dimensions)
        hierarchy_levels = hierarchy_level_dimensions_from_snapshot(deployed_shape)
    else:
        # Genuinely undeployed model (no deployed_version_id). The binder gate
        # above normally rejects serving queries against undeployed models (409),
        # but authoring paths (headless validate, explain) may reach here.
        # Live metadata is the legitimate authority for these paths.
        measures = await _load_measures(model.id, db)
        dimensions = await _load_dimensions(model.id, db)
        hierarchy_levels = await _load_hierarchy_level_dimensions(model.id, db)

    has_hidden_exclusions = False
    # all_dimensions_for_filter retains hidden dimensions so they can be used
    # in WHERE clauses even when include_hidden=False.  Hidden columns must
    # not appear in SELECT * results (enforced by the persona scope gate), but
    # they are valid filter predicates — blocking them in WHERE breaks queries
    # like "SELECT amount FROM model WHERE hidden_id = 42".
    #
    # Bug-3592 / Bug-5381: hidden columns are CURATION, not access control.
    # An explicitly named hidden column in SELECT (not SELECT *) is allowed
    # for any caller — the persona scope gate enforces actual access.  The
    # ``_all_*_for_select`` maps below provide the fallback for explicit
    # SELECT resolution so ``SELECT channel_code FROM modely`` resolves even
    # when include_hidden=False.  SELECT * still hides them (the star
    # expansion uses the filtered ``dimensions``/``measures`` lists).
    all_dimensions_for_filter = list(dimensions)
    all_measures_for_select = list(measures)
    if not include_hidden:
        if deployed_shape is not None:
            hidden_column_ids = deployed_shape.hidden_column_ids
        else:
            # Undeployed model — load from live tables.
            hidden_column_ids = await _load_hidden_column_ids(model.id, db)
        if hidden_column_ids:
            has_hidden_exclusions = True
        dimensions = [d for d in dimensions if not _is_semantic_object_hidden(d, hidden_column_ids)]
        measures = [m for m in measures if not _is_semantic_object_hidden(m, hidden_column_ids)]
        hierarchy_levels = [d for d in hierarchy_levels if not _is_semantic_object_hidden(d, hidden_column_ids)]

    measure_map = {m.name: m for m in measures}
    dimension_map = {d.name: d for d in dimensions}
    for dim in hierarchy_levels:
        # Explicit dimensions win if names collide.
        dimension_map.setdefault(dim.name, dim)
    # F-003-06: case-insensitive fallback maps for SELECT-list resolution.
    # The parser preserves the identifier case sqlglot read (Postgres folds
    # unquoted identifiers to lower-case, but sqlglot does not), so a PG-wire
    # client typing ``SUM(REVENUE)`` against a model measure ``revenue`` must
    # still bind — matching the case-insensitive fallback the FILTER path has
    # always had below. Last writer wins on a case-collision, same as the
    # filter maps; exact-case lookups are tried first so they are unaffected.
    measure_map_lower = {m.name.lower(): m for m in measures}
    dimension_map_lower = {k.lower(): v for k, v in dimension_map.items()}
    # Filter-only lookup including hidden dimensions.  Used below to allow
    # hidden columns in WHERE without exposing them in SELECT.
    _all_dim_map_for_filter = {d.name: d for d in all_dimensions_for_filter}
    _all_dim_map_lower_for_filter = {d.name.lower(): d for d in all_dimensions_for_filter}
    # Bug-3592 / Bug-5381: explicit-SELECT fallback maps including hidden
    # columns.  SELECT * still uses the filtered lists; only name-based
    # SELECT references fall through to these.
    _all_dim_map_for_select = {d.name: d for d in all_dimensions_for_filter}
    _all_dim_map_lower_for_select = {d.name.lower(): d for d in all_dimensions_for_filter}
    _all_measure_map_for_select = {m.name: m for m in all_measures_for_select}
    _all_measure_map_lower_for_select = {m.name.lower(): m for m in all_measures_for_select}

    # F-003-04: collision-poisoned physical-name index. Build a map from
    # lowercase physical column names to the semantic object they back. When a
    # requested name fails semantic-name lookup, fall back to this index to
    # resolve by physical source name. A physical name that maps to more than
    # one semantic object is POISONED (omitted) — ambiguous resolution would
    # silently pick an arbitrary winner and risk wrong numbers. Physical-name
    # binding is only available when a deployed shape exists (the snapshot
    # carries columns_by_id with column_name); for undeployed models, physical
    # names remain unresolvable (semantic names are the only contract).
    _phys_to_measure: dict[str, Any] = {}
    _phys_to_dimension: dict[str, Any] = {}
    if deployed_shape is not None and deployed_shape.columns_by_id:
        # Build column_id -> lowercase physical name lookup
        _colid_to_phys: dict[str, str] = {}
        for _cid_str, _col_dict in deployed_shape.columns_by_id.items():
            _cn = (_col_dict.get("column_name") or "").lower()
            if _cn:
                _colid_to_phys[_cid_str] = _cn
        # Map physical names to measures (including hidden). Poison collisions.
        _phys_meas_candidates: dict[str, list[Any]] = {}
        for _m in all_measures_for_select:
            _scid = getattr(_m, "source_column_id", None)
            if _scid is not None:
                _pn = _colid_to_phys.get(str(_scid))
                if _pn:
                    _phys_meas_candidates.setdefault(_pn, []).append(_m)
        _phys_to_measure = {
            pn: objs[0] for pn, objs in _phys_meas_candidates.items()
            if len(objs) == 1
        }
        # Map physical names to dimensions (including hidden). Poison collisions.
        _phys_dim_candidates: dict[str, list[Any]] = {}
        for _d in all_dimensions_for_filter:
            _scid = getattr(_d, "source_column_id", None)
            if _scid is not None:
                _pn = _colid_to_phys.get(str(_scid))
                if _pn:
                    _phys_dim_candidates.setdefault(_pn, []).append(_d)
        _phys_to_dimension = {
            pn: objs[0] for pn, objs in _phys_dim_candidates.items()
            if len(objs) == 1
        }
        # Fable FINDING-1: a physical name that is POISONED (ambiguous) in one
        # type's candidate map must not silently resolve through the other type.
        # Example: "region" backs 2 dimensions (poisoned) but 1 measure -- the
        # cross-type fallback would pick the measure as an arbitrary winner.
        # Poison any name that appears in either candidate map with >1 entry.
        _phys_poisoned = set()
        for pn, objs in _phys_meas_candidates.items():
            if len(objs) > 1:
                _phys_poisoned.add(pn)
        for pn, objs in _phys_dim_candidates.items():
            if len(objs) > 1:
                _phys_poisoned.add(pn)
        # Remove cross-type poisoned names from both maps.
        for pn in _phys_poisoned:
            _phys_to_measure.pop(pn, None)
            _phys_to_dimension.pop(pn, None)

    # Complex SQL (CTEs, derived tables, subqueries, window functions) is
    # executed as raw SQL after table-name substitution.  The parser's
    # flattener already removes trivial wrappers (SELECT * FROM (subquery)).
    # For remaining complex SQL, the passthrough path preserves the user's
    # SQL structure — hidden columns inside subqueries or aggregate contexts
    # are allowed (same as SUM(hidden_col)).  The post-execution audit
    # skips these queries because no resolved-column set is available.
    #
    # Previously this was a hard reject on business view.  Removed because:
    # (a) legitimate patterns like SELECT 1 FROM (subquery) were blocked,
    # (b) the source DB enforces its own access control on physical columns,
    # (c) hidden columns in subqueries are analogous to hidden columns
    #     inside SUM() — the persona scope gate applies to the SELECT list
    #     of the outermost query, not to inner subquery references.

    # Complex SQL (CTEs, derived tables, subqueries, window functions):
    # skip dimension/measure resolution entirely — the technical query goes
    # through passthrough-with-table-substitution.
    _complex_sql_physical_columns: set[str] | None = None
    _complex_projection_names: set[str] = set()
    if getattr(query, "has_complex_sql", False):
        resolved_dimensions = []
        resolved_measures = []
        # F-003-02 (SECURITY): complex passthrough previously treated allowed
        # TABLE identity as sufficient authorisation. A one-relation CTE/subquery
        # over the allowed model name could SELECT an UNMODELLED physical source
        # column (a column present in the source table but never exposed as a
        # semantic dimension/measure) and receive it — same-tenant data
        # disclosure, since neither the binder, the persona gate, nor the
        # post-execute audit examined individual columns for complex SQL.
        #
        # Root-cause fix: before passthrough, walk EVERY physical column
        # reference across ALL scopes and require each to be a MODELLED physical
        # column of the deployed snapshot. Unmodelled columns, ambiguity, or a
        # parse failure FAIL CLOSED (SemanticBindingError). This is the
        # column-level analogue of the Bug-6964 table-containment fix and uses
        # the SAME deployed-snapshot authority (A1) the rest of this bind reads.
        #
        # ``physical_columns_all`` (every modelled physical column incl. hidden)
        # is the membership boundary: hidden/tag-restricted columns ARE modelled
        # and are governed separately by persona/CLS downstream — this gate only
        # blocks columns the model never declared at all.
        _model_physical_columns = _resolve_model_physical_columns(
            deployed_shape, physical_columns_fallback=None,
        )
        # Live-table fallback ONLY for a genuinely undeployed authoring path
        # (no deployed shape). When a deployed shape exists it is the sole
        # authority (A1) — an empty pinned physical set fails closed in
        # ``_validate_complex_sql_columns`` rather than re-reading mutable
        # live/draft columns.
        if (
            _model_physical_columns is None
            and deployed_shape is None
            and db is not None
        ):
            try:
                _model_physical_columns = await _load_physical_column_names(
                    model.id, db, exclude_hidden=False,
                )
            except Exception:
                _model_physical_columns = None
        _complex_projection_names = _validate_complex_sql_columns(
            query, model, _model_physical_columns,
        )
        # Keep the post-execute result-column audit ACTIVE for complex SQL:
        # carry the validated model physical set so ``audit_result_columns``
        # can whitelist exactly the modelled columns instead of returning early.
        # (Applied to ``physical_columns`` at its initialisation below.)
        _complex_sql_physical_columns = set(_model_physical_columns or set())
    elif query.select_star:
        # CR-002 Finding 6: scope SELECT * to the parser-resolved FROM
        # tables when we have them. Expanding to every dimension/measure
        # of the model would cause _collect_touched_source_ids in the
        # query-router to think the query touches every data source, and
        # a single-table SELECT * against a multi-source model would
        # trigger a false CROSS_SOURCE_UNSUPPORTED. If the parser didn't
        # resolve from_tables (e.g. passthrough), fall back to the full
        # model expansion — the executor path has its own validation.
        from_tables = getattr(query, "from_tables", None) or []
        # When pinned to a deployed snapshot we keep the full pinned lists
        # rather than re-scoping against live ModelTable/ModelColumn rows
        # (which may have drifted from the snapshot). The executor's own
        # cross-source check still guards multi-source SELECT *.
        if from_tables and deployed_shape is None:
            scoped_dims, scoped_meas = await _filter_by_from_tables(
                dimensions, measures, from_tables, model.id, db
            )
            resolved_dimensions = scoped_dims
            resolved_measures = scoped_meas
        else:
            resolved_dimensions = list(dimensions)
            resolved_measures = list(measures)
    else:
        resolved_measures = []
        for name in query.requested_measures:
            if name == "__row_count":
                resolved_measures.append(types.SimpleNamespace(
                    name="__row_count",
                    default_agg="count",
                    is_additive=True,
                    source_column_id=None
                ))
                continue
            m = measure_map.get(name) or measure_map_lower.get(name.lower())
            if m is None:
                # Bug-5381: fall back to hidden measures for explicit SELECT.
                m = _all_measure_map_for_select.get(name) or _all_measure_map_lower_for_select.get(name.lower())
            if m is None:
                # Column may be a dimension used inside an aggregate function
                # (e.g. COUNT(DISTINCT dim_column)).  Wrap as synthetic measure.
                d = dimension_map.get(name) or dimension_map_lower.get(name.lower())
                if d is None:
                    # Bug-5381: fall back to hidden dimensions.
                    d = _all_dim_map_for_select.get(name) or _all_dim_map_lower_for_select.get(name.lower())
                if d is not None:
                    m = types.SimpleNamespace(
                        id=getattr(d, "id", None),
                        name=name,
                        default_agg="count_distinct",
                        is_additive=False,
                        source_column_id=getattr(d, "source_column_id", None),
                        user_defined_attribute_id=getattr(d, "user_defined_attribute_id", None),
                    )
                else:
                    # F-003-04: physical-name fallback. Resolve by the physical
                    # source column name when semantic-name lookup failed.
                    # Canonicalize to the semantic object's name so downstream
                    # consumers see the standard semantic identity.
                    _phys_m = _phys_to_measure.get(name.lower())
                    _phys_d = _phys_to_dimension.get(name.lower()) if _phys_m is None else None
                    if _phys_m is not None:
                        m = _phys_m
                    elif _phys_d is not None:
                        m = types.SimpleNamespace(
                            id=getattr(_phys_d, "id", None),
                            name=_phys_d.name,
                            default_agg="count_distinct",
                            is_additive=False,
                            source_column_id=getattr(_phys_d, "source_column_id", None),
                            user_defined_attribute_id=getattr(_phys_d, "user_defined_attribute_id", None),
                        )
                    else:
                        raise SemanticBindingError(
                            f"Unknown column: {name!r} in model {model.slug!r}"
                        )
            resolved_measures.append(m)

        resolved_dimensions = []
        seen_dims: set[str] = set()
        for name in query.requested_dimensions:
            if name in seen_dims:
                continue
            d = dimension_map.get(name) or dimension_map_lower.get(name.lower())
            if d is None:
                # Bug-5381: fall back to hidden dimensions for explicit SELECT.
                d = _all_dim_map_for_select.get(name) or _all_dim_map_lower_for_select.get(name.lower())
            if d is None:
                # Column may be a measure used outside an aggregate (e.g. in
                # arithmetic expressions).  Wrap it as a virtual dimension so
                # binding succeeds and the rewriter can resolve the physical
                # column name.
                m = measure_map.get(name) or measure_map_lower.get(name.lower())
                if m is None:
                    # Bug-5381: fall back to hidden measures.
                    m = _all_measure_map_for_select.get(name) or _all_measure_map_lower_for_select.get(name.lower())
                if m is not None:
                    if (
                        getattr(m, "measure_type", None) == "calculated"
                        or getattr(m, "variant_kind", None) is not None
                    ):
                        # Calculated measures and variant measures must go
                        # through the measure expansion path: calculated ones
                        # need expression expansion, variants need the
                        # time-intelligence window function from variant_kind.
                        resolved_measures.append(m)
                        seen_dims.add(name)
                        continue
                    d = types.SimpleNamespace(
                        id=getattr(m, "id", None),
                        name=name,
                        source_column_id=getattr(m, "source_column_id", None),
                        user_defined_attribute_id=getattr(m, "user_defined_attribute_id", None),
                        is_measure_as_dimension=True,
                    )
                else:
                    # F-003-04: physical-name fallback for dimension resolution.
                    _phys_d = _phys_to_dimension.get(name.lower())
                    _phys_m = _phys_to_measure.get(name.lower()) if _phys_d is None else None
                    if _phys_d is not None:
                        d = _phys_d
                    elif _phys_m is not None:
                        if (
                            getattr(_phys_m, "measure_type", None) == "calculated"
                            or getattr(_phys_m, "variant_kind", None) is not None
                        ):
                            resolved_measures.append(_phys_m)
                            seen_dims.add(name)
                            continue
                        d = types.SimpleNamespace(
                            id=getattr(_phys_m, "id", None),
                            name=_phys_m.name,
                            source_column_id=getattr(_phys_m, "source_column_id", None),
                            user_defined_attribute_id=getattr(_phys_m, "user_defined_attribute_id", None),
                            is_measure_as_dimension=True,
                        )
                    else:
                        raise SemanticBindingError(
                            f"Unknown column: {name!r} in model {model.slug!r}"
                        )
            resolved_dimensions.append(d)
            seen_dims.add(name)

    # Filters: only keep those whose dimension_name resolves in the semantic model.
    # Case-insensitive fallback for filter dimension names — BI tools and
    # hand-written queries frequently differ in capitalisation.
    _dim_map_lower = {k.lower(): k for k in dimension_map}
    _measure_map_lower = {k.lower(): k for k in measure_map}
    resolved_filters = []
    for f in query.filters:
        # Bug-6383: rebuild the resolved filter with ``dataclasses.replace`` so
        # EVERY field on ``LogicalFilter`` (operator, value, and the LIKE/NOT
        # LIKE ``like_escape`` character) is carried through the canonicalisation
        # of ``dimension_name``. The previous positional rebuild
        # (``LogicalFilter(name, op, value)``) silently DROPPED ``like_escape``,
        # so a ``contains``/``notContains`` filter reached the WHERE renderer
        # with no escape clause and matched wrong rows on SQL Server / Spark.
        # ``replace`` is also future-proof: any new field added to the filter is
        # preserved automatically instead of being lost at this seam.
        if f.dimension_name in dimension_map:
            resolved_filters.append(f)
        elif f.dimension_name.lower() in _dim_map_lower:
            canonical = _dim_map_lower[f.dimension_name.lower()]
            resolved_filters.append(dataclasses.replace(f, dimension_name=canonical))
        elif f.dimension_name in measure_map or f.dimension_name.lower() in _measure_map_lower:
            # Bug-6966: the exact-match path ``measure_map.get(f.dimension_name)``
            # returns the measure OBJECT, while the case-insensitive path
            # ``_measure_map_lower.get(...)`` returns the canonical NAME string.
            # Previously ``hasattr(canonical, 'name')`` was false for the
            # string path, preserving the caller's mis-cased spelling and
            # breaking downstream field-expression lookups in source_sql /
            # conditions that use exact semantic-name keys.  Now we resolve
            # the canonical name uniformly.
            if f.dimension_name in measure_map:
                canonical_name = f.dimension_name  # already canonical
            else:
                canonical_name = _measure_map_lower[f.dimension_name.lower()]
            resolved_filters.append(dataclasses.replace(f, dimension_name=canonical_name))
        elif f.dimension_name in _all_dim_map_for_filter:
            # Hidden dimension — valid in WHERE predicate; persona gate blocks
            # it from appearing in SELECT results.
            resolved_filters.append(f)
        elif f.dimension_name.lower() in _all_dim_map_lower_for_filter:
            # Case-insensitive match against a hidden dimension.
            canonical_dim = _all_dim_map_lower_for_filter[f.dimension_name.lower()]
            resolved_filters.append(dataclasses.replace(f, dimension_name=canonical_dim.name))
        elif (
            f.dimension_name in _all_measure_map_for_select
            or f.dimension_name.lower() in _all_measure_map_lower_for_select
        ):
            # Bug-6087: hidden measure columns are valid in a WHERE predicate,
            # exactly like visible measures (above) and hidden dimensions.
            # ``is_hidden`` is CURATION, not access control (F-003-13) — the
            # explicit-SELECT path already resolves hidden measures, so a
            # WHERE reference to the same column must not be rejected as
            # "Unknown filter column". Canonicalise to the measure's name.
            canonical_meas = (
                _all_measure_map_for_select.get(f.dimension_name)
                or _all_measure_map_lower_for_select.get(f.dimension_name.lower())
            )
            resolved_filters.append(
                dataclasses.replace(f, dimension_name=canonical_meas.name)
            )
        elif getattr(query, "has_complex_sql", False):
            # F-003-12: keep unresolved extracted filters only for complex SQL
            # (containment walks those). Unresolvable WHERE + a typo filter
            # must still raise Unknown filter column.
            resolved_filters.append(f)
        else:
            raise SemanticBindingError(
                f"Unknown filter column: {f.dimension_name!r} in model {model.slug!r}"
            )

    # Bug-5488: collect model dimensions referenced ONLY inside an
    # unresolvable WHERE predicate (function-wrapped comparison, OR-compound,
    # etc.). The parser's strict ``_extract_filters`` deliberately produces no
    # ``LogicalFilter`` for such shapes, so a column that appears nowhere else
    # (not in SELECT, ORDER BY, or a representable filter) never reaches the
    # source rewriter's column/table collection — its physical column is not
    # loaded and its table is not joined, so the rewriter emits the bare name
    # and the source DB raises "column does not exist". Walk the raw WHERE AST
    # and record every column whose name resolves to a known model dimension or
    # measure so the rewriter can fold these into its physical-column /
    # join-table set. Restricted to names present in the model maps, so
    # literals, aliases, and function/keyword tokens are never captured.
    where_referenced_dimensions = _collect_where_referenced_fields(
        query,
        dimension_map=dimension_map,
        dimension_map_lower=dimension_map_lower,
        all_dim_map_for_filter=_all_dim_map_for_filter,
        all_dim_map_lower_for_filter=_all_dim_map_lower_for_filter,
    )
    _reject_unknown_unresolvable_where_columns(
        query,
        model=model,
        dimension_map=dimension_map,
        dimension_map_lower=dimension_map_lower,
        measure_map=measure_map,
        measure_map_lower=_measure_map_lower,
        all_dim_map_for_filter=_all_dim_map_for_filter,
        all_dim_map_lower_for_filter=_all_dim_map_lower_for_filter,
        all_measure_map=_all_measure_map_for_select,
        all_measure_map_lower=_all_measure_map_lower_for_select,
    )

    # Strip surrounding SQL identifier quote characters (double-quote or backtick)
    # before comparing raw_text to inner_column. A bare quoted column like
    # "col_name" has raw_text='"col_name"' but inner_column='col_name'; they
    # are the same expression and must NOT force the passthrough rewrite path.
    # Complex expressions (CASE, COALESCE, aliased columns like "col" AS "alias")
    # contain additional tokens outside/around the quotes (AS, CASE, etc.) so
    # they do NOT match the anchored ``^...$`` regex and continue to trigger
    # passthrough correctly.
    #
    # Bug-6965: the previous regex ``[^"`\s]+`` excluded whitespace, so a
    # quoted semantic name containing spaces (e.g. ``"Net Revenue"``) would NOT
    # match and was misclassified as a passthrough expression.  Semantic names
    # with spaces are valid — the model API accepts them — and they must be
    # resolved to their physical columns, not treated as raw passthrough.  The
    # fix allows any characters inside the matching quote pair, as long as the
    # overall string is a single quoted identifier (anchored start/end).
    _BARE_QUOTED_IDENT = re.compile(r'^["`]([^"`]+)["`]$')

    def _cmp_raw_text(raw_text: str) -> str:
        s = raw_text.lower().strip()
        m = _BARE_QUOTED_IDENT.match(s)
        return m.group(1) if m else s

    has_passthrough = any(
        expr.classification == "passthrough"
        # Composable aggregate expressions (SUM(a)/SUM(b), CASE over SUMs) stay
        # classified passthrough for the source path, but are aggregate-routable
        # — they must NOT force the matcher to bail.
        and not getattr(expr, "composable", False)
        and (
            # inner_column is None → compound aggregate (SUM(a)/SUM(b))
            expr.inner_column is None
            # inner_column is set but raw_text differs → CASE, COALESCE, etc.
            # Strip identifier quoting before comparing so "col" == col.
            or (expr.inner_column and _cmp_raw_text(expr.raw_text) != expr.inner_column.lower())
        )
        for expr in getattr(query, "select_expressions", [])
    )
    # Function-based GROUP BY (DATE_TRUNC, EXTRACT in GROUP BY) cannot be
    # reconstructed by the source rewriter — force passthrough with table
    # name substitution.
    # Bug-7359: when ALL function-grain items are recognized DATE_TRUNC
    # expressions (time_period_grains is non-empty), the query MAY be
    # aggregate-eligible. Still set has_passthrough for the SOURCE rewrite
    # path (the source rewriter cannot reconstruct DATE_TRUNC from the IR),
    # but the aggregate matcher will check time_period_grains and may
    # override the passthrough bail to route to an aggregate whose grain
    # covers the underlying date column.
    if getattr(query, "has_function_grain", False):
        has_passthrough = True
    # Complex SQL (CTEs, derived tables, window functions, subqueries in
    # WHERE/SELECT) cannot be safely reconstructed — force passthrough.
    if getattr(query, "has_complex_sql", False):
        has_passthrough = True

    # Don't expand SELECT * when it wraps a passthrough (e.g. subquery) —
    # the query goes to source raw, so full-model expansion would pollute
    # the QueryMissLog and RouteLog with misleading dimension/measure lists.
    if has_passthrough and query.select_star:
        resolved_dimensions = []
        resolved_measures = []

    # Collect any resolved dims/measures that are currently flagged
    # ``is_invalid=True``. The router uses this list to skip the
    # aggregate matcher and fall back to source path, then records a
    # query_fallback alert against each offending object so the
    # modeler sees the query attempt.
    uses_invalid: list[tuple[str, str]] = []
    for d in resolved_dimensions:
        if bool(getattr(d, "is_invalid", False)):
            uses_invalid.append(("dimension", d.name))
    for m in resolved_measures:
        if bool(getattr(m, "is_invalid", False)):
            uses_invalid.append(("measure", m.name))

    # Cross-model measure detection — same-project cross-model queries not yet resolved
    from src.ir.logical_query import CrossModelNotResolvedError
    for m in resolved_measures:
        _cm_model = getattr(m, "cross_model_source_model_id", None)
        # Bug-6237: a cross-model measure may carry only
        # ``cross_model_source_measure_id`` (the referenced measure) without the
        # model id — e.g. from a partial import. The old guard checked the model
        # id alone, so such a measure slipped through and bound as if it were a
        # local measure, even though it has no local ``source_column_id`` to
        # render. Any cross-model reference metadata (model OR measure) means the
        # measure is unresolvable here; fail loud rather than emit wrong SQL.
        _cm_measure = getattr(m, "cross_model_source_measure_id", None)
        if _cm_model is not None or _cm_measure is not None:
            raise CrossModelNotResolvedError(
                measure_slug=m.name,
                source_model_id=(
                    str(_cm_model) if _cm_model is not None
                    else f"(referenced measure {_cm_measure})"
                ),
            )

    # For SELECT *, load the physical column names so the security audit
    # can validate result columns that come back as physical names rather
    # than semantic dimension/measure names.  On business queries
    # (include_hidden=False), hidden columns are excluded so the audit
    # whitelist does not authorise them.
    physical_columns: set[str] = set()
    if _complex_sql_physical_columns is not None:
        # F-003-02: complex-SQL passthrough. The columns were validated above
        # against the deployed model set; publish that set so the post-execute
        # result-column audit stays ACTIVE (it no longer returns early for
        # complex SQL) and authorises exactly the modelled physical columns.
        physical_columns = _complex_sql_physical_columns
    elif query.select_star:
        if deployed_shape is not None:
            physical_columns = (
                deployed_shape.physical_columns_all
                if include_hidden
                else deployed_shape.physical_columns_visible
            )
        else:
            # Undeployed model — load from live tables.
            try:
                physical_columns = await _load_physical_column_names(
                    model.id, db, exclude_hidden=not include_hidden,
                )
            except Exception:
                pass

    # When hidden columns were excluded and this is a SELECT *, set
    # persona_narrowed_star so the rewriter builds an explicit column
    # projection instead of sending raw SELECT * to the source (which
    # would return hidden physical columns and then fail the audit).
    narrowed_star = query.select_star and has_hidden_exclusions

    # Bug-5546: resolve each dimension's source-column data type so the
    # synchronous aggregate rewriter can type WHERE literals (the source route
    # resolves this via the same ModelColumn lookup; the aggregate route has no
    # DB session of its own). One query over the referenced source columns.
    # Bug-5546: resolve each dimension's source-column data type so the
    # synchronous aggregate rewriter can type WHERE literals.
    #
    # F-003-05 (Bug-7979 fail-closed): when a deployed shape exists, resolve
    # types from the snapshot's ``columns_by_id`` — NOT from live ``ModelColumn``
    # rows, which can reflect draft edits made after deployment. A draft type
    # change (e.g. text -> integer) would otherwise alter filter literal form
    # before redeployment, breaking stable production query behavior.
    # Live ORM lookup is used only for genuinely undeployed models.
    dim_type_by_name: dict[str, str] = {}
    try:
        # Bug-6088: type EVERY dimension that can appear in a WHERE predicate,
        # not just the visible ``dimension_map``. Hidden dimensions are legal
        # in WHERE, so iterate BOTH maps.
        _col_id_to_dim: dict[Any, str] = {}
        for _src_map in (dimension_map, _all_dim_map_for_filter):
            for _name, _d in _src_map.items():
                _cid = getattr(_d, "source_column_id", None)
                if _cid is not None:
                    _col_id_to_dim[_cid] = _name
        if _col_id_to_dim:
            if deployed_shape is not None:
                # Resolve from the immutable deployed snapshot (F-003-05).
                for _cid, _dim_name in _col_id_to_dim.items():
                    _snap_col = deployed_shape.columns_by_id.get(str(_cid))
                    if _snap_col:
                        _dt = _snap_col.get("data_type")
                        if _dt:
                            dim_type_by_name[_dim_name] = _dt
            elif db is not None:
                # Undeployed model — live ORM is the authority.
                _dt_rows = await db.execute(
                    select(ModelColumn.id, ModelColumn.data_type).where(
                        ModelColumn.id.in_(list(_col_id_to_dim.keys()))
                    )
                )
                for _cid, _dt in _dt_rows.all():
                    if _dt:
                        dim_type_by_name[_col_id_to_dim[_cid]] = _dt
    except Exception:
        dim_type_by_name = {}

    # Derived-grain routing (spec §5.1, Phase 1): build diagnostic bound
    # expressions from the captured occurrences. This does NOT change
    # ``has_passthrough`` — a function-grain query still source-routes.
    _derived_occurrences = getattr(query, "expression_occurrences", []) or []
    # The derived-expression leaves are PHYSICAL source column names. Reuse the
    # already-loaded ``physical_columns`` when present (SELECT *), otherwise load
    # the model's physical column names ON DEMAND — but only when there actually
    # are captured occurrences, so an ordinary query pays nothing. Without this,
    # the ``DERIVED_INPUT_KEY_MISSING`` diagnostic would be unreachable for the
    # feature's own target shape (a non-SELECT-* ``GROUP BY DATE_TRUNC(...)``),
    # where ``physical_columns`` is otherwise only populated under SELECT *.
    _known_physical = physical_columns
    if _derived_occurrences and not _known_physical and db is not None:
        try:
            _known_physical = await _load_physical_column_names(
                model.id, db, exclude_hidden=not include_hidden,
            )
        except Exception:
            _known_physical = set()
    # §7.1 stable leaf binding: the name->id map for the derived-expression
    # leaves. Take the map from the SAME source the rest of this bind used — the
    # pinned DEPLOYED-SNAPSHOT shape if present, else the cached live bundle — so
    # the bound leaf ids match the vocabulary the query is pinned to. The on-demand
    # live load is a fallback ONLY when NEITHER source was available (the seed-v1 /
    # no-snapshot-and-no-bundle case; matches the pre-existing physical-name
    # loader's fallback). It is NOT taken merely because the map is empty: a pinned
    # snapshot whose names were all legitimately poisoned (ambiguous) must stay
    # pinned and fail closed, never silently rebind from live tables that may have
    # drifted from the pinned version. All three builders poison ambiguous names
    # and cover all columns, so the vocabulary is identical whichever path supplies
    # it. Consulted ONLY when there are derived occurrences, so an ordinary query
    # pays nothing (the builder returns [] without ever reading the map).
    _physical_column_ids: dict[str, str] = {}
    if _derived_occurrences:
        if deployed_shape is not None:
            _physical_column_ids = getattr(deployed_shape, "physical_column_ids", {}) or {}
        elif db is not None:
            try:
                _physical_column_ids = await _load_physical_column_ids(model.id, db)
            except Exception:
                _physical_column_ids = {}
    # §3.1/§3.2 parsed FROM-scope alias map: alias/token -> deployed table_id.
    # Built once against the pinned snapshot ONLY when there is derived-expression
    # OR stage-4 relabel work to do — i.e. the query captured a function-grain
    # occurrence, OR the model declares at least one attribute relationship (the
    # only way a bare-detail GROUP BY can become a relabel). An ordinary dimensional
    # query on a model with no relationships pays NOTHING (no re-parse), keeping the
    # read-path spine byte-cheap. The turn-on flag is a separate serving gate; this
    # is the cheap capability gate so binding stays inert until the shape warrants it.
    _model_has_relationships = bool(
        deployed_shape is not None
        and getattr(deployed_shape, "attribute_relationships", None)
    )
    _from_scope_alias_map: dict[str, str] = {}
    if deployed_shape is not None and (_derived_occurrences or _model_has_relationships):
        from src.semantic.derived_relabel_binder import build_from_scope_alias_map
        try:
            _from_scope_alias_map = build_from_scope_alias_map(query, deployed_shape)
        except Exception:
            _from_scope_alias_map = {}

    bound_derived_expressions = _build_bound_derived_expressions(
        _derived_occurrences,
        model_id=str(getattr(model, "id", getattr(query, "model_id", ""))),
        physical_columns=_known_physical,
        physical_column_ids=_physical_column_ids,
        deployed_shape=deployed_shape,
        from_scope_alias_map=_from_scope_alias_map,
    )

    # §3.2/§3.4 stage-4 relabel binding: resolve bare detail GROUP BY dimensions to
    # BIJECTION relationships and bind SELECT projections of the group keys. Only
    # against a deployed snapshot; fail-closed and empty for ordinary queries.
    bound_attribute_relabels, bound_group_key_projections = _bind_attribute_relabels(
        query=query,
        deployed_shape=deployed_shape,
        dimension_map=dimension_map,
        dimension_map_lower=dimension_map_lower,
    )

    quantile_requests = _build_quantile_inventory(
        query, resolved_measures, deployed_shape=deployed_shape)

    return BoundQuery(
        logical_query=query,
        model=model,
        resolved_measures=resolved_measures,
        resolved_dimensions=resolved_dimensions,
        resolved_filters=resolved_filters,
        deployed_shape=deployed_shape,
        resolved_dimensions_by_name=dimension_map,
        dim_type_by_name=dim_type_by_name,
        has_passthrough_expressions=has_passthrough,
        uses_invalid_objects=uses_invalid,
        persona_narrowed_star=narrowed_star,
        allowed_physical_columns=physical_columns,
        complex_projection_names=_complex_projection_names,
        where_referenced_dimensions=where_referenced_dimensions,
        bound_derived_expressions=bound_derived_expressions,
        quantile_requests=quantile_requests,
        bound_attribute_relabels=bound_attribute_relabels,
        bound_group_key_projections=bound_group_key_projections,
    )


def _measure_value_type(measure: object) -> str | None:
    """Best-effort canonical source value type of a measure's input column.

    Used for the QuantileRequest input fingerprint and the value-type proof.
    Returns None when unknown — the proof then defers to the coverage (an
    unknown request type never falsely rejects a matching column).
    """
    for attr in ("data_type", "source_data_type", "physical_type", "column_type"):
        value = getattr(measure, attr, None)
        if value:
            return str(value)
    return None


def _build_quantile_inventory(
    query: LogicalQuery, resolved_measures: list,
    deployed_shape: object = None,
) -> list:
    """Build the semantic QuantileRequest inventory (spec §4.1/§5.1, I1).

    One request per SELECT ordered-set percentile / MEDIAN item that carries
    ``quantile_meta``. The input is bound to the resolved measure named by the
    item's ``inner_column`` so the request's fingerprint agrees with the
    coverage the materialiser wrote (``build_input_fingerprint``). A SELECT item
    whose measure cannot be resolved yields NO request — the item then has no
    coverage to prove and the matcher routes to source (fail closed); it is
    never silently dropped from a served plan because the matcher requires a
    request for every pNN SELECT column it would read.

    Deliberately SELECT-scoped for the first serving release: HAVING/ORDER/calc
    quantiles already force exact grain and fire the dialect-exactness gate via
    the Phase-0 semantic inventory (router ``_query_uses_percentile`` /
    ``compute_has_non_additive``), and the matcher's coverage branch only serves
    a query whose every pNN SELECT read is proven — so an unresolved HAVING/calc
    quantile cannot produce a wrong serve.
    """
    from shared.quantile_contracts import (
        METHOD_CONTINUOUS,
        ORDER_ASC,
        QuantileRequest,
        ORIGIN_MEASURE_DEFAULT,
        ORIGIN_SELECT,
        build_input_fingerprint,
        measure_value_definition,
        to_fraction,
    )
    from shared.aggregate_quantiles import (
        is_quantile_agg_token,
        quantile_suffix_to_fraction,
    )

    measures_by_name = {getattr(m, "name", None): m for m in (resolved_measures or [])}
    # FIX 2 (same-ID drift): resolve source column names from the deployed
    # snapshot so measure_value_definition can include the physical column
    # name. A ModelColumn that keeps its UUID but changes its column_name
    # produces a different fingerprint -> fail closed.
    _columns_by_id = (
        getattr(deployed_shape, "columns_by_id", None) or {}
    ) if deployed_shape else {}
    _tables_by_id = (
        getattr(deployed_shape, "tables_by_id", None) or {}
    ) if deployed_shape else {}

    def _source_col_info(measure: object) -> tuple[str | None, str | None, str | None]:
        """Return (column_name, table_physical_name, table_source_id) from the deployed shape."""
        scid = getattr(measure, "source_column_id", None)
        if not scid:
            return None, None, None
        col = _columns_by_id.get(str(scid))
        if col is None:
            return None, None, None
        if isinstance(col, dict):
            cname = col.get("column_name")
            mtid = col.get("model_table_id")
        else:
            cname = getattr(col, "column_name", None)
            mtid = getattr(col, "model_table_id", None)
        tphys = None
        tsid = None
        if mtid:
            tbl = _tables_by_id.get(str(mtid))
            if tbl:
                if isinstance(tbl, dict):
                    tphys = tbl.get("physical_name")
                    tsid = tbl.get("source_id")
                else:
                    tphys = getattr(tbl, "physical_name", None)
                    tsid = getattr(tbl, "source_id", None)
        return cname, tphys, tsid

    requests: list = []
    idx = 0
    for expr in getattr(query, "select_expressions", []) or []:
        meta = getattr(expr, "quantile_meta", None)
        if not meta:
            continue
        col = getattr(expr, "inner_column", None)
        if not col:
            continue
        measure = measures_by_name.get(col)
        if measure is None:
            # Unresolved input -> no request; matcher will refuse to serve any
            # pNN read it cannot prove, so this fails closed to source.
            continue
        fraction = to_fraction(meta.get("fraction_text"))
        if fraction is None:
            # Non-Decimal / out-of-range fraction: cannot select coverage.
            continue
        value_type = _measure_value_type(measure)
        _cname, _tphys, _tsid = _source_col_info(measure)
        idx += 1
        requests.append(
            QuantileRequest(
                request_id=f"q{idx}",
                semantic_measure_name=getattr(measure, "name", col),
                input_expression_fingerprint=build_input_fingerprint(
                    getattr(measure, "name", col), value_type,
                    value_definition=measure_value_definition(
                        measure, source_column_name=_cname,
                        source_table_physical_name=_tphys,
                        source_table_source_id=str(_tsid) if _tsid else None),
                ),
                fraction=fraction,
                method=meta.get("method") or METHOD_CONTINUOUS,
                order_direction=meta.get("direction") or ORDER_ASC,
                value_type=value_type,
                origin=ORIGIN_SELECT,
                output_alias=getattr(expr, "alias", None),
                measure_id=str(getattr(measure, "id", "")) or None,
                source_syntax=meta.get("source_syntax") or "ordered_set",
            )
        )

    # Measure-default quantile inventory (Fable R2 MEDIUM-1, spec I1/I8). A BARE
    # measure whose ``default_agg`` is a quantile stat (e.g. p90) carries NO
    # percentile syntax and no ``quantile_meta``, so without this it would serve
    # the stored ``m__pNN`` column via the plain (measure, stat) path WITHOUT the
    # coverage proof — contradicting I8 under ``enforce``. Inventory it as a
    # quantile request (origin=measure_default) so the coverage gate applies.
    #
    # Inventory is SEMANTIC, from RESOLVED MEASURES — never from surface syntax
    # (spec I1 reviewer addition). Fable R4 HIGH: keying off select_expressions
    # (R3) fails open on DAX/XMLA, the Excel plugin, and headless REST, which
    # build the LogicalQuery from ``requested_measures`` with NO
    # select_expressions — those queries would then skip the quantile gate
    # entirely and serve a stored pNN column unproven under enforce. Deriving the
    # measure-default inventory from every resolved measure whose ``default_agg``
    # is a quantile stat fails ALL protocols closed (an inventoried request that
    # finds no coverage routes to source; it never causes a wrong serve).
    # An EXPLICIT aggregation over the measure overrides its default_agg (its
    # function was already checked in the SELECT loop), so skip those — mirrors
    # ``_explicitly_aggregated_measure_names`` in the matcher (Phase-0 rule). The
    # legacy ``median`` token maps to p50 continuous ASC.
    from src.routing.aggregate_matcher import _explicitly_aggregated_measure_names as _explicit_names_fn

    class _BQShim:
        # _explicitly_aggregated_measure_names reads logical_query.select_expressions
        logical_query = query

    _explicit_names = _explicit_names_fn(_BQShim())
    for measure in (resolved_measures or []):
        name = getattr(measure, "name", None)
        if name is None or name in _explicit_names:
            continue
        default_agg = getattr(measure, "default_agg", None)
        if not is_quantile_agg_token(default_agg):
            continue
        token = (default_agg or "").strip().lower()
        frac_float = quantile_suffix_to_fraction(token)
        # Legacy ``median`` and the sqlglot function keys map to p50.
        if frac_float is None:
            frac_float = 0.5 if token in {"median", "percentilecont", "percentile_cont"} else None
        fraction = to_fraction(str(frac_float)) if frac_float is not None else None
        if fraction is None:
            continue
        value_type = _measure_value_type(measure)
        _cname2, _tphys2, _tsid2 = _source_col_info(measure)
        idx += 1
        requests.append(
            QuantileRequest(
                request_id=f"q{idx}",
                semantic_measure_name=name,
                input_expression_fingerprint=build_input_fingerprint(
                    name, value_type,
                    value_definition=measure_value_definition(
                        measure, source_column_name=_cname2,
                        source_table_physical_name=_tphys2,
                        source_table_source_id=str(_tsid2) if _tsid2 else None),
                ),
                fraction=fraction,
                method=METHOD_CONTINUOUS,  # default_agg quantiles are continuous
                order_direction=ORDER_ASC,
                value_type=value_type,
                origin=ORIGIN_MEASURE_DEFAULT,
                output_alias=None,
                measure_id=str(getattr(measure, "id", "")) or None,
                # A measure-default quantile has no pre-feature ordered-set
                # serving path, BUT it DID serve pre-feature via the plain stat
                # path (that is exactly the I8 hole). Treat it like MEDIAN for the
                # disabled-mode gate: when the feature is off it keeps its
                # pre-existing serving (no regression); when ON it must be proven.
                source_syntax="median",
            )
        )
    return requests


async def load_active_aggregates(
    model_id: object, db: AsyncSession
) -> list[AggregateDefinition]:
    """
    Load all active AggregateDefinitions for the model, with their columns
    and the related Measure for each column (needed by the matcher).
    """
    result = await db.execute(
        select(AggregateDefinition)
        .where(
            AggregateDefinition.model_id == model_id,
            AggregateDefinition.status == "active",
        )
        .options(
            selectinload(AggregateDefinition.columns).selectinload(AggregateColumn.measure),
            # Bug-5148/Bug-8338: the serve-time overdue gate reads the aggregate's
            # refresh cron (via its policy) to detect a missed scheduled refresh.
            # Eager-load it so the matcher never issues a per-candidate query.
            selectinload(AggregateDefinition.refresh_policy),
        )
    )
    return list(result.scalars().all())


async def load_quantile_coverage_by_column(
    aggregate_ids: list, db: AsyncSession
) -> dict:
    """Load persisted QuantileCoverage rows for the given aggregates, keyed by
    ``aggregate_column_id`` (Bug-6969/5891, spec §4.2).

    Returns ``{aggregate_column_id: QuantileCoverage-value-object}``. A pNN
    ``AggregateColumn`` WITHOUT a row is simply absent — the proof then treats it
    as ``unknown`` (ineligible in exact mode, I8/Gap D). Returns the immutable
    ``shared.quantile_contracts.QuantileCoverage`` value objects (not ORM rows)
    so the pure proof core never touches SQLAlchemy.
    """
    from decimal import Decimal, InvalidOperation

    from shared.db.models import QuantileCoverage as _QCRow
    from shared.quantile_contracts import QuantileCoverage as _QCValue

    if not aggregate_ids:
        return {}
    result = await db.execute(
        select(_QCRow)
        .where(_QCRow.aggregate_definition_id.in_(list(aggregate_ids)))
        .options(selectinload(_QCRow.aggregate_column))
    )
    out: dict = {}
    for row in result.scalars().all():
        try:
            frac = Decimal(str(row.fraction))
        except (InvalidOperation, ValueError):
            # A malformed fraction cannot be proven -> omit (fail closed).
            continue
        out[row.aggregate_column_id] = _QCValue(
            physical_column_name=row.aggregate_column.physical_col_name
            if row.aggregate_column is not None
            else "",
            semantic_measure_name=row.semantic_measure_name,
            input_expression_fingerprint=row.input_expression_fingerprint,
            fraction=frac,
            method=row.method,
            order_direction=row.order_direction,
            null_policy=row.null_policy,
            value_type=row.value_type,
            collation=row.collation,
            timezone=row.timezone,
            exactness=row.exactness,
            coverage_schema_version=row.coverage_schema_version,
        )
    return out


async def load_inactive_aggregates(
    model_id: object, db: AsyncSession
) -> list[AggregateDefinition]:
    """Load non-active aggregates for diagnostic skip-reason reporting."""
    result = await db.execute(
        select(AggregateDefinition)
        .where(
            AggregateDefinition.model_id == model_id,
            AggregateDefinition.status != "active",
        )
        .options(
            selectinload(AggregateDefinition.columns).selectinload(AggregateColumn.measure)
        )
    )
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _collect_where_referenced_fields(
    query: LogicalQuery,
    *,
    dimension_map: dict,
    dimension_map_lower: dict,
    all_dim_map_for_filter: dict,
    all_dim_map_lower_for_filter: dict,
) -> set[str]:
    """Return canonical model dimension names referenced anywhere in the
    query's raw WHERE clause (Bug-5488).

    Only runs for the unresolvable-WHERE path: a resolvable WHERE is already
    fully represented by ``resolved_filters`` (and thus by the rewriter's
    ``filter_dim_names``), so walking it would be redundant. Complex passthrough
    queries skip semantic resolution entirely and go to source raw, so they need
    no column collection here either.

    The walk visits EVERY ``exp.Column`` in the WHERE subtree — including those
    nested inside function calls (``UPPER(TRIM(col))``) and on both sides of
    AND/OR compounds — and keeps only names that resolve to a known model
    DIMENSION (exact case first, then case-insensitive, including hidden
    dimensions that are valid WHERE predicates). Names that match nothing in the
    dimension maps (string literals parsed as columns, output aliases, CTE
    columns, function/keyword tokens) are ignored, so the set never captures a
    non-model token. The returned values are the canonical model names, ready to
    fold into the source rewriter's physical-column / join-table collection.

    Scope (deep-review finding, Bug-5488): only DIMENSIONS are collected, not
    measures. The source rewriter's filter backfill loads filter-only columns
    from the ``Dimension`` table, and ``_get_phys_expr`` resolves a measure only
    when it is in ``resolved_measures``/``_order_measures`` — neither of which
    holds a measure referenced ONLY inside the WHERE. Collecting a measure name
    here would therefore add it to ``filter_dim_names`` without the source path
    being able to resolve it, so it is deliberately excluded. A measure used
    only inside an unresolvable WHERE predicate (an unusual shape — measures
    normally appear in SELECT/HAVING) remains out of scope for this fix.
    """
    if not getattr(query, "has_unresolvable_where", False):
        return set()
    if getattr(query, "has_complex_sql", False):
        return set()
    raw_sql = getattr(query, "raw_query", None)
    if not raw_sql:
        return set()

    import sqlglot as _sg
    from sqlglot import exp as _sg_exp

    input_dialect = getattr(query, "input_dialect", "postgres") or "postgres"
    try:
        ast = _sg.parse_one(raw_sql, read=input_dialect, error_level=_sg.ErrorLevel.WARN)
    except Exception:
        # Parse failure: the rewriter's raw-WHERE path will re-parse and fail
        # loudly itself; collecting nothing here is the safe (no-op) direction.
        return set()

    select_node = ast if isinstance(ast, _sg_exp.Select) else ast.find(_sg_exp.Select)
    if select_node is None:
        return set()
    where_node = select_node.args.get("where")
    # Bug-457 shape: WHERE may live inside a subquery wrapper. Mirror the
    # rewriter's raw-WHERE extraction so we collect from the same subtree.
    # sqlglot stores the FROM clause under the ``from_`` key (NOT ``from``) —
    # this must match ``source_sql.py``'s own subquery-WHERE unwrap, otherwise
    # the fallback is dead and a flattened identity-derived query whose
    # unresolvable WHERE lives in the inner SELECT would still leak.
    if where_node is None:
        from_clause = select_node.args.get("from_")
        if from_clause is not None and isinstance(getattr(from_clause, "this", None), _sg_exp.Subquery):
            inner = from_clause.this.this
            if isinstance(inner, _sg_exp.Select):
                where_node = inner.args.get("where")
    if where_node is None:
        return set()

    referenced: set[str] = set()
    for col in where_node.find_all(_sg_exp.Column):
        name = col.name
        if not name:
            continue
        resolved = (
            dimension_map.get(name)
            or dimension_map_lower.get(name.lower())
            or all_dim_map_for_filter.get(name)
            or all_dim_map_lower_for_filter.get(name.lower())
        )
        if resolved is None:
            continue
        canonical = getattr(resolved, "name", None)
        if canonical:
            referenced.add(canonical)
    return referenced


def _reject_unknown_unresolvable_where_columns(
    query: LogicalQuery,
    *,
    model: Model,
    dimension_map: dict,
    dimension_map_lower: dict,
    measure_map: dict,
    measure_map_lower: dict,
    all_dim_map_for_filter: dict,
    all_dim_map_lower_for_filter: dict,
    all_measure_map: dict,
    all_measure_map_lower: dict,
) -> None:
    """F-003-02: named bind error for function-wrapped unknown WHERE columns.

    After the known-name harvest, any remaining ``exp.Column`` in the raw WHERE
    that is not a modelled name, SELECT alias, or parameter raises
    ``SemanticBindingError`` with the same message as the extractable path.
    Skip when ``has_complex_sql`` (containment already walks columns).
    """
    if not getattr(query, "has_unresolvable_where", False):
        return
    if getattr(query, "has_complex_sql", False):
        return
    raw_sql = getattr(query, "raw_query", None)
    if not raw_sql:
        return

    import sqlglot as _sg
    from sqlglot import exp as _sg_exp

    input_dialect = getattr(query, "input_dialect", "postgres") or "postgres"
    try:
        ast = _sg.parse_one(raw_sql, read=input_dialect)
    except Exception:
        return

    select_node = ast if isinstance(ast, _sg_exp.Select) else ast.find(_sg_exp.Select)
    if select_node is None:
        return
    where_node = select_node.args.get("where")
    if where_node is None:
        return

    select_aliases: set[str] = set()
    for se in getattr(query, "select_expressions", []) or []:
        alias = getattr(se, "alias", None)
        if alias:
            select_aliases.add(str(alias).lower())
    for expr in select_node.expressions or []:
        if isinstance(expr, _sg_exp.Alias) and expr.alias:
            select_aliases.add(str(expr.alias).lower())

    slug = getattr(model, "slug", "") or getattr(query, "model_id", "")
    for col in where_node.find_all(_sg_exp.Column):
        if isinstance(getattr(col, "this", None), _sg_exp.Star) or col.name == "*":
            continue
        name = col.name
        if not name:
            continue
        nl = name.lower()
        if nl in select_aliases:
            continue
        if name.startswith("$") or name.startswith("@"):
            continue
        resolved = (
            dimension_map.get(name)
            or dimension_map_lower.get(nl)
            or measure_map.get(name)
            or measure_map_lower.get(nl)
            or all_dim_map_for_filter.get(name)
            or all_dim_map_lower_for_filter.get(nl)
            or all_measure_map.get(name)
            or all_measure_map_lower.get(nl)
        )
        if resolved is None:
            raise SemanticBindingError(
                f"Unknown column: {name!r} in model {slug!r}"
            )


def _resolve_model_physical_columns(
    deployed_shape: Any, *, physical_columns_fallback: set[str] | None,
) -> set[str] | None:
    """Lowercase set of ALL modelled physical column names (incl. hidden).

    F-003-02: the membership boundary for complex-SQL column containment. Reads
    the deployed snapshot's ``physical_columns_all`` (the pinned A1 authority)
    when a deployed shape exists; returns ``None`` when it cannot be resolved
    from the snapshot so the caller loads it from live tables (undeployed
    authoring paths) or fails closed.
    """
    if deployed_shape is not None:
        cols = getattr(deployed_shape, "physical_columns_all", None)
        if cols:
            return {c.lower() for c in cols}
        # A deployed shape with an EMPTY physical set is suspicious; fall through
        # to the caller's live/fallback path rather than authorising nothing.
        return None
    return physical_columns_fallback


def _scope_local_columns(select: Any) -> list[Any]:
    """Every ``exp.Column`` that belongs to THIS scope's own clauses.

    ``traverse_scope`` gives each SELECT / subquery / CTE body its own scope, but
    ``Scope.columns`` deliberately OMITS some positions — notably a column inside
    a ``HAVING`` aggregate (``HAVING SUM(secret) > 0``) — which would let an
    unmodelled column slip past a ``scope.columns``-only walk (a real containment
    bypass). Conversely, walking ``scope.expression.find_all(Column)`` re-visits
    the CTE-definition subtree that belongs to a CHILD scope (double counting).

    This collector walks every direct clause arg of the SELECT EXCEPT ``with``
    (the CTE definitions are their own scopes), and within each clause prunes any
    column that sits under a NESTED ``Select`` / ``Subquery`` (a child scope that
    ``traverse_scope`` reports separately). The result is exactly the columns
    physically evaluated in this scope's SELECT/WHERE/GROUP/HAVING/QUALIFY/ORDER/
    JOIN-ON positions.
    """
    from sqlglot import exp as _exp

    cols: list[Any] = []
    for key, arg in select.args.items():
        if key == "with" or arg is None:
            continue
        nodes = arg if isinstance(arg, list) else [arg]
        for node in nodes:
            if not isinstance(node, _exp.Expression):
                continue
            for col in node.find_all(_exp.Column):
                anc = col.parent
                nested = False
                while anc is not None and anc is not select:
                    if isinstance(anc, (_exp.Select, _exp.Subquery)) and anc is not node:
                        nested = True
                        break
                    anc = anc.parent
                if not nested:
                    cols.append(col)
    return cols


def _scope_projection_aliases(expr: Any) -> set[str]:
    """Output names THIS scope's own projection DEFINES (explicit aliases only).

    These are names the QUERY itself creates — ``SUM(x) AS total``,
    ``CASE ... END AS band``, ``ROW_NUMBER() OVER (...) AS rn`` — as opposed to
    names it READS from a source relation. A bare column projection
    (``SELECT region``) defines nothing new and is deliberately NOT collected:
    it is a physical read and must stay subject to model containment.

    Every top-level set-operation branch is enumerated, because the output names
    of a ``... UNION ...`` are the FIRST branch's aliases but any branch may
    carry them in a differently-shaped projection.
    """
    from sqlglot import exp as _exp

    aliases: set[str] = set()
    for sel in _setop_branch_selects(expr):
        for proj in sel.selects:
            if isinstance(proj, _exp.Alias):
                name = (proj.alias or "").lower()
                if name:
                    aliases.add(name)
    return aliases


def _standalone_order_by_columns(expr: Any) -> set[int]:
    """``id()`` of every column that stands ALONE as an ORDER BY term here.

    Alias visibility in SQL is CLAUSE-SPECIFIC, and only this position is safe to
    resolve against the query's own projection:

    * ``ORDER BY <name>`` — when a bare name matches BOTH an output alias and an
      input column, the standard (and PostgreSQL, BigQuery, T-SQL) resolve it to
      the OUTPUT column. The alias always wins, so exempting it from model
      containment can never hide a physical read.
    * ``GROUP BY`` / ``HAVING`` — the INPUT column wins over an output name, so a
      name that matched an unmodelled physical column would be a real physical
      read wearing an alias's clothes. Not exempted (fail closed).
    * ``WHERE`` / ``JOIN ON`` / the SELECT list itself — output names are not
      visible at all; every bare name there is an input column. Not exempted.

    The name must also STAND ALONE: ``ORDER BY total`` resolves to the output
    column, ``ORDER BY total + 1`` does not (an output name cannot be used inside
    an expression), so only the whole-term position is collected.
    """
    from sqlglot import exp as _exp

    order = expr.args.get("order") if hasattr(expr, "args") else None
    if order is None:
        return set()
    positions: set[int] = set()
    for term in (order.args.get("expressions") or []):
        node = term.this if isinstance(term, _exp.Ordered) else term
        if isinstance(node, _exp.Column):
            positions.add(id(node))
    return positions


def _standalone_group_by_columns(expr: Any) -> set[int]:
    """``id()`` of every column that stands ALONE as a GROUP BY term.

    F-003-05 / Bug-9059: PostgreSQL may GROUP BY a SELECT output alias iff
    that name is NOT also an input column. HAVING still fail-closed.
    """
    from sqlglot import exp as _exp

    group = expr.args.get("group") if hasattr(expr, "args") else None
    if group is None:
        return set()
    positions: set[int] = set()
    for term in (group.args.get("expressions") or []):
        node = term.this if isinstance(term, _exp.Alias) else term
        while isinstance(node, _exp.Paren):
            node = node.this
        if isinstance(node, _exp.Column):
            positions.add(id(node))
    return positions


def _relation_alias_column_lists(ast: Any) -> dict[str, set[str]]:
    """Published names from ``AS x(a, b)`` / ``WITH t(c) AS`` (Bug-9045)."""
    from sqlglot import exp as _exp

    out: dict[str, set[str]] = {}
    if ast is None:
        return out
    for node in ast.walk():
        alias = None
        if isinstance(node, (_exp.CTE, _exp.Table, _exp.Subquery, _exp.Lateral, _exp.Values, _exp.Unnest)):
            alias = node.args.get("alias") if hasattr(node, "args") else None
        if not isinstance(alias, _exp.TableAlias):
            continue
        cols = getattr(alias, "columns", None) or []
        if not cols:
            continue
        alias_name = (
            (alias.name or "")
            or (getattr(alias.this, "name", None) if alias.this is not None else "")
            or ""
        ).lower()
        published = {
            (c.name or "").lower()
            for c in cols
            if getattr(c, "name", None)
        }
        if alias_name and published:
            out.setdefault(alias_name, set()).update(published)
    return out


def _setop_branch_selects(expr: Any) -> list[Any]:
    """Top-level projecting SELECTs of a (possibly set-operation) sub-scope.

    For a plain ``Select`` this is ``[expr]``. For a UNION / EXCEPT / INTERSECT it
    is the SELECTs of every top-level branch (recursively through nested set-ops),
    but NOT SELECTs nested inside a branch's own subqueries (those are separate
    scopes). Used to detect a star projection in ANY branch: sqlglot reports a
    set-op scope's ``.selects`` as only its FIRST branch, so a ``... UNION SELECT *
    ...`` CTE would otherwise be misclassified NAMED and leak the star branch's
    unmodelled columns (Fable-round-3 residual).
    """
    from sqlglot import exp as _exp

    if isinstance(expr, _exp.Select):
        return [expr]
    if isinstance(expr, (_exp.Union, _exp.Except, _exp.Intersect)):
        out: list[Any] = []
        out.extend(_setop_branch_selects(expr.this))
        out.extend(_setop_branch_selects(expr.args.get("expression")))
        return out
    return []


def _proven_scope_outputs(
    scope: Any,
    cache: dict[int, set[str] | None],
    active: set[int],
) -> set[str] | None:
    """Return names provably published by a derived ``Scope``.

    Bug-9458: an identity ``SELECT *`` wrapper over another derived relation
    carries that relation's query-defined aliases (for example ``cnt`` or
    ``rate``).  ``sqlglot`` exposes the wrapper as a STAR scope, so the normal
    physical-column containment check cannot see those names.  Only a wrapper
    whose star resolves to one fully-known derived scope is accepted here;
    stars over a physical table, mixed stars, and ambiguous/set-operation shapes
    remain opaque and continue through the fail-closed model vocabulary check.
    """
    from sqlglot import exp

    key = id(scope)
    if key in cache:
        return cache[key]
    if key in active:
        # Defensive cycle guard for future sqlglot scope graph changes.
        return None
    active.add(key)
    try:
        branches = _setop_branch_selects(getattr(scope, "expression", None))
        if not branches:
            cache[key] = None
            return None

        outputs: set[str] = set()
        for branch in branches:
            projections = list(getattr(branch, "selects", None) or [])
            has_star = any(
                isinstance(proj, exp.Star)
                or (isinstance(proj, exp.Column) and isinstance(proj.this, exp.Star))
                for proj in projections
            )
            if has_star:
                # Propagation is deliberately limited to a pure identity star.
                # Mixed ``SELECT *, expression AS x`` remains covered by the
                # existing star_named_outputs path below.
                if len(projections) != 1:
                    cache[key] = None
                    return None
                star = projections[0]
                qualifier = (
                    (getattr(star, "table", "") or "").lower()
                    if isinstance(star, exp.Column)
                    else ""
                )
                sources = getattr(scope, "sources", {}) or {}
                if qualifier:
                    child = sources.get(qualifier)
                    if not isinstance(child, type(scope)):
                        cache[key] = None
                        return None
                else:
                    if len(sources) != 1:
                        cache[key] = None
                        return None
                    child = next(iter(sources.values()))
                    if not isinstance(child, type(scope)):
                        cache[key] = None
                        return None
                child_outputs = _proven_scope_outputs(child, cache, active)
                if child_outputs is None:
                    cache[key] = None
                    return None
                outputs.update(child_outputs)
                continue

            # A fully named projection is already validated in its own scope;
            # its output names are safe to publish through a later identity star.
            branch_outputs = {
                (getattr(proj, "alias_or_name", "") or "").lower()
                for proj in projections
            }
            branch_outputs.discard("")
            if len(branch_outputs) != len(projections):
                cache[key] = None
                return None
            outputs.update(branch_outputs)

        cache[key] = outputs
        return outputs
    finally:
        active.discard(key)


def _validate_complex_sql_columns(
    query: LogicalQuery,
    model: Model,
    model_physical_columns: set[str] | None,
) -> set[str]:
    """Fail closed unless every physical column in complex SQL is modelled.

    F-003-02 (SECURITY): walks every ``exp.Column`` reference across ALL scopes
    of the raw complex SQL (CTEs, derived tables, subqueries, set operations,
    window functions) and requires each PHYSICAL column reference to be a
    modelled physical column of the deployed model. Intermediate names produced
    by a CTE / derived-table (sub-scope) projection are exempt — they are result
    aliases, not physical source columns, and each sub-scope validates its OWN
    physical reads. A reference that is neither a modelled physical column nor a
    provable sub-scope output is REJECTED (``SemanticBindingError``).

    Fail-closed triggers (all raise rather than pass):
      * ``model_physical_columns`` unavailable (could not resolve the model's
        column vocabulary) — cannot prove containment.
      * raw SQL unparseable / scope analysis fails — a degraded parse could hide
        an unmodelled column reference behind a non-``exp.Column`` token.
      * any physical column reference not present in the model set.

    Returns the OUTERMOST projection's output names (lower-cased) — the column
    names the client will actually receive. Every one of them has, by the time
    this returns, been proven to be either an alias the query itself defines, a
    modelled physical column, or a validated sub-scope output; the post-execute
    result-column audit needs them so it does not mistake a name the QUERY chose
    for an unauthorised column.
    """
    from sqlglot import exp
    from sqlglot.optimizer.scope import Scope, traverse_scope

    slug = getattr(model, "slug", "") or getattr(query, "model_id", "")

    raw_sql = getattr(query, "raw_query", "") or ""
    if not raw_sql.strip():
        # No query text to analyse. A complex query with no raw text cannot have
        # its columns proven contained, but neither can it reference one — the
        # table-containment gate already rejected unknown FROM tables. Nothing to
        # validate here.
        return set()

    input_dialect = getattr(query, "input_dialect", "postgres") or "postgres"
    try:
        import sqlglot as _sg
        ast = _sg.parse_one(raw_sql, read=input_dialect)
        scopes = traverse_scope(ast)
    except Exception as e:
        # A parse / scope failure means an unmodelled column could be hidden in
        # a token we cannot classify — fail closed.
        raise SemanticBindingError(
            f"Cannot verify column containment for model {slug!r}: the query "
            f"could not be structurally analysed ({e}). Query rejected for safety."
        ) from e

    # First pass: are there any PHYSICAL column references to validate at all?
    # A pure ``SELECT *`` passthrough (no explicit column tokens) references no
    # physical column, so it needs no vocabulary — mirrors the Bug-6964 table
    # gate, which allows ``SELECT * FROM model`` under an empty snapshot. Only
    # when a physical column IS referenced do we require the model's column
    # vocabulary; if that vocabulary is unavailable, fail closed.
    _model_set = model_physical_columns or set()
    unmodelled: set[str] = set()
    saw_physical_ref = False
    relation_alias_lists = _relation_alias_column_lists(ast)
    # Bug-9458: cache only the names that can be proven to survive an identity
    # SELECT * wrapper.  Unknown/physical stars deliberately remain ``None``.
    proven_scope_outputs: dict[int, set[str] | None] = {}
    for scope in scopes:
        # Classify each CTE / derived-table (sub-scope) source of THIS scope.
        #
        # A NAMED sub-scope projects its output columns explicitly, so a reference
        # into it (``q.col`` or a bare ``col`` matching an output name) is an
        # intermediate result validated inside that sub-scope — safe to exempt.
        #
        # A STAR sub-scope (``SELECT * FROM physical_table``) enumerates NOTHING
        # by name, yet its star silently carries every PHYSICAL column of the
        # underlying (model-validated) table at execution. So a reference into a
        # star sub-scope is effectively a PHYSICAL read and MUST be a modelled
        # physical column — otherwise ``WITH q AS (SELECT * FROM model) SELECT
        # salary FROM q`` (or ``... SELECT salary AS region FROM q``, which also
        # slips the result audit) would disclose an unmodelled source column
        # (Fable FINDING-1). Its underlying physical column set is a SUBSET of the
        # model's, so the model set is the correct, sound vocabulary to check.
        #
        # Names THIS scope's own projection defines, and the ORDER BY positions
        # where SQL resolves a bare name to one of them (see the two helpers).
        projection_aliases = _scope_projection_aliases(scope.expression)
        order_alias_positions = (
            _standalone_order_by_columns(scope.expression)
            if projection_aliases else set()
        )
        group_alias_positions = (
            _standalone_group_by_columns(scope.expression)
            if projection_aliases else set()
        )
        subscope_outputs: set[str] = set()       # names from NAMED sub-scopes only
        named_subscope_aliases: set[str] = set()
        star_subscope_aliases: set[str] = set()
        star_named_outputs: dict[str, set[str]] = {}
        star_propagated_outputs: dict[str, set[str]] = {}
        physical_source_count = 0
        any_star_subscope = False
        for src_name, src in scope.sources.items():
            if isinstance(src, Scope):
                alias_lc = (src_name or "").lower()
                # Enumerate the projections of EVERY top-level branch (a set-op
                # sub-scope reports only its FIRST branch via ``.selects``, so a
                # ``... UNION SELECT * ...`` CTE would hide its star branch —
                # Fable-round-3 residual). ``_branch_projs`` is empty for a LATERAL
                # outer source Scope (Fable FINDING 2-1).
                _branch_selects = _setop_branch_selects(src.expression)
                _branch_projs = [p for s in _branch_selects for p in s.selects]
                projects_star = any(
                    isinstance(proj, exp.Star)
                    or (isinstance(proj, exp.Column) and isinstance(proj.this, exp.Star))
                    for proj in _branch_projs
                )
                # A sub-scope is NAMED (safe to exempt references into it) ONLY when
                # EVERY branch's projection is fully enumerable by name: NON-EMPTY
                # and star-free. An EMPTY projection is NOT enumerable — sqlglot
                # reports a LATERAL derived table's outer source Scope with no
                # selects (the actual ``SELECT *`` lives in a separate intermediate
                # scope), so treat it as OPAQUE (star) and fail closed. Otherwise a
                # reference into a star / set-op-star / lateral sub-scope would
                # disclose an unmodelled physical column.
                if projects_star or not _branch_projs:
                    star_subscope_aliases.add(alias_lc)
                    any_star_subscope = True
                    # Bug-9045 mixed ``*`` + named projection: the named
                    # ALIAS outputs are query-defined, not physical reads.
                    named_from_star = {
                        (getattr(proj, "alias", "") or "").lower()
                        for proj in _branch_projs
                        if isinstance(proj, exp.Alias) and getattr(proj, "alias", None)
                    }
                    if named_from_star:
                        star_named_outputs[alias_lc] = named_from_star
                    known_outputs = _proven_scope_outputs(
                        src, proven_scope_outputs, set(),
                    )
                    if known_outputs:
                        star_propagated_outputs[alias_lc] = known_outputs
                else:
                    named_subscope_aliases.add(alias_lc)
                    for proj in _branch_projs:
                        out = (getattr(proj, "alias_or_name", "") or "").lower()
                        if out:
                            subscope_outputs.add(out)
            elif isinstance(src, exp.Table):
                physical_source_count += 1

        # INTEG-06: the exemptions below that consult ``relation_alias_lists``
        # (an AST-wide ``AS x(a,b)`` / ``WITH t(c)`` map built once) must only
        # honour names a source of THIS scope publishes. Matching ANY relation's
        # published list statement-wide would suppress validation for an
        # unrelated name a DIFFERENT scope's relation happens to publish. Keys of
        # ``scope.sources`` are the relation aliases visible here.
        scope_source_aliases = {(k or "").lower() for k in scope.sources}
        scope_published_names: set[str] = set()
        values_published_counts: dict[str, int] = {}
        for _src_alias in scope_source_aliases:
            scope_published_names |= relation_alias_lists.get(_src_alias, set())
            _src = scope.sources.get(_src_alias)
            if (
                isinstance(_src, Scope)
                and isinstance(_src.expression, exp.Values)
            ):
                # The JDBC gateway's BI normalizer may remove ``v.`` from
                # references while preserving ``AS v(multiplier)``.  Keep this
                # exemption limited to explicitly published VALUES names; a
                # malformed/unknown name still falls through to containment.
                for _published_name in relation_alias_lists.get(_src_alias, set()):
                    values_published_counts[_published_name] = (
                        values_published_counts.get(_published_name, 0) + 1
                    )

        for col in _scope_local_columns(scope.expression):
            # A QUALIFIED star ``alias.*`` parses to ``Column(this=Star, name='*')``
            # and IS collected by find_all(Column) (a bare ``*`` is a plain
            # ``exp.Star`` and is NOT). It expands to the already table-contained
            # relation's physical columns and discloses nothing beyond a bare
            # ``SELECT *`` (which is exempt), so skip it — otherwise an ordinary
            # ``SELECT t.* FROM model t`` JOIN passthrough would false-reject on a
            # literal column named "*" (Fable FINDING 2-2).
            if isinstance(getattr(col, "this", None), exp.Star) or col.name == "*":
                continue
            name = (col.name or "").lower()
            if not name:
                continue
            qualifier = (col.table or "").lower()
            # Bug-9045: ``AS x(a, b)`` / ``WITH t(c) AS`` publish names that
            # are not physical source columns. INTEG-06: only honour these when
            # the qualifier / published name belongs to a source visible in THIS
            # scope, not any statement-wide relation that shares the alias.
            if (
                qualifier
                and qualifier in scope_source_aliases
                and name in relation_alias_lists.get(qualifier, set())
            ):
                continue
            if (
                not qualifier
                and physical_source_count == 0
                and name in scope_published_names
            ):
                continue
            if (
                not qualifier
                and values_published_counts.get(name) == 1
                and name not in _model_set
            ):
                continue
            # Bug-9045 mixed star: ``q.extra`` / bare ``extra`` from
            # ``SELECT *, 1 AS extra`` is a named output, not a physical read.
            if qualifier and name in star_named_outputs.get(qualifier, set()):
                continue
            # Bug-9458: a qualified reference into an identity STAR wrapper may
            # be one of the fully-proven names emitted by its named inner scope.
            # Physical-table stars never enter this map.
            if qualifier and name in star_propagated_outputs.get(qualifier, set()):
                continue
            if (
                not qualifier
                and physical_source_count == 0
                and any(name in named for named in star_named_outputs.values())
            ):
                continue
            if (
                not qualifier
                and physical_source_count == 0
                and any(name in names for names in star_propagated_outputs.values())
            ):
                continue
            # 1. Qualified by a NAMED sub-scope alias -> intermediate output name,
            #    validated inside that sub-scope. Exempt. (A STAR sub-scope alias
            #    is NOT exempt — it falls through to the physical-read check.)
            if qualifier and qualifier in named_subscope_aliases:
                continue
            # 2. Unqualified in a scope that reads ONLY sub-scopes (no physical
            #    table) AND none of them is a star sub-scope -> it can only be a
            #    NAMED sub-scope output. Exempt. If ANY star sub-scope is present,
            #    an unqualified name may be one of its (physical) star columns, so
            #    fall through to the physical-read check.
            if not qualifier and physical_source_count == 0 and not any_star_subscope:
                continue
            # 3. Unqualified name that provably matches a NAMED sub-scope output of
            #    THIS scope -> an intermediate result reference, not a physical
            #    read. Exempt.
            if not qualifier and name in subscope_outputs:
                continue
            # 3b. Unqualified name standing ALONE as an ORDER BY term of THIS
            #     scope that matches an alias THIS scope's own projection
            #     defines. SQL resolves that position to the OUTPUT column, so
            #     it is a reference to a value the query itself computed — not a
            #     physical read — even when the scope scans a physical table.
            #     Without this the ordinary BI shape ``SELECT dim, SUM(m) AS
            #     total FROM model ORDER BY total DESC`` was rejected as an
            #     unmodelled column. Restricted to ORDER BY on purpose: in every
            #     other clause an input column wins (or an alias is invisible),
            #     so exempting there could authorise a real unmodelled read.
            if (
                not qualifier
                and name in projection_aliases
                and id(col) in order_alias_positions
            ):
                continue
            # F-003-05 / Bug-9059: PostgreSQL GROUP BY of a SELECT alias is
            # an output grouping iff that name is not also a modelled input.
            # INTEG-07: require a NON-EMPTY vocabulary — with an empty ``_model_set``
            # ``name not in _model_set`` is vacuously true, so the exemption would
            # wrongly fire and let an unverifiable GROUP BY name through. Fall
            # through to the physical-read / empty-vocabulary fail-closed instead.
            if (
                not qualifier
                and name in projection_aliases
                and id(col) in group_alias_positions
                and _model_set
                and name not in _model_set
            ):
                continue
            # 4. This is a PHYSICAL column read (direct scan, or a reference into a
            #    star sub-scope whose star expands to physical columns). It must be
            #    a modelled physical column of the deployed model. Record that we
            #    saw a physical read so the empty-vocabulary case fails closed.
            saw_physical_ref = True
            if name not in _model_set:
                unmodelled.add(col.name)

    # Empty model vocabulary + at least one physical column read -> we cannot
    # prove containment. Fail closed (never authorise an unverifiable read).
    if saw_physical_ref and not _model_set:
        raise SemanticBindingError(
            f"Cannot verify column containment for model {slug!r}: the deployed "
            "model's physical columns are unavailable. Query rejected for safety."
        )

    if unmodelled:
        readable = ", ".join(sorted(unmodelled))
        raise SemanticBindingError(
            f"Unknown column(s) {readable} in model {slug!r}. Complex SQL may "
            "only reference columns exposed by the deployed model."
        )

    # Result-column vocabulary: what the OUTERMOST projection names. Reached only
    # after every physical reference above was proven contained.
    projection_names: set[str] = set()
    for sel in _setop_branch_selects(ast):
        for proj in sel.selects:
            if isinstance(proj, exp.Star) or (
                isinstance(proj, exp.Column) and isinstance(proj.this, exp.Star)
            ):
                continue
            out = (getattr(proj, "alias_or_name", "") or "").lower()
            if out:
                projection_names.add(out)
    return projection_names


async def _load_model(model_id: str, db: AsyncSession) -> Model | None:
    result = await db.execute(
        select(Model)
        .options(selectinload(Model.project))
        .where(Model.id == model_id)
    )
    return result.scalar_one_or_none()


async def _load_persona_slugs(model_id: object, db: AsyncSession) -> set[str]:
    """Return the lower-cased persona slugs defined for a model (Bug-6089).

    The JDBC gateway publishes each persona as a sibling catalogue named
    ``<model.slug>_<persona.slug>`` (see the Persona ORM docstring). Direct API
    callers may address those same names in a FROM clause, so the allow-list
    validates a ``<slug>_<persona_slug>`` suffix against the personas that
    actually exist on this model — never an arbitrary suffix, preserving the
    Bug-5193 guard.
    """
    result = await db.execute(
        select(Persona.slug).where(Persona.model_id == model_id)
    )
    return {slug.lower() for (slug,) in result.all() if slug}


async def _load_measures(model_id: object, db: AsyncSession) -> list[Measure]:
    result = await db.execute(
        select(Measure).where(Measure.model_id == model_id)
    )
    return list(result.scalars().all())


async def _load_dimensions(model_id: object, db: AsyncSession) -> list[Dimension]:
    result = await db.execute(
        select(Dimension).where(Dimension.model_id == model_id)
    )
    return list(result.scalars().all())


async def _load_hidden_column_ids(model_id: object, db: AsyncSession) -> set:
    """Return the set of ModelColumn ids whose is_hidden flag is true.

    Phase 2 of the semantic-layer plan: the semantic binder walks every
    dimension and measure back to its source column through this set to
    decide whether the object should be hidden from the business view.
    """
    result = await db.execute(
        select(ModelColumn.id)
        .join(ModelTable, ModelColumn.model_table_id == ModelTable.id)
        .where(ModelTable.model_id == model_id)
        .where(ModelColumn.is_hidden.is_(True))
    )
    return {row[0] for row in result.all()}


async def _load_physical_column_names(
    model_id: object, db: AsyncSession, *, exclude_hidden: bool = True,
) -> set[str]:
    """Return the set of physical column names across all model tables.

    Used by the security audit to validate SELECT * results where the
    source database returns physical column names that don't match
    semantic dimension/measure names.

    When ``exclude_hidden`` is True (the default for business queries),
    columns marked ``is_hidden=True`` are excluded so that a business
    ``SELECT *`` cannot authorise hidden physical columns through the
    audit whitelist.
    """
    stmt = (
        select(ModelColumn.column_name)
        .join(ModelTable, ModelColumn.model_table_id == ModelTable.id)
        .where(ModelTable.model_id == model_id)
    )
    if exclude_hidden:
        stmt = stmt.where(ModelColumn.is_hidden.is_(False))
    result = await db.execute(stmt)
    rows = result.all()
    return {row[0].lower() for row in rows}


async def _load_physical_column_ids(
    model_id: str, db: AsyncSession
) -> dict[str, str]:
    """Return a lowercase physical-column-name -> stable ModelColumn id map.

    Fallback loader for the binder's derived-expression leaf binding (§7.1) on the
    no-deployed-shape / no-live-bundle path. Mirrors ``_load_physical_column_names``
    but also selects the column id, so a derived-grain leaf can resolve its stable
    id when neither the pinned snapshot nor the cached bundle is available. Keyed by
    lowercase name to match the case-folded leaf compare (unquoted SQL identifiers
    case-fold in PostgreSQL). The id is stringified to match the manifest's
    ``input_column_ids`` vocabulary.

    Loads ALL columns (hidden included) so this path yields the SAME id vocabulary
    as the deployed-snapshot and live-bundle builders (no cache-path-dependent bind;
    hidden ACCESS is enforced by CLS, not by this id map). An AMBIGUOUS name — one
    that resolves to more than one ModelColumn across tables — is POISONED (omitted)
    so its leaf binds ``column_id=""`` and the §7.3 cond. 2 lineage gate fails closed
    to source, deterministically. An arbitrary "winner" could bind a query over
    relation A to relation B's id and fake an exact match (wrong-number serve).
    """
    stmt = (
        select(ModelColumn.id, ModelColumn.column_name)
        .join(ModelTable, ModelColumn.model_table_id == ModelTable.id)
        .where(ModelTable.model_id == model_id)
    )
    result = await db.execute(stmt)
    ids_by_name: dict[str, set[str]] = {}
    for col_id, name in result.all():
        if name and col_id is not None:
            ids_by_name.setdefault(name.lower(), set()).add(str(col_id))
    return {n: next(iter(ids)) for n, ids in ids_by_name.items() if len(ids) == 1}


async def _load_live_metadata_bundle(
    model_id: object, db: AsyncSession
) -> LiveMetadataBundle:
    """Load the binder's full live-metadata bundle in one pass (F-003-14).

    Loads measures, dimensions, hierarchy-level dimensions, hidden-column ids,
    and BOTH physical-column sets (all / visible) so the cached bundle answers
    every per-query flag combination without re-querying. The physical-column
    sets are derived from a single name+is_hidden query, replacing the two
    flag-specific ``_load_physical_column_names`` calls. Loading physical
    columns unconditionally (even when the current query is not ``SELECT *``)
    is extra work only on the first cache miss and yields identical results.
    """
    measures = await _load_measures(model_id, db)
    dimensions = await _load_dimensions(model_id, db)
    hierarchy_levels = await _load_hierarchy_level_dimensions(model_id, db)
    hidden_column_ids = await _load_hidden_column_ids(model_id, db)

    physical_columns_all: set[str] = set()
    physical_columns_visible: set[str] = set()
    _ids_by_name: dict[str, set[str]] = {}
    try:
        result = await db.execute(
            select(ModelColumn.id, ModelColumn.column_name, ModelColumn.is_hidden)
            .join(ModelTable, ModelColumn.model_table_id == ModelTable.id)
            .where(ModelTable.model_id == model_id)
        )
        for col_id, name, is_hidden in result.all():
            if not name:
                continue
            lname = name.lower()
            physical_columns_all.add(lname)
            if not is_hidden:
                physical_columns_visible.add(lname)
            # Stable column id for the derived-expression leaf binding (§7.1),
            # keyed by lowercase name. Collect the id SET per name (across all
            # tables, hidden included) and keep only unambiguous names below — a
            # name that resolves to >1 ModelColumn (same name, different tables)
            # cannot be disambiguated from an unqualified leaf, so it is poisoned
            # rather than assigned an arbitrary id (would fake a §7.3 lineage match).
            if col_id is not None:
                _ids_by_name.setdefault(lname, set()).add(str(col_id))
    except Exception:
        # Match the prior fallback's tolerance: a physical-column load failure
        # leaves the audit whitelist empty rather than failing the bind.
        physical_columns_all = set()
        physical_columns_visible = set()
        _ids_by_name = {}
    physical_column_ids: dict[str, str] = {
        n: next(iter(ids)) for n, ids in _ids_by_name.items() if len(ids) == 1
    }

    return LiveMetadataBundle(
        measures=measures,
        dimensions=dimensions,
        hierarchy_levels=hierarchy_levels,
        hidden_column_ids=hidden_column_ids,
        physical_columns_visible=physical_columns_visible,
        physical_columns_all=physical_columns_all,
        physical_column_ids=physical_column_ids,
    )


def _is_semantic_object_hidden(obj: object, hidden_column_ids: set) -> bool:
    """Return True iff the object's source column is in the hidden set."""
    if not hidden_column_ids:
        return False
    source_column_id = getattr(obj, "source_column_id", None)
    if source_column_id is None:
        return False
    return source_column_id in hidden_column_ids


async def _filter_by_from_tables(
    dimensions: list,
    measures: list,
    from_tables: list[str],
    model_id: object,
    db: AsyncSession,
) -> tuple[list, list]:
    """Filter dimensions + measures to those whose source ModelTable is
    in *from_tables*.

    CR-002 Finding 6: `SELECT *` used to expand to every dim/measure in
    the model, which poisoned the downstream ``_collect_touched_source_ids``
    check with every configured data source. This helper constrains the
    expansion to the tables the parser actually resolved from the FROM
    clause (physical_name, alias, or bare base name) so only the touched
    sources are reported. Returns unfiltered lists as a safety fallback
    when no matching ModelTable row is found.
    """
    if not from_tables:
        return list(dimensions), list(measures)

    # Normalise the requested table identifiers to a set of candidates
    # that covers the three ways ModelTable can be matched.
    candidates: set[str] = set()
    for t in from_tables:
        if not t:
            continue
        candidates.add(t.lower())
        tail = t.split(".")[-1]
        candidates.add(tail.lower())

    tbl_result = await db.execute(
        select(ModelTable).where(ModelTable.model_id == model_id)
    )
    all_tables = list(tbl_result.scalars().all())

    def _match(tbl) -> bool:
        for attr in ("physical_name", "alias", "display_name"):
            val = getattr(tbl, attr, None)
            if val and val.lower() in candidates:
                return True
            if val:
                tail = val.split(".")[-1].lower()
                if tail in candidates:
                    return True
        return False

    in_scope_table_ids = {t.id for t in all_tables if _match(t)}
    if not in_scope_table_ids:
        # Couldn't resolve any referenced table — fall through to the
        # full-model behaviour so the executor's cross-source check
        # produces a clear error instead of a silent empty result.
        return list(dimensions), list(measures)

    col_result = await db.execute(
        select(ModelColumn.id).where(
            ModelColumn.model_table_id.in_(in_scope_table_ids)
        )
    )
    in_scope_column_ids = {row[0] for row in col_result.all()}

    def _in_scope(obj) -> bool:
        cid = getattr(obj, "source_column_id", None)
        return cid is not None and cid in in_scope_column_ids

    return (
        [d for d in dimensions if _in_scope(d)],
        [m for m in measures if _in_scope(m)],
    )
