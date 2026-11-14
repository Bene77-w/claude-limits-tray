# Claude Limits Tray — Pitch

## Problem

Wer Claude- und ChatGPT/Codex-Abos intensiv nutzt, muss für die Frage „Wie viel ist
noch übrig?" regelmäßig die Nutzungsseiten im Browser oder `/status` öffnen. Das
unterbricht den Arbeitsfluss und passiert meist erst dann, wenn ein Limit schon greift.

## Lösung

Ein Tray-Symbol in der Cinnamon-Leiste, das den Verbrauch permanent sichtbar macht:
Das Symbol zeigt das gerade benutzte Abo (Buchstabe A = Anthropic/Claude, C = Codex),
bei mehreren gleichzeitig das am stärksten ausgelastete; der Ring zeigt dessen
5-Stunden-Limit (sonst das nächstlängere) und färbt sich orange ab 70 % bzw. rot ab 90 %,
sobald irgendein Limit dieses Abos die Schwelle erreicht. Ein Linksklick öffnet ein kleines Fenster mit allen Abos: Plan, Limits, Prozent,
Reset-Countdown und Reset-Uhrzeit. Kein Browser, kein Kontextwechsel.

## Funktionsumfang (Scope)

**Enthalten**

- Abos: Claude (Claude-Code-Login) und jedes Codex-Profil (`~/.codex` = A, `~/.codex-b` = B)
- Tray-Symbol als StatusNotifierItem mit Ringdiagramm (5-h-Limit, sonst nächstlängeres) und
  Buchstabe; grün, orange ab 70 %, rot ab 90 % (höchstes Limit des Abos)
- Auswahl: Aktivität in den letzten 10 min; mehrere aktiv → höchster Wert im Ring-Limit;
  keins → zuletzt genutzt
- Tooltip ohne Klick: alle Abos mit Limits und Reset-Countdown, angezeigtes mit ▸
- Popup-Fenster (Balkenfarbe je Limit nach denselben Schwellen): je Abo Plan, Status („im Tray“, „aktiv“, „genutzt vor …“), Balken je Limit,
  Prozent, „Zurückgesetzt in …" plus Datum/Uhrzeit
- Linksklick öffnet, Mittelklick aktualisiert, Rechtsklick zeigt Menü
- Menü: Fenster öffnen, Aktualisieren, Beenden
- Automatische Aktualisierung: 60 s bei offenem Fenster, sonst 5 min je Abo; Codex zusätzlich
  live aus den Sitzungsprotokollen
- Cache je Abo in `~/.cache/claude-limits-tray/usage.json` — Werte sind nach Neustart sofort da
- Fehlerbehandlung: 401/403 (Token abgelaufen/ungültig), 429 (Backoff), Netzwerk- und Serverfehler
- Einzelinstanz: ein zweiter Start öffnet nur das bestehende Fenster
- Start/Stopp per `usage-tray start|stop|status` (kein Autostart), `--demo` und `--print`
  als Test-/Diagnosewege
- Timer bei geschlossenem Fenster nur alle 10 s, Symbol-Pixmaps zwischengespeichert

**Nicht enthalten**

- Windows/macOS (reine Linux-/X11-Umsetzung, getestet unter Cinnamon)
- Eigene Token-Erneuerung — abgelaufene Tokens erneuern Claude Code bzw. Codex selbst
- Verlaufsdiagramme, Benachrichtigungen bei Schwellwerten
- Mehrere Claude-Logins gleichzeitig (es zählt der aktuelle Claude-Code-Login)
- Schreiben von Einstellungen in die Claude- oder Codex-Konfiguration

## Bearbeitete und erzeugte Dateien

