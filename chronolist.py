#!/usr/bin/env python3
"""
Chronolist v2.7.0: füllt öffentliche YouTube-Playlists "<Kanal> <Jahr> in Upload-Reihenfolge"
mit allen Videos eines frei wählbaren Kanals aus dem jeweiligen Jahr (alt -> neu).

https://github.com/LooneyN/chronolist  ·  MIT License
Vollständig mit Claude (Anthropic) programmiert.

Oberfläche im Neon-/Terminal-Stil.
Schrift: "Rajdhani" (Google Fonts) wird genutzt, wenn installiert, sonst Orbitron / Segoe UI.

Benötigt:  pip install requests playwright
           (Browser-Fallback nutzt Microsoft Edge, das bei Windows dabei ist)
Start:     pythonw chronolist.py
Als .exe:  pip install pyinstaller
           pyinstaller --onefile --windowed --name Chronolist --icon assets/chronolist.ico chronolist.py

Zugangsdaten:  credentials.json  (über API SETUP im Programm)
Einstellungen: settings.json     (u. a. Zielkanal, Playlist-Namensvorlage, Update-Check)
Fortschritt:   data/<kanal>/videos_<jahr>.json, collect_<jahr>.json, state_<jahr>.json
"""
import datetime
import json
import math
import queue
import random
import re
import shutil
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import webbrowser
from pathlib import Path
from tkinter import ttk

import requests

try:
    import winreg
except ImportError:  # nicht unter Windows
    winreg = None

# ---------------------------------------------------------------- Einstellungen
# Zugangsdaten stehen NICHT im Code, sondern in credentials.json (wird über API SETUP angelegt).
# Kanal wird im Programm eingegeben und in settings.json gespeichert.
# Standard-Vorlagen; in settings.json über "playlist_template" / "description_template" änderbar.
DEFAULT_PLAYLIST_TEMPLATE = "{channel} {year} in Upload-Reihenfolge"
DEFAULT_DESCRIPTION_TEMPLATE = "Alle Videos von {channel} aus {year} in Upload-Reihenfolge."
PLAYLIST_TEMPLATE = DEFAULT_PLAYLIST_TEMPLATE
DESCRIPTION_TEMPLATE = DEFAULT_DESCRIPTION_TEMPLATE
PRIVACY = "public"
APP_NAME = "Chronolist"
VERSION = "2.7.0"
BUILD = 35
REPO = "LooneyN/chronolist"
REPO_URL = f"https://github.com/{REPO}"
# ------------------------------------------------------------------------------

if getattr(sys, "frozen", False):
    HERE = Path(sys.executable).resolve().parent
else:
    HERE = Path(__file__).resolve().parent

TOKEN_FILE = HERE / "token.json"
SETTINGS_FILE = HERE / "settings.json"
CREDENTIALS_FILE = HERE / "credentials.json"
DATA_DIR = HERE / "data"
BROWSER_PROFILE = HERE / "browser_profile"
API = "https://www.googleapis.com/youtube/v3"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/youtube"
BATCH_SIZE = 15                 # Browser-Fallback: Videos pro Anfrage
BATCH_PAUSE = (15, 30)          # Sekunden zwischen zwei Paketen
COOLDOWN_EVERY = 150            # nach so vielen Videos ein kurzer Cooldown
COOLDOWN_PAUSE = (54, 66)       # Sekunden (~60 s)
LONG_BREAK_EVERY = 750          # nach so vielen Videos eine lange Pause
LONG_BREAK_PAUSE = (540, 660)   # Sekunden (~10 min)
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

# --- aktiver Kanal (wird von der App gesetzt)
CHANNEL = {}
CHAN_DIR = DATA_DIR / "_none"
YEARS = []


def channel_slug(ch):
    raw = (ch.get("handle") or ch.get("id") or "channel").lower().lstrip("@")
    return re.sub(r"[^a-z0-9._-]", "", raw) or "channel"


def set_channel(ch):
    global CHANNEL, CHAN_DIR, YEARS
    CHANNEL = dict(ch or {})
    CHAN_DIR = DATA_DIR / channel_slug(CHANNEL) if CHANNEL else DATA_DIR / "_none"
    fy = CHANNEL.get("first_year")
    YEARS = list(range(int(fy), datetime.date.today().year + 1)) if fy else []


def channel_name():
    return CHANNEL.get("title") or CHANNEL.get("handle") or "CHANNEL"


def parse_channel(text):
    """Akzeptiert @handle, handle, Kanal-URL oder Kanal-ID (UC...)."""
    t = (text or "").strip()
    if not t:
        return None
    m = re.search(r"(UC[0-9A-Za-z_-]{22})", t)
    if m:
        return {"id": m.group(1)}
    m = re.search(r"youtube\.com/(@[^/?#\s]+)", t)
    if m:
        return {"handle": m.group(1)}
    t = t.split("/")[-1]
    return {"handle": t if t.startswith("@") else "@" + t}


def get_creds():
    c = load(CREDENTIALS_FILE, {})
    return (c.get("client_id") or "").strip(), (c.get("client_secret") or "").strip()


def _us_pacific_offset(utc_dt):
    """UTC-Versatz der US-Westküste (PST -8 / PDT -7), ohne tzdata.
    Sommerzeit: 2. Sonntag im März 02:00 bis 1. Sonntag im November 02:00 (Ortszeit)."""
    y = utc_dt.year

    def nth_sunday(month, n):
        d = datetime.datetime(y, month, 1)
        d += datetime.timedelta(days=(6 - d.weekday()) % 7)
        return d + datetime.timedelta(weeks=n - 1)
    start_utc = nth_sunday(3, 2).replace(hour=2) + datetime.timedelta(hours=8)    # 02:00 PST
    end_utc = nth_sunday(11, 1).replace(hour=2) + datetime.timedelta(hours=7)     # 02:00 PDT
    return -7 if start_utc <= utc_dt.replace(tzinfo=None) < end_utc else -8


def next_quota_reset():
    """Nächster Reset des API-Kontingents (00:00 Pacific Time) als datetime mit Zeitzone."""
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    off = _us_pacific_offset(now_utc)
    pt_now = now_utc + datetime.timedelta(hours=off)
    pt_midnight = (pt_now + datetime.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    reset_utc = pt_midnight - datetime.timedelta(hours=_us_pacific_offset(pt_midnight - datetime.timedelta(hours=off)))
    return reset_utc.replace(tzinfo=datetime.timezone.utc)


def fmt_reset(reset):
    reset_local = reset.astimezone()
    today = datetime.datetime.now().date()
    day = "TODAY" if reset_local.date() == today else "TOMORROW" if reset_local.date() == today + datetime.timedelta(days=1) \
        else reset_local.strftime("%d.%m.")
    return f"{day} {reset_local.strftime('%H:%M')}"


# --- gemerktes Tageslimit (gilt pro Google-Projekt, also für alle Kanäle)
QUOTA_FILE = HERE / "quota.json"


def quota_blocked(kind):
    """kind = 'units' oder 'search'. Liefert die Reset-Zeit, falls das Limit heute schon erreicht wurde."""
    until = load(QUOTA_FILE, {}).get(kind)
    if until and time.time() < until:
        return datetime.datetime.fromtimestamp(until, datetime.timezone.utc)
    return None


def mark_quota(kind):
    q = load(QUOTA_FILE, {})
    q[kind] = next_quota_reset().timestamp()
    save(QUOTA_FILE, q)


def quota_reset_local():
    """Wann das API-Kontingent zurückgesetzt wird (00:00 Pacific Time), in deiner Ortszeit,
    z. B. 'TODAY 08:00' oder 'TOMORROW 09:00'. Berücksichtigt beide Zeitumstellungen."""
    reset_utc = next_quota_reset().replace(tzinfo=None)
    reset_local = reset_utc.replace(tzinfo=datetime.timezone.utc).astimezone()
    today = datetime.datetime.now().date()
    day = "TODAY" if reset_local.date() == today else "TOMORROW" if reset_local.date() == today + datetime.timedelta(days=1) \
        else reset_local.strftime("%d.%m.")
    return f"{day} {reset_local.strftime('%H:%M')}"


def archive_file(): return CHAN_DIR / "archive_scan.json"


def archive_cutoff():
    """Ältestes Jahr, das die (von YouTube gekappte) Upload-Liste nur noch teilweise enthält.
    Dieses und ältere Jahre gehen direkt über die Datumssuche. None = Liste vollständig / unbekannt."""
    a = load(archive_file(), {})
    if not a.get("done") or not a.get("found"):
        return None
    if a.get("seen", 0) >= a.get("total", 0) - 50:
        return None
    return min(int(p[:4]) for p in a["found"].values())
def videos_file(y): return CHAN_DIR / f"videos_{y}.json"
def collect_file(y): return CHAN_DIR / f"collect_{y}.json"
def state_file(y): return CHAN_DIR / f"state_{y}.json"
def playlist_title(y): return PLAYLIST_TEMPLATE.format(channel=channel_name(), year=y)
def playlist_description(y): return DESCRIPTION_TEMPLATE.format(channel=channel_name(), year=y)


def load(path, default):
    try:
        return json.loads(path.read_text("utf-8")) if path.exists() else default
    except (OSError, ValueError):
        return default


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), "utf-8")


def year_info(y):
    vids = load(videos_file(y), None)
    if vids is None:
        return {"code": "scanning" if collect_file(y).exists() else "none", "done": 0, "total": 0}
    n = len(vids)
    done = min(load(state_file(y), {}).get("next", 0), n)
    code = "done" if done >= n else ("progress" if done else "ready")
    return {"code": code, "done": done, "total": n}


# ------------------------------------------------------------ Windows-Autostart
def autostart_command():
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    return f'"{pyw if pyw.exists() else exe}" "{Path(__file__).resolve()}"'


def get_windows_autostart():
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP_NAME)
            return True
    except OSError:
        return False


def set_windows_autostart(on):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, autostart_command())
        else:
            try:
                winreg.DeleteValue(k, APP_NAME)
            except FileNotFoundError:
                pass


def _template_ok(t):
    try:
        t.format(channel="X", year=2000)
        return True
    except (KeyError, IndexError, ValueError, AttributeError):
        return False


def apply_templates(settings):
    """Playlist-Titel und -Beschreibung aus settings.json übernehmen (Platzhalter: {channel}, {year})."""
    global PLAYLIST_TEMPLATE, DESCRIPTION_TEMPLATE
    pt = settings.get("playlist_template") or DEFAULT_PLAYLIST_TEMPLATE
    dt = settings.get("description_template") or DEFAULT_DESCRIPTION_TEMPLATE
    PLAYLIST_TEMPLATE = pt if isinstance(pt, str) and _template_ok(pt) else DEFAULT_PLAYLIST_TEMPLATE
    DESCRIPTION_TEMPLATE = dt if isinstance(dt, str) and _template_ok(dt) else DEFAULT_DESCRIPTION_TEMPLATE


def version_tuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:3]) or (0,)


def check_for_update(out):
    """Fragt einmal die neueste GitHub-Release ab. Sendet ('update', version, url), wenn sie neuer ist.
    Es wird nichts übertragen außer der normalen Anfrage an die GitHub-API."""
    try:
        r = requests.get(f"https://api.github.com/repos/{REPO}/releases/latest", timeout=10,
                         headers={"Accept": "application/vnd.github+json",
                                  "User-Agent": f"{APP_NAME}/{VERSION}"})
        if not r.ok:
            return
        j = r.json()
        tag = (j.get("tag_name") or "").lstrip("vV")
        if tag and version_tuple(tag) > version_tuple(VERSION):
            out.put(("update", tag, j.get("html_url") or f"{REPO_URL}/releases/latest"))
    except (requests.RequestException, ValueError):
        pass


# ------------------------------------------------------------------- Worker
class Stopped(Exception):
    pass


class QuotaExceeded(Exception):
    pass


class SearchLimit(QuotaExceeded):
    """Eigenes Tageslimit von YouTube für die Suche (standardmäßig ca. 100 Aufrufe pro Tag)."""


class ApiError(Exception):
    def __init__(self, status, reasons, text):
        super().__init__(f"HTTP {status}: {text[:300]}")
        self.reasons = reasons


# ------------------------------------------------------------ Browser-Fallback
# Fügt Videos über die eingeloggte YouTube-Webseite hinzu (wie "Speichern" in der Playlist),
# ohne API-Kontingent. Läuft in einem eigenen Edge-Profil im Programmordner.
ADD_JS = """
async ({pid, vid}) => {
  const cfg = window.ytcfg;
  if (!cfg || !cfg.get) return {fatal: 'NO_YTCFG'};
  if (!cfg.get('LOGGED_IN')) return {fatal: 'NOT_LOGGED_IN'};
  const m = document.cookie.match(/(?:^|;\\s*)SAPISID=([^;]+)/) ||
            document.cookie.match(/(?:^|;\\s*)__Secure-3PAPISID=([^;]+)/);
  if (!m) return {fatal: 'NO_SAPISID'};
  const origin = 'https://www.youtube.com';
  const ts = Math.floor(Date.now() / 1000);
  const buf = await crypto.subtle.digest('SHA-1', new TextEncoder().encode(ts + ' ' + m[1] + ' ' + origin));
  const hash = Array.from(new Uint8Array(buf)).map(b => b.toString(16).padStart(2, '0')).join('');
  const headers = {
    'Content-Type': 'application/json',
    'Authorization': 'SAPISIDHASH ' + ts + '_' + hash,
    'X-Origin': origin,
    'X-Goog-AuthUser': String(cfg.get('SESSION_INDEX') || 0),
    'X-Youtube-Client-Name': '1',
    'X-Youtube-Client-Version': cfg.get('INNERTUBE_CLIENT_VERSION'),
  };
  if (cfg.get('DELEGATED_SESSION_ID')) headers['X-Goog-PageId'] = cfg.get('DELEGATED_SESSION_ID');
  const r = await fetch('/youtubei/v1/browse/edit_playlist?key=' + cfg.get('INNERTUBE_API_KEY') + '&prettyPrint=false', {
    method: 'POST', credentials: 'include', headers,
    body: JSON.stringify({context: cfg.get('INNERTUBE_CONTEXT'), playlistId: pid,
                          actions: [{action: 'ACTION_ADD_VIDEO', addedVideoId: vid}]}),
  });
  let j = {};
  try { j = await r.json(); } catch (e) {}
  return {http: r.status, status: j.status || null,
          err: j.error ? (j.error.status + ' ' + (j.error.message || '')) : null};
}
"""


YT_CALL_JS = """
async ({path, body}) => {
  const cfg = window.ytcfg;
  if (!cfg || !cfg.get) return {fatal: 'NO_YTCFG'};
  if (!cfg.get('LOGGED_IN')) return {fatal: 'NOT_LOGGED_IN'};
  const m = document.cookie.match(/(?:^|;\\s*)SAPISID=([^;]+)/) ||
            document.cookie.match(/(?:^|;\\s*)__Secure-3PAPISID=([^;]+)/);
  if (!m) return {fatal: 'NO_SAPISID'};
  const origin = 'https://www.youtube.com';
  const ts = Math.floor(Date.now() / 1000);
  const buf = await crypto.subtle.digest('SHA-1', new TextEncoder().encode(ts + ' ' + m[1] + ' ' + origin));
  const hash = Array.from(new Uint8Array(buf)).map(b => b.toString(16).padStart(2, '0')).join('');
  const headers = {
    'Content-Type': 'application/json',
    'Authorization': 'SAPISIDHASH ' + ts + '_' + hash,
    'X-Origin': origin,
    'X-Goog-AuthUser': String(cfg.get('SESSION_INDEX') || 0),
    'X-Youtube-Client-Name': '1',
    'X-Youtube-Client-Version': cfg.get('INNERTUBE_CLIENT_VERSION'),
  };
  if (cfg.get('DELEGATED_SESSION_ID')) headers['X-Goog-PageId'] = cfg.get('DELEGATED_SESSION_ID');
  const r = await fetch('/youtubei/v1/' + path + '?key=' + cfg.get('INNERTUBE_API_KEY') + '&prettyPrint=false', {
    method: 'POST', credentials: 'include', headers,
    body: JSON.stringify(Object.assign({context: cfg.get('INNERTUBE_CONTEXT')}, body)),
  });
  let j = null;
  try { j = await r.json(); } catch (e) {}
  return {http: r.status, json: j};
}
"""


def walk_json(node, found_items, found_tokens):
    """Sucht Playlist-Einträge (videoId + setVideoId) und Fortsetzungs-Tokens in einer InnerTube-Antwort."""
    if isinstance(node, dict):
        if "videoId" in node and "setVideoId" in node:
            found_items.append({"vid": node["videoId"], "set": node["setVideoId"]})
            return
        cc = node.get("continuationCommand")
        if isinstance(cc, dict) and cc.get("token"):
            found_tokens.append(cc["token"])
        for v in node.values():
            walk_json(v, found_items, found_tokens)
    elif isinstance(node, list):
        for v in node:
            walk_json(v, found_items, found_tokens)


class RelistNeeded(Exception):
    pass


class BrowserFailed(Exception):
    pass


