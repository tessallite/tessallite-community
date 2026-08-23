"""Tests for AtScale SML parser and mapper."""
import textwrap
from pathlib import Path

import pytest

from shared.importers.atscale_parser import (
    SmlParseError,
    parse_sml_project,
    parse_sml_directory,
)
from shared.importers.atscale_mapper import map_atscale_to_tessallite

SML_FIXTURE_DIR = (
    Path(__file__).resolve().parents[4]
    / "docs" / "strategy" / "competitive-analysis" / "atscale"
    / "sml-models-crisp-cpg-retail"
)


def _details(warnings):
    return [warning.detail for warning in warnings]


def _minimal_sml_files() -> dict[str, str]:
    return {
        "atscale.yml": textwrap.dedent("""\
            unique_name: test-catalog
            object_type: catalog
            label: Test Catalog
        """),
        "datasets/fact_orders.yml": textwrap.dedent("""\
            unique_name: fact_orders
            object_type: dataset
            label: fact_orders
            table: fact_orders
            columns:
              - name: order_id
                data_type: long
              - name: amount
                data_type: "decimal(18,2)"
              - name: customer_id
                data_type: long
              - name: order_date
                data_type: date
        """),
        "datasets/dim_customer.yml": textwrap.dedent("""\
            unique_name: dim_customer
            object_type: dataset
            label: dim_customer
            table: dim_customer
            columns:
              - name: customer_id
                data_type: long
              - name: customer_name
                data_type: string
        """),
        "dimensions/Customer Dimension.yml": textwrap.dedent("""\
            unique_name: Customer Dimension
            object_type: dimension
            label: Customer
            type: standard
            hierarchies:
              - unique_name: Customer Hierarchy
                label: Customer Hierarchy
                levels:
                  - unique_name: Customer
            level_attributes:
              - unique_name: Customer
                label: Customer
                dataset: dim_customer
                name_column: customer_name
                key_columns:
                  - customer_id
        """),
        "metrics/m_total_amount.yml": textwrap.dedent("""\
            unique_name: m_total_amount
            object_type: metric
            label: Total Amount
            calculation_method: sum
            dataset: fact_orders
            column: amount
            format: '$#,##0.00'
        """),
        "metrics/m_order_count.yml": textwrap.dedent("""\
            unique_name: m_order_count
            object_type: metric
            label: Order Count
            calculation_method: count
            dataset: fact_orders
            column: order_id
        """),
        "calculations/Amount YTD.yml": textwrap.dedent("""\
            unique_name: Amount YTD
            object_type: metric_calc
            label: Amount YTD
            expression: "PeriodsToDate([Date].[Year], [Measures].[m_total_amount])"
            format: '$#,##0.00'
        """),
        "models/Orders Model.yml": textwrap.dedent("""\
            unique_name: Orders Model
            object_type: model
            label: Orders Model
            relationships:
              - unique_name: orders_customer
                from:
                  dataset: fact_orders
                  join_columns:
                    - customer_id
                to:
                  dimension: Customer Dimension
                  level: Customer
            metrics:
              - unique_name: m_total_amount
                folder: Measures
              - unique_name: m_order_count
                folder: Measures
              - unique_name: Amount YTD
                folder: Calculated Measures
        """),
    }


