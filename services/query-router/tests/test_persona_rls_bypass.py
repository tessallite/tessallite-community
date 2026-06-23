"""Phase 8.C.1 — persona `bypass_row_security` router behaviour.

When a persona carries ``bypass_row_security=true`` the router must:

  * skip the Phase 5.1 row-security wrap for that execution, and
  * re-enable aggregate + pocket matchers, overriding the
    Phase 5.1 rule that active RLS disables matching (per Q-X3.2=a).

Every other security control (persona allow list, audience gating,
connection binding) still applies — those are enforced before the
router is invoked, so these tests only assert router behaviour.
"""
from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from src.routing.router import route_query
from src.security import Principal

from conftest import make_aggregate, make_agg_col, make_dimension, make_measure
from test_query_flow import _bind
from test_row_security_routing import _db_returning, _role_rule

_PATCH_LOAD = "src.routing.aggregate_matcher.load_active_aggregates"

pytestmark = pytest.mark.integration


def _persona(*, bypass: bool):
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        name="bypass-test",
        bypass_row_security=bypass,
    )


async def test_bypass_persona_skips_row_security_wrap_and_enables_aggregate():
    m = make_measure("revenue")
    d = make_dimension("region_code")
    agg = make_aggregate(["region_code"], [make_agg_col(m)])

    sql = "SELECT region_code, SUM(revenue) FROM sales GROUP BY region_code"
    bq = _bind(sql, [m], [d])

    rule = _role_rule(
        "region.region_code",
        "dimension_equals('region.region_code', 'NORTH')",
        ["region_manager_north"],
    )
    principal = Principal(
        user_identity="alice@x", roles=frozenset({"region_manager_north"})
    )
    db = _db_returning([rule])
    persona = _persona(bypass=True)

    with patch(_PATCH_LOAD, new_callable=AsyncMock) as mock_load, \
            patch("src.routing.router.audit", new_callable=AsyncMock):
        mock_load.return_value = [agg]
        decision = await route_query(
            bq, db, principal=principal, persona=persona
        )

    assert decision.route_type == "aggregate"
    assert decision.aggregate_id == str(agg.id)
    assert "__ts_sec" not in decision.rewritten_query
    mock_load.assert_called()


async def test_non_bypass_persona_keeps_row_security_injection():
    m = make_measure("revenue")
    d = make_dimension("region_code")
    agg = make_aggregate(["region_code"], [make_agg_col(m)])

    sql = "SELECT region_code, SUM(revenue) FROM sales GROUP BY region_code"
    bq = _bind(sql, [m], [d])

    rule = _role_rule(
        "region.region_code",
        "dimension_equals('region.region_code', 'NORTH')",
        ["region_manager_north"],
    )
    principal = Principal(
        user_identity="alice@x", roles=frozenset({"region_manager_north"})
    )
    db = _db_returning([rule])
    persona = _persona(bypass=False)

    with patch(_PATCH_LOAD, new_callable=AsyncMock) as mock_load:
        mock_load.return_value = [agg]
        decision = await route_query(
            bq, db, principal=principal, persona=persona
        )

    # Bug-915: security predicate is injected (not wrapped), predicate must be present.
    assert decision.route_type == "source"
    assert "NORTH" in decision.rewritten_query
    mock_load.assert_not_called()


async def test_bypass_without_active_rules_is_a_noop():
    """When no RLS rules match, bypass should not change router behaviour.

    The "bypass" only kicks in when there *is* something to skip.
    """
    m = make_measure("revenue")
    d = make_dimension("region_code")
    agg = make_aggregate(["region_code"], [make_agg_col(m)])

    sql = "SELECT region_code, SUM(revenue) FROM sales GROUP BY region_code"
    bq = _bind(sql, [m], [d])

    # No rules returned from the compiler — bypass has nothing to skip.
    principal = Principal(user_identity="bob@x", roles=frozenset({"viewer"}))
    db = _db_returning([])
    persona = _persona(bypass=True)

    with patch(_PATCH_LOAD, new_callable=AsyncMock) as mock_load, \
            patch("src.routing.router.audit", new_callable=AsyncMock) as mock_audit:
        mock_load.return_value = [agg]
        decision = await route_query(
            bq, db, principal=principal, persona=persona
        )

    assert decision.route_type == "aggregate"
    assert "__ts_sec" not in decision.rewritten_query
    # No active rules to skip => no bypass audit event.
    mock_audit.assert_not_awaited()


