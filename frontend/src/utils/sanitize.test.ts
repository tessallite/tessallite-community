import { describe, it, expect } from "vitest";
import { sanitizeHtml, csvSafeCell } from "./sanitize";

describe("sanitizeHtml", () => {
  it("strips script tags", () => {
    expect(sanitizeHtml('<script>alert("xss")</script>')).toBe("");
  });

  it("strips event handlers", () => {
    const result = sanitizeHtml('<img src="x" onerror="alert(1)">');
    expect(result).not.toContain("onerror");
    expect(result).toContain("<img");
  });

  it("strips javascript: URLs", () => {
    const result = sanitizeHtml('<a href="javascript:alert(1)">click</a>');
    expect(result).not.toContain("javascript:");
  });

  it("strips onload handlers", () => {
    const result = sanitizeHtml('<img src="x" onload="alert(1)">');
    expect(result).not.toContain("onload");
  });

  it("preserves safe markdown HTML", () => {
    const input =
      "<h1>Title</h1><p>Text with <strong>bold</strong> and <em>italic</em></p>";
    expect(sanitizeHtml(input)).toBe(input);
  });

  it("preserves tables", () => {
    const input = "<table><thead><tr><th>A</th></tr></thead><tbody><tr><td>1</td></tr></tbody></table>";
    expect(sanitizeHtml(input)).toBe(input);
  });

  it("preserves code blocks", () => {
    const input = "<pre><code>const x = 1;</code></pre>";
    expect(sanitizeHtml(input)).toBe(input);
  });

  it("strips data attributes", () => {
    const result = sanitizeHtml('<div data-exploit="payload">text</div>');
    expect(result).not.toContain("data-exploit");
    expect(result).toContain("text");
  });

  it("strips style attribute", () => {
    const result = sanitizeHtml('<div style="background:url(evil)">text</div>');
    expect(result).not.toContain("style");
  });

  it("strips iframe", () => {
    expect(sanitizeHtml('<iframe src="evil.com"></iframe>')).toBe("");
  });
});

// Bug-7286 / Bug-7328: spreadsheet formula-injection guard.
describe("csvSafeCell", () => {
  it("neutralises every formula-leading trigger", () => {
    expect(csvSafeCell("=cmd|'/C calc'!A0")).toBe("'=cmd|'/C calc'!A0");
    expect(csvSafeCell("+cmd")).toBe("'+cmd");
    expect(csvSafeCell("-cmd")).toBe("'-cmd");
    expect(csvSafeCell("@SUM(1,1)")).toBe("'@SUM(1,1)");
    expect(csvSafeCell('=HYPERLINK("https://attacker.example","x")')).toBe(
      "'=HYPERLINK(\"https://attacker.example\",\"x\")",
    );
  });

  it("neutralises leading tab / carriage-return / newline control bytes", () => {
    expect(csvSafeCell("\t=cmd")).toBe("'\t=cmd");
    expect(csvSafeCell("\r=cmd")).toBe("'\r=cmd");
    expect(csvSafeCell("\n=cmd")).toBe("'\n=cmd");
  });

  it("leaves plain text and interior triggers untouched", () => {
    expect(csvSafeCell("EMEA")).toBe("EMEA");
    expect(csvSafeCell("Doe, John")).toBe("Doe, John");
    expect(csvSafeCell("a=b")).toBe("a=b");
  });

  it("coerces non-string input and treats null / undefined as empty", () => {
    expect(csvSafeCell(null)).toBe("");
    expect(csvSafeCell(undefined)).toBe("");
    expect(csvSafeCell(42)).toBe("42");
    expect(csvSafeCell("")).toBe("");
  });
});
