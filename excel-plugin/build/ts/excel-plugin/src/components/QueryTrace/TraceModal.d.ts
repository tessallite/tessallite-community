import type { SemanticQuery, PluginRouteTrace } from '../../types/tessallite';
interface TraceModalProps {
    open: boolean;
    onClose: () => void;
    query: SemanticQuery | null;
    route?: PluginRouteTrace | null;
    modelName: string | null;
    personaName?: string | null;
}
export default function TraceModal({ open, onClose, query, route, modelName, personaName }: TraceModalProps): import("react/jsx-runtime").JSX.Element;
export {};
