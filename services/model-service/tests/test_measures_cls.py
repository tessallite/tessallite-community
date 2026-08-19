"""Bug-6141 — CLS restricted-column hiding must hold on the MEASURES surface.

The dimensions list/get already fail-closed on CLS-restricted columns; measures
did not — the list/get echoed a restricted ``source_column_name`` and the
redundant-partner hint's ``partner_column_name`` to a restricted persona. These
tests lock the mirrored fix in ``api/measures.py``.
"""
from __future__ import annotations

import types
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from .result_fakes import FakeScalarResult

from src.api.measures import (
    _build_response,
    _compute_transitive_hidden_names,
    _measure_touches_restricted_column,
)
from src.auth.middleware import CurrentUser, get_current_user
from src.main import app

from .conftest import (
    TEST_MODEL_ID,
    TEST_PROJECT_ID,
    TEST_TENANT,
    async_gen_from,
    make_mock_db,
)

pytestmark = pytest.mark.unit

PREFIX = f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/measures"

NOW = datetime.now(timezone.utc)


def _measure(*, source_column_id, name="Amount"):
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        model_id=TEST_MODEL_ID,
        name=name,
        display_name=name,
        description=None,
        display_folder=None,
        source_column_id=source_column_id,
        user_defined_attribute_id=None,
        measure_type="standard",
        expression=None,
        calc_agg_mode=None,
        data_type="numeric",
        default_agg="sum",
        format=None,
        variant_kind=None,
        variant_of_measure_id=None,
        variant_n=None,
        is_additive=True,
        semi_additive_behavior=None,
        semi_additive_account_column_id=None,
        calendar_model_table_id=None,
        hierarchy_id=None,
        date_dimension_column_id=None,
        is_invalid=False,
        invalid_reason=None,
        cross_model_source_model_id=None,
        cross_model_source_measure_id=None,
        created_at=NOW,
        updated_at=NOW,
    )


def _col(col_id, name):
    return types.SimpleNamespace(
        id=col_id, column_name=name, display_name=name.replace("_", " ").title(),
        data_type="numeric", model_table_id=uuid.uuid4(), is_hidden=False,
    )


# ---------------------------------------------------------------------------
# Helper — _measure_touches_restricted_column (fail-closed)
# ---------------------------------------------------------------------------


def test_measure_touches_restricted_column_on_source():
    col = uuid.uuid4()
    m = _measure(source_column_id=col)
    assert _measure_touches_restricted_column(m, {col}) is True


def test_measure_not_restricted_when_column_clear():
    m = _measure(source_column_id=uuid.uuid4())
    assert _measure_touches_restricted_column(m, {uuid.uuid4()}) is False


def test_measure_not_restricted_with_empty_set():
    m = _measure(source_column_id=uuid.uuid4())
    assert _measure_touches_restricted_column(m, set()) is False


# ---------------------------------------------------------------------------
# _build_response — redundant-partner hint must not leak a restricted column
# ---------------------------------------------------------------------------


def _partner_inputs():
    src_col_id = uuid.uuid4()
    partner_col_id = uuid.uuid4()
    source_col = _col(src_col_id, "visible_amount")
    measure = _measure(source_column_id=src_col_id)
    hint = types.SimpleNamespace(
        partner_column_id=partner_col_id,
        partner_column_name="SECRET_SALARY",
        partner_table_name="fact", partner_physical_table="fact",
        join_type="inner", reason="Equivalent to fact.SECRET_SALARY",
    )
    partners = {src_col_id: hint}

    db = AsyncMock()

    async def _get(cls, pk):
        if cls.__name__ == "ModelColumn":
            return source_col if pk == src_col_id else None
        return None

    db.get = AsyncMock(side_effect=_get)
    return db, measure, partners, partner_col_id


@pytest.mark.asyncio
async def test_measure_partner_hint_suppressed_when_restricted():
    db, measure, partners, partner_col_id = _partner_inputs()
    resp = await _build_response(
        db, measure, partners, glossary_texts={},
        restricted_cols={partner_col_id},
    )
    assert resp.redundant_partner is None


@pytest.mark.asyncio
async def test_measure_partner_hint_present_when_partner_clear():
    db, measure, partners, _partner_col_id = _partner_inputs()
    resp = await _build_response(
        db, measure, partners, glossary_texts={}, restricted_cols=set(),
    )
    assert resp.redundant_partner is not None
    assert resp.redundant_partner.partner_column_name == "SECRET_SALARY"


# ---------------------------------------------------------------------------
# Route reproduce — list_measures must not leak a restricted column name
# ---------------------------------------------------------------------------


