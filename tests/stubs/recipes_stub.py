"""Минимальная заглушка MS-4 для локального запуска и интеграционных тестов.

Отвечает по контракту API-1/API-2 (202 на создание, 404 в формате ошибки), а
также имеет служебные /health, /metrics, /docs: если gateway их ошибочно
пропустит, тесты это увидят.
"""

import asyncio
import gzip
import json

from fastapi import FastAPI, Request, Response

app = FastAPI(title="recipes-stub", redirect_slashes=False)
app.state.requests = []

RESULT = {
    "status": "done",
    "menu": {
        "recipes": [
            {
                "recipeId": f"00000000-0000-4000-8000-{index + 1:012d}",
                "name": f"Рецепт {index}",
                "ingredients": [{"name": "Вода", "quantity": 1, "unit": "л"}],
                "calories": 100,
                "nutrition": {
                    "calories": 100,
                    "proteins": 1,
                    "fats": 1,
                    "carbohydrates": 1,
                },
                "cookingTime": 10,
                "instructions": ["Приготовить."],
                "photo": None,
            }
            for index in range(3)
        ]
    },
    "error": None,
}


def error(status: int, code: str, message: str) -> Response:
    body = {"error": {"code": code, "message": message, "details": {}}}
    return Response(json.dumps(body), status_code=status, media_type="application/json")


@app.middleware("http")
async def record(request: Request, call_next):
    app.state.requests.append(
        {
            "method": request.method,
            "target": request.scope["raw_path"].decode()
            + (
                "?" + request.scope["query_string"].decode()
                if request.scope["query_string"]
                else ""
            ),
            "headers": dict(request.headers),
            "client_port": request.client.port if request.client else None,
        }
    )
    return await call_next(request)


@app.post("/generate-menu", status_code=202)
async def generate_menu(request: Request) -> Response:
    try:
        payload = await request.json()
        people, budget = payload["peopleCount"], payload["weeklyBudget"]
    except (ValueError, KeyError, TypeError):
        return error(400, "VALIDATION_ERROR", "Request is invalid")
    if not isinstance(people, int) or isinstance(people, bool) or people < 1:
        return error(400, "VALIDATION_ERROR", "Request is invalid")
    if not isinstance(budget, int | float) or budget <= 0:
        return error(400, "VALIDATION_ERROR", "Request is invalid")
    return Response(
        json.dumps({"task_id": "b1e2c3d4-5678-90ab-cdef-1234567890ab"}),
        status_code=202,
        media_type="application/json",
    )


@app.get("/generate-menu/{task_id}")
async def generate_result(task_id: str, request: Request) -> Response:
    if task_id == "slow":
        await asyncio.sleep(5)
    if task_id == "compressed":
        body = gzip.compress(json.dumps(RESULT).encode())
        return Response(
            body,
            media_type="application/json",
            headers={"content-encoding": "gzip"},
        )
    if task_id == "pending":
        return Response(
            json.dumps({"status": "pending", "menu": None, "error": None}),
            media_type="application/json",
        )
    if task_id == "crash":
        return Response(
            "Internal Server Error", status_code=500, media_type="text/plain"
        )
    if task_id.startswith("b1e2c3d4"):
        return Response(json.dumps(RESULT), media_type="application/json")
    return error(404, "NOT_FOUND", "Generation task not found")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/metrics")
async def metrics() -> Response:
    return Response("stub_internal_metric 1\n", media_type="text/plain")


# API-13: у заглушки «сохранены» параметры пользователя 7, у остальных пусто.
SAVED_PARAMETERS = {
    7: {"habits": [3, 5], "diet": 4, "allergies": [1, 2], "kitchenTools": [1, 2, 6]}
}
NO_PARAMETERS = {"habits": [], "diet": None, "allergies": [], "kitchenTools": []}


@app.get("/profile/parameters")
async def profile_parameters(request: Request) -> Response:
    raw = request.headers.get("x-user-id")
    if raw is None or not raw.isdigit():
        return error(401, "UNAUTHORIZED", "User is not identified")
    body = SAVED_PARAMETERS.get(int(raw), NO_PARAMETERS)
    return Response(json.dumps(body), media_type="application/json")
