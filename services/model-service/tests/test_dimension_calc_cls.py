"""Bug-7607 (catalogue half) — calculated dimensions referencing a restricted
column must be HIDDEN from a persona lacking access, so the restricted column
NAME does not leak via the dimension catalogue.

The query-router already blocks the VALUES at serving (runtime half, closed in
the engine lane). This suite covers the model-service CATALOGUE half:
``_dim_touches_restricted_column`` must parse a calc dimension's
``calc_expression`` (mirroring ``router._touches_restricted_columns``) and fail
closed on a restricted-name reference, a whole-row/table reference, an unknown
identifier, a star, or a parse error.
"""
from __future__ import annotations

import types
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from .result_fakes import FakeScalarResult

from src.api.dimensions import (
    _CalcClsContext,
    _calc_expression_touches_restricted,
    _dim_touches_restricted_column,
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

PREFIX_D = f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/dimensions"

NOW = datetime.now(timezone.utc)


def _calc_dim(*, calc_expression, name="Sensitive"):
    """A calculated dimension: no physical key/display column, only an
    expression that references physical columns by name."""
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        model_id=TEST_MODEL_ID,
        name=name,
        display_name=name,
        description=None,
        display_folder=None,
        source_column_id=None,
        display_column_id=None,
        user_defined_attribute_id=None,
        calc_expression=calc_expression,
        is_time_dim=False,
        time_grain=None,
        is_invalid=False,
        invalid_reason=None,
        created_at=NOW,
        updated_at=NOW,
    )


def _plain_dim(*, source_column_id, name="Country"):
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        model_id=TEST_MODEL_ID,
        name=name,
        display_name=name,
        description=None,
        display_folder=None,
        source_column_id=source_column_id,
        display_column_id=None,
        user_defined_attribute_id=None,
        calc_expression=None,
        is_time_dim=False,
        time_grain=None,
        is_invalid=False,
        invalid_reason=None,
        created_at=NOW,
        updated_at=NOW,
    )


def _ctx(*, restricted, known, tables=()):
    return _CalcClsContext(
        restricted_names={n.lower() for n in restricted},
        known_names={n.lower() for n in known},
        table_identifiers={n.lower() for n in tables},
    )


# ===========================================================================
# Expression-level gate (the security decision)
# ===========================================================================


class TestCalcExpressionGate:
    def test_restricted_name_reference_is_blocked(self):
        ctx = _ctx(restricted={"salary"}, known={"salary", "dept"})
        assert _calc_expression_touches_restricted("salary * 2", ctx) is True

    def test_clean_reference_is_allowed(self):
        ctx = _ctx(restricted={"salary"}, known={"salary", "dept", "region"})
        assert _calc_expression_touches_restricted("dept || region", ctx) is False

    def test_star_is_blocked(self):
        ctx = _ctx(restricted={"salary"}, known={"salary", "dept"})
        assert _calc_expression_touches_restricted("count(*)", ctx) is True

    def test_qualified_star_is_blocked(self):
        ctx = _ctx(restricted={"salary"}, known={"dept"}, tables={"emp"})
        assert _calc_expression_touches_restricted("emp.*", ctx) is True

    def test_table_reference_is_blocked(self):
        # to_jsonb(emp) serialises the whole row incl. restricted columns even
        # though 'emp' is a table identifier, not a column.
        ctx = _ctx(restricted={"salary"}, known={"dept", "region"}, tables={"emp"})
        assert _calc_expression_touches_restricted("to_jsonb(emp)", ctx) is True

    def test_unknown_identifier_is_blocked(self):
        # An identifier that is not a known column may be a whole-row/table
        # reference the rewriter leaves verbatim -> fail closed.
        ctx = _ctx(restricted={"salary"}, known={"dept", "region"})
        assert _calc_expression_touches_restricted("CAST(emp AS TEXT)", ctx) is True

    def test_unparseable_expression_is_blocked(self):
        ctx = _ctx(restricted={"salary"}, known={"salary", "dept"})
        assert _calc_expression_touches_restricted("SELECT FROM WHERE ((", ctx) is True

    def test_table_collision_name_still_blocked(self):
        # A same-named column exists but the identifier also matches a table
        # name/alias -> treat as whole-row reference and fail closed.
        ctx = _ctx(
            restricted={"salary"}, known={"orders", "amount"}, tables={"orders"}
        )
        assert _calc_expression_touches_restricted("to_jsonb(orders)", ctx) is True


# ===========================================================================
# Predicate-level: _dim_touches_restricted_column with calc dimensions
# ===========================================================================


