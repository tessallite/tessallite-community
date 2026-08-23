"""Bug-8453 — model-service ``/execute`` consumers must fail closed on a
row-security deny-all instead of reporting it as "no members" / "0 rows".

Covers the two model-service consumers named in the bug plus the KPI evaluator's
delegation to the shared contract:

* ``named_sets._execute_via_router``  — preview AND the member REFRESH, which
  PERSISTS members; a denial there would overwrite a good list with an empty one.
* ``pockets._route_query``            — authoring-time validate + dry-run, where
  ``COUNT(*)`` over ``WHERE 0 = 1`` returns a bare ``0`` that reads as a measured
  pocket size.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.security.execute_contract import ROW_SECURITY_DENY_ALL_RULE_ID


def _router_response(payload: dict, status: int = 200):
    resp = MagicMock()
    resp.status_code = status
    resp.json = MagicMock(return_value=payload)
    resp.text = ""
    return resp


def _patched_httpx(module_name: str, payload: dict):
    """Patch the module's ``httpx.AsyncClient`` so the POST returns *payload*."""
    client = MagicMock()
    client.post = AsyncMock(return_value=_router_response(payload))
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    factory = MagicMock(return_value=ctx)
    return patch(f"{module_name}.httpx.AsyncClient", factory)


_DENIED = {"rows": [], "columns": [], "security_rules_applied": [ROW_SECURITY_DENY_ALL_RULE_ID]}
_DENIED_WITH_COUNT_ROW = {
    # The realistic deny-all shape for the dry-run's COUNT(*): a row IS returned
    # and it contains 0. Keying on "rows is empty" would miss this entirely.
    "rows": [{"__c": 0}],
    "columns": ["__c"],
    "security_rules_applied": [ROW_SECURITY_DENY_ALL_RULE_ID],
}
_NARROWED = {"rows": [{"x": 1}], "columns": ["x"], "security_rules_applied": ["region-rule"]}
_NO_RULES_EMPTY = {"rows": [], "columns": [], "security_rules_applied": []}


class TestNamedSetsRouterClient:
    @pytest.mark.asyncio
    async def test_deny_all_raises_row_security_denied(self):
        from src.api import named_sets

        with _patched_httpx("src.api.named_sets", _DENIED):
            with pytest.raises(named_sets.RowSecurityDeniedError):
                await named_sets._execute_via_router(
                    MagicMock(), "SELECT 1", "tok",
                )

    @pytest.mark.asyncio
    async def test_denial_is_not_a_valueerror(self):
        """The preview branches' ``except Exception`` handler blames the SQL
        expression. A denial must be catchable BEFORE it, so it must not be a
        ValueError (which the HTTP-error path already raises)."""
        from src.api import named_sets

        assert not issubclass(named_sets.RowSecurityDeniedError, ValueError)

    @pytest.mark.asyncio
    async def test_genuinely_empty_result_is_returned_normally(self):
        """Zero rows with no rule applied is real data, not a denial."""
        from src.api import named_sets

        with _patched_httpx("src.api.named_sets", _NO_RULES_EMPTY):
            out = await named_sets._execute_via_router(
                MagicMock(), "SELECT 1", "tok",
            )
        assert out == _NO_RULES_EMPTY

    @pytest.mark.asyncio
    async def test_narrowing_rule_does_not_raise(self):
        """A scoped-but-valid result must flow through untouched — otherwise
        every row-restricted user would be blocked from previewing."""
        from src.api import named_sets

        with _patched_httpx("src.api.named_sets", _NARROWED):
            out = await named_sets._execute_via_router(
                MagicMock(), "SELECT 1", "tok",
            )
        assert out == _NARROWED


class TestPocketsRouterClient:
    @pytest.mark.asyncio
    async def test_deny_all_raises_row_security_denied(self):
        from src.api import pockets

        with _patched_httpx("src.api.pockets", _DENIED):
            with pytest.raises(pockets.RowSecurityDeniedError):
                await pockets._route_query(MagicMock(), "SELECT 1", "tok")

    @pytest.mark.asyncio
    async def test_count_row_of_zero_under_deny_all_still_raises(self):
        """The wrong-number case: a denial that returns COUNT(*) = 0 must not
        be reported to the modeller as a measured pocket size of zero."""
        from src.api import pockets

        with _patched_httpx("src.api.pockets", _DENIED_WITH_COUNT_ROW):
            with pytest.raises(pockets.RowSecurityDeniedError):
                await pockets._route_query(
                    MagicMock(), "SELECT COUNT(*) AS __c FROM t", "tok",
                )

    @pytest.mark.asyncio
    async def test_genuinely_empty_result_is_returned_normally(self):
        from src.api import pockets

        with _patched_httpx("src.api.pockets", _NO_RULES_EMPTY):
            out = await pockets._route_query(MagicMock(), "SELECT 1", "tok")
        assert out == _NO_RULES_EMPTY

    @pytest.mark.asyncio
    async def test_narrowing_rule_does_not_raise(self):
        from src.api import pockets

        with _patched_httpx("src.api.pockets", _NARROWED):
            out = await pockets._route_query(MagicMock(), "SELECT 1", "tok")
        assert out == _NARROWED


def test_kpi_evaluator_delegates_to_the_shared_contract():
    """Bug-8453: the Bug-8449 reference implementation must not keep a private
    copy of the sentinel or the predicate — a second definition is exactly how
    the four other consumers were able to drift in the first place."""
    from src.api import kpis

    assert kpis.ROW_SECURITY_DENY_ALL_RULE_ID == ROW_SECURITY_DENY_ALL_RULE_ID
    assert kpis.row_security_denied_all({ROW_SECURITY_DENY_ALL_RULE_ID}) is True
    assert kpis.row_security_denied_all({"other"}) is False
    assert kpis.row_security_denied_all(set()) is False

    # ... and the absorber must read the field through the shared extractor.
    sink: set[str] = set()
    kpis._absorb_security_rules(
        {"security_rules_applied": [ROW_SECURITY_DENY_ALL_RULE_ID]}, sink,
    )
    assert kpis.row_security_denied_all(sink) is True
