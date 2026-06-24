"""Tests for dbt semantic model parser and mapper."""
import textwrap
from pathlib import Path

import pytest

from shared.importers.dbt_parser import DbtParseError, parse_dbt_yaml, parse_dbt_project
from shared.importers.dbt_mapper import map_dbt_to_tessallite

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "dbt"


SAMPLE_DBT_YAML = textwrap.dedent("""\
    semantic_models:
      - name: orders
        model: ref('stg_orders')
        description: One row per order
        defaults:
          agg_time_dimension: order_date
        entities:
          - name: order_id
            type: primary
          - name: customer_id
            type: foreign
        dimensions:
          - name: order_date
            type: time
            type_params:
              time_granularity: day
          - name: status
            type: categorical
            description: Order status
        measures:
          - name: order_total
            agg: sum
            expr: amount
            description: Total order amount
          - name: order_count
            agg: count
            create_metric: true

    metrics:
      - name: revenue
        type: simple
        label: Total Revenue
        description: Sum of all order amounts
        type_params:
          measure: order_total
      - name: large_orders
        type: derived
        label: Large Orders
        description: Orders above threshold
        type_params:
          expr: "order_total / order_count"
""")


def test_parse_dbt_yaml_basic():
    result = parse_dbt_yaml(SAMPLE_DBT_YAML)
    assert len(result.semantic_models) == 1
    sm = result.semantic_models[0]
    assert sm.name == "orders"
    assert sm.model == "ref('stg_orders')"
    assert len(sm.entities) == 2
    assert len(sm.dimensions) == 2
    assert len(sm.measures) == 2
    assert sm.measures[0].agg == "sum"
    assert sm.measures[0].expr == "amount"
    assert len(result.metrics) == 2
    assert result.metrics[0].metric_type == "simple"


def test_parse_rejects_empty_yaml():
    with pytest.raises(DbtParseError) as exc_info:
        parse_dbt_yaml("foo: bar")
    assert "No semantic_models" in exc_info.value.errors[0]


def test_parse_rejects_missing_name():
    yaml_str = textwrap.dedent("""\
        semantic_models:
          - model: ref('test')
            entities: []
    """)
    with pytest.raises(DbtParseError):
        parse_dbt_yaml(yaml_str)


def test_map_produces_valid_bundle():
    parsed = parse_dbt_yaml(SAMPLE_DBT_YAML)
    result = map_dbt_to_tessallite(parsed)

    bundle = result.bundle
    assert bundle["export_format"] == "tessallite-project/v1"
    assert len(bundle["models"]) == 1

    model = bundle["models"][0]
    assert model["model"]["slug"] == "orders"
    assert len(model["tables"]) == 1
    assert model["tables"][0]["physical_name"] == "stg_orders"

    assert len(model["dimensions"]) == 2
    dim_names = {d["name"] for d in model["dimensions"]}
    assert "order_date" in dim_names
    assert "status" in dim_names

    assert len(model["measures"]) == 2
    m_names = {m["name"] for m in model["measures"]}
    assert "order_total" in m_names
    assert "order_count" in m_names

    order_total = next(m for m in model["measures"] if m["name"] == "order_total")
    assert order_total["default_agg"] == "sum"


def test_map_generates_time_hierarchy():
    parsed = parse_dbt_yaml(SAMPLE_DBT_YAML)
    result = map_dbt_to_tessallite(parsed)
    model = result.bundle["models"][0]

    assert len(model["hierarchies"]) == 1
    hier = model["hierarchies"][0]
    assert hier["type"] == "date_embedded"
    level_names = [l["name"] for l in hier["levels"]]
    assert "Year" in level_names
    assert "Day" in level_names


def test_time_hierarchy_levels_have_unique_uda_keys():
    parsed = parse_dbt_yaml(SAMPLE_DBT_YAML)
    result = map_dbt_to_tessallite(parsed)
    model = result.bundle["models"][0]
    hier = model["hierarchies"][0]
    key_ids = [l["key_attribute_id"] for l in hier["levels"]]
    assert len(key_ids) == len(set(key_ids)), "Each level must have a unique key_attribute_id"
    for lvl in hier["levels"]:
        assert lvl["key_attribute_source"] == "user_defined_attribute"


