import { FormControl, InputLabel, MenuItem, Select } from "@mui/material";
import { usePersonas } from "../../api/hooks";
import { useT } from "../../i18n";

interface Props {
  projectId: string;
  modelId: string;
  value: string | null;
  onChange: (personaId: string | null) => void;
  size?: "small" | "medium";
  label?: string;
  /**
   * When true (default), only personas whose audience_roles match the
   * caller's role are listed.  Set to false on admin surfaces (e.g. the
   * canvas preview overlay) where all personas should be selectable.
   */
  forAudience?: boolean;
}

const NONE = "__none__";

export default function PersonaPicker({
  projectId,
  modelId,
  value,
  onChange,
  size = "small",
  label,
  forAudience = true,
}: Props) {
  const t = useT();
  const resolvedLabel = label ?? t("persona.defaultLabel");

  const personas = usePersonas(projectId, modelId, {
    forAudience,
  });
  const list = personas.data ?? [];

  if (!personas.isLoading && list.length === 0) {
    return null;
  }

  return (
    <FormControl size={size} sx={{ minWidth: 180 }}>
      <InputLabel>{resolvedLabel}</InputLabel>
      <Select
        label={resolvedLabel}
        value={value ?? NONE}
        onChange={(e) => {
          const v = e.target.value as string;
          onChange(v === NONE ? null : v);
        }}
        renderValue={(v) => {
          if (v === NONE) return t("persona.noneFullModel");
          const match = list.find((p) => p.id === v);
          return match?.name ?? v;
        }}
      >
        <MenuItem value={NONE}>
          <em>{t("persona.noneFullModel")}</em>
        </MenuItem>
        {list.map((p) => (
          <MenuItem key={p.id} value={p.id}>
            {p.name}
          </MenuItem>
        ))}
      </Select>
    </FormControl>
  );
}
