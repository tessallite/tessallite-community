"""Serve-time overdue staleness gate (Bug-5148 / Bug-8338 / F-102-01).

The pocket + aggregate matchers previously gated only on the status label
(``fresh`` / ``active``), which is written ONLY by the refresh sweep. If the
scheduler is scaled-to-zero or behind, a fresh-labelled artifact keeps serving
outdated numbers as current — a silent wrong-numbers defect on the primary
serving path.

These known-answer tests prove the serve-time gate:

* an OVERDUE fresh/active artifact (last refresh older than cron fire + grace)
  is refused -> the matcher returns a MISS so the router falls back to source
  (correct numbers), NOT the stale artifact;
* a NOT-overdue fresh/active artifact still serves from the artifact.

Both the aggregate path and the pocket path are covered. The gate is REVERT-
catching: removing it makes the overdue case serve the stale artifact, which
these assertions reject.
"""
from __future__ import annotations

import types
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from conftest import (
    make_aggregate,
    make_agg_col,
    make_bound_query,
    make_dimension,
    make_measure,
    single_table_population_model,
)
from src.ir.logical_query import LogicalFilter, PocketMatchResult
from src.routing.aggregate_matcher import (
    AggregateSkipReason,
    find_best_aggregate,
)
from src.routing.pocket_matcher import PocketSkipReason, find_best_pocket
from shared.staleness_gate import artifact_overdue

pytestmark = pytest.mark.integration


_NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
# Daily cron at 02:00 UTC; on _NOW the previous fire is 2026-07-22 02:00.
_DAILY_CRON = "0 2 * * *"


# ---------------------------------------------------------------------------
# The pure gate (single source of truth for both matchers)
# ---------------------------------------------------------------------------

def test_overdue_when_missed_past_grace():
    # Last refresh a full day before the most recent 02:00 fire; grace 6h.
    # prev_fire = 2026-07-22 02:00; now = 12:00 -> 10h past fire > 6h grace.
    last = datetime(2026, 7, 21, 2, 5, tzinfo=timezone.utc)
    assert artifact_overdue(_DAILY_CRON, last, _NOW, 6 * 3600) is True


def test_not_overdue_within_grace_window():
    # Refreshed AFTER the most recent fire -> not due at all -> never overdue.
    last = datetime(2026, 7, 22, 2, 5, tzinfo=timezone.utc)
    assert artifact_overdue(_DAILY_CRON, last, _NOW, 6 * 3600) is False


def test_due_but_still_inside_grace_is_not_overdue():
    # Missed the 02:00 fire (last refresh predates it) but now is only 3h past
    # the fire while grace is 6h -> still serving, not yet overdue.
    now = datetime(2026, 7, 22, 5, 0, tzinfo=timezone.utc)
    last = datetime(2026, 7, 21, 2, 5, tzinfo=timezone.utc)
    assert artifact_overdue(_DAILY_CRON, last, now, 6 * 3600) is False


def test_no_cron_is_never_overdue():
    last = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert artifact_overdue(None, last, _NOW, 6 * 3600) is False
    assert artifact_overdue("", last, _NOW, 6 * 3600) is False


def test_no_last_refresh_is_never_overdue():
    assert artifact_overdue(_DAILY_CRON, None, _NOW, 6 * 3600) is False


def test_invalid_cron_fails_safe_not_overdue():
    last = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert artifact_overdue("0 99 * * *", last, _NOW, 6 * 3600) is False


# --- Short-cadence + multi-missed-fire: the anchor-at-first-missed-fire class
# (a prev-fire anchor is permanently inert when the cron period <= grace). ---

def test_short_cadence_days_stale_is_overdue():
    """HOURLY cron, scheduler dead 10 days, grace 6h. A prev-fire anchor would
    NEVER fire here (now - prev_fire < 1h < grace); the first-missed-fire anchor
    correctly reports overdue."""
    last = _NOW - timedelta(days=10)
    assert artifact_overdue("0 * * * *", last, _NOW, 6 * 3600) is True


