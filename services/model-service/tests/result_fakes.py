"""Small, boundary-faithful stand-ins for SQLAlchemy result objects.

``AsyncSession.execute`` returns a ``Result``.  Calling ``Result.scalars``
returns a separate ``ScalarResult`` view; it does not mutate the result or
return the same object.  Keeping that boundary here prevents route tests from
passing only because a fake exposes methods that the real object would not.
"""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy.exc import MultipleResultsFound, NoResultFound


def _first_column(row, index: int = 0):
    """Return the scalar represented by a SQLAlchemy row-like value."""
    if isinstance(row, (tuple, list)) or hasattr(row, "_mapping"):
        return row[index]
    return row


class FakeScalarResult:
    """The subset of ``ScalarResult`` used by model-service route tests."""

    def __init__(self, values=()):
        self._values = list(values)

    def all(self):
        return list(self._values)

    fetchall = all

    def first(self):
        return self._values[0] if self._values else None

    def one(self):
        if not self._values:
            raise NoResultFound("No row was found when one was required")
        if len(self._values) > 1:
            raise MultipleResultsFound("Multiple rows were found when one was required")
        return self._values[0]

    def one_or_none(self):
        if len(self._values) > 1:
            raise MultipleResultsFound("Multiple rows were found")
        return self._values[0] if self._values else None

    def unique(self, *_args, **_kwargs):
        return self

    def __iter__(self) -> Iterator:
        return iter(self._values)


class FakeResult:
    """The subset of ``Result`` used by model-service route tests.

    Stored values represent result rows.  ``all``/``first`` preserve those
    rows, while scalar accessors use the first column for tuple rows, matching
    SQLAlchemy's result contract closely enough for both ORM and projection
    queries.
    """

    def __init__(self, rows=()):
        self._rows = list(rows)

    def scalars(self, index: int = 0):
        return FakeScalarResult([_first_column(row, index) for row in self._rows])

    def all(self):
        return list(self._rows)

    fetchall = all

    def first(self):
        return self._rows[0] if self._rows else None

    fetchone = first

    def one(self):
        if not self._rows:
            raise NoResultFound("No row was found when one was required")
        if len(self._rows) > 1:
            raise MultipleResultsFound("Multiple rows were found when one was required")
        return self._rows[0]

    def one_or_none(self):
        if len(self._rows) > 1:
            raise MultipleResultsFound("Multiple rows were found")
        return self._rows[0] if self._rows else None

    def scalar(self):
        return _first_column(self._rows[0]) if self._rows else None

    def scalar_one(self):
        return _first_column(self.one())

    def scalar_one_or_none(self):
        row = self.one_or_none()
        return _first_column(row) if row is not None else None

    def unique(self, *_args, **_kwargs):
        return self

    def __iter__(self) -> Iterator:
        return iter(self._rows)
