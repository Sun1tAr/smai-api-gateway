from pathlib import Path
from typing import Literal

from pydantic import AnyHttpUrl, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    upstream_recipes_url: AnyHttpUrl
    upstream_connect_timeout_seconds: float = Field(default=3, gt=0, le=60)
    upstream_read_timeout_seconds: float = Field(default=10, gt=0, le=120)
    upstream_write_timeout_seconds: float = Field(default=5, gt=0, le=60)
    upstream_pool_timeout_seconds: float = Field(default=2, gt=0, le=60)
    upstream_total_timeout_seconds: float = Field(default=12, gt=0, le=180)
    upstream_max_connections: int = Field(default=100, ge=1, le=1000)
    upstream_keepalive_expiry_seconds: float = Field(default=2, gt=0, le=60)
    upstream_max_response_bytes: int = Field(default=2 * 1024 * 1024, ge=1024)
    max_request_body_bytes: int = Field(default=64 * 1024, ge=1)
    routes_file: Path = Path("config/routes.toml")
    contract_file: Path = Path("contracts/receipes.openapi.json")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    otel_exporter_otlp_endpoint: AnyHttpUrl | None = None

    # Пустой адрес в Compose отключает экспорт трейсов для локальной разработки.
    @field_validator("otel_exporter_otlp_endpoint", mode="before")
    @classmethod
    def optional_collector(cls, value: str | None) -> str | None:
        return None if value == "" else value

    # Базовый адрес сервиса без пути: путь целиком задаёт таблица маршрутов.
    @field_validator("upstream_recipes_url")
    @classmethod
    def origin_only(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        if value.path not in (None, "", "/") or value.query or value.fragment:
            raise ValueError("UPSTREAM_RECIPES_URL must not contain a path or query")
        return value

    # Имя upstream из routes.toml сопоставляется с переменной окружения.
    def upstream_url(self, name: str) -> str | None:
        value = getattr(self, f"upstream_{name}_url", None)
        return None if value is None else str(value).rstrip("/")

    def upstream_urls(self) -> dict[str, str]:
        names = (
            field.removeprefix("upstream_").removesuffix("_url")
            for field in type(self).model_fields
            if field.startswith("upstream_") and field.endswith("_url")
        )
        return {name: url for name in names if (url := self.upstream_url(name))}
