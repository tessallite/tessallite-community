"""Bug-9834 — a watchdog kill must leave a durable, machine-readable trace.

Bug-8533's recovery is process recycling: three failed JDBC liveness probes,
then exit, then the container restart policy recreates the gateway. That is the
right recovery boundary and it is live-proven. What it leaves behind is an
OBSERVABILITY gap, and the gap is what made "do not investigate further unless
it recurs" unsafe: a wedge recurring hourly self-heals every time and looks
exactly like one that never came back. The only people who notice are users
losing in-flight queries.

An in-process counter cannot carry that signal — it dies with the process it is
recording. So the durable carrier is a structured event emitted and FLUSHED
before the exit, carrying the state the exit destroys: how long the process had
been up, how many sessions it was holding, and how many file descriptors were
open (the last is the difference between a resource-exhaustion wedge and an
idle one, and it is unrecoverable afterwards).

Contract under test:
- the event is emitted before the process exit, exactly once, on the strike;
- it is machine-readable and carries the full diagnostic payload;
- collecting the payload can never prevent the exit;
- probe failures are counted during the strike window, before any exit.
"""
from __future__ import annotations

import json
import logging

import pytest

from src.jdbc.liveness_watchdog import (
    WATCHDOG_EXIT_CODE,
    run_jdbc_liveness_watchdog,
)


async def _alive(*_a, **_kw):
    return True, "answered"


async def _dead(*_a, **_kw):
    return False, "no response within 2.0s"


def _events(caplog):
    out = []
    for rec in caplog.records:
        msg = rec.getMessage()
        if msg.startswith("jdbc_watchdog_exit "):
            out.append(json.loads(msg.split(" ", 1)[1]))
    return out


@pytest.mark.asyncio
async def test_bug9834_exit_emits_one_structured_event(caplog):
    caplog.set_level(logging.CRITICAL)
    exits: list[int] = []

    async def probe(*_a, **_kw):
        # Alive once so the watchdog arms, then dead for the strike window.
        probe.calls += 1
        return (True, "answered") if probe.calls == 1 else (False, "silent")
    probe.calls = 0

    await run_jdbc_liveness_watchdog(
        "127.0.0.1", 5433, interval_seconds=0, failure_limit=2,
        probe_timeout_seconds=1, probe=probe, on_exit=exits.append,
    )

    assert exits == [WATCHDOG_EXIT_CODE]
    events = _events(caplog)
    assert len(events) == 1, "the kill must leave exactly one machine-readable record"
    ev = events[0]
    assert ev["event"] == "jdbc_watchdog_exit"
    assert ev["exit_code"] == WATCHDOG_EXIT_CODE
    assert ev["consecutive_failures"] == 2
    assert ev["port"] == 5433
    assert ev["last_probe_error"] == "silent"
    # The payload the exit would otherwise destroy.
    assert "process_uptime_seconds" in ev
    assert "active_jdbc_sessions" in ev
    assert "open_file_descriptors" in ev


@pytest.mark.asyncio
async def test_bug9834_no_event_while_the_listener_answers(caplog):
    """A healthy gateway must never emit the kill record."""
    caplog.set_level(logging.CRITICAL)
    exits: list[int] = []

    async def probe(*_a, **_kw):
        probe.calls += 1
        if probe.calls > 3:
            raise asyncio.CancelledError
        return True, "answered"
    probe.calls = 0

    import asyncio
    with pytest.raises(asyncio.CancelledError):
        await run_jdbc_liveness_watchdog(
            "127.0.0.1", 5433, interval_seconds=0, failure_limit=2,
            probe_timeout_seconds=1, probe=probe, on_exit=exits.append,
        )
    assert exits == []
    assert _events(caplog) == []


