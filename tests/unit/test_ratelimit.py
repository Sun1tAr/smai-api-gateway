import ipaddress

import pytest

from gateway.ratelimit import (
    RateLimiter,
    parse_networks,
    rate_limit_key,
    resolve_client_ip,
)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def limiter(clock: Clock, **overrides) -> RateLimiter:
    options = {"requests": 3, "window_seconds": 60, "max_keys": 100, **overrides}
    return RateLimiter(clock=clock, **options)


def test_requests_up_to_the_limit_pass_and_the_next_is_rejected() -> None:
    clock = Clock()
    rl = limiter(clock)
    assert [rl.check("a") for _ in range(3)] == [None, None, None]
    assert rl.check("a") == 60


def test_retry_after_counts_down_to_the_oldest_attempt() -> None:
    clock = Clock()
    rl = limiter(clock)
    for _ in range(3):
        rl.check("a")
        clock.now += 10
    # Самая старая попытка была 30 с назад: освободится через 30 с.
    assert rl.check("a") == 30
    clock.now += 0.2
    assert rl.check("a") == 30  # 29.8 с округляется вверх


def test_retry_after_is_at_least_one_second() -> None:
    clock = Clock()
    rl = limiter(clock, requests=1)
    rl.check("a")
    clock.now += 59.9999
    assert rl.check("a") == 1


def test_window_slides_instead_of_resetting() -> None:
    clock = Clock()
    rl = limiter(clock)
    for _ in range(3):
        rl.check("a")
        clock.now += 20
    # Первая попытка вышла из окна (60 с), вторая и третья ещё в нём.
    assert rl.check("a") is None
    assert rl.check("a") is not None


def test_rejected_attempts_are_not_counted() -> None:
    clock = Clock()
    rl = limiter(clock)
    for _ in range(3):
        rl.check("a")
    for _ in range(50):
        assert rl.check("a") is not None
    clock.now += 60
    assert rl.check("a") is None


def test_keys_are_independent() -> None:
    rl = limiter(Clock())
    for _ in range(3):
        rl.check("a")
    assert rl.check("a") is not None
    assert rl.check("b") is None


def test_expired_keys_are_swept() -> None:
    clock = Clock()
    rl = limiter(clock)
    for key in "abcde":
        rl.check(key)
    assert rl.tracked_keys == 5
    clock.now += 61
    rl.check("fresh")
    assert rl.tracked_keys == 1


def test_key_table_never_exceeds_the_ceiling() -> None:
    rl = limiter(Clock(), max_keys=50)
    for number in range(1000):
        rl.check(f"10.0.{number // 256}.{number % 256}")
        assert rl.tracked_keys <= 50


def test_ceiling_evicts_the_least_recently_used_key() -> None:
    clock = Clock()
    rl = limiter(clock, requests=1, max_keys=2)
    rl.check("old")
    clock.now += 1
    rl.check("mid")
    clock.now += 1
    assert rl.check("old") is not None  # отклонён и давность не обновляет
    assert list(rl._log) == ["old", "mid"]
    rl.check("new")  # вытесняет самый давний
    assert list(rl._log) == ["mid", "new"]


def test_fingerprint_hides_the_address_and_is_stable() -> None:
    rl = limiter(Clock())
    first = rl.fingerprint("203.0.113.7")
    assert first == rl.fingerprint("203.0.113.7")
    assert "203" not in first
    assert first != rl.fingerprint("203.0.113.8")
    assert first != limiter(Clock()).fingerprint("203.0.113.7")


TRUSTED = parse_networks("10.0.0.0/8, 192.168.1.5")


@pytest.mark.parametrize(
    ("peer", "forwarded", "expected"),
    [
        # Сосед не доверенный: заголовок игнорируется.
        ("203.0.113.9", ["1.2.3.4"], "203.0.113.9"),
        # Сосед доверенный: берём клиента из заголовка.
        ("10.1.1.1", ["203.0.113.9"], "203.0.113.9"),
        # Подделка клиентом в начале цепочки не работает: берём правый недоверенный.
        ("10.1.1.1", ["6.6.6.6, 203.0.113.9"], "203.0.113.9"),
        ("10.1.1.1", ["6.6.6.6", "203.0.113.9, 10.2.2.2"], "203.0.113.9"),
        # Нет заголовка: адрес прокси.
        ("10.1.1.1", [], "10.1.1.1"),
        # Вся цепочка доверенная.
        ("10.1.1.1", ["10.2.2.2"], "10.2.2.2"),
        # Мусор в цепочке: остаёмся на последнем проверенном адресе.
        ("10.1.1.1", ["6.6.6.6, not-an-ip"], "10.1.1.1"),
        ("10.1.1.1", ["unknown"], "10.1.1.1"),
        # IPv4 в виде IPv6.
        ("::ffff:10.1.1.1", ["203.0.113.9"], "203.0.113.9"),
        ("192.168.1.5", ["198.51.100.4"], "198.51.100.4"),
        (None, ["1.2.3.4"], "unknown"),
        ("testclient", [], "unknown"),
    ],
)
def test_client_ip_resolution(peer, forwarded, expected) -> None:
    assert resolve_client_ip(peer, forwarded, TRUSTED) == expected


def test_without_trusted_proxies_the_header_is_never_read() -> None:
    assert resolve_client_ip("10.1.1.1", ["1.2.3.4"], ()) == "10.1.1.1"


def test_ipv6_clients_share_a_key_per_64_prefix() -> None:
    a = rate_limit_key("2001:db8:1:2:aaaa::1")
    b = rate_limit_key("2001:db8:1:2:bbbb::2")
    c = rate_limit_key("2001:db8:1:3::1")
    assert a == b != c
    assert ipaddress.ip_network(a).prefixlen == 64
    assert rate_limit_key("203.0.113.9") == "203.0.113.9"
    assert rate_limit_key("unknown") == "unknown"


@pytest.mark.parametrize("bad", ["10.0.0.0/33", "abc", "10.0.0.1;10.0.0.2"])
def test_invalid_networks_are_rejected(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_networks(bad)
