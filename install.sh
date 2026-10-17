#!/usr/bin/env bash
# =============================================================================
#  Telegram leech bot - one-command installer
#
#    bash <(curl -Ls https://raw.githubusercontent.com/YOUR-USER/leechbot/main/install.sh)
#
#  It downloads the bot, installs everything it needs, asks for your Telegram
#  credentials, starts the service and adds a `leechbot` management command.
#
#  Other ways to run it:
#    sudo bash install.sh                           (from an unpacked copy)
#    sudo bash install.sh -y --api-id 123 --api-hash <hash> --bot-token <token> --owner-id 456
#    sudo bash install.sh --update                  (fetch the newest version, keep settings)
#    sudo bash install.sh --uninstall [--purge]
#
#  Supports Debian/Ubuntu (apt); Fedora/RHEL (dnf/yum) is best-effort.
#  Safe to re-run: it updates the code and dependencies and keeps your settings.
# =============================================================================
set -Eeuo pipefail

ORIG_ARGS=("$@")
FWD_ARGS=()   # arguments handed on to the downloaded installer (everything except --update)
for _a in "$@"; do [[ "$_a" == --update ]] || FWD_ARGS+=("$_a"); done
SELF="${BASH_SOURCE[0]:-}"
# Only a real file on disk has "files next to it". A pipe (curl | bash) or /dev/fd/N (bash <(curl ...))
# has none, so it must always download the bot, never pick up whatever the current directory holds.
if [[ -f "$SELF" && "$SELF" != /dev/* && "$SELF" != /proc/* ]]; then
  SRC_DIR="$(cd "$(dirname "$SELF")" && pwd)"
else
  SRC_DIR=""
fi
BOOT_TMP=""
# Where the bot's files come from when this script is run on its own (set this once when you publish).
REPO="${LEECHBOT_REPO:-YOUR-USER/leechbot}"
REF="${LEECHBOT_REF:-main}"
SOURCE_URL="${LEECHBOT_URL:-}"
FORCE_DOWNLOAD=0
BIN_DIR="${LEECHBOT_BIN_DIR:-/usr/local/bin}"
INSTALL_DIR="/opt/leechbot"
SVC_USER="leechbot"
UNIT_DIR="${LEECHBOT_UNIT_DIR:-/etc/systemd/system}"
SWAP_MB=1024
# A fresh server is often busy with unattended-upgrades holding the apt lock: wait for it instead of failing.
APT=(apt-get -o DPkg::Lock::Timeout=300 -o Acquire::Retries=3)
PIP_OPTS=(--no-cache-dir -q --default-timeout 60 --retries 5)
DO_SWAP=1 DO_DENO=1 DO_SYSTEMD=1 DO_TOKEN_CHECK=1 DO_ZRAM=1 DO_EARLYOOM=1
ASSUME_YES=0 UNINSTALL=0 PURGE=0
API_ID="${API_ID:-}" API_HASH="${API_HASH:-}" BOT_TOKEN="${BOT_TOKEN:-}" OWNER_ID="${OWNER_ID:-}"
BOT_USERNAME=""
TOKEN_DEFERRED=0
DETECT_OWNER=0
TG_API="${LEECHBOT_TG_API:-https://api.telegram.org}"

RE_ID='^[0-9]{1,15}$'
RE_HASH='^[0-9a-fA-F]{32}$'
RE_TOKEN='^[0-9]{5,}:[A-Za-z0-9_-]{30,}$'

# ---------- output helpers ----------------------------------------------------
if [[ -t 1 ]]; then
  C_B=$'\e[1m' C_G=$'\e[32m' C_Y=$'\e[33m' C_R=$'\e[31m' C_0=$'\e[0m'
else
  C_B="" C_G="" C_Y="" C_R="" C_0=""
fi
step() { printf '\n%s==> %s%s\n' "$C_B" "$*" "$C_0"; }
info() { printf '    %s\n' "$*"; }
ok()   { printf '    %s✔%s %s\n' "$C_G" "$C_0" "$*"; }
warn() { printf '    %s!%s %s\n' "$C_Y" "$C_0" "$*" >&2; }
die()  { printf '\n%sERROR:%s %s\n' "$C_R" "$C_0" "$*" >&2; exit 1; }

LOG="$(mktemp /tmp/leechbot-install.XXXXXX.log)"
trap 'printf "\n%sInstall failed at line %s (command: %s).\nDetails: %s%s\n" "$C_R" "$LINENO" "$BASH_COMMAND" "$LOG" "$C_0" >&2' ERR

# run CMD...: quiet, logs output, dies with the log tail on failure
run() {
  if ! "$@" >>"$LOG" 2>&1; then
    tail -n 25 "$LOG" >&2
    die "Command failed: $*"
  fi
}
# run_try CMD...: quiet, returns the exit status instead of dying
run_try() { "$@" >>"$LOG" 2>&1; }

usage() {
  cat <<EOF
Telegram leech bot installer

Options:
  --api-id ID          Telegram API ID        (my.telegram.org)
  --api-hash HASH      Telegram API hash
  --bot-token TOKEN    Bot token              (@BotFather)
  --owner-id ID        Your numeric Telegram user ID (@userinfobot)
  --detect-owner       Find your user ID by tapping a link in Telegram (no ID needed)
  --dir PATH           Install location        (default: $INSTALL_DIR)
  --user NAME          Service user            (default: $SVC_USER)
  --swap-size MB       Swap file size if one is created (default: $SWAP_MB)
  --no-swap            Never create a swap file
  --no-zram            Don't set up compressed swap in RAM (zram)
  --no-earlyoom        Don't install earlyoom (kills the right process before the server thrashes)
  --no-deno            Don't install the Deno JS runtime (YouTube needs one!)
  --no-systemd         Don't install systemd units
  --skip-token-check   Don't validate the bot token against api.telegram.org
  -y, --yes            Non-interactive: never prompt, fail if input is missing
  --update             Download the newest version and install it (settings are kept)
  --uninstall          Remove the service (add --purge to delete data + user)
  -h, --help           Show this help

Credentials can also be given as environment variables (API_ID, API_HASH,
BOT_TOKEN, OWNER_ID). If the bot is already installed, existing values in
its .env are reused.

Where the bot is downloaded from (when this script runs without the bot's files):
  LEECHBOT_REPO=user/repo   GitHub repository   (default: $REPO)
  LEECHBOT_REF=main         branch or tag
  LEECHBOT_URL=<url>        exact tarball URL (overrides the two above)
  LEECHBOT_SHA256=<hash>    refuse to install unless the download matches
EOF
}

# ---------- argument parsing --------------------------------------------------
need_val() { [[ $# -ge 2 && -n "${2:-}" ]] || die "Option $1 needs a value."; }
while [[ $# -gt 0 ]]; do
  case "$1" in
    --api-id)      need_val "$@"; API_ID="$2"; shift 2 ;;
    --api-hash)    need_val "$@"; API_HASH="$2"; shift 2 ;;
    --bot-token)   need_val "$@"; BOT_TOKEN="$2"; shift 2 ;;
    --owner-id)    need_val "$@"; OWNER_ID="$2"; shift 2 ;;
    --detect-owner) DETECT_OWNER=1; shift ;;
    --dir)         need_val "$@"; INSTALL_DIR="$2"; shift 2 ;;
    --user)        need_val "$@"; SVC_USER="$2"; shift 2 ;;
    --swap-size)   need_val "$@"; SWAP_MB="$2"; shift 2 ;;
    --no-swap)     DO_SWAP=0; shift ;;
    --no-zram)     DO_ZRAM=0; shift ;;
    --no-earlyoom) DO_EARLYOOM=0; shift ;;
    --no-deno)     DO_DENO=0; shift ;;
    --no-systemd)  DO_SYSTEMD=0; shift ;;
    --skip-token-check) DO_TOKEN_CHECK=0; shift ;;
    -y|--yes)      ASSUME_YES=1; shift ;;
    --update)      FORCE_DOWNLOAD=1; shift ;;
    --uninstall)   UNINSTALL=1; shift ;;
    --purge)       PURGE=1; shift ;;
    -h|--help)     usage; exit 0 ;;
    *)             die "Unknown option: $1 (see --help)" ;;
  esac
done
[[ "$SWAP_MB" =~ ^[0-9]+$ ]] || die "--swap-size must be a number of MB."
[[ "$INSTALL_DIR" == /* ]] || die "--dir must be an absolute path."
[[ "$INSTALL_DIR" =~ ^/[A-Za-z0-9._/-]+$ ]] || die "--dir may only contain letters, digits and . _ - /"
case "${INSTALL_DIR%/}" in
  ""|/bin|/boot|/dev|/etc|/home|/lib*|/mnt|/opt|/proc|/root|/run|/sbin|/srv|/sys|/tmp|/usr|/var)
    die "Refusing to use '$INSTALL_DIR' as the install dir. Pick a dedicated folder (e.g. /opt/leechbot)." ;;
esac
[[ "$SVC_USER" =~ ^[a-z_][a-z0-9_-]{0,31}$ && "$SVC_USER" != root ]] || die "Invalid or unsafe --user '$SVC_USER'."
ENV_FILE="$INSTALL_DIR/.env"
VENV="$INSTALL_DIR/venv"

INTERACTIVE=0
setup_tty() {
  # `curl ... | bash` feeds the script through stdin, so reconnect the keyboard. This is only safe
  # inside a function (already fully parsed) and when the last line `exit`s before reading more.
  if [[ ! -t 0 && $ASSUME_YES -eq 0 ]] && { : </dev/tty; } 2>/dev/null; then exec </dev/tty; fi
  if [[ -t 0 && -t 1 && $ASSUME_YES -eq 0 ]]; then INTERACTIVE=1; fi
}

# ---------- root ---------------------------------------------------------------
if [[ $EUID -ne 0 ]]; then
  # A real file can be re-run through sudo. A pipe or /dev/fd/N (bash <(curl ...)) can't, so say how.
  if [[ -f "$SELF" && "$SELF" != /dev/* && "$SELF" != /proc/* ]] && command -v sudo >/dev/null 2>&1; then
    exec sudo -E bash "$SELF" "${ORIG_ARGS[@]+"${ORIG_ARGS[@]}"}"
  fi
  die "Please run as root, e.g.:  sudo bash <(curl -Ls <install.sh URL>)"
fi

MEM_MB="${LEECHBOT_MEM_MB:-$(awk '/^MemTotal:/{printf "%d", $2/1024}' /proc/meminfo)}"

# ---------- uninstall ----------------------------------------------------------
have_systemd() { [[ -d /run/systemd/system ]]; }

do_uninstall() {
  step "Uninstalling leech bot"
  if have_systemd; then
    systemctl disable --now leechbot.service leechbot-ytdlp-update.timer >>"$LOG" 2>&1 || true
  fi
  rm -f "$UNIT_DIR/leechbot.service" "$UNIT_DIR/leechbot-ytdlp-update.service" \
        "$UNIT_DIR/leechbot-ytdlp-update.timer" "$BIN_DIR/leechbot"
  have_systemd && systemctl daemon-reload || true
  ok "Service and timer removed"

  if (( PURGE )); then
    if (( ! ASSUME_YES )); then
      (( INTERACTIVE )) || die "--purge in non-interactive mode needs -y."
      read -rp "    Delete $INSTALL_DIR (config, session, venv) and user '$SVC_USER'? [y/N] " a
      [[ "$a" =~ ^[Yy]$ ]] || { info "Kept your data."; exit 0; }
    fi
    rm -rf "$INSTALL_DIR"
    local home; home="$(getent passwd "$SVC_USER" | cut -d: -f6 || true)"
    if [[ -n "$home" && "$home" == "$INSTALL_DIR" ]]; then
      userdel "$SVC_USER" || true
      ok "Removed $INSTALL_DIR and user $SVC_USER"
    else
      ok "Removed $INSTALL_DIR"
      [[ -n "$home" ]] && warn "User '$SVC_USER' wasn't created by this installer (home is $home); left it alone."
    fi
  else
    info "Kept $INSTALL_DIR (config, session). Use --purge to delete it."
  fi
  info "Left in place (shared): system packages, /usr/local/bin/deno, swap/zram, earlyoom."
  exit 0
}

# ---------- configuration ------------------------------------------------------
# Older versions of the installer copied the example file, placeholders and all. Text placeholders
# (your_api_hash...) can never be real. The numeric ones (123456, 123456789) could be, so they are only
# discarded when the same file also contains those text placeholders, i.e. it is an untouched example.
env_get() {
  local v=""
  if [[ -f "$ENV_FILE" ]]; then v="$(sed -n "s/^$1=//p" "$ENV_FILE" | tail -n1)"; fi
  case "$v" in
    your_api_hash|*your-bot-token*|*your_bot_token*) v="" ;;
    123456|123456789) if grep -qE 'your_api_hash|your-bot-token' "$ENV_FILE" 2>/dev/null; then v=""; fi ;;
  esac
  printf '%s' "$v"
}

# Pasted values often carry terminal junk: bracketed-paste markers (ESC[200~ ... ESC[201~), carriage returns,
# stray spaces or wrapping quotes. None of our values (IDs, hashes, tokens) may contain any of those.
sanitize() {
  local v="$1" esc=$'\e'
  v="${v//${esc}\[200~/}"; v="${v//${esc}\[201~/}"
  v="${v//\[200~/}"; v="${v//\[201~/}"        # some terminals drop the ESC byte but leave the rest
  v="${v//$esc/}"
  v="${v//[[:space:]]/}"
  v="${v#[\"\']}"; v="${v%[\"\']}"
  printf '%s' "$v"
}

# read_value VAR "prompt" secret: read one line into VAR. Never dies on odd terminals or EOF without saying why.
read_value() {
  local __var=$1 __prompt=$2 __secret=${3:-0} __line="" __rc=0
  if (( __secret )); then
    IFS= read -rsp "$__prompt" __line || __rc=$?
    echo
  else
    IFS= read -rp "$__prompt" __line || __rc=$?
  fi
  (( __rc == 0 )) || die "Input ended unexpectedly (is this an interactive terminal?). Run the installer in a normal terminal, or pass the values as options (see --help)."
  printf -v "$__var" '%s' "$__line"
}

# what a valid value looks like, for the "try again" message
expected() {
  case "$1" in
    API_ID)    echo "digits only, e.g. 1234567" ;;
    API_HASH)  echo "32 characters, 0-9 and a-f" ;;
    BOT_TOKEN) echo "like 123456789:AAH... (from @BotFather)" ;;
    OWNER_ID)  echo "digits only" ;;
    *)         echo "see the hint above" ;;
  esac
}

# ask VAR "Prompt" REGEX [secret] [hint]
ask() {
  local var=$1 prompt=$2 re=$3 secret=${4:-0} hint=${5:-} val="${!1:-}" flag typed=0
  flag="--$(printf '%s' "${var//_/-}" | tr '[:upper:]' '[:lower:]')"
  val="$(sanitize "$val")"
  if [[ -z "$val" ]]; then
    val="$(env_get "$var")"
    if [[ -n "$val" ]]; then info "$var: using the existing value from $ENV_FILE"; fi
  fi
  while true; do
    if [[ -z "$val" ]]; then
      (( INTERACTIVE )) || die "Missing $var. Run this in an interactive terminal (over SSH use: ssh -t host), or pass $flag / set $var."
      if [[ -n "$hint" && $typed -eq 0 ]]; then info "$hint"; fi
      read_value val "    $prompt: " "$secret"
      val="$(sanitize "$val")"; typed=1
    fi
    if [[ "$var" == API_HASH ]]; then val="${val,,}"; fi   # my.telegram.org shows it in lowercase
    if [[ "$val" =~ $re ]]; then
      printf -v "$var" '%s' "$val"
      if (( secret && typed )); then info "received ${#val} characters (ends ...${val: -4})"; fi
      return 0
    fi
    (( INTERACTIVE )) || die "$var has an invalid format (expected: $(expected "$var"))."
    warn "That doesn't look right: got ${#val} characters. Expected: $(expected "$var"). Try again."
    val=""
  done
}

check_token() {
  (( DO_TOKEN_CHECK )) || return 0
  local body code
  body="$(mktemp)"
  # Token goes via stdin config so it never shows up in `ps`.
  code="$(printf 'url = "%s/bot%s/getMe"\n' "$TG_API" "$BOT_TOKEN" \
          | curl -K - -sS --max-time 15 -o "$body" -w '%{http_code}' 2>/dev/null)" || code=000
  case "$code" in
    200)
      BOT_USERNAME="$(sed -n 's/.*"username" *: *"\([^"]*\)".*/\1/p' "$body")"
      ok "Bot token is valid${BOT_USERNAME:+ (@$BOT_USERNAME)}" ;;
    401|404)
      rm -f "$body"
      die "Telegram rejected the bot token. Double-check it with @BotFather." ;;
    *)
      warn "Couldn't reach api.telegram.org (HTTP $code); skipping token validation." ;;
  esac
  rm -f "$body"
}

