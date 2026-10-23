# Claude Limits Tray — Pitch

## Problem

Wer sein Claude-Abonnement intensiv nutzt, muss für die Frage „Wie viel ist noch übrig?"
regelmäßig `claude.ai/settings/usage` im Browser öffnen. Das unterbricht den Arbeitsfluss
und passiert meist erst dann, wenn ein Limit schon greift.

## Lösung

Ein Tray-Symbol in der Cinnamon-Leiste, das den Verbrauch permanent sichtbar macht:
Der Ring zeigt den höchsten ausgeschöpften Wert, ein Linksklick öffnet ein kleines
Fenster mit 5-Stunden- und Wochen-Limit, Prozentwert, Reset-Countdown und Reset-Uhrzeit.
Kein Browser, kein Kontextwechsel.
Seit Version 1.2 auch für alle Codex-Profile (ChatGPT-Abos): Das Symbol zeigt
mit Buchstabe (C/A/B) das gerade benutzte Abo, bei mehreren das am stärksten
ausgelastete.

## Funktionsumfang (Scope)

**Enthalten**

- Tray-Symbol als StatusNotifierItem mit Ringdiagramm (grün → gelb → orange → rot)
- Tooltip mit den aktuellen Prozentwerten ohne Klick
- Popup-Fenster: Balken je Limit, Prozent, „Zurückgesetzt in …" plus Datum/Uhrzeit
- Linksklick öffnet, Mittelklick aktualisiert, Rechtsklick zeigt Menü
- Menü: Fenster öffnen, Aktualisieren, Beenden
- Automatische Aktualisierung: 60 s bei offenem Fenster, sonst 5 min
- Cache in `~/.cache/claude-limits-tray/usage.json` — Werte sind nach Neustart sofort da
- Fehlerbehandlung: 401/403 (Token abgelaufen) und 429 (Drosselung, `Retry-After` wird beachtet)
- Einzelinstanz: ein zweiter Start öffnet nur das bestehende Fenster
- Autostart beim Login, `--demo` und `--print` als Test-/Diagnosewege

**Nicht enthalten**

- Windows/macOS (reine Linux-/X11-Umsetzung, getestet unter Cinnamon)
- Eigene Token-Erneuerung — abgelaufene Tokens erneuert Claude Code selbst
- Verlaufsdiagramme, Benachrichtigungen bei Schwellwerten, mehrere Accounts
- Schreiben von Einstellungen in die Claude-Konfiguration

## Bearbeitete und erzeugte Dateien

| Datei | Zeilen | Zweck |
|---|---:|---|
| `~/claude-limits-tray/claude_limits_tray.py` | 883 | Tray-Item, Menü, Fenster |
| `~/claude-limits-tray/providers.py` | 642 | Claude/Codex-Abruf, Aktivitätserkennung, Auswahl, Cache |
| `~/claude-limits-tray/tests/` | 434 | Unit-Tests (Abruf, Token-Auswahl, Backoff, Auswahl, Icon) |
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
- Datenquelle: `GET https://api.anthropic.com/api/oauth/usage` — derselbe Endpoint,
  den `/usage` in Claude Code nutzt; OAuth-Token aus `~/.claude/.credentials.json`
  (`claudeAiOauth`) oder `CLAUDE_CODE_OAUTH_TOKEN`; andere Tokens werden nie gesendet
- Der Token bleibt im Speicher und wird weder geloggt noch angezeigt
- Abruf läuft in einem Hintergrund-Thread, die Oberfläche bleibt bedienbar
- Fenster ist rahmenlos, abgerundet, öffnet neben dem Mauszeiger innerhalb der
  Arbeitsfläche (also nie hinter der Leiste)

## Status

23.10.2026: Prototyp überarbeitet (Token-Wahl, Prozent-Umrechnung, 429-Backoff,
Panel-Neustart, Fokus) und um Codex-Abos erweitert (Branch `feature/codex-abos`,
Review-Anker `c492ca5`). Das Symbol zeigt das gerade benutzte Abo; bei mehreren
das mit dem höchsten Verbrauch. 35 Unit-Tests; live geprüft mit echten Daten
(Claude Pro, Codex A Free, Codex B Plus) und End-to-End mit simuliertem
Codex-Profil auf eigenem D-Bus.

## Nächste Schritte (optional)

1. Optionale Desktop-Benachrichtigung ab 80 % / 100 %
2. Kleine Verlaufsgrafik der letzten 24 h aus dem Cache
3. Paketierung als `.deb` oder systemd-User-Unit statt Autostart-Datei
