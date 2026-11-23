from pathlib import Path

import pytest

from gateway.routing import (
    Endpoint,
    InvalidPath,
    RouteConfigError,
    load_routes,
    upstream_target,
)

POST_GENERATE = Endpoint("POST", "recipes", "recipes", "/generate-menu")
GET_RESULT = Endpoint("GET", "recipes", "recipes", "/generate-menu/{task_id}")
TASK = "b1e2c3d4-5678-90ab-cdef-1234567890ab"


def test_prefix_is_stripped() -> None:
    assert (
        upstream_target(POST_GENERATE, b"/recipes/generate-menu") == b"/generate-menu"
    )
    assert (
        upstream_target(GET_RESULT, f"/recipes/generate-menu/{TASK}".encode())
        == f"/generate-menu/{TASK}".encode()
    )


def test_query_is_kept_byte_for_byte() -> None:
    query = b"a=1&b=%D0%B0&a=%2F+x"
    assert upstream_target(POST_GENERATE, b"/recipes/generate-menu", query) == (
        b"/generate-menu?" + query
    )


@pytest.mark.parametrize(
    "task_id",
    [
        "..",
        ".",
        "%2e%2e",
        "%2E%2e",
        ".%2e",
        "%252e%252e",
        "%2f",
        "%2F",
        "%5c",
        "a%5Cb",
        "%00",
        "a%0Ab",
        "%",
        "%zz",
        "",
        "a b",
        "а",
        "%ff",
    ],
)
def test_unsafe_task_id_is_rejected(task_id: str) -> None:
    raw = f"/recipes/generate-menu/{task_id}".encode()
    with pytest.raises(InvalidPath):
        upstream_target(GET_RESULT, raw)


@pytest.mark.parametrize(
    "raw",
    [
        b"/recipes//generate-menu",
        b"//recipes/generate-menu",
        b"/recipes/generate-menu/",
        b"/recipes/generate%2Dmenu",
        b"/recipes/../generate-menu",
        b"/recipesfoo/generate-menu",
        b"/recipes",
        b"/recipes/",
        b"recipes/generate-menu",
        b"/recipes\\generate-menu",
        "/recipes/generate-menuа".encode(),
    ],
)
def test_paths_outside_the_template_are_rejected(raw: bytes) -> None:
    with pytest.raises(InvalidPath):
        upstream_target(POST_GENERATE, raw)


def test_dot_segment_in_prefix_position_is_rejected() -> None:
    with pytest.raises(InvalidPath):
        upstream_target(GET_RESULT, b"/../generate-menu/x")


@pytest.mark.parametrize("query", [b"a=1 b", b"a=\n", "a=а".encode(), b"a=\x00"])
def test_unsafe_query_is_rejected(query: bytes) -> None:
    with pytest.raises(InvalidPath):
        upstream_target(POST_GENERATE, b"/recipes/generate-menu", query)


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


GOOD = """
[[route]]
prefix = "recipes"
upstream = "recipes"
allow = ["POST /generate-menu", "GET /generate-menu/{task_id}"]
"""


def test_default_routes_file_is_valid() -> None:
    routes = load_routes(Path("config/routes.toml"), {"recipes", "dictionary"})
    assert [e.gateway_path for e in routes[0].endpoints] == [
        "/recipes/generate-menu",
        "/recipes/generate-menu/{task_id}",
        "/recipes/generate-menu/{task_id}/replace-recipe",
        "/recipes/profile/parameters",
        "/recipes/profile/menus",
        "/recipes/cart",
        "/recipes/cart/link",
        "/recipes/{recipe_id}/like",
        "/recipes/{recipe_id}/like",
        "/recipes/liked",
    ]
    assert [e.method for e in routes[0].endpoints] == [
        "POST",
        "GET",
        "POST",
        "GET",
        "GET",
        "POST",
        "POST",
        "POST",
        "DELETE",
        "GET",
    ]
    assert [e.gateway_path for e in routes[1].endpoints] == ["/dictionaries"]
    history, cart, link, like, unlike, liked = routes[0].endpoints[-6:]
    assert history.upstream_path == "/profile/menus"
    assert like.upstream_path == unlike.upstream_path == "/recipes/{recipe_id}/like"
    assert liked.upstream_path == "/recipes/liked"
    assert upstream_target(cart, b"/recipes/cart", b"a=%2F") == b"/recipes/cart?a=%2F"
    assert upstream_target(link, b"/recipes/cart/link", b"a=%2F") == (
        b"/recipes/cart/link?a=%2F"
    )


def test_mapped_path_uses_only_validated_parameters(tmp_path: Path) -> None:
    routes = load_routes(
        write(
            tmp_path / "routes.toml",
            GOOD.replace(
                '"POST /generate-menu"',
                '{ endpoint = "POST /a/{x}/{y}", upstream_path = "/v1/{y}/{x}" }',
            ),
        ),
        {"recipes"},
    )
    endpoint = routes[0].endpoints[0]
    assert upstream_target(endpoint, b"/recipes/a/%D0%B0/b") == b"/v1/b/%D0%B0"
    for path in (b"/recipes/a/%2f/b", b"/recipes/a/a/%2e%2e", b"/recipes/%61/a/b"):
        with pytest.raises(InvalidPath):
            upstream_target(endpoint, path)


