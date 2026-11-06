import copy
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog
from fastapi import Depends, FastAPI, Request, Response
from fastapi.openapi.utils import get_openapi
from fastapi.routing import APIRoute

from gateway.auth import AUTH_ROUTE, authenticate
from gateway.bearer import optional_user, require_user
from gateway.errors import (
    ErrorResponse,
    InvalidRequestPath,
    PayloadTooLarge,
    RateLimited,
    RedisUnavailable,
    UpstreamBusy,
    UpstreamOverloaded,
    UpstreamUnavailable,
)
from gateway.health import metrics_path_item
from gateway.logout import LOGOUT_KEY
from gateway.policy import client_response_headers, upstream_request_headers
from gateway.profile import PROFILE_ENDPOINT, fetch_profile
from gateway.proxy import UpstreamClient, read_limited_body
from gateway.ratelimit import (
    AUTH_LIMITED_ENDPOINTS,
    CART_LIMITED_ENDPOINTS,
    GLOBAL_KEY,
    LIMITED_ENDPOINTS,
    RATE_LIMITED_ENDPOINTS,
    RateLimiter,
    ResolvedClient,
    rate_limit_key,
    resolve_client_ip,
)
from gateway.routing import CART_ENDPOINT, Endpoint, InvalidPath, Route, upstream_target

logger = structlog.get_logger()
_BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})
# Ответы, при которых работа в сервисе не начиналась (генерация в MS-4, проверка
# пароля в MS-3): попытка в лимит не идёт.
_NOT_REACHED = (
    InvalidRequestPath,
    PayloadTooLarge,
    UpstreamUnavailable,
    UpstreamOverloaded,
    UpstreamBusy,
)
_PARAM = re.compile(r"\{[a-z][a-z0-9_]*\}")
# Gateway проверяет при старте, что у каждого из этих маршрутов есть проверка JWT.
PROTECTED_ENDPOINTS = frozenset(
    {
        LOGOUT_KEY,
        ("GET", PROFILE_ENDPOINT.gateway_path),
        ("GET", "/recipes/profile/parameters"),
        ("POST", "/recipes/{recipe_id}/like"),
        ("DELETE", "/recipes/{recipe_id}/like"),
        ("GET", "/recipes/liked"),
    }
)
# Маршруты API-1 и API-14 должны проверять токен, когда он передан.
OPTIONAL_AUTH_ENDPOINTS = frozenset({("POST", "/recipes/generate-menu"), CART_ENDPOINT})
# Путь в сервис задан кодом, а не запросом клиента: 400 за небезопасный путь невозможен.
_FIXED_PATH = (*AUTH_ROUTE.endpoints, PROFILE_ENDPOINT)


class ContractError(ValueError):
    """Снимок контракта не описывает разрешённый путь; сервис не стартует."""


