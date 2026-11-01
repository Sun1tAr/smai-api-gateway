from uuid import UUID

import pytest

from tests.integration.conftest import free_port

TASK = "b1e2c3d4-5678-90ab-cdef-1234567890ab"
pytestmark = pytest.mark.integration


async def test_api1_reaches_the_upstream_with_prefix_stripped(
    stub_url, stub_requests, open_gateway
) -> None:
    async with open_gateway(stub_url) as gateway:
        response = await gateway.post(
            "/recipes/generate-menu", json={"peopleCount": 4, "weeklyBudget": 5000}
        )
    assert response.status_code == 202
    assert response.json() == {"task_id": TASK}
    assert response.headers["content-type"] == "application/json"
    seen = stub_requests[0]
    assert (seen["method"], seen["target"]) == ("POST", "/generate-menu")
    assert seen["headers"]["x-forwarded-for"] == "127.0.0.1"
    assert seen["headers"]["x-request-id"] == response.headers["x-request-id"]


async def test_api2_and_upstream_error_formats_pass_through(
    stub_url, stub_requests, open_gateway
) -> None:
    async with open_gateway(stub_url) as gateway:
        done = await gateway.get(f"/recipes/generate-menu/{TASK}")
        pending = await gateway.get("/recipes/generate-menu/pending")
        missing = await gateway.get("/recipes/generate-menu/unknown")
        invalid = await gateway.post("/recipes/generate-menu", json={"peopleCount": 0})
        broken = await gateway.get("/recipes/generate-menu/crash")
    assert done.json()["status"] == "done"
    recipes = done.json()["menu"]["recipes"]
    assert len(recipes) == 3
    assert {recipe["name"]: recipe["recipeId"] for recipe in recipes} == {
        "Рецепт 0": "00000000-0000-4000-8000-000000000001",
        "Рецепт 1": "00000000-0000-4000-8000-000000000002",
        "Рецепт 2": "00000000-0000-4000-8000-000000000003",
    }
    assert all(
        str(UUID(recipe["recipeId"])) == recipe["recipeId"] for recipe in recipes
    )
    assert pending.json() == {"status": "pending", "menu": None, "error": None}
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "NOT_FOUND"
    assert invalid.status_code == 400
    assert invalid.json()["error"]["code"] == "VALIDATION_ERROR"
    assert broken.status_code == 500
    assert broken.text == "Internal Server Error"


async def test_query_reaches_the_upstream_byte_for_byte(
    stub_url, stub_requests, open_gateway
) -> None:
    async with open_gateway(stub_url) as gateway:
        await gateway.get(f"/recipes/generate-menu/{TASK}?a=1&b=%D0%B0&c=%2F")
    assert stub_requests[0]["target"] == f"/generate-menu/{TASK}?a=1&b=%D0%B0&c=%2F"


async def test_closed_port_gives_502(open_gateway) -> None:
    async with open_gateway(f"http://127.0.0.1:{free_port()}") as gateway:
        response = await gateway.get(f"/recipes/generate-menu/{TASK}")
        metrics = (await gateway.get("/metrics")).text
    assert response.status_code == 502
    assert response.json()["error"]["details"] == {
        "upstream": "recipes",
        "reason": "connect",
    }
    assert 'gateway_upstream_up{upstream="recipes"} 0.0' in metrics


async def test_slow_upstream_gives_504(stub_url, open_gateway) -> None:
    async with open_gateway(stub_url) as gateway:
        response = await gateway.get("/recipes/generate-menu/slow")
    assert response.status_code == 504
    assert response.json()["error"]["code"] == "EXTERNAL_SERVICE_ERROR"
    assert response.json()["error"]["details"]["reason"] == "timeout"


async def test_gzip_response_is_delivered_intact(stub_url, open_gateway) -> None:
    async with open_gateway(stub_url) as gateway:
        response = await gateway.get(
            "/recipes/generate-menu/compressed", headers={"Accept-Encoding": "gzip"}
        )
    assert response.headers["content-encoding"] == "gzip"
    assert response.json()["status"] == "done"
    # Передано ровно столько байт, сколько объявлено в Content-Length.
    assert response.num_bytes_downloaded == int(response.headers["content-length"])


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/recipes/health"),
        ("GET", "/recipes/metrics"),
        ("GET", "/recipes/docs"),
        ("GET", "/recipes/openapi.json"),
        ("GET", "/recipes"),
        ("GET", "/recipes/generate-menu"),
        ("POST", f"/recipes/generate-menu/{TASK}"),
        ("GET", "/recipes/generate-menu/%2e%2e"),
        ("GET", "/recipes/generate-menu/%2f..%2fmetrics"),
    ],
)
async def test_internal_paths_of_the_upstream_stay_unreachable(
    stub_url, stub_requests, open_gateway, method: str, path: str
) -> None:
    async with open_gateway(stub_url) as gateway:
        response = await gateway.request(method, path)
    assert response.status_code in (400, 404, 405)
    assert response.json()["error"]["code"] in (
        "VALIDATION_ERROR",
        "NOT_FOUND",
        "METHOD_NOT_ALLOWED",
    )
    assert stub_requests == []


async def test_service_endpoints_of_the_gateway_itself(stub_url, open_gateway) -> None:
    async with open_gateway(stub_url) as gateway:
        assert (await gateway.get("/health")).json() == {"status": "ok"}
        assert (await gateway.get("/metrics")).status_code == 200
        assert (await gateway.get("/docs")).status_code == 200
        schema = (await gateway.get("/openapi.json")).json()
    assert set(schema["paths"]) == {
        "/auth/login",
        "/auth/logout",
        "/auth/register",
        "/dictionaries",
        "/health",
        "/metrics",
        "/recipes/generate-menu",
        "/recipes/generate-menu/{task_id}",
        "/recipes/profile/parameters",
        "/user/profile",
    }


async def test_sequential_requests_reuse_the_connection(
    stub_url, stub_requests, open_gateway
) -> None:
    async with open_gateway(stub_url) as gateway:
        for _ in range(5):
            response = await gateway.get(f"/recipes/generate-menu/{TASK}")
            assert response.status_code == 200
    assert len(stub_requests) == 5
    # Один и тот же исходящий порт: соединение с MS-4 переиспользуется.
    assert len({request["client_port"] for request in stub_requests}) == 1


async def test_register_then_login_through_the_gateway_against_a_real_user_service(
    open_gateway, stub_url: str, user_stub_url: str, jwt_key_file
) -> None:
    import jwt

    from gateway.tokens import load_private_key

    credentials = {"login": "User@Example.com", "password": "correct-horse-battery"}
    async with open_gateway(stub_url, upstream_user_url=user_stub_url) as gateway:
        registered = await gateway.post("/auth/register", json=credentials)
        duplicate = await gateway.post("/auth/register", json=credentials)
        logged_in = await gateway.post("/auth/login", json=credentials)
        wrong = await gateway.post(
            "/auth/login", json={**credentials, "password": "wrong-password-1"}
        )
        invalid = await gateway.post("/auth/login", content=b"nope")
    assert (registered.status_code, logged_in.status_code) == (201, 200)
    assert (duplicate.status_code, wrong.status_code, invalid.status_code) == (
        409,
        401,
        400,
    )
    public_key = load_private_key(jwt_key_file).public_key()
    for response in (registered, logged_in):
        body = response.json()
        claims = jwt.decode(
            body["token"], public_key, algorithms=["ES256"], issuer="smai-gateway"
        )
        assert body["userId"] == 1 and claims["sub"] == "1" and claims["jti"]
