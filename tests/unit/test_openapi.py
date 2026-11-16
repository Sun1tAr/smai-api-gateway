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
API2 = "/recipes/generate-menu/{task_id}"
API18 = f"{API2}/replace-recipe"
LIKE = "/recipes/{recipe_id}/like"
LIKED = "/recipes/liked"
API22 = "/recipes/profile/menus"
OPERATIONS = [
    (API18, "post", "/generate-menu/{task_id}/replace-recipe"),
    ("/recipes/generate-menu", "post", "/generate-menu"),
    ("/recipes/generate-menu/{task_id}", "get", "/generate-menu/{task_id}"),
    ("/recipes/profile/parameters", "get", "/profile/parameters"),
    (API22, "get", "/profile/menus"),
    ("/recipes/cart", "post", "/recipes/cart"),
    ("/recipes/cart/link", "post", "/recipes/cart/link"),
    (LIKE, "post", LIKE),
    (LIKE, "delete", LIKE),
    (LIKED, "get", LIKED),
]
GATEWAY_CODES = {
    ("post", LIKE): {
        "200",
        "400",
        "401",
        "404",
        "409",
        "413",
        "500",
        "502",
        "503",
        "504",
    },
    ("delete", LIKE): {"200", "400", "401", "404", "500", "502", "503", "504"},
    ("get", LIKED): {"200", "400", "401", "500", "502", "503", "504"},
    ("get", API22): {"200", "400", "401", "500", "502", "503", "504"},
    ("post", API18): {"200", "400", "404", "413", "429", "500", "502", "503", "504"},
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
    ("post", "/recipes/cart"): {
        "200",
        "400",
        "401",
        "404",
        "413",
        "429",
        "500",
        "502",
        "503",
        "504",
    },
    ("post", "/recipes/cart/link"): {
        "200",
        "400",
        "413",
        "429",
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
            # Gateway добавляет пример ошибки пути и сохраняет примеры MS-4.
            upstream_examples = (
                {"ms4_validation": {"value": wanted["example"]}}
                if "example" in wanted
                else wanted["examples"]
            )
            assert set(actual["examples"]) == {*upstream_examples, "gateway_path"}
            for name, example in upstream_examples.items():
                assert actual["examples"][name] == example
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


def test_api18_documents_a_public_replacement_with_the_api2_recipe_shape(
    schema: dict,
) -> None:
    operation = schema["paths"][API18]["post"]
    assert "security" not in operation
    (parameter,) = operation["parameters"]
    assert (parameter["in"], parameter["name"], parameter["required"]) == (
        "path",
        "task_id",
        True,
    )
    assert parameter["schema"] == {"type": "string", "format": "uuid"}
    request = operation["requestBody"]
    assert request["required"]
    body = request["content"]["application/json"]
    assert body["schema"]["required"] == ["recipeId"]
    assert body["schema"]["properties"]["recipeId"]["format"] == "uuid"
    jsonschema.validate(body["example"], body["schema"])
    api2 = schema["paths"][API2]["get"]["responses"]["200"]["content"][
        "application/json"
    ]
    recipe = api2["schema"]["properties"]["menu"]["properties"]["recipes"]["items"]
    ok = operation["responses"]["200"]["content"]["application/json"]
    assert ok["schema"]["required"] == ["recipe"]
    assert ok["schema"]["properties"] == {"recipe": recipe}
    jsonschema.validate(ok["example"], ok["schema"])
    assert "$ref" not in json.dumps(
        CONTRACT["paths"]["/generate-menu/{task_id}/replace-recipe"]
    )
    assert "Retry-After" in operation["responses"]["429"]["headers"]


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
        example = media.get("example") or media["examples"][reason]["value"]
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
        {**schema_, "components": CONTRACT.get("components", {})},
        format_checker=jsonschema.FormatChecker(),
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


def test_api2_recipe_id_is_required_uuid(schema: dict) -> None:
    media = schema["paths"]["/recipes/generate-menu/{task_id}"]["get"]["responses"][
        "200"
    ]["content"]["application/json"]
    menu = media["schema"]["properties"]["menu"]
    recipe = menu["properties"]["recipes"]["items"]
    assert "recipeId" in recipe["required"]
    assert recipe["properties"]["recipeId"]["type"] == "string"
    assert recipe["properties"]["recipeId"]["format"] == "uuid"


def test_api14_documents_optional_auth_and_its_own_limit(schema: dict) -> None:
    operation = schema["paths"]["/recipes/cart"]["post"]
    assert operation["security"] == [{}, {"bearerAuth": []}]
    responses = operation["responses"]
    assert responses["401"]["headers"]["WWW-Authenticate"]["schema"]["type"] == "string"
    limit = responses["429"]
    assert limit["headers"]["Retry-After"]["schema"]["type"] == "integer"
    assert "CART_RATE_LIMIT_REQUESTS" in limit["description"]
    assert "CART_RATE_LIMIT_GLOBAL_REQUESTS" in limit["description"]


def test_api16_documents_a_public_cart_link_and_item_limits(schema: dict) -> None:
    operation = schema["paths"]["/recipes/cart/link"]["post"]
    assert not operation.get("security")
    request = operation["requestBody"]["content"]["application/json"]
    items = request["schema"]["properties"]["items"]
    assert (items["minItems"], items["maxItems"]) == (1, 20)
    assert set(items["items"]["required"]) == {"id", "q"}
    properties = items["items"]["properties"]
    assert (
        properties["id"]["type"],
        properties["id"]["minimum"],
        properties["id"]["maximum"],
    ) == ("integer", 1, 999999999)
    assert (
        properties["q"]["type"],
        properties["q"]["minimum"],
        properties["q"]["maximum"],
    ) == ("number", 0.01, 40)
    jsonschema.validate(request["example"], request["schema"])
    response = operation["responses"]["200"]["content"]["application/json"]
    assert response["schema"]["required"] == ["link"]
    jsonschema.validate(response["example"], response["schema"])
    limited = operation["responses"]["429"]
    assert limited["headers"]["Retry-After"]["schema"]["type"] == "integer"
    assert "CART_RATE_LIMIT_REQUESTS" in limited["description"]
    assert "CART_RATE_LIMIT_GLOBAL_REQUESTS" in limited["description"]


@pytest.mark.parametrize(
    "path, method", [(LIKE, "post"), (LIKE, "delete"), (LIKED, "get"), (API22, "get")]
)
def test_protected_recipe_routes_document_auth_and_failures(schema, path, method):
    operation = schema["paths"][path][method]
    assert operation["security"] == [{"bearerAuth": []}]
    parameters = operation.get("parameters", [])
    assert [(p["name"], p["in"]) for p in parameters] == (
        [("recipe_id", "path")] if path == LIKE else []
    )
    if parameters:
        assert parameters[0]["required"] is True
        assert parameters[0]["schema"]["format"] == "uuid"
    unauthorized = operation["responses"]["401"]
    assert unauthorized["headers"]["WWW-Authenticate"]["schema"]["example"] == "Bearer"
    assert unauthorized["content"]["application/json"]["example"] == {
        "error": {
            "code": "UNAUTHORIZED",
            "message": "Authentication token is missing or invalid",
            "details": {},
        }
    }
    unavailable = operation["responses"]["503"]["content"]["application/json"]
    assert set(unavailable["examples"]) == {"redis", "pool"}
    redis = unavailable["examples"]["redis"]["value"]["error"]
    assert redis["details"] == {"upstream": "redis", "reason": "connect"}


def test_api22_documents_menu_history(schema: dict) -> None:
    operation = schema["paths"][API22]["get"]
    assert "x-user-id" not in json.dumps(operation).lower()
    for status, response in operation["responses"].items():
        media = response["content"]["application/json"]
        for _, example_schema, value in _media_examples(status, media):
            jsonschema.Draft202012Validator(
                {**example_schema, "components": schema["components"]},
                format_checker=jsonschema.FormatChecker(),
            ).validate(value)
    ok = operation["responses"]["200"]["content"]["application/json"]
    menu = ok["schema"]["properties"]["menus"]["items"]
    assert set(menu["properties"]) == {"menuId", "createdAt", "recipes"}
    assert menu["properties"]["menuId"]["format"] == "uuid"
    assert menu["properties"]["createdAt"]["format"] == "date-time"
    recipe = menu["properties"]["recipes"]["items"]
    assert set(recipe["properties"]) == {
        "recipeId",
        "name",
        "calories",
        "nutrition",
        "cookingTime",
    }
    assert recipe["properties"]["recipeId"]["format"] == "uuid"
    assert "nutrition" in recipe["required"]
    nutrition = recipe["properties"]["nutrition"]
    assert set(nutrition["required"]) == {
        "calories",
        "proteins",
        "fats",
        "carbohydrates",
    }
    assert all(field["type"] == "number" for field in nutrition["properties"].values())
    values = [example["value"] for example in ok["examples"].values()]
    assert {"menus": []} in values
    assert any(value["menus"] for value in values)


def test_api22_documents_the_gateway_response_size_limit(schema: dict) -> None:
    response = schema["paths"][API22]["get"]["responses"]["502"]
    examples = response["content"]["application/json"]["examples"]
    assert examples["response_too_large"]["value"] == {
        "error": {
            "code": "EXTERNAL_SERVICE_ERROR",
            "message": "Upstream response exceeds the allowed size",
            "details": {"upstream": "recipes", "reason": "response_too_large"},
        }
    }