def test_time_hierarchy_generates_udas():
    parsed = parse_dbt_yaml(SAMPLE_DBT_YAML)
    result = map_dbt_to_tessallite(parsed)
    model = result.bundle["models"][0]
    udas = model["user_defined_attributes"]
    assert len(udas) >= 4
    uda_names = {u["name"] for u in udas}
    assert "order_date_year" in uda_names
    assert "order_date_day" in uda_names
    year_uda = next(u for u in udas if u["name"] == "order_date_year")
    assert "EXTRACT(YEAR" in year_uda["expression"]
    assert year_uda["output_data_type"] == "integer"


def test_time_hierarchy_generates_uda_column_refs():
    parsed = parse_dbt_yaml(SAMPLE_DBT_YAML)
    result = map_dbt_to_tessallite(parsed)
    model = result.bundle["models"][0]
    refs = model["uda_column_refs"]
    assert len(refs) >= 4
    uda_ids = {u["id"] for u in model["user_defined_attributes"]}
    for ref in refs:
        assert ref["attribute_id"] in uda_ids


def test_time_hierarchy_expression_not_quoted_as_identifier():
    yaml_str = textwrap.dedent("""\
        semantic_models:
          - name: orders
            model: ref('orders')
            dimensions:
              - name: order_date
                type: time
                expr: "cast(ordered_at as DATE)"
                type_params:
                  time_granularity: day
            measures:
              - name: order_count
                agg: count
    """)
    parsed = parse_dbt_yaml(yaml_str)
    result = map_dbt_to_tessallite(parsed)
    model = result.bundle["models"][0]
    udas = model["user_defined_attributes"]
    year_uda = next(u for u in udas if u["name"] == "order_date_year")
    assert '"cast(ordered_at as DATE)"' not in year_uda["expression"]
    assert "cast(ordered_at as DATE)" in year_uda["expression"]
    assert year_uda["validated"] is False


def test_time_hierarchy_bare_column_quoted():
    parsed = parse_dbt_yaml(SAMPLE_DBT_YAML)
    result = map_dbt_to_tessallite(parsed)
    model = result.bundle["models"][0]
    udas = model["user_defined_attributes"]
    year_uda = next(u for u in udas if u["name"] == "order_date_year")
    assert '"order_date"' in year_uda["expression"]
    assert year_uda["validated"] is False


def test_expr_dimension_becomes_uda_not_column():
    yaml_str = textwrap.dedent("""\
        semantic_models:
          - name: payments
            model: ref('payments')
            dimensions:
              - name: is_food
                type: categorical
                expr: "case when is_food_item = 1 then 'yes' else 'no' end"
              - name: customer_id
                type: categorical
            measures:
              - name: food_revenue
                agg: sum
                expr: "case when is_food_item = 1 then product_price else 0 end"
              - name: total_qty
                agg: sum
                expr: quantity
    """)
    parsed = parse_dbt_yaml(yaml_str)
    result = map_dbt_to_tessallite(parsed)
    model = result.bundle["models"][0]
    columns = model["columns"]
    dims = model["dimensions"]
    measures = model["measures"]
    udas = model["user_defined_attributes"]

    col_names = [c["column_name"] for c in columns]
    assert "customer_id" in col_names
    assert "quantity" in col_names
    assert "case when is_food_item = 1" not in " ".join(col_names)

    is_food_dim = next(d for d in dims if d["name"] == "is_food")
    assert is_food_dim["source_column_id"] is None
    assert is_food_dim["user_defined_attribute_id"] is not None

    customer_dim = next(d for d in dims if d["name"] == "customer_id")
    assert customer_dim["source_column_id"] is not None
    assert "user_defined_attribute_id" not in customer_dim

    food_rev = next(m for m in measures if m["name"] == "food_revenue")
    assert food_rev["source_column_id"] is None
    assert food_rev["user_defined_attribute_id"] is not None

    total_qty = next(m for m in measures if m["name"] == "total_qty")
    assert total_qty["source_column_id"] is not None

    food_uda = next(u for u in udas if u["name"] == "dbt_is_food")
    assert "case when" in food_uda["expression"]
    assert food_uda["validated"] is False

    rev_uda = next(u for u in udas if u["name"] == "dbt_food_revenue")
    assert "case when" in rev_uda["expression"]
    assert rev_uda["validated"] is False

    assert any("SQL expression" in w for w in result.warnings)


