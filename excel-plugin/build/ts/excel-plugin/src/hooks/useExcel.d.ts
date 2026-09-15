import { type EvaluatedScorecardKpi } from '../utils/kpiScorecard';
import { type ChartTypeRecommendation } from '../utils/excelCharts';
import { type PivotFieldMapping } from '../utils/excelPivotTables';
interface InsertTableOptions {
    useActiveCell?: boolean;
    resetSheetsPerSession?: boolean;
}
interface InsertMetadata {
    projectId?: string;
    modelId?: string;
    personaId?: string;
    modelLabel?: string;
    personaLabel?: string;
    conversationId?: string;
    turnId?: string;
    semanticQuery?: string;
    formatTokens?: Record<string, string>;
    columnHeaders?: string[];
    measureColumns?: Record<string, string>;
    dimensionColumns?: Record<string, string>;
}
export interface ConfirmGuard {
    (message: string): Promise<boolean>;
}
/**
 * Bug-6737: table insert result. The actual write (insertResultTable) is
 * separated from post-steps (metadata tagging, provenance footer) so the
 * toast can report the TRUE outcome -- a post-step failure that occurs
 * after a successful write must never become "Insert failed".
 */
export interface TableInsertResult {
    address: string | null;
    postStepWarning: boolean;
    blocked?: boolean;
}
export declare function useExcel(confirmGuard?: ConfirmGuard, onBusy?: () => void, modelId?: string): {
    insertTable: (headers: string[], rows: (string | number)[][], options?: InsertTableOptions, metadata?: InsertMetadata) => Promise<TableInsertResult>;
    insertChart: (headers: string[], rows: (string | number)[][], chartType?: ChartTypeRecommendation, annotation?: {
        measures?: Record<string, {
            title: string;
            type: string;
        }>;
        dimensions?: Record<string, {
            title: string;
            type: string;
        }>;
        timeDimensions?: Record<string, {
            title: string;
            type: string;
        }>;
    }) => Promise<{
        address: string | null;
        postStepWarning: boolean;
    }>;
    insertLocalPivot: (headers: string[], rows: (string | number)[][], fieldMapping?: PivotFieldMapping, annotation?: {
        measures?: Record<string, {
            title: string;
            type: string;
        }>;
        dimensions?: Record<string, {
            title: string;
            type: string;
        }>;
    }) => Promise<string | null>;
    insertFormula: (formula: string, targetCell?: string) => Promise<boolean>;
    insertLiteral: (value: string | number | null, targetCell?: string) => Promise<boolean>;
    insertNamedSetAsFormulas: (namedSet: {
        id: string;
        name: string;
        display_name: string | null;
        expression: string;
        updated_at?: string;
    }, connectionName: string, memberCount?: number) => Promise<string | null>;
    insertKpiFormulas: (kpi: {
        id: string;
        name: string;
        display_name: string | null;
        updated_at?: string;
    }, valueMeasureName: string | null, goalMeasureName: string | null, connectionName: string, goalLiteral?: number | null, valueLiteral?: number | null, forceLiteral?: boolean) => Promise<string | null>;
    insertKpiFullRow: (kpi: {
        id: string;
        name: string;
        display_name: string | null;
        updated_at?: string;
    }, valueMeasureName: string | null, goalMeasureName: string | null, connectionName: string, goalLiteral?: number | null) => Promise<string | null>;
    insertKpiValueOnly: (valueMeasureName: string, connectionName: string) => Promise<string | null>;
    insertKpiStatusOnly: (kpiName: string, connectionName: string, literal?: {
        statusLiteral: string | number | null;
    }) => Promise<string | null>;
    insertKpiValueFormula: (kpiName: string, connectionName: string) => Promise<string | null>;
    insertMeasureAsFormula: (measureName: string, connectionName: string) => Promise<string | null>;
    insertKpiScorecard: (kpis: EvaluatedScorecardKpi[], _connectionName: string, modelSlug?: string) => Promise<string | null>;
    getActiveCellAddress: () => Promise<string>;
    readCellValue: () => Promise<{
        address: string;
        value: unknown;
        formula: string;
    }>;
    createNewSheet: (baseName: string) => Promise<string>;
};
export {};
