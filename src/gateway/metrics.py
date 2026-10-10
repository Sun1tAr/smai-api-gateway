from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

# Метки только из конечных множеств: имя сервиса, шаблон пути, известный метод.
_DURATION_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 15)


class UpstreamMetrics:
    """Метрики обращений gateway к бизнес-сервисам (DDR-7)."""

    def __init__(self, registry: CollectorRegistry) -> None:
        self.requests = Counter(
            "gateway_upstream_requests_total",
            "Ответы upstream-сервисов, полученные gateway.",
            ["upstream", "route", "method", "status"],
            registry=registry,
        )
        self.duration = Histogram(
            "gateway_upstream_request_duration_seconds",
            "Длительность запроса к upstream-сервису.",
            ["upstream", "route"],
            buckets=_DURATION_BUCKETS,
            registry=registry,
        )
        self.errors = Counter(
            "gateway_upstream_errors_total",
            "Ошибки обращения к upstream-сервису без ответа от него.",
            ["upstream", "reason"],
            registry=registry,
        )
        self.up = Gauge(
            "gateway_upstream_up",
            "1, если последний запрос к сервису получил ответ; 0, если нет.",
            ["upstream"],
            registry=registry,
        )
