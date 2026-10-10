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
    routes = load_routes(Path("config/routes.toml"), {"recipes"})
    assert [e.gateway_path for e in routes[0].endpoints] == [
        "/recipes/generate-menu",
        "/recipes/generate-menu/{task_id}",
    ]
    assert [e.method for e in routes[0].endpoints] == ["POST", "GET"]


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
