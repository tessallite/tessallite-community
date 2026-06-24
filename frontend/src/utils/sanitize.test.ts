import { describe, it, expect } from "vitest";
import { sanitizeHtml } from "./sanitize";

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
