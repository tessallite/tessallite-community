"""Tests for data tag CRUD and persona tag restriction endpoints."""
from __future__ import annotations

import types
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from .conftest import (
    TEST_PROJECT_ID,
    TEST_MODEL_ID,
    TEST_USER_ID,
    NOW,
    async_gen_from,
    make_mock_db,
    make_model,
)

TAGS_PREFIX = f"/api/v1/projects/{TEST_PROJECT_ID}/models/{TEST_MODEL_ID}/data-tags"


def _make_tag(tag_name="PII", description="Personally identifiable", columns=None):
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        model_id=TEST_MODEL_ID,
        tag_name=tag_name,
        description=description,
        created_at=NOW,
        columns=columns or [],
    )


def _make_column(column_name="email", table_alias="customers"):
    # F-008-10: table_name comes from the eagerly-loaded ModelTable
    # relationship (semantic alias), not a fabricated `_table_name` attr.
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        column_name=column_name,
        table=types.SimpleNamespace(
            alias=table_alias, physical_name=table_alias,
        ),
    )


# ------------------------------------------------------------------ #
# GET /data-tags
# ------------------------------------------------------------------ #


class TestListTags:
    @pytest.mark.anyio
    async def test_list_returns_200(self, client):
        db = make_mock_db()
        model = make_model()
        db.get = AsyncMock(return_value=model)

        tags = [_make_tag("PII"), _make_tag("Sensitive", "Finance data")]
        result = MagicMock()
        result.scalars.return_value.all.return_value = tags
        db.execute = AsyncMock(return_value=result)

        with patch("src.api.data_tags.get_tenant_db", async_gen_from(db)):
            resp = await client.get(TAGS_PREFIX)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert data[0]["tag_name"] == "PII"

    @pytest.mark.anyio
    async def test_list_renders_column_table_names(self, client):
        """F-008-10: columns carry the table alias, not an empty string."""
        db = make_mock_db()
        model = make_model()
        db.get = AsyncMock(return_value=model)

        tag = _make_tag("PII", columns=[_make_column("email", "customers")])
        result = MagicMock()
        result.scalars.return_value.all.return_value = [tag]
        db.execute = AsyncMock(return_value=result)

        with patch("src.api.data_tags.get_tenant_db", async_gen_from(db)):
            resp = await client.get(TAGS_PREFIX)
        assert resp.status_code == 200
        cols = resp.json()[0]["columns"]
        assert cols[0]["column_name"] == "email"
        assert cols[0]["table_name"] == "customers"

    @pytest.mark.anyio
    async def test_list_empty(self, client):
        db = make_mock_db()
        model = make_model()
        db.get = AsyncMock(return_value=model)

        result = MagicMock()
        result.scalars.return_value.all.return_value = []
        db.execute = AsyncMock(return_value=result)

        with patch("src.api.data_tags.get_tenant_db", async_gen_from(db)):
            resp = await client.get(TAGS_PREFIX)
        assert resp.status_code == 200
        assert resp.json() == []


# ------------------------------------------------------------------ #
# POST /data-tags
# ------------------------------------------------------------------ #


def _scalar_one_result(value):
    r = MagicMock()
    r.scalar_one_or_none.return_value = value
    return r


def _scalars_all_result(values):
    r = MagicMock()
    r.scalars.return_value.all.return_value = list(values)
    return r


def _execute_queue(*results):
    """AsyncMock whose execute() pops queued results in order, then returns a
    permissive empty result (scalar_one_or_none -> None, scalars().all() -> [])
    for any trailing call (e.g. the audit-level lookup)."""
    queue = list(results)

    async def _side(*_a, **_kw):
        if queue:
            return queue.pop(0)
        trailing = MagicMock()
        trailing.scalar_one_or_none.return_value = None
        trailing.scalars.return_value.all.return_value = []
        return trailing

    return AsyncMock(side_effect=_side)


def _added_audit_events(db):
    """Return every AuditEvent instance handed to db.add()."""
    from shared.db.models import AuditEvent

    return [
        c.args[0]
        for c in db.add.call_args_list
        if c.args and isinstance(c.args[0], AuditEvent)
    ]