collect_config() {
  step "Configuration"
  ask API_ID    "API ID"        "$RE_ID"    0 "Get API ID/hash at https://my.telegram.org -> API development tools"
  ask API_HASH  "API hash"      "$RE_HASH"  1
  ask BOT_TOKEN "Bot token"     "$RE_TOKEN" 1 "Create a bot with @BotFather and paste its token."
  # Validate right away: a typo should be reported now, not after you've answered everything else.
  if command -v curl >/dev/null 2>&1; then check_token; else TOKEN_DEFERRED=1; fi
  if [[ -n "$OWNER_ID" || -n "$(env_get OWNER_ID)" ]]; then
    DETECT_OWNER=0
    ask OWNER_ID "Your Telegram user ID" "$RE_ID" 0
  elif (( DETECT_OWNER )); then
    info "Your Telegram user ID will be detected near the end of the install."
  elif (( INTERACTIVE )); then
    info "Only your Telegram account may use the bot. Paste your numeric user ID (from @userinfobot),"
    info "or just press Enter and I'll detect it with a one-tap link at the end."
    while true; do
      read_value OWNER_ID "    Your Telegram user ID (Enter = detect automatically): "
      OWNER_ID="$(sanitize "$OWNER_ID")"
      if [[ -z "$OWNER_ID" ]]; then DETECT_OWNER=1; break; fi
      [[ "$OWNER_ID" =~ $RE_ID ]] && break
      warn "That doesn't look right, try again."
    done
  else
    die "Missing OWNER_ID (use --owner-id, or --detect-owner to link your account by tapping a link in Telegram)."
  fi
}

