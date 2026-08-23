"""Bug-9179 — star Named Query CTAS aliases with spaces are valid on BigQuery.

R2-PCR-004 / VERIFY_FIRST closeout. Star expansion and explicit projections
emit semantic column names (including spaces) as PostgreSQL-canonical quoted
identifiers; Named Query refresh wraps the router-rewritten SELECT in
``CREATE OR REPLACE TABLE … AS …``. The production choke point
``_transpile_to_dialect`` must turn those aliases into GoogleSQL backtick
identifiers before the CTAS reaches BigQuery.

Probe (documented for re-run):
  CREATE OR REPLACE TABLE `<project>.<dataset>.tmp_bug9179_nq_shape` AS
  SELECT <phys> AS `Country Code`, <phys> AS `Transaction Amount`
  FROM <source> LIMIT 5

When BigQuery credentials are present, the live dry-run (and optional execute)
fails closed — skip is allowed only when no credentials exist.
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from shared.connector_qualify import quote_table_ref, safe_ident
from shared.named_query.star_expansion import expand_named_query_star_definition
from src.rewrite.dialects import _transpile_to_dialect

_SEMANTIC_SPACE_NAMES = ("Country Code", "Transaction Amount")
_DEFAULT_PROJECT = "tessallite-io"
_DEFAULT_DATASET = "demo_data"
_DEFAULT_SOURCE = "payment_transaction"
_PHYS_COLS = ("country_code", "transaction_amount")


def _adc_paths() -> list[Path]:
    paths: list[Path] = []
    for key in ("GOOGLE_APPLICATION_CREDENTIALS", "GCLOUD_ADC_PATH", "BQ_SERVICE_ACCOUNT_JSON"):
        raw = (os.environ.get(key) or "").strip()
        if raw:
            paths.append(Path(raw).expanduser())
    paths.append(Path.home() / ".config/gcloud/application_default_credentials.json")
    return paths


def _bq_credentials_present() -> bool:
    return any(p.is_file() for p in _adc_paths())


def _bq_project() -> str:
    return (
        os.environ.get("BUG9179_BQ_PROJECT")
        or os.environ.get("GOOGLE_CLOUD_PROJECT")
        or os.environ.get("GCLOUD_PROJECT")
        or _DEFAULT_PROJECT
    ).strip()


def _bq_dataset() -> str:
    return (os.environ.get("BUG9179_BQ_DATASET") or _DEFAULT_DATASET).strip()


def _bq_source_table() -> str:
    return (os.environ.get("BUG9179_BQ_SOURCE_TABLE") or _DEFAULT_SOURCE).strip()


def _pg_canonical_select(*, qualify_source: bool = False) -> str:
    """Mirror source_sql's ``safe_ident`` aliases + star semantic names."""
    aliases = ", ".join(
        f'{safe_ident(phys)} AS {safe_ident(name)}'
        for phys, name in zip(_PHYS_COLS, _SEMANTIC_SPACE_NAMES, strict=True)
    )
    dataset = _bq_dataset()
    table = _bq_source_table()
    if qualify_source:
        project = _bq_project()
        from_clause = f"{safe_ident(project)}.{safe_ident(dataset)}.{safe_ident(table)}"
    else:
        from_clause = f"{safe_ident(dataset)}.{safe_ident(table)}"
    return f"SELECT {aliases} FROM {from_clause} LIMIT 5"


def _production_bq_select() -> str:
    """Named Query refresh receives router output already in target dialect."""
    return _transpile_to_dialect(_pg_canonical_select(), "bigquery")


def _nq_refresh_ctas_sql(select_sql: str, *, table_name: str) -> str:
    dotted = f"{_bq_project()}.{_bq_dataset()}.{table_name}"
    return f"CREATE OR REPLACE TABLE {quote_table_ref('bigquery', dotted)} AS {select_sql}"


def test_bug9179_star_expansion_emits_quoted_semantic_names_with_spaces():
    snapshot = {
        "hidden_columns": [],
        "dimensions": [
            {"name": name, "source_column_id": f"c{i}", "table_id": "t1"}
            for i, name in enumerate(_SEMANTIC_SPACE_NAMES, start=1)
        ],
        "measures": [],
        "tables": [],
    }
    expanded = expand_named_query_star_definition("SELECT * FROM modell", snapshot)
    for name in _SEMANTIC_SPACE_NAMES:
        assert safe_ident(name) in expanded, expanded


def test_bug9179_transpile_boundary_emits_backtick_space_aliases():
    """Production choke point must not leave PG double-quotes or bare spaces."""
    bq_sql = _production_bq_select()
    for name in _SEMANTIC_SPACE_NAMES:
        assert f"`{name}`" in bq_sql, bq_sql
        assert f'"{name}"' not in bq_sql, bq_sql
        # Unquoted multi-word alias is invalid GoogleSQL.
        assert f" AS {name}" not in bq_sql, bq_sql


def test_bug9179_nq_refresh_ctas_wrapper_preserves_backtick_aliases():
    select_sql = _production_bq_select()
    ctas = _nq_refresh_ctas_sql(select_sql, table_name="tmp_bug9179_shape")
    assert ctas.startswith("CREATE OR REPLACE TABLE `")
    assert " AS " in ctas
    for name in _SEMANTIC_SPACE_NAMES:
        assert f"`{name}`" in ctas, ctas


@pytest.mark.skipif(
    not _bq_credentials_present(),
    reason=(
        "Bug-9179 live BigQuery CTAS: no ADC / BQ_SERVICE_ACCOUNT_JSON; "
        "emission+transpile contracts above still gate the quoting path"
    ),
)
def test_bug9179_live_bigquery_ctas_accepts_semantic_space_aliases():
    """Fail-closed live probe when credentials exist (R2-PCR-004).

    Dry-run always. Optional real CREATE+SELECT+DROP when BUG9179_BQ_LIVE=1.
    """
    from google.cloud import bigquery

    project = _bq_project()
    dataset = _bq_dataset()
    client = bigquery.Client(project=project)

    # Ensure the destination dataset is reachable before claiming success.
    client.get_dataset(f"{project}.{dataset}")

    # Qualify the source for a real project.dataset.table reference after
    # transpile (router emits dataset.table; live probe needs the project).
    select_sql = _transpile_to_dialect(_pg_canonical_select(), "bigquery")
    select_sql = select_sql.replace(
        f"`{dataset}`.`{_bq_source_table()}`",
        f"`{project}`.`{dataset}`.`{_bq_source_table()}`",
    )
    table_name = f"tmp_bug9179_{uuid.uuid4().hex[:10]}"
    ctas = _nq_refresh_ctas_sql(select_sql, table_name=table_name)

    dry_cfg = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False)
    dry_job = client.query(ctas, job_config=dry_cfg)
    assert dry_job.errors is None, dry_job.errors

    if os.environ.get("BUG9179_BQ_LIVE", "").strip() not in {"1", "true", "TRUE", "yes"}:
        return

    table_id = f"{project}.{dataset}.{table_name}"
    try:
        client.query(ctas).result()
        rows = list(
            client.query(
                f"SELECT `{_SEMANTIC_SPACE_NAMES[0]}`, "
                f"`{_SEMANTIC_SPACE_NAMES[1]}` FROM `{table_id}` LIMIT 1"
            ).result()
        )
        assert rows, "CTAS succeeded but returned no rows"
        schema_names = {field.name for field in client.get_table(table_id).schema}
        assert set(_SEMANTIC_SPACE_NAMES) <= schema_names
    finally:
        client.delete_table(table_id, not_found_ok=True)