class _ScalarResult:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return FakeScalarResult(self._items)

    def all(self):
        return list(self._items)


@pytest.fixture
def as_viewer():
    user = CurrentUser(
        user_id="v@example.com", tenant_id=TEST_TENANT,
        email="v@example.com", role="viewer",
    )
    app.dependency_overrides[get_current_user] = lambda: user
    yield user
    app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
async def test_list_measures_hides_restricted_column_measure(client, as_viewer):
    """A restricted persona must not see a measure backed by a restricted
    column, nor its source_column_name (the leak Bug-6141 closes)."""
    restricted_col = uuid.uuid4()
    visible_col = uuid.uuid4()
    restricted_measure = _measure(source_column_id=restricted_col, name="Salary")
    visible_measure = _measure(source_column_id=visible_col, name="Amount")

    persona = types.SimpleNamespace(
        id=uuid.uuid4(), included_measure_ids=[], included_dimension_ids=[],
    )

    db = make_mock_db()
    db.execute = AsyncMock(
        return_value=_ScalarResult([restricted_measure, visible_measure])
    )

    async def _get(cls, pk):
        if cls.__name__ == "ModelColumn":
            if pk == visible_col:
                return _col(visible_col, "amount_col")
            if pk == restricted_col:
                return _col(restricted_col, "salary_col")
        return None

    db.get = AsyncMock(side_effect=_get)

    with (
        patch("src.api.measures.get_tenant_db", async_gen_from(db)),
        patch("src.api.measures.ensure_model_in_project", new=AsyncMock()),
        patch(
            "src.api.measures.resolve_effective_persona",
            new=AsyncMock(return_value=persona),
        ),
        patch(
            "src.api.measures.get_restricted_column_ids",
            new=AsyncMock(return_value={restricted_col}),
        ),
        patch(
            "src.api.measures._load_uda_column_map",
            new=AsyncMock(return_value={}),
        ),
        patch(
            "src.api.measures._load_redundant_partners",
            new=AsyncMock(return_value={}),
        ),
        patch(
            "src.api.measures._glossary_texts_for_targets",
            new=AsyncMock(return_value={}),
        ),
    ):
        resp = await client.get(PREFIX)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    names = {m["name"] for m in body}
    assert "Salary" not in names  # restricted measure hidden
    assert "Amount" in names
    # The restricted column name must not appear anywhere in the payload.
    assert all(m["source_column_name"] != "salary_col" for m in body)


@pytest.mark.asyncio
async def test_get_measure_404_for_restricted_column(client, as_viewer):
    """The single measure GET must fail-closed (404) when the measure is backed
    by a restricted column — it would otherwise echo source_column_name."""
    restricted_col = uuid.uuid4()
    measure = _measure(source_column_id=restricted_col, name="Salary")
    persona = types.SimpleNamespace(
        id=uuid.uuid4(), included_measure_ids=[], included_dimension_ids=[],
    )
    db = make_mock_db()
    db.get = AsyncMock(return_value=measure)

    with (
        patch("src.api.measures.get_tenant_db", async_gen_from(db)),
        patch("src.api.measures.ensure_model_in_project", new=AsyncMock()),
        patch(
            "src.api.measures.resolve_effective_persona",
            new=AsyncMock(return_value=persona),
        ),
        patch(
            "src.api.measures.get_restricted_column_ids",
            new=AsyncMock(return_value={restricted_col}),
        ),
    ):
        resp = await client.get(f"{PREFIX}/{measure.id}")

    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_enriched_drill_through_omits_restricted_column(client, as_viewer):
    """Bug-6141 sibling: the enriched drill-through set resolves detail-column
    ids to column NAMES. A restricted persona must not learn a restricted
    column name here — it is omitted from the enriched payload."""
    restricted_col = uuid.uuid4()
    visible_col = uuid.uuid4()
    measure = _measure(source_column_id=uuid.uuid4())
    drill = types.SimpleNamespace(
        id=uuid.uuid4(), measure_id=measure.id, source_table_id=uuid.uuid4(),
        # JSONB stores detail_columns as list[str] (mirror production storage so
        # the str-vs-UUID normalization is actually exercised, not masked).
        detail_columns=[str(restricted_col), str(visible_col)],
        joined_dimension_ids=[], row_limit_override=None, source_join_path=None,
    )
    persona = types.SimpleNamespace(
        id=uuid.uuid4(), included_measure_ids=[], included_dimension_ids=[],
    )

    db = make_mock_db()

    async def _get(cls, pk):
        # detail_columns arrive as JSONB strings, so db.get receives a str pk.
        if cls.__name__ == "ModelColumn" and str(pk) == str(visible_col):
            return _col(visible_col, "amount_col")
        # A restricted column must never be fetched (it is skipped first).
        if cls.__name__ == "ModelColumn" and str(pk) == str(restricted_col):
            return _col(restricted_col, "salary_col")
        return None

    db.get = AsyncMock(side_effect=_get)

    with (
        patch("src.api.measures.get_tenant_db", async_gen_from(db)),
        patch("src.api.measures.ensure_model_in_project", new=AsyncMock()),
        patch(
            "src.api.measures._load_drill_through_set_or_404",
            new=AsyncMock(return_value=(measure, drill)),
        ),
        patch(
            "src.api.measures.resolve_effective_persona",
            new=AsyncMock(return_value=persona),
        ),
        patch(
            "src.api.measures.get_restricted_column_ids",
            new=AsyncMock(return_value={restricted_col}),
        ),
    ):
        resp = await client.get(f"{PREFIX}/{measure.id}/drill-through-set/enriched")

    assert resp.status_code == 200, resp.text
    cols = resp.json()["detail_columns"]
    ids = {c["id"] for c in cols}
    names = {c["name"] for c in cols}
    assert str(visible_col) in ids
    assert str(restricted_col) not in ids
    assert "salary_col" not in names  # restricted column name never disclosed


