import json
from pathlib import Path

import httpx
import jsonschema
import pytest

from gateway.config import Settings
from gateway.main import create_app

CONTRACT = json.loads(Path("contracts/receipes.openapi.json").read_text("utf-8"))
DICTIONARY_CONTRACT = json.loads(
    Path("contracts/dictionary.openapi.json").read_text("utf-8")
)
OPERATIONS = [
    ("/recipes/generate-menu", "post", "/generate-menu"),
    ("/recipes/generate-menu/{task_id}", "get", "/generate-menu/{task_id}"),
    ("/recipes/profile/parameters", "get", "/profile/parameters"),
]
GATEWAY_CODES = {
    ("post", "/recipes/generate-menu"): {
        "202",
        "400",
        "401",
        "413",
        "429",
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
    # Токен обязателен: 503 бывает и из-за Redis. 404 нет: пустой ответ это 200.
    ("get", "/recipes/profile/parameters"): {
        "200",
        "400",
        "401",
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
        if path not in ("/health", "/metrics")
    }
    assert documented == {(path, method) for path, method, _ in OPERATIONS} | {
        ("/dictionaries", "get"),
        ("/auth/register", "post"),
        ("/auth/login", "post"),
        ("/auth/logout", "post"),
        ("/user/profile", "get"),
    }


def test_dictionaries_are_documented_under_their_own_path(schema: dict) -> None:
    operation = schema["paths"]["/dictionaries"]["get"]
    expected = DICTIONARY_CONTRACT["paths"]["/dictionaries"]["get"]
    assert operation["summary"] == expected["summary"]
    assert operation["tags"] == ["dictionary"]
    assert set(operation["responses"]) == {"200", "400", "500", "502", "503", "504"}
    ok = operation["responses"]["200"]["content"]["application/json"]
    assert ok == expected["responses"]["200"]["content"]["application/json"]
    assert set(ok["example"]) == {"habits", "diets", "allergies", "kitchenTools"}
    for status, response in operation["responses"].items():
        media = response["content"]["application/json"]
        assert "example" in media or "examples" in media, status
    example = operation["responses"]["502"]["content"]["application/json"]["example"]
    assert example["error"]["details"]["upstream"] == "dictionary"


def test_same_path_in_two_contracts_is_fatal(settings: Settings) -> None:
    from gateway.api import ContractError

    clash = settings.model_copy(
        update={"contract_dictionary_file": settings.contract_file}
    )
    with pytest.raises(ContractError):
        create_app(clash)


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
        actual = operation["responses"][status]["content"]["application/json"]
        wanted = response["content"]["application/json"]
        if status == "400":
            # Пример MS-4 остаётся, рядом появляется пример самого gateway.
            assert set(actual["examples"]) == {"ms4_validation", "gateway_path"}
            assert actual["schema"] == wanted["schema"]
        else:
            assert actual == wanted


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


@pytest.mark.parametrize(("path", "method", "upstream_path"), OPERATIONS)
async def test_gateway_error_examples_carry_the_right_reason(
    schema: dict, path: str, method: str, upstream_path: str
) -> None:
    responses = schema["paths"][path][method]["responses"]
    for status, reason in (("502", "connect"), ("503", "pool"), ("504", "timeout")):
        media = responses[status]["content"]["application/json"]
        # У защищённых маршрутов 503 несёт несколько примеров (Redis, пул).
        example = media.get("example") or media["examples"]["pool"]["value"]
        assert example["error"]["details"] == {"upstream": "recipes", "reason": reason}
    gateway_400 = responses["400"]["content"]["application/json"]
    example = (
        gateway_400.get("example") or gateway_400["examples"]["gateway_path"]["value"]
    )
    assert example["error"]["message"] == "Request path is invalid"


@pytest.mark.parametrize(
    "broken",
    [
        lambda op: op.pop("summary"),
        lambda op: op.pop("description"),
        lambda op: op["responses"]["400"].pop("content"),
        lambda op: op["responses"]["400"]["content"]["application/json"].pop("example"),
        lambda op: op["responses"].pop("202", None) or op["responses"].pop("200", None),
    ],
)
def test_incomplete_contract_stops_the_start(
    settings: Settings, tmp_path: Path, broken
) -> None:
    from gateway.api import ContractError

    contract = json.loads(json.dumps(CONTRACT))
    broken(contract["paths"]["/generate-menu"]["post"])
    file = tmp_path / "contract.json"
    file.write_text(json.dumps(contract), "utf-8")
    with pytest.raises(ContractError):
        create_app(settings.model_copy(update={"contract_file": file}))


def test_service_endpoints_are_documented_with_examples(schema: dict) -> None:
    metrics = schema["paths"]["/metrics"]["get"]["responses"]
    assert "text/plain" in metrics["200"]["content"]
    assert "gateway_upstream_up" in metrics["200"]["content"]["text/plain"]["example"]
    for path in ("/health", "/metrics"):
        responses = schema["paths"][path]["get"]["responses"]
        assert set(responses) >= {"200", "500"}
        assert "example" in responses["500"]["content"]["application/json"]


def _media_examples(where: str, media: dict):
    if "example" in media:
        yield (f"{where} example", media["schema"], media["example"])
    for name, example in media.get("examples", {}).items():
        yield (f"{where} {name}", media["schema"], example["value"])


def _schema_examples(contract: dict):
    for path, item in contract["paths"].items():
        for method, operation in item.items():
            where = f"{method.upper()} {path}"
            for status, response in operation.get("responses", {}).items():
                for media in response.get("content", {}).values():
                    yield from _media_examples(f"{where} {status}", media)
            for media in operation.get("requestBody", {}).get("content", {}).values():
                yield from _media_examples(f"{where} request", media)


@pytest.mark.parametrize(
    ("where", "schema_", "value"),
    list(_schema_examples(CONTRACT)),
    ids=[where for where, _, _ in _schema_examples(CONTRACT)],
)
def test_contract_examples_are_valid_against_their_schema(
    where: str, schema_: dict, value: object
) -> None:
    jsonschema.Draft202012Validator(
        {**schema_, "components": CONTRACT.get("components", {})}
    ).validate(value)


def test_contract_without_paths_is_a_contract_error(tmp_path: Path) -> None:
    from gateway.api import ContractError, load_contracts

    for text in ("{}", '{"paths": null}', "[]"):
        file = tmp_path / "c.json"
        file.write_text(text, "utf-8")
        with pytest.raises(ContractError):
            load_contracts(file)


def test_contract_with_ref_is_rejected(tmp_path: Path) -> None:
    from gateway.api import ContractError, load_contracts

    contract = json.loads(json.dumps(DICTIONARY_CONTRACT))
    op = contract["paths"]["/dictionaries"]["get"]
    op["responses"]["500"]["content"]["application/json"]["schema"] = {
        "$ref": "#/components/schemas/ErrorResponse"
    }
    file = tmp_path / "c.json"
    file.write_text(json.dumps(contract), "utf-8")
    with pytest.raises(ContractError, match=r"\$ref"):
        load_contracts(file)
