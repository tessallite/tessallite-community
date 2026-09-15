"""Bug-9862 F6: the rollup response's lattice and member-identity safety net.

``mdx_execute._validate_rollup_tuples`` proves the emitted axis is a SUBSET of
what the source rows produced. A subset proof cannot see an OMISSION, which is
the failure that actually reaches users: Bug-9891 put a hierarchy LEVEL set on
one axis, the Bug-9785 coverage guard failed safe by dropping every rollup, and
Excel received 210 leaf tuples where 336 were requested -- a structurally valid
response, no error, blank subtotal rows in the pivot.

Two checks close that gap, both run on the finished axis just before
serialisation:

1. LATTICE COVERAGE. The grain combinations the client asked for come from
   ``subtotal_engine.grain_options_for_hierarchies`` -- the same producer the
   grain planner uses -- computed from the rollups DETECTED on the parsed axis
   expressions, before any guard may drop them. A planned grain that the source
   rows can supply and the axis does not carry is an omission and raises.
   ``NON EMPTY`` legitimately removes member COMBINATIONS, so coverage is
   asserted per grain, never per tuple.

2. MEMBER IDENTITY. One unique name must mean one member: every XMLA identity
   field is compared across the tuples it appears in, and its level and parent
   are checked against the level shape the Discover catalogue advertises for
   that hierarchy.

Both raise ``RollupValidationError`` (a ``ValueError``), which the Execute
handler already turns into a SOAP fault -- the loud failure Bug-9891 lacked.
"""

from __future__ import annotations

import re
from itertools import product
from typing import Any

from src.dax.subtotal_engine import (
    GRAIN_ALL,
    SUBTOTAL_GRAIN_KEY,
    SUBTOTAL_GRAIN_PREFIX,
    grain_options_for_hierarchies,
)

# Never enumerate a lattice larger than this. The grain BUDGET (deep-review F4,
# ``build_multi_subtotal_queries``) already refuses an oversized plan with a
# typed error, so this is only a second floor that keeps the validator O(1) in
# the pathological case rather than a policy of its own.
_MAX_LATTICE = 4096

_LEVEL_SUFFIX_RE = re.compile(r"\.\[((?:[^\]]|\]\])*)\]$")

# XMLA member identity. A repeated unique name that disagrees on ANY of these
# is two different members wearing one name, and a client that caches member
# metadata by unique name (Excel's pivot cache does) keeps whichever it saw
# first. ``member_type`` separates a synthetic All from a same-captioned child.
_IDENTITY_FIELDS = (
    "caption",
    "lname",
    "lnum",
    "member_type",
    "member_ordinal",
    "parent",
    "key",
    "value",
    "children_cardinality",
    "has_children",
)

_ALL_LEVEL_NAMES = {"all", "(all)"}


class RollupValidationError(ValueError):
    """A rollup response omitted a requested grain or contradicted itself."""


def _level_name_from_lname(lname: Any) -> str:
    """``[Dim].[Hier].[City]`` -> ``City``; empty when unparseable."""
    m = _LEVEL_SUFFIX_RE.search(str(lname or ""))
    return m.group(1).replace("]]", "]").strip() if m else ""


def expected_grain_lattice(hierarchies: list[Any]) -> list[tuple[int, ...]]:
    """Every grain combination the client asked for, detail included.

    One ordinal per hierarchy in ``hierarchies`` order; ``-1`` is the All
    grain. The detail combination is present because the client asked for it
    too -- the planner skips it only because the caller's original query
    already serves it.
    """
    options = grain_options_for_hierarchies(hierarchies)
    total = 1
    for opts in options:
        total *= len(opts)
        if total > _MAX_LATTICE:
            return []
    return [
        tuple(opt[0] for opt in combo)
        for combo in product(*options)
    ]


def _grain_label(hierarchies: list[Any], grain: tuple[int, ...]) -> str:
    """A human-readable name for one grain, for the SOAP fault text."""
    parts: list[str] = []
    for h, ordinal in zip(hierarchies, grain):
        if ordinal == GRAIN_ALL:
            level = "(All)"
        else:
            level = next(
                (l.name for l in h.levels if l.ordinal == ordinal),
                f"ordinal {ordinal}",
            )
        parts.append(f"{h.hierarchy_name}={level}")
    return " x ".join(parts) if parts else "(empty)"


