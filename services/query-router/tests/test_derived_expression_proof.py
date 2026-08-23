"""T0 tests for the derived-grain PROOF engine + shadow evaluator (Phase 4).

Spec: architecture_derived-grain-aggregate-routing.md §7 (proof algorithm), §7.7
(measure proof), §14.2 (known-value adversarial guards), §14.4 (clause/failure).
These pin the CORRECTNESS decisions that keep the shadow proof from ever declaring
a wrong result servable — before any serving flag exists. The engine is pure
(no DB, no SQL), so these are deterministic unit tests.

Mandatory §14.2 adversarial guards covered:
  - filtered-key quantile merge is NOT proven servable (the pooled 51.5 stays
    source; 50.75 never appears) — verdict must be SOURCE_ONLY;
  - week-boundary is not a contiguous coarsening (month key can't serve a week
    query; week key can't serve month/quarter/year);
  - EXTRACT month/year-collapse is source-only (no coarsening theorem);
  - N:1 collation-fold / NULL endpoint fail closed via the trust predicate;
  - two-ids->one-detail strict edge is never EXACT; same rows N:1 -> ROLLUP.
"""
from __future__ import annotations

import uuid

import pytest

from src.routing.derived_expression_proof import (
    EXACT,
    ROLLUP,
    SOURCE_ONLY,
    CandidateManifest,
    MeasureRequest,
    QueryKeyRequest,
    build_serve_proof,
)
from src.routing.derived_measure_proof import (
    AVG_FROM_SUM_COUNT,
    DIRECT,
    SOURCE_ONLY as M_SOURCE_ONLY,
    SUM_OF_SUM,
    classify_measure_rollup,
)
from src.routing.derived_shadow import (
    DERIVED_ERROR,
    MATCH,
    MISMATCH,
    compare_results,
    result_hash,
    should_sample,
)
from src.routing.derived_time_theorems import coarsen_edge, extract_is_coarsenable
from src.routing.derived_trust_predicate import TrustInputs, evaluate_trust

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Time-coarsening theorems (§7.5, pitfall 23)
# ---------------------------------------------------------------------------


def test_month_to_year_and_quarter_admissible():
    assert coarsen_edge(finer_unit="month", coarse_unit="year").admissible
    assert coarsen_edge(finer_unit="month", coarse_unit="quarter").admissible
    assert coarsen_edge(finer_unit="day", coarse_unit="month").admissible


def test_week_boundary_not_a_coarsening_either_direction():
    # A month key must NOT serve a week query, and a week key must NOT serve
    # month/quarter/year (2025-01-29 & 2025-02-02 share ISO week 2025-01-27).
    assert not coarsen_edge(finer_unit="month", coarse_unit="week").admissible
    assert not coarsen_edge(finer_unit="week", coarse_unit="month").admissible
    assert not coarsen_edge(finer_unit="week", coarse_unit="quarter").admissible
    assert not coarsen_edge(finer_unit="week", coarse_unit="year").admissible


def test_week_from_day_requires_pinned_week_start():
    assert not coarsen_edge(finer_unit="day", coarse_unit="week").admissible
    assert coarsen_edge(finer_unit="day", coarse_unit="week", week_start_pinned=True).admissible


def test_extract_is_never_a_coarsening_target():
    # EXTRACT(month/year) collapse class is structurally unreachable (§7.5).
    assert extract_is_coarsenable("month") is False
    assert extract_is_coarsenable("year") is False


# ---------------------------------------------------------------------------
# Measure roll-up recipes (§7.7, I4, pitfall 9)
# ---------------------------------------------------------------------------


def test_additive_measures_roll_up():
    assert classify_measure_rollup(measure_name="a", requested_stat="sum", key_verdict="ROLLUP").plan == SUM_OF_SUM
    assert classify_measure_rollup(measure_name="a", requested_stat="avg", key_verdict="ROLLUP").plan == AVG_FROM_SUM_COUNT


def test_quantile_direct_only_at_exact_else_source():
    assert classify_measure_rollup(measure_name="a", requested_stat="p50", key_verdict="EXACT").plan == DIRECT
    src = classify_measure_rollup(measure_name="a", requested_stat="p50", key_verdict="ROLLUP")
    assert src.plan == M_SOURCE_ONLY
    assert src.reason == "DERIVED_MEASURE_NOT_ROLLUP_SAFE"


def test_dispersion_and_distinct_direct_only():
    assert classify_measure_rollup(measure_name="a", requested_stat="stddev_samp", key_verdict="ROLLUP").plan == M_SOURCE_ONLY
    assert classify_measure_rollup(measure_name="a", requested_stat="count_distinct", key_verdict="ROLLUP").plan == M_SOURCE_ONLY


