"""Bug-9940: a flat time field must participate in Excel's rollup lattice."""

from src.dax.subtotal_engine import (
    build_multi_subtotal_queries,
    detect_flat_attribute_rollups,
    uncovered_axis_dimensions,
)
from src.dax.xmla_server import _flat_rollup_attribute_names


_ACCOUNT = (
    "Hierarchize(AddCalculatedMembers("
    "{DrilldownLevel({[account_type].[account_type].[All]})}))"
)
_DATE = (
    "Hierarchize(AddCalculatedMembers("
    "{DrilldownLevel({[business_date].[business_date].[All]})}))"
)
_AXIS = f"CrossJoin({_ACCOUNT}, {_DATE})"


def test_flat_time_field_is_registered_with_the_other_excel_rollup() -> None:
    attributes_only = detect_flat_attribute_rollups(
        _AXIS,
        "",
        {"account_type"},
    )
    assert uncovered_axis_dimensions(
        {"account_type", "business_date"}, attributes_only,
    ) == {"business_date"}

    eligible_names = _flat_rollup_attribute_names(
        [
            {"name": "account_type"},
            {"name": "business_date", "is_time_dim": True, "time_grain": "day"},
        ],
        [
            {
                "id": "calendar",
                "name": "Business Calendar",
                "dimension_kind": "time",
                "levels": [{"ordinal": 0, "name": "Year"}],
            },
        ],
    )
    assert eligible_names == {"account_type", "business_date"}

    rollups = detect_flat_attribute_rollups(
        _AXIS,
        "",
        eligible_names,
    )
    assert [rollup.mdx_dim_name for rollup in rollups] == [
        "account_type",
        "business_date",
    ]
    assert uncovered_axis_dimensions(
        {"account_type", "business_date"}, rollups,
    ) == set()

    queries = build_multi_subtotal_queries(
        hierarchies=rollups,
        mdx_dims=["account_type", "business_date"],
        mdx_measures=["avg_base_amount"],
        where_sql_clauses=[],
        model_slug="modely",
        measures_meta=[
            {"name": "avg_base_amount", "default_agg": "avg"},
        ],
        measure_canonical={},
    )
    assert len(queries) == 3
    assert {tuple(query.dim_cols) for query in queries} == {
        ("account_type",),
        ("business_date",),
        (),
    }
