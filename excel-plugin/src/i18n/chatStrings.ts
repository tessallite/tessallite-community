import { translateChat } from "./runtime";

/**
 * Shared-UI i18n key map for the Excel plugin.
 *
 * The shared ChatCanvas and its sub-components call `t(key, params?)` via
 * ChatProvider context. This module provides a thin `t()` wrapper backed by
 * an English fallback map plus the runtime's localized shared-chat subset.
 * Keys mirror the platform's `en.json` grouping convention (domain.key).
 *
 * IMPORTANT: every key referenced in shared-ui/src/components must be present
 * here, and parameterized entries must use the SAME parameter names the
 * shared component passes. The canonical reference is
 * `frontend/src/i18n/en/shared-chat.json`. See Bug-6517.
 *
 * Parameterized entries are stored as plain interpolation templates
 * (`"{{count}} rows"`), identical in shape to the canonical shared-chat.json
 * and to the localized overrides in `runtime.ts`. `translateChat` performs the
 * `{{param}}` substitution uniformly. Entries MUST NOT re-format the incoming
 * params (no `Number(p.count).toLocaleString()` etc.): the shared-ui producers
 * already pass DISPLAY-READY, pre-grouped strings (e.g. MetadataBadges passes
 * `count: rows.toLocaleString()` -> "1,234"), so coercing them back through
 * `Number()` yielded `NaN` in the task pane while the web host — which does
 * plain interpolation — rendered correctly (deep-review finding: "NaN rows").
 * The plugin must mirror the canonical template verbatim so the two hosts agree.
 */

