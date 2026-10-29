"""FC27 STATS — Webserver und Proxy fuer EA-Clubs-Statistiken.

Start lokal: python server.py
Cloud (Render): python server.py, HOST=0.0.0.0, PORT von Render
Keine Zusatzpakete erforderlich (Python 3.9+).
Hinweis: inoffizielle EA-Schnittstelle; kann jederzeit ausfallen.
"""

from collections import deque, OrderedDict, defaultdict
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
import re
import hmac
import secrets
import base64
from datetime import date
from uuid import uuid4

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
        "allTimeLeaderboard/search", "currentSeasonLeaderboard/search", "clubs/info", "clubs/overallStats",
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
            "division": division_override_for_club(str(c.get("clubId", ""))) or c.get("currentDivision"),
            "games": c.get("gamesPlayed"),
            "wins": c.get("wins"),
            "goals": c.get("goals"),
        })
    return {"clubs": clubs, "updated": int(time())}


def match_players_for_club(game, club_id):
    """Gibt nur namentlich erfasste Spieler eines konkreten EA-Matches zurück.

    `mom` ist die EA-Kennzeichnung für Man of the Match ("1").
    Keine Tore, Saisonwerte oder geschätzten Match-Statistiken übernehmen.
    """
    groups = game.get("players") or {}
    raw = groups.get(str(club_id), {}) if isinstance(groups, dict) else {}
    if not isinstance(raw, dict):
        return []
    result = []
    for player_id, stats in list(raw.items())[:32]:
        if not isinstance(stats, dict):
            continue
        name = str(stats.get("playername") or "").strip()[:100]
        if not name:
            name = "Unbekannter Spieler"
        # EA dokumentiert mom = "1" für die Auszeichnung in diesem Match.
        mom = str(stats.get("mom", "")).strip() == "1"
        result.append({"name": name, "motm": mom})
    result.sort(key=lambda p: p["name"].casefold())
    return result


def current_division_for_club(club_id, club_name):
    """Sucht die AKTUELLE Division anhand der exakten Club-ID.

    Sie ist nur in EA-Leaderboard-Suchergebnissen enthalten, nicht in
    clubs/info oder clubs/overallStats. Niemals bestDivision als Ersatz nehmen.
    """
    if not isinstance(club_name, str) or len(club_name.strip()) < 2:
        return None
    for endpoint in ("currentSeasonLeaderboard/search", "allTimeLeaderboard/search"):
        try:
            rows = api_get(endpoint, clubName=club_name.strip(), maxResultCount=50)
        except EAError:
            continue  # Die eigentlichen Club-Daten müssen trotzdem angezeigt werden.
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict) or str(row.get("clubId", "")) != str(club_id):
                continue
            value = str(row.get("currentDivision") or "").strip()
            if value.isdecimal() and 1 <= int(value) <= 10:
                return int(value)
            # Bei einem fehlenden/ungültigen Wert die andere Rangliste probieren.
            break
    return None


def division_override_for_club(club_id):
    """Manuell bestätigte Division aus events.json (GitHub-synchronisiert).

    Kein Zurückfallen auf bestDivision und keine globale +/-1-Korrektur.
    Die Quelle wird im Frontend sichtbar gekennzeichnet.
    """
    with EVENTS_LOCK:
        data = EVENTS_RUNTIME
    if data is None:
        try:
            with (HERE / "events.json").open(encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, ValueError, OSError):
            return None
    overrides = data.get("divisionOverrides") if isinstance(data, dict) else None
    if not isinstance(overrides, dict):
        return None
    value = overrides.get(str(club_id))
    if type(value) is int and 1 <= value <= 10:
        return value
    return None


