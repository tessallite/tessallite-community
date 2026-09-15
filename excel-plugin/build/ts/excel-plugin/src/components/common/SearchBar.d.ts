interface SearchBarProps {
    value: string;
    onChange: (value: string) => void;
    placeholder?: string;
    disabled?: boolean;
}
export default function SearchBar({ value, onChange, placeholder, disabled, }: SearchBarProps): import("react/jsx-runtime").JSX.Element;
export {};
