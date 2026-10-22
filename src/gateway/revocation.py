import asyncio
import time
from collections.abc import Callable

import structlog
from redis import exceptions as redis_errors
from redis.asyncio import Redis
from redis.backoff import NoBackoff
from redis.retry import Retry

from gateway.config import Settings
from gateway.errors import RedisUnavailable
from gateway.metrics import AuthMetrics

logger = structlog.get_logger()

KEY_PREFIX = "revoked:"


def create_redis(settings: Settings) -> Redis:
    """Клиент без соединения: оно открывается при первой команде."""
    return Redis.from_url(
        settings.redis_url.get_secret_value(),
        socket_connect_timeout=settings.redis_connect_timeout_seconds,
        socket_timeout=settings.redis_operation_timeout_seconds,
        # Без ретраев: сбой Redis быстро превращается в 503, а не в зависший запрос.
        retry=Retry(NoBackoff(), 0),
        health_check_interval=0,
        # Зависший Redis не должен раздувать число соединений.
        max_connections=50,
    )


class RevocationStore:
    """Список отозванных токенов: ключ `revoked:{jti}` живёт до `exp` токена."""

    def __init__(
        self,
        client: Redis,
        metrics: AuthMetrics,
        settings: Settings,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._client = client
        self._metrics = metrics
        self._clock = clock
        # Страховка поверх таймаутов сокета (например, зависший DNS).
        self._deadline = (
            settings.redis_connect_timeout_seconds
            + settings.redis_operation_timeout_seconds
            + 0.5
        )

    async def is_revoked(self, jti: str) -> bool:
        try:
            found = await self._run(self._client.exists(KEY_PREFIX + jti))
        except RedisUnavailable:
            self._metrics.revocation_checks.labels("error").inc()
            raise
        revoked = bool(found)
        self._metrics.revocation_checks.labels("revoked" if revoked else "ok").inc()
        return revoked

    async def revoke(self, jti: str, exp: int) -> None:
        ttl = max(1, exp - int(self._clock()))
        try:
            await self._run(self._client.set(KEY_PREFIX + jti, "1", ex=ttl))
        except RedisUnavailable:
            self._metrics.revocation_writes.labels("error").inc()
            raise
        self._metrics.revocation_writes.labels("ok").inc()

    async def _run(self, command):  # type: ignore[no-untyped-def]
        try:
            async with asyncio.timeout(self._deadline):
                result = await command
        except (redis_errors.TimeoutError, TimeoutError):
            self._failed("timeout")
        except (redis_errors.ConnectionError, OSError):
            self._failed("connect")
        except redis_errors.RedisError:
            self._failed("protocol")
        self._metrics.redis_up.set(1)
        return result

    def _failed(self, reason: str) -> None:
        # Недоступностью считаются соединение и таймаут; ошибка протокола (например,
        # отказ команды) говорит, что Redis отвечает.
        self._metrics.redis_up.set(0 if reason != "protocol" else 1)
        logger.warning("redis_failed", reason=reason)
        raise RedisUnavailable(reason)
