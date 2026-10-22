#!/usr/bin/env bash
# Installiert das Claude-Limits-Tray: Symlink in ~/.local/bin und Autostart.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
chmod +x "$DIR/claude_limits_tray.py"

mkdir -p "$HOME/.local/bin" "$HOME/.config/autostart"
ln -sf "$DIR/claude_limits_tray.py" "$HOME/.local/bin/claude-limits-tray"
sed "s|@EXEC@|$DIR/claude_limits_tray.py|g" \
  "$DIR/claude-limits-tray.desktop" > "$HOME/.config/autostart/claude-limits-tray.desktop"

echo "Installiert:"
echo "  Befehl   : ~/.local/bin/claude-limits-tray"
echo "  Autostart: ~/.config/autostart/claude-limits-tray.desktop"