def test_map_applies_simple_metric_labels():
    parsed = parse_dbt_yaml(SAMPLE_DBT_YAML)
    result = map_dbt_to_tessallite(parsed)
    model = result.bundle["models"][0]

    order_total = next(m for m in model["measures"] if m["name"] == "order_total")
    assert order_total["display_name"] == "Total Revenue"
    assert order_total["description"] == "Sum of all order amounts"


def test_map_warns_on_derived_metric():
    parsed = parse_dbt_yaml(SAMPLE_DBT_YAML)
    result = map_dbt_to_tessallite(parsed)
    derived_warnings = [w for w in result.warnings if "large_orders" in w]
    assert len(derived_warnings) == 1
    assert "calculated measure" in derived_warnings[0].lower()


def test_map_warns_on_foreign_entity():
    parsed = parse_dbt_yaml(SAMPLE_DBT_YAML)
    result = map_dbt_to_tessallite(parsed)
    foreign_warnings = [w for w in result.warnings if "customer_id" in w]
    assert len(foreign_warnings) == 1


def test_map_warns_on_unsupported_aggregation():
    yaml_str = textwrap.dedent("""\
        semantic_models:
          - name: test
            model: ref('test')
            measures:
              - name: weird
                agg: hyperloglog
    """)
    parsed = parse_dbt_yaml(yaml_str)
    result = map_dbt_to_tessallite(parsed)
    agg_warnings = [w for w in result.warnings if "hyperloglog" in w]
    assert len(agg_warnings) == 1


def test_map_creates_default_persona():
    parsed = parse_dbt_yaml(SAMPLE_DBT_YAML)
    result = map_dbt_to_tessallite(parsed)
    model = result.bundle["models"][0]
    assert len(model["personas"]) == 1
    assert model["personas"][0]["slug"] == "everyone"


def test_parse_dbt_project_multiple_files():
    other_yaml = textwrap.dedent("""\
        semantic_models:
          - name: customers
            model: ref('stg_customers')
            dimensions:
              - name: country
                type: categorical
            measures:
              - name: customer_count
                agg: count_distinct
                expr: customer_id
    """)
    files = {
        "models/orders.yml": SAMPLE_DBT_YAML,
        "models/customers.yml": other_yaml,
        "readme.md": "not yaml",
    }
    result = parse_dbt_project(files)
    assert len(result.semantic_models) == 2

    mapped = map_dbt_to_tessallite(result)
    assert len(mapped.bundle["models"]) == 2


def test_non_additive_dimension_maps():
    yaml_str = textwrap.dedent("""\
        semantic_models:
          - name: balances
            model: ref('account_balances')
            dimensions:
              - name: snapshot_date
                type: time
                type_params:
                  time_granularity: day
            measures:
              - name: balance
                agg: sum
                expr: balance_amount
                non_additive_dimension:
                  name: snapshot_date
                  agg: max
    """)
    parsed = parse_dbt_yaml(yaml_str)
    result = map_dbt_to_tessallite(parsed)
    model = result.bundle["models"][0]
    balance = next(m for m in model["measures"] if m["name"] == "balance")
    # F-020-09: non_additive_dimension agg maps to a VALID Tessallite
    # semi_additive_behavior enum, not the invalid "max_over_snapshot_date"
    # the rewriter could not interpret.
    assert balance["semi_additive_behavior"] == "max"
    assert balance["is_invalid"] is False


