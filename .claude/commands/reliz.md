---
description: Релиз SETKA на прод — PENDING (если нужно) → описательный commit + PR → SSH git pull → миграции (если есть) → restart → проверки.
argument-hint: [короткое описание релиза, опционально]
allowed-tools: Read, Edit, Write, Bash, Glob, Grep, AskUserQuestion, mcp__ccd_session__mark_chapter
---

# /reliz — релиз правок на прод SETKA

Ведёт через все шаги один за другим. На каждом значимом шаге останавливается и проверяет с пользователем. Прод-операции через SSH (см. `docs/REMOTE_ACCESS.md`).

## Шаг 0. Pre-flight check

Параллельно:

```bash
git status --short --branch                 # что меняется
git diff --stat HEAD                        # объём
git log --oneline main..HEAD 2>/dev/null    # что в текущей ветке, если не main
git log --oneline -5                        # последние коммиты
```

Если рабочее дерево чистое и нет несмердженых коммитов в feature-ветке — сказать «коммитить нечего», выйти.

## Шаг 1. Качественные ворота

Гейты безусловны (канон `AGENTS.md` §Автономия под гейтами) — без вопроса и без варианта «пропустить»:

```bash
.\venv\Scripts\python.exe -m pytest tests/ -q 2>&1 | tail -15
pre-commit run --all-files 2>&1 | tail -30
```

(на Linux/worktree — `./venv/bin/python` соответственно).

Если что-то падает — стоп, показать вывод, спросить как поступить. **Не использовать** `--no-verify` / `pytest -k` для обхода без явного запроса пользователя.

## Шаг 2. Подготовить описательный commit message + PENDING_FOLLOWUPS

С 2026-05-24 хронология ведётся через git ([ADR-0001](../../docs/adr/0001-archive-dev-history.md), `docs/DEV_HISTORY.md` упразднена). Описательное тело коммита + PR-description заменяют старую запись. **Это критично — не пропускать.**

1. **Commit message** должен включать:

```
<type>(scope): <subject под 70 символов>

Что меняли (файлы, поведение).
Почему (контекст, мотивация).
Какие тесты прошли (N/N зелёных).
Как применять на проде (миграция? restart? pip install -e .? ничего?).
Какие хвосты остаются — ссылка на PENDING_FOLLOWUPS.

Co-Authored-By: <агент и его фактическая версия> <noreply@anthropic.com>
```

