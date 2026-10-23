# Claude Limits Tray — Pitch

## Problem

Wer Claude- und ChatGPT/Codex-Abos intensiv nutzt, muss für die Frage „Wie viel ist
noch übrig?" regelmäßig die Nutzungsseiten im Browser oder `/status` öffnen. Das
unterbricht den Arbeitsfluss und passiert meist erst dann, wenn ein Limit schon greift.

## Lösung

Ein Tray-Symbol in der Cinnamon-Leiste, das den Verbrauch permanent sichtbar macht:
Das Symbol zeigt das gerade benutzte Abo (Buchstabe C = Claude, A/B = Codex-Profil),
bei mehreren gleichzeitig das am stärksten ausgelastete; der Ring zeigt dessen höchsten
Wert. Ein Linksklick öffnet ein kleines Fenster mit allen Abos: Plan, Limits, Prozent,
Reset-Countdown und Reset-Uhrzeit. Kein Browser, kein Kontextwechsel.

## Funktionsumfang (Scope)

**Enthalten**

- Abos: Claude (Claude-Code-Login) und jedes Codex-Profil (`~/.codex` = A, `~/.codex-b` = B)
- Tray-Symbol als StatusNotifierItem mit Ringdiagramm (grün → gelb → orange → rot) und Buchstabe
- Auswahl: Aktivität in den letzten 10 min; mehrere aktiv → höchster Verbrauch; keins → zuletzt genutzt
- Tooltip ohne Klick: alle Abos mit Limits und Reset-Countdown, angezeigtes mit ▸
- Popup-Fenster: je Abo Plan, Status („im Tray“, „aktiv“, „genutzt vor …“), Balken je Limit,
  Prozent, „Zurückgesetzt in …" plus Datum/Uhrzeit
- Linksklick öffnet, Mittelklick aktualisiert, Rechtsklick zeigt Menü
- Menü: Fenster öffnen, Aktualisieren, Beenden
- Automatische Aktualisierung: 60 s bei offenem Fenster, sonst 5 min je Abo; Codex zusätzlich
  live aus den Sitzungsprotokollen
- Cache je Abo in `~/.cache/claude-limits-tray/usage.json` — Werte sind nach Neustart sofort da
- Fehlerbehandlung: 401/403 (Token abgelaufen/ungültig), 429 (Backoff), Netzwerk- und Serverfehler
- Einzelinstanz: ein zweiter Start öffnet nur das bestehende Fenster
- Autostart beim Login, `--demo` und `--print` als Test-/Diagnosewege

**Nicht enthalten**

- Windows/macOS (reine Linux-/X11-Umsetzung, getestet unter Cinnamon)
- Eigene Token-Erneuerung — abgelaufene Tokens erneuern Claude Code bzw. Codex selbst
- Verlaufsdiagramme, Benachrichtigungen bei Schwellwerten
- Mehrere Claude-Logins gleichzeitig (es zählt der aktuelle Claude-Code-Login)
- Schreiben von Einstellungen in die Claude- oder Codex-Konfiguration

## Bearbeitete und erzeugte Dateien

| Datei | Zeilen | Zweck |
|---|---:|---|
| `~/claude-limits-tray/claude_limits_tray.py` | 904 | Tray-Item, Menü, Fenster |
| `~/claude-limits-tray/providers.py` | 692 | Claude/Codex-Abruf, Aktivitätserkennung, Auswahl, Cache |
| `~/claude-limits-tray/tests/` | 478 | Unit-Tests (Abruf, Token-Auswahl, Backoff, Auswahl, Icon) |
| `~/claude-limits-tray/README.md` | — | Installation, Bedienung, Datenquelle, Fehlerfälle |
| `~/claude-limits-tray/install.sh` / `uninstall.sh` | — | Symlink und Autostart einrichten bzw. entfernen |
| `~/claude-limits-tray/claude-limits-tray.desktop` | — | Autostart-Vorlage (wird von `install.sh` befüllt) |
| `~/.local/bin/claude-limits-tray` | — | Symlink auf das Skript (installiert) |
| `~/.config/autostart/claude-limits-tray.desktop` | — | Autostart-Eintrag (installiert) |
| `~/.cache/claude-limits-tray/usage.json` | — | Laufzeit-Cache der letzten Werte |

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
behoben. 39 Unit-Tests; live geprüft mit echten Daten
(Claude Pro, Codex A Free, Codex B Plus) und End-to-End mit simuliertem
Codex-Profil auf eigenem D-Bus.

## Nächste Schritte (optional)

1. Optionale Desktop-Benachrichtigung ab 80 % / 100 %
2. Kleine Verlaufsgrafik der letzten 24 h aus dem Cache
3. Paketierung als `.deb` oder systemd-User-Unit statt Autostart-Datei
