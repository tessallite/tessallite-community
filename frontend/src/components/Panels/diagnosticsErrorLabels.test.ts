// Bug-7106: known backend-authored error sentences must resolve through i18n
// keys; anything unmappable (parameterised / raw executor prose) stays
// verbatim rather than being silently rewritten.
import { describe, it, expect } from "vitest";
import { localizeBackendError } from "./diagnosticsErrorLabels";

const t = (key: string): string => `‹${key}›`;

describe("localizeBackendError (Bug-7106)", () => {
  it("maps the known fixed AI-optimiser error sentences to keys", () => {
    expect(
      localizeBackendError(
        "LLM returned an empty response. Check the API key, model name, token limit, and provider logs.",
        t,
      ),
    ).toBe("‹diagnostics.backendError.llmEmptyResponse›");
    expect(
      localizeBackendError(
        "Model has no data target configured. Assign a target before running the AI optimiser.",
        t,
      ),
    ).toBe("‹diagnostics.backendError.noDataTarget›");
    expect(
      localizeBackendError(
        "Model has no deployed version. Deploy the model before running the AI optimiser so recommendations match the served version.",
        t,
      ),
    ).toBe("‹diagnostics.backendError.modelNotDeployed›");
  });

  it("passes unmappable prose through verbatim", () => {
    const raw = 'relation "public.orders" does not exist';
    expect(localizeBackendError(raw, t)).toBe(raw);
  });

  it("handles null/undefined/empty without crashing", () => {
    expect(localizeBackendError(null, t)).toBeNull();
    expect(localizeBackendError(undefined, t)).toBeNull();
    expect(localizeBackendError("", t)).toBe("");
  });
});
