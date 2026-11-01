# smai-api-gateway

MS-1 — API Gateway SmartMeal: единая точка входа для клиента. Задача **SMAI-52**:
инфраструктура и проксирование `/recipes/*` в MS-4 (`smai-api-receipes`).
Бизнес-логики и БД у gateway нет. Исключение — вход и регистрация (SMAI-100, v3):
gateway вызывает MS-3 и сам выдаёт JWT (см. «Аутентификация»). С SMAI-101 gateway
проверяет токены защищённых маршрутов (`POST /auth/logout`, `GET /user/profile`) и
хранит отозванные в Redis; своей БД у gateway по-прежнему нет.

## Эндпоинты

Наружу доступен только белый список из `config/routes.toml`. Любой другой путь
получает 404, неверный метод — 405; запрос в MS-4 при этом не уходит. Служебные
`/health`, `/metrics`, `/docs` самого MS-4 через gateway недоступны.

| Метод и путь на gateway | Куда уходит в MS-4 | Успех | Контракт |
| --- | --- | --- | --- |
| `POST /recipes/generate-menu` | `POST /generate-menu` | 202 `{"task_id"}`; токен необязателен (SMAI-102) | API-1 (SMAI-A-121) |
| `GET /recipes/generate-menu/{task_id}` | `GET /generate-menu/{task_id}` | 200 `{"status","menu","error"}`; каждый рецепт содержит `recipeId` (UUID MS-4), передаётся без изменений | API-2 (SMAI-A-127) |
| `GET /recipes/profile/parameters` | `GET /profile/parameters` | 200 `{"habits","diet","allergies","kitchenTools"}`, токен обязателен (SMAI-102) | API-13 (SMAI-A-274) |
| `GET /dictionaries` | `GET /dictionaries` (MS-2) | 200 `{"habits","diets","allergies","kitchenTools"}` | API-7 (SMAI-A-194) |
| `POST /auth/register` | `POST /register` (MS-3) | 201 `{"userId","token"}` | API-8 (SMAI-A-259) |
| `POST /auth/login` | `POST /login` (MS-3) | 200 `{"userId","token"}` | API-9 (SMAI-A-261) |
| `POST /auth/logout` | — (только gateway, Redis) | 200 `{"success": true}` | API-10 (SMAI-A-263) |
| `GET /user/profile` | `GET /profile` (MS-3) | 200 `{"userId","login","createdAt"}` | API-11 (SMAI-A-270) |
| `GET /health` | — | 200 `{"status":"ok"}` | liveness gateway |
| `GET /metrics` | — | 200, формат Prometheus | не пробрасывать наружу через NetBird |
| `GET /docs`, `GET /openapi.json` | — | Swagger UI и схема | все эндпоинты с примерами и ошибками |

Тела запросов и ответов gateway не разбирает и не меняет (кроме `/auth/*` и `/user/profile`): статус, заголовки и тело
MS-4 (включая его ошибки) возвращаются как есть. Схемы в `/docs` описательные, они
берутся из снимка контракта `contracts/receipes.openapi.json`; после готовности
MS-4 снимок заменяется экспортом его OpenAPI.

### Ошибки самого gateway

Формат везде один: `{"error": {"code", "message", "details": {}}}`. В каждом ответе
есть заголовок `X-Request-ID`.