def test_unknown_statistic_never_defaults_to_sum():
    mp = classify_measure_rollup(measure_name="a", requested_stat="bogus_udf", key_verdict="EXACT")
    assert mp.plan == M_SOURCE_ONLY  # pitfall 9


def test_avg_requires_both_components_when_supplied():
    got = classify_measure_rollup(
        measure_name="a", requested_stat="avg", key_verdict="ROLLUP",
        available_components=frozenset({"a__sum"}),  # missing a__count
    )
    assert got.plan == M_SOURCE_ONLY


# ---------------------------------------------------------------------------
# Proof engine — verdicts + §14.2 adversarial guards
# ---------------------------------------------------------------------------


# Stable ids for the canonical-key-id cover vocabulary (spec §2.1). PHYSICAL keys
# use ``dim:<uuid>`` + ``(source_column_id,)``; the proof matches by key_id +
# ordered lineage, never fingerprint (which is the EXPRESSION-only index).
_DIM_COUNTRY = "dim:d-country"
_COL_COUNTRY = "c-country"
_DIM_PRODUCT = "dim:d-product"
_COL_PRODUCT = "c-product"
_DIM_REGION = "dim:d-region"
_COL_REGION = "c-region"


def _phys_grain(key_id: str, col_id: str):
    """One PHYSICAL grain-key index entry for the canonical-id vocabulary."""
    return dict(
        ids={key_id}, lineage={key_id: (col_id,)},
        source_dim={key_id: key_id.split(":", 1)[1]},
    )


def _active_candidate(**overrides):
    """Build an active candidate on the canonical-id vocabulary (spec §2.1).

    ``phys_keys`` (list of (key_id, col_id)) is the default way to declare the
    artifact's PHYSICAL grain keys; tests that exercise EXPRESSION coarsening pass
    ``grain_key_fingerprints`` + ``grain_key_units`` + ``grain_key_lineage`` +
    ``grain_key_ids`` explicitly.
    """
    phys_keys = overrides.pop("phys_keys", [(_DIM_COUNTRY, _COL_COUNTRY)])
    ids: set[str] = set()
    lineage_by_id: dict = {}
    source_dim: dict = {}
    for kid, cid in phys_keys:
        ids.add(kid)
        lineage_by_id[kid] = (cid,)
        source_dim[kid] = kid.split(":", 1)[1]
    base = dict(
        artifact_id="agg-1", is_active=True, is_stale=False,
        active_refresh_run_id=uuid.uuid4(), manifest_hash="mh1",
        grain_key_fingerprints=frozenset(),
        grain_key_units={}, attribute_edges=[], measure_components=frozenset(),
        trust_by_relationship={}, week_start_pinned=False,
        grain_key_ids=frozenset(ids),
        grain_key_lineage_by_id=lineage_by_id,
        grain_key_source_dim_by_id=source_dim,
    )
    base.update(overrides)
    # If a test overrode grain_key_ids explicitly, honour it; otherwise keep the
    # phys-derived ids computed above.
    return CandidateManifest(**base)


def test_exact_identity_direct_read():
    cand = _active_candidate()
    proof = build_serve_proof(
        query_keys=[QueryKeyRequest(
            logical_name="country_id", kind="PHYSICAL",
            key_id=_DIM_COUNTRY, input_column_ids=(_COL_COUNTRY,),
        )],
        measures=[MeasureRequest("amount", "sum")],
        candidate=cand,
    )
    assert proof.verdict == EXACT
    assert proof.query_key_plans[0].plan == "DIRECT_PHYSICAL_KEY"
    assert proof.query_key_plans[0].artifact_key_id == _DIM_COUNTRY
    assert proof.measure_plans[0].plan == SUM_OF_SUM


def test_missing_key_is_source_only():
    cand = _active_candidate(phys_keys=[("dim:other", "c-other")])
    proof = build_serve_proof(
        query_keys=[QueryKeyRequest(
            logical_name="country_id", kind="PHYSICAL",
            key_id=_DIM_COUNTRY, input_column_ids=(_COL_COUNTRY,),
        )],
        measures=[MeasureRequest("amount", "sum")],
        candidate=cand,
    )
    assert proof.verdict == SOURCE_ONLY
    assert "DERIVED_INPUT_KEY_MISSING" in proof.reason_codes


