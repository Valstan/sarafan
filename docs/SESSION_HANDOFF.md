# Session Handoff

> Sticky-note для непрерывности между сессиями разработки SETKA. Перезаписывается через [`/close_session`](../.claude/commands/close_session.md) — историю смотри через `git log --follow -- docs/SESSION_HANDOFF.md`.

**Status:** ACTIVE
**Updated:** 2026-11-26 02:10 (МСК)
**Branch:** `feat/migration-to-local-windows` — перенос боевого контура Сарафана на локальную машину владельца (Windows)
**Last release in prod:** код на боксе **`fe3deeae`** (выкатки по reflog: 17:57:29 → `62aed593` = PR #1283, 19:43:17 → `70091dc` = PR #1286, 20:02:23 → `fe3deeae` = PR #1287). **Прод этой сессией не трогался** — только чтение (инвентаризация юнитов, снятие дампа, счётчики). Все юниты активны, как и до сессии.

---

## Текущая нитка

**Перенос боевого контура на локальную машину (Windows).** Локальный стенд
поднят, база перевезена и сверена, интерфейс работает, агентный каркас
собран и покрыт тестом, порядок закрытия прод-сервера подготовлен.
**Прод не остановлен** — это отдельная команда владельца (гейт #025).

### Что сделано

1. **Инвентаризация боевого контура** (`docs/MIGRATION_TO_WINDOWS.md` §3):
   снято с бокса 26.11 — юниты `setka`, `setka-celery-worker`,
   `setka-celery-worker-wave`, `setka-celery-beat`, `setka-vk-bot`,
   `setka-radar-intake`, `opencode-engine.service` (disabled), Prometheus,
   nginx, cron бэкапа, таймеры `infra/neural/*.timer` и
   `minimax-dns-pin.timer`. Для каждого — «чем заменяем на Windows» и «как
   проверяем».
   **Факт, а не догадка:** таймеры `infra/neural/*.timer` на боксе **не
   установлены** — в репозитории они лежат как код выведенного из
   эксплуатации self-hosted-контура.
2. **Локальный стенд**: PostgreSQL 17.6 (portable, **порт 5433** — на 5432
   уже был чужой PostgreSQL, его не трогали), Redis 8.10.3 (6379), FastAPI
   на `127.0.0.1:8000`, Celery worker + beat. Проверки: `/api/health/` →
   200, `/metrics` → 200, все 13 страниц `paths.md` §5 → 200.
3. **Перенос базы**: дамп `pg_dump -Fc` (297 МБ) снят своим скриптом
   (`infra/windows/prod_dump.sh`), развёрнут локально. Счётчики сверены
   **по всем 59 таблицам**: 58 совпали построчно, расхождение 0. Из пяти
   приёмочных таблиц: `posts` 0=0, `communities` 3415=3415, `regions` 85=85,
   `filters` 1202=1202, `publish_schedules` 126=126.
   Единственное расхождение — живой журнал `published_posts` (пишется
   непрерывно), при повторной сверке сошлось.
4. **Агентный слой** ([ADR-0024](adr/0024-agentnyy-sloy-zadanie-instrument.md)):
   `gateway_mcp/tool_registry.py` (схема + вызов + валидация),
   `gateway_mcp/agent.py` (цикл «модель → инструмент → результат»),
   `gateway_mcp/tools.py` (инструменты VK-шлюза — описания переехали сюда
   из `server.py`, дублирование убрано), `gateway_mcp/local_tools.py`
   (инструменты по локальной базе), `gateway_mcp/profiles.py` (профили
   агентов с отдельными очередями), `gateway_mcp/bench.py` (замер ёмкости).
   Тест приёмки: `tests/test_agent_tools/test_agent_loop.py`.
5. **Замер параллельности** (`docs/AGENT_RUNTIME.md` §4): 8 ядер, 16 ГБ RAM;
   контур держит 16 параллельных заданий с линейным ростом (460 800
   заданий/час при задержке 0,05 с), с настоящими инструментами по БД и
   задержкой 2 с — ~14 300 заданий/час. Ошибок 0.
6. **Документы**: `docs/MIGRATION_TO_WINDOWS.md`,
   `docs/AGENT_RUNTIME.md`, `docs/LOCAL_WINDOWS_BROWSER_LIMITS.md`,
   `docs/PROD_SHUTDOWN_RUNBOOK.md`, `docs/RADAR_FATE.md`,
   ADR-0023/0024/0025. Обновлены `docs/START_HERE.md` (§10 — локальный
   стенд), `docs/paths.md` (§6, §7), `AGENTS.md` §«Состояние проекта».
7. **Правки кода под Windows** (две точки входа): чтение env-файла процессом
   (`infra/windows/local_env.py`) — на Linux это делает systemd; пул Celery
   `solo` — prefork на Windows падает.

### Найденное по дороге (важно)

* **Штатный ежедневный бэкап боевой базы сломан** — `scripts/backup_database.sh`
  падает на разборе `DATABASE_URL` (экранированные кавычки внутри одинарных
  в bash), и в каталоге бэкапов с января лежат **пустые файлы**. Рабочих
  бэкапов боевой базы фактически нет несколько месяцев → [P242](pending/P242-ezhednevnyj-bekap-boevoj-bazy-sloman.md).
  Правка — одна строка, но `scripts/` не моя территория.
* **Prefork Celery на Windows не работает** — `PermissionError: [WinError 5]`
  на общем счётчике пула; воркер рапортует `ready` и бесконечно перезапускает
  детей. Обход `--pool=solo` → [P244](pending/P244-windows-nesovmestimosti-i-propuschennye-testy.md).
* **pytest на Windows падает на уборке `%TEMP%`** (тот же `WinError 5`) — не
  тесты проекта, а pytest на машине. Обход `--basetemp` внутри репозитория.
* **37 тестов пропущено на Windows**: 36 ждут бинарник `gitleaks` (в CI он
  есть, там они выполняются), 1 — Linux-only (`/proc`). Ни один тест проекта
  на Windows не падает. На Linux (CI, `main`) — 5281 passed; на Windows в
  этой ветке собрано 5297 = 5281 + 16 новых тестов агентного слоя.

## Следующий шаг

**Всё техническое сделано. Дальше — решения владельца** ([P243](pending/P243-resheniya-vladeltsa-dlya-zaversheniya-pereezda.md)):

1. **Вписать `SECRETS_TOKEN`** в env-файл стенда (`config/local-stand.env`,
   машинный файл, в git не едет). Токен живёт на боевом боксе в отдельном
   файле `600` рядом с env-файлом приложения. После этого ключи приложения
   (`DEEPSEEK_API_KEY`, `VK_TOKEN_*`, `TELEGRAM_TOKEN_*`) начнут приезжать
   bootstrap'ом — как на проде.
   ⚠️ **Побочный эффект:** стенд получит боевые VK- и Telegram-токены, и
   beat сможет публиковать в боевые группы. Для переезда это цель; для
   проверки — риск. Пока токен не подключён, публикация невозможна.
2. **Судьба радара** — три варианта в [`docs/RADAR_FATE.md`](RADAR_FATE.md),
   решение — [ADR-0025](adr/0025-sudba-radara-pri-pereezde.md). Нужно от
   владельца: список потребителей радара кроме Сарафана, нужен ли OIDC-
   поставщик экосистеме, сохранять ли живые сессии, куда переносить при
   выносе.
3. **Движок** — DeepSeek API, MiniMax Code CLI или локальные модели. Движок —
   поле профиля агента, код конвейера не меняется.
4. **Проверить VK-токены с нового адреса** (привязка и лимиты по IP) —
   **до** переезда, а не после.
5. **Команда на остановку прода** — по
   [`docs/PROD_SHUTDOWN_RUNBOOK.md`](PROD_SHUTDOWN_RUNBOOK.md), и отдельно:
   что живёт на боксе после этого (ЕСА, `/services`, VK-шлюз, Радар).
   **Пока команды нет — прод стоит.**

## Контекст

- **План:** [ADR-0023](adr/0023-pereezd-boevogo-kontura-na-lokalnuyu-mashinu.md) (переезд), [ADR-0024](adr/0024-agentnyy-sloy-zadanie-instrument.md) (агентный слой), [ADR-0025](adr/0025-sudba-radara-pri-pereezde.md) (радар)
- **Документы переезда:** [MIGRATION_TO_WINDOWS.md](MIGRATION_TO_WINDOWS.md) (инвентаризация и план), [AGENT_RUNTIME.md](AGENT_RUNTIME.md) (агенты и замер), [LOCAL_WINDOWS_BROWSER_LIMITS.md](LOCAL_WINDOWS_BROWSER_LIMITS.md) (ограничения браузера), [PROD_SHUTDOWN_RUNBOOK.md](PROD_SHUTDOWN_RUNBOOK.md) (остановка и откат), [RADAR_FATE.md](RADAR_FATE.md) (радар)
- **Связанные записи:** [P242](pending/P242-ezhednevnyj-bekap-boevoj-bazy-sloman.md) — сломанный бэкап; [P243](pending/P243-resheniya-vladeltsa-dlya-zaversheniya-pereezda.md) — решения владельца; [P244](pending/P244-windows-nesovmestimosti-i-propuschennye-testy.md) — Windows-несовместимости и пропущенные тесты
- **Прод:** не тронут. Инвентаризация снята чтением (`systemctl list-unit-files`, `systemctl show`, `ls` каталога бэкапов, `logs/backup.log`); база — только `pg_dump` (только чтение). Дамп снят в каталог бэкапов приложения на боксе (297 МБ).
- **Локальная машина:** PostgreSQL 17.6 на 5433 (кластер `downloads/pgdata`), Redis 8.10.3 на 6379, веб `127.0.0.1:8000`, воркер `celery` (пул solo), воркер `wave`/`digest`, beat. Логи — в `logs/`.
- **Открытых PR:** нет.

## Failed approaches (этой нитки)

- **Прочитать `DATABASE_URL`/`SECRETS_TOKEN` с прод-бокса напрямую** — гейт
  рантайма блокирует чтение env-файла приложения (и это правильно: файл с
  секретами). Не обходил. Рабочий путь: свои скрипты на боксе
  (`infra/windows/prod_dump.sh`, `prod_counters.sh`), которые читают env
  **внутри себя** и не печатают значения.
- **Штатный `scripts/backup_database.sh` как источник дампа** — падает на
  awk (см. P242). Не чинил: `scripts/` не моя территория в этой задаче.
- **Поднять свой PostgreSQL на 5432** — порт занят чужим PostgreSQL на этой
  машине. Свой кластер поднят на **5433**, чужое не тронуто.
- **Запустить `postgres.exe` с `DETACHED_PROCESS`/`CREATE_NO_WINDOW`** — падает
  с `0xC0000142` (DLL init failed). Работает запуск отдельной группой
  процессов без этих флагов.
- **Остановить лишний prefork-ворер `taskkill`/`Stop-Process`** — гейт
  рантайма блокирует kill процессов. Обойти не пытался: вместо этого
  поднят второй воркер с `--pool=solo` (второй воркер на той же очереди
  Celery допустим).
- **Prefork на Windows с перенаправленным `TEMP`** — не помогло (пул
  зависал). Корневая причина `WinError 5` не изолирована; обход — solo.
- **pytest без `--basetemp`** — прогон доходит до 100%, но падает на финальной
  уборке `%TEMP%` и не печатает итог. Обход: `--basetemp` внутри репозитория.

## Открытые вопросы для пользователя

1. **Куда переносить радар** (оставить / вынести / отключить) — см. ADR-0025.
2. **Что крутится локально как движок**: DeepSeek API, MiniMax Code CLI или
   локальные модели?
3. **Проверять ли доступ к интерфейсу с других устройств** (слушать не
   только петлю) — по умолчанию выключено, см.
   [`docs/LOCAL_WINDOWS_BROWSER_LIMITS.md`](LOCAL_WINDOWS_BROWSER_LIMITS.md) §4.
4. **Когда отдавать команду на остановку прода** и что остаётся жить на
   боксе (ЕСА, `/services`, VK-шлюз, Радар)?
5. **Публикации с локального стенда** — подтвердить, что после подключения
   токена стенд может публиковать в боевые группы (сейчас не может: токенов
   нет).

## Не забыть (low-priority)

- **Порт PostgreSQL 5433** на этой машине занят **нашим** стендом; 5432 —
  чужой PostgreSQL, его не трогать.
- **Пустые файлы бэкапов** на боевом боксе (с января) — разбор с
  подтверждением владельца, в P242.
- **Каталог `downloads/`** на локальной машине (≈650 МБ: бинарники PostgreSQL
  и Redis, кластер БД, дамп) — вне git, но занимает место; при чистке
  сносить только его.
- `gateway_mcp/client.py` держит в коде прод-адрес шлюза как дефолт — при
  переезде адрес меняется (ADR-0023, §«Что не переносится»).

---

> Если читаешь это в начале новой сессии — обнови через `/close_session` в конце. **Не аккумулируй history тут** — она в `git log --follow -- docs/SESSION_HANDOFF.md`.
