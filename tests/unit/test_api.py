import httpx
import pytest

from gateway.main import create_app
from tests.conftest import Recorder

TASK = "b1e2c3d4-5678-90ab-cdef-1234567890ab"
REPLACE = f"/recipes/generate-menu/{TASK}/replace-recipe"


@pytest.mark.parametrize("status", [200, 400, 404, 500])
async def test_replace_recipe_is_public_and_passes_through(
    gateway: httpx.AsyncClient, upstream: Recorder, status: int
) -> None:
    body = b'{"recipeId":"00000000-0000-4000-8000-000000000001"}'
    upstream_body = b'{"recipe":{}}' if status == 200 else b'{"error":{}}'
    upstream.respond(status, upstream_body, {"content-type": "application/json"})
    response = await gateway.post(
        REPLACE + "?x=%2F",
        content=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer invalid-token",
            "Cookie": "session=fake",
            "X-User-Id": "42",
            "X-Auth-Role": "admin",
        },
    )
    assert response.status_code == status
    assert response.content == upstream_body
    (request,) = upstream.requests
    assert request.method == "POST"
    assert (
        request.url.raw_path == f"/generate-menu/{TASK}/replace-recipe?x=%2F".encode()
    )
    assert request.content == body
    assert request.headers["content-type"] == "application/json"
    assert {"authorization", "cookie", "x-user-id", "x-auth-role"}.isdisjoint(
        request.headers
    )


async def test_post_is_proxied_with_prefix_stripped(
    gateway: httpx.AsyncClient, upstream: Recorder
) -> None:
    body = b'{"peopleCount":4,"weeklyBudget":5000}'
    response = await gateway.post(
        "/recipes/generate-menu",
        content=body,
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 202
    assert response.json() == {"task_id": "b1e2c3d4"}
    request = upstream.requests[0]
    assert request.method == "POST"
    assert request.url.raw_path == b"/generate-menu"
    assert request.url.host == "recipes"
    assert request.content == body
    assert request.headers["content-type"] == "application/json"


async def test_get_is_proxied_with_path_param_and_query(
    gateway: httpx.AsyncClient, upstream: Recorder
) -> None:
    upstream.respond(200, {"status": "pending"})
    response = await gateway.get(f"/recipes/generate-menu/{TASK}?verbose=1&x=%2F")
    assert response.status_code == 200
    assert upstream.requests[0].url.raw_path == (
        f"/generate-menu/{TASK}?verbose=1&x=%2F".encode()
    )


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (400, {"error": {"code": "VALIDATION_ERROR", "message": "m", "details": {}}}),
        (404, {"error": {"code": "NOT_FOUND", "message": "m", "details": {}}}),
        (500, {"error": {"code": "INTERNAL_ERROR", "message": "m", "details": {}}}),
        (
            503,
            {"error": {"code": "SERVICE_UNAVAILABLE", "message": "m", "details": {}}},
        ),
    ],
)
async def test_upstream_errors_pass_through_unchanged(
    gateway: httpx.AsyncClient, upstream: Recorder, status: int, body: dict
) -> None:
    upstream.respond(status, body)
    response = await gateway.get(f"/recipes/generate-menu/{TASK}")
    assert response.status_code == status
    assert response.json() == body