| HTTP | `error.code` | Когда |
| --- | --- | --- |
| 400 | `VALIDATION_ERROR` | небезопасный путь (`..`, `%2f`, `%00` и т. п.) или query |
| 401 | `UNAUTHORIZED` | `POST /auth/logout`, `GET /user/profile`, `GET /recipes/profile/parameters`: токен отсутствует, недействителен (подпись, срок, формат), отозван или выдан удалённому пользователю; `POST /recipes/generate-menu`: токен передан, но недействителен (без токена 401 нет); причина в ответе не раскрывается, заголовок `WWW-Authenticate: Bearer` |
| 404 | `NOT_FOUND` | путь вне белого списка |
| 405 | `METHOD_NOT_ALLOWED` | неверный метод, заголовок `Allow` |
| 413 | `PAYLOAD_TOO_LARGE` | тело больше `MAX_REQUEST_BODY_BYTES` |
| 429 | `RATE_LIMITED` | `POST /recipes/generate-menu`, `POST /auth/register`, `POST /auth/login`: превышен лимит с одного адреса или общий потолок; заголовок `Retry-After`, `details.retry_after_seconds` |
| 502 | `EXTERNAL_SERVICE_ERROR` | MS-4 недоступен, оборвал соединение или ответил слишком большим телом |
| 503 | `EXTERNAL_SERVICE_ERROR` | у gateway исчерпан пул соединений (`details.reason = "pool"`); для `/auth/*` ещё MS-3 занят хэшированием (`details.reason = "busy"`, заголовок `Retry-After`); для `/auth/logout`, `/user/profile` и `/recipes/profile/parameters` недоступен Redis (`details = {"upstream": "redis", "reason": "connect\|timeout\|protocol"}`) |
| 504 | `EXTERNAL_SERVICE_ERROR` | MS-4 не ответил вовремя; запрос мог быть выполнен |
| 500 | `INTERNAL_ERROR` | непредвиденная ошибка gateway |

`details` ошибок 502–504: `{"upstream": "recipes", "reason": "connect|protocol|timeout|pool|response_too_large"}`.
Ответы 503 самого MS-4 (например, переполненная очередь) проходят как есть и
отличаются кодом ошибки. Если API-1 вернул 504, не повторяйте запрос
автоматически: задача могла быть создана.

## Локальный запуск

Нужны Docker и Docker Compose ≥ 2.24 (`env_file` с `required: false`). Общая сеть
для сервисов контура создаётся один раз:

```sh
docker network create smai
cp .env.example .env
# Локально прокси нет: ключ лимита — адрес сокета. На stage/prod так не делать.
echo 'RATE_LIMIT_KEY_BY_SOCKET=true' >> .env
# Ключ подписи JWT (путь задаёт JWT_PRIVATE_KEY_PATH в .env, файл не коммитится)
openssl ecparam -name prime256v1 -genkey -noout -out jwt-private.pem
chmod 0444 jwt-private.pem   # только для одиночной dev-машины; на stage/prod — 0400 и владелец UID 10001
docker compose --profile stub up --build --wait   # gateway + заглушки MS-4 и MS-3
curl --fail http://127.0.0.1:8080/health
curl -i -X POST http://127.0.0.1:8080/recipes/generate-menu \
  -H 'content-type: application/json' -d '{"peopleCount":4,"weeklyBudget":5000}'
curl --fail http://127.0.0.1:8080/metrics
TOKEN=$(curl -s -X POST http://127.0.0.1:8080/auth/register \
  -H 'content-type: application/json' \
  -d '{"login":"user@example.com","password":"correct-horse-battery"}' | jq -r .token)
curl -i http://127.0.0.1:8080/user/profile -H "Authorization: Bearer $TOKEN"
curl -i -X POST http://127.0.0.1:8080/auth/logout -H "Authorization: Bearer $TOKEN"
curl -i http://127.0.0.1:8080/user/profile -H "Authorization: Bearer $TOKEN"   # 401
```

Compose поднимает и локальный Redis (сервис `redis`, только в сети `smai`); `REDIS_URL` в `.env` обязателен, как и в контуре (в `.env.example` он уже стоит).

Без профиля `stub` запускается только gateway; MS-4 должен быть в сети `smai` под
именем `recipes`, MS-3 — под именем `user` (оба на порту 8000). Порты на хосте: gateway `GATEWAY_HTTP_PORT`
(по умолчанию 8080), MS-4 — 8001, киоск — 8000. Контейнер работает от
пользователя `gateway` (UID 10001) в одном процессе (`--workers 1`).

## Конфигурация

