"""Deploy-time classifier for a model's row-filtering / row-multiplying joins.

Contract: ``docs/architecture/architecture_join-population-governance.md``
(invariants 1-6). Companion to
``docs/architecture/architecture_join-orientation-and-cardinality.md``, which
owns the ORIENTATION half of join correctness; this module owns the POPULATION
half. The two are independent properties of the same ``Join``.

What this module is for
-----------------------
Projection-driven elision compiles only the relations a query's columns touch.
That is correct exactly when a join neither drops nor duplicates rows. Nothing
in the platform knew whether a given join does, so two logically equivalent
queries over one model could disagree on their own grand total (Bug-8615). This
module answers, per join, once per deploy:

* ``neutral``     — provably no row loss and no row gain
* ``filtering``   — can drop rows
* ``multiplying`` — can ADD rows, either by fanning a retained row out across
  several partners or, when the keyword preserves the FAR side (``RIGHT``
  entered from its non-preserved end, ``FULL`` from either end), by emitting
  far-side rows that have no partner at all. Both are row gain relative to the
  plan that elides the far table, so both belong to the same classification.

and rolls the joins up, against each join's modeller-declared
``population_participation``, into a per-model ``OK`` / ``WARNING`` /
``BLOCKED`` status.

``classification`` above is a single worst-of label — an edge that both
filters and multiplies is labelled by whichever ratio is larger. The rollup
step (:func:`join_status`) cannot use that label directly, because
``population_participation`` excuses filtering and multiplying differently
(``enrichment_only`` excuses multiplying only, never filtering — Bug-8652,
closed 2026-08-04). :func:`resolve_edge_effect_components` recovers the two
facts independently from the same measured ratios before the flag is applied.

Five properties this module holds to
------------------------------------
1. **Deploy time only, never the query path** (invariant 5). Every source
   touch happens in :func:`validate_model_joins_on_deploy`, called once from
   the model-service deploy endpoint. Nothing here is reachable from a query.
2. **Deploy-time policy input** (invariant 4 / plan phase G5). ``BLOCKED`` is
   computed and surfaced here; the model-service deploy handler refuses only
   the measured, policy-relevant rows returned by this module. Measurement
   failures remain unmeasured and therefore never become a deploy block.
3. **Classifies, does not gate.** Nothing here reads or changes the elision
   decision. Wiring the flag into serving is plan phase G3.
4. **Conservative on a metadata gap** (invariant 1: "a join cannot be honestly
   classified against a metadata gap"). Measured zero loss and zero
   multiplication is NOT enough for ``neutral`` — the uniqueness the plan
   depends on must be CONSTRAINT-BACKED (``ModelColumn.is_primary_key``,
   populated from the source by Bug-8618). Unique-in-today's-data but not
   declared unique classifies non-neutral with a 0.0 measured effect, which
   rolls up to ``WARNING`` and never to ``BLOCKED`` — a nudge to close the
   metadata gap, not a block.
5. **Absence of measurement is reported, never manufactured into a verdict.**
   An unreachable source, a timeout, or an unresolvable column yields
   ``measured=False`` with NULL ratios and a ``WARNING``. It is NOT promoted to
   ``BLOCKED``: a temporarily unreachable source must not become a deploy block
   under the active G5 policy. The rollup exposes ``evaluated`` for honest
   health reporting; deploy enforcement remains per-row so an unrelated
   unmeasured row never hides or creates a measured block.

Orientation of the measurement (near side / far side)
-----------------------------------------------------
Elision removes the leaf-ward table from a plan rooted at the model's fact
table, so the population question for edge ``A—B`` is "what happens to the side
CLOSER to the fact when the FARTHER side is joined in". The near side is
resolved by breadth-first distance from the model's fact table over the join
graph. When that cannot be resolved (no fact table, an endpoint not connected
to it, or a tie), BOTH directions are measured and the worse one is taken —
conservative, and recorded as ``orientation_ambiguous``.

Composition caveat, stated rather than hidden: this is a PER-EDGE property.
A neutral edge composes safely (zero loss and zero fan-out propagate), but a
chain of non-neutral edges can compound beyond any single edge's ratio. Each
non-neutral edge is individually surfaced, so nothing is silently lost; the
rollup deliberately does not multiply ratios along a path, because the
compiler's actual FROM-clause order is a per-query property (see
``docs/questions/questions_pocket-join-population.md`` item 1y).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Iterable, Optional, Sequence
from uuid import UUID

import sqlglot

from shared.config.registry import (
    JOIN_POPULATION_PROBE_BUDGET_SECONDS_DEFAULT,
    JOIN_POPULATION_ROW_EFFECT_THRESHOLD_DEFAULT,
)
from shared.connector_qualify import quote_identifier, quote_table_ref
from shared.schemas.domains.aggregates_security import (
    DEFAULT_POPULATION_PARTICIPATION,
    POPULATION_PARTICIPATION_ENRICHMENT_ONLY,
    POPULATION_PARTICIPATION_POPULATION_DEFINING,
    POPULATION_PARTICIPATION_PRESERVE_BASE_ROWS,
    POPULATION_PARTICIPATION_UNDECLARED,
    coerce_population_participation,
)
from shared.semantic.graph_order import is_fact_table
from shared.semantic.join_keyword import (
    FULL_TOKENS,
    LEFT_TOKENS,
    RIGHT_TOKENS,
    normalise_token,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

#: A join with neither measurable row loss nor measurable row multiplication,
#: whose no-multiplication guarantee is backed by declared key metadata.
CLASSIFICATION_NEUTRAL = "neutral"
#: A join that can drop rows from the population it is joined into.
CLASSIFICATION_FILTERING = "filtering"
#: A join that can duplicate rows in the population it is joined into.
CLASSIFICATION_MULTIPLYING = "multiplying"

CLASSIFICATIONS: tuple[str, ...] = (
    CLASSIFICATION_NEUTRAL, CLASSIFICATION_FILTERING, CLASSIFICATION_MULTIPLYING,
)

STATUS_OK = "OK"
STATUS_WARNING = "WARNING"
STATUS_BLOCKED = "BLOCKED"

#: Ordered worst-last, so a model rollup is ``max`` over its joins.
STATUS_SEVERITY: dict[str, int] = {STATUS_OK: 0, STATUS_WARNING: 1, STATUS_BLOCKED: 2}

# --- Reason codes (stable, machine-readable; never user-facing prose) -------
REASON_MEASURED = "measured"
#: Zero measured effect, but the uniqueness it relies on is not declared on any
#: key column — Bug-8618's introspection gap. Non-neutral by invariant 1.
REASON_UNIQUENESS_NOT_DECLARED = "uniqueness_not_declared"
#: Near/far could not be resolved from the fact table; both directions measured.
REASON_ORIENTATION_AMBIGUOUS = "orientation_ambiguous"
#: The join's endpoints could not be resolved to physical tables/columns.
REASON_UNRESOLVED_ENDPOINT = "unresolved_endpoint"
#: The source probe exceeded the source statement timeout.
REASON_MEASUREMENT_TIMEOUT = "measurement_timeout"
#: The source probe failed (connection, permission, dialect).
REASON_MEASUREMENT_FAILED = "measurement_failed"
#: Validation is switched off for this model, or no source connection resolved.
REASON_NOT_MEASURED = "not_measured"
#: The model's whole-validation wall-clock budget ran out before this join was
#: reached. Deploy is a synchronous request: a wide model whose every probe sits
#: at the source statement timeout would otherwise hold it open for
#: joins x 3 x timeout. The remaining joins record an unmeasured verdict, which
#: keeps the rollup honest (``evaluated`` goes false) without stalling anyone.
REASON_MEASUREMENT_BUDGET_EXHAUSTED = "measurement_budget_exhausted"

#: Copy of the registry default so callers can read the threshold without
#: importing the config registry. The registry is the single source of truth.
DEFAULT_ROW_EFFECT_WARNING_THRESHOLD: float = (
    JOIN_POPULATION_ROW_EFFECT_THRESHOLD_DEFAULT
)

#: Copy of the registry default, for the same reason as the threshold above.
DEFAULT_PROBE_BUDGET_SECONDS: float = JOIN_POPULATION_PROBE_BUDGET_SECONDS_DEFAULT

#: Registry keys this module's caller resolves.
SETTING_VALIDATION_MODE = "model.join_population_validation"
SETTING_ROW_EFFECT_THRESHOLD = "model.join_population_row_effect_threshold"
SETTING_PROBE_BUDGET_SECONDS = "model.join_population_probe_budget_seconds"

#: Generated SQL aliases. Constant, lowercase ASCII, not reserved in any
#: supported dialect; still quoted through ``connector_qualify`` so the module
#: has no hand-rolled identifier quoting at all (SQL-generation rule 2).
_ALIAS_RETAINED = "jp_a"
_ALIAS_JOINED = "jp_b"
_ALIAS_KEY = "jp_k"

#: Result-column names the probe reads back. Bug-8670: these were the ONE set
#: of identifiers emitted unquoted, and Snowflake normalises an unquoted
#: identifier to UPPERCASE. ``source_executor`` builds row dicts straight from
#: the driver's ``cursor.description``, so ``row["n_rows"]`` missed, every count
#: read 0, and a 40%-row-loss join was recorded as a MEASURED ``neutral`` — the
#: one verdict this module must never invent. Fixed at both ends: the aliases
#: are quoted like every other identifier, AND the reader folds keys
#: case-insensitively so a driver that normalises anyway cannot resurrect it.
COL_ROWS = "n_rows"
COL_KEY_NON_NULL = "n_key_non_null"
COL_KEY_DISTINCT = "n_key_distinct"
COL_MATCHED = "n_matched"
COL_JOIN_ROWS = "n_join_rows"


def _result_aliases(connector: str) -> dict[str, str]:
    """Connector-quoted spellings of the result-column names."""
    return {
        name: quote_identifier(connector, name)
        for name in (
            COL_ROWS, COL_KEY_NON_NULL, COL_KEY_DISTINCT, COL_MATCHED,
            COL_JOIN_ROWS,
        )
    }


# ---------------------------------------------------------------------------
# Measurement + verdict shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SideProbe:
    """One side of an edge, counted against the other side.

    ``matched`` counts ROWS of this side that have at least one partner on the
    other side (the probe joins against a DISTINCT key set, so a row can
    contribute at most once regardless of far-side duplicates).
    """

    rows: int
    key_non_null: int
    key_distinct: int
    matched: int

    @property
    def key_unique_in_data(self) -> bool:
        """True when this side's join key has no duplicate non-NULL value.

        Data-observed only. It is NOT sufficient for ``neutral`` on its own —
        see invariant 1 and :func:`classify_edge`'s ``*_key_declared_unique``.
        """
        return self.key_non_null == self.key_distinct

    @property
    def unmatched_fraction(self) -> float:
        if self.rows <= 0:
            return 0.0
        return max(0.0, (self.rows - self.matched) / self.rows)


@dataclass(frozen=True)
class EdgeMeasurement:
    """Counted statistics for one edge.

    ``inner_join_rows`` is ``None`` when the probe provably did not need it
    (both keys unique in the data => an inner join emits exactly the matched
    rows, so FAN-OUT is exactly zero). It says nothing about the other source
    of extra rows — far-side rows with no partner under a preserving keyword —
    which is derived from the side probes and needs no third query.
    """

    left: SideProbe
    right: SideProbe
    inner_join_rows: Optional[int] = None


@dataclass(frozen=True)
class JoinPopulationVerdict:
    """The classification + status for ONE join."""

    classification: str
    status: str
    measured: bool
    row_loss_ratio: Optional[float]
    row_mult_ratio: Optional[float]
    row_effect_ratio: Optional[float]
    reason: str
    population_participation: str


@dataclass(frozen=True)
class ModelPopulationRollup:
    """The per-model status (invariant 6).

    ``evaluated`` is False when at least one of the model's joins has no
    measured verdict. Block mode is decided per row, not by this aggregate:
    a measured blocker still refuses a mixed model containing another
    unmeasured join, while the unmeasured row itself never blocks.
    """

    status: str
    evaluated: bool
    join_count: int
    evaluated_count: int
    blocked_count: int
    warning_count: int


def _snapshot_id(value: Any) -> Any:
    """Return snapshot identifiers in the same type used by the ORM graph.

    Snapshots are JSON, so UUID primary/foreign keys arrive as strings.  The
    deployed graph must not be rebuilt from live rows merely to coerce those
    values; normalising them here keeps the existing graph/fingerprint code
    usable for both ORM and immutable-snapshot inputs.
    """
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return value


def _snapshot_graph(snapshot: dict[str, Any]) -> tuple[list[Any], dict[Any, Any], dict[Any, Any]]:
    """Adapt one immutable version snapshot into the classifier's graph shape.

    The snapshot is the sole definition authority when supplied by deploy.  A
    missing ``joins`` key means this version has no join graph (including
    pre-G1 snapshots); it does *not* authorize a fallback to today's mutable
    draft.  Missing participation keeps the rehydration/serving compatibility
    default, while missing endpoint metadata remains an honest unmeasured
    result in ``_verdict_for_join``.
    """
    table_rows = snapshot.get("tables") or []
    column_rows = snapshot.get("columns") or []
    join_rows = snapshot.get("joins") or []
    tables: dict[Any, Any] = {}
    columns: dict[Any, Any] = {}

    for raw in table_rows:
        if not isinstance(raw, dict):
            continue
        table_id = _snapshot_id(raw.get("id"))
        if table_id is None:
            continue
        tables[table_id] = SimpleNamespace(
            id=table_id,
            physical_name=raw.get("physical_name"),
            table_type=raw.get("table_type"),
            alias=raw.get("alias"),
            display_name=raw.get("display_name"),
        )

    for raw in column_rows:
        if not isinstance(raw, dict):
            continue
        column_id = _snapshot_id(raw.get("id"))
        if column_id is None:
            continue
        columns[column_id] = SimpleNamespace(
            id=column_id,
            model_table_id=_snapshot_id(raw.get("model_table_id")),
            column_name=raw.get("column_name"),
            display_name=raw.get("display_name"),
            is_primary_key=bool(raw.get("is_primary_key", False)),
        )

    joins: list[Any] = []
    for raw in join_rows:
        if not isinstance(raw, dict):
            continue
        join_id = _snapshot_id(raw.get("id"))
        if join_id is None:
            continue
        # Snapshot schema evolution is deliberately different from token
        # validation.  Versions written before G1 do not contain this key and
        # must preserve the pre-G1 behaviour; a key that is present but invalid
        # is untrusted and must remain ``undeclared``.
        participation = (
            DEFAULT_POPULATION_PARTICIPATION
            if "population_participation" not in raw
            else coerce_population_participation(raw.get("population_participation"))
        )
        joins.append(SimpleNamespace(
            id=join_id,
            left_table_id=_snapshot_id(raw.get("left_table_id")),
            right_table_id=_snapshot_id(raw.get("right_table_id")),
            left_column_id=_snapshot_id(raw.get("left_column_id")),
            right_column_id=_snapshot_id(raw.get("right_column_id")),
            join_type=raw.get("join_type"),
            population_participation=participation,
        ))
    return joins, tables, columns


def _join_labels(
    joins: Iterable[Any], tables: dict[Any, Any], columns: dict[Any, Any],
    *, prefer_alias: bool = False,
) -> dict[str, dict[str, Any]]:
    """Return stable endpoint labels for one selected graph.

    Labels are definition data.  Keeping this helper independent of the ORM
    lets deploy evidence and health use the exact same selected snapshot graph
    even after the mutable draft deletes or renames its join.
    """
    def table_label(table: Any) -> str | None:
        if table is None:
            return None
        names = (
            ("alias", "display_name", "physical_name")
            if prefer_alias
            else ("display_name", "alias", "physical_name")
        )
        return next((getattr(table, name, None) for name in names if getattr(table, name, None)), None)

    def column_label(column: Any) -> str | None:
        if column is None:
            return None
        return getattr(column, "display_name", None) or getattr(
            column, "column_name", None
        )

    labels: dict[str, dict[str, Any]] = {}
    for join in joins:
        left_table_name = table_label(tables.get(join.left_table_id))
        right_table_name = table_label(tables.get(join.right_table_id))
        left_column_name = column_label(columns.get(join.left_column_id))
        right_column_name = column_label(columns.get(join.right_column_id))
        left_endpoint = ".".join(
            part for part in (left_table_name, left_column_name) if part
        )
        right_endpoint = ".".join(
            part for part in (right_table_name, right_column_name) if part
        )
        labels[str(join.id)] = {
            "join_id": str(join.id),
            "join_label": f"{left_endpoint} ↔ {right_endpoint}".strip(" ↔"),
            "left_table_name": left_table_name,
            "right_table_name": right_table_name,
            "left_column_name": left_column_name,
            "right_column_name": right_column_name,
        }
    return labels


def snapshot_join_labels(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return stable endpoint labels from a selected snapshot's join graph."""
    joins, tables, columns = _snapshot_graph(snapshot)
    return _join_labels(joins, tables, columns)


