"""
Shared fixtures for gateway tests.

sys.path configured via [tool.pytest.ini_options] pythonpath in pyproject.toml:
  - "."  → tessallite/services/gateway/   (enables "from src.xxx import")
  - "../../" → tessallite/               (NOT used by gateway tests — no shared DB models needed)
"""
import pytest