| Переменная | Назначение | По умолчанию |
| --- | --- | --- |
| `UPSTREAM_RECIPES_URL` | Адрес MS-4 без пути, например `http://recipes:8000` | обязательна |
| `UPSTREAM_DICTIONARY_URL` | Адрес MS-2 (справочники) без пути, например `http://dictionary:8000` | обязательна |
| `UPSTREAM_CONNECT_TIMEOUT_SECONDS` | Установка соединения | `3` |
| `UPSTREAM_READ_TIMEOUT_SECONDS` | Ожидание данных от сервиса (MS-4 и MS-2) | `10` |
| `UPSTREAM_WRITE_TIMEOUT_SECONDS` | Отправка запроса | `5` |
| `UPSTREAM_POOL_TIMEOUT_SECONDS` | Ожидание свободного соединения из пула | `2` |
| `UPSTREAM_TOTAL_TIMEOUT_SECONDS` | Общий дедлайн запроса к MS-4 и MS-2 | `12` |
| `UPSTREAM_MAX_CONNECTIONS` | Размер пула | `100` |
| `UPSTREAM_KEEPALIVE_EXPIRY_SECONDS` | Простой keep-alive; меньше таймаута серверов MS-4 и MS-2 | `2` |
| `UPSTREAM_MAX_RESPONSE_BYTES` | Предел ответа MS-4 и MS-2 | `2097152` |
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
| `CONTRACT_DICTIONARY_FILE` | Снимок контракта MS-2 для документации | `contracts/dictionary.openapi.json` |
| `UPSTREAM_USER_URL` | Адрес MS-3 (пользователи) без пути, например `http://user:8000` | обязательна |
| `JWT_PRIVATE_KEY_FILE` | Путь к PEM-файлу приватного ключа подписи JWT (EC P-256, без пароля) | обязательна |
| `JWT_PRIVATE_KEY_PATH` | Только локальный Compose: путь на хосте к PEM-файлу ключа; Compose монтирует его секретом в `JWT_PRIVATE_KEY_FILE=/run/secrets/jwt_private_key` | обязательна в Compose |
| `JWT_TTL_SECONDS` | Срок жизни токена, 60–2592000 | `86400` (24 часа) |
| `CONTRACT_USER_FILE` | Снимок контракта API-8/API-9 для документации | `contracts/user.openapi.json` |
| `AUTH_RATE_LIMIT_ENABLED` | Включает лимит на `/auth/*` (работает только при `RATE_LIMIT_ENABLED=true`) | `true` |
| `AUTH_RATE_LIMIT_REQUESTS` | Сколько запросов `/auth/*` с одного адреса за окно (общий счётчик на оба маршрута) | `10` |
| `AUTH_RATE_LIMIT_GLOBAL_REQUESTS` | Общий потолок `/auth/*` на всех клиентов за окно | `60` |
| `REDIS_URL` | Redis с отозванными токенами: `redis://[:password@]host:port/db` или `rediss://`; пароль только через URL из секрета, значение не логируется | обязательна |
| `REDIS_CONNECT_TIMEOUT_SECONDS` | Установка соединения с Redis (0–30) | `1` |
| `REDIS_OPERATION_TIMEOUT_SECONDS` | Ожидание ответа Redis на команду (0–30) | `1` |
| `LOG_LEVEL` | DEBUG / INFO / WARNING / ERROR | `INFO` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Базовый адрес OTLP HTTP, например `http://collector:4318` | экспорт отключён |
| `GATEWAY_HTTP_PORT` | Порт на 127.0.0.1 в локальном Compose | `8080` |

При запуске Python напрямую читается `.env` из рабочей директории; переменные
процесса приоритетнее. Некорректная конфигурация (в том числе файл маршрутов)
останавливает запуск. `.env` в образ не попадает, но локальный Compose передаёт его
в контейнер целиком (`env_file`, файл необязателен); на stage/prod конфигурацию и
секреты задаёт DevOps.

## Аутентификация

