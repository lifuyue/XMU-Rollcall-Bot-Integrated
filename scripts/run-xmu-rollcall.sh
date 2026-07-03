#!/bin/zsh
set -u

PROJECT_ROOT="/Users/lifuyue/Projects/XMU-Rollcall-Bot"
APP_DIR="$PROJECT_ROOT/xmu-rollcall-cli"
PYTHON="$PROJECT_ROOT/.venv/bin/python"
CONFIG_DIR="$HOME/.xmu_rollcall"
CONFIG_FILE="$CONFIG_DIR/config.json"

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export PYTHONUNBUFFERED="1"
export TERM="${TERM:-xterm-256color}"
export XMU_ROLLCALL_CONFIG_DIR="$CONFIG_DIR"

mkdir -p "$CONFIG_DIR/logs"

if [ ! -x "$PYTHON" ]; then
  echo "$(date '+%Y-%m-%d %H:%M:%S') Missing virtualenv python: $PYTHON" >&2
  exit 127
fi

cd "$APP_DIR" || exit 1

while ! "$PYTHON" - "$CONFIG_FILE" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
try:
    data = json.loads(path.read_text(encoding="utf-8"))
except Exception:
    sys.exit(1)

for account in data.get("accounts") or []:
    if (
        account.get("username")
        and account.get("password")
    ):
        sys.exit(0)

sys.exit(1)
PY
do
  echo "$(date '+%Y-%m-%d %H:%M:%S') Waiting for account config at $CONFIG_FILE. Run: $PROJECT_ROOT/.venv/bin/xmu add-account"
  sleep 60
done

exec "$PYTHON" -m xmu_rollcall.cli start-all
