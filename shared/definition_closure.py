"""Canonical projection of a model's build-relevant definitions (Bug-8250).

An aggregate CTAS is materialised from the LIVE model graph, while the
query-router binds every query to the DEPLOYED SNAPSHOT. Those two must agree
or the artifact holds numbers the router will label as the deployed version's —
a silent wrong number over JDBC/XMLA.

This module owns the *comparison shape*: it projects both sides into the same
canonical structure so they can be diffed field-for-field, and it computes a
digest of that structure so a caller can detect the live side moving during a
build. It performs no I/O; :mod:`shared.deployed_definition_drift` supplies the
live rows and :mod:`shared.artifact_definition_binding` owns the build-window
lifecycle.

Why a whole-row projection instead of a field list
--------------------------------------------------
The predecessor guard compared four hand-picked ``Measure`` attributes. That
list was, by construction, blind to everything not on it: grain-dimension
bindings, the join graph, table/column physical identity — every one of which
determines the numbers a CTAS produces. A hand-picked list also silently stops
covering a field the moment someone adds a column to the ORM.

So both sides are normalised with ``model_snapshot.serialiser.row_to_snapshot_dict``
— the SAME function that wrote the snapshot — and compared whole, minus an
explicit, justified exclusion set. Adding an ORM column extends the comparison
automatically instead of quietly leaving a hole.

Comparison tiers
----------------
**Tier A — bidirectional.** Presence on exactly one side is drift, because an
ADDITION can change what the build produces:

* ``joins`` — a new edge changes path-finding and therefore the FROM clause.
* ``measures`` (closure-scoped) — the measures this aggregate materialises plus
  their transitive calculated-measure dependencies.
* ``dimensions`` (grain-scoped) — the dimensions the aggregate's grain names
  resolve against.
* ``attribute_relationships`` — the passenger planner reads every enabled
  BIJECTION declaration model-wide. A live addition can allocate or displace a
  physical passenger column even when the deployed snapshot has no corresponding
  row. Disabled and non-BIJECTION declarations are inert and excluded.

Measures and dimensions are scoped to the closure so that editing an unrelated
measure does not refuse every refresh on the model. Within the closure the
comparison is bidirectional, so a rename that collides with a closure name is
still caught.

**Tier B — snapshot-authoritative.** Everything the snapshot names must exist
live and match; live-only rows are ignored:

* ``columns`` — the source-schema scan/stats sweep legitimately ADDS columns
  with no modelling act, and an unreferenced new column cannot change a CTAS
  that names its columns. A removed or renamed column is still drift.
* ``tables`` — additions are handled by the fact-anchor rule below rather than
  bidirectionally, because a table can only enter the FROM clause through a
  join (Tier A) or by becoming the BFS anchor.
* ``user_defined_attributes`` / ``uda_column_refs`` — auto-generated UDAs exist;
  an added UDA is inert unless a Tier-A dimension/measure references it, and
  that reference is itself compared.
* ``hierarchies`` (with nested levels and attributes) — content changes are
  drift. Known residual, deliberately accepted rather than hidden: a live-only
  hierarchy LEVEL whose name collides with a grain name is not detected here;
  it surfaces instead as an unresolvable-or-changed grain resolution.
* ``data_sources`` — ``resolve_source_connection`` resolves the database the
  CTAS reads FROM, so a live re-point of ``project_connection_id`` sends the
  same SQL at different data. The whole row is compared, which also covers
  ``default_schema``; that field is compared for completeness rather than
  because it moves a number — no builder or serve path reads it (the CTAS
  qualifies from ``ModelTable.physical_name``), so over-comparing it costs a
  rebuild and can never produce a wrong one. Round-4 review corrected the
  earlier claim that ``default_schema`` re-points the read. This closure covers
  the DataSource ROW; the ``ProjectConnection`` ENDPOINT behind it (host / port
  / database, or BigQuery project / dataset, and the persisted
  ``source_db.fallback_*`` chain) is deliberately NOT compared here — a
  row-versus-row diff cannot see an endpoint that moved without the row
  changing. Both endpoint sides are covered by
  ``shared/artifact_target_binding.py``: the TARGET by
  ``built_for_storage_binding`` (Bug-8473) and the SOURCE by
  ``built_for_source_binding`` (Bug-8602).
* ``calendar_tables`` — ``ensure_target_calendar_table`` derives the TARGET
  table name from ``(calendar_type, fiscal_year_start_month)``, and the fiscal
  variants hold DIFFERENT values under the same column names, so a live
  fiscal-start edit silently re-points the CTAS at a different calendar. Only a
  registered (non-autocreated) calendar can be edited without a deploy, which is
  exactly the case that must be caught.

**The fact-anchor rule.** ``build_from_clause`` anchors on the first table whose
``table_type`` is ``fact``, so a live-only FACT table can move the anchor and
change every row the CTAS produces. That specific addition is drift. A live-only
non-fact table is not: it can only reach the FROM clause via a join, and joins
are Tier A. Treating every table addition as drift instead would refuse every
scheduled refresh on a model the moment a modeller adds a table to a draft.

Additions and removals are asymmetric on purpose: a removal is always drift on
both tiers, because the snapshot names something the build can no longer
resolve.

Fields excluded from comparison
-------------------------------
See ``NON_DEFINING_FIELDS``. Only timestamps, statistics telemetry, validation
status, and pure display text are excluded — nothing that can change a value.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from shared.semantic.graph_order import is_fact_table

#: Fields that cannot change the numbers a CTAS produces, and that move without
#: any modelling act — so comparing them would refuse refreshes for no
#: correctness gain. Everything NOT listed here is compared.
NON_DEFINING_FIELDS: frozenset[str] = frozenset({
    # Row lifecycle timestamps.
    "created_at",
    "updated_at",
    # Statistics/telemetry written by the stats collector and the drift sweep,
    # asynchronously and without a user edit.
    "last_stats_at",
    "cardinality_estimate",
    "row_count_estimate",
    # Written by the SCHEDULED schema-drift sweep (scheduler/jobs/schema_drift.py
    # sets it on removal and clears it on re-add), never by a modeller, and read
    # by no builder — grep across shared/semantic, optimizer/src and both refresh
    # writers returns no consumer. Comparing it meant an unattended nightly job
    # refused every refresh on the model until a human redeployed: an
    # availability and cost regression with no correctness gain, which is
    # exactly what this list exists to prevent. A column that actually
    # disappeared still shows up as a ``column_name``/``data_type`` change or as
    # a snapshot row missing live. Round-3 review, proven by execution.
    "drift_removed",
    # Validation status written by the model validator, not by the modeller.
    "is_invalid",
    "invalid_reason",
    "validated",
    "validation_error",
    # Pure display text. Never reaches generated SQL.
    "description",
    "effective_description",
    "display_name",
    "display_folder",
    "hidden_reason",
})

#: Snapshot keys and how each is compared. ``identity`` names the dict key that
#: identifies a row; ``bidirectional`` marks Tier A.
_TIER_A = True
_TIER_B = False


@dataclass(frozen=True)
class ClosureSpec:
    """Which measures and grain names an artifact build depends on.

    ``measure_names`` is the aggregate's own measures BEFORE calculated-measure
    expansion; the loader expands the transitive closure and passes the expanded
    set in ``closure_measure_names``.
    """

    closure_measure_names: frozenset[str]
    grain_names: frozenset[str]

    @staticmethod
    def of(
        closure_measure_names: Iterable[str],
        grain_names: Iterable[str],
    ) -> "ClosureSpec":
        return ClosureSpec(
            closure_measure_names=frozenset(n for n in closure_measure_names if n),
            grain_names=frozenset(n for n in grain_names if n),
        )


@dataclass(frozen=True)
class DefinitionClosure:
    """One side (live or deployed) of the comparison, already normalised.

    Every value is a list of plain JSON-ready dicts produced by
    ``row_to_snapshot_dict`` with ``NON_DEFINING_FIELDS`` removed.
    """

    measures: list[dict[str, Any]] = field(default_factory=list)
    dimensions: list[dict[str, Any]] = field(default_factory=list)
    tables: list[dict[str, Any]] = field(default_factory=list)
    joins: list[dict[str, Any]] = field(default_factory=list)
    columns: list[dict[str, Any]] = field(default_factory=list)
    user_defined_attributes: list[dict[str, Any]] = field(default_factory=list)
    uda_column_refs: list[dict[str, Any]] = field(default_factory=list)
    hierarchies: list[dict[str, Any]] = field(default_factory=list)
    calendar_tables: list[dict[str, Any]] = field(default_factory=list)
    attribute_relationships: list[dict[str, Any]] = field(default_factory=list)
    data_sources: list[dict[str, Any]] = field(default_factory=list)


def strip_non_defining(row: Mapping[str, Any]) -> dict[str, Any]:
    """Drop the excluded fields from an already-normalised row dict."""
    return {k: v for k, v in row.items() if k not in NON_DEFINING_FIELDS}


def _strip_nested_hierarchy(row: Mapping[str, Any]) -> dict[str, Any]:
    """Strip a hierarchy row and its nested levels/level-attributes."""
    out = strip_non_defining(row)
    levels = []
    for lvl in row.get("levels") or []:
        if not isinstance(lvl, Mapping):
            continue
        lvl_out = strip_non_defining(lvl)
        lvl_out["attributes"] = sorted(
            (
                strip_non_defining(a)
                for a in (lvl.get("attributes") or [])
                if isinstance(a, Mapping)
            ),
            key=lambda a: str(a.get("id") or ""),
        )
        levels.append(lvl_out)
    out["levels"] = sorted(levels, key=lambda l: str(l.get("id") or ""))
    return out


def _referenced_calendar_ids(table_rows: Iterable[Mapping[str, Any]]) -> set[str]:
    """The calendar ids the model's tables actually point at."""
    return {
        str(t.get("calendar_table_id"))
        for t in table_rows
        if t.get("calendar_table_id")
    }


