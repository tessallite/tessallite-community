"""Shared machinery for deployed-snapshot serving authority.

The deployed model snapshot is the SINGLE serving authority for an entity's
DEFINITION on every BI-facing surface. A definition edit is only live to BI
consumers after a model Save + Deploy. Governance/lifecycle fields are the
exception: they are overlaid from the LIVE row so an admin can deprecate or
re-certify an entity without a redeploy.

That contract is identical for every snapshot-pinned entity family; only three
things vary per family:

- the ORM class (which columns exist),
- the snapshot key the family is serialised under (``kpis``, ``named_sets``),
- which columns count as live governance rather than snapshot definition.

Bug-8384. This module exists because that logic was previously hand-copied per
family. ``kpi_deploy_resolver`` (F-017-01 / F-013-04) was the first
implementation; pinning the gateway's MDX named-set surfaces needed the same
fail-closed rules, and a second hand-written copy of a security-adjacent
fail-closed path is exactly the drift hazard this codebase has been bitten by
before. Both family resolvers now delegate here, so a correction to the
fail-closed contract lands for every family at once.

Bug-8712: this module lives in ``shared`` rather than inside model-service
because the same authority is needed by a SECOND SERVICE. The agent-service
prompt assembler builds its KPI / named-set catalogue from the tenant database
directly, so it needs the identical snapshot load + index + fail-closed rules.
The alternative — a third hand-written copy in agent-service — is the exact
drift hazard above. Every consumer adapts this core; nobody re-implements it.
The wider contract it serves is stated in
``docs/architecture/architecture_model-versioning-and-deploy.md``: "the deployed
snapshot is the contract; the live state is editor-only" (F-013-01).

Fail-closed contract for a DEPLOYED model:
- deploy pointer set but the version row / snapshot is missing, empty or
  malformed -> raise the family's ``error_cls``. Serving surfaces surface a
  stable error and NEVER fall back to live/draft definitions.
- snapshot present but the entity id is absent -> the entity is not part of the
  deployed version and is withheld from every BI consumer.
- deployed and pinned -> a detached ORM instance carrying the snapshot
  definition with the live governance overlay.

An UNDEPLOYED model has no serving authority at all: ``load_deployed_snapshot``
returns ``None`` and the caller decides. Only builder/modeller surfaces may
serve the live draft in that case; a gateway/BI producer must not.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional, Type
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from shared.db.models import Model, ModelVersion


def _as_uuid(value: str) -> Any:
    if len(value) == 36 and value.count("-") == 4:
        try:
            return UUID(value)
        except ValueError:
            pass
    return value


def _as_datetime(value: str) -> Any:
    if len(value) >= 19 and value[4:5] == "-" and value[7:8] == "-" and "T" in value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
    return value


def coerce_snapshot_column(value: Any, column: Any) -> Any:
    """Coerce a serialised snapshot scalar back to its ORM column's type.

    The serialiser stores UUIDs as strings, datetimes as ISO strings, and
    NUMERICs as floats (``serialiser._j``). Rebuild UUID/datetime so a detached
    instance behaves like a live row for downstream consumers.

    Bug-8384: the decision is driven by the COLUMN TYPE, not by the string's
    shape. Shape-sniffing silently corrupts any free-text column whose value
    happens to look like a UUID or an ISO timestamp — a named set's
    ``dimensions`` list or a KPI's ``null_display_value`` are plain strings that
    must survive the round trip verbatim, and handing a ``UUID`` object to a
    ``str``-typed response field is a producer/consumer type mismatch. An
    unparseable value falls through unchanged so a malformed snapshot degrades
    rather than raising mid-serve.
    """
    if value is None or not isinstance(value, str):
        return value
    try:
        python_type = column.type.python_type
    except (NotImplementedError, AttributeError):
        return value
    if python_type is UUID:
        return _as_uuid(value)
    if python_type is datetime:
        return _as_datetime(value)
    return value


# The semantic-shape families a snapshot serialises. Named here so the one
# consumer that reads ALL of them (the AI advisor) and the import producer that
# persists restorable history agree on the same list.
SEMANTIC_SHAPE_FAMILIES: tuple[str, ...] = (
    "tables",
    "columns",
    "measures",
    "dimensions",
    "hierarchies",
    "user_defined_attributes",
)


def malformed_snapshot_families(
    snapshot: "dict[str, Any] | None", families: "tuple[str, ...] | list[str]",
) -> list[str]:
    """Families that are PRESENT in the snapshot but not a list of dicts.

    Bug-8032 (sol C1/B3). ``snapshot_has_shape`` structurally validates exactly
    ONE family — the representative family its caller asks about — because each
    of its callers reads exactly one family and that is all any of them needs.
    The AI advisor is the exception: it reads six families, so five of them
    passed through unvalidated and were silently coerced to ``[]`` by its row
    reader. An empty ``dimensions`` mapping therefore presented as "this model
    deploys no dimensions" instead of "this snapshot is malformed", and the
    advisor reasoned — and paid an LLM — over a shape that was never there.

    Deliberately ADDITIVE rather than a widening of ``snapshot_has_shape``: the
    KPI, named-set and agent-catalogue resolvers each read one family, are
    already validated for it, and must not start refusing a deployed snapshot
    over a family they never touch. A consumer that reads many families asks for
    many families; the shared predicate keeps its existing meaning.

    ``None`` and absent are both "family not in this snapshot" and are not
    malformed — a snapshot need not carry every family. Anything else that is
    not a list, or a list holding a non-dict, is.

    The same reasoning applies one level up, to the SNAPSHOT: an absent snapshot
    is a supported state on several paths here (a version row with no snapshot,
    a degraded import, an unmapped connection), and it has no malformed families
    because it has no families. It answers ``[]``, it does not raise — a shared
    predicate that crashes on a legitimate absent input turns a degraded state
    into an outage for every consumer that reaches it. Anything that is present
    but is not a mapping cannot be interrogated family by family and is reported
    as malformed WHOLESALE, so no caller mistakes it for clean.
    """
    if snapshot is None:
        return []
    if not isinstance(snapshot, dict):
        return list(families)
    bad: list[str] = []
    for family in families:
        rows = snapshot.get(family)
        if rows is None:
            continue
        if not isinstance(rows, list) or any(
            not isinstance(row, dict) for row in rows
        ):
            bad.append(family)
    return bad


def snapshot_has_shape(snapshot: dict[str, Any], family: str) -> bool:
    """A usable snapshot carries at least one semantic-shape family.

    Mirrors ``snapshot_resolver._snapshot_has_shape`` (query-router): an
    empty/placeholder snapshot (legacy seed v1) is not a valid serving
    authority, and treating one as authoritative would silently withhold every
    entity instead of failing closed with a diagnosable error.

    The columns-without-tables clause is Bug-8306 parity and must stay in step
    with the query-router predicate. Without it the two authorities disagree
    about the SAME deployed snapshot: the query-router refuses every query with
    DEPLOYED_SNAPSHOT_INVALID while these BI listing routes return HTTP 200 with
    an empty list, so a BI client shows an empty catalogue and no error rather
    than a diagnosable failure.

    The accepted families are the query-router's list VERBATIM, plus ``family``.
    The one deliberate difference is that extra term: a snapshot carrying only
    this resolver's own family is a usable authority HERE (it can answer the
    question being asked) even though the query-router, which needs a semantic
    shape to plan SQL against, would reject it. Everything else must stay
    identical, so a future tightening on either side is a two-line change on
    both rather than a silent divergence.
    """
    family_rows = snapshot.get(family)
    if family_rows is not None and not isinstance(family_rows, list):
        return False
    if isinstance(family_rows, list) and any(
        not isinstance(row, dict) for row in family_rows
    ):
        return False
    if snapshot.get("columns") and not snapshot.get("tables"):
        return False
    return bool(
        snapshot.get("measures")
        or snapshot.get("dimensions")
        or snapshot.get("columns")
        or snapshot.get("hierarchies")
        or snapshot.get(family)
    )


async def load_deployed_snapshot(
    db: AsyncSession,
    model: Model,
    *,
    family: str,
    error_cls: Type[Exception],
) -> Optional[dict[str, Any]]:
    """Return the deployed snapshot dict, or ``None`` if the model is undeployed.

    Raises ``error_cls`` when a DEPLOYED model's version row or snapshot is
    missing / empty / malformed. Never falls back to live definitions.
    """
    deployed_version_id = getattr(model, "deployed_version_id", None)
    if deployed_version_id is None:
        return None
    version = await db.get(ModelVersion, deployed_version_id)
    if version is None or version.model_id != model.id:
        raise error_cls("The deployed model version could not be found.")
    snapshot = version.snapshot_json
    if not isinstance(snapshot, dict) or not snapshot_has_shape(snapshot, family):
        raise error_cls("The deployed model snapshot is empty or malformed.")
    return snapshot


def index_snapshot_rows(
    snapshot: dict[str, Any], family: str
) -> dict[str, dict[str, Any]]:
    """Index a snapshot family's rows by string id."""
    out: dict[str, dict[str, Any]] = {}
    for row in snapshot.get(family, []) or []:
        if isinstance(row, dict) and row.get("id"):
            out[str(row["id"])] = row
    return out


