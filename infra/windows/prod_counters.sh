#!/bin/bash
# Сверка счётчиков боевой базы Сарафана с локальной копией.
#
# Скрипт КЛАДЁТСЯ на боевой бокс и запускается оттуда:
#   Get-Content -Raw infra\windows\prod_counters.sh | ssh sarafan "bash -s"
#
# Печатает построчный список `таблица|точное_число_строк` по ВСЕМ таблицам
# публичной схемы. Локальная копия печатается тем же запросом — и два файла
# сравниваются построчно (diff). Расхождение по любой таблице означает, что
# копия неполна.
#
# Точные числа, а не оценки `pg_stat_user_tables`: оценка — это то, что
# счётчик примерно показывает, и расхождение в единицы на большой таблице
# не отличить от «просто не сходится».
#
# ⚠️ Скрипт ТОЛЬКО ЧИТАЕТ базу.

set -euo pipefail

ENV_FILE="${SETKA_ENV_FILE:-/etc/setka/setka.env}"

DATABASE_URL="$(sudo -n awk -F= '$1=="DATABASE_URL"{sub(/^DATABASE_URL=/, ""); print; exit}' "$ENV_FILE")"
if [[ -z "${DATABASE_URL}" ]]; then
  echo "ERR: DATABASE_URL not found in the app env file" >&2
  exit 1
fi
export DATABASE_URL

read -r DB_USER DB_PASSWORD DB_HOST DB_PORT DB_NAME < <(python3 - <<'PY'
import os
from urllib.parse import urlparse

raw = os.environ.get("DATABASE_URL", "").replace("postgresql+asyncpg://", "postgresql://", 1)
u = urlparse(raw)
print(u.username or "", u.password or "", u.hostname or "localhost", u.port or 5432, (u.path or "/").lstrip("/"))
PY
)

# query_to_xml даёт точный count(*) по каждой таблице: динамической ссылки на
# таблицу в обычном SQL нет, а перечислять 60 таблиц руками — источник
# расхождений (забыли одну).
SQL="SELECT c.relname || '|' || (xpath('/row/cnt/text()', query_to_xml(format('SELECT count(*) AS cnt FROM %I', c.relname), false, true, '')))[1]::text::bigint FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE c.relkind = 'r' AND n.nspname = 'public' ORDER BY c.relname;"

PGPASSWORD="$DB_PASSWORD" psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -At -c "$SQL"
