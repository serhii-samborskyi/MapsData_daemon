#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PORT=8095
HOST="0.0.0.0"
PYTHON_BIN="${PYTHON_BIN:-python3}"
PID_FILE="$SCRIPT_DIR/logs/daemon_web.pid"
LOG_FILE="$SCRIPT_DIR/logs/daemon_web.log"

mkdir -p "$SCRIPT_DIR/logs"

stop_running() {
  if [[ -f "$PID_FILE" ]]; then
    PID="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [[ -n "${PID}" ]] && kill -0 "$PID" 2>/dev/null; then
      kill "$PID" 2>/dev/null || true
      sleep 1
      if kill -0 "$PID" 2>/dev/null; then
        kill -9 "$PID" 2>/dev/null || true
      fi
    fi
    rm -f "$PID_FILE"
  fi

  # Cleanup any leftover process bound to the same daemon web script/port.
  pkill -f "daemon_web.py --host ${HOST} --port ${PORT}" 2>/dev/null || true
}

get_lan_ip() {
  local ip=""
  if command -v ipconfig >/dev/null 2>&1; then
    ip="$(ipconfig getifaddr en0 2>/dev/null || true)"
    if [[ -z "$ip" ]]; then
      ip="$(ipconfig getifaddr en1 2>/dev/null || true)"
    fi
  fi

  if [[ -z "$ip" ]] && command -v hostname >/dev/null 2>&1; then
    ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  fi

  if [[ -z "$ip" ]] && command -v ifconfig >/dev/null 2>&1; then
    ip="$(ifconfig | awk '/inet / && $2 != "127.0.0.1" {print $2; exit}')"
  fi

  if [[ -z "$ip" ]]; then
    ip="localhost"
  fi

  printf '%s' "$ip"
}

if [[ "${1:-}" == "--stop" ]]; then
  stop_running
  echo "Daemon Web UI stopped (port ${PORT})."
  exit 0
fi

stop_running

echo "Starting Daemon Web UI on port ${PORT}..."
nohup "$PYTHON_BIN" daemon_web.py --host "$HOST" --port "$PORT" --config "$SCRIPT_DIR/daemon_settings.json" >> "$LOG_FILE" 2>&1 &
NEW_PID=$!
echo "$NEW_PID" > "$PID_FILE"

# Wait briefly for startup and verify health endpoint.
for _ in {1..20}; do
  if curl -fsS "http://127.0.0.1:${PORT}/api/state?log_limit=1" >/dev/null 2>&1; then
    LAN_IP="$(get_lan_ip)"
    echo "Daemon Web UI started."
    echo "Local URL: http://127.0.0.1:${PORT}"
    echo "LAN URL:   http://${LAN_IP}:${PORT}"
    echo "PID: ${NEW_PID}"
    echo "Log: ${LOG_FILE}"
    exit 0
  fi
  sleep 0.25
done

echo "Failed to start Daemon Web UI on port ${PORT}. Check logs: ${LOG_FILE}" >&2
exit 1
