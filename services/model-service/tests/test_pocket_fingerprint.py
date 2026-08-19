"""Pocket fingerprint hash function tests."""
from __future__ import annotations

import pytest

from shared.pocket.fingerprint import fingerprint_shape, predicate_set_hash

pytestmark = pytest.mark.unit


class TestFingerprintShape:
    def test_shape_hash_stable(self):
        """Same shape produces same hash regardless of input order."""
        a = fingerprint_shape(
            measures=["amount"], dimensions=["country"],
            grain=["country"], filter_cols=["status", "country"],
        )
        b = fingerprint_shape(
            measures=["amount"], dimensions=["country"],
            grain=["country"], filter_cols=["country", "status"],
        )
        assert a == b

    def test_different_measures_differ(self):
        a = fingerprint_shape(
            measures=["amount"], dimensions=[], grain=[], filter_cols=[],
        )
        b = fingerprint_shape(
            measures=["quantity"], dimensions=[], grain=[], filter_cols=[],
        )
        assert a != b

    def test_different_grain_differ(self):
        a = fingerprint_shape(
            measures=["amount"], dimensions=[], grain=["country"], filter_cols=[],
        )
        b = fingerprint_shape(
            measures=["amount"], dimensions=[], grain=["region"], filter_cols=[],
        )
        assert a != b

    def test_pre_feature_digest_is_frozen(self):
        """Byte-identity gate for derived-grain routing (spec I10).

        This asserts a FROZEN literal digest for a canonical ordinary query shape
        — computed on the pre-feature payload — so that any future change to the
        base ``fingerprint_shape`` payload (e.g. a stray key added even when
        ``expr_fingerprints`` is empty) is caught INDEPENDENTLY, not by recomputing
        the expected value from the same function. The derived-grain feature must
        never move this hash for a query that carries no derived expression."""
        digest = fingerprint_shape(
            measures=["revenue"], dimensions=["region"], grain=["region"],
            filter_cols=["country"], having_cols=[],
        )
        assert digest == (
            "93edc74e988e79e940eba7373665ca902167f894690e735725f581e6dd3f7f2d"
        )

    def test_empty_expr_fingerprints_matches_omitted(self):
        """Passing an empty ``expr_fingerprints`` must equal omitting it entirely
        — the derived_exprs payload key is only added when non-empty (spec I10)."""
        omitted = fingerprint_shape(
            measures=["revenue"], dimensions=["region"], grain=["region"],
            filter_cols=["country"],
        )
        empty = fingerprint_shape(
            measures=["revenue"], dimensions=["region"], grain=["region"],
            filter_cols=["country"], expr_fingerprints=[],
        )
        assert omitted == empty
        # A non-empty expr list MUST change the hash.
        nonempty = fingerprint_shape(
            measures=["revenue"], dimensions=["region"], grain=["region"],
            filter_cols=["country"], expr_fingerprints=["GROUP_KEY:abc"],
        )
        assert nonempty != omitted


class TestPredicateSetHash:
    def test_same_predicates_same_hash(self):
        a = predicate_set_hash([
            {"column_name": "country", "operator": "eq", "value": "GB"},
        ])
        b = predicate_set_hash([
            {"column_name": "country", "operator": "eq", "value": "GB"},
        ])
        assert a == b

    def test_different_values_differ(self):
        a = predicate_set_hash([
            {"column_name": "country", "operator": "eq", "value": "GB"},
        ])
        b = predicate_set_hash([
            {"column_name": "country", "operator": "eq", "value": "US"},
        ])
        assert a != b

    def test_empty_predicates_stable(self):
        h = predicate_set_hash([])
        assert len(h) == 64
        assert h == predicate_set_hash([])
