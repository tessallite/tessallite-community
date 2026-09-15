/**
 * Minimal .xlsx writer, with no dependency.
 *
 * Why hand-rolled rather than SheetJS: an .xlsx is only a ZIP holding a handful
 * of small XML parts, and the add-in needs exactly one shape of workbook — a
 * single sheet, a header row, and a rectangle of values. Pulling a general
 * spreadsheet library in for that would add megabytes to the task-pane bundle
 * and a supply-chain dependency for roughly eighty lines of format code.
 *
 * Two choices keep it that small:
 *
 * 1. **STORE, never DEFLATE.** ZIP allows compression method 0, which writes
 *    each part's bytes verbatim. That removes the only part of the format that
 *    would genuinely need a library. The cost is file size; these workbooks are
 *    result sets from one answer, so it does not matter. What still has to be
 *    computed by hand is the CRC-32 of every part, which the ZIP headers carry.
 *
 * 2. **Inline strings, no shared-strings part.** Writing each text cell as
 *    `t="inlineStr"` with its own `<is><t>` removes `xl/sharedStrings.xml` and
 *    the string-interning pass that fills it. Numbers are written bare, so
 *    Excel receives them as numbers and they stay right-aligned and summable.
 *    No `styles.xml` is emitted either — Excel supplies its own defaults.
 *
 * The result is a genuine OOXML SpreadsheetML package: Excel, LibreOffice, and
 * anything reading the standard parts open it without a repair prompt.
 *
 * The bytes are handed to `Excel.createWorkbook` (ExcelApi 1.8) as base64 so
 * the workbook opens in the user's own Excel, which is why `bytesToBase64` is
 * here too. See utils/chartPopout.ts and src/chart-dialog.tsx.
 */

/** OOXML part paths and the fixed XML that never varies with the data. */
const CONTENT_TYPES_XML =
  '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
  '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">' +
  '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>' +
  '<Default Extension="xml" ContentType="application/xml"/>' +
  '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>' +
  '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' +
  "</Types>";

const ROOT_RELS_XML =
  '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
  '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' +
  '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>' +
  "</Relationships>";

const WORKBOOK_RELS_XML =
  '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
  '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' +
  '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>' +
  "</Relationships>";

/** Excel's own limits on a sheet name. Exceeding either makes the file invalid. */
const SHEET_NAME_MAX = 31;
const SHEET_NAME_ILLEGAL = /[\\/?*[\]:]/g;

/** XML 1.0 forbids most C0 control characters outright; they cannot be escaped. */
const XML_FORBIDDEN = /[\x00-\x08\x0B\x0C\x0E-\x1F]/g;

