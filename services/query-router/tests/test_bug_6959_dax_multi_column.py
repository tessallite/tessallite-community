"""
Bug-6959 — Multi-column inline DAX measure expressions must NOT silently
bind to the first column only.

Previously, ``DIVIDE(SUM(Sales[Revenue]), SUM(Sales[Cost]))`` would silently
bind to ``Revenue`` alone, dropping ``Cost`` and the division — returning
``SUM(Revenue)`` instead of the ratio.  The fix raises ``UnsupportedSQL``
(mapped to 422 feature_not_supported) when the expression references more
than one distinct ``Table[Column]``.

Run from tessallite/services/query-router/:
    pytest tests/test_bug_6959_dax_multi_column.py -v
"""
from __future__ import annotations

import pytest

from src.parsing.dax_normalizer import parse_dax_to_ir
from src.ir.logical_query import UnsupportedSQL


class TestBug6959SummarizeColumns:
    """Multi-column inline expressions in SUMMARIZECOLUMNS."""

    def test_divide_two_columns_rejected(self):
        """DIVIDE(SUM(Sales[Revenue]), SUM(Sales[Cost])) must not bind to
        Revenue only — it must raise UnsupportedSQL."""
        with pytest.raises(UnsupportedSQL, match="distinct columns"):
            parse_dax_to_ir(
                'EVALUATE SUMMARIZECOLUMNS(Sales[Region], '
                '"Ratio", DIVIDE(SUM(Sales[Revenue]), SUM(Sales[Cost])))',
                "m1",
            )

    def test_subtraction_two_columns_rejected(self):
        """SUM(Sales[Revenue]) - SUM(Sales[Cost]) must not bind to Revenue only."""
        with pytest.raises(UnsupportedSQL, match="distinct columns"):
            parse_dax_to_ir(
                'EVALUATE SUMMARIZECOLUMNS(Sales[Region], '
                '"Margin", SUM(Sales[Revenue]) - SUM(Sales[Cost]))',
                "m1",
            )

    def test_single_column_aggregate_still_works(self):
        """SUM(Sales[Revenue]) with one column must still bind normally."""
        q = parse_dax_to_ir(
            'EVALUATE SUMMARIZECOLUMNS(Sales[Region], '
            '"Total", SUM(Sales[Revenue]))',
            "m1",
        )
        assert q.requested_measures == ["Revenue"]
        assert q.requested_dimensions == ["Region"]

    def test_same_column_twice_rejected(self):
        """Bug-7595: SUM(Sales[Revenue]) + SUM(Sales[Revenue]) references
        the same column twice.  The arithmetic structure would be silently
        lost (result is SUM(Revenue) instead of 2*SUM(Revenue)) if we bind
        to a single aggregate.  Must raise UnsupportedSQL."""
        with pytest.raises(UnsupportedSQL):
            parse_dax_to_ir(
                'EVALUATE SUMMARIZECOLUMNS(Sales[Region], '
                '"Double", SUM(Sales[Revenue]) + SUM(Sales[Revenue]))',
                "m1",
            )

    def test_measure_ref_not_affected(self):
        """A measure ref like [Revenue] (no inline expression) is unaffected."""
        q = parse_dax_to_ir(
            'EVALUATE SUMMARIZECOLUMNS(Sales[Region], "Rev", [Revenue])',
            "m1",
        )
        assert q.requested_measures == ["Revenue"]


class TestBug6959F3SameNameDifferentTable:
    """Bug-6959/F3 — same column name from DIFFERENT tables must be rejected.

    The original fix compared bare column names only, so
    ``DIVIDE(SUM(Sales[Amount]), SUM(Budget[Amount]))`` collapsed "Amount"
    into one entry and passed the gate — wrong numbers (the whole ratio
    silently binds to one column aggregate).
    """

    def test_same_col_name_different_table_rejected_summarizecolumns(self):
        """DIVIDE(SUM(Sales[Amount]), SUM(Budget[Amount])) — different tables,
        same column name — must be rejected."""
        with pytest.raises(UnsupportedSQL, match="distinct columns"):
            parse_dax_to_ir(
                'EVALUATE SUMMARIZECOLUMNS(Sales[Region], '
                '"Ratio", DIVIDE(SUM(Sales[Amount]), SUM(Budget[Amount])))',
                "m1",
            )

    def test_same_col_name_different_table_rejected_summarize(self):
        """Same-name-different-table in SUMMARIZE form."""
        with pytest.raises(UnsupportedSQL, match="distinct columns"):
            parse_dax_to_ir(
                'EVALUATE SUMMARIZE(Sales, Sales[Region], '
                '"Ratio", DIVIDE(SUM(Sales[Amount]), SUM(Budget[Amount])))',
                "m1",
            )

    def test_same_col_name_different_quoted_table_rejected(self):
        """Quoted table names: ``'Sales Table'[Amount]`` vs ``'Budget Table'[Amount]``."""
        with pytest.raises(UnsupportedSQL, match="distinct columns"):
            parse_dax_to_ir(
                "EVALUATE SUMMARIZECOLUMNS(Sales[Region], "
                "\"Ratio\", DIVIDE(SUM('Sales Table'[Amount]), SUM('Budget Table'[Amount])))",
                "m1",
            )

    def test_same_table_same_column_rejected(self):
        """Bug-7595: SUM(Sales[Amount]) + SUM(Sales[Amount]) — the same
        Table[Column] appears twice.  The arithmetic structure would be
        silently lost (result is SUM(Amount) instead of 2*SUM(Amount)),
        so this must be rejected."""
        with pytest.raises(UnsupportedSQL):
            parse_dax_to_ir(
                'EVALUATE SUMMARIZECOLUMNS(Sales[Region], '
                '"Double", SUM(Sales[Amount]) + SUM(Sales[Amount]))',
                "m1",
            )


