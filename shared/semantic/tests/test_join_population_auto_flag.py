"""G4 safe introspection auto-flag contract."""
from __future__ import annotations

from types import SimpleNamespace

from shared.semantic.join_population_auto_flag import auto_flag_safe_population_joins


def _fixture(*, join_type="left", key_count=1, participation=None, source=None):
    fact_id, dim_id = "fact", "dim"
    fact_fk, dim_key = "fact-fk", "dim-key"
    tables = [
        SimpleNamespace(id=fact_id, table_type="fact"),
        SimpleNamespace(id=dim_id, table_type="dim_detail"),
    ]
    columns = [
        SimpleNamespace(id=fact_fk, model_table_id=fact_id, is_primary_key=False),
    ]
    for idx in range(key_count):
        columns.append(
            SimpleNamespace(
                id=dim_key if idx == 0 else f"dim-key-{idx}",
                model_table_id=dim_id,
                is_primary_key=True,
            )
        )
    join = SimpleNamespace(
        left_table_id=fact_id,
        right_table_id=dim_id,
        left_column_id=fact_fk,
        right_column_id=dim_key,
        join_type=join_type,
        population_participation=participation,
        population_participation_source=source,
    )
    return join, tables, columns


def test_g4_auto_flags_only_a_structurally_proven_left_fact_dimension_key():
    join, tables, columns = _fixture()

    flagged = auto_flag_safe_population_joins(
        [join], tables, columns, introspected_table_ids={"dim"}
    )

    assert flagged == [join]
    assert join.population_participation == "preserve_base_rows"
    assert join.population_participation_source == "auto"


def test_g4_auto_flag_fails_closed_for_missing_or_ambiguous_key():
    missing, tables, columns = _fixture(key_count=0)
    ambiguous, _, ambiguous_columns = _fixture(key_count=2)

    assert auto_flag_safe_population_joins(
        [missing], tables, columns, introspected_table_ids={"dim"}
    ) == []
    assert auto_flag_safe_population_joins(
        [ambiguous], tables, ambiguous_columns, introspected_table_ids={"dim"}
    ) == []
    assert missing.population_participation is None
    assert ambiguous.population_participation is None


def test_g4_auto_flag_does_not_guess_orientation_or_overwrite_manual_state():
    reverse, tables, columns = _fixture(join_type="right")
    manual, _, _ = _fixture(participation="undeclared")

    assert auto_flag_safe_population_joins(
        [reverse, manual], tables, columns, introspected_table_ids={"dim"}
    ) == []
    assert reverse.population_participation is None
    assert manual.population_participation == "undeclared"


def test_g4_auto_flag_preserves_explicit_default_owned_as_manual():
    manual, tables, columns = _fixture(
        participation="preserve_base_rows", source="manual"
    )

    assert auto_flag_safe_population_joins(
        [manual], tables, columns, introspected_table_ids={"dim"}
    ) == []
    assert manual.population_participation == "preserve_base_rows"
    assert manual.population_participation_source == "manual"


def test_g4_auto_flag_requires_the_declared_dimension_endpoint_to_be_the_key():
    join, tables, columns = _fixture()
    join.right_column_id = "non-key-column"
    columns.append(
        SimpleNamespace(
            id="non-key-column", model_table_id="dim", is_primary_key=False
        )
    )

    assert auto_flag_safe_population_joins(
        [join], tables, columns, introspected_table_ids={"dim"}
    ) == []


def test_g4_auto_flag_requires_both_join_endpoints_in_the_catalogue():
    join, tables, columns = _fixture()
    columns = [column for column in columns if column.id != join.left_column_id]

    assert auto_flag_safe_population_joins(
        [join], tables, columns, introspected_table_ids={"fact", "dim"}
    ) == []
    assert join.population_participation is None
