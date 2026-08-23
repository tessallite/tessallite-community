import DOMPurify from "dompurify";

export function sanitizeHtml(dirty: string): string {
  return DOMPurify.sanitize(dirty, {
    ALLOWED_TAGS: [
      "p", "br", "strong", "em", "b", "i", "u", "code", "pre",
      "ul", "ol", "li", "a", "h1", "h2", "h3", "h4", "h5", "h6",
      "blockquote", "table", "thead", "tbody", "tr", "th", "td",
      "span", "div", "hr", "sup", "sub", "img",
    ],
    ALLOWED_ATTR: ["href", "target", "rel", "class", "src", "alt", "title"],
    ALLOW_DATA_ATTR: false,
  });
}

// Bug-7286 / Bug-7328: CSV / spreadsheet formula-injection guard.
//
// A cell value whose first character is one of these triggers is interpreted as
// a FORMULA (not text) when the file is opened in Excel / Google Sheets / LibreOffice.
// A source-derived value such as `=WEBSERVICE(...)`, `+cmd`, `-2+3`, `@SUM(...)`,
// or a leading tab / carriage-return / newline can therefore execute on the
// analyst's workstation. This mirrors the backend policy (`_csv_safe` in
// model-service `api/audit.py` / `api/logs.py`, added under Bug-6314) so every
// export producer — backend and frontend — neutralises the same trigger set.
//
// Neutralisation follows OWASP guidance: prefix a single quote so the spreadsheet
// treats the cell as literal text while the displayed value stays correct.
const CSV_FORMULA_TRIGGERS = ["=", "+", "-", "@", "\t", "\r", "\n"] as const;

/**
 * Neutralise spreadsheet formula injection in a single CSV / worksheet cell.
 *
 * Only the value channel is guarded — this does NOT perform RFC-4180 CSV quoting
 * (comma / quote / newline escaping). Call this FIRST, then apply CSV quoting on
 * top, so the guard prefix is inside any surrounding quotes.
 *
 * @param value the cell value; non-string input is coerced to string.
 * @returns the value, prefixed with `'` when it starts with a formula trigger.
 */
export function csvSafeCell(value: unknown): string {
  if (value === null || value === undefined) return "";
  const s = typeof value === "string" ? value : String(value);
  if (s.length > 0 && (CSV_FORMULA_TRIGGERS as readonly string[]).includes(s[0])) {
    return "'" + s;
  }
  return s;
}
