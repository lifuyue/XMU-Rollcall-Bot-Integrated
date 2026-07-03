#!/bin/zsh
set -euo pipefail

LABEL="com.lifuyue.xmu-rollcall"
PROJECT_ROOT="/Users/lifuyue/Projects/XMU-Rollcall-Bot"
RUNNER="$PROJECT_ROOT/scripts/run-xmu-rollcall.sh"
SOURCE_PLIST="$PROJECT_ROOT/scripts/$LABEL.plist"
TARGET_PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
CONFIG_DIR="$HOME/.xmu_rollcall"
LOG_DIR="$CONFIG_DIR/logs"
DOMAIN="gui/$(id -u)"

mkdir -p "$LOG_DIR" "$HOME/Library/LaunchAgents"
chmod +x "$RUNNER"

plutil -lint "$SOURCE_PLIST" >/dev/null
install -m 0644 "$SOURCE_PLIST" "$TARGET_PLIST"
plutil -lint "$TARGET_PLIST" >/dev/null

launchctl bootout "$DOMAIN" "$TARGET_PLIST" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$TARGET_PLIST"
launchctl enable "$DOMAIN/$LABEL"
launchctl kickstart -k "$DOMAIN/$LABEL"

echo "LaunchAgent installed and started: $LABEL"
echo "Status: launchctl print $DOMAIN/$LABEL"
echo "Logs: $LOG_DIR/launchd.out.log"
