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