class TestCreateTag:
    @pytest.mark.anyio
    async def test_create_returns_201(self, client):
        db = make_mock_db()
        model = make_model()
        db.get = AsyncMock(return_value=model)

        # F-008-01: after commit the endpoint re-selects the tag with its
        # columns eagerly loaded instead of touching lazy relationships.
        created = _make_tag("PII", "Personal data")
        load_result = MagicMock()
        load_result.scalar_one_or_none.return_value = created
        db.execute = AsyncMock(return_value=load_result)

        with patch("src.api.data_tags.get_tenant_db", async_gen_from(db)):
            resp = await client.post(
                TAGS_PREFIX,
                json={"tag_name": "PII", "description": "Personal data"},
            )
        assert resp.status_code == 201
        assert resp.json()["tag_name"] == "PII"

    @pytest.mark.anyio
    async def test_create_with_columns_emits_columns_set_audit(self, client):
        """F-008-06 (Bug-8019): creating a tag with a column membership records a
        DURABLE security.data_tag_columns_set audit event whose after-set is the tagged
        columns and before-set is empty (a brand-new tag). The tag->column set
        feeds the CLS restricted-column closure, so this must never be silent."""
        db = make_mock_db()
        model = make_model()
        db.get = AsyncMock(return_value=model)

        col1 = _make_column("email", "customers")
        col2 = _make_column("ssn", "customers")
        created = _make_tag("PII", "Personal data", columns=[col1, col2])
        db.execute = _execute_queue(
            _scalars_all_result([col1, col2]),   # _resolve_model_columns
            _scalar_one_result(None),            # audit level -> info
            _scalar_one_result(created),         # post-commit _load_tag
        )

        with patch("src.api.data_tags.get_tenant_db", async_gen_from(db)):
            resp = await client.post(
                TAGS_PREFIX,
                json={
                    "tag_name": "PII",
                    "description": "Personal data",
                    "column_ids": [str(col1.id), str(col2.id)],
                },
            )

        assert resp.status_code == 201, resp.text
        events = _added_audit_events(db)
        assert len(events) == 1, "exactly one columns_set audit expected"
        ev = events[0]
        assert ev.action == "security.data_tag_columns_set"
        assert ev.severity == "critical"
        assert ev.target_type == "data_tag"
        assert ev.detail["operation"] == "create"
        assert ev.detail["before_column_ids"] == []
        assert ev.detail["after_column_ids"] == sorted(
            [str(col1.id), str(col2.id)]
        )
        # Adding columns to a new tag broadens what a restriction can cover.
        assert ev.detail["widens_surface"] is True
        # The defect being fixed is "no ACTOR/before/after record" — the audit
        # must attribute the change, so assert the actor is recorded (a
        # regression that drops actor recording must fail this test).
        assert ev.actor_email == TEST_USER_ID
        assert ev.detail["actor_user_id"] == str(TEST_USER_ID)
        db.commit.assert_awaited()

    @pytest.mark.anyio
    async def test_create_audit_write_failure_rolls_back(self, client):
        """F-008-06 (Bug-8019): the columns_set audit is FAIL-CLOSED. If the
        durable AuditEvent cannot be flushed, the whole create must roll back —
        a restricted-column membership never lands without a surviving record."""
        db = make_mock_db()
        model = make_model()
        db.get = AsyncMock(return_value=model)

        col1 = _make_column("email", "customers")
        db.execute = _execute_queue(
            _scalars_all_result([col1]),   # _resolve_model_columns
            _scalar_one_result(None),      # audit level -> info
        )
        # First flush = the tag insert (succeeds); second flush = the audit
        # flush inside audit_required (fails). Fail-closed => rollback, no commit.
        calls = {"n": 0}

        async def _flush(*_a, **_kw):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise RuntimeError("audit write failed")

        db.flush = AsyncMock(side_effect=_flush)

        with patch("src.api.data_tags.get_tenant_db", async_gen_from(db)):
            resp = await client.post(
                TAGS_PREFIX,
                json={"tag_name": "PII", "column_ids": [str(col1.id)]},
            )

        assert resp.status_code == 500, resp.text
        db.rollback.assert_awaited()
        db.commit.assert_not_called()


# ------------------------------------------------------------------ #
# PUT /data-tags/{id}
# ------------------------------------------------------------------ #