function escapeXml(value: string): string {
  return value
    .replace(XML_FORBIDDEN, "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/** 0 -> "A", 25 -> "Z", 26 -> "AA". Sheet columns are base-26 with no zero. */
function columnLetter(index: number): string {
  let n = index;
  let out = "";
  do {
    out = String.fromCharCode(65 + (n % 26)) + out;
    n = Math.floor(n / 26) - 1;
  } while (n >= 0);
  return out;
}

function sanitiseSheetName(name: string): string {
  const cleaned = name.replace(SHEET_NAME_ILLEGAL, " ").trim();
  if (!cleaned) return "Data";
  return cleaned.slice(0, SHEET_NAME_MAX);
}

/**
 * One cell of SpreadsheetML.
 *
 * A finite number is written bare so Excel stores it as a number. Everything
 * else becomes an inline string. `null`/`undefined` emit no cell at all, which
 * is how OOXML represents a genuinely empty cell (an empty `<v>` would be read
 * as the number zero).
 */
function cellXml(ref: string, value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "number" && Number.isFinite(value)) {
    return `<c r="${ref}"><v>${value}</v></c>`;
  }
  if (typeof value === "boolean") {
    return `<c r="${ref}" t="b"><v>${value ? 1 : 0}</v></c>`;
  }
  const text = typeof value === "string" ? value : String(value);
  if (text === "") return "";
  return (
    `<c r="${ref}" t="inlineStr"><is><t xml:space="preserve">` +
    `${escapeXml(text)}</t></is></c>`
  );
}

function sheetXml(columns: string[], rows: Record<string, unknown>[]): string {
  const parts: string[] = [
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
    '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
    "<sheetData>",
  ];

  // Row 1 is always the header row — every export carries its column names.
  parts.push('<row r="1">');
  columns.forEach((col, i) => {
    parts.push(cellXml(`${columnLetter(i)}1`, col));
  });
  parts.push("</row>");

  rows.forEach((row, r) => {
    const rowNumber = r + 2; // 1-based, and row 1 is the header
    parts.push(`<row r="${rowNumber}">`);
    columns.forEach((col, c) => {
      parts.push(cellXml(`${columnLetter(c)}${rowNumber}`, row[col]));
    });
    parts.push("</row>");
  });

  parts.push("</sheetData></worksheet>");
  return parts.join("");
}

function workbookXml(sheetName: string): string {
  return (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' +
    '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" ' +
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">' +
    `<sheets><sheet name="${escapeXml(sheetName)}" sheetId="1" r:id="rId1"/></sheets>` +
    "</workbook>"
  );
}

// ── ZIP (STORE only) ────────────────────────────────────────────────────────

/** Standard CRC-32 (IEEE 802.3), built once on first use. */
let crcTable: Uint32Array | null = null;

function getCrcTable(): Uint32Array {
  if (crcTable) return crcTable;
  const table = new Uint32Array(256);
  for (let i = 0; i < 256; i++) {
    let c = i;
    for (let k = 0; k < 8; k++) {
      c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    }
    table[i] = c >>> 0;
  }
  crcTable = table;
  return table;
}

function crc32(bytes: Uint8Array): number {
  const table = getCrcTable();
  let crc = 0xffffffff;
  for (let i = 0; i < bytes.length; i++) {
    crc = table[(crc ^ bytes[i]!) & 0xff]! ^ (crc >>> 8);
  }
  return (crc ^ 0xffffffff) >>> 0;
}

interface ZipEntry {
  name: string;
  data: Uint8Array;
}

/** Little-endian writers over a growing byte list. */
function pushU16(out: number[], value: number): void {
  out.push(value & 0xff, (value >>> 8) & 0xff);
}

function pushU32(out: number[], value: number): void {
  out.push(
    value & 0xff,
    (value >>> 8) & 0xff,
    (value >>> 16) & 0xff,
    (value >>> 24) & 0xff,
  );
}

function pushBytes(out: number[], bytes: Uint8Array): void {
  for (let i = 0; i < bytes.length; i++) out.push(bytes[i]!);
}

/**
 * DOS date/time, the only timestamp format ZIP local headers carry. Seconds
 * have 2-second resolution, and the year is an offset from 1980.
 */
function dosDateTime(when: Date): { time: number; date: number } {
  const year = Math.max(1980, when.getFullYear());
  return {
    time:
      (when.getHours() << 11) |
      (when.getMinutes() << 5) |
      Math.floor(when.getSeconds() / 2),
    date: ((year - 1980) << 9) | ((when.getMonth() + 1) << 5) | when.getDate(),
  };
}

/**
 * Assemble a ZIP archive with every entry stored uncompressed.
 *
 * Layout is the ZIP spec's: each entry's local file header followed by its
 * bytes, then one central-directory header per entry, then the end-of-central-
 * directory record pointing at that directory. Bit 11 of the general-purpose
 * flags marks entry names as UTF-8; all part names here are ASCII, but the flag
 * is what tells a reader not to guess a legacy code page.
 */
function zipStore(entries: ZipEntry[], when: Date): Uint8Array {
  const encoder = new TextEncoder();
  const { time, date } = dosDateTime(when);
  const local: number[] = [];
  const central: number[] = [];

  for (const entry of entries) {
    const nameBytes = encoder.encode(entry.name);
    const crc = crc32(entry.data);
    const offset = local.length;

    pushU32(local, 0x04034b50); // local file header signature
    pushU16(local, 20); // version needed to extract (2.0)
    pushU16(local, 0x0800); // flags: UTF-8 names
    pushU16(local, 0); // compression method: store
    pushU16(local, time);
    pushU16(local, date);
    pushU32(local, crc);
    pushU32(local, entry.data.length); // compressed size == uncompressed
    pushU32(local, entry.data.length);
    pushU16(local, nameBytes.length);
    pushU16(local, 0); // extra field length
    pushBytes(local, nameBytes);
    pushBytes(local, entry.data);

    pushU32(central, 0x02014b50); // central directory header signature
    pushU16(central, 20); // version made by
    pushU16(central, 20); // version needed to extract
    pushU16(central, 0x0800);
    pushU16(central, 0);
    pushU16(central, time);
    pushU16(central, date);
    pushU32(central, crc);
    pushU32(central, entry.data.length);
    pushU32(central, entry.data.length);
    pushU16(central, nameBytes.length);
    pushU16(central, 0); // extra field length
    pushU16(central, 0); // file comment length
    pushU16(central, 0); // disk number start
    pushU16(central, 0); // internal file attributes
    pushU32(central, 0); // external file attributes
    pushU32(central, offset); // relative offset of local header
    pushBytes(central, nameBytes);
  }

  const centralOffset = local.length;
  const end: number[] = [];
  pushU32(end, 0x06054b50); // end of central directory signature
  pushU16(end, 0); // number of this disk
  pushU16(end, 0); // disk with the start of the central directory
  pushU16(end, entries.length);
  pushU16(end, entries.length);
  pushU32(end, central.length);
  pushU32(end, centralOffset);
  pushU16(end, 0); // comment length

  // Concatenated by index rather than by spreading the three arrays into one
  // call: a workbook of any size would exceed the argument limit and throw.
  const out = new Uint8Array(local.length + central.length + end.length);
  let at = 0;
  for (const part of [local, central, end]) {
    for (let i = 0; i < part.length; i++) out[at++] = part[i]!;
  }
  return out;
}

// ── Public API ──────────────────────────────────────────────────────────────

export interface XlsxOptions {
  /** Sheet tab name. Sanitised to Excel's rules; defaults to "Data". */
  sheetName?: string;
  /** Timestamp written into the ZIP headers. Injectable so tests are stable. */
  now?: Date;
}

/**
 * Build a single-sheet .xlsx from a header row and a rectangle of values.
 *
 * `columns` drives both the header row and the per-row lookup, so the export
 * always carries its column names and the column order is the caller's, not
 * whatever order a row object happens to enumerate in.
 */
export function buildXlsx(
  columns: string[],
  rows: Record<string, unknown>[],
  options: XlsxOptions = {},
): Uint8Array {
  const encoder = new TextEncoder();
  const sheetName = sanitiseSheetName(options.sheetName ?? "Data");
  const entries: ZipEntry[] = [
    { name: "[Content_Types].xml", data: encoder.encode(CONTENT_TYPES_XML) },
    { name: "_rels/.rels", data: encoder.encode(ROOT_RELS_XML) },
    { name: "xl/workbook.xml", data: encoder.encode(workbookXml(sheetName)) },
    { name: "xl/_rels/workbook.xml.rels", data: encoder.encode(WORKBOOK_RELS_XML) },
    {
      name: "xl/worksheets/sheet1.xml",
      data: encoder.encode(sheetXml(columns, rows)),
    },
  ];
  return zipStore(entries, options.now ?? new Date());
}

/**
 * Base64 for `Excel.createWorkbook`, chunked because `String.fromCharCode`
 * applied to a whole workbook at once overflows the argument stack.
 */
export function bytesToBase64(bytes: Uint8Array): string {
  const CHUNK = 0x8000;
  let binary = "";
  for (let i = 0; i < bytes.length; i += CHUNK) {
    binary += String.fromCharCode(...bytes.subarray(i, i + CHUNK));
  }
  return btoa(binary);
}
