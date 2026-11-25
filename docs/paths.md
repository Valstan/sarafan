# SETKA: архитектура и пути (актуально)

Если документация расходится с кодом — приоритет у `main.py`, `web/api/*` и `tasks/celery_app.py`.

## 1) Компоненты

- FastAPI: `main.py`, `web/api/*`, `web/templates/*`
- PostgreSQL: `database/models.py`
- Redis: кеш + уведомления + Celery broker/backend
- Celery: задачи и расписания (`tasks/celery_app.py`)
- Nginx: reverse proxy и `/static` (`config/setka.conf.editable`)
- Monitoring: `/metrics` + `config/prometheus.yml`
- **Агентный слой:** `gateway_mcp/` (реестр инструментов, цикл агента, профили)
- **Локальный стенд (Windows):** `infra/windows/` (замена systemd-юнитов)

## 2) Потоки данных (основные)

Контент-пайплайн:

```mermaid
flowchart LR
  Vk[VK_API] --> VkMonitor[modules.vk_monitor]
  VkMonitor --> DbPosts[(PostgreSQL_posts)]
  DbPosts --> Filters[modules.filters]
  Filters --> Scoring[modules.core.scoring]
  Scoring --> Aggregator[modules.aggregation]
  Aggregator --> Publisher[modules.publisher]
  Publisher --> VkOut[VK_Publication]
  Publisher --> TgOut[Telegram_Publication]
```

Уведомления:

```mermaid
flowchart LR
  CeleryBeat[Celery_beat] --> NotifTasks[tasks_notification_tasks]
  NotifTasks --> Checkers[modules.notifications]
  Checkers --> Redis[(Redis_setka_notifications)]
  Api[web.api.notifications] --> Redis
  Checkers --> Telegram[Telegram_Bot]
```

## 3) Модель данных (кратко)

Источник истины: `database/models.py`.

- `regions`: код, имя, `vk_group_id`, активность, JSON `config`
- `communities`: источники VK, связь с регионом, категория, активность
- `posts`: посты VK, метрики, AI‑поля, статус публикации, fingerprints
- `vk_tokens`: имена токенов и метаданные
- `filters`: фильтры по паттернам, типу, категории
- `publish_schedules`: расписания публикаций

## 4) API (фактические endpoints)

Источник истины: `web/api/*`. Ниже — сводка по префиксам.

### Health (`/api/health`)

- `GET /`
- `GET /full`

### Regions (`/api/regions`)

- CRUD: `GET /`, `GET /{region_code}`, `POST /`, `PUT /{region_id}`, `DELETE /{region_id}`
- Управление активностью: `PATCH /{region_id}/toggle-status`
- Bulletin template: `GET/PUT /{region_code}/bulletin-template`, `POST /{region_code}/bulletin-template/reset`, `POST /{region_code}/bulletin-template/reset-topic`
- Список сообществ сети для рассылки людям: `GET /vk-links` (страница `/regions/links`, формат — `modules/region_links.py`); в том же ответе поле `growth` — прирост подписчиков сети (окна сутки/30 дней/полгода + помесячная полоса, арифметика в `modules/network_growth.py` по снимкам `region_member_snapshots`)

### Communities (`/api/communities`)

- CRUD: `GET /`, `POST /`, `PUT /{community_id}`, `DELETE /{community_id}`
- По региону: `GET /region/{region_id}`

### Posts (`/api/posts`)

- `GET /` (список), `GET /{post_id}` (деталь)

### Notifications (`/api/notifications`)

- Списки: `GET /`, `GET /suggested`, `GET /messages`, `GET /comments`
- Управление: `POST /check-now`, `DELETE /`

### Tokens (`/api/tokens`)

- CRUD: `GET /`, `GET /{token_name}`, `POST /add`, `PUT /{token_name}`, `DELETE /{token_name}`
- Валидация: `POST /{token_name}/validate`, `POST /validate-all`

### VK monitoring (`/api/vk`)

- `GET /stats`, `GET /validate-tokens`, `GET /carousel-status`, `POST /optimize-frequency`

### Publisher (`/api/publisher`)

- Статусы: `GET /groups`, `GET /status`
- Публикация: `POST /publish/simple`, `POST /publish/region`, `POST /publish/custom`
- Черновики региона: `GET /regions/{region_code}/posts`

### Parsing (`/api/parsing`)

- `POST /start`
- `GET /status/{job_id}`
- `GET /download/{job_id}`

### Scheduler (`/api/scheduler`)