# ---------------------------------------------------------------------------
# Pure classification
# ---------------------------------------------------------------------------

_SIDE_LEFT = "left"
_SIDE_RIGHT = "right"


def preserved_sides(join_type: str | None) -> frozenset[str]:
    """Which declared side(s) a join provably keeps every row of.

    Mirrors ``shared.semantic.join_keyword`` exactly:

    * ``inner`` preserves neither side.
    * ``left`` preserves the modeller's LEFT table; ``right`` the RIGHT.
    * ``full`` preserves both.
    * an unrecognised / legacy token preserves NOTHING *provably*: it renders
      as an un-flipped ``LEFT JOIN`` onto whichever relation the traversal
      accumulated first, which is a property of the plan's base table and not
      of the join (join-orientation contract invariant 4). Treating it as
      "preserves nothing" is the conservative reading, and when the data shows
      total coverage on both sides the edge still measures zero loss — so a
      legacy token is not penalised on a genuinely clean model.
    """
    token = normalise_token(join_type)
    if token in FULL_TOKENS:
        return frozenset({_SIDE_LEFT, _SIDE_RIGHT})
    if token in LEFT_TOKENS:
        return frozenset({_SIDE_LEFT})
    if token in RIGHT_TOKENS:
        return frozenset({_SIDE_RIGHT})
    # inner, and every unrecognised/legacy token.
    return frozenset()


