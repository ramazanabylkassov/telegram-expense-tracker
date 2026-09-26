#!/bin/bash
LABEL="local.expense-bot"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null && echo "Stopped." || echo "Not running."
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
