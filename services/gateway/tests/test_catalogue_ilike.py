"""Bug-5183 — the JDBC catalogue (information_schema) query path must accept
PostgreSQL ``ILIKE``.

The catalogue answers system-catalogue / information_schema queries from an
embedded SQLite database. SQLite has no ``ILIKE`` keyword, so a BI client that
introspects the catalogue with ``... WHERE column_name ILIKE '%pay%'`` (e2e
case S3.052) failed with ``near "ILIKE": syntax error``. The transform layer
now rewrites ``ILIKE`` → ``LIKE`` (SQLite's LIKE is case-insensitive for ASCII,
which covers every catalogue identifier), so the introspection query runs.
"""
from src.jdbc.catalogue import _transform_sql, CatalogueDB


def test_transform_rewrites_ilike_to_like():
    sql = "SELECT column_name FROM information_schema.columns WHERE column_name ILIKE '%pay%'"
    out = _transform_sql(sql)
    assert "ILIKE" not in out.upper()
    assert " LIKE " in out.upper()
    # The information_schema dotted name is still mapped to its SQLite table.
    assert "information_schema_columns" in out


def test_transform_rewrites_ilike_case_insensitively():
    # Lowercase / mixed-case spellings are all rewritten.
    for spelling in ("ilike", "IlIkE", "ILIKE"):
        out = _transform_sql(f"SELECT 1 WHERE x {spelling} 'y'")
        assert "LIKE" in out.upper()
        assert out.upper().count("ILIKE") == 0


def test_catalogue_execute_accepts_ilike_end_to_end():
    """End-to-end: the embedded catalogue executes an ILIKE introspection
    query and returns the matching rows case-insensitively."""
    cat = CatalogueDB(
        model_names=["payment_transaction", "customer"],
        table_columns={
            "payment_transaction": [
                {"name": "business_date", "type": "date"},
                {"name": "transaction_amount", "type": "numeric"},
            ],
            "customer": [{"name": "customer_id", "type": "integer"}],
        },
    )

    # Upper-case pattern must still match the lower-case stored table name.
    cols, rows = cat.execute(
        "SELECT table_name FROM information_schema.columns "
        "WHERE table_name ILIKE 'PAYMENT%'"
    )
    names = {r[0] for r in rows}
    assert "payment_transaction" in names
    assert "customer" not in names
