import type { ConnectionStub, ExportBundle } from "../../api/importExportApi";

// Bug-6292: the on-disk model export file is the bundle plus the authoritative
// connection stub list. Older exports omit `connections_required`; those fall
// back to deriving stubs from the snapshot.
export type ModelExportFile = ExportBundle & {
  connections_required?: ConnectionStub[];
};

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

// Bug-6292: prefer the authoritative connection stub list embedded at export
// time. It carries the real ProjectConnection.connection_type, whereas the
// snapshot only records the source_type/target_type vocabulary
// (jdbc/tessallite_passthrough), which does not match a local connection's
// connection_type (postgresql/hadoop_spark/...) — so re-deriving from the
// snapshot makes the import dialog's compatibility filter reject every local
// connection and dead-end the rebind. Only fall back to snapshot derivation
// for older files that predate the embedded list.
export function stubsFromExportFile(file: ModelExportFile): ConnectionStub[] {
  const embedded = file.connections_required;
  if (Array.isArray(embedded)) {
    return embedded
      .filter((s) => s && typeof s.id === "string" && s.id.length > 0)
      .map((s) => ({
        id: s.id,
        role: s.role === "target" ? "target" : "source",
        display_name: s.display_name ?? null,
        connection_type: s.connection_type ?? null,
      }));
  }
  return extractStubsFromBundle(file);
}

export const SECTION_KEYS = [
  { key: "connections", i18nKey: "exportDialog.sectionConnections", defaultOn: true },
  { key: "llm_configs", i18nKey: "exportDialog.sectionLlmConfigs", defaultOn: true },
  { key: "agent_config", i18nKey: "exportDialog.sectionAgentConfig", defaultOn: true },
  { key: "cross_model_recipes", i18nKey: "exportDialog.sectionCrossModelRecipes", defaultOn: true },
  { key: "project_settings", i18nKey: "exportDialog.sectionProjectSettings", defaultOn: true },
  { key: "access_bindings", i18nKey: "exportDialog.sectionAccessBindings", defaultOn: false },
] as const;
