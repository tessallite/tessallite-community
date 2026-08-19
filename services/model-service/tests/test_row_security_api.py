"""HTTP route tests for src.api.row_security (Phase 5.1.D)."""
from __future__ import annotations

import types
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.db.models import Model, ModelTable, RowSecurityRule

from .conftest import (
    TEST_MODEL_ID,
    TEST_PROJECT_ID,
    TEST_TENANT,
    async_gen_from,
    client,
    make_mock_db,
    make_model,
)

pytestmark = pytest.mark.unit

PREFIX = f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/row-security"
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def evict_query_router_cache():
    evict = AsyncMock()
    with patch("src.api.row_security._evict_query_router_cache", evict):
        yield evict


def _make_rule(
    rule_id=None,
    rule_type="role_predicate",
    name="north",
    path="region.region_code",
    expr="dimension_equals('region.region_code', 'NORTH')",
    roles=("region_manager_north",),
    mapping_table_id=None,
    mapping_user_column=None,
    mapping_value_column=None,
    attribute_source="jwt_role",
    attribute_claim_name=None,
):
    return types.SimpleNamespace(
        id=rule_id or uuid.uuid4(),
        model_id=TEST_MODEL_ID,
        name=name,
        dimension_path=path,
        rule_type=rule_type,
        predicate_expression=expr if rule_type == "role_predicate" else None,
        applies_to_roles=list(roles) if rule_type == "role_predicate" else None,
        mapping_table_id=mapping_table_id,
        mapping_user_column=mapping_user_column,
        mapping_value_column=mapping_value_column,
        is_enabled=True,
        attribute_source=attribute_source,
        attribute_claim_name=attribute_claim_name,
        created_at=NOW,
        updated_at=NOW,
    )


def _result_with(items):
    # scalar_one_or_none returns the first row (or None) so the simulate
    # endpoint's connector-resolution query (F-007-07, which loads the
    # model's DataSource) works against the mock the same way the rule
    # loader's scalars().all() does. ``.all()`` (no scalars) backs the
    # Bug-7035 protected-dimension query which selects a single column and
    # iterates the resulting rows directly.
    return types.SimpleNamespace(
        scalars=lambda: types.SimpleNamespace(all=lambda: items),
        scalar_one_or_none=lambda: (items[0] if items else None),
        all=lambda: items,
    )


# ---------------------------------------------------------------------------
# Scope enforcement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_rejects_wrong_project_scope(client):
    db = make_mock_db()
    wrong_model = types.SimpleNamespace(
        id=TEST_MODEL_ID, project_id=uuid.uuid4()
    )
    db.get = AsyncMock(return_value=wrong_model)

    with patch("src.api.row_security.get_tenant_db", async_gen_from(db)):
        resp = await client.get(PREFIX)

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Model not found"


# ---------------------------------------------------------------------------
# List + get
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_returns_rules_for_model(client):
    db = make_mock_db()
    r1 = _make_rule(name="north")
    r2 = _make_rule(name="south", roles=("region_manager_south",))

    db.get = AsyncMock(return_value=make_model())
    db.execute = AsyncMock(return_value=_result_with([r1, r2]))

    with patch("src.api.row_security.get_tenant_db", async_gen_from(db)):
        resp = await client.get(PREFIX)

    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    assert {r["name"] for r in data} == {"north", "south"}


@pytest.mark.asyncio
async def test_get_single_rule(client):
    db = make_mock_db()
    rule = _make_rule()

    async def _get(cls, obj_id):
        if cls is Model:
            return make_model()
        if cls is RowSecurityRule:
            return rule
        return None

    db.get = AsyncMock(side_effect=_get)

    with patch("src.api.row_security.get_tenant_db", async_gen_from(db)):
        resp = await client.get(f"{PREFIX}/{rule.id}")

    assert resp.status_code == 200
    assert resp.json()["name"] == "north"


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_role_predicate_rule(client, evict_query_router_cache):
    db = make_mock_db()
    db.get = AsyncMock(return_value=make_model())

    # Bug-5206 validation queries for a Dimension matching the path's last
    # segment. Return a matching row so the validation passes.
    dim_result = MagicMock()
    dim_result.scalar_one_or_none.return_value = uuid.uuid4()
    db.execute = AsyncMock(return_value=dim_result)

    async def _refresh(obj):
        obj.id = uuid.uuid4()
        obj.created_at = NOW
        obj.updated_at = NOW

    db.refresh = AsyncMock(side_effect=_refresh)

    with patch("src.api.row_security.get_tenant_db", async_gen_from(db)):
        resp = await client.post(
            PREFIX,
            json={
                "name": "north-only",
                "dimension_path": "region.region_code",
                "rule_type": "role_predicate",
                "predicate_expression": "dimension_equals('region.region_code', 'NORTH')",
                "applies_to_roles": ["region_manager_north"],
            },
        )

    assert resp.status_code == 201, resp.text
    assert resp.json()["rule_type"] == "role_predicate"
    assert db.add.call_count >= 1
    db.commit.assert_awaited()
    evict_query_router_cache.assert_awaited_once_with(TEST_MODEL_ID, TEST_TENANT)


