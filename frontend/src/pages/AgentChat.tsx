import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Box,
  CircularProgress,
  Divider,
  FormControl,
  IconButton,
  InputAdornment,
  List,
  ListItemButton,
  ListItemText,
  Paper,
  InputLabel,
  MenuItem,
  Select,
  Stack,
  TextField,
  Tooltip,
  Typography,
  Alert,
} from "@mui/material";
import AddIcon from "@mui/icons-material/Add";
import ArrowBackIcon from "@mui/icons-material/ArrowBack";
import DeleteIcon from "@mui/icons-material/DeleteOutline";
import PushPinIcon from "@mui/icons-material/PushPin";
import PushPinOutlinedIcon from "@mui/icons-material/PushPinOutlined";
import SearchIcon from "@mui/icons-material/Search";
import {
  agentApi,
  type AgentConversation,
} from "../api/agentApi";
import { mainAppAdapter, setProjectPersonaWriteBarrier } from "../api/agentChatAdapter";
import { useProject } from "../api/hooks";
import { useConfirm } from "../components/Confirm/useConfirm";
import HelpIconButton from "../components/HelpIconButton";
import { useT } from "../i18n";
import { TESSALLITE_ECHARTS_THEME } from "../theme/echartsTheme";
import chartsCss from "../assets/charts.min.css?raw";
import {
  ChatProvider,
  ChatCanvas,
  TraceDrawer,
  useConversationStore,
  type TurnResponse,
} from "@tessallite/shared-ui";

