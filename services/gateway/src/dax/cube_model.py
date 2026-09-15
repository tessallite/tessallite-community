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

Grouping sets ``DIMENSION_UNIQUE_NAME`` (``dimension_unique_name_for``), and the
whole unique-name tree follows from it. Bug-9771: XMLA/MDX requires a hierarchy's
unique name to be prefixed by the unique name of the dimension that OWNS it, so a
grouped standalone attribute publishes ``[Dimensions].[account_type]``, its levels
``[Dimensions].[account_type].[account_type]``, and its members are built from the
hierarchy name in turn. Member discovery
(``xmla_server._load_discover_member_data``) and the Execute axis / member
unique-name layer (Bug-3617) consume those same names, so they stay consistent —
but they are NOT "unchanged by grouping", and any reasoning that assumes a fixed
``[Name].[Name]`` form is wrong. Each standalone dimension remains its OWN
attribute hierarchy under the group (it is NOT flattened to a single attribute).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

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
# preserves its per-dimension time typing).
#
# SUPERSEDED IN PART by Bug-9771. This block used to state that HIERARCHY / LEVEL /
# MEMBER unique names were "deliberately LEFT as ``[Name].[Name]``" so that grouping
# touched the DIMENSION_UNIQUE_NAME column ONLY. That is no longer true and must not be
# relied on: XMLA/MDX requires a hierarchy's unique name to be prefixed by the unique
# name of the dimension that OWNS it, so a grouped attribute now publishes
# ``[Dimensions].[account_type]`` (and its levels nest under that). See
# ``hierarchy_unique_name_for`` for the governing contract. Grouping therefore changes
# the whole unique-name tree, not one column.
STANDALONE_GROUP_NAME = "Dimensions"
STANDALONE_GROUP_UNIQUE_NAME = f"[{STANDALONE_GROUP_NAME}]"

# Bug-6891 (user decision 2026-07-10, supersedes the hierarchies-own-node part
# of the 2026-07-07 grouping decision): multi-level user/calendar hierarchies
# collapse into ONE [Hierarchies] group node so the field list reads
# measures / KPIs / Dimensions / Hierarchies. The claim that grouping remains
# "the DIMENSION_UNIQUE_NAME column only" was superseded by Bug-9771 — see the
# Bug-6603 block above; hierarchy and level unique names are prefixed by the
# group node they belong to.
# Level time typing survives via MDSCHEMA_LEVELS; the group node itself is
# DIMENSION_TYPE 3 (mixed content), like [Dimensions].
HIERARCHY_GROUP_NAME = "Hierarchies"
HIERARCHY_GROUP_UNIQUE_NAME = f"[{HIERARCHY_GROUP_NAME}]"

# Bug-9878 (owner decision 2026-09-05, option D "embedded date hierarchies"):
# everything time-typed -- the flat date/timestamp attributes AND the calendar
# hierarchies built over them -- lives in ONE ``[Time]`` dimension node of
# DIMENSION_TYPE 1, the SSAS "Date dimension" shape (attributes and user
# hierarchies of one time dimension side by side). Before this the flat time
# attributes each kept a top-level node of their own (five stray peers of
# [Dimensions] and [Hierarchies] in Excel's field list) while their calendar
# hierarchies sat under [Hierarchies]. One typed node keeps Excel's date
# Timeline filter (it needs a time-typed dimension) and removes the clutter.
# [Hierarchies] keeps the non-time user hierarchies (Geography Channel).
TIME_GROUP_NAME = "Time"
TIME_GROUP_UNIQUE_NAME = f"[{TIME_GROUP_NAME}]"


