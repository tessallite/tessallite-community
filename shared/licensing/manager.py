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
import importlib.util
import logging
from dataclasses import dataclass
from typing import Any

from .errors import LicenseError

logger = logging.getLogger(__name__)

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

    # Bug-7466: a build in which the closed manager is PRESENT but unloadable is
    # not the same state as a source-only checkout where it is simply absent, and
    # must not present to the operator as "not activated". ``load_error`` carries
    # that distinction to ``status()`` and to the 403 reason.
    LOAD_FAILED_REASON = (
        "License manager failed to load. The closed license manager is present "
        "in this build but could not be loaded, so no license can be verified. "
        "This is a build/packaging fault, not an activation state — check the "
        "service logs and reinstall the build."
    )

    def __init__(
        self,
        load_error: str | None = None,
        license_error_code: str | None = None,
    ) -> None:
        self._load_error = load_error
        # Bug-8164: when the stub stands in for a REJECTED licence (expired / bad
        # signature / unknown key), carry that error's stable machine-readable
        # taxonomy code so the operator-facing status path can say WHY, not just
        # "not activated". Distinct from ``load_error`` (a build/packaging fault).
        self._license_error_code = license_error_code

    def status(self) -> dict[str, Any]:
        out: dict[str, Any] = {"edition": "unactivated", "activated": False}
        if self._load_error:
            out["license_state"] = "manager_load_failed"
            out["load_error"] = self._load_error
        elif self._license_error_code:
            out["license_state"] = "invalid"
            out["error_code"] = self._license_error_code
        return out

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
            reason=self.LOAD_FAILED_REASON if self._load_error else self.REASON,
        )


_CLOSED_IMPL = f"{__package__}.closed.manager_impl"

# Bug-7466: the closed-manager probe is TRI-STATE — collapsing PROBE_FAILED into
# ABSENT is exactly the silent-degrade defect this fix closes.
_PROBE_ABSENT = "absent"        # genuinely not installed -> silent Community stub
_PROBE_PRESENT = "present"      # a spec was found -> the import below is authoritative
_PROBE_FAILED = "probe_failed"  # present-but-broken / discovery error -> load-failed


def _probe_closed_impl() -> str:
    """Tri-state presence probe for the closed manager module (Bug-7466).

    ``find_spec`` imports the parent packages to locate the target, so its
    outcome distinguishes three cases that MUST NOT be collapsed into one:

      * ``_PROBE_ABSENT``  — the closed tree (the target or one of its parent
        packages) is simply not installed. This is a source-only / Community
        checkout and falls back to the open stub SILENTLY.
      * ``_PROBE_PRESENT`` — a spec was found; the import in ``load_manager`` is
        authoritative.
      * ``_PROBE_FAILED``  — ``find_spec`` raised for a NON-absence reason: a
        present-but-broken parent ``__init__`` (e.g. its initializer raises
        ``RuntimeError``), a compiled parent that fails to load, a
        ``ModuleNotFoundError`` for some OTHER dependency the closed package
        imports, or any other spec-discovery error. This is a build/packaging
        fault, NOT ordinary absence, and must reach the loud
        ``manager_load_failed`` branch — never the silent stub.

    The previous ``except Exception: return False`` hid every ``_PROBE_FAILED``
    as absence, so a present-but-broken closed package read as a source-only
    checkout and silently degraded to unactivated.
    """
    try:
        spec = importlib.util.find_spec(_CLOSED_IMPL)
    except ModuleNotFoundError as exc:
        # A package in the dotted path is missing. If the missing module IS the
        # target or one of its parents, the closed tree is genuinely absent. If
        # it is some OTHER module, a PRESENT closed package failed to import its
        # own dependency -> a broken build, not absence.
        missing = exc.name or ""
        if missing and (missing == _CLOSED_IMPL or _CLOSED_IMPL.startswith(missing + ".")):
            return _PROBE_ABSENT
        logger.error(
            "closed license manager %s presence probe failed: its package raised "
            "ModuleNotFoundError for %r; treating as present-but-broken",
            _CLOSED_IMPL, missing, exc_info=True,
        )
        return _PROBE_FAILED
    except Exception:  # noqa: BLE001 — a present-but-broken parent / discovery error
        logger.error(
            "closed license manager %s presence probe raised; treating as "
            "present-but-broken (build fault), not absent",
            _CLOSED_IMPL, exc_info=True,
        )
        return _PROBE_FAILED
    return _PROBE_PRESENT if spec is not None else _PROBE_ABSENT