class TestSmlParser:
    def test_parse_minimal_project(self):
        files = _minimal_sml_files()
        result = parse_sml_project(files)
        assert result.catalog is not None
        assert result.catalog.unique_name == "test-catalog"
        assert len(result.models) == 1
        assert len(result.datasets) == 2
        assert len(result.dimensions) == 1
        assert len(result.metrics) == 2
        assert len(result.calculations) == 1

    def test_model_parsed_correctly(self):
        files = _minimal_sml_files()
        result = parse_sml_project(files)
        model = result.models[0]
        assert model.unique_name == "Orders Model"
        assert model.label == "Orders Model"
        assert len(model.relationships) == 1
        assert model.relationships[0].from_dataset == "fact_orders"
        assert model.relationships[0].to_dimension == "Customer Dimension"
        assert len(model.metric_refs) == 3

    def test_dataset_columns_parsed(self):
        files = _minimal_sml_files()
        result = parse_sml_project(files)
        ds = next(d for d in result.datasets if d.unique_name == "fact_orders")
        assert ds.table == "fact_orders"
        assert len(ds.columns) == 4
        col_names = {c.name for c in ds.columns}
        assert "amount" in col_names
        assert "order_date" in col_names

    def test_dimension_hierarchy_parsed(self):
        files = _minimal_sml_files()
        result = parse_sml_project(files)
        dim = result.dimensions[0]
        assert dim.unique_name == "Customer Dimension"
        assert len(dim.hierarchies) == 1
        assert dim.hierarchies[0].levels == ["Customer"]
        assert len(dim.level_attributes) == 1
        assert dim.level_attributes[0].name_column == "customer_name"

    def test_metric_parsed(self):
        files = _minimal_sml_files()
        result = parse_sml_project(files)
        metric = next(m for m in result.metrics if m.unique_name == "m_total_amount")
        assert metric.calculation_method == "sum"
        assert metric.column == "amount"
        assert metric.format == "$#,##0.00"

    def test_calculation_parsed(self):
        files = _minimal_sml_files()
        result = parse_sml_project(files)
        calc = result.calculations[0]
        assert calc.unique_name == "Amount YTD"
        assert "PeriodsToDate" in calc.expression

    def test_empty_project_raises(self):
        with pytest.raises(SmlParseError):
            parse_sml_project({"readme.md": "nothing here"})

    def test_invalid_yaml_skipped(self):
        files = _minimal_sml_files()
        files["bad.yml"] = "{{{{invalid yaml"
        result = parse_sml_project(files)
        skip_warnings = [w for w in _details(result.warnings) if "bad.yml" in w]
        assert len(skip_warnings) == 1

    def test_unknown_object_type_warned(self):
        files = _minimal_sml_files()
        files["custom/thing.yml"] = textwrap.dedent("""\
            unique_name: something
            object_type: custom_widget
            label: Something
        """)
        result = parse_sml_project(files)
        unknown_warnings = [w for w in _details(result.warnings) if "custom_widget" in w]
        assert len(unknown_warnings) == 1


