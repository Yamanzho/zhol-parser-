#!/bin/bash
# macOS login service for the existing Zhol Selenium collector.
# Launchd runs `run`. The other commands are the local management interface.
# Database passwords and the 2GIS authorization scope are read from the running
# Docker services. They are not stored in the LaunchAgent plist or in Git.
set -euo pipefail

LABEL="kz.duna.zhol.collector"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PLIST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
SUPPORT="${HOME}/Library/Application Support/Zhol"
ENV_FILE="${SUPPORT}/collector.env"
LOG_DIR="${HOME}/Library/Logs/Zhol"
OUT_LOG="${LOG_DIR}/collector.log"
ERR_LOG="${LOG_DIR}/collector.err.log"
LOCK_DIR="${SUPPORT}/collector.lock.d"
CRASH_FILE="${SUPPORT}/crash-stamps"
PROXY_NAME="zhol-db-host-proxy"
PROXY_PORT="18032"
PROFILE_DEFAULT="${HOME}/Library/Application Support/Almaty2GISCollector/ChromeProfile"
PERSONAL_PROFILE="${HOME}/Library/Application Support/Google/Chrome"
CHROME_DEFAULT="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
DOMAIN="gui/$(id -u)"
TARGET="${DOMAIN}/${LABEL}"
DOCKER="${ZHOL_DOCKER_BIN:-}"

usage() {
  cat <<EOF
Usage: scripts/zhol-collector-macos.sh <command>

  install   Install the LaunchAgent and start it in this graphical session
  status    Show whether the collector is loaded, its process id, and the log paths
  restart   Stop the current process and start it again
  stop      Unload the collector until the next login or an explicit start
  start     Load the collector in this graphical session
  errors    Show recent collector errors
  run       Internal entry used by launchd
EOF
}

log_line() {
  printf '%s %s\n' "$(date '+%Y-%m-%dT%H:%M:%S%z')" "$*"
}

log_info() {
  log_line "$*"
}

log_error() {
  log_line "ERROR $*" >&2
  log_line "ERROR $*"
}

prepare_dirs() {
  mkdir -p "$SUPPORT" "$LOG_DIR" "${HOME}/Library/LaunchAgents"
  chmod 700 "$SUPPORT" "$LOG_DIR"
}

find_docker() {
  if [[ -n "$DOCKER" && -x "$DOCKER" ]]; then
    return 0
  fi
  local candidate
  for candidate in /opt/homebrew/bin/docker /usr/local/bin/docker "${HOME}/.docker/bin/docker"; do
    if [[ -x "$candidate" ]]; then
      DOCKER="$candidate"
      return 0
    fi
  done
  return 1
}

compose() {
  "$DOCKER" compose -f "${ROOT}/docker-compose.yml" -f "${ROOT}/docker-compose.offline.yml" "$@"
}

load_env_file() {
  [[ -f "$ENV_FILE" ]] || return 0
  local line key value
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ "$line" =~ ^[[:space:]]*# ]] && continue
    [[ "$line" =~ ^[[:space:]]*$ ]] && continue
    [[ "$line" == *"="* ]] || continue
    key="${line%%=*}"
    value="${line#*=}"
    case "$key" in
      DATABASE_URL|POSTGRES_PASSWORD|ZHOL_2GIS_BROWSER_GRANT|ZHOL_YANDEX_BROWSER_GRANT|ZHOL_GOOGLE_BROWSER_GRANT|ZHOL_BROWSER_BASE_URL|ZHOL_BROWSER_HTML_ROOT|ZHOL_CHROME_CONTAINER)
        log_error "ignored ${key} in ${ENV_FILE}; the service takes the database and authorization scope from Docker"
        ;;
      ZHOL_CHROME_HEADLESS|ZHOL_CHROME_PROFILE|ZHOL_CHROME_BINARY|ZHOL_BROWSER_ENGINE|ZHOL_DB_PROXY_PORT|ZHOL_DOCKER_BIN)
        if [[ -z "${!key:-}" ]]; then
          printf -v "$key" '%s' "$value"
          export "$key"
        fi
        ;;
    esac
  done < "$ENV_FILE"
  if [[ -n "${ZHOL_DB_PROXY_PORT:-}" ]]; then
    PROXY_PORT="$ZHOL_DB_PROXY_PORT"
  fi
  if [[ -n "${ZHOL_DOCKER_BIN:-}" ]]; then
    DOCKER="$ZHOL_DOCKER_BIN"
  fi
}

