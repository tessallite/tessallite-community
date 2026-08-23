/**
 * ModelLLMFunctions — model-level LLM tab.
 *
 * Per-model overrides for the two functions that run per model: the aggregate
 * creator and the glossary creator. Each dropdown defaults to "Inherit from
 * project (<name>)". The conversational agent and judge are project-scoped, so
 * they are set on the project LLM screen only.
 *
 * See docs/architecture/architecture_llm-function-config.md.
 */
import { useEffect, useState } from "react";
import { Alert, Box, Button, CircularProgress, Stack, Typography } from "@mui/material";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useT } from "../../i18n";
import { useAISchedulerConfig, useLLMConfigs } from "../../api/hooks";
import { aiSchedulerApi } from "../../api/client";
import { agentApi } from "../../api/agentApi";
import { extractApiError } from "../../utils/extractApiError";
import type { ModelAISchedulerConfigUpdate, LLMProviderConfig } from "../../api/types";
import LLMFunctionAssignments, { type LLMFunctionRow } from "./LLMFunctionAssignments";

/**
 * The scheduler-config endpoint answers two error shapes. Most failures carry
 * a plain string `detail`; the body foreign-key scope guards in model-service's
 * `_scope` family carry the structured `{error_code, field, ids, message}`
 * object that the whole family uses. Rendering that object straight into an
 * MUI `<Alert>` throws in React ("Objects are not valid as a React child"), so
 * the message is unwrapped here rather than assumed to be a string.
 */
export default function ModelLLMFunctions({
  projectId,
  modelId,
}: {
  projectId: string;
  modelId: string;
}) {
  const t = useT();
  const qc = useQueryClient();
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  const configQuery = useAISchedulerConfig(projectId, modelId);
  const providersQuery = useLLMConfigs(projectId);
  const projectCfgQuery = useQuery({
    queryKey: ["agent-config", projectId],
    queryFn: () => agentApi.getConfig(projectId),
    enabled: Boolean(projectId),
  });

  const [aggregate, setAggregate] = useState("");
  const [glossary, setGlossary] = useState("");
  useEffect(() => {
    if (configQuery.data) {
      setAggregate(configQuery.data.llm_config_id ?? "");
      setGlossary(configQuery.data.glossary_llm_config_id ?? "");
    }
  }, [configQuery.data]);

  const save = useMutation({
    mutationFn: (patch: ModelAISchedulerConfigUpdate) =>
      aiSchedulerApi.update(projectId, modelId, patch),
    onSuccess: (data) => {
      setError(null);
      setSaved(true);
      setTimeout(() => setSaved(false), 1500);
      qc.setQueryData(["aiSchedulerConfig", projectId, modelId], data);
    },
    // The hand-rolled `detail?: string` unwrap here only ever saw a plain-string
    // detail: it rendered nothing for FastAPI's list-shaped 422 and nothing for
    // the structured {error_code, field, message} body-FK detail. Use the shared
    // helper so every consumer reads the same three shapes.
    onError: (err: unknown) => {
      setError(extractApiError(err, t("llmFunctions.saveFailed")));
      if (configQuery.data) {
        setAggregate(configQuery.data.llm_config_id ?? "");
        setGlossary(configQuery.data.glossary_llm_config_id ?? "");
      }
    },
  });

  const providers: LLMProviderConfig[] = providersQuery.data ?? [];

  function providerName(id: string | null | undefined): string | null {
    if (!id) return null;
    return providers.find((p) => p.id === id)?.display_name ?? null;
  }

  // Resolve what "inherit from project" points at, per function, for the label.
  const pc = projectCfgQuery.data;
  const aggInheritName =
    providerName(pc?.aggregate_llm_config_id) ?? providerName(pc?.answer_llm_config_id);
  const gloInheritName =
    providerName(pc?.glossary_llm_config_id) ?? providerName(pc?.answer_llm_config_id);

  function inheritLabel(name: string | null): string {
    return name
      ? t("llmFunctions.inheritProjectNamed", { name })
      : t("llmFunctions.inheritProject");
  }

  function handleChange(key: string, value: string) {
    if (key === "aggregate") setAggregate(value);
    else setGlossary(value);
  }

  const dirty =
    (aggregate || null) !== (configQuery.data?.llm_config_id ?? null) ||
    (glossary || null) !== (configQuery.data?.glossary_llm_config_id ?? null);

  function handleSave() {
    save.mutate({
      llm_config_id: aggregate || null,
      glossary_llm_config_id: glossary || null,
    } as ModelAISchedulerConfigUpdate);
  }

  const rows: LLMFunctionRow[] = [
    {
      key: "aggregate",
      label: t("llmFunctions.aggregate"),
      help: t("llmFunctions.aggregateHelp"),
      value: aggregate,
      emptyLabel: inheritLabel(aggInheritName),
    },
    {
      key: "glossary",
      label: t("llmFunctions.glossary"),
      help: t("llmFunctions.glossaryHelp"),
      value: glossary,
      emptyLabel: inheritLabel(gloInheritName),
    },
  ];

  if (configQuery.isLoading) {
    return (
      <Box sx={{ py: 3, textAlign: "center" }}>
        <CircularProgress size={20} />
      </Box>
    );
  }

  return (
    <Box>
      <Typography variant="caption" color="text.secondary" sx={{ mb: 1.5, display: "block" }}>
        {t("llmFunctions.modelScopeNote")}
      </Typography>

      {providers.length === 0 ? (
        <Alert severity="info">{t("llmFunctions.noProvidersModel")}</Alert>
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
    </Box>
  );
}
