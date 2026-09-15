"""Canonical SSAS-style member unique names and compatible inbound parsing.

B8 round-2 fix (deep-review Findings 2/4/6): member unames were emitted
in one grammar (path-qualified composite keys, ``[Dim].[Hier].[Level]
.&[2025]&[4]``) but parsed in another (single-key regexes that captured
only the first ``&[key]``), so a server-emitted uname echoed back by a
client (Excel keep-only, report filter, timeline, DrilldownMember)
resolved to the wrong member. Every site that produces or consumes a
member unique name must go through this module.

Outbound grammars
-----------------
Flat attribute hierarchy:
``<hier_bracket>.[<key>]``

Flat source keys equal to the synthetic ``All`` token use the key grammar
``<hier_bracket>.&[<key>]`` so they cannot be confused with the hierarchy's
synthetic All member.

Multi-level hierarchy:
``<hier_bracket>.[<level_name>].&[k0]&[k1]...&[kn]``

- ``hier_bracket`` is ``[Dim].[Hier]``.
- A flat attribute carries its stable key in the final bracket token.
- A multi-level key path is ancestor-first: ``k0`` is the root level key
  and ``kn`` is the key of the named level itself.
- The legacy single-key form ``.&[k]`` is the degenerate one-segment path.
- A literal ``]`` inside a key is escaped as ``]]`` (SSAS convention,
  B8 round-3 Bug-1052) on emit and unescaped on parse.

Status: active. Last meaningful update: 2026-08-29.
"""

from __future__ import annotations

import re
from typing import Any

# Bracket body: any non-``]`` character, or an escaped ``]]`` pair.
_BRACKET_BODY = r'(?:[^\]]|\]\])'

# One-or-more ``&[key]`` segments. Embeddable, non-capturing.
KEY_PATH = rf'(?:\&\[{_BRACKET_BODY}+\])+'

# Key path optionally starting in caption form: ``&[k]&[k]...`` or ``[m]``.
# Embeddable as a single capturing group.
KEYS_OR_CAPTION = rf'(\&?\[{_BRACKET_BODY}+\](?:\&\[{_BRACKET_BODY}+\])*)'

_KEY_SEGMENT_RE = re.compile(rf'\&\[({_BRACKET_BODY}+)\]')
_CAPTION_RE = re.compile(rf'^\s*\[({_BRACKET_BODY}+)\]\s*$')


_FIRST_BRACKET_RE = re.compile(rf'^\[({_BRACKET_BODY}+)\]')


def first_bracket_body(uname: str | None) -> str | None:
    """Return the (unescaped) body of the FIRST bracketed token of a unique name.

    ``[Dim].[Hier].[West]`` / ``[Dim].[Hier]`` / ``[Dim]`` all yield ``Dim``.
    Bracket-aware, so a name carrying a literal ``.`` (``[Sales.Region]``) or an
    escaped ``]]`` is decoded correctly -- unlike ``split(".")[0].strip("[]")``,
    which mis-parses dotted names. Returns ``None`` when nothing parses.
    """
    if not uname:
        return None
    m = _FIRST_BRACKET_RE.match(uname.strip())
    if not m:
        return None
    return unescape_member_key(m.group(1))


def escape_member_key(key: str) -> str:
    """Escape a raw key for embedding in a bracketed segment (``]`` → ``]]``)."""
    return key.replace("]", "]]")


def unescape_member_key(key: str) -> str:
    """Reverse :func:`escape_member_key` (``]]`` → ``]``)."""
    return key.replace("]]", "]")


def qualify_member_uname(
    hier_bracket: str,
    level_name: str,
    key_path: list[str],
) -> str:
    """Build a path-qualified member unique name.

    ``[Cal].[Cal].[Month].&[4]`` is ambiguous — month 4 of 2025 and
    month 4 of 2026 collide, which both confuses MSOLAP member identity
    and causes false tuple deduplication. The SSAS convention carries
    the full ancestor key path: ``[Cal].[Cal].[Month].&[2025]&[4]``.

    Keys containing ``]`` are escaped as ``]]`` so the emitted uname
    round-trips through :func:`parse_member_keys` (Bug-1052). Bug-6746: the
    ``level_name`` body is escaped the same way (``hier_bracket`` is already an
    emitted, escaped bracket expression).
    """
    keys = "".join(f"&[{escape_member_key(k)}]" for k in key_path)
    return f"{hier_bracket}.[{escape_member_key(level_name)}].{keys}"


