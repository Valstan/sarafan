#!/usr/bin/env bash
# Restart the engine unit when its whole process tree outgrows the threshold.
#
# The box may run without systemd memory accounting (OpenVZ container: the
# cgroup files are absent and MemoryMax silently does nothing), so the guard
# lives in userspace. Counts every process of the engine user, not only the
# main pid: the engine spawns JIT workers and they hold real RSS too.
#
# Unit and threshold come from the environment (LimitMB=... in the unit file).
set -euo pipefail

UNIT="${UNIT:-opencode-web}"
ENGINE_USER="${ENGINE_USER:-opencode}"
LIMIT_MB="${LIMIT_MB:-400}"

rss_mb=$(ps -u "$ENGINE_USER" -o rss= 2>/dev/null | awk '{s+=$1} END {printf "%d", s/1024}')
rss_mb="${rss_mb:-0}"

if [ "$rss_mb" -gt "$LIMIT_MB" ]; then
  logger -t opencode-rss-guard "restart $UNIT: engine user $ENGINE_USER holds ${rss_mb}MB > ${LIMIT_MB}MB"
  systemctl restart "$UNIT"
  exit 0
fi

# Quiet by default: the timer runs every 2 minutes and must not spam the journal.
exit 0
