# P253 — Локальный стенд: лента хаба падает целиком, волны-мамы падают в треть — «This event loop is already running»

**Приоритет:** 🔴 блокер (боевой контур публикует не всё; вечерние волны под вопросом)
**Открыто:** 2026-11-26 · `⏱ 2026-11-26 · snooze 0 · fresh`

## Симптом

На локальном стенде (теперь это боевой контур) общий персистентный event-loop
процесса оказывается **занят** к моменту, когда очередная задача зовёт
`utils.celery_asyncio.run_coro`. Отказ приходит снизу, чужим текстом:
`RuntimeError: This event loop is already running`.

Живой замер 26.11 (`logs/setka-wave-worker.err.log`, окно 09:58–18:33,
лог воркера с утра):

| что | сколько |
|---|---|
| `Task failed for copy/setka` (лента хаба) | **44 из 44 — 100 %** |
| `run_all_regions_theme` (мама волн): received / succeeded / **упало** | 6 / 4 / **2** |
| `parse_and_publish_theme` succeeded / retry (60 с) | 278 / 44 |
| `Discarding revoked task` | 58 |
| `missed heartbeat from celery@PC79` | 26 (слот волнового воркера занят) |
| «This event loop is already running» | 94, первое — 10:42 |

**Читать так:** лента хаба (`copy/setka`, «Короткой строкой») не работает **ни
одного раза** с утра; из шести волн-мам **две упали** — а вечерние волны
22:00 / 22:20 / 22:50 сегодня ещё не начинались, и каждая стартует с
мамы-задачи. «Revoked» и «missed heartbeat» — следствие того же: слот занят
зависшими задачами.

## Локализация (трейсбек из лога, не догадка)

```
tasks/parsing_scheduler_tasks.py:1263  run_all_regions_theme → regions = run_coro(_get_regions())
utils/celery_asyncio.py:59             run_coro → return _loop.run_until_complete(coro)
asyncio/base_events.py:626             raise RuntimeError('This event loop is already running')
```

То есть `_loop` (общий персистентный цикл процесса) **уже крутится** к моменту
вызова. Рядом в логе — `RuntimeWarning: coroutine 'parse_and_publish...' was
never awaited` (`tasks/parsing_scheduler_tasks.py:1150`): корутину запустили, но
не дождались.

## Почему сторож не поймал

`utils/celery_asyncio.py` (сторож `run_coro`) проверяет **только** «цикл крутится
в текущем потоке»:

```python
try:
    asyncio.get_running_loop()
except RuntimeError:
    pass
else:
    raise RuntimeError("run_coro() cannot be called from within a running event loop")
```

Если цикл занят **другим** потоком (а `run_coro` зовут из `asyncio.to_thread`),
`get_running_loop()` в текущем потоке ничего не видит — сторож пропускает, и
отказ приходит снизу, чужим текстом. Это ровно тот класс, о котором уже писали
мозгу (`mailbox/to-brain/2026-09-01-a-guard-that-catches-its-own-raise-silences-itself.md`)
и который закрыт тестом `tests/test_core/test_celery_asyncio_guard.py` — но тест
ловит лишь «вызов из async-контекста», а не «цикл занят другим потоком».

## Кандидаты в держатели цикла (нужен живой замер, не версия)

1. **Sync-мост `get_active_parse_tokens_sync`** (`modules/vk_token_router.py:979`)
   — сегодня уже известен как негодный в async-контексте: три его вызова в
   `tasks/discovery_tasks.py` и `tasks/parsing_tasks.py` переписаны на прямой
   `await` именно из-за этого. Если его позвать **из потока**
   (`asyncio.to_thread`), он заберёт общий цикл — и следующая задача упадёт.
2. `asyncio.to_thread` в копи-пути (`modules/copy_setka_network.py:81,182`):
   там крутится синхронный `VKClient`; если внутри него есть мост на `run_coro`,
   цикл уходит в поток.
3. **Незавершённая корутина от прерванной задачи**: `run_until_complete`
   прерывается (revoke / истечение срока), корутина остаётся висеть на общем
   цикле — механизм описан в `config/celery_config.py:105-115` (там же — почему
   мягкий лимит не вводили). 58 «Discarding revoked task» — по порядку
   совпадает.

## Что делать

1. **Живой замер (первым шагом):** в `run_coro` при отказе логировать
   `threading.get_ident()`, `getattr(_loop, "_thread_id", None)` и
   `_loop.is_running()` — кто держит цикл, видно сразу.
2. **Полный сторож:** отказывать и когда `_loop` уже занят (`_loop.is_running()`),
   и говорить **своим** сообщением вместо чужого «This event loop is already
   running». Это чинит диагностику, но не ленту.
3. **Убрать мосты из потоков:** все `run_coro`-вызовы из `asyncio.to_thread` —
   либо переписать на async (как сделано в `tasks/discovery_tasks.py`), либо
   передавать токен аргументом.
4. **Проверка сценария:** «прервать задачу (revoke) во время работы» → следующая
   задача обязана стартовать без «This event loop is already running».

⚠️ **Лечение требует рестарта юнитов стенда** — гейт хоста не даёт
(`Stop-ScheduledTask` — `HOST_CAPABILITY_UNAVAILABLE`). Владельцу рестарт всё
равно нужен (защита памяти движка, PR #1297) — значит, фикс стоит залить **до**
того рестарта, иначе он не встанет к ночи.

## Связанное

- [P233](P233-volna-zabiraet-vorker-periodika-zhdet-za-nej.md) — волна забирает
  воркер.
- [P238](P238-kladbishche-v-ocheredi-celery.md) — кладбище общей очереди.
- [P250](P250-sbor-osirotevshih-cli-ne-rabotaet-na-windows.md) — Windows-разрывы
  регулятора ёмкости.
- [P254](P254-proba-stenda-chitaet-ne-te-logi.md) — проба читала не те логи, и
  потому не видела этого.
- `utils/celery_asyncio.py`, `config/celery_config.py`,
  `tasks/parsing_scheduler_tasks.py`.
