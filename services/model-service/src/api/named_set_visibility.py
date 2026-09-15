"""Persona visibility for named sets and Tessallite Named Lists.

Bug-9877 / audit rows A18, A19, A20, A39. SQL generation rule 4 says a
derivation is visible to a persona **if and only if it binds over that
persona's model query**, and that visibility must follow from binding rather
than from a separate check on model metadata.

What this module replaces: a bracket-token regex over the stored MDX that
compared only DIMENSION names against ``included_dimension_ids``, ignored the
ranking/filter MEASURE inside ``TopCount(...)`` / ``Filter(...)`` entirely, and
left any token it could not resolve VISIBLE (fail-open).

How it decides now — the "equivalent by construction" shape of the rule:

1. ``shared.semantic.named_set_binding`` says which model dimensions and
   measures the definition references (the builder definition when there is
   one, otherwise the MDX chains).
2. Every candidate is resolved against the model's real dimensions/measures
   here. A candidate that resolves to neither hides the set — fail CLOSED.
3. The referenced objects are assembled into the model query the set layers on
   top of, and that query is submitted to the query-router's own persona-scoped
   path: **call site ``POST {QUERY_ROUTER_URL}/api/v1/explain``** with the
   caller's ``persona_id``. ``/explain`` parses, binds, applies
   ``enforce_persona_gate`` (allow-lists, hierarchy levels, measure backing
   columns), merges persona default filters and routes through ``route_query``
   (column-level security, row-security compilation) without executing
   anything. A 2xx is the bind; anything else is not.

Because Discover (``MDSCHEMA_SETS``), Execute-time inlining and the member
preview all consult this one verdict, the catalogue and the executor cannot
disagree.
"""
from __future__ import annotations

import logging
import time
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import select

from shared.config.settings import get_settings
from shared.db.models import Dimension, Measure
from shared.semantic.named_set_binding import (
    build_binding_probe_sql,
    extract_named_set_references,
)

log = logging.getLogger(__name__)
_settings = get_settings()

_AGG_FUNCS = {"sum", "count", "avg", "min", "max", "count_distinct"}

# A bind verdict is a function of (model, persona, served definition), so it is
# cached on exactly those three. Nothing user-specific enters the key because
# nothing user-specific enters the verdict: /explain's persona gate and CLS are
# persona-scoped, and row security cannot change whether a query BINDS, only
# which rows it would return. Short TTL so a persona edit takes effect quickly.
_VERDICT_TTL_SECONDS = 60.0
_VERDICT_CACHE_MAX = 2048
_VERDICT_CACHE: dict[tuple[str, str, str], tuple[float, bool]] = {}


def clear_named_set_visibility_cache() -> None:
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


def _definition_fingerprint(ns: Any) -> str:
    """Identity of the DEFINITION whose bind is being cached."""
    return repr((
        str(getattr(ns, "id", "")),
        getattr(ns, "expression", None),
        getattr(ns, "dimensions", None),
        repr(getattr(ns, "builder_definition", None)),
    ))


def _agg_expr(column: str, default_agg: str | None) -> str:
    from shared.connector_qualify import safe_ident

    agg = (default_agg or "sum").lower()
    if agg not in _AGG_FUNCS:
        agg = "sum"
    ident = safe_ident(column)
    if agg == "count_distinct":
        return f"COUNT(DISTINCT {ident})"
    return f"{agg.upper()}({ident})"


class ModelSurface:
    """The model's dimension and measure names, resolved once per request.

    Built once and passed to every set so a list of N sets costs one pair of
    queries, not N (the N+1 fix Bug-7255 made for the old scan is preserved).
    """

    __slots__ = ("dimensions", "measures")

    def __init__(self, dimensions: dict[str, str], measures: dict[str, tuple[str, str]]):
        self.dimensions = dimensions
        self.measures = measures

    @classmethod
    async def load(cls, db: Any, model_id: UUID) -> "ModelSurface":
        dim_rows = await db.execute(
            select(Dimension.name).where(Dimension.model_id == model_id)
        )
        dimensions = {name.lower(): name for (name,) in dim_rows.all()}
        meas_rows = await db.execute(
            select(Measure.name, Measure.default_agg).where(
                Measure.model_id == model_id
            )
        )
        measures = {
            name.lower(): (name, agg or "sum") for name, agg in meas_rows.all()
        }
        return cls(dimensions, measures)

    def resolve_dimension(self, candidate: str) -> str | None:
        """Resolve a dimension candidate to its real model dimension name.

        A builder ``entity`` is authored as ``dimension.hierarchy.level`` in a
        single MDX bracket (``named_list_compiler._compile_top_n``), so the
        dotted head is tried after the whole token.
        """
        token = candidate.strip()
        if not token:
            return None
        hit = self.dimensions.get(token.lower())
        if hit is not None:
            return hit
        if "." in token:
            return self.dimensions.get(token.split(".", 1)[0].strip().lower())
        return None

    def resolve_measure(self, candidate: str) -> tuple[str, str] | None:
        token = candidate.strip()
        if not token:
            return None
        return self.measures.get(token.lower())


