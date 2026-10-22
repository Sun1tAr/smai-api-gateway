import asyncio
import socket
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import httpx
import pytest
import uvicorn

from gateway.config import Settings
from gateway.main import create_app
from tests.stubs.recipes_stub import app as stub_app
from tests.stubs.user_stub import app as user_stub_app


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
async def stub_url() -> AsyncIterator[str]:
    """Заглушка MS-4 под настоящим uvicorn: сокеты, таймауты и framing реальные."""
    stub_app.state.requests.clear()
    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            stub_app,
            host="127.0.0.1",
            port=port,
            log_level="warning",
            timeout_graceful_shutdown=1,
        )
    )
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task


@pytest.fixture
async def user_stub_url() -> AsyncIterator[str]:
    """Заглушка MS-3 под настоящим uvicorn."""
    user_stub_app.state.users.clear()
    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            user_stub_app,
            host="127.0.0.1",
            port=port,
            log_level="warning",
            timeout_graceful_shutdown=1,
        )
    )
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task


@pytest.fixture
def stub_requests() -> list[dict]:
    return stub_app.state.requests


@pytest.fixture
def open_gateway() -> Callable[..., object]:
    @asynccontextmanager
    async def open_(upstream_url: str, **overrides: object):
        settings = Settings(
            _env_file=None,
            upstream_recipes_url=upstream_url,
            upstream_dictionary_url="http://dictionary:8000",
            upstream_read_timeout_seconds=0.3,
            upstream_total_timeout_seconds=1,
            **overrides,
        )
        app = create_app(settings)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway"
        ) as client:
            yield client
            await app.state.upstream_client.aclose()

    return open_