def test_duplicate_query_key_id_does_not_fake_exact():
    # DANGER (R1 finding 1): §2.1 cover is a SET cover of canonical key ids, not a
    # count. Two query group keys resolving to the SAME artifact key (dim country)
    # while the artifact ALSO keys on product must leave product uncovered -> ROLLUP
    # -> a direct-only quantile refuses (never fakes EXACT on a count match).
    cand = _active_candidate(phys_keys=[(_DIM_COUNTRY, _COL_COUNTRY), (_DIM_PRODUCT, _COL_PRODUCT)])
    proof = build_serve_proof(
        query_keys=[
            QueryKeyRequest(logical_name="k1", kind="PHYSICAL", key_id=_DIM_COUNTRY,
                            input_column_ids=(_COL_COUNTRY,), in_group_by=True),
            QueryKeyRequest(logical_name="k1_dup", kind="PHYSICAL", key_id=_DIM_COUNTRY,
                            input_column_ids=(_COL_COUNTRY,), in_group_by=True),
        ],
        measures=[MeasureRequest("latency", "p50")],
        candidate=cand,
    )
    assert proof.verdict == SOURCE_ONLY  # product uncovered -> not EXACT -> p50 refuses


def _expr_grain_overrides(fp: str, unit: str, leaf: str) -> dict:
    """Overrides for an EXPRESSION grain key on the canonical-id vocabulary.

    An EXPRESSION artifact key's canonical id is ``expr:<fp>``. The coarsening path
    reads ``grain_key_units`` + ``grain_key_lineage`` (fingerprint-keyed), while the
    §2.1 cover reads ``grain_key_ids`` / ``grain_key_lineage_by_id`` (id-keyed).
    """
    key_id = f"expr:{fp}"
    return dict(
        grain_key_fingerprints=frozenset({fp}),
        grain_key_units={fp: unit},
        grain_key_lineage={fp: (leaf,)},
        grain_key_ids=frozenset({key_id}),
        grain_key_id_by_fingerprint={fp: key_id},
        grain_key_lineage_by_id={key_id: (leaf,)},
    )


def test_time_coarsening_month_key_serves_year_query_rollup():
    # The stored month key and the query year expression share the SAME leaf column
    # (order_ts, id "col_order_ts") -> a valid coarsening.
    cand = _active_candidate(**_expr_grain_overrides("fp_month", "month", "col_order_ts"))
    proof = build_serve_proof(
        query_keys=[QueryKeyRequest(
            logical_name="order_year", kind="EXPRESSION", fingerprint="fp_year",
            time_unit="year", input_column_ids=("col_order_ts",),
        )],
        measures=[MeasureRequest("amount", "sum")],
        candidate=cand,
    )
    assert proof.verdict == ROLLUP
    assert proof.measure_plans[0].plan == SUM_OF_SUM


def test_week_query_from_month_key_is_source_only():
    cand = _active_candidate(**_expr_grain_overrides("fp_month", "month", "col_order_ts"))
    proof = build_serve_proof(
        query_keys=[QueryKeyRequest(
            logical_name="order_week", kind="EXPRESSION", fingerprint="fp_week",
            time_unit="week", input_column_ids=("col_order_ts",),
        )],
        measures=[MeasureRequest("amount", "sum")],
        candidate=cand,
    )
    assert proof.verdict == SOURCE_ONLY


def test_cross_column_time_coarsening_is_refused():
    # DANGER (R1 finding 2): DATE_TRUNC('year', order_ts) must NOT be derived from a
    # stored DATE_TRUNC('month', ship_ts) key — that computes year-of-order from
    # month-of-ship, a wrong-number merge. Different leaf column -> SOURCE_ONLY.
    cand = _active_candidate(**_expr_grain_overrides("fp_month_ship", "month", "col_ship_ts"))
    proof = build_serve_proof(
        query_keys=[QueryKeyRequest(
            logical_name="order_year", kind="EXPRESSION", fingerprint="fp_year_order",
            time_unit="year", input_column_ids=("col_order_ts",),  # DIFFERENT column
        )],
        measures=[MeasureRequest("amount", "sum")],
        candidate=cand,
    )
    assert proof.verdict == SOURCE_ONLY


def test_time_coarsening_without_lineage_is_refused():
    # A query time key with no asserted leaf lineage cannot prove a coarsening
    # (fail-closed): the engine must not derive from a stored key on unit alone.
    cand = _active_candidate(**_expr_grain_overrides("fp_month", "month", "col_order_ts"))
    proof = build_serve_proof(
        query_keys=[QueryKeyRequest(
            logical_name="order_year", kind="EXPRESSION", fingerprint="fp_year",
            time_unit="year",  # no input_column_ids
        )],
        measures=[MeasureRequest("amount", "sum")],
        candidate=cand,
    )
    assert proof.verdict == SOURCE_ONLY


