/**
 * Shared-UI i18n key map for the Excel plugin.
 *
 * The shared ChatCanvas and its sub-components call `t(key, params?)` via
 * ChatProvider context. This module provides a thin `t()` wrapper backed by
 * a flat English-only string map. Keys mirror the platform's `en.json`
 * grouping convention (domain.key).
 */

const STRINGS: Record<string, string> = {
  // Chat canvas
  "chat.chatAria": "Chat",
  "chat.conversationAria": "Conversation messages",
  "chat.createConversationFailed": "Failed to create conversation. Please try again.",
  "chat.titleSaveFailed": "Conversation was created, but the title could not be saved.",
  "chat.requestError": "An error occurred while processing your request.",
  "chat.connectionError": "Connection error. Please try again.",
  "chat.scopeMismatch": "That conversation was created under a different model or persona. Starting a new conversation with your current settings.",
  "chat.retry": "Retry",
  "chat.retryAria": "Retry sending message",
  "chat.thinking": "Thinking...",
  "chat.thinkingAria": "Assistant is thinking",
  "chat.emptyTitle": "Ask Tessallite anything",
  "chat.emptyHint": 'Try: "What was revenue last quarter?"',

  // Composer
  "composer.placeholder": "Ask a question about your data...",
  "composer.messageInputAria": "Message input",
  "composer.sendAria": "Send message",
  "composer.stopAria": "Stop generating",

  // Turn
  "turn.blockedQuality": "Response Blocked for Quality Assurance",
  "turn.refused": "I can't answer that request",
  "turn.rephraseHint": "Try rephrasing: name a specific metric and time window.",
  "turn.rephrase": "Rephrase",
  "turn.errorFallback": "An error occurred while processing this request.",
  "turn.hideData": "Hide data",
  "turn.thinking": "Thinking",
  "turn.semanticQuery": "Semantic query",
  "turn.physicalQuery": "Physical query",
  "turn.calcStepDescription": "Description",
  "turn.calcStepValue": "Value",

  // Badges
  "badges.completed": "Completed",
  "badges.blocked": "Blocked",

  // Citations
  "citations.label": "Citations",
  "citations.source": "Source",
  "citations.measure": "Measure",
  "citations.dimension": "Dimension",

  // Judge
  "judge.evaluating": "Evaluating response quality...",
  "judge.approves": "Quality check approved this response.",
  "judge.concerned": "Quality check has concerns about this response.",
  "judge.didNotApprove": "Quality check did not approve this response.",
  "judge.unavailable": "Quality check unavailable.",
  "judge.sendBackToCorrect": "Correct previous answer",
  "judge.correctPreviousNoReason": "The previous answer has been corrected.",
  "judge.correctPreviousWithReason": "The previous answer has been corrected.",

  // Model picker
  "modelPicker.tooltip": "Choose a model",
  "modelPicker.ariaLabel": "Choose a model for this conversation",
  "modelPicker.projectDefault": "Project default",
  "modelPicker.projectDefaultHint": "Use all models configured for this project",
  "modelPicker.sectionLabel": "Restrict to one model",
  "modelPicker.none": "No models available",
  "modelPicker.updateFailed": "Could not change the model. Please try again.",

  // Trace
  "trace.answerTrace": "Answer trace",
  "trace.howIThought": "How I thought about this",
  "trace.semanticQuery": "Semantic query",
  "trace.physicalQuery": "Physical query",
};

const PARAM_STRINGS: Record<string, (params: Record<string, string | number>) => string> = {
  "badges.judge": (p) => `Judge: ${p.verdict}`,
  "badges.route": (p) => `Route: ${p.route}`,
  "badges.rows": (p) => `${Number(p.count).toLocaleString()} rows`,
  "badges.seconds": (p) => `${Number(p.seconds).toFixed(1)}s`,
  "chart.truncated": (p) => `Showing first ${Number(p.shown).toLocaleString()} of ${Number(p.total).toLocaleString()} points`,
  "composer.tooLong": (p) => `Message is too long. Maximum is ${Number(p.max).toLocaleString()} characters.`,
  "steps.header": (p) => `Steps (${p.count})`,
  "steps.step": (p) => `Step ${p.number}`,
  "steps.rows": (p) => `${Number(p.count).toLocaleString()} rows`,
  "trace.latencyMs": (p) => `${Number(p.ms).toFixed(0)}ms`,
  "trace.routeWithValue": (p) => `Route: ${p.value}`,
  "trace.physicalQueryRoute": (p) => `Physical query (${p.route})`,
  "trace.toolWithValue": (p) => `Tool: ${p.value}`,
  "turn.calculationSteps": (p) => `Calculation steps (${p.count})`,
  "turn.calcStepFormula": (p) => `Formula: ${p.formula}`,
  "turn.showData": (p) => `Show data (${Number(p.count).toLocaleString()} rows)`,
  "turn.answeredBy": (p) => `Answered by ${p.provider}`,
};

export function chatT(key: string, params?: Record<string, string | number>): string {
  if (params) {
    const fn = PARAM_STRINGS[key];
    if (fn) return fn(params);
  }
  return STRINGS[key] ?? key;
}
