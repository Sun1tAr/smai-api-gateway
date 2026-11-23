# Smart Meal AI — API Gateway (MS-1)

Единая точка входа **Smart Meal AI**: принимает запросы клиента, проверяет JWT,
проксирует разрешённые маршруты в MS-4 и MS-2 и отдаёт клиенту единый API.


## Роль в системе

- публикует белый список маршрутов из `config/routes.toml`;
- проксирует рецепты в MS-4 (`smai-api-receipes`) и справочники в MS-2
  (`smai-api-dictionary`);
- выполняет регистрацию и вход через MS-3 (`smai-api-user`) и сам выпускает JWT;
- проверяет токены защищённых маршрутов и хранит отозванные в Redis;
- ограничивает частоту запросов к ресурсоёмким маршрутам.

Своей БД у gateway нет. Состояние — только отозванные JWT в Redis.

## Стек

- Python 3.12, FastAPI, Starlette;
- httpx для проксирования;
- PyJWT (`ES256`), приватный ключ EC P-256;
- Redis для списка отозванных токенов;
- Pydantic Settings, structlog;
- OpenTelemetry, Prometheus;
- uv, Ruff, pytest, Docker Compose, GitHub Actions.

## API

Наружу доступен только белый список. Неизвестный путь — 404, неверный метод —
405; служебные `/health`, `/metrics`, `/docs` самих сервисов через gateway
недоступны.

| Метод | Публичный путь | Куда уходит | Аутентификация |
|---|---|---|---|
| `POST` | `/recipes/generate-menu` | MS-4 `POST /generate-menu` | необязательна |
| `GET` | `/recipes/generate-menu/{task_id}` | MS-4 `GET /generate-menu/{task_id}` | нет |
| `POST` | `/recipes/generate-menu/{task_id}/replace-recipe` | MS-4 `POST /generate-menu/{task_id}/replace-recipe` | нет |
| `GET` | `/recipes/profile/parameters` | MS-4 `GET /profile/parameters` | JWT |
| `GET` | `/recipes/profile/menus` | MS-4 `GET /profile/menus` | JWT |
| `POST` | `/recipes/cart` | MS-4 `POST /recipes/cart` | необязательна |
| `POST` | `/recipes/cart/link` | MS-4 `POST /recipes/cart/link` | нет |
| `POST` | `/recipes/{recipe_id}/like` | MS-4 `POST /recipes/{recipe_id}/like` | JWT |
| `DELETE` | `/recipes/{recipe_id}/like` | MS-4 `DELETE /recipes/{recipe_id}/like` | JWT |
| `GET` | `/recipes/liked` | MS-4 `GET /recipes/liked` | JWT |
| `GET` | `/dictionaries` | MS-2 `GET /dictionaries` | нет |
| `POST` | `/auth/register` | MS-3 `POST /register` | нет |
| `POST` | `/auth/login` | MS-3 `POST /login` | нет |
| `POST` | `/auth/logout` | gateway + Redis | JWT |
| `GET` | `/user/profile` | MS-3 `GET /profile` | JWT |
| `GET` | `/health`, `/metrics`, `/docs`, `/openapi.json` | gateway | нет |

Тела запросов и ответов gateway не разбирает и не меняет (кроме `/auth/*` и
`/user/profile`): статус, заголовки и тело MS-4 возвращаются как есть. Схемы в
`/docs` собираются из снимков контрактов в `contracts/`.

## Аутентификация

`POST /auth/register` (201) и `POST /auth/login` (200) возвращают
`{"userId", "token"}`. Gateway пересылает `{login, password}` в MS-3, получает
`userId` и подписывает JWT.

- Алгоритм `ES256` задан в коде; при старте принимается только EC-ключ P-256 в
  PEM без пароля.
- Claims: `iss=smai-gateway`, `sub=userId`, `jti` (UUID4), `iat`, `nbf`, `exp`.
- Срок жизни — `JWT_TTL_SECONDS` (по умолчанию 24 часа).
- Защищённые маршруты перечислены в `PROTECTED_ENDPOINTS`; если маршрут без
  зависимости проверки токена, gateway не стартует.
