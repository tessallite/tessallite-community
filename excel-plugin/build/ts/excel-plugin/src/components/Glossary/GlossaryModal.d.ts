import type { GlossaryEntry } from '../../types/tessallite';
interface GlossaryModalProps {
    open: boolean;
    onClose: () => void;
    entries: GlossaryEntry[];
}
export default function GlossaryModal({ open, onClose, entries }: GlossaryModalProps): import("react/jsx-runtime").JSX.Element;
export {};
