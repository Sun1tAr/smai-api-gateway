import json
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from gateway.config import Settings
from gateway.main import create_app
from gateway.ratelimit import RateLimiter
from tests.conftest import Recorder

EXAMPLE_ENV = Path(__file__).resolve().parents[2] / ".env.example"
TASK = "b1e2c3d4-5678-90ab-cdef-1234567890ab"
POST = "/recipes/generate-menu"
CART = "/recipes/cart"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def build(upstream: Recorder, clock: Clock | None = None, **overrides):
    values = {
        "rate_limit_requests": 3,
        "rate_limit_window_seconds": 60,
        "rate_limit_global_requests": 1000,
        **overrides,
    }
    settings = Settings(
        _env_file=None,
        upstream_recipes_url="http://recipes:8000",
        upstream_dictionary_url="http://dictionary:8000",
        **values,
    )
    app = create_app(settings, transport=httpx.MockTransport(upstream))
    if clock is not None and app.state.rate_limiter is not None:
        app.state.rate_limiter = RateLimiter(
            requests=settings.rate_limit_requests,
            window_seconds=settings.rate_limit_window_seconds,
            max_keys=settings.rate_limit_max_keys,
            clock=clock,
        )
        app.state.global_limiter = RateLimiter(
            requests=settings.rate_limit_global_requests,
            window_seconds=settings.rate_limit_window_seconds,
            max_keys=1,
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


async def test_cart_has_a_separate_client_budget(upstream: Recorder) -> None:
    app = build(upstream, rate_limit_requests=1)
    async with client(app) as c:
        assert (await c.post(POST)).status_code == 202
        upstream.respond(
            200, {"matches": [], "total": 0, "allMatched": True, "unmatchedCount": 0}
        )
        assert (await c.post(CART)).status_code == 200
        rejected = await c.post(CART)
    assert rejected.status_code == 429
    wait = int(rejected.headers["retry-after"])
    assert 1 <= wait <= 60
    assert rejected.json()["error"]["details"] == {"retry_after_seconds": wait}
    assert [request.url.path for request in upstream.requests] == [
        "/generate-menu",
        "/recipes/cart",
    ]


async def test_cart_has_a_separate_process_ceiling(upstream: Recorder) -> None:
    app = build(upstream, rate_limit_global_requests=1)
    async with client(app) as c:
        assert (await c.post(POST)).status_code == 202
    codes = []
    for number in range(4):
        async with client(app, f"203.0.113.{number}") as c:
            codes.append((await c.post(CART)).status_code)
    assert codes == [202, 202, 202, 429]


async def test_bad_cart_path_and_oversized_body_refund_both_slots(
    upstream: Recorder,
) -> None:
    app = build(upstream, cart_rate_limit_global_requests=1)
    async with client(app) as first, client(app, "203.0.113.3") as second:
        assert (await first.post("/recipes/%63art")).status_code == 400
        assert (await first.post(CART, content=b"x" * 70_000)).status_code == 413
        assert (await second.post(CART)).status_code == 202
        assert (await first.post(CART)).status_code == 429
    assert len(upstream.requests) == 1


@pytest.mark.parametrize("flag", ["rate_limit_enabled", "cart_rate_limit_enabled"])
async def test_cart_rate_limit_can_be_disabled(upstream: Recorder, flag: str) -> None:
    app = build(upstream, **{flag: False})
    async with client(app) as c:
        assert [(await c.post(CART)).status_code for _ in range(4)] == [202] * 4


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
    assert (
        'gateway_rate_limited_total{route="/recipes/generate-menu",scope="client"} 1.0'
        in text
    )
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
        Settings(
            _env_file=None,
            upstream_recipes_url="http://recipes:8000",
            upstream_dictionary_url="http://dictionary:8000",
            **bad,
        )


# --- общий потолок, возврат слота, разбор клиента ---------------------------------


async def test_global_ceiling_limits_all_clients_together(upstream: Recorder) -> None:
    app = build(upstream, Clock(), rate_limit_global_requests=4)
    codes = []
    for number in range(6):
        async with client(app, f"203.0.113.{number}") as c:
            codes.append((await c.post(POST, content=b"{}")).status_code)
    assert codes == [202, 202, 202, 202, 429, 429]
    assert len(upstream.requests) == 4


async def test_global_rejection_does_not_burn_the_clients_own_slot(
    upstream: Recorder,
) -> None:
    clock = Clock()
    app = build(upstream, clock, rate_limit_global_requests=1, rate_limit_requests=1)
    async with (
        client(app, "203.0.113.1") as first,
        client(app, "203.0.113.2") as second,
    ):
        assert (await first.post(POST, content=b"{}")).status_code == 202  # t=1000
        clock.now += 30
        assert (await second.post(POST, content=b"{}")).status_code == 429  # global
        clock.now += 31  # t=1061: слот первого истёк, общий потолок свободен
        # Слот второго от t=1030 был бы занят до 1090, если бы не вернулся.
        assert (await second.post(POST, content=b"{}")).status_code == 202
        text = (await second.get("/metrics")).text
    assert 'scope="global"} 1.0' in text


async def test_requests_that_never_reached_the_service_return_the_slot(
    upstream: Recorder,
) -> None:
    app = build(upstream, Clock(), rate_limit_requests=1)
    async with client(app) as c:
        too_big = await c.post(POST, content=b"x" * 70_000)
        bad_path = await c.post("/recipes/generate%2Dmenu", content=b"{}")
        assert (too_big.status_code, bad_path.status_code) == (413, 400)
        # Оба слота возвращены: единственная попытка бюджета цела.
        assert (await c.post(POST, content=b"{}")).status_code == 202
        assert (await c.post(POST, content=b"{}")).status_code == 429


@pytest.mark.parametrize("failure", [httpx.ConnectError("x"), httpx.PoolTimeout("x")])
async def test_connect_and_pool_failures_return_the_slot(failure: Exception) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise failure

    settings = Settings(
        _env_file=None,
        upstream_recipes_url="http://recipes:8000",
        upstream_dictionary_url="http://dictionary:8000",
        rate_limit_requests=1,
    )
    app = create_app(settings, transport=httpx.MockTransport(handler))
    async with client(app) as c:
        statuses = [(await c.post(POST, content=b"{}")).status_code for _ in range(3)]
    assert statuses == [502 if isinstance(failure, httpx.ConnectError) else 503] * 3


@pytest.mark.parametrize(
    "failure", [httpx.ReadTimeout("x"), httpx.RemoteProtocolError("x")]
)
async def test_timeouts_and_broken_replies_still_cost_a_slot(
    failure: Exception,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise failure

    settings = Settings(
        _env_file=None,
        upstream_recipes_url="http://recipes:8000",
        upstream_dictionary_url="http://dictionary:8000",
        rate_limit_requests=1,
    )
    app = create_app(settings, transport=httpx.MockTransport(handler))
    async with client(app) as c:
        first = await c.post(POST, content=b"{}")
        second = await c.post(POST, content=b"{}")
    assert first.status_code in (502, 504)
    assert second.status_code == 429  # генерация могла начаться: слот потрачен


async def test_error_replies_of_the_service_cost_a_slot(upstream: Recorder) -> None:
    upstream.respond(
        500, {"error": {"code": "INTERNAL_ERROR", "message": "m", "details": {}}}
    )
    app = build(upstream, Clock(), rate_limit_requests=1)
    async with client(app) as c:
        assert (await c.post(POST, content=b"{}")).status_code == 500
        assert (await c.post(POST, content=b"{}")).status_code == 429


async def test_several_forwarded_for_headers_are_joined(upstream: Recorder) -> None:
    app = build(upstream, Clock(), trusted_proxies="10.0.0.0/8")
    async with client(app, "10.1.1.1") as proxy:
        for _ in range(3):
            await proxy.post(
                POST,
                content=b"{}",
                headers=[
                    ("X-Forwarded-For", "6.6.6.6, 198.51.100.1"),
                    ("X-Forwarded-For", "198.51.100.7"),
                ],
            )
        blocked = await proxy.post(
            POST, content=b"{}", headers=[("X-Forwarded-For", "198.51.100.7")]
        )
    assert blocked.status_code == 429
    assert upstream.requests[0].headers["x-forwarded-for"] == "198.51.100.7"


@pytest.mark.parametrize(
    "xff", ["203.0.113.9:5555", "203.0.113.9,", "[2001:db8::1]:443", "fe80::1%é"]
)
async def test_odd_forwarded_for_values_never_break_requests(
    upstream: Recorder, xff: str
) -> None:
    app = build(upstream, trusted_proxies="10.0.0.0/8")
    async with client(app, "10.1.1.1") as proxy:
        post = await proxy.post(
            POST, content=b"{}", headers={"X-Forwarded-For": xff.encode("utf-8")}
        )
        get = await proxy.get(
            f"{POST}/{TASK}", headers={"X-Forwarded-For": xff.encode("utf-8")}
        )
    assert (post.status_code, get.status_code) == (202, 202)
    assert upstream.requests[0].headers["x-forwarded-for"].isascii()


async def test_fallback_to_the_proxy_address_is_counted(upstream: Recorder) -> None:
    app = build(upstream, trusted_proxies="10.0.0.0/8")
    async with client(app, "10.1.1.1") as proxy:
        await proxy.get(f"{POST}/{TASK}")
        await proxy.get(f"{POST}/{TASK}", headers={"X-Forwarded-For": "junk"})
        text = (await proxy.get("/metrics")).text
    assert 'gateway_client_ip_fallback_total{reason="no_xff"} 1.0' in text
    assert 'gateway_client_ip_fallback_total{reason="garbage"} 1.0' in text


async def test_unknown_client_is_not_sent_to_the_service(upstream: Recorder) -> None:
    app = build(upstream, Clock())
    transport = httpx.ASGITransport(app=app, client=None)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as c:
        assert (await c.post(POST, content=b"{}")).status_code == 202
        assert (await c.post(POST, content=b"{}")).status_code == 202
        assert (await c.post(POST, content=b"{}")).status_code == 202
        assert (await c.post(POST, content=b"{}")).status_code == 429
    assert "x-forwarded-for" not in upstream.requests[0].headers


async def test_ipv6_addresses_of_one_prefix_share_the_budget(
    upstream: Recorder,
) -> None:
    app = build(upstream, Clock())
    codes = []
    for ip in (
        "2001:db8:1:1::1",
        "2001:db8:1:2::1",
        "2001:db8:1:3::1",
        "2001:db8:1:4::1",
    ):
        async with client(app, ip) as c:
            codes.append((await c.post(POST, content=b"{}")).status_code)
    assert codes == [202, 202, 202, 429]
    async with client(app, "2001:db8:2::1") as other:
        assert (await other.post(POST, content=b"{}")).status_code == 202


async def test_ipv6_prefix_is_configurable(upstream: Recorder) -> None:
    app = build(upstream, Clock(), rate_limit_ipv6_prefix=64)
    for ip in (
        "2001:db8:1:1::1",
        "2001:db8:1:2::1",
        "2001:db8:1:3::1",
        "2001:db8:1:4::1",
    ):
        async with client(app, ip) as c:
            assert (await c.post(POST, content=b"{}")).status_code == 202


async def test_full_table_rejects_new_clients_and_is_visible(
    upstream: Recorder,
) -> None:
    app = build(upstream, Clock(), rate_limit_max_keys=2)
    codes = []
    for number in range(3):
        async with client(app, f"203.0.113.{number}") as c:
            codes.append((await c.post(POST, content=b"{}")).status_code)
    async with client(app, "203.0.113.0") as known:
        codes.append((await known.post(POST, content=b"{}")).status_code)
        text = (await known.get("/metrics")).text
    assert codes == [202, 202, 429, 202]
    assert 'scope="table_full"} 1.0' in text
    assert "gateway_rate_limit_tracked_keys 2.0" in text


async def test_metric_series_exist_before_the_first_rejection(
    upstream: Recorder,
) -> None:
    app = build(upstream)
    async with client(app) as c:
        text = (await c.get("/metrics")).text
    for scope in ("client", "global", "table_full"):
        assert (
            f'gateway_rate_limited_total{{route="{POST}",scope="{scope}"}} 0.0' in text
        )
    assert "gateway_rate_limit_tracked_keys 0.0" in text


async def test_burst_of_parallel_requests_never_exceeds_the_limit(
    upstream: Recorder,
) -> None:
    import asyncio

    app = build(upstream, Clock(), rate_limit_requests=10)
    async with client(app) as c:
        responses = await asyncio.gather(
            *(c.post(POST, content=b"{}") for _ in range(50))
        )
    assert sorted(r.status_code for r in responses).count(202) == 10
    assert len(upstream.requests) == 10


async def test_rejections_are_logged_once_per_window(
    upstream: Recorder, capsys: pytest.CaptureFixture[str]
) -> None:
    app = build(upstream, Clock())
    async with client(app) as c:
        for _ in range(10):
            await c.post(POST, content=b"{}")
    events = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("{")
    ]
    assert [e["message"] for e in events].count("rate_limited") == 1


def test_enabled_limit_needs_an_explicit_key_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RATE_LIMIT_KEY_BY_SOCKET")
    base = {
        "_env_file": None,
        "upstream_recipes_url": "http://recipes:8000",
        "upstream_dictionary_url": "http://dictionary:8000",
    }
    with pytest.raises(ValidationError, match="TRUSTED_PROXIES"):
        Settings(**base)
    with pytest.raises(ValidationError):
        Settings(**base, trusted_proxies=" , ")
    assert Settings(**base, trusted_proxies="10.1.2.3").trusted_networks
    assert Settings(**base, rate_limit_key_by_socket=True).rate_limit_enabled
    assert not Settings(**base, rate_limit_enabled=False).rate_limit_enabled


@pytest.mark.parametrize(
    "bad",
    [
        {"trusted_proxies": "0.0.0.0/0"},
        {"trusted_proxies": "10.1.2.3/8"},
        {"rate_limit_requests": 10_000, "rate_limit_max_keys": 1_000_000},
        {"rate_limit_ipv6_prefix": 40},
        {"rate_limit_global_requests": 0},
        {"cart_rate_limit_requests": 10_000, "rate_limit_max_keys": 1_000_000},
        {"cart_rate_limit_global_requests": 0},
        {"upstream_cart_timeout_seconds": 0},
    ],
)
def test_unsafe_rate_limit_settings_stop_the_start(bad: dict) -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            upstream_recipes_url="http://recipes:8000",
            upstream_dictionary_url="http://dictionary:8000",
            **bad,
        )


