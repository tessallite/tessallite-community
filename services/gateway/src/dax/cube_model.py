"""Model -> cube mapping (Bug-6603).

Single source of truth for the XMLA cube SHAPE that every MDSCHEMA rowset
builder and the DISCOVER dimensions builder consumes. Before this module the
cube shape was re-derived ad hoc in half a dozen places, and the model's
hierarchy definitions were grafted in as *detached pseudo-dimensions* that
lost their id, captions, and time typing — so in Excel every dimension read as
a single flat one-level hierarchy and calendar/user hierarchies were neither
time-typed nor persona-scoped correctly (Fable diagnostic symptoms 1 & 2).

What this module produces
-------------------------
``build_cube_dimensions(raw_dimensions, hierarchy_defs)`` returns ONE ordered
list of *cube dimension* dicts that the ``mdschema._rows_*`` builders read.
Each entry carries the correct, pre-computed shape:

- **Attribute (flat) dimensions** — one Tessallite ``Dimension`` (a single
  column) becomes a dimension whose only hierarchy is an *attribute hierarchy*
  (``HIERARCHY_ORIGIN = 2``). SSAS/Excel renders origin-2 as a plain attribute
  field, not a spurious "user-defined one-level hierarchy" (symptom 1).
- **User-defined / calendar hierarchies** — each ``HierarchyDefinition`` becomes
  a dimension whose hierarchy is a *user hierarchy* (``HIERARCHY_ORIGIN = 1``)
  with its ORDERED multi-level levels. ``is_time_dim`` is derived from
  ``dimension_kind == "time"`` (mirroring
  ``shared/semantic/hierarchy_resolver``), so the calendar's Year>Quarter>
  Month>Day levels reach ``mdschema._TIME_LEVEL_TYPES`` and Excel treats them
  as a real time hierarchy (symptom 2). The hierarchy's ``id`` is preserved so
  persona ``included_hierarchy_ids`` scoping works instead of silently deleting
  every hierarchy.

Field-list grouping (decision 2026-07-07)
-----------------------------------------
The Excel/BI field list is grouped to mirror the Tessallite excel-plugin: all
STANDALONE flat attribute dimensions collapse into ONE ``[Dimensions]`` group node,
each user/calendar HIERARCHY keeps its own node, and KPIs are their own native
group. The grouping key chosen was NOT source-table or display-folder (the earlier
open question) but a single ``[Dimensions]`` umbrella — see
``docs/questions/questions_xmla-dimension-grouping.md``.

Grouping is applied to the ``DIMENSION_UNIQUE_NAME`` COLUMN ONLY
(``dimension_unique_name_for``). The canonical ``[Name].[Name]`` HIERARCHY / LEVEL /
MEMBER unique-name grammar is PRESERVED unchanged, so member discovery
(``xmla_server._load_discover_member_data``) and the Execute axis / member
unique-name layer (Bug-3617) — which key off the hierarchy bracket and the level's
real column, never the dimension bracket — still resolve an expand to the right
members. Each standalone dimension remains its OWN attribute hierarchy under the
group (it is NOT flattened to a single attribute).
"""
from __future__ import annotations

from typing import Any

# HIERARCHY_ORIGIN (MD_ORIGIN) bit values used by SSAS / MSOLAP.
#   1 = MD_ORIGIN_USER_DEFINED  — a modeller-authored multi-level hierarchy
#   2 = MD_ORIGIN_ATTRIBUTE     — the system attribute hierarchy of one column
ORIGIN_USER_DEFINED = "1"
ORIGIN_ATTRIBUTE = "2"

