"""Typed request contract for saved pivot-view configurations.

A pivot view is an API-boundary request: the analyst's row/column/measure/filter
layout, submitted to the model-service pivot-views endpoints and echoed back. It
is NOT a persisted semantic-model field, so it has no ORM column, no snapshot
serialiser entry, and no binder involvement — the ORM/snapshot/binder alignment
rule that governs model fields does not apply here.

This module gives that request a typed shape and a stable, machine-readable
rejection vocabulary so the client can map a validation failure to a friendly
message WITHOUT parsing prose (producer/consumer field alignment):

  * ``PivotConfigErrorCode`` — stable string tokens; the API returns one in the
    422 body's ``error_code`` and the frontend keys its ``ERROR_CODE_MAP`` off
    the identical values.
  * ``validate_pivot_config`` — pure (no DB) structural + format validation of
    the submitted config blob and reference ids. Referential-existence checks
    (does this measure/dimension belong to the model?) stay in the route because
    they require the tenant DB, but they raise the SAME typed vocabulary.

The structural half is deliberately STRICT on every field the frontend loader
actually dereferences (``measureSelections``, ``slicers``, ``conditionalFormat``,
the boolean/display toggles, ``personaId``) and LENIENT on genuinely-unknown
extra keys the loader never reads (``extra="allow"`` for forward-compat). The
acceptance bar is: no config that passes validation can crash the loader
(SlicerBar / PivotGrid dereference a malformed ``slicers`` / ``conditionalFormat``
otherwise — Bug-8161). A legitimate historical config the current frontend wrote
must still validate; the frontend decoder (pivotConfigDecode.ts) is the matching
belt-and-braces guard for configs persisted BEFORE this contract existed.

``measure_id`` contract (Bug-7442), enforced by ``validate_pivot_config`` +
the route request models:
  * POST (create): ``measure_id`` is REQUIRED and non-nullable — ``null`` is a
    422 (FastAPI request validation on ``PivotViewCreate.measure_id: str``).
  * PATCH (update) omitted / ``null``: leave the stored pointer UNCHANGED
    (``PivotViewUpdate.measure_id: str | None`` with ``exclude_unset``).
  * ``""`` (empty string): a valid sentinel meaning "no primary model-measure
    pointer" — the authoritative selection travels in ``config.measureSelections``
    and the pivot has a synthetic first column (Record Count / scratchpad / none).
    Skips only the measure existence check.
  * A non-empty string MUST be a UUID (else ``INVALID_MEASURE_ID``) and must
    resolve to a measure of this model (else ``UNKNOWN_MEASURE``).

History: Bug-8161 (untyped config could crash the loader), Bug-7442 (``measure_id``
semantics), Bug-8182 (client had only prose to surface, so recovery exposed raw
backend text).
"""
from __future__ import annotations

from enum import Enum
from typing import Annotated, Any, Literal, Optional, Union
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictBool


class PivotConfigErrorCode(str, Enum):
    """Stable, machine-readable reason a pivot-view config was rejected.

    ``str`` mixin so the value serialises directly to JSON as its token and
    compares equal to that token in tests and in the frontend map. Callers MUST
    depend on the enum member, never on the human ``message`` text.
    """

    # The ``config`` blob is not a valid object or a loader-critical sub-field
    # (``measureSelections`` / ``slicers`` / ``conditionalFormat`` / the display
    # toggles) has the wrong shape (Bug-8161).
    INVALID_STRUCTURE = "INVALID_STRUCTURE"
    # ``measure_id`` is a non-empty string that is not a UUID (Bug-7442).
    INVALID_MEASURE_ID = "INVALID_MEASURE_ID"
    # ``measure_id`` is a valid UUID but not a measure of this model.
    UNKNOWN_MEASURE = "UNKNOWN_MEASURE"
    # A row/column dimension id is not a UUID.
    INVALID_DIMENSION_ID = "INVALID_DIMENSION_ID"
    # A row/column dimension id is a valid UUID but not found in this model.
    UNKNOWN_DIMENSION = "UNKNOWN_DIMENSION"


# Full stable vocabulary — for the producer/consumer contract test that asserts
# the frontend ``ERROR_CODE_MAP`` covers exactly this set, and back.
ALL_PIVOT_CONFIG_ERROR_CODES: tuple[str, ...] = tuple(c.value for c in PivotConfigErrorCode)


