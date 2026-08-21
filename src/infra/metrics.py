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

RATE_LIMIT_REJECTED = Counter(
    "rate_limit_rejected_total",
    "被限流拒绝的请求总数",
    labelnames=["route", "method", "subject_type"],
    registry=METRICS_REGISTRY,
)

RATE_LIMIT_DEPENDENCY_FAILURES = Counter(
    "rate_limit_dependency_failures_total",
    "限流依赖失败总数",
    labelnames=["route", "method", "subject_type"],
    registry=METRICS_REGISTRY,
)

LLM_TIMEOUT = Counter(
    "llm_timeout_total",
    "LLM 请求超时总数",
    labelnames=["dependency", "operation"],
    registry=METRICS_REGISTRY,
)

LLM_RETRY = Counter(
    "llm_retry_total",
    "LLM 应用层重试总数",
    labelnames=["dependency", "operation"],
    registry=METRICS_REGISTRY,
)

LLM_CIRCUIT_OPEN = Counter(
    "llm_circuit_open_total",
    "LLM 熔断拒绝总数",
    labelnames=["dependency", "operation"],
    registry=METRICS_REGISTRY,
)

LLM_DEGRADED = Counter(
    "llm_degraded_total",
    "LLM 依赖故障导致降级的总数",
    labelnames=["dependency", "operation"],
    registry=METRICS_REGISTRY,
)

MCP_TIMEOUT = Counter(
    "mcp_timeout_total",
    "MCP 请求超时总数",
    labelnames=["dependency", "operation"],
    registry=METRICS_REGISTRY,
)

MCP_CALL_FAILURE = Counter(
    "mcp_call_failure_total",
    "MCP 工具调用失败总数",
    labelnames=["dependency", "operation"],
    registry=METRICS_REGISTRY,
)

MCP_CIRCUIT_OPEN = Counter(
    "mcp_circuit_open_total",
    "MCP 熔断拒绝总数",
    labelnames=["dependency", "operation"],
    registry=METRICS_REGISTRY,
)

MCP_DEGRADED = Counter(
    "mcp_degraded_total",
    "MCP 依赖故障导致受控降级的总数",
    labelnames=["dependency", "operation"],
    registry=METRICS_REGISTRY,
)
