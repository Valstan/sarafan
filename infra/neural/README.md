# neural — контур нейро-движка на нашем боксе

Воспроизводимая часть того, что стоит на прод-боксе 20.10 (см. ADR-0009 и
P205). Инструкция-первоисточник — `brain_matrica/docs/plans/neural-selfhost.md`
(read-only), здесь — только то, что мы добавили сверх неё: **сторож памяти**.

## Зачем сторож, если в юните есть `MemoryMax`

Потому что на этом боксе лимиты systemd **не исполняются**. Бокс — контейнер
OpenVZ (P163), а systemd внутри него поднялся без учёта памяти:

```text
systemctl show opencode-web -p MemoryAccounting -p MemoryCurrent
MemoryAccounting=no
MemoryCurrent=[not set]
```

`MemoryHigh`/`MemoryMax` в юните читаются как настройки, но cgroup-файлов нет
(`/sys/fs/cgroup/system.slice/opencode-web.service` отсутствует), ядро их не
применяет, и жертву при нехватке памяти выбирает OOM killer — не обязательно
движок (14.10 он убивал opencode при 290–620 МБ, но при другом раскладе
возьмёт uvicorn или celery, P164).

Поэтому движок держит **userspace-сторож**: таймер раз в 2 минуты смотрит
суммарный RSS процессов пользователя движка и перезапускает юнит, если порог
перейдён. Перезапуск дешёвый: сессии после каждого вызова удаляются, потеря —
один чанк волны (fail-open, волна идёт по обычным фильтрам).

## Файлы

| Файл | Куда на боксе |
|---|---|
| `opencode-web.service` | `/etc/systemd/system/opencode-web.service` (пример; uid подставить) |
| `opencode-rss-guard.sh` | `/usr/local/lib/neural/opencode-rss-guard.sh` (755) |
| `opencode-rss-guard.service` | `/etc/systemd/system/` |
| `opencode-rss-guard.timer` | `/etc/systemd/system/` |

## Установка

```bash
install -m755 opencode-rss-guard.sh /usr/local/lib/neural/
install -m644 opencode-rss-guard.service opencode-rss-guard.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now opencode-rss-guard.timer
systemctl list-timers opencode-rss-guard --no-pager
```

Порог задаётся `Environment=LIMIT_MB=...` в юните сторожа (по умолчанию 400).

## Проверка перед тем, как верить лимитам

Одна команда, которую стоит помнить на любом боксе:

```bash
systemctl show <unit> -p MemoryAccounting
```

`no` — лимиты не действуют, нужен внешний сторож. `yes` — действуют.
