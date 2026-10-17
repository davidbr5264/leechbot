#!/usr/bin/env bash
# =============================================================================
#  Management menu for the Telegram leech bot (installed as /usr/local/bin/leechbot)
#
#    leechbot            interactive menu
#    leechbot status | start | stop | restart | log | update | settings | ytdlp | cookies | uninstall
# =============================================================================
set -u
INSTALL_DIR="__INSTALL_DIR__"
SVC_USER="__SVC_USER__"
SERVICE="leechbot.service"
ENV_FILE="$INSTALL_DIR/.env"
TG_API="${LEECHBOT_TG_API:-https://api.telegram.org}"
RE_ID='^[0-9]{1,15}$'
RE_HASH='^[0-9a-fA-F]{32}$'
RE_TOKEN='^[0-9]{5,}:[A-Za-z0-9_-]{30,}$'

if [[ -t 1 ]]; then B=$'\e[1m' G=$'\e[32m' Y=$'\e[33m' R=$'\e[31m' D=$'\e[2m' Z=$'\e[0m'; else B="" G="" Y="" R="" D="" Z=""; fi
ok()   { printf '%s✔%s %s\n' "$G" "$Z" "$*"; }
warn() { printf '%s!%s %s\n' "$Y" "$Z" "$*" >&2; }
err()  { printf '%s✘%s %s\n' "$R" "$Z" "$*" >&2; }

if [[ $EUID -ne 0 ]]; then
  command -v sudo >/dev/null 2>&1 || { err "Run as root."; exit 1; }
  exec sudo "$0" "$@"
fi
[[ -d "$INSTALL_DIR" ]] || { err "Not installed (missing $INSTALL_DIR)."; exit 1; }

have_systemd() { [[ -d /run/systemd/system || -n "${LEECHBOT_ASSUME_SYSTEMD:-}" ]]; }
sc() {
  have_systemd || { warn "systemd isn't running on this machine."; return 1; }
  systemctl "$@"
}
is_active() { have_systemd && systemctl is-active --quiet "$SERVICE"; }

