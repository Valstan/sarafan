#!/usr/bin/env bash
# Смена идентичности анонимного аккаунта движка после мёртвой квоты free-тира.
#
# **Зачем.** Замер 24.10: класс отказа на проде — `http_429 Free usage limit
# reached. Subscribe to OpenCode Go` (первый раз 23.10 06:48, 664 записи за двое
# суток). Пять поворотов ВПН подряд (DE, PL, NL, US, PL — разные адреса) его НЕ
# сняли. То есть квота считается по аккаунту, а не по IP: смена выхода создаёт
# новый адрес, но не новый аккаунт. Тот же симптом описан сообществом: блок
# держится больше суток, на смену IP не реагирует, все free-модели падают
# одновременно.
#
# **Порядок (слово владельца 24.10).** Выгрузить движок полностью со всеми
# процессами → сменить сервер и страну ВПН → сбросить идентичность → поднять →
# проверить free-тир ОДНИМ коротким сообщением. Проверка одиночная намеренно:
# она не должна ни жечь квоту, ни занимать слот воркера работой модели.
#
# ⚠️ Это обход лимита вендора, решение владельца принято с явным пониманием.
# Движок сам этого не делает: он только ставит метку `/tmp/setka_engine_quota.stamp`,
# а сюда приходит таймер. Причина разделения — движок в момент отказа стоит в
# единственном слоте воркера, и выгрузка serve внутри волны публикации означала
# бы ожидание рестарта в этом слоте.
#
# ⚠️ Движок — он же веб-кабинет владельца для кодинга. Выгрузка рвёт его
# сессию, поэтому у сброса свой длинный предохранитель (6 часов) и обязательный
# бэкап: не удалось — состояние возвращается на место.
#
# Использование:
#   neural-identity-reset.sh              # сухой прогон: что и почему
#   neural-identity-reset.sh --apply      # выполнить (так зовёт таймер)
set -uo pipefail

STAMP=/tmp/setka_engine_quota.stamp
STATE_DIR=/var/lib/neural
LAST=$STATE_DIR/identity-reset.last
BACKUP_DIR=$STATE_DIR/identity-backup
MIN_AGE=1800          # квота мертва не меньше получаса — только тогда лечим
COOLDOWN=21600        # не чаще раза в 6 часов
PORT=49374
ENV_FILE=/etc/opencode-web/anon.env
EGRESS=/etc/neural/egress.d/10-nextgenvpn.sh
DATA_DIR=/home/opencode/.local/share/opencode
PROBE_TIMEOUT=90

APPLY=0
[ "${1:-}" = "--apply" ] && APPLY=1

log() { echo "identity reset: $*"; }

if [ ! -f "$STAMP" ]; then
    exit 0
fi

now=$(date +%s)
stamp_mtime=$(stat -c %Y "$STAMP" 2>/dev/null || echo "$now")
age=$((now - stamp_mtime))
if [ "$age" -lt "$MIN_AGE" ]; then
    log "квота мертва ${age}с (< ${MIN_AGE}с) — ждём, не трогаем движок"
    exit 0
fi

if [ -f "$LAST" ]; then
    last=$(cat "$LAST" 2>/dev/null || echo 0)
    case "$last" in ''|*[!0-9]*) last=0 ;; esac
    since=$((now - last))
    if [ "$since" -lt "$COOLDOWN" ]; then
        log "сброс был ${since}с назад (< ${COOLDOWN}с) — пропускаю"
        exit 0
    fi
fi

# Идентичность анонимного аккаунта лежит в каталоге данных движка. Точный файл
# на 24.10 не подтверждён живьём (SSH-канал лежал), поэтому список кандидатов
# явный и проверяемый, а не «удалить каталог»: там же сессии владельца.
# Пусто в списке — не угадываем, а честно выходим: см. условие ниже.
IDENTITY_PATHS="${IDENTITY_PATHS:-}"
candidates=()
if [ -n "$IDENTITY_PATHS" ]; then
    for p in $IDENTITY_PATHS; do candidates+=("$p"); done
else
    for p in \
        "$DATA_DIR/opencode/auth.json" \
        "$DATA_DIR/opencode/installation.json" \
        "$DATA_DIR/opencode/device.json" \
        "$DATA_DIR/auth.json" \
        "$DATA_DIR/installation.json" \
        "$DATA_DIR/device.json"; do
        [ -e "$p" ] && candidates+=("$p")
    done
fi

if [ "${#candidates[@]}" -eq 0 ]; then
    log "файлов идентичности не нашёл в $DATA_DIR — нужен замер бокса,"
    log "укажи путь явно: IDENTITY_PATHS='/путь/к/файлу' $0 --apply"
    exit 2
fi

log "квота мертва ${age}с; кандидаты на сброс:"
for p in "${candidates[@]}"; do
    log "  $p ($(stat -c %s "$p" 2>/dev/null || echo '?') байт)"
done

if [ "$APPLY" -ne 1 ]; then
    log "сухой прогон: дальше были бы выгрузка движка, поворот ВПН,"
    log "бэкап и удаление перечисленного, подъём и одна проверка free-тира"
    exit 0
