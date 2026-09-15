"""1.1.7 release certification, finding F-RC-01 — cross-request inventory reuse.

Bug-9885 exists because ``load_active_aggregates`` hydrates the model's WHOLE
aggregate inventory (every definition, every column, the Measure behind each
column — thousands of ORM rows) and the router asks for it on EVERY query. An
unrestricted ``MDSCHEMA_MEMBERS`` Discover issues one ``/discover/members``
request per dimension (~110 on ``modely_technical``), so without cross-request
reuse one user action pays that hydration ~110 times. The measured cost is in
``binder.py``'s own comment and in ``architecture_query-routing.md``: ~250 ms
of Python per request, ~85% of the request's CPU, and the reason a cold
unrestricted Discover cost ~45 s.

Bug-9938 correctly stopped ORM rows crossing sessions, but it did so by moving
the cache into ``AsyncSession.info``. FastAPI opens one session per request
(``shared/db/session.get_tenant_db``), so the reuse window collapsed to a
single request and the ~110x hydration returned. Measured on the running dev
stack (query-router built from the release tip, tenant ``acme-demo``, a model
with 107 aggregate definitions / 8,461 aggregate columns): 10 ms for a second
load in the SAME session, 191-306 ms for a load in a FRESH session.

Both halves of the contract must hold at once, and this guard pins both:

* no ORM instance may be handed to a session that did not load it (Bug-9938);
* an unchanged inventory must not be hydrated again for the next request
  (Bug-9885).

A cache of plain, immutable, session-free data satisfies both. A cache keyed on
the session satisfies only the first; a cache of ORM rows satisfies only the
second.

Status when written: FAILS against main ``68cb2f9fe`` by design — it pins the
finding. The old ``test_bug9938_inventory_cache_is_session_local`` assertion in
``test_bug9885_aggregate_inventory_cache.py`` pinned the interim session-local
design and was reconciled with this contract as part of the fix; that
reconciliation is a design correction, not a weakening.

Tier: T3 — the regression is on the Excel XMLA Discover path.
"""
from __future__ import annotations

import dataclasses

import pytest

from src.semantic.binder import (
    invalidate_aggregate_inventory,
    load_active_aggregates,
)

from test_bug9885_aggregate_inventory_cache import (  # noqa: E402
    MODEL_ID,
    FakeSession,
    _aggregate,
    _identity,
)


def _request_session() -> FakeSession:
    """A session shaped like one FastAPI request's ``get_tenant_db`` session."""
    return FakeSession(
        _identity(),
        [_aggregate("active", "sales")],
        [_aggregate("retired", "legacy")],
    )


@pytest.fixture(autouse=True)
def _clean_inventory():
    invalidate_aggregate_inventory()
    yield
    invalidate_aggregate_inventory()


@pytest.mark.asyncio
async def test_f_rc_01_next_request_does_not_rehydrate_an_unchanged_inventory():
    """The second request re-reads the identity, not the thousands of rows."""
    first = _request_session()
    second = _request_session()

    await load_active_aggregates(MODEL_ID, first)
    # One cold load issues two hydration queries: the active set and the
    # inactive set, as the Bug-9885 guards already assert.
    assert first.hydration_queries == 2, "the cold request must hydrate once"

    await load_active_aggregates(MODEL_ID, second)

    assert second.identity_queries == 1, (
        "the identity probe must still run on every load — the reuse is not a TTL"
    )
    assert second.hydration_queries == 0, (
        "a second request re-hydrated the whole aggregate inventory although "
        "the identity was unchanged; this is the ~110x Discover cost Bug-9885 "
        "removed and the session-local cache reintroduced"
    )


@pytest.mark.asyncio
async def test_f_rc_01_reuse_never_hands_one_session_another_session_rows():
    """Reuse must not be bought back by sharing session-bound ORM instances."""
    first = _request_session()
    second = _request_session()

    served_first = await load_active_aggregates(MODEL_ID, first)
    served_second = await load_active_aggregates(MODEL_ID, second)

    loaded_by_first = {id(obj) for obj in first.active}
    assert served_first, "the first request must be served an inventory"
    assert served_second, "the second request must be served an inventory"
    assert dataclasses.is_dataclass(served_second[0])
    assert isinstance(served_second[0].columns, tuple)
    with pytest.raises(dataclasses.FrozenInstanceError):
        served_second[0].status = "inactive"
    for definition in served_second:
        assert id(definition) not in loaded_by_first, (
            "the second request received an ORM instance loaded by the first "
            "request's session (Bug-9938)"
        )


@pytest.mark.asyncio
async def test_f_rc_01_a_changed_identity_still_rehydrates_for_the_next_request():
    """Cross-request reuse must not outlive a change to the aggregates."""
    first = _request_session()
    await load_active_aggregates(MODEL_ID, first)

    second = _request_session()
    second.identity = _identity(deploy_epoch="8")
    await load_active_aggregates(MODEL_ID, second)

    assert second.hydration_queries == 2, (
        "a moved deploy epoch must rehydrate — serving a stale inventory is a "
        "wrong-numbers routing risk"
    )
