import type { Dimension, DiscoverMembersResponse } from '../../types/tessallite';
import type { DimensionCompatibilityState } from '../../utils/fieldCompatibility';
interface DimensionLibraryProps {
    dimensions: Dimension[];
    searchQuery: string;
    onAddToRows: (dimensionId: string) => void;
    onAddToColumns: (dimensionId: string) => void;
    onAddToFilter: (dimensionId: string) => void;
    onPreviewMembers: (dimensionId: string) => void;
    expanded: boolean;
    onToggleExpanded: () => void;
    loading?: boolean;
    memberPreviewDimId: string | null;
    memberPreview: DiscoverMembersResponse | null;
    membersPreviewLoading: boolean;
    onCloseMemberPreview: () => void;
    compatibilityByDimensionId?: Record<string, DimensionCompatibilityState>;
}
export default function DimensionLibrary({ dimensions, searchQuery, onAddToRows, onAddToColumns, onAddToFilter, onPreviewMembers, expanded, onToggleExpanded, loading, memberPreviewDimId, memberPreview, membersPreviewLoading, onCloseMemberPreview, compatibilityByDimensionId, }: DimensionLibraryProps): import("react/jsx-runtime").JSX.Element;
export {};