def _added_rows(
    *,
    joined_rows: Optional[int],
    retained: SideProbe,
    joined_side: SideProbe,
    far_is_preserved: bool,
) -> float:
    """Rows the edge ADDS to the retained population, as a fraction of it.

    Two independent sources of extra rows, and both must be counted or a real
    population change reads as ``neutral``:

    1. **Fan-out.** A retained row with N partners appears N times, so the
       inner-join output exceeds the number of retained rows that matched:
       ``joined_rows - retained.matched`` extra rows. ``joined_rows is None``
       means the probe PROVED no fan-out was possible (both keys unique in the
       data), so this term is exactly zero without a query.

    2. **Far-side rows with no partner**, when the keyword preserves the FAR
       side — a ``RIGHT`` join entered from its non-preserved end, or a ``FULL``
       join from either end. Those rows are emitted with NULLs on the retained
       side, so the compiled plan returns MORE rows than the plan that elides
       the far table, and a grand total moves. Missing this made a ``FULL`` join
       over partially-overlapping keys classify ``neutral`` (Bug-8658).

    Expressed as a fraction of the retained population (``retained.rows``) so it
    is directly comparable with ``row_loss_ratio`` and with the row-effect
    threshold, both of which are fractions of that same baseline.
    """
    fan_out = 0.0
    if joined_rows is not None:
        fan_out = max(0.0, float(joined_rows - retained.matched))
    far_only = (
        float(max(0, joined_side.rows - joined_side.matched))
        if far_is_preserved else 0.0
    )
    extra = fan_out + far_only
    if extra <= 0.0:
        return 0.0
    if retained.rows <= 0:
        # Bug-8663. The retained table is EMPTY and the far side still emits
        # unpartnered rows, so the compiled plan returns rows where the plan
        # that elides the far table returns none. That is the largest row gain
        # there is, but the ratio is 0/0. Returning 0.0 read as ``neutral``.
        # Report a full 1.0 — "everything in the output is added" — which is
        # finite and JSON-safe, unlike an infinity that would classify
        # correctly and then break the health payload.
        return 1.0
    return extra / retained.rows


