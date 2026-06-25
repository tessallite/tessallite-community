"""Tests for Cube.dev YAML parser and mapper."""
import textwrap

import pytest

from shared.importers.cube_parser import CubeParseError, parse_cube_yaml, parse_cube_project
from shared.importers.cube_mapper import map_cube_to_tessallite


SAMPLE_CUBE_YAML = textwrap.dedent("""\
    cubes:
      - name: orders
        sql_table: public.orders
        title: Orders
        description: One row per order
        measures:
          - name: count
            type: count
          - name: total_amount
            sql: amount
            type: sum
            title: Total Amount
            description: Sum of order amounts
            format: currency
          - name: avg_amount
            sql: amount
            type: avg
        dimensions:
          - name: id
            sql: id
            type: number
            primary_key: true
          - name: status
            sql: status
            type: string
            title: Order Status
          - name: created_at
            sql: created_at
            type: time
            title: Created At
        joins:
          - name: users
            sql: "{CUBE}.user_id = {users}.id"
            relationship: many_to_one
        segments:
          - name: completed
            sql: "{CUBE}.status = 'completed'"
            title: Completed Orders
        hierarchies:
          - name: time_hierarchy
            title: Time
            levels:
              - created_at
""")


class TestCubeParser:
    def test_parse_basic_yaml(self):
        result = parse_cube_yaml(SAMPLE_CUBE_YAML)
        assert len(result.cubes) == 1
        cube = result.cubes[0]
        assert cube.name == "orders"
        assert cube.sql_table == "public.orders"
        assert cube.title == "Orders"

    def test_measures_parsed(self):
        result = parse_cube_yaml(SAMPLE_CUBE_YAML)
        cube = result.cubes[0]
        assert len(cube.measures) == 3
        m_names = {m.name for m in cube.measures}
        assert m_names == {"count", "total_amount", "avg_amount"}
        total = next(m for m in cube.measures if m.name == "total_amount")
        assert total.measure_type == "sum"
        assert total.format == "currency"

    def test_dimensions_parsed(self):
        result = parse_cube_yaml(SAMPLE_CUBE_YAML)
        cube = result.cubes[0]
        assert len(cube.dimensions) == 3
        status = next(d for d in cube.dimensions if d.name == "status")
        assert status.dim_type == "string"
        assert status.title == "Order Status"
        pk = next(d for d in cube.dimensions if d.name == "id")
        assert pk.primary_key is True

    def test_joins_parsed(self):
        result = parse_cube_yaml(SAMPLE_CUBE_YAML)
        cube = result.cubes[0]
        assert len(cube.joins) == 1
        assert cube.joins[0].name == "users"
        assert cube.joins[0].relationship == "many_to_one"

    def test_segments_parsed(self):
        result = parse_cube_yaml(SAMPLE_CUBE_YAML)
        cube = result.cubes[0]
        assert len(cube.segments) == 1
        assert cube.segments[0].name == "completed"

    def test_hierarchies_parsed(self):
        result = parse_cube_yaml(SAMPLE_CUBE_YAML)
        cube = result.cubes[0]
        assert len(cube.hierarchies) == 1
        assert cube.hierarchies[0].levels == ["created_at"]

    def test_empty_yaml_raises(self):
        with pytest.raises(CubeParseError):
            parse_cube_yaml("foo: bar")

    def test_missing_name_raises(self):
        yaml_str = textwrap.dedent("""\
            cubes:
              - sql_table: test
        """)
        with pytest.raises(CubeParseError):
            parse_cube_yaml(yaml_str)

    def test_rolling_window_parsed(self):
        yaml_str = textwrap.dedent("""\
            cubes:
              - name: orders
                sql_table: orders
                measures:
                  - name: rolling_count
                    type: count
                    rolling_window:
                      trailing: 7 day
                      offset: end
        """)
        result = parse_cube_yaml(yaml_str)
        m = result.cubes[0].measures[0]
        assert m.rolling_window is not None
        assert m.rolling_window["trailing"] == "7 day"

    def test_private_cube_dimension_measure(self):
        yaml_str = textwrap.dedent("""\
            cubes:
              - name: internal
                sql_table: internal
                public: false
                measures:
                  - name: count
                    type: count
                    public: false
                dimensions:
                  - name: id
                    sql: id
                    type: number
                    public: false
        """)
        result = parse_cube_yaml(yaml_str)
        assert result.cubes[0].public is False
        assert result.cubes[0].measures[0].public is False
        assert result.cubes[0].dimensions[0].public is False