class BrowserUplink:
    def __init__(self, worker):
        self.w = worker
        self.pw = self.ctx = self.page = None
        self.count = 0

    def start(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise BrowserFailed("PLAYWRIGHT NOT INSTALLED // RUN: pip install playwright")
        self.w.log("BROWSER UPLINK // LAUNCHING EDGE ...")
        self.pw = sync_playwright().start()
        opts = dict(user_data_dir=str(BROWSER_PROFILE), headless=False,
                    args=["--disable-blink-features=AutomationControlled"],
                    ignore_default_args=["--enable-automation"], viewport=None)
        try:
            self.ctx = self.pw.chromium.launch_persistent_context(channel="msedge", **opts)
        except Exception:  # noqa: BLE001
            try:
                self.ctx = self.pw.chromium.launch_persistent_context(**opts)
            except Exception as e:  # noqa: BLE001
                self.close()
                raise BrowserFailed(f"BROWSER LAUNCH FAILED // {e}")
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        self.open_youtube()
        self.wait_login()

    def open_youtube(self):
        self.page.goto("https://www.youtube.com/", wait_until="domcontentloaded", timeout=60000)
        self.w.sleep(3)

    def logged_in(self):
        try:
            return bool(self.page.evaluate("() => !!(window.ytcfg && ytcfg.get && ytcfg.get('LOGGED_IN'))"))
        except Exception:  # noqa: BLE001
            return False

    def wait_login(self):
        if self.logged_in():
            self.w.log("BROWSER UPLINK // SIGNED IN", "ok")
            return
        self.w.log("SIGN IN TO YOUTUBE IN THE EDGE WINDOW // WAITING (MAX 15 MIN)", "warn")
        self.w.emit("browser_login")
        end = time.time() + 15 * 60
        while time.time() < end:
            self.w.sleep(4)
            if "youtube.com" in (self.page.url or "") and self.logged_in():
                self.w.log("BROWSER UPLINK // SIGNED IN", "ok")
                self.w.emit("mode", "browser")
                return
        raise BrowserFailed("NO YOUTUBE SIGN-IN WITHIN 15 MIN")

    def add(self, playlist_id, video_id):
        """True = drin, False = Video nicht verfügbar (überspringen)."""
        if self.count and self.count % 150 == 0:
            self.open_youtube()  # Sitzungsdaten auffrischen
        for attempt in range(3):
            try:
                if "youtube.com" not in (self.page.url or ""):
                    self.open_youtube()
                res = self.page.evaluate(ADD_JS, {"pid": playlist_id, "vid": video_id})
            except Stopped:
                raise
            except Exception as e:  # noqa: BLE001
                if attempt == 2:
                    raise BrowserFailed(f"BROWSER ERROR // {str(e)[:200]}")
                self.w.sleep(10)
                self.open_youtube()
                continue
            if res.get("fatal") == "NOT_LOGGED_IN":
                self.wait_login()
                continue
            if res.get("fatal"):
                if attempt == 2:
                    raise BrowserFailed(f"BROWSER UPLINK // {res['fatal']}")
                self.open_youtube()
                continue
            if res.get("status") == "STATUS_SUCCEEDED":
                self.count += 1
                return True
            http, err = res.get("http"), res.get("err") or res.get("status")
            if http in (401, 403):
                raise BrowserFailed(f"YOUTUBE REFUSED ACCESS ({http}) // CHECK SIGNED-IN CHANNEL OWNS THE PLAYLIST")
            if http == 429:
                self.w.log("YOUTUBE RATE LIMIT // COOLING DOWN 10 MIN", "warn")
                self.w.sleep(600, "RATE LIMIT")
                continue
            if http == 400 or res.get("status") == "STATUS_FAILED":
                self.w.log(f"SKIPPED {video_id} // {err}", "warn")
                return False
            if attempt == 2:
                raise BrowserFailed(f"BROWSER UPLINK // HTTP {http} {err}")
            self.w.sleep(15)
        raise BrowserFailed("BROWSER UPLINK // GAVE UP")

    def yt_call(self, path, body):
        for attempt in range(3):
            try:
                if "youtube.com" not in (self.page.url or ""):
                    self.open_youtube()
                res = self.page.evaluate(YT_CALL_JS, {"path": path, "body": body})
            except Stopped:
                raise
            except Exception as e:  # noqa: BLE001
                if attempt == 2:
                    raise BrowserFailed(f"BROWSER ERROR // {str(e)[:200]}")
                self.open_youtube()
                continue
            if res.get("fatal") == "NOT_LOGGED_IN":
                self.wait_login()
                continue
            if res.get("fatal"):
                if attempt == 2:
                    raise BrowserFailed(f"BROWSER UPLINK // {res['fatal']}")
                self.open_youtube()
                continue
            if res.get("http") == 200 and res.get("json") is not None:
                return res["json"]
            if attempt == 2:
                raise BrowserFailed(f"BROWSER UPLINK // HTTP {res.get('http')} ON {path}")
            time.sleep(5)
        raise BrowserFailed("BROWSER UPLINK // GAVE UP")

    def add_many(self, playlist_id, video_ids):
        """Mehrere Videos in einer Anfrage. True = alle drin, False = abgelehnt (einzeln prüfen)."""
        self.calls = getattr(self, "calls", 0) + 1
        if self.calls % 10 == 0:
            self.open_youtube()  # Sitzungsdaten auffrischen
        body = {"playlistId": playlist_id,
                "actions": [{"action": "ACTION_ADD_VIDEO", "addedVideoId": v} for v in video_ids]}
        for attempt in range(3):
            try:
                if "youtube.com" not in (self.page.url or ""):
                    self.open_youtube()
                res = self.page.evaluate(YT_CALL_JS, {"path": "browse/edit_playlist", "body": body})
            except Stopped:
                raise
            except Exception as e:  # noqa: BLE001
                if attempt == 2:
                    raise BrowserFailed(f"BROWSER ERROR // {str(e)[:200]}")
                self.w.sleep(10)
                self.open_youtube()
                continue
            if res.get("fatal") == "NOT_LOGGED_IN":
                self.wait_login()
                continue
            if res.get("fatal"):
                if attempt == 2:
                    raise BrowserFailed(f"BROWSER UPLINK // {res['fatal']}")
                self.open_youtube()
                continue
            http, j = res.get("http"), res.get("json") or {}
            if http == 200 and j.get("status") == "STATUS_SUCCEEDED":
                self.count += len(video_ids)
                return True
            if http in (401, 403):
                raise BrowserFailed(f"YOUTUBE REFUSED ACCESS ({http}) // CHECK SIGNED-IN CHANNEL OWNS THE PLAYLIST")
            if http == 429:
                self.w.log("YOUTUBE RATE LIMIT // COOLING DOWN 10 MIN", "warn")
                self.w.sleep(600, "RATE LIMIT")
                continue
            if http == 400 or j.get("status") == "STATUS_FAILED":
                return False
            if attempt == 2:
                raise BrowserFailed(f"BROWSER UPLINK // HTTP {http}")
            self.w.sleep(15)
        raise BrowserFailed("BROWSER UPLINK // GAVE UP")

    def create_playlist(self, title, description):
        j = self.yt_call("playlist/create", {"title": title, "privacyStatus": PRIVACY.upper()})
        pid = j.get("playlistId")
        if not pid:
            raise BrowserFailed("PLAYLIST CREATE FAILED")
        try:
            self.yt_call("browse/edit_playlist", {"playlistId": pid, "actions": [
                {"action": "ACTION_SET_PLAYLIST_DESCRIPTION", "playlistDescription": description}]})
        except BrowserFailed:
            self.w.log("PLAYLIST DESCRIPTION NOT SET // ADD IT MANUALLY IF NEEDED", "warn")
        return pid

    def list_items(self, playlist_id):
        items, tokens = [], []
        walk_json(self.yt_call("browse", {"browseId": "VL" + playlist_id}), items, tokens)
        seen_tokens = set()
        while tokens:
            tok = tokens.pop(0)
            if tok in seen_tokens:
                continue
            seen_tokens.add(tok)
            new_items, new_tokens = [], []
            walk_json(self.yt_call("browse", {"continuation": tok}), new_items, new_tokens)
            items += new_items
            tokens += new_tokens
            time.sleep(1)
        return items

    def remove(self, playlist_id, item):
        j = self.yt_call("browse/edit_playlist", {"playlistId": playlist_id, "actions": [
            {"action": "ACTION_REMOVE_VIDEO", "setVideoId": item["set"], "removedVideoId": item["vid"]}]})
        if j.get("status") != "STATUS_SUCCEEDED":
            raise BrowserFailed(f"REMOVE FAILED // {j.get('status')}")
        time.sleep(random.uniform(2, 4))

    def close(self):
        for fn in (lambda: self.ctx and self.ctx.close(), lambda: self.pw and self.pw.stop()):
            try:
                fn()
            except Exception:  # noqa: BLE001
                pass
        self.pw = self.ctx = self.page = None


class Worker(threading.Thread):
    def __init__(self, years, limit, out, fallback=False, resolve=None, check_only=False):
        super().__init__(daemon=True)
        self.resolve_target = resolve
        self.check_only = check_only
        self.ignore_stop = False     # für den Abschluss-Check nach STOP
        self.skip_ahead = False      # nicht scanbare Jahre überspringen
        self.cur = None              # (jahr, videos) des laufenden Jahres
        self.years = years
        self.limit = limit
        self.out = out
        self.fallback = fallback
        self.mode = "api"
        self.browser = None
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()
        self.tok = None

    # --- Hilfen
    def emit(self, *msg):
        self.out.put(msg)

    def log(self, text, level="info"):
        self.emit("log", text, level)

    def sleep(self, seconds, label=None):
        """Wartet unterbrechbar. Ab 10 s wird die Wartezeit mit Countdown angezeigt."""
        if self.ignore_stop:
            time.sleep(min(seconds, 15))
            return
        show = label is not None and seconds >= 10
        end = time.time() + seconds
        if show:
            self.emit("pause", {"kind": "wait", "t": end, "label": label})
        try:
            while True:
                left = end - time.time()
                if left <= 0:
                    return
                if self.stop_event.wait(min(0.25, left)):
                    raise Stopped()
                if self.pause_event.is_set():
                    held = self.hold()
                    end += held
                    if show:
                        self.emit("pause", {"kind": "wait", "t": end, "label": label})
        finally:
            if show:
                self.emit("pause", None)

    def hold(self):
        """Manuelle Pause: hält an, bis RESUME gedrückt wird. Gibt die Pausendauer zurück."""
        t0 = time.time()
        self.emit("pause", {"kind": "manual", "t": t0})
        self.log("PAUSED BY USER", "warn")
        while self.pause_event.is_set():
            if self.stop_event.wait(0.25):
                raise Stopped()
        self.emit("pause", None)
        self.log(f"RESUMED // PAUSED FOR {fmt_secs(time.time() - t0)}", "ok")
        return time.time() - t0

    def check(self):
        if self.ignore_stop:
            return
        if self.stop_event.is_set():
            raise Stopped()
        if self.pause_event.is_set():
            self.hold()

    # --- Ablauf
    def run(self):
        reason = "fertig"
        try:
            if not all(get_creds()):
                raise RuntimeError("NO API CREDENTIALS // OPEN API SETUP")
            self.tok = load(TOKEN_FILE, None) or self.device_login()
            if self.resolve_target is not None:
                reason = "resolved"
                self.emit("channel", self.resolve_channel(self.resolve_target))
                return
            if self.check_only:
                self.manual_check()
                return
            for kind, label in (("units", "API QUOTA"), ("search", "SEARCH LIMIT")):
                r = quota_blocked(kind)
                if r:
                    self.log(f"{label} ALREADY USED UP TODAY // SKIPPING IT UNTIL RESET {fmt_reset(r)}", "warn")
            if self.fallback:
                self.scan_archive()
            queue_ = list(self.years)
            deferred = []
            while queue_ or deferred:
                if not queue_:
                    # übersprungene Jahre am Ende noch einmal versuchen (z. B. nach dem Reset)
                    queue_, deferred, retry = deferred, [], True
                else:
                    retry = False
                y = queue_.pop(0)
                self.check()
                self.emit("current", y)
                self.log(f"// TARGET {y}", "sep")
                try:
                    videos = self.collect(y)
                except QuotaExceeded as e:
                    nxt = self.next_ready_year(y, queue_) if self.skip_ahead else None
                    if nxt is None or retry:
                        raise
                    self.scanning = None
                    why = "DAILY SEARCH LIMIT" if isinstance(e, SearchLimit) else "API QUOTA"
                    self.log(f"{y} NOT SCANNED YET ({why}) // SKIP AHEAD TO {nxt}", "warn")
                    deferred.append(y)
                    queue_.insert(0, nxt)
                    self.emit("select", nxt, None)
                    continue
                if self.fallback and self.mode == "api":
                    self.prefetch(y + 1)
                if not self.fill(y, videos):
                    continue
                # Jahr fertig -> automatisch mit dem nächsten abgeschlossenen Jahr weitermachen
                nxt = y + 1
                while nxt in deferred or (self.skip_ahead and nxt < datetime.date.today().year
                                          and year_info(nxt)["code"] == "done"):
                    nxt += 1
                if nxt < datetime.date.today().year and nxt not in queue_ and nxt not in self.years:
                    queue_.append(nxt)
                    self.emit("select", nxt, y)
                    self.log(f"{y} DONE // AUTO-ADVANCE TO {nxt}", "ok")
            self.log("ALL TARGETS PROCESSED", "ok")
        except Stopped:
            reason = "gestoppt"
            self.log("HALTED BY USER", "warn")
            self.final_check()
        except SearchLimit:
            reason = "quota"
            self.log(f"{getattr(self, 'scanning', None) or ''} SCAN PAUSED // DAILY SEARCH LIMIT (~100 SEARCHES) "
                     f"REACHED // CONTINUES AFTER RESET {quota_reset_local()}".strip(), "warn")
        except QuotaExceeded:
            reason = "quota"
            y = getattr(self, "scanning", None)
            if y is not None:
                self.log(f"{y} NOT SCANNED YET // THE VIDEO SCAN NEEDS API QUOTA (THE BROWSER FALLBACK "
                         f"CAN ONLY ADD) // CONTINUES AFTER RESET {quota_reset_local()}", "warn")
            else:
                self.log(f"DAILY API QUOTA REACHED // PROGRESS SAVED // RESET {quota_reset_local()}", "warn")
        except BrowserFailed as e:
            reason = "fehler"
            self.log(f"BROWSER FALLBACK FAILED // {e}", "err")
        except Exception as e:  # noqa: BLE001
            reason = "fehler"
            self.log(f"ERROR // {e}", "err")
        finally:
            if self.browser:
                self.browser.close()
            self.emit("current", None)
            self.emit("code_done")
            self.emit("done", reason)

    # --- Login
    def device_login(self):
        r = requests.post("https://oauth2.googleapis.com/device/code",
                          data={"client_id": get_creds()[0], "scope": SCOPE}, timeout=30)
        if not r.ok:
            self.emit("api", False)
            raise RuntimeError(f"GOOGLE SIGN-IN REFUSED ({r.status_code}) // CHECK CLIENT ID IN API SETUP")
        d = r.json()
        self.emit("code", d["verification_url"], d["user_code"])
        self.log("WAITING FOR GOOGLE AUTH ...")
        interval = d.get("interval", 5)
        while True:
            self.sleep(interval)
            t = requests.post(TOKEN_URL, data={
                "client_id": get_creds()[0],
                "client_secret": get_creds()[1],
                "device_code": d["device_code"],
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            }, timeout=30).json()
            err = t.get("error")
            if err == "authorization_pending":
                continue
            if err == "slow_down":
                interval += 5
                continue
            if err:
                self.emit("api", False)
                raise RuntimeError(f"AUTH FAILED: {t}")
            t["expires_at"] = time.time() + t["expires_in"] - 60
            save(TOKEN_FILE, t)
            self.emit("code_done")
            self.log("AUTH SUCCESSFUL", "ok")
            return t

    def refresh(self):
        if "refresh_token" not in self.tok:
            self.tok = self.device_login()
            return
        r = requests.post(TOKEN_URL, data={
            "client_id": get_creds()[0],
            "client_secret": get_creds()[1],
            "refresh_token": self.tok["refresh_token"],
            "grant_type": "refresh_token",
        }, timeout=30).json()
        if "access_token" not in r:
            self.tok = self.device_login()
            return
        self.tok["access_token"] = r["access_token"]
        self.tok["expires_at"] = time.time() + r["expires_in"] - 60
        save(TOKEN_FILE, self.tok)

    # --- API
    def call(self, method, path, params=None, body=None):
        # Heute schon festgestellt? Dann gar nicht erst fragen.
        if path == "search" and quota_blocked("search"):
            raise SearchLimit()
        if quota_blocked("units"):
            raise QuotaExceeded()
        tries = 8
        retries = tries - 1                       # so oft wird höchstens neu versucht
        search_retries = 2                        # Suche: danach gilt das Tageslimit der Suche
        for attempt in range(tries):
            self.check()
            try:
                if time.time() > self.tok.get("expires_at", 0):
                    self.refresh()
                r = requests.request(
                    method, f"{API}/{path}", params=params, json=body,
                    headers={"Authorization": f"Bearer {self.tok['access_token']}"},
                    timeout=30,
                )
            except requests.RequestException:
                self.log("NO SIGNAL // RETRY IN 15 S", "warn")
                self.sleep(15, "NO SIGNAL")
                continue
            if r.ok:
                if not getattr(self, "_api_reported", False):
                    self._api_reported = True
                    self.emit("api", True)
                return r.json() if r.content else {}
            if r.status_code == 401:
                self.refresh()
                continue
            try:
                errors = r.json().get("error", {}).get("errors", [])
            except ValueError:
                errors = []
            reasons = {e.get("reason") for e in errors}
            last_fail = f"HTTP {r.status_code} {','.join(sorted(x for x in reasons if x)) or ''}".strip()
            if r.status_code >= 500:
                if attempt >= retries:
                    break
                self.log(f"YOUTUBE API HICCUP // {last_fail} // RETRY {attempt + 1}/{retries}", "warn")
                self.sleep(min(2 ** attempt, 60), "RETRY")
                continue
            if reasons & {"quotaExceeded", "dailyLimitExceeded"}:
                mark_quota("units")
                self.emit("api", True)
                raise QuotaExceeded()
            if "rateLimitExceeded" in reasons:
                limit = search_retries if path == "search" else retries
                if attempt >= limit:
                    if path == "search":
                        mark_quota("search")
                        raise SearchLimit()   # anhaltend = Tageslimit der Suche, Warten bringt nichts
                    break
                self.log(f"YOUTUBE API RATE LIMIT // RETRY {attempt + 1}/{limit}", "warn")
                self.sleep(min(2 ** attempt, 60), "RETRY")
                continue
            raise ApiError(r.status_code, reasons, r.text)
        raise RuntimeError(f"TOO MANY FAILED ATTEMPTS // {method} {path} // LAST: {locals().get('last_fail', 'NO SIGNAL')}")

    def next_ready_year(self, after, queue_):
        """Nächstes Jahr nach `after`, dessen Videoliste schon fertig ist und das noch nicht voll ist."""
        for y in range(after + 1, datetime.date.today().year):
            if y in queue_:
                continue
            info = year_info(y)
            if info["code"] in ("ready", "progress"):
                return y
        return None

    # --- Kanal auflösen (Name, ID, Startjahr)
    def resolve_channel(self, target):
        q = {"id": target["id"]} if target.get("id") else {"forHandle": target["handle"]}
        self.log(f"RESOLVING CHANNEL {target.get('handle') or target.get('id')} ...")
        items = self.call("GET", "channels", {"part": "snippet", **q}).get("items", [])
        if not items:
            raise RuntimeError(f"CHANNEL {target.get('handle') or target.get('id')} NOT FOUND")
        sn = items[0]["snippet"]
        handle = sn.get("customUrl") or target.get("handle") or ""
        if handle and not handle.startswith("@"):
            handle = "@" + handle
        info = {"id": items[0]["id"], "handle": handle, "title": sn.get("title", ""),
                "first_year": int(sn.get("publishedAt", "2005")[:4])}
        self.log(f"CHANNEL LOCKED // {info['title']} // SINCE {info['first_year']}", "ok")
        return info

    # --- Videos eines Jahres sammeln
    def scan_archive(self):
        """Einmaliger Scan ALLER Jahre in einem Durchgang über die Upload-Liste des Kanals.
        Kostet ca. 1 API-Einheit pro 50 Videos. Danach kann der Browser-Fallback jedes Jahr ohne API füllen."""
        this_year = datetime.date.today().year
        missing = [y for y in YEARS if y < this_year and load(videos_file(y), None) is None]
        prog = load(archive_file(), {})
        if not missing or prog.get("done"):
            return
        try:
            if "uploads" not in prog:
                q = {"id": CHANNEL["id"]} if CHANNEL.get("id") else {"forHandle": CHANNEL.get("handle", "")}
                items = self.call("GET", "channels", {"part": "contentDetails,statistics", **q}).get("items", [])
                if not items:
                    return
                prog = {"uploads": items[0]["contentDetails"]["relatedPlaylists"]["uploads"],
                        "total": int(items[0]["statistics"]["videoCount"]),
                        "found": {}, "page_token": None, "seen": 0}
                save(archive_file(), prog)
            self.log(f"ARCHIVE SCAN // ALL YEARS IN ONE PASS // {prog['seen']}/{prog['total']} ...")
            pages = 0
            while True:
                self.check()
                params = {"part": "contentDetails", "playlistId": prog["uploads"], "maxResults": 50}
                if prog["page_token"]:
                    params["pageToken"] = prog["page_token"]
                d = self.call("GET", "playlistItems", params)
                for it in d.get("items", []):
                    prog["seen"] += 1
                    pub = it["contentDetails"].get("videoPublishedAt")
                    if pub:
                        prog["found"][it["contentDetails"]["videoId"]] = pub
                prog["page_token"] = d.get("nextPageToken")
                pages += 1
                if pages % 10 == 0 or not prog["page_token"]:
                    save(archive_file(), prog)
                if pages % 40 == 0:
                    self.log(f"ARCHIVE SCAN // {prog['seen']}/{prog['total']} UPLOADS")
                if not prog["page_token"]:
                    break
        except QuotaExceeded:
            save(archive_file(), prog)
            self.log(f"ARCHIVE SCAN PAUSED // QUOTA // {prog.get('seen', 0)}/{prog.get('total', '?')} "
                     f"// CONTINUES AFTER RESET {quota_reset_local()}", "warn")
            return
        by_year = {}
        for vid, pub in prog["found"].items():
            by_year.setdefault(int(pub[:4]), []).append({"id": vid, "published": pub})
        complete = prog["seen"] >= prog["total"] - 50
        oldest = min(by_year) if by_year else this_year
        written = []
        for y, vids in by_year.items():
            if y >= this_year or (not complete and y <= oldest):
                continue          # laufendes Jahr / evtl. abgeschnittenes ältestes Jahr nicht festschreiben
            if load(videos_file(y), None) is None:
                save(videos_file(y), sorted(vids, key=lambda v: v["published"]))
                written.append(y)
                self.emit("progress", y)
        prog["done"] = True
        save(archive_file(), prog)
        self.log(f"ARCHIVE SCAN COMPLETE // {len(written)} YEARS READY FOR THE BROWSER FALLBACK", "ok")
        if not complete:
            self.log(f"UPLOAD LIST CAPPED BY YOUTUBE // {oldest} AND OLDER USE THE SLOWER DATE SEARCH", "warn")

    def collect(self, year):
        cached = load(videos_file(year), None)
        if cached is not None:
            return cached
        self.scanning = year

        cf = collect_file(year)
        prog = load(cf, {})
        if "channel_id" not in prog:
            q = {"id": CHANNEL["id"]} if CHANNEL.get("id") else {"forHandle": CHANNEL.get("handle", "")}
            items = self.call("GET", "channels", {"part": "contentDetails,statistics", **q}).get("items", [])
            if not items:
                raise RuntimeError(f"CHANNEL {CHANNEL.get('handle') or CHANNEL.get('id')} NOT FOUND")
            ch = items[0]
            prog = {
                "channel_id": ch["id"],
                "uploads": ch["contentDetails"]["relatedPlaylists"]["uploads"],
                "total": int(ch["statistics"]["videoCount"]),
                "found": {}, "method": "uploads", "page_token": None,
                "seen": 0, "done_months": [],
            }
            save(cf, prog)
            self.emit("progress", year)

        cut = archive_cutoff()
        if prog.get("method") == "uploads" and cut is not None and year <= cut:
            prog["method"] = "search"
            save(cf, prog)
            self.log(f"{year} IS BEYOND YOUTUBE'S 20,000 UPLOAD LIMIT // DATE SEARCH (~100 UNITS PER 50 VIDEOS)")
        prefix = str(year)
        if prog["method"] == "uploads":
            self.log(f"SCANNING CHANNEL UPLOADS FOR {year} ...")
            reached_older, pages = False, 0
            while True:
                params = {"part": "contentDetails", "playlistId": prog["uploads"], "maxResults": 50}
                if prog["page_token"]:
                    params["pageToken"] = prog["page_token"]
                d = self.call("GET", "playlistItems", params)
                for it in d.get("items", []):
                    prog["seen"] += 1
                    cd = it["contentDetails"]
                    pub = cd.get("videoPublishedAt")
                    if not pub:
                        continue
                    if pub.startswith(prefix):
                        prog["found"][cd["videoId"]] = pub
                    elif pub < prefix:
                        reached_older = True
                prog["page_token"] = d.get("nextPageToken")
                save(cf, prog)
                pages += 1
                if pages % 20 == 0:
                    self.log(f"{prog['seen']}/{prog['total']} UPLOADS SCANNED // "
                             f"{len(prog['found'])} FROM {year}")
                if reached_older or not prog["page_token"]:
                    break
            if reached_older or prog["seen"] >= prog["total"] - 50:
                prog["method"] = "done"
            else:
                self.log("UPLOAD LIST CAPPED // SWITCHING TO DATE SEARCH", "warn")
                prog["method"] = "search"
            save(cf, prog)

        if prog["method"] == "search":
            for m in range(1, 13):
                if m in prog["done_months"]:
                    continue
                start = f"{year}-{m:02d}-01T00:00:00Z"
                end = f"{year + 1}-01-01T00:00:00Z" if m == 12 else f"{year}-{m + 1:02d}-01T00:00:00Z"
                token, count = None, 0
                while True:
                    params = {
                        "part": "snippet", "channelId": prog["channel_id"], "type": "video",
                        "order": "date", "maxResults": 50,
                        "publishedAfter": start, "publishedBefore": end,
                    }
                    if token:
                        params["pageToken"] = token
                    d = self.call("GET", "search", params)
                    for it in d.get("items", []):
                        prog["found"][it["id"]["videoId"]] = it["snippet"]["publishedAt"]
                        count += 1
                    token = d.get("nextPageToken")
                    if not token:
                        break
                if count >= 500:
                    self.log(f"WARNING // {m:02d}/{year} HAS 500+ HITS, MAY BE INCOMPLETE", "warn")
                prog["done_months"].append(m)
                save(cf, prog)
                self.log(f"{m:02d}/{year} // {count} VIDEOS")
            prog["method"] = "done"
            save(cf, prog)

        videos = sorted(
            ({"id": vid, "published": pub} for vid, pub in prog["found"].items()),
            key=lambda v: v["published"],
        )
        save(videos_file(year), videos)
        self.scanning = None
        self.log(f"{len(videos)} VIDEOS FROM {year} FOUND", "ok")
        self.emit("progress", year)
        return videos

    # --- Playlist befüllen
    def prefetch(self, year):
        """Nächstes Jahr schon mit API-Kontingent vorbereiten, damit der Browser dort weitermachen kann."""
        if year >= datetime.date.today().year:
            return
        try:
            if load(videos_file(year), None) is None:
                self.log(f"PRE-SCAN {year} FOR BROWSER FALLBACK")
            self.collect(year)
            self.ensure_playlist(year)
        except SearchLimit:
            self.scanning = None
            self.log(f"PRE-SCAN {year} PAUSED // DAILY SEARCH LIMIT (~100 SEARCHES) REACHED // "
                     f"CONTINUES AFTER RESET {quota_reset_local()}", "warn")
        except QuotaExceeded:
            self.scanning = None
            self.log(f"PRE-SCAN {year} SKIPPED // QUOTA", "warn")
        except (Stopped, BrowserFailed):
            raise
        except Exception as e:  # noqa: BLE001
            self.scanning = None
            self.log(f"PRE-SCAN {year} INTERRUPTED // {e} // PROGRESS SAVED, CONTINUES NEXT RUN", "warn")

    def ensure_playlist(self, year):
        sf = state_file(year)
        state = load(sf, {})
        if not state.get("playlist_id"):
            pid = None
            if self.mode == "api":
                try:
                    pid = self.call("POST", "playlists", {"part": "snippet,status"}, {
                        "snippet": {
                            "title": playlist_title(year),
                            "description": playlist_description(year),
                        },
                        "status": {"privacyStatus": PRIVACY},
                    })["id"]
                except QuotaExceeded:
                    if not self.fallback:
                        raise
                    self.switch_to_browser()
            if pid is None:
                if not self.browser:
                    raise QuotaExceeded()
                pid = self.browser.create_playlist(playlist_title(year), playlist_description(year))
            state = {"playlist_id": pid, "next": 0, "skipped": []}
            save(sf, state)
            self.log(f"PLAYLIST CREATED // {playlist_title(year)}", "ok")
        return state

    def switch_to_browser(self):
        self.log("DAILY API QUOTA REACHED // SWITCHING TO BROWSER FALLBACK", "warn")
        self.mode = "browser"
        self.emit("mode", "browser")
        self.browser = BrowserUplink(self)
        self.browser.start()

    def add_video(self, playlist_id, video_id):
        """True = hinzugefügt, False = übersprungen."""
        if self.mode == "api":
            try:
                self.call("POST", "playlistItems", {"part": "snippet"}, {"snippet": {
                    "playlistId": playlist_id,
                    "resourceId": {"kind": "youtube#video", "videoId": video_id},
                }})
                return True
            except ApiError as e:
                if e.reasons & {"videoNotFound", "forbidden"}:
                    return False
                raise
            except QuotaExceeded:
                if not self.fallback:
                    raise
                self.switch_to_browser()
        return self.browser.add(playlist_id, video_id)

    def add_batch(self, playlist_id, video_ids):
        """Browser-Modus: bis zu BATCH_SIZE Videos in einer Anfrage. Liefert pro Video True/False."""
        if self.browser.add_many(playlist_id, video_ids):
            return [True] * len(video_ids)
        # Abgelehnt: prüfen, was trotzdem drin ist, den Rest einzeln versuchen (verhindert Duplikate)
        self.log(f"BATCH OF {len(video_ids)} REJECTED // CHECKING SINGLY", "warn")
        present = {it["vid"] for it in self.browser.list_items(playlist_id)}
        result = []
        for v in video_ids:
            if v in present:
                result.append(True)
                continue
            result.append(self.browser.add(playlist_id, v))
            self.sleep(random.uniform(3, 6))
        return result

    def pace(self):
        """Browser-Modus: Pause nach jedem Paket, nach COOLDOWN_EVERY Paketen eine längere mit Countdown."""
        if self.mode != "browser" or not self.browser:
            return
        count = self.browser.count
        last_long = getattr(self, "_last_long", 0)
        last_cool = getattr(self, "_last_cool", 0)
        if count - last_long >= LONG_BREAK_EVERY:
            self._last_long = self._last_cool = count
            pause, label = random.uniform(*LONG_BREAK_PAUSE), "BREAK"
        elif count - last_cool >= COOLDOWN_EVERY:
            self._last_cool = count
            pause, label = random.uniform(*COOLDOWN_PAUSE), "COOLDOWN"
        else:
            pause, label = None, None
        if pause:
            resume = (datetime.datetime.now() + datetime.timedelta(seconds=pause)).strftime("%H:%M:%S")
            self.log(f"BROWSER UPLINK // {count} ADDED // {label} {fmt_secs(pause)}, RESUME {resume}")
            self.sleep(pause, label)
        else:
            self.sleep(random.uniform(*BATCH_PAUSE), "NEXT BATCH")

    # --- Playlist-Checker
    def list_playlist(self, pid):
        if self.mode == "api":
            try:
                items, token = [], None
                while True:
                    params = {"part": "contentDetails", "playlistId": pid, "maxResults": 50}
                    if token:
                        params["pageToken"] = token
                    d = self.call("GET", "playlistItems", params)
                    for it in d.get("items", []):
                        items.append({"vid": it["contentDetails"]["videoId"], "item": it["id"]})
                    token = d.get("nextPageToken")
                    if not token:
                        return items
            except QuotaExceeded:
                if not self.fallback:
                    raise
                self.switch_to_browser()
        return self.browser.list_items(pid)

    def remove_item(self, pid, item):
        if self.mode == "api" and item.get("item"):
            try:
                self.call("DELETE", "playlistItems", {"id": item["item"]})
                return
            except QuotaExceeded:
                if not self.fallback:
                    raise
                self.switch_to_browser()
                raise RelistNeeded()
        if not item.get("set"):
            raise RelistNeeded()
        self.browser.remove(pid, item)

    def verify(self, year, videos, state, why):
        """Prüft die Playlist: entfernt doppelte Videos (das erste bleibt) und gleicht den
        Fortschritt mit der Playlist ab, falls ein Video schon drin, aber nicht gespeichert war."""
        pid = state.get("playlist_id")
        if not pid:
            return state
        self.emit("checking", year, True)
        self.log(f"PLAYLIST CHECK {year} // {why} ...")
        try:
            removed = 0
            seen = set()
            for _ in range(3):
                try:
                    items = self.list_playlist(pid)
                    seen, dups = set(), []
                    for it in items:
                        if it["vid"] in seen:
                            dups.append(it)
                        else:
                            seen.add(it["vid"])
                    for it in dups:
                        self.remove_item(pid, it)
                        removed += 1
                        self.log(f"DUPLICATE REMOVED // {it['vid']}", "warn")
                    break
                except RelistNeeded:
                    continue
            n = state.get("next", 0)
            moved = 0
            while n < len(videos) and videos[n]["id"] in seen:
                n += 1
                moved += 1
            if moved:
                state["next"] = n
                save(state_file(year), state)
                self.emit("progress", year)
                self.log(f"RESYNC {year} // {moved} VIDEO(S) WERE ALREADY IN THE PLAYLIST", "warn")
            msg = f"{removed} DUPLICATE(S) REMOVED" if removed else "NO DUPLICATES"
            self.log(f"PLAYLIST CHECK {year} // {len(seen)} VIDEOS // {msg}", "ok")
        except QuotaExceeded:
            self.log(f"PLAYLIST CHECK {year} SKIPPED // QUOTA", "warn")
        finally:
            self.emit("checking", year, False)
        return state

    def final_check(self):
        """Nach STOP: aktuelles Jahr noch einmal prüfen (nicht durch STOP unterbrechbar)."""
        if not self.cur:
            return
        year, videos = self.cur
        state = load(state_file(year), {})
        if not state.get("playlist_id"):
            return
        self.ignore_stop = True
        try:
            self.verify(year, videos, state, "AFTER STOP")
        except Exception as e:  # noqa: BLE001
            self.log(f"PLAYLIST CHECK FAILED // {e}", "err")
        finally:
            self.ignore_stop = False

    def manual_check(self):
        for y in self.years:
            self.check()
            videos = load(videos_file(y), None)
            state = load(state_file(y), {})
            if videos is None or not state.get("playlist_id"):
                self.log(f"{y} // NO PLAYLIST YET, NOTHING TO CHECK")
                continue
            self.emit("current", y)
            self.verify(y, videos, state, "FULL SCAN")
        self.log("PLAYLIST CHECK COMPLETE", "ok")

    def fill(self, year, videos):
        self.scanning = None
        sf = state_file(year)
        state = self.ensure_playlist(year)
        self.cur = (year, videos)
        if 0 < state.get("next", 0) < len(videos):
            state = self.verify(year, videos, state, "SYNC ON START")
        i, added = state["next"], 0
        if i >= len(videos):
            self.log(f"{year} ALREADY COMPLETE")
            return True
        while i < len(videos):
            if self.limit and added >= self.limit:
                self.log(f"{year} // RUN LIMIT OF {self.limit} VIDEOS REACHED")
                return False
            self.check()
            if self.mode == "browser":
                n = min(BATCH_SIZE, len(videos) - i)
                if self.limit:
                    n = min(n, self.limit - added)
                chunk = [v["id"] for v in videos[i:i + n]]
                results = self.add_batch(state["playlist_id"], chunk)
            else:
                chunk = [videos[i]["id"]]
                results = [self.add_video(state["playlist_id"], chunk[0])]   # kann auf Browser umschalten
            for vid, ok in zip(chunk, results):
                if ok:
                    added += 1
                else:
                    state.setdefault("skipped", []).append(vid)
            i += len(chunk)
            state["next"] = i
            save(sf, state)
            self.emit("progress", year)
            since_check = getattr(self, "_since_check", 0) + len(chunk)
            self._since_check = since_check
            if since_check >= 100 and i < len(videos):
                self._since_check = 0
                state = self.verify(year, videos, state, "INTERVAL")
                i = state["next"]
            if i < len(videos):
                self.pace()
        state = self.verify(year, videos, state, "FINAL FULL SCAN")
        self.log(f"{year} COMPLETE // https://www.youtube.com/playlist?list={state['playlist_id']}", "ok")
        return True


# ============================================================ UI
# Neon-Palette
RED, RED_MILD, RED_DARK = "#FF6159", "#AE3B36", "#431618"
CYAN, CYAN_HOT, CYAN_FAINT = "#5EF6FF", "#28FFFF", "#172C2E"
YELLOW, GREEN = "#FFD741", "#1DED83"
BG, BG2 = "#0E0E17", "#121221"
TEXT, TEXT_DIM, TEXT_MUTED = "#FFFFFF", "#B4ECF2", "#94B8C2"
FLUFF = None  # wird nach mix() gesetzt

S = 1.0                 # DPI-Faktor
FONT_UI = "Segoe UI"
FONT_MONO = "Consolas"


def px(n):
    return max(1, int(round(n * S)))


def F(kind, size, bold=True):
    return (FONT_UI if kind == "ui" else FONT_MONO, -px(size), "bold" if bold else "normal")


def mix(c1, c2, a):
    """c1 mit Deckkraft a über c2."""
    a = max(0.0, min(1.0, a))
    v1 = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    v2 = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(round(x * a + y * (1 - a)) for x, y in zip(v1, v2))


FLUFF = mix(RED, BG, 0.55)


def flat(pts):
    return [c for p in pts for c in p]


def shape(x0, y0, x1, y1, c, kind="br"):
    """br: eine Ecke unten rechts abgeschnitten. trbl: oben rechts + unten links."""
    if kind == "br":
        return [(x0, y0), (x1, y0), (x1, y1 - c), (x1 - c, y1), (x0, y1)]
    return [(x0, y0), (x1 - c, y0), (x1, y0 + c), (x1, y1), (x0 + c, y1), (x0, y1 - c)]


def clip(pts, a, b, c):
    """Konvexes Polygon auf die Halbebene a*x + b*y + c >= 0 beschneiden."""
    out = []
    for i, p in enumerate(pts):
        q = pts[(i + 1) % len(pts)]
        fp = a * p[0] + b * p[1] + c
        fq = a * q[0] + b * q[1] + c
        if fp >= 0:
            out.append(p)
        if (fp >= 0) != (fq >= 0):
            t = fp / (fp - fq)
            out.append((p[0] + t * (q[0] - p[0]), p[1] + t * (q[1] - p[1])))
    return out


def gradient(cv, pts, x0, y0, x1, y1, role, tint, steps=10):
    """Diagonaler Verlauf: Rollenfarbe oben links -> dunkel unten rechts."""
    w, h = max(1, x1 - x0), max(1, y1 - y0)
    for i in range(steps):
        s0, s1 = 2 * i / steps, 2 * (i + 1) / steps
        poly = clip(pts, 1 / w, 1 / h, -x0 / w - y0 / h - s0)
        poly = clip(poly, -1 / w, -1 / h, x0 / w + y0 / h + s1)
        if len(poly) >= 3:
            col = mix(role, BG, tint * (1 - (s0 + s1) / 4))
            cv.create_polygon(flat(poly), fill=col, outline=col)


def blink_alpha():
    """Deckkraft 45–100 %, 4-s-Zyklus."""
    phase = (time.time() % 4) / 4
    return 0.45 + 0.55 * (0.5 - 0.5 * math.cos(2 * math.pi * phase))


# ------------------------------------------------------------------- Logo
# Eigenes Emblem: Rahmen mit abgeschnittener Ecke (rot), Play-Dreieck (cyan),
# Zeitleiste mit Ticks (Chronik) und Rec-Punkt. Koordinaten 0..100.
LOGO_FRAME = [(6, 6), (94, 6), (94, 72), (72, 94), (6, 94)]
LOGO_PLAY = [(36, 26), (36, 66), (70, 46)]
LOGO_TICKS = [18, 30, 42, 54]


def draw_logo(cv, x, y, size, tags=()):
    k = size / 100

    def P(pts):
        return [c for px_, py_ in pts for c in (x + px_ * k, y + py_ * k)]
    w = max(1, round(size / 22))
    cv.create_polygon(P(LOGO_FRAME), fill=mix(RED, BG, 0.08), outline="", tags=tags)
    for d, a in ((2, 0.30), (4, 0.12)):
        cv.create_polygon(P([(px_ + (-d if px_ < 50 else d), py_ + (-d if py_ < 50 else d))
                             for px_, py_ in LOGO_FRAME]), fill="", outline=mix(RED, BG, a), tags=tags)
    cv.create_polygon(P(LOGO_FRAME), fill="", outline=RED, width=w, tags=tags)
    cv.create_polygon(P(LOGO_PLAY), fill=CYAN, outline=CYAN, tags=tags)
    cv.create_line(*P([(16, 80), (62, 80)]), fill=RED, width=w, tags=tags)
    for tx_ in LOGO_TICKS:
        cv.create_line(*P([(tx_, 80), (tx_, 74)]), fill=RED, width=max(1, w - 1), tags=tags)
    cv.create_rectangle(*P([(14, 14), (22, 22)]), fill=YELLOW, outline="", tags=tags)


def logo_photo(root, size):
    """Logo als PhotoImage fürs Fenster-Icon (ohne Zusatzbibliothek)."""
    ss = 4
    n = size * ss
    k = n / 100

    def inside(poly, xx, yy):
        hit = False
        for i in range(len(poly)):
            (ax, ay), (bx, by) = poly[i], poly[i - 1]
            if (ay > yy) != (by > yy) and xx < (bx - ax) * (yy - ay) / (by - ay) + ax:
                hit = not hit
        return hit

    def edge_dist(poly, xx, yy):
        best = 1e9
        for i in range(len(poly)):
            (ax, ay), (bx, by) = poly[i - 1], poly[i]
            dx, dy = bx - ax, by - ay
            t = max(0, min(1, ((xx - ax) * dx + (yy - ay) * dy) / (dx * dx + dy * dy)))
            best = min(best, math.hypot(xx - ax - t * dx, yy - ay - t * dy))
        return best

    def col(xx, yy):
        if inside(LOGO_PLAY, xx, yy):
            return CYAN
        if 14 <= xx <= 22 and 14 <= yy <= 22:
            return YELLOW
        if (16 <= xx <= 62 and 78 <= yy <= 82) or any(abs(xx - t) <= 1.5 and 74 <= yy <= 80 for t in LOGO_TICKS):
            return RED
        if inside(LOGO_FRAME, xx, yy):
            return RED if edge_dist(LOGO_FRAME, xx, yy) <= 5 else mix(RED, BG, 0.08)
        return None

    img = tk.PhotoImage(master=root, width=size, height=size)
    rows = []
    for py_ in range(size):
        row = []
        for px_ in range(size):
            acc, cnt = [0, 0, 0], 0
            for sy in range(ss):
                for sx in range(ss):
                    c = col((px_ * ss + sx + 0.5) / k, (py_ * ss + sy + 0.5) / k)
                    if c:
                        cnt += 1
                        for i, j in enumerate((1, 3, 5)):
                            acc[i] += int(c[j:j + 2], 16)
            if cnt == 0:
                row.append(None)
            else:
                row.append("#%02x%02x%02x" % tuple(v // cnt for v in acc))
        rows.append(row)
    for py_, row in enumerate(rows):
        for px_, c in enumerate(row):
            if c:
                img.put(c, (px_, py_))
    return img


class Panel(tk.Canvas):
    """Kantiges Panel mit einer abgeschnittenen Ecke unten rechts, Verlauf, Scanlines, Glow."""

    STATES = {  # Rahmen, Verlauf, Glow
        "active": (1.00, 0.42, True),
        "running": (1.00, 0.30, True),
        "inactive": (0.50, 0.12, False),
        "frame": (0.85, 0.10, False),
        "unavailable": (0.45, 0.04, False),
    }

    def __init__(self, master, height, role=CYAN, state="inactive", cut=16, command=None):
        super().__init__(master, height=px(height), bg=BG, highlightthickness=0, bd=0)
        self.role, self.st, self.cut = role, state, px(cut)
        self.hover = False
        self.command = command
        self.blink = None          # (item, farbe)
        self.windows = []          # Callbacks, die eingebettete Widgets platzieren
        self.bind("<Configure>", lambda e: self.redraw())
        if command:
            self.configure(cursor="hand2")
            self.bind("<Enter>", lambda e: self._hover(True))
            self.bind("<Leave>", lambda e: self._hover(False))
            self.bind("<ButtonRelease-1>", self._click)

    def _hover(self, on):
        self.hover = on
        self.redraw()

    def _click(self, e):
        if 0 <= e.x <= self.winfo_width() and 0 <= e.y <= self.winfo_height() and self.command:
            self.command()

    def set(self, role=None, state=None):
        changed = (role or self.role) != self.role or (state or self.st) != self.st
        self.role = role or self.role
        self.st = state or self.st
        if changed:
            self.redraw()

    def palette(self):
        role = GREEN if self.st == "running" else RED if self.st in ("frame", "unavailable") else self.role
        edge, tint, glow = self.STATES[self.st]
        if self.hover and self.st != "unavailable":
            edge, tint, glow = min(1.0, edge + 0.3), tint + 0.05, True
        return role, edge, tint, glow

    def redraw(self):
        self.delete("all")
        self.blink = None
        w, h = self.winfo_width(), self.winfo_height()
        if w < 12 or h < 12:
            return
        m = px(3)
        x0, y0, x1, y1, c = m, m, w - m - 1, h - m - 1, self.cut
        role, edge, tint, glow = self.palette()
        pts = shape(x0, y0, x1, y1, c)
        gradient(self, pts, x0, y0, x1, y1, role, tint)
        # Scanlines
        for y in range(y0 + 2, y1 - 1, px(3)):
            xr = x1 if y <= y1 - c else x1 + y1 - c - y
            t = (((x0 + xr) / 2 - x0) / (x1 - x0) + (y - y0) / (y1 - y0)) / 2
            base = mix(role, BG, tint * (1 - t))
            self.create_line(x0 + 1, y, xr - 1, y, fill=mix("#ffffff", base, 0.035))
        if glow:
            for d, a in ((1, 0.65), (2, 0.35), (3, 0.16)):
                o = shape(x0 - d, y0 - d, x1 + d, y1 + d, c + 0.586 * d)
                self.create_polygon(flat(o), fill="", outline=mix(role, BG, a), tags=("glow", f"glow{d}"))
        self.create_polygon(flat(pts), fill="", outline=mix(role, BG, edge), tags="edge")
        self.draw_content(x0, y0, x1, y1, c, role)
        for place in self.windows:
            place(x0, y0, x1, y1, c)

    def draw_content(self, x0, y0, x1, y1, c, role):
        pass

    def blink_tick(self, alpha):
        if self.blink:
            item, col = self.blink
            self.itemconfigure(item, fill=mix(col, BG, alpha))

    # --- Bausteine
    def icon_box(self, x, cy, size, role, on):
        self.create_rectangle(x, cy - size / 2, x + size, cy + size / 2,
                              fill=mix(role, BG, 0.22 if on else 0.10),
                              outline=mix(role, BG, 0.8 if on else 0.45))

    def right_tag(self, x0, y0, x1, y1, c, width, role, on):
        tx = x1 - width
        pts = [(tx, y0 + 1), (x1 - 1, y0 + 1), (x1 - 1, y1 - c), (x1 - c, y1 - 1), (tx, y1 - 1)]
        gradient(self, pts, tx, y0, x1, y1, role, 0.24 if on else 0.12, steps=6)
        self.create_line(tx, y0 + 1, tx, y1, fill=mix(role, BG, 0.6 if on else 0.35))
        return tx


def fmt_secs(sec):
    sec = max(0, int(sec))
    h, rem = divmod(sec, 3600)
    m, s_ = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s_:02d}" if h else f"{m:02d}:{s_:02d}"


def pause_label(p, now):
    """Text für Pause-Zustände: manuell (zählt hoch) oder Wartezeit (zählt runter)."""
    if not p:
        return ""
    if p["kind"] == "manual":
        return f"PAUSED // {fmt_secs(now - p['t'])}"
    return f"{p['label']} // {fmt_secs(p['t'] - now)}"


class YearRow(Panel):
    def __init__(self, master, year, command):
        super().__init__(master, 62, cut=10, command=command)
        self.year = year
        self.selected = False
        self.running = False
        self.checking = False
        self.pause = None          # None | {"kind": "manual", "t": start} | {"kind": "wait", "t": ende, "label": ..}
        self.info = year_info(year)
        self.geo = None
        self.last_label = None

    def status_text(self):
        code, run = self.info["code"], self.running
        if self.checking:
            return "CHECKING PLAYLIST...", True
        if run and self.pause:
            return pause_label(self.pause, time.time()), False
        if code in ("none", "scanning"):
            return ("SCANNING..." if run else "SCAN PAUSED" if code == "scanning" else "NOT SCANNED"), run
        if code == "done":
            return "COMPLETE", False
        if run:
            return "UPLINK ACTIVE", False
        if code == "progress":
            return "IN PROGRESS", False
        return f"READY // {self.info['total']} VIDEOS", False

    def draw_content(self, x0, y0, x1, y1, c, role):
        on = self.selected or self.running
        h = y1 - y0
        cy = (y0 + y1) / 2
        isz = px(32)
        ix = x0 + px(10)
        self.icon_box(ix, cy, isz, role, on)
        u = isz / 32
        icol = role if on else mix(role, BG, 0.8)
        top = cy - isz / 2
        if self.running and self.pause:
            # Pause-Symbol statt Play
            for xx in (11, 18):
                self.create_rectangle(ix + xx * u, top + 9 * u, ix + (xx + 4) * u, top + 23 * u,
                                      fill=icol, outline="")
        else:
            for yy, xe in ((9, 20), (14, 20), (19, 15)):
                self.create_line(ix + 7 * u, top + yy * u, ix + xe * u, top + yy * u, fill=icol,
                                 width=max(1, px(1.5)))
            self.create_polygon(ix + 18 * u, top + 17 * u, ix + 18 * u, top + 25 * u, ix + 25 * u, top + 21 * u,
                                fill=icol, outline=icol)

        tx = self.right_tag(x0, y0, x1, y1, c, px(116), role, on)
        nx = ix + isz + px(12)
        self.create_text(nx, y0 + h * 0.32, text=str(self.year), anchor="w", font=F("ui", 16),
                         fill=TEXT if on else TEXT_DIM)
        text, blinking = self.status_text()
        scol = role if on else TEXT_MUTED
        item = self.create_text(nx, y0 + h * 0.62, text=text, anchor="w", font=F("mono", 11), fill=scol,
                                tags="stat")
        self.last_label = text
        if blinking:
            self.blink = (item, scol)

        total, done = self.info["total"], self.info["done"]
        mid = (y0 + y1 - c / 2) / 2
        self.geo = dict(x0=x0, y0=y0, x1=x1, y1=y1, c=c, nx=nx, tx=tx, role=role, fx=None)
        cx = (tx + x1) / 2
        if total:
            bx0, bx1, by = nx, tx - px(14), y1 - px(8)
            self.create_rectangle(bx0, by, bx1, by + px(3), fill=mix(role, BG, 0.25), outline="")
            fx = bx0 + (bx1 - bx0) * done / total
            self.create_rectangle(bx0, by, max(bx0, fx), by + px(3), fill=role if on else mix(role, BG, 0.75),
                                  outline="", tags="bar", state="normal" if done else "hidden")
            self.geo.update(bx0=bx0, bx1=bx1, by=by, fx=fx)
            pct = done * 100 / total
            ptxt = "100 %" if done >= total else f"{pct:.1f} %"
            self.create_text(cx, mid - px(9), text=ptxt, font=F("mono", 17),
                             fill=role if on else TEXT_DIM, tags="pct")
            self.create_text(cx, mid + px(11), text=f"{done} / {total}", font=F("mono", 12),
                             fill=TEXT if on else TEXT_MUTED, tags="cnt")
        else:
            self.create_text(cx, mid, text="--", font=F("mono", 15), fill=mix(TEXT_MUTED, BG, 0.6))

        manual_pause = self.pause and self.pause.get("kind") == "manual"
        if self.running and self.geo.get("fx") and not manual_pause:
            # Datenpaket auf dem Ladebalken
            self.create_rectangle(0, 0, 0, 0, fill=mix("#ffffff", role, 0.6), outline="", tags="pkt")
        if self.running:
            self.tick(time.time())

    # --- Animation der aktiven Karte: nur Ladebalken + ruhiger Glow
    def tick(self, now):
        g = self.geo
        if not (self.running and g):
            return
        role = g["role"]
        # Glow: 4 s, weiches Ein- und Ausblenden
        pulse = 0.5 - 0.5 * math.cos(now * 2 * math.pi / 4.0)
        for d, a in ((1, 0.65), (2, 0.35), (3, 0.16)):
            self.itemconfigure(f"glow{d}", outline=mix(role, BG, a * (0.35 + 0.65 * pulse)))
        # Datenpaket: langsam (6 s pro Durchlauf), weich ein- und ausgeblendet
        if g.get("fx") and self.find_withtag("pkt"):
            bx0, fx, by = g["bx0"], g["fx"], g["by"]
            L = px(36)
            ph = (now % 6.0) / 6.0
            pos = bx0 - L + (fx - bx0 + L) * ph
            a, b = max(bx0, pos), min(fx, pos + L)
            if b - a > 1:
                fade = math.sin(math.pi * ph)
                self.coords("pkt", a, by, b, by + px(3))
                self.itemconfigure("pkt", state="normal", fill=mix("#ffffff", role, 0.6 * fade))
            else:
                self.itemconfigure("pkt", state="hidden")
        # Pause-Timer
        if self.pause:
            label = pause_label(self.pause, now)
            if label != self.last_label:
                self.itemconfigure("stat", text=label)
                self.last_label = label

    def set_checking(self, on):
        self.checking = on
        self.redraw()

    def set_pause(self, p):
        self.pause = p
        self.refresh()

    def refresh(self, selected=None, running=None, info=None):
        if selected is not None:
            self.selected = selected
        if running is not None:
            self.running = running
            if not running:
                self.pause = None
        if info is not None:
            old = self.info
            self.info = info
            g = self.geo
            # Nur Zahlen/Balken geändert -> Elemente aktualisieren statt alles neu zu zeichnen
            if (selected is None and running is None and g and g.get("bx1") and info["total"]
                    and old["total"] == info["total"] and old["code"] == info["code"]):
                done, total = info["done"], info["total"]
                fx = g["bx0"] + (g["bx1"] - g["bx0"]) * done / total
                g["fx"] = fx
                self.coords("bar", g["bx0"], g["by"], fx, g["by"] + px(3))
                self.itemconfigure("bar", state="normal" if done else "hidden")
                self.itemconfigure("pct", text="100 %" if done >= total else f"{done * 100 / total:.1f} %")
                self.itemconfigure("cnt", text=f"{done} / {total}")
                return
        manual = self.running and self.pause and self.pause["kind"] == "manual"
        self.role = YELLOW if manual else CYAN
        self.st = "active" if manual else "running" if self.running else "active" if self.selected else "inactive"
        self.redraw()


class ToggleCard(Panel):
    def __init__(self, master, name, sub, value, command, available=True, height=54, compact=False):
        super().__init__(master, height, command=command if available else None,
                         state="inactive" if available else "unavailable")
        self.name, self.sub, self.value, self.available = name, sub, value, available
        self.compact = compact
        self.update_value(value)

    def update_value(self, value):
        self.value = value
        if self.available:
            self.st = "active" if value else "inactive"
        self.redraw()

    def draw_content(self, x0, y0, x1, y1, c, role):
        on = self.value and self.available
        h = y1 - y0
        cy = (y0 + y1) / 2
        isz = px(24 if self.compact else 32)
        ix = x0 + px(8 if self.compact else 10)
        self.icon_box(ix, cy, isz, role, on)
        k = px(8 if self.compact else 10)
        if on:
            self.create_rectangle(ix + isz / 2 - k / 2, cy - k / 2, ix + isz / 2 + k / 2, cy + k / 2,
                                  fill=role, outline=role)
        else:
            self.create_rectangle(ix + isz / 2 - k / 2, cy - k / 2, ix + isz / 2 + k / 2, cy + k / 2,
                                  fill="", outline=mix(role, BG, 0.5))
        tx = self.right_tag(x0, y0, x1, y1, c, px(46 if self.compact else 54), role, on)
        nx = ix + isz + px(10 if self.compact else 12)
        maxw = tx - nx - px(6)
        fn, fs = F("ui", 14 if self.compact else 15), F("mono", 10 if self.compact else 11)
        self.create_text(nx, y0 + h * 0.34, text=fit_text(self.name, fn, maxw), anchor="w",
                         font=fn, fill=TEXT if on else TEXT_DIM)
        sub = self.sub if self.available else "UNAVAILABLE"
        self.create_text(nx, y0 + h * 0.66, text=fit_text(sub, fs, maxw), anchor="w",
                         font=fs, fill=role if on else TEXT_MUTED)
        mid = (y0 + y1 - c / 2) / 2
        tag = ("ON" if self.value else "OFF") if self.available else "--"
        self.create_text((tx + x1) / 2, mid, text=tag, font=F("mono", 13),
                         fill=role if on else TEXT_MUTED)


class LimitPanel(Panel):
    def __init__(self, master, value):
        super().__init__(master, 58)
        self.value = value

    def draw_content(self, x0, y0, x1, y1, c, role):
        h = y1 - y0
        cy = (y0 + y1) / 2
        isz = px(32)
        ix = x0 + px(10)
        self.icon_box(ix, cy, isz, role, False)
        col = mix(role, BG, 0.8)
        u = isz / 32
        top = cy - isz / 2
        for k, hh in enumerate((8, 14, 11, 18)):
            xx = ix + (8 + k * 5) * u
            self.create_rectangle(xx, top + (24 - hh) * u, xx + 3 * u, top + 24 * u, fill=col, outline="")
        nx = ix + isz + px(12)
        self.create_text(nx, y0 + h * 0.34, text="BATCH LIMIT", anchor="w", font=F("ui", 15), fill=TEXT_DIM)
        self.create_text(nx, y0 + h * 0.66, text="VIDEOS / YEAR / RUN", anchor="w", font=F("mono", 11),
                         fill=TEXT_MUTED)
        val = "UNLIMITED" if self.value == 0 else str(self.value)
        self.create_text(x1 - px(18), y0 + h * 0.45, text=val, anchor="e", font=F("mono", 14), fill=CYAN)


class StatusChip(Panel):
    def __init__(self, master):
        super().__init__(master, 36, cut=8)
        self.text = "STANDBY"
        self.blinking = False

    def show(self, text, role, state, blinking=False):
        same = (role, state, blinking) == (self.role, self.st, self.blinking) and self.find_withtag("ctext")
        self.text, self.role, self.st, self.blinking = text, role, state, blinking
        if same:
            self.itemconfigure("ctext", text=text)   # nur Text tauschen, kein Neuzeichnen
        else:
            self.redraw()

    def draw_content(self, x0, y0, x1, y1, c, role):
        on = self.st in ("active", "running", "frame")
        col = role if on else TEXT_MUTED
        fl = self.create_text(x0 + px(12), (y0 + y1) / 2, text="SYS //", anchor="w",
                              font=F("mono", 10, False), fill=FLUFF)
        left = self.bbox(fl)[2] + px(8)
        item = self.create_text((left + x1 - c) / 2, (y0 + y1) / 2, text=self.text, font=F("mono", 12),
                                fill=col, tags="ctext")
        if self.blinking:
            self.blink = (item, col)


def fit_text(text, font, maxw):
    """Text auf maximale Breite kürzen (mit …)."""
    f = tkfont.Font(font=font)
    if f.measure(text) <= maxw:
        return text
    while text and f.measure(text + "…") > maxw:
        text = text[:-1]
    return text.rstrip(" /") + "…"


class ChannelCard(Panel):
    """Zielkanal als eigenes 'Target Lock'-Panel: roter Rahmen, Eckklammern, großer Name.
    Klickbar ist nur der cyan EDIT-Bereich rechts."""

    def __init__(self, master, command):
        super().__init__(master, 84, state="frame", cut=16)
        self.on_edit = command
        self.resolving = False
        self.edit_hover = False

    def draw_content(self, x0, y0, x1, y1, c, role):
        has = bool(CHANNEL)
        h = y1 - y0
        cy = (y0 + y1) / 2
        # Eckklammern (Zielerfassung)
        k, t = px(12), px(2)
        for (ax, ay, sx, sy) in ((x0 + px(6), y0 + px(6), 1, 1), (x0 + px(6), y1 - px(6), 1, -1)):
            self.create_line(ax, ay, ax + sx * k, ay, fill=RED, width=t)
            self.create_line(ax, ay, ax, ay + sy * k, fill=RED, width=t)
        # @-Icon
        isz = px(42)
        ix = x0 + px(20)
        icx = ix + isz / 2
        self.create_rectangle(ix, cy - isz / 2, ix + isz, cy + isz / 2, fill=mix(RED, BG, 0.12),
                              outline=mix(RED, BG, 0.7))
        self.create_text(icx, cy, text="@", font=F("mono", 22), fill=RED)
        # EDIT-Bereich (cyan = klickbar)
        tw = px(96)
        tx = x1 - tw
        pts = [(tx, y0 + 1), (x1 - 1, y0 + 1), (x1 - 1, y1 - c), (x1 - c, y1 - 1), (tx, y1 - 1)]
        gradient(self, pts, tx, y0, x1, y1, CYAN, 0.30 if self.edit_hover else 0.16, steps=6)
        self.create_polygon(flat(pts), fill="", outline=mix(CYAN, BG, 1.0 if self.edit_hover else 0.75),
                            tags="edit")
        mid = (y0 + y1 - c / 2) / 2
        self.create_text((tx + x1) / 2, mid, text="EDIT ›", font=F("mono", 14),
                         fill=CYAN_HOT if self.edit_hover else CYAN, tags="edit")
        self.create_rectangle(tx, y0 + 1, x1 - 1, y1 - 1, fill="", outline="", tags=("edit", "edithit"))
        self.tag_bind("edit", "<Enter>", lambda e: self._edit_hover(True))
        self.tag_bind("edit", "<Leave>", lambda e: self._edit_hover(False))
        self.tag_bind("edit", "<ButtonRelease-1>", lambda e: self.on_edit())
        # Texte
        nx = ix + isz + px(16)
        self.create_text(nx, y0 + h * 0.22, text="TARGET LOCKED" if has else "NO TARGET", anchor="w",
                         font=F("mono", 10), fill=RED)
        if tx - nx > px(260):
            self.create_text(tx - px(12), y0 + h * 0.22, text="CH.ID " + (CHANNEL.get("id", "")[-6:] or "------"),
                             anchor="e", font=F("mono", 10, False), fill=FLUFF)
        maxw = tx - nx - px(12)
        name = channel_name().upper() if has else "SET A YOUTUBE CHANNEL"
        self.create_text(nx, y0 + h * 0.50, text=fit_text(name, F("ui", 21), maxw), anchor="w",
                         font=F("ui", 21), fill=TEXT if has else TEXT_DIM)
        if self.resolving:
            sub = "RESOLVING..."
        elif has:
            handle = (CHANNEL.get("handle") or CHANNEL.get("id", "")).upper()
            options = [handle]
            if YEARS:
                options = [f"{handle} // ARCHIVE {YEARS[0]}-{YEARS[-1]} // {len(YEARS)} YEARS",
                           f"{handle} // {YEARS[0]}-{YEARS[-1]}", handle]
            f11 = tkfont.Font(font=F("mono", 11))
            sub = next((o for o in options if f11.measure(o) <= maxw), options[-1])
        else:
            sub = "@HANDLE, URL OR CHANNEL ID"
        sub = fit_text(sub, F("mono", 11), maxw)
        item = self.create_text(nx, y0 + h * 0.78, text=sub, anchor="w", font=F("mono", 11),
                                fill=mix(RED, TEXT, 0.35) if has else TEXT_MUTED)
        if self.resolving:
            self.blink = (item, TEXT_MUTED)

    def _edit_hover(self, on):
        if on != self.edit_hover:
            self.edit_hover = on
            self.configure(cursor="hand2" if on else "")
            self.redraw()

    def update_state(self, resolving=None):
        if resolving is not None:
            self.resolving = resolving
        self.st = "frame"
        self.redraw()


class NCDialog(tk.Toplevel):
    """Eingabe-Dialog im Neon-Stil. fields: [(key, label, initial, secret)]"""

    def __init__(self, master, title, fields, hint, on_save):
        super().__init__(master, bg=BG)
        self.title(title)
        self.resizable(False, False)
        self.transient(master)
        self.on_save = on_save
        self.vars = {}
        pad = px(18)
        Section(self, title.upper(), "INPUT // SECURE").pack(fill="x", padx=pad, pady=(pad, px(10)))
        for key, label, initial, secret in fields:
            tk.Label(self, text=label, bg=BG, fg=TEXT_MUTED, font=F("mono", 11), anchor="w").pack(
                fill="x", padx=pad)
            v = tk.StringVar(value=initial)
            e = tk.Entry(self, textvariable=v, show="•" if secret else "", bg=BG2, fg=CYAN,
                         insertbackground=CYAN, relief="flat", font=F("mono", 13, False), width=52,
                         highlightthickness=1, highlightcolor=CYAN,
                         highlightbackground=mix(CYAN, BG, 0.4), selectbackground=mix(CYAN, BG, 0.3))
            e.pack(fill="x", padx=pad, pady=(px(4), px(12)), ipady=px(6))
            self.vars[key] = v
        if hint:
            tk.Label(self, text=hint, bg=BG, fg=FLUFF, font=F("mono", 10, False), justify="left",
                     anchor="w", wraplength=px(520)).pack(fill="x", padx=pad, pady=(0, px(10)))
        row = tk.Frame(self, bg=BG)
        row.pack(fill="x", padx=pad, pady=(0, pad))
        CutButton(row, "SAVE", self.save, width=110).pack(side="left")
        CutButton(row, "CANCEL", self.destroy, width=110).pack(side="left", padx=(px(8), 0))
        self.bind("<Return>", lambda e: self.save())
        self.bind("<Escape>", lambda e: self.destroy())
        style_titlebar(self)
        self.update_idletasks()
        x = master.winfo_rootx() + (master.winfo_width() - self.winfo_width()) // 2
        y = master.winfo_rooty() + (master.winfo_height() - self.winfo_height()) // 3
        self.geometry(f"+{max(0, x)}+{max(0, y)}")
        self.grab_set()
        self.focus_force()

    def save(self):
        if self.on_save({k: v.get().strip() for k, v in self.vars.items()}) is not False:
            self.destroy()


HELP_TEXT = [
    ("h1", "Chronolist Hilfe"),
    ("p", "Chronolist erstellt für einen YouTube-Kanal pro Jahr eine Playlist mit allen Videos aus diesem Jahr. "
          "Die Videos stehen in der Reihenfolge, in der sie hochgeladen wurden. Das älteste kommt zuerst."),

    ("h2", "Schnellstart"),
    ("li", "Einmalig oben auf den API-Knopf klicken und die Zugangsdaten eintragen."),
    ("li", "Links bei Target Channel auf EDIT klicken und den Kanal eingeben."),
    ("li", "In der Jahresliste ein oder mehrere Jahre anklicken."),
    ("li", "START drücken. Den Rest erledigt das Programm."),

    ("h2", "Wie Chronolist arbeitet"),
    ("p", "Chronolist spricht auf zwei Wegen mit YouTube. Normalerweise über die offizielle Schnittstelle von Google "
          "(YouTube Data API). Wenn du FALLBACK einschaltest, zusätzlich über die YouTube-Webseite in einem eigenen "
          "Edge-Fenster. Ein Durchlauf sieht so aus:"),
    ("num", "1.  Anmelden. Beim ersten Start meldest du dich einmal bei Google an. Dafür öffnest du den Link und gibst "
            "den angezeigten Code ein. Danach merkt sich Chronolist die Anmeldung."),
    ("num", "2.  Videos finden. Zuerst muss Chronolist wissen, welche Videos zu welchem Jahr gehören. "
            "Das geht nur über die Schnittstelle, und zwar auf zwei Arten:\n"
            "• Archiv-Scan: Chronolist geht einmal die Upload-Liste des Kanals durch und speichert alle Jahre auf "
            "einmal. Dafür braucht es nur sehr wenige Einheiten.\n"
            "• Datumssuche: In dieser Liste stehen aber nur die neuesten 20.000 Uploads. Ältere Jahre sucht "
            "Chronolist deshalb Monat für Monat. Das verbraucht viele Einheiten und ist auf etwa 100 Suchen am Tag "
            "begrenzt. Bei großen "
            "Kanälen schafft man so ungefähr ein altes Jahr pro Tag.\n"
            "Was einmal gefunden wurde, wird gespeichert und nicht noch einmal gesucht."),
    ("num", "3.  Playlist anlegen. Für jedes Jahr legt Chronolist eine öffentliche Playlist an. "
            "Sie heißt zum Beispiel \"MeinKanal 2012 in Upload-Reihenfolge\". Den Namen kannst du in settings.json über playlist_template ändern (Platzhalter {channel} und {year})."),
    ("num", "4.  Füllen. Die Videos werden der Reihe nach eingefügt. Jedes Video verbraucht 50 von 10.000 Einheiten, "
            "die man pro Tag hat. Das reicht für ungefähr 200 Videos am Tag."),
    ("num", "5.  Wenn das Tageskontingent leer ist:\n"
            "• FALLBACK aus: Chronolist hält an. Im Log steht, wann das Kontingent zurückgesetzt wird. Das ist um "
            "Mitternacht kalifornischer Zeit, bei uns also meistens um 9 Uhr, ein paar Wochen im Jahr schon um 8 Uhr.\n"
            "• FALLBACK an: Chronolist öffnet ein eigenes Edge-Fenster und fügt die restlichen Videos über die "
            "YouTube-Webseite ein, genau so, wie die Webseite es beim Speichern selbst macht. Dabei werden keine "
            "Einheiten verbraucht. Es läuft in Paketen zu 15 Videos mit Pausen dazwischen. Neue Playlists kann das Edge-Fenster "
            "auch anlegen. Videos finden (Schritt 2) kann es aber nicht.\n"
            "Chronolist merkt sich, wenn das Kontingent oder das Suchlimit an einem Tag aufgebraucht ist. Bis zum "
            "Reset fragt es nach einem Neustart nicht noch einmal nach, sondern nimmt gleich den Fallback. In dieser "
            "Zeit ist der API-Knopf gelb und zeigt LIMIT."),
    ("num", "6.  Prüfen. Beim Start, alle 100 Videos, nach STOP und am Ende eines Jahres schaut Chronolist die "
            "Playlist auf doppelte Videos durch. Doppelte werden entfernt und der Fortschritt wird abgeglichen."),
    ("num", "7.  Nächstes Jahr. Ist ein Jahr fertig, geht es mit dem nächsten weiter. Wurden dessen Videos noch nicht "
            "gefunden und ist das Kontingent leer, hält Chronolist an. Mit SKIP AHEAD springt es stattdessen zum "
            "nächsten Jahr, das schon bereit ist, und holt das fehlende später nach."),

    ("h2", "Was sind Einheiten?"),
    ("p", "Die Nutzung der YouTube-Schnittstelle ist kostenlos. Es fällt kein Geld an. Google gibt aber jedem Projekt "
          "ein festes Tageskontingent, gemessen in sogenannten Einheiten. Standardmäßig sind das 10.000 Einheiten "
          "am Tag, dazu ein eigenes Limit von etwa 100 Suchen am Tag. Ist das Kontingent aufgebraucht, geht es nach "
          "dem Reset am nächsten Tag weiter. Über das Edge-Fenster werden gar keine Einheiten verbraucht."),
    ("p", "So viele Einheiten braucht jede Aktion:"),
    ("li", "Upload-Liste durchgehen (Archiv-Scan): 1 Einheit pro 50 Videos"),
    ("li", "Datumssuche für alte Jahre: 100 Einheiten pro 50 Videos, zählt außerdem zum Suchlimit"),
    ("li", "Playlist anlegen: 50 Einheiten"),
    ("li", "Ein Video einfügen: 50 Einheiten"),
    ("li", "Playlist prüfen: 1 Einheit pro 50 Videos"),
    ("li", "Ein doppeltes Video entfernen: 50 Einheiten"),

    ("h2", "Oben im Fenster"),
    ("dt", "HELP"),
    ("dd", "Öffnet diese Hilfe."),
    ("dt", "API-Knopf"),
    ("dd", "Zeigt, ob der Zugang zu YouTube funktioniert. Mit einem Klick kannst du die Zugangsdaten eintragen oder "
           "ändern (Client ID und Client Secret aus der Google Cloud Console).\n"
           "Rot MISSING: Es sind noch keine Zugangsdaten eingetragen.\n"
           "Rot ERROR: Google hat die Anmeldung abgelehnt.\n"
           "Blau SIGN IN: Zugangsdaten sind da, angemeldet bist du aber noch nicht.\n"
           "Gelb LIMIT: Das Tageskontingent ist aufgebraucht.\n"
           "Grün ONLINE: Alles funktioniert."),
    ("dt", "Statusanzeige"),
    ("dd", "Zeigt, was das Programm gerade macht, zum Beispiel STANDBY (wartet), RUNNING (läuft), BROWSER UPLINK "
           "(läuft über das Edge-Fenster), PAUSED (angehalten) oder eine Wartezeit mit Countdown."),

    ("h2", "Target Channel"),
    ("p", "Hier steht der Kanal, dessen Videos gesammelt werden. Über EDIT gibst du einen anderen Kanal ein. "
          "Das geht als @Name, als Link zum Kanal oder als Kanal-ID (die fängt mit UC an). Chronolist holt sich dann "
          "den Namen und das Startjahr. Jeder Kanal hat seinen eigenen Fortschritt."),

    ("h2", "Target Years"),
    ("p", "Jede Karte steht für ein Jahr. Mit einem Klick wählst du ein Jahr aus oder wieder ab. Ausgewählte Jahre "
          "sind hell umrandet und werden beim Start nacheinander bearbeitet. Rechts siehst du den Fortschritt in "
          "Prozent und wie viele Videos schon drin sind."),
    ("dt", "Was unter dem Jahr stehen kann"),
    ("dd", "NOT SCANNED: Die Videos wurden noch nicht gesucht.\n"
           "SCAN PAUSED: Die Suche wurde angefangen, ist aber noch nicht fertig.\n"
           "READY: Alle Videos sind gefunden, die Playlist kann gefüllt werden.\n"
           "IN PROGRESS: Die Playlist ist schon teilweise gefüllt.\n"
           "COMPLETE: Das Jahr ist fertig.\n"
           "UPLINK ACTIVE: Wird gerade gefüllt (grün).\n"
           "CHECKING PLAYLIST: Die Playlist wird gerade auf doppelte Videos geprüft."),

    ("h2", "Control"),
    ("dt", "START"),
    ("dd", "Startet für die ausgewählten Jahre. Ist ein Jahr fertig, geht es von selbst mit dem nächsten weiter."),
    ("dt", "PAUSE und RESUME"),
    ("dd", "PAUSE hält nach dem aktuellen Schritt an. Mit RESUME geht es genau dort weiter."),
    ("dt", "STOP"),
    ("dd", "Beendet den Lauf. Vorher prüft Chronolist die Playlist des aktuellen Jahres noch einmal auf doppelte "
           "Videos. Der Fortschritt bleibt gespeichert."),
    ("dt", "CHECK"),
    ("dd", "Prüft die Playlists der ausgewählten Jahre komplett. Doppelte Videos werden entfernt, wobei immer das "
           "erste stehen bleibt, damit die Reihenfolge stimmt. Außerdem wird der Fortschritt abgeglichen."),
    ("dt", "BATCH LIMIT"),
    ("dd", "So viele Videos werden pro Jahr und Durchlauf höchstens eingefügt. Mit den Knöpfen oder dem Mausrad "
           "änderst du den Wert in 50er-Schritten. UNLIMITED heißt: keine Grenze. Das ist praktisch, wenn du bei "
           "mehreren Jahren erst einmal nur die ersten Videos haben willst."),

    ("h2", "Options"),
    ("dt", "FALLBACK"),
    ("dd", "Wenn das Tageskontingent aufgebraucht ist, macht Chronolist über ein eigenes Edge-Fenster weiter, in dem "
           "du bei YouTube angemeldet bist. Das Fenster muss offen bleiben, minimieren geht aber.\n"
           "Wichtig: Das ist eine automatische Nutzung der YouTube-Webseite und laut den Nutzungsbedingungen von "
           "YouTube nicht erlaubt. Im schlimmsten Fall kann dein Konto gesperrt werden. Deshalb ist der Schalter am "
           "Anfang aus."),
    ("dt", "SKIP AHEAD"),
    ("dd", "Kann ein Jahr gerade nicht gesucht werden, weil das Kontingent oder das Suchlimit aufgebraucht ist, "
           "macht Chronolist mit dem nächsten Jahr weiter, das schon bereit ist. Das übersprungene Jahr kommt später "
           "dran. Ist der Schalter aus, hält Chronolist in diesem Fall an."),
    ("dt", "AUTO-RUN"),
    ("dd", "Startet automatisch, sobald du das Programm öffnest."),
    ("dt", "BOOT"),
    ("dd", "Startet Chronolist zusammen mit Windows. Zusammen mit AUTO-RUN läuft dann alles von allein."),

    ("h2", "Warum macht Chronolist Pausen?"),
    ("p", "Über das Edge-Fenster arbeitet Chronolist absichtlich nicht im Höchsttempo, damit es nicht wie ein Bot "
          "aussieht. Die Pausen siehst du mit Countdown auf der Jahreskarte und oben in der Statusanzeige."),
    ("dd", "NEXT BATCH: kurze Pause zwischen zwei Paketen mit je 15 Videos, 15 bis 30 Sekunden.\n"
           "COOLDOWN: etwa eine Minute nach jeweils 150 Videos.\n"
           "BREAK: etwa zehn Minuten nach jeweils 750 Videos.\n"
           "RATE LIMIT: YouTube bremst kurz, Chronolist wartet zehn Minuten.\n"
           "RETRY oder NO SIGNAL: kurzer Fehler oder keine Verbindung. Chronolist versucht es gleich noch einmal."),

    ("h2", "Was du sonst noch wissen solltest"),
    ("li", "Die Datumssuche für alte Jahre ist nicht ganz lückenlos. Einzelne Videos können fehlen. Jahre aus der "
           "Upload-Liste sind vollständig."),
    ("li", "In eine Playlist passen höchstens 5.000 Videos."),
    ("li", "Für die Webseite nennt YouTube keine Grenzen. Die Pausen in Chronolist sind eine vorsichtige Schätzung "
           "und keine Garantie."),

    ("h2", "System Log"),
    ("p", "Unten rechts steht jeder Schritt mit Uhrzeit. Grün heißt erledigt, Gelb ist ein Hinweis oder eine "
          "Wartezeit, Rot ist ein Fehler oder der Beginn eines neuen Abschnitts."),

    ("h2", "Wo liegen meine Daten?"),
    ("p", "Alles liegt im Ordner des Programms: credentials.json (Zugangsdaten), token.json (Anmeldung), "
          "settings.json (Einstellungen), quota.json (gemerktes Tageslimit), der Ordner data mit dem Fortschritt "
          "pro Kanal und Jahr und der Ordner browser_profile mit der Anmeldung im Edge-Fenster. Nichts davon wird "
          "irgendwohin hochgeladen."),

    ("h2", "Updates"),
    ("p", "Beim Start fragt Chronolist einmal bei GitHub nach, ob es eine neuere Version gibt. Wenn ja, erscheint oben "
          "neben der Versionsnummer ein grüner Hinweis, der zur Download-Seite führt. Dabei wird nichts außer dieser "
          "einen Anfrage übertragen. Abschalten: in settings.json \"check_updates\": false eintragen."),

    ("h2", "Über Chronolist"),
    ("p", f"Version {VERSION}, Build {BUILD:04d}. Open Source unter der MIT-Lizenz: {REPO_URL}"),
    ("p", "Chronolist wurde vollständig mit Claude programmiert, einer KI von Anthropic. Fehler, Wünsche und Fragen "
          "gerne als Issue auf GitHub."),
    ("p", "Chronolist ist ein unabhängiges Projekt und steht in keiner Verbindung zu YouTube oder Google. "
          "Du nutzt dabei deine eigenen API-Zugangsdaten und dein eigenes Konto. Für die Einhaltung der "
          "Nutzungsbedingungen von YouTube bist du selbst verantwortlich, besonders beim FALLBACK."),
]


def monitor_rect(x, y):
    """Arbeitsbereich des Monitors, auf dem der Punkt liegt (Windows, mehrere Monitore). None = unbekannt."""
    try:
        import ctypes
        from ctypes import wintypes

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]
        user32 = ctypes.windll.user32
        user32.MonitorFromPoint.restype = wintypes.HMONITOR
        user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
        hmon = user32.MonitorFromPoint(wintypes.POINT(int(x), int(y)), 2)   # MONITOR_DEFAULTTONEAREST
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        if not user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            return None
        r = mi.rcWork
        return r.left, r.top, r.right, r.bottom
    except Exception:  # noqa: BLE001
        return None


class Tooltip:
    """Kurze, normal lesbare Erklärung beim Drüberfahren mit der Maus (nach kurzer Verzögerung)."""

    def __init__(self, widget, text, delay=550):
        self.w, self.text, self.delay = widget, text, delay
        self.tip = self.job = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, e=None):
        self._cancel()
        self.job = self.w.after(self.delay, self._show)

    def _cancel(self):
        if self.job:
            self.w.after_cancel(self.job)
            self.job = None

    def _show(self):
        self.job = None
        text = self.text() if callable(self.text) else self.text
        if not text or self.tip:
            return
        base = "Segoe UI" if "Segoe UI" in tkfont.families(self.w) else "TkDefaultFont"
        self.tip = tk.Toplevel(self.w)
        self.tip.wm_overrideredirect(True)
        try:
            self.tip.attributes("-topmost", True)
        except tk.TclError:
            pass
        frame = tk.Frame(self.tip, bg=mix(CYAN, BG, 0.55), padx=1, pady=1)
        frame.pack()
        tk.Label(frame, text=text, bg="#1c1c28", fg="#ececf2", font=(base, -px(13)), justify="left",
                 wraplength=px(320), padx=px(10), pady=px(7)).pack()
        self.tip.update_idletasks()
        mx, my = self.w.winfo_pointerxy()
        tw, th = self.tip.winfo_reqwidth(), self.tip.winfo_reqheight()
        x, y = mx + px(12), my + px(20)              # direkt unter dem Mauszeiger
        area = monitor_rect(mx, my)
        if area:                                      # nur am Rand des jeweiligen Monitors verschieben
            left, top, right, bottom = area
            if x + tw > right - 4:
                x = max(left + 4, right - tw - 4)
            if y + th > bottom - 4:
                y = my - th - px(10)
        self.tip.geometry(f"+{x}+{y}")

    def _hide(self, e=None):
        self._cancel()
        if self.tip:
            self.tip.destroy()
            self.tip = None