class TestUpdateTag:
    @pytest.mark.anyio
    async def test_update_membership_emits_columns_set_audit(self, client):
        """F-008-06 (Bug-8019): replacing a tag's column set records a durable
        security.data_tag_columns_set audit with the exact BEFORE and AFTER column ids so
        the CLS-surface change (widen/narrow) is reconstructable and attributed."""
        db = make_mock_db()
        model = make_model()
        db.get = AsyncMock(return_value=model)

        old_col = _make_column("email", "customers")
        new_col = _make_column("ssn", "customers")
        existing = _make_tag("PII", "Personal data", columns=[old_col])
        updated = _make_tag("PII", "Personal data", columns=[new_col])
        updated.id = existing.id

        db.execute = _execute_queue(
            _scalar_one_result(existing),        # initial _load_tag
            _scalars_all_result([new_col]),      # _resolve_model_columns
            _scalar_one_result(None),            # audit level -> info
            _scalar_one_result(updated),         # post-commit _load_tag
        )

        with patch("src.api.data_tags.get_tenant_db", async_gen_from(db)):
            resp = await client.put(
                f"{TAGS_PREFIX}/{existing.id}",
                json={"column_ids": [str(new_col.id)]},
            )

        assert resp.status_code == 200, resp.text
        events = _added_audit_events(db)
        assert len(events) == 1
        ev = events[0]
        assert ev.action == "security.data_tag_columns_set"
        assert ev.severity == "critical"
        assert ev.detail["operation"] == "update"
        assert ev.detail["before_column_ids"] == [str(old_col.id)]
        assert ev.detail["after_column_ids"] == [str(new_col.id)]
        # new_col is NOT a subset of the old set -> the surface widened.
        assert ev.detail["widens_surface"] is True
        # Actor attribution must survive (the defect was "no actor record").
        assert ev.actor_email == TEST_USER_ID
        assert ev.detail["actor_user_id"] == str(TEST_USER_ID)
        db.commit.assert_awaited()

    @pytest.mark.anyio
    async def test_update_narrowing_membership_records_not_widening(self, client):
        """Removing a column (narrowing the set) must still be audited, but flag
        widens_surface=False so a genuine broadening is distinguishable."""
        db = make_mock_db()
        model = make_model()
        db.get = AsyncMock(return_value=model)

        col_a = _make_column("email", "customers")
        col_b = _make_column("ssn", "customers")
        existing = _make_tag("PII", columns=[col_a, col_b])
        updated = _make_tag("PII", columns=[col_a])
        updated.id = existing.id

        db.execute = _execute_queue(
            _scalar_one_result(existing),        # initial _load_tag
            _scalars_all_result([col_a]),        # _resolve_model_columns
            _scalar_one_result(None),            # audit level
            _scalar_one_result(updated),         # post-commit _load_tag
        )

        with patch("src.api.data_tags.get_tenant_db", async_gen_from(db)):
            resp = await client.put(
                f"{TAGS_PREFIX}/{existing.id}",
                json={"column_ids": [str(col_a.id)]},
            )

        assert resp.status_code == 200, resp.text
        ev = _added_audit_events(db)[0]
        assert ev.detail["before_column_ids"] == sorted(
            [str(col_a.id), str(col_b.id)]
        )
        assert ev.detail["after_column_ids"] == [str(col_a.id)]
        assert ev.detail["widens_surface"] is False

    @pytest.mark.anyio
    async def test_update_without_column_ids_does_not_audit(self, client):
        """A metadata-only update (rename/description, column_ids omitted) does
        NOT touch the CLS surface and must NOT emit a columns_set audit."""
        db = make_mock_db()
        model = make_model()
        db.get = AsyncMock(return_value=model)

        existing = _make_tag("PII", "old", columns=[_make_column()])
        renamed = _make_tag("Sensitive", "old", columns=[_make_column()])
        renamed.id = existing.id

        db.execute = _execute_queue(
            _scalar_one_result(existing),        # initial _load_tag
            _scalar_one_result(renamed),         # post-commit _load_tag
        )

        with patch("src.api.data_tags.get_tenant_db", async_gen_from(db)):
            resp = await client.put(
                f"{TAGS_PREFIX}/{existing.id}",
                json={"tag_name": "Sensitive"},
            )

        assert resp.status_code == 200, resp.text
        assert _added_audit_events(db) == []
        db.commit.assert_awaited()

    @pytest.mark.anyio
    async def test_update_audit_write_failure_rolls_back(self, client):
        """F-008-06 (Bug-8019): the update membership audit is FAIL-CLOSED. If
        the durable AuditEvent cannot be flushed the membership change must roll
        back — the tag's column set never changes without a surviving record."""
        db = make_mock_db()
        model = make_model()
        db.get = AsyncMock(return_value=model)

        old_col = _make_column("email", "customers")
        new_col = _make_column("ssn", "customers")
        existing = _make_tag("PII", columns=[old_col])

        db.execute = _execute_queue(
            _scalar_one_result(existing),        # initial _load_tag
            _scalars_all_result([new_col]),      # _resolve_model_columns
            _scalar_one_result(None),            # audit level
        )
        # First flush = membership pre-flush (succeeds); second = audit flush
        # inside audit_required (fails) -> fail closed.
        calls = {"n": 0}

        async def _flush(*_a, **_kw):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise RuntimeError("audit write failed")

        db.flush = AsyncMock(side_effect=_flush)

        with patch("src.api.data_tags.get_tenant_db", async_gen_from(db)):
            resp = await client.put(
                f"{TAGS_PREFIX}/{existing.id}",
                json={"column_ids": [str(new_col.id)]},
            )

        assert resp.status_code == 500, resp.text
        db.rollback.assert_awaited()
        db.commit.assert_not_called()