def closure_from_snapshot(
    snapshot: Mapping[str, Any],
    spec: ClosureSpec,
) -> DefinitionClosure:
    """Project a deployed ``snapshot_json`` into the comparison structure."""

    def rows(key: str) -> list[Mapping[str, Any]]:
        value = snapshot.get(key)
        return [r for r in value if isinstance(r, Mapping)] if isinstance(value, list) else []

    return DefinitionClosure(
        measures=[
            strip_non_defining(r)
            for r in rows("measures")
            if r.get("name") in spec.closure_measure_names
        ],
        dimensions=[
            strip_non_defining(r)
            for r in rows("dimensions")
            if r.get("name") in spec.grain_names
        ],
        tables=[strip_non_defining(r) for r in rows("tables")],
        joins=[strip_non_defining(r) for r in rows("joins")],
        columns=[strip_non_defining(r) for r in rows("columns")],
        user_defined_attributes=[
            strip_non_defining(r) for r in rows("user_defined_attributes")
        ],
        uda_column_refs=[strip_non_defining(r) for r in rows("uda_column_refs")],
        hierarchies=[_strip_nested_hierarchy(r) for r in rows("hierarchies")],
        # Scoped to the calendars the snapshot's OWN tables point at, mirroring
        # how the live side is scoped. The snapshot carries every calendar of
        # every data source the model touches, most of which no ModelTable
        # references and no CTAS can reach; comparing those would report a
        # difference the build cannot possibly be affected by. A table changing
        # WHICH calendar it points at is caught by the ``tables`` comparison,
        # since ``calendar_table_id`` is one of its fields.
        calendar_tables=[
            strip_non_defining(r)
            for r in rows("calendar_tables")
            if str(r.get("id") or "") in _referenced_calendar_ids(rows("tables"))
        ],
        attribute_relationships=[
            strip_non_defining(r) for r in rows("attribute_relationships")
        ],
        data_sources=[strip_non_defining(r) for r in rows("data_sources")],
    )