def field_list_grouping_enabled() -> bool:
    """Whether the field-list grouping above is applied (Bug-9788).

    Defaults TRUE -- the shipped behaviour, and a user decision.

    GROUPING IS NOT THE SAVE-CORRUPTION CAUSE. Do not re-derive that hypothesis
    from this flag's existence. Two independent pieces of evidence in this
    repository disprove it:

    - ``work/evidence/save-regression-2026-08-29/README.md`` — the workbook that
      SAVED CORRECTLY (``Book1-SAVED-OK-27aug.xlsx``) already carried 72
      hierarchies under ``[Dimensions]``, i.e. grouping was ENABLED. The only
      structural difference from the corrupted file is ``allUniqueName``
      (0 vs 85), which Bug-9772 introduced by advertising ``ALL_MEMBER``
      (Bug-9789).
    - ``work/sessions/2026-08-29-160000.md`` — a live test with grouping OFF
      still failed to save, and grouping is recorded there under "Hypotheses
      ELIMINATED, each on evidence -- do not retry".

    The flag is retained as a controlled INTERACTION test for the current
    post-Bug-9771 save/reopen path (Bug-9788), not as a suspected root cause. It
    is a config value rather than a code edit so a flip costs a container
    restart, not a rebuild.

    An environment variable wins over the config file. A deployed container's
    source tree is read-only, so the file alone would need a rebuild per flip;
    the variable makes the experiment a restart. It is also the right home for a
    per-deployment switch regardless.
    """
    import json as _json
    import os as _os
    from pathlib import Path as _Path

    _env = _os.environ.get("TESSALLITE_XMLA_FIELD_LIST_GROUPING")
    if _env is not None and _env.strip() != "":
        return _env.strip().lower() not in {"0", "false", "no", "off"}
    try:
        _c = _json.loads(
            (_Path(__file__).parent / "mdschema_config.json").read_text()
        )
        return bool(_c.get("field_list_grouping", {}).get("enabled", True))
    except Exception:
        # Fail to the SHIPPED behaviour: a missing or malformed config must not
        # silently change the cube shape every client has already cached.
        return True


def is_excel_xmla_client(
    *,
    client_app_name: str | None = None,
    properties: dict | None = None,
) -> bool:
    """Return whether the XMLA request originates from native Excel/MSOLAP."""
    app_name = client_app_name or ""
    if properties:
        app_name = str(properties.get("SspropInitAppName") or app_name)
    return "excel" in app_name.strip().lower()


def advertise_all_member(
    *,
    client_app_name: str | None = None,
    properties: dict | None = None,
) -> bool:
    """Whether ``MDSCHEMA_HIERARCHIES`` emits the ``ALL_MEMBER`` column.

    For Excel this is one element of the single ``native-all`` wire contract
    and is never decided on its own: the axis coordinate (``rollup_wire_mode``)
    and the member-property enumeration (``advertise_member_properties``) move
    with it. It is always advertised to Excel. ``TESSALLITE_XMLA_ALL_MEMBER``
    cannot move Excel; it remains an emergency suppression override for other
    clients only. The legacy ``calculated-total`` profile was retired
    (Bug-9874).

    History, kept because this flag was flipped six times between 2026-08-29
    and 2026-09-03 on the belief that advertising it refused ``SaveAs``
    ("Document not saved.", ~50 ms, no file activity): added by ``c8cef7195``,
    removed by ``450565d65``, restored by ``0daca7c35``, removed by
    ``de820dd72``, restored by Bug-9789's controlled A/B. Every one of those
    observations was real and every conclusion drawn from them was wrong.
    ``ALL_MEMBER`` was never the cause. It decides whether Excel mounts the
    ``(All)`` level as a field; with it advertised the level is implicit, and
    the intrinsic member properties enumerated by ``MDSCHEMA_PROPERTIES`` -
    which Excel treats as custom properties needing pivot-cache fields - then
    have nothing to bind to, so the OOXML writer refuses. Remove those rows
    and the native shape saves, as a real engine's does. Bisected one Discover
    variable per rebuild on 2026-09-03 (`probe_save_formats`), confirmed
    against the Power BI Desktop engine, recorded on Bug-9772 and in
    ``docs/architecture/architecture_excel-hierarchy-contract.md`` section 4.1.

    The two-flat-attribute save (Bug-9788) and the two- and three-dimension
    saves (Bug-9789) were the same defect and pass on ``native-all``
    (harness 16-4, 16-5, 16-16..16-20, 17, 18; 2026-09-04).
    """
    import os as _os

    if is_excel_xmla_client(
        client_app_name=client_app_name,
        properties=properties,
    ):
        # Bug-9772: one element of Excel's single wire contract, paired with
        # rollup_wire_mode. TESSALLITE_XMLA_ALL_MEMBER cannot move Excel.
        return True

    _env = _os.environ.get("TESSALLITE_XMLA_ALL_MEMBER")
    if _env is not None and _env.strip() != "":
        return _env.strip().lower() not in {"0", "false", "no", "off"}

    return True


