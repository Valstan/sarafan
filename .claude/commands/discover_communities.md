---
description: Подбор и нейро-классификация VK-сообществ в пул региона (район/область) — read-only скан на проде + ручная классификация по постам в чате + идемпотентный засев в communities.
argument-hint: [<region_code> — напр. kirov_obl; опц. --themes=novost,sport,... ]
allowed-tools: Bash, Read, Write, Edit, AskUserQuestion
---

# /discover_communities — подбор сообществ в пул региона

Наполняет таблицу `communities` для региона качественными VK-источниками по темам.
Ключевая идея — **нейронка в петле**: скрипт только СОБИРАЕТ сырьё (группы + свежие
посты), а решение «какая тема / брать ли» принимает Claude, **прочитав посты**.
Алгоритмы/Groq для классификации сознательно НЕ используются — суть группы по постам
понимает нейросеть.

Применимо и к **районам**, и к **областям** (`kind in {oblast,strana}` с
`digest_mode='communities'`). Выработано на `kirov_obl` (53 источника, 12 тем, май 2026).

## Когда использовать
- Новый регион добавлен, пул `communities` пуст/беден.
- Нужно расширить охват темы (мало источников → дайджест пустой).
- Периодический добор «свежей крови» в существующий пул.

## Инструменты (в репо)
- [`scripts/discover_scan.py`](../../scripts/discover_scan.py) — read-only VK-сканер.
  Токен **только** из env `SCAN_VK_TOKEN`, наружу — лишь данные групп/постов.
  Флаги области: `--per-label-top N` (ранжир ВНУТРИ темы), `--region-filter REGEX`
  (по name+description — режет чужие регионы), `--name-filter REGEX` (по имени —
  выцепляет профильные/официальные паблики), `--count/--top/--posts/--min-members`.
  Флаги района: `--localities`, `--main-group`, `--newsfeed-search`,
  `--crawl-subscriptions` (см. раздел «Режим: район»). Вывод включает
  `source_breakdown` (вклад источников) и `all_found` (полный компактный список).
- [`scripts/seed_region_communities.py`](../../scripts/seed_region_communities.py) —
  идемпотентный засев из JSON (`vk_id`→`-abs`, дедуп по region+vk+category).

## ⚠️ Секреты
VK-токен живёт **только на проде**. В чат НЕ печатать. Скан берёт токен из БД в
шелл-переменную (никогда не `echo`); сканер читает его из env. См. шаг 3.

---

## Шаг 0. Подтверждение прод-доступа
SSH на прод классификатор может блокировать → подтвердить через `AskUserQuestion`
(«дать доступ ssh sarafan на сессию»). Проверка попадания в SETKA:
`ssh sarafan 'test -f ~/SETKA/main.py && echo OK_SETKA'`.

## Шаг 1. Запросы по темам
Составь `queries.json` — список `{"q": "<запрос>", "label": "<тема>"}`. Принципы
зависят от уровня региона:
- **Область** (из опыта kirov_obl): общие новостные (`<город> новости/онлайн`,
  `ЧП <город>`, `<область>`); официальные по темам (`Министерство <X> <области>`,
  `Правительство`, `Губернатор`, вузы `ВятГУ`, `Динамо <город>`, `Движение Первых`);
  расширенный набор тем `novost, proisshestviya, admin, kultura, sport, molodezh,
  nauka, promyshlennost, selhoz, zdorovie, zhkh, priroda`.
- **Район** (из опыта `mi`/Малмыж): ручные запросы — лишь подспорье (`<райцентр>
  новости/объявления/спорт/школа`, `Подслушано <райцентр>`, `ЧП <райцентр>`). Главную
  работу делают **локалити-автозапросы и главная ИНФО-группа** — см. «Режим: район».
  НЕ используй областные шаблоны (`Министерство`/`Губернатор`) — район мельче.
  Канон-набор тем района — подмножество: `novost, reklama, kultura, sport, admin,
  detsad, union` (без областных тем). **`sosed` в набор не входит с 2026-09-14** —
  тема снята с расписания, и добирать под неё источники больше незачем.