@pytest.mark.asyncio
async def test_create_user_mapping_rule_validates_mapping_table_scope(client):
    db = make_mock_db()
    mapping_table_id = uuid.uuid4()
    # mapping table belongs to a DIFFERENT model → must be rejected.
    other_model_table = types.SimpleNamespace(
        id=mapping_table_id, model_id=uuid.uuid4()
    )

    async def _get(cls, obj_id):
        if cls is Model:
            return make_model()
        if cls is ModelTable and str(obj_id) == str(mapping_table_id):
            return other_model_table
        return None

    db.get = AsyncMock(side_effect=_get)

    # Bug-5206 validation queries for a Dimension matching the path's last
    # segment. Return a matching row so the validation passes and the test
    # reaches the mapping_table_id scope check.
    dim_result = MagicMock()
    dim_result.scalar_one_or_none.return_value = uuid.uuid4()
    db.execute = AsyncMock(return_value=dim_result)

    with patch("src.api.row_security.get_tenant_db", async_gen_from(db)):
        resp = await client.post(
            PREFIX,
            json={
                "name": "per-user-region",
                "dimension_path": "region.region_code",
                "rule_type": "user_mapping",
                "mapping_table_id": str(mapping_table_id),
                "mapping_user_column": "user_id",
                "mapping_value_column": "region_code",
            },
        )

    assert resp.status_code == 400
    assert "mapping_table_id" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_create_rejects_role_predicate_without_expression(client):
    """Pydantic shape validator rejects at the 422 layer before hitting DB."""
    db = make_mock_db()
    db.get = AsyncMock(return_value=make_model())

    with patch("src.api.row_security.get_tenant_db", async_gen_from(db)):
        resp = await client.post(
            PREFIX,
            json={
                "name": "bad",
                "dimension_path": "region.region_code",
                "rule_type": "role_predicate",
                "applies_to_roles": ["x"],
                # missing predicate_expression
            },
        )

    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Update + delete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_rule_patches_fields(client, evict_query_router_cache):
    db = make_mock_db()
    rule = _make_rule()

    async def _get(cls, obj_id):
        if cls is Model:
            return make_model()
        if cls is RowSecurityRule:
            return rule
        return None

    db.get = AsyncMock(side_effect=_get)
    db.refresh = AsyncMock()

    with patch("src.api.row_security.get_tenant_db", async_gen_from(db)):
        resp = await client.patch(
            f"{PREFIX}/{rule.id}",
            json={"is_enabled": False, "name": "north-disabled"},
        )

    assert resp.status_code == 200
    assert rule.is_enabled is False
    assert rule.name == "north-disabled"
    evict_query_router_cache.assert_awaited_once_with(TEST_MODEL_ID, TEST_TENANT)


@pytest.mark.asyncio
async def test_delete_rule(client, evict_query_router_cache):
    db = make_mock_db()
    rule = _make_rule()

    async def _get(cls, obj_id):
        if cls is Model:
            return make_model()
        if cls is RowSecurityRule:
            return rule
        return None

    db.get = AsyncMock(side_effect=_get)

    with patch("src.api.row_security.get_tenant_db", async_gen_from(db)):
        resp = await client.delete(f"{PREFIX}/{rule.id}")

    assert resp.status_code == 204
    db.delete.assert_awaited_once_with(rule)
    evict_query_router_cache.assert_awaited_once_with(TEST_MODEL_ID, TEST_TENANT)


@pytest.mark.asyncio
async def test_delete_404_when_rule_on_other_model(client):
    db = make_mock_db()
    foreign_rule = _make_rule()
    foreign_rule.model_id = uuid.uuid4()  # different model

    async def _get(cls, obj_id):
        if cls is Model:
            return make_model()
        if cls is RowSecurityRule:
            return foreign_rule
        return None

    db.get = AsyncMock(side_effect=_get)

    with patch("src.api.row_security.get_tenant_db", async_gen_from(db)):
        resp = await client.delete(f"{PREFIX}/{foreign_rule.id}")

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Simulate-as-user
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_simulate_no_matching_rule_shows_fail_closed_deny_all(client):
    """F-007-01: a model with a role_predicate rule governs an audience. A
    principal who matches NO rule is a governed non-member — Simulate must SHOW
    the fail-closed deny (compiled_predicate ``0 = 1``), which is precisely what
    proves to an admin that this principal sees no rows. It must NOT report an
    empty/None predicate (the old unsafe 'unrestricted' preview)."""
    db = make_mock_db()
    db.get = AsyncMock(return_value=make_model())
    # Loader returns a rule that won't match the simulated roles.
    rule = _make_rule()

    async def _execute(stmt):
        text = str(stmt).lower()
        if "row_security_rules" in text:
            return _result_with([rule])
        return _result_with([])

    db.execute = _execute

    with patch("src.api.row_security.get_tenant_db", async_gen_from(db)):
        resp = await client.post(
            f"{PREFIX}/simulate",
            json={"user_identity": "alice@x", "roles": ["viewer"]},
        )

    assert resp.status_code == 200
    body = resp.json()
    # The synthetic deny-all sentinel is not a real rule id, so active_rule_ids
    # stays empty, but the compiled predicate is the deny-all.
    assert body["active_rule_ids"] == []
    assert body["compiled_predicate"] == "0 = 1"


