#!/usr/bin/env bash
# Off-site backup job for the hybrid VPS profile (ADR-0023,
# docs/backup-and-recovery.md → Off-site backup job).
#
# Once a day it takes a custom-format `pg_dump` with the isolated operational
# credential (DATABASE_OPERATOR_URL, the one BYPASSRLS application role), checks
# the archive is readable, and uploads it plus a copy of the application's
# object-storage bucket to an S3-compatible bucket at a different provider. All
# data is encrypted client-side (rclone crypt) before it leaves the host.
#
# Opt-in by configuration: nothing runs unless the destination is configured
# (BACKUP_S3_ENDPOINT, BACKUP_S3_BUCKET, BACKUP_S3_ACCESS_KEY_ID and
# BACKUP_S3_SECRET_ACCESS_KEY). With none of them set the job reports itself
# disabled and idles; with only some set, or without the encryption passphrase
# or a source credential, it refuses to run.
#
# The job never deletes anything off-site: dumps get unique names and the file
# copy never removes destination objects, so the destination credential needs
# no delete permission. Retention is a provider-side lifecycle rule.
#
# Commands:
#   schedule      (default) idle when disabled, otherwise run daily at
#                 BACKUP_HOUR_UTC
#   run-once      run one backup now; exits non-zero on failure
#   check-config  print `enabled` or `disabled`; exits 1 on a misconfiguration
#   list          list the off-site database dumps (decrypted names)
#   fetch-dump NAME|latest PATH
#                 download and decrypt one dump to PATH for pg_restore
#   restore-files copy the off-site file copy back into the configured
#                 STORAGE_BUCKET (only missing or differing objects)
#   health        container healthcheck: fails when enabled and the last
#                 successful run is older than 26 hours
#
# Secrets (database password, keys, passphrase, heartbeat URL) are passed to
# child processes through the environment only, never on a command line, and
# are never logged.

set -euo pipefail

STATE_DIR="${BACKUP_STATE_DIR:-/var/lib/offsite-backup}"
STALE_AFTER_SECONDS=$((26 * 3600))
DESTINATION_VARS=(
  BACKUP_S3_ENDPOINT
  BACKUP_S3_BUCKET
  BACKUP_S3_ACCESS_KEY_ID
  BACKUP_S3_SECRET_ACCESS_KEY
)
SOURCE_STORAGE_VARS=(
  STORAGE_ENDPOINT_URL
  STORAGE_BUCKET
  STORAGE_ACCESS_KEY_ID
  STORAGE_SECRET_ACCESS_KEY
)
# Transient prefixes the application cleans up itself; copying them off-site
# would only retain bytes the application has already decided to discard.
FILE_EXCLUDES=(
  "organisations/*/documents/*/staging/**"
  "organisations/*/ai/scratch/**"
)
# Summary-level logging only: per-object lines would put object keys in logs.
RCLONE_FLAGS=(--log-level NOTICE --stats 0 --stats-log-level NOTICE)

log() {
  printf '%s offsite_backup %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2
}

include_files() {
  case "${BACKUP_INCLUDE_FILES:-true}" in
    true) return 0 ;;
    false) return 1 ;;
    *) return 2 ;;
  esac
}

# Print `enabled` or `disabled`; return 1 (after logging the reason) when the
# configuration is partial or invalid. Never prints a value.
config_state() {
  local name missing=() configured=0
  for name in "${DESTINATION_VARS[@]}"; do
    if [ -n "${!name:-}" ]; then
      configured=$((configured + 1))
    else
      missing+=("$name")
    fi
  done
  if [ "$configured" -eq 0 ]; then
    echo disabled
    return 0
  fi
  if [ "${#missing[@]}" -gt 0 ]; then
    log "event=config_error reason=partial_destination missing=${missing[*]}"
    return 1
  fi

  local files_status=0
  include_files || files_status=$?
  if [ "$files_status" -eq 2 ]; then
    log "event=config_error reason=invalid_value variable=BACKUP_INCLUDE_FILES expected=true|false"
    return 1
  fi

  local required=(BACKUP_ENCRYPTION_PASSWORD DATABASE_OPERATOR_URL)
  if [ "$files_status" -eq 0 ]; then
    required+=("${SOURCE_STORAGE_VARS[@]}")
  fi
  missing=()
  for name in "${required[@]}"; do
    [ -n "${!name:-}" ] || missing+=("$name")
  done
  if [ "${#missing[@]}" -gt 0 ]; then
    log "event=config_error reason=missing_required missing=${missing[*]}"
    return 1
  fi

  local hour="${BACKUP_HOUR_UTC:-2}"
  if ! [[ "$hour" =~ ^[0-9]{1,2}$ ]] || [ "$((10#$hour))" -gt 23 ]; then
    log "event=config_error reason=invalid_value variable=BACKUP_HOUR_UTC expected=0-23"
    return 1
  fi

  if ! export_pg_environment "$DATABASE_OPERATOR_URL"; then
    return 1
  fi
  echo enabled
}

urldecode() {
  local value="${1//+/ }"
  printf '%b' "${value//%/\\x}"
}

