"""Bug-6291: EVERY slug override value is validated at preflight, including one
that targets a model or persona the bundle does not contain.

Regression guard. The PERSONA half of ``_validate_slug_overrides`` was silently
deleted by a shared-tree blob reconstruction in commit 8d2670ca (the
info-backlog-triage lane rebuilding this file to exclude another lane's hunks),
and the 38-test project-import suite stayed green because nothing covered this
function's persona input at all. The used-override case is still caught later by
``_insert_personas``; what was lost is exactly what Bug-6291 was for -- an
UNUSED or typo'd override with a BI-unsafe value failing loudly at preflight
instead of being silently ignored.
"""
from __future__ import annotations

import pytest

from shared.model_snapshot.project_rehydrator import (
    ProjectImportError,
    _validate_slug_overrides,
)


@pytest.mark.parametrize("kind", ["model", "persona"])
def test_an_unused_bi_unsafe_slug_override_is_refused_at_preflight(kind):
    overrides = {"absent-from-bundle": "bad slug!"}
    args = (overrides, None) if kind == "model" else (None, overrides)
    with pytest.raises(ProjectImportError) as exc:
        _validate_slug_overrides(*args)
    assert kind in str(exc.value).lower(), (
        f"the {kind} override was not the one reported: {exc.value}"
    )


@pytest.mark.parametrize("kind", ["model", "persona"])
def test_a_valid_override_passes(kind):
    overrides = {"old-slug": "new_slug"}
    args = (overrides, None) if kind == "model" else (None, overrides)
    _validate_slug_overrides(*args)


def test_both_override_kinds_are_checked_in_one_call():
    """A bad persona override must not be masked by a clean model override."""
    with pytest.raises(ProjectImportError) as exc:
        _validate_slug_overrides({"m": "good_slug"}, {"p": "bad slug!"})
    assert "persona" in str(exc.value).lower()


def test_no_overrides_is_a_no_op():
    _validate_slug_overrides(None, None)
    _validate_slug_overrides({}, {})