class HelpWindow(tk.Toplevel):
    """Hilfe in normaler, gut lesbarer Sprache und Schrift, bewusst ohne Neon-Stil."""

    def __init__(self, master):
        super().__init__(master, bg="#16161f")
        self.title("Chronolist Hilfe")
        self.geometry(f"{px(720)}x{px(760)}")
        self.minsize(px(480), px(400))
        base = "Segoe UI" if "Segoe UI" in tkfont.families(self) else "TkDefaultFont"
        txt = tk.Text(self, wrap="word", bg="#16161f", fg="#e8e8ee", bd=0, relief="flat",
                      highlightthickness=0, padx=px(28), pady=px(22), cursor="arrow",
                      font=(base, -px(15)), spacing1=px(2), spacing3=px(6), selectbackground="#3a3a55")
        sb = ttk.Scrollbar(self, orient="vertical", command=txt.yview, style="NC.Vertical.TScrollbar")
        txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        txt.pack(side="left", fill="both", expand=True)
        txt.tag_configure("h1", font=(base, -px(24), "bold"), foreground="#ffffff", spacing3=px(10))
        txt.tag_configure("h2", font=(base, -px(18), "bold"), foreground=CYAN, spacing1=px(18), spacing3=px(6))
        txt.tag_configure("p", lmargin1=0, lmargin2=0)
        txt.tag_configure("dt", font=(base, -px(15), "bold"), foreground="#ffffff", spacing1=px(8), spacing3=px(1))
        txt.tag_configure("dd", lmargin1=px(16), lmargin2=px(16), foreground="#c9c9d6")
        txt.tag_configure("li", lmargin1=px(16), lmargin2=px(32))
        txt.tag_configure("num", lmargin1=px(8), lmargin2=px(34), spacing1=px(6))
        txt.tag_configure("sub", lmargin1=px(34), lmargin2=px(52), foreground="#c9c9d6")
        txt.tag_configure("cont", lmargin1=px(34), lmargin2=px(34))
        for kind, text in HELP_TEXT:
            if kind == "li":
                txt.insert("end", "•  " + text + "\n", "li")
            elif kind == "num":
                first, *rest = text.split("\n")
                txt.insert("end", first + "\n", "num")
                for line in rest:
                    txt.insert("end", line + "\n", "sub" if line.startswith("•") else "cont")
            else:
                txt.insert("end", text + "\n", kind)
        txt.configure(state="disabled")
        txt.bind("<MouseWheel>", lambda e: txt.yview_scroll(int(-e.delta / 120), "units"))
        style_titlebar(self)
        self.transient(master)
        self.focus_force()


