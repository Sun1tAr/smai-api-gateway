from functools import cached_property
from pathlib import Path
from typing import Literal

from pydantic import (
    AnyHttpUrl,
    Field,
    SecretStr,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from gateway.ratelimit import Network, parse_networks


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    upstream_recipes_url: AnyHttpUrl
    upstream_dictionary_url: AnyHttpUrl
    upstream_user_url: AnyHttpUrl
    upstream_connect_timeout_seconds: float = Field(default=3, gt=0, le=60)
    upstream_read_timeout_seconds: float = Field(default=10, gt=0, le=120)
    upstream_write_timeout_seconds: float = Field(default=5, gt=0, le=60)
    upstream_pool_timeout_seconds: float = Field(default=2, gt=0, le=60)
    upstream_total_timeout_seconds: float = Field(default=12, gt=0, le=180)
    upstream_cart_timeout_seconds: float = Field(default=40, gt=0, le=120)
    upstream_replace_timeout_seconds: float = Field(default=125, gt=0, le=180)
    upstream_max_connections: int = Field(default=100, ge=1, le=1000)
    upstream_keepalive_expiry_seconds: float = Field(default=2, gt=0, le=60)
    upstream_max_response_bytes: int = Field(default=2 * 1024 * 1024, ge=1024)
    max_request_body_bytes: int = Field(default=64 * 1024, ge=1)
    rate_limit_enabled: bool = True
    rate_limit_requests: int = Field(default=10, ge=1, le=10_000)
    rate_limit_window_seconds: float = Field(default=60, gt=0, le=86_400)
    rate_limit_max_keys: int = Field(default=10_000, ge=1, le=1_000_000)
    # Общий потолок на всех клиентов: защищает LLM, даже если адресов много.
    rate_limit_global_requests: int = Field(default=100, ge=1, le=100_000)
    # Вход и регистрация: свой бакет на клиента и свой общий потолок, отдельно от LLM.
    auth_rate_limit_enabled: bool = True
    auth_rate_limit_requests: int = Field(default=10, ge=1, le=10_000)
    auth_rate_limit_global_requests: int = Field(default=60, ge=1, le=100_000)
    cart_rate_limit_enabled: bool = True
    cart_rate_limit_requests: int = Field(default=1, ge=1, le=10_000)
    cart_rate_limit_global_requests: int = Field(default=3, ge=1, le=100_000)
    rate_limit_ipv6_prefix: int = Field(default=56, ge=48, le=64)
    # Явный выбор «ключ — адрес сокета» для запуска без доверенных прокси.
    rate_limit_key_by_socket: bool = False
    # Точные адреса прокси (CIDR через запятую), которым верим в X-Forwarded-For.
    trusted_proxies: str = ""
    routes_file: Path = Path("config/routes.toml")
    contract_file: Path = Path("contracts/receipes.openapi.json")
    contract_dictionary_file: Path = Path("contracts/dictionary.openapi.json")
    contract_user_file: Path = Path("contracts/user.openapi.json")
    # Приватный ключ подписи JWT: EC P-256 в PEM без пароля. Только файл, не значение.
    jwt_private_key_file: Path
    jwt_ttl_seconds: int = Field(default=86_400, ge=60, le=2_592_000)
    # Хранилище отозванных токенов. Адрес может содержать пароль, поэтому SecretStr.
    redis_url: SecretStr
    redis_connect_timeout_seconds: float = Field(default=1, gt=0, le=30)
    redis_operation_timeout_seconds: float = Field(default=1, gt=0, le=30)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    otel_exporter_otlp_endpoint: AnyHttpUrl | None = None

    # Пустой адрес в Compose отключает экспорт трейсов для локальной разработки.
    @field_validator("otel_exporter_otlp_endpoint", mode="before")
    @classmethod
    def optional_collector(cls, value: str | None) -> str | None:
        return None if value == "" else value

    @field_validator("redis_url")
    @classmethod
    def redis_scheme(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().startswith(("redis://", "rediss://")):
            raise ValueError("REDIS_URL must start with redis:// or rediss://")
        return value

    @field_validator("trusted_proxies")
    @classmethod
    def valid_networks(cls, value: str) -> str:
        try:
            parse_networks(value)
        except ValueError:
            raise ValueError("TRUSTED_PROXIES must be comma-separated CIDRs") from None
        return value

    @cached_property
    def trusted_networks(self) -> tuple[Network, ...]:
        return parse_networks(self.trusted_proxies)

    @model_validator(mode="after")
    def rate_limit_is_safe_to_start(self) -> "Settings":
        if (
            max(
                self.rate_limit_requests,
                self.auth_rate_limit_requests,
                self.cart_rate_limit_requests,
            )
            * self.rate_limit_max_keys
            > 10_000_000
        ):
            raise ValueError(
                "(AUTH_/CART_)RATE_LIMIT_REQUESTS * RATE_LIMIT_MAX_KEYS is too large"
            )
        # За обратным прокси без TRUSTED_PROXIES все клиенты окажутся в одном бакете.
        if (
            self.rate_limit_enabled
            and not self.trusted_networks
            and not self.rate_limit_key_by_socket
        ):
            raise ValueError(
                "rate limit needs TRUSTED_PROXIES, or RATE_LIMIT_KEY_BY_SOCKET=true "
                "to key by the socket address, or RATE_LIMIT_ENABLED=false"
            )
        return self

    # Базовый адрес сервиса без пути: путь целиком задаёт таблица маршрутов.
    @field_validator(
        "upstream_recipes_url", "upstream_dictionary_url", "upstream_user_url"
    )
    @classmethod
    def origin_only(cls, value: AnyHttpUrl, info: ValidationInfo) -> AnyHttpUrl:
        if value.path not in (None, "", "/") or value.query or value.fragment:
            name = (info.field_name or "").upper()
            raise ValueError(f"{name} must not contain a path or query")
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
