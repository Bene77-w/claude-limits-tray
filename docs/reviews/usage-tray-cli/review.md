# Reviews usage-tray-cli

## Final Review

- Anker: `b020e9a` · Endstand Runde 1: `e81e431`
- Besetzung: 1 Generalist (Final Review, ein Plan); keine Fachprüfer (kein Auslöser: kleiner Diff, Ressourcenwirkung vom Generalisten mitgeprüft)
- Releasezusatz: gilt nicht (Repo `PRIVATE`, keine Festlegung auf Öffentlichkeit)

### Runde 1

| Rolle | Modell/Effort | Verdikt | Status |
|---|---|---|---|
| Generalist | Haiku 5.5 / high | mit Auflagen abgenommen | aktiv – wartet auf User-Entscheidung zu H1 |

| Befund | Schweregrad laut Prüfer | Umgang |
|---|---|---|
| H1 Dateiscan entgegen Nutzervorgabe nicht optimiert; Messangabe „< 1 ms“ falsch | HOCH | Messung korrigiert (~8 ms je Scan) in `entscheidungen.md`; Optimierung dem User zur Entscheidung vorgelegt |
| M1 Timer-Umschaltung ohne Test | MITTEL | behoben: `TimerTest` in `tests/test_tray.py` |
| M2 `gdbus` als Abhängigkeit nicht geprüft/dokumentiert | MITTEL | behoben: Prüfung in `install.sh`, README-Abhängigkeiten |
| M3 Ressourcen-Nachweis nicht vergleichbar | MITTEL | behoben: A/B-Messung alte vs. neue Version, je 15 s Anlauf + 120 s, Fenster zu: Kontextwechsel 126 → 23, CPU-Ticks 16 → 14, RSS 60,8 → 61,0 MB. Die frühere Aussage 76 → 62 MB stammte vom Neustart und ist aus PR und PITCH entfernt |
| N1 Review-Anker fehlt in `auftrag.md` | NIEDRIG | behoben |
| N2 `prog`-Name in `--help` | NIEDRIG | erfasst (→ `entscheidungen.md`, sachlich widerlegt) |
| N3 Lesbarkeit Timer-Protokoll | NIEDRIG | erfasst (→ `entscheidungen.md`, akzeptiertes Risiko) |
| N4 Tooltip bis ~70 s verzögert | NIEDRIG | erfasst (→ `entscheidungen.md`, akzeptiertes Risiko) |

Korrekturen M1, M2 und N1 betreffen nur Tests, Installationsprüfung und Doku und sind trivial; H1 ist offen.