class PivotConfigError(BaseModel):
    """A single typed validation failure: the machine code plus a human message.

    Serialised into the 422 response body's ``detail`` so ``detail.error_code``
    is the contract field and ``detail.message`` is the human fallback text.
    """

    error_code: PivotConfigErrorCode
    message: str


# ---------------------------------------------------------------------------
# Typed shapes for every config field the frontend loader dereferences.
# ---------------------------------------------------------------------------
#
# Rule for the crash-prone object/array fields (slicers, conditionalFormat,
# measureSelections): declared NON-Optional with ``default=None``. Pydantic then
# ACCEPTS an absent key (historical configs that never carried it) but REJECTS an
# explicit ``null`` or a scalar — the exact payloads (``slicers:[null]``,
# ``conditionalFormat:null``/``"oops"``) that dereference to a runtime crash.

# Mirrors the frontend ``SlicerOp`` union (controls/types.ts).
PivotSlicerOp = Literal[
    "eq", "ne", "gt", "gte", "lt", "lte", "in", "between", "like",
    "is_null", "is_not_null",
]


class PivotSlicer(BaseModel):
    """One WHERE-clause filter. Mirrors the frontend ``Slicer``; SlicerBar
    dereferences ``dimensionId`` / ``op`` / ``values`` on every saved view, so a
    ``null`` entry or a wrong ``op`` must be rejected, not persisted."""

    model_config = ConfigDict(extra="ignore")

    dimensionId: str = Field(min_length=1)
    op: PivotSlicerOp
    values: list[str] = Field(default_factory=list)


class PivotMeasureSelection(BaseModel):
    """One ordered (measure, aggregation) column of a saved pivot.

    Mirrors the frontend ``MeasureSel`` ({measureId, agg}). ``agg`` is optional
    (empty string means "measure default"); ``measureId`` must be a non-empty
    string. Validating this shape is what stops a malformed ``measureSelections``
    from crashing the loader (Bug-8161)."""

    model_config = ConfigDict(extra="ignore")

    measureId: str = Field(min_length=1)
    agg: str = ""


# Conditional-format discriminated union — mirrors the frontend
# ``ConditionalFormat`` (grid/PivotGrid.tsx). PivotGrid dereferences kind-specific
# fields (``low``/``high``/``color``/``below``/``above``/``threshold``); a wrong
# shape crashes ``interpolateColor``/``parseHex``, so each variant is typed.
class _CFNone(BaseModel):
    model_config = ConfigDict(extra="ignore")
    kind: Literal["none"]


class _CFColorScale(BaseModel):
    model_config = ConfigDict(extra="ignore")
    kind: Literal["color-scale"]
    low: str
    high: str


class _CFDataBars(BaseModel):
    model_config = ConfigDict(extra="ignore")
    kind: Literal["data-bars"]
    color: str


class _CFThreshold(BaseModel):
    model_config = ConfigDict(extra="ignore")
    kind: Literal["threshold"]
    below: str
    above: str
    threshold: float


PivotConditionalFormat = Annotated[
    Union[_CFNone, _CFColorScale, _CFDataBars, _CFThreshold],
    Field(discriminator="kind"),
]


class PivotViewConfigModel(BaseModel):
    """Structural contract for the saved-view ``config`` blob.

    Every field the loader READS is typed; unknown keys the loader never touches
    are accepted (``extra="allow"``) for forward-compat. Crash-prone object/array
    fields are non-Optional with a ``None`` default so an absent key passes (old
    configs) but an explicit ``null`` / scalar is rejected. ``sort`` stays opaque:
    the client's ``parsePivotViewSortConfig`` already degrades gracefully on a bad
    sort, so over-typing it would reject legitimate historical variants.
    """

    model_config = ConfigDict(extra="allow")

    configVersion: Optional[int] = None
    # Measure selection (current + legacy back-compat forms selectionsFromView reads).
    measureSelections: list[PivotMeasureSelection] = Field(default=None)
    extraMeasureIds: list[str] = Field(default=None)
    measureAggOverrides: dict[str, str] = Field(default=None)
    # Filters + display toggles.
    slicers: list[PivotSlicer] = Field(default=None)
    conditionalFormat: PivotConditionalFormat = Field(default=None)
    emptyCellMode: Literal["blank", "zero", "dash"] = Field(default=None)
    # StrictBool: reject a truthy string like "yes"/"1" (a display toggle that
    # silently mis-set is a wrong-render, not just a crash — reviewer B1).
    showSubtotals: StrictBool = Field(default=None)
    showGrandTotals: StrictBool = Field(default=None)
    forceLive: StrictBool = Field(default=None)
    personaId: Optional[str] = None
    # Sort is validated leniently by the client; keep it opaque here.
    sort: Optional[Any] = None


