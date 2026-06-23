"""MDX construct validators — reject patterns we cannot translate to SQL."""
from __future__ import annotations

import re

from src.dax.member_uname import KEYS_OR_CAPTION

_UNSUPPORTED_MDX_FUNCTIONS = re.compile(
    r'\b(Except|Intersect|Order|Filter|TopCount|BottomCount)\s*\(',
    re.IGNORECASE,
)

# A range endpoint is ``[Dim].[Hier]`` with an optional ``[Level]`` segment,
# followed by a key path (single OR composite ``&[k0]&[k1]...``) or a
# caption member. B8 round-3 (Bug-1051): the previous single-key pattern
# let composite paths such as ``[Month].&[2025]&[5]:[Month].&[2025]&[7]``
# bypass the intended clean rejection — built from the shared
# ``member_uname`` grammar fragments so every key shape is covered.
_RANGE_ENDPOINT = (
    r'\[[^\]]+\]\.\[[^\]]+\]\.(?:\[[^\]]+\]\.)?' + KEYS_OR_CAPTION
)

_MDX_MEMBER_RANGE = re.compile(
    _RANGE_ENDPOINT + r'\s*:\s*' + _RANGE_ENDPOINT,
)

def check_unsupported_mdx_constructs(
    expr: str,
    label: str,
    *,
    allow_range: bool = False,
    allow_topn: bool = False,
    allow_filter: bool = False,
) -> None:
    """Raise ValueError if *expr* contains MDX set functions we cannot translate."""
    if not expr:
        return
    for m in _UNSUPPORTED_MDX_FUNCTIONS.finditer(expr):
        func_name = m.group(1)
        fn_lower = func_name.lower()
        if fn_lower == "filter" and allow_filter:
            continue
        if fn_lower in ("topcount", "bottomcount") and allow_topn:
            # The caller verified the single supported Top-N shape was
            # extracted and will be translated to ORDER BY ... LIMIT; only an
            # allowed, consumed Top-N reaches here (F-002-05).
            continue
        if fn_lower == "order":
            # F-002-17: Order() commonly arrives from an Excel "Sort by value"
            # action. It is rejected (rather than silently dropped) because
            # ignoring the sort would change the row order — and, combined with
            # a Top-N/MAXROWS limit, the actual rows returned. Give an
            # actionable message so the user knows the DATA is fine and only the
            # server-side sort is unsupported, instead of a bare "cannot
            # translate" fault.
            raise ValueError(
                f"MDX Order() in {label} is not supported for server-side "
                f"sorting. The underlying values are correct; sort the result "
                f"in your BI client instead. (Server-side Order() is rejected "
                f"rather than ignored so the row order — and any Top-N limit — "
                f"is never silently changed.)"
            )
        raise ValueError(
            f"Unsupported MDX function '{func_name}()' in {label}. "
            f"This function's semantics cannot be translated to SQL; "
            f"the query would return incorrect results."
        )
    if not allow_range and _MDX_MEMBER_RANGE.search(expr):
        raise ValueError(
            f"Unsupported MDX member range operator ':' in {label}. "
            f"Range expressions cannot be translated to SQL; "
            f"the query would return incorrect results."
        )
