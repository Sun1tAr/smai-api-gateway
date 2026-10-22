"""Сквозной SMAI-102: gateway, заглушка MS-3 (токены) и заглушка MS-4 (владелец)."""

import pytest

from tests.stubs.user_stub import app as user_stub_app  # noqa: F401

pytestmark = pytest.mark.integration
CREDENTIALS = {"login": "user@example.com", "password": "correct-horse-battery"}
BODY = {"peopleCount": 4, "weeklyBudget": 5000}
# Порт 1 закрыт: Redis «упал».
DEAD_REDIS = "redis://127.0.0.1:1/0"


async def register(gateway) -> tuple[int, str]:
    response = await gateway.post("/auth/register", json=CREDENTIALS)
    assert response.status_code == 201
    return response.json()["userId"], response.json()["token"]


def requests_to(stub_requests: list[dict], target: str) -> list[dict]:
    return [r for r in stub_requests if r["target"] == target]


async def test_owner_reaches_ms4_only_from_the_token(
    stub_url, user_stub_url, stub_requests, real_redis_url, open_auth_gateway
) -> None:
    async with open_auth_gateway(stub_url, user_stub_url, real_redis_url) as gateway:
        user_id, token = await register(gateway)
        headers = {"Authorization": f"Bearer {token}", "X-User-Id": "999"}
        created = await gateway.post(
            "/recipes/generate-menu", json=BODY, headers=headers
        )
        guest = await gateway.post(
            "/recipes/generate-menu", json=BODY, headers={"X-User-Id": "999"}
        )
        parameters = await gateway.get("/recipes/profile/parameters", headers=headers)
        anonymous = await gateway.get("/recipes/profile/parameters")
    assert created.status_code == guest.status_code == 202
    owned, anonymous_post = requests_to(stub_requests, "/generate-menu")
    assert owned["headers"]["x-user-id"] == str(user_id)
    assert "authorization" not in owned["headers"]
    assert "x-user-id" not in anonymous_post["headers"]
    assert parameters.status_code == 200
    assert parameters.json()["diet"] is None
    assert anonymous.status_code == 401


async def test_saved_parameters_are_passed_back_for_the_owner(
    stub_url,
    user_stub_url,
    stub_requests,
    real_redis_url,
    open_auth_gateway,
    monkeypatch,
) -> None:
    from tests.stubs import recipes_stub

    async with open_auth_gateway(stub_url, user_stub_url, real_redis_url) as gateway:
        user_id, token = await register(gateway)
        monkeypatch.setitem(
            recipes_stub.SAVED_PARAMETERS, user_id, recipes_stub.SAVED_PARAMETERS[7]
        )
        response = await gateway.get(
            "/recipes/profile/parameters",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.json() == recipes_stub.SAVED_PARAMETERS[7]


async def test_redis_down_token_request_becomes_a_guest_request(
    stub_url, user_stub_url, stub_requests, open_auth_gateway
) -> None:
    async with open_auth_gateway(stub_url, user_stub_url, DEAD_REDIS) as gateway:
        _, token = await register(gateway)
        headers = {"Authorization": f"Bearer {token}"}
        created = await gateway.post(
            "/recipes/generate-menu", json=BODY, headers=headers
        )
        parameters = await gateway.get("/recipes/profile/parameters", headers=headers)
    assert created.status_code == 202
    (sent,) = requests_to(stub_requests, "/generate-menu")
    assert "x-user-id" not in sent["headers"]
    assert parameters.status_code == 503
    assert parameters.json()["error"]["details"]["upstream"] == "redis"
