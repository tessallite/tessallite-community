"""Bug-7623 (per-version fidelity — "correct restore from now on") — the
serialiser must now INCLUDE each model version's own ``snapshot_json`` in the
export, so a NEW-format (PROJECT_BUNDLE_VERSION 2+) bundle can restore version N
to version N's real shape.

This reverses the earlier trim, which excluded ``snapshot_json`` from exported
versions on the grounds that the importer discarded it anyway. The importer now
rebinds the portable fields and persists the real per-version snapshot, so the
payload IS usable and correctness for a restore feature outweighs the size cost.

This is a source-level contract test (inspecting the ``_row_to_dict`` call site
inside ``snapshot_model``'s ``include_versions`` branch), since running the
async serialiser against a live DB is db-integration tier.
"""
from __future__ import annotations

import ast
import inspect


def _include_versions_branch_row_to_dict_calls() -> list[ast.Call]:
    """Return every ``_row_to_dict`` Call node that appears inside an
    ``if include_versions:`` branch of ``snapshot_model``."""
    src = inspect.getsource(__import__(
        "shared.model_snapshot.serialiser", fromlist=["snapshot_model"]
    ).snapshot_model)
    tree = ast.parse(src)
    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.If)
            and isinstance(node.test, ast.Name)
            and node.test.id == "include_versions"
        ):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call):
                    func = sub.func
                    name = (
                        func.id if isinstance(func, ast.Name)
                        else func.attr if isinstance(func, ast.Attribute)
                        else None
                    )
                    if name == "_row_to_dict":
                        calls.append(sub)
    return calls


def test_serialiser_includes_snapshot_json_in_model_versions_bug7623():
    """Inside the ``include_versions`` branch, the ``_row_to_dict`` call that
    serialises each version must NOT exclude ``snapshot_json`` — the real
    per-version shape has to travel so a restore can reproduce it."""
    calls = _include_versions_branch_row_to_dict_calls()
    assert calls, (
        "expected a _row_to_dict call for model versions inside the "
        "include_versions branch of snapshot_model"
    )
    for call in calls:
        for kw in call.keywords:
            if kw.arg == "exclude" and isinstance(kw.value, ast.Tuple):
                excluded = {
                    elt.value
                    for elt in kw.value.elts
                    if isinstance(elt, ast.Constant)
                }
                assert "snapshot_json" not in excluded, (
                    "snapshot_model must NOT exclude 'snapshot_json' from "
                    "exported model versions (Bug-7623): a NEW-format bundle "
                    "carries each version's own snapshot so a restore can "
                    "reproduce version N's real shape."
                )
