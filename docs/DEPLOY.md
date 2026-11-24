# 🚀 Deployment Guide — Postopus Migration

## ✅ Migration Complete

- **25 files** created/updated
- **+4,946 lines** of code
- **All syntax checks passed**
- **Committed and pushed** to GitHub (`main` branch, commit `a45583a`)

---

## 🏷️ Project Name

**SETKA** → **САРАФАН** (репозиторий на GitHub пока `setka`, проект называется САРАФАН)

## 📋 Pre-Deployment Checklist

### 1. Verify Environment Variables

```bash
# These must be set in /etc/sarafan/sarafan.env
cat /etc/sarafan/sarafan.env | grep -E "DATABASE_URL|MONGO_CLIENT|VK_TOKEN|REDIS"
```

Required:
- `DATABASE_URL` — PostgreSQL connection string
- `VK_TOKEN_*` — VK API tokens
- `MONGO_CLIENT` — MongoDB URI (for migration script only)

### 2. Verify Services Status

```bash
systemctl status setka setka-celery-worker setka-celery-beat setka-vk-bot
```

If services don't exist yet, see setup below.

---

## 🔧 Deployment Steps

### Step 1: Pull Latest Code

```bash
cd ~/SETKA
git pull origin main
```

### Step 2: Install Dependencies (if new packages needed)

```bash
source venv/bin/activate
pip install -r requirements.txt

# For MongoDB migration (one-time):
pip install pymongo
```

### Step 3: Run Database Migration (creates new tables)

The new tables will be **auto-created** on app startup (via `init_db()` → `Base.metadata.create_all`).

Tables added:
- `parsing_stats` — parsing run statistics
- `region_configs` — extended region configuration
- `work_tables` — lip/hash tracking (from MongoDB)
- `scheduled_publications` — planned publications

**If you have MongoDB data to migrate:**

```bash
source venv/bin/activate
python scripts/migrate_mongodb_config.py
```

This will:
- Read MongoDB config document
- Populate `regions`, `communities`, `filters` tables
- Create `region_configs` with zagolovki, heshteg, blacklists
- Migrate `work_tables` (lip, hash, bezfoto arrays)

### Step 4: Restart Services

```bash
# Main application
sudo systemctl restart setka

# Celery worker (processes tasks)
sudo systemctl restart setka-celery-worker

# Celery beat (schedules tasks — NOW WITH 27 NEW POSTOPUS TASKS)
sudo systemctl restart setka-celery-beat
```

#### ⚙️ Юнит воркера: очереди (P233, [ADR-0020](adr/0020-ochered-volny-i-periodika.md))

Воркер слушает **две** очереди: дефолтную `celery` (мелкая периодика) и `wave`
(веер волны публикации). Без `--queues` в ExecStart воркер слушает только
`celery`, и волны, ушедшие в `wave`, **останутся в очереди**.

```ini
ExecStart=/home/valstan/SETKA/venv/bin/celery -A celery_app worker \
    --loglevel=info --logfile=/home/valstan/SETKA/logs/celery-worker.log \
    --queues=celery,wave
```

**Порядок выката обязателен: сначала юнит, потом рестарт с новым кодом.**
Обратный порядок (новый код, старый юнит) останавливает волны: код отправляет
веер в `wave`, а слушает её только старый воркер, который её не слушает.

```bash
# 1) обновить ExecStart (см. выше) и перечитать юниты
sudo systemctl daemon-reload
# 2) выкатить код и перезапустить воркер (обычный шаг деплоя)
cd ~/SETKA && git pull origin main
sudo systemctl restart setka-celery-worker
# 3) убедиться, что обе очереди слушаются
./venv/bin/celery -A celery_app inspect active_queues
```

**Откат — одна строка, без отката кода:** вернуть веер в дефолтную очередь.

```bash
sudo sed -i 's/^SETKA_WAVE_QUEUE=.*/SETKA_WAVE_QUEUE=celery/' /etc/setka/setka.env
sudo systemctl restart setka-celery-worker
```

Юнит при этом остаётся `--queues=celery,wave`: лишняя очередь без задач просто
пуста, и волны возвращаются в общую очередь.

**Второй воркер — включён 24.11 (решение владельца).**