class TestDimTouchesRestrictedCalc:
    def test_calc_dim_hidden_when_expression_references_restricted(self):
        restricted_col = uuid.uuid4()
        d = _calc_dim(calc_expression="salary * 1.1")
        ctx = _ctx(restricted={"salary"}, known={"salary", "dept"})
        assert _dim_touches_restricted_column(
            d, {restricted_col}, None, ctx
        ) is True

    def test_calc_dim_visible_when_expression_is_clean(self):
        restricted_col = uuid.uuid4()
        d = _calc_dim(calc_expression="dept || '-' || region")
        ctx = _ctx(restricted={"salary"}, known={"salary", "dept", "region"})
        assert _dim_touches_restricted_column(
            d, {restricted_col}, None, ctx
        ) is False

    def test_calc_dim_fails_closed_without_context(self):
        """A calc dimension reached under an active restriction but with no
        name context cannot be verified -> fail closed (hidden)."""
        restricted_col = uuid.uuid4()
        d = _calc_dim(calc_expression="salary * 2")
        assert _dim_touches_restricted_column(
            d, {restricted_col}, None, None
        ) is True

    def test_no_restriction_short_circuits(self):
        d = _calc_dim(calc_expression="salary * 2")
        assert _dim_touches_restricted_column(d, set()) is False

    def test_plain_source_column_dim_unaffected(self):
        col = uuid.uuid4()
        d = _plain_dim(source_column_id=col)
        assert _dim_touches_restricted_column(d, {col}) is True


# ===========================================================================
# Route-level: list_dimensions hides / get_dimension 404s the calc dim
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


class _ScalarResult:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return FakeScalarResult(self._items)

    def all(self):
        return list(self._items)


@pytest.mark.asyncio
async def test_list_dimensions_hides_calc_dim_over_restricted_column(client, as_viewer):
    """A restricted persona must NOT see a calc dimension whose expression
    references a CLS-restricted column — the column name must not leak."""
    restricted_col = uuid.uuid4()
    visible_col = uuid.uuid4()

    calc_dim = _calc_dim(calc_expression="salary * 1.1", name="AdjustedSalary")
    visible_dim = _plain_dim(source_column_id=visible_col, name="Country")

    persona = types.SimpleNamespace(
        id=uuid.uuid4(), included_measure_ids=[], included_dimension_ids=[],
    )

    db = make_mock_db()
    db.execute = AsyncMock(return_value=_ScalarResult([calc_dim, visible_dim]))

    async def _get(cls, pk):
        if hasattr(cls, "__name__") and cls.__name__ == "ModelColumn":
            if pk == visible_col:
                return types.SimpleNamespace(
                    id=visible_col, column_name="country_col",
                    data_type="text", model_table_id=uuid.uuid4(),
                    is_hidden=False, cardinality_estimate=None,
                )
        return None

    db.get = AsyncMock(side_effect=_get)

    calc_ctx = _ctx(restricted={"salary"}, known={"salary", "country_col"})

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
            new=AsyncMock(return_value={}),
        ),
        patch(
            "src.api.dimensions._load_calc_cls_context",
            new=AsyncMock(return_value=calc_ctx),
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
    names = {d["name"] for d in resp.json()}
    assert "AdjustedSalary" not in names, (
        "calc dimension referencing a restricted column must be hidden"
    )
    assert "Country" in names


@pytest.mark.asyncio
async def test_list_dimensions_shows_clean_calc_dim(client, as_viewer):
    """A calc dimension whose expression references only unrestricted columns
    must remain visible to the restricted persona."""
    restricted_col = uuid.uuid4()

    calc_dim = _calc_dim(calc_expression="region || dept", name="RegionDept")

    persona = types.SimpleNamespace(
        id=uuid.uuid4(), included_measure_ids=[], included_dimension_ids=[],
    )

    db = make_mock_db()
    db.execute = AsyncMock(return_value=_ScalarResult([calc_dim]))
    db.get = AsyncMock(return_value=None)

    calc_ctx = _ctx(restricted={"salary"}, known={"salary", "region", "dept"})

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
            new=AsyncMock(return_value={}),
        ),
        patch(
            "src.api.dimensions._load_calc_cls_context",
            new=AsyncMock(return_value=calc_ctx),
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
    names = {d["name"] for d in resp.json()}
    assert "RegionDept" in names


@pytest.mark.asyncio
async def test_get_dimension_404_for_calc_dim_over_restricted_column(client, as_viewer):
    """The single dimension GET must fail closed (404) for a calc dimension
    whose expression references a CLS-restricted column."""
    restricted_col = uuid.uuid4()
    calc_dim = _calc_dim(calc_expression="salary + bonus", name="TotalComp")

    persona = types.SimpleNamespace(
        id=uuid.uuid4(), included_measure_ids=[], included_dimension_ids=[],
    )

    db = make_mock_db()
    db.get = AsyncMock(return_value=calc_dim)

    calc_ctx = _ctx(restricted={"salary"}, known={"salary", "bonus"})

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
            new=AsyncMock(return_value={}),
        ),
        patch(
            "src.api.dimensions._load_calc_cls_context",
            new=AsyncMock(return_value=calc_ctx),
        ),
    ):
        resp = await client.get(f"{PREFIX_D}/{calc_dim.id}")

    assert resp.status_code == 404, resp.text
