"""The shipped acme-demo/modely seed must be pocket-provable (Bug-8619).

Bug-8580 added a row-population proof that refuses to serve a pocket unless
the pocket's plan and the query's plan provably hold the same rows. On the
shipped ``modely`` seed EVERY leg of that proof failed, so the pocket route
returned nothing on the demo model and two registered live scenarios
(``LIVE-POCKET-RLS-001`` and the ``force_route="pocket"`` gateway scenario)
went PRODUCT_FAIL. Measured at the time: 23 joins — 13 INNER, 6 legacy
``many_to_one``, 3 LEFT, 1 RIGHT — and ZERO columns flagged
``is_primary_key`` across all 212 columns.

The resolution was to fix the MODEL, not to weaken the proof or the
scenarios:

* every dimension edge now preserves the FACT rows (``right`` where the
  modeller drew dim -> fact, ``left`` where they drew fact -> calendar), which
  is what a star schema means and what makes the model's own totals
  independent of which dimension a report groups by;
* every dimension's join key carries the PRIMARY KEY the source DDL actually
  declares (``deploy/Sample-db/create-tables.sql`` /
  ``calendar-test-tables.sql``), so the non-duplication leg has real evidence.

This test is the executable form of that contract. It runs against the
SHIPPED bundle rather than a fixture, because the failure mode it guards is
"someone edits the seed and the live pocket scenarios silently go red again"
— a fixture copy could not see that. It is a pure function of the bundle: no
database, no live stack.

Measured with this harness on the repaired bundle: proven for a fact-only
plan and for a fact+dimension plan. On the PRE-repair bundle
(``git show HEAD~:...``) the same call returns False, so the assertion below
genuinely depends on the seed being fixed.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.routing.pocket_population import (
    JoinEdge,
    ModelJoinGraph,
    population_proven,
)
from shared.semantic.join_keyword import is_orientation_declared

pytestmark = pytest.mark.unit

_SEED = (
    Path(__file__).resolve().parents[3] / "seeds" / "acme-demo" / "project.json"
)


def _modely() -> dict:
    if not _SEED.exists():  # pragma: no cover - bundle is checked in
        pytest.skip(f"seed bundle not found at {_SEED}")
    bundle = json.loads(_SEED.read_text(encoding="utf-8"))
    for model in bundle["models"]:
        if model["model"].get("slug") == "modely":
            return model
    raise AssertionError("acme-demo bundle no longer contains a 'modely' model")


def _graph(model: dict) -> ModelJoinGraph:
    return ModelJoinGraph(
        table_ids=frozenset(str(t["id"]) for t in model["tables"]),
        edges=tuple(
            JoinEdge(
                left_table_id=str(j["left_table_id"]),
                right_table_id=str(j["right_table_id"]),
                left_column_id=str(j["left_column_id"]),
                right_column_id=str(j["right_column_id"]),
                join_type=j["join_type"],
            )
            for j in model["joins"]
        ),
        pk_column_ids=frozenset(
            str(c["id"]) for c in model["columns"] if c.get("is_primary_key")
        ),
        table_id_by_column_id={
            str(c["id"]): str(c["model_table_id"]) for c in model["columns"]
        },
    )


def _fact_id(model: dict) -> str:
    facts = [t for t in model["tables"] if t.get("table_type") == "fact"]
    assert len(facts) == 1, f"expected exactly one fact table, got {len(facts)}"
    return str(facts[0]["id"])


def test_every_seed_join_declares_a_real_orientation():
    """No join may sit at a legacy/cardinality token.

    Rule 2 of the proof refuses the WHOLE plan when any edge carries one,
    because an undeclared token renders as an un-flipped LEFT JOIN whose
    preserved side depends on the compiler's base table rather than on the
    model.
    """
    model = _modely()
    undeclared = [
        j["join_type"] for j in model["joins"]
        if not is_orientation_declared(j["join_type"])
    ]
    assert undeclared == [], (
        f"modely joins still carry undeclared orientation tokens: {undeclared}"
    )


def test_every_seed_join_preserves_the_fact_rows():
    """A dimension lookup must not silently delete fact rows.

    The pre-repair seed declared ``dim_card_entry_mode LEFT JOIN
    payment_transaction`` — preserving the DIMENSION — which dropped 83,278 of
    the fact's 100,000 rows (measured against the demo source), and was the
    whole of the model's 100,000 -> 16,722 star-join collapse.
    """
    model = _modely()
    fact = _fact_id(model)
    offenders = []
    for j in model["joins"]:
        left_is_fact = str(j["left_table_id"]) == fact
        right_is_fact = str(j["right_table_id"]) == fact
        assert left_is_fact or right_is_fact, (
            "modely is a star; every join is expected to touch the fact"
        )
        expected = "left" if left_is_fact else "right"
        if j["join_type"] != expected:
            offenders.append((j["join_type"], expected))
    assert offenders == [], (
        f"joins that do not preserve the fact rows (got, expected): {offenders}"
    )


def test_every_joined_dimension_declares_exactly_one_primary_key():
    """The non-duplication leg needs a SINGLE-column declared key.

    A composite key flagged on two columns and joined on one half matches N
    rows per fact row and inflates every SUM, so the proof requires the join
    column to be the table's SOLE declared key — not merely a member of it.
    """
    model = _modely()
    fact = _fact_id(model)
    columns = {str(c["id"]): c for c in model["columns"]}
    keys_by_table: dict[str, set[str]] = {}
    for col in model["columns"]:
        if col.get("is_primary_key"):
            keys_by_table.setdefault(str(col["model_table_id"]), set()).add(
                str(col["id"])
            )

    problems = []
    for j in model["joins"]:
        for side in ("left", "right"):
            table_id = str(j[f"{side}_table_id"])
            if table_id == fact:
                continue
            column_id = str(j[f"{side}_column_id"])
            declared = keys_by_table.get(table_id, set())
            if declared != {column_id}:
                problems.append(
                    (columns[column_id]["column_name"], sorted(declared))
                )
    assert problems == [], (
        f"dimension join keys that are not the table's sole declared primary "
        f"key (join column, declared keys): {problems}"
    )


@pytest.mark.parametrize("extra_dimension", [None, "dim_card_entry_mode"])
def test_a_pocket_over_the_seed_model_is_provable(extra_dimension):
    """The end-to-end property the two red live scenarios depend on.

    ``LIVE-POCKET-RLS-001`` and the gateway ``force_route="pocket"`` scenario
    both run ``SELECT region_code, COUNT(*) FROM modely GROUP BY region_code``,
    whose plan resolves to the fact alone (``region_code`` is a fact column).
    The parametrized second case adds a dimension-bound projection so the
    ``extra`` / lossless-attachment leg of the proof is exercised too, not
    only the identical-plan shortcut.
    """
    model = _modely()
    graph = _graph(model)
    query_tables = [_fact_id(model)]
    if extra_dimension:
        match = [t for t in model["tables"] if t.get("alias") == extra_dimension]
        assert match, f"seed no longer has a {extra_dimension!r} table"
        query_tables.append(str(match[0]["id"]))

    assert population_proven(graph=graph, query_table_ids=query_tables) is True, (
        "a pocket over the shipped modely seed cannot be proven "
        "row-equivalent to this query's own plan, so the pocket route will "
        "fall back to source and the registered live pocket scenarios will "
        "report PRODUCT_FAIL"
    )


def test_the_proof_harness_is_not_vacuous():
    """Guard the guard: the assertion above must be capable of failing.

    Stripping the declared primary keys reproduces the pre-repair bundle's
    state, and the proof must refuse it. Without this, a change that made
    ``population_proven`` return True unconditionally would leave the test
    above green.
    """
    model = _modely()
    graph = _graph(model)
    keyless = ModelJoinGraph(
        table_ids=graph.table_ids,
        edges=graph.edges,
        pk_column_ids=frozenset(),
        table_id_by_column_id=graph.table_id_by_column_id,
    )
    assert (
        population_proven(graph=keyless, query_table_ids=[_fact_id(model)]) is False
    )