# set_env KEY VALUE: replace the line in .env (or append it)
set_env() {
  K="$1" V="$2" awk 'BEGIN{FS=OFS="="; k=ENVIRON["K"]; v=ENVIRON["V"]}
       $1==k {print k "=" v; d=1; next} {print}
       END {if (!d) print k "=" v}' "$ENV_FILE" > "$ENV_FILE.tmp"
  mv "$ENV_FILE.tmp" "$ENV_FILE"
  chmod 600 "$ENV_FILE"   # the file holds the bot token: every rewrite must keep it private
}

# ---------- system setup -------------------------------------------------------
detect_pm() {
  if   command -v apt-get >/dev/null 2>&1; then PM=apt
  elif command -v dnf     >/dev/null 2>&1; then PM=dnf
  elif command -v yum     >/dev/null 2>&1; then PM=yum
  else die "Unsupported distribution: need apt, dnf or yum."; fi
}

fetch() {  # fetch URL DEST
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL --retry 3 --connect-timeout 20 -o "$2" "$1"
  elif command -v wget >/dev/null 2>&1; then
    wget -q -T 30 -t 3 -O "$2" "$1"
  else
    return 1
  fi
}

ensure_downloader() {
  if command -v curl >/dev/null 2>&1 || command -v wget >/dev/null 2>&1; then return 0; fi
  case "$PM" in
    apt) export DEBIAN_FRONTEND=noninteractive; run "${APT[@]}" update -qq; run "${APT[@]}" install -y -qq curl ca-certificates ;;
    dnf|yum) run "$PM" install -y -q curl ca-certificates ;;
  esac
}

