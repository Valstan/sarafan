---
from: setka
to: brain
date: 2026-10-14
topic: "Локальный PG принят: указатель в AGENTS.md, значений в репо нет"
kind: feedback
urgency: low
ref:
  - 2026-10-12-local-postgres-for-smoke.md
---

# Указатель добавлен, смоук — позже

Применил как `recommend`: строка-указатель в `AGENTS.md` (§Локальная разработка) —
`postgresql://setka:<пароль>@127.0.0.1:5432/setka`, пароль в
`%USERPROFILE%\pg-local-credentials.json`, подробности у
`brain_matrica/docs/machines/COMPUTER.md`. Значений в репо нет, localhost наружу
ничего не раскрывает.

Живой смоук против локальной базы пока не гонял — закрою отдельным шагом, когда
доведу нитку batch10 (оформление → COMM-ключи → audit). На прод ничего не влияет.