def suppress_rollup_all_member(
    client_app_name: str | None = None,
    properties: dict | None = None,
) -> bool:
    """Omit All-grain tuples from rollup Execute axes (Bug-9788 pairing).

    ``ALL_MEMBER`` advertising and rollup All-grain tuples are ONE paired wire
    contract, not two independent switches. A client whose DISCOVER omitted
    ``ALL_MEMBER`` must not receive All members as Execute axis data: Excel's
    pivot-cache writer refuses ``Workbook.SaveAs`` outright ("Document not
    saved.", a sub-60 ms synchronous refusal with no dialog) when the axis
    carries All members its metadata never declared.

    Proven live on ALEX, 2026-09-02 (Bug-9788): one gateway image, only
    ``TESSALLITE_XMLA_SUPPRESS_ROLLUP_ALL`` changed between runs —

        env unset (decoupled default False)  -> 16-16 and 16-20 save FAIL
        env=true  (paired suppression)       -> 16-16, 16-20 and all controls PASS

    with ``[XMLA-9644] dropped 6/41`` / ``6/16`` log lines proving the All rows
    were live on the exact failing queries. The failing set — two or more flat
    attribute hierarchies on one axis — is exactly the set that reaches the
    multi-hierarchy rollup path, the only Execute path that still emitted
    unadvertised All members. A saved workbook shows Excel happily persists the
    ``[(All)]`` LEVEL cacheField and the All DEFAULT_MEMBER name with
    ``allUniqueName`` absent; only All members as axis DATA break the writer.

    The decoupling this reverts (450565d65, "restore native Excel rollups")
    traded that save failure for visible subtotal rows. The subtotal-display
    gap is real but belongs to Bug-9772; a refused save is strictly worse than
    a blank subtotal, and the blank rows carry NO wrong numbers (verified from
    the saved grid: detail averages source-computed, All rows empty).

    The Excel pairing is not overrideable, mirroring ``advertise_all_member``:
    an environment flag must not be able to split a wire contract known to
    make workbooks unsaveable. For other clients
    ``TESSALLITE_XMLA_SUPPRESS_ROLLUP_ALL`` remains an explicit emergency
    switch; otherwise suppression simply follows ``advertise_all_member`` so
    DISCOVER and Execute stay consistent for every client.
    """
    return rollup_wire_mode(
        client_app_name=client_app_name, properties=properties,
    ) is RollupWireMode.SUPPRESS


@dataclass(frozen=True)
class XmlaClientProfile:
    """The complete wire contract for one client, decided in one place.

    Excel's contract has three elements that were once three independent
    switches — and every regression in the Bug-9772/9788/9789 history came
    from flipping one without the others. They are one object now; the
    per-element accessors below are derived views of it, not decisions.
    """

    advertise_all_member: bool
    rollup_wire_mode: "RollupWireMode"
    advertise_kpis: bool
    advertise_member_properties: bool


def xmla_client_profile(
    client_app_name: str | None = None,
    properties: dict | None = None,
) -> "XmlaClientProfile":
    """Resolve the wire contract for this client.

    Excel gets the one measured profile, ``native-all`` (Bug-9772; the
    legacy ``calculated-total`` profile was retired by Bug-9874):
    ``ALL_MEMBER`` advertised, native All on the axis, and NO
    intrinsic member-property rows. The last element is what lets the OOXML
    pivot-cache writer persist the native shape: with the ``(All)`` level
    implicit, every enumerated property is a field Excel cannot serialise and
    ``SaveAs`` is refused in ~50 ms. A real engine (SSAS, Power BI) lists no
    intrinsic properties either. Measured 2026-09-04: save ``.xlsx`` → close →
    reopen → refresh → correct totals, one properly parented total per group.

    KPI discovery follows ``advertise_kpis_in_discover`` (Bug-9830).
    Other clients: native All advertised and returned, intrinsic properties
    enumerated as before; the emergency ``TESSALLITE_XMLA_SUPPRESS_ROLLUP_ALL``
    / ``TESSALLITE_XMLA_ALL_MEMBER`` overrides apply to them only.
    """
    return XmlaClientProfile(
        advertise_all_member=advertise_all_member(
            client_app_name=client_app_name, properties=properties,
        ),
        rollup_wire_mode=rollup_wire_mode(
            client_app_name=client_app_name, properties=properties,
        ),
        advertise_kpis=advertise_kpis_in_discover(
            client_app_name=client_app_name, properties=properties,
        ),
        advertise_member_properties=advertise_member_properties(
            client_app_name=client_app_name, properties=properties,
        ),
    )