def canonical_member_uname(
    hier_bracket: str,
    level_name: str,
    key_path: list[str],
    *,
    is_multi_level: bool,
) -> str:
    """Build the one wire identity used by DISCOVER and every Execute path.

    A flat attribute has one data level and its established SSAS-compatible
    identity is the name form ``[Dim].[Hier].[key]``.  Reserved source keys
    equal to All use the key form ``[Dim].[Hier].&[key]`` so they remain
    distinct from the synthetic ``[Dim].[Hier].[All]`` member. Adding the
    redundant level plus ``&[key]`` only on a subtotal axis gives ordinary
    members two identities and corrupts Excel's saved PivotCache (Bug-9789).

    A multi-level hierarchy needs the level and full ancestor key path to keep
    equal leaf keys under different parents distinct.  ``key_path`` must not be
    empty: emitting a regular member with no stable key would make its identity
    ambiguous, so fail before a malformed response reaches a client.
    """
    if not key_path:
        raise ValueError("A regular XMLA member requires a non-empty key path")
    if is_multi_level:
        return qualify_member_uname(hier_bracket, level_name, key_path)
    # The synthetic hierarchy member is ``[Hier].[All]``.  A source row whose
    # real key is ``All`` or ``(All)`` must therefore use the key grammar so a
    # client can distinguish data from the rollup member.  Apply the same
    # reservation to case/space variants because XMLA consumers commonly
    # compare All case-insensitively while the source key remains exact.
    if is_all_member_token(key_path[-1]):
        return f"{hier_bracket}.&[{escape_member_key(key_path[-1])}]"
    return f"{hier_bracket}.[{escape_member_key(key_path[-1])}]"


def synthetic_all_member_uname(hier_bracket: str) -> str:
    """Return the sole canonical unique name for a hierarchy's All member."""
    return f"{hier_bracket}.[All]"


def synthetic_all_member_metadata(
    hier_bracket: str,
    dimension_name: str,
    *,
    children_cardinality: int = 0,
    member_ordinal: int = 0,
    caption: str | None = None,
) -> dict[str, Any]:
    """Build complete metadata for the synthetic hierarchy All member.

    MDSCHEMA_MEMBERS and Execute both consume this producer.  Keeping the
    caption, level, type, parent, and child count together prevents one
    response path from emitting a plausible but incompatible All dictionary.

    ``caption`` is presentation only. Unique name, member name, and key stay
    ``All``. The default ``All {dimension_name}`` is the non-Excel contract.
    Excel standalone flats pass ``All`` so nested blank subtotal rows do not
    show the technical field name (Bug-9772 member-caption leftover).
    """
    try:
        child_count = max(int(children_cardinality), 0)
    except (TypeError, ValueError):
        child_count = 0
    return {
        "hierarchy": hier_bracket,
        "uname": synthetic_all_member_uname(hier_bracket),
        "name": "All",
        "key": "All",
        "value": "All",
        "caption": caption if caption is not None else f"All {dimension_name}",
        "lname": f"{hier_bracket}.[(All)]",
        "lnum": "0",
        # XMLA defines the parent of a root member as NULL, not an empty
        # unique name.  DISCOVER omits this optional rowset value and Execute
        # serialises it as xsi:nil so MSOLAP cannot cache an empty member ID.
        "parent": None,
        "has_children": child_count > 0,
        "member_type": 2,
        "member_ordinal": member_ordinal,
        "children_cardinality": child_count,
    }


def parse_member_keys(keys_text: str) -> list[str]:
    """Parse the key-path portion of a member reference.

    Returns the ancestor-first key list for ``&[k0]&[k1]...`` input,
    a one-element list for the caption form ``[member]``, and an empty
    list when nothing parses. Whitespace surrounding the complete wire
    fragment is syntax; whitespace inside a bracket is key content and is
    preserved. This distinction is required for member identities whose raw
    keys intentionally begin or end with spaces.
    """
    if not keys_text:
        return []
    wire_text = keys_text.strip()
    keys = [unescape_member_key(k) for k in _KEY_SEGMENT_RE.findall(wire_text)]
    if keys:
        return keys
    m = _CAPTION_RE.match(wire_text)
    if m:
        return [unescape_member_key(m.group(1))]
    return []


def deepest_member_key(keys_text: str) -> str | None:
    """The key of the named level itself — the last segment of the path."""
    keys = parse_member_keys(keys_text)
    return keys[-1] if keys else None