def test_filtered_key_quantile_merge_not_servable():
    # §14.2 adversarial: an aggregate keyed on country_id carrying stored P50; the
    # query filters country_id IN (1,2) with NO GROUP BY country_id and requests
    # P50. The filter must NOT consume the key -> the artifact still has an extra
    # independent key -> ROLLUP -> a direct-only quantile is SOURCE_ONLY (never
    # merges the stored per-id medians into 50.75).
    cand = _active_candidate()  # artifact keyed on dim country only
    proof = build_serve_proof(
        # country_id appears ONLY as a filter (in_group_by=False); the sole group
        # key is a different, un-materialised dimension -> not exact on it.
        query_keys=[
            QueryKeyRequest(logical_name="country_id", kind="PHYSICAL", key_id=_DIM_COUNTRY,
                            input_column_ids=(_COL_COUNTRY,), in_group_by=False),
            QueryKeyRequest(logical_name="region", kind="PHYSICAL", key_id=_DIM_REGION,
                            input_column_ids=(_COL_REGION,), in_group_by=True),
        ],
        measures=[MeasureRequest("latency", "p50")],
        candidate=cand,
    )
    assert proof.verdict == SOURCE_ONLY  # 50.75 can never be produced


def test_filtered_extra_key_forces_rollup_not_exact():
    # Group by country_id (exact) BUT the artifact also keys on product_id which is
    # filtered by equality (not grouped). The extra artifact key remains -> ROLLUP,
    # so a direct-only quantile refuses (pitfall 24 / F-004-06).
    cand = _active_candidate(phys_keys=[(_DIM_COUNTRY, _COL_COUNTRY), (_DIM_PRODUCT, _COL_PRODUCT)])
    proof = build_serve_proof(
        query_keys=[
            QueryKeyRequest(logical_name="country_id", kind="PHYSICAL", key_id=_DIM_COUNTRY,
                            input_column_ids=(_COL_COUNTRY,), in_group_by=True),
            QueryKeyRequest(logical_name="product_id", kind="PHYSICAL", key_id=_DIM_PRODUCT,
                            input_column_ids=(_COL_PRODUCT,), in_group_by=False),
        ],
        measures=[MeasureRequest("latency", "p50")],
        candidate=cand,
    )
    # sum would roll up, but p50 is direct-only and the extra key makes it ROLLUP.
    assert proof.verdict == SOURCE_ONLY


# ---------------------------------------------------------------------------
# Attribute-edge proof via the trust predicate (§7.6.4, §14.2)
# ---------------------------------------------------------------------------


class _Ev:
    def __init__(self, **kw):
        self.id = kw.get("id", uuid.uuid4())
        self.status = kw.get("status", "VERIFIED")
        self.declaration_hash = kw.get("declaration_hash", "dh1")
        self.verifier_version = kw.get("verifier_version", "v0")
        self.deployed_version_id = kw.get("deployed_version_id")
        self.deploy_epoch = kw.get("deploy_epoch", 1)
        self.artifact_refresh_run_id = kw.get("artifact_refresh_run_id")
        self.artifact_manifest_hash = kw.get("artifact_manifest_hash", "mh1")
        self.source_data_version = kw.get("source_data_version")
        self.violation_count = kw.get("violation_count", 0)


# Canonical attribute identity for the relabel edge (§2.4). The edge sits beside
# the dim-country grain key, so its ``key_grain_key_id`` == that key's key_id.
_ATTR_KEY = "attr:r1"


def _trust_inputs(cardinality, run_id, dep_ver, key_id=_DIM_COUNTRY, **ev_over):
    ev = _Ev(artifact_refresh_run_id=run_id, deployed_version_id=dep_ver, **ev_over)
    edge = {
        "relationship_id": "r1",
        "attribute_key": _ATTR_KEY,
        "detail_passenger_column": "country_name__passenger",
        "cardinality": cardinality,
        "declaration_hash": "dh1",
    }
    if key_id is not None:
        # The canonical id of the grain key the edge sits beside (§2.4 set-cover).
        edge["key_grain_key_id"] = key_id
    return TrustInputs(
        declaration_enabled=True, deployed_declaration_hash="dh1", evidence=ev,
        accepted_verifier_version="v0", artifact_active_refresh_run_id=run_id,
        artifact_manifest_hash="mh1", artifact_is_active=True, artifact_is_stale=False,
        bound_deployed_version_id=dep_ver, bound_deploy_epoch=1,
        manifest_edge=edge,
        # Bug-7905: these proof tests model fresh, currently-servable health evidence
        # (checked_at within the model's sweep-cadence age bound). The expired-evidence
        # rejection is covered by a dedicated predicate test.
        evidence_age_ok=True,
    )


