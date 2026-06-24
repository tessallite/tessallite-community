import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  Box,
  Button,
  Checkbox,
  CircularProgress,
  Dialog,
  DialogContent,
  DialogTitle,
  Divider,
  FormControl,
  FormControlLabel,
  FormGroup,
  IconButton,
  InputLabel,
  MenuItem,
  Radio,
  RadioGroup,
  Select,
  Stack,
  Switch,
  TextField,
  Typography,
} from "@mui/material";
import CloseIcon from "@mui/icons-material/Close";
import {
  agentApi,
  AgentConfig,
  DEFAULT_AGENT_CONFIG,
  type ConversationStats,
  type CostReport,
} from "../../api/agentApi";
import { modelsApi } from "../../api/client";
import type { Model } from "../../api/types";
import ModelContextDialog from "./ModelContextDialog";
import RecipesTab from "./RecipesTab";
import JudgeTab from "./JudgeTab";
import { useT } from "../../i18n";

export type AgentTabKey =
  | "setup"
  | "identity"
  | "knowledge"
  | "guardrails"
  | "advanced";

type Props = { projectId: string; tab: AgentTabKey };

export default function ProjectAgentTabs({ projectId, tab }: Props) {
  const qc = useQueryClient();
  const t = useT();
  const [draft, setDraft] = useState<AgentConfig>(DEFAULT_AGENT_CONFIG);
  const [error, setError] = useState<string | null>(null);

  const configQuery = useQuery({
    queryKey: ["agent-config", projectId],
    queryFn: () => agentApi.getConfig(projectId),
    enabled: Boolean(projectId),
  });


  useEffect(() => {
    if (configQuery.data) {
      setDraft({ ...DEFAULT_AGENT_CONFIG, ...configQuery.data });
    } else if (configQuery.data === null) {
      setDraft(DEFAULT_AGENT_CONFIG);
    }
  }, [configQuery.data]);

  const upsertConfig = useMutation({
    mutationFn: (body: AgentConfig) => agentApi.upsertConfig(projectId, body),
    onSuccess: (data) => {
      setDraft({ ...DEFAULT_AGENT_CONFIG, ...data });
      setError(null);
      qc.invalidateQueries({ queryKey: ["agent-config", projectId] });
    },
    onError: (err: { response?: { data?: { detail?: string } } }) => {
      setError(err.response?.data?.detail ?? t("agent.setup.saveFailed"));
    },
  });

  function update<K extends keyof AgentConfig>(key: K, value: AgentConfig[K]) {
    setDraft((d) => ({ ...d, [key]: value }));
  }

  if (configQuery.isLoading) {
    return (
      <Box sx={{ p: 4, textAlign: "center" }}>
        <CircularProgress size={22} />
      </Box>
    );
  }

  const showConfigSave = tab !== "knowledge";

  return (
    <Stack spacing={2}>
      {error && <Alert severity="error">{error}</Alert>}

      {tab === "setup" && (
        <SetupPanel
          projectId={projectId}
          draft={draft}
          update={update}
        />
      )}
      {tab === "identity" && <IdentityPanel draft={draft} update={update} />}
      {tab === "knowledge" && <KnowledgePanel projectId={projectId} />}
      {tab === "guardrails" && (
        <GuardrailsPanel
          projectId={projectId}
          draft={draft}
          update={update}
        />
      )}
      {tab === "advanced" && (
        <AdvancedPanel
          projectId={projectId}
          draft={draft}
          update={update}
        />
      )}

      {showConfigSave && (
        <>
          <Divider />
          <Stack direction="row" justifyContent="flex-end" spacing={1}>
            <Button
              variant="contained"
              size="small"
              disabled={upsertConfig.isPending}
              onClick={() => upsertConfig.mutate(draft)}
            >
              {upsertConfig.isPending ? <CircularProgress size={14} /> : t("agent.setup.saveButton")}
            </Button>
          </Stack>
        </>
      )}
    </Stack>
  );
}

// ---------------------------------------------------------------------------
// Shared helpers
// ---------------------------------------------------------------------------

type ChangeFn = <K extends keyof AgentConfig>(
  key: K,
  value: AgentConfig[K],
) => void;

// ---------------------------------------------------------------------------
// Setup — enable + basics + model allow-list + answer LLM
// ---------------------------------------------------------------------------

