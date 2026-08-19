"""Bug-7043: JDBC catalogue metadata must not be STALE after CLS reconfiguration.

After a persona's column-level security restrictions change, an already-open
JDBC connection must reflect the updated restrictions in its catalogue (the
in-memory SQLite database that answers information_schema / pg_catalog queries).

This test verifies that:
  1. A catalogue built with CLS restrictions omits the restricted columns.
  2. When CLS restrictions change (column becomes restricted mid-connection),
     the catalogue refresh mechanism picks up the change and the restricted
     column disappears from the catalogue.
  3. Conversely, when a restriction is lifted, the column reappears.
  4. The default TTL of 0 causes a refresh on every catalogue query (fail-closed).
  5. A non-zero TTL delays the refresh until the TTL expires.
"""
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src import router_client
from src.jdbc.catalogue import CatalogueDB


MODEL_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
PROJECT_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"

# Source columns (pre-CLS)
SOURCE_COLUMN_ID_REGION = "sc-region"
SOURCE_COLUMN_ID_PRODUCT = "sc-product"
SOURCE_COLUMN_ID_SECRET = "sc-secret"

DIMENSIONS = [
    {
        "id": "d1", "name": "Region", "data_type": "text",
        "source_column_id": SOURCE_COLUMN_ID_REGION,
    },
    {
        "id": "d2", "name": "Product", "data_type": "text",
        "source_column_id": SOURCE_COLUMN_ID_PRODUCT,
    },
    {
        "id": "d3", "name": "SecretDim", "data_type": "text",
        "source_column_id": SOURCE_COLUMN_ID_SECRET,
    },
]

MEASURES = [
    {"id": "m1", "name": "Revenue", "default_agg": "sum", "source_column_id": "sc-rev"},
]


def _make_persona(*, restricted_column_ids=None):
    """Build a persona dict with optional CLS-restricted source column IDs."""
    return {
        "id": "p1",
        "slug": "eu_analyst",
        "includes_hidden_columns": False,
        "included_measure_ids": [],
        "included_dimension_ids": [],
        "restricted_column_ids": restricted_column_ids or [],
    }


def _patch_clients(monkeypatch, *, persona):
    """Monkeypatch HTTP client calls for fetch_model_metadata."""
    model = {
        "id": MODEL_ID,
        "project_id": PROJECT_ID,
        "project_slug": "public",
        "slug": "sales",
        "deployed_version_id": None,
    }

    async def _list_models(tenant_slug, jwt_token):
        return [model]

    async def _measures(mid, tenant_slug, jwt_token, project_id=""):
        return MEASURES

    async def _dimensions(mid, tenant_slug, jwt_token, project_id=""):
        return DIMENSIONS

    async def _personas(mid, tenant_slug, jwt_token, project_id=""):
        return [persona]

    async def _snapshot(mid, tenant_slug, jwt_token, project_id=""):
        return {"columns": [], "tables": []}

    async def _kpis(mid, tenant_slug, jwt_token, project_id=""):
        return []

    monkeypatch.setattr(router_client, "list_all_models_for_tenant", _list_models)
    monkeypatch.setattr(router_client, "get_model_measures", _measures)
    monkeypatch.setattr(router_client, "get_model_dimensions", _dimensions)
    monkeypatch.setattr(router_client, "get_model_personas", _personas)
    monkeypatch.setattr(router_client, "get_model_snapshot", _snapshot)
    monkeypatch.setattr(router_client, "get_model_kpis", _kpis)


def _catalogue_column_names(table_columns: dict, relation: str) -> set[str]:
    """Extract column names from a table_columns dict for a given relation."""
    return {col["name"] for col in table_columns.get(relation, [])}


def _catalogue_column_names_from_db(
    cat: CatalogueDB, relation: str = "sales_eu_analyst",
) -> set[str]:
    """Query the CatalogueDB for column names in a specific relation."""
    result = cat.execute(
        "SELECT column_name FROM information_schema.columns "
        f"WHERE table_name = '{relation}'"
    )
    if result is None:
        return set()
    _cols, rows = result
    return {row[0] for row in rows if row[0] is not None}