async def test_bypass_logs_structured_audit_field(caplog):
    """Bypassed executions must emit ``persona_bypass_row_security=true``
    so operators can filter existing request logs (per Q-X2.2=c)."""
    import logging

    m = make_measure("revenue")
    d = make_dimension("region_code")
    agg = make_aggregate(["region_code"], [make_agg_col(m)])

    sql = "SELECT region_code, SUM(revenue) FROM sales GROUP BY region_code"
    bq = _bind(sql, [m], [d])

    rule = _role_rule(
        "region.region_code",
        "dimension_equals('region.region_code', 'NORTH')",
        ["region_manager_north"],
    )
    principal = Principal(
        user_identity="alice@x", roles=frozenset({"region_manager_north"})
    )
    db = _db_returning([rule])
    persona = _persona(bypass=True)

    with caplog.at_level(logging.INFO, logger="src.routing.router"), \
            patch(_PATCH_LOAD, new_callable=AsyncMock) as mock_load, \
            patch("src.routing.router.audit", new_callable=AsyncMock):
        mock_load.return_value = [agg]
        await route_query(
            bq, db, principal=principal, persona=persona
        )

    assert any(
        "persona_bypass_row_security=true" in rec.getMessage()
        for rec in caplog.records
    )


async def test_bypass_writes_platform_audit_event():
    """Bypassed RLS rules must be visible in the tenant audit trail, not
    only in service logs (F-008-24)."""
    m = make_measure("revenue")
    d = make_dimension("region_code")
    agg = make_aggregate(["region_code"], [make_agg_col(m)])

    sql = "SELECT region_code, SUM(revenue) FROM sales GROUP BY region_code"
    bq = _bind(sql, [m], [d])
    model_id = uuid.uuid4()
    bq.model.id = model_id
    bq.model.display_name = "Revenue Model"

    rule_id = uuid.uuid4()
    rule = _role_rule(
        "region.region_code",
        "dimension_equals('region.region_code', 'NORTH')",
        ["region_manager_north"],
        rule_id=rule_id,
    )
    principal = Principal(
        user_identity="alice@x", roles=frozenset({"region_manager_north"})
    )
    db = _db_returning([rule])
    persona = _persona(bypass=True)

    with patch(_PATCH_LOAD, new_callable=AsyncMock) as mock_load, \
            patch("src.routing.router.audit", new_callable=AsyncMock) as mock_audit:
        mock_load.return_value = [agg]
        await route_query(
            bq, db, principal=principal, persona=persona
        )

    mock_audit.assert_awaited_once()
    assert mock_audit.call_args.args[0] is db
    audit_kwargs = mock_audit.call_args.kwargs
    assert audit_kwargs["action"] == "query.rls_bypass"
    assert audit_kwargs["severity"] == "warn"
    assert audit_kwargs["actor_email"] == "alice@x"
    assert audit_kwargs["target_type"] == "model"
    assert audit_kwargs["target_id"] == model_id
    assert audit_kwargs["target_name"] == "Revenue Model"
    assert audit_kwargs["detail"]["persona_id"] == str(persona.id)
    assert audit_kwargs["detail"]["rules_skipped"] == [str(rule_id)]
    assert audit_kwargs["detail"]["rule_count"] == 1
    assert audit_kwargs["detail"]["protocol"] == "jdbc"
