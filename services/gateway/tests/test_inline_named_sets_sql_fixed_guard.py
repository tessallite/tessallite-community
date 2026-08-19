"""Guard: _inline_named_sets skips sql_fixed named lists.

Invariant 8 (architecture_tessallite-named-lists.md): ``_inline_named_sets()``
must never inline a named set whose ``list_type == "sql_fixed"``.  Such lists
carry no MDX expression and are consumed only on the SQL parameter path via the
query-router.  A NULL/empty expression would be harmlessly skipped by the
pre-existing empty-expression check, but a sql_fixed set that somehow carries a
non-empty expression would be wrongly inlined as MDX (silent wrong results).
``list_type`` is the authoritative discriminator, not the expression value.

Spec reference: strategy_tessallite-named-lists.md, section 8 ("XMLA path
guard") and section 12.3 ("Gateway guard test").
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.dax.xmla_server import _inline_named_sets


# -----------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------

_MDX_TEMPLATE = (
    "SELECT {{[Measures].[Revenue]}} ON COLUMNS, "
    "{{[{set_name}]}} ON ROWS FROM [Model]"
)


def _mdx_with(set_name: str) -> str:
    return _MDX_TEMPLATE.format(set_name=set_name)


# -----------------------------------------------------------------------
# Tests
# -----------------------------------------------------------------------


class TestSqlFixedNamedSetGuard:
    """Verify that sql_fixed named sets are never inlined into MDX."""

    def test_sql_fixed_with_none_expression_not_inlined(self):
        """A sql_fixed set with expression=None must not crash and must
        leave the MDX unchanged (the bracket reference stays)."""
        mdx = _mdx_with("TopRegions")
        named_sets = [
            {
                "name": "TopRegions",
                "list_type": "sql_fixed",
                "expression": None,
            },
        ]
        result = _inline_named_sets(mdx, named_sets)
        # The original bracket reference must survive untouched.
        assert "[TopRegions]" in result

    def test_sql_fixed_with_empty_expression_not_inlined(self):
        """A sql_fixed set with expression="" must not be inlined."""
        mdx = _mdx_with("TopRegions")
        named_sets = [
            {
                "name": "TopRegions",
                "list_type": "sql_fixed",
                "expression": "",
            },
        ]
        result = _inline_named_sets(mdx, named_sets)
        assert "[TopRegions]" in result

    def test_sql_fixed_with_nonempty_expression_not_inlined(self):
        """Even if an sql_fixed set somehow has a non-empty expression
        field, the guard must still skip it — the list_type is the
        authoritative discriminator, not the expression value."""
        mdx = _mdx_with("TopRegions")
        named_sets = [
            {
                "name": "TopRegions",
                "list_type": "sql_fixed",
                "expression": "TopCount([Region].Members, 5)",
            },
        ]
        result = _inline_named_sets(mdx, named_sets)
        # Must NOT be replaced by the expression.
        assert "[TopRegions]" in result
        assert "TopCount([Region].Members, 5)" not in result

    def test_advanced_mdx_set_still_inlined(self):
        """Regression check: an advanced_mdx set with a valid expression
        must still be inlined as before."""
        mdx = _mdx_with("TopCustomers")
        named_sets = [
            {
                "name": "TopCustomers",
                "list_type": "advanced_mdx",
                "expression": "TopCount([Customer].Members, 10)",
            },
        ]
        result = _inline_named_sets(mdx, named_sets)
        assert "TopCount([Customer].Members, 10)" in result
        assert "[TopCustomers]" not in result

    def test_mixed_list_only_mdx_set_inlined(self):
        """A mixed list of one MDX set and one sql_fixed set: only the
        MDX set is inlined."""
        mdx = (
            "SELECT {[Measures].[Revenue]} ON COLUMNS, "
            "{[TopCustomers], [TopRegions]} ON ROWS FROM [Model]"
        )
        named_sets = [
            {
                "name": "TopCustomers",
                "list_type": "advanced_mdx",
                "expression": "TopCount([Customer].Members, 10)",
            },
            {
                "name": "TopRegions",
                "list_type": "sql_fixed",
                "expression": None,
            },
        ]
        result = _inline_named_sets(mdx, named_sets)
        # MDX set inlined.
        assert "TopCount([Customer].Members, 10)" in result
        assert "[TopCustomers]" not in result
        # sql_fixed set untouched.
        assert "[TopRegions]" in result

    def test_set_without_list_type_still_inlined(self):
        """Legacy named sets that predate the list_type field (no key in
        the dict) must still be inlined normally — backward compat."""
        mdx = _mdx_with("TopProducts")
        named_sets = [
            {
                "name": "TopProducts",
                "expression": "TopCount([Product].Members, 5)",
            },
        ]
        result = _inline_named_sets(mdx, named_sets)
        assert "TopCount([Product].Members, 5)" in result
        assert "[TopProducts]" not in result