# ---------------------------------------------------------------------------
# Test 1: CLS restriction hides column from catalogue
# ---------------------------------------------------------------------------
async def test_cls_restriction_hides_column_from_catalogue(monkeypatch):
    """When a persona has restricted_column_ids, those columns must not
    appear in the catalogue metadata (neither in fetch_model_metadata's
    column dict nor in the CatalogueDB's information_schema).
    """
    persona = _make_persona(restricted_column_ids=[SOURCE_COLUMN_ID_SECRET])
    _patch_clients(monkeypatch, persona=persona)

    result = await router_client.fetch_model_metadata(MODEL_ID, "acme", "jwt")
    table_columns = result[1]

    # The persona relation is "sales_eu_analyst"
    persona_cols = _catalogue_column_names(table_columns, "sales_eu_analyst")
    assert "Region" in persona_cols, "unrestricted column missing"
    assert "Product" in persona_cols, "unrestricted column missing"
    assert "SecretDim" not in persona_cols, (
        "CLS-restricted column still visible in persona catalogue"
    )

    # Build a CatalogueDB and verify the restricted column is absent from SQL queries
    cat = CatalogueDB(
        model_names=list(table_columns.keys()),
        table_columns=table_columns,
        tenant_slug="acme",
    )
    try:
        sql_cols = _catalogue_column_names_from_db(cat)
        assert "SecretDim" not in sql_cols, (
            "CLS-restricted column visible in CatalogueDB SQL query"
        )
    finally:
        cat.close()


# ---------------------------------------------------------------------------
# Test 2: CLS tightening mid-connection causes catalogue refresh (TTL=0)
# ---------------------------------------------------------------------------
async def test_cls_tighten_refreshes_catalogue_ttl_zero(monkeypatch):
    """Bug-7043 core test: after CLS restricts a column mid-connection,
    the next catalogue query (with TTL=0) must NOT list that column.

    Simulates the _refresh_catalogue_if_stale flow by calling
    fetch_model_metadata twice with different persona configurations,
    building CatalogueDB instances, and checking the column sets.
    """
    # Phase 1: initial connection, no CLS restriction on SecretDim
    persona_unrestricted = _make_persona(restricted_column_ids=[])
    _patch_clients(monkeypatch, persona=persona_unrestricted)

    result1 = await router_client.fetch_model_metadata(MODEL_ID, "acme", "jwt")
    table_columns1 = result1[1]

    persona_cols1 = _catalogue_column_names(table_columns1, "sales_eu_analyst")
    assert "SecretDim" in persona_cols1, (
        "SecretDim should be visible before CLS restriction"
    )

    cat1 = CatalogueDB(
        model_names=list(table_columns1.keys()),
        table_columns=table_columns1,
        tenant_slug="acme",
    )
    try:
        sql_cols1 = _catalogue_column_names_from_db(cat1)
        assert "SecretDim" in sql_cols1, (
            "SecretDim should be in CatalogueDB before CLS restriction"
        )
    finally:
        cat1.close()

    # Phase 2: CLS tightened — SecretDim now restricted
    persona_restricted = _make_persona(
        restricted_column_ids=[SOURCE_COLUMN_ID_SECRET],
    )
    _patch_clients(monkeypatch, persona=persona_restricted)

    result2 = await router_client.fetch_model_metadata(MODEL_ID, "acme", "jwt")
    table_columns2 = result2[1]

    persona_cols2 = _catalogue_column_names(table_columns2, "sales_eu_analyst")
    assert "SecretDim" not in persona_cols2, (
        "Bug-7043: CLS-restricted column still visible after tightening"
    )

    # Rebuild the catalogue as _refresh_catalogue_if_stale would
    cat2 = CatalogueDB(
        model_names=list(table_columns2.keys()),
        table_columns=table_columns2,
        tenant_slug="acme",
    )
    try:
        sql_cols2 = _catalogue_column_names_from_db(cat2)
        assert "SecretDim" not in sql_cols2, (
            "Bug-7043: CLS-restricted column still in CatalogueDB after rebuild"
        )
    finally:
        cat2.close()