@pytest.mark.asyncio
async def test_simulate_matching_role_returns_compiled_predicate(client):
    db = make_mock_db()
    db.get = AsyncMock(return_value=make_model())
    rule = _make_rule()

    async def _execute(stmt):
        text = str(stmt).lower()
        if "row_security_rules" in text:
            return _result_with([rule])
        return _result_with([])

    db.execute = _execute

    with patch("src.api.row_security.get_tenant_db", async_gen_from(db)):
        resp = await client.post(
            f"{PREFIX}/simulate",
            json={
                "user_identity": "alice@x",
                "roles": ["region_manager_north"],
            },
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["active_rule_ids"]) == 1
    assert body["active_rule_ids"][0] == str(rule.id)
    assert body["compiled_predicate"] == "\"region_code\" = 'NORTH'"


# ---------------------------------------------------------------------------
# Bug-7035: multi-source connector resolution for the simulate preview
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_model_connector_uses_rls_protected_source_bug7035():
    """On a multi-source model, the simulate preview must resolve the connector
    from the source that hosts the RLS-protected dimension, NOT an arbitrary
    first source. Previously ``DataSource.limit(1)`` (unordered) could pick a
    Postgres source and preview double-quotes while the protected column lives
    on a BigQuery source that quotes with backticks.
    """
    from src.api.row_security import _resolve_model_connector

    protected_source = types.SimpleNamespace(
        id=uuid.uuid4(), project_connection_id=uuid.uuid4()
    )
    bq_conn = types.SimpleNamespace(connection_type="bigquery")

    async def _execute(stmt):
        text = str(stmt).lower()
        if "row_security_rules" in text:
            # Enabled rule protecting region.region_code.
            return types.SimpleNamespace(all=lambda: [("region.region_code",)])
        # The protected-dimension -> source join returns the BigQuery source.
        if "data_sources" in text and "dimensions" in text:
            return types.SimpleNamespace(
                scalars=lambda: types.SimpleNamespace(all=lambda: [protected_source])
            )
        # Any primary-source fallback query — should NOT be reached here.
        return types.SimpleNamespace(
            scalars=lambda: types.SimpleNamespace(all=lambda: []),
            scalar_one_or_none=lambda: None,
        )

    db = make_mock_db()
    db.execute = _execute
    db.get = AsyncMock(return_value=bq_conn)

    connector, note = await _resolve_model_connector(db, TEST_MODEL_ID)
    assert connector == "bigquery"
    # Bug-8904: the protected dimensions resolved to exactly ONE connector, so
    # the resolution is definitive and the preview needs no dialect caveat.
    assert note is None


@pytest.mark.asyncio
async def test_resolve_model_connector_falls_back_to_ordered_primary_bug7035():
    """When no protected dimension resolves to a source (e.g. rule column not
    yet mapped), fall back to the model's earliest source by created_at — the
    same primary-source default the runtime uses — never an arbitrary row."""
    from src.api.row_security import _resolve_model_connector

    primary_source = types.SimpleNamespace(
        id=uuid.uuid4(), project_connection_id=uuid.uuid4()
    )
    pg_conn = types.SimpleNamespace(connection_type="postgresql")

    async def _execute(stmt):
        text = str(stmt).lower()
        if "row_security_rules" in text:
            return types.SimpleNamespace(all=lambda: [("region.region_code",)])
        if "data_sources" in text and "dimensions" in text:
            # Protected dimension resolves to no source.
            return types.SimpleNamespace(
                scalars=lambda: types.SimpleNamespace(all=lambda: [])
            )
        # Ordered primary-source fallback.
        return types.SimpleNamespace(
            scalars=lambda: types.SimpleNamespace(all=lambda: [primary_source]),
            scalar_one_or_none=lambda: primary_source,
        )

    db = make_mock_db()
    db.execute = _execute
    db.get = AsyncMock(return_value=pg_conn)

    connector, note = await _resolve_model_connector(db, TEST_MODEL_ID)
    assert connector == "postgresql"
    # Bug-8904: an enabled rule exists but its protected dimension resolved to
    # no source, so the dialect was GUESSED from the primary source. That is
    # exactly the case that used to be silent — the modeller must be told the
    # previewed quoting may not match the connector that runs the query.
    assert note is not None
    assert "postgresql" in note
    assert "quoting" in note.lower()