class LoginPanel(Panel):
    def __init__(self, master, on_url, on_copy):
        super().__init__(master, 116, state="frame")
        self.url, self.code, self.on_url, self.on_copy = "", "", on_url, on_copy

    def draw_content(self, x0, y0, x1, y1, c, role):
        x = x0 + px(14)
        self.create_rectangle(x, y0 + px(14), x + px(6), y0 + px(20), fill=RED, outline="")
        self.create_text(x + px(14), y0 + px(17), text="GOOGLE AUTH // LINK REQUIRED", anchor="w",
                         font=F("ui", 15), fill=RED)
        self.create_text(x, y0 + px(46), text="OPEN", anchor="w", font=F("mono", 11), fill=TEXT_MUTED)
        link = self.create_text(x + px(70), y0 + px(46), text=self.url, anchor="w", font=F("mono", 12),
                                fill=CYAN)
        self.tag_bind(link, "<Button-1>", lambda e: self.on_url())
        self.tag_bind(link, "<Enter>", lambda e: (self.configure(cursor="hand2"),
                                                 self.itemconfigure(link, fill=CYAN_HOT)))
        self.tag_bind(link, "<Leave>", lambda e: (self.configure(cursor=""),
                                                 self.itemconfigure(link, fill=CYAN)))
        self.create_text(x, y0 + px(80), text="ENTER CODE", anchor="w", font=F("mono", 11), fill=TEXT_MUTED)
        code = self.create_text(x + px(110), y0 + px(80), text=self.code, anchor="w", font=F("mono", 24),
                                fill=YELLOW)
        hint = self.create_text(self.bbox(code)[2] + px(14), y0 + px(82), text="CLICK TO COPY", anchor="w",
                                font=F("mono", 10, False), fill=FLUFF)
        for item in (code, hint):
            self.tag_bind(item, "<Button-1>", lambda e: self.copied(hint))
            self.tag_bind(item, "<Enter>", lambda e: (self.configure(cursor="hand2"),
                                                     self.itemconfigure(code, fill=mix("#ffffff", YELLOW, 0.35))))
            self.tag_bind(item, "<Leave>", lambda e: (self.configure(cursor=""),
                                                     self.itemconfigure(code, fill=YELLOW)))

    def copied(self, hint):
        self.on_copy()
        self.itemconfigure(hint, text="COPIED ✓", fill=GREEN)
        self.after(1500, lambda: self.itemconfigure(hint, text="CLICK TO COPY", fill=FLUFF))


