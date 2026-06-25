import type { Measure } from '../types/tessallite';

export interface ResultAnnotation {
  measures?: Record<string, { title: string; type: string; format?: string }>;
  dimensions?: Record<string, { title: string; type: string }>;
  timeDimensions?: Record<string, { title: string; type: string }>;
}

export interface TableDrillMetadata {
  formatTokens: Record<string, string>;
  measureColumns: Record<string, string>;
  dimensionColumns: Record<string, string>;
}

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

export function buildTableDrillMetadata(
  annotation: ResultAnnotation | undefined,
  measures: Measure[] = [],
): TableDrillMetadata {
  const formatTokens: Record<string, string> = {};
  const measureColumns: Record<string, string> = {};
  const dimensionColumns: Record<string, string> = {};

  const byName = new Map(measures.map(m => [m.name, m.id]));
  const byDisplayName = new Map(measures.map(m => [m.display_name, m.id]));
  const byId = new Set(measures.map(m => m.id));

  for (const [key, m] of Object.entries(annotation?.measures || {})) {
    if (m.format) formatTokens[key] = m.format;
    if (m.title && m.format) formatTokens[m.title] = m.format;
    const measureId = resolveMeasureId(key, m.title, byName, byDisplayName, byId);
    if (m.title && measureId) {
      measureColumns[m.title] = measureId;
    }
  }

  for (const [name, d] of Object.entries(annotation?.dimensions || {})) {
    if (d.title) dimensionColumns[d.title] = name;
  }
  for (const [name, d] of Object.entries(annotation?.timeDimensions || {})) {
    if (d.title) dimensionColumns[d.title] = name;
  }

  return { formatTokens, measureColumns, dimensionColumns };
}

function resolveMeasureId(
  key: string,
  title: string | undefined,
  byName: Map<string, string>,
  byDisplayName: Map<string, string>,
  byId: Set<string>,
): string | undefined {
  if (byId.has(key) || UUID_RE.test(key)) return key;
  return byName.get(key) || (title ? byDisplayName.get(title) : undefined);
}
