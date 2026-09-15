import type { ConnectionProfile } from '../../utils/storage';
interface AppHeaderProps {
    profiles: ConnectionProfile[];
    activeProfile: ConnectionProfile | null;
    onOpenDrill: () => void;
    onOpenGlossary: () => void;
    onOpenDiagnostics: () => void;
    onSwitchProfile: (profileId: string) => void;
    onRemoveProfile: (profileId: string) => void;
    onLogout: () => void;
}
/**
 * Task-pane header: brand mark, title and subtitle, then the drill-through,
 * glossary, profile and settings actions. The settings menu (Diagnostics,
 * Sign Out) is owned here; everything else is reported through callbacks.
 */
export default function AppHeader({ profiles, activeProfile, onOpenDrill, onOpenGlossary, onOpenDiagnostics, onSwitchProfile, onRemoveProfile, onLogout, }: AppHeaderProps): import("react/jsx-runtime").JSX.Element;
export {};
