/**
 * Workbook XMLA-connection helpers for the CUBE wizard.
 *
 * F-025-18: the Excel JS API exposes no `workbook.connections` collection in
 * any released requirement set, so the previous detection (and the
 * create/remove helpers built on it) could never work — detection always
 * returned empty, which surfaced a permanent, misleading "No connection
 * detected" warning even after the user had created the connection manually.
 * The phantom-API code and the unused create/remove helpers (F-025-21) have
 * been removed. Connection setup is documented-manual (see LiveConnectionWizard),
 * and the wizard now shows a neutral reminder rather than a false negative.
 */
import { useCallback } from 'react';

export interface ExcelConnectionInfo {
  id: string;
  name: string;
  type: string;
  description: string;
}

interface UseExcelConnectionsReturn {
  /**
   * Whether a Tessallite workbook connection is known to exist. Office.js
   * cannot enumerate workbook connections, so this is always `null`
   * ("unknown") — callers must treat it as "cannot confirm" and never as a
   * hard "missing" signal.
   */
  connectionStatus: 'unknown';
  /** No-op retained for call-site compatibility; there is nothing to refresh. */
  refreshConnections: () => Promise<void>;
}

export function useExcelConnections(): UseExcelConnectionsReturn {
  const refreshConnections = useCallback(async () => {
    // Intentionally a no-op: Office.js provides no workbook-connections API.
  }, []);

  return {
    connectionStatus: 'unknown',
    refreshConnections,
  };
}
