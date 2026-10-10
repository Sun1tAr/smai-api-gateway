import json

import httpx
import pytest
from pydantic import ValidationError

from gateway.config import Settings
from gateway.main import create_app
from gateway.ratelimit import RateLimiter
from tests.conftest import Recorder

TASK = "b1e2c3d4-5678-90ab-cdef-1234567890ab"
POST = "/recipes/generate-menu"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def build(upstream: Recorder, clock: Clock | None = None, **overrides):
    values = {"rate_limit_requests": 3, "rate_limit_window_seconds": 60, **overrides}
    settings = Settings(
        _env_file=None, upstream_recipes_url="http://recipes:8000", **values
    )
    app = create_app(settings, transport=httpx.MockTransport(upstream))
    if clock is not None and app.state.rate_limiter is not None:
        app.state.rate_limiter = RateLimiter(
            requests=settings.rate_limit_requests,
            window_seconds=settings.rate_limit_window_seconds,
            max_keys=settings.rate_limit_max_keys,
            clock=clock,
        )
    return app


def client(app, ip: str = "203.0.113.9") -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(ip, 5555)),
        base_url="http://gateway",
    )


async def test_over_the_limit_gives_429_with_retry_after(upstream: Recorder) -> None:
    app = build(upstream, Clock())
    async with client(app) as c:
        codes = [(await c.post(POST, content=b"{}")).status_code for _ in range(3)]
        rejected = await c.post(POST, content=b"{}")
    assert codes == [202, 202, 202]
    assert rejected.status_code == 429
    assert rejected.headers["retry-after"] == "60"
    assert rejected.headers["x-request-id"]
    assert rejected.json() == {
        "error": {
            "code": "RATE_LIMITED",
            "message": "Too many requests, try again later",
            "details": {"retry_after_seconds": 60},
        }
    }
    assert len(upstream.requests) == 3  # отклонённый запрос в MS-4 не ушёл


async def test_limit_resets_after_the_window(upstream: Recorder) -> None:
    clock = Clock()
    app = build(upstream, clock)
    async with client(app) as c:
        for _ in range(3):
            await c.post(POST, content=b"{}")
        assert (await c.post(POST, content=b"{}")).status_code == 429
        clock.now += 61
        assert (await c.post(POST, content=b"{}")).status_code == 202


async def test_each_client_has_its_own_budget(upstream: Recorder) -> None:
    app = build(upstream, Clock())
    async with (
        client(app, "203.0.113.1") as first,
        client(app, "203.0.113.2") as second,
    ):
        for _ in range(3):
            await first.post(POST, content=b"{}")
        assert (await first.post(POST, content=b"{}")).status_code == 429
        assert (await second.post(POST, content=b"{}")).status_code == 202


async def test_polling_get_is_not_limited(upstream: Recorder) -> None:
    app = build(upstream, Clock())
    async with client(app) as c:
        for _ in range(20):
            assert (await c.get(f"{POST}/{TASK}")).status_code == 202
        assert (await c.post(POST, content=b"{}")).status_code == 202


async def test_disabled_limit_never_rejects(upstream: Recorder) -> None:
    app = build(upstream, rate_limit_enabled=False)
    assert app.state.rate_limiter is None
    async with client(app) as c:
        for _ in range(30):
            assert (await c.post(POST, content=b"{}")).status_code == 202


def test_openapi_is_identical_with_the_limit_on_and_off(upstream: Recorder) -> None:
    on = build(upstream).openapi()
    off = build(upstream, rate_limit_enabled=False).openapi()
    assert on == off


def test_429_is_documented_only_for_the_limited_operation(upstream: Recorder) -> None:
    paths = build(upstream).openapi()["paths"]
    post = paths[POST]["post"]["responses"]["429"]
    assert post["headers"]["Retry-After"]["schema"]["type"] == "integer"
    example = post["content"]["application/json"]["example"]
    assert example["error"]["code"] == "RATE_LIMITED"
    assert example["error"]["details"] == {"retry_after_seconds": 30}
    assert "429" not in paths[f"{POST}/{{task_id}}"]["get"]["responses"]


