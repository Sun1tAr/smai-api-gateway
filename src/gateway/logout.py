from typing import Annotated, Any

import structlog
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from gateway.bearer import AuthenticatedUser, require_user
from gateway.errors import ErrorResponse

logger = structlog.get_logger()

LOGOUT_KEY = ("POST", "/auth/logout")


class LogoutResponse(BaseModel):
    success: bool = Field(description="Всегда `true`: токен отозван.")


def _error(code: str, message: str, details: dict[str, Any] | None = None) -> dict:
    body = {"error": {"code": code, "message": message, "details": details or {}}}
    return {"application/json": {"example": body}}


def register_logout(app: FastAPI) -> None:
    """`POST /auth/logout` (API-10): живёт только на gateway, MS-3 его не знает."""

    async def logout(
        request: Request, user: Annotated[AuthenticatedUser, Depends(require_user)]
    ) -> JSONResponse:
        await request.app.state.revocations.revoke(user.jti, user.exp)
        logger.info("token_revoked", jti=user.jti[:8])
        return JSONResponse({"success": True}, headers={"Cache-Control": "no-store"})

    method, path = LOGOUT_KEY
    app.add_api_route(
        path,
        logout,
        methods=[method],
        operation_id="post_auth_logout",
        tags=["auth"],
        summary="Выход (API-10)",
        description=(
            "Отзывает токен из заголовка `Authorization: Bearer <jwt>`: его `jti` "
            "записывается в список отозванных в Redis (`revoked:{jti}`) до момента "
            "`exp` токена. Дальше любой защищённый запрос с этим токеном получает "
            "401, повторный `POST /auth/logout` тоже. Тело запроса не нужно и не "
            "читается. Другие токены этого пользователя продолжают действовать.\n\n"
            "Если Redis недоступен, токен остаётся действующим и ответ 503: "
            "повторите запрос позже."
        ),
        response_model=LogoutResponse,
        status_code=200,
        responses={
            200: {
                "description": "Токен отозван.",
                "headers": {
                    "Cache-Control": {
                        "schema": {"type": "string", "example": "no-store"}
                    }
                },
                "content": {"application/json": {"example": {"success": True}}},
            },
            401: {
                "model": ErrorResponse,
                "description": (
                    "Токен отсутствует, недействителен (подпись, срок, формат) "
                    "или уже отозван. Причина в ответе не раскрывается."
                ),
                "headers": {
                    "WWW-Authenticate": {
                        "schema": {"type": "string", "example": "Bearer"}
                    }
                },
                "content": _error(
                    "UNAUTHORIZED", "Authentication token is missing or invalid"
                ),
            },
            500: {
                "model": ErrorResponse,
                "description": "Необработанная ошибка gateway.",
                "content": _error("INTERNAL_ERROR", "Internal server error"),
            },
            503: {
                "model": ErrorResponse,
                "description": (
                    "Redis недоступен (`reason`: `connect`, `timeout` или "
                    "`protocol`): отзыв не записан, токен всё ещё действует. "
                    "Запрос можно повторить."
                ),
                "content": _error(
                    "EXTERNAL_SERVICE_ERROR",
                    "Token store is unavailable, try again later",
                    {"upstream": "redis", "reason": "connect"},
                ),
            },
        },
    )
