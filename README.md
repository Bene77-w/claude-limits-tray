# Claude Limits Tray („Abo-Limits“)

Tray-Symbol für Cinnamon/X11 mit den Nutzungslimits des Claude-Abos und aller
Codex-Profile (ChatGPT-Abos). Linksklick öffnet ein Fenster mit allen Abos.

- Symbol: zeigt das Abo, das **gerade benutzt** wird (Buchstabe im Ring:
  `C` = Claude, `A`/`B` = Codex-Profil). Ring = höchster Verbrauch dieses Abos
  (grün → gelb → orange → rot); blass = Werte veraltet, gepunktet = noch keine Daten
- Auswahl: „in Benutzung“ = Aktivität in den letzten 10 min. Mehrere gleichzeitig →
  das mit dem höchsten Verbrauch. Keins aktiv → das zuletzt genutzte
- Tooltip: alle Abos, das angezeigte mit ▸ markiert
- Fenster: je Abo Plan, Status („im Tray“, „aktiv“, „genutzt vor …“), Balken je Limit,
  Prozentwert, Reset-Countdown und Reset-Uhrzeit
- Aktualisierung: alle 60 s bei offenem Fenster, sonst alle 5 min je Abo; Codex
  zusätzlich live aus den Sitzungsprotokollen (spätestens 10 s nach jeder Antwort)
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

## Datenquellen

**Claude:** `GET https://api.anthropic.com/api/oauth/usage` mit dem Access-Token
des Claude-Code-Logins (`claudeAiOauth` in `~/.claude/.credentials.json`,
alternativ `CLAUDE_CODE_OAUTH_TOKEN`). Andere Tokens in der Datei (z. B. von
MCP-Servern) werden nie verwendet. Aktivität: zuletzt geschriebene Sitzungen unter
`~/.claude/projects/`, aber nur mit Claude-Modell – `claude-dsh`-Sitzungen mit
DeepSeek zählen nicht.

**Codex:** jedes Profil `~/.codex` (= A) und `~/.codex-<x>` (= X) mit `auth.json`.
`GET https://chatgpt.com/backend-api/wham/usage` (wie `/status` in Codex) mit
`access_token` und `account_id` des Profils; der Token geht nur an chatgpt.com.
Aktivität und Live-Werte: neueste `sessions/**/rollout-*.jsonl` des Profils
(`token_count`-Einträge mit `rate_limits`). Abgelaufene Tokens erneuert die App
nicht selbst – Codex im Profil einmal starten (`codex-a`/`codex-b`).

Tokens werden nur im Speicher gehalten und nie geloggt oder angezeigt.
`utilization`/`used_percent` sind Prozentwerte.

## Fehlerfälle

| Fall | Verhalten |
|---|---|
| Token abgelaufen (laut Ablaufzeit oder HTTP 401) | Hinweis im Fenster; sobald Claude Code bzw. Codex die Anmeldedatei erneuert, wird sofort neu abgefragt, sonst nach 5 min |
| HTTP 403 | Hinweis auf fehlende Berechtigung bzw. neue Anmeldung |
| HTTP 429 | Pause mit Verdopplung bei Wiederholung (3 → 6 → 12 → 24 → max. 30 min); `Retry-After` wird beachtet, kommt aber oft als `0`. Der Aktualisieren-Knopf fragt während der Pause nicht an |
| Netzwerk weg | neuer Versuch nach 1 min |
| sonstige Serverfehler | neuer Versuch nach 3 min |

Bei Fehlern bleiben die letzten Werte sichtbar (blass im Fenster und im Symbol).
Sie liegen je Abo in `~/.cache/claude-limits-tray/usage.json` und werden nach
einem Neustart sofort angezeigt.

## Entwicklung

```bash
python3 -m unittest discover -s tests
```

Aufbau: `providers.py` (Abruf, Aktivität, Auswahl, Cache – ohne GTK) und
`claude_limits_tray.py` (Tray, Menü, Fenster).

## Abhängigkeiten

`python3-gi`, `python3-gi-cairo`, `gir1.2-gtk-3.0`, `python3-dbus`,
`python3-pil` (Systempakete). Das Tray-Symbol wird direkt als
StatusNotifierItem über D-Bus bereitgestellt (kein AppIndicator-Paket nötig).