@pytest.mark.asyncio
async def test_resolve_model_connector_warns_when_protected_dims_span_connectors_bug8904():
    """Bug-8904: a model whose protected dimensions live on BOTH a BigQuery and
    a PostgreSQL source is genuinely ambiguous — the preview can only be right
    about identifier quoting for one of them. The resolver still picks the
    primary source (deterministic), but it must now SAY SO.

    Before this fix the ``connector_note`` field existed on the response schema
    and was never populated by anything, so the modeller saw a predicate quoted
    for one dialect with no indication that the other was equally in play.
    """
    from src.api.row_security import _resolve_model_connector

    bq_source = types.SimpleNamespace(id=uuid.uuid4(), project_connection_id=uuid.uuid4())
    pg_source = types.SimpleNamespace(id=uuid.uuid4(), project_connection_id=uuid.uuid4())
    conn_by_id = {
        bq_source.project_connection_id: types.SimpleNamespace(connection_type="bigquery"),
        pg_source.project_connection_id: types.SimpleNamespace(connection_type="postgresql"),
    }

    async def _execute(stmt):
        text = str(stmt).lower()
        if "row_security_rules" in text:
            return types.SimpleNamespace(all=lambda: [("region.region_code",)])
        if "data_sources" in text and "dimensions" in text:
            # Protected dimensions span TWO connectors.
            return types.SimpleNamespace(
                scalars=lambda: types.SimpleNamespace(
                    all=lambda: [bq_source, pg_source]
                )
            )
        # Ordered primary-source fallback resolves to the PostgreSQL source.
        return types.SimpleNamespace(
            scalars=lambda: types.SimpleNamespace(all=lambda: [pg_source]),
            scalar_one_or_none=lambda: pg_source,
        )

    db = make_mock_db()
    db.execute = _execute
    db.get = AsyncMock(side_effect=lambda _entity, ref: conn_by_id.get(ref))

    connector, note = await _resolve_model_connector(db, TEST_MODEL_ID)
    assert connector == "postgresql"
    assert note is not None
    # The note must name BOTH competing connectors, otherwise the modeller
    # cannot tell which other dialect the predicate might actually run under.
    assert "bigquery" in note
    assert "postgresql" in note


# ---------------------------------------------------------------------------
# Bug-7027: connector fallback surfaces diagnostic note
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_model_connector_warns_when_source_connector_unresolvable_bug7027():
    """When the model HAS a source but its connector cannot be resolved
    (e.g. connection row missing, connection_type empty), the fallback to
    'postgresql' must be DISCLOSED so the simulate response can warn the
    modeller that the preview may differ from runtime quoting.

    Distinct from the Bug-8904 tests above: there the primary source resolved
    to a real connector and only the protected dimension was unmapped. Here the
    primary source itself is unreachable, which is the branch that also emits
    the operator-facing ``logger.warning``.
    """
    from src.api.row_security import _resolve_model_connector

    unreachable_source = types.SimpleNamespace(
        id=uuid.uuid4(), project_connection_id=uuid.uuid4()
    )

    async def _execute(stmt):
        text = str(stmt).lower()
        if "row_security_rules" in text:
            return types.SimpleNamespace(all=lambda: [("region.region_code",)])
        if "data_sources" in text and "dimensions" in text:
            # Protected dimension resolves to no source.
            return types.SimpleNamespace(
                scalars=lambda: types.SimpleNamespace(all=lambda: [])
            )
        # Ordered primary-source fallback — source exists.
        return types.SimpleNamespace(
            scalars=lambda: types.SimpleNamespace(all=lambda: [unreachable_source]),
            scalar_one_or_none=lambda: unreachable_source,
        )

    db = make_mock_db()
    db.execute = _execute
    # _connector_for_source will return None because the connection lookup
    # returns None.
    db.get = AsyncMock(return_value=None)

    connector, note = await _resolve_model_connector(db, TEST_MODEL_ID)
    assert connector == "postgresql"
    assert note is not None
    # No reachable connection at all — a different caveat from the "protected
    # dimension unmapped" one, and it must name the dialect actually used.
    assert "no reachable source connection" in note
    assert "postgresql" in note
    assert "quoting" in note.lower()


@pytest.mark.asyncio
async def test_resolve_model_connector_warns_when_no_source_but_rules_enabled_bug7027():
    """A model with enabled rules but NO DataSource still compiles its
    predicate against the guessed compiler default, so the guess is disclosed.

    This assertion was inverted when Bug-8904 (574dca3d) broadened disclosure:
    the earlier contract treated a sourceless model as unconditionally benign.
    It is benign only when there are no enabled rules — with no rule there is no
    predicate and therefore no quoting to caveat. That silent case is pinned by
    ``TestSimulateConnectorResolution.test_no_source_falls_back_to_postgresql``
    in test_row_security_ml6.py; this test pins the complementary loud case.
    """
    from src.api.row_security import _resolve_model_connector

    async def _execute(stmt):
        text = str(stmt).lower()
        if "row_security_rules" in text:
            return types.SimpleNamespace(all=lambda: [("region.region_code",)])
        if "data_sources" in text and "dimensions" in text:
            return types.SimpleNamespace(
                scalars=lambda: types.SimpleNamespace(all=lambda: [])
            )
        # No source at all.
        return types.SimpleNamespace(
            scalars=lambda: types.SimpleNamespace(all=lambda: []),
            scalar_one_or_none=lambda: None,
        )

    db = make_mock_db()
    db.execute = _execute
    # _connector_for_source returns None because source is None.
    connector, note = await _resolve_model_connector(db, TEST_MODEL_ID)
    assert connector == "postgresql"
    assert note is not None
    assert "no reachable source connection" in note


# ---------------------------------------------------------------------------
# Bug-5206: dimension_path / predicate mismatch rejection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_rejects_predicate_column_mismatch_bug5206(client):
    """A rule declaring dimension_path 'region.region_code' but filtering
    on 'country_code' inside the predicate must be rejected at create time."""
    db = make_mock_db()
    db.get = AsyncMock(return_value=make_model())

    # Return a matching dimension so _validate_dimension_path_exists passes.
    dim_result = MagicMock()
    dim_result.scalar_one_or_none.return_value = uuid.uuid4()
    db.execute = AsyncMock(return_value=dim_result)

    with patch("src.api.row_security.get_tenant_db", async_gen_from(db)):
        resp = await client.post(
            PREFIX,
            json={
                "name": "mismatch-rule",
                "dimension_path": "region.region_code",
                "rule_type": "role_predicate",
                "predicate_expression": "dimension_equals('region.country_code', 'US')",
                "applies_to_roles": ["viewer"],
            },
        )

    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "country_code" in detail
    assert "region_code" in detail


