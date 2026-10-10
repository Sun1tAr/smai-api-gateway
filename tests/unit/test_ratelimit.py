import ipaddress

import pytest

from gateway.ratelimit import (
    RateLimiter,
    ResolvedClient,
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


def wait(rl: RateLimiter, key: str) -> int | None:
    return rl.check(key).retry_after


def test_requests_up_to_the_limit_pass_and_the_next_is_rejected() -> None:
    rl = limiter(Clock())
    assert [wait(rl, "a") for _ in range(3)] == [None, None, None]
    assert wait(rl, "a") == 60


def test_retry_after_counts_down_to_the_oldest_attempt() -> None:
    clock = Clock()
    rl = limiter(clock)
    for _ in range(3):
        rl.check("a")
        clock.now += 10
    # Самая старая попытка была 30 с назад: освободится через 30 с.
    assert wait(rl, "a") == 30
    clock.now += 0.2
    assert wait(rl, "a") == 30  # 29.8 с округляется вверх


def test_client_who_waits_retry_after_is_admitted() -> None:
    clock = Clock()
    rl = limiter(clock, requests=2, window_seconds=7)
    for step in (0, 1.3, 2.9):
        clock.now = 1000 + step
        denied = rl.check("a").retry_after
        if denied is not None:
            clock.now += denied
            assert wait(rl, "a") is None


def test_window_slides_instead_of_resetting() -> None:
    clock = Clock()
    rl = limiter(clock)
    for _ in range(3):
        rl.check("a")
        clock.now += 20
    # Первая попытка вышла из окна (60 с), вторая и третья ещё в нём.
    assert wait(rl, "a") is None
    assert wait(rl, "a") is not None


def test_attempt_exactly_one_window_old_no_longer_counts() -> None:
    clock = Clock()
    rl = limiter(clock, requests=1)
    rl.check("a")
    clock.now += 59.99
    assert wait(rl, "a") == 1
    clock.now += 0.01
    assert wait(rl, "a") is None


def test_rejected_attempts_are_not_counted() -> None:
    clock = Clock()
    rl = limiter(clock)
    for _ in range(3):
        rl.check("a")
    for _ in range(50):
        assert wait(rl, "a") is not None
    clock.now += 60
    assert wait(rl, "a") is None


def test_keys_are_independent() -> None:
    rl = limiter(Clock())
    for _ in range(3):
        rl.check("a")
    assert wait(rl, "a") is not None
    assert wait(rl, "b") is None


def test_denial_is_flagged_first_only_once_per_window_position() -> None:
    clock = Clock()
    rl = limiter(clock, requests=1)
    rl.check("a")
    assert rl.check("a").first_denial is True
    assert rl.check("a").first_denial is False
    clock.now += 61
    rl.check("a")
    assert rl.check("a").first_denial is True


def test_expired_keys_are_swept() -> None:
    clock = Clock()
    rl = limiter(clock)
    for key in "abcde":
        rl.check(key)
    assert rl.tracked_keys == 5
    clock.now += 61
    rl.check("fresh")
    assert rl.tracked_keys == 1


def test_key_whose_last_attempt_is_exactly_one_window_old_is_swept() -> None:
    clock = Clock()
    rl = limiter(clock)
    rl.check("a")
    clock.now += 60
    rl.check("b")
    assert rl.tracked_keys == 1


def test_sweep_stops_at_the_first_active_key() -> None:
    clock = Clock()
    rl = limiter(clock)
    rl.check("a")
    clock.now += 30
    rl.check("b")
    clock.now += 31  # a истёк, b ещё активен
    rl.check("c")
    assert rl.tracked_keys == 2


def test_recently_accepted_key_moves_to_the_back_of_the_queue() -> None:
    # a принят снова, значит b теперь самый давний и истекает первым.
    clock = Clock()
    rl = limiter(clock)
    rl.check("a")
    clock.now += 10
    rl.check("b")
    clock.now += 10
    rl.check("a")
    clock.now += 45  # b: 55 с назад ещё активен? нет, 45+10=55 < 60, активен
    rl.check("c")
    assert rl.tracked_keys == 3
    clock.now += 6  # b последний раз был 61 с назад, a — 51 с
    rl.check("d")
    assert rl.tracked_keys == 3  # b истёк (a, c, d остались)


def test_full_table_rejects_new_keys_instead_of_evicting_active_ones() -> None:
    clock = Clock()
    rl = limiter(clock, requests=1, max_keys=2)
    rl.check("a")
    clock.now += 5
    rl.check("b")
    denied = rl.check("c")
    assert denied.table_full is True
    assert denied.retry_after == 55  # a освободится через 60 - 5 с
    assert rl.tracked_keys == 2
    assert wait(rl, "a") is not None  # чужая запись не вытеснена
    clock.now += 56
    assert rl.check("c").allowed  # a истёк, место освободилось


def test_key_table_never_exceeds_the_ceiling() -> None:
    rl = limiter(Clock(), max_keys=50)
    for number in range(1000):
        rl.check(f"10.0.{number // 256}.{number % 256}")
        assert rl.tracked_keys <= 50


def test_refund_returns_the_slot_and_drops_empty_keys() -> None:
    rl = limiter(Clock(), requests=1)
    first = rl.check("a")
    assert wait(rl, "a") is not None
    rl.refund("a", first.stamp)
    assert rl.tracked_keys == 0
    assert wait(rl, "a") is None


def test_refund_of_an_unknown_or_expired_stamp_is_harmless() -> None:
    rl = limiter(Clock())
    rl.refund("missing", 1.0)
    rl.check("a")
    rl.refund("a", 123.0)
    assert rl.tracked_keys == 1


def test_fingerprint_hides_the_address_and_is_stable() -> None:
    rl = limiter(Clock())
    first = rl.fingerprint("203.0.113.7")
    assert first == rl.fingerprint("203.0.113.7")
    assert "203" not in first
    assert first != rl.fingerprint("203.0.113.8")
    assert first != limiter(Clock()).fingerprint("203.0.113.7")


TRUSTED = parse_networks("10.0.0.0/8, 192.168.1.5")


@pytest.mark.parametrize(
    ("peer", "forwarded", "ip", "fallback"),
    [
        # Сосед не доверенный: заголовок игнорируется.
        ("203.0.113.9", ["1.2.3.4"], "203.0.113.9", None),
        # Сосед доверенный: берём клиента из заголовка.
        ("10.1.1.1", ["203.0.113.9"], "203.0.113.9", None),
        # Подделка клиентом в начале цепочки не работает: правый недоверенный.
        ("10.1.1.1", ["6.6.6.6, 203.0.113.9"], "203.0.113.9", None),
        ("10.1.1.1", ["6.6.6.6", "203.0.113.9, 10.2.2.2"], "203.0.113.9", None),
        # Нет заголовка: адрес прокси.
        ("10.1.1.1", [], "10.1.1.1", "no_xff"),
        ("10.1.1.1", [""], "10.1.1.1", "no_xff"),
        ("10.1.1.1", [" , "], "10.1.1.1", "no_xff"),
        # Вся цепочка доверенная.
        ("10.1.1.1", ["10.2.2.2"], "10.2.2.2", None),
        # Мусор в цепочке: остаёмся на последнем проверенном адресе.
        ("10.1.1.1", ["6.6.6.6, not-an-ip"], "10.1.1.1", "garbage"),
        ("10.1.1.1", ["unknown"], "10.1.1.1", "garbage"),
        # Порт, хвостовая запятая, скобки: так пишут разные прокси.
        ("10.1.1.1", ["203.0.113.9:5555"], "203.0.113.9", None),
        ("10.1.1.1", ["203.0.113.9,"], "203.0.113.9", None),
        ("10.1.1.1", ["[2001:db8::1]:443"], "2001:db8::1", None),
        ("10.1.1.1", ["[2001:db8::1]"], "2001:db8::1", None),
        ("10.1.1.1", ["2001:db8::1"], "2001:db8::1", None),
        ("10.1.1.1", ["203.0.113.9:port"], "10.1.1.1", "garbage"),
        ("10.1.1.1", ["[2001:db8::1"], "10.1.1.1", "garbage"),
        ("10.1.1.1", ["[2001:db8::1]x"], "10.1.1.1", "garbage"),
        ("10.1.1.1", ["[2001:db8::1]:abc"], "10.1.1.1", "garbage"),
        ("10.1.1.1", ["[2001:db8::1]:"], "10.1.1.1", "garbage"),
        # zone id и не-ASCII не пропускаем: дальше адрес уходит в заголовок MS-4.
        ("10.1.1.1", ["fe80::1%eth0"], "10.1.1.1", "garbage"),
        ("10.1.1.1", ["fe80::1%é"], "10.1.1.1", "garbage"),
        # IPv4 в виде IPv6.
        ("::ffff:10.1.1.1", ["203.0.113.9"], "203.0.113.9", None),
        ("192.168.1.5", ["198.51.100.4"], "198.51.100.4", None),
        # Адрес соседа неизвестен.
        (None, ["1.2.3.4"], None, None),
        ("testclient", [], None, None),
    ],
)
def test_client_ip_resolution(peer, forwarded, ip, fallback) -> None:
    assert resolve_client_ip(peer, forwarded, TRUSTED) == ResolvedClient(ip, fallback)


def test_without_trusted_proxies_the_header_is_never_read() -> None:
    assert resolve_client_ip("10.1.1.1", ["1.2.3.4"], ()) == ResolvedClient("10.1.1.1")


def test_ipv6_clients_share_a_key_per_prefix() -> None:
    a = rate_limit_key("2001:db8:1:2:aaaa::1")
    b = rate_limit_key("2001:db8:1:2:bbbb::2")
    same_56 = rate_limit_key("2001:db8:1:3::1")
    other_56 = rate_limit_key("2001:db8:2::1")
    assert a == b == same_56 != other_56
    assert ipaddress.ip_network(a).prefixlen == 56
    narrow = {rate_limit_key(ip, 64) for ip in ("2001:db8:1:2::1", "2001:db8:1:3::1")}
    assert len(narrow) == 2
    assert rate_limit_key("203.0.113.9") == "203.0.113.9"
    assert rate_limit_key(None) == "unknown"


@pytest.mark.parametrize(
    "bad",
    [
        "10.0.0.0/33",
        "abc",
        "10.0.0.1;10.0.0.2",
        "0.0.0.0/0",
        "::/0",
        "10.0.0.0/7",
        "2001:db8::/31",
        "10.1.2.3/8",  # биты хоста: опечатка молча расширила бы доверие
    ],
)
def test_invalid_or_too_wide_networks_are_rejected(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_networks(bad)


def test_blank_entries_are_skipped_and_single_addresses_are_accepted() -> None:
    assert parse_networks(" , ") == ()
    assert [str(n) for n in parse_networks("10.0.0.0/8, 192.168.1.5,")] == [
        "10.0.0.0/8",
        "192.168.1.5/32",
    ]


def test_table_full_wait_uses_the_last_attempt_of_the_earliest_expiring_key() -> None:
    clock = Clock()
    rl = limiter(clock, requests=2, max_keys=1)
    rl.check("a")
    clock.now += 40
    rl.check("a")  # последняя попытка t=1040, первая t=1000
    clock.now += 10
    assert rl.check("b").retry_after == 50  # 1040 + 60 - 1050


def test_refund_keeps_table_full_answers_correct() -> None:
    clock = Clock()
    rl = limiter(clock, requests=2, max_keys=2)
    first = rl.check("a")  # t=1000
    clock.now += 10
    rl.check("b")  # t=1010
    clock.now += 10
    second = rl.check("a")  # t=1020: a в конце очереди
    rl.refund("a", second.stamp)
    assert first.allowed
    clock.now += 41  # t=1061: у a осталась попытка t=1000, она истекла; b активен
    assert rl.check("c").allowed