write_env_file() {
  umask 077
  cat > "$ENV_FILE" <<EOF
# Non-secret collector settings. The database password and the 2GIS
# authorization scope are read from Docker when the service starts.
ZHOL_CHROME_HEADLESS=0
ZHOL_CHROME_PROFILE=${PROFILE_DEFAULT}
ZHOL_CHROME_BINARY=${CHROME_DEFAULT}
ZHOL_BROWSER_ENGINE=1
ZHOL_DB_PROXY_PORT=${PROXY_PORT}
ZHOL_DOCKER_BIN=${DOCKER}
EOF
  chmod 600 "$ENV_FILE"
}

write_plist() {
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>${ROOT}/scripts/zhol-collector-macos.sh</string>
    <string>run</string>
  </array>
  <key>WorkingDirectory</key>
  <string>${ROOT}</string>
  <key>RunAtLoad</key>
  <true/>
  <key>LimitLoadToSessionType</key>
  <string>Aqua</string>
  <key>ProcessType</key>
  <string>Interactive</string>
  <key>KeepAlive</key>
  <dict>
    <key>SuccessfulExit</key>
    <false/>
  </dict>
  <key>ThrottleInterval</key>
  <integer>15</integer>
  <key>ExitTimeOut</key>
  <integer>30</integer>
  <key>StandardOutPath</key>
  <string>${OUT_LOG}</string>
  <key>StandardErrorPath</key>
  <string>${ERR_LOG}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>ZHOL_CHROME_HEADLESS</key>
    <string>0</string>
  </dict>
</dict>
</plist>
EOF
  chmod 644 "$PLIST"
}

service_loaded() {
  launchctl print "$TARGET" >/dev/null 2>&1
}

cmd_install() {
  prepare_dirs
  find_docker || {
    printf '%s\n' "Docker was not found. Install Docker and run install again." >&2
    exit 1
  }
  if [[ "${ZHOL_INSTALL_SKIP_LAUNCHD:-}" != "1" ]] && service_loaded && [[ -f "$PLIST" ]] && grep -F "${ROOT}/scripts/zhol-collector-macos.sh" "$PLIST" >/dev/null 2>&1; then
    printf '%s\n' "Collector already installed for this directory. It was not restarted."
    return 0
  fi
  write_env_file
  write_plist
  if [[ "${ZHOL_INSTALL_SKIP_LAUNCHD:-}" == "1" ]]; then
    printf '%s\n' "LaunchAgent file written without launchctl: ${PLIST}"
    return 0
  fi
  if service_loaded; then
    launchctl bootout "$TARGET" >/dev/null 2>&1 || true
  fi
  launchctl bootstrap "$DOMAIN" "$PLIST"
  launchctl enable "$TARGET" >/dev/null 2>&1 || true
  printf '%s\n' "Installed ${PLIST}"
  printf '%s\n' "Logs: ${OUT_LOG}"
}

cmd_start() {
  prepare_dirs
  if [[ ! -f "$PLIST" ]]; then
    cmd_install
    return 0
  fi
  if service_loaded; then
    launchctl kickstart "$TARGET" >/dev/null 2>&1 || true
  else
    launchctl bootstrap "$DOMAIN" "$PLIST"
  fi
}