# ------------------------------------------------------------------ #
# DELETE /data-tags/{id}
# ------------------------------------------------------------------ #


def _tag_load_result(tag):
    r = MagicMock()
    r.scalar_one_or_none.return_value = tag
    return r


def _persona_names_result(names):
    r = MagicMock()
    r.scalars.return_value.all.return_value = list(names)
    return r


def _delete_execute_queue(*results):
    """Return an AsyncMock whose execute() pops the queued results in order,
    then returns an empty scalars() result for any trailing DELETE."""
    queue = list(results)

    async def _side(*_a, **_kw):
        if queue:
            return queue.pop(0)
        trailing = MagicMock()
        trailing.scalars.return_value.all.return_value = []
        return trailing

    return AsyncMock(side_effect=_side)


class TestDeleteTag:
    @pytest.mark.anyio
    async def test_delete_returns_204(self, client):
        db = make_mock_db()
        model = make_model()
        tag = _make_tag()

        db.get = AsyncMock(return_value=model)
        # F-008-01: the tag is loaded via an eager select, not db.get.
        # Bug-7790: then the dependent-persona check runs (no dependents here).
        db.execute = _delete_execute_queue(
            _tag_load_result(tag),        # _load_tag
            MagicMock(),                  # Bug-7790: FOR UPDATE lock select
            _persona_names_result([]),    # dependent personas -> none
        )

        with patch("src.api.data_tags.get_tenant_db", async_gen_from(db)):
            resp = await client.delete(f"{TAGS_PREFIX}/{tag.id}")
        assert resp.status_code == 204

    @pytest.mark.anyio
    async def test_delete_emits_columns_set_audit_before_empty(self, client):
        """F-008-06 (Bug-8299/Bug-8308): deleting a tag empties its column
        membership (a CLS-surface change), so it must emit a DURABLE
        security.data_tag_columns_set audit whose before-set is the tag's columns and
        after-set is empty — no dependent restrictions on this path, so only the
        columns_set audit fires (not force_delete_cls_widening)."""
        db = make_mock_db()
        model = make_model()
        col1 = _make_column("email", "customers")
        col2 = _make_column("ssn", "customers")
        tag = _make_tag(columns=[col1, col2])

        db.get = AsyncMock(return_value=model)
        db.execute = _delete_execute_queue(
            _tag_load_result(tag),        # _load_tag (eager columns)
            MagicMock(),                  # FOR UPDATE lock select
            _persona_names_result([]),    # no dependent personas
            _scalar_one_result(None),     # audit level -> info
        )

        with patch("src.api.data_tags.get_tenant_db", async_gen_from(db)):
            resp = await client.delete(f"{TAGS_PREFIX}/{tag.id}")

        assert resp.status_code == 204, resp.text
        events = _added_audit_events(db)
        assert len(events) == 1, "exactly one columns_set audit expected"
        ev = events[0]
        assert ev.action == "security.data_tag_columns_set"
        assert ev.severity == "critical"
        assert ev.target_type == "data_tag"
        assert ev.detail["operation"] == "delete"
        assert ev.detail["before_column_ids"] == sorted(
            [str(col1.id), str(col2.id)]
        )
        assert ev.detail["after_column_ids"] == []
        # after ({}) IS a subset of before -> the surface narrowed, not widened.
        assert ev.detail["widens_surface"] is False
        # Actor attribution must survive (the defect was "no actor record").
        assert ev.actor_email == TEST_USER_ID
        assert ev.detail["actor_user_id"] == str(TEST_USER_ID)
        db.delete.assert_awaited_once_with(tag)
        db.commit.assert_awaited()

    @pytest.mark.anyio
    async def test_delete_audit_write_failure_rolls_back(self, client):
        """F-008-06 (Bug-8299/Bug-8308): the delete columns_set audit is
        FAIL-CLOSED. If the durable AuditEvent cannot be flushed, the delete must
        roll back — a tag's column membership never collapses to empty without a
        surviving record, and the tag must survive."""
        db = make_mock_db()
        model = make_model()
        col1 = _make_column("email", "customers")
        tag = _make_tag(columns=[col1])

        db.get = AsyncMock(return_value=model)
        db.execute = _delete_execute_queue(
            _tag_load_result(tag),        # _load_tag
            MagicMock(),                  # FOR UPDATE lock select
            _persona_names_result([]),    # no dependent personas
            _scalar_one_result(None),     # audit level -> info
        )
        # The columns_set audit flush fails -> fail closed, no delete/commit.
        db.flush = AsyncMock(side_effect=RuntimeError("audit write failed"))

        with patch("src.api.data_tags.get_tenant_db", async_gen_from(db)):
            resp = await client.delete(f"{TAGS_PREFIX}/{tag.id}")

        assert resp.status_code == 500, resp.text
        db.rollback.assert_awaited()
        db.delete.assert_not_called()
        db.commit.assert_not_called()

    @pytest.mark.anyio
    async def test_delete_not_found(self, client):
        db = make_mock_db()
        model = make_model()

        db.get = AsyncMock(return_value=model)
        load_result = MagicMock()
        load_result.scalar_one_or_none.return_value = None
        db.execute = AsyncMock(return_value=load_result)

        with patch("src.api.data_tags.get_tenant_db", async_gen_from(db)):
            resp = await client.delete(f"{TAGS_PREFIX}/{uuid.uuid4()}")
        assert resp.status_code == 404

    @pytest.mark.anyio
    async def test_delete_blocked_when_personas_restrict_via_tag(self, client):
        """Bug-7790 [CLS DATA LEAK]: deleting a tag that still enforces CLS for
        some personas must be blocked with 409 — never silently cascade-drop
        the restrictions (which would expose the restricted columns). The tag
        row must NOT be deleted."""
        db = make_mock_db()
        model = make_model()
        tag = _make_tag(tag_name="PII")

        db.get = AsyncMock(return_value=model)
        db.execute = _delete_execute_queue(
            _tag_load_result(tag),                          # _load_tag
            MagicMock(),                                    # FOR UPDATE lock
            _persona_names_result(["Finance", "Support"]),  # dependents
        )

        with patch("src.api.data_tags.get_tenant_db", async_gen_from(db)):
            resp = await client.delete(f"{TAGS_PREFIX}/{tag.id}")

        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert detail["error_code"] == "DATA_TAG_CLS_RESTRICTION_DEPENDENTS"
        assert detail["dependent_personas"] == ["Finance", "Support"]
        # The restriction was NOT dropped and the tag was NOT deleted.
        db.delete.assert_not_called()

    @pytest.mark.anyio
    async def test_delete_forced_purges_restrictions_explicitly(self, client):
        """Bug-7790: force=true is the deliberate path — the restrictions are
        purged by an explicit DELETE (not the silent FK cascade) and the tag is
        then removed."""
        db = make_mock_db()
        model = make_model()
        tag = _make_tag(tag_name="PII")

        db.get = AsyncMock(return_value=model)
        db.execute = _delete_execute_queue(
            _tag_load_result(tag),                    # _load_tag
            MagicMock(),                              # FOR UPDATE lock
            _persona_names_result(["Finance"]),       # dependents present
            MagicMock(),                              # DELETE(PersonaTagRestriction)
            _scalar_one_result(None),                 # audit level -> info (columns_set)
        )

        with patch("src.api.data_tags.get_tenant_db", async_gen_from(db)):
            resp = await client.delete(f"{TAGS_PREFIX}/{tag.id}?force=true")

        assert resp.status_code == 204, resp.text
        # The tag itself was deleted only after the explicit restriction purge.
        db.delete.assert_awaited_once_with(tag)
        # Bug-7790 + F-008-06 (Bug-8299/Bug-8308): the forced path records TWO
        # durable audit events in-transaction — the CLS-widening (dropped
        # persona restrictions) AND the columns_set membership-emptied event.
        from shared.db.models import AuditEvent

        added_audit = [
            c.args[0]
            for c in db.add.call_args_list
            if c.args and isinstance(c.args[0], AuditEvent)
        ]
        actions = {ev.action for ev in added_audit}
        assert actions == {
            "data_tag.force_delete_cls_widening",
            "security.data_tag_columns_set",
        }
        widening = next(
            ev for ev in added_audit
            if ev.action == "data_tag.force_delete_cls_widening"
        )
        assert widening.severity == "critical"
        assert widening.detail["dropped_cls_restriction_personas"] == ["Finance"]
        columns_set = next(
            ev for ev in added_audit if ev.action == "security.data_tag_columns_set"
        )
        assert columns_set.severity == "critical"
        assert columns_set.detail["operation"] == "delete"
        assert columns_set.detail["after_column_ids"] == []

    @pytest.mark.anyio
    async def test_forced_delete_aborts_when_audit_write_fails(self, client):
        """Bug-7790 R2 [CLS DATA LEAK]: the forced-delete audit is FAIL-CLOSED.
        If the durable AuditEvent cannot be flushed, the endpoint must abort and
        the tag (and its CLS restrictions) must survive — never expose a
        restricted column without a surviving audit trail. Also proves the audit
        is UNGATED: it is written directly, not via the level-gated audit()
        writer which suppresses on audit.log_level=off."""
        db = make_mock_db()
        model = make_model()
        tag = _make_tag(tag_name="PII")

        db.get = AsyncMock(return_value=model)
        db.execute = _delete_execute_queue(
            _tag_load_result(tag),               # _load_tag
            MagicMock(),                         # FOR UPDATE lock
            _persona_names_result(["Finance"]),  # dependents present
        )
        # The audit flush fails (e.g. DB write error). The handler flushes right
        # after db.add(AuditEvent); make that flush raise.
        db.flush = AsyncMock(side_effect=RuntimeError("audit write failed"))

        with patch("src.api.data_tags.get_tenant_db", async_gen_from(db)):
            resp = await client.delete(f"{TAGS_PREFIX}/{tag.id}?force=true")

        assert resp.status_code == 500, resp.text
        # Fail closed: rolled back, tag NOT deleted, no commit.
        db.rollback.assert_awaited()
        db.delete.assert_not_called()
        db.commit.assert_not_called()


