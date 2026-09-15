import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Box, Button, Collapse, IconButton, Snackbar, Typography } from "@mui/material";
import { ExpandLess, ExpandMore } from "@mui/icons-material";
import { useState, useCallback, useRef, useEffect } from "react";
import { useAutoScroll } from "../hooks/useAutoScroll";
import { useConversationStore } from "../stores/conversationStore";
import { useChatContext } from "../providers/ChatProvider";
import { sendMessageStream, newIdempotencyKey } from "../streaming/messagesStream";
import type { TurnResponse } from "../types/turn";
import type { CompoundStep } from "../types/streaming";
import { resolveStreamErrorMessage } from "../types/streaming";
import type { TraceVisibility } from "./TraceStrip";
import { UserMessage } from "./UserMessage";
import { AssistantTurn } from "./AssistantTurn";
import { parseVisualArtifact } from "./VisualArtifactBlock";
import { ChatComposer } from "./ChatComposer";
import { EmptyChatState } from "./EmptyChatState";
import { ScrollToBottomButton } from "./ScrollToBottomButton";
import { LoadingSkeleton } from "./LoadingSkeleton";
import { InlineStepCard } from "./InlineStepCard";
import { ModelPicker } from "./ModelPicker";
import { formatThoughtText } from "../utils/thoughtText";

interface StreamingDraft {
  id: string;
  thought: string;
  narration: string;
  status: string;
  steps: CompoundStep[];
}

// Bug-8336 — a turn is only "reconcilable" (safe to treat a dropped stream as a
// success and suppress the error/retry) once the server has persisted it in a
// TERMINAL state. The reservation placeholder row is committed with
// status="streaming" and the real user_message BEFORE the pipeline runs, and
// list_turns returns it unfiltered; a judge_pending row is likewise
// non-releasable (its answer is withheld). Reconciling against either would
// suppress error+retry while showing an empty/unvetted turn — worse than the
// pre-fix behaviour. Only these terminal statuses represent a real, user-facing
// outcome the user should see rather than re-ask.
const TERMINAL_TURN_STATUSES = new Set([
  "ok",
  "error",
  "refused",
  "judge_blocked",
  // `clarify` is a fully-releasable terminal outcome (the agent asked a
  // clarifying question); its row carries the question in answer_text and is
  // never redacted. It MUST reconcile so a dropped stream after a clarify turn
  // does not re-ask it (the exact Bug-8336 symptom).
  "clarify",
]);

function isReconcilableTurn(turn: TurnResponse): boolean {
  return TERMINAL_TURN_STATUSES.has(turn.status);
}

function appendStep(
  steps: CompoundStep[],
  data: Record<string, unknown>,
): CompoundStep[] {
  const stepName =
    typeof data.step_name === "string" ? data.step_name : undefined;
  const rowCount =
    typeof data.rows_returned === "number" ? data.rows_returned : undefined;
  const previewRow =
    data.first_row &&
    typeof data.first_row === "object" &&
    Object.keys(data.first_row as object).length > 0
      ? (data.first_row as Record<string, unknown>)
      : undefined;
  return [
    ...steps,
    {
      step_number: steps.length + 1,
      title: stepName,
      status: "complete",
      row_count: rowCount,
      preview_row: previewRow,
    },
  ];
}

export interface ChatCanvasProps {
  /** Parent-owned authority transition gate. */
  disabled?: boolean;
  visibility?: TraceVisibility;
  onFeedback?: (turnId: string, vote: "up" | "down") => void;
  feedbackEnabled?: boolean;
  onOpenTrace?: (turn: TurnResponse) => void;
  maxChars?: number;
  composerPlaceholder?: string;
  echartsTheme?: Record<string, unknown>;
  chartsCss?: string;
  showModelPicker?: boolean;
  headerSlot?: React.ReactNode;
  renderTurnActions?: (turn: TurnResponse, resultRows?: Record<string, unknown>[]) => React.ReactNode;
  /**
   * Take over the Visual panel's maximise, compact hosts only. Excel supplies
   * this so the control opens a real Office dialog window instead of an overlay
   * that cannot outgrow the task pane. Left undefined, the overlay stays.
   */
  onMaximizeVisual?: (turn: TurnResponse, resultRows?: Record<string, unknown>[]) => void;
  /** Opt-in compact task-pane presentation. Transport and state remain unchanged. */
  compact?: boolean;
}

