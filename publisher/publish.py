"""Veröffentlicht fällige Beiträge aus queue/ auf Instagram (@rewerichter).

Läuft in GitHub Actions (siehe .github/workflows/publish.yml), nur mit der
Python-Standardbibliothek.

  python publisher/publish.py check        # Verbindung prüfen, nichts posten
  python publisher/publish.py run          # alle fälligen Einträge veröffentlichen
  python publisher/publish.py run <id>     # genau diesen Eintrag sofort veröffentlichen

Ein Eintrag ist ein Ordner queue/<id>/ mit item.json und der Mediendatei:

  {
    "typ": "post" | "story" | "reel" | "carousel",
    "zeit": "2026-11-12T07:30:00+01:00",   # frühester Veröffentlichungszeitpunkt
    "datei": "bild.jpg",                    # .jpg für post/story, .mp4 für reel/story
    "dateien": ["01.jpg", "02.jpg"],        # nur carousel: 2-10 Bilder (statt "datei")
    "caption": "Text mit #Hashtags",        # nicht bei story
    "cover": "cover.jpg",                   # optional, nur reel
    "location_id": "…",                     # optional; sonst IG_LOCATION_ID (nur post/reel)
    "angebot_bis": "2026-11-15"             # nur Angebote: Samstag 22 Uhr der Angebotswoche auf Instagram und in queue/ gelöscht
  }

Instagram holt die Datei selbst über GitHub Pages ab (MEDIA_BASE_URL).
Der Status landet in state/published.json und wird vom Workflow committet.
"""
import datetime as dt
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

API = "https://graph.facebook.com/v25.0"
ROOT = Path(__file__).resolve().parent.parent
QUEUE = ROOT / "queue"
STATE = ROOT / "state" / "published.json"
MAX_VERSPAETUNG = dt.timedelta(hours=6)   # ältere Einträge werden übersprungen, nicht nachgeholt
MAX_VERSUCHE = 3
class _MitteleuropaeischeZeit(dt.tzinfo):
    """Ersatz, wenn die Zeitzonen-Datenbank fehlt (Windows ohne tzdata): MEZ/MESZ nach EU-Regel,
    Sommerzeit vom letzten Sonntag im März bis zum letzten Sonntag im Oktober (je 1 Uhr UTC)."""

    @staticmethod
    def _letzter_sonntag(jahr, monat):
        d = dt.date(jahr, monat, 31)
        return d - dt.timedelta(days=(d.weekday() + 1) % 7)

    def _sommer(self, utc):
        beginn = dt.datetime.combine(self._letzter_sonntag(utc.year, 3), dt.time(1))
        ende = dt.datetime.combine(self._letzter_sonntag(utc.year, 10), dt.time(1))
        return beginn <= utc < ende

    def utcoffset(self, d):
        return dt.timedelta(hours=2 if self._sommer(d.replace(tzinfo=None) - dt.timedelta(hours=1)) else 1)

    def dst(self, d):
        return self.utcoffset(d) - dt.timedelta(hours=1)

    def fromutc(self, d):
        utc = d.replace(tzinfo=None)
        return (utc + dt.timedelta(hours=2 if self._sommer(utc) else 1)).replace(tzinfo=self)

    def tzname(self, d):
        return "MESZ" if self.dst(d) else "MEZ"


try:
    BERLIN = ZoneInfo("Europe/Berlin")
except Exception:  # ZoneInfoNotFoundError
    BERLIN = _MitteleuropaeischeZeit()
AUFRAEUMEN_UHR = 22    # Samstag ab 22 Uhr: Angebote der Woche löschen, der Sonntag bleibt frei von alter Werbung
STORY_SCHLUSS_UHR = 20  # letzte Angebots-Story spätestens Freitag 20 Uhr (nach 24 h vor dem Aufräumen weg)
STORY_LEBENSDAUER = dt.timedelta(hours=24)

TOKEN = os.environ.get("IG_ACCESS_TOKEN", "")
IG_ID = os.environ.get("IG_USER_ID", "")
MEDIA_BASE = os.environ.get("MEDIA_BASE_URL", "").rstrip("/")
# Facebook-Ortsseite des Marktes (Instagram-Standort). Gilt für Posts und Reels, nicht für Storys.
LOCATION_ID = os.environ.get("IG_LOCATION_ID", "").strip()


