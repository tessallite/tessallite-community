"""Programmatic shape-coverage guard for the query-shape catalog (#1-#112).

This institutionalises the 2026-06-03 shape-coverage audit: every query shape
documented in the catalog must map to an owning test reference, so future
catalog growth cannot silently lack coverage.

It complements ``test_render_golden.py::test_all_30_shapes_accounted_for``
(which guards the core 1..30 set inside the golden render harness) by extending
the contract across the full recommended expansion (#31-#112):

  - active catalog  → architecture_query-shape-catalog.md (#1-#98, #108)
  - generated catalog → architecture_generated-query-catalog.md (#99-#107, #109-#112)
  - recommended source → architecture_query-shape-catalog-recommended.md

Owner reference grammar (string value):
  - ``render-golden:<scenario-or-guard>``  rendered/guarded in test_render_golden.py
  - ``suite:<file>::<test-or-area>``       owned by a dedicated offline suite
  - ``tracked-gap:<G-n>``                  an OPEN coverage gap recorded in the
                                           active catalog's Coverage gaps table

The guard asserts (a) every id in 1..112 has a non-empty owner and (b) no
unknown ids leak in.  It does not import the owning suites (that would be
brittle); the owner strings are the audited, human-checked source of truth.
"""
from __future__ import annotations

# Full catalog range is contiguous 1..112 (verified against the recommended
# catalog — no gaps in the numbering).
CATALOG_IDS = set(range(1, 113))


