"""Pytest config for shared.config tests.

These tests live deep under ``shared/`` but import via the ``shared.x``
namespace, so we need the ``tessallite/`` directory on sys.path. The
shared package's ``__init__.py`` files are intentionally empty; the
parent ``tessallite/__init__.py`` is unrelated to test collection here
and is skipped by ensuring this conftest provides the import root
explicitly.
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
# tests/ -> config/ -> shared/ -> tessallite/
_TESSALLITE_ROOT = os.path.abspath(os.path.join(_HERE, "..", "..", ".."))

if _TESSALLITE_ROOT not in sys.path:
    sys.path.insert(0, _TESSALLITE_ROOT)
