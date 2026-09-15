import type { Measure, GlossaryEntry } from '../../types/tessallite';
interface MeasureCardProps {
    measure: Measure;
    checked: boolean;
    onToggle: () => void;
    onAddToValues: () => void;
    /** Bug-9747: add a HAVING-style filter on this measure's aggregated value. */
    onAddToFilter?: () => void;
    /** Phase A default: insert as TESSALLITE.VALUE() formula (connectionless). */
    onInsertAsFunction?: () => void;
    /** Advanced: insert as CUBEVALUE formula (requires workbook connection). */
    onInsertAsFormula?: () => void;
    glossaryEntries?: GlossaryEntry[];
}
export default function MeasureCard({ measure, checked, onToggle, onAddToValues, onAddToFilter, onInsertAsFunction, onInsertAsFormula, glossaryEntries, }: MeasureCardProps): import("react/jsx-runtime").JSX.Element;
export {};