fi

if [ "$(id -u)" -ne 0 ]; then
    log "нужен root: выгрузка юнита и правка файлов движка"
    exit 1
fi

mkdir -p "$BACKUP_DIR"
backup="$BACKUP_DIR/identity-$(date +%Y%m%d-%H%M%S).tgz"
if ! tar czf "$backup" "${candidates[@]}" 2>/dev/null; then
    log "бэкап не создан — ничего не трогаю"
    exit 1
fi
log "бэкап: $backup"

# 1. Выгрузить движок полностью: юнит и все процессы пользователя движка.
log "выгружаю движок"
systemctl stop opencode-web || log "юнит не остановился штатно"
pkill -u opencode 2>/dev/null && log "добил оставшиеся процессы пользователя"
sleep 2

# 2. Сменить сервер и страну ВПН.
if [ -x "$EGRESS" ]; then
    log "меняю выход ВПН"
    "$EGRESS" || log "поворот ВПН не подтвердился — продолжаю (адрес не главное)"
else
    log "скрипта поворота нет: $EGRESS"
fi

# 3. Сбросить идентичность.
for p in "${candidates[@]}"; do
    rm -f "$p" && log "удалён $p"
done

# 4. Поднять движок и дождаться порта.
log "поднимаю движок"
systemctl start opencode-web || log "юнит не стартовал"

port_up=0
for _ in $(seq 1 30); do
    if (exec 3<>/dev/tcp/127.0.0.1/$PORT) 2>/dev/null; then
        exec 3<&- 2>/dev/null
        port_up=1
        break
    fi
    sleep 1
done
if [ "$port_up" -ne 1 ]; then
    log "порт $PORT не поднялся — возвращаю идентичность из бэкапа"
    systemctl stop opencode-web || true
    tar xzf "$backup" -C / || log "бэкап не развернулся: $backup"
    systemctl start opencode-web || true
    exit 1
fi

# 5. Проверить free-тир ОДНИМ коротким сообщением.
probe_free_tier() {
    local pw sid resp verdict
    pw=$(sed -n 's/^OPENCODE_PASSWORD=//p' "$ENV_FILE" 2>/dev/null | tr -d '"' | head -1)
    if [ -z "$pw" ]; then
        log "пароля serve нет в $ENV_FILE — проверить нечем"
        return 1
    fi

    sid=$(curl -s --max-time 20 -u "opencode:$pw" \
        -X POST "http://127.0.0.1:$PORT/api/session" \
        -H 'Content-Type: application/json' \
        -d '{"title":"quota-probe","model":{"id":"deepseek-v4.1-flash-free","providerID":"opencode","variant":"high"}}' \
        | python3 -c 'import sys,json;print((json.load(sys.stdin).get("data") or {}).get("id") or "")' 2>/dev/null)
    if [ -z "$sid" ]; then
        log "сессия не создалась"
        return 1
    fi

    curl -s --max-time 20 -u "opencode:$pw" \
        -X POST "http://127.0.0.1:$PORT/api/session/$sid/prompt" \
        -H 'Content-Type: application/json' -d '{"text":"Ответь одним словом: да"}' >/dev/null
    curl -s --max-time "$PROBE_TIMEOUT" -u "opencode:$pw" \
        -X POST "http://127.0.0.1:$PORT/api/experimental/session/$sid/wait" >/dev/null

    resp=$(curl -s --max-time 20 -u "opencode:$pw" \
        "http://127.0.0.1:$PORT/api/session/$sid/message?limit=6&order=desc")
    curl -s --max-time 10 -u "opencode:$pw" -X DELETE \
        "http://127.0.0.1:$PORT/api/session/$sid" >/dev/null

    if printf '%s' "$resp" | grep -qi 'free usage limit reached'; then
        log "free-тир всё ещё закрыт: квота не сбросилась"
        return 1
    fi
    verdict=$(printf '%s' "$resp" | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin).get("data") or []
except Exception:
    print("")
    raise SystemExit
for msg in data:
    if msg.get("type") != "assistant":
        continue
    text = "".join(
        part.get("text", "") for part in (msg.get("content") or [])
        if isinstance(part, dict) and part.get("type") == "text"
    ).strip()
    if text:
        print("ok")
        break
' 2>/dev/null)
    [ "$verdict" = "ok" ]
}

if probe_free_tier; then
    log "free-тир отвечает — идентичность сменена, метка квоты снята"
    rm -f "$STAMP"
    date +%s > "$LAST"
    # Оставляем один бэкап: второй понадобится только если этот сброс не спасёт.
    ls -1t "$BACKUP_DIR"/identity-*.tgz 2>/dev/null | tail -n +2 | xargs -r rm -f
    exit 0
fi

log "после сброса free-тир закрыт — возвращаю идентичность и поднимаю движок"
systemctl stop opencode-web || true
tar xzf "$backup" -C / || log "бэкап не развернулся: $backup"
systemctl start opencode-web || true
date +%s > "$LAST"
exit 1
