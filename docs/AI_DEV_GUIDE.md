# 📘 САРАФАН — Документация для AI-разработчиков

## 🎯 Назначение проекта

**САРАФАН** — автоматизированная система парсинга, фильтрации, анализа и постинга контента из ВКонтакте для сети региональных новостных пабликов. Включает AI-анализ (DeepSeek, D-024), дедупликацию, агрегацию, планирование публикаций и мониторинг.

**Удалённый продакшен:** только **SSH** на хост САРАФАН. MCP не использовать для деплоя и отладки САРАФАН. Подробно: [`REMOTE_ACCESS.md`](REMOTE_ACCESS.md).

---

## 🏗 Архитектура проекта

### Структура директорий

```
~/САРАФАН/
├── main.py                     # FastAPI приложение (lifespan, роуты, middleware)
├── celery_app.py               # Точка входа Celery (re-export из tasks/)
├── config/                     # Конфигурация
│   ├── runtime.py              # Runtime config (env vars, секреты НЕ в git!)
│   ├── celery_config.py        # Celery config
│   └── sarafan.conf.editable   # Nginx config template
├── database/                   # БД слой
│   ├── connection.py           # Async SQLAlchemy engine, session
│   ├── models.py               # SQLAlchemy models (Region, Community, Post, Filter...)
│   └── migrations/             # Alembic миграции
├── modules/                    # Бизнес-логика
│   ├── vk_monitor/             # Мониторинг VK (client, advanced_parser, carousel)
│   ├── filters/                # Модульная система фильтрации (11 типов фильтров)
│   ├── deduplication/          # Дедупликация (detector, fingerprints)
│   ├── aggregation/            # Агрегация контента (aggregator, content mixer)
│   ├── ai_analyzer/            # Sentiment (keyword-based, без нейросети)
│   ├── publisher/              # Публикация (VK, Telegram, WordPress)
│   ├── scheduler/              # Планировщик контента
│   ├── notifications/          # Система уведомлений (VK checkers, storage)
│   └── monitoring/             # Health checker, Telegram notifier
├── tasks/                      # Celery задачи и расписания
│   ├── celery_app.py           # Celery app + beat_schedule (большинство задач живёт здесь)
│   ├── parsing_tasks.py        # Parsing tasks
│   ├── parsing_scheduler_tasks.py  # Postopus scheduling (27 тем)
│   ├── discovery_tasks.py      # Подбор сообществ + еженедельная перепроверка
│   ├── radar_tasks.py          # Контент-радар: fan-out опрос источников
│   └── broadcast_tasks.py      # Сетевая рассылка: диспетчер-публикатор
├── web/                        # Web слой
│   ├── api/                    # REST API endpoints (17 роутеров)
│   ├── static/                 # Static files (CSS, JS)
│   └── templates/              # Jinja2 templates (UI страницы)
├── core/                       # Core exceptions
├── utils/                      # Утилиты (cache, retry, text_utils, post_utils...)
├── middleware/                 # ASGI middleware (metrics, rate_limiter)
├── monitoring/                 # Prometheus metrics
├── scripts/                    # Админ-скрипты и тест-скрипты
├── logs/                       # Логи приложения (logrotate настроен)
├── docs/                       # Документация
├── tests/                      # Unit-тесты (pytest)
└── config/sarafan.env.example  # Шаблон переменных окружения
```

### Ключевые компоненты

| Модуль | Описание |
|--------|----------|
| `main.py` | FastAPI приложение, lifespan, 17 API роутеров, Jinja2 UI |
| `modules/vk_monitor/advanced_parser.py` | Основной парсинг VK постов |
| `modules/filters/` | Пайплайн фильтрации (blacklist, ads, age, photo dedup...) |
| `modules/aggregation/` | Агрегация и кластеризация контента |
| `modules/publisher/` | Публикация в VK, Telegram, WordPress |
| `modules/ai_analyzer/` | Sentiment постов — keyword-based, нейросети здесь нет |
| `modules/deduplication/` | LIP + media fingerprint дедупликация |
| `modules/scheduler/` | Smart scheduler для оптимального времени публикаций |
| `tasks/celery_app.py` | Celery app + beat schedule (27 Postopus тем + САРАФАН задачи) |
| `database/models.py` | SQLAlchemy модели: Region, Community, Post, Filter, VKToken, PublishSchedule |
| `config/runtime.py` | Все конфиги из env vars, НИКАКИХ секретов в коде |

