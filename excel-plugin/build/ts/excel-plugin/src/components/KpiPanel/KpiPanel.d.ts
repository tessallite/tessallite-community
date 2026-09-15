import { type EvaluatedScorecardKpi } from '../../utils/kpiScorecard';
interface KpiPanelProps {
    projectId: string;
    modelId: string;
    personaId?: string | null;
    /** Bug-6903: model slug for TESSALLITE.KPI formula scorecard (live refresh). */
    modelSlug?: string | null;
    onInsertTable: (headers: string[], rows: (string | number)[][]) => Promise<{
        address: string | null;
        postStepWarning: boolean;
    }>;
    onInsertChart: (headers: string[], rows: (string | number)[][]) => Promise<string | null>;
    onInsertScorecard: (kpis: EvaluatedScorecardKpi[], connectionName: string, modelSlug?: string) => Promise<string | null>;
    connectionName: string;
}
export default function KpiPanel({ projectId, modelId, personaId, modelSlug, onInsertTable, onInsertChart, onInsertScorecard, connectionName, }: KpiPanelProps): import("react/jsx-runtime").JSX.Element;
export {};
