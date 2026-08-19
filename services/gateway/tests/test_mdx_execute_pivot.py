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


def test_execute_measure_caption_uses_display_name_bug6657():
    """F-002-05 / Bug-6657: the Execute measure axis caption must equal the
    field-list MEASURE_CAPTION (display_name), not the internal measure name.

    F-002-01 / F-103-01 / Bug-9232: Caption rewrite must not nil the cell.
    CellData looks up the UName internal name (``[Measures].[net_sales]``),
    so a non-nil numeric Value (10) is required alongside the friendly caption.
    """
    mdx = """
    SELECT {[Measures].[net_sales]} ON COLUMNS,
           {[Geography].[Geography].[(All)].Members} ON ROWS
    FROM [demo]
    """
    measures_meta = [{"name": "net_sales", "display_name": "Net Sales",
                      "default_agg": "sum"}]
    dimensions_meta = [{"name": "Geography"}]
    columns = ["Geography", "net_sales"]
    rows = [{"Geography": "France", "net_sales": 10}]

    xml = build_real_execute_response(
        mdx=mdx, catalog="demo", columns=columns, rows=rows,
        measures_meta=measures_meta, dimensions_meta=dimensions_meta,
    )
    # Pivot header shows the friendly caption; the internal name is only the UName.
    assert "<Caption>Net Sales</Caption>" in xml
    assert "[Measures].[net_sales]" in xml  # UName keeps the internal name
    assert "<Caption>net_sales</Caption>" not in xml
    assert 'xsi:nil="true"' not in xml
    assert '<Value xsi:type="xsd:double">10.0</Value>' in xml


def test_execute_cell_lookup_uses_uname_not_display_caption_f002_01():
    """F-002-01 / F-103-01: live-shaped display_name with a space (``base amount``)
    must not become the CellData lookup key. Result column is ``base_amount``.
    """
    mdx = """
    SELECT {[Measures].[base_amount]} ON COLUMNS
    FROM [modely]
    """
    measures_meta = [{"name": "base_amount", "display_name": "base amount",
                      "default_agg": "sum"}]
    xml = build_real_execute_response(
        mdx=mdx, catalog="modely",
        columns=["base_amount"],
        rows=[{"base_amount": 10}],
        measures_meta=measures_meta, dimensions_meta=[],
    )
    assert "<Caption>base amount</Caption>" in xml
    assert "[Measures].[base_amount]" in xml
    assert "<Caption>base_amount</Caption>" not in xml
    assert 'xsi:nil="true"' not in xml
    assert '<Value xsi:type="xsd:double">10.0</Value>' in xml


def test_execute_measure_dim_crossjoin_on_columns_emits_cells_bug9246():
    """Bug-9246 / F-002-01 / XLC-01: measure × flat dim CrossJoin on COLUMNS
    must emit Axis0 tuples and numeric cells, not empty ``<Tuples></Tuples>``
    plus ``xsi:nil``.

    Pre-fix, ``_build_existing_axis_tuples`` looked up ``row.get("Measures")``
    (always None) and returned ``[]``; Axis0 rendered empty and CellData
    missed the Gender keys. Caption→UName cell lookup (Bug-6657) stays.
    """
    import re
    mdx = """
    SELECT NON EMPTY CrossJoin({[Measures].[base_amount]}, {[Gender].[Gender].[(All)].Members}) ON COLUMNS FROM [modely]
    """
    xml = build_real_execute_response(
        mdx=mdx, catalog="modely",
        columns=["Gender", "base_amount"],
        rows=[
            {"Gender": "F", "base_amount": 100.0},
            {"Gender": "M", "base_amount": 200.0},
        ],
        measures_meta=[{"name": "base_amount", "display_name": "base amount",
                        "default_agg": "sum"}],
        dimensions_meta=[{"name": "Gender"}],
    )

    axis0 = re.search(r'<Axis name="Axis0">(.*?)</Axis>', xml, re.DOTALL)
    assert axis0 is not None, "Axis0 missing from response"
    assert "<Tuples></Tuples>" not in axis0.group(1)
    tuples = re.findall(r'<Tuple>(.*?)</Tuple>', axis0.group(1), re.DOTALL)
    caption_sets = {
        frozenset(re.findall(r'<Caption>([^<]*)</Caption>', t))
        for t in tuples
    }
    # CrossJoin({Measures}, {Gender}) emits (measure, gender); equivalent
    # (gender, measure) order is also accepted.
    assert frozenset({"F", "base amount"}) in caption_sets
    assert frozenset({"M", "base amount"}) in caption_sets
    assert len(tuples) == 2

    assert 'xsi:nil="true"' not in xml
    assert xml.count('xsi:type="xsd:double"') == 2
    assert '<Value xsi:type="xsd:double">100.0</Value>' in xml
    assert '<Value xsi:type="xsd:double">200.0</Value>' in xml
    # Bug-6657 caption contract: axis label is display_name, UName is internal.
    assert "[Measures].[base_amount]" in xml
    assert "<Caption>base_amount</Caption>" not in xml


def test_execute_measure_dim_crossjoin_on_rows_emits_cells_bug9246_mirror():
    """Bug-9246 mirror: measure x flat dim CrossJoin on ROWS must emit Axis1
    tuples and numeric cells (direct-caller coverage of the None fallback on
    the row axis)."""
    import re
    mdx = """
    SELECT NON EMPTY CrossJoin({[Measures].[base_amount]}, {[Gender].[Gender].[(All)].Members}) ON ROWS FROM [modely]
    """
    xml = build_real_execute_response(
        mdx=mdx, catalog="modely",
        columns=["Gender", "base_amount"],
        rows=[{"Gender": "F", "base_amount": 100.0},
              {"Gender": "M", "base_amount": 200.0}],
        measures_meta=[{"name": "base_amount", "display_name": "base amount",
                        "default_agg": "sum"}],
        dimensions_meta=[{"name": "Gender"}],
    )
    axis1 = re.search(r'<Axis name="Axis1">(.*?)</Axis>', xml, re.DOTALL)
    assert axis1 is not None, "Axis1 missing from response"
    assert "<Tuples></Tuples>" not in axis1.group(1)
    assert len(re.findall(r'<Tuple>', axis1.group(1))) == 2
    assert 'xsi:nil="true"' not in xml
    assert '<Value xsi:type="xsd:double">100.0</Value>' in xml
    assert '<Value xsi:type="xsd:double">200.0</Value>' in xml


def test_execute_member_caption_uses_display_column_bug6659():
    """F-002-05 / Bug-6659 CONSUMER contract: when result rows carry a projected
    ``<dim>__caption`` companion column, the Execute member axis must emit
    Caption=<display value> and UName=<key>. This tests the CONSUMER side of
    Bug-6659; the PRODUCER side (projecting the caption column through the XMLA
    Execute raw-SQL path) is a separate query-router change and is inert in the
    deployed system (no seeded dimension sets ``display_column_name``). The test
    hand-seeds the companion column to pin the consumer contract.
    must emit Caption=<display value> while the UName keeps the key."""
    mdx = """
    SELECT {[Measures].[Amount]} ON COLUMNS,
           {[Geography].[Geography].[(All)].Members} ON ROWS
    FROM [demo]
    """
    measures_meta = [{"name": "Amount", "default_agg": "sum"}]
    # Geography declares a display column distinct from its key.
    dimensions_meta = [{"name": "Geography",
                        "display_column_name": "country_name"}]
    columns = ["Geography", "Geography__caption", "Amount"]
    rows = [
        {"Geography": "FR", "Geography__caption": "France", "Amount": 10},
        {"Geography": "DE", "Geography__caption": "Germany", "Amount": 20},
    ]

    xml = build_real_execute_response(
        mdx=mdx, catalog="demo", columns=columns, rows=rows,
        measures_meta=measures_meta, dimensions_meta=dimensions_meta,
    )
    # Captions are the friendly display values; UName keeps the raw key.
    assert "<Caption>France</Caption>" in xml
    assert "<Caption>Germany</Caption>" in xml
    assert "[Geography].[FR]" in xml
    assert "[Geography].[DE]" in xml
    # The raw keys must NOT appear as captions.
    assert "<Caption>FR</Caption>" not in xml
    assert "<Caption>DE</Caption>" not in xml


