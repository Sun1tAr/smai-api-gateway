import asyncio
import time
from dataclasses import dataclass

import httpx
import structlog
from fastapi import Request

from gateway.config import Settings
from gateway.errors import (
    PayloadTooLarge,
    UpstreamFailure,
    UpstreamOverloaded,
    UpstreamProtocolError,
    UpstreamResponseTooLarge,
    UpstreamTimeout,
    UpstreamUnavailable,
)
from gateway.metrics import UpstreamMetrics
from gateway.policy import Headers

logger = structlog.get_logger()


@dataclass(frozen=True)
class UpstreamResponse:
    status_code: int
    headers: Headers
    body: bytes


# Тело буферизуется: ретраев нет, а размер можно проверить до отправки в MS-4.
async def read_limited_body(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise PayloadTooLarge({"max_bytes": limit})
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise PayloadTooLarge({"max_bytes": limit})
        chunks.append(chunk)
    return b"".join(chunks)


class UpstreamClient:
    """Единственный HTTP-клиент gateway к бизнес-сервисам."""

    def __init__(
        self,
        settings: Settings,
        upstreams: dict[str, str],
        transport: httpx.AsyncBaseTransport | None = None,
        metrics: UpstreamMetrics | None = None,
    ) -> None:
        self._metrics = metrics
        self._upstreams = upstreams
        self._max_response_bytes = settings.upstream_max_response_bytes
        self._total_timeout = settings.upstream_total_timeout_seconds
        if transport is None:
            # retries повторяет только ошибки установки соединения: запрос не ушёл.
            transport = httpx.AsyncHTTPTransport(
                retries=1,
                limits=httpx.Limits(
                    max_connections=settings.upstream_max_connections,
                    max_keepalive_connections=min(
                        settings.upstream_max_connections, 20
                    ),
                    keepalive_expiry=settings.upstream_keepalive_expiry_seconds,
                ),
            )
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(
                connect=settings.upstream_connect_timeout_seconds,
                read=settings.upstream_read_timeout_seconds,
                write=settings.upstream_write_timeout_seconds,
                pool=settings.upstream_pool_timeout_seconds,
            ),
            trust_env=False,
            follow_redirects=False,
            http2=False,
        )

    @property
    def client(self) -> httpx.AsyncClient:
        return self._client

    async def aclose(self) -> None:
        await self._client.aclose()

    async def forward(
        self,
        *,
        upstream: str,
        method: str,
        route: str,
        target: bytes,
        headers: Headers,
        body: bytes,
    ) -> UpstreamResponse:
        started = time.perf_counter()
        try:
            response = await self._forward(upstream, method, target, headers, body)
        except UpstreamFailure as failure:
            self._record_failure(upstream, route, failure, started)
            raise
        self._record_response(upstream, route, method, response, started)
        return response

    def _record_response(
        self,
        upstream: str,
        route: str,
        method: str,
        response: UpstreamResponse,
        started: float,
    ) -> None:
        elapsed = time.perf_counter() - started
        if self._metrics is not None:
            self._metrics.requests.labels(
                upstream, route, method, str(response.status_code)
            ).inc()
            self._metrics.duration.labels(upstream, route).observe(elapsed)
            self._metrics.up.labels(upstream).set(1)
        logger.info(
            "upstream_request",
            upstream=upstream,
            method=method,
            route=route,
            status_code=response.status_code,
            duration_seconds=round(elapsed, 6),
        )

    def _record_failure(
        self, upstream: str, route: str, failure: UpstreamFailure, started: float
    ) -> None:
        elapsed = time.perf_counter() - started
        if self._metrics is not None:
            self._metrics.errors.labels(upstream, failure.reason).inc()
            self._metrics.duration.labels(upstream, route).observe(elapsed)
            # Только connect и timeout означают недоступность: остальные ошибки
            # (pool, protocol, response_too_large) не говорят о недоступности.
            if failure.reason in ("connect", "timeout"):
                self._metrics.up.labels(upstream).set(0)
        logger.warning(
            "upstream_request_failed",
            upstream=upstream,
            route=route,
            reason=failure.reason,
            duration_seconds=round(elapsed, 6),
        )

    async def _forward(
        self,
        upstream: str,
        method: str,
        target: bytes,
        headers: Headers,
        body: bytes,
    ) -> UpstreamResponse:
        url = httpx.URL(self._upstreams[upstream]).copy_with(raw_path=target)
        outgoing = list(headers)
        # Иначе httpx сам запросит gzip, а клиент получит сжатое тело без запроса.
        if not any(name.lower() == b"accept-encoding" for name, _ in outgoing):
            outgoing.append((b"accept-encoding", b"identity"))
        request = self._client.build_request(
            method, url, headers=outgoing, content=body or None
        )
        try:
            async with asyncio.timeout(self._total_timeout):
                return await self._send(request, upstream)
        except UpstreamFailure:
            raise
        except httpx.PoolTimeout:
            raise UpstreamOverloaded(upstream) from None
        except (httpx.ConnectTimeout, httpx.ConnectError):
            raise UpstreamUnavailable(upstream) from None
        except (httpx.TimeoutException, TimeoutError):
            raise UpstreamTimeout(upstream) from None
        except httpx.TransportError:
            raise UpstreamProtocolError(upstream) from None

    async def _send(self, request: httpx.Request, upstream: str) -> UpstreamResponse:
        response = await self._client.send(request, stream=True)
        try:
            # Контракт не знает редиректов; Location с внутренним хостом не отдаём.
            if 300 <= response.status_code < 400:
                raise UpstreamProtocolError(upstream)
            declared = response.headers.get("content-length")
            if declared is not None and declared.isdigit():
                if int(declared) > self._max_response_bytes:
                    raise UpstreamResponseTooLarge(upstream)
            chunks: list[bytes] = []
            size = 0
            # aiter_raw отдаёт байты как есть, поэтому content-encoding остаётся верным.
            async for chunk in response.aiter_raw():
                size += len(chunk)
                if size > self._max_response_bytes:
                    raise UpstreamResponseTooLarge(upstream)
                chunks.append(chunk)
        finally:
            await response.aclose()
        return UpstreamResponse(
            status_code=response.status_code,
            headers=list(response.headers.raw),
            body=b"".join(chunks),
        )
