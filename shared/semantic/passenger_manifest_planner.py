"""Shared pure passenger/diagnostic build-manifest planner (spec §3.6, Gap 6).

This is the TURN-ON half of the derived-grain stage-4 bundle: it turns a pinned
deployed shape + the exact ``ResolvedAggregateLayout`` used for SQL generation into
the passenger + diagnostic build draft that

  1. the CTAS/INSERT SELECT physically materialises (via ``build_select_parts``'s
     ``passenger_fragments`` seam), and
  2. ``advance_artifact_manifest`` validates/hashes/persists (built names only).

It performs NO writes and NO execution — the producer splices the fragments and
passes the draft to the manifest advance, which activates trust in the SAME
lifecycle transaction as the physical build/swap (spec §3.6 lifecycle law).

Everything is gated by the caller behind ``optimizer.derived_expression_auto_build``
(default OFF). When no relationship is eligible (or the flag is off, so the caller
never calls this), the ordinary CTAS SELECT stays byte-identical.

Collision namespace (spec §3.6): grains, passengers, diagnostics, measures,
quantiles/statistics, and ``__row_count__count`` share ONE case-folded namespace.
:func:`plan_passengers` reserves the whole existing set before it allocates a
passenger/diagnostic name, deterministically clamps a collision using the
relationship id + column role, and — if uniqueness still cannot be proven after
connector normalization — FAILS (raises :class:`PassengerCollisionError`) BEFORE any
DDL is emitted (never a silent overwrite of a measure/grain column).

Fail-closed: a relationship whose owning key is not a materialised PHYSICAL grain
key, or whose detail column cannot be resolved to a stable id in the layout's FROM
scope, is SKIPPED (no passenger, so it earns no serving trust) rather than emitted
with a guessed reference.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from shared.connector_qualify import quote_identifier
from shared.semantic.artifact_manifest import (
    KIND_PHYSICAL_COLUMN,
    MaterializedAttributeEdge,
    MaterializedGrainKey,
    PassengerColumn,
)
from shared.semantic.grain_resolver import bound_ident
from shared.semantic.passenger_build import (
    DISTINCT_COUNT_SUFFIX,
    NULL_COUNT_SUFFIX,
    PASSENGER_SUFFIX,
)

logger = logging.getLogger(__name__)


class PassengerCollisionError(ValueError):
    """A passenger/diagnostic physical name could not be made unique.

    Raised BEFORE any DDL is emitted so the build fails loudly rather than
    silently overwriting a grain/measure/row-count column (spec §3.6).
    """


@dataclass
class PassengerPlan:
    """One eligible relationship's built passenger + diagnostics + edge draft."""

    relationship_id: str
    attribute_key: str
    key_grain_key_id: str
    key_grain_column: str
    passenger_column: str
    detail_ndistinct_column: str
    detail_nullcount_column: str
    detail_column_id: str
    detail_type: Optional[str]
    cardinality: str
    declaration_hash: str
    # Pre-rendered, connector-quoted ``<expr> AS <alias>`` SELECT fragments (spec
    # §3.6): MIN(detail), COUNT(DISTINCT detail), SUM(CASE WHEN detail IS NULL ...).
    select_fragments: list[str] = field(default_factory=list)

    def passenger_column_draft(self) -> PassengerColumn:
        return PassengerColumn(
            passenger_column=self.passenger_column,
            source_column_id=self.detail_column_id,
            output_type=self.detail_type,
            nullable=False,
            relationship_id=self.relationship_id,
            attribute_key=self.attribute_key,
            detail_ndistinct_column=self.detail_ndistinct_column,
            detail_nullcount_column=self.detail_nullcount_column,
        )

    def edge_draft(self, *, artifact_refresh_run_id: str) -> MaterializedAttributeEdge:
        return MaterializedAttributeEdge(
            relationship_id=self.relationship_id,
            key_grain_column=self.key_grain_column,
            detail_passenger_column=self.passenger_column,
            cardinality=self.cardinality,
            declaration_hash=self.declaration_hash,
            artifact_refresh_run_id=artifact_refresh_run_id,
            attribute_key=self.attribute_key,
            key_grain_key_id=self.key_grain_key_id,
        )


