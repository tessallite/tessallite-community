"""License manager interface + open stub + loader seam.

This module is PUBLIC (ships in the shell). It defines:
  - the ``LicenseManager`` interface the services code against,
  - ``Decision`` (the answer to a create-permission question),
  - ``UnactivatedManager`` — the open stub used when no closed manager / license is
    present (source-only dev mode): the product runs, query paths work, but
    protected control-plane creates are refused with a clear "not activated" reason
    rather than crashing,
  - ``load_manager()`` — the seam that loads the closed manager if available, else
    returns the stub.

The real policy engine (``SignedLicenseManager``) is CLOSED (``licensing.closed``,
export-excluded). The open shell never decides entitlement itself — it asks the
manager. See spec §4.1, §6.1.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Any

# Control-plane resources that carry Community caps.
RESOURCE_TENANT = "tenant"
RESOURCE_PROJECT = "project"
RESOURCE_MODEL = "model"
RESOURCE_USER = "user"


@dataclass(frozen=True)
class Decision:
    """Answer to a create-permission question."""

    allowed: bool
    resource: str
    current: int
    limit: int | None  # None == unlimited (enterprise)
    reason: str

    @property
    def http_status(self) -> int:
        return 200 if self.allowed else 403


class LicenseManager(abc.ABC):
    """Policy brain. Services ask it; they never embed the policy themselves."""

    @abc.abstractmethod
    def status(self) -> dict[str, Any]:
        """Edition + activation summary for ``/edition`` (no secrets)."""

    @abc.abstractmethod
    def entitlements(self) -> dict[str, Any]:
        ...

    @abc.abstractmethod
    def classify_tenant(self, tenant_id: str) -> str:
        """Return ``"demo"`` or ``"own"`` (demo does not count against caps)."""

    @abc.abstractmethod
    def can_create(self, resource: str, current_count: int) -> Decision:
        """Whether one more of ``resource`` may be created given ``current_count``."""


class UnactivatedManager(LicenseManager):
    """Open stub: no closed manager / no valid license. Degrades, never crashes."""

    REASON = (
        "License not activated. The closed license manager or a valid signed "
        "license is not present; control-plane creation is disabled until activation."
    )

    def status(self) -> dict[str, Any]:
        return {"edition": "unactivated", "activated": False}

    def entitlements(self) -> dict[str, Any]:
        return {}

    def classify_tenant(self, tenant_id: str) -> str:
        return "own"

    def can_create(self, resource: str, current_count: int) -> Decision:
        return Decision(
            allowed=False,
            resource=resource,
            current=current_count,
            limit=0,
            reason=self.REASON,
        )


def load_manager(**kwargs: Any) -> LicenseManager:
    """Return the closed manager if available + configured, else the open stub.

    The closed implementation lives in ``licensing.closed`` (not in the public
    repo). Its absence (source-only checkout) yields the stub — the product runs
    in an unactivated state instead of crashing.
    """
    try:
        from .closed.manager_impl import build_manager  # type: ignore
    except Exception:
        return UnactivatedManager()
    try:
        manager = build_manager(**kwargs)
    except Exception:
        return UnactivatedManager()
    return manager or UnactivatedManager()