# Run on its own (bash <(curl ...)) or with --update: fetch the bot's files, then hand over to the
# downloaded copy of this installer with the same arguments.
bootstrap() {
  if (( ! FORCE_DOWNLOAD )) && [[ -n "$SRC_DIR" && -f "$SRC_DIR/bot/main.py" ]]; then return 0; fi
  local url="$SOURCE_URL"
  if [[ -z "$url" && -f "$INSTALL_DIR/.source" ]]; then url="$(sed -n 's/^LEECHBOT_URL=//p' "$INSTALL_DIR/.source" | tail -n1)"; fi
  [[ -n "$url" ]] || url="https://github.com/${REPO}/archive/${REF}.tar.gz"
  [[ "$url" != *YOUR-USER* ]] || die "This copy of the installer doesn't know where the bot is hosted. Set LEECHBOT_REPO=<user>/<repo> (or LEECHBOT_URL=<tarball url>), or edit REPO at the top of install.sh."

  step "Downloading the leech bot"
  ensure_downloader
  BOOT_TMP="$(mktemp -d /tmp/leechbot-src.XXXXXX)"
  trap 'rm -rf "$BOOT_TMP"' EXIT
  trap 'rm -rf "$BOOT_TMP"; exit 130' INT
  trap 'rm -rf "$BOOT_TMP"; exit 143' TERM
  if ! fetch "$url" "$BOOT_TMP/pkg.tar.gz"; then die "Download failed: $url"; fi
  if [[ -n "${LEECHBOT_SHA256:-}" ]]; then
    local got; got="$(sha256sum "$BOOT_TMP/pkg.tar.gz" | cut -d' ' -f1)"
    if [[ "$got" != "${LEECHBOT_SHA256,,}" ]]; then die "Checksum mismatch (expected ${LEECHBOT_SHA256}, got ${got}). Refusing to install."; fi
    ok "Checksum verified"
  fi
  mkdir -p "$BOOT_TMP/src"
  if ! tar -xzf "$BOOT_TMP/pkg.tar.gz" -C "$BOOT_TMP/src" --strip-components=1 2>/dev/null; then die "That download isn't a valid archive: $url"; fi
  if [[ ! -f "$BOOT_TMP/src/install.sh" || ! -f "$BOOT_TMP/src/bot/main.py" ]]; then die "That archive doesn't look like the leech bot: $url"; fi
  ok "Downloaded $(du -h "$BOOT_TMP/pkg.tar.gz" | cut -f1)"

  local rc=0
  LEECHBOT_URL="$url" bash "$BOOT_TMP/src/install.sh" "${FWD_ARGS[@]+"${FWD_ARGS[@]}"}" || rc=$?
  exit "$rc"
}