def test_four_hourly_cadence_month_stale_is_overdue():
    last = _NOW - timedelta(days=30)
    assert artifact_overdue("0 */4 * * *", last, _NOW, 6 * 3600) is True


def test_daily_multi_missed_fires_stays_overdue_monotonic():
    """DAILY cron 00:00, 3 days stale, now = a fire + 1h. The first missed fire
    is days ago, so past grace -> overdue; a prev-fire anchor would oscillate
    back to 'serve' in the first grace window after each fire."""
    now = datetime(2026, 7, 22, 1, 0, tzinfo=timezone.utc)  # 1h past 00:00 fire
    last = now - timedelta(days=3)
    assert artifact_overdue("0 0 * * *", last, now, 6 * 3600) is True


def test_grace_zero_refuses_the_instant_a_fire_is_missed():
    """grace=0 (strictest) must be honored: overdue as soon as now passes the
    first missed fire."""
    # Daily 02:00; last refresh before it; now just past 02:00.
    now = datetime(2026, 7, 22, 2, 1, tzinfo=timezone.utc)
    last = datetime(2026, 7, 21, 2, 5, tzinfo=timezone.utc)
    assert artifact_overdue("0 2 * * *", last, now, 0) is True
    # ...but still not overdue before the fire is even due.
    last_after = datetime(2026, 7, 22, 2, 0, tzinfo=timezone.utc)
    assert artifact_overdue("0 2 * * *", last_after, now, 0) is False


# ---------------------------------------------------------------------------
# Aggregate matcher path
# ---------------------------------------------------------------------------

_AGG_PATCH = "src.routing.aggregate_matcher.load_active_aggregates"
_AGG_INACTIVE_PATCH = "src.routing.aggregate_matcher.load_inactive_aggregates"


def _agg_with_policy(agg, cron, *, is_enabled=True):
    agg.refresh_policy = types.SimpleNamespace(
        cron_expression=cron, is_enabled=is_enabled
    )
    return agg


async def _run_agg(bq, agg, *, grace_hours=6):
    with patch(_AGG_PATCH, new_callable=AsyncMock) as mock_load, patch(
        _AGG_INACTIVE_PATCH, new_callable=AsyncMock
    ) as mock_inactive, patch(
        "src.routing.aggregate_matcher.datetime"
    ) as mock_dt, patch(
        # Bug-8528: the matcher no longer reads the grace key itself — it calls
        # the SHARED ``shared.staleness_gate.resolve_overdue_grace_seconds``,
        # so the config read to intercept is the one inside that resolver.
        # Patching the matcher's own (now removed) import would silently stop
        # controlling the grace window.
        "shared.config.bootstrap.system_snapshot_get"
    ) as mock_snap:
        mock_load.return_value = [agg]
        mock_inactive.return_value = []
        mock_dt.now.return_value = _NOW
        mock_snap.return_value = grace_hours
        return await find_best_aggregate(bq, AsyncMock())


async def test_aggregate_overdue_refused_falls_to_source():
    """An active aggregate refreshed a day ago on a DAILY cron (missed past the
    6h grace) must NOT serve — the matcher returns a miss (no aggregate) with the
    STALE_OVERDUE reason, so the router falls back to source."""
    m = make_measure("revenue")
    agg = make_aggregate(
        ["country"], [make_agg_col(m)],
        # last_refreshed_at = _NOW - 34h -> before the 02:00 fire on _NOW's day.
        age_hours=34,
    )
    # Pin last_refreshed relative to the frozen _NOW rather than wall-clock.
    agg.last_refreshed_at = _NOW - timedelta(hours=34)
    _agg_with_policy(agg, _DAILY_CRON)
    bq = make_bound_query([make_dimension("country")], [m])

    result = await _run_agg(bq, agg)

    assert result.aggregate is None
    assert AggregateSkipReason.STALE_OVERDUE in (result.skip_reasons or [])


