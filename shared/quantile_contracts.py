"""Immutable contracts for proof-carrying percentile/quantile routing.

Single source of truth (spec §4.1/§4.2/§8) for the value objects the
query-router proof engine and the persistence layer share:

- ``QuantileMethod`` — continuous vs discrete (never interchangeable, I2).
- ``QuantileRequest`` — one inventoried quantile the query reads (I1).
- ``QuantileCoverage`` — the persisted, versioned semantic identity of one
  materialised pNN column (I2/I8); the authoritative proof input, NOT the
  ``pNN`` physical-name suffix (Gap D).
- ``QuantileReason`` — the stable fail-closed reason-code registry (§8).
- ``QuantileServeVerdict`` — a coverage match (per-request) or a structured
  miss.

These are pure, dependency-free dataclasses so the proof core can be unit
tested without a DB, a model, or sqlglot. Fractions are exact ``Decimal``
values, never binary floats (§4.1) — float rounding must never let
``PERCENTILE_CONT(0.3333333333)`` alias a ``pNN`` column.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Optional


# --- Method (I2) -----------------------------------------------------------
# Continuous interpolates between ranks; discrete selects a stored data value
# at a rank. ``p50 CONT`` is NOT interchangeable with ``p50 DISC`` (§10).
METHOD_CONTINUOUS = "continuous"
METHOD_DISCRETE = "discrete"
_VALID_METHODS = frozenset({METHOD_CONTINUOUS, METHOD_DISCRETE})


ORDER_ASC = "asc"
ORDER_DESC = "desc"
_VALID_DIRECTIONS = frozenset({ORDER_ASC, ORDER_DESC})


NULL_IGNORE = "ignore_nulls"
NULL_RESPECT = "respect_nulls"


# Exactness of a materialised column (I8). ``unknown`` = build evidence cannot
# prove exact production; NEVER served in exact mode.
EXACT = "exact"
BOUNDED_APPROX = "bounded_approximate"
UNKNOWN = "unknown"


# Request origins (spec §4.1). ``measure_default`` = a bare measure whose
# ``default_agg`` is a quantile stat (Bug-7779/7780 semantic-inventory path).
ORIGIN_SELECT = "select"
ORIGIN_HAVING = "having"
ORIGIN_ORDER_BY = "order_by"
ORIGIN_CALCULATED = "calculated"
ORIGIN_MEASURE_DEFAULT = "measure_default"


def to_fraction(value: object) -> Optional[Decimal]:
    """Coerce a literal to an exact ``Decimal`` fraction in [0, 1], or None.

    Accepts ``Decimal``, ``int``, ``str`` (exact) — NEVER a Python ``float``
    (binary rounding would corrupt exact fraction equality, spec §4.1, §16.14).
    A float argument returns None so the caller fails closed to source rather
    than matching a ``pNN`` column through rounding.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, float):
        return None
    try:
        if isinstance(value, Decimal):
            frac = value
        elif isinstance(value, int):
            frac = Decimal(value)
        elif isinstance(value, str):
            frac = Decimal(value.strip())
        else:
            return None
    except (InvalidOperation, ValueError):
        return None
    if frac < 0 or frac > 1:
        return None
    return frac


def measure_value_type(measure: object) -> Optional[str]:
    """Best-effort canonical source value type of a measure's input column.

    Shared between the PRODUCER (coverage writer) and the CONSUMER (binder's
    QuantileRequest builder) so both resolve the same type string end to end.
    Returns None when unknown — the proof then fails closed on DISCRETE
    percentiles (VALUE_TYPE_LOSSY) while CONTINUOUS is unaffected.

    Mirrors the binder's ``_measure_value_type`` attribute chain: the canonical
    location is now HERE so both sides use one function (Bug-7852 R1 fix).
    """
    for attr in ("data_type", "source_data_type", "physical_type", "column_type"):
        value = getattr(measure, attr, None)
        if value:
            return str(value)
    return None


def measure_value_definition(
    measure: object,
    source_column_name: Optional[str] = None,
    source_table_physical_name: Optional[str] = None,
    source_table_source_id: Optional[str] = None,
) -> str:
    """Stable identity of the measure's value-producing definition.

    Covers all binding paths: ``source_column_id`` (standard measures),
    ``expression`` (calculated measures), ``user_defined_attribute_id``
    (UDA-bound measures). A change to ANY of these means the percentile
    is computed over different data, so the fingerprint must change.

    ``source_column_name``: physical column name from the ModelColumn row.
    ``source_table_physical_name``: physical table name from ModelTable.
    ``source_table_source_id``: the table's source_id (connection identity).
    All are hashed when provided so a same-ID column repointed to a different
    table, schema, or connection produces a different fingerprint -> fail closed.

    Both the PRODUCER and CONSUMER call this so a save-without-deploy that
    changes the source column, expression, or UDA -> different fingerprint
    -> fail closed to source (no wrong pNN via deployed-snapshot drift).
    """
    import hashlib

    scid = getattr(measure, "source_column_id", None)
    if scid:
        parts = [str(scid).strip().lower()]
        if source_column_name:
            parts.append(source_column_name.strip().lower())
        if source_table_physical_name:
            parts.append(source_table_physical_name.strip().lower())
        if source_table_source_id:
            parts.append(str(source_table_source_id).strip().lower())
        if len(parts) > 1:
            return hashlib.sha256(":".join(parts).encode()).hexdigest()[:24]
        return parts[0]
    expr = getattr(measure, "expression", None)
    if expr:
        return "expr:" + hashlib.sha256(expr.strip().encode()).hexdigest()[:16]
    uda_id = getattr(measure, "user_defined_attribute_id", None)
    if uda_id:
        return "uda:" + str(uda_id).strip().lower()
    return ""


