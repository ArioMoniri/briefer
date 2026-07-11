#!/usr/bin/env bash
# =====================================================================
# Briefer — one-shot server installer & setup wizard.
#
#   scp this whole repo to your server, then:  sudo ./setup.sh
#
# It will:
#   • install system deps (python3, venv, pip, git) via your package mgr
#   • create an isolated virtualenv and install the bot
#   • run an interactive wizard that writes a locked-down .env
#   • install a hardened, auto-restarting systemd service (revives on
#     reboot and on crash — "self-healing")
#   • install cron jobs: @reboot fallback start + a health watchdog
#
# Safe to re-run (idempotent). Designed to make NO outbound calls except
# your OS package manager and pip.
# =====================================================================
set -Eeuo pipefail

# --- pretty printing -------------------------------------------------
c()  { printf '\033[%sm' "$1"; }
say(){ printf '%s%s%s\n' "$(c '1;36')" "$*" "$(c 0)"; }
ok(){  printf '%s✓ %s%s\n' "$(c '1;32')" "$*" "$(c 0)"; }
warn(){ printf '%s! %s%s\n' "$(c '1;33')" "$*" "$(c 0)"; }
die(){ printf '%s✗ %s%s\n' "$(c '1;31')" "$*" "$(c 0)" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

APP_NAME="briefer"
SERVICE="${APP_NAME}.service"

# --- 0. privilege / user model --------------------------------------
IS_ROOT=0
[ "$(id -u)" -eq 0 ] && IS_ROOT=1

if [ "$IS_ROOT" -eq 1 ]; then
  RUN_USER="${BRIEFER_USER:-briefer}"
  if ! id "$RUN_USER" >/dev/null 2>&1; then
    say "Creating dedicated service user '$RUN_USER' (no login shell)…"
    useradd --system --create-home --shell /usr/sbin/nologin "$RUN_USER" 2>/dev/null \
      || useradd --system --create-home --shell /bin/false "$RUN_USER"
    ok "Created user $RUN_USER"
  fi
else
  RUN_USER="$(id -un)"
  warn "Not running as root. Installing for current user '$RUN_USER'."
  warn "systemd/cron steps that need root will be skipped with instructions."
fi

# --- 1. system dependencies -----------------------------------------
install_pkgs() {
  local pm=""
  for cand in apt-get dnf yum apk pacman zypper; do
    command -v "$cand" >/dev/null 2>&1 && { pm="$cand"; break; }
  done
  [ -n "$pm" ] || { warn "No known package manager; ensure python3, venv, pip, git are installed."; return; }
  say "Installing system dependencies via $pm…"
  local SUDO=""; [ "$IS_ROOT" -eq 0 ] && command -v sudo >/dev/null && SUDO="sudo"
  case "$pm" in
    apt-get) $SUDO apt-get update -y && $SUDO apt-get install -y python3 python3-venv python3-pip git curl ca-certificates ffmpeg ;;
    dnf|yum) $SUDO "$pm" install -y python3 python3-pip git curl ca-certificates ffmpeg ;;
    apk)     $SUDO apk add --no-cache python3 py3-pip git curl ca-certificates ffmpeg ;;
    pacman)  $SUDO pacman -Sy --noconfirm python python-pip git curl ca-certificates ffmpeg ;;
    zypper)  $SUDO zypper install -y python3 python3-pip git curl ca-certificates ffmpeg ;;
  esac
  ok "System dependencies ready"
}
install_pkgs

command -v python3 >/dev/null || die "python3 not available after install."

# --- 2. virtualenv + python deps ------------------------------------
say "Creating virtualenv…"
python3 -m venv .venv
# shellcheck disable=SC1091
./.venv/bin/python -m pip install --upgrade pip wheel >/dev/null
say "Installing Python dependencies (this can take a minute)…"
./.venv/bin/pip install -r requirements.txt
ok "Python environment ready"

mkdir -p data data/logs data/downloads

# --- 3. .env wizard --------------------------------------------------
rand_pw() { ./.venv/bin/python -c "import secrets; print(secrets.token_urlsafe(18))"; }

if [ -f .env ]; then
  warn ".env already exists — keeping it. Delete it to re-run the wizard."
