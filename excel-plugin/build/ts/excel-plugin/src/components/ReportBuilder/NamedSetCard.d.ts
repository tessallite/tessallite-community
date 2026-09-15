import type { NamedSet } from '../../types/tessallite';
interface NamedSetCardProps {
    namedSet: NamedSet;
    projectId: string;
    modelId: string;
    personaId?: string;
    onAddToRows: () => void;
    onAddToColumns: () => void;
    onAddToFilter: () => void;
    onInsertAsFormulas?: () => void;
}
export default function NamedSetCard({ namedSet, projectId, modelId, personaId, onAddToRows, onAddToColumns, onAddToFilter, onInsertAsFormulas, }: NamedSetCardProps): import("react/jsx-runtime").JSX.Element;
export {};
