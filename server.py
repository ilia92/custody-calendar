#!/usr/bin/env python3
"""Custody Calendar - a shared 50/50 custody planner for two parents.

One HTML file + one Python file + plain JSON, in the spirit of
https://github.com/ilia92/task-tree-planner

Run:   python3 server.py [port]
Help:  python3 server.py --help
"""
import hashlib
import json
import os
import re
import secrets
import sys
import threading
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs, unquote, urlparse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
DATA_PATH = os.path.join(BASE_DIR, "data.json")
HOLIDAYS_PATH = os.path.join(BASE_DIR, "holidays.json")
APP_FILE = "custody_calendar.html"

ROLES = ("mum", "dad")

OTHER = {"mum": "dad", "dad": "mum"}
SESSION_TTL = 7 * 24 * 3600
MAX_BODY = 1024 * 1024
DEFAULTS = {
    "mum": {"color": "#ec4899", "display_name": "Mum"},
    "dad": {"color": "#3b82f6", "display_name": "Dad"},
}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class ApiError(Exception):
    def __init__(self, status, message, payload=None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.payload = payload or {}


_MGMT_FLAGS = ("--help", "-h", "--hash-password", "--check-holidays",
                "--refresh-holidays", "--set-user", "--list-users")


# --------------------------------------------------------------------------- config

def load_config():
    cfg = {}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as fh:
                cfg = json.load(fh)
        except Exception as exc:
            raise SystemExit("config.json is not valid JSON: %s" % exc)
    cfg.setdefault("instance_name", "Custody Calendar")
    cfg.setdefault("port", 8010)
    cfg.setdefault("country", "BG")
    cfg.setdefault("poll_seconds", 10)
    # which parent is shown first, everywhere in the interface
    order = cfg.get("display_order")
    if not (isinstance(order, list) and sorted(str(r) for r in order) == sorted(ROLES)):
        order = list(ROLES)
    cfg["display_order"] = [str(r) for r in order]
    parents = cfg.setdefault("parents", {})
    for role in ROLES:
        p = parents.setdefault(role, {})
        p["username"] = str(p.get("username") or role).strip().lower()
        p["display_name"] = str(p.get("display_name") or DEFAULTS[role]["display_name"])
        p["color"] = str(p.get("color") or DEFAULTS[role]["color"])
    return cfg


_config_lock = threading.RLock()
_config_mtime = None


def _config_mtime_now():
    try:
        return os.path.getmtime(CONFIG_PATH)
    except OSError:
        return None


def save_config():
    """Atomic write of CONFIG back to config.json; caller holds _config_lock."""
    global _config_mtime
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(CONFIG, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, CONFIG_PATH)
    _config_mtime = _config_mtime_now()


def maybe_reload_config():
    """Pick up hand edits to config.json without a restart.

    Names, usernames and passwords apply immediately. The port, instance_name and
    country are read once at startup, so changing those still needs a restart.
    """
    global _config_mtime
    with _config_lock:
        mtime = _config_mtime_now()
        if mtime is None or mtime == _config_mtime:
            return False
        try:
            fresh = load_config()
        except SystemExit:
            print("[config] config.json is currently invalid - keeping the loaded one")
            _config_mtime = mtime
            return False
        CONFIG.clear()
        CONFIG.update(fresh)
        _config_mtime = mtime
        print("[config] reloaded config.json (%s = %s, %s = %s)"
              % (CONFIG["parents"]["mum"]["display_name"], CONFIG["parents"]["mum"]["username"],
                 CONFIG["parents"]["dad"]["display_name"], CONFIG["parents"]["dad"]["username"]))
        return True


CONFIG = load_config()
PORT = int(CONFIG.get("port") or 8010)
if len(sys.argv) > 1 and sys.argv[1] not in _MGMT_FLAGS:
    try:
        PORT = int(sys.argv[1])
    except ValueError:
        sys.exit("Port must be a number (got %r). Try --help." % sys.argv[1])

INSTANCE = CONFIG["instance_name"]
COOKIE_NAME = "session_%s_%s" % (re.sub(r"[^A-Za-z0-9]+", "_", INSTANCE).strip("_").lower(), PORT)


def public_parents():
    return {
        role: {
            "username": CONFIG["parents"][role]["username"],
            "display_name": CONFIG["parents"][role]["display_name"],
            "color": CONFIG["parents"][role]["color"],
        }
        for role in ROLES
    }


# --------------------------------------------------------------------------- auth

def hash_password(password):
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def check_credentials(username, password):
    """Return the role ('mum'/'dad') on success, else None."""
    maybe_reload_config()
    username = (username or "").strip().lower()
    for role in ROLES:
        p = CONFIG["parents"][role]
        if not secrets.compare_digest(p["username"], username):
            continue
        if p.get("password_hash"):
            if secrets.compare_digest(hash_password(password or ""), str(p["password_hash"]).lower()):
                return role
        elif p.get("password"):
            if secrets.compare_digest(str(p["password"]), password or ""):
                return role
        return None
    return None


USERNAME_RE = re.compile(r"^[a-z0-9._-]{1,32}$")


def update_account(role, current_password, display_name=None, username=None, new_password=None):
    """Let a parent change their own name / username / password. Returns a note for the log."""
    with _config_lock:
        p = CONFIG["parents"][role]
        if not check_credentials(p["username"], current_password) == role:
            raise ApiError(403, "Your current password is not right.")
        changed = []
        if display_name is not None:
            display_name = " ".join(str(display_name).split())
            if not 1 <= len(display_name) <= 40:
                raise ApiError(400, "The display name must be 1-40 characters.")
            if display_name != p["display_name"]:
                p["display_name"] = display_name
                changed.append("name")
        if username is not None:
            username = str(username).strip().lower()
            if not USERNAME_RE.match(username):
                raise ApiError(400, "The username may use a-z, 0-9, dot, dash and underscore only.")
            if username == CONFIG["parents"][OTHER[role]]["username"]:
                raise ApiError(400, "That username is already taken by the other parent.")
            if username != p["username"]:
                p["username"] = username
                changed.append("username")
        if new_password:
            if len(new_password) < 4:
                raise ApiError(400, "Use at least 4 characters for the password.")
            p["password_hash"] = hash_password(new_password)
            p.pop("password", None)   # never keep the old plaintext next to a hash
            changed.append("password")
        if changed:
            save_config()
        return changed


_sessions = {}
_session_lock = threading.Lock()


def create_session(role):
    token = secrets.token_hex(24)
    with _session_lock:
        _sessions[token] = {"role": role, "exp": datetime.now().timestamp() + SESSION_TTL}
    return token


def get_session(token):
    if not token:
        return None
    with _session_lock:
        sess = _sessions.get(token)
        if not sess:
            return None
        if sess["exp"] < datetime.now().timestamp():
            _sessions.pop(token, None)
            return None
        return dict(sess)


def session_token_from(headers):
    raw = headers.get("Cookie")
    if not raw:
        return None
    try:
        return SimpleCookie(raw).get(COOKIE_NAME).value
    except Exception:
        return None


# --------------------------------------------------------------------------- holidays

def orthodox_easter(year):
    """Easter Sunday of the Orthodox church, as a Gregorian date (valid 1900-2099)."""
    a, b, c = year % 4, year % 7, year % 19
    d = (19 * c + 15) % 30
    e = (2 * a + 4 * b - d + 34) % 7
    month = (d + e + 114) // 31
    day = ((d + e + 114) % 31) + 1
    return date(year, month, day) + timedelta(days=13)  # Julian -> Gregorian


# Official non-working days, Bulgarian Labour Code art. 154(1).
FIXED_BG = [
    ((1, 1), "Нова година", "New Year's Day"),
    ((3, 3), "Ден на Освобождението на България", "Liberation Day"),
    ((5, 1), "Ден на труда", "Labour Day"),
    ((5, 6), "Гергьовден, Ден на храбростта и Българската армия", "St George's Day / Army Day"),
    ((5, 24), "Ден на българската азбука, просвета и култура", "Education and Culture Day"),
    ((9, 6), "Ден на Съединението", "Unification Day"),
    ((9, 22), "Ден на Независимостта", "Independence Day"),
    ((12, 24), "Бъдни вечер", "Christmas Eve"),
    ((12, 25), "Рождество Христово", "Christmas Day"),
    ((12, 26), "Втори ден на Рождество Христово", "Second day of Christmas"),
]
# Non-working for pupils only - which is what matters for the kids.
SCHOOL_BG = [((11, 1), "Ден на народните будители", "Day of the Bulgarian Enlighteners")]


def bg_holidays(year):
    """{iso: {name_bg, name_en, type, source}} for one year, computed offline."""
    out = {}
    for (m, d), bg, en in FIXED_BG:
        out[date(year, m, d).isoformat()] = {
            "name_bg": bg, "name_en": en, "type": "public", "source": "law",
        }
    easter = orthodox_easter(year)
    for offset, bg, en in (
        (-2, "Разпети петък", "Good Friday"),
        (-1, "Велика събота", "Holy Saturday"),
        (0, "Великден", "Easter Sunday"),
        (1, "Втори ден на Великден", "Easter Monday"),
    ):
        out[(easter + timedelta(days=offset)).isoformat()] = {
            "name_bg": bg, "name_en": en, "type": "easter", "source": "law",
        }
    # A public holiday (Easter excluded) falling on a weekend moves to the next working day.
    for iso in sorted(k for k, v in out.items() if v["type"] == "public"):
        day = date.fromisoformat(iso)
        if day.weekday() < 5:
            continue
        nxt = day + timedelta(days=1)
        while nxt.weekday() >= 5 or nxt.isoformat() in out:
            nxt += timedelta(days=1)
        out[nxt.isoformat()] = {
            "name_bg": "Почивен ден (%s)" % out[iso]["name_bg"],
            "name_en": "Day off in lieu of %s" % out[iso]["name_en"],
            "type": "compensation", "source": "law",
        }
    for (m, d), bg, en in SCHOOL_BG:
        iso = date(year, m, d).isoformat()
        if iso not in out:
            out[iso] = {"name_bg": bg, "name_en": en, "type": "school", "source": "law"}
    return out


def fetch_nager(year, country):
    url = "https://date.nager.at/api/v3/PublicHolidays/%d/%s" % (year, country)
    req = urllib.request.Request(url, headers={"User-Agent": "custody-calendar"})
    with urllib.request.urlopen(req, timeout=6) as resp:
        return json.loads(resp.read().decode("utf-8"))


def holiday_cache():
    if os.path.exists(HOLIDAYS_PATH):
        try:
            with open(HOLIDAYS_PATH, encoding="utf-8") as fh:
                cache = json.load(fh)
            if isinstance(cache, dict):
                return cache
        except Exception:
            pass
    return {}


def get_holidays(year, refresh=False):
    """Computed law-based list, optionally cross-checked against date.nager.at.

    Anything hand-edited in holidays.json (source == "manual") always wins.
    """
    cache = holiday_cache()
    key = str(year)
    cached = cache.get(key) or {}
    if not refresh and cached:
        return cached
    merged = bg_holidays(year)
    if refresh:
        try:
            for item in fetch_nager(year, CONFIG["country"]):
                iso = item.get("date")
                if not iso or iso in merged:
                    continue
                merged[iso] = {
                    "name_bg": item.get("localName") or item.get("name") or "",
                    "name_en": item.get("name") or "",
                    "type": "public", "source": "nager",
                }
        except Exception as exc:
            print("[holidays] %s refresh failed (%s); using the computed list" % (year, exc))
    for iso, info in cached.items():
        if info.get("source") == "manual":
            merged[iso] = info
    cache[key] = dict(sorted(merged.items()))
    try:
        tmp = HOLIDAYS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(dict(sorted(cache.items())), fh, ensure_ascii=False, indent=1)
        os.replace(tmp, HOLIDAYS_PATH)
    except Exception as exc:
        print("[holidays] could not write cache: %s" % exc)
    return cache[key]


# --------------------------------------------------------------------------- data

_data_lock = threading.RLock()
_data = None


def blank_day():
    return {
        "day": {"mum": False, "dad": False},
        "night": {"mum": False, "dad": False},
        "off": {"mum": False, "dad": False},
        "lock": {"mum": False, "dad": False},
        "note": "",
        "rev": 0,
        "log": [],
    }


def empty_data():
    return {
        "version": 1,
        "rev": 0,
        "settings": {"night_belongs_to": "evening", "week_starts": "monday"},
        "days": {},
    }


def normalise_day(raw):
    day = blank_day()
    if isinstance(raw, dict):
        for group in ("day", "night", "off", "lock"):
            block = raw.get(group) or {}
            if isinstance(block, dict):
                for role in ROLES:
                    day[group][role] = bool(block.get(role))
        day["note"] = str(raw.get("note") or "")
        day["rev"] = int(raw.get("rev") or 0)
        log = raw.get("log")
        day["log"] = log[-10:] if isinstance(log, list) else []
    return day


def is_blank(day):
    if day["note"].strip():
        return False
    return not any(day[g][r] for g in ("day", "night", "off", "lock") for r in ROLES)


def load_data():
    global _data
    with _data_lock:
        data = empty_data()
        if os.path.exists(DATA_PATH):
            try:
                with open(DATA_PATH, encoding="utf-8") as fh:
                    raw = json.load(fh)
                if isinstance(raw, dict):
                    data["rev"] = int(raw.get("rev") or 0)
                    if isinstance(raw.get("settings"), dict):
                        data["settings"].update(raw["settings"])
                    for iso, rawday in (raw.get("days") or {}).items():
                        if DATE_RE.match(str(iso)):
                            day = normalise_day(rawday)
                            if not is_blank(day):
                                data["days"][iso] = day
            except Exception as exc:
                sys.exit("data.json is not readable (%s). Move it aside and restart." % exc)
        _data = data
        return _data


def write_data():
    """Atomic write; caller holds _data_lock."""
    tmp = DATA_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(_data, fh, ensure_ascii=False, indent=1, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, DATA_PATH)


def parse_changes(changes, role):
    """Validate one change set against the role's permissions."""
    if not isinstance(changes, dict):
        raise ApiError(400, "changes must be an object")
    wants = []
    for group in ("day", "night", "off", "lock"):
        block = changes.get(group)
        if block is None:
            continue
        if not isinstance(block, dict):
            raise ApiError(400, "%s must be an object" % group)
        for r in ROLES:
            if r not in block:
                continue
            if group in ("off", "lock") and r != role:
                raise ApiError(403, "You can only set your own %s flag." % (
                    "unavailability" if group == "off" else "lock"))
            wants.append((group, r, bool(block[r])))
    note = changes.get("note")
    if note is not None:
        note = str(note)[:500]
    return wants, note


def change_day(iso, wants, note, role, username, base_rev=None):
    """Apply a validated change set to one date. Caller holds _data_lock."""
    day = _data["days"].get(iso) or blank_day()
    locked_by = [r for r in ROLES if day["lock"][r]]
    if locked_by:
        # while a lock stands the day is frozen; the only thing you may still touch is your own lock
        only_own_lock = bool(wants) and note is None and all(
            g == "lock" and r == role for g, r, v in wants)
        if not only_own_lock:
            who = " and ".join(CONFIG["parents"][r]["display_name"] for r in locked_by)
            if role in locked_by and len(locked_by) == 1:
                msg = "You locked this day. Unlock it first to change anything."
            else:
                msg = "%s locked this day - ask %s to unlock it." % (
                    who, "them" if len(locked_by) > 1 else CONFIG["parents"][locked_by[0]]["display_name"])
            raise ApiError(409, msg, {"day": day})
    if base_rev is not None and day["rev"] > base_rev:
        raise ApiError(409, "This day was changed by the other parent. Reloading.", {"day": day})

    applied = []
    for group, r, val in wants:
        if day[group][r] != val:
            day[group][r] = val
            applied.append("%s.%s=%s" % (group, r, "on" if val else "off"))
    if note is not None and note != day["note"]:
        day["note"] = note
        applied.append("note")
    if not applied:
        return day, False

    _data["rev"] += 1
    day["rev"] = _data["rev"]
    day["log"] = (day["log"] + [{
        "ts": datetime.now().replace(microsecond=0).isoformat(),
        "by": username,
        "role": role,
        "what": ", ".join(applied),
    }])[-10:]
    if is_blank(day):
        _data["days"].pop(iso, None)
    else:
        _data["days"][iso] = day
    return day, True


def daterange(start_iso, end_iso, weekdays=None):
    start, end = date.fromisoformat(start_iso), date.fromisoformat(end_iso)
    if end < start:
        start, end = end, start
    if (end - start).days > 750:
        raise ApiError(400, "Range too long (max about two years).")
    cur = start
    while cur <= end:
        if not weekdays or cur.weekday() in weekdays:
            yield cur.isoformat()
        cur += timedelta(days=1)


# --------------------------------------------------------------------------- http

LOGIN_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>%(title)s</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<link rel="alternate icon" href="/favicon.ico" sizes="16x16 32x32 48x48">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<style>
 :root { --mum:%(mum_color)s; --dad:%(dad_color)s; }
 * { box-sizing:border-box }
 body { margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center;
   font-family:Inter,Arial,sans-serif; color:#111827;
   background:linear-gradient(135deg,#fce7f3 0%%,#f3f4f6 50%%,#dbeafe 100%%); }
 .card { background:#fff; padding:34px 32px; border-radius:18px; width:340px;
   box-shadow:0 6px 18px rgba(0,0,0,.10); }
 h1 { margin:0 0 4px; font-size:22px; text-align:center }
 h1 { margin-bottom:20px }
 label { display:block; font-size:12px; color:#6b7280; margin:12px 0 4px }
 input { width:100%%; padding:10px 12px; border:1px solid #d1d5db; border-radius:10px;
   font-size:14px; font-family:inherit }
 input:focus { outline:2px solid #c7d2fe; border-color:#a5b4fc }
 button { width:100%%; margin-top:20px; padding:11px; border:0; border-radius:10px; color:#fff;
   font-size:15px; font-weight:600; cursor:pointer;
   background:linear-gradient(90deg,var(--mum),var(--dad)); }
 .err { margin-top:14px; padding:9px 11px; border-radius:9px; background:#fee2e2; color:#991b1b;
   font-size:13px; text-align:center }
 .hearts { text-align:center; font-size:20px; margin-bottom:6px }
</style></head><body>
<form class="card" method="POST" action="/do-login">
  <div class="hearts">&#128150;</div>
  <h1>%(title)s</h1>
  <label for="u">Username</label>
  <input id="u" name="username" autocapitalize="none" autocomplete="username" autofocus>
  <label for="p">Password</label>
  <input id="p" name="password" type="password" autocomplete="current-password">
  <button type="submit">Sign in</button>
  %(error)s
</form></body></html>
"""

MIME = {
    ".html": "text/html; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".ttf": "font/ttf",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}
PRIVATE_FILES = {"config.json", "config.json.example", "data.json", "holidays.json", "server.py"}
# served without a session so the login page and the browser tab get their icon
PUBLIC_FILES = {"/favicon.ico", "/favicon.svg", "/apple-touch-icon.png"}


class Handler(BaseHTTPRequestHandler):
    server_version = "CustodyCalendar/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # -- helpers ----------------------------------------------------------
    def _send(self, status, body, ctype="text/plain; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, val in (extra or []):
            self.send_header(key, val)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status, obj, extra=None):
        self._send(status, json.dumps(obj, ensure_ascii=False),
                   "application/json; charset=utf-8", extra)

    def _redirect(self, location, extra=None):
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        for key, val in (extra or []):
            self.send_header(key, val)
        self.end_headers()

    def _login_page(self, error=""):
        html = LOGIN_PAGE % {
            "title": INSTANCE,
            "mum_color": CONFIG["parents"]["mum"]["color"],
            "dad_color": CONFIG["parents"]["dad"]["color"],
            "error": '<div class="err">%s</div>' % error if error else "",
        }
        self._send(200, html, "text/html; charset=utf-8")

    def _session(self):
        return get_session(session_token_from(self.headers))

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return b""
        if length > MAX_BODY:
            raise ApiError(413, "Request too large")
        return self.rfile.read(length)

    def _json_body(self):
        try:
            return json.loads(self._body().decode("utf-8") or "{}")
        except ApiError:
            raise
        except Exception:
            raise ApiError(400, "Invalid JSON body")

    def _me(self, sess):
        role = sess["role"]
        p = CONFIG["parents"][role]
        return {"role": role, "username": p["username"], "display_name": p["display_name"],
                "color": p["color"]}

    # -- GET --------------------------------------------------------------
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)

        if path == "/login":
            return self._login_page()
        if path in PUBLIC_FILES:
            return self._static(path)

        sess = self._session()
        if not sess:
            if path.startswith("/api/"):
                return self._json(401, {"error": "Not signed in", "login": "/login"})
            return self._redirect("/login")

        try:
            if path == "/api/state":
                maybe_reload_config()
                this_year = date.today().year
                # ship the holidays with the state so the calendar never paints without them
                seed = {str(y): get_holidays(y) for y in (this_year - 1, this_year, this_year + 1)}
                with _data_lock:
                    return self._json(200, {
                        "rev": _data["rev"],
                        "days": _data["days"],
                        "settings": _data["settings"],
                        "parents": public_parents(),
                        "me": self._me(sess),
                        "instance_name": INSTANCE,
                        "poll_seconds": CONFIG["poll_seconds"],
                        "display_order": CONFIG["display_order"],
                        "today": date.today().isoformat(),
                        "holidays": seed,
                    })
            if path == "/api/rev":
                with _data_lock:
                    return self._json(200, {"rev": _data["rev"]})
            if path == "/api/holidays":
                years = query.get("year") or [str(date.today().year)]
                refresh = query.get("refresh", ["0"])[0] in ("1", "true", "yes")
                out = {}
                for raw in years[:6]:
                    try:
                        year = int(raw)
                    except ValueError:
                        raise ApiError(400, "year must be a number")
                    if not 1900 <= year <= 2099:
                        raise ApiError(400, "year out of range")
                    out[str(year)] = get_holidays(year, refresh)
                return self._json(200, {"holidays": out, "country": CONFIG["country"]})
            if path == "/logout":
                token = session_token_from(self.headers)
                with _session_lock:
                    _sessions.pop(token, None)
                return self._redirect("/login", [(
                    "Set-Cookie", "%s=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict" % COOKIE_NAME)])
        except ApiError as exc:
            return self._json(exc.status, dict({"error": exc.message}, **exc.payload))

        return self._static(path if path != "/" else "/" + APP_FILE)

    def _static(self, path):
        rel = path.lstrip("/")
        if not rel or rel in PRIVATE_FILES or rel.endswith(".tmp"):
            return self._send(404, "Not found")
        full = os.path.normpath(os.path.join(BASE_DIR, rel))
        if not full.startswith(BASE_DIR + os.sep) or not os.path.isfile(full):
            return self._send(404, "Not found")
        ext = os.path.splitext(full)[1].lower()
        if ext not in MIME:
            return self._send(404, "Not found")
        with open(full, "rb") as fh:
            body = fh.read()
        cache = "no-store" if ext == ".html" else "public, max-age=86400"
        self.send_response(200)
        self.send_header("Content-Type", MIME[ext])
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    # -- POST -------------------------------------------------------------
    def do_POST(self):
        path = urlparse(self.path).path

        if path == "/do-login":
            try:
                fields = parse_qs(self._body().decode("utf-8"))
            except ApiError as exc:
                return self._send(exc.status, exc.message)
            username = (fields.get("username") or [""])[0]
            password = (fields.get("password") or [""])[0]
            role = check_credentials(username, password)
            if not role:
                print("[auth] failed login for %r from %s" % (username, self.address_string()))
                return self._login_page("Wrong username or password.")
            token = create_session(role)
            cookie = ("%s=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Strict"
                      % (COOKIE_NAME, token, SESSION_TTL))
            return self._redirect("/", [("Set-Cookie", cookie)])

        sess = self._session()
        if not sess:
            return self._json(401, {"error": "Not signed in", "login": "/login"})
        role = sess["role"]
        username = CONFIG["parents"][role]["username"]

        try:
            payload = self._json_body()
            if path == "/api/day":
                iso = str(payload.get("date") or "")
                if not DATE_RE.match(iso):
                    raise ApiError(400, "date must be YYYY-MM-DD")
                try:
                    date.fromisoformat(iso)
                except ValueError:
                    raise ApiError(400, "not a real date")
                wants, note = parse_changes(payload.get("changes") or {}, role)
                base_rev = payload.get("base_rev")
                base_rev = int(base_rev) if isinstance(base_rev, (int, float)) else None
                with _data_lock:
                    day, changed = change_day(iso, wants, note, role, username, base_rev)
                    if changed:
                        write_data()
                    return self._json(200, {"rev": _data["rev"], "date": iso, "day": day,
                                            "changed": changed})

            if path == "/api/range":
                wants, note = parse_changes(payload.get("changes") or {}, role)
                start, end = str(payload.get("from") or ""), str(payload.get("to") or "")
                if not (DATE_RE.match(start) and DATE_RE.match(end)):
                    raise ApiError(400, "from/to must be YYYY-MM-DD")
                weekdays = payload.get("weekdays")
                weekdays = {int(w) for w in weekdays if 0 <= int(w) <= 6} if weekdays else None
                changed, skipped = {}, []
                with _data_lock:
                    for iso in daterange(start, end, weekdays):
                        try:
                            day, did = change_day(iso, wants, note, role, username)
                        except ApiError as exc:
                            if exc.status == 409:
                                skipped.append(iso)
                                continue
                            raise
                        if did:
                            changed[iso] = day
                    if changed:
                        write_data()
                    return self._json(200, {"rev": _data["rev"], "changed": changed,
                                           "skipped_locked": skipped})

            if path == "/api/account":
                changed = update_account(
                    role,
                    str(payload.get("current_password") or ""),
                    payload.get("display_name"),
                    payload.get("username"),
                    payload.get("new_password") or None,
                )
                if changed:
                    print("[account] %s updated: %s" % (role, ", ".join(changed)))
                return self._json(200, {"changed": changed, "parents": public_parents(),
                                        "me": self._me(sess)})

            if path == "/api/lock":
                iso = str(payload.get("date") or "")
                if not DATE_RE.match(iso):
                    raise ApiError(400, "date must be YYYY-MM-DD")
                want = bool(payload.get("locked"))
                with _data_lock:
                    day, changed = change_day(iso, [("lock", role, want)], None, role, username)
                    if changed:
                        write_data()
                    return self._json(200, {"rev": _data["rev"], "date": iso, "day": day,
                                            "changed": changed})
        except ApiError as exc:
            return self._json(exc.status, dict({"error": exc.message}, **exc.payload))
        except Exception as exc:  # pragma: no cover - defensive
            print("[error] %s %s: %r" % (self.command, path, exc))
            return self._json(500, {"error": "Server error"})

        return self._json(404, {"error": "Unknown endpoint"})


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


# --------------------------------------------------------------------------- cli

HELP = """Custody Calendar - shared 50/50 custody planner

  python3 server.py [port]           start the server (port also from config.json)
  python3 server.py --set-user mum   set mum's name, username and password (interactive)
  python3 server.py --set-user dad   same for dad
  python3 server.py --list-users     show both accounts
  python3 server.py --hash-password  print a SHA-256 hash to put in config.json by hand
  python3 server.py --check-holidays [year]    print the computed holiday table
  python3 server.py --refresh-holidays [year]  re-fetch holidays from date.nager.at

There are always exactly two accounts, "mum" and "dad" - that is what the calendar is about.
You do not create users, you fill these two in (here or in config.json, or from the page itself
via the account button once you can sign in).
"""


def cmd_list_users():
    print("Accounts in %s:" % CONFIG_PATH)
    for role in ROLES:
        p = CONFIG["parents"][role]
        how = ("hashed password" if p.get("password_hash")
               else "plain password" if p.get("password") else "NO PASSWORD SET")
        print("  %-4s %-16s %-22s %s" % (role, p["username"], p["display_name"], how))


def cmd_set_user(role):
    import getpass
    if role not in ROLES:
        sys.exit("Role must be 'mum' or 'dad' (got %r)." % role)
    p = CONFIG["parents"][role]
    print("Setting up the %s account. Press Enter to keep the current value." % role)
    name = input("  Display name [%s]: " % p["display_name"]).strip() or p["display_name"]
    user = input("  Username     [%s]: " % p["username"]).strip().lower() or p["username"]
    pwd = getpass.getpass("  Password (blank = keep current): ")
    if pwd and pwd != getpass.getpass("  Repeat password: "):
        sys.exit("Passwords do not match - nothing changed.")
    if not USERNAME_RE.match(user):
        sys.exit("The username may use a-z, 0-9, dot, dash and underscore only.")
    if user == CONFIG["parents"][OTHER[role]]["username"]:
        sys.exit("That username already belongs to the other parent.")
    with _config_lock:
        p["display_name"] = name
        p["username"] = user
        if pwd:
            p["password_hash"] = hash_password(pwd)
            p.pop("password", None)
        save_config()
    print("Saved to %s" % CONFIG_PATH)
    cmd_list_users()


def cmd_check_holidays(year, refresh=False):
    holidays = get_holidays(year, refresh) if refresh else bg_holidays(year)
    names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    print("Holidays for %d (%s)  -  Orthodox Easter: %s"
          % (year, CONFIG["country"], orthodox_easter(year).isoformat()))
    for iso in sorted(holidays):
        info = holidays[iso]
        print("  %s %s  %-14s %-34s %s" % (
            iso, names[date.fromisoformat(iso).weekday()], info["type"],
            info["name_en"], info["name_bg"]))
    print("  %d entries" % len(holidays))


def main():
    try:
        sys.stdout.reconfigure(line_buffering=True)   # readable logs under systemd / redirects
    except Exception:
        pass
    args = sys.argv[1:]
    if args and args[0] in ("--help", "-h"):
        print(HELP)
        return
    if args and args[0] == "--list-users":
        cmd_list_users()
        return
    if args and args[0] == "--set-user":
        cmd_set_user(args[1] if len(args) > 1 else "")
        return
    if args and args[0] == "--hash-password":
        import getpass
        pwd = getpass.getpass("Password: ")
        if pwd != getpass.getpass("Repeat:   "):
            sys.exit("Passwords do not match.")
        print(hash_password(pwd))
        return
    if args and args[0] in ("--check-holidays", "--refresh-holidays"):
        year = int(args[1]) if len(args) > 1 else date.today().year
        cmd_check_holidays(year, refresh=args[0] == "--refresh-holidays")
        return

    load_data()
    globals()["_config_mtime"] = _config_mtime_now()
    if not os.path.exists(CONFIG_PATH):
        print("! No config.json found - copy config.json.example and set the names/passwords.")
    for role in ROLES:
        p = CONFIG["parents"][role]
        if not p.get("password") and not p.get("password_hash"):
            print("! %s (%s) has no password in config.json - nobody can sign in as them."
                  % (p["display_name"], role))
        elif p.get("password") == "changeme":
            print("! %s (%s) still uses the example password 'changeme' - change it in config.json."
                  % (p["display_name"], role))
    print("%s serving on http://0.0.0.0:%d   (%s = %s, %s = %s)"
          % (INSTANCE, PORT,
             CONFIG["parents"]["mum"]["display_name"], CONFIG["parents"]["mum"]["username"],
             CONFIG["parents"]["dad"]["display_name"], CONFIG["parents"]["dad"]["username"]))
    try:
        ThreadedHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
