import pytest

from src.dax.mdx_execute import build_real_execute_response
from src.dax.mdschema import _rows_hierarchies, _rows_levels, _rows_md_properties, _rows_members
from src.dax.xmla_server import _mdx_to_sql, _statement_to_sql


from src.dax.mdschema import _rows_measures, _AGG_TO_XMLA


# ---------------------------------------------------------------------------
# MEASURE_AGGREGATOR mapping (D.1)
# ---------------------------------------------------------------------------

def test_measure_aggregator_sum():
    rows = _rows_measures("demo", [{"name": "Revenue", "default_agg": "sum"}])
    assert rows[0]["MEASURE_AGGREGATOR"] == "1"


def test_measure_aggregator_avg():
    rows = _rows_measures("demo", [{"name": "AvgPrice", "default_agg": "avg"}])
    assert rows[0]["MEASURE_AGGREGATOR"] == "5"


def test_measure_aggregator_count():
    rows = _rows_measures("demo", [{"name": "OrderCount", "default_agg": "count"}])
    assert rows[0]["MEASURE_AGGREGATOR"] == "2"


def test_measure_aggregator_min():
    rows = _rows_measures("demo", [{"name": "MinPrice", "default_agg": "min"}])
    assert rows[0]["MEASURE_AGGREGATOR"] == "3"


def test_measure_aggregator_max():
    rows = _rows_measures("demo", [{"name": "MaxPrice", "default_agg": "max"}])
    assert rows[0]["MEASURE_AGGREGATOR"] == "4"


def test_measure_aggregator_count_distinct():
    rows = _rows_measures("demo", [{"name": "UniqueCustomers", "default_agg": "count_distinct"}])
    assert rows[0]["MEASURE_AGGREGATOR"] == "127"


def test_measure_aggregator_defaults_to_sum():
    rows = _rows_measures("demo", [{"name": "X"}])
    assert rows[0]["MEASURE_AGGREGATOR"] == "1"


def test_measure_aggregator_mixed():
    measures = [
        {"name": "Revenue", "default_agg": "sum"},
        {"name": "AvgPrice", "default_agg": "avg"},
        {"name": "OrderCount", "default_agg": "count"},
    ]
    rows = _rows_measures("demo", measures)
    assert rows[0]["MEASURE_AGGREGATOR"] == "1"
    assert rows[1]["MEASURE_AGGREGATOR"] == "5"
    assert rows[2]["MEASURE_AGGREGATOR"] == "2"


# ---------------------------------------------------------------------------
# MDX translation with multiple measures / mixed aggregations (D.3)
# ---------------------------------------------------------------------------

def test_mdx_two_measures_different_aggs():
    mdx = """
    SELECT {[Measures].[Revenue], [Measures].[AvgPrice]} ON COLUMNS,
           {[Geography].[Geography].[(All)].Members} ON ROWS
    FROM [demo]
    """
    measures_meta = [
        {"name": "Revenue", "default_agg": "sum"},
        {"name": "AvgPrice", "default_agg": "avg"},
    ]
    dimensions_meta = [{"name": "Geography"}]

    sql, protocol = _mdx_to_sql(mdx, measures_meta, dimensions_meta)

    assert protocol == "jdbc"
    assert 'SUM("Revenue") AS "Revenue"' in sql
    assert 'AVG("AvgPrice") AS "AvgPrice"' in sql
    assert 'GROUP BY "Geography"' in sql


def test_mdx_three_measures_mixed_aggs():
    mdx = """
    SELECT {[Measures].[Revenue], [Measures].[OrderCount], [Measures].[MinPrice]} ON COLUMNS,
           {[Geography].[Geography].[(All)].Members} ON ROWS
    FROM [demo]
    """
    measures_meta = [
        {"name": "Revenue", "default_agg": "sum"},
        {"name": "OrderCount", "default_agg": "count"},
        {"name": "MinPrice", "default_agg": "min"},
    ]
    dimensions_meta = [{"name": "Geography"}]

    sql, protocol = _mdx_to_sql(mdx, measures_meta, dimensions_meta)

    assert protocol == "jdbc"
    assert 'SUM("Revenue") AS "Revenue"' in sql
    assert 'COUNT("OrderCount") AS "OrderCount"' in sql
    assert 'MIN("MinPrice") AS "MinPrice"' in sql


def test_mdx_count_distinct_measure():
    mdx = """
    SELECT {[Measures].[UniqueCustomers]} ON COLUMNS
    FROM [demo]
    """
    measures_meta = [{"name": "UniqueCustomers", "default_agg": "count_distinct"}]
    dimensions_meta = []

    sql, protocol = _mdx_to_sql(mdx, measures_meta, dimensions_meta)

    assert protocol == "jdbc"
    assert 'COUNT(DISTINCT "UniqueCustomers") AS "UniqueCustomers"' in sql


# ---------------------------------------------------------------------------
# Existing tests
# ---------------------------------------------------------------------------

def test_mdx_drilldown_keeps_dimensions():
    mdx = """
    SELECT DrilldownLevel({[Geography].[Geography].[(All)]}) ON ROWS,
           {[Measures].[Amount]} ON COLUMNS
    FROM [demo]
    """
    measures_meta = [{"name": "Amount", "default_agg": "sum"}]
    dimensions_meta = [{"name": "Geography"}]

    sql, protocol = _mdx_to_sql(mdx, measures_meta, dimensions_meta)

    assert protocol == "jdbc"
    assert "Geography" in sql
    assert 'GROUP BY "Geography"' in sql
    assert 'SUM("Amount")' in sql


def test_mdx_where_filter_translated():
    mdx = """
    SELECT {[Measures].[Amount]} ON COLUMNS
    FROM [demo]
    WHERE ([Geography].[Geography].[France])
    """
    measures_meta = [{"name": "Amount", "default_agg": "sum"}]
    dimensions_meta = [{"name": "Geography"}]

    sql, protocol = _mdx_to_sql(mdx, measures_meta, dimensions_meta)

    assert protocol == "jdbc"
    assert """WHERE "Geography" = 'France'""" in sql
    # Should still select measure without grouping since no dims on axes
    assert "GROUP BY" not in sql


def test_mdx_translation_ignores_extra_metadata_fields_from_model_service():
    """
    Regression guard:
    Model-service now returns extra UDA-related fields on dimensions/measures.
    Excel MDX translation must continue using only semantic name/default_agg
    and ignore unknown keys.
    """
    mdx = """
    SELECT DrilldownLevel({[account_type].[account_type].[(All)]}) ON ROWS,
           {[Measures].[base_amount]} ON COLUMNS
    FROM [demo]
    WHERE ([account_type].[account_type].[CURRENT])
    """
    measures_meta = [{
        "name": "base_amount",
        "default_agg": "sum",
        "user_defined_attribute_id": "uda-1",
        "user_defined_attribute_name": "fx_amount",
    }]
    dimensions_meta = [{
        "name": "account_type",
        "source_column_name": "account_type",
        "user_defined_attribute_id": None,
        "user_defined_attribute_name": None,
    }]

    sql, protocol = _mdx_to_sql(mdx, measures_meta, dimensions_meta)

    assert protocol == "jdbc"
    assert 'SUM("base_amount")' in sql
    assert 'GROUP BY "account_type"' in sql
    assert """WHERE "account_type" = 'CURRENT'""" in sql


def test_mdx_translation_supports_dimension_names_that_look_like_fx_attributes():
    """
    Regression guard:
    UDA names can look like regular dimension names (e.g. prefixed with fx_).
    Excel MDX axis extraction should still keep dimensions and group correctly.
    """
    mdx = """
    SELECT DrilldownLevel({[fx_account_type].[fx_account_type].[(All)]}) ON ROWS,
           {[Measures].[base_amount]} ON COLUMNS
    FROM [demo]
    """
    measures_meta = [{"name": "base_amount", "default_agg": "sum"}]
    dimensions_meta = [{"name": "fx_account_type"}]

    sql, protocol = _mdx_to_sql(mdx, measures_meta, dimensions_meta)

    assert protocol == "jdbc"
    assert 'SUM("base_amount")' in sql
    assert 'GROUP BY "fx_account_type"' in sql


def test_mdx_translation_maps_hierarchy_level_to_matching_semantic_dimension():
    mdx = """
    SELECT {[Measures].[Amount]} ON COLUMNS,
           {[GeoHierarchy].[GeoHierarchy].[Region].Members} ON ROWS
    FROM [demo]
    """
    measures_meta = [{"name": "Amount", "default_agg": "sum"}]
    dimensions_meta = [
        {"name": "region_dim", "source_column_id": "col-region"},
        {"name": "country_dim", "source_column_id": "col-country"},
    ]
    hierarchy_meta = [{
        "name": "GeoHierarchy",
        "levels": [
            {
                "ordinal": 0,
                "name": "Region",
                "key_attribute": {"id": "col-region", "source": "physical_column"},
            },
            {
                "ordinal": 1,
                "name": "Country",
                "key_attribute": {"id": "col-country", "source": "physical_column"},
            },
        ],
    }]

    sql, protocol = _mdx_to_sql(
        mdx,
        measures_meta,
        dimensions_meta,
        hierarchy_meta=hierarchy_meta,
    )

    assert protocol == "jdbc"
    assert 'SUM("Amount")' in sql
    assert 'GROUP BY "region_dim"' in sql
    assert "GeoHierarchy" not in sql


def test_mdx_translation_maps_hierarchy_slicer_to_matching_semantic_dimension():
    mdx = """
    SELECT {[Measures].[Amount]} ON COLUMNS
    FROM [demo]
    WHERE ([GeoHierarchy].[GeoHierarchy].[Region].[EMEA])
    """
    measures_meta = [{"name": "Amount", "default_agg": "sum"}]
    dimensions_meta = [{"name": "region_dim", "source_column_id": "col-region"}]
    hierarchy_meta = [{
        "name": "GeoHierarchy",
        "levels": [
            {
                "ordinal": 0,
                "name": "Region",
                "key_attribute": {"id": "col-region", "source": "physical_column"},
            },
            {
                "ordinal": 1,
                "name": "Country",
                "key_attribute": {"id": "col-country", "source": "physical_column"},
            },
        ],
    }]

    sql, protocol = _mdx_to_sql(
        mdx,
        measures_meta,
        dimensions_meta,
        hierarchy_meta=hierarchy_meta,
    )

    assert protocol == "jdbc"
    assert """WHERE "region_dim" = 'EMEA'""" in sql
    assert """"country_dim" = 'Region'""" not in sql
    assert "GROUP BY" not in sql


def test_mdx_translation_uses_hierarchy_level_name_when_no_matching_dimension():
    mdx = """
    SELECT {[Measures].[Amount]} ON COLUMNS,
           {[GeoHierarchy].[GeoHierarchy].[Region].Members} ON ROWS
    FROM [demo]
    """
    measures_meta = [{"name": "Amount", "default_agg": "sum"}]
    dimensions_meta = []
    hierarchy_meta = [{
        "name": "GeoHierarchy",
        "levels": [
            {
                "ordinal": 0,
                "name": "Region",
                "key_attribute": {"id": "col-region", "source": "physical_column"},
            },
            {
                "ordinal": 1,
                "name": "Country",
                "key_attribute": {"id": "col-country", "source": "physical_column"},
            },
        ],
    }]

    sql, protocol = _mdx_to_sql(
        mdx,
        measures_meta,
        dimensions_meta,
        hierarchy_meta=hierarchy_meta,
    )

    assert protocol == "jdbc"
    assert 'SUM("Amount")' in sql
    assert 'GROUP BY "Region"' in sql


def test_mdx_translation_explicit_hierarchy_level_does_not_add_default_level_dimension():
    mdx = """
    SELECT {[Measures].[Amount]} ON COLUMNS,
           {[GeoHierarchy].[GeoHierarchy].[Region].Members} ON ROWS
    FROM [demo]
    """
    measures_meta = [{"name": "Amount", "default_agg": "sum"}]
    dimensions_meta = [
        {"name": "region_dim", "source_column_id": "col-region"},
        {"name": "country_dim", "source_column_id": "col-country"},
    ]
    hierarchy_meta = [{
        "name": "GeoHierarchy",
        "levels": [
            {
                "ordinal": 0,
                "name": "Region",
                "key_attribute": {"id": "col-region", "source": "physical_column"},
            },
            {
                "ordinal": 1,
                "name": "Country",
                "key_attribute": {"id": "col-country", "source": "physical_column"},
            },
        ],
    }]

    sql, protocol = _mdx_to_sql(
        mdx,
        measures_meta,
        dimensions_meta,
        hierarchy_meta=hierarchy_meta,
    )

    assert protocol == "jdbc"
    assert 'GROUP BY "region_dim"' in sql
    assert "country_dim" not in sql


def test_mdx_translation_explicit_hierarchy_level_does_not_leak_level_name_dimension():
    """
    Regression guard:
    [GeoHierarchy].[GeoHierarchy].[Region].Members must not be parsed as [Region].Members.
    """
    mdx = """
    SELECT {[Measures].[Amount]} ON COLUMNS,
           {[GeoHierarchy].[GeoHierarchy].[Region].Members} ON ROWS
    FROM [demo]
    """
    measures_meta = [{"name": "Amount", "default_agg": "sum"}]
    dimensions_meta = [
        {"name": "region_dim", "source_column_id": "col-region"},
        {"name": "country_dim", "source_column_id": "col-country"},
        # Intentional collision: semantic dimension named like hierarchy level caption.
        {"name": "Region", "source_column_id": "col-region-caption"},
    ]
    hierarchy_meta = [{
        "name": "GeoHierarchy",
        "levels": [
            {
                "ordinal": 0,
                "name": "Region",
                "key_attribute": {"id": "col-region", "source": "physical_column"},
            },
            {
                "ordinal": 1,
                "name": "Country",
                "key_attribute": {"id": "col-country", "source": "physical_column"},
            },
        ],
    }]

    sql, protocol = _mdx_to_sql(
        mdx,
        measures_meta,
        dimensions_meta,
        hierarchy_meta=hierarchy_meta,
    )

    assert protocol == "jdbc"
    assert 'GROUP BY "region_dim"' in sql
    assert 'GROUP BY "region_dim", "Region"' not in sql
    assert 'SELECT "region_dim", "Region"' not in sql