preflight() {
  step "Pre-flight checks"
  [[ -f "$SRC_DIR/bot/main.py" ]] || die "Run this script from the project folder (bot/main.py not found next to it)."
  ok "Package manager: $PM, RAM: ${MEM_MB} MB"
  mkdir -p "$INSTALL_DIR"
  local free_mb; free_mb="$(df -Pm "$INSTALL_DIR" | awk 'NR==2{print $4}')"
  if (( free_mb < 1200 )); then
    warn "Only ${free_mb} MB free disk. The install needs ~1 GB (ffmpeg, Deno, Python deps) plus room for downloads."
  else
    ok "Free disk: ${free_mb} MB"
  fi
}

pkg_install() {  # pkg_install NAME...  (quiet; returns the exit status instead of dying)
  case "$PM" in
    apt) run_try "${APT[@]}" install -y -qq "$@" ;;
    *)   run_try "$PM" install -y -q "$@" ;;
  esac
}

install_packages() {
  step "Installing system packages (this can take a few minutes)"
  case "$PM" in
    apt)
      export DEBIAN_FRONTEND=noninteractive
      run "${APT[@]}" update -qq
      run "${APT[@]}" install -y -qq --no-install-recommends \
        python3 python3-venv python3-pip python3-dev gcc libc6-dev ffmpeg curl ca-certificates unzip ;;
    dnf|yum)
      run "$PM" install -y -q python3 python3-pip python3-devel gcc curl ca-certificates unzip
      run_try "$PM" install -y -q ffmpeg-free || run_try "$PM" install -y -q ffmpeg \
        || warn "ffmpeg isn't in your repos (try enabling RPM Fusion/EPEL). Video merging and thumbnails need it." ;;
  esac
  ok "Packages installed"

  PY="$(command -v python3)" || die "python3 not found after install."
  "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
    || die "Python 3.10+ is required (found: $("$PY" -V 2>&1)). Use a newer distro release."
  ok "$("$PY" -V 2>&1)"
}

create_user() {
  step "Service user and files"
  if id -u "$SVC_USER" >/dev/null 2>&1; then
    ok "User '$SVC_USER' exists"
  else
    local shell; shell="$(command -v nologin || echo /usr/sbin/nologin)"
    run useradd --system --no-create-home --home-dir "$INSTALL_DIR" --shell "$shell" "$SVC_USER"
    ok "Created system user '$SVC_USER'"
  fi
}

copy_files() {
  mkdir -p "$INSTALL_DIR"
  if [[ "$(realpath "$SRC_DIR")" != "$(realpath "$INSTALL_DIR")" ]]; then
    rm -rf "$INSTALL_DIR/bot" "$INSTALL_DIR/deploy"
    tar -C "$SRC_DIR" --exclude='__pycache__' --exclude='*.pyc' \
        -cf - bot deploy requirements.txt .env.example install.sh \
      | tar -C "$INSTALL_DIR" -xf -
    ok "Code copied to $INSTALL_DIR"
  else
    ok "Running from $INSTALL_DIR already"
  fi

  if [[ ! -f "$ENV_FILE" ]]; then ( umask 077; cp "$INSTALL_DIR/.env.example" "$ENV_FILE" ); fi
  local old_tok old_id old_hash
  old_tok="$(env_get BOT_TOKEN)"; old_id="$(env_get API_ID)"; old_hash="$(env_get API_HASH)"
  if [[ ( -n "$old_tok" && "$old_tok" != "$BOT_TOKEN" ) || ( -n "$old_id" && "$old_id" != "$API_ID" ) \
        || ( -n "$old_hash" && "$old_hash" != "$API_HASH" ) ]]; then
    rm -f "$INSTALL_DIR"/leechbot.session*   # a Telegram session belongs to one bot/app; a stale one would ignore new credentials
    ok "Credentials changed: cleared the old Telegram session"
  fi
  [[ -z "$SOURCE_URL" ]] || printf 'LEECHBOT_URL=%s\n' "$SOURCE_URL" > "$INSTALL_DIR/.source"
  set_env API_ID    "$API_ID"
  set_env API_HASH  "$API_HASH"
  set_env BOT_TOKEN "$BOT_TOKEN"
  [[ -z "$OWNER_ID" ]] || set_env OWNER_ID "$OWNER_ID"
  chmod 600 "$ENV_FILE"
  ok "Wrote $ENV_FILE (mode 600)"
}

