"""SMAI-102: необязательный токен на API-1 и защищённый API-13."""

import json

import httpx
import jsonschema
import pytest
from redis import exceptions as redis_errors

from tests.unit.test_protected_routes import (
    BrokenRedis,
    UserService,
    bearer,
    build,
    client_for,
    invalid_tokens,
    sample,
)

GENERATE = "/recipes/generate-menu"
PARAMETERS = "/recipes/profile/parameters"
SAVED = {"habits": [3, 5], "diet": 4, "allergies": [1, 2], "kitchenTools": [1, 2, 6]}
SPOOFED = [
    ("X-User-Id", "1"),
    ("x-user-id", "2"),
    ("X-USER-ID", "3"),
    ("X-User-Role", "admin"),
    ("X-Auth-User", "9"),
    ("Authorization", "Bearer x.y.z"),
]


def owner_of(request: httpx.Request) -> list[str]:
    return request.headers.get_list("x-user-id")


@pytest.fixture
def user() -> UserService:
    service = UserService()
    service.respond(202, {"task_id": "b1e2c3d4"})
    return service


# ---- API-1: гость, пользователь, недействительный токен ------------------------


async def test_guest_has_no_owner_and_spoofing_is_stripped(user, fake_redis) -> None:
    app = build(user, fake_redis)
    async with client_for(app) as client:
        response = await client.post(GENERATE, json={}, headers=SPOOFED)
    # Поддельный Authorization недействителен: 401 и до MS-4 дело не доходит.
    assert response.status_code == 401
    assert user.requests == []
    async with client_for(app) as client:
        response = await client.post(
            GENERATE, json={}, headers=[h for h in SPOOFED if h[0] != "Authorization"]
        )
    assert response.status_code == 202
    sent = user.requests[0].headers
    assert owner_of(user.requests[0]) == []
    assert "x-user-role" not in sent and "x-auth-user" not in sent
    assert "authorization" not in sent


async def test_user_id_comes_only_from_the_token(user, fake_redis) -> None:
    app = build(user, fake_redis)
    async with client_for(app) as client:
        response = await client.post(
            GENERATE,
            json={},
            headers=[*bearer(app, 42).items(), ("X-User-Id", "1"), ("x-user-id", "2")],
        )
    assert response.status_code == 202
    assert owner_of(user.requests[0]) == ["42"]
    assert "authorization" not in user.requests[0].headers


async def test_invalid_expired_or_revoked_token_is_401_and_not_proxied(
    user, fake_redis
) -> None:
    app = build(user, fake_redis)
    async with client_for(app) as client:
        for token in invalid_tokens(app).values():
            response = await client.post(
                GENERATE, json={}, headers={"Authorization": f"Bearer {token}"}
            )
            assert response.status_code == 401
        headers = bearer(app)
        await client.post("/auth/logout", headers=headers)
        assert (
            await client.post(GENERATE, json={}, headers=headers)
        ).status_code == 401
    assert [r for r in user.requests if r.url.path == "/generate-menu"] == []


@pytest.mark.parametrize(
    "error",
    [
        redis_errors.ConnectionError("down"),
        redis_errors.TimeoutError("slow"),
    ],
)
async def test_redis_down_with_a_token_falls_back_to_a_guest(user, error) -> None:
    redis = BrokenRedis(error)
    app = build(user, redis)
    async with client_for(app) as client:
        response = await client.post(
            GENERATE, json={}, headers={**bearer(app, 42), "X-User-Id": "1"}
        )
        text = (await client.get("/metrics")).text
    assert response.status_code == 202
    assert response.json() == {"task_id": "b1e2c3d4"}
    assert owner_of(user.requests[0]) == []
    assert "authorization" not in user.requests[0].headers
    assert sample(text, "gateway_token_fallback_guest_total") == 1


async def test_revoked_token_is_a_guest_while_redis_is_down(user, fake_redis) -> None:
    # Известное ограничение (локального кэша отзывов нет): ни 401, ни владельца.
    app = build(user, fake_redis)
    headers = bearer(app, 42)
    async with client_for(app) as client:
        await client.post("/auth/logout", headers=headers)
        assert (
            await client.post(GENERATE, json={}, headers=headers)
        ).status_code == 401
        app.state.revocations._client = BrokenRedis(redis_errors.ConnectionError("x"))
        response = await client.post(GENERATE, json={}, headers=headers)
    assert response.status_code == 202
    assert owner_of(user.requests[-1]) == []


async def test_redis_fallback_does_not_log_the_token(user, capsys) -> None:
    app = build(user, BrokenRedis(redis_errors.ConnectionError("down")))
    headers = bearer(app, 42)
    async with client_for(app) as client:
        await client.post(GENERATE, json={}, headers=headers)
    output = capsys.readouterr().out
    assert "token_redis_down_guest" in output
    assert headers["Authorization"].split(" ")[1] not in output


async def test_bad_signature_is_401_even_when_redis_is_down(user) -> None:
    redis = BrokenRedis(redis_errors.ConnectionError("down"))
    app = build(user, redis)
    async with client_for(app) as client:
        response = await client.post(
            GENERATE, json={}, headers={"Authorization": "Bearer a.b.c"}
        )
    assert response.status_code == 401
    assert redis.calls == 0 and user.requests == []


async def test_guest_without_a_token_never_touches_redis(user) -> None:
    redis = BrokenRedis(redis_errors.ConnectionError("down"))
    app = build(user, redis)
    async with client_for(app) as client:
        response = await client.post(GENERATE, json={})
        text = (await client.get("/metrics")).text
    assert response.status_code == 202
    assert redis.calls == 0
    assert owner_of(user.requests[0]) == []
    assert sample(text, "gateway_token_fallback_guest_total") == 0


