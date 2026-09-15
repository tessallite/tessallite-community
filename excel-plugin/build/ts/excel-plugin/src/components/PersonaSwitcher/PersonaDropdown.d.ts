import type { Persona } from '../../types/tessallite';
interface PersonaDropdownProps {
    personas: Persona[];
    activePersonaId: string | null;
    onSelect: (persona: Persona | null) => void;
}
export default function PersonaDropdown({ personas, activePersonaId, onSelect }: PersonaDropdownProps): import("react/jsx-runtime").JSX.Element | null;
export {};
