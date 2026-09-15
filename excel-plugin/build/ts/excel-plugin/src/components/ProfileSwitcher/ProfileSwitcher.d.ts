import type { ConnectionProfile } from '../../utils/storage';
interface ProfileSwitcherProps {
    profiles: ConnectionProfile[];
    activeProfile: ConnectionProfile | null;
    onSwitch: (profileId: string) => void;
    onRemove: (profileId: string) => void;
    onLogout: () => void;
}
export default function ProfileSwitcher({ profiles, activeProfile, onSwitch, onRemove, onLogout, }: ProfileSwitcherProps): import("react/jsx-runtime").JSX.Element;
export {};
