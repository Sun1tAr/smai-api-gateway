import httpx
import pytest
from fastapi import FastAPI

from gateway.config import Settings
from gateway.errors import (
    GatewayError,
    InvalidRequestPath,
    PayloadTooLarge,
    UpstreamOverloaded,
    UpstreamProtocolError,
    UpstreamResponseTooLarge,
    UpstreamTimeout,
    UpstreamUnavailable,
    register_error_handlers,
)
from gateway.main import create_app


def build_app() -> FastAPI:
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/items/{item_id}")
    async def item(item_id: int) -> dict[str, int]:
        return {"id": item_id}

    @app.get("/boom/{kind}")
    async def boom(kind: str) -> None:
        errors: dict[str, GatewayError] = {
            "path": InvalidRequestPath(),
            "large": PayloadTooLarge(),
            "unavailable": UpstreamUnavailable("recipes"),
            "protocol": UpstreamProtocolError("recipes"),
            "toolarge": UpstreamResponseTooLarge("recipes"),
            "timeout": UpstreamTimeout("recipes"),
            "pool": UpstreamOverloaded("recipes"),
        }
        raise errors[kind]

    return app


async def get(path: str, method: str = "GET") -> httpx.Response:
    transport = httpx.ASGITransport(app=build_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        return await client.request(method, path)


def assert_error_shape(response: httpx.Response, code: str) -> dict:
    body = response.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "details"}
    assert body["error"]["code"] == code
    assert isinstance(body["error"]["details"], dict)
    return body["error"]


@pytest.mark.parametrize(
    ("kind", "status", "code", "reason"),
    [
        ("path", 400, "VALIDATION_ERROR", None),
        ("large", 413, "PAYLOAD_TOO_LARGE", None),
        ("unavailable", 502, "EXTERNAL_SERVICE_ERROR", "connect"),
        ("protocol", 502, "EXTERNAL_SERVICE_ERROR", "protocol"),
        ("toolarge", 502, "EXTERNAL_SERVICE_ERROR", "response_too_large"),
        ("timeout", 504, "EXTERNAL_SERVICE_ERROR", "timeout"),
        ("pool", 503, "EXTERNAL_SERVICE_ERROR", "pool"),
    ],
)
async def test_gateway_errors(
    kind: str, status: int, code: str, reason: str | None
) -> None:
    response = await get(f"/boom/{kind}")
    assert response.status_code == status
    error = assert_error_shape(response, code)
    if reason is not None:
        assert error["details"] == {"upstream": "recipes", "reason": reason}


async def test_validation_error_is_400_without_input_values() -> None:
    response = await get("/items/secret-value")
    assert response.status_code == 400
    error = assert_error_shape(response, "VALIDATION_ERROR")
    assert "secret-value" not in response.text
    assert error["details"]["fields"][0]["loc"] == ["path", "item_id"]


async def test_unknown_path_is_404() -> None:
    response = await get("/nope")
    assert response.status_code == 404
    assert_error_shape(response, "NOT_FOUND")


async def test_wrong_method_is_405_with_allow() -> None:
    response = await get("/items/1", method="POST")
    assert response.status_code == 405
    assert_error_shape(response, "METHOD_NOT_ALLOWED")
    assert response.headers["allow"] == "GET"


async def test_create_app_uses_the_same_format() -> None:
    app = create_app(Settings(_env_file=None, upstream_recipes_url="http://r:8000"))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        response = await client.get("/missing")
    assert response.status_code == 404
    assert_error_shape(response, "NOT_FOUND")