cmd_stop() {
  if service_loaded; then
    launchctl bootout "$TARGET"
    printf '%s\n' "Collector unloaded. The next macOS login loads it again."
  else
    printf '%s\n' "Collector is not loaded."
  fi
}

cmd_restart() {
  prepare_dirs
  if [[ ! -f "$PLIST" ]]; then
    cmd_install
    return 0
  fi
  rm -f "$CRASH_FILE"
  if service_loaded; then
    launchctl kickstart -k "$TARGET"
  else
    launchctl bootstrap "$DOMAIN" "$PLIST"
  fi
  printf '%s\n' "Collector restart requested."
}

cmd_errors() {
  if [[ ! -f "$ERR_LOG" && ! -f "$OUT_LOG" ]]; then
    printf '%s\n' "No collector logs yet: ${LOG_DIR}"
    return 0
  fi
  if [[ -f "$ERR_LOG" ]]; then
    printf '%s\n' "--- ${ERR_LOG} ---"
    tail -n 80 "$ERR_LOG"
  fi
  if [[ -f "$OUT_LOG" ]]; then
    printf '%s\n' "--- recent ERROR lines in ${OUT_LOG} ---"
    grep -E 'ERROR|Traceback|Error' "$OUT_LOG" | tail -n 40 || true
  fi
}

cmd_status() {
  local pid worker docker_collector
  printf '%s\n' "plist: ${PLIST}"
  printf '%s\n' "logs: ${OUT_LOG}"
  printf '%s\n' "errors: ${ERR_LOG}"
  if service_loaded; then
    pid="$(launchctl print "$TARGET" | awk -F' = ' '/^[[:space:]]*pid = / { print $2; exit }')"
    printf '%s\n' "launchd: loaded"
    printf '%s\n' "pid: ${pid:-none}"
  else
    printf '%s\n' "launchd: not loaded"
    printf '%s\n' "pid: none"
  fi
  worker="$(pgrep -f 'python -m zhol.worker --queue collector' || true)"
  if [[ -n "$worker" ]]; then
    printf '%s\n' "worker pid: ${worker}"
  else
    printf '%s\n' "worker pid: none"
  fi
  if find_docker && "$DOCKER" info >/dev/null 2>&1; then
    docker_collector="$(compose --profile collector ps --status running --services 2>/dev/null | grep -x collector || true)"
    if [[ -n "$docker_collector" ]]; then
      printf '%s\n' "docker collector: running"
    else
      printf '%s\n' "docker collector: not running"
    fi
  else
    printf '%s\n' "docker collector: docker is not reachable"
  fi
}

container_env() {
  local cid="$1"
  local key="$2"
  "$DOCKER" inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$cid" 2>/dev/null \
    | sed -n "s/^${key}=//p" \
    | head -n 1 \
    | tr -d '\r' || true
}

scope_name() {
  case "$1" in
    written-browser-retention-local) printf '%s\n' local ;;
    written-browser-retention-commercial) printf '%s\n' commercial ;;
    *) printf '%s\n' unset ;;
  esac
}

profile_is_personal() {
  local profile="$1"
  local personal="$PERSONAL_PROFILE"
  [[ -d "$profile" ]] || return 1
  [[ -d "$personal" ]] || return 1
  [[ "$(cd "$profile" && pwd -P)" == "$(cd "$personal" && pwd -P)" ]]
}

acquire_lock() {
  if mkdir "$LOCK_DIR" 2>/dev/null; then
    printf '%s\n' "$$" > "${LOCK_DIR}/pid"
    return 0
  fi
  local old
  old="$(cat "${LOCK_DIR}/pid" 2>/dev/null || true)"
  if [[ -n "$old" ]] && kill -0 "$old" 2>/dev/null; then
    log_error "another collector startup is already running pid=${old}"
    return 1
  fi
  rm -rf "$LOCK_DIR"
  mkdir "$LOCK_DIR"
  printf '%s\n' "$$" > "${LOCK_DIR}/pid"
}

