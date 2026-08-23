"""T0 canonicaliser + reason-code tests for derived-grain routing (spec §14.5).

These are pure unit tests over the shared canonicaliser and the stable reason
codes. They assert the adversarial identity properties the spec's correctness
argument (§13) and invariants I1/I11 depend on:

  - DATE_TRUNC('month', ts) and EXTRACT(month FROM ts) fingerprint DISTINCTLY
    (the January-2025 / January-2026 collapse defence, spec §13, §12);
  - the semantic unit literal ('month' vs 'year') is part of the identity;
  - harmless quote / whitespace / alias / redundant-paren differences fold to
    one identity (proof-preserving normalisation, spec §6.1 steps 6-8);
  - mathematically-commutative operands are NOT reordered (spec §6.1 step 7);
  - a function absent from the registry is flagged unknown -> SOURCE_ONLY-eligible.

They are behaviour-free: nothing here routes a query. They protect the Phase 1
capture layer against a future edit that would let a name heuristic or a lossy
normalisation reintroduce the wrong-numbers class.
"""
from __future__ import annotations

from shared.semantic.derived_expression import (
    CANONICALIZER_VERSION,
    canonicalise_sql,
    load_semantics_registry,
    registry_version,
)
from shared.semantic.derived_grain_reasons import (
    ALL_DERIVED_REASON_CODES,
    DerivedReasonCode,
)


def fp(sql: str) -> str:
    ce = canonicalise_sql(sql)
    assert ce is not None, f"failed to canonicalise: {sql!r}"
    return ce.fingerprint


# ---------------------------------------------------------------------------
# Adversarial distinctness (spec §13 correctness argument, I1/I11)
# ---------------------------------------------------------------------------


def test_date_trunc_month_distinct_from_extract_month():
    """The core January-collapse defence: DATE_TRUNC month and EXTRACT month
    must never share an identity, because their partitions differ across years
    (2025-01 vs 2026-01 collapse into month=1 under EXTRACT)."""
    assert fp("DATE_TRUNC('month', order_date)") != fp("EXTRACT(month FROM order_date)")


def test_date_trunc_unit_is_part_of_identity():
    assert fp("DATE_TRUNC('month', order_date)") != fp("DATE_TRUNC('year', order_date)")
    assert fp("DATE_TRUNC('month', order_date)") != fp("DATE_TRUNC('quarter', order_date)")


def test_date_trunc_and_timestamp_trunc_nodes_are_distinct():
    """sqlglot parses BigQuery DATE_TRUNC (over a DATE) to a DateTrunc node and
    TIMESTAMP_TRUNC (over a TIMESTAMP) to a TimestampTrunc node. They render
    identically to postgres DATE_TRUNC but are different typed operations (spec
    I5), so their pre-binding identities must NOT collide."""
    import sqlglot
    from shared.semantic.derived_expression import canonicalise_ast

    ts = canonicalise_ast(
        sqlglot.parse_one("TIMESTAMP_TRUNC(ts, MONTH)", read="bigquery"),
        input_dialect="bigquery",
    )
    dt = canonicalise_ast(
        sqlglot.parse_one("DATE_TRUNC(ts, MONTH)", read="bigquery"),
        input_dialect="bigquery",
    )
    assert ts.fingerprint != dt.fingerprint
    assert ts.semantic_function_ids == ["timestamp_trunc"]
    assert dt.semantic_function_ids == ["date_trunc"]
    # The PostgreSQL headline expression parses to the TimestampTrunc node and is
    # a KNOWN (registered) function — not flagged unknown / source-only.
    pg = canonicalise_sql("DATE_TRUNC('month', order_date)")
    assert pg is not None and pg.has_unknown_function is False
    assert pg.semantic_function_ids == ["timestamp_trunc"]


def test_extract_is_unknown_function_in_v1_registry():
    """EXTRACT has no v1 registry entry, so it is flagged unknown (source-only
    eligible). DATE_TRUNC is registered and is NOT unknown."""
    ex = canonicalise_sql("EXTRACT(month FROM order_date)")
    dt = canonicalise_sql("DATE_TRUNC('month', order_date)")
    assert ex is not None and dt is not None
    assert ex.has_unknown_function is True
    assert dt.has_unknown_function is False
    # PostgreSQL DATE_TRUNC('month', ts) parses to the TimestampTrunc node.
    assert "timestamp_trunc" in dt.semantic_function_ids


def test_different_input_column_distinct():
    assert fp("DATE_TRUNC('month', order_date)") != fp("DATE_TRUNC('month', ship_date)")


def test_cast_and_try_cast_are_distinct_despite_identical_render():
    """CAST may error on invalid input; TRY_CAST / SAFE_CAST returns NULL. They
    render to the same canonical SQL (``CAST(x AS INT)``) but have different
    NULL/error semantics (spec I5/I6), so their identities MUST diverge — the
    folded semantic-function ids carry the distinction the rendered SQL loses."""
    assert fp("CAST(x AS INT)") != fp("TRY_CAST(x AS INT)")
    # SAFE_CAST (BigQuery spelling of TRY_CAST) folds WITH try_cast, not with cast.
    assert fp("SAFE_CAST(x AS INT)") == fp("TRY_CAST(x AS INT)")
    assert fp("SAFE_CAST(x AS INT)") != fp("CAST(x AS INT)")


