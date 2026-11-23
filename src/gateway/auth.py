import json
import re
from typing import Any

import structlog
from fastapi import Request, Response
from fastapi.responses import JSONResponse

from gateway.errors import (
    GatewayError,
    UpstreamBusy,
    UpstreamProtocolError,
)
from gateway.policy import upstream_request_headers
from gateway.proxy import UpstreamResponse, read_limited_body
from gateway.routing import Endpoint, Route

logger = structlog.get_logger()

UPSTREAM = "user"
# Вход и регистрация не проксируются побайтно: MS-3 отдаёт только userId, а токен
# подписывает gateway. Поэтому у них свои обработчики и своя таблица ответов.
AUTH_ROUTE = Route(
    prefix="auth",
    upstream=UPSTREAM,
    endpoints=(
        Endpoint("POST", "auth", UPSTREAM, "/register"),
        Endpoint("POST", "auth", UPSTREAM, "/login"),
    ),
)
# Что MS-3 имеет право ответить: успех и ошибки, которые gateway переотдаёт как есть.
SUCCESS_STATUS = {"/register": 201, "/login": 200}
PASSED_ERRORS = {"/register": frozenset({400, 409}), "/login": frozenset({400, 401})}
DEFAULT_RETRY_AFTER = 5

_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
_MAX_MESSAGE = 500


class UserServiceError(GatewayError):
    """Ошибка MS-3 из белого списка: статус, code и message берутся из его ответа."""

    def __init__(
        self, status_code: int, code: str, message: str, details: dict[str, Any]
    ) -> None:
        super().__init__(details)
        self.status_code = status_code
        self.code = code
        self.message = message


def error_body(body: bytes) -> dict[str, Any] | None:
    try:
        error = json.loads(body)["error"]
        code, message = error["code"], error["message"]
        details = error.get("details", {})
    except (ValueError, KeyError, TypeError):
        return None
    if (
        not isinstance(code, str)
        or not _CODE.fullmatch(code)
        or not isinstance(message, str)
        or not 0 < len(message) <= _MAX_MESSAGE
        or not isinstance(details, dict)
    ):
        return None
    return {"code": code, "message": message, "details": details}


def _user_id(body: bytes) -> int | None:
    try:
        user_id = json.loads(body)["userId"]
    except (ValueError, KeyError, TypeError):
        return None
    # bool в Python — int; true в качестве userId принимать нельзя.
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id < 1:
        return None
    return user_id


def retry_after(response: UpstreamResponse) -> int:
    for name, value in response.headers:
        if name.lower() == b"retry-after" and value.isdigit() and int(value) > 0:
            return min(int(value), 3600)
    return DEFAULT_RETRY_AFTER


def interpret(endpoint: Endpoint, response: UpstreamResponse) -> int:
    """Возвращает userId при успехе MS-3, иначе бросает ошибку для клиента."""
    status = response.status_code
    path = endpoint.upstream_path
    if status == SUCCESS_STATUS[path]:
        if (user_id := _user_id(response.body)) is not None:
            return user_id
    elif status in PASSED_ERRORS[path]:
        if (error := error_body(response.body)) is not None:
            raise UserServiceError(status, **error)
    elif status == 500:
        # Подробности MS-3 клиенту не нужны и не отдаются.
        logger.warning("user_service_failed", route=endpoint.gateway_path)
        raise GatewayError()
    elif status == 503:
        error = error_body(response.body)
        if error is not None and error["code"] == "SERVICE_BUSY":
            raise UpstreamBusy(endpoint.upstream, retry_after(response))
    # Любой другой статус и тело, которое не разобрать, — нарушение контракта MS-3.
    logger.warning(
        "user_service_contract_violation", route=endpoint.gateway_path, status=status
    )
    raise UpstreamProtocolError(endpoint.upstream)


async def authenticate(
    request: Request, endpoint: Endpoint, limit: int, client_ip: str | None
) -> Response:
    """Пересылает учётные данные в MS-3 и выдаёт клиенту `{userId, token}`."""
    state = request.app.state
    body = await read_limited_body(request, limit)
    headers = upstream_request_headers(
        request.headers.raw,
        request_id=request.state.request_id,
        client_ip=client_ip,
        scheme=request.url.scheme,
    )
    upstream = await state.upstream_client.forward(
        upstream=endpoint.upstream,
        method=endpoint.method,
        route=endpoint.gateway_path,
        target=endpoint.upstream_path.encode(),
        headers=headers,
        body=body,
    )
    user_id = interpret(endpoint, upstream)
    return JSONResponse(
        {"userId": user_id, "token": state.token_issuer.issue(user_id)},
        status_code=SUCCESS_STATUS[endpoint.upstream_path],
        # Ответ с токеном не должен оседать в кэшах.
        headers={"Cache-Control": "no-store"},
    )
