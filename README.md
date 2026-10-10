# smai-api-gateway

MS-1 — API Gateway SmartMeal: единая точка входа для клиента. Задача **SMAI-52**:
инфраструктура и проксирование `/recipes/*` в MS-4 (`smai-api-receipes`).
Бизнес-логики и БД у gateway нет. Аутентификации в v1 нет (гостевой режим, она
запланирована на v3), как нет и ограничения частоты запросов: это отдельная задача.

## Эндпоинты

Наружу доступен только белый список из `config/routes.toml`. Любой другой путь
получает 404, неверный метод — 405; запрос в MS-4 при этом не уходит. Служебные
`/health`, `/metrics`, `/docs` самого MS-4 через gateway недоступны.

| Метод и путь на gateway | Куда уходит в MS-4 | Успех | Контракт |
| --- | --- | --- | --- |
| `POST /recipes/generate-menu` | `POST /generate-menu` | 202 `{"task_id"}` | API-1 (SMAI-A-121) |
| `GET /recipes/generate-menu/{task_id}` | `GET /generate-menu/{task_id}` | 200 `{"status","menu","error"}` | API-2 (SMAI-A-127) |
| `GET /health` | — | 200 `{"status":"ok"}` | liveness gateway |
| `GET /metrics` | — | 200, формат Prometheus | не пробрасывать наружу через NetBird |
| `GET /docs`, `GET /openapi.json` | — | Swagger UI и схема | все эндпоинты с примерами и ошибками |

Тела запросов и ответов gateway не разбирает и не меняет: статус, заголовки и тело
MS-4 (включая его ошибки) возвращаются как есть. Схемы в `/docs` описательные, они
берутся из снимка контракта `contracts/receipes.openapi.json`; после готовности
MS-4 снимок заменяется экспортом его OpenAPI.

### Ошибки самого gateway

Формат везде один: `{"error": {"code", "message", "details": {}}}`. В каждом ответе
есть заголовок `X-Request-ID`.

| HTTP | `error.code` | Когда |
| --- | --- | --- |
| 400 | `VALIDATION_ERROR` | небезопасный путь (`..`, `%2f`, `%00` и т. п.) или query |
| 404 | `NOT_FOUND` | путь вне белого списка |
| 405 | `METHOD_NOT_ALLOWED` | неверный метод, заголовок `Allow` |
| 413 | `PAYLOAD_TOO_LARGE` | тело больше `MAX_REQUEST_BODY_BYTES` |
| 502 | `EXTERNAL_SERVICE_ERROR` | MS-4 недоступен, оборвал соединение или ответил слишком большим телом |
| 503 | `EXTERNAL_SERVICE_ERROR` | у gateway исчерпан пул соединений (`details.reason = "pool"`) |
| 504 | `EXTERNAL_SERVICE_ERROR` | MS-4 не ответил вовремя; запрос мог быть выполнен |
| 500 | `INTERNAL_ERROR` | непредвиденная ошибка gateway |

`details` ошибок 502–504: `{"upstream": "recipes", "reason": "connect|protocol|timeout|pool|response_too_large"}`.
Ответы 503 самого MS-4 (например, переполненная очередь) проходят как есть и
отличаются кодом ошибки. Если API-1 вернул 504, не повторяйте запрос
автоматически: задача могла быть создана.

## Локальный запуск

Нужен Docker Compose ≥ 2.24 (`env_file` с `required: false`).

Нужны Docker и Docker Compose. Общая сеть для сервисов контура создаётся один раз:

```sh
docker network create smai
cp .env.example .env
docker compose --profile stub up --build --wait   # gateway + заглушка MS-4
curl --fail http://127.0.0.1:8080/health
curl -i -X POST http://127.0.0.1:8080/recipes/generate-menu \
  -H 'content-type: application/json' -d '{"peopleCount":4,"weeklyBudget":5000}'
curl --fail http://127.0.0.1:8080/metrics
```

Без профиля `stub` запускается только gateway; MS-4 должен быть в сети `smai` под
именем `recipes` и слушать порт 8000. Порты на хосте: gateway `GATEWAY_HTTP_PORT`
(по умолчанию 8080), MS-4 — 8001, киоск — 8000. Контейнер работает от
пользователя `gateway` (UID 10001) в одном процессе (`--workers 1`).

## Конфигурация

