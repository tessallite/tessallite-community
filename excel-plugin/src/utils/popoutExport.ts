/**
 * Save and Copy for the popped-out answer window.
 *
 * These run in the Office dialog (src/chart-dialog.tsx), which is a real
 * browser window rather than the task-pane iframe. That is the whole reason the
 * export controls live there: `<a download>` is unreliable inside the pane, and
 * an image clipboard write needs a secure context plus a user gesture, both of
 * which a dialog click satisfies.
 *
 * Every entry point here either completes or throws with a message fit to show
 * the user. Nothing fails silently.
 */

/** Charts render through the ECharts SVG renderer, so PNG has to be rasterised. */
const PNG_BACKGROUND = "#ffffff";

// ── Text formats ────────────────────────────────────────────────────────────

function cellText(value: unknown): string {
  if (value === null || value === undefined) return "";
  return String(value);
}

/**
 * TSV, following the shape of DrillPanel's copy: header line, then one line per
 * row. Tabs and newlines inside a value are collapsed to spaces — a delimiter
 * character in the data would otherwise shift every later column of that row
 * when it is pasted into a sheet.
 */
export function toTsv(columns: string[], rows: Record<string, unknown>[]): string {
  const flatten = (value: unknown) => cellText(value).replace(/[\t\r\n]+/g, " ");
  const header = columns.map(flatten).join("\t");
  const body = rows.map((row) =>
    columns.map((col) => flatten(row[col])).join("\t"),
  );
  return [header, ...body].join("\n");
}

/**
 * CSV with RFC 4180 quoting: a field containing a comma, quote, or newline is
 * wrapped in quotes and its own quotes are doubled. Unlike TSV this keeps the
 * original text intact, because the format can represent it.
 */