def classify_edge(
    *,
    join_type: str | None,
    measurement: Optional[EdgeMeasurement],
    near_side: Optional[str],
    left_key_declared_unique: bool,
    right_key_declared_unique: bool,
    unmeasured_reason: str = REASON_NOT_MEASURED,
) -> tuple[str, Optional[float], Optional[float], str]:
    """Classify one edge. PURE — no I/O, no DB, no source access.

    Returns ``(classification, row_loss_ratio, row_mult_ratio, reason)``.

    ``near_side`` is ``"left"``/``"right"`` when the fact-rooted graph walk
    resolved which endpoint is retained and which is joined in; ``None`` means
    ambiguous, and both directions are measured with the worse one taken.

    ``*_key_declared_unique`` is CONSTRAINT-BACKED uniqueness
    (``ModelColumn.is_primary_key``), not data-observed uniqueness. Invariant 1
    forbids calling an edge ``neutral`` on the strength of today's data alone.
    """
    if measurement is None:
        # Invariant/brief rule: absent metadata or measurement classifies
        # conservatively as NON-neutral. ``filtering`` is the honest label —
        # "this join may drop rows and we could not prove otherwise".
        return CLASSIFICATION_FILTERING, None, None, unmeasured_reason

    keeps = preserved_sides(join_type)
    if near_side in (_SIDE_LEFT, _SIDE_RIGHT):
        directions: tuple[str, ...] = (near_side,)
        ambiguous = False
    else:
        directions = (_SIDE_LEFT, _SIDE_RIGHT)
        ambiguous = True

    loss = 0.0
    mult = 0.0
    uniqueness_backed = True
    for retained in directions:
        far = _SIDE_RIGHT if retained == _SIDE_LEFT else _SIDE_LEFT
        if retained == _SIDE_LEFT:
            retained_probe, joined_probe, far_declared_unique = (
                measurement.left, measurement.right, right_key_declared_unique,
            )
        else:
            retained_probe, joined_probe, far_declared_unique = (
                measurement.right, measurement.left, left_key_declared_unique,
            )
        loss = max(
            loss,
            0.0 if retained in keeps else retained_probe.unmatched_fraction,
        )
        mult = max(mult, _added_rows(
            joined_rows=measurement.inner_join_rows,
            retained=retained_probe,
            joined_side=joined_probe,
            far_is_preserved=(far in keeps),
        ))
        # The retained side can only be multiplied by duplicates on the side
        # being joined IN, so it is the FAR key whose uniqueness must be
        # constraint-backed for a zero-multiplication guarantee to hold beyond
        # today's rows.
        uniqueness_backed = uniqueness_backed and far_declared_unique

    if mult > 0.0 and mult >= loss:
        classification = CLASSIFICATION_MULTIPLYING
    elif loss > 0.0:
        classification = CLASSIFICATION_FILTERING
    elif uniqueness_backed:
        classification = CLASSIFICATION_NEUTRAL
    else:
        # Zero measured effect, but nothing constrains it to stay zero.
        # Invariant 1: not honestly neutral. Its 0.0 effect keeps it at
        # WARNING, never BLOCKED.
        return (
            CLASSIFICATION_MULTIPLYING, loss, mult, REASON_UNIQUENESS_NOT_DECLARED,
        )

    reason = REASON_ORIENTATION_AMBIGUOUS if ambiguous else REASON_MEASURED
    return classification, loss, mult, reason


def row_effect_ratio(
    row_loss: Optional[float], row_mult: Optional[float],
) -> Optional[float]:
    """The single number the WARNING/BLOCKED threshold compares against.

    ``None`` when nothing was measured — deliberately not 0.0, so "no effect"
    and "no measurement" can never be confused by a consumer.
    """
    present = [v for v in (row_loss, row_mult) if v is not None]
    return max(present) if present else None


def _max_present(values: Iterable[Optional[float]]) -> Optional[float]:
    """Same rule as :func:`row_effect_ratio`, generalised to N ratios."""
    present = [v for v in values if v is not None]
    return max(present) if present else None


def resolve_edge_effect_components(
    *,
    row_loss_ratio: Optional[float],
    row_mult_ratio: Optional[float],
    reason: str,
) -> tuple[bool, bool]:
    """Split one edge's non-neutral effect into its two INDEPENDENT facts.

    Bug-8652 / the governance contract's "``enrichment_only`` excuses
    multiplication only" decision (2026-08-04): a join can in principle both
    FILTER and MULTIPLY at once, but :func:`classify_edge` reports a single
    worst-of ``classification`` (picking ``multiplying`` whenever
    ``mult >= loss``). Deciding which ``population_participation`` flag
    excuses a join therefore cannot be done from ``classification`` alone —
    it needs both facts independently. Returns
    ``(is_filtering, is_multiplying)``.

    Two cases cannot be read straight off the numeric ratios:

    * **Unmeasured** (``row_loss_ratio`` and ``row_mult_ratio`` both
      ``None``). Which effect, if either, the edge actually has is unknown.
      This mirrors :func:`classify_edge`'s own conservative choice for the
      same case — "may drop rows and we could not prove otherwise" — so an
      unmeasured edge is always treated as (possibly) filtering and never
      excused by ``enrichment_only``, which covers multiplication only.
    * **Metadata gap** (``reason == REASON_UNIQUENESS_NOT_DECLARED``). Loss is
      genuinely measured as ``0.0`` here — invariant 1 is entirely about the
      NO-MULTIPLICATION guarantee not being constraint-backed, so this is a
      pure multiplication-side uncertainty and never a filtering one.

    Every other case reads directly off the measured ratios: filtering iff
    ``row_loss_ratio > 0``, multiplying iff ``row_mult_ratio > 0`` — both can
    be true for the same edge.
    """
    if row_loss_ratio is None and row_mult_ratio is None:
        return True, False
    is_filtering = bool(row_loss_ratio and row_loss_ratio > 0.0)
    is_multiplying = bool(row_mult_ratio and row_mult_ratio > 0.0)
    if reason == REASON_UNIQUENESS_NOT_DECLARED:
        is_multiplying = True
    return is_filtering, is_multiplying


def join_status(
    *,
    is_filtering: bool,
    is_multiplying: bool,
    population_participation: str,
    row_loss_ratio: Optional[float] = None,
    row_mult_ratio: Optional[float] = None,
    threshold: float = DEFAULT_ROW_EFFECT_WARNING_THRESHOLD,
) -> str:
    """Map one join's per-component effect + declared intent onto a status.

    Bug-8652 fix. The contract's own wording for ``enrichment_only`` (§2, item
    2) is "the join may be elided; any row multiplication it causes is
    accepted" — it says nothing about excusing row loss. The rules, from the
    governance plan's phase G1, corrected to excuse only the component each
    flag actually covers:

    * neither filtering nor multiplying                    -> OK
    * declared ``population_defining``                     -> OK (never
      elided at all, so whichever effect(s) it has always apply uniformly —
      excuses BOTH components)
    * declared ``enrichment_only``, multiplying only        -> OK (the only
      effect present is exactly the one this flag covers)
    * declared ``enrichment_only``, ALSO filtering          -> WARNING/BLOCKED
      on the filtering ratio, same threshold rule as ``undeclared`` — the flag
      does not cover row loss, so an unexcused filtering component must not
      read as a clean bill of health just because multiplication is excused
    * ``undeclared`` (or any out-of-enum value, folded to ``undeclared``),
      effect <= threshold                                   -> WARNING
    * ``undeclared``, effect > threshold                     -> BLOCKED
    * ``preserve_base_rows``                                 -> WARNING,
      whatever the magnitude

    ``preserve_base_rows`` is the DEFAULT every pre-existing join carries, so
    it is not an affirmative modeller decision and must not read as OK — but
    escalating it to BLOCKED would, under the active G5 policy, block
    every model that predates this field without a modeller ever having been
    asked. WARNING at any magnitude is the only reading that satisfies both
    invariant 4 ("no existing model changes unless a modeller acts") and
    contract 2 ("never silently ignored"). ``enrichment_only`` IS an
    affirmative declaration, so its unexcused filtering component gets the
    same threshold treatment as ``undeclared`` rather than the capped
    ``preserve_base_rows`` treatment — a join mis-declared ``enrichment_only``
    when it is actually filtering deserves the same visibility as one nobody
    declared at all.

    An unmeasured effect (``None`` ratios) is WARNING, never BLOCKED — see the
    module docstring, property 5.
    """
    if not is_filtering and not is_multiplying:
        return STATUS_OK
    participation = coerce_population_participation(population_participation)
    if participation == POPULATION_PARTICIPATION_POPULATION_DEFINING:
        return STATUS_OK

    excuses_multiplying = participation == POPULATION_PARTICIPATION_ENRICHMENT_ONLY
    unexcused_filtering = is_filtering  # enrichment_only never excuses this.
    unexcused_multiplying = is_multiplying and not excuses_multiplying
    if not unexcused_filtering and not unexcused_multiplying:
        # Every effect this edge actually has is covered by its declared flag
        # (enrichment_only, and the edge only multiplies).
        return STATUS_OK

    if participation == POPULATION_PARTICIPATION_PRESERVE_BASE_ROWS:
        return STATUS_WARNING

    effect = _max_present(
        ([row_loss_ratio] if unexcused_filtering else [])
        + ([row_mult_ratio] if unexcused_multiplying else [])
    )
    if effect is None:
        return STATUS_WARNING
    return STATUS_BLOCKED if effect > threshold else STATUS_WARNING


