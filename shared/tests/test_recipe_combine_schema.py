"""Canonical cross-service combine-schema contract (Bug-8096 R4)."""
from __future__ import annotations

import pytest

from shared.recipes.schema import (
    COMBINE_OP_ARITY,
    CombineSchemaError,
    collect_combine_references,
    inspect_combine_tree,
)


_CONST = {"const": 1}


@pytest.mark.parametrize(
    "op_value",
    [None, False, 0, 1.5, [], {}, ""],
    ids=["null", "boolean", "integer", "number", "array", "object", "empty"],
)
def test_every_non_string_operator_type_fails_at_canonical_path(op_value):
    node = {"op": op_value, "args": [_CONST, _CONST]}
    references, issues = inspect_combine_tree(node)
    assert references == []
    assert len(issues) == 1
    assert issues[0].path == "$.combine.op"
    assert issues[0].detail == "must be a non-empty string"
    with pytest.raises(CombineSchemaError, match=r"\$\.combine\.op"):
        collect_combine_references(node)


def test_unsupported_string_operator_uses_same_canonical_path():
    with pytest.raises(CombineSchemaError, match=r"\$\.combine\.op"):
        collect_combine_references({"op": "execute", "args": [_CONST, _CONST]})


@pytest.mark.parametrize("op", sorted(COMBINE_OP_ARITY))
def test_every_canonical_operator_accepts_its_minimum_arity(op):
    minimum, _maximum = COMBINE_OP_ARITY[op]
    collect_combine_references({"op": op, "args": [_CONST] * minimum})


@pytest.mark.parametrize("op", sorted(COMBINE_OP_ARITY))
def test_every_canonical_operator_rejects_too_few_arguments(op):
    minimum, _maximum = COMBINE_OP_ARITY[op]
    with pytest.raises(CombineSchemaError, match=r"\$\.combine\.args"):
        collect_combine_references(
            {"op": op, "args": [_CONST] * (minimum - 1)}
        )


@pytest.mark.parametrize(
    "op",
    sorted(name for name, bounds in COMBINE_OP_ARITY.items() if bounds[1] is not None),
)
def test_bounded_canonical_operators_reject_too_many_arguments(op):
    _minimum, maximum = COMBINE_OP_ARITY[op]
    assert maximum is not None
    with pytest.raises(CombineSchemaError, match=r"\$\.combine\.args"):
        collect_combine_references({"op": op, "args": [_CONST] * (maximum + 1)})
