"""Scoped keyset cursors for semantic drill-through pagination.

The token is authenticated, not encrypted. It carries only a scope digest and
the typed order-key values needed to continue after the last returned row.
Tenant/project/model/query/security identity is hashed into the scope digest so
a valid token cannot be replayed against another governed query surface.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence
from uuid import UUID


_CURSOR_VERSION = 2
_CURSOR_HMAC_LABEL = b"tessallite-drill-keyset-cursor:"
_MAX_TOKEN_LENGTH = 16_384
_MAX_ORDER_TERMS = 64
_MAX_STRING_VALUE_LENGTH = 4_096


class CursorValidationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class CursorOrderTerm:
    name: str
    descending: bool = False


@dataclass(frozen=True)
class CursorValue:
    kind: str
    value: Any


def _signing_key() -> bytes:
    from shared.config.settings import get_settings

    return get_settings().JWT_SECRET_KEY.encode("utf-8")


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    ).encode("utf-8")


def _scope_digest(scope: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(scope)).hexdigest()


def _signature(payload_b64: str) -> str:
    return hmac.new(
        _signing_key(),
        _CURSOR_HMAC_LABEL + payload_b64.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()


def _pack_value(value: Any) -> list[Any]:
    if value is None:
        return ["null", None]
    if isinstance(value, bool):
        return ["bool", value]
    if isinstance(value, int):
        return ["int", str(value)]
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise CursorValidationError(
                "UNSTABLE_CURSOR_VALUE", "Cursor order value is not finite."
            )
        return ["decimal", str(value)]
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CursorValidationError(
                "UNSTABLE_CURSOR_VALUE", "Cursor order value is not finite."
            )
        return ["float", repr(value)]
    if isinstance(value, datetime):
        return ["datetime", value.isoformat()]
    if isinstance(value, date):
        return ["date", value.isoformat()]
    if isinstance(value, UUID):
        return ["uuid", str(value)]
    if isinstance(value, str):
        if len(value) > _MAX_STRING_VALUE_LENGTH:
            raise CursorValidationError(
                "UNSTABLE_CURSOR_VALUE", "Cursor order value is too long."
            )
        return ["str", value]
    raise CursorValidationError(
        "UNSTABLE_CURSOR_VALUE",
        f"Cursor order value type {type(value).__name__!r} is unsupported.",
    )


def _unpack_value(encoded: Any) -> CursorValue:
    if not isinstance(encoded, list) or len(encoded) != 2:
        raise CursorValidationError("INVALID_CURSOR", "Malformed cursor order value.")
    kind, value = encoded
    try:
        if kind == "null" and value is None:
            return CursorValue("null", None)
        if kind == "bool" and isinstance(value, bool):
            return CursorValue("bool", value)
        if kind == "int" and isinstance(value, str):
            return CursorValue("int", int(value))
        if kind == "decimal" and isinstance(value, str):
            parsed = Decimal(value)
            if not parsed.is_finite():
                raise InvalidOperation
            return CursorValue("decimal", parsed)
        if kind == "float" and isinstance(value, str):
            parsed_float = float(value)
            if not math.isfinite(parsed_float):
                raise ValueError
            return CursorValue("float", parsed_float)
        if kind == "datetime" and isinstance(value, str):
            return CursorValue("datetime", datetime.fromisoformat(value))
        if kind == "date" and isinstance(value, str):
            return CursorValue("date", date.fromisoformat(value))
        if kind == "uuid" and isinstance(value, str):
            return CursorValue("uuid", UUID(value))
        if kind == "str" and isinstance(value, str):
            if len(value) > _MAX_STRING_VALUE_LENGTH:
                raise ValueError
            return CursorValue("str", value)
    except (ValueError, TypeError, InvalidOperation) as exc:
        raise CursorValidationError(
            "INVALID_CURSOR", "Malformed typed cursor order value."
        ) from exc
    raise CursorValidationError("INVALID_CURSOR", "Unknown cursor order value type.")


@dataclass(frozen=True)
class DrillCursorSpec:
    scope_digest: str
    order_terms: tuple[CursorOrderTerm, ...]
    stable: bool

    @classmethod
    def build(
        cls,
        *,
        scope: Mapping[str, Any],
        order_terms: Sequence[CursorOrderTerm],
        stable: bool,
    ) -> "DrillCursorSpec":
        terms = tuple(order_terms)
        if not terms or len(terms) > _MAX_ORDER_TERMS:
            raise CursorValidationError(
                "STABLE_CURSOR_UNAVAILABLE",
                "A bounded, non-empty order key is required for drill pagination.",
            )
        scoped = {
            **dict(scope),
            "cursor_version": _CURSOR_VERSION,
            "order": [
                {"name": term.name, "descending": term.descending}
                for term in terms
            ],
            "stable": bool(stable),
        }
        return cls(_scope_digest(scoped), terms, bool(stable))

    def encode(self, row: Mapping[str, Any] | None = None) -> str:
        if row is not None and not self.stable:
            raise CursorValidationError(
                "STABLE_CURSOR_UNAVAILABLE",
                "This drill result has no projectable unique order key; "
                "continuation would risk skipped or repeated rows.",
            )
        values: list[list[Any]] = []
        if row is not None:
            for term in self.order_terms:
                if term.name not in row:
                    raise CursorValidationError(
                        "UNSTABLE_CURSOR_VALUE",
                        f"Result row is missing cursor order column {term.name!r}.",
                    )
                values.append(_pack_value(row[term.name]))
        payload = {
            "v": _CURSOR_VERSION,
            "s": self.scope_digest,
            "k": values,
        }
        payload_b64 = base64.urlsafe_b64encode(_canonical_json(payload)).decode(
            "ascii"
        ).rstrip("=")
        token = f"{payload_b64}.{_signature(payload_b64)}"
        if len(token) > _MAX_TOKEN_LENGTH:
            raise CursorValidationError(
                "CURSOR_TOO_LARGE",
                "The complete drill order key is too large to encode safely; "
                "reduce projected key size or use a smaller primary-key dimension.",
            )
        return token

    def decode(self, token: str | None) -> tuple[CursorValue, ...] | None:
        if not token:
            return None
        if not isinstance(token, str) or len(token) > _MAX_TOKEN_LENGTH:
            raise CursorValidationError("INVALID_CURSOR", "Cursor is malformed.")
        payload_b64, separator, presented_signature = token.rpartition(".")
        if not separator or not payload_b64 or not presented_signature:
            raise CursorValidationError(
                "INVALID_CURSOR", "Cursor is unsigned or malformed."
            )
        if not hmac.compare_digest(presented_signature, _signature(payload_b64)):
            raise CursorValidationError(
                "INVALID_CURSOR", "Cursor signature verification failed."
            )
        try:
            padding = "=" * (-len(payload_b64) % 4)
            raw = base64.b64decode(
                payload_b64 + padding,
                altchars=b"-_",
                validate=True,
            )
            payload = json.loads(raw)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise CursorValidationError("INVALID_CURSOR", "Cursor payload is malformed.") from exc
        if not isinstance(payload, dict) or set(payload) != {"v", "s", "k"}:
            raise CursorValidationError("INVALID_CURSOR", "Cursor payload shape is invalid.")
        if payload["v"] != _CURSOR_VERSION:
            raise CursorValidationError(
                "STALE_CURSOR", "Cursor version is no longer supported; restart the drill."
            )
        if not isinstance(payload["s"], str) or not hmac.compare_digest(
            payload["s"], self.scope_digest
        ):
            raise CursorValidationError(
                "STALE_CURSOR",
                "Cursor does not belong to this tenant, project, model, query, or security context.",
            )
        keys = payload["k"]
        if not isinstance(keys, list):
            raise CursorValidationError("INVALID_CURSOR", "Cursor order key is malformed.")
        if len(keys) == 0:
            return None
        if len(keys) != len(self.order_terms):
            raise CursorValidationError(
                "STALE_CURSOR", "Cursor order shape changed; restart the drill."
            )
        return tuple(_unpack_value(value) for value in keys)
