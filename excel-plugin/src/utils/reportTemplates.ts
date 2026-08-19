/**
 * Report templates utility.
 * Defines pre-configured report patterns that populate the zone mapping grid.
 */

import type { Zone } from '../types/tessallite';

export interface ReportTemplate {
  id: string;
  name: string;
  description: string;
  icon: string;
  requiresMeasure?: boolean;
  requiresTimeDimension?: boolean;
  requiresCategoricalDimension?: boolean;
  requiresComparisonMeasure?: boolean;
  /**
   * Bug-6365: when set, applying the template ranks the result by its primary
   * measure (descending) and caps it to this many rows — the actual "top-N"
   * behaviour the Top N Breakdown template promises. The zone-query builder
   * receives the sort selection and row limit from the applying component.
   */
  topN?: number;
}

export const REPORT_TEMPLATES: ReportTemplate[] = [
  {
    id: 'time-series',
    name: 'Time Series',
    description: 'Track a measure over time with date on rows',
    icon: 'Timeline',
    requiresMeasure: true,
    requiresTimeDimension: true,
  },
  {
    id: 'top-n',
    name: 'Top N Breakdown',
    description: 'Rank entities by a measure with a top-10 filter',
    icon: 'Leaderboard',
    requiresMeasure: true,
    requiresCategoricalDimension: true,
    topN: 10,
  },
  {
    id: 'period-comparison',
    name: 'Period Comparison',
    description: 'Compare two time periods side by side',
    icon: 'CompareArrows',
    requiresMeasure: true,
    requiresTimeDimension: true,
  },
  {
    id: 'geographic',
    name: 'Geographic Breakdown',
    description: 'Analyze a measure across geographic regions',
    icon: 'Public',
    requiresMeasure: true,
    requiresCategoricalDimension: true,
  },
  {
    id: 'variance',
    name: 'Variance Analysis',
    description: 'Compare actual vs target with dimension rows',
    icon: 'ShowChart',
    requiresMeasure: true,
    requiresCategoricalDimension: true,
    requiresComparisonMeasure: true,
  },
  {
    id: 'kpi-snapshot',
    name: 'KPI Snapshot',
    description: 'View key metrics at a glance',
    icon: 'Assessment',
    requiresMeasure: true,
  },
];

/**
 * Check if a template can be applied given the available model fields.
 */
export function checkTemplatePrerequisites(
  template: ReportTemplate,
  measureCount: number,
  hasTimeDimension: boolean,
  hasCategoricalDimension: boolean,
): { valid: boolean; missing: string[] } {
  const missing: string[] = [];

  if (template.requiresMeasure && measureCount === 0) {
    missing.push('at least one measure');
  }
  if (template.requiresTimeDimension && !hasTimeDimension) {
    missing.push('a time dimension');
  }
  if (template.requiresCategoricalDimension && !hasCategoricalDimension) {
    missing.push('a categorical dimension');
  }
  if (template.requiresComparisonMeasure && measureCount < 2) {
    missing.push('at least two measures (for comparison)');
  }

  return { valid: missing.length === 0, missing };
}