@pytest.mark.asyncio
async def test_update_rejects_predicate_column_mismatch_bug5206(client):
    """Updating a rule's predicate to reference a different column than the
    declared dimension_path must be rejected."""
    db = make_mock_db()
    rule = _make_rule(
        path="region.region_code",
        expr="dimension_equals('region.region_code', 'NORTH')",
    )

    async def _get(cls, obj_id):
        if cls is Model:
            return make_model()
        if cls is RowSecurityRule:
            return rule
        return None

    db.get = AsyncMock(side_effect=_get)
    db.refresh = AsyncMock()

    with patch("src.api.row_security.get_tenant_db", async_gen_from(db)):
        resp = await client.patch(
            f"{PREFIX}/{rule.id}",
            json={
                "predicate_expression": "dimension_equals('region.country_code', 'US')",
            },
        )

    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "country_code" in detail
    assert "region_code" in detail


@pytest.mark.asyncio
async def test_update_rejects_dimension_path_mismatch_with_existing_predicate_bug5206(client):
    """Changing only dimension_path to a column that doesn't match the existing
    predicate must be rejected."""
    db = make_mock_db()
    rule = _make_rule(
        path="region.region_code",
        expr="dimension_equals('region.region_code', 'NORTH')",
    )

    async def _get(cls, obj_id):
        if cls is Model:
            return make_model()
        if cls is RowSecurityRule:
            return rule
        return None

    db.get = AsyncMock(side_effect=_get)

    # _validate_dimension_path_exists must pass — return a matching dimension.
    dim_result = MagicMock()
    dim_result.scalar_one_or_none.return_value = uuid.uuid4()
    db.execute = AsyncMock(return_value=dim_result)
    db.refresh = AsyncMock()

    with patch("src.api.row_security.get_tenant_db", async_gen_from(db)):
        resp = await client.patch(
            f"{PREFIX}/{rule.id}",
            json={
                "dimension_path": "region.country_code",
            },
        )

    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "region_code" in detail
    assert "country_code" in detail


# ---------------------------------------------------------------------------
# Bug-5207: mapping column existence rejection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_rejects_nonexistent_mapping_columns_bug5207(client):
    """A user_mapping rule referencing columns that don't exist on the
    mapping table must be rejected at create time."""
    db = make_mock_db()
    mapping_table_id = uuid.uuid4()
    # Mapping table belongs to the SAME model — passes scope check.
    same_model_table = types.SimpleNamespace(
        id=mapping_table_id, model_id=TEST_MODEL_ID,
    )

    async def _get(cls, obj_id):
        if cls is Model:
            return make_model()
        if cls is ModelTable and str(obj_id) == str(mapping_table_id):
            return same_model_table
        return None

    db.get = AsyncMock(side_effect=_get)

    # First execute: _validate_dimension_path_exists — return a match.
    dim_result = MagicMock()
    dim_result.scalar_one_or_none.return_value = uuid.uuid4()
    # Second execute: _validate_mapping_columns — return empty (no columns found).
    col_result = MagicMock()
    col_result.scalars.return_value.all.return_value = []

    db.execute = AsyncMock(side_effect=[dim_result, col_result])

    with patch("src.api.row_security.get_tenant_db", async_gen_from(db)):
        resp = await client.post(
            PREFIX,
            json={
                "name": "per-user-region",
                "dimension_path": "region.region_code",
                "rule_type": "user_mapping",
                "mapping_table_id": str(mapping_table_id),
                "mapping_user_column": "nonexistent_user",
                "mapping_value_column": "nonexistent_val",
            },
        )

    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "nonexistent_user" in detail or "nonexistent_val" in detail
    assert "not found" in detail.lower()


@pytest.mark.asyncio
async def test_update_rejects_nonexistent_mapping_columns_bug5207(client):
    """Updating a user_mapping rule to reference a column that doesn't exist
    on the mapping table must be rejected."""
    db = make_mock_db()
    mapping_table_id = uuid.uuid4()
    rule = _make_rule(
        rule_type="user_mapping",
        path="region.region_code",
        expr=None,
        roles=None,
        mapping_table_id=mapping_table_id,
        mapping_user_column="user_id",
        mapping_value_column="region_code",
    )

    same_model_table = types.SimpleNamespace(
        id=mapping_table_id, model_id=TEST_MODEL_ID,
    )

    async def _get(cls, obj_id):
        if cls is Model:
            return make_model()
        if cls is RowSecurityRule:
            return rule
        if cls is ModelTable and str(obj_id) == str(mapping_table_id):
            return same_model_table
        return None

    db.get = AsyncMock(side_effect=_get)

    # _validate_mapping_columns returns empty — column not found.
    col_result = MagicMock()
    col_result.scalars.return_value.all.return_value = []
    db.execute = AsyncMock(return_value=col_result)
    db.refresh = AsyncMock()

    with patch("src.api.row_security.get_tenant_db", async_gen_from(db)):
        resp = await client.patch(
            f"{PREFIX}/{rule.id}",
            json={
                "mapping_value_column": "bad_column",
            },
        )

    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "bad_column" in detail or "not found" in detail.lower()


