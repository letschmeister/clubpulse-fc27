"""CLUBPULSE FC 27 — Webserver und Proxy fuer EA-Clubs-Statistiken.

Start lokal: python server.py
Cloud (Render): python server.py, HOST=0.0.0.0, PORT von Render
Keine Zusatzpakete erforderlich (Python 3.9+).
Hinweis: inoffizielle EA-Schnittstelle; kann jederzeit ausfallen.
"""

from collections import deque, OrderedDict
from concurrent.futures import ThreadPoolExecutor
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from time import monotonic, time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, parse_qs
from urllib.request import Request, urlopen
import json
import os
import socket

HERE = Path(__file__).resolve().parent
EA_BASE = "https://proclubs.ea.com/api/fc"
PLATFORM = "common-gen5"  # PS5 / Xbox Series / PC — FC27 nach Community-Test
CACHE_TTL = 120
CACHE = {}
CACHE_LOCK = Lock()
# Spieler können ausschließlich innerhalb bekannter Club-Kader gefunden werden.
# Der kleine, temporäre Index enthält nur öffentliche EA-Clubs-Daten.
KNOWN_ROSTERS = OrderedDict()
ROSTER_LOCK = Lock()
ROSTER_LIMIT = 50
ROSTER_TTL = 3600


def remember_roster(club_id, club_name, members):
    with ROSTER_LOCK:
        KNOWN_ROSTERS.pop(club_id, None)
        KNOWN_ROSTERS[club_id] = (monotonic(), club_name, members)
        while len(KNOWN_ROSTERS) > ROSTER_LIMIT:
            KNOWN_ROSTERS.popitem(last=False)


def known_rosters():
    with ROSTER_LOCK:
        return [(key, name, members) for key, (stored, name, members) in KNOWN_ROSTERS.items()
                if monotonic() - stored < ROSTER_TTL]


# Einfache globale Schutzbremse fuer die oeffentliche Test-Website.
# Verhindert extrem viele parallele Abfragen an die inoffizielle EA-API.
REQUEST_LOG = deque()
REQUEST_LOCK = Lock()
RATE_WINDOW = 60
RATE_MAX = 75


def request_allowed():
    now = monotonic()
    with REQUEST_LOCK:
        while REQUEST_LOG and now - REQUEST_LOG[0] > RATE_WINDOW:
            REQUEST_LOG.popleft()
        if len(REQUEST_LOG) >= RATE_MAX:
            return False
        REQUEST_LOG.append(now)
        return True

# Community-Dokumentation: https://github.com/1erkandogan/fc27-clubs-api
# Nicht-offizielle Schnittstelle. Kein Login, keine Cookies, keine Kontodaten.
EA_HEADERS = {
    "accept": "application/json",
    "accept-language": "en-US,en;q=0.9",
    "sec-ch-ua": '"Google Chrome";v="141", "Not?A_Brand";v="8", "Chromium";v="141"',
    "sec-fetch-site": "same-origin",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36",
}


class EAError(Exception):
    pass


def api_get(endpoint, **params):
    """Nur vordefinierte EA-Endpunkte; Cache verhindert unnötige Anfragen."""
    allowed = {
        "allTimeLeaderboard/search", "clubs/info", "clubs/overallStats",
        "members/stats", "members/career/stats", "clubs/matches"
    }
    if endpoint not in allowed:
        raise ValueError("Nicht erlaubter Endpunkt")
    url = f"{EA_BASE}/{endpoint}?{urlencode({'platform': PLATFORM, **params})}"
    with CACHE_LOCK:
        value = CACHE.get(url)
        if value and monotonic() - value[0] < CACHE_TTL:
            return value[1]
    try:
        with urlopen(Request(url, headers=EA_HEADERS), timeout=12) as response:
            result = json.load(response)
    except HTTPError as error:
        raise EAError(f"EA hat die Anfrage abgelehnt (HTTP {error.code}).") from None
    except (URLError, socket.timeout, TimeoutError, OSError):
        raise EAError("EA ist derzeit nicht erreichbar. Bitte später erneut versuchen.") from None
    except (ValueError, json.JSONDecodeError):
        raise EAError("EA hat unerwartete Daten gesendet.") from None
    if result is None:
        result = []
    with CACHE_LOCK:
        CACHE[url] = (monotonic(), result)
        # Speicherverbrauch bei vielen Suchbegriffen begrenzen.
        if len(CACHE) > 350:
            for old_key in list(CACHE)[:100]:
                CACHE.pop(old_key, None)
    return result


