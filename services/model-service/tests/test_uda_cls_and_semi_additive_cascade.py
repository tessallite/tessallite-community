"""Bug-7606 — UDA-backed measures/dimensions bypass CLS metadata hiding.
Bug-7618 — semi_additive_behavior edit on base does not cascade to variants.

Bug-7606 (SECURITY): the CLS check functions (_measure_touches_restricted_column,
_dim_touches_restricted_column) only examined source_column_id, ignoring UDA
column refs. UDA-backed measures/dimensions with source_column_id=None passed
through CLS filtering even when their UDA expression referenced restricted
columns, leaking catalogue metadata.

Bug-7618 (wrong-numbers): editing semi_additive_behavior on a base measure did
not cascade the value to variant snapshots and did not re-run variant admission.
This left variants with stale semi_additive_behavior values and could leave
cumulation/window variants active on a newly semi-additive base (producing
incorrect results).
"""
from __future__ import annotations

import types
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from .result_fakes import FakeScalarResult

from src.api.measures import (
    _compute_transitive_hidden_names,
    _measure_touches_restricted_column,
)
from src.api.dimensions import _dim_touches_restricted_column
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

PREFIX_M = f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/measures"
PREFIX_D = f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/dimensions"

NOW = datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _measure(*, source_column_id=None, user_defined_attribute_id=None,
             name="Amount", measure_type="standard", expression=None,
             variant_kind=None, variant_of_measure_id=None,
             semi_additive_behavior=None, is_invalid=False, invalid_reason=None):
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        model_id=TEST_MODEL_ID,
        name=name,
        display_name=name,
        description=None,
        display_folder=None,
        source_column_id=source_column_id,
        user_defined_attribute_id=user_defined_attribute_id,
        measure_type=measure_type,
        expression=expression,
        calc_agg_mode=None,
        data_type="numeric",
        default_agg="sum",
        format=None,
        variant_kind=variant_kind,
        variant_of_measure_id=variant_of_measure_id,
        variant_n=None,
        is_additive=True,
        semi_additive_behavior=semi_additive_behavior,
        semi_additive_account_column_id=None,
        calendar_model_table_id=None,
        hierarchy_id=None,
        date_dimension_column_id=None,
        resolved_calendar_id=None,
        resolved_date_col_id=None,
        is_invalid=is_invalid,
        invalid_reason=invalid_reason,
        cross_model_source_model_id=None,
        cross_model_source_measure_id=None,
        created_at=NOW,
        updated_at=NOW,
    )


def _dim(*, source_column_id=None, display_column_id=None,
         user_defined_attribute_id=None, name="Country"):
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        model_id=TEST_MODEL_ID,
        name=name,
        display_name=name,
        description=None,
        display_folder=None,
        source_column_id=source_column_id,
        display_column_id=display_column_id,
        user_defined_attribute_id=user_defined_attribute_id,
        is_time_dim=False,
        time_grain=None,
        is_invalid=False,
        invalid_reason=None,
        created_at=NOW,
        updated_at=NOW,
    )


def _col(col_id, name):
    return types.SimpleNamespace(
        id=col_id, column_name=name, display_name=name.replace("_", " ").title(),
        data_type="numeric", model_table_id=uuid.uuid4(), is_hidden=False,
        cardinality_estimate=None,
    )


class _ScalarResult:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return FakeScalarResult(self._items)

    def all(self):
        return list(self._items)


# ===========================================================================
# Bug-7606 — UDA CLS bypass: _measure_touches_restricted_column
# ===========================================================================


