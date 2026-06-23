"""
Semantic Binder — resolves measure and dimension names in a LogicalQuery
against the semantic model stored in the metadata DB.

Also loads the model's active aggregates with their columns so the matcher
can work without additional DB calls.
"""
from __future__ import annotations

import logging
import re
import types

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
)
from shared.semantic.hierarchy_resolver import load_hierarchy_level_dimensions as _load_hierarchy_level_dimensions
from src.ir.logical_query import (
    BoundQuery,
    LogicalFilter,
    LogicalQuery,
    ModelNotDeployedError,
    SemanticBindingError,
)
from src.semantic.snapshot_resolver import (
    LiveMetadataBundle,
    hierarchy_level_dimensions_from_snapshot,
    resolve_deployed_shape,
    resolve_live_metadata_bundle,
)

logger = logging.getLogger(__name__)

# Variant suffixes the FROM-table allow-list recognises as legitimate model
# views: the technical view plus the persona-scoped variants the gateway
# resolves. Bug-5193: unrecognised ``<slug>_*`` suffixes are now REJECTED
# with SemanticBindingError (previously they were only logged and silently
# bound to base-model data, allowing fabricated names to return real data).
_KNOWN_VARIANT_SUFFIXES = ("_technical",)


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
        if slug:
            for ft in from_tables:
                ft_lower = ft.lower()
                if ft_lower == slug:
                    allowed.add(ft_lower)
                elif ft_lower.startswith(slug + "_"):
                    # Bug-5193: only KNOWN variant suffixes (_technical, and
                    # persona-scoped names the gateway resolves) are legitimate.
                    # Previously ANY ``<slug>_*`` suffix was silently accepted and
                    # bound to base-model data, so fabricated names like
                    # ``modely_fake`` returned real data instead of failing.
                    # Now: known suffixes are allowed; unknown suffixes are
                    # REJECTED with SemanticBindingError.
                    suffix = ft_lower[len(slug):]
                    if suffix in _KNOWN_VARIANT_SUFFIXES:
                        allowed.add(ft_lower)
                    else:
                        raise SemanticBindingError(
                            f"Unknown model variant {ft!r}. The model "
                            f"{model.slug!r} does not have a variant "
                            f"with suffix {suffix!r}."
                        )
        cte_names = {a.lower() for a in getattr(query, "cte_aliases", []) or []}
        # Bug-5192: _extract_from_tables collects table references from ALL
        # scopes including CTE bodies. CTE-body table refs are physical DB
        # tables (e.g. WITH sales AS (SELECT * FROM sales_raw) ...) that the
        # passthrough-with-table-substitution path handles at rewrite time.
        # The bare cte_names set only contains CTE ALIAS names, not the
        # tables referenced INSIDE CTE bodies, so a CTE-body physical table
        # would be rejected as "Unknown table". Build a proper exclusion set
        # by extracting the table names referenced inside each CTE body.
        cte_body_tables: set[str] = set()
        if cte_names:
            try:
                import sqlglot as _sg
                from sqlglot import exp as _sg_exp
                _input_dialect = getattr(query, "input_dialect", "postgres") or "postgres"
                _ast = _sg.parse_one(query.raw_query, read=_input_dialect)
                _with_node = _ast.find(_sg_exp.With)
                if _with_node:
                    for _cte in _with_node.expressions:
                        if isinstance(_cte, _sg_exp.CTE):
                            for _tbl in _cte.find_all(_sg_exp.Table):
                                if _tbl.name:
                                    cte_body_tables.add(_tbl.name.lower().split(".")[-1])
            except Exception:
                # Parse failure: leave cte_body_tables empty. The FROM
                # validation below will still pass CTE-alias references
                # through cte_names; physical tables inside unparseable
                # CTE bodies that happen to share a name with a model
                # slug will still be allowed. This is the safe direction
                # (over-allow, not over-reject) for complex SQL that goes
                # through passthrough-with-table-substitution.
                logger.debug(
                    "CTE body table extraction failed for model %s; "
                    "proceeding with bare CTE-alias filtering",
                    getattr(model, "slug", "?"),
                )
        for ft in from_tables:
            ft_lower = ft.lower().split(".")[-1]
            if ft_lower not in allowed and ft_lower not in cte_names and ft_lower not in cte_body_tables:
                raise SemanticBindingError(
                    f"Unknown table {ft!r} in FROM clause. "
                    f"Use the model name {model.slug!r} instead."
                )

    # B15 / F-013-01 (gate G1 Option A): when the model is deployed, resolve
    # the semantic SHAPE (measures, dimensions, hidden-column flags, physical
    # column names, hierarchy-level dimensions) from the deployed version's
    # immutable snapshot instead of the live editable tables. This pins what
    # BI tools see to the deployed contract — draft edits do not leak until
    # the next Deploy. Row security, personas, data-tags, aggregates and
    # pockets stay live (resolved elsewhere). See
    # docs/architecture/architecture_b15-deploy-snapshot-pinning-design.md.
    deployed_shape = await resolve_deployed_shape(model, db)

    live_bundle = None
    if deployed_shape is not None:
        measures = list(deployed_shape.measures)
        dimensions = list(deployed_shape.dimensions)
        hierarchy_levels = hierarchy_level_dimensions_from_snapshot(deployed_shape)
    else:
        # F-003-14: the live-load fallback (seed v1 / empty-snapshot models)
        # ran up to five sequential metadata queries per execution. Cache the
        # whole bundle keyed by (model_id, deployed_version_id) — immutable per
        # deployed version, key changes on re-deploy (multi-replica safe). The
        # loader loads measures/dimensions/hierarchy-levels PLUS hidden-column
        # ids and physical column names unconditionally so the cached bundle is
        # complete; the per-query include_hidden / select_star flags are applied
        # to the cached data below exactly as before, so results are identical.
        live_bundle = await resolve_live_metadata_bundle(
            model, db, loader=lambda: _load_live_metadata_bundle(model.id, db),
        )
        if live_bundle is not None:
            measures = list(live_bundle.measures)
            dimensions = list(live_bundle.dimensions)
            hierarchy_levels = list(live_bundle.hierarchy_levels)
        else:
            # No deploy pointer (binder gate normally precludes this) — load live.
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
        elif live_bundle is not None:
            hidden_column_ids = live_bundle.hidden_column_ids
        else:
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
    if getattr(query, "has_complex_sql", False):
        resolved_dimensions = []
        resolved_measures = []
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
    _is_passthrough = (
        getattr(query, "has_complex_sql", False)
        or getattr(query, "has_unresolvable_where", False)
    )
    resolved_filters = []
    for f in query.filters:
        if f.dimension_name in dimension_map:
            resolved_filters.append(
                LogicalFilter(f.dimension_name, f.operator, f.value)
            )
        elif f.dimension_name.lower() in _dim_map_lower:
            canonical = _dim_map_lower[f.dimension_name.lower()]
            resolved_filters.append(
                LogicalFilter(canonical, f.operator, f.value)
            )
        elif f.dimension_name in measure_map or f.dimension_name.lower() in _measure_map_lower:
            canonical = measure_map.get(f.dimension_name) or _measure_map_lower.get(f.dimension_name.lower())
            resolved_filters.append(
                LogicalFilter(canonical.name if hasattr(canonical, "name") else f.dimension_name, f.operator, f.value)
            )
        elif f.dimension_name in _all_dim_map_for_filter:
            # Hidden dimension — valid in WHERE predicate; persona gate blocks
            # it from appearing in SELECT results.
            resolved_filters.append(
                LogicalFilter(f.dimension_name, f.operator, f.value)
            )
        elif f.dimension_name.lower() in _all_dim_map_lower_for_filter:
            # Case-insensitive match against a hidden dimension.
            canonical_dim = _all_dim_map_lower_for_filter[f.dimension_name.lower()]
            resolved_filters.append(
                LogicalFilter(canonical_dim.name, f.operator, f.value)
            )
        elif _is_passthrough:
            resolved_filters.append(f)
        else:
            raise SemanticBindingError(
                f"Unknown filter column: {f.dimension_name!r} in model {model.slug!r}"
            )
            
    # Strip surrounding SQL identifier quote characters (double-quote or backtick)
    # before comparing raw_text to inner_column. A bare quoted column like
    # "col_name" has raw_text='"col_name"' but inner_column='col_name'; they
    # are the same expression and must NOT force the passthrough rewrite path.
    # Complex expressions (CASE, COALESCE, aliased columns like "col" AS "alias")
    # contain spaces or multi-token structure that the regex won't strip, so
    # they continue to trigger passthrough correctly.
    _BARE_QUOTED_IDENT = re.compile(r'^["`]([^"`\s]+)["`]$')

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
        if getattr(m, "cross_model_source_model_id", None) is not None:
            raise CrossModelNotResolvedError(
                measure_slug=m.name,
                source_model_id=str(m.cross_model_source_model_id),
            )

    # For SELECT *, load the physical column names so the security audit
    # can validate result columns that come back as physical names rather
    # than semantic dimension/measure names.  On business queries
    # (include_hidden=False), hidden columns are excluded so the audit
    # whitelist does not authorise them.
    physical_columns: set[str] = set()
    if query.select_star:
        if deployed_shape is not None:
            physical_columns = (
                deployed_shape.physical_columns_all
                if include_hidden
                else deployed_shape.physical_columns_visible
            )
        elif live_bundle is not None:
            physical_columns = (
                live_bundle.physical_columns_all
                if include_hidden
                else live_bundle.physical_columns_visible
            )
        else:
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

    return BoundQuery(
        logical_query=query,
        model=model,
        resolved_measures=resolved_measures,
        resolved_dimensions=resolved_dimensions,
        resolved_filters=resolved_filters,
        resolved_dimensions_by_name=dimension_map,
        has_passthrough_expressions=has_passthrough,
        uses_invalid_objects=uses_invalid,
        persona_narrowed_star=narrowed_star,
        allowed_physical_columns=physical_columns,
    )


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
            selectinload(AggregateDefinition.columns).selectinload(AggregateColumn.measure)
        )
    )
    return list(result.scalars().all())


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

async def _load_model(model_id: str, db: AsyncSession) -> Model | None:
    result = await db.execute(
        select(Model)
        .options(selectinload(Model.project))
        .where(Model.id == model_id)
    )
    return result.scalar_one_or_none()


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
    try:
        result = await db.execute(
            select(ModelColumn.column_name, ModelColumn.is_hidden)
            .join(ModelTable, ModelColumn.model_table_id == ModelTable.id)
            .where(ModelTable.model_id == model_id)
        )
        for name, is_hidden in result.all():
            if not name:
                continue
            lname = name.lower()
            physical_columns_all.add(lname)
            if not is_hidden:
                physical_columns_visible.add(lname)
    except Exception:
        # Match the prior fallback's tolerance: a physical-column load failure
        # leaves the audit whitelist empty rather than failing the bind.
        physical_columns_all = set()
        physical_columns_visible = set()

    return LiveMetadataBundle(
        measures=measures,
        dimensions=dimensions,
        hierarchy_levels=hierarchy_levels,
        hidden_column_ids=hidden_column_ids,
        physical_columns_visible=physical_columns_visible,
        physical_columns_all=physical_columns_all,
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

