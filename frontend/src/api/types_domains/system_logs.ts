export interface SystemLog {
  id: string;
  timestamp: string;
  service: string;
  level: string;
  logger: string;
  instance: string;
  message: string;
}

export interface SystemLogPage {
  items: SystemLog[];
  next_cursor: string | null;
}

export interface SystemLogSettings {
  enabled: boolean;
  retention_days: number;
  cutoff: string | null;
  levels: string[];
  services: string[];
  poll_seconds: number;
  page_size: number;
}

export interface SystemLogFilters {
  q?: string;
  service?: string;
  level?: string;
  from_date?: string;
  to_date?: string;
  cursor?: string;
  limit?: number;
}

export interface SystemLogPurgeResponse {
  deleted: number;
  cutoff: string | null;
}