def build_input_fingerprint(
    measure_name: str,
    value_type: Optional[str],
    value_definition: Optional[str] = None,
) -> str:
    """Canonical bound-input fingerprint shared by the coverage PRODUCER (the
    materialisation/backfill that writes ``QuantileCoverage.input_expression_
    fingerprint``) and the CONSUMER (the binder that builds a
    ``QuantileRequest``). Both MUST call this so a request and its column agree
    end to end (producer/consumer alignment).

    The fingerprint includes three components:
    - ``measure_name``: the semantic measure name (case-insensitive).
    - ``value_type``: the canonical source value type (e.g. "numeric").
    - ``value_definition``: the stable identity of the measure's value-producing
      definition (source_column_id, expression hash, or UDA id) from
      ``measure_value_definition``. A save-without-deploy that changes the
      source -> different definition -> different fingerprint -> no match ->
      fail closed to source (no wrong pNN via deployed-snapshot drift).

    ``value_definition`` is optional for backward compatibility with callers
    that have not yet adopted it; both producer and consumer SHOULD pass it.
    """
    name = (measure_name or "").strip().lower()
    vt = (value_type or "").strip().lower()
    vd = (value_definition or "").strip().lower()
    return f"{name}|{vt}|{vd}"


@dataclass(frozen=True)
class QuantileRequest:
    """One inventoried quantile the query reads (spec §4.1, invariant I1).

    Built AFTER binding from resolved measures + parsed occurrences, never from
    surface syntax alone. Two requests are the *same computation* when their
    ``(measure_name, input_expression_fingerprint, fraction, method,
    order_direction, null_policy, value_type)`` tuple is identical; output
    locations (alias/origin) may differ.
    """
    request_id: str
    semantic_measure_name: str
    input_expression_fingerprint: str
    fraction: Decimal
    method: str                         # METHOD_CONTINUOUS | METHOD_DISCRETE
    order_direction: str                # ORDER_ASC | ORDER_DESC
    null_policy: str = NULL_IGNORE
    value_type: Optional[str] = None
    collation: Optional[str] = None
    timezone: Optional[str] = None
    origin: str = ORIGIN_SELECT
    output_alias: Optional[str] = None
    measure_id: Optional[str] = None
    # 'median' = pre-existing MEDIAN(col) p50 serving; 'ordered_set' = the new
    # explicit PERCENTILE_CONT/DISC syntax gated behind quantile_routing.proof_mode.
    # Lets the matcher keep serving MEDIAN when the feature is off while routing
    # ordered-set percentiles to source (strict no-regression when disabled).
    source_syntax: str = "ordered_set"

    def __post_init__(self) -> None:
        if self.method not in _VALID_METHODS:
            raise ValueError(f"invalid quantile method: {self.method!r}")
        if self.order_direction not in _VALID_DIRECTIONS:
            raise ValueError(f"invalid order_direction: {self.order_direction!r}")
        if not isinstance(self.fraction, Decimal):
            raise TypeError("fraction must be an exact Decimal, never a float")

    @property
    def ascending_fraction(self) -> Optional[Decimal]:
        """The fraction to look up against ASC-stored coverage.

        CONTINUOUS interpolation is positionally symmetric, so a DESC request
        equals ``CONT(1 - p) ASC`` for every multiset (§4.1 reviewer
        correction). DISCRETE has NO data-independent ascending equivalent:
        return None so the caller matches only direction-identical coverage and
        never rewrites a DESC discrete fraction to an ascending one.
        """
        if self.method == METHOD_CONTINUOUS:
            if self.order_direction == ORDER_DESC:
                return Decimal(1) - self.fraction
            return self.fraction
        # discrete
        if self.order_direction == ORDER_ASC:
            return self.fraction
        return None