async def test_non_json_upstream_error_is_passed_as_is(
    gateway: httpx.AsyncClient, upstream: Recorder
) -> None:
    upstream.respond(
        500, "Internal Server Error", headers={"content-type": "text/plain"}
    )
    response = await gateway.get(f"/recipes/generate-menu/{TASK}")
    assert response.status_code == 500
    assert response.text == "Internal Server Error"


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/recipes"),
        ("GET", "/recipes/"),
        ("GET", "/recipes/metrics"),
        ("GET", "/recipes/docs"),
        ("GET", "/recipes/redoc"),
        ("GET", "/recipes/openapi.json"),
        ("GET", "/recipes/health"),
        ("POST", "/recipes/generate-menu/"),
        ("GET", f"/recipes/generate-menu/{TASK}/"),
        ("GET", f"/recipes/generate-menu/{TASK}/extra"),
        ("POST", f"{REPLACE}/"),
        ("POST", f"/generate-menu/{TASK}/replace-recipe"),
        ("GET", "/recipesfoo/generate-menu"),
        ("GET", "/unknown/generate-menu"),
        ("POST", "//recipes/generate-menu"),
        ("POST", "/recipes//generate-menu"),
    ],
)
async def test_paths_outside_the_allowlist_are_404(
    gateway: httpx.AsyncClient, upstream: Recorder, method: str, path: str
) -> None:
    response = await gateway.request(method, path)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"
    assert upstream.requests == []


@pytest.mark.parametrize(
    ("method", "path", "allow"),
    [
        ("GET", "/recipes/generate-menu", "POST"),
        ("DELETE", "/recipes/generate-menu", "POST"),
        ("POST", f"/recipes/generate-menu/{TASK}", "GET"),
        ("PUT", f"/recipes/generate-menu/{TASK}", "GET"),
        ("GET", REPLACE, "POST"),
    ],
)
async def test_wrong_method_is_405_and_never_reaches_upstream(
    gateway: httpx.AsyncClient,
    upstream: Recorder,
    method: str,
    path: str,
    allow: str,
) -> None:
    response = await gateway.request(method, path)
    assert response.status_code == 405
    assert response.json()["error"]["code"] == "METHOD_NOT_ALLOWED"
    assert response.headers["allow"] == allow
    assert upstream.requests == []


@pytest.mark.parametrize(
    ("task_id", "status", "code"),
    [
        ("%2e%2e", 400, "VALIDATION_ERROR"),
        ("%2E%2e", 400, "VALIDATION_ERROR"),
        (".%2e", 400, "VALIDATION_ERROR"),
        ("%252e%252e", 400, "VALIDATION_ERROR"),
        ("%2f", 404, "NOT_FOUND"),
        ("%5c", 400, "VALIDATION_ERROR"),
        ("%00", 400, "VALIDATION_ERROR"),
        ("%0a", 400, "VALIDATION_ERROR"),
    ],
)
@pytest.mark.parametrize(
    ("method", "suffix"), [("GET", ""), ("POST", "/replace-recipe")]
)
async def test_traversal_in_task_id_never_reaches_upstream(
    gateway: httpx.AsyncClient,
    upstream: Recorder,
    task_id: str,
    status: int,
    code: str,
    method: str,
    suffix: str,
) -> None:
    response = await gateway.request(
        method, f"/recipes/generate-menu/{task_id}{suffix}"
    )
    assert (response.status_code, response.json()["error"]["code"]) == (status, code)
    assert upstream.requests == []


