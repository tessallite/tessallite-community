import { useState } from "react";
import {
  IconButton,
  ListSubheader,
  Menu,
  MenuItem,
  Tooltip,
  Typography,
} from "@mui/material";
import LanguageIcon from "@mui/icons-material/Language";
import { useBuilderStore } from "../store/builderStore";
import { useT } from "../i18n";

/**
 * Bug-7542: only English is a complete, fully-tested locale. The seven
 * parked locales use English-fallback for many keys and have not completed
 * their translation pass. They are offered under a "Preview" section so
 * users know they are incomplete but can still try them.
 */

interface LocaleEntry {
  code: string;
  labelKey: string;
}

const SUPPORTED_LOCALES: LocaleEntry[] = [
  { code: "en", labelKey: "i18n.en" },
];

const PREVIEW_LOCALES: LocaleEntry[] = [
  { code: "ar", labelKey: "i18n.ar" },
  { code: "de", labelKey: "i18n.de" },
  { code: "es", labelKey: "i18n.es" },
  { code: "fr", labelKey: "i18n.fr" },
  { code: "ja", labelKey: "i18n.ja" },
  { code: "pt", labelKey: "i18n.pt" },
  { code: "zh", labelKey: "i18n.zh" },
];

export default function LocaleSelector() {
  const t = useT();
  const displayLocale = useBuilderStore((s) => s.displayLocale);
  const setDisplayLocale = useBuilderStore((s) => s.setDisplayLocale);
  const [anchorEl, setAnchorEl] = useState<null | HTMLElement>(null);

  const currentCode = displayLocale ? displayLocale.split("-")[0] : "en";

  function handleSelect(code: string) {
    // Use a single storage key (display_locale) via the builder store.
    // English is represented as null (base/default state).
    setDisplayLocale(code === "en" ? null : code);
    setAnchorEl(null);
  }

  function renderItem(l: LocaleEntry) {
    return (
      <MenuItem
        key={l.code}
        selected={l.code === currentCode}
        onClick={() => handleSelect(l.code)}
      >
        {t(l.labelKey)}
      </MenuItem>
    );
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
        {SUPPORTED_LOCALES.map(renderItem)}
        <ListSubheader sx={{ lineHeight: "32px", fontSize: 11 }}>
          <Typography variant="caption" color="text.secondary">
            {t("i18n.previewLocales")}
          </Typography>
        </ListSubheader>
        {PREVIEW_LOCALES.map(renderItem)}
      </Menu>
    </>
  );
}
