#!/usr/bin/env bash
# Local development process supervisor (ADR-0008).
#
# Each child runs in its own session so terminal Ctrl-C reaches this supervisor
# only. The supervisor then sends one SIGTERM to each top-level process and
# waits for it to stop, preventing orphaned API, worker, or frontend processes.

set -u

# Some VPN clients export a SOCKS proxy such as ``socks://127.0.0.1:2080``.
# HTTPX (used by the WorkOS adapter) cannot construct a client from that URL,
# which prevents authenticated local API requests from reaching WorkOS. This
# is deliberately opt-in and only lives in the native local-development
# supervisor; production and Compose processes are unaffected.
case "${DEV_DISABLE_PROXY:-}" in
  1|true|TRUE|yes|YES|on|ON)
    unset ALL_PROXY all_proxy HTTP_PROXY http_proxy HTTPS_PROXY https_proxy NO_PROXY no_proxy
    echo "DEV_DISABLE_PROXY enabled: starting local processes without proxy environment variables"
    ;;
esac

api_pid=""
worker_pid=""
frontend_pid=""

cleanup() {
  trap - INT TERM EXIT
  for pid in "$api_pid" "$worker_pid" "$frontend_pid"; do
    if [[ -n "$pid" ]]; then
      kill -TERM "$pid" 2>/dev/null || true
    fi
  done
  for pid in "$api_pid" "$worker_pid" "$frontend_pid"; do
    if [[ -n "$pid" ]]; then
      wait "$pid" 2>/dev/null || true
    fi
  done
}

setsid bash -c 'cd backend && exec uv run uvicorn app.main:app --reload --port 8000' &
api_pid=$!
setsid bash -c 'cd backend && exec uv run dramatiq app.workers --processes 1 --threads "${WORKER_CONCURRENCY:-8}" --worker-shutdown-timeout 10000' &
worker_pid=$!
setsid bash -c 'cd frontend && exec ./node_modules/.bin/vite' &
frontend_pid=$!

trap 'cleanup; exit 0' INT TERM
trap cleanup EXIT

wait "$api_pid" "$worker_pid" "$frontend_pid"