install_ctl() {
  mkdir -p "$BIN_DIR"
  sed -e "s|__INSTALL_DIR__|$INSTALL_DIR|g" -e "s|__SVC_USER__|$SVC_USER|g" \
      "$INSTALL_DIR/deploy/leechbot.sh" > "$BIN_DIR/leechbot"
  chmod 755 "$BIN_DIR/leechbot"
  ok "Management command installed: type 'leechbot' for the menu"
}

setup_venv() {
  step "Python environment"
  [[ -x "$VENV/bin/python" ]] || run "$PY" -m venv "$VENV"
  run "$VENV/bin/python" -m pip install "${PIP_OPTS[@]}" --upgrade pip wheel
  # tgcrypto compiles C code; install it separately so a build failure isn't fatal.
  local req; req="$(mktemp)"
  grep -v -i -E '^[[:space:]]*(tgcrypto|#|$)' "$INSTALL_DIR/requirements.txt" | sed 's/[[:space:]]*#.*//' > "$req"
  run "$VENV/bin/python" -m pip install "${PIP_OPTS[@]}" -r "$req"
  rm -f "$req"
  if run_try "$VENV/bin/python" -m pip install "${PIP_OPTS[@]}" tgcrypto; then
    ok "Dependencies installed (with TgCrypto for fast uploads)"
  else
    warn "TgCrypto failed to build; uploads will work but slower."
  fi
  run "$VENV/bin/python" -c 'import pyrogram, dotenv, yt_dlp'
  ok "yt-dlp $("$VENV/bin/python" -m yt_dlp --version)"

  # Lazy extractors cut ~17 MB of RAM from every yt-dlp job. Optional: failure = normal mode.
  local rc=0
  run_try "$VENV/bin/python" "$INSTALL_DIR/deploy/update_ytdlp.py" --no-upgrade || rc=$?
  case "$rc" in
    0) ok "yt-dlp lazy extractors enabled (saves ~17 MB RAM per download)" ;;
    *) warn "Couldn't enable yt-dlp lazy extractors; it works, but uses ~17 MB more RAM per download." ;;
  esac
}

install_deno() {
  step "JavaScript runtime for YouTube (Deno)"
  if (( ! DO_DENO )); then info "Skipped (--no-deno). YouTube downloads need Deno, Node, Bun or QuickJS."; return 0; fi
  if command -v deno >/dev/null 2>&1; then ok "Already installed: $(deno --version | head -n1)"; return 0; fi
  local arch tmp
  case "$(uname -m)" in
    x86_64|amd64)  arch=x86_64 ;;
    aarch64|arm64) arch=aarch64 ;;
    *) warn "No prebuilt Deno for $(uname -m). Install a JS runtime manually for YouTube support."; return 0 ;;
  esac
  tmp="$(mktemp -d)"
  if run_try curl -fsSL --retry 3 -o "$tmp/deno.zip" \
       "https://github.com/denoland/deno/releases/latest/download/deno-${arch}-unknown-linux-gnu.zip" \
     && run_try unzip -o -q "$tmp/deno.zip" -d /usr/local/bin \
     && chmod 755 /usr/local/bin/deno && /usr/local/bin/deno --version >>"$LOG" 2>&1; then
    ok "Installed $(/usr/local/bin/deno --version | head -n1)"
  else
    rm -f /usr/local/bin/deno
    warn "Couldn't install Deno (no network, or a non-glibc system). YouTube downloads may fail until a JS runtime is installed."
  fi
  rm -rf "$tmp"
}

setup_swap() {
  step "Swap safety net"
  if (( ! DO_SWAP )); then info "Skipped (--no-swap)"; return 0; fi
  if (( MEM_MB >= 1536 )); then ok "${MEM_MB} MB RAM is plenty; no swap needed"; return 0; fi
  local swaps; swaps="$(swapon --show=NAME --noheadings 2>/dev/null || true)"
  if [[ -n "$(printf '%s\n' "$swaps" | grep -v zram || true)" ]]; then ok "Swap already active"; return 0; fi
  local avail_mb; avail_mb="$(df -Pm / | awk 'NR==2{print $4}')"
  if (( avail_mb < SWAP_MB + 1024 )); then warn "Not enough free disk for a ${SWAP_MB} MB swap file; skipping."; return 0; fi
  local f=/swapfile
  if [[ -e "$f" ]]; then warn "$f exists but isn't active; leaving it alone."; return 0; fi

  local method made=0
  for method in fallocate dd; do
    if [[ $method == fallocate ]]; then
      run_try fallocate -l "${SWAP_MB}M" "$f" || { rm -f "$f"; continue; }
    else
      run_try dd if=/dev/zero of="$f" bs=1M count="$SWAP_MB" status=none || { rm -f "$f"; continue; }
    fi
    chmod 600 "$f"
    if run_try mkswap "$f" && run_try swapon "$f"; then made=1; break; fi
    rm -f "$f"
  done

  if (( made )); then
    grep -qs "^$f " /etc/fstab || echo "$f none swap sw 0 0" >> /etc/fstab
    echo 'vm.swappiness=10' > /etc/sysctl.d/99-leechbot.conf
    run_try sysctl -q -p /etc/sysctl.d/99-leechbot.conf || true
    ok "Created and enabled a ${SWAP_MB} MB swap file (persisted in /etc/fstab)"
  else
    warn "Couldn't enable swap (many containers/VPS plans forbid it). Continuing without."
  fi
}

