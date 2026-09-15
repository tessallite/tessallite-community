"""Bug-9865: the UNRESTRICTED MDSCHEMA_MEMBERS Discover fetch path.

Bug-9865's row-builder half was fixed in 6f1f7ebab (quadratic level scans in
``mdschema._rows_members``). This file pins the FETCH half, which measurement
on the demo technical catalog (111 hierarchies, 171,418 members) attributed to
three distinct defects:

1. **Unbounded fan-out.** ``_load_discover_member_data`` gathered one
   model-service / query-router member fetch per dimension with no bound, so a
   whole-cube browse opened ~110 at once against a tenant pool of
   ``TENANT_DB_POOL_SIZE`` (default 2). The surplus queued until SQLAlchemy's
   pool timeout and returned 500 — measured 56 ``QueuePool`` timeouts in one
   run. That set the request's wall clock AND silently dropped every timed-out
   dimension: the identical Discover returned 101,126 / 110,424 / 121,184 /
   130,878 rows on four consecutive runs. Bounded, it returns 171,418 every
   time — the old path was losing up to 40% of the cube's members with no
   error surfaced to the client.

2. **Unbounded source query behind a bounded page.** The flat path asked the
   query-router for ``MEMBER_DISCOVERY_LIMIT`` (100,000) distinct values and
   then discarded everything past the XMLA page cap (10,000).

3. **A cache that could never hit.** ``principal_fingerprint`` hashed the raw
   JWT bytes, but the Basic-auth path Excel uses re-mints a token every 30s
   (``credential_cache``), so every rotation invalidated the whole member and
   metadata cache. An unrestricted Discover takes LONGER than that window, so
   the next request always arrived on a fresh token and the cache missed 110
   of 110 dimensions on every single run — the burst de-duplicator was dead in
   exactly the case it was written for. Keyed on the principal's claims
   instead, the same request goes 43s cold -> 3.4s warm with an identical
   171,418-row result.

The restricted shapes Excel actually drills with (TREE_OP + MEMBER_UNIQUE_NAME,
or a HIERARCHY_UNIQUE_NAME restriction) must stay COMPLETE and uncapped; the
tests below pin that boundary as well as the bounds.
"""
import asyncio
import itertools
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from jose import jwt

sys.path.insert(0, str(Path(__file__).parent.parent))

from shared.config.settings import get_settings  # noqa: E402

from src.dax import member_cache  # noqa: E402
from src.dax import xmla_server  # noqa: E402

_settings = get_settings()

_DIMS = [{"name": f"D{i}", "source": "column"} for i in range(40)]

_BASE_KW = dict(
    model_id="m1",
    project_id="p1",
    tenant_slug="acme",
    jwt_token="tok",
    persona_id=None,
)


_mint_seq = itertools.count()


def _mint(sub="u1", tenant_id="t1", role="viewer", **extra):
    """Mint a token the way the gateway's own issuer does.

    Each call advances ``iat``/``exp`` by a second so two mints for the same
    principal are genuinely DIFFERENT tokens — which is the whole point of the
    rotation test, and what a real re-login produces.
    """
    now = datetime.now(timezone.utc) + timedelta(seconds=next(_mint_seq))
    return jwt.encode(
        {
            "sub": sub,
            "tenant_id": tenant_id,
            "role": role,
            "iat": now,
            "exp": now + timedelta(minutes=60),
            **extra,
        },
        _settings.JWT_SECRET_KEY,
        algorithm=_settings.JWT_ALGORITHM,
    )


# --------------------------------------------------------------------------
# 1. Bounded fan-out
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bug9865_unrestricted_fan_out_is_bounded(monkeypatch):
    """The whole-cube browse never has more than the configured number of
    member fetches in flight.

    Pre-fix this was a bare ``asyncio.gather`` over every dimension, so the
    peak equalled the dimension count (110 on the demo technical catalog)
    against a tenant pool of 2 — the pool-timeout storm that both slowed the
    request and silently truncated it. Fails against the pre-fix builder with
    peak == len(_DIMS).
    """
    limit = int(_settings.XMLA_MEMBER_FETCH_CONCURRENCY)
    state = {"live": 0, "peak": 0}

    async def fake_members(model_id, dname, tenant_slug, jwt_token, **kw):
        state["live"] += 1
        state["peak"] = max(state["peak"], state["live"])
        try:
            # Yield to the loop so genuinely concurrent callers overlap here.
            await asyncio.sleep(0.01)
            return {"members": [{"name": "a"}], "levels": [dname]}
        finally:
            state["live"] -= 1

    monkeypatch.setattr(xmla_server, "get_dimension_members", fake_members)

    out = await xmla_server._load_discover_member_data(
        dimensions=_DIMS, restrictions={}, **_BASE_KW,
    )

    assert len(out) == len(_DIMS), "every dimension must still be served"
    assert state["peak"] <= limit, (
        f"member fetch fan-out peaked at {state['peak']}, above the "
        f"XMLA_MEMBER_FETCH_CONCURRENCY bound of {limit} (Bug-9865)"
    )
    # Guard against a bound so tight it serialises the fetch: with 40 tasks and
    # a bound of N the peak should actually reach N.
    assert state["peak"] == min(limit, len(_DIMS)), (
        "the fetch must still run concurrently up to the bound, not serially"
    )


