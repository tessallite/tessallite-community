"""Canonical data-type families across all source connectors.

Source connectors spell their column types differently:

- PostgreSQL / Redshift: ``integer``, ``numeric``, ``timestamp without time zone``,
  ``character varying``, ``boolean``
- BigQuery: ``int64``, ``float64``, ``numeric``, ``string``, ``datetime``, ``bool``
- Snowflake: ``number``, ``float``, ``timestamp_ntz``, ``text``, ``boolean``
- SQL Server: ``int``, ``decimal``, ``datetime``, ``nvarchar``, ``bit``
- Spark / Hive: ``double``, ``bigint``, ``string``, ``timestamp``, ``boolean``

The auto-classify profiling heuristics must reason about a *family*
(``numeric`` / ``datetime`` / ``boolean`` / ``text``), not a connector-native
spelling, or measures and time dimensions are silently lost on every connector
but PostgreSQL (review finding F-014-02).

This module is the single source of truth for that mapping. It is intentionally
substring-based so it stays connector-agnostic — there is **no** per-connector
branching here, in line with the SQL-generation rules. Add a new spelling once,
here, and every consumer benefits.
"""

from __future__ import annotations

# Canonical family names returned by ``type_family``.
NUMERIC = "numeric"
DATETIME = "datetime"
BOOLEAN = "boolean"
TEXT = "text"
OTHER = "other"

# Substring tokens checked against the lowercased, parameter-stripped type name.
# Order matters: boolean is checked before numeric/text so ``bit`` and ``bool``
# do not get swallowed by a broader token, and datetime is checked before
# numeric so ``timestamp`` (which contains no numeric token) is unambiguous.
_BOOLEAN_TOKENS = ("bool", "bit")
_DATETIME_TOKENS = ("date", "time", "timestamp", "datetime")
_NUMERIC_TOKENS = (
    "int",       # int, integer, int2/4/8, bigint, smallint, int64, tinyint
    "numeric",
    "decimal",
    "float",     # float, float4/8, float64
    "double",
    "real",
    "number",    # snowflake NUMBER
    "money",
    "dec",       # ansi DEC
    "smallmoney",
)
_TEXT_TOKENS = (
    "char",      # char, varchar, nchar, nvarchar, bpchar, character (varying)
    "text",
    "string",
    "clob",
    "uuid",      # identifiers — treated as categorical text, never a measure
)


def _normalise(data_type: str | None) -> str:
    """Lowercase and strip type parameters/whitespace, e.g. ``NUMERIC(10,2)`` -> ``numeric``."""
    if not data_type:
        return ""
    t = str(data_type).strip().lower()
    # Drop precision/scale/length parameters: ``decimal(10,2)`` -> ``decimal``.
    if "(" in t:
        t = t.split("(", 1)[0].strip()
    return t


def type_family(data_type: str | None) -> str:
    """Map a connector-native data type to a canonical family.

    Returns one of ``numeric``, ``datetime``, ``boolean``, ``text``, ``other``.

    The check order guards against token collisions:
    ``bit`` / ``bool`` resolve to boolean before the numeric pass; ``timestamp``
    and ``datetime`` resolve to datetime before any text/numeric token can match.
    """
    t = _normalise(data_type)
    if not t:
        return OTHER
    # Boolean first: ``bit`` and ``bool`` must not be read as numeric/text.
    if any(tok in t for tok in _BOOLEAN_TOKENS):
        return BOOLEAN
    # Datetime before numeric/text: ``timestamp``/``datetime``/``date``/``time``.
    if any(tok in t for tok in _DATETIME_TOKENS):
        return DATETIME
    if any(tok in t for tok in _NUMERIC_TOKENS):
        return NUMERIC
    if any(tok in t for tok in _TEXT_TOKENS):
        return TEXT
    return OTHER


def is_numeric(data_type: str | None) -> bool:
    return type_family(data_type) == NUMERIC


def is_datetime(data_type: str | None) -> bool:
    return type_family(data_type) == DATETIME


def is_boolean(data_type: str | None) -> bool:
    return type_family(data_type) == BOOLEAN


def is_text(data_type: str | None) -> bool:
    return type_family(data_type) == TEXT
