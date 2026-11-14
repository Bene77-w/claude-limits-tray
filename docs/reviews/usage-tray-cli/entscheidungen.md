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
| Dateiscan **nicht** optimiert, obwohl in der gewählten Option genannt | gemessen < 1 ms für ~560 Dateien je 10 s; eine Einschränkung auf neueste Tagesordner könnte Aktivität in fortgesetzten alten Codex-Sitzungen übersehen | `providers.py` `scan_activity` |
| Zeichnen nicht von PIL auf cairo umgestellt | spart nur ~4 MB bei weiter geladenem GTK; der Cache oben deckt den Zeichenaufwand ab | `render_icon` |

## Reviewbefunde

| Befund und Auswirkung | Schweregrad laut Prüfer | Prüfer, Review, Runde | Bezug (Spec/Ticket/`datei:zeile`) | Umgang | Begründung/Belege |
|---|---|---|---|---|---|
