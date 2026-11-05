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
        _env_file=None,
        upstream_recipes_url="http://recipes:8000",
        upstream_dictionary_url="http://dictionary:8000",
        **overrides,
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


async def test_redirect_is_not_followed_and_not_passed_on() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return reply(307, headers={"location": "http://recipes:8000/x"})

    with pytest.raises(UpstreamProtocolError):
        await forward(make_client(handler))
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


async def test_cart_uses_40_seconds_read_budget_without_changing_other_routes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return reply(200, str(request.extensions["timeout"]["read"]).encode())

    upstream = make_client(handler)
    try:
        cart = await forward(upstream, method="POST", route="/recipes/cart")
        other = await forward(upstream)
    finally:
        await upstream.aclose()
    assert float(cart.body) == 40
    assert float(other.body) == 10


async def test_cart_uses_its_own_total_deadline() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.15)
        return reply()

    upstream = make_client(
        handler, upstream_total_timeout_seconds=1, upstream_cart_timeout_seconds=0.05
    )
    try:
        with pytest.raises(UpstreamTimeout):
            await forward(upstream, method="POST", route="/recipes/cart")
        assert (await forward(upstream)).status_code == 200
    finally:
        await upstream.aclose()


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


async def test_pool_and_keepalive_limits_reach_the_transport() -> None:
    settings = Settings(
        _env_file=None,
        upstream_recipes_url="http://recipes:8000",
        upstream_dictionary_url="http://dictionary:8000",
        upstream_max_connections=7,
        upstream_keepalive_expiry_seconds=3,
    )
    client = UpstreamClient(settings, {"recipes": "http://recipes:8000"})
    try:
        pool = client.client._transport._pool  # type: ignore[attr-defined]
        assert pool._max_connections == 7
        assert pool._keepalive_expiry == 3
        assert pool._retries == 1
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    ("failure", "expected_up"),
    [
        (httpx.ConnectError("x"), 0.0),
        (httpx.ReadTimeout("x"), 0.0),
        (httpx.PoolTimeout("x"), 1.0),
        (httpx.RemoteProtocolError("x"), 1.0),
    ],
)
async def test_upstream_up_gauge_drops_only_when_the_service_is_unreachable(
    failure: Exception, expected_up: float
) -> None:
    from prometheus_client import CollectorRegistry

    from gateway.metrics import UpstreamMetrics

    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return reply(200, b"ok")
        raise failure

    registry = CollectorRegistry()
    settings = Settings(
        _env_file=None,
        upstream_recipes_url="http://recipes:8000",
        upstream_dictionary_url="http://dictionary:8000",
    )
    client = UpstreamClient(
        settings,
        {"recipes": "http://recipes:8000"},
        transport=httpx.MockTransport(handler),
        metrics=UpstreamMetrics(registry),
    )
    await forward(client)
    with pytest.raises(Exception):  # noqa: B017
        await forward(client)
    assert registry.get_sample_value(
        "gateway_upstream_up", {"upstream": "recipes"}
    ) == (expected_up)


async def test_response_too_large_keeps_the_gauge_up() -> None:
    from prometheus_client import CollectorRegistry

    from gateway.metrics import UpstreamMetrics

    registry = CollectorRegistry()
    settings = Settings(
        _env_file=None,
        upstream_recipes_url="http://recipes:8000",
        upstream_dictionary_url="http://dictionary:8000",
        upstream_max_response_bytes=1024,
    )
    client = UpstreamClient(
        settings,
        {"recipes": "http://recipes:8000"},
        transport=httpx.MockTransport(lambda r: reply(200, b"x" * 2000)),
        metrics=UpstreamMetrics(registry),
    )
    with pytest.raises(UpstreamResponseTooLarge):
        await forward(client)
    assert (
        registry.get_sample_value(
            "gateway_upstream_errors_total",
            {"upstream": "recipes", "reason": "response_too_large"},
        )
        == 1.0
    )