def build_served_row(
    orm_cls: type,
    snapshot_row: dict[str, Any],
    live_row: Any,
    *,
    governance_fields: tuple[str, ...],
    identity_fields: tuple[str, ...],
) -> Any:
    """Build a detached ORM instance: snapshot definition + live governance.

    Definition fields come from ``snapshot_row`` (the deployed authority);
    governance/lifecycle and identity fields come from ``live_row``.

    A column absent from the snapshot (a legacy snapshot predating that column)
    falls back to the live value, so a schema addition never crashes serving.
    The definition contract still holds for every field the snapshot does pin.

    ``live_row`` is always a real row. Both families resolve against the LIVE row
    set, so there is no "snapshot orphan" case here. Do not add one by making
    this tolerate ``live_row=None``: a row with no live counterpart also has no
    live governance, and silently sourcing governance from the snapshot lets a
    delete undo a deprecation (see the reverted-orphan note in
    ``named_set_deploy_resolver`` and Bug-8711 — the membership gap; the number
    is Bug-8711 on this branch, recorded as "AKA source Bug-8753", and main's
    own Bug-8753 is an unrelated query-router miss-reason issue).
    """
    served = orm_cls()
    for column in orm_cls.__table__.columns:
        name = column.name
        if name in governance_fields or name in identity_fields:
            setattr(served, name, getattr(live_row, name, None))
        elif name in snapshot_row:
            setattr(served, name, coerce_snapshot_column(snapshot_row[name], column))
        else:
            setattr(served, name, getattr(live_row, name, None))
    return served
