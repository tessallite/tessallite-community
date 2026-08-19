import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  Box,
  Dialog,
  DialogContent,
  DialogTitle,
  Typography,
  IconButton,
} from "@mui/material";
import { Fullscreen, Close } from "@mui/icons-material";
import DOMPurify from "dompurify";
import { ErrorBoundary } from "./ErrorBoundary";
import { useChatContext } from "../providers/ChatProvider";

interface RenderedOutputProps {
  html: string;
  chartsCss?: string;
  /**
   * Bug-6584: optional URL of a served charts stylesheet. Only when this is a
   * non-empty string does the iframe emit a `<link rel="stylesheet">`. Hosts
   * that inline the stylesheet via `chartsCss` (the frontend, conversational
   * client, and Excel task-pane plugin all do) must NOT set this — a hardcoded
   * `/charts.min.css` link 404s in the Excel host, which does not serve it.
   */
  chartsCssHref?: string;
}

function RenderedContent({
  html,
  chartsCss,
  chartsCssHref,
}: {
  html: string;
  chartsCss?: string;
  chartsCssHref?: string;
}) {
  const iframeRef = useRef<HTMLIFrameElement | null>(null);
  const resizeObserverRef = useRef<ResizeObserver | null>(null);
  const [height, setHeight] = useState(360);
  const { t } = useChatContext();

  const sanitized = useMemo(
    () =>
      DOMPurify.sanitize(html, {
        ADD_TAGS: [
          "style",
          "svg",
          "path",
          "circle",
          "rect",
          "line",
          "polyline",
          "polygon",
          "text",
          "g",
          "details",
          "summary",
        ],
        ADD_ATTR: [
          "viewBox",
          "d",
          "cx",
          "cy",
          "r",
          "x",
          "y",
          "width",
          "height",
          "fill",
          "stroke",
          "transform",
          "class",
          "id",
          "points",
          "x1",
          "y1",
          "x2",
          "y2",
          "font-size",
          "text-anchor",
          "style",
          "scope",
          "colspan",
          "rowspan",
          "open",
        ],
        FORBID_TAGS: [
          "script",
          "iframe",
          "object",
          "embed",
          "form",
          "input",
          "button",
          "select",
          "textarea",
        ],
        FORBID_ATTR: [
          "onclick",
          "onload",
          "onerror",
          "onmouseover",
          "onfocus",
          "onblur",
          "onsubmit",
          "formaction",
        ],
      }),
    [html],
  );

  // Bug-6584: only emit the stylesheet <link> when a caller supplies a served
  // URL. Attribute-encode it so the href cannot break out of the attribute.
  const styleLink =
    chartsCssHref && chartsCssHref.trim()
      ? `<link rel="stylesheet" href="${chartsCssHref
          .replace(/&/g, "&amp;")
          .replace(/"/g, "&quot;")
          .replace(/</g, "&lt;")
          .replace(/>/g, "&gt;")}" />`
      : "";

  const srcDoc = useMemo(
    () => `<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    ${styleLink}
    <style>
      ${chartsCss ?? ""}
      :root { color-scheme: light; }
      * { box-sizing: border-box; }
      html { margin: 0; padding: 0; }
      body {
        margin: 0;
        padding: 0;
        font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        color: #1f2933;
        background: transparent;
        overflow-x: auto;
      }
      body > * + * { margin-top: 12px; }
      img, svg, canvas, video { max-width: 100%; height: auto; }
      table:not(.charts-css) {
        border-collapse: collapse;
        width: max-content;
        min-width: 100%;
        font-size: 12px;
        margin-top: 10px;
      }
      table:not(.charts-css) th,
      table:not(.charts-css) td {
        border: 1px solid #d0d7de;
        padding: 6px 8px;
        text-align: left;
        vertical-align: top;
        white-space: nowrap;
      }
      table:not(.charts-css) th {
        background: #f6f8fa;
        font-weight: 650;
      }
      .charts-css {
        min-height: 160px;
        overflow: visible;
      }
      .compound-result,
      .compound-step {
        display: block;
        max-width: 100%;
      }
      .compound-kpi {
        border: 1px solid #d0d7de;
        border-radius: 8px;
        padding: 12px;
        margin-bottom: 10px;
      }
      details.compound-step {
        border: 1px solid #d0d7de;
        border-radius: 8px;
        padding: 8px 10px;
        margin-top: 10px;
      }
      details.compound-step summary {
        cursor: pointer;
        font-weight: 650;
      }
    </style>
  </head>
  <body>${sanitized}</body>
</html>`,
    [sanitized, chartsCss, styleLink],
  );

  const updateHeight = useCallback(() => {
    const doc = iframeRef.current?.contentDocument;
    if (!doc) return;
    const bodyHeight = doc.body?.scrollHeight ?? 0;
    const documentHeight = doc.documentElement?.scrollHeight ?? 0;
    const contentHeight = Math.max(bodyHeight, documentHeight);
    const nextHeight = Math.max(220, Math.min(760, contentHeight));
    if (Number.isFinite(nextHeight)) setHeight(nextHeight);
  }, []);

  const handleLoad = useCallback(() => {
    resizeObserverRef.current?.disconnect();
    updateHeight();
    const body = iframeRef.current?.contentDocument?.body;
    if (body && typeof ResizeObserver !== "undefined") {
      const observer = new ResizeObserver(updateHeight);
      observer.observe(body);
      resizeObserverRef.current = observer;
    }
  }, [updateHeight]);

  useEffect(() => {
    return () => resizeObserverRef.current?.disconnect();
  }, []);

  if (!sanitized.trim()) {
    return (
      <Box sx={{ p: 2, textAlign: "center" }}>
        <Typography variant="body2" color="text.secondary">
          {t("renderedOutput.unsafeFallback")}
        </Typography>
      </Box>
    );
  }

  return (
    <Box
      component="iframe"
      ref={iframeRef}
      title={t("renderedOutput.iframeTitle")}
      /* allow-scripts must NEVER be added — allow-same-origin + allow-scripts defeats the sandbox */
      sandbox="allow-same-origin"
      srcDoc={srcDoc}
      onLoad={handleLoad}
      sx={{
        width: "100%",
        height,
        maxHeight: "70vh",
        border: 0,
        display: "block",
        bgcolor: "background.default",
      }}
    />
  );
}

