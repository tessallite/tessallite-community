import { useEffect, useState } from "react";
import { useT } from "../../i18n";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  Box,
  Button,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  IconButton,
  Stack,
  TextField,
  Typography,
} from "@mui/material";
import DeleteIcon from "@mui/icons-material/DeleteOutline";
import AddIcon from "@mui/icons-material/Add";
import { agentApi, AgentModelContext } from "../../api/agentApi";

const EMPTY_CONTEXT: AgentModelContext = {
  model_overview: null,
  analytical_capabilities: null,
  abbreviation_conflict_rules: null,
  example_questions: [],
};

type Example = { q: string; decomposition: string };

export default function ModelContextDialog({
  open,
  onClose,
  projectId,
  modelId,
  modelName,
}: {
  open: boolean;
  onClose: () => void;
  projectId: string;
  modelId: string;
  modelName: string;
}) {
  const t = useT();
  const qc = useQueryClient();
  const [draft, setDraft] = useState<AgentModelContext>(EMPTY_CONTEXT);
  const [examples, setExamples] = useState<Example[]>([]);
  const [error, setError] = useState<string | null>(null);

  const ctxQuery = useQuery({
    queryKey: ["agent-model-context", projectId, modelId],
    queryFn: () => agentApi.getModelContext(projectId, modelId),
    enabled: open && Boolean(projectId && modelId),
  });

  useEffect(() => {
    if (!open) return;
    const data = ctxQuery.data;
    if (data) {
      setDraft({ ...EMPTY_CONTEXT, ...data });
      setExamples(
        Array.isArray(data.example_questions)
          ? (data.example_questions as Example[])
          : [],
      );
    } else if (data === null) {
      setDraft(EMPTY_CONTEXT);
      setExamples([]);
    }
  }, [ctxQuery.data, open]);

  const upsert = useMutation({
    mutationFn: (body: AgentModelContext) =>
      agentApi.upsertModelContext(projectId, modelId, body),
    onSuccess: () => {
      qc.invalidateQueries({
        queryKey: ["agent-model-context", projectId, modelId],
      });
      setError(null);
      onClose();
    },
    onError: (err: { response?: { data?: { detail?: string } } }) => {
      setError(err.response?.data?.detail ?? t("modelContext.saveFailed"));
    },
  });

  function handleSave() {
    upsert.mutate({
      ...draft,
      example_questions: examples.filter(
        (e) => e.q.trim() || e.decomposition.trim(),
      ),
    });
  }

  function update<K extends keyof AgentModelContext>(
    key: K,
    value: AgentModelContext[K],
  ) {
    setDraft((d) => ({ ...d, [key]: value }));
  }

  return (
    <Dialog open={open} onClose={onClose} maxWidth="md" fullWidth>
      <DialogTitle>{t("modelContext.title", { modelName })}</DialogTitle>
      <DialogContent dividers>
        {ctxQuery.isLoading ? (
          <Box sx={{ p: 4, textAlign: "center" }}>
            <CircularProgress size={24} />
          </Box>
        ) : (
          <Stack spacing={2}>
            {error && <Alert severity="error">{error}</Alert>}

            <Alert severity="info">
              {t("modelContext.perModelInfo")}
            </Alert>

            <TextField
              label={t("modelContext.modelOverview")}
              multiline
              minRows={4}
              value={draft.model_overview ?? ""}
              onChange={(e) =>
                update("model_overview", e.target.value || null)
              }
              helperText={t("modelContext.modelOverviewHelp")}
            />
            <TextField
              label={t("modelContext.analyticalCapabilities")}
              multiline
              minRows={3}
              value={draft.analytical_capabilities ?? ""}
              onChange={(e) =>
                update("analytical_capabilities", e.target.value || null)
              }
              helperText={t("modelContext.analyticalCapabilitiesHelp")}
            />
            <TextField
              label={t("modelContext.abbreviationConflictRules")}
              multiline
              minRows={3}
              value={draft.abbreviation_conflict_rules ?? ""}
              onChange={(e) =>
                update("abbreviation_conflict_rules", e.target.value || null)
              }
              helperText={t("modelContext.abbreviationConflictRulesHelp")}
            />

            <Box>
              <Stack
                direction="row"
                alignItems="center"
                justifyContent="space-between"
                sx={{ mb: 1 }}
              >
                <Typography variant="subtitle2">{t("modelContext.exampleQuestions")}</Typography>
                <Button
                  size="small"
                  startIcon={<AddIcon />}
                  onClick={() =>
                    setExamples((e) => [...e, { q: "", decomposition: "" }])
                  }
                >
                  {t("common.add")}
                </Button>
              </Stack>
              <Stack spacing={1.5}>
                {examples.map((ex, idx) => (
                  <Box
                    key={idx}
                    sx={{
                      display: "grid",
                      gridTemplateColumns: "1fr 1fr auto",
                      gap: 1,
                      alignItems: "flex-start",
                    }}
                  >
                    <TextField
                      size="small"
                      label={t("modelContext.questionLabel")}
                      multiline
                      minRows={2}
                      value={ex.q}
                      onChange={(e) =>
                        setExamples((cur) =>
                          cur.map((c, i) =>
                            i === idx ? { ...c, q: e.target.value } : c,
                          ),
                        )
                      }
                    />
                    <TextField
                      size="small"
                      label={t("modelContext.decompositionLabel")}
                      multiline
                      minRows={2}
                      value={ex.decomposition}
                      onChange={(e) =>
                        setExamples((cur) =>
                          cur.map((c, i) =>
                            i === idx
                              ? { ...c, decomposition: e.target.value }
                              : c,
                          ),
                        )
                      }
                    />
                    <IconButton
                      size="small"
                      onClick={() =>
                        setExamples((cur) => cur.filter((_, i) => i !== idx))
                      }
                    >
                      <DeleteIcon fontSize="small" />
                    </IconButton>
                  </Box>
                ))}
                {examples.length === 0 && (
                  <Typography color="text.secondary" variant="body2">
                    {t("modelContext.noExampleQuestions")}
                  </Typography>
                )}
              </Stack>
            </Box>
          </Stack>
        )}
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>{t("modelContext.cancel")}</Button>
        <Button
          variant="contained"
          disabled={upsert.isPending}
          onClick={handleSave}
        >
          {upsert.isPending ? <CircularProgress size={14} /> : t("modelContext.save")}
        </Button>
      </DialogActions>
    </Dialog>
  );
}
