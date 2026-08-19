import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Box, Button, Collapse, IconButton, Snackbar, Typography } from "@mui/material";
import { ExpandLess, ExpandMore } from "@mui/icons-material";
import { useState, useCallback, useRef } from "react";
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
}

export function ChatCanvas({
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
}: ChatCanvasProps) {
  const { adapter, t, projectId, isEmbed } = useChatContext();
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
      if (!projectId || isStreaming) return;

      // Bug-6521 — one idempotency key per logical send, generated ABOVE the
      // retry loop and captured in the fetchStream closure so every automatic
      // retry re-sends the SAME key and the backend dedupes the turn.
      const idempotencyKey = newIdempotencyKey();
      const optimisticId = `opt-${Date.now()}`;
      setOptimisticMessages((prev) => [...prev, { text, id: optimisticId }]);
      setRetryText(null);

      // Bug-8336 — snapshot the turn ids that already exist BEFORE this send, so
      // the onError reconcile can tell a turn THIS send produced apart from an
      // older turn that merely shares the same text (re-asking the same
      // question is a normal pattern). Distinguish an UNLOADED cache (undefined
      // — the turns query has not resolved yet, e.g. a send right after
      // switching conversations) from a LOADED-EMPTY cache ([] or a just-created
      // conversation): with an unloaded cache we cannot prove a matching turn is
      // new, so the text fallback must NOT fire (it would let an older
      // identical-text turn falsely reconcile a genuinely failed send). Also
      // capture the server turn_id from turn.started so reconciliation can match
      // on identity if the backend ever supplies it (today it does not — see
      // Bug intake to emit turn_id on turn.started); until then the newness
      // snapshot is the sole discriminator, hence the unloaded-cache guard.
      const priorTurnsSnapshot = activeConversationId
        ? queryClient.getQueryData<TurnResponse[]>([
            "turns",
            projectId,
            activeConversationId,
          ])
        : []; // no active conversation yet -> genuinely no prior turns
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
          convId = conv.id;
          createdConversation = true;
          setActiveConversation(convId);
          setDraftTitleCandidate(titleCandidate);
        } catch {
          setToast(t("chat.createConversationFailed"));
          setRetryText(text);
          setPrefillText(text);
          setComposerKey((k) => k + 1);
          setOptimisticMessages((prev) =>
            prev.filter((m) => m.id !== optimisticId),
          );
          return;
        }
      }

      // Persist the auto-derived conversation title once a terminal turn has
      // landed. Split out from the refetch so the Bug-8336 onError reconcile
      // path (which already refetched to detect the persisted turn) can finalize
      // the title without triggering a redundant second refetch.
      async function finalizeCreatedConversationTitle() {
        if (!createdConversation || !projectId || !convId || !titleCandidate)
          return;
        if (isEmbed) {
          setDraftTitleCandidate(null);
          return;
        }
        try {
          await adapter.updateConversation(projectId, convId, {
            title: titleCandidate,
          });
          setDraftTitleCandidate(null);
          queryClient.invalidateQueries({
            queryKey: ["conversations", projectId],
          });
        } catch {
          setToast(t("chat.titleSaveFailed"));
        }
      }

      async function finalizeCompletedTurn() {
        refetch();
        await finalizeCreatedConversationTitle();
      }

      setStreaming(true);
      setStreamThoughtOpen(false);
      setStreamingDraft({
        id: `stream-${Date.now()}`,
        thought: "",
        narration: "",
        status: "streaming",
        steps: [],
      });

      const controller = new AbortController();
      setAbortController(controller);

      const fetchStream = () =>
        adapter.streamMessageRaw(
          projectId,
          convId!,
          text,
          controller.signal,
          idempotencyKey,
        );

      sendMessageStream(fetchStream, {
          onEvent: (eventName, data) => {
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
                        thought:
                          d.thought + ((data.text as string) || ""),
                      }
                    : d,
                );
                break;
              case "narration.delta":
                setStreamingDraft((d) =>
                  d
                    ? {
                        ...d,
                        narration:
                          d.narration + ((data.text as string) || ""),
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
                refetch();
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
                break;
            }
          },
          onError: async (err) => {
            setStreamingDraft(null);
            setStreaming(false);
            setAbortController(null);
            setOptimisticMessages((prev) =>
              prev.filter((m) => m.id !== optimisticId),
            );
            // Bug-8336 (G-037-01) — an onError can fire AFTER the server has
            // already persisted the terminal turn (post-token connection loss,
            // proxy close, EOF without a terminal event). Reconcile FIRST:
            // refetch the persisted turns and, if the turn for this send now
            // exists, treat it as a success — do NOT surface an error and do
            // NOT offer a retry, which would re-ask an already-answered
            // question. This mirrors the onComplete/finalizeCompletedTurn
            // refetch path so both terminal outcomes converge on server truth.
            // If no persisted turn is found, the request genuinely failed:
            // show the error and offer an (idempotent) retry as before.
            let reconciled = false;
            try {
              const { data: latestTurns } = await refetch();
              reconciled = Boolean(
                latestTurns?.some((turn) => {
                  // Must be a real, persisted terminal turn — never the
                  // "streaming" reservation placeholder or a withheld
                  // judge_pending row (see TERMINAL_TURN_STATUSES).
                  if (!isReconcilableTurn(turn)) return false;
                  // Prefer identity: the turn_id the server assigned to THIS
                  // send (from turn.started), if the backend supplies it. This
                  // is unambiguous even when the same question was asked earlier
                  // in the conversation.
                  if (startedTurnId) return turn.id === startedTurnId;
                  // No turn_id available. Fall back to "a turn that did NOT
                  // exist before this send and matches this text". This is only
                  // trustworthy when we actually had the prior turns loaded;
                  // with an unloaded snapshot we cannot tell new from old, so we
                  // decline to reconcile and let the error/retry path run.
                  return (
                    priorTurnsLoaded &&
                    !priorTurnIds.has(turn.id) &&
                    turn.user_message === text
                  );
                }),
              );
            } catch {
              // refetch failed — fall through to the error/retry path so the
              // user is never left silently stuck.
            }
            if (reconciled) {
              // The turn was persisted; finalize the created-conversation title
              // exactly as a normal completion would, without a second refetch.
              void finalizeCreatedConversationTitle();
              return;
            }
            // Bug-8370 — resolve the typed StreamErrorCode to a friendly,
            // i18n-backed message instead of surfacing messagesStream.ts's
            // raw English `err.message` verbatim. An unrecognised code (or a
            // non-StreamError) degrades to the generic connection-error copy.
            setToast(resolveStreamErrorMessage(err, t));
            setRetryText(text);
            setPrefillText(text);
            setComposerKey((k) => k + 1);
          },
          onComplete: () => {
            setStreamingDraft(null);
            setStreaming(false);
            setAbortController(null);
          },
        },
      );
    },
    [
      projectId,
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
    abortController?.abort();
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
        <Box sx={{ flex: 1 }}>
          <EmptyChatState />
        </Box>
        <ChatComposer
          key={composerKey}
          initialText={prefillText}
          onSend={handleSend}
          onAbort={handleAbort}
          isStreaming={isStreaming}
          maxChars={maxChars}
          placeholder={composerPlaceholder}
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
          overflow: "auto",
          px: 2,
          py: 2,
          maxWidth: 960,
          mx: "auto",
          width: "100%",
        }}
      >
        {isLoading && activeConversationId && <LoadingSkeleton />}

        {turns.map((turn) => {
          const rows =
            resultSamplesRef.current.get(turn.id) ??
            turn.query_result_sample ??
            undefined;
          return (
            <Box key={turn.id}>
              <UserMessage text={turn.user_message} />
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
              />
              {renderTurnActions?.(turn, rows)}
            </Box>
          );
        })}

        {optimisticMessages
          .filter((m) => !turns.some((t) => t.user_message === m.text))
          .map((m) => (
            <UserMessage key={m.id} text={m.text} />
          ))}

        {streamingDraft && (
          <Box sx={{ mb: 2 }}>
            <Box
              sx={{
                p: 2,
                border: 1,
                borderColor: "divider",
                borderRadius: 2,
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
                    mb: 1,
                    p: 0.75,
                    bgcolor: "action.hover",
                    borderRadius: 1,
                  }}
                >
                  <Box sx={{ display: "flex", alignItems: "center", gap: 0.5 }}>
                    <IconButton
                      size="small"
                      onClick={() => setStreamThoughtOpen((v) => !v)}
                      aria-label={streamThoughtOpen ? t("chat.collapseThought") : t("chat.expandThought")}
                      sx={{ p: 0.25 }}
                    >
                      {streamThoughtOpen ? (
                        <ExpandLess fontSize="small" />
                      ) : (
                        <ExpandMore fontSize="small" />
                      )}
                    </IconButton>
                    <Typography variant="caption" color="text.secondary" fontWeight={650}>
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
                        fontSize: 12,
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
                      fontSize: 12,
                      lineHeight: 1.4,
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
                <Box sx={{ mb: 1 }}>
                  <InlineStepCard steps={streamingDraft.steps} />
                </Box>
              )}
              <Box
                sx={{
                  fontSize: 15,
                  lineHeight: 1.6,
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

      {showScrollButton && <ScrollToBottomButton onClick={scrollToBottom} />}

      <ChatComposer
        key={composerKey}
        initialText={prefillText}
        onSend={handleSend}
        onAbort={handleAbort}
        isStreaming={isStreaming}
        maxChars={maxChars}
        placeholder={composerPlaceholder}
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