# ---------------------------------------------------------------------------
# Bug-5904: claim/scope-sourced rules must require attribute_claim_name
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("attribute_source", ["saml_claim", "oidc_scope"])
async def test_create_rejects_claim_source_without_claim_name_bug5904(
    client, attribute_source
):
    """A claim/scope-sourced role_predicate rule with no attribute_claim_name
    must be rejected at create — the schema validator fires before the
    handler runs any DB query, so no mocking of the model/dimension lookups
    is needed."""
    db = make_mock_db()
    db.get = AsyncMock(return_value=make_model())

    with patch("src.api.row_security.get_tenant_db", async_gen_from(db)):
        resp = await client.post(
            PREFIX,
            json={
                "name": "claim-backed",
                "dimension_path": "region.region_code",
                "rule_type": "role_predicate",
                "predicate_expression": "dimension_equals('region.region_code', 'NORTH')",
                "applies_to_roles": ["finance"],
                "attribute_source": attribute_source,
            },
        )

    assert resp.status_code == 422
    assert "attribute_claim_name" in resp.text


@pytest.mark.asyncio
@pytest.mark.parametrize("attribute_source", ["saml_claim", "oidc_scope"])
async def test_create_rejects_claim_source_with_blank_claim_name_bug5904(
    client, attribute_source
):
    """A whitespace-only attribute_claim_name is as inert as a missing one —
    must be rejected the same way, not silently trimmed and accepted."""
    db = make_mock_db()
    db.get = AsyncMock(return_value=make_model())

    with patch("src.api.row_security.get_tenant_db", async_gen_from(db)):
        resp = await client.post(
            PREFIX,
            json={
                "name": "claim-backed",
                "dimension_path": "region.region_code",
                "rule_type": "role_predicate",
                "predicate_expression": "dimension_equals('region.region_code', 'NORTH')",
                "applies_to_roles": ["finance"],
                "attribute_source": attribute_source,
                "attribute_claim_name": "   ",
            },
        )

    assert resp.status_code == 422
    assert "attribute_claim_name" in resp.text


@pytest.mark.asyncio
async def test_create_accepts_claim_source_with_claim_name_bug5904(client):
    """The legitimate case: a claim-sourced rule WITH a claim name must still
    save cleanly — the fix must not over-reject valid rules."""
    db = make_mock_db()
    db.get = AsyncMock(return_value=make_model())

    dim_result = MagicMock()
    dim_result.scalar_one_or_none.return_value = uuid.uuid4()
    db.execute = AsyncMock(return_value=dim_result)

    async def _refresh(obj):
        obj.id = uuid.uuid4()
        obj.created_at = NOW
        obj.updated_at = NOW

    db.refresh = AsyncMock(side_effect=_refresh)

    with patch("src.api.row_security.get_tenant_db", async_gen_from(db)):
        resp = await client.post(
            PREFIX,
            json={
                "name": "claim-backed",
                "dimension_path": "region.region_code",
                "rule_type": "role_predicate",
                "predicate_expression": "dimension_equals('region.region_code', 'NORTH')",
                "applies_to_roles": ["finance"],
                "attribute_source": "saml_claim",
                "attribute_claim_name": "department",
            },
        )

    assert resp.status_code == 201, resp.text
    assert resp.json()["attribute_claim_name"] == "department"


@pytest.mark.asyncio
async def test_update_rejects_switching_to_claim_source_without_claim_name_bug5904(
    client,
):
    """Updating an existing (default jwt_role) role_predicate rule's
    attribute_source to saml_claim, without also supplying a claim name in
    the same request, must be rejected — the update body is a partial PATCH
    so the schema alone cannot see the merged (post-update) state; the
    handler must compute the effective value."""
    db = make_mock_db()
    rule = _make_rule()  # attribute_source="jwt_role", attribute_claim_name=None

    async def _get(cls, obj_id):
        if cls is Model:
            return make_model()
        if cls is RowSecurityRule:
            return rule
        return None

    db.get = AsyncMock(side_effect=_get)
    db.refresh = AsyncMock()

    with patch("src.api.row_security.get_tenant_db", async_gen_from(db)):
        resp = await client.patch(
            f"{PREFIX}/{rule.id}",
            json={"attribute_source": "saml_claim"},
        )

    assert resp.status_code == 400
    assert "attribute_claim_name" in resp.json()["detail"]
    # The rejected update must not have been applied.
    assert rule.attribute_source == "jwt_role"


