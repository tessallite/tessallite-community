"""Bug-8323: the aggregate_set (custom-group) planner/evaluator must partition
over DETAIL rows only, never the merged (detail + subtotal/grand-total) set.

On a pivot that carries a subtotal hierarchy, ``rows`` is the merged result. A
subtotal row taken at/above the group's target level matches a group member but
has ``None`` in its finer OTHER-dimension columns. Partitioning over it emits a
spurious ``other_dim='None'`` partition — a wasted re-query that hits the source
DB (and re-faults) and paints a blank/mis-aggregated custom-group cell. Both the
planner (``plan_aggregate_requeried``) and the evaluator (``_eval_aggregate_set``)
must scope to detail rows so their partition keys stay in lock-step.
"""
from src.dax.mdx_calc_members import (
    CalcMember,
    plan_aggregate_requeried,
    evaluate_calc_members,
)
from src.dax.subtotal_engine import SUBTOTAL_LEVEL_KEY


def _calc():
    return CalcMember(
        name="Grp",
        expression="AGGREGATE({[City].[A], [City].[B]})",
        calc_type="aggregate_set",
        dim_name="City",
        aggregate_members=["A", "B"],
    )


def _merged_rows():
    """Two regions of detail plus one City-level subtotal row (Region=None)."""
    return [
        {"Region": "R1", "City": "A", "Amt": 10, SUBTOTAL_LEVEL_KEY: "detail"},
        {"Region": "R1", "City": "B", "Amt": 20, SUBTOTAL_LEVEL_KEY: "detail"},
        {"Region": "R2", "City": "A", "Amt": 30, SUBTOTAL_LEVEL_KEY: "detail"},
        # City subtotal across regions: matches member A, Region is None.
        {"Region": None, "City": "A", "Amt": 40, SUBTOTAL_LEVEL_KEY: "city_total"},
    ]


def test_planner_emits_no_none_partition_from_subtotal_rows():
    measures_meta = [{"name": "Amt", "default_agg": "avg"}]  # non-composable
    specs = plan_aggregate_requeried(
        [_calc()], measures_meta, "model", ["Region", "City"], _merged_rows()
    )
    # avg is non-composable -> one spec per DETAIL partition (R1, R2), never a
    # ('None',) partition sourced from the subtotal row.
    assert specs, "expected re-query specs for the non-composable custom group"
    for sp in specs:
        assert "None" not in [str(v) for v in sp.partition_values], (
            f"spurious None-valued partition spec emitted from a subtotal row: "
            f"{sp.partition_dims}={sp.partition_values}"
        )
    part_value_sets = sorted(tuple(sp.partition_values) for sp in specs)
    assert part_value_sets == [("R1",), ("R2",)]


def test_evaluator_adds_no_synthetic_group_row_for_none_partition():
    measures_meta = [{"name": "Amt", "default_agg": "avg"}]
    rows = _merged_rows()
    # Provide the per-partition re-query results the evaluator consumes for the
    # two REAL detail partitions. If the evaluator (wrongly) built a ('None',)
    # partition it would emit a third synthetic group row with a blank cell.
    requery_results = {
        ("Grp", "Amt", ("R1",)): 15.0,
        ("Grp", "Amt", ("R2",)): 30.0,
    }
    out = evaluate_calc_members(
        [_calc()], rows, ["Amt"], ["Region", "City"],
        measures_meta=measures_meta, requery_results=requery_results,
    )
    synth = [r for r in out if r.get("City") == "Grp"]
    # Exactly two synthetic group rows (one per detail region); none for Region=None.
    assert len(synth) == 2, f"expected 2 custom-group rows, got {synth}"
    assert all(r.get("Region") in ("R1", "R2") for r in synth), (
        f"a custom-group row was emitted for a subtotal (None) partition: {synth}"
    )


def test_flat_pivot_unaffected_no_subtotal_key():
    """A flat pivot (no subtotal rows / no SUBTOTAL_LEVEL_KEY) must behave exactly
    as before — the detail filter defaults every row to 'detail'."""
    measures_meta = [{"name": "Amt", "default_agg": "avg"}]
    flat_rows = [
        {"Region": "R1", "City": "A", "Amt": 10},
        {"Region": "R2", "City": "B", "Amt": 20},
    ]
    specs = plan_aggregate_requeried(
        [_calc()], measures_meta, "model", ["Region", "City"], flat_rows
    )
    part_value_sets = sorted(tuple(sp.partition_values) for sp in specs)
    assert part_value_sets == [("R1",), ("R2",)]
