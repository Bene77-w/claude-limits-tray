#!/usr/bin/env bash
# Entfernt Symlink, Autostart und Cache und beendet eine laufende Instanz.
set -euo pipefail

pkill -f '^python3 .*(claude_limits_tray\.py|claude-limits-tray)( |$)' 2>/dev/null || true
rm -f "$HOME/.local/bin/claude-limits-tray" "$HOME/.config/autostart/claude-limits-tray.desktop"
rm -rf "$HOME/.cache/claude-limits-tray"
echo "Claude-Limits-Tray entfernt (Projektordner bleibt erhalten)."
