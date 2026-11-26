# START HERE (AI / новая сессия)

Если что-то в документации расходится с кодом — приоритет у кода и реальных конфигов сервера.

**Два окружения.** Боевой контур **жив** (Ubuntu, systemd, SSH-доступ) — и
параллельно идёт перенос на локальную машину владельца (Windows). Разделы
0–9 ниже — про прод; **раздел 10 — про локальный стенд**. Полный план
переезда — [`MIGRATION_TO_WINDOWS.md`](MIGRATION_TO_WINDOWS.md).

---

## Удалённый доступ к продакшену (важно для AI)

**Только SSH** к хосту САРАФАН (`~/SETKA`): shell, git, systemd, логи, деплой. MCP-серверы в IDE для этого проекта **не используются** (см. **[`REMOTE_ACCESS.md`](REMOTE_ACCESS.md)**).

---

## ⚠️ ОБЯЗАТЕЛЬНО: Начало каждой сессии разработки

**Перед любой работой AI должен синхронизироваться:**

```bash
cd ~/SETKA
git status
git fetch origin
git pull origin $(git branch --show-current)
git log --oneline -5
```

Это гарантирует актуальность кода и предотвращает конфликты.

---

## 0) VPS и окружение

- ОС: Ubuntu 24.04.3 LTS
- Проект: `~/SETKA` (в удалённом шелле разворачивается сам)
- Переменные окружения: env-файл приложения на боксе, `600 root:root` — секреты только там, не коммитить
- Systemd-сервисы: `setka`, `setka-celery-worker`, `setka-celery-beat`, `setka-vk-bot` (демон ВК-бота САРАФАНа)
- FastAPI слушает `127.0.0.1:8000`, наружу проксирует Nginx

## 1) Источники истины в коде

- `main.py` — FastAPI приложение, подключение роутов, `/metrics`
- `web/api/*` — фактические API endpoints
- `tasks/celery_app.py` — расписания Celery (beat)
- `tasks/*.py` — фоновые задачи
- `database/models.py` — структура БД
- `database/connection.py`, `config/runtime.py` — конфиг и env

## 2) Быстрая проверка живости

```bash
systemctl status setka setka-celery-worker setka-celery-beat setka-vk-bot
curl http://127.0.0.1:8000/api/health/
```

Swagger: `http://127.0.0.1:8000/docs`

## 3) Production: перезапуск сервисов

```bash
sudo systemctl restart setka setka-celery-worker setka-celery-beat setka-vk-bot
```

Логи: `~/SETKA/logs/` (есть logrotate).

## 4) Dev/ручной запуск

FastAPI:

```bash
cd ~/SETKA
source venv/bin/activate
python main.py
```

Celery:

```bash
cd ~/SETKA
source venv/bin/activate
./scripts/start_celery.sh
```

Остановка:

```bash
./scripts/stop_celery.sh
```

## 5) "Ребилд" backend и модулей

Если менялись зависимости или окружение:

```bash
cd ~/SETKA
source venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart setka setka-celery-worker setka-celery-beat setka-vk-bot
```

Если менялся только код — достаточно перезапуска сервисов.

## 6) Тестирование (НОВОЕ!)

Проект использует pytest для unit-тестирования. **Все новые модули должны быть покрыты тестами.**

```bash
# Запуск всех unit-тестов
cd ~/SETKA
source venv/bin/activate
pytest tests/ -v

# С покрытием
pytest tests/ --cov=modules --cov=tasks

# Конкретный модуль
pytest tests/test_filters/ -v
```

Подробнее: [`docs/TESTING.md`](TESTING.md)

### Pre-commit хуки

```bash
# Установка
pip install pre-commit
pre-commit install

# Запуск вручную
pre-commit run --all-files
```

## 7) Рабочий процесс разработки

### При добавлении функциональности:
1. Изучить существующую архитектуру (`modules/`, `tasks/`, `web/api/`)
2. Создать модуль с типизацией и валидацией
3. **Написать тесты (обязательно!)**
4. Проверить self-review чек-лист (см. [`AI_DEV_GUIDE.md`](AI_DEV_GUIDE.md))
5. Обновить документацию если нужно
6. **Описательный commit message** (Conventional Commits — `feat(scope): ...` + тело с контекстом, тестами, применением на проде) — заменяет старую запись в DEV_HISTORY (см. [ADR-0001](adr/0001-archive-dev-history.md)).
7. Запустить тесты: `pytest tests/ -q`
8. Запустить pre-commit: `pre-commit run --all-files`

### При рефакторинге:
1. Убедиться что тесты проходят
2. Сохранить обратную совместимость
3. Актуализировать тесты под изменения
4. **Описательный commit message** — что было сломано, как починили, какие тесты прошли (Conventional Commits + body).

## 8) Быстрые ориентиры

- UI: `/`, `/regions`, `/posts`, `/communities`, `/notifications`, `/tokens`, `/publisher`, `/monitoring`, `/schedule`
- Метрики: `/metrics`
- Nginx: `config/nginx_setka.conf` (живой конфиг техдомена), применять `scripts/apply_nginx_config.sh`

