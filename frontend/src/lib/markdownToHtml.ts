/**
 * Minimal markdown → HTML converter for narration output.
 * No external dependencies. Handles the patterns the narration LLM produces
 * for markup/html/rich_html output formats: headers, bold, italic, lists,
 * and paragraphs. Input is HTML-escaped before pattern matching.
 */

export function looksLikeMarkdown(text: string): boolean {
  return /\*\*|^#{1,6}\s|^[-*]\s/m.test(text);
}

export function markdownToHtml(text: string): string {
  // Escape HTML entities to prevent injection from LLM output.
  let out = text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");

  // Headers (must be at start of line).
  out = out.replace(/^######\s+(.+)$/gm, "<h6>$1</h6>");
  out = out.replace(/^#####\s+(.+)$/gm, "<h5>$1</h5>");
  out = out.replace(/^####\s+(.+)$/gm, "<h4>$1</h4>");
  out = out.replace(/^###\s+(.+)$/gm, "<h3>$1</h3>");
  out = out.replace(/^##\s+(.+)$/gm, "<h2>$1</h2>");
  out = out.replace(/^#\s+(.+)$/gm, "<h1>$1</h1>");

  // Bold + italic together must come before bold-only and italic-only.
  out = out.replace(/\*\*\*(.+?)\*\*\*/g, "<strong><em>$1</em></strong>");
  out = out.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  out = out.replace(/\*(.+?)\*/g, "<em>$1</em>");

  // Unordered list items (lines starting with - or *).
  out = out.replace(/^[-*]\s+(.+)$/gm, "<li>$1</li>");
  // Wrap consecutive <li> blocks in <ul>.
  out = out.replace(/(<li>[\s\S]*?<\/li>\n?)+/g, (match) => `<ul>${match}</ul>`);

  // Paragraphs: split on blank lines, wrap non-block content.
  const blocks = out.split(/\n{2,}/);
  out = blocks
    .map((block) => {
      const trimmed = block.trim();
      if (!trimmed) return "";
      if (/^<(h[1-6]|ul|ol|li|table|thead|tbody|tr|th|td|p)[\s>]/.test(trimmed)) {
        return trimmed;
      }
      return `<p>${trimmed.replace(/\n/g, "<br>")}</p>`;
    })
    .filter(Boolean)
    .join("\n");

  return out;
}
