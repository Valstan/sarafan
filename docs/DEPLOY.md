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
systemctl status sarafan sarafan-celery-worker sarafan-celery-beat sarafan-vk-bot
```

If services don't exist yet, see setup below.

---

## 🔧 Deployment Steps

### Step 1: Pull Latest Code

```bash
cd ~/САРАФАН
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
sudo systemctl restart sarafan

# Celery worker (processes tasks)
sudo systemctl restart sarafan-celery-worker

# Celery beat (schedules tasks — NOW WITH 27 NEW POSTOPUS TASKS)
sudo systemctl restart sarafan-celery-beat
```

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
| **Reklama** | 10:05, 14:05, 19:05 | `run_all_regions_theme('reklama')` |
| ~~**Sosed**~~ | снята 2026-09-14 | пустые прогоны: ноль сводок за 30 дней, см. `tasks/celery_app.py` |
| **Novost** | 6:40, 11:40, 12:40, 16:40, 18:40, 20:40 | `run_all_regions_theme('novost')` |
| **Kultura** | 7:20, 13:20, 16:20, 19:20, 21:20 | `run_all_regions_theme('kultura')` |
| **Sport** | 12:30, 19:30 | `run_all_regions_theme('sport')` |
| **Admin** | 8:20, 12:20, 20:20 | `run_all_regions_theme('admin')` |
| **Union** | 11:30, 17:30 | `run_all_regions_theme('union')` |
| **Detsad** | 13:30 | `run_all_regions_theme('detsad')` |
| **Addons** | 6:20, 11:20, 18:20, 22:20 | `run_all_regions_theme('addons')` |
| **Copy Setka** | Every hour at :07, :37 | `parse_and_publish_theme('copy', 'setka')` |

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
# stdout/stderr в этот файл, см. sarafan.service)
tail -f ~/САРАФАН/logs/uvicorn_production.log

# Celery worker logs
journalctl -u sarafan-celery-worker -f --no-pager

# Celery beat logs
journalctl -u sarafan-celery-beat -f --no-pager
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
sudo systemctl restart sarafan sarafan-celery-worker sarafan-celery-beat sarafan-vk-bot
```

### Celery tasks not running
```bash
# Check beat schedule
journalctl -u sarafan-celery-beat | grep "postopus"

# Check worker picks up tasks
journalctl -u sarafan-celery-worker | grep "parsing_scheduler"
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