def build_verdict(
    *,
    join_type: str | None,
    population_participation: str,
    measurement: Optional[EdgeMeasurement],
    near_side: Optional[str],
    left_key_declared_unique: bool,
    right_key_declared_unique: bool,
    threshold: float = DEFAULT_ROW_EFFECT_WARNING_THRESHOLD,
    unmeasured_reason: str = REASON_NOT_MEASURED,
) -> JoinPopulationVerdict:
    """Classify one edge and resolve its status. PURE."""
    participation = coerce_population_participation(population_participation)
    classification, loss, mult, reason = classify_edge(
        join_type=join_type,
        measurement=measurement,
        near_side=near_side,
        left_key_declared_unique=left_key_declared_unique,
        right_key_declared_unique=right_key_declared_unique,
        unmeasured_reason=unmeasured_reason,
    )
    is_filtering, is_multiplying = resolve_edge_effect_components(
        row_loss_ratio=loss, row_mult_ratio=mult, reason=reason,
    )
    effect = row_effect_ratio(loss, mult)
    return JoinPopulationVerdict(
        classification=classification,
        status=join_status(
            is_filtering=is_filtering,
            is_multiplying=is_multiplying,
            population_participation=participation,
            row_loss_ratio=loss,
            row_mult_ratio=mult,
            threshold=threshold,
        ),
        measured=measurement is not None,
        row_loss_ratio=loss,
        row_mult_ratio=mult,
        row_effect_ratio=effect,
        reason=reason,
        population_participation=participation,
    )


def roll_up_model_status(
    verdict_rows: Iterable[Any], *, join_count: int,
) -> ModelPopulationRollup:
    """Roll per-join statuses up to the model status (invariant 6).

    Accepts anything with ``.status`` and ``.measured`` — the pure
    :class:`JoinPopulationVerdict` and the persisted ``JoinPopulationCheck`` ORM
    row both satisfy that, so the deploy path and the health endpoint share one
    rollup implementation instead of two that can drift.

    ``join_count`` is the model's CURRENT number of joins. A model with joins
    but fewer verdicts than joins is not fully evaluated, and says so.
    """
    rows = list(verdict_rows)
    worst = STATUS_OK
    blocked = 0
    warning = 0
    evaluated_count = 0
    for row in rows:
        status = str(getattr(row, "status", STATUS_OK) or STATUS_OK)
        if STATUS_SEVERITY.get(status, 0) > STATUS_SEVERITY.get(worst, 0):
            worst = status
        if status == STATUS_BLOCKED:
            blocked += 1
        elif status == STATUS_WARNING:
            warning += 1
        if bool(getattr(row, "measured", False)):
            evaluated_count += 1
    return ModelPopulationRollup(
        status=worst,
        # Vacuously evaluated when the model declares no joins at all.
        evaluated=(join_count == 0) or (evaluated_count == join_count),
        join_count=join_count,
        evaluated_count=evaluated_count,
        blocked_count=blocked,
        warning_count=warning,
    )


def blocking_join_population_rows(
    verdict_rows: Iterable[Any],
    *,
    threshold: Optional[float] = None,
) -> list[Any]:
    """Return the exact rows authorised to refuse a deployment.

    G5 deliberately uses per-join evidence rather than the model rollup's
    ``evaluated`` flag.  A mixed model may contain one measured blocker and a
    second row that timed out (or was disabled); the measured blocker still
    needs to be acted on, while the unmeasured row can never block.  The two
    affirmative declarations below are the only policies whose unresolved
    effects may block.  ``preserve_base_rows`` is warning-only at every
    magnitude, and the classifier already accounts for enrichment-only's
    multiplying-only exemption before producing ``BLOCKED``.
    """
    blockable_participation = {
        POPULATION_PARTICIPATION_ENRICHMENT_ONLY,
        POPULATION_PARTICIPATION_UNDECLARED,
    }
    blocking: list[Any] = []
    for row in verdict_rows:
        if not (
            bool(getattr(row, "measured", False))
            and str(getattr(row, "status", "") or "") == STATUS_BLOCKED
            and coerce_population_participation(
                getattr(row, "population_participation", None)
            ) in blockable_participation
        ):
            continue
        if threshold is not None:
            effect = getattr(row, "row_effect_ratio", None)
            if effect is None or float(effect) <= threshold:
                continue
        blocking.append(row)
    return blocking


# ---------------------------------------------------------------------------
# Join-graph orientation (which endpoint is nearer the fact table)
# ---------------------------------------------------------------------------


def resolve_near_sides(
    joins: Sequence[Any], *, fact_table_id: Optional[UUID],
) -> dict[UUID, Optional[str]]:
    """Map ``join.id -> "left" | "right" | None`` (None == ambiguous).

    Breadth-first distance from the model's fact table over the undirected join
    graph. The endpoint at the SHORTER distance is the one a fact-rooted plan
    retains; the other is the one elision would drop. Equal distances (a cycle,
    or two arms meeting), an endpoint the walk never reaches, or a model with no
    fact table all yield ``None`` and are measured conservatively in both
    directions.
    """
    if fact_table_id is None:
        return {j.id: None for j in joins}

    adjacency: dict[UUID, set[UUID]] = {}
    for j in joins:
        adjacency.setdefault(j.left_table_id, set()).add(j.right_table_id)
        adjacency.setdefault(j.right_table_id, set()).add(j.left_table_id)

    depth: dict[UUID, int] = {fact_table_id: 0}
    frontier = [fact_table_id]
    while frontier:
        nxt: list[UUID] = []
        for node in frontier:
            for peer in adjacency.get(node, ()):  # noqa: SIM118 - set iteration
                if peer not in depth:
                    depth[peer] = depth[node] + 1
                    nxt.append(peer)
        frontier = nxt

    out: dict[UUID, Optional[str]] = {}
    for j in joins:
        dl = depth.get(j.left_table_id)
        dr = depth.get(j.right_table_id)
        if dl is None or dr is None or dl == dr:
            out[j.id] = None
        else:
            out[j.id] = _SIDE_LEFT if dl < dr else _SIDE_RIGHT
    return out


# ---------------------------------------------------------------------------
# Source probes
# ---------------------------------------------------------------------------


