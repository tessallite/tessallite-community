/**
 * Entry for the popped-out answer window: a chart or a table, with Save, Copy
 * and Close.
 *
 * It announces itself to the task pane, renders whatever payload comes back,
 * and owns the export controls. Save and Copy live here rather than in the
 * pane because this is a real browser window: `<a download>` works, and the
 * clipboard grants an image write to a click in a secure context. Neither is
 * dependable inside the task-pane iframe.
 *
 * The one thing this window cannot do is touch the workbook, so "Save as
 * Excel" builds the .xlsx bytes here (utils/xlsxWriter.ts) and posts them to
 * the pane, which calls `Excel.createWorkbook`. See utils/chartPopout.ts.
 */
import { StrictMode, useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { MouseEvent, ReactNode } from "react";
import ReactDOM from "react-dom/client";
import {
  Box,
  CssBaseline,
  IconButton,
  Menu,
  MenuItem,
  Snackbar,
  ThemeProvider,
  Tooltip,
  Typography,
} from "@mui/material";
import { Close, ContentCopy, SaveAlt } from "@mui/icons-material";
import {
  ChatProvider,
  ChartBlock,
  DataTableBlock,
  VisualArtifactBlock,
  type VisualArtifact,
} from "@tessallite/shared-ui";
import { chatT } from "./i18n/chatStrings";
import { strings } from "./i18n/strings";
import { theme, tokens } from "./theme";
import {
  POPOUT_READY,
  type ChartPopoutPayload,
  type PopoutKind,
  type PopoutRequest,
  type PopoutWorkbookResult,
} from "./utils/chartPopout";
import {
  bytesToBlob,
  copyImageToClipboard,
  copyTableToClipboard,
  downloadBlob,
  safeFileName,
  serialiseSvg,
  svgToPngBlob,
  toCsv,
  toTsv,
} from "./utils/popoutExport";
import { buildXlsx, bytesToBase64 } from "./utils/xlsxWriter";

const XLSX_MIME =
  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";

/**
 * Above this many base64 characters the workbook is downloaded rather than
 * posted to the task pane. Office publishes no ceiling for dialog messages and
 * hosts differ, so a very large result takes the path that cannot be silently
 * dropped. Ordinary answers are orders of magnitude below it.
 */
const WORKBOOK_MESSAGE_MAX = 900_000;

/** Room left below the toolbar row: the table also carries a pagination bar. */
const CHART_HEIGHT = "calc(100vh - 96px)";
const TABLE_HEIGHT = "calc(100vh - 150px)";

type SaveFormat = "excel" | "csv" | "tsv" | "svg" | "png";

/**
 * Save menu contents by kind. Built per render rather than held in a module
 * constant: `strings` resolves against the locale active at read time.
 */
function saveFormatsFor(kind: PopoutKind): { format: SaveFormat; label: string }[] {
  return kind === "table"
    ? [
        { format: "excel", label: strings.chartPopout.saveExcel },
        { format: "csv", label: strings.chartPopout.saveCsv },
        { format: "tsv", label: strings.chartPopout.saveTsv },
      ]
    : [
        { format: "svg", label: strings.chartPopout.saveSvg },
        { format: "png", label: strings.chartPopout.savePng },
      ];
}

/** Send an instruction to the task pane; report rather than fail silently. */
function requestFromParent(request: PopoutRequest): boolean {
  try {
    Office.context.ui.messageParent(JSON.stringify(request));
    return true;
  } catch {
    return false;
  }
}

function PopoutWindow() {
  const [payload, setPayload] = useState<ChartPopoutPayload | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [saveAnchor, setSaveAnchor] = useState<HTMLElement | null>(null);
  // Wraps the rendered visual so the chart's live <svg> can be found for the
  // SVG and PNG exports without reaching across the whole document.
  const visualRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    Office.onReady(() => {
      Office.context.ui.addHandlerAsync(
        Office.EventType.DialogParentMessageReceived,
        (arg: { message: string }) => {
          let parsed: unknown;
          try {
            parsed = JSON.parse(arg.message);
          } catch {
            setPayload({ kind: "chart" });
            return;
          }
          // The pane sends two kinds of message down this one channel: the
          // initial payload, and later the outcome of a workbook request. Only
          // the latter carries `type`.
          if (
            parsed &&
            typeof parsed === "object" &&
            (parsed as { type?: string }).type === "workbookResult"
          ) {
            const result = parsed as PopoutWorkbookResult;
            setStatus(
              result.ok
                ? strings.chartPopout.openedInExcel
                : result.error || strings.chartPopout.workbookFailed,
            );
            return;
          }
          setPayload(parsed as ChartPopoutPayload);
        },
        () => Office.context.ui.messageParent(POPOUT_READY),
      );
    });
  }, []);

  const artifact = payload?.artifact as VisualArtifact | undefined;
  const kind: PopoutKind = payload?.kind ?? "chart";

  // A chart artifact carries its own rows, so the pane does not send them
  // twice; either source is the same data.
  const rows = useMemo<Record<string, unknown>[]>(() => {
    if (payload?.rows && payload.rows.length > 0) return payload.rows;
    return artifact?.rows ?? [];
  }, [payload, artifact]);

  const columns = useMemo<string[]>(() => {
    if (payload?.columns && payload.columns.length > 0) return payload.columns;
    return rows.length > 0 ? Object.keys(rows[0]!) : [];
  }, [payload, rows]);

  const baseName = payload?.title?.trim() || "tessallite-answer";

  const findSvg = useCallback(
    () => visualRef.current?.querySelector("svg") as SVGSVGElement | null,
    [],
  );

  const handleCopy = useCallback(async () => {
    if (kind === "table") {
      if (rows.length === 0) {
        setStatus(strings.chartPopout.nothingToExport);
        return;
      }
      try {
        await copyTableToClipboard(columns, rows);
        setStatus(strings.chartPopout.copiedTable);
      } catch {
        setStatus(strings.chartPopout.copyFailed);
      }
      return;
    }

    const svg = findSvg();
    if (!svg) {
      setStatus(strings.chartPopout.chartNotReady);
      return;
    }
    let image: Blob;
    try {
      image = await svgToPngBlob(svg);
    } catch {
      setStatus(strings.chartPopout.chartNotReady);
      return;
    }
    try {
      await copyImageToClipboard(image);
      setStatus(strings.chartPopout.copiedChart);
    } catch {
      setStatus(strings.chartPopout.copyFailed);
    }
  }, [kind, rows, columns, findSvg]);

  /**
   * Open the result as a workbook in the user's own Excel, so they save it
   * where they like with Excel's own Save. Hosts below ExcelApi 1.8, and
   * results too large to post safely, download the same bytes instead.
   */
  const saveAsExcel = useCallback(() => {
    const bytes = buildXlsx(columns, rows, { sheetName: baseName });
    if (payload?.canCreateWorkbook) {
      const base64 = bytesToBase64(bytes);
      if (base64.length <= WORKBOOK_MESSAGE_MAX) {
        if (requestFromParent({ type: "createWorkbook", base64 })) {
          setStatus(strings.chartPopout.openingInExcel);
          return;
        }
        setStatus(strings.chartPopout.workbookFailed);
        return;
      }
    }
    downloadBlob(bytesToBlob(bytes, XLSX_MIME), safeFileName(baseName, "xlsx"));
    setStatus(strings.chartPopout.savedFile);
  }, [columns, rows, baseName, payload]);

  const handleSave = useCallback(
    async (format: SaveFormat) => {
      setSaveAnchor(null);

      if (format === "svg" || format === "png") {
        const svg = findSvg();
        if (!svg) {
          setStatus(strings.chartPopout.chartNotReady);
          return;
        }
        try {
          if (format === "svg") {
            downloadBlob(
              new Blob([serialiseSvg(svg)], { type: "image/svg+xml;charset=utf-8" }),
              safeFileName(baseName, "svg"),
            );
          } else {
            downloadBlob(await svgToPngBlob(svg), safeFileName(baseName, "png"));
          }
          setStatus(strings.chartPopout.savedFile);
        } catch {
          setStatus(strings.chartPopout.saveFailed);
        }
        return;
      }

      if (rows.length === 0) {
        setStatus(strings.chartPopout.nothingToExport);
        return;
      }
      try {
        if (format === "excel") {
          saveAsExcel();
        } else if (format === "csv") {
          downloadBlob(
            new Blob([toCsv(columns, rows)], { type: "text/csv;charset=utf-8" }),
            safeFileName(baseName, "csv"),
          );
          setStatus(strings.chartPopout.savedFile);
        } else {
          downloadBlob(
            new Blob([toTsv(columns, rows)], {
              type: "text/tab-separated-values;charset=utf-8",
            }),
            safeFileName(baseName, "tsv"),
          );
          setStatus(strings.chartPopout.savedFile);
        }
      } catch {
        setStatus(strings.chartPopout.saveFailed);
      }
    },
    [baseName, columns, rows, findSvg, saveAsExcel],
  );

  const handleClose = useCallback(() => {
    // Office closes its own dialog from the parent side; a dialog cannot
    // reliably close itself.
    if (!requestFromParent({ type: "close" })) {
      setStatus(strings.chartPopout.closeFailed);
    }
  }, []);

  if (!payload) {
    return (
      <Box sx={{ p: 3 }}>
        <Typography variant="body2" color="text.secondary">
          {strings.chartPopout.loading}
        </Typography>
      </Box>
    );
  }

  const hasChart = Boolean(artifact?.chart_type) || rows.length > 0;
  const hasContent = kind === "table" ? rows.length > 0 : hasChart;
  const saveOptions = saveFormatsFor(kind);

  const toolbarButton = (
    label: string,
    icon: ReactNode,
    onClick: (event: MouseEvent<HTMLElement>) => void,
    menu?: boolean,
  ) => (
    <Tooltip title={label}>
      <span>
        <IconButton
          size="small"
          aria-label={label}
          aria-haspopup={menu ? "menu" : undefined}
          aria-expanded={menu ? Boolean(saveAnchor) : undefined}
          onClick={onClick}
          disabled={!hasContent}
          sx={{
            width: 28,
            height: 26,
            p: 0,
            borderRadius: 0.5,
            border: 1,
            borderColor: tokens.colorBorder,
            color: tokens.colorCharcoal,
          }}
        >
          {icon}
        </IconButton>
      </span>
    </Tooltip>
  );

  return (
    <Box
      sx={{
        height: "100vh",
        boxSizing: "border-box",
        display: "flex",
        flexDirection: "column",
      }}
    >
      <Box
        sx={{
          display: "flex",
          alignItems: "center",
          gap: 1,
          px: 2,
          py: 1,
          borderBottom: 1,
          borderColor: tokens.colorBorderLight,
        }}
      >
        <Typography
          sx={{
            flex: 1,
            minWidth: 0,
            fontWeight: 650,
            fontSize: 14,
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
          }}
        >
          {payload.title ?? ""}
        </Typography>
        {toolbarButton(
          strings.chartPopout.save,
          <SaveAlt sx={{ fontSize: 16 }} />,
          (event) => setSaveAnchor(event.currentTarget),
          true,
        )}
        {toolbarButton(
          strings.chartPopout.copy,
          <ContentCopy sx={{ fontSize: 16 }} />,
          () => {
            void handleCopy();
          },
        )}
        <Tooltip title={strings.chartPopout.close}>
          <IconButton
            size="small"
            aria-label={strings.chartPopout.close}
            onClick={handleClose}
            sx={{
              width: 28,
              height: 26,
              p: 0,
              borderRadius: 0.5,
              border: 1,
              borderColor: tokens.colorBorder,
              color: tokens.colorCharcoal,
            }}
          >
            <Close sx={{ fontSize: 16 }} />
          </IconButton>
        </Tooltip>
      </Box>

      <Menu
        anchorEl={saveAnchor}
        open={Boolean(saveAnchor)}
        onClose={() => setSaveAnchor(null)}
        anchorOrigin={{ vertical: "bottom", horizontal: "right" }}
        transformOrigin={{ vertical: "top", horizontal: "right" }}
      >
        {saveOptions.map(({ format, label }) => (
          <MenuItem
            key={format}
            dense
            onClick={() => {
              void handleSave(format);
            }}
            sx={{ fontSize: 13 }}
          >
            {label}
          </MenuItem>
        ))}
      </Menu>

      <Box ref={visualRef} sx={{ flex: 1, minHeight: 0, overflow: "auto", px: 2, pb: 2 }}>
        {!hasContent ? (
          <Typography variant="body2" color="text.secondary" sx={{ mt: 2 }}>
            {strings.chartPopout.empty}
          </Typography>
        ) : kind === "table" ? (
          <DataTableBlock
            rows={rows}
            maxHeight={TABLE_HEIGHT}
            showCopy={false}
          />
        ) : artifact?.chart_type ? (
          <VisualArtifactBlock artifact={artifact} heightOverride={CHART_HEIGHT} />
        ) : (
          <ChartBlock rows={rows} heightOverride={CHART_HEIGHT} />
        )}
      </Box>

      <Snackbar
        open={status !== null}
        message={status ?? ""}
        autoHideDuration={5000}
        onClose={() => setStatus(null)}
        anchorOrigin={{ vertical: "bottom", horizontal: "center" }}
      />
    </Box>
  );
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <StrictMode>
    {/* Same theme as the task pane, so the popped-out answer looks like the
        pane it came from. CssBaseline also clears the browser's default body
        margin, which the full-height layout above depends on. */}
    <ThemeProvider theme={theme}>
      <CssBaseline />
      {/* The blocks read `t` from chat context; nothing here streams, so the
          adapter and project are inert. */}
      <ChatProvider
        adapter={{} as never}
        t={chatT}
        projectId=""
        config={null}
      >
        <PopoutWindow />
      </ChatProvider>
    </ThemeProvider>
  </StrictMode>,
);