# The relabel endpoints: key column == the dim-country grain key's lineage column
# (_COL_COUNTRY); detail column == the passenger's source column.
_DETAIL_COL = "c-country-name"


def _edge_indices(ti):
    """Build edge_by_attribute_key + passenger_by_attribute_key from a trust input."""
    edge = ti.manifest_edge
    ak = edge.get("attribute_key")
    return (
        {ak: edge},
        {ak: {
            "passenger_column": edge.get("detail_passenger_column"),
            "attribute_key": ak,
            "source_column_id": _DETAIL_COL,  # == the relabel detail column
        }},
    )


def _attr_request():
    return QueryKeyRequest(
        logical_name="country_name", kind="ATTRIBUTE",
        relationship_id="r1", attribute_key=_ATTR_KEY,
        input_column_ids=(_COL_COUNTRY, _DETAIL_COL),  # (key_column, detail_column)
        declaration_hash="dh1",
        owning_dimension_id=_DIM_COUNTRY.split(":", 1)[1],  # == the edge's dim key
    )


def test_verified_bijection_relabel_is_exact():
    run_id, dep_ver = uuid.uuid4(), uuid.uuid4()
    ti = _trust_inputs("BIJECTION", run_id, dep_ver)
    e_idx, p_idx = _edge_indices(ti)
    cand = _active_candidate(
        active_refresh_run_id=run_id, manifest_hash="mh1",
        trust_by_relationship={"r1": ti},
        edge_by_attribute_key=e_idx, passenger_by_attribute_key=p_idx,
    )
    proof = build_serve_proof(
        query_keys=[_attr_request()],
        measures=[MeasureRequest("amount", "sum")],
        candidate=cand,
    )
    assert proof.verdict == EXACT
    assert proof.query_key_plans[0].plan == "DIRECT_ATTRIBUTE_RELABEL"
    assert proof.query_key_plans[0].artifact_key_id == _DIM_COUNTRY


def test_bijection_relabel_with_extra_dropped_key_is_rollup_not_exact():
    # §7.7 last paragraph: a verified bijection relabel COMBINED WITH another
    # artifact key being aggregated away is a coarsening — the tuple partition is
    # no longer preserved — so it must be ROLLUP and a stored quantile must refuse
    # (never opportunistically stay EXACT just because one key relabels 1:1).
    run_id, dep_ver = uuid.uuid4(), uuid.uuid4()
    ti = _trust_inputs("BIJECTION", run_id, dep_ver)
    e_idx, p_idx = _edge_indices(ti)
    cand = _active_candidate(
        active_refresh_run_id=run_id, manifest_hash="mh1",
        # Artifact keyed on TWO columns; the query groups only by the relabel key,
        # so product_id is an extra independent key aggregated away.
        phys_keys=[(_DIM_COUNTRY, _COL_COUNTRY), (_DIM_PRODUCT, _COL_PRODUCT)],
        trust_by_relationship={"r1": ti},
        edge_by_attribute_key=e_idx, passenger_by_attribute_key=p_idx,
    )
    sum_proof = build_serve_proof(
        query_keys=[_attr_request()],
        measures=[MeasureRequest("amount", "sum")],
        candidate=cand,
    )
    assert sum_proof.verdict == ROLLUP  # extra dropped key => coarsening
    p50_proof = build_serve_proof(
        query_keys=[_attr_request()],
        measures=[MeasureRequest("latency", "p50")],
        candidate=cand,
    )
    assert p50_proof.verdict == SOURCE_ONLY  # direct-only quantile refuses at ROLLUP


def test_relabel_without_key_grain_id_is_not_exact():
    # Conservative fallback: a bijection relabel whose manifest edge omits
    # key_grain_key_id cannot prove it covers the artifact key -> NOT EXACT
    # (ROLLUP), so a direct-only quantile refuses. Never a false EXACT.
    run_id, dep_ver = uuid.uuid4(), uuid.uuid4()
    ti = _trust_inputs("BIJECTION", run_id, dep_ver, key_id=None)  # no key grain id
    e_idx, p_idx = _edge_indices(ti)
    cand = _active_candidate(
        active_refresh_run_id=run_id, manifest_hash="mh1",
        trust_by_relationship={"r1": ti},
        edge_by_attribute_key=e_idx, passenger_by_attribute_key=p_idx,
    )
    p50_proof = build_serve_proof(
        query_keys=[_attr_request()],
        measures=[MeasureRequest("latency", "p50")],
        candidate=cand,
    )
    assert p50_proof.verdict == SOURCE_ONLY  # uncovered artifact key -> ROLLUP -> refuse


