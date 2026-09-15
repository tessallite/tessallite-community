interface DrillPanelProps {
    open: boolean;
    onClose: () => void;
    measureId: string;
    measureName: string;
    context: Record<string, unknown>;
    projectId?: string;
    modelId?: string;
    onInsertSheet: (headers: string[], rows: (string | number)[][]) => void;
}
export default function DrillPanel({ open, onClose, measureId, measureName, context, projectId, modelId, onInsertSheet, }: DrillPanelProps): import("react/jsx-runtime").JSX.Element | null;
export {};