## Шаг 2. Залить сканер + запросы на прод
```bash
scp scripts/discover_scan.py /tmp/queries.json sarafan:/tmp/
```

## Шаг 3. Запустить скан на проде (read-only, токен не светится)
Через runner-скрипт (надёжнее, чем экранирование через ssh). Создай `/tmp/run.sh`:
```bash
#!/usr/bin/env bash
set -euo pipefail
TOKEN="$(sudo -u postgres psql -d setka -tA -c "SELECT token FROM vk_tokens WHERE community_id IS NULL AND is_active AND token<>'' AND (disabled_until IS NULL OR disabled_until < now()) AND COALESCE(validation_status,'')<>'invalid' ORDER BY last_used NULLS FIRST, id LIMIT 1")"
[ -z "$TOKEN" ] && { echo NO_TOKEN; exit 1; }
echo "token loaded (len ${#TOKEN})"   # печатаем только длину
SCAN_VK_TOKEN="$TOKEN" ~/SETKA/venv/bin/python /tmp/discover_scan.py \
  --queries /tmp/queries.json --out /tmp/scan.json \
  --count 40 --per-label-top 8 --top 70 --posts 5 --min-members 300 \
  --region-filter '(<маркеры региона>)' \
  --name-filter '(<тематические корни, если добираешь нишу>)'
```
(SQL-фильтр токена зеркалит `modules.vk_token_router.get_active_parse_tokens`.)
`scp scripts/_run.sh sarafan:/tmp/run.sh && ssh sarafan 'bash /tmp/run.sh'`.

**Эволюция фильтров (важно — иначе мусор):**
1. Без фильтров + сортировка по подписчикам → перекос в общегородские гиганты и коммерцию.
2. `--per-label-top` → нишевые темы не тонут под гигантами.
3. `--region-filter '(киров|вятк|хлынов)'` → fuzzy-выдача VK тащит чужие регионы
   (Тюмень/Калуга/СПб/Москва…) — этот фильтр их режет по name+description.
4. `--name-filter '(сельск|агро|апк|… )'` → для нишевых/официальных тем оставляет
   только группы, чьё ИМЯ про тему (выцепляет министерства, отсекает гигантов).

## Режим: район (отработано на `mi`/Малмыж, май 2026)

Для района generic-запросы дают мало — главный сигнал в **локалитетах (сёлах)** и в
**самой главной ИНФО-группе**. Включается флагами (по умолчанию off → область работает
по-старому). **Обязательно `--min-members 0`** — сельские клубы крошечные.

| Флаг | Источник | Эмпирика на Малмыже (`mi`) |
|---|---|---|
| `--localities "село1,село2,…"` | `groups.search` по каждому селу + locality-скоринг (ранжир по `matched_localities`) | **Главный выигрыш**: +442 уник. групп к 261 от ручных запросов (≈2.7×). |
| `--main-group <id>` | стена главной: репосты (`copy_history`), @упоминания/ссылки из текста, **блок «Ссылки»** (`groups.getById fields=links`) | «Ссылки» — **высокоточный** курируемый источник (вернул партнёров + 2 пропущенных из пула). Репосты = 0, если главная постит оригиналы. Упоминания — мало, но дают хэштеги. |
| `--newsfeed-search --days N` | глобальный `newsfeed.search` по локалитетам+хэштегам | Мощно в «холодном» бурсте (+~500 групп), но VK **мягко троттлит до пустого** (`count:0` без ошибки) при бурсте. Гонять РЕДКО, мало терминов. |
| `--crawl-subscriptions` | `groups.getMembers(managers)` → `groups.get(user)` | **Не работает** обычным токеном (VK error 15 «you should be a group administrator»). Оставлен под админ-токен/будущее. |

Тонкая настройка: `--main-group-posts`, `--newsfeed-count`, `--crawl-max-seeds/-managers`.
Пример запуска района — в `scripts/_run_mi.sh` шаблоне (scratch, не коммитим).

