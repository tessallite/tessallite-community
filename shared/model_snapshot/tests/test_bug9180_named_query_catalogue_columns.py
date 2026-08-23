"""Bug-9180: deployed star Named Queries publish their real semantic columns.

Test escape: catalogue tests started from hand-built deployed rows, while the
snapshot producer still copied the authoring-time ``*`` placeholder unchanged.
Guard: the deployment serializer's pure output-column derivation. Tier: T2.
"""
from shared.model_snapshot.serialiser import (
    _deployed_named_query_output_columns,
)


def _snapshot() -> dict:
    return {
        "columns": [
            {"id": "c-region", "data_type": "text", "is_hidden": False},
            {"id": "c-date", "data_type": "date", "is_hidden": False},
            {"id": "c-active", "data_type": "boolean", "is_hidden": False},
            {"id": "c-secret", "data_type": "text", "is_hidden": True},
            {"id": "c-revenue", "data_type": "numeric", "is_hidden": False},
        ],
        "dimensions": [
            {"name": "Region", "source_column_id": "c-region"},
            {"name": "Sale Date", "source_column_id": "c-date"},
            {"name": "Is Active", "source_column_id": "c-active"},
            {"name": "Secret", "source_column_id": "c-secret"},
        ],
        "measures": [
            {
                "name": "Revenue",
                "measure_type": "standard",
                "variant_kind": None,
                "source_column_id": "c-revenue",
            },
            {
                "name": "Margin",
                "measure_type": "calculated",
                "variant_kind": None,
                "source_column_id": None,
            },
        ],
    }


def test_bug9180_star_catalogue_uses_deployed_semantic_projection() -> None:
    columns = _deployed_named_query_output_columns(
        {
            "definition_sql": "SELECT * FROM modely",
            "output_columns": [{"name": "*", "type": "string"}],
        },
        _snapshot(),
    )

    assert columns == [
        {"name": "Region", "type": "string"},
        {"name": "Sale Date", "type": "date"},
        {"name": "Is Active", "type": "boolean"},
        {"name": "Revenue", "type": "number"},
    ]


def test_bug9180_explicit_projection_keeps_authored_metadata() -> None:
    authored = [
        {"name": "Region", "type": "string"},
        {"name": "Total", "type": "number"},
    ]

    assert _deployed_named_query_output_columns(
        {
            "definition_sql": (
                "SELECT Region, SUM(Revenue) AS Total "
                "FROM modely GROUP BY Region"
            ),
            "output_columns": authored,
        },
        _snapshot(),
    ) == authored