- Клиентский `X-User-Id` срезается, `Authorization` в сервисы не пересылается:
  MS-4 получает только `X-User-Id` из проверенного токена.
- `POST /auth/logout` пишет в Redis `revoked:{jti}` со сроком жизни до `exp`.
  Повторный logout и любые защищённые запросы с этим токеном получают 401.
- При недоступном Redis защищённые маршруты отвечают 503, а не 401. Исключение —
  `POST /recipes/generate-menu`: запрос обрабатывается как гостевой.

Ключ создаётся так:

```sh
openssl ecparam -name prime256v1 -genkey -noout -out jwt-private.pem
chmod 0400 jwt-private.pem
```

Файл не коммитится, в Compose монтируется секретом в
`JWT_PRIVATE_KEY_FILE=/run/secrets/jwt_private_key` и принадлежит UID 10001.

## Ошибки

Формат: `{"error": {"code", "message", "details": {}}}`. Каждый ответ содержит
`X-Request-ID`.

| HTTP | `error.code` | Когда |
|---|---|---|
| 400 | `VALIDATION_ERROR` | небезопасный путь или query |
| 401 | `UNAUTHORIZED` | токен отсутствует, недействителен или отозван |
| 404 | `NOT_FOUND` | путь вне белого списка |
| 405 | `METHOD_NOT_ALLOWED` | неверный метод |
| 413 | `PAYLOAD_TOO_LARGE` | тело больше `MAX_REQUEST_BODY_BYTES` |
| 429 | `RATE_LIMITED` | превышен лимит клиента или общий потолок |
| 502 | `EXTERNAL_SERVICE_ERROR` | сервис недоступен, оборвал соединение или ответил слишком большим телом |
| 503 | `EXTERNAL_SERVICE_ERROR` | пул соединений исчерпан, MS-3 занят хэшированием, Redis недоступен |
| 504 | `EXTERNAL_SERVICE_ERROR` | сервис не ответил вовремя |
| 500 | `INTERNAL_ERROR` | непредвиденная ошибка gateway |

Для 502–504 в `details` указывается `upstream` и `reason`
(`connect`, `protocol`, `timeout`, `pool`, `response_too_large`).

## Лимиты запросов

Лимиты включаются `RATE_LIMIT_ENABLED` и требуют выбора режима ключа: либо
`TRUSTED_PROXIES` (точные адреса прокси), либо `RATE_LIMIT_KEY_BY_SOCKET=true`
(только локально). Без режима ключа gateway не стартует.

- генерация и замена: `RATE_LIMIT_REQUESTS` на клиента и
  `RATE_LIMIT_GLOBAL_REQUESTS` суммарно за окно `RATE_LIMIT_WINDOW_SECONDS`;
- `/auth/*`: отдельные `AUTH_RATE_LIMIT_*`;
- корзина: отдельные `CART_RATE_LIMIT_*`;
- опрос `GET .../{task_id}` не лимитируется.

Счётчики живут в памяти процесса, поэтому при нескольких репликах лимит
умножается на их число.

## Конфигурация

| Переменная | Назначение | По умолчанию |
|---|---|---|
| `UPSTREAM_RECIPES_URL` | Адрес MS-4 без пути | обязательна |
| `UPSTREAM_DICTIONARY_URL` | Адрес MS-2 без пути | обязательна |
| `UPSTREAM_USER_URL` | Адрес MS-3 без пути | обязательна |
| `JWT_PRIVATE_KEY_FILE` | Путь к PEM-ключу ES256 | обязательна |
| `JWT_PRIVATE_KEY_PATH` | Путь к ключу на хосте (локальный Compose) | обязательна в Compose |
| `JWT_TTL_SECONDS` | Срок жизни токена, 60–2592000 | `86400` |
| `REDIS_URL` | Redis отозванных токенов | обязательна |
| `UPSTREAM_TOTAL_TIMEOUT_SECONDS` | Общий дедлайн запроса к MS-4/MS-2 | `12` |
| `UPSTREAM_CART_TIMEOUT_SECONDS` | Дедлайн `POST /recipes/cart` | `40` |
| `UPSTREAM_REPLACE_TIMEOUT_SECONDS` | Дедлайн замены рецепта | `125` |
| `UPSTREAM_MAX_RESPONSE_BYTES` | Предел ответа сервиса | `2097152` |
| `MAX_REQUEST_BODY_BYTES` | Предел тела запроса клиента | `65536` |
| `RATE_LIMIT_ENABLED` | Включить лимиты | `true` |
| `RATE_LIMIT_KEY_BY_SOCKET` | Ключ по адресу сокета (локально) | `false` |
| `TRUSTED_PROXIES` | Доверенные адреса прокси, CIDR | пусто |
| `ROUTES_FILE` | Файл маршрутов | `config/routes.toml` |
| `LOG_LEVEL` | Уровень логирования | `INFO` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Адрес OTLP HTTP-коллектора | экспорт отключён |
| `GATEWAY_HTTP_PORT` | Хостовой порт Compose | `8080` |

