"""Bug-6887: mixed MDX with a synthetic Info/trust measure faulted the pivot.

Excel users dragging Info > Owner (or Source System / Last Refreshed) onto a
pivot that already contains a real measure produced MDX referencing both. The
flat-pivot translator did not recognise the synthetic measures, routed them
into ``unresolved_measures``, and faulted with the misleading Bug-1067 message
"Measure not available to this persona: Owner". The synthetic measures are not
SQL columns — the shared execute path post-joins their constant values after
the SQL runs — so the translator must simply drop them from SQL resolution.

Test escape: live-Excel only shape (the info-only short-circuit and the DAX
post-join were covered; the mixed-MDX leg was not). Guard: translator-level
tests below. Tier: T1.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.dax.xmla_server import _mdx_to_sql  # noqa: E402


MEASURES_META = [
    {"name": "transaction_count", "default_agg": "count"},
    {"name": "transaction_amount", "default_agg": "sum"},
]
DIMENSIONS_META = [
    {"name": "customer_segment"},
]


def test_mixed_mdx_with_owner_translates_without_fault():
    """[Measures].[Owner] alongside a real measure must not fault; the SQL
    carries the real measure only (Owner is post-joined by the caller)."""
    mdx = (
        "SELECT {[Measures].[Owner],[Measures].[transaction_count]} ON COLUMNS "
        "FROM [modely]"
    )
    sql, protocol = _mdx_to_sql(mdx, MEASURES_META, DIMENSIONS_META, model_slug="modely")
    assert "transaction_count" in sql
    assert "Owner" not in sql
    assert "_info_owner" not in sql


def test_mixed_mdx_internal_name_also_dropped():
    """The internal name form (_info_owner) is dropped from SQL resolution too."""
    mdx = (
        "SELECT {[Measures].[_info_owner],[Measures].[transaction_amount]} ON COLUMNS "
        "FROM [modely]"
    )
    sql, _ = _mdx_to_sql(mdx, MEASURES_META, DIMENSIONS_META, model_slug="modely")
    assert "transaction_amount" in sql
    assert "_info_owner" not in sql


def test_unknown_real_measure_still_faults():
    """The Bug-1067 fail-loud contract stands for genuinely unknown measures."""
    mdx = "SELECT {[Measures].[secret_measure]} ON COLUMNS FROM [modely]"
    with pytest.raises(ValueError, match="not available to this persona"):
        _mdx_to_sql(mdx, MEASURES_META, DIMENSIONS_META, model_slug="modely")


def test_info_measure_with_dimension_rows_translates():
    """Owner + a dimension on rows (no real measure) still yields SQL for the
    dimension; the constant column is appended post-execution."""
    mdx = (
        "SELECT {[Measures].[Owner]} ON COLUMNS, "
        "{[customer_segment].[customer_segment].Members} ON ROWS FROM [modely]"
    )
    sql, _ = _mdx_to_sql(mdx, MEASURES_META, DIMENSIONS_META, model_slug="modely")
    assert "customer_segment" in sql
    assert "Owner" not in sql
