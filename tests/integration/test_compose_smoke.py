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


# Gateway, Redis и заглушка MS-3 в разных контейнерах: полный путь выхода.
def test_logout_invalidates_the_token_through_real_redis() -> None:
    with httpx.Client(base_url=GATEWAY_URL, timeout=10) as client:
        registered = client.post(
            "/auth/register",
            json={"login": "smoke@example.com", "password": "correct-horse-battery"},
        )
        headers = {"Authorization": f"Bearer {registered.json()['token']}"}
        before = client.get("/user/profile", headers=headers)
        logout = client.post("/auth/logout", headers=headers)
        after = client.get("/user/profile", headers=headers)
        guest = client.post(
            "/recipes/generate-menu", json={"peopleCount": 4, "weeklyBudget": 5000}
        )
    assert registered.status_code == 201
    assert before.status_code == 200
    assert before.json()["login"] == "smoke@example.com"
    assert (logout.status_code, logout.json()) == (200, {"success": True})
    assert after.status_code == 401
    assert guest.status_code == 202