function SetupPanel({
  projectId,
  draft,
  update,
}: {
  projectId: string;
  draft: AgentConfig;
  update: ChangeFn;
}) {
  const t = useT();
  const qc = useQueryClient();

  const modelsQuery = useQuery({
    queryKey: ["models", projectId],
    queryFn: () => modelsApi.list(projectId),
    enabled: Boolean(projectId),
  });
  const activeModels = useMemo(
    () => (modelsQuery.data ?? []).filter((m) => m.status === "active"),
    [modelsQuery.data],
  );

  const allowListQuery = useQuery({
    queryKey: ["agent-allow-list", projectId],
    queryFn: () => agentApi.listAllowList(projectId),
    enabled: Boolean(projectId),
  });
  const [draftAllow, setDraftAllow] = useState<string[]>([]);
  useEffect(() => {
    if (allowListQuery.data) setDraftAllow(allowListQuery.data);
  }, [allowListQuery.data]);

  const allowDirty = useMemo(() => {
    const server = new Set(allowListQuery.data ?? []);
    if (draftAllow.length !== server.size) return true;
    return draftAllow.some((id) => !server.has(id));
  }, [draftAllow, allowListQuery.data]);

  function toggleAllow(id: string) {
    setDraftAllow((cur) =>
      cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id],
    );
  }

  const replaceAllowList = useMutation({
    mutationFn: (ids: string[]) => agentApi.replaceAllowList(projectId, ids),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["agent-allow-list", projectId] });
    },
  });

  return (
    <Stack spacing={2}>
      <FormControlLabel
        control={
          <Switch
            checked={draft.enabled}
            onChange={(e) => update("enabled", e.target.checked)}
          />
        }
        label={
          <Typography variant="body2">
            {draft.enabled
              ? t("agent.setup.enabled")
              : t("agent.setup.disabled")}
          </Typography>
        }
      />
      {draft.enabled && (
        <Alert severity="info">
          {t("agent.enablingRequires")}
        </Alert>
      )}
      {draft.enabled && (
        <FormControlLabel
          control={
            <Switch
              checked={draft.enable_agent_log_screen ?? false}
              onChange={(e) => update("enable_agent_log_screen", e.target.checked)}
            />
          }
          label={
            <Typography variant="body2">
              {draft.enable_agent_log_screen
                ? t("agent.setup.logEnabled")
                : t("agent.setup.logDisabled")}
            </Typography>
          }
        />
      )}

      <TextField
        label={t("agent.setup.displayName")}
        value={draft.display_name ?? ""}
        onChange={(e) => update("display_name", e.target.value || null)}
        helperText={t("agent.setup.displayNameHelp")}
      />
      <Stack direction="row" spacing={2}>
        <TextField
          label={t("agent.setup.agentRole")}
          value={draft.agent_role ?? ""}
          onChange={(e) => update("agent_role", e.target.value)}
          helperText={t("agent.setup.agentRoleHelp")}
          sx={{ flex: 1 }}
        />
        <TextField
          label={t("agent.setup.locale")}
          value={draft.default_locale ?? ""}
          onChange={(e) => update("default_locale", e.target.value || null)}
          helperText={t("agent.setup.localeHelp")}
          sx={{ width: 140 }}
        />
      </Stack>

      <Divider />

      <Typography variant="subtitle2">{t("agent.setup.modelsHeading")}</Typography>
      <Typography variant="caption" color="text.secondary">
        {t("agent.modelsHelp")}
      </Typography>

      {activeModels.length === 0 ? (
        <Alert severity="warning" variant="outlined">
          {t("agent.noActiveModels")}
        </Alert>
      ) : (
        <>
          <Stack spacing={0}>
            {activeModels.map((m) => (
              <FormControlLabel
                key={m.id}
                control={
                  <Checkbox
                    checked={draftAllow.includes(m.id)}
                    onChange={() => toggleAllow(m.id)}
                    size="small"
                  />
                }
                label={
                  <Typography variant="body2">
                    {m.display_name}{" "}
                    <Typography
                      component="span"
                      variant="caption"
                      color="text.secondary"
                    >
                      ({m.slug})
                    </Typography>
                  </Typography>
                }
              />
            ))}
          </Stack>

          <Stack direction="row" spacing={1} justifyContent="flex-end">
            <Button
              size="small"
              variant="outlined"
              disabled={!allowDirty || replaceAllowList.isPending}
              onClick={() => setDraftAllow(allowListQuery.data ?? [])}
            >
              {t("agent.reset")}
            </Button>
            <Button
              size="small"
              variant="contained"
              disabled={!allowDirty || replaceAllowList.isPending}
              onClick={() => replaceAllowList.mutate(draftAllow)}
            >
              {t("agent.saveAllowList")}
            </Button>
          </Stack>

          <Typography variant="subtitle2" sx={{ mt: 1 }}>
            {t("agent.primaryModel")}
          </Typography>
          <RadioGroup
            value={draft.primary_model_id ?? ""}
            onChange={(_, v) => update("primary_model_id", v || null)}
          >
            <FormControlLabel
              value=""
              control={<Radio size="small" />}
              label={
                <Typography variant="body2" color="text.secondary">
                  {t("agent.noPrimary")}
                </Typography>
              }
            />
            {activeModels
              .filter((m) => draftAllow.includes(m.id))
              .map((m) => (
                <FormControlLabel
                  key={m.id}
                  value={m.id}
                  control={<Radio size="small" />}
                  label={
                    <Typography variant="body2">{m.display_name}</Typography>
                  }
                />
              ))}
          </RadioGroup>
        </>
      )}
    </Stack>
  );
}

