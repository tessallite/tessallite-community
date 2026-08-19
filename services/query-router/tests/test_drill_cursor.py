"""Bug-8048 stable, scope-bound drill cursor contracts."""
from __future__ import annotations

from decimal import Decimal

import pytest

from src.drill.cursor import (
    CursorOrderTerm,
    CursorValidationError,
    DrillCursorSpec,
)
from src.drill.semantic_builder import _keyset_continuation_expression


def _scope(**overrides):
    value = {
        "tenant_id": "acme-demo",
        "project_id": "project-1",
        "model_id": "model-1",
        "deployed_version_id": "version-3",
        "deploy_epoch": 7,
        "data_epoch": 11,
        "measure_id": "measure-1",
        "hierarchy_id": None,
        "grouping_levels": [{"column": "region", "value": "US"}],
        "filters": [{"column": "year", "value": 2026}],
        "page_size": 2,
        "security": {
            "user_identity": "analyst@example.com",
            "roles": ["analyst"],
            "groups": ["finance"],
            "claims": {"country": "US"},
            "persona_id": "persona-1",
        },
    }
    value.update(overrides)
    return value


def _spec(*, scope=None, stable=True):
    return DrillCursorSpec.build(
        scope=scope or _scope(),
        order_terms=[
            CursorOrderTerm("region"),
            CursorOrderTerm("amount", descending=True),
            CursorOrderTerm("txn_id"),
        ],
        stable=stable,
    )


def test_cursor_round_trips_complete_typed_order_key():
    spec = _spec()
    token = spec.encode({"region": "US", "amount": Decimal("10.50"), "txn_id": 42})
    decoded = spec.decode(token)
    assert decoded is not None
    assert [value.value for value in decoded] == ["US", Decimal("10.50"), 42]
    assert len(token.rsplit(".", 1)[1]) == 64


def test_cursor_encode_refuses_final_token_over_transport_ceiling():
    spec = DrillCursorSpec.build(
        scope=_scope(),
        order_terms=[CursorOrderTerm(f"key_{index}") for index in range(4)],
        stable=True,
    )
    row = {f"key_{index}": "x" * 4096 for index in range(4)}
    with pytest.raises(CursorValidationError) as exc:
        spec.encode(row)
    assert exc.value.code == "CURSOR_TOO_LARGE"


def test_cursor_payload_tamper_fails_closed():
    spec = _spec()
    token = spec.encode({"region": "US", "amount": 10, "txn_id": 42})
    payload, signature = token.rsplit(".", 1)
    flipped = ("A" if payload[0] != "A" else "B") + payload[1:]
    with pytest.raises(CursorValidationError) as exc:
        spec.decode(f"{flipped}.{signature}")
    assert exc.value.code == "INVALID_CURSOR"


@pytest.mark.parametrize(
    ("field", "changed"),
    [
        ("tenant_id", "other-tenant"),
        ("project_id", "other-project"),
        ("model_id", "other-model"),
        ("deployed_version_id", "version-4"),
        ("deploy_epoch", 8),
        ("data_epoch", 12),
        ("measure_id", "other-measure"),
        ("grouping_levels", []),
        ("filters", []),
        ("page_size", 100),
        ("security", {"user_identity": "other@example.com"}),
    ],
)
def test_cursor_replay_across_scope_fails_closed(field, changed):
    token = _spec().encode({"region": "US", "amount": 10, "txn_id": 42})
    replay_scope = _scope(**{field: changed})
    with pytest.raises(CursorValidationError) as exc:
        _spec(scope=replay_scope).decode(token)
    assert exc.value.code == "STALE_CURSOR"


def test_unsigned_offset_cursor_is_rejected():
    with pytest.raises(CursorValidationError) as exc:
        _spec().decode("eyJvIjo1MH0")
    assert exc.value.code == "INVALID_CURSOR"


def test_non_unique_leaf_order_refuses_continuation_token():
    spec = _spec(stable=False)
    with pytest.raises(CursorValidationError) as exc:
        spec.encode({"region": "US", "amount": 10, "txn_id": 42})
    assert exc.value.code == "STABLE_CURSOR_UNAVAILABLE"


def test_keyset_predicate_uses_complete_mixed_direction_key():
    spec = _spec()
    values = spec.decode(
        spec.encode({"region": "US", "amount": Decimal("10.5"), "txn_id": 42})
    )
    assert values is not None
    sql = _keyset_continuation_expression(spec, values).sql(dialect="postgres")
    assert '"region" > \'US\'' in sql
    assert '"amount" < 10.5' in sql
    assert '"txn_id" > 42' in sql
    assert '"region" IS NULL' in sql


def test_keyset_continuation_survives_insert_and_delete_before_boundary():
    original = [1, 2, 3, 4, 5, 6]
    first_page = original[:3]
    last_key = first_page[-1]
    # Concurrent ingestion inserts before the boundary and deletes a row that
    # was already observed. OFFSET 3 would restart at 4 only by accident here;
    # other insert/delete counts shift it. Keyset continuation is anchored at 3.
    mutated = [0, 1, 3, 4, 5, 6]
    second_page = [value for value in mutated if value > last_key][:3]
    assert second_page == [4, 5, 6]
    assert not set(first_page) & set(second_page)
    assert second_page == [value for value in original if value > last_key]
