"""Transactional propagation for model-owned measure-name consumers."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import sqlglot
from sqlglot import exp
from sqlalchemy import select

from shared.db.models import (
    AggregateDefinition,
    Dimension,
    KPI,
    Measure,
    Model,
    ModelAliasMap,
    NamedSet,
    ProjectCrossModelRecipe,
    QuantileCoverage,
    SavedQuery,
    ScratchpadMeasure,
)
from shared.recipes.schema import inspect_combine_tree

# Keys inside a KPI ``business_definition`` whose STRING value is a bare measure
# NAME (``_compiled.summary_tokens``, built by ``kpi_business_builder._build_summary``).
#
# Bug-9483: this used to be a private copy of three keys while the PRODUCER wrote
# five — ``compare_measures`` also writes ``measure_a_name``/``measure_b_name``.
# Renaming a measure therefore left that (shipped, wizard-reachable) family's
# summary naming a measure that no longer exists. Imported from the producer so
# the two cannot drift again; ``dimension_name`` and ``filter_dimensions`` are
# excluded THERE, where the reason lives.
from src.kpi_business_builder import (
    MEASURE_NAME_SUMMARY_TOKEN_KEYS as _MEASURE_NAME_JSON_KEYS,
)


_MEASURE_CALL_RE = re.compile(
    r"(?P<prefix>\bmeasure\s*\(\s*)(?P<quote>['\"])(?P<name>[^'\"]+)"
    r"(?P=quote)(?P<suffix>\s*\))",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Consumer-type vocabulary
# ---------------------------------------------------------------------------
# The CLOSED set of ``consumer_type`` tokens the rename plan may emit, on either
# the ``rewrites`` or the ``blockers`` path. This is a published API contract:
# ``MeasureRenameImpactItem.consumer_type`` documents exactly this set and the
# rename-confirmation dialog switches on it.
#
# Bug-9394 follow-up (L7-R3): the two paths had drifted. The rewrite recorded the
# model alias map as ``"alias_map"`` while the blocker recorded the SAME consumer
# as ``"model_alias_map"``, and the schema documented only the first — so a client
# that mapped the documented vocabulary to labels rendered an undocumented token
# for a blocker. ``model_alias_map`` is the surviving token: it names the
# ``ModelAliasMap`` ORM entity rather than its ``alias_map`` column, which is what
# every other token in this set does.
#
# ``named_set`` is blockers-only BY DESIGN: a named set's expression and builder
# definition are matched by containment, not parsed, so a hit is reported and
# refused rather than rewritten. ``quantile_coverage`` is rewrites-only for the
# mirror reason: the coverage row is INVALIDATED (``field == "$invalidated"``),
# never rewritten in place.
CONSUMER_TYPE_MEASURE = "measure"
CONSUMER_TYPE_KPI = "kpi"
CONSUMER_TYPE_SAVED_QUERY = "saved_query"
CONSUMER_TYPE_SCRATCHPAD_MEASURE = "scratchpad_measure"
CONSUMER_TYPE_CROSS_MODEL_RECIPE = "cross_model_recipe"
CONSUMER_TYPE_MODEL_ALIAS_MAP = "model_alias_map"
CONSUMER_TYPE_NAMED_SET = "named_set"
CONSUMER_TYPE_QUANTILE_COVERAGE = "quantile_coverage"

RENAME_CONSUMER_TYPES = frozenset({
    CONSUMER_TYPE_MEASURE,
    CONSUMER_TYPE_KPI,
    CONSUMER_TYPE_SAVED_QUERY,
    CONSUMER_TYPE_SCRATCHPAD_MEASURE,
    CONSUMER_TYPE_CROSS_MODEL_RECIPE,
    CONSUMER_TYPE_MODEL_ALIAS_MAP,
    CONSUMER_TYPE_NAMED_SET,
    CONSUMER_TYPE_QUANTILE_COVERAGE,
})


@dataclass(frozen=True)
class UnsafeRenameReference:
    consumer_type: str
    consumer_id: str
    field: str
    visible_to: str | None = None


class UnsafeMeasureRename(ValueError):
    def __init__(self, references: list[UnsafeRenameReference]):
        self.references = references
        super().__init__(self.detail_for(None))

    def detail_for(self, viewer_identity: str | None) -> str:
        """Render exact owned/shared refs and redact another user's artifacts."""
        details: list[str] = []
        redacted_types: set[str] = set()
        for ref in self.references:
            if ref.visible_to is not None and ref.visible_to != viewer_identity:
                if ref.consumer_type not in redacted_types:
                    details.append(f"{ref.consumer_type}:private")
                    redacted_types.add(ref.consumer_type)
                continue
            details.append(
                f"{ref.consumer_type}:{ref.consumer_id}"
                f"{ref.field if ref.field.startswith('$') else '.' + ref.field}"
            )
        return (
            "Measure rename cannot safely rewrite these model-owned references: "
            + ", ".join(details)
        )