class RollupWireMode(str, Enum):
    """How an aggregate (grain -1) coordinate is represented on the wire.

    One owner for a decision that used to be split across independent
    booleans — which is how Excel repeatedly ended up with Discover and
    Execute disagreeing.
    """

    NATIVE_ALL = "native_all"            # MEMBER_TYPE=2 synthetic All member
    SUPPRESS = "suppress"                # drop the aggregate rows entirely


EXCEL_PROFILE_NATIVE_ALL = "native-all"
"""The only Excel wire profile (Bug-9772; Bug-9874 retired ``calculated-total``).

Excel's wire contract is one paired thing: ``ALL_MEMBER`` advertised in
Discover, the native All member as every aggregate coordinate on the Execute
axis, and no intrinsic member-property rows. Six separate regressions came
from flipping one element alone, which is why none is individually
overridable for Excel. Measured on ALEX with ``probe_save_formats`` and the
persistence ladder: Excel mounts the data level only, keeps ``(All)``
implicit, renders one correctly parented total per group with the
hierarchy's own caption, and saves ``.xlsx``.
"""


def rollup_wire_mode(
    client_app_name: str | None = None,
    properties: dict | None = None,
) -> RollupWireMode:
    """Aggregate-coordinate representation for this client (Bug-9772).

    Native Excel always gets ``NATIVE_ALL`` (the ``native-all`` profile):
    every source-computed subtotal and grand-total row sits on the native All
    member, ``ALL_MEMBER`` is advertised, and the intrinsic member-property
    rowset is omitted; the workbook saves as .xlsx and survives
    reopen/refresh. The elements move together with ``advertise_all_member``
    and ``advertise_member_properties``; none is environment-overridable on
    its own for Excel — the whole point is one coherent contract.

    Other clients keep native All. ``TESSALLITE_XMLA_SUPPRESS_ROLLUP_ALL``
    remains an explicit emergency switch for them; otherwise suppression simply
    follows ``advertise_all_member`` so Discover and Execute stay consistent.
    """
    import os as _os

    if is_excel_xmla_client(
        client_app_name=client_app_name,
        properties=properties,
    ):
        return RollupWireMode.NATIVE_ALL

    _env = _os.environ.get("TESSALLITE_XMLA_SUPPRESS_ROLLUP_ALL")
    if _env is not None and _env.strip() != "":
        forced = _env.strip().lower() not in {"0", "false", "no", "off"}
        return RollupWireMode.SUPPRESS if forced else RollupWireMode.NATIVE_ALL
    if advertise_all_member(
        client_app_name=client_app_name,
        properties=properties,
    ):
        return RollupWireMode.NATIVE_ALL
    return RollupWireMode.SUPPRESS


def advertise_member_properties(
    *,
    client_app_name: str | None = None,
    properties: dict | None = None,
) -> bool:
    """Whether ``MDSCHEMA_PROPERTIES`` enumerates intrinsic member properties.

    Excel gets none: Excel treats every PROPERTY_TYPE=1 row as a custom
    member property that needs a pivot-cache field, and with the ``(All)``
    level implicit those fields cannot be serialised — the sole cause of the
    native-shape ``.xlsx`` refusal (Bug-9772 bisect, 2026-09-03/04). Every
    other client keeps the enumeration it always had.
    """
    return not is_excel_xmla_client(
        client_app_name=client_app_name,
        properties=properties,
    )


