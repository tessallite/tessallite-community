import { useEffect, useMemo, useState } from "react";
import { useT } from "../../i18n";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Alert,
  Box,
  Button,
  Checkbox,
  CircularProgress,
  Divider,
  FormControl,
  FormControlLabel,
  IconButton,
  InputLabel,
  MenuItem,
  Select,
  Stack,
  TextField,
  Typography,
} from "@mui/material";
import DeleteIcon from "@mui/icons-material/Delete";
import AddIcon from "@mui/icons-material/Add";
import {
  Recipe,
  RecipeBody,
  RecipeParameter,
  RecipeStep,
  agentApi,
} from "../../api/agentApi";
import CombineExpressionBuilder from "./CombineExpressionBuilder";

const EMPTY_BODY: RecipeBody = {
  name: "",
  description: null,
  parameters: [],
  steps: [],
  combine: null,
  notes: null,
};

export default function RecipesTab({
  projectId,
  publishedModels,
  allowList,
}: {
  projectId: string;
  publishedModels: { id: string; display_name: string; slug: string }[];
  allowList: string[];
}) {
  const t = useT();
  const qc = useQueryClient();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [draft, setDraft] = useState<RecipeBody>(EMPTY_BODY);
  const [error, setError] = useState<string | null>(null);

  const recipesQuery = useQuery({
    queryKey: ["agent-recipes", projectId],
    queryFn: () => agentApi.listRecipes(projectId),
    enabled: Boolean(projectId),
  });

  const allowListedModels = useMemo(
    () => publishedModels.filter((m) => allowList.includes(m.id)),
    [publishedModels, allowList],
  );

  const selected: Recipe | null = useMemo(() => {
    if (!selectedId) return null;
    return recipesQuery.data?.find((r) => r.id === selectedId) ?? null;
  }, [selectedId, recipesQuery.data]);

  useEffect(() => {
    if (selected) {
      setDraft({
        name: selected.name,
        description: selected.description,
        parameters: selected.parameters,
        steps: selected.steps,
        combine: selected.combine,
        notes: selected.notes,
      });
    } else {
      setDraft(EMPTY_BODY);
    }
    setError(null);
  }, [selected]);

  const createMut = useMutation({
    mutationFn: (body: RecipeBody) => agentApi.createRecipe(projectId, body),
    onSuccess: (data) => {
      qc.invalidateQueries({ queryKey: ["agent-recipes", projectId] });
      setSelectedId(data.id);
      setError(null);
    },
    onError: (err: { response?: { data?: { detail?: string } } }) => {
      setError(err.response?.data?.detail ?? t("recipes.failedCreate"));
    },
  });

  const updateMut = useMutation({
    mutationFn: ({ id, body }: { id: string; body: RecipeBody }) =>
      agentApi.updateRecipe(projectId, id, body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["agent-recipes", projectId] });
      setError(null);
    },
    onError: (err: { response?: { data?: { detail?: string } } }) => {
      setError(err.response?.data?.detail ?? t("recipes.failedUpdate"));
    },
  });

  const deleteMut = useMutation({
    mutationFn: (id: string) => agentApi.deleteRecipe(projectId, id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["agent-recipes", projectId] });
      setSelectedId(null);
    },
  });

  const isNew = !selected;
  const canSave = draft.name.trim().length > 0 && draft.steps.length > 0;

  function save() {
    if (isNew) createMut.mutate(draft);
    else if (selected) updateMut.mutate({ id: selected.id, body: draft });
  }

  if (recipesQuery.isLoading) {
    return (
      <Box sx={{ p: 4, textAlign: "center" }}>
        <CircularProgress size={24} />
      </Box>
    );
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
          <Typography variant="subtitle2">{t("recipes.title")}</Typography>
          <Button
            size="small"
            startIcon={<AddIcon />}
            onClick={() => setSelectedId(null)}
          >
            {t("recipes.newRecipe")}
          </Button>
        </Stack>
        <Stack spacing={0.5}>
          {(recipesQuery.data ?? []).map((r) => (
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
          {(recipesQuery.data ?? []).length === 0 && (
            <Typography variant="caption" color="text.secondary">
              {t("recipes.noRecipes")}
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
          {t("recipes.recipesInfo")}
        </Alert>

        <Stack spacing={2}>
          <TextField
            label={t("recipes.nameLabel")}
            size="small"
            value={draft.name}
            onChange={(e) => setDraft({ ...draft, name: e.target.value })}
          />
          <TextField
            label={t("recipes.descriptionLabel")}
            size="small"
            multiline
            minRows={2}
            value={draft.description ?? ""}
            onChange={(e) =>
              setDraft({ ...draft, description: e.target.value || null })
            }
          />

          <Typography variant="subtitle2">{t("recipes.parametersTitle")}</Typography>
          {draft.parameters.map((p, idx) => (
            <ParameterRow
              key={idx}
              value={p}
              onChange={(next) => {
                const params = [...draft.parameters];
                params[idx] = next;
                setDraft({ ...draft, parameters: params });
              }}
              onRemove={() =>
                setDraft({
                  ...draft,
                  parameters: draft.parameters.filter((_, i) => i !== idx),
                })
              }
            />
          ))}
          <Button
            size="small"
            startIcon={<AddIcon />}
            onClick={() =>
              setDraft({
                ...draft,
                parameters: [
                  ...draft.parameters,
                  {
                    name: "",
                    description: null,
                    resolves_to_glossary_entity: false,
                  },
                ],
              })
            }
          >
            {t("recipes.addParameter")}
          </Button>

          <Typography variant="subtitle2">{t("recipes.stepsTitle")}</Typography>
          {draft.steps.map((s, idx) => (
            <StepRow
              key={idx}
              value={s}
              models={allowListedModels}
              onChange={(next) => {
                const steps = [...draft.steps];
                steps[idx] = next;
                setDraft({ ...draft, steps });
              }}
              onRemove={() =>
                setDraft({
                  ...draft,
                  steps: draft.steps.filter((_, i) => i !== idx),
                })
              }
            />
          ))}
          <Button
            size="small"
            startIcon={<AddIcon />}
            onClick={() =>
              setDraft({
                ...draft,
                steps: [
                  ...draft.steps,
                  {
                    name: "",
                    model_id: allowListedModels[0]?.id ?? "",
                    measures: [],
                    dimensions: [],
                    filters: [],
                    limit: 100,
                  },
                ],
              })
            }
          >
            {t("recipes.addStep")}
          </Button>

          <CombineExpressionBuilder
            value={draft.combine}
            steps={draft.steps}
            onChange={(combine) => setDraft({ ...draft, combine })}
          />
          <TextField
            label={t("recipes.notesLabel")}
            size="small"
            multiline
            minRows={2}
            value={draft.notes ?? ""}
            onChange={(e) =>
              setDraft({ ...draft, notes: e.target.value || null })
            }
          />

          <Stack direction="row" spacing={1} justifyContent="flex-end">
            {!isNew && selected && (
              <Button
                size="small"
                variant="outlined"
                onClick={() => {
                  if (confirm(`${t("recipes.delete")} "${selected.name}"?`)) {
                    deleteMut.mutate(selected.id);
                  }
                }}
              >
                {t("recipes.delete")}
              </Button>
            )}
            <Button
              size="small"
              variant="contained"
              disabled={!canSave || createMut.isPending || updateMut.isPending}
              onClick={save}
            >
              {isNew ? t("recipes.createRecipe") : t("recipes.saveChanges")}
            </Button>
          </Stack>
        </Stack>
      </Box>
    </Stack>
  );
}

function ParameterRow({
  value,
  onChange,
  onRemove,
}: {
  value: RecipeParameter;
  onChange: (next: RecipeParameter) => void;
  onRemove: () => void;
}) {
  const t = useT();
  return (
    <Stack direction="row" spacing={1} alignItems="center">
      <TextField
        label={t("recipes.nameLabel")}
        size="small"
        value={value.name}
        onChange={(e) => onChange({ ...value, name: e.target.value })}
        sx={{ width: 160 }}
      />
      <TextField
        label={t("recipes.descriptionLabel")}
        size="small"
        value={value.description ?? ""}
        onChange={(e) =>
          onChange({ ...value, description: e.target.value || null })
        }
        sx={{ flex: 1 }}
      />
      <FormControlLabel
        control={
          <Checkbox
            checked={value.resolves_to_glossary_entity}
            onChange={(e) =>
              onChange({
                ...value,
                resolves_to_glossary_entity: e.target.checked,
              })
            }
          />
        }
        label={<Typography variant="caption">{t("recipes.glossary")}</Typography>}
      />
      <IconButton size="small" onClick={onRemove}>
        <DeleteIcon fontSize="small" />
      </IconButton>
    </Stack>
  );
}

function StepRow({
  value,
  models,
  onChange,
  onRemove,
}: {
  value: RecipeStep;
  models: { id: string; display_name: string; slug: string }[];
  onChange: (next: RecipeStep) => void;
  onRemove: () => void;
}) {
  const t = useT();
  return (
    <Box sx={{ border: 1, borderColor: "divider", p: 1.5, borderRadius: 1 }}>
      <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 1 }}>
        <TextField
          label={t("recipes.stepName")}
          size="small"
          value={value.name}
          onChange={(e) => onChange({ ...value, name: e.target.value })}
          sx={{ width: 160 }}
        />
        <FormControl size="small" sx={{ flex: 1 }}>
          <InputLabel>{t("recipes.model")}</InputLabel>
          <Select
            label={t("recipes.model")}
            value={value.model_id}
            onChange={(e) =>
              onChange({ ...value, model_id: String(e.target.value) })
            }
          >
            {models.map((m) => (
              <MenuItem key={m.id} value={m.id}>
                {m.display_name} ({m.slug})
              </MenuItem>
            ))}
          </Select>
        </FormControl>
        <TextField
          label={t("recipes.limit")}
          size="small"
          type="number"
          value={value.limit}
          onChange={(e) =>
            onChange({ ...value, limit: Number(e.target.value) || 100 })
          }
          sx={{ width: 100 }}
        />
        <IconButton size="small" onClick={onRemove}>
          <DeleteIcon fontSize="small" />
        </IconButton>
      </Stack>
      <TextField
        label={t("recipes.measures")}
        size="small"
        fullWidth
        value={value.measures.join(", ")}
        onChange={(e) =>
          onChange({
            ...value,
            measures: e.target.value
              .split(",")
              .map((s) => s.trim())
              .filter(Boolean),
          })
        }
        sx={{ mb: 1 }}
      />
      <TextField
        label={t("recipes.dimensions")}
        size="small"
        fullWidth
        value={value.dimensions.join(", ")}
        onChange={(e) =>
          onChange({
            ...value,
            dimensions: e.target.value
              .split(",")
              .map((s) => s.trim())
              .filter(Boolean),
          })
        }
        sx={{ mb: 1 }}
      />
      <TextField
        label={t("recipes.filtersJsonArray")}
        size="small"
        fullWidth
        multiline
        minRows={2}
        value={JSON.stringify(value.filters)}
        onChange={(e) => {
          try {
            const parsed = JSON.parse(e.target.value);
            if (Array.isArray(parsed)) {
              onChange({ ...value, filters: parsed });
            }
          } catch {
            /* ignore until valid */
          }
        }}
        helperText={t("recipes.filtersHelperText")}
      />
    </Box>
  );
}
