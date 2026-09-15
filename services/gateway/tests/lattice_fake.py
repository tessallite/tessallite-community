"""Bug-9864 -- make a per-grain fake executor answer a lattice request too.

Fakes written for the one-query-per-grain protocol branch on which dimension
names appear in the SQL and return that grain's rows. A ``grouping_sets``
request is ONE query covering every grain, and the real query-router answers it
with the concatenation of every set's rows plus a ``GROUPING()`` marker per
grain column. A fake that has never heard of markers returns detail-shaped rows,
the gateway correctly refuses to split them, and the test silently exercises the
FALLBACK instead of the path it is naming.

``lattice_aware`` closes that gap without rewriting each fake's data: on a
lattice call it asks the inner fake for each grouping set in turn, using a
synthetic GROUP BY naming exactly that set's columns, then stitches the answers
into the single marked result set the router would have returned.

Recording moves into the wrapper so ``sql_calls`` counts the queries the gateway
ACTUALLY issued -- one lattice query -- rather than the wrapper's internal
per-set lookups.
"""

from __future__ import annotations

from typing import Any, Callable

# Must match the query-router's GROUPING_MARKER_PREFIX and the gateway's
# subtotal_engine.grouping_marker_name.
GROUPING_MARKER_PREFIX = "_grouping__"


def _grain_sql(dims: list[str]) -> str:
    """SQL naming exactly *dims*, in the shape these fakes match on."""
    if not dims:
        return 'SELECT SUM("m") AS "m" FROM "t"'
    cols = ", ".join(f'"{d}"' for d in dims)
    return f'SELECT {cols}, SUM("m") AS "m" FROM "t" GROUP BY {cols}'


def lattice_aware(
    inner: Callable[..., Any],
    sql_calls: list[str] | None = None,
) -> Callable[..., Any]:
    """Wrap a per-grain fake ``execute_query`` so it also serves a lattice.

    *inner* must NOT record into ``sql_calls`` itself; pass the list here so the
    wrapper records one entry per query the gateway really issued.
    """

    async def wrapper(model_id, sql, tenant_slug, jwt_token,
                      protocol="dax", **kw):
        grouping_sets = kw.get("grouping_sets")
        if sql_calls is not None:
            sql_calls.append(sql)
        if not grouping_sets:
            return await inner(model_id, sql, tenant_slug, jwt_token,
                               protocol, **kw)

        union: list[str] = []
        for gs in grouping_sets:
            for d in gs:
                if d not in union:
                    union.append(d)

        markers = {d: f"{GROUPING_MARKER_PREFIX}{d}" for d in union}
        measure_cols: list[str] = []
        out_rows: list[dict[str, Any]] = []

        for gs in grouping_sets:
            grain = await inner(
                model_id, _grain_sql(list(gs)), tenant_slug, jwt_token,
                protocol,
            )
            grain_cols = list(grain.get("columns", []))
            for c in grain_cols:
                if c not in union and c not in measure_cols:
                    measure_cols.append(c)
            for row in grain.get("rows", []):
                out = {d: (row.get(d) if d in gs else None) for d in union}
                for d in union:
                    out[markers[d]] = 0 if d in gs else 1
                for c in grain_cols:
                    if c not in union:
                        out[c] = row.get(c)
                out_rows.append(out)

        columns = union + [markers[d] for d in union] + measure_cols
        return {"columns": columns, "rows": out_rows}

    return wrapper