def advertise_kpis_in_discover(
    *,
    client_app_name: str | None = None,
    properties: dict | None = None,
) -> bool:
    """Whether ``MDSCHEMA_KPIS`` emits any rows for this client.

    ``TESSALLITE_XMLA_KPIS`` remains an emergency override. The supported
    default enables KPI discovery for Excel, while the row builder applies the
    executable-value/persona/deployed-snapshot eligibility contract so an empty
    or hidden KPI is never advertised (Bug-9830).
    """
    import os as _os

    _env = _os.environ.get("TESSALLITE_XMLA_KPIS")
    if _env is not None and _env.strip() != "":
        return _env.strip().lower() not in {"0", "false", "no", "off"}

    return True


def include_empty_value_kpis(
    *,
    client_app_name: str | None = None,
    properties: dict | None = None,
) -> bool:
    """Deprecated compatibility alias for the KPI discovery switch.

    Empty values are filtered by the shared XMLA KPI eligibility path, not by
    this switch. ``TESSALLITE_XMLA_KPIS`` is still honored for emergency
    containment.
    """
    return advertise_kpis_in_discover(
        client_app_name=client_app_name,
        properties=properties,
    )


def is_standalone_attribute(dimension: dict[str, Any]) -> bool:
    """True when a cube dimension is a standalone flat attribute.

    Only genuinely flat single-attribute dimensions (attribute-hierarchy origin)
    share the one ``[Dimensions]`` group node. Any multi-level structure — a user
    or calendar hierarchy (``source == "hierarchy"``) OR a dimension that carries
    its own multi-level shape (user-defined origin) — keeps its OWN dimension node
    so its levels and per-dimension time typing are preserved. Resolving via
    :func:`hierarchy_origin_for` keeps this in lock-step with the SSAS grammar the
    ``_rows_hierarchies`` builder emits (origin 2 = attribute, origin 1 = hierarchy).

    A flat TIME dimension (``is_time_dim``) is not a standalone attribute
    either: it belongs to the time-typed ``[Time]`` node (Bug-9878), whose
    DIMENSION_TYPE 1 agrees with the hierarchy row and keeps Excel's Timeline.
    """
    return (
        dimension.get("source") != "hierarchy"
        and not dimension.get("is_time_dim")
        and hierarchy_origin_for(dimension) == ORIGIN_ATTRIBUTE
    )


def is_time_group_member(dimension: dict[str, Any]) -> bool:
    """True when a cube dimension belongs to the shared ``[Time]`` node
    (Bug-9878): a flat time attribute or a calendar/time hierarchy."""
    return bool(dimension.get("is_time_dim"))


def is_grouped_hierarchy(dimension: dict[str, Any]) -> bool:
    """True when a cube dimension is a multi-level NON-time user hierarchy that
    collapses into the shared ``[Hierarchies]`` group node (Bug-6891). Time
    hierarchies belong to ``[Time]`` (Bug-9878, see ``is_time_group_member``)."""
    return (
        hierarchy_origin_for(dimension) == ORIGIN_USER_DEFINED
        and not is_time_group_member(dimension)
    )