def test_mdx_translation_hierarchy_key_member_filter_maps_without_spurious_filters():
    mdx = """
    SELECT {[Measures].[Amount]} ON COLUMNS
    FROM [demo]
    WHERE ([GeoHierarchy].[GeoHierarchy].&[United Kingdom])
    """
    measures_meta = [{"name": "Amount", "default_agg": "sum"}]
    dimensions_meta = [
        {"name": "region_dim", "source_column_id": "col-region"},
        {"name": "country_dim", "source_column_id": "col-country"},
    ]
    hierarchy_meta = [{
        "name": "GeoHierarchy",
        "levels": [
            {
                "ordinal": 0,
                "name": "Region",
                "key_attribute": {"id": "col-region", "source": "physical_column"},
            },
            {
                "ordinal": 1,
                "name": "Country",
                "key_attribute": {"id": "col-country", "source": "physical_column"},
            },
        ],
    }]

    sql, protocol = _mdx_to_sql(
        mdx,
        measures_meta,
        dimensions_meta,
        hierarchy_meta=hierarchy_meta,
    )

    assert protocol == "jdbc"
    assert """WHERE "country_dim" = 'United Kingdom'""" in sql
    assert "region_dim" not in sql


def test_mdx_translation_multi_hierarchy_crossjoin_with_tuple_slicers():
    mdx = """
    SELECT {[Measures].[Amount]} ON COLUMNS,
           CrossJoin(
             {[GeoHierarchy].[GeoHierarchy].[Region].Members},
             {[AccountHierarchy].[AccountHierarchy].[Type].Members}
           ) ON ROWS
    FROM [demo]
    WHERE (
      [GeoHierarchy].[GeoHierarchy].[Region].[EMEA],
      [AccountHierarchy].[AccountHierarchy].[Type].[CURRENT]
    )
    """
    measures_meta = [{"name": "Amount", "default_agg": "sum"}]
    dimensions_meta = [
        {"name": "region_dim", "source_column_id": "col-region"},
        {"name": "country_dim", "source_column_id": "col-country"},
        {"name": "type_dim", "source_column_id": "col-type"},
        {"name": "subtype_dim", "source_column_id": "col-subtype"},
    ]
    hierarchy_meta = [
        {
            "name": "GeoHierarchy",
            "levels": [
                {
                    "ordinal": 0,
                    "name": "Region",
                    "key_attribute": {"id": "col-region", "source": "physical_column"},
                },
                {
                    "ordinal": 1,
                    "name": "Country",
                    "key_attribute": {"id": "col-country", "source": "physical_column"},
                },
            ],
        },
        {
            "name": "AccountHierarchy",
            "levels": [
                {
                    "ordinal": 0,
                    "name": "Type",
                    "key_attribute": {"id": "col-type", "source": "physical_column"},
                },
                {
                    "ordinal": 1,
                    "name": "Subtype",
                    "key_attribute": {"id": "col-subtype", "source": "physical_column"},
                },
            ],
        },
    ]

    sql, protocol = _mdx_to_sql(
        mdx,
        measures_meta,
        dimensions_meta,
        hierarchy_meta=hierarchy_meta,
    )

    assert protocol == "jdbc"
    assert 'SUM("Amount")' in sql
    assert """WHERE "region_dim" = 'EMEA'""" in sql
    assert """"type_dim" = 'CURRENT'""" in sql
    assert 'GROUP BY "region_dim", "type_dim"' in sql
    assert "country_dim" not in sql
    assert "subtype_dim" not in sql


def test_mdx_translation_dual_hierarchy_key_member_slicers_without_spurious_filters():
    mdx = """
    SELECT {[Measures].[Amount]} ON COLUMNS
    FROM [demo]
    WHERE (
      [GeoHierarchy].[GeoHierarchy].&[EMEA],
      [AccountHierarchy].[AccountHierarchy].&[CURRENT]
    )
    """
    measures_meta = [{"name": "Amount", "default_agg": "sum"}]
    dimensions_meta = [
        {"name": "region_dim", "source_column_id": "col-region"},
        {"name": "type_dim", "source_column_id": "col-type"},
    ]
    hierarchy_meta = [
        {
            "name": "GeoHierarchy",
            "levels": [
                {
                    "ordinal": 0,
                    "name": "Region",
                    "key_attribute": {"id": "col-region", "source": "physical_column"},
                }
            ],
        },
        {
            "name": "AccountHierarchy",
            "levels": [
                {
                    "ordinal": 0,
                    "name": "Type",
                    "key_attribute": {"id": "col-type", "source": "physical_column"},
                }
            ],
        },
    ]

    sql, protocol = _mdx_to_sql(
        mdx,
        measures_meta,
        dimensions_meta,
        hierarchy_meta=hierarchy_meta,
    )

    assert protocol == "jdbc"
    assert 'SUM("Amount")' in sql
    assert """WHERE "region_dim" = 'EMEA'""" in sql
    assert """"type_dim" = 'CURRENT'""" in sql
    assert "GROUP BY" not in sql


def test_dax_translation_maps_hierarchy_level_column_to_semantic_dimension():
    dax = """
    EVALUATE
    SUMMARIZECOLUMNS(
      Geography[Region],
      "Amount", SUM(Fact[Amount])
    )
    """
    measures_meta = [{"name": "Amount", "default_agg": "sum"}]
    dimensions_meta = [
        {"name": "region_dim", "source_column_id": "col-region"},
        {"name": "country_dim", "source_column_id": "col-country"},
    ]
    hierarchy_meta = [{
        "name": "Geography",
        "levels": [
            {
                "ordinal": 0,
                "name": "Region",
                "key_attribute": {"id": "col-region", "source": "physical_column"},
            },
            {
                "ordinal": 1,
                "name": "Country",
                "key_attribute": {"id": "col-country", "source": "physical_column"},
            },
        ],
    }]

    sql, protocol = _statement_to_sql(
        dax,
        measures_meta,
        dimensions_meta,
        hierarchy_meta=hierarchy_meta,
    )

    assert protocol == "jdbc"
    assert 'SELECT "region_dim", SUM("Amount") AS "Amount" FROM "model_table"' in sql
    assert 'GROUP BY "region_dim"' in sql
    assert 'GROUP BY "Region"' not in sql
    assert "country_dim" not in sql


def test_dax_translation_maps_hierarchy_level_filter_to_semantic_dimension():
    dax = """
    EVALUATE
    SUMMARIZECOLUMNS(
      Geography[Region],
      FILTER(Geography, Geography[Region] = "EMEA"),
      "Amount", SUM(Fact[Amount])
    )
    """
    measures_meta = [{"name": "Amount", "default_agg": "sum"}]
    dimensions_meta = [{"name": "region_dim", "source_column_id": "col-region"}]
    hierarchy_meta = [{
        "name": "Geography",
        "levels": [
            {
                "ordinal": 0,
                "name": "Region",
                "key_attribute": {"id": "col-region", "source": "physical_column"},
            },
        ],
    }]

    sql, protocol = _statement_to_sql(
        dax,
        measures_meta,
        dimensions_meta,
        hierarchy_meta=hierarchy_meta,
    )

    assert protocol == "jdbc"
    assert """WHERE "region_dim" = 'EMEA'""" in sql
    assert 'GROUP BY "region_dim"' in sql


def test_dax_translation_preserves_topn_order_and_limit_with_hierarchy_columns():
    dax = """
    EVALUATE
    TOPN(
      5,
      SUMMARIZECOLUMNS(
        Geography[Region],
        "Amount", SUM(Fact[Amount])
      ),
      Geography[Region],
      DESC
    )
    """
    measures_meta = [{"name": "Amount", "default_agg": "sum"}]
    dimensions_meta = [{"name": "region_dim", "source_column_id": "col-region"}]
    hierarchy_meta = [{
        "name": "Geography",
        "levels": [
            {
                "ordinal": 0,
                "name": "Region",
                "key_attribute": {"id": "col-region", "source": "physical_column"},
            },
        ],
    }]

    sql, protocol = _statement_to_sql(
        dax,
        measures_meta,
        dimensions_meta,
        hierarchy_meta=hierarchy_meta,
    )

    assert protocol == "jdbc"
    assert 'GROUP BY "region_dim"' in sql
    assert 'ORDER BY "region_dim" DESC' in sql
    assert sql.endswith("LIMIT 5")


def test_dax_translation_raises_clear_error_for_ambiguous_hierarchy_level_name():
    dax = """
    EVALUATE
    SUMMARIZECOLUMNS(
      [Region],
      "Amount", SUM(Fact[Amount])
    )
    """
    measures_meta = [{"name": "Amount", "default_agg": "sum"}]
    dimensions_meta = [
        {"name": "region_a_dim", "source_column_id": "col-region-a"},
        {"name": "region_b_dim", "source_column_id": "col-region-b"},
    ]
    hierarchy_meta = [
        {
            "name": "GeoA",
            "levels": [
                {
                    "ordinal": 0,
                    "name": "Region",
                    "key_attribute": {"id": "col-region-a", "source": "physical_column"},
                }
            ],
        },
        {
            "name": "GeoB",
            "levels": [
                {
                    "ordinal": 0,
                    "name": "Region",
                    "key_attribute": {"id": "col-region-b", "source": "physical_column"},
                }
            ],
        },
    ]

    with pytest.raises(ValueError, match="Ambiguous DAX dimension reference 'Region'"):
        _statement_to_sql(
            dax,
            measures_meta,
            dimensions_meta,
            hierarchy_meta=hierarchy_meta,
        )


def test_dax_translation_uses_hierarchy_hint_to_disambiguate_level_name():
    dax = """
    EVALUATE
    SUMMARIZECOLUMNS(
      GeoA[Region],
      "Amount", SUM(Fact[Amount])
    )
    """
    measures_meta = [{"name": "Amount", "default_agg": "sum"}]
    dimensions_meta = [
        {"name": "region_a_dim", "source_column_id": "col-region-a"},
        {"name": "region_b_dim", "source_column_id": "col-region-b"},
    ]
    hierarchy_meta = [
        {
            "name": "GeoA",
            "levels": [
                {
                    "ordinal": 0,
                    "name": "Region",
                    "key_attribute": {"id": "col-region-a", "source": "physical_column"},
                }
            ],
        },
        {
            "name": "GeoB",
            "levels": [
                {
                    "ordinal": 0,
                    "name": "Region",
                    "key_attribute": {"id": "col-region-b", "source": "physical_column"},
                }
            ],
        },
    ]

    sql, protocol = _statement_to_sql(
        dax,
        measures_meta,
        dimensions_meta,
        hierarchy_meta=hierarchy_meta,
    )

    assert protocol == "jdbc"
    assert 'SELECT "region_a_dim", SUM("Amount") AS "Amount" FROM "model_table"' in sql
    assert "region_b_dim" not in sql


def test_dax_translation_hierarchy_refs_are_case_insensitive():
    dax = """
    EVALUATE
    SUMMARIZECOLUMNS(
      geography[region],
      "Amount", SUM(Fact[Amount])
    )
    """
    measures_meta = [{"name": "Amount", "default_agg": "sum"}]
    dimensions_meta = [{"name": "region_dim", "source_column_id": "col-region"}]
    hierarchy_meta = [{
        "name": "Geography",
        "levels": [
            {
                "ordinal": 0,
                "name": "Region",
                "key_attribute": {"id": "col-region", "source": "physical_column"},
            },
        ],
    }]

    sql, protocol = _statement_to_sql(
        dax,
        measures_meta,
        dimensions_meta,
        hierarchy_meta=hierarchy_meta,
    )

    assert protocol == "jdbc"
    assert 'SELECT "region_dim", SUM("Amount") AS "Amount" FROM "model_table"' in sql


def test_dax_translation_escapes_single_quotes_in_filter_literals():
    dax = """
    EVALUATE
    SUMMARIZECOLUMNS(
      Geography[Region],
      FILTER(Geography, Geography[Region] = "O'Reilly"),
      "Amount", SUM(Fact[Amount])
    )
    """
    measures_meta = [{"name": "Amount", "default_agg": "sum"}]
    dimensions_meta = [{"name": "region_dim", "source_column_id": "col-region"}]
    hierarchy_meta = [{
        "name": "Geography",
        "levels": [
            {
                "ordinal": 0,
                "name": "Region",
                "key_attribute": {"id": "col-region", "source": "physical_column"},
            },
        ],
    }]

    sql, protocol = _statement_to_sql(
        dax,
        measures_meta,
        dimensions_meta,
        hierarchy_meta=hierarchy_meta,
    )

    assert protocol == "jdbc"
    assert """WHERE "region_dim" = 'O''Reilly'""" in sql


def test_execute_response_contains_drilldown_members_and_values():
    mdx = """
    SELECT DrilldownLevel({[Geography].[Geography].[(All)]}) ON ROWS,
           {[Measures].[Amount]} ON COLUMNS
    FROM [demo]
    """
    measures_meta = [{"name": "Amount", "default_agg": "sum"}]
    dimensions_meta = [{"name": "Geography"}]
    columns = ["Geography", "Amount"]
    rows = [
        {"Geography": "France", "Amount": 10},
        {"Geography": "USA", "Amount": 20},
    ]

    xml = build_real_execute_response(
        mdx=mdx,
        catalog="demo",
        columns=columns,
        rows=rows,
        measures_meta=measures_meta,
        dimensions_meta=dimensions_meta,
    )

    assert "<Caption>France</Caption>" in xml
    assert "<Caption>USA</Caption>" in xml
    assert xml.count("<Cell CellOrdinal=") == 2
    assert "<Value xsi:type=\"xsd:double\">10.0" in xml
    assert "<Value xsi:type=\"xsd:double\">20.0" in xml
    assert '<Axis name="Axis0">' in xml
    assert '<Caption>Amount</Caption>' in xml


def test_execute_response_for_excel_keeps_shape_with_enriched_metadata():
    """
    Regression guard for Bug-XMLA-001:

    `[(All)].Members` in MDX means "children of the (All) level", not the
    (All) node itself. When real member data is present in the SQL result,
    the response must emit one tuple per child member with the matching
    cell value — even when the client is Excel. Previously the gateway
    short-circuited on `client_app_name == "Excel"` and emitted only the
    [All] member, leaving Excel pivots blank with `+ All <field>` and
    no expandable children.

    The test also verifies the response shape stays stable when metadata
    rows include extra UDA keys returned by model-service.
    """
    mdx = """
    SELECT {AddCalculatedMembers({[account_type].[account_type].[(All)].Members})}
    DIMENSION PROPERTIES MEMBER_TYPE
    ON COLUMNS
    FROM [m]
    CELL PROPERTIES CELL_ORDINAL
    """

    xml = build_real_execute_response(
        mdx=mdx,
        catalog="m",
        columns=["account_type", "base_amount"],
        rows=[
            {"account_type": "CURRENT", "base_amount": 10},
            {"account_type": "LOAN", "base_amount": 20},
        ],
        measures_meta=[{
            "name": "base_amount",
            "default_agg": "sum",
            "user_defined_attribute_id": "uda-2",
        }],
        dimensions_meta=[{
            "name": "account_type",
            "user_defined_attribute_name": "fx_account_type",
        }],
        client_app_name="Excel",
    )

    assert "<Caption>CURRENT</Caption>" in xml
    assert "<Caption>LOAN</Caption>" in xml
    assert "<Caption>All account_type</Caption>" not in xml
    assert '<Axis name="Axis0"><Tuples>' in xml
    assert "<MEMBER_TYPE>1</MEMBER_TYPE>" in xml
    assert '<Value xsi:type="xsd:double">10' in xml
    assert '<Value xsi:type="xsd:double">20' in xml


