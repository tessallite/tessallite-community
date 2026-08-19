"""Bug-7412 / Bug-6141 — headless metadata surface honours column-level security.

The headless ``/measures`` and ``/dimensions`` listing endpoints must withhold a
measure/dimension whose column closure reaches a persona-CLS (data-tag) restricted
column, even when the persona allow-list INCLUDES it. Otherwise the restricted
object's name + description leak to a persona that cannot query it.

These tests assert the SECURITY DECISION at the helper boundary
(``cls_blocked_measure_and_dimension_ids``) and at the endpoint response:
a persona with a tag restriction on a column does NOT see the measure/dimension
built on that column, while an unrestricted persona does.
"""
from __future__ import annotations

import types
import uuid

import pytest


def _col_id() -> str:
    return str(uuid.uuid4())


def _make_measure(name: str, source_column_id: str | None):
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        name=name,
        display_name=name.title(),
        description=f"Measure {name}",
        source_column_id=uuid.UUID(source_column_id) if source_column_id else None,
        user_defined_attribute_id=None,
        variant_of_measure_id=None,
        measure_type="simple",
        expression=None,
    )


def _make_dimension(name: str, source_column_id: str | None, display_column_id: str | None = None):
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        name=name,
        display_name=name.title(),
        description=f"Dimension {name}",
        source_column_id=uuid.UUID(source_column_id) if source_column_id else None,
        display_column_id=uuid.UUID(display_column_id) if display_column_id else None,
        user_defined_attribute_id=None,
        variant_of_measure_id=None,
        measure_type=None,
        calc_expression=None,
    )


class _SequencedDB:
    """Minimal AsyncSession stub returning a scripted sequence of result sets.

    Each ``execute`` call pops the next scripted scalar list. The helper issues,
    in order: persona tag ids, restricted column ids, measures, dimensions, uda
    ids, restricted physical names. We script exactly that order.
    """

    def __init__(self, script: list[list]):
        self._script = list(script)

    async def execute(self, _stmt):
        values = self._script.pop(0) if self._script else []

        class _Result:
            def __init__(self, vals):
                self._vals = vals

            def scalars(self):
                return self

            def all(self):
                return self._vals

        return _Result(values)


@pytest.mark.asyncio
async def test_helper_blocks_measure_and_dimension_on_restricted_column():
    from src.security.cls_metadata import cls_blocked_measure_and_dimension_ids

    restricted_col = _col_id()
    clean_col = _col_id()

    revenue = _make_measure("revenue", clean_col)
    secret_headcount = _make_measure("secret_headcount", restricted_col)
    region = _make_dimension("region", clean_col)
    salary_band = _make_dimension("salary_band", restricted_col)

    persona = types.SimpleNamespace(id=uuid.uuid4())
    tag_id = uuid.uuid4()

    db = _SequencedDB([
        [tag_id],                       # PersonaTagRestriction.data_tag_id
        [uuid.UUID(restricted_col)],    # data_tag_columns.model_column_id
        [revenue, secret_headcount],    # Measure rows
        [region, salary_band],          # Dimension rows
        [],                             # UserDefinedAttributeColumnRef.attribute_id
        [],                             # _restricted_physical_names
    ])

    blocked_m, blocked_d = await cls_blocked_measure_and_dimension_ids(
        db, str(uuid.uuid4()), persona
    )

    assert str(secret_headcount.id) in blocked_m
    assert str(revenue.id) not in blocked_m
    assert str(salary_band.id) in blocked_d
    assert str(region.id) not in blocked_d


@pytest.mark.asyncio
async def test_helper_blocks_dimension_on_restricted_display_column():
    """Bug-7412 R2: a dimension whose KEY column is clean but whose separate
    DISPLAY column is restricted must still be withheld (its members' caption
    would leak the restricted value)."""
    from src.security.cls_metadata import cls_blocked_measure_and_dimension_ids

    restricted_display = _col_id()
    clean_key = _col_id()
    employee = _make_dimension(
        "employee", source_column_id=clean_key, display_column_id=restricted_display
    )

    persona = types.SimpleNamespace(id=uuid.uuid4())
    db = _SequencedDB([
        [uuid.uuid4()],                     # tag ids
        [uuid.UUID(restricted_display)],    # restricted column ids
        [],                                 # measures
        [employee],                         # dimensions
        [],                                 # uda ids
        [],                                 # restricted physical names
    ])

    _blocked_m, blocked_d = await cls_blocked_measure_and_dimension_ids(
        db, str(uuid.uuid4()), persona
    )
    assert str(employee.id) in blocked_d


@pytest.mark.asyncio
async def test_helper_no_restriction_returns_empty():
    from src.security.cls_metadata import cls_blocked_measure_and_dimension_ids

    # persona=None -> no CLS in force.
    blocked_m, blocked_d = await cls_blocked_measure_and_dimension_ids(
        _SequencedDB([]), str(uuid.uuid4()), None
    )
    assert blocked_m == frozenset()
    assert blocked_d == frozenset()

    # persona present but no tag restrictions -> nothing blocked.
    persona = types.SimpleNamespace(id=uuid.uuid4())
    db = _SequencedDB([[]])  # empty PersonaTagRestriction result
    blocked_m, blocked_d = await cls_blocked_measure_and_dimension_ids(
        db, str(uuid.uuid4()), persona
    )
    assert blocked_m == frozenset()
    assert blocked_d == frozenset()
