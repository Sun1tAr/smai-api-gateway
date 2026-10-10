from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from gateway import __version__
from gateway.config import Settings
from gateway.errors import register_error_handlers
from gateway.health import router as health_router
from gateway.observability import configure_logging, create_tracer_provider

logger = structlog.get_logger()


# Фабрика позволяет задавать отдельное окружение при тестировании.
def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    configure_logging(settings.log_level)
    provider = create_tracer_provider(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logger.info("service_started")
        try:
            yield
        finally:
            provider.shutdown()
            logger.info("service_stopped")

    app = FastAPI(title="SmartMeal API gateway", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.tracer_provider = provider
    register_error_handlers(app)
    app.include_router(health_router)
    return app
