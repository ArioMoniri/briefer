#!/usr/bin/env bash
# =====================================================================
# Briefer control script.
#
# Picks the best available runner automatically:
#   systemd service  →  else a tmux session  →  else a nohup process.
#
#   ./manage.sh start | stop | restart | refresh | reset | status
#   ./manage.sh logs | attach | foreground | update
#   ./manage.sh google-auth            # headless Google login (prints a link)
# =====================================================================
set -Eeuo pipefail
APPDIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$APPDIR"

SERVICE="briefer.service"
PY="$APPDIR/.venv/bin/python"
PIP="$APPDIR/.venv/bin/pip"
PIDFILE="$APPDIR/data/briefer.pid"
LOG="$APPDIR/data/logs/briefer.out"
LOGFILE="$APPDIR/data/logs/briefer.log"
TMUX_SESSION="briefer"

has_systemd() {
  command -v systemctl >/dev/null 2>&1 && \
  systemctl list-unit-files 2>/dev/null | grep -q "^$SERVICE"
}
has_tmux() { command -v tmux >/dev/null 2>&1; }
tmux_running() { has_tmux && tmux has-session -t "$TMUX_SESSION" 2>/dev/null; }
_sudo() { if [ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null; then sudo "$@"; else "$@"; fi; }

start() {
  if has_systemd; then _sudo systemctl start "$SERVICE"; echo "started (systemd)"; return; fi
  mkdir -p data/logs
  if has_tmux; then
    if tmux_running; then echo "already running (tmux '$TMUX_SESSION')"; return; fi
    tmux new-session -d -s "$TMUX_SESSION" \
      "cd '$APPDIR' && PYTHONPATH='$APPDIR/src' '$PY' -m briefer 2>&1 | tee -a '$LOG'"
    echo "started (tmux '$TMUX_SESSION' — attach with: ./manage.sh attach)"
    return
  fi
  if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "already running (pid $(cat "$PIDFILE"))"; return
  fi
  PYTHONPATH="$APPDIR/src" nohup "$PY" -m briefer >>"$LOG" 2>&1 &
  echo $! > "$PIDFILE"
  echo "started (nohup, pid $(cat "$PIDFILE"))"
}

stop() {
  if has_systemd; then _sudo systemctl stop "$SERVICE"; echo "stopped (systemd)"; return; fi
  if tmux_running; then tmux kill-session -t "$TMUX_SESSION"; echo "stopped (tmux)"; return; fi
  if [ -f "$PIDFILE" ]; then
    kill "$(cat "$PIDFILE")" 2>/dev/null || true
    rm -f "$PIDFILE"; echo "stopped"
  else echo "not running"; fi
}

restart() { stop || true; sleep 1; start; }
refresh() { echo "Refreshing (restart with current code & .env)…"; restart; }

reset() {
  read -rp "This clears sessions, dedup history and pending reminders. Proceed? [y/N] " a
  [ "${a:-N}" = "y" ] || { echo "aborted"; return; }
  stop || true
  rm -f data/briefer.db data/briefer.db-wal data/briefer.db-shm
  echo "state wiped"; start
}

status() {
  if has_systemd; then systemctl status "$SERVICE" --no-pager || true; return; fi
  if tmux_running; then echo "running (tmux '$TMUX_SESSION')"; return; fi
  if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "running (nohup, pid $(cat "$PIDFILE"))"
  else echo "not running"; fi
}

logs() {
  if has_systemd; then _sudo journalctl -u "$SERVICE" -f --no-pager; return; fi
  touch "$LOGFILE"; tail -f "$LOGFILE"
}

# recent errors/warnings (all history), not a follow
errors() {
  touch "$LOGFILE"
  grep -E "ERROR|WARNING|CRITICAL|Traceback|Exception" "$LOGFILE" | tail -n 60 \
    || echo "no errors logged"
}

attach() {
  if tmux_running; then tmux attach -t "$TMUX_SESSION";
  else echo "no tmux session — use ./manage.sh logs"; fi
}

foreground() { PYTHONPATH="$APPDIR/src" exec "$PY" -m briefer; }

update() {
  echo "Pulling latest code… (.env, token.json and data/ are preserved)"
  git pull --ff-only || echo "git pull skipped/failed"
  "$PIP" install -r requirements.txt
  restart
}

google_auth() { PYTHONPATH="$APPDIR/src" "$PY" "$APPDIR/authorize_google.py"; }

cmd="${1:-status}"
case "$cmd" in
  start) start ;; stop) stop ;; restart) restart ;;
  refresh) refresh ;; reset) reset ;; status) status ;;
  logs) logs ;; errors) errors ;; attach) attach ;; foreground|fg) foreground ;; update) update ;;
  google-auth|gauth) google_auth ;;
  *) echo "usage: $0 {start|stop|restart|refresh|reset|status|logs|errors|attach|foreground|update|google-auth}"; exit 1 ;;
esac
