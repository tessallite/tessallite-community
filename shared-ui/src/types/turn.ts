export interface Citation {
  kind: "measure" | "dimension";
  id: string;
  name: string;
  display_name: string;
  value: number | string | null;
  // Bug-8181 (checkable citations, L3) — provenance so a citation chip can be
  // verified, not just displayed: the field's business definition (falls
  // back to its formula when no description was authored), the route that
  // served the value (aggregate | pocket | source), and a human-readable
  // summary of the WHERE filters / GROUP BY grain that produced it. Field
  // names match the backend citation dict exactly
  // (services/agent-service/src/citations/builder.py).
  definition: string | null;
  route_type: string | null;
  filter_grain: string | null;
}

export interface CalculationStep {
  step: number;
  step_number?: number;
  description?: string;
  name?: string;
  measure?: string | null;
  value?: unknown;
  formatted_value?: string;
  formula?: string;
  sql?: string;
  result_preview?: string;
}

export interface TurnResponse {
  id: string;
  conversation_id: string;
  turn_index: number;
  user_message: string;
  answer_text: string | null;
  status: string;
  latency_ms: number | null;
  thought_summary: string | null;
  semantic_query: unknown | null;
  routed_sql: string | null;
  route: string | null;
  citations: Citation[] | null;
  user_feedback: { vote: string; comment: string | null } | null;
  judge_verdict: string | null;
  judge_reasoning: string | null;
  judge_metrics: Record<string, number> | null;
  guardrail_actions: unknown[] | null;
  usage_input_tokens: number | null;
  usage_output_tokens: number | null;
  rendered_output: string | null;
  llm_plan: unknown | null;
  query_result_rows: number | null;
  query_result_sample: Record<string, unknown>[] | null;
  calculation_steps: CalculationStep[] | null;
  chart_type: string | null;
  provider: string | null;
  judge_pending: boolean | undefined;
}
