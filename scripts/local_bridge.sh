#!/usr/bin/env bash
# Обёртка моста к локальному движку: грузит env приложения и зовёт CLI.
#
# Живёт на боксе. Демон на ПК владельца зовёт его по SSH:
#
#   ssh sarafan "sudo -n ~/SETKA/scripts/local_bridge.sh export --limit 20"
#   ssh sarafan "sudo -n ~/SETKA/scripts/local_bridge.sh submit" < verdicts.json
#
# env-файл приложения — root-only, поэтому и sudo, и обёртка: демону ключ
# доступа к боксу не нужен, нужен только SSH.
#
# Про путь деплоя: под sudo `~` — это /root, а деплой лежит в домашнем
# каталоге пользователя-владельца. Поэтому путь берётся явно и переопределяется
# переменной SETKA_ROOT.
set -euo pipefail

DEPLOY_ROOT="${SETKA_ROOT:-/home/valstan/SETKA}"

set -a
. /etc/setka/setka.env
set +a

cd "$DEPLOY_ROOT"

status=0
./venv/bin/python scripts/local_bridge.py "$@" || status=$?

# Журнал прогонов пишется от root, а панель (сервис setka) читает его от
# пользователя деплоя. Отдаём файл владельцу каталога: иначе читаемость
# зависит от umask'а root.
if [ -f "$DEPLOY_ROOT/logs/local_engine_runs.jsonl" ]; then
  chown "$(stat -c '%u:%g' "$DEPLOY_ROOT")" "$DEPLOY_ROOT/logs/local_engine_runs.jsonl" || true
fi

exit $status