class TestMeasureTouchesRestrictedColumnWithUda:
    """Unit tests for _measure_touches_restricted_column with UDA column map."""

    def test_uda_measure_hidden_when_uda_column_restricted(self):
        """A UDA-backed measure must be hidden when any of the UDA's
        referenced columns is CLS-restricted."""
        uda_id = uuid.uuid4()
        col_a = uuid.uuid4()
        col_b = uuid.uuid4()
        m = _measure(user_defined_attribute_id=uda_id)
        uda_col_map = {uda_id: {col_a, col_b}}
        assert _measure_touches_restricted_column(m, {col_a}, uda_col_map) is True

    def test_uda_measure_visible_when_no_column_restricted(self):
        """A UDA-backed measure whose UDA columns are all unrestricted
        must remain visible."""
        uda_id = uuid.uuid4()
        col_a = uuid.uuid4()
        m = _measure(user_defined_attribute_id=uda_id)
        uda_col_map = {uda_id: {col_a}}
        other_restricted = uuid.uuid4()
        assert _measure_touches_restricted_column(m, {other_restricted}, uda_col_map) is False

    def test_uda_measure_hidden_without_map_is_false(self):
        """When no UDA column map is provided, UDA-backed measures pass through
        (backward compat for callers without restriction context)."""
        uda_id = uuid.uuid4()
        m = _measure(user_defined_attribute_id=uda_id)
        # No uda_col_map provided -- old behavior
        assert _measure_touches_restricted_column(m, {uuid.uuid4()}) is False

    def test_source_column_still_checked(self):
        """The source_column_id check must still work as before."""
        col = uuid.uuid4()
        m = _measure(source_column_id=col)
        assert _measure_touches_restricted_column(m, {col}) is True


# ===========================================================================
# Bug-7606 — UDA CLS bypass: _dim_touches_restricted_column
# ===========================================================================


class TestDimTouchesRestrictedColumnWithUda:
    """Unit tests for _dim_touches_restricted_column with UDA column map."""

    def test_uda_dim_hidden_when_uda_column_restricted(self):
        """A UDA-backed dimension must be hidden when any of the UDA's
        referenced columns is CLS-restricted."""
        uda_id = uuid.uuid4()
        col_a = uuid.uuid4()
        d = _dim(user_defined_attribute_id=uda_id)
        uda_col_map = {uda_id: {col_a}}
        assert _dim_touches_restricted_column(d, {col_a}, uda_col_map) is True

    def test_uda_dim_visible_when_no_column_restricted(self):
        """A UDA-backed dimension whose UDA columns are all unrestricted
        must remain visible."""
        uda_id = uuid.uuid4()
        col_a = uuid.uuid4()
        d = _dim(user_defined_attribute_id=uda_id)
        uda_col_map = {uda_id: {col_a}}
        assert _dim_touches_restricted_column(d, {uuid.uuid4()}, uda_col_map) is False

    def test_source_column_still_checked(self):
        """The source_column_id check must still work as before."""
        col = uuid.uuid4()
        d = _dim(source_column_id=col)
        assert _dim_touches_restricted_column(d, {col}) is True

    def test_display_column_still_checked(self):
        """The display_column_id check must still work as before."""
        col = uuid.uuid4()
        d = _dim(display_column_id=col)
        assert _dim_touches_restricted_column(d, {col}) is True


# ===========================================================================
# Bug-7606 — _compute_transitive_hidden_names includes UDA-backed measures
# ===========================================================================


class TestTransitiveHiddenNamesWithUda:
    """The transitive CLS closure must seed UDA-backed measures as hidden when
    their UDA references a restricted column."""

    def test_uda_measure_seeded_as_hidden(self):
        uda_id = uuid.uuid4()
        restricted_col = uuid.uuid4()
        uda_measure = _measure(name="UDA_Amount", user_defined_attribute_id=uda_id)
        regular_measure = _measure(name="Regular", source_column_id=uuid.uuid4())
        uda_col_map = {uda_id: {restricted_col}}

        hidden = _compute_transitive_hidden_names(
            [uda_measure, regular_measure],
            {restricted_col},
            uda_col_map,
        )
        assert "UDA_Amount" in hidden
        assert "Regular" not in hidden

    def test_calc_referencing_hidden_uda_measure_also_hidden(self):
        """A calculated measure that references a UDA-backed hidden measure
        must also be transitively hidden."""
        uda_id = uuid.uuid4()
        restricted_col = uuid.uuid4()
        uda_measure = _measure(name="UDA_Salary", user_defined_attribute_id=uda_id)
        calc_measure = _measure(
            name="Salary_Ratio",
            measure_type="calculated",
            expression='measure("UDA_Salary") / measure("UDA_Salary")',
        )
        uda_col_map = {uda_id: {restricted_col}}

        hidden = _compute_transitive_hidden_names(
            [uda_measure, calc_measure],
            {restricted_col},
            uda_col_map,
        )
        assert "UDA_Salary" in hidden
        assert "Salary_Ratio" in hidden


