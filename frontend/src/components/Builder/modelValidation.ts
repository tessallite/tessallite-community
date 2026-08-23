/**
 * Model validation producer (F-026-01).
 *
 * Two sources feed the validation tray:
 *
 * 1. Server alerts — the structural validator in
 *    `shared/semantic/model_validator.py` writes ModelAlert rows (invalid
 *    dimensions/measures/aggregates, refresh failures, schema drift, ...)
 *    which the model-service exposes at
 *    `GET /projects/{p}/models/{m}/alerts`. `mapAlertsToIssues` converts the
 *    open alerts into tray issues.
 *
 * 2. Client-side structural rules — cheap checks over data the builder has
 *    already fetched (`computeStructuralIssues`): no fact table, isolated
 *    tables, missing query target.
 *
 * Pure functions — the React wiring lives in `useModelValidation.ts`.
 */
import type { Join, ModelAlert, ModelTable } from "../../api/types";
import type { ObjectType, ValidationIssue, ValidationSeverity } from "../../store/builderStore";

type Translate = (key: string, vars?: Record<string, string>) => string;

/** Backend alert severities → tray severities (MUI Alert palette). */
const ALERT_SEVERITY: Record<string, ValidationSeverity> = {
  critical: "error",
  error: "error",
  warning: "warning",
  info: "info",
};

/** Backend alert categories with a localised label. */
const ALERT_CATEGORY_KEYS: Record<string, string> = {
  invalid_dimension: "validation.category.invalid_dimension",
  invalid_measure: "validation.category.invalid_measure",
  invalid_aggregate: "validation.category.invalid_aggregate",
  refresh_failure: "validation.category.refresh_failure",
  optimiser_failure: "validation.category.optimiser_failure",
  query_fallback: "validation.category.query_fallback",
  schema_drift: "validation.category.schema_drift",
  data_quality: "validation.category.data_quality",
};

/** related_object_type values that map onto navigable builder objects. */
const NAVIGABLE_OBJECT_TYPES = new Set<ObjectType>([
  "dimension",
  "measure",
  "aggregate",
]);

function humaniseCategory(category: string): string {
  return category.replace(/_/g, " ");
}

/**
 * Display-layer mitigation for backend alert detail that leaks a raw model
 * UUID (e.g. the optimiser's "… on model ed2d141a-…", review finding 5).
 * Drops the trailing " on model <uuid>" qualifier (the tray is already scoped
 * to one model, so the id is noise) and elides any other bare UUID token to a
 * short prefix. This is a presentation nicety only — the producer-side fix
 * (use the model name, not the id) is tracked separately (Bug-1059, owner H4).
 */
const UUID_RE =
  /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/gi;

function elideUuids(text: string): string {
  return text
    .replace(/\s+on model\s+[0-9a-f-]{36}\b/gi, "")
    .replace(UUID_RE, (m) => `${m.slice(0, 8)}…`);
}

export function mapAlertsToIssues(
  t: Translate,
  alerts: ModelAlert[],
): ValidationIssue[] {
  const issues: ValidationIssue[] = [];
  for (const alert of alerts) {
    if (alert.resolved_at || alert.dismissed_at) continue;
    const categoryKey = ALERT_CATEGORY_KEYS[alert.category];
    const label = categoryKey ? t(categoryKey) : humaniseCategory(alert.category);
    const body = elideUuids(alert.detail || alert.title);
    const affectedType =
      alert.related_object_type &&
      NAVIGABLE_OBJECT_TYPES.has(alert.related_object_type as ObjectType)
        ? (alert.related_object_type as ObjectType)
        : undefined;
    issues.push({
      id: `alert-${alert.id}`,
      severity: ALERT_SEVERITY[alert.severity] ?? "warning",
      message: `${label}: ${body}`,
      affectedType,
      affectedObject:
        affectedType && alert.related_object_id
          ? alert.related_object_id
          : undefined,
    });
  }
  return issues;
}

function tableLabel(table: ModelTable): string {
  return table.display_name || table.alias || table.physical_name;
}

export function computeStructuralIssues(
  t: Translate,
  tables: ModelTable[],
  joins: Join[],
  hasTarget: boolean,
): ValidationIssue[] {
  const issues: ValidationIssue[] = [];
  if (tables.length === 0) return issues;

  // Rule 1 — a multi-table model needs one declared fact anchor. A
  // single-table model is implicitly fact at deploy (Bug-8614).
  const hasFact = tables.some((tb) => tb.table_type === "fact");
  if (tables.length > 1 && !hasFact) {
    issues.push({
      id: "struct-no-fact-table",
      severity: "warning",
      message: t("validation.noFactTable"),
    });
  }

  // Rule 2 — isolated tables: not an endpoint of any join (only meaningful
  // once the model has more than one table). Joins to hidden calendar tables
  // still appear in the joins list, so calendar-linked tables count as joined.
  if (tables.length >= 2) {
    const joinedIds = new Set<string>();
    for (const j of joins) {
      joinedIds.add(j.left_table_id);
      joinedIds.add(j.right_table_id);
    }
    for (const tb of tables) {
      if (!joinedIds.has(tb.id)) {
        issues.push({
          id: `struct-isolated-${tb.id}`,
          severity: "warning",
          message: t("validation.isolatedTable", { name: tableLabel(tb) }),
          tableId: tb.id,
          sourceId: tb.source_id,
        });
      }
    }
  }

  // Rule 3 — no query target: aggregates and pocket tables cannot be
  // materialised. Informational; the model is still queryable on source.
  if (!hasTarget) {
    issues.push({
      id: "struct-no-target",
      severity: "info",
      message: t("validation.noTarget"),
    });
  }

  return issues;
}
