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

The guard asserts (a) every id in 1..112 has a non-empty owner, (b) no unknown
ids leak in, and (c) any owner that names an EXPLICIT offline test function
(``suite:<file>.py::test_<name>``) resolves to a node that is actually
collectable by pytest in this suite.

Rule (c) closes the F-003-06 / Bug-8110 escape: previously the map could name
an owner file that did not exist in the offline suite (shapes #71-#73 pointed at
``test_multi_source_reject.py`` — a docker-bound e2e test that lives under
``tessallite/tests/e2e/`` and is NOT collectable here), so the "green" gate
certified coverage that could never run offline.  Named-node owners are now
verified against ``pytest --collect-only``; owners that only describe an AREA
(``suite:<file>.py (area)`` with no ``::test_`` node) are documentation and are
not node-verified — but their referenced ``.py`` file must still exist in the
offline suite.

Coverage that genuinely cannot run offline (needs two live source DBs, a live
stack, etc.) must be recorded as a ``tracked-gap:<G-n>`` matching the catalog's
Coverage gaps / RG table — NOT dressed up as a ``suite:`` owner.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

# Full catalog range is contiguous 1..112 (verified against the recommended
# catalog — no gaps in the numbering).
CATALOG_IDS = set(range(1, 113))

_TESTS_DIR = Path(__file__).resolve().parent

# Matches an owner fragment that names an explicit offline test FUNCTION, e.g.
# ``suite:test_sql_parser.py::test_order_by_desc``.  Only ``test_``-prefixed
# names are treated as node references; anything else after ``::`` (or in
# parentheses) is a free-text AREA descriptor, not a node id.
_NODE_REF_RE = re.compile(r"(test_[A-Za-z0-9_]+\.py)::(test_[A-Za-z0-9_]+)")

# Matches the leading ``.py`` file of a ``suite:`` owner fragment, whether or
# not it carries a ``::node`` or ``(area)`` suffix.  Accepts both offline pytest
# suites (``test_*.py``) and the deployed validation suites (``validate_*.py``),
# which are real files in this directory even though pytest does not collect them
# offline (they run via ``python tests/validate_*.py`` against a live stack).
_SUITE_FILE_RE = re.compile(r"^([A-Za-z0-9_]+\.py)")


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
    41: "suite:test_sql_parser.py::test_count_distinct_extracted_as_measure + suite:test_expression_routing.py",
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
    # #71-#73 need TWO live source databases to exercise the cross-source reject
    # end to end, so they cannot be owned by an offline query-router suite. The
    # real exercise is the docker-bound e2e test
    # ``tessallite/tests/e2e/test_multi_source_reject.py::test_cross_source_reject_71_72_73``
    # (RG-6 / coverage gap G-2 in architecture_query-shape-catalog.md). Recording
    # them as tracked gaps keeps the gate honest: they are OPEN offline, covered
    # only when the two-DB e2e stack is available.
    71: "tracked-gap:G-2 (multi-source SELECT reject; e2e-only, tests/e2e/test_multi_source_reject.py)",
    72: "tracked-gap:G-2 (multi-source filter-only reject; e2e-only, tests/e2e/test_multi_source_reject.py)",
    73: "tracked-gap:G-2 (multi-source order-only reject; e2e-only, tests/e2e/test_multi_source_reject.py)",
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
    93: "suite:test_sql_parser.py::test_jdbc_rejects_consecutive_commas",
    94: "suite:test_sql_parser.py::test_jdbc_rejects_stray_semicolon",
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


def _iter_suite_fragments():
    """Yield (shape_id, fragment) for every ``suite:`` owner fragment."""
    for shape_id, owner in SHAPE_OWNERS.items():
        for part in owner.split("+"):
            part = part.strip()
            if part.startswith("suite:"):
                yield shape_id, part[len("suite:"):].strip()


def test_suite_owner_files_exist_offline():
    """Every ``suite:`` owner names a test file that exists in THIS offline suite.

    Root-cause guard for F-003-06 / Bug-8110: the shape-coverage gate previously
    accepted an owner file that did not exist in the offline query-router suite
    (``test_multi_source_reject.py`` lives under ``tessallite/tests/e2e/``), so a
    green gate certified coverage that could never run here.  A shape whose real
    exercise is e2e/live-only must be recorded as a ``tracked-gap:`` instead.
    """
    missing: dict[int, str] = {}
    for shape_id, fragment in _iter_suite_fragments():
        m = _SUITE_FILE_RE.match(fragment)
        assert m, (
            f"shape #{shape_id}: suite owner fragment {fragment!r} does not "
            f"start with a test_*.py file name"
        )
        fname = m.group(1)
        if not (_TESTS_DIR / fname).is_file():
            missing[shape_id] = fname
    assert not missing, (
        "suite: owners referencing a file absent from the offline query-router "
        f"suite (record these as tracked-gap: instead): {missing}"
    )


def _defined_test_functions(fname: str) -> set[str] | None:
    """Return the set of ``test_*`` function/method names DEFINED in a suite file.

    Uses ``ast`` — a static parse, so it never imports the module, never needs
    the service ``.env``, and never re-runs pytest collection.  That keeps this
    a pure coding-tier guard (no Docker, no live deps) while still detecting a
    renamed or deleted test.  Returns ``None`` if the file does not exist.

    Method-level tests (``def test_x`` inside a ``class TestFoo``) are included
    by their bare function name, matching how the owner map cites them.
    """
    path = _TESTS_DIR / fname
    if not path.is_file():
        return None
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()

    def _walk(node) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if child.name.startswith("test_"):
                    names.add(child.name)
            elif isinstance(child, ast.ClassDef):
                _walk(child)

    _walk(tree)
    return names


def test_named_suite_nodes_are_collectable():
    """Every owner that names an explicit ``::test_<name>`` node must be defined.

    Teeth behind rule (c): if an owner claims a specific offline test owns a
    shape, that test must actually exist.  Renamed, deleted, or file-relocated
    tests (e.g. an e2e test cited as an offline suite owner) can no longer keep
    the gate green.  Area descriptors (no ``::test_`` node) are intentionally not
    checked here — file existence is covered by
    ``test_suite_owner_files_exist_offline``.
    """
    dangling: dict[int, str] = {}
    checked_any = False
    defined_cache: dict[str, set[str] | None] = {}
    for shape_id, fragment in _iter_suite_fragments():
        m = _NODE_REF_RE.search(fragment)
        if not m:
            continue
        checked_any = True
        fname, func = m.group(1), m.group(2)
        if fname not in defined_cache:
            defined_cache[fname] = _defined_test_functions(fname)
        defined = defined_cache[fname]
        if defined is None or func not in defined:
            dangling[shape_id] = f"{fname}::{func}"
    assert checked_any, (
        "no named suite nodes found to verify — the collectability guard would "
        "be a no-op; check the owner grammar"
    )
    assert not dangling, (
        "shape owners naming a test node not defined in the offline suite "
        f"(renamed/deleted/e2e-only?): {dangling}"
    )
