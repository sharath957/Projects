#!/bin/bash
# Installs two launchd jobs for your Mac user (no sudo):
#   com.tradesignals.day         weekdays 6:40 AM local: opening-range scans + alerts until flat-by
#   com.tradesignals.afterclose  weekdays 1:35 PM local: swing scan + outcome tracker + Notion sync
# Times assume the Mac's clock is Pacific (Vancouver). Run: bash mac/install.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="$(command -v python3 || true)"
AGENTS="$HOME/Library/LaunchAgents"
LOGS="$ROOT/data/logs"

if [ -z "$PY" ]; then echo "python3 not found. Install the Xcode command line tools: xcode-select --install"; exit 1; fi
"$PY" -c 'import sys; assert sys.version_info >= (3, 9), sys.version' \
  || { echo "Python 3.9+ needed (found $("$PY" --version))"; exit 1; }

TZNAME="$(readlink /etc/localtime 2>/dev/null | sed 's#.*/zoneinfo/##')"
case "$TZNAME" in
  America/Vancouver|America/Los_Angeles|America/Tijuana) ;;
  *) echo "WARNING: this Mac's time zone is '$TZNAME', not Pacific. The 6:40 / 13:35 schedule is local time."
     echo "         Edit the hours in this script before installing, or set the Mac to Pacific time." ;;
esac

[ -f "$ROOT/config.json" ] || { cp "$ROOT/config.example.json" "$ROOT/config.json"; echo "Created config.json from the example."; }
[ -f "$ROOT/.env" ] || { cp "$ROOT/.env.example" "$ROOT/.env"; chmod 600 "$ROOT/.env"; echo "Created .env: add your NTFY_TOPIC (and NOTION_TOKEN)."; }
mkdir -p "$AGENTS" "$LOGS"

write_plist () {  # label hour minute command
  local label="$1" hour="$2" minute="$3" cmd="$4" days=""
  for d in 1 2 3 4 5; do
    days="$days<dict><key>Weekday</key><integer>$d</integer><key>Hour</key><integer>$hour</integer><key>Minute</key><integer>$minute</integer></dict>"
  done
  cat > "$AGENTS/$label.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key><array>
    <string>/usr/bin/caffeinate</string><string>-i</string>
    <string>$PY</string><string>$ROOT/run.py</string><string>$cmd</string>
  </array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>StartCalendarInterval</key><array>$days</array>
  <key>StandardOutPath</key><string>$LOGS/launchd-$cmd.log</string>
  <key>StandardErrorPath</key><string>$LOGS/launchd-$cmd.log</string>
</dict></plist>
EOF
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$AGENTS/$label.plist"
  echo "Installed $label (weekdays $hour:$(printf %02d "$minute"))"
}

write_plist com.tradesignals.day 6 40 day
write_plist com.tradesignals.afterclose 13 35 after-close

echo
echo "Done. Check with:  launchctl list | grep tradesignals"
echo "Test your phone:   python3 \"$ROOT/run.py\" test-notify"
echo "Keep the Mac awake on trading days, e.g.:  sudo pmset repeat wakeorpoweron MTWRF 06:30:00"