class TestSmlMapper:
    def test_map_produces_valid_bundle(self):
        files = _minimal_sml_files()
        parsed = parse_sml_project(files)
        result = map_atscale_to_tessallite(parsed)
        bundle = result.bundle
        assert bundle["export_format"] == "tessallite-project/v1"
        assert len(bundle["models"]) == 1

    def test_model_has_correct_structure(self):
        files = _minimal_sml_files()
        parsed = parse_sml_project(files)
        result = map_atscale_to_tessallite(parsed)
        model = result.bundle["models"][0]
        assert model["model"]["slug"] == "orders_model"
        assert model["model"]["display_name"] == "Orders Model"

    def test_tables_from_datasets(self):
        files = _minimal_sml_files()
        parsed = parse_sml_project(files)
        result = map_atscale_to_tessallite(parsed)
        model = result.bundle["models"][0]
        assert len(model["tables"]) == 2
        table_names = {t["physical_name"] for t in model["tables"]}
        assert "fact_orders" in table_names
        assert "dim_customer" in table_names
        fact = next(t for t in model["tables"] if t["physical_name"] == "fact_orders")
        assert fact["table_type"] == "fact"
        dim_t = next(t for t in model["tables"] if t["physical_name"] == "dim_customer")
        assert dim_t["table_type"] == "dim_detail"

    def test_columns_mapped(self):
        files = _minimal_sml_files()
        parsed = parse_sml_project(files)
        result = map_atscale_to_tessallite(parsed)
        model = result.bundle["models"][0]
        assert len(model["columns"]) == 6

    def test_dimensions_mapped(self):
        files = _minimal_sml_files()
        parsed = parse_sml_project(files)
        result = map_atscale_to_tessallite(parsed)
        model = result.bundle["models"][0]
        assert len(model["dimensions"]) == 1
        assert model["dimensions"][0]["display_name"] == "Customer"

    def test_measures_mapped(self):
        files = _minimal_sml_files()
        parsed = parse_sml_project(files)
        result = map_atscale_to_tessallite(parsed)
        model = result.bundle["models"][0]
        assert len(model["measures"]) == 3
        names = {m["name"] for m in model["measures"]}
        assert "m_total_amount" in names
        assert "m_order_count" in names
        assert "Amount YTD" in names

    def test_calculated_measures_warned(self):
        files = _minimal_sml_files()
        parsed = parse_sml_project(files)
        result = map_atscale_to_tessallite(parsed)
        calc_warnings = [w for w in _details(result.warnings) if "Amount YTD" in w]
        assert len(calc_warnings) == 1
        assert "MDX" in calc_warnings[0]

    def test_joins_from_relationships(self):
        files = _minimal_sml_files()
        parsed = parse_sml_project(files)
        result = map_atscale_to_tessallite(parsed)
        model = result.bundle["models"][0]
        assert len(model["joins"]) == 1
        j = model["joins"][0]
        # Must use UUID FK fields matching the Join ORM model
        assert "left_table_id" in j
        assert "right_table_id" in j
        assert "left_column_id" in j
        assert "right_column_id" in j
        # Orientation and cardinality are SEPARATE fields (join-orientation
        # contract, invariant 3). An SML relationship is fact -> dimension, so
        # its cardinality is many-to-one and the orientation that preserves
        # the many side (the fact) is a LEFT join. Landing "many_to_one" in
        # ``join_type`` — as this import used to — parks a fan-out label in
        # the field that decides which rows survive, and the pocket
        # row-population proof then refuses the whole model.
        assert j["join_type"] == "left"
        assert j["cardinality"] == "many_to_one"
        # Left side: fact_orders table, customer_id column
        fact_table = next(
            t for t in model["tables"] if t["physical_name"] == "fact_orders"
        )
        assert j["left_table_id"] == fact_table["id"]
        left_col = next(
            c for c in model["columns"]
            if c["column_name"] == "customer_id"
            and c["model_table_id"] == fact_table["id"]
        )
        assert j["left_column_id"] == left_col["id"]
        # Right side: dim_customer table, customer_id column
        dim_table = next(
            t for t in model["tables"] if t["physical_name"] == "dim_customer"
        )
        assert j["right_table_id"] == dim_table["id"]
        right_col = next(
            c for c in model["columns"]
            if c["column_name"] == "customer_id"
            and c["model_table_id"] == dim_table["id"]
        )
        assert j["right_column_id"] == right_col["id"]

    def test_join_skipped_when_resolution_fails(self):
        files = _minimal_sml_files()
        # Remove the dimension dataset so right-side resolution fails
        del files["datasets/dim_customer.yml"]
        parsed = parse_sml_project(files)
        result = map_atscale_to_tessallite(parsed)
        model = result.bundle["models"][0]
        assert len(model["joins"]) == 0
        skip_warnings = [
            w for w in _details(result.warnings)
            if "skipped" in w.lower() and "join" in w.lower()
        ]
        assert len(skip_warnings) >= 1

    def test_hierarchies_mapped(self):
        files = _minimal_sml_files()
        parsed = parse_sml_project(files)
        result = map_atscale_to_tessallite(parsed)
        model = result.bundle["models"][0]
        assert len(model["hierarchies"]) == 1
        assert model["hierarchies"][0]["type"] == "explicit"
        assert len(model["hierarchies"][0]["levels"]) == 1

    def test_default_persona_created(self):
        files = _minimal_sml_files()
        parsed = parse_sml_project(files)
        result = map_atscale_to_tessallite(parsed)
        model = result.bundle["models"][0]
        assert len(model["personas"]) == 1
        assert model["personas"][0]["slug"] == "everyone"

    def test_format_string_preserved(self):
        files = _minimal_sml_files()
        parsed = parse_sml_project(files)
        result = map_atscale_to_tessallite(parsed)
        model = result.bundle["models"][0]
        total = next(m for m in model["measures"] if m["name"] == "m_total_amount")
        assert total["format"] == "$#,##0.00"


