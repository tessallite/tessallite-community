/**
 * LLMFunctionAssignments — presentation-only list of "function → LLM" dropdowns.
 *
 * Used by both the project LLM screen and the model config LLM tab. The parent
 * owns the data (current values, provider list, persistence); this component
 * only renders one labelled dropdown per row and reports changes.
 *
 * See docs/architecture/architecture_llm-function-config.md.
 */
import {
  Box,
  FormControl,
  InputLabel,
  MenuItem,
  Select,
  Stack,
  Typography,
} from "@mui/material";
import type { LLMProviderConfig } from "../../api/types";

export interface LLMFunctionRow {
  /** Stable key, e.g. "aggregate" | "agent" | "judge" | "glossary". */
  key: string;
  /** Translated function name shown as the field label. */
  label: string;
  /** Optional translated helper line under the label. */
  help?: string;
  /** Currently selected provider config id; "" selects the empty option. */
  value: string;
  /** Translated text for the empty option (e.g. "None" or "Inherit from project"). */
  emptyLabel: string;
}

interface Props {
  rows: LLMFunctionRow[];
  providers: LLMProviderConfig[];
  disabled?: boolean;
  onChange: (key: string, value: string) => void;
}

export default function LLMFunctionAssignments({
  rows,
  providers,
  disabled = false,
  onChange,
}: Props) {
  return (
    <Stack spacing={2}>
      {rows.map((row) => (
        <Box key={row.key}>
          <FormControl size="small" fullWidth disabled={disabled}>
            <InputLabel>{row.label}</InputLabel>
            <Select
              label={row.label}
              value={row.value}
              onChange={(e) => onChange(row.key, e.target.value)}
            >
              <MenuItem value="">{row.emptyLabel}</MenuItem>
              {providers.map((p) => (
                <MenuItem key={p.id} value={p.id}>
                  {p.display_name} — {p.provider} / {p.model_name}
                </MenuItem>
              ))}
            </Select>
          </FormControl>
          {row.help && (
            <Typography
              variant="caption"
              color="text.secondary"
              sx={{ mt: 0.5, display: "block" }}
            >
              {row.help}
            </Typography>
          )}
        </Box>
      ))}
    </Stack>
  );
}