# Bug-6603 (field-list grouping decision, 2026-07-07): the Excel/BI field list must
# mirror the excel-plugin's grouped sections — STANDALONE dimensions gathered into ONE
# group, HIERARCHIES in their own group(s), KPIs in their own group. In SSAS a client
# groups the field list by the DIMENSION_UNIQUE_NAME *column*, so grouping is achieved
# by giving every standalone attribute dimension one shared containing dimension node
# named ``[Dimensions]`` while each user/calendar hierarchy keeps its own node (which
# preserves its per-dimension time typing). HIERARCHY / LEVEL / MEMBER unique names are
# deliberately LEFT as ``[Name].[Name]...`` — the Execute + member-discovery paths
# (Bug-3617 grammar) resolve members through the hierarchy bracket and the level's real
# column, never the dimension bracket, so a shared DIMENSION_UNIQUE_NAME does not touch
# drill/axis behaviour. This is the group key column ONLY.
STANDALONE_GROUP_NAME = "Dimensions"
STANDALONE_GROUP_UNIQUE_NAME = f"[{STANDALONE_GROUP_NAME}]"

# Bug-6891 (user decision 2026-07-10, supersedes the hierarchies-own-node part
# of the 2026-07-07 grouping decision): multi-level user/calendar hierarchies
# collapse into ONE [Hierarchies] group node so the field list reads
# measures / KPIs / Dimensions / Hierarchies. Grouping remains the
# DIMENSION_UNIQUE_NAME column only — hierarchy/level/member unique names keep
# the [Name].[Name] grammar, so Execute and member discovery are unaffected.
# Level time typing survives via MDSCHEMA_LEVELS; the group node itself is
# DIMENSION_TYPE 3 (mixed content), like [Dimensions].
HIERARCHY_GROUP_NAME = "Hierarchies"
HIERARCHY_GROUP_UNIQUE_NAME = f"[{HIERARCHY_GROUP_NAME}]"


def is_standalone_attribute(dimension: dict[str, Any]) -> bool:
    """True when a cube dimension is a standalone flat attribute.

    Only genuinely flat single-attribute dimensions (attribute-hierarchy origin)
    share the one ``[Dimensions]`` group node. Any multi-level structure — a user
    or calendar hierarchy (``source == "hierarchy"``) OR a dimension that carries
    its own multi-level shape (user-defined origin) — keeps its OWN dimension node
    so its levels and per-dimension time typing are preserved. Resolving via
    :func:`hierarchy_origin_for` keeps this in lock-step with the SSAS grammar the
    ``_rows_hierarchies`` builder emits (origin 2 = attribute, origin 1 = hierarchy).

    A flat TIME dimension (``is_time_dim``) also keeps its own node: the group node
    is DIMENSION_TYPE 3 (other), but a time dimension emits DIMENSION_TYPE 1 on its
    hierarchy row, so grouping it would make the DIMENSIONS and HIERARCHIES rowsets
    disagree for the same unique name. Keeping it standalone stays consistent and
    lets Excel offer a timeline for it.
    """
    return (
        dimension.get("source") != "hierarchy"
        and not dimension.get("is_time_dim")
        and hierarchy_origin_for(dimension) == ORIGIN_ATTRIBUTE
    )


def is_grouped_hierarchy(dimension: dict[str, Any]) -> bool:
    """True when a cube dimension is a multi-level user/calendar hierarchy that
    collapses into the shared ``[Hierarchies]`` group node (Bug-6891). Flat
    time dimensions keep their own node (origin 2) so Excel's per-dimension
    time typing/timeline behaviour is preserved for them."""
    return hierarchy_origin_for(dimension) == ORIGIN_USER_DEFINED


def dimension_unique_name_for(dimension: dict[str, Any]) -> str:
    """DIMENSION_UNIQUE_NAME (the field-list group key column) for a cube dimension.

    Standalone attributes collapse to the shared ``[Dimensions]`` group;
    multi-level user/calendar hierarchies collapse to the shared
    ``[Hierarchies]`` group (Bug-6891); flat time dimensions keep their own
    ``[<name>]`` node. Note this governs ONLY the grouping column — the
    hierarchy/level/member unique names stay ``[<name>].[<name>]`` so Execute
    and member discovery are unaffected.
    """
    if is_standalone_attribute(dimension):
        return STANDALONE_GROUP_UNIQUE_NAME
    if is_grouped_hierarchy(dimension):
        return HIERARCHY_GROUP_UNIQUE_NAME
    # Bug-6746/Bug-6806: escape ``]`` in the name so a dimension literally named
    # with a ``]`` produces a valid MDX unique name the escape-aware parse path
    # reads back correctly.
    name = str(dimension.get("name", "")).replace("]", "]]")
    return f"[{name}]"


