#!/usr/bin/env bash
# Start the Hosted Agents backend proxy (and serve the UI from /).
# Handles venv creation, dependency install, .env bootstrap, and az login check.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
cd "$SCRIPT_DIR"

VENV="$SCRIPT_DIR/.venv"
BACKEND="$SCRIPT_DIR/backend"
PY_BIN="${PYTHON:-python3}"

log() { printf '\033[1;36m[start]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*"; }
err() { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; }

# 1. Python check
if ! command -v "$PY_BIN" >/dev/null 2>&1; then
  err "python3 not found. Install Python 3.12+ or set PYTHON=<path>."
  exit 1
fi
PY_VER=$("$PY_BIN" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
log "Using $PY_BIN (Python $PY_VER)"

# 2. Virtualenv
if [[ ! -x "$VENV/bin/python" ]]; then
  log "Creating virtualenv at $VENV"
  "$PY_BIN" -m venv "$VENV"
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"

# 3. Install / refresh dependencies (idempotent)
REQ="$BACKEND/requirements.txt"
STAMP="$VENV/.requirements.sha256"
NEW_SUM=$(sha256sum "$REQ" | awk '{print $1}')
OLD_SUM=$(cat "$STAMP" 2>/dev/null || true)
if [[ "$NEW_SUM" != "$OLD_SUM" ]]; then
  log "Installing backend requirements"
  if command -v uv >/dev/null 2>&1; then
    VIRTUAL_ENV="$VENV" uv pip install --quiet -r "$REQ"
  else
    python -m ensurepip --upgrade >/dev/null 2>&1 || true
    python -m pip install --quiet --upgrade pip
    python -m pip install --quiet -r "$REQ"
  fi
  echo "$NEW_SUM" > "$STAMP"
else
  log "Dependencies up to date"
fi

# 4. .env bootstrap
ENV_FILE="$BACKEND/.env"
if [[ ! -f "$ENV_FILE" ]]; then
  log "Creating $ENV_FILE from .env.example"
  cp "$BACKEND/.env.example" "$ENV_FILE"
  warn "Edit $ENV_FILE before running in Foundry mode (set FOUNDRY_PROJECT_ENDPOINT / FOUNDRY_AGENT_NAME)."
fi

# 5. Verify Azure CLI login (required — the proxy uses DefaultAzureCredential)
log "Verifying Azure CLI login"
if ! command -v az >/dev/null 2>&1; then
  warn "Azure CLI 'az' not found. DefaultAzureCredential may still work via env/managed identity."
elif ! az account show >/dev/null 2>&1; then
  warn "Not logged in to Azure. Run: az login"
else
  SUB=$(az account show --query name -o tsv 2>/dev/null || echo "?")
  log "Azure subscription: $SUB"
fi

# 6. Port
PORT=$(grep -E '^PORT=' "$ENV_FILE" | tail -n1 | cut -d= -f2 | tr -d '"' | tr -d "'" || echo "8080")
PORT="${PORT:-8080}"

log "Starting backend on http://localhost:$PORT/"
cd "$BACKEND"
exec python server.py
