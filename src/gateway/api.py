import copy
import json
import re
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.openapi.utils import get_openapi

from gateway.errors import ErrorResponse, InvalidRequestPath
from gateway.policy import client_response_headers, upstream_request_headers
from gateway.proxy import UpstreamClient, read_limited_body
from gateway.routing import Endpoint, InvalidPath, Route, upstream_target

_BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})
_PARAM = re.compile(r"\{[a-z][a-z0-9_]*\}")


class ContractError(ValueError):
    """Снимок контракта не описывает разрешённый путь; сервис не стартует."""


def load_contract(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ContractError(f"cannot read contract: {type(error).__name__}") from None


# Ошибки, которые отвечает сам gateway, описываются у каждой проксируемой операции.
_GATEWAY_ERRORS: dict[int, tuple[str, str, str]] = {
    400: (
        "Gateway отклонил недопустимые символы или закодированные разделители "
        "в пути или query.",
        "VALIDATION_ERROR",
        "Request path is invalid",
    ),
    413: (
        "Тело запроса больше `MAX_REQUEST_BODY_BYTES`.",
        "PAYLOAD_TOO_LARGE",
        "Request body is too large",
    ),
    502: (
        "Сервис недоступен или оборвал соединение.",
        "EXTERNAL_SERVICE_ERROR",
        "Upstream service is unavailable",
    ),
    503: (
        "Gateway перегружен: исчерпан пул соединений к сервису. Ответ 503 самого "
        "сервиса приходит как есть и отличается кодом ошибки.",
        "EXTERNAL_SERVICE_ERROR",
        "Gateway is overloaded, try again later",
    ),
    504: (
        "Сервис не ответил за отведённое время. Запрос мог быть выполнен.",
        "EXTERNAL_SERVICE_ERROR",
        "Upstream service did not respond in time",
    ),
}


def _gateway_response(status: int, description: str, code: str, message: str) -> dict:
    details = {"upstream": "recipes", "reason": "timeout"} if status >= 500 else {}
    return {
        "model": ErrorResponse,
        "description": description,
        "content": {
            "application/json": {
                "example": {
                    "error": {"code": code, "message": message, "details": details}
                }
            }
        },
    }


def _operation_docs(endpoint: Endpoint, contract: dict[str, Any]) -> dict[str, Any]:
    try:
        operation = contract["paths"][endpoint.upstream_path][endpoint.method.lower()]
    except KeyError:
        raise ContractError(
            f"contract has no {endpoint.method} {endpoint.upstream_path}"
        ) from None
    responses: dict[int | str, dict[str, Any]] = {}
    for status, (description, code, message) in sorted(_GATEWAY_ERRORS.items()):
        if status == 413 and endpoint.method not in _BODY_METHODS:
            continue
        responses[status] = _gateway_response(status, description, code, message)
    # Ответы MS-4 описывают его собственные ошибки и перекрывают общие.
    for status, response in operation["responses"].items():
        response = dict(response)
        if status == "400":
            response["description"] += " " + _GATEWAY_ERRORS[400][0]
        responses[status] = response
    extra: dict[str, Any] = {}
    if "requestBody" in operation:
        extra["requestBody"] = operation["requestBody"]
    if "parameters" in operation:
        extra["parameters"] = operation["parameters"]
    elif _PARAM.search(endpoint.upstream_path):
        raise ContractError(f"contract has no parameters for {endpoint.upstream_path}")
    successes = sorted(int(s) for s in operation["responses"] if s.startswith("2"))
    return {
        "summary": operation["summary"],
        "description": operation["description"],
        "responses": responses,
        "openapi_extra": extra,
        "status_code": successes[0],
    }


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
        body = await read_limited_body(request, limit)
        request_id = request.state.request_id
        headers = upstream_request_headers(
            request.headers.raw,
            request_id=request_id,
            client_ip=request.client.host if request.client else None,
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
        **docs,
    )


def register_routes(
    app: FastAPI, routes: tuple[Route, ...], contract: dict[str, Any], limit: int
) -> None:
    for route in routes:
        for endpoint in route.endpoints:
            _register(app, endpoint, _operation_docs(endpoint, contract), limit)


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
                        target["responses"][status]["content"] = copy.deepcopy(
                            response["content"]
                        )
                    if "requestBody" in source:
                        target["requestBody"] = copy.deepcopy(source["requestBody"])
            app.openapi_schema = schema
        return app.openapi_schema

    app.openapi = build  # type: ignore[method-assign]
