#!/usr/bin/env bash
# Сторож ВПН-плеча: движок ходит в сеть только через него (ADR-0009).
#
# Упал xray или умер текущий сервер подписки — у движка нет выхода: отказ
# безопасный (волна идёт по обычным фильтрам), но нейро-фильтр молчит, а
# снаружи это выглядит как «всё работает». Автоповорот при гейте free-тира
# (modules/opencode_engine._rotate_egress) этот случай НЕ лечит: там отказ
# вендора, а здесь мёртвое плечо.
#
# Два подряд провала — переключаем сервер подписки (он сам проверяет выход и
# перебирает адреса). Один провал не трогаем: одиночный таймаут бывает у любого
# сервера, а лишний поворот под нагрузкой рвёт живой туннель.
set -uo pipefail

STATE=/var/lib/neural/egress-guard.fail
SOCKS=127.0.0.1:10808
PROXY=http://127.0.0.1:8118

socks_ok=1
proxy_ok=1
curl -s --max-time 10 --socks5-hostname "$SOCKS" https://api.ipify.org >/dev/null || socks_ok=0
curl -s --max-time 10 -x "$PROXY" https://api.ipify.org >/dev/null || proxy_ok=0

if [ "$socks_ok" = 1 ] && [ "$proxy_ok" = 1 ]; then
    rm -f "$STATE"
    exit 0
fi

fails=$(cat "$STATE" 2>/dev/null || echo 0)
case "$fails" in
    ''|*[!0-9]*) fails=0 ;;
esac
fails=$((fails + 1))
echo "$fails" > "$STATE"
echo "egress guard: провал (socks=$socks_ok proxy=$proxy_ok), подряд=$fails"

if [ "$fails" -lt 2 ]; then
    exit 0
fi

rm -f "$STATE"
if [ -x /etc/neural/egress.d/10-nextgenvpn.sh ]; then
    echo "egress guard: переключаю сервер подписки"
    /etc/neural/egress.d/10-nextgenvpn.sh || echo "egress guard: поворот не дал выхода"
else
    echo "egress guard: скрипта поворота нет — перезапускаю neural-vpn"
    systemctl restart neural-vpn || echo "egress guard: neural-vpn не перезапустился"
fi
