"""Bug-9788: ALL_MEMBER advertising and rollup All tuples are ONE pair.

Native Excel refused ``Workbook.SaveAs`` ("Document not saved.", a sub-60 ms
synchronous refusal with no dialog) as soon as TWO flat attribute hierarchies
shared a PivotTable axis.  Root cause, proven by a live env A/B on ALEX
2026-09-02 (one gateway image, only ``TESSALLITE_XMLA_SUPPRESS_ROLLUP_ALL``
changed, ``[XMLA-9644] dropped 6/41`` / ``6/16`` log lines proving the
manipulation was active on the exact failing queries):

* Excel's DISCOVER profile omits ``MDSCHEMA_HIERARCHIES.ALL_MEMBER``
  (Bug-9789 — advertising it breaks even the one-dimension save), BUT
* commit ``450565d65`` decoupled ``suppress_rollup_all_member`` from
  ``advertise_all_member``, so Execute kept returning synthetic All-grain
  tuples on the multi-hierarchy rollup path — the only Execute path that
  still emitted them, and exactly the path a CrossJoin of 2+ flat attributes
  takes.  Excel's pivot-cache writer refuses to persist All members its
  metadata never declared, so precisely the two-flat scenarios failed while
  one-flat (lone attribute: no rollup), flat+time and time+time (coverage
  guard drops rollups) passed.

The fix restores the pairing in ``cube_model.suppress_rollup_all_member``:
Excel never receives All-grain rollup tuples (not overrideable, mirroring the
``advertise_all_member`` pin), and other clients follow their own
``advertise_all_member`` state with the env switch as an explicit override.

Both directions are pinned here: the Excel axis must be detail-only, and a
non-Excel client must KEEP the complete All identity with source-computed
weighted subtotals (suppressing totals for everyone would silently restore
the wrong-weighted-average failure mode the rollup engine exists to prevent).
"""

from __future__ import annotations

from typing import Any

import pytest

from src.dax.cube_model import RollupWireMode, rollup_wire_mode, suppress_rollup_all_member

from tests.test_bug9789_9244_xmla_production_path import (
    _cells_by_ordinal,
    _excel_slicer_statement,
    _execute_method,
    _member_identity,
    _members_from_axis,
    _patch_execute_environment,
)
from src.dax import xmla_server


_EXCEL_APP = "Microsoft Office Excel"
_WEIGHTED_GRAND_TOTAL = 1816.8733126510879


# ---------------------------------------------------------------------------
# Unit contract: the pairing itself
# ---------------------------------------------------------------------------

def test_bug9788_excel_pairing_is_pinned(monkeypatch: pytest.MonkeyPatch):
    """Excel's aggregate coordinate is the native All member and no
    environment flag can move it.

    The env flag re-splitting the pair is exactly how the save regression
    shipped; mirror the non-overridable ``advertise_all_member`` Excel pin.
    Bug-9874 retired the interim calculated-total coordinate, so the pin is
    now the SSAS-faithful shape on both halves.
    """
    monkeypatch.delenv("TESSALLITE_XMLA_SUPPRESS_ROLLUP_ALL", raising=False)
    assert rollup_wire_mode(_EXCEL_APP) is RollupWireMode.NATIVE_ALL
    assert rollup_wire_mode(
        properties={"SspropInitAppName": _EXCEL_APP},
    ) is RollupWireMode.NATIVE_ALL
    assert suppress_rollup_all_member(_EXCEL_APP) is False

    for value in ("0", "1"):
        monkeypatch.setenv("TESSALLITE_XMLA_SUPPRESS_ROLLUP_ALL", value)
        assert rollup_wire_mode(_EXCEL_APP) is RollupWireMode.NATIVE_ALL, (
            "an environment flag must not re-split the Excel wire contract"
        )


def test_bug9788_non_excel_follows_advertising(monkeypatch: pytest.MonkeyPatch):
    """Non-Excel clients keep All tuples while ALL_MEMBER is advertised."""
    monkeypatch.delenv("TESSALLITE_XMLA_SUPPRESS_ROLLUP_ALL", raising=False)
    monkeypatch.delenv("TESSALLITE_XMLA_ALL_MEMBER", raising=False)
    assert suppress_rollup_all_member("DBeaver") is False
    assert suppress_rollup_all_member(None) is False

    # Emergency env switch still forces suppression for non-Excel clients.
    monkeypatch.setenv("TESSALLITE_XMLA_SUPPRESS_ROLLUP_ALL", "1")
    assert suppress_rollup_all_member("DBeaver") is True

    # Coupling: a client whose ALL_MEMBER advertising is suppressed must not
    # receive All tuples either — DISCOVER and Execute stay consistent.
    monkeypatch.delenv("TESSALLITE_XMLA_SUPPRESS_ROLLUP_ALL", raising=False)
    monkeypatch.setenv("TESSALLITE_XMLA_ALL_MEMBER", "0")
    assert suppress_rollup_all_member("DBeaver") is True


# ---------------------------------------------------------------------------
# Production path: the exact live 16-16 wire shape
# ---------------------------------------------------------------------------

_DIMS = ["account_type", "channel_name"]
_ACCOUNTS = ["CREDIT", "DEBIT", "SAVINGS", "CHECKING", "OTHER"]
_CHANNELS = ["WEB", "BRANCH", "ATM", "MOBILE", "PHONE", "PARTNER", "MAIL"]