class CutButton(tk.Canvas):
    """Eckiger Button, zwei abgeschrägte Ecken, Cyan-Rahmen."""

    def __init__(self, master, text, command, width=0):
        f = tkfont.Font(family=FONT_MONO, size=-px(12), weight="bold")
        w = max(px(width), f.measure(text) + px(36))
        super().__init__(master, width=w, height=px(36), bg=BG, highlightthickness=0, bd=0, cursor="hand2")
        self.text, self.command = text, command
        self.enabled, self.hover = True, False
        self.color = CYAN
        self.bind("<Configure>", lambda e: self.redraw())
        self.bind("<Enter>", lambda e: self._hover(True))
        self.bind("<Leave>", lambda e: self._hover(False))
        self.bind("<ButtonRelease-1>", self._click)

    def _hover(self, on):
        self.hover = on
        self.redraw()

    def _click(self, e):
        if self.enabled and 0 <= e.x <= self.winfo_width() and 0 <= e.y <= self.winfo_height():
            self.command()

    def set_enabled(self, on):
        self.enabled = on
        self.configure(cursor="hand2" if on else "")
        self.redraw()

    def set_text(self, text):
        self.text = text
        self.redraw()

    def set_color(self, color):
        if color != self.color:
            self.color = color
            self.redraw()

    def redraw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        if w < 10:
            return
        m, c = px(3), px(7)
        x0, y0, x1, y1 = m, m, w - m - 1, h - m - 1
        col = self.color
        if not self.enabled:
            fill, edge, tcol = mix(col, BG, 0.06), mix(col, BG, 0.35), mix(col, BG, 0.5)
        elif self.hover:
            fill, edge, tcol = mix(col, BG, 0.22), mix("#ffffff", col, 0.25), mix("#ffffff", col, 0.25)
            for d, a in ((1, 0.35), (2, 0.15)):
                o = shape(x0 - d, y0 - d, x1 + d, y1 + d, c + 0.586 * d, "trbl")
                self.create_polygon(flat(o), fill="", outline=mix(col, BG, a))
        else:
            fill, edge, tcol = mix(col, BG, 0.20), mix(col, BG, 0.9), col
        self.create_polygon(flat(shape(x0, y0, x1, y1, c, "trbl")), fill=fill, outline=edge)
        self.create_text(w / 2, h / 2, text=self.text, font=F("mono", 12), fill=tcol)