@dataclass
class PassengerBuildDraft:
    """Ordered passenger plans + the pre-rendered SELECT fragment list."""

    plans: list[PassengerPlan] = field(default_factory=list)

    @property
    def select_fragments(self) -> list[str]:
        frags: list[str] = []
        for p in self.plans:
            frags.extend(p.select_fragments)
        return frags

    def passenger_columns(self) -> list[PassengerColumn]:
        return [p.passenger_column_draft() for p in self.plans]

    def column_defs(
        self, *, target_dialect: str, source_dialect: Optional[str] = None,
    ) -> list[tuple[str, str]]:
        """``(name, type)`` pairs for the cross-db CREATE TABLE column list.

        Ordered exactly as the SELECT fragments (passenger, ndistinct, nullcount per
        plan), so the explicit column list stays in lock-step with the executable
        SELECT. The passenger column keeps the detail's source-faithful type
        (``MIN(detail)`` preserves the type); the two diagnostics are integer counts.
        """
        from shared.aggregate_type_mapping import (
            aggregate_result_type,
            grain_column_type,
        )

        count_type = aggregate_result_type("count", target_dialect)
        defs: list[tuple[str, str]] = []
        for p in self.plans:
            passenger_type = grain_column_type(
                target_dialect,
                source_type=p.detail_type,
                source_dialect=source_dialect if p.detail_type else None,
            )
            defs.append((p.passenger_column, passenger_type))
            defs.append((p.detail_ndistinct_column, count_type))
            defs.append((p.detail_nullcount_column, count_type))
        return defs

    def edges(self, *, artifact_refresh_run_id: str) -> list[MaterializedAttributeEdge]:
        return [
            p.edge_draft(artifact_refresh_run_id=str(artifact_refresh_run_id))
            for p in self.plans
        ]

    def is_empty(self) -> bool:
        return not self.plans


def _key_grain_key_by_source_col(
    grain_keys: list[MaterializedGrainKey],
) -> dict[str, MaterializedGrainKey]:
    """Index the PHYSICAL grain keys by the single source column they materialise.

    Restricted to ``kind=PHYSICAL_COLUMN`` + ``dim:`` prefix + a SINGLE-column
    lineage (mirrors ``advance_artifact_manifest``): an expression grain key whose
    first leaf happens to be the relationship key column must NEVER be selected as
    the edge's key grain (that would point the edge at an ``expr:`` id, not the
    physical dim).
    """
    out: dict[str, MaterializedGrainKey] = {}
    for gk in grain_keys:
        key_id = str(getattr(gk, "key_id", "") or "")
        inputs = list(getattr(gk, "input_column_ids", None) or [])
        if (
            key_id.startswith("dim:")
            and getattr(gk, "kind", None) == KIND_PHYSICAL_COLUMN
            and len(inputs) == 1
        ):
            out[str(inputs[0])] = gk
    return out


def reserved_stat_names(
    layout: Any, *, include_quantiles: bool, include_stats: bool,
) -> list[str]:
    """Physical names of quantile/stat columns a build may emit (spec §3.6 namespace).

    Fable R1 #7: quantiles/statistics share the ONE collision namespace with
    passengers. The quantile/stat columns are emitted by the DDL builders as
    ``bound_ident(f"{measure_name}__{stat_type}")`` for sum/avg measures — they are
    NOT all present in ``layout.measure_cols`` (they are appended by the builder), so
    ``_reserved_namespace`` alone misses them. This over-reserves the full canonical
    quantile + dispersion-stat suffix set for every base measure, which is SAFE
    (reserving more never mis-emits; it only forces a passenger to disambiguate). The
    real collision is unreachable today (disjoint suffixes) but this makes the
    namespace complete per spec, not merely lucky.
    """
    if not (include_quantiles or include_stats):
        return []
    from shared.aggregate_quantiles import QUANTILE_STAT_TYPES
    from shared.aggregate_stats import STAT_TYPES

    names: list[str] = []
    base_names = {
        m.measure_name for m in getattr(layout, "measure_cols", []) or []
    }
    for base in base_names:
        if include_quantiles:
            for suf in QUANTILE_STAT_TYPES:
                names.append(bound_ident(f"{base}__{suf}"))
        if include_stats:
            for suf in STAT_TYPES:
                names.append(bound_ident(f"{base}__{suf}"))
    return names


