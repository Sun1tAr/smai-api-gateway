import hashlib
import hmac
import ipaddress
import math
import os
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from gateway.routing import CART_ENDPOINT, CART_LINK_ENDPOINT

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
Network = ipaddress.IPv4Network | ipaddress.IPv6Network

# Ограничена дорогая LLM-генерация.
LIMITED_ENDPOINTS = frozenset({("POST", "/recipes/generate-menu")})
# Вход и регистрация: перебор паролей и заливка дорогим хэшированием в MS-3.
AUTH_LIMITED_ENDPOINTS = frozenset(
    {("POST", "/auth/register"), ("POST", "/auth/login")}
)
CART_LIMITED_ENDPOINTS = frozenset({CART_ENDPOINT, CART_LINK_ENDPOINT})
RATE_LIMITED_ENDPOINTS = (
    LIMITED_ENDPOINTS | AUTH_LIMITED_ENDPOINTS | CART_LIMITED_ENDPOINTS
)
UNKNOWN_CLIENT = "unknown"
GLOBAL_KEY = "*"
# Доверять можно узкой группе прокси: короткий префикс почти наверняка опечатка.
MIN_TRUSTED_PREFIX = {4: 8, 6: 32}


def parse_networks(value: str) -> tuple[Network, ...]:
    """CIDR или одиночные адреса через запятую; пусто = доверенных прокси нет.

    Адрес с битами хоста (`10.1.2.3/8`) и слишком короткий префикс (`/0`) — ошибка,
    чтобы опечатка не расширяла доверие незаметно.
    """
    networks = []
    for part in value.split(","):
        if not part.strip():
            continue
        network = ipaddress.ip_network(part.strip(), strict=True)
        if network.prefixlen < MIN_TRUSTED_PREFIX[network.version]:
            raise ValueError(f"trusted network {network} is too wide")
        networks.append(network)
    return tuple(networks)


def _parse_ip(raw: str) -> IPAddress | None:
    """Разбирает адрес из цепочки XFF: допускает порт и `[v6]`, но не zone id."""
    token = raw.strip()
    if token.startswith("["):
        end = token.find("]")
        if end == -1:
            return None
        rest = token[end + 1 :]
        if rest and not (rest.startswith(":") and rest[1:].isdigit()):
            return None
        token = token[1:end]
    elif token.count(":") == 1:  # a.b.c.d:port
        token, _, port = token.partition(":")
        if not port.isdigit():
            return None
    try:
        address = ipaddress.ip_address(token)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address):
        if address.scope_id:
            return None
        if address.ipv4_mapped:
            return address.ipv4_mapped
    return address


@dataclass(frozen=True)
class ResolvedClient:
    """Адрес клиента (None, если неизвестен) и причина отката на адрес прокси."""

    ip: str | None
    fallback: str | None = None  # "no_xff" | "garbage"


def resolve_client_ip(
    peer: str | None, forwarded_for: Iterable[str], trusted: tuple[Network, ...]
) -> ResolvedClient:
    """Клиент — первый недоверенный адрес справа в цепочке XFF + адрес соседа.

    Заголовок читается, только если сокетный сосед доверенный: иначе клиент мог бы
    подставить любой XFF и обойти лимит или спрятаться за чужим адресом. Доверенный
    прокси обязан дописывать или перезаписывать XFF; `Forwarded` не поддерживается.
    """
    current = _parse_ip(peer) if peer else None
    if current is None:
        return ResolvedClient(None)
    if not trusted or not _is_trusted(current, trusted):
        return ResolvedClient(str(current))
    hops = [
        token for value in forwarded_for for token in value.split(",") if token.strip()
    ]
    if not hops:
        return ResolvedClient(str(current), "no_xff")
    for token in reversed(hops):
        candidate = _parse_ip(token)
        # Мусор в цепочке: дальше ей верить нельзя, остаёмся на последнем адресе.
        if candidate is None:
            return ResolvedClient(str(current), "garbage")
        current = candidate
        if not _is_trusted(current, trusted):
            break
    return ResolvedClient(str(current))


def _is_trusted(address: IPAddress, trusted: tuple[Network, ...]) -> bool:
    return any(address.version == net.version and address in net for net in trusted)