# ---------------------------------------------------------------------------
# Test 3: CLS loosening mid-connection restores column
# ---------------------------------------------------------------------------
async def test_cls_loosen_restores_column(monkeypatch):
    """When a CLS restriction is removed, the column must reappear in the
    catalogue after a refresh.
    """
    # Phase 1: SecretDim restricted
    persona_restricted = _make_persona(
        restricted_column_ids=[SOURCE_COLUMN_ID_SECRET],
    )
    _patch_clients(monkeypatch, persona=persona_restricted)

    result1 = await router_client.fetch_model_metadata(MODEL_ID, "acme", "jwt")
    persona_cols1 = _catalogue_column_names(result1[1], "sales_eu_analyst")
    assert "SecretDim" not in persona_cols1

    # Phase 2: restriction lifted
    persona_unrestricted = _make_persona(restricted_column_ids=[])
    _patch_clients(monkeypatch, persona=persona_unrestricted)

    result2 = await router_client.fetch_model_metadata(MODEL_ID, "acme", "jwt")
    persona_cols2 = _catalogue_column_names(result2[1], "sales_eu_analyst")
    assert "SecretDim" in persona_cols2, (
        "Column should reappear after CLS restriction is lifted"
    )


# ---------------------------------------------------------------------------
# Test 4: CatalogueDB change detection (column set comparison)
# ---------------------------------------------------------------------------
async def test_catalogue_change_detection(monkeypatch):
    """The refresh logic compares model_names and table_columns to decide
    whether a rebuild is needed. Verify that different CLS configurations
    produce different table_columns dicts.
    """
    # Unrestricted
    persona_unrestricted = _make_persona(restricted_column_ids=[])
    _patch_clients(monkeypatch, persona=persona_unrestricted)
    result_unr = await router_client.fetch_model_metadata(MODEL_ID, "acme", "jwt")
    cols_unr = result_unr[1]

    # Restricted
    persona_restricted = _make_persona(
        restricted_column_ids=[SOURCE_COLUMN_ID_SECRET],
    )
    _patch_clients(monkeypatch, persona=persona_restricted)
    result_res = await router_client.fetch_model_metadata(MODEL_ID, "acme", "jwt")
    cols_res = result_res[1]

    # The column dicts must differ so the refresh logic detects the change
    assert cols_unr != cols_res, (
        "table_columns should differ when CLS restrictions change"
    )

    # Specifically, the persona relation column count should differ
    eu_unr = cols_unr.get("sales_eu_analyst", [])
    eu_res = cols_res.get("sales_eu_analyst", [])
    assert len(eu_unr) > len(eu_res), (
        "Restricted persona should have fewer columns"
    )