**Грабли locality-скоринга:** наивный стем ловит омонимы — `Калинино`→`калинин`
матчит фамилию «Калинина»; `Старый/Новый/Большой` тянут чужие «Старый Оскол» и т.п.
Нейро-классификация (Шаг 4) их отсеивает — `matched_localities` слепо не доверяй.

**Что скил всё ещё упускает (Малмыж: recall ≈45% от ручного пула из 79 групп):**
крошечные сельские СДК/библиотеки (<100 подписчиков) VK-поиск не индексирует и нигде
не слинкованы — добираются только локальным знанием. Засев района — гибрид: скил даёт
основу (новостники, школы, поселения, сельские паблики), длинный хвост — руками.

## Шаг 4. Забрать результат и классифицировать в чате
```bash
scp sarafan:/tmp/scan.json ./_scan.json
```
`Read` файла (большой — читать страницами). Для **каждого** кандидата прочитай
`recent_posts` и реши. **Правила (нейро-классификация):**
- ✅ Брать: официальные ведомства/министерства региона, СМИ, профильные тематические
  и крупные UGC-новостники с реальной повесткой. `via`-метки НЕ доверять (это лишь
  поисковый запрос — часто врут).
- ❌ Отклонять: коммерция/магазины/барахолки/доски объявлений, чистая реклама,
  18+/жёлтый трэш, **чужой регион** (омонимы «Киров»/«Кировский»/«Кировск» — Калуга,
  СПб, Ленобласть, Пермь, Ставрополь и т.п.), мелкие местечковые (если нужен область).
- Строгость по запросу пользователя: «строго» (только явные новости/официальные) vs
  «охватом» (включать крупные UGC — дедуп-пайплайн всё равно режет рекламу).

## Шаг 5. Собрать seed-JSON
`[{"vk_id": <положительный>, "category": "<тема>", "name": "...", "screen_name": "..."}]`.
`vk_id` — положительный (как из groups.search); сидер сам пишет `-abs`.

⚠️ Сверку кандидатов с текущим пулом делать по **`abs(vk_id)`**: скан отдаёт
положительные id, в БД они отрицательные, и наивное сравнение даёт «уже в пуле: 0»
на всём списке — то есть выглядит как «90 новых», а не как ошибка сверки
(напоролось 20.10).

## Шаг 6. Засев (сначала dry-run!)
`communities.vk_id` хранится **отрицательным**; уникального constraint нет — сидер
дедуплит сам по (region, abs(vk_id), category).

Команда живёт в **файле-обёртке**, а не в строке `ssh`: env нужно прочитать под `sudo`,
а `cd ~/SETKA` под `sudo` уводит в `/root` (проверено 20.10 — команда падает на
`cd: /root/SETKA: No such file or directory`). Плюс `$(...)` и вложенные кавычки в
строке переживают два шелла и PowerShell хуже, чем путь к файлу.
```bash
# /tmp/run_seed.sh — содержимое:
#   set -euo pipefail
#   set -a; . /etc/setka/setka.env; set +a
#   cd "$(getent passwd "${SUDO_USER:-$(id -un)}" | cut -d: -f6)/SETKA"
#   exec ./venv/bin/python /tmp/seed_region_communities.py --region-code <code> --file /tmp/seed.json "$@"
scp scripts/seed_region_communities.py ./seed.json ./run_seed.sh sarafan:/tmp/
ssh sarafan "sudo bash /tmp/run_seed.sh --dry-run"   # DRY-RUN
ssh sarafan "sudo bash /tmp/run_seed.sh"             # WRITE — после проверки вывода
```
(env читается под `sudo`, т.к. обычный пользователь `/etc/setka/setka.env` не видит.)

## Шаг 6б. Снятие чужих доноров (если пул был клоном или разъехался)

Добор и снятие — **разные шаги, и порядок обязателен: сначала добор, потом снятие**
(решение владельца 20.10). Снять чужих раньше, чем набраны свои, значит оставить район
без доноров.

Признаки, что пора: у соседних районов совпадает размер пула и состав;
своего района в пуле меньше половины; в пуле лежит **наша собственная** группа
(`regions.vk_group_id`) — тогда район собирает наши же сводки.