def _row_grain(row: dict[str, Any], hierarchies: list[Any]) -> tuple[int, ...]:
    """The grain a merged source row was produced at.

    Multi-hierarchy grains are tagged per hierarchy; the single-hierarchy
    merger tags only the global key. A row carrying neither (the plain path,
    which is what a dropped-rollup response is made of) is a DETAIL row.
    """
    single = len(hierarchies) == 1
    grain: list[int] = []
    for h in hierarchies:
        key = SUBTOTAL_GRAIN_PREFIX + h.hierarchy_name
        if key in row:
            grain.append(int(row[key]))
        elif single and SUBTOTAL_GRAIN_KEY in row:
            grain.append(int(row[SUBTOTAL_GRAIN_KEY]))
        else:
            grain.append(h.levels[-1].ordinal if h.levels else 0)
    return tuple(grain)


def _tuple_grain(
    members_by_hierarchy: dict[str, dict[str, Any]],
    hierarchies: list[Any],
) -> tuple[int, ...] | None:
    """The grain one emitted axis tuple sits at, or ``None`` if unclassifiable.

    A synthetic All member (``member_type`` 2) is the All grain; any other
    member is the level its ``lname`` names. An unrecognised level makes the
    whole tuple unclassifiable rather than guessing -- guessing here would let
    the validator certify an omission.
    """
    grain: list[int] = []
    for h in hierarchies:
        member = members_by_hierarchy.get(f"[{h.mdx_dim_name}].[{h.mdx_hier_name}]")
        if member is None:
            return None
        if member.get("member_type") == 2:
            grain.append(GRAIN_ALL)
            continue
        level_name = _level_name_from_lname(member.get("lname")).lower()
        if level_name in _ALL_LEVEL_NAMES:
            grain.append(GRAIN_ALL)
            continue
        ordinal = next(
            (l.ordinal for l in h.levels if l.name.strip().lower() == level_name),
            None,
        )
        if ordinal is None:
            return None
        grain.append(ordinal)
    return tuple(grain)


def axis_tuples_from(
    tuples: list[list[dict[str, Any]]] | None,
    members: list[dict[str, Any]],
) -> list[list[dict[str, Any]]]:
    """Normalise an axis to a tuple list.

    A single-hierarchy axis carries a flat member list and no tuple list; every
    other shape carries the tuples. Both are the same thing to a coverage check.
    """
    if tuples is not None:
        return tuples
    return [[m] for m in members]


def validate_rollup_lattice(
    axis_name: str,
    hierarchies: list[Any],
    rows: list[dict[str, Any]],
    axis_tuples: list[list[dict[str, Any]]],
    *,
    all_grain_suppressed: bool = False,
) -> None:
    """Raise when the axis omits a requested grain the source rows can supply.

    *hierarchies* are the rollups DETECTED for this axis, passed in whether or
    not the pipeline went on to serve them -- that is the whole point: a guard
    that silently drops every rollup leaves a response the subset check calls
    perfect.

    A planned grain "can be supplied" when some observed source grain is at
    least as fine in every hierarchy: aggregating a non-empty finer grain
    cannot produce an empty coarser one, so the coarser grain has rows. A grain
    no observed row coarsens into is genuinely absent from the result and is
    not required. Within a grain, ``NON EMPTY`` may drop any number of member
    combinations; only a grain with ZERO emitted tuples is an omission.

    Cost is O(rows + tuples) plus the lattice square, and the lattice is capped
    by the grain budget, so the check adds nothing measurable to a response.
    A caller with no requested rollups pays nothing at all.
    """
    if not hierarchies or not rows:
        return
    planned = expected_grain_lattice(hierarchies)
    if not planned:
        return

    observed = {_row_grain(row, hierarchies) for row in rows}

    brackets = [f"[{h.mdx_dim_name}].[{h.mdx_hier_name}]" for h in hierarchies]
    wanted = set(brackets)
    emitted: set[tuple[int, ...]] = set()
    for members in axis_tuples:
        by_hier = {
            m.get("hierarchy", ""): m
            for m in members
            if m.get("hierarchy", "") in wanted
        }
        grain = _tuple_grain(by_hier, hierarchies)
        if grain is not None:
            emitted.add(grain)

    for grain in planned:
        if grain in emitted:
            continue
        if all_grain_suppressed and GRAIN_ALL in grain:
            # This client's wire contract represents aggregate coordinates
            # without All members (``RollupWireMode.SUPPRESS``); those rows are
            # dropped on purpose, so their absence is the contract, not a loss.
            continue
        if any(
            all(g <= o for g, o in zip(grain, obs))
            for obs in observed
        ):
            raise RollupValidationError(
                f"{axis_name} omitted a requested rollup grain: "
                f"{_grain_label(hierarchies, grain)} -- the source rows supply "
                f"it and the axis carries no tuple at that grain "
                f"({len(axis_tuples)} tuples over {len(emitted)} of "
                f"{len(planned)} requested grains)"
            )


