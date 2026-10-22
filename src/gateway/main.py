from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import structlog
from fastapi import FastAPI
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from prometheus_client import CollectorRegistry
from prometheus_fastapi_instrumentator import Instrumentator
from starlette.types import ASGIApp

from gateway import __version__
from gateway.api import install_openapi, load_contracts, register_routes
from gateway.asgi import RequestContextMiddleware, StripTraceContext
from gateway.auth import AUTH_ROUTE
from gateway.config import Settings
from gateway.errors import register_error_handlers
from gateway.health import router as health_router
from gateway.metrics import RateLimitMetrics, UpstreamMetrics
from gateway.observability import (
    configure_logging,
    create_tracer_provider,
    redact_client_span,
    redact_span,
)
from gateway.proxy import UpstreamClient
from gateway.ratelimit import AUTH_LIMITED_ENDPOINTS, LIMITED_ENDPOINTS, RateLimiter
from gateway.routing import load_routes
from gateway.tokens import TokenIssuer

logger = structlog.get_logger()


# Фабрика позволяет задавать отдельное окружение при тестировании.
def create_app(
    settings: Settings | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    settings = settings or Settings()
    configure_logging(settings.log_level)
    provider = create_tracer_provider(settings)
    upstreams = settings.upstream_urls()
    # Вход и регистрация описаны в коде, а не в routes.toml: ответ MS-3 не проксируется.
    routes = (*load_routes(settings.routes_file, upstreams), AUTH_ROUTE)
    contract = load_contracts(
        settings.contract_file,
        settings.contract_dictionary_file,
        settings.contract_user_file,
    )
    token_issuer = TokenIssuer.from_file(
        settings.jwt_private_key_file, settings.jwt_ttl_seconds
    )
    registry = CollectorRegistry()
    client = UpstreamClient(
        settings, upstreams, transport=transport, metrics=UpstreamMetrics(registry)
    )
    HTTPXClientInstrumentor.instrument_client(
        client.client,
        tracer_provider=provider,
        request_hook=redact_client_span,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logger.info("service_started")
        if app.state.rate_limiter is not None and not settings.trusted_networks:
            logger.warning("rate_limit_keyed_by_socket_address")
        if settings.trusted_networks and settings.rate_limit_key_by_socket:
            logger.warning("trusted_proxies_override_key_by_socket")
        wide = [
            str(net)
            for net in settings.trusted_networks
            if net.prefixlen < net.max_prefixlen
        ]
        if wide:
            logger.warning("trusted_proxy_is_a_network", networks=wide)
        try:
            yield
        finally:
            HTTPXClientInstrumentor.uninstrument_client(client.client)
            await client.aclose()
            provider.shutdown()
            logger.info("service_stopped")

    # Слэш на конце не нормализуем: путь вне белого списка должен давать 404.
    app = FastAPI(
        title="SmartMeal API gateway",
        version=__version__,
        lifespan=lifespan,
        redirect_slashes=False,
    )
    app.state.settings = settings
    app.state.tracer_provider = provider
    app.state.upstream_client = client
    app.state.token_issuer = token_issuer
    limiter = (
        RateLimiter(
            requests=settings.rate_limit_requests,
            window_seconds=settings.rate_limit_window_seconds,
            max_keys=settings.rate_limit_max_keys,
        )
        if settings.rate_limit_enabled
        else None
    )
    app.state.rate_limiter = limiter
    app.state.global_limiter = RateLimiter(
        requests=settings.rate_limit_global_requests,
        window_seconds=settings.rate_limit_window_seconds,
        max_keys=1,
    )
    auth_limiter = (
        RateLimiter(
            requests=settings.auth_rate_limit_requests,
            window_seconds=settings.rate_limit_window_seconds,
            max_keys=settings.rate_limit_max_keys,
        )
        if settings.rate_limit_enabled and settings.auth_rate_limit_enabled
        else None
    )
    app.state.auth_rate_limiter = auth_limiter
    app.state.auth_global_limiter = RateLimiter(
        requests=settings.auth_rate_limit_global_requests,
        window_seconds=settings.rate_limit_window_seconds,
        max_keys=1,
    )
    app.state.rate_limit_metrics = RateLimitMetrics(
        registry,
        limited_routes=sorted(
            {path for _, path in LIMITED_ENDPOINTS | AUTH_LIMITED_ENDPOINTS}
        ),
        tracked_keys=(
            (lambda: app.state.rate_limiter.tracked_keys)
            if limiter is not None
            else None
        ),
    )
    register_error_handlers(app)
    app.include_router(health_router)
    register_routes(app, routes, contract, settings.max_request_body_bytes)
    install_openapi(app, routes, contract)
    app.add_middleware(RequestContextMiddleware)
    Instrumentator(
        registry=registry,
        should_group_status_codes=False,
        should_ignore_untemplated=True,
    ).instrument(app).expose(app, include_in_schema=False)
    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=provider,
        excluded_urls="^https?://[^/]+/health$,^https?://[^/]+/metrics$",
        server_request_hook=redact_span,
    )
    # Снаружи всего стека, включая OTel: клиентский traceparent в трейс не попадает.
    build_stack = app.build_middleware_stack

    def build_with_trace_reset() -> ASGIApp:
        return StripTraceContext(build_stack())

    app.build_middleware_stack = build_with_trace_reset
    return app
