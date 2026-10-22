import json
from collections.abc import AsyncIterator

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from gateway.config import Settings
from gateway.main import create_app


# Тесты не за прокси: ключ лимита — адрес сокета. Проверки старта задают флаги сами.
@pytest.fixture(autouse=True)
def _socket_keyed_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RATE_LIMIT_KEY_BY_SOCKET", "true")


def write_key(path, curve: ec.EllipticCurve | None = None):
    key = ec.generate_private_key(curve or ec.SECP256R1())
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return path


# Ключ создаётся один раз на прогон; секретов в репозитории нет.
@pytest.fixture(scope="session")
def jwt_key_file(tmp_path_factory: pytest.TempPathFactory):
    return write_key(tmp_path_factory.mktemp("jwt") / "jwt-private.pem")


# Обязательные настройки входа задаются окружением, чтобы Settings(...) в тестах
# мог их не повторять; конкретные тесты переопределяют их аргументами.
@pytest.fixture(autouse=True)
def _auth_environment(monkeypatch: pytest.MonkeyPatch, jwt_key_file) -> None:
    monkeypatch.setenv("UPSTREAM_USER_URL", "http://user:8000")
    monkeypatch.setenv("JWT_PRIVATE_KEY_FILE", str(jwt_key_file))


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