@pytest.mark.asyncio
async def test_update_rejects_blanking_claim_name_on_existing_claim_rule_bug5904(
    client,
):
    """A rule that is already claim-sourced and correctly configured must
    not be silently neutered by a PATCH that blanks the claim name while
    leaving attribute_source untouched."""
    db = make_mock_db()
    rule = _make_rule(
        attribute_source="oidc_scope", attribute_claim_name="reports:read"
    )

    async def _get(cls, obj_id):
        if cls is Model:
            return make_model()
        if cls is RowSecurityRule:
            return rule
        return None

    db.get = AsyncMock(side_effect=_get)
    db.refresh = AsyncMock()

    with patch("src.api.row_security.get_tenant_db", async_gen_from(db)):
        resp = await client.patch(
            f"{PREFIX}/{rule.id}",
            json={"attribute_claim_name": ""},
        )

    assert resp.status_code == 400
    assert "attribute_claim_name" in resp.json()["detail"]
    # The rejected update must not have been applied.
    assert rule.attribute_claim_name == "reports:read"


@pytest.mark.asyncio
async def test_update_rejects_explicit_null_claim_name_on_existing_claim_rule_bug5904(
    client,
):
    """Same as the blank-string case above, but via an explicit JSON `null`
    instead of an empty string — pydantic treats an explicitly-provided
    None as "set" (retained by exclude_unset=True), so this must be
    rejected too, not silently treated as "field omitted"."""
    db = make_mock_db()
    rule = _make_rule(
        attribute_source="saml_claim", attribute_claim_name="department"
    )

    async def _get(cls, obj_id):
        if cls is Model:
            return make_model()
        if cls is RowSecurityRule:
            return rule
        return None

    db.get = AsyncMock(side_effect=_get)
    db.refresh = AsyncMock()

    with patch("src.api.row_security.get_tenant_db", async_gen_from(db)):
        resp = await client.patch(
            f"{PREFIX}/{rule.id}",
            json={"attribute_claim_name": None},
        )

    assert resp.status_code == 400
    assert "attribute_claim_name" in resp.json()["detail"]
    # The rejected update must not have been applied.
    assert rule.attribute_claim_name == "department"


@pytest.mark.asyncio
async def test_update_allows_switching_away_from_claim_source_bug5904(client):
    """The legitimate escape hatch: switching a claim-sourced rule back to
    jwt_role while clearing the now-irrelevant claim name in the SAME
    request must still be allowed."""
    db = make_mock_db()
    rule = _make_rule(
        attribute_source="saml_claim", attribute_claim_name="department"
    )

    async def _get(cls, obj_id):
        if cls is Model:
            return make_model()
        if cls is RowSecurityRule:
            return rule
        return None

    db.get = AsyncMock(side_effect=_get)
    db.refresh = AsyncMock()

    with patch("src.api.row_security.get_tenant_db", async_gen_from(db)):
        resp = await client.patch(
            f"{PREFIX}/{rule.id}",
            json={"attribute_source": "jwt_role", "attribute_claim_name": None},
        )

    assert resp.status_code == 200, resp.text
    assert rule.attribute_source == "jwt_role"
    assert rule.attribute_claim_name is None


@pytest.mark.asyncio
async def test_update_unrelated_field_on_claim_rule_is_unaffected_bug5904(client):
    """A PATCH that does not touch attribute_source/attribute_claim_name at
    all must not be blocked just because the rule happens to be
    claim-sourced — the effective-value check must read the existing
    (already-valid) stored values, not treat their absence from the patch
    body as missing."""
    db = make_mock_db()
    rule = _make_rule(
        attribute_source="saml_claim", attribute_claim_name="department"
    )

    async def _get(cls, obj_id):
        if cls is Model:
            return make_model()
        if cls is RowSecurityRule:
            return rule
        return None

    db.get = AsyncMock(side_effect=_get)
    db.refresh = AsyncMock()

    with patch("src.api.row_security.get_tenant_db", async_gen_from(db)):
        resp = await client.patch(
            f"{PREFIX}/{rule.id}",
            json={"is_enabled": False},
        )

    assert resp.status_code == 200, resp.text
    assert rule.is_enabled is False
    assert rule.attribute_source == "saml_claim"
    assert rule.attribute_claim_name == "department"


# ---------------------------------------------------------------------------
# Bug-7807 [SECURITY]: row-security rule LIST/GET disclose the tenant's
# access-control policy (predicate_expression, applies_to_roles,
# attribute_claim_name — exactly who is restricted from what). Read is raised
# from viewer to modeler so a plain viewer can no longer read rule
# definitions, while the modeller authoring flow is preserved. These tests
# assert the KNOWN authorization decision on the REAL route dependencies,
# invoking require_role's dependency directly against viewer/modeler bindings
# so the autouse bootstrap-admin fixture (conftest.mock_rbac_get_tenant_db,
# which grants every caller) does not mask the decision.
# ---------------------------------------------------------------------------

import types as _types  # noqa: E402

from fastapi import HTTPException as _HTTPException  # noqa: E402

from src.api.row_security import get_rule as _get_rule_route  # noqa: E402
from src.api.row_security import list_rules as _list_rules_route  # noqa: E402
from src.api.row_security import router as _rs_router  # noqa: E402

_RS_PROJECT_ID = uuid.uuid4()
_RS_MODEL_ID = uuid.uuid4()
_RS_USER_ID = "rls-caller"


def _rls_binding(role, model_id=None):
    return _types.SimpleNamespace(
        user_identity=_RS_USER_ID,
        project_id=_RS_PROJECT_ID,
        model_id=model_id,
        role=role,
    )


