"""Client-safe messages for errors that must not be published verbatim.

Bug-9745 (review F2, consolidation item 4). The outward message is CHOSEN from
a fixed set; it is never BUILT from the exception. Classification decides WHICH
constant is returned, so a classification miss costs precision and nothing else
— an unrecognised failure falls to the generic constant and still discloses
nothing.

That inversion is the whole point. The design this replaces did the opposite:
the driver's own text was the default output, and known-bad fragments were
subtracted from it by regular expression. Under that model every message shape
nobody had seen yet was a live leak waiting for someone to notice it and write
another pattern — which is exactly how a row-security rule's internal column
name reached callers as a 502 body, and why fixing the one handler that had
been patched did not stop the same text arriving through member discovery.

What is protected: the physical schema, table and column names the semantic
layer exists to keep behind logical names, the shape of the row-security
configuration, connection strings, server addresses, and internal file paths
and class names. Callers get the KIND of fault and what to do about it. The
full native text goes to the service log and to the persisted query-failure
record, which is reachable through the project-RBAC-gated diagnostics log.

Callers that raise a typed Tessallite error with an authored, deliberately
actionable message (an unknown measure, a malformed row-security rule, a
timeout) do not come through here — those are handled by their own branches and
keep their own text. This module is for the foreign and the unexpected.
"""
from __future__ import annotations

import re
from enum import Enum


class SourceFault(str, Enum):
    """The kinds of fault a caller is told apart."""

    UNREACHABLE = "source_unreachable"
    OBJECT_MISSING = "source_object_missing"
    PERMISSION_DENIED = "source_permission_denied"
    INVALID_QUERY = "source_invalid_query"
    RESOURCE_LIMIT = "source_resource_limit"
    CANCELLED = "source_cancelled"
    UNKNOWN = "source_error"


# The complete set of messages this module can return. Adding a case means
# adding a constant here, deliberately — not widening a pattern.
CLIENT_MESSAGES: dict[SourceFault, str] = {
    SourceFault.UNREACHABLE: (
        "The source database could not be reached. Its connection may be down "
        "or misconfigured. Check the model's source connection."
    ),
    SourceFault.OBJECT_MISSING: (
        "A table or column this model reads is no longer present in the "
        "source. It may have been dropped or renamed, or the connection may "
        "point to a different database. Check the model's source connection "
        "and re-synchronise the model with the source schema."
    ),
    SourceFault.PERMISSION_DENIED: (
        "The source connection is not permitted to read the data this query "
        "needs. Check the credentials the model's source connection uses."
    ),
    SourceFault.INVALID_QUERY: (
        "The source database rejected this query. This usually means the "
        "model no longer matches the source schema."
    ),
    SourceFault.RESOURCE_LIMIT: (
        "The source database ran out of capacity while answering this query. "
        "Try a narrower query, a smaller date range, or fewer columns."
    ),
    SourceFault.CANCELLED: (
        "The source database cancelled this query before it completed."
    ),
    SourceFault.UNKNOWN: (
        "The query could not be completed by the source database. The details "
        "have been recorded in the query log for your administrator."
    ),
}

# SQLSTATE is the strongest signal available and needs no text matching at all.
# Exact codes win over their class.
_SQLSTATE_EXACT: dict[str, SourceFault] = {
    "42P01": SourceFault.OBJECT_MISSING,   # undefined_table
    "42703": SourceFault.OBJECT_MISSING,   # undefined_column
    "42704": SourceFault.OBJECT_MISSING,   # undefined_object
    "42883": SourceFault.OBJECT_MISSING,   # undefined_function
    "42P02": SourceFault.OBJECT_MISSING,   # undefined_parameter
    "42501": SourceFault.PERMISSION_DENIED,  # insufficient_privilege
    "57014": SourceFault.CANCELLED,          # query_canceled
}