`POST /auth/register` (API-8) и `POST /auth/login` (API-9) — не побайтовый прокси,
а явные обработчики (`src/gateway/auth.py`). Клиент отправляет `{"login","password"}`,
gateway пересылает тело в MS-3 (`/register`, `/login`), получает `{"userId"}`,
подписывает JWT и отвечает `{"userId","token"}` с кодом 201 (регистрация) или 200
(вход) и `Cache-Control: no-store`. Пароль gateway не разбирает, не хранит и не
пишет в логи и спаны; клиентский `X-User-Id` в MS-3 не передаётся. Токен выдаёт
только gateway: MS-3 токен не знает (отклонение от текста API-8/API-9, где `token`
указан и для сервиса).

**Ответы MS-3** принимаются по белому списку: 400 (оба маршрута), 401 (только вход),
409 (только регистрация) переотдаются с его `code`, `message` и `details`; 500
превращается в собственный 500 `INTERNAL_ERROR` без подробностей MS-3; 503 с кодом
`SERVICE_BUSY` — в 503 `EXTERNAL_SERVICE_ERROR` (`reason = "busy"`) с его
`Retry-After`. Всё остальное — другой статус, 2xx без целого `userId > 0`, тело не в
формате ошибки — это 502 `EXTERNAL_SERVICE_ERROR` (`reason = "protocol"`).

**JWT.** Алгоритм `ES256` задан в коде, а не берётся из токена. При старте
принимается только EC-ключ P-256 в PEM без пароля; RSA, другие кривые и
зашифрованный PEM останавливают запуск с понятной ошибкой (содержимое файла не
логируется). Заголовок: `alg=ES256`, `typ=JWT`, `kid` — отпечаток публичного ключа
(RFC 7638, задел под ротацию ключей). Claims:

| Claim | Значение |
| --- | --- |
| `iss` | `smai-gateway` |
| `sub` | `userId` строкой |
| `jti` | случайный UUID4, уникален для каждого токена |
| `iat`, `nbf` | время выдачи, секунды |
| `exp` | `iat + JWT_TTL_SECONDS` |

Ключ создаётся и хранится так:

```sh
openssl ecparam -name prime256v1 -genkey -noout -out jwt-private.pem
chmod 0400 jwt-private.pem && sudo chown 10001:10001 jwt-private.pem
```

**Права на Linux.** Compose монтирует секрет как bind mount и сохраняет владельца и
права файла с хоста: с `0400` и владельцем не UID 10001 gateway не стартует
(`JWT_PRIVATE_KEY_FILE cannot be read`). Связка `chown 10001:10001` + `chmod 0400` на
Linux-хосте пока не проверена (на Docker Desktop for Mac права скрыты): проверить на
стенде до выкатки. Файл монтируется в контейнер только на чтение (в Compose — секрет `jwt_private_key`,
путь на хосте задаёт `JWT_PRIVATE_KEY_PATH`), принадлежит непривилегированному
пользователю gateway (UID 10001), не кладётся в образ и не коммитится (`*.pem` в
`.gitignore` и `.dockerignore`). Токены проверяет сам gateway по
публичной части этого же ключа; другим сервисам токен не передаётся.

**Лимит.** `/auth/register` и `/auth/login` имеют отдельный лимит с теми же
механизмом, окном и режимом ключа, что и `/recipes/generate-menu`, но своими
счётчиками: не более `AUTH_RATE_LIMIT_REQUESTS` (10) запросов за окно с одного
клиента на оба маршрута вместе и не более `AUTH_RATE_LIMIT_GLOBAL_REQUESTS` (60) от
всех. Слот возвращается, если запрос не дошёл до проверки пароля (502 `connect`,
503 `pool`, 503 `busy`, 413). `AUTH_RATE_LIMIT_ENABLED=false` отключает только его.
Путь `auth` зарезервирован и не может быть префиксом в `config/routes.toml`.

### Проверка токена, выход и профиль (SMAI-101)

**Защищённые маршруты** — `POST /auth/logout`, `GET /user/profile` и
`GET /recipes/profile/parameters` (API-13); в коде их
перечисляет `PROTECTED_ENDPOINTS` (`src/gateway/api.py`). Если у такого маршрута нет
зависимости проверки токена или он не зарегистрирован, gateway не стартует. Остальные
маршруты работают без токена; `POST /recipes/generate-menu` принимает токен
необязательно (см. «Необязательный токен» ниже).

