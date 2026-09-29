# 🌐 САРАФАН - Система менеджмента мультимедиа для новостных ресурсов

**Версия:** 1.0.0-beta  
**Статус:** ✅ Production-ready (90% готовности)  
**Домен:** https://сарафан.вмалмыже.рф

---

## 🚀 ДЛЯ AI АССИСТЕНТА: НАЧНИ ЗДЕСЬ!

**Если ты AI помощник в новой сессии, начни с документации:**

- `docs/ai/START_HERE.md`
- `docs/README.md`

---

---

## 📋 Описание

САРАФАН - автоматизированная система для управления новостным контентом из социальных сетей (VK, Telegram, WordPress) с AI-анализом для 50 региональных новостных каналов.

### Ключевые возможности:
- 🤖 **AI-анализ контента** (DeepSeek API, D-024; sentiment — keyword-based, без нейросети)
- 📡 **Мониторинг 1000+ сообществ VK**
- 📤 **Автоматическая публикация** по расписанию
- 📊 **Статистика и аналитика** постов
- 🔔 **Telegram уведомления** об ошибках
- 🔒 **SSL/HTTPS** защита
- 💾 **Автоматические бэкапы**

---

## 🏗️ Архитектура

### Backend:
- **Python 3.12** + **FastAPI**
- **PostgreSQL 17.6** (основная БД)
- **Redis 7.4.1** (кеш + очереди)
- **Celery** (фоновые задачи)

### Модули:
```
modules/
├── vk_monitor/      ✅ Мониторинг VK сообществ
├── ai_analyzer/     ✅ Sentiment постов (keyword-based, русский)
├── monitoring/      ✅ Система мониторинга + Telegram
├── publisher/       ⏳ Публикация контента
├── telegram_bot/    ⏳ Telegram интеграция
└── scheduler/       ⏳ Планировщик задач
```

### База данных:
- **regions** - регионы/районы (14)
- **communities** - сообщества VK (2+)
- **posts** - посты из VK (10+)
- **filters** - фильтры контента (11)
- **vk_tokens** - токены VK (3)
- **publish_schedules** - расписания публикаций

---

## 🚀 Быстрый старт

### Активация окружения:
```bash
cd ~/САРАФАН
source venv/bin/activate
```

### Запуск приложения:
```bash
# Development mode
python main.py

# Production mode
uvicorn main:app --host 127.0.0.1 --port 8000 --workers 1
```

### Тестирование модулей:
```bash
# VK Monitor
python scripts/test_vk_monitor.py

# Health Check
python scripts/test_monitoring.py

# AI Analyzer
python scripts/test_ai_analyzer.py  # (создать)
```

---

## 📡 API Endpoints

**Base URL:** `https://сарафан.вмалмыже.рф`

### Health:
- `GET /api/health/` - Quick health check
- `GET /api/health/full` - Full system health check

### Regions:
- `GET /api/regions/` - List all regions
- `GET /api/regions/{code}` - Get region by code

### Communities:
- `GET /api/communities/` - List communities
- `GET /api/communities/{id}` - Get community by ID

### Posts:
- `GET /api/posts/` - List posts
- `GET /api/posts/{id}` - Get post by ID

**Документация:** https://сарафан.вмалмыже.рф/docs

---

## 🔧 Конфигурация

### База данных:
```bash
Database: sarafan
User: sarafan_user
Host: localhost:5432
```

### VK Tokens:
Хранятся в env (`VK_TOKEN_*`) и/или в таблице `vk_tokens` (см. `docs/features/token_management.md`)

### Telegram Bots:
Настраиваются через env (`TELEGRAM_TOKEN_*`, `TELEGRAM_ALERT_CHAT_ID`) — см. `docs/ops/configuration.md`

---

## 🗂️ Структура проекта

```
САРАФАН/
├── main.py              # FastAPI приложение
├── config/              # Конфигурация (токены)
├── database/            # БД модели и подключение
├── modules/             # Модули системы
│   ├── vk_monitor/      # Мониторинг VK
│   ├── ai_analyzer/     # AI анализ
│   ├── monitoring/      # Система мониторинга
│   └── ...
├── web/                 # Web API
│   └── api/             # API endpoints
├── scripts/             # Утилиты и скрипты
├── docs/                # Документация
├── backup/              # Бэкапы БД
├── logs/                # Логи
└── venv/                # Python окружение
```

---

## 📊 Текущий статус

**Завершено:**
- ✅ Инфраструктура и БД
- ✅ VK мониторинг модуль
- ✅ Нейро-классификация (DeepSeek API, `modules/deepseek_client.py`)
- ✅ Система мониторинга
- ✅ Автоматические бэкапы
- ✅ SSL сертификат
- ✅ FastAPI backend

**Завершено дополнительно:**
- ✅ Publisher модуль (публикация в VK/Telegram/WordPress)
- ✅ Планировщик задач (Smart Scheduler)
- ✅ Web интерфейс (Bootstrap 5)
- ✅ Полная интеграция всех модулей
- ✅ Celery автоматизация
- ✅ Система уведомлений

**Прогресс:** ~95% (production-ready)

---

## 💾 Бэкапы

**Автоматический бэкап:** Ежедневно в 3:00 AM  
**Расположение:** `~/САРАФАН/backup/`  
**Хранение:** Последние 7 бэкапов

Ручной бэкап:
```bash
~/САРАФАН/scripts/backup_database.sh
```

---

## 🔔 Мониторинг

**Health checks:** Каждые 5 минут  
**Telegram алерты:** При ошибках и предупреждениях  
**Логи:** `~/САРАФАН/logs/`

---

## 📚 Документация

Единый вход в документацию: `docs/README.md`

Для AI/новой сессии: `docs/ai/START_HERE.md`

Ops/runbook:
- `docs/ops/configuration.md`
- `docs/ops/run_local.md`
- `docs/ops/nginx.md`
- `docs/ops/monitoring.md`
- `docs/ops/troubleshooting.md`

Фичи:
- `docs/features/*`

---

## 🛠️ Полезные команды

```bash
# Проверить статус БД
sudo -u postgres psql -d sarafan -c "\dt"

# Просмотреть регионы
sudo -u postgres psql -d sarafan -c "SELECT code, name FROM regions;"

# Просмотреть посты
sudo -u postgres psql -d sarafan -c "SELECT COUNT(*) FROM posts;"

# Логи приложения (uvicorn stdout/stderr + Python logging — systemd
# редиректит туда же)
tail -f logs/uvicorn_production.log

# Бэкап
scripts/backup_database.sh
```

---

## 📈 Статистика

**Размер проекта:** ~200 MB  
**Python файлов:** 100+  
**API endpoints:** 20+  
**Регионов в БД:** 14  
**Постов собрано:** 1000+  
**Сообществ VK:** 100+  

---

## 🔗 Ссылки

- **API Docs:** https://сарафан.вмалмыже.рф/docs
- **Старый проект:** https://github.com/Valstan/postopus
- **DeepSeek Platform:** https://platform.deepseek.com

---

**Разработка:** Valstan  
**Начало:** 8 октября 2025