@pytest.mark.parametrize(
    ("endpoint", "target", "prefix", "error"),
    [
        ("POST /generate-menu", "/v1", "recipes", "duplicate endpoint"),
        ("POST /docs", "/v1", "", "reserved or ambiguous"),
        ("POST /a", "/../v1", "recipes", "invalid path segment"),
        ("POST /a", "//other/v1", "recipes", "invalid path segment"),
        ("POST /a", "/v1/%2f", "recipes", "invalid path segment"),
        ("POST /a/{id}", "/v1/{other}", "recipes", "parameters"),
        ("POST /a/{id}", "/v1/{id}/{id}", "recipes", "parameters"),
    ],
)
def test_mapped_paths_keep_the_allowlist_checks(
    tmp_path: Path, endpoint: str, target: str, prefix: str, error: str
) -> None:
    config = write(
        tmp_path / "routes.toml",
        f'[[route]]\nprefix = "{prefix}"\nupstream = "recipes"\n'
        'allow = ["POST /generate-menu", '
        f'{{ endpoint = "{endpoint}", upstream_path = "{target}" }}]\n',
    )
    with pytest.raises(RouteConfigError, match=error):
        load_routes(config, {"recipes"})


GET_DICTIONARIES = Endpoint("GET", "", "dictionary", "/dictionaries")


def test_route_without_prefix_keeps_the_service_path() -> None:
    assert GET_DICTIONARIES.gateway_path == "/dictionaries"
    assert upstream_target(GET_DICTIONARIES, b"/dictionaries") == b"/dictionaries"
    assert (
        upstream_target(GET_DICTIONARIES, b"/dictionaries", b"a=1")
        == b"/dictionaries?a=1"
    )


@pytest.mark.parametrize(
    "raw", [b"/dictionaries/dictionaries", b"/dictionaries/", b"/other", b"/"]
)
def test_route_without_prefix_rejects_other_paths(raw: bytes) -> None:
    with pytest.raises(InvalidPath):
        upstream_target(GET_DICTIONARIES, raw)


@pytest.mark.parametrize(
    "path",
    ["/health", "/metrics", "/docs", "/redoc", "/openapi.json", "/{id}", "/recipes/x"],
)
def test_unprefixed_route_cannot_take_reserved_or_foreign_paths(
    tmp_path: Path, path: str
) -> None:
    file = tmp_path / "routes.toml"
    file.write_text(
        '[[route]]\nprefix = "recipes"\nupstream = "recipes"\n'
        'allow = ["GET /a"]\n'
        f'[[route]]\nprefix = ""\nupstream = "recipes"\nallow = ["GET {path}"]\n',
        encoding="utf-8",
    )
    with pytest.raises(
        RouteConfigError, match="reserved or ambiguous|overlaps|invalid path segment"
    ):
        load_routes(file, {"recipes"})


def test_unprefixed_route_declared_before_a_prefixed_one_cannot_overlap(
    tmp_path: Path,
) -> None:
    file = tmp_path / "routes.toml"
    file.write_text(
        '[[route]]\nprefix = ""\nupstream = "recipes"\nallow = ["GET /recipes/x"]\n'
        '[[route]]\nprefix = "recipes"\nupstream = "recipes"\nallow = ["GET /a"]\n',
        encoding="utf-8",
    )
    with pytest.raises(RouteConfigError):
        load_routes(file, {"recipes"})


def test_only_one_unprefixed_route_is_allowed(tmp_path: Path) -> None:
    file = tmp_path / "routes.toml"
    file.write_text(
        '[[route]]\nprefix = ""\nupstream = "recipes"\nallow = ["GET /a"]\n'
        '[[route]]\nprefix = ""\nupstream = "recipes"\nallow = ["GET /b"]\n',
        encoding="utf-8",
    )
    with pytest.raises(RouteConfigError, match="duplicate route prefix"):
        load_routes(file, {"recipes"})


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not toml [",
        GOOD.replace('"recipes"', '"docs"', 1),
        GOOD.replace('"recipes"', '"openapi.json"', 1),
        GOOD.replace('prefix = "recipes"', 'prefix = "Recipes"'),
        GOOD.replace('upstream = "recipes"', 'upstream = "missing"'),
        GOOD + GOOD,
        GOOD.replace('"GET /generate-menu/{task_id}"', '"POST /generate-menu"'),
        GOOD.replace('"POST /generate-menu"', '"FOO /generate-menu"'),
        GOOD.replace('"POST /generate-menu"', '"POST generate-menu"'),
        GOOD.replace('"POST /generate-menu"', '"POST /generate-menu/"'),
        GOOD.replace('"POST /generate-menu"', '"POST /../x"'),
        GOOD.replace('"POST /generate-menu"', '"POST /a b"'),
        GOOD.replace(
            'allow = ["POST /generate-menu", "GET /generate-menu/{task_id}"]',
            "allow = []",
        ),
    ],
)
def test_invalid_routes_file_is_fatal(tmp_path: Path, text: str) -> None:
    with pytest.raises(RouteConfigError):
        load_routes(write(tmp_path / "routes.toml", text), {"recipes"})


def test_missing_routes_file_is_fatal(tmp_path: Path) -> None:
    with pytest.raises(RouteConfigError):
        load_routes(tmp_path / "absent.toml", {"recipes"})
