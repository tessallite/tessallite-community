"""Canonical column-bound contract for ``kpi_latest`` / ``kpi_snapshots`` writes.

Bug-7982 R7 finding 5 (root cause). ``status_label`` was truncated to its column
width before every write, but ``formatted_value`` (``String(128)``) and
``kpi_name`` (``String(255)``) were not — so a long formatted value raises
``StringDataRightTruncationError`` inside the per-row upsert. That exception was
swallowed by a log-and-continue handler, ``evaluate-batch`` still returned HTTP
200, and both outbox-clearing paths deleted the durable ``pending_kpi_reeval``
row on the strength of that 200. A publish failure therefore destroyed its own
safety net. Bounding EVERY persisted string column removes the trigger; making
the failure propagate (see ``KpiPublishOutcome``) removes the class.

The widths are DERIVED from the ORM column definitions, never restated as
literals: a future migration that widens or narrows a column cannot leave a
stale constant behind (the exact drift class that produced the mismatch above).

The helpers live in ``shared`` because BOTH writers must apply the identical
bound — the model-service ``evaluate-batch`` publish (``api/kpi_latest.py``) and
the scheduler snapshot sweep (``jobs/sweep.py``). The sweep previously kept a
private copy of the status-label truncation with the comment "the scheduler does
not import across services"; it already imports the shared ordering guard, so
the copy was avoidable duplication of a safety-critical bound.
"""
from __future__ import annotations

from dataclasses import dataclass

from shared.db.models import KPILatest, KPISnapshot

# Marker appended when a value is cut, so truncation is visible in the UI/BI
# surface rather than silently changing the rendered number's meaning.
_ELLIPSIS = "…"


def _column_length(model, column_name: str) -> int | None:
    """The declared ``String`` length of an ORM column, or None when unbounded."""
    return getattr(model.__table__.c[column_name].type, "length", None)


def truncate_for_column(value: str | None, model, column_name: str) -> str | None:
    """Bound ``value`` to ``model.column_name``'s declared width.

    ``None`` passes through. An unbounded column (``Text``/no length) passes
    through unchanged. Otherwise the value is cut to the column width with a
    trailing ellipsis so the truncation is visible.
    """
    if value is None:
        return None
    max_len = _column_length(model, column_name)
    if max_len is None or len(value) <= max_len:
        return value
    return value[: max_len - 1] + _ELLIPSIS


def bound_kpi_latest_strings(
    *, kpi_name: str, status_label: str | None, formatted_value: str | None
) -> tuple[str, str | None, str | None]:
    """Bound every length-constrained ``kpi_latest`` string column.

    Returns ``(kpi_name, status_label, formatted_value)``. Callers MUST use this
    for all three — bounding only some of them is what left the reachable
    ``formatted_value`` overflow in place.
    """
    return (
        truncate_for_column(kpi_name, KPILatest, "kpi_name"),
        truncate_for_column(status_label, KPILatest, "status_label"),
        truncate_for_column(formatted_value, KPILatest, "formatted_value"),
    )


def bound_kpi_snapshot_status_label(label: str | None) -> str | None:
    """Bound ``kpi_snapshots.status_label`` to its declared column width."""
    return truncate_for_column(label, KPISnapshot, "status_label")


# ---------------------------------------------------------------------------
# Publish outcome
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KpiPublishOutcome:
    """The result of one ``kpi_latest`` publish attempt.

    Bug-7982 R7 finding 5: the publish helpers used to return ``None``, so a
    per-row failure was invisible to every caller and an HTTP 200 was returned
    regardless. The durable ``pending_kpi_reeval`` outbox row — whose entire
    purpose is to survive a failed publish — was then deleted on the strength of
    that 200. Callers MUST consult :attr:`succeeded` before clearing the outbox.

    * ``considered`` — rows the caller asked to publish.
    * ``persisted``  — rows the upsert actually wrote.
    * ``suppressed`` — rows a FRESHER published evaluation refused to regress.
      A deliberate no-op, not a failure: the stored row is already at or beyond
      the requested (epoch, generation), so the publish intent is satisfied.
    * ``skipped``    — rows whose stored value, deploy binding AND ordering token
      already equal what would be written (a true no-op).
    * ``failed``     — rows whose write raised. The ONLY outcome that must block
      an outbox clear.
    """

    considered: int = 0
    persisted: int = 0
    suppressed: int = 0
    skipped: int = 0
    failed: int = 0

    @property
    def succeeded(self) -> bool:
        """True when every considered row reached a definite published state.

        Fail-closed: any failure, or any row that was neither persisted,
        suppressed nor skipped, means the publish cannot be treated as proof
        that ``$KPIs`` now serves a current-epoch value.
        """
        if self.failed:
            return False
        return (
            self.persisted + self.suppressed + self.skipped
        ) == self.considered