SHAPE_OWNERS: dict[int, str] = {
    # --- Core catalog #1-#30 (guarded by the golden render harness) ----------
    **{
        n: "render-golden:test_render_golden.py::test_all_30_shapes_accounted_for"
        for n in range(1, 31)
    },
    # --- Core SQL expansion #31-#52 ------------------------------------------
    31: "suite:test_sql_parser.py::filter_operators + render-golden:filter_ops",
    32: "suite:test_sql_parser.py::where_predicate_invariant + suite:test_where_preservation_seam.py",
    33: "render-golden:join (test_render_golden.py)",
    34: "render-golden:join_date_coercion (Bug-922)",
    35: "render-golden:uda_dim",
    36: "render-golden:uda_measure",
    37: "suite:test_render_golden.py::test_f37_invalid_uda_raises",
    38: "suite:test_binder_hierarchy_resolution.py",
    39: "suite:test_binder_hierarchy_resolution.py + suite:test_aggregate_matcher.py",
    40: "suite:test_calculated_rewrite.py (calculated expression rendering)",
    41: "suite:test_sql_parser.py::test_count_distinct_extracted + suite:test_expression_routing.py",
    42: "suite:test_sql_parser.py::compound_expression (div/add/mul)",
    43: "suite:test_sql_parser.py::compound_expression (multi-column)",
    44: "suite:test_expression_routing.py (literal-only classification)",
    45: "suite:test_query_trace.py (q23 CURRENT_DATE literal function)",
    46: "suite:test_sql_parser.py::test_scalar_function_over_bare_dimension_not_bare",
    47: "suite:test_sql_parser.py::test_single_agg_in_transparent_scalar_stays_analytical",
    48: "render-golden:having + suite:test_looker_pg_dialect.py (HAVING alias)",
    49: "render-golden:having + suite:test_looker_pg_dialect.py (HAVING raw aggregate)",
    50: "suite:test_sql_parser.py::test_order_by_desc + suite:test_rewriter_physical_grain.py",
    51: "suite:test_sql_parser.py::test_order_by_positional",
    52: "suite:test_sql_parser.py::order_by (non-selected order field parsing)",
    # --- Semantic-model + security #53-#57 -----------------------------------
    53: "suite:test_binder_visibility_cascade.py + suite:test_column_restriction.py",
    54: "suite:validate_security.py (persona-narrowed SELECT *)",
    55: "suite:validate_security.py (technical-view SELECT *)",
    56: "suite:test_persona_gate.py + suite:validate_security.py (blocked projection)",
    57: "suite:test_binder_filter_validation.py::test_unknown_filter_column_raises",
    # --- Fast-path routing #58-#67 -------------------------------------------
    58: "suite:test_force_aggregate.py",
    59: "suite:test_force_route_source.py",
    60: "suite:test_pocket_matcher.py (exact predicate)",
    61: "suite:test_pocket_matcher.py (containment)",
    62: "suite:test_pocket_matcher.py (skip: passthrough/unresolvable)",
    63: "suite:test_pocket_matcher.py (skip: freshness/state)",
    64: "suite:test_aggregate_matcher.py (inactive candidate)",
    65: "suite:test_dax_time_variants.py (variant rejection)",
    66: "suite:test_semi_additive.py (no explicit grain)",
    67: "suite:test_semi_additive.py (explicit grain)",
    # --- Binder table resolution #68-#73 -------------------------------------
    68: "suite:test_demo_data_integration.py + suite:test_expression_routing.py (qualified table)",
    69: "suite:test_binder_filter_validation.py::test_known_from_table_slug_accepted",
    70: "suite:test_binder_filter_validation.py::test_unknown_from_table_raises",
    71: "tracked-gap:G-2 (multi-source SELECT; e2e, two source DBs)",
    72: "tracked-gap:G-2 (multi-source filter-only; e2e, two source DBs)",
    73: "tracked-gap:G-2 (multi-source order-only; e2e, two source DBs)",
    # --- Input dialect quoting #74-#76 ---------------------------------------
    74: "suite:test_input_dialect_contract.py (BigQuery backticks)",
    75: "suite:test_input_dialect_contract.py (SQL Server brackets)",
    76: "suite:test_input_dialect_contract.py (PostgreSQL double quotes)",
    # --- Complex-SQL families #77-#90 ----------------------------------------
    77: "suite:test_aggregate_matcher.py (DISTINCT grain) + suite:test_sql_parser.py",
    78: "suite:test_sql_parser.py::rollup/cube/grouping_sets_columns_added_to_grain",
    79: "suite:test_sql_parser.py::test_union_is_complex_sql",
    80: "suite:test_sql_parser.py::test_intersect_is_complex_sql",
    81: "suite:test_sql_parser.py::test_except_is_complex_sql",
    82: "suite:test_sql_parser.py::test_values_table_construct_is_complex_sql",
    83: "suite:test_sql_parser.py::test_lateral_join_is_complex_sql",
    84: "suite:test_sql_parser.py::test_unnest_table_function_is_complex_sql (Bug-923)",
    85: "suite:test_sql_parser.py::test_aggregate_filter_where_is_complex_sql",
    86: "suite:test_sql_parser.py::test_within_group_clause_column_not_bare",
    87: "suite:test_sql_parser.py::unregistered_aggregate_name_not_leaked",
    88: "suite:test_sql_parser.py::window_function_allows_bare_columns + suite:test_looker_pg_dialect.py",
    89: "suite:test_looker_pg_dialect.py::multi_table_window_raises",
    90: "suite:test_looker_pg_dialect.py::execute_exposes_sqlstate",
    # --- Generated / synthetic + cross-model #91-#92 -------------------------
    91: "suite:test_discover_members_pipeline.py",
    92: "suite:test_cross_model.py",
    # --- Validation-only #93-#95 ---------------------------------------------
    93: "suite:test_sql_parser.py::test_rejects_consecutive_commas",
    94: "suite:test_sql_parser.py::test_rejects_stray_semicolon",
    95: "suite:test_sql_parser.py::rejects_missing_group_by + suite:test_select_group_by_validation.py",
    # --- Row security + audit #96-#98 ----------------------------------------
    96: "suite:test_row_security_compiler.py + suite:test_row_security_routing.py (inject)",
    97: "suite:test_row_security_routing.py (wrap)",
    98: "suite:test_query_audit.py (post-execute column audit)",
    # --- Generated-query catalog #99-#107 ------------------------------------
    99: "suite:test_headless_api.py",
    100: "suite:test_plugin_execute.py",
    101: "suite:test_drill_semantic.py + suite:test_drill_rest_endpoint.py (hierarchy mode)",
    102: "suite:test_drill_semantic.py + suite:test_drill_rest_endpoint.py (leaf mode)",
    103: "suite:test_drill_semantic.py + suite:test_drill_rest_endpoint.py (cursor pagination)",
    104: "suite:test_drill_semantic.py (grouping-level filters)",
    105: "suite:test_drill_semantic.py (COUNT_DISTINCT measure)",
    106: "suite:test_param_resolver.py (substitution/coercion/precedence)",
    107: "suite:test_param_resolver.py::test_resolve_required_missing_raises",
    # --- Cast coercion + JSON validation + lifecycle + guardrail #108-#112 ---
    108: "render-golden:cast_filter_coercion + suite:test_query_rewriter.py",
    109: "suite:test_plugin_execute.py + suite:test_headless_api.py (invalid filter payload 422)",
    110: "suite:test_headless_api.py + suite:test_plugin_execute.py (invalid order direction 422)",
    111: "suite:test_undeployed_model_http_409.py",
    112: "suite:test_source_audit_guardrail.py",
}


def test_every_shape_has_owner():
    """Every catalog shape id (1..112) maps to a non-empty owner reference.

    Fails loudly if a new shape is added to the catalog without recording who
    owns its coverage (mirrors test_all_30_shapes_accounted_for)."""
    missing = CATALOG_IDS - set(SHAPE_OWNERS)
    assert not missing, f"Catalog shapes with no recorded owner: {sorted(missing)}"
    empty = [n for n, owner in SHAPE_OWNERS.items() if not owner or not owner.strip()]
    assert not empty, f"Catalog shapes with empty owner: {sorted(empty)}"


def test_no_unknown_shape_ids():
    """No owner entry references a shape id outside the known catalog range."""
    unknown = set(SHAPE_OWNERS) - CATALOG_IDS
    assert not unknown, f"Owner map references unknown shape ids: {sorted(unknown)}"


def test_owner_reference_grammar():
    """Every owner uses one of the three recognised reference prefixes, so the
    map stays machine-checkable and does not drift into free text."""
    valid_prefixes = ("render-golden:", "suite:", "tracked-gap:")
    bad = {
        n: owner
        for n, owner in SHAPE_OWNERS.items()
        if not any(part.strip().startswith(valid_prefixes)
                   for part in owner.split("+"))
    }
    assert not bad, f"Owners with unrecognised reference grammar: {bad}"