def rewrite_measure_calls(expression: str, names: dict[str, str]) -> tuple[str, bool]:
    """Rewrite exact ``measure('name')`` DSL references in one lexical pass."""
    changed = False

    def _replace(match: re.Match[str]) -> str:
        nonlocal changed
        old = match.group("name")
        new = names.get(old)
        if new is None:
            return match.group(0)
        changed = True
        return (
            match.group("prefix")
            + match.group("quote")
            + new
            + match.group("quote")
            + match.group("suffix")
        )

    return _MEASURE_CALL_RE.sub(_replace, expression), changed


def rewrite_semantic_sql(query_text: str, names: dict[str, str]) -> tuple[str, bool]:
    """Rewrite exact semantic column identifiers through sqlglot."""
    tree = sqlglot.parse_one(query_text, read="postgres")
    changed = False
    for column in tree.find_all(exp.Column):
        new = names.get(column.name)
        if new is None:
            continue
        quoted = bool(getattr(column.this, "args", {}).get("quoted"))
        column.set("this", exp.to_identifier(new, quoted=quoted))
        changed = True
    if not changed:
        return query_text, False
    return tree.sql(dialect="postgres"), True


def _contains_name(text: str | None, names: set[str]) -> bool:
    if not text:
        return False
    return any(
        re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", text)
        for name in names
    )


def _rewrite_json(
    value: Any, names: dict[str, str], *, key: str | None = None,
) -> tuple[Any, bool]:
    """Rewrite measure references inside a JSON blob.

    A bare string is replaced ONLY when its KEY is a known measure-name carrier.
    Rewriting any string that merely EQUALS the old name silently corrupted data:
    ``business_definition.filters[i].value`` holds a dimension MEMBER value, so
    renaming a measure ``Retail`` to ``Retail Sales`` rewrote the KPI's
    ``channel = "Retail"`` filter and changed which rows the KPI computes over —
    a wrong number with no error anywhere. ``measure("...")`` DSL calls are still
    rewritten wherever they appear, because those are unambiguous references.
    """
    if isinstance(value, dict):
        changed = False
        out = {}
        for item_key, item in value.items():
            rewritten, item_changed = _rewrite_json(item, names, key=item_key)
            out[item_key] = rewritten
            changed = changed or item_changed
        return out, changed
    if isinstance(value, list):
        changed = False
        out = []
        for item in value:
            # A list inherits its parent key: ``measure_names: [...]`` is still a
            # measure-name carrier, ``filters: [...]`` is still not one.
            rewritten, item_changed = _rewrite_json(item, names, key=key)
            out.append(rewritten)
            changed = changed or item_changed
        return out, changed
    if isinstance(value, str):
        if key in _MEASURE_NAME_JSON_KEYS and value in names:
            return names[value], True
        return rewrite_measure_calls(value, names)
    return value, False