async def test_aggregate_fresh_within_grace_still_serves():
    """A just-refreshed active aggregate (after the most recent fire) still
    serves from the aggregate — the gate must not refuse a genuinely-fresh one."""
    m = make_measure("revenue")
    agg = make_aggregate(["country"], [make_agg_col(m)])
    # Refreshed AFTER the most recent 02:00 fire.
    agg.last_refreshed_at = _NOW - timedelta(hours=2)
    _agg_with_policy(agg, _DAILY_CRON)
    bq = make_bound_query([make_dimension("country")], [m])

    result = await _run_agg(bq, agg)

    assert result.aggregate is agg


async def test_aggregate_manual_policy_no_cron_not_refused():
    """A manual-policy aggregate (no cron cadence) is never overdue — it keeps
    its status-gated behaviour even if last refresh is ancient."""
    m = make_measure("revenue")
    agg = make_aggregate(["country"], [make_agg_col(m)])
    agg.last_refreshed_at = _NOW - timedelta(days=30)
    _agg_with_policy(agg, None)  # manual: no cron
    bq = make_bound_query([make_dimension("country")], [m])

    result = await _run_agg(bq, agg)

    assert result.aggregate is agg


async def test_aggregate_hourly_cron_days_stale_refused():
    """Short-cadence guard for the anchor defect: an HOURLY-cron aggregate stale
    for 5 days must be refused (a prev-fire anchor would never fire here)."""
    m = make_measure("revenue")
    agg = make_aggregate(["country"], [make_agg_col(m)])
    agg.last_refreshed_at = _NOW - timedelta(days=5)
    _agg_with_policy(agg, "0 * * * *")  # hourly
    bq = make_bound_query([make_dimension("country")], [m])

    result = await _run_agg(bq, agg)

    assert result.aggregate is None
    assert AggregateSkipReason.STALE_OVERDUE in (result.skip_reasons or [])


async def test_aggregate_disabled_policy_not_refused():
    """A disabled refresh policy has no active cadence, so the gate must not
    refuse the aggregate even if its last refresh is ancient."""
    m = make_measure("revenue")
    agg = make_aggregate(["country"], [make_agg_col(m)])
    agg.last_refreshed_at = _NOW - timedelta(days=30)
    _agg_with_policy(agg, "0 * * * *", is_enabled=False)
    bq = make_bound_query([make_dimension("country")], [m])

    result = await _run_agg(bq, agg)

    assert result.aggregate is agg


# ---------------------------------------------------------------------------
# Pocket matcher path
# ---------------------------------------------------------------------------

def _pocket_result(items):
    return types.SimpleNamespace(
        scalars=lambda: types.SimpleNamespace(all=lambda: items),
    )


def _make_pocket(cron, last_refresh, *, is_enabled=True, fp="fp-ovd"):
    return types.SimpleNamespace(
        id="pocket-ovd",
        model_id="model-1",
        status="fresh",
        built_for_version_id="v1",
        built_for_epoch=0,
        query_fingerprint=fp,
        last_refresh_at=last_refresh,
        persona_id=None,
        predicates=[
            types.SimpleNamespace(
                column_name="tenant_id", operator="eq", value_json={"value": 12}
            ),
        ],
        refresh_policy_row=types.SimpleNamespace(
            cron_expression=cron, is_enabled=is_enabled
        ),
    )


async def _run_pocket(bq, pocket, *, grace_hours=6):
    # Bug-8528: pocket.enabled / require_tenant_filter / tenant_scope_from_context
    # are still read by the matcher itself, but the GRACE window now comes from
    # the shared resolver, so it has to be intercepted at the shared config read.
    with single_table_population_model(), patch(
        "shared.config.bootstrap.system_snapshot_get",
        side_effect=lambda key: grace_hours,
    ), patch("src.routing.pocket_matcher.system_snapshot_get") as snap, patch(
        "src.routing.pocket_matcher.get_setting", new_callable=AsyncMock
    ) as get_setting, patch(
        "src.routing.pocket_matcher.datetime"
    ) as mock_dt:
        snap.side_effect = lambda key: {
            "pocket.enabled": True,
            "pocket.require_tenant_filter": True,
            "pocket.tenant_scope_from_context": True,
        }.get(key)
        get_setting.return_value = True  # model_enabled
        mock_dt.now.return_value = _NOW
        db = AsyncMock()
        db.execute = AsyncMock(return_value=_pocket_result([pocket]))
        return await find_best_pocket(bq, db)