class TestMetricSourceBinding:
    def test_metric_bound_to_physical_column(self):
        files = _minimal_sml_files()
        parsed = parse_sml_project(files)
        result = map_atscale_to_tessallite(parsed)
        model = result.bundle["models"][0]
        total = next(m for m in model["measures"] if m["name"] == "m_total_amount")
        assert total["source_column_id"] is not None
        amount_col = next(
            c for c in model["columns"] if c["column_name"] == "amount"
        )
        assert total["source_column_id"] == amount_col["id"]

    def test_order_count_bound_to_physical_column(self):
        files = _minimal_sml_files()
        parsed = parse_sml_project(files)
        result = map_atscale_to_tessallite(parsed)
        model = result.bundle["models"][0]
        count_m = next(m for m in model["measures"] if m["name"] == "m_order_count")
        assert count_m["source_column_id"] is not None
        oid_col = next(
            c for c in model["columns"] if c["column_name"] == "order_id"
        )
        assert count_m["source_column_id"] == oid_col["id"]

    def test_standalone_metrics_include_tables(self):
        """Metrics without a model file should still import dataset tables."""
        files = {
            "datasets/fact_orders.yml": textwrap.dedent("""\
                unique_name: fact_orders
                object_type: dataset
                table: fact_orders
                columns:
                  - name: amount
                    data_type: "decimal(18,2)"
            """),
            "metrics/m_revenue.yml": textwrap.dedent("""\
                unique_name: m_revenue
                object_type: metric
                label: Revenue
                calculation_method: sum
                dataset: fact_orders
                column: amount
            """),
        }
        parsed = parse_sml_project(files)
        result = map_atscale_to_tessallite(parsed)
        model = result.bundle["models"][0]
        assert len(model["tables"]) == 1
        assert model["tables"][0]["physical_name"] == "fact_orders"
        assert len(model["columns"]) == 1
        m = model["measures"][0]
        assert m["source_column_id"] is not None
        assert m["source_column_id"] == model["columns"][0]["id"]


class TestDimensionColumnBinding:
    def test_dimension_source_column_bound(self):
        files = _minimal_sml_files()
        parsed = parse_sml_project(files)
        result = map_atscale_to_tessallite(parsed)
        model = result.bundle["models"][0]
        dim = model["dimensions"][0]
        assert dim["source_column_id"] is not None
        name_col = next(
            c for c in model["columns"] if c["column_name"] == "customer_name"
        )
        assert dim["source_column_id"] == name_col["id"]

    def test_hierarchy_level_key_attribute_bound(self):
        files = _minimal_sml_files()
        parsed = parse_sml_project(files)
        result = map_atscale_to_tessallite(parsed)
        model = result.bundle["models"][0]
        hier = model["hierarchies"][0]
        lvl = hier["levels"][0]
        assert lvl["key_attribute_id"] is not None
        assert lvl["key_attribute_source"] == "physical_column"
        dim_table = next(
            t for t in model["tables"] if t["physical_name"] == "dim_customer"
        )
        key_col = next(
            c for c in model["columns"]
            if c["column_name"] == "customer_id"
            and c["model_table_id"] == dim_table["id"]
        )
        assert lvl["key_attribute_id"] == key_col["id"]

    def test_hierarchy_level_display_attribute_bound(self):
        files = _minimal_sml_files()
        parsed = parse_sml_project(files)
        result = map_atscale_to_tessallite(parsed)
        model = result.bundle["models"][0]
        hier = model["hierarchies"][0]
        lvl = hier["levels"][0]
        assert "attributes" in lvl
        assert len(lvl["attributes"]) == 1
        attr = lvl["attributes"][0]
        assert attr["attribute_source"] == "physical_column"
        assert attr["role"] == "display"
        name_col = next(
            c for c in model["columns"] if c["column_name"] == "customer_name"
        )
        assert attr["attribute_id"] == name_col["id"]

    def test_dimension_dataset_table_role(self):
        files = _minimal_sml_files()
        parsed = parse_sml_project(files)
        result = map_atscale_to_tessallite(parsed)
        model = result.bundle["models"][0]
        dim_table = next(
            t for t in model["tables"] if t["physical_name"] == "dim_customer"
        )
        assert dim_table["table_type"] == "dim_detail"

    def test_missing_dimension_dataset_warned(self):
        files = _minimal_sml_files()
        del files["datasets/dim_customer.yml"]
        parsed = parse_sml_project(files)
        result = map_atscale_to_tessallite(parsed)
        dim_warnings = [w for w in _details(result.warnings) if "dim_customer" in w]
        assert len(dim_warnings) == 1

    def test_missing_dataset_skips_hierarchy_levels(self):
        files = _minimal_sml_files()
        del files["datasets/dim_customer.yml"]
        parsed = parse_sml_project(files)
        result = map_atscale_to_tessallite(parsed)
        model = result.bundle["models"][0]
        for hier in model["hierarchies"]:
            for lvl in hier["levels"]:
                assert lvl["key_attribute_id"] is not None

    def test_display_attribute_has_level_id(self):
        files = _minimal_sml_files()
        parsed = parse_sml_project(files)
        result = map_atscale_to_tessallite(parsed)
        model = result.bundle["models"][0]
        for hier in model["hierarchies"]:
            for lvl in hier["levels"]:
                if "attributes" in lvl:
                    for attr in lvl["attributes"]:
                        assert "level_id" in attr
                        assert attr["level_id"] == lvl["id"]

    def test_missing_dataset_warns_skipped_levels(self):
        files = _minimal_sml_files()
        del files["datasets/dim_customer.yml"]
        parsed = parse_sml_project(files)
        result = map_atscale_to_tessallite(parsed)
        skip_warnings = [w for w in _details(result.warnings) if "skipped levels" in w]
        assert len(skip_warnings) >= 1


