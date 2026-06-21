#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="$APP_DIR/out"
ENV_FILE="$APP_DIR/.env"

mkdir -p "$LOG_DIR"

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

if [[ -z "${NEWS_SMTP_PASSWORD:-}" ]]; then
  echo "$(date '+%Y-%m-%d %H:%M:%S %z') Missing NEWS_SMTP_PASSWORD. Set it in $ENV_FILE." >&2
  exit 1
fi

cd "$APP_DIR"
if command -v python3.11 >/dev/null 2>&1; then
  PYTHON_BIN="$(command -v python3.11)"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="$(command -v python3)"
else
  echo "$(date '+%Y-%m-%d %H:%M:%S %z') Missing python3.11 or python3." >&2
  exit 1
fi

timeout 20m "$PYTHON_BIN" main.py