def _json_contains_exact(value: Any, names: set[str]) -> bool:
    if isinstance(value, dict):
        return any(_json_contains_exact(item, names) for item in value.values())
    if isinstance(value, list):
        return any(_json_contains_exact(item, names) for item in value)
    return isinstance(value, str) and value in names


def _rewrite_recipe_consumer(
    recipe: Any,
    model_id: UUID,
    names: dict[str, str],
) -> tuple[list[Any], Any, list[UnsafeRenameReference], bool, bool]:
    """Plan exact, model-scoped recipe rewrites without mutating ORM state."""
    original_steps = getattr(recipe, "steps", None)
    steps = deepcopy(original_steps)
    combine = deepcopy(getattr(recipe, "combine", None))
    unsafe: list[UnsafeRenameReference] = []
    recipe_id = str(recipe.id)
    name_set = set(names)

    if not isinstance(steps, list):
        if _json_contains_exact(steps, name_set):
            unsafe.append(
                UnsafeRenameReference(CONSUMER_TYPE_CROSS_MODEL_RECIPE, recipe_id, "$.steps")
            )
        return steps, combine, unsafe, False, False

    step_name_counts = Counter(
        step.get("name")
        for step in steps
        if isinstance(step, dict)
        and isinstance(step.get("name"), str)
        and step.get("name")
    )
    # One entry per target-model step name. Its value is the set of old names
    # that were actually present in that step's declared measures.
    target_steps: dict[str, set[str]] = {}
    target_step_names: set[str] = set()
    steps_changed = False
    for index, step in enumerate(steps):
        path = f"$.steps[{index}]"
        if not isinstance(step, dict):
            if _json_contains_exact(step, name_set):
                unsafe.append(
                    UnsafeRenameReference(CONSUMER_TYPE_CROSS_MODEL_RECIPE, recipe_id, path)
                )
            continue

        measures = step.get("measures")
        try:
            step_model_id = UUID(str(step.get("model_id")))
        except (TypeError, ValueError, AttributeError):
            if _json_contains_exact(measures, name_set):
                unsafe.append(
                    UnsafeRenameReference(
                        CONSUMER_TYPE_CROSS_MODEL_RECIPE, recipe_id, f"{path}.model_id"
                    )
                )
            continue
        if step_model_id != model_id:
            continue

        step_name = step.get("name")
        if isinstance(step_name, str) and step_name:
            target_step_names.add(step_name)
        if not isinstance(measures, list):
            if _json_contains_exact(measures, name_set):
                unsafe.append(
                    UnsafeRenameReference(
                        CONSUMER_TYPE_CROSS_MODEL_RECIPE, recipe_id, f"{path}.measures"
                    )
                )
            continue

        old_names_present: set[str] = set()
        for measure_index, measure_name in enumerate(measures):
            if not isinstance(measure_name, str):
                if _json_contains_exact(measure_name, name_set):
                    unsafe.append(
                        UnsafeRenameReference(
                            CONSUMER_TYPE_CROSS_MODEL_RECIPE,
                            recipe_id,
                            f"{path}.measures[{measure_index}]",
                        )
                    )
                continue
            replacement = names.get(measure_name)
            if replacement is None:
                continue
            old_names_present.add(measure_name)
            measures[measure_index] = replacement
            steps_changed = True

        if old_names_present:
            if not isinstance(step_name, str) or not step_name:
                unsafe.append(
                    UnsafeRenameReference(
                        CONSUMER_TYPE_CROSS_MODEL_RECIPE, recipe_id, f"{path}.name"
                    )
                )
            else:
                target_steps.setdefault(step_name, set()).update(old_names_present)

    combine_changed = False
    if combine is not None:
        combine_references, combine_issues = inspect_combine_tree(combine)
        for issue in combine_issues:
            if _json_contains_exact(issue.scope, name_set):
                unsafe.append(
                    UnsafeRenameReference(
                        CONSUMER_TYPE_CROSS_MODEL_RECIPE, recipe_id, issue.path
                    )
                )
        for reference in combine_references:
            if (
                reference.step not in target_step_names
                or reference.measure not in names
            ):
                continue
            if step_name_counts[reference.step] != 1:
                unsafe.append(
                    UnsafeRenameReference(
                        CONSUMER_TYPE_CROSS_MODEL_RECIPE,
                        recipe_id,
                        f"{reference.path}.step",
                    )
                )
            elif reference.measure not in target_steps.get(reference.step, set()):
                unsafe.append(
                    UnsafeRenameReference(
                        CONSUMER_TYPE_CROSS_MODEL_RECIPE,
                        recipe_id,
                        f"{reference.path}.measure",
                    )
                )
            else:
                reference.container["measure"] = names[reference.measure]
                combine_changed = True
    return steps, combine, unsafe, steps_changed, combine_changed


