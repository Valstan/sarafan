# neural — контур нейро-движка на нашем боксе

Воспроизводимая часть того, что стоит на прод-боксе 20.10 (см. ADR-0009 и
P205). Инструкция-первоисточник — `brain_matrica/docs/plans/neural-selfhost.md`
(read-only). Здесь — только то, что мы добавили сверх неё, потому что два шага
эталона на нашем боксе не сработали: **nft-redirect движка** и **лимиты
памяти systemd**.

## 1. Egress: почему не nft-redirect

Эталон заворачивает трафик uid движка в ВПН через `nft … redirect to :12345`.
На нашем боксе (контейнер OpenVZ) правило **устанавливается, но не действует**:

```text
meta skuid 1001 meta l4proto tcp counter redirect to :12345
counter packets 0 bytes 0          # ← ни одного пакета
```

Счётчик нулевой даже под нагрузкой, `nf_conntrack_count` всегда 0 — netfilter
в этом netns не отслеживает соединения, NAT-output не срабатывает. Ошибки нет,
конфиг выглядит настроенным: тот же класс отказа, что и с лимитами памяти
(см. §2) — настройка, которая «применяется», не применяясь.

**Рабочая схема:** HTTP-прокси перед socks-инбаундом xray.

```text
движок ──HTTPS_PROXY──> privoxy 127.0.0.1:8118 ──socks5──> xray 127.0.0.1:10808 ──> ВПН
```

- `privoxy-neural.conf` → `/etc/privoxy/config` (`listen-address 127.0.0.1:8118`
  + `forward-socks5t / 127.0.0.1:10808 .`);
- в юните движка: `Environment=HTTPS_PROXY=http://127.0.0.1:8118`,
  `HTTP_PROXY=…`, `NO_PROXY=127.0.0.1,localhost`, `After=/Wants=privoxy.service`;
- `neural-vpn` остаётся как в эталоне (xray + подписка), `neural-egress-redirect`
  стоит **disabled** — он бесполезен в контейнере.

Проверка, что движок реально ходит через ВПН, — не «правило в nft», а журнал
privoxy: после вызова там видно `Request: models.opencode.ai:443/`, а прямой
`curl` с бокса и `curl -x http://127.0.0.1:8118` дают **разные** адреса.

Замер 20.10: вызов модели через ВПН — 2–10 с (прямой выход — 2,7 с), смена
сервера (`/etc/neural/egress.d/10-nextgenvpn.sh`, PL → DE) — **2,6 с**, движок
при этом не перезапускается (`ActiveEnterTimestamp` не меняется).

⚠️ Движок стал зависеть от ВПН: упал xray — у движка нет выхода. Отказ
безопасный (классификатор fail-open, волна идёт по обычным фильтрам), но это
ещё одна точка отказа на прод-пути.

### Сброс гейта free-тира: поворот выхода по требованию

20.10 в 16:20 free-тир закрылся по IP: `provider.auth` — «free models aren't
available from your current network… we've detected abusive traffic from this
IP». Гейт **per-IP**: все модели каталога закрыты одним и тем же адресом, смена
модели цепочки не помогает, а смена выхода ВПН снимает отказ за 1,7–2,6 с
(проверено: DE → AT, вызов модели 2,3 с).

Автоповорот встроен в клиент движка (`modules/opencode_engine.py`), а не в
отдельный сторож:

- причина отказа с текстом гейта классифицируется как `network_gate` — видно в
  логах воркера (`...:network_gate`), не только «движок молчит»;
- **лимит квоты `http_429: Rate limit exceeded` тоже per-IP** (замер 20.10 19:40:
  на выходе NL все три модели — 429; смена сервера NL → US, тот же запрос прошёл
  за 4,9 с), поэтому он запускает тот же поворот. Условие одно: **вся цепочка**
  легла — 429 одной модели это обычный фолбэк, ВПН не трогаем;
- если **вся цепочка** легла на гейт или лимит, клиент выполняет команду из
  `NEURAL_EGRESS_ROTATE_CMD` и повторяет цепочку один раз;
- пауза между поворотами — `NEURAL_EGRESS_ROTATE_COOLDOWN` (дефолт 600 с),
  метка на диске (воркер многопроцессный). Без паузы волна из десятков регионов
  сожгла бы все адреса подписки за час.

В env-файле приложения на боксе:

