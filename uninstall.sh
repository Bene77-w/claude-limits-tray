#!/usr/bin/env bash
# Beendet eine laufende Instanz und entfernt Befehl und Cache.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
"$DIR/usage-tray" stop || true
rm -f "$HOME/.local/bin/usage-tray"
# Reste früherer Versionen (eigener Befehl und Autostart)
rm -f "$HOME/.local/bin/claude-limits-tray" "$HOME/.config/autostart/claude-limits-tray.desktop"
rm -rf "$HOME/.cache/claude-limits-tray"
echo "Abo-Limits-Tray entfernt (Projektordner bleibt erhalten)."