# Translate the application's SQLAlchemy URL into libpq environment variables
# so the password never appears in a process argument list. Accepts
# postgresql[+asyncpg]://user[:password]@host[:port]/database[?ssl=|sslmode=].
export_pg_environment() {
  local url="$1"
  local pattern='^postgres(ql)?(\+asyncpg)?://([^:@/]+)(:([^@]*))?@([^/:?@]+)(:([0-9]+))?/([^?]+)(\?(.*))?$'
  if ! [[ "$url" =~ $pattern ]]; then
    log "event=config_error reason=unsupported_url variable=DATABASE_OPERATOR_URL"
    return 1
  fi
  PGUSER="$(urldecode "${BASH_REMATCH[3]}")"
  PGPASSWORD="$(urldecode "${BASH_REMATCH[5]}")"
  PGHOST="${BASH_REMATCH[6]}"
  PGPORT="${BASH_REMATCH[8]:-5432}"
  PGDATABASE="$(urldecode "${BASH_REMATCH[9]}")"
  local query="${BASH_REMATCH[11]}" parameter key value
  local -a parameters=()
  unset PGSSLMODE
  if [ -n "$query" ]; then
    IFS='&' read -r -a parameters <<<"$query"
  fi
  for parameter in "${parameters[@]}"; do
    key="${parameter%%=*}"
    value="${parameter#*=}"
    case "$key" in
      ssl | sslmode) PGSSLMODE="$value" ;;
      *)
        log "event=config_error reason=unsupported_url_parameter variable=DATABASE_OPERATOR_URL parameter=$key"
        return 1
        ;;
    esac
  done
  export PGUSER PGPASSWORD PGHOST PGPORT PGDATABASE
  if [ -n "${PGSSLMODE:-}" ]; then
    export PGSSLMODE
  fi
}

# Configure the rclone remotes through environment variables (no config file
# on disk): `offsite` is the destination bucket, `vault` encrypts everything
# written beneath it, and `source` is the application's bucket.
configure_rclone() {
  export RCLONE_CONFIG=/dev/null
  export RCLONE_CONFIG_OFFSITE_TYPE=s3
  export RCLONE_CONFIG_OFFSITE_PROVIDER=Other
  export RCLONE_CONFIG_OFFSITE_ENDPOINT="$BACKUP_S3_ENDPOINT"
  export RCLONE_CONFIG_OFFSITE_REGION="${BACKUP_S3_REGION:-us-east-1}"
  export RCLONE_CONFIG_OFFSITE_ACCESS_KEY_ID="$BACKUP_S3_ACCESS_KEY_ID"
  export RCLONE_CONFIG_OFFSITE_SECRET_ACCESS_KEY="$BACKUP_S3_SECRET_ACCESS_KEY"
  # The destination bucket is provisioned by the operator; never try to
  # create it, so the credential needs no bucket-level permissions.
  export RCLONE_CONFIG_OFFSITE_NO_CHECK_BUCKET=true

  local prefix="${BACKUP_S3_PREFIX:-}"
  prefix="${prefix#/}"
  prefix="${prefix%/}"
  export RCLONE_CONFIG_VAULT_TYPE=crypt
  export RCLONE_CONFIG_VAULT_REMOTE="offsite:${BACKUP_S3_BUCKET}${prefix:+/$prefix}"
  RCLONE_CONFIG_VAULT_PASSWORD="$(printf '%s' "$BACKUP_ENCRYPTION_PASSWORD" | rclone obscure -)"
  export RCLONE_CONFIG_VAULT_PASSWORD

  if include_files; then
    export RCLONE_CONFIG_SOURCE_TYPE=s3
    export RCLONE_CONFIG_SOURCE_PROVIDER=Other
    export RCLONE_CONFIG_SOURCE_ENDPOINT="$STORAGE_ENDPOINT_URL"
    export RCLONE_CONFIG_SOURCE_REGION="${STORAGE_REGION:-us-east-1}"
    export RCLONE_CONFIG_SOURCE_ACCESS_KEY_ID="$STORAGE_ACCESS_KEY_ID"
    export RCLONE_CONFIG_SOURCE_SECRET_ACCESS_KEY="$STORAGE_SECRET_ACCESS_KEY"
    export RCLONE_CONFIG_SOURCE_NO_CHECK_BUCKET=true
  fi
}

send_heartbeat() {
  [ -n "${BACKUP_HEARTBEAT_URL:-}" ] || return 0
  # The URL usually carries a token, so it is never logged.
  if ! wget -q -O /dev/null -T 15 "$BACKUP_HEARTBEAT_URL"; then
    log "event=heartbeat_failed"
  fi
}

