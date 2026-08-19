"""Passenger + forward-diagnostic column construction for artifact builds.

Spec: architecture_derived-grain-aggregate-routing.md §7.6.3 and pitfall 18
(check/build race). To remove the race between a source relationship check and a
later unrelated CTAS, the passenger value and the forward-dependency diagnostics
must be computed from the SAME keyed source statement/snapshot as the measure
rows. A portable shape is a keyed CTE that, per grain key, carries beside the
measure components:

  MIN(detail)                                  -- the passenger value
  COUNT(DISTINCT detail)                       -- forward-dependency diagnostic
  SUM(CASE WHEN detail IS NULL THEN 1 ELSE 0)  -- explicit NULL-endpoint count

The build activates an edge only if every grain key has exactly one distinct,
non-NULL detail (``COUNT(DISTINCT detail) = 1`` and ``null_count = 0``). The
detail is carried as a passenger; it is NEVER added as an independent grain key.

This module produces the SQLGlot expression fragments (canonical postgres) that a
build path splices into its keyed aggregation SELECT, plus the passenger/diagnostic
column names. It does not execute anything and adds no connector branch — the
caller transpiles the complete statement once and executes through source_executor.

Phase 3 note: the DIAGNOSTIC read (used to decide edge activation + write the
manifest) is always safe to build. Physically MATERIALISING the passenger column
into the served CTAS changes the artifact's column shape, so that is gated behind
the build feature flag and stays OFF until serving lands (Phase 5); ordinary
builds remain byte-identical.
"""
from __future__ import annotations

from dataclasses import dataclass

from shared.connector_qualify import quote_identifier


# Suffixes for the generated passenger + diagnostic columns. Kept stable so the
# manifest and the artifact-local verifier agree on physical names.
PASSENGER_SUFFIX = "__passenger"
DISTINCT_COUNT_SUFFIX = "__detail_ndistinct"
NULL_COUNT_SUFFIX = "__detail_nullcount"


@dataclass
class PassengerSpec:
    """One detail to carry as a passenger beside its key on the build.

    ``detail_physical`` is the source detail column name; ``base_name`` is a
    stable, collision-clamped base (typically the relationship id short form or
    the detail column name) used to derive the generated physical column names.
    """
    relationship_id: str
    detail_physical: str
    base_name: str

    @property
    def passenger_column(self) -> str:
        return f"{self.base_name}{PASSENGER_SUFFIX}"

    @property
    def distinct_count_column(self) -> str:
        return f"{self.base_name}{DISTINCT_COUNT_SUFFIX}"

    @property
    def null_count_column(self) -> str:
        return f"{self.base_name}{NULL_COUNT_SUFFIX}"


def build_passenger_select_fragments(
    spec: PassengerSpec, connector: str,
) -> list[str]:
    """Return the ``<expr> AS <alias>`` SELECT fragments for one passenger.

    Canonical-postgres expressions; the caller quotes/transpiles the whole
    statement once. Produces the passenger value plus the two forward-dependency
    diagnostics (distinct-count, NULL-count) from the SAME grouped statement so
    the diagnostics describe exactly the rows that were built (pitfall 18).
    """
    detail = quote_identifier(connector, spec.detail_physical)
    passenger_alias = quote_identifier(connector, spec.passenger_column)
    ndistinct_alias = quote_identifier(connector, spec.distinct_count_column)
    nullcount_alias = quote_identifier(connector, spec.null_count_column)
    return [
        f"MIN({detail}) AS {passenger_alias}",
        f"COUNT(DISTINCT {detail}) AS {ndistinct_alias}",
        f"SUM(CASE WHEN {detail} IS NULL THEN 1 ELSE 0 END) AS {nullcount_alias}",
    ]


def edge_activatable(distinct_count: int | None, null_count: int | None) -> bool:
    """Decide, from the build diagnostics, whether the edge may be activated.

    An edge is activatable only when EVERY grain key has exactly one distinct,
    non-NULL detail. The caller passes the MAX distinct-count and the SUM of
    NULL-counts across all built key rows (or the aggregate of the diagnostic
    read). Any NULL/unknown diagnostic fails closed (spec §7.6.3, I16).
    """
    if distinct_count is None or null_count is None:
        return False
    return distinct_count <= 1 and null_count == 0
