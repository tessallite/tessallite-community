"""Bug-9877 — XMLA Discover and Execute agree on named-set persona visibility.

Audit rows A19/A20. MDSCHEMA_SETS must advertise exactly the sets model-service
returns as bindable for the persona, and Execute must REFUSE a reference to a
set that does not bind rather than leaving it un-inlined (which rendered an
empty axis — the deceptive-empty failure Bug-7254 already rejected).
"""
from __future__ import annotations

from src.dax.mdschema import _rows_sets
from src.dax.xmla_server import _mdx_references_named_set


def _set(name: str, persona_visible=None) -> dict:
    row = {
        "name": name,
        "expression": f"TopCount([customer].Members, 5, [Measures].[{name}])",
        "list_type": "advanced_mdx",
    }
    if persona_visible is not None:
        row["persona_visible"] = persona_visible
    return row


class TestDiscoverExcludesHiddenSets:
    def test_bug9877_mdschema_sets_omits_a_set_that_does_not_bind(self):
        rows = _rows_sets(
            "modely",
            [_set("Visible", True), _set("Hidden", False)],
            [],
        )
        names = {r["SET_NAME"] for r in rows}
        assert "Visible" in names
        assert "Hidden" not in names

    def test_bug9877_unflagged_sets_are_still_advertised(self):
        rows = _rows_sets("modely", [_set("Plain")], [])
        assert {r["SET_NAME"] for r in rows} == {"Plain"}


class TestExecuteDetectsAReference:
    def test_bug9877_bracketed_reference_is_detected(self):
        mdx = "SELECT {[Measures].[m]} ON 0, {[Top Customers]} ON 1 FROM [modely]"
        assert _mdx_references_named_set(mdx, "Top Customers")

    def test_bug9877_bare_reference_is_detected(self):
        mdx = "SELECT {[Measures].[m]} ON 0, TopCustomers ON 1 FROM [modely]"
        assert _mdx_references_named_set(mdx, "TopCustomers")

    def test_bug9877_cube_name_after_from_is_not_a_set_reference(self):
        mdx = "SELECT {[Measures].[m]} ON 0 FROM [modely]"
        assert not _mdx_references_named_set(mdx, "modely")

    def test_bug9877_a_hierarchy_path_is_not_a_set_reference(self):
        mdx = "SELECT {[customer].[customer].Members} ON 1 FROM [modely]"
        assert not _mdx_references_named_set(mdx, "customer")

    def test_bug9877_unreferenced_set_is_not_detected(self):
        mdx = "SELECT {[Measures].[m]} ON 0 FROM [modely]"
        assert not _mdx_references_named_set(mdx, "Top Customers")