def load_manager(**kwargs: Any) -> LicenseManager:
    """Return the closed manager if available + configured, else the open stub.

    The closed implementation lives in ``licensing.closed`` (not in the public
    repo). Its absence (source-only checkout) yields the stub — the product runs
    in an unactivated state instead of crashing.

    Bug-7466 — three outcomes, not one. Previously every failure collapsed into
    a bare ``UnactivatedManager``, so a Community build shipping a broken
    compiled manager was indistinguishable from a source-only dev checkout: the
    product refused every control-plane create with "License not activated" and
    nothing anywhere said the cause was the build.

      * closed module ABSENT      -> stub, silently. This is source-only dev.
      * closed module PRESENT but unloadable, its presence probe FAILING, or
        ``build_manager`` failing for a non-licence reason -> stub carrying
        ``load_error``, logged at ERROR. The operator sees "License manager
        failed to load", not "not activated".
      * ``build_manager`` raising a ``LicenseError`` (expired, bad signature,
        unknown key) -> stub carrying that error's ``error_code`` (Bug-8164),
        logged at WARNING. This is an ordinary licence state, reclassified
        downstream by model-service's ``_InvalidLicenseManager`` (Bug-6437).

    This function never raises. "Degrades, never crashes" is the contract of the
    whole open shell (see the module docstring), and the licence-policy failures
    above are expected runtime states reached by a licence merely expiring — a
    caller such as ``licensing_guard.reload_license_manager`` invokes this at
    service startup with no exception handling, so raising here would turn an
    expired licence into a failed boot.
    """
    probe = _probe_closed_impl()
    if probe == _PROBE_ABSENT:
        # Source-only checkout: the closed module is simply not in this build.
        return UnactivatedManager()
    if probe == _PROBE_FAILED:
        # Bug-7466: a present-but-broken closed package (its probe raised for a
        # non-absence reason) is a BUILD FAULT, not source-only absence. Surface
        # the loud manager_load_failed state instead of silently degrading.
        return UnactivatedManager(
            load_error="presence probe failed: closed manager present but not loadable"
        )

    try:
        from .closed.manager_impl import build_manager  # type: ignore
    except Exception as exc:  # noqa: BLE001 — present but unloadable
        logger.error(
            "closed license manager %s is present but failed to import (%s); "
            "no license can be verified in this build",
            _CLOSED_IMPL, type(exc).__name__, exc_info=True,
        )
        return UnactivatedManager(load_error=f"import failed: {type(exc).__name__}")

    try:
        manager = build_manager(**kwargs)
    except LicenseError as exc:
        # An ordinary licence outcome (expired / invalid signature / unknown
        # key), not a build fault. Degrade, but carry the machine-readable
        # taxonomy code (Bug-8164) so the status path can say WHY.
        logger.warning(
            "license rejected by the closed manager (%s): %s",
            type(exc).__name__, exc,
        )
        return UnactivatedManager(license_error_code=exc.error_code)
    except Exception as exc:  # noqa: BLE001 — an internal fault in the closed manager
        logger.error(
            "closed license manager build_manager() raised %s; "
            "no license can be verified in this build",
            type(exc).__name__, exc_info=True,
        )
        return UnactivatedManager(load_error=f"build failed: {type(exc).__name__}")

    return manager or UnactivatedManager()
