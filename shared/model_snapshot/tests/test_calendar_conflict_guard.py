import pytest

from shared.model_snapshot.rehydrator import (
    SnapshotSchemaError,
    _guard_conflicting_calendar_tables,
)


def test_rejects_conflicting_calendar_maps_for_same_bigquery_table():
    snapshot = {
        "calendar_tables": [
            {
                "data_source_id": "source-1",
                "table_name": "inventory_analytics.dim_date",
                "calendar_type": "standard",
                "date_column": "full_date",
                "year_column": "year",
            },
            {
                "data_source_id": "source-1",
                "table_name": "tessallite-io.inventory_analytics.dim_date",
                "calendar_type": "standard",
                "date_column": "date_key",
                "year_column": "year_no",
            },
        ]
    }

    with pytest.raises(SnapshotSchemaError, match="conflicting calendar mappings"):
        _guard_conflicting_calendar_tables(snapshot)


def test_allows_project_qualified_duplicate_with_same_mapping():
    snapshot = {
        "calendar_tables": [
            {
                "data_source_id": "source-1",
                "table_name": "inventory_analytics.dim_date",
                "calendar_type": "standard",
                "date_column": "full_date",
                "year_column": "year",
            },
            {
                "data_source_id": "source-1",
                "table_name": "tessallite-io.inventory_analytics.dim_date",
                "calendar_type": "standard",
                "date_column": "full_date",
                "year_column": "year",
            },
        ]
    }

    _guard_conflicting_calendar_tables(snapshot)