**Как проверяется токен.** Заголовок `Authorization: Bearer <jwt>`: схема без учёта
регистра, один заголовок, один токен, до 4096 символов. Затем подпись `ES256` по
публичному ключу gateway (алгоритм задан в коде: `alg=none`, HS256 и другие
отклоняются), `kid` текущего ключа, `iss=smai-gateway`, наличие `exp`, `iat`, `nbf`,
`jti`, `sub` без запаса по времени, `sub` — целое от 1 до 2147483647 (int32, как `users.id` в MS-3), `jti` — UUID. Любая ошибка даёт
одинаковый 401 `UNAUTHORIZED`; причина (`missing`, `malformed`, `algorithm`, `kid`,
`signature`, `expired`, `not_yet_valid`, `claims`, `revoked`, `user_not_found`) видна только в логе и в
метрике `gateway_token_rejected_total{reason}`, сам токен нигде не пишется. Потом
проверяется список отозванных в Redis. Идентификатор пользователя берётся из `sub`;
клиентский `X-User-Id` (любой регистр, дубли) срезается, а `Authorization` в сервисы
не пересылается ни на каких маршрутах: MS-3 получает только `X-User-Id` из токена.

**`POST /auth/logout`.** Тело не нужно. Пишет в Redis ключ `revoked:{jti}` со
значением `1` и сроком жизни `exp − сейчас` (минимум 1 секунда), так что запись
исчезает вместе с токеном. Ответ 200 `{"success": true}`. Дальше этот токен получает
401 на любом защищённом маршруте, повторный logout тоже 401; другие токены
пользователя действуют. Если запись не удалась, ответ 503 и токен остаётся
действующим: logout можно повторить.

**`GET /user/profile`.** Берёт пользователя из токена и вызывает MS-3
`GET /profile` с `X-User-Id`. Ответ MS-3 проверяется по белому списку: 200 с телом
`{userId, login, createdAt}`, где `userId` совпадает с `sub`; 404 `USER_NOT_FOUND`
(токен выдан удалённому пользователю) превращается в 401; 500 в 500 без подробностей;
503 `SERVICE_BUSY` в 503 с `Retry-After`; остальное, в том числе 401 от MS-3, в 502
`EXTERNAL_SERVICE_ERROR`. Лишние поля ответа MS-3 клиенту не отдаются.

**Redis недоступен.** Токен без проверки на отзыв не принимается, поэтому
`POST /auth/logout` и `GET /user/profile` отвечают 503 `EXTERNAL_SERVICE_ERROR`
(`details.upstream = "redis"`, `reason`: `connect`, `timeout` или `protocol`). Это 503,
а не 401: пользователя не нужно «разлогинивать» из-за сбоя, запрос можно повторить.
Гостевые маршруты (`POST /recipes/generate-menu` — в том числе с токеном, как гость,
`GET /dictionaries`) работают;
gateway стартует и при мёртвом Redis, соединение открывается при первой проверке.
`/health` Redis не проверяет, иначе его падение остановило бы и гостевые маршруты у
оркестратора. Ретраев нет, таймауты `REDIS_*_TIMEOUT_SECONDS` по секунде.

### Сохранённые параметры и необязательный токен (SMAI-102)

**`GET /recipes/profile/parameters` (API-13).** Токен обязателен (401 без него, 503
при недоступном Redis). Gateway ставит в MS-4 `X-User-Id` из токена и проксирует
`GET /profile/parameters`; ответ 200 `{"habits","diet","allergies","kitchenTools"}` —
параметры последней готовой генерации пользователя, при её отсутствии пустые списки и
`diet: null` (404 нет). В `/docs` маршрут с обязательным `bearerAuth` и ответами 200,
401, 500, 502, 503, 504.