class TestCubeMapper:
    def test_map_produces_valid_bundle(self):
        parsed = parse_cube_yaml(SAMPLE_CUBE_YAML)
        result = map_cube_to_tessallite(parsed)
        bundle = result.bundle
        assert bundle["export_format"] == "tessallite-project/v1"
        assert len(bundle["models"]) == 1

    def test_model_display_name_from_title(self):
        parsed = parse_cube_yaml(SAMPLE_CUBE_YAML)
        result = map_cube_to_tessallite(parsed)
        model = result.bundle["models"][0]
        assert model["model"]["display_name"] == "Orders"
        assert model["model"]["slug"] == "orders"

    def test_measures_mapped(self):
        parsed = parse_cube_yaml(SAMPLE_CUBE_YAML)
        result = map_cube_to_tessallite(parsed)
        model = result.bundle["models"][0]
        assert len(model["measures"]) == 3
        total = next(m for m in model["measures"] if m["name"] == "total_amount")
        assert total["default_agg"] == "sum"
        assert total["display_name"] == "Total Amount"
        assert total["format"] == "currency"
        avg_m = next(m for m in model["measures"] if m["name"] == "avg_amount")
        assert avg_m["default_agg"] == "average"

    def test_dimensions_mapped(self):
        parsed = parse_cube_yaml(SAMPLE_CUBE_YAML)
        result = map_cube_to_tessallite(parsed)
        model = result.bundle["models"][0]
        assert len(model["dimensions"]) == 3
        status = next(d for d in model["dimensions"] if d["name"] == "status")
        assert status["display_name"] == "Order Status"
        time_dim = next(d for d in model["dimensions"] if d["name"] == "created_at")
        assert time_dim["is_time_dim"] is True

    def test_joins_dropped_with_warning(self):
        # F-020-07: Cube joins reference other cubes (which import as separate
        # models) and cannot be represented as intra-model joins. They are
        # dropped and a warning is emitted per join, instead of producing a
        # Join row with a `to_cube` column the ORM rejects (which crashed
        # every import of a cube with joins).
        parsed = parse_cube_yaml(SAMPLE_CUBE_YAML)
        result = map_cube_to_tessallite(parsed)
        model = result.bundle["models"][0]
        assert model["joins"] == []
        join_warnings = [w for w in result.warnings if "join to 'users'" in w]
        assert len(join_warnings) == 1

    def test_hierarchies_mapped(self):
        parsed = parse_cube_yaml(SAMPLE_CUBE_YAML)
        result = map_cube_to_tessallite(parsed)
        model = result.bundle["models"][0]
        assert len(model["hierarchies"]) >= 1
        named_hier = next(
            h for h in model["hierarchies"] if h["name"] == "time_hierarchy"
        )
        assert named_hier["type"] == "explicit"
        assert named_hier["levels"][0]["name"] == "created_at"

    def test_default_persona_created(self):
        parsed = parse_cube_yaml(SAMPLE_CUBE_YAML)
        result = map_cube_to_tessallite(parsed)
        model = result.bundle["models"][0]
        assert len(model["personas"]) == 1
        assert model["personas"][0]["slug"] == "everyone"

    def test_private_cube_skipped(self):
        yaml_str = textwrap.dedent("""\
            cubes:
              - name: public_cube
                sql_table: t1
                measures:
                  - name: count
                    type: count
              - name: private_cube
                sql_table: t2
                public: false
                measures:
                  - name: count
                    type: count
        """)
        parsed = parse_cube_yaml(yaml_str)
        result = map_cube_to_tessallite(parsed)
        assert len(result.bundle["models"]) == 1
        assert result.bundle["models"][0]["model"]["slug"] == "public_cube"

    def test_rolling_window_produces_warning(self):
        yaml_str = textwrap.dedent("""\
            cubes:
              - name: orders
                sql_table: orders
                measures:
                  - name: rolling_count
                    type: count
                    rolling_window:
                      trailing: 7 day
        """)
        parsed = parse_cube_yaml(yaml_str)
        result = map_cube_to_tessallite(parsed)
        rolling_warnings = [w for w in result.warnings if "rolling_window" in w]
        assert len(rolling_warnings) == 1
        m = result.bundle["models"][0]["measures"][0]
        assert m["measure_type"] == "calculated"

    def test_private_dimensions_excluded(self):
        yaml_str = textwrap.dedent("""\
            cubes:
              - name: test
                sql_table: test
                measures:
                  - name: count
                    type: count
                dimensions:
                  - name: visible
                    sql: visible
                    type: string
                  - name: hidden
                    sql: hidden
                    type: string
                    public: false
        """)
        parsed = parse_cube_yaml(yaml_str)
        result = map_cube_to_tessallite(parsed)
        model = result.bundle["models"][0]
        dim_names = {d["name"] for d in model["dimensions"]}
        assert "visible" in dim_names
        assert "hidden" not in dim_names


