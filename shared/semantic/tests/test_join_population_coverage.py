"""Fail-closed inventory guards for G3 consumers and wrappers."""
from __future__ import annotations

from pathlib import Path

from shared.semantic.join_population_coverage import audit_consumer_inventory


def test_every_inventoried_production_consumer_is_wired():
    root = Path(__file__).resolve().parents[3]
    assert audit_consumer_inventory(root) == []


def test_projection_only_mutation_is_rejected_by_inventory():
    root = Path(__file__).resolve().parents[3]
    path = "services/query-router/src/rewrite/table_resolution.py"
    source = (root / path).read_text(encoding="utf-8")
    mutated = source.replace(
        "augment_required_table_ids(\n        required_table_ids,",
        "removed_population_contract(\n        required_table_ids,",
        1,
    )
    assert mutated != source
    problems = audit_consumer_inventory(root, source_overrides={path: mutated})
    assert any("table_resolution.py:_resolve_required_and_base_tables" in problem for problem in problems)


def test_dead_contract_call_mutation_is_rejected_by_inventory():
    """Bug-8745: a bare shared call is not a serving contract."""
    root = Path(__file__).resolve().parents[3]
    path = "services/query-router/src/rewrite/table_resolution.py"
    source = (root / path).read_text(encoding="utf-8")
    mutated = source.replace(
        "_required_with_population = augment_required_table_ids(",
        "augment_required_table_ids(",
        1,
    )
    assert mutated != source
    problems = audit_consumer_inventory(root, source_overrides={path: mutated})
    assert any(
        "table_resolution.py:_resolve_required_and_base_tables" in problem
        and "not propagated" in problem
        for problem in problems
    )


def test_discarded_contract_result_mutation_is_rejected_by_inventory():
    """Bug-8745: assigning the closure to an unused name must fail closed."""
    root = Path(__file__).resolve().parents[3]
    path = "services/query-router/src/rewrite/raw_sql.py"
    source = (root / path).read_text(encoding="utf-8")
    mutated = source.replace(
        "population_required = augment_required_table_ids(",
        "_discarded_population_required = augment_required_table_ids(",
        1,
    )
    assert mutated != source
    problems = audit_consumer_inventory(root, source_overrides={path: mutated})
    assert any(
        "raw_sql.py:rewrite_for_raw" in problem
        and "not propagated" in problem
        for problem in problems
    )


def test_bug_l3_r1_b02_overwrite_after_contract_call_is_rejected():
    """An assigned contract value killed before the plan sink must fail."""
    root = Path(__file__).resolve().parents[3]
    path = "services/query-router/src/rewrite/raw_sql.py"
    source = (root / path).read_text(encoding="utf-8")
    marker = "    if population_required is None:\n"
    mutated = source.replace(
        marker,
        "    population_required = set()  # L3-R1-B02 overwrite mutation\n" + marker,
        1,
    )
    assert mutated != source
    problems = audit_consumer_inventory(root, source_overrides={path: mutated})
    assert any(
        "raw_sql.py:rewrite_for_raw" in problem
        and "not propagated" in problem
        for problem in problems
    )


def test_bug_l3_r1_b02_vacuous_observation_before_overwrite_fails_closed():
    """Bug L3-R1-B02: observation calls are not serving sinks."""
    root = Path(__file__).resolve().parents[3]
    path = "services/query-router/src/rewrite/raw_sql.py"
    source = (root / path).read_text(encoding="utf-8")
    marker = "    if population_required is None:\n"
    mutated = source.replace(
        marker,
        "    tuple(population_required)  # vacuous observation mutation\n"
        "    population_required = set()  # kill before serving sink\n"
        + marker,
        1,
    )
    assert mutated != source
    problems = audit_consumer_inventory(root, source_overrides={path: mutated})
    assert any(
        "raw_sql.py:rewrite_for_raw" in problem
        and "not propagated" in problem
        for problem in problems
    )


def test_bug_l3_ch_001_star_consumers_require_population_closure():
    """Both SELECT-* source consumers must retain the G3 closure edge."""
    root = Path(__file__).resolve().parents[3]

    substitute_path = "services/query-router/src/rewrite/source_sql.py"
    source = (root / substitute_path).read_text(encoding="utf-8")
    substitute_mutation = source.replace(
        "population_from_clause = await _population_star_from_clause(",
        "population_from_clause = await removed_population_star_from_clause(",
        1,
    )
    assert substitute_mutation != source
    problems = audit_consumer_inventory(
        root, source_overrides={substitute_path: substitute_mutation},
    )
    assert any(
        "source_sql.py:_substitute_table_names" in problem
        for problem in problems
    )

    persona_mutation = source.replace(
        "required_table_ids = augment_required_table_ids(\n"
        "        required_table_ids, joins, table_ids=tables_by_id,\n"
        "    )",
        "required_table_ids = removed_population_contract(\n"
        "        required_table_ids, joins, table_ids=tables_by_id,\n"
        "    )",
        1,
    )
    assert persona_mutation != source
    problems = audit_consumer_inventory(
        root, source_overrides={substitute_path: persona_mutation},
    )
    assert any(
        "source_sql.py:_build_persona_star_sql" in problem
        for problem in problems
    )


def test_matcher_population_field_mutation_is_rejected_by_inventory():
    root = Path(__file__).resolve().parents[3]
    path = "services/query-router/src/routing/pocket_matcher.py"
    source = (root / path).read_text(encoding="utf-8")
    marker = "population_participation=normalized_population_participation(row),"
    mutated = source.replace(marker, "", 1)
    assert mutated != source
    problems = audit_consumer_inventory(root, source_overrides={path: mutated})
    assert any("pocket_matcher.py:_edges_from_rows" in problem for problem in problems)