```text
NEURAL_EGRESS_ROTATE_CMD="sudo -n /etc/neural/egress.d/10-nextgenvpn.sh"
NEURAL_EGRESS_ROTATE_COOLDOWN=600
```

Пусто в `NEURAL_EGRESS_ROTATE_CMD` — автоповорот выключен (движок только пишет
`network_gate`/`http_429` в лог). Рабочему пользователю воркера нужен `sudo -n` на этот
скрипт: он меняет `/etc/neural/vpn/*` и перезапускает `neural-vpn`.

### Сторож плеча: `neural-egress-guard.timer`

Автоповорот лечит **отказ вендора**, а не мёртвое плечо: упал xray или умер
текущий сервер подписки — у движка нет выхода, и в логе это только
`engine_all_failed`. Поэтому раз в 2 минуты `neural-egress-guard` проверяет
выход двумя путями (socks `10808` и HTTP-прокси `8118`):

- оба живы → молчит, счётчик провалов сбрасывает;
- **два провала подряд** → запускает `10-nextgenvpn.sh` (он сам перебирает
  серверы и проверяет выход), счётчик сбрасывает.

Один провал не трогаем: одиночный таймаут бывает у любого сервера, а лишний
поворот рвёт живой туннель под нагрузкой.

## 2. Память: почему сторож, а не `MemoryMax`

В юните стоит `MemoryHigh=384M` / `MemoryMax=450M`, но на этом боксе лимиты
systemd **не исполняются**: контейнер (OpenVZ, P163), systemd поднялся без
учёта памяти.

```text
systemctl show opencode-web -p MemoryAccounting -p MemoryCurrent
MemoryAccounting=no
MemoryCurrent=[not set]
```

Cgroup-файлов нет (`/sys/fs/cgroup/system.slice/opencode-web.service`
отсутствует), ядро лимит не применяет, жертву при нехватке памяти выбирает
OOM killer — не обязательно движок (14.10 он убивал и uvicorn, и celery, P164).

Поэтому память сторожит `opencode-rss-guard.timer`: раз в 2 минуты смотрит
суммарный RSS процессов пользователя движка и перезапускает юнит, если порог
(400 МБ) перейдён. Перезапуск дешёвый — сессии после каждого вызова удаляются,
потеря — один чанк волны (fail-open).

## Файлы

| Файл | Куда на боксе |
|---|---|
| `opencode-web.service` | `/etc/systemd/system/opencode-web.service` (пример) |
| `privoxy-neural.conf` | `/etc/privoxy/config` |
| `opencode-rss-guard.sh` | `/usr/local/lib/neural/opencode-rss-guard.sh` (755) |
| `opencode-rss-guard.service` / `.timer` | `/etc/systemd/system/` |
| `neural-egress-guard.sh` | `/usr/local/lib/neural/neural-egress-guard.sh` (755) |
| `neural-egress-guard.service` / `.timer` | `/etc/systemd/system/` |

ВПН-клиент (`neural-vpn`, `10-nextgenvpn.sh`, `refresh-subscription.sh`) —
без изменений из `brain_matrica/docs/plans/neural/`.

## Установка

```bash
apt-get install -y privoxy
install -m644 privoxy-neural.conf /etc/privoxy/config
install -d -m750 /var/log/privoxy && chown privoxy:nogroup /var/log/privoxy
systemctl enable --now privoxy

install -m755 opencode-rss-guard.sh /usr/local/lib/neural/
install -m644 opencode-rss-guard.service opencode-rss-guard.timer /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now opencode-rss-guard.timer

install -m755 neural-egress-guard.sh /usr/local/lib/neural/
install -m644 neural-egress-guard.service neural-egress-guard.timer /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now neural-egress-guard.timer
```

Права каталога ВПН: `/etc/neural/vpn` — `root:nogroup 750` (xray работает под
`nobody`), `client.json` — `root:nogroup 640`. С `700 root:root` xray не
стартует: `open … client.json: permission denied` (и `Restart=` крутит счётчик
рестартов в сотни).

## Проверки, которые стоит помнить на любом боксе

```bash
systemctl show <unit> -p MemoryAccounting          # no → лимиты декоративны
nft list table ip neural                           # смотреть counter, а не наличие правила
curl -s --socks5-hostname 127.0.0.1:10808 https://api.ipify.org   # выход ВПН
curl -s -x http://127.0.0.1:8118 https://api.ipify.org            # выход через прокси
curl -s https://api.ipify.org                                     # прямой выход бокса
```
