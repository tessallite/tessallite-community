import type { Kpi, Measure } from '../../types/tessallite';
export type KpiInsertMode = 'full_row' | 'value_only' | 'value_goal' | 'status_only' | 'kpi_card' | 'formula_ref';
interface KpiCardProps {
    kpi: Kpi;
    measures: Measure[];
    projectId: string;
    modelId: string;
    personaId?: string | null;
    checked: boolean;
    onToggle: () => void;
    onAddToValues: () => void;
    onInsertAsFormulas?: () => void;
    onInsertKpi?: (mode: KpiInsertMode) => void;
}
export default function KpiCard({ kpi, measures, projectId, modelId, personaId, checked, onToggle, onAddToValues, onInsertAsFormulas, onInsertKpi, }: KpiCardProps): import("react/jsx-runtime").JSX.Element;
export {};
