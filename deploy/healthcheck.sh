#!/usr/bin/env bash
# Self-healing watchdog. Run by cron. If the bot isn't alive, (re)start it.
# Prefers systemd; falls back to the nohup PID file used by manage.sh.
set -Eeuo pipefail
APPDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE="briefer.service"
STAMP="$(date '+%Y-%m-%d %H:%M:%S')"

if command -v systemctl >/dev/null 2>&1 && \
   systemctl list-unit-files 2>/dev/null | grep -q "^$SERVICE"; then
  if ! systemctl is-active --quiet "$SERVICE"; then
    echo "$STAMP watchdog: $SERVICE down → restarting"
    systemctl restart "$SERVICE" || echo "$STAMP watchdog: restart FAILED"
  fi
  exit 0
fi

# Non-systemd fallback (nohup mode).
PIDFILE="$APPDIR/data/briefer.pid"
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  exit 0
fi
echo "$STAMP watchdog: process down → starting via manage.sh"
"$APPDIR/manage.sh" start >/dev/null 2>&1 || echo "$STAMP watchdog: start FAILED"