release_lock() {
  rm -rf "$LOCK_DIR"
}

other_worker_pids() {
  local pid state command
  for pid in $(pgrep -f 'python -m zhol.worker --queue collector' || true); do
    [[ "$pid" -eq "$$" ]] && continue
    state="$(ps -o state= -p "$pid" 2>/dev/null | tr -d ' ' || true)"
    [[ -z "$state" || "$state" == Z* ]] && continue
    command="$(ps -o command= -p "$pid" 2>/dev/null || true)"
    [[ "$command" == *'zhol.worker --queue collector'* ]] || continue
    printf '%s\n' "$pid"
  done
}

docker_collector_running() {
  compose --profile collector ps --status running --services 2>/dev/null | grep -qx collector || return 1
}

ensure_proxy() {
  local db_cid network
  db_cid="$(compose ps -q db 2>/dev/null || true)"
  [[ -n "$db_cid" ]] || return 1
  network="$("$DOCKER" inspect -f '{{range $name, $net := .NetworkSettings.Networks}}{{$name}}{{end}}' "$db_cid" 2>/dev/null || true)"
  [[ -n "$network" ]] || return 1
  if "$DOCKER" ps --filter "name=^/${PROXY_NAME}$" --filter status=running --format '{{.Names}}' | grep -qx "$PROXY_NAME"; then
    return 0
  fi
  if "$DOCKER" ps -aq --filter "name=^/${PROXY_NAME}$" | grep -q .; then
    "$DOCKER" start "$PROXY_NAME" >/dev/null || return 1
    return 0
  fi
  "$DOCKER" run -d --name "$PROXY_NAME" --restart unless-stopped \
    --network "$network" \
    -p "127.0.0.1:${PROXY_PORT}:5432" \
    alpine/socat \
    TCP-LISTEN:5432,fork,reuseaddr TCP:db:5432 >/dev/null || return 1
}

database_ready() {
  local db_cid api_cid password db_user db_name grant google_grant yandex_grant engine encoded
  db_cid="$(compose ps -q db 2>/dev/null || true)"
  api_cid="$(compose ps -q api 2>/dev/null || true)"
  [[ -n "$db_cid" && -n "$api_cid" ]] || return 1
  password="$(container_env "$db_cid" POSTGRES_PASSWORD || true)"
  db_user="$(container_env "$db_cid" POSTGRES_USER || true)"
  db_name="$(container_env "$db_cid" POSTGRES_DB || true)"
  grant="$(container_env "$api_cid" ZHOL_2GIS_BROWSER_GRANT || true)"
  google_grant="$(container_env "$api_cid" ZHOL_GOOGLE_BROWSER_GRANT || true)"
  yandex_grant="$(container_env "$api_cid" ZHOL_YANDEX_BROWSER_GRANT || true)"
  engine="$(container_env "$api_cid" ZHOL_BROWSER_ENGINE || true)"
  [[ -n "$password" && -n "$db_user" && -n "$db_name" ]] || return 1
  case "$grant" in
    written-browser-retention-local|written-browser-retention-commercial) ;;
    *)
      log_error "API authorization scope is not one of the two written 2GIS scopes"
      return 1
      ;;
  esac
  [[ -n "$engine" ]] || engine="1"
  ensure_proxy || return 1
  encoded="$(
    ZHOL_DB_PASSWORD="$password" ZHOL_DB_USER="$db_user" ZHOL_DB_NAME="$db_name" ZHOL_DB_PORT="$PROXY_PORT" "${ROOT}/.venv/bin/python" - <<'PY'
