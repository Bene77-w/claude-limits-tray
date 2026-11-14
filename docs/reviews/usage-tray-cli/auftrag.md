# Auftrag usage-tray-cli (Planquelle)

Vom User in der Sitzung am 14.11.2026 vorgegeben:

- Ziel des Trays: Abo-Usages schnell sehen.
- „ich möchte das Tray zukünftig mit start/stop usage tray im Terminal starten und beenden“
- „können wir es ressourcenschonender designen?“

Entscheidungen des Users auf Rückfrage:

- Befehl: `usage-tray start|stop|status`, **kein Autostart** mehr.
- Fenster: **unverändert** (GTK-Fenster mit Balken bleibt). Gewählte Option lautete:
  „Nur Timer, Dateiscan und Zeichnen werden optimiert; der Speicher bleibt bei ca. 70 MB.“
