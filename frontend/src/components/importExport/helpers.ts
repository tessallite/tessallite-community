import type { ConnectionStub, ExportBundle } from "../../api/importExportApi";

export function readFileAsText(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onerror = () => reject(r.error);
    r.onload = () => resolve(String(r.result || ""));
    r.readAsText(file);
  });
}

export function extractStubsFromBundle(bundle: ExportBundle): ConnectionStub[] {
  const out: ConnectionStub[] = [];
  const seen = new Set<string>();
  const sources =
    (bundle.snapshot?.data_sources as Array<Record<string, unknown>>) || [];
  for (const s of sources) {
    const id = String(s.project_connection_id || "");
    if (id && !seen.has(id)) {
      seen.add(id);
      out.push({
        id,
        role: "source",
        display_name:
          typeof s.display_name === "string" ? s.display_name : null,
        connection_type:
          typeof s.source_type === "string" ? s.source_type : null,
      });
    }
  }
  const targets =
    (bundle.snapshot?.data_targets as Array<Record<string, unknown>>) || [];
  for (const t of targets) {
    const id = String(t.project_connection_id || "");
    if (id && !seen.has(id)) {
      seen.add(id);
      out.push({
        id,
        role: "target",
        display_name:
          typeof t.display_name === "string" ? t.display_name : null,
        connection_type:
          typeof t.target_type === "string" ? t.target_type : null,
      });
    }
  }
  return out;
}

export const SECTION_KEYS = [
  { key: "connections", i18nKey: "exportDialog.sectionConnections", defaultOn: true },
  { key: "llm_configs", i18nKey: "exportDialog.sectionLlmConfigs", defaultOn: true },
  { key: "agent_config", i18nKey: "exportDialog.sectionAgentConfig", defaultOn: true },
  { key: "cross_model_recipes", i18nKey: "exportDialog.sectionCrossModelRecipes", defaultOn: true },
  { key: "project_settings", i18nKey: "exportDialog.sectionProjectSettings", defaultOn: true },
  { key: "access_bindings", i18nKey: "exportDialog.sectionAccessBindings", defaultOn: false },
] as const;