def test_execute_response_emits_excel_dimension_properties_with_all_in_path():
    """
    Bug-XMLA-001b regression: Excel scopes DIMENSION PROPERTIES to the
    (All) level, e.g. `[country_code].[country_code].[(All)].[MEMBER_KEY]`.
    The parser must accept `(` and `)` in the property list. If it
    doesn't, dim_props parses as empty, the AxisInfo declares only the
    six base properties, the response shape mismatches Excel's request,
    and Excel rejects the response (RPC failure).
    """
    from src.dax.mdx_execute import _parse_dimension_properties

    mdx = (
        "SELECT NON EMPTY Hierarchize(AddCalculatedMembers("
        "{[country_code].[country_code].[(All)].Members})) "
        "DIMENSION PROPERTIES PARENT_UNIQUE_NAME,HIERARCHY_UNIQUE_NAME,"
        "[country_code].[country_code].[(All)].[MEMBER_KEY],"
        "[country_code].[country_code].[(All)].[MEMBER_VALUE],"
        "[country_code].[country_code].[(All)].[MEMBER_NAME] "
        "ON COLUMNS FROM [modely]"
    )
    props = _parse_dimension_properties(mdx)
    tags = [p["tag"] for p in props]
    assert "PARENT_UNIQUE_NAME" in tags
    assert "HIERARCHY_UNIQUE_NAME" in tags
    assert "MEMBER_KEY" in tags
    assert "MEMBER_VALUE" in tags
    assert "MEMBER_NAME" in tags

    xml = build_real_execute_response(
        mdx=mdx,
        catalog="modely",
        columns=["country_code"],
        rows=[
            {"country_code": "US"},
            {"country_code": "GB"},
        ],
        measures_meta=[{"name": "base_amount", "default_agg": "sum"}],
        dimensions_meta=[{"name": "country_code"}],
        client_app_name="Excel",
    )
    assert "<MEMBER_KEY " in xml
    assert "<MEMBER_NAME " in xml
    assert "<MEMBER_KEY>US</MEMBER_KEY>" in xml
    assert "<MEMBER_KEY>GB</MEMBER_KEY>" in xml


def test_execute_response_falls_back_to_all_when_no_data_present():
    """
    Bug-XMLA-001 guard rail: when the SQL result is empty (e.g. a schema
    probe), the gateway should fall back to the (All) placeholder so
    Excel can render the pivot stub. The fix must not break this path.
    """
    mdx = """
    SELECT {AddCalculatedMembers({[account_type].[account_type].[(All)].Members})}
    DIMENSION PROPERTIES MEMBER_TYPE
    ON COLUMNS
    FROM [m]
    """
    xml = build_real_execute_response(
        mdx=mdx,
        catalog="m",
        columns=["account_type"],
        rows=[],
        measures_meta=[{"name": "base_amount", "default_agg": "sum"}],
        dimensions_meta=[{"name": "account_type"}],
        client_app_name="Excel",
    )
    assert "<Caption>All account_type</Caption>" in xml
    assert "<MEMBER_TYPE>2</MEMBER_TYPE>" in xml


def test_execute_response_scopes_dimension_properties_per_hierarchy():
    mdx = """
    SELECT NON EMPTY CrossJoin(
        Hierarchize(AddCalculatedMembers({DrilldownLevel({[account_type].[account_type].[All]})})),
        Hierarchize(AddCalculatedMembers({DrilldownLevel({[account_type_name].[account_type_name].[All]})}))
    )
    DIMENSION PROPERTIES
        PARENT_UNIQUE_NAME,
        HIERARCHY_UNIQUE_NAME,
        [account_type_name].[account_type_name].[account_type_name].[MEMBER_KEY],
        [account_type_name].[account_type_name].[account_type_name].[MEMBER_NAME],
        [account_type].[account_type].[account_type].[MEMBER_KEY],
        [account_type].[account_type].[account_type].[MEMBER_NAME]
    ON COLUMNS
    FROM [m]
    WHERE ([Measures].[base_amount])
    """
    measures_meta = [{"name": "base_amount", "default_agg": "sum"}]
    dimensions_meta = [{"name": "account_type"}, {"name": "account_type_name"}]
    columns = ["account_type", "account_type_name", "base_amount"]
    rows = [
        {"account_type": "CURRENT", "account_type_name": "Current Account", "base_amount": 10},
        {"account_type": "LOAN", "account_type_name": "Loan Account", "base_amount": 20},
    ]

    xml = build_real_execute_response(
        mdx=mdx,
        catalog="m",
        columns=columns,
        rows=rows,
        measures_meta=measures_meta,
        dimensions_meta=dimensions_meta,
    )

    assert '<HierarchyInfo name="[account_type].[account_type]">' in xml
    assert 'name="[account_type].[account_type].[MEMBER_KEY]"' in xml
    assert 'name="[account_type_name].[account_type_name].[MEMBER_KEY]"' in xml
    assert xml.count('name="[account_type].[account_type].[MEMBER_KEY]"') == 1
    assert xml.count('<PARENT_UNIQUE_NAME name="[account_type].[account_type].[PARENT_UNIQUE_NAME]"') == 1
    assert xml.count('<HIERARCHY_UNIQUE_NAME name="[account_type].[account_type].[HIERARCHY_UNIQUE_NAME]"') == 1


def test_execute_response_uses_level_one_children_for_drilldown_members():
    mdx = """
    SELECT NON EMPTY Hierarchize(AddCalculatedMembers({DrilldownLevel({[account_type].[account_type].[All]})}))
    DIMENSION PROPERTIES PARENT_UNIQUE_NAME,HIERARCHY_UNIQUE_NAME,[account_type].[account_type].[account_type].[MEMBER_KEY],[account_type].[account_type].[account_type].[MEMBER_NAME]
    ON COLUMNS
    FROM [m]
    WHERE ([Measures].[base_amount])
    """
    xml = build_real_execute_response(
        mdx=mdx,
        catalog="m",
        columns=["account_type", "base_amount"],
        rows=[
            {"account_type": "CURRENT", "base_amount": 10},
            {"account_type": "LOAN", "base_amount": 20},
        ],
        measures_meta=[{"name": "base_amount", "default_agg": "sum"}],
        dimensions_meta=[{"name": "account_type"}],
    )

    assert '<LName>[account_type].[account_type].[account_type]</LName>' in xml
    assert '<LNum>1</LNum>' in xml
    assert '<PARENT_UNIQUE_NAME>[account_type].[account_type].[All]</PARENT_UNIQUE_NAME>' in xml
    assert '<Axis name="SlicerAxis"><Tuples>' in xml
    assert "[Measures].[base_amount]" in xml


def test_real_execute_response_returns_children_for_drilldown_all_query():
    mdx = """
    SELECT NON EMPTY Hierarchize(AddCalculatedMembers({DrilldownLevel({[account_type].[account_type].[All]})}))
    DIMENSION PROPERTIES PARENT_UNIQUE_NAME,HIERARCHY_UNIQUE_NAME,[account_type].[account_type].[account_type].[MEMBER_KEY],[account_type].[account_type].[account_type].[MEMBER_NAME]
    ON COLUMNS
    FROM [m]
    WHERE ([Measures].[base_amount])
    """

    xml = build_real_execute_response(
        mdx=mdx,
        catalog="m",
        columns=["account_type"],
        rows=[
            {"account_type": "CURRENT"},
            {"account_type": "LOAN"},
            {"account_type": "SAVINGS"},
        ],
        measures_meta=[{"name": "base_amount", "default_agg": "sum"}],
        dimensions_meta=[{"name": "account_type"}],
    )

    assert "<Caption>CURRENT</Caption>" in xml
    assert "<Caption>LOAN</Caption>" in xml
    assert "<Caption>SAVINGS</Caption>" in xml
    assert "<Caption>All account_type</Caption>" not in xml


def test_real_execute_response_supports_cchildren_measure_form():
    mdx = """
    WITH MEMBER [Measures].cChildren As 'AddCalculatedMembers([account_type].[account_type].currentmember.children).count'
    Set FilteredMembers As '{[account_type].[account_type].[All]}'
    Select {[Measures].cChildren} on ROWS,
           Hierarchize(Generate(FilteredMembers, Ascendants([account_type].[account_type].currentmember)))
           DIMENSION PROPERTIES PARENT_UNIQUE_NAME, MEMBER_TYPE ON COLUMNS
    FROM [m]
    """

    xml = build_real_execute_response(
        mdx=mdx,
        catalog="m",
        columns=["account_type"],
        rows=[
            {"account_type": "CURRENT"},
            {"account_type": "LOAN"},
            {"account_type": "SAVINGS"},
        ],
        measures_meta=[],
        dimensions_meta=[{"name": "account_type"}],
    )

    assert "<Caption>cChildren</Caption>" in xml
    assert "<Value xsi:type=\"xsd:int\">3</Value>" in xml


def test_mdschema_members_tree_op_self_only_for_all_member():
    rows = _rows_members(
        catalog="m",
        measures=[{"name": "base_amount"}],
        dimensions=[{"name": "account_type"}],
        restrictions={
            "CUBE_NAME": ["m"],
            "MEMBER_UNIQUE_NAME": ["[account_type].[account_type].[All]"],
            "TREE_OP": ["8"],
        },
        member_data={
            "account_type": {
                "members": [
                    {"name": "CURRENT", "ordinal": 0},
                    {"name": "LOAN", "ordinal": 1},
                ]
            }
        },
    )

    assert [r["MEMBER_UNIQUE_NAME"] for r in rows] == [
        "[account_type].[account_type].[All]"
    ]


def test_mdschema_members_tree_op_children_for_all_member():
    rows = _rows_members(
        catalog="m",
        measures=[{"name": "base_amount"}],
        dimensions=[{"name": "account_type"}],
        restrictions={
            "CUBE_NAME": ["m"],
            "MEMBER_UNIQUE_NAME": ["[account_type].[account_type].[All]"],
            "TREE_OP": ["1"],
        },
        member_data={
            "account_type": {
                "members": [
                    {"name": "CURRENT", "ordinal": 0},
                    {"name": "LOAN", "ordinal": 1},
                ]
            }
        },
    )

    # Bug-3617 (Phase 2): account_type is a FLAT dimension (one level) — it never
    # collides, so it keeps the caption-form uname (only multi-level hierarchies
    # switch to the canonical key form).
    assert [r["MEMBER_UNIQUE_NAME"] for r in rows] == [
        "[account_type].[account_type].[CURRENT]",
        "[account_type].[account_type].[LOAN]",
    ]


def test_mdschema_properties_unrestricted_includes_member_and_cell_properties():
    rows = _rows_md_properties(
        catalog="m",
        dimensions=[{"name": "account_type"}],
        measures=[{"name": "base_amount"}],
        restrictions={},
    )

    names = {row["PROPERTY_NAME"] for row in rows}
    types = {(row["PROPERTY_NAME"], row["PROPERTY_TYPE"]) for row in rows}

    assert ("MEMBER_KEY", "1") in types
    assert ("MEMBER_VALUE", "1") in types
    assert ("PARENT_UNIQUE_NAME", "1") in types
    assert ("MEMBER_ORDINAL", "1") in types
    assert ("VALUE", "2") in types
    assert "FORMAT_STRING" in names


def test_mdschema_properties_member_filter_returns_hierarchy_member_properties():
    rows = _rows_md_properties(
        catalog="m",
        dimensions=[{"name": "account_type"}],
        measures=[{"name": "base_amount"}],
        restrictions={
            "PROPERTY_TYPE": ["1"],
            "HIERARCHY_UNIQUE_NAME": ["[account_type].[account_type]"],
        },
    )

    assert rows
    assert all(row["PROPERTY_TYPE"] == "1" for row in rows)
    assert all(row["HIERARCHY_UNIQUE_NAME"] == "[account_type].[account_type]" for row in rows)
    assert {row["LEVEL_UNIQUE_NAME"] for row in rows} == {
        "[account_type].[account_type].[(All)]",
        "[account_type].[account_type].[account_type]",
    }


def test_mdschema_properties_property_name_filter_returns_only_requested_property():
    rows = _rows_md_properties(
        catalog="m",
        dimensions=[{"name": "account_type"}],
        measures=[{"name": "base_amount"}],
        restrictions={
            "PROPERTY_TYPE": ["1"],
            "HIERARCHY_UNIQUE_NAME": ["[account_type].[account_type]"],
            "PROPERTY_NAME": ["MEMBER_VALUE"],
        },
    )

    assert rows
    assert all(row["PROPERTY_TYPE"] == "1" for row in rows)
    assert all(row["HIERARCHY_UNIQUE_NAME"] == "[account_type].[account_type]" for row in rows)
    assert {row["PROPERTY_NAME"] for row in rows} == {"MEMBER_VALUE"}


def test_mdschema_hierarchies_omits_all_member_for_excel_discover():
    rows = _rows_hierarchies(
        catalog="m",
        dimensions=[{"name": "account_type"}],
        measures=[{"name": "base_amount"}],
        member_data={"account_type": {"members": [{"name": "CURRENT"}]}},
        properties={"SspropInitAppName": "Excel"},
    )

    dim_row = next(r for r in rows if r.get("HIERARCHY_UNIQUE_NAME") == "[account_type].[account_type]")
    assert dim_row.get("DEFAULT_MEMBER") == "[account_type].[account_type].[All]"
    assert "ALL_MEMBER" not in dim_row


def test_mdschema_hierarchies_keeps_all_member_for_tabular_non_excel():
    rows = _rows_hierarchies(
        catalog="m",
        dimensions=[{"name": "account_type"}],
        measures=[{"name": "base_amount"}],
        member_data={"account_type": {"members": [{"name": "CURRENT"}]}},
        properties={"Format": "Tabular", "SspropInitAppName": "OnlyOffice"},
    )

    dim_row = next(r for r in rows if r.get("HIERARCHY_UNIQUE_NAME") == "[account_type].[account_type]")
    assert dim_row.get("ALL_MEMBER") == "[account_type].[account_type].[All]"