export default function AgentChat() {
  const t = useT();
  const { tenantId, projectId } = useParams<{
    tenantId: string;
    projectId: string;
  }>();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const project = useProject(projectId!);
  const qc = useQueryClient();
  const confirm = useConfirm();
  const [traceTurn, setTraceTurn] = useState<TurnResponse | null>(null);

  const activeConversationId = useConversationStore(
    (s: { activeConversationId: string | null }) => s.activeConversationId,
  );
  const setActiveConversation = useConversationStore(
    (s: { setActiveConversation: (id: string | null) => void }) => s.setActiveConversation,
  );
  const pendingProjectPersonaId = useConversationStore(
    (s: { pendingPersonaId: string | null }) => s.pendingPersonaId,
  );
  const setPendingProjectPersonaId = useConversationStore(
    (s: { setPendingPersonaId: (id: string | null) => void }) => s.setPendingPersonaId,
  );
  const conversationIdFromUrl = searchParams.get("conversation");

  useEffect(() => {
    if (
      conversationIdFromUrl &&
      conversationIdFromUrl !== activeConversationId
    ) {
      setActiveConversation(conversationIdFromUrl);
    }
  }, [
    activeConversationId,
    conversationIdFromUrl,
    setActiveConversation,
  ]);

  const configQuery = useQuery({
    queryKey: ["agent-config", projectId],
    queryFn: () => agentApi.getConfig(projectId!),
    enabled: Boolean(projectId),
  });

  const conversationsQuery = useQuery({
    queryKey: ["agent-conversations", projectId],
    queryFn: () => agentApi.listConversations(projectId!),
    enabled: Boolean(projectId) && configQuery.data?.enabled === true,
  });

  // Agent personas are ProjectPersona records.  They are intentionally kept
  // on the conversation API and never passed through the model Persona
  // picker used by SQL/query surfaces.
  const projectPersonasQuery = useQuery({
    queryKey: ["agent-project-personas", projectId],
    queryFn: () => agentApi.listPersonas(projectId!),
    enabled: Boolean(projectId) && configQuery.data?.enabled === true,
  });

  const activeConversation = useMemo(
    () =>
      (conversationsQuery.data ?? []).find(
        (conversation) => conversation.id === activeConversationId,
      ) ?? null,
    [conversationsQuery.data, activeConversationId],
  );
  const [projectPersonaSelection, setProjectPersonaSelection] =
    useState<string | null>(null);
  const [projectPersonaError, setProjectPersonaError] = useState<string | null>(null);
  const selectedProjectPersonaId = projectPersonaSelection;

  // Keep a new-conversation selection in the shared store, but do not let a
  // stale conversation-list query overwrite an in-flight persona update on an
  // already active conversation. The list query is the eventual authority for
  // that active conversation and will resync the picker when its persona_id
  // changes.
  useEffect(() => {
    if (!activeConversation && !activeConversationId) {
      setProjectPersonaSelection(pendingProjectPersonaId);
    }
  }, [activeConversation, activeConversationId, pendingProjectPersonaId]);

  useEffect(() => {
    if (activeConversation) {
      setProjectPersonaSelection(activeConversation.persona_id);
      setPendingProjectPersonaId(activeConversation.persona_id);
    }
  }, [
    activeConversation?.id,
    activeConversation?.persona_id,
    setPendingProjectPersonaId,
  ]);

  const createConv = useMutation({
    mutationFn: () =>
      mainAppAdapter.createConversation(projectId!, {
        // The agent service calls this ProjectPersona.  Do not substitute a
        // model Persona ID here; the two contracts have different scopes.
        personaId: projectPersonaSelection,
      }),
    onSuccess: (c) => {
      setActiveConversation(c.id);
      qc.invalidateQueries({ queryKey: ["agent-conversations", projectId] });
    },
  });

  const updateProjectPersona = useMutation({
    mutationFn: (personaId: string | null) =>
      agentApi.patchConversation(projectId!, activeConversationId!, {
        persona_id: personaId,
      }),
    onMutate: async (_personaId) => {
      await qc.cancelQueries({ queryKey: ["agent-conversations", projectId] });
      const previous = qc.getQueryData<AgentConversation[]>([
        "agent-conversations", projectId,
      ]);
      // Keep the picker and conversation list on the persisted value while
      // PATCH is in flight. Optimistically changing either one lets Agent
      // Chat display an authority that the backend has not accepted yet.
      return {
        previous,
        prior: activeConversation?.persona_id ?? projectPersonaSelection,
      };
    },
    onSuccess: (updated, personaId) => {
      const persisted = updated?.persona_id ?? personaId;
      setProjectPersonaSelection(persisted);
      setPendingProjectPersonaId(persisted);
      setProjectPersonaError(null);
      qc.invalidateQueries({ queryKey: ["agent-conversations", projectId] });
    },
    onError: (err: any, _personaId, context) => {
      if (context?.previous) {
        qc.setQueryData(["agent-conversations", projectId], context.previous);
      }
      const prior = context?.prior ?? null;
      setProjectPersonaSelection(prior);
      setPendingProjectPersonaId(prior);
      setProjectPersonaError(
        err?.response?.data?.detail ?? t("agentChat.projectPersonaSaveFailed"),
      );
    },
  });

  function handleProjectPersonaChange(personaId: string) {
    const selected = personaId || null;
    setProjectPersonaError(null);
    if (activeConversationId) {
      // An active conversation's displayed persona is its persisted execution
      // authority. Hold it steady until the PATCH resolves; ChatCanvas is
      // gated below for the same transition.
      setProjectPersonaWriteBarrier(updateProjectPersona.mutateAsync(selected));
      return;
    }
    setProjectPersonaSelection(selected);
    setPendingProjectPersonaId(selected);
  }

  const togglePin = useMutation({
    mutationFn: (args: { id: string; pinned: boolean }) =>
      agentApi.patchConversation(projectId!, args.id, {
        pinned: args.pinned,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["agent-conversations", projectId] });
    },
  });

  const deleteConv = useMutation({
    mutationFn: (id: string) => agentApi.deleteConversation(projectId!, id),
    onSuccess: (_data, id) => {
      if (activeConversationId === id) setActiveConversation(null);
      qc.invalidateQueries({ queryKey: ["agent-conversations", projectId] });
    },
  });

  const handleFeedback = useCallback(
    (turnId: string, vote: "up" | "down") => {
      if (!activeConversationId) return;
      agentApi.submitFeedback(
        projectId!,
        activeConversationId,
        turnId,
        vote,
      );
    },
    [projectId, activeConversationId],
  );

  const projectName =
    project.data?.display_name ?? project.data?.slug ?? "";

  const visibility = useMemo(
    () => ({
      showThoughtProcess: Boolean(configQuery.data?.show_thought_process),
      showSemanticQuery: Boolean(configQuery.data?.show_semantic_query),
      showPhysicalQuery: Boolean(configQuery.data?.show_physical_query),
    }),
    [configQuery.data],
  );

  const headerStrip = (
    <Box
      sx={{
        display: "flex",
        alignItems: "center",
        px: 1.5,
        py: 0.5,
        borderBottom: 1,
        borderColor: "divider",
        gap: 1,
      }}
    >
      <Tooltip title={t("agentChat.backToProject")}>
        <IconButton
          size="small"
          onClick={() =>
            navigate(`/tenants/${tenantId}/projects/${projectId}`)
          }
        >
          <ArrowBackIcon fontSize="small" />
        </IconButton>
      </Tooltip>
      <Typography variant="h6" fontWeight={700} noWrap>
        {projectName}
        <Typography
          component="span"
          variant="h6"
          fontWeight={400}
          color="text.secondary"
          sx={{ mx: 0.75 }}
        >
          |
        </Typography>
        {t("agentChat.conversationalAgent")}
      </Typography>
      <FormControl size="small" sx={{ minWidth: 210, ml: "auto" }}>
        <InputLabel id="project-persona-label">
          {t("agentChat.projectPersona")}
        </InputLabel>
        <Select
          labelId="project-persona-label"
          value={selectedProjectPersonaId ?? ""}
          label={t("agentChat.projectPersona")}
          onChange={(event) => handleProjectPersonaChange(event.target.value)}
          disabled={projectPersonasQuery.isLoading || updateProjectPersona.isPending}
        >
          <MenuItem value="">
            {t("agentChat.projectPersonaNone")}
          </MenuItem>
          {(projectPersonasQuery.data ?? []).map((persona) => (
            <MenuItem key={persona.id} value={persona.id}>
              {persona.name}
            </MenuItem>
          ))}
        </Select>
      </FormControl>
      {projectPersonaError && (
        <Alert severity="error" sx={{ py: 0, ml: 1 }} role="alert">
          {projectPersonaError}
        </Alert>
      )}
      <HelpIconButton href="/help/agent/agent-chat.html" />
    </Box>
  );

  if (configQuery.isLoading) {
    return (
      <Box>
        {headerStrip}
        <Box sx={{ p: 6, textAlign: "center" }}>
          <CircularProgress />
        </Box>
      </Box>
    );
  }

  if (!configQuery.data?.enabled) {
    return (
      <Box>
        {headerStrip}
        <Box sx={{ p: 4, maxWidth: 720, mx: "auto" }}>
          <Alert severity="warning">
            {t("agentChat.agentNotEnabled")}
          </Alert>
        </Box>
      </Box>
    );
  }

  return (
    <Box
      sx={{
        display: "flex",
        flexDirection: "column",
        height: "calc(100vh - 64px)",
      }}
    >
      {headerStrip}
      <Box sx={{ display: "flex", flex: 1, minHeight: 0 }}>
        <ConversationList
          conversations={conversationsQuery.data ?? []}
          activeId={activeConversationId}
          loading={conversationsQuery.isLoading}
          disabled={updateProjectPersona.isPending}
          onSelect={setActiveConversation}
          onCreate={() => createConv.mutate()}
          onDelete={async (id) => {
            const ok = await confirm({
              title: t("agentChat.deleteConversationTitle"),
              message: t("agentChat.deleteConversationMessage"),
            });
            if (ok) deleteConv.mutate(id);
          }}
          onTogglePin={(id, pinned) =>
            togglePin.mutate({ id, pinned })
          }
        />

        <Divider orientation="vertical" flexItem />

        <ChatProvider
          adapter={mainAppAdapter}
          t={t}
          projectId={projectId!}
          config={configQuery.data}
        >
          <Box sx={{ flex: 1, display: "flex", flexDirection: "column" }}>
            {!activeConversationId ? (
              <Box
                sx={{
                  p: 6,
                  textAlign: "center",
                  color: "text.secondary",
                }}
              >
                <Typography>{t("agentChat.selectOrCreate")}</Typography>
              </Box>
            ) : (
              <ChatCanvas
                visibility={visibility}
                onFeedback={handleFeedback}
                feedbackEnabled={Boolean(
                  configQuery.data?.feedback_enabled,
                )}
                onOpenTrace={setTraceTurn}
                disabled={updateProjectPersona.isPending}
                showModelPicker
                echartsTheme={TESSALLITE_ECHARTS_THEME}
                chartsCss={chartsCss}
              />
            )}
          </Box>

          <TraceDrawer
            open={Boolean(traceTurn)}
            onClose={() => setTraceTurn(null)}
            turn={traceTurn}
            visibility={visibility}
          />
        </ChatProvider>
      </Box>
    </Box>
  );
}

