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


_MEASURE_CALL_RE = re.compile(
    r"(?P<prefix>\bmeasure\s*\(\s*)(?P<quote>['\"])(?P<name>[^'\"]+)"
    r"(?P=quote)(?P<suffix>\s*\))",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class UnsafeRenameReference:
    consumer_type: str
    consumer_id: str
    field: str


class UnsafeMeasureRename(ValueError):
    def __init__(self, references: list[UnsafeRenameReference]):
        self.references = references
        detail = ", ".join(
            f"{r.consumer_type}:{r.consumer_id}"
            f"{r.field if r.field.startswith('$') else '.' + r.field}"
            for r in references
        )
        super().__init__(
            "Measure rename cannot safely rewrite these model-owned references: "
            + detail
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


def _rewrite_json(value: Any, names: dict[str, str]) -> tuple[Any, bool]:
    if isinstance(value, dict):
        changed = False
        out = {}
        for key, item in value.items():
            rewritten, item_changed = _rewrite_json(item, names)
            out[key] = rewritten
            changed = changed or item_changed
        return out, changed
    if isinstance(value, list):
        changed = False
        out = []
        for item in value:
            rewritten, item_changed = _rewrite_json(item, names)
            out.append(rewritten)
            changed = changed or item_changed
        return out, changed
    if isinstance(value, str):
        if value in names:
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
                UnsafeRenameReference("cross_model_recipe", recipe_id, "$.steps")
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
                    UnsafeRenameReference("cross_model_recipe", recipe_id, path)
                )
            continue

        measures = step.get("measures")
        try:
            step_model_id = UUID(str(step.get("model_id")))
        except (TypeError, ValueError, AttributeError):
            if _json_contains_exact(measures, name_set):
                unsafe.append(
                    UnsafeRenameReference(
                        "cross_model_recipe", recipe_id, f"{path}.model_id"
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
                        "cross_model_recipe", recipe_id, f"{path}.measures"
                    )
                )
            continue

        old_names_present: set[str] = set()
        for measure_index, measure_name in enumerate(measures):
            if not isinstance(measure_name, str):
                if _json_contains_exact(measure_name, name_set):
                    unsafe.append(
                        UnsafeRenameReference(
                            "cross_model_recipe",
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
                        "cross_model_recipe", recipe_id, f"{path}.name"
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
                        "cross_model_recipe", recipe_id, issue.path
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
                        "cross_model_recipe",
                        recipe_id,
                        f"{reference.path}.step",
                    )
                )
            elif reference.measure not in target_steps.get(reference.step, set()):
                unsafe.append(
                    UnsafeRenameReference(
                        "cross_model_recipe",
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
            [UnsafeRenameReference("model_alias_map", alias_id, "$.alias_map")]
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
                    UnsafeRenameReference("model_alias_map", alias_id, path)
                )
            continue
        replacement = names.get(canonical)
        if replacement is not None:
            alias_map[phrase] = replacement
            changed = True
    return alias_map, unsafe, changed


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
    names = {old: new for old, new in renames.values() if old != new}
    if not names:
        return
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
    recipe_rows = list((await db.execute(
        select(ProjectCrossModelRecipe)
        .join(Model, ProjectCrossModelRecipe.project_id == Model.project_id)
        .where(Model.id == model_id)
        .with_for_update(of=ProjectCrossModelRecipe)
    )).scalars().all())
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
    for measure in calc_rows:
        if measure.id in renames:
            continue
        rewritten, changed = rewrite_measure_calls(measure.expression or "", names)
        if changed:
            field_updates.append((measure, "expression", rewritten))
        elif _contains_name(measure.expression, name_set):
            unsafe.append(
                UnsafeRenameReference("measure", str(measure.id), "expression")
            )

    for kpi in kpi_rows:
        for field in ("expression", "target_expression"):
            value = getattr(kpi, field, None)
            rewritten, changed = rewrite_measure_calls(value or "", names)
            if changed:
                field_updates.append((kpi, field, rewritten))
            elif _contains_name(value, name_set):
                unsafe.append(UnsafeRenameReference("kpi", str(kpi.id), field))
        for field in ("status_expression", "trend_expression"):
            if _contains_name(getattr(kpi, field, None), name_set):
                unsafe.append(UnsafeRenameReference("kpi", str(kpi.id), field))
        rewritten_json, changed = _rewrite_json(
            getattr(kpi, "business_definition", None), names
        )
        if changed:
            field_updates.append((kpi, "business_definition", rewritten_json))

    for saved in saved_rows:
        if not _contains_name(saved.query_text, name_set):
            continue
        # Bug-8829: a sqlglot Column carries syntax, not semantic identity.
        # Models may legally contain a measure and dimension with the same
        # name, so blindly renaming every matching Column also renames the
        # grouping/filter dimension. Resolving individual occurrences would
        # require the protected semantic binder; this consumer therefore
        # fails closed on the ambiguous cross-type name and leaves every
        # planned update unapplied.
        if _contains_name(saved.query_text, ambiguous_saved_query_names):
            unsafe.append(
                UnsafeRenameReference("saved_query", str(saved.id), "query_text")
            )
            continue
        if str(saved.query_type or "sql").lower() != "sql":
            unsafe.append(
                UnsafeRenameReference("saved_query", str(saved.id), "query_text")
            )
            continue
        try:
            rewritten, changed = rewrite_semantic_sql(saved.query_text, names)
        except sqlglot.errors.ParseError:
            unsafe.append(
                UnsafeRenameReference("saved_query", str(saved.id), "query_text")
            )
            continue
        if changed:
            field_updates.append((saved, "query_text", rewritten))
        else:
            unsafe.append(
                UnsafeRenameReference("saved_query", str(saved.id), "query_text")
            )

    for named_set in named_rows:
        expression_hit = _contains_name(named_set.expression, name_set)
        builder_hit = _contains_name(repr(named_set.builder_definition or {}), name_set)
        if expression_hit or builder_hit:
            unsafe.append(
                UnsafeRenameReference(
                    "named_set",
                    str(named_set.id),
                    "expression" if expression_hit else "builder_definition",
                )
            )

    for scratchpad in scratchpad_rows:
        rewritten, changed = rewrite_measure_calls(scratchpad.expression or "", names)
        if changed:
            field_updates.append((scratchpad, "expression", rewritten))
        elif _contains_name(scratchpad.expression, name_set):
            unsafe.append(
                UnsafeRenameReference(
                    "scratchpad_measure", str(scratchpad.id), "expression"
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
            field_updates.append((recipe, "steps", rewritten_steps))
        if combine_changed:
            field_updates.append((recipe, "combine", rewritten_combine))

    for alias_row in alias_rows:
        rewritten_aliases, alias_unsafe, changed = _rewrite_alias_consumer(
            alias_row, names
        )
        unsafe.extend(alias_unsafe)
        if changed:
            field_updates.append((alias_row, "alias_map", rewritten_aliases))

    if unsafe:
        raise UnsafeMeasureRename(unsafe)

    # Apply only after the complete consumer inventory has proved safe. This
    # prevents a later unsupported reference from leaving earlier ORM rows
    # dirty in the request transaction before the API returns 409.
    for row, field, value in field_updates:
        setattr(row, field, value)
    for coverage in coverage_to_invalidate:
        await db.delete(coverage)
