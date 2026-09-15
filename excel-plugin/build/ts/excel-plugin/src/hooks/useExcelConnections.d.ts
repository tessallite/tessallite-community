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
export declare function useExcelConnections(): UseExcelConnectionsReturn;
export {};
