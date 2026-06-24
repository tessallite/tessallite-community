export function downloadText(filename: string, mime: string, body: string): void {
  const utf8Bom = mime.startsWith("text/csv") ? "﻿" : "";
  const blob = new Blob([utf8Bom + body], { type: `${mime};charset=utf-8` });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

export function safeFilename(base: string): string {
  const clean = base.replace(/[^A-Za-z0-9._-]+/g, "_").replace(/^_+|_+$/g, "");
  return clean.length > 0 ? clean : "export";
}
