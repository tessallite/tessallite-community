interface EmptyStateProps {
    icon?: React.ReactNode;
    title: string;
    description?: string;
    action?: {
        label: string;
        onClick: () => void;
    };
}
export default function EmptyState({ icon, title, description, action, }: EmptyStateProps): import("react/jsx-runtime").JSX.Element;
export {};
