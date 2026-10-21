#!/usr/bin/env bash
# Установка/обновление systemd-юнита демона интейков радара на прод-боксе.
# Запускать на боксе из корня деплоя: bash scripts/install_radar_intake_service.sh
# Пути можно переопределить переменными окружения, по умолчанию — как у
# setka-vk-bot (D-038).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
USER_NAME="${SETKA_USER:-$(id -un)}"
ENV_FILE="${SETKA_ENV_FILE:-/etc/setka/setka.env}"
SECRETS_FILE="${SETKA_SECRETS_FILE:-/etc/setka/secrets-token.env}"
UNIT=/etc/systemd/system/setka-radar-intake.service

sed -e "s#__SETKA_ROOT__#${ROOT}#g" \
    -e "s#__USER__#${USER_NAME}#g" \
    -e "s#__ENV_FILE__#${ENV_FILE}#g" \
    -e "s#__SECRETS_FILE__#${SECRETS_FILE}#g" \
    "${ROOT}/config/setka-radar-intake.service.template" | sudo tee "${UNIT}" >/dev/null

sudo systemctl daemon-reload
sudo systemctl enable --now setka-radar-intake
sudo systemctl restart setka-radar-intake
sleep 5
systemctl is-active setka-radar-intake
tail -5 "${ROOT}/logs/radar-intake.log" || true