export function ChatCanvas({
  disabled = false,
  visibility,
  onFeedback,
  feedbackEnabled,
  onOpenTrace,
  maxChars,
  composerPlaceholder,
  echartsTheme,
  chartsCss,
  showModelPicker,
  headerSlot,
  renderTurnActions,
  onMaximizeVisual,
  compact = false,
}: ChatCanvasProps) {
  const { adapter, t, projectId, isEmbed, activeModelId } = useChatContext();
  const activeConversationId = useConversationStore(
    (s) => s.activeConversationId,
  );
  const isStreaming = useConversationStore((s) => s.isStreaming);
  const setStreaming = useConversationStore((s) => s.setStreaming);
  const setActiveConversation = useConversationStore(
    (s) => s.setActiveConversation,
  );
  const setDraftTitleCandidate = useConversationStore(
    (s) => s.setDraftTitleCandidate,
  );
  const pendingModelId = useConversationStore((s) => s.pendingModelId);
  const pendingPersonaId = useConversationStore((s) => s.pendingPersonaId);
  const queryClient = useQueryClient();

  const [streamingDraft, setStreamingDraft] =
    useState<StreamingDraft | null>(null);
  const [optimisticMessages, setOptimisticMessages] = useState<
    Array<{ text: string; id: string }>
  >([]);
  const [toast, setToast] = useState<string | null>(null);
  const [retryText, setRetryText] = useState<string | null>(null);
  const [abortController, setAbortController] =
    useState<AbortController | null>(null);
  const [composerKey, setComposerKey] = useState(0);
  const [prefillText, setPrefillText] = useState("");
  const [streamThoughtOpen, setStreamThoughtOpen] = useState(false);
  const resultSamplesRef = useRef<Map<string, Record<string, unknown>[]>>(
    new Map(),
  );
  const sendGenerationRef = useRef(0);
  const activeSendRef = useRef<{
    generation: number;
    controller: AbortController;
  } | null>(null);
  const currentContextRef = useRef({
    projectId,
    modelId: activeModelId ?? null,
  });
  currentContextRef.current = {
    projectId,
    modelId: activeModelId ?? null,
  };
  const previousContextRef = useRef({
    projectId,
    modelId: activeModelId ?? null,
  });

  useEffect(() => {
    const modelId = activeModelId ?? null;
    const previous = previousContextRef.current;
    if (previous.projectId !== projectId || previous.modelId !== modelId) {
      activeSendRef.current?.controller.abort();
      activeSendRef.current = null;
      sendGenerationRef.current += 1;
      setStreamingDraft(null);
      setStreaming(false);
      setAbortController(null);
      setOptimisticMessages([]);
    }
    previousContextRef.current = { projectId, modelId };
  }, [activeModelId, projectId, setStreaming]);

  const { containerRef, scrollToBottom, showScrollButton } = useAutoScroll([
    optimisticMessages,
    streamingDraft,
  ]);

  const {
    data: turns = [],
    isLoading,
    refetch,
  } = useQuery({
    queryKey: ["turns", projectId, activeConversationId],
    queryFn: () => adapter.getTurns(projectId, activeConversationId!),
    enabled: !!projectId && !!activeConversationId,
  });

  const handleSend = useCallback(
    async (text: string) => {
      if (!projectId || isStreaming || disabled) return;

      // Bug-9805 / Bug-9826: one generation owns the complete send lifecycle.
      // It aborts the transport when superseded and remains the final authority
      // for every callback, including reconciliation and title persistence.
      activeSendRef.current?.controller.abort();
      const generation = sendGenerationRef.current + 1;
      sendGenerationRef.current = generation;
      const controller = new AbortController();
      activeSendRef.current = { generation, controller };
      let streamFinished = false;
      const sendProjectId = projectId;
      const sendModelId = activeModelId ?? null;
      const isSendCurrent = () =>
        sendGenerationRef.current === generation &&
        currentContextRef.current.projectId === sendProjectId &&
        currentContextRef.current.modelId === sendModelId &&
        !controller.signal.aborted;
      const isCallbackCurrent = () => isSendCurrent() && !streamFinished;
      const finishSend = () => {
        streamFinished = true;
        if (activeSendRef.current?.generation === generation) {
          activeSendRef.current = null;
        }
      };

      // Bug-6521 — one idempotency key per logical send, generated ABOVE the
      // retry loop and captured in the fetchStream closure so every automatic
      // retry re-sends the SAME key and the backend dedupes the turn.
      const idempotencyKey = newIdempotencyKey();
      const optimisticId = `opt-${Date.now()}`;
      setOptimisticMessages((prev) => [...prev, { text, id: optimisticId }]);
      setRetryText(null);

      // Bug-8336 — snapshot the turn ids that already exist BEFORE this send,
      // so reconciliation cannot mistake an older identical question for this
      // send's persisted turn.
      const priorTurnsSnapshot = activeConversationId
        ? queryClient.getQueryData<TurnResponse[]>([
            "turns",
            projectId,
            activeConversationId,
          ])
        : [];
      const priorTurnsLoaded = priorTurnsSnapshot !== undefined;
      const priorTurnIds = new Set(
        (priorTurnsSnapshot ?? []).map((turn) => turn.id),
      );
      let startedTurnId: string | null = null;

      let convId = activeConversationId;
      let createdConversation = false;
      const titleCandidate = text.trim().replace(/\s+/g, " ").slice(0, 80);

      if (!convId) {
        try {
          const conv = await adapter.createConversation(projectId, {
            pinnedModelId: pendingModelId,
            personaId: pendingPersonaId,
          });
          if (!isSendCurrent()) return;
          convId = conv.id;
          createdConversation = true;
          setActiveConversation(convId);
          setDraftTitleCandidate(titleCandidate);
        } catch {
          if (!isSendCurrent()) return;
          setToast(t("chat.createConversationFailed"));
          setRetryText(text);
          setPrefillText(text);
          setComposerKey((k) => k + 1);
          setOptimisticMessages((prev) =>
            prev.filter((m) => m.id !== optimisticId),
          );
          finishSend();
          return;
        }
      }

      // Persist the auto-derived conversation title once a terminal turn has
      // landed. Every post-await mutation checks this send generation first.
      async function finalizeCreatedConversationTitle() {
        if (!createdConversation || !projectId || !convId || !titleCandidate)
          return;
        if (!isSendCurrent()) return;
        if (isEmbed) {
          if (isSendCurrent()) setDraftTitleCandidate(null);
          return;
        }
        try {
          await adapter.updateConversation(projectId, convId, {
            title: titleCandidate,
          });
          if (!isSendCurrent()) return;
          setDraftTitleCandidate(null);
          queryClient.invalidateQueries({
            queryKey: ["conversations", projectId],
          });
        } catch {
          if (isSendCurrent()) setToast(t("chat.titleSaveFailed"));
        }
      }

      async function finalizeCompletedTurn() {
        if (!isSendCurrent()) return;
        try {
          await refetch();
        } catch {
          // The persisted turn remains the source of truth; the next query
          // refresh can reconcile a transient refetch failure.
        }
        if (!isSendCurrent()) return;
        await finalizeCreatedConversationTitle();
      }

      if (!isSendCurrent()) return;
      setStreaming(true);
      setStreamThoughtOpen(false);
      setStreamingDraft({
        id: `stream-${Date.now()}`,
        thought: "",
        narration: "",
        status: "streaming",
        steps: [],
      });
      setAbortController(controller);

      const fetchStream = () =>
        adapter.streamMessageRaw(
          projectId,
          convId!,
          text,
          controller.signal,
          idempotencyKey,
        );

      sendMessageStream(
        fetchStream,
        {
          onEvent: (eventName, data) => {
            if (!isCallbackCurrent()) return;
            switch (eventName) {
              case "turn.started":
                if (typeof data.turn_id === "string" && data.turn_id) {
                  startedTurnId = data.turn_id;
                }
                setStreamingDraft((d) =>
                  d
                    ? { ...d, id: (data.turn_id as string) || d.id }
                    : d,
                );
                break;
              case "thought.delta":
                setStreamingDraft((d) =>
                  d
                    ? {
                        ...d,
                        thought: d.thought + ((data.text as string) || ""),
                      }
                    : d,
                );
                break;
              case "narration.delta":
                setStreamingDraft((d) =>
                  d
                    ? {
                        ...d,
                        narration: d.narration + ((data.text as string) || ""),
                      }
                    : d,
                );
                break;
              case "compound.step":
              case "recipe.step":
                setStreamingDraft((d) =>
                  d ? { ...d, steps: appendStep(d.steps, data) } : d,
                );
                break;
              case "turn.completed": {
                const turnId = data.turn_id as string | undefined;
                const sample = data.result_sample as
                  | Record<string, unknown>[]
                  | undefined;
                if (turnId && sample && sample.length > 0) {
                  resultSamplesRef.current.set(turnId, sample);
                }
                setStreamingDraft(null);
                setStreaming(false);
                setAbortController(null);
                setOptimisticMessages((prev) =>
                  prev.filter((m) => m.id !== optimisticId),
                );
                finishSend();
                void finalizeCompletedTurn();
                break;
              }
              case "turn.blocked":
                setStreamingDraft(null);
                setStreaming(false);
                setAbortController(null);
                setOptimisticMessages((prev) =>
                  prev.filter((m) => m.id !== optimisticId),
                );
                finishSend();
                if (isSendCurrent()) void refetch().catch(() => {});
                break;
              case "turn.error":
                setStreamingDraft(null);
                setStreaming(false);
                setAbortController(null);
                setToast(t("chat.requestError"));
                setRetryText(text);
                setPrefillText(text);
                setComposerKey((k) => k + 1);
                setOptimisticMessages((prev) =>
                  prev.filter((m) => m.id !== optimisticId),
                );
                finishSend();
                break;
            }
          },
          onError: async (err) => {
            if (!isCallbackCurrent()) return;
            setStreamingDraft(null);
            setStreaming(false);
            setAbortController(null);
            setOptimisticMessages((prev) =>
              prev.filter((m) => m.id !== optimisticId),
            );
            finishSend();
            // Bug-8336 (G-037-01) — reconcile a persisted terminal turn before
            // showing an error after a dropped stream. The generation guard
            // makes the whole async reconcile obsolete when a newer send or
            // context transition takes over.
            let reconciled = false;
            try {
              if (!isSendCurrent()) return;
              const { data: latestTurns } = await refetch();
              if (!isSendCurrent()) return;
              reconciled = Boolean(
                latestTurns?.some((turn) => {
                  if (!isReconcilableTurn(turn)) return false;
                  if (startedTurnId) return turn.id === startedTurnId;
                  return (
                    priorTurnsLoaded &&
                    !priorTurnIds.has(turn.id) &&
                    turn.user_message === text
                  );
                }),
              );
            } catch {
              // refetch failed — fall through to the error/retry path.
            }
            if (!isSendCurrent()) return;
            if (reconciled) {
              void finalizeCreatedConversationTitle();
              return;
            }
            if (!isSendCurrent()) return;
            setToast(resolveStreamErrorMessage(err, t));
            setRetryText(text);
            setPrefillText(text);
            setComposerKey((k) => k + 1);
          },
          onComplete: () => {
            if (!isCallbackCurrent()) return;
            setStreamingDraft(null);
            setStreaming(false);
            setAbortController(null);
            finishSend();
          },
        },
        { signal: controller.signal, isCurrent: isSendCurrent },
      );
    },
    [
      projectId,
      activeModelId,
      disabled,
      isEmbed,
      activeConversationId,
      isStreaming,
      pendingModelId,
      pendingPersonaId,
      adapter,
      t,
      setActiveConversation,
      setStreaming,
      setDraftTitleCandidate,
      refetch,
      queryClient,
    ],
  );

  const handleRetry = useCallback(() => {
    const text = retryText;
    if (!text) return;
    setToast(null);
    setRetryText(null);
    void handleSend(text);
  }, [retryText, handleSend]);

  const handleAbort = useCallback(() => {
    // Bug-9805: abort the controller held by the send lifecycle, not only the
    // render snapshot, and advance the generation so callbacks already queued
    // by the transport cannot clear or repopulate the current conversation.
    (activeSendRef.current?.controller ?? abortController)?.abort();
    activeSendRef.current = null;
    sendGenerationRef.current += 1;
    setStreamingDraft(null);
    setStreaming(false);
    setAbortController(null);
  }, [abortController, setStreaming]);

  const handleRephrase = useCallback((originalMessage: string) => {
    setPrefillText(originalMessage);
    setComposerKey((k) => k + 1);
  }, []);

  const showPicker = showModelPicker || (isEmbed && showModelPicker !== false);
  const modelPickerSlot = showPicker ? (
    <Box
      sx={{
        display: "flex",
        justifyContent: "flex-end",
        alignItems: "center",
        px: 1,
        py: 0.5,
        borderBottom: 1,
        borderColor: "divider",
        bgcolor: "background.paper",
      }}
    >
      <ModelPicker onError={setToast} />
    </Box>
  ) : null;

  if (!activeConversationId && optimisticMessages.length === 0) {
    return (
      <Box sx={{ height: "100%", display: "flex", flexDirection: "column" }}>
        {headerSlot}
        {modelPickerSlot}
        <Box sx={{ flex: 1, ...(compact ? { minHeight: 0 } : {}) }}>
          <EmptyChatState compact={compact} onSelectExample={compact ? handleRephrase : undefined} />
        </Box>
        <ChatComposer
          key={composerKey}
          initialText={prefillText}
          onSend={handleSend}
          onAbort={handleAbort}
          isStreaming={isStreaming}
          disabled={disabled}
          maxChars={maxChars}
          placeholder={composerPlaceholder}
          compact={compact}
        />
      </Box>
    );
  }

  return (
    <Box
      sx={{
        height: "100%",
        display: "flex",
        flexDirection: "column",
        position: "relative",
      }}
      role="main"
      aria-label={t("chat.chatAria")}
    >
      {headerSlot}
      {modelPickerSlot}
      <Box
        ref={containerRef}
        role="log"
        aria-label={t("chat.conversationAria")}
        aria-live="polite"
        sx={{
          flex: 1,
          ...(compact ? { minHeight: 0 } : {}),
          overflow: "auto",
          px: compact ? 1.25 : 2,
          py: compact ? 1 : 2,
          maxWidth: compact ? "none" : 960,
          mx: compact ? 0 : "auto",
          width: "100%",
        }}
      >
        {isLoading && activeConversationId && <LoadingSkeleton compact={compact} />}

        {turns.map((turn) => {
          const cachedRows = resultSamplesRef.current.get(turn.id);
          const sampleRows = turn.query_result_sample ?? undefined;
          const artifactRows = parseVisualArtifact(turn.rendered_output)?.rows;
          const rows =
            (cachedRows && cachedRows.length > 0 ? cachedRows : undefined) ??
            (sampleRows && sampleRows.length > 0 ? sampleRows : undefined) ??
            artifactRows;
          return (
            <Box key={turn.id}>
              <UserMessage text={turn.user_message} compact={compact} />
              <AssistantTurn
                turn={turn}
                resultRows={rows}
                visibility={visibility}
                onRephrase={handleRephrase}
                onResend={(prompt) => handleSend(prompt)}
                actionsDisabled={isStreaming}
                onFeedback={
                  onFeedback
                    ? (vote) => onFeedback(turn.id, vote)
                    : undefined
                }
                feedbackEnabled={feedbackEnabled}
                onOpenTrace={
                  onOpenTrace ? () => onOpenTrace(turn) : undefined
                }
                echartsTheme={echartsTheme}
                chartsCss={chartsCss}
                compact={compact}
                turnActions={compact ? renderTurnActions?.(turn, rows) : undefined}
                onMaximizeVisual={
                  compact && onMaximizeVisual
                    ? () => onMaximizeVisual(turn, rows)
                    : undefined
                }
              />
              {!compact && renderTurnActions?.(turn, rows)}
            </Box>
          );
        })}

        {optimisticMessages
          .filter((m) => !turns.some((t) => t.user_message === m.text))
          .map((m) => (
            <UserMessage key={m.id} text={m.text} compact={compact} />
          ))}

        {streamingDraft && (
          <Box sx={{ mb: compact ? 1 : 2 }}>
            <Box
              sx={{
                p: compact ? 0 : 2,
                border: 1,
                borderColor: "divider",
                borderRadius: compact ? 0.5 : 2,
                bgcolor: "background.paper",
              }}
            >
              {/* Bug-7376/7545: gate the streaming thought panel on
                  visibility.showThoughtProcess so the server config controls
                  whether thought-process is visible, matching TraceStrip and
                  TraceDrawer behaviour. */}
              {streamingDraft.thought && visibility?.showThoughtProcess !== false && (
                <Box
                  sx={{
                    mb: compact ? 0.5 : 1,
                    p: compact ? 0.5 : 0.75,
                    bgcolor: "action.hover",
                    borderRadius: 1,
                  }}
                >
                  <Box
                    sx={{
                      display: "flex",
                      alignItems: "center",
                      gap: 0.5,
                      minHeight: compact ? 22 : undefined,
                    }}
                  >
                    <IconButton
                      size="small"
                      onClick={() => setStreamThoughtOpen((v) => !v)}
                      aria-label={streamThoughtOpen ? t("chat.collapseThought") : t("chat.expandThought")}
                      sx={{
                        p: compact ? 0.125 : 0.25,
                        ...(compact ? { width: 18, height: 18 } : {}),
                      }}
                    >
                      {streamThoughtOpen ? (
                        <ExpandLess fontSize="small" />
                      ) : (
                        <ExpandMore fontSize="small" />
                      )}
                    </IconButton>
                    <Typography
                      variant="caption"
                      color="text.secondary"
                      fontWeight={650}
                      sx={compact ? { fontSize: 10 } : undefined}
                    >
                      {t("turn.thinking")}
                    </Typography>
                  </Box>
                  <Collapse in={streamThoughtOpen}>
                    <Box
                      component="span"
                      data-testid="streaming-thought"
                      sx={{
                        display: "block",
                        mt: 0.5,
                        maxHeight: "5.5em",
                        overflowY: "auto",
                        fontSize: compact ? 10.5 : 12,
                        lineHeight: 1.35,
                        color: "text.secondary",
                        whiteSpace: "pre-wrap",
                        overflowWrap: "anywhere",
                      }}
                    >
                      {formatThoughtText(streamingDraft.thought)}
                      <Box
                        component="span"
                        sx={{
                          animation: "blink 1s step-end infinite",
                          ml: 0.5,
                        }}
                      >
                        |
                      </Box>
                    </Box>
                  </Collapse>
                  <Box
                    sx={{
                      fontSize: compact ? 10.5 : 12,
                      lineHeight: compact ? 1.35 : 1.4,
                      color: "text.secondary",
                      display: streamThoughtOpen ? "none" : "block",
                      pl: 3.5,
                    }}
                  >
                    {t("chat.thoughtStreaming")}
                  </Box>
                </Box>
              )}
              {streamingDraft.steps.length > 0 && (
                <Box sx={{ mb: compact ? 0.5 : 1 }}>
                  <InlineStepCard steps={streamingDraft.steps} compact={compact} />
                </Box>
              )}
              <Box
                sx={{
                  fontSize: compact ? 12 : 15,
                  lineHeight: compact ? 1.4 : 1.6,
                  whiteSpace: "pre-wrap",
                }}
              >
                {streamingDraft.narration || (
                  <Box
                    sx={{
                      display: "flex",
                      gap: 0.5,
                      alignItems: "center",
                    }}
                  >
                    <Box
                      sx={{
                        width: 6,
                        height: 6,
                        borderRadius: "50%",
                        bgcolor: "primary.main",
                        animation:
                          "pulse 1.5s ease-in-out infinite",
                      }}
                    />
                    <Box
                      sx={{ fontSize: 13, color: "text.secondary" }}
                      role="status"
                      aria-label={t("chat.thinkingAria")}
                    >
                      {t("chat.thinking")}
                    </Box>
                  </Box>
                )}
                {streamingDraft.narration && (
                  <Box
                    component="span"
                    sx={{ animation: "blink 1s step-end infinite" }}
                  >
                    |
                  </Box>
                )}
              </Box>
            </Box>
            <style>{`
              @keyframes blink { 50% { opacity: 0; } }
              @keyframes pulse { 0%, 100% { opacity: 0.4; } 50% { opacity: 1; } }
            `}</style>
          </Box>
        )}
      </Box>

      {showScrollButton && <ScrollToBottomButton onClick={scrollToBottom} compact={compact} />}

      <ChatComposer
        key={composerKey}
        initialText={prefillText}
        onSend={handleSend}
        onAbort={handleAbort}
        isStreaming={isStreaming}
        disabled={disabled}
        maxChars={maxChars}
        placeholder={composerPlaceholder}
        compact={compact}
      />

      <Snackbar
        open={!!toast}
        autoHideDuration={6000}
        onClose={() => setToast(null)}
        anchorOrigin={{ vertical: "bottom", horizontal: "right" }}
      >
        <Alert
          severity="error"
          onClose={() => setToast(null)}
          variant="filled"
          sx={compact ? { fontSize: 11, borderRadius: 0.5 } : undefined}
        >
          {retryText && (
            <Button
              color="inherit"
              size="small"
              onClick={handleRetry}
              sx={{ mr: 1 }}
              aria-label={t("chat.retryAria")}
            >
              {t("chat.retry")}
            </Button>
          )}
          {toast}
        </Alert>
      </Snackbar>
    </Box>
  );
}