def test_mdschema_levels_supports_multi_level_hierarchy_metadata():
    rows = _rows_levels(
        catalog="m",
        dimensions=[{
            "name": "geo_hierarchy",
            "source": "hierarchy",
            "levels": [
                {"ordinal": 0, "name": "Region"},
                {"ordinal": 1, "name": "Country"},
            ],
        }],
        member_data={
            "geo_hierarchy": {
                "levels": ["Region", "Country"],
                "members_by_level": {
                    "0": [{"name": "EMEA", "ordinal": 0}],
                    "1": [{"name": "United Kingdom", "ordinal": 0, "parent": "EMEA"}],
                },
            }
        },
    )

    level_unames = [row["LEVEL_UNIQUE_NAME"] for row in rows if row["DIMENSION_UNIQUE_NAME"] == "[geo_hierarchy]"]
    assert "[geo_hierarchy].[geo_hierarchy].[(All)]" in level_unames
    assert "[geo_hierarchy].[geo_hierarchy].[Region]" in level_unames
    assert "[geo_hierarchy].[geo_hierarchy].[Country]" in level_unames


def test_mdschema_members_supports_children_for_hierarchy_member():
    rows = _rows_members(
        catalog="m",
        measures=[{"name": "base_amount"}],
        dimensions=[{
            "name": "geo_hierarchy",
            "source": "hierarchy",
            "levels": [
                {"ordinal": 0, "name": "Region"},
                {"ordinal": 1, "name": "Country"},
            ],
        }],
        restrictions={
            "MEMBER_UNIQUE_NAME": ["[geo_hierarchy].[geo_hierarchy].[EMEA]"],
            "LEVEL_UNIQUE_NAME": ["[geo_hierarchy].[geo_hierarchy].[Region]"],
            "TREE_OP": ["1"],
        },
        member_data={
            "geo_hierarchy": {
                "levels": ["Region", "Country"],
                "members_by_level": {
                    "0": [{"name": "EMEA", "ordinal": 0}],
                    "1": [{"name": "United Kingdom", "ordinal": 0, "parent": "EMEA"}],
                },
            }
        },
    )

    member_unames = [row["MEMBER_UNIQUE_NAME"] for row in rows if row["HIERARCHY_UNIQUE_NAME"] == "[geo_hierarchy].[geo_hierarchy]"]
    # Bug-3617 (Phase 2): canonical path-qualified uname (Region EMEA > Country UK).
    assert "[geo_hierarchy].[geo_hierarchy].[Country].&[EMEA]&[United Kingdom]" in member_unames
    assert "[geo_hierarchy].[geo_hierarchy].[United Kingdom]" not in member_unames
    assert "[geo_hierarchy].[geo_hierarchy].[EMEA]" not in member_unames


def test_mdx_where_filter_orders_where_before_group_by():
    mdx = """
    SELECT NON EMPTY Hierarchize(AddCalculatedMembers({DrilldownLevel({[account_type].[account_type].[All]})}))
    ON COLUMNS
    FROM [m]
    WHERE ([Measures].[base_amount], [account_type].[account_type].[CURRENT])
    """
    measures_meta = [{"name": "base_amount", "default_agg": "sum"}]
    dimensions_meta = [{"name": "account_type"}]

    sql, protocol = _mdx_to_sql(mdx, measures_meta, dimensions_meta)

    assert protocol == "jdbc"
    assert """WHERE "account_type" = 'CURRENT' GROUP BY "account_type\"""" in sql


def test_mdx_where_filter_ignores_all_member():
    mdx = """
    SELECT NON EMPTY Hierarchize(AddCalculatedMembers({DrilldownLevel({[account_type].[account_type].[All]})}))
    ON COLUMNS
    FROM [m]
    WHERE ([Measures].[base_amount], [account_type].[account_type].[All])
    """
    measures_meta = [{"name": "base_amount", "default_agg": "sum"}]
    dimensions_meta = [{"name": "account_type"}]

    sql, protocol = _mdx_to_sql(mdx, measures_meta, dimensions_meta)

    assert protocol == "jdbc"
    assert "WHERE" not in sql
    assert 'GROUP BY "account_type"' in sql


def test_execute_response_honors_tuple_axis_format_for_measure_probe():
    mdx = """
    SELECT
    FROM [m]
    WHERE ([Measures].[base_amount])
    CELL PROPERTIES VALUE, FORMAT_STRING, LANGUAGE, BACK_COLOR, FORE_COLOR, FONT_FLAGS
    """

    xml = build_real_execute_response(
        mdx=mdx,
        catalog="m",
        columns=["base_amount"],
        rows=[{"base_amount": 10}],
        measures_meta=[{"name": "base_amount", "default_agg": "sum"}],
        dimensions_meta=[],
        axis_format="TupleFormat",
    )

    assert '<AxisInfo name="Axis0">' not in xml
    assert '<Axis name="Axis0">' not in xml
    assert '<AxisInfo name="SlicerAxis">' in xml
    assert '<Axis name="SlicerAxis"><Tuples>' in xml
    assert "<Tuple><Member Hierarchy=\"[Measures]\">" in xml


def test_execute_response_preserves_no_axis_filter_probe_shape():
    mdx = """
    SELECT
    FROM [m]
    WHERE ([account_type].[account_type].[All],[Measures].[base_amount])
    CELL PROPERTIES VALUE, FORMAT_STRING, LANGUAGE, BACK_COLOR, FORE_COLOR, FONT_FLAGS
    """

    xml = build_real_execute_response(
        mdx=mdx,
        catalog="m",
        columns=["base_amount"],
        rows=[{"base_amount": 10}],
        measures_meta=[{"name": "base_amount", "default_agg": "sum"}],
        dimensions_meta=[{"name": "account_type"}],
        axis_format="TupleFormat",
    )

    assert '<AxisInfo name="Axis0">' not in xml
    assert '<Axis name="Axis0">' not in xml
    assert '<Axis name="SlicerAxis"><Tuples>' in xml
    assert '<UName>[Measures].[base_amount]</UName>' in xml
    assert '<UName>[account_type].[account_type].[All]</UName>' in xml


def test_execute_response_uses_all_member_for_slicer_dimension():
    mdx = """
    SELECT NON EMPTY Hierarchize(AddCalculatedMembers({DrilldownLevel({[account_type_code].[account_type_code].[All]})}))
    DIMENSION PROPERTIES PARENT_UNIQUE_NAME,HIERARCHY_UNIQUE_NAME,[account_type_code].[account_type_code].[account_type_code].[MEMBER_KEY],[account_type_code].[account_type_code].[account_type_code].[MEMBER_NAME]
    ON COLUMNS
    FROM [m]
    WHERE ([account_type].[account_type].[All],[Measures].[base_amount])
    """

    xml = build_real_execute_response(
        mdx=mdx,
        catalog="m",
        columns=["account_type_code", "base_amount"],
        rows=[
            {"account_type_code": "CURRENT", "base_amount": 10},
            {"account_type_code": "LOAN", "base_amount": 20},
        ],
        measures_meta=[{"name": "base_amount", "default_agg": "sum"}],
        dimensions_meta=[{"name": "account_type"}, {"name": "account_type_code"}],
    )

    assert '<Axis name="SlicerAxis"><Tuples>' in xml
    assert '<Caption>All account_type</Caption>' in xml
    assert '<UName>[account_type].[account_type].[All]</UName>' in xml


def test_execute_response_uses_existing_multi_hierarchy_tuples_not_cross_product():
    mdx = """
    SELECT NON EMPTY CrossJoin(
        Hierarchize(AddCalculatedMembers({DrilldownLevel({[account_type].[account_type].[All]})})),
        Hierarchize(AddCalculatedMembers({DrilldownLevel({[account_type_name].[account_type_name].[All]})}))
    ) ON COLUMNS
    FROM [m]
    WHERE ([Measures].[base_amount])
    """

    xml = build_real_execute_response(
        mdx=mdx,
        catalog="m",
        columns=["account_type", "account_type_name", "base_amount"],
        rows=[
            {"account_type": "CURRENT", "account_type_name": "Current", "base_amount": 10},
            {"account_type": "LOAN", "account_type_name": "Loan", "base_amount": 20},
        ],
        measures_meta=[{"name": "base_amount", "default_agg": "sum"}],
        dimensions_meta=[{"name": "account_type"}, {"name": "account_type_name"}],
    )

    assert xml.count("<Tuple><Member Hierarchy=\"[account_type].[account_type]\">") == 2
    assert "Current" in xml
    assert "Loan" in xml


def test_execute_response_returns_children_for_all_members_query():
    mdx = """
    SELECT {AddCalculatedMembers({[account_type_name].[account_type_name].[(All)].Members})}
    DIMENSION PROPERTIES MEMBER_TYPE
    ON COLUMNS
    FROM [m]
    CELL PROPERTIES CELL_ORDINAL
    """

    xml = build_real_execute_response(
        mdx=mdx,
        catalog="m",
        columns=["account_type_name"],
        rows=[
            {"account_type_name": "Current"},
            {"account_type_name": "Loan"},
        ],
        measures_meta=[],
        dimensions_meta=[{"name": "account_type_name"}],
    )

    assert "<Caption>All account_type_name</Caption>" not in xml
    assert "<Caption>Current</Caption>" in xml
    assert "<Caption>Loan</Caption>" in xml
    assert "<LNum>1</LNum>" in xml
    assert "<PARENT_UNIQUE_NAME>[account_type_name].[account_type_name].[All]</PARENT_UNIQUE_NAME>" in xml
    assert "<MEMBER_ORDINAL>0</MEMBER_ORDINAL>" in xml
    assert "<MEMBER_ORDINAL>1</MEMBER_ORDINAL>" in xml
    assert "<MEMBER_KEY>Current</MEMBER_KEY>" in xml
    assert "<MEMBER_NAME>Loan</MEMBER_NAME>" in xml


def test_execute_response_uses_members_axis_by_default_for_single_hierarchy():
    mdx = """
    SELECT {AddCalculatedMembers({[account_type].[account_type].[(All)].Members})}
    DIMENSION PROPERTIES MEMBER_TYPE
    ON COLUMNS
    FROM [m]
    CELL PROPERTIES CELL_ORDINAL
    """

    xml = build_real_execute_response(
        mdx=mdx,
        catalog="m",
        columns=["account_type"],
        rows=[
            {"account_type": "CURRENT"},
            {"account_type": "LOAN"},
        ],
        measures_meta=[],
        dimensions_meta=[{"name": "account_type"}],
    )

    assert '<Axis name="Axis0"><Members Hierarchy="[account_type].[account_type]">' in xml
    assert '<Axis name="Axis0"><Tuples>' not in xml


def test_execute_response_emits_member_value_for_dimension_members():
    mdx = """
    SELECT {AddCalculatedMembers({[account_type].[account_type].[(All)].Members})}
    DIMENSION PROPERTIES MEMBER_TYPE, MEMBER_VALUE
    ON COLUMNS
    FROM [m]
    CELL PROPERTIES CELL_ORDINAL
    """

    xml = build_real_execute_response(
        mdx=mdx,
        catalog="m",
        columns=["account_type"],
        rows=[
            {"account_type": "CURRENT"},
            {"account_type": "LOAN"},
        ],
        measures_meta=[],
        dimensions_meta=[{"name": "account_type"}],
    )

    assert 'name="[account_type].[account_type].[MEMBER_VALUE]"' in xml
    assert "<MEMBER_VALUE>CURRENT</MEMBER_VALUE>" in xml
    assert "<MEMBER_VALUE>LOAN</MEMBER_VALUE>" in xml


def test_execute_response_for_excel_emits_full_member_properties():
    """
    Excel's MSOLAP client requires the full set of member properties on
    every member. Pre-Bug-XMLA-001 the gateway used a `minimal_excel_props`
    mode that stripped PARENT_UNIQUE_NAME, HIERARCHY_UNIQUE_NAME,
    MEMBER_ORDINAL, etc. — Excel would then either render an empty pivot
    or crash with an RPC failure when the schema mismatch was detected.
    This regression locks in that the full property set is always emitted
    even when Excel only asks for MEMBER_TYPE in DIMENSION PROPERTIES.
    """
    mdx = """
    SELECT {AddCalculatedMembers({[account_type].[account_type].[(All)].Members})}
    DIMENSION PROPERTIES MEMBER_TYPE
    ON COLUMNS
    FROM [m]
    CELL PROPERTIES CELL_ORDINAL
    """

    xml = build_real_execute_response(
        mdx=mdx,
        catalog="m",
        columns=["account_type"],
        rows=[
            {"account_type": "CURRENT"},
            {"account_type": "LOAN"},
        ],
        measures_meta=[],
        dimensions_meta=[{"name": "account_type"}],
        client_app_name="Excel",
    )

    assert "<Caption>CURRENT</Caption>" in xml
    assert "<Caption>LOAN</Caption>" in xml
    assert "<MEMBER_TYPE>1</MEMBER_TYPE>" in xml
    assert "<PARENT_UNIQUE_NAME>" in xml
    assert "<HIERARCHY_UNIQUE_NAME>" in xml
    assert "<MEMBER_ORDINAL>" in xml
    assert "<CHILDREN_CARDINALITY>" in xml
    assert "<MEMBER_KEY>CURRENT</MEMBER_KEY>" in xml
    assert "<MEMBER_NAME>CURRENT</MEMBER_NAME>" in xml
    assert '<Axis name="Axis0"><Tuples>' in xml
    assert '<Axis name="Axis0"><Members' not in xml


def test_execute_response_for_excel_keeps_explicitly_requested_member_value():
    """
    DIMENSION PROPERTIES MEMBER_VALUE must be honoured: every emitted member
    should carry its own MEMBER_VALUE element. After the Bug-XMLA-001 fix
    the emitted members are the children, so MEMBER_VALUE should be the
    leaf member key, not "All".
    """
    mdx = """
    SELECT {AddCalculatedMembers({[account_type].[account_type].[(All)].Members})}
    DIMENSION PROPERTIES MEMBER_TYPE, MEMBER_VALUE
    ON COLUMNS
    FROM [m]
    CELL PROPERTIES CELL_ORDINAL
    """

    xml = build_real_execute_response(
        mdx=mdx,
        catalog="m",
        columns=["account_type"],
        rows=[
            {"account_type": "CURRENT"},
            {"account_type": "LOAN"},
        ],
        measures_meta=[],
        dimensions_meta=[{"name": "account_type"}],
        client_app_name="Excel",
    )

    assert "<Caption>CURRENT</Caption>" in xml
    assert "<Caption>LOAN</Caption>" in xml
    assert "<MEMBER_VALUE>CURRENT</MEMBER_VALUE>" in xml
    assert "<MEMBER_VALUE>LOAN</MEMBER_VALUE>" in xml
    assert "<MEMBER_VALUE>All</MEMBER_VALUE>" not in xml