def _rls_db_with_bindings(bindings):
    """Mock DB that answers require_role's binding lookups.

    Mirrors tests/test_rbac_model_scope.py: caller-scoped lookups filter on
    user_identity; the bootstrap existence probe filters on project_id only
    and reads via .first().
    """
    db = MagicMock()

    async def _execute(stmt):
        result = MagicMock()
        text = str(stmt)
        is_existence_probe = "user_identity" not in text
        matching = [b for b in bindings if b.user_identity == _RS_USER_ID]
        result.scalar_one_or_none.return_value = matching[0] if matching else None
        result.scalars.return_value.all.return_value = matching
        if is_existence_probe:
            result.first.return_value = (bindings[0],) if bindings else None
        return result

    db.execute = AsyncMock(side_effect=_execute)
    return db


def _route_role_dependency(endpoint):
    """Return the require_role dependency callable wired to a route endpoint.

    Reads the ACTUAL dependency FastAPI resolves for the given route function,
    so the test verifies the deployed gate — not a hand-written copy of it.
    """
    for route in _rs_router.routes:
        if getattr(route, "endpoint", None) is endpoint:
            for dep in route.dependant.dependencies:
                # require_role's inner callable is named _dependency; that is
                # the gate under test. forbid_embed_user etc. have other names.
                if dep.call is not None and dep.call.__name__ == "_dependency":
                    return dep.call
    raise AssertionError(f"require_role dependency not found on {endpoint.__name__}")


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", [_list_rules_route, _get_rule_route])
async def test_viewer_denied_reading_row_security_rules_bug7807(endpoint):
    """A project-level 'viewer' is 403'd on list_rules and get_rule — a plain
    viewer can no longer read row-security rule definitions."""
    dep = _route_role_dependency(endpoint)
    db = _rls_db_with_bindings([_rls_binding("viewer", model_id=None)])
    caller = _types.SimpleNamespace(
        role="member", tenant_id=TEST_TENANT, user_id=_RS_USER_ID, email=_RS_USER_ID
    )

    async def _gen(*a, **kw):
        yield db

    with patch("src.auth.rbac.get_tenant_db") as mock_gen:
        mock_gen.side_effect = _gen
        with pytest.raises(_HTTPException) as exc:
            await dep(
                project_id=_RS_PROJECT_ID,
                model_id=_RS_MODEL_ID,
                current_user=caller,
            )
    assert exc.value.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", [_list_rules_route, _get_rule_route])
async def test_modeler_allowed_reading_row_security_rules_bug7807(endpoint):
    """A 'modeler' (and by hierarchy, admin) still reads rule definitions —
    the raise must not over-restrict the legitimate authoring flow."""
    dep = _route_role_dependency(endpoint)
    db = _rls_db_with_bindings([_rls_binding("modeler", model_id=None)])
    caller = _types.SimpleNamespace(
        role="member", tenant_id=TEST_TENANT, user_id=_RS_USER_ID, email=_RS_USER_ID
    )

    async def _gen(*a, **kw):
        yield db

    with patch("src.auth.rbac.get_tenant_db") as mock_gen:
        mock_gen.side_effect = _gen
        # Must NOT raise — modeler meets the raised read requirement.
        await dep(
            project_id=_RS_PROJECT_ID,
            model_id=_RS_MODEL_ID,
            current_user=caller,
        )


# ---------------------------------------------------------------------------
# Bug-7027 — the connector_note must actually SURVIVE onto the response
# ---------------------------------------------------------------------------


def test_connector_note_round_trips_on_the_simulate_response_bug7027():
    """Guard against a silent drop.

    ``simulate_as_user`` passes ``connector_note=`` to
    ``RowSecuritySimulateResponse``. Pydantic v2's default ``extra="ignore"``
    SILENTLY DISCARDS unknown keyword arguments — so if the field is missing
    from the schema (for example because the schema half of this change was not
    applied, or was reverted by a merge), the note is computed, passed, and
    thrown away with no error anywhere. That is exactly the dead-code outcome
    the fallback disclosure exists to avoid.

    This converts that silent drop into a red test.
    """
    from shared.schemas.pydantic_models import RowSecuritySimulateResponse

    resp = RowSecuritySimulateResponse(
        user_identity="alice@x",
        roles=["viewer"],
        active_rule_ids=[],
        compiled_predicate='"region_code" = \'NORTH\'',
        executed=False,
        applied_rules=None,
        connector_note="Connector could not be resolved for this model's source;"
                       " preview may differ from runtime quoting.",
    )
    assert resp.connector_note is not None, (
        "connector_note was silently dropped by the response model — the "
        "Bug-7027 fallback disclosure never reaches the caller"
    )
    assert "could not be resolved" in resp.connector_note
    assert "connector_note" in resp.model_dump(), (
        "connector_note is absent from the serialised payload, so no client "
        "can observe the fallback"
    )


def test_connector_note_defaults_to_none_when_resolution_succeeded_bug7027():
    """The absence of a note is meaningful: it says the connector WAS resolved
    definitively, so the preview matches runtime quoting."""
    from shared.schemas.pydantic_models import RowSecuritySimulateResponse

    resp = RowSecuritySimulateResponse(
        user_identity="alice@x",
        roles=["viewer"],
        active_rule_ids=[],
        compiled_predicate=None,
        executed=False,
    )
    assert resp.connector_note is None
