"""Bug-8250 (finding 4) — deploy/revert staling must use the SHARED gate.

The Codex cross-family gate found that ``_stale_incompatible_artifacts`` did not
call the comparator both runtime matchers use; it re-wrote the rule inline. The
two copies then diverged: the SQL one refused a NULL ``built_for_epoch`` while
the Python one coerced it to ``0`` and let such an artifact SERVE. A design that
calls itself a single source of truth while shipping two rules is the defect,
not the divergence.

What is proven here:
  * both staling statements are built from ``artifact_incompatible_sql``, so a
    future edit cannot quietly reintroduce a private, weaker copy; and
  * the predicate keeps its explicit ``IS NULL`` arms, without which SQL's
    three-valued logic makes ``col != :value`` evaluate to NULL for a NULL
    column and the statement silently SKIPS exactly the rows the matcher
    refuses -- leaving a permanently unservable artifact never marked for
    rebuild.

The predicate's SEMANTICS (that it selects exactly the rows the Python gate
refuses, over the whole truth table) are proven against a real SQL engine in
tessallite/shared/tests/test_artifact_version_gate.py; this file proves the
deploy path is wired to it.
"""
from __future__ import annotations

import uuid

import pytest

from shared.artifact_version_gate import artifact_incompatible_sql
from src.api.versions import _stale_incompatible_artifacts

_AGGREGATES = "aggregate_definitions"
_POCKETS = "pocket_definitions"
# F-013-03: Named Query artifacts are the third materialised family staled on
# deploy/revert through the SAME shared incompatibility gate.
_NAMED_QUERY_ARTIFACTS = "named_query_artifacts"


class _CapturingSession:
    def __init__(self):
        self.statements = []

    async def execute(self, stmt):
        self.statements.append(stmt)
        return None


def _sql(clause) -> str:
    return str(clause.compile(compile_kwargs={"literal_binds": True}))


def _statement_for(db, table_name):
    """Select a captured UPDATE by TABLE NAME.

    Not by ORM class identity: this suite can hold two import instances of
    ``shared.db.models`` (package path vs service path), so ``is`` against
    ``Model.__table__`` silently matches nothing and the lookup raises
    StopIteration instead of failing the assertion it was meant to make.
    """
    for stmt in db.statements:
        if stmt.table.name == table_name:
            return stmt
    raise AssertionError(f"no staling UPDATE was issued against {table_name}")


@pytest.mark.asyncio
async def test_all_materialised_families_are_staled():
    db = _CapturingSession()
    version_id = uuid.uuid4()
    await _stale_incompatible_artifacts(
        db, model_id=uuid.uuid4(), new_version_id=version_id, new_epoch=7
    )
    assert len(db.statements) == 3, (
        "a deploy must stale aggregates, pockets AND Named Query artifacts; any "
        "family built for the previous definition serves stale rows the same way"
    )
    assert {stmt.table.name for stmt in db.statements} == {
        _AGGREGATES, _POCKETS, _NAMED_QUERY_ARTIFACTS,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "table_name", [_AGGREGATES, _POCKETS, _NAMED_QUERY_ARTIFACTS]
)
async def test_staling_predicate_is_the_shared_one(table_name):
    db = _CapturingSession()
    version_id = uuid.uuid4()
    await _stale_incompatible_artifacts(
        db, model_id=uuid.uuid4(), new_version_id=version_id, new_epoch=7
    )
    stmt = _statement_for(db, table_name)
    expected = _sql(
        artifact_incompatible_sql(
            stmt.table.c.built_for_version_id,
            stmt.table.c.built_for_epoch,
            version_id,
            7,
        )
    )
    assert expected in _sql(stmt.whereclause), (
        f"{table_name} staling no longer uses the shared incompatibility "
        f"predicate; a private copy is how the two encodings diverged"
    )


@pytest.mark.asyncio
async def test_staling_predicate_keeps_the_null_arms():
    """Three-valued logic: without IS NULL, NULL-bound artifacts are skipped."""
    db = _CapturingSession()
    await _stale_incompatible_artifacts(
        db, model_id=uuid.uuid4(), new_version_id=uuid.uuid4(), new_epoch=7
    )
    for stmt in db.statements:
        sql = _sql(stmt.whereclause)
        assert "built_for_version_id IS NULL" in sql
        assert "built_for_epoch IS NULL" in sql


@pytest.mark.asyncio
async def test_only_servable_artifacts_are_touched():
    """Retired/failed/already-stale rows must not be rewritten by every deploy."""
    db = _CapturingSession()
    await _stale_incompatible_artifacts(
        db, model_id=uuid.uuid4(), new_version_id=uuid.uuid4(), new_epoch=7
    )
    agg = _statement_for(db, _AGGREGATES)
    pocket = _statement_for(db, _POCKETS)
    agg_sql = _sql(agg.whereclause)
    assert "status = 'active'" in agg_sql
    assert "is_stale IS false" in agg_sql or "is_stale = false" in agg_sql

    pocket_sql = _sql(pocket.whereclause)
    assert "status = 'fresh'" in pocket_sql
    assert "retired_at IS NULL" in pocket_sql
