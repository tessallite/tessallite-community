/**
 * Opens the answer — a chart or a table — in an Office dialog window.
 *
 * A task pane is a few hundred pixels wide and Office has no API to resize it,
 * so the shared chat UI's in-pane maximise can never exceed the pane.
 * `displayDialogAsync` opens a real window sized to the screen.
 *
 * A dialog cannot read add-in storage or call the API — Office allows it only
 * `messageParent`/`addHandlerAsync` — so the child says when it is listening and
 * the parent posts the payload back. Needs DialogApi 1.2 for `messageChild`.
 *
 * The traffic in the other direction is what makes Save work. The dialog is a
 * real browser window, so it can download files and write the clipboard, but it
 * cannot touch the workbook. For "Save as Excel" it therefore builds the .xlsx
 * bytes itself and posts them here as base64, and the parent — which does have
 * the Excel API — calls `Excel.createWorkbook`. See utils/xlsxWriter.ts.
 */
import {
  buildAutoChartSpec,
  parseVisualArtifact,
  type VisualArtifact,
} from "@tessallite/shared-ui";
import { strings } from "../i18n/strings";

export interface VisualActionData {
  artifact: VisualArtifact | null;
  rows: Record<string, unknown>[];
}

/**
 * Resolve the dataset used by every rendered-turn action. Persisted visual
 * artifacts survive a reload even when the turn's result sample does not, so
 * parse the artifact before the empty-data gate and use its rows as fallback.
 */
export function resolveVisualActionData(
  renderedOutput: string | null | undefined,
  resultRows?: Record<string, unknown>[],
): VisualActionData {
  const artifact = parseVisualArtifact(renderedOutput);
  const rows =
    resultRows && resultRows.length > 0
      ? resultRows
      : artifact?.rows ?? [];
  return { artifact, rows };
}

/**
 * Whether this turn shows a chart that can be popped out.
 *
 * Deliberately the same predicate `AssistantTurn.hasChart` uses, so the control
 * appears for exactly the charts on screen. Do not substitute the Excel insert
 * recommendation (`recommendChartType`): it answers "what should we write into
 * the sheet?", not "is a chart displayed?", and the two disagree — a pie-shaped
 * result renders a chart while the insert recommendation says table.
 */
export function turnHasPopoutChart(
  artifact: { chart_type?: string | null } | null | undefined,
  rows: Record<string, unknown>[] | undefined,
): boolean {
  if (artifact?.chart_type) return artifact.chart_type !== "kpi";
  if (!rows || rows.length === 0) return false;
  const spec = buildAutoChartSpec(rows);
  return spec !== null && spec.kind !== "metric";
}

/** What the popped-out window shows, and so which Save formats it offers. */
export type PopoutKind = "chart" | "table";

export interface ChartPopoutPayload {
  /**
   * Chart or table. Decides the rendered block and the Save menu. Required:
   * this payload crosses a window boundary, so it says what it is rather than
   * leaving the dialog to infer it from which fields happen to be populated.
   */
  kind: PopoutKind;
  /** Parsed `tessallite.visual.v1` artifact, when the turn carried one. */
  artifact?: Record<string, unknown> | null;
  /** Result rows: the table's data, or the source of a derived chart. */
  rows?: Record<string, unknown>[];
  /** Column order for a table export. Falls back to the first row's keys. */
  columns?: string[];
  title?: string;
  /**
   * Whether the parent can open a workbook for the dialog. Resolved here, in
   * the task pane, because the dialog runs outside the Excel document and
   * cannot answer the requirement-set question for itself.
   */
  canCreateWorkbook?: boolean;
}

/** Sent by the dialog once it is listening. */
export const POPOUT_READY = "tessallite:chart-popout:ready";

/**
 * Messages the dialog sends back to the task pane. The initial payload travels
 * the other way and carries no `type`, which is how the dialog tells an
 * instruction from its data.
 */
export type PopoutRequest =
  | { type: "close" }
  | { type: "createWorkbook"; base64: string };

/** The parent's answer to a `createWorkbook` request, so Save is never silent. */
export interface PopoutWorkbookResult {
  type: "workbookResult";
  ok: boolean;
  error?: string;
}

/** Office: a dialog is already open for this add-in (only one is allowed). */
const DIALOG_ALREADY_OPEN = 12007;

