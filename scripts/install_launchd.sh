#!/bin/zsh
# Installs launchd jobs. Default: only the free daily light run (03:30, ~15 min, no paid calls).
#   ./scripts/install_launchd.sh            # light only (the decision of 2026-11-04)
#   ./scripts/install_launchd.sh light weekly   # also the weekly full run (paid APIs; normally started by hand)
set -e
cd "$(dirname "$0")/.."
mkdir -p logs
for j in "${@:-light}"; do
  cp scripts/launchd/com.sanvvy.apptopics.$j.plist ~/Library/LaunchAgents/
  launchctl unload ~/Library/LaunchAgents/com.sanvvy.apptopics.$j.plist 2>/dev/null || true
  launchctl load ~/Library/LaunchAgents/com.sanvvy.apptopics.$j.plist
done
launchctl list | grep apptopics