## 9) Чек-лист завершения задачи

Перед коммитом AI должен проверить:

- [ ] Код соответствует требованиям типизации
- [ ] Написаны/обновлены тесты для новых/изменённых модулей
- [ ] `pytest tests/ -q` проходит
- [ ] `pre-commit run --all-files` проходит
- [ ] Секреты не захардкожены
- [ ] **Описательный commit message** (Conventional Commits + body с тестами и применением; см. [ADR-0001](adr/0001-archive-dev-history.md))
- [ ] Синхронизация с git: `git pull` → `git add` → `git commit` → `git push`

---

## 10) Локальный стенд (Windows)

**Назначение.** Перенос боевого контура на машину владельца
([ADR-0023](adr/0023-pereezd-boevogo-kontura-na-lokalnuyu-mashinu.md)). Прод
при этом **не остановлен** и остаётся страховкой до отдельной команды
владельца.

### Состав

| Компонент | Что | Где |
|---|---|---|
| Python | 3.12.10 + venv | `venv/` |
| PostgreSQL | portable-сборка 17.6, порт **5433** | `downloads/pgsql`, кластер `downloads/pgdata` |
| Redis | сборка для Windows 8.10.3, порт 6379 | `downloads/Redis-*` |
| FastAPI | uvicorn, `127.0.0.1:8000` | `infra/windows/start-web.ps1` |
| Celery | worker (`celery`), worker (`wave`,`digest`), beat | `infra/windows/start-*.ps1` |
| env-файл | машинный, в git не едет | `config/local-stand.env` |

> Порт PostgreSQL **5433** — не ошибка: на машине уже был чужой PostgreSQL на
> 5432, и свой кластер поднят на свободном порту, чтобы его не трогать.

### Запуск

```powershell
powershell -ExecutionPolicy Bypass -File infra\windows\start-web.ps1
powershell -ExecutionPolicy Bypass -File infra\windows\start-worker.ps1
powershell -ExecutionPolicy Bypass -File infra\windows\start-wave-worker.ps1
powershell -ExecutionPolicy Bypass -File infra\windows\start-beat.ps1

# круглосуточно: задачи планировщика Windows (по одной на юнит прода)
powershell -ExecutionPolicy Bypass -File infra\windows\register-scheduled-tasks.ps1
```

### Проверки

```powershell
Invoke-WebRequest http://127.0.0.1:8000/api/health/ -UseBasicParsing   # 200
Invoke-WebRequest http://127.0.0.1:8000/metrics     -UseBasicParsing   # 200
.\venv\Scripts\python.exe -m pytest tests\ -q --basetemp=temp\pytest_win
```

> На Windows pytest падает на финальной уборке временных каталогов
> (`PermissionError: [WinError 5]` в `%TEMP%`) — **не тесты проекта, а pytest
> на этой машине**. Обход: `--basetemp` внутри репозитория.

### Что нужно помнить

* **Пул Celery на Windows — `solo`.** Prefork падает с `WinError 5`
  (см. [`AGENT_RUNTIME.md`](AGENT_RUNTIME.md) §5).
* **env-файл читает сам процесс** (`SETKA_ENV_FILES`), потому что на Windows
  нет systemd'овского `EnvironmentFile=`.
* **Локальные `DATABASE_URL`/`REDIS_URL` заданы явно** — иначе bootstrap
  подхватил бы из комнаты КАРМАНа **боевые** адреса, и локальный процесс
  писал бы в продовую базу.
* **Секреты в репозитории нет** и не появится; ключи приезжают из комнаты
  КАРМАНа.

### Агентный слой

```powershell
.\venv\Scripts\python.exe -m gateway_mcp.bench --concurrency 1,4,8 --tasks 16 --latency 0.05
```

Каталог инструментов и профили агентов — в
[`AGENT_RUNTIME.md`](AGENT_RUNTIME.md).

### Локальный движок (MiniMax Code)

Движку MiniMax Code нужен пул аккаунтов (профиль CLI + токен-стор + состояние
сессии). На машине владельца он заводится один раз:

```powershell
.\venv\Scripts\python.exe infra\windows\setup_minimax_area.py
.\venv\Scripts\python.exe infra\windows\setup_minimax_area.py --check
```

Без этого движок отвечает `no_accounts` — то есть локальная машина не может
ни классифицировать посты, ни выполнять задания агентов. Подробности и
разбор про refresh-токен — в [`AGENT_RUNTIME.md`](AGENT_RUNTIME.md) §6.

Сверка локальных вердиктов с боевыми:

```powershell
$env:MINIMAX_ACCOUNTS_DIR = "$env:USERPROFILE\minimax-accounts"
$env:CLASSIFIER_ENGINE = "minimax"
$env:CLASSIFIER_PAID_FALLBACK = "0"
.\venv\Scripts\python.exe infra\windows\classifier_parity.py --limit 40 --env-file config\local-stand.env
```

Результат и разбор — в [`MIGRATION_TO_WINDOWS.md`](MIGRATION_TO_WINDOWS.md) §6.4.

---

*Последнее обновление: 2026-11-26*
