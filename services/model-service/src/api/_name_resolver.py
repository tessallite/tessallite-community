"""
Attribute name uniqueness resolver.

Mirrors the frontend ``resolveUniqueName`` in ClassificationTab.tsx exactly.
Any change to the priority rules must be applied to both.
"""
from __future__ import annotations


def resolve_unique_name(
    col_name: str,
    alias: str,
    physical_name: str,
    taken: set[str],
) -> str:
    """Return the first available unique name for *col_name*.

    Priority:
      1. col_name                         (bare column name)
      2. {alias}_{col_name}               (alias prefix — takes priority)
      3. {physical_name}_{col_name}       (physical table name prefix)
      4. {alias}_2_{col_name}, {alias}_3_{col_name}, …   (sequential)

    ``taken`` must contain lowercase names already in use.  It is **not**
    mutated by this function; the caller is responsible for reserving the
    returned name after each call when processing a batch.
    """
    candidates = [
        col_name,
        f"{alias}_{col_name}",
        f"{physical_name}_{col_name}",
    ]
    for c in candidates:
        if c.lower() not in taken:
            return c
    n = 2
    while True:
        c = f"{alias}_{n}_{col_name}"
        if c.lower() not in taken:
            return c
        n += 1