@pytest.mark.asyncio
async def test_bug9865_a_failed_dimension_fetch_is_reported(monkeypatch, caplog):
    """A dimension whose fetch fails is named in the log, not dropped in silence.

    The pool-timeout storm removed whole dimensions from the rowset with no
    trace on this path, which is indistinguishable to a client from a dimension
    that legitimately has no members.
    """
    async def fake_members(model_id, dname, tenant_slug, jwt_token, **kw):
        if dname == "D3":
            raise RuntimeError("pool timeout")
        return {"members": [{"name": "a"}], "levels": [dname]}

    monkeypatch.setattr(xmla_server, "get_dimension_members", fake_members)

    with caplog.at_level("WARNING"):
        out = await xmla_server._load_discover_member_data(
            dimensions=_DIMS, restrictions={}, **_BASE_KW,
        )

    assert "D3" not in out
    assert len(out) == len(_DIMS) - 1
    assert any(
        "D3" in r.getMessage() for r in caplog.records
    ), "the dropped dimension must be named in the warning (Bug-9865)"


# --------------------------------------------------------------------------
# 2. The source query is bounded to the page the browse will emit
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bug9865_browse_bounds_the_source_query(monkeypatch):
    """An unrestricted browse asks the source only for the page it will emit
    (plus one, so overflow is still detectable and logged)."""
    seen: list[int | None] = []

    async def fake_members(model_id, dname, tenant_slug, jwt_token, *,
                           persona_id=None, limit=None):
        seen.append(limit)
        return {"members": [{"name": "a"}], "levels": [dname]}

    monkeypatch.setattr(xmla_server, "get_dimension_members", fake_members)

    await xmla_server._load_discover_member_data(
        dimensions=_DIMS[:3], restrictions={}, **_BASE_KW,
    )

    expected = xmla_server._member_page_limit() + 1
    assert seen == [expected] * 3, (
        "a browse must bound the source scan to its page, not request "
        f"MEMBER_DISCOVERY_LIMIT ({_settings.MEMBER_DISCOVERY_LIMIT}) and "
        "discard the remainder (Bug-9865)"
    )
    assert expected <= _settings.MEMBER_DISCOVERY_LIMIT + 1


@pytest.mark.asyncio
async def test_bug9865_specific_member_lookup_is_not_source_capped(monkeypatch):
    """A specific-member restriction must NOT bound the source query.

    Flat dimensions have no parent pushdown, so the whole level is fetched and
    re-filtered downstream to the one member. Capping the fetch would make a
    member past the page boundary unresolvable — the completeness boundary
    Bug-6602 established and this change must not cross.
    """
    seen: list[int | None] = []

    async def fake_members(model_id, dname, tenant_slug, jwt_token, *,
                           persona_id=None, limit=None):
        seen.append(limit)
        return {"members": [{"name": "a"}], "levels": [dname]}

    monkeypatch.setattr(xmla_server, "get_dimension_members", fake_members)

    await xmla_server._load_discover_member_data(
        dimensions=[{"name": "D0", "source": "column"}],
        restrictions={"MEMBER_UNIQUE_NAME": ["[D0].[D0].&[zzz]"]},
        **_BASE_KW,
    )

    assert seen == [None], (
        "a specific-member lookup must keep the uncapped fetch (Bug-9865)"
    )