# ---------------------------------------------------------------------------
# Full member-unique-name parser + canonical matcher (Bug-3617 Phase 1)
# ---------------------------------------------------------------------------
#
# One parser for every MEMBER_UNIQUE_NAME grammar, returning a structured
# decomposition the matcher uses. The matcher (``member_filter_matches``)
# replaces the scattered ``mem_uname == member_filter`` caption-equality checks
# with a grammar-aware comparison that accepts BOTH the canonical key form and
# the legacy caption form on input — so a client echoing back either grammar
# resolves correctly (the round-trip the naive Option A broke).

# A parsed member reference. ``grammar`` is one of:
#   "key"     — path-qualified ``[Dim].[Hier].[Level].&[k0]...&[kn]``
#   "caption" — historical parser name for flat ``[Dim].[Hier].[key]`` and
#               legacy caption input (single token, no ancestors)
#   "all"     — the ``(All)`` member ``[Dim].[Hier].[All]`` / ``[(All)]``
#   "measure" — ``[Measures].[name]``
#   "invalid" — unparseable
ParsedMemberName = tuple  # (hier_bracket, level_name|None, grammar, key_path)

_HIER_BRACKET_RE = re.compile(
    rf'^(\[{_BRACKET_BODY}+\]\.\[{_BRACKET_BODY}+\])\.(.+)$'
)
# Canonical tail: ``[Level].&[k0]&[k1]...``
_CANONICAL_TAIL_RE = re.compile(
    rf'^\[({_BRACKET_BODY}+)\]\.({KEY_PATH})$'
)
# Legacy key-only tail: ``&[k0]&[k1]...``. Excel still emits this form for
# flat hierarchy members in DrilldownMember and slicer requests. It is a key
# grammar, not a caption grammar, so key whitespace and parentheses are data.
_KEY_ONLY_TAIL_RE = re.compile(rf'^({KEY_PATH})$')
# Bare-bracket tail: ``[Member]`` (caption / All).
_BARE_TAIL_RE = re.compile(rf'^\[({_BRACKET_BODY}+)\]$')


def _norm_all(token: str) -> bool:
    """True if a bare member token denotes the (All) member in either form.

    Bug-9854: one rule for the whole member surface. This used to be
    case-sensitive while :func:`is_all_member_token` was not, so a client that
    lower-cases the token (Bug-5519 round 2: Excel and Power BI both do) reached
    the parser and was classified as an ordinary caption member ``all`` that
    does not exist -- an empty or wrong result on a real BI-client path. A
    real source key spelled ``all`` is never ambiguous here: producers emit
    data members in the key grammar (``.&[all]``, Bug-9789), which this
    bare-token rule never sees.
    """
    return is_all_member_token(token)


def is_all_member_token(token: str | None) -> bool:
    """True when a bare member token denotes a hierarchy's (All) member.

    Accepts every spelling BI clients are known to emit: the MEMBER form
    ``All``, the LEVEL form ``(All)``, and any case variant. Bug-5519 round-2
    established that Excel and Power BI send both forms and that clients may
    lower-case them; ``mdx_execute._member_axis_kind`` and
    ``mdx_calc_members._extract_all_pinned_dims`` already compare
    case-insensitively for exactly that reason. This is the callable form of
    that same rule, so a consumer never re-derives it inline.

    Only those two exact spellings. sol review F-CR-01: the first version used
    ``strip("()")``, which also classified ``All)``, ``(All`` and ``((All))``
    as the grand total. Those are ordinary captions, and a consumer that drops
    a filter for them widens the result set silently — the one outcome this
    recognizer must never cause.

    Since Bug-9854 this is also the rule :func:`parse_member_uname` applies
    to a bare bracket token, so discovery, axis resolution and the filter
    audits classify the grand total identically.
    """
    if token is None:
        return False
    return token.strip().lower() in ("all", "(all)")


