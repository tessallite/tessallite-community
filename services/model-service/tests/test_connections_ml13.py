"""ML13 (unit 014) business-outcome tests for the connections layer.

Covers the behaviour changes made in batch ML13:

- F-014-09: large unnamed high-cardinality dimensions are classified
  ``dim_detail`` instead of being mislabelled ``fact`` on row count alone.
- F-014-10: ``ConnectionUpdate.connection_type`` is validated; a connector
  type change replaces credentials rather than merging stale keys.
- F-014-17: an explicit clear sentinel removes a stored credential field, while
  a blank value still means "keep stored value".
- F-014-14: the profile request body is a typed model rejecting malformed
  entries with a validation error rather than a bare KeyError.

Pure-function / schema tests: no FastAPI app, no DB.
"""
from __future__ import annotations

import pytest

from src.api.connections import (
    CREDENTIAL_CLEAR_SENTINEL,
    CREDENTIAL_EMPTY_SENTINEL,
    _classify_table,
    _merge_credentials,
)


def _col(name, dtype, distinct=None):
    return {
        "column_name": name,
        "data_type": dtype,
        "is_nullable": True,
        "approx_distinct": distinct,
    }


# ---------------------------------------------------------------------------
# F-014-09: dim_detail reachable for a large, high-cardinality, unnamed dim
# ---------------------------------------------------------------------------

def test_large_unnamed_text_dimension_is_dim_detail_not_fact():
    """The review's trace T4: a 50k-row `customers` table, all-text columns,
    name/email/street near-unique. Previously scored +1.8 -> fact. It must now
    be recognised as dim_detail because the positive score was driven only by
    row count, with no name/composition fact evidence."""
    columns = [
        _col("customer_id", "bigint", 50_000),  # FK (excluded from card avg)
        _col("name", "varchar", 48_000),
        _col("email", "varchar", 48_000),
        _col("street", "varchar", 48_000),
        _col("city", "varchar", 900),
        _col("segment", "varchar", 5),
    ]
    assert _classify_table("customers", columns, 50_000) == "dim_detail"


def test_named_fact_table_stays_fact_even_when_large():
    """A table whose *name* carries a fact hint must remain fact — the
    dim_detail re-check only fires when name and composition are both <= 0."""
    columns = [
        _col("order_id", "bigint", 50_000),
        _col("customer_id", "bigint", 5_000),
        _col("order_date", "timestamp", 365),
        _col("amount", "numeric", 45_000),
        _col("status", "varchar", 5),
    ]
    assert _classify_table("orders", columns, 50_000) == "fact"


def test_numeric_heavy_large_table_stays_fact():
    """Composition evidence (numeric ratio, dates) keeps a genuine fact table
    classified as fact even without a name hint."""
    columns = [
        _col("metric_a", "numeric", 9_000),
        _col("metric_b", "numeric", 9_000),
        _col("metric_c", "numeric", 9_000),
        _col("event_ts", "timestamp", 8_000),
        _col("created_ts", "timestamp", 8_000),
    ]
    assert _classify_table("telemetry_stream", columns, 200_000) == "fact"


def test_low_cardinality_large_dimension_is_dim_aggregate():
    """Large but low-cardinality (most values repeat) -> dim_aggregate, not
    dim_detail (the avg cardinality ratio stays low)."""
    columns = [
        _col("region", "varchar", 8),
        _col("country", "varchar", 50),
        _col("segment", "varchar", 5),
    ]
    assert _classify_table("geo_lookup", columns, 20_000) == "dim_aggregate"


# ---------------------------------------------------------------------------
# F-014-17 / F-014-10: credential merge semantics
# ---------------------------------------------------------------------------

def test_blank_value_keeps_stored_credential():
    existing = {"host": "db", "password": "secret"}
    merged = _merge_credentials(existing, {"password": ""})
    assert merged["password"] == "secret"
    assert merged["host"] == "db"


def test_none_value_keeps_stored_credential():
    existing = {"host": "db", "password": "secret"}
    merged = _merge_credentials(existing, {"password": None})
    assert merged["password"] == "secret"


def test_new_value_overwrites_stored_credential():
    existing = {"host": "db", "password": "old"}
    merged = _merge_credentials(existing, {"password": "new"})
    assert merged["password"] == "new"


