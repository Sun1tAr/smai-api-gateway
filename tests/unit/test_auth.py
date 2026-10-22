import json
from pathlib import Path

import httpx
import jsonschema
import jwt
import pytest
from fastapi import FastAPI
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from gateway.api import ContractError, load_contracts, register_routes
from gateway.auth import AUTH_ROUTE
from gateway.config import Settings
from gateway.main import create_app
from gateway.routing import Route, load_routes
from tests.conftest import reply

CONTRACT_PATH = Path("contracts/user.openapi.json")
CONTRACT = json.loads(CONTRACT_PATH.read_text("utf-8"))
PASSWORD = "correct-horse-battery"
CREDENTIALS = {"login": "user@example.com", "password": PASSWORD}


def error_body(code: str, message: str = "m", details: dict | None = None) -> dict:
    return {"error": {"code": code, "message": message, "details": details or {}}}


class UserService:
    """Подменяет MS-3: отвечает заданным статусом и телом, запоминает запросы."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.status: int | None = None
        self.body: bytes = b'{"userId": 42}'
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
        # По умолчанию MS-3 отвечает успехом: 201 на регистрацию, 200 на остальное.
        status = self.status or (201 if request.url.path == "/register" else 200)
        return reply(status, self.body, headers=self.headers)


@pytest.fixture
def user() -> UserService:
    return UserService()


def make_app(user: UserService, **overrides: object) -> FastAPI:
    settings = Settings(
        _env_file=None,
        upstream_recipes_url="http://recipes:8000",
        upstream_dictionary_url="http://dictionary:8000",
        **overrides,
    )
    return create_app(settings, transport=httpx.MockTransport(user))


def client_for(app: FastAPI, ip: str = "203.0.113.9") -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(ip, 5555)),
        base_url="http://gateway",
    )


@pytest.fixture
async def gateway(user: UserService):
    async with client_for(make_app(user, rate_limit_enabled=False)) as client:
        yield client


async def test_register_returns_201_with_user_id_and_signed_token(
    gateway, user
) -> None:
    response = await gateway.post("/auth/register", json=CREDENTIALS)
    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"userId", "token"} and body["userId"] == 42
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-request-id"]
    claims = jwt.decode(body["token"], options={"verify_signature": False})
    assert claims["sub"] == "42" and claims["iss"] == "smai-gateway" and claims["jti"]
    assert claims["exp"] - claims["iat"] == 86_400


async def test_login_returns_200_and_token_verifiable_with_the_public_key(
    user, jwt_key_file
) -> None:
    from gateway.tokens import load_private_key

    user.respond(200, {"userId": 7})
    async with client_for(make_app(user, rate_limit_enabled=False)) as client:
        response = await client.post("/auth/login", json=CREDENTIALS)
    assert response.status_code == 200
    public_key = load_private_key(jwt_key_file).public_key()
    claims = jwt.decode(
        response.json()["token"],
        public_key,
        algorithms=["ES256"],
        issuer="smai-gateway",
    )
    assert claims["sub"] == "7"


async def test_configured_ttl_sets_exp(user) -> None:
    async with client_for(
        make_app(user, rate_limit_enabled=False, jwt_ttl_seconds=600)
    ) as client:
        token = (await client.post("/auth/register", json=CREDENTIALS)).json()["token"]
    claims = jwt.decode(token, options={"verify_signature": False})
    assert claims["exp"] - claims["iat"] == 600


async def test_request_reaches_the_user_service_unchanged(gateway, user) -> None:
    raw = b'{"login": "User@Example.com",  "password": "correct-horse-battery"}'
    await gateway.post(
        "/auth/register",
        content=raw,
        headers={"content-type": "application/json", "x-user-id": "999"},
    )
    (request,) = user.requests
    assert (request.method, str(request.url)) == ("POST", "http://user:8000/register")
    assert request.content == raw
    assert request.headers["content-type"] == "application/json"
    assert "x-user-id" not in request.headers
    assert request.headers["x-request-id"]


async def test_query_string_is_not_forwarded(gateway, user) -> None:
    await gateway.post("/auth/login?admin=1", json=CREDENTIALS)
    assert str(user.requests[0].url) == "http://user:8000/login"


@pytest.mark.parametrize(
    ("path", "status", "code"),
    [
        ("/auth/register", 400, "VALIDATION_ERROR"),
        ("/auth/register", 409, "LOGIN_ALREADY_EXISTS"),
        ("/auth/login", 400, "VALIDATION_ERROR"),
        ("/auth/login", 401, "INVALID_CREDENTIALS"),
    ],
)
async def test_whitelisted_errors_of_the_user_service_are_passed_on(
    gateway, user, path: str, status: int, code: str
) -> None:
    details = {"fields": [{"loc": ["body", "login"], "type": "missing"}]}
    user.respond(status, error_body(code, "Reason text", details))
    response = await gateway.post(path, json=CREDENTIALS)
    assert response.status_code == status
    assert response.json() == error_body(code, "Reason text", details)
    assert "token" not in response.text


async def test_user_service_500_becomes_a_plain_500(gateway, user) -> None:
    user.respond(
        500, error_body("INTERNAL_ERROR", "connect to 10.0.0.5 failed", {"x": 1})
    )
    response = await gateway.post("/auth/register", json=CREDENTIALS)
    assert response.status_code == 500
    assert response.json() == error_body("INTERNAL_ERROR", "Internal server error")


@pytest.mark.parametrize(
    ("path", "status", "body"),
    [
        ("/auth/register", 200, {"userId": 1}),  # успех регистрации — только 201
        ("/auth/login", 201, {"userId": 1}),
        ("/auth/register", 401, error_body("INVALID_CREDENTIALS")),  # не его статус
        ("/auth/login", 409, error_body("LOGIN_ALREADY_EXISTS")),
        ("/auth/register", 404, error_body("NOT_FOUND")),
        ("/auth/login", 405, error_body("METHOD_NOT_ALLOWED")),
        ("/auth/login", 422, {"detail": []}),
        ("/auth/login", 204, b""),
        ("/auth/login", 418, {"userId": 1}),
        ("/auth/login", 302, {"userId": 1}),
    ],
)
async def test_any_other_status_is_a_502_protocol_error(
    gateway, user, path: str, status: int, body: object
) -> None:
    user.respond(status, body)
    response = await gateway.post(path, json=CREDENTIALS)
    assert response.status_code == 502
    assert response.json() == error_body(
        "EXTERNAL_SERVICE_ERROR",
        "Upstream service failed while handling the request",
        {"upstream": "user", "reason": "protocol"},
    )


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"",
        b"[]",
        b"{}",
        b'{"userId": 0}',
        b'{"userId": -3}',
        b'{"userId": "5"}',
        b'{"userId": true}',
        b'{"userId": 1.5}',
        b'{"userId": null}',
        b'{"token": "x"}',
    ],
)
async def test_success_without_a_positive_integer_user_id_is_502(
    gateway, user, body: bytes
) -> None:
    user.respond(201, body)
    response = await gateway.post("/auth/register", json=CREDENTIALS)
    assert response.status_code == 502
    assert "token" not in response.text


@pytest.mark.parametrize(
    "body",
    [
        b"<html>",
        b"{}",
        b'{"error": "text"}',
        b'{"error": {"code": "lower", "message": "m"}}',
        b'{"error": {"code": "A", "message": ""}}',
        b'{"error": {"code": "A", "message": 5}}',
        b'{"error": {"code": "A"}}',
        b'{"error": {"code": "A", "message": "m", "details": []}}',
        b'{"error": {"code": "A", "message": "' + b"x" * 501 + b'"}}',
    ],
)
async def test_whitelisted_status_with_a_broken_error_body_is_502(
    gateway, user, body: bytes
) -> None:
    user.respond(409, body)
    response = await gateway.post("/auth/register", json=CREDENTIALS)
    assert response.status_code == 502


async def test_error_without_details_gets_empty_details(gateway, user) -> None:
    user.respond(409, {"error": {"code": "LOGIN_ALREADY_EXISTS", "message": "m"}})
    response = await gateway.post("/auth/register", json=CREDENTIALS)
    assert response.json()["error"]["details"] == {}


@pytest.mark.parametrize(("sent", "expected"), [("7", "7"), (None, "5"), ("abc", "5")])
async def test_busy_user_service_is_503_with_retry_after(
    gateway, user, sent: str | None, expected: str
) -> None:
    user.respond(
        503,
        error_body("SERVICE_BUSY", "Service is busy, retry later"),
        {"retry-after": sent} if sent else None,
    )
    response = await gateway.post("/auth/login", json=CREDENTIALS)
    assert response.status_code == 503
    assert response.headers["retry-after"] == expected
    assert response.json() == error_body(
        "EXTERNAL_SERVICE_ERROR",
        "Upstream service is busy, try again later",
        {"upstream": "user", "reason": "busy"},
    )


async def test_other_503_from_the_user_service_is_502(gateway, user) -> None:
    user.respond(503, error_body("INTERNAL_ERROR", "Service is unavailable"))
    response = await gateway.post("/auth/login", json=CREDENTIALS)
    assert response.status_code == 502


@pytest.mark.parametrize(
    ("failure", "status", "reason"),
    [
        (httpx.ConnectError("refused"), 502, "connect"),
        (httpx.ReadTimeout("slow"), 504, "timeout"),
        (httpx.PoolTimeout("busy"), 503, "pool"),
        (httpx.ReadError("reset"), 502, "protocol"),
    ],
)
async def test_user_service_failures_name_the_user_upstream(
    gateway, user, failure: Exception, status: int, reason: str
) -> None:
    user.failure = failure
    response = await gateway.post("/auth/login", json=CREDENTIALS)
    assert response.status_code == status
    assert response.json()["error"]["details"] == {"upstream": "user", "reason": reason}


async def test_oversized_body_is_413_and_not_forwarded(user) -> None:
    async with client_for(
        make_app(user, rate_limit_enabled=False, max_request_body_bytes=64)
    ) as client:
        response = await client.post(
            "/auth/register", content=b'{"password": "' + b"x" * 100 + b'"}'
        )
    assert response.status_code == 413
    assert user.requests == []


@pytest.mark.parametrize("path", ["/auth/register", "/auth/login"])
async def test_only_post_is_allowed(gateway, path: str) -> None:
    response = await gateway.get(path)
    assert response.status_code == 405
    assert "POST" in response.headers["allow"]


async def test_password_is_not_logged_or_traced(
    user, capsys: pytest.CaptureFixture[str]
) -> None:
    app = make_app(user, rate_limit_enabled=False)
    exporter = InMemorySpanExporter()
    app.state.tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
    async with client_for(app) as client:
        await client.post("/auth/register", json=CREDENTIALS)
        user.respond(401, error_body("INVALID_CREDENTIALS"))
        await client.post("/auth/login", json=CREDENTIALS)
        user.respond(500, error_body("INTERNAL_ERROR"))
        await client.post("/auth/login", json=CREDENTIALS)
    output = capsys.readouterr().out
    assert "upstream_request" in output
    assert PASSWORD not in output
    spans = repr(
        [(s.name, dict(s.attributes or {})) for s in exporter.get_finished_spans()]
    )
    assert exporter.get_finished_spans() and PASSWORD not in spans
    assert "token" not in output.lower().replace("token_issuer", "")


async def test_probes_stay_quiet_next_to_auth_routes(
    user, capsys: pytest.CaptureFixture[str]
) -> None:
    app = make_app(user, rate_limit_enabled=False)
    async with client_for(app) as client:
        await client.get("/health")
        await client.get("/metrics")
        await client.post("/auth/login", json=CREDENTIALS)
    paths = [
        json.loads(line)["path"]
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("{") and json.loads(line)["message"] == "http_request"
    ]
    assert paths == ["/auth/login"]


# --- лимит ---


async def hit(client: httpx.AsyncClient, path: str = "/auth/login") -> httpx.Response:
    return await client.post(path, json=CREDENTIALS)


async def test_auth_limit_gives_429_with_retry_after_shared_by_both_routes(
    user,
) -> None:
    app = make_app(user, auth_rate_limit_requests=2)
    async with client_for(app) as client:
        first = await hit(client, "/auth/login")
        second = await hit(client, "/auth/register")
        blocked = await hit(client, "/auth/login")
    assert (first.status_code, second.status_code) == (200, 201)
    assert blocked.status_code == 429
    assert blocked.headers["retry-after"].isdigit()
    assert blocked.json()["error"]["code"] == "RATE_LIMITED"
    assert len(user.requests) == 2


async def test_auth_limit_defaults_to_10_per_client_and_60_overall() -> None:
    settings = Settings(
        _env_file=None,
        upstream_recipes_url="http://recipes:8000",
        upstream_dictionary_url="http://dictionary:8000",
    )
    assert settings.auth_rate_limit_enabled is True
    assert settings.auth_rate_limit_requests == 10
    assert settings.auth_rate_limit_global_requests == 60


async def test_auth_and_llm_limits_do_not_share_counters(user) -> None:
    app = make_app(user, auth_rate_limit_requests=1, rate_limit_requests=1)
    async with client_for(app) as client:
        assert (await hit(client)).status_code == 200
        assert (await hit(client)).status_code == 429
        # LLM-бакет нетронут, хотя auth исчерпан
        generated = await client.post("/recipes/generate-menu", content=b"{}")
        assert generated.status_code != 429
        assert (
            await client.post("/recipes/generate-menu", content=b"{}")
        ).status_code == 429
        assert (await hit(client)).status_code == 429


async def test_auth_global_ceiling_applies_across_clients_and_refunds_the_slot(
    user,
) -> None:
    app = make_app(user, auth_rate_limit_requests=5, auth_rate_limit_global_requests=2)
    async with client_for(app, "198.51.100.1") as first:
        assert (await hit(first)).status_code == 200
    async with client_for(app, "198.51.100.2") as second:
        assert (await hit(second)).status_code == 200
    async with client_for(app, "198.51.100.3") as third:
        assert (await hit(third)).status_code == 429
    # отказанный по общему потолку не занял слот в бакете клиента
    assert app.state.auth_rate_limiter.tracked_keys == 2


@pytest.mark.parametrize(
    "overrides",
    [{"auth_rate_limit_enabled": False}, {"rate_limit_enabled": False}],
)
async def test_auth_limit_can_be_switched_off(user, overrides: dict) -> None:
    app = make_app(user, auth_rate_limit_requests=1, **overrides)
    assert app.state.auth_rate_limiter is None
    async with client_for(app) as client:
        codes = [(await hit(client)).status_code for _ in range(3)]
    assert codes == [200, 200, 200]


@pytest.mark.parametrize(
    "failure",
    [httpx.ConnectError("refused"), httpx.PoolTimeout("busy")],
)
async def test_attempts_that_never_reached_the_service_return_the_slot(
    user, failure: Exception
) -> None:
    user.failure = failure
    app = make_app(user, auth_rate_limit_requests=1)
    async with client_for(app) as client:
        codes = [(await hit(client)).status_code for _ in range(3)]
    assert 429 not in codes


async def test_busy_user_service_returns_the_slot(user) -> None:
    user.respond(503, error_body("SERVICE_BUSY"))
    app = make_app(user, auth_rate_limit_requests=1)
    async with client_for(app) as client:
        codes = [(await hit(client)).status_code for _ in range(3)]
    assert codes == [503, 503, 503]


async def test_attempts_that_reached_the_service_keep_their_slot(user) -> None:
    user.respond(401, error_body("INVALID_CREDENTIALS"))
    app = make_app(user, auth_rate_limit_requests=2)
    async with client_for(app) as client:
        codes = [(await hit(client)).status_code for _ in range(3)]
    assert codes == [401, 401, 429]


def test_gateway_does_not_start_when_an_auth_route_is_missing() -> None:
    settings = Settings(
        _env_file=None,
        upstream_recipes_url="http://recipes:8000",
        upstream_dictionary_url="http://dictionary:8000",
    )
    routes = load_routes(settings.routes_file, settings.upstream_urls())
    contract = load_contracts(
        settings.contract_file,
        settings.contract_dictionary_file,
        settings.contract_user_file,
    )
    no_login = Route(AUTH_ROUTE.prefix, AUTH_ROUTE.upstream, (AUTH_ROUTE.endpoints[0],))
    with pytest.raises(ContractError, match="/auth/login"):
        register_routes(FastAPI(), (*routes, no_login), contract, 65536)


def test_auth_is_a_reserved_prefix(tmp_path) -> None:
    from gateway.routing import RouteConfigError

    routes = tmp_path / "routes.toml"
    routes.write_text(
        '[[route]]\nprefix = "auth"\nupstream = "user"\nallow = ["POST /x"]\n'
    )
    with pytest.raises(RouteConfigError, match="reserved"):
        load_routes(routes, {"user"})


# --- документация ---


def has_ref(node: object) -> bool:
    if isinstance(node, dict):
        return "$ref" in node or any(has_ref(v) for v in node.values())
    if isinstance(node, list):
        return any(has_ref(v) for v in node)
    return False


def test_user_contract_has_no_refs() -> None:
    assert not has_ref(CONTRACT)


@pytest.fixture
def schema(user) -> dict:
    return make_app(user).openapi()


def test_openapi_documents_both_auth_routes_with_every_error(schema: dict) -> None:
    register = schema["paths"]["/auth/register"]["post"]
    login = schema["paths"]["/auth/login"]["post"]
    assert set(register["responses"]) == {
        "201",
        "400",
        "409",
        "413",
        "429",
        "500",
        "502",
        "503",
        "504",
    }
    assert set(login["responses"]) == {
        "200",
        "400",
        "401",
        "413",
        "429",
        "500",
        "502",
        "503",
        "504",
    }
    for operation in (register, login):
        assert operation["tags"] == ["auth"]
        assert operation["summary"] and operation["description"]
        assert operation["requestBody"]["content"]["application/json"]["example"]
        for response in operation["responses"].values():
            media = response["content"]["application/json"]
            assert "example" in media or "examples" in media


def test_auth_400_does_not_mention_gateway_path_validation(schema: dict) -> None:
    for path in ("/auth/register", "/auth/login"):
        response = schema["paths"][path]["post"]["responses"]["400"]
        assert "небезопасн" not in response["description"].lower()
        assert "gateway_path" not in response["content"]["application/json"]["examples"]
    recipes = schema["paths"]["/recipes/generate-menu"]["post"]["responses"]["400"]
    assert "gateway_path" in recipes["content"]["application/json"]["examples"]


def test_openapi_429_and_503_document_retry_after(schema: dict) -> None:
    for path in ("/auth/register", "/auth/login"):
        responses = schema["paths"][path]["post"]["responses"]
        example = responses["429"]["content"]["application/json"]["example"]
        assert example["error"]["details"] == {"retry_after_seconds": 30}
        assert "Retry-After" in responses["429"]["headers"]
        assert "Retry-After" in responses["503"]["headers"]
        busy = responses["503"]["content"]["application/json"]["examples"]["busy"]
        assert busy["value"]["error"]["details"]["reason"] == "busy"


def test_every_example_in_the_contract_validates_against_its_schema() -> None:
    checked = 0
    for item in CONTRACT["paths"].values():
        for response in item["post"]["responses"].values():
            media = response["content"]["application/json"]
            values = (
                [media["example"]]
                if "example" in media
                else [e["value"] for e in media["examples"].values()]
            )
            for value in values:
                jsonschema.validate(value, media["schema"])
                checked += 1
    assert checked >= 14


async def test_real_responses_match_the_documented_examples(user, schema: dict) -> None:
    def documented(path: str, status: str, name: str | None = None) -> dict:
        media = schema["paths"][path]["post"]["responses"][status]["content"][
            "application/json"
        ]
        return media["examples"][name]["value"] if name else media["example"]

    app = make_app(user, auth_rate_limit_requests=1)
    async with client_for(app) as client:
        user.respond(409, documented("/auth/register", "409"))
        assert (await hit(client, "/auth/register")).json() == documented(
            "/auth/register", "409"
        )
        limited = await hit(client, "/auth/register")
        example = documented("/auth/register", "429")
        # Число секунд зависит от окна; в примере оно иллюстративное.
        assert limited.json()["error"]["code"] == example["error"]["code"]
        assert limited.json()["error"]["message"] == example["error"]["message"]
        assert list(limited.json()["error"]["details"]) == ["retry_after_seconds"]
        assert limited.headers["retry-after"]
    app = make_app(user)
    async with client_for(app) as client:
        user.respond(401, documented("/auth/login", "401"))
        assert (await hit(client)).json() == documented("/auth/login", "401")
        user.respond(
            503,
            error_body("SERVICE_BUSY", "Service is busy, retry later"),
            {"retry-after": "5"},
        )
        assert (await hit(client)).json() == documented("/auth/login", "503", "busy")
        user.respond(201, {"userId": 42})
        ok = await hit(client, "/auth/register")
        jsonschema.validate(
            ok.json(),
            schema["paths"]["/auth/register"]["post"]["responses"]["201"]["content"][
                "application/json"
            ]["schema"],
        )
