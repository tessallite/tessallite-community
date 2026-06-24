"""Collibra HTTP client placeholder.

The real Collibra API contract is tenant-specific (operating model,
asset types, relation types vary by installation). Validation remains a
lightweight placeholder for config flows, but write methods deliberately
fail instead of pretending that a push succeeded.

When the real API contract is available, replace this with actual HTTP calls.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CollibraConnectionStatus:
    # Tri-state: True = verified live, False = verified failed,
    # None = not contacted (simulated). The placeholder client always
    # returns None so callers never mistake a non-networking check for a pass.
    ok: bool | None
    base_url: str
    simulated: bool = False
    community_found: bool | None = None
    domain_found: bool | None = None
    missing_asset_types: list[str] = field(default_factory=list)
    missing_relation_types: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class CollibraUpsertResult:
    created: int = 0
    updated: int = 0
    skipped: int = 0
    object_mappings: dict[str, str] = field(default_factory=dict)


class CollibraPushNotImplementedError(NotImplementedError):
    """Raised when non-dry-run Collibra push is attempted without a real client."""


class CollibraClient:
    """Collibra client shell used until a tenant-specific API contract is wired."""

    def __init__(self, base_url: str, token: str, timeout_seconds: int = 30):
        self._base_url = base_url
        self._token = token
        self._timeout = timeout_seconds

    async def validate_connection(self) -> CollibraConnectionStatus:
        # No real HTTP client yet: do not assert a pass we cannot prove.
        # ok=None + simulated=True tells the caller the connector was never
        # contacted, so a wrong URL / expired token cannot read as success.
        return CollibraConnectionStatus(
            ok=None,
            base_url=self._base_url,
            simulated=True,
            community_found=None,
            domain_found=None,
            missing_asset_types=[],
            missing_relation_types=[],
            warnings=[
                "Validation simulated — the Collibra connector is not yet "
                "contacted. Configuration was saved but has not been verified "
                "against a live Collibra instance.",
            ],
        )

    async def upsert_assets(
        self, assets: list, domain_id: str = ""
    ) -> CollibraUpsertResult:
        """Upsert assets into Collibra."""
        raise CollibraPushNotImplementedError(
            "Collibra non-dry-run sync is not implemented for this deployment; "
            "configure a real Collibra client before running push mode."
        )

    async def upsert_relations(
        self, relations: list
    ) -> CollibraUpsertResult:
        """Upsert relations into Collibra."""
        raise CollibraPushNotImplementedError(
            "Collibra non-dry-run sync is not implemented for this deployment; "
            "configure a real Collibra client before running push mode."
        )

    async def upsert_responsibilities(
        self, responsibilities: list
    ) -> CollibraUpsertResult:
        """Upsert responsibilities into Collibra."""
        raise CollibraPushNotImplementedError(
            "Collibra non-dry-run sync is not implemented for this deployment; "
            "configure a real Collibra client before running push mode."
        )