@pytest.mark.parametrize("suffix", ["", "/enriched"])
@pytest.mark.asyncio
async def test_drill_through_404_for_restricted_measure(client, as_viewer, suffix):
    """Bug-6614: a measure hidden by CLS on get_measure must also 404 on both
    drill-through surfaces — they must not disclose its existence/config."""
    restricted_col = uuid.uuid4()
    measure = _measure(source_column_id=restricted_col, name="Salary")
    drill = types.SimpleNamespace(
        id=uuid.uuid4(), measure_id=measure.id, source_table_id=uuid.uuid4(),
        detail_columns=[], joined_dimension_ids=[], row_limit_override=None,
        source_join_path=None,
    )
    persona = types.SimpleNamespace(
        id=uuid.uuid4(), included_measure_ids=[], included_dimension_ids=[],
    )
    db = make_mock_db()

    with (
        patch("src.api.measures.get_tenant_db", async_gen_from(db)),
        patch("src.api.measures.ensure_model_in_project", new=AsyncMock()),
        patch(
            "src.api.measures._load_drill_through_set_or_404",
            new=AsyncMock(return_value=(measure, drill)),
        ),
        patch(
            "src.api.measures.resolve_effective_persona",
            new=AsyncMock(return_value=persona),
        ),
        patch(
            "src.api.measures.get_restricted_column_ids",
            new=AsyncMock(return_value={restricted_col}),
        ),
    ):
        resp = await client.get(f"{PREFIX}/{measure.id}/drill-through-set{suffix}")

    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_available_variants_404_for_restricted_measure(client, as_viewer):
    """Bug-6141 sibling: available-variants echoes the base measure name (often
    the restricted column name) — must 404 for a CLS-restricted measure."""
    restricted_col = uuid.uuid4()
    measure = _measure(source_column_id=restricted_col, name="Salary")
    persona = types.SimpleNamespace(
        id=uuid.uuid4(), included_measure_ids=[], included_dimension_ids=[],
    )
    db = make_mock_db()
    db.get = AsyncMock(return_value=measure)
    with (
        patch("src.api.measures.get_tenant_db", async_gen_from(db)),
        patch("src.api.measures.ensure_model_in_project", new=AsyncMock()),
        patch(
            "src.api.measures.resolve_effective_persona",
            new=AsyncMock(return_value=persona),
        ),
        patch(
            "src.api.measures.get_restricted_column_ids",
            new=AsyncMock(return_value={restricted_col}),
        ),
    ):
        resp = await client.get(f"{PREFIX}/{measure.id}/available-variants")
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_join_paths_404_for_restricted_measure(client, as_viewer):
    """Bug-6614 sibling: join-paths must 404 for a CLS-restricted measure rather
    than disclose its existence + join topology."""
    restricted_col = uuid.uuid4()
    measure = _measure(source_column_id=restricted_col, name="Salary")
    drill = types.SimpleNamespace(
        id=uuid.uuid4(), measure_id=measure.id, source_table_id=uuid.uuid4(),
        detail_columns=[], joined_dimension_ids=[], row_limit_override=None,
        source_join_path=None,
    )
    persona = types.SimpleNamespace(
        id=uuid.uuid4(), included_measure_ids=[], included_dimension_ids=[],
    )
    db = make_mock_db()
    with (
        patch("src.api.measures.get_tenant_db", async_gen_from(db)),
        patch("src.api.measures.ensure_model_in_project", new=AsyncMock()),
        patch(
            "src.api.measures._load_drill_through_set_or_404",
            new=AsyncMock(return_value=(measure, drill)),
        ),
        patch(
            "src.api.measures.resolve_effective_persona",
            new=AsyncMock(return_value=persona),
        ),
        patch(
            "src.api.measures.get_restricted_column_ids",
            new=AsyncMock(return_value={restricted_col}),
        ),
    ):
        resp = await client.get(
            f"{PREFIX}/{measure.id}/drill-through-set/join-paths"
            f"?source_table_id={uuid.uuid4()}"
        )
    assert resp.status_code == 404, resp.text


