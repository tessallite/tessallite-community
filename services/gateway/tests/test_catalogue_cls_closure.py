"""F-008-04: gateway catalogue transitive CLS closure.

The catalogue must hide an object that reaches a restricted column TRANSITIVELY
(calculated measure, UDA-backed object, variant of a restricted base), matching
the query-router serving gate — not only objects that bind the column directly.
These tests assert the DECISION (hidden vs advertised) for each transitive
channel using the shared closure, driven by snapshot-shaped dicts.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.catalogue_cls import build_closure_context, object_hidden_by_cls


RESTRICTED_COL = "col-salary"
CLEAN_COL = "col-region"


def _snapshot():
    return {
        "columns": [
            {"id": RESTRICTED_COL, "column_name": "salary"},
            {"id": CLEAN_COL, "column_name": "region"},
        ],
        "uda_column_refs": [],
        "tables": [{"physical_name": "employees"}],
    }


def _ctx(measures, restricted):
    return build_closure_context(
        measures=measures, snapshot=_snapshot(), restricted_column_ids=restricted,
    )


def test_direct_restricted_measure_is_hidden():
    m = {"id": "m1", "name": "Salary", "source_column_id": RESTRICTED_COL}
    ctx = _ctx([m], {RESTRICTED_COL})
    assert object_hidden_by_cls(m, {RESTRICTED_COL}, ctx) is True


def test_clean_measure_is_advertised():
    m = {"id": "m2", "name": "Headcount", "source_column_id": CLEAN_COL}
    ctx = _ctx([m], {RESTRICTED_COL})
    assert object_hidden_by_cls(m, {RESTRICTED_COL}, ctx) is False


def test_calculated_measure_over_restricted_measure_is_hidden():
    """F-008-04: a calc measure referencing a restricted measure by name must be
    hidden even though it binds NO source column directly — the exact gap where
    a 'salary per headcount' measure was advertised in JDBC but blocked at run."""
    base = {"id": "m1", "name": "Salary", "source_column_id": RESTRICTED_COL}
    calc = {
        "id": "m3", "name": "SalaryPerHead",
        "source_column_id": None,
        "measure_type": "calculated",
        "expression": 'measure("Salary") / measure("Headcount")',
    }
    head = {"id": "m2", "name": "Headcount", "source_column_id": CLEAN_COL}
    ctx = _ctx([base, calc, head], {RESTRICTED_COL})
    assert object_hidden_by_cls(calc, {RESTRICTED_COL}, ctx) is True
    # The clean base stays advertised.
    assert object_hidden_by_cls(head, {RESTRICTED_COL}, ctx) is False


def test_variant_of_restricted_base_is_hidden():
    base = {"id": "m1", "name": "Salary", "source_column_id": RESTRICTED_COL}
    variant = {
        "id": "m4", "name": "SalaryYTD",
        "source_column_id": None,
        "variant_of_measure_id": "m1",
    }
    ctx = _ctx([base, variant], {RESTRICTED_COL})
    assert object_hidden_by_cls(variant, {RESTRICTED_COL}, ctx) is True


def test_uda_backed_object_referencing_restricted_column_is_hidden():
    snapshot = _snapshot()
    snapshot["uda_column_refs"] = [
        {"attribute_id": "uda1", "column_id": RESTRICTED_COL},
    ]
    dim = {
        "id": "d1", "name": "SalaryBand",
        "source_column_id": None,
        "user_defined_attribute_id": "uda1",
    }
    ctx = build_closure_context(
        measures=[], snapshot=snapshot, restricted_column_ids={RESTRICTED_COL},
    )
    assert object_hidden_by_cls(dim, {RESTRICTED_COL}, ctx) is True


def test_no_restrictions_advertises_everything():
    m = {"id": "m1", "name": "Salary", "source_column_id": RESTRICTED_COL}
    ctx = _ctx([m], set())
    assert object_hidden_by_cls(m, set(), ctx) is False
