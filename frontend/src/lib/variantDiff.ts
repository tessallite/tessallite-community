import type { Measure } from "../api/types";
import {
  TIME_VARIANT_DEFAULT_N,
  isParametricVariant,
} from "../constants/timeVariants";

export interface DesiredVariant {
  kind: string;
  n?: number | null;
}

export interface VariantCreatePlan {
  kind: string;
  n: number | null;
}

export interface VariantDiff {
  toCreate: VariantCreatePlan[];
  toDelete: string[];
  toUpdateN: Array<{ id: string; n: number | null }>;
}

function existingVariantsForBase(
  baseId: string,
  measures: Measure[],
): Measure[] {
  return measures.filter((m) => m.variant_of_measure_id === baseId);
}

export function diffVariants(
  baseId: string,
  measures: Measure[],
  desired: DesiredVariant[],
): VariantDiff {
  const existing = existingVariantsForBase(baseId, measures);
  const existingByKind = new Map<string, Measure>();
  for (const m of existing) {
    if (m.variant_kind) existingByKind.set(m.variant_kind, m);
  }
  const desiredByKind = new Map<string, DesiredVariant>();
  for (const d of desired) desiredByKind.set(d.kind, d);

  const toCreate: VariantCreatePlan[] = [];
  const toDelete: string[] = [];
  const toUpdateN: Array<{ id: string; n: number | null }> = [];

  for (const [kind, d] of desiredByKind) {
    const cur = existingByKind.get(kind);
    const desiredN = isParametricVariant(kind)
      ? d.n ?? TIME_VARIANT_DEFAULT_N[kind] ?? null
      : null;
    if (!cur) {
      toCreate.push({ kind, n: desiredN });
    } else if (
      isParametricVariant(kind) &&
      (cur.variant_n ?? null) !== desiredN
    ) {
      toUpdateN.push({ id: cur.id, n: desiredN });
    }
  }

  for (const [kind, m] of existingByKind) {
    if (!desiredByKind.has(kind)) toDelete.push(m.id);
  }

  return { toCreate, toDelete, toUpdateN };
}
