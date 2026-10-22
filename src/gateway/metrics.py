from collections.abc import Callable, Iterable

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram
from prometheus_client.core import GaugeMetricFamily

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


TOKEN_REJECT_REASONS = (
    "missing",
    "malformed",
    "algorithm",
    "kid",
    "signature",
    "expired",
    "not_yet_valid",
    "claims",
    "revoked",
    "user_not_found",
)


class AuthMetrics:
    """Проверка токенов и Redis с отзывами. Без токенов, jti и адресов."""

    def __init__(self, registry: CollectorRegistry) -> None:
        self.rejected = Counter(
            "gateway_token_rejected_total",
            "Запросы, отклонённые с кодом 401, по причине.",
            ["reason"],
            registry=registry,
        )
        self.revocation_checks = Counter(
            "gateway_revocation_checks_total",
            "Проверки списка отозванных токенов в Redis.",
            ["result"],
            registry=registry,
        )
        self.revocation_writes = Counter(
            "gateway_revocation_writes_total",
            "Записи отзыва токена в Redis (POST /auth/logout).",
            ["result"],
            registry=registry,
        )
        self.fallback_guest = Counter(
            "gateway_token_fallback_guest_total",
            "Запросы с токеном на необязательном маршруте, обработанные как гостевые "
            "из-за недоступного Redis.",
            registry=registry,
        )
        # Серии есть с первого скрапа: rate() и алерты не работают с пустой серией.
        for reason in TOKEN_REJECT_REASONS:
            self.rejected.labels(reason)
        for result in ("ok", "revoked", "error"):
            self.revocation_checks.labels(result)
        for result in ("ok", "error"):
            self.revocation_writes.labels(result)
        self.redis_up = _RedisUp(registry)


class _RedisUp:
    """gateway_redis_up: пока к Redis не обращались, серии нет (не 0).

    Обычный Gauge без меток показывал бы 0 с первого скрапа, и алерт «Redis
    упал» срабатывал бы у гейта, который просто ещё не проверял токены.
    """

    def __init__(self, registry: CollectorRegistry) -> None:
        self._value: float | None = None
        registry.register(self)  # type: ignore[arg-type]

    def set(self, value: float) -> None:
        self._value = value

    def collect(self):  # type: ignore[no-untyped-def]
        if self._value is not None:
            family = GaugeMetricFamily(
                "gateway_redis_up",
                "1, если последняя команда к Redis прошла; 0 при ошибке "
                "соединения или таймауте.",
            )
            family.add_metric([], self._value)
            yield family
