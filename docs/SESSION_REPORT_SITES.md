# Отчёт: «Обслужи сайты» — процедура `/sites` заблокирована отсутствием SSH-доступа к прод-серверу

## Что просил пользователь
«Обслужи сайты» (повтор 7 раз) — это триггер процедуры [`/sites`](.claude/commands/sites.md): один заход = отбор → разбор глазами → сверка → доставка без дублей (4 сайта: `vmalmyzhe`, `kazanskaya`, `sabantuy`, `kultura`).

## Почему не выполнено полностью
**SSH-алиас `sarafan` не работает в этой сессии.** Проверено:

- `~/.ssh/config` содержит только `Host es-vps` (`162.141.93.61`), записи `sarafan` **нет**.
- Ключ `~/.ssh/id_rsa` (ED25519, историческое имя для доступа к `sarafan`, см. [`P150`](docs/pending/P150-d-035-pilot-prod-ssh-klyuchey-iz-komnaty-karmana.md) п.31) **отсутствует** в `~/.ssh/` — есть только `id_ed25519_es_vps`.
- Прямое подключение как `valstan` к `3931b3fe50ab.vps.myjino.ru` (`81.177.6.46`, хост `sarafan`) с `id_ed25519_es_vps` даёт `Permission denied (publickey)`.
- `dev-doctor.sh` рапортует: `alias 'sarafan' не найден в ~/.ssh/config` (`WARN`).

Без `ssh sarafan` нельзя выполнить ключевые шаги процедуры:
- `--emit` (нужен `DATABASE_URL` из `/etc/setka/setka.env` и доступ к прод-БД);
- `--check` / `--deliver` (нужен `SECRETS_TOKEN` из `/etc/setka/secrets-token.env` для bootstrap ключей сайта из комнаты КАРМАНа, см. `scripts/conveyor_manual_deliver.py` п.29–35).

