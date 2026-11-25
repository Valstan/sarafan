#!/bin/bash
# Снятие дампа боевой базы Сарафана для переезда на локальную машину.
#
# Скрипт КЛАДЁТСЯ на боевой бокс и запускается оттуда:
#   Get-Content -Raw infra\windows\prod_dump.sh | ssh sarafan "bash -s"
#
# Что делает:
# 1. берёт DATABASE_URL из env-файла приложения (значение не печатается);
# 2. снимает дамп базы в формате custom (pg_dump -Fc) — он сжимается и
#    восстанавливается без потерь, включая индексы и частичные индексы
#    (`postgresql_where=...` из database/models.py — ради них SQLite и не
#    рассматривался как замена);
# 3. печатает счётчики по сверяемым таблицам ДО и ПОСЛЕ дампа.
#
# Почему счётчики дважды. База живая: между дампом и сверкой в неё
# дописываются посты. Счётчик «после» показывает, насколько база уехала
# вперёд за время снятия; если «до» и «после» совпали с локальной базой —
# расхождение нулевое и по существу, а не по времени.
#
# ⚠️ Скрипт ТОЛЬКО ЧИТАЕТ базу. Ничего не меняет, не останавливает,
#    не удаляет. Дамп кладётся в каталог бэкапов приложения — он
#    игнорируется git'ом.

set -euo pipefail

ENV_FILE="${SETKA_ENV_FILE:-/etc/setka/setka.env}"
BACKUP_DIR="${SETKA_BACKUP_DIR:-/home/valstan/SETKA/backup}"
STAMP="$(date +%Y%m%d_%H%M%S)"
DUMP_FILE="$BACKUP_DIR/sarafan_migration_${STAMP}.dump"

TABLES="posts communities regions filters publish_schedules"

mkdir -p "$BACKUP_DIR"

# Разбор DATABASE_URL. awk здесь — без экранированных кавычек: в одинарных
# кавычках bash обратный слэш перед «остаётся в тексте программы, и awk
# падает («backslash not last character on line»). Именно на этом сломался
# штатный scripts/backup_database.sh — ежедневные дампы на боевом боксе
# с начала января пустые (0 байт).
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

count_query() {
  local q=""
  for t in $TABLES; do
    q="$q SELECT '$t' AS table_name, count(*) AS rows FROM $t;"
  done
  q="${q%;}"
  PGPASSWORD="$DB_PASSWORD" psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -At -F'|' -c "$q"
}

echo "=== СЧЁТЧИКИ ДО ДАМПА ==="
count_query

echo "=== ДАМП ==="
PGPASSWORD="$DB_PASSWORD" pg_dump \
  -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" \
  --format=custom --no-owner --no-privileges --file="$DUMP_FILE"
chmod 644 "$DUMP_FILE"
echo "дамп: $DUMP_FILE ($(du -h "$DUMP_FILE" | cut -f1))"

echo "=== СЧЁТЧИКИ ПОСЛЕ ДАМПА ==="
count_query

echo "=== ГОТОВО ==="
echo "DUMP_FILE=$DUMP_FILE"
