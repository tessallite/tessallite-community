import type { Hierarchy, HierarchyLevel } from '../../types/tessallite';
interface HierarchyLibraryProps {
    hierarchies: Hierarchy[];
    expanded: boolean;
    onToggle: () => void;
    onAssignToRows: (hierarchy: Hierarchy, level?: HierarchyLevel) => void;
}
export default function HierarchyLibrary({ hierarchies, expanded, onToggle, onAssignToRows, }: HierarchyLibraryProps): import("react/jsx-runtime").JSX.Element;
export {};