def build_side_probe_sql(
    *,
    connector: str,
    retained_table: str,
    retained_column: str,
    joined_table: str,
    joined_column: str,
) -> str:
    """Count one side of an edge against the other.

    Emits, per retained row: total rows, non-NULL keys, distinct non-NULL keys,
    and whether the row has a partner. The far side is reduced to a DISTINCT key
    set first, so ``COUNT(partner_key)`` counts MATCHED ROWS and never inflates
    on far-side duplicates — multiplication is measured separately, exactly once,
    by :func:`build_inner_join_count_sql`.

    Plain ANSI: ``COUNT``/``COUNT(DISTINCT)``/``LEFT JOIN`` over two derived
    tables. Every identifier goes through ``connector_qualify``; the statement
    is transpiled once by sqlglot (SQL-generation rules 1 and 2). There is no
    per-connector branch anywhere in this module.
    """
    a = quote_identifier(connector, _ALIAS_RETAINED)
    b = quote_identifier(connector, _ALIAS_JOINED)
    k = quote_identifier(connector, _ALIAS_KEY)
    q = _result_aliases(connector)
    sql = (
        f"SELECT COUNT(*) AS {q[COL_ROWS]}, "
        f"COUNT({a}.{k}) AS {q[COL_KEY_NON_NULL]}, "
        f"COUNT(DISTINCT {a}.{k}) AS {q[COL_KEY_DISTINCT]}, "
        f"COUNT({b}.{k}) AS {q[COL_MATCHED]} "
        f"FROM (SELECT {quote_identifier(connector, retained_column)} AS {k} "
        f"FROM {quote_table_ref(connector, retained_table)}) {a} "
        f"LEFT JOIN (SELECT DISTINCT {quote_identifier(connector, joined_column)} AS {k} "
        f"FROM {quote_table_ref(connector, joined_table)}) {b} "
        f"ON {b}.{k} = {a}.{k}"
    )
    return _transpile(sql, connector)


def build_inner_join_count_sql(
    *,
    connector: str,
    left_table: str,
    left_column: str,
    right_table: str,
    right_column: str,
) -> str:
    """Exact inner-join output cardinality for the edge.

    Only issued when at least one side's key is NOT unique in the data — when
    both are unique the inner join emits exactly the matched rows and the
    multiplication ratio is exactly zero with no query at all.
    """
    a = quote_identifier(connector, _ALIAS_RETAINED)
    b = quote_identifier(connector, _ALIAS_JOINED)
    k = quote_identifier(connector, _ALIAS_KEY)
    q = _result_aliases(connector)
    sql = (
        f"SELECT COUNT(*) AS {q[COL_JOIN_ROWS]} "
        f"FROM (SELECT {quote_identifier(connector, left_column)} AS {k} "
        f"FROM {quote_table_ref(connector, left_table)}) {a} "
        f"JOIN (SELECT {quote_identifier(connector, right_column)} AS {k} "
        f"FROM {quote_table_ref(connector, right_table)}) {b} "
        f"ON {b}.{k} = {a}.{k}"
    )
    return _transpile(sql, connector)


def _transpile(canonical_sql: str, connector: str) -> str:
    """Transpile a canonical-postgres statement ONCE to the source dialect.

    Identical to the pattern in ``attribute_relationship_verifier``:
    identifiers are already connector-quoted, this pass handles dialect-level
    syntax, and an untranslatable statement falls back to the canonical form
    rather than silently running something different.
    """
    from shared.connector_qualify import CONNECTOR_TO_SQLGLOT

    target = CONNECTOR_TO_SQLGLOT.get(connector, "postgres")
    if target == "postgres":
        return canonical_sql
    try:
        return sqlglot.transpile(canonical_sql, read="postgres", write=target)[0]
    except Exception:
        return canonical_sql


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


class _BudgetExhausted(Exception):
    """Internal: the model-wide probe deadline passed between statements."""


class _MalformedProbeResult(Exception):
    """Internal: the source returned no row, or a row missing a count.

    The generalisation of Bug-8670. Reading a count with ``row.get(name)`` and
    coalescing a miss to 0 is not a safe default HERE, because all-zero counts
    are indistinguishable from a genuinely clean edge: ``rows=0`` gives zero
    loss, ``key_non_null == key_distinct`` reads as "unique in the data" so the
    third probe is skipped, and the verdict comes out ``neutral`` / ``OK`` /
    ``measured=True``. That is the module's property 5 inverted — a verdict
    manufactured out of the ABSENCE of a measurement, and the most dangerous
    single output this classifier can produce. Any result whose shape we do not
    recognise is a probe failure, not a clean answer.
    """


#: The counts a side probe must return for its result to be usable.
_SIDE_RESULT_COLUMNS: tuple[str, ...] = (
    COL_ROWS, COL_KEY_NON_NULL, COL_KEY_DISTINCT, COL_MATCHED,
)


async def probe_edge(
    *,
    conn_obj: Any,
    connector: str,
    left_table: str,
    left_column: str,
    right_table: str,
    right_column: str,
    tenant_session: Any = None,
    deadline: Optional[float] = None,
) -> tuple[Optional[EdgeMeasurement], str]:
    """Measure one edge against the source. Returns ``(measurement, reason)``.

    Two or three aggregate statements, all through
    ``shared.source_executor.execute_source_sql`` (never a direct driver call),
    all bounded by the source statement timeout that module already applies.
    On timeout or any execution error the measurement is ``None`` with the
    matching reason code — the caller then classifies conservatively.

    ``deadline`` is the model-wide ``time.monotonic()`` cut-off. It is checked
    BEFORE each statement, not just once per join: checking only per join would
    let a join that started just inside the budget still issue three sequential
    statements, making the real deploy bound ``budget + 3x statement timeout``
    instead of the ``budget + 1x`` the caller documents. Partial results are
    discarded rather than mixed with unmeasured ones — a half-measured edge
    would produce a ratio computed against counts from a different moment.
    """
    from shared.source_executor import QueryTimeoutError, execute_source_sql

    async def _one(sql: str, required: tuple[str, ...]) -> dict:
        if deadline is not None and time.monotonic() >= deadline:
            raise _BudgetExhausted
        rows, _cols = await execute_source_sql(
            conn_obj, sql,
            tenant_session=tenant_session,
            purpose="join_population_validation",
        )
        if not rows:
            raise _MalformedProbeResult("probe returned no row")
        # Bug-8670: fold the driver's column names to lower case. Snowflake
        # returns UPPERCASE keys and ``source_executor`` passes
        # ``cursor.description`` through untouched, so reading by the literal
        # lower-case name silently produced zeros — and zeros classify as a
        # confident ``neutral``. Quoting the aliases fixes the emitted SQL;
        # this makes the READ side robust to any driver that normalises anyway.
        row = {str(key).lower(): value for key, value in rows[0].items()}
        missing = [
            name for name in required
            if row.get(name) is None
        ]
        if missing:
            raise _MalformedProbeResult(
                f"probe result missing {', '.join(missing)}"
            )
        return row

    try:
        left_row = await _one(build_side_probe_sql(
            connector=connector,
            retained_table=left_table, retained_column=left_column,
            joined_table=right_table, joined_column=right_column,
        ), _SIDE_RESULT_COLUMNS)
        right_row = await _one(build_side_probe_sql(
            connector=connector,
            retained_table=right_table, retained_column=right_column,
            joined_table=left_table, joined_column=left_column,
        ), _SIDE_RESULT_COLUMNS)
        left = SideProbe(
            rows=_as_int(left_row.get(COL_ROWS)),
            key_non_null=_as_int(left_row.get(COL_KEY_NON_NULL)),
            key_distinct=_as_int(left_row.get(COL_KEY_DISTINCT)),
            matched=_as_int(left_row.get(COL_MATCHED)),
        )
        right = SideProbe(
            rows=_as_int(right_row.get(COL_ROWS)),
            key_non_null=_as_int(right_row.get(COL_KEY_NON_NULL)),
            key_distinct=_as_int(right_row.get(COL_KEY_DISTINCT)),
            matched=_as_int(right_row.get(COL_MATCHED)),
        )
        inner_rows: Optional[int] = None
        if not (left.key_unique_in_data and right.key_unique_in_data):
            join_row = await _one(build_inner_join_count_sql(
                connector=connector,
                left_table=left_table, left_column=left_column,
                right_table=right_table, right_column=right_column,
            ), (COL_JOIN_ROWS,))
            inner_rows = _as_int(join_row.get(COL_JOIN_ROWS))
        return EdgeMeasurement(left=left, right=right, inner_join_rows=inner_rows), (
            REASON_MEASURED
        )
    except _BudgetExhausted:
        return None, REASON_MEASUREMENT_BUDGET_EXHAUSTED
    except _MalformedProbeResult as err:
        logger.warning(
            "join population probe returned an unusable result (%s.%s -> "
            "%s.%s): %s",
            left_table, left_column, right_table, right_column, err,
        )
        return None, REASON_MEASUREMENT_FAILED
    except QueryTimeoutError:
        logger.warning(
            "join population probe timed out (%s.%s -> %s.%s)",
            left_table, left_column, right_table, right_column,
        )
        return None, REASON_MEASUREMENT_TIMEOUT
    except Exception:
        logger.warning(
            "join population probe failed (%s.%s -> %s.%s)",
            left_table, left_column, right_table, right_column, exc_info=True,
        )
        return None, REASON_MEASUREMENT_FAILED


