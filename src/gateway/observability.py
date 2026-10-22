import logging
import sys
from typing import Any

import structlog
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from gateway.config import Settings

SERVICE_NAME = "smai-api-gateway"


# Связываем JSON-логи с активным трейсом без логирования содержимого запросов.
def add_context(
    logger: Any,
    method_name: str,
    event: dict[str, Any],
) -> dict[str, Any]:
    context = trace.get_current_span().get_span_context()
    event["service"] = SERVICE_NAME
    event["trace_id"] = f"{context.trace_id:032x}" if context.is_valid else None
    event.setdefault("request_id", None)
    return event


# Одинаковый формат применяется к structlog и стандартным логгерам библиотек.
def configure_logging(level: str) -> None:
    shared = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        add_context,
        structlog.processors.format_exc_info,
    ]
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.EventRenamer("message"),
                structlog.processors.JSONRenderer(),
            ],
        )
    )
    logging.basicConfig(handlers=[handler], level=level, force=True)
    for name in ("uvicorn", "uvicorn.error"):
        server_logger = logging.getLogger(name)
        server_logger.handlers.clear()
        server_logger.propagate = True
        server_logger.setLevel(level)
    # HTTP-события пишет middleware без query; access log мог бы раскрыть query.
    logging.getLogger("uvicorn.access").disabled = True
    # httpx на уровне INFO печатает полный URL запроса вместе с query.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )


# Без коллектора сохраняем trace_id в логах, не делая внешних запросов.
def create_tracer_provider(settings: Settings) -> TracerProvider:
    provider = TracerProvider(resource=Resource.create({"service.name": SERVICE_NAME}))
    if settings.otel_exporter_otlp_endpoint is not None:
        endpoint = str(settings.otel_exporter_otlp_endpoint).rstrip("/") + "/v1/traces"
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, timeout=5))
        )
    return provider


_DROPPED_SPAN_ATTRIBUTES = frozenset(
    {"http.user_agent", "user_agent.original", "net.peer.ip", "client.address"}
)
# Заголовки запроса попадают в спан, если оператор включил их захват в OTel; токен и
# идентичность среди них не должны уходить в коллектор ни при какой настройке.
_SENSITIVE_HEADER_PREFIX = "http.request.header."
_SENSITIVE_HEADERS = frozenset(
    {"authorization", "proxy-authorization", "cookie", "x-user-id"}
)
_URL_SPAN_ATTRIBUTES = ("http.url", "url.full", "http.target", "url.query")


def _is_sensitive_header(key: str) -> bool:
    lowered = key.lower()
    return lowered.startswith(_SENSITIVE_HEADER_PREFIX) and (
        lowered.removeprefix(_SENSITIVE_HEADER_PREFIX).replace("_", "-")
        in _SENSITIVE_HEADERS
    )


# В коллектор не уходят query, User-Agent и адрес клиента: логи их тоже не пишут.
# Вызывается из хуков инструментаторов, когда атрибуты спана уже выставлены.
def redact_span(span: Any, *_: Any) -> None:
    attributes = getattr(span, "_attributes", None)
    if attributes is None:
        return
    for key in list(attributes):
        if (
            key in _DROPPED_SPAN_ATTRIBUTES
            or key == "url.query"
            or _is_sensitive_header(key)
        ):
            del attributes[key]
        elif key in _URL_SPAN_ATTRIBUTES:
            attributes[key] = str(attributes[key]).split("?", 1)[0]


# Клиент httpx асинхронный, поэтому его хук тоже должен быть корутиной.
async def redact_client_span(span: Any, *_: Any) -> None:
    redact_span(span)