| Переменная | Назначение | По умолчанию |
| --- | --- | --- |
| `UPSTREAM_RECIPES_URL` | Адрес MS-4 без пути, например `http://recipes:8000` | обязательна |
| `UPSTREAM_CONNECT_TIMEOUT_SECONDS` | Установка соединения | `3` |
| `UPSTREAM_READ_TIMEOUT_SECONDS` | Ожидание данных от MS-4 | `10` |
| `UPSTREAM_WRITE_TIMEOUT_SECONDS` | Отправка запроса | `5` |
| `UPSTREAM_POOL_TIMEOUT_SECONDS` | Ожидание свободного соединения из пула | `2` |
| `UPSTREAM_TOTAL_TIMEOUT_SECONDS` | Общий дедлайн запроса к MS-4 | `12` |
| `UPSTREAM_MAX_CONNECTIONS` | Размер пула | `100` |
| `UPSTREAM_KEEPALIVE_EXPIRY_SECONDS` | Простой keep-alive; меньше таймаута сервера MS-4 | `2` |
| `UPSTREAM_MAX_RESPONSE_BYTES` | Предел ответа MS-4 | `2097152` |
| `MAX_REQUEST_BODY_BYTES` | Предел тела запроса клиента | `65536` |
| `ROUTES_FILE` | Файл маршрутов | `config/routes.toml` |
| `CONTRACT_FILE` | Снимок контракта MS-4 для документации | `contracts/receipes.openapi.json` |
| `LOG_LEVEL` | DEBUG / INFO / WARNING / ERROR | `INFO` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Базовый адрес OTLP HTTP, например `http://collector:4318` | экспорт отключён |
| `GATEWAY_HTTP_PORT` | Порт на 127.0.0.1 в локальном Compose | `8080` |

При запуске Python напрямую читается `.env` из рабочей директории; переменные
процесса приоритетнее. Некорректная конфигурация (в том числе файл маршрутов)
останавливает запуск. `.env` в образ не попадает, но локальный Compose передаёт его
в контейнер целиком (`env_file`, файл необязателен); на stage/prod конфигурацию и
секреты задаёт DevOps.

## Как добавить путь или сервис

1. Добавьте строку в `allow` (или новый `[[route]]`) в `config/routes.toml`.
   Формат: `"МЕТОД /путь/в/сервисе"`, `{name}` — один сегмент пути.
2. Для нового сервиса добавьте поле `upstream_<имя>_url` в `Settings` и переменную
   окружения `UPSTREAM_<ИМЯ>_URL`.
3. Опишите операцию в снимке контракта (`summary`, `description`, `requestBody`,
   `responses` с примерами): без этого gateway не стартует.

## Безопасность прокси

- Путь разбирается по сырому `raw_path`; закодированные `..`, `/`, `\`, NUL и
  двойное кодирование отклоняются с 400. Пути с `//` и `%2f` вместо разделителя не
  совпадают с шаблоном маршрута и получают 404; в MS-4 они не уходят.
- Клиентские `X-Forwarded-*`, `Forwarded`, `X-Real-IP`, `X-Request-ID`, `Cookie`,
  `traceparent`, `tracestate`, `baggage`, `X-User-*`, `X-Auth-*` в MS-4 не
  передаются; gateway выставляет свои `X-Forwarded-For/Proto` и `X-Request-ID`.
  `X-Forwarded-For` для MS-4 — адрес непосредственного соседа gateway (за NetBird
  это адрес его reverse proxy, а не клиента). uvicorn запущен с `--no-proxy-headers`,
  поэтому клиентские `X-Forwarded-*` на адрес соседа не влияют. Заголовок `Server`
  не отдаётся (`--no-server-header`). `X-Forwarded-Proto` в MS-4 всегда `http`,
  даже если снаружи был TLS.
- Тело запроса читается и пересылается только у методов с телом (POST); у GET оно
  отбрасывается. Тело POST больше `MAX_REQUEST_BODY_BYTES` получает 413.
- `X-Request-ID` клиента принимается, если он ≤128 символов `[A-Za-z0-9._-]`,
  иначе генерируется новый. Тот же идентификатор возвращается клиенту.
- Редиректы MS-4 не выполняются: ответ 3xx превращается в 502 (`reason=protocol`),
  `Location` с внутренним хостом клиенту не отдаётся. `HTTP_PROXY`/`NO_PROXY` из окружения не используются:
  клиент всегда создаётся с явным транспортом, а `trust_env=False`.
- Повторов нет, кроме одной попытки установить соединение, когда запрос ещё не
  ушёл.

## Наблюдаемость

- Логи: JSON в stdout (structlog) с `trace_id`, `request_id`, `service`; тела,
  query и заголовки авторизации не пишутся. События `http_request`,
  `upstream_request`, `upstream_request_failed`.
- Метрики `/metrics`: стандартные HTTP-метрики по шаблону пути и
  `gateway_upstream_requests_total{upstream,route,method,status}`,
  `gateway_upstream_request_duration_seconds{upstream,route}`,
  `gateway_upstream_errors_total{upstream,reason}`, `gateway_upstream_up{upstream}`
  (0 только после `connect`/`timeout`). Эндпоинт описан в `/docs` с тегом `service`.
- Трейсы: `/health` и `/metrics` не трассируются; query, `User-Agent` и адрес клиента
  в атрибуты спанов не попадают. Каждый запрос начинает новый трейс gateway (клиентский `traceparent`
  не доверяется) и передаёт `traceparent` в MS-4.

## Разработка и тесты

```sh
uv sync
uv run pytest                 # юнит- и интеграционные тесты, MS-4 — заглушка на локальном порту
uv run ruff check . && uv run ruff format --check .
docker compose -f docker-compose.test.yml up --build --abort-on-container-exit --exit-code-from tests
```

Последняя команда собирает образ, поднимает gateway и заглушку MS-4 в отдельной
Docker-сети и прогоняет все тесты, включая smoke по HTTP между контейнерами.