@pytest.mark.parametrize(
    ("path", "status"),
    [
        # Нормализация роутера даёт тот же маршрут: попытка считается и получает 429.
        ("/recipes/generate%2Dmenu", 429),
        # Другие формы не совпадают с маршрутом и в MS-4 не уходят.
        ("/recipes/generate-menu/", 404),
        ("/recipes//generate-menu", 404),
        ("/recipes/generate-menu%2f", 404),
    ],
)
async def test_path_variants_cannot_bypass_the_limit(
    upstream: Recorder, path: str, status: int
) -> None:
    app = build(upstream, Clock())
    async with client(app) as c:
        for _ in range(3):
            await c.post(POST, content=b"{}")
        response = await c.post(path, content=b"{}")
    assert response.status_code == status
    assert len(upstream.requests) == 3


async def test_forged_forwarded_for_from_an_untrusted_peer_is_ignored(
    upstream: Recorder,
) -> None:
    app = build(upstream, Clock(), trusted_proxies="10.0.0.0/8")
    async with client(app, "203.0.113.9") as c:
        for number in range(3):
            await c.post(
                POST, content=b"{}", headers={"X-Forwarded-For": f"1.1.1.{number}"}
            )
        response = await c.post(
            POST, content=b"{}", headers={"X-Forwarded-For": "9.9.9.9"}
        )
    assert response.status_code == 429
    assert upstream.requests[0].headers["x-forwarded-for"] == "203.0.113.9"


async def test_trusted_proxy_forwards_the_real_client_address(
    upstream: Recorder,
) -> None:
    app = build(upstream, Clock(), trusted_proxies="10.0.0.0/8")
    async with client(app, "10.1.1.1") as proxy:
        for _ in range(3):
            await proxy.post(
                POST,
                content=b"{}",
                headers={"X-Forwarded-For": "6.6.6.6, 198.51.100.1"},
            )
        same = await proxy.post(
            POST, content=b"{}", headers={"X-Forwarded-For": "7.7.7.7, 198.51.100.1"}
        )
        other = await proxy.post(
            POST, content=b"{}", headers={"X-Forwarded-For": "198.51.100.2"}
        )
    assert same.status_code == 429  # подделанное начало цепочки не помогает
    assert other.status_code == 202
    assert upstream.requests[0].headers["x-forwarded-for"] == "198.51.100.1"


async def test_rejections_are_counted_without_addresses(upstream: Recorder) -> None:
    app = build(upstream, Clock())
    async with client(app) as c:
        for _ in range(4):
            await c.post(POST, content=b"{}")
        text = (await c.get("/metrics")).text
    assert 'gateway_rate_limited_total{route="/recipes/generate-menu"} 1.0' in text
    assert "203.0.113" not in text


async def test_log_has_a_hash_of_the_client_not_the_address(
    upstream: Recorder, capsys: pytest.CaptureFixture[str]
) -> None:
    app = build(upstream, Clock())
    async with client(app) as c:
        for _ in range(4):
            await c.post(POST, content=b"{}")
    output = capsys.readouterr().out
    events = [json.loads(line) for line in output.splitlines() if line.startswith("{")]
    event = next(e for e in events if e["message"] == "rate_limited")
    assert len(event["client"]) == 12
    assert event["retry_after_seconds"] == 60
    assert "203.0.113.9" not in output


async def test_startup_warns_when_the_key_is_the_socket_address(
    upstream: Recorder, capsys: pytest.CaptureFixture[str]
) -> None:
    for trusted, warned in (("", True), ("10.0.0.0/8", False)):
        app = build(upstream, trusted_proxies=trusted)
        async with app.router.lifespan_context(app):
            pass
        output = capsys.readouterr().out
        assert ("rate_limit_keyed_by_socket_address" in output) is warned
    app = build(upstream, rate_limit_enabled=False)
    async with app.router.lifespan_context(app):
        pass
    assert "rate_limit_keyed_by_socket_address" not in capsys.readouterr().out


@pytest.mark.parametrize(
    "bad",
    [
        {"rate_limit_requests": 0},
        {"rate_limit_window_seconds": 0},
        {"rate_limit_max_keys": 0},
        {"trusted_proxies": "10.0.0.0/99"},
        {"trusted_proxies": "proxy.local"},
    ],
)
def test_invalid_rate_limit_settings_stop_the_start(bad: dict) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, upstream_recipes_url="http://recipes:8000", **bad)
