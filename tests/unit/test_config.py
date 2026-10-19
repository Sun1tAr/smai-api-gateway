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
