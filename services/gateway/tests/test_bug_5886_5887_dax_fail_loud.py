"""Regression tests for Bug-5886 / Bug-5887 (GPT-2026-07-02 F-002-01/02).

Both bugs were silent wrong-number defects in `_dax_to_sql`:

- Bug-5886: `TREATAS(...)` filters were parsed and recognised, but the
  translator only logged a warning and executed the query with the filter
  omitted -- returning a valid-looking but unfiltered result.
- Bug-5887: when a DAX time-intelligence function (TOTALYTD, etc.) had no
  matching pre-built variant measure on the model, the translator silently
  fell back to the base (non-time-filtered) measure instead of surfacing the
  gap to the caller.

Both are now fail-loud: `_dax_to_sql` raises `ValueError`, which the XMLA
Execute handler (`xmla_server.py`) already converts into a client-visible
SOAP fault (`except ValueError as exc: return _soap_fault(str(exc), "Client")`).
"""

import pytest

from src.dax.xmla_server import _dax_to_sql


def test_treatas_filter_fails_loud_instead_of_dropping_the_filter():
    dax = (
        'EVALUATE CALCULATE([Revenue], '
        'TREATAS({"2024", "2025"}, Calendar[Year]))'
    )
    measures_meta = [{"name": "Revenue", "default_agg": "sum", "id": "m-revenue"}]

    with pytest.raises(ValueError, match="TREATAS"):
        _dax_to_sql(dax, measures_meta, dimensions_meta=[], model_slug="sales")


def test_missing_time_variant_fails_loud_instead_of_using_base_measure():
    dax = 'EVALUATE ROW("YTD Revenue", TOTALYTD([Revenue], Calendar[Date]))'
    measures_meta = [{"name": "Revenue", "default_agg": "sum", "id": "m-revenue"}]

    with pytest.raises(ValueError, match="time-intelligence"):
        _dax_to_sql(dax, measures_meta, dimensions_meta=[], model_slug="sales")


def test_time_variant_resolves_when_the_matching_measure_exists():
    """Regression: a model that DOES define the variant measure must still
    route to it, not raise -- the fail-loud path is for the missing case
    only."""
    dax = 'EVALUATE ROW("YTD Revenue", TOTALYTD([Revenue], Calendar[Date]))'
    measures_meta = [
        {"name": "Revenue", "default_agg": "sum", "id": "m-revenue"},
        {
            "name": "Revenue_ytd",
            "default_agg": "sum",
            "id": "m-revenue-ytd",
            "variant_kind": "ytd",
            "variant_of_measure_id": "m-revenue",
        },
    ]

    sql, protocol = _dax_to_sql(dax, measures_meta, dimensions_meta=[], model_slug="sales")

    assert protocol == "jdbc"
    assert '"Revenue_ytd"' in sql
    assert '"Revenue"' not in sql.replace('"Revenue_ytd"', "")


def test_plain_dax_without_treatas_or_time_intel_is_unaffected():
    dax = 'EVALUATE ROW("Total Revenue", [Revenue])'
    measures_meta = [{"name": "Revenue", "default_agg": "sum", "id": "m-revenue"}]

    sql, protocol = _dax_to_sql(dax, measures_meta, dimensions_meta=[], model_slug="sales")

    assert protocol == "jdbc"
    assert "SUM(\"Revenue\")" in sql
