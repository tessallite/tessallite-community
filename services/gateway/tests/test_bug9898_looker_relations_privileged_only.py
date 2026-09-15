"""Bug-9898 / persona-layering rule 4, audit row A42.

The generated-LookML adapter registers one relation per semantic table with
``table_persona_id = None``, ``table_include_hidden = True`` and the UNFILTERED
dimension/measure lists: no persona allow-list, no hidden-column filter, no
column-level-security closure. That shape is deliberate -- Looker needs the
declared (often hidden) key columns to emit symmetric-aggregate SQL -- and
owner decision 4.5(b) keeps it rather than persona-filtering it.

What was missing is the other half of that decision: the surface has to run on
a PRIVILEGED, documented connection. It did not. Any JDBC client on a
Looker-enabled deployment saw the whole persona-blind, hidden-column-exposing
relation set, and a query against it reached the query-router with no persona
at all (``persona_id=None`` means ``apply_persona_gate`` returns immediately --
no allow-list, no default filters).

The gate is ``_caller_is_privileged``, the same predicate the base relation
already uses to decide it is unrestricted. So the adapter relations are
advertised only to an identity the EXECUTOR also treats as persona-free: what
this catalogue promises equals what ``/execute`` accepts for the same caller.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src import router_client  # noqa: E402

FACT_RELATION = "sales__payment_transaction"
DIM_RELATION = "sales__dim_account_type"


def _patch(monkeypatch):
    async def _models(*_a, **_kw):
        return [{
            "id": "m1", "project_id": "p1", "project_slug": "alpha",
            "slug": "sales", "description": "Sales",
        }]

    async def _dimensions(*_a, **_kw):
        return [
            # A HIDDEN declared key: the exact column the adapter surface
            # exists to expose, and the exact column no persona catalogue may.
            {"id": "d1", "name": "payment_id", "source_column_id": "c1",
             "is_hidden": True},
            {"id": "d2", "name": "payment_status", "source_column_id": "c2"},
            {"id": "d3", "name": "account_type_code", "source_column_id": "c3"},
        ]

    async def _measures(*_a, **_kw):
        return [{"id": "me1", "name": "amount", "source_column_id": "c4",
                 "default_agg": "sum"}]

    async def _personas(*_a, **_kw):
        return [{
            "id": "cccccccc-cccc-cccc-cccc-cccccccccccc",
            "slug": "business", "name": "Business",
            "included_measure_ids": ["me1"],
            "included_dimension_ids": ["d2"],
            "includes_hidden_columns": False,
            "restricted_column_ids": [],
        }]

    async def _snapshot(*_a, **_kw):
        return {
            "tables": [
                {"id": "t1", "alias": "payment_transaction",
                 "table_type": "fact", "row_count_estimate": 2500},
                {"id": "t2", "alias": "dim_account_type",
                 "table_type": "dimension", "row_count_estimate": 12},
            ],
            "columns": [
                {"id": "c1", "model_table_id": "t1", "is_primary_key": True},
                {"id": "c2", "model_table_id": "t1"},
                {"id": "c3", "model_table_id": "t2", "is_primary_key": True},
                {"id": "c4", "model_table_id": "t1"},
            ],
            "joins": [],
        }

    async def _kpis(*_a, **_kw):
        return []

    monkeypatch.setattr(router_client, "list_all_models_for_tenant", _models)
    monkeypatch.setattr(router_client, "get_model_dimensions", _dimensions)
    monkeypatch.setattr(router_client, "get_model_measures", _measures)
    monkeypatch.setattr(router_client, "get_model_personas", _personas)
    monkeypatch.setattr(router_client, "get_model_snapshot", _snapshot)
    monkeypatch.setattr(router_client, "get_model_kpis", _kpis)
    monkeypatch.setattr(router_client.settings, "LOOKER_GATEWAY_ENABLED", True)


async def _catalogue(monkeypatch, *, privileged: bool):
    _patch(monkeypatch)
    monkeypatch.setattr(
        router_client, "_caller_is_privileged", lambda _tok: privileged,
    )
    result = await router_client.fetch_model_metadata(None, "acme", "jwt")
    return {
        "names": result[0],
        "columns": result[1],
        "persona_ids": result[5],
        "include_hidden": result[6],
        "looker_relations": result[10],
    }


class TestTheAdapterSurfaceIsPrivilegedOnly:
    @pytest.mark.asyncio
    async def test_non_privileged_caller_never_sees_the_adapter_relations(
        self, monkeypatch,
    ):
        cat = await _catalogue(monkeypatch, privileged=False)
        assert FACT_RELATION not in cat["names"], (
            "a non-privileged caller must not be offered a persona-blind "
            "semantic-table relation"
        )
        assert DIM_RELATION not in cat["names"]
        assert FACT_RELATION not in cat["columns"]
        assert FACT_RELATION not in cat["persona_ids"]

    @pytest.mark.asyncio
    async def test_non_privileged_caller_cannot_reach_a_hidden_column(
        self, monkeypatch,
    ):
        """The concrete exposure: ``payment_id`` is hidden, so it is absent
        from every relation this caller can see."""
        cat = await _catalogue(monkeypatch, privileged=False)
        for relation, columns in cat["columns"].items():
            names = {c["name"] for c in columns}
            assert "payment_id" not in names, (
                f"hidden column reachable through relation {relation}"
            )

    @pytest.mark.asyncio
    async def test_disabled_detection_still_names_the_relations(
        self, monkeypatch,
    ):
        """The Looker client-detection path reads ``looker_relations`` to tell
        a Looker client the surface is off. Withholding the relation from the
        catalogue must not blind that path."""
        cat = await _catalogue(monkeypatch, privileged=False)
        assert cat["looker_relations"] == {FACT_RELATION, DIM_RELATION}

    @pytest.mark.asyncio
    async def test_privileged_caller_keeps_the_full_adapter_surface(
        self, monkeypatch,
    ):
        """Owner decision 4.5(b): do NOT persona-filter these relations. On the
        privileged connection the shape Looker needs is unchanged."""
        cat = await _catalogue(monkeypatch, privileged=True)
        assert FACT_RELATION in cat["names"]
        assert DIM_RELATION in cat["names"]
        assert [c["name"] for c in cat["columns"][FACT_RELATION]] == [
            "payment_id", "payment_status", "amount",
        ]
        assert cat["include_hidden"][FACT_RELATION] is True
        assert cat["persona_ids"][FACT_RELATION] is None

    @pytest.mark.asyncio
    async def test_privileged_caller_still_gated_by_the_flag(self, monkeypatch):
        """Both conditions are required; the flag default stays off."""
        _patch(monkeypatch)
        monkeypatch.setattr(
            router_client, "_caller_is_privileged", lambda _tok: True,
        )
        monkeypatch.setattr(
            router_client.settings, "LOOKER_GATEWAY_ENABLED", False,
        )
        result = await router_client.fetch_model_metadata(None, "acme", "jwt")
        assert FACT_RELATION not in result[0]
        assert result[10] == {FACT_RELATION, DIM_RELATION}


class TestTheCatalogueAgreesWithTheExecutorForTheSameIdentity:
    @pytest.mark.asyncio
    async def test_every_relation_a_non_privileged_caller_sees_carries_a_persona(
        self, monkeypatch,
    ):
        """The rule as a biconditional over the whole catalogue: a
        non-privileged caller with one assigned persona is described exactly
        the surface the router will serve them, on EVERY relation. A
        ``persona_id`` of None on such a catalogue is the bypass shape --
        ``apply_persona_gate`` returns immediately, so no allow-list and no
        default filter is applied at execution."""
        cat = await _catalogue(monkeypatch, privileged=False)
        unscoped = [
            name for name, pid in cat["persona_ids"].items() if pid is None
        ]
        assert unscoped == [], (
            "these relations would execute with no persona at all: "
            f"{unscoped}"
        )
