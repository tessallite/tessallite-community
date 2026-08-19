"""Producer/consumer enum alignment: Pydantic wire contract vs engine (spec §13.5).

The engine (shared.model_dependency.types) is the producer of severity/effect/
policy/operation enums; the Pydantic schema is a derived consumer. This test
fails if they drift, catching a silently missing literal before it reaches the
frontend type.
"""

from __future__ import annotations

import typing

from shared.model_dependency import types as eng
from shared.schemas.domains import model_impact as wire


def _literal_values(tp) -> set[str]:
    return set(typing.get_args(tp))


def test_severity_literals_match_engine():
    engine_severity = set(typing.get_args(eng.Severity))
    assert _literal_values(wire.SeverityStr) == engine_severity


def test_effect_literals_match_engine():
    assert _literal_values(wire.EffectStr) == set(typing.get_args(eng.Effect))


def test_delete_policy_literals_match_engine():
    assert _literal_values(wire.DeletePolicyStr) == set(typing.get_args(eng.DeletePolicy))


def test_operation_literals_match_engine():
    assert _literal_values(wire.OperationStr) == set(typing.get_args(eng.Operation))


def test_response_model_has_all_summary_fields():
    """Every ImpactSummary dataclass field has a wire counterpart (spec §9.5)."""
    import dataclasses

    engine_fields = {f.name for f in dataclasses.fields(eng.ImpactSummary)}
    wire_fields = set(wire.ImpactSummaryModel.model_fields)
    assert engine_fields <= wire_fields, engine_fields - wire_fields