def test_execute_member_caption_absent_falls_back_to_key():
    """F-002-05: with no display column the caption stays the key (no regression)."""
    mdx = """
    SELECT {[Measures].[Amount]} ON COLUMNS,
           {[Geography].[Geography].[(All)].Members} ON ROWS
    FROM [demo]
    """
    xml = build_real_execute_response(
        mdx=mdx, catalog="demo",
        columns=["Geography", "Amount"],
        rows=[{"Geography": "France", "Amount": 10}],
        measures_meta=[{"name": "Amount", "default_agg": "sum"}],
        dimensions_meta=[{"name": "Geography"}],
    )
    assert "<Caption>France</Caption>" in xml


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

    level_unames = [row["LEVEL_UNIQUE_NAME"] for row in rows if row["HIERARCHY_UNIQUE_NAME"] == "[geo_hierarchy].[geo_hierarchy]"]
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

    def test_mixed_shape_measure_caption_uses_display_name_f002_01_bug6657(self):
        """F-002-01 / Bug-6657 mixed-shape: when display_name is set, Axis0
        shows the friendly caption while cells still look up Amount (10.0/20.0).
        Existing mixed-shape tests omit display_name and still expect Caption
        ``Amount``; this sibling pins the caption+value contract together.
        """
        mdx = """SELECT CrossJoin({[channel].[channel].[channel].Members}, {[Measures].[Amount]}) ON COLUMNS,
        CrossJoin([Geography].[Geo].MEMBERS, [Product].[Prod].MEMBERS) ON ROWS
        FROM [demo]"""
        h_geo = self._make_hier("Geo", "Geography", "Geo",
                                [("Country", 0, "country")], axis=1)
        h_prod = self._make_hier("Prod", "Product", "Prod",
                                 [("Category", 0, "category")], axis=1)
        xml = build_real_execute_response(
            mdx=mdx,
            catalog="demo",
            columns=["country", "category", "channel", "Amount"],
            rows=self._merged_rows(),
            measures_meta=[{"name": "Amount", "display_name": "base amount",
                            "default_agg": "sum"}],
            dimensions_meta=[
                {"name": "country"}, {"name": "category"}, {"name": "channel"},
            ],
            client_app_name="Excel",
            subtotal_hierarchies=[h_geo, h_prod],
        )
        assert "<Caption>base amount</Caption>" in xml
        assert "[Measures].[Amount]" in xml
        axes, cells = self._parse(xml)
        measure_captions = {
            cap for tup in axes.get("Axis0", []) for cap in tup
            if cap not in ("Web", "Store")
        }
        assert "base amount" in measure_captions
        assert "Amount" not in measure_captions
        assert cells[0] == 10.0
        assert cells[1] == 20.0
        assert '<Value xsi:type="xsd:double">10.0</Value>' in xml
        assert '<Value xsi:type="xsd:double">20.0</Value>' in xml
        # Populated ordinals 0 and 1 must not be nil cells.
        import re as _re
        for ordinal in (0, 1):
            cell_xml = _re.search(
                rf'<Cell CellOrdinal="{ordinal}">(.*?)</Cell>', xml, _re.DOTALL
            )
            assert cell_xml is not None, f"missing CellOrdinal {ordinal}"
            assert 'xsi:nil="true"' not in cell_xml.group(1)


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


def test_format_cell_value_bug6070_suffix_currency_preserved():
    # Bug-6070: suffix-currency patterns previously dropped the symbol (only a
    # leading $/€/£ was recognised). Both prefix and suffix currency literals of
    # any symbol must survive, and suffix text must not pollute the decimal count.
    assert _format_cell_value(1234.5, "#,##0.00 €") == "1,234.50 €"
    assert _format_cell_value(1234.5, "#,##0.00 kr") == "1,234.50 kr"
    assert _format_cell_value(1235.0, "#,##0 zł") == "1,235 zł"
    assert _format_cell_value(1234.5, "€#,##0.00") == "€1,234.50"
    assert _format_cell_value(1234.5, '#,##0.00" kr"') == "1,234.50 kr"
    # Regression: bare prefix $ and plain patterns still behave.
    assert _format_cell_value(1234.5, "$#,##0.00") == "$1,234.50"
    assert _format_cell_value(1234.567, "#,##0.00") == "1,234.57"
    # Leading-dot fraction (no integer placeholder) keeps its decimals.
    assert _format_cell_value(5.0, ".00") == "5.00"


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


# ---------------------------------------------------------------------------
# Bug-5492: a measure used as a predicate / sort-key operand inside a set
# function (Filter / Order / TopCount ...) must NOT be counted as an axis
# hierarchy. Before the fix, `Filter([dim].Members, [Measures].[m] > N)` on ROWS
# leaked `[Measures]` into the row-hierarchy list, flipped has_measures_axis, and
# corrupted axis/slicer classification so the member tuples were dropped and the
# row axis rendered empty.
# ---------------------------------------------------------------------------

