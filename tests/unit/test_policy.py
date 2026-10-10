from gateway.policy import (
    client_response_headers,
    strip_hop_by_hop,
    upstream_request_headers,
)


def names(headers: list[tuple[bytes, bytes]]) -> list[bytes]:
    return [name.lower() for name, _ in headers]


def test_hop_by_hop_headers_are_removed_in_both_directions() -> None:
    headers = [
        (b"Connection", b"keep-alive, X-Private"),
        (b"Keep-Alive", b"timeout=5"),
        (b"Transfer-Encoding", b"chunked"),
        (b"TE", b"trailers"),
        (b"Upgrade", b"websocket"),
        (b"Proxy-Authorization", b"secret"),
        (b"X-Private", b"1"),
        (b"Content-Type", b"application/json"),
    ]
    assert strip_hop_by_hop(headers) == [(b"Content-Type", b"application/json")]


def test_repeated_headers_are_kept() -> None:
    headers = [(b"Set-Cookie", b"a=1"), (b"Set-Cookie", b"b=2")]
    assert strip_hop_by_hop(headers) == headers


def test_client_trust_headers_are_replaced_by_gateway_values() -> None:
    incoming = [
        (b"Host", b"evil"),
        (b"Content-Length", b"999"),
        (b"Content-Type", b"application/json"),
        (b"X-Forwarded-For", b"6.6.6.6"),
        (b"X-Forwarded-Host", b"evil"),
        (b"X-Forwarded-Proto", b"gopher"),
        (b"Forwarded", b"for=6.6.6.6"),
        (b"X-Real-IP", b"6.6.6.6"),
        (b"X-Request-ID", b"client"),
        (b"X-User-Id", b"admin"),
        (b"X-Auth-Role", b"root"),
        (b"Cookie", b"s=1"),
        (b"Traceparent", b"00-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-bbbbbbbbbbbbbbbb-01"),
        (b"Tracestate", b"x=y"),
        (b"Baggage", b"k=v"),
        (b"Accept", b"application/json"),
    ]
    result = upstream_request_headers(
        incoming, request_id="rid-1", client_ip="203.0.113.7", scheme="https"
    )
    assert dict(result) == {
        b"Content-Type": b"application/json",
        b"Accept": b"application/json",
        b"x-request-id": b"rid-1",
        b"x-forwarded-proto": b"https",
        b"x-forwarded-for": b"203.0.113.7",
    }


def test_forwarded_for_is_omitted_without_client_address() -> None:
    result = upstream_request_headers(
        [], request_id="rid", client_ip=None, scheme="http"
    )
    assert b"x-forwarded-for" not in names(result)


def test_response_headers_are_cleaned_and_length_recomputed() -> None:
    upstream = [
        (b"content-type", b"application/json"),
        (b"content-encoding", b"gzip"),
        (b"content-length", b"1"),
        (b"transfer-encoding", b"chunked"),
        (b"connection", b"close"),
        (b"server", b"uvicorn"),
        (b"date", b"x"),
        (b"x-request-id", b"upstream"),
        (b"x-extra", b"1"),
    ]
    result = client_response_headers(upstream, body_length=42)
    assert dict(result) == {
        b"content-type": b"application/json",
        b"content-encoding": b"gzip",
        b"x-extra": b"1",
        b"content-length": b"42",
    }
