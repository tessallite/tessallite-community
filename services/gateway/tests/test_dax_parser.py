"""
Unit tests for src.dax.dax_parser — translate_dax().

Pure function — no DB or network required.

Run from tessallite/services/gateway/:
    pytest tests/test_dax_parser.py
"""
import pytest

from src.dax.dax_parser import translate_dax, ParsedDAX


# ---------------------------------------------------------------------------
# SUMMARIZECOLUMNS
# ---------------------------------------------------------------------------

def test_summarize_columns_single_dim_single_measure():
    dax = 'EVALUATE SUMMARIZECOLUMNS(Sales[Country], "Revenue", SUM(Sales[Revenue]))'
    result = translate_dax(dax, "m1")
    assert "Country" in result.dimensions
    assert "Revenue" in result.measures


def test_summarize_columns_multiple_dims():
    dax = 'EVALUATE SUMMARIZECOLUMNS(Sales[Region], Sales[Month], "Revenue", SUM(Sales[Revenue]))'
    result = translate_dax(dax, "m1")
    assert "Region" in result.dimensions
    assert "Month" in result.dimensions


def test_summarize_columns_filter_extracted():
    dax = (
        'EVALUATE SUMMARIZECOLUMNS('
        'Sales[Country], '
        'FILTER(Sales, Sales[Status] = "Active"), '
        '"Revenue", SUM(Sales[Revenue]))'
    )
    result = translate_dax(dax, "m1")
    assert len(result.filters) == 1
    f = result.filters[0]
    assert f["column"] == "Status"
    assert f["operator"] == "eq"
    assert f["value"] == "Active"


def test_all_does_not_produce_filter():
    dax = 'EVALUATE SUMMARIZECOLUMNS(Sales[Country], ALL(Sales[Status]), "Revenue", SUM(Sales[Revenue]))'
    result = translate_dax(dax, "m1")
    assert len(result.filters) == 0


def test_allselected_does_not_produce_filter():
    dax = 'EVALUATE SUMMARIZECOLUMNS(Sales[Country], ALLSELECTED(Sales), "Revenue", SUM(Sales[Revenue]))'
    result = translate_dax(dax, "m1")
    assert len(result.filters) == 0


# ---------------------------------------------------------------------------
# SUMMARIZE
# ---------------------------------------------------------------------------

def test_summarize_basic():
    dax = 'EVALUATE SUMMARIZE(Sales, Sales[Region], "Revenue", SUM(Sales[Revenue]))'
    result = translate_dax(dax, "m1")
    assert "Region" in result.dimensions


# ---------------------------------------------------------------------------
# ROW
# ---------------------------------------------------------------------------

def test_row_single_measure():
    dax = "EVALUATE ROW(\"Total\", SUM(Sales[Revenue]))"
    result = translate_dax(dax, "m1")
    assert "Revenue" in result.measures


# ---------------------------------------------------------------------------
# TOPN
# ---------------------------------------------------------------------------

def test_topn_sets_limit():
    dax = (
        "EVALUATE TOPN(10, "
        'SUMMARIZECOLUMNS(Sales[Country], "Revenue", SUM(Sales[Revenue])), '
        "Sales[Revenue], DESC)"
    )
    result = translate_dax(dax, "m1")
    assert result.limit == 10


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------

def test_missing_evaluate_keyword_raises():
    with pytest.raises(ValueError, match="EVALUATE"):
        translate_dax("SUMMARIZECOLUMNS(Sales[Country])", "m1")


def test_unsupported_expression_raises():
    with pytest.raises(ValueError):
        translate_dax("EVALUATE GENERATEALL(Sales, Products)", "m1")


# ---------------------------------------------------------------------------
# to_query_body serialization
# ---------------------------------------------------------------------------

def test_to_query_body_fields():
    dax = 'EVALUATE SUMMARIZECOLUMNS(Sales[Region], "Revenue", SUM(Sales[Revenue]))'
    result = translate_dax(dax, "model-99")
    body = result.to_query_body()
    assert body["model_id"] == "model-99"
    assert body["query_type"] == "dax"
    assert "Region" in body["dimensions"]
    assert "Revenue" in body["measures"]


def test_to_query_body_omits_empty_filters():
    dax = 'EVALUATE SUMMARIZECOLUMNS(Sales[Region], "Revenue", SUM(Sales[Revenue]))'
    result = translate_dax(dax, "m1")
    body = result.to_query_body()
    assert "filters" not in body


def test_to_query_body_includes_limit_when_set():
    dax = (
        "EVALUATE TOPN(5, "
        'SUMMARIZECOLUMNS(Sales[Country], "Revenue", SUM(Sales[Revenue])), '
        "Sales[Revenue], ASC)"
    )
    result = translate_dax(dax, "m1")
    body = result.to_query_body()
    assert body.get("limit") == 5