# ---------------------------------------------------------------------------
# Deploy-time driver
# ---------------------------------------------------------------------------


async def validate_model_joins_on_deploy(
    *,
    db: Any,
    model_id: UUID,
    deployed_version_id: Optional[UUID],
    deploy_epoch: int,
    conn_obj: Any,
    connector: str,
    threshold: float = DEFAULT_ROW_EFFECT_WARNING_THRESHOLD,
    measure: bool = True,
    budget_seconds: float = DEFAULT_PROBE_BUDGET_SECONDS,
    tenant_session: Any = None,
    snapshot: Optional[dict[str, Any]] = None,
) -> list[Any]:
    """Classify every join of a model and stage its evidence rows.

    Replaces the model's whole ``join_population_checks`` set: the caller's
    transaction first deletes the existing rows, then this stages the new ones,
    so a stale verdict can never outlive the deploy that produced it and "no
    row" honestly means "not evaluated at the last deploy".

    Rows are added to ``db`` and committed by the CALLER, in the same deploy
    transaction, so the evidence carries the committed deploy epoch — the same
    contract ``verify_model_relationships_on_deploy`` uses.

    NEVER raises and NEVER decides the deploy gate itself; the model-service
    caller consumes the returned rows for G5 enforcement.
    ``measure=False`` records conservative unmeasured verdicts without touching
    the source at all; that is what a caller passes when validation is switched
    off for the model or no source connection resolved.

    ``budget_seconds`` bounds how long the WHOLE model's probing may take.
    Deploy is a synchronous request and a wide model can declare dozens of
    joins; at 2-3 statements each, a source that is merely slow (not failing)
    would otherwise hold the deploy open for joins x 3 x the source statement
    timeout. Once the budget is spent, the remaining joins record an UNMEASURED
    verdict with ``measurement_budget_exhausted`` — the rollup then reports
    ``evaluated=False``, which is honest, instead of the deploy stalling.

    The deadline is checked before EVERY source statement (see ``probe_edge``),
    not only between joins. Checking only between joins would let a join that
    started just inside the budget still issue three sequential statements,
    making the true bound ``budget + 3x statement timeout``. With the
    per-statement check at most one statement can be in flight when the budget
    runs out, and an in-flight statement is not interrupted, so the real worst
    case is ``budget + one statement timeout``.
    """
    from sqlalchemy import delete, select

    from shared.db.models import Join, JoinPopulationCheck, ModelColumn, ModelTable

    staged: list[Any] = []
    try:
        # Always clear first, even when there is nothing to record: an empty set
        # must read as "not evaluated", not as last deploy's verdicts.
        await db.execute(
            delete(JoinPopulationCheck).where(
                JoinPopulationCheck.model_id == model_id
            )
        )
        if snapshot is None:
            joins = list(
                (
                    await db.execute(
                        select(Join).where(Join.model_id == model_id).order_by(Join.id)
                    )
                ).scalars().all()
            )
            tables = {
                t.id: t
                for t in (
                    await db.execute(
                        select(ModelTable).where(ModelTable.model_id == model_id)
                    )
                ).scalars().all()
            }
            column_ids = {
                cid
                for j in joins
                for cid in (j.left_column_id, j.right_column_id)
                if cid is not None
            }
            columns = {
                c.id: c
                for c in (
                    await db.execute(
                        select(ModelColumn).where(ModelColumn.id.in_(column_ids))
                    )
                ).scalars().all()
            } if column_ids else {}
        else:
            # Deploy policy is about the selected immutable version, never the
            # mutable draft graph.  Operational rows (evidence/FKs) continue
            # through this tenant transaction, but they never supply classifier
            # definitions when a snapshot was explicitly selected.
            joins, tables, columns = _snapshot_graph(snapshot)

        # The evidence row carries the selected graph's labels as well as its
        # machine identity. This keeps health/deploy diagnostics useful after
        # the mutable draft deletes or renames the selected join.
        selected_labels = _join_labels(joins, tables, columns)

        if not joins:
            return staged

        # Bug-8668: the FULL declared-key column set per table, not just the two
        # endpoint columns. ``source_introspection`` stamps ``is_primary_key``
        # on EVERY column of a PRIMARY KEY constraint, so a composite key flags
        # two columns and reading the endpoint's own flag would call
        # ``(product_id, variant_code)`` a uniqueness guarantee for a join on
        # ``product_id`` alone — which matches N rows per retained row. This is
        # the same rule ``query-router/src/routing/pocket_population.py``
        # (``_declared_key_columns``) already applies to the same flag for the
        # same question; the two consumers must not disagree.
        pk_columns_by_table: dict[Any, set[Any]] = {}
        if snapshot is None:
            if tables:
                for col in (
                    await db.execute(
                        select(ModelColumn).where(
                            ModelColumn.model_table_id.in_(list(tables.keys())),
                            ModelColumn.is_primary_key.is_(True),
                        )
                    )
                ).scalars().all():
                    pk_columns_by_table.setdefault(col.model_table_id, set()).add(col.id)
        else:
            for col in columns.values():
                if getattr(col, "is_primary_key", False):
                    pk_columns_by_table.setdefault(
                        col.model_table_id, set()
                    ).add(col.id)

        # Deterministic anchor. ``uq_model_tables_one_fact_per_model`` makes a
        # second fact table impossible at the storage layer, but picking with a
        # bare ``next()`` over a dict would silently make the BFS root — and so
        # every edge's near/far verdict — depend on row order if that guard
        # were ever relaxed or bypassed. Same determinism class as Bug-8605's
        # canonical graph order; sorting costs nothing.
        #
        # The fact test itself goes through ``graph_order.is_fact_table``, not a
        # local ``== "fact"``. That primitive exists because two copies of this
        # comparison drifted apart inside a single commit and re-opened
        # Bug-8600's fail-open; this root choice decides every edge's near/far
        # verdict, so it is exactly the kind of caller that must not own a
        # private copy.
        fact_ids = sorted(
            (t.id for t in tables.values() if is_fact_table(t)), key=str,
        )
        fact_id = fact_ids[0] if fact_ids else None
        near_by_join = resolve_near_sides(joins, fact_table_id=fact_id)

        # One deadline for the whole model, enforced before EVERY source
        # statement (see ``probe_edge``) rather than only between joins, so the
        # deploy's real bound is the budget plus at most one in-flight
        # statement timeout.
        deadline = (
            time.monotonic() + budget_seconds if budget_seconds > 0 else None
        )
        # ONE budget check, in ``probe_edge``, before every statement. This
        # latch just remembers that it fired, so the joins after it skip the
        # source entirely instead of each re-discovering the same expired
        # deadline, and the operator gets one warning rather than one per join.
        # Deliberately not ALSO pre-checking the clock here: a second check
        # with the same condition would be an unreachable branch that no
        # mutation could distinguish from the real one.
        budget_spent = False
        for j in joins:
            verdict = await _verdict_for_join(
                join=j,
                tables=tables,
                columns=columns,
                pk_columns_by_table=pk_columns_by_table,
                near_side=near_by_join.get(j.id),
                conn_obj=conn_obj,
                connector=connector,
                threshold=threshold,
                measure=measure and not budget_spent,
                deadline=deadline,
                unmeasured_reason=(
                    REASON_MEASUREMENT_BUDGET_EXHAUSTED if budget_spent
                    else REASON_NOT_MEASURED
                ),
                tenant_session=tenant_session,
            )
            if (
                verdict.reason == REASON_MEASUREMENT_BUDGET_EXHAUSTED
                and not budget_spent
            ):
                budget_spent = True
                logger.warning(
                    "join population validation hit its %.0fs probe budget on "
                    "model %s; remaining joins recorded unmeasured",
                    budget_seconds, model_id,
                )
            row = JoinPopulationCheck(
                join_id=j.id,
                model_id=model_id,
                deployed_version_id=deployed_version_id,
                deploy_epoch=deploy_epoch,
                classification=verdict.classification,
                population_participation=verdict.population_participation,
                status=verdict.status,
                measured=verdict.measured,
                row_loss_ratio=verdict.row_loss_ratio,
                row_mult_ratio=verdict.row_mult_ratio,
                row_effect_ratio=verdict.row_effect_ratio,
                reason=verdict.reason,
                inputs_fingerprint=join_definition_fingerprint(j),
                **{
                    name: selected_labels.get(str(j.id), {}).get(name)
                    for name in (
                        "join_label", "left_table_name", "right_table_name",
                        "left_column_name", "right_column_name",
                    )
                },
            )
            db.add(row)
            staged.append(row)
    except Exception:
        # Measurement failure must never fail a deploy; the caller only blocks
        # rows successfully returned and classified by this driver.
        logger.warning(
            "join population validation skipped for model %s", model_id,
            exc_info=True,
        )
    return staged