class TestCubeProject:
    def test_multi_file_project(self):
        orders_yaml = textwrap.dedent("""\
            cubes:
              - name: orders
                sql_table: orders
                measures:
                  - name: count
                    type: count
        """)
        users_yaml = textwrap.dedent("""\
            cubes:
              - name: users
                sql_table: users
                measures:
                  - name: count
                    type: count
                dimensions:
                  - name: email
                    sql: email
                    type: string
        """)
        files = {
            "schema/orders.yml": orders_yaml,
            "schema/users.yml": users_yaml,
        }
        result = parse_cube_project(files)
        assert len(result.cubes) == 2

        mapped = map_cube_to_tessallite(result)
        assert len(mapped.bundle["models"]) == 2


class TestCubePhysicalColumnBinding:
    def test_measure_sql_used_as_physical_column(self):
        yaml_str = textwrap.dedent("""\
            cubes:
              - name: orders
                sql_table: orders
                measures:
                  - name: total_amount
                    sql: amount
                    type: sum
        """)
        parsed = parse_cube_yaml(yaml_str)
        result = map_cube_to_tessallite(parsed)
        model = result.bundle["models"][0]
        col_names = {c["column_name"] for c in model["columns"]}
        assert "amount" in col_names
        assert "total_amount" not in col_names
        amount_col = next(c for c in model["columns"] if c["column_name"] == "amount")
        m = model["measures"][0]
        assert m["source_column_id"] == amount_col["id"]

    def test_dimension_sql_used_as_physical_column(self):
        yaml_str = textwrap.dedent("""\
            cubes:
              - name: orders
                sql_table: orders
                measures:
                  - name: count
                    type: count
                dimensions:
                  - name: order_status
                    sql: status
                    type: string
        """)
        parsed = parse_cube_yaml(yaml_str)
        result = map_cube_to_tessallite(parsed)
        model = result.bundle["models"][0]
        col_names = {c["column_name"] for c in model["columns"]}
        assert "status" in col_names
        assert "order_status" not in col_names
        status_col = next(c for c in model["columns"] if c["column_name"] == "status")
        d = next(d for d in model["dimensions"] if d["name"] == "order_status")
        assert d["source_column_id"] == status_col["id"]

    def test_expression_sql_warns_and_keeps_logical_name(self):
        yaml_str = textwrap.dedent("""\
            cubes:
              - name: orders
                sql_table: orders
                measures:
                  - name: profit
                    sql: "{CUBE}.revenue - {CUBE}.cost"
                    type: number
        """)
        parsed = parse_cube_yaml(yaml_str)
        result = map_cube_to_tessallite(parsed)
        expr_warnings = [w for w in result.warnings if "SQL expression" in w]
        assert len(expr_warnings) == 1
        model = result.bundle["models"][0]
        col_names = {c["column_name"] for c in model["columns"]}
        assert "profit" in col_names

    def test_cube_macro_sql_extracts_column_name(self):
        yaml_str = textwrap.dedent("""\
            cubes:
              - name: orders
                sql_table: orders
                measures:
                  - name: total_amount
                    sql: "{CUBE}.amount"
                    type: sum
                dimensions:
                  - name: order_status
                    sql: "{orders}.status"
                    type: string
        """)
        parsed = parse_cube_yaml(yaml_str)
        result = map_cube_to_tessallite(parsed)
        model = result.bundle["models"][0]
        col_names = {c["column_name"] for c in model["columns"]}
        assert "amount" in col_names
        assert "status" in col_names
        expr_warnings = [w for w in result.warnings if "SQL expression" in w]
        assert len(expr_warnings) == 0

    def test_empty_sql_falls_back_to_logical_name(self):
        yaml_str = textwrap.dedent("""\
            cubes:
              - name: orders
                sql_table: orders
                measures:
                  - name: count
                    type: count
        """)
        parsed = parse_cube_yaml(yaml_str)
        result = map_cube_to_tessallite(parsed)
        model = result.bundle["models"][0]
        col_names = {c["column_name"] for c in model["columns"]}
        assert "count" in col_names


