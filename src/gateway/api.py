import copy
import json
import re
from pathlib import Path
from typing import Any

import structlog
from fastapi import Depends, FastAPI, Request, Response
from fastapi.openapi.utils import get_openapi

from gateway.errors import ErrorResponse, InvalidRequestPath, RateLimited
from gateway.health import metrics_path_item
from gateway.policy import client_response_headers, upstream_request_headers
from gateway.proxy import UpstreamClient, read_limited_body
from gateway.ratelimit import (
    LIMITED_ENDPOINTS,
    RateLimiter,
    rate_limit_key,
    resolve_client_ip,
)
from gateway.routing import Endpoint, InvalidPath, Route, upstream_target

logger = structlog.get_logger()
_BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})
_PARAM = re.compile(r"\{[a-z][a-z0-9_]*\}")


class ContractError(ValueError):
    """Снимок контракта не описывает разрешённый путь; сервис не стартует."""


def load_contract(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ContractError(f"cannot read contract: {type(error).__name__}") from None


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
        "Слишком много запросов с одного адреса: превышен лимит "
        "`RATE_LIMIT_REQUESTS` за `RATE_LIMIT_WINDOW_SECONDS`. Через сколько секунд "
        "повторить, указано в `Retry-After` и `details.retry_after_seconds`. "
        "Отклонённые запросы в лимит не засчитываются.",
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
    responses: dict[int | str, dict[str, Any]] = {}
    for status in sorted(_GATEWAY_ERRORS):
        if status == 413 and endpoint.method not in _BODY_METHODS:
            continue
        if status == 429 and (endpoint.method, endpoint.gateway_path) not in (
            LIMITED_ENDPOINTS
        ):
            continue
        responses[status] = _gateway_response(status, endpoint.upstream, limit)
    # Ответы MS-4 описывают его собственные ошибки и перекрывают общие.
    for status, response in operation["responses"].items():
        response = dict(_contract_response(where, status, response))
        if status == "400":
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


def request_client_ip(request: Request) -> str:
    """Адрес клиента с учётом доверенных прокси (TRUSTED_PROXIES)."""
    return resolve_client_ip(
        request.client.host if request.client else None,
        request.headers.getlist("x-forwarded-for"),
        request.app.state.settings.trusted_networks,
    )


# Вход в проксируемый маршрут: лимит запросов; сюда же встанет проверка пользователя.
async def edge_guard(request: Request) -> None:
    limiter: RateLimiter | None = request.app.state.rate_limiter
    route = request.scope.get("route")
    if limiter is None or (request.method, getattr(route, "path", "")) not in (
        LIMITED_ENDPOINTS
    ):
        return
    key = rate_limit_key(request_client_ip(request))
    retry_after = limiter.check(key)
    if retry_after is None:
        return
    request.app.state.rate_limit_metrics.rejected.labels(route.path).inc()
    logger.warning(
        "rate_limited",
        route=route.path,
        client=limiter.fingerprint(key),
        retry_after_seconds=retry_after,
    )
    raise RateLimited(retry_after)


def _register(
    app: FastAPI, endpoint: Endpoint, docs: dict[str, Any], limit: int
) -> None:
    async def handler(request: Request) -> Response:
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
        request_id = request.state.request_id
        headers = upstream_request_headers(
            request.headers.raw,
            request_id=request_id,
            client_ip=request_client_ip(request),
            scheme=request.url.scheme,
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
        tags=[endpoint.prefix],
        response_class=Response,
        dependencies=[Depends(edge_guard)],
        **docs,
    )


def register_routes(
    app: FastAPI, routes: tuple[Route, ...], contract: dict[str, Any], limit: int
) -> None:
    for route in routes:
        for endpoint in route.endpoints:
            _register(app, endpoint, _operation_docs(endpoint, contract, limit), limit)


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
                        if status == "400":
                            content = _merge_400(content, endpoint.upstream)
                        target["responses"][status]["content"] = content
                    if "requestBody" in source:
                        target["requestBody"] = copy.deepcopy(source["requestBody"])
            schema["paths"]["/metrics"] = metrics_path_item()
            app.openapi_schema = schema
        return app.openapi_schema

    app.openapi = build  # type: ignore[method-assign]
