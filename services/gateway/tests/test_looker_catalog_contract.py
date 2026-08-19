"""License-free PostgreSQL catalogue contract required by Looker products."""
from __future__ import annotations

from src.jdbc.catalogue import CatalogueDB
from src.jdbc import protocol


NAMES = ["modelx__payment_transaction", "modelx__dim_account_type"]
COLUMNS = {
    NAMES[0]: [
        {"name": "payment_id", "data_type": "bigint", "is_nullable": False, "is_primary_key": True},
        {"name": "amount", "data_type": "numeric(18, 2)", "is_primary_key": False},
        {"name": "ratio", "data_type": "float8", "is_primary_key": False},
        {"name": "memo", "data_type": "text", "is_primary_key": False},
    ],
    NAMES[1]: [{"name": "account_type_code", "data_type": "varchar", "is_primary_key": True}],
}


def _make_catalogue(**kwargs):
    return CatalogueDB(
        model_names=NAMES,
        table_columns=COLUMNS,
        **kwargs,
    )


def _row_as_dict(columns: list[tuple[str, int]], row: list) -> dict[str, object]:
    return {column[0]: value for column, value in zip(columns, row, strict=True)}


def test_pg_proc_lists_supported_aggregate_functions() -> None:
    cat = _make_catalogue()
    result = cat.execute("SELECT * FROM pg_catalog.pg_proc")
    assert result is not None
    columns, rows = result
    by_name = {
        row["proname"]: row
        for row in (_row_as_dict(columns, values) for values in rows)
    }
    aggregates = {"sum", "avg", "min", "max", "count", "count_distinct"}
    assert aggregates <= set(by_name)
    # Beyond the aggregates, pg_proc also carries per-type *recv functions
    # (Bug-5552/5553: Npgsql resolves typreceive by joining pg_proc) — but
    # nothing else.
    extras = set(by_name) - aggregates
    assert extras and all(name.endswith("recv") for name in extras)
    assert all(
        row["pronargs"] == "1" and row["proisagg"] == "t"
        for name, row in by_name.items()
        if name in aggregates
    )
    assert by_name["sum"]["prorettype"] == "1700"
    assert by_name["count"]["prorettype"] == "20"
    cat.close()


def test_pg_type_exposes_categories_elements_and_relation_ids() -> None:
    cat = _make_catalogue()
    result = cat.execute("SELECT * FROM pg_catalog.pg_type")
    assert result is not None
    columns, rows = result
    by_name = {
        row["typname"]: row
        for row in (_row_as_dict(columns, values) for values in rows)
    }
    assert by_name["numeric"]["typcategory"] == "N"
    assert by_name["text"]["typcategory"] == "S"
    assert by_name["date"]["typcategory"] == "D"
    assert by_name["bool"]["typcategory"] == "B"
    assert by_name["numeric"]["typelem"] == "0"
    assert by_name["numeric"]["typrelid"] == "0"
    assert by_name["numeric"]["typnotnull"] == "f"
    cat.close()


def test_information_schema_numeric_precision_is_populated() -> None:
    cat = _make_catalogue()
    result = cat.execute(
        "SELECT * FROM information_schema.columns "
        "WHERE table_name = 'modelx__payment_transaction'"
    )
    assert result is not None
    columns, rows = result
    by_column = {
        row["column_name"]: row
        for row in (_row_as_dict(columns, values) for values in rows)
    }
    assert by_column["payment_id"]["numeric_precision"] == "64"
    assert by_column["payment_id"]["numeric_scale"] == "0"
    assert by_column["payment_id"]["is_nullable"] == "NO"
    assert by_column["amount"]["data_type"] == "numeric"
    assert by_column["amount"]["numeric_precision"] == "18"
    assert by_column["amount"]["numeric_scale"] == "2"
    assert by_column["ratio"]["numeric_precision"] == "53"
    assert by_column["memo"]["numeric_precision"] is None
    cat.close()


def test_pg_class_reports_key_index_and_row_estimate() -> None:
    cat = CatalogueDB(
        model_names=NAMES,
        table_columns=COLUMNS,
        table_row_estimates={NAMES[0]: 2500},
    )
    result = cat.execute("SELECT * FROM pg_class")
    assert result is not None
    columns, rows = result
    by_table = {
        row["relname"]: row
        for row in (_row_as_dict(columns, values) for values in rows)
    }
    assert by_table[NAMES[0]]["relhasindex"] == "t"
    assert by_table[NAMES[0]]["reltuples"] == "2500.0"  # SQLite returns float as string
    assert by_table[NAMES[0]]["relhasrules"] == "f"
    assert by_table[NAMES[0]]["relhastriggers"] == "f"
    cat.close()


def test_pg_settings_reports_looker_required_values() -> None:
    cat = _make_catalogue()
    result = cat.execute("SELECT * FROM pg_settings")
    assert result is not None
    columns, rows = result
    settings = {
        row["name"]: row["setting"]
        for row in (_row_as_dict(columns, values) for values in rows)
    }
    assert settings["max_identifier_length"] == "63"
    assert settings["standard_conforming_strings"] == "on"
    cat.close()


def test_startup_status_reports_server_version_number() -> None:
    startup = protocol.startup_sequence(pid=9)
    assert b"server_version_num\x00150000\x00" in startup