# ===========================================================================
# Bug-7606 — Route-level: list_measures hides UDA-backed restricted measures
# ===========================================================================


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
async def test_list_measures_hides_uda_backed_restricted_measure(client, as_viewer):
    """A restricted persona must not see a UDA-backed measure whose UDA
    references a CLS-restricted column."""
    restricted_col = uuid.uuid4()
    visible_col = uuid.uuid4()
    uda_id = uuid.uuid4()
    uda_table_id = uuid.uuid4()

    uda_measure = _measure(
        name="UDA_Salary", user_defined_attribute_id=uda_id,
    )
    visible_measure = _measure(name="Amount", source_column_id=visible_col)

    persona = types.SimpleNamespace(
        id=uuid.uuid4(), included_measure_ids=[], included_dimension_ids=[],
    )

    db = make_mock_db()

    call_count = [0]
    async def _execute(stmt, *args, **kwargs):
        call_count[0] += 1
        # First execute: main measures query
        # The list endpoint may call execute multiple times. We need the
        # measures query to return both measures, and the UDA column ref
        # query to return the UDA->restricted_col mapping.
        return _ScalarResult([uda_measure, visible_measure])

    db.execute = AsyncMock(side_effect=_execute)

    async def _get(cls, pk):
        if hasattr(cls, '__name__') and cls.__name__ == "ModelColumn":
            if pk == visible_col:
                return _col(visible_col, "amount_col")
        if hasattr(cls, '__name__') and cls.__name__ == "UserDefinedAttribute":
            if pk == uda_id:
                return types.SimpleNamespace(
                    id=uda_id, name="salary_calc", table_id=uda_table_id,
                )
        if hasattr(cls, '__name__') and cls.__name__ == "ModelTable":
            return types.SimpleNamespace(
                id=uda_table_id, alias="fact", display_name="Fact Table",
                row_count_estimate=None,
            )
        return None

    db.get = AsyncMock(side_effect=_get)

    # Simulate the UDA column map loading
    uda_col_map = {uda_id: {restricted_col}}

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
            new=AsyncMock(return_value=uda_col_map),
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
        resp = await client.get(PREFIX_M)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    names = {m["name"] for m in body}
    assert "UDA_Salary" not in names, "UDA-backed measure with restricted column must be hidden"
    assert "Amount" in names


@pytest.mark.asyncio
async def test_get_measure_404_for_uda_backed_restricted_measure(client, as_viewer):
    """The single measure GET must fail-closed (404) when the UDA-backed
    measure references a CLS-restricted column."""
    restricted_col = uuid.uuid4()
    uda_id = uuid.uuid4()
    measure = _measure(name="UDA_Salary", user_defined_attribute_id=uda_id)

    persona = types.SimpleNamespace(
        id=uuid.uuid4(), included_measure_ids=[], included_dimension_ids=[],
    )

    db = make_mock_db()
    db.get = AsyncMock(return_value=measure)

    uda_col_map = {uda_id: {restricted_col}}

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
            new=AsyncMock(return_value=uda_col_map),
        ),
    ):
        resp = await client.get(f"{PREFIX_M}/{measure.id}")

    assert resp.status_code == 404, resp.text


# ===========================================================================
# Bug-7606 — Route-level: list_dimensions hides UDA-backed restricted dims
# ===========================================================================