def build_probe_for_named_set(
    ns: Any, surface: ModelSurface, model_slug: str
) -> str | None:
    """The model query this named set layers on top of, or ``None``.

    ``None`` means the definition could not be resolved to model objects at
    all; the caller must then treat the set as NOT bindable (fail closed).
    """
    refs = extract_named_set_references(
        expression=getattr(ns, "expression", None),
        builder_definition=getattr(ns, "builder_definition", None),
        persisted_dimensions=getattr(ns, "dimensions", None),
    )
    if refs.undecidable:
        log.info(
            "Named set %s cannot be decomposed for a bind test (%s); hiding it "
            "from restricted personas (Bug-9877).",
            getattr(ns, "id", "?"), "; ".join(refs.undecidable),
        )
        return None

    if refs.raw_sql:
        # A sql_query Named List IS a model query; probe it verbatim.
        return refs.raw_sql

    dimension_columns: list[str] = []
    for candidate in refs.dimension_names:
        resolved = surface.resolve_dimension(candidate)
        if resolved is None:
            log.info(
                "Named set %s references '%s', which resolves to no model "
                "dimension; hiding it from restricted personas (Bug-9877).",
                getattr(ns, "id", "?"), candidate,
            )
            return None
        dimension_columns.append(resolved)

    measure_aggregates: list[str] = []
    for candidate in refs.measure_names:
        resolved_measure = surface.resolve_measure(candidate)
        if resolved_measure is None:
            log.info(
                "Named set %s references measure '%s', which resolves to no "
                "model measure; hiding it from restricted personas (Bug-9877).",
                getattr(ns, "id", "?"), candidate,
            )
            return None
        measure_aggregates.append(_agg_expr(*resolved_measure))

    try:
        return build_binding_probe_sql(
            model_relation=model_slug,
            dimension_columns=dimension_columns,
            measure_aggregates=measure_aggregates,
        )
    except ValueError:
        return None


async def probe_binds(
    *, model_id: UUID, sql: str, persona_id: str, bearer: str, timeout_s: float = 15.0
) -> bool:
    """Ask the query-router whether *sql* binds for *persona_id*.

    Call site of the rule-4 "equivalent by construction" re-entry:
    ``POST /api/v1/explain``. Parse, bind, persona gate, default filters,
    column-level security and row-security compilation all run there; nothing
    is executed. Any non-2xx — including a transport failure — is NOT a bind.
    """
    url = f"{_settings.QUERY_ROUTER_URL}/api/v1/explain"
    body = {
        "model_id": str(model_id),
        "raw_query": sql,
        "protocol": "jdbc",
        "persona_id": persona_id,
    }
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.post(
                url, json=body, headers={"Authorization": f"Bearer {bearer}"}
            )
    except Exception as exc:
        # Fail CLOSED: a visibility decision that cannot be proven is not made
        # in the caller's favour. Logged loudly because "every set disappeared"
        # must be diagnosable.
        log.error(
            "Named-set bind probe could not reach the query-router for model "
            "%s persona %s; hiding the set (Bug-9877): %s",
            model_id, persona_id, exc,
        )
        return False
    if resp.status_code < 400:
        return True
    log.debug(
        "Named-set bind probe refused for model %s persona %s: HTTP %s",
        model_id, persona_id, resp.status_code,
    )
    return False


async def named_set_binds_for_persona(
    ns: Any,
    *,
    model_id: UUID,
    model_slug: str,
    surface: ModelSurface,
    persona: Any | None,
    bearer: str,
) -> bool:
    """True when this named set binds over the persona's model query.

    ``persona is None`` means no persona is in force (a modeller in the model
    builder): the model's whole surface is the persona surface, and the set
    stays visible exactly as before.
    """
    if persona is None:
        return True

    persona_id = str(persona.id)
    key = (str(model_id), persona_id, _definition_fingerprint(ns))
    cached = _cache_get(key)
    if cached is not None:
        return cached

    sql = build_probe_for_named_set(ns, surface, model_slug)
    if sql is None:
        _cache_put(key, False)
        return False

    verdict = await probe_binds(
        model_id=model_id, sql=sql, persona_id=persona_id, bearer=bearer,
    )
    _cache_put(key, verdict)
    return verdict