_SQLSTATE_CLASS: dict[str, SourceFault] = {
    "08": SourceFault.UNREACHABLE,        # connection exception
    "58": SourceFault.UNREACHABLE,        # system error (io)
    "28": SourceFault.PERMISSION_DENIED,  # invalid authorization
    "3D": SourceFault.OBJECT_MISSING,     # invalid_catalog_name
    "3F": SourceFault.OBJECT_MISSING,     # invalid_schema_name
    "42": SourceFault.INVALID_QUERY,      # syntax / access rule violation
    "22": SourceFault.INVALID_QUERY,      # data exception
    "0A": SourceFault.INVALID_QUERY,      # feature_not_supported
    "53": SourceFault.RESOURCE_LIMIT,     # insufficient_resources
    "54": SourceFault.RESOURCE_LIMIT,     # program_limit_exceeded
    "57": SourceFault.CANCELLED,          # operator intervention
}

# Fallback for sources that do not report SQLSTATE (BigQuery, Snowflake, Spark
# and the driver-agnostic wrappers around them). These patterns choose a
# CATEGORY only. They never contribute text to the reply, so an over-broad or
# missed match changes which safe sentence is returned and nothing more.
# Ordered: the first match wins.
_KEYWORD_RULES: tuple[tuple[re.Pattern[str], SourceFault], ...] = (
    (re.compile(
        r"connection (?:refused|reset|timed out|to server)"
        r"|could not connect|server closed the connection"
        r"|network is unreachable|name or service not known"
        r"|connection is closed|no route to host",
        re.IGNORECASE), SourceFault.UNREACHABLE),
    (re.compile(
        r"permission denied|access denied|not authori[sz]ed"
        r"|insufficient privilege|forbidden",
        re.IGNORECASE), SourceFault.PERMISSION_DENIED),
    (re.compile(
        r"does not exist|not found|unknown (?:table|column|field|database)"
        r"|invalid (?:table|object|identifier)|no such (?:table|column)"
        r"|was not found in location",
        re.IGNORECASE), SourceFault.OBJECT_MISSING),
    (re.compile(
        r"out of memory|too many connections|resources exceeded"
        r"|quota exceeded|exceeded .{0,40}limit|disk (?:is )?full",
        re.IGNORECASE), SourceFault.RESOURCE_LIMIT),
    (re.compile(
        r"cancell?ed|canceling statement|terminating connection due to",
        re.IGNORECASE), SourceFault.CANCELLED),
    (re.compile(
        r"syntax error|type mismatch|cannot be cast|invalid input syntax"
        r"|could not be parsed",
        re.IGNORECASE), SourceFault.INVALID_QUERY),
)


def _sqlstate_of(exc: BaseException) -> str | None:
    """The SQLSTATE an exception carries, unwrapping driver wrappers.

    SQLAlchemy wraps the driver error in ``.orig``; asyncpg spells the code
    ``sqlstate`` and psycopg spells it ``pgcode``.
    """
    seen = 0
    candidate: BaseException | None = exc
    while candidate is not None and seen < 4:
        for attr in ("sqlstate", "pgcode"):
            code = getattr(candidate, attr, None)
            if isinstance(code, str) and len(code) == 5:
                return code.upper()
        candidate = getattr(candidate, "orig", None)
        seen += 1
    return None


def classify_source_error(exc: BaseException) -> SourceFault:
    """Classify an exception into one of the fixed fault kinds.

    Unrecognised is a first-class answer, not a failure: it returns
    ``UNKNOWN``, whose message is as safe as every other.
    """
    code = _sqlstate_of(exc)
    if code:
        exact = _SQLSTATE_EXACT.get(code)
        if exact is not None:
            return exact
        by_class = _SQLSTATE_CLASS.get(code[:2])
        if by_class is not None:
            return by_class

    text = str(exc)
    for pattern, fault in _KEYWORD_RULES:
        if pattern.search(text):
            return fault
    return SourceFault.UNKNOWN


def client_safe_error(exc: BaseException) -> str:
    """The message a client may see for an error we did not author."""
    return CLIENT_MESSAGES[classify_source_error(exc)]
