/**
 * Report templates utility.
 * Defines pre-configured report patterns that populate the zone mapping grid.
 */
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
export declare const REPORT_TEMPLATES: ReportTemplate[];
/**
 * Check if a template can be applied given the available model fields.
 */
export declare function checkTemplatePrerequisites(template: ReportTemplate, measureCount: number, hasTimeDimension: boolean, hasCategoricalDimension: boolean): {
    valid: boolean;
    missing: string[];
};
