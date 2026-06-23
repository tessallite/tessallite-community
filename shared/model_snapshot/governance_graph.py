"""Canonical governance graph shared by Solidatus, Collibra, and future
governance-platform integrations.

Produces an intermediate representation (nodes + edges) that each
adapter maps to its target platform's payload format.

Reviewer note (2026-06-13):  extracted from the nearly-identical copies
in the Solidatus and Collibra architecture plans so there is a single
source of truth for the Tessallite-governance export shape.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class GovernanceNode(BaseModel):
    """One asset/object in the Tessallite governance graph."""

    stable_key: str
    """Human-readable compound key, e.g. ``sales.revenue.measure.gross_revenue``."""

    object_type: str
    """Tessallite domain type: domain, semantic_model, source_system, table,
    column, dimension, measure, kpi, glossary_term, downstream_asset,
    aggregate, data_tag, data_target."""

    object_id: str
    """UUID of the backing database row."""

    label: str
    """Display name for UI / governance tools."""

    description: str | None = None
    """Business description, if present."""

    owner: str | None = None
    """Owner identifier (email / user id), if available."""

    steward: str | None = None
    """Steward / data-owner identifier, if available."""

    status: str | None = None
    """Lifecycle status: active, draft, deprecated, certified, invalid, etc."""

    properties: dict[str, Any] = {}
    """All other non-sensitive fields from the ORM row (keyed by column name)."""


class GovernanceEdge(BaseModel):
    """One relationship between two governance-graph nodes."""

    stable_key: str
    """Human-readable edge key, e.g.
    ``sales.revenue.model->measure.gross_revenue``."""

    source_key: str
    """``GovernanceNode.stable_key`` of the source."""

    target_key: str
    """``GovernanceNode.stable_key`` of the target."""

    relationship_type: str
    """Tessallite domain type: contains, contains_table, contains_column,
    defines_dimension, defines_measure, derived_from, governed_by_term,
    produces_aggregate, materialized_to, consumed_by, feeds_semantic_field,
    uses_measure, classified_by, stored_in."""

    label: str | None = None
    """Optional human-readable edge label."""

    properties: dict[str, Any] = {}
    """Additional edge metadata."""


class GovernanceGraph(BaseModel):
    """Complete governance export for one Tessallite model."""

    nodes: list[GovernanceNode]
    edges: list[GovernanceEdge]
