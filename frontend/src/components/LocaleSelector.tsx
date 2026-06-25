import { useState } from "react";
import { IconButton, Menu, MenuItem, Tooltip } from "@mui/material";
import LanguageIcon from "@mui/icons-material/Language";
import { useBuilderStore } from "../store/builderStore";
import { useT } from "../i18n";

const LOCALES = [
  { code: "en", labelKey: "i18n.en" },
  { code: "ar", labelKey: "i18n.ar" },
  { code: "fr", labelKey: "i18n.fr" },
  { code: "de", labelKey: "i18n.de" },
  { code: "es", labelKey: "i18n.es" },
  { code: "ja", labelKey: "i18n.ja" },
  { code: "pt", labelKey: "i18n.pt" },
  { code: "zh", labelKey: "i18n.zh" },
];

const STORAGE_KEY = "user_language_preference";

export default function LocaleSelector() {
  const t = useT();
  const displayLocale = useBuilderStore((s) => s.displayLocale);
  const setDisplayLocale = useBuilderStore((s) => s.setDisplayLocale);
  const [anchorEl, setAnchorEl] = useState<null | HTMLElement>(null);

  const currentCode = displayLocale ? displayLocale.split("-")[0] : "en";

  function handleSelect(code: string) {
    setDisplayLocale(code === "en" ? null : code);
    localStorage.setItem(STORAGE_KEY, code);
    setAnchorEl(null);
  }

  return (
    <>
      <Tooltip title={t("i18n.selectLanguage")}>
        <IconButton
          size="large"
          color="inherit"
          aria-label={t("i18n.selectLanguage")}
          onClick={(e) => setAnchorEl(e.currentTarget)}
        >
          <LanguageIcon />
        </IconButton>
      </Tooltip>
      <Menu
        anchorEl={anchorEl}
        open={Boolean(anchorEl)}
        onClose={() => setAnchorEl(null)}
        anchorOrigin={{ vertical: "bottom", horizontal: "right" }}
        transformOrigin={{ vertical: "top", horizontal: "right" }}
      >
        {LOCALES.map((l) => (
          <MenuItem
            key={l.code}
            selected={l.code === currentCode}
            onClick={() => handleSelect(l.code)}
          >
            {t(l.labelKey)}
          </MenuItem>
        ))}
      </Menu>
    </>
  );
}
