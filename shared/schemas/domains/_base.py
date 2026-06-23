"""Shared base class for all domain schemas."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class OrmBase(BaseModel):
    model_config = ConfigDict(from_attributes=True)
