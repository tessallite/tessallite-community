"""Bug-6667: UserUpdate schema rejects explicit null for NOT NULL DB columns."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from shared.schemas.pydantic_models import UserUpdate

pytestmark = pytest.mark.unit


class TestUserUpdateNullRejection:
    """Verify explicit null for NOT NULL columns produces a 422-equivalent."""

    def test_explicit_null_role_rejected(self):
        with pytest.raises(ValidationError, match="role cannot be null"):
            UserUpdate.model_validate({"role": None})

    def test_explicit_null_is_active_rejected(self):
        with pytest.raises(ValidationError, match="is_active cannot be null"):
            UserUpdate.model_validate({"is_active": None})

    def test_explicit_null_username_rejected(self):
        with pytest.raises(ValidationError, match="username cannot be null"):
            UserUpdate.model_validate({"username": None})

    def test_explicit_null_email_rejected(self):
        with pytest.raises(ValidationError, match="email cannot be null"):
            UserUpdate.model_validate({"email": None})

    def test_omitted_fields_are_fine(self):
        """Omitting a field (not sending it at all) must not trigger rejection."""
        obj = UserUpdate.model_validate({})
        assert obj.role is None
        assert obj.is_active is None
        assert obj.username is None
        assert obj.email is None

    def test_valid_partial_update(self):
        """Sending real values for a subset of fields must succeed."""
        obj = UserUpdate.model_validate({"role": "member", "is_active": True})
        assert obj.role == "member"
        assert obj.is_active is True
