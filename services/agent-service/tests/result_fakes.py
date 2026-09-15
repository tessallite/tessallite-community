"""Faithful SQLAlchemy scalar-result view for agent-service test doubles."""
from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy.exc import MultipleResultsFound, NoResultFound


class ScalarResult:
    """Small, copy-preserving subset of SQLAlchemy ``ScalarResult``.

    A test ``Result`` must return this separate view from ``scalars()``. Keeping
    the result-only methods on the outer fake is what lets tests catch a
    production caller that uses the wrong SQLAlchemy result boundary.
    """

    def __init__(self, rows: Iterable[object]):
        self._rows = list(rows)

    def all(self) -> list[object]:
        return list(self._rows)

    def first(self) -> object | None:
        return self._rows[0] if self._rows else None

    def one(self) -> object:
        if not self._rows:
            raise NoResultFound
        if len(self._rows) > 1:
            raise MultipleResultsFound
        return self._rows[0]

    def one_or_none(self) -> object | None:
        if len(self._rows) > 1:
            raise MultipleResultsFound
        return self._rows[0] if self._rows else None

    def unique(self) -> "ScalarResult":
        return self
