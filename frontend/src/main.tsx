// Local fonts — bundled at build time, no CDN required (airgapped compatible)
import "@fontsource/inter/400.css";
import "@fontsource/inter/500.css";
import "@fontsource/inter/600.css";
import "@fontsource/inter/700.css";
import "@fontsource/jetbrains-mono/400.css";
import "@fontsource/jetbrains-mono/500.css";
import "@fontsource/jetbrains-mono/600.css";
import "@fontsource/jetbrains-mono/700.css";

import React, { useEffect, useMemo, useState } from "react";
import ReactDOM from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { CssBaseline, ThemeProvider, createTheme } from "@mui/material";
import type { PaletteMode } from "@mui/material";
import { palette, font } from "./theme/tokens";
import { registerTessalliteEChartsTheme } from "./theme/echartsTheme";
import App from "./App";
import StaleBundleGuard from "./components/StaleBundleGuard";
import { safeLocalGet } from "./utils/safeLocalStorage";
import { brandingApi, type BrandingConfig } from "./api/client";

// Register Tessallite-branded ECharts theme once at startup
registerTessalliteEChartsTheme();

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { retry: 1, staleTime: 30_000 },
  },
});

const KEY_THEME = "builder.settings.theme";
const SETTINGS_CHANGED_EVENT = "builder-settings-changed";

type ThemePreference = "system" | "light" | "dark";

function readThemePreference(): ThemePreference {
  if (typeof window === "undefined") {
    return "light";
  }
  try {
    const value = localStorage.getItem(KEY_THEME);
    if (value === "light" || value === "dark") {
      return value;
    }
    if (value === "system") {
      localStorage.removeItem(KEY_THEME);
    }
  } catch {
    console.warn('Could not read localStorage key "builder.settings.theme", using default.');
  }
  return "light";
}

function systemPrefersDark(): boolean {
  if (typeof window === "undefined" || !window.matchMedia) {
    return false;
  }
  return window.matchMedia("(prefers-color-scheme: dark)").matches;
}

function isValidHex(c: string | null | undefined): c is string {
  return typeof c === "string" && /^#[0-9a-fA-F]{3,8}$/.test(c);
}

function buildBrandTheme(mode: PaletteMode, branding?: BrandingConfig | null) {
  const light = mode === "light";
  const primaryMain = isValidHex(branding?.primary_color)
    ? branding.primary_color
    : light ? palette.primaryGreen : "#34A069";
  const secondaryMain = isValidHex(branding?.secondary_color)
    ? branding.secondary_color
    : "#D4AF37";
  const fontFamily = branding?.font_family || font.sans;
  return createTheme({
    palette: light
      ? {
          mode: "light",
          primary:    { main: primaryMain, contrastText: palette.white },
          secondary:  { main: secondaryMain, contrastText: palette.charcoal },
          warning:    { main: "#A67C00", light: "#FFF8E1", dark: "#7A5800" },
          info:       { main: primaryMain, light: palette.mint, dark: palette.primaryGreenDark },
          text:       { primary: palette.charcoal,  secondary: palette.textSecondary },
          background: { default: palette.mint,      paper: palette.white },
          divider:    palette.slateBorder,
        }
      : {
          mode: "dark",
          primary:    { main: primaryMain,  contrastText: palette.white },
          secondary:  { main: secondaryMain, contrastText: "#1A1A1A" },
          warning:    { main: "#D4AF37", light: "#3D3520", dark: "#A67C00" },
          info:       { main: primaryMain, light: "#1A2820", dark: "#004E25" },
          text:       { primary: "#E7EFEA", secondary: "#B8C8C0" },
          background: { default: "#1A2820", paper: "#1F3028" },
          divider:    "#304338",
        },
    typography: {
      fontFamily,
    },
  });
}

function Root() {
  const [preference, setPreference] = useState<ThemePreference>(() => readThemePreference());
  const [prefersDark, setPrefersDark] = useState<boolean>(() => systemPrefersDark());
  const [branding, setBranding] = useState<BrandingConfig | null>(null);

  useEffect(() => {
    const media = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;
    const onMediaChange = (e: MediaQueryListEvent) => setPrefersDark(e.matches);
    if (media) {
      setPrefersDark(media.matches);
      media.addEventListener("change", onMediaChange);
    }

    const refreshPreference = () => setPreference(readThemePreference());
    window.addEventListener("storage", refreshPreference);
    window.addEventListener(SETTINGS_CHANGED_EVENT, refreshPreference);

    return () => {
      if (media) {
        media.removeEventListener("change", onMediaChange);
      }
      window.removeEventListener("storage", refreshPreference);
      window.removeEventListener(SETTINGS_CHANGED_EVENT, refreshPreference);
    };
  }, []);

  useEffect(() => {
    const tenantId = safeLocalGet("tenant_id", "");
    if (!tenantId) return;
    brandingApi.get(tenantId).then(setBranding).catch(() => {});
  }, []);

  const paletteMode: PaletteMode =
    preference === "dark" ? "dark" : preference === "light" ? "light" : prefersDark ? "dark" : "light";
  const theme = useMemo(() => buildBrandTheme(paletteMode, branding), [paletteMode, branding]);

  return (
    <QueryClientProvider client={queryClient}>
      <ThemeProvider theme={theme}>
        <CssBaseline />
        <StaleBundleGuard />
        <App />
      </ThemeProvider>
    </QueryClientProvider>
  );
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <Root />
  </React.StrictMode>
);