# ---------------------------------------------------------------------------
# Test 5: _refresh_catalogue_if_stale integration test (handler-level)
# ---------------------------------------------------------------------------
async def test_handler_refresh_catalogue_if_stale(monkeypatch):
    """Integration test: exercise _refresh_catalogue_if_stale on a
    JDBCClientHandler with monkeypatched model metadata that changes
    between calls, verifying the catalogue is rebuilt.
    """
    # Import the handler; we need the class and its dependencies
    from src.jdbc import server as jdbc_server

    # Mock system_snapshot_get for TTL = 0 (re-validate every time)
    monkeypatch.setattr(
        "src.jdbc.server.system_snapshot_get",
        lambda key: {
            "gateway.catalogue_cls_ttl": 0,
        }.get(key, 15),
    )

    handler = jdbc_server.PGWireServer()
    handler._tenant_slug = "acme"
    handler._model_id = MODEL_ID
    handler._jwt_token = "fake-jwt"

    # Phase 1: set up with unrestricted persona
    persona_unrestricted = _make_persona(restricted_column_ids=[])
    _patch_clients(monkeypatch, persona=persona_unrestricted)
    result1 = await router_client.fetch_model_metadata(MODEL_ID, "acme", "jwt")
    handler._model_names = result1[0]
    handler._table_columns = result1[1]
    handler._table_model_id = result1[2]
    handler._table_descriptions = result1[3]
    handler._table_trust_meta = result1[4]
    handler._table_persona_id = result1[5]
    handler._table_include_hidden = result1[6]
    handler._table_query_name = result1[7]
    handler._table_foreign_keys = result1[8]
    handler._table_row_estimates = result1[9]
    handler._looker_relations = result1[10]
    handler._table_project_slug = result1[11]

    handler._catalogue = CatalogueDB(
        model_names=handler._model_names,
        table_columns=handler._table_columns,
        tenant_slug="acme",
    )
    handler._catalogue_built_at = time.monotonic()

    # Verify SecretDim is visible
    cols_before = _catalogue_column_names_from_db(handler._catalogue)
    assert "SecretDim" in cols_before

    # Phase 2: CLS tightened — SecretDim now restricted
    persona_restricted = _make_persona(
        restricted_column_ids=[SOURCE_COLUMN_ID_SECRET],
    )
    _patch_clients(monkeypatch, persona=persona_restricted)

    # With TTL=0, _refresh_catalogue_if_stale should re-fetch and rebuild
    await handler._refresh_catalogue_if_stale()

    # Verify SecretDim is now hidden
    cols_after = _catalogue_column_names_from_db(handler._catalogue)
    assert "SecretDim" not in cols_after, (
        "Bug-7043: CLS-restricted column still visible in catalogue "
        "after _refresh_catalogue_if_stale with TTL=0"
    )
    # Other columns still present
    assert "Region" in cols_after
    assert "Product" in cols_after
    assert "Revenue" in cols_after

    handler._catalogue.close()


# ---------------------------------------------------------------------------
# Test 6: TTL > 0 prevents refresh within window
# ---------------------------------------------------------------------------
async def test_handler_ttl_prevents_premature_refresh(monkeypatch):
    """When TTL > 0, the catalogue should NOT refresh within the TTL window."""
    from src.jdbc import server as jdbc_server

    # TTL = 3600 (1 hour)
    monkeypatch.setattr(
        "src.jdbc.server.system_snapshot_get",
        lambda key: {
            "gateway.catalogue_cls_ttl": 3600,
        }.get(key, 15),
    )

    handler = jdbc_server.PGWireServer()
    handler._tenant_slug = "acme"
    handler._model_id = MODEL_ID
    handler._jwt_token = "fake-jwt"

    persona_unrestricted = _make_persona(restricted_column_ids=[])
    _patch_clients(monkeypatch, persona=persona_unrestricted)
    result1 = await router_client.fetch_model_metadata(MODEL_ID, "acme", "jwt")
    handler._model_names = result1[0]
    handler._table_columns = result1[1]
    handler._table_model_id = result1[2]
    handler._table_descriptions = result1[3]
    handler._table_trust_meta = result1[4]
    handler._table_persona_id = result1[5]
    handler._table_include_hidden = result1[6]
    handler._table_query_name = result1[7]
    handler._table_foreign_keys = result1[8]
    handler._table_row_estimates = result1[9]
    handler._looker_relations = result1[10]
    handler._table_project_slug = result1[11]

    handler._catalogue = CatalogueDB(
        model_names=handler._model_names,
        table_columns=handler._table_columns,
        tenant_slug="acme",
    )
    handler._catalogue_built_at = time.monotonic()

    # Now tighten CLS but keep TTL high
    persona_restricted = _make_persona(
        restricted_column_ids=[SOURCE_COLUMN_ID_SECRET],
    )
    _patch_clients(monkeypatch, persona=persona_restricted)

    # The catalogue was just built, so TTL=3600 should prevent refresh
    await handler._refresh_catalogue_if_stale()

    # SecretDim should STILL be visible (no refresh happened)
    cols = _catalogue_column_names_from_db(handler._catalogue)
    assert "SecretDim" in cols, (
        "TTL not expired — catalogue should not have been refreshed"
    )

    handler._catalogue.close()