def _leaf_rows() -> list[dict[str, Any]]:
    return [
        {
            "account_type": account,
            "channel_name": channel,
            "average_base_amount": float(account_index * 100 + channel_index),
        }
        for account_index, account in enumerate(_ACCOUNTS, 1)
        for channel_index, channel in enumerate(_CHANNELS, 1)
    ]


def _fake_execute_query():
    subtotal_values = {
        account: float(1000 + index * 37)
        for index, account in enumerate(_ACCOUNTS, 1)
    }

    async def fake(
        model_id: str, sql: str, tenant_slug: str, jwt_token: str,
        protocol: str = "dax", **_: Any,
    ):
        has_account = '"account_type"' in sql
        has_channel = '"channel_name"' in sql
        if has_account and has_channel:
            return {
                "columns": [*_DIMS, "average_base_amount"],
                "rows": _leaf_rows(),
            }
        if has_account:
            return {
                "columns": ["account_type", "average_base_amount"],
                "rows": [
                    {"account_type": account, "average_base_amount": value}
                    for account, value in subtotal_values.items()
                ],
            }
        if has_channel:
            return {
                "columns": ["channel_name", "average_base_amount"],
                "rows": [
                    {"channel_name": channel, "average_base_amount": 500.0 + i}
                    for i, channel in enumerate(_CHANNELS, 1)
                ],
            }
        return {
            "columns": ["average_base_amount"],
            "rows": [{"average_base_amount": _WEIGHTED_GRAND_TOTAL}],
        }

    return fake


@pytest.mark.asyncio
async def test_bug9788_excel_two_flat_axis_carries_native_all_tuples(
    monkeypatch: pytest.MonkeyPatch,
):
    """The failing 16-16 shape is served to Excel exactly as to any client.

    Pre-fix this returned 41 tuples whose six All-grain tuples Excel's
    DISCOVER disclaimed, and Excel refused to save. The interim fixes
    (suppression, then a calculated Total) are gone: Discover advertises
    ``ALL_MEMBER`` and the axis carries the native All member on every
    aggregate coordinate -- the full Bug-9845 lattice -- and the workbook
    saves (ALEX ladder 16-16 / 16-20, native-all).
    """
    monkeypatch.delenv("TESSALLITE_XMLA_SUPPRESS_ROLLUP_ALL", raising=False)
    _patch_execute_environment(monkeypatch, _DIMS, _fake_execute_query())
    response = await xmla_server._handle_execute(
        _execute_method(
            _excel_slicer_statement(_DIMS, "average_base_amount"),
            app_name=_EXCEL_APP,
        ),
        tenant_slug="demo", jwt_token="token", session_id="bug-9788-excel",
    )
    body = response.body.decode("utf-8")
    assert response.status_code == 200
    assert "<soap11env:Fault>" not in body

    tuples = _members_from_axis(body, "Axis0")
    assert len(tuples) == 48
    type_signatures = [
        tuple(_member_identity(m)["type"] for m in item) for item in tuples
    ]
    assert type_signatures.count(("1", "1")) == 35
    assert type_signatures.count(("1", "2")) == 5
    assert type_signatures.count(("2", "1")) == 7
    assert type_signatures.count(("2", "2")) == 1
    assert not any(
        _member_identity(m)["type"] == "4" for item in tuples for m in item
    ), "the retired calculated Total must never reach the axis"

    cells = _cells_by_ordinal(body)
    assert len(cells) == len(tuples)
    grand_ordinal = type_signatures.index(("2", "2"))
    assert cells[grand_ordinal] == pytest.approx(_WEIGHTED_GRAND_TOTAL)


@pytest.mark.asyncio
async def test_bug9788_non_excel_keeps_weighted_all_tuples(
    monkeypatch: pytest.MonkeyPatch,
):
    """The same wire shape keeps its full All identity for non-Excel clients.

    This is the guard against over-correcting: suppressing All-grain tuples
    globally would blank server-computed subtotals for clients that CAN cache
    them, and any client-side re-aggregation of averages produces wrong
    weighted totals.  The grand tuple must stay present and carry the exact
    source-weighted value.
    """
    monkeypatch.delenv("TESSALLITE_XMLA_SUPPRESS_ROLLUP_ALL", raising=False)
    monkeypatch.delenv("TESSALLITE_XMLA_ALL_MEMBER", raising=False)
    _patch_execute_environment(monkeypatch, _DIMS, _fake_execute_query())
    response = await xmla_server._handle_execute(
        _execute_method(_excel_slicer_statement(_DIMS, "average_base_amount")),
        tenant_slug="demo", jwt_token="token", session_id="bug-9788-nonexcel",
    )
    body = response.body.decode("utf-8")
    assert response.status_code == 200
    assert "<soap11env:Fault>" not in body

    tuples = _members_from_axis(body, "Axis0")
    # Bug-9845: the full CrossJoin - 35 leaves + 5 account subtotals
    # + 7 channel subtotals + grand total.
    assert len(tuples) == 48
    type_signatures = [
        tuple(_member_identity(m)["type"] for m in item) for item in tuples
    ]
    assert type_signatures.count(("1", "1")) == 35
    assert type_signatures.count(("1", "2")) == 5
    assert type_signatures.count(("2", "1")) == 7
    assert type_signatures.count(("2", "2")) == 1

    cells = _cells_by_ordinal(body)
    grand_ordinal = type_signatures.index(("2", "2"))
    assert cells[grand_ordinal] == pytest.approx(_WEIGHTED_GRAND_TOTAL)
