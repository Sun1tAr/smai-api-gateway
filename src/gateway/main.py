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
from gateway.api import install_openapi, load_contract, register_routes
from gateway.asgi import RequestContextMiddleware, StripTraceContext
from gateway.config import Settings
from gateway.errors import register_error_handlers
from gateway.health import router as health_router
from gateway.metrics import UpstreamMetrics
from gateway.observability import (
    configure_logging,
    create_tracer_provider,
    redact_client_span,
    redact_span,
)
from gateway.proxy import UpstreamClient
from gateway.routing import load_routes

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
    routes = load_routes(settings.routes_file, upstreams)
    contract = load_contract(settings.contract_file)
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
