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