# ---------------------------------------------------------------------------
# CALCULATE with filter context (A.1)
# ---------------------------------------------------------------------------

def test_calculate_single_filter():
    dax = 'EVALUATE CALCULATE([Revenue], Sales[Region] = "West")'
    result = translate_dax(dax, "m1")
    assert "Revenue" in result.measures
    assert len(result.filters) == 1
    assert result.filters[0]["column"] == "Region"
    assert result.filters[0]["value"] == "West"


def test_calculate_multiple_filters():
    dax = 'EVALUATE CALCULATE([Revenue], Sales[Region] = "West", Sales[Year] = "2024")'
    result = translate_dax(dax, "m1")
    assert "Revenue" in result.measures
    assert len(result.filters) == 2


def test_calculate_with_all():
    dax = 'EVALUATE CALCULATE([Revenue], ALL(Sales[Region]))'
    result = translate_dax(dax, "m1")
    assert "Revenue" in result.measures
    assert len(result.filters) == 0


def test_calculate_with_removefilters():
    dax = 'EVALUATE CALCULATE([Revenue], REMOVEFILTERS(Sales[Region]))'
    result = translate_dax(dax, "m1")
    assert "Revenue" in result.measures
    assert len(result.filters) == 0


def test_nested_calculate():
    dax = (
        'EVALUATE CALCULATE('
        'CALCULATE([Revenue], Sales[Status] = "Active"), '
        'Sales[Region] = "West")'
    )
    result = translate_dax(dax, "m1")
    assert "Revenue" in result.measures
    assert len(result.filters) == 2


def test_calculate_in_summarizecolumns():
    dax = (
        'EVALUATE SUMMARIZECOLUMNS('
        'Sales[Region], '
        '"Filtered Revenue", CALCULATE([Revenue], Sales[Status] = "Active"))'
    )
    result = translate_dax(dax, "m1")
    assert "Region" in result.dimensions
    assert "Filtered Revenue" in result.measures


# ---------------------------------------------------------------------------
# SUMMARIZECOLUMNS with expressions (A.2)
# ---------------------------------------------------------------------------

def test_summarize_columns_inline_divide():
    dax = (
        'EVALUATE SUMMARIZECOLUMNS('
        'Sales[Region], '
        '"Avg Price", DIVIDE(SUM(Sales[Revenue]), SUM(Sales[Quantity])))'
    )
    result = translate_dax(dax, "m1")
    assert "Region" in result.dimensions
    assert "Avg Price" in result.measures


def test_summarize_columns_inline_if():
    dax = (
        'EVALUATE SUMMARIZECOLUMNS('
        'Sales[Country], '
        '"Status", IF([Revenue] > 1000, [Revenue], 0))'
    )
    result = translate_dax(dax, "m1")
    assert "Country" in result.dimensions
    assert "Status" in result.measures


def test_summarize_columns_multiple_inline_measures():
    dax = (
        'EVALUATE SUMMARIZECOLUMNS('
        'Sales[Region], '
        '"Revenue", SUM(Sales[Revenue]), '
        '"Cost", SUM(Sales[Cost]), '
        '"Margin", DIVIDE(SUM(Sales[Revenue]), SUM(Sales[Cost])))'
    )
    result = translate_dax(dax, "m1")
    assert "Region" in result.dimensions
    assert "Revenue" in result.measures
    assert "Cost" in result.measures
    assert "Margin" in result.measures


# ---------------------------------------------------------------------------
# VAR / RETURN (A.3)
# ---------------------------------------------------------------------------

def test_var_single():
    dax = (
        'EVALUATE '
        'VAR TotalRev = SUM(Sales[Revenue]) '
        'RETURN ROW("Total", TotalRev)'
    )
    result = translate_dax(dax, "m1")
    assert "Revenue" in result.measures


def test_var_chained():
    dax = (
        'EVALUATE '
        'VAR Rev = SUM(Sales[Revenue]) '
        'VAR Cost = SUM(Sales[Cost]) '
        'RETURN ROW("Margin", Rev - Cost)'
    )
    result = translate_dax(dax, "m1")
    assert "Revenue" in result.measures or "Cost" in result.measures


def test_var_with_calculate():
    dax = (
        'EVALUATE '
        'VAR FilteredRev = CALCULATE([Revenue], Sales[Region] = "West") '
        'RETURN ROW("West Revenue", FilteredRev)'
    )
    result = translate_dax(dax, "m1")
    assert "Revenue" in result.measures