def test_execute_response_crossjoin_two_row_dimensions_emits_unique_member_ordinals():
    """Bug-XMLA-003 regression.

    When Excel puts two dimensions on the same axis via CrossJoin, the
    result tuples carry the same member repeated across multiple
    tuples (every ``country_code`` appearing once per ``customer_segment``).
    The previous serializer emitted ``MEMBER_ORDINAL=0`` for every
    entry, which Excel's MSOLAP client rejects with an internal
    uniqueness violation, killing the Excel process with an RPC
    failure.

    This test locks in three invariants that together make the
    multi-dim row axis response valid for strict Excel clients:

    1. Each distinct member within each hierarchy receives a unique
       ``MEMBER_ORDINAL`` (first-appearance order: 0, 1, 2, ...).
    2. The cartesian tuple list reflects exactly the rows returned
       from the query router (NonEmpty semantics) — no phantom
       cross-products.
    3. Every tuple carries both hierarchies as ``<Member>``
       elements inside a single ``<Tuple>``.
    """
    mdx = """
    SELECT NON EMPTY Hierarchize(
      CrossJoin(
        {[country_code].[country_code].[(All)].Members},
        {[customer_segment].[customer_segment].[(All)].Members}
      )
    ) DIMENSION PROPERTIES PARENT_UNIQUE_NAME,HIERARCHY_UNIQUE_NAME,MEMBER_KEY,MEMBER_NAME,MEMBER_UNIQUE_NAME ON ROWS,
    {[Measures].[base_amount]} ON COLUMNS
    FROM [m]
    CELL PROPERTIES VALUE
    """

    rows = [
        {"country_code": "US", "customer_segment": "Retail", "base_amount": 100},
        {"country_code": "US", "customer_segment": "Corporate", "base_amount": 200},
        {"country_code": "UK", "customer_segment": "Retail", "base_amount": 150},
        {"country_code": "UK", "customer_segment": "Corporate", "base_amount": 250},
    ]

    xml = build_real_execute_response(
        mdx=mdx,
        catalog="m",
        columns=["country_code", "customer_segment", "base_amount"],
        rows=rows,
        measures_meta=[{"name": "base_amount", "default_agg": "sum"}],
        dimensions_meta=[{"name": "country_code"}, {"name": "customer_segment"}],
        client_app_name="Microsoft Excel",
    )

    # 1. NonEmpty produced exactly 4 tuples (no invented combinations).
    import re
    # The response has one measure tuple on Axis0 plus the four row
    # tuples on Axis1; we only count tuples inside the Axis1 block.
    axis1 = re.search(
        r'<Axis name="Axis1">(.*?)</Axis>', xml, re.DOTALL
    )
    assert axis1 is not None, "Axis1 missing from response"
    tuple_count = axis1.group(1).count("<Tuple>")
    assert tuple_count == 4, (
        f"Expected 4 row tuples for 4 result rows, got {tuple_count}"
    )

    # 2. Every tuple carries both hierarchies. Count tuples that
    #    contain both member hierarchies — should equal the total
    #    tuple count.
    per_tuple = re.findall(r'<Tuple>.*?</Tuple>', axis1.group(1), re.DOTALL)
    for i, t in enumerate(per_tuple):
        assert 'Hierarchy="[country_code].[country_code]"' in t, (
            f"tuple {i} missing country_code hierarchy"
        )
        assert 'Hierarchy="[customer_segment].[customer_segment]"' in t, (
            f"tuple {i} missing customer_segment hierarchy"
        )

    # 3. Each distinct member within a hierarchy gets a unique ordinal.
    #    Build a map of (hierarchy, ordinal) → set(captions) and
    #    assert no hierarchy has two different captions at the same
    #    ordinal.
    cap_by_ord: dict[tuple[str, str], set[str]] = {}
    for member_match in re.finditer(
        r'<Member Hierarchy="([^"]+)">(.*?)</Member>',
        axis1.group(1),
        re.DOTALL,
    ):
        hier = member_match.group(1)
        body = member_match.group(2)
        cap = re.search(r"<Caption>([^<]*)</Caption>", body).group(1)
        ord_match = re.search(r"<MEMBER_ORDINAL>(\d+)</MEMBER_ORDINAL>", body)
        assert ord_match is not None, (
            f"member {cap!r} in {hier!r} missing MEMBER_ORDINAL"
        )
        ordinal = ord_match.group(1)
        cap_by_ord.setdefault((hier, ordinal), set()).add(cap)

    for (hier, ordinal), captions in cap_by_ord.items():
        assert len(captions) == 1, (
            f"Hierarchy {hier!r} has {len(captions)} distinct captions at "
            f"ordinal {ordinal}: {sorted(captions)!r}. Excel requires each "
            f"ordinal to map to exactly one distinct member per hierarchy."
        )

    # 4. The two hierarchies' ordinal ranges must cover all their
    #    distinct members (0..N-1). For US/UK and Retail/Corporate
    #    that means each hierarchy has exactly two unique ordinals
    #    {0, 1}.
    country_ordinals = {
        o for (h, o) in cap_by_ord.keys()
        if h == "[country_code].[country_code]"
    }
    segment_ordinals = {
        o for (h, o) in cap_by_ord.keys()
        if h == "[customer_segment].[customer_segment]"
    }
    assert country_ordinals == {"0", "1"}, country_ordinals
    assert segment_ordinals == {"0", "1"}, segment_ordinals


# ---------------------------------------------------------------------------
# MDX Union pattern (D.2 — subtotals)
# ---------------------------------------------------------------------------

def test_mdx_hierarchize_union_extracts_dimensions():
    """Excel sends HIERARCHIZE(UNION(...)) for subtotals with mixed levels."""
    mdx = """
    SELECT {[Measures].[Amount]} ON COLUMNS,
           NON EMPTY Hierarchize(Union(
             {[Geography].[Geography].[Region].Members},
             {[Geography].[Geography].[Country].Members}
           )) ON ROWS
    FROM [demo]
    """
    measures_meta = [{"name": "Amount", "default_agg": "sum"}]
    dimensions_meta = [{"name": "Geography"}]

    sql, protocol = _mdx_to_sql(mdx, measures_meta, dimensions_meta)

    assert protocol == "jdbc"
    assert 'SUM("Amount")' in sql
    assert "Geography" in sql


def test_execute_response_multi_measure_mixed_aggs():
    """Multiple measures with different aggregation types produce correct cells."""
    mdx = """
    SELECT {[Measures].[Revenue], [Measures].[AvgPrice]} ON COLUMNS,
           {[Geography].[Geography].[(All)].Members} ON ROWS
    FROM [demo]
    """
    xml = build_real_execute_response(
        mdx=mdx,
        catalog="demo",
        columns=["Geography", "Revenue", "AvgPrice"],
        rows=[
            {"Geography": "France", "Revenue": 1000, "AvgPrice": 50},
            {"Geography": "USA", "Revenue": 2000, "AvgPrice": 75},
        ],
        measures_meta=[
            {"name": "Revenue", "default_agg": "sum"},
            {"name": "AvgPrice", "default_agg": "avg"},
        ],
        dimensions_meta=[{"name": "Geography"}],
    )

    assert "<Caption>Revenue</Caption>" in xml
    assert "<Caption>AvgPrice</Caption>" in xml
    assert "<Caption>France</Caption>" in xml
    assert "<Caption>USA</Caption>" in xml
    assert xml.count("<Cell CellOrdinal=") == 4


def test_execute_response_three_measures_with_grand_total_metadata():
    """Three measures with different aggs: grand total relies on MEASURE_AGGREGATOR."""
    measures = [
        {"name": "Revenue", "default_agg": "sum"},
        {"name": "OrderCount", "default_agg": "count"},
        {"name": "MinPrice", "default_agg": "min"},
    ]
    rows_result = _rows_measures("demo", measures)
    assert rows_result[0]["MEASURE_AGGREGATOR"] == "1"   # SUM
    assert rows_result[1]["MEASURE_AGGREGATOR"] == "2"   # COUNT
    assert rows_result[2]["MEASURE_AGGREGATOR"] == "3"   # MIN


# ---------------------------------------------------------------------------
# Multi-select WHERE clause (E.1 — report filters)
# ---------------------------------------------------------------------------

def test_mdx_where_multi_select_produces_in_clause():
    mdx = """
    SELECT {[Measures].[Amount]} ON COLUMNS,
           {[Product].[Product].[(All)].Members} ON ROWS
    FROM [demo]
    WHERE ({[Geography].[Geography].[France], [Geography].[Geography].[USA]})
    """
    measures_meta = [{"name": "Amount", "default_agg": "sum"}]
    dimensions_meta = [{"name": "Geography"}, {"name": "Product"}]

    sql, protocol = _mdx_to_sql(mdx, measures_meta, dimensions_meta)

    assert protocol == "jdbc"
    assert "IN ('France', 'USA')" in sql
    assert 'GROUP BY "Product"' in sql


def test_mdx_where_single_member_stays_eq():
    mdx = """
    SELECT {[Measures].[Amount]} ON COLUMNS
    FROM [demo]
    WHERE ([Geography].[Geography].[France])
    """
    measures_meta = [{"name": "Amount", "default_agg": "sum"}]
    dimensions_meta = [{"name": "Geography"}]

    sql, protocol = _mdx_to_sql(mdx, measures_meta, dimensions_meta)

    assert protocol == "jdbc"
    assert """WHERE "Geography" = 'France'""" in sql


def test_mdx_where_two_dimensions_with_multi_select():
    mdx = """
    SELECT {[Measures].[Amount]} ON COLUMNS
    FROM [demo]
    WHERE ({[Geography].[Geography].[France], [Geography].[Geography].[USA]}, [Product].[Product].[Laptop])
    """
    measures_meta = [{"name": "Amount", "default_agg": "sum"}]
    dimensions_meta = [{"name": "Geography"}, {"name": "Product"}]

    sql, protocol = _mdx_to_sql(mdx, measures_meta, dimensions_meta)

    assert protocol == "jdbc"
    assert "IN ('France', 'USA')" in sql
    assert """"Product" = 'Laptop'""" in sql


# ---------------------------------------------------------------------------
# Slicer subselect (E.2)
# ---------------------------------------------------------------------------

def test_mdx_slicer_subselect_single_member():
    mdx = """
    SELECT {[Measures].[Amount]} ON COLUMNS,
           {[Product].[Product].[(All)].Members} ON ROWS
    FROM (SELECT {[Geography].[Geography].[France]} ON COLUMNS FROM [demo])
    """
    measures_meta = [{"name": "Amount", "default_agg": "sum"}]
    dimensions_meta = [{"name": "Geography"}, {"name": "Product"}]

    sql, protocol = _mdx_to_sql(mdx, measures_meta, dimensions_meta)

    assert protocol == "jdbc"
    assert """"Geography" = 'France'""" in sql


def test_mdx_slicer_subselect_multi_select():
    mdx = """
    SELECT {[Measures].[Amount]} ON COLUMNS,
           {[Product].[Product].[(All)].Members} ON ROWS
    FROM (SELECT {[Geography].[Geography].[France], [Geography].[Geography].[USA]} ON 0 FROM [demo])
    """
    measures_meta = [{"name": "Amount", "default_agg": "sum"}]
    dimensions_meta = [{"name": "Geography"}, {"name": "Product"}]

    sql, protocol = _mdx_to_sql(mdx, measures_meta, dimensions_meta)

    assert protocol == "jdbc"
    assert "IN ('France', 'USA')" in sql


def test_mdx_slicer_subselect_combined_with_where():
    mdx = """
    SELECT {[Measures].[Amount]} ON COLUMNS
    FROM (SELECT {[Geography].[Geography].[France]} ON 0 FROM [demo])
    WHERE ([Product].[Product].[Laptop])
    """
    measures_meta = [{"name": "Amount", "default_agg": "sum"}]
    dimensions_meta = [{"name": "Geography"}, {"name": "Product"}]

    sql, protocol = _mdx_to_sql(mdx, measures_meta, dimensions_meta)

    assert protocol == "jdbc"
    assert """"Geography" = 'France'""" in sql
    assert """"Product" = 'Laptop'""" in sql


# ---------------------------------------------------------------------------
# Drill-down hardening (F)
# ---------------------------------------------------------------------------

def test_mdx_drilldown_three_level_hierarchy_resolves_dimension():
    """DrilldownLevel on a 3-level hierarchy resolves to a valid dimension."""
    mdx = """
    SELECT {[Measures].[Amount]} ON COLUMNS,
           DrilldownLevel({[GeoHierarchy].[GeoHierarchy].[All]}) ON ROWS
    FROM [demo]
    """
    measures_meta = [{"name": "Amount", "default_agg": "sum"}]
    dimensions_meta = [
        {"name": "continent_dim", "source_column_id": "col-continent"},
        {"name": "country_dim", "source_column_id": "col-country"},
        {"name": "city_dim", "source_column_id": "col-city"},
    ]
    hierarchy_meta = [{
        "name": "GeoHierarchy",
        "levels": [
            {"ordinal": 0, "name": "Continent", "key_attribute": {"id": "col-continent", "source": "physical_column"}},
            {"ordinal": 1, "name": "Country", "key_attribute": {"id": "col-country", "source": "physical_column"}},
            {"ordinal": 2, "name": "City", "key_attribute": {"id": "col-city", "source": "physical_column"}},
        ],
    }]

    sql, protocol = _mdx_to_sql(mdx, measures_meta, dimensions_meta, hierarchy_meta=hierarchy_meta)

    assert protocol == "jdbc"
    assert 'SUM("Amount")' in sql
    # Hierarchy resolves to a dimension; query-router handles the mapping
    assert "GROUP BY" in sql


def test_mdx_drilldown_explicit_level_resolves_to_correct_dimension():
    """When MDX specifies an explicit level, resolve to that level's dimension."""
    mdx = """
    SELECT {[Measures].[Amount]} ON COLUMNS,
           {[GeoHierarchy].[GeoHierarchy].[Continent].Members} ON ROWS
    FROM [demo]
    """
    measures_meta = [{"name": "Amount", "default_agg": "sum"}]
    dimensions_meta = [
        {"name": "continent_dim", "source_column_id": "col-continent"},
        {"name": "country_dim", "source_column_id": "col-country"},
    ]
    hierarchy_meta = [{
        "name": "GeoHierarchy",
        "levels": [
            {"ordinal": 0, "name": "Continent", "key_attribute": {"id": "col-continent", "source": "physical_column"}},
            {"ordinal": 1, "name": "Country", "key_attribute": {"id": "col-country", "source": "physical_column"}},
        ],
    }]

    sql, protocol = _mdx_to_sql(mdx, measures_meta, dimensions_meta, hierarchy_meta=hierarchy_meta)

    assert protocol == "jdbc"
    assert 'GROUP BY "continent_dim"' in sql
    assert "country_dim" not in sql


