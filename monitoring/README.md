# SETKA — мониторинг

Prometheus + Grafana стек для наблюдения за сводками, VK API, кэшем и БД.

## Что есть

- **`/metrics`** на setka-web (FastAPI, port 8000) — Prometheus exposition. Доступ только с `127.0.0.1` (Prometheus локально). Override `SETKA_METRICS_PUBLIC=1` если надо открыть наружу.
- **Prometheus** — TSDB + scrape. Слушает `127.0.0.1:9090`, retention 5 дней (тонкий VPS).
- **Grafana** — `127.0.0.1:3000`, доступ через SSH-tunnel. Дашборд «SETKA — состояние сводок».

## Установка (один раз на проде)

```bash
ssh sarafan 'cd ~/SETKA && sudo bash scripts/setup-monitoring.sh'
```

Скрипт ставит `prometheus` и `grafana` через apt, копирует конфиги из репо. Идемпотентен — повторный запуск только обновляет конфиги. Доп. флаги:

```bash
PROM_RETENTION=14d sudo bash scripts/setup-monitoring.sh
```

## Доступ к Grafana

```bash
ssh -L 3000:127.0.0.1:3000 sarafan
```

Открыть витрину на её локальном порту. Первый вход — дефолтная пара логин/пароль из документации Grafana; **сменить сразу**, до того как порт станет доступен снаружи.

Дашборд: **SETKA → SETKA — состояние сводок**.

## Что показывает дашборд

| Панель | Что |
|---|---|
| **Часов с последней публикации** | Таблица region × topic с цветом: зелёный <3h, жёлтый 3-6h, оранжевый 6-12h, красный >12h |
| **Регион×тема простаивает >12h** | Stat-плашка с числом «застывших» комбинаций. >0 в рабочее окно = что-то сломалось |
| **Темп публикаций (в час)** | Time-series: rate per topic + result (success/empty/failed) |
| **Доля публикаций по темам** | Pie за выбранный период |

## Новые метрики (2026-05-26)

```
setka_digest_published_total{region,topic,result}   # Counter
setka_digest_last_published_timestamp{region,topic}  # Gauge (unix-ts)
```

`result` = `success` | `empty` | `failed`. Только `success` обновляет timestamp Gauge.

## Multiprocess (web + Celery worker)

`track_digest_published()` вызывается **из Celery worker'а** (`tasks/parsing_scheduler_tasks.py`, `modules/cascaded_bulletin.py`), а `/metrics` живёт **в FastAPI web**. В обычном `prometheus_client` counter'ы хранятся в памяти процесса — без shared backend worker'овские инкременты до scrape'а не доходят, и дашборд остаётся пустым.

Поэтому:

- `setup-monitoring.sh` создаёт `/var/lib/setka/prom_multiproc` (mode 0750) и кладёт drop-in `prometheus-multiproc.conf` в `/etc/systemd/system/{setka,setka-celery-worker}.service.d/`:
  - `Environment=PROMETHEUS_MULTIPROC_DIR=/var/lib/setka/prom_multiproc`
  - `ExecStartPre=` чистит каталог при рестарте (mmap-файлы от dead PID'ов искажают агрегации `digest_last_published_timestamp`).
- `monitoring/metrics.py` при выставленной env-var собирает выдачу через `MultiProcessCollector` поверх временной `CollectorRegistry`. Без env-var — обычный singleton-registry.
- Gauge'ы определены с `multiprocess_mode`:
  - `digest_last_published_timestamp` → `max` (timestamp монотонно растёт; самое свежее значение «выигрывает» при агрегации).
  - Остальные (`api_requests_in_progress`, `db_connections_active`, `communities_monitored`, `regions_active`, `notifications_zero_streak`, `cache_size_bytes`) → `livesum`.
- Celery worker при shutdown вызывает `multiprocess.mark_process_dead(pid)` — исключает свой PID из выдачи `MultiProcessCollector` (см. `tasks/celery_app.py`).

**Beat-сервис env-var не получает** — он метрики не пишет, дополнительный mmap-файл не нужен.

## Что НЕ настроено (by design)

- **alertmanager** — экономим RAM на 1.5GB VPS. Алёрты — глазами через дашборд или ручным `promtool query`.
- **Nginx-роут** на Grafana — доступ через SSH tunnel. Если понадобится через https + auth — добавить отдельный location в `/etc/nginx/conf.d/setka.conf`.
- **Внешние exporter'ы** (node_exporter, postgres_exporter) — пока только setka-web. При нужде добавить scrape-jobs в `prometheus.yml`.

## Файлы

- `monitoring/prometheus/prometheus.yml` — scrape config.
- `monitoring/grafana/provisioning/datasources/prometheus.yml` — datasource auto-config.
- `monitoring/grafana/provisioning/dashboards/setka.yml` — dashboard loader.
- `monitoring/grafana/dashboards/bulletins.json` — JSON дашборда.
- `monitoring/metrics.py` — Python-side метрики (импортируется из FastAPI и Celery).
- `scripts/setup-monitoring.sh` — установщик.