class TestObjectTypeDispatch:
    def test_row_security_parsed_and_warned(self):
        files = _minimal_sml_files()
        files["security/row_sec.yml"] = textwrap.dedent("""\
            unique_name: region_filter
            object_type: row_security
            label: Region Filter
            dimension: Region Dimension
            attribute: Region
        """)
        result = parse_sml_project(files)
        assert len(result.row_security_rules) == 1
        assert result.row_security_rules[0].unique_name == "region_filter"
        sec_warnings = [w for w in _details(result.warnings) if "security" in w.lower()]
        assert len(sec_warnings) >= 1

    def test_composite_model_warned(self):
        files = _minimal_sml_files()
        files["composite/comp.yml"] = textwrap.dedent("""\
            unique_name: composite_orders
            object_type: composite_model
            label: Composite Orders
        """)
        result = parse_sml_project(files)
        comp_warnings = [w for w in _details(result.warnings) if "composite_model" in w]
        assert len(comp_warnings) == 1

    def test_package_warned(self):
        files = _minimal_sml_files()
        files["packages/pkg.yml"] = textwrap.dedent("""\
            unique_name: analytics_pkg
            object_type: package
            label: Analytics Package
        """)
        result = parse_sml_project(files)
        pkg_warnings = [w for w in _details(result.warnings) if "package" in w]
        assert len(pkg_warnings) == 1


class TestDuplicateModelDedup:
    def test_duplicate_model_deduplicated(self):
        files = _minimal_sml_files()
        # Add a second copy of the model file (simulates LLM-generated duplicate)
        files["models/Orders Model Copy.yml"] = files["models/Orders Model.yml"]
        parsed = parse_sml_project(files)
        assert len(parsed.models) == 1
        dup_warnings = [w for w in _details(parsed.warnings) if "Duplicate model" in w]
        assert len(dup_warnings) == 1

    def test_two_distinct_models_kept(self):
        files = _minimal_sml_files()
        files["models/Other Model.yml"] = textwrap.dedent("""\
            unique_name: Other Model
            object_type: model
            label: Other Model
            metrics:
              - unique_name: m_total_amount
                folder: Measures
        """)
        parsed = parse_sml_project(files)
        assert len(parsed.models) == 2