const STRINGS: Record<string, string> = {
  // Chat canvas
  "chat.chatAria": "Chat panel",
  "chat.collapseThought": "Collapse thought stream",
  "chat.conversationAria": "Conversation messages",
  "chat.createConversationFailed": "Could not create conversation. Please try again.",
  "chat.connectionError": "Connection error. Please try again.",
  "chat.emptyHint": "Ask a question to get started.",
  "chat.emptyTitle": "No messages yet",
  "chat.expandThought": "Expand thought stream",
  "chat.requestError": "Something went wrong. Please try again.",
  "chat.retry": "Retry",
  "chat.retryAria": "Retry sending message",
  "chat.scopeMismatch": "That conversation was created under a different model or persona. Starting a new conversation with your current settings.",
  "chat.thinking": "Thinking...",
  "chat.thinkingAria": "Agent is thinking",
  "chat.thoughtStreaming": "Thought stream is available",
  "chat.titleSaveFailed": "Could not save conversation title.",

  // Stream errors (Bug-8370) — typed StreamErrorCode -> friendly message
  "stream.error.httpError": "The server rejected the request. Please try again.",
  "stream.error.timeout": "The response took too long and timed out. Please try again.",
  "stream.error.unexpectedEnd": "The response ended unexpectedly. Please check the conversation or try again.",
  "stream.error.connectionLost": "Connection was lost after the response started. Please check the conversation before resending.",
  "stream.error.networkError": "A connection problem prevented the response from completing. Please try again.",

  // Composer
  "composer.placeholder": "Ask a question about your data...",

  // DataTableBlock (Bug-7546: these keys were missing, causing raw key display)
  "dataTable.copied": "Copied",
  "dataTable.copy": "Copy table as TSV",
  "dataTable.exceedsLimit": "Results exceed the display limit ({{count}} rows). Try refining your query.",
  "dataTable.no": "No",
  "dataTable.yes": "Yes",
  "composer.messageInputAria": "Message input",
  "composer.sendAria": "Send message",
  "composer.stopAria": "Stop generating",

  // Turn
  "turn.blockedQuality": "This response was blocked by the quality review",
  "turn.blockedSafeDetail": "The original answer is hidden because it did not pass review. You can rephrase the question, retry with the review note, or inspect safe trace details if they are enabled.",
  "turn.calcStepDescription": "Step",
  "turn.calcStepValue": "Value",
  "turn.diagnostics": "Diagnostics",
  "turn.errorFallback": "An error occurred while generating this response.",
  "turn.hideData": "Hide data",
  "turn.physicalQuery": "Physical query",
  "turn.refused": "This request was refused",
  "turn.rawRecordsNotSupported": "Raw transaction records are not supported by the aggregate query tools. Ask for a count, total, or grouped summary instead.",
  "turn.refusedSafeDetail": "The request was refused by the configured guardrails.",
  "turn.rephrase": "Rephrase",
  "turn.rephraseHint": "Try rephrasing your question.",
  "turn.semanticQuery": "Semantic query",
  "turn.supportingDetails": "Supporting details",
  "turn.thinking": "Thinking",
  "turn.viewTrace": "View trace",
  "turn.visual": "Visual",

  // Badges
  "badges.completed": "Completed",
  "badges.blocked": "Blocked",

  // Citations
  "citations.label": "Citations",
  "citations.source": "Source",
  "citations.measure": "Measure",
  "citations.dimension": "Dimension",
  "citations.provenance.title": "How this number was calculated",
  "citations.provenance.definitionLabel": "Definition",
  "citations.provenance.noDefinition": "No definition recorded for this field.",
  "citations.provenance.valueLabel": "Value",
  "citations.provenance.filterGrainLabel": "Filters and grouping applied",
  "citations.provenance.noFilterGrain": "No filters. This is the unfiltered total.",

  // Chart
  "chart.ariaDescription": "Chart of the query result.",
  "chart.distribution": "Distribution",
  "chart.exportTitle": "Save",
  "chart.result": "Result",
  "chart.trend": "Trend",

  // Judge
  "judge.evaluating": "Reviewing answer...",
  "judge.approves": "Review passed",
  "judge.concerned": "Review warning",
  "judge.didNotApprove": "Review blocked this answer",
  "judge.unavailable": "Review unavailable",
  "judge.sendBackToCorrect": "Retry with review note",
  "judge.correctPreviousNoReason": "Please correct the previous answer and answer again without using the blocked wording.",
  "judge.correctPreviousWithReason": "Please correct the previous answer using this review note:",

  // Model picker
  "modelPicker.tooltip": "Select model",
  "modelPicker.ariaLabel": "Select model",
  "modelPicker.projectDefault": "Project default",
  "modelPicker.projectDefaultHint": "Uses the project's configured primary model",
  "modelPicker.sectionLabel": "Available models",
  "modelPicker.none": "No models available",
  "modelPicker.updateFailed": "Could not update model selection.",

  // Rendered output
  "renderedOutput.closeAria": "Close",
  "renderedOutput.dialogTitle": "Output",
  "renderedOutput.iframeTitle": "Rendered answer output",
  "renderedOutput.openLargerAria": "Open larger view",
  "renderedOutput.unsafeFallback": "The visual output could not be displayed safely.",

  // Trace
  "trace.answerTrace": "Answer trace",
  "trace.howIThought": "How I thought about this",
  "trace.semanticQuery": "Semantic query",
  "trace.physicalQuery": "Physical query",

  // Parameterized entries — plain `{{param}}` interpolation templates, mirroring
  // the canonical shared-chat.json verbatim. `translateChat` substitutes the
  // params (as-is, no re-formatting — see the file header). The param NAMES here
  // are the ones the shared-ui producers pass to `t()` (e.g. InlineStepCard
  // passes `{ n }`, MetadataBadges passes `{ count }`).
  "badges.judge": "Judge: {{verdict}}",
  "badges.route": "Route: {{route}}",
  "badges.rows": "{{count}} rows",
  "badges.seconds": "{{seconds}}s",
  "chart.ariaLabel": "{{title}} chart. Expand the data table below for the underlying values.",
  "chart.truncated": "Showing first {{count}} of {{total}} rows",
  "composer.tooLong": "Message exceeds {{max}} character limit",
  "steps.header": "Steps ({{n}})",
  "steps.step": "Step {{n}}",
  "steps.rows": "{{count}} rows",
  "trace.latencyMs": "{{ms}}ms",
  "trace.routeWithValue": "Route: {{route}}",
  "trace.physicalQueryRoute": "Physical query ({{route}})",
  "trace.toolWithValue": "Tool: {{tool}}",
  "turn.calculationSteps": "Calculation steps ({{count}})",
  "turn.calcStepFormula": "Formula: {{formula}}",
  "turn.showData": "Show data ({{count}} rows)",
  "turn.answeredBy": "Answered by {{provider}}",
};

/**
 * All shared-chat keys this plugin resolves. Exported so the parity test can
 * assert (structurally, against the canonical shared-chat.json) that every
 * canonical key is covered and correctly classified — replacing the former
 * hand-maintained key list that silently drifted (deep-review finding).
 */
export const CHAT_STRING_KEYS: ReadonlySet<string> = new Set(Object.keys(STRINGS));

export function chatT(key: string, params?: Record<string, string | number>): string {
  const english = STRINGS[key] ?? key;
  return translateChat(key, english, params);
}
