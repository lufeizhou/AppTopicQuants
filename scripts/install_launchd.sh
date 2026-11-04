#!/bin/zsh
# Installs two jobs: weekly full run (Wednesday 04:00) and daily light run (03:30). Both free unless keys are set.
set -e
cd "$(dirname "$0")/.."
mkdir -p logs
for j in weekly light; do
  cp scripts/launchd/com.sanvvy.apptopics.$j.plist ~/Library/LaunchAgents/
  launchctl unload ~/Library/LaunchAgents/com.sanvvy.apptopics.$j.plist 2>/dev/null || true
  launchctl load ~/Library/LaunchAgents/com.sanvvy.apptopics.$j.plist
done
launchctl list | grep apptopics
