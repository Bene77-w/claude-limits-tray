# Claude Limits Tray

Ein Tray-Symbol für Linux-Desktops (Cinnamon/X11), das die Nutzungslimits deines
**Claude-Abos** und deiner **ChatGPT/Codex-Abos** auf einen Blick zeigt: wie viel vom
5-Stunden- und Wochenlimit verbraucht ist und wann es zurückgesetzt wird.

Du musst nicht mehr `/status` aufrufen oder eine Webseite öffnen. Das Symbol zeigt
immer das Abo, mit dem du gerade arbeitest.

> **Inoffiziell.** Dieses Projekt gehört nicht zu Anthropic oder OpenAI. Es fragt
> dieselben internen Nutzungs-Endpunkte ab wie Claude Code und Codex selbst. Diese
> Endpunkte sind nicht dokumentiert und können sich jederzeit ändern.

## Funktionen

- **Ring-Symbol im Tray:** Der Buchstabe zeigt das Abo (`A` = Anthropic/Claude,
  `C` = Codex), der Ring den Verbrauch des 5-Stunden-Limits (sonst des nächstlängeren
  Limits).
- **Ampelfarben:** grün, ab 70 % orange, ab 90 % rot. Die Farbe richtet sich nach dem
  höchsten Limit des Abos, ein Wochenlimit bei 92 % färbt den Ring also rot.
- **Automatische Abo-Wahl:** Angezeigt wird das Abo, das in den letzten 10 Minuten
  benutzt wurde, sonst das zuletzt genutzte.
- **Tooltip:** alle Abos mit Limits und Countdown bis zum Reset.
- **Fenster (Linksklick):** je Abo Plan, Status, ein Balken je Limit, Prozentwert und
  Reset-Zeitpunkt.
- **Mehrere Codex-Profile:** `~/.codex`, `~/.codex-b`, `~/.codex-c` usw. werden
  automatisch erkannt.
- **Sparsam:** Bei geschlossenem Fenster wacht die App nur alle 10 s auf und fragt die
  Server höchstens alle 5 Minuten je Abo ab. Bei HTTP 429 wartet sie mit wachsendem
  Abstand.
- **Offline-fest:** Die letzten Werte bleiben sichtbar (blass) und sind nach einem
  Neustart sofort wieder da.

Bedienung: Linksklick öffnet das Fenster, Mittelklick aktualisiert sofort, Rechtsklick
öffnet das Menü.

## Voraussetzungen

- Linux mit einem Panel, das StatusNotifierItems anzeigt (getestet mit Cinnamon/X11)
- Python 3.10 oder neuer
- Systempakete (Debian/Ubuntu/Mint):

  ```bash
  sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0 python3-dbus python3-pil libglib2.0-bin
  ```

- Mindestens einer dieser Logins:
  - [Claude Code](https://docs.claude.com/en/docs/claude-code), angemeldet mit einem Claude-Abo (`/login`)
  - [Codex CLI](https://github.com/openai/codex), angemeldet mit einem ChatGPT-Konto
    (ein reiner API-Key reicht nicht)

## Installation

```bash
git clone https://github.com/Bene77-w/claude-limits-tray.git
cd claude-limits-tray
./install.sh
```

`install.sh` prüft die Abhängigkeiten und legt den Befehl `~/.local/bin/usage-tray`
als Symlink an. Den Projektordner danach nicht verschieben. Ein Autostart wird nicht
eingerichtet.

## Benutzung

```bash
usage-tray start     # im Hintergrund starten (Log: ~/.cache/claude-limits-tray/tray.log)
usage-tray stop      # laufende Instanz beenden
usage-tray status    # läuft es? (Exit-Code 1, wenn nicht)

./claude_limits_tray.py --print   # Werte einmal im Terminal ausgeben
./claude_limits_tray.py --demo    # Oberfläche mit Beispielwerten, ohne Netzwerk
```

Für einen Autostart trägst du `usage-tray start` in den Startprogrammen deiner
Desktop-Umgebung ein.

Deinstallation: `./uninstall.sh` beendet das Tray und entfernt den Befehl und den
Cache. Der Projektordner bleibt erhalten.

## Datenschutz und Sicherheit

Die App liest nur die Anmeldedateien, die Claude Code und Codex ohnehin anlegen:

| Quelle | Gelesen wird | Gesendet an |
|---|---|---|
| `~/.claude/.credentials.json` (oder `CLAUDE_CODE_OAUTH_TOKEN`) | nur der Claude-Code-Login (`claudeAiOauth`), keine anderen Tokens wie die von MCP-Servern | `api.anthropic.com/api/oauth/usage` |
| `~/.codex*/auth.json` | `access_token` und `account_id` | `chatgpt.com/backend-api/wham/usage` |
| `~/.claude/projects/`, `~/.codex*/sessions/` | nur Zeitstempel, Modellname und Limit-Einträge, um die Aktivität zu erkennen | nirgendwohin |

- Tokens werden nur im Speicher gehalten. Sie werden nie geloggt, angezeigt oder
  gespeichert.
- Die App folgt keinen HTTP-Weiterleitungen, damit kein Token an einen anderen Host
  gelangt.
- Abgelaufene Tokens erneuert sie nicht selbst. Das übernehmen Claude Code bzw. Codex
  beim nächsten Start, danach aktualisiert sich die Anzeige automatisch.
- Im Cache (`~/.cache/claude-limits-tray/usage.json`) liegen nur Prozentwerte,
  Reset-Zeiten und der Name des Plans.
- Es gibt keine Telemetrie und keine Verbindungen zu anderen Servern.

## Fehlerbehebung

| Anzeige | Ursache und Lösung |
|---|---|
| „Token abgelaufen“ | Claude Code bzw. Codex einmal starten. Sobald die Anmeldedatei erneuert ist, fragt das Tray sofort neu ab. |
| HTTP 403 | Der Login hat keine Berechtigung für Nutzungsdaten. Neu anmelden (`/login`). |
| HTTP 429 | Zu viele Anfragen. Das Tray pausiert automatisch (3 → 6 → 12 → … max. 30 min). |
| Symbol blass | Die Werte sind veraltet (offline oder Fehler). Details stehen im Fenster. |
| Kein Symbol sichtbar | Das Panel zeigt keine StatusNotifierItems. Prüfe das mit `usage-tray status` und im Log. |

## Entwicklung

```bash
python3 -m unittest discover -s tests
```

| Datei | Inhalt |
|---|---|
| `providers.py` | Abruf, Aktivitätserkennung, Abo-Wahl, Cache (ohne GTK, gut testbar) |
| `claude_limits_tray.py` | Tray-Symbol (StatusNotifierItem über D-Bus), Menü, Fenster |
| `usage-tray` | Start/Stopp; findet die Instanz über ihren D-Bus-Namen |

Das Tray-Symbol wird direkt über D-Bus bereitgestellt, ein AppIndicator-Paket ist
nicht nötig. Startet das Panel neu, meldet sich das Symbol automatisch wieder an.
Eine zweite Instanz öffnet nur das Fenster der laufenden.

## Lizenz

[MIT](LICENSE)