def _index(rows: Sequence[Mapping[str, Any]], key: str) -> tuple[dict[str, Any], list[str]]:
    """Index rows by ``key``; report any duplicate key as its own drift signal.

    A duplicate is load-bearing, not cosmetic: two live dimensions sharing a
    grain name means the layout resolver's choice is ambiguous, so the build's
    output is not determined by the snapshot alone.
    """
    out: dict[str, Any] = {}
    duplicates: list[str] = []
    for row in rows:
        k = row.get(key)
        k = str(k) if k is not None else ""
        if k in out:
            duplicates.append(k)
            continue
        out[k] = row
    return out, duplicates


def _compare_group(
    label: str,
    live_rows: Sequence[Mapping[str, Any]],
    deployed_rows: Sequence[Mapping[str, Any]],
    *,
    key: str,
    bidirectional: bool,
) -> list[str]:
    """Diff one group. Returns human-readable drift reasons; empty = no drift."""
    reasons: list[str] = []
    live, live_dupes = _index(live_rows, key)
    deployed, deployed_dupes = _index(deployed_rows, key)
    for dupe in sorted(set(live_dupes)):
        reasons.append(f"{label} {dupe!r}: duplicated in the live model (ambiguous)")
    for dupe in sorted(set(deployed_dupes)):
        reasons.append(f"{label} {dupe!r}: duplicated in the deployed snapshot")

    for k in sorted(deployed):
        d_row = deployed[k]
        l_row = live.get(k)
        if l_row is None:
            reasons.append(
                f"{label} {k!r}: present in the deployed snapshot but missing live"
            )
            continue
        # Compare only the fields the SNAPSHOT carries. A field added to the ORM
        # after this snapshot was written is absent here and is not treated as
        # drift: the router rehydrates that snapshot, so it never sees the new
        # field either. Without this, every model deployed before a schema
        # addition would have all of its refreshes refused forever.
        for fname in sorted(d_row):
            if fname not in l_row:
                reasons.append(
                    f"{label} {k!r}: field {fname!r} present in the deployed "
                    f"snapshot but absent live"
                )
                continue
            if l_row[fname] != d_row[fname]:
                reasons.append(
                    f"{label} {k!r}: {fname} changed "
                    f"(live={l_row[fname]!r} vs deployed={d_row[fname]!r})"
                )

    if bidirectional:
        for k in sorted(set(live) - set(deployed)):
            reasons.append(
                f"{label} {k!r}: present live but not in the deployed snapshot"
            )
    return reasons


