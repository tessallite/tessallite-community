// Bug-7106: backend services compose English-only error prose (AI-optimiser
// runner messages, query-log error details) and the diagnostics panel rendered
// it verbatim. The known FIXED sentences are mapped here to i18n keys; anything
// not in the map (parameterised messages, raw database/executor prose) renders
// verbatim — that open-ended remainder is REGISTERed as unmappable in the
// frontend, not silently dropped (see lane handoff).

type TFn = (key: string) => string;

const BACKEND_ERROR_LABEL_KEYS: Record<string, string> = {
  // tessallite/services/optimizer/src/ai/runner.py
  "LLM returned an empty response. Check the API key, model name, token limit, and provider logs.":
    "diagnostics.backendError.llmEmptyResponse",
  "Model has no data target configured. Assign a target before running the AI optimiser.":
    "diagnostics.backendError.noDataTarget",
  "Model has no deployed version. Deploy the model before running the AI optimiser so recommendations match the served version.":
    "diagnostics.backendError.modelNotDeployed",
};

/**
 * Translate a KNOWN backend-authored error sentence; pass everything else
 * through verbatim (custom prose / parameterised messages stay as authored).
 */
export function localizeBackendError(text: string | null | undefined, t: TFn): string | null {
  if (!text) return text ?? null;
  const key = BACKEND_ERROR_LABEL_KEYS[text];
  if (!key) return text;
  const translated = t(key);
  return translated && translated !== key ? translated : text;
}