# ---------------------------------------------------------------------------
# Bug-584 — Multi-hierarchy subtotal row tuple builder
# ---------------------------------------------------------------------------

class TestBuildMultiHierarchyRowTuples:

    @staticmethod
    def _make_hier(name, mdx_dim, mdx_hier, levels):
        from src.dax.subtotal_engine import SubtotalHierarchy, SubtotalLevel
        return SubtotalHierarchy(
            hierarchy_name=name,
            mdx_dim_name=mdx_dim,
            mdx_hier_name=mdx_hier,
            levels=[SubtotalLevel(name=n, ordinal=o, dim_name=d) for n, o, d in levels],
        )

    def test_detail_rows_produce_leaf_members(self):
        from src.dax.mdx_execute import _build_multi_hierarchy_row_tuples
        from src.dax.subtotal_engine import SUBTOTAL_GRAIN_PREFIX

        h1 = self._make_hier("Geo", "Geography", "Geo", [("Continent", 0, "continent"), ("Country", 1, "country")])
        h2 = self._make_hier("Time", "Date", "Calendar", [("Year", 0, "year"), ("Month", 1, "month")])

        rows = [{
            "continent": "Europe", "country": "France",
            "year": "2024", "month": "Jan",
            SUBTOTAL_GRAIN_PREFIX + "Geo": 1,
            SUBTOTAL_GRAIN_PREFIX + "Time": 1,
        }]
        result = _build_multi_hierarchy_row_tuples(rows, [h1, h2])
        assert len(result) == 1
        assert len(result[0]) == 2
        assert result[0][0]["name"] == "France"
        assert result[0][1]["name"] == "Jan"
        assert result[0][0]["hierarchy"] == "[Geography].[Geo]"
        assert result[0][1]["hierarchy"] == "[Date].[Calendar]"

    def test_all_grain_produces_all_member(self):
        from src.dax.mdx_execute import _build_multi_hierarchy_row_tuples
        from src.dax.subtotal_engine import SUBTOTAL_GRAIN_PREFIX

        h1 = self._make_hier("Geo", "Geography", "Geo", [("Country", 0, "country")])
        h2 = self._make_hier("Time", "Date", "Calendar", [("Year", 0, "year")])

        rows = [{
            "country": "", "year": "",
            SUBTOTAL_GRAIN_PREFIX + "Geo": -1,
            SUBTOTAL_GRAIN_PREFIX + "Time": -1,
        }]
        result = _build_multi_hierarchy_row_tuples(rows, [h1, h2])
        assert len(result) == 1
        assert result[0][0]["name"] == "All"
        assert result[0][0]["member_type"] == 2
        assert result[0][1]["name"] == "All"

    def test_mixed_grain_rows(self):
        from src.dax.mdx_execute import _build_multi_hierarchy_row_tuples
        from src.dax.subtotal_engine import SUBTOTAL_GRAIN_PREFIX

        h1 = self._make_hier("Geo", "Geography", "Geo", [("Continent", 0, "continent"), ("Country", 1, "country")])
        h2 = self._make_hier("Time", "Date", "Calendar", [("Year", 0, "year")])

        rows = [
            {
                "continent": "Europe", "country": "",
                "year": "",
                SUBTOTAL_GRAIN_PREFIX + "Geo": 0,
                SUBTOTAL_GRAIN_PREFIX + "Time": -1,
            },
            {
                "continent": "Europe", "country": "France",
                "year": "2024",
                SUBTOTAL_GRAIN_PREFIX + "Geo": 1,
                SUBTOTAL_GRAIN_PREFIX + "Time": 0,
            },
        ]
        result = _build_multi_hierarchy_row_tuples(rows, [h1, h2])
        assert len(result) == 2
        assert result[0][0]["name"] == "Europe"
        assert result[0][0]["has_children"] is True
        assert result[0][1]["name"] == "All"
        assert result[1][0]["name"] == "France"
        assert result[1][0]["has_children"] is False
        assert result[1][1]["name"] == "2024"


# ---------------------------------------------------------------------------
# Bug-586 — Subtotal cell data with column-axis subtotals (no measures on columns)
# ---------------------------------------------------------------------------

class TestSubtotalCellDataColumnAxis:

    def test_slicer_measure_emits_cells(self):
        from src.dax.mdx_execute import _build_subtotal_cell_data
        rows = [
            {"country": "France", "Amount": 100},
            {"country": "Germany", "Amount": 200},
            {"country": "", "Amount": 300},
        ]
        col_members = [
            {"hierarchy": "[Geography].[Geo]", "caption": "France"},
            {"hierarchy": "[Geography].[Geo]", "caption": "Germany"},
            {"hierarchy": "[Geography].[Geo]", "caption": "All"},
        ]
        cell_data = _build_subtotal_cell_data(
            rows, col_members, ["Amount"], "Amount",
        )
        assert "CellOrdinal" in cell_data
        assert "100" in cell_data
        assert "200" in cell_data
        assert "300" in cell_data

    def test_empty_col_members_uses_slicer(self):
        from src.dax.mdx_execute import _build_subtotal_cell_data
        rows = [
            {"Amount": 100},
            {"Amount": 200},
        ]
        cell_data = _build_subtotal_cell_data(
            rows, [], ["Amount"], "Amount",
        )
        assert "CellOrdinal" in cell_data
        assert "100" in cell_data
        assert "200" in cell_data


# ---------------------------------------------------------------------------
# Round-54 — Cross-axis subtotal CellData ordinal alignment
# ---------------------------------------------------------------------------

class TestCrossAxisSubtotalCellData:

    def test_ordinals_are_row_by_column(self):
        """Cross-axis cell ordinals must follow row_idx * num_cols + col_idx."""
        from src.dax.mdx_execute import _build_cross_axis_subtotal_cell_data
        rows = [
            {"Amount": 100},
            {"Amount": 200},
            {"Amount": 150},
            {"Amount": 250},
        ]
        row_tuples = [
            [{"uname": "[Cal].[Cal].[Year].&[2024]"}],
            [{"uname": "[Cal].[Cal].[Year].&[2024]"}],
            [{"uname": "[Cal].[Cal].[Year].&[2025]"}],
            [{"uname": "[Cal].[Cal].[Year].&[2025]"}],
        ]
        col_tuples = [
            [{"uname": "[Geo].[Geo].[Country].&[US]"}],
            [{"uname": "[Geo].[Geo].[Country].&[UK]"}],
            [{"uname": "[Geo].[Geo].[Country].&[US]"}],
            [{"uname": "[Geo].[Geo].[Country].&[UK]"}],
        ]
        cell_data = _build_cross_axis_subtotal_cell_data(
            rows, row_tuples, col_tuples, ["Amount"], "Amount",
        )
        import re
        ordinals = [int(m.group(1)) for m in re.finditer(r'CellOrdinal="(\d+)"', cell_data)]
        assert ordinals == [0, 1, 2, 3]
        assert "100" in cell_data
        assert "200" in cell_data
        assert "150" in cell_data
        assert "250" in cell_data

    def test_multiple_measures(self):
        from src.dax.mdx_execute import _build_cross_axis_subtotal_cell_data
        rows = [
            {"Amount": 100, "Qty": 10},
            {"Amount": 200, "Qty": 20},
        ]
        row_tuples = [
            [{"uname": "[Cal].[Cal].[Year].&[2024]"}],
            [{"uname": "[Cal].[Cal].[Year].&[2024]"}],
        ]
        col_tuples = [
            [{"uname": "[Geo].[Geo].[Country].&[US]"}],
            [{"uname": "[Geo].[Geo].[Country].&[UK]"}],
        ]
        cell_data = _build_cross_axis_subtotal_cell_data(
            rows, row_tuples, col_tuples, ["Amount", "Qty"], None,
        )
        import re
        ordinals = [int(m.group(1)) for m in re.finditer(r'CellOrdinal="(\d+)"', cell_data)]
        # 1 row x 2 cols x 2 measures = ordinals 0,1,2,3
        assert ordinals == [0, 1, 2, 3]

    def test_deduplicate_axis_tuples(self):
        from src.dax.mdx_execute import _deduplicate_axis_tuples
        tuples = [
            [{"uname": "A"}],
            [{"uname": "B"}],
            [{"uname": "A"}],
            [{"uname": "B"}],
        ]
        unique = _deduplicate_axis_tuples(tuples)
        assert len(unique) == 2
        assert unique[0][0]["uname"] == "A"
        assert unique[1][0]["uname"] == "B"

    def test_subtotal_rows_get_correct_ordinals(self):
        """Subtotal/grand-total rows should map to correct grid positions."""
        from src.dax.mdx_execute import _build_cross_axis_subtotal_cell_data
        rows = [
            {"Amount": 100},  # 2024 x US
            {"Amount": 200},  # 2024 x UK
            {"Amount": 300},  # 2024 x All (col subtotal)
            {"Amount": 400},  # All x US (row subtotal)
            {"Amount": 500},  # All x UK (row subtotal)
            {"Amount": 600},  # All x All (grand total)
        ]
        row_tuples = [
            [{"uname": "[Cal].[Cal].[Year].&[2024]"}],
            [{"uname": "[Cal].[Cal].[Year].&[2024]"}],
            [{"uname": "[Cal].[Cal].[Year].&[2024]"}],
            [{"uname": "[Cal].[Cal].[(All)]"}],
            [{"uname": "[Cal].[Cal].[(All)]"}],
            [{"uname": "[Cal].[Cal].[(All)]"}],
        ]
        col_tuples = [
            [{"uname": "[Geo].[Geo].[Country].&[US]"}],
            [{"uname": "[Geo].[Geo].[Country].&[UK]"}],
            [{"uname": "[Geo].[Geo].[(All)]"}],
            [{"uname": "[Geo].[Geo].[Country].&[US]"}],
            [{"uname": "[Geo].[Geo].[Country].&[UK]"}],
            [{"uname": "[Geo].[Geo].[(All)]"}],
        ]
        cell_data = _build_cross_axis_subtotal_cell_data(
            rows, row_tuples, col_tuples, ["Amount"], "Amount",
        )
        import re
        ordinals = [int(m.group(1)) for m in re.finditer(r'CellOrdinal="(\d+)"', cell_data)]
        # 2 unique rows (2024, All) x 3 unique cols (US, UK, All) = 6 cells
        # Row 0 (2024): col 0 (US)=0, col 1 (UK)=1, col 2 (All)=2
        # Row 1 (All):  col 0 (US)=3, col 1 (UK)=4, col 2 (All)=5
        assert ordinals == [0, 1, 2, 3, 4, 5]


# ---------------------------------------------------------------------------
# F-002-01 — Multi-hierarchy subtotals with BOTH hierarchies on the rows axis
# crashed build_real_execute_response with UnboundLocalError on
# col_axis_tuples. This is the standard Excel layout: two hierarchies
# stacked in the Rows area with subtotals on. The tests below drive the
# orchestrating build_real_execute_response end to end (not the helpers in
# isolation) for the rows-only, columns-only and cross-axis shapes, and
# assert the business outcome: every parent subtotal equals the sum of its
# children for an additive measure.
# ---------------------------------------------------------------------------