def _levels_are_multi(levels: Any) -> bool:
    """True when a level list describes more than one data level."""
    if not isinstance(levels, list):
        return False
    count = 0
    for lvl in levels:
        if isinstance(lvl, dict):
            if str(lvl.get("name", "")).strip():
                count += 1
        elif str(lvl).strip():
            count += 1
        if count > 1:
            return True
    return False


def _normalize_levels(levels: Any) -> list[dict[str, Any]]:
    """Return ordered ``[{name, ordinal, time_unit}]`` from a hierarchy's levels.

    Accepts the model-service ``HierarchyLevelResponse`` shape (dicts with
    ``name``/``ordinal``/``time_unit``) and is defensive about bare-string
    level lists. Ordering is by ``ordinal`` so ``LEVEL_NUMBER`` comes out
    monotonic in the builders.
    """
    if not isinstance(levels, list):
        return []
    detailed: list[dict[str, Any]] = []
    for idx, lvl in enumerate(levels):
        if isinstance(lvl, dict):
            name = str(lvl.get("name", "")).strip()
            if not name:
                continue
            detailed.append({
                "name": name,
                "ordinal": int(lvl.get("ordinal", idx) or 0),
                "time_unit": lvl.get("time_unit"),
            })
        else:
            name = str(lvl).strip()
            if not name:
                continue
            detailed.append({"name": name, "ordinal": idx, "time_unit": None})
    detailed.sort(key=lambda item: item["ordinal"])
    return detailed


def hierarchy_origin_for(dimension: dict[str, Any]) -> str:
    """Resolve HIERARCHY_ORIGIN for a cube dimension dict.

    Explicit ``hierarchy_origin`` (set by :func:`build_cube_dimensions`) wins.
    Otherwise derive from shape so direct builder callers (and unit tests that
    pass raw dicts) still get the correct SSAS grammar: a multi-level dimension
    is a user-defined hierarchy (origin 1); a single-column dimension is an
    attribute hierarchy (origin 2).
    """
    explicit = dimension.get("hierarchy_origin")
    if explicit:
        return str(explicit)
    if dimension.get("source") == "hierarchy":
        return ORIGIN_USER_DEFINED
    if _levels_are_multi(dimension.get("levels")):
        return ORIGIN_USER_DEFINED
    return ORIGIN_ATTRIBUTE


