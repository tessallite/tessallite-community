"""Measure values leave a protocol boundary as JSON NUMBERS (Bug-9876/9910).

A result row reaches the API layer with whatever Python type the source driver
produced. For a ``NUMERIC``/``DECIMAL`` column — which is what a SUM over a
money or count column is on every supported source — that type is
``decimal.Decimal``, and pydantic's JSON mode serialises a ``Decimal`` as a
STRING.

The consequence is a wrong number with no error anywhere. The Excel add-in
writes the string into a cell, Excel stores it as TEXT, and a PivotTable over
that column COUNTS instead of summing. Measured live on the local stack
(``modely``, tenant admin)::

    base_amount        -> "180442041.28"     (string)
    transaction_count  -> "1.0E+5"           (string, scientific notation)
    unique_customers   -> 99993              (int -> already a JSON number)

The text form is not even stable between measures: ``str(Decimal)`` follows the
value's exponent, so one measure arrives as plain decimal text and the next in
scientific notation. Every consumer therefore has to GUESS which strings are
numbers, and each consumer guesses differently. This module removes the guess:
the producer names its own measure columns and types them.

Scope note. Only the named measure columns are converted. A dimension column
keeps exactly the type the source returned — the add-in's member-key fan-out
compares dimension values as strings, and silently renumbering ``"0042"`` to
``42`` would break member matching.

Precision note. A non-integral ``Decimal`` is narrowed to an IEEE-754 double.
That is not a loss this boundary can avoid and not one the consumer could have
kept: JSON numbers are doubles to every JavaScript client, and an Excel cell
stores a double. An INTEGRAL ``Decimal`` is emitted as a Python ``int`` instead,
which JSON carries exactly at any magnitude.
"""
from __future__ import annotations

import math
from decimal import Decimal
from typing import Any, Iterable


def coerce_measure_value(value: Any) -> Any:
    """One measure cell, typed for the wire.

    ``Decimal`` becomes an ``int`` (when integral) or a ``float``. A non-finite
    value — ``Decimal('NaN')``, ``Decimal('Infinity')``, ``float('nan')`` —
    becomes ``None``: JSON has no literal for it, and emitting a bare ``NaN``
    token produces a body that ``JSON.parse`` rejects outright, which reaches
    the user as an unexplained failure rather than as an empty cell.

    Every other type is returned unchanged. ``bool`` is deliberately included in
    that set: it is an ``int`` subclass in Python, and a boolean measure must
    stay ``true``/``false`` rather than collapse to ``1``/``0``.
    """
    if isinstance(value, Decimal):
        if not value.is_finite():
            return None
        # ``to_integral_value`` keeps arbitrary precision; ``==`` on Decimals is
        # exact, so this asks "is this a whole number?" without going through a
        # float first.
        if value == value.to_integral_value():
            return int(value)
        return float(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def coerce_measure_values(
    rows: list[dict[str, Any]],
    measure_names: Iterable[str],
) -> list[dict[str, Any]]:
    """Return ``rows`` with every named measure column typed for the wire.

    ``measure_names`` must come from the bound query's resolved measures — the
    same authority that names the columns in the response annotation — so the
    set of columns the annotation describes and the set this function types can
    never drift apart.
    """
    names = [n for n in measure_names if n]
    if not names or not rows:
        return rows
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            out.append(row)
            continue
        coerced = dict(row)
        for name in names:
            if name in coerced:
                coerced[name] = coerce_measure_value(coerced[name])
        out.append(coerced)
    return out
