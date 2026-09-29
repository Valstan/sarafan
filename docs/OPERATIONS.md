# Ops / эксплуатация САРАФАН

## 1) Быстрый запуск и проверка

```bash
cd ~/САРАФАН
source venv/bin/activate
python main.py
```

Проверка:

```bash
curl http://127.0.0.1:8000/api/health/
```

Swagger: `http://127.0.0.1:8000/docs`

## 2) Systemd (production)

Сервисы:

- `sarafan`
- `sarafan-celery-worker`
- `sarafan-celery-beat`
- `sarafan-vk-bot` — демон ВК-бота САРАФАНа (Bots Long Poll, `scripts/vk_bot_daemon.py`;
  юнит ставится `scripts/install_vk_bot_service.sh`)

Перезапуск:

```bash
sudo systemctl restart sarafan sarafan-celery-worker sarafan-celery-beat sarafan-vk-bot
```

Логи: `~/САРАФАН/logs/` (есть logrotate); демон бота пишет в `logs/vk-bot.log`.

ВК-бот: демон пишет heartbeat `setka:vkbot:heartbeat` (Redis db 1, unix-ts) после
каждого ответа Long Poll; сторож `vk-bot-watchdog` (beat, каждые 10 минут) шлёт Telegram,
если ключ старше 15 минут (cooldown 6 ч). Ключа нет — предупреждение в логе воркера, без
алёрта (свежий деплой). Проверка руками: `redis-cli -n 1 get setka:vkbot:heartbeat`.

## 3) Celery (ручной запуск)

```bash
cd ~/САРАФАН
./scripts/start_celery.sh
```

Остановка:

```bash
./scripts/stop_celery.sh
```

Расписания: `tasks/celery_app.py` → `app.conf.beat_schedule`.

## 4) Конфигурация и env

Принцип: секреты не в репозитории. Читаются из env (см. `config/runtime.py`, `database/connection.py`).
Второй непубликуемый класс — инфра-детали; правило целиком в [`AGENTS.md` §«Что не публикуется»](../AGENTS.md#что-не-публикуется-секреты-и-инфра-детали) (копии здесь нет — ADR-0011).

На прод-боксе env хранится в защищённом файле вне репозитория (`600 root:root`).

Обязательные переменные:

- `DATABASE_URL` (async SQLAlchemy)
- `REDIS_URL`

Часто используемые:

- `VK_TOKEN_<NAME>`
- `TELEGRAM_TOKEN_<NAME>`
- `TELEGRAM_ALERT_CHAT_ID`
- `DEEPSEEK_API_KEY` (нейро-движок экосистемы, MANDATE brain 2026-08-10)
- `SERVER_HOST`, `SERVER_PORT`
- `LOG_LEVEL` (по умолчанию `WARNING`)

## 5) Nginx

Редактируемая копия: `config/sarafan.conf.editable`.

Применение:

```bash
~/САРАФАН/scripts/apply_nginx_config.sh
```

Что должно работать:

- `:80` → редирект на `:443`
- `:443` → прокси на `127.0.0.1:8000`
- `/static` → alias на `web/static`

## 6) Мониторинг

- Метрики: `GET /metrics`
- Prometheus config: `config/prometheus.yml`
- Grafana подключается к Prometheus

## 7) Troubleshooting (коротко)

Сводка состояния:

```bash
cd ~/САРАФАН
bash scripts/check-setka.sh
```

Диагностика:

```bash
bash scripts/diagnose_connection.sh
```

Если FastAPI не отвечает:

```bash
ps aux | grep uvicorn
tail -n 200 logs/uvicorn_production.log
curl http://127.0.0.1:8000/api/health/
```

Если воркер Celery молча «моргнул» (`WorkerLostError` в логе, задача повторилась):

```bash
bash scripts/oom_probe.sh
```

**`journalctl` здесь слеп** — за 30 дней ноль строк при восьми убийствах, видимых в
`dmesg` (P164). Скрипт смотрит туда, где они есть, и заодно показывает RSS живых
процессов, срабатывания порога памяти и строки прибора `mem: task=…` — по ним видно,
какая задача поднимает потолок памяти. Бокс: 1536 МБ, **swap завести нельзя**
(OpenVZ, P163), поэтому единственная защита — не дать процессу дорасти.

## 8) Hot-fix runbook — branch protection

С 2026-05-23 на `main` GitHub-репо включена branch protection ([ADR-0002](../../brain_matrica/adr/0002-pr-only-flow-no-direct-push.md) §D):

- PR required (0 approvals; self-merge OK)
- CI `test (3.12)` required + strict (ветка должна быть свежей)
- force-push в `main` запрещён
- удаление `main` запрещено
- enforce_admins = true (даже owner не может обходить случайно)

**Обычный flow никогда не упирается в защиту** — feature-ветка → PR → CI зелёный → squash-merge.

### Если прод 502 и нужен срочный hot-fix без CI

ADR-0002 §8 даёт лазейку для аварий: разово снять protection, запушить fix, **сразу** вернуть protection и завести follow-up PR постфактум для audit trail.

```bash
# 1. Снять protection (требует gh + admin token)
gh api -X DELETE repos/Valstan/setka/branches/main/protection

# 2. Зафиксить и запушить напрямую в main
git checkout main
# ... правка ...
git commit -m "hotfix: ..."
git push origin main

# 3. Сразу вернуть protection (тот же JSON, что был при включении)
gh api -X PUT repos/Valstan/setka/branches/main/protection --input scripts/branch-protection.json

# 4. Завести follow-up PR с описанием инцидента
gh issue create --title "post-mortem: <инцидент>" --body "..."
```

Шаг 3 — **не забыть**. Если protection не вернётся, защита будет потеряна молча.

JSON конфиг лежит в `scripts/branch-protection.json` (см. репо). Проверить текущее состояние: `gh api repos/Valstan/setka/branches/main/protection`.

> ⚠️ **Этот файл — источник ВОССТАНОВЛЕНИЯ, а не пожелание.** Шаг 3 подаёт его в `PUT`, поэтому
> любое расхождение с сервером не «останется на бумаге», а молча **применится** при ближайшем
> hot-fix. Расхождение уже случалось: до 2026-08-20 в файле стояло `"strict": true`, тогда как на
> сервере `strict: false` — и это **сознательное** решение brain (при одном мержащем `strict` даёт
> лишний цикл пересборки без выигрыша). Восстановление после hot-fix включило бы strict, которого
> никто не просил. **Правило: правя защиту на сервере, правь и файл; расходятся — сверяй с
> сервером, он ground truth.**
>
> Текущее состояние (сверено 2026-08-20): обязательная проверка ровно `test (3.12)`,
> `enforce_admins: true`, `strict: false`, `allow_auto_merge: false`.

**Обойти обязательную проверку через `gh pr merge --admin` нельзя** — при `enforce_admins: true`
сервер отклоняет его и у владельца. Единственный путь мимо неё — hot-fix runbook выше, то есть
временно снять саму защиту, и это осознанная процедура с follow-up PR, а не «флажок в команде».

### Если CI красный из-за стороннего фейла (не своего кода)

Например, упал GitHub Actions runner, флакающий тест, временный 502 при `pip install`. Варианты:

1. **Перегнать CI** — в PR кнопка «Re-run failed jobs». Самый частый случай.
2. **Дождаться** и попробовать ещё раз через 5-10 минут.
3. **В крайнем случае** — то же что hot-fix runbook, но обязательно follow-up PR с описанием «почему обошли CI».

Если флакающий тест повторяется — это уже техдолг, в `PENDING_FOLLOWUPS.md`.