def test_a_renamed_limited_route_stops_the_start(settings: Settings, tmp_path) -> None:
    from gateway.api import ContractError

    routes = tmp_path / "routes.toml"
    routes.write_text(
        '[[route]]\nprefix = "recipes"\nupstream = "recipes"\n'
        'allow = ["GET /generate-menu/{task_id}"]\n'
    )
    with pytest.raises(ContractError, match="not routed"):
        create_app(settings.model_copy(update={"routes_file": routes}))


async def test_global_slot_is_returned_with_the_clients_slot(
    upstream: Recorder,
) -> None:
    app = build(upstream, Clock(), rate_limit_global_requests=1)
    async with (
        client(app, "203.0.113.1") as first,
        client(app, "203.0.113.2") as second,
    ):
        assert (await first.post(POST, content=b"x" * 70_000)).status_code == 413
        assert (await second.post(POST, content=b"{}")).status_code == 202


async def test_table_full_retry_after_points_at_the_earliest_expiry(
    upstream: Recorder,
) -> None:
    clock = Clock()
    app = build(upstream, clock, rate_limit_max_keys=2, rate_limit_requests=2)
    async with client(app, "203.0.113.1") as a, client(app, "203.0.113.2") as b:
        await a.post(POST, content=b"{}")  # t=1000
        clock.now += 40
        await a.post(POST, content=b"{}")  # t=1040: последняя попытка a
        clock.now += 5
        await b.post(POST, content=b"{}")  # t=1045
        clock.now += 5  # t=1050
        async with client(app, "203.0.113.3") as c:
            denied = await c.post(POST, content=b"{}")
    # Самая давняя запись истечёт по её последней попытке: 1040 + 60 - 1050 = 50.
    assert denied.status_code == 429
    assert denied.headers["retry-after"] == "50"


