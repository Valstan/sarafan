# Итерация 3 — localities-driven discovery + human-in-the-loop AI

> Запись `P043` реестра [PENDING_FOLLOWUPS](../PENDING_FOLLOWUPS.md). Индекс грепается, запись читается целиком, реестр целиком не читается никогда ([D-097](../../AGENTS.md)).

Начато 2026-05-25 после жалобы на качество подбора кандидатов: discovery для `tuzha` возвращал крупные общегородские паблики Кирова/Татарстана без географической привязки к Тужинскому району. Корень — VK `groups.search` не строгий + сортировка по `members_count` усиливала перекос. Параллельно — ~~`GROQ_API_KEY` 403 на проде (нет бюджета, см. 🔴 блокеры) → AI-категоризация перестала фильтровать релевантность~~ **(устарело после D-024: движка Groq больше нет, AI-категоризация работает на DeepSeek — `modules/discovery/ai_categorizer.py:31` = `from modules.deepseek_client import chat`, слова `groq/GROQ` в файле нет; ре-триаж 2026-08-28)**.

Решение в 3 PR:

- ✅ **PR 1 — backend** ([#39](https://github.com/Valstan/setka/pull/39)): миграция 012 (`community_candidates.ai_is_relevant`), `vk_search.py` принимает `localities`/`keywords` из `region.config`, hard relevance-filter, сортировка `(matched_localities desc, members_count desc)`. +34 теста.
- ✅ **PR 2 — UI «Подготовка района»** ([#40](https://github.com/Valstan/setka/pull/40)): `/regions/<code>/prepare`. Два блока: localities (OSM Overpass auto-suggest + clipboard-prompt fallback) и discovery_keywords. API: `GET/PATCH /api/discovery/regions/{code}/config`, `GET /api/discovery/osm-localities`. +24 теста.
- ✅ **PR 3 — AI-batch через clipboard** ([#41](https://github.com/Valstan/setka/pull/41)): `/regions/<code>/discovery/ai-batch`. Чанки по 30, готовый prompt + clipboard, robust JSON parser. API: `GET /ai-batch`, `POST /ai-batch/apply`, `GET /ai-batch/status`. Badge «✓/✗ районный» + фильтр «скрыть нерелевантных» на discovery. +11 тестов.
- ✅ **Релиз на прод 2026-05-25**: HEAD `7ba2560`, миграция 012 применена, restart всех 3 сервисов, health 200. Endpoints `/regions/tuzha/prepare`, `/regions/tuzha/discovery/ai-batch` → 200. AI batch status: `total: 147 pending, processed: 0`.

⏳ `⏱ 2026-05-25 · snooze 3+ · stale → ре-триаж 2026-06-10: переформулировано — влит в «Пакет браузер-верификаций владельцем» (🟢 Идеи ниже), отдельным пунктом не висит` **Осталось — практический smoke на tuzha** в браузере: `/regions/tuzha/prepare` → OSM auto-suggest или ChatGPT prompt → save → re-trigger discovery → должно отвалиться ~120/147 нерелевантных. Затем `/discovery/ai-batch` → прогнать через нейросеть → approve → commit. Это пользовательский шаг (нажимать кнопки), не код.

~~Зачем не ждать Groq: пользователь явно сказал — бюджета на API нет. Human-in-the-loop через clipboard бесплатно (юзер тратит свой ChatGPT/Claude.ai тариф), прозрачно, юзер видит точный prompt. Не масштабируется на еженедельный recheck — но и не нужно, recheck без AI работает (он смотрит health, не категоризацию), `changed_category` детекция временно отключена.~~

**Переписано по факту кода 2026-08-28 — обе опорные фразы абзаца неверны:**
- **Ждать больше нечего:** движок сменён на DeepSeek (D-024, 2026-08-12), ключ есть, автоматическая
  AI-категоризация в discovery работает без участия человека.
- **`changed_category` детекция НЕ отключена:** `modules/discovery/health_check.py:289` ставит
  `status="changed_category"`, порог описан там же на `:26` (`confidence >= 70`).
- ⏳ **Отдельный открытый шаг (ход агента): решить судьбу clipboard-ветки `ai-batch`.** Она жива и
  никуда не делась — `web/api/discovery.py:278` `GET /regions/{code}/ai-batch`, `:344`
  `POST /ai-batch/apply`, `:420` `/ai-batch/status`, — но при работающем авто-пути она его дублирует.
  Кандидат в dead-code (#036): либо сознательно оставляем как ручной запасной путь и пишем это
  прямо, либо сносим целиком с UI-бейджем.

_Смежный факт (живёт в секции D-024, не здесь): beat-слот `discovery-rolling-daily` закомментирован —
`tasks/celery_app.py:2264` `# "discovery-rolling-daily": {`._

---

### ⏱ Ре-триаж 2026-10-01: clipboard AI-batch vs авто-путь DeepSeek

**Итог:** PR 1/2/3 выкачены, миграция 012 применена, UI `/regions/<code>/prepare` и `/discovery/ai-batch` работают. Но после D-024 (DeepSeek вместо Groq, 2026-08-12) **автоматическая AI-категоризация в discovery работает без участия человека** (`modules/discovery/ai_categorizer.py` вызывается из `tasks/discovery_tasks.py:_ai_categorize_all`). Clipboard-ветка (`web/api/discovery.py`: `GET /ai-batch`, `POST /ai-batch/apply`, `/ai-batch/status`) теперь **дублирует авто-путь** — ручной fallback для случая, когда модели нет, а модель есть.

**Решение владельца (единственный открытый вопрос):**  
- **Вариант А** — оставить clipboard как *ручной запасной путь* (на случай если DeepSeek отвалится), но **писать это прямо** в UI и коде: бейдж «Ручной режим — DeepSeek недоступен», кнопка отключена пока авто-путь жив.  
- **Вариант Б** — снести целиком (dead-code #036): UI-бейдж, API endpoints, JS. Чище, меньше кода.

Рекомендую **Вариант А с явным флагом**: `DISCOVERY_AI_BATCH_MODE=auto|manual|off`. Сейчас — `auto` (авто-путь), clipboard скрыт. Если DeepSeek 402 (P177) — владелец ставит `manual`, clipboard появляется. Это снимает «дублирует» и даёт runway.

**Смежный:** beat-слот `discovery-rolling-daily` (P001) ждёт только оплаты DeepSeek; clipboard не нужен для включения авто-подбора.

**Действие:** добавить флаг `DISCOVERY_AI_BATCH_MODE` в `config/runtime.py`, в `web/api/discovery.py` — бейдж в ответе `/ai-batch/status`, в UI — условный рендер. Dead-code гейт (#036) следом проверит.
