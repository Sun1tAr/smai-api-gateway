from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException


class ErrorDetail(BaseModel):
    code: str = Field(description="Машиночитаемый код ошибки.", examples=["NOT_FOUND"])
    message: str = Field(description="Описание ошибки для разработчика.")
    details: dict[str, Any] = Field(
        default_factory=dict, description="Дополнительные сведения, могут быть пустыми."
    )


class ErrorResponse(BaseModel):
    """Единый формат ошибки (TDR-4)."""

    error: ErrorDetail


def error_response(
    status_code: int,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body = {"error": {"code": code, "message": message, "details": details or {}}}
    return JSONResponse(body, status_code=status_code, headers=headers)


class GatewayError(Exception):
    """Ошибка, которую gateway сообщает клиенту сам, а не пересылает из MS-4."""

    status_code = 500
    code = "INTERNAL_ERROR"
    message = "Internal server error"

    def __init__(self, details: dict[str, Any] | None = None) -> None:
        super().__init__(self.message)
        self.details = details or {}


class InvalidRequestPath(GatewayError):
    status_code = 400
    code = "VALIDATION_ERROR"
    message = "Request path is invalid"


class PayloadTooLarge(GatewayError):
    status_code = 413
    code = "PAYLOAD_TOO_LARGE"
    message = "Request body is too large"


class UpstreamFailure(GatewayError):
    code = "EXTERNAL_SERVICE_ERROR"
    reason = "unknown"

    def __init__(self, upstream: str) -> None:
        super().__init__({"upstream": upstream, "reason": self.reason})
        self.upstream = upstream


class UpstreamUnavailable(UpstreamFailure):
    status_code = 502
    message = "Upstream service is unavailable"
    reason = "connect"


class UpstreamProtocolError(UpstreamFailure):
    status_code = 502
    message = "Upstream service failed while handling the request"
    reason = "protocol"


class UpstreamResponseTooLarge(UpstreamFailure):
    status_code = 502
    message = "Upstream response exceeds the allowed size"
    reason = "response_too_large"


class UpstreamTimeout(UpstreamFailure):
    status_code = 504
    message = "Upstream service did not respond in time"
    reason = "timeout"


class UpstreamOverloaded(UpstreamFailure):
    status_code = 503
    message = "Gateway is overloaded, try again later"
    reason = "pool"


_HTTP_ERRORS = {
    404: ("NOT_FOUND", "Resource not found"),
    405: ("METHOD_NOT_ALLOWED", "Method is not allowed for this resource"),
}


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(GatewayError)
    async def gateway_error(request: Request, exc: GatewayError) -> JSONResponse:
        return error_response(exc.status_code, exc.code, exc.message, exc.details)

    # Значения полей не возвращаем: в них могут быть данные клиента.
    @app.exception_handler(RequestValidationError)
    async def validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        fields = [
            {"loc": [str(part) for part in error["loc"]], "type": error["type"]}
            for error in exc.errors()
        ]
        return error_response(
            400, "VALIDATION_ERROR", "Request is invalid", {"fields": fields}
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code, message = _HTTP_ERRORS.get(
            exc.status_code,
            ("INTERNAL_ERROR", "Internal server error")
            if exc.status_code >= 500
            else ("VALIDATION_ERROR", "Request is invalid"),
        )
        headers = {
            name: value
            for name, value in (exc.headers or {}).items()
            if name.lower() == "allow"
        }
        return error_response(exc.status_code, code, message, headers=headers)
