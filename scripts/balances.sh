#!/usr/bin/env bash
# Show current API balances. Enter = fetch again, q = stop. Never closes your terminal.
(
  cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." || exit 1
  while true; do
    clear; printf '\033[3J'          # wipe screen and scrollback so only one table shows
    .venv/bin/python scripts/balances.py
    printf '\n\033[2mEnter = refresh · q = stop\033[0m '
    read -r key || break
    [ "$key" = q ] && break
  done
)
