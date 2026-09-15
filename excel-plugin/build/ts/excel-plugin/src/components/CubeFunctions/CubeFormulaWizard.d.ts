import type { Measure, Dimension } from '../../types/tessallite';
interface CubeFormulaWizardProps {
    open: boolean;
    onClose: () => void;
    measures: Measure[];
    dimensions: Dimension[];
    projectId: string;
    modelId: string;
    personaId?: string;
    connectionName: string;
    onInsertFormula: (formula: string, targetCell: string) => void;
}
export default function CubeFormulaWizard({ open, onClose, measures, dimensions, projectId, modelId, personaId, connectionName, onInsertFormula, }: CubeFormulaWizardProps): import("react/jsx-runtime").JSX.Element;
export {};
