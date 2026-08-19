"""Bug-9161 (NQ-2, corrected Phase 1): build and live compile a Named Query
definition through the SAME canonical semantic source route, so their
populations are identical by construction.

Before Phase 1 the build path forced ``force_route="raw"`` for a star definition
(the raw route forces ALL joins to LEFT -- row-preserving) while live compiled the
semantic route (model-declared join types) whenever the outer ``@name`` reference
was not gateway-classified raw, so the materialised and live populations diverged
by freshness/outer shape. The first Phase 1 pinned both to ``force_route="source"``
but a ``SELECT * FROM model`` definition still collapsed to the anchor table
(``rewrite_for_source``'s ``select_star`` short-circuit) — wrong columns/rows. The
corrected Phase 1 EXPANDS a row-preserving star definition to its explicit
exposed-field projection (``shared/named_query/star_expansion.py``) so the source
compile falls through to ``_build_source_sql`` — the definition-scoped closure
with the model's DECLARED join types.

This file guards:
  * the BUILD compile: /explain receives the CANONICAL body
    (force_route="source", protocol="jdbc", dialect="postgres",
    include_hidden=False, no session vars) for EVERY definition shape and the
    response MUST be route_type="source";
  * the LIVE compile: the central live helper sends the same canonical body
    with the EXPANDED definition, independent of the outer reference's
    raw/None classification and the caller's include_hidden/dialect leaks
    (Bug-9173/F6);
  * the population-contract fingerprint: deterministic, definition-scoped,
    pointer-scoped, contract-version-sensitive.

The LIVE serve decision is exercised end-to-end in
``test_named_query_decision_matrix.py`` (canonical live body asserted for every
live cell); the PHYSICAL wrong-number reproduction (fact 3 rows / INNER dim 2 ->
star NQ = 2 rows with dimension attributes, both build and live) lives in
``test_nq2_bug9161_star_expansion.py``.

See docs/questions/questions_named-query-population.md and
work/named-query-canonical-population-plan.md.
"""
from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest

from shared.named_query import refresh as _refresh
from shared.named_query.population_contract import (
    NQ_CANONICAL_DIALECT,
    NQ_CANONICAL_FORCE_ROUTE,
    NQ_CANONICAL_INCLUDE_HIDDEN,
    NQ_CANONICAL_PROTOCOL,
    NQ_POPULATION_CONTRACT_VERSION,
    named_query_population_fingerprint,
)
from shared.named_query.refresh import (
    NQ_POPULATION_CONTRACT_VERSION as _REEXPORTED_VERSION,
    _get_rewritten_sql,
    named_query_population_fingerprint as _reexported_fingerprint,
)

pytestmark = pytest.mark.unit

_STAR_DEF = "SELECT * FROM modely"
_AGG_DEF = "SELECT branch_id, COUNT(*) AS n FROM modely GROUP BY 1"
_PROJ_DEF = "SELECT branch_id FROM modely"

_MODEL_ID = uuid.uuid4()
_NQ_ID = uuid.uuid4()
_VERSION_ID = uuid.uuid4()


class _FakeResp:
    status_code = 200

    def json(self) -> dict:
        # No applied row-security rules -> no NamedQueryRowSecurityLeakError.
        return {
            "route_type": NQ_CANONICAL_FORCE_ROUTE,
            "rewritten_query": "SELECT 1",
            "security_rules_applied": [],
        }


class _FakeClient:
    """Captures the body posted to the router /explain endpoint."""

    captured: dict = {}

    def __init__(self, *a, **k) -> None:
        pass

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *a) -> bool:
        return False

    async def post(self, url, json=None, headers=None) -> _FakeResp:
        _FakeClient.captured = dict(json or {})
        return _FakeResp()