setup_zram() {
  step "Compressed swap in RAM (zram)"
  if (( ! DO_ZRAM )); then info "Skipped (--no-zram)"; return 0; fi
  if (( MEM_MB >= 3072 )); then ok "${MEM_MB} MB RAM is plenty; zram not needed"; return 0; fi
  local swaps; swaps="$(swapon --show=NAME --noheadings 2>/dev/null || true)"
  if [[ "$swaps" == *zram* ]]; then ok "zram swap already active"; return 0; fi
  if [[ "$PM" != apt ]]; then info "Automatic zram setup is for Debian/Ubuntu only; skipping."; return 0; fi
  if ! run_try modprobe zram; then
    warn "This kernel has no zram module (typical for containers/OpenVZ); skipping. Disk swap still protects you."
    return 0
  fi
  if ! run_try "${APT[@]}" install -y -qq --no-install-recommends zram-tools; then warn "Couldn't install zram-tools; skipping."; return 0; fi

  local algo=lzo a
  for a in zstd lz4; do
    if grep -qw "$a" /sys/block/zram0/comp_algorithm 2>/dev/null; then algo=$a; break; fi
  done
  printf 'ALGO=%s\nPERCENT=50\nPRIORITY=100\n' "$algo" > /etc/default/zramswap
  printf 'vm.swappiness=100\nvm.page-cluster=0\n' > /etc/sysctl.d/99-leechbot.conf   # swapping into RAM is cheap
  run_try sysctl -q -p /etc/sysctl.d/99-leechbot.conf || true
  if have_systemd; then
    run_try systemctl enable zramswap.service || true
    run_try systemctl restart zramswap.service || true
    swaps="$(swapon --show=NAME --noheadings 2>/dev/null || true)"
    if [[ "$swaps" == *zram* ]]; then
      ok "zram swap active (${algo}, up to 50% of RAM; typically 2-3x compression)"
    else
      warn "zram-tools installed but the swap didn't come up; check: systemctl status zramswap"
    fi
  else
    info "zram-tools configured (${algo}); it activates on the next boot (no systemd here)."
  fi
}

setup_earlyoom() {
  step "Out-of-memory protection (earlyoom)"
  if (( ! DO_EARLYOOM )); then info "Skipped (--no-earlyoom)"; return 0; fi
  if ! pkg_install earlyoom; then
    warn "Couldn't install earlyoom (not in your repos?). The kernel OOM killer still works, just later."
    return 0
  fi
  if have_systemd; then
    run_try systemctl enable --now earlyoom.service || warn "earlyoom installed but not started."
  fi
  ok "earlyoom installed: when RAM and swap run low it kills the biggest low-priority process"
  info "(the bot's yt-dlp/ffmpeg children are marked as first to be killed; the bot itself is protected)"
}

detect_owner_id() {
  step "Linking your Telegram account"
  command -v python3 >/dev/null 2>&1 || die "python3 is required to detect your Telegram ID (or pass --owner-id)."
  local code out rc=0 oid oname ouser
  code="leech-$(python3 -c 'import secrets; print(secrets.token_hex(3).upper())')"
  # The one-time code means only the person looking at this terminal can claim ownership.
  out="$(TG_API="$TG_API" BOT_TOKEN="$BOT_TOKEN" CODE="$code" python3 - <<'PY'
import json, os, sys, time, urllib.error, urllib.parse, urllib.request

api, token, code = os.environ["TG_API"].rstrip("/"), os.environ["BOT_TOKEN"], os.environ["CODE"]
limit = float(os.environ.get("LEECHBOT_DETECT_TIMEOUT", "600"))
deadline = time.time() + limit


def call(method, **params):
    req = urllib.request.Request(f"{api}/bot{token}/{method}", data=urllib.parse.urlencode(params).encode())
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            body = json.load(e)
        except Exception:
            body = {}
        body["ok"] = False
        body["_status"] = e.code
        return body
    except (urllib.error.URLError, OSError) as e:
        return {"ok": False, "description": f"network error: {e}", "_status": 0}


me = call("getMe")
if not me.get("ok"):
    if me.get("_status") == 0:
        print(f"Couldn't reach Telegram ({me.get('description')}). Check the server's internet access.", file=sys.stderr)
    else:
        print(f"Telegram rejected the bot token: {me.get('description', 'unknown error')}", file=sys.stderr)
    sys.exit(3)
username = me["result"].get("username", "YOUR_BOT")
print(f"\n    Open this link and tap START:\n\n        https://t.me/{username}?start={code}\n\n"
      f"    (or send your bot the message:  /start {code})\n"
      f"    Waiting up to {int(limit // 60) or 1} minutes...", file=sys.stderr, flush=True)

offset, cleared_webhook = 0, False
while time.time() < deadline:
    res = call("getUpdates", offset=offset, timeout=20, allowed_updates=json.dumps(["message"]))
    if not res.get("ok"):
        if res.get("_status") == 409 and not cleared_webhook:  # a webhook blocks getUpdates
            call("deleteWebhook")
            cleared_webhook = True
            continue
        if res.get("_status") == 0:  # network blip: keep waiting
            time.sleep(3)
            continue
        print(f"Telegram error: {res.get('description', 'unknown')}", file=sys.stderr)
        sys.exit(3)
    for upd in res["result"]:
        offset = upd["update_id"] + 1
        msg = upd.get("message") or {}
        if msg.get("chat", {}).get("type") != "private":
            continue
        if code.lower() not in (msg.get("text") or "").lower():
            continue
        user = msg.get("from") or {}
        call("getUpdates", offset=offset, timeout=0)  # mark everything up to here as read
        call("sendMessage", chat_id=msg["chat"]["id"], text="✅ Linked! You are the owner of this bot. Finishing the installation…")
        print(f"{user['id']}|{user.get('first_name', '').replace('|', ' ')}|{user.get('username', '')}")
        sys.exit(0)
sys.exit(2)
PY
)" || rc=$?
  case "$rc" in
    0) ;;
    2) die "Didn't hear from you in time. Re-run the installer, or pass --owner-id YOUR_ID (get it from @userinfobot)." ;;
    *) die "Couldn't link your Telegram account (details above). You can also pass --owner-id YOUR_ID." ;;
  esac
  IFS='|' read -r oid oname ouser <<<"$out"
  [[ "$oid" =~ $RE_ID ]] || die "Got an unexpected answer while detecting your ID."
  OWNER_ID="$oid"
  set_env OWNER_ID "$OWNER_ID"
  ok "Linked to ${oname:-your account}${ouser:+ (@$ouser)}, user ID $OWNER_ID"
}

