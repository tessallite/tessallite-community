"""Cross-format consistency tests.

Verify that all three importers (dbt, AtScale, Cube) produce bundles
with the same schema structure so the project_rehydrator can consume
any of them identically.
"""
import textwrap

from shared.importers.dbt_parser import parse_dbt_yaml
from shared.importers.dbt_mapper import map_dbt_to_tessallite
from shared.importers.atscale_parser import parse_sml_project
from shared.importers.atscale_mapper import map_atscale_to_tessallite
from shared.importers.cube_parser import parse_cube_yaml
from shared.importers.cube_mapper import map_cube_to_tessallite


def _dbt_bundle():
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
    """)
    parsed = parse_dbt_yaml(yaml_str)
    return map_dbt_to_tessallite(parsed).bundle


def _atscale_bundle():
    files = {
        "models/test.yml": textwrap.dedent("""\
            unique_name: Orders
            object_type: model
            relationships:
              - unique_name: orders_dim
                from:
                  dataset: fact_orders
                  join_columns:
                    - status_id
                to:
                  dimension: Status Dimension
                  level: Status
            metrics:
              - unique_name: m_revenue
        """),
        "datasets/fact_orders.yml": textwrap.dedent("""\
            unique_name: fact_orders
            object_type: dataset
            table: fact_orders
            columns:
              - name: amount
                data_type: "decimal(18,2)"
              - name: status_id
                data_type: long
        """),
        "dimensions/Status.yml": textwrap.dedent("""\
            unique_name: Status Dimension
            object_type: dimension
            label: Status
            type: standard
            hierarchies:
              - unique_name: Status Hierarchy
                label: Status
                levels:
                  - unique_name: Status
            level_attributes:
              - unique_name: Status
                label: Status
                dataset: dim_status
                name_column: status_name
                key_columns:
                  - status_id
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
    return map_atscale_to_tessallite(parsed).bundle


def _cube_bundle():
    yaml_str = textwrap.dedent("""\
        cubes:
          - name: orders
            sql_table: orders
            measures:
              - name: revenue
                sql: amount
                type: sum
            dimensions:
              - name: status
                sql: status
                type: string
    """)
    parsed = parse_cube_yaml(yaml_str)
    return map_cube_to_tessallite(parsed).bundle


_REQUIRED_MODEL_KEYS = {
    "schema_version", "model_id", "model", "tables", "columns",
    "joins", "dimensions", "measures", "hierarchies", "personas",
    "user_defined_attributes", "uda_column_refs",
    "aggregates", "data_sources", "data_targets",
}

_REQUIRED_BUNDLE_KEYS = {
    "schema_version", "export_format", "project", "connections", "models",
}


class TestBundleSchemaConsistency:
    def test_all_formats_produce_same_bundle_keys(self):
        dbt = _dbt_bundle()
        atscale = _atscale_bundle()
        cube = _cube_bundle()
        for b in [dbt, atscale, cube]:
            assert set(b.keys()) == _REQUIRED_BUNDLE_KEYS

    def test_all_formats_produce_same_model_keys(self):
        dbt = _dbt_bundle()["models"][0]
        atscale = _atscale_bundle()["models"][0]
        cube = _cube_bundle()["models"][0]
        for m in [dbt, atscale, cube]:
            assert _REQUIRED_MODEL_KEYS.issubset(set(m.keys())), (
                f"Missing keys: {_REQUIRED_MODEL_KEYS - set(m.keys())}"
            )

    def test_all_formats_have_export_format(self):
        for b in [_dbt_bundle(), _atscale_bundle(), _cube_bundle()]:
            assert b["export_format"] == "tessallite-project/v1"

    def test_all_formats_have_personas(self):
        for b in [_dbt_bundle(), _atscale_bundle(), _cube_bundle()]:
            model = b["models"][0]
            assert len(model["personas"]) >= 1
            assert model["personas"][0]["slug"] == "everyone"

    def test_all_formats_have_measures(self):
        for b in [_dbt_bundle(), _atscale_bundle(), _cube_bundle()]:
            model = b["models"][0]
            assert len(model["measures"]) >= 1
            m = model["measures"][0]
            assert "name" in m
            assert "display_name" in m
            assert "default_agg" in m

    def test_all_formats_have_dimensions(self):
        dbt = _dbt_bundle()["models"][0]
        cube = _cube_bundle()["models"][0]
        for m in [dbt, cube]:
            assert len(m["dimensions"]) >= 1
            d = m["dimensions"][0]
            assert "name" in d
            assert "display_name" in d
            assert "is_time_dim" in d