@dataclass(frozen=True)
class QuantileCoverage:
    """Persisted, versioned semantic identity of one materialised pNN column.

    Spec §4.2. The authoritative proof input (Gap D): a ``pNN`` physical-name
    suffix proves only a conventional fraction, NOT method/direction/null/type/
    exactness. Legacy columns with no coverage row are treated as
    ``exactness=unknown`` and never served in exact mode (I8).

    ``physical_column_name`` is the stored column the rewriter reads (quoted at
    render time). ``fraction`` and ``ascending_fraction`` are exact Decimals.
    """
    physical_column_name: str
    semantic_measure_name: str
    input_expression_fingerprint: str
    fraction: Decimal
    method: str                         # METHOD_CONTINUOUS | METHOD_DISCRETE
    order_direction: str                # ORDER_ASC | ORDER_DESC
    null_policy: str = NULL_IGNORE
    value_type: Optional[str] = None
    collation: Optional[str] = None
    timezone: Optional[str] = None
    exactness: str = UNKNOWN            # EXACT | BOUNDED_APPROX | UNKNOWN
    coverage_schema_version: int = 1

    def __post_init__(self) -> None:
        if self.method not in _VALID_METHODS:
            raise ValueError(f"invalid coverage method: {self.method!r}")
        if self.order_direction not in _VALID_DIRECTIONS:
            raise ValueError(f"invalid coverage order_direction: {self.order_direction!r}")
        if not isinstance(self.fraction, Decimal):
            raise TypeError("coverage fraction must be an exact Decimal")

    @property
    def ascending_fraction(self) -> Decimal:
        """Fraction expressed in ascending terms, for CONT symmetric lookup.

        Discrete coverage carries its own ``order_direction`` as a first-class
        matching key, so this is only consulted on the continuous path.
        """
        if self.method == METHOD_CONTINUOUS and self.order_direction == ORDER_DESC:
            return Decimal(1) - self.fraction
        return self.fraction


class QuantileReason:
    """Stable fail-closed reason codes (spec §8). Every miss carries one so the
    optimizer/explain surface can tell a modeller precisely why source was used.
    """
    PARSE_UNSUPPORTED = "QUANTILE_PARSE_UNSUPPORTED"
    FRACTION_NOT_COVERED = "QUANTILE_FRACTION_NOT_COVERED"
    METHOD_MISMATCH = "QUANTILE_METHOD_MISMATCH"
    DIRECTION_MISMATCH = "QUANTILE_DIRECTION_MISMATCH"
    ARTIFACT_NOT_UNIQUE = "QUANTILE_ARTIFACT_NOT_UNIQUE"
    VALUE_TYPE_LOSSY = "QUANTILE_VALUE_TYPE_LOSSY"
    EVAL_SEMANTICS_UNPROVEN = "QUANTILE_EVAL_SEMANTICS_UNPROVEN"
    INPUT_MISMATCH = "QUANTILE_INPUT_MISMATCH"
    NULL_POLICY_MISMATCH = "QUANTILE_NULL_POLICY_MISMATCH"
    GRAIN_MISSING = "QUANTILE_GRAIN_MISSING"
    GRAIN_EXTRA = "QUANTILE_GRAIN_EXTRA"
    ROW_PREDICATE_MISMATCH = "QUANTILE_ROW_PREDICATE_MISMATCH"
    KEY_PREDICATE_NOT_CONTAINED = "QUANTILE_KEY_PREDICATE_NOT_CONTAINED"
    PREDICATE_PROOF_UNSUPPORTED = "QUANTILE_PREDICATE_PROOF_UNSUPPORTED"
    EXACTNESS_UNKNOWN = "QUANTILE_EXACTNESS_UNKNOWN"
    APPROX_NOT_OPTED_IN = "QUANTILE_APPROX_NOT_OPTED_IN"
    RLS_SPLITS_GROUP = "QUANTILE_RLS_SPLITS_GROUP"
    HAVING_UNCOVERED = "QUANTILE_HAVING_UNCOVERED"
    CALC_DEPENDENCY_UNCOVERED = "QUANTILE_CALC_DEPENDENCY_UNCOVERED"
    MULTI_ARTIFACT_REQUIRED = "QUANTILE_MULTI_ARTIFACT_REQUIRED"
    ARTIFACT_STALE = "QUANTILE_ARTIFACT_STALE"
    REWRITE_PROOF_FAILED = "QUANTILE_REWRITE_PROOF_FAILED"
    LATE_BOUND_FRACTION = "QUANTILE_LATE_BOUND_FRACTION"
    # Feature-flag gate (spec §15): proof mode is not 'enforce' for this
    # tenant/model, so an explicit ordered-set percentile routes to source.
    ROUTING_DISABLED = "QUANTILE_ROUTING_DISABLED"


@dataclass(frozen=True)
class QuantileServeVerdict:
    """Per-candidate outcome: a match (``matched=True`` with the request→column
    map) or a structured miss (``matched=False`` with a stable reason code).

    ``request_to_column`` maps ``QuantileRequest.request_id`` ->
    ``QuantileCoverage`` for every quantile the candidate proves. Only a fully
    matched verdict is eligible to serve.
    """
    matched: bool
    reason: Optional[str] = None
    safe_message: Optional[str] = None
    request_to_column: dict = field(default_factory=dict)
    request_ids: tuple = ()

    @classmethod
    def miss(cls, reason: str, message: str = "", request_ids: tuple = ()) -> "QuantileServeVerdict":
        return cls(matched=False, reason=reason, safe_message=message, request_ids=request_ids)

    @classmethod
    def match(cls, request_to_column: dict) -> "QuantileServeVerdict":
        return cls(matched=True, request_to_column=dict(request_to_column))
