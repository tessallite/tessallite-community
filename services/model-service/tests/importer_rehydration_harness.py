"""Shared cases for importer-to-rehydrator integration coverage (F-020-E2).

Each case turns an ecosystem importer (dbt, Cube, AtScale, native YAML, data
catalog) through its parser+mapper and yields a model snapshot. The DB-backed
test in ``tests/integration/test_importer_rehydration_harness.py`` then runs
every snapshot through the real ``prepare_snapshot_for_import`` +
``rehydrate_into_live`` path against a throwaway Postgres schema, proving the
importers keep emitting ORM-aligned, rehydratable snapshots.

After the B14/H23 import/export remediation (F-020-02/03/04/07 fixed) all five
importers rehydrate cleanly, so no case carries an ``expected_failure`` marker.
"""
from __future__ import annotations

import textwrap
from dataclasses import dataclass
from typing import Callable

from shared.importers.atscale_mapper import map_atscale_to_tessallite
from shared.importers.atscale_parser import parse_sml_project
from shared.importers.cube_mapper import map_cube_to_tessallite
from shared.importers.cube_parser import parse_cube_yaml
from shared.importers.dbt_mapper import map_dbt_to_tessallite
from shared.importers.dbt_parser import parse_dbt_yaml
from shared.model_snapshot.yaml_deserialiser import parse_model_yaml
from src.api.catalog_import import _catalog_to_bundle


@dataclass(frozen=True)
class ImporterRehydrationCase:
    name: str
    build_snapshot: Callable[[], dict]
    inject_project_connection: bool = True


def importer_rehydration_cases() -> list[ImporterRehydrationCase]:
    """Return the importer snapshots that must stay DB-rehydratable."""
    return [
        ImporterRehydrationCase("dbt", _build_dbt_snapshot),
        ImporterRehydrationCase("cube-with-join", _build_cube_snapshot),
        ImporterRehydrationCase("atscale", _build_atscale_snapshot),
        ImporterRehydrationCase("yaml-roundtrip", _build_yaml_snapshot),
        ImporterRehydrationCase("catalog", _build_catalog_snapshot),
    ]


def _build_dbt_snapshot() -> dict:
    parsed = parse_dbt_yaml(
        textwrap.dedent(
            """\
            semantic_models:
              - name: orders
                model: ref('stg_orders')
                dimensions:
                  - name: order_date
                    type: time
                    type_params:
                      time_granularity: day
                  - name: status
                    type: categorical
                measures:
                  - name: order_total
                    agg: sum
                    expr: amount
                  - name: order_count
                    agg: count
            """
        )
    )
    return map_dbt_to_tessallite(parsed).bundle["models"][0]


def _build_cube_snapshot() -> dict:
    parsed = parse_cube_yaml(
        textwrap.dedent(
            """\
            cubes:
              - name: orders
                sql_table: public.orders
                measures:
                  - name: count
                    type: count
                  - name: total_amount
                    sql: amount
                    type: sum
                dimensions:
                  - name: id
                    sql: id
                    type: number
                    primary_key: true
                  - name: user_id
                    sql: user_id
                    type: number
                  - name: created_at
                    sql: created_at
                    type: time
                joins:
                  - name: users
                    sql: "{CUBE}.user_id = {users}.id"
                    relationship: many_to_one
            """
        )
    )
    return map_cube_to_tessallite(parsed).bundle["models"][0]


def _build_atscale_snapshot() -> dict:
    files = {
        "datasets/fact_orders.yml": textwrap.dedent(
            """\
            unique_name: fact_orders
            object_type: dataset
            label: fact_orders
            table: fact_orders
            columns:
              - name: order_id
                data_type: long
              - name: customer_id
                data_type: long
              - name: amount
                data_type: "decimal(18,2)"
            """
        ),
        "datasets/dim_customer.yml": textwrap.dedent(
            """\
            unique_name: dim_customer
            object_type: dataset
            label: dim_customer
            table: dim_customer
            columns:
              - name: customer_id
                data_type: long
              - name: customer_name
                data_type: string
            """
        ),
        "dimensions/customer.yml": textwrap.dedent(
            """\
            unique_name: Customer
            object_type: dimension
            label: Customer
            hierarchies:
              - unique_name: Customer Hierarchy
                levels:
                  - unique_name: Customer
            level_attributes:
              - unique_name: Customer
                dataset: dim_customer
                name_column: customer_name
                key_columns:
                  - customer_id
            """
        ),
        "metrics/total_amount.yml": textwrap.dedent(
            """\
            unique_name: total_amount
            object_type: metric
            label: Total Amount
            calculation_method: sum
            dataset: fact_orders
            column: amount
            """
        ),
        "models/orders.yml": textwrap.dedent(
            """\
            unique_name: Orders
            object_type: model
            label: Orders
            relationships:
              - unique_name: orders_customer
                from:
                  dataset: fact_orders
                  join_columns:
                    - customer_id
                to:
                  dimension: Customer
                  level: Customer
            metrics:
              - unique_name: total_amount
            """
        ),
    }
    return map_atscale_to_tessallite(parse_sml_project(files)).bundle["models"][0]


def _build_yaml_snapshot() -> dict:
    return parse_model_yaml(
        textwrap.dedent(
            """\
            model:
              name: sales
              display_name: Sales
            tables:
              - name: orders
                source_table: public.orders
                role: fact
              - name: customers
                source_table: public.customers
                role: dim_detail
            joins:
              - left: orders
                right: customers
                "on": orders.customer_id = customers.customer_id
                type: many-to-one
            dimensions:
              - name: customer_name
                table: customers
                column: customer_name
                type: text
            measures:
              - name: total_amount
                table: orders
                column: amount
                aggregation: sum
            hierarchies:
              - name: Customer
                type: explicit
                levels:
                  - name: Customer
                    column: customers.customer_name
            """
        )
    )


def _build_catalog_snapshot() -> dict:
    import uuid as _uuid

    bundle, _, _, _ = _catalog_to_bundle(
        [
            {
                "name": "orders",
                "description": "Orders from the upstream catalog",
                "fields": [
                    {"name": "customer_name", "data_type": "string"},
                    {"name": "amount", "data_type": "numeric"},
                ],
            }
        ],
        str(_uuid.uuid4()),
        "catalog-orders",
        "Catalog Orders",
    )
    return bundle["models"][0]