- `GET /optimal-time/{region_code}`, `GET /engagement-report/{region_code}`
- `GET /should-publish-now/{region_code}`, `GET /calendar/{region_code}`
- `GET /forecast`, `POST /schedule`

### Schedule Management (`/api/schedule`)

- CRUD: `GET /all`, `GET /region/{region_code}`, `POST /add`, `PUT /update/{schedule_id}`, `DELETE /delete/{schedule_id}`
- Генерация: `POST /generate-default`

### Системные/служебные endpoints (укрупнено)

Источники: `web/api/workflow.py`, `system_monitoring.py`, `task_monitoring.py`, `service_notifications.py`.

- Workflow: статус, запуск цикла, publish, расписание, stats
- System monitoring: stats/operations/regions-status/workflow-status/live/system-status
- Task monitoring: recent/active/scheduled/statistics/overview
- Service notifications: list/status/by-type/clear/types/live
- Test workflow: start/stop/status/scan-region

## 5) UI (страницы)

`/`, `/regions`, `/regions/links`, `/posts`, `/communities`, `/notifications`, `/tokens`, `/publisher`, `/monitoring`, `/schedule`, `/parsing`, `/parsing-stats`, `/pipeline`

## 6) Агентный слой (`gateway_mcp/`)

Единый слой «модель выдаёт задание — инструмент выполняет»
([ADR-0024](adr/0024-agentnyy-sloy-zadanie-instrument.md)).

| Модуль | Что |
|---|---|
| `gateway_mcp/tool_registry.py` | `ToolSpec` (имя, описание, JSON Schema, обработчик) и `ToolRegistry` (каталог, вызов с валидацией) |
| `gateway_mcp/tools.py` | Read-инструменты VK-шлюза: `vk_get_community`, `vk_get_wall`, `vk_call` |
| `gateway_mcp/local_tools.py` | Read-инструменты по локальной базе: `sarafan_list_regions`, `sarafan_find_posts`, `sarafan_region_stats` |
| `gateway_mcp/registry.py` | Сборка реестра (какие инструменты доступны агенту) |
| `gateway_mcp/agent.py` | Цикл агента: задание → модель → инструмент → результат (`AgentResult`) |
| `gateway_mcp/model_adapters.py` | Адаптеры движков: DeepSeek, MiniMax Code CLI, заглушка |
| `gateway_mcp/profiles.py` | Профили агентов: очередь (`agent`, `agent_wave`, `agent_discovery`), движок, лимит шагов |
| `gateway_mcp/bench.py` | Замер ёмкости машины для агентов |
| `gateway_mcp/client.py`, `gateway_mcp/server.py` | HTTP-клиент шлюза и MCP-обёртка |

Вход в конвейер — задача `tasks.celery_app.run_agent_task`; постановка в
очередь профиля — `tasks.celery_app.enqueue_agent_task`.

Подробности и живой каталог инструментов — в
[`AGENT_RUNTIME.md`](AGENT_RUNTIME.md).

## 7) Локальный стенд (Windows)

Замена systemd-юнитов боевого контура — `infra/windows/`:

| Файл | Что |
|---|---|
| `infra/windows/local_env.py` | Чтение env-файла процессом (на Windows нет `EnvironmentFile=`) |
| `infra/windows/common.ps1` | Общая часть скриптов запуска |
| `infra/windows/start-web.ps1` | Веб (замена юнита `setka`) |
| `infra/windows/start-worker.ps1` | Воркер очереди `celery` (пул `solo`) |
| `infra/windows/start-wave-worker.ps1` | Воркер очередей `wave`, `digest` |
| `infra/windows/start-beat.ps1` | Планировщик (замена `setka-celery-beat`) |
| `infra/windows/start-vk-bot.ps1` | Демон VK-бота (замена `setka-vk-bot`) |
| `infra/windows/start-radar-intake.ps1` | Приём радара (замена `setka-radar-intake`) |
| `infra/windows/register-scheduled-tasks.ps1` | Задачи планировщика Windows (по одной на юнит) |
| `infra/windows/prod_dump.sh` | Снятие дампа боевой базы (только чтение) |
| `infra/windows/prod_counters.sh` | Счётчики боевой базы для построчной сверки |
| `infra/windows/classifier_parity.py` | Построчная сверка классификатора: локальный движок против боевых вердиктов |

Инвентаризация и план переезда — в
[`MIGRATION_TO_WINDOWS.md`](MIGRATION_TO_WINDOWS.md).
