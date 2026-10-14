# P195 — Muse geo-заблокирована с IP бокса

**Приоритет:** 🔴 (блокер цели «Muse на проде», не блокер прод-работы)
**Создано:** 2026-10-14
**Теги:** `⏱ 2026-10-14 · snooze 0 · parked`

## Симптом

Замер 14.10.2026 с VPS: `POST https://opencode.ai/inference/v1/responses`
(model=muse-spark-1.3-contributor-free) → `403 RegionError`:
«This model is not available in your country». Тот же вызов с машины
владельца — через его VPN-канал — работает. Остальные free-модели Zen
(longcat, space-bunny, mimo) с бокса отвечают; muse — нет.

## Причина

Geo-политика Muse (модель Meta) на стороне шлюза Zen привязана к IP.
IP бокса в EU-хостинге (myjino) под условие не подходит. Продолжение
бесплатного тира Muse — «contributor», диалоги идут в обучение моделей Meta
(условие бесплатности, проверять перед скармливанием личных данных постов).

## Почему parked, а не в работе

[ADR-0007](../adr/0007-opencode-classifier-engine.md): muse стоит ПЕРВОЙ в
цепочке фолбэков `OPENCODE_MODEL_CHAIN`. Сейчас вердикты выносит
longcat-2.5-preview-free (проверено на живой паре постов: publish 95% /
delete 90% с корректными обоснованиями). Прод работает. Muse — качественный
апгрейд, а не блокер.

## Условие расконсервации

Владелец решает один из путей (бос не критичен):

1. **Донарский VPS под ssh -D** (~200–400 ₽/мес): `ssh -D 1080 -N donor` в
   systemd-юните на нашем боксе, трафик харнесса — через
   `ALL_PROXY=socks5://127.0.0.1:1080`. Root на доноре не нужен, tun не
   нужен (OpenVZ, P163) — чистый userspace.
2. **Платный SOCKS5/HTTP-прокси** в нужной гео: быстро, но третья сторона
   видит текст постов.
3. **Отказаться от Muse** и остаться на longcat/space-bunny/mimo (free,
   zero-retention, ротация оживляема замером).

## Что проверить в момент расконсервации

- env-прокси на процессе харнесса (bun-fetch уважает `HTTPS_PROXY`/
  `ALL_PROXY`; serve унаследует от systemd);
- muse отвечает по `/responses` (endpoint отличается от chat/completions);
- качество вердиктов на 20 постах против longcat (agree-rate — панель
  `/classifier/stats`).
