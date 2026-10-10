import re
import time
from uuid import uuid4

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from gateway.errors import error_response

logger = structlog.get_logger()

# Общее правило gateway и MS-4: чужой идентификатор принимаем, только если он безопасен.
REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,128}")
_TRACE_HEADERS = frozenset({b"traceparent", b"tracestate", b"baggage"})


def pick_request_id(scope: Scope) -> str:
    for name, value in scope["headers"]:
        if name == b"x-request-id":
            candidate = value.decode("latin-1")
            if REQUEST_ID.fullmatch(candidate):
                return candidate
            break
    return str(uuid4())


class StripTraceContext:
    """Внешний вход не доверяет клиентскому трейсу: gateway начинает свой."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            scope = {
                **scope,
                "headers": [
                    (name, value)
                    for name, value in scope["headers"]
                    if name.lower() not in _TRACE_HEADERS
                ],
            }
        await self.app(scope, receive, send)


class RequestContextMiddleware:
    """X-Request-ID, лог запроса и единый ответ на необработанную ошибку.

    Написан как чистый ASGI: BaseHTTPMiddleware не отдаёт id в ответы с ошибкой
    и усложняет закрытие соединений при обрыве клиента.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = pick_request_id(scope)
        scope.setdefault("state", {})["request_id"] = request_id
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        started = time.perf_counter()
        status_code = 500
        response_started = False

        async def send_with_id(message: Message) -> None:
            nonlocal status_code, response_started
            if message["type"] == "http.response.start":
                response_started = True
                status_code = message["status"]
                headers = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if name.lower() != b"x-request-id"
                ]
                headers.append((b"x-request-id", request_id.encode("ascii")))
                message = {**message, "headers": headers}
            await send(message)

        try:
            try:
                await self.app(scope, receive, send_with_id)
            except Exception as error:
                logger.error(
                    "http_request_failed",
                    error_type=type(error).__name__,
                    exc_info=True,
                )
                if response_started:
                    raise
                response = error_response(
                    500, "INTERNAL_ERROR", "Internal server error"
                )
                await response(scope, receive, send_with_id)
            route = scope.get("route")
            logger.info(
                "http_request",
                method=scope["method"],
                path=getattr(route, "path", "unmatched"),
                status_code=status_code,
                duration_seconds=round(time.perf_counter() - started, 6),
            )
        finally:
            structlog.contextvars.clear_contextvars()
