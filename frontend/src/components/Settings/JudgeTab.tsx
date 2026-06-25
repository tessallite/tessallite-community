import { useEffect, useMemo, useState } from "react";
import { useT } from "../../i18n";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  Box,
  Button,
  CircularProgress,
  Divider,
  IconButton,
  Stack,
  TextField,
  Typography,
} from "@mui/material";
import DeleteIcon from "@mui/icons-material/Delete";
import AddIcon from "@mui/icons-material/Add";
import { Rubric, RubricBody, RubricSection, agentApi } from "../../api/agentApi";

const EMPTY_BODY: RubricBody = { name: "", sections: [] };

export default function JudgeTab({ projectId }: { projectId: string }) {
  const t = useT();
  const qc = useQueryClient();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [draft, setDraft] = useState<RubricBody>(EMPTY_BODY);
  const [error, setError] = useState<string | null>(null);

  const rubricsQuery = useQuery({
    queryKey: ["agent-rubrics", projectId],
    queryFn: () => agentApi.listRubrics(projectId),
    enabled: Boolean(projectId),
  });

  const selected: Rubric | null = useMemo(() => {
    if (!selectedId) return null;
    return rubricsQuery.data?.find((r) => r.id === selectedId) ?? null;
  }, [selectedId, rubricsQuery.data]);

  useEffect(() => {
    if (selected) {
      setDraft({ name: selected.name, sections: selected.sections });
    } else {
      setDraft(EMPTY_BODY);
    }
    setError(null);
  }, [selected]);

  const createMut = useMutation({
    mutationFn: (body: RubricBody) => agentApi.createRubric(projectId, body),
    onSuccess: (data) => {
      qc.invalidateQueries({ queryKey: ["agent-rubrics", projectId] });
      setSelectedId(data.id);
    },
    onError: (err: { response?: { data?: { detail?: string } } }) =>
      setError(err.response?.data?.detail ?? t("judge.failedCreate")),
  });

  const updateMut = useMutation({
    mutationFn: ({ id, body }: { id: string; body: RubricBody }) =>
      agentApi.updateRubric(projectId, id, body),
    onSuccess: () =>
      qc.invalidateQueries({ queryKey: ["agent-rubrics", projectId] }),
    onError: (err: { response?: { data?: { detail?: string } } }) =>
      setError(err.response?.data?.detail ?? t("judge.failedUpdate")),
  });

  const deleteMut = useMutation({
    mutationFn: (id: string) => agentApi.deleteRubric(projectId, id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["agent-rubrics", projectId] });
      setSelectedId(null);
    },
  });

  const isNew = !selected;
  const canSave = draft.name.trim().length > 0;

  if (rubricsQuery.isLoading) {
    return (
      <Box sx={{ p: 4, textAlign: "center" }}>
        <CircularProgress size={24} />
      </Box>
    );
  }

  function save() {
    if (isNew) createMut.mutate(draft);
    else if (selected) updateMut.mutate({ id: selected.id, body: draft });
  }

  function updateSection(idx: number, next: RubricSection) {
    const sections = [...draft.sections];
    sections[idx] = next;
    setDraft({ ...draft, sections });
  }

  return (
    <Stack direction="row" spacing={2}>
      <Box sx={{ width: 220, flexShrink: 0 }}>
        <Stack
          direction="row"
          alignItems="center"
          justifyContent="space-between"
          sx={{ mb: 1 }}
        >
          <Typography variant="subtitle2">{t("judge.rubricsTitle")}</Typography>
          <Button
            size="small"
            startIcon={<AddIcon />}
            onClick={() => setSelectedId(null)}
          >
            {t("judge.newRubric")}
          </Button>
        </Stack>
        <Stack spacing={0.5}>
          {(rubricsQuery.data ?? []).map((r) => (
            <Button
              key={r.id}
              size="small"
              variant={r.id === selectedId ? "contained" : "text"}
              sx={{ justifyContent: "flex-start" }}
              onClick={() => setSelectedId(r.id)}
            >
              {r.name}
            </Button>
          ))}
          {(rubricsQuery.data ?? []).length === 0 && (
            <Typography variant="caption" color="text.secondary">
              {t("judge.noRubrics")}
            </Typography>
          )}
        </Stack>
      </Box>

      <Divider orientation="vertical" flexItem />

      <Box sx={{ flex: 1, minWidth: 0 }}>
        {error && (
          <Alert severity="error" sx={{ mb: 2 }}>
            {error}
          </Alert>
        )}
        <Alert severity="info" sx={{ mb: 2 }}>
          {t("judge.rubricInfo")}
        </Alert>

        <Stack spacing={2}>
          <TextField
            label={t("judge.rubricName")}
            size="small"
            value={draft.name}
            onChange={(e) => setDraft({ ...draft, name: e.target.value })}
          />

          <Typography variant="subtitle2">{t("judge.sections")}</Typography>
          {draft.sections.map((s, idx) => (
            <Box
              key={idx}
              sx={{
                border: 1,
                borderColor: "divider",
                p: 1.5,
                borderRadius: 1,
              }}
            >
              <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 1 }}>
                <TextField
                  label={t("judge.sectionTitle")}
                  size="small"
                  fullWidth
                  value={s.title}
                  onChange={(e) =>
                    updateSection(idx, { ...s, title: e.target.value })
                  }
                />
                <IconButton
                  size="small"
                  onClick={() =>
                    setDraft({
                      ...draft,
                      sections: draft.sections.filter((_, i) => i !== idx),
                    })
                  }
                >
                  <DeleteIcon fontSize="small" />
                </IconButton>
              </Stack>
              <TextField
                label={t("judge.sectionBody")}
                size="small"
                fullWidth
                multiline
                minRows={2}
                value={s.body ?? ""}
                onChange={(e) =>
                  updateSection(idx, { ...s, body: e.target.value })
                }
                sx={{ mb: 1 }}
              />
              <TextField
                label={t("judge.sectionBullets")}
                size="small"
                fullWidth
                multiline
                minRows={3}
                value={(s.bullets ?? []).join("\n")}
                onChange={(e) =>
                  updateSection(idx, {
                    ...s,
                    bullets: e.target.value
                      .split("\n")
                      .map((b) => b.trim())
                      .filter(Boolean),
                  })
                }
              />
            </Box>
          ))}
          <Button
            size="small"
            startIcon={<AddIcon />}
            onClick={() =>
              setDraft({
                ...draft,
                sections: [
                  ...draft.sections,
                  { title: "", body: "", bullets: [] },
                ],
              })
            }
          >
            {t("judge.addSection")}
          </Button>

          <Stack direction="row" spacing={1} justifyContent="flex-end">
            {!isNew && selected && (
              <Button
                size="small"
                variant="outlined"
                onClick={() => {
                  if (confirm(`${t("judge.delete")} "${selected.name}"?`)) {
                    deleteMut.mutate(selected.id);
                  }
                }}
              >
                {t("judge.delete")}
              </Button>
            )}
            <Button
              size="small"
              variant="contained"
              disabled={!canSave || createMut.isPending || updateMut.isPending}
              onClick={save}
            >
              {isNew ? t("judge.createRubric") : t("judge.saveChanges")}
            </Button>
          </Stack>
        </Stack>
      </Box>
    </Stack>
  );
}