def test_same_rows_n_to_1_is_rollup_not_exact():
    # I14: a verified N:1 edge over the SAME rows is a real coarsening -> ROLLUP,
    # never opportunistically upgraded to EXACT. A quantile over it must refuse.
    run_id, dep_ver = uuid.uuid4(), uuid.uuid4()
    ti = _trust_inputs("FUNCTIONAL_N_TO_1", run_id, dep_ver)
    e_idx, p_idx = _edge_indices(ti)
    cand = _active_candidate(
        active_refresh_run_id=run_id, manifest_hash="mh1",
        trust_by_relationship={"r1": ti},
        edge_by_attribute_key=e_idx, passenger_by_attribute_key=p_idx,
    )
    sum_proof = build_serve_proof(
        query_keys=[_attr_request()],
        measures=[MeasureRequest("amount", "sum")],
        candidate=cand,
    )
    assert sum_proof.verdict == ROLLUP
    p50_proof = build_serve_proof(
        query_keys=[_attr_request()],
        measures=[MeasureRequest("latency", "p50")],
        candidate=cand,
    )
    assert p50_proof.verdict == SOURCE_ONLY


def test_broken_edge_null_endpoint_fails_closed():
    # NULL endpoint (violation_count != 0) -> trust predicate rejects -> SOURCE_ONLY.
    run_id, dep_ver = uuid.uuid4(), uuid.uuid4()
    ti = _trust_inputs("FUNCTIONAL_N_TO_1", run_id, dep_ver, violation_count=1)
    e_idx, p_idx = _edge_indices(ti)
    cand = _active_candidate(
        active_refresh_run_id=run_id, trust_by_relationship={"r1": ti},
        edge_by_attribute_key=e_idx, passenger_by_attribute_key=p_idx,
    )
    proof = build_serve_proof(
        query_keys=[_attr_request()],
        measures=[MeasureRequest("amount", "sum")],
        candidate=cand,
    )
    assert proof.verdict == SOURCE_ONLY
    assert "ATTRIBUTE_RELATIONSHIP_NULL_ENDPOINT" in proof.reason_codes


def test_broken_status_evidence_rejects_relabel_source_only():
    # Operational-serving item 4: when the periodic sweep marks a relationship
    # BROKEN (its served-data 1:1 was violated), the newest evidence status is
    # BROKEN. The trust predicate must REJECT the relabel (rule 2) so the proof is
    # SOURCE_ONLY — no relabel substitution — and the router then continues to
    # ORDINARY aggregate/source routing (never a forced raw source, never a wrong
    # number). A BROKEN status must not serve.
    run_id, dep_ver = uuid.uuid4(), uuid.uuid4()
    ti = _trust_inputs("BIJECTION", run_id, dep_ver, status="BROKEN")
    e_idx, p_idx = _edge_indices(ti)
    cand = _active_candidate(
        active_refresh_run_id=run_id, manifest_hash="mh1",
        trust_by_relationship={"r1": ti},
        edge_by_attribute_key=e_idx, passenger_by_attribute_key=p_idx,
    )
    proof = build_serve_proof(
        query_keys=[_attr_request()],
        measures=[MeasureRequest("amount", "sum")],
        candidate=cand,
    )
    assert proof.verdict == SOURCE_ONLY  # relabel refused -> ordinary routing
    assert "ATTRIBUTE_RELATIONSHIP_BROKEN" in proof.reason_codes


def test_stale_status_evidence_rejects_relabel_source_only():
    # A STALE sweep result (declaration superseded / edge no longer servable) is
    # likewise never served: the trust predicate admits only VERIFIED.
    run_id, dep_ver = uuid.uuid4(), uuid.uuid4()
    ti = _trust_inputs("BIJECTION", run_id, dep_ver, status="STALE")
    e_idx, p_idx = _edge_indices(ti)
    cand = _active_candidate(
        active_refresh_run_id=run_id, manifest_hash="mh1",
        trust_by_relationship={"r1": ti},
        edge_by_attribute_key=e_idx, passenger_by_attribute_key=p_idx,
    )
    proof = build_serve_proof(
        query_keys=[_attr_request()],
        measures=[MeasureRequest("amount", "sum")],
        candidate=cand,
    )
    assert proof.verdict == SOURCE_ONLY