def catalogue_level_shape(
    hierarchies: list[Any],
    data_levels_for: Any,
) -> dict[str, list[str]]:
    """``{hierarchy bracket: ordered data level names}`` from the catalogue.

    *data_levels_for* is the model-definition lookup Discover uses
    (``mdx_execute._defined_data_levels`` bound to this request's metadata), so
    Execute is checked against the very level list MDSCHEMA_LEVELS advertised.
    Hierarchies the model does not define are omitted and left unchecked.
    """
    shape: dict[str, list[str]] = {}
    for h in hierarchies:
        bracket = f"[{h.mdx_dim_name}].[{h.mdx_hier_name}]"
        if bracket in shape:
            continue
        levels = data_levels_for(bracket) or []
        if levels:
            shape[bracket] = list(levels)
    return shape


def validate_member_identity(
    axis_name: str,
    axis_tuples: list[list[dict[str, Any]]],
    catalogue_levels: dict[str, list[str]] | None = None,
) -> None:
    """Raise when one unique name carries two identities, or contradicts Discover.

    Consistency covers every XMLA identity field a client may cache against the
    unique name. The catalogue check then asserts the emitted level exists in
    the hierarchy's advertised level list, that its level NUMBER is that list's
    position, and that a member's parent is not at an impossible depth.

    A parent is only depth-checked when the parent member is itself on this
    axis; the All member is always an admissible parent, because an emitted
    member whose ancestor value is absent is deliberately rooted there.
    """
    catalogue_levels = catalogue_levels or {}
    seen: dict[tuple[str, str], tuple[Any, ...]] = {}
    level_of: dict[tuple[str, str], int] = {}
    all_unames: set[tuple[str, str]] = set()

    for members in axis_tuples:
        for member in members:
            hierarchy = member.get("hierarchy", "")
            identity = (hierarchy, member.get("uname", ""))
            signature = tuple(member.get(f) for f in _IDENTITY_FIELDS)
            previous = seen.setdefault(identity, signature)
            if previous != signature:
                differing = [
                    field
                    for field, before, now in zip(
                        _IDENTITY_FIELDS, previous, signature,
                    )
                    if before != now
                ]
                raise RollupValidationError(
                    f"{axis_name} member identity changed across tuples: "
                    f"{identity[1]!r} on {hierarchy!r} disagrees on "
                    f"{', '.join(differing)} "
                    f"({[previous[_IDENTITY_FIELDS.index(f)] for f in differing]!r}"
                    f" != {[signature[_IDENTITY_FIELDS.index(f)] for f in differing]!r})"
                )

            if identity in level_of or hierarchy not in catalogue_levels:
                continue
            levels = catalogue_levels[hierarchy]
            level_name = _level_name_from_lname(member.get("lname"))
            if member.get("member_type") == 2 or level_name.lower() in _ALL_LEVEL_NAMES:
                level_of[identity] = 0
                all_unames.add(identity)
                continue
            index = next(
                (
                    i for i, n in enumerate(levels)
                    if n.strip().lower() == level_name.strip().lower()
                ),
                None,
            )
            if index is None:
                raise RollupValidationError(
                    f"{axis_name} member {member.get('uname')!r} names level "
                    f"{level_name!r}, which {hierarchy} does not advertise "
                    f"(catalogue levels: {levels!r})"
                )
            expected_lnum = str(index + 1)
            actual_lnum = str(member.get("lnum", ""))
            if actual_lnum != expected_lnum:
                raise RollupValidationError(
                    f"{axis_name} member {member.get('uname')!r} is at level "
                    f"{level_name!r} (catalogue position {expected_lnum}) but "
                    f"reports LEVEL_NUMBER {actual_lnum!r}"
                )
            level_of[identity] = index + 1

    for members in axis_tuples:
        for member in members:
            hierarchy = member.get("hierarchy", "")
            identity = (hierarchy, member.get("uname", ""))
            depth = level_of.get(identity)
            if not depth or identity in all_unames:
                continue
            parent = member.get("parent")
            if not parent:
                continue
            parent_identity = (hierarchy, parent)
            if parent_identity in all_unames:
                continue
            parent_depth = level_of.get(parent_identity)
            if parent_depth is None:
                continue  # the parent is not on this axis; nothing to compare
            if parent_depth != depth - 1:
                raise RollupValidationError(
                    f"{axis_name} member {member.get('uname')!r} at level "
                    f"{depth} declares parent {parent!r} at level "
                    f"{parent_depth} -- a parent must sit exactly one level "
                    f"above its child in the catalogue hierarchy"
                )
