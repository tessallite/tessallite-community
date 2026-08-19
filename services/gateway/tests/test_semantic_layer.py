"""Regression tests for the semantic-layer plan fixes.

Covers:
  - Phase 1 visibility cascade in JDBC / XMLA metadata rowsets
  - Phase 2 two-catalog emission for XMLA and _technical recognition
  - Phase 5 info.* virtual schema dispatching in the JDBC layer
  - Phase 5 description footer appended to both JDBC and XMLA metadata
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.dax import mdschema  # noqa: E402
from src.jdbc.catalogue import CatalogueDB, _build_trust_footer  # noqa: E402


CATALOG = "sales"
DIMS = [
    {"name": "Region", "display_name": "Region", "description": "Sales region", "is_hidden": False},
    {"name": "account_type_code", "display_name": "account_type_code",
     "description": "Internal surrogate", "is_hidden": True},
]
MEASURES = [
    {"name": "Revenue", "display_name": "Revenue", "description": "Net revenue",
     "display_folder": "Sales", "is_hidden": False},
    {"name": "internal_cost", "display_name": "internal_cost",
     "description": "Audit cost", "display_folder": "", "is_hidden": True},
]
TRUST_META = {
    "last_refreshed_at": "2026-04-13T14:00:00",
    "source_system": "bigquery",
    "owner": "jane.doe@example.com",
}


# ---------------------------------------------------------------------------
# Phase 1 — visibility cascade
# ---------------------------------------------------------------------------

def test_rows_dimensions_skips_hidden_and_marks_visible():
    # Bug-6603: standalone attribute dims collapse into the visible [Dimensions]
    # group node; the per-dimension hidden cascade shows on the hierarchies rowset
    # (each visible attribute keeps its own [Name].[Name] hierarchy).
    drows = mdschema._rows_dimensions(CATALOG, DIMS, {})
    group = next(r for r in drows if r["DIMENSION_NAME"] == "Dimensions")
    assert group["DIMENSION_IS_VISIBLE"] == "true"
    hrows = mdschema._rows_hierarchies(CATALOG, DIMS)
    hier_names = {r["HIERARCHY_NAME"] for r in hrows}
    assert "Region" in hier_names
    assert "account_type_code" not in hier_names


def test_rows_measures_skips_hidden_and_keeps_folder():
    rows = mdschema._rows_measures(CATALOG, MEASURES)
    names = {r["MEASURE_NAME"] for r in rows}
    assert "Revenue" in names
    assert "internal_cost" not in names
    rev = next(r for r in rows if r["MEASURE_NAME"] == "Revenue")
    assert rev["MEASURE_DISPLAY_FOLDER"] == "Sales"


# ---------------------------------------------------------------------------
# Phase 2 — two catalogs per model
# ---------------------------------------------------------------------------

def _model_with_personas(**extras):
    """Helper: a model dict that carries the auto-seeded technical
    persona plus any additional personas passed in."""
    personas = [
        {
            "slug": "technical",
            "name": "Technical",
            "description": "technical view",
            "includes_hidden_columns": True,
        }
    ] + list(extras.pop("extra_personas", []))
    return {
        "slug": "sales",
        "display_name": "Sales",
        "id": "m1",
        "personas": personas,
        **extras,
    }


def test_rows_catalogs_emits_business_base_plus_one_per_persona():
    tenant_models = [_model_with_personas()]
    rows = mdschema._rows_catalogs("", tenant_models)
    catalog_names = {r["CATALOG_NAME"] for r in rows}
    assert "sales" in catalog_names
    assert "sales_technical" in catalog_names


def test_rows_catalogs_emits_custom_persona_catalog():
    tenant_models = [_model_with_personas(extra_personas=[
        {"slug": "finance", "name": "Finance", "description": "Finance scope"},
    ])]
    rows = mdschema._rows_catalogs("", tenant_models)
    catalog_names = {r["CATALOG_NAME"] for r in rows}
    assert catalog_names == {"sales", "sales_technical", "sales_finance"}


def test_rows_cubes_enumerates_all_variants_when_no_catalog_restriction():
    tenant_models = [_model_with_personas()]
    rows = mdschema._rows_cubes("", tenant_models=tenant_models)
    cube_names = {r["CUBE_NAME"] for r in rows}
    assert cube_names == {"sales", "sales_technical"}


def test_rows_cubes_returns_single_cube_when_bound_to_catalog():
    rows = mdschema._rows_cubes("sales_technical", tenant_models=None)
    assert len(rows) == 1
    assert rows[0]["CUBE_NAME"] == "sales_technical"


# ---------------------------------------------------------------------------
# Phase 5 — info.* virtual schema (via CatalogueDB)
# ---------------------------------------------------------------------------

MODEL_NAMES = ["sales", "sales_technical"]
TABLE_COLUMNS = {
    "sales": [
        {"name": "Revenue", "kind": "measure", "display_name": "Revenue"},
        {"name": "Region",  "kind": "dimension", "display_name": "Region"},
    ],
    "sales_technical": [
        {"name": "Revenue", "kind": "measure", "display_name": "Revenue"},
        {"name": "Region",  "kind": "dimension", "display_name": "Region"},
        {"name": "internal_cost", "kind": "measure", "display_name": "internal_cost"},
    ],
}
TABLE_TRUST = {
    "sales": TRUST_META,
    "sales_technical": TRUST_META,
}
TABLE_MODEL_ID = {
    "sales": "m1",
    "sales_technical": "m1",
}


def _make_catalogue():
    return CatalogueDB(
        model_names=MODEL_NAMES,
        table_columns=TABLE_COLUMNS,
        table_trust_meta=TABLE_TRUST,
        table_model_id=TABLE_MODEL_ID,
    )


def test_catalogue_routes_info_freshness():
    cat = _make_catalogue()
    result = cat.execute("SELECT * FROM info.model_freshness")
    assert result is not None
    cat.close()


def test_catalogue_info_freshness_returns_row_per_measure_once_per_model():
    cat = _make_catalogue()
    result = cat.execute("SELECT * FROM info.model_freshness")
    assert result is not None
    cols, rows = result
    col_names = [c[0] for c in cols]
    assert col_names == ["model_name", "measure_name", "last_refreshed_at", "source_system"]
    # Business view has 1 measure -> one row; the technical variant is
    # deduped because it shares the same canonical model name.
    assert all(row[0] == "sales" for row in rows)
    assert any(row[1] == "Revenue" for row in rows)
    cat.close()


def test_catalogue_info_owners_deduplicates_by_canonical_model():
    cat = _make_catalogue()
    result = cat.execute("SELECT * FROM info.model_owners")
    assert result is not None
    _, rows = result
    model_names = [row[0] for row in rows]
    assert model_names.count("sales") == 1
    cat.close()


def test_catalogue_info_lineage_emits_one_row_per_visible_object():
    cat = _make_catalogue()
    result = cat.execute("SELECT * FROM info.model_lineage")
    assert result is not None
    _, rows = result
    objects = {(row[1], row[2]) for row in rows if row[0] == "sales"}
    assert ("measure", "Revenue") in objects
    assert ("dimension", "Region") in objects
    cat.close()


# ---------------------------------------------------------------------------
# Phase 5 — description footers
# ---------------------------------------------------------------------------

def test_jdbc_trust_footer_includes_all_three_signals():
    footer = _build_trust_footer(TRUST_META)
    assert "last refreshed 2026-04-13 14:00:00" in footer
    assert "source: bigquery" in footer
    assert "owner: jane.doe@example.com" in footer


def test_xmla_trust_footer_matches_jdbc_format():
    footer = mdschema._build_trust_footer_xmla(TRUST_META)
    assert footer.startswith("(")
    assert "source: bigquery" in footer


def test_xmla_dimension_description_carries_footer():
    # Bug-6603: a standalone dimension's own description now lives on its hierarchy
    # row (the DIMENSIONS rowset emits the shared [Dimensions] group node); the
    # trust footer must still reach it.
    rows = mdschema._rows_hierarchies(CATALOG, DIMS, trust_meta=TRUST_META)
    region_row = next(r for r in rows if r["HIERARCHY_NAME"] == "Region")
    assert "source: bigquery" in region_row["DESCRIPTION"]


def test_xmla_measure_description_carries_footer():
    rows = mdschema._rows_measures(CATALOG, MEASURES, trust_meta=TRUST_META)
    rev = next(r for r in rows if r["MEASURE_NAME"] == "Revenue")
    assert "source: bigquery" in rev["DESCRIPTION"]


def test_named_list_refresh_vintage_is_available_in_gateway_set_catalogue():
    """2026-08-11 named-list refresh vintage gap: named-set catalogue
    descriptions consume the same trust metadata as tables and measures."""
    rows = mdschema._rows_sets(
        CATALOG,
        [{
            "name": "FocusRegions",
            "display_name": "Focus Regions",
            "description": "Regions used by the sales team",
            "list_type": "advanced_mdx",
            "expression": "{ [Region].[Region].Members }",
            "trust_meta": TRUST_META,
        }],
    )
    assert len(rows) == 1
    assert "last refreshed 2026-04-13 14:00:00" in rows[0]["SET_DESCRIPTION"]
    assert "source: bigquery" in rows[0]["SET_DESCRIPTION"]


# ---------------------------------------------------------------------------
# Phase 2 — schema-per-project
# ---------------------------------------------------------------------------

def _make_project_catalogue():
    """Build a catalogue with two project slugs."""
    names = ["orders", "orders_technical", "inventory"]
    columns = {
        "orders": [{"name": "order_id", "data_type": "bigint", "is_primary_key": True}],
        "orders_technical": [{"name": "order_id", "data_type": "bigint", "is_primary_key": True}],
        "inventory": [{"name": "sku", "data_type": "varchar", "is_primary_key": True}],
    }
    project_slug = {
        "orders": "ecommerce",
        "orders_technical": "ecommerce",
        "inventory": "warehouse",
    }
    return CatalogueDB(
        model_names=names,
        table_columns=columns,
        tenant_slug="acme",
        table_project_slug=project_slug,
    )


def test_schema_per_project_pg_namespace_has_project_entries():
    cat = _make_project_catalogue()
    result = cat.execute("SELECT nspname FROM pg_namespace ORDER BY oid")
    assert result is not None
    _, rows = result
    names = [row[0] for row in rows]
    assert "public" in names
    assert "ecommerce" in names
    assert "warehouse" in names
    cat.close()


def test_schema_per_project_public_schema_has_no_tables():
    cat = _make_project_catalogue()
    result = cat.execute(
        "SELECT relname FROM pg_class c "
        "JOIN pg_namespace n ON c.relnamespace = n.oid "
        "WHERE n.nspname = 'public'"
    )
    assert result is not None
    _, rows = result
    assert rows == []
    cat.close()


def test_schema_per_project_tables_in_correct_schema():
    cat = _make_project_catalogue()
    result = cat.execute(
        "SELECT n.nspname, c.relname FROM pg_class c "
        "JOIN pg_namespace n ON c.relnamespace = n.oid "
        "ORDER BY c.relname"
    )
    assert result is not None
    _, rows = result
    by_table = {row[1]: row[0] for row in rows}
    assert by_table["inventory"] == "warehouse"
    assert by_table["orders"] == "ecommerce"
    assert by_table["orders_technical"] == "ecommerce"
    cat.close()


def test_schema_per_project_information_schema_tables():
    cat = _make_project_catalogue()
    result = cat.execute(
        "SELECT table_schema, table_name FROM information_schema.tables "
        "ORDER BY table_name"
    )
    assert result is not None
    _, rows = result
    # Since Bug-5552/5553 (e865ce70) tables are registered ONLY under their
    # project schema -- the old duplicate 'public' registration made Power BI
    # show every table twice.
    from collections import defaultdict
    schemas_by_table = defaultdict(set)
    for row in rows:
        schemas_by_table[row[1]].add(row[0])
    assert schemas_by_table["inventory"] == {"warehouse"}
    assert schemas_by_table["orders"] == {"ecommerce"}
    assert schemas_by_table["orders_technical"] == {"ecommerce"}
    cat.close()


def test_schema_per_project_public_filter_finds_project_tables():
    """Compatibility: BI clients that probe public explicitly still discover
    project-scoped semantic tables without broad catalogue duplication."""
    cat = _make_project_catalogue()
    result = cat.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public' ORDER BY table_name"
    )
    assert result is not None
    _, rows = result
    table_names = {row[0] for row in rows}
    assert {"inventory", "orders", "orders_technical"}.issubset(table_names)
    cat.close()


def test_information_schema_tables_are_derived_from_model_names_only():
    """A source relation in column metadata must not become a BI catalogue table."""
    cat = CatalogueDB(
        model_names=["sales"],
        table_columns={
            "sales": [{"name": "revenue", "data_type": "numeric"}],
            "source_payment_transaction": [{"name": "amount", "data_type": "numeric"}],
        },
    )
    result = cat.execute(
        "SELECT table_name FROM information_schema.tables ORDER BY table_name"
    )
    assert result is not None
    _, rows = result
    table_names = {row[0] for row in rows}
    assert "sales" in table_names
    assert "source_payment_transaction" not in table_names
    cat.close()


def test_schema_per_project_information_schema_columns():
    cat = _make_project_catalogue()
    result = cat.execute(
        "SELECT table_schema, table_name, column_name "
        "FROM information_schema.columns ORDER BY table_name"
    )
    assert result is not None
    _, rows = result
    # Project-schema-only registration (see Bug-5552/5553 note above).
    from collections import defaultdict
    schemas_by_table = defaultdict(set)
    for row in rows:
        schemas_by_table[row[1]].add(row[0])
    assert schemas_by_table["inventory"] == {"warehouse"}
    assert schemas_by_table["orders"] == {"ecommerce"}
    cat.close()


def test_schema_per_project_public_filter_finds_project_columns():
    cat = _make_project_catalogue()
    result = cat.execute(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' ORDER BY table_name, column_name"
    )
    assert result is not None
    _, rows = result
    columns_by_table = {row[0]: row[1] for row in rows}
    assert columns_by_table["inventory"] == "sku"
    assert columns_by_table["orders"] == "order_id"
    cat.close()


def test_schema_per_project_pg_tables_uses_project_slug():
    cat = _make_project_catalogue()
    result = cat.execute(
        "SELECT schemaname, tablename FROM pg_tables ORDER BY tablename"
    )
    assert result is not None
    _, rows = result
    # Project-schema-only registration (see Bug-5552/5553 note above).
    from collections import defaultdict
    schemas_by_table = defaultdict(set)
    for row in rows:
        schemas_by_table[row[1]].add(row[0])
    assert schemas_by_table["inventory"] == {"warehouse"}
    assert schemas_by_table["orders"] == {"ecommerce"}
    cat.close()


def test_schema_per_project_fallback_to_public_without_mapping():
    """Without table_project_slug, tables fall back to public schema."""
    cat = CatalogueDB(
        model_names=["sales"],
        table_columns={"sales": [{"name": "revenue", "data_type": "numeric"}]},
    )
    result = cat.execute(
        "SELECT table_schema FROM information_schema.tables WHERE table_name = 'sales'"
    )
    assert result is not None
    _, rows = result
    assert rows[0][0] == "public"
    cat.close()


def test_schema_per_project_schemata_lists_all():
    cat = _make_project_catalogue()
    result = cat.execute(
        "SELECT schema_name FROM information_schema.schemata ORDER BY schema_name"
    )
    assert result is not None
    _, rows = result
    names = [row[0] for row in rows]
    assert "ecommerce" in names
    assert "info" in names
    assert "public" in names
    assert "warehouse" in names
    cat.close()
