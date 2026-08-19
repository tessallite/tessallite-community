"""F-013-12 (Bug-8998) — version-mutation audits use a CACHED display_name.

Save already cached ``model.display_name`` right after the access check because a
lazy load on a possibly-expired ORM instance raises MissingGreenlet and 500s
what looks like a failed operation. Revert / deploy / undeploy still read
``model.display_name`` at their (post-lock, post-rollback-window) audit and
webhook call sites. This mechanically pins the fix: no audit ``target_name`` or
webhook ``model_name`` in versions.py may lazy-load ``model.display_name`` — they
must use the pre-lock cached scalar ``model_display_name``.
"""
from __future__ import annotations

import pathlib

SRC = (
    pathlib.Path(__file__).resolve().parents[1]
    / "src" / "api" / "versions.py"
)


def test_no_audit_or_webhook_lazy_loads_display_name():
    text = SRC.read_text(encoding="utf-8")
    offenders = [
        "target_name=model.display_name",
        '"model_name": model.display_name',
    ]
    found = [needle for needle in offenders if needle in text]
    assert not found, (
        "these versions.py audit/webhook sites lazy-load model.display_name on a "
        f"possibly-expired ORM instance (F-013-12 regression): {found} — capture "
        "model_display_name after the access check and use it instead."
    )


def test_each_mutation_route_caches_display_name():
    text = SRC.read_text(encoding="utf-8")
    # Save + revert + deploy + undeploy each capture the scalar once.
    assert text.count("model_display_name = model.display_name") >= 4
