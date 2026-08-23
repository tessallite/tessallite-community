"""Bug-8096 behavior guards for model-owned measure rename consumers.

Test escape: rename validation revalidated the model after changing the measure
row but never rewrote persisted name-based consumers. Guard: exact DSL and SQL
references move transactionally; ambiguous/unparseable owned references refuse
the rename with an object-level diagnostic. Tier: T1 contract.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from shared.db.models import (
    ModelAliasMap,
    ProjectCrossModelRecipe,
    QuantileCoverage,
    ScratchpadMeasure,
)
from src.measure_rename import (
    UnsafeRenameReference,
    UnsafeMeasureRename,
    propagate_measure_renames,
    rewrite_measure_calls,
    rewrite_semantic_sql,
)

from .conftest import (
    TEST_MODEL_ID,
    TEST_PROJECT_ID,
    async_gen_from,
    client,
    make_mock_db,
)


class _Scalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return _Scalars(self._rows)


class _Db:
    def __init__(self, *row_sets):
        self._row_sets = list(row_sets)
        self.deleted = []

    async def execute(self, _stmt):
        return _Result(self._row_sets.pop(0) if self._row_sets else [])

    async def delete(self, row):
        self.deleted.append(row)


def test_measure_dsl_batch_rename_is_simultaneous_and_exact():
    expression = 'safe_div(measure("Revenue"), measure("Cost"))'
    rewritten, changed = rewrite_measure_calls(
        expression, {"Revenue": "Net Revenue", "Cost": "Revenue"}
    )
    assert changed is True
    assert rewritten == 'safe_div(measure("Net Revenue"), measure("Revenue"))'


def test_semantic_sql_rewrites_only_column_identifier():
    rewritten, changed = rewrite_semantic_sql(
        'SELECT SUM("Revenue") AS total FROM "sales" WHERE note = \'Revenue\'',
        {"Revenue": "Net Revenue"},
    )
    assert changed is True
    assert 'SUM("Net Revenue")' in rewritten
    assert "note = 'Revenue'" in rewritten


@pytest.mark.asyncio
async def test_owned_calculated_kpi_json_and_saved_sql_consumers_propagate():
    model_id = uuid.uuid4()
    renamed_id = uuid.uuid4()
    calculated = SimpleNamespace(
        id=uuid.uuid4(), expression='measure("Revenue") * 0.8'
    )
    kpi = SimpleNamespace(
        id=uuid.uuid4(),
        expression='measure("Revenue")',
        target_expression='measure("Revenue") * 1.1',
        status_expression=None,
        trend_expression=None,
        business_definition={
            "measure_name": "Revenue",
            "formula": 'measure("Revenue")',
        },
    )
    saved = SimpleNamespace(
        id=uuid.uuid4(),
        query_type="sql",
        query_text='SELECT SUM("Revenue") FROM "model"',
    )
    db = _Db([calculated], [kpi], [saved], [])

    await propagate_measure_renames(
        db, model_id, {renamed_id: ("Revenue", "Net Revenue")}
    )

    assert calculated.expression == 'measure("Net Revenue") * 0.8'
    assert kpi.expression == 'measure("Net Revenue")'
    assert kpi.target_expression == 'measure("Net Revenue") * 1.1'
    assert kpi.business_definition["measure_name"] == "Net Revenue"
    assert 'SUM("Net Revenue")' in saved.query_text


@pytest.mark.asyncio
async def test_bug_8829_ambiguous_dimension_name_does_not_rewrite_saved_sql():
    """A SQL column name alone cannot identify measure versus dimension.

    The model permits both semantic object classes to use ``Revenue``. Without
    binding, rewriting every matching ``Column`` changes the grouping dimension
    as well as the measure. The consumer must fail closed and leave the query
    byte-for-byte unchanged.
    """
    model_id = uuid.uuid4()
    saved_id = uuid.uuid4()
    original = (
        'SELECT "Revenue", SUM("Revenue") FROM "model" '
        'GROUP BY "Revenue"'
    )
    saved = SimpleNamespace(
        id=saved_id,
        query_type="sql",
        query_text=original,
    )
    # The ninth result is the model's colliding Dimension.name inventory. The
    # original implementation never issues that query and therefore rewrites
    # all three occurrences instead of refusing.
    db = _Db([], [], [saved], [], [], [], [], [], ["Revenue"])

    with pytest.raises(UnsafeMeasureRename) as exc:
        await propagate_measure_renames(
            db,
            model_id,
            {uuid.uuid4(): ("Revenue", "Net Revenue")},
        )

    assert f"saved_query:{saved_id}.query_text" in str(exc.value)
    assert saved.query_text == original


@pytest.mark.asyncio
async def test_bug_8829_rename_to_dimension_name_does_not_create_ambiguity():
    """The destination name can collide even when the source name did not."""
    model_id = uuid.uuid4()
    saved_id = uuid.uuid4()
    original = 'SELECT SUM("Revenue") FROM "model"'
    saved = SimpleNamespace(
        id=saved_id,
        query_type="sql",
        query_text=original,
    )
    db = _Db([], [], [saved], [], [], [], [], [], ["Profit"])

    with pytest.raises(UnsafeMeasureRename) as exc:
        await propagate_measure_renames(
            db,
            model_id,
            {uuid.uuid4(): ("Revenue", "Profit")},
        )

    assert f"saved_query:{saved_id}.query_text" in str(exc.value)
    assert saved.query_text == original


@pytest.mark.asyncio
async def test_unsafe_named_set_reference_refuses_with_object_identity():
    named_set_id = uuid.uuid4()
    named_set = SimpleNamespace(
        id=named_set_id,
        expression="{[Measures].[Revenue]}",
        builder_definition=None,
    )
    db = _Db([], [], [], [named_set])

    with pytest.raises(UnsafeMeasureRename) as exc:
        await propagate_measure_renames(
            db,
            uuid.uuid4(),
            {uuid.uuid4(): ("Revenue", "Net Revenue")},
        )

    assert f"named_set:{named_set_id}.expression" in str(exc.value)


@pytest.mark.asyncio
async def test_non_sql_saved_query_reference_refuses_instead_of_guessing():
    saved_id = uuid.uuid4()
    saved = SimpleNamespace(
        id=saved_id,
        query_type="dax",
        query_text="SUM('Model'[Revenue])",
    )
    db = _Db([], [], [saved], [])

    with pytest.raises(UnsafeMeasureRename) as exc:
        await propagate_measure_renames(
            db,
            uuid.uuid4(),
            {uuid.uuid4(): ("Revenue", "Net Revenue")},
        )

    assert f"saved_query:{saved_id}.query_text" in str(exc.value)


@pytest.mark.asyncio
async def test_persisted_scratchpad_rewrites_and_quantile_proof_is_invalidated():
    model_id = uuid.uuid4()
    renamed_id = uuid.uuid4()
    scratchpad = ScratchpadMeasure(
        id=uuid.uuid4(),
        model_id=model_id,
        name="working margin",
        expression='safe_div(measure("Revenue"), measure("Cost"))',
        created_by="analyst@example.com",
    )
    coverage = QuantileCoverage(
        id=uuid.uuid4(),
        aggregate_column_id=uuid.uuid4(),
        aggregate_definition_id=uuid.uuid4(),
        measure_id=renamed_id,
        semantic_measure_name="Revenue",
        input_expression_fingerprint="revenue|numeric|source-column",
        fraction="0.95",
        method="continuous",
        order_direction="asc",
        null_policy="ignore_nulls",
        exactness="exact",
    )
    db = _Db([], [], [], [], [scratchpad], [coverage])

    await propagate_measure_renames(
        db, model_id, {renamed_id: ("Revenue", "Net Revenue")}
    )

    assert scratchpad.expression == (
        'safe_div(measure("Net Revenue"), measure("Cost"))'
    )
    assert db.deleted == [coverage]


@pytest.mark.asyncio
async def test_unsupported_reference_applies_no_planned_persistent_updates():
    model_id = uuid.uuid4()
    renamed_id = uuid.uuid4()
    calculated = SimpleNamespace(
        id=uuid.uuid4(), expression='measure("Revenue") * 0.8'
    )
    named_set = SimpleNamespace(
        id=uuid.uuid4(),
        expression="{[Measures].[Revenue]}",
        builder_definition=None,
    )
    scratchpad = ScratchpadMeasure(
        id=uuid.uuid4(),
        model_id=model_id,
        name="working revenue",
        expression='measure("Revenue")',
        created_by="analyst@example.com",
    )
    db = _Db([calculated], [], [], [named_set], [scratchpad], [])

    with pytest.raises(UnsafeMeasureRename):
        await propagate_measure_renames(
            db, model_id, {renamed_id: ("Revenue", "Net Revenue")}
        )

    assert calculated.expression == 'measure("Revenue") * 0.8'
    assert scratchpad.expression == 'measure("Revenue")'
    assert db.deleted == []


@pytest.mark.asyncio
async def test_recipe_and_alias_rewrite_exact_target_model_references_only():
    model_id = uuid.uuid4()
    other_model_id = uuid.uuid4()
    recipe = ProjectCrossModelRecipe(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        name="Revenue comparison",
        parameters=[],
        steps=[
            {
                "name": "target",
                "model_id": str(model_id),
                "measures": ["Revenue", "Cost"],
            },
            {
                "name": "other",
                "model_id": str(other_model_id),
                "measures": ["Revenue"],
            },
        ],
        combine={
            "op": "add",
            "args": [
                {"ref": {"step": "target", "measure": "Revenue"}},
                {"ref": {"step": "other", "measure": "Revenue"}},
            ],
        },
    )
    alias_row = ModelAliasMap(
        model_id=model_id,
        alias_map={"sales": "Revenue", "usd sales": "Revenue USD"},
    )
    db = _Db([], [], [], [], [], [], [recipe], [alias_row])

    await propagate_measure_renames(
        db, model_id, {uuid.uuid4(): ("Revenue", "Net Revenue")}
    )

    assert recipe.steps[0]["measures"] == ["Net Revenue", "Cost"]
    assert recipe.steps[1]["measures"] == ["Revenue"]
    assert recipe.combine["args"][0]["ref"]["measure"] == "Net Revenue"
    assert recipe.combine["args"][1]["ref"]["measure"] == "Revenue"
    assert alias_row.alias_map == {
        "sales": "Net Revenue",
        "usd sales": "Revenue USD",
    }


@pytest.mark.asyncio
async def test_ambiguous_recipe_reference_reports_json_path_and_applies_nothing():
    model_id = uuid.uuid4()
    calculated = SimpleNamespace(
        id=uuid.uuid4(), expression='measure("Revenue") * 0.8'
    )
    recipe = ProjectCrossModelRecipe(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        name="Broken recipe",
        parameters=[],
        steps=[
            {
                "name": "sales",
                "model_id": "not-a-uuid",
                "measures": ["Revenue"],
            }
        ],
        combine=None,
    )
    alias_row = ModelAliasMap(
        model_id=model_id, alias_map={"sales": "Revenue"}
    )
    db = _Db([calculated], [], [], [], [], [], [recipe], [alias_row])

    with pytest.raises(UnsafeMeasureRename) as exc:
        await propagate_measure_renames(
            db, model_id, {uuid.uuid4(): ("Revenue", "Net Revenue")}
        )

    assert f"cross_model_recipe:{recipe.id}$.steps[0].model_id" in str(exc.value)
    assert calculated.expression == 'measure("Revenue") * 0.8'
    assert recipe.steps[0]["measures"] == ["Revenue"]
    assert alias_row.alias_map == {"sales": "Revenue"}


@pytest.mark.asyncio
async def test_malformed_alias_value_refuses_recipe_and_alias_partial_updates():
    model_id = uuid.uuid4()
    recipe = ProjectCrossModelRecipe(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        name="Revenue recipe",
        parameters=[],
        steps=[
            {
                "name": "sales",
                "model_id": str(model_id),
                "measures": ["Revenue"],
            }
        ],
        combine={"ref": {"step": "sales", "measure": "Revenue"}},
    )
    alias_row = ModelAliasMap(
        model_id=model_id,
        alias_map={"sales": "Revenue", "broken": {"canonical": "Revenue"}},
    )
    db = _Db([], [], [], [], [], [], [recipe], [alias_row])

    with pytest.raises(UnsafeMeasureRename) as exc:
        await propagate_measure_renames(
            db, model_id, {uuid.uuid4(): ("Revenue", "Net Revenue")}
        )

    assert f"model_alias_map:{model_id}$.alias_map['broken']" in str(exc.value)
    assert recipe.steps[0]["measures"] == ["Revenue"]
    assert recipe.combine["ref"]["measure"] == "Revenue"
    assert alias_row.alias_map["sales"] == "Revenue"


@pytest.mark.asyncio
async def test_affected_name_in_nested_malformed_combine_reports_exact_path():
    model_id = uuid.uuid4()
    recipe = ProjectCrossModelRecipe(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        name="Malformed affected combine",
        parameters=[],
        steps=[
            {
                "name": "sales",
                "model_id": str(model_id),
                "measures": ["Revenue"],
            }
        ],
        combine={
            "op": "add",
            "args": [
                {"ref": {"step": "sales", "measure": "Revenue"}},
                {
                    "ref": {
                        "step": "sales",
                        "measure": {"legacy": "Revenue"},
                    }
                },
            ],
        },
    )
    db = _Db([], [], [], [], [], [], [recipe], [])

    with pytest.raises(UnsafeMeasureRename) as exc:
        await propagate_measure_renames(
            db, model_id, {uuid.uuid4(): ("Revenue", "Net Revenue")}
        )

    assert (
        f"cross_model_recipe:{recipe.id}"
        "$.combine.args[1].ref.measure"
    ) in str(exc.value)
    assert recipe.steps[0]["measures"] == ["Revenue"]
    assert recipe.combine["args"][0]["ref"]["measure"] == "Revenue"


@pytest.mark.asyncio
async def test_unrelated_malformed_values_do_not_block_affected_exact_rewrites():
    model_id = uuid.uuid4()
    recipe = ProjectCrossModelRecipe(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        name="Legacy unrelated shape",
        parameters=[],
        steps=[
            {
                "name": "sales",
                "model_id": str(model_id),
                "measures": ["Revenue", {"legacy": "Other Measure"}],
            }
        ],
        combine={
            "op": "add",
            "args": [
                {"ref": {"step": "sales", "measure": "Revenue"}},
                {
                    "ref": {
                        "step": None,
                        "measure": "Unrelated Measure",
                    }
                },
            ],
        },
    )
    alias_row = ModelAliasMap(
        model_id=model_id,
        alias_map={
            "sales": "Revenue",
            "legacy": {"canonical": "Unrelated Measure"},
        },
    )
    db = _Db([], [], [], [], [], [], [recipe], [alias_row])

    await propagate_measure_renames(
        db, model_id, {uuid.uuid4(): ("Revenue", "Net Revenue")}
    )

    assert recipe.steps[0]["measures"] == [
        "Net Revenue",
        {"legacy": "Other Measure"},
    ]
    assert recipe.combine["args"][0]["ref"]["measure"] == "Net Revenue"
    assert recipe.combine["args"][1] == {
        "ref": {"step": None, "measure": "Unrelated Measure"}
    }
    assert alias_row.alias_map == {
        "sales": "Net Revenue",
        "legacy": {"canonical": "Unrelated Measure"},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "op_value", [["Revenue"], {"legacy": "Revenue"}],
    ids=["array-op", "object-op"],
)
async def test_affected_malformed_operator_refuses_at_exact_path_and_rolls_back(
    op_value,
):
    model_id = uuid.uuid4()
    recipe = ProjectCrossModelRecipe(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        name="Affected malformed operator",
        parameters=[],
        steps=[
            {
                "name": "sales",
                "model_id": str(model_id),
                "measures": ["Revenue"],
            }
        ],
        combine={"op": op_value, "args": []},
    )
    db = _Db([], [], [], [], [], [], [recipe], [])

    with pytest.raises(UnsafeMeasureRename) as exc:
        await propagate_measure_renames(
            db, model_id, {uuid.uuid4(): ("Revenue", "Net Revenue")}
        )

    assert f"cross_model_recipe:{recipe.id}$.combine.op" in str(exc.value)
    assert recipe.steps[0]["measures"] == ["Revenue"]
    assert recipe.combine == {"op": op_value, "args": []}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "op_value", [["Unrelated"], {"legacy": "Unrelated"}],
    ids=["array-op", "object-op"],
)
async def test_unrelated_malformed_operator_does_not_block_valid_step_rename(
    op_value,
):
    model_id = uuid.uuid4()
    recipe = ProjectCrossModelRecipe(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        name="Unrelated malformed operator",
        parameters=[],
        steps=[
            {
                "name": "sales",
                "model_id": str(model_id),
                "measures": ["Revenue"],
            }
        ],
        combine={"op": op_value, "args": []},
    )
    db = _Db([], [], [], [], [], [], [recipe], [])

    await propagate_measure_renames(
        db, model_id, {uuid.uuid4(): ("Revenue", "Net Revenue")}
    )

    assert recipe.steps[0]["measures"] == ["Net Revenue"]
    assert recipe.combine == {"op": op_value, "args": []}


@pytest.mark.asyncio
async def test_stable_id_only_consumers_are_not_mutated():
    renamed_id = uuid.uuid4()
    variant = SimpleNamespace(variant_of_measure_id=renamed_id)
    pivot = SimpleNamespace(measure_id=str(renamed_id))
    drill = SimpleNamespace(measure_id=renamed_id)
    persona = SimpleNamespace(measure_ids=[str(renamed_id)])
    db = _Db([], [], [], [], [], [])

    await propagate_measure_renames(
        db, uuid.uuid4(), {renamed_id: ("Revenue", "Net Revenue")}
    )

    assert variant.variant_of_measure_id == renamed_id
    assert pivot.measure_id == str(renamed_id)
    assert drill.measure_id == renamed_id
    assert persona.measure_ids == [str(renamed_id)]


@pytest.mark.asyncio
async def test_measure_patch_unsupported_reference_returns_409_and_rolls_back(client):
    measure_id = uuid.uuid4()
    row = SimpleNamespace(
        id=measure_id,
        model_id=TEST_MODEL_ID,
        name="Revenue",
        display_name="Revenue",
        measure_type="standard",
        variant_kind=None,
    )
    db = make_mock_db()
    db.get = AsyncMock(return_value=row)

    async def _scope_ok(_db, *, project_id, model_id):
        return None

    blocker = UnsafeMeasureRename([
        UnsafeRenameReference("named_set", str(uuid.uuid4()), "expression")
    ])
    with (
        patch("src.api.measures.ensure_model_in_project", _scope_ok),
        patch("src.api.measures.get_tenant_db", async_gen_from(db)),
        patch(
            "shared.audit.logger._get_audit_level",
            new=AsyncMock(return_value="off"),
        ),
        patch(
            "src.api.measures.propagate_measure_renames",
            new=AsyncMock(side_effect=blocker),
        ),
    ):
        response = await client.patch(
            f"/api/v1/projects/{TEST_PROJECT_ID}/models/"
            f"{TEST_MODEL_ID}/measures/{measure_id}",
            json={"name": "Net Revenue"},
        )

    assert response.status_code == 409, response.text
    assert "named_set" in response.json()["detail"]
    db.rollback.assert_awaited_once()
    assert row.name == "Revenue"
