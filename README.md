# Claude Limits Tray

Tray-Symbol für Cinnamon/X11, das per Linksklick ein kleines Fenster mit dem
5-Stunden- und Wochen-Limit des Claude-Abonnements öffnet.

- Symbol: Ring, der den höchsten Verbrauch zeigt (grün → gelb → orange → rot);
  blass = Werte veraltet (letzter Abruf fehlgeschlagen), gepunktet = noch keine Daten
- Tooltip: aktuelle Prozente, ohne Klick
- Fenster: Balken je Limit, Prozentwert, Reset-Countdown und Reset-Uhrzeit
- Aktualisierung: alle 60 s bei offenem Fenster, sonst alle 5 min
- Linksklick = öffnen, Mittelklick = sofort aktualisieren, Rechtsklick = Menü

## Installation

```bash
./install.sh     # prüft Abhängigkeiten, legt Symlink und Autostart an
./uninstall.sh   # entfernt Symlink, Autostart und Cache
```

## Start

```bash
setsid -f ~/.local/bin/claude-limits-tray >/dev/null 2>&1   # ohne Neuanmeldung
~/.local/bin/claude-limits-tray --demo   # Beispielwerte, ohne Netzwerk
~/.local/bin/claude-limits-tray --print  # Werte auf der Konsole ausgeben
```

Eine zweite Instanz öffnet nur das Fenster der laufenden Instanz. Startet das
Cinnamon-Panel neu, meldet sich das Symbol automatisch wieder an.

## Datenquelle

`GET https://api.anthropic.com/api/oauth/usage` mit dem Access-Token des
Claude-Code-Logins (`claudeAiOauth` in `~/.claude/.credentials.json`,
alternativ Umgebungsvariable `CLAUDE_CODE_OAUTH_TOKEN`). Andere Tokens in der
Datei (z. B. von MCP-Servern) werden nie verwendet. Der Token wird nur im
Speicher gehalten und nie geloggt oder angezeigt. `utilization` ist ein
Prozentwert.

## Fehlerfälle

| Fall | Verhalten |
|---|---|
| Token abgelaufen (laut `expiresAt` oder HTTP 401) | Hinweis im Fenster; sobald Claude Code die Anmeldedatei erneuert, wird sofort neu abgefragt, sonst nach 5 min |
| HTTP 403 | Hinweis auf fehlende Berechtigung bzw. neue Anmeldung |
| HTTP 429 | Pause mit Verdopplung bei Wiederholung (3 → 6 → 12 → 24 → max. 30 min); `Retry-After` wird beachtet, kommt aber oft als `0`. Der Aktualisieren-Knopf fragt während der Pause nicht an |
| Netzwerk weg | neuer Versuch nach 1 min |
| sonstige Serverfehler | neuer Versuch nach 3 min |

Bei Fehlern bleiben die letzten Werte sichtbar (blass im Fenster und im Symbol).
Sie liegen in `~/.cache/claude-limits-tray/usage.json` und werden nach einem
Neustart sofort angezeigt.

## Entwicklung

```bash
python3 -m unittest discover -s tests
```

## Abhängigkeiten

`python3-gi`, `python3-gi-cairo`, `gir1.2-gtk-3.0`, `python3-dbus`,
`python3-pil` (Systempakete). Das Tray-Symbol wird direkt als
StatusNotifierItem über D-Bus bereitgestellt (kein AppIndicator-Paket nötig).
