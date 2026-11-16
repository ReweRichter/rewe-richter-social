# rewe-richter-social

Automatische Veröffentlichung auf Instagram (@rewerichter) für den REWE Markt Richter.

- `queue/<id>/`: geplante Beiträge (item.json + Bild/Video), werden über GitHub Pages für Instagram bereitgestellt
- `state/published.json`: was wann veröffentlicht wurde
- Samstags ab 22 Uhr (Ende der Angebotswoche) werden die Angebote (`angebot_bis`) auf Instagram gelöscht; Storys sind dann schon von selbst weg und werden nur vermerkt. Angebots-Storys nach Freitag 20 Uhr werden nicht veröffentlicht. Ihre Dateien werden danach aus `queue/` entfernt. Der Eintrag in `state/published.json` bleibt als Protokoll.
- `publisher/publish.py`: Veröffentlichungs-Skript (Instagram Graph API)
- `.github/workflows/publish.yml`: läuft alle 15 Minuten und veröffentlicht fällige Einträge

Der Zugangsschlüssel liegt ausschließlich als GitHub-Secret `IG_ACCESS_TOKEN` vor.
