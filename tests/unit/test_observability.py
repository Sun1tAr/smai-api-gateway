import json
import re
import uuid

import httpx
import pytest
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import SpanKind

from gateway.config import Settings
from gateway.main import create_app
from tests.conftest import Recorder, reply

TASK = "b1e2c3d4-5678-90ab-cdef-1234567890ab"
CLIENT_TRACE = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


def client_for(app, **kwargs) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, **kwargs), base_url="http://gateway"
    )


async def test_valid_client_request_id_is_echoed_and_forwarded(
    gateway: httpx.AsyncClient, upstream: Recorder
) -> None:
    response = await gateway.get(
        f"/recipes/generate-menu/{TASK}", headers={"X-Request-ID": "mobile-42.a_b"}
    )
    assert response.headers["x-request-id"] == "mobile-42.a_b"
    assert upstream.requests[0].headers.get_list("x-request-id") == ["mobile-42.a_b"]


@pytest.mark.parametrize("bad", ["has space", "x" * 129, "a/b", "a;b", ""])
async def test_unsafe_client_request_id_is_replaced(
    gateway: httpx.AsyncClient, upstream: Recorder, bad: str
) -> None:
    response = await gateway.get(
        f"/recipes/generate-menu/{TASK}", headers={"X-Request-ID": bad}
    )
    generated = response.headers["x-request-id"]
    assert uuid.UUID(generated)
    assert upstream.requests[0].headers.get_list("x-request-id") == [generated]


async def test_upstream_request_id_is_not_passed_to_the_client(
    gateway: httpx.AsyncClient, upstream: Recorder
) -> None:
    upstream.respond(200, b"{}", headers={"x-request-id": "from-upstream"})
    response = await gateway.get(f"/recipes/generate-menu/{TASK}")
    assert response.headers.get_list("x-request-id") != ["from-upstream"]
    assert len(response.headers.get_list("x-request-id")) == 1


@pytest.mark.parametrize(
    ("method", "path", "status"),
    [
        ("GET", "/nope", 404),
        ("DELETE", "/recipes/generate-menu", 405),
        ("GET", "/recipes/generate-menu/%2e%2e", 400),
        ("GET", "/health", 200),
    ],
)
async def test_request_id_is_on_every_response(
    gateway: httpx.AsyncClient, method: str, path: str, status: int
) -> None:
    response = await gateway.request(method, path, headers={"X-Request-ID": "rid-1"})
    assert response.status_code == status
    assert response.headers["x-request-id"] == "rid-1"


async def test_request_id_on_413(gateway: httpx.AsyncClient) -> None:
    response = await gateway.post(
        "/recipes/generate-menu",
        content=b"x" * (64 * 1024 + 1),
        headers={"X-Request-ID": "rid-413"},
    )
    assert response.status_code == 413
    assert response.headers["x-request-id"] == "rid-413"


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (httpx.ConnectError("x"), 502),
        (httpx.PoolTimeout("x"), 503),
        (httpx.ReadTimeout("x"), 504),
        (RuntimeError("bug"), 500),
    ],
)
async def test_request_id_on_upstream_failures_and_unhandled_errors(
    settings: Settings, error: Exception, status: int
) -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise error

    app = create_app(settings, transport=httpx.MockTransport(fail))
    async with client_for(app) as client:
        response = await client.get(
            f"/recipes/generate-menu/{TASK}", headers={"X-Request-ID": "rid-x"}
        )
    assert response.status_code == status
    assert response.headers["x-request-id"] == "rid-x"
    assert set(response.json()["error"]) == {"code", "message", "details"}


async def test_unhandled_error_does_not_leak_details(settings: Settings) -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise RuntimeError("secret internal detail")

    app = create_app(settings, transport=httpx.MockTransport(fail))
    async with client_for(app) as client:
        response = await client.get(f"/recipes/generate-menu/{TASK}")
    assert response.status_code == 500
    assert "secret internal detail" not in response.text


async def metrics_text(client: httpx.AsyncClient) -> str:
    return (await client.get("/metrics")).text


