"""Bounded labels for stored runtime logs and collector health."""

from prometheus_client import Counter, Gauge
from shared.system_logs.config import CONFIG

LOG_EVENTS = Counter(
    "tessallite_system_log_events_total", "Raw log records stored", ["service", "level"]
)
LOG_FAILURES = Counter(
    "tessallite_system_log_write_failures_total", "Failed ingestion batches"
)
LOG_TRUNCATED = Counter(
    "tessallite_system_log_truncated_reads_total",
    "Docker reads that exceeded collector bounds; older output may be missing",
)
LOG_HEARTBEAT = Gauge(
    "tessallite_system_log_collector_last_seen_seconds",
    "Last successful collector batch or heartbeat",
)
LOG_BYTES = Gauge(
    "tessallite_system_log_database_bytes", "Bytes used by the log table and indexes"
)
LOG_ROWS = Gauge(
    "tessallite_system_log_estimated_rows", "PostgreSQL estimate of retained log rows"
)
LOG_STORAGE_MEASURED_AT = Gauge(
    "tessallite_system_log_storage_last_measured_timestamp_seconds",
    "Unix timestamp of the last successful raw-log storage observation",
)
LOG_ENABLED = Gauge(
    "tessallite_system_logs_enabled", "Whether raw log ingestion is enabled"
)
LOG_LAST_ERROR = Gauge(
    "tessallite_system_log_last_error_timestamp_seconds",
    "Timestamp of the latest stored ERROR or CRITICAL record",
)
for _service in CONFIG["services"]:
    for _level in CONFIG["levels"]:
        LOG_EVENTS.labels(_service, _level)

_latest_error_timestamp = 0.0
LOG_PURGED = Counter(
    "tessallite_system_log_purged_total", "Expired raw log records removed"
)


def record_error_timestamp(value: float) -> None:
    global _latest_error_timestamp
    _latest_error_timestamp = max(_latest_error_timestamp, value)
    LOG_LAST_ERROR.set(_latest_error_timestamp)
