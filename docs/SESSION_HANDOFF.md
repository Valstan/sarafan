# Session Handoff

> Sticky-note для непрерывности между сессиями разработки SETKA. Перезаписывается через [`/close_session`](../.claude/commands/close_session.md) — историю смотри через `git log --follow -- docs/SESSION_HANDOFF.md`.

**Status:** ACTIVE
**Updated:** 2026-10-15
**Branch:** `chore/handoff-2026-10-15`
**Last release in prod:** `72457d9` (#894, движок классификатора) — `git pull` на боксе, `CLASSIFIER_ENGINE=opencode` в `setka.env`, воркер перезапущен; `origin/main` — `9fa612f` (#896).

---

## Текущая нитка

Пилот движка OpenCode завершён и влит: 7 PR (#886, #888–#894) — вердикты классификатора выносит харнесс `opencode serve` вместо мёртвого DeepSeek (402 с 22.09, P177). В проде движок включён (`CLASSIFIER_ENGINE=opencode`, systemd-oneshot), free-tier отвечает (longcat/space-bunny), muse geo-заблокирована (P195, parked). Первая живая волна с вердиктами ещё не подтверждена — это мониторинг следующей сессии.

## Следующий шаг

Проверить следующую волну в логе воркера на боксе: `grep "classifier headless: posts=" ~/SETKA/logs/celery-worker.log | tail -3` — нужны `verdicts>0` (а не `failures=72`). Если всё ещё нули — смотреть `opencode-engine` строки там же и `/home/valstan/opencode-serve-child.log`. Сверка БД: `SELECT count(*), max(created_at) FROM content_classifications WHERE source='headless' AND created_at > now() - interval '30 min'`.

## Контекст

- **План:** нет активного плана (ad-hoc пилот по заказу владельца 14.10).
- **Связанные коммиты сессии:** #886 (движок + цепочка фолбэков), #888 (прогон-скрипт), #889 (harness_server oneshot), #890 (батч в менеджере), #891 (SERVE_LOG), #892 (RSS-монитор, без muse), #893 (systemd-oneshot), #894 (systemd-путь из воркера). Детали — в телах PR.
- **Прод:** `setka`, `setka-celery-worker`, `setka-celery-beat`, `setka-vk-bot` — active, health 200 (проверено 14.10); `setka-opencode` снят с автозапуска (oneshot); `memcached`/`grafana-server` выключены (аудит памяти, +~360 МБ, доступно ~600).
- **Открытых PR:** нет.

## Failed approaches (этой нитки)

- **`opencode run` (CLI headless)** — стабильно висит после `init` (~170 c, ноль байт). **Не использовать** для автоматики; правильный режим — `opencode serve`.
- **Always-on `serve`** — держит ~264 МБ на боксе 1.5 ГБ, лёг OOM'ом трижды. **Не держать**; oneshot на прогон (`harness_server`).
- **`RLIMIT_AS` на ребёнка serve** — bun резервирует ~74 ГБ VM на старте, лимит рубит exec (SIGABRT). **Не ставить**; вместо него RSS-монитор (`/proc/<pid>/status`, потолок 450 МБ).
- **`Popen` serve из prefork-воркера Celery** — ребёнок умирает молча, 72/72 чанков `Connection refused` при живом ручном serve. **Не полагаться**; автоматический путь — systemd-oneshot (`OPENCODE_SYSTEMD_RUN=1`, юнит `setka-opencode-run`).
- **Muse в дефолтной цепочке** — geo-мертва с IP бокса (403 RegionError), каждый чанк съедал таймаут. Убрана из дефолта до расконсервации P195.

## Открытые вопросы для пользователя

- **P195 (Muse):** донорский VPS под `ssh -D` (~200–400 ₽/мес) / платный прокси / отказ от Muse (цепочка покрывает)?
- **P194 (безтекстовые, 88 шт.):** добор медиа по `lip` или постоянный пропуск?
- **P177:** проверить `DEEPSEEK_API_KEY` в KАРМАН-выдаче воркера (`no_api_key`, не 402)?

## Не забыть (low-priority)

- Ранги мест batch8 (TICKLER 13.10, просрочен) и batch9 (15.10).
- D-096 к 16.10 (`immutable` + таблица путей).
- Дистилляция из чата (`/distill`), верх DISTILL_LOG — 06.10.

---

> Если читаешь это в начале новой сессии — обнови через `/close_session` в конце. **Не аккумулируй history тут** — она в `git log --follow -- docs/SESSION_HANDOFF.md`.
