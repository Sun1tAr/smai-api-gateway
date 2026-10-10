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
| 429 | `RATE_LIMITED` | только `POST /recipes/generate-menu`: превышен лимит запросов с одного адреса; заголовок `Retry-After`, `details.retry_after_seconds` |
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
| `RATE_LIMIT_ENABLED` | Включает лимит на `POST /recipes/generate-menu` (`false` — отключён, схема `/docs` не меняется) | `true` |
| `RATE_LIMIT_GLOBAL_REQUESTS` | Общий потолок на всех клиентов за окно | `100` |
| `RATE_LIMIT_IPV6_PREFIX` | Длина префикса IPv6 в ключе (48–64) | `56` |
| `RATE_LIMIT_KEY_BY_SOCKET` | Явный выбор «ключ — адрес сокета», если `TRUSTED_PROXIES` пуст | `false` |
| `RATE_LIMIT_REQUESTS` | Сколько запросов с одного адреса за окно | `10` |
| `RATE_LIMIT_WINDOW_SECONDS` | Длина скользящего окна | `60` |
| `RATE_LIMIT_MAX_KEYS` | Потолок числа отслеживаемых адресов (новые отклоняются при переполнении); `REQUESTS × MAX_KEYS ≤ 10 000 000` | `10000` |
| `TRUSTED_PROXIES` | Точные адреса прокси (CIDR) через запятую, которым верим в `X-Forwarded-For`; обязателен при включённом лимите, если нет `RATE_LIMIT_KEY_BY_SOCKET` | пусто |
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
  `X-Forwarded-For` для MS-4 — адрес клиента: по умолчанию адрес сокетного соседа
  (за NetBird это адрес его reverse proxy), а при заданных `TRUSTED_PROXIES` —
  клиент из цепочки (см. «Лимит запросов»). uvicorn запущен с `--no-proxy-headers`,
  поэтому клиентские `X-Forwarded-*` на этот адрес не влияют, пока сосед не доверенный. Заголовок `Server`
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

## Лимит запросов

Лимит защищает дорогую LLM-генерацию: `POST /recipes/generate-menu` принимает не
более `RATE_LIMIT_REQUESTS` запросов за `RATE_LIMIT_WINDOW_SECONDS` (10 за 60 с) с
одного клиента и не более `RATE_LIMIT_GLOBAL_REQUESTS` (100) за то же окно от всех
вместе. Остальные операции (в том числе опрос `GET .../{task_id}`) не ограничены.
Это отклонение от «без ограничений доступа» в спеках v1, поэтому лимит можно
выключить: `RATE_LIMIT_ENABLED=false` (метрики лимита при этом остаются, серии нулевые).

**Режим ключа обязателен.** Включённый лимит не стартует, пока не выбран способ
определить клиента: либо `TRUSTED_PROXIES` (клиент берётся из `X-Forwarded-For`),
либо явное `RATE_LIMIT_KEY_BY_SOCKET=true` (ключ — адрес сокета; за обратным прокси
это адрес прокси, и все клиенты окажутся в одном бакете), либо
`RATE_LIMIT_ENABLED=false`. В режиме сокета при старте пишется warning
`rate_limit_keyed_by_socket_address`. В `.env.example` для локального запуска стоит
`RATE_LIMIT_KEY_BY_SOCKET=true`; на stage и prod его ставить не нужно.

- Проверка стоит после сопоставления маршрута (зависимость FastAPI, обязана
  оставаться `async def`), поэтому варианты пути либо дают 404/400, либо учитываются.
- Скользящий журнал в памяти процесса. `Retry-After` — целое число секунд ≥ 1 до
  освобождения самой старой попытки; клиент, выждавший его, проходит.
- В лимит идут все попытки, кроме тех, что заведомо не дошли до сервиса: 400
  (небезопасный путь), 413, 502 `connect` и 503 `pool` возвращают слот. Остальные,
  включая 504, 502 `protocol` и любые ответы MS-4, слот тратят: генерация могла
  начаться. Отклонённые (429) в счёт не идут; если общий потолок отказал, слот
  клиента возвращается.
