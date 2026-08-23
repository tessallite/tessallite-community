"""
Join CRUD routes.
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select

from shared.db.models import Join, ModelColumn, ModelTable
from shared.db.session import get_tenant_db
from shared.schemas.pydantic_models import (
    JoinCreate,
    JoinResponse,
    JoinUpdate,
    coerce_population_participation,
    coerce_population_participation_source,
)
from shared.semantic.graph_order import is_fact_table
from src.api._model_lock import acquire_model_definition_lock
from src.api._scope import ensure_model_in_project
from src.auth.middleware import CurrentUser, forbid_embed_user
from src.auth.rbac import require_role
from src.api._column_helpers import resolve_column

router = APIRouter(
    prefix="/projects/{project_id}/models/{model_id}/joins", tags=["joins"]
)


async def _build_join_response(
    db,
    join: Join,
    *,
    table_cache: dict[UUID, ModelTable] | None = None,
) -> JoinResponse:
    """Build the one authoritative join response, including type warnings."""
    left_col = await db.get(ModelColumn, join.left_column_id)
    right_col = await db.get(ModelColumn, join.right_column_id)
    warnings: list[str] | None = None
    if left_col and right_col:
        cache = table_cache if table_cache is not None else {}
        for table_id in (join.left_table_id, join.right_table_id):
            if table_id not in cache:
                cache[table_id] = await db.get(ModelTable, table_id)
        left_table = cache.get(join.left_table_id)
        right_table = cache.get(join.right_table_id)
        if left_table and right_table:
            mismatch = _check_join_type_mismatch(
                left_col,
                right_col,
                left_table,
                right_table,
                left_col.column_name,
                right_col.column_name,
            )
            warnings = mismatch or None
    return JoinResponse(
        id=join.id,
        model_id=join.model_id,
        left_table_id=join.left_table_id,
        right_table_id=join.right_table_id,
        join_type=join.join_type,
        cardinality=join.cardinality,
        # Bug-8615 G1: coerced on READ so a row written before the field
        # existed, or one carrying a value from a hand-edited bundle, cannot
        # make the joins list 500.
        population_participation=coerce_population_participation(
            join.population_participation
        ),
        population_participation_source=coerce_population_participation_source(
            getattr(join, "population_participation_source", None)
        ),
        left_column_id=join.left_column_id,
        right_column_id=join.right_column_id,
        left_column_name=left_col.column_name if left_col else None,
        right_column_name=right_col.column_name if right_col else None,
        created_at=join.created_at,
        warnings=warnings,
    )


async def _auto_hide_dim_join_key(
    db,
    left_table: ModelTable,
    right_table: ModelTable,
    left_col: ModelColumn,
    right_col: ModelColumn,
) -> None:
    """Hide the dimension-side join key column when a join is created.

    Only acts on fact-to-dimension joins.  Skips columns already managed
    by the user (hidden_reason='user').
    """
    dim_col: ModelColumn | None = None
    if is_fact_table(left_table) and right_table.table_type.startswith("dim_"):
        dim_col = right_col
    elif is_fact_table(right_table) and left_table.table_type.startswith("dim_"):
        dim_col = left_col

    if dim_col is None:
        return
    if dim_col.hidden_reason == "user":
        return

    dim_col.is_hidden = True
    dim_col.hidden_reason = "join"
    await db.flush()


async def _auto_unhide_dim_join_key(
    db,
    model_id,
    left_table: ModelTable,
    right_table: ModelTable,
    left_col: ModelColumn,
    right_col: ModelColumn,
    excluded_join_id=None,
) -> None:
    """Unhide the dimension-side join key when the last join referencing it
    is removed.

    Skips columns hidden by the user (hidden_reason='user') or columns
    that are still referenced by another join.
    """
    dim_col: ModelColumn | None = None
    if is_fact_table(left_table) and right_table.table_type.startswith("dim_"):
        dim_col = right_col
    elif is_fact_table(right_table) and left_table.table_type.startswith("dim_"):
        dim_col = left_col

    if dim_col is None:
        return
    if dim_col.hidden_reason != "join":
        return

    # Check if any other join still references this column
    other_joins = (
        await db.execute(
            select(Join).where(
                Join.model_id == model_id,
                (Join.left_column_id == dim_col.id)
                | (Join.right_column_id == dim_col.id),
                *([Join.id != excluded_join_id] if excluded_join_id else []),
            )
        )
    ).scalars().all()

    if len(other_joins) > 0:
        return

    dim_col.is_hidden = False
    dim_col.hidden_reason = None
    await db.flush()


@router.post(
    "",
    response_model=JoinResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[require_role("modeler")],
)
async def create_join(
    project_id: UUID,
    model_id: UUID,
    body: JoinCreate,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> JoinResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(
            db, project_id=project_id, model_id=model_id,
        )
        await acquire_model_definition_lock(db, model_id)  # Bug-7982 cross-family lock (read-modify-write: entity read UNDER lock)
        # Verify tables exist and belong to this model
        left_table = await db.get(ModelTable, body.left_table_id)
        if left_table is None or left_table.model_id != model_id:
            raise HTTPException(status_code=404, detail="Left table not found")
        right_table = await db.get(ModelTable, body.right_table_id)
        if right_table is None or right_table.model_id != model_id:
            raise HTTPException(status_code=404, detail="Right table not found")

        # Resolve column names to ModelColumn records
        left_col = await resolve_column(
            db, body.left_table_id, body.left_column_name,
            model_id=model_id, project_id=project_id,
            field_name="left_table_id",
        )
        right_col = await resolve_column(
            db, body.right_table_id, body.right_column_name,
            model_id=model_id, project_id=project_id,
            field_name="right_table_id",
        )

        # Pydantic applies the compatibility default to the request model, so
        # inspect the original field set to distinguish an omitted value from
        # an explicit modeller choice of the same token.
        participation_source = (
            "manual"
            if "population_participation" in body.model_fields_set
            else "default"
        )
        j = Join(
            model_id=model_id,
            left_table_id=body.left_table_id,
            right_table_id=body.right_table_id,
            join_type=body.join_type,
            cardinality=body.cardinality,
            # Bug-8615 G1: the schema default is ``preserve_base_rows``, so a
            # caller that does not send this field creates exactly the join it
            # created before the field existed.
            population_participation=body.population_participation,
            population_participation_source=participation_source,
            left_column_id=left_col.id,
            right_column_id=right_col.id,
        )
        db.add(j)

        await _auto_hide_dim_join_key(
            db, left_table, right_table, left_col, right_col,
        )

        await db.commit()
        await db.refresh(j)
        return await _build_join_response(db, j)


@router.get("", response_model=list[JoinResponse], dependencies=[require_role("viewer")])
async def list_joins(
    project_id: UUID,
    model_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> list[JoinResponse]:
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(
            db, project_id=project_id, model_id=model_id,
        )
        result = await db.execute(
            select(Join).where(Join.model_id == model_id)
        )
        joins = result.scalars().all()
        table_cache: dict[UUID, ModelTable] = {}
        return [
            await _build_join_response(db, j, table_cache=table_cache)
            for j in joins
        ]


@router.get("/{join_id}", response_model=JoinResponse, dependencies=[require_role("viewer")])
async def get_join(
    project_id: UUID,
    model_id: UUID,
    join_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> JoinResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(
            db, project_id=project_id, model_id=model_id,
        )
        j = await db.get(Join, join_id)
        if j is None or j.model_id != model_id:
            raise HTTPException(status_code=404, detail="Join not found")
        return await _build_join_response(db, j)


@router.patch(
    "/{join_id}",
    response_model=JoinResponse,
    dependencies=[require_role("modeler")],
)
async def update_join(
    project_id: UUID,
    model_id: UUID,
    join_id: UUID,
    body: JoinUpdate,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> JoinResponse:
    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(
            db, project_id=project_id, model_id=model_id,
        )
        await acquire_model_definition_lock(db, model_id)  # Bug-7982 cross-family lock (read-modify-write: entity read UNDER lock)
        j = await db.get(Join, join_id)
        if j is None or j.model_id != model_id:
            raise HTTPException(status_code=404, detail="Join not found")

        from shared.semantic.model_validator import revalidate_model

        # Capture old column IDs for auto-hide delta
        old_left_col_id = j.left_column_id
        old_right_col_id = j.right_column_id

        data = body.model_dump(exclude_unset=True)
        join_cols_changed = False
        if "join_type" in data:
            j.join_type = data["join_type"]
        if "cardinality" in data:
            # Orthogonal to join_type (contract invariant 3): declaring the
            # cardinality never changes the rendered JOIN keyword.
            j.cardinality = data["cardinality"]
        if data.get("population_participation") is not None:
            # Bug-8615 G1. Orthogonal to BOTH fields above: it declares the
            # modeller's INTENT about the model's row population, and in this
            # phase nothing reads it at serve time (wiring is phase G3). An
            # explicit null is ignored rather than treated as "clear it" —
            # ``population_participation`` is NOT NULL and its four states are
            # all meaningful, so there is nothing to clear TO.
            j.population_participation = data["population_participation"]
            j.population_participation_source = "manual"
        if "left_column_name" in data:
            # ``j.left_table_id`` is persisted state on a join already proven to
            # belong to this model, not a body value — but the model context is
            # still passed, because the engine's guarantee is that NO caller can
            # write a column into an unscoped table, not that each caller
            # remembers whether its own table id was trustworthy.
            left_col = await resolve_column(
                db, j.left_table_id, data["left_column_name"],
                model_id=model_id, project_id=project_id,
                field_name="left_table_id",
            )
            j.left_column_id = left_col.id
            join_cols_changed = True
        if "right_column_name" in data:
            right_col = await resolve_column(
                db, j.right_table_id, data["right_column_name"],
                model_id=model_id, project_id=project_id,
                field_name="right_table_id",
            )
            j.right_column_id = right_col.id
            join_cols_changed = True

        if join_cols_changed:
            # Unhide old dimension-side column, hide new one
            left_table = await db.get(ModelTable, j.left_table_id)
            right_table = await db.get(ModelTable, j.right_table_id)
            old_left_col = await db.get(ModelColumn, old_left_col_id)
            old_right_col = await db.get(ModelColumn, old_right_col_id)
            new_left_col = await db.get(ModelColumn, j.left_column_id)
            new_right_col = await db.get(ModelColumn, j.right_column_id)

            if left_table and right_table and old_left_col and old_right_col:
                await _auto_unhide_dim_join_key(
                    db, model_id, left_table, right_table,
                    old_left_col, old_right_col, excluded_join_id=join_id,
                )
            if left_table and right_table and new_left_col and new_right_col:
                await _auto_hide_dim_join_key(
                    db, left_table, right_table,
                    new_left_col, new_right_col,
                )

            await db.flush()
            await revalidate_model(model_id, db)

        await db.commit()
        await db.refresh(j)

        return await _build_join_response(db, j)


@router.delete(
    "/{join_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[require_role("modeler")],
)
async def delete_join(
    project_id: UUID,
    model_id: UUID,
    join_id: UUID,
    current_user: CurrentUser = Depends(forbid_embed_user),
) -> None:
    from shared.semantic.model_validator import revalidate_model

    async for db in get_tenant_db(current_user.tenant_id):
        await ensure_model_in_project(
            db, project_id=project_id, model_id=model_id,
        )
        await acquire_model_definition_lock(db, model_id)  # Bug-7982 cross-family lock (read-modify-write: entity read UNDER lock)
        j = await db.get(Join, join_id)
        if j is None or j.model_id != model_id:
            raise HTTPException(status_code=404, detail="Join not found")

        # Capture references before delete for auto-unhide
        left_table = await db.get(ModelTable, j.left_table_id)
        right_table = await db.get(ModelTable, j.right_table_id)
        left_col = await db.get(ModelColumn, j.left_column_id)
        right_col = await db.get(ModelColumn, j.right_column_id)

        await db.delete(j)
        await db.flush()

        if left_table and right_table and left_col and right_col:
            await _auto_unhide_dim_join_key(
                db, model_id, left_table, right_table, left_col, right_col,
            )

        await revalidate_model(model_id, db)
        await db.commit()


_TYPE_GROUPS = {
    "date": {"DATE", "DATETIME"},
    "timestamp": {"TIMESTAMP", "TIMESTAMPTZ", "TIMESTAMP_TZ", "TIMESTAMP_NTZ"},
    "integer": {"INT", "INT64", "INTEGER", "BIGINT", "SMALLINT", "TINYINT"},
    "float": {"FLOAT", "FLOAT64", "DOUBLE", "NUMERIC", "DECIMAL", "REAL", "NUMBER"},
    "string": {"STRING", "VARCHAR", "TEXT", "CHAR", "NVARCHAR", "NCHAR", "BPCHAR"},
}

_CAST_SUGGESTIONS: dict[tuple[str, str], tuple[str, str]] = {
    ("timestamp", "date"): ("DATE", 'CAST("{col}" AS DATE)'),
    ("date", "timestamp"): ("TIMESTAMP", 'CAST("{col}" AS TIMESTAMP)'),
    ("integer", "string"): ("STRING", 'CAST("{col}" AS STRING)'),
    ("string", "integer"): ("INTEGER", 'CAST("{col}" AS INTEGER)'),
    ("float", "integer"): ("INTEGER", 'CAST("{col}" AS INTEGER)'),
    ("integer", "float"): ("FLOAT", 'CAST("{col}" AS FLOAT)'),
    ("float", "string"): ("STRING", 'CAST("{col}" AS STRING)'),
    ("string", "float"): ("FLOAT", 'CAST("{col}" AS FLOAT)'),
}


def _type_group(data_type: str) -> str | None:
    upper = data_type.upper()
    for group, members in _TYPE_GROUPS.items():
        if upper in members:
            return group
    return None


def _check_join_type_mismatch(
    left_col: ModelColumn,
    right_col: ModelColumn,
    left_table: ModelTable,
    right_table: ModelTable,
    left_name: str,
    right_name: str,
) -> list[str]:
    """Return warnings when join columns have incompatible data types."""
    warnings: list[str] = []
    lt = (left_col.data_type or "").upper()
    rt = (right_col.data_type or "").upper()
    if not lt or not rt or lt == rt:
        return warnings

    lg = _type_group(lt)
    rg = _type_group(rt)
    if lg and rg and lg != rg:
        suggestion = _CAST_SUGGESTIONS.get((lg, rg))
        if suggestion:
            target_type, expr_template = suggestion
            expr = expr_template.replace("{col}", left_name)
            warnings.append(
                f"Column type mismatch: {left_name} ({lt}) vs {right_name} ({rt}). "
                f"Equality joins between {lg} and {rg} columns may produce "
                f"unexpected results or zero rows. "
                f"Create a user-defined attribute with expression "
                f'{expr} on table {left_table.physical_name}, '
                f"then use that attribute as the join column instead."
            )
        else:
            warnings.append(
                f"Column type mismatch: {left_name} ({lt}) vs {right_name} ({rt}). "
                f"Equality joins between {lg} and {rg} columns may produce "
                f"unexpected results or zero rows. "
                f"Consider creating a user-defined attribute with a "
                f"CAST expression to align the types before joining."
            )
    elif lt != rt:
        warnings.append(
            f"Column type mismatch: {left_name} ({lt}) vs {right_name} ({rt}). "
            f"Equality joins between different data types may produce "
            f"unexpected results or zero rows. "
            f"Consider creating a user-defined attribute with a "
            f"CAST expression to align the types before joining."
        )

    return warnings
