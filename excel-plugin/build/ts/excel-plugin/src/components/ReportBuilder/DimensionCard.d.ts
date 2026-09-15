import type { DimensionCompatibilityState } from '../../utils/fieldCompatibility';
interface DimensionCardProps {
    id: string;
    displayName: string;
    description?: string;
    dataType: string;
    sourceType: 'dim' | 'calculated';
    isTimeDimension?: boolean;
    calendarType?: string;
    compatibility?: DimensionCompatibilityState;
    onAssign: (zone: 'rows' | 'columns' | 'filter' | 'slicer') => void;
    onPreviewMembers?: (id: string) => void;
}
export default function DimensionCard({ id, displayName, description, dataType, sourceType, isTimeDimension, calendarType, compatibility, onAssign, onPreviewMembers, }: DimensionCardProps): import("react/jsx-runtime").JSX.Element;
export {};
