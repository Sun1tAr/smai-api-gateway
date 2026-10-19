import httpx
import pytest

from gateway.config import Settings
from gateway.main import create_app
from tests.conftest import reply

BODY = b'{"habits":[],"diets":[],"allergies":[],"kitchenTools":[]}'


async def test_dictionaries_are_proxied_to_the_dictionary_service() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return reply(200, BODY, headers={"content-type": "application/json"})

    settings = Settings(
        _env_file=None,
        upstream_recipes_url="http://recipes:8000",
        upstream_dictionary_url="http://dictionary:8000",
        rate_limit_enabled=False,
    )
    app = create_app(settings, transport=httpx.MockTransport(handler))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        ok = await client.get("/dictionaries", params={"x": "1"})
        doubled = await client.get("/dictionaries/dictionaries")
        wrong_method = await client.post("/dictionaries")
    assert ok.status_code == 200
    assert ok.content == BODY
    assert [str(r.url) for r in seen] == ["http://dictionary:8000/dictionaries?x=1"]
    assert doubled.status_code == 404
    assert wrong_method.status_code == 405


def make_client(handler) -> httpx.AsyncClient:
    settings = Settings(
        _env_file=None,
        upstream_recipes_url="http://recipes:8000",
        upstream_dictionary_url="http://dictionary:8000",
        rate_limit_enabled=False,
    )
    app = create_app(settings, transport=httpx.MockTransport(handler))
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    )


@pytest.mark.parametrize(
    ("failure", "status", "reason"),
    [
        (httpx.ConnectError("refused"), 502, "connect"),
        (httpx.ReadTimeout("slow"), 504, "timeout"),
    ],
)
async def test_dictionary_failures_name_the_dictionary_upstream(
    failure: Exception, status: int, reason: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise failure

    async with make_client(handler) as client:
        response = await client.get("/dictionaries")
    assert response.status_code == status
    error = response.json()["error"]
    assert error["code"] == "EXTERNAL_SERVICE_ERROR"
    assert error["details"] == {"upstream": "dictionary", "reason": reason}


async def test_dictionary_service_500_passes_through() -> None:
    body = b'{"error":{"code":"INTERNAL_ERROR","message":"x","details":{}}}'

    def handler(request: httpx.Request) -> httpx.Response:
        return reply(500, body, headers={"content-type": "application/json"})

    async with make_client(handler) as client:
        response = await client.get("/dictionaries")
    assert response.status_code == 500
    assert response.content == body


async def test_trailing_slash_and_wrong_method_use_the_error_format() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not reach the service")

    async with make_client(handler) as client:
        missing = await client.get("/dictionaries/")
        wrong = await client.post("/dictionaries")
    assert missing.status_code == 404
    assert missing.json()["error"].keys() == {"code", "message", "details"}
    assert missing.json()["error"]["details"] == {}
    assert wrong.status_code == 405
    assert wrong.json()["error"]["code"] == "METHOD_NOT_ALLOWED"
    assert wrong.headers["allow"] == "GET"
