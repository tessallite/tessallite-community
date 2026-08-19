"""F-017-08: statistical KPI evaluation types must carry their data dependency
at SAVE, so the modeller gets a loud rejection instead of a silent grey
"No Data" badge on every surface.

- z_score needs snapshot_frequency (>= 2 snapshots to classify).
- percentile_rank needs presentation_meta.peer_dimension.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from shared.schemas.domains.governance_advanced import KPICreate, KPIUpdate

pytestmark = pytest.mark.unit


class TestZScoreRequiresSnapshotFrequency:
    def test_create_z_score_without_frequency_rejected(self):
        with pytest.raises(ValidationError, match="snapshot_frequency"):
            KPICreate(
                name="Anomaly",
                presentation_meta={"evaluation_type": "z_score"},
            )

    def test_create_z_score_with_frequency_ok(self):
        kpi = KPICreate(
            name="Anomaly",
            snapshot_frequency="daily",
            presentation_meta={"evaluation_type": "z_score"},
        )
        assert kpi.snapshot_frequency == "daily"

    def test_update_z_score_clearing_frequency_rejected(self):
        # Explicitly writing snapshot_frequency=None on a z_score KPI is rejected.
        with pytest.raises(ValidationError, match="snapshot_frequency"):
            KPIUpdate(
                snapshot_frequency=None,
                presentation_meta={"evaluation_type": "z_score"},
            )

    def test_update_z_score_without_touching_frequency_ok(self):
        # Partial update that does not write snapshot_frequency must pass — the
        # value may already live on the row.
        kpi = KPIUpdate(presentation_meta={"evaluation_type": "z_score"})
        assert kpi.presentation_meta["evaluation_type"] == "z_score"


class TestPercentileRequiresPeerDimension:
    def test_create_percentile_without_peer_dimension_rejected(self):
        with pytest.raises(ValidationError, match="peer_dimension"):
            KPICreate(
                name="Ranked",
                presentation_meta={"evaluation_type": "percentile_rank"},
            )

    def test_create_percentile_with_peer_dimension_ok(self):
        kpi = KPICreate(
            name="Ranked",
            presentation_meta={
                "evaluation_type": "percentile_rank",
                "peer_dimension": "region",
            },
        )
        assert kpi.presentation_meta["peer_dimension"] == "region"

    def test_blank_peer_dimension_rejected(self):
        with pytest.raises(ValidationError, match="peer_dimension"):
            KPICreate(
                name="Ranked",
                presentation_meta={
                    "evaluation_type": "percentile_rank",
                    "peer_dimension": "   ",
                },
            )

    def test_update_percentile_without_peer_dimension_rejected(self):
        with pytest.raises(ValidationError, match="peer_dimension"):
            KPIUpdate(presentation_meta={"evaluation_type": "percentile_rank"})


class TestNonStatisticalUnaffected:
    def test_percentage_of_target_needs_no_extras(self):
        kpi = KPICreate(
            name="Revenue",
            presentation_meta={"evaluation_type": "percentage_of_target"},
        )
        assert kpi.presentation_meta["evaluation_type"] == "percentage_of_target"

    def test_absolute_variance_needs_no_extras(self):
        kpi = KPICreate(
            name="Cost variance",
            presentation_meta={"evaluation_type": "absolute_variance"},
        )
        assert kpi.name == "Cost variance"

    def test_no_presentation_meta_ok(self):
        kpi = KPICreate(name="Plain")
        assert kpi.presentation_meta is None
