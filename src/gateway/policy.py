from collections.abc import Iterable

Headers = list[tuple[bytes, bytes]]

# RFC 9110 §7.6.1: относятся к одному соединению и не пересылаются.
HOP_BY_HOP = frozenset(
    {
        b"connection",
        b"keep-alive",
        b"proxy-authenticate",
        b"proxy-authorization",
        b"te",
        b"trailer",
        b"transfer-encoding",
        b"upgrade",
    }
)
# Заголовки доверия и идентичности выставляет только gateway, клиентские игнорируются.
EDGE_STRIP = frozenset(
    {
        b"host",
        b"content-length",
        b"forwarded",
        b"x-real-ip",
        b"x-request-id",
        b"cookie",
        b"traceparent",
        b"tracestate",
        b"baggage",
    }
)
EDGE_STRIP_PREFIXES = (b"x-forwarded-", b"x-user-", b"x-auth-")
# gateway ставит их сам; дубли от upstream дали бы два Date/Server на ответе.
RESPONSE_STRIP = frozenset({b"server", b"date", b"x-request-id", b"content-length"})


def _connection_tokens(headers: Iterable[tuple[bytes, bytes]]) -> set[bytes]:
    tokens: set[bytes] = set()
    for name, value in headers:
        if name.lower() == b"connection":
            tokens.update(
                part.strip().lower() for part in value.split(b",") if part.strip()
            )
    return tokens


# Одна функция для обоих направлений: список плюс токены из значения Connection.
def strip_hop_by_hop(headers: Iterable[tuple[bytes, bytes]]) -> Headers:
    pairs = list(headers)
    named = _connection_tokens(pairs)
    return [
        (name, value)
        for name, value in pairs
        if (lowered := name.lower()) not in HOP_BY_HOP and lowered not in named
    ]


def upstream_request_headers(
    headers: Iterable[tuple[bytes, bytes]],
    *,
    request_id: str,
    client_ip: str | None,
    scheme: str,
) -> Headers:
    kept = [
        (name, value)
        for name, value in strip_hop_by_hop(headers)
        if (lowered := name.lower()) not in EDGE_STRIP
        and not lowered.startswith(EDGE_STRIP_PREFIXES)
    ]
    kept.append((b"x-request-id", request_id.encode("ascii")))
    kept.append((b"x-forwarded-proto", scheme.encode("ascii")))
    if client_ip is not None:
        kept.append((b"x-forwarded-for", client_ip.encode("ascii")))
    return kept


def client_response_headers(
    headers: Iterable[tuple[bytes, bytes]], *, body_length: int
) -> Headers:
    kept = [
        (name, value)
        for name, value in strip_hop_by_hop(headers)
        if name.lower() not in RESPONSE_STRIP
    ]
    kept.append((b"content-length", str(body_length).encode("ascii")))
    return kept