else
  say "── Setup wizard ─────────────────────────────────────────────"
  echo "Press Enter to accept the [default]."

  read -rp "Telegram bot token (from @BotFather): " TG_TOKEN
  [ -n "${TG_TOKEN:-}" ] || die "A bot token is required."

  read -rp "Anthropic API key: " ANTHROPIC_KEY
  [ -n "${ANTHROPIC_KEY:-}" ] || warn "No API key entered — analysis will fail until you add it."

  read -rp "Allowed Telegram chat id(s), comma-separated (blank = bootstrap): " CHAT_IDS

  DEF_PW="$(rand_pw)"
  read -rp "Login password [random: $DEF_PW]: " LOGIN_PW
  LOGIN_PW="${LOGIN_PW:-$DEF_PW}"

  echo
  say "Google Sheets access — pick how the bot authenticates:"
  echo "  1) Log in with your Google account (OAuth). Sheets are created in"
  echo "     YOUR Drive. This server has no browser, so it prints a LINK you"
  echo "     open on your phone/laptop, then paste a code back. (recommended)"
  echo "  2) Service account (a robot Google identity). You share the sheets"
  echo "     with its email. No personal login."
  read -rp "Choice [1]: " GAUTH_CHOICE
  GAUTH_CHOICE="${GAUTH_CHOICE:-1}"
  if [ "$GAUTH_CHOICE" = "2" ]; then
    GAUTH_MODE="service_account"
    read -rp "Path to Google service-account JSON [service_account.json]: " SA_FILE
    SA_FILE="${SA_FILE:-service_account.json}"
    OAUTH_CLIENT="client_secret.json"
  else
    GAUTH_MODE="oauth"
    SA_FILE="service_account.json"
    read -rp "Path to your OAuth 'Desktop app' client JSON [client_secret.json]: " OAUTH_CLIENT
    OAUTH_CLIENT="${OAUTH_CLIENT:-client_secret.json}"
  fi

  read -rp "Articles spreadsheet id (blank = auto-create): " ART_ID
  read -rp "Events spreadsheet id (blank = auto-create): " EVT_ID

  read -rp "Model [claude-sonnet-5]: " MODEL
  MODEL="${MODEL:-claude-sonnet-5}"
  read -rp "Verification model [claude-opus-4-8]: " VMODEL
  VMODEL="${VMODEL:-claude-opus-4-8}"
  read -rp "Timezone [Europe/Istanbul]: " TZ_IN
  TZ_IN="${TZ_IN:-Europe/Istanbul}"

  echo
  say "Media parsing (tweets + video/audio transcription):"
  echo "  X/Twitter is OPTIONAL — tweets are read without keys via a fallback."
  echo "  Keys only help on a PAID X tier (Free can't read tweets)."
  read -rp "X bearer token (blank to use consumer key/secret or the fallback): " XTOKEN
  XCKEY=""; XCSECRET=""
  if [ -z "${XTOKEN:-}" ]; then
    read -rp "X consumer key / API Key (optional): " XCKEY
    read -rp "X consumer secret / API Key Secret (optional): " XCSECRET
  fi
  read -rp "Transcribe videos/audio with local Whisper? [Y/n]: " TR_IN
  case "${TR_IN:-Y}" in n|N) ENABLE_TR=0 ;; *) ENABLE_TR=1 ;; esac
  read -rp "Whisper model (tiny/base/small/medium) [base]: " WMODEL
  WMODEL="${WMODEL:-base}"

  BOOT=0; [ -z "${CHAT_IDS:-}" ] && BOOT=1

  umask 077
  cat > .env <<EOF
TELEGRAM_BOT_TOKEN=${TG_TOKEN}
ALLOWED_CHAT_IDS=${CHAT_IDS}
# Admins can /allow and /deny other chats at runtime. Defaults to the ids
# above when left blank.
ADMIN_CHAT_IDS=${CHAT_IDS}
LOGIN_PASSWORD=${LOGIN_PW}
BRIEFER_BOOTSTRAP=${BOOT}
ANTHROPIC_API_KEY=${ANTHROPIC_KEY}
ANTHROPIC_MODEL=${MODEL}
ANTHROPIC_VERIFY_MODEL=${VMODEL}
GOOGLE_AUTH_MODE=${GAUTH_MODE}
GOOGLE_SERVICE_ACCOUNT_FILE=${SA_FILE}
GOOGLE_OAUTH_CLIENT_FILE=${OAUTH_CLIENT}
GOOGLE_TOKEN_FILE=token.json
ARTICLES_SHEET_ID=${ART_ID}
EVENTS_SHEET_ID=${EVT_ID}
TWITTER_BEARER_TOKEN=${XTOKEN}
TWITTER_CONSUMER_KEY=${XCKEY}
TWITTER_CONSUMER_SECRET=${XCSECRET}
ENABLE_TRANSCRIPTION=${ENABLE_TR}
WHISPER_MODEL=${WMODEL}
TRANSCRIPTION_MAX_SECONDS=1800
MEDIA_MAX_BYTES=50000000
COMPANY_NAME=Vivax
COMPANY_URL=https://getvivax.com
COMPANY_FOCUS=Medical AI, medical education, clinical simulation, and operating-room intelligence ("Reality Motors").
MAX_DOWNLOAD_BYTES=15000000
RATE_LIMIT_PER_MINUTE=20
DEADLINE_REMINDER_HOURS=72,24,3
TIMEZONE=${TZ_IN}
DATA_DIR=data
LOG_LEVEL=INFO
EOF
  chmod 600 .env
  ok "Wrote .env (permissions 600)"

  # Offer to run the headless Google login now (OAuth mode).
  if [ "$GAUTH_MODE" = "oauth" ]; then
    if [ -f "$OAUTH_CLIENT" ]; then
      say "Let's log in to Google now (a link will be printed)…"
      ./.venv/bin/python authorize_google.py || \
        warn "Google login not completed — run it later: ./manage.sh google-auth"
    else
      warn "OAuth client file '$OAUTH_CLIENT' not found yet."
      warn "Create a 'Desktop app' OAuth client in Google Cloud, save the JSON"
      warn "here as '$OAUTH_CLIENT', then run: ./manage.sh google-auth"
    fi
  fi

  if [ "$BOOT" -eq 1 ]; then
    warn "No chat id set → BOOTSTRAP mode. Start the bot, send /whoami, put the"
    warn "id in ALLOWED_CHAT_IDS + ADMIN_CHAT_IDS, set BRIEFER_BOOTSTRAP=0, then: ./manage.sh restart"
  fi
  echo "Your login password is: $LOGIN_PW"
