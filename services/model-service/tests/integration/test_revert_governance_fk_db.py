"""Bug-6851 real-Postgres coverage for governance-revert FK boundaries.

The unit tests around ``rehydrate_into_live`` prove the governance decision and
the SQL statement shapes with a session double.  This tier proves the
database-owned behavior those tests cannot: a preserved governance row is
detached before definition-table deletion, the definition is rebuilt with the
same IDs, and the two FK couplings are either restored or left detached when
the reverted-to snapshot no longer contains their targets.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from shared.db.models import (
    DataSource,
    DataTag,
    Model,
    ModelAlert,
    ModelColumn,
    ModelTable,
    ProjectConnection,
    RowSecurityRule,
    data_tag_columns,
)
from shared.model_snapshot.rehydrator import rehydrate_into_live

from tests.integration.test_versioning_consistency_db import (
    _DB_URL,
    _isolated_schema,
    _seed_model,
)

pytestmark = [pytest.mark.integration]


async def _seed_governance_graph(session, model_id: uuid.UUID) -> dict[str, uuid.UUID]:
    """Create one live table/column plus both preserved governance couplings."""
    # ``_seed_model`` intentionally keeps the fixture small and does not expose
    # the project id. Resolve it through the same database boundary the
    # connection FK enforces.
    project_id = (
        await session.execute(select(Model.project_id).where(Model.id == model_id))
    ).scalar_one()

    connection_id = uuid.uuid4()
    source_id = uuid.uuid4()
    table_id = uuid.uuid4()
    column_id = uuid.uuid4()
    tag_id = uuid.uuid4()
    rule_id = uuid.uuid4()

    session.add(
        ProjectConnection(
            id=connection_id,
            project_id=project_id,
            display_name="source",
            connection_type="postgresql",
            encrypted_credentials=b"test",
        )
    )
    session.add(
        DataSource(
            id=source_id,
            model_id=model_id,
            project_connection_id=connection_id,
            source_type="postgresql",
            display_name="source",
        )
    )
    session.add(
        ModelTable(
            id=table_id,
            model_id=model_id,
            source_id=source_id,
            table_type="fact",
            physical_name="fact_orders",
            alias="orders",
            display_name="Orders",
        )
    )
    session.add(
        ModelColumn(
            id=column_id,
            model_table_id=table_id,
            column_name="region",
            data_type="text",
        )
    )
    # The ORM has no relationship edge from RowSecurityRule to ModelTable, so
    # explicitly flush the referenced definition rows before inserting the
    # RESTRICT FK and make the setup's ordering visible in the test.
    await session.flush()
    session.add(DataTag(id=tag_id, model_id=model_id, tag_name="sensitive"))
    session.add(
        RowSecurityRule(
            id=rule_id,
            model_id=model_id,
            name="region_rule",
            dimension_path="orders.region",
            rule_type="user_mapping",
            mapping_table_id=table_id,
            mapping_user_column="user_id",
            mapping_value_column="region",
        )
    )
    await session.flush()
    await session.execute(
        data_tag_columns.insert().values(tag_id=tag_id, model_column_id=column_id)
    )
    await session.commit()
    return {
        "connection_id": connection_id,
        "source_id": source_id,
        "table_id": table_id,
        "column_id": column_id,
        "tag_id": tag_id,
        "rule_id": rule_id,
    }


def _snapshot(model_id: uuid.UUID, ids: dict[str, uuid.UUID], *, keep_definition: bool) -> dict:
    """Build the two supported revert targets without deriving answers from DB state."""
    snapshot = {
        "schema_version": 5,
        "model": {"id": str(model_id)},
        "data_sources": [],
        "tables": [],
        "columns": [],
        "hierarchies": [],
    }
    if keep_definition:
        snapshot["data_sources"] = [
            {
                "id": str(ids["source_id"]),
                "project_connection_id": str(ids["connection_id"]),
                "source_type": "postgresql",
                "display_name": "source",
            }
        ]
        snapshot["tables"] = [
            {
                "id": str(ids["table_id"]),
                "source_id": str(ids["source_id"]),
                "table_type": "fact",
                "physical_name": "fact_orders_v1",
                "alias": "orders",
                "display_name": "Orders v1",
            }
        ]
        snapshot["columns"] = [
            {
                "id": str(ids["column_id"]),
                "model_table_id": str(ids["table_id"]),
                "column_name": "region",
                "data_type": "text",
            }
        ]
    return snapshot


@pytest.mark.skipif(not _DB_URL, reason="no versioning DB URL configured")
@pytest.mark.asyncio
@pytest.mark.parametrize("keep_definition", [True, False])
async def test_bug_6851_real_postgres_revert_reattaches_or_detaches_governance_fks(
    keep_definition: bool,
) -> None:
    """Bug-6851: real FK enforcement must match the revert contract.

    The positive case proves the original IDs are re-attached after the
    model-table/column rebuild.  The negative case proves a removed target is
    left NULL/absent and produces the operator alert instead of crashing or
    attaching to a different definition.
    """
    async with _isolated_schema() as (factory, _schema):
        async with factory() as setup:
            model_id = await _seed_model(setup)
            ids = await _seed_governance_graph(setup, model_id)

        async with factory() as session:
            await rehydrate_into_live(
                model_id,
                _snapshot(model_id, ids, keep_definition=keep_definition),
                session,
                preserve_aggregates=True,
                preserve_pockets=True,
                restore_governance=False,
                drop_orphan_aggregates=False,
                actor="bug-6851-test",
            )
            await session.commit()

        async with factory() as verify:
            rule = (
                await verify.execute(
                    select(RowSecurityRule).where(RowSecurityRule.id == ids["rule_id"])
                )
            ).scalar_one()
            links = (
                await verify.execute(
                    select(data_tag_columns.c.model_column_id).where(
                        data_tag_columns.c.tag_id == ids["tag_id"]
                    )
                )
            ).scalars().all()
            alerts = (
                await verify.execute(
                    select(ModelAlert).where(
                        ModelAlert.model_id == model_id,
                        ModelAlert.category == "governance_revert",
                    )
                )
            ).scalars().all()

        if keep_definition:
            assert rule.mapping_table_id == ids["table_id"]
            assert links == [ids["column_id"]]
            assert alerts == []
        else:
            assert rule.mapping_table_id is None
            assert links == []
            assert len(alerts) == 1
            assert alerts[0].severity == "warning"