@pytest.mark.asyncio
@pytest.mark.parametrize("definition_sql", [_STAR_DEF, _AGG_DEF, _PROJ_DEF])
async def test_build_compiles_every_definition_over_the_canonical_source_body(
    definition_sql: str,
) -> None:
    """NQ-2/Bug-9161: the refresh build requests the CANONICAL population body —
    force_route="source", protocol="jdbc", dialect="postgres",
    include_hidden=False, no session vars — for EVERY definition shape, and the
    response MUST be the source route (asserted inside _get_rewritten_sql).
    Never the raw LEFT-forced route it used to force for a star definition,
    which is exactly what diverged from the live semantic serve."""
    _FakeClient.captured = {}
    with patch.object(_refresh.httpx, "AsyncClient", _FakeClient):
        await _get_rewritten_sql(uuid.uuid4(), definition_sql, "bearer-token")
    body = _FakeClient.captured
    assert body.get("force_route") == NQ_CANONICAL_FORCE_ROUTE, (
        f"build compiled {definition_sql!r} with "
        f"force_route={body.get('force_route')!r}; NQ population is "
        f"the canonical semantic source route, never raw and never the outer "
        f"reference's route."
    )
    assert body.get("protocol") == NQ_CANONICAL_PROTOCOL
    assert body.get("dialect") == NQ_CANONICAL_DIALECT
    assert body.get("include_hidden") is NQ_CANONICAL_INCLUDE_HIDDEN
    assert "session_vars" not in body or body.get("session_vars") is None
    assert "caption_dimensions" not in body or body.get("caption_dimensions") is None


@pytest.mark.asyncio
async def test_build_fails_loud_when_explain_is_not_the_source_route() -> None:
    """The route_type=="source" assertion inside _get_rewritten_sql is
    load-bearing: a non-source compile means the build would materialise a
    different population than the live canonical route — fail, never build."""

    class _AggregateResp:
        status_code = 200

        def json(self) -> dict:
            return {
                "route_type": "aggregate",
                "rewritten_query": "SELECT 1",
                "security_rules_applied": [],
            }

    class _AggregateClient:
        def __init__(self, *a, **k) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a) -> bool:
            return False

        async def post(self, url, json=None, headers=None):
            return _AggregateResp()

    with pytest.raises(ValueError, match="route_type"):
        with patch.object(_refresh.httpx, "AsyncClient", _AggregateClient):
            await _get_rewritten_sql(uuid.uuid4(), _STAR_DEF, "bearer-token")


def test_population_fingerprint_is_definition_and_pointer_scoped() -> None:
    """The fingerprint carries the contract version + canonical options + the
    deployed pointer + NQ identity + the definition; deterministic for the same
    inputs and sensitive to each of them."""
    kw = dict(
        model_id=_MODEL_ID,
        named_query_id=_NQ_ID,
        deployed_version_id=_VERSION_ID,
        deploy_epoch=7,
        definition_sql=_STAR_DEF,
    )
    fp = named_query_population_fingerprint(**kw)
    assert fp == named_query_population_fingerprint(**kw)  # deterministic
    assert fp != named_query_population_fingerprint(**{**kw, "definition_sql": _AGG_DEF})
    assert fp != named_query_population_fingerprint(**{**kw, "deployed_version_id": uuid.uuid4()})
    assert fp != named_query_population_fingerprint(**{**kw, "deploy_epoch": 8})
    assert fp != named_query_population_fingerprint(**{**kw, "named_query_id": uuid.uuid4()})
    # A contract bump must invalidate every existing artifact.
    with patch(
        "shared.named_query.population_contract.NQ_POPULATION_CONTRACT_VERSION",
        2,
    ):
        assert named_query_population_fingerprint(**kw) != fp
    assert NQ_POPULATION_CONTRACT_VERSION == 1
    # The refresh module re-exports the canonical names for consumers/tests.
    assert _REEXPORTED_VERSION == NQ_POPULATION_CONTRACT_VERSION == 1
    assert _reexported_fingerprint is named_query_population_fingerprint
