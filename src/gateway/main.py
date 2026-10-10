from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import structlog
from fastapi import FastAPI

from gateway import __version__
from gateway.api import install_openapi, load_contract, register_routes
from gateway.config import Settings
from gateway.errors import register_error_handlers
from gateway.health import router as health_router
from gateway.observability import configure_logging, create_tracer_provider
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
    client = UpstreamClient(settings, upstreams, transport=transport)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logger.info("service_started")
        try:
            yield
        finally:
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
    return app