# ---------------------------------------------------------------------------
# DISTINCT / VALUES (A.4)
# ---------------------------------------------------------------------------

def test_distinct():
    dax = 'EVALUATE DISTINCT(Sales[Country])'
    result = translate_dax(dax, "m1")
    assert "Country" in result.dimensions


def test_values():
    dax = 'EVALUATE VALUES(Sales[Region])'
    result = translate_dax(dax, "m1")
    assert "Region" in result.dimensions


# ---------------------------------------------------------------------------
# Power BI patterns (A.5)
# ---------------------------------------------------------------------------

def test_selectcolumns():
    dax = (
        'EVALUATE SELECTCOLUMNS('
        'SUMMARIZECOLUMNS(Sales[Region], "Revenue", SUM(Sales[Revenue])), '
        '"Region Name", [Region], '
        '"Total Revenue", [Revenue])'
    )
    result = translate_dax(dax, "m1")
    assert "Region" in result.dimensions
    assert "Revenue" in result.measures


def test_addcolumns():
    dax = (
        'EVALUATE ADDCOLUMNS('
        'SUMMARIZECOLUMNS(Sales[Country], "Revenue", SUM(Sales[Revenue])), '
        '"Revenue Rank", [Revenue] + 0)'
    )
    result = translate_dax(dax, "m1")
    assert "Country" in result.dimensions
    assert "Revenue" in result.measures


def test_treatas_produces_warning():
    dax = (
        'EVALUATE CALCULATE([Revenue], '
        'TREATAS({"2024", "2025"}, Calendar[Year]))'
    )
    result = translate_dax(dax, "m1")
    assert "Revenue" in result.measures
    assert any("TREATAS" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# Time-intelligence DAX functions (A.6 / B.2 bridge)
# ---------------------------------------------------------------------------

def test_totalytd():
    dax = 'EVALUATE ROW("YTD Revenue", TOTALYTD([Revenue], Calendar[Date]))'
    result = translate_dax(dax, "m1")
    assert "Revenue" in result.measures
    assert result.time_variant_hints.get("Revenue") == "ytd"


def test_totalqtd():
    dax = 'EVALUATE ROW("QTD Revenue", TOTALQTD([Revenue], Calendar[Date]))'
    result = translate_dax(dax, "m1")
    assert result.time_variant_hints.get("Revenue") == "qtd"


def test_totalmtd():
    dax = 'EVALUATE ROW("MTD Revenue", TOTALMTD([Revenue], Calendar[Date]))'
    result = translate_dax(dax, "m1")
    assert result.time_variant_hints.get("Revenue") == "mtd"


def test_sameperiodlastyear():
    dax = (
        'EVALUATE ROW("LY Revenue", '
        'CALCULATE([Revenue], SAMEPERIODLASTYEAR(Calendar[Date])))'
    )
    result = translate_dax(dax, "m1")
    assert "Revenue" in result.measures
    assert result.time_variant_hints.get("Revenue") == "prior_year"


def test_previousyear():
    dax = 'EVALUATE ROW("PY", CALCULATE([Revenue], PREVIOUSYEAR(Calendar[Date])))'
    result = translate_dax(dax, "m1")
    assert result.time_variant_hints.get("Revenue") == "prior_year"


def test_dateadd():
    dax = 'EVALUATE ROW("Offset", CALCULATE([Revenue], DATEADD(Calendar[Date], -1, YEAR)))'
    result = translate_dax(dax, "m1")
    assert result.time_variant_hints.get("Revenue") == "period_offset"


def test_time_variant_in_query_body():
    dax = 'EVALUATE ROW("YTD", TOTALYTD([Revenue], Calendar[Date]))'
    result = translate_dax(dax, "m1")
    body = result.to_query_body()
    assert body["time_variant_hints"]["Revenue"] == "ytd"


# ---------------------------------------------------------------------------
# Error recovery and diagnostics (A.6)
# ---------------------------------------------------------------------------

def test_warnings_list_populated():
    dax = (
        'EVALUATE CALCULATE([Revenue], '
        'TREATAS({"2024"}, Calendar[Year]))'
    )
    result = translate_dax(dax, "m1")
    assert len(result.warnings) >= 1


def test_partial_parse_does_not_raise():
    """CALCULATE with a mix of known and unknown filter modifiers."""
    dax = (
        'EVALUATE CALCULATE([Revenue], '
        'Sales[Region] = "West", '
        'TREATAS({"2024"}, Calendar[Year]))'
    )
    result = translate_dax(dax, "m1")
    assert "Revenue" in result.measures
    assert len(result.filters) == 1
    assert len(result.warnings) >= 1