class TestCrispFixture:
    """Test against real AtScale CRISP CPG Retail SML project."""

    @pytest.mark.skipif(
        not SML_FIXTURE_DIR.exists(),
        reason="CRISP CPG Retail fixture not found",
    )
    def test_crisp_parses_without_errors(self):
        result = parse_sml_directory(SML_FIXTURE_DIR)
        assert result.errors == []
        assert len(result.models) == 1
        assert len(result.datasets) >= 10
        assert len(result.dimensions) >= 5
        assert len(result.metrics) >= 10

    @pytest.mark.skipif(
        not SML_FIXTURE_DIR.exists(),
        reason="CRISP CPG Retail fixture not found",
    )
    def test_crisp_maps_to_bundle(self):
        parsed = parse_sml_directory(SML_FIXTURE_DIR)
        result = map_atscale_to_tessallite(parsed, project_name="crisp-cpg")
        bundle = result.bundle
        assert bundle["export_format"] == "tessallite-project/v1"
        assert len(bundle["models"]) == 1
        model = bundle["models"][0]
        assert model["model"]["slug"] == "crisp_databricks"
        assert len(model["measures"]) >= 15
        assert len(model["dimensions"]) >= 4
        assert len(model["tables"]) >= 2

    @pytest.mark.skipif(
        not SML_FIXTURE_DIR.exists(),
        reason="CRISP CPG Retail fixture not found",
    )
    def test_crisp_calculations_produce_warnings(self):
        parsed = parse_sml_directory(SML_FIXTURE_DIR)
        result = map_atscale_to_tessallite(parsed)
        mdx_warnings = [w for w in _details(result.warnings) if "MDX" in w]
        assert len(mdx_warnings) >= 5

    @pytest.mark.skipif(
        not SML_FIXTURE_DIR.exists(),
        reason="CRISP CPG Retail fixture not found",
    )
    def test_crisp_source_type_detected_from_real_connection_file(self):
        # Bug-5939 (F-020-03): the real CRISP fixture's
        # connections/Connection - Crisp.yml has `as_connection: Databricks`.
        # The importer previously ignored this and always stamped
        # source_type: "postgresql" regardless of the actual platform.
        parsed = parse_sml_directory(SML_FIXTURE_DIR)
        assert len(parsed.connections) == 1
        assert parsed.connections[0].platform == "Databricks"
        result = map_atscale_to_tessallite(parsed)
        model = result.bundle["models"][0]
        source = model["data_sources"][0]
        assert source["source_type"] == "hadoop_spark"
        # Confidently detected — not a placeholder needing manual setup.
        assert source["config"] == {}


class TestAtScaleSourceTypeDetection:
    """Bug-5939 (F-020-03): AtScale importer must preserve the real source
    platform where the SML exposes it, and fall back to a clearly-marked
    unconfigured placeholder (with a warning) rather than silently
    presenting a guessed/default type as confirmed PostgreSQL."""

    def _files_with_connection(self, connection_yaml: str) -> dict[str, str]:
        files = _minimal_sml_files()
        files["connections/conn1.yml"] = textwrap.dedent(connection_yaml)
        # Point the fact dataset at the new connection.
        files["datasets/fact_orders.yml"] = files["datasets/fact_orders.yml"].replace(
            "object_type: dataset\n",
            "object_type: dataset\nconnection_id: conn1\n",
            1,
        )
        return files

    def test_as_connection_field_detected(self):
        files = self._files_with_connection("""\
            unique_name: conn1
            object_type: connection
            label: Main Connection
            as_connection: Snowflake
            database: analytics
            schema: public
        """)
        parsed = parse_sml_project(files)
        result = map_atscale_to_tessallite(parsed)
        source = result.bundle["models"][0]["data_sources"][0]
        assert source["source_type"] == "snowflake"
        assert source["config"] == {}

    def test_bigquery_detected_via_as_connection(self):
        files = self._files_with_connection("""\
            unique_name: conn1
            object_type: connection
            label: Main Connection
            as_connection: BigQuery
            database: proj
            schema: dataset1
        """)
        parsed = parse_sml_project(files)
        result = map_atscale_to_tessallite(parsed)
        source = result.bundle["models"][0]["data_sources"][0]
        assert source["source_type"] == "bigquery"

    def test_keyword_fallback_when_as_connection_absent(self):
        # No as_connection/platform-like field at all — the mapper falls
        # back to matching a known platform keyword in unique_name/label.
        files = self._files_with_connection("""\
            unique_name: Redshift Prod
            object_type: connection
            label: Redshift Prod
            database: analytics
            schema: public
        """)
        parsed = parse_sml_project(files)
        result = map_atscale_to_tessallite(parsed)
        source = result.bundle["models"][0]["data_sources"][0]
        assert source["source_type"] == "redshift"
        assert source["config"] == {}

    def test_unresolvable_platform_marked_as_unconfigured_placeholder(self):
        # No platform signal anywhere — must NOT silently present
        # PostgreSQL as a confirmed fact; must warn and mark as placeholder.
        files = self._files_with_connection("""\
            unique_name: conn1
            object_type: connection
            label: Main Connection
            database: analytics
            schema: public
        """)
        parsed = parse_sml_project(files)
        result = map_atscale_to_tessallite(parsed)
        source = result.bundle["models"][0]["data_sources"][0]
        assert source["source_type"] == "postgresql"
        assert source["config"] == {"unconfigured": True, "import_placeholder": True}
        assert any(
            "Could not determine the source database platform" in w
            for w in _details(result.warnings)
        )

    def test_no_connections_at_all_marked_as_unconfigured_placeholder(self):
        files = _minimal_sml_files()  # no connections/ files
        parsed = parse_sml_project(files)
        result = map_atscale_to_tessallite(parsed)
        source = result.bundle["models"][0]["data_sources"][0]
        assert source["source_type"] == "postgresql"
        assert source["config"] == {"unconfigured": True, "import_placeholder": True}