@pytest.mark.asyncio
async def test_bug9834_payload_collection_never_blocks_the_exit(monkeypatch, caplog):
    """Diagnostics are best-effort: the exit is the one thing that must happen."""
    caplog.set_level(logging.CRITICAL)
    import src.jdbc.liveness_watchdog as wd

    monkeypatch.setattr(
        wd, "_open_file_descriptors",
        lambda: (_ for _ in ()).throw(RuntimeError("no /proc")),
    )
    exits: list[int] = []

    async def probe(*_a, **_kw):
        probe.calls += 1
        return (True, "ok") if probe.calls == 1 else (False, "silent")
    probe.calls = 0

    await run_jdbc_liveness_watchdog(
        "127.0.0.1", 5433, interval_seconds=0, failure_limit=2,
        probe_timeout_seconds=1, probe=probe, on_exit=exits.append,
    )

    # Losing the record is bad; losing the recovery is worse.
    assert exits == [WATCHDOG_EXIT_CODE], (
        "a raising diagnostic collector prevented the wedged gateway from "
        "being recycled"
    )


# ---------------------------------------------------------------------------
# Review F3 — the recurrence signal must outlive the process that records it.
#
# The watchdog increments the counter and exits immediately. With a 15s scrape
# interval Prometheus almost never observes the incremented value, and the
# replacement process starts at zero — so `increase()` over that series could
# stay flat through any number of wedges and the recurrence alert would never
# fire. The tally is persisted before the exit and seeded back at start-up, so
# the series steps across restarts instead of resetting.
# ---------------------------------------------------------------------------


def test_bug9834_f3_tally_survives_the_exit(tmp_path, monkeypatch):
    """A restart must not lose the count — that loss is the detection gap."""
    from src.jdbc import liveness_watchdog as wd

    monkeypatch.setenv(
        "TESSALLITE_JDBC_WATCHDOG_STATE", str(tmp_path / "exits")
    )
    assert wd.read_persisted_exit_count() == 0
    assert wd.record_persisted_exit() == 1
    # A NEW process reads what the dead one wrote.
    assert wd.read_persisted_exit_count() == 1
    assert wd.record_persisted_exit() == 2
    assert wd.read_persisted_exit_count() == 2


def test_bug9834_f3_startup_seeding_makes_the_series_monotonic(
    tmp_path, monkeypatch
):
    """Seeding is what stops the counter sawtoothing back to zero."""
    from src.jdbc import liveness_watchdog as wd

    monkeypatch.setenv(
        "TESSALLITE_JDBC_WATCHDOG_STATE", str(tmp_path / "exits")
    )
    wd.record_persisted_exit()
    wd.record_persisted_exit()
    assert wd.seed_exit_counter_from_disk() == 2


def test_bug9834_f3_an_unwritable_tally_never_blocks_recovery(
    tmp_path, monkeypatch
):
    """Losing the record must not cost the restart. Recovery outranks telemetry."""
    from src.jdbc import liveness_watchdog as wd

    unwritable = tmp_path / "no-such-dir" / "exits"
    monkeypatch.setenv("TESSALLITE_JDBC_WATCHDOG_STATE", str(unwritable))
    # Returns a value and does not raise, so the exit path continues.
    assert wd.record_persisted_exit() == 1
    assert wd.read_persisted_exit_count() == 0


def test_bug9834_f3_corrupt_tally_degrades_to_zero(tmp_path, monkeypatch):
    from src.jdbc import liveness_watchdog as wd

    state = tmp_path / "exits"
    state.write_text("not-a-number")
    monkeypatch.setenv("TESSALLITE_JDBC_WATCHDOG_STATE", str(state))
    assert wd.read_persisted_exit_count() == 0


# ---------------------------------------------------------------------------
# Review finding 5 — monitor JDBC DIRECTLY.
#
# Every signal above is derived from the watchdog, and the watchdog only runs
# when the listener started. The one failure it therefore cannot describe is the
# listener never binding at all: the HTTP surface stays healthy, ``up`` stays 1,
# no probe ever fails, no exit is recorded, and JDBC is dead in silence.
# ---------------------------------------------------------------------------

import re
from pathlib import Path

import yaml

_ALERT_RULES = (
    Path(__file__).resolve().parents[4] / "monitoring" / "alert_rules.yml"
)


