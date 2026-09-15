interface LiveConnectionWizardProps {
    open: boolean;
    onClose: () => void;
    serverUrl: string;
    catalog: string;
}
export default function LiveConnectionWizard({ open, onClose, serverUrl, catalog, }: LiveConnectionWizardProps): import("react/jsx-runtime").JSX.Element;
export {};