def do_search(name):
    name = name.strip()
    if len(name) < 2 or len(name) > 80:
        raise ValueError("Bitte mindestens 2 und höchstens 80 Zeichen eingeben.")
    raw = api_get("allTimeLeaderboard/search", clubName=name, maxResultCount=50)
    if not isinstance(raw, list):
        raise EAError("Das Suchergebnis von EA hat ein unbekanntes Format.")
    clubs = []
    for c in raw[:40]:
        if not isinstance(c, dict):
            continue
        info = c.get("clubInfo") or {}
        clubs.append({
            "id": str(c.get("clubId", "")),
            "name": str(info.get("name") or c.get("clubName") or "Unbekannter Club"),
            "division": c.get("currentDivision"),
            "games": c.get("gamesPlayed"),
            "wins": c.get("wins"),
            "goals": c.get("goals"),
        })
    return {"clubs": clubs, "updated": int(time())}


def do_club(club_id):
    club_id = club_id.strip()
    if not club_id.isdecimal() or not 1 <= len(club_id) <= 20:
        raise ValueError("Ungültige Club-ID.")
    jobs = {
        "info": ("clubs/info", {"clubIds": club_id}),
        "overall": ("clubs/overallStats", {"clubIds": club_id}),
        "members": ("members/stats", {"clubId": club_id}),
        "career": ("members/career/stats", {"clubId": club_id}),
        "matches": ("clubs/matches", {"clubIds": club_id, "matchType": "leagueMatch", "maxResultCount": 10}),
    }
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {key: pool.submit(api_get, endpoint, **params) for key, (endpoint, params) in jobs.items()}
        data = {key: future.result() for key, future in futures.items()}
    info = data["info"].get(club_id, {}) if isinstance(data["info"], dict) else {}
    stats = data["overall"][0] if isinstance(data["overall"], list) and data["overall"] else {}
    if not info and not stats:
        raise ValueError("Club nicht gefunden. Bitte neu suchen.")
    members = data["members"].get("members", []) if isinstance(data["members"], dict) else []
    career = data["career"].get("members", []) if isinstance(data["career"], dict) else []
    matches = []
    for game in (data["matches"] if isinstance(data["matches"], list) else []):
        clubs = game.get("clubs") or {}
        our = clubs.get(club_id)
        if not isinstance(our, dict):
            continue
        other = next((v for k, v in clubs.items() if str(k) != club_id and isinstance(v, dict)), {})
        # Bei DNF zeigt EA teils zusätzliche Ergebnis-Codes; Toranzahl ist die sichere Grundlage.
        gf = int(our.get("goals") or 0)
        ga = int(our.get("goalsAgainst") or 0)
        result = "W" if gf > ga else "L" if gf < ga else "D"
        matches.append({
            "id": str(game.get("matchId") or ""),
            "timestamp": game.get("timestamp"),
            "opponent": (other.get("details") or {}).get("name") or "Unbekannter Gegner",
            "goals": gf, "against": ga, "result": result,
        })
    remember_roster(club_id, info.get("name") or "Unbekannter Club", members)
    return {
        "id": club_id,
        "name": info.get("name") or "Unbekannter Club",
        "stadium": (info.get("customKit") or {}).get("stadName") or "",
        "stats": stats,
        "members": members,
        "career": career,
        "matches": matches,
        "updated": int(time()),
    }




# Vereine, die bei der Spielersuche AUTOMATISCH geprüft werden, ohne dass
# Besucher zuvor einen Clubnamen eintippen müssen. Optional mit der Render-
# Umgebungsvariable AUTO_CLUBS anpassen: "Viennas Finest,Club Zwei".
AUTO_CLUBS = [n.strip() for n in os.environ.get("AUTO_CLUBS", "Viennas Finest").split(",") if n.strip()][:5]


