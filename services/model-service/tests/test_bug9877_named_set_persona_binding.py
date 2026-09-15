"""Bug-9877 — a named set is visible to a persona IFF it binds for that persona.

Audit rows A18/A19/A20/A39 (docs/execution/execution_persona-layering-audit.md).
Before this fix, ``_named_set_visible_to_persona`` compared bracket tokens of
the stored MDX against the persona's DIMENSION allow-list only: the ranking
measure inside ``TopCount(...)`` never entered the decision, and a token the
scan could not resolve was left VISIBLE (fail-open). These tests assert the
replacement contract:

* the query the decision is delegated to NAMES every dimension and measure the
  set references, so the persona gate can refuse on either;
* the verdict is exactly that query's bind result, in both directions;
* an unresolvable reference hides the set (fail closed);
* a persona-free caller is never probed and keeps seeing everything.
"""
from __future__ import annotations

import types
import uuid

import pytest

from src.api.named_set_visibility import (
    ModelSurface,
    build_probe_for_named_set,
    clear_named_set_visibility_cache,
    named_set_binds_for_persona,
)


MODEL_ID = uuid.uuid4()
MODEL_SLUG = "modely"


def _surface() -> ModelSurface:
    return ModelSurface(
        dimensions={"customer": "customer", "region": "region"},
        measures={
            "transaction_amount": ("transaction_amount", "sum"),
            "order_count": ("order_count", "count"),
        },
    )


def _topn_set(measure: str = "transaction_amount"):
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        expression=(
            f"TopCount([customer].Members, 5, [Measures].[{measure}])"
        ),
        dimensions="customer",
        builder_definition={
            "type": "topN", "entity": "customer",
            "measure": measure, "count": 5, "direction": "top",
        },
    )


def _persona(bypass: bool = False):
    return types.SimpleNamespace(id=uuid.uuid4(), bypass_row_security=bypass)


@pytest.fixture(autouse=True)
def _clean_cache():
    clear_named_set_visibility_cache()
    yield
    clear_named_set_visibility_cache()


class TestProbeNamesEveryReference:
    def test_bug9877_probe_names_the_ranking_measure_and_the_entity(self):
        """The measure the old scan ignored is IN the query the gate sees."""
        sql = build_probe_for_named_set(_topn_set(), _surface(), MODEL_SLUG)
        assert sql is not None
        assert "transaction_amount" in sql
        assert "customer" in sql
        assert MODEL_SLUG in sql

    def test_bug9877_probe_reads_the_raw_mdx_when_there_is_no_builder(self):
        ns = types.SimpleNamespace(
            id=uuid.uuid4(),
            expression="TopCount([region].Members, 3, [Measures].[order_count])",
            dimensions=None,
            builder_definition=None,
        )
        sql = build_probe_for_named_set(ns, _surface(), MODEL_SLUG)
        assert sql is not None
        assert "region" in sql
        assert "order_count" in sql

    def test_bug9877_unresolvable_reference_produces_no_probe(self):
        """Fail CLOSED: the old scan left this set visible."""
        ns = types.SimpleNamespace(
            id=uuid.uuid4(),
            expression="TopCount([not_a_dimension].Members, 5, [Measures].[nope])",
            dimensions=None,
            builder_definition=None,
        )
        assert build_probe_for_named_set(ns, _surface(), MODEL_SLUG) is None


class TestVisibleIffBinds:
    @pytest.mark.asyncio
    async def test_bug9877_persona_without_the_ranking_measure_cannot_see_it(
        self, monkeypatch,
    ):
        calls: list[str] = []

        async def _refuse(*, model_id, sql, persona_id, bearer, timeout_s=15.0):
            calls.append(sql)
            return False

        monkeypatch.setattr(
            "src.api.named_set_visibility.probe_binds", _refuse,
        )
        visible = await named_set_binds_for_persona(
            _topn_set(),
            model_id=MODEL_ID, model_slug=MODEL_SLUG,
            surface=_surface(), persona=_persona(), bearer="t",
        )
        assert visible is False
        # The refusal was decided on a query that names the measure.
        assert calls and "transaction_amount" in calls[0]

    @pytest.mark.asyncio
    async def test_bug9877_persona_with_the_measure_can_see_it(self, monkeypatch):
        async def _allow(*, model_id, sql, persona_id, bearer, timeout_s=15.0):
            return True

        monkeypatch.setattr(
            "src.api.named_set_visibility.probe_binds", _allow,
        )
        visible = await named_set_binds_for_persona(
            _topn_set(),
            model_id=MODEL_ID, model_slug=MODEL_SLUG,
            surface=_surface(), persona=_persona(), bearer="t",
        )
        assert visible is True

    @pytest.mark.asyncio
    async def test_bug9877_unresolvable_token_hides_without_probing(
        self, monkeypatch,
    ):
        async def _explode(**_kwargs):
            raise AssertionError("an unresolvable set must never be probed")

        monkeypatch.setattr(
            "src.api.named_set_visibility.probe_binds", _explode,
        )
        ns = types.SimpleNamespace(
            id=uuid.uuid4(),
            expression="{ [ghost_dimension].[ghost].&[x] }",
            dimensions=None,
            builder_definition=None,
        )
        visible = await named_set_binds_for_persona(
            ns,
            model_id=MODEL_ID, model_slug=MODEL_SLUG,
            surface=_surface(), persona=_persona(), bearer="t",
        )
        assert visible is False

    @pytest.mark.asyncio
    async def test_bug9877_no_persona_means_no_probe_and_full_visibility(
        self, monkeypatch,
    ):
        async def _explode(**_kwargs):
            raise AssertionError("a persona-free caller must not be probed")

        monkeypatch.setattr(
            "src.api.named_set_visibility.probe_binds", _explode,
        )
        visible = await named_set_binds_for_persona(
            _topn_set(),
            model_id=MODEL_ID, model_slug=MODEL_SLUG,
            surface=_surface(), persona=None, bearer="t",
        )
        assert visible is True

    @pytest.mark.asyncio
    async def test_bug9877_verdict_is_cached_per_model_persona_definition(
        self, monkeypatch,
    ):
        seen: list[str] = []

        async def _allow(*, model_id, sql, persona_id, bearer, timeout_s=15.0):
            seen.append(persona_id)
            return True

        monkeypatch.setattr(
            "src.api.named_set_visibility.probe_binds", _allow,
        )
        ns = _topn_set()
        persona = _persona()
        for _ in range(3):
            assert await named_set_binds_for_persona(
                ns, model_id=MODEL_ID, model_slug=MODEL_SLUG,
                surface=_surface(), persona=persona, bearer="t",
            )
        assert len(seen) == 1
        # A DIFFERENT persona is a different verdict, never the cached one.
        other = _persona()
        assert await named_set_binds_for_persona(
            ns, model_id=MODEL_ID, model_slug=MODEL_SLUG,
            surface=_surface(), persona=other, bearer="t",
        )
        assert len(seen) == 2
