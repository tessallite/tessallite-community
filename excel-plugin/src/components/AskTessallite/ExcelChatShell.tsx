import { useCallback, useEffect, useState } from "react";
import {
  Box,
  Typography,
  IconButton,
  Menu,
  MenuItem,
  Dialog,
  DialogTitle,
  DialogContent,
  DialogActions,
  Button,
  Skeleton,
} from "@mui/material";
import { Add, History, DeleteOutline, SmartToy } from "@mui/icons-material";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ChatProvider,
  ChatCanvas,
  useConversationStore,
  type AgentChatAdapter,
  type AgentConfig,
  type TurnResponse,
  type ConversationResponse,
  type ConversationState,
} from "@tessallite/shared-ui";
import InsertActions from "./InsertActions";
import EmptyState from "../common/EmptyState";
import { tokens } from "../../theme";
import { recommendChartType, type ChartTypeRecommendation } from "../../utils/excelCharts";
import { strings, templates } from "../../i18n/strings";
import { useToast } from "../Toast/ToastProvider";
// Bug-6520: inline the charts-css stylesheet TEXT so agent HTML artifacts render
// styled inside the sandboxed task-pane iframe. RenderedOutput's `<link
// rel="stylesheet" href="/charts.min.css">` 404s in the Excel host (the plugin
// is served under /excel-plugin/, not the origin root), leaving charts unstyled.
// `?inline` yields the CSS as a string with no external request or CDN.
import chartsCssText from "charts.css/dist/charts.min.css?inline";

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

function ConversationHeader({
  providerModel,
  conversations,
  activeConversationId,
  onNewConversation,
  onSelectConversation,
  onDeleteConversation,
}: {
  providerModel?: string;
  conversations: ConversationResponse[];
  activeConversationId: string | null;
  onNewConversation: () => void;
  onSelectConversation: (id: string) => void;
  onDeleteConversation: (id: string) => void;
}) {
  const [historyAnchor, setHistoryAnchor] = useState<HTMLElement | null>(null);
  const [deleteConfirmId, setDeleteConfirmId] = useState<string | null>(null);

  return (
    <>
      <Box
        sx={{
          px: 1.5,
          py: 0.5,
          display: "flex",
          gap: 0.5,
          alignItems: "center",
          borderBottom: `1px solid ${tokens.colorBorderLight}`,
        }}
      >
        {providerModel && (
          <Typography
            sx={{
              fontSize: 11,
              color: tokens.colorTextSecondary,
              fontWeight: 600,
              flex: 1,
            }}
          >
            {providerModel}
          </Typography>
        )}
        <IconButton
          size="small"
          onClick={onNewConversation}
          title={strings.chatShell.newConversation}
          aria-label={strings.chatShell.newConversation}
          sx={{ p: 0.25 }}
        >
          <Add sx={{ fontSize: 16, color: tokens.colorPrimary }} />
        </IconButton>
        {conversations.length > 0 && (
          <IconButton
            size="small"
            onClick={(e) => setHistoryAnchor(e.currentTarget)}
            title={strings.chatShell.conversationHistory}
            aria-label={strings.chatShell.conversationHistory}
            sx={{ p: 0.25 }}
          >
            <History sx={{ fontSize: 16, color: tokens.colorTextSecondary }} />
          </IconButton>
        )}
      </Box>

      <Menu
        anchorEl={historyAnchor}
        open={Boolean(historyAnchor)}
        onClose={() => setHistoryAnchor(null)}
      >
        {conversations.map((c) => (
          <MenuItem
            key={c.id}
            selected={c.id === activeConversationId}
            // F-037-03: attach selection to the MenuItem itself, not an inner
            // Box. MUI moves keyboard focus to the MenuItem; a handler on a
            // descendant Box never receives the Enter/Space activation, so
            // keyboard-only Excel users could not return to an existing
            // conversation. delete keeps its own stopPropagation below.
            onClick={() => {
              setHistoryAnchor(null);
              onSelectConversation(c.id);
            }}
            sx={{ display: "flex", justifyContent: "space-between" }}
          >
            <Box sx={{ flex: 1 }}>
              <Typography sx={{ fontSize: 11 }}>
                {c.title || c.id.slice(0, 8)}
              </Typography>
            </Box>
            <IconButton
              size="small"
              onClick={(e) => {
                e.stopPropagation();
                setHistoryAnchor(null);
                setDeleteConfirmId(c.id);
              }}
              sx={{ p: 0.25 }}
              aria-label={templates.chatShell.deleteConversationAria(c.title || c.id.slice(0, 8))}
            >
              <DeleteOutline sx={{ fontSize: 14, color: tokens.colorRed }} />
            </IconButton>
          </MenuItem>
        ))}
      </Menu>

      <Dialog open={deleteConfirmId !== null} onClose={() => setDeleteConfirmId(null)}>
        <DialogTitle sx={{ fontSize: 14, fontWeight: 700 }}>{strings.chatShell.deleteTitle}</DialogTitle>
        <DialogContent>
          <Typography sx={{ fontSize: 13 }}>
            {strings.chatShell.deleteConfirmation}
          </Typography>
        </DialogContent>
        <DialogActions>
          <Button size="small" onClick={() => setDeleteConfirmId(null)} sx={{ textTransform: "none" }}>
            {strings.chatShell.cancel}
          </Button>
          <Button
            size="small"
            variant="contained"
            onClick={() => {
              if (deleteConfirmId) {
                onDeleteConversation(deleteConfirmId);
                setDeleteConfirmId(null);
              }
            }}
            sx={{ textTransform: "none" }}
          >
            {strings.chatShell.delete}
          </Button>
        </DialogActions>
      </Dialog>
    </>
  );
}