def _exported_metric_names() -> set[str]:
    """Every metric name the process actually publishes."""
    from prometheus_client import REGISTRY

    # Every module that registers platform metrics, not just the gateway's own.
    #
    # monitoring/alert_rules.yml covers the whole platform, so the set this is
    # compared against has to be the union of what all services publish. With
    # only shared.metrics imported, the system-log gauges — which model-service
    # exports, verified live on its /metrics endpoint — looked unexported, and
    # the guard failed on seven metrics that are genuinely published.
    #
    # A module that registers metrics and is missing here reads as a dead alert
    # when it is nothing of the kind, so add new ones as they appear.
    import shared.metrics  # noqa: F401  (registers the metrics)
    import shared.system_logs.metrics  # noqa: F401  (registers the system-log metrics)

    names: set[str] = set()
    for family in REGISTRY.collect():
        names.add(family.name)
        for sample in family.samples:
            names.add(sample.name)
            # A Counter is declared ``x_total`` but its family name is ``x``;
            # an alert may legitimately reference either spelling.
            if sample.name.endswith("_total"):
                names.add(sample.name[: -len("_total")])
    return names


def test_bug9834_the_direct_jdbc_gauges_are_exported():
    """The listener and watchdog states are published, not merely logged."""
    exported = _exported_metric_names()
    assert "tessallite_gateway_jdbc_listener_up" in exported
    assert "tessallite_gateway_jdbc_watchdog_up" in exported


def test_bug9834_every_alerted_metric_is_actually_exported():
    """An alert on a metric nobody publishes is a silently dead alert.

    That is the same defect class as the finding this group exists to close:
    monitoring that looks present and reports nothing. A renamed or mistyped
    metric would leave the rule permanently unfired, and nothing else in the
    system would notice.
    """
    assert _ALERT_RULES.is_file(), f"alert rules not found at {_ALERT_RULES}"
    text = _ALERT_RULES.read_text(encoding="utf-8")
    # Rule expressions only; the prose in comments and annotations names
    # metrics too and must not be treated as a reference.
    rules = yaml.safe_load(text)
    expressions = " ".join(
        str(rule.get("expr", ""))
        for group in rules["groups"]
        for rule in group["rules"]
    )
    referenced = set(re.findall(r"\btessallite_[a-z0-9_]+", expressions))
    assert referenced, "no Tessallite metric is referenced by any alert"

    exported = _exported_metric_names()
    missing = sorted(referenced - exported)
    assert not missing, (
        f"alert rules reference metrics that nothing exports: {missing}"
    )


def test_bug9834_the_listener_failure_case_has_an_alert():
    """A listener that never bound must be alertable on its own.

    Before this, every rule keyed off the watchdog or off ``up{job="gateway"}``.
    Neither can see a gateway that is serving HTTP perfectly with no JDBC accept
    loop behind it.
    """
    rules = yaml.safe_load(_ALERT_RULES.read_text(encoding="utf-8"))
    by_name = {
        rule["alert"]: rule
        for group in rules["groups"]
        for rule in group["rules"]
        if "alert" in rule
    }
    listener_down = by_name.get("GatewayJdbcListenerDown")
    assert listener_down is not None, "no alert covers a listener that never bound"
    assert "tessallite_gateway_jdbc_listener_up" in listener_down["expr"]
    assert "up{job=" not in listener_down["expr"], (
        "the scrape target is not the accept loop; this alert must not depend "
        "on it"
    )


def test_bug9834_an_absent_health_signal_is_itself_alerted():
    """Every JDBC availability rule tests for zero, so a missing series would
    satisfy none of them and report nothing at all."""
    rules = yaml.safe_load(_ALERT_RULES.read_text(encoding="utf-8"))
    exprs = [
        str(rule.get("expr", ""))
        for group in rules["groups"]
        for rule in group["rules"]
    ]
    assert any(
        "absent(tessallite_gateway_jdbc_listener_up)" in e for e in exprs
    ), "a gateway that stopped exporting JDBC health would alert on nothing"