@pytest.mark.parametrize("suffix", ["", "/enriched"])
@pytest.mark.asyncio
async def test_drill_through_404_for_measure_outside_persona_scope(client, as_viewer, suffix):
    """Bug-6614 (allow-list gate): a measure outside the persona's
    included_measure_ids must 404 on both drill surfaces, mirroring get_measure —
    otherwise its drill config / detail-column names leak for an out-of-scope
    measure."""
    measure = _measure(source_column_id=uuid.uuid4(), name="Amount")
    drill = types.SimpleNamespace(
        id=uuid.uuid4(), measure_id=measure.id, source_table_id=uuid.uuid4(),
        detail_columns=[], joined_dimension_ids=[], row_limit_override=None,
        source_join_path=None,
    )
    # Persona allows only some OTHER measure — this one is out of scope.
    persona = types.SimpleNamespace(
        id=uuid.uuid4(), included_measure_ids=[str(uuid.uuid4())],
        included_dimension_ids=[],
    )
    db = make_mock_db()

    with (
        patch("src.api.measures.get_tenant_db", async_gen_from(db)),
        patch("src.api.measures.ensure_model_in_project", new=AsyncMock()),
        patch(
            "src.api.measures._load_drill_through_set_or_404",
            new=AsyncMock(return_value=(measure, drill)),
        ),
        patch(
            "src.api.measures.resolve_effective_persona",
            new=AsyncMock(return_value=persona),
        ),
        patch(
            "src.api.measures.get_restricted_column_ids",
            new=AsyncMock(return_value=set()),
        ),
    ):
        resp = await client.get(f"{PREFIX}/{measure.id}/drill-through-set{suffix}")

    assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# Bug-6896: transitive CLS hiding — fixed-point closure
# ---------------------------------------------------------------------------


def _calc_measure(*, name, expression, source_column_id=None):
    """Build a calculated measure SimpleNamespace with an expression."""
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        model_id=TEST_MODEL_ID,
        name=name,
        display_name=name,
        description=None,
        display_folder=None,
        source_column_id=source_column_id,
        user_defined_attribute_id=None,
        measure_type="calculated",
        expression=expression,
        calc_agg_mode=None,
        data_type="numeric",
        default_agg="sum",
        format=None,
        variant_kind=None,
        variant_of_measure_id=None,
        variant_n=None,
        is_additive=True,
        semi_additive_behavior=None,
        semi_additive_account_column_id=None,
        calendar_model_table_id=None,
        hierarchy_id=None,
        date_dimension_column_id=None,
        is_invalid=False,
        invalid_reason=None,
        cross_model_source_model_id=None,
        cross_model_source_measure_id=None,
        created_at=NOW,
        updated_at=NOW,
    )