def test_clear_sentinel_removes_stored_credential():
    """F-014-17: there was previously no way to wipe a stored secret."""
    existing = {"host": "db", "password": "secret"}
    merged = _merge_credentials(existing, {"password": CREDENTIAL_CLEAR_SENTINEL})
    assert "password" not in merged
    assert merged["host"] == "db"


def test_clear_sentinel_on_absent_key_is_noop():
    merged = _merge_credentials({"host": "db"}, {"password": CREDENTIAL_CLEAR_SENTINEL})
    assert merged == {"host": "db"}


# ---------------------------------------------------------------------------
# Bug-7162: an explicitly EMPTY credential coordinate must be storable
# ---------------------------------------------------------------------------

def test_empty_sentinel_sets_stored_credential_to_empty_string():
    """Bug-7162: ``""`` means "keep stored", so blanking a non-secret
    coordinate (e.g. dropping an explicit Snowflake role so the account default
    applies) was unreachable. The sentinel makes it explicit."""
    existing = {"host": "db", "role": "ANALYST"}
    merged = _merge_credentials(existing, {"role": CREDENTIAL_EMPTY_SENTINEL})
    assert merged["role"] == ""
    assert merged["host"] == "db"


def test_empty_sentinel_differs_from_clear_sentinel():
    """Bug-7162: "present but empty" and "absent" are distinct states; the two
    sentinels must not collapse into each other."""
    existing = {"schema": "public"}
    emptied = _merge_credentials(existing, {"schema": CREDENTIAL_EMPTY_SENTINEL})
    cleared = _merge_credentials(existing, {"schema": CREDENTIAL_CLEAR_SENTINEL})
    assert emptied == {"schema": ""}
    assert cleared == {}


def test_empty_sentinel_on_absent_key_adds_empty_value():
    merged = _merge_credentials({"host": "db"}, {"schema": CREDENTIAL_EMPTY_SENTINEL})
    assert merged == {"host": "db", "schema": ""}


def test_blank_string_still_means_keep_stored_value():
    """Bug-7162 must not change the default: an omitted/blank field in the edit
    dialog still means "unchanged", so users need not retype the password."""
    merged = _merge_credentials({"password": "secret"}, {"password": ""})
    assert merged["password"] == "secret"


# ---------------------------------------------------------------------------
# F-014-10: ConnectionUpdate validation
# ---------------------------------------------------------------------------

def test_connection_update_rejects_jdbc():
    from shared.schemas.pydantic_models import ConnectionUpdate
    with pytest.raises(ValueError):
        ConnectionUpdate(connection_type="jdbc")


def test_connection_update_rejects_unknown_type():
    from shared.schemas.pydantic_models import ConnectionUpdate
    with pytest.raises(ValueError):
        ConnectionUpdate(connection_type="oracle")


def test_connection_update_accepts_known_type():
    from shared.schemas.pydantic_models import ConnectionUpdate
    assert ConnectionUpdate(connection_type="snowflake").connection_type == "snowflake"


def test_connection_update_allows_none_connection_type():
    """A PATCH that does not touch connection_type must validate."""
    from shared.schemas.pydantic_models import ConnectionUpdate
    body = ConnectionUpdate(display_name="renamed")
    assert body.connection_type is None


# ---------------------------------------------------------------------------
# F-014-14: typed profile request body
# ---------------------------------------------------------------------------

def test_profile_request_rejects_entry_without_table():
    from shared.schemas.pydantic_models import ProfileTablesRequest
    with pytest.raises(ValueError):
        ProfileTablesRequest(tables=[{"schema": "public"}])


def test_profile_request_defaults_schema_to_public():
    from shared.schemas.pydantic_models import ProfileTablesRequest
    req = ProfileTablesRequest(tables=[{"table": "orders"}])
    assert req.tables[0].schema_ == "public"
    assert req.tables[0].table == "orders"


def test_profile_request_accepts_schema_alias():
    from shared.schemas.pydantic_models import ProfileTablesRequest
    req = ProfileTablesRequest(tables=[{"schema": "sales", "table": "orders"}])
    assert req.tables[0].schema_ == "sales"