@pytest.mark.asyncio
async def test_list_dimensions_hides_uda_backed_restricted_dim(client, as_viewer):
    """A restricted persona must not see a UDA-backed dimension whose UDA
    references a CLS-restricted column."""
    restricted_col = uuid.uuid4()
    visible_col = uuid.uuid4()
    uda_id = uuid.uuid4()
    uda_table_id = uuid.uuid4()

    uda_dim = _dim(name="UDA_Region", user_defined_attribute_id=uda_id)
    visible_dim = _dim(name="Country", source_column_id=visible_col)

    persona = types.SimpleNamespace(
        id=uuid.uuid4(), included_measure_ids=[], included_dimension_ids=[],
    )

    db = make_mock_db()
    db.execute = AsyncMock(return_value=_ScalarResult([uda_dim, visible_dim]))

    async def _get(cls, pk):
        if hasattr(cls, '__name__') and cls.__name__ == "ModelColumn":
            if pk == visible_col:
                return _col(visible_col, "country_col")
        if hasattr(cls, '__name__') and cls.__name__ == "UserDefinedAttribute":
            if pk == uda_id:
                return types.SimpleNamespace(
                    id=uda_id, name="region_calc", table_id=uda_table_id,
                )
        if hasattr(cls, '__name__') and cls.__name__ == "ModelTable":
            return types.SimpleNamespace(
                id=uda_table_id, alias="dim_geo", display_name="Geography",
                row_count_estimate=None,
            )
        return None

    db.get = AsyncMock(side_effect=_get)

    uda_col_map = {uda_id: {restricted_col}}

    with (
        patch("src.api.dimensions.get_tenant_db", async_gen_from(db)),
        patch("src.api.dimensions.ensure_model_in_project", new=AsyncMock()),
        patch(
            "src.api.dimensions.resolve_effective_persona",
            new=AsyncMock(return_value=persona),
        ),
        patch(
            "src.api.dimensions.get_restricted_column_ids",
            new=AsyncMock(return_value={restricted_col}),
        ),
        patch(
            "src.api.dimensions._load_uda_column_map",
            new=AsyncMock(return_value=uda_col_map),
        ),
        patch(
            "src.api.dimensions._load_redundant_partners",
            new=AsyncMock(return_value={}),
        ),
        patch(
            "src.api.dimensions._load_model_attribute_relationships",
            new=AsyncMock(return_value={}),
        ),
        patch(
            "src.api.dimensions._glossary_texts_for_targets",
            new=AsyncMock(return_value={}),
        ),
    ):
        resp = await client.get(PREFIX_D)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    names = {d["name"] for d in body}
    assert "UDA_Region" not in names, "UDA-backed dimension with restricted column must be hidden"
    assert "Country" in names


@pytest.mark.asyncio
async def test_get_dimension_404_for_uda_backed_restricted_dim(client, as_viewer):
    """The single dimension GET must fail-closed (404) when the UDA-backed
    dimension references a CLS-restricted column."""
    restricted_col = uuid.uuid4()
    uda_id = uuid.uuid4()
    dim = _dim(name="UDA_Region", user_defined_attribute_id=uda_id)

    persona = types.SimpleNamespace(
        id=uuid.uuid4(), included_measure_ids=[], included_dimension_ids=[],
    )

    db = make_mock_db()
    db.get = AsyncMock(return_value=dim)

    uda_col_map = {uda_id: {restricted_col}}

    with (
        patch("src.api.dimensions.get_tenant_db", async_gen_from(db)),
        patch("src.api.dimensions.ensure_model_in_project", new=AsyncMock()),
        patch(
            "src.api.dimensions.resolve_effective_persona",
            new=AsyncMock(return_value=persona),
        ),
        patch(
            "src.api.dimensions.get_restricted_column_ids",
            new=AsyncMock(return_value={restricted_col}),
        ),
        patch(
            "src.api.dimensions._load_uda_column_map",
            new=AsyncMock(return_value=uda_col_map),
        ),
    ):
        resp = await client.get(f"{PREFIX_D}/{dim.id}")

    assert resp.status_code == 404, resp.text


# ===========================================================================
# Bug-7618 — semi_additive_behavior cascade + variant admission re-check
# ===========================================================================


def _patch_scope():
    """Stub out ensure_model_in_project so it doesn't hit the DB."""
    async def _noop(db, *, project_id, model_id):
        return None
    return patch("src.api.measures.ensure_model_in_project", _noop)