async def test_refund_does_not_leave_a_dead_key_blocking_a_full_table(
    upstream: Recorder,
) -> None:
    clock = Clock()
    app = build(upstream, clock, rate_limit_max_keys=2, rate_limit_requests=2)
    async with client(app, "203.0.113.1") as a, client(app, "203.0.113.2") as b:
        await a.post(POST, content=b"{}")  # t=1000
        clock.now += 10
        await b.post(POST, content=b"{}")  # t=1010
        clock.now += 10
        # Принят и возвращён: у a остаётся только попытка t=1000, но ключ уже в конце.
        assert (await a.post(POST, content=b"x" * 70_000)).status_code == 413
        clock.now += 41  # t=1061: попытка a истекла, b ещё активен
        async with client(app, "203.0.113.3") as c:
            assert (await c.post(POST, content=b"{}")).status_code == 202


async def test_client_is_resolved_and_counted_once_per_request(
    upstream: Recorder,
) -> None:
    app = build(upstream, trusted_proxies="10.0.0.0/8")
    async with client(app, "10.1.1.1") as proxy:
        await proxy.post(POST, content=b"{}")  # без XFF: откат на адрес прокси
        text = (await proxy.get("/metrics")).text
    assert 'gateway_client_ip_fallback_total{reason="no_xff"} 1.0' in text


def test_example_env_alone_does_not_start_the_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RATE_LIMIT_KEY_BY_SOCKET")
    with pytest.raises(ValidationError, match="TRUSTED_PROXIES"):
        Settings(_env_file=EXAMPLE_ENV)


async def test_startup_warns_about_confusing_proxy_settings(
    upstream: Recorder, capsys: pytest.CaptureFixture[str]
) -> None:
    app = build(
        upstream,
        trusted_proxies="10.0.0.0/8,192.168.1.5",
        rate_limit_key_by_socket=True,
    )
    async with app.router.lifespan_context(app):
        pass
    output = capsys.readouterr().out
    assert "trusted_proxies_override_key_by_socket" in output
    event = next(
        json.loads(line)
        for line in output.splitlines()
        if "trusted_proxy_is_a_network" in line
    )
    assert event["networks"] == ["10.0.0.0/8"]
