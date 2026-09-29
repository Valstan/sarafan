# VK Gateway — ворота доступа в VK для проектов @valstan

SARAFAN — внутренняя кухня VK: рабочие токены, клиент, smart-routing с
cooldown, per-token rate-limiter. **VK-шлюз** (`/api/gateway`) даёт другим
проектам read-only доступ к VK по HTTP: проект шлёт задачу → SARAFAN исполняет
её своим токеном (со своего IP, под своим rate-limit) → возвращает JSON.

**Токен наружу не выдаётся** — только результат. Причина не только безопасность:
VK привязывает user-токен к IP выпуска, и чужой проект с нашим токеном со своего
сервера получит `error 5 (access_token was given to another ip address)`. Работает
только модель «исполни и верни».

> **v1 — READ-ONLY.** Постинг/удаление в VK через шлюз не делаются (риск бана
> аккаунта) — отдельный guarded-слой позже.

---

## База и авторизация

| | |
|---|---|
| Base URL (внешний) | `https://3931b3fe50ab.vps.myjino.ru` (текущий myjino-хост; при переезде VPS обновить — `config/sarafan.conf.editable` → `server_name`) |
| Base URL (тот же хост) | `http://127.0.0.1:8000` |
| Заголовок авторизации | `X-API-Key: <ключ-проекта>` |
| Ключи | свой на каждый проект; **единый источник — БД `gateway_keys`** (миграция 059); env `GATEWAY_KEY_<PROJECT>` — bootstrap/аварийный fallback |

Без валидного `X-API-Key` → `401`. В логах фигурирует только имя проекта, не секрет.

### Как получить ключ (self-serve, без владельца)

Заявка — письмом через мозг (`mailbox/to-brain/` своего проекта) или напрямую
AI-сессии САРАФАН. Выдача — одна команда на хосте sarafan (рестарт НЕ нужен,
шлюз читает ключи из БД на каждом запросе):

```bash
python scripts/issue_gateway_key.py <PROJECT> --note "заявка/письмо"
# секрет печатается ОДИН раз — передать потребителю по защищённому каналу
python scripts/issue_gateway_key.py <PROJECT> --rotate    # ротация секрета
python scripts/issue_gateway_key.py <PROJECT> --disable   # отключить (env не воскресит)
python scripts/issue_gateway_key.py --import-env          # перенос старых env-ключей в БД
```

