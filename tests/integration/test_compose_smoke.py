import os

import httpx
import pytest

GATEWAY_URL = os.environ.get("GATEWAY_URL")
pytestmark = pytest.mark.skipif(
    GATEWAY_URL is None, reason="GATEWAY_URL is set only by docker-compose.test.yml"
)


# Настоящий образ gateway и заглушка MS-4 в одной Docker-сети.
def test_gateway_container_proxies_to_the_upstream_container() -> None:
    with httpx.Client(base_url=GATEWAY_URL, timeout=10) as client:
        created = client.post(
            "/recipes/generate-menu", json={"peopleCount": 4, "weeklyBudget": 5000}
        )
        task_id = created.json()["task_id"]
        result = client.get(f"/recipes/generate-menu/{task_id}")
        internal = client.get("/recipes/metrics")
        health = client.get("/health")
    assert created.status_code == 202
    assert result.status_code == 200 and result.json()["status"] == "done"
    assert internal.status_code == 404
    assert health.json() == {"status": "ok"}
    assert created.headers["x-request-id"]