# ---------------------------------------------------------------------------
# Test 7: TTL expiry triggers refresh
# ---------------------------------------------------------------------------
async def test_handler_ttl_expiry_triggers_refresh(monkeypatch):
    """When the TTL expires, the catalogue should refresh."""
    from src.jdbc import server as jdbc_server

    monkeypatch.setattr(
        "src.jdbc.server.system_snapshot_get",
        lambda key: {
            "gateway.catalogue_cls_ttl": 1,  # 1 second TTL
        }.get(key, 15),
    )

    handler = jdbc_server.PGWireServer()
    handler._tenant_slug = "acme"
    handler._model_id = MODEL_ID
    handler._jwt_token = "fake-jwt"

    persona_unrestricted = _make_persona(restricted_column_ids=[])
    _patch_clients(monkeypatch, persona=persona_unrestricted)
    result1 = await router_client.fetch_model_metadata(MODEL_ID, "acme", "jwt")
    handler._model_names = result1[0]
    handler._table_columns = result1[1]
    handler._table_model_id = result1[2]
    handler._table_descriptions = result1[3]
    handler._table_trust_meta = result1[4]
    handler._table_persona_id = result1[5]
    handler._table_include_hidden = result1[6]
    handler._table_query_name = result1[7]
    handler._table_foreign_keys = result1[8]
    handler._table_row_estimates = result1[9]
    handler._looker_relations = result1[10]
    handler._table_project_slug = result1[11]

    handler._catalogue = CatalogueDB(
        model_names=handler._model_names,
        table_columns=handler._table_columns,
        tenant_slug="acme",
    )
    # Set catalogue_built_at to 2 seconds ago (past TTL of 1s)
    handler._catalogue_built_at = time.monotonic() - 2.0

    # Now tighten CLS
    persona_restricted = _make_persona(
        restricted_column_ids=[SOURCE_COLUMN_ID_SECRET],
    )
    _patch_clients(monkeypatch, persona=persona_restricted)

    # TTL expired, so refresh should happen
    await handler._refresh_catalogue_if_stale()

    cols = _catalogue_column_names_from_db(handler._catalogue)
    assert "SecretDim" not in cols, (
        "TTL expired — CLS-restricted column should be hidden after refresh"
    )
    assert "Region" in cols
    assert "Product" in cols

    handler._catalogue.close()