class TestMultiHierarchySubtotalAxisShapes:

    _NS = "{urn:schemas-microsoft-com:xml-analysis:mddataset}"

    @staticmethod
    def _make_hier(name, mdx_dim, mdx_hier, levels, axis):
        from src.dax.subtotal_engine import SubtotalHierarchy, SubtotalLevel
        return SubtotalHierarchy(
            hierarchy_name=name,
            mdx_dim_name=mdx_dim,
            mdx_hier_name=mdx_hier,
            levels=[SubtotalLevel(name=n, ordinal=o, dim_name=d) for n, o, d in levels],
            axis=axis,
        )

    @classmethod
    def _merged_rows(cls):
        """Detail + per-country subtotal + grand-total rows, as produced by
        merge_multi_hierarchy_results for Geography x Product on rows."""
        from src.dax.subtotal_engine import SUBTOTAL_GRAIN_PREFIX
        g = SUBTOTAL_GRAIN_PREFIX
        return [
            {"country": "France",  "category": "Bikes", "Amount": 100, g + "Geo": 0,  g + "Prod": 0},
            {"country": "France",  "category": "Cars",  "Amount": 200, g + "Geo": 0,  g + "Prod": 0},
            {"country": "France",  "category": "",      "Amount": 300, g + "Geo": 0,  g + "Prod": -1},
            {"country": "Germany", "category": "Bikes", "Amount": 50,  g + "Geo": 0,  g + "Prod": 0},
            {"country": "Germany", "category": "",      "Amount": 50,  g + "Geo": 0,  g + "Prod": -1},
            {"country": "",        "category": "",      "Amount": 350, g + "Geo": -1, g + "Prod": -1},
        ]

    def _build(self, mdx, hierarchies):
        return build_real_execute_response(
            mdx=mdx,
            catalog="demo",
            columns=["country", "category", "Amount"],
            rows=self._merged_rows(),
            measures_meta=[{"name": "Amount", "default_agg": "sum"}],
            dimensions_meta=[{"name": "country"}, {"name": "category"}],
            client_app_name="Excel",
            subtotal_hierarchies=hierarchies,
        )

    def _parse(self, xml):
        """Return ({axis_name: [tuple captions]}, {ordinal: value})."""
        import xml.etree.ElementTree as ET
        root = ET.fromstring(xml)
        axes: dict[str, list[list[str]]] = {}
        for axis in root.iter(self._NS + "Axis"):
            tuples = []
            for t in axis.iter(self._NS + "Tuple"):
                tuples.append([
                    m.findtext(self._NS + "Caption")
                    for m in t.iter(self._NS + "Member")
                ])
            axes[axis.get("name")] = tuples
        cells = {}
        for c in root.iter(self._NS + "Cell"):
            cells[int(c.get("CellOrdinal"))] = float(
                c.findtext(self._NS + "Value")
            )
        return axes, cells

    _ROWS_ONLY_MDX = """SELECT {[Measures].[Amount]} ON COLUMNS,
    CrossJoin([Geography].[Geo].MEMBERS, [Product].[Prod].MEMBERS) ON ROWS
    FROM [demo]"""

    def test_two_hierarchies_on_rows_axis_builds_well_formed_response(self):
        """The exact Excel layout from the F-002-01 report: two subtotal
        hierarchies, both axis=1. Previously raised UnboundLocalError."""
        h_geo = self._make_hier("Geo", "Geography", "Geo",
                                [("Country", 0, "country")], axis=1)
        h_prod = self._make_hier("Prod", "Product", "Prod",
                                 [("Category", 0, "category")], axis=1)

        xml = self._build(self._ROWS_ONLY_MDX, [h_geo, h_prod])

        axes, cells = self._parse(xml)  # also asserts well-formed XML
        # One tuple per merged result row on the rows axis.
        assert len(axes["Axis1"]) == 6
        # Measures stay on the columns axis.
        assert ["Amount"] in axes["Axis0"]
        # Every merged row produced a cell.
        assert len(cells) == 6

    def test_two_hierarchies_on_rows_subtotals_equal_sum_of_children(self):
        """Business outcome: each parent subtotal must equal the sum of its
        children for an additive measure, and cells must line up with the
        axis tuples they belong to."""
        h_geo = self._make_hier("Geo", "Geography", "Geo",
                                [("Country", 0, "country")], axis=1)
        h_prod = self._make_hier("Prod", "Product", "Prod",
                                 [("Category", 0, "category")], axis=1)

        xml = self._build(self._ROWS_ONLY_MDX, [h_geo, h_prod])
        axes, cells = self._parse(xml)

        # Rows-only layout with a single measure column: cell ordinal i
        # belongs to Axis1 tuple i.
        value_by_tuple = {
            tuple(caps): cells[i] for i, caps in enumerate(axes["Axis1"])
        }
        assert value_by_tuple[("France", "Bikes")] == 100.0
        assert value_by_tuple[("France", "Cars")] == 200.0
        # France subtotal = Bikes + Cars
        assert value_by_tuple[("France", "All")] == 300.0
        assert value_by_tuple[("Germany", "Bikes")] == 50.0
        assert value_by_tuple[("Germany", "All")] == 50.0
        # Grand total = France subtotal + Germany subtotal
        assert value_by_tuple[("All", "All")] == 350.0

    def test_two_hierarchies_on_columns_axis_builds_well_formed_response(self):
        """Mirror case: both subtotal hierarchies on the columns axis."""
        mdx = """SELECT CrossJoin([Geography].[Geo].MEMBERS, [Product].[Prod].MEMBERS) ON COLUMNS,
        {[Measures].[Amount]} ON ROWS
        FROM [demo]"""
        h_geo = self._make_hier("Geo", "Geography", "Geo",
                                [("Country", 0, "country")], axis=0)
        h_prod = self._make_hier("Prod", "Product", "Prod",
                                 [("Category", 0, "category")], axis=0)

        xml = self._build(mdx, [h_geo, h_prod])

        axes, cells = self._parse(xml)
        assert len(axes["Axis0"]) == 6
        assert len(cells) == 6

    def test_cross_axis_hierarchies_still_build_after_fix(self):
        """Equivalence guard: one hierarchy per axis (the shape the old
        guards were written for) must keep working identically."""
        mdx = """SELECT [Product].[Prod].MEMBERS ON COLUMNS,
        [Geography].[Geo].MEMBERS ON ROWS
        FROM [demo]"""
        h_geo = self._make_hier("Geo", "Geography", "Geo",
                                [("Country", 0, "country")], axis=1)
        h_prod = self._make_hier("Prod", "Product", "Prod",
                                 [("Category", 0, "category")], axis=0)

        xml = self._build(mdx, [h_geo, h_prod])
        axes, cells = self._parse(xml)

        # Unique members per axis: rows France/Germany/All, cols Bikes/Cars/All.
        assert len(axes["Axis1"]) == 3
        assert len(axes["Axis0"]) == 3
        # Grand total cell present with the correct value: row All x col All
        # = last row position x last col position = ordinal 2*3 + 2 = 8.
        assert cells[8] == 350.0

    def test_ambiguous_member_captions_stay_distinct_and_cells_aligned(self):
        """Two-level date hierarchy crossed with geography, both on rows.
        Month "4" exists under both 2025 and 2026. Without path-qualified
        unames the two month members collide, tuple deduplication collapses
        them, and every later cell shifts against the axis — silently wrong
        subtotals (observed live before the fix: 3438 cells vs 3429 tuples).
        """
        from src.dax.subtotal_engine import SUBTOTAL_GRAIN_PREFIX
        g = SUBTOTAL_GRAIN_PREFIX

        h_geo = self._make_hier("Geo", "Geography", "Geo",
                                [("Country", 0, "country")], axis=1)
        h_cal = self._make_hier("Cal", "Calendar", "Cal",
                                [("Year", 0, "year"), ("Month", 1, "month")],
                                axis=1)
        mdx = """SELECT {[Measures].[Amount]} ON COLUMNS,
        CrossJoin([Geography].[Geo].MEMBERS, [Calendar].[Cal].MEMBERS) ON ROWS
        FROM [demo]"""
        rows = [
            {"country": "France", "year": "2025", "month": "4",  "Amount": 10,  g + "Geo": 0,  g + "Cal": 1},
            {"country": "France", "year": "2025", "month": "5",  "Amount": 20,  g + "Geo": 0,  g + "Cal": 1},
            {"country": "France", "year": "2025", "month": "",   "Amount": 30,  g + "Geo": 0,  g + "Cal": 0},
            {"country": "France", "year": "2026", "month": "4",  "Amount": 40,  g + "Geo": 0,  g + "Cal": 1},
            {"country": "France", "year": "2026", "month": "",   "Amount": 40,  g + "Geo": 0,  g + "Cal": 0},
            {"country": "France", "year": "",     "month": "",   "Amount": 70,  g + "Geo": 0,  g + "Cal": -1},
            {"country": "",       "year": "",     "month": "",   "Amount": 70,  g + "Geo": -1, g + "Cal": -1},
        ]
        xml = build_real_execute_response(
            mdx=mdx,
            catalog="demo",
            columns=["country", "year", "month", "Amount"],
            rows=rows,
            measures_meta=[{"name": "Amount", "default_agg": "sum"}],
            dimensions_meta=[{"name": "country"}, {"name": "year"}, {"name": "month"}],
            client_app_name="Excel",
            subtotal_hierarchies=[h_geo, h_cal],
        )
        axes, cells = self._parse(xml)

        # Month 4-2025 and month 4-2026 must remain distinct tuples:
        # one axis position per result row, one cell per result row.
        assert len(axes["Axis1"]) == 7
        assert len(cells) == 7
        # Cells line up with their tuples; month-level values per year are
        # distinguishable and year subtotals equal the sum of their months.
        value_by_pos = dict(cells)
        captions = [tuple(c) for c in axes["Axis1"]]
        assert value_by_pos[captions.index(("France", "4"))] in (10.0, 40.0)
        assert value_by_pos[captions.index(("France", "2025"))] == 30.0
        assert value_by_pos[captions.index(("France", "2026"))] == 40.0
        assert value_by_pos[captions.index(("France", "All"))] == 70.0
        assert value_by_pos[captions.index(("All", "All"))] == 70.0
        # The two month-4 members carry path-qualified unames
        # (& is XML-escaped in the raw response).
        assert "[Calendar].[Cal].[Month].&amp;[2025]&amp;[4]" in xml
        assert "[Calendar].[Cal].[Month].&amp;[2026]&amp;[4]" in xml
        # Exact per-position check for both month-4 cells.
        import xml.etree.ElementTree as ET
        root = ET.fromstring(xml)
        month4_positions = {}
        for axis in root.iter(self._NS + "Axis"):
            if axis.get("name") != "Axis1":
                continue
            for i, t in enumerate(axis.iter(self._NS + "Tuple")):
                for m in t.iter(self._NS + "Member"):
                    uname = m.findtext(self._NS + "UName")
                    if uname and "[Month].&[2025]&[4]" in uname:
                        month4_positions["2025"] = i
                    if uname and "[Month].&[2026]&[4]" in uname:
                        month4_positions["2026"] = i
        assert value_by_pos[month4_positions["2025"]] == 10.0
        assert value_by_pos[month4_positions["2026"]] == 40.0


# ---------------------------------------------------------------------------
# B8 round 2 (deep-review Finding 2) — the single-hierarchy subtotal builder
# must use the same path-qualified uname grammar and stable per-member
# ordinals as the multi-hierarchy builder. Previously [Cal].[Cal].[Month].&[4]
# was emitted for month 4 of both 2025 and 2026, with raw-row-index ordinals —
# one member identity with two contradictory parents and ordinals (violates
# the Bug-XMLA-003 MSOLAP uniqueness invariant).
# ---------------------------------------------------------------------------

class TestSingleHierarchySubtotalUnames:

    _NS = "{urn:schemas-microsoft-com:xml-analysis:mddataset}"

    @staticmethod
    def _make_hier():
        from src.dax.subtotal_engine import SubtotalHierarchy, SubtotalLevel
        return SubtotalHierarchy(
            hierarchy_name="Cal",
            mdx_dim_name="Calendar",
            mdx_hier_name="Cal",
            levels=[
                SubtotalLevel(name="Year", ordinal=0, dim_name="year"),
                SubtotalLevel(name="Month", ordinal=1, dim_name="month"),
            ],
            axis=1,
        )

    @staticmethod
    def _rows():
        from src.dax.subtotal_engine import SUBTOTAL_GRAIN_KEY
        g = SUBTOTAL_GRAIN_KEY
        return [
            {"year": "2025", "month": "4", "Amount": 10, g: 1},
            {"year": "2025", "month": "5", "Amount": 20, g: 1},
            {"year": "2025", "month": "",  "Amount": 30, g: 0},
            {"year": "2026", "month": "4", "Amount": 40, g: 1},
            {"year": "2026", "month": "",  "Amount": 40, g: 0},
            {"year": "",     "month": "",  "Amount": 70, g: -1},
        ]

    def test_duplicate_captions_get_distinct_path_qualified_unames(self):
        from src.dax.mdx_execute import _build_subtotal_row_members

        members = _build_subtotal_row_members(
            self._rows(), self._make_hier(), "Calendar", "Cal",
        )
        unames = [m["uname"] for m in members]
        # No two members may share a unique name.
        assert len(unames) == len(set(unames))
        assert "[Calendar].[Cal].[Month].&[2025]&[4]" in unames
        assert "[Calendar].[Cal].[Month].&[2026]&[4]" in unames
        # Each month-4 member carries its own year as parent.
        by_uname = {m["uname"]: m for m in members}
        assert (
            by_uname["[Calendar].[Cal].[Month].&[2025]&[4]"]["parent"]
            == "[Calendar].[Cal].[Year].&[2025]"
        )
        assert (
            by_uname["[Calendar].[Cal].[Month].&[2026]&[4]"]["parent"]
            == "[Calendar].[Cal].[Year].&[2026]"
        )
        # Captions stay the plain member value.
        assert by_uname["[Calendar].[Cal].[Month].&[2025]&[4]"]["caption"] == "4"

    def test_member_ordinals_are_stable_per_distinct_member(self):
        """Bug-XMLA-003 invariant: a repeated member must carry the same
        ordinal everywhere it appears; distinct members carry distinct
        ordinals."""
        from src.dax.mdx_execute import _build_subtotal_row_members

        rows = self._rows() + self._rows()[:1]  # repeat the first row
        members = _build_subtotal_row_members(
            rows, self._make_hier(), "Calendar", "Cal",
        )
        ordinal_by_uname: dict[str, set[int]] = {}
        for m in members:
            ordinal_by_uname.setdefault(m["uname"], set()).add(m["member_ordinal"])
        # Same member -> always the same ordinal.
        assert all(len(s) == 1 for s in ordinal_by_uname.values())
        # Distinct members -> distinct ordinals.
        all_ordinals = [next(iter(s)) for s in ordinal_by_uname.values()]
        assert len(all_ordinals) == len(set(all_ordinals))

    def test_cell_values_line_up_with_uname_positions(self):
        """Business outcome end to end: year subtotals equal the sum of
        their months and both month-4 cells carry their own year's value."""
        import xml.etree.ElementTree as ET

        mdx = """SELECT {[Measures].[Amount]} ON COLUMNS,
        [Calendar].[Cal].MEMBERS ON ROWS
        FROM [demo]"""
        xml = build_real_execute_response(
            mdx=mdx,
            catalog="demo",
            columns=["year", "month", "Amount"],
            rows=self._rows(),
            measures_meta=[{"name": "Amount", "default_agg": "sum"}],
            dimensions_meta=[{"name": "year"}, {"name": "month"}],
            client_app_name="Excel",
            subtotal_hierarchy=self._make_hier(),
        )
        root = ET.fromstring(xml)
        cells = {
            int(c.get("CellOrdinal")): float(c.findtext(self._NS + "Value"))
            for c in root.iter(self._NS + "Cell")
        }
        pos_by_uname = {}
        for axis in root.iter(self._NS + "Axis"):
            if axis.get("name") != "Axis1":
                continue
            for i, t in enumerate(axis.iter(self._NS + "Tuple")):
                for m in t.iter(self._NS + "Member"):
                    pos_by_uname[m.findtext(self._NS + "UName")] = i
        assert cells[pos_by_uname["[Calendar].[Cal].[Month].&[2025]&[4]"]] == 10.0
        assert cells[pos_by_uname["[Calendar].[Cal].[Month].&[2026]&[4]"]] == 40.0
        # Year subtotals = sum of their months; grand total = sum of years.
        assert cells[pos_by_uname["[Calendar].[Cal].[Year].&[2025]"]] == 30.0
        assert cells[pos_by_uname["[Calendar].[Cal].[Year].&[2026]"]] == 40.0
        # Bug-5433: the (All) MEMBER uname is [Hier].[All] ([(All)] is the level),
        # aligned with DISCOVER.
        assert cells[pos_by_uname["[Calendar].[Cal].[All]"]] == 70.0


# ---------------------------------------------------------------------------
# B8 round 2 (deep-review Finding 3) — mixed shape: two subtotal hierarchies
# on ROWS + a flat (non-subtotal) dimension crossjoined with the measure on
# COLUMNS. Pre-F-002-01 this crashed; post-fix it emitted duplicate
# CellOrdinals (every measure cell of a row landed at the same column
# position regardless of the row's column-dim value) and an empty Axis0.
# ---------------------------------------------------------------------------