export function RenderedOutput({ html, chartsCss, chartsCssHref }: RenderedOutputProps) {
  const [expanded, setExpanded] = useState(false);
  const { t } = useChatContext();

  if (!html) return null;

  return (
    <ErrorBoundary
      fallback={
        <Box
          sx={{ p: 2, bgcolor: "action.hover", borderRadius: 1, mt: 1 }}
        >
          <Typography variant="body2" color="warning.main">
            {t("renderedOutput.unsafeFallback")}
          </Typography>
        </Box>
      }
    >
      <Box sx={{ mt: 1, position: "relative", minWidth: 0 }}>
        <Box
          sx={{
            border: 1,
            borderColor: "divider",
            borderRadius: 1,
            overflow: "hidden",
            bgcolor: "background.paper",
          }}
        >
          <RenderedContent html={html} chartsCss={chartsCss} chartsCssHref={chartsCssHref} />
        </Box>
        <IconButton
          size="small"
          onClick={() => setExpanded(true)}
          aria-label={t("renderedOutput.openLargerAria")}
          sx={{
            position: "absolute",
            top: 4,
            right: 4,
            bgcolor: "background.paper",
            opacity: 0.7,
            "&:hover": { opacity: 1 },
          }}
        >
          <Fullscreen fontSize="small" />
        </IconButton>
      </Box>

      <Dialog
        open={expanded}
        onClose={() => setExpanded(false)}
        maxWidth="lg"
        fullWidth
      >
        <DialogTitle
          sx={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
          }}
        >
          {t("renderedOutput.dialogTitle")}
          <IconButton
            onClick={() => setExpanded(false)}
            aria-label={t("renderedOutput.closeAria")}
          >
            <Close />
          </IconButton>
        </DialogTitle>
        <DialogContent sx={{ p: 0 }}>
          <ErrorBoundary>
            <RenderedContent html={html} chartsCss={chartsCss} chartsCssHref={chartsCssHref} />
          </ErrorBoundary>
        </DialogContent>
      </Dialog>
    </ErrorBoundary>
  );
}
