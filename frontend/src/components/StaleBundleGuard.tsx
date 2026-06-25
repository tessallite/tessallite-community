import { useEffect, useState } from "react";
import { Alert, Box, Button, Snackbar } from "@mui/material";
import { useT } from "../i18n";

function isChunkLoadError(err: unknown): boolean {
  const msg = (err as { message?: string } | null)?.message ?? "";
  return (
    msg.includes("Failed to fetch dynamically imported module") ||
    msg.includes("Importing a module script failed") ||
    msg.includes("error loading dynamically imported module")
  );
}

function isJsPreloadFailure(payload: unknown): boolean {
  if (isChunkLoadError(payload)) return true;
  const href =
    (payload as { href?: string } | null)?.href ??
    (payload as { url?: string } | null)?.url ??
    "";
  return /\.(js|mjs)(\?|$)/i.test(href);
}

export default function StaleBundleGuard() {
  const [stale, setStale] = useState(false);
  const t = useT();

  useEffect(() => {
    function onError(e: ErrorEvent) {
      if (isChunkLoadError(e.error) || isChunkLoadError({ message: e.message })) {
        setStale(true);
      }
    }
    function onRejection(e: PromiseRejectionEvent) {
      if (isChunkLoadError(e.reason)) {
        setStale(true);
      }
    }
    function onVitePreloadError(e: Event) {
      const ev = e as Event & { payload?: unknown };
      if (isJsPreloadFailure(ev.payload)) {
        setStale(true);
      }
    }
    window.addEventListener("error", onError);
    window.addEventListener("unhandledrejection", onRejection);
    window.addEventListener("vite:preloadError", onVitePreloadError);
    return () => {
      window.removeEventListener("error", onError);
      window.removeEventListener("unhandledrejection", onRejection);
      window.removeEventListener("vite:preloadError", onVitePreloadError);
    };
  }, []);

  if (!stale) return null;

  function reload() {
    window.location.reload();
  }

  return (
    <Snackbar
      open
      anchorOrigin={{ vertical: "top", horizontal: "center" }}
      sx={{ zIndex: (t) => t.zIndex.modal + 1 }}
    >
      <Alert
        severity="warning"
        action={
          <Box sx={{ display: "flex", gap: 1 }}>
            <Button color="inherit" size="small" onClick={reload}>
              {t("common.reload")}
            </Button>
          </Box>
        }
      >
        {t("errors.newerVersionAvailable")}
      </Alert>
    </Snackbar>
  );
}