class Section(tk.Canvas):
    """Rotes Abschnitts-Label mit Linie."""

    def __init__(self, master, text, fluff=""):
        super().__init__(master, height=px(26), bg=BG, highlightthickness=0, bd=0)
        self.text, self.fluff = text, fluff
        self.bind("<Configure>", lambda e: self.redraw())

    def redraw(self):
        self.delete("all")
        w = self.winfo_width()
        y = px(13)
        self.create_rectangle(0, y - px(3), px(6), y + px(3), fill=RED, outline="")
        t = self.create_text(px(14), y, text=self.text, anchor="w", font=F("mono", 12), fill=RED)
        x = self.bbox(t)[2] + px(10)
        right = w - px(4)
        if self.fluff:
            f = self.create_text(right, y, text=self.fluff, anchor="e", font=F("mono", 10, False), fill=FLUFF)
            right = self.bbox(f)[0] - px(10)
        if right > x:
            self.create_line(x, y, right, y, fill=RED_MILD)


class Header(tk.Canvas):
    def __init__(self, master):
        super().__init__(master, height=px(88), bg=BG, highlightthickness=0, bd=0)
        self.chip = StatusChip(self)
        self.api_btn = None      # wird von der App gesetzt
        self.help_btn = None
        self.update = None       # (version, url) wenn eine neuere Release existiert
        self.title_end = 0
        self.bind("<Configure>", lambda e: self.redraw())
        self.after(8000, self.glitch)

    def redraw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        lsz = px(54)
        draw_logo(self, px(18), px(18), lsz)
        x, y = px(18) + lsz + px(16), px(34)
        title = "\u200a".join("CHRONOLIST")
        f = F("ui", 26)
        self.create_text(x + px(2), y, text=title, anchor="w", font=f, fill=mix(CYAN, BG, 0.8), tags="title")
        self.create_text(x - px(2), y, text=title, anchor="w", font=f, fill=mix(RED, BG, 0.8), tags="title")
        t = self.create_text(x, y, text=title, anchor="w", font=f, fill=RED, tags="title")
        # Versions-Tag rechts neben dem Titel
        vx0 = self.bbox(t)[2] + px(14)
        vtxt = f"v{VERSION}"
        vf = tkfont.Font(family=FONT_MONO, size=-px(12), weight="bold")
        vx1 = vx0 + vf.measure(vtxt) + px(18)
        vy0, vy1, vc = y - px(11), y + px(11), px(6)
        self.create_polygon(flat(shape(vx0, vy0, vx1, vy1, vc, "trbl")), fill=mix(YELLOW, BG, 0.12),
                            outline=mix(YELLOW, BG, 0.75))
        self.create_text((vx0 + vx1) / 2, y, text=vtxt, font=vf, fill=YELLOW)
        bt = self.create_text(vx1 + px(10), y, text=f"BUILD {BUILD:04d}", anchor="w",
                              font=F("mono", 10, False), fill=FLUFF)
        self.title_end = self.bbox(bt)[2] + px(20)
        if self.update:
            ut = self.create_text(self.title_end - px(6), y, text=f"UPDATE v{self.update[0]} ›", anchor="w",
                                  font=F("mono", 11), fill=GREEN, tags="upd")
            self.tag_bind("upd", "<Button-1>", lambda e: webbrowser.open(self.update[1]))
            self.tag_bind("upd", "<Enter>", lambda e: self.configure(cursor="hand2"))
            self.tag_bind("upd", "<Leave>", lambda e: self.configure(cursor=""))
            self.title_end = self.bbox(ut)[2] + px(20)
        sub_t = self.create_text(x, px(64), text=f"PLAYLIST UPLINK // {(CHANNEL.get('handle') or 'NO TARGET').upper()} // UPLOAD ORDER", anchor="w",
                         font=F("mono", 11), fill=TEXT_MUTED)
        self.title_end = max(self.title_end, self.bbox(sub_t)[2] + px(20))

        chip_w = px(210)
        cx1 = w - px(18)
        cx0 = cx1 - chip_w
        self.create_text(cx1, px(12), text="CONNECTION 201.89.43 // SYS.ID 0x7F3A", anchor="e",
                         font=F("mono", 10, False), fill=FLUFF)
        self.create_window(cx0, px(46), window=self.chip, anchor="w", width=chip_w, height=px(36))
        bx = cx0 - px(12)
        if self.api_btn is not None:
            bw_ = self.api_btn.winfo_reqwidth()
            self.create_window(bx, px(46), window=self.api_btn, anchor="e")
            bx -= bw_ + px(8)
        if self.help_btn is not None:
            bw_ = self.help_btn.winfo_reqwidth()
            self.create_window(bx, px(46), window=self.help_btn, anchor="e")
            bx -= bw_ + px(16)
        # Deko nur, wenn neben dem Titel Platz ist
        if bx - px(110) > self.title_end:
            for bw in (1, 3, 1, 2, 1, 1, 3, 1, 2, 1):
                self.create_rectangle(bx - px(bw), px(30), bx, px(60), fill=RED_MILD, outline="")
                bx -= px(bw) + px(2)
            self.create_text(bx - px(10), px(45), text="01101001\n01010000", anchor="e", justify="right",
                             font=F("mono", 10, False), fill=FLUFF)

        yb = h - px(4)
        self.create_line(px(18), yb, w - px(18), yb, fill=RED_MILD)
        self.create_line(px(18), yb, px(96), yb, fill=RED, width=px(2))
        for i in range(3):
            xx = w - px(18) - i * px(6)
            self.create_line(xx, yb - px(4), xx, yb, fill=RED_MILD)

    def glitch(self):
        seq = [(-2, 1), (4, -2), (-3, 0), (1, 1)]

        def step(i=0):
            if i < len(seq):
                dx, dy = seq[i]
                self.move("title", round(dx * S), round(dy * S))
                self.after(60, step, i + 1)
        step()
        self.after(8000, self.glitch)