# ---------------------------------------------------------------------------
# Test 8: fetch failure is fail-closed (keeps prior restrictions)
# ---------------------------------------------------------------------------
async def test_refresh_failure_is_fail_closed(monkeypatch):
    """When fetch_model_metadata fails during refresh, the existing
    catalogue (with whatever CLS was last applied) should be retained.
    The stale catalogue is already CLS-filtered from its last build,
    so keeping it is fail-closed.
    """
    from src.jdbc import server as jdbc_server

    monkeypatch.setattr(
        "src.jdbc.server.system_snapshot_get",
        lambda key: {
            "gateway.catalogue_cls_ttl": 0,
        }.get(key, 15),
    )

    handler = jdbc_server.PGWireServer()
    handler._tenant_slug = "acme"
    handler._model_id = MODEL_ID
    handler._jwt_token = "fake-jwt"

    # Build with SecretDim restricted
    persona_restricted = _make_persona(
        restricted_column_ids=[SOURCE_COLUMN_ID_SECRET],
    )
    _patch_clients(monkeypatch, persona=persona_restricted)
    result1 = await router_client.fetch_model_metadata(MODEL_ID, "acme", "jwt")
    handler._model_names = result1[0]
    handler._table_columns = result1[1]
    handler._table_model_id = result1[2]
    handler._table_descriptions = result1[3]
    handler._table_trust_meta = result1[4]
    handler._table_persona_id = result1[5]
    handler._table_include_hidden = result1[6]
    handler._table_query_name = result1[7]
    handler._table_foreign_keys = result1[8]
    handler._table_row_estimates = result1[9]
    handler._looker_relations = result1[10]
    handler._table_project_slug = result1[11]

    handler._catalogue = CatalogueDB(
        model_names=handler._model_names,
        table_columns=handler._table_columns,
        tenant_slug="acme",
    )
    handler._catalogue_built_at = time.monotonic()

    # Now make fetch_model_metadata raise an error
    async def _failing_fetch(*args, **kwargs):
        raise ConnectionError("model-service down")

    monkeypatch.setattr(
        "src.jdbc.server.fetch_model_metadata", _failing_fetch,
    )

    # The refresh should fail gracefully, keeping the existing catalogue
    await handler._refresh_catalogue_if_stale()

    # SecretDim should STILL be hidden (the previous CLS-filtered catalogue is kept)
    cols = _catalogue_column_names_from_db(handler._catalogue)
    assert "SecretDim" not in cols, (
        "Fetch failure should keep prior CLS restrictions (fail-closed)"
    )
    assert "Region" in cols

    handler._catalogue.close()


# ---------------------------------------------------------------------------
# Test 9: persona-drop guard rejects partial snapshots (fail-closed)
# ---------------------------------------------------------------------------
async def test_persona_drop_guard_rejects_partial_snapshot(monkeypatch):
    """When the model-service returns metadata WITHOUT persona variants
    (e.g. persona API failed silently), the refresh must NOT replace the
    prior CLS-filtered catalogue with an unrestricted base relation.
    """
    from src.jdbc import server as jdbc_server

    monkeypatch.setattr(
        "src.jdbc.server.system_snapshot_get",
        lambda key: {
            "gateway.catalogue_cls_ttl": 0,
        }.get(key, 15),
    )

    handler = jdbc_server.PGWireServer()
    handler._tenant_slug = "acme"
    handler._model_id = MODEL_ID
    handler._jwt_token = "fake-jwt"

    # Phase 1: build with a persona that restricts SecretDim
    persona_restricted = _make_persona(
        restricted_column_ids=[SOURCE_COLUMN_ID_SECRET],
    )
    _patch_clients(monkeypatch, persona=persona_restricted)
    result1 = await router_client.fetch_model_metadata(MODEL_ID, "acme", "jwt")
    handler._model_names = result1[0]
    handler._table_columns = result1[1]
    handler._table_model_id = result1[2]
    handler._table_descriptions = result1[3]
    handler._table_trust_meta = result1[4]
    handler._table_persona_id = result1[5]
    handler._table_include_hidden = result1[6]
    handler._table_query_name = result1[7]
    handler._table_foreign_keys = result1[8]
    handler._table_row_estimates = result1[9]
    handler._looker_relations = result1[10]
    handler._table_project_slug = result1[11]

    handler._catalogue = CatalogueDB(
        model_names=handler._model_names,
        table_columns=handler._table_columns,
        tenant_slug="acme",
    )
    handler._catalogue_built_at = time.monotonic()

    # Verify the persona relation exists with SecretDim hidden
    assert "sales_eu_analyst" in handler._model_names
    persona_cols = _catalogue_column_names(handler._table_columns, "sales_eu_analyst")
    assert "SecretDim" not in persona_cols

    # Phase 2: simulate persona API failure by returning no personas
    # This would produce metadata with only the base "sales" relation
    # (no persona variants), which has ALL columns including SecretDim.
    model = {
        "id": MODEL_ID,
        "project_id": PROJECT_ID,
        "project_slug": "public",
        "slug": "sales",
        "deployed_version_id": None,
    }

    async def _list_models(tenant_slug, jwt_token):
        return [model]

    async def _measures(mid, tenant_slug, jwt_token, project_id=""):
        return MEASURES

    async def _dimensions(mid, tenant_slug, jwt_token, project_id=""):
        return DIMENSIONS

    async def _no_personas(mid, tenant_slug, jwt_token, project_id=""):
        return []  # Simulates persona API failure returning empty list

    async def _snapshot(mid, tenant_slug, jwt_token, project_id=""):
        return {"columns": [], "tables": []}

    async def _kpis(mid, tenant_slug, jwt_token, project_id=""):
        return []

    monkeypatch.setattr(router_client, "list_all_models_for_tenant", _list_models)
    monkeypatch.setattr(router_client, "get_model_measures", _measures)
    monkeypatch.setattr(router_client, "get_model_dimensions", _dimensions)
    monkeypatch.setattr(router_client, "get_model_personas", _no_personas)
    monkeypatch.setattr(router_client, "get_model_snapshot", _snapshot)
    monkeypatch.setattr(router_client, "get_model_kpis", _kpis)

    # The refresh should detect the persona drop and keep the prior catalogue
    await handler._refresh_catalogue_if_stale()

    # The catalogue should still be the old one with SecretDim hidden
    cols = _catalogue_column_names_from_db(handler._catalogue)
    assert "SecretDim" not in cols, (
        "Persona-drop guard should have rejected the partial snapshot; "
        "SecretDim is now visible in the catalogue (fail-open)"
    )
    assert "Region" in cols

    handler._catalogue.close()