def _rewrite_alias_consumer(
    alias_row: Any,
    names: dict[str, str],
) -> tuple[dict[str, Any], list[UnsafeRenameReference], bool]:
    """Plan exact canonical alias-value rewrites; reject malformed values."""
    alias_map = deepcopy(getattr(alias_row, "alias_map", None))
    alias_id = str(alias_row.model_id)
    if not isinstance(alias_map, dict):
        unsafe = (
            [UnsafeRenameReference(CONSUMER_TYPE_MODEL_ALIAS_MAP, alias_id, "$.alias_map")]
            if _json_contains_exact(alias_map, set(names))
            else []
        )
        return alias_map, unsafe, False

    unsafe: list[UnsafeRenameReference] = []
    changed = False
    for phrase, canonical in alias_map.items():
        path = f"$.alias_map[{phrase!r}]"
        if not isinstance(canonical, str):
            if _json_contains_exact(canonical, set(names)):
                unsafe.append(
                    UnsafeRenameReference(CONSUMER_TYPE_MODEL_ALIAS_MAP, alias_id, path)
                )
            continue
        replacement = names.get(canonical)
        if replacement is not None:
            alias_map[phrase] = replacement
            changed = True
    return alias_map, unsafe, changed


@dataclass(frozen=True)
class RenameImpactItem:
    """One consumer a rename touches, in a shape a UI can list."""
    consumer_type: str
    consumer_id: str
    consumer_name: str | None
    field: str
    visible_to: str | None = None


@dataclass(frozen=True)
class MeasureRenamePlan:
    """What a rename WOULD do, computed without mutating anything (Bug-9394).

    ``rewrites`` are consumers the rename rewrites automatically;
    ``blockers`` are references it cannot safely rewrite, each of which makes
    the rename fail with 409. ``safe`` is the single question the confirmation
    dialog needs answered.
    """
    rewrites: list[RenameImpactItem]
    blockers: list[RenameImpactItem]

    @property
    def safe(self) -> bool:
        return not self.blockers

    def for_viewer(self, viewer_identity: str) -> "MeasureRenamePlan":
        """Hide exact details for another user's personal artifacts.

        Safe rewrites stay invisible. A private blocker keeps ``safe`` false but
        is represented once per consumer type without an id, name, or field.
        """
        rewrites = [
            item
            for item in self.rewrites
            if item.visible_to is None or item.visible_to == viewer_identity
        ]
        blockers: list[RenameImpactItem] = []
        redacted_types: set[str] = set()
        for item in self.blockers:
            if item.visible_to is None or item.visible_to == viewer_identity:
                blockers.append(item)
            elif item.consumer_type not in redacted_types:
                blockers.append(
                    RenameImpactItem(
                        consumer_type=item.consumer_type,
                        consumer_id="",
                        consumer_name=None,
                        field="$private",
                    )
                )
                redacted_types.add(item.consumer_type)
        return MeasureRenamePlan(rewrites=rewrites, blockers=blockers)


