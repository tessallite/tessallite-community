import type { NamedSet } from '../../types/tessallite';
interface NamedSetLibraryProps {
    namedSets: NamedSet[];
    searchQuery: string;
    projectId: string;
    modelId: string;
    personaId?: string;
    onAddToRows: (ns: NamedSet) => void;
    onAddToColumns: (ns: NamedSet) => void;
    onAddToFilter: (ns: NamedSet) => void;
    onInsertAsFormulas?: (ns: NamedSet) => void;
    expanded: boolean;
    onToggleExpanded: () => void;
    loading?: boolean;
}
export default function NamedSetLibrary({ namedSets, searchQuery, projectId, modelId, personaId, onAddToRows, onAddToColumns, onAddToFilter, onInsertAsFormulas, expanded, onToggleExpanded, loading, }: NamedSetLibraryProps): import("react/jsx-runtime").JSX.Element;
export {};
