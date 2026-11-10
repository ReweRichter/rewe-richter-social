# rewe-richter-social

Automatische Veröffentlichung auf Instagram (@rewerichter) für den REWE Markt Richter.

- `queue/<id>/`: geplante Beiträge (item.json + Bild/Video), werden über GitHub Pages für Instagram bereitgestellt
- `state/published.json`: was wann veröffentlicht wurde
- `publisher/publish.py`: Veröffentlichungs-Skript (Instagram Graph API)
- `.github/workflows/publish.yml`: läuft alle 15 Minuten und veröffentlicht fällige Einträge

Der Zugangsschlüssel liegt ausschließlich als GitHub-Secret `IG_ACCESS_TOKEN` vor.
