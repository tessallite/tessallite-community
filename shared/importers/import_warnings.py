"""Canonical structured warnings for every supported import producer.

The JSON catalog is the finite producer contract: code, source, severity,
action, element derivation, and exact typed parameter schema.  Current
producers must call :func:`make_import_warning`; free text is retained only in
``detail`` for headless diagnostics and unknown/legacy UI fallback.
"""
from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

WarningSeverity = Literal["info", "warning", "error"]
_PARAM_TYPES = frozenset({"string", "integer", "string_list"})


@dataclass(frozen=True, slots=True)
class WarningSpec:
    source: str
    severity: WarningSeverity
    action: str
    params: Mapping[str, str]
    element_param: str | None = None


def _load_catalog() -> dict[str, WarningSpec]:
    path = Path(__file__).with_name("import_warning_catalog.json")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not raw:
        raise RuntimeError("import warning catalog must be a non-empty object")
    catalog: dict[str, WarningSpec] = {}
    for code, value in raw.items():
        if not isinstance(code, str) or not isinstance(value, dict):
            raise RuntimeError("invalid import warning catalog entry")
        params = value.get("params")
        if not isinstance(params, dict) or any(
            not isinstance(name, str) or kind not in _PARAM_TYPES
            for name, kind in params.items()
        ):
            raise RuntimeError(f"invalid parameter schema for {code}")
        severity = value.get("severity")
        if severity not in {"info", "warning", "error"}:
            raise RuntimeError(f"invalid severity for {code}")
        source = value.get("source")
        action = value.get("action")
        if not isinstance(source, str) or not source:
            raise RuntimeError(f"invalid source for {code}")
        if not isinstance(action, str) or not action:
            raise RuntimeError(f"invalid action for {code}")
        element_param = value.get("element_param")
        if element_param is not None and element_param not in params:
            raise RuntimeError(f"invalid element_param for {code}")
        catalog[code] = WarningSpec(
            source=source,
            severity=severity,
            action=action,
            params=dict(params),
            element_param=element_param,
        )
    return catalog


WARNING_CATALOG = _load_catalog()
KNOWN_WARNING_CODES = frozenset(WARNING_CATALOG)


class ImportWarningResponse(BaseModel):
    """Stable backend/frontend warning record."""

    model_config = ConfigDict(frozen=True)

    code: str
    severity: WarningSeverity
    source: str
    element: str | None = None
    action: str
    params: dict[str, Any] = Field(default_factory=dict)
    detail: str


def _validate_params(code: str, values: Mapping[str, Any]) -> dict[str, Any]:
    spec = WARNING_CATALOG[code]
    params = dict(values)
    expected = set(spec.params)
    actual = set(params)
    if actual != expected:
        raise ValueError(
            f"warning {code} params mismatch: missing={sorted(expected - actual)} "
            f"extra={sorted(actual - expected)}"
        )
    for name, kind in spec.params.items():
        value = params[name]
        valid = (
            (kind == "string" and isinstance(value, str))
            or (
                kind == "integer"
                and isinstance(value, int)
                and not isinstance(value, bool)
            )
            or (
                kind == "string_list"
                and isinstance(value, list)
                and all(isinstance(item, str) for item in value)
            )
        )
        if not valid:
            raise TypeError(
                f"warning {code} param {name!r} must be {kind}, "
                f"got {type(value).__name__}"
            )
    return params


def make_import_warning(
    *,
    code: str,
    detail: str,
    params: Mapping[str, Any],
) -> ImportWarningResponse:
    """Create one known warning, failing closed on code/parameter drift."""
    if code not in WARNING_CATALOG:
        raise ValueError(f"unknown import warning code: {code}")
    if not isinstance(detail, str) or not detail:
        raise ValueError(f"warning {code} requires diagnostic detail")
    spec = WARNING_CATALOG[code]
    typed_params = _validate_params(code, params)
    element = None
    if spec.element_param is not None:
        element = str(typed_params[spec.element_param])
    return ImportWarningResponse(
        code=code,
        severity=spec.severity,
        source=spec.source,
        element=element,
        action=spec.action,
        params=typed_params,
        detail=detail,
    )


def extend_known_import_warnings(
    target: list[ImportWarningResponse], values: Iterable[object]
) -> None:
    """Transfer producer records without opening a free-text bypass.

    Parser-to-mapper and partial-parser merges are still producer seams.  They
    must not use a raw ``list.extend`` that could silently carry a string or an
    unknown code around the direct-constructor inventory guard.
    """
    for value in values:
        if not isinstance(value, ImportWarningResponse):
            raise TypeError(
                "current import producers may transfer only "
                "ImportWarningResponse records"
            )
        if value.code not in KNOWN_WARNING_CODES:
            raise ValueError(
                f"current import producer transferred unknown code: {value.code}"
            )
        target.append(_normalize_known(value.model_dump()))


def _normalize_known(value: Mapping[str, Any]) -> ImportWarningResponse:
    code = str(value.get("code", ""))
    warning = make_import_warning(
        code=code,
        params=(
            value.get("params", {})
            if isinstance(value.get("params", {}), Mapping)
            else {}
        ),
        detail=str(value.get("detail", "")),
    )
    for field in ("source", "severity", "action", "element"):
        supplied = value.get(field)
        if supplied is not None and supplied != getattr(warning, field):
            raise ValueError(
                f"known warning {code} supplied inconsistent {field}: {supplied!r}"
            )
    return warning


def normalize_import_warnings(
    values: Iterable[object], *, source: str
) -> list[ImportWarningResponse]:
    """Normalize only genuine legacy/unknown compatibility inputs.

    Known records are revalidated against the catalog.  Unknown structured
    records and old strings preserve diagnostic detail for headless clients and
    the SPA's unknown-code fallback; no English-text heuristic assigns meaning.
    """
    normalized: list[ImportWarningResponse] = []
    for value in values:
        if isinstance(value, ImportWarningResponse):
            if value.code in KNOWN_WARNING_CODES:
                normalized.append(_normalize_known(value.model_dump()))
            else:
                normalized.append(value)
            continue
        if isinstance(value, Mapping):
            code = str(value.get("code", ""))
            if code in KNOWN_WARNING_CODES:
                normalized.append(_normalize_known(value))
                continue
            detail = str(value.get("detail", ""))
            if code and detail:
                severity = value.get("severity", "warning")
                if severity not in {"info", "warning", "error"}:
                    severity = "warning"
                normalized.append(ImportWarningResponse(
                    code=code,
                    severity=severity,
                    source=str(value.get("source", source)),
                    element=(
                        str(value["element"])
                        if value.get("element") is not None else None
                    ),
                    action=str(value.get("action", "review")),
                    params=(
                        dict(value.get("params", {}))
                        if isinstance(value.get("params", {}), Mapping) else {}
                    ),
                    detail=detail,
                ))
                continue
        # Genuine legacy string/unknown object: no heuristic classification.
        normalized.append(ImportWarningResponse(
            code="legacy.warning",
            severity="warning",
            source=source,
            element=None,
            action="review",
            params={},
            detail=str(value),
        ))
    return normalized
