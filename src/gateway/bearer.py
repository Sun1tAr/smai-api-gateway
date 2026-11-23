import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated

import structlog
from fastapi import Depends, Request
from fastapi.security import HTTPBearer

from gateway.errors import RedisUnavailable, Unauthorized
from gateway.tokens import InvalidToken, VerifiedToken

logger = structlog.get_logger()

MAX_HEADER_LENGTH = 4096
_BEARER = re.compile(
    r"[Bb][Ee][Aa][Rr][Ee][Rr] ([A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)"
)
# Только описание в /docs (кнопка Authorize): заголовок разбирается ниже вручную.
bearer_scheme = HTTPBearer(
    scheme_name="bearerAuth",
    bearerFormat="JWT",
    description=(
        "JWT (ES256) из ответа `POST /auth/register` или `POST /auth/login`. "
        "Заголовок: `Authorization: Bearer <token>`."
    ),
    auto_error=False,
)


@dataclass(frozen=True)
class AuthenticatedUser:
    user_id: int
    jti: str
    exp: int


def _reject(request: Request, reason: str) -> Unauthorized:
    request.app.state.auth_metrics.rejected.labels(reason).inc()
    # Токена и его частей в логе нет: только причина.
    logger.info("token_rejected", reason=reason)
    return Unauthorized()


def _token(request: Request) -> str | None:
    """Токен из заголовка; None, если заголовка нет. Некорректный — 401."""
    values = request.headers.getlist("authorization")
    if not values:
        return None
    if len(values) != 1 or len(values[0]) > MAX_HEADER_LENGTH:
        raise _reject(request, "malformed")
    match = _BEARER.fullmatch(values[0])
    if match is None:
        raise _reject(request, "malformed")
    return match.group(1)


async def _authenticate(request: Request) -> AuthenticatedUser | None:
    token = _token(request)
    if token is None:
        return None
    state = request.app.state
    try:
        verified: VerifiedToken = state.token_verifier.verify(token)
    except InvalidToken as invalid:
        raise _reject(request, invalid.reason) from None
    # Сначала подпись и срок: Redis не нагружается мусором.
    if await state.revocations.is_revoked(verified.jti):
        logger.info("token_revoked_used", jti=verified.jti[:8])
        raise _reject(request, "revoked")
    return AuthenticatedUser(verified.user_id, verified.jti, verified.exp)


def authenticate_request(
    required: bool,
) -> Callable[..., Awaitable[AuthenticatedUser | None]]:
    """Зависимость маршрута: проверяет токен и кладёт `request.state.user`.

    required=False: без заголовка запрос идёт как гостевой (`user` = None), но
    заголовок, который есть, проверяется так же строго: подпись, срок и формат
    дают 401. Исключение одно: если Redis недоступен и отзыв проверить нельзя,
    запрос идёт как гостевой (владелец не передаётся, параметры не сохраняются).
    Права это не добавляет: гость не получает ничего, что есть у пользователя.
    """

    async def dependency(
        request: Request, _: Annotated[object, Depends(bearer_scheme)]
    ) -> AuthenticatedUser | None:
        try:
            user = await _authenticate(request)
        except RedisUnavailable:
            if required:
                raise
            # Токена и jti в логе нет; причина сбоя уже записана в `redis_failed`.
            request.app.state.auth_metrics.fallback_guest.inc()
            logger.warning("token_redis_down_guest")
            user = None
        if user is None and required:
            raise _reject(request, "missing")
        request.state.user = user
        return user

    dependency.token_required = required  # type: ignore[attr-defined]
    return dependency


require_user = authenticate_request(True)
optional_user = authenticate_request(False)
