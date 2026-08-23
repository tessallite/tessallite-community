"""Phase 8.C.1 — persona `bypass_row_security` router behaviour.

When a persona carries ``bypass_row_security=true`` the router must:

  * skip row-security compilation and injection entirely for that
    execution (there is no "wrap" — F-007-01 replaced the outer subquery
    with per-scan WHERE injection), and
  * run the aggregate + pocket matchers UNCONDITIONALLY (per Q-X3.2=a).
    Note the framing has changed since this suite was written: active RLS no
    longer disables matching at all (Bug-7033 / Bug-8018), so what bypass
    buys is that a candidate no longer has to PROVE it carries the security
    predicate — not that matching happens where it otherwise could not.

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

    # Bug-7033: the aggregate is RLS-safe (grain includes region_code),
    # so the route is "aggregate" with the security predicate injected.
    assert decision.route_type == "aggregate"
    assert "NORTH" in decision.rewritten_query
    mock_load.assert_called()


async def test_bypass_without_active_rules_is_routing_noop_but_audited():
    """When no RLS rules match, an inert bypass flag must not change ROUTING,
    but it IS now surfaced as an INFO audit event (Bug-7046).

    The "bypass" only alters query behaviour when there is something to skip —
    so the route and injected predicate stay identical to the no-flag case.
    But a persona carrying ``bypass_row_security=true`` on a model with no
    active rules is a security-relevant (inert) configuration that deserves a
    durable, self-contained audit record so an auditor sees the flag without
    cross-referencing persona config.
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

    # Routing is unchanged by the inert flag.
    assert decision.route_type == "aggregate"
    assert "__ts_sec" not in decision.rewritten_query
    # Bug-7046: the inert bypass flag emits exactly one INFO audit event with
    # an empty rules_skipped list and an inert marker.
    mock_audit.assert_awaited_once()
    _, kwargs = mock_audit.await_args
    assert kwargs["action"] == "query.rls_bypass"
    assert kwargs["severity"] == "info"
    assert kwargs["detail"]["inert"] is True
    assert kwargs["detail"]["rules_skipped"] == []
    assert kwargs["detail"]["rule_count"] == 0


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


# ---------------------------------------------------------------------------
# Bug-8121 — the audit-evidence gap must be observable, and must never block
# the query
# ---------------------------------------------------------------------------


async def test_audit_write_failure_increments_the_evidence_gap_counter():
    """Bug-8121: an RLS-bypass audit write that fails must (a) not block the
    query and (b) increment a monitored counter, so operators can alert on a
    durable audit trail that is silently incomplete.

    Without the counter the bypass proceeds and the only evidence of the gap is
    a log line -- the security-relevant fact that a principal's RLS was bypassed
    with NO durable record becomes invisible to monitoring.
    """
    from shared.metrics import RLS_BYPASS_AUDIT_FAILURES

    m = make_measure("revenue")
    d = make_dimension("region_code")
    agg = make_aggregate(["region_code"], [make_agg_col(m)])
    sql = "SELECT region_code, SUM(revenue) FROM sales GROUP BY region_code"
    bq = _bind(sql, [m], [d])

    principal = Principal(user_identity="bob@x", roles=frozenset({"viewer"}))
    db = _db_returning([])
    persona = _persona(bypass=True)

    def _count() -> float:
        # prometheus_client exposes the current value of an unlabelled Counter
        # through its single child sample.
        return RLS_BYPASS_AUDIT_FAILURES._value.get()

    before = _count()

    with patch(_PATCH_LOAD, new_callable=AsyncMock) as mock_load, \
            patch(
                "src.routing.router.audit",
                new=AsyncMock(side_effect=RuntimeError("audit store unreachable")),
            ):
        mock_load.return_value = [agg]
        # (a) the query still routes -- the audit failure is swallowed.
        decision = await route_query(
            bq, db, principal=principal, persona=persona
        )

    assert decision.route_type == "aggregate"
    # (b) the gap is counted.
    assert _count() == before + 1, (
        "an RLS-bypass audit write failed but the evidence-gap counter did not "
        "advance; the incomplete audit trail is invisible to monitoring"
    )


async def test_audit_write_failure_never_propagates_to_the_query_path():
    """The observability code added for Bug-8121 lives INSIDE the handler whose
    contract is 'never block the query'. ``_record_rls_bypass_audit`` is awaited
    with no try/except around it, so anything that raises there fails the query
    for exactly the personas whose audit record just went missing.

    This pins that contract directly rather than through the counter, so a
    future change that reintroduces a deferred import or any other raising call
    in that handler is caught.
    """
    m = make_measure("revenue")
    d = make_dimension("region_code")
    agg = make_aggregate(["region_code"], [make_agg_col(m)])
    sql = "SELECT region_code, SUM(revenue) FROM sales GROUP BY region_code"
    bq = _bind(sql, [m], [d])

    principal = Principal(user_identity="bob@x", roles=frozenset({"viewer"}))
    db = _db_returning([])
    persona = _persona(bypass=True)

    with patch(_PATCH_LOAD, new_callable=AsyncMock) as mock_load, \
            patch(
                "src.routing.router.audit",
                new=AsyncMock(side_effect=RuntimeError("audit store unreachable")),
            ):
        mock_load.return_value = [agg]
        decision = await route_query(
            bq, db, principal=principal, persona=persona
        )

    assert decision is not None
    assert decision.route_type == "aggregate"