class TestStatisticalAggregationSafety:
    """F-020-08/09: statistical aggs and semi-additive enums must not be
    silently mapped to wrong functions / invalid enum values."""

    def _files_with_metric(self, calc_method: str, semi_additive: str = "") -> dict:
        files = _minimal_sml_files()
        extra = ""
        if semi_additive:
            extra = textwrap.dedent(f"""\
                semi_additive:
                  position: {semi_additive}
            """)
        files["metrics/m_stat.yml"] = textwrap.dedent(f"""\
            unique_name: m_stat
            object_type: metric
            label: Stat
            calculation_method: {calc_method}
            dataset: fact_orders
            column: amount
        """) + extra
        # Register the metric on the model so it is mapped.
        files["models/Orders Model.yml"] = files["models/Orders Model.yml"].replace(
            "metrics:\n",
            "metrics:\n              - unique_name: m_stat\n                folder: Measures\n",
        )
        return files

    def test_stddev_imported_as_disabled_not_sum(self):
        parsed = parse_sml_project(self._files_with_metric("stddev_pop"))
        result = map_atscale_to_tessallite(parsed)
        model = result.bundle["models"][0]
        stat = next(m for m in model["measures"] if m["name"] == "m_stat")
        # Must NOT silently become a real, queryable SUM measure.
        assert stat["is_invalid"] is True
        assert stat["invalid_reason"] is not None
        assert any("stddev_pop" in w for w in _details(result.warnings))

    def test_var_pop_imported_as_disabled(self):
        parsed = parse_sml_project(self._files_with_metric("var_pop"))
        result = map_atscale_to_tessallite(parsed)
        model = result.bundle["models"][0]
        stat = next(m for m in model["measures"] if m["name"] == "m_stat")
        assert stat["is_invalid"] is True

    def test_semi_additive_position_maps_to_valid_enum(self):
        parsed = parse_sml_project(self._files_with_metric("sum", semi_additive="last"))
        result = map_atscale_to_tessallite(parsed)
        model = result.bundle["models"][0]
        stat = next(m for m in model["measures"] if m["name"] == "m_stat")
        # F-020-09: "last" → "last_non_empty", NOT the invalid "last_value".
        assert stat["semi_additive_behavior"] == "last_non_empty"

    def test_supported_agg_stays_active(self):
        parsed = parse_sml_project(self._files_with_metric("average"))
        result = map_atscale_to_tessallite(parsed)
        model = result.bundle["models"][0]
        stat = next(m for m in model["measures"] if m["name"] == "m_stat")
        # Bug-6591: a representable agg imports active with the CANONICAL token
        # ("avg", not the non-canonical "average" that failed late at query time).
        assert stat["default_agg"] == "avg"
        assert stat["is_invalid"] is False
