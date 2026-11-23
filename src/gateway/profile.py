import json
import re
from datetime import datetime
from typing import Any

import structlog
from fastapi import Request, Response
from fastapi.responses import JSONResponse

from gateway.auth import error_body, retry_after
from gateway.bearer import AuthenticatedUser
from gateway.errors import (
    GatewayError,
    Unauthorized,
    UpstreamBusy,
    UpstreamProtocolError,
)
from gateway.policy import upstream_request_headers
from gateway.proxy import UpstreamResponse
from gateway.routing import Endpoint, Route

logger = structlog.get_logger()

UPSTREAM = "user"
# Профиль не проксируется побайтно: gateway сверяет ответ MS-3 с токеном,
# ставит X-User-Id из токена и переводит 404 в 401. Поэтому у него свой обработчик.
PROFILE_ENDPOINT = Endpoint("GET", "user", UPSTREAM, "/profile")
PROFILE_ROUTE = Route(prefix="user", upstream=UPSTREAM, endpoints=(PROFILE_ENDPOINT,))

_LOGIN_MAX = 254
_CREATED_AT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z")


def _profile(body: bytes, user_id: int) -> dict[str, Any] | None:
    """Поля профиля, если тело MS-3 соответствует контракту и токену."""
    try:
        data = json.loads(body)
        found, login, created_at = data["userId"], data["login"], data["createdAt"]
    except (ValueError, KeyError, TypeError):
        return None
    # bool в Python — int; userId чужого пользователя в ответе — нарушение контракта.
    if isinstance(found, bool) or found != user_id:
        return None
    if not isinstance(login, str) or not 0 < len(login) <= _LOGIN_MAX:
        return None
    if not isinstance(created_at, str) or not _CREATED_AT.fullmatch(created_at):
        return None
    try:
        datetime.fromisoformat(created_at)
    except ValueError:
        return None
    # Лишние поля MS-3 клиенту не уходят.
    return {"userId": user_id, "login": login, "createdAt": created_at}


def interpret_profile(response: UpstreamResponse, user_id: int) -> dict[str, Any]:
    """Профиль при успехе MS-3, иначе бросает ошибку для клиента."""
    status = response.status_code
    route = PROFILE_ENDPOINT.gateway_path
    if status == 200:
        if (profile := _profile(response.body, user_id)) is not None:
            return profile
    elif status == 404:
        error = error_body(response.body)
        if error is not None and error["code"] == "USER_NOT_FOUND":
            # Токен выдан пользователю, которого больше нет: он недействителен.
            logger.info("token_user_not_found")
            raise Unauthorized()
    elif status == 500:
        # Подробности MS-3 клиенту не нужны и не отдаются.
        logger.warning("user_service_failed", route=route)
        raise GatewayError()
    elif status == 503:
        error = error_body(response.body)
        if error is not None and error["code"] == "SERVICE_BUSY":
            raise UpstreamBusy(UPSTREAM, retry_after(response))
    # Остальное, включая 401 от MS-3 на заведомо верный X-User-Id, — нарушение
    # контракта MS-3.
    logger.warning("user_service_contract_violation", route=route, status=status)
    raise UpstreamProtocolError(UPSTREAM)


async def fetch_profile(
    request: Request, user: AuthenticatedUser, client_ip: str | None
) -> Response:
    """Запрашивает профиль в MS-3 от имени владельца токена."""
    headers = upstream_request_headers(
        request.headers.raw,
        request_id=request.state.request_id,
        client_ip=client_ip,
        scheme=request.url.scheme,
        user_id=user.user_id,
    )
    upstream = await request.app.state.upstream_client.forward(
        upstream=UPSTREAM,
        method=PROFILE_ENDPOINT.method,
        route=PROFILE_ENDPOINT.gateway_path,
        target=PROFILE_ENDPOINT.upstream_path.encode(),
        headers=headers,
        body=b"",
    )
    try:
        profile = interpret_profile(upstream, user.user_id)
    except Unauthorized:
        # 404 от MS-3: токен выдан пользователю, которого нет; это тоже отказ по токену.
        request.app.state.auth_metrics.rejected.labels("user_not_found").inc()
        raise
    return JSONResponse(
        profile,
        headers={"Cache-Control": "no-store"},
    )
