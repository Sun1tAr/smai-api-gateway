import asyncio
import time

import jwt
import pytest
from redis.asyncio import Redis

from tests.integration.conftest import free_port
from tests.stubs.user_stub import app as user_stub_app

pytestmark = pytest.mark.integration
CREDENTIALS = {"login": "user@example.com", "password": "correct-horse-battery"}


async def register(gateway) -> tuple[int, str]:
    response = await gateway.post("/auth/register", json=CREDENTIALS)
    assert response.status_code == 201
    return response.json()["userId"], response.json()["token"]


async def test_logout_then_the_same_token_gets_401_on_real_redis(
    stub_url, user_stub_url, real_redis_url, open_auth_gateway
) -> None:
    redis = Redis.from_url(real_redis_url)
    await redis.flushdb()
    try:
        async with open_auth_gateway(
            stub_url, user_stub_url, real_redis_url
        ) as gateway:
            user_id, token = await register(gateway)
            headers = {"Authorization": f"Bearer {token}", "X-User-Id": "999"}
            profile = await gateway.get("/user/profile", headers=headers)
            assert profile.status_code == 200
            assert profile.json()["userId"] == user_id
            assert profile.json()["login"] == "user@example.com"
            sent = user_stub_app.state.profile_headers[-1]
            assert sent["x-user-id"] == str(user_id)
            assert "authorization" not in sent
            logout = await gateway.post("/auth/logout", headers=headers)
            assert (logout.status_code, logout.json()) == (200, {"success": True})
            claims = jwt.decode(token, options={"verify_signature": False})
            key = f"revoked:{claims['jti']}"
            assert await redis.get(key) == b"1"
            assert 0 < await redis.ttl(key) <= claims["exp"] - int(time.time())
            again = await gateway.get("/user/profile", headers=headers)
            assert again.status_code == 401
            assert (
                await gateway.post("/auth/logout", headers=headers)
            ).status_code == 401
            # Новый вход даёт новый токен, и он действует.
            login = await gateway.post("/auth/login", json=CREDENTIALS)
            fresh = {"Authorization": f"Bearer {login.json()['token']}"}
            assert (
                await gateway.get("/user/profile", headers=fresh)
            ).status_code == 200
    finally:
        await redis.flushdb()
        await redis.aclose()


async def test_revocation_survives_a_gateway_restart(
    stub_url, user_stub_url, real_redis_url, open_auth_gateway
) -> None:
    redis = Redis.from_url(real_redis_url)
    await redis.flushdb()
    try:
        async with open_auth_gateway(stub_url, user_stub_url, real_redis_url) as first:
            _, token = await register(first)
            headers = {"Authorization": f"Bearer {token}"}
            await first.post("/auth/logout", headers=headers)
        # Тот же ключ подписи (из окружения), новый процесс: отзыв хранит Redis.
        async with open_auth_gateway(stub_url, user_stub_url, real_redis_url) as second:
            response = await second.get("/user/profile", headers=headers)
            assert response.status_code == 401
    finally:
        await redis.flushdb()
        await redis.aclose()


async def test_stopped_redis_rejects_protected_routes_but_not_guests(
    stub_url, user_stub_url, open_auth_gateway
) -> None:
    # Закрытый порт ведёт себя как остановленный Redis: соединение отклоняется.
    dead = f"redis://127.0.0.1:{free_port()}/0"
    async with open_auth_gateway(stub_url, user_stub_url, dead) as gateway:
        _, token = await register(gateway)
        headers = {"Authorization": f"Bearer {token}"}
        for method, path in (("GET", "/user/profile"), ("POST", "/auth/logout")):
            response = await gateway.request(method, path, headers=headers)
            assert response.status_code == 503
            assert response.json()["error"]["details"] == {
                "upstream": "redis",
                "reason": "connect",
            }
        guest = await gateway.post(
            "/recipes/generate-menu", json={"peopleCount": 4, "weeklyBudget": 5000}
        )
        assert guest.status_code == 202
        assert (await gateway.get("/health")).status_code == 200
        metrics = (await gateway.get("/metrics")).text
        assert "gateway_redis_up 0.0" in metrics


async def test_silent_redis_times_out_quickly(
    stub_url, user_stub_url, open_auth_gateway
) -> None:
    async def hold(reader, writer) -> None:
        await asyncio.sleep(30)

    server = await asyncio.start_server(hold, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        async with open_auth_gateway(
            stub_url,
            user_stub_url,
            f"redis://127.0.0.1:{port}/0",
            redis_operation_timeout_seconds=0.3,
        ) as gateway:
            _, token = await register(gateway)
            started = time.perf_counter()
            response = await gateway.get(
                "/user/profile", headers={"Authorization": f"Bearer {token}"}
            )
            elapsed = time.perf_counter() - started
        assert response.status_code == 503
        assert response.json()["error"]["details"]["reason"] == "timeout"
        assert elapsed < 3
    finally:
        server.close()