const ConversationList = memo(function ConversationList({
  conversations,
  activeId,
  loading,
  disabled,
  onSelect,
  onCreate,
  onDelete,
  onTogglePin,
}: {
  conversations: AgentConversation[];
  activeId: string | null;
  loading: boolean;
  disabled?: boolean;
  onSelect: (id: string | null) => void;
  onCreate: () => void;
  onDelete: (id: string) => void;
  onTogglePin: (id: string, pinned: boolean) => void;
}) {
  const t = useT();
  const [searchTerm, setSearchTerm] = useState("");
  const searchRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "k") {
        e.preventDefault();
        searchRef.current?.focus();
      }
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, []);

  const visible = useMemo(() => {
    let filtered = conversations.filter((c) => !c.deleted_at);
    if (searchTerm.trim()) {
      const lc = searchTerm.toLowerCase();
      filtered = filtered.filter((c) =>
        (c.title ?? "").toLowerCase().includes(lc),
      );
    }
    filtered.sort((a, b) => {
      const ap = a.pinned_at ? 1 : 0;
      const bp = b.pinned_at ? 1 : 0;
      if (ap !== bp) return bp - ap;
      return (
        new Date(b.last_active_at).getTime() -
        new Date(a.last_active_at).getTime()
      );
    });
    return filtered;
  }, [conversations, searchTerm]);

  return (
    <Paper
      square
      variant="outlined"
      sx={{
        width: 280,
        borderRight: 0,
        borderTop: 0,
        borderBottom: 0,
        display: "flex",
        flexDirection: "column",
      }}
    >
      <Stack
        direction="row"
        alignItems="center"
        justifyContent="space-between"
        sx={{ p: 1.25 }}
      >
        <Typography variant="subtitle2" fontWeight={700}>
          {t("agentChat.conversationsHeader")}
        </Typography>
        <Tooltip title={t("agentChat.newConversationTooltip")}>
          <IconButton size="small" onClick={onCreate} disabled={disabled}>
            <AddIcon fontSize="small" />
          </IconButton>
        </Tooltip>
      </Stack>
      <Box sx={{ px: 1, pb: 0.5 }}>
        <TextField
          inputRef={searchRef}
          size="small"
          placeholder={t("agentChat.searchPlaceholder")}
          value={searchTerm}
          onChange={(e) => setSearchTerm(e.target.value)}
          fullWidth
          InputProps={{
            startAdornment: (
              <InputAdornment position="start">
                <SearchIcon sx={{ fontSize: 16 }} />
              </InputAdornment>
            ),
            sx: { fontSize: 12, py: 0.25 },
          }}
        />
      </Box>
      <Divider />
      {loading ? (
        <Box sx={{ p: 2, textAlign: "center" }}>
          <CircularProgress size={20} />
        </Box>
      ) : (
        <List dense disablePadding sx={{ overflowY: "auto" }}>
          {visible.length === 0 && (
            <Typography
              variant="body2"
              color="text.secondary"
              sx={{ p: 2 }}
            >
              {t("agentChat.noConversations")}
            </Typography>
          )}
          {visible.map((c) => (
            <ListItemButton
              key={c.id}
              selected={c.id === activeId}
              onClick={() => onSelect(c.id)}
              disabled={disabled}
            >
              <ListItemText
                primary={
                  c.title
                    ? c.title.length > 60
                      ? c.title.slice(0, 57) + "..."
                      : c.title
                    : new Date(c.started_at).toLocaleDateString()
                }
                secondary={new Date(c.last_active_at).toLocaleString(
                  undefined,
                  {
                    month: "short",
                    day: "numeric",
                    hour: "2-digit",
                    minute: "2-digit",
                  },
                )}
                primaryTypographyProps={{
                  variant: "body2",
                  noWrap: true,
                  title: c.title ?? undefined,
                }}
                secondaryTypographyProps={{ variant: "caption" }}
              />
              <Tooltip
                title={
                  c.pinned_at
                    ? t("agentChat.unpinTooltip")
                    : t("agentChat.pinTooltip")
                }
              >
                <IconButton
                  size="small"
                  disabled={disabled}
                  onClick={(e) => {
                    e.stopPropagation();
                    onTogglePin(c.id, !c.pinned_at);
                  }}
                >
                  {c.pinned_at ? (
                    <PushPinIcon fontSize="small" color="primary" />
                  ) : (
                    <PushPinOutlinedIcon fontSize="small" />
                  )}
                </IconButton>
              </Tooltip>
              <IconButton
                size="small"
                disabled={disabled}
                onClick={(e) => {
                  e.stopPropagation();
                  onDelete(c.id);
                }}
              >
                <DeleteIcon fontSize="small" />
              </IconButton>
            </ListItemButton>
          ))}
        </List>
      )}
    </Paper>
  );
});