class TestTransitiveCLSHiding:
    """Bug-6896: the CLS filter must iterate to a fixed point for multi-level
    calculated measure chains.  If hidden measure A is referenced by calculated
    B, and calculated C references B, then all three must be hidden."""

    # Bug-7608 / Bug-7045: measure-reference parsing and hidden-reference
    # detection now live in the shared closure module
    # (``shared.security.restricted_column_closure``) and are exercised through
    # the real ``_compute_transitive_hidden_names`` code path below (and by
    # ``tests/unit/test_restricted_column_closure.py``). The former direct-helper
    # micro-tests were removed with the helpers they tested.

    def test_single_level_transitive_closure(self):
        """A -> B (B refs A): both hidden."""
        restricted_col = uuid.uuid4()
        a = _measure(source_column_id=restricted_col, name="A")
        b = _calc_measure(name="B", expression='measure("A") + 1')
        hidden = _compute_transitive_hidden_names([a, b], {restricted_col})
        assert hidden == {"A", "B"}

    def test_multi_level_transitive_closure(self):
        """A -> B -> C (chain of 3): all hidden."""
        restricted_col = uuid.uuid4()
        a = _measure(source_column_id=restricted_col, name="A")
        b = _calc_measure(name="B", expression='measure("A") + 1')
        c = _calc_measure(name="C", expression='measure("B") * 2')
        hidden = _compute_transitive_hidden_names([a, b, c], {restricted_col})
        assert hidden == {"A", "B", "C"}

    def test_deep_chain_transitive_closure(self):
        """A -> B -> C -> D -> E: 5-level chain."""
        restricted_col = uuid.uuid4()
        a = _measure(source_column_id=restricted_col, name="A")
        b = _calc_measure(name="B", expression='measure("A") + 1')
        c = _calc_measure(name="C", expression='measure("B") + 2')
        d = _calc_measure(name="D", expression='measure("C") + 3')
        e = _calc_measure(name="E", expression='measure("D") + 4')
        hidden = _compute_transitive_hidden_names(
            [a, b, c, d, e], {restricted_col},
        )
        assert hidden == {"A", "B", "C", "D", "E"}

    def test_cycle_terminates(self):
        """Mutual references (cycle): A -> B -> A.  Must not loop forever."""
        restricted_col = uuid.uuid4()
        a = _calc_measure(
            name="A",
            expression='measure("B") + 1',
            source_column_id=restricted_col,
        )
        b = _calc_measure(name="B", expression='measure("A") + 2')
        hidden = _compute_transitive_hidden_names([a, b], {restricted_col})
        assert hidden == {"A", "B"}

    def test_unrelated_measures_not_hidden(self):
        """Only measures reachable from a restricted base are hidden."""
        restricted_col = uuid.uuid4()
        a = _measure(source_column_id=restricted_col, name="A")
        b = _calc_measure(name="B", expression='measure("A") * 2')
        independent = _measure(source_column_id=uuid.uuid4(), name="Ind")
        ind_calc = _calc_measure(
            name="IndCalc", expression='measure("Ind") + 5',
        )
        hidden = _compute_transitive_hidden_names(
            [a, b, independent, ind_calc], {restricted_col},
        )
        assert hidden == {"A", "B"}

    def test_no_restricted_columns_returns_empty(self):
        """No restricted columns means nothing hidden."""
        a = _measure(source_column_id=uuid.uuid4(), name="A")
        hidden = _compute_transitive_hidden_names([a], set())
        assert hidden == set()

    @pytest.mark.asyncio
    async def test_list_measures_hides_transitive_chain(self, client, as_viewer):
        """Bug-6896 regression: list_measures must hide the full transitive
        chain A -> B -> C when A is backed by a restricted column."""
        restricted_col = uuid.uuid4()
        visible_col = uuid.uuid4()
        a = _measure(source_column_id=restricted_col, name="A_Hidden")
        b = _calc_measure(name="B_DerivedFromA", expression='measure("A_Hidden") + 1')
        c = _calc_measure(
            name="C_DerivedFromB", expression='measure("B_DerivedFromA") * 2',
        )
        visible = _measure(source_column_id=visible_col, name="Visible")

        persona = types.SimpleNamespace(
            id=uuid.uuid4(), included_measure_ids=[],
            included_dimension_ids=[],
        )

        all_measures = [a, b, c, visible]

        db = make_mock_db()
        db.execute = AsyncMock(return_value=_ScalarResult(all_measures))

        async def _get(cls, pk):
            if cls.__name__ == "ModelColumn":
                if pk == visible_col:
                    return _col(visible_col, "amount_col")
            return None

        db.get = AsyncMock(side_effect=_get)

        with (
            patch("src.api.measures.get_tenant_db", async_gen_from(db)),
            patch("src.api.measures.ensure_model_in_project", new=AsyncMock()),
            patch(
                "src.api.measures.resolve_effective_persona",
                new=AsyncMock(return_value=persona),
            ),
            patch(
                "src.api.measures.get_restricted_column_ids",
                new=AsyncMock(return_value={restricted_col}),
            ),
            patch(
                "src.api.measures._load_uda_column_map",
                new=AsyncMock(return_value={}),
            ),
            patch(
                "src.api.measures._load_redundant_partners",
                new=AsyncMock(return_value={}),
            ),
            patch(
                "src.api.measures._glossary_texts_for_targets",
                new=AsyncMock(return_value={}),
            ),
        ):
            resp = await client.get(PREFIX)

        assert resp.status_code == 200, resp.text
        body = resp.json()
        names = {m["name"] for m in body}
        # A is directly restricted, B references A, C references B — all hidden.
        assert "A_Hidden" not in names
        assert "B_DerivedFromA" not in names
        assert "C_DerivedFromB" not in names
        # Unrelated measure stays visible.
        assert "Visible" in names