#: Every ``Join`` attribute :func:`_verdict_for_join` feeds into a verdict.
#: Adding a classifier input means adding it HERE, and staleness then follows
#: automatically — which is the whole point of a fingerprint over another
#: hand-picked field comparison.
_CLASSIFICATION_INPUTS: tuple[str, ...] = (
    "join_type",
    "population_participation",
    "left_table_id",
    "right_table_id",
    "left_column_id",
    "right_column_id",
)


def join_definition_fingerprint(join: Any) -> str:
    """Hash of everything about a join that can change its classification.

    A stored verdict is only current while the join it was measured against is
    unchanged. Keying that on the deploy epoch alone is not enough:
    ``PATCH /joins/{id}`` can change ``join_type`` or either join column —
    all direct inputs to :func:`classify_edge` — and does NOT bump
    ``Model.deploy_epoch``. A LEFT-measured ``neutral`` verdict would then keep
    reading as current after the modeller switched the join to INNER, giving a
    clean bill of health to an edge that now drops rows.

    Comparing one hand-picked field is how that gap appeared in the first
    place (Bug-8667 fixed ``population_participation`` and left the rest), so
    this hashes the WHOLE input tuple. Both the writer (the deploy-time
    classifier) and the reader (the health endpoint) call this one function, so
    they cannot disagree about what "unchanged" means.
    """
    import hashlib

    parts = []
    for name in _CLASSIFICATION_INPUTS:
        value = getattr(join, name, None)
        if name == "join_type":
            value = normalise_token(value)
        elif name == "population_participation":
            value = coerce_population_participation(value)
        parts.append(f"{name}={'' if value is None else value}")
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def _declares_single_column_key(
    pk_columns_by_table: dict, table_id: Any, column_id: Any,
) -> bool:
    """True only when ``column_id`` is the table's WHOLE declared key.

    Bug-8668. ``ModelColumn.is_primary_key`` is stamped on every column of a
    PRIMARY KEY constraint, so a composite key flags several columns and the
    per-column flag cannot answer "does a value of this column identify at most
    one row". Joining on one half of ``(product_id, variant_code)`` matches N
    rows per retained row — the fan-out this classifier exists to catch — while
    the endpoint column's own flag says True.

    Identical rule to ``pocket_population._declared_key_columns``, the other
    consumer of the same flag answering the same question. A table with NO
    declared key returns False, which is the Bug-8618 metadata gap and reports
    as ``uniqueness_not_declared``, not as neutrality.
    """
    declared = pk_columns_by_table.get(table_id) or set()
    return declared == {column_id}


async def _verdict_for_join(
    *,
    join: Any,
    tables: dict,
    columns: dict,
    pk_columns_by_table: dict,
    near_side: Optional[str],
    conn_obj: Any,
    connector: str,
    threshold: float,
    measure: bool,
    tenant_session: Any,
    unmeasured_reason: str = REASON_NOT_MEASURED,
    deadline: Optional[float] = None,
) -> JoinPopulationVerdict:
    """Resolve one join's endpoints, measure it, and build its verdict."""
    participation = coerce_population_participation(
        getattr(join, "population_participation", DEFAULT_POPULATION_PARTICIPATION)
    )
    left_table = tables.get(join.left_table_id)
    right_table = tables.get(join.right_table_id)
    left_col = columns.get(join.left_column_id)
    right_col = columns.get(join.right_column_id)

    if not (left_table and right_table and left_col and right_col):
        return build_verdict(
            join_type=getattr(join, "join_type", None),
            population_participation=participation,
            measurement=None,
            near_side=near_side,
            left_key_declared_unique=False,
            right_key_declared_unique=False,
            threshold=threshold,
            unmeasured_reason=REASON_UNRESOLVED_ENDPOINT,
        )

    measurement: Optional[EdgeMeasurement] = None
    probe_reason = unmeasured_reason
    if measure:
        measurement, probe_reason = await probe_edge(
            conn_obj=conn_obj,
            connector=connector,
            left_table=left_table.physical_name,
            left_column=left_col.column_name,
            right_table=right_table.physical_name,
            right_column=right_col.column_name,
            tenant_session=tenant_session,
            deadline=deadline,
        )

    return build_verdict(
        join_type=getattr(join, "join_type", None),
        population_participation=participation,
        measurement=measurement,
        near_side=near_side,
        # Constraint-backed uniqueness only (invariant 1). Bug-8618's source
        # introspection is what populates ``is_primary_key``; until it runs on a
        # given model this stays False and the edge reports the metadata gap
        # rather than claiming neutrality it cannot prove.
        left_key_declared_unique=_declares_single_column_key(
            pk_columns_by_table, join.left_table_id, join.left_column_id,
        ),
        right_key_declared_unique=_declares_single_column_key(
            pk_columns_by_table, join.right_table_id, join.right_column_id,
        ),
        threshold=threshold,
        unmeasured_reason=probe_reason,
    )