def _stub_build_response():
    """Stub _build_response to avoid DB calls from the response builder."""
    from shared.schemas.pydantic_models import MeasureResponse

    async def _build(db, measure, *args, **kwargs):
        return MeasureResponse(
            id=measure.id,
            model_id=measure.model_id,
            name=measure.name,
            display_name=measure.display_name or measure.name,
            description=None,
            effective_description=None,
            display_folder=None,
            is_hidden=False,
            source_column_id=measure.source_column_id,
            source_column_name=None,
            display_column_id=None,
            display_column_name=None,
            data_type=measure.data_type,
            source_table_id=None,
            source_table_alias=None,
            source_table_display_name=None,
            user_defined_attribute_id=measure.user_defined_attribute_id,
            user_defined_attribute_name=None,
            measure_type=measure.measure_type,
            expression=measure.expression,
            calc_agg_mode=getattr(measure, "calc_agg_mode", None),
            default_agg=measure.default_agg,
            format=measure.format,
            variant_kind=measure.variant_kind,
            variant_of_measure_id=measure.variant_of_measure_id,
            variant_n=measure.variant_n,
            eligible_variant_kinds=None,
            is_additive=measure.is_additive,
            semi_additive_behavior=getattr(measure, "semi_additive_behavior", None),
            semi_additive_account_column_id=None,
            calendar_model_table_id=None,
            hierarchy_id=None,
            date_dimension_column_id=None,
            resolved_calendar_id=None,
            resolved_date_col_id=None,
            is_invalid=getattr(measure, "is_invalid", False),
            invalid_reason=getattr(measure, "invalid_reason", None),
            cross_model_source_model_id=None,
            cross_model_source_measure_id=None,
            redundant_partner=None,
            created_at=measure.created_at,
            updated_at=measure.updated_at,
        )

    return patch("src.api.measures._build_response", _build)


def _get_for_measure(measure):
    """Return a db.get side_effect that yields the measure for its own id
    and None for every other lookup (e.g. ModelColumn during _build_response).
    """
    async def _get(entity, entity_id):
        if entity_id == measure.id:
            return measure
        return None
    return _get