def load_contract(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ContractError(f"cannot read contract: {type(error).__name__}") from None


def _has_ref(node: Any) -> bool:
    if isinstance(node, dict):
        return "$ref" in node or any(_has_ref(value) for value in node.values())
    if isinstance(node, list):
        return any(_has_ref(value) for value in node)
    return False


def load_contracts(*paths: Path) -> dict[str, Any]:
    """Склеивает пути нескольких снимков; один путь в двух снимках — ошибка.

    Берутся только `paths`, `components` не переносятся, поэтому `$ref` в снимке
    оборвался бы в итоговой схеме: такой снимок отклоняется.
    """
    merged: dict[str, Any] = {"paths": {}}
    for path in paths:
        contract = load_contract(path)
        contract_paths = contract.get("paths") if isinstance(contract, dict) else None
        if not isinstance(contract_paths, dict):
            raise ContractError(f"{path.name}: contract has no paths")
        if _has_ref(contract_paths):
            raise ContractError(f"{path.name}: $ref is not supported, inline schemas")
        for upstream_path, item in contract_paths.items():
            if upstream_path in merged["paths"]:
                raise ContractError(f"contracts define {upstream_path} twice")
            merged["paths"][upstream_path] = item
    return merged


# Ошибки, которые отвечает сам gateway: описание, code, message и reason в details.
_GATEWAY_ERRORS: dict[int, tuple[str, str, str, str | None]] = {
    400: (
        "Gateway отклонил недопустимые символы или закодированные разделители "
        "в пути или query.",
        "VALIDATION_ERROR",
        "Request path is invalid",
        None,
    ),
    413: (
        "Тело запроса больше `MAX_REQUEST_BODY_BYTES`.",
        "PAYLOAD_TOO_LARGE",
        "Request body is too large",
        None,
    ),
    429: (
        "Слишком много запросов: по умолчанию не более 10 за минуту с одного адреса "
        "(IPv6 считается по сети) и общий потолок на всех клиентов. Значения "
        "настраиваются, лимит может быть выключен, тогда ответа 429 не будет. "
        "Через сколько секунд повторить, "
        "указано в `Retry-After` и в `details.retry_after_seconds` (целое число ≥ 1). "
        "В лимит идут все запросы, кроме тех, что не дошли до сервиса: 400 самого "
        "gateway (небезопасный путь), 413, 502 `connect` и 503 `pool` слот "
        "возвращают. Отклонённые запросы не засчитываются.",
        "RATE_LIMITED",
        "Too many requests, try again later",
        None,
    ),
    502: (
        "Сервис недоступен, оборвал соединение или ответил некорректно.",
        "EXTERNAL_SERVICE_ERROR",
        "Upstream service is unavailable",
        "connect",
    ),
    503: (
        "Gateway перегружен: исчерпан пул соединений к сервису. Ответ 503 самого "
        "сервиса приходит как есть и отличается кодом ошибки.",
        "EXTERNAL_SERVICE_ERROR",
        "Gateway is overloaded, try again later",
        "pool",
    ),
    504: (
        "Сервис не ответил за отведённое время. Запрос мог быть выполнен.",
        "EXTERNAL_SERVICE_ERROR",
        "Upstream service did not respond in time",
        "timeout",
    ),
}


def _gateway_example(status: int, upstream: str, limit: int = 65536) -> dict[str, Any]:
    _, code, message, reason = _GATEWAY_ERRORS[status]
    details: dict[str, Any] = {}
    if reason is not None:
        details = {"upstream": upstream, "reason": reason}
    elif status == 413:
        details = {"max_bytes": limit}
    elif status == 429:
        details = {"retry_after_seconds": 30}
    return {"error": {"code": code, "message": message, "details": details}}


def _gateway_response(status: int, upstream: str, limit: int) -> dict[str, Any]:
    extra: dict[str, Any] = {}
    if status == 429:
        extra["headers"] = {
            "Retry-After": {
                "description": "Через сколько секунд повторить запрос.",
                "schema": {"type": "integer", "minimum": 1},
            }
        }
    return {
        **extra,
        "model": ErrorResponse,
        "description": _GATEWAY_ERRORS[status][0],
        "content": {
            "application/json": {"example": _gateway_example(status, upstream, limit)}
        },
    }


# У защищённого маршрута 503 бывает ещё и из-за Redis: два примера рядом.
def _protected_503(endpoint: Endpoint, limit: int) -> dict[str, Any]:
    pool = _gateway_response(503, endpoint.upstream, limit)
    redis = {
        "error": {
            "code": "EXTERNAL_SERVICE_ERROR",
            "message": RedisUnavailable.message,
            "details": {"upstream": "redis", "reason": "connect"},
        }
    }
    description = (
        "Токен нельзя проверить или gateway перегружен. Redis с отозванными "
        "токенами недоступен (`details.upstream` = `redis`, `reason`: "
        "`connect`, `timeout`, `protocol`): запрос можно повторить. "
        "Либо исчерпан пул соединений к сервису (`reason` = `pool`)."
    )
    examples = {
        "redis": {"summary": "Redis недоступен", "value": redis},
        "pool": {
            "summary": "Пул соединений исчерпан",
            "value": _gateway_example(503, endpoint.upstream, limit),
        },
    }
    if endpoint == PROFILE_ENDPOINT:
        description += " MS-3 занят: `Retry-After`, `reason` = `busy`."
        examples["busy"] = {
            "summary": "MS-3 занят (заголовок Retry-After)",
            "value": {
                "error": {
                    "code": "EXTERNAL_SERVICE_ERROR",
                    "message": UpstreamBusy.message,
                    "details": {"upstream": "user", "reason": "busy"},
                }
            },
        }
    return {
        **pool,
        "description": description,
        "content": {"application/json": {"examples": examples}},
    }


def _contract_response(where: str, status: str, response: Any) -> dict[str, Any]:
    if not isinstance(response, dict) or "description" not in response:
        raise ContractError(f"{where}: response {status} has no description")
    media = response.get("content", {}).get("application/json")
    if not isinstance(media, dict) or not ("example" in media or "examples" in media):
        raise ContractError(f"{where}: response {status} has no JSON example")
    return response


def _operation_docs(
    endpoint: Endpoint, contract: dict[str, Any], limit: int = 65536
) -> dict[str, Any]:
    where = f"{endpoint.method} {endpoint.upstream_path}"
    try:
        operation = contract["paths"][endpoint.upstream_path][endpoint.method.lower()]
    except KeyError:
        raise ContractError(f"contract has no {where}") from None
    for key in ("summary", "description", "responses"):
        if not operation.get(key):
            raise ContractError(f"{where}: no {key}")
    endpoint_key = (endpoint.method, endpoint.gateway_path)
    responses: dict[int | str, dict[str, Any]] = {}
    for status in sorted(_GATEWAY_ERRORS):
        if status == 413 and endpoint.method not in _BODY_METHODS:
            continue
        if status == 400 and endpoint in _FIXED_PATH:
            continue
        if status == 429 and endpoint_key not in RATE_LIMITED_ENDPOINTS:
            continue
        responses[status] = _gateway_response(status, endpoint.upstream, limit)
    if endpoint_key in PROTECTED_ENDPOINTS:
        responses[503] = _protected_503(endpoint, limit)
    # Ответы MS-4 описывают его собственные ошибки и перекрывают общие.
    for status, response in operation["responses"].items():
        response = dict(_contract_response(where, status, response))
        if status == "400" and endpoint not in _FIXED_PATH:
            response["description"] += " " + _GATEWAY_ERRORS[400][0]
        responses[status] = response
    extra: dict[str, Any] = {}
    if "requestBody" in operation:
        extra["requestBody"] = operation["requestBody"]
    if "parameters" in operation:
        extra["parameters"] = operation["parameters"]
    elif _PARAM.search(endpoint.upstream_path):
        raise ContractError(f"{where}: no parameters")
    successes = sorted(int(s) for s in operation["responses"] if s.startswith("2"))
    if not successes:
        raise ContractError(f"{where}: no 2xx response")
    return {
        "summary": operation["summary"],
        "description": operation["description"],
        "responses": responses,
        "openapi_extra": extra,
        "status_code": successes[0],
    }


def request_client(request: Request) -> ResolvedClient:
    """Адрес клиента с учётом доверенных прокси; считается один раз на запрос."""
    cached = getattr(request.state, "client", None)
    if cached is not None:
        return cached
    client = resolve_client_ip(
        request.client.host if request.client else None,
        request.headers.getlist("x-forwarded-for"),
        request.app.state.settings.trusted_networks,
    )
    if client.fallback is not None:
        request.app.state.rate_limit_metrics.client_ip_fallback.labels(
            client.fallback
        ).inc()
    request.state.client = client
    return client


def _refund_rate_limit(request: Request) -> None:
    ticket = getattr(request.state, "rate_ticket", None)
    if ticket is None:
        return
    request.state.rate_ticket = None
    ticket.limiter.refund(ticket.key, ticket.stamp)
    ticket.global_limiter.refund(GLOBAL_KEY, ticket.global_stamp)


@dataclass
class _Ticket:
    key: str
    stamp: float
    global_stamp: float
    limiter: RateLimiter
    global_limiter: RateLimiter


# API-14 и API-16 делят общий потолок, но используют разные лимитеры клиентов.
def _limiters(
    state: Any, key: tuple[str, str]
) -> tuple[RateLimiter | None, RateLimiter | None] | None:
    if key in LIMITED_ENDPOINTS:
        return state.rate_limiter, state.global_limiter
    if key in AUTH_LIMITED_ENDPOINTS:
        return state.auth_rate_limiter, state.auth_global_limiter
    if key in CART_LIMITED_ENDPOINTS:
        return state.cart_rate_limiters.get(key), state.cart_global_limiter
    return None


# Вход в проксируемый маршрут: лимит запросов; сюда же встанет проверка пользователя.
# Должна оставаться async def: в threadpool проверка и запись начали бы гоняться.
async def edge_guard(request: Request) -> None:
    state = request.app.state
    route = request.scope.get("route")
    limiters = _limiters(state, (request.method, getattr(route, "path", "")))
    if limiters is None or limiters[0] is None:
        return
    limiter, global_limiter = limiters
    assert global_limiter is not None  # задан всегда, когда задан клиентский
    client = request_client(request)
    key = rate_limit_key(client.ip, state.settings.rate_limit_ipv6_prefix)
    admission = limiter.check(key)
    scope = "table_full" if admission.table_full else "client"
    fingerprint = limiter.fingerprint(key)
    if admission.allowed:
        overall = global_limiter.check(GLOBAL_KEY)
        if overall.allowed:
            request.state.rate_ticket = _Ticket(
                key, admission.stamp, overall.stamp, limiter, global_limiter
            )
            return
        limiter.refund(key, admission.stamp)
        admission, scope, fingerprint = overall, "global", GLOBAL_KEY
    state.rate_limit_metrics.rejected.labels(route.path, scope).inc()
    # Строка на каждую новую «самую старую попытку», а не на каждый 429: под атакой
    # поток логов ограничен числом слотов окна. Отказы table_full есть лишь в метрике.
    if admission.first_denial:
        logger.info(
            "rate_limited",
            route=route.path,
            scope=scope,
            client=fingerprint,
            retry_after_seconds=admission.retry_after,
        )
    raise RateLimited(admission.retry_after)


# Токен проверяется раньше лимитера: подпись дешёвая, а лимит на неё не нужен.
def _dependencies(endpoint: Endpoint) -> list[Any]:
    key = (endpoint.method, endpoint.gateway_path)
    guard: list[Any] = []
    if key in PROTECTED_ENDPOINTS:
        guard = [Depends(require_user)]
    elif key in OPTIONAL_AUTH_ENDPOINTS:
        guard = [Depends(optional_user)]
    return [*guard, Depends(edge_guard)]


def _register(
    app: FastAPI, endpoint: Endpoint, docs: dict[str, Any], limit: int
) -> None:
    async def handler(request: Request) -> Response:
        try:
            return await _proxy(request)
        except _NOT_REACHED:
            # Запрос заведомо не дошёл до сервиса: слот лимита не тратится.
            _refund_rate_limit(request)
            raise

    async def _proxy(request: Request) -> Response:
        if endpoint in AUTH_ROUTE.endpoints:
            return await authenticate(
                request, endpoint, limit, request_client(request).ip
            )
        if endpoint == PROFILE_ENDPOINT:
            return await fetch_profile(
                request, request.state.user, request_client(request).ip
            )
        client: UpstreamClient = request.app.state.upstream_client
        raw_path = request.scope.get("raw_path") or request.scope["path"].encode()
        try:
            target = upstream_target(
                endpoint, raw_path, request.scope.get("query_string", b"")
            )
        except InvalidPath:
            raise InvalidRequestPath() from None
        # Тело есть только у методов с телом; у GET оно не читается и не пересылается.
        body = (
            await read_limited_body(request, limit)
            if endpoint.method in _BODY_METHODS
            else b""
        )
        headers = upstream_request_headers(
            request.headers.raw,
            request_id=request.state.request_id,
            client_ip=request_client(request).ip,
            scheme=request.url.scheme,
            # Только из проверенного токена; у гостя и на публичных маршрутах нет.
            user_id=user.user_id
            if (user := getattr(request.state, "user", None))
            else None,
        )
        upstream = await client.forward(
            upstream=endpoint.upstream,
            method=endpoint.method,
            route=endpoint.gateway_path,
            target=target,
            headers=headers,
            body=body,
        )
        response = Response(content=upstream.body, status_code=upstream.status_code)
        response.raw_headers = client_response_headers(
            upstream.headers, body_length=len(upstream.body)
        )
        return response

    slug = re.sub(r"[^a-z0-9]+", "_", endpoint.gateway_path.lower()).strip("_")
    app.add_api_route(
        endpoint.gateway_path,
        handler,
        methods=[endpoint.method],
        operation_id=f"{endpoint.method.lower()}_{slug}",
        tags=[endpoint.prefix or endpoint.upstream],
        response_class=Response,
        dependencies=_dependencies(endpoint),
        **docs,
    )


def register_routes(
    app: FastAPI, routes: tuple[Route, ...], contract: dict[str, Any], limit: int
) -> None:
    registered = set()
    for route in routes:
        for endpoint in route.endpoints:
            _register(app, endpoint, _operation_docs(endpoint, contract, limit), limit)
            registered.add((endpoint.method, endpoint.gateway_path))
    # Переименованный путь иначе тихо остался бы без лимита.
    missing = RATE_LIMITED_ENDPOINTS - registered
    if missing:
        raise ContractError(f"rate limited endpoints are not routed: {sorted(missing)}")


def check_protected_routes(app: FastAPI) -> None:
    """Маршрут с токеном без проверки или без регистрации не стартует."""
    checked: set[tuple[str, str]] = set()
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        guarded = any(
            getattr(dep.call, "token_required", False) is True
            for dep in route.dependant.dependencies
        )
        for method in route.methods:
            key = (method, route.path)
            if key in PROTECTED_ENDPOINTS:
                if not guarded:
                    raise ContractError(f"{key} has no token check")
                checked.add(key)
            if key in OPTIONAL_AUTH_ENDPOINTS:
                if not any(
                    dep.call is optional_user for dep in route.dependant.dependencies
                ):
                    raise ContractError(f"{key} has no optional token check")
                checked.add(key)
    missing = (PROTECTED_ENDPOINTS | OPTIONAL_AUTH_ENDPOINTS) - checked
    if missing:
        raise ContractError(f"token endpoints are not routed: {sorted(missing)}")


# 400 из контракта (валидация MS-4) и 400 самого gateway живут рядом как два примера.
def _merge_400(content: dict[str, Any], upstream: str) -> dict[str, Any]:
    media = content["application/json"]
    examples: dict[str, Any] = {}
    if "example" in media:
        examples["ms4_validation"] = {"value": media.pop("example")}
    else:
        examples.update(media.pop("examples", {}))
    examples["gateway_path"] = {"value": _gateway_example(400, upstream)}
    media["examples"] = examples
    return content


# FastAPI выкидывает null из примеров (exclude_none), а в контракте `menu: null`
# значим. Поэтому содержимое из снимка возвращается в схему как есть.
def install_openapi(
    app: FastAPI, routes: tuple[Route, ...], contract: dict[str, Any]
) -> None:
    def build() -> dict[str, Any]:
        if app.openapi_schema is None:
            schema = get_openapi(
                title=app.title,
                version=app.version,
                routes=app.routes,
                description=app.description,
            )
            for route in routes:
                for endpoint in route.endpoints:
                    source = contract["paths"][endpoint.upstream_path][
                        endpoint.method.lower()
                    ]
                    target = schema["paths"][endpoint.gateway_path][
                        endpoint.method.lower()
                    ]
                    for status, response in source["responses"].items():
                        content = copy.deepcopy(response["content"])
                        if status == "400" and endpoint not in _FIXED_PATH:
                            content = _merge_400(content, endpoint.upstream)
                        target["responses"][status]["content"] = content
                    if "requestBody" in source:
                        target["requestBody"] = copy.deepcopy(source["requestBody"])
                    # Токен необязателен: пустой вариант security означает «можно без».
                    if (
                        endpoint.method,
                        endpoint.gateway_path,
                    ) in OPTIONAL_AUTH_ENDPOINTS:
                        target["security"] = [{}, {"bearerAuth": []}]
            schema["paths"]["/metrics"] = metrics_path_item()
            app.openapi_schema = schema
        return app.openapi_schema

    app.openapi = build  # type: ignore[method-assign]