# ---------- .env helpers ----------
env_get() { sed -n "s/^$1=//p" "$ENV_FILE" 2>/dev/null | tail -n1; }
set_env() {  # set_env KEY VALUE
  K="$1" V="$2" awk 'BEGIN{FS=OFS="="; k=ENVIRON["K"]; v=ENVIRON["V"]}
       $1==k {print k "=" v; d=1; next} {print}
       END {if (!d) print k "=" v}' "$ENV_FILE" > "$ENV_FILE.tmp" && mv "$ENV_FILE.tmp" "$ENV_FILE"
  chown "$SVC_USER":"$SVC_USER" "$ENV_FILE" 2>/dev/null || true
  chmod 600 "$ENV_FILE"
}
unset_env() {  # unset_env KEY: remove the line
  K="$1" awk 'BEGIN{FS="="; k=ENVIRON["K"]} $1!=k' "$ENV_FILE" > "$ENV_FILE.tmp" && mv "$ENV_FILE.tmp" "$ENV_FILE"
  chown "$SVC_USER":"$SVC_USER" "$ENV_FILE" 2>/dev/null || true
  chmod 600 "$ENV_FILE"
}
mask() { local v="$1"; if (( ${#v} > 8 )); then printf '%s…%s' "${v:0:4}" "${v: -3}"; else printf '(set)'; fi; }
confirm() { local a=""; read -rp "$1 [y/N] " a || return 1; [[ "$a" =~ ^[Yy]$ ]]; }

# Pasted values often carry terminal junk (bracketed-paste markers, CR, spaces, wrapping quotes).
sanitize() {
  local v="$1" esc=$'\e'
  v="${v//${esc}\[200~/}"; v="${v//${esc}\[201~/}"; v="${v//\[200~/}"; v="${v//\[201~/}"; v="${v//$esc/}"
  v="${v//[[:space:]]/}"; v="${v#[\"\']}"; v="${v%[\"\']}"
  printf '%s' "$v"
}

# ---------- actions ----------
wait_started() {
  local since="$1"
  for _ in $(seq 1 25); do
    if journalctl -u leechbot --since "$since" --no-pager 2>/dev/null | grep -q "Bot started"; then ok "Bot is running"; return 0; fi
    systemctl is-failed --quiet "$SERVICE" && break
    sleep 1
  done
  err "The bot didn't report 'started'. Last log lines:"; journalctl -u leechbot -n 15 --no-pager 2>&1 | tail -n 15
  return 1
}
do_start()   { local t; t="$(date '+%Y-%m-%d %H:%M:%S')"; sc start "$SERVICE" && wait_started "$t"; }
do_restart() { local t; t="$(date '+%Y-%m-%d %H:%M:%S')"; sc restart "$SERVICE" && wait_started "$t"; }
do_stop()    { sc stop "$SERVICE" && ok "Stopped"; }

do_status() {
  if ! have_systemd; then warn "systemd isn't running; can't query the service."; return 1; fi
  local -A p=()
  local line
  while IFS='=' read -r k v; do p[$k]="$v"; done < <(systemctl show "$SERVICE" -p ActiveState -p SubState -p NRestarts -p MemoryCurrent -p MemoryMax -p ActiveEnterTimestamp 2>/dev/null)
  local state="${p[ActiveState]:-unknown}/${p[SubState]:-?}" col="$R"
  [[ "${p[ActiveState]:-}" == active ]] && col="$G"
  printf '  Service   : %s%s%s' "$col" "$state" "$Z"; [[ -n "${p[ActiveEnterTimestamp]:-}" ]] && printf '  (since %s)' "${p[ActiveEnterTimestamp]}"; echo
  printf '  Autostart : %s\n' "$(systemctl is-enabled "$SERVICE" 2>/dev/null || echo disabled)"
  printf '  Restarts  : %s\n' "${p[NRestarts]:-0}"
  local cur="${p[MemoryCurrent]:-}" max="${p[MemoryMax]:-}"
  if [[ "$cur" =~ ^[0-9]+$ ]]; then
    line="$((cur / 1048576)) MB"
    [[ "$max" =~ ^[0-9]+$ ]] && line="$line of $((max / 1048576)) MB cap"
    printf '  Memory    : %s\n' "$line"
  fi
  printf '  yt-dlp    : %s\n' "$("$INSTALL_DIR/venv/bin/python" -m yt_dlp --version 2>/dev/null || echo '?')"
  printf '  Install   : %s\n' "$INSTALL_DIR"
}

do_log() {
  trap ':' INT   # Ctrl-C stops journalctl but keeps the menu alive
  journalctl -u leechbot -n 50 -f --no-pager
  trap - INT
}

do_update() {
  echo "Updating the bot (settings are kept)..."
  bash "$INSTALL_DIR/install.sh" --update -y --dir "$INSTALL_DIR" --user "$SVC_USER"
}

do_ytdlp() {
  echo "Updating yt-dlp..."
  runuser -u "$SVC_USER" -- "$INSTALL_DIR/venv/bin/python" "$INSTALL_DIR/deploy/update_ytdlp.py"
  local rc=$?
  if [[ $rc -eq 0 || $rc -eq 3 ]]; then ok "yt-dlp is up to date"; else err "yt-dlp update failed (exit $rc)"; fi
}

do_cookies() {
  local f="$INSTALL_DIR/cookies.txt"
  if [[ -f "$f" ]]; then
    printf 'YouTube cookies: installed, last updated %s\n' "$(date -r "$f" '+%Y-%m-%d %H:%M')"
    confirm "Delete them?" && { rm -f "$f"; ok "Cookies removed."; }
  else
    echo "YouTube cookies: none installed."
  fi
  echo "${D}To add or refresh them, send a cookies.txt file to the bot in Telegram with the caption /cookies.${Z}"
}

do_autostart() {
  case "$1" in
    enable)  sc enable "$SERVICE" >/dev/null 2>&1 && ok "Will start automatically on boot." ;;
    disable) sc disable "$SERVICE" >/dev/null 2>&1 && ok "Won't start automatically on boot." ;;
  esac
}

do_uninstall() {
  local args=(--uninstall --dir "$INSTALL_DIR" --user "$SVC_USER")
  if confirm "Also delete your settings, Telegram session and the service user?"; then args+=(--purge -y); fi
  bash "$INSTALL_DIR/install.sh" "${args[@]}"
}

# ---------- settings ----------
ask_restart() { is_active && confirm "Restart the bot now to apply this?" && do_restart; return 0; }

prompt_value() {  # prompt_value "Label" REGEX [secret]  -> sets REPLY_VAL ("" = keep current)
  local label="$1" re="$2" secret="${3:-0}" v="" rc=0
  while true; do
    rc=0
    if [[ "$secret" == 1 ]]; then
      read -rsp "  $label (Enter = keep current): " v || rc=$?; echo
    else
      read -rp "  $label (Enter = keep current): " v || rc=$?
    fi
    if (( rc != 0 )); then REPLY_VAL=""; return 0; fi   # input closed: treat as "keep current"
    v="$(sanitize "$v")"
    [[ -z "$v" ]] && { REPLY_VAL=""; return 0; }
    [[ "$re" == "$RE_HASH" ]] && v="${v,,}"
    [[ "$v" =~ $re ]] && { REPLY_VAL="$v"; return 0; }
    warn "That doesn't look right (got ${#v} characters). Try again, or press Enter to keep the current value."
  done
}

set_token() {
  prompt_value "New bot token" "$RE_TOKEN" 1; local t="$REPLY_VAL"; [[ -z "$t" ]] && return
  local code body; body="$(mktemp)"
  code="$(printf 'url = "%s/bot%s/getMe"\n' "$TG_API" "$t" | curl -K - -sS --max-time 15 -o "$body" -w '%{http_code}' 2>/dev/null)" || code=000
  case "$code" in
    200) ok "Token is valid (@$(sed -n 's/.*"username" *: *"\([^"]*\)".*/\1/p' "$body"))" ;;
    401|404) rm -f "$body"; err "Telegram rejected that token. Nothing changed."; return ;;
    *) warn "Couldn't reach Telegram to verify the token."; confirm "Save it anyway?" || { rm -f "$body"; return; } ;;
  esac
  rm -f "$body"
  set_env BOT_TOKEN "$t"
  rm -f "$INSTALL_DIR"/leechbot.session*   # a Telegram session belongs to one bot; keeping it would ignore the new token
  ok "Bot token updated (old Telegram session cleared)."; ask_restart
}

set_api() {
  prompt_value "New API ID" "$RE_ID"; local id="$REPLY_VAL"
  prompt_value "New API hash" "$RE_HASH" 1; local hash="$REPLY_VAL"
  [[ -z "$id" && -z "$hash" ]] && return
  [[ -n "$id" ]] && set_env API_ID "$id"
  [[ -n "$hash" ]] && set_env API_HASH "$hash"
  rm -f "$INSTALL_DIR"/leechbot.session*
  ok "API credentials updated (old Telegram session cleared)."; ask_restart
}

set_owner() {
  echo "  Current owner ID: $(env_get OWNER_ID)   (get yours from @userinfobot)"
  prompt_value "New owner user ID" "$RE_ID"; [[ -z "$REPLY_VAL" ]] && return
  set_env OWNER_ID "$REPLY_VAL"; ok "Owner changed to $REPLY_VAL."; ask_restart
}

set_limits() {
  local spec key label re cur
  for spec in "MAX_FILE_SIZE_MB|Max size of one Telegram upload part, MB (bigger files are split)|^([5-9][0-9]|[1-3][0-9]{3}|4000)$" \
              "MAX_DOWNLOAD_MB|Max size of a whole download, MB|^([1-9][0-9]{2,5})$" \
              "MIN_FREE_RAM_MB|Wait for this much free RAM before heavy jobs, MB|^([0-9]|[1-9][0-9]{1,3})$" \
              "MAX_QUEUE|Max jobs waiting in the queue|^([1-9]|[1-9][0-9]|100)$" \
              "YTDL_SORT|yt-dlp format sort, e.g. res:1080 or vcodec:h264 (type 'default' for best quality)|^([A-Za-z0-9:,+~*._-]+)$"; do
    IFS='|' read -r key label re <<<"$spec"
    cur="$(env_get "$key")"
    echo "  $label"
    prompt_value "$key [current: ${cur:-default}]" "$re"
    if [[ "$key" == YTDL_SORT && "$REPLY_VAL" == default ]]; then
      unset_env YTDL_SORT; ok "YTDL_SORT cleared (best quality)"; changed=1
    elif [[ -n "$REPLY_VAL" ]]; then
      set_env "$key" "$REPLY_VAL"; ok "$key = $REPLY_VAL"; changed=1
    fi
  done
  [[ "${changed:-0}" == 1 ]] && ask_restart
  return 0
}

show_settings() {
  echo "  API_ID      : $(env_get API_ID)"
  echo "  API_HASH    : $(mask "$(env_get API_HASH)")"
  echo "  BOT_TOKEN   : $(mask "$(env_get BOT_TOKEN)")"
  echo "  OWNER_ID    : $(env_get OWNER_ID)"
  local k; for k in MAX_FILE_SIZE_MB MAX_DOWNLOAD_MB MIN_FREE_RAM_MB MAX_QUEUE YTDL_SORT DOWNLOAD_DIR; do
    printf '  %-12s: %s\n' "$k" "$(env_get "$k" | sed 's/^$/(default)/')"
  done
}

settings_menu() {
  local c
  while true; do
    echo; echo "${B}Settings${Z}"
    echo "  1. Bot token"
    echo "  2. API ID and hash"
    echo "  3. Owner user ID"
    echo "  4. Limits (upload size, RAM guard, queue)"
    echo "  5. Show current settings"
    echo "  0. Back"
    read -rp "Choose [0-5]: " c || return
    case "$c" in
      1) set_token ;; 2) set_api ;; 3) set_owner ;; 4) changed=0; set_limits ;; 5) show_settings ;;
      0|"") return ;; *) warn "Pick a number from the list." ;;
    esac
  done
}

