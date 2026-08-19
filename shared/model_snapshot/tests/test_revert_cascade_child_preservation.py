"""Producer-derived guard: a model revert must not cascade-wipe the CASCADE
children of NamedSet / KPI (Bug-7982, Codex re-gate residual 3).

The fix is IN-PLACE upsert: NamedSet and KPI are never truncate-deleted, so their
``ondelete=CASCADE`` children (version history, usage telemetry, KPI snapshot
history, the KPILatest ``$KPIs`` cache) are never cascaded away — which also
closes the lost-update race with concurrent lock-less child writers (evaluate-
batch, scheduler sweep, usage report). This test locks that invariant at the
producer: ``_truncate_model_children`` must NOT delete NamedSet or KPI. If a
future edit reintroduces a truncate-delete of either parent, every CASCADE child
is at risk again and this fails.
"""
from __future__ import annotations

import ast
import inspect
import textwrap

import pytest

from shared.db.models import TenantBase
from shared.model_snapshot import rehydrator

pytestmark = pytest.mark.unit

# Parent ORM classes that MUST be upserted in place (never truncate-deleted).
_IN_PLACE_PARENTS = {"NamedSet", "KPI"}


def _cascade_children_of(parent_tables: set[str]) -> dict[str, str]:
    found: dict[str, str] = {}
    for table in TenantBase.metadata.tables.values():
        for fk in table.foreign_keys:
            if fk.column.table.name in parent_tables and (fk.ondelete or "").upper() == "CASCADE":
                found[table.name] = fk.column.table.name
    return found


def _delete_targets_in(func) -> set[str]:
    """ORM class names passed to ``delete(<Model>)`` inside a function."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    targets: set[str] = set()
    for call in ast.walk(tree):
        if isinstance(call, ast.Call) and getattr(call.func, "id", None) == "delete":
            if call.args and isinstance(call.args[0], ast.Name):
                targets.add(call.args[0].id)
    return targets


def test_truncate_does_not_delete_named_set_or_kpi_parents():
    """The in-place invariant: a revert must NOT truncate-delete NamedSet or KPI
    (that would cascade-wipe their children). They are upserted in place."""
    deleted = _delete_targets_in(rehydrator._truncate_model_children)
    offending = sorted(_IN_PLACE_PARENTS & deleted)
    assert not offending, (
        f"_truncate_model_children deletes {offending}, which would cascade-wipe "
        "their CASCADE children on a revert. These parents MUST be upserted in "
        "place (Bug-7982 residual 3), never truncate-deleted."
    )


def test_named_set_and_kpi_have_cascade_children_worth_protecting():
    """Sanity: the parents we protect actually have CASCADE children (guards the
    invariant from silently becoming vacuous if the FKs change)."""
    children = _cascade_children_of({"named_sets", "kpis"})
    assert "kpi_versions" in children and "kpi_usage" in children
    assert "kpi_snapshots" in children and "kpi_latest" in children
    assert "named_set_versions" in children and "named_set_usage" in children