async def test_upstream_metrics_use_route_templates(
    gateway: httpx.AsyncClient,
) -> None:
    await gateway.get(f"/recipes/generate-menu/{TASK}")
    await gateway.post("/recipes/generate-menu", content=b"{}")
    text = await metrics_text(gateway)
    assert (
        'gateway_upstream_requests_total{method="GET",route="/recipes/generate-menu/'
        '{task_id}",status="202",upstream="recipes"} 1.0' in text
    )
    assert 'route="/recipes/generate-menu",status="202"' in text
    assert 'gateway_upstream_up{upstream="recipes"} 1.0' in text
    assert "gateway_upstream_request_duration_seconds_bucket" in text
    assert TASK not in text


async def test_http_metrics_are_exposed_by_template(
    gateway: httpx.AsyncClient,
) -> None:
    await gateway.get(f"/recipes/generate-menu/{TASK}")
    text = await metrics_text(gateway)
    assert "http_requests_total" in text
    assert TASK not in text


async def test_failures_are_counted_by_reason_and_mark_upstream_down(
    settings: Settings,
) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    app = create_app(settings, transport=httpx.MockTransport(refuse))
    async with client_for(app) as client:
        await client.get(f"/recipes/generate-menu/{TASK}")
        text = await metrics_text(client)
    assert (
        'gateway_upstream_errors_total{reason="connect",upstream="recipes"} 1.0' in text
    )
    assert 'gateway_upstream_up{upstream="recipes"} 0.0' in text


async def test_pool_exhaustion_does_not_mark_upstream_down(
    settings: Settings,
) -> None:
    def ok(request: httpx.Request) -> httpx.Response:
        return reply(200)

    def pool(request: httpx.Request) -> httpx.Response:
        raise httpx.PoolTimeout("busy")

    state = {"handler": ok}
    app = create_app(
        settings, transport=httpx.MockTransport(lambda r: state["handler"](r))
    )
    async with client_for(app) as client:
        await client.get(f"/recipes/generate-menu/{TASK}")
        state["handler"] = pool
        await client.get(f"/recipes/generate-menu/{TASK}")
        text = await metrics_text(client)
    assert 'gateway_upstream_errors_total{reason="pool",upstream="recipes"} 1.0' in text
    assert 'gateway_upstream_up{upstream="recipes"} 1.0' in text


async def test_trace_starts_at_the_gateway_and_continues_to_the_upstream(
    settings: Settings, upstream: Recorder
) -> None:
    app = create_app(settings, transport=httpx.MockTransport(upstream))
    exporter = InMemorySpanExporter()
    app.state.tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
    async with client_for(app) as client:
        await client.get(
            f"/recipes/generate-menu/{TASK}",
            headers={
                "traceparent": f"00-{CLIENT_TRACE}-bbbbbbbbbbbbbbbb-01",
                "tracestate": "k=v",
                "baggage": "user=1",
            },
        )
    sent = upstream.requests[0].headers
    forwarded_trace = sent["traceparent"].split("-")[1]
    server_spans = [
        s for s in exporter.get_finished_spans() if s.kind == SpanKind.SERVER
    ]
    assert server_spans
    assert f"{server_spans[0].context.trace_id:032x}" == forwarded_trace
    assert forwarded_trace != CLIENT_TRACE
    assert server_spans[0].parent is None
    assert "baggage" not in sent
    assert "k=v" not in sent.get("tracestate", "")


async def test_logs_are_json_and_do_not_contain_query_or_body(
    settings: Settings, upstream: Recorder, capsys: pytest.CaptureFixture[str]
) -> None:
    app = create_app(settings, transport=httpx.MockTransport(upstream))
    async with client_for(app) as client:
        await client.post(
            "/recipes/generate-menu?token=SECRETQUERY",
            content=b'{"note":"SECRETBODY"}',
            headers={
                "Authorization": "Bearer SECRETTOKEN",
                "Cookie": "s=SECRETCOOKIE",
            },
        )
    output = capsys.readouterr().out
    events = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
    messages = {event["message"] for event in events}
    assert {"http_request", "upstream_request"} <= messages
    for secret in ("SECRETQUERY", "SECRETBODY", "SECRETTOKEN", "SECRETCOOKIE"):
        assert secret not in output
    request_event = next(e for e in events if e["message"] == "http_request")
    assert request_event["path"] == "/recipes/generate-menu"
    assert re.fullmatch(r"[0-9a-f]{32}", request_event["trace_id"])
    assert request_event["request_id"]
