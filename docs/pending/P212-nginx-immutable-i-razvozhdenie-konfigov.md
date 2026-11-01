# P212 — nginx: загружаемые файлы под `immutable` + копии конфигов разошлись с продом

> Запись `P212` реестра [PENDING_FOLLOWUPS](../PENDING_FOLLOWUPS.md). Индекс грепается, запись читается целиком, реестр целиком не читается никогда ([D-097](../../AGENTS.md)).

**Статус:** ⏱ 2026-11-01 · snooze 0 · fresh · **закрыто в этой сессии** (правка конфигов + гейт-тест, PR #1002)

## Что было (D-096 №3, обещано к 30.10)

Загружаемые файлы отдавались под `Cache-Control: public, immutable` — тем же
заголовком, что и версионированная статика. Замер на проде 31.10:

```
curl -sI https://3931b3fe50ab.vps.myjino.ru/static/broadcast/2026-06-18_12-53-41.png
→ Cache-Control: max-age=2592000
→ Cache-Control: public, immutable
```

## Почему это ложь, а не перестраховка

`immutable` — обещание «**по этому адресу навсегда те же байты**». Оно честно
там, где адрес меняется на каждом деплое (у нас статика несёт `?v={{
STATIC_VERSION}}`, а версия считается сама из git-sha — см. идею пула #250).

Но у загружаемых файлов **адрес не меняется**:

- `web/api/broadcast.py:369` — `safe_image_name(file.filename)` возвращает
  **базовое** имя файла, путь отбрасывается;
- `web/api/broadcast.py:380` — `dest.write_bytes(data)` пишет **поверх** того
  же имени.

То есть загрузка «Прайс 1.png» второй раз с другим содержимым даёт тот же
URL и новое содержимое. `immutable` на таком адресе означал бы: браузер, взявший
картинку один раз, держит старую **до 30 дней** (`expires 30d`).

Косвенный путь тот же для `/static/ad_offers/*` (`web/api/ad_cabinet.py:561`) —
библиотека офферных картинок.

**Кто реально страдает.** Загрузка картинок в VK идёт **байтами**, а не по URL
(`modules/broadcast/dispatcher.py:345` и `web/api/ad_cabinet.py:593`:
`p.read_bytes()` → заливка). Поэтому в VK уедет свежая картинка, а **в
браузере оператора** на превью в библиотеке кампаний останется старая — то
есть расхождение между тем, что увидели в VK, и тем, что показало превью.

**Куда бьёт сильнее.** На публичном домене (`сарафан.вмалмыже.рф`) `/static`
отдаёт приложение, а не nginx: `RevalidatingStaticFiles` (`web/static_files.py`)
ставит `Cache-Control: no-cache`. Проверено замером:

```
curl -sI https://xn--80aaa6cmey.xn--80adkdyec4j.xn--p1ai/static/broadcast/…
→ Cache-Control: no-cache
```

`immutable` жил только на **техдомене** (`3931b3fe50ab.vps.myjino.ru`) — там
nginx отдаёт `/static` напрямую через `alias`. То есть страдали машинные
интеграции и любой, кто зашёл на техдомен за статикой.

## Правка

Отдельный `location` на каждый загружаемый каталог, без `immutable`:

```nginx
location /static/broadcast/ {
    alias /home/valstan/SETKA/web/static/broadcast/;
    expires -1;
}
```

`expires -1` — это `Cache-Control: no-cache`, **ровно то, что отдаёт второй
слой** (`RevalidatingStaticFiles`). Смысл один: копию хранить можно, показывать
без перепроверки — нельзя. Перепроверка дешёвая: nginx шлёт ETag и
Last-Modified, браузер получает 304, а не файл целиком.

Первый вариант был `expires 5m` + `must-revalidate` — сознательно заменён на
`expires -1`: окно в 5 минут, в котором новая картинка не доезжает, — это тот
же класс проблемы, только меньше. Разница с приложением появилась бы там, где
её быть не должно.

## Найденное рядом: копии конфигов разошлись с продом

Пока сверял конфиги, выяснилось, что **git-версии конфигов — не то, что живёт
на боксе**. Снял все `conf.d` с прода и сравнил (без комментариев):

| Файл на проде | В git | Расхождение |
|---|---|---|
| `setka.conf` | `config/nginx_setka.conf` | в git нет `setka_host` в access_log |
| `setka_http_default.conf` | `config/nginx_setka_http_default.conf` | **в git лежит ДО-#251 версия**: `location /` проксирует всё в приложение |
| `00_setka_log_format.conf` | — | **в git отсутствует вовсе** |
| остальные 6 | — | совпадают |

Три следствия, каждое опасное:

1. **`nginx_setka_http_default.conf` — ловушка.** Прод-версия (от 18.09, #251)
   закрывает чужой Host кодом 444 и отклоняет чужой SNI рукопожатием. В git
   лежала версия, где `location /` проксировал **всё** в приложение — ровно то,
   из-за чего сканеры (`.env`, `.git/HEAD`, `phpinfo`) получали 401 и
   подтверждали, что здесь кто-то живёт. Скопировавший git-версию на бокс
   **вернул бы сканерам доступ**. Это тот же класс «разъезда копий», из-за
   которого 26.07 зародилась петля редиректов на радаре (HOSTS.md, правило 1).

2. **`00_setka_log_format.conf` не было в git.** Формат `setka_host` живёт на
   боксе, а определения в репозитории нет — сборка бокса из репозитория была бы
   невозможна: `nginx -t` падает с «unknown log format».

3. **`scripts/apply_nginx_config.sh` ставил на бокс ШАБЛОН.** Скрипт копировал
   `config/setka.conf.editable` → `/etc/nginx/conf.d/setka.conf`. А шаблон —
   это `listen 80`, `server_name YOUR_DOMAIN`, `ssl_certificate
   .../YOUR_DOMAIN/...`, без TLS. Скрипт из документации (`docs/OPERATIONS.md`,
   `docs/START_HERE.md`) **снёс бы с техдомена HTTPS**. Плюс шаблон содержал
   `location /static` под `immutable` — то есть даже «правильная» установка
   возвращала бы тот же баг.

## Что сделано

- `config/nginx_setka.conf`, `config/nginx_setka_http_redirect_domain.conf`,
  `config/setka.conf.editable` — отдельный `location` на
  `/static/broadcast/` и `/static/ad_offers/` с `expires -1`.
- `config/nginx_setka_http_default.conf` — заменён на прод-версию (#251,
  `return 444` + `ssl_reject_handshake`).
- `config/00_setka_log_format.conf` — добавлен в репозиторий (снят с прода).
- `config/nginx_setka.conf` — в access_log добавлен `setka_host` (как на проде).
- `scripts/apply_nginx_config.sh` — источник теперь `config/nginx_setka.conf`,
  плюс отказ, если в исходнике остался `YOUR_DOMAIN` (шаблон на бокс не ставится).
- `config/setka.conf.editable` — помечен как шаблон для **чужого**
  развёртывания; плейсхолдеры оставлены (это его назначение), но шапка
  предупреждает, что на наш бокс его ставить нельзя.
- `docs/OPERATIONS.md`, `docs/START_HERE.md` — ссылка на несуществующий
  `config/sarafan.conf.editable` заменена на живой `config/nginx_setka.conf`.

## Гейт-тест

`tests/test_infra/test_nginx_config_invariants.py` (новый) фиксирует все четыре
инварианта: загружаемые каталоги не под `immutable` и с `expires -1`;
исключение смотрит в тот же корень `web/static` (ошибка alias'а тихая — 404 на
файле, который ждут в VK); default-сервер — усиленная версия без `proxy_pass`;
формат `setka_host` определён в репозитории; скрипт ставит живой конфиг, а не
шаблон.

## Проверка синтаксиса на настоящем nginx

Правки конфигов — не питон, их не проверить тестами. Синтаксис проверен
**настоящим nginx бокса** (1.28.0): конфиги скопированы во временный каталог,
собран отдельный главный конфиг с `include` на них, `nginx -t -c` → «syntax is
ok». Живой `/etc/nginx/conf.d` при проверке не трогался.

## Чего НЕ делал

- Не переносил на прод в этой сессии: деплой конфигов — релизной процедурой
  после мержа (с `sudo cp`, `nginx -t`, `systemctl reload nginx`).
- Не менял политику кэша у версионированной статики: `immutable` там честный.
- Не трогал `valentin.conf` — это чужой проект (Next-экспорт), свой кэш.

## Связанное

- [P211](P211-os-kill-pid-0-ubivaet-process-na-windows.md) — другой молчаливый
  баг того же класса («работает на Linux, молчит на Windows»).
- `docs/ops/HOSTS.md` — карта доменов и правило 1 «одно решение — один
  владелец».
- [ADR-0011](../../../brain_matrica/adr/0011-vendor-neutral-agent-contract.md) —
  про две расходящиеся копии канона.