2. **`docs/PENDING_FOLLOWUPS.md`**: `Read`, и если что-то из закрываемой задачи висело в ⏳/🟡/🟢 — `Edit` убрать (или пометить `~~strikethrough~~` с пометкой «закрыто в PR #N»). Если в процессе вылезли новые техдолги — `Edit` добавить.

Делать это **до** коммита, чтобы попало в тот же коммит.

## Шаг 3. Commit + ветка

**PR-only flow** — direct push в `main` запрещён ([ADR-0002](../../../brain_matrica/adr/0002-pr-only-flow-no-direct-push.md), [POSTULATES §VI](../../../brain_matrica/docs/POSTULATES.md)). Исключение — hot-fix аварии прода (см. ниже §Hot-fix).

Если сейчас на `main` — создать feature-ветку **до коммита**:

```bash
# Slug — kebab-case, описательный. Префикс — по сути правки.
git checkout -b <type>/<slug>   # feat/, fix/, chore/, docs/, refactor/
```

Сообщение коммита составить самому по `git diff --stat` и содержанию правки (описательное, без вопроса пользователю). Conventional-commits prefix:

- `feat(scope):` — новая фича
- `fix(scope):` — баг-фикс
- `refactor(scope):` — рефакторинг без смены поведения
- `docs:` — только документация
- `chore:` — обслуживание (deps, configs)
- `test:` — только тесты

```bash
# Конкретные пути, НЕ git add -A
git add docs/PENDING_FOLLOWUPS.md <other-paths>

git commit -F <scratchpad>/commitmsg.txt
```

Сообщение **написать файлом** (`Write` в scratchpad), а команде отдать путь — heredoc
и `-m` с переносами запрещены каноном (`AGENTS.md` §«Локальная разработка», заказ
владельца 2026-08-25). Содержимое `commitmsg.txt`:

```text
feat(scope): краткое описание

Опционально — тело с подробностями (что и почему).

Co-Authored-By: <агент и его фактическая версия> <noreply@anthropic.com>
```

После коммита проверить `git log -1 --format='%s'` — subject не должен быть `@` или
первой строкой тела (G190 мозга).

Покажи пользователю `git log -1 --stat` для подтверждения.

## Шаг 4. Push + PR

```bash
git push -u origin <type>/<slug>

gh pr create --title "<short subject, под 70 символов>" --body-file <scratchpad>/prbody.md
```

Содержимое `prbody.md` (тоже файлом, не heredoc'ом):

```markdown
## Summary
- bullet 1
- bullet 2

## Test plan
- [ ] pytest tests/ -q — N/N зелёных
- [ ] pre-commit run --all-files (если правка кода)
- [ ] /check skill после merge

🤖 Generated with [Claude Code](https://claude.com/claude-code)
```

Покажи пользователю URL созданного PR и `gh pr diff` для финального review. **Без явного OK пользователя на diff — не мержить.**

После OK — сначала дождаться **обязательной** проверки, потом мержить:

```bash
# Защита main требует ровно `test (3.12)` (директива brain 2026-08-17, сверено с
# сервером 2026-08-20). Merge отклоняет СЕРВЕР, а не интерфейс, и правило
# действует на владельца тоже (enforce_admins: true).
gh pr checks --required --watch --interval 15 \
  && gh pr merge --squash --delete-branch \
  && git checkout main && git pull --ff-only
```

Проверка красная — **релиз останавливается здесь**. Не `--admin` (при `enforce_admins: true`
отклоняется и у владельца), не `--auto` (в репозитории `allow_auto_merge: false`), не перезапуск CI
руками. Красный гейт значит «чинить причину», и до её устранения на прод не выкатываем ничего:
непрошедший CI — это ровно тот случай, ради которого гейт и ставили.

Merge-стратегия по умолчанию `--squash` (для коротких PR в 1-3 коммита). Длинные линейки коммитов, где история ценна — `--merge` вместо `--squash` (спросить пользователя).

### Hot-fix исключение

Прод упал, нужно зафиксить в течение часа? Допустим direct push в `main` ([ADR-0002 §8](../../../brain_matrica/adr/0002-pr-only-flow-no-direct-push.md)), **но обязательный follow-up PR постфактум** с описанием инцидента (для audit trail). Спросить пользователя через `AskUserQuestion`: «Это hot-fix аварии прода? (иначе — через PR)».

## Шаг 5. Прод-доступ

Деплой не-схемных правок идёт без вопроса (канон). SSH-подтверждение — только если его требует фильтр инструмента на этой машине; это удобство, не гейт.

## Шаг 6. Прод: pull кода

```bash
ssh sarafan "cd ~/SETKA && git fetch --all && git log --oneline HEAD..origin/main 2>&1 | head -10"
```

Показать пользователю diff. Если есть конфликты или нет fast-forward — стоп, разобраться вручную.

Если всё чисто:

```bash
ssh sarafan "cd ~/SETKA && git pull --ff-only origin main && git log --oneline -3"
```

## Шаг 7. Миграции БД (если есть)

Проверить, есть ли в pushed-коммитах новые SQL-миграции:

```bash
git diff --name-only HEAD~1 HEAD -- 'database/migrations/*.sql' 2>&1
# или, если несколько коммитов:
git log --since=<previous-prod-commit> --name-only --diff-filter=A -- 'database/migrations/*.sql'
```

Если есть — для каждой:

1. `Read` файл.
2. `AskUserQuestion`: «Применить миграцию <NNN_file.sql> на прод?» с опциями «да / dry-run / отмена».
3. При «да» — через `/sql migrate <file>` или эквивалентно:
   ```bash
   ssh sarafan 'sudo -u postgres psql -d setka -f ~/SETKA/database/migrations/<file>'
   ```
4. Зафиксировать факт применения в commit message следующего коммита (если ещё не указали).

Если в pull притянулся `requirements.txt` — тогда:

```bash
ssh sarafan "cd ~/SETKA && source venv/bin/activate && pip install -r requirements.txt 2>&1 | tail -10"
```

Если в pull притянулся `pyproject.toml` (`git diff --name-only HEAD~1 HEAD -- pyproject.toml`) — переустановить editable пакет:

```bash
ssh sarafan "cd ~/SETKA && source venv/bin/activate && pip install -e . 2>&1 | tail -5"
```

Это регистрирует `setka` как editable-пакет в venv, чтобы `from modules.X import Y` работало из любой папки без `sys.path.insert`. Прод-systemd-сервисы используют `PYTHONPATH=~/SETKA`, ничего там менять не нужно.

## Шаг 8. Restart сервисов

Рестарт — без вопроса, по составу правки: `setka` (web/шаблоны/middleware), `setka-celery-worker` (tasks/modules), `setka-celery-beat` (расписание), `setka-vk-bot` (бот). Только тесты/доки — рестартов нет.

```bash
ssh sarafan "sudo systemctl restart <services> && sleep 4 && systemctl is-active <services>"
```

После рестарта **дождаться готовности web поллингом**, а не одиночным curl —
на тонком VPS (1 ядро / 1.5 ГБ) при рестарте нескольких сервисов uvicorn
встаёт >5с, и одиночный `curl` ловит `000` — ложный фейл деплоя. Если рестартили `setka` (web):

```bash
ssh sarafan "cd ~/SETKA && ./venv/bin/python scripts/wait_for_health.py --timeout 90 --interval 3"
```

Exit 0 — web поднялся (health 200). Exit 1 — не поднялся за 90с: **тогда**
смотреть журнал (Шаг 9 / откат Шаг 10), а не рестартить вслепую.

## Шаг 8.5. Smoke-test пайплайна (dry-run)

После рестарта — за один шаг проверить, что пайплайн **живой** (токены валидны, VK
отвечает, парсинг → фильтр → сборка дайджеста проходят), **ничего не публикуя**.
Использует `scripts/smoke_test.py` поверх seam'а `parse_and_publish_theme(dry_run=True)`
(PR #122): ставит diagnostics-задачу эталонного региона и опрашивает по `task_id`.

Пропускать, если деплой был **без рестарта worker/beat** (только docs / web-статика /
тесты) — тогда пайплайн не затронут. Рестартили worker/beat — прогонять без вопроса:

```bash
ssh sarafan "cd ~/SETKA && ./venv/bin/python scripts/smoke_test.py --region mi --theme novost"
```

Exit 0 — пайплайн жив (в stderr: `posts_parsed=…, would_publish=…`). Exit 1 — провал
(не спарсилось постов / `success=False` / таймаут): **показать вывод, разобраться** —
частые причины: все READ-токены в cooldown, VK error на токене, пустой пул региона.
Exit 2 — сетевая ошибка/нет `task_id` (API не поднялся после рестарта → к Шагу 9 откат).
Эталон по умолчанию — `mi`/`novost` (флагман с активным пулом); при желании задать
другой регион/тему `--region <code> --theme <theme>` или ослабить порог `--min-posts 0`.

## Шаг 9. Проверки

Параллельно:

```bash
# Поллер (Шаг 8) уже дождался 200; этот вызов вернётся сразу, если web жив.
ssh sarafan "cd ~/SETKA && ./venv/bin/python scripts/wait_for_health.py --timeout 30 --interval 3"

ssh sarafan "systemctl is-active setka setka-celery-worker setka-celery-beat setka-vk-bot"

ssh sarafan "journalctl -u setka -u setka-celery-worker -u setka-celery-beat --since '2 minutes ago' --no-pager 2>&1 | grep -iE 'error|critical|exception' | tail -10"

ssh sarafan "tail -50 ~/SETKA/logs/uvicorn_production.log 2>&1 | grep -iE 'error|critical|exception|traceback' | tail -5"

# Демон бота пишет в файл, а не в journal: падение на ImportError видно только здесь
ssh sarafan "test -f ~/SETKA/logs/vk-bot.log && tail -50 ~/SETKA/logs/vk-bot.log | grep -iE 'error|critical|exception|traceback' | tail -5 || echo 'vk-bot.log: MISSING'"
```


## Шаг 9.5. Зеркало секретов в комнату КАРМАНа (P038)

Только если этот релиз **выпускал или ротировал секрет** (VK-токены, COMM-ключи,
секреты ESA-клиентов, гранты): тем же ходом положить копию в свою комнату
bulk-upsert'ом — иначе зеркало отстаёт молча, а восстанавливает старое тоже
молча. Ротаций не было — пропустить молча, шаг ничего не делает.

Рецепт — шаг 4 из `brain_matrica/docs/KARMAN_ROOM.md` (читать там, адрес и
токен — только с прод-бокса, в репо им не место). После — сверить чтением:
запросить обратно перечислением всех ожидаемых имён (промах любого — 404
с именами). Значения секретов — никогда в чат, лог и коммит.

## Шаг 10. Финальный отчёт

- Что коммитнули (`git log -1 --stat`)
- Что задеплоено (на проде новый коммит `<hash> <subject>`)
- Какие миграции применены (если были)
- Какие сервисы перезапущены
- Результат smoke-test (Шаг 8.5), если прогоняли
- Результаты health-проверок
- Если в `PENDING_FOLLOWUPS.md` остались хвосты — напомнить какие

## Если что-то упало

- **Тесты упали** → стоп до коммита, разобраться. **Никогда не** обходить через `--no-verify`.
- **psql упал на миграции** → откатить если можно (`BEGIN; ... ROLLBACK;` либо обратная миграция). Зафиксировать в `PENDING_FOLLOWUPS.md` как 🔴.
- **Сервис не запускается после restart** → `journalctl -u <service> -n 100 --no-pager`. Чаще всего — синтакс/импорт ошибка от свежего коммита. Откатить prod-репо: `ssh sarafan "cd ~/SETKA && git reset --hard <prev-hash>"` + restart. **Только с явным «да» пользователя через AskUserQuestion.**
- **`/api/health/full` отвечает 500** → тоже самое: журнал, откат.

Никогда не оставляй прод в сломанном виде. Если не можешь починить за 5 минут — спроси «откатываемся?», и при «да» выполни откат.