def _consumer_name(row: Any) -> str | None:
    return (
        getattr(row, "display_name", None)
        or getattr(row, "name", None)
        or getattr(row, "title", None)
    )


async def plan_measure_renames(
    db: Any,
    model_id: UUID,
    renames: dict[UUID, tuple[str, str]],
    *,
    for_update: bool = True,
) -> tuple[list[tuple[Any, str, Any]], list[Any], list[UnsafeRenameReference], MeasureRenamePlan]:
    """Enumerate every owned consumer of the renamed measures WITHOUT mutating.

    Bug-9394: the confirmation dialog and the rename itself must be answered by
    the SAME enumeration — a preview computed by a second, parallel walk drifts
    from the writer the first time a consumer type is added on one side only.
    ``propagate_measure_renames`` applies this plan; the rename-impact endpoint
    renders it.

    *for_update* takes the recipe row lock, which the applying path needs and a
    read-only preview must not.

    Returns ``(field_updates, coverage_to_invalidate, unsafe, plan)``.
    """
    names = {old: new for old, new in renames.values() if old != new}
    if not names:
        return [], [], [], MeasureRenamePlan(rewrites=[], blockers=[])
    name_set = set(names)

    calc_rows = list((await db.execute(
        select(Measure).where(
            Measure.model_id == model_id,
            Measure.measure_type == "calculated",
        )
    )).scalars().all())
    kpi_rows = list((await db.execute(
        select(KPI).where(KPI.model_id == model_id)
    )).scalars().all())
    saved_rows = list((await db.execute(
        select(SavedQuery).where(SavedQuery.model_id == model_id)
    )).scalars().all())
    named_rows = list((await db.execute(
        select(NamedSet).where(NamedSet.model_id == model_id)
    )).scalars().all())
    scratchpad_rows = list((await db.execute(
        select(ScratchpadMeasure).where(ScratchpadMeasure.model_id == model_id)
    )).scalars().all())
    coverage_rows = list((await db.execute(
        select(QuantileCoverage)
        .join(
            AggregateDefinition,
            QuantileCoverage.aggregate_definition_id == AggregateDefinition.id,
        )
        .where(AggregateDefinition.model_id == model_id)
    )).scalars().all())
    recipe_query = (
        select(ProjectCrossModelRecipe)
        .join(Model, ProjectCrossModelRecipe.project_id == Model.project_id)
        .where(Model.id == model_id)
    )
    if for_update:
        recipe_query = recipe_query.with_for_update(of=ProjectCrossModelRecipe)
    recipe_rows = list((await db.execute(recipe_query)).scalars().all())
    alias_rows = list((await db.execute(
        select(ModelAliasMap).where(ModelAliasMap.model_id == model_id)
    )).scalars().all())
    candidate_dimension_names = name_set | set(names.values())
    dimension_names = set((await db.execute(
        select(Dimension.name).where(
            Dimension.model_id == model_id,
            Dimension.name.in_(candidate_dimension_names),
        )
    )).scalars().all())
    ambiguous_saved_query_names = {
        old_name
        for old_name, new_name in names.items()
        if old_name in dimension_names or new_name in dimension_names
    }

    unsafe: list[UnsafeRenameReference] = []
    field_updates: list[tuple[Any, str, Any]] = []
    coverage_to_invalidate: list[Any] = []
    rewrite_items: list[RenameImpactItem] = []

    def _record(consumer_type: str, row: Any, field: str, value: Any) -> None:
        """Queue one rewrite AND its impact-preview entry in one place.

        The consumer type is named HERE, where the walk already knows it, so
        the preview cannot mislabel a consumer or silently omit a new one.
        """
        field_updates.append((row, field, value))
        visible_to = None
        if (
            consumer_type == CONSUMER_TYPE_SAVED_QUERY
            and not getattr(row, "is_shared", False)
        ) or consumer_type == CONSUMER_TYPE_SCRATCHPAD_MEASURE:
            visible_to = str(getattr(row, "created_by", "") or "") or None
        rewrite_items.append(
            RenameImpactItem(
                consumer_type=consumer_type,
                consumer_id=str(getattr(row, "id", "")),
                consumer_name=_consumer_name(row),
                field=field,
                visible_to=visible_to,
            )
        )
    for measure in calc_rows:
        if measure.id in renames:
            continue
        rewritten, changed = rewrite_measure_calls(measure.expression or "", names)
        if changed:
            _record(CONSUMER_TYPE_MEASURE, measure, "expression", rewritten)
        elif _contains_name(measure.expression, name_set):
            unsafe.append(
                UnsafeRenameReference(CONSUMER_TYPE_MEASURE, str(measure.id), "expression")
            )

    for kpi in kpi_rows:
        for field in ("expression", "target_expression"):
            value = getattr(kpi, field, None)
            rewritten, changed = rewrite_measure_calls(value or "", names)
            if changed:
                _record(CONSUMER_TYPE_KPI, kpi, field, rewritten)
            elif _contains_name(value, name_set):
                unsafe.append(UnsafeRenameReference(CONSUMER_TYPE_KPI, str(kpi.id), field))
        for field in ("status_expression", "trend_expression"):
            if _contains_name(getattr(kpi, field, None), name_set):
                unsafe.append(UnsafeRenameReference(CONSUMER_TYPE_KPI, str(kpi.id), field))
        rewritten_json, changed = _rewrite_json(
            getattr(kpi, "business_definition", None), names
        )
        if changed:
            _record(CONSUMER_TYPE_KPI, kpi, "business_definition", rewritten_json)

    for saved in saved_rows:
        if not _contains_name(saved.query_text, name_set):
            continue
        saved_visible_to = (
            str(getattr(saved, "created_by", "") or "") or None
            if not getattr(saved, "is_shared", False)
            else None
        )
        # Bug-8829: a sqlglot Column carries syntax, not semantic identity.
        # Models may legally contain a measure and dimension with the same
        # name, so blindly renaming every matching Column also renames the
        # grouping/filter dimension. Resolving individual occurrences would
        # require the protected semantic binder; this consumer therefore
        # fails closed on the ambiguous cross-type name and leaves every
        # planned update unapplied.
        if _contains_name(saved.query_text, ambiguous_saved_query_names):
            unsafe.append(
                UnsafeRenameReference(
                    CONSUMER_TYPE_SAVED_QUERY,
                    str(saved.id),
                    "query_text",
                    visible_to=saved_visible_to,
                )
            )
            continue
        if str(saved.query_type or "sql").lower() != "sql":
            unsafe.append(
                UnsafeRenameReference(
                    CONSUMER_TYPE_SAVED_QUERY,
                    str(saved.id),
                    "query_text",
                    visible_to=saved_visible_to,
                )
            )
            continue
        try:
            rewritten, changed = rewrite_semantic_sql(saved.query_text, names)
        except sqlglot.errors.ParseError:
            unsafe.append(
                UnsafeRenameReference(
                    CONSUMER_TYPE_SAVED_QUERY,
                    str(saved.id),
                    "query_text",
                    visible_to=saved_visible_to,
                )
            )
            continue
        if changed:
            _record(CONSUMER_TYPE_SAVED_QUERY, saved, "query_text", rewritten)
        else:
            unsafe.append(
                UnsafeRenameReference(
                    CONSUMER_TYPE_SAVED_QUERY,
                    str(saved.id),
                    "query_text",
                    visible_to=saved_visible_to,
                )
            )

    for named_set in named_rows:
        expression_hit = _contains_name(named_set.expression, name_set)
        builder_hit = _contains_name(repr(named_set.builder_definition or {}), name_set)
        if expression_hit or builder_hit:
            unsafe.append(
                UnsafeRenameReference(
                    CONSUMER_TYPE_NAMED_SET,
                    str(named_set.id),
                    "expression" if expression_hit else "builder_definition",
                )
            )

    for scratchpad in scratchpad_rows:
        rewritten, changed = rewrite_measure_calls(scratchpad.expression or "", names)
        if changed:
            _record(CONSUMER_TYPE_SCRATCHPAD_MEASURE, scratchpad, "expression", rewritten)
        elif _contains_name(scratchpad.expression, name_set):
            unsafe.append(
                UnsafeRenameReference(
                    CONSUMER_TYPE_SCRATCHPAD_MEASURE, str(scratchpad.id),
                    "expression",
                    visible_to=(
                        str(getattr(scratchpad, "created_by", "") or "") or None
                    ),
                )
            )

    renamed_ids = set(renames)
    for coverage in coverage_rows:
        # Quantile proof fingerprints include the semantic measure name. The
        # physical aggregate remains valid data, but its old proof must not be
        # served under the renamed semantic identity. Deleting the proof row is
        # the established fail-closed invalidation: routing falls back to source
        # until the aggregate is refreshed/rebuilt and the producer writes a new
        # coverage row with the new name/fingerprint.
        if (
            getattr(coverage, "measure_id", None) in renamed_ids
            or getattr(coverage, "semantic_measure_name", None) in names
        ):
            coverage_to_invalidate.append(coverage)

    for recipe in recipe_rows:
        (
            rewritten_steps,
            rewritten_combine,
            recipe_unsafe,
            steps_changed,
            combine_changed,
        ) = _rewrite_recipe_consumer(recipe, model_id, names)
        unsafe.extend(recipe_unsafe)
        if steps_changed:
            _record(CONSUMER_TYPE_CROSS_MODEL_RECIPE, recipe, "steps", rewritten_steps)
        if combine_changed:
            _record(CONSUMER_TYPE_CROSS_MODEL_RECIPE, recipe, "combine", rewritten_combine)

    for alias_row in alias_rows:
        rewritten_aliases, alias_unsafe, changed = _rewrite_alias_consumer(
            alias_row, names
        )
        unsafe.extend(alias_unsafe)
        if changed:
            _record(CONSUMER_TYPE_MODEL_ALIAS_MAP, alias_row, "alias_map", rewritten_aliases)

    plan = MeasureRenamePlan(
        rewrites=rewrite_items + [
            RenameImpactItem(
                consumer_type=CONSUMER_TYPE_QUANTILE_COVERAGE,
                consumer_id=str(getattr(cov, "id", "")),
                consumer_name=getattr(cov, "semantic_measure_name", None),
                field="$invalidated",
            )
            for cov in coverage_to_invalidate
        ],
        blockers=[
            RenameImpactItem(
                consumer_type=ref.consumer_type,
                consumer_id=ref.consumer_id,
                consumer_name=None,
                field=ref.field,
                visible_to=ref.visible_to,
            )
            for ref in unsafe
        ],
    )
    return field_updates, coverage_to_invalidate, unsafe, plan



async def propagate_measure_renames(
    db: Any,
    model_id: UUID,
    renames: dict[UUID, tuple[str, str]],
) -> None:
    """Rewrite safe owned consumers or fail before the rename commits.

    Stable-ID consumers (variants, pivots, drill-through, persona allowlists,
    KPI target FKs and aggregate columns) require no mutation. Historical query
    logs and immutable deployed snapshots are deliberately not rewritten.
    """
    field_updates, coverage_to_invalidate, unsafe, _plan = (
        await plan_measure_renames(db, model_id, renames, for_update=True)
    )

    if unsafe:
        raise UnsafeMeasureRename(unsafe)

    # Apply only after the complete consumer inventory has proved safe. This
    # prevents a later unsupported reference from leaving earlier ORM rows
    # dirty in the request transaction before the API returns 409.
    for row, field, value in field_updates:
        setattr(row, field, value)
    for coverage in coverage_to_invalidate:
        await db.delete(coverage)