write_units() {
  step "systemd service"
  mkdir -p "$UNIT_DIR"
  # Cap memory at 70% of RAM (max 600M) so a runaway job can't take the server down.
  local max=600 high
  (( MEM_MB * 70 / 100 < max )) && max=$(( MEM_MB * 70 / 100 ))
  high=$(( max * 75 / 100 ))

  sed -e "s|^User=.*|User=$SVC_USER|" \
      -e "s|/opt/leechbot|$INSTALL_DIR|g" \
      -e "s|^MemoryHigh=.*|MemoryHigh=${high}M|" \
      -e "s|^MemoryMax=.*|MemoryMax=${max}M|" \
      "$INSTALL_DIR/deploy/leechbot.service" > "$UNIT_DIR/leechbot.service"

  cat > "$UNIT_DIR/leechbot-ytdlp-update.service" <<EOF
[Unit]
Description=Update yt-dlp for the Telegram leech bot
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
User=$SVC_USER
ExecStart=$VENV/bin/python $INSTALL_DIR/deploy/update_ytdlp.py
# exit 3 = upgraded fine, but lazy extractors unavailable (normal mode); not a failure
SuccessExitStatus=3
EOF

  cat > "$UNIT_DIR/leechbot-ytdlp-update.timer" <<EOF
[Unit]
Description=Weekly yt-dlp update (YouTube breaks old versions often)

[Timer]
OnCalendar=weekly
RandomizedDelaySec=1h
Persistent=true

[Install]
WantedBy=timers.target
EOF
  ok "Unit files written to $UNIT_DIR (MemoryMax=${max}M, MemoryHigh=${high}M)"
}

start_service() {
  if ! have_systemd; then
    warn "systemd isn't running here, so nothing was enabled or started."
    info "Start it manually with:"
    info "  cd $INSTALL_DIR && sudo -u $SVC_USER $VENV/bin/python -m bot.main"
    return 0
  fi
  run systemctl daemon-reload
  run systemctl enable leechbot.service leechbot-ytdlp-update.timer
  run systemctl start leechbot-ytdlp-update.timer

  local since started=0
  since="$(date '+%Y-%m-%d %H:%M:%S')"
  run systemctl restart leechbot.service
  info "Waiting for the bot to connect to Telegram..."
  for _ in $(seq 1 40); do
    if journalctl -u leechbot --since "$since" --no-pager 2>/dev/null | grep -q "Bot started"; then
      started=1; break
    fi
    systemctl is-failed --quiet leechbot.service && break
    sleep 1
  done
  if (( started )); then
    ok "Bot is running"
  else
    journalctl -u leechbot -n 30 --no-pager >&2 || true
    die "The bot didn't start. Logs are above (also: journalctl -u leechbot -e). Most often it's a wrong API ID/hash or token."
  fi
}

summary() {
  printf '\n%s%s Installed!%s\n\n' "$C_G" "✔" "$C_0"
  [[ -n "$BOT_USERNAME" ]] && info "Open Telegram and send /start to @$BOT_USERNAME (only user $OWNER_ID is allowed)."
  cat <<EOF
    Use it      : just send the bot a link (direct file or video site), no commands needed
    Manage      : leechbot            (menu: start/stop/restart, logs, settings, update, uninstall)
                  leechbot status | log | update | settings
    Install dir : $INSTALL_DIR
    Config      : $ENV_FILE
    yt-dlp      : auto-updates weekly (leechbot-ytdlp-update.timer)
    YouTube bot check? Send your cookies.txt to the bot with the caption /cookies (see README)
EOF
}

# ---------- main ---------------------------------------------------------------
main() {
  setup_tty
  printf '%sTelegram leech bot installer%s\n' "$C_B" "$C_0"
  detect_pm
  if (( UNINSTALL )); then do_uninstall; fi
  bootstrap               # no-op when the bot's files are already next to this script

  collect_config          # fail fast on missing/bad input before any heavy work
  preflight
  install_packages
  if (( TOKEN_DEFERRED )); then check_token; fi
  create_user
  copy_files
  install_ctl
  setup_venv
  install_deno
  setup_swap
  setup_zram
  setup_earlyoom
  if (( DETECT_OWNER )); then detect_owner_id; fi
  chown -R "$SVC_USER":"$SVC_USER" "$INSTALL_DIR"
  if (( DO_SYSTEMD )); then
    write_units
    start_service
  else
    info "Skipping systemd (--no-systemd)."
  fi
  summary
  rm -f "$LOG"
}

if [[ -z "${BASH_SOURCE[0]:-}" || "${BASH_SOURCE[0]}" == "$0" ]]; then main; exit $?; fi
