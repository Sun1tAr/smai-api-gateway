import httpx

from gateway.config import Settings
from gateway.main import create_app
from tests.conftest import reply

BODY = b'{"habits":[],"diets":[],"allergies":[],"kitchenTools":[]}'


async def test_dictionaries_are_proxied_to_the_dictionary_service() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return reply(200, BODY, headers={"content-type": "application/json"})

    settings = Settings(
        _env_file=None,
        upstream_recipes_url="http://recipes:8000",
        upstream_dictionary_url="http://dictionary:8000",
        rate_limit_enabled=False,
    )
    app = create_app(settings, transport=httpx.MockTransport(handler))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        ok = await client.get("/dictionaries", params={"x": "1"})
        doubled = await client.get("/dictionaries/dictionaries")
        wrong_method = await client.post("/dictionaries")
    assert ok.status_code == 200
    assert ok.content == BODY
    assert [str(r.url) for r in seen] == ["http://dictionary:8000/dictionaries?x=1"]
    assert doubled.status_code == 404
    assert wrong_method.status_code == 405
