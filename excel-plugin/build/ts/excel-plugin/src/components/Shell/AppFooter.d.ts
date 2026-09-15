import type { Persona } from '../../types/tessallite';
interface AppFooterProps {
    /** Signed-in user's email; empty when no profile is active. */
    email: string;
    personas: Persona[];
    activePersonaId: string | null;
    onPersonaSelect: (persona: Persona | null) => void;
    connected: boolean;
    /** True while the connection is lost and being retried. */
    reconnecting: boolean;
}
/** Footer bar: user email, persona switcher and the connection status badge. */
export default function AppFooter({ email, personas, activePersonaId, onPersonaSelect, connected, reconnecting }: AppFooterProps): import("react/jsx-runtime").JSX.Element;
export {};