```sql
-- 1) один пул у соседей (одинаковый состав = клон)
SELECT c.vk_id, count(*) AS n, string_agg(r.code, ', ' ORDER BY r.code)
FROM communities c JOIN regions r ON r.id = c.region_id
WHERE c.is_active GROUP BY c.vk_id HAVING count(*) > 1 ORDER BY n DESC;
-- 2) наша группа как донор (должно быть пусто)
SELECT rd.code AS group_region, rp.code AS in_pool_of, c.vk_id, c.name
FROM communities c
JOIN regions rp ON rp.id = c.region_id
JOIN regions rd ON rd.vk_group_id = c.vk_id
WHERE c.is_active;
```

Снятие — [`scripts/deactivate_communities.py`](../../scripts/deactivate_communities.py)
(мягко: `is_active=false` + `disabled_reason`, строка и статистика остаются):
```bash
scp scripts/deactivate_communities.py ./off.json ./run_off.sh sarafan:/tmp/
ssh sarafan "sudo bash /tmp/run_off.sh --dry-run"   # DRY-RUN: покажет ровно то, что погаснет
ssh sarafan "sudo bash /tmp/run_off.sh"             # WRITE — под гейтом #025, список владельцу
```
`off.json` — JSON-список id (знак не важен). Причины: `pool_foreign_district`
(чужой район), `pool_own_group` (наша группа), `pool_not_news` (коммерция/не новости).
⚠️ `disabled_reason='dormant_t1_auto'` **зарезервирован**: ежемесячный recheck вернёт
такие строки в пул, и снятие окажется мнимым — скрипт такую причину не принимает.

## Шаг 7. Проверка
```bash
ssh sarafan "sudo -u postgres psql -d setka -tA -c \"SELECT category, count(*) FROM communities WHERE region_id=(SELECT id FROM regions WHERE code='<code>') AND is_active GROUP BY category ORDER BY count(*) DESC;\""
```

## Шаг 8. Уборка
- Прод `/tmp`: `ssh sarafan 'rm -f /tmp/discover_scan.py /tmp/seed_region_communities.py /tmp/queries.json /tmp/scan.json /tmp/run.sh /tmp/seed.json'`.
- Локально: `_scan*.json`, `_*.json`, `_run*.sh` — **scratch, не коммитить** (в `.gitignore`
  или удалить). Коммитим только `scripts/discover_scan.py` и `scripts/seed_region_communities.py`.

## Таксономия тем: канон (что читает дайджест) ↔ легаси

Дайджест отбирает по `Community.category == <theme>`, где theme = **канон** (beat-слоты
`postopus-<theme>-*` в `tasks/celery_app.py`). Легаси-вокабуляр `modules/region_config.py`
(`CommunityCategory`) в БД-пул **НЕ синкается** (`sync_region_settings.py` пишет только
поля региона) — **сей строго в канон**, иначе дайджест группу не увидит:

| Канон (сеять так) | Легаси `region_config` | Что входит |
|---|---|---|
| `novost` | news | новостные паблики, сельские сообщества |
| `reklama` | advertising | объявления, барахолки |
| `kultura` | culture | ДК, СДК, библиотеки, музеи, храмы |
| `sport` | sports | ДЮСШ, клубы, секции |
| `admin` | administration | админ. района/поселений, сельские думы |
| `detsad` | preschool_education | детсады |
| `union` | youth/entertainment | школы, техникумы, Движение Первых, ветеранские орг. |
| ~~`sosed`~~ | — | **снята 2026-09-14** — тема без расписания, источники под неё не добираем |

Район использует это подмножество (без областных `proisshestviya/zhkh/selhoz/nauka/…`).

## Замечания
- Для областей пул «спит», пока не сделан рефактор публикации (`digest_mode='communities'`).
- Periodic-добор: сидер идемпотентен — повторный прогон ничего не дублирует.
- Можно резолвить точные хэндлы напрямую: `groups.getById` принимает `screen_name`.
