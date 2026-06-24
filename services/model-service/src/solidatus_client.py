"""Solidatus HTTP client placeholder.

The real Solidatus API contract is tenant-specific and not yet available.
Validation remains a lightweight placeholder for config flows, but write
methods deliberately fail instead of pretending that a push succeeded.

When the real API contract is available, replace this with actual HTTP calls.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SolidatusConnectionStatus:
    # Tri-state: True = verified live, False = verified failed,
    # None = not contacted (simulated). The placeholder client always
    # returns None so callers never mistake a non-networking check for a pass.
    ok: bool | None
    base_url: str
    simulated: bool = False
    workspace_found: bool | None = None
    model_ref_found: bool | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass
class SolidatusUpsertResult:
    created: int = 0
    updated: int = 0
    skipped: int = 0
    object_mappings: dict[str, str] = field(default_factory=dict)


class SolidatusPushNotImplementedError(NotImplementedError):
    """Raised when non-dry-run Solidatus push is attempted without a real client."""


class SolidatusClient:
    """Solidatus client shell used until a tenant-specific API contract is wired."""

    def __init__(self, base_url: str, token: str, timeout_seconds: int = 30):
        self._base_url = base_url
        self._token = token
        self._timeout = timeout_seconds

    async def validate_connection(self) -> SolidatusConnectionStatus:
        # No real HTTP client yet: do not assert a pass we cannot prove.
        # ok=None + simulated=True tells the caller the connector was never
        # contacted, so a wrong URL / expired token cannot read as success.
        return SolidatusConnectionStatus(
            ok=None,
            base_url=self._base_url,
            simulated=True,
            workspace_found=None,
            model_ref_found=None,
            warnings=[
                "Validation simulated — the Solidatus connector is not yet "
                "contacted. Configuration was saved but has not been verified "
                "against a live Solidatus instance.",
            ],
        )

    async def upsert_nodes(
        self, nodes: list, workspace_id: str = ""
    ) -> SolidatusUpsertResult:
        """Upsert nodes into Solidatus.

        In production this would POST to the Solidatus model import endpoint.
        """
        raise SolidatusPushNotImplementedError(
            "Solidatus non-dry-run sync is not implemented for this deployment; "
            "configure a real Solidatus client before running push mode."
        )

    async def upsert_edges(
        self, edges: list, workspace_id: str = ""
    ) -> SolidatusUpsertResult:
        """Upsert edges into Solidatus."""
        raise SolidatusPushNotImplementedError(
            "Solidatus non-dry-run sync is not implemented for this deployment; "
            "configure a real Solidatus client before running push mode."
        )