def test_stale_active_run_mismatch_fails_closed():
    # Evidence names a DIFFERENT run than the artifact's active run -> reject.
    dep_ver = uuid.uuid4()
    ti = _trust_inputs("BIJECTION", uuid.uuid4(), dep_ver)  # ev run
    e_idx, p_idx = _edge_indices(ti)
    cand = _active_candidate(
        active_refresh_run_id=uuid.uuid4(),  # different active run
        trust_by_relationship={"r1": ti},
        edge_by_attribute_key=e_idx, passenger_by_attribute_key=p_idx,
    )
    # Re-point the trust inputs' artifact run to the candidate's (as the caller
    # would), but leave evidence's run mismatched.
    ti.artifact_active_refresh_run_id = cand.active_refresh_run_id
    proof = build_serve_proof(
        query_keys=[_attr_request()],
        measures=[MeasureRequest("amount", "sum")],
        candidate=cand,
    )
    assert proof.verdict == SOURCE_ONLY
    assert "ATTRIBUTE_ACTIVE_REFRESH_MISMATCH" in proof.reason_codes


def test_stale_artifact_rejected():
    cand = _active_candidate(is_stale=True)
    proof = build_serve_proof(
        query_keys=[QueryKeyRequest(
            logical_name="country_id", kind="PHYSICAL",
            key_id=_DIM_COUNTRY, input_column_ids=(_COL_COUNTRY,),
        )],
        measures=[MeasureRequest("amount", "sum")],
        candidate=cand,
    )
    assert proof.verdict == SOURCE_ONLY


def test_one_unproved_key_rejects_whole_candidate():
    # §7.8: a mixed tuple where one key is unproved rejects the candidate.
    cand = _active_candidate()  # keyed on dim country only
    proof = build_serve_proof(
        query_keys=[
            QueryKeyRequest(logical_name="country_id", kind="PHYSICAL", key_id=_DIM_COUNTRY,
                            input_column_ids=(_COL_COUNTRY,), in_group_by=True),
            QueryKeyRequest(logical_name="product_id", kind="PHYSICAL", key_id=_DIM_PRODUCT,
                            input_column_ids=(_COL_PRODUCT,), in_group_by=True),
        ],
        measures=[MeasureRequest("amount", "sum")],
        candidate=cand,
    )
    assert proof.verdict == SOURCE_ONLY


# ---------------------------------------------------------------------------
# Shadow evaluator (§20 Q10)
# ---------------------------------------------------------------------------


def test_shadow_hash_order_insensitive_duplicate_preserving():
    assert result_hash([{"a": 1}, {"a": 2}]) == result_hash([{"a": 2}, {"a": 1}])
    # A dropped duplicate changes the hash.
    assert result_hash([{"a": 1}, {"a": 1}]) != result_hash([{"a": 1}])


def test_shadow_hash_type_sensitive():
    assert result_hash([{"a": 1}]) != result_hash([{"a": "1"}])
    assert result_hash([{"a": None}]) != result_hash([{"a": ""}])
    assert result_hash([{"a": True}]) != result_hash([{"a": 1}])


def test_shadow_match_and_mismatch():
    src = [{"country": "US", "amt": 42}, {"country": None, "amt": 3}]
    same = [{"amt": 42, "country": "US"}, {"amt": 3, "country": None}]
    rec = compare_results(artifact_id="agg-1", verdict="ROLLUP", source_rows=src, derived_rows=same)
    assert rec.outcome == MATCH
    diff = [{"country": "US", "amt": 51}, {"country": None, "amt": 3}]  # merged/wrong
    rec2 = compare_results(artifact_id="agg-1", verdict="ROLLUP", source_rows=src, derived_rows=diff)
    assert rec2.outcome == MISMATCH


def test_shadow_derived_error_never_false_match():
    src = [{"a": 1}]
    rec = compare_results(artifact_id="agg-1", verdict="EXACT", source_rows=src, derived_rows=None)
    assert rec.outcome == DERIVED_ERROR
    assert rec.derived_hash is None


def test_shadow_sample_rate_gate():
    assert should_sample(0.0, 0.5) is False   # 0 disables
    assert should_sample(1.0, 0.99) is True    # 1 always
    assert should_sample(0.5, 0.4) is True
    assert should_sample(0.5, 0.6) is False


# ---------------------------------------------------------------------------
# Fable R1 hardening: ATTRIBUTE endpoint/declaration guards + manifest poison.
# ---------------------------------------------------------------------------