def formation_history_for_club(club_id):
    # Saved formation/outcome snapshots are curator-maintained: EA doesn't provide formations.
    with EVENTS_LOCK:
        data=EVENTS_RUNTIME
    if data is None:
        try:
            with (HERE / 'events.json').open(encoding='utf-8') as f:
                data=json.load(f)
        except (OSError,ValueError):
            return {}
    histories=data.get('formationHistory',{}) if isinstance(data,dict) else {}
    club=histories.get(str(club_id),{}) if isinstance(histories,dict) else {}
    return {mid:val for mid,val in list(club.items())[:500]
            if isinstance(val,dict)} if isinstance(club,dict) else {}


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
        opponent_id, other = next(((str(k), v) for k, v in clubs.items()
                                    if str(k) != club_id and isinstance(v, dict)), ("", {}))
        # Bei DNF zeigt EA teils zusätzliche Ergebnis-Codes; Toranzahl ist die sichere Grundlage.
        gf = int(our.get("goals") or 0)
        ga = int(our.get("goalsAgainst") or 0)
        result = "W" if gf > ga else "L" if gf < ga else "D"
        ours = match_players_for_club(game, club_id)
        theirs = match_players_for_club(game, opponent_id) if opponent_id else []
        match_id = str(game.get("matchId") or "")
        matches.append({
            "id": match_id,
            "timestamp": game.get("timestamp"),
            "opponent": (other.get("details") or {}).get("name") or "Unbekannter Gegner",
            "goals": gf, "against": ga, "result": result,
            "eaGoals": gf, "eaAgainst": ga,
            "lineup": {"ours": ours, "opponents": theirs},
        })
    club_name = info.get("name") or "Unbekannter Club"
    ea_division = current_division_for_club(club_id, club_name)
    manual_division = division_override_for_club(club_id)
    division = manual_division if manual_division is not None else ea_division
    history = formation_history_for_club(club_id)
    # Manuell erfasste Korrekturen werden deutlich als solche gekennzeichnet.
    # EA-Originalergebnis wird weiterhin separat mitgeliefert.
    for match in matches:
        annotation = history.get(match["id"], {})
        if not isinstance(annotation, dict):
            continue
        if annotation.get("scoreSource") == "manual" and type(annotation.get("goals")) is int and type(annotation.get("against")) is int:
            match["goals"], match["against"] = annotation["goals"], annotation["against"]
            match["result"] = "W" if match["goals"] > match["against"] else "L" if match["goals"] < match["against"] else "D"
            match["manualScore"] = True
        if annotation.get("playersSource") == "manual" and isinstance(annotation.get("players"), list):
            match["lineup"]["ours"] = [
                {"name": str(player.get("name", "")), "position": str(player.get("position", "")), "motm": False}
                for player in annotation["players"] if isinstance(player, dict)
            ]
            match["manualPlayers"] = True
        if annotation.get("formation"):
            match["formation"] = annotation["formation"]
    remember_roster(club_id, club_name, members)
    return {
        "id": club_id,
        "currentDivision": division,
        "divisionSource": "manual" if manual_division is not None else "ea",
        "eaDivision": ea_division,
        "name": info.get("name") or "Unbekannter Club",
        "stadium": (info.get("customKit") or {}).get("stadName") or "",
        "stats": stats,
        "members": members,
        "career": career,
        "matches": matches,
        "formationHistory": history,
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

# ADMIN: Zugangsdaten stehen ausschließlich in Render-Umgebungsvariablen.
# Niemals Passwörter oder GitHub-Tokens in diese Datei / GitHub eintragen.
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "letschmeister/clubpulse-fc27")
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")
ADMIN_WRITE_LOCK = Lock()
ADMIN_FAILURES = defaultdict(deque)
ADMIN_FAILURE_LOCK = Lock()
# Fluechtige Admin-Sitzungen: niemals das Admin-Passwort im Browser speichern.
# Sitzungen werden bei Render-Neustart geloescht und nach 12 Stunden Inaktivitaet ungueltig.
ADMIN_SESSION_LOCK = Lock()
ADMIN_SESSIONS = {}
ADMIN_SESSION_IDLE_SECONDS = 12 * 60 * 60
ADMIN_SESSION_LIMIT = 100
EVENTS_RUNTIME = None
EVENTS_LOCK = Lock()
EVENT_CATEGORIES = {"Playoffs", "Saison", "Turniere", "News"}
EVENT_STATUSES = {"confirmed", "estimated", "unknown"}


class AdminError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def admin_configured():
    # Auch lokal ohne Konfiguration benutzbar – aber Admin-Änderungen gesperrt.
    return len(ADMIN_PASSWORD) >= 12 and bool(GITHUB_TOKEN) and bool(re.fullmatch(r"[\w.-]+/[\w.-]+", GITHUB_REPO))


def admin_auth(password, ip):
    if not admin_configured():
        raise AdminError(503, "Admin noch nicht eingerichtet. ADMIN_PASSWORD und GITHUB_TOKEN in Render konfigurieren.")
    now = monotonic()
    with ADMIN_FAILURE_LOCK:
        attempts = ADMIN_FAILURES[ip]
        while attempts and now - attempts[0] > 300:
            attempts.popleft()
        if len(attempts) >= 10:
            raise AdminError(429, "Zu viele Anmeldeversuche. Bitte in 5 Minuten erneut versuchen.")
    if not hmac.compare_digest(password.encode("utf-8")[:1024], ADMIN_PASSWORD.encode("utf-8")):
        with ADMIN_FAILURE_LOCK:
            ADMIN_FAILURES[ip].append(now)
        raise AdminError(401, "Falsches Admin-Passwort.")
    with ADMIN_FAILURE_LOCK:
        ADMIN_FAILURES.pop(ip, None)


