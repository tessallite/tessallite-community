"""Which Named Queries a persona may see (Bug-9186, audit rows A14/A15/A17).

SQL generation rule 4 says a derivation is visible to a persona **if and only
if it binds over that persona's model query**. A Named Query definition IS a
query over the model, so the bind test needs no probe to be synthesised: the
deployed ``definition_sql`` is submitted to the router's own persona-scoped
plan path and the verdict is whether that path accepts it.

What this replaces: ``shared/named_query/persona_scope.py``'s identifier scan,
which compared the names a definition MENTIONS against the dimension names a
persona filter had already hidden. That rule could not see a restricted
MEASURE, a column-level-security tag or a persona default filter, so a Named
Query aggregating a measure outside the persona's allow-list was advertised on
every catalogue and then refused at execute time -- the catalogue and the
executor disagreeing, which is exactly what rule 4 forbids.

The call site of the "equivalent by construction" re-entry is
``api/routes.py::_handle_explain``, in-process: it parses, binds, applies
``enforce_persona_gate`` (allow-lists, hierarchy levels, measure backing
columns), merges the persona's default filters and routes through
``route_query`` (column-level security, row-security compilation) WITHOUT
executing anything. No exception is the bind; any refusal is not.

Fail CLOSED. A verdict that cannot be proven is not made in the caller's
favour: a transport/DB fault, an unparseable definition and an empty
definition all hide the Named Query. That matches the direction the blunt rule
this replaces already had, so the change can only reveal a Named Query it has
positively cleared.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

__all__ = [
    "clear_named_query_visibility_cache",
    "named_query_definition_sql",
    "visible_named_query_names",
]

# A bind verdict is a function of (model, persona, served definition) and
# nothing else: the persona gate and column-level security are persona-scoped,
# and row security cannot change whether a query BINDS, only which rows it
# would return. Short TTL so a persona edit takes effect quickly. Mirrors
# ``model-service/src/api/named_set_visibility.py`` (Bug-9877, wave 1).
_VERDICT_TTL_SECONDS = 60.0
_VERDICT_CACHE_MAX = 2048
_VERDICT_CACHE: dict[tuple[str, str, str], tuple[float, bool]] = {}


def clear_named_query_visibility_cache() -> None:
    """Drop every cached bind verdict (test hook and persona-edit hook)."""
    _VERDICT_CACHE.clear()


def _cache_get(key: tuple[str, str, str]) -> bool | None:
    hit = _VERDICT_CACHE.get(key)
    if hit is None:
        return None
    if hit[0] <= time.monotonic():
        _VERDICT_CACHE.pop(key, None)
        return None
    return hit[1]


def _cache_put(key: tuple[str, str, str], value: bool) -> None:
    if len(_VERDICT_CACHE) >= _VERDICT_CACHE_MAX:
        oldest = min(_VERDICT_CACHE, key=lambda k: _VERDICT_CACHE[k][0])
        _VERDICT_CACHE.pop(oldest, None)
    _VERDICT_CACHE[key] = (time.monotonic() + _VERDICT_TTL_SECONDS, value)


def named_query_definition_sql(nq: Any) -> str:
    """The deployed definition of *nq*, or ``""`` when it carries none.

    Accepts both the snapshot dict shape and the resolver's dataclass, because
    the catalogue route and the deployed-snapshot readers hold different ones.
    """
    if isinstance(nq, dict):
        value = nq.get("definition_sql")
    else:
        value = getattr(nq, "definition_sql", None)
    return str(value or "").strip()


async def visible_named_query_names(
    named_queries: list[Any],
    *,
    model_id: str,
    persona: Any | None,
    probe: Callable[[str], Awaitable[bool]],
) -> set[str]:
    """The lowercased names of the Named Queries that bind for this persona.

    ``persona is None`` means no persona narrows this caller (a privileged
    caller, or a model with no persona in force): the model's whole surface is
    the persona surface and every definable Named Query stays visible, exactly
    as before. A Named Query with no definition is hidden either way -- there
    is nothing to bind, and the catalogue must not advertise a relation the
    executor would refuse.
    """
    names: set[str] = set()
    persona_key = str(getattr(persona, "id", "")) if persona is not None else ""
    for nq in named_queries:
        name = str(
            (nq.get("name") if isinstance(nq, dict) else getattr(nq, "name", ""))
            or ""
        ).strip()
        if not name:
            continue
        definition = named_query_definition_sql(nq)
        if not definition:
            logger.info(
                "Bug-9186: Named Query %r carries no deployed definition; "
                "withholding it from the catalogue (fail closed).", name,
            )
            continue
        if persona is None:
            names.add(name.lower())
            continue
        key = (str(model_id), persona_key, definition)
        verdict = _cache_get(key)
        if verdict is None:
            verdict = await probe(definition)
            _cache_put(key, verdict)
        if verdict:
            names.add(name.lower())
    return names