def _looks_like_uuid(value: str) -> bool:
    try:
        UUID(value)
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def validate_pivot_config_structure(config: Optional[dict]) -> Optional[PivotConfigError]:
    """Validate ONLY the config blob's STRUCTURE (Bug-8161) — the loader-crash
    vector — independent of the measure/dimension reference ids.

    Returns a typed ``INVALID_STRUCTURE`` error if the blob is not a JSON object
    or a dereferenced field (``slicers`` / ``conditionalFormat`` / display
    toggles / ``measureSelections``) has the wrong shape; ``None`` otherwise.
    Absent optional keys and genuinely-unknown extra keys are accepted.

    Used on its own by the PATCH publish path (review B2): sharing a view must
    revalidate the config STRUCTURE that could crash another user's loader, but
    must NOT re-reject a legacy ``measure_id``/dimension pointer that predates the
    format contract — those are a referential concern, not a share-safety one.
    """
    if config is None:
        return None
    if not isinstance(config, dict):
        return PivotConfigError(
            error_code=PivotConfigErrorCode.INVALID_STRUCTURE,
            message="Pivot config must be a JSON object.",
        )
    try:
        PivotViewConfigModel.model_validate(config)
    except Exception:
        return PivotConfigError(
            error_code=PivotConfigErrorCode.INVALID_STRUCTURE,
            message="Pivot config has an invalid structure.",
        )
    return None


def validate_pivot_config(
    config: Optional[dict],
    measure_id: str,
    row_dim_ids: list[str],
    col_dim_ids: list[str],
) -> Optional[PivotConfigError]:
    """Pure structural + format validation of a submitted pivot-view config.

    Returns the FIRST typed error found, or ``None`` when the config blob and all
    reference-id FORMATS are well-formed. This does NOT check that the referenced
    measure/dimensions exist in the model — that needs the tenant DB and is done
    by the route, which raises ``UNKNOWN_MEASURE`` / ``UNKNOWN_DIMENSION`` from
    this same vocabulary.

    ``measure_id`` semantics are documented at module level (Bug-7442). Here:
    an empty string is a valid sentinel and is NOT rejected; a NON-empty
    ``measure_id`` MUST be a UUID.
    """
    # 1. Config blob structure (Bug-8161). STRICT on every dereferenced field,
    #    lenient on unknown extras (PivotViewConfigModel.extra="allow").
    structure_err = validate_pivot_config_structure(config)
    if structure_err is not None:
        return structure_err

    # 2. measure_id format (Bug-7442): empty is allowed; non-empty must be a UUID.
    if measure_id and not _looks_like_uuid(measure_id):
        return PivotConfigError(
            error_code=PivotConfigErrorCode.INVALID_MEASURE_ID,
            message=f"measure_id {measure_id!r} is not a valid UUID.",
        )

    # 3. Dimension id formats: every row/col id must be a UUID.
    for raw_id in (*row_dim_ids, *col_dim_ids):
        if not _looks_like_uuid(raw_id):
            return PivotConfigError(
                error_code=PivotConfigErrorCode.INVALID_DIMENSION_ID,
                message=f"Dimension ID {raw_id!r} is not a valid UUID.",
            )

    return None


__all__ = [
    "PivotConfigErrorCode",
    "ALL_PIVOT_CONFIG_ERROR_CODES",
    "PivotConfigError",
    "PivotSlicer",
    "PivotSlicerOp",
    "PivotMeasureSelection",
    "PivotConditionalFormat",
    "PivotViewConfigModel",
    "validate_pivot_config",
    "validate_pivot_config_structure",
]
