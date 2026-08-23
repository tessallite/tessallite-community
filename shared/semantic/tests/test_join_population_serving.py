"""G3/Bug-8615 contract tests for mandatory join population closure."""
from __future__ import annotations

from types import SimpleNamespace

from shared.semantic.join_population_serving import (
    DEFAULT_POPULATION_PARTICIPATION,
    POPULATION_PARTICIPATION_POPULATION_DEFINING,
    augment_required_table_ids,
    normalized_population_participation,
    population_defining_join_rows,
    population_defining_table_ids,
)


def _join(left="fact", right="dim", participation=POPULATION_PARTICIPATION_POPULATION_DEFINING):
    return {
        "left_table_id": left,
        "right_table_id": right,
        "population_participation": participation,
    }


def test_dict_orm_and_namespace_rows_share_normalization():
    assert normalized_population_participation(_join()) == POPULATION_PARTICIPATION_POPULATION_DEFINING
    assert normalized_population_participation(
        SimpleNamespace(**_join())
    ) == POPULATION_PARTICIPATION_POPULATION_DEFINING
    assert normalized_population_participation(
        {"left_table_id": "fact", "right_table_id": "dim"}
    ) == DEFAULT_POPULATION_PARTICIPATION
    # Unknown input must not silently widen the serving plan.
    assert normalized_population_participation(
        _join(participation="future-token")
    ) == "undeclared"


def test_population_defining_edges_add_both_endpoints_independent_of_projection():
    joins = [_join(), _join("fact", "calendar", "preserve_base_rows")]
    assert population_defining_table_ids(joins) == frozenset({"fact", "dim"})
    assert augment_required_table_ids(
        {"fact"}, joins, table_ids={"fact", "dim", "calendar"}
    ) == {"fact", "dim"}


def test_other_states_remain_elidable():
    joins = [
        _join("fact", "dim", "preserve_base_rows"),
        _join("fact", "calendar", "enrichment_only"),
        _join("fact", "other", "undeclared"),
    ]
    assert population_defining_join_rows(joins) == ()
    assert augment_required_table_ids(
        {"fact"}, joins, table_ids={"fact", "dim", "calendar", "other"}
    ) == {"fact"}


def test_malformed_mandatory_endpoint_fails_closed():
    malformed = _join(right=None)
    assert population_defining_join_rows([malformed]) is None
    assert population_defining_table_ids([malformed]) is None
    assert augment_required_table_ids(
        {"fact"}, [malformed], table_ids={"fact", "dim"}
    ) is None


def test_unknown_graph_endpoint_fails_closed_instead_of_dropping_edge():
    joins = [_join("fact", "ghost")]
    assert augment_required_table_ids(
        {"fact"}, joins, table_ids={"fact", "dim"}
    ) is None