Настоящая изоляция «периодика не ждёт волну никогда» требует отдельного
процесса. Прежняя оценка «не влезает по памяти» считала второй воркер в
~280 МБ при свободных ~270 МБ в волну; **замер 24.11 23:00** показал:
родитель 80 МБ + ребёнок 119 МБ = **~200 МБ** на воркер, свободно **794 МБ**.
Место есть — вопрос снят.

Юнит: [`infra/celery/setka-celery-worker-wave.service`](../infra/celery/setka-celery-worker-wave.service).

```ini
[Unit]
Description=SETKA Celery Worker — второй слот (очереди волны и вечерней сводки)
After=network.target redis.service postgresql.service setka-celery-worker.service

[Service]
EnvironmentFile=-/etc/setka/setka.env
EnvironmentFile=/etc/setka/secrets-token.env
Type=simple
User=valstan
Group=valstan
WorkingDirectory=/home/valstan/SETKA
Environment="PATH=/home/valstan/SETKA/venv/bin:/usr/local/bin:/usr/bin:/bin"
Environment="PYTHONPATH=/home/valstan/SETKA"
ExecStart=/home/valstan/SETKA/venv/bin/celery -A celery_app worker \
    --loglevel=info --logfile=/home/valstan/SETKA/logs/celery-wave.log \
    --queues=wave,digest --concurrency=1 --hostname=wave@%h
Restart=on-failure
RestartSec=10s
MemoryHigh=380M
MemoryMax=460M

[Install]
WantedBy=multi-user.target
```

**Почему только `wave,digest`.** Это ровно те очереди, где задания длинные
(MiniMax + фото): дешёвая периодика (`celery`) остаётся у первого воркера.
Второй слот нужен там, где **расписание ждёт своей очереди**: слот 21:40 и
волна `admin` ждали по 17 минут, потому что единственный слот уходил в волны.

**Потолки памяти обязательны.** Бокс — 1,5 ГБ RAM, свопа нет. `MemoryHigh` —
«сожмись», `MemoryMax` — «ядро снимает **только** этот cgroup», а не движок и
не postgres (P164). Без потолка при совпадении пиков двух воркеров (движок
MiniMax ~330 МБ в задаче) ядро выбьет случайную жертву.

Установка и откат:

```bash
# установка
sudo cp ~/SETKA/infra/celery/setka-celery-worker-wave.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now setka-celery-worker-wave.service
systemctl is-active setka-celery-worker-wave.service

# откат: остановить и убрать второй слот
sudo systemctl disable --now setka-celery-worker-wave.service
sudo rm /etc/systemd/system/setka-celery-worker-wave.service
sudo systemctl daemon-reload
```

Проверка, что воркер жив и слоты делятся:

```bash
cd ~/SETKA && ./venv/bin/celery -A celery_app inspect active_queues
free -m | head -2
```

Имя узла `wave@%h` — **обязательно**: без него два воркера назывались бы одинаково
(`celery@host`), и `celery inspect`/`status` показывали бы только один из них.

### Step 5: Verify

```bash
# Check app is running
curl -s http://localhost:8000/api/health/ | python3 -m json.tool

# Check new API endpoints
curl -s http://localhost:8000/api/parsing-stats/recent | python3 -m json.tool

# Check Celery tasks registered
source venv/bin/activate
python3 -c "
from tasks.celery_app import app
tasks = [t for t in app.tasks if 'parsing_scheduler' in t]
print(f'Postopus tasks registered: {len(tasks)}')
for t in sorted(tasks):
    print(f'  - {t}')
"

# Check Celery beat schedule
python3 -c "
from tasks.celery_app import app
postopus_tasks = {k: v for k, v in app.conf.beat_schedule.items() if 'postopus' in k}
print(f'Postopus scheduled tasks: {len(postopus_tasks)}')
for name, config in sorted(postopus_tasks.items()):
    print(f'  - {name}: {config.get(\"schedule\")}')
"
```

### Step 6: Web UI

Open in browser:
- **Dashboard**: `http://your-server/`
- **Parsing Stats**: `http://your-server/parsing-stats` (NEW!)
- **API Docs**: `http://your-server/docs`

Check navbar — you should see new **"Статистика"** link.

---

## 📊 Celery Beat Schedule (Postopus Tasks)

All crontab entries from old_postopus are now Celery Beat schedules:

| Theme | Schedule | Task |
|-------|----------|------|
| **Reklama** | 19:05 | `run_all_regions_theme('reklama')` |
| ~~**Sosed**~~ | снята 2026-09-14 | пустые прогоны: ноль сводок за 30 дней, см. `tasks/celery_app.py` |
| **Novost** | 6:40, 10:40, 13:40, 16:40, 18:40, 20:40 | `run_all_regions_theme('novost')` |
| **Kultura** | 7:20 | `run_all_regions_theme('kultura')` |
| **Sport** | 22:50 | `run_all_regions_theme('sport')` |
| **Admin** | 22:20 | `run_all_regions_theme('admin')` |
| **Union** | 17:30 | `run_all_regions_theme('union')` |
| **Detsad** | 13:30 | `run_all_regions_theme('detsad')` |
| ~~**Addons**~~ | снята 2026-09-22 | тема разбиралась волнами своих тем, см. `tasks/celery_app.py` |
| **Proisshestviya** (strict) | 20:50 | `run_all_regions_theme('proisshestviya', strict=True)` |
| **Tatarstan oblast** | 19:45 | `parse_and_publish_theme('tatarstan_obl', 'oblast')` |
| **Copy Setka** | раз в 5 минут 06:00–23:00 МСК (мин. 2, 7, 12, 17, 22, 32, 37, 42, 47, 52) | `parse_and_publish_theme('copy', 'setka')` |

Источник правды — beat-расписание в [`tasks/celery_app.py`](../tasks/celery_app.py)
(имена слотов `postopus-<тема>-<час>`); таблица выше переписана 21.11 по живому
состоянию прода после возврата шести слотов `novost` (ADR-0018).

**Plus existing SETKA tasks:**
- Hourly workflow (at :05)
- Notifications checks (8:00-22:00 at :15, :16, :17)
- Daily bulletin (18:00)
- Daily cleanup (03:00)

---

## 🔍 Monitoring

### Logs

```bash
# App logs (uvicorn stdout/stderr + Python logging — systemd редиректит
# stdout/stderr в этот файл, см. setka.service)
tail -f ~/SETKA/logs/uvicorn_production.log

# Celery worker logs
journalctl -u setka-celery-worker -f --no-pager

# Celery beat logs
journalctl -u setka-celery-beat -f --no-pager
```

### Celery Flower (if installed)

```bash
celery -A tasks.celery_app flower --port=5555
# Open: http://your-server:5555
```

### Parsing Stats via API

```bash
# Recent runs
curl http://localhost:8000/api/parsing-stats/recent

# By region/theme
curl "http://localhost:8000/api/parsing-stats/?region_code=mi&theme=novost&days=7"

# Summary
curl "http://localhost:8000/api/parsing-stats/summary?days=7"
```

---

## ⚠️ Important Notes

1. **old_postopus branch is UNTOUCHED** — you can still rollback
2. **Crontab entries still exist** — they won't conflict (different times), but you can disable old ones after verifying SETKA works
3. **MongoDB is NOT deleted** — migration script reads from it, doesn't modify
4. **Test Polygon mode** is preserved — set `test_mode=True` in task calls

### Disabling old crontab (after verification)

```bash
crontab -e
# Comment out or remove old postopus entries
# Save and exit
```

---

## 🐛 Troubleshooting

### "Module not found" errors
```bash
# Restart services to reload PYTHONPATH
sudo systemctl restart setka setka-celery-worker setka-celery-beat setka-vk-bot
```

### Celery tasks not running
```bash
# Check beat schedule
journalctl -u setka-celery-beat | grep "postopus"

# Check worker picks up tasks
journalctl -u setka-celery-worker | grep "parsing_scheduler"
```

### Database tables missing
```bash
sudo -u postgres psql -d sarafan -c "\dt"
# Should see: parsing_stats, region_configs, work_tables, scheduled_publications
```

### MongoDB migration fails
```bash
# Check connection
python3 -c "from pymongo import MongoClient; c = MongoClient('YOUR_MONGO_URI'); print(c.list_database_names())"

# Verify MONGO_CLIENT env var
echo $MONGO_CLIENT
```

---

## 📞 Support

- **Full migration guide**: `docs/MIGRATION_GUIDE.md`
- **API docs**: `http://your-server/docs`
- **Git history**: `git log --oneline` to see all changes
