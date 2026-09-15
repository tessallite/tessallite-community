# Excel plugin task pane: structure and behaviour document

Status: draft, 2026-09-06. Companion to `excel-plugin-ui.openui.json`.
Format: Tessallite-UI-Specs v0.1 (an OpenUI extension), see
https://github.com/M-O-Othman/Tessallite-UI-Specs.

## What the document is

`excel-plugin-ui.openui.json` describes the task pane as a single-parent
tree of typed nodes: what contains what, what each element shows, what
happens on every handler wired in the code, and which states each part can
take. It carries no layout or visual information.

Sources of truth: `src/App.tsx`, `src/components/**`, the shared chat
components under `tessallite/shared-ui/src/components`, the generated
declarations under `build/ts` (props) and `src/i18n/strings.ts` plus
`src/i18n/chatStrings.ts` (labels; the `i18n` field carries the key,
chat keys are prefixed `chatStrings:`).

## Layout of the document

- `components`: every plugin and shared-ui component with its real props and
  callback events. Reused shapes (the five library cards, the chat
  sub-components, StatusBadge, SearchBar, EmptyState, InsertActions) carry a
  `structure`; instances in the trees reference them with `$ref`.
- `structures.task-pane`: the authenticated shell. Header, project bar, tab
  strip, offline and viewing-as banners, the `main` region with the three
  tab panels (each a `$ref` to its screen structure), footer, toast stack and
  confirm dialog. Overlays sit under the control that opens them: the Drill
  Through panel under the drill button, the Glossary under its button, the
  Diagnostics dialog under the settings menu item, the Remove Profile dialog
  under the profile entry's remove button.
- `structures.login-screen`: the sign-in form.
- `structures.report-builder`: zones with chips and the filter editor,
  insert-mode chips and tool buttons (Refresh, Refresh sheet data, CUBE
  wizard, Connect wizard, Trace), the sort row, the field search, and the
  five libraries at card level down to every icon button.
- `structures.kpi-panel`: header, status chips, filter bar and the grouped
  KPI rows.
- `structures.ask-tessallite`: ExcelChatShell over the shared ChatCanvas,
  with the conversation header and per-turn insert actions as slot fillers;
  the answered turn is `components.AssistantTurn.structure`.

## Reading it

```sh
tuis validate excel-plugin-ui.openui.json
tuis query excel-plugin-ui.openui.json structures
tuis query excel-plugin-ui.openui.json events-of tab-kpi
tuis query excel-plugin-ui.openui.json find --event "App.handleOpenDrill"
tuis query excel-plugin-ui.openui.json path-to at-calc-value-text
```

Or serve it to an agent: `tuis-mcp excel-plugin-ui.openui.json`.

## Conventions used here

- `handler` records the callback chain when a leaf's handler is a prop
  passed through: `AppHeader.onOpenDrill -> App.handleOpenDrill`.
- `condition` says when an element exists (`profiles.length > 0`); states
  with `present` cover whole alternative renderings (loading, empty, error).
- Text that comes from data is written in braces: `{kpi.display_name}`.
- Icon URIs are placeholders named after the MUI icon (`icons/settings.svg`);
  the files are not part of this document.
- Items not mounted in the Excel host but defined by shared-ui (TraceStrip,
  the unwired InsertActions buttons, suggested questions) are included with
  a condition that says so.

## Known limits

- MUI internals (Select popups, TablePagination) are modelled at the level
  the code names them, not as MUI's own DOM.
- Toast messages are one repeated toast; the individual message strings are
  in `strings.toasts` and are not enumerated as nodes.