class ApiError(RuntimeError):
    def __init__(self, msg, code=None):
        super().__init__(msg)
        self.code = code


def samstag_der_woche(angebot_bis):
    """Samstag der Angebotswoche: das Ende selbst, bei Sonntag der Vortag, sonst der folgende Samstag."""
    d = dt.date.fromisoformat(angebot_bis)
    return d - dt.timedelta(days=1) if d.weekday() == 6 else d + dt.timedelta(days=5 - d.weekday())


def aufraeum_zeit(angebot_bis):
    """Samstag 22 Uhr der Angebotswoche (Berliner Zeit): ab dann wird das Angebot gelöscht."""
    return dt.datetime.combine(samstag_der_woche(angebot_bis), dt.time(AUFRAEUMEN_UHR), BERLIN)


def story_schluss(angebot_bis):
    """Freitag 20 Uhr der Angebotswoche: spätester Zeitpunkt für eine Angebots-Story."""
    sa = samstag_der_woche(angebot_bis)
    return dt.datetime.combine(sa - dt.timedelta(days=1), dt.time(STORY_SCHLUSS_UHR), BERLIN)


def zu_spaet_fuer_angebot(item):
    """Grund, warum ein Angebots-Eintrag nicht mehr veröffentlicht werden darf, sonst None."""
    bis = item.get("angebot_bis")
    if not bis:
        return None
    zeit = dt.datetime.fromisoformat(item["zeit"])
    if item["typ"] == "story" and zeit > story_schluss(bis):
        return "Angebots-Story nach Freitag 20 Uhr (wäre am Samstagabend noch sichtbar)"
    if zeit >= aufraeum_zeit(bis):
        return "Angebot nach dem Aufräumen am Samstag 22 Uhr"
    return None