def _materialized_attribute_relationships(
    rows: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """Relationships the passenger planner can materialise into an artifact."""
    return [
        row
        for row in rows
        if row.get("enabled") is True and row.get("cardinality") == "BIJECTION"
    ]


def _fact_anchor_additions(
    live_tables: Sequence[Mapping[str, Any]],
    deployed_tables: Sequence[Mapping[str, Any]],
) -> list[str]:
    """A live-only table that can become ``build_from_clause``'s anchor is drift.

    Tables are otherwise Tier B, because a table normally reaches the FROM
    clause only through a join (Tier A, bidirectional) — and refusing every
    refresh the moment a modeller adds a table to a draft is an availability
    cost with no correctness benefit. The anchor is the exception.

    Which additions can move the anchor depends on whether the DEPLOYED model
    has a fact table at all. Since Bug-8605 the anchor is
    ``shared.semantic.graph_order.pick_anchor_table``: the fact table if the
    model has one, otherwise the first table in canonical ``id`` order.

    * **With a fact table** — only another FACT table can take the anchor, and
      the storage layer's partial unique index caps that at one.
    * **Without one** — ANY live-only table can take it. This rule is still
      required after Bug-8605, and deliberately so: canonical order is ``id``,
      which for a random UUID means a table added to a draft can sort AHEAD of
      the existing ones and become the anchor. (``id`` is used rather than
      creation order because it is the only key the deployed snapshot carries
      and ``rehydrate_into_live`` preserves — see the graph_order docstring.)
      Round-2 review proved the exposure by execution: adding an UNJOINED
      ``staging_customer`` to a zero-fact model moved the FROM clause from
      ``dim_customer AS base JOIN dim_region`` to ``staging_customer AS base``
      with the join gone entirely, and the comparison reported no drift.
      A staging copy carrying the same column names then makes the CTAS
      aggregate the wrong table and serve it as the deployed version's numbers.

    Nothing requires a model to have a fact table: ``table_type`` also takes
    ``dim_aggregate`` and ``dim_detail``, and the one-fact-per-model index
    (F-013-11) caps the count at one — it does not impose a minimum.
    """
    deployed_ids = {str(t.get("id") or "") for t in deployed_tables}
    # Bug-8605 R2 review (finding 1): the SAME fact test the anchor rule uses.
    # These two drifted apart within one commit -- the anchor rule was made
    # case-sensitive to match the read path while this guard still lowercased,
    # so a table stored as "Fact" was a fact here and NOT a fact to the anchor,
    # and the guard skipped a live-only addition that provably took the anchor.
    deployed_has_fact = any(is_fact_table(t) for t in deployed_tables)
    reasons: list[str] = []
    for t in live_tables:
        tid = str(t.get("id") or "")
        if tid in deployed_ids:
            continue
        is_fact = is_fact_table(t)
        if deployed_has_fact and not is_fact:
            continue
        why = (
            "a FACT table present live but not in the deployed snapshot"
            if deployed_has_fact
            else (
                "the deployed model has NO fact table, so the FROM-clause "
                "anchor is whichever table the query returns first and ANY "
                "live-only table can take it"
            )
        )
        reasons.append(
            f"table {tid!r}: {why} can move the FROM-clause anchor"
        )
    return reasons


def compare_closures(
    live: DefinitionClosure,
    deployed: DefinitionClosure,
) -> list[str]:
    """Return every way the live closure differs from the deployed one.

    Empty list means the build inputs are provably identical to what the router
    binds. A non-empty list must be treated as a refusal, never a warning.
    """
    reasons: list[str] = []
    # Tier A — bidirectional.
    reasons += _compare_group(
        "measure", live.measures, deployed.measures, key="name", bidirectional=_TIER_A
    )
    reasons += _compare_group(
        "dimension", live.dimensions, deployed.dimensions, key="name",
        bidirectional=_TIER_A,
    )
    reasons += _compare_group(
        "table", live.tables, deployed.tables, key="id", bidirectional=_TIER_B
    )
    reasons += _fact_anchor_additions(live.tables, deployed.tables)
    reasons += _compare_group(
        "join", live.joins, deployed.joins, key="id", bidirectional=_TIER_A
    )
    # Tier B — snapshot-authoritative.
    reasons += _compare_group(
        "column", live.columns, deployed.columns, key="id", bidirectional=_TIER_B
    )
    reasons += _compare_group(
        "user-defined attribute", live.user_defined_attributes,
        deployed.user_defined_attributes, key="id", bidirectional=_TIER_B,
    )
    reasons += _compare_group(
        "user-defined attribute column ref", live.uda_column_refs,
        deployed.uda_column_refs, key="id", bidirectional=_TIER_B,
    )
    reasons += _compare_group(
        "hierarchy", live.hierarchies, deployed.hierarchies, key="id",
        bidirectional=_TIER_B,
    )
    reasons += _compare_group(
        "calendar table", live.calendar_tables, deployed.calendar_tables,
        key="id", bidirectional=_TIER_B,
    )
    reasons += _compare_group(
        "attribute relationship",
        _materialized_attribute_relationships(live.attribute_relationships),
        _materialized_attribute_relationships(deployed.attribute_relationships),
        key="id", bidirectional=_TIER_A,
    )
    reasons += _compare_group(
        "data source", live.data_sources, deployed.data_sources, key="id",
        bidirectional=_TIER_B,
    )
    return reasons


def closure_digest(closure: DefinitionClosure) -> str:
    """A stable digest of a closure, for detecting a mid-build definition move.

    Deterministic across processes: every group is sorted by its identity key
    and serialised with sorted field names. Two closures with the same digest
    describe the same build inputs.
    """
    payload = {
        "measures": sorted(closure.measures, key=lambda r: str(r.get("name") or "")),
        "dimensions": sorted(closure.dimensions, key=lambda r: str(r.get("name") or "")),
        "tables": sorted(closure.tables, key=lambda r: str(r.get("id") or "")),
        "joins": sorted(closure.joins, key=lambda r: str(r.get("id") or "")),
        "columns": sorted(closure.columns, key=lambda r: str(r.get("id") or "")),
        "user_defined_attributes": sorted(
            closure.user_defined_attributes, key=lambda r: str(r.get("id") or "")
        ),
        "uda_column_refs": sorted(
            closure.uda_column_refs, key=lambda r: str(r.get("id") or "")
        ),
        "hierarchies": sorted(closure.hierarchies, key=lambda r: str(r.get("id") or "")),
        "calendar_tables": sorted(
            closure.calendar_tables, key=lambda r: str(r.get("id") or "")
        ),
        "attribute_relationships": sorted(
            _materialized_attribute_relationships(closure.attribute_relationships),
            key=lambda r: str(r.get("id") or ""),
        ),
        "data_sources": sorted(
            closure.data_sources, key=lambda r: str(r.get("id") or "")
        ),
    }
    canonical = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