run_once() {
  local state
  state="$(config_state)"
  if [ "$state" != enabled ]; then
    log "event=skipped reason=disabled"
    return 0
  fi
  # config_state validated the URL in a subshell; export it here for pg_dump.
  export_pg_environment "$DATABASE_OPERATOR_URL"
  configure_rclone

  local stamp workdir started
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  started="$(date -u +%s)"
  mkdir -p "$STATE_DIR"
  workdir="$(mktemp -d "$STATE_DIR/run.XXXXXX")"
  # shellcheck disable=SC2064 # expand now: the path is fixed for this run
  trap "rm -rf '$workdir'" EXIT
  log "event=started run=$stamp"

  pg_dump --format=custom --no-password --file="$workdir/database.dump"
  # An unreadable archive must never be uploaded as if it were a backup.
  pg_restore --list "$workdir/database.dump" >/dev/null
  rclone copyto "$workdir/database.dump" "vault:db/${stamp}.dump" "${RCLONE_FLAGS[@]}"
  log "event=database_uploaded run=$stamp bytes=$(stat -c %s "$workdir/database.dump")"

  if include_files; then
    local exclude excludes=()
    for exclude in "${FILE_EXCLUDES[@]}"; do
      excludes+=(--exclude "$exclude")
    done
    rclone copy "source:${STORAGE_BUCKET}" vault:files "${excludes[@]}" "${RCLONE_FLAGS[@]}"
    log "event=files_copied run=$stamp"
  fi

  date -u +%s >"$STATE_DIR/last-success"
  log "event=succeeded run=$stamp seconds=$(($(date -u +%s) - started))"
  send_heartbeat
}

# Shared set-up for the operator restore commands, which need the same
# destination and encryption configuration as a backup run.
require_enabled() {
  local state
  state="$(config_state)"
  if [ "$state" != enabled ]; then
    log "event=config_error reason=disabled"
    exit 1
  fi
  configure_rclone
}

list_dumps() {
  require_enabled
  rclone lsf vault:db --files-only "${RCLONE_FLAGS[@]}" | sort
}

fetch_dump() {
  local name="${1:-}" target="${2:-}"
  if [ -z "$name" ] || [ -z "$target" ]; then
    echo "usage: offsite-backup fetch-dump NAME|latest PATH" >&2
    exit 2
  fi
  require_enabled
  if [ "$name" = latest ]; then
    name="$(rclone lsf vault:db --files-only "${RCLONE_FLAGS[@]}" | sort | tail -n 1)"
    if [ -z "$name" ]; then
      log "event=fetch_failed reason=no_dumps"
      exit 1
    fi
  fi
  rclone copyto "vault:db/${name}" "$target" "${RCLONE_FLAGS[@]}"
  pg_restore --list "$target" >/dev/null
  log "event=dump_fetched name=$name"
}

restore_files() {
  if ! include_files; then
    log "event=config_error reason=files_not_included"
    exit 1
  fi
  require_enabled
  rclone copy vault:files "source:${STORAGE_BUCKET}" "${RCLONE_FLAGS[@]}"
  log "event=files_restored"
}

seconds_until_next_run() {
  local hour now target
  hour="$((10#${BACKUP_HOUR_UTC:-2}))"
  now="$(date -u +%s)"
  target=$((now - now % 86400 + hour * 3600))
  if [ "$target" -le "$now" ]; then
    target=$((target + 86400))
  fi
  echo $((target - now))
}

idle() {
  # Sleep in the background so SIGTERM from `docker compose stop` is handled
  # immediately instead of after the current sleep.
  sleep "$1" &
  wait $!
}

schedule() {
  trap 'exit 0' TERM INT
  local state
  if ! state="$(config_state)"; then
    exit 1
  fi
  if [ "$state" = disabled ]; then
    log "event=disabled reason=no_destination_configured"
    while :; do idle 86400; done
  fi
  mkdir -p "$STATE_DIR"
  date -u +%s >"$STATE_DIR/started"
  log "event=scheduled hour_utc=$((10#${BACKUP_HOUR_UTC:-2}))"
  while :; do
    idle "$(seconds_until_next_run)"
    # A separate process so `set -e` applies inside the run; a failure is
    # logged and the next night's run still happens.
    if ! "$0" run-once; then
      log "event=failed"
    fi
  done
}

health() {
  local state now last started
  state="$(config_state)" || return 1
  [ "$state" = enabled ] || return 0
  now="$(date -u +%s)"
  if [ -f "$STATE_DIR/last-success" ]; then
    last="$(cat "$STATE_DIR/last-success")"
    [ $((now - last)) -le "$STALE_AFTER_SECONDS" ] && return 0
    return 1
  fi
  # No successful run yet: healthy until the first run is overdue.
  if [ -f "$STATE_DIR/started" ]; then
    started="$(cat "$STATE_DIR/started")"
    [ $((now - started)) -le "$STALE_AFTER_SECONDS" ] && return 0
  fi
  return 1
}

main() {
  case "${1:-schedule}" in
    schedule) schedule ;;
    run-once) run_once ;;
    check-config) config_state ;;
    health) health ;;
    list) list_dumps ;;
    fetch-dump) fetch_dump "${2:-}" "${3:-}" ;;
    restore-files) restore_files ;;
    *)
      echo "usage: offsite-backup [schedule|run-once|check-config|health|list|fetch-dump|restore-files]" >&2
      exit 2
      ;;
  esac
}

# Sourcing the script (the unit tests do) defines the functions only.
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  main "$@"
fi