def test_attribute_declaration_hash_mismatch_fails_closed():
    # A post-deploy relationship edit changes the declaration hash; the bound
    # relabel (old hash) must NOT serve over the rebuilt edge (new hash).
    run_id, dep_ver = uuid.uuid4(), uuid.uuid4()
    ti = _trust_inputs("BIJECTION", run_id, dep_ver)  # edge declaration_hash="dh1"
    e_idx, p_idx = _edge_indices(ti)
    cand = _active_candidate(
        active_refresh_run_id=run_id, manifest_hash="mh1",
        trust_by_relationship={"r1": ti},
        edge_by_attribute_key=e_idx, passenger_by_attribute_key=p_idx,
    )
    req = _attr_request()
    req.declaration_hash = "OLD-HASH"  # binder bound an older declaration
    proof = build_serve_proof(
        query_keys=[req], measures=[MeasureRequest("amount", "sum")], candidate=cand,
    )
    assert proof.verdict == SOURCE_ONLY
    assert "ATTRIBUTE_DECLARATION_HASH_MISMATCH" in proof.reason_codes


def test_attribute_endpoint_lineage_mismatch_fails_closed():
    # The edge's covered grain key does NOT carry the relationship key column ->
    # the relabel would serve a different column's values under the label -> reject.
    run_id, dep_ver = uuid.uuid4(), uuid.uuid4()
    ti = _trust_inputs("BIJECTION", run_id, dep_ver)
    e_idx, p_idx = _edge_indices(ti)
    cand = _active_candidate(
        active_refresh_run_id=run_id, manifest_hash="mh1",
        trust_by_relationship={"r1": ti},
        edge_by_attribute_key=e_idx, passenger_by_attribute_key=p_idx,
    )
    req = _attr_request()
    # The request's key column no longer equals the grain key's stored lineage.
    req.input_column_ids = ("WRONG-KEY-COL", _DETAIL_COL)
    proof = build_serve_proof(
        query_keys=[req], measures=[MeasureRequest("amount", "sum")], candidate=cand,
    )
    assert proof.verdict == SOURCE_ONLY
    assert "ATTRIBUTE_LINEAGE_MISMATCH" in proof.reason_codes


def test_forged_attribute_key_for_other_relationship_fails_closed():
    run_id, dep_ver = uuid.uuid4(), uuid.uuid4()
    ti = _trust_inputs("BIJECTION", run_id, dep_ver)
    e_idx, p_idx = _edge_indices(ti)
    cand = _active_candidate(
        active_refresh_run_id=run_id, manifest_hash="mh1",
        trust_by_relationship={"r1": ti},
        edge_by_attribute_key=e_idx, passenger_by_attribute_key=p_idx,
    )
    req = _attr_request()
    req.attribute_key = "attr:DIFFERENT-REL"  # encodes a different relationship
    proof = build_serve_proof(
        query_keys=[req], measures=[MeasureRequest("amount", "sum")], candidate=cand,
    )
    assert proof.verdict == SOURCE_ONLY


def test_bijection_relabel_from_expr_key_is_rejected():
    # Fable R2 #2: a bijection relabel whose edge points at an expr:<fp> grain key
    # (a transformed partition) — even when that key's single leaf IS the key
    # column — must NOT serve; the covered key must be the owning dimension's
    # dim:<uuid> PHYSICAL key.
    run_id, dep_ver = uuid.uuid4(), uuid.uuid4()
    # Edge points at an expr: key instead of dim:d-country.
    ti = _trust_inputs("BIJECTION", run_id, dep_ver, key_id="expr:some-fp")
    e_idx, p_idx = _edge_indices(ti)
    cand = _active_candidate(
        active_refresh_run_id=run_id, manifest_hash="mh1",
        # The artifact key is an expr: key whose lineage is the key column.
        grain_key_ids=frozenset({"expr:some-fp"}),
        grain_key_lineage_by_id={"expr:some-fp": (_COL_COUNTRY,)},
        grain_key_source_dim_by_id={},  # expr keys carry no source_dimension_id
        trust_by_relationship={"r1": ti},
        edge_by_attribute_key=e_idx, passenger_by_attribute_key=p_idx,
    )
    proof = build_serve_proof(
        query_keys=[_attr_request()], measures=[MeasureRequest("amount", "sum")],
        candidate=cand,
    )
    assert proof.verdict == SOURCE_ONLY
    assert "ATTRIBUTE_LINEAGE_MISMATCH" in proof.reason_codes