def test_commutative_operands_not_reordered():
    """Overflow / float / collation / 3VL make operand reordering observable,
    so a - b and b - a must stay distinct (spec §6.1 step 7)."""
    assert fp("a - b") != fp("b - a")
    assert fp("a / b") != fp("b / a")


# ---------------------------------------------------------------------------
# Proof-preserving normalisation folds (spec §6.1 steps 6-8)
# ---------------------------------------------------------------------------


def test_whitespace_and_case_of_keyword_fold():
    assert fp("UPPER(country_code)") == fp("upper(  country_code  )")


def test_alias_wrapper_folds():
    assert fp("DATE_TRUNC('month', order_date)") == fp("DATE_TRUNC('month', order_date) AS m")


def test_redundant_parens_fold():
    assert fp("DATE_TRUNC('month', order_date)") == fp("(DATE_TRUNC('month', (order_date)))")


def test_quoted_and_unquoted_identifiers_stay_distinct_before_binding():
    # Spec §6.1 steps 3/6: quote normalisation is a BIND-TIME operation, not a
    # pre-binding one. In PostgreSQL unquoted ``Foo`` folds to ``foo`` while
    # ``"Foo"`` may name a different column, so the pre-binding canonical form
    # MUST keep them distinct — collapsing them here would conflate two physical
    # columns into one identity (I1/I2). The binder folds quoting when it
    # substitutes the stable column id.
    assert fp('UPPER("Foo")') != fp("UPPER(Foo)")
    # Same-spelling lowercase identifiers are still identical (nothing to fold).
    assert fp("UPPER(country_code)") == fp("upper(country_code)")


# ---------------------------------------------------------------------------
# Canonicaliser metadata + registry
# ---------------------------------------------------------------------------


def test_input_columns_collected_completely():
    # Lineage completeness is the contract (every leaf column is reported);
    # ORDER is not part of the identity, so it is not asserted here.
    ce = canonicalise_sql("CASE WHEN a > b THEN c ELSE d END")
    assert ce is not None
    assert set(ce.input_columns) == {"a", "b", "c", "d"}


def test_registry_version_and_canonicalizer_version_present():
    ce = canonicalise_sql("UPPER(country_code)")
    assert ce is not None
    assert ce.canonicalizer_version == CANONICALIZER_VERSION
    assert ce.registry_version == registry_version()


def test_registry_loads_and_has_trunc_functions():
    reg = load_semantics_registry()
    assert "functions" in reg
    # The TIMESTAMP truncation is timezone-dependent; the DATE truncation is not.
    assert reg["functions"]["timestamp_trunc"]["timezone_dependent"] is True
    assert reg["functions"]["date_trunc"]["timezone_dependent"] is False


def test_unparseable_expression_returns_none():
    assert canonicalise_sql("SELECT )(") is None


# ---------------------------------------------------------------------------
# Reason codes (spec §10.1)
# ---------------------------------------------------------------------------


def test_reason_codes_are_stable_strings():
    assert DerivedReasonCode.ATTRIBUTE_RELATIONSHIP_STALE.value == "ATTRIBUTE_RELATIONSHIP_STALE"
    assert DerivedReasonCode.DERIVED_MEASURE_NOT_ROLLUP_SAFE == "DERIVED_MEASURE_NOT_ROLLUP_SAFE"


def test_reason_codes_cover_spec_vocabulary():
    # Spec §10.1 lists exactly these stable codes; guard against silent drift.
    expected = {
        "DERIVED_EXACT_KEY_NOT_FOUND",
        "DERIVED_INPUT_KEY_MISSING",
        "DERIVED_FUNCTION_UNKNOWN",
        "DERIVED_NONDETERMINISTIC",
        "DERIVED_MAY_ERROR",
        "DERIVED_DIALECT_SEMANTICS_UNPROVEN",
        "DERIVED_TIMEZONE_UNPINNED",
        "DERIVED_COLLATION_UNPINNED",
        "DERIVED_PREDICATE_NOT_MOVABLE",
        "DERIVED_RLS_KEY_MISSING",
        "DERIVED_MEASURE_NOT_ROLLUP_SAFE",
        "DERIVED_LEGACY_MANIFEST_UNPROVEN",
        "ATTRIBUTE_RELATIONSHIP_UNDECLARED",
        "ATTRIBUTE_RELATIONSHIP_UNVERIFIED",
        "ATTRIBUTE_RELATIONSHIP_BROKEN",
        "ATTRIBUTE_RELATIONSHIP_STALE",
        # Bug-7905: a VERIFIED health row older than N x the model's sweep cadence is
        # EXPIRED — it must not keep serving a relabel a stalled/failed re-check never
        # got to demote. Distinct from ..._STALE (hash/version/scope drift).
        "ATTRIBUTE_EVIDENCE_EXPIRED",
        "ATTRIBUTE_RELATIONSHIP_SCOPE_MISMATCH",
        "ATTRIBUTE_RELATIONSHIP_NULL_ENDPOINT",
        "ATTRIBUTE_PASSENGER_MISSING",
        "ATTRIBUTE_ACTIVE_REFRESH_MISMATCH",
        # Stage-4 relabel identity guards (spec §2.4 / §3.2 / §3.3): the bound
        # declaration hash disagrees with the built edge, or the covered grain
        # key / passenger does not carry exactly the relationship key / detail
        # column — both fail closed to source.
        "ATTRIBUTE_DECLARATION_HASH_MISMATCH",
        "ATTRIBUTE_LINEAGE_MISMATCH",
    }
    assert set(ALL_DERIVED_REASON_CODES) == expected
