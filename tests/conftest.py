import json
from collections.abc import AsyncIterator

import httpx
import pytest

from gateway.config import Settings
from gateway.main import create_app


# Тесты не за прокси: ключ лимита — адрес сокета. Проверки старта задают флаги сами.
@pytest.fixture(autouse=True)
def _socket_keyed_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RATE_LIMIT_KEY_BY_SOCKET", "true")


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        upstream_recipes_url="http://recipes:8000",
        upstream_dictionary_url="http://dictionary:8000",
    )


class Chunks(httpx.AsyncByteStream):
    def __init__(self, *chunks: bytes) -> None:
        self._chunks = chunks

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk


# Тело отдаётся потоком, как по сети: ответ не прочитан заранее и не декодирован.
def reply(
    status: int = 200,
    *chunks: bytes,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    return httpx.Response(status, headers=headers, stream=Chunks(*chunks))


class Recorder:
    """Подменяет MS-4: запоминает запросы и отвечает заданным ответом."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.status = 202
        self.body = b'{"task_id":"b1e2c3d4"}'
        self.headers = {"content-type": "application/json"}

    def respond(
        self, status: int, body: object = b"", headers: dict[str, str] | None = None
    ) -> None:
        self.status = status
        self.headers = dict(headers or {})
        if isinstance(body, bytes):
            self.body = body
        elif isinstance(body, str):
            self.body = body.encode()
        else:
            self.body = json.dumps(body).encode()
            self.headers.setdefault("content-type", "application/json")

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return reply(self.status, self.body, headers=self.headers)


@pytest.fixture
def upstream() -> Recorder:
    return Recorder()


@pytest.fixture
async def gateway(
    settings: Settings, upstream: Recorder
) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(settings, transport=httpx.MockTransport(upstream))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway"
    ) as client:
        yield client
