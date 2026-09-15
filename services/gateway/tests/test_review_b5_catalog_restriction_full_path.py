"""Deep-review B5 / Bug-9853: catalog compatibility through the FULL Discover
path (row builder -> restriction -> XML), not the helper alone.

A saved workbook echoes back whichever catalog spelling it was created with:
the canonical qualified name (Bug-9825), the legacy bare slug, or the legacy
``<slug>_<persona>`` form. All must resolve to the one canonical row.
"""

import re

import pytest

from src.dax.mdschema import build_discover_response

MODELS = [{
    "id": "m1", "slug": "modely", "display_name": "Model Y",
    "tenant_slug": "acme-demo", "project_slug": "project1",
    "personas": [{"slug": "technical", "name": "Technical"}],
}]
CANONICAL = "acme-demo__project1__modely"
CANONICAL_PERSONA = "acme-demo__project1__modely__technical"


def _catalog_names(rowset: str, connection_catalog: str, restriction: dict) -> list[str]:
    xml = build_discover_response(
        rowset, connection_catalog, "m1", [], [],
        properties={"SspropInitAppName": "Microsoft Office Excel"},
        restrictions=restriction, tenant_models=MODELS,
    )
    return re.findall(r"<CATALOG_NAME>([^<]*)</CATALOG_NAME>", xml)


@pytest.mark.parametrize("rowset", ["MDSCHEMA_CATALOGS", "DBSCHEMA_CATALOGS"])
@pytest.mark.parametrize("spelling,expected", [
    ("modely", [CANONICAL]),
    (CANONICAL, [CANONICAL]),
    ("modely_technical", [CANONICAL_PERSONA]),
    (CANONICAL_PERSONA, [CANONICAL_PERSONA]),
    ("nope", []),
])
def test_every_catalog_spelling_resolves_to_its_canonical_row(rowset, spelling, expected):
    assert _catalog_names(rowset, spelling, {"CATALOG_NAME": [spelling]}) == expected
    # The connection's own catalog with no restriction behaves the same.
    assert _catalog_names(rowset, spelling, {}) == expected


def test_cubes_agree_with_the_catalog_identity_the_client_connected_with():
    xml = build_discover_response(
        "MDSCHEMA_CUBES", CANONICAL, "m1", [], [],
        properties={"SspropInitAppName": "Microsoft Office Excel"},
        restrictions={"CATALOG_NAME": [CANONICAL], "CUBE_NAME": [CANONICAL]},
        tenant_models=MODELS,
    )
    assert re.findall(r"<CUBE_NAME>([^<]*)</CUBE_NAME>", xml) == [CANONICAL]
