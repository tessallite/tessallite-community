"""Bug-9883 -- MDSCHEMA_SETS must name the hierarchies a set spans.

Excel refused to place any server-defined set (COM 0x800A03EC on
``CubeField.Orientation``) because the ``DIMENSIONS`` column was empty: the
builder read a key the named-set payload never carried. SSAS lists the
comma-separated hierarchy unique names; they are derived here from the
wire-form expression so they can never name a hierarchy MDSCHEMA_HIERARCHIES
does not advertise.
"""

from src.dax.cube_model import build_cube_dimensions
from src.dax.mdschema import _rows_sets, _set_dimensions_from_expression


def test_dimensions_are_the_non_measure_hierarchy_references():
    assert _set_dimensions_from_expression(
        "TopCount([Dimensions].[counterparty_country_code].Members, 5, [Measures].[transaction_amount])"
    ) == "[Dimensions].[counterparty_country_code]"
    assert _set_dimensions_from_expression(
        "Filter(CrossJoin([Dimensions].[a].Members, [Time].[b Calendar].[Year].Members), [Measures].[m] > 1)"
    ) == "[Dimensions].[a],[Time].[b Calendar]"
    assert _set_dimensions_from_expression("") == ""


def test_set_rows_carry_wire_dimensions():
    dims = build_cube_dimensions([{"name": "counterparty_country_code"}], [])
    rows = _rows_sets("m", [{
        "name": "Top 5 Countries by Revenue", "list_type": "mdx",
        "expression": "TopCount([counterparty_country_code].[counterparty_country_code].Members, 5, [Measures].[transaction_amount])",
    }], dims)
    assert rows[0]["DIMENSIONS"] == "[Dimensions].[counterparty_country_code]"
    assert rows[0]["EXPRESSION"].startswith("TopCount([Dimensions].[counterparty_country_code]")