def create_admin_session(password, ip):
    admin_auth(password, ip)
    session_token = secrets.token_urlsafe(32)
    now = monotonic()
    with ADMIN_SESSION_LOCK:
        # Alte Sitzungen aufraeumen, ohne einen Hintergrundtask zu benoetigen.
        for token, expires in list(ADMIN_SESSIONS.items()):
            if expires <= now:
                ADMIN_SESSIONS.pop(token, None)
        if len(ADMIN_SESSIONS) >= ADMIN_SESSION_LIMIT:
            oldest = min(ADMIN_SESSIONS, key=ADMIN_SESSIONS.get)
            ADMIN_SESSIONS.pop(oldest, None)
        ADMIN_SESSIONS[session_token] = now + ADMIN_SESSION_IDLE_SECONDS
    return session_token


def admin_session_auth(session_token):
    if not admin_configured():
        raise AdminError(503, "Admin noch nicht eingerichtet. ADMIN_PASSWORD und GITHUB_TOKEN in Render konfigurieren.")
    if not isinstance(session_token, str) or len(session_token) != 43:
        raise AdminError(401, "Admin-Sitzung abgelaufen. Bitte erneut anmelden.")
    now = monotonic()
    with ADMIN_SESSION_LOCK:
        expiry = ADMIN_SESSIONS.get(session_token, 0)
        if expiry <= now:
            ADMIN_SESSIONS.pop(session_token, None)
            raise AdminError(401, "Admin-Sitzung abgelaufen. Bitte erneut anmelden.")
        ADMIN_SESSIONS[session_token] = now + ADMIN_SESSION_IDLE_SECONDS


def revoke_admin_session(session_token):
    with ADMIN_SESSION_LOCK:
        ADMIN_SESSIONS.pop(session_token, None)


