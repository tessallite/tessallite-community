import { useCallback, useEffect, useState } from "react";
import {
  Alert,
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
  Snackbar,
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
          title="New conversation"
          aria-label="New conversation"
          sx={{ p: 0.25 }}
        >
          <Add sx={{ fontSize: 16, color: tokens.colorPrimary }} />
        </IconButton>
        {conversations.length > 0 && (
          <IconButton
            size="small"
            onClick={(e) => setHistoryAnchor(e.currentTarget)}
            title="Conversation history"
            aria-label="Conversation history"
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
            sx={{ display: "flex", justifyContent: "space-between" }}
          >
            <Box
              sx={{ flex: 1 }}
              onClick={() => {
                setHistoryAnchor(null);
                onSelectConversation(c.id);
              }}
            >
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
              aria-label={`Delete conversation ${c.title || c.id.slice(0, 8)}`}
            >
              <DeleteOutline sx={{ fontSize: 14, color: tokens.colorRed }} />
            </IconButton>
          </MenuItem>
        ))}
      </Menu>

      <Dialog open={deleteConfirmId !== null} onClose={() => setDeleteConfirmId(null)}>
        <DialogTitle sx={{ fontSize: 14, fontWeight: 700 }}>Delete Conversation</DialogTitle>
        <DialogContent>
          <Typography sx={{ fontSize: 13 }}>
            This action cannot be undone. Delete this conversation?
          </Typography>
        </DialogContent>
        <DialogActions>
          <Button size="small" onClick={() => setDeleteConfirmId(null)} sx={{ textTransform: "none" }}>
            Cancel
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
            Delete
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

  const [scopeToast, setScopeToast] = useState<string | null>(null);

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
          setScopeToast(
            t("chat.scopeMismatch"),
          );
          return;
        }
      }
      setActiveConversation(id);
    },
    [conversations, activePersonaId, activeModelId, setActiveConversation, startNewConversation, t],
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
      if (rows.length === 0 || headers.length < 2) {
        recommendedAction = "table";
      } else if (turn.chart_type && turn.chart_type !== "kpi") {
        recommendedAction = "chart";
      } else {
        const rec = recommendChartType(headers, rows);
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
            onInsertChart={onInsertChart ? () => onInsertChart(turn) : undefined}
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
          title="Conversational analytics unavailable"
          description="Contact your Tessallite administrator to configure an LLM provider."
        />
      </Box>
    );
  }

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
          renderTurnActions={renderTurnActions}
          composerPlaceholder="Ask a question about your data..."
        />
      </ChatProvider>
      <Snackbar
        open={Boolean(scopeToast)}
        autoHideDuration={4000}
        onClose={() => setScopeToast(null)}
        anchorOrigin={{ vertical: "bottom", horizontal: "center" }}
      >
        <Alert severity="info" onClose={() => setScopeToast(null)} sx={{ fontSize: 12 }}>
          {scopeToast}
        </Alert>
      </Snackbar>
    </>
  );
}