def passenger_fragment_connector(*, connector: str, cross_db: bool) -> str:
    """The connector whose quoting the passenger SELECT fragments must use.

    Fable R1 #2/#3: pre-rendered fragment TEXT is spliced verbatim into a statement,
    so its identifier quoting must match the dialect that statement is PARSED as —
    not the caller's target connector. There are two statement regimes:

      * A statement that is emitted verbatim in the TARGET dialect — same-DB
        BigQuery (``build_bq_ctas``) and same-DB Spark (``build_spark_ctas``) — needs
        fragments quoted in that target dialect (backticks).
      * A statement that is built CANONICAL-PostgreSQL and then transpiled once via
        sqlglot (``read="postgres"``) — every cross-DB path (executable SELECT runs on
        the source), AND same-DB PG-FAMILY targets that transpile to
        redshift/snowflake/sqlserver — needs PG-canonical fragments (double-quotes),
        or the transpile/parse fails (e.g. tsql brackets or bq backticks inside a
        postgres-dialect statement).

    So: target-dialect quoting ONLY for same-DB bigquery/hadoop_spark; PostgreSQL
    canonical everywhere else (all cross-DB, and same-DB postgresql/redshift/
    snowflake/sqlserver which all go through the canonical-PG + transpile path).
    """
    if not cross_db and connector in ("bigquery", "hadoop_spark"):
        return connector
    return "postgresql"


def _casefold(name: str) -> str:
    return (name or "").lower()


def _reserved_namespace(
    layout: Any, extra_names: Optional[list[str]] = None,
) -> set[str]:
    """The full case-folded namespace already claimed by the built table.

    Grains + measures + row count + any caller-supplied quantile/stat/extra names.
    Passenger + diagnostic names must not collide with ANY of these (spec §3.6).
    """
    reserved: set[str] = set()
    for g in layout.grain_cols:
        reserved.add(_casefold(g.physical_col_name))
    for m in layout.measure_cols:
        reserved.add(_casefold(m.physical_col_name))
    reserved.add(_casefold("__row_count__count"))
    for n in (extra_names or []):
        reserved.add(_casefold(n))
    return reserved


def _allocate_name(
    base: str, role: str, relationship_id: str, reserved: set[str],
) -> str:
    """Allocate one collision-free, length-clamped physical name for a role.

    First choice is ``bound_ident(base)`` (matches every other emitted column's
    length discipline). On a case-folded collision, deterministically disambiguate
    with the relationship id + role, re-clamp, and re-check. If it STILL collides,
    raise :class:`PassengerCollisionError` BEFORE any DDL (spec §3.6). ``reserved``
    is updated in place so passenger/diagnostic names of the same build are mutually
    exclusive too.
    """
    candidate = bound_ident(base)
    if _casefold(candidate) not in reserved:
        reserved.add(_casefold(candidate))
        return candidate
    # Deterministic disambiguation: relationship id short form + role.
    rel_short = str(relationship_id).replace("-", "")[:8]
    disambiguated = bound_ident(f"{base}__{rel_short}__{role}")
    if _casefold(disambiguated) not in reserved:
        reserved.add(_casefold(disambiguated))
        return disambiguated
    raise PassengerCollisionError(
        f"passenger/diagnostic name for relationship {relationship_id} role {role!r} "
        f"collides with an existing built column even after disambiguation "
        f"(base={base!r}, candidate={candidate!r}, disambiguated={disambiguated!r}); "
        f"failing before DDL to avoid overwriting a grain/measure column"
    )