Это **не отказ от работы**, а **блокер доступа** (#196 в `PENDING` — SSH-канал деградировал; решение — файловый редирект + kill, но для этого сначала нужен канал).

## Что сделано в рамках процедуры (локально, read-only)

### Шаг 0 — состояние (выполнено)
- `git status`: `main`, чисто.
- `git fetch`: `main` ушёл вперёд (`2f43a80` — `docs(p210): триггер деградации выкачен`, #988, 28.10).
- `PENDING` прочитан: [`P185`](docs/pending/P185-kultura-standard-delivery-echelon.md) — подключение Культуры завершено (грант, SITES в 31 рубрику, PR #783/#787), ручные досылки 03–04.10 и 11.10 прошли, автоматический поток заблокирован `P177` (DeepSeek `http_402` с 22.09); [`P075`](docs/pending/P075-multisaytovyy-konveyer-vk-sayty-klastera.md) — конвейер работает (`CONVEYOR_SITES=vmalmyzhe`), ключ `VMALMYZHE_INGEST_KEY` живой; [`P066`](docs/pending/P066-konveyer-vk-vmalmyzhe-rf-setka-v-vault-zhdem.md) — **закрыто** 2026-08-28 (поглощено `P075`).
- Правила для всех 4 сайтов прочитаны: [`rules/_base.md`](rules/_base.md) + [`rules/vmalmyzhe.md`](rules/vmalmyzhe.md) (портал: 7 рубрик, `skip_themes`: reklama/neighbors) + [`rules/kazanskaya.md`](rules/kazanskaya.md) (ярмарка, ключевые слова: казанская/ярмарк/карнавал) + [`rules/sabantuy.md`](rules/sabantuy.md) (праздник, `news`, ключевые слова: сабантуй и формы) + [`rules/kultura.md`](rules/kultura.md) (31 секция/slug учреждений, `publish_key_env`: `KULTURA_PUBLISH_KEY`).

### Шаг 1 — отбор (`--emit`) — **НЕ выполнен** (нужен прод-БД + env)
### Шаг 2 — разбор глазами — **готово к выполнению** (правила прочитаны, но файла emit нет)
### Шаг 3 — заливка/сверка/доставка (`--check`/`--deliver`) — **НЕ выполнен** (нужен `SECRETS_TOKEN` + ключи сайта из комнаты КАРМАНа)

### Шаг 4 — фиксация — **НЕ выполнен** (нет новых доставок для записи в `P185` или `pilot-qa-log.md`)

## Что нужно, чтобы закончить процедуру

### Вариант А: восстановить SSH-доступ (рекомендуется)
В [`P150`](docs/pending/P150-d-035-pilot-prod-ssh-klyuchey-iz-komnaty-karmana.md) указано, что ключ `~/.ssh/id_rsa` (ED25519) — это ключ доступа, и что `ssh sarafan 'hostname'` возвращает `3931b3fe50ab.vps.myjino.ru` / `valstan`. Возможные пути:

1. **Найти/восстановить ключ** `~/.ssh/id_rsa` (он отсутствует в этой среде, но может быть на другой машине владельца или в бэкапе).
2. **Создать запись в `~/.ssh/config`** с данными из документации (`Host sarafan`, `HostName 3931b3fe50ab.vps.myjino.ru`, `User valstan`, `Port 22`, `IdentityFile` — правильный ключ), но без ключа это не решит проблему.
3. **Получить ключ из комнаты КАРМАНа** (`SSH_KEY__sarafan__rmz4val`): `scripts/accept_secret_grants.py` или напрямую через `GET /api/secrets` с `SECRETS_TOKEN` (если токен есть в `/etc/setka/secrets-token.env` на боксе или в локальном env). Но без доступа к боксу это невозможно сделать из этой сессии.

После восстановления доступа — выполнить в одном SSH-блоке (см. [`sites.md`](.claude/commands/sites.md) п.39–72):

```bash
# Для каждого активного сайта (vmalmyzhe, kazanskaya, sabantuy, kultura)
ssh sarafan "sudo -n bash -c 'set -a; . /etc/setka/secrets-token.env; . /etc/setka/setka.env; set +a; cd /home/valstan/SETKA && venv/bin/python scripts/conveyor_manual_deliver.py --site <site> --emit /tmp/sites-<DD>-<site>.json'"
# Затем scp файла вниз, ручной разбор по правилам, scp вердиктов обратно, check, deliver
```

**⚠️ Гейт #025:** `vmalmyzhe` и `kultura` — это публичные сайты (`publish: true` для `vmalmyzhe`, `KULTURA_PUBLISH_KEY` для `kultura`). Любой `--deliver` на них — это destructive-операция с внешними эффектами: **требуется явное подтверждение пользователя в том же ходе** (`AskUserQuestion` по памятке [`sites.md`](.claude/commands/sites.md) п.87–91, [`AGENTS.md`](AGENTS.md) §«Автономия под гейтами»).

### Вариант Б: выполнить локальную подготовку (если доступ восстановить сейчас нельзя)
Могу подготовить всё, что не требует прод-БД:
- **Подготовить шаблон вердиктов** для ручной доставки (на основе правил `rules/*.md` и истории `P185`). Например, для `kultura` — 4 последних `accept` из журнала (`P185` п.66: `dshi`, `kalinino`, `kraevedcheskiy-muzej`, `konstantinovka`) — но это только фиксация истории, не новая доставка.
- **Проверить `scripts/conveyor_manual_deliver.py`** на наличие изменений или багов (например, `bootstrap_secrets()` — вызов обязателен, иначе `no_key` при живом ключе в комнате, [`P185`](docs/pending/P185-kultura-standard-delivery-echelon.md) п.29–35).
- **Обновить `docs/SESSION_HANDOFF.md`** или `docs/ops/pilot-qa-log.md` с текущим состоянием (если это часть задачи).

## Рекомендация следующего шага
1. **Восстановить `ssh sarafan`** (найти/создать `~/.ssh/config` + ключ `id_rsa` или получить `SSH_KEY__sarafan__rmz4val` из комнаты КАРМАНа).
2. После этого — запустить `/sites` по памятке (`.claude/commands/sites.md`): `--emit` для каждого из 4 сайтов, разбор глазами по правилам `rules/*.md`, `--check`, затем `--deliver` под гейтом `#025` с подтверждением пользователя.
3. Зафиксировать результат в `docs/pending/P185-*.md` и `docs/ops/pilot-qa-log.md` (новая `✅`-строка, ответы на вопросы — в `pilot-qa-log.md`, [`sites.md`](.claude/commands/sites.md) п.79–85).

## Дополнительные наблюдения из сессии
- `P177` (`DeepSeek 402`) — блокер автоматической доставки для всех сайтов. Ручной режим (`scripts/conveyor_manual_deliver.py`) — штатный путь, пока движок не оплачен или не восстановлен (`P177` п. «ручной конвейер сделан штатным CLI: scripts/conveyor_manual_deliver.py (30.09)`).
- `P141` — `kazanskaya`: ключ принят (`KAZANSKAYA_INGEST_KEY`), но сайт всё ещё не в `CONVEYOR_SITES` на проде (не проверено 11.10, `watch`). Перед запуском `--emit` для `kazanskaya` нужно сверить `CONVEYOR_SITES` на проде (`ssh sarafan "sudo -n grep -E 'CONVEYOR_SITES=' /etc/setka/setka.env"`, [`sites.md`](.claude/commands/sites.md) п.21–23).
- `P140` — `sabantuy`: `SABANTUY_INGEST_KEY` принят, сайт включён (`CONVEYOR_SITES` расширен), сухой прогон `0` — материала нет; первая живая доставка ждёт `DeepSeek` + материал (`P140`).

---

**Готов продолжать, как только восстановите SSH-доступ или выберете вариант Б (локальная подготовка).** Для варианта А — просто подтвердите «восстанавливаю доступ» или «запускай с текущим `es-vps` как прокси» (если это ваш способ), и я выполню полный прогон `/sites` с подтверждением каждого destructive-шага (`--deliver`) отдельно.
