#!/usr/bin/env bash
# git_sync_check.sh — проверка синхронизации рабочего дерева с GitHub.
#
# GitHub — источник истины при работе на нескольких машинах (днём один комп,
# вечером другой). Скрипт ловит «застрявшую» локально работу, которая не доехала
# на origin, и расхождение с origin (другая машина уже запушила).
#
# Режимы:
#   --warn  : печатает предупреждение в stdout, если есть несинхронизированная
#             работа. ВСЕГДА exit 0 — используется в SessionStart-хуке и не должен
#             блокировать старт сессии. stdout попадает в контекст Claude, и он
#             подсвечивает несинхрон пользователю при входе в сессию.
#   --gate  : та же детекция, но exit 1 если что-то не синхронизировано (используется
#             в /close_session как жёсткий гейт «всё ли на GitHub»). exit 0 = чисто.
#
# Кросс-платформенно: POSIX-ish, вызывается через `bash` (Git Bash на Windows и
# системный bash на Linux). Сетевой fetch — best-effort с таймаутом, офлайн не ломает.

set -u

MODE="${1:---warn}"

# Не git-репозиторий — молча выходим (хук не должен мешать).
if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  exit 0
fi

# best-effort fetch (таймаут 5s), чтобы знать про behind/diverged. Без `timeout`
# (некоторые окружения) — пропускаем, чтобы не рисковать зависанием офлайн.
if command -v timeout >/dev/null 2>&1; then
  timeout 5 git fetch --quiet 2>/dev/null || true
fi

issues=""
branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "?")

# 1. Незакоммиченные / неотслеживаемые изменения.
#
# `-c core.autocrlf=true` — не косметика. Репозиторий живёт на Windows с
# autocrlf=true: в рабочем дереве CRLF, в индексе LF, и git нормализует их при
# чтении. Но скрипт зовут из разных bash: Git Bash видит пользовательский
# gitconfig, а WSL-овский `bash` (он же первый в PATH) — нет, и тогда все
# CRLF-файлы выглядят изменёнными (замер 25.10: 1494 файла «грязных» при чистом
# дереве). Ложное «грязно» здесь дороже пропуска: следующий `git add -A` из того
# же bash закоммитил бы CRLF-шум по всему репозиторию. Явная нормализация делает
# ответ одинаковым из любой оболочки.
if [ -n "$(git -c core.autocrlf=true status --porcelain 2>/dev/null)" ]; then
  dirty=$(git -c core.autocrlf=true status --porcelain 2>/dev/null | wc -l | tr -d ' ')
  issues="${issues}- Незакоммиченные/неотслеживаемые правки: ${dirty} файл(ов).\n"
fi

# 2. Upstream / неотправленные / отставшие коммиты.
if git rev-parse --abbrev-ref --symbolic-full-name '@{u}' >/dev/null 2>&1; then
  ahead=$(git rev-list --count '@{u}..HEAD' 2>/dev/null || echo 0)
  behind=$(git rev-list --count 'HEAD..@{u}' 2>/dev/null || echo 0)
  if [ "${ahead:-0}" -gt 0 ]; then
    issues="${issues}- Неотправленные коммиты на '${branch}': ${ahead} (нужен git push).\n"
  fi
  if [ "${behind:-0}" -gt 0 ]; then
    issues="${issues}- origin опережает '${branch}' на ${behind} коммит(ов) — другая машина запушила, нужен git pull.\n"
  fi
else
  issues="${issues}- Ветка '${branch}' не привязана к origin (нет upstream) — работа может быть не на GitHub.\n"
fi

repo=$(git rev-parse --show-toplevel 2>/dev/null | sed 's#.*/##')

if [ -n "$issues" ]; then
  printf '⚠️  СИНХРОНИЗАЦИЯ С GITHUB (%s): есть работа, которой может не быть на GitHub\n' "$repo"
  printf '%b' "$issues"
  printf 'GitHub — источник истины между машинами. Закрывай сессию через /close_session — '
  printf 'она закоммитит и запушит всё, прежде чем считать сессию закрытой.\n'
  [ "$MODE" = "--gate" ] && exit 1
  exit 0
fi

# Чисто и всё на origin.
if [ "$MODE" = "--gate" ]; then
  printf 'OK: рабочее дерево чистое, всё запушено на origin (%s, ветка %s).\n' "$repo" "$branch"
fi
exit 0
