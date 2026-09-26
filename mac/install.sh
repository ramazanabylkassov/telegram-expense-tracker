#!/bin/bash
# Installs the bot as a background service on macOS (launchd).
# It starts at login, restarts if it crashes, and logs to ~/Library/Logs/expense-bot.log
set -euo pipefail
cd "$(dirname "$0")/.."
DIR="$(pwd)"
LABEL="local.expense-bot"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
.venv/bin/pip install -q -r requirements.txt

mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>$DIR/.venv/bin/python</string><string>$DIR/bot.py</string></array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>$HOME/Library/Logs/expense-bot.log</string>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/expense-bot.log</string>
</dict>
</plist>
PLIST

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Installed. Logs: tail -f ~/Library/Logs/expense-bot.log"
echo "Stop:      bash mac/uninstall.sh"
