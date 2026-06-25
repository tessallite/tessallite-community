"""Tests for table-preview connector-aware table qualification (Bug-5470).

The table-preview endpoint builds a ``SELECT * FROM <table>`` statement and
sends it to the query-router /introspect endpoint, which executes it against
the source as-is. Previously the physical name was quoted with PostgreSQL
rules regardless of connector, so BigQuery sources whose physical_name lacked
a dataset prefix produced "Table must be qualified with a dataset".

These offline tests verify:
  * the resolver (`qualify_physical_name`) returns the correct fully-qualified
    dotted name per connector (BigQuery project/dataset, PostgreSQL pass-through);
  * the full preview SQL build path (resolve -> PG-quote -> transpile) emits a
    valid, dataset-qualified BigQuery statement with backtick quoting and a
    still-correct double-quoted PostgreSQL statement.
"""
from unittest.mock import MagicMock

from shared.connector_qualify import quote_table_ref, transpile_preview_sql
from src.api._table_qualify import qualify_physical_name


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_connection(conn_type, config=None, creds=None):
    conn = MagicMock()
    conn.connection_type = conn_type
    conn.config = config or {}
    if creds is not None:
        from shared.security.credential_crypto import encrypt_json
        conn.encrypted_credentials = encrypt_json(creds)
    else:
        conn.encrypted_credentials = None
    return conn


def _make_source(config=None, default_schema=None):
    source = MagicMock()
    source.config = config or {}
    source.default_schema = default_schema
    return source


def _build_preview_sql(connector, qualified_name, page_size=50, offset=0):
    """Mirror the SQL build in table_preview.preview_table."""
    pg_quoted = quote_table_ref("postgresql", qualified_name)
    canonical = (
        f"SELECT * FROM {pg_quoted} LIMIT {page_size + 1} OFFSET {offset}"
    )
    return transpile_preview_sql(connector, canonical)


# ---------------------------------------------------------------------------
# resolver: BigQuery
# ---------------------------------------------------------------------------

class TestQualifyBigQuery:
    def test_bare_name_gets_project_and_dataset(self):
        conn = _make_connection(
            "bigquery",
            creds={"project_id": "my-project", "service_account_json": "{}"},
        )
        source = _make_source(config={"schema": "demo_data"})
        assert (
            qualify_physical_name("store_sales", conn, source)
            == "my-project.demo_data.store_sales"
        )

    def test_dataset_prefixed_name_gets_project(self):
        conn = _make_connection(
            "bigquery",
            creds={"project_id": "my-project", "service_account_json": "{}"},
        )
        source = _make_source()
        assert (
            qualify_physical_name("demo_data.store_sales", conn, source)
            == "my-project.demo_data.store_sales"
        )

    def test_already_fully_qualified_passes_through(self):
        conn = _make_connection(
            "bigquery",
            creds={"project_id": "my-project", "service_account_json": "{}"},
        )
        source = _make_source()
        assert (
            qualify_physical_name("proj.ds.store_sales", conn, source)
            == "proj.ds.store_sales"
        )

    def test_dataset_only_when_project_unknown(self):
        conn = _make_connection("bigquery")
        source = _make_source(config={"dataset": "analytics"})
        assert (
            qualify_physical_name("store_sales", conn, source)
            == "analytics.store_sales"
        )

    def test_schema_already_contains_project_no_duplication(self):
        conn = _make_connection(
            "bigquery",
            creds={"project_id": "tessallite-io", "service_account_json": "{}"},
        )
        source = _make_source(config={"schema": "tessallite-io.demo_data"})
        assert (
            qualify_physical_name("store_sales", conn, source)
            == "tessallite-io.demo_data.store_sales"
        )


# ---------------------------------------------------------------------------
# resolver: PostgreSQL (preserve existing behaviour)
# ---------------------------------------------------------------------------

class TestQualifyPostgreSQL:
    def test_dotted_name_passes_through_unchanged(self):
        conn = _make_connection("postgresql")
        source = _make_source(default_schema="public")
        # Existing behaviour: an already-dotted physical_name is used verbatim.
        assert (
            qualify_physical_name("public.orders", conn, source)
            == "public.orders"
        )

    def test_bare_name_no_schema_stays_bare(self):
        conn = _make_connection("postgresql")
        source = _make_source()
        assert qualify_physical_name("orders", conn, source) == "orders"

    def test_bare_name_uses_default_schema(self):
        conn = _make_connection("postgresql")
        source = _make_source(default_schema="analytics")
        assert (
            qualify_physical_name("orders", conn, source)
            == "analytics.orders"
        )


# ---------------------------------------------------------------------------
# full SQL build path
# ---------------------------------------------------------------------------

