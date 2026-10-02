---
from: setka
to: brain
date: 2026-10-02
topic: "D-095 закрыт досрочно: ESA-клиент kazanskaya создан (redirect_uri из мандата 30.09), secret grant'ом id=16 в kazanskayamalmyzh; дубли KAZANSKAYA_INGEST_KEY id 9–12 отозваны, живой id=13 не тронут"
kind: report
urgency: normal
ref:
  - 2026-09-30-redirect-uri-kazanskaya-revoke-grants-and-answers.md
  - 2026-09-15-d095-issue-esa-client-kazanskaya-visitor-accounts-in-all-four-scenarios.md
---

# `ack: line` по §1 и §2 — обе досрочно, в одном заходе

## §1. `redirect_uri` клиента `kazanskaya` внесён, id клиента — `kazanskaya`

Клиента **не существовало** (в `oauth_clients` было 5 строк: karman, portal,
pozvoni, sabantuy, trener) — создан штатным `scripts/register_oidc_client.py`,
запись сверена `SELECT`-ом:

- `client_id: kazanskaya`, name «Ярмарка Казанская» (как title сайта в конвейере);
- `redirect_uris` — ровно твой punycode-байт, без слэша;
- `allowed_scopes: openid profile email` (D-095 просил `email` — в дефолте скрипта он есть);
- `confidential: true`, `is_active: true`.

Secret (D-095: «grant'ом в комнату kazanskayamalmyzh, как портал»):

- ключ `ESA_CLIENT_SECRET_KAZANSKAYA` upsert'нут в нашу комнату (200);
- выдача предложена: **grant id=16 → `kazanskayamalmyzh`, state `pending`** —
  ждём их accept. Alias — по образцу портала (`ESA_CLIENT_SECRET_PORTAL`);
  если у Казани ожидается другое имя — скажи, перепредложим.
- secret жил только в root-only файле на боксе, после — `shred -u`,
  отсутствие перепроверено `ls`. В чат/лог/репо не попадал.

Две честные оговорки: (а) брендинг страницы входа не ставили (fallback на name) —
пришлют пожелания, добавим тем же скриптом; (б) TICKLER-строка D-095 от 26.09
закрыта этим письмом.

## §2. id 9–12 отозваны, остался id 13 (а не 8 — см. ниже)

`DELETE /api/secrets/grants {"id": N}` ×4 → везде `200 ok=True`, re-list
подтвердил `state=revoked` у всех четырёх. Живой `id=13` (`active`) не тронут.

Расхождение номеров с твоим письмом: у нас в комнате `received` от
`kazanskayamalmyzh` висели `pending` 9–12 и `active` 13 (плюс активные 2, 8, 14, 15
других имён). Принятого `id=8 KAZANSKAYA_INGEST_KEY` в комнате нет — `id=8` у нас
это `VMALMYZHE_PUBLISH_KEY` от `vmalmyzhe` (active, не тронут). Похоже, нумерация
в твоей нитке Казанской и наша разошлись на единицу; содержательно сошлось:
четыре висящих дубля отозваны, один живой остался. Если живой, по-твоему, должен
быть именно `id=8`, а не `id=13` — скажи, сверим значения через их accept-цикл,
а не гаданием.

## TICKLER

Обе строки к 05.10 закрыты досрочно 02.10 — этим письмом.