class TestFilterPredicateMeasureNotAxisHierarchy:

    def _hiers(self, expr):
        from src.dax.mdx_execute import _extract_hierarchies
        return _extract_hierarchies(expr)

    # --- the bug: Filter predicate measure must not leak onto the axis -------

    def test_filter_predicate_measure_excluded_from_row_hierarchies(self):
        hiers = self._hiers(
            "Filter([item_category].[item_category].Members, "
            "[Measures].[net_sales] > 480000000)"
        )
        assert hiers == ["[item_category].[item_category]"]
        assert "[Measures]" not in hiers

    def test_filter_predicate_measure_excluded_when_operator_reversed(self):
        # `N < [Measures].[m]` is the same condition written the other way round.
        hiers = self._hiers("Filter([d].[d].Members, 5 < [Measures].[m])")
        assert hiers == ["[d].[d]"]

    def test_filter_measure_versus_measure_predicate_excludes_both(self):
        # `[Measures].[a] > [Measures].[b]` is a condition: neither operand is an
        # axis member, so [Measures] must not leak onto the axis.
        hiers = self._hiers("Filter([d].[d].Members, [Measures].[a] > [Measures].[b])")
        assert hiers == ["[d].[d]"]

    def test_filter_predicate_covers_all_comparison_operators(self):
        for op in (">", ">=", "<", "<=", "=", "<>"):
            hiers = self._hiers(f"Filter([d].[d].Members, [Measures].[m] {op} 5)")
            assert hiers == ["[d].[d]"], op

    # --- genuine axis measures must still be detected ------------------------

    def test_plain_members_stay_clean(self):
        assert self._hiers("[item_category].[item_category].Members") == [
            "[item_category].[item_category]"
        ]

    def test_measure_on_axis_still_detected(self):
        assert self._hiers("{[Measures].[net_sales]}") == ["[Measures]"]

    def test_multiple_measures_on_axis_collapse_to_one_measures_hier(self):
        assert self._hiers("{[Measures].[a], [Measures].[b]}") == ["[Measures]"]

    def test_crossjoin_measure_axis_member_preserved(self):
        # A bare measure argument of CrossJoin is a genuine axis member, not a
        # sort key — it must survive the predicate-stripping.
        assert self._hiers("CrossJoin([d].[d].Members, [Measures].[m])") == [
            "[d].[d]",
            "[Measures]",
        ]

    def test_filter_predicate_plus_real_axis_measure_keeps_measure(self):
        # The predicate measure is excluded but the explicit axis measure stays.
        assert self._hiers(
            "CrossJoin(Filter([d].[d].Members, [Measures].[m] > 5), {[Measures].[m]})"
        ) == ["[d].[d]", "[Measures]"]

    # --- Order / TopCount sort-key measures share the same shape ------------

    def test_order_sort_key_measure_excluded(self):
        assert self._hiers("Order([d].[d].Members, [Measures].[m], BDESC)") == [
            "[d].[d]"
        ]

    def test_topcount_sort_key_measure_excluded(self):
        assert self._hiers("TopCount([d].[d].Members, 10, [Measures].[m])") == [
            "[d].[d]"
        ]

    def test_order_over_filter_excludes_both_operand_measures(self):
        assert self._hiers(
            "Order(Filter([d].[d].Members, [Measures].[x] > 5), [Measures].[m], BDESC)"
        ) == ["[d].[d]"]

    def test_rank_sort_key_excluded_at_deep_nesting_depth(self):
        # The set argument nests two levels (CrossJoin -> Filter); the sort-key
        # measure must still be excluded regardless of head depth.
        assert self._hiers(
            "Order(CrossJoin([a].[a].Members, "
            "Filter([b].[b].Members, [Measures].[z] > 1)), [Measures].[m], BDESC)"
        ) == ["[a].[a]", "[b].[b]"]

    def test_rank_set_argument_measures_preserved(self):
        # A measure-set passed AS the ranked set (not the sort key) is a genuine
        # axis member set and must survive; only the trailing sort key is dropped.
        assert self._hiers(
            "Order({[Measures].[a], [Measures].[b]}, [Measures].[m], BDESC)"
        ) == ["[Measures]"]

    def test_member_name_with_comma_does_not_split_argument(self):
        # A bracketed member name containing a comma must not be split mid-name
        # by the top-level argument scanner — the sort key still strips cleanly.
        assert self._hiers(
            "Order([d].[d].Members, [Measures].[Gross, Net], BDESC)"
        ) == ["[d].[d]"]

    def test_member_name_with_parens_does_not_break_balanced_scan(self):
        # Parens inside a bracketed member name are literal text, not call
        # structure — the balanced-paren span scan must ignore them.
        assert self._hiers(
            "Order([d].[d].Members, [Measures].[Sales (Net)], BDESC)"
        ) == ["[d].[d]"]
        assert self._hiers(
            "Filter([d].[d].Members, [Measures].[Sales (Net)] > 5)"
        ) == ["[d].[d]"]

    # --- Bug-5495: PAREN / FUNCTION-wrapped predicate & sort-key operands -----
    # A wrapped measure that is a comparison operand or a sort key is still a
    # condition, not an axis member, so it must be stripped. A wrapped measure
    # that is an AXIS member (set/tuple, CrossJoin) must be preserved.

    def test_paren_wrapped_predicate_measure_stripped(self):
        # `([Measures].[m]) > 5` — paren-wrapped comparison operand.
        assert self._hiers(
            "Filter([d].[d].Members, ([Measures].[m]) > 5)"
        ) == ["[d].[d]"]

    def test_paren_wrapped_predicate_measure_stripped_operator_reversed(self):
        assert self._hiers(
            "Filter([d].[d].Members, 5 < ([Measures].[m]))"
        ) == ["[d].[d]"]

    def test_function_wrapped_predicate_measure_stripped(self):
        # `CoalesceEmpty([Measures].[m], 0) > 5` — the function call is the
        # predicate operand; the measure inside it is a condition.
        assert self._hiers(
            "Filter([d].[d].Members, CoalesceEmpty([Measures].[m], 0) > 5)"
        ) == ["[d].[d]"]

    def test_function_wrapped_predicate_measure_stripped_operator_reversed(self):
        # The comparison operator precedes the function name.
        assert self._hiers(
            "Filter([d].[d].Members, 5 < CoalesceEmpty([Measures].[m], 0))"
        ) == ["[d].[d]"]

    def test_function_wrapped_predicate_measure_in_multi_arg_function(self):
        # `IIF(cond, [Measures].[m], 0) >= 5` — measure is a non-first argument of
        # a multi-arg scalar function that is itself the predicate operand.
        assert self._hiers(
            "Filter([d].[d].Members, IIF([d].x, [Measures].[m], 0) >= 5)"
        ) == ["[d].[d]"]

    def test_function_wrapped_predicate_covers_all_comparison_operators(self):
        for op in (">", ">=", "<", "<=", "=", "<>"):
            assert self._hiers(
                f"Filter([d].[d].Members, CoalesceEmpty([Measures].[m], 0) {op} 5)"
            ) == ["[d].[d]"], op

    def test_function_wrapped_sort_key_measure_stripped(self):
        # `Order(set, CoalesceEmpty([Measures].[m], 0), BDESC)` — the scalar
        # function-wrapped sort key is not an axis member.
        assert self._hiers(
            "Order([d].[d].Members, CoalesceEmpty([Measures].[m], 0), BDESC)"
        ) == ["[d].[d]"]

    def test_function_wrapped_topcount_sort_key_measure_stripped(self):
        assert self._hiers(
            "TopCount([d].[d].Members, 10, Abs([Measures].[m]))"
        ) == ["[d].[d]"]

    # --- wrapped AXIS measures must be PRESERVED (precision guard) ------------

    def test_set_wrapped_axis_measure_preserved_not_stripped_as_wrapped(self):
        # `{[Measures].[m]}` is a set-wrapped AXIS member, never a predicate/sort
        # operand — it must survive the broadened wrapped-operand strip.
        assert self._hiers("{[Measures].[net_sales]}") == ["[Measures]"]

    def test_crossjoin_function_wrapped_axis_measure_preserved(self):
        # A measure inside an axis set-function (CrossJoin) whose result is NOT
        # compared is a genuine axis member and must be preserved.
        assert self._hiers(
            "CrossJoin([d].[d].Members, {[Measures].[m]})"
        ) == ["[d].[d]", "[Measures]"]

    def test_addcalculatedmembers_wrapped_axis_measure_preserved(self):
        # AddCalculatedMembers is a set function; its measure set is an axis set.
        assert self._hiers(
            "AddCalculatedMembers({[Measures].[a]})"
        ) == ["[Measures]"]

    def test_rank_set_argument_function_wrapped_measures_preserved(self):
        # The ranked SET argument (a measure set) is an axis set; only the trailing
        # sort key is dropped. A function-wrapped sort key strips, the set survives.
        assert self._hiers(
            "Order({[Measures].[a], [Measures].[b]}, CoalesceEmpty([Measures].[m], 0), BDESC)"
        ) == ["[Measures]"]

    def test_function_wrapped_predicate_with_real_axis_measure_keeps_axis(self):
        # The function-wrapped predicate measure is excluded, the explicit
        # set-wrapped axis measure on the CrossJoin is kept.
        assert self._hiers(
            "CrossJoin(Filter([d].[d].Members, CoalesceEmpty([Measures].[m], 0) > 5), "
            "{[Measures].[m]})"
        ) == ["[d].[d]", "[Measures]"]