class TestBug6959F3NormalisationEdgeCases:
    """Bug-6959/F3 codex findings — normalisation must not collapse or split
    identifiers that differ only by internal whitespace or optional quoting.
    """

    def test_quoted_vs_unquoted_same_table_rejected(self):
        """Bug-7595: ``'Sales'[Amount]`` and ``Sales[Amount]`` normalise to
        the same column.  ``SUM('Sales'[Amount]) + SUM(Sales[Amount])`` is
        still 2*SUM(Amount), which the IR cannot represent — must reject."""
        with pytest.raises(UnsupportedSQL):
            parse_dax_to_ir(
                "EVALUATE SUMMARIZECOLUMNS(Sales[Region], "
                "\"Total\", SUM('Sales'[Amount]) + SUM(Sales[Amount]))",
                "m1",
            )

    def test_internal_whitespace_in_table_preserved(self):
        """``'Sales Data'[Amount]`` vs ``'SalesData'[Amount]`` are DIFFERENT
        tables — must be rejected (not collapsed by stripping all whitespace)."""
        with pytest.raises(UnsupportedSQL, match="distinct columns"):
            parse_dax_to_ir(
                "EVALUATE SUMMARIZECOLUMNS(Sales[Region], "
                "\"Ratio\", DIVIDE(SUM('Sales Data'[Amount]), SUM('SalesData'[Amount])))",
                "m1",
            )

    def test_internal_whitespace_in_column_preserved(self):
        """``Sales[Order Amount]`` vs ``Sales[OrderAmount]`` are DIFFERENT
        columns — must be rejected."""
        with pytest.raises(UnsupportedSQL, match="distinct columns"):
            parse_dax_to_ir(
                'EVALUATE SUMMARIZECOLUMNS(Sales[Region], '
                '"Ratio", DIVIDE(SUM(Sales[Order Amount]), SUM(Sales[OrderAmount])))',
                "m1",
            )

    def test_escaped_apostrophe_different_tables_rejected(self):
        """``'North''s Sales'[Amount]`` vs ``'South''s Sales'[Amount]`` are
        DIFFERENT tables — must NOT collapse due to apostrophe escaping."""
        with pytest.raises(UnsupportedSQL, match="distinct columns"):
            parse_dax_to_ir(
                "EVALUATE SUMMARIZECOLUMNS(Sales[Region], "
                "\"Ratio\", DIVIDE(SUM('North''s Sales'[Amount]), SUM('South''s Sales'[Amount])))",
                "m1",
            )

    def test_escaped_apostrophe_same_table_rejected(self):
        """Bug-7595: ``'North''s Sales'[Amount]`` repeated is one distinct
        column, but repeated references lose arithmetic structure — reject."""
        with pytest.raises(UnsupportedSQL):
            parse_dax_to_ir(
                "EVALUATE SUMMARIZECOLUMNS(Sales[Region], "
                "\"Double\", SUM('North''s Sales'[Amount]) + SUM('North''s Sales'[Amount]))",
                "m1",
            )


class TestBug6959F3DifferentAggregationSameColumn:
    """Bug-6959/F3 — different aggregations over the SAME column must be
    rejected.

    ``DIVIDE(SUM(Sales[Amount]), COUNT(Sales[Amount]))`` references one
    column but two different aggregate operations. Binding to a single
    aggregate silently produces wrong numbers.
    """

    def test_sum_vs_count_same_column_rejected(self):
        with pytest.raises(UnsupportedSQL, match="distinct aggregate"):
            parse_dax_to_ir(
                'EVALUATE SUMMARIZECOLUMNS(Sales[Region], '
                '"Ratio", DIVIDE(SUM(Sales[Amount]), COUNT(Sales[Amount])))',
                "m1",
            )

    def test_sum_vs_average_same_column_rejected(self):
        with pytest.raises(UnsupportedSQL, match="distinct aggregate"):
            parse_dax_to_ir(
                'EVALUATE SUMMARIZECOLUMNS(Sales[Region], '
                '"Ratio", DIVIDE(SUM(Sales[Amount]), AVERAGE(Sales[Amount])))',
                "m1",
            )

    def test_single_agg_single_column_still_passes(self):
        """Genuine single-agg single-column must still pass — no false reject."""
        q = parse_dax_to_ir(
            'EVALUATE SUMMARIZECOLUMNS(Sales[Region], '
            '"Total", SUM(Sales[Amount]))',
            "m1",
        )
        assert q.requested_measures == ["Amount"]
        assert q.requested_dimensions == ["Region"]


class TestBug6959Summarize:
    """Multi-column inline expressions in SUMMARIZE."""

    def test_divide_two_columns_rejected_summarize(self):
        with pytest.raises(UnsupportedSQL, match="distinct columns"):
            parse_dax_to_ir(
                'EVALUATE SUMMARIZE(Sales, Sales[Region], '
                '"Ratio", DIVIDE(SUM(Sales[Amount]), SUM(Sales[Qty])))',
                "m1",
            )

    def test_single_column_aggregate_summarize_still_works(self):
        q = parse_dax_to_ir(
            'EVALUATE SUMMARIZE(Sales, Sales[Region], '
            '"Total", SUM(Sales[Amount]))',
            "m1",
        )
        assert q.requested_measures == ["Amount"]
        assert q.requested_dimensions == ["Region"]