def plan_passengers(
    *,
    layout: Any,
    grain_keys: list[MaterializedGrainKey],
    relationships: list[Any],
    columns_by_id: dict[str, Any],
    alias_by_table_id: dict[Any, str],
    connector: str,
    extra_reserved_names: Optional[list[str]] = None,
) -> PassengerBuildDraft:
    """Build the ordered passenger/diagnostic draft for one aggregate layout.

    The relationship's owning-key membership in this layout is proven directly from
    ``grain_keys`` (the built PHYSICAL grain key that materialises the key column) —
    not from a dimension map — so no ``dimensions_by_id`` is needed (Fable R1 #10).

    Args:
      layout: the ``ResolvedAggregateLayout`` used for SQL generation (its physical
        names seed the collision namespace).
      grain_keys: the ordered ``MaterializedGrainKey`` list already built for this
        layout (the passenger's owning key grain is resolved from it by source col).
      relationships: enabled deployed ``DimensionAttributeRelationship`` rows.
      columns_by_id: str(ModelColumn.id) -> ModelColumn, to resolve the detail
        column's ``model_table_id`` + physical name for the qualified ``detail_ref``.
      alias_by_table_id: source-table-id -> FROM-clause alias (same map the DDL
        builder uses to qualify grains/measures), so the passenger reads the SAME
        aliased relation.
      connector: canonical connector type for identifier quoting (PG double-quote,
        BigQuery backtick — spec requires BOTH work).
      extra_reserved_names: quantile/stat physical names the caller emits (so a
        passenger never collides with a pNN/stddev column).

    Returns a :class:`PassengerBuildDraft` (possibly empty). Raises
    :class:`PassengerCollisionError` on an unresolvable name collision (before DDL).
    """
    draft = PassengerBuildDraft()
    if not relationships:
        return draft

    key_grain_by_col = _key_grain_key_by_source_col(grain_keys)
    reserved = _reserved_namespace(layout, extra_reserved_names)

    for rel in relationships:
        detail_col_id = getattr(rel, "detail_column_id", None)
        key_col_id = getattr(rel, "key_column_id", None)
        if detail_col_id is None or key_col_id is None:
            continue
        # Bug-7877: only materialise passengers for BIJECTION relationships. The
        # serving binder (derived_relabel_binder.py:217) rejects non-BIJECTION
        # relabels, so passengers for FUNCTIONAL_N_TO_1 or unknown cardinalities
        # can never be served. Skip them to avoid wasted CTAS width/build cost.
        rel_cardinality = str(getattr(rel, "cardinality", "") or "")
        if rel_cardinality != "BIJECTION":
            continue
        detail_col_id = str(detail_col_id)
        key_col_id = str(key_col_id)

        # The relationship's KEY column must be a materialised PHYSICAL grain key of
        # THIS layout — otherwise there is no built key column to sit the passenger
        # beside (fail closed: skip, no passenger, no trust).
        key_grain = key_grain_by_col.get(key_col_id)
        if key_grain is None:
            continue

        # Resolve the detail column's stable source table + physical name so the
        # passenger reads the correctly-aliased relation (never a name guess).
        detail_col = columns_by_id.get(detail_col_id)
        if detail_col is None:
            continue
        detail_table_id = getattr(detail_col, "model_table_id", None)
        detail_phys = getattr(detail_col, "column_name", None)
        if detail_table_id is None or not detail_phys:
            continue
        # Fail-closed (Fable R1 #4): the detail table MUST be in the layout's FROM
        # scope so the passenger reads the correctly-aliased relation. A missing
        # alias means an unqualified ``MIN("detail")`` — if another joined table has
        # a same-named column, the passenger + diagnostics would compute over the
        # WRONG column and a VERIFIED edge could serve wrong relabel values. Skip
        # (no passenger, no trust) rather than emit a bare, ambiguous reference.
        alias = alias_by_table_id.get(detail_table_id)
        if not alias:
            logger.debug(
                "passenger skipped: detail table %s for relationship %s not in "
                "the layout FROM scope (no alias) — fail-closed",
                detail_table_id, rel.id,
            )
            continue

        # Allocate the three collision-clamped physical names in ONE shared
        # namespace (spec §3.6). base = the detail physical name (readable), clamped.
        base = str(detail_phys)
        passenger_col = _allocate_name(f"{base}{PASSENGER_SUFFIX}", "passenger", rel.id, reserved)
        ndistinct_col = _allocate_name(f"{base}{DISTINCT_COUNT_SUFFIX}", "ndistinct", rel.id, reserved)
        nullcount_col = _allocate_name(f"{base}{NULL_COUNT_SUFFIX}", "nullcount", rel.id, reserved)

        # Qualified, connector-quoted detail reference (spec §3.6): resolved from
        # the stable source table id via alias_by_table_id, quoted through
        # connector_qualify (PG "col" / BigQuery `col`). The whole statement is
        # transpiled once by the DDL builder — this fragment is canonical for the
        # builder's dialect (PG builders emit PG-quoted; BigQuery builder emits
        # BigQuery-quoted); the passenger fragment matches that dialect exactly.
        detail_ref = quote_identifier(connector, str(detail_phys))
        if alias:
            detail_ref = f"{alias}.{detail_ref}"
        p_alias = quote_identifier(connector, passenger_col)
        nd_alias = quote_identifier(connector, ndistinct_col)
        nc_alias = quote_identifier(connector, nullcount_col)
        fragments = [
            f"MIN({detail_ref}) AS {p_alias}",
            f"COUNT(DISTINCT {detail_ref}) AS {nd_alias}",
            f"SUM(CASE WHEN {detail_ref} IS NULL THEN 1 ELSE 0 END) AS {nc_alias}",
        ]

        draft.plans.append(PassengerPlan(
            relationship_id=str(rel.id),
            attribute_key=f"attr:{rel.id}",
            key_grain_key_id=str(key_grain.key_id),
            key_grain_column=str(key_grain.physical_column),
            passenger_column=passenger_col,
            detail_ndistinct_column=ndistinct_col,
            detail_nullcount_column=nullcount_col,
            detail_column_id=detail_col_id,
            detail_type=getattr(detail_col, "data_type", None),
            cardinality=str(getattr(rel, "cardinality", "") or ""),
            declaration_hash=str(getattr(rel, "declaration_hash", "") or ""),
            select_fragments=fragments,
        ))

    return draft