/** False in a plain browser, and on hosts too old to message the dialog. */
export function isChartPopoutSupported(): boolean {
  try {
    return (
      typeof Office?.context?.ui?.displayDialogAsync === "function" &&
      Office.context.requirements.isSetSupported("DialogApi", "1.2")
    );
  } catch {
    return false;
  }
}

/**
 * Whether "Save as Excel" can open a workbook instead of downloading a file.
 * `Excel.createWorkbook` arrived in ExcelApi 1.8; below that the dialog falls
 * back to an .xlsx download.
 */
export function isCreateWorkbookSupported(): boolean {
  try {
    return (
      typeof Excel !== "undefined" &&
      typeof Excel.createWorkbook === "function" &&
      Office.context.requirements.isSetSupported("ExcelApi", "1.8")
    );
  } catch {
    return false;
  }
}

/**
 * Assemble what the dialog needs, in one place, so both entry points — the
 * pop-out button and the Visual panel's maximise — send an identical payload.
 */
export function buildPopoutPayload({
  kind,
  artifact,
  rows,
  title,
}: {
  kind: PopoutKind;
  artifact: VisualArtifact | null;
  rows: Record<string, unknown>[];
  title?: string;
}): ChartPopoutPayload {
  return {
    kind,
    artifact: artifact as Record<string, unknown> | null,
    // A chart artifact already carries its own rows; sending them twice would
    // double the message for nothing. A table always needs them.
    rows: artifact && kind === "chart" ? undefined : rows,
    columns: artifact?.columns ?? (rows.length > 0 ? Object.keys(rows[0]!) : []),
    title,
    canCreateWorkbook: isCreateWorkbookSupported(),
  };
}

/**
 * Act on a request from the dialog. Split out so the message handler below
 * stays readable, and so each branch reports its own failure — a Save that
 * quietly does nothing is worse than one that says why.
 */
function handleDialogRequest(
  raw: string,
  dialog: Office.Dialog,
  onError?: (reason: string) => void,
): void {
  let request: PopoutRequest;
  try {
    request = JSON.parse(raw) as PopoutRequest;
  } catch {
    return; // not one of ours
  }

  if (request?.type === "close") {
    dialog.close();
    return;
  }

  if (request?.type === "createWorkbook") {
    const answer = (result: PopoutWorkbookResult) => {
      try {
        dialog.messageChild(JSON.stringify(result));
      } catch {
        // The window was closed before the workbook opened; nothing to tell.
      }
    };
    try {
      Promise.resolve(Excel.createWorkbook(request.base64))
        .then(() => answer({ type: "workbookResult", ok: true }))
        .catch(() => {
          onError?.(strings.chartPopout.workbookFailed);
          answer({
            type: "workbookResult",
            ok: false,
            error: strings.chartPopout.workbookFailed,
          });
        });
    } catch {
      onError?.(strings.chartPopout.workbookFailed);
      answer({
        type: "workbookResult",
        ok: false,
        error: strings.chartPopout.workbookFailed,
      });
    }
  }
}

/**
 * @param onError told why the window did not open, so the click is never a
 *   silent no-op. Office allows one dialog per add-in, so the common case is a
 *   second pop-out while the first is still open (12007).
 */
export function openChartPopout(
  payload: ChartPopoutPayload,
  onError?: (reason: string) => void,
): void {
  const url = new URL("chart-dialog.html", document.baseURI).href;
  Office.context.ui.displayDialogAsync(
    url,
    { height: 70, width: 70, displayInIframe: false },
    (result) => {
      if (result.status !== Office.AsyncResultStatus.Succeeded) {
        onError?.(
          result.error?.code === DIALOG_ALREADY_OPEN
            ? strings.chartPopout.alreadyOpen
            : strings.chartPopout.openFailed,
        );
        return;
      }
      const dialog = result.value;
      dialog.addEventHandler(Office.EventType.DialogMessageReceived, (arg) => {
        const message = (arg as { message?: string }).message;
        // Only send once the child is listening; sending earlier races its load.
        if (message === POPOUT_READY) {
          dialog.messageChild(JSON.stringify(payload));
          return;
        }
        if (typeof message === "string") {
          handleDialogRequest(message, dialog, onError);
        }
      });
    },
  );
}