class TestCubeAutoTimeHierarchy:
    def test_auto_time_hierarchy_levels_have_unique_uda_keys(self):
        yaml_str = textwrap.dedent("""\
            cubes:
              - name: orders
                sql_table: orders
                measures:
                  - name: count
                    type: count
                dimensions:
                  - name: order_date
                    sql: order_date
                    type: time
        """)
        parsed = parse_cube_yaml(yaml_str)
        result = map_cube_to_tessallite(parsed)
        model = result.bundle["models"][0]
        hier = next(h for h in model["hierarchies"] if h["type"] == "date_embedded")
        key_ids = [l["key_attribute_id"] for l in hier["levels"]]
        assert len(key_ids) == len(set(key_ids))
        for lvl in hier["levels"]:
            assert lvl["key_attribute_source"] == "user_defined_attribute"

    def test_auto_time_hierarchy_generates_udas(self):
        yaml_str = textwrap.dedent("""\
            cubes:
              - name: orders
                sql_table: orders
                measures:
                  - name: count
                    type: count
                dimensions:
                  - name: created_at
                    sql: created_at
                    type: time
        """)
        parsed = parse_cube_yaml(yaml_str)
        result = map_cube_to_tessallite(parsed)
        model = result.bundle["models"][0]
        udas = model["user_defined_attributes"]
        assert len(udas) == 4
        uda_names = {u["name"] for u in udas}
        assert "created_at_year" in uda_names
        assert "created_at_day" in uda_names
        year_uda = next(u for u in udas if u["name"] == "created_at_year")
        assert "EXTRACT(YEAR" in year_uda["expression"]

    def test_auto_time_hierarchy_generates_uda_column_refs(self):
        yaml_str = textwrap.dedent("""\
            cubes:
              - name: orders
                sql_table: orders
                measures:
                  - name: count
                    type: count
                dimensions:
                  - name: created_at
                    sql: created_at
                    type: time
        """)
        parsed = parse_cube_yaml(yaml_str)
        result = map_cube_to_tessallite(parsed)
        model = result.bundle["models"][0]
        refs = model["uda_column_refs"]
        assert len(refs) == 4
        uda_ids = {u["id"] for u in model["user_defined_attributes"]}
        for ref in refs:
            assert ref["attribute_id"] in uda_ids

    def test_explicit_hierarchy_skips_auto_time(self):
        parsed = parse_cube_yaml(SAMPLE_CUBE_YAML)
        result = map_cube_to_tessallite(parsed)
        model = result.bundle["models"][0]
        date_embedded = [h for h in model["hierarchies"] if h["type"] == "date_embedded"]
        assert len(date_embedded) == 0

    def test_auto_time_hierarchy_udas_not_marked_validated(self):
        yaml_str = textwrap.dedent("""\
            cubes:
              - name: orders
                sql_table: orders
                measures:
                  - name: count
                    type: count
                dimensions:
                  - name: created_at
                    sql: created_at
                    type: time
        """)
        parsed = parse_cube_yaml(yaml_str)
        result = map_cube_to_tessallite(parsed)
        model = result.bundle["models"][0]
        for uda in model["user_defined_attributes"]:
            assert uda["validated"] is False


