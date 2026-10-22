"""Минимальная заглушка MS-3 для интеграционных тестов.

Хранит пользователей в памяти и отвечает как настоящий сервис: `{userId}` без
токена, ошибки в общем формате. Пароли хранятся открыто, поэтому только для тестов.
"""

import json
from datetime import UTC, datetime

from fastapi import FastAPI, Request, Response

app = FastAPI(title="user-stub", redirect_slashes=False)
app.state.users = {}
# Заголовки последнего запроса профиля: тесты проверяют, что токен до MS-3 не дошёл.
app.state.profile_headers = []


def error(status: int, code: str, message: str) -> Response:
    body = {"error": {"code": code, "message": message, "details": {}}}
    return Response(json.dumps(body), status_code=status, media_type="application/json")


async def credentials(request: Request) -> tuple[str, str] | None:
    try:
        body = await request.json()
        login, password = body["login"], body["password"]
    except (ValueError, KeyError, TypeError):
        return None
    if not isinstance(login, str) or not isinstance(password, str):
        return None
    return login.strip().lower(), password


@app.post("/register")
async def register(request: Request) -> Response:
    given = await credentials(request)
    if given is None or len(given[1]) < 12:
        return error(400, "VALIDATION_ERROR", "Request validation failed")
    login, password = given
    if login in app.state.users:
        return error(409, "LOGIN_ALREADY_EXISTS", "User with this login already exists")
    app.state.users[login] = (len(app.state.users) + 1, password)
    return Response(
        json.dumps({"userId": app.state.users[login][0]}),
        status_code=201,
        media_type="application/json",
    )


@app.post("/login")
async def login(request: Request) -> Response:
    given = await credentials(request)
    if given is None:
        return error(400, "VALIDATION_ERROR", "Request validation failed")
    user = app.state.users.get(given[0])
    if user is None or user[1] != given[1]:
        return error(401, "INVALID_CREDENTIALS", "Invalid login or password")
    return Response(
        json.dumps({"userId": user[0]}), status_code=200, media_type="application/json"
    )


@app.get("/profile")
async def profile(request: Request) -> Response:
    app.state.profile_headers.append(dict(request.headers))
    raw = request.headers.get("x-user-id", "")
    for login, (user_id, _) in app.state.users.items():
        if raw == str(user_id):
            body = {
                "userId": user_id,
                "login": login,
                "createdAt": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            return Response(json.dumps(body), media_type="application/json")
    if not raw.isdigit():
        return error(401, "UNAUTHORIZED", "User is not identified")
    return error(404, "USER_NOT_FOUND", "User not found")
