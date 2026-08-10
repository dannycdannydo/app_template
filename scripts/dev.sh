#!/usr/bin/env bash
# Local development process supervisor (ADR-0008).
#
# Each child runs in its own session so terminal Ctrl-C reaches this supervisor
# only. The supervisor then sends one SIGTERM to each top-level process and
# waits for it to stop, preventing orphaned API, worker, or frontend processes.

set -u

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