// ---------------------------------------------------------------------------
// Identity & Tone — brief, tone, brand, disclosure
// ---------------------------------------------------------------------------

function IdentityPanel({
  draft,
  update,
}: {
  draft: AgentConfig;
  update: ChangeFn;
}) {
  const t = useT();
  return (
    <Stack spacing={2}>
      <Typography variant="caption" color="text.secondary">
        {t("agent.identityBriefHelp")}
      </Typography>
      <TextField
        label={t("agent.setup.projectBrief")}
        multiline
        minRows={5}
        value={draft.project_brief ?? ""}
        onChange={(e) => update("project_brief", e.target.value || null)}
      />
      <FormControl size="small">
        <InputLabel>{t("agent.setup.tonePreset")}</InputLabel>
        <Select
          label={t("agent.setup.tonePreset")}
          value={draft.tone_preset}
          onChange={(e) =>
            update("tone_preset", e.target.value as AgentConfig["tone_preset"])
          }
        >
          <MenuItem value="professional">{t("agent.setup.toneProfessional")}</MenuItem>
          <MenuItem value="friendly">{t("agent.setup.toneFriendly")}</MenuItem>
        </Select>
      </FormControl>
      <TextField
        label={t("agent.setup.toneOverrides")}
        multiline
        minRows={2}
        value={draft.tone_overrides ?? ""}
        onChange={(e) => update("tone_overrides", e.target.value || null)}
        helperText={t("agent.setup.toneRefinementsHelp")}
      />
      <TextField
        label={t("agent.setup.brandGuidelines")}
        multiline
        minRows={3}
        value={draft.brand_guidelines ?? ""}
        onChange={(e) => update("brand_guidelines", e.target.value || null)}
        helperText={t("agent.setup.brandGuidelinesHelp")}
      />
      <TextField
        label={t("agent.setup.disclosureText")}
        multiline
        minRows={2}
        value={draft.disclosure_text ?? ""}
        onChange={(e) => update("disclosure_text", e.target.value || null)}
        helperText={t("agent.disclosureTextHelp")}
      />
    </Stack>
  );
}

// ---------------------------------------------------------------------------
// Knowledge — per-model context + cross-model recipes
// ---------------------------------------------------------------------------

