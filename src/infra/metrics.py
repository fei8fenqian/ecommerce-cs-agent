from prometheus_client import CollectorRegistry, Counter, Histogram

METRICS_REGISTRY = CollectorRegistry()

HTTP_REQUESTS = Counter(
    "http_requests_total",
    "统计 HTTP 请求总数",
    labelnames=["method", "route", "status_code"],
    registry=METRICS_REGISTRY,
)

HTTP_REQUEST_DURATION = Histogram(
    "http_request_duration_seconds",
    "HTTP 请求用时(秒)",
    labelnames=["method", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0),
    registry=METRICS_REGISTRY,
)
