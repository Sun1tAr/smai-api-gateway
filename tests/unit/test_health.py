import httpx
import pytest

from gateway.config import Settings
from gateway.main import create_app


@pytest.mark.parametrize("upstream", ["http://127.0.0.1:9", "http://recipes:8000"])
async def test_health_does_not_depend_on_upstream(upstream: str) -> None:
    app = create_app(Settings(_env_file=None, upstream_recipes_url=upstream))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_health_is_documented() -> None:
    app = create_app(Settings(_env_file=None, upstream_recipes_url="http://r:8000"))
    operation = app.openapi()["paths"]["/health"]["get"]
    assert operation["summary"]
    assert operation["description"]
    assert operation["responses"]["200"]["content"]["application/json"]["example"] == {
        "status": "ok"
    }