import os
import urllib.parse
print(
    "postgresql+psycopg://%s:%s@127.0.0.1:%s/%s"
    % (
        urllib.parse.quote(os.environ["ZHOL_DB_USER"], safe=""),
        urllib.parse.quote(os.environ["ZHOL_DB_PASSWORD"], safe=""),
        os.environ["ZHOL_DB_PORT"],
        urllib.parse.quote(os.environ["ZHOL_DB_NAME"], safe=""),
    )
)
PY
  )" || true
  unset password
  [[ -n "${encoded:-}" ]] || return 1
  if ! DATABASE_URL="$encoded" "${ROOT}/.venv/bin/python" - <<'PY'
import os
import sys
from sqlalchemy import create_engine, text
try:
    engine = create_engine(os.environ["DATABASE_URL"], future=True)
    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))
    engine.dispose()
except Exception as exc:
    print(type(exc).__name__, file=sys.stderr)
    sys.exit(1)
PY
  then
    unset encoded
    return 1
  fi
  export DATABASE_URL="$encoded"
  export ZHOL_2GIS_BROWSER_GRANT="$grant"
  export ZHOL_BROWSER_ENGINE="$engine"
  unset ZHOL_GOOGLE_BROWSER_GRANT
  unset ZHOL_YANDEX_BROWSER_GRANT
  case "$google_grant" in
    written-browser-retention-local|written-browser-retention-commercial)
      export ZHOL_GOOGLE_BROWSER_GRANT="$google_grant"
      log_info "google authorization scope preserved: $(scope_name "$google_grant")"
      ;;
    "")
      log_info "google authorization scope preserved: unset"
      ;;
    *)
      log_error "google authorization scope is not one of the two written scopes; Google stays closed"
      ;;
  esac
  case "$yandex_grant" in
    written-browser-retention-local|written-browser-retention-commercial)
      export ZHOL_YANDEX_BROWSER_GRANT="$yandex_grant"
      log_info "yandex authorization scope preserved: $(scope_name "$yandex_grant")"
      ;;
    "")
      log_info "yandex authorization scope preserved: unset"
      ;;
    *)
      log_error "yandex authorization scope is not one of the two written scopes; Yandex stays closed"
      ;;
  esac
  ready_name="$db_name"
  unset encoded password db_user db_name grant google_grant yandex_grant
  log_info "database connection ready host=127.0.0.1 port=${PROXY_PORT} database=${ready_name}"
  log_info "authorization scope preserved: $(scope_name "$ZHOL_2GIS_BROWSER_GRANT")"
}

wait_for_dependencies() {
  local attempt
  for attempt in $(seq 1 30); do
    if find_docker && "$DOCKER" info >/dev/null 2>&1 && database_ready; then
      return 0
    fi
    if (( attempt == 1 || attempt % 5 == 0 )); then
      log_error "waiting for Docker and PostgreSQL (attempt ${attempt} of 30)"
    fi
    sleep 2
  done
  log_error "Docker or PostgreSQL was not ready after 30 attempts. The service will try again."
  return 1
}

crash_limit_reached() {
  local now line count=0 tmp
  now="$(date +%s)"
  tmp="$(mktemp "${SUPPORT}/crash.XXXXXX")"
  if [[ -f "$CRASH_FILE" ]]; then
    while IFS= read -r line; do
      if [[ "$line" =~ ^[0-9]+$ ]] && (( now - line < 600 )); then
        printf '%s\n' "$line" >> "$tmp"
        count=$((count + 1))
      fi
    done < "$CRASH_FILE"
  fi
  printf '%s\n' "$now" >> "$tmp"
  count=$((count + 1))
  mv "$tmp" "$CRASH_FILE"
  if (( count >= 5 )); then
    : > "$CRASH_FILE"
    log_error "bounded restart: ${count} worker failures in 10 minutes. Automatic restart is paused until start or restart."
    return 0
  fi
  return 1
}

clear_crashes() {
  rm -f "$CRASH_FILE"
}