class TestFilterOnRowsPopulatesMemberAxis:
    """End-to-end: a Filter(...) named-set on ROWS with a measure predicate must
    render a populated member axis (the named-list-display flow), not an empty
    row axis. The measure on COLUMNS stays detected and the cells align."""

    _NS = "{urn:schemas-microsoft-com:xml-analysis:mddataset}"

    def _build(self):
        mdx = (
            "SELECT {[Measures].[net_sales]} ON COLUMNS, "
            "Filter([item_category].[item_category].Members, "
            "[Measures].[net_sales] > 480000000) ON ROWS "
            "FROM [demo]"
        )
        rows = [
            {"item_category": "Electronics", "net_sales": 500000000},
            {"item_category": "Furniture", "net_sales": 490000000},
        ]
        return build_real_execute_response(
            mdx=mdx,
            catalog="demo",
            columns=["item_category", "net_sales"],
            rows=rows,
            measures_meta=[{"name": "net_sales", "default_agg": "sum"}],
            dimensions_meta=[{"name": "item_category"}],
            client_app_name="Excel",
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
        return axes

    def test_row_axis_is_populated_with_filtered_members(self):
        axes = self._parse(self._build())
        # Axis1 (ROWS) must carry the filtered category members, not be empty.
        row_captions = [c for tup in axes.get("Axis1", []) for c in tup]
        assert "Electronics" in row_captions
        assert "Furniture" in row_captions
        assert axes.get("Axis1"), "row axis rendered empty (Bug-5492 regression)"

    def test_measure_on_columns_still_detected(self):
        axes = self._parse(self._build())
        col_captions = [c for tup in axes.get("Axis0", []) for c in tup]
        assert "net_sales" in col_captions


# ---------------------------------------------------------------------------
# Bug-5499: a saved named set referenced BY NAME on an axis must expand into
# its member tuples. The gateway inlines the named set's compiled expression
# into the MDX before axis extraction, so the existing Filter/TopCount/member
# machinery operates on the expanded expression.
# ---------------------------------------------------------------------------

class TestNamedSetInlining:
    """Unit tests for _inline_named_sets: set names in MDX are replaced with
    their compiled expressions."""

    @staticmethod
    def _inline(mdx, named_sets):
        from src.dax.xmla_server import _inline_named_sets
        return _inline_named_sets(mdx, named_sets)

    def test_bracket_quoted_set_name_replaced(self):
        ns = [{"name": "Top Customers", "expression": "TopCount([c].[c].Members, 5, [Measures].[Rev])"}]
        mdx = "SELECT {[Measures].[Rev]} ON COLUMNS, {[Top Customers]} ON ROWS FROM [demo]"
        result = self._inline(mdx, ns)
        assert "TopCount([c].[c].Members, 5, [Measures].[Rev])" in result
        assert "[Top Customers]" not in result

    def test_bare_set_name_replaced(self):
        ns = [{"name": "TopCust", "expression": "TopCount([c].[c].Members, 5, [Measures].[Rev])"}]
        mdx = "SELECT {[Measures].[Rev]} ON COLUMNS, {TopCust} ON ROWS FROM [demo]"
        result = self._inline(mdx, ns)
        assert "TopCount" in result
        assert "TopCust" not in result  # bare name replaced

    def test_hierarchy_path_not_replaced(self):
        """A set name that coincides with a dimension name must not be replaced
        when it appears as part of a [Dim].[Hier] hierarchy path."""
        ns = [{"name": "Geography", "expression": "Filter([Geography].[Geography].Members, [Measures].[Rev] > 100)"}]
        mdx = "SELECT {[Geography].[Geography].Members} ON ROWS FROM [demo]"
        result = self._inline(mdx, ns)
        # The [Geography].[Geography].Members path must survive intact — only
        # a bare [Geography] (not followed by .[) would be inlined.
        assert "[Geography].[Geography].Members" in result

    def test_case_insensitive_replacement(self):
        ns = [{"name": "Top Items", "expression": "TopCount([d].[d].Members, 3, [Measures].[m])"}]
        mdx = "SELECT {[top items]} ON ROWS FROM [demo]"
        result = self._inline(mdx, ns)
        assert "TopCount" in result

    def test_empty_expression_skipped(self):
        ns = [{"name": "EmptySet", "expression": ""}]
        mdx = "SELECT {[EmptySet]} ON ROWS FROM [demo]"
        result = self._inline(mdx, ns)
        # Expression is empty, so replacement is skipped; original survives.
        assert "[EmptySet]" in result

    def test_no_named_sets_returns_unchanged(self):
        mdx = "SELECT {[Measures].[Rev]} ON COLUMNS FROM [demo]"
        assert self._inline(mdx, []) == mdx

    def test_multiple_sets_all_replaced(self):
        ns = [
            {"name": "SetA", "expression": "TopCount([a].[a].Members, 3, [Measures].[m])"},
            {"name": "SetB", "expression": "Filter([b].[b].Members, [Measures].[m] > 0)"},
        ]
        mdx = "SELECT {SetA} ON COLUMNS, {SetB} ON ROWS FROM [demo]"
        result = self._inline(mdx, ns)
        assert "TopCount([a]" in result
        assert "Filter([b]" in result

    def test_cube_name_not_replaced(self):
        """FROM [cube] must not be replaced when a named set has the same name."""
        ns = [{"name": "demo", "expression": "TopCount([c].[c].Members, 5, [Measures].[m])"}]
        mdx = "SELECT {[Measures].[Rev]} ON COLUMNS FROM [demo]"
        result = self._inline(mdx, ns)
        assert "FROM [demo]" in result

    def test_terminal_member_not_replaced(self):
        """[Dim].[Hier].[Member] terminal bracket must not be replaced when a
        named set has the same name as the member."""
        ns = [{"name": "France", "expression": "TopCount([c].[c].Members, 5, [Measures].[m])"}]
        mdx = "SELECT {[Measures].[Rev]} ON COLUMNS FROM [demo] WHERE ([Geography].[Geography].[France])"
        result = self._inline(mdx, ns)
        assert "[Geography].[Geography].[France]" in result

    def test_where_measure_not_replaced(self):
        """WHERE ([Measures].[SetName]) must not be replaced when a named set
        has the same name as a measure."""
        ns = [{"name": "Rev", "expression": "TopCount([c].[c].Members, 5, [Measures].[m])"}]
        mdx = "SELECT {[d].[d].Members} ON COLUMNS FROM [demo] WHERE ([Measures].[Rev])"
        result = self._inline(mdx, ns)
        assert "[Measures].[Rev]" in result

    def test_key_member_not_replaced(self):
        """&[SetName] key member references must not be replaced."""
        ns = [{"name": "TopCust", "expression": "TopCount([c].[c].Members, 5, [Measures].[m])"}]
        mdx = "SELECT {[Measures].[Rev]} ON COLUMNS FROM [demo] WHERE ([Customer].[Customer].&[TopCust])"
        result = self._inline(mdx, ns)
        assert "&[TopCust]" in result


class TestNamedSetOnRowsExpandsMembers:
    """End-to-end: a saved named set (Top-N) referenced on ROWS must expand to
    its member tuples in the MDDataSet response, not produce an empty axis.
    Mirrors TestFilterOnRowsPopulatesMemberAxis for Bug-5492."""

    _NS = "{urn:schemas-microsoft-com:xml-analysis:mddataset}"

    def _build_topn(self):
        """Simulate what happens when a saved TopCount named set is inlined
        and then build_real_execute_response processes the expanded MDX."""
        # The named set expression is inlined before this point; the MDX that
        # reaches build_real_execute_response already contains the expansion.
        mdx = (
            "SELECT {[Measures].[net_sales]} ON COLUMNS, "
            "TopCount([item_category].[item_category].Members, 3, "
            "[Measures].[net_sales]) ON ROWS "
            "FROM [demo]"
        )
        rows = [
            {"item_category": "Electronics", "net_sales": 500000000},
            {"item_category": "Furniture", "net_sales": 490000000},
            {"item_category": "Apparel", "net_sales": 480000000},
        ]
        return build_real_execute_response(
            mdx=mdx,
            catalog="demo",
            columns=["item_category", "net_sales"],
            rows=rows,
            measures_meta=[{"name": "net_sales", "default_agg": "sum"}],
            dimensions_meta=[{"name": "item_category"}],
            client_app_name="Excel",
        )

    def _build_filtered(self):
        """Simulate a saved Filter named set inlined on ROWS."""
        mdx = (
            "SELECT {[Measures].[net_sales]} ON COLUMNS, "
            "Filter([item_category].[item_category].Members, "
            "[Measures].[net_sales] > 480000000) ON ROWS "
            "FROM [demo]"
        )
        rows = [
            {"item_category": "Electronics", "net_sales": 500000000},
            {"item_category": "Furniture", "net_sales": 490000000},
        ]
        return build_real_execute_response(
            mdx=mdx,
            catalog="demo",
            columns=["item_category", "net_sales"],
            rows=rows,
            measures_meta=[{"name": "net_sales", "default_agg": "sum"}],
            dimensions_meta=[{"name": "item_category"}],
            client_app_name="Excel",
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
        return axes

    def test_topn_named_set_on_rows_expands_members(self):
        axes = self._parse(self._build_topn())
        row_captions = [c for tup in axes.get("Axis1", []) for c in tup]
        assert "Electronics" in row_captions
        assert "Furniture" in row_captions
        assert "Apparel" in row_captions
        assert axes.get("Axis1"), "row axis empty — named set not expanded (Bug-5499)"

    def test_topn_named_set_measure_on_columns_detected(self):
        axes = self._parse(self._build_topn())
        col_captions = [c for tup in axes.get("Axis0", []) for c in tup]
        assert "net_sales" in col_captions

    def test_filtered_named_set_on_rows_expands_members(self):
        axes = self._parse(self._build_filtered())
        row_captions = [c for tup in axes.get("Axis1", []) for c in tup]
        assert "Electronics" in row_captions
        assert "Furniture" in row_captions
        assert axes.get("Axis1"), "row axis empty — named set not expanded (Bug-5499)"


# ---------------------------------------------------------------------------
# Bug-5495: paren-wrapped and function-wrapped predicate operand measures must
# be stripped from axis hierarchy detection. Without this fix,
# `([Measures].[m]) > 5` or `CoalesceEmpty([Measures].[m], 0) > 5` leaks
# [Measures] onto ROWS.
# ---------------------------------------------------------------------------

class TestWrappedPredicateMeasureStripping:
    """_strip_predicate_measures must handle wrapped measure operands."""

    def _hiers(self, expr):
        from src.dax.mdx_execute import _extract_hierarchies
        return _extract_hierarchies(expr)

    # --- paren-wrapped predicate operands (Bug-5495) -------------------------

    def test_paren_wrapped_measure_leading_stripped(self):
        """([Measures].[m]) > 5 must not leak [Measures] onto the axis."""
        hiers = self._hiers(
            "Filter([d].[d].Members, ([Measures].[m]) > 5)"
        )
        assert hiers == ["[d].[d]"]

    def test_paren_wrapped_measure_trailing_stripped(self):
        """5 < ([Measures].[m]) must not leak [Measures] onto the axis."""
        hiers = self._hiers(
            "Filter([d].[d].Members, 5 < ([Measures].[m]))"
        )
        assert hiers == ["[d].[d]"]

    # --- function-wrapped predicate operands (Bug-5495) ----------------------

    def test_coalesce_empty_wrapped_measure_stripped(self):
        """CoalesceEmpty([Measures].[m], 0) > 5 must not leak [Measures]."""
        hiers = self._hiers(
            "Filter([d].[d].Members, CoalesceEmpty([Measures].[m], 0) > 5)"
        )
        assert hiers == ["[d].[d]"]

    def test_iif_wrapped_measure_stripped(self):
        """IIF(cond, [Measures].[m], 0) > 5 must not leak [Measures]."""
        hiers = self._hiers(
            "Filter([d].[d].Members, IIF(1=1, [Measures].[m], 0) > 5)"
        )
        assert hiers == ["[d].[d]"]

    def test_double_paren_wrapped_measure_stripped(self):
        """(([Measures].[m])) > 5 must not leak [Measures] (Bug-5495)."""
        hiers = self._hiers(
            "Filter([d].[d].Members, (([Measures].[m])) > 5)"
        )
        assert hiers == ["[d].[d]"]

    def test_paren_wrapped_func_call_comparison_stripped(self):
        """(CoalesceEmpty([Measures].[m], 0)) > 5 must not leak [Measures]."""
        hiers = self._hiers(
            "Filter([d].[d].Members, (CoalesceEmpty([Measures].[m], 0)) > 5)"
        )
        assert hiers == ["[d].[d]"]

    # --- genuine wrapped axis measures must NOT be stripped -------------------

    def test_set_wrapped_axis_measure_preserved(self):
        """{[Measures].[m]} is a genuine axis member, not a predicate operand."""
        hiers = self._hiers("{[Measures].[m]}")
        assert "[Measures]" in hiers

    def test_addcalculated_members_axis_measure_preserved(self):
        """AddCalculatedMembers({[Measures].[m]}) is a genuine axis usage."""
        hiers = self._hiers("AddCalculatedMembers({[Measures].[m]})")
        assert "[Measures]" in hiers

    def test_crossjoin_axis_measure_preserved(self):
        """CrossJoin([d].[d].Members, [Measures].[m]) — measure is axis member."""
        hiers = self._hiers("CrossJoin([d].[d].Members, [Measures].[m])")
        assert "[Measures]" in hiers

    def test_filter_predicate_plus_genuine_axis_measure_preserved(self):
        """Predicate measure is stripped but explicit axis measure stays."""
        hiers = self._hiers(
            "CrossJoin(Filter([d].[d].Members, ([Measures].[x]) > 5), {[Measures].[m]})"
        )
        assert "[Measures]" in hiers
        assert "[d].[d]" in hiers


# ---------------------------------------------------------------------------
# Bug-5519: a LEAF member's `.Children` must be the EMPTY set, not the level
# ---------------------------------------------------------------------------

class TestLeafChildrenEmpty:
    """`[Dim].[Hier].[Member].Children` on a leaf member must yield no axis
    members. `[All].Children` (and `.Members`) stay correct.

    Repro: Excel year report-filter where each year "drills" to the full year
    list -> infinite recursion -> Excel "insufficient memory". The MDX axis
    `.Children` path returned the whole level for a leaf; the discovery
    (TREE_OP) path was already correct (Bug-5431).
    """

    # A flat dimension (one data level), exactly like the demo `year` dim:
    # the model-service dimension payload carries no explicit `levels`.
    _FLAT_DIM = [{"name": "year"}]
    _MEASURES = [{"name": "net_sales", "default_agg": "sum"}]

    def _year_rows(self):
        # The SQL behind `.Children` returns the whole year list (the symptom
        # source); the fix must suppress it for a leaf regardless of the rows.
        return [
            {"year": "(blank)", "net_sales": 1},
            {"year": "1998", "net_sales": 2},
            {"year": "1999", "net_sales": 3},
            {"year": "2000", "net_sales": 4},
        ]

    def test_leaf_children_is_empty_axis(self):
        mdx = (
            "SELECT {[Measures].[net_sales]} ON COLUMNS, "
            "[year].[year].[1999].Children ON ROWS FROM [tpcds_retail]"
        )
        xml = build_real_execute_response(
            mdx=mdx,
            catalog="tpcds_retail",
            columns=["year", "net_sales"],
            rows=self._year_rows(),
            measures_meta=self._MEASURES,
            dimensions_meta=self._FLAT_DIM,
        )
        # No year member row may appear on the row axis.
        assert "[year].[year].[1999]" not in xml
        assert "[year].[year].[1998]" not in xml
        assert "[year].[year].[2000]" not in xml
        assert "<Caption>1999</Caption>" not in xml
        assert "<Caption>1998</Caption>" not in xml
        # No SOAP fault.
        assert "<Fault" not in xml and "Exception" not in xml

    def test_leaf_children_no_recursion_member_set(self):
        """The whole-level set must never be emitted under a leaf's children —
        this is what made Excel recurse and exhaust memory."""
        mdx = (
            "SELECT {[Measures].[net_sales]} ON COLUMNS, "
            "[year].[year].[1999].Children ON ROWS FROM [tpcds_retail]"
        )
        xml = build_real_execute_response(
            mdx=mdx,
            catalog="tpcds_retail",
            columns=["year", "net_sales"],
            rows=self._year_rows(),
            measures_meta=self._MEASURES,
            dimensions_meta=self._FLAT_DIM,
        )
        # The leaf row axis (Axis1) declares the hierarchy but emits zero
        # member tuples — only the measure member on Axis0 survives.
        axes = xml[xml.find("<Axes>"):xml.find("</Axes>")]
        axis1 = axes[axes.find('<Axis name="Axis1">'):]
        assert "<UName>[year].[year]." not in axis1
        # The single measure member on Axis0 is the only axis member emitted.
        assert axes.count("<Member ") == 1
        # CellData for an empty leaf axis is a single nil cell (the same shape
        # an empty-result pivot already ships). Guards against a future change
        # to the empty-axis fallback silently re-emitting per-member cells.
        cell_data = xml[xml.find("<CellData>"):xml.find("</CellData>")]
        assert cell_data.count("<Cell ") == 1
        assert 'xsi:nil="true"' in cell_data

    def test_all_children_returns_year_level(self):
        """CONTROL: `[All].Children` = the children of the (All) level = the
        year members. Must stay unchanged."""
        mdx = (
            "SELECT {[Measures].[net_sales]} ON COLUMNS, "
            "[year].[year].[All].Children ON ROWS FROM [tpcds_retail]"
        )
        xml = build_real_execute_response(
            mdx=mdx,
            catalog="tpcds_retail",
            columns=["year", "net_sales"],
            rows=self._year_rows(),
            measures_meta=self._MEASURES,
            dimensions_meta=self._FLAT_DIM,
        )
        assert "<Caption>1998</Caption>" in xml
        assert "<Caption>1999</Caption>" in xml
        assert "<Caption>2000</Caption>" in xml

    def test_members_unchanged_returns_year_level(self):
        """CONTROL: `[year].[year].Members` returns the data years (unchanged)."""
        mdx = (
            "SELECT {[Measures].[net_sales]} ON COLUMNS, "
            "[year].[year].Members ON ROWS FROM [tpcds_retail]"
        )
        xml = build_real_execute_response(
            mdx=mdx,
            catalog="tpcds_retail",
            columns=["year", "net_sales"],
            rows=self._year_rows(),
            measures_meta=self._MEASURES,
            dimensions_meta=self._FLAT_DIM,
        )
        assert "<Caption>1998</Caption>" in xml
        assert "<Caption>1999</Caption>" in xml

    def test_leaf_children_other_flat_dim_empty(self):
        """Another flat dim (item_category) leaf `.Children` is also empty."""
        mdx = (
            "SELECT {[Measures].[net_sales]} ON COLUMNS, "
            "[item_category].[item_category].[Electronics].Children ON ROWS "
            "FROM [tpcds_retail]"
        )
        xml = build_real_execute_response(
            mdx=mdx,
            catalog="tpcds_retail",
            columns=["item_category", "net_sales"],
            rows=[
                {"item_category": "Electronics", "net_sales": 5},
                {"item_category": "Home", "net_sales": 6},
            ],
            measures_meta=self._MEASURES,
            dimensions_meta=[{"name": "item_category"}],
        )
        assert "<Caption>Electronics</Caption>" not in xml
        assert "<Caption>Home</Caption>" not in xml
        assert "<Fault" not in xml

    def test_leaf_children_on_columns_axis_empty(self):
        """The leaf-children fix applies to the COLUMNS axis too."""
        mdx = (
            "SELECT [year].[year].[1999].Children ON COLUMNS, "
            "{[Measures].[net_sales]} ON ROWS FROM [tpcds_retail]"
        )
        xml = build_real_execute_response(
            mdx=mdx,
            catalog="tpcds_retail",
            columns=["year", "net_sales"],
            rows=self._year_rows(),
            measures_meta=self._MEASURES,
            dimensions_meta=self._FLAT_DIM,
        )
        assert "<Caption>1999</Caption>" not in xml
        assert "<Caption>1998</Caption>" not in xml
        assert "<Fault" not in xml

    # --- leaf-determination helper unit coverage -------------------------------

    def test_resolution_flat_leaf(self):
        from src.dax.mdx_execute import _member_children_resolution
        assert _member_children_resolution(
            "[year].[year]", "1999", [{"name": "year"}], [],
        ) == "leaf"

    def test_resolution_all_member(self):
        from src.dax.mdx_execute import _member_children_resolution
        assert _member_children_resolution(
            "[year].[year]", "All", [{"name": "year"}], [],
        ) == "all"
        # Excel's parenthesised All form normalises identically.
        assert _member_children_resolution(
            "[year].[year]", "(All)", [{"name": "year"}], [],
        ) == "all"

    def test_resolution_unknown_hierarchy(self):
        from src.dax.mdx_execute import _member_children_resolution
        # Unknown dim/hier -> no leaf claim, preserve existing behaviour.
        assert _member_children_resolution(
            "[mystery].[mystery]", "X", [], [],
        ) == "unknown"

    def test_resolution_multilevel_intermediate_member_unknown(self):
        from src.dax.mdx_execute import _member_children_resolution
        # A defined 3-level hierarchy: a caption alone does not pin the level,
        # so an intermediate member keeps existing behaviour (not falsely leaf).
        hdefs = [{
            "name": "Geo",
            "levels": [
                {"name": "Country", "ordinal": 0},
                {"name": "Region", "ordinal": 1},
                {"name": "City", "ordinal": 2},
            ],
        }]
        assert _member_children_resolution(
            "[Geo].[Geo]", "France", [{"name": "Country"}], hdefs,
        ) == "unknown"

    def test_data_levels_flat_no_explicit_levels(self):
        from src.dax.mdx_execute import _hierarchy_data_levels
        assert _hierarchy_data_levels(
            "[year].[year]", [{"name": "year"}], [],
        ) == ["year"]

    def test_data_levels_multilevel_ordered(self):
        from src.dax.mdx_execute import _hierarchy_data_levels
        hdefs = [{
            "name": "Geo",
            "levels": [
                {"name": "City", "ordinal": 2},
                {"name": "Country", "ordinal": 0},
                {"name": "Region", "ordinal": 1},
            ],
        }]
        assert _hierarchy_data_levels("[Geo].[Geo]", [], hdefs) == [
            "Country", "Region", "City",
        ]


class TestChildrenMatrix:
    """Systematic `.Children` edge-case matrix (Bug-5519 round-2).

    Cross-product of:
      - member level:   {All member, leaf member, multi-level intermediate}
      - member form:    {caption `[1999]`, key `&[1999]`, composite `&[k0]&[k1]`}
      - All spelling:   {`[All]`, `(All)`, `[all]`, `[(all)]`}
      - hierarchy kind: {flat auto-hierarchy `[Dim].[Dim]`, known multi-level,
                         UNKNOWN hierarchy over a known dim, totally unknown dim}

    Invariants asserted:
      - leaf `.Children` -> EMPTY (caption AND key forms),
      - All `.Children`  -> the data-level members (any All spelling),
      - unknown-hierarchy / unknown-dim `.Children` -> prior whole-level
        behaviour preserved (NOT wrongly emptied),
      - multi-level intermediate caption -> "unknown" (no false leaf claim).

    Each assertion is constructed to FAIL on the pre-fix code for the case it
    targets (key-form leaf, unknown-hierarchy-over-flat-dim, lowercase All).
    """

    _FLAT_DIM = [{"name": "year"}]
    _MEASURES = [{"name": "net_sales", "default_agg": "sum"}]
    _MULTI_HDEF = [{
        "name": "Geo",
        "levels": [
            {"name": "Country", "ordinal": 0},
            {"name": "Region", "ordinal": 1},
            {"name": "City", "ordinal": 2},
        ],
    }]
    _MULTI_DIMS = [{"name": "Country"}, {"name": "Region"}, {"name": "City"}]

    def _year_rows(self):
        return [
            {"year": "1998", "net_sales": 2},
            {"year": "1999", "net_sales": 3},
            {"year": "2000", "net_sales": 4},
        ]

    def _exec(self, mdx, *, rows=None, dims=None, hdefs=None, cols=None):
        return build_real_execute_response(
            mdx=mdx,
            catalog="tpcds_retail",
            columns=cols or ["year", "net_sales"],
            rows=rows if rows is not None else self._year_rows(),
            measures_meta=self._MEASURES,
            dimensions_meta=dims or self._FLAT_DIM,
            hierarchy_defs=hdefs,
        )

    # --- member-form extraction (Finding 2: key form must be captured) --------

    def test_extract_caption_children(self):
        from src.dax.mdx_execute import _extract_all_member_filters
        out = _extract_all_member_filters("[year].[year].[1999].Children")
        assert out["[year].[year]"] == ("1999", "children")

    def test_extract_key_form_children(self):
        from src.dax.mdx_execute import _extract_all_member_filters
        # FAILS pre-fix: the old single-regex only matched the 3-bracket caption
        # form, so the key form produced no children spec at all.
        out = _extract_all_member_filters("[year].[year].&[1999].Children")
        assert out["[year].[year]"] == ("1999", "children")

    def test_extract_composite_key_children(self):
        from src.dax.mdx_execute import _extract_all_member_filters
        # Path-qualified composite key: the named member is the deepest key.
        out = _extract_all_member_filters(
            "[Geo].[Geo].[City].&[France]&[Paris].Children"
        )
        assert out["[Geo].[Geo]"] == ("Paris", "children")

    def test_extract_key_form_members(self):
        from src.dax.mdx_execute import _extract_all_member_filters
        out = _extract_all_member_filters("[year].[year].&[1999].Members")
        assert out["[year].[year]"] == ("1999", "members")

    # --- resolution classification matrix -------------------------------------

    def test_resolution_flat_leaf_caption(self):
        from src.dax.mdx_execute import _member_children_resolution
        assert _member_children_resolution(
            "[year].[year]", "1999", self._FLAT_DIM, [],
        ) == "leaf"

    def test_resolution_flat_leaf_key(self):
        from src.dax.mdx_execute import (
            _extract_all_member_filters,
            _member_children_resolution,
        )
        # Exercise the full key-form path: the extractor must pick the deepest
        # key from `&[1999]`, and that member must classify as a flat leaf.
        spec = _extract_all_member_filters("[year].[year].&[1999].Children")
        member, op = spec["[year].[year]"]
        assert (member, op) == ("1999", "children")
        assert _member_children_resolution(
            "[year].[year]", member, self._FLAT_DIM, [],
        ) == "leaf"

    @pytest.mark.parametrize("spelling", ["All", "(All)", "all", "(all)", "ALL"])
    def test_resolution_all_spellings(self, spelling):
        from src.dax.mdx_execute import _member_children_resolution
        # FAILS pre-fix for "all"/"(all)"/"ALL": case-sensitive compare
        # misclassified lower/upper-case All as a leaf -> wrongly EMPTY.
        assert _member_children_resolution(
            "[year].[year]", spelling, self._FLAT_DIM, [],
        ) == "all"

    def test_resolution_unknown_hierarchy_over_known_flat_dim(self):
        from src.dax.mdx_execute import _member_children_resolution
        # FAILS pre-fix: `[year].[not_year]` over the known `year` dim was
        # misclassified as the flat dim's single level -> "leaf" -> EMPTY.
        # It must be "unknown" so prior whole-level behaviour is preserved.
        assert _member_children_resolution(
            "[year].[not_year]", "1999", self._FLAT_DIM, [],
        ) == "unknown"

    def test_resolution_totally_unknown_dim(self):
        from src.dax.mdx_execute import _member_children_resolution
        assert _member_children_resolution(
            "[mystery].[mystery]", "X", [], [],
        ) == "unknown"

    def test_resolution_multilevel_intermediate_unknown(self):
        from src.dax.mdx_execute import _member_children_resolution
        # A caption alone cannot pin a level in a multi-level hierarchy, so an
        # intermediate member is "unknown" (no false leaf claim).
        assert _member_children_resolution(
            "[Geo].[Geo]", "France", self._MULTI_DIMS, self._MULTI_HDEF,
        ) == "unknown"

    def test_data_levels_unknown_hierarchy_over_flat_dim_empty(self):
        from src.dax.mdx_execute import _hierarchy_data_levels
        # FAILS pre-fix: returned ["year"] for the wrong-named hierarchy.
        assert _hierarchy_data_levels(
            "[year].[not_year]", self._FLAT_DIM, [],
        ) == []

    # --- end-to-end axis behaviour: leaf -> EMPTY (caption AND key) -----------

    def test_e2e_leaf_caption_children_empty(self):
        xml = self._exec(
            "SELECT {[Measures].[net_sales]} ON COLUMNS, "
            "[year].[year].[1999].Children ON ROWS FROM [tpcds_retail]"
        )
        assert "<Caption>1999</Caption>" not in xml
        assert "<Caption>1998</Caption>" not in xml
        assert "<Fault" not in xml

    def test_e2e_leaf_key_children_empty(self):
        # The important one: Excel/Power BI emit the `&[key]` member form.
        # FAILS pre-fix: the key form bypassed the leaf resolver and the whole
        # year level was re-rendered (the original infinite-drill bug).
        xml = self._exec(
            "SELECT {[Measures].[net_sales]} ON COLUMNS, "
            "[year].[year].&[1999].Children ON ROWS FROM [tpcds_retail]"
        )
        assert "<Caption>1999</Caption>" not in xml
        assert "<Caption>1998</Caption>" not in xml
        assert "<Caption>2000</Caption>" not in xml
        assert "[year].[year].[1999]" not in xml
        assert "<Fault" not in xml

    def test_e2e_leaf_key_children_empty_on_columns(self):
        xml = self._exec(
            "SELECT [year].[year].&[1999].Children ON COLUMNS, "
            "{[Measures].[net_sales]} ON ROWS FROM [tpcds_retail]"
        )
        assert "<Caption>1999</Caption>" not in xml
        assert "<Caption>1998</Caption>" not in xml
        assert "<Fault" not in xml

    # --- end-to-end: All spellings -> data-level members ----------------------

    @pytest.mark.parametrize("spelling", ["[All]", "[(All)]", "[all]", "[(all)]"])
    def test_e2e_all_spellings_return_year_level(self, spelling):
        # FAILS pre-fix for `[all]`/`[(all)]`: lower-case All was classified as a
        # leaf and the year members were wrongly suppressed.
        xml = self._exec(
            "SELECT {[Measures].[net_sales]} ON COLUMNS, "
            f"[year].[year].{spelling}.Children ON ROWS FROM [tpcds_retail]"
        )
        assert "<Caption>1998</Caption>" in xml
        assert "<Caption>1999</Caption>" in xml
        assert "<Caption>2000</Caption>" in xml

    # --- end-to-end: unknown hierarchy / unknown dim -> whole level preserved -

    def test_e2e_unknown_hierarchy_over_flat_dim_preserves_whole_level(self):
        # FAILS pre-fix: `[year].[not_year].[1999].Children` was emptied. The
        # old (pre-Bug-5519) behaviour rendered the whole level; that must be
        # preserved for an unresolved hierarchy.
        xml = self._exec(
            "SELECT {[Measures].[net_sales]} ON COLUMNS, "
            "[year].[not_year].[1999].Children ON ROWS FROM [tpcds_retail]"
        )
        assert "<Caption>1998</Caption>" in xml
        assert "<Caption>1999</Caption>" in xml
        assert "<Caption>2000</Caption>" in xml

    def test_e2e_unknown_hierarchy_key_form_preserves_whole_level(self):
        xml = self._exec(
            "SELECT {[Measures].[net_sales]} ON COLUMNS, "
            "[year].[not_year].&[1999].Children ON ROWS FROM [tpcds_retail]"
        )
        assert "<Caption>1998</Caption>" in xml
        assert "<Caption>1999</Caption>" in xml

    def test_e2e_totally_unknown_dim_preserves_whole_level(self):
        # `[stuff].[stuff]` is an unknown dim, but the result column matches the
        # hierarchy dim name so the whole-level rendering can be observed. The
        # resolver makes no leaf claim (unknown), so members are NOT emptied.
        rows = [
            {"stuff": "A", "net_sales": 1},
            {"stuff": "B", "net_sales": 2},
        ]
        xml = self._exec(
            "SELECT {[Measures].[net_sales]} ON COLUMNS, "
            "[stuff].[stuff].[A].Children ON ROWS FROM [tpcds_retail]",
            rows=rows,
            cols=["stuff", "net_sales"],
            dims=[{"name": "stuff_other"}],  # known dims, but not [stuff]
        )
        assert "<Caption>A</Caption>" in xml
        assert "<Caption>B</Caption>" in xml

    # --- end-to-end: multi-level intermediate member --------------------------

    def test_e2e_multilevel_intermediate_caption_preserves_behaviour(self):
        # An intermediate caption in a multi-level hierarchy is "unknown" -> the
        # documented scope is to preserve existing (whole-level) rendering. The
        # result column matches the hierarchy dim name (`Geo`) so the whole-level
        # member set is observable; a false leaf claim would empty it.
        hdef = [{
            "name": "Geo",
            "levels": [
                {"name": "Country", "ordinal": 0},
                {"name": "Region", "ordinal": 1},
                {"name": "City", "ordinal": 2},
            ],
        }]
        rows = [
            {"Geo": "North", "net_sales": 1},
            {"Geo": "South", "net_sales": 2},
        ]
        xml = self._exec(
            "SELECT {[Measures].[net_sales]} ON COLUMNS, "
            "[Geo].[Geo].[France].Children ON ROWS FROM [tpcds_retail]",
            rows=rows,
            cols=["Geo", "net_sales"],
            dims=[{"name": "Geo"}],
            hdefs=hdef,
        )
        assert "<Caption>North</Caption>" in xml
        assert "<Caption>South</Caption>" in xml
        assert "<Fault" not in xml


def test_cubeinfo_lastdataupdate_uses_model_refresh_time():
    """F-002-10: CubeInfo LastDataUpdate must reflect the model's real data
    refresh time (trust_meta.last_refreshed_at), not the static system config
    stamp, so Excel / Power BI do not treat stale pivots as fresh."""
    xml = build_real_execute_response(
        mdx="SELECT {[Measures].[Amount]} ON COLUMNS, "
            "[Geography].[Geography].Members ON ROWS FROM [demo]",
        catalog="demo",
        columns=["Geography", "Amount"],
        rows=[{"Geography": "France", "Amount": 10}],
        measures_meta=[{"name": "Amount", "default_agg": "sum"}],
        dimensions_meta=[{"name": "Geography"}],
        last_data_update="2026-07-01T08:30:00",
    )
    # The refresh time flows into LastDataUpdate (T retained for xs:dateTime).
    assert "<LastDataUpdate" in xml
    assert "2026-07-01T08:30:00</LastDataUpdate>" in xml


def test_cubeinfo_lastdataupdate_normalizes_fractional_and_zulu():
    from src.dax.mdx_execute import _normalize_cube_timestamp
    assert _normalize_cube_timestamp("2026-07-01T08:30:00.123456Z") == "2026-07-01T08:30:00"
    assert _normalize_cube_timestamp("2026-07-01T08:30:00Z") == "2026-07-01T08:30:00"
    assert _normalize_cube_timestamp("") == ""
    assert _normalize_cube_timestamp(None) == ""


def test_cubeinfo_lastdataupdate_strips_numeric_utc_offset():
    """R1 finding 4: a TIMESTAMPTZ .isoformat() with zero microseconds yields a
    numeric offset (e.g. +00:00); it must be stripped, and the date's own
    hyphens must be preserved."""
    from src.dax.mdx_execute import _normalize_cube_timestamp
    assert _normalize_cube_timestamp("2026-07-01T08:30:00+00:00") == "2026-07-01T08:30:00"
    assert _normalize_cube_timestamp("2026-07-01T08:30:00-05:00") == "2026-07-01T08:30:00"
    assert _normalize_cube_timestamp("2026-07-01T08:30:00.5+0000") == "2026-07-01T08:30:00"
    # A bare date/time with a space is normalised to T for xs:dateTime compliance.
    assert _normalize_cube_timestamp("2026-07-01 08:30:00") == "2026-07-01T08:30:00"
