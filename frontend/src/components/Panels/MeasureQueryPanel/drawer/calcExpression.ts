import type { Measure } from "../../../../api/types";

const MEASURE_REF_RE = /measure\(\s*["']([^"']+)["']\s*\)/g;

export function extractReferencedMeasureNames(expression: string | null | undefined): string[] {
  if (!expression) return [];
  const seen = new Set<string>();
  const ordered: string[] = [];
  MEASURE_REF_RE.lastIndex = 0;
  let m: RegExpExecArray | null;
  while ((m = MEASURE_REF_RE.exec(expression)) !== null) {
    const name = m[1];
    if (!seen.has(name)) {
      seen.add(name);
      ordered.push(name);
    }
  }
  return ordered;
}

export function resolveReferencedMeasures(
  expression: string | null | undefined,
  allMeasures: Measure[],
): { resolved: Measure[]; unresolvedNames: string[] } {
  const names = extractReferencedMeasureNames(expression);
  const byName = new Map(allMeasures.map((x) => [x.name, x]));
  const resolved: Measure[] = [];
  const unresolvedNames: string[] = [];
  for (const n of names) {
    const m = byName.get(n);
    if (m) resolved.push(m);
    else unresolvedNames.push(n);
  }
  return { resolved, unresolvedNames };
}
