from pathlib import Path

import pytest
from pydantic import ValidationError

from gateway.config import Settings


def make(**values: object) -> Settings:
    return Settings(_env_file=None, **values)


def test_defaults_match_the_plan() -> None:
    settings = make(
        upstream_recipes_url="http://recipes:8000",
        upstream_dictionary_url="http://dictionary:8000",
    )
    assert settings.upstream_connect_timeout_seconds == 3
    assert settings.upstream_read_timeout_seconds == 10
    assert settings.upstream_total_timeout_seconds == 12
    assert settings.max_request_body_bytes == 64 * 1024
    assert settings.upstream_max_response_bytes == 2 * 1024 * 1024
    assert settings.otel_exporter_otlp_endpoint is None


def test_upstream_url_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("UPSTREAM_RECIPES_URL", raising=False)
    with pytest.raises(ValidationError):
        make()


@pytest.mark.parametrize("value", ["", "recipes:8000", "ftp://recipes", "http://r/x"])
def test_invalid_upstream_url_is_rejected(value: str) -> None:
    with pytest.raises(ValidationError):
        make(upstream_recipes_url=value)


def test_upstream_url_error_does_not_echo_the_value() -> None:
    with pytest.raises(ValidationError) as error:
        make(
            upstream_recipes_url="http://user:secret@r/path",
            upstream_dictionary_url="http://dictionary:8000",
        )
    assert "secret" not in str(error.value)


def test_dictionary_url_with_a_path_is_rejected_and_named() -> None:
    with pytest.raises(ValidationError) as error:
        make(
            upstream_recipes_url="http://recipes:8000",
            upstream_dictionary_url="http://dictionary:8000/api",
        )
    assert "UPSTREAM_DICTIONARY_URL" in str(error.value)


def test_empty_otlp_endpoint_disables_export() -> None:
    settings = make(
        upstream_recipes_url="http://recipes:8000",
        upstream_dictionary_url="http://dictionary:8000",
        otel_exporter_otlp_endpoint="",
    )
    assert settings.otel_exporter_otlp_endpoint is None


@pytest.mark.parametrize(
    "name", ["upstream_read_timeout_seconds", "max_request_body_bytes"]
)
def test_non_positive_limits_are_rejected(name: str) -> None:
    with pytest.raises(ValidationError):
        make(
            upstream_recipes_url="http://recipes:8000",
            upstream_dictionary_url="http://dictionary:8000",
            **{name: 0},
        )


def test_upstream_url_lookup_by_name() -> None:
    settings = make(
        upstream_recipes_url="http://recipes:8000",
        upstream_dictionary_url="http://dictionary:8000",
    )
    assert settings.upstream_url("recipes") == "http://recipes:8000"
    assert settings.upstream_url("unknown") is None


def test_user_service_and_key_are_required(monkeypatch) -> None:
    monkeypatch.delenv("UPSTREAM_USER_URL")
    monkeypatch.delenv("JWT_PRIVATE_KEY_FILE")
    monkeypatch.delenv("REDIS_URL")
    with pytest.raises(ValidationError) as error:
        Settings(
            _env_file=None,
            upstream_recipes_url="http://recipes:8000",
            upstream_dictionary_url="http://dictionary:8000",
        )
    fields = {item["loc"][0] for item in error.value.errors()}
    assert fields == {"upstream_user_url", "jwt_private_key_file", "redis_url"}


def test_user_url_must_be_an_origin() -> None:
    with pytest.raises(ValidationError) as error:
        Settings(
            _env_file=None,
            upstream_recipes_url="http://recipes:8000",
            upstream_dictionary_url="http://dictionary:8000",
            upstream_user_url="http://user:8000/api",
        )
    assert "UPSTREAM_USER_URL" in str(error.value)


@pytest.mark.parametrize("ttl", [59, 2_592_001, 0, -1])
def test_jwt_ttl_is_bounded(ttl: int) -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            upstream_recipes_url="http://recipes:8000",
            upstream_dictionary_url="http://dictionary:8000",
            jwt_ttl_seconds=ttl,
        )


def test_jwt_defaults() -> None:
    settings = Settings(
        _env_file=None,
        upstream_recipes_url="http://recipes:8000",
        upstream_dictionary_url="http://dictionary:8000",
    )
    assert settings.jwt_ttl_seconds == 86_400
    assert settings.contract_user_file == Path("contracts/user.openapi.json")
    assert "user" in settings.upstream_urls()


def test_redis_defaults() -> None:
    settings = make(
        upstream_recipes_url="http://recipes:8000",
        upstream_dictionary_url="http://dictionary:8000",
    )
    assert settings.redis_connect_timeout_seconds == 1
    assert settings.redis_operation_timeout_seconds == 1
    assert settings.redis_url.get_secret_value() == "redis://redis:6379/0"


@pytest.mark.parametrize(
    "value", ["", "redis", "http://redis:6379", "unix:///run/redis.sock", "tcp://r:1"]
)
def test_redis_url_scheme_is_checked_without_echoing_the_value(value: str) -> None:
    secret = f"{value}-hunter2"
    with pytest.raises(ValidationError) as error:
        make(
            upstream_recipes_url="http://recipes:8000",
            upstream_dictionary_url="http://dictionary:8000",
            redis_url=secret,
        )
    assert "REDIS_URL" in str(error.value)
    assert "hunter2" not in str(error.value)


def test_rediss_with_password_is_accepted_and_hidden() -> None:
    settings = make(
        upstream_recipes_url="http://recipes:8000",
        upstream_dictionary_url="http://dictionary:8000",
        redis_url="rediss://:hunter2@redis:6380/1",
    )
    assert "hunter2" not in repr(settings)
    assert "hunter2" not in str(settings)


@pytest.mark.parametrize(
    "name",
    ["redis_connect_timeout_seconds", "redis_operation_timeout_seconds"],
)
@pytest.mark.parametrize("value", [0, -1, 31])
def test_redis_timeouts_are_bounded(name: str, value: float) -> None:
    with pytest.raises(ValidationError):
        make(
            upstream_recipes_url="http://recipes:8000",
            upstream_dictionary_url="http://dictionary:8000",
            **{name: value},
        )