def _pocket_bq(fp="fp-ovd"):
    bq = make_bound_query(
        [make_dimension("tenant_id")],
        [make_measure("amount")],
        filters=[LogicalFilter("tenant_id", "eq", 12)],
        raw_sql="SELECT amount FROM sales WHERE tenant_id = 12",
    )
    bq.logical_query.query_fingerprint = fp
    return bq


async def test_pocket_overdue_refused_falls_to_source():
    """A fresh-labelled pocket refreshed 34h ago on a DAILY cron (missed past the
    6h grace) must NOT serve — the matcher returns a miss with STALE_OVERDUE."""
    pocket = _make_pocket(_DAILY_CRON, _NOW - timedelta(hours=34))
    result = await _run_pocket(_pocket_bq(), pocket)

    assert isinstance(result, PocketMatchResult)
    assert result.pocket is None
    assert result.skipped_reason == PocketSkipReason.STALE_OVERDUE


async def test_pocket_fresh_within_grace_still_serves():
    """A pocket refreshed after the most recent fire still serves."""
    pocket = _make_pocket(_DAILY_CRON, _NOW - timedelta(hours=2))
    result = await _run_pocket(_pocket_bq(), pocket)

    assert result.pocket is pocket
    assert result.skipped_reason is None


async def test_pocket_manual_policy_no_cron_not_refused():
    """A pocket whose policy row carries no cron (manual/event) is never overdue."""
    pocket = _make_pocket(None, _NOW - timedelta(days=30))
    result = await _run_pocket(_pocket_bq(), pocket)

    assert result.pocket is pocket


async def test_pocket_hourly_cron_days_stale_refused():
    """Short-cadence guard: an HOURLY-cron pocket stale for 5 days is refused."""
    pocket = _make_pocket("0 * * * *", _NOW - timedelta(days=5))
    result = await _run_pocket(_pocket_bq(), pocket)

    assert result.pocket is None
    assert result.skipped_reason == PocketSkipReason.STALE_OVERDUE


async def test_pocket_disabled_policy_not_refused():
    """A disabled pocket refresh policy row has no active cadence -> not overdue."""
    pocket = _make_pocket("0 * * * *", _NOW - timedelta(days=30), is_enabled=False)
    result = await _run_pocket(_pocket_bq(), pocket)

    assert result.pocket is pocket


# ---------------------------------------------------------------------------
# Grace-window parity (Bug-8528): the freshness PRODUCER and both matcher gates
# must resolve the overdue grace from ONE shared resolver.
#
# The grace-window resolution used to be inlined at three sites -- both matchers
# and Bug-8365's `_result_freshness`. All three were at parity, but nothing
# structural held them there: a key or unit edited at one site would label a
# served result with a verdict computed from a threshold DIFFERENT to the gate
# that admitted the artifact, so the analyst-facing "as of / stale" chip would
# contradict the gate that let the artifact serve -- a trust defect on the
# primary serving path.
#
# Bug-8528 extracted `shared.staleness_gate.resolve_overdue_grace_seconds`, the
# natural home next to the `artifact_overdue` predicate the same module already
# owns. These tests pin (a) that all three sites call it, (b) that NO site has
# re-inlined the idiom (which is how a 4th copy would appear), and (c) the
# resolver's own key / units / fail-safe contract.
#
# Test escape: no test asserted the producer and the matchers agree.
# Guard: this section. Tier: T1 producer/consumer contract.
# ---------------------------------------------------------------------------

import inspect  # noqa: E402
import re  # noqa: E402

from shared.config.registry import all_for_level  # noqa: E402
from shared.staleness_gate import (  # noqa: E402
    OVERDUE_GRACE_KINDS,
    overdue_grace_config_key,
    resolve_overdue_grace_seconds,
)
from src.api import routes as _routes  # noqa: E402
from src.routing import aggregate_matcher as _agg_matcher  # noqa: E402
from src.routing import pocket_matcher as _pkt_matcher  # noqa: E402