configure_collector_env() {
  export ZHOL_CHROME_HEADLESS=0
  unset ZHOL_CHROME_CONTAINER
  export ZHOL_CHROME_PROFILE="${ZHOL_CHROME_PROFILE:-$PROFILE_DEFAULT}"
  export ZHOL_CHROME_BINARY="${ZHOL_CHROME_BINARY:-$CHROME_DEFAULT}"
  if [[ ! -x "$ZHOL_CHROME_BINARY" ]]; then
    log_error "Google Chrome was not found at ${ZHOL_CHROME_BINARY}"
    return 1
  fi
  if profile_is_personal "$ZHOL_CHROME_PROFILE"; then
    log_error "Chrome profile is the personal profile. The collector will not start."
    return 1
  fi
  mkdir -p "$ZHOL_CHROME_PROFILE"
  if [[ "${ZHOL_COLLECTOR_RUNTIME:-}" == "isolated" ]]; then
    [[ -n "${DATABASE_URL:-}" ]] || {
      log_error "isolated collector requires DATABASE_URL"
      return 1
    }
    [[ -n "${ZHOL_BROWSER_BASE_URL:-}" ]] || {
      log_error "isolated collector requires ZHOL_BROWSER_BASE_URL"
      return 1
    }
    case "$DATABASE_URL" in
      *127.0.0.1:${PROXY_PORT}/zhol|*localhost:${PROXY_PORT}/zhol|*127.0.0.1:18032/zhol)
        log_error "isolated collector refused the application database"
        return 1
        ;;
    esac
    log_info "isolated collector using the fixture origin"
  else
    unset ZHOL_BROWSER_BASE_URL
    unset ZHOL_BROWSER_HTML_ROOT
  fi
}

cmd_run() {
  local worker_pid="" stop_requested=0 status=0
  prepare_dirs
  cd "$ROOT"
  load_env_file
  umask 077
  log_info "collector service starting"
  if ! acquire_lock; then
    exit 75
  fi
  trap 'release_lock' EXIT
  on_term() {
    stop_requested=1
    if [[ -n "$worker_pid" ]]; then
      kill -TERM "$worker_pid" 2>/dev/null || true
    fi
  }
  trap on_term TERM INT

  if [[ "${ZHOL_COLLECTOR_RUNTIME:-}" != "isolated" ]]; then
    if ! wait_for_dependencies; then
      exit 75
    fi
  else
    find_docker || true
  fi
  if find_docker && "$DOCKER" info >/dev/null 2>&1 && docker_collector_running; then
    log_error "Docker collector is already running. This service will not start a second collector."
    exit 75
  fi
  if [[ -n "$(other_worker_pids)" ]]; then
    log_error "a collector worker is already running. This service will not start another."
    exit 75
  fi
  if ! configure_collector_env; then
    exit 75
  fi
  log_info "starting worker queue=collector headless=${ZHOL_CHROME_HEADLESS} profile=${ZHOL_CHROME_PROFILE}"
  "${ROOT}/.venv/bin/python" -m zhol.worker --queue collector &
  worker_pid=$!
  set +e
  wait "$worker_pid"
  status=$?
  if [[ "$stop_requested" -eq 1 ]]; then
    wait "$worker_pid" 2>/dev/null
    status=0
  fi
  set -e
  worker_pid=""
  if [[ "$stop_requested" -eq 1 ]]; then
    log_info "collector stopped"
    exit 0
  fi
  if [[ "$status" -eq 0 ]]; then
    clear_crashes
    log_info "collector exited"
    exit 0
  fi
  log_error "collector worker exited status=${status}"
  if crash_limit_reached; then
    exit 0
  fi
  exit "$status"
}

main() {
  local command="${1:-}"
  case "$command" in
    install) cmd_install ;;
    start) cmd_start ;;
    stop) cmd_stop ;;
    restart) cmd_restart ;;
    status) cmd_status ;;
    errors) cmd_errors ;;
    run) cmd_run ;;
    *) usage; exit 2 ;;
  esac
}

main "$@"
