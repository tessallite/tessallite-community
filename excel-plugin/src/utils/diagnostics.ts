/**
 * Client-side diagnostics.
 * Keeps last 100 events for support copy/paste.
 * Redacts passwords, JWTs, and connection strings.
 */

interface DiagnosticsEvent {
  timestamp: string;
  type: 'api' | 'excel' | 'error' | 'info';
  route?: string;
  status?: number;
  durationMs?: number;
  message?: string;
  excelOperation?: string;
  serverUrl?: string;
  tenantId?: string;
}

const MAX_EVENTS = 100;
const events: DiagnosticsEvent[] = [];

export function logApiEvent(
  route: string,
  status: number,
  durationMs: number,
): void {
  events.push({
    timestamp: new Date().toISOString(),
    type: 'api',
    route: redactString(redactUrl(route)),
    status,
    durationMs,
  });
  trimEvents();
}

export function logExcelEvent(operation: string, error?: string): void {
  events.push({
    timestamp: new Date().toISOString(),
    type: 'excel',
    excelOperation: operation,
    message: error,
  });
  trimEvents();
}

export function logError(message: string): void {
  events.push({
    timestamp: new Date().toISOString(),
    type: 'error',
    message: redactString(message),
  });
  trimEvents();
}

export function logInfo(message: string): void {
  events.push({
    timestamp: new Date().toISOString(),
    type: 'info',
    message,
  });
  trimEvents();
}

export function getDiagnosticsReport(): string {
  const lines = [
    '=== Tessallite Excel Plugin Diagnostics ===',
    `Date: ${new Date().toISOString()}`,
    `Events: ${events.length}`,
    '',
  ];

  for (const e of events) {
    lines.push(
      `[${e.timestamp}] ${e.type.toUpperCase()}` +
      (e.route ? ` ${e.route}` : '') +
      (e.status ? ` -> ${e.status}` : '') +
      (e.durationMs ? ` (${e.durationMs}ms)` : '') +
      (e.excelOperation ? ` | Excel: ${e.excelOperation}` : '') +
      (e.message ? ` | ${e.message}` : ''),
    );
  }

  lines.push('', '=== End of Diagnostics ===');
  return lines.join('\n');
}

export function copyDiagnostics(): void {
  const report = getDiagnosticsReport();
  navigator.clipboard.writeText(report).catch(() => {});
}

export function clearDiagnostics(): void {
  events.length = 0;
}

function trimEvents(): void {
  while (events.length > MAX_EVENTS) {
    events.shift();
  }
}

function redactUrl(url: string): string {
  // Redact all path segments after /api/v1/ (handles arbitrary depth)
  return url.replace(/\/api\/v1\/[^\s"]+/g, '/api/v1/[REDACTED]');
}

function redactString(str: string): string {
  return str
    // Bearer tokens
    .replace(/Bearer\s+[\w.-]+/gi, 'Bearer [REDACTED]')
    // Connection string credentials (MSOLAP, OLEDB)
    .replace(/Password=[^;]+/gi, 'Password=[REDACTED]')
    .replace(/User ID=[^;]+/gi, 'User ID=[REDACTED]')
    .replace(/Persist Security Info=[^;]+/gi, 'Persist Security Info=[REDACTED]')
    // JSON credential fields
    .replace(/"password"\s*:\s*"[^"]*"/gi, '"password":"[REDACTED]"')
    .replace(/"access_token"\s*:\s*"[^"]*"/gi, '"access_token":"[REDACTED]"')
    .replace(/"refresh_token"\s*:\s*"[^"]*"/gi, '"refresh_token":"[REDACTED]"')
    .replace(/"token"\s*:\s*"[^"]*"/gi, '"token":"[REDACTED]"')
    .replace(/"secret"\s*:\s*"[^"]*"/gi, '"secret":"[REDACTED]"')
    .replace(/"client_secret"\s*:\s*"[^"]*"/gi, '"client_secret":"[REDACTED]"')
    .replace(/"api_key"\s*:\s*"[^"]*"/gi, '"api_key":"[REDACTED]"')
    .replace(/"connectionString"\s*:\s*"[^"]*"/gi, '"connectionString":"[REDACTED]"')
    .replace(/"Authorization"\s*:\s*"[^"]*"/gi, '"Authorization":"[REDACTED]"')
    // Simulate-principal header
    .replace(/X-Tessallite-Simulate-Principal[:\s]+[^\s"]+/gi, 'X-Tessallite-Simulate-Principal: [REDACTED]')
    // Auth API paths
    .replace(/\/api\/v1\/auth\/[^?\s"]+/g, '/api/v1/auth/[REDACTED]');
}
