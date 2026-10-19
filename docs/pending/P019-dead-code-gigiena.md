# Dead-code гигиена (#036, директива brain 2026-06-10)

> Запись `P019` реестра [PENDING_FOLLOWUPS](../PENDING_FOLLOWUPS.md). Индекс грепается, запись читается целиком, реестр целиком не читается никогда ([D-097](../../AGENTS.md)).

- 🟡 `⏱ 2026-09-04 · snooze 0 · recurring (следующий прогон 2026-11-19)` **Ежемесячный прогон `/deadcode`.**
  _Ре-триаж 2026-07-27: это не задача, а **постоянная программа** — гнить не может, поэтому меряется не
  возрастом записи, а датой следующего прогона._ Сканер `scripts/deadcode_scan.py` (vulture + авто-allowlist Celery/pydantic/SQLAlchemy), триаженное подавлено в `scripts/deadcode_known.txt` → отчёт показывает только новую дельту. Report-only, удаление — обычным PR. **Прогон 2026-09-04** (просрочен на месяц): 27 кандидатов дельты → триаж дал **11 `test-only`, 6 `sleeping`, 5 `dead`, 1 `alive`**. Ложное срабатывание вскрыло дефект самого сканера: `middleware/`, `core/`, `gateway_mcp/` не входили в `SCAN_TARGETS`, из-за чего живой `radar_id/vk_upstream.py::host_shares_session` выглядел мёртвым — его зовёт `middleware/auth_gate.py`. **Забытый каталог врёт дважды и оба раза молча:** мёртвое внутри него не ищется вовсе, а живой символ с потребителем внутри него всплывает кандидатом на удаление. Каталоги добавлены в скоуп (+17 кандидатов, из них 12 новых после гашения фреймворковых ложных: `dispatch` — контракт Starlette, `@mcp.tool` — MCP-инструменты шлюза). Итог: 38 кандидатов триажены, дельта 0, `known.txt` 143 → 192 строки. **Пакет `dead` на удаление ждёт решения владельца** (7 символов, ниже отдельным пунктом), `sleeping` — там же на ре-триаж. **Прогон 2026-07-11:** 14 «новых» кандидатов → из них **12 оказались устаревшими записями known.txt** (рефактор `digest→bulletin` #275 переименовал файлы/символы, но не обновил ключи подавления → digest_template/kirov_oblast_digest/digest_builder всплыли под новыми именами). Пропагировал rename в known.txt (`parse_region_hashtags` при этом стал реально `dead` — вызывающий `compute_effective_*` ушёл при рефакторе). **2 реально новых**, оба `sleeping`: `classifier/schema.py::has_merge_signal` (для merge agree-rate, не подключён), `radar_id/keys.py::keys_available` (readiness RS256-ключа Ф1, не подключён). Ре-прогон → 0 дельты. Урок для rename-playbook (#056): при переименовании файлов/символов грепать `deadcode_known.txt` по старым именам. **Прогон 2026-06-14:** 11 новых → удалены 2 dead-хвоста, 9 подавлены. **Прогон 2026-10-19** (просрочен на 15 дней — отсюда ре-триаж): дельта **18**, разобрана по категориям, но в `known.txt` пока не погашена — подавление в следующем `/deadcode`, чтобы не закрепить «спящее» как мёртвое мимоходом.
  - **1 ложное:** `ad_cabinet/pinning.py::exclude_client_id` — это *параметр сигнатуры*, а не символ; vulture читает «не использован в теле».
  - **3 test-only:** `ad_cabinet/client_orders.py::TRUST_AFTER_POSTS` (алиас ради обратной совместимости; зовут тесты и доки), `ad_cabinet/dm_channel.py::reset_for_tests` (autouse-фикстуры), `promotion/group_setup_vk.py::accepts_suggestions` (`tests/test_promotion/test_accepts_suggestions.py`).
  - **14 sleeping:** `ad_cabinet/outreach.py::SENT_STATUSES` и `::is_blacklisted`, `ad_cabinet/pinning.py::pin_window_day` (потребителей нет, но и удалять без решения владельца нельзя) и **11 методов `service_notifications`** (`system_start`…`region_queue`) — старый интерфейс этапов конвейера: сам менеджер жив (`add_notification`/`get_*` зовут три модуля и API), а методы этапов не зовёт никто.
- 🟡 `⏱ 2026-09-04 · snooze 0 · ✅ ЗАКРЫТО 2026-09-04 — владелец разрешил («если ничего важного, удаляй»), все семь сняты` **Пакет `dead` прогона 04.09 — 7 символов, удалены.**
  Тесты после удаления: 3272 passed, 36 skipped — ровно столько же, сколько до него, то есть ни один
  тест на удалённое не опирался. Подавления вычищены из `deadcode_known.txt` (186 строк), дельта сканера 0.
  Две правки оказались не «удалить строку», а мелким рефактором: `h_top` — переменная распаковки в цикле
  `render_cover` (убран третий элемент кортежей), `check_vk_rate_limit` снят вместе с транзитивным orphan
  `vk_rate_limiter` (#028 — идти цепочкой). На месте каждого удалённого куска оставлен комментарий-указатель,
  куда переехала задача: ограничитель VK — в `modules/vk_monitor/rate_limiter.py`, перечень env-ключей —
  в `scripts/list_gateway_keys.py`, разметка потока — в `modules/classifier`.
- 🟡 `⏱ 2026-09-04 · snooze 0 · parked · условие расконсервации: решение владельца по судьбе PostAnalyzer` **Удаление обнажило следующий слой цепочки — `modules/ai_analyzer/analyzer.py::analyze_post`.**
  Его единственным потребителем был снятый `analyze_new_posts`, поэтому после удаления он остался
  без вызывающих. **Дальше по цепочке не пошёл сознательно:** `analyze_post` — тело класса `PostAnalyzer`,
  за ним осиротеют приватные `_check_filters`, `_keyword_analysis`, `_calculate_score`, а это весь файл
  (~230 строк) плюс правка `modules/real_workflow.py:50`, где объект создаётся и больше не используется.
  Формально путь живой: `real_workflow_manager` дёргается из `web/api/test_workflow.py`. То есть это уже
  не гигиена хвостов, а снятие подсистемы — отдельное решение владельца, а не довесок к прогону
  `/deadcode`. Записан в `deadcode_known.txt` как `dead` с пометкой о происхождении.
- ~~🟡 Пакет `dead` — 7 символов ждали разрешения~~ (исходная запись): По памятке `/deadcode` сканер никогда не удаляет сам; вот что триаж признал хвостами, каждый — с потребителем, который был и ушёл, либо с задачей, решённой в другом месте:
  1. `modules/ad_cabinet/vk_bot/dialog.py::REGIONS_KEYBOARD` — хвост #609: клавиатуру теперь строит функция `regions_keyboard()`;
  2. `modules/ai_analyzer/analyzer.py::analyze_new_posts` — звавшие `tasks/analysis_tasks.py` (D-024) и `correct_workflow` удалены;
  3. `modules/classifier/schema.py::VerdictBatch` — `POST /verdicts` переведён на `batch: dict` + `parse_verdict_loose` (#354);
  4. `modules/promotion/branding.py::h_top` — переприсвоенная локальная в `render_cover`, ниже не читается;
  5. `utils/timezone.py::is_work_hours_for_region` — хвост b05d5c2 (#510), рядом живёт `is_work_hours_moscow`;
  6. `modules/gateway/keys.py::get_bootstrap_env_keys` — дубль: `scripts/list_gateway_keys.py` считает то же сам (строки 119, 207);
  7. `middleware/rate_limiter.py::check_vk_rate_limit` — вытеснен `modules/vk_monitor/rate_limiter.py`; **при удалении тянуть транзитивный orphan `vk_rate_limiter`** (`middleware/rate_limiter.py:227`) — по #028 идти по цепочке, а не по одному символу.
  Удалять — отдельным PR после «да»; шесть из семи безболезненны, седьмой требует цепочки.
- 🟢 `⏱ 2026-09-04 · snooze 0 · parked · условие расконсервации: касание соответствующей фичи` **`sleeping` прогона 04.09 — 20 символов, живых как замысел, но без потребителя.** Удалять нельзя без решения владельца (#036), гнить самостоятельно они тоже не могут — расконсервируются, когда дойдут руки до своей фичи:
  - **раскрутка, этап 0** (#538): `config/promo.py::CHANNELS` и `::USER_TOKEN_CHANNELS`, `modules/promotion/pairing.py::HOP_SECOND` и `::HOP_OBLAST`, `modules/promotion/vk_errors.py::alert` (поле контракта выставляется, но будильщика владельца нет), `modules/promotion/copy.py::render_outreach_draft` (этап 5: колонка `draft_text` есть, UI нет). Общая черта: рабочий реестр построен рядом и уже — `modules/promotion/settings.py::DEFAULT_CHANNELS`, а `hop` в `dispatcher.py:649` пишется литералом мимо констант;
  - **кабинет** (#531): `modules/ad_cabinet/packages.py::PROMO_POSTS` и `::KINDS` — «3 поста акции» и перечень видов пакетов заданы литералами в `web/static/js/ad_crm.js:1971` и regex'ом в `web/api/ad_crm.py:2168`, то есть константы описывают правду, но не участвуют в ней;
  - **конвейер**: `modules/conveyor/runner.py::retry_failed` — готовый ops-инструмент повтора failed-доставок, ни разу не вызванный (покрыт тестами);
  - **таксономия исключений** `core/exceptions.py` — 11 листьев без потребителя (`ValidationException`, `DuplicateException`, `DatabaseConnectionException`, `DatabaseQueryException`, `CacheConnectionException`, `AnalysisException`, `PublishingException`, `DeduplicationException`, `MissingConfigException`, `LLMAPIException`, `TelegramAPIException`). Живут только корни и то, что ловят `utils/retry.py`, `tasks/parsing_tasks.py`, `modules/vk_monitor/vk_client_async.py`; `DuplicateException` — единственный, у кого потребитель есть, но в `examples/error_handling_example.py`, то есть в демо, а не в прод-пути. Эти восемь всплыли только сейчас — каталог `core/` до 04.09 не сканировался.
- ~~**Разбор первого триажа: ~120 dead-кандидатов**~~ — закрыто 2026-06-12 четырьмя пакетными PR (делегировано владельцем «на твой выбор»): [#211](https://github.com/Valstan/setka/pull/211) carousel-цепочка (`vk_carousel_tasks.py` + orphan `carousel_manager.py`), [#212](https://github.com/Valstan/setka/pull/212) старые publisher'ы (`wordpress_publisher`/`telegram_publisher`/`event_distribution` + orphan `base_publisher`), [#213](https://github.com/Valstan/setka/pull/213) postopus-слой `modules/core` (остался только живой `calculate_post_score`), [#214](https://github.com/Valstan/setka/pull/214) россыпь utils/ + 6 декораторов metrics (−556 строк). Все цепочки orphan'ов прослежены (#028), подавления вычищены из `deadcode_known.txt`, 1236 тестов зелёные. ~~Мини-хвост `utils/post_utils.py::format_number`~~ — снят прогоном 2026-06-14 (см. выше).
- ✅ **Обратная сверка имён Celery-задач — ПОСТАВЛЕНА** (caveat brain 2026-07-26 `deadcode-gate-caveat-string-bound-channels`, `suggest`; G184 — соседу удаление «мёртвого» файла со ~52 строково-адресуемыми задачами уронило прод). У нас **первая половина защиты уже была**: `scripts/deadcode_scan.py::collect_celery_task_names` вносит декорированные функции и строки `"task"` из beat в allowlist, поэтому vulture не предлагает удалить задачу, которую зовут строкой. Не было **обратной** сверки — «строка ведёт к живому обработчику»; она и добавлена: `tests/test_celery_task_names.py` резолвит beat-расписание (80 записей) и все `send_task`/`signature`-вызовы по AST (включая имя через модульную константу, как `scripts/smoke_test.py::TASK_NAME`) против `app.tasks` (43 задачи после `import_default_modules`). Сейчас сирот **0**; проверено негативно — ловит и опечатку в beat-имени, и сироту в `send_task`. Заодно ловит опечатки в именах, которые раньше не ловило ничто.
- ~~🟢 **Мёртвые строковые имена задач в UI-подписях**~~ Закрыто 2026-07-26 ([PR #390](https://github.com/Valstan/setka/pull/390)): `modules/celery_task_monitor.py::_format_task_name` и `modules/system_status_notifier.py::_format_task_name_for_user` держали словари человекочитаемых названий на **8 имён, ни одно из которых не было зарегистрировано** (модулей `tasks.notification_tasks` / `tasks.publishing_tasks` / `tasks.real_vk_workflow` нет в репо вовсе; в остальных нет перечисленных задач) — то есть подписывать словари ничего не подписывали, каждое живое имя уходило в fallback. Оба словаря удалены, fallback оставлен как был (разный у двух методов, поэтому в общий хелпер **не** сводил — это изменило бы вывод одного из них). В жёсткий гейт `tests/test_celery_task_names.py` такие словари сознательно не включены: там имя — ключ поиска с fallback'ом, а не вызов.
- 🟡 `⏱ 2026-07-26 · snooze 0 · parked` — **условие расконсервации: владелец решил, нужен ли
  ежемесячный замер прав токенов** (это вопрос не кода; решён — либо включаем модуль в `include`,
  либо удаляем как мёртвый слой). _Ре-триаж 2026-07-28: стояло `fresh`, но открытым тут висит
  не моя работа, а чужое решение по scope. Детектор злоупотребления `parked` от КАРМАНа (через
  brain 2026-07-28): «если условие нельзя проверить одной командой или одним фактом — это не
  парк, а снуз». Здесь проверяется одним фактом — ответом владельца._
  **`tasks/monitoring_tasks.py` не подключён к воркеру** —
  найдено гейтом имён задач при постановке месячного замера прав: модуля нет в `include` Celery-app
  (`tasks/celery_app.py:171`), поэтому его четыре задачи (`scan_all_communities`, `scan_region`,
  `health_check`, `cleanup_old_data`) воркеру недоступны — beat их не зовёт, `send_task` по имени
  упал бы `No handler registered`. Мою задачу я перенёс в `celery_app.py`, но сам модуль остался
  висеть: решить, включать его в `include` (тогда задачи оживут) или удалить как мёртвый слой.
  Именно тот класс, о котором предупреждал brain в caveat G184, только с другой стороны: не
  «строка без символа», а «символ без канала».
  ✅ **Закрыто 2026-08-20 — удалён как мёртвый слой.** Условие парковки протухло само: месячный
  замер прав уже живёт как `tasks.celery_app.probe_token_capabilities` с beat-записью
  `probe-token-capabilities-monthly` и от этого модуля не зависит — то есть решать владельцу
  оказалось нечего. Включение не дало бы ни одной новой функции (`cleanup` перекрыт
  `cleanup_old_posts`, health — HTTP-эндпоинтом `/api/health/full`, scan — конвейером
  `parsing_scheduler_tasks`), зато воскресило бы легаси-запись в таблицу `Post` параллельно живому
  конвейеру. `HealthChecker` и `VKMonitor` сиротами не стали — оба живут вне модуля.
  **Заодно найден второй модуль в ровно том же состоянии, которого в бэклоге не было:**
  `tasks/production_workflow_tasks.py` (207 строк) — тоже не в `include`, в beat не значится,
  строкой не зовётся. Единственная «ссылка» на него — импорт `run_single_region_workflow` в
  `scripts/test_production_automation.py`, а функции с таким именем в модуле **не было никогда**:
  импорт падал внутри `except Exception` и молча возвращал False. Тест годами показывал провал как
  норму. Оба модуля удалены, мёртвый блок из скрипта убран.
  **И третье, попутно:** дерево каталогов в `docs/AI_DEV_GUIDE.md` описывало `tasks/`, которого
  давно нет — обещало `publishing_tasks.py` и `analysis_tasks.py` (отсутствуют в репо) и молчало
  про живые `discovery_tasks` / `radar_tasks` / `broadcast_tasks`. Приведено к реальности.
- 🟢 `⏱ 2026-06-14 · snooze 0 · parked` (ре-триаж 2026-07-27: ждёт инструментирования либо явного
  решения владельца чистить; удаление меняет surface `/metrics`, поэтому молча не делается)
  **5 Prometheus-метрик без продьюсера** (`vk_api_request_duration_seconds`, `db_queries_total`, `db_query_duration_seconds`, `posts_processed_total`, `posts_published_total`) — определены, но никем не инкрементятся (экспортятся пустыми). Помечены `sleeping` в known.txt (не удалял молча — удаление меняет surface `/metrics`). Кандидат на чистку при желании владельца (или дождаться, пока инструментируем).
