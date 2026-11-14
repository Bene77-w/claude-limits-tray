# Reviews usage-tray-cli

## Final Review

- Anker: `b020e9a` · Endstand Runde 1: `e81e431`
- Besetzung: 1 Generalist (Final Review, ein Plan); keine Fachprüfer (kein Auslöser: kleiner Diff, Ressourcenwirkung vom Generalisten mitgeprüft)
- Releasezusatz: gilt nicht (Repo `PRIVATE`, keine Festlegung auf Öffentlichkeit)

### Runde 1

| Rolle | Modell/Effort | Verdikt | Status |
|---|---|---|---|
| Generalist | Haiku 5.5 / high | mit Auflagen abgenommen | aktiv (Codekorrektur zu H1) |

| Befund | Schweregrad laut Prüfer | Umgang |
|---|---|---|
| H1 Dateiscan entgegen Nutzervorgabe nicht optimiert; Messangabe „< 1 ms“ falsch | HOCH | behoben: Messung korrigiert, User wählte „Optimieren“ → Codex-Schnellscan (8,3 → 2,8 ms) |
| M1 Timer-Umschaltung ohne Test | MITTEL | teilweise behoben: `TimerTest` für `_schedule`; Rest erfasst in Runde 2 |
| M2 `gdbus` als Abhängigkeit nicht geprüft/dokumentiert | MITTEL | behoben: Prüfung in `install.sh`, README-Abhängigkeiten |
| M3 Ressourcen-Nachweis nicht vergleichbar | MITTEL | behoben: A/B-Messung alte vs. neue Version, je 15 s Anlauf + 120 s, Fenster zu: Kontextwechsel 126 → 23, CPU-Ticks 16 → 14, RSS 60,8 → 61,0 MB. Die frühere Aussage 76 → 62 MB stammte vom Neustart und ist aus PR und PITCH entfernt |
| N1 Review-Anker fehlt in `auftrag.md` | NIEDRIG | behoben |
| N2 `prog`-Name in `--help` | NIEDRIG | erfasst (→ `entscheidungen.md`, sachlich widerlegt) |
| N3 Lesbarkeit Timer-Protokoll | NIEDRIG | erfasst (→ `entscheidungen.md`, akzeptiertes Risiko) |
| N4 Tooltip bis ~70 s verzögert | NIEDRIG | erfasst (→ `entscheidungen.md`, akzeptiertes Risiko) |

Korrekturen M1, M2, M3 und N1 betreffen nur Tests, Installationsprüfung und Doku; die Codekorrektur zu H1 ist nicht trivial → Runde 2.

### Runde 2

- Endstand: `6e608a0` (Korrekturdiff `e81e431..6e608a0`)

| Rolle | Modell/Effort | Verdikt | Status |
|---|---|---|---|
| Generalist (neuer Subagent) | Haiku 5.5 / high | mit Auflagen abgenommen | abgeschlossen |

| Befund | Schweregrad laut Prüfer | Umgang |
|---|---|---|
| H2 Schnellscan verzögert auch Live-Werte, Kostenangabe an User unvollständig, README widersprüchlich | HOCH | behoben: vollständige Nachteile aller Optionen vorgelegt, User wählte „So akzeptieren“ (→ `entscheidungen.md`); README korrigiert; Test für `live_limits` im Schnellmodus ergänzt |
| M1 (erneut) `open_window`/`_periodic` ungetestet | MITTEL | erfasst (→ `entscheidungen.md`, akzeptiertes Risiko) |
| M3 (erneut) Messung nicht auf Endstand | MITTEL | behoben: Endstand `6e608a0` 120 s gemessen: Kontextwechsel 33, CPU-Ticks 10, RSS 61,3 MB (vorher 126 / 16 / 60,8) |
| N5 `time.time()` statt `monotonic` | NIEDRIG | erfasst (→ `entscheidungen.md`, akzeptiertes Risiko) |
| N6 Tagesordner-Liste wächst | NIEDRIG | erfasst (→ `entscheidungen.md`, akzeptiertes Risiko) |
| N7 überlange README-Zeile | NIEDRIG | behoben |

Stoppregel erfüllt: In Runde 2 wurde kein Produktcode geändert (nur README, Doku und ein Test), alle Befunde sind behoben oder erfasst. Prüfer abgeschlossen.

**Ergebnis Final Review: abgenommen.** 44 Unit-Tests grün.