@pytest.mark.asyncio
async def test_bug9865_capped_browse_is_not_served_to_an_uncapped_lookup(
    monkeypatch,
):
    """The bounded browse value and the full level are separate cache entries.

    Sharing one key would let the browse's truncated page answer a later
    specific-member lookup, silently making a real member unresolvable.
    """
    seen: list[int | None] = []

    async def fake_members(model_id, dname, tenant_slug, jwt_token, *,
                           persona_id=None, limit=None):
        seen.append(limit)
        return {"members": [{"name": "a"}], "levels": [dname]}

    monkeypatch.setattr(xmla_server, "get_dimension_members", fake_members)
    dims = [{"name": "D0", "source": "column"}]

    await xmla_server._load_discover_member_data(
        dimensions=dims, restrictions={}, **_BASE_KW,
    )
    await xmla_server._load_discover_member_data(
        dimensions=dims,
        restrictions={"MEMBER_UNIQUE_NAME": ["[D0].[D0].&[zzz]"]},
        **_BASE_KW,
    )

    assert seen == [xmla_server._member_page_limit() + 1, None], (
        "the specific-member lookup must re-fetch uncapped rather than reuse "
        "the browse's bounded page (Bug-9865)"
    )


# --------------------------------------------------------------------------
# 3. The cache survives token rotation — without widening its audience
# --------------------------------------------------------------------------

def test_bug9865_fingerprint_survives_token_rotation():
    """Two tokens minted for the SAME principal share a cache entry.

    Pre-fix the fingerprint hashed the raw token bytes, so the 30s
    ``credential_cache`` rotation invalidated every member and metadata entry —
    and since an unrestricted Discover takes longer than 30s, the cache could
    never hit at all. Fails against the pre-fix fingerprint.
    """
    first = _mint()
    second = _mint()
    assert first != second, "the two tokens must genuinely differ (iat/exp)"
    assert (
        member_cache.principal_fingerprint(first)
        == member_cache.principal_fingerprint(second)
    ), "a rotated token for the same principal must reuse its cache entries"


@pytest.mark.parametrize(
    "changed",
    [
        {"sub": "u2"},
        {"tenant_id": "t2"},
        {"role": "admin"},
        {"email": "someone@else.test"},
        {"groups": ["finance"]},
    ],
)
def test_bug9865_fingerprint_separates_security_contexts(changed):
    """Any claim that can change a member list must change the fingerprint.

    Member lists are row-security filtered from the caller's Principal
    (identity, tenant, roles, groups, email), so two principals must never
    share an entry. This is the security boundary the rotation fix must not
    cross.
    """
    base = member_cache.principal_fingerprint(_mint(groups=[], email="a@b.test"))
    other = member_cache.principal_fingerprint(
        _mint(**{"groups": [], "email": "a@b.test", **changed})
    )
    assert base != other, f"changing {changed} must not reuse another cache entry"


def test_bug9865_undecodable_token_keys_on_its_own_bytes():
    """An unverifiable token falls back to the previous, strictly narrower
    behaviour: it shares an entry with nothing but itself."""
    assert (
        member_cache.principal_fingerprint("not-a-jwt")
        == member_cache.principal_fingerprint("not-a-jwt")
    )
    assert (
        member_cache.principal_fingerprint("not-a-jwt")
        != member_cache.principal_fingerprint("also-not-a-jwt")
    )
    # A forged token (right shape, wrong secret) must not collide with the
    # real principal's entry.
    forged = jwt.encode({"sub": "u1", "tenant_id": "t1"}, "wrong-secret",
                        algorithm=_settings.JWT_ALGORITHM)
    assert (
        member_cache.principal_fingerprint(forged)
        != member_cache.principal_fingerprint(_mint())
    )
    assert member_cache.principal_fingerprint("") == ""


@pytest.mark.asyncio
async def test_bug9865_second_browse_on_a_rotated_token_hits_the_cache(
    monkeypatch,
):
    """End to end: the repeat unrestricted Discover does no source work.

    This is the behaviour the 43s -> 3.4s measurement rests on. Pre-fix the
    rotated token missed every entry and re-fetched all 110 dimensions.
    """
    calls: list[str] = []

    async def fake_members(model_id, dname, tenant_slug, jwt_token, **kw):
        calls.append(dname)
        return {"members": [{"name": "a"}], "levels": [dname]}

    monkeypatch.setattr(xmla_server, "get_dimension_members", fake_members)
    kw = {k: v for k, v in _BASE_KW.items() if k != "jwt_token"}

    first = await xmla_server._load_discover_member_data(
        dimensions=_DIMS, restrictions={}, jwt_token=_mint(), **kw,
    )
    assert len(calls) == len(_DIMS)

    calls.clear()
    second = await xmla_server._load_discover_member_data(
        dimensions=_DIMS, restrictions={}, jwt_token=_mint(), **kw,
    )
    assert calls == [], (
        "the repeat browse re-fetched every dimension — the member cache is "
        "still keyed on the rotating token (Bug-9865)"
    )
    assert second == first, "the cached result must be identical"
