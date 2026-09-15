"""Bug-9745 (review F2) — a source error must not name physical identifiers.

The first fix mapped a missing ROW-SECURITY column to a typed non-disclosing
response on the normal execute path and deliberately left every other column
error verbatim as "operational help". That was inconsistent with the boundary
the service already enforces (``api/_sql_disclosure`` exists to protect exactly
these names), and it was incomplete: member discovery and the routing catch-all
built their own 502 from the shared sanitizer, so the same driver text reached
callers through a different door.

The second fix subtracted the known-bad shapes from the native text with a
regular expression. That left the native text as the DEFAULT output, so every
message shape nobody had written a pattern for was still a leak.

This is the third and structural fix: the reply is CHOSEN from a fixed set and
never BUILT from the exception. These tests hold that property — including for
inputs the classifier does not recognise, which is the case the previous two
designs got wrong.
"""
from __future__ import annotations

import pytest

from shared.error_sanitizer import (
    CLIENT_MESSAGES,
    SourceFault,
    classify_source_error,
    client_safe_error,
)

FIXED_MESSAGES = set(CLIENT_MESSAGES.values())


class _PgError(Exception):
    """An exception carrying a SQLSTATE the way asyncpg does."""

    def __init__(self, message, sqlstate):
        super().__init__(message)
        self.sqlstate = sqlstate


class _Wrapped(Exception):
    """A SQLAlchemy-style wrapper holding the driver error in ``.orig``."""

    def __init__(self, message, orig):
        super().__init__(message)
        self.orig = orig


# --- the property that makes this design different --------------------------

@pytest.mark.parametrize("raw", [
    'column "nonexistent_dimension_xyz" does not exist',
    'relation "reporting.fact_sales" does not exist',
    'table "customer_pii" does not exist',
    'schema "restricted" does not exist',
    'Table "acme-prod:warehouse.salaries" was not found in location EU',
    "postgresql://user:pw@10.0.0.4:5432/prod — connection refused",
    '/srv/tessallite/src/routing/router.py:812: AssertionError: SELECT '
    'salary FROM hr.employees',
    "permission denied for table hr_salaries",
    "an entirely unrecognised failure from some future driver",
    "",
])
def test_the_reply_is_always_one_of_the_fixed_messages(raw):
    """Whatever goes in, what comes out is a constant from the table.

    This is the guard the two earlier designs could not have passed: it holds
    for inputs nobody anticipated, because the input never reaches the output.
    """
    assert client_safe_error(Exception(raw)) in FIXED_MESSAGES


@pytest.mark.parametrize("secret", [
    "nonexistent_dimension_xyz", "fact_sales", "customer_pii", "restricted",
    "salaries", "hr_salaries", "10.0.0.4", "postgresql://", "router.py",
    "acme-prod",
])
def test_no_identifier_from_the_exception_survives(secret):
    out = client_safe_error(Exception(
        f'relation "{secret}" does not exist at {secret} via {secret}'
    ))
    assert secret not in out


def test_the_specific_reported_leak():
    """The live-reproduced case: a row-security rule's internal column name."""
    out = client_safe_error(
        Exception('column "nonexistent_dimension_xyz" does not exist')
    )
    assert "nonexistent_dimension_xyz" not in out
    assert out == CLIENT_MESSAGES[SourceFault.OBJECT_MISSING]


# --- classification: precision, never disclosure ----------------------------

@pytest.mark.parametrize("sqlstate,expected", [
    ("42P01", SourceFault.OBJECT_MISSING),     # undefined_table
    ("42703", SourceFault.OBJECT_MISSING),     # undefined_column
    ("42501", SourceFault.PERMISSION_DENIED),  # insufficient_privilege
    ("57014", SourceFault.CANCELLED),          # query_canceled
    ("08006", SourceFault.UNREACHABLE),        # connection_failure
    ("28000", SourceFault.PERMISSION_DENIED),  # invalid_authorization
    ("3F000", SourceFault.OBJECT_MISSING),     # invalid_schema_name
    ("42601", SourceFault.INVALID_QUERY),      # syntax_error
    ("53200", SourceFault.RESOURCE_LIMIT),     # out_of_memory
])
def test_sqlstate_decides_the_category(sqlstate, expected):
    """SQLSTATE needs no text matching, so it cannot drift with driver wording."""
    assert classify_source_error(_PgError("anything at all", sqlstate)) is expected


def test_sqlstate_is_read_through_a_driver_wrapper():
    """SQLAlchemy hides the driver error in ``.orig``; the code is still found."""
    wrapped = _Wrapped("(asyncpg.UndefinedTableError) ...", _PgError("x", "42P01"))
    assert classify_source_error(wrapped) is SourceFault.OBJECT_MISSING


@pytest.mark.parametrize("raw,expected", [
    ("connection refused", SourceFault.UNREACHABLE),
    ("could not connect to server", SourceFault.UNREACHABLE),
    ("permission denied for table x", SourceFault.PERMISSION_DENIED),
    ("Not found: Table acme:ds.t was not found in location EU",
     SourceFault.OBJECT_MISSING),
    ("Resources exceeded during query execution", SourceFault.RESOURCE_LIMIT),
    ("canceling statement due to user request", SourceFault.CANCELLED),
    ("syntax error at or near SELECT", SourceFault.INVALID_QUERY),
])
def test_sources_without_sqlstate_are_classified_on_wording(raw, expected):
    """BigQuery, Snowflake and Spark report no SQLSTATE."""
    assert classify_source_error(Exception(raw)) is expected


def test_an_unrecognised_error_is_a_first_class_answer():
    """Unknown is a category with a safe message, not a fall-through to raw text."""
    fault = classify_source_error(Exception("something nobody has seen before"))
    assert fault is SourceFault.UNKNOWN
    assert client_safe_error(Exception("something nobody has seen before")) == (
        CLIENT_MESSAGES[SourceFault.UNKNOWN]
    )


def test_every_fault_has_a_message():
    """A new fault kind cannot be added without its outward message."""
    for fault in SourceFault:
        assert CLIENT_MESSAGES.get(fault), f"{fault} has no client message"


def test_no_message_contains_a_placeholder():
    """Nothing in the table is interpolated at call time."""
    for message in CLIENT_MESSAGES.values():
        assert "{" not in message and "%s" not in message
