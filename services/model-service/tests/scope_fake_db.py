"""An in-memory session that really EVALUATES the body-FK ownership predicate.

Why this exists
---------------
The body-FK guards in ``src/api/_scope.py`` prove ownership INSIDE a single
SELECT. The service's ordinary test double (``conftest.make_mock_db``) cannot
evaluate a WHERE clause: it answers every statement from a canned list, so a
guard that had been deleted outright would still look green, and — the other
failure direction, the one that shipped as Bug-8864 — a guard that denies
EVERYTHING would also look green. Both are exactly what these tests must catch,
so the double has to hold real rows and decide from the values the route
actually bound into the statement.

What it does
------------
``execute`` recognises an ownership SELECT by the ``JOIN models ON`` the scope
helper always emits, then:

* REFUSES the statement unless it carries BOTH a ``model_id`` and a
  ``project_id`` bind (raises ``ScopeGuardBypass``). Deleting either predicate
  from ``scoped_select`` therefore turns these tests red instead of silently
  widening the guard.
* resolves each requested id against an independent in-memory ownership map
  (column -> table -> model -> project) and returns only the rows the bound
  project+model really own.

Every other statement is answered from the registered rows with a small amount
of best-effort filtering, the same way the rest of this suite's doubles behave.
It is a test double, not a database: it is trustworthy for the ownership
question it was built to answer and for nothing else.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any


class ScopeGuardBypass(AssertionError):
    """An ownership SELECT reached the store without both scope predicates.

    Raised rather than answered, because an ownership query missing the model
    or the project bind is not a query with a different answer — it is the
    absence of the guard.
    """


_FROM = re.compile(r"\bFROM\s+([a-z_]+)")

_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class ScopedFakeDB:
    """Async-session stand-in with real project -> model -> row ownership."""

    def __init__(self, *, project_id: uuid.UUID, model_id: uuid.UUID):
        self.project_id = project_id
        self.model_id = model_id
        # model_id -> project_id
        self.models: dict[uuid.UUID, uuid.UUID] = {}
        # table_id -> model_id
        self.table_owner: dict[uuid.UUID, uuid.UUID] = {}
        # row id -> owning model id, for entities that carry model_id directly
        self.row_owner: dict[uuid.UUID, uuid.UUID] = {}
        # table name -> list of rows
        self.rows: dict[str, list[Any]] = {}
        self.by_id: dict[uuid.UUID, Any] = {}
        self.added: list[Any] = []
        self.committed = False
        self.info: dict[str, Any] = {}
        self.register_model(model_id, project_id)

    # -- registration -------------------------------------------------------

    def register_model(self, model_id: uuid.UUID, project_id: uuid.UUID):
        self.models[model_id] = project_id
        row = SimpleNamespace(id=model_id, project_id=project_id)
        self.rows.setdefault("models", []).append(row)
        self.by_id[model_id] = row
        return row

    def add_table(
        self,
        *,
        model_id: uuid.UUID,
        table_id: uuid.UUID | None = None,
        calendar_table_id: uuid.UUID | None = None,
        table_type: str = "fact",
        physical_name: str = "public.t",
        alias: str = "t",
        display_name: str | None = None,
    ):
        table_id = table_id or uuid.uuid4()
        row = SimpleNamespace(
            id=table_id,
            model_id=model_id,
            calendar_table_id=calendar_table_id,
            table_type=table_type,
            physical_name=physical_name,
            alias=alias,
            display_name=display_name or alias,
            source_id=uuid.uuid4(),
        )
        self.table_owner[table_id] = model_id
        self.rows.setdefault("model_tables", []).append(row)
        self.by_id[table_id] = row
        return row

    def add_column(
        self,
        *,
        table_id: uuid.UUID,
        column_name: str = "amount",
        column_id: uuid.UUID | None = None,
        data_type: str = "numeric",
    ):
        column_id = column_id or uuid.uuid4()
        row = SimpleNamespace(
            id=column_id,
            model_table_id=table_id,
            column_name=column_name,
            data_type=data_type,
            is_hidden=False,
            hidden_reason=None,
            is_nullable=True,
            display_name=column_name,
            cardinality_estimate=None,
            high_cardinality=None,
        )
        self.rows.setdefault("model_columns", []).append(row)
        self.by_id[column_id] = row
        return row

    def add_measure(self, *, model_id: uuid.UUID, **attrs):
        measure_id = attrs.pop("id", None) or uuid.uuid4()
        row = SimpleNamespace(
            id=measure_id,
            model_id=model_id,
            name=attrs.pop("name", f"m_{measure_id.hex[:6]}"),
            measure_type=attrs.pop("measure_type", "standard"),
            variant_kind=attrs.pop("variant_kind", None),
            calendar_model_table_id=attrs.pop("calendar_model_table_id", None),
            source_column_id=attrs.pop("source_column_id", None),
            display_name=attrs.pop("display_name", None),
            description=attrs.pop("description", None),
            display_folder=attrs.pop("display_folder", None),
            user_defined_attribute_id=attrs.pop(
                "user_defined_attribute_id", None,
            ),
            expression=attrs.pop("expression", None),
            calc_agg_mode=attrs.pop("calc_agg_mode", None),
            data_type=attrs.pop("data_type", "numeric"),
            default_agg=attrs.pop("default_agg", "sum"),
            format=attrs.pop("format", None),
            variant_of_measure_id=attrs.pop("variant_of_measure_id", None),
            variant_n=attrs.pop("variant_n", None),
            is_additive=attrs.pop("is_additive", True),
            semi_additive_behavior=attrs.pop(
                "semi_additive_behavior", None,
            ),
            semi_additive_account_column_id=attrs.pop(
                "semi_additive_account_column_id", None,
            ),
            hierarchy_id=attrs.pop("hierarchy_id", None),
            resolved_calendar_id=attrs.pop("resolved_calendar_id", None),
            resolved_date_col_id=attrs.pop("resolved_date_col_id", None),
            date_dimension_column_id=attrs.pop(
                "date_dimension_column_id", None,
            ),
            cross_model_source_model_id=attrs.pop(
                "cross_model_source_model_id", None,
            ),
            cross_model_source_measure_id=attrs.pop(
                "cross_model_source_measure_id", None,
            ),
            is_invalid=attrs.pop("is_invalid", False),
            invalid_reason=attrs.pop("invalid_reason", None),
            created_at=attrs.pop("created_at", _NOW),
            updated_at=attrs.pop("updated_at", _NOW),
            **attrs,
        )
        self.row_owner[measure_id] = model_id
        self.rows.setdefault("measures", []).append(row)
        self.by_id[measure_id] = row
        return row

    def add_dimension(self, *, model_id: uuid.UUID, **attrs):
        dim_id = attrs.pop("id", None) or uuid.uuid4()
        row = SimpleNamespace(
            id=dim_id,
            model_id=model_id,
            name=attrs.pop("name", f"d_{dim_id.hex[:6]}"),
            display_name=attrs.pop("display_name", None),
            source_column_id=attrs.pop("source_column_id", None),
            display_column_id=attrs.pop("display_column_id", None),
            user_defined_attribute_id=attrs.pop(
                "user_defined_attribute_id", None,
            ),
            calc_expression=attrs.pop("calc_expression", None),
            is_time_dim=attrs.pop("is_time_dim", False),
            time_grain=attrs.pop("time_grain", None),
            created_at=attrs.pop("created_at", _NOW),
            updated_at=attrs.pop("updated_at", _NOW),
            **attrs,
        )
        self.row_owner[dim_id] = model_id
        self.rows.setdefault("dimensions", []).append(row)
        self.by_id[dim_id] = row
        return row

    # -- ownership ----------------------------------------------------------

    def _owning_model(self, table: str, row) -> uuid.UUID | None:
        if table == "model_columns":
            return self.table_owner.get(row.model_table_id)
        if table == "model_tables":
            return row.model_id
        return self.row_owner.get(row.id, getattr(row, "model_id", None))

    def _ownership_answer(self, table: str, params: dict) -> list:
        model_bind = [v for k, v in params.items() if k.startswith("model_id")]
        project_bind = [v for k, v in params.items() if k.startswith("project_id")]
        if not model_bind or not project_bind:
            raise ScopeGuardBypass(
                f"an ownership SELECT over {table!r} carried "
                f"model_id={model_bind!r} project_id={project_bind!r}; both "
                "predicates must be in the statement or the guard proves "
                "nothing"
            )
        want_model, want_project = model_bind[0], project_bind[0]
        # ``IN (...)`` compiles to a single expanding bind whose value is a
        # list, while ``== :id_1`` binds a scalar; flatten both shapes.
        wanted: set = set()
        for key, value in params.items():
            if not key.startswith("id_"):
                continue
            if isinstance(value, (list, tuple, set)):
                wanted.update(value)
            else:
                wanted.add(value)
        out = []
        for row in self.rows.get(table, []):
            if row.id not in wanted:
                continue
            owner = self._owning_model(table, row)
            if owner != want_model:
                continue
            if self.models.get(owner) != want_project:
                continue
            out.append(row)
        return out

    # -- AsyncSession surface ----------------------------------------------

    async def execute(self, stmt, *args, **kwargs):
        text = str(stmt)
        try:
            params = dict(stmt.compile().params)
        except Exception:  # pragma: no cover - non-select constructs
            params = {}
        match = _FROM.search(text)
        table = match.group(1) if match else ""
        if "JOIN models ON" in text:
            return _Result(self._ownership_answer(table, params))
        if table == "model_columns":
            rows = self.rows.get("model_columns", [])
            table_ids = {
                v for k, v in params.items() if k.startswith("model_table_id")
            }
            names = {v for k, v in params.items() if k.startswith("column_name")}
            if table_ids:
                rows = [r for r in rows if r.model_table_id in table_ids]
            if names:
                rows = [r for r in rows if r.column_name in names]
            return _Result(rows)
        return _Result(self.rows.get(table, []))

    async def get(self, _entity, pk):
        return self.by_id.get(pk)

    def add(self, obj):
        self.added.append(obj)
        row_id = getattr(obj, "id", None) or uuid.uuid4()
        obj.id = row_id
        self.by_id[row_id] = obj

    async def flush(self):
        return None

    async def commit(self):
        self.committed = True

    async def rollback(self):
        return None

    async def delete(self, obj):
        return None

    async def refresh(self, obj, *_a, **_kw):
        # Stand in for the flush/refresh that assigns the ORM's Python-side
        # default (id) and the database's server defaults (timestamps), so the
        # route's response model validates.
        obj.id = getattr(obj, "id", None) or uuid.uuid4()
        for field, default in (
            ("created_at", _NOW), ("updated_at", _NOW),
            ("is_hidden", False), ("is_invalid", False),
            ("invalid_reason", None),
        ):
            if getattr(obj, field, None) is None:
                setattr(obj, field, default)

    def begin_nested(self):
        return _NoopSavepoint()


class _NoopSavepoint:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _ScalarResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None

    def one_or_none(self):
        if len(self._rows) > 1:
            raise AssertionError("more than one row for one id")
        return self._rows[0] if self._rows else None


class _Result:
    def __init__(self, rows):
        self._rows = list(rows)

    def scalars(self):
        return _ScalarResult(self._rows)

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None

    def scalar(self):
        return self._rows[0] if self._rows else None

    def scalar_one_or_none(self):
        if len(self._rows) > 1:
            raise AssertionError("more than one row")
        return self._rows[0] if self._rows else None
