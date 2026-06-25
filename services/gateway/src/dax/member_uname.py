"""Single grammar for SSAS-style member unique names.

B8 round-2 fix (deep-review Findings 2/4/6): member unames were emitted
in one grammar (path-qualified composite keys, ``[Dim].[Hier].[Level]
.&[2025]&[4]``) but parsed in another (single-key regexes that captured
only the first ``&[key]``), so a server-emitted uname echoed back by a
client (Excel keep-only, report filter, timeline, DrilldownMember)
resolved to the wrong member. Every site that produces or consumes a
member unique name must go through this module.

Grammar
-------
``<hier_bracket>.[<level_name>].&[k0]&[k1]...&[kn]``

- ``hier_bracket`` is ``[Dim].[Hier]``.
- The key path is ancestor-first: ``k0`` is the root level key and
  ``kn`` is the key of the named level itself.
- The legacy single-key form ``.&[k]`` is the degenerate one-segment
  path; the caption form ``.[Member]`` carries no keys.
- A literal ``]`` inside a key is escaped as ``]]`` (SSAS convention,
  B8 round-3 Bug-1052) on emit and unescaped on parse.

Status: active. Last meaningful update: 2026-06-12.
"""

from __future__ import annotations

import re

# Bracket body: any non-``]`` character, or an escaped ``]]`` pair.
_BRACKET_BODY = r'(?:[^\]]|\]\])'

# One-or-more ``&[key]`` segments. Embeddable, non-capturing.
KEY_PATH = rf'(?:\&\[{_BRACKET_BODY}+\])+'

# Key path optionally starting in caption form: ``&[k]&[k]...`` or ``[m]``.
# Embeddable as a single capturing group.
KEYS_OR_CAPTION = rf'(\&?\[{_BRACKET_BODY}+\](?:\&\[{_BRACKET_BODY}+\])*)'

_KEY_SEGMENT_RE = re.compile(rf'\&\[({_BRACKET_BODY}+)\]')
_CAPTION_RE = re.compile(rf'^\s*\[({_BRACKET_BODY}+)\]\s*$')


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
    round-trips through :func:`parse_member_keys` (Bug-1052).
    """
    keys = "".join(f"&[{escape_member_key(k)}]" for k in key_path)
    return f"{hier_bracket}.[{level_name}].{keys}"


def parse_member_keys(keys_text: str) -> list[str]:
    """Parse the key-path portion of a member reference.

    Returns the ancestor-first key list for ``&[k0]&[k1]...`` input,
    a one-element list for the caption form ``[member]``, and an empty
    list when nothing parses.
    """
    if not keys_text:
        return []
    keys = [unescape_member_key(k).strip() for k in _KEY_SEGMENT_RE.findall(keys_text)]
    if keys:
        return keys
    m = _CAPTION_RE.match(keys_text)
    if m:
        return [unescape_member_key(m.group(1)).strip()]
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
#   "key"     — canonical ``[Dim].[Hier].[Level].&[k0]...&[kn]`` (full ancestor path)
#   "caption" — legacy ``[Dim].[Hier].[Member]`` (single caption/key, no ancestors)
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
# Bare-bracket tail: ``[Member]`` (caption / All).
_BARE_TAIL_RE = re.compile(rf'^\[({_BRACKET_BODY}+)\]$')


def _norm_all(token: str) -> bool:
    """True if a bare member token denotes the (All) member in either form."""
    return token in ("All", "(All)")


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
    - **Caption input** (``grammar == "caption"``): legacy fallback — match when
      the single inbound token equals the candidate's caption OR its deepest key.
      This is inherently ambiguous (resolves to ALL same-caption members),
      preserving today's "not zero rows" behaviour for caption-only clients.

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
# DISCOVER <-> Execute uname mapping (Bug-3617)
# ---------------------------------------------------------------------------
#
# Two member-unique-name grammars coexist on the wire for the SAME member:
#
#   DISCOVER (MDSCHEMA_MEMBERS) and the non-subtotal Execute axis emit the
#   CAPTION form ``[Dim].[Hier].[<member caption>]`` — a flat reference with no
#   ancestor context. This is what Excel sends back as a MEMBER_UNIQUE_NAME
#   restriction and what its filter dropdowns resolve against; the gateway's
#   member-filter parsers and the existing live-validated Excel flows depend on
#   it, so it is the canonical wire identity for member discovery.
#
#   The SUBTOTAL Execute axis emits the PATH-QUALIFIED key form
#   ``[Dim].[Hier].[<level>].&[k0]&[k1]...`` so two members with equal captions
#   at the same level but different ancestors stay distinct (month 4 of 2025 vs
#   2026). This is required for cell/tuple identity inside one Execute response.
#
# A client that joins DISCOVER members to a SUBTOTAL Execute axis therefore sees
# two different strings for the same member. The functions below are the single
# documented bridge between the two grammars: given the hierarchy bracket, the
# level name, and the ancestor-first key path of a member, both grammars are
# derivable, and each can be converted to the other. Every site that needs to
# reconcile a discovered member with a subtotal-axis tuple (or vice versa) must
# go through these — never re-derive the conversion ad hoc.


def ancestor_key_path_from_parent_chain(
    member_name: str,
    level_idx: int,
    parent_name: str,
    members_by_level: dict[int, list[dict]],
) -> list[str]:
    """Ancestor-first key path for a member, walked from a DISCOVER parent chain.

    DISCOVER (MDSCHEMA_MEMBERS) carries only each member's immediate ``parent``
    name. To map a discovered member to its path-qualified Execute identity the
    full ancestor key path is needed, so this walks up the ``members_by_level``
    parent links and returns ``[root_key, ..., member_name]`` (ending with the
    member itself). Stops early if the chain breaks.
    """
    path = [member_name]
    cur_parent = parent_name
    for lvl in range(level_idx - 1, -1, -1):
        if not cur_parent:
            break
        path.append(cur_parent)
        nxt = ""
        for m in members_by_level.get(lvl, []):
            if str(m.get("name", "")) == cur_parent:
                nxt = str(m.get("parent") or "")
                break
        cur_parent = nxt
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