- Ключ — IP клиента; IPv6 группируется по префиксу `RATE_LIMIT_IPV6_PREFIX`
  (по умолчанию /56, допустимо 48–64), потому что абоненту обычно отдают целую сеть.
  Клиенты без адреса (unix-сокет) делят один бакет `unknown`.
- Таблица адресов ограничена `RATE_LIMIT_MAX_KEYS`. При переполнении новые адреса
  отклоняются с 429 (`scope="table_full"`), пока не истечёт самая давняя запись:
  вытеснять активные записи значило бы снимать лимит, поэтому таблица закрыта, а не
  открыта. Размер таблицы виден в `gateway_rate_limit_tracked_keys`.
- Один процесс (`--workers 1`) и память: при нескольких репликах лимит умножается на
  их число (нужен общий Redis), при перезапуске счётчики сбрасываются. Клиенты за
  CGNAT делят адрес и могут получать ложные 429, поэтому есть выключатель.
- Метрики без адресов: `gateway_rate_limited_total{route,scope}` (`scope` =
  `client`, `global`, `table_full`), `gateway_rate_limit_tracked_keys`,
  `gateway_client_ip_fallback_total{reason}`. В логе событие `rate_limited` один раз
  за окно на ключ, с коротким HMAC-хэшем вместо адреса.
- Клиент (Android) должен обрабатывать 429 и не повторять запрос раньше `Retry-After`.
  Коды `RATE_LIMITED` (429), `PAYLOAD_TOO_LARGE` (413), `METHOD_NOT_ALLOWED` (405) и
  504 нужно добавить в TDR-4.

### Доверенные прокси (`TRUSTED_PROXIES`)

`X-Forwarded-For` читается, только если сокетный сосед входит в `TRUSTED_PROXIES`;
тогда клиентом считается правый недоверенный адрес цепочки, а её начало (его пишет
клиент) игнорируется. `Forwarded` (RFC 7239) не поддерживается. Разбор терпим к
порту (`1.2.3.4:5555`, `[::1]:443`), пустым элементам и скобкам; zone id и мусор
останавливают разбор, и клиентом остаётся адрес прокси (счётчик
`gateway_client_ip_fallback_total`: если он растёт, прокси пишет заголовок в формате,
который gateway не понимает, и клиенты слипаются в один бакет).

Безопасность держится на трёх условиях:

1. Доверять нужно **точным адресам** (`/32`, `/128`) самих прокси, а не подсети. В
   подсети оверлея NetBird (100.64.0.0/10) и в сети Docker `smai` сидят другие узлы,
   и любой из них подставил бы произвольный `X-Forwarded-For`.
2. Прокси обязан **перезаписывать или дописывать** `X-Forwarded-For`. Прокси, который
   пропускает клиентский заголовок без изменений, делает подделку тривиальной.
3. Нужно знать, какой адрес gateway видит у соседа на самом деле. Порт опубликован как
   `127.0.0.1:8080:8000`; при userland-proxy Docker сосед выглядит как шлюз bridge-сети
   (`172.x.0.1`), а не как NetBird proxy. Адрес проверяют так: временно запустить с
   `RATE_LIMIT_KEY_BY_SOCKET=true` и посмотреть `docker compose exec gateway ss -tn`.

Валидация при старте: `/0` и префиксы короче `/8` (IPv4) и `/32` (IPv6) отклоняются,
адрес с битами хоста (`10.1.2.3/8`) — тоже: опечатка не должна молча расширять
доверие. Пробельное значение `" , "` не считается заданным.

**Чек-лист выкатки на stage:** узнать фактический адрес соседа (NetBird Reverse
Proxy), записать в секрет `TRUSTED_PROXIES` его `/32`, убедиться, что прокси
перезаписывает `X-Forwarded-For`, проверить два запроса с разных клиентов (разные
`gateway_rate_limit_tracked_keys`), смотреть `gateway_client_ip_fallback_total`.

## Наблюдаемость

- Логи: JSON в stdout (structlog) с `trace_id`, `request_id`, `service`; тела,
  query и заголовки авторизации не пишутся. Успешные `/health` и `/metrics`
  пишутся на DEBUG, их ошибки (4xx/5xx) на INFO. События `http_request`,
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
