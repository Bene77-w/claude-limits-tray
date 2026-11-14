#!/usr/bin/env bash
# Installiert den Befehl usage-tray (Symlink in ~/.local/bin); kein Autostart.
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"

if ! python3 -c 'import gi, dbus, PIL; gi.require_version("Gtk", "3.0"); from gi.repository import Gtk' 2>/dev/null; then
  echo "Fehlende Abhängigkeiten. Installieren mit:" >&2
  echo "  sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0 python3-dbus python3-pil" >&2
  exit 1
fi
if ! command -v gdbus >/dev/null; then  # usage-tray findet die Instanz darüber
  echo "gdbus fehlt. Installieren mit: sudo apt install libglib2.0-bin" >&2
  exit 1
fi

chmod +x "$DIR/claude_limits_tray.py" "$DIR/usage-tray"
mkdir -p "$HOME/.local/bin"
ln -sf "$DIR/usage-tray" "$HOME/.local/bin/usage-tray"
# Reste früherer Versionen (eigener Befehl und Autostart)
rm -f "$HOME/.local/bin/claude-limits-tray" "$HOME/.config/autostart/claude-limits-tray.desktop"

echo "Installiert: usage-tray start|stop|status"