class TestLabelMapping:
    def test_semantic_model_label_used_as_display_name(self):
        yaml_str = textwrap.dedent("""\
            semantic_models:
              - name: order_items
                model: ref('order_items')
                label: Order Line Items
                dimensions:
                  - name: sku
                    type: categorical
                measures:
                  - name: qty
                    agg: sum
        """)
        parsed = parse_dbt_yaml(yaml_str)
        assert parsed.semantic_models[0].label == "Order Line Items"
        result = map_dbt_to_tessallite(parsed)
        assert result.bundle["models"][0]["model"]["display_name"] == "Order Line Items"

    def test_dimension_label_used_as_display_name(self):
        yaml_str = textwrap.dedent("""\
            semantic_models:
              - name: sales
                model: ref('sales')
                dimensions:
                  - name: product_category
                    type: categorical
                    label: Product Category
                measures:
                  - name: amount
                    agg: sum
        """)
        parsed = parse_dbt_yaml(yaml_str)
        assert parsed.semantic_models[0].dimensions[0].label == "Product Category"
        result = map_dbt_to_tessallite(parsed)
        dim = result.bundle["models"][0]["dimensions"][0]
        assert dim["display_name"] == "Product Category"

    def test_measure_label_used_as_display_name(self):
        yaml_str = textwrap.dedent("""\
            semantic_models:
              - name: sales
                model: ref('sales')
                measures:
                  - name: total_revenue
                    agg: sum
                    expr: amount
                    label: Total Revenue (USD)
        """)
        parsed = parse_dbt_yaml(yaml_str)
        assert parsed.semantic_models[0].measures[0].label == "Total Revenue (USD)"
        result = map_dbt_to_tessallite(parsed)
        m = result.bundle["models"][0]["measures"][0]
        assert m["display_name"] == "Total Revenue (USD)"

    def test_missing_label_falls_back_to_humanized_name(self):
        yaml_str = textwrap.dedent("""\
            semantic_models:
              - name: my_model
                model: ref('my_model')
                dimensions:
                  - name: order_status
                    type: categorical
                measures:
                  - name: order_total
                    agg: sum
        """)
        parsed = parse_dbt_yaml(yaml_str)
        result = map_dbt_to_tessallite(parsed)
        model = result.bundle["models"][0]
        assert model["model"]["display_name"] == "My Model"
        assert model["dimensions"][0]["display_name"] == "Order Status"
        assert model["measures"][0]["display_name"] == "Order Total"


class TestSavedQueryParsing:
    def test_saved_query_parsed(self):
        yaml_str = textwrap.dedent("""\
            semantic_models:
              - name: orders
                model: ref('orders')
                measures:
                  - name: revenue
                    agg: sum
            saved_queries:
              - name: weekly_revenue
                description: Revenue by week
                label: Weekly Revenue Report
                query_params:
                  metrics:
                    - revenue
                  group_by:
                    - "Dimension('order_date')"
                  where:
                    - where_sql_template: "{{ Dimension('status') }} = 'completed'"
        """)
        parsed = parse_dbt_yaml(yaml_str)
        assert len(parsed.saved_queries) == 1
        sq = parsed.saved_queries[0]
        assert sq.name == "weekly_revenue"
        assert sq.label == "Weekly Revenue Report"
        assert sq.metrics == ["revenue"]
        assert len(sq.group_by) == 1
        assert len(sq.where) == 1

    def test_saved_query_exports_produce_warning(self):
        yaml_str = textwrap.dedent("""\
            semantic_models:
              - name: orders
                model: ref('orders')
                measures:
                  - name: revenue
                    agg: sum
            saved_queries:
              - name: export_test
                query_params:
                  metrics:
                    - revenue
                exports:
                  - name: my_export
                    config:
                      export_as: table
        """)
        parsed = parse_dbt_yaml(yaml_str)
        assert len(parsed.saved_queries) == 1
        export_warnings = [w for w in parsed.warnings if "export" in w.lower()]
        assert len(export_warnings) == 1

    def test_saved_query_mapped_to_warning(self):
        yaml_str = textwrap.dedent("""\
            semantic_models:
              - name: orders
                model: ref('orders')
                measures:
                  - name: revenue
                    agg: sum
            saved_queries:
              - name: my_report
                query_params:
                  metrics:
                    - revenue
                  group_by:
                    - "Dimension('status')"
        """)
        parsed = parse_dbt_yaml(yaml_str)
        result = map_dbt_to_tessallite(parsed)
        sq_warnings = [w for w in result.warnings if "my_report" in w]
        assert len(sq_warnings) == 1
        assert "report template" in sq_warnings[0].lower() or "saved view" in sq_warnings[0].lower()

    def test_saved_query_without_name_skipped(self):
        yaml_str = textwrap.dedent("""\
            semantic_models:
              - name: orders
                model: ref('orders')
                measures:
                  - name: revenue
                    agg: sum
            saved_queries:
              - query_params:
                  metrics:
                    - revenue
        """)
        parsed = parse_dbt_yaml(yaml_str)
        assert len(parsed.saved_queries) == 0
        skip_warnings = [w for w in parsed.warnings if "missing 'name'" in w]
        assert len(skip_warnings) == 1