class Footer(tk.Canvas):
    def __init__(self, master):
        super().__init__(master, height=px(24), bg=BG, highlightthickness=0, bd=0)
        self.bind("<Configure>", lambda e: self.redraw())

    def redraw(self):
        self.delete("all")
        w = self.winfo_width()
        y = px(12)
        self.create_text(px(18), y, text="OPEN SOURCE // MIT  ·  LINK STABLE", anchor="w",
                         font=F("mono", 10, False), fill=FLUFF)
        self.create_text(w - px(18), y, text="ARCHIVE %s // FW %s.%04d // 01001110" % (
                             f"{YEARS[0]}-{YEARS[-1]}" if YEARS else "--", VERSION, BUILD),
                         anchor="e", font=F("mono", 10, False), fill=FLUFF)


class ScrollArea(tk.Frame):
    def __init__(self, master):
        super().__init__(master, bg=BG)
        self.canvas = tk.Canvas(self, bg=BG, highlightthickness=0, bd=0, yscrollincrement=px(20))
        self.sb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview,
                                style="NC.Vertical.TScrollbar")
        self.inner = tk.Frame(self.canvas, bg=BG)
        self.win = self.canvas.create_window(0, 0, window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.sb.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.sb.grid(row=0, column=1, sticky="ns", padx=(px(6), 0))
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self.win, width=e.width))
        self.wheel(self.canvas)

    def wheel(self, widget):
        widget.bind("<MouseWheel>", lambda e: self.canvas.yview_scroll(int(-e.delta / 120), "units"))
        widget.bind("<Button-4>", lambda e: self.canvas.yview_scroll(-1, "units"))
        widget.bind("<Button-5>", lambda e: self.canvas.yview_scroll(1, "units"))


def enable_dpi_awareness():
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:  # noqa: BLE001
        pass


def style_titlebar(root):
    """Dunkle Titelleiste mit rotem Rand (Windows 11; Windows 10 nur dunkel)."""
    try:
        import ctypes
        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())

        def dwm(attr, value):
            v = ctypes.c_int(value)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(v), ctypes.sizeof(v))

        def colorref(hexcol):
            r, g, b = (int(hexcol[i:i + 2], 16) for i in (1, 3, 5))
            return (b << 16) | (g << 8) | r

        dwm(20, 1)                      # Dark Mode
        dwm(35, colorref(BG))           # Titelleiste
        dwm(34, colorref(RED_MILD))     # Fensterrand
        dwm(36, colorref(RED))          # Titeltext
        # Kein Mica/Acrylic-Hintergrund durch den Fensterinhalt scheinen lassen:
        # Backdrop aus, Glas-Rahmen nicht in den Inhalt ziehen.
        dwm(38, 1)                      # DWMWA_SYSTEMBACKDROP_TYPE = NONE

        class MARGINS(ctypes.Structure):
            _fields_ = [("l", ctypes.c_int), ("r", ctypes.c_int), ("t", ctypes.c_int), ("b", ctypes.c_int)]
        ctypes.windll.dwmapi.DwmExtendFrameIntoClientArea(hwnd, ctypes.byref(MARGINS(0, 0, 0, 0)))
    except Exception:  # noqa: BLE001
        pass


def setup_theme(root):
    global S, FONT_UI, FONT_MONO
    S = max(1.0, root.winfo_fpixels("1i") / 96)
    fams = set(tkfont.families(root))
    FONT_UI = next((f for f in ("Rajdhani", "Orbitron", "Segoe UI") if f in fams), "TkDefaultFont")
    FONT_MONO = next((f for f in ("Consolas", "Cascadia Mono", "Courier New") if f in fams), "TkFixedFont")
    style = ttk.Style(root)
    style.theme_use("clam")
    style.layout("NC.Vertical.TScrollbar", [
        ("Vertical.Scrollbar.trough", {"sticky": "ns", "children": [
            ("Vertical.Scrollbar.thumb", {"expand": "1", "sticky": "nswe"})]})])
    style.configure("NC.Vertical.TScrollbar", troughcolor=BG, background=RED_DARK, bordercolor=BG,
                    lightcolor=RED_DARK, darkcolor=RED_DARK, arrowsize=px(6), width=px(6),
                    relief="flat", borderwidth=0, gripcount=0)
    style.map("NC.Vertical.TScrollbar", background=[("active", RED_MILD)],
              lightcolor=[("active", RED_MILD)], darkcolor=[("active", RED_MILD)])


