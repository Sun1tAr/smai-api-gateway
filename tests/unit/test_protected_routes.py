import asyncio
import json
import time
import uuid

import httpx
import jwt
import pytest
from fakeredis import FakeAsyncRedis
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from redis import exceptions as redis_errors

from gateway import api
from gateway.bearer import MAX_HEADER_LENGTH, optional_user
from gateway.config import Settings
from gateway.main import create_app
from gateway.revocation import RevocationStore
from gateway.tokens import ISSUER, TokenIssuer, load_private_key
from tests.conftest import reply

UNAUTHORIZED = {
    "error": {
        "code": "UNAUTHORIZED",
        "message": "Authentication token is missing or invalid",
        "details": {},
    }
}
PROFILE = {
    "userId": 42,
    "login": "user@example.com",
    "createdAt": "2026-10-14T12:00:00Z",
}


class UserService:
    """Подменяет MS-3: отвечает заданным статусом и телом, запоминает запросы."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.status = 200
        self.body: bytes = json.dumps(PROFILE).encode()
        self.headers = {"content-type": "application/json"}
        self.failure: Exception | None = None

    def respond(self, status: int, body: object, headers: dict | None = None) -> None:
        self.status = status
        self.body = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.headers = {"content-type": "application/json", **(headers or {})}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.failure is not None:
            raise self.failure
        return reply(self.status, self.body, headers=self.headers)


class BrokenRedis:
    """Redis, который падает на любой команде заданной ошибкой (или зависает)."""

    def __init__(self, error: Exception | None) -> None:
        self.error = error
        self.calls = 0

    async def _fail(self):
        self.calls += 1
        if self.error is None:
            await asyncio.sleep(60)
        raise self.error

    async def exists(self, *args, **kwargs):
        await self._fail()

    async def set(self, *args, **kwargs):
        await self._fail()

    async def aclose(self) -> None:
        return None


class WriteFailsRedis:
    """Читает из fakeredis, но запись отзыва не проходит."""

    def __init__(self, inner: FakeAsyncRedis) -> None:
        self.inner = inner

    async def exists(self, *args, **kwargs):
        return await self.inner.exists(*args, **kwargs)

    async def set(self, *args, **kwargs):
        raise redis_errors.ConnectionError("down")

    async def aclose(self) -> None:
        return None


@pytest.fixture
def user() -> UserService:
    return UserService()


def build(user: UserService, redis, **overrides: object) -> FastAPI:
    settings = Settings(
        _env_file=None,
        upstream_recipes_url="http://recipes:8000",
        upstream_dictionary_url="http://dictionary:8000",
        rate_limit_enabled=False,
        **overrides,
    )
    return create_app(settings, transport=httpx.MockTransport(user), redis_client=redis)


def client_for(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway"
    )


@pytest.fixture
async def app(user: UserService, fake_redis: FakeAsyncRedis) -> FastAPI:
    return build(user, fake_redis)


@pytest.fixture
async def gateway(app: FastAPI):
    async with client_for(app) as client:
        yield client


def bearer(app: FastAPI, user_id: int = 42) -> dict[str, str]:
    return {"Authorization": f"Bearer {app.state.token_issuer.issue(user_id)}"}


def claims_of(headers: dict[str, str]) -> dict:
    token = headers["Authorization"].split(" ")[1]
    return jwt.decode(token, options={"verify_signature": False})


# ---- профиль ---------------------------------------------------------------


async def test_profile_returns_the_user_of_the_token(gateway, app, user) -> None:
    response = await gateway.get("/user/profile", headers=bearer(app))
    assert response.status_code == 200
    assert response.json() == PROFILE
    assert response.headers["cache-control"] == "no-store"
    sent = user.requests[0]
    assert (sent.method, sent.url.path) == ("GET", "/profile")


async def test_profile_sends_only_the_user_id_from_the_token(
    gateway, app, user
) -> None:
    headers = [
        ("Authorization", bearer(app)["Authorization"]),
        ("X-User-Id", "1"),
        ("x-user-id", "2"),
        ("X-USER-ID", "3"),
        ("X-User-Role", "admin"),
        ("X-Auth-User", "root"),
    ]
    response = await gateway.get("/user/profile", headers=headers)
    assert response.status_code == 200
    sent = user.requests[0].headers
    assert sent.get_list("x-user-id") == ["42"]
    assert "x-user-role" not in sent and "x-auth-user" not in sent
    assert "authorization" not in sent


async def test_profile_user_id_follows_the_token_subject(gateway, app, user) -> None:
    user.respond(200, {**PROFILE, "userId": 7})
    response = await gateway.get("/user/profile", headers=bearer(app, 7))
    assert response.status_code == 200
    assert user.requests[0].headers["x-user-id"] == "7"


async def test_profile_query_and_body_are_not_forwarded(gateway, app, user) -> None:
    await gateway.request(
        "GET", "/user/profile?userId=1", headers=bearer(app), content=b"{}"
    )
    sent = user.requests[0]
    assert sent.url.query == b""
    assert sent.content == b""


async def test_profile_without_a_token_is_401_and_never_reaches_ms3(
    gateway, user
) -> None:
    response = await gateway.get("/user/profile", headers={"X-User-Id": "42"})
    assert response.status_code == 401
    assert response.json() == UNAUTHORIZED
    assert response.headers["www-authenticate"] == "Bearer"
    assert user.requests == []


@pytest.mark.parametrize(
    "response_status, body, expected",
    [
        (
            404,
            {"error": {"code": "USER_NOT_FOUND", "message": "m", "details": {}}},
            401,
        ),
        (404, {"error": {"code": "NOT_FOUND", "message": "m", "details": {}}}, 502),
        (404, b"not json", 502),
        (401, {"error": {"code": "UNAUTHORIZED", "message": "m", "details": {}}}, 502),
        (
            400,
            {"error": {"code": "VALIDATION_ERROR", "message": "m", "details": {}}},
            502,
        ),
        (403, {}, 502),
        (201, PROFILE, 502),
        (302, b"", 502),
        (
            500,
            {"error": {"code": "INTERNAL_ERROR", "message": "secret", "details": {}}},
            500,
        ),
        (502, b"", 502),
        (503, b"", 502),
        (200, b"", 502),
        (200, b"[]", 502),
        (200, {"userId": 42, "login": "a@b.ru"}, 502),
        (200, {**PROFILE, "userId": 43}, 502),
        (200, {**PROFILE, "userId": "42"}, 502),
        (200, {**PROFILE, "userId": True}, 502),
        (200, {**PROFILE, "login": ""}, 502),
        (200, {**PROFILE, "login": 5}, 502),
        (200, {**PROFILE, "login": "x" * 255}, 502),
        (200, {**PROFILE, "createdAt": "yesterday"}, 502),
        (200, {**PROFILE, "createdAt": "2026-13-45T25:00:00Z"}, 502),
        (200, {**PROFILE, "createdAt": "2026-10-14T12:00:00+03:00"}, 502),
        (200, {**PROFILE, "createdAt": 1700000000}, 502),
    ],
)
async def test_ms3_response_table_for_profile(
    gateway, app, user, response_status, body, expected
) -> None:
    user.respond(response_status, body)
    response = await gateway.get("/user/profile", headers=bearer(app))
    assert response.status_code == expected
    if expected == 401:
        assert response.json() == UNAUTHORIZED
    if expected == 500:
        assert response.json()["error"]["code"] == "INTERNAL_ERROR"
        assert "secret" not in response.text
    if expected == 502:
        assert response.json()["error"]["details"] == {
            "upstream": "user",
            "reason": "protocol",
        }


async def test_ms3_busy_503_keeps_retry_after(gateway, app, user) -> None:
    user.respond(
        503,
        {"error": {"code": "SERVICE_BUSY", "message": "m", "details": {}}},
        {"retry-after": "7"},
    )
    response = await gateway.get("/user/profile", headers=bearer(app))
    assert response.status_code == 503
    assert response.headers["retry-after"] == "7"
    assert response.json()["error"]["details"] == {"upstream": "user", "reason": "busy"}


async def test_extra_ms3_fields_do_not_reach_the_client(gateway, app, user) -> None:
    user.respond(200, {**PROFILE, "passwordHash": "$argon2id$secret"})
    response = await gateway.get("/user/profile", headers=bearer(app))
    assert response.json() == PROFILE
    assert "argon2" not in response.text


async def test_ms3_down_is_a_gateway_error_not_a_401(gateway, app, user) -> None:
    user.failure = httpx.ConnectError("refused")
    response = await gateway.get("/user/profile", headers=bearer(app))
    assert response.status_code == 502
    assert response.json()["error"]["details"]["reason"] == "connect"


async def test_profile_rejects_other_methods(gateway, app, user) -> None:
    response = await gateway.post("/user/profile", headers=bearer(app))
    assert response.status_code == 405
    assert user.requests == []


# ---- проверка токена -------------------------------------------------------


def forged(app: FastAPI, **override: object) -> str:
    key = load_private_key(app.state.settings.jwt_private_key_file)
    now = int(time.time())
    payload = {
        "iss": ISSUER,
        "sub": "42",
        "jti": str(uuid.uuid4()),
        "iat": now,
        "nbf": now,
        "exp": now + 3600,
        **override,
    }
    payload = {k: v for k, v in payload.items() if v is not None}
    return jwt.encode(
        payload, key, algorithm="ES256", headers={"kid": app.state.token_issuer.kid}
    )


def invalid_tokens(app: FastAPI) -> dict[str, str]:
    from cryptography.hazmat.primitives.asymmetric import ec

    now = int(time.time())
    other = ec.generate_private_key(ec.SECP256R1())
    return {
        "foreign_key": jwt.encode(
            {"iss": ISSUER, "sub": "42"},
            other,
            algorithm="ES256",
            headers={"kid": app.state.token_issuer.kid},
        ),
        "alg_none": jwt.encode(
            {"iss": ISSUER, "sub": "42"},
            None,
            algorithm="none",
            headers={"kid": app.state.token_issuer.kid},
        ),
        "expired": forged(app, iat=now - 200, nbf=now - 200, exp=now - 100),
        "not_yet_valid": forged(app, nbf=now + 600, exp=now + 900),
        "no_exp": forged(app, exp=None),
        "no_jti": forged(app, jti=None),
        "wrong_issuer": forged(app, iss="evil"),
        "bad_subject": forged(app, sub="0"),
        "garbage": "not.a.token",
    }


async def test_every_invalid_token_gets_the_same_401(gateway, app, user) -> None:
    for name, token in invalid_tokens(app).items():
        response = await gateway.get(
            "/user/profile", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 401, name
        assert response.json() == UNAUTHORIZED, name
    assert user.requests == []


@pytest.mark.parametrize(
    "header",
    [
        "",
        "Bearer",
        "Bearer ",
        "Bearer a.b",
        "Bearer a.b.c d",
        "Bearer  a.b.c",
        "Basic dXNlcjpwYXNz",
        "Token a.b.c",
        "a.b.c",
        "Bearer a.b.c\t",
        "Bearer " + "a" * MAX_HEADER_LENGTH + ".b.c",
    ],
)
async def test_malformed_authorization_header_is_401(
    gateway, user, header: str
) -> None:
    response = await gateway.get("/user/profile", headers={"Authorization": header})
    assert response.status_code == 401
    assert response.json() == UNAUTHORIZED
    assert user.requests == []


async def test_two_authorization_headers_are_401(gateway, app, user) -> None:
    token = bearer(app)["Authorization"]
    response = await gateway.get(
        "/user/profile", headers=[("Authorization", token), ("authorization", token)]
    )
    assert response.status_code == 401
    assert user.requests == []


async def test_bearer_scheme_is_case_insensitive(gateway, app) -> None:
    token = bearer(app)["Authorization"].split(" ")[1]
    for scheme in ("bearer", "BEARER", "BeArEr"):
        response = await gateway.get(
            "/user/profile", headers={"Authorization": f"{scheme} {token}"}
        )
        assert response.status_code == 200


async def test_invalid_tokens_do_not_touch_redis(user) -> None:
    redis = BrokenRedis(redis_errors.ConnectionError("down"))
    app = build(user, redis)
    async with client_for(app) as client:
        for token in invalid_tokens(app).values():
            response = await client.get(
                "/user/profile", headers={"Authorization": f"Bearer {token}"}
            )
            assert response.status_code == 401
        assert (await client.get("/user/profile")).status_code == 401
    assert redis.calls == 0


# ---- logout ----------------------------------------------------------------


async def test_logout_revokes_the_token(gateway, app, fake_redis, user) -> None:
    headers = bearer(app)
    claims = claims_of(headers)
    response = await gateway.post("/auth/logout", headers=headers)
    assert response.status_code == 200
    assert response.json() == {"success": True}
    assert response.headers["cache-control"] == "no-store"
    key = f"revoked:{claims['jti']}"
    assert await fake_redis.get(key) == b"1"
    ttl = await fake_redis.ttl(key)
    assert 0 < ttl <= claims["exp"] - int(time.time())
    assert ttl >= claims["exp"] - int(time.time()) - 2
    assert user.requests == []


async def test_revoked_token_is_rejected_everywhere(gateway, app, user) -> None:
    headers = bearer(app)
    await gateway.post("/auth/logout", headers=headers)
    for method, path in (("GET", "/user/profile"), ("POST", "/auth/logout")):
        response = await gateway.request(method, path, headers=headers)
        assert response.status_code == 401
        assert response.json() == UNAUTHORIZED
    assert user.requests == []


async def test_logout_revokes_only_that_token(gateway, app) -> None:
    first, second = bearer(app), bearer(app)
    assert first != second
    await gateway.post("/auth/logout", headers=first)
    assert (await gateway.get("/user/profile", headers=second)).status_code == 200


async def test_logout_does_not_read_the_body(gateway, app) -> None:
    response = await gateway.post(
        "/auth/logout", headers=bearer(app), content=b"\xff" * 100_000
    )
    assert response.status_code == 200


async def test_logout_without_a_token_is_401(gateway, fake_redis) -> None:
    response = await gateway.post("/auth/logout")
    assert response.status_code == 401
    assert response.json() == UNAUTHORIZED
    assert await fake_redis.keys("*") == []


async def test_logout_with_an_invalid_token_writes_nothing(
    gateway, app, fake_redis
) -> None:
    for token in invalid_tokens(app).values():
        await gateway.post("/auth/logout", headers={"Authorization": f"Bearer {token}"})
    assert await fake_redis.keys("*") == []


async def test_logout_rejects_get(gateway, app) -> None:
    assert (await gateway.get("/auth/logout", headers=bearer(app))).status_code == 405


async def test_revocation_ttl_is_exp_minus_now_with_a_minimum_of_one_second(
    fake_redis,
) -> None:
    from prometheus_client import CollectorRegistry

    from gateway.metrics import AuthMetrics

    settings = Settings(
        _env_file=None,
        upstream_recipes_url="http://recipes:8000",
        upstream_dictionary_url="http://dictionary:8000",
    )
    store = RevocationStore(
        fake_redis, AuthMetrics(CollectorRegistry()), settings, clock=lambda: 1000.9
    )
    await store.revoke("a", exp=4600)
    await store.revoke("b", exp=1000)
    await store.revoke("c", exp=10)
    assert await fake_redis.ttl("revoked:a") == 3600
    assert await fake_redis.ttl("revoked:b") == 1
    assert await fake_redis.ttl("revoked:c") == 1
    assert await store.is_revoked("a") is True
    assert await store.is_revoked("zzz") is False


async def test_revocation_expires_with_the_token(gateway, app, fake_redis) -> None:
    short = TokenIssuer(load_private_key(app.state.settings.jwt_private_key_file), 60)
    token = short.issue(42)
    await gateway.post("/auth/logout", headers={"Authorization": f"Bearer {token}"})
    jti = jwt.decode(token, options={"verify_signature": False})["jti"]
    assert 0 < await fake_redis.ttl(f"revoked:{jti}") <= 60


# ---- Redis недоступен ------------------------------------------------------


REDIS_FAILURES = [
    (redis_errors.ConnectionError("down"), "connect"),
    (ConnectionRefusedError("refused"), "connect"),
    (redis_errors.TimeoutError("slow"), "timeout"),
    (redis_errors.ResponseError("OOM command not allowed"), "protocol"),
    (None, "timeout"),
]


@pytest.mark.parametrize("error, reason", REDIS_FAILURES)
async def test_redis_failure_rejects_protected_routes_with_503(
    user, error, reason
) -> None:
    app = build(
        user,
        BrokenRedis(error),
        redis_connect_timeout_seconds=0.05,
        redis_operation_timeout_seconds=0.05,
    )
    async with client_for(app) as client:
        headers = bearer(app)
        for method, path in (("GET", "/user/profile"), ("POST", "/auth/logout")):
            response = await client.request(method, path, headers=headers)
            assert response.status_code == 503
            assert response.json() == {
                "error": {
                    "code": "EXTERNAL_SERVICE_ERROR",
                    "message": "Token store is unavailable, try again later",
                    "details": {"upstream": "redis", "reason": reason},
                }
            }
    assert user.requests == []


async def test_guest_generation_works_without_redis(user) -> None:
    redis = BrokenRedis(redis_errors.ConnectionError("down"))
    app = build(user, redis)
    user.respond(202, {"task_id": "b1e2c3d4"})
    async with client_for(app) as client:
        response = await client.post("/recipes/generate-menu", json={})
        assert (await client.get("/health")).status_code == 200
        assert (await client.get("/dictionaries")).status_code == 202
    assert response.status_code == 202
    assert redis.calls == 0


async def test_logout_write_failure_is_503_and_token_stays_valid(
    user, fake_redis
) -> None:
    app = build(user, WriteFailsRedis(fake_redis))
    async with client_for(app) as client:
        headers = bearer(app)
        response = await client.post("/auth/logout", headers=headers)
        assert response.status_code == 503
        assert response.json()["error"]["details"]["upstream"] == "redis"
        assert (await client.get("/user/profile", headers=headers)).status_code == 200
    assert await fake_redis.keys("*") == []


async def test_recovered_redis_serves_protected_routes_again(user, fake_redis) -> None:
    broken = BrokenRedis(redis_errors.ConnectionError("down"))
    app = build(user, broken)
    async with client_for(app) as client:
        headers = bearer(app)
        assert (await client.get("/user/profile", headers=headers)).status_code == 503
        app.state.revocations._client = fake_redis
        assert (await client.get("/user/profile", headers=headers)).status_code == 200


def sample(text: str, name: str, labels: str = "") -> float | None:
    for line in text.splitlines():
        if line.startswith(f"{name}{labels} "):
            return float(line.rsplit(" ", 1)[1])
    return None


async def test_redis_metrics(user, fake_redis) -> None:
    app = build(user, fake_redis)
    async with client_for(app) as client:
        before = (await client.get("/metrics")).text
        # До первого обращения к Redis серии нет: не 0, который выглядел бы как сбой.
        assert "gateway_redis_up" not in before.replace("# ", "")
        assert sample(before, "gateway_revocation_checks_total", '{result="ok"}') == 0
        headers = bearer(app)
        await client.get("/user/profile", headers=headers)
        await client.post("/auth/logout", headers=headers)
        await client.get("/user/profile", headers=headers)
        await client.get("/user/profile")
        await client.get("/user/profile", headers={"Authorization": "Bearer a.b.c"})
        text = (await client.get("/metrics")).text
    assert sample(text, "gateway_redis_up") == 1
    assert sample(text, "gateway_revocation_checks_total", '{result="ok"}') == 2
    assert sample(text, "gateway_revocation_checks_total", '{result="revoked"}') == 1
    assert sample(text, "gateway_revocation_writes_total", '{result="ok"}') == 1
    assert sample(text, "gateway_token_rejected_total", '{reason="revoked"}') == 1
    assert sample(text, "gateway_token_rejected_total", '{reason="missing"}') == 1
    assert sample(text, "gateway_token_rejected_total", '{reason="malformed"}') == 1
    assert sample(text, "gateway_token_rejected_total", '{reason="signature"}') == 0


async def test_redis_up_drops_to_zero_on_connect_failure(user) -> None:
    app = build(user, BrokenRedis(redis_errors.ConnectionError("down")))
    async with client_for(app) as client:
        await client.get("/user/profile", headers=bearer(app))
        text = (await client.get("/metrics")).text
    assert sample(text, "gateway_redis_up") == 0
    assert sample(text, "gateway_revocation_checks_total", '{result="error"}') == 1


async def test_protocol_error_is_not_counted_as_redis_down(user) -> None:
    app = build(user, BrokenRedis(redis_errors.ResponseError("OOM")))
    async with client_for(app) as client:
        await client.get("/user/profile", headers=bearer(app))
        text = (await client.get("/metrics")).text
    assert sample(text, "gateway_redis_up") == 1


async def test_health_does_not_depend_on_redis(user) -> None:
    app = build(user, BrokenRedis(redis_errors.ConnectionError("down")))
    async with client_for(app) as client:
        assert (await client.get("/health")).status_code == 200


# ---- необязательный токен (SMAI-102) -----------------------------------------


@pytest.fixture
async def optional_client(app: FastAPI):
    async def who(request: Request, _: object = Depends(optional_user)):
        user = request.state.user
        return JSONResponse({"user": user.user_id if user else None})

    app.add_api_route("/_who", who, methods=["GET"])
    async with client_for(app) as client:
        yield client


async def test_optional_token_without_a_header_is_a_guest(optional_client) -> None:
    response = await optional_client.get("/_who")
    assert (response.status_code, response.json()) == (200, {"user": None})


async def test_optional_token_with_a_valid_header_is_the_user(
    optional_client, app
) -> None:
    response = await optional_client.get("/_who", headers=bearer(app, 9))
    assert response.json() == {"user": 9}


async def test_optional_token_with_an_invalid_or_revoked_header_is_401(
    optional_client, app
) -> None:
    for token in invalid_tokens(app).values():
        response = await optional_client.get(
            "/_who", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 401
    headers = bearer(app)
    await optional_client.post("/auth/logout", headers=headers)
    assert (await optional_client.get("/_who", headers=headers)).status_code == 401
    assert (
        await optional_client.get("/_who", headers={"Authorization": ""})
    ).status_code == 401


# ---- защита от забытой проверки ------------------------------------------------


def test_startup_fails_if_a_protected_route_has_no_token_check(
    user, fake_redis, monkeypatch
) -> None:
    async def no_check() -> None:
        return None

    monkeypatch.setattr(api, "require_user", no_check)
    with pytest.raises(api.ContractError, match="no token check"):
        build(user, fake_redis)


def test_startup_fails_if_a_protected_route_is_not_registered(
    user, fake_redis, monkeypatch
) -> None:
    monkeypatch.setattr(
        api,
        "PROTECTED_ENDPOINTS",
        api.PROTECTED_ENDPOINTS | {("GET", "/recipes/no-such-route")},
    )
    with pytest.raises(api.ContractError, match="not routed"):
        build(user, fake_redis)


def test_protected_endpoints_are_logout_profile_and_saved_parameters() -> None:
    assert api.PROTECTED_ENDPOINTS == {
        ("POST", "/auth/logout"),
        ("GET", "/user/profile"),
        ("GET", "/recipes/profile/parameters"),
    }


def test_optional_auth_endpoints_are_generate_menu_and_cart() -> None:
    assert api.OPTIONAL_AUTH_ENDPOINTS == {
        ("POST", "/recipes/generate-menu"),
        ("POST", "/recipes/cart"),
    }


@pytest.mark.parametrize("auth", ["missing", "valid", "invalid", "redis_down"])
async def test_cart_accepts_guest_or_verified_identity(user, fake_redis, auth) -> None:
    redis = (
        BrokenRedis(redis_errors.ConnectionError("down"))
        if auth == "redis_down"
        else fake_redis
    )
    app = build(user, redis)
    user.respond(
        200, {"matches": [], "total": 0, "allMatched": True, "unmatchedCount": 0}
    )
    headers = {"X-User-Id": "1"}
    if auth == "invalid":
        headers["Authorization"] = "Bearer invalid.jwt.token"
    elif auth != "missing":
        headers.update(bearer(app))
    async with client_for(app) as client:
        response = await client.post("/recipes/cart", headers=headers)
    if auth == "invalid":
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"
        assert user.requests == []
    else:
        assert response.status_code == 200
        sent = user.requests[0].headers
        assert "authorization" not in sent
        assert sent.get_list("x-user-id") == (["42"] if auth == "valid" else [])


def test_startup_fails_if_generate_menu_has_no_optional_token_check(
    user, fake_redis, monkeypatch
) -> None:
    # Без зависимости все пользователи тихо стали бы гостями.
    original = api._dependencies

    def without_token(endpoint):
        if (endpoint.method, endpoint.gateway_path) in api.OPTIONAL_AUTH_ENDPOINTS:
            return [Depends(api.edge_guard)]
        return original(endpoint)

    monkeypatch.setattr(api, "_dependencies", without_token)
    with pytest.raises(api.ContractError, match="no optional token check"):
        build(user, fake_redis)


def test_startup_fails_if_an_optional_route_is_not_registered(
    user, fake_redis, monkeypatch
) -> None:
    monkeypatch.setattr(
        api,
        "OPTIONAL_AUTH_ENDPOINTS",
        api.OPTIONAL_AUTH_ENDPOINTS | {("POST", "/recipes/no-such-route")},
    )
    with pytest.raises(api.ContractError, match="not routed"):
        build(user, fake_redis)


def test_only_protected_routes_have_a_token_dependency(app: FastAPI) -> None:
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        guarded = any(
            getattr(dep.call, "token_required", False) is True
            for dep in route.dependant.dependencies
        )
        for method in route.methods:
            assert guarded == ((method, route.path) in api.PROTECTED_ENDPOINTS)


# ---- логи, спаны, документация ----------------------------------------------------


async def test_token_never_reaches_the_logs(
    user, fake_redis, capsys: pytest.CaptureFixture[str]
) -> None:
    app = build(user, fake_redis)
    async with client_for(app) as client:
        headers = bearer(app)
        token = headers["Authorization"].split(" ")[1]
        jti = claims_of(headers)["jti"]
        await client.get("/user/profile", headers=headers)
        await client.post("/auth/logout", headers=headers)
        await client.get("/user/profile", headers=headers)
        await client.get("/user/profile", headers={"Authorization": "Bearer a.b.c"})
        bad = invalid_tokens(app)["foreign_key"]
        await client.get("/user/profile", headers={"Authorization": f"Bearer {bad}"})
    output = capsys.readouterr().out
    for secret in (token, jti, *token.split("."), bad, *bad.split(".")[1:]):
        assert secret not in output
    assert "Bearer" not in output
    assert "token_rejected" in output and jti[:8] in output


def test_authorization_header_is_dropped_from_spans() -> None:
    from gateway.observability import redact_span

    class Span:
        _attributes = {
            "http.request.header.authorization": ["Bearer SECRET"],
            "http.request.header.Authorization": ["Bearer SECRET"],
            "http.request.header.proxy_authorization": ["x"],
            "http.request.header.cookie": ["s=1"],
            "http.request.header.x-user-id": ["42"],
            "http.request.header.content-type": ["application/json"],
        }

    span = Span()
    redact_span(span)
    assert list(span._attributes) == ["http.request.header.content-type"]


def test_openapi_documents_both_routes(app: FastAPI) -> None:
    schema = app.openapi()
    logout = schema["paths"]["/auth/logout"]["post"]
    profile = schema["paths"]["/user/profile"]["get"]
    assert set(logout["responses"]) == {"200", "401", "500", "503"}
    assert {"200", "401", "500", "502", "503", "504"} <= set(profile["responses"])
    assert "400" not in profile["responses"] and "429" not in profile["responses"]
    for operation in (logout, profile):
        assert operation["summary"] and operation["description"]
        assert operation["security"] == [{"bearerAuth": []}]
    scheme = schema["components"]["securitySchemes"]["bearerAuth"]
    assert (scheme["type"], scheme["scheme"], scheme["bearerFormat"]) == (
        "http",
        "bearer",
        "JWT",
    )
    redis = logout["responses"]["503"]["content"]["application/json"]["example"]
    assert redis["error"]["details"] == {"upstream": "redis", "reason": "connect"}
    examples = profile["responses"]["503"]["content"]["application/json"]["examples"]
    assert examples["redis"]["value"]["error"]["details"]["upstream"] == "redis"
    busy = examples["busy"]["value"]["error"]["details"]
    assert busy == {"upstream": "user", "reason": "busy"}
    assert examples["pool"]["value"]["error"]["details"]["reason"] == "pool"


async def test_documented_examples_match_real_responses(
    gateway, app, user, fake_redis
) -> None:
    schema = app.openapi()
    profile = schema["paths"]["/user/profile"]["get"]["responses"]
    logout = schema["paths"]["/auth/logout"]["post"]["responses"]
    headers = bearer(app)
    ok = await gateway.get("/user/profile", headers=headers)
    assert ok.json() == profile["200"]["content"]["application/json"]["example"]
    out = await gateway.post("/auth/logout", headers=headers)
    assert out.json() == logout["200"]["content"]["application/json"]["example"]
    denied = await gateway.get("/user/profile")
    assert denied.json() == profile["401"]["content"]["application/json"]["example"]
    assert denied.json() == logout["401"]["content"]["application/json"]["example"]


def test_client_user_id_header_is_stripped_for_every_upstream() -> None:
    from gateway.policy import upstream_request_headers

    result = upstream_request_headers(
        [
            (b"Authorization", b"Bearer x.y.z"),
            (b"authorization", b"Bearer a.b.c"),
            (b"X-User-Id", b"1"),
            (b"x-user-id", b"2"),
            (b"X-USER-ID", b"3"),
        ],
        request_id="r",
        client_ip=None,
        scheme="http",
        user_id=42,
    )
    assert [v for n, v in result if n == b"x-user-id"] == [b"42"]
    assert b"authorization" not in [n.lower() for n, _ in result]


def test_no_user_id_header_without_a_token() -> None:
    from gateway.policy import upstream_request_headers

    result = upstream_request_headers(
        [(b"X-User-Id", b"1")], request_id="r", client_ip=None, scheme="http"
    )
    assert b"x-user-id" not in [n.lower() for n, _ in result]


async def test_authorization_and_user_id_are_not_forwarded_to_other_services(
    gateway, app, user
) -> None:
    headers = {**bearer(app), "X-User-Id": "1"}
    user.respond(202, {"task_id": "b1e2c3d4"})
    await gateway.post("/recipes/generate-menu", json={}, headers=headers)
    sent = user.requests[0].headers
    assert "authorization" not in sent
    # Владелец только из токена (42), клиентское значение (1) заменено.
    assert sent.get_list("x-user-id") == ["42"]


async def test_deleted_user_is_counted_as_a_rejected_token(gateway, app, user) -> None:
    user.respond(
        404, {"error": {"code": "USER_NOT_FOUND", "message": "m", "details": {}}}
    )
    assert (await gateway.get("/user/profile", headers=bearer(app))).status_code == 401
    text = (await gateway.get("/metrics")).text
    assert (
        sample(text, "gateway_token_rejected_total", '{reason="user_not_found"}') == 1
    )
