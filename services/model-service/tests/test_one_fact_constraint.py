"""F-013-11: the one-fact-per-model partial unique index closes the
check-then-act race. The API maps the index's IntegrityError back to the
friendly 409, not a raw 500.

The DB index itself is exercised live; here we lock the helper that recognises
the violation and the 409 mapping.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from sqlalchemy.exc import IntegrityError

from src.api.tables import _ONE_FACT_MESSAGE, _is_one_fact_violation

pytestmark = pytest.mark.unit


def _integrity(msg: str) -> IntegrityError:
    return IntegrityError("INSERT", {}, Exception(msg))


def test_recognises_one_fact_index_violation():
    exc = _integrity(
        'duplicate key value violates unique constraint '
        '"uq_model_tables_one_fact_per_model"'
    )
    assert _is_one_fact_violation(exc) is True


def test_ignores_unrelated_integrity_error():
    exc = _integrity('null value in column "alias" violates not-null constraint')
    assert _is_one_fact_violation(exc) is False


def test_one_fact_message_is_educational():
    # The 409 body must explain the rule, not just reject (UI design standard).
    assert "one fact table" in _ONE_FACT_MESSAGE
    assert "second model" in _ONE_FACT_MESSAGE
