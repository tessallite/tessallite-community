import type { Kpi, Measure } from '../../types/tessallite';
import { type KpiInsertMode } from './KpiCard';
interface KpiLibraryProps {
    kpis: Kpi[];
    measures: Measure[];
    projectId: string;
    modelId: string;
    personaId?: string | null;
    searchQuery: string;
    selectedKpiValueMeasureIds: string[];
    onToggleKpi: (kpi: Kpi) => void;
    onAddKpiToValues: (kpi: Kpi) => void;
    onInsertKpiAsFormulas?: (kpi: Kpi) => void;
    onInsertKpi?: (kpi: Kpi, mode: KpiInsertMode) => void;
    onInsertScorecard?: () => void;
    expanded: boolean;
    onToggleExpanded: () => void;
    loading?: boolean;
}
export default function KpiLibrary({ kpis, measures, projectId, modelId, personaId, searchQuery, selectedKpiValueMeasureIds, onToggleKpi, onAddKpiToValues, onInsertKpiAsFormulas, onInsertKpi, onInsertScorecard, expanded, onToggleExpanded, loading, }: KpiLibraryProps): import("react/jsx-runtime").JSX.Element;
export {};
