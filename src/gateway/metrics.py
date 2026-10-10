from collections.abc import Callable, Iterable

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


class RateLimitMetrics:
    """Отказы по лимиту, состояние таблицы и откаты на адрес прокси. Без адресов."""

    def __init__(
        self,
        registry: CollectorRegistry,
        limited_routes: Iterable[str] = (),
        tracked_keys: Callable[[], int] | None = None,
    ) -> None:
        self.rejected = Counter(
            "gateway_rate_limited_total",
            "Запросы, отклонённые лимитом с кодом 429.",
            ["route", "scope"],
            registry=registry,
        )
        self.client_ip_fallback = Counter(
            "gateway_client_ip_fallback_total",
            "Запросы доверенного прокси, у которых клиента не удалось определить.",
            ["reason"],
            registry=registry,
        )
        tracked = Gauge(
            "gateway_rate_limit_tracked_keys",
            "Сколько адресов клиентов сейчас отслеживает лимит.",
            registry=registry,
        )
        if tracked_keys is not None:
            tracked.set_function(tracked_keys)
        # Серии есть с первого скрапа: rate() и алерты не работают с пустой серией.
        for route in limited_routes:
            for scope in ("client", "global", "table_full"):
                self.rejected.labels(route, scope)
        for reason in ("no_xff", "garbage"):
            self.client_ip_fallback.labels(reason)
