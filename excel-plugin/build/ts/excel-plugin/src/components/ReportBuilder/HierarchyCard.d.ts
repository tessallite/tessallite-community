import type { Hierarchy, HierarchyLevel } from '../../types/tessallite';
interface HierarchyCardProps {
    hierarchy: Hierarchy;
    onAssignToRows: (level?: HierarchyLevel) => void;
}
/**
 * Display levels for a hierarchy from the LIST endpoint. The summary
 * response carries `level_names` (ordinal-ordered) but NOT full `levels`
 * objects -- the previous `hierarchy.levels ?? []` therefore rendered every
 * hierarchy as a bare header with no levels. Synthesized levels carry no
 * `dimensionName`; the add-to-zone handler resolves that from the detail
 * endpoint BY NAME (persona exclusions can skip ordinals, so a positional
 * index is not a safe join key).
 */
export declare function deriveDisplayLevels(hierarchy: Pick<Hierarchy, 'levels' | 'level_names'>): HierarchyLevel[];
export default function HierarchyCard({ hierarchy, onAssignToRows }: HierarchyCardProps): import("react/jsx-runtime").JSX.Element;
export {};
