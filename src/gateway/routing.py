import re
import tomllib
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote_to_bytes

RESERVED_PREFIXES = frozenset(
    {"health", "metrics", "docs", "redoc", "openapi.json", "auth", "user"}
)
METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})

_PREFIX = re.compile(r"[a-z][a-z0-9-]*")
_LITERAL = re.compile(r"[A-Za-z0-9._~-]+")
_PARAM = re.compile(r"\{([a-z][a-z0-9_]*)\}")
# Один сегмент пути: незарезервированные символы или корректный %XX.
_RAW_SEGMENT = re.compile(r"(?:[A-Za-z0-9._~!$&'()*+,;=:@-]|%[0-9A-Fa-f]{2})+")


class RouteConfigError(ValueError):
    """Файл маршрутов некорректен; сервис не должен стартовать."""


class InvalidPath(ValueError):
    """Путь запроса небезопасно или неоднозначно передавать в upstream."""


@dataclass(frozen=True)
class Endpoint:
    method: str
    prefix: str
    upstream: str
    upstream_path: str

    @property
    def gateway_path(self) -> str:
        # Пустой prefix: сервис публикуется под своим же путём, без добавки.
        return (
            f"/{self.prefix}{self.upstream_path}" if self.prefix else self.upstream_path
        )


@dataclass(frozen=True)
class Route:
    prefix: str
    upstream: str
    endpoints: tuple[Endpoint, ...]


def _parse_endpoint(prefix: str, upstream: str, entry: object) -> Endpoint:
    if not isinstance(entry, str):
        raise RouteConfigError(f"allow entries must be strings: {entry!r}")
    method, _, path = entry.partition(" ")
    if method not in METHODS or not path.startswith("/") or path.endswith("/"):
        raise RouteConfigError(f"invalid allow entry: {entry!r}")
    for segment in path[1:].split("/"):
        if segment in (".", "..") or not (
            _LITERAL.fullmatch(segment) or _PARAM.fullmatch(segment)
        ):
            raise RouteConfigError(f"invalid path segment in {entry!r}")
    return Endpoint(method=method, prefix=prefix, upstream=upstream, upstream_path=path)


# Белый список читается один раз при старте: любая ошибка конфигурации фатальна.
def load_routes(path: Path, configured_upstreams: Collection[str]) -> tuple[Route, ...]:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise RouteConfigError(
            f"cannot read routes file: {type(error).__name__}"
        ) from None
    entries = data.get("route")
    if not isinstance(entries, list) or not entries:
        raise RouteConfigError("routes file must define at least one [[route]]")
    routes: list[Route] = []
    prefixes: set[str] = set()
    seen: set[tuple[str, str]] = set()
    for entry in entries:
        prefix, upstream, allow = (
            entry.get("prefix"),
            entry.get("upstream"),
            entry.get("allow"),
        )
        if not isinstance(prefix, str) or not (
            prefix == "" or _PREFIX.fullmatch(prefix)
        ):
            raise RouteConfigError(f"invalid route prefix: {prefix!r}")
        if prefix in RESERVED_PREFIXES:
            raise RouteConfigError(f"route prefix is reserved: {prefix!r}")
        if prefix in prefixes:
            raise RouteConfigError(f"duplicate route prefix: {prefix!r}")
        if upstream not in configured_upstreams:
            raise RouteConfigError(f"no address configured for upstream {upstream!r}")
        if not isinstance(allow, list) or not allow:
            raise RouteConfigError(f"route {prefix!r} must allow at least one path")
        endpoints = tuple(_parse_endpoint(prefix, upstream, item) for item in allow)
        for endpoint in endpoints:
            first = endpoint.gateway_path[1:].split("/")[0]
            if not prefix and (
                not _LITERAL.fullmatch(first) or first in RESERVED_PREFIXES
            ):
                raise RouteConfigError(f"path is reserved or ambiguous: {first!r}")
            key = (endpoint.method, endpoint.gateway_path)
            if key in seen:
                raise RouteConfigError(f"duplicate endpoint: {key}")
            seen.add(key)
        prefixes.add(prefix)
        routes.append(Route(prefix=prefix, upstream=upstream, endpoints=endpoints))
    # Путь без префикса не должен заходить в пространство имён другого маршрута.
    for route in routes:
        for endpoint in route.endpoints:
            first = endpoint.gateway_path[1:].split("/")[0]
            if not route.prefix and first in prefixes:
                raise RouteConfigError(f"path overlaps route prefix: {first!r}")
    return tuple(routes)


def _check_param_segment(segment: str) -> None:
    if not _RAW_SEGMENT.fullmatch(segment):
        raise InvalidPath("invalid characters in path")
    decoded = unquote_to_bytes(segment)
    if decoded in (b".", b".."):
        raise InvalidPath("dot segments are not allowed")
    if any(
        byte in (0x2F, 0x5C, 0x25) or byte < 0x20 or byte == 0x7F for byte in decoded
    ):
        raise InvalidPath("encoded separators are not allowed")
    try:
        decoded.decode("utf-8")
    except UnicodeDecodeError:
        raise InvalidPath("path is not valid UTF-8") from None


# Работаем с сырым путём: декодированный путь скрывает %2F и двойное кодирование.
def upstream_target(endpoint: Endpoint, raw_path: bytes, query: bytes = b"") -> bytes:
    try:
        text = raw_path.decode("ascii")
    except UnicodeDecodeError:
        raise InvalidPath("path must be ASCII") from None
    if "\\" in text or not text.startswith("/"):
        raise InvalidPath("malformed path")
    actual = text[1:].split("/")
    gateway = endpoint.gateway_path[1:].split("/")
    if len(actual) != len(gateway):
        raise InvalidPath("path does not match the endpoint")
    parts: list[str] = []
    for given, expected in zip(actual, gateway, strict=True):
        if _PARAM.fullmatch(expected):
            _check_param_segment(given)
        elif given != expected:
            raise InvalidPath("path does not match the endpoint")
        parts.append(given)
    # Первый сегмент — префикс gateway; его в сервис не передаём.
    if endpoint.prefix:
        parts = parts[1:]
    if any(byte < 0x21 or byte > 0x7E for byte in query):
        raise InvalidPath("invalid characters in query")
    target = b"/" + "/".join(parts).encode("ascii")
    return target + (b"?" + query if query else b"")
