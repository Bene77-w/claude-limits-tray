# Entscheidungen usage-tray-cli

## Implementierungsentscheidungen

| Entscheidung/Annahme | Begründung | Bezug (Spec/Ticket/Datei) |
|---|---|---|
| `usage-tray` als Bash-Skript, PID über den D-Bus-Owner von `org.bene.ClaudeLimitsTray` (`GetConnectionUnixProcessID`) | `pkill -f` träfe auch Editoren o. Ä. mit dem Dateinamen; der D-Bus-Name ist eindeutig und existiert schon für die Einzelinstanz. Bash statt Python, damit `stop`/`status` kein GTK laden | `usage-tray` |
| `start` wartet bis 5 s auf den D-Bus-Namen und meldet Erfolg/Fehler; Ausgaben nach `~/.cache/claude-limits-tray/tray.log` | Rückmeldung im Terminal; Fehler sonst unsichtbar | `usage-tray` |
| Befehl `claude-limits-tray` und Autostart-Datei entfernt; `install.sh`/`uninstall.sh` räumen Reste alter Versionen weg | ein Einstiegspunkt; User will keinen Autostart | `install.sh`, `uninstall.sh`, gelöschte `claude-limits-tray.desktop` |
| Timer 1 s nur bei offenem Fenster, sonst 10 s (= `ACTIVITY_SCAN_SECONDS`); `open_window` schaltet sofort um | bei geschlossenem Fenster gibt es nichts Sekündliches zu aktualisieren; 10 s ist der kürzeste fachliche Takt (Aktivität/Codex-Live-Werte) | `claude_limits_tray.py` `_schedule`, `_periodic`, `open_window` |
| Im 10-s-Takt prüft jeder Aufruf die Aktivität, unabhängig vom gemessenen Abstand | `timeout_add_seconds` darf leicht früh auslösen; sonst würde die Prüfung jedes zweite Mal übersprungen (effektiv 20 s) | `_periodic` |
| `icon_pixmaps` mit `lru_cache(maxsize=8)` | Panel ruft `Get`/`GetAll` oft auf, und der Tooltip-Refresh zeichnete minütlich alle 4 Größen neu, obwohl sich das Symbol selten ändert | `icon_pixmaps` |
| Codex-Scan: alle 10 s nur die zwei neuesten Tagesordner plus zuletzt gefundene Datei, vollständig alle 300 s (`FULL_SCAN_SECONDS`); Claude-Scan unverändert | User-Entscheidung zu Review-Befund H1. Gemessen 8,3 → 2,8 ms je Scan bei gleichem Ergebnis (Codex A 5,5 → 0,8 ms, Codex B 1,2 → 0,4 ms; Claude 1,6 ms bleibt, Modellerkennung je Datei ist schon gecacht). Kosten (vollständig erst nach Runde 2, Befund H2): Wird eine ältere Codex-Sitzung fortgesetzt, erkennt die App deren Aktivität **und Live-Werte** bis zu 5 min später; bis dahin gilt der 5-min-Abruf (bei 429-Pause bis zu 30 min alt). User hat nach Vorlage der Nachteile aller drei Optionen (akzeptieren / Lücke über `history.jsonl` schließen / zurücknehmen) „So akzeptieren“ gewählt. „Neueste zwei“ statt Datumsrechnung, damit keine Zeitzonenannahme nötig ist | `providers.py` `CodexAccount.scan_activity` |
| Zeichnen nicht von PIL auf cairo umgestellt | spart nur ~4 MB bei weiter geladenem GTK; der Cache oben deckt den Zeichenaufwand ab | `render_icon` |

## Reviewbefunde

| Befund und Auswirkung | Schweregrad laut Prüfer | Prüfer, Review, Runde | Bezug (Spec/Ticket/`datei:zeile`) | Umgang | Begründung/Belege |
|---|---|---|---|---|---|
| N2: `prog="claude-limits-tray"` in `--help` veraltet | NIEDRIG | Generalist, Final Review, Runde 1 | `claude_limits_tray.py:860` | sachlich widerlegt | `usage-tray` reicht keine Argumente an das Skript weiter; `--help` sieht nur, wer `claude_limits_tray.py` direkt aufruft, und das heißt weiterhin so (= `APP_ID`) |
| N3: Timer-Protokoll (`return not self._schedule()`, Entfernen in `open_window`) schwer lesbar | NIEDRIG | Generalist, Final Review, Runde 1 | `claude_limits_tray.py` `_schedule`/`_periodic`/`open_window` | akzeptiertes Risiko | Prüfer bestätigt korrektes Verhalten; Docstring von `_schedule` beschreibt den Vertrag, neuer `TimerTest` sichert ihn ab |
| N4: Tooltip-Countdown kann bis ~70 s hinterherlaufen | NIEDRIG | Generalist, Final Review, Runde 1 | `_periodic` | akzeptiertes Risiko | Tooltip zeigt Minuten-/Stundenauflösung; 10 s zusätzliche Verzögerung ist nicht sichtbar relevant |
| M1 (Rest): `open_window` und `_periodic` ohne Unit-Test; abgedeckt ist nur `_schedule` | MITTEL | Generalist, Final Review, Runde 2 | `claude_limits_tray.py` `open_window`, `_periodic` | akzeptiertes Risiko | Beide brauchen ein GTK-Fenster; Umschalten manuell geprüft (Fenster per D-Bus geöffnet, keine GLib-Warnungen), Runde 1 hat alle Hide-Pfade und das Fehlen doppelter Timer im Code bestätigt; der Vertrag von `_schedule`, auf dem beide beruhen, ist getestet |
| N5: Intervall des Vollscans mit `time.time()` statt `time.monotonic()` | NIEDRIG | Generalist, Final Review, Runde 2 | `providers.py` `CodexAccount.scan_activity` | akzeptiertes Risiko | Nur eine rückwärts gestellte Systemuhr verschiebt den nächsten Vollscan, das heilt sich selbst; `time.time()` ist UTC, Sommerzeit wirkt nicht; einheitlich mit den übrigen Zeitvergleichen der App |
| N6: Liste der Tagesordner wächst linear mit dem Archiv | NIEDRIG | Generalist, Final Review, Runde 2 | `providers.py` `CodexAccount.scan_activity` | akzeptiertes Risiko | ~365 Verzeichniseinträge pro Jahr ohne `stat` je Datei; gemessen 0,84 ms inkl. Dateiprüfung bei 66 Ordnern |