def parse_member_uname(uname: str | None) -> ParsedMemberName:
    """Decompose any MEMBER_UNIQUE_NAME into ``(hier_bracket, level_name, grammar,
    key_path)``.

    - ``[Measures].[x]``                       -> ("[Measures]", None, "measure", ["x"])
    - ``[D].[H].[Level].&[2025]&[4]``          -> ("[D].[H]", "Level", "key", ["2025","4"])
    - ``[D].[H].[4]``                          -> ("[D].[H]", None, "caption", ["4"])
    - ``[D].[H].[All]`` / ``[D].[H].[(All)]``  -> ("[D].[H]", None, "all", [])
    - anything else                            -> ("", None, "invalid", [])

    Keys are unescaped (``]]`` -> ``]``). This is the single entry point for
    consuming a member reference — never re-derive the grammar ad hoc.
    """
    if not uname:
        return ("", None, "invalid", [])
    text = uname.strip()

    # Measures: [Measures].[name]
    m_meas = re.match(rf'^\[Measures\]\.\[({_BRACKET_BODY}+)\]$', text)
    if m_meas:
        return ("[Measures]", None, "measure", [unescape_member_key(m_meas.group(1))])

    m = _HIER_BRACKET_RE.match(text)
    if not m:
        return ("", None, "invalid", [])
    hier_bracket = m.group(1)
    tail = m.group(2)

    m_canon = _CANONICAL_TAIL_RE.match(tail)
    if m_canon:
        level_name = unescape_member_key(m_canon.group(1))
        key_path = parse_member_keys(m_canon.group(2))
        return (hier_bracket, level_name, "key", key_path)

    m_key_only = _KEY_ONLY_TAIL_RE.match(tail)
    if m_key_only:
        return (hier_bracket, None, "key", parse_member_keys(m_key_only.group(1)))

    m_bare = _BARE_TAIL_RE.match(tail)
    if m_bare:
        token = unescape_member_key(m_bare.group(1))
        if _norm_all(token):
            return (hier_bracket, None, "all", [])
        return (hier_bracket, None, "caption", [token])

    return ("", None, "invalid", [])


def member_filter_matches(
    member_filter: str | None,
    *,
    candidate_hier_bracket: str,
    candidate_level_name: str | None,
    candidate_key_path: list[str],
    candidate_caption: str | None = None,
) -> bool:
    """Does an inbound ``MEMBER_UNIQUE_NAME`` restriction match a candidate member?

    Dual-grammar (Bug-3617 Phase 1):

    - **Canonical input** (``grammar == "key"``): EXACT member match — the
      hierarchy, the level (when the candidate level is known), and the FULL
      ancestor key path must all be equal. This disambiguates month-4-of-2025
      from month-4-of-2026.
    - **Flat/name input** (the historical ``grammar == "caption"`` parser tag):
      match when the single inbound token equals the candidate's deepest key.
      Legacy caption input remains accepted as a fallback. Caption-only input is
      inherently ambiguous and resolves to all same-caption members, preserving
      the established compatibility behaviour.

    ``None``/empty filter does not match here (callers treat "no filter" as
    "emit all" before reaching this).
    """
    if not member_filter:
        return False
    hier_bracket, level_name, grammar, key_path = parse_member_uname(member_filter)
    if grammar in ("invalid", "all", "measure"):
        return False
    if hier_bracket != candidate_hier_bracket:
        return False

    if grammar == "key":
        if (
            candidate_level_name is not None
            and level_name is not None
            and level_name != candidate_level_name
        ):
            return False
        return key_path == candidate_key_path

    # caption fallback: single token vs candidate caption or deepest key
    token = key_path[0] if key_path else None
    if token is None:
        return False
    deepest = candidate_key_path[-1] if candidate_key_path else None
    return token == candidate_caption or token == deepest


# ---------------------------------------------------------------------------
# DISCOVER <-> Execute canonical identity and compatibility mapping
# ---------------------------------------------------------------------------
#
# Two member-unique-name grammars are supported, selected by hierarchy shape:
#
#   A FLAT, one-data-level hierarchy uses the NAME form
#   ``[Dim].[Hier].[<member key>]``. The bracket token is the stable key, never
#   the display caption. DISCOVER, plain Execute and subtotal Execute all emit
#   this same identity.
#
#   A MULTI-LEVEL hierarchy uses the PATH-QUALIFIED key form
#   ``[Dim].[Hier].[<level>].&[k0]&[k1]...`` so two members with equal captions
#   at the same level but different ancestors stay distinct (month 4 of 2025 vs
#   2026). DISCOVER and every Execute path emit this same identity.
#
# ``canonical_member_uname`` owns that producer decision. The conversion
# functions below remain only for legacy inbound/client compatibility; server
# producers must never use them to publish two identities for one member.


def build_parent_index(
    members_by_level: dict[int, list[dict]],
) -> dict[int, dict[str, str]]:
    """Per-level ``member name -> parent name`` lookup (Bug-9865).

    ``ancestor_key_path_from_parent_chain`` used to find each ancestor by
    scanning the whole level, which makes a whole-hierarchy DISCOVER quadratic
    in the member count. Callers that resolve MANY members over the SAME
    ``members_by_level`` build this index once and pass it in.

    First occurrence wins, exactly as the linear scan's ``break`` did, so a
    duplicated member name resolves to the same parent as before.
    """
    return {
        level_idx: _level_parent_lookup(members)
        for level_idx, members in members_by_level.items()
    }


