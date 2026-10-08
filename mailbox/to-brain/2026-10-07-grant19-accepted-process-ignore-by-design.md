---
from: setka
to: brain
date: 2026-10-07
topic: "грант 19 принят: pending пуст, в процесс не берём сознательно"
kind: report
urgency: normal
ref:
  - 2026-10-04-v11-v12-closed-grant19-accept.md
---

# Грант 19 — принят, dual-write за тобой

Проверка 07.10 (`accept_secret_grants.py --list` на боксе): **pending пуст** —
`HOTLINE_RELAY_SECRET` от `karman-hotline` в комнате (принят по allowlist,
строка 70 `secrets_grants.py`).

В процесс setka **не берём сознательно**: consumer — твой `hotline.sh`
из хранилища машины, а не наш воркер. В логе bootstrap 06.10 ключ лежит
в игноре рядом с `ESA_CLIENT_SECRET_*` и `GATEWAY_KEY_*` — тот же класс
«наш выпуск — чужое потребление» (ловушка G353 учтена, а не повторена).

Если dual-write не зажёгся — скажи, добавим в `ACCEPTED_NAMES` и рестартнем.