function KnowledgePanel({ projectId }: { projectId: string }) {
  const t = useT();
  const allowListQuery = useQuery({
    queryKey: ["agent-allow-list", projectId],
    queryFn: () => agentApi.listAllowList(projectId),
    enabled: Boolean(projectId),
  });
  const modelsQuery = useQuery({
    queryKey: ["models", projectId],
    queryFn: () => modelsApi.list(projectId),
    enabled: Boolean(projectId),
  });
  const [recipesOpen, setRecipesOpen] = useState(false);
  const [contextDialog, setContextDialog] = useState<{
    modelId: string;
    modelName: string;
  } | null>(null);

  const allowedModels = useMemo<Model[]>(() => {
    const ids = new Set(allowListQuery.data ?? []);
    return (modelsQuery.data ?? []).filter((m) => ids.has(m.id));
  }, [allowListQuery.data, modelsQuery.data]);

  return (
    <Stack spacing={2}>
      <Typography variant="subtitle2">{t("agent.setup.perModelContext")}</Typography>
      <Typography variant="caption" color="text.secondary">
        {t("agent.perModelContextHelp")}
      </Typography>
      {allowedModels.length === 0 ? (
        <Alert severity="info" variant="outlined">
          {t("agent.noAllowListedModels")}
        </Alert>
      ) : (
        <Stack spacing={0.5}>
          {allowedModels.map((m) => (
            <Stack
              key={m.id}
              direction="row"
              alignItems="center"
              justifyContent="space-between"
              sx={{
                py: 0.5,
                px: 1,
                borderRadius: 1,
                "&:hover": { bgcolor: "action.hover" },
              }}
            >
              <Typography variant="body2">
                {m.display_name}{" "}
                <Typography
                  component="span"
                  variant="caption"
                  color="text.secondary"
                >
                  ({m.slug})
                </Typography>
              </Typography>
              <Button
                size="small"
                variant="text"
                onClick={() =>
                  setContextDialog({
                    modelId: m.id,
                    modelName: m.display_name,
                  })
                }
              >
                {t("agent.setup.editContext")}
              </Button>
            </Stack>
          ))}
        </Stack>
      )}

      <Divider />

      <Typography variant="subtitle2">{t("agent.setup.crossModelRecipes")}</Typography>
      <Typography variant="caption" color="text.secondary">
        {t("agent.crossModelRecipesHelp")}
      </Typography>
      <Box>
        <Button
          size="small"
          variant="outlined"
          onClick={() => setRecipesOpen(true)}
        >
          {t("agent.setup.manageRecipes")}
        </Button>
      </Box>

      <DialogShell
        open={recipesOpen}
        title={t("agent.setup.crossModelRecipesTooltip")}
        onClose={() => setRecipesOpen(false)}
      >
        <RecipesTab
          projectId={projectId}
          publishedModels={(modelsQuery.data ?? []).filter(
            (m) => m.status === "active",
          )}
          allowList={allowListQuery.data ?? []}
        />
      </DialogShell>

      {contextDialog && (
        <ModelContextDialog
          open
          onClose={() => setContextDialog(null)}
          projectId={projectId}
          modelId={contextDialog.modelId}
          modelName={contextDialog.modelName}
        />
      )}
    </Stack>
  );
}

// ---------------------------------------------------------------------------
// Guardrails — safety policy + content rules + judge
// ---------------------------------------------------------------------------

