interface ReportBuilderProps {
    projectId: string;
    modelId: string;
    serverUrl: string;
    personaId?: string | null;
    personaSlug?: string | null;
    modelsList?: {
        id: string;
        name: string;
        slug?: string;
    }[];
    onModelChange?: (modelId: string) => void;
}
export default function ReportBuilder({ projectId, modelId, serverUrl, personaId, personaSlug, modelsList, onModelChange }: ReportBuilderProps): import("react/jsx-runtime").JSX.Element;
export {};
