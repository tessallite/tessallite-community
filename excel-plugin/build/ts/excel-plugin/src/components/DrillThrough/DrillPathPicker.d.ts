import type { DrillOption } from '../../types/tessallite';
interface DrillPathPickerProps {
    options: DrillOption[];
    selectedPath: string;
    onSelect: (hierarchyId: string) => void;
}
export default function DrillPathPicker({ options, selectedPath, onSelect, }: DrillPathPickerProps): import("react/jsx-runtime").JSX.Element | null;
export {};
