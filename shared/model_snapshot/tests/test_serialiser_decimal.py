"""Regression test for Bug-1028: snapshot version-save 500s on Decimal.

asyncpg returns NUMERIC columns (KPI ``target_value`` / ``trend_threshold``,
KPI ``presentation_meta`` band values, source-statistics ratios) as
``decimal.Decimal``. The version-save path (``POST .../versions`` →
``snapshot_model`` → JSONB ``snapshot_json``) serialises the snapshot with
stdlib ``json`` inside the asyncpg JSONB codec, which raises
``TypeError: Object of type Decimal is not JSON serializable`` → HTTP 500.

``_j()`` must coerce Decimal to float so that:

1. the snapshot is stdlib-json serialisable (version-save succeeds), and
2. the values restored from the stored JSON round-trip correctly — equal to
   the original NUMERIC values within float precision, matching the
   ``Mapped[Optional[float]]`` app-level contract of every consuming column.
"""
from __future__ import annotations

import json
import uuid
from decimal import Decimal

from shared.db.models import KPI
from shared.model_snapshot.serialiser import _j, _row_to_dict


def test_j_coerces_decimal_to_float():
    assert _j(Decimal("0.01")) == 0.01
    assert isinstance(_j(Decimal("0.01")), float)


def test_j_coerces_decimal_nested_in_lists_and_dicts():
    """Stats shapes: top_values frequencies (list) and KPI bands (dict)."""
    out = _j(
        {
            "bands": [
                {"min": Decimal("0.0"), "max": Decimal("0.75")},
                {"min": Decimal("0.75"), "max": Decimal("1.0")},
            ],
            "top_values": [{"value": "GOLD", "frequency": Decimal("0.4321")}],
        }
    )
    # json-serialisable — the exact failure mode of the JSONB write
    encoded = json.dumps(out)
    restored = json.loads(encoded)
    assert restored["bands"][0]["max"] == 0.75
    assert restored["top_values"][0]["frequency"] == 0.4321


def test_kpi_row_with_decimal_stats_is_json_serialisable_and_round_trips():
    """Build a snapshot row from a model object whose stats carry Decimal.

    Mirrors the reviewer's live reproduction on modely: a KPI whose NUMERIC
    columns come back from asyncpg as Decimal crashed the version-save JSONB
    write. After the fix, the row dict must serialise with stdlib json and
    the restored values must equal the originals.
    """
    kpi = KPI(
        id=uuid.uuid4(),
        model_id=uuid.uuid4(),
        name="probe_margin",
        target_value=Decimal("123456.789"),
        trend_threshold=Decimal("0.01"),
        presentation_meta={
            "bands": [{"min": Decimal("0.0"), "max": Decimal("0.5")}]
        },
    )

    row = _row_to_dict(kpi, exclude=("created_at", "updated_at"))

    # 1) version-save succeeds: stdlib json (the asyncpg JSONB serialiser)
    #    must accept the row without a Decimal TypeError.
    encoded = json.dumps(row)

    # 2) restored values are correct: round-trip through the stored JSON
    #    yields the same numbers the rehydrator will re-insert.
    restored = json.loads(encoded)
    assert restored["target_value"] == 123456.789
    assert restored["trend_threshold"] == 0.01
    assert restored["presentation_meta"]["bands"][0]["max"] == 0.5
    assert isinstance(restored["target_value"], float)
    assert restored["id"] == str(kpi.id)
