from __future__ import annotations

import types
import uuid
from unittest.mock import AsyncMock

import pytest

from src.auth.token_version import bump_local_user_token_version

pytestmark = pytest.mark.unit


class _Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


@pytest.mark.asyncio
async def test_bump_local_user_token_version_uses_atomic_update_without_commit():
    user = types.SimpleNamespace(id=uuid.uuid4(), token_version=3)
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_Result(4))
    db.commit = AsyncMock()

    new_version = await bump_local_user_token_version(db, user)

    assert new_version == 4
    assert user.token_version == 4
    db.commit.assert_not_called()
    stmt = db.execute.call_args.args[0]
    rendered = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "UPDATE local_users" in rendered
    assert "token_version=(local_users.token_version + 1)" in rendered
    assert "RETURNING local_users.token_version" in rendered
