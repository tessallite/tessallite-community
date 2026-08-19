/**
 * The visibility-relevant subset of the agent config.  Adapters that only
 * fetch the visibility flags (e.g. the conversational-client proxy) return
 * this narrower type.  The full {@link AgentConfig} extends it, so callers
 * that fetch the complete config are still assignable.
 */
export interface AgentVisibilityConfig {
  enabled: boolean;
  show_thought_process: boolean;
  show_semantic_query: boolean;
  show_physical_query: boolean;
  feedback_enabled: boolean;
}

export interface AgentConfig extends AgentVisibilityConfig {
  id?: string;
  project_id?: string;
  display_name: string | null;
  project_brief: string | null;
  agent_role: string;
  tone_preset: "professional" | "friendly";
  tone_overrides: string | null;
  brand_guidelines: string | null;
  safety_policy: string | null;
  content_rules: string | null;
  default_locale: string | null;
  disclosure_text: string | null;
  webhook_url: string | null;
  primary_model_id: string | null;
  answer_llm_config_id: string | null;
  judge_llm_config_id: string | null;
  aggregate_llm_config_id: string | null;
  glossary_llm_config_id: string | null;
  judge_mode: "async" | "sync";
  judge_rubric_id: string | null;
  judge_block_visibility: "transparent" | "opaque";
  conversation_retention_days: number;
  enable_agent_log_screen: boolean;
  session_history_depth: number;
  daily_token_budget: number;
  daily_cost_budget_usd: number;
  max_query_complexity: number;
  agent_output_format: "json" | "plain" | "markup" | "html" | "rich_html";
  chart_type_selector: "none" | "llm" | "auto";
  chart_renderer: "echarts" | "html";
  chart_max_rows: number;
  chart_color_palette: "default" | "tessallite" | "muted" | "high_contrast" | "colorblind_safe";
  chart_size: "sm" | "md" | "lg";
  include_data_table: boolean;
  max_compound_steps: number;
}

export interface AgentPersona {
  id: string;
  name: string;
  description: string | null;
  system_prompt: string | null;
  persona_type: string;
  scope_filter: Record<string, unknown> | null;
}

export interface SelectableModel {
  id: string;
  name: string;
}
