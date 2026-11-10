#!/usr/bin/env bash
# Обёртка локального движка: держит `opencode serve` живым.
#
# **Зачем обёртка, а не сам serve в юните.** OpenCode v2 в режиме `--service`
# уходит в фон и печатает пароль сервера в `~/.config/opencode/service.json` —
# именно оттуда клиент движка берёт пароль. Но systemd такой процесс не
# видит: юнит с `--service` сразу завершится, а serve останется сиротой,
# которую никто не сторожит. Обёртка решает обе задачи: поднимает serve в
# фоновом режиме (файл с паролем появляется) и сама следит за портом —
# если serve упал, поднимает заново.
#
# Запуск: infra/engine/opencode-engine.sh [порт] (по умолчанию 4097).

set -uo pipefail

PORT="${1:-4097}"
LOG="${OPENCODE_ENGINE_LOG:-/tmp/opencode_serve.log}"
CHECK_INTERVAL="${OPENCODE_ENGINE_CHECK_INTERVAL:-20}"

# Порт слушает только loopback: движек — внутренний, снаружи не выставляем.
while true; do
  if ss -ltn 2>/dev/null | grep -q ":${PORT} "; then
    sleep "${CHECK_INTERVAL}"
    continue
  fi

  echo "[$(date -Is)] serve на порту ${PORT} не отвечает — поднимаю" >> "${LOG}"
  # `--service` пишет пароль в ~/.config/opencode/service.json; без него клиент
  # движка не сможет зайти на сервер (basic-auth).
  opencode serve --service --hostname 127.0.0.1 --port "${PORT}" >> "${LOG}" 2>&1
  sleep 10
done
