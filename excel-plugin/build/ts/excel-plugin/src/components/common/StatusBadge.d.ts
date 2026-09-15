interface StatusBadgeProps {
    status: 'connected' | 'disconnected' | 'reconnecting' | 'active' | 'inactive';
    label: string;
    size?: 'small' | 'medium';
}
export default function StatusBadge({ status, label, size, }: StatusBadgeProps): import("react/jsx-runtime").JSX.Element;
export {};
