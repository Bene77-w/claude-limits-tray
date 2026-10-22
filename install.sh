#!/usr/bin/env bash
# Installiert das Claude-Limits-Tray: Symlink in ~/.local/bin und Autostart.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"

if ! python3 -c 'import gi, dbus, PIL; gi.require_version("Gtk", "3.0"); from gi.repository import Gtk' 2>/dev/null; then
  echo "Fehlende Abhängigkeiten. Installieren mit:" >&2
  echo "  sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0 python3-dbus python3-pil" >&2
  exit 1
fi

chmod +x "$DIR/claude_limits_tray.py"
mkdir -p "$HOME/.local/bin" "$HOME/.config/autostart"
ln -sf "$DIR/claude_limits_tray.py" "$HOME/.local/bin/claude-limits-tray"
sed "s|@EXEC@|$DIR/claude_limits_tray.py|g" \
  "$DIR/claude-limits-tray.desktop" > "$HOME/.config/autostart/claude-limits-tray.desktop"

echo "Installiert:"
echo "  Befehl   : ~/.local/bin/claude-limits-tray"
echo "  Autostart: ~/.config/autostart/claude-limits-tray.desktop"
echo "Starten ohne Neuanmeldung: setsid -f ~/.local/bin/claude-limits-tray >/dev/null 2>&1"