# ------------------------------------------------------------------ #
# Pydantic schema validation
# ------------------------------------------------------------------ #


class TestDataTagSchemas:
    def test_create_valid(self):
        from shared.schemas.pydantic_models import DataTagCreate

        obj = DataTagCreate(tag_name="PII", description="Personal data")
        assert obj.tag_name == "PII"

    def test_create_with_columns(self):
        from shared.schemas.pydantic_models import DataTagCreate

        col_id = uuid.uuid4()
        obj = DataTagCreate(
            tag_name="Financial",
            column_ids=[col_id],
        )
        assert len(obj.column_ids) == 1

    def test_response_with_columns(self):
        from shared.schemas.pydantic_models import DataTagResponse, DataTagColumnInfo

        col = DataTagColumnInfo(
            column_id=uuid.uuid4(),
            table_name="customers",
            column_name="email",
        )
        resp = DataTagResponse(
            id=uuid.uuid4(),
            model_id=TEST_MODEL_ID,
            tag_name="PII",
            description=None,
            created_at=NOW,
            columns=[col],
        )
        assert resp.columns[0].column_name == "email"

    def test_restriction_request(self):
        from shared.schemas.pydantic_models import PersonaTagRestrictionRequest

        req = PersonaTagRestrictionRequest(
            tag_ids=[uuid.uuid4(), uuid.uuid4()]
        )
        assert len(req.tag_ids) == 2

    def test_restriction_response(self):
        from shared.schemas.pydantic_models import PersonaTagRestrictionResponse

        resp = PersonaTagRestrictionResponse(
            tag_id=uuid.uuid4(),
            tag_name="PII",
            description="Personal data",
            column_count=5,
        )
        assert resp.column_count == 5