# ---------------------------------------------------------------------------
# SOL-LAT-001: classify before refreshing, so an ordinary model query does not
# reload the whole tenant catalogue on the hot path. These tests are also the
# mutation proof of the fix: pre-fix BOTH seams called _refresh_catalogue_if_stale
# unconditionally, so the "refresh NOT awaited for a model query" assertions
# below fail against pre-fix code for the right reason.
# ---------------------------------------------------------------------------


async def _handler_with_catalogue(monkeypatch):
    """Build a PGWireServer with a real, unrestricted CatalogueDB (SOL-LAT-001)."""
    from src.jdbc import server as jdbc_server

    monkeypatch.setattr(
        "src.jdbc.server.system_snapshot_get",
        lambda key: {"gateway.catalogue_cls_ttl": 0}.get(key, 15),
    )
    handler = jdbc_server.PGWireServer()
    handler._tenant_slug = "acme"
    handler._model_id = MODEL_ID
    handler._jwt_token = "fake-jwt"

    _patch_clients(monkeypatch, persona=_make_persona(restricted_column_ids=[]))
    meta = await router_client.fetch_model_metadata(MODEL_ID, "acme", "jwt")
    (
        handler._model_names, handler._table_columns, handler._table_model_id,
        handler._table_descriptions, handler._table_trust_meta,
        handler._table_persona_id, handler._table_include_hidden,
        handler._table_query_name, handler._table_foreign_keys,
        handler._table_row_estimates, handler._looker_relations,
        handler._table_project_slug,
    ) = meta
    handler._catalogue = CatalogueDB(
        model_names=handler._model_names,
        table_columns=handler._table_columns,
        tenant_slug="acme",
    )
    handler._catalogue_built_at = time.monotonic()
    return handler


_MODEL_SQL = "SELECT * FROM sales_eu_analyst"
_CATALOGUE_SQL = "SELECT table_name FROM information_schema.tables"


