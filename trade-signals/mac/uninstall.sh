#!/bin/bash
# Removes the two launchd jobs. Your log (data/) and settings stay.
for label in com.tradesignals.day com.tradesignals.afterclose; do
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
  rm -f "$HOME/Library/LaunchAgents/$label.plist"
  echo "Removed $label"
done