`UPSTREAM_REPLACE_TIMEOUT_SECONDS` должен быть больше `REPLACE_WAIT_SECONDS` в
MS-4 (125 > 120 по умолчанию).

## Локальный запуск

Нужны Docker и Docker Compose ≥ 2.24.

```sh
docker network create smai
cp .env.example .env
echo 'RATE_LIMIT_KEY_BY_SOCKET=true' >> .env   # только для локальной машины
openssl ecparam -name prime256v1 -genkey -noout -out jwt-private.pem
chmod 0444 jwt-private.pem
docker compose --profile stub up --build --wait
curl --fail http://127.0.0.1:8080/health
curl --fail http://127.0.0.1:8080/docs
```

Профиль `stub` поднимает gateway и заглушки MS-4/MS-3. Без него должны быть
запущены реальные MS-4 (имя `recipes`), MS-3 (`user`) и MS-2 (`dictionary`) в
сети `smai`.

Проверка аутентификации:

```sh
TOKEN=$(curl -s -X POST http://127.0.0.1:8080/auth/register \
  -H 'content-type: application/json' \
  -d '{"login":"user@example.com","password":"correct-horse-battery"}' | jq -r .token)

curl -i http://127.0.0.1:8080/user/profile -H "Authorization: Bearer $TOKEN"
curl -i -X POST http://127.0.0.1:8080/auth/logout -H "Authorization: Bearer $TOKEN"
curl -i http://127.0.0.1:8080/user/profile -H "Authorization: Bearer $TOKEN"   # 401
```

## Наблюдаемость

- JSON-логи structlog в stdout: `trace_id`, `request_id`, `service`; тела
  запросов, query и заголовки авторизации не пишутся.
- `/metrics`: HTTP-метрики по шаблону пути, метрики upstream-запросов и ошибок,
  отказов токенов, проверок и записей отзыва, состояния Redis, лимитов.
- Трейсы: OTLP HTTP; `/health` и `/metrics` не трассируются, секреты в атрибуты
  спанов не попадают.

> **Ветка.** Актуальный код и полная документация — в ветке `develop`. Этот
> README описывает состояние `develop`.

## Разработка и тесты

```sh
uv sync
uv run ruff check . && uv run ruff format --check .
uv run pytest

docker compose -f docker-compose.test.yml up --build \
  --abort-on-container-exit --exit-code-from tests
```

CI выполняет lint, unit- и интеграционные тесты, compose-smoke и сборку образа;
при push в `develop`/`main` публикует образ в GHCR и запускает деплой через
`smai-infra`.

## Известные ограничения

- Один процесс (`--workers 1`); лимиты в памяти, при нескольких репликах нужен
  общий Redis.
- При недоступном Redis отзыв токенов не проверяется; для API-1 это осознанно
  трактуется как гостевой запрос.
- Между сервисами нет mTLS: защита строится на сетевой изоляции и срезании
  клиентских заголовков.
- Базовые образы закреплены тегами, а не digest.

## Связанные репозитории

- [SmartMeal-AI](https://github.com/Sun1tAr/SmartMeal-AI);
- [smai-api-receipes](https://github.com/Sun1tAr/smai-api-receipes);
- [smai-api-dictionary](https://github.com/Sun1tAr/smai-api-dictionary);
- [smai-api-user](https://github.com/Sun1tAr/smai-api-user);
- [smai-infra](https://github.com/Sun1tAr/smai-infra).