async def test_references_catalogue_classifies_model_vs_catalogue(monkeypatch):
    """The public classifier must send model queries away from the catalogue
    (so they do not trigger a refresh) while keeping real catalogue queries,
    FROM-less metadata probes, and unparseable metadata SQL on the catalogue
    path. Preserves F-001-03 (a catalogue token inside a model-query literal
    must not hijack routing)."""
    handler = await _handler_with_catalogue(monkeypatch)
    cat = handler._catalogue
    try:
        # Ordinary model queries → NOT a catalogue query → no refresh.
        assert cat.references_catalogue(_MODEL_SQL) is False
        assert cat.references_catalogue(
            "SELECT region, sum(revenue) FROM sales_eu_analyst GROUP BY region"
        ) is False
        # F-001-03: a catalogue token living in a STRING LITERAL of a model
        # query must not be misrouted to the catalogue.
        assert cat.references_catalogue(
            "SELECT * FROM sales_eu_analyst WHERE region = 'pg_class'"
        ) is False

        # Real catalogue queries → catalogue path.
        assert cat.references_catalogue(_CATALOGUE_SQL) is True
        assert cat.references_catalogue("SELECT * FROM pg_catalog.pg_class") is True
        # FROM-less metadata probe (regex matched, no table refs) → catalogue.
        assert cat.references_catalogue("SELECT version()") is True

        # Conservative parse-fail: a catalogue-token statement sqlglot cannot
        # parse must stay on the security-safe catalogue path, not be forwarded.
        with patch("src.jdbc.catalogue.sqlglot.parse", side_effect=ValueError("boom")):
            assert cat.references_catalogue(
                "SELECT table_name FROM information_schema.tables WHERE ((("
            ) is True
    finally:
        cat.close()


async def test_simple_query_seam_refreshes_only_for_catalogue_sql(monkeypatch):
    """_handle_query (simple protocol) must refresh the catalogue ONLY for a
    real catalogue query, and forward an ordinary model query without a
    tenant-metadata reload. Pre-fix this seam refreshed unconditionally."""
    handler = await _handler_with_catalogue(monkeypatch)
    writer = AsyncMock()
    writer.write = lambda *_a, **_kw: None
    try:
        with (
            patch.object(handler, "_refresh_catalogue_if_stale", new=AsyncMock()) as refresh,
            patch.object(handler, "_handle_user_query", new=AsyncMock()) as forward,
        ):
            # Model query: no refresh, forwarded to the router.
            await handler._handle_query(_MODEL_SQL, writer)
            refresh.assert_not_awaited()
            forward.assert_awaited_once()

            # Catalogue query: refresh runs, not forwarded (catalogue serves it).
            refresh.reset_mock()
            forward.reset_mock()
            await handler._handle_query(_CATALOGUE_SQL, writer)
            refresh.assert_awaited_once()
            forward.assert_not_awaited()
    finally:
        handler._catalogue.close()


async def test_extended_query_seam_refreshes_only_for_catalogue_sql(monkeypatch):
    """_execute_for_extended (extended protocol) must refresh the catalogue
    ONLY for a real catalogue query. Both seams duplicate the boundary, so both
    are covered. Pre-fix this seam refreshed unconditionally."""
    handler = await _handler_with_catalogue(monkeypatch)
    try:
        with (
            patch.object(handler, "_refresh_catalogue_if_stale", new=AsyncMock()) as refresh,
            # The model-query branch (execute returns None) falls through to
            # session revalidation + router dispatch, both of which make HTTP
            # calls. Short-circuit them: the refresh gate runs first, which is
            # all this test asserts. Resolving to no model returns a clean
            # 42703 before any router dispatch.
            patch.object(handler, "_revalidate_session", new=AsyncMock()),
            patch.object(
                handler, "_resolve_model_id_and_variant",
                new=lambda *_a, **_kw: (None, False, None),
            ),
        ):
            await handler._execute_for_extended(_MODEL_SQL)
            refresh.assert_not_awaited()

            # Catalogue query: execute serves it from SQLite and returns before
            # any dispatch, so no network short-circuit is needed here.
            refresh.reset_mock()
            await handler._execute_for_extended(_CATALOGUE_SQL)
            refresh.assert_awaited_once()
    finally:
        handler._catalogue.close()