export default function ExcelChatShell({
  adapter,
  t,
  projectId,
  config,
  activeModelId,
  activePersonaId,
  agentConfigured,
  providerModel,
  loading,
  error,
  onInsertTable,
  onInsertChart,
  onInsertLocalPivot,
  onFeedback,
}: ExcelChatShellProps) {
  const startNewConversation = useConversationStore((s: ConversationState) => s.startNewConversation);
  const setActiveConversation = useConversationStore((s: ConversationState) => s.setActiveConversation);
  const activeConversationId = useConversationStore((s: ConversationState) => s.activeConversationId);
  const setPendingPersonaId = useConversationStore((s: ConversationState) => s.setPendingPersonaId);
  const queryClient = useQueryClient();

  useEffect(() => {
    setPendingPersonaId(activePersonaId ?? null);
  }, [activePersonaId, setPendingPersonaId]);

  const { data: conversations = [] } = useQuery({
    queryKey: ["conversations", projectId],
    queryFn: () => adapter.getConversations(projectId),
    enabled: !!projectId && agentConfigured,
  });

  // Bug-6734: route through the centralized toast helper instead of a
  // component-local Snackbar with an ad-hoc autoHideDuration.
  const { showToast } = useToast();

  const handleSelectConversation = useCallback(
    (id: string) => {
      const conv = conversations.find((c) => c.id === id);
      if (conv) {
        const personaMismatch =
          (activePersonaId ?? null) !== (conv.persona_id ?? null);
        const modelMismatch =
          (activeModelId ?? null) !== (conv.pinned_model_id ?? null);
        if (personaMismatch || modelMismatch) {
          startNewConversation();
          showToast(t("chat.scopeMismatch"), 'info');
          return;
        }
      }
      setActiveConversation(id);
    },
    [conversations, activePersonaId, activeModelId, setActiveConversation, startNewConversation, t, showToast],
  );

  const handleDeleteConversation = useCallback(
    async (id: string) => {
      try {
        await adapter.deleteConversation(projectId, id);
        if (activeConversationId === id) {
          startNewConversation();
        }
        queryClient.invalidateQueries({ queryKey: ["conversations", projectId] });
      } catch {
        // silent
      }
    },
    [adapter, projectId, activeConversationId, startNewConversation, queryClient],
  );

  const renderTurnActions = useCallback(
    (turn: TurnResponse, resultRows?: Record<string, unknown>[]) => {
      if (!resultRows || resultRows.length === 0) return null;
      const headers = Object.keys(resultRows[0]);
      const rows = resultRows.map((r) =>
        headers.map((h) => r[h] as string | number),
      );

      let recommendedAction: "table" | "chart" | "pivot" | "cube" | undefined;
      let chartRec: ChartTypeRecommendation | undefined;
      if (rows.length === 0 || headers.length < 2) {
        recommendedAction = "table";
      } else if (turn.chart_type && turn.chart_type !== "kpi") {
        recommendedAction = "chart";
      } else {
        const rec = recommendChartType(headers, rows);
        chartRec = rec.chartType;
        if (
          rec.confidence === "high" &&
          (rec.chartType === "line" || rec.chartType === "columnClustered")
        ) {
          recommendedAction = "chart";
        } else {
          recommendedAction = "table";
        }
      }

      return (
        <Box sx={{ px: 1, mb: 1 }}>
          <InsertActions
            data={resultRows}
            headers={headers}
            onInsertTable={onInsertTable ? () => onInsertTable(turn) : undefined}
            onInsertChart={onInsertChart ? () => onInsertChart(turn, chartRec) : undefined}
            onLocalPivot={onInsertLocalPivot ? () => onInsertLocalPivot(turn) : undefined}
            recommendedAction={recommendedAction}
          />
        </Box>
      );
    },
    [onInsertTable, onInsertChart, onInsertLocalPivot],
  );

  if (loading) {
    return (
      <Box
        sx={{
          flex: 1,
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "center",
          p: 3,
          gap: 1,
        }}
      >
        <Skeleton variant="text" width="60%" height={16} />
        <Skeleton variant="text" width="80%" height={16} />
        <Skeleton variant="rectangular" width="80%" height={48} sx={{ borderRadius: 1, mt: 2 }} />
      </Box>
    );
  }

  if (error) {
    return (
      <Box
        sx={{
          flex: 1,
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "center",
          p: 3,
        }}
      >
        <Typography sx={{ fontSize: 13, fontWeight: 700, color: tokens.colorCharcoal, mb: 1, textAlign: "center" }}>
          {error}
        </Typography>
      </Box>
    );
  }

  if (!agentConfigured) {
    return (
      <Box sx={{ flex: 1 }}>
        <EmptyState
          icon={<SmartToy sx={{ fontSize: 48, color: tokens.colorGoldDark }} />}
          title={strings.chatShell.unavailableTitle}
          description={strings.chatShell.unavailableDescription}
        />
      </Box>
    );
  }

  // F-037-04: derive a fail-closed visibility object from the loaded config and
  // pass it to ChatCanvas, matching the main and standalone hosts. Without this
  // Excel showed completed-turn trace fields whenever present and allowed
  // streaming thought — silently depending on server redaction while the other
  // hosts enforce the client-side half of the two-sided policy too. Boolean(...)
  // defaults every flag to false when the config is null/unreachable.
  const visibility = {
    showThoughtProcess: Boolean(config?.show_thought_process),
    showSemanticQuery: Boolean(config?.show_semantic_query),
    showPhysicalQuery: Boolean(config?.show_physical_query),
  };

  return (
    <>
      <ChatProvider
        adapter={adapter}
        t={t}
        projectId={projectId}
        config={config}
        activeModelId={activeModelId}
      >
        <ChatCanvas
          visibility={visibility}
          headerSlot={
            <ConversationHeader
              providerModel={providerModel}
              conversations={conversations}
              activeConversationId={activeConversationId}
              onNewConversation={startNewConversation}
              onSelectConversation={handleSelectConversation}
              onDeleteConversation={handleDeleteConversation}
            />
          }
          onFeedback={onFeedback}
          feedbackEnabled={Boolean(config?.feedback_enabled)}
          chartsCss={chartsCssText}
          renderTurnActions={renderTurnActions}
          composerPlaceholder={strings.chatShell.composerPlaceholder}
        />
      </ChatProvider>
    </>
  );
}
