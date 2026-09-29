# START HERE (AI / новая сессия)

Если что-то в документации расходится с кодом — приоритет у кода и реальных конфигов сервера.

---

## Удалённый доступ к продакшену (важно для AI)

**Только SSH** к хосту САРАФАН (`~/САРАФАН`): shell, git, systemd, логи, деплой. MCP-серверы в IDE для этого проекта **не используются** (см. **[`REMOTE_ACCESS.md`](REMOTE_ACCESS.md)**).

---

## ⚠️ ОБЯЗАТЕЛЬНО: Начало каждой сессии разработки

**Перед любой работой AI должен синхронизироваться:**

```bash
cd ~/САРАФАН
git status
git fetch origin
git pull origin $(git branch --show-current)
git log --oneline -5
```

Это гарантирует актуальность кода и предотвращает конфликты.

---

## 0) VPS и окружение

- ОС: Ubuntu 24.04.3 LTS
- Проект: `~/САРАФАН` (в удалённом шелле разворачивается сам)
- Переменные окружения: env-файл приложения на боксе, `600 root:root` — секреты только там, не коммитить
- Systemd-сервисы: `sarafan`, `sarafan-celery-worker`, `sarafan-celery-beat`, `sarafan-vk-bot` (демон ВК-бота САРАФАНа)
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
systemctl status sarafan sarafan-celery-worker sarafan-celery-beat sarafan-vk-bot
curl http://127.0.0.1:8000/api/health/
```

Swagger: `http://127.0.0.1:8000/docs`

## 3) Production: перезапуск сервисов

```bash
sudo systemctl restart sarafan sarafan-celery-worker sarafan-celery-beat sarafan-vk-bot
```

Логи: `~/САРАФАН/logs/` (есть logrotate).

## 4) Dev/ручной запуск

FastAPI:

```bash
cd ~/САРАФАН
source venv/bin/activate
python main.py
```

Celery:

```bash
cd ~/САРАФАН
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
cd ~/САРАФАН
source venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart sarafan sarafan-celery-worker sarafan-celery-beat sarafan-vk-bot
```

Если менялся только код — достаточно перезапуска сервисов.

## 6) Тестирование (НОВОЕ!)

Проект использует pytest для unit-тестирования. **Все новые модули должны быть покрыты тестами.**

```bash
# Запуск всех unit-тестов
cd ~/САРАФАН
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
- Nginx: редактировать `config/sarafan.conf.editable`, применять `scripts/apply_nginx_config.sh`

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

*Последнее обновление: 2026-09-29*
