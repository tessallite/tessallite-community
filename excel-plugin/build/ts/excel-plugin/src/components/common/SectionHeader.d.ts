interface SectionHeaderProps {
    title: string;
    count?: number;
    collapsed: boolean;
    onToggle: () => void;
}
export default function SectionHeader({ title, count, collapsed, onToggle, }: SectionHeaderProps): import("react/jsx-runtime").JSX.Element;
export {};
