import json
from pathlib import Path

import httpx
import pytest

from gateway.config import Settings
from gateway.main import create_app

CONTRACT = json.loads(Path("contracts/receipes.openapi.json").read_text("utf-8"))
OPERATIONS = [
    ("/recipes/generate-menu", "post", "/generate-menu"),
    ("/recipes/generate-menu/{task_id}", "get", "/generate-menu/{task_id}"),
]
GATEWAY_CODES = {
    ("post", "/recipes/generate-menu"): {
        "202",
        "400",
        "413",
        "500",
        "502",
        "503",
        "504",
    },
    ("get", "/recipes/generate-menu/{task_id}"): {
        "200",
        "400",
        "404",
        "500",
        "502",
        "503",
        "504",
    },
}


@pytest.fixture
def schema(settings: Settings) -> dict:
    return create_app(settings).openapi()


def test_documented_paths_equal_the_allowlist(schema: dict) -> None:
    documented = {
        (path, method)
        for path, item in schema["paths"].items()
        for method in item
        if path != "/health"
    }
    assert documented == {(path, method) for path, method, _ in OPERATIONS}


@pytest.mark.parametrize(("path", "method", "upstream_path"), OPERATIONS)
def test_operation_matches_the_contract_snapshot(
    schema: dict, path: str, method: str, upstream_path: str
) -> None:
    operation = schema["paths"][path][method]
    expected = CONTRACT["paths"][upstream_path][method]
    assert operation["summary"] == expected["summary"]
    assert operation["description"] == expected["description"]
    assert operation.get("requestBody") == expected.get("requestBody")
    assert operation.get("parameters") == expected.get("parameters")
    for status, response in expected["responses"].items():
        assert operation["responses"][status]["content"] == response["content"]


@pytest.mark.parametrize(("path", "method", "upstream_path"), OPERATIONS)
def test_every_error_is_documented_with_an_example(
    schema: dict, path: str, method: str, upstream_path: str
) -> None:
    responses = schema["paths"][path][method]["responses"]
    assert set(responses) == GATEWAY_CODES[(method, path)]
    for status, response in responses.items():
        assert response["description"]
        media = response["content"]["application/json"]
        assert "example" in media or "examples" in media, status


def test_success_codes(schema: dict) -> None:
    assert "202" in schema["paths"]["/recipes/generate-menu"]["post"]["responses"]
    assert (
        "200" in schema["paths"]["/recipes/generate-menu/{task_id}"]["get"]["responses"]
    )
    assert "ErrorResponse" in schema["components"]["schemas"]


async def test_docs_pages_are_served(settings: Settings) -> None:
    app = create_app(settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        assert (await client.get("/docs")).status_code == 200
        assert (await client.get("/openapi.json")).status_code == 200


def test_missing_contract_for_an_allowed_path_is_fatal(
    settings: Settings, tmp_path: Path
) -> None:
    broken = tmp_path / "contract.json"
    broken.write_text('{"paths": {}}', encoding="utf-8")
    from gateway.api import ContractError

    with pytest.raises(ContractError):
        create_app(settings.model_copy(update={"contract_file": broken}))