class TestCubeExplicitHierarchyValidation:
    def test_unknown_level_skipped_with_warning(self):
        yaml_str = textwrap.dedent("""\
            cubes:
              - name: orders
                sql_table: orders
                measures:
                  - name: count
                    type: count
                dimensions:
                  - name: status
                    sql: status
                    type: string
                hierarchies:
                  - name: status_hier
                    levels:
                      - status
                      - nonexistent_dim
        """)
        parsed = parse_cube_yaml(yaml_str)
        result = map_cube_to_tessallite(parsed)
        model = result.bundle["models"][0]
        hier = model["hierarchies"][0]
        level_names = [l["name"] for l in hier["levels"]]
        assert "status" in level_names
        assert "nonexistent_dim" not in level_names
        for lvl in hier["levels"]:
            assert lvl["key_attribute_id"] is not None
        skip_warnings = [w for w in result.warnings if "skipped levels" in w]
        assert len(skip_warnings) == 1

    def test_private_dimension_level_skipped(self):
        yaml_str = textwrap.dedent("""\
            cubes:
              - name: orders
                sql_table: orders
                measures:
                  - name: count
                    type: count
                dimensions:
                  - name: status
                    sql: status
                    type: string
                  - name: hidden
                    sql: hidden
                    type: string
                    public: false
                hierarchies:
                  - name: combo
                    levels:
                      - status
                      - hidden
        """)
        parsed = parse_cube_yaml(yaml_str)
        result = map_cube_to_tessallite(parsed)
        model = result.bundle["models"][0]
        hier = model["hierarchies"][0]
        level_names = [l["name"] for l in hier["levels"]]
        assert "status" in level_names
        assert "hidden" not in level_names

    def test_all_levels_invalid_omits_hierarchy(self):
        yaml_str = textwrap.dedent("""\
            cubes:
              - name: orders
                sql_table: orders
                measures:
                  - name: count
                    type: count
                dimensions:
                  - name: status
                    sql: status
                    type: string
                hierarchies:
                  - name: bad_hier
                    levels:
                      - missing_a
                      - missing_b
        """)
        parsed = parse_cube_yaml(yaml_str)
        result = map_cube_to_tessallite(parsed)
        model = result.bundle["models"][0]
        assert len(model["hierarchies"]) == 0


class TestCubeMeasureTypes:
    @pytest.mark.parametrize("cube_type,expected_agg", [
        ("count", "count"),
        ("count_distinct", "count_distinct"),
        ("count_distinct_approx", "count_distinct"),
        ("sum", "sum"),
        ("avg", "average"),
        ("min", "min"),
        ("max", "max"),
        ("number", "sum"),
        ("running_total", "sum"),
    ])
    def test_measure_type_mapping(self, cube_type, expected_agg):
        yaml_str = textwrap.dedent(f"""\
            cubes:
              - name: test
                sql_table: test
                measures:
                  - name: m
                    sql: col
                    type: {cube_type}
        """)
        parsed = parse_cube_yaml(yaml_str)
        result = map_cube_to_tessallite(parsed)
        m = result.bundle["models"][0]["measures"][0]
        assert m["default_agg"] == expected_agg


class TestCubeUnrepresentableMeasures:
    """F-020-08: running_total / number measures must import disabled, not as
    a silently-wrong flat SUM."""

    def test_running_total_imported_as_disabled(self):
        yaml_str = textwrap.dedent("""\
            cubes:
              - name: orders
                sql_table: orders
                measures:
                  - name: cumulative_revenue
                    sql: revenue
                    type: running_total
        """)
        parsed = parse_cube_yaml(yaml_str)
        result = map_cube_to_tessallite(parsed)
        model = result.bundle["models"][0]
        m = next(x for x in model["measures"] if x["name"] == "cumulative_revenue")
        assert m["is_invalid"] is True
        assert any("running_total" in w for w in result.warnings)

    def test_supported_cube_measure_stays_active(self):
        yaml_str = textwrap.dedent("""\
            cubes:
              - name: orders
                sql_table: orders
                measures:
                  - name: total_revenue
                    sql: revenue
                    type: sum
        """)
        parsed = parse_cube_yaml(yaml_str)
        result = map_cube_to_tessallite(parsed)
        model = result.bundle["models"][0]
        m = next(x for x in model["measures"] if x["name"] == "total_revenue")
        assert m["default_agg"] == "sum"
        assert m["is_invalid"] is False
