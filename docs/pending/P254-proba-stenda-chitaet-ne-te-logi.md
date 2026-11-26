# P254 — Проба состояния стенда читает не те логи: «0 ошибок» в отчёте пустое

**Приоритет:** 🟡 техдолг (проба врёт, а на её показания опираются решения)
**Открыто:** 2026-11-26 · `⏱ 2026-11-26 · snooze 0 · fresh`

## Симптом

`scripts/local_status.py` (проба состояния локального стенда, read-only) читает
`logs/celery-worker.log` и `logs/celery-wave.log` и по ним рапортует:
«ошибок в хвосте — 0, отброшенных просроченных задач — 0».

На стенде эти два файла — **stdout** воркеров, и в них почти ничего: 4 КБ,
последняя запись 09:58 (момент старта юнитов). Живые логи лежат в
`logs/setka-worker.err.log` (1.4 МБ, пишется до сих пор) и
`logs/setka-wave-worker.err.log` (809 КБ): celery пишет в **stderr**, а
`Start-StandProcess` (`infra/windows/common.ps1:110-121`) шлёт stdout в
`celery-*.log`, stderr — в `setka-*.err.log`.

Итог: проба рапортует «0 ошибок», **не открыв ни одного живого лога**. Ровно по
её пустым показаниям в handoff и TICKLER писалось «в логах воркеров 0 ошибок и
0 отброшенных просроченных задач» — и так прошло мимо 94 отказов
«This event loop is already running», 58 отброшенных задач и 26 «missed
heartbeat» (см. [P253](P253-lenta-khaba-i-chast-publikaciy-padayut-s-event-loop.md)).

## Что делать

1. Проба должна читать `logs/setka-worker.err.log` и
   `logs/setka-wave-worker.err.log`. Надёжнее — искать по шаблону `setka-*.err.log`
   (имя выводится из имени задачи планировщика), а не по фиксированным именам.
   ⚠️ Файл принадлежит **другому агенту** — правило «незнакомые изменения
   считаем чужими»: не правим без решения владельца.
2. **Ссылки на логи в TICKLER и handoff** (`logs/celery-wave.log` →
   `logs/setka-wave-worker.err.log`) заменены 26.11 — см. PR.
3. Проверка: проба обязана показывать **свежий** хвост (строки текущего часа) и
   ловить «Discarding revoked task», если он есть. Пустой «0 ошибок» на файле,
   который не пишется месяцами, — не показание, а его отсутствие.

## Связанное

- [P253](P253-lenta-khaba-i-chast-publikaciy-padayut-s-event-loop.md) — живые
  дефекты стенда, которых проба не увидела.
- `infra/windows/common.ps1` (`Start-StandProcess`), `scripts/local_status.py`.