# ---- API-13 --------------------------------------------------------------------


async def test_saved_parameters_require_a_token(user, fake_redis) -> None:
    app = build(user, fake_redis)
    async with client_for(app) as client:
        none = await client.get(PARAMETERS)
        bad = await client.get(PARAMETERS, headers={"Authorization": "Bearer a.b.c"})
    assert none.status_code == bad.status_code == 401
    assert none.json()["error"]["code"] == "UNAUTHORIZED"
    assert user.requests == []


async def test_saved_parameters_are_proxied_for_the_token_owner(
    user, fake_redis
) -> None:
    user.respond(200, SAVED)
    app = build(user, fake_redis)
    async with client_for(app) as client:
        response = await client.get(
            PARAMETERS, headers={**bearer(app, 42), "X-User-Id": "1"}
        )
    assert response.status_code == 200
    assert response.json() == SAVED
    request = user.requests[0]
    assert request.url.path == "/profile/parameters"
    assert owner_of(request) == ["42"]
    assert "authorization" not in request.headers


async def test_saved_parameters_with_redis_down_is_503(user) -> None:
    app = build(user, BrokenRedis(redis_errors.ConnectionError("down")))
    async with client_for(app) as client:
        response = await client.get(PARAMETERS, headers=bearer(app, 42))
    assert response.status_code == 503
    assert response.json()["error"]["details"] == {
        "upstream": "redis",
        "reason": "connect",
    }
    assert user.requests == []


async def test_revoked_token_cannot_read_saved_parameters(user, fake_redis) -> None:
    app = build(user, fake_redis)
    headers = bearer(app, 42)
    async with client_for(app) as client:
        await client.post("/auth/logout", headers=headers)
        response = await client.get(PARAMETERS, headers=headers)
    assert response.status_code == 401


async def test_upstream_error_of_saved_parameters_is_passed_through(
    user, fake_redis
) -> None:
    body = {"error": {"code": "INTERNAL_ERROR", "message": "m", "details": {}}}
    user.respond(500, body)
    app = build(user, fake_redis)
    async with client_for(app) as client:
        response = await client.get(PARAMETERS, headers=bearer(app, 42))
    assert (response.status_code, response.json()) == (500, body)


# ---- остальные маршруты: заголовки клиента не доходят до сервиса -----------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/recipes/generate-menu/b1e2c3d4-5678-90ab-cdef-1234567890ab"),
        ("GET", "/dictionaries"),
    ],
)
async def test_public_routes_strip_identity_headers(
    user, fake_redis, method, path
) -> None:
    user.respond(200, {})
    app = build(user, fake_redis)
    async with client_for(app) as client:
        response = await client.request(
            method, path, headers=[*SPOOFED, ("x-user-id", "5"), *bearer(app).items()]
        )
    assert response.status_code == 200
    sent = user.requests[0].headers
    assert owner_of(user.requests[0]) == []
    for name in ("authorization", "x-user-role", "x-auth-user"):
        assert name not in sent


def test_authorization_is_in_the_edge_strip_list() -> None:
    from gateway.policy import EDGE_STRIP

    assert b"authorization" in EDGE_STRIP


# ---- документация и контракт -----------------------------------------------------


@pytest.fixture
def schema(user, fake_redis) -> dict:
    return build(user, fake_redis).openapi()


def test_generate_menu_token_is_optional_in_docs(schema: dict) -> None:
    operation = schema["paths"][GENERATE]["post"]
    assert operation["security"] == [{}, {"bearerAuth": []}]
    assert set(operation["responses"]) >= {"202", "400", "401", "500"}
    assert "guest" not in json.dumps(operation["responses"]["503"]["content"])
    # 503 у API-1 только про пул: Redis для него в гостя превращается, а не в ошибку.
    assert "redis" not in json.dumps(operation["responses"]["503"]).lower()
    assert "необязателен" in operation["description"]


def test_saved_parameters_documentation(schema: dict) -> None:
    operation = schema["paths"][PARAMETERS]["get"]
    assert operation["security"] == [{"bearerAuth": []}]
    assert "404" not in operation["responses"]
    assert {"200", "401", "500", "502", "503", "504"} <= set(operation["responses"])
    examples = operation["responses"]["200"]["content"]["application/json"]["examples"]
    assert set(examples) == {"saved", "empty"}
    assert examples["empty"]["value"]["diet"] is None
    body = operation["responses"]["200"]["content"]["application/json"]["schema"]
    for item in examples.values():
        jsonschema.validate(item["value"], body)
    redis = operation["responses"]["503"]["content"]["application/json"]["examples"]
    assert redis["redis"]["value"]["error"]["details"]["upstream"] == "redis"


def test_snapshot_has_no_refs_and_matches_ms4_paths_and_codes() -> None:
    from pathlib import Path

    contract = json.loads(Path("contracts/receipes.openapi.json").read_text("utf-8"))
    assert "$ref" not in json.dumps(contract)
    paths = contract["paths"]
    # MS-4 отвечает на эти коды; 401 добавляет gateway (токен проверяет он).
    assert set(paths["/generate-menu"]["post"]["responses"]) == {
        "202",
        "400",
        "401",
        "500",
    }
    assert set(paths["/profile/parameters"]["get"]["responses"]) == {
        "200",
        "401",
        "500",
    }
    assert "404" not in json.dumps(paths["/profile/parameters"])
