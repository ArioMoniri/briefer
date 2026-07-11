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
  # Add any new settings introduced by the update (prompts if interactive).
  [ -f "$APPDIR/.env" ] && "$PY" "$APPDIR/configure.py" || true
  restart
}

google_auth() { PYTHONPATH="$APPDIR/src" "$PY" "$APPDIR/authorize_google.py"; }

# Fill in any missing/new config (prompts only for empty settings).
reconfigure() { "$PY" "$APPDIR/configure.py"; }

# Interactive login on the (headless) server via a virtual display + VNC.
# You connect a VNC viewer through an SSH tunnel, log in, and the session is
# saved to a persistent profile the bot reuses.
browser_login() {
  command -v "$APPDIR/.venv/bin/playwright" >/dev/null 2>&1 || {
    echo "Browser not installed. Run first: ./manage.sh enable-browser"; exit 1; }
  if ! command -v Xvfb >/dev/null 2>&1 || ! command -v x11vnc >/dev/null 2>&1; then
    echo "Installing xvfb + x11vnc (needed to show a browser on a headless server)…"
    local pm=""; for c in apt-get dnf yum apk pacman zypper; do command -v "$c" >/dev/null 2>&1 && { pm="$c"; break; }; done
    case "$pm" in
      apt-get) _sudo apt-get update -y && _sudo apt-get install -y xvfb x11vnc ;;
      dnf|yum) _sudo "$pm" install -y xorg-x11-server-Xvfb x11vnc ;;
      apk)     _sudo apk add --no-cache xvfb x11vnc ;;
      pacman)  _sudo pacman -Sy --noconfirm xorg-server-xvfb x11vnc ;;
      zypper)  _sudo zypper install -y xvfb-run x11vnc ;;
      *) echo "Install xvfb and x11vnc manually, then re-run."; exit 1 ;;
    esac
  fi
  # A VNC password is REQUIRED — macOS Screen Sharing hangs on no-auth VNC.
  local vpass="${VNC_PASSWORD:-briefer}"
  Xvfb :99 -screen 0 1360x900x24 >/dev/null 2>&1 &
  local xvfb=$!
  sleep 2
  x11vnc -display :99 -localhost -rfbport 5900 -passwd "$vpass" \
    -forever -shared -quiet >/dev/null 2>&1 &
  local vnc=$!
  sleep 1
  if ! kill -0 "$vnc" 2>/dev/null; then
    echo "x11vnc failed to start. Is it installed? Try: which x11vnc"; kill "$xvfb" 2>/dev/null; return 1
  fi
  echo
  echo "──────────────────────────────────────────────────────────────"
  echo " 1) On YOUR computer, open an SSH tunnel to this server:"
  echo "      ssh -L 5900:localhost:5900 <you>@<this-server>"
  echo " 2) Open a VNC viewer and connect to:  localhost:5900"
  echo "      VNC password:  $vpass   ← type THIS (not your Mac/server password)"
  echo " 3) In the browser window, log in to LinkedIn / Instagram / etc."
  echo " 4) Come BACK HERE and press Enter to save the session."
  echo "──────────────────────────────────────────────────────────────"
  DISPLAY=:99 PYTHONPATH="$APPDIR/src" \
    BROWSER_PROFILE_DIR="$APPDIR/browser_profile" \
    BROWSER_STORAGE_STATE="$APPDIR/storage_state.json" \
    "$PY" "$APPDIR/login_browser.py" || true
  kill "$vnc" "$xvfb" 2>/dev/null || true
  echo "Login saved. Now: ./manage.sh restart"
}

# Install the optional headless-browser fallback (Playwright + Chromium).
enable_browser() {
  echo "Installing Playwright + Chromium into the venv (adds a browser download)…"
  "$PIP" install playwright==1.48.0
  if [ "$(id -u)" -eq 0 ]; then
    "$APPDIR/.venv/bin/playwright" install --with-deps chromium
  else
    "$APPDIR/.venv/bin/playwright" install chromium \
      || echo "If Chromium fails to launch, run as root: sudo ./manage.sh enable-browser"
  fi
  echo "Browser fallback ready. It activates automatically for pages that don't"
  echo "return text to a plain fetch. Restart:  ./manage.sh restart"
}

cmd="${1:-status}"
case "$cmd" in
  start) start ;; stop) stop ;; restart) restart ;;
  refresh) refresh ;; reset) reset ;; status) status ;;
  logs) logs ;; errors) errors ;; attach) attach ;; foreground|fg) foreground ;; update) update ;;
  google-auth|gauth) google_auth ;;
  reconfigure|configure) reconfigure ;;
  enable-browser) enable_browser ;;
  browser-login) browser_login ;;
  *) echo "usage: $0 {start|stop|restart|refresh|reset|status|logs|errors|attach|foreground|update|google-auth|reconfigure|enable-browser|browser-login}"; exit 1 ;;
esac
