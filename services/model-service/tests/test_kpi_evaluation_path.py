"""F-017-09: the KPI evaluate response tells the user WHICH engine produced the
value, so a Python fallback (the SQL compiler could not handle the expression)
is visible instead of a silent internal sentinel (charter C7).
"""
from __future__ import annotations

import uuid

import pytest

from shared.schemas.domains.governance_advanced import KPIEvaluateResponse
from src.api.kpis import _PYTHON_FALLBACK_REASON, _result_to_response
from src.kpi_evaluator import EvaluationResult

pytestmark = pytest.mark.unit


def test_response_defaults_to_sql_path():
    # The SQL success path builds KPIEvaluateResponse directly / via
    # _finalize_kpi_response, both of which default to the sql engine.
    resp = KPIEvaluateResponse(kpi_id=uuid.uuid4(), value=100.0)
    assert resp.evaluation_path == "sql"
    assert resp.fallback_reason is None


def test_python_fallback_converter_marks_python_path():
    result = EvaluationResult(kpi_id=uuid.uuid4(), value=42.0)
    resp = _result_to_response(result)
    assert resp.evaluation_path == "python"
    assert resp.fallback_reason == _PYTHON_FALLBACK_REASON
    # The value/visual fields still round-trip (no regression on the fallback).
    assert resp.value == 42.0


def test_converter_accepts_explicit_path_override():
    result = EvaluationResult(kpi_id=uuid.uuid4(), value=1.0)
    resp = _result_to_response(result, evaluation_path="refused", fallback_reason="guard")
    assert resp.evaluation_path == "refused"
    assert resp.fallback_reason == "guard"