async def load_passenger_draft_if_enabled(
    *,
    db: Any,
    model_id: Any,
    layout: Any,
    grain_keys: list[MaterializedGrainKey],
    model_columns: list[Any],
    alias_by_table_id: dict[Any, str],
    connector: str,
    extra_reserved_names: Optional[list[str]] = None,
) -> Optional[PassengerBuildDraft]:
    """Producer helper: build the passenger draft ONLY when the flag is on.

    Reads ``optimizer.derived_expression_auto_build`` (default off) and
    ``model.attribute_relationship_verification`` (off => no edge trust). Returns
    ``None`` when either says do-not-materialise (the caller then emits the ordinary
    byte-identical CTAS and passes ``passenger_draft=None`` to the manifest advance),
    or a :class:`PassengerBuildDraft` (possibly empty) otherwise. Loading the enabled
    relationships + building the draft is centralised here so the three producers
    (creator, full-refresh, incremental) never drift.

    Raises :class:`PassengerCollisionError` on an unresolvable name collision (the
    caller lets it propagate so the build fails loudly BEFORE any DDL — spec §3.6).
    """
    from sqlalchemy import select

    from shared.config.resolver import get_setting
    from shared.db.models import DimensionAttributeRelationship

    try:
        build_mode = await get_setting(
            "optimizer.derived_expression_auto_build",
            tenant_session=db, model_id=model_id,
        )
    except Exception:
        build_mode = "off"
    if str(build_mode).lower() not in ("approval", "automatic"):
        return None
    try:
        verif_mode = await get_setting(
            "model.attribute_relationship_verification",
            tenant_session=db, model_id=model_id,
        )
    except Exception:
        # Fail to "off" (Fable R1 #11): a transient settings-read error must NOT
        # flip the CTAS into materialising passengers (a physical-shape change) —
        # keep it byte-identical for shape stability, symmetric with the build-flag
        # fail-to-off above. No trust impact (advance gates independently).
        verif_mode = "off"
    if str(verif_mode).lower() == "off":
        # Verification disabled: never materialise passengers (they only exist to
        # earn edge trust, which is off) so the CTAS stays byte-identical.
        return None

    rels = (
        await db.execute(
            select(DimensionAttributeRelationship).where(
                DimensionAttributeRelationship.model_id == model_id,
                DimensionAttributeRelationship.enabled.is_(True),
            )
            # Deterministic order (Fable R1 #6): plan order fixes physical column
            # order + collision-disambiguation winners, and must match the order
            # advance_artifact_manifest persists so the incremental shape guard's
            # list comparison is stable across builds (spec §3.6 determinism).
            .order_by(DimensionAttributeRelationship.id)
        )
    ).scalars().all()
    if not rels:
        return PassengerBuildDraft()

    columns_by_id = {
        str(c.id): c for c in model_columns if getattr(c, "id", None) is not None
    }
    return plan_passengers(
        layout=layout,
        grain_keys=grain_keys,
        relationships=list(rels),
        columns_by_id=columns_by_id,
        alias_by_table_id=alias_by_table_id,
        connector=connector,
        extra_reserved_names=extra_reserved_names,
    )