def checked_date(raw, optional=True):
    if raw is None or raw == "":
        if optional: return None
        raise ValueError("Bitte ein Datum eingeben.")
    if not isinstance(raw, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        raise ValueError("Datum muss JJJJ-MM-TT sein.")
    try: date.fromisoformat(raw)
    except ValueError: raise ValueError("Ungültiges Datum.") from None
    return raw


def validate_event(entry, old_id=None):
    if not isinstance(entry, dict):
        raise ValueError("Ungültiges Event.")
    def cleaned(key, size):
        x = entry.get(key) or ""
        if not isinstance(x, str) or len(x) > size:
            raise ValueError(f"Feld {key} ist zu lang oder ungültig.")
        return x.strip()
    category = cleaned("category", 20)
    if category not in EVENT_CATEGORIES:
        raise ValueError("Ungültige Event-Kategorie.")
    title = cleaned("title", 110)
    if len(title) < 3:
        raise ValueError("Bitte einen Titel mit mindestens 3 Zeichen eingeben.")
    description = cleaned("description", 1500)
    start = checked_date(entry.get("start"))
    end = checked_date(entry.get("end"))
    if end and start and end < start:
        raise ValueError("Das Enddatum darf nicht vor dem Startdatum liegen.")
    date_status = cleaned("dateStatus", 20)
    if date_status not in EVENT_STATUSES:
        raise ValueError("Ungültiger Terminstatus.")
    if date_status in ("confirmed", "estimated") and not start:
        raise ValueError("Bestätigte oder geschätzte Termine brauchen ein Startdatum.")
    if date_status == "unknown" and (start or end):
        raise ValueError("Bei 'Termin noch offen' bitte kein Datum eintragen.")
    source = cleaned("source", 500)
    if source and (not source.startswith("https://") or any(c in source for c in (' ', '\n', '\r', '\t'))):
        raise ValueError("Quellenlink muss eine HTTPS-Adresse sein.")
    label = cleaned("sourceLabel", 100)
    eid = old_id or ("event-" + uuid4().hex[:16])
    return {"id": eid, "category": category, "title": title, "start": start,
            "end": end, "description": description, "source": source,
            "sourceLabel": label, "dateStatus": date_status}


def github_file_and_sha():
    owner, repo = GITHUB_REPO.split("/")
    endpoint = f"https://api.github.com/repos/{owner}/{repo}/contents/events.json?{urlencode({'ref':GITHUB_BRANCH})}"
    req = Request(endpoint, headers={"Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "FC27-STATS-admin/1.0"})
    try:
        with urlopen(req, timeout=14) as resp:
            payload=json.load(resp)
        decoded=base64.b64decode(payload["content"]).decode("utf-8")
        events=json.loads(decoded)
        if not isinstance(events, dict) or not isinstance(events.get("items"), list):
            raise ValueError("Ungültiges Format von events.json auf GitHub.")
        return events, payload["sha"]
    except (HTTPError, URLError, TimeoutError, KeyError, ValueError, OSError) as e:
        # Keine Token oder Zugriffsinformationen aus HTTP-Antworten protokollieren.
        print("GitHub lesen fehlgeschlagen:", type(e).__name__, getattr(e, "code", ""))
        raise AdminError(502, "Events können auf GitHub nicht gelesen werden. Token, Repo und Berechtigungen prüfen.") from None


def github_write_events(events, sha):
    owner, repo=GITHUB_REPO.split("/")
    url=f"https://api.github.com/repos/{owner}/{repo}/contents/events.json"
    raw=(json.dumps(events,ensure_ascii=False,indent=2)+"\n").encode("utf-8")
    body=json.dumps({"message":"FC27 STATS: Admin-Aktualisierung",
                     "content":base64.b64encode(raw).decode("ascii"),
                     "sha":sha,"branch":GITHUB_BRANCH}).encode("utf-8")
    req=Request(url, data=body, method="PUT", headers={
        "Authorization":f"Bearer {GITHUB_TOKEN}",
        "Accept":"application/vnd.github+json",
        "Content-Type":"application/json", "User-Agent":"FC27-STATS-admin/1.0",
        "X-GitHub-Api-Version":"2022-11-28"})
    try:
        with urlopen(req,timeout=16) as resp:
            if resp.status not in (200,201):
                raise AdminError(502,"GitHub hat die Änderung nicht bestätigt.")
    except HTTPError as e:
        print("GitHub speichern fehlgeschlagen: HTTP", e.code)
        if e.code == 409:
            raise AdminError(409,"Events wurden zwischenzeitlich verändert. Bitte neu laden.") from None
        raise AdminError(502,"Speichern auf GitHub fehlgeschlagen. Token muss Contents: Read and write besitzen.") from None
    except (URLError, TimeoutError, OSError):
        raise AdminError(502,"GitHub ist gerade nicht erreichbar; es wurde nichts gespeichert.") from None


def admin_change(body):
    global EVENTS_RUNTIME
    action = body.get("action")
    if action not in ("create", "update", "delete"):
        raise ValueError("Unbekannte Aktion.")
    with ADMIN_WRITE_LOCK:
        data, sha=github_file_and_sha()
        items=data["items"]
        if len(items)>100:
            raise ValueError("Zu viele Events in events.json.")
        eid=body.get("id","")
        if not isinstance(eid,str):
            raise ValueError("Ungültige Event-ID.")
        if action in ("delete", "update"):
            current=next((i for i,x in enumerate(items) if isinstance(x,dict) and x.get("id")==eid),None)
            if current is None:
                raise ValueError("Event nicht gefunden. Liste neu laden.")
            if action=="delete": del items[current]
            else: items[current]=validate_event(body.get("event"), old_id=eid)
        else:
            if len(items)>=100: raise ValueError("Maximal 100 Events möglich.")
            items.insert(0, validate_event(body.get("event")))
        data["lastChecked"]=date.today().isoformat()
        github_write_events(data,sha)
        with EVENTS_LOCK:
            EVENTS_RUNTIME=data
        return data


def admin_set_division(body):
    """Speichert eine manuelle Division auf GitHub in events.json.

    Kann nur nach erfolgreicher Passwortprüfung aufgerufen werden.
    `division=null` löscht die Korrektur (EA-Zahl wird wieder angezeigt).
    """
    global EVENTS_RUNTIME
    club_id = str(body.get("clubId", ""))
    value = body.get("division")
    if not (club_id.isdecimal() and 1 <= len(club_id) <= 20):
        raise ValueError("Ungültige Club-ID.")
    if value is not None and (type(value) is not int or not 1 <= value <= 10):
        raise ValueError("Die Division muss zwischen 1 und 10 liegen.")
    with ADMIN_WRITE_LOCK:
        data, sha = github_file_and_sha()
        overrides = data.get("divisionOverrides", {})
        if not isinstance(overrides, dict) or len(overrides) > 300:
            raise ValueError("Ungültiges Format der Divisionskorrekturen.")
        overrides = dict(overrides)
        if value is None:
            overrides.pop(club_id, None)
        else:
            if club_id not in overrides and len(overrides) >= 300:
                raise ValueError("Zu viele Divisionskorrekturen.")
            overrides[club_id] = value
        data["divisionOverrides"] = overrides
        github_write_events(data, sha)
        with EVENTS_LOCK:
            EVENTS_RUNTIME = data
        return {"clubId": club_id, "division": value}


FORMATIONS = {"4-2-3-1", "4-3-3", "4-4-2", "4-1-2-1-2", "4-1-4-1", "4-3-2-1",
              "4-2-2-2", "4-2-4", "4-5-1", "3-5-2", "3-4-3", "3-4-2-1", "3-4-1-2",
              "3-1-4-2", "5-2-3", "5-3-2", "5-4-1"}


POSITIONS = {"TW", "LV", "IV", "RV", "LAV", "RAV", "ZDM", "ZM", "ZOM", "LM", "RM", "LF", "RF", "LS", "RS", "ST"}


def validate_manual_roster(raw):
    if not isinstance(raw, list) or len(raw) > 16:
        raise ValueError("Bitte höchstens 16 Spieler pro Match eintragen.")
    result = []
    seen = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("Ungültiger Spieler.")
        name = item.get("name")
        position = item.get("position")
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 70:
            raise ValueError("Jeder Spieler braucht einen Namen mit höchstens 70 Zeichen.")
        if position not in POSITIONS and position != "":
            raise ValueError("Bitte eine gültige Position wählen oder das Feld leer lassen.")
        name = name.strip()
        if name.casefold() in seen:
            raise ValueError("Spieler dürfen in einem Match nicht doppelt eingetragen werden.")
        seen.add(name.casefold())
        result.append({"name": name, "position": position})
    return result


def admin_set_formation(body):
    """Protected match editor for EA matches and matches already saved in history.

    EA results and players are initial suggestions; manual edits are explicitly
    marked separately. Snapshots persist in GitHub events.json and support
    historic formations after EA no longer returns the match in its last ten.
    """
    global EVENTS_RUNTIME
    club_id = str(body.get("clubId", ""))
    match_id = str(body.get("matchId", ""))
    formation = body.get("formation") or None
    reset = body.get("reset") is True
    if not (club_id.isdecimal() and 1 <= len(club_id) <= 20 and match_id.isdecimal() and 1 <= len(match_id) <= 25):
        raise ValueError("Ungültige Club- oder Match-ID.")
    if not reset and formation is not None and formation not in FORMATIONS:
        raise ValueError("Ungültige Formation.")
    gf = body.get("goals")
    ga = body.get("against")
    if not reset and (type(gf) is not int or type(ga) is not int or not (0 <= gf <= 30 and 0 <= ga <= 30)):
        raise ValueError("Das Ergebnis muss aus zwei ganzen Zahlen zwischen 0 und 30 bestehen.")
    players = [] if reset else validate_manual_roster(body.get("players", []))

    # Verify match ID belongs to the club using EA, OR that this match was
    # previously verified and stored. Never allow arbitrary invented match IDs.
    verified = None
    if not reset:
        try:
            games = api_get("clubs/matches", clubIds=club_id, matchType="leagueMatch", maxResultCount=10)
        except EAError:
            games = []
        for game in games if isinstance(games, list) else []:
            if not isinstance(game, dict) or str(game.get("matchId", "")) != match_id:
                continue
            ours = (game.get("clubs") or {}).get(club_id, {})
            if not isinstance(ours, dict) or not ours:
                continue
            opponents = [v for k,v in (game.get("clubs") or {}).items() if str(k) != club_id and isinstance(v, dict)]
            ea_gf, ea_ga = int(ours.get("goals") or 0), int(ours.get("goalsAgainst") or 0)
            verified = {
                "timestamp": int(game.get("timestamp") or 0),
                "opponent": str(((opponents[0].get("details") if opponents else {}) or {}).get("name") or "Gegner")[:90],
                "eaGoals": ea_gf, "eaAgainst": ea_ga
            }
            break
    with ADMIN_WRITE_LOCK:
        data, sha = github_file_and_sha()
        histories = data.get("formationHistory", {})
        if not isinstance(histories, dict) or len(histories) > 300:
            raise ValueError("Ungültiger Aufstellungsspeicher.")
        histories = dict(histories)
        club = histories.get(club_id, {})
        if not isinstance(club, dict) or len(club) > 500:
            raise ValueError("Zu viele gespeicherte Matches.")
        club = dict(club)
        if reset:
            if match_id not in club:
                return club
            club.pop(match_id, None)
        else:
            old = club.get(match_id)
            if verified is None:
                if not isinstance(old, dict):
                    raise ValueError("Match nicht unter den letzten zehn EA-Spielen gefunden. Öffne den Club erneut.")
                verified = {"timestamp": old.get("timestamp", 0), "opponent": old.get("opponent", "Gegner"),
                            "eaGoals": old.get("eaGoals", old.get("goals", 0)),
                            "eaAgainst": old.get("eaAgainst", old.get("against", 0))}
            if match_id not in club and len(club) >= 500:
                raise ValueError("Maximal 500 archivierte Matches pro Club.")
            club[match_id] = {
                **verified, "formation": formation, "goals": gf, "against": ga,
                "result": "W" if gf > ga else "L" if gf < ga else "D",
                "scoreSource": "manual", "players": players, "playersSource": "manual"
            }
        if club: histories[club_id] = club
        else: histories.pop(club_id, None)
        data["formationHistory"] = histories
        github_write_events(data, sha)
        with EVENTS_LOCK:
            EVENTS_RUNTIME = data
        return club


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

    def do_POST(self):
        p=urlsplit(self.path)
        if p.path not in ("/api/admin/login", "/api/admin/session", "/api/admin/logout", "/api/admin/events", "/api/admin/division", "/api/admin/formation"):
            return self.send_json(404,{"error":"Endpunkt nicht gefunden."})
        try:
            length=int(self.headers.get("Content-Length", "0"))
            if not 1<=length<=22000:
                return self.send_json(413,{"error":"Anfrage zu groß oder leer."})
            payload=json.loads(self.rfile.read(length))
            if not isinstance(payload,dict):
                raise ValueError("Ungültige Anfrage.")
            # Nicht von einer fremden Website aus Browseranfragen erlauben.
            origin=self.headers.get("Origin", "")
            host=self.headers.get("Host", "")
            if origin and urlsplit(origin).netloc != host:
                raise AdminError(403,"Anfrage von fremder Website abgelehnt.")
            if p.path=="/api/admin/login":
                password=self.headers.get("X-Admin-Password", "")
                if len(password)>1024:
                    raise AdminError(401,"Ungültiges Passwort.")
                session=create_admin_session(password,self.client_address[0])
                return self.send_json(200,{"ok":True,"session":session})
            token=self.headers.get("X-Admin-Session", "")
            if token:
                admin_session_auth(token)
            else:
                # Abwaertskompatibilitaet fuer alte index.html aus dem Browsercache.
                password=self.headers.get("X-Admin-Password", "")
                if not password or len(password)>1024:
                    raise AdminError(401,"Admin-Anmeldung erforderlich.")
                admin_auth(password,self.client_address[0])
            if p.path=="/api/admin/session":
                return self.send_json(200,{"ok":True})
            if p.path=="/api/admin/logout":
                if token:
                    revoke_admin_session(token)
                return self.send_json(200,{"ok":True})
            if p.path == "/api/admin/division":
                result = admin_set_division(payload)
                return self.send_json(200, {"ok": True, "result": result})
            if p.path == "/api/admin/formation":
                result = admin_set_formation(payload)
                return self.send_json(200, {"ok": True, "formationHistory": result})
            result=admin_change(payload)
            return self.send_json(200,{"ok":True,"events":result})
        except AdminError as error:
            return self.send_json(error.code,{"error":str(error)})
        except (ValueError,UnicodeError,json.JSONDecodeError) as error:
            return self.send_json(400,{"error":str(error) or "Ungültige Anfrage."})
        except Exception as error:
            print("Unerwarteter Adminfehler:",type(error).__name__)
            return self.send_json(500,{"error":"Interner Serverfehler."})

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
                    with EVENTS_LOCK:
                        result=EVENTS_RUNTIME
                    if result is None:
                        with (HERE / "events.json").open(encoding="utf-8") as events_file:
                            result=json.load(events_file)
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
    print(f"\n  FC27 STATS startet auf {host}:{port}\n  Lokal: http://127.0.0.1:{port}\n  Zum Beenden: Strg+C\n", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer beendet.")
    finally:
        server.server_close()
