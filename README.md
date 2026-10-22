# Claude Limits Tray

Tray-Symbol für Cinnamon/X11, das per Linksklick ein kleines Fenster mit dem
5-Stunden- und Wochen-Limit des Claude-Abonnements öffnet.

- Symbol: Ring, der den höchsten Verbrauch zeigt (grün → gelb → orange → rot)
- Tooltip: aktuelle Prozente, ohne Klick
- Fenster: Balken je Limit, Prozentwert, Reset-Countdown und Reset-Uhrzeit
- Aktualisierung: alle 60 s bei offenem Fenster, sonst alle 5 min
- Linksklick = öffnen, Mittelklick = sofort aktualisieren, Rechtsklick = Menü

## Installation

```bash
./install.sh
```

Legt `~/.local/bin/claude-limits-tray` an und trägt den Autostart unter
`~/.config/autostart/claude-limits-tray.desktop` ein.

## Start

```bash
~/.local/bin/claude-limits-tray          # normal
~/.local/bin/claude-limits-tray --demo   # Beispielwerte, ohne Netzwerk
~/.local/bin/claude-limits-tray --print  # Werte auf der Konsole ausgeben
```

Eine zweite Instanz öffnet nur das Fenster der laufenden Instanz.

## Datenquelle

`GET https://api.anthropic.com/api/oauth/usage` mit dem OAuth-Token aus
`~/.claude/.credentials.json` (alternativ Umgebungsvariable
`CLAUDE_CODE_OAUTH_TOKEN`). Der Token wird nur im Speicher gehalten und
nie geloggt oder angezeigt.

Antwortet der Endpoint mit 401/403, ist der Token abgelaufen: Claude Code
einmal starten (oder `/login`), damit er erneuert wird. Bei 429 oder
Netzwerkfehlern zeigt das Fenster den Fehler, behält die letzten Werte und
wartet 3 Minuten bis zum nächsten Versuch.

Die zuletzt geladenen Werte liegen in
`~/.cache/claude-limits-tray/usage.json` und werden nach einem Neustart
sofort angezeigt.

## Abhängigkeiten

`python3-gi`, `python3-dbus`, `python3-pil`, `python3-gi-cairo` — alle als
Systempakete vorhanden. Das Tray-Symbol wird direkt als StatusNotifierItem
über D-Bus bereitgestellt (kein AppIndicator-Paket nötig).
