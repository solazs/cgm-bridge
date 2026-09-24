"""Prometheus metrics for the service itself (not the glucose values)."""

from prometheus_client import Counter, Gauge, Histogram

REQUESTS = Counter(
    "cgm_bridge_http_requests_total", "HTTP requests handled", ["route", "method", "code"]
)
REQUEST_SECONDS = Histogram(
    "cgm_bridge_http_request_duration_seconds", "HTTP request duration", ["route"]
)
DOCUMENTS = Counter(
    "cgm_bridge_documents_total",
    "Uploaded documents by kind and outcome (inserted, duplicate, skipped, upserted, deleted)",
    ["kind", "outcome"],
)
LATEST_READING = Gauge(
    "cgm_bridge_latest_reading_timestamp_seconds",
    "Unix time of the newest stored glucose reading",
)
EXPORT_SAMPLES = Counter(
    "cgm_bridge_export_samples_total", "Glucose samples pushed to VictoriaMetrics"
)
EXPORT_FAILURES = Counter(
    "cgm_bridge_export_failures_total", "Failed VictoriaMetrics export attempts"
)
EXPORT_PENDING = Gauge(
    "cgm_bridge_export_pending_samples", "Stored glucose readings not yet in VictoriaMetrics"
)