def _level_parent_lookup(members: list[dict]) -> dict[str, str]:
    """``member name -> parent name`` for one level; first occurrence wins."""
    lookup: dict[str, str] = {}
    for mem in members:
        key = str(mem.get("name", ""))
        if key not in lookup:
            lookup[key] = str(mem.get("parent") or "")
    return lookup


def ancestor_key_path_from_parent_chain(
    member_name: str,
    level_idx: int,
    parent_name: str,
    members_by_level: dict[int, list[dict]],
    *,
    parent_index: dict[int, dict[str, str]] | None = None,
) -> list[str]:
    """Ancestor-first key path for a member, walked from a DISCOVER parent chain.

    DISCOVER (MDSCHEMA_MEMBERS) carries only each member's immediate ``parent``
    name. To map a discovered member to its path-qualified Execute identity the
    full ancestor key path is needed, so this walks up the ``members_by_level``
    parent links and returns ``[root_key, ..., member_name]`` (ending with the
    member itself). Stops early if the chain breaks.

    ``parent_index`` is the ``build_parent_index`` result for the SAME
    ``members_by_level``; supply it when resolving many members so each hop is a
    dict lookup instead of a level scan (Bug-9865). Omitting it keeps the
    previous behaviour, building only the levels this one walk touches.
    """
    index: dict[int, dict[str, str]] = {} if parent_index is None else parent_index
    path = [member_name]
    cur_parent = parent_name
    for lvl in range(level_idx - 1, -1, -1):
        if not cur_parent:
            break
        path.append(cur_parent)
        lookup = index.get(lvl)
        if lookup is None:
            # No index supplied (or this level was absent from it): build the
            # one level this hop needs, memoised for the rest of the walk.
            lookup = _level_parent_lookup(members_by_level.get(lvl, []))
            index[lvl] = lookup
        cur_parent = lookup.get(cur_parent, "")
    path.reverse()
    return path


def execute_uname_to_caption(execute_uname: str) -> str | None:
    """DEPRECATED (Bug-3617 Phase 4). Client reconciliation is no longer needed.

    The gateway now emits ONE canonical member unique name in BOTH DISCOVER and
    Execute (Phase 2), so DISCOVER and Execute strings are already identical for
    multi-level hierarchies — there is nothing to reconcile. Retained as a thin,
    documented compatibility wrapper for any external client still calling it;
    will be removed once external retirement is agreed. New code must not use it.

    Map a path-qualified Execute uname to its DISCOVER caption-form uname.
    ``[Cal].[Cal].[Month].&[2025]&[4]`` -> ``[Cal].[Cal].[4]`` (the caption is
    the deepest key). Returns ``None`` when *execute_uname* is not in the
    path-qualified key grammar (already caption form, or unparseable).
    """
    m = re.match(
        rf'^(\[{_BRACKET_BODY}+\]\.\[{_BRACKET_BODY}+\])\.'
        rf'\[{_BRACKET_BODY}+\]\.({KEY_PATH})$',
        execute_uname,
    )
    if not m:
        return None
    hier_bracket = m.group(1)
    deepest = deepest_member_key(m.group(2))
    if deepest is None:
        return None
    return f"{hier_bracket}.[{escape_member_key(deepest)}]"


def caption_uname_to_execute(
    caption_uname: str,
    level_name: str,
    key_path: list[str],
) -> str | None:
    """DEPRECATED (Bug-3617 Phase 4). Client reconciliation is no longer needed —
    DISCOVER and Execute now emit one canonical uname (Phase 2). Retained as a
    thin compatibility wrapper until external retirement is agreed; new code must
    not use it. (Note: ``ancestor_key_path_from_parent_chain`` is NOT deprecated —
    it is now core to the gateway's own canonical-path resolution.)

    Map a DISCOVER caption-form uname to its path-qualified Execute uname.
    The caption form carries no ancestor context, so the caller supplies the
    member's *level_name* and ancestor-first *key_path* (resolved from the
    DISCOVER parent chain). ``[Cal].[Cal].[4]`` + ``Month`` + ``["2025","4"]``
    -> ``[Cal].[Cal].[Month].&[2025]&[4]``. Returns ``None`` when *caption_uname*
    is not a ``[Dim].[Hier].[member]`` reference.
    """
    m = re.match(
        rf'^(\[{_BRACKET_BODY}+\]\.\[{_BRACKET_BODY}+\])\.\[{_BRACKET_BODY}+\]$',
        caption_uname,
    )
    if not m or not key_path:
        return None
    return qualify_member_uname(m.group(1), level_name, key_path)