# ----------------------------------------------------------------------- App
class App(tk.Tk):
    def __init__(self):
        enable_dpi_awareness()
        super().__init__()
        setup_theme(self)
        self.title(f"{APP_NAME.upper()}  v{VERSION}")
        self.configure(bg=BG)
        self.geometry(f"{px(900)}x{px(760)}")
        self.minsize(px(900), px(640))
        style_titlebar(self)
        try:
            self._icons = [logo_photo(self, 64), logo_photo(self, 32), logo_photo(self, 16)]
            self.iconphoto(True, *self._icons)
        except Exception:  # noqa: BLE001
            pass
        for ms in (500, 2000, 5000):
            self.after(ms, lambda: style_titlebar(self))

        self.settings = load(SETTINGS_FILE, {"years": [], "autorun": False, "limit": 0})
        apply_templates(self.settings)
        set_channel(self.settings.get("channel"))
        self.out = queue.Queue()
        self.worker = None
        self.code = ""
        self.login_url = ""
        self.limit_val = max(0, int(self.settings.get("limit", 0) or 0))

        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        self.header = Header(self)
        self.header.grid(row=0, column=0, sticky="ew")

        body = tk.Frame(self, bg=BG)
        body.grid(row=1, column=0, sticky="nsew", padx=px(16), pady=(px(8), 0))
        body.columnconfigure(0, weight=5, uniform="col")
        body.columnconfigure(1, weight=6, uniform="col")
        body.rowconfigure(0, weight=1)

        # --- links: Jahre
        left = tk.Frame(body, bg=BG)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, px(14)))
        left.rowconfigure(3, weight=1)
        left.columnconfigure(0, weight=1)
        Section(left, "TARGET CHANNEL", "YOUTUBE // HANDLE").grid(row=0, column=0, sticky="ew", pady=(0, px(6)))
        self.channel_card = ChannelCard(left, self.edit_channel)
        self.channel_card.grid(row=1, column=0, sticky="ew", pady=(0, px(16)))
        self.channel_card.update_state()
        Section(left, "TARGET YEARS", "CLICK TO TOGGLE").grid(row=2, column=0, sticky="ew", pady=(0, px(6)))
        self.scroll = ScrollArea(left)
        self.scroll.grid(row=3, column=0, sticky="nsew")
        self.rows = {}
        self.build_rows()

        # --- rechts: Steuerung, Optionen, Login, Log
        right = tk.Frame(body, bg=BG)
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(6, weight=1)

        Section(right, "CONTROL", "HUD // STATUS").grid(row=0, column=0, sticky="ew", pady=(0, px(6)))
        btns = tk.Frame(right, bg=BG)
        btns.grid(row=1, column=0, sticky="ew", pady=(0, px(8)))
        self.start_btn = CutButton(btns, "▶  START", self.start, width=100)
        self.start_btn.pack(side="left")
        self.pause_btn = CutButton(btns, "❚❚  PAUSE", self.toggle_pause, width=112)
        self.pause_btn.pack(side="left", padx=(px(6), 0))
        self.pause_btn.set_enabled(False)
        self.stop_btn = CutButton(btns, "■  STOP", self.stop, width=96)
        self.stop_btn.pack(side="left", padx=(px(6), 0))
        self.check_btn = CutButton(btns, "✓  CHECK", self.run_check, width=104)
        self.check_btn.pack(side="left", padx=(px(6), 0))
        self.setup_btn = CutButton(self.header, "API // MISSING", self.edit_credentials, width=150)
        self.header.api_btn = self.setup_btn
        self.help_btn = CutButton(self.header, "?  HELP", self.show_help, width=86)
        self.header.help_btn = self.help_btn
        self.header.redraw()
        self.api_ok = None   # None = unbekannt, True = Login/Aufruf erfolgreich, False = Fehler
        self.update_api_button()
        self.stop_btn.set_enabled(False)

        lim = tk.Frame(right, bg=BG)
        lim.grid(row=2, column=0, sticky="ew", pady=(0, px(4)))
        lim.columnconfigure(0, weight=1)
        self.limit_panel = LimitPanel(lim, self.limit_val)
        Tooltip(self.limit_panel, "Höchstens so viele Videos pro Jahr und Lauf einfügen. "
                                  "Mit den Knöpfen daneben oder dem Mausrad in 50er-Schritten. UNLIMITED heißt keine Grenze.")
        self.limit_panel.grid(row=0, column=0, sticky="ew")
        for w in (self.limit_panel,):
            w.bind("<MouseWheel>", lambda e: self.change_limit(50 if e.delta > 0 else -50))
            w.bind("<Button-4>", lambda e: self.change_limit(50))
            w.bind("<Button-5>", lambda e: self.change_limit(-50))
        b_minus = CutButton(lim, "−", lambda: self.change_limit(-50), width=40)
        b_minus.grid(row=0, column=1, padx=(px(6), 0))
        b_plus = CutButton(lim, "+", lambda: self.change_limit(50), width=40)
        b_plus.grid(row=0, column=2, padx=(px(4), 0))
        Tooltip(b_minus, "Batch-Limit um 50 verringern.")
        Tooltip(b_plus, "Batch-Limit um 50 erhöhen.")

        Section(right, "OPTIONS", "RUN // SYSTEM").grid(row=3, column=0, sticky="ew", pady=(px(6), px(6)))
        opts = tk.Frame(right, bg=BG)
        opts.grid(row=4, column=0, sticky="ew", pady=(0, px(8)))
        opts.columnconfigure(0, weight=1, uniform="opt")
        opts.columnconfigure(1, weight=1, uniform="opt")
        # Zeile 1: Ablauf
        self.fallback = bool(self.settings.get("browser_fallback", False))
        self.fallback_card = ToggleCard(opts, "FALLBACK", "EDGE AFTER QUOTA",
                                        self.fallback, self.toggle_fallback, compact=True)
        self.fallback_card.grid(row=0, column=0, sticky="ew", padx=(0, px(3)), pady=(0, px(4)))
        self.skip_ahead = bool(self.settings.get("skip_ahead", False))
        self.skip_card = ToggleCard(opts, "SKIP AHEAD", "NEXT READY YEAR",
                                    self.skip_ahead, self.toggle_skip, compact=True)
        self.skip_card.grid(row=0, column=1, sticky="ew", padx=(px(3), 0), pady=(0, px(4)))
        # Zeile 2: System
        self.autorun = bool(self.settings.get("autorun", False))
        self.autorun_card = ToggleCard(opts, "AUTO-RUN", "ON LAUNCH", self.autorun, self.toggle_autorun,
                                       compact=True)
        self.autorun_card.grid(row=1, column=0, sticky="ew", padx=(0, px(3)))
        self.win_on = get_windows_autostart()
        self.win_card = ToggleCard(opts, "BOOT", "WITH WINDOWS", self.win_on,
                                   self.toggle_windows, available=winreg is not None, compact=True)
        self.win_card.grid(row=1, column=1, sticky="ew", padx=(px(3), 0))

        self.login = tk.Frame(right, bg=BG)
        self.login.columnconfigure(0, weight=1)
        self.login_panel = LoginPanel(self.login, self.open_login, self.copy_code)
        self.login_panel.grid(row=0, column=0, sticky="ew")
        lb = tk.Frame(self.login, bg=BG)
        lb.grid(row=1, column=0, sticky="w", pady=(px(4), px(8)))
        b_open = CutButton(lb, "OPEN LINK", self.open_login)
        b_open.pack(side="left")
        b_copy = CutButton(lb, "COPY CODE", self.copy_code)
        b_copy.pack(side="left", padx=(px(8), 0))
        Tooltip(b_open, "Öffnet die Google-Anmeldeseite im Browser.")
        Tooltip(b_copy, "Kopiert den Anmeldecode in die Zwischenablage. Auf der Google-Seite einfügen und bestätigen.")

        logwrap = tk.Frame(right, bg=BG)
        logwrap.grid(row=6, column=0, sticky="nsew")
        logwrap.columnconfigure(0, weight=1)
        logwrap.rowconfigure(1, weight=1)
        Section(logwrap, "SYSTEM LOG", "NET // 0x7F3A").grid(row=0, column=0, sticky="ew", pady=(0, px(6)))
        self.log_panel = Panel(logwrap, 200, state="frame")
        self.log_panel.grid(row=1, column=0, sticky="nsew")
        inner = tk.Frame(self.log_panel, bg=BG)
        inner.columnconfigure(0, weight=1)
        inner.rowconfigure(0, weight=1)
        self.logbox = tk.Text(inner, state="disabled", wrap="word", bg=BG, fg=TEXT_DIM, bd=0,
                              relief="flat", highlightthickness=0, font=F("mono", 14, False),
                              padx=px(4), pady=px(2), selectbackground=mix(CYAN, BG, 0.3),
                              insertbackground=CYAN, spacing1=px(4), spacing3=px(2))
        self.logbox.grid(row=0, column=0, sticky="nsew")
        lsb = ttk.Scrollbar(inner, orient="vertical", command=self.logbox.yview,
                            style="NC.Vertical.TScrollbar")
        lsb.grid(row=0, column=1, sticky="ns", padx=(px(4), 0))
        self.logbox.configure(yscrollcommand=lsb.set)
        # Log folgt der neuesten Meldung, solange man nicht selbst nach oben scrollt
        self.log_follow = True

        def update_follow(_e=None):
            self.after_idle(lambda: setattr(self, "log_follow", self.logbox.yview()[1] >= 0.999))

        def keep_bottom(_e=None):
            if self.log_follow:
                self.after_idle(lambda: self.logbox.see("end"))
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>", "<KeyRelease>"):
            self.logbox.bind(seq, update_follow, add="+")
        lsb.bind("<ButtonRelease-1>", update_follow, add="+")
        lsb.bind("<B1-Motion>", update_follow, add="+")
        self.logbox.bind("<Configure>", keep_bottom, add="+")
        for tag, col in (("ts", RED_MILD), ("info", TEXT_DIM), ("ok", GREEN), ("warn", YELLOW),
                         ("err", RED), ("sep", RED)):
            self.logbox.tag_configure(tag, foreground=col)
        self.log_panel.windows.append(
            lambda x0, y0, x1, y1, c: self.log_panel.create_window(
                x0 + px(10), y0 + px(10), window=inner, anchor="nw",
                width=max(1, x1 - x0 - px(20)), height=max(1, y1 - y0 - px(14) - c)))

        self.footer = Footer(self)
        self.footer.grid(row=2, column=0, sticky="ew", pady=(px(4), px(2)))

        self.add_tooltips()
        self.after(60000, self.api_tick)
        self.set_status("idle")
        self.write_log("CHRONOLIST ONLINE // SYSTEMS NOMINAL", "info")
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(200, self.poll)
        self.after(40, self.animate)
        if self.settings.get("check_updates", True):
            threading.Thread(target=check_for_update, args=(self.out,), daemon=True).start()
        if self.autorun:
            self.after(1500, self.start)

    # --- Einstellungen
    def selected_years(self):
        return sorted(y for y, r in self.rows.items() if r.selected)

    def save_settings(self):
        self.settings.update(years=self.selected_years(), autorun=self.autorun, limit=self.limit_val,
                             browser_fallback=getattr(self, "fallback", False),
                             skip_ahead=getattr(self, "skip_ahead", False))
        save(SETTINGS_FILE, self.settings)

    def toggle_year(self, y):
        row = self.rows[y]
        row.refresh(selected=not row.selected)
        self.save_settings()

    def change_limit(self, delta):
        self.limit_val = max(0, min(5000, self.limit_val + delta))
        self.limit_panel.value = self.limit_val
        self.limit_panel.redraw()
        self.save_settings()

    def toggle_autorun(self):
        self.autorun = not self.autorun
        self.autorun_card.update_value(self.autorun)
        self.save_settings()

    def toggle_skip(self):
        self.skip_ahead = not self.skip_ahead
        self.skip_card.update_value(self.skip_ahead)
        self.save_settings()
        if self.worker and self.worker.is_alive():
            self.worker.skip_ahead = self.skip_ahead

    def toggle_fallback(self):
        self.fallback = not self.fallback
        self.fallback_card.update_value(self.fallback)
        self.save_settings()
        if self.worker and self.worker.is_alive():
            self.worker.fallback = self.fallback

    def toggle_windows(self):
        new = not self.win_on
        try:
            set_windows_autostart(new)
        except OSError as e:
            self.write_log(f"BOOT SEQUENCE UPDATE FAILED // {e}", "err")
            return
        self.win_on = new
        self.win_card.update_value(new)

    # --- Status
    def set_status(self, key):
        text, role, state, blink = {
            "idle": ("STANDBY", CYAN, "inactive", False),
            "running": ("RUNNING", GREEN, "running", False),
            "browser": ("BROWSER UPLINK", GREEN, "running", False),
            "browser_login": ("SIGN IN // EDGE", YELLOW, "active", True),
            "checking": ("PLAYLIST CHECK...", CYAN, "active", True),
            "stopping": ("STOPPING...", CYAN, "inactive", True),
            "fertig": ("COMPLETE", CYAN, "active", False),
            "gestoppt": ("HALTED", RED, "frame", False),
            "quota": ("DAILY QUOTA REACHED", YELLOW, "active", False),
            "fehler": ("ERROR // SEE LOG", RED, "frame", False),
        }.get(key, (key.upper(), CYAN, "inactive", False))
        self.header.chip.show(text, role, state, blink)

    def animate(self):
        a = blink_alpha()
        self.header.chip.blink_tick(a)
        now = time.time()
        p = getattr(self, "pause_info", None)
        if p:
            label = pause_label(p, now)
            if label != getattr(self, "chip_label", None):
                self.chip_label = label
                if p["kind"] == "manual":
                    self.header.chip.show(label, YELLOW, "active", False)
                else:
                    self.header.chip.show(label, GREEN, "running", False)
        for r in self.rows.values():
            r.blink_tick(a)
            if r.running:
                r.tick(now)
        self.after(40, self.animate)

    # --- Start / Stopp
    # --- Kanal / Zugangsdaten
    def build_rows(self):
        for r in self.rows.values():
            r.destroy()
        self.rows = {}
        selected = set(self.settings.get("years", []))
        for y in YEARS:
            row = YearRow(self.scroll.inner, y, command=lambda y=y: self.toggle_year(y))
            Tooltip(row, lambda r=row: self.year_tip(r))
            row.pack(fill="x", pady=(0, px(4)))
            row.refresh(selected=y in selected)
            self.scroll.wheel(row)
            self.rows[y] = row
        self.scroll.canvas.yview_moveto(0)

    def busy(self):
        if self.worker and self.worker.is_alive():
            self.write_log("STOP THE CURRENT RUN FIRST", "err")
            return True
        return False

    def edit_credentials(self):
        if self.busy():
            return
        cid, sec = get_creds()
        NCDialog(self, "API Setup", [
            ("client_id", "CLIENT ID", cid, False),
            ("client_secret", "CLIENT SECRET", sec, True),
        ], "GOOGLE CLOUD CONSOLE // YOUTUBE DATA API V3 // OAUTH CLIENT TYPE: TVS AND LIMITED INPUT DEVICES. "
           "STORED LOCALLY IN CREDENTIALS.JSON NEXT TO THE PROGRAM.", self.save_credentials)

    def year_tip(self, row):
        sel = "Ausgewählt, wird beim START bearbeitet." if row.selected else "Klicken, um dieses Jahr auszuwählen."
        if row.selected:
            sel += " Erneut klicken zum Abwählen."
        info = row.info
        state = {"none": "Videos noch nicht gesucht.", "scanning": "Suche angefangen, aber noch nicht fertig.",
                 "ready": f"{info['total']} Videos gefunden, Playlist noch leer.",
                 "progress": f"{info['done']} von {info['total']} Videos eingefügt.",
                 "done": f"Fertig, alle {info['total']} Videos sind in der Playlist."}.get(info["code"], "")
        return f"{row.year}: {state}\n{sel}"

    def add_tooltips(self):
        Tooltip(self.help_btn, "Öffnet die ausführliche Hilfe zu allen Funktionen.")
        Tooltip(self.setup_btn, lambda: {
            YELLOW: "Das Tageskontingent ist aufgebraucht. Bis zum Reset fragt Chronolist nicht mehr nach und nimmt "
                    "direkt den Fallback, wenn er eingeschaltet ist."
                    + (f" Reset: {fmt_reset(quota_blocked('units'))}." if quota_blocked("units") else ""),
            RED: "Zugang zu YouTube fehlt oder wurde abgelehnt. Klicken, um Client ID und Secret einzutragen.",
            GREEN: "Zugang zu YouTube funktioniert. Klicken, um die Zugangsdaten zu ändern.",
        }.get(self.setup_btn.color, "Zugangsdaten sind eingetragen, die Anmeldung bei Google folgt beim nächsten START."))
        Tooltip(self.header.chip, "Zeigt, was Chronolist gerade macht. Bei Pausen läuft hier ein Countdown.")
        Tooltip(self.channel_card, "Der Kanal, dessen Videos gesammelt werden. Über EDIT einen anderen Kanal eingeben "
                                   "(@Name, Link oder Kanal-ID).")
        Tooltip(self.start_btn, "Startet die Arbeit für die links ausgewählten Jahre. Fertige Jahre gehen automatisch "
                                "ins nächste über.")
        Tooltip(self.pause_btn, "Hält nach dem aktuellen Schritt an. RESUME macht genau dort weiter.")
        Tooltip(self.stop_btn, "Beendet den Lauf. Vorher wird die aktuelle Playlist noch einmal auf Doppelte geprüft. "
                               "Der Fortschritt bleibt gespeichert.")
        Tooltip(self.check_btn, "Prüft die Playlists der ausgewählten Jahre: entfernt doppelte Videos und gleicht den "
                                "Fortschritt ab.")
        Tooltip(self.fallback_card, "Wenn das tägliche YouTube-Kontingent leer ist, über ein eigenes Edge-Fenster "
                                    "weitermachen. Achtung: verstößt gegen die YouTube-Regeln, Risiko für dein Konto.")
        Tooltip(self.skip_card, "Kann ein Jahr gerade nicht gesucht werden, mit dem nächsten fertigen Jahr "
                                "weitermachen und das übersprungene später nachholen.")
        Tooltip(self.autorun_card, "Beim Öffnen von Chronolist automatisch starten.")
        Tooltip(self.win_card, "Chronolist automatisch mit Windows starten.")
        Tooltip(self.login_panel, "Google-Anmeldung: Link öffnen, Code eingeben, bestätigen. Klick auf den Code kopiert ihn.")

    def api_tick(self):
        self.update_api_button()
        self.after(60000, self.api_tick)

    def show_help(self):
        w = getattr(self, "help_win", None)
        if w is not None and w.winfo_exists():
            w.lift()
            w.focus_force()
            return
        self.help_win = HelpWindow(self)

    def update_api_button(self):
        """Rot = keine Zugangsdaten oder Fehler, Grün = angemeldet und funktioniert, Cyan = gesetzt, ungeprüft."""
        if not all(get_creds()) or self.api_ok is False:
            self.setup_btn.set_color(RED)
            self.setup_btn.set_text("API // MISSING" if not all(get_creds()) else "API // ERROR")
        elif quota_blocked("units"):
            self.setup_btn.set_color(YELLOW)
            self.setup_btn.set_text("API // LIMIT")
        elif self.api_ok or TOKEN_FILE.exists():
            self.setup_btn.set_color(GREEN)
            self.setup_btn.set_text("API // ONLINE")
        else:
            self.setup_btn.set_color(CYAN)
            self.setup_btn.set_text("API // SIGN IN")

    def save_credentials(self, v):
        if not v["client_id"] or not v["client_secret"]:
            self.write_log("CLIENT ID AND SECRET REQUIRED", "err")
            return False
        if (v["client_id"], v["client_secret"]) != get_creds():
            save(CREDENTIALS_FILE, v)
            if TOKEN_FILE.exists():
                TOKEN_FILE.unlink()   # alter Login gehört zu den alten Zugangsdaten
            if QUOTA_FILE.exists():
                QUOTA_FILE.unlink()   # Limit gilt pro Google-Projekt
            self.write_log("API CREDENTIALS UPDATED // NEW GOOGLE SIGN-IN ON NEXT START", "ok")
            self.api_ok = None
            self.update_api_button()

    def edit_channel(self):
        if self.busy():
            return
        if not all(get_creds()):
            self.write_log("SET API CREDENTIALS FIRST", "warn")
            self.edit_credentials()
            return
        NCDialog(self, "Target Channel", [
            ("channel", "@HANDLE, CHANNEL URL OR CHANNEL ID", CHANNEL.get("handle") or CHANNEL.get("id", ""), False),
        ], "EXAMPLES: @CHANNELNAME // YOUTUBE.COM/@CHANNELNAME // UC...", self.save_channel)

    def save_channel(self, v):
        target = parse_channel(v["channel"])
        if not target:
            return False
        same_handle = target.get("handle") and target["handle"].lower() == (CHANNEL.get("handle") or "").lower()
        same_id = target.get("id") and target["id"] == CHANNEL.get("id")
        if CHANNEL.get("first_year") and (same_handle or same_id):
            return None
        self.channel_card.update_state(resolving=True)
        self.worker = Worker([], 0, self.out, resolve=target)
        self.worker.start()
        self.start_btn.set_enabled(False)
        self.set_status("running")

    def apply_channel(self, info):
        self.settings["channel"] = info
        set_channel(info)
        self.settings["years"] = [YEARS[0]] if YEARS else []
        save(SETTINGS_FILE, self.settings)
        self.build_rows()
        self.channel_card.update_state(resolving=False)
        self.header.redraw()
        self.footer.redraw()

    def start(self):
        if self.worker and self.worker.is_alive():
            return
        if not all(get_creds()):
            self.write_log("NO API CREDENTIALS // OPEN API SETUP", "err")
            self.edit_credentials()
            return
        if not CHANNEL:
            self.write_log("NO TARGET CHANNEL // CLICK THE CHANNEL CARD", "err")
            self.edit_channel()
            return
        years = self.selected_years()
        if not years:
            self.write_log("NO TARGET SELECTED // CLICK A YEAR ON THE LEFT", "err")
            return
        self.save_settings()
        self.write_log(f"START // {', '.join(map(str, years))}", "sep")
        self.worker = Worker(years, self.limit_val, self.out, fallback=self.fallback)
        self.worker.skip_ahead = self.skip_ahead
        self.run_mode = "running"
        self.pause_info = None
        self.worker.start()
        self.start_btn.set_enabled(False)
        self.check_btn.set_enabled(False)
        self.stop_btn.set_enabled(True)
        self.pause_btn.set_enabled(True)
        self.pause_btn.set_text("❚❚  PAUSE")
        self.set_status("running")

    def run_check(self):
        """Manueller Playlist-Check für die ausgewählten Jahre (Duplikate entfernen, Fortschritt abgleichen)."""
        if self.busy():
            return
        if not all(get_creds()):
            self.edit_credentials()
            return
        years = self.selected_years()
        if not years:
            self.write_log("NO TARGET SELECTED // CLICK A YEAR ON THE LEFT", "err")
            return
        self.write_log(f"PLAYLIST CHECK // {', '.join(map(str, years))}", "sep")
        self.worker = Worker(years, 0, self.out, fallback=self.fallback, check_only=True)
        self.run_mode = "running"
        self.pause_info = None
        self.worker.start()
        self.start_btn.set_enabled(False)
        self.check_btn.set_enabled(False)
        self.stop_btn.set_enabled(True)
        self.set_status("checking")

    def toggle_pause(self):
        if not (self.worker and self.worker.is_alive()):
            return
        if self.worker.pause_event.is_set():
            self.worker.pause_event.clear()
            self.pause_btn.set_text("❚❚  PAUSE")
        else:
            self.worker.pause_event.set()
            self.pause_btn.set_text("▶  RESUME")

    def stop(self):
        if self.worker and self.worker.is_alive():
            self.pause_btn.set_enabled(False)
            self.worker.stop_event.set()
            self.stop_btn.set_enabled(False)
            self.set_status("stopping")

    def on_close(self):
        if self.worker and self.worker.is_alive():
            self.worker.stop_event.set()
            self.worker.join(timeout=5)
        self.destroy()

    # --- Login-Anzeige
    def open_login(self):
        if self.login_url:
            webbrowser.open(self.login_url)

    def copy_code(self):
        self.clipboard_clear()
        self.clipboard_append(self.code)
        self.write_log("CODE COPIED TO CLIPBOARD", "info")

    # --- Nachrichten vom Worker
    def write_log(self, text, level="info"):
        stamp = datetime.datetime.now().strftime("%H:%M:%S")
        self.logbox.config(state="normal")
        self.logbox.insert("end", f"[{stamp}] ", "ts")
        self.logbox.insert("end", f"{text}\n", level)
        if getattr(self, "log_follow", True):
            self.logbox.see("end")
        self.logbox.config(state="disabled")

    def poll(self):
        try:
            while True:
                msg = self.out.get_nowait()
                kind = msg[0]
                if kind == "log":
                    self.write_log(msg[1], msg[2])
                elif kind == "progress":
                    y = int(msg[1])
                    if y in self.rows:
                        self.rows[y].refresh(info=year_info(y))
                elif kind == "checking":
                    if msg[1] in self.rows:
                        self.rows[msg[1]].set_checking(msg[2])
                    self.set_status("checking" if msg[2] else getattr(self, "run_mode", "running"))
                elif kind == "api":
                    self.api_ok = msg[1]
                    self.update_api_button()
                elif kind == "channel":
                    self.apply_channel(msg[1])
                elif kind == "update":
                    self.header.update = (msg[1], msg[2])
                    self.header.redraw()
                    self.write_log(f"UPDATE AVAILABLE // v{msg[1]} // CLICK THE GREEN TAG IN THE HEADER", "ok")
                elif kind == "mode":
                    self.run_mode = msg[1]
                    self.set_status(msg[1])
                elif kind == "pause":
                    self.pause_info = msg[1]
                    for r in self.rows.values():
                        if r.running:
                            r.set_pause(msg[1])
                    if msg[1] is None:
                        self.set_status(self.run_mode)
                    self.chip_label = None
                elif kind == "browser_login":
                    self.set_status("browser_login")
                elif kind == "select":
                    if msg[1] in self.rows:
                        self.rows[msg[1]].refresh(selected=True)
                    if msg[2] is not None and msg[2] in self.rows:
                        self.rows[msg[2]].refresh(selected=False)
                    self.save_settings()
                elif kind == "current":
                    for y, r in self.rows.items():
                        if r.running != (y == msg[1]):
                            r.refresh(running=(y == msg[1]), info=year_info(y))
                elif kind == "code":
                    self.login_url, self.code = msg[1], msg[2]
                    self.login_panel.url, self.login_panel.code = msg[1], msg[2]
                    self.login.grid(row=5, column=0, sticky="ew")
                    self.login_panel.redraw()
                    self.deiconify()
                    self.lift()
                elif kind == "code_done":
                    self.login.grid_remove()
                elif kind == "done":
                    self.start_btn.set_enabled(True)
                    self.check_btn.set_enabled(True)
                    self.stop_btn.set_enabled(False)
                    self.pause_btn.set_enabled(False)
                    self.pause_btn.set_text("❚❚  PAUSE")
                    self.pause_info = None
                    self.set_status("idle" if msg[1] == "resolved" else msg[1])
                    self.update_api_button()
                    if msg[1] != "resolved":
                        self.channel_card.update_state(resolving=False)
                    for y, r in self.rows.items():
                        r.refresh(info=year_info(y))
        except queue.Empty:
            pass
        self.after(200, self.poll)


if __name__ == "__main__":
    App().mainloop()