def build_cube_dimensions(
    raw_dimensions: list[dict[str, Any]],
    hierarchy_defs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Map model metadata to the ordered XMLA cube-dimension list.

    Attribute dimensions come first (in model order), then user-defined /
    calendar hierarchies. When a hierarchy shares a dimension's name the two
    are MERGED into one entry that keeps the real dimension's id/caption while
    taking the hierarchy's multi-level shape and time typing — the previous
    code replaced the dimension outright and lost id/is_time_dim/captions.
    """
    by_name: dict[str, dict[str, Any]] = {}
    order: list[str] = []

    for item in raw_dimensions:
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        enriched = dict(item)
        enriched.setdefault("source", "dimension")
        # A plain Tessallite dimension is one column -> attribute hierarchy.
        # (If a raw dim already carries multi-levels, honour that as a user
        # hierarchy.)
        enriched["hierarchy_origin"] = (
            ORIGIN_USER_DEFINED
            if _levels_are_multi(enriched.get("levels"))
            else ORIGIN_ATTRIBUTE
        )
        by_name[name] = enriched
        if name not in order:
            order.append(name)

    for hierarchy in hierarchy_defs:
        name = str(hierarchy.get("name", "")).strip()
        if not name:
            continue
        levels = _normalize_levels(hierarchy.get("levels"))
        kind = hierarchy.get("dimension_kind")
        is_time = str(kind or "").strip().lower() == "time"
        hid = str(hierarchy.get("id", ""))
        cube_hier: dict[str, Any] = {
            "name": name,
            "source": "hierarchy",
            "hierarchy_id": hid,
            "id": hid,
            "display_name": hierarchy.get("display_name") or name,
            "description": hierarchy.get("description") or "",
            "is_hidden": bool(hierarchy.get("is_hidden", False)),
            "is_time_dim": is_time,
            "dimension_kind": kind,
            "calendar_type": hierarchy.get("calendar_type"),
            "levels": levels,
            "hierarchy_origin": ORIGIN_USER_DEFINED,
        }
        existing = by_name.get(name)
        if existing is not None:
            # Merge: keep the real dimension's identity, adopt the hierarchy
            # shape. dimension_id lets persona included_dimension_ids still
            # match; hierarchy_id/id lets included_hierarchy_ids match.
            merged = dict(existing)
            merged.update({
                "source": "hierarchy",
                "hierarchy_id": hid,
                "dimension_id": str(existing.get("id", "")),
                "id": hid or str(existing.get("id", "")),
                "levels": levels,
                "hierarchy_origin": ORIGIN_USER_DEFINED,
                "is_time_dim": is_time or bool(existing.get("is_time_dim")),
                "dimension_kind": kind or existing.get("dimension_kind"),
                "calendar_type": (
                    cube_hier["calendar_type"] or existing.get("calendar_type")
                ),
                "display_name": (
                    existing.get("display_name") or cube_hier["display_name"]
                ),
                # Bug-6803: preserve the hierarchy's is_hidden flag on merge.
                "is_hidden": (
                    bool(existing.get("is_hidden")) or cube_hier["is_hidden"]
                ),
            })
            if not (existing.get("effective_description") or existing.get("description")):
                merged["description"] = cube_hier["description"]
            by_name[name] = merged
        else:
            by_name[name] = cube_hier
            order.append(name)

    # Bug-6890: a date-embedded/time hierarchy materialises each grain level
    # as its own Dimension row (e.g. "Year (updated_at Calendar)"). Those rows
    # are the hierarchy's OWN levels — advertising them again as standalone
    # attribute hierarchies duplicated every calendar level in Excel's field
    # list. Drop a raw attribute dimension when a visible TIME hierarchy owns
    # it as a level key attribute. Scoped to time hierarchies only: for
    # regular hierarchies (e.g. category > product) the level attributes are
    # real model dimensions users legitimately browse flat. Execute metadata
    # is unaffected — it reads the raw dimension list, so hierarchy-level SQL
    # resolution still finds the grain columns.
    covered_level_keys: set[str] = set()
    for hierarchy in hierarchy_defs:
        kind = str(hierarchy.get("dimension_kind") or "").strip().lower()
        if kind != "time" or bool(hierarchy.get("is_hidden", False)):
            continue
        for lvl in hierarchy.get("levels") or []:
            if not isinstance(lvl, dict):
                continue
            key_name = str((lvl.get("key_attribute") or {}).get("name", "")).strip()
            if key_name:
                covered_level_keys.add(key_name)
    if covered_level_keys:
        for name in list(order):
            entry = by_name.get(name)
            if (
                entry is not None
                and entry.get("source") != "hierarchy"
                and name in covered_level_keys
            ):
                order.remove(name)
                del by_name[name]

    return [by_name[name] for name in order if name in by_name]


def _flat_entry_from_merged(
    merged: dict[str, Any], dimension_id: str,
) -> dict[str, Any]:
    """Collapse a merged hierarchy entry back to its flat attribute dimension.

    Used when a persona grants the underlying dimension but not the merged
    hierarchy (F-3b): the entry must discover members via ``get_dimension_members``
    (source != "hierarchy") and advertise a single attribute level (origin 2),
    keyed by the real DIMENSION id so ``included_dimension_ids`` and the flat
    member fetch both resolve. The multi-level hierarchy shape and hierarchy id
    are dropped.
    """
    flat = dict(merged)
    flat["source"] = "dimension"
    flat["id"] = dimension_id
    flat["hierarchy_origin"] = ORIGIN_ATTRIBUTE
    flat["levels"] = []  # -> single self-named attribute level in mdschema
    flat.pop("hierarchy_id", None)
    flat.pop("dimension_id", None)
    return flat


def filter_cube_dimensions_by_persona(
    dimensions: list[dict[str, Any]],
    persona: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Scope cube dimensions to a persona's allow lists, source-aware.

    A persona's ``included_dimension_ids`` governs ATTRIBUTE dimensions and
    ``included_hierarchy_ids`` governs USER/CALENDAR hierarchies. The previous
    code filtered the merged discover list by ``included_dimension_ids`` alone,
    so a persona that populated only that list silently dropped EVERY hierarchy
    (hierarchy entries carry no dimension id) — the calendar and every drill
    hierarchy vanished from that persona's catalog (Fable symptom 2, finding b).

    Empty allow lists (or ``None`` persona) are a no-op. A hierarchy is kept
    when the hierarchy allow list is empty or lists its id; an attribute
    dimension is kept when the dimension allow list is empty or lists its id.
    """
    if not persona:
        return list(dimensions)
    allow_d = {str(x) for x in (persona.get("included_dimension_ids") or [])}
    allow_h = {str(x) for x in (persona.get("included_hierarchy_ids") or [])}
    if not allow_d and not allow_h:
        return list(dimensions)

    out: list[dict[str, Any]] = []
    for d in dimensions:
        if d.get("source") == "hierarchy":
            hid = str(d.get("hierarchy_id") or d.get("id") or "")
            hier_ok = (not allow_h) or (hid and hid in allow_h)
            # A merged entry (a hierarchy sharing a real dimension's name) also
            # carries dimension_id: if the persona EXPLICITLY grants that
            # dimension via included_dimension_ids, keep the entry even when a
            # non-empty included_hierarchy_ids omits the hierarchy — the user
            # was granted the underlying dimension.
            merged_dim_id = str(d.get("dimension_id") or "")
            explicit_dim_grant = bool(merged_dim_id) and merged_dim_id in allow_d
            if hier_ok:
                out.append(d)
            elif explicit_dim_grant:
                # F-3(b): the persona grants the underlying attribute DIMENSION
                # but a non-empty included_hierarchy_ids omits THIS hierarchy.
                # Keeping the merged entry (source="hierarchy") would route
                # member discovery to get_hierarchy_preview, which 404s for this
                # persona (the hierarchy is not in included_hierarchy_ids) — so
                # the granted dimension is advertised as a multi-level hierarchy
                # that never expands, and the flat attribute has no entry at all.
                # Emit the FLAT attribute entry instead (origin 2) so discovery
                # goes through get_dimension_members and the grant is reachable.
                out.append(_flat_entry_from_merged(d, merged_dim_id))
        else:
            did = str(d.get("id") or "")
            if not allow_d or (did and did in allow_d):
                out.append(d)
    return out


__all__ = [
    "ORIGIN_USER_DEFINED",
    "ORIGIN_ATTRIBUTE",
    "STANDALONE_GROUP_NAME",
    "STANDALONE_GROUP_UNIQUE_NAME",
    "build_cube_dimensions",
    "filter_cube_dimensions_by_persona",
    "hierarchy_origin_for",
    "is_standalone_attribute",
    "dimension_unique_name_for",
]
