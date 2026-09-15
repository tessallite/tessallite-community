"""How an XMLA catalog is named and matched (Bug-9825).

A model slug is unique only WITHIN a project, and a project slug only within a
tenant. XMLA published and resolved catalogs by bare model slug or display name
and returned the FIRST match, so with two accessible projects that each contain
a ``sales`` model, Excel could bind a workbook to plausible data from the wrong
project and nothing in the response said so. Wrong numbers, silently.

The name is therefore the qualifying combination that IS unique — tenant,
project, model, and the persona for a persona view — and an unqualified legacy
name is accepted only when exactly one accessible model matches. Ambiguity is
refused rather than resolved by list order.

The name stays readable on purpose. It is what a client shows in its database
list, and, because MDSCHEMA_CUBES sets CUBE_NAME to the catalog name, in its
cube list too — so carrying the persona here is what gives the user a visible
viewpoint to pick.
"""
from __future__ import annotations

__all__ = [
    "AmbiguousCatalogError",
    "CATALOG_NAME_MAX_LENGTH",
    "CATALOG_PART_SEPARATOR",
    "build_catalog_name",
]

# Separates the parts of a catalog name. Double underscore, matching the
# convention the JDBC catalogue already uses to qualify a colliding relation
# (``project_slug__name``) rather than inventing a second one.
CATALOG_PART_SEPARATOR = "__"

# What DISCOVER_LITERALS advertises for DBLITERAL_CATALOG_NAME. The previous
# value, 24, was copied from OlaPy and was never true of this product — a
# ``<slug>_<persona-slug>`` catalog passes it easily, and a qualified name is
# longer still. Advertising a limit shorter than the names actually emitted
# invites a strict client to truncate one. 100 is what SSAS reports.
CATALOG_NAME_MAX_LENGTH = 100


class AmbiguousCatalogError(Exception):
    """An unqualified catalog name matched more than one accessible model.

    Raised instead of picking one. Silently choosing a match is the defect
    Bug-9825 describes: the caller receives real data from a model it did not
    ask for, and nothing in the response says so.
    """

    def __init__(self, catalog: str, matches: list[str]):
        self.catalog = catalog
        self.matches = matches
        super().__init__(
            f"Catalog {catalog!r} is ambiguous: it matches {len(matches)} "
            f"accessible models ({', '.join(matches)}). Connect using the full "
            f"tenant{CATALOG_PART_SEPARATOR}project{CATALOG_PART_SEPARATOR}model "
            f"name shown in the catalog list."
        )


def build_catalog_name(
    tenant_slug: str,
    project_slug: str,
    model_slug: str,
    persona_slug: str = "",
) -> str:
    """The published catalog name: tenant, project, model, then persona.

    Parts that are unknown are omitted rather than rendered blank, so a caller
    that cannot supply a tenant still gets a project-qualified name instead of a
    leading separator.
    """
    parts = [p for p in (tenant_slug, project_slug, model_slug) if p]
    name = CATALOG_PART_SEPARATOR.join(parts)
    if persona_slug:
        name = f"{name}{CATALOG_PART_SEPARATOR}{persona_slug}"
    return name