fi

# --- 4. ownership (least privilege) ---------------------------------
# The service user must NOT be able to modify code/scripts, otherwise a
# compromise of the bot could rewrite a script that root's cron watchdog
# later executes (local privilege escalation). So: code stays root-owned
# and non-writable by the service user; only data/ is writable; .env is
# group-readable by the service user but not writable.
if [ "$IS_ROOT" -eq 1 ]; then
  chown -R root:root "$SCRIPT_DIR"
  chmod -R go-w "$SCRIPT_DIR"                 # nobody but root may write code
  chmod 755 manage.sh setup.sh deploy/healthcheck.sh 2>/dev/null || true
  # Writable runtime state for the service user only.
  mkdir -p "$SCRIPT_DIR/data"
  chown -R "$RUN_USER":"$RUN_USER" "$SCRIPT_DIR/data"
  # Secrets: readable (not writable) by the service user via its group.
  chown "root:$RUN_USER" .env 2>/dev/null || true
  chmod 640 .env 2>/dev/null || true
  # Google credential files, if present: readable by the service user, not writable.
  for f in service_account.json token.json client_secret.json "${OAUTH_CLIENT:-}" "${SA_FILE:-}"; do
    [ -n "${f:-}" ] && [ -f "$f" ] && { chown "root:$RUN_USER" "$f"; chmod 640 "$f"; }
  done
fi

# --- 5. systemd service (self-healing) ------------------------------
install_systemd() {
  command -v systemctl >/dev/null 2>&1 || { warn "systemd not found; skipping service."; return 1; }
  [ "$IS_ROOT" -eq 1 ] || { warn "Need root for systemd. Run: sudo ./setup.sh"; return 1; }
  say "Installing hardened systemd service…"
  sed -e "s|@APPDIR@|$SCRIPT_DIR|g" -e "s|@USER@|$RUN_USER|g" \
      deploy/briefer.service > "/etc/systemd/system/$SERVICE"
  systemctl daemon-reload
  systemctl enable "$SERVICE"          # revive on every reboot
  systemctl restart "$SERVICE"
  ok "systemd service installed & enabled (auto-restart + boot revival)"
  return 0
}

install_cron() {
  [ "$IS_ROOT" -eq 1 ] || { warn "Skipping cron watchdog (need root)."; return; }
  say "Installing cron watchdog + reboot fallback…"
  local cronfile="/etc/cron.d/${APP_NAME}"
  cat > "$cronfile" <<EOF
# Briefer self-healing watchdog. Restarts the service if it's down.
*/5 * * * * root $SCRIPT_DIR/deploy/healthcheck.sh >> $SCRIPT_DIR/data/logs/watchdog.log 2>&1
# Fallback boot start in case systemd unit is ever removed.
@reboot root $SCRIPT_DIR/deploy/healthcheck.sh >> $SCRIPT_DIR/data/logs/watchdog.log 2>&1
EOF
  chmod 644 "$cronfile"
  ok "Cron watchdog installed (every 5 min + @reboot)"
}

chmod +x manage.sh deploy/healthcheck.sh 2>/dev/null || true

if install_systemd; then
  install_cron
  echo
  ok "Briefer is installed and running."
  echo "  Logs:    journalctl -u $SERVICE -f   (or ./manage.sh logs)"
  echo "  Control: ./manage.sh {start|stop|restart|refresh|reset|status|update}"
else
  echo
  warn "Running without systemd. Start it manually with:"
  echo "  ./manage.sh start        # background via nohup"
  echo "  ./manage.sh foreground   # attached, for debugging"
fi

echo
say "Done. Send /start to your bot on Telegram."