Каждая выдача/ротация видна в usage-логе (`/gateway-stats`, endpoint
`issue-key`). Семантика merge БД/env — как у VK-токенов (#336): БД главнее
при совпадении имени; выключенный в БД ключ env не воскрешает; недоступная
БД → шлюз живёт на env-ключах (аварийный fail-open).

### Форма аргументов привязки (D-047) — две грабли, стоившие прогонов

Обе найдены при выдаче `DKMALMYZH` 2026-09-01 и обе выглядят как ошибка в коде,
а не в командной строке:

- **Список owner_id начинается с минуса**, поэтому `--owner-ids <список>` argparse
  читает как ещё один флаг («expected one argument»). Работает только форма
  `--owner-ids=<список>`.
- **Личные страницы передаются голыми цифрами.** `owner_id` профиля положительный,
  и записать его как `+420841463` нельзя: валидация принимает `-?\d{1,20}`, знака
  `+` не знает, и выдача падает на `not an id`. Сама привязка положительные id
  поддерживает — и на запись, и на энфорсмент (`wall.get`/`users.get` с ними
  проходят); отрицательного требуют только `group_id`-параметры, где положительное
  `N` и означает сообщество `-N`.

Секрет печатается один раз — **не давай ему попасть в вывод сессии**: выдачу
запускают скриптом, который уводит stdout в файл с правами 600 и отдаёт наружу
только длину и первые знаки sha256, а копию убирает `shred -u` после доставки.

---

## Эндпоинты

### `POST /api/gateway/call` — универсальная дверь
Исполнить любой read-метод VK из allowlist.

```json
{ "method": "wall.get", "params": { "owner_id": -1, "count": 5 } }
```

### `GET /api/gateway/community?group=<id|screen_name>&fields=<csv>`
Инфо о сообществе (`groups.getById`). `fields` опционально (дефолт — описание,
число подписчиков, активность, статус, screen_name, фото, сайт, контакты).

### `GET /api/gateway/wall?owner_id=<-id>&count=<1..100>&offset=<n>`
Последние посты со стены (`wall.get`). Сообщество — `owner_id` со знаком минус.

---

## Формат ответа

Успех:
```json
{ "ok": true, "response": <сырой VK-payload метода> }
```

VK вернул доменную ошибку (закрытая стена, удалённый объект и т.п.):
```json
{ "ok": false, "error": { "error_code": 15, "error_msg": "Access denied" } }
```

HTTP-коды: `401` (нет/неверный ключ), `400` (метод вне allowlist), `429`
(квота, см. ниже), `503` (шлюз выключен / нет живого токена).

---

## Разрешённые методы (`GATEWAY_READ_METHODS`)

`groups.getById`, `groups.getMembers`, `groups.search`, `groups.isMember`,
`wall.get`, `wall.getById`, `wall.getComments`, `wall.getReposts`,
`users.get`, `users.getFollowers`, `users.getSubscriptions`,
`likes.getList`, `board.getTopics`, `board.getComments`,
`photos.get`, `photos.getAlbums`, `video.get`,
`utils.resolveScreenName`, `database.getCities`, `database.getCountries`,
`newsfeed.search`, `stats.getPostReach`.

Запись (`wall.post/edit/delete`, `messages.send`, `likes.add`, `wall.repost`)
запрещена → `400`.

---

## Квоты и rate-limit

- **На ключ:** `GATEWAY_QUOTA_PER_MIN` (дефолт 30) и `GATEWAY_QUOTA_PER_DAY`
  (дефолт 5000). Превышение → `429` + заголовок `Retry-After: <сек>`.
- **Агрегатный бюджет шлюза** (анти-бан, мандат brain 2026-07-12):
  `GATEWAY_GLOBAL_QUOTA_PER_MIN` (дефолт 120) — сумма по ВСЕМ потребителям
  держится ниже лимитов VK с запасом. Превышение → `429 Gateway budget
  exceeded` + `Retry-After`; запрос НЕ проглатывается в счёт VK-токена.
- **Самозащита токена** (прозрачна для потребителя): per-token rate-limiter
  (~2.5 запроса/с) + авто-cooldown токена при VK error 5/17/29 (+Telegram-alert)
  + карусель READ-токенов `last_used ASC` (#337) — нагрузка равномерна по пулу.
- **Глобально:** общий per-IP лимит приложения ~100 запросов/мин.

Эти лимиты держат VK-аккаунт SARAFAN от блокировок — потребителю достаточно
обрабатывать `429`/`Retry-After`.

---

## Примеры (curl)

```bash
KEY=...   # из /etc/sarafan/sarafan.env, GATEWAY_KEY_<PROJECT>

# инфо о сообществе
curl -s -H "X-API-Key: $KEY" \
  "https://3931b3fe50ab.vps.myjino.ru/api/gateway/community?group=apiclub"

# последние 5 постов со стены сообщества id=1
curl -s -H "X-API-Key: $KEY" \
  "https://3931b3fe50ab.vps.myjino.ru/api/gateway/wall?owner_id=-1&count=5"

# универсальный вызов
curl -s -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"method":"users.get","params":{"user_ids":"1,2","fields":"city,bdate"}}' \
  "https://3931b3fe50ab.vps.myjino.ru/api/gateway/call"
```

Для AI-сессии другого проекта: «возьми VK-данные через шлюз SARAFAN» = один
`curl` с `X-API-Key`. SDK не нужен.

---

## MCP-обёртка (для AI-сессий проектов-потребителей)

Чтобы Claude-сессия проекта-потребителя ходила в VK не через `curl`, а
**инструментами**, есть готовый MCP-сервер: [`gateway_mcp/`](../gateway_mcp/)
([README](../gateway_mcp/README.md)). Запускается в среде потребителя (не в
SARAFAN), конфиг — env `SARAFAN_GATEWAY_KEY` (+ опц. `SARAFAN_GATEWAY_URL`).

Инструменты (read-only): `vk_get_community`, `vk_get_wall`, `vk_call`. Пример
`.mcp.json` и формат ответов — в README обёртки. Ядро (`client.py`) зависит
только от `httpx` и покрыто тестами; `mcp` нужен лишь для запуска сервера у
потребителя (в зависимостях SARAFAN его нет).

---

## Эксплуатация

- Аварийный kill-switch: env `GATEWAY_DISABLED=1` → весь шлюз отдаёт `503`
  (токены и публикации SARAFAN не затрагиваются).
- Реализация: роутер `web/api/gateway.py`, квота `modules/gateway/quota.py`,
  конфиг/allowlist `config/gateway.py`. Переиспользует `TokenPolicy`
  (`modules/vk_token_router.py`) и `VKClient` (`modules/vk_monitor/vk_client.py`).
- Деплой: без миграции БД — добавить `GATEWAY_KEY_<PROJECT>` в
  `/etc/sarafan/sarafan.env` → restart web.