def rate_limit_key(client_ip: str | None, ipv6_prefix: int = 56) -> str:
    """IPv6 группируется по префиксу: у абонента обычно целая сеть /56 или /48."""
    address = _parse_ip(client_ip) if client_ip else None
    if isinstance(address, ipaddress.IPv6Address):
        return str(ipaddress.ip_network(f"{address}/{ipv6_prefix}", strict=False))
    return str(address) if address is not None else UNKNOWN_CLIENT


@dataclass(frozen=True)
class Admission:
    """Итог проверки: `retry_after` — секунды до повтора, None — принято."""

    retry_after: int | None
    stamp: float = 0.0
    first_denial: bool = False
    table_full: bool = False

    @property
    def allowed(self) -> bool:
        return self.retry_after is None


class _Entry:
    __slots__ = ("logged_for", "stamps")

    def __init__(self) -> None:
        self.stamps: deque[float] = deque()
        self.logged_for: float | None = None


class RateLimiter:
    """Скользящий журнал в памяти процесса: не более `requests` попыток за окно.

    Корректность держится на одном процессе и отсутствии await между проверкой и
    записью, поэтому `check` синхронный, а вызывающая зависимость обязана оставаться
    `async def` (в threadpool методы начали бы гоняться). Для нескольких реплик лимит
    умножается (нужен общий Redis); при перезапуске счётчики сбрасываются.

    Переполнение таблицы ключей закрыто, а не открыто: новый ключ отклоняется, пока не
    истечёт самый давний, вместо вытеснения чужих записей, которое снимало бы лимит.
    """

    def __init__(
        self,
        *,
        requests: int,
        window_seconds: float,
        max_keys: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._requests = requests
        self._window = window_seconds
        self._max_keys = max_keys
        self._clock = clock
        # Порядок = давность последней принятой попытки: старые ключи в начале.
        self._log: OrderedDict[str, _Entry] = OrderedDict()
        # Пока таблица заведомо полна, полный проход по ней не повторяется на каждый
        # отказ: иначе перебор новых адресов был бы дешёвой нагрузкой на CPU.
        self._full_until = 0.0
        self._secret = os.urandom(16)

    @property
    def tracked_keys(self) -> int:
        return len(self._log)

    def check(self, key: str) -> Admission:
        now = self._clock()
        self._sweep(now)
        entry = self._log.get(key)
        if entry is None:
            if len(self._log) >= self._max_keys:
                if now < self._full_until:
                    return Admission(math.ceil(self._full_until - now), table_full=True)
                # После refund порядок таблицы приблизителен: перед отказом проверяем
                # все записи (редкий случай), а не только начало очереди.
                self._sweep_all(now)
            if len(self._log) >= self._max_keys:
                freed_at = min(e.stamps[-1] for e in self._log.values())
                self._full_until = freed_at + self._window
                return Admission(math.ceil(self._full_until - now), table_full=True)
            entry = self._log[key] = _Entry()
        else:
            while entry.stamps and entry.stamps[0] <= now - self._window:
                entry.stamps.popleft()
        if len(entry.stamps) >= self._requests:
            oldest = entry.stamps[0]
            first = entry.logged_for != oldest
            entry.logged_for = oldest
            wait = math.ceil(oldest + self._window - now)
            return Admission(wait, first_denial=first)
        entry.stamps.append(now)
        self._full_until = 0.0  # у ключа появилась новая последняя попытка
        self._log.move_to_end(key)
        return Admission(None, stamp=now)

    def refund(self, key: str, stamp: float) -> None:
        """Возвращает слот попытке, которая заведомо не дошла до сервиса."""
        entry = self._log.get(key)
        if entry is None:
            return
        try:
            entry.stamps.remove(stamp)
        except ValueError:
            return
        self._full_until = 0.0  # записи могли истекать раньше, чем считалось
        if not entry.stamps:
            del self._log[key]

    def _sweep(self, now: float) -> None:
        while self._log:
            oldest_key, entry = next(iter(self._log.items()))
            if entry.stamps and entry.stamps[-1] > now - self._window:
                break
            del self._log[oldest_key]

    def _sweep_all(self, now: float) -> None:
        for key in [
            k for k, e in self._log.items() if e.stamps[-1] <= now - self._window
        ]:
            del self._log[key]

    def fingerprint(self, key: str) -> str:
        """Стабильный в рамках процесса короткий хэш ключа для логов (без адреса)."""
        return hmac.new(self._secret, key.encode(), hashlib.sha256).hexdigest()[:12]