**Необязательный токен на `POST /recipes/generate-menu` (API-1).** Маршрут перечислен в
`OPTIONAL_AUTH_ENDPOINTS` (`src/gateway/api.py`); без зависимости `optional_user`
gateway не стартует, иначе все пользователи тихо стали бы гостями.
Без заголовка `Authorization` запрос гостевой: в MS-4 нет `X-User-Id`, параметры не
сохраняются. С действительным токеном `X-User-Id` берётся из токена. Недействительный
токен (подпись, срок, формат, отзыв) даёт 401, а не гостя; единственное исключение —
сбой Redis (ниже). Токен проверяется раньше
лимита запросов. В `/docs` `security: [{}, {bearerAuth: []}]`, ответ 401.
Клиентский `X-User-Id` срезается на всех маршрутах, `Authorization` в сервисы не уходит.

**Redis недоступен для API-1.** Если запрос пришёл с токеном, но отзыв проверить
нельзя (локального кэша отзывов нет), запрос обрабатывается как гостевой, в том числе
с **отозванным** токеном: отзыв в этот момент не проверяется, 401 нет. Ответ 202, `X-User-Id` в MS-4 не
передаётся, параметры не сохраняются. Прав это не добавляет. В логе предупреждение
`token_redis_down_guest` (без токена), счётчик `gateway_token_fallback_guest_total`.
Неверная подпись или истёкший токен по-прежнему дают 401 (Redis для них не нужен).
Для остальных защищённых маршрутов (в том числе API-13) остаётся 503.

**Выкатка (DevOps).** Env не меняются. Порядок: gateway со SMAI-101, затем MS-4 со
SMAI-102 (миграция `0005_parameters_user_id` при старте), затем gateway со SMAI-102.
MS-4 доверяет `X-User-Id`, поэтому его нельзя выкатывать раньше gateway, который
срезает клиентский `X-User-Id` (SMAI-101): иначе владельца можно подделать.

**Требования к Redis (DevOps).** Доступен только из сети `smai`, наружу не
публиковать; пароль через `REDIS_URL` из секрета. Нужно сохранение на диск
(`appendonly yes`, `appendfsync everysec`) и `maxmemory-policy noeviction`: после
рестарта без сохранения или при вытеснении ключей отозванные токены снова станут
действующими до своего `exp`. Данных мало: только `jti` отозванных токенов не дольше
`JWT_TTL_SECONDS`. Выкатка: Redis, затем MS-3, затем gateway. Состояние видно в
метриках: `gateway_redis_up` (появляется после первого обращения), `gateway_revocation_checks_total{result}`,
`gateway_revocation_writes_total{result}`.

Лимит запросов на эти два маршрута не вводится: проверка подписи дешёвая, а
защищаемых ресурсов за ними нет. Префиксы `auth` и `user` зарезервированы и не могут
быть префиксом в `config/routes.toml`.

## Как добавить путь или сервис

1. Добавьте строку в `allow` (или новый `[[route]]`) в `config/routes.toml`.
   Формат: `"МЕТОД /путь/в/сервисе"`, `{name}` — один сегмент пути. Публичный путь —
   `/<prefix><путь в сервисе>`; при `prefix = ""` он совпадает с путём в сервисе
   (так опубликован `/dictionaries`). Пустой префикс не может занимать `/health`,
   `/metrics`, `/docs` и пространства имён других префиксов. Пустой префикс может
   быть только у одного маршрута.
2. Для нового сервиса добавьте поле `upstream_<имя>_url` в `Settings` и переменную
   окружения `UPSTREAM_<ИМЯ>_URL`.