| Datei | Zeilen | Zweck |
|---|---:|---|
| `~/claude-limits-tray/claude_limits_tray.py` | 927 | Tray-Item, Menü, Fenster |
| `~/claude-limits-tray/providers.py` | 722 | Claude/Codex-Abruf, Aktivitätserkennung, Auswahl, Cache |
| `~/claude-limits-tray/tests/` | 567 | Unit-Tests (Abruf, Token-Auswahl, Backoff, Auswahl, Icon, Timer-Takt) |
| `~/claude-limits-tray/README.md` | — | Installation, Bedienung, Datenquelle, Fehlerfälle |
| `~/claude-limits-tray/usage-tray` | — | Befehl `start`/`stop`/`status` (findet die Instanz über D-Bus) |
| `~/claude-limits-tray/install.sh` / `uninstall.sh` | — | Befehl `usage-tray` einrichten bzw. entfernen |
| `~/.local/bin/usage-tray` | — | Symlink auf `usage-tray` (installiert) |
| `~/.cache/claude-limits-tray/usage.json` | — | Laufzeit-Cache der letzten Werte |
| `~/.cache/claude-limits-tray/tray.log` | — | Ausgaben des letzten `usage-tray start` |

## Technik

- Reines Python 3 mit GTK3, `dbus-python` und Pillow — keine zusätzlichen Pakete
- Das Tray-Symbol wird direkt als StatusNotifierItem über D-Bus bereitgestellt
  (`org.kde.StatusNotifierItem` plus minimales `com.canonical.dbusmenu`);
  ein AppIndicator-Paket ist nicht nötig
- Datenquelle Claude: `GET https://api.anthropic.com/api/oauth/usage` — derselbe Endpoint,
  den `/usage` in Claude Code nutzt; OAuth-Token aus `~/.claude/.credentials.json`
  (`claudeAiOauth`) oder `CLAUDE_CODE_OAUTH_TOKEN`; andere Tokens werden nie gesendet
- Datenquelle Codex: `GET https://chatgpt.com/backend-api/wham/usage` (wie `/status`) mit
  Token und Account aus `<profil>/auth.json`; Live-Werte und Aktivität aus
  `<profil>/sessions/**/rollout-*.jsonl`
- Aktivität Claude: Sitzungen in `~/.claude/projects/` mit Claude-Modell (claude-dsh zählt nicht)
- Tokens bleiben im Speicher, werden weder geloggt noch angezeigt; Weiterleitungen werden
  nicht verfolgt, damit kein Token an einen anderen Host geht
- Aufbau: `providers.py` (Daten, ohne GTK) und `claude_limits_tray.py` (Oberfläche)
- Abruf läuft in einem Hintergrund-Thread, die Oberfläche bleibt bedienbar
- Fenster ist rahmenlos, abgerundet, öffnet neben dem Mauszeiger innerhalb der
  Arbeitsfläche (also nie hinter der Leiste)

## Status

23.10.2026: Prototyp überarbeitet (Token-Wahl, Prozent-Umrechnung, 429-Backoff,
Panel-Neustart, Fokus) und um Codex-Abos erweitert (Branch `feature/codex-abos`,
Review-Anker `c492ca5`). Das Symbol zeigt das gerade benutzte Abo; bei mehreren
das mit dem höchsten Verbrauch. Review (Konformität + Logik) mit Befunden
behoben. 42 Unit-Tests; live geprüft mit echten Daten
(Claude Pro, Codex A Free, Codex B Plus) und End-to-End mit simuliertem
Codex-Profil auf eigenem D-Bus.

14.11.2026: Befehl `usage-tray start|stop|status` statt Autostart; Timer bei
geschlossenem Fenster 10 s statt 1 s; Symbol-Pixmaps gecacht; Codex-Archiv nur
alle 5 min vollständig gescannt (Branch `usage-tray-cli`, Review-Anker `b020e9a`).
Gemessen über 120 s bei geschlossenem Fenster (b020e9a → 6e608a0): Kontextwechsel
126 → 33, CPU-Ticks 16 → 10, RAM unverändert ~61 MB; Scan 8,3 → 2,8 ms. Final
Review abgenommen (`docs/reviews/usage-tray-cli/`); 44 Unit-Tests.

## Nächste Schritte (optional)

1. Optionale Desktop-Benachrichtigung ab 80 % / 100 %
2. Kleine Verlaufsgrafik der letzten 24 h aus dem Cache
3. Paketierung als `.deb`
