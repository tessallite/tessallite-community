---
title: "Agent Conversation Export"
audience: all
area: agent
updated: 2026-05-24
---

## What this covers

You can export any agent conversation as **Markdown** or **PDF** for sharing, archiving, or compliance. This page explains the export options and what the output contains.

---

## How to export

1. Open an **Agent Chat** conversation.
2. Click the **Export** button in the conversation toolbar.
3. Choose **Markdown** or **PDF**.

### Markdown export

Downloads a `.md` file containing every turn in the conversation: user messages, assistant responses, embedded queries, result tables, and chart references. The file is ready for pasting into wikis, documentation systems, or version control.

### PDF export

Opens a browser print dialog with a styled, print-ready version of the conversation. You can save as PDF from the print dialog or send directly to a printer. The PDF includes the same content as the Markdown export but with formatted tables and headings.

---

## What is included

| Content | Included |
|---|---|
| User messages | Yes |
| Assistant responses | Yes |
| SQL queries | Yes (fenced code blocks in Markdown) |
| Query results (tables) | Yes |
| Chart references | Yes (described by type and data summary) |
| Citations and sources | Yes |
| Judge verdicts | Yes, if present |
| Timestamps | Yes, per turn |

---

## Notes

- Export captures the conversation as it exists at the time you click Export. Subsequent turns are not retroactively added.
- Special characters (`"`, `'`, `<`, `>`, `&`) are properly escaped in both formats.
- PDF rendering uses `window.onload` to ensure styles are fully applied before the print dialog opens.

---

## Related

- [Agent Chat](agent-chat.md)
- [Agent Log Screen](agent-log-screen.md)
- [Agent Session Memory](session-memory.md)

---

<- [Agent Session Memory](session-memory.md) | [Home](../index.md) | [Agent Stop Button ->](agent-stop-button.md)