function GuardrailsPanel({
  projectId,
  draft,
  update,
}: {
  projectId: string;
  draft: AgentConfig;
  update: ChangeFn;
}) {
  const t = useT();
  const rubricsQuery = useQuery({
    queryKey: ["agent-rubrics", projectId],
    queryFn: () => agentApi.listRubrics(projectId),
    enabled: Boolean(projectId),
  });
  const [rubricsOpen, setRubricsOpen] = useState(false);

  return (
    <Stack spacing={2}>
      <Typography variant="subtitle2">{t("agent.setup.safetyPolicy")}</Typography>
      <Typography variant="caption" color="text.secondary">
        {t("agent.safetyPolicyHelp")}
      </Typography>
      <TextField
        label={t("agent.setup.forbiddenTopics")}
        multiline
        minRows={4}
        value={draft.safety_policy ?? ""}
        onChange={(e) => update("safety_policy", e.target.value || null)}
      />
      <TextField
        label={t("agent.setup.contentRules")}
        multiline
        minRows={3}
        value={draft.content_rules ?? ""}
        onChange={(e) => update("content_rules", e.target.value || null)}
        helperText={t("agent.setup.contentRulesHelp")}
      />

      <Divider />

      <Typography variant="subtitle2">{t("agent.setup.judgeHeading")}</Typography>
      <Typography variant="caption" color="text.secondary">
        {t("agent.judgeHelp")}
      </Typography>

      <Stack direction="row" spacing={2}>
        <FormControl size="small" sx={{ flex: 1 }}>
          <InputLabel>{t("agent.judgeMode")}</InputLabel>
          <Select
            label={t("agent.setup.judgeMode")}
            value={draft.judge_mode}
            onChange={(e) =>
              update("judge_mode", e.target.value as AgentConfig["judge_mode"])
            }
          >
            <MenuItem value="async">{t("agent.setup.judgeAsync")}</MenuItem>
            <MenuItem value="sync">{t("agent.setup.judgeSync")}</MenuItem>
          </Select>
        </FormControl>
        <FormControl size="small" sx={{ flex: 1 }}>
          <InputLabel>{t("agent.blockVisibility")}</InputLabel>
          <Select
            label={t("agent.setup.blockVisibility")}
            value={draft.judge_block_visibility}
            onChange={(e) =>
              update(
                "judge_block_visibility",
                e.target.value as AgentConfig["judge_block_visibility"],
              )
            }
          >
            <MenuItem value="transparent">{t("agent.setup.blockTransparent")}</MenuItem>
            <MenuItem value="opaque">{t("agent.setup.blockOpaque")}</MenuItem>
          </Select>
        </FormControl>
      </Stack>

      <FormControl size="small">
        <InputLabel>{t("agent.judgeRubric")}</InputLabel>
        <Select
          label={t("agent.setup.judgeRubric")}
          value={draft.judge_rubric_id ?? ""}
          onChange={(e) =>
            update("judge_rubric_id", (e.target.value as string) || null)
          }
        >
          <MenuItem value="">{t("agent.setup.noRubric")}</MenuItem>
          {(rubricsQuery.data ?? []).map((r) => (
            <MenuItem key={r.id} value={r.id}>
              {r.name}
            </MenuItem>
          ))}
        </Select>
      </FormControl>
      <Box>
        <Button
          size="small"
          variant="outlined"
          onClick={() => setRubricsOpen(true)}
        >
          {t("agent.setup.manageRubrics")}
        </Button>
      </Box>

      <DialogShell
        open={rubricsOpen}
        title={t("agent.setup.judgeRubricsTooltip")}
        onClose={() => setRubricsOpen(false)}
      >
        <JudgeTab projectId={projectId} />
      </DialogShell>
    </Stack>
  );
}

// ---------------------------------------------------------------------------
// Advanced — webhook + retention + visibility toggles
// ---------------------------------------------------------------------------