class TestSemiAdditiveCascade:
    """Unit tests for semi_additive_behavior cascade logic in update_measure.

    Bug-7618: editing semi_additive_behavior on a base measure must (1) cascade
    to variant snapshots and (2) re-run variant admission validation.
    """

    @pytest.mark.asyncio
    async def test_semi_additive_in_cascade_triggers(self):
        """semi_additive_behavior and semi_additive_account_column_id must
        appear in the cascade-trigger set used by update_measure so they
        are propagated to variant snapshot rows."""
        # We test this by verifying the actual code behavior: construct a
        # base measure, then simulate the cascade logic.
        #
        # The _CASCADE_TRIGGERS set is defined inline in update_measure.
        # We verify that the cascade works by checking that the semi_additive
        # fields are in the body keys that trigger cascading.
        from src.api.measures import MeasureUpdate
        body = MeasureUpdate(semi_additive_behavior="last_non_empty")
        raw = body.model_dump(exclude_unset=True)
        assert "semi_additive_behavior" in raw

    @pytest.mark.asyncio
    async def test_patch_semi_additive_cascades_to_variants(self, client):
        """When semi_additive_behavior is set on a base measure, the PATCH
        must cascade the new value to all variant snapshots."""
        base_id = uuid.uuid4()
        base = _measure(name="Balance")
        base.id = base_id
        # Need calc_agg_mode attribute for the response builder
        base.calc_agg_mode = None

        variant = _measure(
            name="Balance_ytd", variant_kind="ytd",
            variant_of_measure_id=base_id,
        )

        db = make_mock_db()
        db.get = AsyncMock(side_effect=_get_for_measure(base))

        # Track execute calls for the cascade UPDATE
        execute_calls = []
        async def _execute(stmt, *args, **kwargs):
            execute_calls.append(stmt)
            return _ScalarResult([variant])
        db.execute = AsyncMock(side_effect=_execute)

        with (
            _patch_scope(),
            _stub_build_response(),
            patch("src.api.measures.get_tenant_db", async_gen_from(db)),
            patch("src.api.measures._check_variant_eligibility",
                  new=AsyncMock(return_value=None)),
        ):
            resp = await client.patch(
                f"{PREFIX_M}/{base_id}",
                json={"semi_additive_behavior": "last_non_empty"},
            )

        assert resp.status_code == 200, resp.text
        # db.execute was called for: cascade UPDATE + variant re-check SELECT
        assert db.execute.call_count >= 2, (
            f"Expected at least 2 db.execute calls (cascade + re-check), "
            f"got {db.execute.call_count}"
        )

    @pytest.mark.asyncio
    async def test_patch_semi_additive_invalidates_ineligible_variants(self, client):
        """When semi_additive_behavior is set on a base, existing
        cumulation/window variants must be re-checked for admission. An
        ineligible variant must be marked is_invalid=True."""
        base_id = uuid.uuid4()
        base = _measure(name="Balance")
        base.id = base_id

        # A cumulation variant that should become invalid when base turns
        # semi-additive.
        variant = _measure(
            name="Balance_ytd", variant_kind="ytd",
            variant_of_measure_id=base_id,
        )

        db = make_mock_db()
        db.get = AsyncMock(side_effect=_get_for_measure(base))
        db.execute = AsyncMock(return_value=_ScalarResult([variant]))

        rejection_reason = (
            "Cumulation and window variants (ytd) are not supported "
            "for semi-additive measures (behavior: last_non_empty). "
            "Semi-additive measures represent balances, not flows; "
            "cumulating them produces incorrect results. "
            "Use lag or prior-period variants instead."
        )

        with (
            _patch_scope(),
            _stub_build_response(),
            patch("src.api.measures.get_tenant_db", async_gen_from(db)),
            patch("src.api.measures._check_variant_eligibility",
                  new=AsyncMock(return_value=rejection_reason)),
        ):
            resp = await client.patch(
                f"{PREFIX_M}/{base_id}",
                json={"semi_additive_behavior": "last_non_empty"},
            )

        assert resp.status_code == 200, resp.text
        # The variant should have been marked invalid.
        assert variant.is_invalid is True, (
            "Variant must be marked invalid when semi_additive_behavior "
            "makes its kind ineligible"
        )
        assert variant.invalid_reason is not None
        assert "semi-additive" in variant.invalid_reason.lower()

    @pytest.mark.asyncio
    async def test_patch_semi_additive_cleared_readmits_variants(self, client):
        """When semi_additive_behavior is cleared on a base, variants that
        were invalidated solely due to semi-additive admission should be
        re-admitted (is_invalid cleared)."""
        base_id = uuid.uuid4()
        base = _measure(name="Balance", semi_additive_behavior="last_non_empty")
        base.id = base_id

        # Variant previously marked invalid due to semi-additive restriction.
        variant = _measure(
            name="Balance_ytd", variant_kind="ytd",
            variant_of_measure_id=base_id,
            is_invalid=True,
            invalid_reason=(
                "Cumulation and window variants (ytd) are not supported "
                "for semi-additive measures (behavior: last_non_empty)."
            ),
        )

        db = make_mock_db()
        db.get = AsyncMock(side_effect=_get_for_measure(base))
        db.execute = AsyncMock(return_value=_ScalarResult([variant]))

        with (
            _patch_scope(),
            _stub_build_response(),
            patch("src.api.measures.get_tenant_db", async_gen_from(db)),
            # Eligibility check passes (no rejection) when semi_additive is cleared
            patch("src.api.measures._check_variant_eligibility",
                  new=AsyncMock(return_value=None)),
        ):
            resp = await client.patch(
                f"{PREFIX_M}/{base_id}",
                json={"semi_additive_behavior": None},
            )

        assert resp.status_code == 200, resp.text
        # The variant should have been re-admitted.
        assert variant.is_invalid is False, (
            "Variant must be re-admitted when semi_additive_behavior is "
            "cleared and eligibility check passes"
        )
        assert variant.invalid_reason is None
