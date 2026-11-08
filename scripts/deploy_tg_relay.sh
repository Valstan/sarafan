#!/usr/bin/env bash
# Деплой TG egress-relay (infra/tg_relay/worker.js) на Cloudflare Workers
# через голый CF API (без wrangler/node). Запускать НА ПРОДЕ (setka), где
# в /etc/setka/setka.env лежат CLOUDFLARE_API_TOKEN и TG_RELAY_SECRET (#008),
# а токены ботов приходят из комнаты КАРМАНА (vault, имена TELEGRAM_TOKEN_*):
#
#   ssh sarafan "cd /home/valstan/SETKA && sudo bash scripts/deploy_tg_relay.sh"
#
# Идемпотентен: повторный запуск перезаливает скрипт той же командой.
set -euo pipefail

SCRIPT_NAME="tg-relay"
WORKER_FILE="$(dirname "$0")/../infra/tg_relay/worker.js"

TOKEN=$(grep -oP '(?<=^CLOUDFLARE_API_TOKEN=).*' /etc/setka/setka.env)
SECRET=$(grep -oP '(?<=^TG_RELAY_SECRET=).*' /etc/setka/setka.env)
[ -n "$TOKEN" ] || { echo "CLOUDFLARE_API_TOKEN not found in /etc/setka/setka.env"; exit 1; }
[ -n "$SECRET" ] || { echo "TG_RELAY_SECRET not found in /etc/setka/setka.env"; exit 1; }

# Токены ботов — из комнаты КАРМАНА: воркер ходит в Bot API от их имени.
# Пусто ⇒ воркер принимает любой токен (гейт по X-Relay-Secret остаётся).
VAULT=$(grep -oP '(?<=^SECRETS_VAULT_URL=).*' /etc/setka/setka.env)
VAULT_TOKEN=$(grep -oP '(?<=^SECRETS_TOKEN=).*' /etc/setka/secrets-token.env || true)
BOT_TOKENS=""
if [ -n "$VAULT" ] && [ -n "$VAULT_TOKEN" ]; then
  BOT_TOKENS=$(curl -sf -H "Authorization: Bearer $VAULT_TOKEN" "$VAULT" |
    python3 -c 'import json,sys
try:
    s = (json.load(sys.stdin) or {}).get("secrets") or {}
except Exception:
    s = {}
print(s.get("TELEGRAM_TOKEN_AFONYA", ""), s.get("TELEGRAM_TOKEN_VALSTANBOT", ""))' || true)
fi
echo "bot tokens: $(echo "$BOT_TOKENS" | awk '{print ($1!=""?"AFONYA":"") ($2!=""?" VALSTANBOT":"")}')"

API="https://api.cloudflare.com/client/v4"
ACCOUNT=$(curl -sf -H "Authorization: Bearer $TOKEN" "$API/accounts" |
  python3 -c 'import sys,json;print(json.load(sys.stdin)["result"][0]["id"])')
echo "account: $ACCOUNT"

# Загрузка module-worker'а: multipart c metadata + сам модуль.
# Токены ботов идут в окружение воркера как secret_text — в коде они не видны.
RELAY_SECRET="$SECRET" BOT_TOKENS="$BOT_TOKENS" python3 > /tmp/tg_relay_meta.json <<'EOF'
import json
import os

bindings = [{"type": "secret_text", "name": "RELAY_SECRET", "text": os.environ["RELAY_SECRET"]}]
for idx, name in enumerate(("TG_TOKEN_AFONYA", "TG_TOKEN_VALSTANBOT")):
    parts = (os.environ.get("BOT_TOKENS") or "").split()
    if idx < len(parts) and parts[idx]:
        bindings.append({"type": "secret_text", "name": name, "text": parts[idx]})
print(json.dumps({
    "main_module": "worker.js",
    "compatibility_date": "2026-06-01",
    "bindings": bindings,
}))
EOF

curl -sf -X PUT "$API/accounts/$ACCOUNT/workers/scripts/$SCRIPT_NAME" \
    -H "Authorization: Bearer $TOKEN" \
    -F "metadata=@/tmp/tg_relay_meta.json;type=application/json" \
    -F "worker.js=@$WORKER_FILE;type=application/javascript+module" \
    | python3 -c 'import sys,json;d=json.load(sys.stdin);print("upload:", "OK" if d["success"] else d["errors"])'

# Включить workers.dev-маршрут (https://tg-relay.<subdomain>.workers.dev).
curl -sf -X POST "$API/accounts/$ACCOUNT/workers/scripts/$SCRIPT_NAME/subdomain" \
    -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
    -d '{"enabled": true}' \
    | python3 -c 'import sys,json;d=json.load(sys.stdin);print("subdomain:", "OK" if d["success"] else d["errors"])'

SUBDOMAIN=$(curl -sf -H "Authorization: Bearer $TOKEN" "$API/accounts/$ACCOUNT/workers/subdomain" |
  python3 -c 'import sys,json;print(json.load(sys.stdin)["result"]["subdomain"])')
echo "relay URL: https://$SCRIPT_NAME.$SUBDOMAIN.workers.dev"
echo "прописать в /etc/setka/setka.env:  TELEGRAM_API_BASE=https://$SCRIPT_NAME.$SUBDOMAIN.workers.dev/bot"