function AdvancedPanel({
  projectId,
  draft,
  update,
}: {
  projectId: string;
  draft: AgentConfig;
  update: ChangeFn;
}) {
  const t = useT();
  const qc = useQueryClient();
  const [purgeDays, setPurgeDays] = useState(90);
  const [purgeConfirm, setPurgeConfirm] = useState("");

  const costQuery = useQuery<CostReport | null>({
    queryKey: ["agent-cost", projectId],
    queryFn: () => agentApi.getCost(projectId, 1),
    enabled: Boolean(projectId),
  });

  const statsQuery = useQuery({
    queryKey: ["agent-conv-stats", projectId],
    queryFn: () => agentApi.getConversationStats(projectId),
    enabled: Boolean(projectId),
  });

  const purgeMut = useMutation({
    mutationFn: (days: number) =>
      agentApi.purgeConversations(projectId, days),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["agent-conv-stats", projectId] });
      setPurgeConfirm("");
    },
  });

  const stats: ConversationStats | null = statsQuery.data ?? null;

  return (
    <Stack spacing={2}>
      <Typography variant="subtitle2">{t("agent.setup.userVisibility")}</Typography>
      <Typography variant="caption" color="text.secondary">
        {t("agent.userFacingVisibilityHelp")}
      </Typography>
      <FormGroup>
        <FormControlLabel
          control={
            <Switch
              checked={draft.show_thought_process}
              onChange={(e) => update("show_thought_process", e.target.checked)}
              size="small"
            />
          }
          label={<Typography variant="body2">{t("agent.setup.showThoughtProcess")}</Typography>}
        />
        <FormControlLabel
          control={
            <Switch
              checked={draft.show_semantic_query}
              onChange={(e) => update("show_semantic_query", e.target.checked)}
              size="small"
            />
          }
          label={<Typography variant="body2">{t("agent.setup.showSemanticQuery")}</Typography>}
        />
        <FormControlLabel
          control={
            <Switch
              checked={draft.show_physical_query}
              onChange={(e) => update("show_physical_query", e.target.checked)}
              size="small"
            />
          }
          label={<Typography variant="body2">{t("agent.setup.showPhysicalQuery")}</Typography>}
        />
        <FormControlLabel
          control={
            <Switch
              checked={draft.feedback_enabled}
              onChange={(e) => update("feedback_enabled", e.target.checked)}
              size="small"
            />
          }
          label={
            <Typography variant="body2">{t("agent.setup.allowFeedback")}</Typography>
          }
        />
      </FormGroup>

      <Divider />

      <Typography variant="subtitle2">{t("agent.setup.clientApp")}</Typography>
      <Typography variant="caption" color="text.secondary">
        {t("agent.clientApplicationHelp")}
      </Typography>
      <Box display="flex" alignItems="center" gap={1}>
        <TextField
          size="small"
          value={window.location.origin}
          InputProps={{ readOnly: true, sx: { fontFamily: "monospace", fontSize: 13 } }}
          sx={{ flex: 1 }}
        />
        <Button size="small" onClick={() => navigator.clipboard.writeText(window.location.origin)}>
          {t("agent.setup.copy")}
        </Button>
      </Box>

      <Divider />

      <Typography variant="subtitle2">{t("agent.setup.webhookHeading")}</Typography>
      <Typography variant="caption" color="text.secondary">
        {t("agent.webhookHelp")}
      </Typography>
      <TextField
        label={t("agent.setup.webhookUrl")}
        size="small"
        value={draft.webhook_url ?? ""}
        onChange={(e) => update("webhook_url", e.target.value || null)}
        helperText={t("agent.setup.webhookHelp")}
      />

      <Divider />

      <Typography variant="subtitle2">{t("agent.setup.sessionMemory")}</Typography>
      <Typography variant="caption" color="text.secondary">
        {t("agent.sessionMemoryHelp")}
      </Typography>
      <TextField
        label={t("agent.setup.sessionHistoryDepth")}
        size="small"
        type="number"
        value={draft.session_history_depth}
        onChange={(e) =>
          update(
            "session_history_depth",
            Math.max(1, Number(e.target.value) || 20),
          )
        }
        inputProps={{ min: 1, max: 100, step: 1 }}
        sx={{ maxWidth: 220 }}
      />

      <Divider />

      <Typography variant="subtitle2">{t("agent.setup.budgetLimits")}</Typography>
      <Typography variant="caption" color="text.secondary">
        {t("agent.budgetAndLimitsHelp")}
      </Typography>
      {costQuery.data && (
        <Typography variant="body2" color="text.secondary">
          {costQuery.data.estimated_usd > 0
            ? t("agent.todayTokensCost", {
                tokens: String((costQuery.data.total_input_tokens + costQuery.data.total_output_tokens).toLocaleString()),
                cost: costQuery.data.estimated_usd.toFixed(4),
              })
            : t("agent.todayTokens", {
                tokens: String((costQuery.data.total_input_tokens + costQuery.data.total_output_tokens).toLocaleString()),
              })}
        </Typography>
      )}
      <Stack direction="row" spacing={2}>
        <TextField
          label={t("agent.setup.dailyTokenBudget")}
          size="small"
          type="number"
          value={draft.daily_token_budget}
          onChange={(e) =>
            update("daily_token_budget", Math.max(0, Number(e.target.value) || 0))
          }
          inputProps={{ min: 0, step: 1000 }}
          helperText={t("agent.setup.unlimited")}
          sx={{ flex: 1 }}
        />
        <TextField
          label={t("agent.setup.dailyCostBudget")}
          size="small"
          type="number"
          value={draft.daily_cost_budget_usd}
          onChange={(e) =>
            update("daily_cost_budget_usd", Math.max(0, Number(e.target.value) || 0))
          }
          inputProps={{ min: 0, step: 0.1 }}
          helperText={t("agent.setup.unlimited")}
          sx={{ flex: 1 }}
        />
        <TextField
          label={t("agent.setup.maxQueryComplexity")}
          size="small"
          type="number"
          value={draft.max_query_complexity}
          onChange={(e) =>
            update("max_query_complexity", Math.max(0, Number(e.target.value) || 0))
          }
          inputProps={{ min: 0, step: 1 }}
          helperText={t("agent.setup.unlimited")}
          sx={{ flex: 1 }}
        />
      </Stack>

      <Divider />

      <Typography variant="subtitle2">{t("agent.setup.retentionHeading")}</Typography>
      <Typography variant="caption" color="text.secondary">
        {t("agent.retentionHelp")}
      </Typography>
      <TextField
        label={t("agent.setup.conversationRetention")}
        size="small"
        type="number"
        value={draft.conversation_retention_days}
        onChange={(e) =>
          update(
            "conversation_retention_days",
            Math.max(1, Number(e.target.value) || 30),
          )
        }
        inputProps={{ min: 1, step: 1 }}
        sx={{ maxWidth: 220 }}
      />

      <Divider />

      <Typography variant="subtitle2">{t("agent.setup.conversationHistory")}</Typography>
      {stats && (
        <Typography variant="body2" color="text.secondary">
          {t(stats.total === 1 ? "agent.conversationsDeletedSingular" : "agent.conversationsDeletedPlural", stats.total !== 1 ? { count: String(stats.total) } : {})}
          {stats.oldest_at
            ? ` (oldest: ${new Date(stats.oldest_at).toLocaleDateString()})`
            : ""}
        </Typography>
      )}
      <Stack direction="row" spacing={1} alignItems="flex-end">
        <TextField
          label={t("agent.setup.olderThanDays")}
          size="small"
          type="number"
          value={purgeDays}
          onChange={(e) => setPurgeDays(Math.max(1, Number(e.target.value) || 1))}
          inputProps={{ min: 1, step: 1 }}
          sx={{ maxWidth: 160 }}
        />
        <Button
          size="small"
          variant="outlined"
          disabled={purgeMut.isPending}
          onClick={() => purgeMut.mutate(purgeDays)}
        >
          {purgeMut.isPending ? <CircularProgress size={14} /> : t("agent.setup.purge")}
        </Button>
      </Stack>
      <Stack direction="row" spacing={1} alignItems="flex-end">
        <TextField
          label={t("agent.typeDeleteAll")}
          size="small"
          value={purgeConfirm}
          onChange={(e) => setPurgeConfirm(e.target.value)}
          sx={{ maxWidth: 240 }}
        />
        <Button
          size="small"
          variant="contained"
          disabled={purgeConfirm !== "DELETE ALL" || purgeMut.isPending}
          onClick={() => {
            purgeMut.mutate(0);
          }}
        >
          {purgeMut.isPending ? (
            <CircularProgress size={14} />
          ) : (
            t("agent.setup.deleteAllHistory")
          )}
        </Button>
      </Stack>
      {purgeMut.isSuccess && (
        <Alert severity="success">
          {t(purgeMut.data.deleted_conversations === 1 ? "agent.conversationsDeletedSingular" : "agent.conversationsDeletedPlural", purgeMut.data.deleted_conversations !== 1 ? { count: String(purgeMut.data.deleted_conversations) } : {})}
        </Alert>
      )}

      <Divider />
      <Typography variant="subtitle2">{t("agent.setup.outputCharts")}</Typography>
      <Typography variant="caption" color="text.secondary">
        {t("agent.outputAndChartsHelp")}
      </Typography>

      <FormControl size="small" fullWidth>
        <InputLabel>{t("agent.answerFormat")}</InputLabel>
        <Select
          value={draft.agent_output_format}
          label={t("agent.setup.answerFormat")}
          onChange={(e) => update("agent_output_format", e.target.value as AgentConfig["agent_output_format"])}
        >
          <MenuItem value="json">{t("agent.setup.answerFormatJson")}</MenuItem>
          <MenuItem value="plain">{t("agent.setup.answerFormatPlain")}</MenuItem>
          <MenuItem value="markup">{t("agent.setup.answerFormatMarkdown")}</MenuItem>
          <MenuItem value="html">{t("agent.setup.answerFormatHtml")}</MenuItem>
          <MenuItem value="rich_html">{t("agent.setup.answerFormatRichHtml")}</MenuItem>
        </Select>
      </FormControl>

      <FormControl size="small" fullWidth>
        <InputLabel>{t("agent.setup.chartSelector")}</InputLabel>
        <Select
          value={draft.chart_type_selector}
          label={t("agent.setup.chartSelector")}
          onChange={(e) => update("chart_type_selector", e.target.value as AgentConfig["chart_type_selector"])}
        >
          <MenuItem value="none">{t("agent.setup.chartNone")}</MenuItem>
          <MenuItem value="auto">{t("agent.setup.chartAuto")}</MenuItem>
          <MenuItem value="llm">{t("agent.setup.chartLlm")}</MenuItem>
        </Select>
      </FormControl>

      {draft.chart_type_selector !== "none" && (
        <>
          <FormControl size="small" fullWidth>
            <InputLabel>{t("agent.setup.chartRenderer")}</InputLabel>
            <Select
              value={draft.chart_renderer}
              label={t("agent.setup.chartRenderer")}
              onChange={(e) => update("chart_renderer", e.target.value as AgentConfig["chart_renderer"])}
            >
              <MenuItem value="echarts">{t("agent.setup.chartRendererEcharts")}</MenuItem>
              <MenuItem value="html">{t("agent.setup.chartRendererHtml")}</MenuItem>
            </Select>
          </FormControl>
          <TextField
            label={t("agent.setup.maxRowsForChart")}
            type="number"
            size="small"
            fullWidth
            value={draft.chart_max_rows}
            onChange={(e) => update("chart_max_rows", Number(e.target.value))}
            inputProps={{ min: 1 }}
          />
          <FormControl size="small" fullWidth>
            <InputLabel>{t("agent.setup.colorPalette")}</InputLabel>
            <Select
              value={draft.chart_color_palette}
              label={t("agent.setup.colorPalette")}
              onChange={(e) => update("chart_color_palette", e.target.value as AgentConfig["chart_color_palette"])}
            >
              <MenuItem value="default">{t("agent.setup.paletteTessallite")}</MenuItem>
              <MenuItem value="muted">{t("agent.setup.paletteMuted")}</MenuItem>
              <MenuItem value="high_contrast">{t("agent.setup.paletteHighContrast")}</MenuItem>
              <MenuItem value="colorblind_safe">{t("agent.setup.paletteColorblindSafe")}</MenuItem>
            </Select>
          </FormControl>
          <FormControl size="small" fullWidth>
            <InputLabel>{t("agent.setup.chartSize")}</InputLabel>
            <Select
              value={draft.chart_size}
              label={t("agent.setup.chartSize")}
              onChange={(e) => update("chart_size", e.target.value as AgentConfig["chart_size"])}
            >
              <MenuItem value="sm">{t("agent.setup.sizeSmall")}</MenuItem>
              <MenuItem value="md">{t("agent.setup.sizeMedium")}</MenuItem>
              <MenuItem value="lg">{t("agent.setup.sizeLarge")}</MenuItem>
            </Select>
          </FormControl>
          <FormControlLabel
            control={
              <Switch
                checked={draft.include_data_table}
                onChange={(e) => update("include_data_table", e.target.checked)}
                size="small"
              />
            }
            label={<Typography variant="body2">{t("agent.setup.includeDataTable")}</Typography>}
          />
        </>
      )}
      <TextField
        label={t("agent.setup.maxCompoundSteps")}
        type="number"
        size="small"
        value={draft.max_compound_steps}
        onChange={(e) => update("max_compound_steps", Math.max(2, Math.min(5, Number(e.target.value) || 3)))}
        inputProps={{ min: 2, max: 5 }}
        helperText={t("agent.setup.maxCompoundHelp")}
      />
    </Stack>
  );
}

// ---------------------------------------------------------------------------
// Reusable dialog shell for JudgeTab / RecipesTab
// ---------------------------------------------------------------------------

function DialogShell({
  open,
  title,
  onClose,
  children,
}: {
  open: boolean;
  title: string;
  onClose: () => void;
  children: React.ReactNode;
}) {
  return (
    <Dialog open={open} onClose={onClose} fullWidth maxWidth="md">
      <DialogTitle sx={{ display: "flex", alignItems: "center" }}>
        <Box sx={{ flex: 1 }}>{title}</Box>
        <IconButton size="small" onClick={onClose}>
          <CloseIcon fontSize="small" />
        </IconButton>
      </DialogTitle>
      <DialogContent dividers>{children}</DialogContent>
    </Dialog>
  );
}