class TestMetricFilterMapping:
    def test_simple_metric_filter_mapped_to_persona(self):
        yaml_str = textwrap.dedent("""\
            semantic_models:
              - name: orders
                model: ref('orders')
                dimensions:
                  - name: status
                    type: categorical
                measures:
                  - name: revenue
                    agg: sum
                    expr: amount
            metrics:
              - name: completed_revenue
                type: simple
                label: Completed Revenue
                type_params:
                  measure: revenue
                filter: "{{ Dimension('status') }} = 'completed'"
        """)
        parsed = parse_dbt_yaml(yaml_str)
        assert parsed.metrics[0].filter is not None
        result = map_dbt_to_tessallite(parsed)
        persona = result.bundle["models"][0]["personas"][0]
        # F-020-16: filters now land in the REAL persona default_filters dict
        # shape ({dim_name: value}) the query router reads — not a junk list of
        # {source_metric, filter_sql} the platform ignored.
        df = persona["default_filters"]
        assert isinstance(df, dict)
        assert df["status"] == "completed"

    def test_metric_without_filter_not_mapped(self):
        parsed = parse_dbt_yaml(SAMPLE_DBT_YAML)
        result = map_dbt_to_tessallite(parsed)
        persona = result.bundle["models"][0]["personas"][0]
        assert persona.get("default_filters") is None or len(persona.get("default_filters", [])) == 0


class TestAggTimeDimension:
    def test_agg_time_dimension_parsed(self):
        yaml_str = textwrap.dedent("""\
            semantic_models:
              - name: orders
                model: ref('orders')
                dimensions:
                  - name: order_date
                    type: time
                    type_params:
                      time_granularity: day
                measures:
                  - name: revenue
                    agg: sum
                    expr: amount
                    agg_time_dimension: order_date
        """)
        parsed = parse_dbt_yaml(yaml_str)
        assert parsed.semantic_models[0].measures[0].agg_time_dimension == "order_date"


class TestProjectWithSavedQueries:
    def test_saved_query_only_file_preserved(self):
        """A YAML file containing only saved_queries must not be dropped."""
        semantic_yaml = textwrap.dedent("""\
            semantic_models:
              - name: orders
                model: ref('orders')
                measures:
                  - name: revenue
                    agg: sum
        """)
        sq_yaml = textwrap.dedent("""\
            saved_queries:
              - name: top_orders
                query_params:
                  metrics: [revenue]
                  group_by: [Dimension('status')]
        """)
        files = {
            "models/semantic.yml": semantic_yaml,
            "models/saved_queries.yml": sq_yaml,
        }
        result = parse_dbt_project(files)
        assert len(result.saved_queries) == 1
        assert result.saved_queries[0].name == "top_orders"

    def test_saved_queries_merged_across_files(self):
        orders_yaml = textwrap.dedent("""\
            semantic_models:
              - name: orders
                model: ref('orders')
                measures:
                  - name: revenue
                    agg: sum
            saved_queries:
              - name: sq_one
                query_params:
                  metrics: [revenue]
        """)
        metrics_yaml = textwrap.dedent("""\
            semantic_models:
              - name: customers
                model: ref('customers')
                measures:
                  - name: cust_count
                    agg: count
            saved_queries:
              - name: sq_two
                query_params:
                  metrics: [cust_count]
        """)
        files = {
            "models/orders.yml": orders_yaml,
            "models/customers.yml": metrics_yaml,
        }
        result = parse_dbt_project(files)
        assert len(result.saved_queries) == 2
        names = {sq.name for sq in result.saved_queries}
        assert names == {"sq_one", "sq_two"}


