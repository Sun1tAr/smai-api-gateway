import hashlib
import hmac
import ipaddress
import math
import os
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Iterable

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
Network = ipaddress.IPv4Network | ipaddress.IPv6Network

# Ограничиваются только операции, которые запускают дорогую LLM-генерацию.
LIMITED_ENDPOINTS = frozenset({("POST", "/recipes/generate-menu")})
UNKNOWN_CLIENT = "unknown"


def parse_networks(value: str) -> tuple[Network, ...]:
    """CIDR или одиночные адреса через запятую; пусто = доверенных прокси нет."""
    return tuple(
        ipaddress.ip_network(part.strip(), strict=False)
        for part in value.split(",")
        if part.strip()
    )


def _parse_ip(raw: str) -> IPAddress | None:
    try:
        address = ipaddress.ip_address(raw.strip())
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        return address.ipv4_mapped
    return address


def resolve_client_ip(
    peer: str | None, forwarded_for: Iterable[str], trusted: tuple[Network, ...]
) -> str:
    """Клиент — первый недоверенный адрес справа в цепочке XFF + адрес соседа.

    Заголовок читается, только если сокетный сосед доверенный: иначе клиент мог бы
    подставить любой XFF и обойти лимит или спрятаться за чужим адресом.
    """
    current = _parse_ip(peer) if peer else None
    if current is None:
        return UNKNOWN_CLIENT
    if not trusted or not _is_trusted(current, trusted):
        return str(current)
    hops = [token for value in forwarded_for for token in value.split(",")]
    for token in reversed(hops):
        candidate = _parse_ip(token)
        # Мусор в цепочке: дальше ей верить нельзя, остаёмся на последнем адресе.
        if candidate is None:
            return str(current)
        current = candidate
        if not _is_trusted(current, trusted):
            break
    return str(current)


def _is_trusted(address: IPAddress, trusted: tuple[Network, ...]) -> bool:
    return any(address.version == net.version and address in net for net in trusted)


def rate_limit_key(client_ip: str) -> str:
    """IPv6 группируется по /64: один хост получает целую подсеть адресов."""
    address = _parse_ip(client_ip) if client_ip != UNKNOWN_CLIENT else None
    if isinstance(address, ipaddress.IPv6Address):
        return str(ipaddress.ip_network(f"{address}/64", strict=False))
    return str(address) if address is not None else UNKNOWN_CLIENT


class RateLimiter:
    """Скользящий журнал в памяти процесса: не более `requests` попыток за окно.

    Корректность держится на одном процессе и отсутствии await между проверкой и
    записью. Для нескольких реплик лимит умножается (нужен общий Redis).
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
        # Порядок = давность последней попытки: старые ключи всегда в начале.
        self._log: OrderedDict[str, deque[float]] = OrderedDict()
        self._secret = os.urandom(16)

    @property
    def tracked_keys(self) -> int:
        return len(self._log)

    def check(self, key: str) -> int | None:
        """None — попытка принята; число — через сколько секунд повторить."""
        now = self._clock()
        self._sweep(now)
        stamps = self._log.get(key)
        if stamps is None:
            if len(self._log) >= self._max_keys:
                self._log.popitem(last=False)
            stamps = self._log[key] = deque()
        else:
            while stamps and stamps[0] <= now - self._window:
                stamps.popleft()
        if len(stamps) >= self._requests:
            return max(1, math.ceil(stamps[0] + self._window - now))
        stamps.append(now)
        self._log.move_to_end(key)
        return None

    def _sweep(self, now: float) -> None:
        while self._log:
            oldest_key, stamps = next(iter(self._log.items()))
            if stamps and stamps[-1] > now - self._window:
                break
            del self._log[oldest_key]

    def fingerprint(self, key: str) -> str:
        """Стабильный в рамках процесса короткий хэш ключа для логов (без адреса)."""
        return hmac.new(self._secret, key.encode(), hashlib.sha256).hexdigest()[:12]
