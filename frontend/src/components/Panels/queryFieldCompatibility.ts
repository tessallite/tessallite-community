import type { QueryRouterFieldCompatibilityFeedback } from "../../api/types";

export const SEMANTIC_COMPATIBILITY_NOT_ANALYZED =
  "SEMANTIC_COMPATIBILITY_NOT_ANALYZED";

export interface QueryFieldCompatibilityContext {
  sql: string;
  dialect: string;
  personaId?: string | null;
  forceRoute?: string | null;
}

type RecordValue = Record<string, unknown>;

function isRecord(value: unknown): value is RecordValue {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((entry) => typeof entry === "string");
}

export function isQueryFieldCompatibilityFeedback(
  value: unknown,
): value is QueryRouterFieldCompatibilityFeedback {
  if (!isRecord(value)) return false;
  if (
    value.status !== "compatible" &&
    value.status !== "incompatible" &&
    value.status !== "not_analyzed"
  ) {
    return false;
  }
  if (!Array.isArray(value.issues)) return false;
  return value.issues.every((issue) => {
    if (!isRecord(issue)) return false;
    if (typeof issue.code !== "string") return false;
    if (typeof issue.message !== "string") return false;
    if (
      issue.severity !== undefined &&
      issue.severity !== "error" &&
      issue.severity !== "warning"
    ) {
      return false;
    }
    if (
      issue.measure_name !== undefined &&
      issue.measure_name !== null &&
      typeof issue.measure_name !== "string"
    ) {
      return false;
    }
    if (
      issue.dimension_name !== undefined &&
      issue.dimension_name !== null &&
      typeof issue.dimension_name !== "string"
    ) {
      return false;
    }
    return (
      issue.compatible_dimension_names === undefined ||
      isStringArray(issue.compatible_dimension_names)
    );
  });
}

export function extractQueryFieldCompatibilityFromError(
  err: unknown,
): QueryRouterFieldCompatibilityFeedback | null {
  const detail = isRecord(err)
    ? isRecord(err.response) &&
      isRecord(err.response.data) &&
      "detail" in err.response.data
      ? err.response.data.detail
      : undefined
    : undefined;
  if (!isRecord(detail)) return null;
  return isQueryFieldCompatibilityFeedback(detail.field_compatibility)
    ? detail.field_compatibility
    : null;
}

export function hasNotAnalyzedFieldCompatibility(
  feedback: QueryRouterFieldCompatibilityFeedback | null | undefined,
): boolean {
  return Boolean(
    feedback &&
      (feedback.status === "not_analyzed" ||
        feedback.issues.some(
          (issue) => issue.code === SEMANTIC_COMPATIBILITY_NOT_ANALYZED,
        )),
  );
}

export function hasBlockingFieldCompatibility(
  feedback: QueryRouterFieldCompatibilityFeedback | null | undefined,
): boolean {
  return Boolean(
    feedback &&
      feedback.status === "incompatible" &&
      !hasNotAnalyzedFieldCompatibility(feedback),
  );
}

export function fieldCompatibilityValidationContextKey(
  context: QueryFieldCompatibilityContext,
): string {
  return JSON.stringify({
    sql: context.sql,
    dialect: context.dialect,
    personaId: context.personaId ?? null,
    forceRoute: context.forceRoute || null,
  });
}

export function fieldCompatibilityMessages(
  feedback: QueryRouterFieldCompatibilityFeedback | null | undefined,
): string[] {
  if (!feedback) return [];
  return dedupe(
    feedback.issues
      .map((issue) => issue.message.trim())
      .filter((message) => message.length > 0),
  );
}

export function fieldCompatibilityCompatibleDimensionNames(
  feedback: QueryRouterFieldCompatibilityFeedback | null | undefined,
): string[] {
  if (!feedback) return [];
  return dedupe(
    feedback.issues.flatMap((issue) => issue.compatible_dimension_names ?? []),
  );
}

export function shouldRenderFieldCompatibility(
  feedback: QueryRouterFieldCompatibilityFeedback | null | undefined,
): boolean {
  return Boolean(
    feedback &&
      (feedback.status !== "compatible" ||
        feedback.issues.length > 0 ||
        hasNotAnalyzedFieldCompatibility(feedback)),
  );
}

function dedupe(values: string[]): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const value of values) {
    if (!seen.has(value)) {
      seen.add(value);
      out.push(value);
    }
  }
  return out;
}