class TestFixtureFiles:
    """Parse real-world dbt fixture files to verify coverage."""

    @pytest.mark.parametrize("fixture_name", [
        "jaffle_shop.yml",
        "acme_payments.yml",
        "saas_subscriptions.yml",
        "powerbi_semantic_layer.yml",
        "community_ecommerce.yml",
    ])
    def test_fixture_parses_without_errors(self, fixture_name):
        fixture_path = FIXTURES_DIR / fixture_name
        content = fixture_path.read_text(encoding="utf-8")
        result = parse_dbt_yaml(content)
        assert len(result.semantic_models) >= 1
        assert result.errors == []

    @pytest.mark.parametrize("fixture_name", [
        "jaffle_shop.yml",
        "acme_payments.yml",
        "saas_subscriptions.yml",
        "powerbi_semantic_layer.yml",
        "community_ecommerce.yml",
    ])
    def test_fixture_maps_to_valid_bundle(self, fixture_name):
        fixture_path = FIXTURES_DIR / fixture_name
        content = fixture_path.read_text(encoding="utf-8")
        parsed = parse_dbt_yaml(content)
        result = map_dbt_to_tessallite(parsed)
        bundle = result.bundle
        assert bundle["export_format"] == "tessallite-project/v1"
        assert len(bundle["models"]) >= 1
        for model in bundle["models"]:
            assert model["model"]["slug"]
            assert model["model"]["display_name"]
            assert len(model["measures"]) >= 1 or len(model["dimensions"]) >= 1

    def test_jaffle_shop_multi_model(self):
        content = (FIXTURES_DIR / "jaffle_shop.yml").read_text(encoding="utf-8")
        parsed = parse_dbt_yaml(content)
        assert len(parsed.semantic_models) == 3
        names = {sm.name for sm in parsed.semantic_models}
        assert names == {"orders", "order_items", "customers"}
        assert len(parsed.metrics) >= 10

    def test_jaffle_shop_all_metric_types_parsed(self):
        content = (FIXTURES_DIR / "jaffle_shop.yml").read_text(encoding="utf-8")
        parsed = parse_dbt_yaml(content)
        metric_types = {m.metric_type for m in parsed.metrics}
        assert "simple" in metric_types
        assert "derived" in metric_types
        assert "cumulative" in metric_types
        assert "ratio" in metric_types

    def test_jaffle_shop_filter_parsed(self):
        content = (FIXTURES_DIR / "jaffle_shop.yml").read_text(encoding="utf-8")
        parsed = parse_dbt_yaml(content)
        food_orders = next(m for m in parsed.metrics if m.name == "food_orders")
        assert food_orders.filter is not None
        assert "is_food_order" in food_orders.filter

    def test_jaffle_shop_map_produces_warnings_for_complex_metrics(self):
        content = (FIXTURES_DIR / "jaffle_shop.yml").read_text(encoding="utf-8")
        parsed = parse_dbt_yaml(content)
        result = map_dbt_to_tessallite(parsed)
        has_derived_warning = any("derived" in w.lower() or "Derived" in w for w in result.warnings)
        has_cumulative_warning = any("cumulative" in w.lower() or "Cumulative" in w for w in result.warnings)
        has_ratio_warning = any("ratio" in w.lower() or "Ratio" in w for w in result.warnings)
        assert has_derived_warning
        assert has_cumulative_warning
        assert has_ratio_warning

    def test_powerbi_semantic_layer_multi_model_and_saved_queries(self):
        content = (FIXTURES_DIR / "powerbi_semantic_layer.yml").read_text(encoding="utf-8")
        parsed = parse_dbt_yaml(content)
        assert len(parsed.semantic_models) == 2
        names = {sm.name for sm in parsed.semantic_models}
        assert names == {"fct_sales", "dim_products"}
        assert len(parsed.metrics) == 6
        metric_types = {m.metric_type for m in parsed.metrics}
        assert metric_types == {"simple", "derived", "ratio", "cumulative"}
        assert len(parsed.saved_queries) == 1
        assert parsed.saved_queries[0].name == "daily_sales_summary"
        assert len(parsed.saved_queries[0].exports) == 1

    def test_powerbi_labels_mapped(self):
        content = (FIXTURES_DIR / "powerbi_semantic_layer.yml").read_text(encoding="utf-8")
        parsed = parse_dbt_yaml(content)
        result = map_dbt_to_tessallite(parsed)
        model = next(
            m for m in result.bundle["models"]
            if m["model"]["slug"] == "fct_sales"
        )
        assert model["model"]["display_name"] == "Sales Transactions"
        revenue = next(m for m in model["measures"] if m["name"] == "revenue")
        assert revenue["display_name"] == "Total Revenue"

    def test_community_ecommerce_conversion_metric_warned(self):
        content = (FIXTURES_DIR / "community_ecommerce.yml").read_text(encoding="utf-8")
        parsed = parse_dbt_yaml(content)
        assert len(parsed.semantic_models) == 2
        conversion = next(m for m in parsed.metrics if m.name == "conversion_rate")
        assert conversion.metric_type == "conversion"
        result = map_dbt_to_tessallite(parsed)
        conversion_warnings = [w for w in result.warnings if "conversion" in w.lower()]
        assert len(conversion_warnings) >= 1

    def test_community_ecommerce_list_filter_parsed(self):
        content = (FIXTURES_DIR / "community_ecommerce.yml").read_text(encoding="utf-8")
        parsed = parse_dbt_yaml(content)
        completed = next(m for m in parsed.metrics if m.name == "completed_orders")
        assert completed.filter is not None
        assert "delivered" in completed.filter

    def test_community_ecommerce_non_additive_dimension(self):
        content = (FIXTURES_DIR / "community_ecommerce.yml").read_text(encoding="utf-8")
        parsed = parse_dbt_yaml(content)
        orders_sm = next(sm for sm in parsed.semantic_models if sm.name == "orders")
        latest = next(m for m in orders_sm.measures if m.name == "latest_order_amount")
        assert latest.non_additive_dimension is not None
        assert latest.non_additive_dimension["name"] == "order_date"


