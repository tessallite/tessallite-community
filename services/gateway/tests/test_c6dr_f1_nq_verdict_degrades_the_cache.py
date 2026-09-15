"""C6DR-F1: an unreadable Named Query verdict must not be cached as a good one.

Bug-9186 (rule-4 wave 2) made the JDBC catalogue advertise a Named Query only
when the query-router says its definition BINDS for that persona. The verdict is
fetched inside ``fetch_model_metadata``, and its fail-closed branch returned an
empty set on any error -- indistinguishable from the honest verdict "this
persona narrows away every Named Query".

``fetch_model_metadata`` caches a completed build for the metadata TTL, gated on
``_degraded_models == 0`` (the Bug-9061 rule: "only a COMPLETE fetch is cached
... never pinned for the TTL -- that is how a transient model-service blip would
turn into 30 s of 'my model vanished'"). Because the verdict's failure was
invisible, a build during a brief query-router outage produced an ``@NQ``-less
catalogue that the gate then stored as good: every Named Query relation vanished
from the catalogue for the whole TTL, for every caller sharing the principal
fingerprint, while the executor would still have accepted those queries.

The distinction this file pins is the whole fix: an EMPTY verdict is a real
answer and stays cacheable; an UNOBTAINABLE verdict raises and marks the build
degraded so it is not stored.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src import router_client  # noqa: E402


def _patch_model(monkeypatch):
    """A deployed model carrying exactly one Named Query."""

    async def _models(*_a, **_kw):
        # A DEPLOYED model: the Named Query leg only runs for one, because the
        # relations are built from the deployed version snapshot.
        return [{
            "id": "m1", "project_id": "p1", "project_slug": "alpha",
            "slug": "sales", "description": "Sales",
            "deployed_version_id": "v1",
        }]

    async def _dimensions(*_a, **_kw):
        return [{"id": "d1", "name": "region", "source_column_id": "c1"}]

    async def _measures(*_a, **_kw):
        return [{"id": "me1", "name": "amount", "source_column_id": "c2",
                 "default_agg": "sum"}]

    async def _personas(*_a, **_kw):
        return []

    async def _snapshot(*_a, **_kw):
        return {
            "tables": [{"id": "t1", "alias": "sales", "table_type": "fact"}],
            "columns": [
                {"id": "c1", "model_table_id": "t1"},
                {"id": "c2", "model_table_id": "t1"},
            ],
            "joins": [],
            "named_queries": [{
                "name": "nq_ok",
                "definition_sql": "SELECT region FROM sales",
                "output_columns": [{"name": "region", "data_type": "text"}],
            }],
        }

    async def _kpis(*_a, **_kw):
        return []

    async def _version_snapshot(*_a, **_kw):
        return await _snapshot()

    for name, fn in (
        ("get_model_version_snapshot", _version_snapshot),
        ("list_all_models_for_tenant", _models),
        ("get_model_dimensions", _dimensions),
        ("get_model_measures", _measures),
        ("get_model_personas", _personas),
        ("get_model_snapshot", _snapshot),
        ("get_model_kpis", _kpis),
    ):
        monkeypatch.setattr(router_client, name, fn, raising=False)


def _relation_names(catalogue) -> list[str]:
    first = catalogue[0] if catalogue else {}
    if isinstance(first, dict):
        return [str(k) for k in first]
    return [str(x) for x in (first or [])]


@pytest.mark.asyncio
async def test_an_unobtainable_verdict_is_not_cached_as_a_good_catalogue(monkeypatch):
    """The defect, end to end: a fault during the build must not pin an
    ``@NQ``-less catalogue for the TTL. Pre-fix, phase 2 still served the
    cached empty catalogue (the review's probe recorded ``phase2 []``)."""
    _patch_model(monkeypatch)
    router_client.invalidate_metadata_cache() if hasattr(
        router_client, "invalidate_metadata_cache") else None

    async def _unobtainable(*_a, **_kw):
        raise router_client.NamedQueryVisibilityUnavailable("router unreachable")

    monkeypatch.setattr(
        router_client, "persona_visible_named_queries", _unobtainable,
    )
    during_fault = await router_client.fetch_model_metadata(
        None, "acme", "jwt-c6dr-f1", use_cache=True,
    )
    # Fail CLOSED for this response: nothing is advertised that the executor
    # might refuse.
    assert not any(n.startswith("@") for n in _relation_names(during_fault))

    async def _obtainable(*_a, **_kw):
        return {"nq_ok"}

    monkeypatch.setattr(
        router_client, "persona_visible_named_queries", _obtainable,
    )
    after_fault = await router_client.fetch_model_metadata(
        None, "acme", "jwt-c6dr-f1", use_cache=True,
    )
    # ...and the SAME identity recovers immediately, because the degraded build
    # was never stored. This is the assertion that fails on the pre-fix code.
    assert any(n.startswith("@") for n in _relation_names(after_fault)), (
        "the catalogue built during the fault was cached, so the Named Query "
        "stayed invisible after the fault cleared (C6DR-F1)"
    )


@pytest.mark.asyncio
async def test_a_genuinely_empty_verdict_remains_cacheable(monkeypatch):
    """The control, and the reason a blanket 'never cache an empty NQ set' fix
    would be wrong: a persona that narrows away every Named Query is a correct,
    complete answer and must not bust the cache."""
    _patch_model(monkeypatch)

    calls: list[int] = []

    async def _empty_but_answered(*_a, **_kw):
        calls.append(1)
        return set()

    monkeypatch.setattr(
        router_client, "persona_visible_named_queries", _empty_but_answered,
    )
    first = await router_client.fetch_model_metadata(
        None, "acme", "jwt-c6dr-f1-empty", use_cache=True,
    )
    second = await router_client.fetch_model_metadata(
        None, "acme", "jwt-c6dr-f1-empty", use_cache=True,
    )

    assert not any(n.startswith("@") for n in _relation_names(first))
    assert not any(n.startswith("@") for n in _relation_names(second))
    # Served from the cache: the verdict was obtained, so the build was complete.
    assert len(calls) == 1, (
        "an answered-but-empty verdict must stay cacheable; only an "
        "UNOBTAINABLE verdict marks the build degraded"
    )