async def test_dot_segment_in_task_id_is_a_validation_error(
    gateway: httpx.AsyncClient, upstream: Recorder
) -> None:
    response = await gateway.get("/recipes/generate-menu/%2e%2e")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_declared_body_over_the_limit_is_413(
    gateway: httpx.AsyncClient, upstream: Recorder
) -> None:
    response = await gateway.post(
        "/recipes/generate-menu", content=b"x" * (64 * 1024 + 1)
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"
    assert upstream.requests == []


async def test_chunked_body_over_the_limit_is_413(
    gateway: httpx.AsyncClient, upstream: Recorder
) -> None:
    async def chunks():
        for _ in range(65):
            yield b"x" * 1024

    response = await gateway.post("/recipes/generate-menu", content=chunks())
    assert response.status_code == 413
    assert upstream.requests == []


async def test_body_at_the_limit_is_accepted(
    gateway: httpx.AsyncClient, upstream: Recorder
) -> None:
    response = await gateway.post("/recipes/generate-menu", content=b"x" * (64 * 1024))
    assert response.status_code == 202
    assert len(upstream.requests[0].content) == 64 * 1024


async def test_client_trust_headers_are_not_forwarded(
    gateway: httpx.AsyncClient, upstream: Recorder
) -> None:
    await gateway.get(
        f"/recipes/generate-menu/{TASK}",
        headers={
            "X-Forwarded-For": "6.6.6.6",
            "X-Forwarded-Host": "evil",
            "X-Real-IP": "6.6.6.6",
            "Forwarded": "for=6.6.6.6",
            "X-Request-ID": "client id",
            "X-User-Id": "admin",
            "Cookie": "s=1",
            "Traceparent": "00-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-bbbbbbbbbbbbbbbb-01",
            "Accept": "application/json",
        },
    )
    sent = upstream.requests[0].headers
    assert sent["x-forwarded-for"] == "127.0.0.1"
    assert sent["x-forwarded-proto"] == "http"
    assert sent["x-request-id"] != "client id"
    assert sent["accept"] == "application/json"
    for name in ("x-forwarded-host", "x-real-ip", "forwarded", "x-user-id", "cookie"):
        assert name not in sent
    assert "aaaaaaaa" not in sent.get("traceparent", "")


async def test_response_hop_by_hop_headers_are_dropped(
    gateway: httpx.AsyncClient, upstream: Recorder
) -> None:
    upstream.respond(
        200,
        b'{"status":"pending"}',
        headers={
            "connection": "x-foo",
            "x-foo": "named by connection",
            "keep-alive": "timeout=5",
            "server": "internal-app",
            "x-extra": "1",
        },
    )
    response = await gateway.get(f"/recipes/generate-menu/{TASK}")
    for name in ("keep-alive", "connection", "x-foo", "server"):
        assert name not in response.headers
    assert response.headers["x-extra"] == "1"
    assert int(response.headers["content-length"]) == len(response.content)


async def test_upstream_redirect_is_502_and_location_does_not_leak(
    gateway: httpx.AsyncClient, upstream: Recorder
) -> None:
    upstream.respond(307, b"", headers={"location": "http://recipes:8000/x"})
    response = await gateway.get(f"/recipes/generate-menu/{TASK}")
    assert response.status_code == 502
    assert response.json()["error"]["details"]["reason"] == "protocol"
    assert "location" not in response.headers
    assert "8000" not in response.text


async def test_get_body_is_not_read_or_forwarded(
    gateway: httpx.AsyncClient, upstream: Recorder
) -> None:
    response = await gateway.request(
        "GET", f"/recipes/generate-menu/{TASK}", content=b"x" * 200_000
    )
    assert response.status_code == 202
    assert upstream.requests[0].content == b""


async def test_edge_guard_runs_on_every_proxied_route(settings, upstream) -> None:
    from fastapi.routing import APIRoute

    from gateway.api import edge_guard

    app = create_app(settings, transport=httpx.MockTransport(upstream))
    proxied = [
        r
        for r in app.routes
        if isinstance(r, APIRoute) and r.path.startswith("/recipes")
    ]
    assert len(proxied) == 9
    for route in proxied:
        # Токен (если он есть у маршрута) проверяется раньше лимитера.
        assert route.dependant.dependencies[-1].call is edge_guard


async def test_upstream_down_is_502_in_error_format(settings) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    from gateway.main import create_app

    app = create_app(settings, transport=httpx.MockTransport(refuse))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway"
    ) as client:
        response = await client.get(f"/recipes/generate-menu/{TASK}")
    assert response.status_code == 502
    assert response.json()["error"] == {
        "code": "EXTERNAL_SERVICE_ERROR",
        "message": "Upstream service is unavailable",
        "details": {"upstream": "recipes", "reason": "connect"},
    }


async def test_encoded_literal_segment_is_rejected_not_normalized(
    gateway: httpx.AsyncClient, upstream: Recorder
) -> None:
    response = await gateway.get("/recipes/generate%2Dmenu/x")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert upstream.requests == []