def do_player_search(gamertag, saved_club_ids=""):
    """Ein Feld: Gamertag. Prüft bekannte, gemerkte und automatisch vorgemerkte Clubs.

    EA stellt KEINE globale Spielersuche bereit. Auch diese Suche ist daher
    bewusst eine Teilmenge, und das meldet die API offen zurück.
    """
    gamertag = gamertag.strip()
    if not 2 <= len(gamertag) <= 60:
        raise ValueError("Bitte einen Spielernamen mit 2 bis 60 Zeichen eingeben.")

    # Serverweit gemerkte Kader aus aufgerufenen Clubs (aktueller Prozess).
    candidates = {str(cid): (str(name), members)
                  for cid, name, members in known_rosters()}

    # Vereine, die auf DIESEM Browser früher besucht wurden, helfen auch nach
    # einem Neustart des kostenlosen Render-Servers.
    given_ids = []
    for item in saved_club_ids.split(",")[:12]:
        item = item.strip()
        if item.isdecimal() and 1 <= len(item) <= 20 and item not in given_ids:
            given_ids.append(item)

    # Die konfigurierten Vereine werden ohne Benutzer-Eingabe gefunden.
    # Fehler eines einzelnen Vereins sollen die ganze Suche nicht abbrechen.
    errors = []
    for name in AUTO_CLUBS:
        try:
            found = do_search(name)["clubs"]
            found.sort(key=lambda c: (
                c["name"].casefold() != name.casefold(),
                not c["name"].casefold().startswith(name.casefold())))
            for c in found[:2]:
                cid = str(c["id"])
                if cid not in candidates and cid not in given_ids:
                    given_ids.append(cid)
        except EAError as exc:
            errors.append(str(exc))

    # Wenige Netzwerkzugriffe parallel. Die EA-API wird nicht massenhaft
    # abgefragt; bis zu 12 bekannte/vorgemerkte Clubs pro Anfrage.
    missing = [cid for cid in given_ids if cid not in candidates][:12]

    def fetch_members(cid):
        raw = api_get("members/stats", clubId=cid)
        members = raw.get("members", []) if isinstance(raw, dict) else []
        # Für browserseitig gemerkte Clubs fehlt eventuell der Clubname.
        club_name = candidates.get(cid, ("", []))[0]
        if not club_name:
            try:
                info_raw = api_get("clubs/info", clubIds=cid)
                club_name = (info_raw.get(cid, {}) if isinstance(info_raw, dict) else {}).get("name") or "Club " + cid
            except EAError:
                club_name = "Club " + cid
        return cid, club_name, members

    if missing:
        with ThreadPoolExecutor(max_workers=3) as pool:
            for future in [pool.submit(fetch_members, cid) for cid in missing]:
                try:
                    cid, name, members = future.result()
                    candidates[cid] = (name, members)
                    remember_roster(cid, name, members)
                except EAError as exc:
                    errors.append(str(exc))

    if not candidates and errors:
        raise EAError("EA ist für die automatische Spielersuche gerade nicht erreichbar.")

    needle = gamertag.casefold()
    results = []
    for cid, (name, members) in candidates.items():
        for player in members:
            if not isinstance(player, dict):
                continue
            username = str(player.get("name") or "")
            pro_name = str(player.get("proName") or "")
            if needle in username.casefold() or (pro_name and needle in pro_name.casefold()):
                results.append({"clubId": cid, "clubName": name, "player": {
                    "name": username, "proName": pro_name,
                    "gamesPlayed": player.get("gamesPlayed"),
                    "goals": player.get("goals"), "assists": player.get("assists"),
                    "ratingAve": player.get("ratingAve"),
                    "favoritePosition": player.get("favoritePosition")
                }})
    results.sort(key=lambda r: (
        r["player"]["name"].casefold() != needle,
        r["player"]["proName"].casefold() != needle,
        r["player"]["name"].casefold()
    ))
    return {"players": results[:25], "searchedClubs": len(candidates),
            "partial": True, "hadErrors": bool(errors), "updated": int(time())}

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(HERE), **kwargs)

    def send_json(self, code, payload):
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        p = urlsplit(self.path)
        if p.path.startswith("/api/"):
            if p.path in ("/api/search", "/api/club", "/api/player") and not request_allowed():
                return self.send_json(429, {"error": "Gerade zu viele Anfragen. Bitte in einer Minute erneut versuchen."})
            query = parse_qs(p.query)
            try:
                if p.path == "/api/search":
                    result = do_search(query.get("q", [""])[0])
                elif p.path == "/api/club":
                    result = do_club(query.get("id", [""])[0])
                elif p.path == "/api/player":
                    result = do_player_search(query.get("q", [""])[0], query.get("ids", [""])[0])
                elif p.path == "/api/events":
                    # Öffentlich kuratierte Termine aus events.json, ohne EA-API-Abhängigkeit.
                    with (HERE / "events.json").open(encoding="utf-8") as events_file:
                        result = json.load(events_file)
                elif p.path == "/api/status":
                    result = {"ok": True}
                else:
                    return self.send_json(404, {"error": "Endpunkt nicht gefunden."})
                return self.send_json(200, result)
            except ValueError as error:
                return self.send_json(400, {"error": str(error)})
            except EAError as error:
                return self.send_json(502, {"error": str(error)})
            except Exception as error:
                print("Unerwarteter Fehler:", repr(error))
                return self.send_json(500, {"error": "Interner Serverfehler."})
        # Keine beliebigen Dateien ausliefern (z. B. server.py)
        if p.path not in ("/", "/index.html", "/favicon.svg"):
            return self.send_error(404, "Nicht gefunden")
        return super().do_GET()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    # Render benoetigt 0.0.0.0; lokal nur 127.0.0.1, ausser HOST ist gesetzt.
    host = os.environ.get("HOST", "0.0.0.0" if os.environ.get("RENDER") else "127.0.0.1")
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"\n  CLUBPULSE FC27 startet auf {host}:{port}\n  Lokal: http://127.0.0.1:{port}\n  Zum Beenden: Strg+C\n", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer beendet.")
    finally:
        server.server_close()
