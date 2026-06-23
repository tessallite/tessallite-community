/**
 * ProjectLLMScreen — single project-level "LLM" screen.
 *
 * Top: one dropdown per AI function (aggregate creator, conversational agent,
 * LLM judge, glossary creator), writing to ProjectAgentConfig.
 * Bottom: the existing provider pool CRUD (LLMConfigurationsPanel).
 *
 * Consolidates the LLM pickers that used to live on the agent Setup/Guardrails
 * tabs and the model Scheduler panel.
 * See docs/architecture/architecture_llm-function-config.md.
 */
import { useEffect, useState } from "react";
import {
  Alert,
  Box,
  Button,
  CircularProgress,
  Divider,
  Stack,
  Typography,
} from "@mui/material";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useT } from "../../i18n";
import { agentApi, DEFAULT_AGENT_CONFIG, type AgentConfig } from "../../api/agentApi";
import { llmConfigsApi } from "../../api/client";
import LLMConfigurationsPanel from "./LLMConfigurationsPanel";
import LLMFunctionAssignments, { type LLMFunctionRow } from "./LLMFunctionAssignments";

const FIELD: Record<string, keyof AgentConfig> = {
  agent: "answer_llm_config_id",
  judge: "judge_llm_config_id",
  aggregate: "aggregate_llm_config_id",
  glossary: "glossary_llm_config_id",
};

export default function ProjectLLMScreen({ projectId }: { projectId: string }) {
  const t = useT();
  const qc = useQueryClient();
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  const configQuery = useQuery({
    queryKey: ["agent-config", projectId],
    queryFn: () => agentApi.getConfig(projectId),
    enabled: Boolean(projectId),
  });
  const providersQuery = useQuery({
    queryKey: ["llmConfigs", projectId],
    queryFn: () => llmConfigsApi.list(projectId),
    enabled: Boolean(projectId),
  });

  // Local view of the four ids so dropdowns react instantly while the PATCH runs.
  const [draft, setDraft] = useState<AgentConfig>(DEFAULT_AGENT_CONFIG);
  useEffect(() => {
    if (configQuery.data) setDraft({ ...DEFAULT_AGENT_CONFIG, ...configQuery.data });
    else if (configQuery.data === null) setDraft(DEFAULT_AGENT_CONFIG);
  }, [configQuery.data]);

  const save = useMutation({
    mutationFn: (patch: Partial<AgentConfig>) =>
      configQuery.data
        ? agentApi.patchConfig(projectId, patch)
        : agentApi.upsertConfig(projectId, { ...DEFAULT_AGENT_CONFIG, ...draft, ...patch }),
    onSuccess: (data) => {
      setError(null);
      setSaved(true);
      setTimeout(() => setSaved(false), 1500);
      qc.setQueryData(["agent-config", projectId], data);
      setDraft({ ...DEFAULT_AGENT_CONFIG, ...data });
    },
    onError: (err: { response?: { data?: { detail?: string } } }) => {
      setError(err.response?.data?.detail ?? t("llmFunctions.saveFailed"));
      // Roll the dropdown back to the last persisted value.
      if (configQuery.data) setDraft({ ...DEFAULT_AGENT_CONFIG, ...configQuery.data });
    },
  });

  function handleChange(key: string, value: string) {
    const field = FIELD[key];
    setDraft((d) => ({ ...d, [field]: value || null }));
  }

  const persisted = configQuery.data;
  const dirty =
    (draft.answer_llm_config_id ?? null) !== (persisted?.answer_llm_config_id ?? null) ||
    (draft.judge_llm_config_id ?? null) !== (persisted?.judge_llm_config_id ?? null) ||
    (draft.aggregate_llm_config_id ?? null) !== (persisted?.aggregate_llm_config_id ?? null) ||
    (draft.glossary_llm_config_id ?? null) !== (persisted?.glossary_llm_config_id ?? null);

  function handleSave() {
    save.mutate({
      answer_llm_config_id: draft.answer_llm_config_id ?? null,
      judge_llm_config_id: draft.judge_llm_config_id ?? null,
      aggregate_llm_config_id: draft.aggregate_llm_config_id ?? null,
      glossary_llm_config_id: draft.glossary_llm_config_id ?? null,
    });
  }

  const providers = providersQuery.data ?? [];

  const rows: LLMFunctionRow[] = [
    {
      key: "aggregate",
      label: t("llmFunctions.aggregate"),
      help: t("llmFunctions.aggregateHelp"),
      value: draft.aggregate_llm_config_id ?? "",
      emptyLabel: t("llmFunctions.useAgentDefault"),
    },
    {
      key: "agent",
      label: t("llmFunctions.agent"),
      help: t("llmFunctions.agentHelp"),
      value: draft.answer_llm_config_id ?? "",
      emptyLabel: t("llmFunctions.none"),
    },
    {
      key: "judge",
      label: t("llmFunctions.judge"),
      help: t("llmFunctions.judgeHelp"),
      value: draft.judge_llm_config_id ?? "",
      emptyLabel: t("llmFunctions.sameAsAgent"),
    },
    {
      key: "glossary",
      label: t("llmFunctions.glossary"),
      help: t("llmFunctions.glossaryHelp"),
      value: draft.glossary_llm_config_id ?? "",
      emptyLabel: t("llmFunctions.useAgentDefault"),
    },
  ];

  return (
    <Box>
      <Typography variant="subtitle2" sx={{ fontWeight: 600, mb: 0.5 }}>
        {t("llmFunctions.assignmentsHeading")}
      </Typography>
      <Typography variant="caption" color="text.secondary" sx={{ mb: 1.5, display: "block" }}>
        {t("llmFunctions.assignmentsHelp")}
      </Typography>

      {configQuery.isLoading ? (
        <Box sx={{ py: 3, textAlign: "center" }}>
          <CircularProgress size={20} />
        </Box>
      ) : providers.length === 0 ? (
        <Alert severity="info" sx={{ mb: 2 }}>{t("llmFunctions.noProviders")}</Alert>
      ) : (
        <>
          <LLMFunctionAssignments
            rows={rows}
            providers={providers}
            disabled={save.isPending}
            onChange={handleChange}
          />
          <Stack direction="row" spacing={1} sx={{ mt: 1.5 }}>
            <Button
              variant="contained"
              size="small"
              onClick={handleSave}
              disabled={!dirty || save.isPending}
            >
              {save.isPending ? <CircularProgress size={16} /> : t("common.save")}
            </Button>
          </Stack>
        </>
      )}

      {error && <Alert severity="error" sx={{ mt: 1.5 }}>{error}</Alert>}
      {saved && <Alert severity="success" sx={{ mt: 1.5 }}>{t("llmFunctions.saved")}</Alert>}

      <Divider sx={{ my: 2.5 }} />

      <Typography variant="subtitle2" sx={{ fontWeight: 600, mb: 1 }}>
        {t("llmFunctions.providersHeading")}
      </Typography>
      <LLMConfigurationsPanel projectId={projectId} />
    </Box>
  );
}
