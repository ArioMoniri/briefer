#!/usr/bin/env bash
# =====================================================================
# Briefer control script. Works with systemd if the service is
# installed, otherwise falls back to a nohup background process.
#
#   ./manage.sh start | stop | restart | refresh | reset | status
#   ./manage.sh logs | foreground | update
# =====================================================================
set -Eeuo pipefail
APPDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$APPDIR"

SERVICE="briefer.service"
PY="$APPDIR/.venv/bin/python"
PIDFILE="$APPDIR/data/briefer.pid"
LOG="$APPDIR/data/logs/briefer.out"

has_systemd() {
  command -v systemctl >/dev/null 2>&1 && \
  systemctl list-unit-files 2>/dev/null | grep -q "^$SERVICE"
}

_sudo() { if [ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null; then sudo "$@"; else "$@"; fi; }

start() {
  if has_systemd; then _sudo systemctl start "$SERVICE"; echo "started (systemd)"; return; fi
  if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "already running (pid $(cat "$PIDFILE"))"; return
  fi
  mkdir -p data/logs
  PYTHONPATH="$APPDIR/src" nohup "$PY" -m briefer >>"$LOG" 2>&1 &
  echo $! > "$PIDFILE"
  echo "started (nohup, pid $(cat "$PIDFILE"))"
}

stop() {
  if has_systemd; then _sudo systemctl stop "$SERVICE"; echo "stopped (systemd)"; return; fi
  if [ -f "$PIDFILE" ]; then
    kill "$(cat "$PIDFILE")" 2>/dev/null || true
    rm -f "$PIDFILE"; echo "stopped"
  else echo "not running"; fi
}

restart() { stop || true; sleep 1; start; }

# refresh = reload config/code without touching data (soft restart)
refresh() { echo "Refreshing (restart with current code & .env)…"; restart; }

# reset = wipe runtime state (auth sessions, dedup, reminders) then restart.
# Keeps .env and sheets. Requires confirmation.
reset() {
  read -rp "This clears sessions, dedup history and pending reminders. Proceed? [y/N] " a
  [ "${a:-N}" = "y" ] || { echo "aborted"; return; }
  stop || true
  rm -f data/briefer.db data/briefer.db-wal data/briefer.db-shm
  echo "state wiped"
  start
}

status() {
  if has_systemd; then systemctl status "$SERVICE" --no-pager || true; return; fi
  if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "running (nohup, pid $(cat "$PIDFILE"))"
  else echo "not running"; fi
}

logs() {
  if has_systemd; then _sudo journalctl -u "$SERVICE" -f --no-pager; else tail -f "$LOG"; fi
}

foreground() { PYTHONPATH="$APPDIR/src" exec "$PY" -m briefer; }

update() {
  echo "Pulling latest code…"
  git pull --ff-only || echo "git pull skipped/failed"
  "$PY" -m pip install -r requirements.txt
  restart
}

# google-auth = headless Google login (prints a link, no browser needed)
google_auth() { PYTHONPATH="$APPDIR/src" "$PY" "$APPDIR/authorize_google.py"; }

cmd="${1:-status}"
case "$cmd" in
  start) start ;; stop) stop ;; restart) restart ;;
  refresh) refresh ;; reset) reset ;; status) status ;;
  logs) logs ;; foreground|fg) foreground ;; update) update ;;
  google-auth|gauth) google_auth ;;
  *) echo "usage: $0 {start|stop|restart|refresh|reset|status|logs|foreground|update|google-auth}"; exit 1 ;;
esac
