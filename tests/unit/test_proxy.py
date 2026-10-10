import asyncio
import gzip
from collections.abc import Callable

import httpx
import pytest

from gateway.config import Settings
from gateway.errors import (
    UpstreamOverloaded,
    UpstreamProtocolError,
    UpstreamResponseTooLarge,
    UpstreamTimeout,
    UpstreamUnavailable,
)
from gateway.proxy import UpstreamClient
from tests.conftest import reply

Handler = Callable[[httpx.Request], httpx.Response]


def make_client(handler: Handler, **overrides: object) -> UpstreamClient:
    settings = Settings(
        _env_file=None, upstream_recipes_url="http://recipes:8000", **overrides
    )
    return UpstreamClient(
        settings,
        {"recipes": "http://recipes:8000"},
        transport=httpx.MockTransport(handler),
    )


async def forward(client: UpstreamClient, **kwargs: object):
    options = {
        "upstream": "recipes",
        "method": "GET",
        "route": "/recipes/generate-menu/{task_id}",
        "target": b"/generate-menu/x",
        "headers": [],
        "body": b"",
    }
    options.update(kwargs)
    return await client.forward(**options)


async def test_status_headers_and_body_pass_through_unchanged() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return reply(
            404,
            b'{"error":{"code":"NOT_FOUND","message":"m","details":{}}}',
            headers={"content-type": "application/json", "x-extra": "1"},
        )

    response = await forward(make_client(handler))
    assert response.status_code == 404
    assert response.body.startswith(b'{"error"')
    assert (b"x-extra", b"1") in response.headers


async def test_request_goes_to_the_exact_target() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return reply(202, b'{"task_id":"t"}')

    await forward(
        make_client(handler),
        method="POST",
        target=b"/generate-menu?a=1&b=%2F",
        headers=[(b"content-type", b"application/json")],
        body=b'{"peopleCount":4}',
    )
    request = seen[0]
    assert request.method == "POST"
    assert request.url.raw_path == b"/generate-menu?a=1&b=%2F"
    assert request.url.host == "recipes"
    assert request.content == b'{"peopleCount":4}'
    assert request.headers["content-type"] == "application/json"
    assert request.headers["accept-encoding"] == "identity"


async def test_client_accept_encoding_is_respected() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return reply()

    await forward(make_client(handler), headers=[(b"accept-encoding", b"gzip")])
    assert seen[0].headers["accept-encoding"] == "gzip"


async def test_gzip_body_is_returned_raw_with_its_encoding_header() -> None:
    payload = gzip.compress(b'{"status":"done"}')

    def handler(request: httpx.Request) -> httpx.Response:
        return reply(200, payload, headers={"content-encoding": "gzip"})

    response = await forward(make_client(handler))
    assert response.body == payload
    assert (b"content-encoding", b"gzip") in response.headers


async def test_redirect_is_not_followed() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return reply(307, headers={"location": "http://recipes:8000/x"})

    response = await forward(make_client(handler))
    assert response.status_code == 307
    assert calls == 1


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (httpx.ConnectError("x"), UpstreamUnavailable),
        (httpx.ConnectTimeout("x"), UpstreamUnavailable),
        (httpx.ReadTimeout("x"), UpstreamTimeout),
        (httpx.WriteTimeout("x"), UpstreamTimeout),
        (httpx.PoolTimeout("x"), UpstreamOverloaded),
        (httpx.RemoteProtocolError("x"), UpstreamProtocolError),
        (httpx.ReadError("x"), UpstreamProtocolError),
        (httpx.WriteError("x"), UpstreamProtocolError),
    ],
)
async def test_transport_errors_are_mapped(error: Exception, expected: type) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise error

    with pytest.raises(expected) as raised:
        await forward(make_client(handler))
    assert raised.value.details["upstream"] == "recipes"


async def test_total_deadline_gives_504() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(1)
        return reply()

    client = make_client(handler, upstream_total_timeout_seconds=0.05)
    with pytest.raises(UpstreamTimeout):
        await forward(client)


async def test_response_over_the_limit_is_rejected_by_header() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return reply(200, b"x" * 2048, headers={"content-length": "2048"})

    client = make_client(handler, upstream_max_response_bytes=1024)
    with pytest.raises(UpstreamResponseTooLarge):
        await forward(client)


async def test_response_over_the_limit_is_rejected_while_reading() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return reply(200, b"x" * 800, b"x" * 800)

    client = make_client(handler, upstream_max_response_bytes=1024)
    with pytest.raises(UpstreamResponseTooLarge):
        await forward(client)


async def test_environment_proxy_variables_are_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:3128")
    client = make_client(lambda request: reply())
    assert client.client.trust_env is False
