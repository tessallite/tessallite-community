import type { Measure, GlossaryEntry } from '../../types/tessallite';
interface MeasureLibraryProps {
    measures: Measure[];
    searchQuery: string;
    selectedMeasureIds: string[];
    onToggleMeasure: (measureId: string) => void;
    onAddToValues: (measureId: string) => void;
    /** Bug-9747: add a HAVING-style filter on the measure's aggregated value. */
    onAddToFilter?: (measureId: string) => void;
    /** Phase A default: insert as TESSALLITE.VALUE() formula (connectionless). */
    onInsertMeasureAsFunction?: (measureId: string) => void;
    /** Advanced: insert as CUBEVALUE formula (requires workbook connection). */
    onInsertMeasureAsFormula?: (measureId: string) => void;
    expanded: boolean;
    onToggleExpanded: () => void;
    loading?: boolean;
    glossaryEntries?: GlossaryEntry[];
}
export default function MeasureLibrary({ measures, searchQuery, selectedMeasureIds, onToggleMeasure, onAddToValues, onAddToFilter, onInsertMeasureAsFunction, onInsertMeasureAsFormula, expanded, onToggleExpanded, loading, glossaryEntries, }: MeasureLibraryProps): import("react/jsx-runtime").JSX.Element;
export {};
