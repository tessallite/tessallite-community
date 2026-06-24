/**
 * Gateway API client for XMLA gateway endpoints.
 * The plugin does not send XMLA queries through this client.
 * XMLA traffic flows through Excel's MSOLAP provider directly to the gateway.
 */
import { apiClient } from './client';

export async function healthCheck(): Promise<{ status: string }> {
  return apiClient.get('/health');
}

export async function gatewayVersion(): Promise<{ version: string }> {
  return apiClient.get('/api/v1/gateway/version');
}