def call(method, path, **params):
    params["access_token"] = TOKEN
    query = urllib.parse.urlencode(params)
    if method == "GET":
        req = urllib.request.Request(f"{API}/{path}?{query}")
    else:
        req = urllib.request.Request(f"{API}/{path}", data=query.encode(), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        err = {}
        try:
            err = json.loads(body).get("error", {})
            msg = f"{err.get('type')} {err.get('code')}/{err.get('error_subcode')}: {err.get('message')}"
        except ValueError:
            msg = body[:300]
        raise ApiError(f"HTTP {e.code} bei {path}: {msg}", code=err.get("code")) from None


def url_erreichbar(url):
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status == 200
    except urllib.error.URLError:
        return False


def warte_auf_container(cid, max_sek=600):
    """Videos werden von Instagram verarbeitet; erst bei FINISHED darf veröffentlicht werden."""
    ende = time.time() + max_sek
    while time.time() < ende:
        status = call("GET", cid, fields="status_code,status").get("status_code")
        if status == "FINISHED":
            return
        if status in ("ERROR", "EXPIRED"):
            info = call("GET", cid, fields="status")
            raise ApiError(f"Container {status}: {info.get('status')}")
        time.sleep(10)
    raise ApiError("Zeitüberschreitung bei der Verarbeitung durch Instagram")


def media_url(item_id, datei):
    url = f"{MEDIA_BASE}/queue/{item_id}/{urllib.parse.quote(datei)}"
    if not url_erreichbar(url):
        raise ApiError(f"Mediendatei noch nicht öffentlich erreichbar: {url}")
    return url


def veroeffentliche_karussell(item_id, item):
    """Karussell: erst je Bild einen Kind-Container, dann den Karussell-Container veröffentlichen."""
    dateien = item["dateien"]
    if not 2 <= len(dateien) <= 10:
        raise ApiError(f"Karussell braucht 2-10 Bilder, hat {len(dateien)}")
    urls = [media_url(item_id, d) for d in dateien]  # alle prüfen, bevor etwas angelegt wird
    kinder = []
    for url in urls:
        kid = call("POST", f"{IG_ID}/media", image_url=url, is_carousel_item="true")["id"]
        warte_auf_container(kid)
        kinder.append(kid)
    params = {"media_type": "CAROUSEL", "children": ",".join(kinder), "caption": item.get("caption", "")}
    standort = item.get("location_id") or LOCATION_ID
    if standort:
        params["location_id"] = standort
    cid = call("POST", f"{IG_ID}/media", **params)["id"]
    warte_auf_container(cid)
    return call("POST", f"{IG_ID}/media_publish", creation_id=cid)["id"]


def veroeffentliche(item_id, item):
    typ = item["typ"]
    if typ == "carousel":
        media_id = veroeffentliche_karussell(item_id, item)
        try:
            link = call("GET", media_id, fields="permalink").get("permalink")
        except ApiError:
            link = None
        return media_id, link
    datei_url = f"{MEDIA_BASE}/queue/{item_id}/{urllib.parse.quote(item['datei'])}"
    if not url_erreichbar(datei_url):
        raise ApiError(f"Mediendatei noch nicht öffentlich erreichbar: {datei_url}")
    ist_video = item["datei"].lower().endswith((".mp4", ".mov"))

    params = {}
    if typ == "post":
        params = {"image_url": datei_url, "caption": item.get("caption", "")}
    elif typ == "story":
        params = {"media_type": "STORIES", ("video_url" if ist_video else "image_url"): datei_url}
    elif typ == "reel":
        params = {"media_type": "REELS", "video_url": datei_url,
                  "caption": item.get("caption", ""), "share_to_feed": "true"}
        if item.get("cover"):
            params["cover_url"] = f"{MEDIA_BASE}/queue/{item_id}/{urllib.parse.quote(item['cover'])}"
    else:
        raise ApiError(f"Unbekannter Typ {typ!r}")
    standort = item.get("location_id") or LOCATION_ID
    if standort and typ in ("post", "reel"):
        params["location_id"] = standort

    cid = call("POST", f"{IG_ID}/media", **params)["id"]
    warte_auf_container(cid)
    media_id = call("POST", f"{IG_ID}/media_publish", creation_id=cid)["id"]
    try:
        link = call("GET", media_id, fields="permalink").get("permalink")
    except ApiError:
        link = None
    return media_id, link


def loesche(media_id):
    """Löscht ein Instagram-Medium (Beitrag, Reel oder Story). Braucht instagram_manage_contents."""
    params = {"access_token": TOKEN}
    req = urllib.request.Request(f"{API}/{media_id}?{urllib.parse.urlencode(params)}", method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r).get("success", True)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        err = {}
        try:
            err = json.loads(body).get("error", {})
            msg = f"{err.get('type')} {err.get('code')}/{err.get('error_subcode')}: {err.get('message')}"
        except ValueError:
            msg = body[:300]
        raise ApiError(f"HTTP {e.code} beim Löschen: {msg}", code=err.get("code")) from None


def noch_vorhanden(media_id):
    """False, wenn Instagram das Medium nicht mehr kennt (z. B. von Hand gelöscht)."""
    try:
        call("GET", media_id, fields="id")
        return True
    except ApiError as e:
        if e.code == 100:
            return False
        raise


def aufraeumen(erzwingen=False):
    """Regel des Inhabers: Angebots-Beiträge, -Reels und -Storys werden am Samstag ab 22 Uhr
    am Ende der Angebotswoche gelöscht, damit am Sonntag keine abgelaufenen Preise zu sehen sind.
    Gelöscht wird NUR, was dieses System selbst veröffentlicht hat und ein angebot_bis trägt,
    sobald der Samstag 22 Uhr dieser Angebotswoche erreicht ist (holt verpasste Läufe nach).
    erzwingen (Modus aufraeumen): zusätzlich alles mit angebot_bis heute oder früher, sofort.
    Storys verschwinden nach 24 h von selbst; sie werden nur noch als abgelaufen vermerkt.
    Alles andere bleibt unangetastet."""
    jetzt = dt.datetime.now(BERLIN)
    state = lade_state()
    heute = jetzt.date()
    fehler = False
    for item_id, eintrag in sorted(state.items()):
        if eintrag.get("status") != "veröffentlicht" or not eintrag.get("angebot_bis"):
            continue
        faellig = jetzt >= aufraeum_zeit(eintrag["angebot_bis"])
        if erzwingen:
            faellig = faellig or dt.date.fromisoformat(eintrag["angebot_bis"]) <= heute
        if not faellig or eintrag.get("loeschversuche", 0) >= MAX_VERSUCHE:
            continue
        veroeffentlicht = dt.datetime.fromisoformat(eintrag["am"])
        if eintrag.get("typ") == "story" and jetzt - veroeffentlicht >= STORY_LEBENSDAUER:
            eintrag.update(status="abgelaufen", abgelaufen_am=(veroeffentlicht + STORY_LEBENSDAUER).isoformat(timespec="seconds"))
            print(f"{item_id}: Story nach 24 h von selbst verschwunden, nichts zu löschen")
            speichere_state(state)
            continue
        try:
            loesche(eintrag["media_id"])
            eintrag.update(status="gelöscht", geloescht_am=jetzt.isoformat(timespec="seconds"))
            print(f"{item_id}: gelöscht (Angebot bis {eintrag['angebot_bis']})")
        except ApiError as e:
            try:
                weg = not noch_vorhanden(eintrag["media_id"])
            except ApiError:
                weg = False
            if weg:
                eintrag.update(status="nicht mehr vorhanden", geprueft_am=jetzt.isoformat(timespec="seconds"))
                print(f"{item_id}: war auf Instagram schon gelöscht (z. B. von Hand)")
            else:
                fehler = True
                eintrag["loeschfehler"] = str(e)
                eintrag["loeschversuche"] = eintrag.get("loeschversuche", 0) + 1
                print(f"{item_id}: Löschen FEHLGESCHLAGEN: {e}")
        speichere_state(state)
    dateien_aufraeumen(state, jetzt, erzwingen)
    return 1 if fehler else 0


def dateien_aufraeumen(state, jetzt, erzwingen=False):
    """Entfernt die Dateien abgelaufener Angebote aus queue/ (Bild/Video werden danach nicht mehr
    gebraucht). Nur Einträge, deren Aufräumzeit (Samstag 22 Uhr) erreicht ist und die auf Instagram
    gelöscht, abgelaufen, übersprungen oder nie veröffentlicht wurden. Veröffentlichte, deren Löschen
    noch aussteht, bleiben liegen. Der Eintrag in state/published.json bleibt als Protokoll erhalten."""
    heute = jetzt.date()
    for ordner in sorted(p for p in QUEUE.iterdir() if p.is_dir()) if QUEUE.exists() else []:
        try:
            item = json.loads((ordner / "item.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        bis = item.get("angebot_bis")
        if not bis:
            continue
        if jetzt < aufraeum_zeit(bis) and not (erzwingen and dt.date.fromisoformat(bis) <= heute):
            continue
        status = state.get(ordner.name, {}).get("status")
        if status == "veröffentlicht":
            continue
        shutil.rmtree(ordner)
        if ordner.name in state:
            state[ordner.name]["dateien_entfernt"] = heute.isoformat()
        print(f"{ordner.name}: Dateien aus queue/ entfernt (Angebot bis {bis}, Status {status or 'nie veröffentlicht'})")
    speichere_state(state)


def lade_state():
    return json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}


def speichere_state(state):
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def check():
    info = call("GET", IG_ID, fields="username,name,followers_count,media_count")
    print(f"Verbindung OK: @{info.get('username')} ({info.get('followers_count')} Follower, "
          f"{info.get('media_count')} Beiträge)")
    try:
        perms = call("GET", "me/permissions").get("data", [])
        erteilt = sorted(p["permission"] for p in perms if p.get("status") == "granted")
        print(f"Berechtigungen des Schlüssels: {', '.join(erteilt)}")
        print("Löschen am Samstagabend: " + ("möglich" if "instagram_manage_contents" in erteilt
                                         else "NICHT möglich – instagram_manage_contents fehlt"))
    except ApiError as e:
        print(f"Berechtigungen nicht abrufbar: {e}")
    try:
        limit = call("GET", f"{IG_ID}/content_publishing_limit", fields="quota_usage,config")
        print(f"Veröffentlichungs-Kontingent: {json.dumps(limit.get('data'))}")
    except ApiError as e:
        print(f"Kontingent nicht abrufbar: {e}")


def standort_test(location_id):
    """Legt einen UNVERÖFFENTLICHTEN Bild-Container mit Standort an (verfällt nach 24 h).
    Es wird niemals media_publish aufgerufen."""
    bild = f"{MEDIA_BASE}/queue/2026-11-12-1000-post-knorr-fix/bild.jpg"
    try:
        cid = call("POST", f"{IG_ID}/media", image_url=bild, location_id=location_id, caption="Standort-Test")["id"]
        print(f"Standort {location_id}: OK – Instagram akzeptiert die Ortsmarkierung (Test-Container {cid}, nicht veröffentlicht)")
        return 0
    except ApiError as e:
        print(f"Standort {location_id}: abgelehnt – {e}")
        return 1


def run(nur_id=None):
    state = lade_state()
    jetzt = dt.datetime.now(dt.timezone.utc)
    fehler = False
    for ordner in sorted(p for p in QUEUE.iterdir() if p.is_dir()) if QUEUE.exists() else []:
        item_id = ordner.name
        if nur_id and item_id != nur_id:
            continue
        eintrag = state.get(item_id, {})
        if eintrag.get("status") in ("veröffentlicht", "übersprungen"):
            continue
        item = json.loads((ordner / "item.json").read_text(encoding="utf-8"))
        zeit = dt.datetime.fromisoformat(item["zeit"])
        if not nur_id:
            if zeit > jetzt:
                continue
            if jetzt - zeit > MAX_VERSPAETUNG:
                state[item_id] = {"status": "übersprungen", "grund": "zu spät (Ausfall?)",
                                  "am": jetzt.isoformat(timespec="seconds")}
                print(f"{item_id}: übersprungen, Zeitpunkt lag mehr als 6 h zurück")
                continue
        # Regel des Inhabers (auch beim manuellen Veröffentlichen): keine Angebots-Story nach
        # Freitag 20 Uhr (geplante Zeit), kein Angebot mehr, sobald das Aufräumen am Samstag
        # 22 Uhr erreicht ist (tatsächliche Zeit, auch bei verspäteten Läufen).
        sperre = zu_spaet_fuer_angebot(item)
        if not sperre and item.get("angebot_bis") and jetzt >= aufraeum_zeit(item["angebot_bis"]):
            sperre = "Angebot nach dem Aufräumen am Samstag 22 Uhr"
        if sperre:
            state[item_id] = {"status": "übersprungen", "grund": sperre,
                              "am": jetzt.isoformat(timespec="seconds")}
            print(f"{item_id}: übersprungen, {sperre}")
            speichere_state(state)
            continue
        if eintrag.get("versuche", 0) >= MAX_VERSUCHE:
            continue
        try:
            media_id, link = veroeffentliche(item_id, item)
            state[item_id] = {"status": "veröffentlicht", "typ": item["typ"], "media_id": media_id,
                              "link": link, "am": jetzt.isoformat(timespec="seconds")}
            if item.get("angebot_bis"):
                state[item_id]["angebot_bis"] = item["angebot_bis"]
            print(f"{item_id}: veröffentlicht ({item['typ']}) {link or media_id}")
        except ApiError as e:
            fehler = True
            versuche = eintrag.get("versuche", 0) + 1
            state[item_id] = {"status": "fehler", "versuche": versuche, "fehler": str(e),
                              "am": jetzt.isoformat(timespec="seconds")}
            print(f"{item_id}: FEHLER (Versuch {versuche}/{MAX_VERSUCHE}): {e}")
        speichere_state(state)
    speichere_state(state)
    return 1 if fehler else 0


def main():
    if not TOKEN or not IG_ID:
        sys.exit("IG_ACCESS_TOKEN oder IG_USER_ID fehlt.")
    modus = sys.argv[1] if len(sys.argv) > 1 else "run"
    if modus == "check":
        check()
        return 0
    if modus == "standort":
        return standort_test(sys.argv[2])
    if modus == "run":
        rc = run(sys.argv[2] if len(sys.argv) > 2 else None)
        return max(rc, aufraeumen())
    if modus == "aufraeumen":  # manuell, sofort (unabhängig vom Wochentag)
        return aufraeumen(erzwingen=True)
    sys.exit(f"Unbekannter Modus {modus!r}")


if __name__ == "__main__":
    sys.exit(main())