### Потоки данных

**Контент-пайплайн:**

```
VK API → vk_monitor → PostgreSQL posts → filters → scoring → aggregator → publisher → VK/Telegram
```

**Уведомления:**

```
Celery Beat → notification_tasks → notifications checkers → Redis → Telegram Bot
```

---

## 🔐 Безопасность и секреты

### ⚠️ КРИТИЧЕСКИ ВАЖНО

1. **Секреты ТОЛЬКО в env vars** — env-файл приложения на прод-боксе
2. **НИКОГДА не коммить** токены, пароли, ключи в репозиторий. Рядом действует второй непубликуемый класс — **инфра-детали** (хостнеймы, пути на сервере, состав жильцов бокса): полное правило и его границы — [`AGENTS.md` §«Что не публикуется»](../AGENTS.md#что-не-публикуется-секреты-и-инфра-детали), здесь копии нет намеренно (ADR-0011)
3. **VK токены** — собирать через `VK_TOKEN_<NAME>` префикс (см. `config/runtime.py`)
4. **Rate limits VK** — ~3 запроса/сек на токен, exponential backoff при 429
5. **Разделение токенов**:
   - `VK_TOKEN_VALSTAN` — основной (чтение + постинг)
   - `VK_TOKEN_VITA` и другие — только чтение

### Пример `.env` (не коммитить!)

```env
DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/sarafan
REDIS_URL=redis://localhost:6379/0
VK_TOKEN_VALSTAN=vk1.a....
VK_TOKEN_VITA=vk1.a....
DEEPSEEK_API_KEY=sk-...
TELEGRAM_TOKEN_VALSTANBOT=...
TELEGRAM_ALERT_CHAT_ID=-100...
VK_TEST_GROUP_ID=-137760500
```

### Загрузка из env

```python
from config.runtime import VK_TOKENS, TELEGRAM_TOKENS
from config.deepseek import get_api_key  # ключ читается при КАЖДОМ вызове,
                                         # чтобы ротация в КАРМАНе не требовала рестарта
# Всё уже распаршено из env vars, НИЧЕГО не хардкодить!
```

---

## 🧪 Тестирование

### Требования к тестам

1. **Все новые модули** должны быть покрыты тестами (pytest)
2. **Критические пути** — фильтры, дедупликация, публикация — обязательны к покрытию
3. **Unit-тесты** — с моками, без БД и внешних API, для CI/CD
4. **Интеграционные тесты** — отдельно, требуют реального окружения
5. **Запуск перед коммитом**: `pytest tests/ -q`

### Структура тестов

```
САРАФАН/
├── tests/                          # Unit-тесты (pytest)
│   ├── conftest.py                 # Фикстуры и моки
│   ├── test_filters/               # Тесты фильтров
│   ├── test_deduplication/         # Тесты дедупликации
│   ├── test_publisher/             # Тесты паблишера
│   ├── test_vk_monitor/            # Тесты VK монитора
│   ├── test_aggregation/           # Тесты агрегации
│   ├── test_api/                   # Тесты API endpoints
│   └── test_config/                # Тесты конфигурации
├── scripts/test_*.py               # Ручные интеграционные скрипты
└── pytest.ini                      # Конфигурация pytest
```

### Быстрый запуск тестов

```bash
# Все unit-тесты
pytest tests/ -v

# С покрытием
pytest tests/ --cov=modules --cov=tasks

# Конкретный модуль
pytest tests/test_filters/ -v

# Только быстрые тесты (маркировка)
pytest tests/ -m "not slow"
```

Подробно: [TESTING.md](TESTING.md)

---

## 📝 Типизация и стандарты кода

### Требования к коду

1. **Type hints**: Все функции должны иметь аннотации типов
2. **Data classes**: Использовать `@dataclass` / `pydantic` для структур данных
3. **Docstrings**: Краткое описание назначения функции
4. **Именование**: snake_case для функций/переменных, PascalCase для классов

### Пример

```python
from dataclasses import dataclass
from typing import Optional

@dataclass
class Post:
    id: int
    text: str
    owner_id: int
    views: Optional[int] = None

def process_post(post: Post) -> bool:
    """Process a single post through the filter pipeline."""
    if not isinstance(post.id, int):
        raise TypeError("post.id must be int")
    # ...
```

### Форматирование и линтинг

Проект использует pre-commit хуки:
- **black** — форматирование кода
- **isort** — сортировка импортов
- **flake8** — линтинг

```bash
# Запуск вручную
pre-commit run --all-files
```

---

## 🤖 Self-review для AI

### Чек-лист перед генерацией кода

Перед выдачей решения AI должен:

1. ✅ **Самопроверка**: "Есть ли в этом коде потенциальные баги?"
2. ✅ **Краевые случаи**: "Что если входные данные пустые/null?"
3. ✅ **Оптимизация**: "Можно ли сделать код эффективнее?"
4. ✅ **Безопасность**: "Не утекают ли секреты? Нет ли injection?"
5. ✅ **Тестируемость**: "Можно ли легко написать тесты для этого кода?"
6. ✅ **Согласованность**: "Соответствует ли код стилю проекта?"
7. ✅ **Секреты**: "Не захардкожены ли токены/пароли?"
8. ✅ **Документация**: "Нужно ли обновить документацию?"

### Дискуссия с собой

```
AI: "Предлагаю решение X..."
AI: "Но подожди, что если VK API вернет ошибку? Нужно добавить try-except"
AI: "Также нужно учесть rate limiting..."
AI: "Финальное решение: X с обработкой ошибок и кэшированием"
```

---

## 🔄 Рабочий процесс разработки

### При добавлении функциональности

1. Изучить существующую архитектуру (`modules/`, `tasks/`, `web/api/`)
2. Создать модуль с типизацией и валидацией
3. Написать тесты (обязательно!)
4. Проверить self-review (см. чек-лист выше)
5. Обновить документацию если нужно
6. Описательный commit message (Conventional Commits + body — заменяет упразднённую DEV_HISTORY, см. [ADR-0001](adr/0001-archive-dev-history.md))

### При рефакторинге

1. Убедиться что тесты проходят
2. Сохранить обратную совместимость
3. Актуализировать тесты под изменения
4. Описательный commit message с обоснованием рефакторинга

---

## 🚀 Синхронизация с GitHub

### ⚠️ ОБЯЗАТЕЛЬНАЯ ПРОЦЕДУРА В НАЧАЛЕ СЕССИИ

**В начале каждой сессии AI должен:**

1. **Проверить текущую ветку и статус:**
   ```bash
   cd ~/САРАФАН && git status && git branch -a
   ```

2. **Синхронизироваться:**
   ```bash
   git fetch origin
   git pull origin <текущая_ветка>
   ```

3. **Убедиться что код актуален:**
   ```bash
   git log --oneline -5
   ```

4. **После изменений (только по запросу пользователя):**
   ```bash
   git add .
   git commit -m "Описание изменений"
   git push origin <ветка>
   ```

### ⚠️ AI НЕ ДОЛЖЕН:
- Генерировать новые SSH-ключи
- Менять `~/.ssh/config`
- Трогать настройки аутентификации

---

## 📊 Технологический стек

| Компонент | Технология |
|-----------|------------|
| **Язык** | Python 3.12+ |
| **Web框架** | FastAPI 0.118 + Uvicorn |
| **ORM** | SQLAlchemy 2.0 (async) |
| **БД** | PostgreSQL 17.6 |
| **Cache/Broker** | Redis 7.4 |
| **Task Queue** | Celery 5.5 + Beat |
| **AI** | DeepSeek API (`modules/deepseek_client.py`, D-024) |
| **VK API** | vk-api 11.10 |
| **Telegram** | python-telegram-bot 22.5 |
| **Monitoring** | Prometheus + Grafana |
| **Proxy** | Nginx |
| **Testing** | pytest |
| **CI/CD** | GitHub Actions |
| **Code Quality** | black, isort, flake8, pre-commit |

---

## 🌍 Регионы и темы

### Регионы

Регионы организованы в трёхуровневую иерархию `strana → oblast → raion` (миграция 015, 2026-05-27). Полная картина — [`docs/REGIONS_HIERARCHY.md`](REGIONS_HIERARCHY.md). Кратко:

- **`raion`** (район) — низший уровень, источники = `communities`. Коды: `mi`, `klz`, `vp`, `ur`, `kukmor`, `bal`, `leb`, `nolinsk`, `nema`, `sovetsk`, `pizhanka`, `arbazh`, `tuzha`, `verhoshizhem`.
- **`oblast`** (область) — содержит районы, источники = главные сообщества детей (5 свежих постов). Код: `kirov_obl`.
- **`strana`** (страна) — верх иерархии, источники = главные сообщества областей.

Поля: `Region.kind` (`raion`/`oblast`/`strana`), `Region.parent_region_id` (FK self-ref). Каскадная сводка для `oblast`/`strana` — `modules/cascaded_bulletin.py`. Полный список регионов — в таблице `regions` в БД.

### Темы постов (Postopus migration)

| Тема | Описание | Расписание |
|------|----------|------------|
| `novost` | Новости | 6 раз/день |
| `reklama` | Объявления | 3 раза/день |
| ~~`sosed`~~ | Снята 2026-09-14 — за 30 дней ноль сводок при 4109 собранных постов; соседский обмен несёт `bulletin-share-neighbors-daily` | — |
| `kultura` | Культура | 5 раз/день |
| `sport` | Спорт | 2 раза/день |
| `admin` | Административное | 3 раза/день |
| `union` | Союз | 2 раза/день |
| `detsad` | Детский сад | 1 раз/день |
| `addons` | Дополнительные темы | 4 раза/день |
| `copy_setka` | Копирование Setka | Каждый час |

Расписания: `tasks/celery_app.py` → `app.conf.beat_schedule`

---

## 🚫 Антипаттерны (избегать!)

- ❌ Хардкод токенов/секретов в коде
- ❌ Отсутствие обработки ошибок VK API
- ❌ Игнорирование rate limits
- ❌ Код без тестов
- ❌ Функции без type hints
- ❌ Глобальные переменные без необходимости
- ❌ Дублирование логики между модулями
- ❌ Изменение кода без описательного commit message (с 2026-05-24 commit-message + PR description заменяют упразднённую DEV_HISTORY, см. [ADR-0001](adr/0001-archive-dev-history.md))
- ❌ Прямые SQL запросы без ORM (кроме миграций)
- ❌ Блокирующие вызовы в async функциях

---

## 📋 Чек-лист завершения задачи

Перед завершением любой задачи AI должен проверить:

- [ ] Код соответствует требованиям типизации
- [ ] Написаны/обновлены тесты для новых/изменённых модулей
- [ ] Секреты не захардкожены, используются env vars
- [ ] Обращения к VK API оптимизированы
- [ ] Проведён self-review кода
- [ ] **Описательный commit message** (Conventional Commits + body с тестами и применением; см. [ADR-0001](adr/0001-archive-dev-history.md))
- [ ] **Актуализирована документация** (удалены устаревшие сведения)
- [ ] `pytest tests/ -q` проходит
- [ ] `pre-commit run --all-files` проходит

---

## 📚 Карта документации

| Файл | Описание |
|------|----------|
| [`START_HERE.md`](START_HERE.md) | Быстрый старт, команды, сервисы |
| [`REMOTE_ACCESS.md`](REMOTE_ACCESS.md) | Только SSH на хост САРАФАН |
| [`AI_DEV_GUIDE.md`](AI_DEV_GUIDE.md) | Полное руководство для AI-разработчиков (этот файл) |
| [`TESTING.md`](TESTING.md) | Тестирование: unit, интеграция, CI/CD |
| [`adr/`](adr/) | Architectural Decision Records (см. [ADR-0001](adr/0001-archive-dev-history.md) — DEV_HISTORY упразднена 2026-05-24; история — `git log` + `gh pr view`) |
| [`paths.md`](paths.md) | Архитектура, API endpoints, потоки данных |
| [`OPERATIONS.md`](OPERATIONS.md) | Эксплуатация, runbook, troubleshooting |
| [`DEPLOY.md`](DEPLOY.md) | Deployment guide |
| [`MIGRATION_GUIDE.md`](MIGRATION_GUIDE.md) | Миграция old_postopus → САРАФАН |

---

*Документ создан на основе лучших практик Postopus (old_postopus) и адаптирован для САРАФАН.
При каждой новой сессии начинать с изучения этого документа и синхронизации с git.*