usage() {
  cat <<EOT
Telegram leech bot management

  leechbot               open the menu
  leechbot status        service state, memory, versions
  leechbot start|stop|restart
  leechbot log           live logs (Ctrl-C to leave)
  leechbot update        update the bot (keeps your settings)
  leechbot ytdlp         update yt-dlp now
  leechbot settings      change token, API credentials, owner, limits
  leechbot cookies       YouTube cookies status
  leechbot uninstall
EOT
}

menu() {
  local c
  while true; do
    echo
    echo "${B}┌──────────────────────────────────────────────┐${Z}"
    echo "${B}│   Telegram leech bot management              │${Z}"
    echo "${B}└──────────────────────────────────────────────┘${Z}"
    if is_active; then echo "  Bot is ${G}running${Z}"; else echo "  Bot is ${R}not running${Z}"; fi
    echo
    echo "   0. Exit"
    echo "   1. Start          2. Stop          3. Restart"
    echo "   4. Status         5. Live logs     6. Settings"
    echo "   7. Update bot     8. Update yt-dlp 9. YouTube cookies"
    echo "  10. Enable autostart   11. Disable autostart"
    echo "  12. Uninstall"
    read -rp "Choose [0-12]: " c || return 0
    echo
    case "$c" in
      1) do_start ;; 2) do_stop ;; 3) do_restart ;; 4) do_status ;; 5) do_log ;;
      6) settings_menu ;; 7) do_update ;; 8) do_ytdlp ;; 9) do_cookies ;;
      10) do_autostart enable ;; 11) do_autostart disable ;;
      12) do_uninstall; return 0 ;;
      0|q|"") return 0 ;;
      *) warn "Pick a number from the list." ;;
    esac
  done
}

main() {
  case "${1:-}" in
    "")        menu ;;
    status)    do_status ;;
    start)     do_start ;;
    stop)      do_stop ;;
    restart)   do_restart ;;
    log|logs)  do_log ;;
    update)    do_update ;;
    ytdlp)     do_ytdlp ;;
    cookies)   do_cookies ;;
    settings)  settings_menu ;;
    uninstall) do_uninstall ;;
    -h|--help|help) usage ;;
    *) err "Unknown command: $1"; usage; return 1 ;;
  esac
}

main "$@"; exit $?