_GRACE_SITES = [
    ("aggregate", _agg_matcher),
    ("pocket", _pkt_matcher),
]

# Every module that must resolve the grace window, and the kind(s) it resolves.
_ALL_GRACE_CONSUMERS = [
    (_agg_matcher, ["aggregate"]),
    (_pkt_matcher, ["pocket"]),
    (_routes, ["aggregate", "pocket"]),
]

# The idiom that used to be copy-pasted. Its reappearance ANYWHERE is a new
# divergent copy, which is exactly the class of drift Bug-8528 closed.
_INLINED_IDIOM = re.compile(
    r"float\(\s*system_snapshot_get\(\s*[\"']\w+\.serve_overdue_grace_hours"
)


@pytest.mark.parametrize("module,kinds", _ALL_GRACE_CONSUMERS)
def test_every_consumer_calls_the_shared_resolver(module, kinds):
    source = re.sub(r"\s+", " ", inspect.getsource(module))
    for kind in kinds:
        assert f'resolve_overdue_grace_seconds("{kind}")' in source, (
            f"{module.__name__} no longer resolves the {kind!r} overdue grace "
            "through shared.staleness_gate.resolve_overdue_grace_seconds; a "
            "local copy can drift from the gate that admitted the artifact"
        )


@pytest.mark.parametrize("module,_kinds", _ALL_GRACE_CONSUMERS)
def test_no_consumer_re_inlines_the_grace_idiom(module, _kinds):
    """A 4th copy of the resolution idiom is the drift this bug closed."""
    source = inspect.getsource(module)
    assert not _INLINED_IDIOM.search(source), (
        f"{module.__name__} re-inlines the serve_overdue_grace_hours "
        "resolution instead of calling the shared resolver"
    )


@pytest.mark.parametrize("kind,_matcher", _GRACE_SITES)
def test_resolver_converts_hours_to_seconds_from_the_registered_key(kind, _matcher):
    config_key = overdue_grace_config_key(kind)
    assert config_key == f"{kind}.serve_overdue_grace_hours"
    with patch(
        "shared.config.bootstrap.system_snapshot_get", return_value=2
    ) as snap:
        assert resolve_overdue_grace_seconds(kind) == 2 * 3600.0
    assert snap.call_args.args == (config_key,)


@pytest.mark.parametrize("kind,_matcher", _GRACE_SITES)
def test_grace_keys_are_registered_settings(kind, _matcher):
    """A rename in the config registry must not leave a site silently reading a
    missing key (which disables the gate/verdict)."""
    config_key = overdue_grace_config_key(kind)
    assert any(
        s.key == config_key for s in all_for_level("system")
    ), config_key


@pytest.mark.parametrize("kind,_matcher", _GRACE_SITES)
def test_grace_disabled_only_on_an_uncoercible_value(kind, _matcher):
    """A numeric grace (including the strictest 0) keeps the gate/verdict live;
    only an uncoercible value disables it. Treating 0 as "disabled" would report
    an overdue artifact as fresh."""
    with patch("shared.config.bootstrap.system_snapshot_get", return_value=0):
        assert resolve_overdue_grace_seconds(kind) == 0.0
    with patch("shared.config.bootstrap.system_snapshot_get", return_value=None):
        assert resolve_overdue_grace_seconds(kind) is None
    with patch(
        "shared.config.bootstrap.system_snapshot_get", return_value="not-a-number"
    ):
        assert resolve_overdue_grace_seconds(kind) is None


def test_unknown_artifact_kind_fails_closed():
    """Interpolating an unvalidated kind would silently resolve a key that is
    not in the registry, which DISABLES the gate rather than erroring."""
    assert OVERDUE_GRACE_KINDS == frozenset(
        {"aggregate", "pocket", "named_query"}
    )
    with pytest.raises(ValueError, match="Unknown artifact kind"):
        resolve_overdue_grace_seconds("agregate")
