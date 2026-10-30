# Session Handoff

> Sticky-note для непрерывности между сессиями разработки SETKA. Перезаписывается через [`/close_session`](../.claude/commands/close_session.md) — историю смотри через `git log --follow -- docs/SESSION_HANDOFF.md`.

**Status:** ACTIVE
**Updated:** 2026-10-30
**Branch:** `main`
**Last release in prod:** `ad9c48b` (#990, 29.10 ~01:41 MSK: `high` вариант, 4 сервиса `active`, health 200, `deepseek-v4.1-flash-free`, 2.5 с; перед ним — `017d317` — `high` вариант по слову владельца, перезапуск `setka` + `setka-celery-worker` + `setka-celery-beat`).

---

## Текущая нитка

**Процедура `/sites` (обслужить 4 сайта: `vmalmyzhe`, `kazanskaya`, `sabantuy`, `kultura`) — заблокирована отсутствием SSH-доступа к `sarafan`.**

Проверено в этой сессии (`docs/SESSION_REPORT_SITES.md`):
- `~/.ssh/config` — без записи `sarafan` (только `es-vps`);
- ключ `~/.ssh/id_rsa` (ED25519, ключ доступа к `sarafan`, [`P150`](pending/P150-d-035-pilot-prod-ssh-klyuchey-iz-komnaty-karmana.md) п.31) — отсутствует (`~/.ssh/` содержит только `id_ed25519_es_vps`);
- `ssh sarafan "test -f ~/SETKA/main.py"` с созданной записью (`Host sarafan`, `HostName 3931b3fe50ab.vps.myjino.ru`, `User valstan`, `Port 22`, `IdentityFile ~/.ssh/id_ed25519_es_vps`) → `Permission denied (publickey)`;
- ключ `SSH_KEY__sarafan__rmz4val`, предоставленный через `question` (публичная часть `ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIH+HBE27c0206kJ1c6ObFSJoPrdtA1ddvrEwT3TeyLaY valstan@setka`), не содержит приватной части (вместо тела ключа пришла только строка-ограничитель начала ключевого блока OPENSSH) и `ssh-keygen -y` падает с `error in libcrypto` — ключ повреждён или обрезан.

Без доступа к `sarafan` (SSH) не выполнены ключевые шаги процедуры (`.claude/commands/sites.md`): `--emit` (нужна прод-БД + `DATABASE_URL` из `/etc/setka/setka.env`), `--check` / `--deliver` (нужен `SECRETS_TOKEN` из `/etc/setka/secrets-token.env` для `bootstrap_secrets()` в `scripts/conveyor_manual_deliver.py`, иначе `no_key` при живом ключе в комнате, [`P185`](pending/P185-kultura-standard-delivery-echelon.md) п.29–35).

**Что сделано локально в сессии:**
- `git fetch` → `pull --ff-only` до `ad9c48b` (11 коммитов впереди `main` до сессии);
- прочитаны правила всех 4 сайтов (`rules/_base.md`, `vmalmyzhe.md`, `kazanskaya.md`, `sabantuy.md`, `kultura.md`);
- `PENDING` сверено: `P185` — подключение Культуры завершено (грант, SITES в 31 рубрику, PR #783/#787), ручные досылки 03–04.10 и 11.10 прошли, автоматический поток заблокирован `P177` (`DeepSeek 402` с 22.09); `P075` — конвейер работает (`CONVEYOR_SITES=vmalmyzhe`, `VMALMYZHE_INGEST_KEY` живой); `P066` — закрыто 2026-08-28; `P141` (`kazanskaya`: ключ принят, но `CONVEYOR_SITES` не проверено 11.10 — `watch`); `P140` (`sabantuy`: ключ принят, сайт включён, материал `0` — ждёт `DeepSeek` + материал);
- подготовлен отчёт `docs/SESSION_REPORT_SITES.md` (состояние, блокер, варианты восстановления доступа, наблюдения из `P177`/`P141`/`P140`).

**Гейт `#025` (автономия под гейтами, `AGENTS.md` §«Автономия под гейтами», [`sites.md`](.claude/commands/sites.md) п.87–91):** `vmalmyzhe` и `kultura` — публичные сайты (`publish: true` для `vmalmyzhe`, `KULTURA_PUBLISH_KEY` для `kultura`). Любой `--deliver` — destructive-операция с внешними эффектами: **требуется явное «да» пользователя в том же ходе** (`AskUserQuestion`).

## Следующий шаг

**Первое действие следующей сессии:**

1. **Восстановить SSH-доступ к `sarafan`** (одним из путей):
   - найти/восстановить ключ `~/.ssh/id_rsa` (ED25519) из бэкапа или другой машины владельца (`P150` п.31);
   - или получить полный приватный ключ `SSH_KEY__sarafan__rmz4val` из комнаты КАРМАНа (`scripts/accept_secret_grants.py` требует `SECRETS_TOKEN` из `/etc/setka/secrets-token.env` — в этой сессии отсутствует, `env | grep SECRETS` пусто);
   - или создать новый ключ (`ssh-keygen -t ed25519 -f ~/.ssh/id_rsa_sarafan`) и добавить публичный ключ в `~/.ssh/authorized_keys` на `sarafan` (нужен текущий канал доступа).
2. После восстановления доступа — выполнить `/sites` полностью (`.claude/commands/sites.md` п.39–85):
   - `ssh sarafan "... --site vmalmyzhe --emit /tmp/sites-<DD>-vmalmyzhe.json"`;
   - `scp` файла вниз, ручной разбор глазами по `rules/*.md`;
   - `scp` вердиктов обратно → `--check` → `--deliver` (под гейтом `#025`: явное «да» пользователя для `vmalmyzhe` и `kultura`);
   - проверить состояние `CONVEYOR_SITES` на проде (`P141`: `kazanskaya` всё ещё не в `CONVEYOR_SITES` на 11.10, `watch`).
3. Зафиксировать результат в `docs/pending/P185-*.md` (новая `✅`-строка: дата, remote_id, секция, наблюдения) и в `docs/ops/pilot-qa-log.md` (ответы на вопросы прогонов, [`sites.md`](.claude/commands/sites.md) п.79–85).

**Если восстановление доступа невозможно в ближайшей сессии** — выполнить локальную подготовку (вариант Б из `docs/SESSION_REPORT_SITES.md`): проверить `scripts/conveyor_manual_deliver.py` (`bootstrap_secrets()` обязателен, иначе `no_key` при живом ключе в комнате), подготовить шаблон вердиктов для ручной доставки на основе правил `rules/*.md` и истории `P185` (последние `accept`: `kultura` — `dshi`, `kalinino`, `kraevedcheskiy-muzej`, `konstantinovka` с 06.10; `vmalmyzhe` — `novosti` ×2 из 19.10: `179280169_23174`, `207566395_4873`, школа `217735862_1295` от 07.10).

## Контекст

- **План:** [`OPTIMIZATION_PLAN_2026-10.md`](OPTIMIZATION_PLAN_2026-10.md) (Ф2 — квота, Ф3 — гигиена); нитка `/sites` — отдельная, не часть плана оптимизации.
- **Связанные коммиты сессии:** `git log --oneline` за 30.10 — только `pull` до `ad9c48b` (11 коммитов с `main` до сессии), создание `docs/SESSION_REPORT_SITES.md` (незакоммичено в текущей сессии — будет в закрывающем PR).
- **Прод:** 4 сервиса `active` (`ad9c48b` / `017d317`, 29.10 ~01:41 MSK). `P177` (`DeepSeek 402` с 22.09) — блокер автоматической доставки для всех 4 сайтов; ручной режим (`scripts/conveyor_manual_deliver.py`) — штатный путь, пока движок не оплачен или не восстановлен.
- **Открытых PR:** нет (`#988` влит 28.10, `#990` влит 29.10, `#991` будильник, `#992` вариант `high`).
- **PENDING (изменения в сессии):** не добавлено новых записей (индекс `PENDING` — 51168 Б из 51200, новая запись не влезает, только правка существующих); `P185` остаётся `watch` с остатком «автоматический поток заблокирован `P177`»; `P141` (`kazanskaya`) остаётся `watch` с остатком «сверка `CONVEYOR_SITES` на проде».

## Failed approaches (этой нитки)

- **`ssh sarafan` с ключом `id_ed25519_es_vps`** — подключение как `valstan` к `81.177.6.46` (`3931b3fe50ab.vps.myjino.ru`) даёт `Permission denied (publickey)`; ключ `id_rsa` (ED25519) отсутствует в `~/.ssh/`.
- **Создание записи `Host sarafan` в `~/.ssh/config`** — запись создана (`HostName 3931b3fe50ab.vps.myjino.ru`, `User valstan`, `Port 22`, `IdentityFile ~/.ssh/id_ed25519_es_vps`), но ключ не подходит.
- **Ключ `SSH_KEY__sarafan__rmz4val` из КАРМАНа** — пользователь выбрал вариант «Получить ключ из КАРМАНа», но предоставил публичную часть (`SSH_PUB__sarafan__rmz4val`: `ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIH+HBE27c0206kJ1c6ObFSJoPrdtA1ddvrEwT3TeyLaY valstan@setka`), а не приватную (`SSH_KEY__sarafan__rmz4val`); приватная часть, предоставленная в следующем ходе (пришли только две строки-ограничителя, начало и конец ключевого блока OPENSSH, а тела ключа между ними не было), повреждена или обрезана (`ssh-keygen -y` → `error in libcrypto`).
- **`SECRETS_TOKEN`** — отсутствует в текущем окружении (`env | grep SECRETS` пусто, `.env.example` — только шаблон, `/etc/setka/` на `es-vps` — отсутствует), поэтому `scripts/accept_secret_grants.py` или `GET /api/secrets` недоступны без предоставления токена из другого источника.

## Открытые вопросы для пользователя

- **Восстановление доступа к `sarafan`:** предоставить полный приватный ключ `SSH_KEY__sarafan__rmz4val` (без обрезки) или `SECRETS_TOKEN` (из `/etc/setka/secrets-token.env` или комнаты КАРМАНа) — тогда `/sites` выполнится в следующей сессии.
- **`P210`, развилка:** платить за Go (`$10/мес`) или держать объём в рамках `free-тира`? Правки `#988`/`#990` обе не увеличивают квоту — они перестают её транжирить (`P210` «29.10»).
- **`P208`:** баланс `DeepSeek` (`http_402`) или RAM для второго слота (`P164`: OOM воркера, `mmap-сироты`, PR #866).
- **`P196` (SSH):** канал флэпает (`sshd` висит после вывода, `G455`); рабочий приём — локальный файл со скриптом + `bash -s` + `kill` клиента по таймауту; интерактивные `ssh "..."` с длинным выводом висят до таймаута.

## Не забыть (low-priority)

- `P177` (`DeepSeek 402`) — блокер автоматической доставки; ручной режим (`scripts/conveyor_manual_deliver.py`) — штатный путь до оплаты или восстановления движка.
- `P141` (`kazanskaya`) — ключ принят (`KAZANSKAYA_INGEST_KEY`), но `CONVEYOR_SITES` на проде не проверено с 11.10; перед `--emit` сверить (`PENDING` `watch`).
- `P140` (`sabantuy`) — `SABANTUY_INGEST_KEY` принят, сайт включён, сухой прогон `0` (материала нет); первая живая доставка ждёт `DeepSeek` + материал.
- `P186` (`kultura` дубль `vkPostId` со слепым минусом) — наша часть закрыта (`P150`); остаток — удаление черновика `1325` (просьба мозгу 30.09, не выполнено).
- `P150` — пилот `D-035` пройден (`SSH_KEY__sarafan__rmz4val` в комнате КАРМАНа); вопрос к схеме (`per-machine` ключи) — `parked`, условие расконсервации: решение владельца.

---

> Если читаешь это в начале новой сессии — обнови через `/close_session` в конце. **Не аккумулируй history тут** — она в `git log --follow -- docs/SESSION_HANDOFF.md`.
