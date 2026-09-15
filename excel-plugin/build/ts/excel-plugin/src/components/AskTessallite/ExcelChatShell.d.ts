import { type AgentChatAdapter, type AgentConfig, type TurnResponse } from "@tessallite/shared-ui";
import { type ChartTypeRecommendation } from "../../utils/excelCharts";
interface ExcelChatShellProps {
    adapter: AgentChatAdapter;
    t: (key: string, params?: Record<string, string | number>) => string;
    projectId: string;
    config: AgentConfig | null;
    activeModelId: string | null;
    activePersonaId?: string | null;
    agentConfigured: boolean;
    providerModel?: string;
    loading?: boolean;
    error?: string | null;
    onInsertTable?: (turn: TurnResponse) => void;
    onInsertChart?: (turn: TurnResponse, chartType?: ChartTypeRecommendation) => void;
    onInsertLocalPivot?: (turn: TurnResponse) => void;
    onFeedback?: (turnId: string, vote: "up" | "down") => void;
}
export default function ExcelChatShell({ adapter, t, projectId, config, activeModelId, activePersonaId, agentConfigured, providerModel, loading, error, onInsertTable, onInsertChart, onInsertLocalPivot, onFeedback, }: ExcelChatShellProps): import("react/jsx-runtime").JSX.Element;
export {};
