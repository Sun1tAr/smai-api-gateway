import pytest

from gateway.config import Settings


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, upstream_recipes_url="http://recipes:8000")