class TestMixedShapeFlatColumnDim:

    _NS = "{urn:schemas-microsoft-com:xml-analysis:mddataset}"

    @staticmethod
    def _make_hier(name, mdx_dim, mdx_hier, levels, axis):
        from src.dax.subtotal_engine import SubtotalHierarchy, SubtotalLevel
        return SubtotalHierarchy(
            hierarchy_name=name,
            mdx_dim_name=mdx_dim,
            mdx_hier_name=mdx_hier,
            levels=[SubtotalLevel(name=n, ordinal=o, dim_name=d) for n, o, d in levels],
            axis=axis,
        )

    @classmethod
    def _merged_rows(cls):
        """Detail + subtotal + grand-total rows, every row carrying its
        channel value (grain queries GROUP BY non-hierarchy dims)."""
        from src.dax.subtotal_engine import SUBTOTAL_GRAIN_PREFIX
        g = SUBTOTAL_GRAIN_PREFIX
        return [
            {"country": "France",  "category": "Bikes", "channel": "Web",   "Amount": 10, g + "Geo": 0,  g + "Prod": 0},
            {"country": "France",  "category": "Bikes", "channel": "Store", "Amount": 20, g + "Geo": 0,  g + "Prod": 0},
            {"country": "France",  "category": "Cars",  "channel": "Web",   "Amount": 5,  g + "Geo": 0,  g + "Prod": 0},
            {"country": "France",  "category": "",      "channel": "Web",   "Amount": 15, g + "Geo": 0,  g + "Prod": -1},
            {"country": "France",  "category": "",      "channel": "Store", "Amount": 20, g + "Geo": 0,  g + "Prod": -1},
            {"country": "",        "category": "",      "channel": "Web",   "Amount": 15, g + "Geo": -1, g + "Prod": -1},
            {"country": "",        "category": "",      "channel": "Store", "Amount": 20, g + "Geo": -1, g + "Prod": -1},
        ]

    def _build(self):
        mdx = """SELECT CrossJoin({[channel].[channel].[channel].Members}, {[Measures].[Amount]}) ON COLUMNS,
        CrossJoin([Geography].[Geo].MEMBERS, [Product].[Prod].MEMBERS) ON ROWS
        FROM [demo]"""
        h_geo = self._make_hier("Geo", "Geography", "Geo",
                                [("Country", 0, "country")], axis=1)
        h_prod = self._make_hier("Prod", "Product", "Prod",
                                 [("Category", 0, "category")], axis=1)
        return build_real_execute_response(
            mdx=mdx,
            catalog="demo",
            columns=["country", "category", "channel", "Amount"],
            rows=self._merged_rows(),
            measures_meta=[{"name": "Amount", "default_agg": "sum"}],
            dimensions_meta=[
                {"name": "country"}, {"name": "category"}, {"name": "channel"},
            ],
            client_app_name="Excel",
            subtotal_hierarchies=[h_geo, h_prod],
        )

    def _parse(self, xml):
        import xml.etree.ElementTree as ET
        root = ET.fromstring(xml)
        axes: dict[str, list[list[str]]] = {}
        for axis in root.iter(self._NS + "Axis"):
            tuples = []
            for t in axis.iter(self._NS + "Tuple"):
                tuples.append([
                    m.findtext(self._NS + "Caption")
                    for m in t.iter(self._NS + "Member")
                ])
            axes[axis.get("name")] = tuples
        cells = {}
        for c in root.iter(self._NS + "Cell"):
            cells[int(c.get("CellOrdinal"))] = float(
                c.findtext(self._NS + "Value")
            )
        return axes, cells

    def test_column_axis_carries_channel_by_measure_tuples(self):
        """Axis0 must render (channel, measure) tuples — it rendered empty
        before this fix."""
        axes, _ = self._parse(self._build())
        assert axes["Axis0"] == [["Web", "Amount"], ["Store", "Amount"]]

    def test_cells_align_one_to_one_with_tuple_grid_no_duplicate_ordinals(self):
        import re as _re
        xml = self._build()
        axes, cells = self._parse(xml)
        ordinals = [
            int(m.group(1)) for m in _re.finditer(r'CellOrdinal="(\d+)"', xml)
        ]
        assert len(ordinals) == len(set(ordinals)), "duplicate CellOrdinals"
        # 4 deduplicated row tuples x 2 column tuples; 7 result rows -> 7 cells.
        assert len(axes["Axis1"]) == 4
        assert len(cells) == 7

    def test_cell_values_hand_verified_per_row_and_column(self):
        """ordinal = row_pos * 2 + col_pos with rows (France,Bikes)=0,
        (France,Cars)=1, (France,All)=2, (All,All)=3 and cols Web=0, Store=1."""
        _, cells = self._parse(self._build())
        assert cells[0] == 10.0   # France/Bikes  x Web
        assert cells[1] == 20.0   # France/Bikes  x Store
        assert cells[2] == 5.0    # France/Cars   x Web
        assert 3 not in cells     # France/Cars   x Store — no data row
        assert cells[4] == 15.0   # France subtotal x Web
        assert cells[5] == 20.0   # France subtotal x Store
        assert cells[6] == 15.0   # Grand total   x Web
        assert cells[7] == 20.0   # Grand total   x Store


# ---------------------------------------------------------------------------
# B8 round 2 (deep-review Finding 5) — duplicate per-row tuple keys must not
# emit two cells at the same ordinal: the duplicate is skipped and logged.
# ---------------------------------------------------------------------------

class TestDuplicateTupleKeyGuard:

    def test_duplicate_row_tuple_keys_emit_one_cell_and_warn(self, caplog):
        import logging as _logging
        from src.dax.mdx_execute import _build_subtotal_cell_data

        rows = [{"Amount": 100}, {"Amount": 999}]
        per_row_tuples = [
            [{"uname": "[Geo].[Geo].[Country].&[France]"}],
            [{"uname": "[Geo].[Geo].[Country].&[France]"}],  # duplicate key
        ]
        with caplog.at_level(_logging.WARNING, logger="src.dax.mdx_execute"):
            cell_data = _build_subtotal_cell_data(
                rows, [], ["Amount"], "Amount",
                per_row_tuples=per_row_tuples,
            )
        assert cell_data.count("<Cell ") == 1
        assert "100" in cell_data and "999" not in cell_data
        assert any("Duplicate CellOrdinal" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# F-002-10 — NULL/empty dimension members surface as a stable (blank) member
# ---------------------------------------------------------------------------

class TestBlankDimensionMembers:
    def test_null_member_kept_as_blank_and_addressable(self):
        from src.dax.mdx_execute import build_real_execute_response, BLANK_MEMBER

        mdx = (
            "SELECT {[Measures].[Amount]} ON COLUMNS, "
            "[Country].[Country].[Country].Members ON ROWS FROM [demo]"
        )
        columns = ["Country", "Amount"]
        rows = [
            {"Country": "France", "Amount": 300},
            {"Country": None, "Amount": 120},   # NULL dimension member
        ]
        measures_meta = [{"name": "Amount", "default_agg": "sum"}]
        dimensions_meta = [{"name": "Country"}]

        xml = build_real_execute_response(
            mdx=mdx, catalog="demo", columns=columns, rows=rows,
            measures_meta=measures_meta, dimensions_meta=dimensions_meta,
        )
        # The blank member must appear on the axis (not be dropped) and both
        # cell values must be present so totals reconcile.
        assert BLANK_MEMBER in xml
        assert "120" in xml
        assert "300" in xml

    def test_empty_string_member_normalised_in_place(self):
        from src.dax.mdx_execute import build_real_execute_response, BLANK_MEMBER

        mdx = (
            "SELECT {[Measures].[Amount]} ON COLUMNS, "
            "[Country].[Country].[Country].Members ON ROWS FROM [demo]"
        )
        rows = [{"Country": "", "Amount": 50}]
        xml = build_real_execute_response(
            mdx=mdx, catalog="demo", columns=["Country", "Amount"], rows=rows,
            measures_meta=[{"name": "Amount", "default_agg": "sum"}],
            dimensions_meta=[{"name": "Country"}],
        )
        assert rows[0]["Country"] == BLANK_MEMBER
        assert BLANK_MEMBER in xml


# ---------------------------------------------------------------------------
# F-002-13 — info-measure reference detection (mixed-pivot post-join input)
# ---------------------------------------------------------------------------

class TestReferencedInfoMeasures:
    def test_detects_display_and_internal_names(self):
        from src.dax.xmla_server import _referenced_info_measures

        mdx = (
            "SELECT {[Measures].[Sales], [Measures].[Last Refreshed]} ON COLUMNS, "
            "[Country].[Country].[Country].Members ON ROWS FROM [demo]"
        )
        assert _referenced_info_measures(mdx) == ["_info_last_refreshed"]

    def test_no_info_measure_returns_empty(self):
        from src.dax.xmla_server import _referenced_info_measures

        mdx = "SELECT {[Measures].[Sales]} ON COLUMNS FROM [demo]"
        assert _referenced_info_measures(mdx) == []

    def test_dedupes_repeated_reference(self):
        from src.dax.xmla_server import _referenced_info_measures

        mdx = (
            "SELECT {[Measures].[_info_owner], [Measures].[Owner]} ON COLUMNS "
            "FROM [demo]"
        )
        assert _referenced_info_measures(mdx) == ["_info_owner"]


# ---------------------------------------------------------------------------
# Bug-5431 — TREE_OP parent(2) / siblings(4) / ancestors(32)
# ---------------------------------------------------------------------------

def _geo3_args(tree_op: str):
    """_rows_members args for a 3-level Region>Country>City hierarchy, filtered
    on the canonical City member London (EMEA>UK>London) with the given TREE_OP."""
    return dict(
        catalog="m",
        measures=[{"name": "base_amount"}],
        dimensions=[{
            "name": "geo3",
            "source": "hierarchy",
            "levels": [
                {"ordinal": 0, "name": "Region"},
                {"ordinal": 1, "name": "Country"},
                {"ordinal": 2, "name": "City"},
            ],
        }],
        restrictions={
            "HIERARCHY_UNIQUE_NAME": ["[geo3].[geo3]"],
            "LEVEL_UNIQUE_NAME": ["[geo3].[geo3].[City]"],
            "MEMBER_UNIQUE_NAME": ["[geo3].[geo3].[City].&[EMEA]&[UK]&[London]"],
            "TREE_OP": [tree_op],
        },
        member_data={
            "geo3": {
                "levels": ["Region", "Country", "City"],
                "members_by_level": {
                    "0": [{"name": "EMEA", "ordinal": 0, "parent": ""}],
                    "1": [
                        {"name": "UK", "ordinal": 0, "parent": "EMEA"},
                        {"name": "FR", "ordinal": 1, "parent": "EMEA"},
                    ],
                    "2": [
                        {"name": "London", "ordinal": 0, "parent": "UK"},
                        {"name": "Manchester", "ordinal": 1, "parent": "UK"},
                        {"name": "Paris", "ordinal": 2, "parent": "FR"},
                    ],
                },
            }
        },
    )


def _unames(rows):
    return [r["MEMBER_UNIQUE_NAME"] for r in rows
            if r.get("HIERARCHY_UNIQUE_NAME") == "[geo3].[geo3]"]


def test_tree_op_parent_returns_immediate_parent():
    rows = _rows_members(**_geo3_args("2"))  # PARENT
    assert _unames(rows) == ["[geo3].[geo3].[Country].&[EMEA]&[UK]"]


def test_tree_op_ancestors_returns_full_chain():
    rows = _rows_members(**_geo3_args("32"))  # ANCESTORS
    assert set(_unames(rows)) == {
        "[geo3].[geo3].[Region].&[EMEA]",
        "[geo3].[geo3].[Country].&[EMEA]&[UK]",
    }


def test_tree_op_siblings_returns_same_parent_members():
    rows = _rows_members(**_geo3_args("4"))  # SIBLINGS
    got = set(_unames(rows))
    assert "[geo3].[geo3].[City].&[EMEA]&[UK]&[London]" in got
    assert "[geo3].[geo3].[City].&[EMEA]&[UK]&[Manchester]" in got
    # Paris is under FR, not a sibling.
    assert "[geo3].[geo3].[City].&[EMEA]&[FR]&[Paris]" not in got


def test_tree_op_parent_then_self_includes_both():
    rows = _rows_members(**_geo3_args("10"))  # PARENT(2) | SELF(8)
    got = set(_unames(rows))
    assert "[geo3].[geo3].[Country].&[EMEA]&[UK]" in got
    assert "[geo3].[geo3].[City].&[EMEA]&[UK]&[London]" in got


# ---------------------------------------------------------------------------
# Bug-5432 — cell FmtValue / FORMATTED_VALUE formatter
# ---------------------------------------------------------------------------
from src.dax.mdx_execute import _format_cell_value


def test_format_cell_value_named_formats():
    assert _format_cell_value(1234.5, "Standard") == "1,234.50"
    assert _format_cell_value(0.1234, "Percent") == "12.34%"
    assert _format_cell_value(1234.5, "Currency") == "$1,234.50"


def test_format_cell_value_patterns():
    assert _format_cell_value(1234.567, "#,##0.00") == "1,234.57"
    assert _format_cell_value(1234.0, "#,##0") == "1,234"
    assert _format_cell_value(0.5, "0.0%") == "50.0%"
    assert _format_cell_value(1234.5, "$#,##0.00") == "$1,234.50"
    assert _format_cell_value(42.0, "0") == "42"


def test_format_cell_value_none_when_no_or_general_format():
    assert _format_cell_value(5.0, "") is None
    assert _format_cell_value(5.0, None) is None
    assert _format_cell_value(5.0, "General Number") is None


def test_format_cell_value_non_numeric_returns_none():
    assert _format_cell_value("abc", "Standard") is None


def test_format_cell_value_multi_section_uses_positive_section():
    # Bug-5432 deep-review: .NET positive;negative sections must not pollute the
    # decimal count or hide the trailing %.
    assert _format_cell_value(1234.567, "#,##0.00;(#,##0.00)") == "1,234.57"
    assert _format_cell_value(0.5, "0.00%;(0.00%)") == "50.00%"
    assert _format_cell_value(5.0, '#,##0.00;-#,##0.00;"zero"') == "5.00"


# ---------------------------------------------------------------------------
# Bug-5432 — measure format TOKEN -> SSAS/.NET FORMAT_STRING resolver
# ---------------------------------------------------------------------------
from shared.schemas.measure_formats import format_token_to_mdx


def test_format_token_to_mdx_known_tokens():
    assert format_token_to_mdx("percent_2dp") == "0.00%"
    assert format_token_to_mdx("currency") == "$#,##0.00"
    assert format_token_to_mdx("integer") == "#,##0"
    assert format_token_to_mdx("decimal_2dp") == "#,##0.00"


def test_format_token_to_mdx_passthrough_and_none():
    # A literal .NET format string passes through unchanged.
    assert format_token_to_mdx("0.00%") == "0.00%"
    assert format_token_to_mdx("$#,##0") == "$#,##0"
    # Empty / unknown non-format tokens yield None (caller omits formatting).
    assert format_token_to_mdx("") is None
    assert format_token_to_mdx(None) is None
    assert format_token_to_mdx("mystery") is None


def test_format_token_end_to_end_fmtvalue():
    # The gateway path: token -> format string -> formatted cell value.
    assert _format_cell_value(0.1234, format_token_to_mdx("percent_2dp")) == "12.34%"
    assert _format_cell_value(1234.5, format_token_to_mdx("currency")) == "$1,234.50"