class TestPreviewSqlBuild:
    def test_bigquery_emits_dataset_qualified_backticks(self):
        conn = _make_connection(
            "bigquery",
            creds={"project_id": "my-project", "service_account_json": "{}"},
        )
        source = _make_source(config={"schema": "demo_data"})
        qualified = qualify_physical_name("store_sales", conn, source)
        sql = _build_preview_sql("bigquery", qualified)
        # Dataset must be present (the Bug-5470 failure) and backtick-quoted.
        assert "`my-project`.`demo_data`.`store_sales`" in sql
        assert '"' not in sql  # no leftover PostgreSQL quoting
        assert "LIMIT 51 OFFSET 0" in sql

    def test_bigquery_bare_name_no_longer_unqualified(self):
        """Regression for the live failure: bare physical_name + dataset."""
        conn = _make_connection(
            "bigquery",
            creds={"project_id": "p", "service_account_json": "{}"},
        )
        source = _make_source(config={"schema": "ds"})
        qualified = qualify_physical_name("store_sales", conn, source)
        sql = _build_preview_sql("bigquery", qualified)
        # The table reference carries a dataset segment, so BigQuery will not
        # reject it with "Table must be qualified with a dataset".
        assert "`ds`.`store_sales`" in sql

    def test_postgresql_emits_double_quoted_unchanged(self):
        conn = _make_connection("postgresql")
        source = _make_source()
        qualified = qualify_physical_name("public.orders", conn, source)
        sql = _build_preview_sql("postgresql", qualified)
        assert sql == 'SELECT * FROM "public"."orders" LIMIT 51 OFFSET 0'

    def test_postgresql_bare_name_unchanged(self):
        conn = _make_connection("postgresql")
        source = _make_source()
        qualified = qualify_physical_name("orders", conn, source)
        sql = _build_preview_sql("postgresql", qualified)
        assert sql == 'SELECT * FROM "orders" LIMIT 51 OFFSET 0'


# ---------------------------------------------------------------------------
# Bug-5480: cross-connector qualify parity (proves calendar.py consolidation)
# ---------------------------------------------------------------------------

class TestCrossConnectorQualifyParity:
    """Verify qualify_physical_name produces correct results for every
    connector type, especially BigQuery backticks vs double-quotes for
    non-BQ connectors. This guards the Bug-5480 consolidation: calendar.py
    now delegates to _table_qualify.qualify_physical_name instead of carrying
    its own duplicate qualifier."""

    def test_redshift_dotted_passes_through(self):
        conn = _make_connection("redshift")
        source = _make_source(default_schema="analytics")
        assert qualify_physical_name("analytics.orders", conn, source) == "analytics.orders"

    def test_redshift_bare_name_uses_schema(self):
        conn = _make_connection("redshift")
        source = _make_source(config={"schema": "analytics"})
        assert qualify_physical_name("orders", conn, source) == "analytics.orders"

    def test_snowflake_dotted_passes_through(self):
        conn = _make_connection("snowflake")
        source = _make_source(default_schema="PUBLIC")
        assert qualify_physical_name("PUBLIC.orders", conn, source) == "PUBLIC.orders"

    def test_snowflake_bare_name_uses_schema(self):
        conn = _make_connection("snowflake")
        source = _make_source(config={"schema": "ANALYTICS"})
        assert qualify_physical_name("orders", conn, source) == "ANALYTICS.orders"

    def test_sqlserver_dotted_passes_through(self):
        conn = _make_connection("sqlserver")
        source = _make_source(default_schema="dbo")
        assert qualify_physical_name("dbo.orders", conn, source) == "dbo.orders"

    def test_sqlserver_bare_name_uses_schema(self):
        conn = _make_connection("sqlserver")
        source = _make_source(config={"schema": "dbo"})
        assert qualify_physical_name("orders", conn, source) == "dbo.orders"

    def test_hadoop_spark_dotted_passes_through(self):
        conn = _make_connection("hadoop_spark")
        source = _make_source(default_schema="default")
        assert qualify_physical_name("default.orders", conn, source) == "default.orders"

    def test_hadoop_spark_bare_name_uses_schema(self):
        conn = _make_connection("hadoop_spark")
        source = _make_source(config={"schema": "warehouse"})
        assert qualify_physical_name("orders", conn, source) == "warehouse.orders"

    def test_bigquery_backticks_not_double_quotes(self):
        """BigQuery must produce backtick-quoted SQL, not double-quoted."""
        conn = _make_connection(
            "bigquery",
            creds={"project_id": "proj", "service_account_json": "{}"},
        )
        source = _make_source(config={"dataset": "ds"})
        qualified = qualify_physical_name("orders", conn, source)
        assert qualified == "proj.ds.orders"
        sql = _build_preview_sql("bigquery", qualified)
        assert "`proj`.`ds`.`orders`" in sql
        assert '"' not in sql

    def test_postgresql_double_quotes_not_backticks(self):
        """PostgreSQL must produce double-quoted SQL, not backtick-quoted."""
        conn = _make_connection("postgresql")
        source = _make_source(config={"schema": "public"})
        qualified = qualify_physical_name("orders", conn, source)
        assert qualified == "public.orders"
        sql = _build_preview_sql("postgresql", qualified)
        assert '"public"."orders"' in sql
        assert '`' not in sql
