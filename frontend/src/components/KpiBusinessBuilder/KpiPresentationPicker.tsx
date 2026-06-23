import {
  FormControl,
  InputLabel,
  MenuItem,
  Select,
  Typography,
} from "@mui/material";
import { useT } from "../../i18n";
import type { PresentationType } from "../../api/types_domains/kpis";
import { PRESENTATION_TYPE_OPTIONS } from "../KpiWizard/types";

interface Props {
  value: PresentationType | "";
  onChange: (value: PresentationType | "") => void;
}

export function KpiPresentationPicker({ value, onChange }: Props) {
  const t = useT();

  return (
    <FormControl fullWidth size="small">
      <InputLabel>{t("kpiBusiness.visualType")}</InputLabel>
      <Select
        value={value}
        label={t("kpiBusiness.visualType")}
        onChange={(e) => onChange(e.target.value as PresentationType | "")}
      >
        <MenuItem value="">{t("kpis.none")}</MenuItem>
        {PRESENTATION_TYPE_OPTIONS.map((opt) => (
          <MenuItem key={opt.value} value={opt.value} sx={{ display: "block", py: 0.75 }}>
            <Typography variant="body2" sx={{ fontWeight: 600 }}>
              {t(opt.labelKey)}
            </Typography>
            <Typography variant="caption" color="text.secondary" sx={{ whiteSpace: "normal" }}>
              {t(opt.descKey)}
            </Typography>
          </MenuItem>
        ))}
      </Select>
    </FormControl>
  );
}
