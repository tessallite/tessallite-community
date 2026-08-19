// Types
export type {
  AgentChatAdapter,
  CreateConversationOptions,
} from "./types/adapter";
export type { TurnResponse, Citation, CalculationStep } from "./types/turn";
export type {
  ConversationResponse,
  ConversationUpdatePayload,
} from "./types/conversation";
export type {
  AgentConfig,
  AgentVisibilityConfig,
  AgentPersona,
  SelectableModel,
} from "./types/config";
export type { StreamCallbacks, CompoundStep, StreamErrorCode } from "./types/streaming";
export { StreamError, resolveStreamErrorMessage } from "./types/streaming";

// Provider
export { ChatProvider, useChatContext } from "./providers/ChatProvider";
export type { ChatContextValue } from "./providers/ChatProvider";

// Store
export { useConversationStore } from "./stores/conversationStore";
export type { ConversationState } from "./stores/conversationStore";

// Streaming
export { sendMessageStream, newIdempotencyKey } from "./streaming/messagesStream";

// Hooks
export { useAutoScroll } from "./hooks/useAutoScroll";

// Components
export { ChatCanvas } from "./components/ChatCanvas";
export type { ChatCanvasProps } from "./components/ChatCanvas";
export { AssistantTurn } from "./components/AssistantTurn";
export type { AssistantTurnProps } from "./components/AssistantTurn";
export { ChatComposer } from "./components/ChatComposer";
export type { ChatComposerProps } from "./components/ChatComposer";
export { UserMessage } from "./components/UserMessage";
export { EmptyChatState } from "./components/EmptyChatState";
export { ScrollToBottomButton } from "./components/ScrollToBottomButton";
export { LoadingSkeleton } from "./components/LoadingSkeleton";
export { MarkdownAnswer } from "./components/MarkdownAnswer";
export { QueryBlock } from "./components/QueryBlock";
export { MetadataBadges } from "./components/MetadataBadges";
export { CitationChips } from "./components/CitationChips";
export { CitationProvenanceDialog } from "./components/CitationProvenanceDialog";
export type { CitationProvenanceDialogProps } from "./components/CitationProvenanceDialog";
export { InlineStepCard } from "./components/InlineStepCard";
export { DataTableBlock } from "./components/DataTableBlock";
export { ChartBlock } from "./components/ChartBlock";
export { RenderedOutput } from "./components/RenderedOutput";
export { VisualArtifactBlock, parseVisualArtifact } from "./components/VisualArtifactBlock";
export type { VisualArtifact } from "./components/VisualArtifactBlock";
export { ErrorBoundary } from "./components/ErrorBoundary";
export { FeedbackButtons } from "./components/FeedbackButtons";
export { JudgeBlockCard } from "./components/JudgeBlockCard";
export { JudgeVerdictStrip } from "./components/JudgeVerdictStrip";
export { ModelPicker } from "./components/ModelPicker";
export { TraceStrip } from "./components/TraceStrip";
export type { TraceVisibility } from "./components/TraceStrip";
export { TraceDrawer } from "./components/TraceDrawer";

// Utils
export { buildAutoChartSpec } from "./utils/chartSpec";