3. Опишите операцию в снимке контракта (`summary`, `description`, `requestBody`,
   `responses` с примерами): без этого gateway не стартует. Снимок не должен
   содержать `$ref` (схемы вписываются внутрь). Новому сервису нужен свой снимок:
   добавьте поле `contract_<имя>_file` (`CONTRACT_<ИМЯ>_FILE`) в `Settings` и
   передайте его в `load_contracts(...)` в `main.py`.

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
  поэтому клиентские `X-Forwarded-*` на этот адрес не влияют, пока сосед не
  доверенный. Заголовок `Server` не отдаётся (`--no-server-header`). `X-Forwarded-Proto` в MS-4 всегда `http`,
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
  `gateway_client_ip_fallback_total{reason}`. В логе событие `rate_limited` пишется
  на каждую новую «самую старую попытку» ключа (не на каждый 429, поэтому под атакой
  не больше числа слотов окна), с коротким HMAC-хэшем вместо адреса. Отказы
  `table_full` в лог не пишутся, они видны только в метрике.
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
   `RATE_LIMIT_KEY_BY_SOCKET=true` и вывести адреса соединений к порту 8000 (в образе
   нет `ss`, поэтому через `/proc/net/tcp`, где `1F40` = порт 8000, `01` = ESTABLISHED):

   ```sh
   docker compose exec gateway python -c "import socket,struct;[print(socket.inet_ntoa(struct.pack('<I',int(r.split()[2].split(':')[0],16)))) for r in open('/proc/net/tcp').readlines()[1:] if r.split()[1].endswith(':1F40') and r.split()[3]=='01']"
   ```

Валидация при старте: `/0` и префиксы короче `/8` (IPv4) и `/32` (IPv6) отклоняются,
адрес с битами хоста (`10.1.2.3/8`) — тоже: опечатка не должна молча расширять
доверие. Пробельное значение `" , "` не считается заданным.

**Порядок выкатки на stage.** После мержа gateway **не стартует**, пока в секрете
контура нет режима ключа:

1. До мержа DevOps (Sun1tAr) добавляет в секрет `RATE_LIMIT_ENABLED=false`.
2. Узнать фактический адрес соседа (NetBird Reverse Proxy, команда выше), убедиться,
   что прокси перезаписывает `X-Forwarded-For`.
3. Заменить в секрете на `TRUSTED_PROXIES=<адрес>/32` и `RATE_LIMIT_ENABLED=true`.
4. Проверить два запроса с разных клиентов: растёт `gateway_rate_limit_tracked_keys`
   на две записи; следить за `gateway_client_ip_fallback_total` (рост = клиенты слиплись).

### Компромисс общего потолка

Общий потолок защищает бюджет LLM, но им можно управлять: 10 активных клиентов (или
10 подсетей /56 одной сети /48) по 10 запросов исчерпывают 100 запросов за окно, и все
остальные получают 429 `global` с `Retry-After`. Это осознанный выбор в пользу
стоимости генерации против доступности; значения 100 на всех и 10 на клиента нужно
подтвердить. Алерт: `increase(gateway_rate_limited_total{scope="global"}[5m]) > 0`.
`table_full` при значениях по умолчанию недостижим (общий потолок принимает не более
100 новых клиентов за окно, а таблица на 10000), он нужен как страховка при больших
`RATE_LIMIT_GLOBAL_REQUESTS`.

## Наблюдаемость

- Логи: JSON в stdout (structlog) с `trace_id`, `request_id`, `service`; тела,
  query и заголовки авторизации не пишутся. Успешные `/health` и `/metrics`
  пишутся на DEBUG, их ошибки (4xx/5xx) на INFO. События `http_request`,
  `upstream_request`, `upstream_request_failed`.
- Метрики `/metrics`: стандартные HTTP-метрики по шаблону пути и
  `gateway_upstream_requests_total{upstream,route,method,status}`,
  `gateway_upstream_request_duration_seconds{upstream,route}`,
  `gateway_upstream_errors_total{upstream,reason}`, `gateway_upstream_up{upstream}`
  (0 только после `connect`/`timeout`), `gateway_token_rejected_total{reason}`,
  `gateway_revocation_checks_total{result}`, `gateway_revocation_writes_total{result}`,
  `gateway_redis_up` (1 после успешной команды, 0 после ошибки соединения или
  таймаута; до первого обращения к Redis серии нет). Эндпоинт описан в `/docs` с тегом `service`.
- Трейсы: `/health` и `/metrics` не трассируются; query, `User-Agent`, адрес клиента и заголовки
  `Authorization`/`Cookie`/`X-User-Id` в атрибуты спанов не попадают. Каждый запрос начинает новый трейс gateway (клиентский `traceparent`
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