def dimension_unique_name_for(dimension: dict[str, Any]) -> str:
    """DIMENSION_UNIQUE_NAME (the field-list group key column) for a cube dimension.

    Standalone attributes collapse to the shared ``[Dimensions]`` group;
    every time-typed field -- flat date attributes and calendar hierarchies --
    collapses to the shared, time-typed ``[Time]`` group (Bug-9878);
    the remaining multi-level user hierarchies collapse to the shared
    ``[Hierarchies]`` group (Bug-6891).

    This value is the ROOT of the whole unique-name tree, not an isolated
    grouping column. Bug-9771: ``hierarchy_unique_name_for`` prefixes the
    hierarchy with whatever this returns, levels nest under the hierarchy, and
    member unique names are built from the hierarchy name — so grouping changes
    all four. The earlier claim that hierarchy/level/member names "stay
    ``[<name>].[<name>]``" is FALSE; see ``hierarchy_unique_name_for``.
    """
    # Bug-9788: when grouping is switched off every dimension keeps its OWN node.
    # The hierarchy/level/member unique names follow from this function through
    # hierarchy_unique_name_for, so the whole cube stays internally consistent in
    # either mode -- the point of the flag is to change ONE structural variable,
    # not to produce a half-grouped cube. (The known-good workbook did NOT record
    # the ungrouped shape: Book1-SAVED-OK-27aug.xlsx saved WITH grouping on.)
    if field_list_grouping_enabled():
        if is_standalone_attribute(dimension):
            return STANDALONE_GROUP_UNIQUE_NAME
        if is_time_group_member(dimension):
            return TIME_GROUP_UNIQUE_NAME
        if is_grouped_hierarchy(dimension):
            return HIERARCHY_GROUP_UNIQUE_NAME
    # Bug-6746/Bug-6806: escape ``]`` in the name so a dimension literally named
    # with a ``]`` produces a valid MDX unique name the escape-aware parse path
    # reads back correctly.
    name = str(dimension.get("name", "")).replace("]", "]]")
    return f"[{name}]"


def hierarchy_unique_name_for(dimension: dict[str, Any]) -> str:
    """HIERARCHY_UNIQUE_NAME for a cube dimension — the WIRE form.

    Bug-9771: XMLA/MDX object naming requires a hierarchy's unique name to be
    prefixed by the unique name of the dimension that OWNS it::

        HIERARCHY_UNIQUE_NAME == <DIMENSION_UNIQUE_NAME>.<hierarchy name>

    (Real SSAS: dimension ``[Product]`` owns ``[Product].[Color]``.)

    The Bug-6603/Bug-6891 grouping set ``DIMENSION_UNIQUE_NAME`` to the shared
    ``[Dimensions]`` / ``[Hierarchies]`` group node but deliberately left the
    hierarchy unique name as ``[Name].[Name]`` so the Execute and member
    discovery paths would not have to change. That combination is not
    expressible in the grammar: ``[account_type].[account_type]`` declares an
    owning dimension ``[account_type]`` which does NOT exist in
    MDSCHEMA_DIMENSIONS (only the group nodes and the flat time dims do). Excel
    cannot resolve the owner, and falls back to the level-0 caption — which is
    why every grouped flat attribute rendered as ``(All)`` in the PivotTable
    Rows drop zone.

    Deriving the hierarchy name from ``dimension_unique_name_for`` makes the
    two columns consistent BY CONSTRUCTION, so the grammar holds for grouped
    and ungrouped fields alike. For a field that already keeps its own
    dimension node (the flat time dimensions) this returns exactly what it
    always did, ``[business_date].[business_date]`` — those were already
    conformant, which is why they never showed the ``(All)`` symptom.

    This is the WIRE name. Internally the gateway still addresses hierarchies
    as ``[Name].[Name]``; ``mdx_execute`` maps between the two at the response
    boundary and ``xmla_server`` normalises inbound MDX, so no member/axis
    resolution logic has to learn the grouped form.
    """
    name = str(dimension.get("name", "")).replace("]", "]]")
    return f"{dimension_unique_name_for(dimension)}.[{name}]"


def internal_hierarchy_unique_name_for(dimension: dict[str, Any]) -> str:
    """The INTERNAL ``[Name].[Name]`` hierarchy bracket for a cube dimension.

    This is the form every parser, member builder and axis resolver in
    ``mdx_execute`` works in (see :func:`hierarchy_unique_name_for` for why the
    wire form differs). Kept here so producer and consumer derive both names
    from ONE definition instead of re-spelling the bracket grammar.
    """
    name = str(dimension.get("name", "")).replace("]", "]]")
    return f"[{name}].[{name}]"


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
    "TIME_GROUP_NAME",
    "TIME_GROUP_UNIQUE_NAME",
    "is_time_group_member",
    "build_cube_dimensions",
    "filter_cube_dimensions_by_persona",
    "hierarchy_origin_for",
    "is_standalone_attribute",
    "dimension_unique_name_for",
    "hierarchy_unique_name_for",
    "internal_hierarchy_unique_name_for",
]