export function toCsv(columns: string[], rows: Record<string, unknown>[]): string {
  const field = (value: unknown) => {
    const text = cellText(value);
    return /[",\r\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
  };
  const header = columns.map(field).join(",");
  const body = rows.map((row) => columns.map((col) => field(row[col])).join(","));
  return [header, ...body].join("\r\n");
}

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/**
 * An HTML `<table>` for the clipboard's `text/html` flavour. Excel reads that
 * flavour in preference to plain text and pastes it as a real grid, with the
 * `<th>` row landing as the header — which plain TSV cannot express.
 */
export function toHtmlTable(
  columns: string[],
  rows: Record<string, unknown>[],
): string {
  const head = columns
    .map((col) => `<th>${escapeHtml(cellText(col))}</th>`)
    .join("");
  const body = rows
    .map(
      (row) =>
        "<tr>" +
        columns
          .map((col) => `<td>${escapeHtml(cellText(row[col]))}</td>`)
          .join("") +
        "</tr>",
    )
    .join("");
  return `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}

// ── SVG and PNG ─────────────────────────────────────────────────────────────

/** Rendered size of the chart, falling back to the viewBox then a sane default. */
function svgPixelSize(svg: SVGSVGElement): { width: number; height: number } {
  const rect = svg.getBoundingClientRect();
  if (rect.width >= 1 && rect.height >= 1) {
    return { width: rect.width, height: rect.height };
  }
  const box = svg.viewBox?.baseVal;
  if (box && box.width >= 1 && box.height >= 1) {
    return { width: box.width, height: box.height };
  }
  return { width: 960, height: 540 };
}

/**
 * Serialise the live chart to a standalone SVG document.
 *
 * The clone carries explicit width/height and the SVG namespace so the result
 * stands on its own: without them a file opened outside the page has no
 * intrinsic size, and `<img>` refuses to load it during rasterisation.
 */
export function serialiseSvg(svg: SVGSVGElement): string {
  const { width, height } = svgPixelSize(svg);
  const clone = svg.cloneNode(true) as SVGSVGElement;
  clone.setAttribute("xmlns", "http://www.w3.org/2000/svg");
  clone.setAttribute("xmlns:xlink", "http://www.w3.org/1999/xlink");
  clone.setAttribute("width", String(Math.round(width)));
  clone.setAttribute("height", String(Math.round(height)));
  if (!clone.getAttribute("viewBox")) {
    clone.setAttribute("viewBox", `0 0 ${Math.round(width)} ${Math.round(height)}`);
  }
  return `<?xml version="1.0" encoding="UTF-8"?>\n${new XMLSerializer().serializeToString(clone)}`;
}

function loadImage(src: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const image = new Image();
    image.onload = () => resolve(image);
    image.onerror = () => reject(new Error("The chart image could not be rendered."));
    image.src = src;
  });
}

/**
 * Rasterise the chart to PNG.
 *
 * ECharts' own toolbox export cannot produce PNG under the SVG renderer, so the
 * serialised `<svg>` is loaded as an image and drawn onto a canvas scaled by
 * `devicePixelRatio` — otherwise the PNG is the CSS pixel size and looks soft
 * on the high-DPI screens these windows usually open on. The canvas is filled
 * first because a chart drawn for a white page is illegible on transparency.
 *
 * The same blob serves both Copy and Save as PNG.
 */
export async function svgToPngBlob(
  svg: SVGSVGElement,
  scale: number = typeof window !== "undefined" ? window.devicePixelRatio || 1 : 1,
): Promise<Blob> {
  const { width, height } = svgPixelSize(svg);
  const url = URL.createObjectURL(
    new Blob([serialiseSvg(svg)], { type: "image/svg+xml;charset=utf-8" }),
  );
  try {
    const image = await loadImage(url);
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, Math.round(width * scale));
    canvas.height = Math.max(1, Math.round(height * scale));
    const ctx = canvas.getContext("2d");
    if (!ctx) throw new Error("This browser could not prepare the image.");
    ctx.fillStyle = PNG_BACKGROUND;
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.drawImage(image, 0, 0, canvas.width, canvas.height);
    return await new Promise<Blob>((resolve, reject) => {
      canvas.toBlob(
        (blob) =>
          blob ? resolve(blob) : reject(new Error("The image could not be encoded.")),
        "image/png",
      );
    });
  } finally {
    URL.revokeObjectURL(url);
  }
}

// ── Clipboard ───────────────────────────────────────────────────────────────

/**
 * Copy a table in both flavours at once: `text/html` so Excel and Word paste a
 * grid complete with its header row, and `text/plain` (TSV) for anything that
 * only reads text. Hosts without the async clipboard fall back to text.
 */
export async function copyTableToClipboard(
  columns: string[],
  rows: Record<string, unknown>[],
): Promise<void> {
  const tsv = toTsv(columns, rows);
  const canWriteRich =
    typeof ClipboardItem !== "undefined" &&
    typeof navigator !== "undefined" &&
    typeof navigator.clipboard?.write === "function";

  if (!canWriteRich) {
    if (typeof navigator?.clipboard?.writeText !== "function") {
      throw new Error("This window is not allowed to use the clipboard.");
    }
    await navigator.clipboard.writeText(tsv);
    return;
  }

  await navigator.clipboard.write([
    new ClipboardItem({
      "text/html": new Blob([toHtmlTable(columns, rows)], { type: "text/html" }),
      "text/plain": new Blob([tsv], { type: "text/plain" }),
    }),
  ]);
}

/** Copy the chart as a bitmap, the only image flavour the clipboard accepts. */
export async function copyImageToClipboard(blob: Blob): Promise<void> {
  if (
    typeof ClipboardItem === "undefined" ||
    typeof navigator?.clipboard?.write !== "function"
  ) {
    throw new Error("This window is not allowed to put images on the clipboard.");
  }
  await navigator.clipboard.write([new ClipboardItem({ "image/png": blob })]);
}

// ── Download ────────────────────────────────────────────────────────────────

/**
 * Wrap raw bytes as a Blob.
 *
 * The copy is deliberate. Since TypeScript's DOM lib made `ArrayBufferView`
 * generic, a plain `Uint8Array` is a view over `ArrayBufferLike` — possibly a
 * `SharedArrayBuffer` — which `Blob` does not accept. A freshly allocated array
 * is a view over an ordinary `ArrayBuffer`, so the type is exact rather than
 * asserted. These payloads are one answer's worth of data, so the copy costs
 * nothing worth optimising.
 */
export function bytesToBlob(bytes: Uint8Array, type: string): Blob {
  const copy = new Uint8Array(bytes.byteLength);
  copy.set(bytes);
  return new Blob([copy], { type });
}

/** Strip what Windows forbids in a file name, and keep it a sensible length. */
export function safeFileName(base: string, extension: string): string {
  const cleaned = base
    .replace(/[\\/:*?"<>|]+/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 80);
  return `${cleaned || "tessallite-answer"}.${extension}`;
}

/**
 * Hand the file to the browser's own download. This works here because the
 * pop-out is a real window; the same call inside the task pane is unreliable,
 * which is why Save is offered in the pop-out and not in the pane.
 */
export function downloadBlob(blob: Blob, fileName: string): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = fileName;
  link.rel = "noopener";
  link.style.display = "none";
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  // Revoked on a later tick: revoking synchronously can cancel the download
  // before the browser has finished reading the object URL.
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
}