def test_sum_boolean_imported_as_disabled_not_count():
    # F-020-08: sum_boolean counts only TRUE rows; mapping to plain "count"
    # counts ALL rows (silently wrong). It must import disabled with a warning.
    yaml_str = textwrap.dedent("""\
        semantic_models:
          - name: flags
            model: ref('flag_table')
            dimensions:
              - name: region
                type: categorical
            measures:
              - name: active_count
                agg: sum_boolean
                expr: is_active
    """)
    parsed = parse_dbt_yaml(yaml_str)
    result = map_dbt_to_tessallite(parsed)
    model = result.bundle["models"][0]
    m = next(x for x in model["measures"] if x["name"] == "active_count")
    assert m["is_invalid"] is True
    assert any("sum_boolean" in w for w in result.warnings)


def test_supported_dbt_agg_stays_active():
    yaml_str = textwrap.dedent("""\
        semantic_models:
          - name: sales
            model: ref('sales_table')
            measures:
              - name: total
                agg: sum
                expr: amount
    """)
    parsed = parse_dbt_yaml(yaml_str)
    result = map_dbt_to_tessallite(parsed)
    model = result.bundle["models"][0]
    m = next(x for x in model["measures"] if x["name"] == "total")
    assert m["default_agg"] == "sum"
    assert m["is_invalid"] is False
