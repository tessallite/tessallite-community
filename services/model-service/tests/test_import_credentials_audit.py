"""F-020-06 — project-import audit correctly records credentials_included.

The predicate used ``getattr(body, "include_credentials", False)``, but the
IMPORT request has no such field, so a credentialed import was audited
severity=warn with credentials_included=false — under-ranking the most sensitive
ingress event the product has. It must derive credential inclusion from the
BUNDLE (which declares it) AND the presence of a passphrase.
"""
from __future__ import annotations

import pytest

from src.api.project_import_export import _import_credentials_included


def test_credentialed_import_is_recognised():
    assert _import_credentials_included(
        "pass", {"credentials_included": True}
    ) is True


def test_no_passphrase_is_not_credentialed():
    assert _import_credentials_included(
        None, {"credentials_included": True}
    ) is False


def test_bundle_without_credentials_is_not_credentialed():
    assert _import_credentials_included(
        "pass", {"credentials_included": False}
    ) is False
    assert _import_credentials_included("pass", {}) is False


def test_missing_bundle_is_safe():
    assert _import_credentials_included("pass", None) is False
