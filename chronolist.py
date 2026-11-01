#!/usr/bin/env python3
"""
Chronolist v2.8.0-dev: füllt öffentliche YouTube-Playlists "<Kanal> <Jahr> in Upload-Reihenfolge"
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
import os
import math
import queue
import random
import re
import shutil
import sys
import threading
import time
import zipfile
import tkinter as tk
import tkinter.font as tkfont
import webbrowser
from pathlib import Path
from tkinter import filedialog, ttk

import requests

try:
    import winreg
except ImportError:  # nicht unter Windows
    winreg = None

# ---------------------------------------------------------------- Einstellungen
# Zugangsdaten stehen NICHT im Code, sondern in credentials.json (wird über API SETUP angelegt).
# Kanal wird im Programm eingegeben und in settings.json gespeichert.
# Standard-Vorlagen; im Programm unter PLAYLISTS änderbar (gespeichert in settings.json).
# Platzhalter: {channel} {handle} {year} {count}
# --- Sprache der Oberfläche: "en" oder "de" (in settings.json "language", im Programm unter PROGRAMM)
LANGUAGES = {"en": "ENGLISH", "de": "DEUTSCH"}
LANG = "en"


def L(en, de):
    """Gibt den Text in der eingestellten Sprache zurück."""
    return de if LANG == "de" else en


def detect_language():
    """Standardsprache aus dem System: Deutsch, wenn Windows/das System deutsch ist, sonst Englisch."""
    try:
        import ctypes
        lid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
        return "de" if (lid & 0x3FF) == 0x07 else "en"
    except Exception:  # noqa: BLE001
        pass
    try:
        import locale
        loc = (locale.getlocale()[0] or "") + (os.environ.get("LANG") or "")
        return "de" if loc.lower().startswith(("de", "german")) or "de_" in loc.lower() else "en"
    except Exception:  # noqa: BLE001
        return "en"


def default_playlist_template():
    return L("{channel} {year} in upload order", "{channel} {year} in Upload-Reihenfolge")


def default_description_template():
    return L("All videos by {channel} from {year} in upload order.",
             "Alle Videos von {channel} aus {year} in Upload-Reihenfolge.")


PLAYLIST_TEMPLATE = "{channel} {year} in upload order"
DESCRIPTION_TEMPLATE = "All videos by {channel} from {year} in upload order."
PRIVACY = "public"              # public / unlisted / private
PRIVACY_OPTIONS = ("public", "unlisted", "private")
TEMPLATE_FIELDS = ("channel", "handle", "year", "count")
THUMB_MAX_BYTES = 2 * 1024 * 1024
APP_NAME = "Chronolist"
VERSION = "3.0.0-dev"   # Entwicklungsstand; beim Release ohne "-dev"
BUILD = 105
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
BATCH_SIZE = 30                 # Browser-Fallback: Videos pro Anfrage
BATCH_PAUSE = (4, 12)          # Sekunden zwischen zwei Paketen
COOLDOWN_EVERY = 250            # nach so vielen Videos ein kurzer Cooldown
COOLDOWN_PAUSE = (17, 23)       # Sekunden (~20 s)
LONG_BREAK_EVERY = 1500         # nach so vielen Videos eine lange Pause
LONG_BREAK_PAUSE = (210, 270)   # Sekunden (~4 min)
REMOVE_EXTRAS = True            # nur Videos, die nachweislich Shorts sind, werden aus Playlists entfernt
CARD_H = 62                     # Höhe aller Listen-/Optionskarten
DECK_H = 72                     # Höhe der Kanalkarte und der Steuerkacheln (gleiche Linie links/rechts)
PART_MAX = 5000                 # YouTube: höchstens 5.000 Videos pro Playlist -> danach Teil 2, 3, ...
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
    day = L("TODAY", "HEUTE") if reset_local.date() == today else L("TOMORROW", "MORGEN") \
        if reset_local.date() == today + datetime.timedelta(days=1) \
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
    day = L("TODAY", "HEUTE") if reset_local.date() == today else L("TOMORROW", "MORGEN") \
        if reset_local.date() == today + datetime.timedelta(days=1) \
        else reset_local.strftime("%d.%m.")
    return f"{day} {reset_local.strftime('%H:%M')}"


def archive_file(): return CHAN_DIR / "archive_scan.json"


def archive_cutoff():
    """Ältestes Jahr, das die (von YouTube gekappte) Upload-Liste nur noch teilweise enthält.
    Dieses und ältere Jahre gehen direkt über die Datumssuche. None = Liste vollständig / unbekannt."""
    a = load(archive_file(), {})
    if not a.get("done") or not a.get("found"):
        return None
    if a.get("seen", 0) < CAP_SEEN:
        return None
    return min(int(p[:4]) for p in a["found"].values())


CAP_SEEN = 19900      # YouTube liefert Upload-Listen nur bis ca. 20.000 Einträge


def full_uploads(uploads_id):
    """Immer die vollständige Upload-Liste 'UU…' (inkl. Livestreams; Shorts filtern wir selbst)."""
    if uploads_id and uploads_id.startswith("UU") and uploads_id[2:4] in ("LF", "SH", "LV") and len(uploads_id) > 24:
        return "UU" + uploads_id[4:]
    return uploads_id


def shorts_list(uploads_id):
    """Upload-Liste 'UU…' -> Liste nur mit den Shorts des Kanals 'UUSH…'."""
    up = full_uploads(uploads_id)
    return "UUSH" + up[2:] if up and up.startswith("UU") else None


def lf_file(): return CHAN_DIR / "videos_only.json"
def shorts_file(): return CHAN_DIR / "shorts.json"


def known_shorts():
    d = load(shorts_file(), {})
    return set(d.get("ids") or []) if d.get("done") else None
def titles_file(): return CHAN_DIR / "titles.json"


_titles_lock = threading.Lock()
HIDDEN_TITLES = ("Private video", "Deleted video", "Privates Video", "Gelöschtes Video")


def remember_titles(new):
    """Videotitel zu titles.json hinzufügen (aus API/Kanalseite, ohne Zusatzkosten). Gibt die Anzahl neuer/geänderter zurück."""
    new = {k: v for k, v in (new or {}).items() if v and v not in HIDDEN_TITLES}
    if not new:
        return 0
    with _titles_lock:
        cur = load(titles_file(), {})
        changed = {k: v for k, v in new.items() if cur.get(k) != v}
        if changed:
            cur.update(changed)
            save(titles_file(), cur)
        return len(changed)


def item_titles(items):
    """{videoId: Titel} aus playlistItems-Antworten mit part=snippet."""
    out = {}
    for it in items or []:
        sn = it.get("snippet") or {}
        vid = (sn.get("resourceId") or {}).get("videoId") or (it.get("contentDetails") or {}).get("videoId")
        if vid and sn.get("title"):
            out[vid] = sn["title"]
    return out


def merge_keep(base_ids, old_items):
    """base_ids (Reihenfolge der Kanalseite) plus alle Einträge aus old_items, die dort fehlen,
    jeweils hinter ihrem Vorgänger aus der alten Liste eingefügt."""
    merged = list(base_ids)
    have = set(merged)
    prev = None
    for v in old_items:
        vid = v["id"]
        if vid in have:
            prev = vid
            continue
        pos = merged.index(prev) + 1 if prev in merged else 0
        merged.insert(pos, vid)
        have.add(vid)
        prev = vid
    return merged


def known_dates():
    """Veröffentlichungsdaten aus allen gespeicherten API-Daten (Archiv, Suche, Nur-Videos-Liste)."""
    d = {}
    d.update(load(archive_file(), {}).get("found") or {})
    for f in (sorted(CHAN_DIR.glob("collect_*.json")) if CHAN_DIR.exists() else []):
        d.update(load(f, {}).get("found") or {})
    return d


def insert_by_date(videos, extra, dates):
    """Fehlende Videos (extra: [(id, datum)]) nach Datum zwischen die vorhandenen einsortieren."""
    out = list(videos)
    for vid, pub in sorted(extra, key=lambda t: t[1]):
        pos = len(out)
        for i, v in enumerate(out):
            dv = v.get("published") or dates.get(v["id"])
            if dv and dv > pub:
                pos = i
                break
        out.insert(pos, {"id": vid, "published": pub})
    return out


def removed_by_year():
    """Videos, die gefunden, aber nicht in der Jahresliste sind (Shorts). Ergibt sich aus den
    gespeicherten Scan-Daten im Vergleich zur heutigen Liste."""
    cand = {}
    for vid, pub in (load(archive_file(), {}).get("found") or {}).items():
        cand.setdefault(int(pub[:4]), {})[vid] = pub
    for f in (sorted(CHAN_DIR.glob("collect_*.json")) if CHAN_DIR.exists() else []):
        for vid, pub in (load(f, {}).get("found") or {}).items():
            cand.setdefault(int(pub[:4]), {})[vid] = pub
    out = {}
    for y, found in cand.items():
        vids = load(videos_file(y), None)
        if vids is None:
            continue
        have = {v["id"] for v in vids}
        gone = sorted(((p, v) for v, p in found.items() if v not in have))
        if gone:
            out[y] = [{"id": v, "published": p} for p, v in gone]
    return out


# ------------------------------------------------------------------ Stichwortlisten
# Eine Liste ist wie ein Jahr eine Playlist in Upload-Reihenfolge, nur dass die Videos nach ihrem
# Titel gewählt werden. Gespeichert pro Kanal in lists.json; der Schlüssel "k_<id>" steht dort,
# wo bei Jahren die Jahreszahl steht (videos_k_<id>.json, state_k_<id>.json).
def lists_file(): return CHAN_DIR / "lists.json"
def notitle_file(): return CHAN_DIR / "notitle.json"


def load_lists():
    d = load(lists_file(), [])
    return d if isinstance(d, list) else []


def save_lists(lists):
    save(lists_file(), lists)


def list_key(lst): return "k_" + lst["id"]
def is_list_key(y): return isinstance(y, str)


def find_list(key):
    for lst in load_lists():
        if list_key(lst) == key:
            return lst
    return None


def list_name(key):
    lst = find_list(key) if isinstance(key, str) else None
    return (lst or {}).get("name") or str(key)


def year_label(y):
    """Anzeigename einer Karte: Jahreszahl oder Listenname."""
    return list_name(y) if is_list_key(y) else str(y)


def store_thumbnail(key, src):
    """Bild für eine Playlist (Jahr oder Liste) vormerken: Kopie im Kanal-Ordner, Upload beim nächsten START."""
    thumbs = load(thumbs_file(), {})
    old = thumbs.pop(str(key), None)
    if old and old.get("file") and (thumbs_dir() / old["file"]).exists():
        try:
            (thumbs_dir() / old["file"]).unlink()
        except OSError:
            pass
    if src:
        thumbs_dir().mkdir(parents=True, exist_ok=True)
        name = f"{key}{Path(src).suffix.lower()}"
        shutil.copyfile(src, thumbs_dir() / name)
        thumbs[str(key)] = {"file": name, "status": "queued", "error": ""}
    save(thumbs_file(), thumbs)


def new_list_id():
    have = {lst["id"] for lst in load_lists()}
    while True:
        i = "%06x" % random.randrange(16 ** 6)
        if i not in have:
            return i


def norm_text(s):
    return " ".join(str(s or "").casefold().split())


def parse_terms(text):
    """'a, b' oder Zeilen -> ['a', 'b'] (klein geschrieben, ohne Leereinträge)."""
    if isinstance(text, (list, tuple)):
        text = "\n".join(text)
    out = []
    for part in re.split(r"[,\n;]", str(text or "")):
        t = norm_text(part)
        if t and t not in out:
            out.append(t)
    return out


def title_matches(lst, title):
    """Passt ein Titel zur Liste? any = mindestens eins, all = alle, none = keins davon."""
    t = norm_text(title)
    any_t, all_t, none_t = (lst.get("any") or []), (lst.get("all") or []), (lst.get("none") or [])
    if not (any_t or all_t):
        return False                      # ohne Suchbegriff keine Treffer
    if any_t and not any(x in t for x in any_t):
        return False
    if all_t and not all(x in t for x in all_t):
        return False
    return not any(x in t for x in none_t)


def year_files():
    """[(Jahr, Pfad)] aller Jahreslisten, älteste zuerst."""
    out = []
    for f in (CHAN_DIR.glob("videos_*.json") if CHAN_DIR.exists() else []):
        m_ = re.fullmatch(r"videos_(\d{4})\.json", f.name)
        if m_:
            out.append((int(m_.group(1)), f))
    return sorted(out)


def all_year_videos():
    """Alle Videos aller Jahre in Upload-Reihenfolge: [{'id','published','year'}]."""
    out = []
    for y, f in year_files():
        for v in load(f, []) or []:
            out.append({"id": v["id"], "published": v.get("published"), "year": y})
    return out


def in_years(lst, year):
    """Liegt das Jahr im eingestellten Zeitraum der Liste (leer = unbegrenzt)?"""
    a, b = lst.get("from"), lst.get("to")
    return (not a or year >= a) and (not b or year <= b)


def list_videos(lst, titles=None, pool=None):
    """Treffer einer Liste in Upload-Reihenfolge. Einzelne Videos lassen sich fest hinzufügen/ausschließen."""
    titles = titles if titles is not None else load(titles_file(), {})
    add, skip = set(lst.get("add") or []), set(lst.get("skip") or [])
    out = []
    for v in (pool if pool is not None else all_year_videos()):
        vid = v["id"]
        if vid in skip:
            continue
        if vid in add or (in_years(lst, v["year"]) and title_matches(lst, titles.get(vid, ""))):
            out.append({"id": vid, "published": v.get("published")})
    return out


def missing_titles(pool=None):
    titles = load(titles_file(), {})
    nt = set(load(notitle_file(), []))
    return [v["id"] for v in (pool if pool is not None else all_year_videos())
            if v["id"] not in titles and v["id"] not in nt]


def video_title(vid):
    """Titel über oEmbed (öffentlich, ohne Kontingent). None, wenn nicht abrufbar (privat/gelöscht)."""
    try:
        r = requests.get("https://www.youtube.com/oembed",
                         params={"url": f"https://www.youtube.com/watch?v={vid}", "format": "json"},
                         timeout=10, headers={"User-Agent": f"{APP_NAME}/{VERSION}"})
        if r.ok:
            return r.json().get("title")
    except (requests.RequestException, ValueError):
        pass
    return None
def videos_file(y): return CHAN_DIR / f"videos_{y}.json"
def collect_file(y): return CHAN_DIR / f"collect_{y}.json"
def state_file(y): return CHAN_DIR / f"state_{y}.json"
def template_values(y, count=None):
    if is_list_key(y):
        if count is None:
            vids = load(videos_file(y), None)
            count = len(vids) if vids is not None else "?"
        return {"channel": channel_name(), "handle": CHANNEL.get("handle", ""), "year": list_name(y), "count": count}
    if count is None:
        vids = load(videos_file(y), None)
        count = len(vids) if vids is not None else "?"
    return {"channel": channel_name(), "handle": CHANNEL.get("handle", ""), "year": y, "count": count}


def fill_template(t, y, count=None):
    try:
        return t.format(**template_values(y, count))
    except (KeyError, IndexError, ValueError, AttributeError):
        return t


def playlist_title(y): return fill_template(PLAYLIST_TEMPLATE, y)


def part_title(y, n, total_parts):
    """Titel von Teil n (ab 1). Bei nur einem Teil ohne Zusatz."""
    base = playlist_title(y)
    return base if total_parts <= 1 else base + L(f" (Part {n})", f" (Teil {n})")


def part_ids(state):
    """Playlist-IDs aller Teile eines Jahres (ältere Stände kennen nur 'playlist_id')."""
    if state.get("parts"):
        return list(state["parts"])
    return [state["playlist_id"]] if state.get("playlist_id") else []


def growing(y):
    """Kann in diesem Jahr noch etwas dazukommen? Laufendes Jahr, im Januar auch das Vorjahr."""
    if is_list_key(y):
        return True                       # Listen werden bei jedem Lauf neu abgeglichen
    t = datetime.date.today()
    return y >= t.year or (y == t.year - 1 and t.month == 1)
def playlist_description(y): return fill_template(DESCRIPTION_TEMPLATE, y)
def thumbs_dir(): return CHAN_DIR / "thumbs"
def thumbs_file(): return CHAN_DIR / "thumbs.json"


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


FALLBACK_ON = False   # Spiegel des FALLBACK-Schalters, damit die Jahreskarten wissen, was ohne API geht


def year_blocked(info):
    """Reset-Zeit, wenn dieses Jahr erst nach dem API-Reset weitergehen kann, sonst None.
    Suchen geht nur über die API; Einfügen geht mit FALLBACK auch ohne."""
    code = info["code"]
    if code == "done":
        return None
    if FALLBACK_ON:
        return None          # mit FALLBACK geht Suchen (Kanalseite) und Füllen ohne API
    if code in ("none", "scanning"):
        return quota_blocked("units") or quota_blocked("search")
    return quota_blocked("units")


def status_role(code, running=False):
    """Farbe nach Zustand: Cyan läuft gerade, Gelb angefangen und wartet, Grün fertig,
    Blaugrau noch nicht angefangen. (Rot für 'braucht API' kommt als Text dazu.)"""
    if running:
        return CYAN
    return GREEN if code == "done" else YELLOW if code == "progress" else NEUTRAL


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
        t.format(channel="X", handle="@x", year=2000, count=1)
        return True
    except (KeyError, IndexError, ValueError, AttributeError):
        return False


def apply_templates(settings):
    """Playlist-Titel und -Beschreibung aus settings.json übernehmen (Platzhalter: {channel}, {year})."""
    global PLAYLIST_TEMPLATE, DESCRIPTION_TEMPLATE, PRIVACY
    pt = settings.get("playlist_template") or default_playlist_template()
    dt = settings.get("description_template", default_description_template())   # "" = keine Beschreibung
    PLAYLIST_TEMPLATE = pt if isinstance(pt, str) and _template_ok(pt) else default_playlist_template()
    DESCRIPTION_TEMPLATE = dt if isinstance(dt, str) and _template_ok(dt) else default_description_template()
    pv = settings.get("privacy", "public")
    PRIVACY = pv if pv in PRIVACY_OPTIONS else "public"


def version_tuple(v):
    """'2.8.0' -> (2, 8, 0, 1), '2.8.0-dev' -> (2, 8, 0, 0): ein Entwicklungsstand gilt als älter
    als das fertige Release mit derselben Nummer, aber neuer als alle früheren Releases."""
    nums = [int(x) for x in re.findall(r"\d+", (v or "").split("-")[0])[:3]]
    nums += [0] * (3 - len(nums))
    return tuple(nums) + (0 if "-" in (v or "") else 1,)


def check_for_update(out):
    """Fragt einmal die neueste GitHub-Release ab. Sendet ('update', version, url, zip_url), wenn sie neuer ist.
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
            zip_url = next((a.get("browser_download_url") for a in j.get("assets") or []
                            if (a.get("name") or "").lower().endswith("-windows.zip")), None)
            out.put(("update", tag, j.get("html_url") or f"{REPO_URL}/releases/latest", zip_url, j.get("body") or ""))
    except (requests.RequestException, ValueError):
        pass


# --------------------------------------------------------------- Selbst-Update
# Nur für die exe: ZIP aus der Release laden, neue exe daneben ablegen, laufende exe umbenennen
# (Windows erlaubt das, überschreiben nicht), neue exe an ihren Platz, neu starten.
def update_paths():
    exe = Path(sys.executable).resolve()
    return exe, exe.with_name(exe.stem + ".new.exe"), exe.with_name(exe.stem + ".old.exe"), HERE / "update.zip"


def can_self_update():
    return getattr(sys, "frozen", False) and os.name == "nt"


def download_update(zip_url, out):
    """Läuft im Thread. Meldet ('upd_progress', prozent) und am Ende ('upd_ready', None) oder ('upd_failed', text)."""
    exe, new, old, tmp = update_paths()
    try:
        with requests.get(zip_url, stream=True, timeout=30,
                          headers={"User-Agent": f"{APP_NAME}/{VERSION}"}) as r:
            r.raise_for_status()
            total = int(r.headers.get("Content-Length") or 0)
            done, last = 0, -1
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(256 * 1024):
                    f.write(chunk)
                    done += len(chunk)
                    if total:
                        pct = done * 100 // total
                        if pct // 10 != last // 10:
                            out.put(("upd_progress", pct))
                            last = pct
        with zipfile.ZipFile(tmp) as z:
            name = next((n for n in z.namelist() if n.lower().endswith(".exe")), None)
            if not name:
                raise ValueError("no .exe in the zip")
            with z.open(name) as src, open(new, "wb") as dst:
                shutil.copyfileobj(src, dst)
        with open(new, "rb") as f:
            if f.read(2) != b"MZ":
                raise ValueError("downloaded file is not a Windows program")
        out.put(("upd_ready", None))
    except (requests.RequestException, OSError, ValueError, zipfile.BadZipFile) as e:
        try:
            new.unlink()
        except OSError:
            pass
        out.put(("upd_failed", str(e)[:200]))
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def install_update():
    """Tauscht die exe aus. Gibt bei Fehler den Text zurück, sonst None. Bei Fehler bleibt die alte exe."""
    exe, new, old, _ = update_paths()
    try:
        if old.exists():
            old.unlink()
    except OSError:
        pass
    try:
        exe.rename(old)
    except OSError as e:
        return str(e)
    try:
        new.rename(exe)
    except OSError as e:
        try:
            old.rename(exe)
        except OSError:
            pass
        return str(e)
    return None


def cleanup_update(tries=10):
    """Alte exe und Reste eines Updates entfernen. Die alte exe kann noch kurz gesperrt sein."""
    if not can_self_update():
        return True
    exe, new, old, tmp = update_paths()
    ok = True
    for f in (old, new, tmp):
        try:
            if f.exists():
                f.unlink()
        except OSError:
            ok = False
    return ok


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
        self.status = status
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


def first_token(node):
    """Erstes continuationCommand-Token irgendwo unterhalb von node."""
    if isinstance(node, dict):
        cc = node.get("continuationCommand")
        if isinstance(cc, dict) and cc.get("token"):
            return cc["token"]
        for v in node.values():
            t = first_token(v)
            if t:
                return t
    elif isinstance(node, list):
        for v in node:
            t = first_token(v)
            if t:
                return t
    return None


def text_of(node):
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        if "simpleText" in node:
            return node["simpleText"]
        if "runs" in node:
            return "".join(r.get("text", "") for r in node["runs"])
        if "content" in node and isinstance(node["content"], str):
            return node["content"]
    return ""


def find_chips(node, out):
    """Sortier-Chips der Videoseite (Neueste / Beliebt / Älteste) als [(text, token)]."""
    if isinstance(node, dict):
        for key in ("chipCloudChipRenderer", "chipViewModel"):
            c = node.get(key)
            if isinstance(c, dict):
                tok = first_token(c)
                if tok:
                    out.append((text_of(c.get("text")).strip().lower(), tok))
                return
        for v in node.values():
            find_chips(v, out)
    elif isinstance(node, list):
        for v in node:
            find_chips(v, out)


def find_channel_videos(node, ids, tokens, titles=None):
    """Video-IDs (in Seitenreihenfolge) und das Fortsetzungs-Token einer Kanal-Videoseite.
    Mit `titles` (dict) werden zusätzlich die Titel eingesammelt."""
    if isinstance(node, dict):
        for key in ("videoRenderer", "gridVideoRenderer"):
            r = node.get(key)
            if isinstance(r, dict) and r.get("videoId"):
                ids.append(r["videoId"])
                if titles is not None:
                    t = text_of(r.get("title")).strip()
                    if t:
                        titles[r["videoId"]] = t
                return
        lv = node.get("lockupViewModel")
        if isinstance(lv, dict) and lv.get("contentId") and "VIDEO" in str(lv.get("contentType", "VIDEO")):
            ids.append(lv["contentId"])
            if titles is not None:
                m = (lv.get("metadata") or {}).get("lockupMetadataViewModel") or {}
                t = text_of(m.get("title")).strip()
                if t:
                    titles[lv["contentId"]] = t
            return
        ci = node.get("continuationItemRenderer")
        if isinstance(ci, dict):
            t = first_token(ci)
            if t:
                tokens.append(t)
            return
        for v in node.values():
            find_channel_videos(v, ids, tokens, titles)
    elif isinstance(node, list):
        for v in node:
            find_channel_videos(v, ids, tokens, titles)


OLDEST_WORDS = ("oldest", "älteste", "ältest", "plus anciennes", "más antiguos", "più vecchi", "oudste")


def web_index_file(): return CHAN_DIR / "web_index.json"


def fmt_day(iso):
    """'2014-03-12...' -> '12.03.2014' (DE) bzw. '12 MAR 2014' (EN)."""
    try:
        d = datetime.date.fromisoformat((iso or "")[:10])
    except ValueError:
        return "?"
    months = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
    return L(f"{d.day} {months[d.month - 1]} {d.year}", d.strftime("%d.%m.%Y"))


def known_channel_id():
    """Kanal-ID aus den Einstellungen oder aus schon gespeicherten Scan-Daten (ohne Netz)."""
    if CHANNEL.get("id"):
        return CHANNEL["id"]
    up = load(archive_file(), {}).get("uploads", "")
    if up.startswith("UU"):
        return "UC" + up[2:]
    for f in sorted(CHAN_DIR.glob("collect_*.json")) if CHAN_DIR.exists() else []:
        cid = load(f, {}).get("channel_id")
        if cid:
            return cid
    return None


def plan_moves(order, items):
    """Welche Playlist-Einträge müssen wohin, damit sie der Reihenfolge `order` folgen?
    Behält die längste schon richtig sortierte Folge und schiebt nur den Rest.
    Liefert [(setVideoId, setVideoId des Nachfolgers)] in ausführbarer Reihenfolge."""
    pos = {v: i for i, v in enumerate(order)}
    seq = []          # (Sollposition, vid, set) in aktueller Playlist-Reihenfolge, nur erste Vorkommen
    seen = set()
    for it in items:
        if it["vid"] in pos and it["vid"] not in seen:
            seen.add(it["vid"])
            seq.append((pos[it["vid"]], it["vid"], it["set"]))
    if not seq:
        return []
    # Das letzte Soll-Video bleibt fest (es braucht keinen Nachfolger). Davor die längste schon
    # aufsteigende Teilfolge behalten, alles andere wird verschoben.
    z = max(range(len(seq)), key=lambda i: seq[i][0])
    import bisect
    tails, tails_i, prev = [], [], [-1] * len(seq)
    for i, (p, _, _) in enumerate(seq[:z]):
        k = bisect.bisect_left(tails, p)
        if k == len(tails):
            tails.append(p); tails_i.append(i)
        else:
            tails[k] = p; tails_i[k] = i
        prev[i] = tails_i[k - 1] if k else -1
    keep = {seq[z][1]}
    i = tails_i[-1] if tails_i else -1
    while i != -1:
        keep.add(seq[i][1])
        i = prev[i]
    set_of = {v: st for _, v, st in seq}
    present = [v for v in order if v in set_of]
    moves = []
    for j in range(len(present) - 1, -1, -1):
        v = present[j]
        if v in keep:
            continue
        moves.append((set_of[v], set_of[present[j + 1]]))
    return moves


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
            raise BrowserFailed(L("PLAYWRIGHT NOT INSTALLED // RUN: pip install playwright", "PLAYWRIGHT FEHLT // AUSFÜHREN: pip install playwright"))
        self.w.log(L("BROWSER UPLINK // LAUNCHING EDGE ...", "BROWSER-MODUS // EDGE WIRD GESTARTET ..."))
        self.w.act(L("STARTING EDGE ...", "STARTE EDGE ..."))
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
                raise BrowserFailed(L(f"BROWSER LAUNCH FAILED // {e}", f"BROWSER-START FEHLGESCHLAGEN // {e}"))
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        self.open_youtube()
        self.wait_login()
        self.w.act()

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
            self.w.log(L("BROWSER UPLINK // SIGNED IN", "BROWSER-MODUS // ANGEMELDET"), "ok")
            return
        self.w.log(L("SIGN IN TO YOUTUBE IN THE EDGE WINDOW // WAITING (MAX 15 MIN)", "IM EDGE-FENSTER BEI YOUTUBE ANMELDEN // WARTE (MAX. 15 MIN)"), "warn")
        self.w.emit("browser_login")
        end = time.time() + 15 * 60
        while time.time() < end:
            self.w.sleep(4)
            if "youtube.com" in (self.page.url or "") and self.logged_in():
                self.w.log(L("BROWSER UPLINK // SIGNED IN", "BROWSER-MODUS // ANGEMELDET"), "ok")
                self.w.emit("mode", "browser")
                return
        raise BrowserFailed(L("NO YOUTUBE SIGN-IN WITHIN 15 MIN", "KEINE YOUTUBE-ANMELDUNG INNERHALB VON 15 MIN"))

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
                    raise BrowserFailed(L(f"BROWSER ERROR // {str(e)[:200]}", f"BROWSER-FEHLER // {str(e)[:200]}"))
                self.w.sleep(10)
                self.open_youtube()
                continue
            if res.get("fatal") == "NOT_LOGGED_IN":
                self.wait_login()
                continue
            if res.get("fatal"):
                if attempt == 2:
                    raise BrowserFailed(L(f"BROWSER UPLINK // {res['fatal']}", f"BROWSER-MODUS // {res['fatal']}"))
                self.open_youtube()
                continue
            if res.get("status") == "STATUS_SUCCEEDED":
                self.count += 1
                return True
            http, err = res.get("http"), res.get("err") or res.get("status")
            if http in (401, 403):
                raise BrowserFailed(L(f"YOUTUBE REFUSED ACCESS ({http}) // CHECK SIGNED-IN CHANNEL OWNS THE PLAYLIST", f"YOUTUBE VERWEIGERT DEN ZUGRIFF ({http}) // GEHÖRT DIE PLAYLIST DEM ANGEMELDETEN KANAL?"))
            if http == 429:
                self.w.log(L("YOUTUBE RATE LIMIT // COOLING DOWN 10 MIN", "YOUTUBE BREMST // 10 MIN PAUSE"), "warn")
                self.w.sleep(600, "RATE LIMIT")
                continue
            if http == 400 or res.get("status") == "STATUS_FAILED":
                self.w.log(L(f"SKIPPED {video_id} // {err}", f"ÜBERSPRUNGEN {video_id} // {err}"), "warn")
                return False
            if attempt == 2:
                raise BrowserFailed(L(f"BROWSER UPLINK // HTTP {http} {err}", f"BROWSER-MODUS // HTTP {http} {err}"))
            self.w.sleep(15)
        raise BrowserFailed(L("BROWSER UPLINK // GAVE UP", "BROWSER-MODUS // AUFGEGEBEN"))

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
                    raise BrowserFailed(L(f"BROWSER ERROR // {str(e)[:200]}", f"BROWSER-FEHLER // {str(e)[:200]}"))
                self.open_youtube()
                continue
            if res.get("fatal") == "NOT_LOGGED_IN":
                self.wait_login()
                continue
            if res.get("fatal"):
                if attempt == 2:
                    raise BrowserFailed(L(f"BROWSER UPLINK // {res['fatal']}", f"BROWSER-MODUS // {res['fatal']}"))
                self.open_youtube()
                continue
            if res.get("http") == 200 and res.get("json") is not None:
                return res["json"]
            if attempt == 2:
                raise BrowserFailed(L(f"BROWSER UPLINK // HTTP {res.get('http')} ON {path}", f"BROWSER-MODUS // HTTP {res.get('http')} BEI {path}"))
            time.sleep(5)
        raise BrowserFailed(L("BROWSER UPLINK // GAVE UP", "BROWSER-MODUS // AUFGEGEBEN"))

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
                    raise BrowserFailed(L(f"BROWSER ERROR // {str(e)[:200]}", f"BROWSER-FEHLER // {str(e)[:200]}"))
                self.w.sleep(10)
                self.open_youtube()
                continue
            if res.get("fatal") == "NOT_LOGGED_IN":
                self.wait_login()
                continue
            if res.get("fatal"):
                if attempt == 2:
                    raise BrowserFailed(L(f"BROWSER UPLINK // {res['fatal']}", f"BROWSER-MODUS // {res['fatal']}"))
                self.open_youtube()
                continue
            http, j = res.get("http"), res.get("json") or {}
            if http == 200 and j.get("status") == "STATUS_SUCCEEDED":
                self.count += len(video_ids)
                return True
            if http in (401, 403):
                raise BrowserFailed(L(f"YOUTUBE REFUSED ACCESS ({http}) // CHECK SIGNED-IN CHANNEL OWNS THE PLAYLIST", f"YOUTUBE VERWEIGERT DEN ZUGRIFF ({http}) // GEHÖRT DIE PLAYLIST DEM ANGEMELDETEN KANAL?"))
            if http == 429:
                self.w.log(L("YOUTUBE RATE LIMIT // COOLING DOWN 10 MIN", "YOUTUBE BREMST // 10 MIN PAUSE"), "warn")
                self.w.sleep(600, "RATE LIMIT")
                continue
            if http == 400 or j.get("status") == "STATUS_FAILED":
                return False
            if attempt == 2:
                raise BrowserFailed(L(f"BROWSER UPLINK // HTTP {http}", f"BROWSER-MODUS // HTTP {http}"))
            self.w.sleep(15)
        raise BrowserFailed(L("BROWSER UPLINK // GAVE UP", "BROWSER-MODUS // AUFGEGEBEN"))

    def create_playlist(self, title, description):
        j = self.yt_call("playlist/create", {"title": title, "privacyStatus": PRIVACY.upper()})
        pid = j.get("playlistId")
        if not pid:
            raise BrowserFailed(L("PLAYLIST CREATE FAILED", "PLAYLIST KONNTE NICHT ANGELEGT WERDEN"))
        try:
            self.yt_call("browse/edit_playlist", {"playlistId": pid, "actions": [
                {"action": "ACTION_SET_PLAYLIST_DESCRIPTION", "playlistDescription": description}]})
        except BrowserFailed:
            self.w.log(L("PLAYLIST DESCRIPTION NOT SET // ADD IT MANUALLY IF NEEDED", "BESCHREIBUNG NICHT GESETZT // BEI BEDARF VON HAND EINTRAGEN"), "warn")
        return pid

    def list_items(self, playlist_id, progress=None):
        items, tokens = [], []
        walk_json(self.yt_call("browse", {"browseId": "VL" + playlist_id}), items, tokens)
        if progress:
            progress(len(items))
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
            if progress:
                progress(len(items))
            time.sleep(0.3)
        return items

    def resolve_channel(self, handle):
        """@handle -> Kanal-ID über die Webseite."""
        if not handle:
            return None
        h = handle if handle.startswith("@") else "@" + handle
        try:
            j = self.yt_call("navigation/resolve_url", {"url": "https://www.youtube.com/" + h})
        except BrowserFailed:
            return None
        be = (j.get("endpoint") or {}).get("browseEndpoint") or {}
        cid = be.get("browseId")
        return cid if cid and cid.startswith("UC") else None

    def rename_playlist(self, playlist_id, title):
        j = self.yt_call("browse/edit_playlist", {"playlistId": playlist_id, "actions": [
            {"action": "ACTION_SET_PLAYLIST_NAME", "playlistName": title}]})
        return j.get("status") == "STATUS_SUCCEEDED"

    def latest_videos(self, channel_id, known, limit_pages=40):
        """Neueste Videos der Kanalseite (neueste zuerst), bis ein bekanntes Video kommt."""
        ids, tokens, titles = [], [], {}
        find_channel_videos(self.yt_call("browse", {"browseId": channel_id, "params": "EgZ2aWRlb3PyBgQKAjoA"}),
                            ids, tokens, titles)
        out = []
        for _ in range(limit_pages):
            for v in ids:
                if v in known:
                    remember_titles(titles)
                    return out
                if v not in out:
                    out.append(v)
            if not tokens:
                remember_titles(titles)
                return out
            ids, new_tokens = [], []
            find_channel_videos(self.yt_call("browse", {"continuation": tokens[-1]}), ids, new_tokens, titles)
            tokens = new_tokens
            time.sleep(random.uniform(0.8, 1.6))
        remember_titles(titles)
        return out

    def oldest_token(self, channel_id):
        """Token für die Videoseite des Kanals, sortiert nach 'Älteste'."""
        j = self.yt_call("browse", {"browseId": channel_id, "params": "EgZ2aWRlb3PyBgQKAjoA"})
        chips = []
        find_chips(j, chips)
        for text, tok in chips:
            if any(w in text for w in OLDEST_WORDS):
                return tok
        if len(chips) >= 3:
            return chips[2][1]       # Reihenfolge: Neueste, Beliebt, Älteste
        raise BrowserFailed(L("CHANNEL PAGE // NO 'OLDEST' SORTING FOUND", "KANALSEITE // KEINE SORTIERUNG 'ÄLTESTE' GEFUNDEN"))

    def video_page(self, token):
        ids, tokens, titles = [], [], {}
        find_channel_videos(self.yt_call("browse", {"continuation": token}), ids, tokens, titles)
        remember_titles(titles)
        return ids, (tokens[-1] if tokens else None)

    def video_date(self, vid):
        """Veröffentlichungsdatum als ISO-String in UTC, oder None."""
        try:
            j = self.yt_call("player", {"videoId": vid})
        except BrowserFailed:
            return None
        mf = (j.get("microformat") or {}).get("playerMicroformatRenderer") or {}
        raw = mf.get("publishDate") or mf.get("uploadDate")
        if not raw:
            return None
        try:
            d = datetime.datetime.fromisoformat(raw)
            if d.tzinfo is None:
                return d.strftime("%Y-%m-%dT%H:%M:%SZ")
            return d.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            return raw[:10] + "T00:00:00Z" if re.match(r"\d{4}-\d{2}-\d{2}", raw) else None

    def move_before(self, playlist_id, moves, progress=None):
        """moves: [(setVideoId, setVideoId des Nachfolgers)], in Paketen."""
        for i in range(0, len(moves), 20):
            self.w.check()
            acts = [{"action": "ACTION_MOVE_VIDEO_BEFORE", "setVideoId": a, "movedSetVideoIdSuccessor": b}
                    for a, b in moves[i:i + 20]]
            j = self.yt_call("browse/edit_playlist", {"playlistId": playlist_id, "actions": acts})
            if j.get("status") != "STATUS_SUCCEEDED":
                raise BrowserFailed(L(f"REORDER FAILED // {j.get('status')}", f"SORTIEREN FEHLGESCHLAGEN // {j.get('status')}"))
            if progress:
                progress(min(i + 20, len(moves)))
            self.w.sleep(random.uniform(2, 4))

    def remove_many(self, playlist_id, items):
        acts = [{"action": "ACTION_REMOVE_VIDEO", "setVideoId": it["set"], "removedVideoId": it["vid"]} for it in items]
        j = self.yt_call("browse/edit_playlist", {"playlistId": playlist_id, "actions": acts})
        if j.get("status") != "STATUS_SUCCEEDED":
            raise BrowserFailed(L(f"REMOVE FAILED // {j.get('status')}", f"ENTFERNEN FEHLGESCHLAGEN // {j.get('status')}"))

    def contains(self, playlist_id, video_id):
        """Steckt das Video in der Playlist? Über den 'Speichern in'-Dialog der Webseite.
        None = konnte nicht festgestellt werden."""
        try:
            j = self.yt_call("playlist/get_add_to_playlist", {"videoIds": [video_id], "excludeWatchLater": True})
        except BrowserFailed:
            return None
        found = []

        def walk(n):
            if isinstance(n, dict):
                if n.get("playlistId") == playlist_id and "containsSelectedVideos" in n:
                    found.append(n["containsSelectedVideos"])
                for v in n.values():
                    walk(v)
            elif isinstance(n, list):
                for v in n:
                    walk(v)
        walk(j)
        if not found:
            return None
        return found[0] == "ALL"

    def remove(self, playlist_id, item):
        j = self.yt_call("browse/edit_playlist", {"playlistId": playlist_id, "actions": [
            {"action": "ACTION_REMOVE_VIDEO", "setVideoId": item["set"], "removedVideoId": item["vid"]}]})
        if j.get("status") != "STATUS_SUCCEEDED":
            raise BrowserFailed(L(f"REMOVE FAILED // {j.get('status')}", f"ENTFERNEN FEHLGESCHLAGEN // {j.get('status')}"))
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
        if msg and msg[0] == "current":
            self.current_year = msg[1]
        self.out.put(msg)

    def act(self, text=None, year=None):
        """Was gerade passiert, für die Statuszeile der Jahreskarte, um die es geht
        (Standard: das laufende Jahr). None = nichts Besonderes."""
        self.emit("activity", year or getattr(self, "current_year", None), text)

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
        self.log(L("PAUSED BY USER", "VON HAND PAUSIERT"), "warn")
        while self.pause_event.is_set():
            if self.stop_event.wait(0.25):
                raise Stopped()
        self.emit("pause", None)
        self.log(L(f"RESUMED // PAUSED FOR {fmt_secs(time.time() - t0)}", f"GEHT WEITER // PAUSE WAR {fmt_secs(time.time() - t0)}"), "ok")
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
                raise RuntimeError(L("NO API CREDENTIALS // OPEN API SETUP", "KEINE API-ZUGANGSDATEN // API-KNOPF OBEN ÖFFNEN"))
            self.tok = load(TOKEN_FILE, None) or self.device_login()
            if self.resolve_target is not None:
                reason = "resolved"
                self.emit("channel", self.resolve_channel(self.resolve_target))
                return
            if self.check_only:
                self.manual_check()
                return
            for kind, label in (("units", L("API QUOTA", "API-KONTINGENT")), ("search", L("SEARCH LIMIT", "SUCHLIMIT"))):
                r = quota_blocked(kind)
                if r:
                    self.log(L(f"{label} ALREADY USED UP TODAY // SKIPPING IT UNTIL RESET {fmt_reset(r)}", f"{label} HEUTE SCHON AUFGEBRAUCHT // WIRD BIS ZUM RESET {fmt_reset(r)} ÜBERSPRUNGEN"), "warn")
            try:
                self.apply_thumbnails()
            except Exception as e:  # noqa: BLE001
                self.log(L(f"THUMBNAILS SKIPPED // {str(e)[:120]}", f"THUMBNAILS ÜBERSPRUNGEN // {str(e)[:120]}"), "warn")
            if self.fallback:
                self.scan_archive()
            queue_ = [y for y in self.years if not is_list_key(y)]
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
                self.log(L(f"// TARGET {y}", f"// JAHR {y}"), "sep")
                try:
                    videos = self.collect(y)
                except QuotaExceeded as e:
                    nxt = self.next_ready_year(y, queue_) if self.skip_ahead else None
                    if nxt is None or retry:
                        raise
                    self.scanning = None
                    why = L("DAILY SEARCH LIMIT", "SUCHLIMIT") if isinstance(e, SearchLimit) else L("API QUOTA", "API-KONTINGENT")
                    self.log(L(f"{y} NOT SCANNED YET ({why}) // SKIP AHEAD TO {nxt}", f"{y} NOCH NICHT GESUCHT ({why}) // WEITER MIT {nxt}"), "warn")
                    deferred.append(y)
                    queue_.remove(nxt)          # nach vorne holen, Rest bleibt in deiner Reihenfolge
                    queue_.insert(0, nxt)
                    continue
                if self.fallback and self.mode == "api":
                    self.prefetch(y + 1)
                if self.fill(y, videos) and not growing(y):
                    self.emit("select", None, y)    # fertig -> abwählen (laufendes Jahr bleibt für neue Videos)
            self.run_lists([y for y in self.years if is_list_key(y)])
            self.log(L("ALL TARGETS PROCESSED", "ALLE AUSGEWÄHLTEN JAHRE BEARBEITET"), "ok")
        except Stopped:
            reason = "gestoppt"
            self.log(L("HALTED BY USER", "VON HAND GESTOPPT"), "warn")
            self.final_check()
        except SearchLimit:
            reason = "quota"
            yy = getattr(self, 'scanning', None) or ''
            self.log(L(f"{yy} SCAN PAUSED // DAILY SEARCH LIMIT (~100 SEARCHES) REACHED // "
                       f"CONTINUES AFTER RESET {quota_reset_local()}",
                       f"{yy} SUCHE PAUSIERT // SUCHLIMIT (~100 SUCHEN) ERREICHT // "
                       f"GEHT NACH DEM RESET {quota_reset_local()} WEITER").strip(), "warn")
        except QuotaExceeded:
            reason = "quota"
            y = getattr(self, "scanning", None)
            if y is not None:
                self.log(L(f"{y} NOT SCANNED YET // THE VIDEO SCAN NEEDS API QUOTA (THE BROWSER FALLBACK "
                           f"CAN ONLY ADD) // CONTINUES AFTER RESET {quota_reset_local()}",
                           f"{y} NOCH NICHT GESUCHT // DIE SUCHE BRAUCHT API-KONTINGENT (DER BROWSER KANN NUR "
                           f"EINFÜGEN) // GEHT NACH DEM RESET {quota_reset_local()} WEITER"), "warn")
            else:
                self.log(L(f"DAILY API QUOTA REACHED // PROGRESS SAVED // RESET {quota_reset_local()}", f"TAGESKONTINGENT ERREICHT // FORTSCHRITT GESPEICHERT // RESET {quota_reset_local()}"), "warn")
        except BrowserFailed as e:
            reason = "fehler"
            self.log(L(f"BROWSER FALLBACK FAILED // {e}", f"FALLBACK FEHLGESCHLAGEN // {e}"), "err")
        except Exception as e:  # noqa: BLE001
            reason = "fehler"
            self.log(L(f"ERROR // {e}", f"FEHLER // {e}"), "err")
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
            raise RuntimeError(L(f"GOOGLE SIGN-IN REFUSED ({r.status_code}) // CHECK CLIENT ID IN API SETUP", f"GOOGLE HAT DIE ANMELDUNG ABGELEHNT ({r.status_code}) // CLIENT ID PRÜFEN"))
        d = r.json()
        self.emit("code", d["verification_url"], d["user_code"])
        self.log(L("WAITING FOR GOOGLE AUTH ...", "WARTE AUF GOOGLE-ANMELDUNG ..."))
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
                raise RuntimeError(L(f"AUTH FAILED: {t}", f"ANMELDUNG FEHLGESCHLAGEN: {t}"))
            t["expires_at"] = time.time() + t["expires_in"] - 60
            save(TOKEN_FILE, t)
            self.emit("code_done")
            self.log(L("AUTH SUCCESSFUL", "ANMELDUNG ERFOLGREICH"), "ok")
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
                self.log(L("NO SIGNAL // RETRY IN 15 S", "KEINE VERBINDUNG // NEUER VERSUCH IN 15 S"), "warn")
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
                self.log(L(f"YOUTUBE API HICCUP // {last_fail} // RETRY {attempt + 1}/{retries}", f"YOUTUBE-API-AUSSETZER // {last_fail} // VERSUCH {attempt + 1}/{retries}"), "warn")
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
                self.log(L(f"YOUTUBE API RATE LIMIT // RETRY {attempt + 1}/{limit}", f"YOUTUBE BREMST // VERSUCH {attempt + 1}/{limit}"), "warn")
                self.sleep(min(2 ** attempt, 60), "RETRY")
                continue
            raise ApiError(r.status_code, reasons, r.text)
        last = locals().get('last_fail', 'NO SIGNAL')
        raise RuntimeError(L(f"TOO MANY FAILED ATTEMPTS // {method} {path} // LAST: {last}",
                             f"ZU VIELE FEHLVERSUCHE // {method} {path} // ZULETZT: {last}"))

    def next_ready_year(self, after, queue_):
        """Nächstes markiertes Jahr (in deiner Reihenfolge), dessen Videoliste schon fertig ist und das noch
        nicht voll ist. Es werden nie Jahre dazugenommen, die du nicht markiert hast."""
        for y in queue_:
            if year_info(y)["code"] in ("ready", "progress"):
                return y
        return None

    # --- Kanal auflösen (Name, ID, Startjahr)
    def resolve_channel(self, target):
        q = {"id": target["id"]} if target.get("id") else {"forHandle": target["handle"]}
        self.log(L(f"RESOLVING CHANNEL {target.get('handle') or target.get('id')} ...", f"SUCHE KANAL {target.get('handle') or target.get('id')} ..."))
        items = self.call("GET", "channels", {"part": "snippet", **q}).get("items", [])
        if not items:
            raise RuntimeError(L(f"CHANNEL {target.get('handle') or target.get('id')} NOT FOUND", f"KANAL {target.get('handle') or target.get('id')} NICHT GEFUNDEN"))
        sn = items[0]["snippet"]
        handle = sn.get("customUrl") or target.get("handle") or ""
        if handle and not handle.startswith("@"):
            handle = "@" + handle
        info = {"id": items[0]["id"], "handle": handle, "title": sn.get("title", ""),
                "first_year": int(sn.get("publishedAt", "2005")[:4])}
        self.log(L(f"CHANNEL LOCKED // {info['title']} // SINCE {info['first_year']}", f"KANAL ERFASST // {info['title']} // SEIT {info['first_year']}"), "ok")
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
                prog = {"uploads": full_uploads(items[0]["contentDetails"]["relatedPlaylists"]["uploads"]),
                        "total": int(items[0]["statistics"]["videoCount"]),
                        "found": {}, "page_token": None, "seen": 0}
                save(archive_file(), prog)
            self.log(L(f"ARCHIVE SCAN // ALL YEARS IN ONE PASS // {prog['seen']}/{prog['total']} ...", f"ARCHIV-SCAN // ALLE JAHRE IN EINEM DURCHGANG // {prog['seen']}/{prog['total']} ..."))
            pages = 0
            while True:
                self.check()
                params = {"part": "contentDetails,snippet", "playlistId": prog["uploads"], "maxResults": 50}
                if prog["page_token"]:
                    params["pageToken"] = prog["page_token"]
                d = self.call("GET", "playlistItems", params)
                remember_titles(item_titles(d.get("items")))
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
                    self.log(L(f"ARCHIVE SCAN // {prog['seen']}/{prog['total']} UPLOADS", f"ARCHIV-SCAN // {prog['seen']}/{prog['total']} UPLOADS"))
                if not prog["page_token"]:
                    break
        except QuotaExceeded:
            save(archive_file(), prog)
            self.log(L(f"ARCHIVE SCAN PAUSED // QUOTA // {prog.get('seen', 0)}/{prog.get('total', '?')} "
                       f"// CONTINUES AFTER RESET {quota_reset_local()}",
                       f"ARCHIV-SCAN PAUSIERT // KONTINGENT // {prog.get('seen', 0)}/{prog.get('total', '?')} "
                       f"// GEHT NACH DEM RESET {quota_reset_local()} WEITER"), "warn")
            return
        by_year = {}
        for vid, pub in prog["found"].items():
            by_year.setdefault(int(pub[:4]), []).append({"id": vid, "published": pub})
        complete = prog["seen"] < CAP_SEEN
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
        self.log(L(f"ARCHIVE SCAN COMPLETE // {len(written)} YEARS READY FOR THE BROWSER FALLBACK", f"ARCHIV-SCAN FERTIG // {len(written)} JAHRE BEREIT FÜR DEN FALLBACK"), "ok")
        if not complete:
            self.log(L(f"UPLOAD LIST CAPPED BY YOUTUBE // {oldest} AND OLDER USE THE SLOWER DATE SEARCH", f"YOUTUBE KAPPT DIE UPLOAD-LISTE // {oldest} UND ÄLTER ÜBER DIE LANGSAMERE DATUMSSUCHE"), "warn")

    def use_web(self, year):
        """Videoliste über die Kanalseite holen? Nur mit FALLBACK. Für Jahre hinter der 20.000er-Grenze
        immer (die API-Suche ist lückenhaft), sonst nur, wenn das Kontingent leer ist."""
        if not self.fallback:
            return False
        cut = archive_cutoff()
        if cut is not None and year <= cut:
            return True
        return bool(quota_blocked("units") or quota_blocked("search"))

    def ensure_browser(self):
        if not self.browser:
            self.browser = BrowserUplink(self)
            self.browser.start()

    def channel_id(self):
        """Kanal-ID besorgen: gespeichert, aus Scan-Daten oder über YouTube auflösen. Wird gemerkt."""
        cid = known_channel_id()
        if not cid:
            self.ensure_browser()
            cid = self.browser.resolve_channel(CHANNEL.get("handle") or "")
        if not cid:
            raise BrowserFailed(L("CHANNEL ID NOT FOUND // EDIT THE CHANNEL ONCE", "KANAL-ID NICHT GEFUNDEN // KANAL EINMAL NEU EINTRAGEN"))
        if CHANNEL.get("id") != cid:
            CHANNEL["id"] = cid
            self.emit("channel_id", cid)
        return cid

    def web_index(self, year):
        """Liest die Videoseite des Kanals (älteste zuerst), bis das Jahr komplett abgedeckt ist.
        Speichert Reihenfolge und Jahr jedes Videos in web_index.json (wird fortgesetzt)."""
        cid = self.channel_id()
        self.ensure_browser()
        idx = load(web_index_file(), {})
        if idx.get("channel") != cid:
            idx = {"channel": cid, "ids": [], "year": {}, "token": None, "last": None, "end": False}
        if not idx["ids"] and not idx.get("token"):
            self.log(L("CHANNEL PAGE // OPENING VIDEOS SORTED BY OLDEST ...", "KANALSEITE // ÖFFNE VIDEOS, ÄLTESTE ZUERST ..."))
            idx["token"] = self.browser.oldest_token(cid)
        pages = 0
        while not idx["end"] and (idx["last"] is None or int(idx["last"][:4]) <= year):
            self.check()
            ids, tok = self.browser.video_page(idx["token"])
            ids = [v for v in ids if v not in idx["year"]]
            if ids:
                last = self.browser.video_date(ids[-1]) or idx["last"]
                prev = idx["last"]
                if prev is None:
                    first = self.browser.video_date(ids[0])
                    prev = first
                if prev and last and prev[:4] == last[:4]:
                    for v in ids:
                        idx["year"][v] = int(last[:4])
                else:
                    # Jahreswechsel auf dieser Seite: jedes Video einzeln datieren
                    cur = prev
                    for v in ids:
                        d = self.browser.video_date(v) or cur
                        cur = d
                        if d:
                            idx["year"][v] = int(d[:4])
                idx["ids"] += ids
                idx["last"] = last
            idx["token"] = tok
            if not tok:
                idx["end"] = True
            save(web_index_file(), idx)
            self.act(L(f"CHANNEL PAGE // {len(idx['ids'])} READ // {fmt_day(idx['last'])}",
                       f"KANALSEITE // {len(idx['ids'])} GELESEN // {fmt_day(idx['last'])}"), year)
            pages += 1
            if pages % 10 == 0:
                self.log(L(f"CHANNEL PAGE // {len(idx['ids'])} VIDEOS READ // NOW AT {fmt_day(idx['last'])}",
                           f"KANALSEITE // {len(idx['ids'])} VIDEOS GELESEN // STAND {fmt_day(idx['last'])}"))
            time.sleep(random.uniform(0.8, 1.6))
        return [v for v in idx["ids"] if idx["year"].get(v) == year]

    def collect_web(self, year, old=None):
        """Videoliste eines Jahres über die Kanalseite. Videos aus einer alten Liste (Suche), die dort
        fehlen, bleiben an ihrer bisherigen Stelle erhalten."""
        self.scanning = year
        self.log(L(f"{year_label(year)} // READING THE CHANNEL PAGE (NO API QUOTA NEEDED) ...",
                   f"{year_label(year)} // LESE DIE KANALSEITE (OHNE API-KONTINGENT) ..."))
        self.act(L("CHANNEL PAGE // OPENING ...", "KANALSEITE // WIRD GEÖFFNET ..."), year)
        web = self.web_index(year)
        if not web:
            raise BrowserFailed(L(f"{year_label(year)} // NO VIDEOS FOUND ON THE CHANNEL PAGE", f"{year_label(year)} // KEINE VIDEOS AUF DER KANALSEITE GEFUNDEN"))
        merged = merge_keep(web, old or [])   # Kanalseite ergänzt nur; sie zeigt nicht alle Videos, also nie etwas entfernen
        videos = [{"id": v, "published": None} for v in merged]
        save(videos_file(year), videos)
        cf = collect_file(year)
        prog = load(cf, {})
        prog.update(method="done", source="web")
        save(cf, prog)
        self.scanning = None
        if old:
            old_ids = {v["id"] for v in old}
            plus = len(set(merged) - old_ids)
            minus = len(old_ids - set(merged))
            self.log(L(f"{year_label(year)} // {len(merged)} VIDEOS // {plus} NEW",
                       f"{year_label(year)} // {len(merged)} VIDEOS // {plus} NEU"),
                     "ok" if plus or minus else "info")
        else:
            self.log(L(f"{len(videos)} VIDEOS FROM {year_label(year)} FOUND", f"{len(videos)} VIDEOS AUS {year_label(year)} GEFUNDEN"), "ok")
        self.emit("progress", year)
        self.act(None, year)
        return videos

    def uploads_id(self):
        up = load(archive_file(), {}).get("uploads")
        if not up:
            for f in sorted(CHAN_DIR.glob("collect_*.json")):
                up = load(f, {}).get("uploads")
                if up:
                    break
        if not up:
            q = {"id": CHANNEL["id"]} if CHANNEL.get("id") else {"forHandle": CHANNEL.get("handle", "")}
            items = self.call("GET", "channels", {"part": "contentDetails", **q}).get("items", [])
            up = items[0]["contentDetails"]["relatedPlaylists"]["uploads"] if items else None
        return full_uploads(up)

    def shorts_scan(self):
        """Einmal die Shorts-Liste des Kanals über die API lesen (1 Einheit pro 50). Ergebnis in shorts.json."""
        data = load(shorts_file(), {})
        if data.get("done"):
            return set(data.get("ids") or [])
        if not data.get("list"):
            up = self.uploads_id()
            if not up:
                return None
            data = {"list": shorts_list(up), "token": None, "ids": []}
        self.log(L("SHORTS // READING THE CHANNEL'S SHORTS LIST ...", "SHORTS // LESE DIE SHORTS-LISTE DES KANALS ..."))
        try:
            while True:
                self.check()
                params = {"part": "contentDetails", "playlistId": data["list"], "maxResults": 50}
                if data["token"]:
                    params["pageToken"] = data["token"]
                try:
                    d = self.call("GET", "playlistItems", params)
                except ApiError as e:
                    if e.reasons & {"playlistNotFound"} or "404" in str(e):
                        d = {"items": []}       # Kanal ohne Shorts
                    else:
                        raise
                data["ids"] += [it["contentDetails"]["videoId"] for it in d.get("items", [])]
                data["token"] = d.get("nextPageToken")
                self.act(L(f"SHORTS // {len(data['ids'])} FOUND", f"SHORTS // {len(data['ids'])} GEFUNDEN"))
                if not data["token"]:
                    break
        except QuotaExceeded:
            save(shorts_file(), data)
            self.act()
            raise
        data["done"] = True
        save(shorts_file(), data)
        self.act()
        self.log(L(f"SHORTS // {len(data['ids'])} SHORTS ON THE CHANNEL", f"SHORTS // {len(data['ids'])} SHORTS AUF DEM KANAL"), "ok")
        return set(data["ids"])

    def get_shorts(self):
        """Shorts-IDs oder None (dann ist noch kein Kontingent dafür da)."""
        sh = known_shorts()
        if sh is not None:
            return sh
        if quota_blocked("units"):
            return None
        try:
            return self.shorts_scan()
        except QuotaExceeded:
            return None

    def restore_wrongly_removed(self, year, videos):
        """Builds 75–82 haben auch Livestreams (und wegen der Kanalseite teils normale Videos) aussortiert.
        Einmalig alles zurückholen, was in den gespeicherten API-Daten für dieses Jahr steht – außer Shorts."""
        cf = collect_file(year)
        prog = load(cf, {})
        if prog.get("restored_v83"):
            return videos
        dates = known_dates()
        have = {v["id"] for v in videos}
        extra = [(v, p) for v, p in dates.items() if p[:4] == str(year) and v not in have]
        shorts = set()
        if extra and year >= 2020:              # Shorts gibt es erst seit 2020
            shorts = self.get_shorts()
            if shorts is None:
                return videos                   # erst mit Kontingent unterscheidbar
        extra = [(v, p) for v, p in extra if v not in shorts]
        prog["restored_v83"] = True
        for k in ("videos_only", "lf_checked", "restored_v80"):
            prog.pop(k, None)
        if extra:
            videos = insert_by_date(videos, extra, dates)
            save(videos_file(year), videos)
            self.log(L(f"{year_label(year)} // {len(extra)} VIDEO(S) RESTORED TO THE LIST (WRONGLY LEFT OUT BEFORE)",
                       f"{year_label(year)} // {len(extra)} VIDEO(S) WIEDER IN DIE LISTE (VORHER FÄLSCHLICH AUSSORTIERT)"), "warn")
            self.emit("progress", year)
        st = load(state_file(year), {})
        if st.get("playlist_id"):
            st["repair"] = True                 # einmal komplett nachtragen/prüfen
            save(state_file(year), st)
        save(cf, prog)
        return videos

    def videos_only(self, year, videos):
        """Shorts aus der Liste entfernen (Livestreams bleiben drin)."""
        videos = self.restore_wrongly_removed(year, videos)
        cf = collect_file(year)
        prog = load(cf, {})
        if prog.get("shorts_checked"):
            return videos
        if year < 2020:
            prog["shorts_checked"] = True
            save(cf, prog)
            return videos
        shorts = self.get_shorts()
        if shorts is None:
            if not getattr(self, "_shorts_postponed", False):
                self._shorts_postponed = True
                self.log(L("SHORTS CHECK POSTPONED // NEEDS A LITTLE API QUOTA",
                           "PRÜFUNG AUF SHORTS VERSCHOBEN // BRAUCHT ETWAS API-KONTINGENT"), "warn")
            return videos
        cleaned = [v for v in videos if v["id"] not in shorts]
        prog["shorts_checked"] = True
        save(cf, prog)
        removed = len(videos) - len(cleaned)
        if not removed:
            return videos
        save(videos_file(year), cleaned)
        st = load(state_file(year), {})
        if st.get("playlist_id"):
            st["repair"] = True
            save(state_file(year), st)
        self.log(L(f"{year_label(year)} // {removed} SHORTS REMOVED FROM THE LIST", f"{year_label(year)} // {removed} SHORTS AUS DER LISTE ENTFERNT"), "warn")
        self.emit("progress", year)
        return cleaned

    def refresh_new(self, year, videos):
        """Laufendes Jahr: neue Videos hinten anhängen. Mit Kontingent über die API (Upload-Liste,
        1 Einheit pro 50), sonst mit FALLBACK über die Kanalseite."""
        known = {v["id"] for v in videos}
        self.act(L("LOOKING FOR NEW VIDEOS ...", "SUCHE NEUE VIDEOS ..."), year)
        new = []          # (id, published) neueste zuerst
        try:
            if not quota_blocked("units"):
                up = full_uploads(load(archive_file(), {}).get("uploads") or load(collect_file(year), {}).get("uploads"))
                if not up:
                    q = {"id": CHANNEL["id"]} if CHANNEL.get("id") else {"forHandle": CHANNEL.get("handle", "")}
                    items = self.call("GET", "channels", {"part": "contentDetails", **q}).get("items", [])
                    up = full_uploads(items[0]["contentDetails"]["relatedPlaylists"]["uploads"]) if items else None
                token, done = None, False
                while up and not done:
                    params = {"part": "contentDetails,snippet", "playlistId": up, "maxResults": 50}
                    if token:
                        params["pageToken"] = token
                    d = self.call("GET", "playlistItems", params)
                    remember_titles(item_titles(d.get("items")))
                    for it in d.get("items", []):
                        cd = it["contentDetails"]
                        pub = cd.get("videoPublishedAt")
                        if not pub:
                            continue
                        if cd["videoId"] in known or int(pub[:4]) < year:
                            done = True
                            break
                        if int(pub[:4]) == year:
                            new.append((cd["videoId"], pub))
                    token = d.get("nextPageToken")
                    if not token:
                        break
            elif self.fallback:
                cid = self.channel_id()
                self.ensure_browser()
                ids = self.browser.latest_videos(cid, known)
                if ids:
                    oldest = self.browser.video_date(ids[-1]) or ""
                    newest = self.browser.video_date(ids[0]) or ""
                    if oldest[:4] == newest[:4] == str(year):
                        new = [(v, None) for v in ids]
                    else:
                        for v in ids:
                            d = self.browser.video_date(v) or ""
                            if d[:4] == str(year):
                                new.append((v, d))
            else:
                self.log(L(f"{year_label(year)} // CHECK FOR NEW VIDEOS SKIPPED // NEEDS API QUOTA OR FALLBACK",
                           f"{year_label(year)} // NEUE VIDEOS NICHT GEPRÜFT // BRAUCHT API-KONTINGENT ODER FALLBACK"), "warn")
        except QuotaExceeded:
            self.log(L(f"{year_label(year)} // CHECK FOR NEW VIDEOS SKIPPED // QUOTA", f"{year_label(year)} // NEUE VIDEOS NICHT GEPRÜFT // KONTINGENT"), "warn")
        finally:
            self.act(None, year)
        if not new:
            self.log(L(f"{year_label(year)} // NO NEW VIDEOS", f"{year_label(year)} // KEINE NEUEN VIDEOS"))
            return videos
        videos = videos + [{"id": v, "published": p} for v, p in reversed(new)]
        save(videos_file(year), videos)
        self.log(L(f"{year_label(year)} // {len(new)} NEW VIDEO(S) FOUND", f"{year_label(year)} // {len(new)} NEUE(S) VIDEO(S) GEFUNDEN"), "ok")
        self.emit("progress", year)
        return videos

    def collect(self, year):
        cached = load(videos_file(year), None)
        if cached is not None:
            cached = self.restore_wrongly_removed(year, cached)
            cut = archive_cutoff()
            searched = cut is not None and year <= cut          # Liste stammt aus der lückenhaften Suche
            if self.fallback and searched and load(collect_file(year), {}).get("source") != "web":
                # bisher über die lückenhafte Suche gefunden -> mit der Kanalseite abgleichen
                videos = self.collect_web(year, cached)
                if [v["id"] for v in videos] != [v["id"] for v in cached]:
                    st = load(state_file(year), {})
                    if st.get("playlist_id"):
                        st["repair"] = True
                        save(state_file(year), st)
                return videos
            cached = self.videos_only(year, cached)
            if growing(year):
                done = getattr(self, "_refreshed", set())
                if year not in done:            # pro Lauf nur einmal nachsehen
                    self._refreshed = done | {year}
                    return self.refresh_new(year, cached)
            return cached
        if self.use_web(year):
            return self.collect_web(year)
        self.scanning = year

        cf = collect_file(year)
        prog = load(cf, {})
        if "channel_id" not in prog:
            q = {"id": CHANNEL["id"]} if CHANNEL.get("id") else {"forHandle": CHANNEL.get("handle", "")}
            items = self.call("GET", "channels", {"part": "contentDetails,statistics", **q}).get("items", [])
            if not items:
                raise RuntimeError(L(f"CHANNEL {CHANNEL.get('handle') or CHANNEL.get('id')} NOT FOUND", f"KANAL {CHANNEL.get('handle') or CHANNEL.get('id')} NICHT GEFUNDEN"))
            ch = items[0]
            prog = {
                "channel_id": ch["id"],
                "uploads": full_uploads(ch["contentDetails"]["relatedPlaylists"]["uploads"]),
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
            self.log(L(f"{year_label(year)} IS BEYOND YOUTUBE'S 20,000 UPLOAD LIMIT // DATE SEARCH (~100 UNITS PER 50 VIDEOS)", f"{year_label(year)} LIEGT HINTER DER 20.000-UPLOAD-GRENZE // DATUMSSUCHE (~100 EINHEITEN PRO 50 VIDEOS)"))
        prefix = str(year)
        if prog["method"] == "uploads":
            self.log(L(f"SCANNING CHANNEL UPLOADS FOR {year_label(year)} ...", f"DURCHSUCHE UPLOADS NACH {year_label(year)} ..."))
            reached_older, pages = False, 0
            while True:
                params = {"part": "contentDetails,snippet", "playlistId": prog["uploads"], "maxResults": 50}
                if prog["page_token"]:
                    params["pageToken"] = prog["page_token"]
                d = self.call("GET", "playlistItems", params)
                remember_titles(item_titles(d.get("items")))
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
                    self.log(L(f"{prog['seen']}/{prog['total']} UPLOADS SCANNED // {len(prog['found'])} FROM {year_label(year)}",
                               f"{prog['seen']}/{prog['total']} UPLOADS DURCHSUCHT // {len(prog['found'])} AUS {year_label(year)}"))
                if reached_older or not prog["page_token"]:
                    break
            if reached_older or prog["seen"] < CAP_SEEN:
                prog["method"] = "done"
            else:
                self.log(L("UPLOAD LIST CAPPED // SWITCHING TO DATE SEARCH", "UPLOAD-LISTE GEKAPPT // WECHSEL ZUR DATUMSSUCHE"), "warn")
                prog["method"] = "search"
            save(cf, prog)

        if prog["method"] == "search":
            for m in range(1, 13):
                if m in prog["done_months"]:
                    continue
                start = f"{year_label(year)}-{m:02d}-01T00:00:00Z"
                end = f"{year + 1}-01-01T00:00:00Z" if m == 12 else f"{year_label(year)}-{m + 1:02d}-01T00:00:00Z"
                token, count = None, 0
                self.act(L(f"DATE SEARCH // {m:02d}/{year_label(year)}", f"DATUMSSUCHE // {m:02d}/{year_label(year)}"), year)
                while True:
                    params = {
                        "part": "snippet", "channelId": prog["channel_id"], "type": "video",
                        "order": "date", "maxResults": 50,
                        "publishedAfter": start, "publishedBefore": end,
                    }
                    if token:
                        params["pageToken"] = token
                    d = self.call("GET", "search", params)
                    remember_titles({it["id"]["videoId"]: it["snippet"].get("title") for it in d.get("items", [])})
                    for it in d.get("items", []):
                        prog["found"][it["id"]["videoId"]] = it["snippet"]["publishedAt"]
                        count += 1
                    token = d.get("nextPageToken")
                    if not token:
                        break
                if count >= 500:
                    self.log(L(f"WARNING // {m:02d}/{year_label(year)} HAS 500+ HITS, MAY BE INCOMPLETE", f"WARNUNG // {m:02d}/{year_label(year)} HAT ÜBER 500 TREFFER, EVTL. UNVOLLSTÄNDIG"), "warn")
                prog["done_months"].append(m)
                save(cf, prog)
                self.log(f"{m:02d}/{year_label(year)} // {count} VIDEOS")
            prog["method"] = "done"
            save(cf, prog)
            self.act(None, year)

        videos = sorted(
            ({"id": vid, "published": pub} for vid, pub in prog["found"].items()),
            key=lambda v: v["published"],
        )
        save(videos_file(year), videos)
        self.scanning = None
        self.log(L(f"{len(videos)} VIDEOS FROM {year_label(year)} FOUND", f"{len(videos)} VIDEOS AUS {year_label(year)} GEFUNDEN"), "ok")
        self.emit("progress", year)
        return videos

    # --- Stichwortlisten
    def fetch_missing_titles(self, key=None):
        """Fehlende Videotitel nachladen: erst aus den eigenen Playlists (API, 1 Einheit pro 50), den Rest per oEmbed.
        Fortschritt erscheint auf der Karte `key` (Zähler und Prozent) und alle 10 % im Protokoll."""
        pool = all_year_videos()
        left = set(missing_titles(pool))
        total = len(left)
        if not total:
            return
        self.log(L(f"LISTS // LOADING {total} MISSING VIDEO TITLES ...", f"LISTEN // LADE {total} FEHLENDE VIDEOTITEL ..."))
        state = {"step": 0}

        def progress():
            done = total - len(left)
            pct = done * 100 // total
            self.act(L(f"LOADING TITLES // {done}/{total} ({pct}%)", f"LADE TITEL // {done}/{total} ({pct} %)"), key)
            if pct // 10 > state["step"]:
                state["step"] = pct // 10
                self.log(L(f"TITLES // {done}/{total} ({pct}%)", f"TITEL // {done}/{total} ({pct} %)"))

        def got_titles(t):
            n = remember_titles(t)
            left.difference_update(t)
            return n
        by_year = {}
        for v in pool:
            if v["id"] in left:
                by_year.setdefault(v["year"], set()).add(v["id"])
        got = 0
        progress()
        try:
            if self.mode == "api" and not quota_blocked("units"):
                for y in sorted(by_year):
                    for pid in part_ids(load(state_file(y), {})):
                        token = None
                        while True:
                            self.check()
                            params = {"part": "snippet", "playlistId": pid, "maxResults": 50}
                            if token:
                                params["pageToken"] = token
                            d = self.call("GET", "playlistItems", params)
                            got += got_titles(item_titles(d.get("items")))
                            progress()
                            token = d.get("nextPageToken")
                            if not token:
                                break
        except QuotaExceeded:
            self.log(L("TITLES // API QUOTA USED UP // CONTINUING WITHOUT IT", "TITEL // KONTINGENT AUFGEBRAUCHT // WEITER OHNE"), "warn")
        rest = [v["id"] for v in pool if v["id"] in left and v["id"] not in set(load(notitle_file(), []))]
        if rest:
            self.log(L(f"TITLES // {len(rest)} MORE VIA OEMBED (SLOW, NO QUOTA)", f"TITEL // NOCH {len(rest)} ÜBER OEMBED (LANGSAM, KEIN KONTINGENT)"))
        nt = set(load(notitle_file(), []))
        batch = {}
        try:
            for n, vid in enumerate(rest, 1):
                self.check()
                t = video_title(vid)
                if t:
                    batch[vid] = t
                else:
                    nt.add(vid)
                    left.discard(vid)
                if n % 25 == 0 or n == len(rest):
                    got += got_titles(batch)
                    batch = {}
                    save(notitle_file(), sorted(nt))
                    progress()
                self.sleep(random.uniform(0.1, 0.3))
        finally:
            got += got_titles(batch)
            save(notitle_file(), sorted(nt))
            self.act(None, key)
        self.log(L(f"LISTS // {got} TITLES LOADED", f"LISTEN // {got} TITEL GELADEN"), "ok")

    def sync_list(self, key):
        """Treffer der Liste neu berechnen und als Videoliste speichern. Ändert sich mehr als ein Anhängen
        am Ende, muss die Playlist einmal repariert (einsortiert/bereinigt) werden."""
        lst = find_list(key)
        new = list_videos(lst)
        old = load(videos_file(key), None)
        state = load(state_file(key), {})
        if old is not None and state.get("playlist_id"):
            oldids, newids = [v["id"] for v in old], [v["id"] for v in new]
            if newids[:len(oldids)] != oldids:
                state["repair"] = True
                save(state_file(key), state)
        save(videos_file(key), new)
        return new

    def run_lists(self, keys):
        if not keys:
            return
        self.emit("current", keys[0])
        self.fetch_missing_titles(keys[0])
        for key in keys:
            self.check()
            lst = find_list(key)
            if not lst:
                continue
            self.emit("current", key)
            self.log(L(f"// LIST {lst['name']}", f"// LISTE {lst['name']}"), "sep")
            videos = self.sync_list(key)
            self.log(L(f"{len(videos)} VIDEOS MATCH", f"{len(videos)} VIDEOS PASSEN"), "ok")
            self.emit("progress", key)
            if not videos and not load(state_file(key), {}).get("playlist_id"):
                self.log(L("NO MATCHES // NO PLAYLIST CREATED YET", "KEINE TREFFER // NOCH KEINE PLAYLIST ANGELEGT"), "warn")
                continue
            self.fill(key, videos)

    # --- Playlist befüllen
    def prefetch(self, year):
        """Nächstes Jahr schon mit API-Kontingent vorbereiten, damit der Browser dort weitermachen kann."""
        if year >= datetime.date.today().year:
            return
        try:
            if load(videos_file(year), None) is None:
                self.log(L(f"PRE-SCAN {year_label(year)} FOR BROWSER FALLBACK", f"VORAB-SUCHE {year_label(year)} FÜR DEN FALLBACK"))
            self.collect(year)
            self.ensure_playlist(year)
        except SearchLimit:
            self.scanning = None
            self.log(L(f"PRE-SCAN {year_label(year)} PAUSED // DAILY SEARCH LIMIT (~100 SEARCHES) REACHED // "
                       f"CONTINUES AFTER RESET {quota_reset_local()}",
                       f"VORAB-SUCHE {year_label(year)} PAUSIERT // SUCHLIMIT (~100 SUCHEN) ERREICHT // "
                       f"GEHT NACH DEM RESET {quota_reset_local()} WEITER"), "warn")
        except QuotaExceeded:
            self.scanning = None
            self.log(L(f"PRE-SCAN {year_label(year)} SKIPPED // QUOTA", f"VORAB-SUCHE {year_label(year)} ÜBERSPRUNGEN // KONTINGENT"), "warn")
        except (Stopped, BrowserFailed):
            raise
        except Exception as e:  # noqa: BLE001
            self.scanning = None
            self.log(L(f"PRE-SCAN {year_label(year)} INTERRUPTED // {e} // PROGRESS SAVED, CONTINUES NEXT RUN", f"VORAB-SUCHE {year_label(year)} UNTERBROCHEN // {e} // GESPEICHERT, GEHT BEIM NÄCHSTEN MAL WEITER"), "warn")
        finally:
            self.act(None, year)

    def create_playlist(self, title, description):
        pid = None
        if self.mode == "api":
            try:
                pid = self.call("POST", "playlists", {"part": "snippet,status"}, {
                    "snippet": {"title": title, "description": description},
                    "status": {"privacyStatus": PRIVACY},
                })["id"]
            except QuotaExceeded:
                if not self.fallback:
                    raise
                self.switch_to_browser()
        if pid is None:
            if not self.browser:
                raise QuotaExceeded()
            pid = self.browser.create_playlist(title, description)
        return pid

    def rename_playlist(self, pid, title, description):
        """Bestmöglich umbenennen (Browser oder API). Fehler sind nicht schlimm, nur ein Hinweis."""
        try:
            if self.browser and self.browser.rename_playlist(pid, title):
                return True
            if not quota_blocked("units"):
                self.call("PUT", "playlists", {"part": "snippet"},
                          {"id": pid, "snippet": {"title": title, "description": description}})
                return True
        except Exception:  # noqa: BLE001
            pass
        self.log(L(f"COULD NOT RENAME PLAYLIST // PLEASE RENAME IT TO \"{title}\" BY HAND",
                   f"PLAYLIST KONNTE NICHT UMBENANNT WERDEN // BITTE VON HAND IN \"{title}\" UMBENENNEN"), "warn")
        return False

    def ensure_part(self, year, state, k):
        """Playlist für Teil k (ab 0) besorgen, bei Bedarf anlegen. Ab Teil 2 wird Teil 1 umbenannt."""
        parts = part_ids(state)
        if k < len(parts):
            return parts[k]
        while len(parts) <= k:
            n = len(parts) + 1
            title = part_title(year, n, n)
            pid = self.create_playlist(title, playlist_description(year))
            parts.append(pid)
            state["parts"] = parts
            state["playlist_id"] = parts[0]
            save(state_file(year), state)
            self.log(L(f"{year_label(year)} HAS MORE THAN {PART_MAX} VIDEOS // PLAYLIST PART {n} CREATED // {title}",
                       f"{year_label(year)} HAT MEHR ALS {PART_MAX} VIDEOS // PLAYLIST TEIL {n} ANGELEGT // {title}"), "ok")
            if n == 2:
                self.rename_playlist(parts[0], part_title(year, 1, 2), playlist_description(year))
        try:
            self.apply_thumbnails(only_year=year)
        except Exception as e:  # noqa: BLE001
            self.log(L(f"THUMBNAIL {year_label(year)} FAILED // {str(e)[:120]}", f"THUMBNAIL {year_label(year)} FEHLGESCHLAGEN // {str(e)[:120]}"), "err")
        return parts[k]

    def ensure_playlist(self, year):
        sf = state_file(year)
        state = load(sf, {})
        if not state.get("playlist_id"):
            pid = self.create_playlist(playlist_title(year), playlist_description(year))
            state = {"playlist_id": pid, "parts": [pid], "next": 0, "skipped": []}
            save(sf, state)
            self.log(L(f"PLAYLIST CREATED // {playlist_title(year)}", f"PLAYLIST ANGELEGT // {playlist_title(year)}"), "ok")
            try:
                self.apply_thumbnails(only_year=year)
            except Exception as e:  # noqa: BLE001
                self.log(L(f"THUMBNAIL {year_label(year)} FAILED // {str(e)[:120]}", f"THUMBNAIL {year_label(year)} FEHLGESCHLAGEN // {str(e)[:120]}"), "err")
        return state

    # --- Playlist-Thumbnails (eigenes Bild pro Jahr)
    def upload_call(self, method, params, meta, data, mime):
        """Multipart-Upload an /upload/youtube/v3/playlistImages (Metadaten + Bild)."""
        if quota_blocked("units"):
            raise QuotaExceeded()
        boundary = "chronolist" + str(random.randint(10 ** 8, 10 ** 9))
        body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
                f"{json.dumps(meta)}\r\n--{boundary}\r\nContent-Type: {mime}\r\n\r\n").encode() + data + \
            f"\r\n--{boundary}--\r\n".encode()
        for _ in range(3):
            if time.time() > self.tok.get("expires_at", 0):
                self.refresh()
            r = requests.request(method, "https://www.googleapis.com/upload/youtube/v3/playlistImages",
                                 params={**params, "uploadType": "multipart"}, data=body, timeout=60,
                                 headers={"Authorization": f"Bearer {self.tok['access_token']}",
                                          "Content-Type": f"multipart/related; boundary={boundary}"})
            if r.ok:
                return r.json() if r.content else {}
            if r.status_code == 401:
                self.refresh()
                continue
            try:
                errs = r.json().get("error", {})
            except ValueError:
                errs = {}
            reasons = {e.get("reason") for e in errs.get("errors", [])}
            if reasons & {"quotaExceeded", "dailyLimitExceeded"}:
                mark_quota("units")
                raise QuotaExceeded()
            raise ApiError(r.status_code, reasons, errs.get("message") or r.text)
        raise RuntimeError(L("THUMBNAIL UPLOAD FAILED // AUTH", "THUMBNAIL-UPLOAD FEHLGESCHLAGEN // ANMELDUNG"))

    def apply_thumbnails(self, only_year=None):
        """Wartende Thumbnails hochladen, sobald die Playlist existiert. Nur über die API (50 Einheiten pro Bild)."""
        info = load(thumbs_file(), {})
        todo = [(int(y) if str(y).isdigit() else y, t) for y, t in info.items()
                if t.get("status") in ("queued", "error")        # Fehlgeschlagene bei jedem Lauf noch einmal versuchen
                and (only_year is None or (int(y) if str(y).isdigit() else y) == only_year)]
        for y, t in sorted(todo, key=lambda x: str(x[0])):
            pids = part_ids(load(state_file(y), {}))
            path = thumbs_dir() / t.get("file", "")
            if not pids or not path.exists():
                continue
            if self.mode != "api" or quota_blocked("units"):
                self.log(L(f"THUMBNAIL {year_label(y)} WAITING // NEEDS API QUOTA", f"THUMBNAIL {year_label(y)} WARTET // BRAUCHT API-KONTINGENT"), "warn")
                return
            mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
            self.log(L(f"THUMBNAIL {year_label(y)} // UPLOADING ...", f"THUMBNAIL {year_label(y)} // WIRD HOCHGELADEN ..."))
            try:
                for pid in pids:
                    # list: die Playlist heißt hier "parent" (nicht "playlistId")
                    existing = self.call("GET", "playlistImages", {"part": "snippet", "parent": pid}).get("items", [])
                    meta = {"kind": "youtube#playlistImage", "snippet": {"playlistId": pid, "type": "hero"}}
                    data = path.read_bytes()
                    if existing:
                        meta["id"] = existing[0]["id"]
                        self.upload_call("PUT", {"part": "snippet"}, meta, data, mime)
                    else:
                        self.upload_call("POST", {"part": "snippet"}, meta, data, mime)
                t.update(status="done", error="", parts=len(pids))
                self.log(L(f"THUMBNAIL {year_label(y)} UPLOADED", f"THUMBNAIL {year_label(y)} HOCHGELADEN"), "ok")
            except QuotaExceeded:
                self.log(L(f"THUMBNAIL {year_label(y)} WAITING // NEEDS API QUOTA", f"THUMBNAIL {year_label(y)} WARTET // BRAUCHT API-KONTINGENT"), "warn")
                return
            except (ApiError, RuntimeError, requests.RequestException) as e:
                t.update(status="error", error=str(e)[:200])
                self.log(L(f"THUMBNAIL {year_label(y)} FAILED // {str(e)[:120]}", f"THUMBNAIL {year_label(y)} FEHLGESCHLAGEN // {str(e)[:120]}"), "err")
            info[str(y)] = t
            save(thumbs_file(), info)

    def switch_to_browser(self):
        self.log(L("DAILY API QUOTA REACHED // SWITCHING TO BROWSER FALLBACK", "TAGESKONTINGENT ERREICHT // WECHSEL ZUM FALLBACK"), "warn")
        self.mode = "browser"
        self.emit("mode", "browser")
        if not self.browser:          # evtl. schon offen (Kanalseite)
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
        self.log(L(f"BATCH OF {len(video_ids)} REJECTED // CHECKING SINGLY", f"PAKET MIT {len(video_ids)} ABGELEHNT // PRÜFE EINZELN"), "warn")
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
            self.log(L(f"BROWSER UPLINK // {count} ADDED // {label} {fmt_secs(pause)}, RESUME {resume}", f"BROWSER-MODUS // {count} EINGEFÜGT // {wait_label(label)} {fmt_secs(pause)}, WEITER UM {resume}"))
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
                    self.act(L(f"CHECKING PLAYLIST // {len(items)} READ", f"PRÜFE PLAYLIST // {len(items)} GELESEN"))
                    token = d.get("nextPageToken")
                    if not token:
                        return items
            except QuotaExceeded:
                if not self.fallback:
                    raise
                self.switch_to_browser()
        return self.browser.list_items(pid, lambda n: self.act(
            L(f"CHECKING PLAYLIST // {n} READ", f"PRÜFE PLAYLIST // {n} GELESEN")))

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
        pids = part_ids(state)
        if not pids:
            return state
        self.emit("checking", year, True)
        self.log(L(f"PLAYLIST CHECK {year_label(year)} // {why} ...", f"PLAYLIST-PRÜFUNG {year_label(year)} // {why} ..."))
        try:
            removed = 0
            seen = set()
            for pid in pids:              # Teile der Reihe nach; doppelt = schon in einem früheren Teil
                before = set(seen)
                for _ in range(3):
                    try:
                        items = self.list_playlist(pid)
                        here, dups = set(), []
                        for it in items:
                            if it["vid"] in here or it["vid"] in before:
                                dups.append(it)
                            else:
                                here.add(it["vid"])
                        for it in dups:
                            self.remove_item(pid, it)
                            removed += 1
                            self.log(L(f"DUPLICATE REMOVED // {it['vid']}", f"DOPPELTES VIDEO ENTFERNT // {it['vid']}"), "warn")
                        seen = before | here
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
                self.log(L(f"RESYNC {year_label(year)} // {moved} VIDEO(S) WERE ALREADY IN THE PLAYLIST", f"ABGLEICH {year_label(year)} // {moved} VIDEO(S) WAREN SCHON IN DER PLAYLIST"), "warn")
            msg = L(f"{removed} DUPLICATE(S) REMOVED", f"{removed} DOPPELTE ENTFERNT") if removed else L("NO DUPLICATES", "KEINE DOPPELTEN")
            self.log(L(f"PLAYLIST CHECK {year_label(year)} // {len(seen)} VIDEOS // {msg}", f"PLAYLIST-PRÜFUNG {year_label(year)} // {len(seen)} VIDEOS // {msg}"), "ok")
        except QuotaExceeded:
            self.log(L(f"PLAYLIST CHECK {year_label(year)} SKIPPED // QUOTA", f"PLAYLIST-PRÜFUNG {year_label(year)} ÜBERSPRUNGEN // KONTINGENT"), "warn")
        finally:
            self.emit("checking", year, False)
        return state

    def repair(self, year, videos, state):
        """Fehlende Videos in eine schon (teilweise) gefüllte Playlist nachtragen und an die
        richtige Stelle schieben, Teil für Teil. Danach geht es mit dem normalen Füllen weiter."""
        self.act(L("REPAIR // OPENING BROWSER ...", "NACHTRAG // ÖFFNE BROWSER ..."), year)
        self.ensure_browser()
        order_all = [v["id"] for v in videos]
        pids = part_ids(state)
        new_next = 0
        try:
            for k, pid in enumerate(pids):
                order = order_all[k * PART_MAX:(k + 1) * PART_MAX]
                tag = f"{year_label(year)}" + (L(f" PART {k + 1}", f" TEIL {k + 1}") if len(pids) > 1 else "")

                def reading(n, tag=tag):
                    self.act(L(f"READING PLAYLIST // {n} READ", f"LESE PLAYLIST // {n} GELESEN"), year)
                self.emit("checking", year, True)
                self.log(L(f"REPAIR {tag} // READING THE PLAYLIST ...", f"NACHTRAG {tag} // LESE DIE PLAYLIST ..."))
                items = self.browser.list_items(pid, reading)
                self.emit("checking", year, False)
                self.log(L(f"REPAIR {tag} // PLAYLIST HAS {len(items)} VIDEOS", f"NACHTRAG {tag} // PLAYLIST HAT {len(items)} VIDEOS"))
                wanted = set(order_all)
                extras = [it for it in items if it["vid"] not in wanted]
                if is_list_key(year):
                    keep = []                                              # Listen enthalten genau die Treffer
                else:
                    shorts = known_shorts() or set()
                    keep = [it for it in extras if it["vid"] not in shorts]
                    extras = [it for it in extras if it["vid"] in shorts]     # nur Shorts werden entfernt
                if keep:
                    self.log(L(f"REPAIR {tag} // {len(keep)} VIDEO(S) NOT IN THE LIST // LEFT IN THE PLAYLIST",
                               f"NACHTRAG {tag} // {len(keep)} VIDEO(S) NICHT IN DER LISTE // BLEIBEN IN DER PLAYLIST"), "info")
                if extras and not REMOVE_EXTRAS:
                    self.log(L(f"REPAIR {tag} // {len(extras)} VIDEO(S) NOT IN THE LIST // LEFT IN THE PLAYLIST FOR NOW",
                               f"NACHTRAG {tag} // {len(extras)} VIDEO(S) NICHT IN DER LISTE // BLEIBEN VORERST IN DER PLAYLIST"), "warn")
                    extras = []
                if extras:
                    self.log(L(f"REPAIR {tag} // REMOVING {len(extras)} {'VIDEO(S)' if is_list_key(year) else 'SHORT(S)'}",
                               f"NACHTRAG {tag} // ENTFERNE {len(extras)} {'VIDEO(S)' if is_list_key(year) else 'SHORT(S)'}"), "warn")
                    for i in range(0, len(extras), BATCH_SIZE):
                        self.check()
                        self.browser.remove_many(pid, extras[i:i + BATCH_SIZE])
                        self.act(L(f"REPAIR // {min(i + BATCH_SIZE, len(extras))}/{len(extras)} REMOVED",
                                   f"NACHTRAG // {min(i + BATCH_SIZE, len(extras))}/{len(extras)} ENTFERNT"), year)
                        self.sleep(random.uniform(*BATCH_PAUSE), "NEXT BATCH")
                    items = [it for it in items if it["vid"] not in {x["vid"] for x in extras}]
                present = {it["vid"] for it in items}
                last = max((i for i, v in enumerate(order) if v in present), default=-1)
                missing = [v for v in order[:last + 1] if v not in present]
                if len(items) + len(missing) > PART_MAX:
                    self.log(L(f"REPAIR {tag} // PLAYLIST WOULD EXCEED {PART_MAX} VIDEOS // SOME VIDEOS ARE LEFT OUT",
                               f"NACHTRAG {tag} // PLAYLIST WÜRDE {PART_MAX} VIDEOS ÜBERSCHREITEN // EINIGE BLEIBEN DRAUSSEN"), "warn")
                    missing = missing[:max(0, PART_MAX - len(items))]
                self.log(L(f"REPAIR {tag} // {len(missing)} MISSING VIDEO(S) INSIDE THE PLAYLIST",
                           f"NACHTRAG {tag} // {len(missing)} FEHLENDE(S) VIDEO(S) MITTEN IN DER PLAYLIST"),
                         "warn" if missing else "info")
                have = len(present & set(order))
                base = k * PART_MAX

                def show(n, base=base):
                    state["next"] = base + n
                    save(state_file(year), state)
                    self.emit("progress", year)
                show(have)
                added = []
                for i in range(0, len(missing), BATCH_SIZE):
                    self.check()
                    chunk = missing[i:i + BATCH_SIZE]
                    if self.browser.add_many(pid, chunk):
                        added += chunk
                    else:
                        for v in chunk:
                            if self.browser.add(pid, v):
                                added.append(v)
                    n_done = min(i + len(chunk), len(missing))
                    show(have + len(added))
                    self.log(L(f"REPAIR {tag} // {n_done}/{len(missing)} ADDED",
                               f"NACHTRAG {tag} // {n_done}/{len(missing)} EINGEFÜGT"))
                    self.act(L(f"REPAIR // {n_done}/{len(missing)} ADDED", f"NACHTRAG // {n_done}/{len(missing)} EINGEFÜGT"), year)
                    if i + BATCH_SIZE < len(missing):
                        self.sleep(random.uniform(*BATCH_PAUSE), "NEXT BATCH")
                if added:
                    self.log(L(f"REPAIR {tag} // {len(added)} VIDEO(S) ADDED", f"NACHTRAG {tag} // {len(added)} VIDEO(S) EINGEFÜGT"), "ok")
                # Reihenfolge prüfen und alles verschieben, was falsch steht (auch nach einem Abbruch)
                self.emit("checking", year, True)
                self.log(L(f"REPAIR {tag} // CHECKING THE ORDER ...", f"NACHTRAG {tag} // PRÜFE DIE REIHENFOLGE ..."))
                items = self.browser.list_items(pid, reading)
                self.emit("checking", year, False)
                moves = plan_moves(order, items)
                if not moves:
                    self.log(L(f"REPAIR {tag} // ORDER IS CORRECT", f"NACHTRAG {tag} // REIHENFOLGE STIMMT"), "ok")
                else:
                    self.log(L(f"REPAIR {tag} // MOVING {len(moves)} VIDEO(S) TO THE RIGHT SPOT ...",
                               f"NACHTRAG {tag} // VERSCHIEBE {len(moves)} VIDEO(S) AN DIE RICHTIGE STELLE ..."))
                    self.act(L(f"SORTING // 0/{len(moves)}", f"SORTIERE // 0/{len(moves)}"), year)

                    def moved(n, tag=tag, total=len(moves)):
                        self.log(L(f"REPAIR {tag} // {n}/{total} MOVED", f"NACHTRAG {tag} // {n}/{total} VERSCHOBEN"))
                        self.act(L(f"SORTING // {n}/{total}", f"SORTIERE // {n}/{total}"), year)
                    self.browser.move_before(pid, moves, moved)
                    self.log(L(f"REPAIR {tag} // ORDER FIXED", f"NACHTRAG {tag} // REIHENFOLGE KORRIGIERT"), "ok")
                if last >= 0:
                    new_next = base + last + 1
            self.act(None, year)
            state["next"] = new_next
            state.pop("repair", None)
            save(state_file(year), state)
            self.emit("progress", year)
        finally:
            self.emit("checking", year, False)
            self.act(None, year)
        return state

    def mark_inflight(self, year, state, pid, start, ids):
        """Vor dem Senden merken, welches Paket unterwegs ist. Bricht etwas ab, wird nur dieses geprüft."""
        state["inflight"] = {"pid": pid, "start": start, "ids": ids}
        save(state_file(year), state)

    def in_playlist(self, pid, vid):
        """True/False, ob das Video in der Playlist ist; None = unbekannt."""
        if self.browser:
            r = self.browser.contains(pid, vid)
            if r is not None:
                return r
        if not quota_blocked("units"):
            try:
                d = self.call("GET", "playlistItems", {"part": "id", "playlistId": pid, "videoId": vid})
                return bool(d.get("items"))
            except (QuotaExceeded, ApiError):
                return None
        return None

    def resolve_inflight(self, year, state):
        """Schnelle Prüfung statt die ganze Playlist zu lesen: Ist das zuletzt gesendete Paket angekommen?"""
        inf = state.get("inflight")
        if not inf:
            return state
        ids, start = inf.get("ids") or [], inf.get("start", state.get("next", 0))
        self.log(L(f"{year_label(year)} // CHECKING THE LAST BATCH ({len(ids)} VIDEOS) ...", f"{year_label(year)} // PRÜFE DAS LETZTE PAKET ({len(ids)} VIDEOS) ..."))
        first = self.in_playlist(inf["pid"], ids[0]) if ids else False
        last = self.in_playlist(inf["pid"], ids[-1]) if ids else False
        if first is None or last is None:
            videos = load(videos_file(year), [])
            state.pop("inflight", None)
            return self.verify(year, videos, state, L("SYNC ON START", "ABGLEICH BEIM START"))
        if first and last:
            state["next"] = max(state.get("next", 0), start + len(ids))
            self.log(L(f"{year_label(year)} // LAST BATCH WAS ALREADY IN THE PLAYLIST", f"{year_label(year)} // LETZTES PAKET WAR SCHON DRIN"), "warn")
        elif first or last:
            videos = load(videos_file(year), [])
            state.pop("inflight", None)
            return self.verify(year, videos, state, L("SYNC ON START", "ABGLEICH BEIM START"))
        state.pop("inflight", None)
        save(state_file(year), state)
        self.emit("progress", year)
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
            if state.get("tracked"):
                self.resolve_inflight(year, state)
            else:
                self.verify(year, videos, state, L("AFTER STOP", "NACH STOPP"))
        except Exception as e:  # noqa: BLE001
            self.log(L(f"PLAYLIST CHECK FAILED // {e}", f"PLAYLIST-PRÜFUNG FEHLGESCHLAGEN // {e}"), "err")
        finally:
            self.ignore_stop = False

    def manual_check(self):
        for y in self.years:
            self.check()
            videos = load(videos_file(y), None)
            state = load(state_file(y), {})
            if videos is None or not state.get("playlist_id"):
                self.log(L(f"{year_label(y)} // NO PLAYLIST YET, NOTHING TO CHECK", f"{year_label(y)} // NOCH KEINE PLAYLIST, NICHTS ZU PRÜFEN"))
                continue
            self.emit("current", y)
            self.verify(y, videos, state, L("FULL SCAN", "KOMPLETT"))
        self.log(L("PLAYLIST CHECK COMPLETE", "PLAYLIST-PRÜFUNG FERTIG"), "ok")

    def fill(self, year, videos):
        self.scanning = None
        sf = state_file(year)
        state = self.ensure_playlist(year)
        self.cur = (year, videos)
        if state.get("repair"):
            if not self.fallback:
                self.log(L(f"{year_label(year)} // PLAYLIST NEEDS A REPAIR (MISSING VIDEOS / SHORTS TO REMOVE) // TURN ON FALLBACK FOR IT",
                           f"{year_label(year)} // PLAYLIST MUSS NACHGETRAGEN/BEREINIGT WERDEN // DAFÜR FALLBACK EINSCHALTEN"), "warn")
                return False
            state = self.repair(year, videos, state)
        if not state.get("tracked") and 0 < state.get("next", 0) < len(videos):
            # einmalig für ältere Stände: volle Prüfung, danach reicht die schnelle
            state = self.verify(year, videos, state, L("SYNC ON START", "ABGLEICH BEIM START"))
        state = self.resolve_inflight(year, state)
        state["tracked"] = True
        save(sf, state)
        i, added = state["next"], 0
        if i >= len(videos):
            self.log(L(f"{year_label(year)} ALREADY COMPLETE", f"{year_label(year)} IST SCHON FERTIG"))
            return True
        while i < len(videos):
            if self.limit and added >= self.limit:
                self.log(L(f"{year_label(year)} // RUN LIMIT OF {self.limit} VIDEOS REACHED", f"{year_label(year)} // LIMIT VON {self.limit} VIDEOS FÜR DIESEN LAUF ERREICHT"))
                return False
            self.check()
            pid = self.ensure_part(year, state, i // PART_MAX)     # ab 5.000 Videos: nächster Teil
            if self.mode == "browser":
                n = min(BATCH_SIZE, len(videos) - i, PART_MAX - i % PART_MAX)
                if self.limit:
                    n = min(n, self.limit - added)
                chunk = [v["id"] for v in videos[i:i + n]]
                self.mark_inflight(year, state, pid, i, chunk)
                results = self.add_batch(pid, chunk)
            else:
                chunk = [videos[i]["id"]]
                self.mark_inflight(year, state, pid, i, chunk)
                results = [self.add_video(pid, chunk[0])]   # kann auf Browser umschalten
            for vid, ok in zip(chunk, results):
                if ok:
                    added += 1
                else:
                    state.setdefault("skipped", []).append(vid)
            i += len(chunk)
            state["next"] = i
            state.pop("inflight", None)
            save(sf, state)
            self.emit("progress", year)
            if i < len(videos):
                self.pace()
        state = self.verify(year, videos, state, L("FINAL FULL SCAN", "ABSCHLUSSPRÜFUNG"))
        links = " // ".join(f"https://www.youtube.com/playlist?list={p}" for p in part_ids(state))
        self.log(L(f"{year_label(year)} COMPLETE // {links}", f"{year_label(year)} FERTIG // {links}"), "ok")
        return True


# ============================================================ UI
# Neon-Palette
RED, RED_MILD, RED_DARK = "#FF6159", "#AE3B36", "#431618"
CYAN, CYAN_HOT, CYAN_FAINT = "#5EF6FF", "#28FFFF", "#172C2E"
YELLOW, GREEN = "#FFD741", "#1DED83"
NEUTRAL = "#7F92AE"   # gedämpftes Blaugrau für "noch nicht angefangen"
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
        "faded": (0.28, 0.05, False),     # fertige Jahre: in den Hintergrund
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
            gw = max(1, px(2)) if self.st == "running" else 1      # laufende Karte: kräftigerer Glow
            for d, a in ((1, 0.65), (2, 0.35), (3, 0.16)):
                o = shape(x0 - d, y0 - d, x1 + d, y1 + d, c + 0.586 * d)
                self.create_polygon(flat(o), fill="", outline=mix(role, BG, a), width=gw, tags=("glow", f"glow{d}"))
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
        return f"{L('PAUSED', 'PAUSIERT')} // {fmt_secs(now - p['t'])}"
    return f"{wait_label(p['label'])} // {fmt_secs(p['t'] - now)}"


def wait_label(label):
    """Interne Wartezeit-Namen in die Anzeigesprache übersetzen."""
    return L(label, {"NEXT BATCH": "NÄCHSTES PAKET", "COOLDOWN": "KURZE PAUSE", "BREAK": "LANGE PAUSE",
                     "RATE LIMIT": "YOUTUBE BREMST", "RETRY": "NEUER VERSUCH",
                     "NO SIGNAL": "KEINE VERBINDUNG"}.get(label, label))


class YearRow(Panel):
    def __init__(self, master, year, command, pause_command=None):
        super().__init__(master, CARD_H, cut=10, command=command)
        self.pause_command = pause_command
        self.icon_geo = None       # (x0, y0, x1, y1) des Symbols, klickbar während des Laufs
        self.year = year
        self.selected = False
        self.running = False
        self.checking = False
        self.pause = None          # None | {"kind": "manual", "t": start} | {"kind": "wait", "t": ende, "label": ..}
        self.info = year_info(year)
        self.blocked = None        # Reset-Zeit, wenn das Jahr erst nach dem API-Reset weitergeht
        self.activity = None       # was gerade passiert (nur auf der laufenden Karte)
        self.role = status_role(self.info["code"])
        self.geo = None
        self.last_label = None

    def palette(self):
        # laufende Karte leuchtet in ihrer Zustandsfarbe statt pauschal grün
        _, edge, tint, glow = super().palette()
        return self.role, edge, tint, glow

    def status_text(self):
        code, run = self.info["code"], self.running
        if run and self.pause:
            return pause_label(self.pause, time.time()), False
        if self.activity:
            return self.activity, False      # ruhig, kein Blinken (ändert sich ohnehin laufend)
        if self.checking:
            return L("CHECKING PLAYLIST...", "PLAYLIST WIRD GEPRÜFT..."), True
        if self.blocked and not run:
            t = self.blocked.astimezone().strftime("%H:%M")
            return L(f"NEEDS API // FROM {t}", f"BRAUCHT API // AB {t}"), False
        if is_list_key(self.year) and code in ("none", "scanning"):
            return (L("MATCHING...", "TREFFER WERDEN GESUCHT...") if run
                    else L("NEW LIST // FILLS ON START", "NEUE LISTE // WIRD BEIM START GEFÜLLT")), run
        if code in ("none", "scanning"):
            return (L("SCANNING...", "SUCHE LÄUFT...") if run else L("SCAN PAUSED", "SUCHE PAUSIERT")
                    if code == "scanning" else L("NOT SCANNED", "NICHT GESUCHT")), run
        if code == "done":
            if run:
                return L("SYNCING...", "WIRD ABGEGLICHEN..."), False     # läuft noch (z. B. Nachtrag), nicht schon "fertig"
            return L("COMPLETE", "FERTIG"), False
        if run:
            return L("UPLINK ACTIVE", "WIRD GEFÜLLT"), False
        if code == "progress":
            return L("IN PROGRESS", "IN ARBEIT"), False
        return f"{L('READY', 'BEREIT')} // {self.info['total']} VIDEOS", False

    def draw_content(self, x0, y0, x1, y1, c, role):
        on = self.selected or self.running
        faded = self.st == "faded"
        h = y1 - y0
        cy = (y0 + y1) / 2
        isz = px(32)
        ix = x0 + px(10)
        self.icon_box(ix, cy, isz, role, on)
        u = isz / 32
        icol = role if on else mix(role, BG, 0.5 if faded else 0.8)
        top = cy - isz / 2
        self.icon_geo = (ix - px(4), top - px(4), ix + isz + px(4), top + isz + px(4))
        manual = bool(self.pause and self.pause.get("kind") == "manual")
        if self.running and not manual:
            # läuft: Pause-Symbol, Klick pausiert
            for xx in (11, 18):
                self.create_rectangle(ix + xx * u, top + 9 * u, ix + (xx + 4) * u, top + 23 * u,
                                      fill=icol, outline="")
        elif self.running:
            # pausiert: Play-Symbol, Klick setzt fort
            self.create_polygon(ix + 11 * u, top + 8 * u, ix + 11 * u, top + 24 * u, ix + 24 * u, top + 16 * u,
                                fill=icol, outline=icol)
        elif self.info["code"] == "done":
            # fertig: Haken
            self.create_line(ix + 9 * u, top + 16 * u, ix + 14 * u, top + 21 * u, ix + 23 * u, top + 11 * u,
                             fill=icol, width=max(2, px(2.5)), capstyle="round", joinstyle="round")
        else:
            for yy, xe in ((9, 20), (14, 20), (19, 15)):
                self.create_line(ix + 7 * u, top + yy * u, ix + xe * u, top + yy * u, fill=icol,
                                 width=max(1, px(1.5)))
            self.create_polygon(ix + 18 * u, top + 17 * u, ix + 18 * u, top + 25 * u, ix + 25 * u, top + 21 * u,
                                fill=icol, outline=icol)

        tx = self.right_tag(x0, y0, x1, y1, c, px(116), role, on)
        nx = ix + isz + px(12)
        self.create_text(nx, y0 + h * 0.32, text=year_label(self.year), anchor="w", font=F("ui", 16),
                         fill=TEXT if on else mix(TEXT_DIM, BG, 0.5) if faded else mix(TEXT_DIM, BG, 0.75))
        text, blinking = self.status_text()
        scol = (CYAN if self.activity else
                (RED if on else mix(RED, BG, 0.55)) if (self.blocked and not self.running) else role if on
                else mix(role, BG, 0.55) if faded else mix(TEXT_MUTED, BG, 0.65))
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
            self.create_rectangle(bx0, by, max(bx0, fx), by + px(3),
                                  fill=role if on else mix(role, BG, 0.4 if faded else 0.75),
                                  outline="", tags="bar", state="normal" if done else "hidden")
            self.geo.update(bx0=bx0, bx1=bx1, by=by, fx=fx)
            pct = done * 100 / total
            ptxt = "100 %" if done >= total else f"{pct:.1f} %"
            self.create_text(cx, mid - px(9), text=ptxt, font=F("mono", 17),
                             fill=(TEXT_DIM if role == NEUTRAL else role) if on else mix(role, BG, 0.5) if faded
                             else mix(TEXT_MUTED, BG, 0.6), tags="pct")
            self.create_text(cx, mid + px(11), text=f"{done} / {total}", font=F("mono", 12),
                             fill=TEXT if on else mix(TEXT_MUTED, BG, 0.45) if faded else mix(TEXT_MUTED, BG, 0.6),
                             tags="cnt")
        elif self.blocked and not self.running:
            self.create_text(cx, mid, text="API", font=F("mono", 15), fill=RED if on else mix(RED, BG, 0.55))
        else:
            self.create_text(cx, mid, text="--", font=F("mono", 15), fill=mix(TEXT_MUTED, BG, 0.6))

        manual_pause = self.pause and self.pause.get("kind") == "manual"
        if self.running and self.geo.get("fx") and not manual_pause:
            # Datenpaket auf dem Ladebalken
            self.create_rectangle(0, 0, 0, 0, fill=mix("#ffffff", role, 0.6), outline="", tags="pkt")
        if self.running:
            self.tick(time.time())

    # --- Animation der aktiven Karte: nur Ladebalken + ruhiger Glow
    def _click(self, e):
        if getattr(self, "dragging", False):
            return
        g = self.icon_geo
        if (self.running and self.pause_command and g
                and g[0] <= e.x <= g[2] and g[1] <= e.y <= g[3]):
            self.pause_command()
            return
        super()._click(e)

    def tick(self, now):
        g = self.geo
        if not (self.running and g):
            return
        role = g["role"]
        # Glow: 4 s, weiches Ein- und Ausblenden
        pulse = 0.5 - 0.5 * math.cos(now * 2 * math.pi / 3.0)
        for d, a in ((1, 1.0), (2, 0.7), (3, 0.4)):
            self.itemconfigure(f"glow{d}", outline=mix(role, BG, a * (0.25 + 0.75 * pulse)))
        self.itemconfigure("edge", outline=mix("#ffffff", role, 0.35 * pulse))
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

    def set_activity(self, text):
        if text == self.activity:
            return
        had = self.activity
        self.activity = text
        if had and text and self.find_withtag("stat") and not (self.running and self.pause):
            # nur den Text tauschen statt alles neu zu zeichnen (sonst flackert es)
            self.itemconfigure("stat", text=text)
            self.last_label = text
        else:
            self.redraw()

    def refresh_blocked(self):
        b = None if self.running or is_list_key(self.year) else year_blocked(self.info)
        if b != self.blocked:
            self.refresh()

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
                self.activity = None
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
        self.blocked = None if self.running or is_list_key(self.year) else year_blocked(self.info)
        self.role = status_role(self.info["code"], self.running)
        faded = self.info["code"] == "done" and not self.selected and not self.running
        self.st = ("active" if manual else "running" if self.running else "active" if self.selected
                   else "faded" if faded else "inactive")
        if faded:
            self.role = mix(self.role, BG, 0.6)
        self.redraw()


class ToggleCard(Panel):
    def __init__(self, master, name, sub, value, command, available=True, height=CARD_H, compact=False):
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
        sub = self.sub if self.available else L("UNAVAILABLE", "NICHT VERFÜGBAR")
        self.create_text(nx, y0 + h * 0.66, text=fit_text(sub, fs, maxw), anchor="w",
                         font=fs, fill=role if on else TEXT_MUTED)
        mid = (y0 + y1 - c / 2) / 2
        tag = (L("ON", "AN") if self.value else L("OFF", "AUS")) if self.available else "--"
        self.create_text((tx + x1) / 2, mid, text=tag, font=F("mono", 13),
                         fill=role if on else TEXT_MUTED)


class StatusChip(Panel):
    def __init__(self, master):
        super().__init__(master, 36, cut=8)
        self.text = "STANDBY"
        self.blinking = False

    def show(self, text, role, state, blinking=False):
        same = (role, state, blinking) == (self.role, self.st, self.blinking) and self.find_withtag("ctext")
        self.text, self.role, self.st, self.blinking = text, role, state, blinking
        prev = getattr(self, "_last_text", "")
        self._last_text = text
        self._prev_len = len(prev)
        if same and len(text) == self._prev_len:
            self.itemconfigure("ctext", text=text)   # nur Text tauschen (z. B. Countdown), kein Neuzeichnen
        else:
            self.redraw()

    def draw_content(self, x0, y0, x1, y1, c, role):
        on = self.st in ("active", "running", "frame")
        col = role if on else TEXT_MUTED
        fl = self.create_text(x0 + px(12), (y0 + y1) / 2, text="SYS //", anchor="w",
                              font=F("mono", 10, False), fill=FLUFF, tags="fluff")
        left = self.bbox(fl)[2] + px(8)
        avail = x1 - c - left - px(4)
        if tkfont.Font(font=F("mono", 12)).measure(self.text) > avail:
            self.delete("fluff")             # lange Texte: "SYS //" weglassen, Platz für den Text
            left = x0 + px(8)
            avail = x1 - c - left - px(4)
        size = 12
        while size > 9 and tkfont.Font(font=F("mono", size)).measure(self.text) > avail:
            size -= 1
        self.fit_size = size
        item = self.create_text((left + x1 - c) / 2, (y0 + y1) / 2, text=self.text, font=F("mono", size),
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
        super().__init__(master, DECK_H, state="frame", cut=16)
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
        isz = px(38)
        ix = x0 + px(20)
        icx = ix + isz / 2
        self.create_rectangle(ix, cy - isz / 2, ix + isz, cy + isz / 2, fill=mix(RED, BG, 0.12),
                              outline=mix(RED, BG, 0.7))
        self.create_text(icx, cy, text="@", font=F("mono", 22), fill=RED)
        # EDIT-Bereich (cyan = klickbar)
        tw = px(116)            # so breit wie der Prozentblock der Jahreskarten
        tx = x1 - tw
        pts = [(tx, y0 + 1), (x1 - 1, y0 + 1), (x1 - 1, y1 - c), (x1 - c, y1 - 1), (tx, y1 - 1)]
        gradient(self, pts, tx, y0, x1, y1, YELLOW, 0.30 if self.edit_hover else 0.16, steps=6)
        self.create_polygon(flat(pts), fill="", outline=mix(YELLOW, BG, 1.0 if self.edit_hover else 0.75),
                            tags="edit")
        mid = (y0 + y1 - c / 2) / 2
        self.create_text((tx + x1) / 2, mid, text=L("EDIT ›", "ÄNDERN ›"), font=F("mono", 14),
                         fill=mix("#ffffff", YELLOW, 0.3) if self.edit_hover else YELLOW, tags="edit")
        self.create_rectangle(tx, y0 + 1, x1 - 1, y1 - 1, fill="", outline="", tags=("edit", "edithit"))
        self.tag_bind("edit", "<Enter>", lambda e: self._edit_hover(True))
        self.tag_bind("edit", "<Leave>", lambda e: self._edit_hover(False))
        self.tag_bind("edit", "<ButtonRelease-1>", lambda e: self.on_edit())
        # Texte
        nx = ix + isz + px(16)
        self.create_text(nx, y0 + h * 0.22, text=L("TARGET LOCKED", "ZIEL ERFASST") if has else L("NO TARGET", "KEIN ZIEL"), anchor="w",
                         font=F("mono", 10), fill=RED)
        if tx - nx > px(260):
            self.create_text(tx - px(12), y0 + h * 0.22, text="CH.ID " + (CHANNEL.get("id", "")[-6:] or "------"),
                             anchor="e", font=F("mono", 10, False), fill=FLUFF)
        maxw = tx - nx - px(12)
        name = channel_name().upper() if has else L("SET A YOUTUBE CHANNEL", "YOUTUBE-KANAL WÄHLEN")
        self.create_text(nx, y0 + h * 0.50, text=fit_text(name, F("ui", 21), maxw), anchor="w",
                         font=F("ui", 21), fill=TEXT if has else TEXT_DIM)
        if self.resolving:
            sub = L("RESOLVING...", "WIRD GESUCHT...")
        elif has:
            handle = (CHANNEL.get("handle") or CHANNEL.get("id", "")).upper()
            options = [handle]
            if YEARS:
                options = [f"{handle} // {L('ARCHIVE', 'ARCHIV')} {YEARS[0]}-{YEARS[-1]} // {len(YEARS)} {L('YEARS', 'JAHRE')}",
                           f"{handle} // {YEARS[0]}-{YEARS[-1]}", handle]
            f11 = tkfont.Font(font=F("mono", 11))
            sub = next((o for o in options if f11.measure(o) <= maxw), options[-1])
        else:
            sub = L("@HANDLE, URL OR CHANNEL ID", "@HANDLE, LINK ODER KANAL-ID")
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
        CutButton(row, L("SAVE", "SPEICHERN"), self.save, width=120).pack(side="left")
        CutButton(row, L("CANCEL", "ABBRECHEN"), self.destroy, width=120).pack(side="left", padx=(px(8), 0))
        self.bind("<Return>", lambda e: self.save())
        self.bind("<Escape>", lambda e: self.destroy())
        style_window(self)
        self.update_idletasks()
        x = master.winfo_rootx() + (master.winfo_width() - self.winfo_width()) // 2
        y = master.winfo_rooty() + (master.winfo_height() - self.winfo_height()) // 3
        self.geometry(f"+{max(0, x)}+{max(0, y)}")
        self.grab_set()
        self.focus_force()

    def save(self):
        if self.on_save({k: v.get().strip() for k, v in self.vars.items()}) is not False:
            self.destroy()


class SettingsDialog(tk.Toplevel):
    """Playlist-Einstellungen: Titel, Beschreibung, Sichtbarkeit, Thumbnails pro Jahr."""

    CHIPS = (("CHANNEL", "{channel}"), ("HANDLE", "{handle}"), ("YEAR", "{year}"), ("COUNT", "{count}"))

    def __init__(self, app):
        super().__init__(app, bg=BG)
        self.app = app
        self.title(L("Playlist settings", "Playlist-Einstellungen"))
        self.transient(app)
        self.minsize(px(640), px(560))
        self.geometry(f"{px(720)}x{px(800)}")
        self.focus_field = None
        pad = px(18)
        st = app.settings
        sample = (app.selected_years() or YEARS or [datetime.date.today().year - 1])[0]
        self.sample_year = sample

        row = tk.Frame(self, bg=BG)
        row.pack(side="bottom", fill="x", padx=pad, pady=pad)
        CutButton(row, L("SAVE", "SPEICHERN"), self.save, width=120).pack(side="left")
        CutButton(row, L("CANCEL", "ABBRECHEN"), self.destroy, width=120).pack(side="left", padx=(px(8), 0))
        CutButton(row, L("RESET TO DEFAULT", "STANDARDWERTE"), self.reset, width=170).pack(side="right")
        CutButton(row, L("LEFT OUT", "AUSSORTIERT"), lambda: app.show_removed(owner=self), width=140).pack(
            side="right", padx=(0, px(8)))
        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=pad, pady=(pad, 0))

        Section(body, L("PLAYLIST SETTINGS", "PLAYLIST-EINSTELLUNGEN"), "TEMPLATE // VISIBILITY").pack(fill="x", pady=(0, px(10)))
        self.lbl(body, L("TITLE", "TITEL"))
        self.title_var = tk.StringVar(value=st.get("playlist_template") or default_playlist_template())
        self.title_entry = self.entry(body, self.title_var)
        self.title_entry.bind("<FocusIn>", lambda e: setattr(self, "focus_field", self.title_entry))
        self.focus_field = self.title_entry

        chips = tk.Frame(body, bg=BG)
        chips.pack(fill="x", pady=(px(2), px(6)))
        tk.Label(chips, text=L("INSERT:", "EINFÜGEN:"), bg=BG, fg=TEXT_MUTED, font=F("mono", 10)).pack(side="left", padx=(0, px(6)))
        tips = {"{channel}": L("channel name", "Name des Kanals"), "{handle}": L("@handle of the channel", "@Handle des Kanals"),
                "{year}": L("year of the playlist (name of the list for custom lists)", "Jahr der Playlist (bei eigenen Listen der Listenname)"),
                "{count}": L("number of videos in the playlist", "Anzahl der Videos in der Playlist")}
        chip_names = {"{channel}": L("CHANNEL", "KANAL"), "{handle}": "HANDLE", "{year}": L("YEAR", "JAHR"),
                      "{count}": L("COUNT", "ANZAHL")}
        for label, tok in self.CHIPS:
            label = chip_names[tok]
            b = CutButton(chips, label, lambda t=tok: self.insert(t))
            b.pack(side="left", padx=(0, px(6)))
            Tooltip(b, L(f"Inserts {tok}: {tips[tok]}.", f"Fügt {tok} ein: {tips[tok]}."))

        self.lbl(body, L("DESCRIPTION (OPTIONAL, LEAVE EMPTY FOR NONE)", "BESCHREIBUNG (OPTIONAL, LEER = KEINE)"))
        self.desc = tk.Text(body, height=4, wrap="word", bg=BG2, fg=CYAN, insertbackground=CYAN, relief="flat",
                            font=F("mono", 12, False), highlightthickness=1, highlightcolor=CYAN,
                            highlightbackground=mix(CYAN, BG, 0.4), selectbackground=mix(CYAN, BG, 0.3),
                            padx=px(6), pady=px(6))
        self.desc.insert("1.0", st.get("description_template", default_description_template()))
        self.desc.pack(fill="x", pady=(px(4), px(8)))
        self.desc.bind("<FocusIn>", lambda e: setattr(self, "focus_field", self.desc))
        self.desc.bind("<KeyRelease>", lambda e: self.preview())
        self.title_var.trace_add("write", lambda *a: self.preview())

        self.prev = tk.Label(body, text="", bg=BG, fg=TEXT, font=F("mono", 11, False), justify="left", anchor="w",
                             wraplength=px(640))
        self.prev.pack(fill="x", pady=(0, px(8)))

        self.lbl(body, L("VISIBILITY", "SICHTBARKEIT"))
        vis = tk.Frame(body, bg=BG)
        vis.pack(fill="x", pady=(px(4), px(4)))
        self.privacy = st.get("privacy", "public")
        self.vis_btns = {}
        for opt in PRIVACY_OPTIONS:
            b = CutButton(vis, privacy_label(opt), lambda o=opt: self.set_privacy(o), width=150)
            b.pack(side="left", padx=(0, px(6)))
            self.vis_btns[opt] = b
        Tooltip(self.vis_btns["public"], L("Anyone can find and watch the playlist.", "Jeder kann die Playlist finden und ansehen."))
        Tooltip(self.vis_btns["unlisted"], L("Only people with the link can see the playlist.", "Nur Leute mit dem Link können die Playlist sehen."))
        Tooltip(self.vis_btns["private"], L("Only you can see the playlist.", "Nur du kannst die Playlist sehen."))
        self.set_privacy(self.privacy)
        tk.Label(body, text=L("TITLE, DESCRIPTION AND VISIBILITY APPLY TO PLAYLISTS CREATED FROM NOW ON.",
                              "TITEL, BESCHREIBUNG UND SICHTBARKEIT GELTEN FÜR PLAYLISTS, DIE AB JETZT ANGELEGT WERDEN."),
                 bg=BG, fg=FLUFF, font=F("mono", 10, False), anchor="w").pack(fill="x", pady=(px(2), px(12)))

        Section(body, L("THUMBNAILS (OPTIONAL, EXPERIMENTAL)", "THUMBNAILS (OPTIONAL, EXPERIMENTELL)"), "JPG/PNG // MAX 2 MB").pack(fill="x", pady=(0, px(6)))
        tk.Label(body, text=L("PICK AN IMAGE PER YEAR OR CUSTOM LIST, ALSO FOR PLAYLISTS THAT DON'T EXIST YET. IT IS UPLOADED VIA "
                              "THE API (50 UNITS) AS SOON AS THE PLAYLIST EXISTS AND QUOTA IS AVAILABLE. "
                              "RECOMMENDED: 1280×720.",
                              "WÄHLE PRO JAHR ODER EIGENE LISTE EIN BILD, AUCH FÜR PLAYLISTS, DIE ES NOCH NICHT GIBT. ES WIRD ÜBER DIE "
                              "API HOCHGELADEN (50 EINHEITEN), SOBALD DIE PLAYLIST EXISTIERT UND KONTINGENT DA IST. "
                              "EMPFOHLEN: 1280×720."),
                 bg=BG, fg=TEXT_MUTED, font=F("mono", 10, False), justify="left", anchor="w",
                 wraplength=px(660)).pack(fill="x", pady=(0, px(6)))
        wrap = tk.Frame(body, bg=BG)
        wrap.pack(fill="both", expand=True)
        self.thumb_area = ScrollArea(wrap)
        self.thumb_area.pack(fill="both", expand=True)
        self.thumbs = load(thumbs_file(), {})
        self.pending = {}      # jahr -> neuer Dateipfad oder None (= entfernen)
        self.thumb_rows = {}
        for y in YEARS:
            self.build_thumb_row(y)
        if load_lists():
            tk.Label(self.thumb_area.inner, text=L("CUSTOM LISTS", "EIGENE LISTEN"), bg=BG, fg=RED, font=F("mono", 11),
                     anchor="w").pack(fill="x", pady=(px(10), px(4)))
            for lst in load_lists():
                self.build_thumb_row(list_key(lst), editable=True)

        self.bind("<Escape>", lambda e: self.destroy())
        self.preview()
        style_window(self)
        self.update_idletasks()
        x = app.winfo_rootx() + (app.winfo_width() - self.winfo_width()) // 2
        y = app.winfo_rooty() + max(0, (app.winfo_height() - self.winfo_height()) // 3)
        self.geometry(f"+{max(0, x)}+{max(0, y)}")
        self.grab_set()
        self.focus_force()

    # --- Bausteine
    def lbl(self, parent, text):
        tk.Label(parent, text=text, bg=BG, fg=TEXT_MUTED, font=F("mono", 11), anchor="w").pack(fill="x")

    def entry(self, parent, var):
        e = tk.Entry(parent, textvariable=var, bg=BG2, fg=CYAN, insertbackground=CYAN, relief="flat",
                     font=F("mono", 13, False), highlightthickness=1, highlightcolor=CYAN,
                     highlightbackground=mix(CYAN, BG, 0.4), selectbackground=mix(CYAN, BG, 0.3))
        e.pack(fill="x", pady=(px(4), 0), ipady=px(6))
        return e

    def insert(self, tok):
        w = self.focus_field or self.title_entry
        w.insert("insert", tok)
        w.focus_set()
        self.preview()

    def set_privacy(self, opt):
        self.privacy = opt
        for o, b in self.vis_btns.items():
            b.set_color(GREEN if o == opt else YELLOW)
            b.set_text(("● " if o == opt else "") + privacy_label(o))

    def preview(self):
        t, d = self.title_var.get(), self.desc.get("1.0", "end").strip()
        ok_t, ok_d = _template_ok(t), _template_ok(d) if d else True
        y = self.sample_year
        lines = [f"{L('PREVIEW', 'VORSCHAU')} {year_label(y)}: " + (fill_template(t, y) if ok_t else L("INVALID PLACEHOLDER IN TITLE", "UNGÜLTIGER PLATZHALTER IM TITEL"))]
        if d:
            short = fill_template(d, y) if ok_d else L("INVALID PLACEHOLDER IN DESCRIPTION", "UNGÜLTIGER PLATZHALTER IN DER BESCHREIBUNG")
            lines.append(L("DESCRIPTION: ", "BESCHREIBUNG: ") + (short if len(short) < 140 else short[:137] + "..."))
        else:
            lines.append(L("DESCRIPTION: (NONE)", "BESCHREIBUNG: (KEINE)"))
        self.prev.configure(text="\n".join(lines), fg=TEXT if ok_t and ok_d else RED)
        return ok_t and ok_d

    def thumb_state(self, y):
        if y in self.pending:
            return ("NEW", Path(self.pending[y]).name) if self.pending[y] else ("REMOVE", "")
        t = self.thumbs.get(str(y))
        if not t:
            return ("NONE", "")
        status = {"queued": "QUEUED", "done": "UPLOADED", "error": "ERROR"}.get(t.get("status"), "?")
        return (status, t.get("file", ""))

    def build_thumb_row(self, y, editable=False):
        fr = tk.Frame(self.thumb_area.inner, bg=BG)
        fr.pack(fill="x", pady=(0, px(4)))
        name = year_label(y)
        tk.Label(fr, text=name if len(name) <= 18 else name[:17] + "…", bg=BG, fg=TEXT, font=F("ui", 14),
                 width=18 if editable else 6, anchor="w").pack(side="left")
        # Knöpfe zuerst packen, damit bei langem Text die Statuszeile gekürzt wird und nicht die Knöpfe
        rm = CutButton(fr, L("REMOVE", "ENTFERNEN"), lambda: self.remove_thumb(y), width=110)
        rm.pack(side="right")
        CutButton(fr, L("CHOOSE…", "AUSWÄHLEN…"), lambda: self.choose_thumb(y), width=120).pack(side="right", padx=(0, px(6)))
        if editable:
            CutButton(fr, L("EDIT LIST", "LISTE ÄNDERN"), lambda: ListDialog(self.app, y, parent=self),
                      width=140).pack(side="right", padx=(0, px(6)))
        info = tk.Label(fr, text="", bg=BG, fg=TEXT_MUTED, font=F("mono", 11, False), anchor="w")
        info.pack(side="left", fill="x", expand=True)
        self.thumb_area.wheel(fr)
        self.thumb_rows[y] = (info, rm)
        self.refresh_thumb_row(y)

    def refresh_thumb_row(self, y):
        info, rm = self.thumb_rows[y]
        status, name = self.thumb_state(y)
        col = {"UPLOADED": GREEN, "QUEUED": YELLOW, "NEW": CYAN, "ERROR": RED, "REMOVE": RED}.get(status, TEXT_MUTED)
        shown = {"NEW": L("NEW", "NEU"), "REMOVE": L("REMOVE", "WIRD ENTFERNT"), "NONE": L("NONE", "KEINS"),
                 "QUEUED": L("QUEUED", "WARTET"), "UPLOADED": L("UPLOADED", "HOCHGELADEN"),
                 "ERROR": L("ERROR", "FEHLER")}.get(status, status)
        if len(name) > 26:
            name = name[:12] + "…" + name[-12:]
        txt = f"{shown}" + (f" // {name}" if name else "")
        t = self.thumbs.get(str(y)) or {}
        if status == "ERROR" and t.get("error"):
            txt += f" // {t['error'][:60]}"
        info.configure(text=txt, fg=col)
        rm.set_enabled(status not in ("NONE", "REMOVE"))

    def choose_thumb(self, y):
        f = filedialog.askopenfilename(parent=self, title=f"Thumbnail {year_label(y)}",
                                       filetypes=[(L("Images", "Bilder"), "*.jpg *.jpeg *.png"),
                                                  (L("All files", "Alle Dateien"), "*.*")])
        if not f:
            return
        p_ = Path(f)
        if p_.suffix.lower() not in (".jpg", ".jpeg", ".png"):
            self.app.write_log(L(f"THUMBNAIL {year_label(y)} // ONLY JPG OR PNG", f"THUMBNAIL {year_label(y)} // NUR JPG ODER PNG"), "err")
            return
        if p_.stat().st_size > THUMB_MAX_BYTES:
            self.app.write_log(L(f"THUMBNAIL {year_label(y)} // FILE TOO LARGE (MAX 2 MB)", f"THUMBNAIL {year_label(y)} // DATEI ZU GROSS (MAX 2 MB)"), "err")
            return
        self.pending[y] = str(p_)
        self.refresh_thumb_row(y)

    def remove_thumb(self, y):
        self.pending[y] = None
        self.refresh_thumb_row(y)

    def reset(self):
        self.title_var.set(default_playlist_template())
        self.desc.delete("1.0", "end")
        self.desc.insert("1.0", default_description_template())
        self.set_privacy("public")
        self.preview()

    def save(self):
        if not self.preview():
            self.app.write_log(L("SETTINGS NOT SAVED // INVALID PLACEHOLDER (USE {channel} {handle} {year} {count})",
                                 "NICHT GESPEICHERT // UNGÜLTIGER PLATZHALTER (ERLAUBT: {channel} {handle} {year} {count})"),
                               "err")
            return
        st = self.app.settings
        st["playlist_template"] = self.title_var.get().strip() or default_playlist_template()
        st["description_template"] = self.desc.get("1.0", "end").strip()
        st["privacy"] = self.privacy
        save(SETTINGS_FILE, st)
        apply_templates(st)
        # Thumbnails übernehmen (Kopie im Kanal-Ordner)
        if self.pending:
            thumbs_dir().mkdir(parents=True, exist_ok=True)
            for y, src in self.pending.items():
                old = self.thumbs.pop(str(y), None)
                if old and old.get("file") and (thumbs_dir() / old["file"]).exists():
                    try:
                        (thumbs_dir() / old["file"]).unlink()
                    except OSError:
                        pass
                if src:
                    name = f"{y}{Path(src).suffix.lower()}"
                    shutil.copyfile(src, thumbs_dir() / name)
                    self.thumbs[str(y)] = {"file": name, "status": "queued", "error": ""}
            save(thumbs_file(), self.thumbs)
        self.app.write_log(L("PLAYLIST SETTINGS SAVED", "PLAYLIST-EINSTELLUNGEN GESPEICHERT"), "ok")
        self.app.settings_card.redraw()
        self.destroy()


def privacy_label(opt):
    return {"public": L("PUBLIC", "ÖFFENTLICH"), "unlisted": L("UNLISTED", "NICHT GELISTET"),
            "private": L("PRIVATE", "PRIVAT")}.get(opt, opt.upper())


class SettingsCard(Panel):
    """Kompakte Karte unter den Optionen. name/sub sind Funktionen, damit sie immer aktuell sind."""

    def __init__(self, master, command, name, sub, icon="lines"):
        super().__init__(master, CARD_H, command=command)
        self.name_fn, self.sub_fn, self.icon = name, sub, icon

    def draw_content(self, x0, y0, x1, y1, c, role):
        h = y1 - y0
        cy = (y0 + y1) / 2
        isz = px(24)
        ix = x0 + px(8)
        self.icon_box(ix, cy, isz, role, False)
        u = isz / 24
        top = cy - isz / 2
        col = mix(role, BG, 0.8)
        if self.icon == "gear":
            r = 5 * u
            self.create_oval(ix + 12 * u - r, top + 12 * u - r, ix + 12 * u + r, top + 12 * u + r,
                             outline=col, width=max(1, px(1.5)))
            for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0)):
                self.create_line(ix + (12 + dx * 6) * u, top + (12 + dy * 6) * u,
                                 ix + (12 + dx * 9) * u, top + (12 + dy * 9) * u, fill=col, width=max(1, px(2)))
        else:
            for yy in (8, 12, 16):
                self.create_line(ix + 6 * u, top + yy * u, ix + 18 * u, top + yy * u, fill=col, width=max(1, px(1.5)))
        tx = self.right_tag(x0, y0, x1, y1, c, px(46), YELLOW, False)
        nx = ix + isz + px(10)
        maxw = tx - nx - px(6)
        self.create_text(nx, y0 + h * 0.34, text=fit_text(self.name_fn(), F("ui", 14), maxw), anchor="w",
                         font=F("ui", 14), fill=TEXT_DIM)
        self.create_text(nx, y0 + h * 0.66, text=fit_text(self.sub_fn(), F("mono", 10), maxw), anchor="w",
                         font=F("mono", 10), fill=TEXT_MUTED)
        self.create_text((tx + x1) / 2, (y0 + y1 - c / 2) / 2, text="›", font=F("mono", 18), fill=YELLOW)


class UpdateDialog(tk.Toplevel):
    """Rückfrage vor dem Update."""

    def __init__(self, app, version, url):
        super().__init__(app, bg=BG)
        self.app = app
        self.title("Update")
        self.transient(app)
        self.resizable(False, False)
        pad = px(18)
        Section(self, f"UPDATE // v{version}", "GITHUB // RELEASE").pack(fill="x", padx=pad, pady=(pad, px(10)))
        tk.Label(self, text=L(f"Chronolist v{version} is available (you have v{VERSION}).\n\n"
                              "The new version is downloaded from GitHub and replaces this program. Then Chronolist "
                              "restarts. Settings, sign-in and progress stay as they are.",
                              f"Chronolist v{version} ist verfügbar (du hast v{VERSION}).\n\n"
                              "Die neue Version wird von GitHub geladen und ersetzt dieses Programm. Danach startet "
                              "Chronolist neu. Einstellungen, Anmeldung und Fortschritt bleiben erhalten."),
                 bg=BG, fg=TEXT, font=F("ui", 16, False), justify="left", anchor="w",
                 wraplength=px(500)).pack(fill="x", padx=pad)
        row = tk.Frame(self, bg=BG)
        row.pack(fill="x", padx=pad, pady=pad)
        CutButton(row, L("UPDATE NOW", "JETZT UPDATEN"), self.go, width=150).pack(side="left")
        CutButton(row, L("WHAT'S NEW", "WAS IST NEU"), lambda: webbrowser.open(url), width=140).pack(
            side="left", padx=(px(8), 0))
        CutButton(row, L("LATER", "SPÄTER"), self.destroy, width=100).pack(side="left", padx=(px(8), 0))
        self.bind("<Escape>", lambda e: self.destroy())
        style_window(self)
        self.update_idletasks()
        x = app.winfo_rootx() + (app.winfo_width() - self.winfo_width()) // 2
        y = app.winfo_rooty() + max(0, (app.winfo_height() - self.winfo_height()) // 3)
        self.geometry(f"+{max(0, x)}+{max(0, y)}")
        self.grab_set()
        self.focus_force()

    def go(self):
        self.destroy()
        self.app.run_update()


class AppSettingsDialog(tk.Toplevel):
    """Programm-Einstellungen: Sprache, Start-Verhalten und Update-Check."""

    def __init__(self, app):
        super().__init__(app, bg=BG)
        self.app = app
        self.title(L("Program settings", "Programm-Einstellungen"))
        self.transient(app)
        self.resizable(False, False)
        pad = px(18)
        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=pad, pady=(pad, 0))

        Section(body, L("LANGUAGE", "SPRACHE"), "UI // HELP // LOG").pack(fill="x", pady=(0, px(6)))
        lang_row = tk.Frame(body, bg=BG)
        lang_row.pack(fill="x", pady=(0, px(4)))
        self.language = LANG
        self.lang_btns = {}
        for code, name in LANGUAGES.items():
            b = CutButton(lang_row, name, lambda c=code: self.set_language(c), width=140)
            b.pack(side="left", padx=(0, px(6)))
            self.lang_btns[code] = b
        tk.Label(body, text=L("INTERFACE, TOOLTIPS, HELP AND LOG. CHRONOLIST RESTARTS WHEN THE LANGUAGE CHANGES.",
                              "OBERFLÄCHE, TOOLTIPS, HILFE UND PROTOKOLL. CHRONOLIST STARTET BEIM SPRACHWECHSEL NEU."),
                 bg=BG, fg=FLUFF, font=F("mono", 10, False), anchor="w", justify="left",
                 wraplength=px(460)).pack(fill="x", pady=(px(2), px(14)))
        self.set_language(LANG)

        Section(body, L("START", "START"), "SYSTEM // BOOT").pack(fill="x", pady=(0, px(6)))
        self.autorun = app.autorun
        self.auto_card = ToggleCard(body, L("AUTO-RUN", "AUTOSTART"), L("START WORKING WHEN CHRONOLIST OPENS",
                                                                       "BEIM ÖFFNEN SOFORT LOSLEGEN"),
                                    self.autorun, self.toggle_auto, compact=True)
        self.auto_card.pack(fill="x", pady=(0, px(4)))
        Tooltip(self.auto_card, L("Start working automatically when Chronolist opens.",
                                  "Beim Öffnen von Chronolist automatisch starten."))
        self.boot = app.win_on
        self.boot_card = ToggleCard(body, "BOOT", L("START CHRONOLIST WITH WINDOWS", "CHRONOLIST MIT WINDOWS STARTEN"),
                                    self.boot, self.toggle_boot, available=winreg is not None, compact=True)
        self.boot_card.pack(fill="x", pady=(0, px(14)))
        Tooltip(self.boot_card, L("Start Chronolist automatically with Windows. Together with AUTO-RUN everything "
                                  "runs by itself.",
                                  "Chronolist automatisch mit Windows starten. Zusammen mit AUTOSTART läuft alles "
                                  "von allein."))

        Section(body, "UPDATES", "GITHUB // RELEASES").pack(fill="x", pady=(0, px(6)))
        self.updates = bool(app.settings.get("check_updates", True))
        self.upd_card = ToggleCard(body, L("UPDATE CHECK", "UPDATE-PRÜFUNG"),
                                   L("ASK GITHUB ON START", "BEIM START BEI GITHUB FRAGEN"),
                                   self.updates, self.toggle_updates, compact=True)
        self.upd_card.pack(fill="x", pady=(0, px(4)))
        Tooltip(self.upd_card, L("On start, Chronolist asks GitHub once whether a newer version exists. Nothing else "
                                 "is sent.",
                                 "Beim Start fragt Chronolist einmal bei GitHub, ob es eine neuere Version gibt. "
                                 "Sonst wird nichts übertragen."))

        row = tk.Frame(self, bg=BG)
        row.pack(fill="x", padx=pad, pady=pad)
        CutButton(row, L("SAVE", "SPEICHERN"), self.save, width=120).pack(side="left")
        CutButton(row, L("CANCEL", "ABBRECHEN"), self.destroy, width=120).pack(side="left", padx=(px(8), 0))
        self.bind("<Escape>", lambda e: self.destroy())
        self.bind("<Return>", lambda e: self.save())
        self.geometry(f"{px(500)}x{px(450)}")
        style_window(self)
        self.update_idletasks()
        x = app.winfo_rootx() + (app.winfo_width() - self.winfo_width()) // 2
        y = app.winfo_rooty() + max(0, (app.winfo_height() - self.winfo_height()) // 3)
        self.geometry(f"+{max(0, x)}+{max(0, y)}")
        self.grab_set()
        self.focus_force()

    def set_language(self, code):
        self.language = code
        for c, b in self.lang_btns.items():
            b.set_color(GREEN if c == code else YELLOW)
            b.set_text(("● " if c == code else "") + LANGUAGES[c])

    def toggle_auto(self):
        self.autorun = not self.autorun
        self.auto_card.update_value(self.autorun)

    def toggle_boot(self):
        self.boot = not self.boot
        self.boot_card.update_value(self.boot)

    def toggle_updates(self):
        self.updates = not self.updates
        self.upd_card.update_value(self.updates)

    def save(self):
        st = self.app.settings
        st["check_updates"] = self.updates
        self.app.set_windows(self.boot)
        self.app.autorun = self.autorun
        st["autorun"] = self.autorun
        lang_changed = self.language != LANG
        if lang_changed:
            # Standard-Vorlagen wechseln mit der Sprache, eigene Vorlagen bleiben
            for key, fn in (("playlist_template", default_playlist_template),
                            ("description_template", default_description_template)):
                if st.get(key) == fn():
                    st.pop(key, None)
            st["language"] = self.language
        save(SETTINGS_FILE, st)
        self.app.write_log(L("SETTINGS SAVED", "EINSTELLUNGEN GESPEICHERT"), "ok")
        self.app.app_card.redraw()
        self.destroy()
        if lang_changed:
            if self.app.worker and self.app.worker.is_alive():
                self.app.write_log(L("NEW LANGUAGE APPLIES AFTER THE NEXT START OF CHRONOLIST",
                                     "NEUE SPRACHE GILT AB DEM NÄCHSTEN START VON CHRONOLIST"), "info")
            else:
                self.app.restart()


def help_text():
    return HELP_TEXT_DE if LANG == "de" else HELP_TEXT_EN


HELP_TEXT_DE = [
    ("h1", "Chronolist Hilfe"),
    ("p", "Chronolist erstellt für einen YouTube-Kanal pro Jahr eine Playlist mit allen Videos aus diesem Jahr. "
          "Die Videos stehen in der Reihenfolge, in der sie hochgeladen wurden. Das älteste kommt zuerst."),

    ("h2", "Schnellstart"),
    ("li", "Einmalig oben auf den API-Knopf klicken und die Zugangsdaten eintragen."),
    ("li", "Links beim Zielkanal auf ÄNDERN klicken und den Kanal eingeben."),
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
            "begrenzt. Bei großen Kanälen schafft man so ungefähr ein altes Jahr pro Tag. Außerdem lässt diese Suche "
            "von YouTube manchmal einzelne Videos aus.\n"
            "• Kanalseite (nur mit FALLBACK): Chronolist liest im Edge-Fenster die Videoseite des Kanals, sortiert nach "
            "\"Älteste\". Das ist vollständig und braucht kein Kontingent. Mit FALLBACK wird das für alte Jahre immer "
            "genommen und für alle anderen, sobald das Kontingent leer ist. Jahre, die vorher über die Suche gefunden "
            "wurden, werden damit abgeglichen: fehlende Videos werden in die Playlist nachgetragen und an die "
            "richtige Stelle geschoben. Dafür das Jahr einfach markieren und START drücken.\n"
            "Was einmal gefunden wurde, wird gespeichert und nicht noch einmal gesucht."),
    ("num", "3.  Playlist anlegen. Für jedes Jahr legt Chronolist eine Playlist an. Standardmäßig heißt sie zum "
            "Beispiel \"MeinKanal 2012 in Upload-Reihenfolge\" und ist öffentlich. Name, Beschreibung, Sichtbarkeit "
            "und ein eigenes Bild stellst du unter PLAYLISTS ein."),
    ("num", "4.  Füllen. Die Videos werden der Reihe nach eingefügt. Jedes Video verbraucht 50 von 10.000 Einheiten, "
            "die man pro Tag hat. Das reicht für ungefähr 200 Videos am Tag."),
    ("num", "5.  Wenn das Tageskontingent leer ist:\n"
            "• FALLBACK aus: Chronolist hält an. Im Protokoll steht, wann das Kontingent zurückgesetzt wird. Das ist "
            "um Mitternacht kalifornischer Zeit. Chronolist rechnet das in deine Uhrzeit um.\n"
            "• FALLBACK an: Chronolist öffnet ein eigenes Edge-Fenster und fügt die restlichen Videos über die "
            "YouTube-Webseite ein, genau so, wie die Webseite es beim Speichern selbst macht. Dabei werden keine "
            "Einheiten verbraucht. Es läuft in Paketen zu 30 Videos mit Pausen dazwischen. Neue Playlists kann das "
            "Edge-Fenster auch anlegen. Videos finden (Schritt 2) kann es aber nicht.\n"
            "Chronolist merkt sich, wenn das Kontingent oder das Suchlimit an einem Tag aufgebraucht ist. Bis zum "
            "Reset fragt es nach einem Neustart nicht noch einmal nach, sondern nimmt gleich den Fallback. In dieser "
            "Zeit ist der API-Knopf gelb und zeigt LIMIT."),
    ("num", "6.  Prüfen. Doppelte Videos können nur entstehen, wenn ein Lauf mitten im Senden eines Pakets abbricht. "
            "Chronolist merkt sich deshalb das Paket, das gerade unterwegs ist, und prüft nach STOPP oder beim "
            "nächsten Start nur dieses. Das geht in Sekunden. Die ganze Playlist liest es nur am Ende eines Jahres "
            "und beim Knopf PRÜFEN: Doppelte werden entfernt und der Fortschritt wird abgeglichen.\n"
            "Shorts kommen nicht in die Playlists, Livestreams schon (manche Kanäle haben darüber z. B. mehrere "
            "Perspektiven veröffentlicht). Shorts erkennt Chronolist über die Shorts-Liste des Kanals. Die wird "
            "einmal über die API gelesen, sobald etwas Kontingent da ist. Mit FALLBACK werden Shorts auch aus "
            "bestehenden Playlists entfernt. Die Kanalseite ergänzt nur fehlende Videos, entfernt aber nie welche."),
    ("num", "7.  Nächstes Jahr. Ist ein Jahr fertig, geht es mit dem nächsten markierten Jahr in der Reihenfolge der "
            "Liste weiter. Kann ein Jahr gerade nicht gesucht werden, weil das Kontingent leer ist, nimmt Chronolist "
            "das nächste markierte Jahr, das schon bereit ist, und holt das fehlende später nach."),
    ("num", "8.  Laufendes Jahr. Das aktuelle Jahr bleibt markiert, auch wenn es fertig ist. Bei jedem Lauf schaut "
            "Chronolist nach neuen Videos und hängt sie hinten an. Mit Kontingent kostet das nur ein paar Einheiten, "
            "ohne geht es mit FALLBACK über die Kanalseite. Im Januar gilt das auch noch für das Vorjahr.\n"
            "Mehr als 5.000 Videos: Eine YouTube-Playlist fasst höchstens 5.000 Videos. Hat ein Jahr mehr, legt "
            "Chronolist automatisch eine zweite Playlist \"… (Teil 2)\" an und benennt die erste in \"… (Teil 1)\" "
            "um. Prüfen und Nachtragen laufen dann für jeden Teil."),

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
    ("li", "Ein Thumbnail hochladen: 50 Einheiten"),

    ("h2", "Oben im Fenster"),
    ("dt", "HILFE"),
    ("dd", "Öffnet diese Hilfe."),
    ("dt", "API-Knopf"),
    ("dd", "Zeigt, ob der Zugang zu YouTube funktioniert. Mit einem Klick kannst du die Zugangsdaten eintragen oder "
           "ändern (Client ID und Client Secret aus der Google Cloud Console).\n"
           "Rot FEHLT: Es sind noch keine Zugangsdaten eingetragen.\n"
           "Rot FEHLER: Google hat die Anmeldung abgelehnt.\n"
           "Blau ANMELDEN: Zugangsdaten sind da, angemeldet bist du aber noch nicht.\n"
           "Gelb LIMIT: Das Tageskontingent ist aufgebraucht.\n"
           "Grün ONLINE: Alles funktioniert."),
    ("dt", "Statusanzeige"),
    ("dd", "Zeigt, was das Programm gerade macht, zum Beispiel BEREIT, LÄUFT, BROWSER-MODUS (läuft über das "
           "Edge-Fenster), PAUSIERT oder eine Wartezeit mit Countdown."),

    ("h2", "Zielkanal"),
    ("p", "Hier steht der Kanal, dessen Videos gesammelt werden. Über ÄNDERN gibst du einen anderen Kanal ein. "
          "Das geht als @Name, als Link zum Kanal oder als Kanal-ID (die fängt mit UC an). Chronolist holt sich dann "
          "den Namen und das Startjahr. Jeder Kanal hat seinen eigenen Fortschritt."),

    ("h2", "Jahre"),
    ("p", "Jede Karte steht für ein Jahr. Mit einem Klick markierst du ein Jahr oder nimmst die Markierung weg. "
          "Markierte Jahre sind hell umrandet und werden beim Start nacheinander bearbeitet, von oben nach unten. "
          "Bei einem neuen Kanal sind alle abgeschlossenen Jahre markiert, nimm einfach die raus, die du nicht willst. "
          "Die Reihenfolge änderst du, indem du eine Karte mit gedrückter Maustaste nach oben oder unten ziehst. "
          "Rechts siehst du den Fortschritt in "
          "Prozent und wie viele Videos schon drin sind. Während ein Jahr läuft, ist sein Symbol links ein "
          "Pause-Knopf, beim Pausieren ein Play-Knopf. Das funktioniert wie PAUSE und WEITER."),
    ("dt", "Farben"),
    ("p", "Cyan, leuchtend: läuft gerade. Es ist immer nur ein Jahr cyan.\n"
          "Gelb: angefangen, wartet darauf, weiterzumachen.\n"
          "Blaugrau: noch nicht angefangen.\n"
          "Grün mit Haken, abgedunkelt: fertig, alle Videos sind drin. Fertige Jahre werden automatisch abgewählt.\n"
          "Roter Text \"BRAUCHT API\": Hier geht es erst nach dem Reset des Tageskontingents weiter, die Uhrzeit "
          "steht dahinter. Das betrifft Jahre, deren Videos noch gesucht werden müssen, denn das geht nur über die "
          "API. Bei angefangenen und bereiten Jahren nur, wenn FALLBACK aus ist."),
    ("dt", "Was unter dem Jahr stehen kann"),
    ("dd", "Läuft für ein Jahr gerade etwas, steht hier in Cyan, was passiert, mit Fortschritt, z. B. KANALSEITE // 1400 GELESEN, "
           "NACHTRAG // 40/102 EINGEFÜGT, SORTIERE // 20/102 oder PRÜFE PLAYLIST // 600 GELESEN.\n"
           "NICHT GESUCHT: Die Videos wurden noch nicht gesucht.\n"
           "SUCHE PAUSIERT: Die Suche wurde angefangen, ist aber noch nicht fertig.\n"
           "BEREIT: Alle Videos sind gefunden, die Playlist kann gefüllt werden.\n"
           "IN ARBEIT: Die Playlist ist schon teilweise gefüllt.\n"
           "FERTIG: Das Jahr ist fertig.\n"
           "WIRD GEFÜLLT: Wird gerade gefüllt (grün).\n"
           "PLAYLIST WIRD GEPRÜFT: Die Playlist wird gerade auf doppelte Videos geprüft."),

    ("h2", "Stichwortlisten"),
    ("p", "Unter den Jahren legt EIGENE LISTE eine Playlist an, in die alle Videos kommen, deren Titel zu deinen "
          "Stichwörtern passen, in Upload-Reihenfolge. Du gibst einen Namen ein (er wird der Playlist-Titel) und Begriffe: "
          "\"enthält eines davon\", \"und alle diese\" und \"aber keines dieser\". Gross-/Kleinschreibung zählt nicht. "
          "Optional grenzt du die Liste auf einen Zeitraum von Jahren ein (z. B. 2016 bis 2020). "
          "Die Vorschau zeigt sofort, wie viele Videos passen. Videos schließt du aus (mit Strg/Umschalt+Klick mehrere wählen, dann AUSSCHLIESSEN), und in der Ansicht ALLE VIDEOS nimmst du auch Videos auf, die nicht passen "
          "oder nimmst sie über Link oder Video-ID fest hinzu.\n"
          "Eine Liste ist eine Karte wie ein Jahr: anklicken zum Markieren, Rechtsklick zum Bearbeiten. Sie wird nach den Jahren "
          "bearbeitet und bei jedem Lauf neu abgeglichen, also kommen neue Videos automatisch dazu. Ändern sich die "
          "Begriffe, wird die Playlist beim nächsten Lauf angepasst (dafür muss FALLBACK an sein). Dafür braucht Chronolist "
          "die Titel aller Videos. Fehlende werden beim Start aus deinen Playlists nachgeladen (kostet wenig Kontingent) "
          "oder sonst einzeln und langsam ohne Kontingent. Eine gelöschte Liste bleibt auf YouTube bestehen, bis du sie dort löschst."),

    ("h2", "Steuerung"),
    ("dt", "START"),
    ("dd", "Arbeitet die markierten Jahre nacheinander ab, in der Reihenfolge der Liste."),
    ("dt", "PAUSE und WEITER"),
    ("dd", "PAUSE hält nach dem aktuellen Schritt an. Mit WEITER geht es genau dort weiter."),
    ("dt", "STOPP"),
    ("dd", "Beendet den Lauf. Vorher prüft Chronolist die Playlist des aktuellen Jahres noch einmal auf doppelte "
           "Videos. Der Fortschritt bleibt gespeichert."),
    ("dt", "PRÜFEN"),
    ("dd", "Prüft die Playlists der ausgewählten Jahre komplett. Doppelte Videos werden entfernt, wobei immer das "
           "erste stehen bleibt, damit die Reihenfolge stimmt. Außerdem wird der Fortschritt abgeglichen."),

    ("h2", "Optionen"),
    ("dt", "FALLBACK"),
    ("dd", "Wenn das Tageskontingent aufgebraucht ist, macht Chronolist über ein eigenes Edge-Fenster weiter, in dem "
           "du bei YouTube angemeldet bist. Das Fenster muss offen bleiben, minimieren geht aber. Alte Jahre werden "
           "darüber außerdem vollständig über die Kanalseite gesucht, auch ohne Kontingent.\n"
           "Wichtig: Das ist eine automatische Nutzung der YouTube-Webseite und laut den Nutzungsbedingungen von "
           "YouTube nicht erlaubt. Im schlimmsten Fall kann dein Konto gesperrt werden. Deshalb ist der Schalter am "
           "Anfang aus."),
    ("dt", "PROGRAMM"),
    ("dd", "Einstellungen für das Programm selbst.\n"
           "Sprache: Englisch oder Deutsch. Das gilt für die ganze Oberfläche, die Tooltips, diese Hilfe und das "
           "Protokoll. Nach dem Speichern startet Chronolist kurz neu.\n"
           "Autostart: legt beim Öffnen von Chronolist sofort los.\n"
           "Boot: startet Chronolist zusammen mit Windows. Zusammen mit Autostart läuft dann alles von allein.\n"
           "Update-Prüfung: ob Chronolist beim Start bei GitHub nach einer neuen Version fragt."),
    ("dt", "PLAYLISTS"),
    ("dd", "Titel und Beschreibung: Du kannst Platzhalter verwenden, die beim Anlegen ersetzt werden. {channel} wird "
           "zum Kanalnamen, {handle} zum @Handle, {year} zum Jahr und {count} zur Anzahl der Videos in diesem Jahr. "
           "Die Knöpfe unter dem Titelfeld fügen sie an der Stelle ein, an der der Cursor steht. Darunter siehst du "
           "eine Vorschau. Lässt du die Beschreibung leer, bekommt die Playlist keine.\n"
           "Sichtbarkeit: öffentlich, nicht gelistet (nur mit Link) oder privat.\n"
           "Thumbnails: Für jedes Jahr kannst du ein eigenes Bild auswählen, auch schon bevor es die Playlist gibt. "
           "Es wird hochgeladen, sobald die Playlist existiert und Kontingent da ist (50 Einheiten pro Bild). "
           "Erlaubt sind JPG und PNG bis 2 MB, am besten im Format 1280×720. Diese Funktion ist noch neu und "
           "nicht mit allen Konten getestet.\n"
           "Titel, Beschreibung und Sichtbarkeit gelten für Playlists, die ab jetzt angelegt werden. Schon "
           "vorhandene Playlists bleiben so, wie sie sind."),

    ("h2", "Warum macht Chronolist Pausen?"),
    ("p", "Über das Edge-Fenster arbeitet Chronolist absichtlich nicht im Höchsttempo, damit es nicht wie ein Bot "
          "aussieht. Die Pausen siehst du mit Countdown auf der Jahreskarte und oben in der Statusanzeige."),
    ("dd", "NÄCHSTES PAKET: kurze Pause zwischen zwei Paketen mit je 30 Videos, 4 bis 12 Sekunden.\n"
           "KURZE PAUSE: etwa 20 Sekunden nach jeweils 250 Videos.\n"
           "LANGE PAUSE: etwa vier Minuten nach jeweils 1500 Videos.\n"
           "YOUTUBE BREMST: YouTube bremst kurz, Chronolist wartet zehn Minuten.\n"
           "NEUER VERSUCH oder KEINE VERBINDUNG: kurzer Fehler oder keine Verbindung. Chronolist versucht es gleich "
           "noch einmal."),

    ("h2", "Was du sonst noch wissen solltest"),
    ("li", "Die Datumssuche für alte Jahre ist nicht ganz lückenlos. Einzelne Videos können fehlen. Jahre aus der "
           "Upload-Liste sind vollständig."),
    ("li", "In eine Playlist passen höchstens 5.000 Videos."),
    ("li", "Für die Webseite nennt YouTube keine Grenzen. Die Pausen in Chronolist sind eine vorsichtige Schätzung "
           "und keine Garantie."),

    ("h2", "Protokoll"),
    ("p", "Unten rechts steht jeder Schritt mit Uhrzeit. Grün heißt erledigt, Gelb ist ein Hinweis oder eine "
          "Wartezeit, Rot ist ein Fehler oder der Beginn eines neuen Abschnitts."),

    ("h2", "Wo liegen meine Daten?"),
    ("p", "Alles liegt im Ordner des Programms: credentials.json (Zugangsdaten), token.json (Anmeldung), "
          "settings.json (Einstellungen), quota.json (gemerktes Tageslimit), der Ordner data mit dem Fortschritt "
          "und den Thumbnails pro Kanal und Jahr und der Ordner browser_profile mit der Anmeldung im Edge-Fenster. "
          "Nichts davon wird irgendwohin hochgeladen, außer zu Google/YouTube selbst."),

    ("h2", "Updates"),
    ("p", "Beim Start fragt Chronolist einmal bei GitHub nach, ob es eine neuere Version gibt. Wenn ja, erscheint oben "
          "neben dem HILFE-Knopf ein grüner UPDATE-Knopf. Ein Klick darauf fragt, ob du jetzt updaten willst. Dann lädt "
          "Chronolist die neue Version von GitHub, ersetzt sich selbst und startet neu. Danach steht einmal im "
          "Protokoll, was neu ist. Einstellungen, Anmeldung und "
          "Fortschritt bleiben erhalten. Während ein Durchlauf läuft, geht das nicht, erst STOPP drücken. Wenn du "
          "Chronolist als Python-Skript startest, öffnet der Klick stattdessen die Download-Seite. Außer diesen "
          "Anfragen an GitHub wird nichts übertragen. Die Prüfung beim Start kannst du unter PROGRAMM abschalten."),

    ("h2", "Über Chronolist"),
    ("p", f"Version {VERSION}, Build {BUILD:04d}. Open Source unter der MIT-Lizenz: {REPO_URL}"),
    ("p", "Chronolist wurde vollständig mit Claude programmiert, einer KI von Anthropic. Fehler, Wünsche und Fragen "
          "gerne als Issue auf GitHub."),
    ("p", "Chronolist ist ein unabhängiges Projekt und steht in keiner Verbindung zu YouTube oder Google. "
          "Du nutzt dabei deine eigenen API-Zugangsdaten und dein eigenes Konto. Für die Einhaltung der "
          "Nutzungsbedingungen von YouTube bist du selbst verantwortlich, besonders beim FALLBACK."),
]


HELP_TEXT_EN = [
    ("h1", "Chronolist Help"),
    ("p", "Chronolist creates one playlist per year for a YouTube channel, containing every video from that year. "
          "The videos are in the order they were uploaded, oldest first."),

    ("h2", "Quick start"),
    ("li", "Once: click the API button at the top and enter your credentials."),
    ("li", "On the left, click EDIT on the target channel and enter the channel."),
    ("li", "Click one or more years in the list."),
    ("li", "Press START. The program does the rest."),

    ("h2", "How Chronolist works"),
    ("p", "Chronolist talks to YouTube in two ways. Normally through Google's official interface (YouTube Data API). "
          "If you switch on FALLBACK, also through the YouTube website in a separate Edge window. A run looks like this:"),
    ("num", "1.  Sign in. On the first start you sign in to Google once by opening the link and entering the code "
            "shown. After that Chronolist remembers the sign-in."),
    ("num", "2.  Find the videos. First Chronolist has to know which videos belong to which year. This only works "
            "through the API, in two ways:\n"
            "• Archive scan: Chronolist goes through the channel's upload list once and saves all years in one go. "
            "This needs very few units.\n"
            "• Date search: that list only contains the newest 20,000 uploads, so older years are searched month by "
            "month. This uses many units and is limited to about 100 searches per day. For large channels that is "
            "roughly one old year per day. On top of that, YouTube's search sometimes leaves out single videos.\n"
            "• Channel page (only with FALLBACK): Chronolist reads the channel's videos page in the Edge window, sorted "
            "by \"Oldest\". This is complete and needs no quota. With FALLBACK it is always used for old years and for "
            "all other years once the quota is used up. Years found earlier through the search are checked against it: "
            "missing videos are added to the playlist and moved to the right spot. Just select the year and press "
            "START.\n"
            "Whatever has been found is saved and never searched again."),
    ("num", "3.  Create the playlist. Chronolist creates one playlist per year. By default it is called, for "
            "example, \"MyChannel 2012 in upload order\" and is public. Title, description, visibility and a custom "
            "image are set under PLAYLISTS."),
    ("num", "4.  Fill it. The videos are added in order. Each video uses 50 of the 10,000 units you get per day, "
            "which is enough for about 200 videos a day."),
    ("num", "5.  When the daily quota is used up:\n"
            "• FALLBACK off: Chronolist stops. The log shows when the quota resets. That is midnight California "
            "time; Chronolist converts it to your local time.\n"
            "• FALLBACK on: Chronolist opens its own Edge window and adds the remaining videos through the YouTube "
            "website, exactly the way the website does it when you save a video. This uses no units. It works in "
            "batches of 30 videos with pauses in between. The Edge window can also create new playlists. It cannot "
            "find videos (step 2), though.\n"
            "Chronolist remembers when the quota or the search limit was used up on a given day. Until the reset it "
            "won't ask again after a restart and goes straight to the fallback. During that time the API button is "
            "yellow and shows LIMIT."),
    ("num", "6.  Check. Duplicates can only appear if a run stops while a batch is being sent. So Chronolist "
            "remembers the batch in flight and after STOP or on the next start checks only that one, which takes "
            "seconds. It reads the whole playlist only at the end of a year and with the CHECK button: duplicates "
            "are removed and the progress is synced.\n"
            "Shorts are not added to the playlists, livestreams are (some channels used them to publish several "
            "perspectives, for example). Shorts are detected through the channel's shorts list, which is read once "
            "through the API when a little quota is available. With FALLBACK shorts are also removed from existing "
            "playlists. The channel page only adds missing videos and never removes any."),
    ("num", "7.  Next year. When a year is done, Chronolist moves on to the next selected year in the order of the "
            "list. If a year can't be searched right now because the quota is used up, Chronolist takes the next "
            "selected year that is already ready and catches up later."),
    ("num", "8.  Current year. The current year stays selected even when it is done. On every run Chronolist looks "
            "for new videos and appends them. With quota this costs only a few units, without quota it works with "
            "FALLBACK through the channel page. In January this also applies to the previous year.\n"
            "More than 5,000 videos: a YouTube playlist holds at most 5,000 videos. If a year has more, Chronolist "
            "automatically creates a second playlist \"… (Part 2)\" and renames the first one to \"… (Part 1)\". "
            "Checking and repairing then work for each part."),

    ("h2", "What are units?"),
    ("p", "Using the YouTube API is free. There is no money involved. Google does give every project a fixed daily "
          "quota, measured in units. By default that is 10,000 units per day, plus a separate limit of about 100 "
          "searches per day. When the quota is used up, things continue after the reset the next day. The Edge "
          "window uses no units at all."),
    ("p", "Units per action:"),
    ("li", "Going through the upload list (archive scan): 1 unit per 50 videos"),
    ("li", "Date search for old years: 100 units per 50 videos, also counts towards the search limit"),
    ("li", "Creating a playlist: 50 units"),
    ("li", "Adding one video: 50 units"),
    ("li", "Checking a playlist: 1 unit per 50 videos"),
    ("li", "Removing one duplicate: 50 units"),
    ("li", "Uploading a thumbnail: 50 units"),

    ("h2", "At the top of the window"),
    ("dt", "HELP"),
    ("dd", "Opens this help."),
    ("dt", "API button"),
    ("dd", "Shows whether access to YouTube works. Click it to enter or change your credentials (Client ID and "
           "Client Secret from the Google Cloud Console).\n"
           "Red MISSING: no credentials entered yet.\n"
           "Red ERROR: Google refused the sign-in.\n"
           "Blue SIGN IN: credentials are set, but you're not signed in yet.\n"
           "Yellow LIMIT: the daily quota is used up.\n"
           "Green ONLINE: everything works."),
    ("dt", "Status display"),
    ("dd", "Shows what the program is doing, for example STANDBY, RUNNING, BROWSER UPLINK (running through the Edge "
           "window), PAUSED or a waiting time with a countdown."),

    ("h2", "Target channel"),
    ("p", "The channel whose videos are collected. Use EDIT to enter another channel, as @name, as a link to the "
          "channel or as the channel ID (starts with UC). Chronolist then looks up its name and start year. Every "
          "channel has its own progress."),

    ("h2", "Target years"),
    ("p", "Each card is one year. Click a year to select or deselect it. Selected years have a bright border and are "
          "processed one after another on START, from top to bottom. For a new channel all completed years are "
          "selected; just deselect the ones you don't want. To change the order, drag a card up or down with the "
          "mouse button held. On the right you see the progress in percent and how many videos "
          "are already in. While a year is running, its icon on the left is a pause button, and a play button while "
          "paused. It works like PAUSE and RESUME."),
    ("dt", "Colors"),
    ("p", "Cyan, glowing: running right now. Only one year is ever cyan.\n"
          "Yellow: started, waiting to continue.\n"
          "Blue-gray: not started yet.\n"
          "Green with a check mark, dimmed: done, all videos are in. Finished years are deselected automatically.\n"
          "Red text \"NEEDS API\": this year can only continue after the daily quota resets, the time is shown "
          "next to it. This applies to years whose videos still have to be searched, which only works through the "
          "API. For started and ready years only when FALLBACK is off."),
    ("dt", "What can appear under a year"),
    ("dd", "While something is happening for a year, this line shows it in cyan, with progress, e.g. CHANNEL PAGE // "
           "1400 READ, REPAIR // 40/102 ADDED, SORTING // 20/102 or CHECKING PLAYLIST // 600 READ.\n"
           "NOT SCANNED: the videos haven't been searched yet.\n"
           "SCAN PAUSED: the search was started but isn't finished.\n"
           "READY: all videos found, the playlist can be filled.\n"
           "IN PROGRESS: the playlist is partly filled.\n"
           "COMPLETE: the year is done.\n"
           "UPLINK ACTIVE: being filled right now (green).\n"
           "CHECKING PLAYLIST: the playlist is being checked for duplicates."),

    ("h2", "Keyword lists"),
    ("p", "Below the years, CUSTOM LIST creates a playlist of all videos whose title matches your keywords, in upload "
          "order. Enter a name (it becomes the playlist title) and terms: \"contains any of\", \"and all of these\" and "
          "\"but none of these\". Upper/lower case is ignored. Optionally limit the list to a range of years (e.g. 2016 to 2020). "
          "The preview shows right away how many videos match. "
          "Exclude videos (select with Ctrl/Shift+click, then EXCLUDE); the ALL VIDEOS view lets you include videos that do not match or add them for good with a link or video ID.\n"
          "A list is a card like a year: click to select, right-click to edit. It runs after the years and is matched again "
          "on every run, so new videos are added automatically. If you change the terms, the playlist is adjusted on the "
          "next run (this needs FALLBACK on). Chronolist needs the titles of all videos for this. Missing ones are loaded "
          "at the start from your playlists (little quota) or otherwise one by one, slowly, without quota. A deleted list "
          "stays on YouTube until you delete it there."),

    ("h2", "Control"),
    ("dt", "START"),
    ("dd", "Starts working on the selected years. When a year is done, it moves on to the next one by itself."),
    ("dt", "PAUSE and RESUME"),
    ("dd", "PAUSE stops after the current step. RESUME continues right there."),
    ("dt", "STOP"),
    ("dd", "Ends the run. Before that, Chronolist checks the current year's playlist for duplicates once more. "
           "Your progress is saved."),
    ("dt", "CHECK"),
    ("dd", "Checks the playlists of the selected years completely. Duplicates are removed, always keeping the first "
           "one so the order stays right. The progress is synced as well."),

    ("h2", "Options"),
    ("dt", "FALLBACK"),
    ("dd", "When the daily quota is used up, Chronolist continues in a separate Edge window where you are signed in "
           "to YouTube. The window has to stay open, minimizing is fine. Old years are also searched completely "
           "through the channel page there, even without quota.\n"
           "Important: this is automated use of the YouTube website and is not allowed by YouTube's Terms of "
           "Service. In the worst case your account can be restricted. That's why the switch is off by default."),
    ("dt", "PROGRAM"),
    ("dd", "Settings for the program itself.\n"
           "Language: English or German. This applies to the whole interface, the tooltips, this help and the log. "
           "Chronolist restarts briefly after saving.\n"
           "Auto-run: starts working as soon as Chronolist opens.\n"
           "Boot: starts Chronolist together with Windows. Together with auto-run everything runs by itself.\n"
           "Update check: whether Chronolist asks GitHub for a new version on start."),
    ("dt", "PLAYLISTS"),
    ("dd", "Title and description: you can use placeholders that are filled in when the playlist is created. "
           "{channel} becomes the channel name, {handle} the @handle, {year} the year and {count} the number of "
           "videos in that year. The buttons below the title field insert them at the cursor. Below you see a "
           "preview. Leave the description empty for no description.\n"
           "Visibility: public, unlisted (link only) or private.\n"
           "Thumbnails: you can pick a custom image for every year, even before the playlist exists. It is "
           "uploaded as soon as the playlist exists and quota is available (50 units per image). JPG and PNG up to "
           "2 MB, ideally 1280×720. This feature is new and not tested with every account.\n"
           "Title, description and visibility apply to playlists created from now on. Existing playlists stay as "
           "they are."),

    ("h2", "Why does Chronolist pause?"),
    ("p", "Through the Edge window Chronolist deliberately doesn't work at full speed, so it doesn't look like a bot. "
          "You see the pauses with a countdown on the year card and in the status display at the top."),
    ("dd", "NEXT BATCH: short pause between two batches of 30 videos, 4 to 12 seconds.\n"
           "COOLDOWN: about 20 seconds after every 250 videos.\n"
           "BREAK: about four minutes after every 1500 videos.\n"
           "RATE LIMIT: YouTube slows things down briefly, Chronolist waits ten minutes.\n"
           "RETRY or NO SIGNAL: a short error or no connection. Chronolist tries again shortly."),

    ("h2", "Good to know"),
    ("li", "The date search for old years isn't completely gap-free. Single videos can be missing. Years from the "
           "upload list are complete."),
    ("li", "A playlist can hold at most 5,000 videos."),
    ("li", "YouTube doesn't publish limits for the website. The pauses in Chronolist are a careful estimate, not a "
           "guarantee."),

    ("h2", "System log"),
    ("p", "At the bottom right every step is listed with the time. Green means done, yellow is a note or a waiting "
          "time, red is an error or the start of a new section."),

    ("h2", "Where is my data?"),
    ("p", "Everything is in the program folder: credentials.json (credentials), token.json (sign-in), settings.json "
          "(settings), quota.json (remembered daily limit), the data folder with progress and thumbnails per channel "
          "and year, and the browser_profile folder with the sign-in for the Edge window. None of it is uploaded "
          "anywhere except to Google/YouTube itself."),

    ("h2", "Updates"),
    ("p", "On start, Chronolist asks GitHub once whether there is a newer version. If so, a green UPDATE "
          "button appears next to HELP at the top. Clicking it asks whether you want to update now. Chronolist then "
          "downloads the new version from GitHub, replaces itself and restarts. Afterwards the log shows once "
          "what's new. Settings, sign-in and progress stay "
          "as they are. This is not possible during a run, press STOP first. If you run Chronolist as a Python "
          "script, the click opens the download page instead. Nothing but these requests to GitHub is sent. You "
          "can turn off the check on start under PROGRAM."),

    ("h2", "About Chronolist"),
    ("p", f"Version {VERSION}, build {BUILD:04d}. Open source under the MIT license: {REPO_URL}"),
    ("p", "Chronolist was written entirely with Claude, an AI by Anthropic. Bugs, wishes and questions are welcome "
          "as issues on GitHub."),
    ("p", "Chronolist is an independent project and not affiliated with YouTube or Google. You use your own API "
          "credentials and your own account. You are responsible for following YouTube's Terms of Service, "
          "especially when using FALLBACK."),
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


def _win_placement_api():
    import ctypes
    from ctypes import wintypes

    class WINDOWPLACEMENT(ctypes.Structure):
        _fields_ = [("length", wintypes.UINT), ("flags", wintypes.UINT), ("showCmd", wintypes.UINT),
                    ("ptMinPosition", wintypes.POINT), ("ptMaxPosition", wintypes.POINT),
                    ("rcNormalPosition", wintypes.RECT)]
    return ctypes, WINDOWPLACEMENT


def get_placement(win):
    """Windows: (links, oben, rechts, unten) der normalen Fensterlage + maximiert? Sonst None."""
    if os.name != "nt":
        return None
    try:
        ctypes, WP = _win_placement_api()
        hwnd = int(win.wm_frame(), 16)
        wp = WP()
        wp.length = ctypes.sizeof(WP)
        if not ctypes.windll.user32.GetWindowPlacement(hwnd, ctypes.byref(wp)):
            return None
        r = wp.rcNormalPosition
        return [r.left, r.top, r.right, r.bottom], wp.showCmd == 3      # 3 = SW_SHOWMAXIMIZED
    except Exception:  # noqa: BLE001
        return None


def set_placement(win, rect, zoomed):
    """Windows: Fensterlage setzen (auch die 'normale' Lage hinter einem maximierten Fenster)."""
    if os.name != "nt":
        return False
    try:
        ctypes, WP = _win_placement_api()
        hwnd = int(win.wm_frame(), 16)
        wp = WP()
        wp.length = ctypes.sizeof(WP)
        wp.showCmd = 3 if zoomed else 1
        wp.rcNormalPosition.left, wp.rcNormalPosition.top, wp.rcNormalPosition.right, wp.rcNormalPosition.bottom = rect
        return bool(ctypes.windll.user32.SetWindowPlacement(hwnd, ctypes.byref(wp)))
    except Exception:  # noqa: BLE001
        return False


def point_on_screen(x, y):
    """Liegt der Punkt auf einem angeschlossenen Monitor? (Windows; sonst immer True)"""
    if os.name != "nt":
        return True
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        user32.MonitorFromPoint.restype = wintypes.HMONITOR
        user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
        return bool(user32.MonitorFromPoint(wintypes.POINT(int(x), int(y)), 0))   # MONITOR_DEFAULTTONULL
    except Exception:  # noqa: BLE001
        return True


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
        self.title(L("Chronolist Help", "Chronolist Hilfe"))
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
        for kind, text in help_text():
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
        style_window(self)
        self.transient(master)
        self.focus_force()


class RemovedWindow(tk.Toplevel):
    """Liste der nicht übernommenen Videos (Shorts, Livestreams, Fehltreffer), mit Titeln und Links."""

    def __init__(self, master, focus_year=None):
        super().__init__(master, bg="#16161f")
        self.title(L("Left out videos", "Nicht übernommene Videos"))
        self.geometry(f"{px(760)}x{px(720)}")
        self.minsize(px(480), px(360))
        base = "Segoe UI" if "Segoe UI" in tkfont.families(self) else "TkDefaultFont"
        txt = tk.Text(self, wrap="word", bg="#16161f", fg="#e8e8ee", bd=0, relief="flat",
                      highlightthickness=0, padx=px(28), pady=px(22), cursor="arrow",
                      font=(base, -px(15)), spacing1=px(2), spacing3=px(4), selectbackground="#3a3a55")
        sb = ttk.Scrollbar(self, orient="vertical", command=txt.yview, style="NC.Vertical.TScrollbar")
        txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        txt.pack(side="left", fill="both", expand=True)
        txt.tag_configure("h1", font=(base, -px(22), "bold"), foreground="#ffffff", spacing3=px(6))
        txt.tag_configure("p", foreground="#c9c9d6", spacing3=px(10))
        txt.tag_configure("h2", font=(base, -px(18), "bold"), foreground=CYAN, spacing1=px(16), spacing3=px(4))
        txt.tag_configure("date", foreground="#8a8aa0", font=(base, -px(13)))
        txt.tag_configure("link", foreground="#e8e8ee", lmargin1=px(12), lmargin2=px(12))
        txt.tag_configure("hover", foreground=CYAN)
        self.txt = txt
        data = removed_by_year()
        self.titles = load(titles_file(), {})
        txt.insert("end", L("Left out videos", "Nicht übernommene Videos") + "\n", "h1")
        txt.insert("end", L("Videos that were found but are left out of the playlists (shorts). "
                            "Click a title to open it on YouTube.",
                            "Videos, die gefunden, aber nicht in die Playlists übernommen wurden (Shorts). "
                            "Klick auf einen Titel öffnet ihn auf YouTube.") + "\n", "p")
        if not data:
            txt.insert("end", L("Nothing left out so far.", "Bisher wurde nichts aussortiert.") + "\n", "p")
        self.pending = []
        jump = None
        for y in sorted(data):
            mark = f"y{y}"
            txt.mark_set(mark, "end-1c")
            txt.mark_gravity(mark, "left")
            if y == focus_year:
                jump = mark
            n = len(data[y])
            txt.insert("end", f"{y}  ·  {n} " + (L("video", "Video") if n == 1 else L("videos", "Videos")) + "\n", "h2")
            for v in data[y]:
                tag = "v_" + v["id"]
                d = v.get("published") or ""
                txt.insert("end", (d[8:10] + "." + d[5:7] + "." + d[:4] + "   ") if len(d) >= 10 else "", "date")
                txt.insert("end", (self.titles.get(v["id"]) or v["id"]) + "\n", ("link", tag))
                txt.tag_bind(tag, "<Button-1>", lambda e, vid=v["id"]: webbrowser.open(f"https://www.youtube.com/watch?v={vid}"))
                txt.tag_bind(tag, "<Enter>", lambda e, t=tag: (txt.configure(cursor="hand2"), txt.tag_add("hover", f"{t}.first", f"{t}.last")))
                txt.tag_bind(tag, "<Leave>", lambda e, t=tag: (txt.configure(cursor="arrow"), txt.tag_remove("hover", "1.0", "end")))
                if v["id"] not in self.titles:
                    self.pending.append(v["id"])
        txt.configure(state="disabled")
        if jump:
            self.after(50, lambda: txt.see(jump) or txt.yview(jump))
        style_window(self)
        self.transient(master)
        self.focus_force()
        self.q = queue.Queue()
        if self.pending:
            threading.Thread(target=self.fetch_titles, args=(list(self.pending),), daemon=True).start()
            self.after(300, self.apply_titles)

    def fetch_titles(self, ids):
        for vid in ids:
            t = video_title(vid)
            self.q.put((vid, t, bool(t)))

    def apply_titles(self):
        if not self.winfo_exists():
            return
        changed = False
        try:
            while True:
                vid, t, ok = self.q.get_nowait()
                if not ok:
                    t = L("(unavailable) ", "(nicht verfügbar) ") + vid
                tag = "v_" + vid
                rng = self.txt.tag_ranges(tag)
                if rng:
                    self.txt.configure(state="normal")
                    self.txt.delete(rng[0], rng[1])
                    self.txt.insert(rng[0], t + "\n", ("link", tag))
                    self.txt.configure(state="disabled")
                if ok:
                    self.titles[vid] = t       # nur echte Titel merken
                    changed = True
                if vid in self.pending:
                    self.pending.remove(vid)
        except queue.Empty:
            pass
        if changed:
            remember_titles(self.titles)
        if self.pending:
            self.after(400, self.apply_titles)


class ListDialog(tk.Toplevel):
    """Stichwortliste anlegen/bearbeiten: Suchbegriffe, Live-Vorschau, einzelne Videos ausschließen/hinzufügen."""
    SHOW_MAX = 4000

    def __init__(self, app, key, parent=None):
        super().__init__(app, bg=BG)
        self.app, self.key, self.parent_dlg = app, key, parent
        lst = (find_list(key) if key else None) or {}
        self.lst_id = lst.get("id") or None
        self.add, self.skip = set(lst.get("add") or []), set(lst.get("skip") or [])
        self.title(L("Custom list", "Eigene Liste"))
        self.transient(parent or app)
        self.minsize(px(700), px(640))
        self.geometry(f"{px(780)}x{px(880)}")
        self.pool = all_year_videos()
        self.titles = load(titles_file(), {})
        self.shown = []          # (typ, id) je Zeile: "hit" | "add" | "skip"
        self.after_id = None
        self.confirm_delete = False
        pad = px(18)

        row = tk.Frame(self, bg=BG)
        row.pack(side="bottom", fill="x", padx=pad, pady=pad)
        CutButton(row, L("SAVE", "SPEICHERN"), self.save, width=120).pack(side="left")
        CutButton(row, L("CANCEL", "ABBRECHEN"), self.destroy, width=120).pack(side="left", padx=(px(8), 0))
        if self.lst_id:
            self.del_btn = CutButton(row, L("DELETE LIST", "LISTE LÖSCHEN"), self.delete, width=170)
            self.del_btn.set_color(RED)
            self.del_btn.pack(side="right")
        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=pad, pady=(pad, 0))
        Section(body, L("CUSTOM LIST", "EIGENE LISTE"), "TITLE // FILTER").pack(fill="x", pady=(0, px(10)))

        self.name_var = tk.StringVar(value=lst.get("name", ""))
        self.field(body, L("NAME (REPLACES {year} IN THE PLAYLIST TITLE)", "NAME (ERSETZT {year} IM PLAYLIST-TITEL)"), self.name_var)
        self.any_var = tk.StringVar(value=", ".join(lst.get("any") or []))
        self.field(body, L("TITLE CONTAINS ANY OF  (SEPARATE WITH COMMAS)", "TITEL ENTHÄLT EINES DAVON  (MIT KOMMA TRENNEN)"), self.any_var)
        self.all_var = tk.StringVar(value=", ".join(lst.get("all") or []))
        self.field(body, L("AND ALL OF THESE  (OPTIONAL)", "UND ALLE DIESE  (OPTIONAL)"), self.all_var)
        self.none_var = tk.StringVar(value=", ".join(lst.get("none") or []))
        self.field(body, L("BUT NONE OF THESE  (OPTIONAL)", "ABER KEINES DIESER  (OPTIONAL)"), self.none_var)
        yr = tk.Frame(body, bg=BG)
        yr.pack(fill="x", pady=(0, px(8)))
        tk.Label(yr, text=L("ONLY YEARS  (OPTIONAL)", "NUR JAHRE  (OPTIONAL)"), bg=BG, fg=TEXT_MUTED, font=F("mono", 11),
                 anchor="w").pack(side="left")
        self.from_var = tk.StringVar(value=str(lst.get("from") or ""))
        self.to_var = tk.StringVar(value=str(lst.get("to") or ""))
        for var, txt in ((self.from_var, L("FROM", "VON")), (self.to_var, L("TO", "BIS"))):
            tk.Label(yr, text=txt, bg=BG, fg=TEXT_MUTED, font=F("mono", 11)).pack(side="left", padx=(px(14), px(6)))
            e = tk.Entry(yr, textvariable=var, bg=BG2, fg=CYAN, insertbackground=CYAN, relief="flat", width=6,
                         font=F("mono", 13, False), highlightthickness=1, highlightcolor=CYAN,
                         highlightbackground=mix(CYAN, BG, 0.4), selectbackground=mix(CYAN, BG, 0.3))
            e.pack(side="left", ipady=px(4))
        tk.Label(body, text=L("UPPER/LOWER CASE IS IGNORED. A TERM MATCHES ANYWHERE IN THE TITLE.",
                              "GROSS-/KLEINSCHREIBUNG ZÄHLT NICHT. EIN BEGRIFF PASST ÜBERALL IM TITEL."),
                 bg=BG, fg=FLUFF, font=F("mono", 10, False), anchor="w").pack(fill="x", pady=(0, px(8)))

        th = tk.Frame(body, bg=BG)
        th.pack(fill="x", pady=(0, px(8)))
        tk.Label(th, text=L("THUMBNAIL  (OPTIONAL)", "THUMBNAIL  (OPTIONAL)"), bg=BG, fg=TEXT_MUTED, font=F("mono", 11),
                 anchor="w").pack(side="left")
        self.thumb_pending = None            # None = unverändert | Pfad = neues Bild | "" = entfernen
        self.thumb_key = list_key(lst) if lst else None
        rmb = CutButton(th, L("REMOVE", "ENTFERNEN"), self.thumb_remove, width=110)
        rmb.pack(side="right")
        CutButton(th, L("CHOOSE…", "AUSWÄHLEN…"), self.thumb_choose, width=120).pack(side="right", padx=(0, px(6)))
        self.thumb_info = tk.Label(th, text="", bg=BG, fg=TEXT_MUTED, font=F("mono", 11, False), anchor="w")
        self.thumb_info.pack(side="left", fill="x", expand=True, padx=(px(14), 0))
        self.thumb_rm = rmb
        self.thumb_refresh()

        self.count = tk.Label(body, text="", bg=BG, fg=CYAN, font=F("mono", 12), anchor="w")
        self.count.pack(fill="x")
        self.note = tk.Label(body, text="", bg=BG, fg=YELLOW, font=F("mono", 10, False), anchor="w", justify="left",
                             wraplength=px(700))
        self.note.pack(fill="x", pady=(0, px(4)))

        self.view = "hits"
        bar = tk.Frame(body, bg=BG)
        bar.pack(fill="x", pady=(0, px(6)))
        self.view_btns = {}
        for v_, txt_ in (("all", L("ALL VIDEOS", "ALLE VIDEOS")), ("hits", L("IN THE LIST", "IN DER LISTE"))):
            b = CutButton(bar, txt_, lambda v_=v_: self.set_view(v_), width=170)
            b.pack(side="left", padx=(0, px(6)))
            self.view_btns[v_] = b
        Tooltip(self.view_btns["all"], L("Shows every video of the channel, also those that do not match, so you can add single ones.",
                                         "Zeigt alle Videos des Kanals, auch die, die nicht passen, damit du einzelne aufnehmen kannst."))
        self.filter_var = tk.StringVar()
        fe = tk.Entry(bar, textvariable=self.filter_var, bg=BG2, fg=CYAN, insertbackground=CYAN, relief="flat",
                      font=F("mono", 12, False), highlightthickness=1, highlightcolor=CYAN,
                      highlightbackground=mix(CYAN, BG, 0.4), selectbackground=mix(CYAN, BG, 0.3), width=22)
        fe.pack(side="right", ipady=px(5))
        tk.Label(bar, text=L("FILTER", "FILTER"), bg=BG, fg=TEXT_MUTED, font=F("mono", 11)).pack(side="right", padx=(0, px(6)))
        Tooltip(fe, L("Searches all videos of the channel for this text (several terms separated by commas: any of them). "
                      "Videos that are not in the list yet are grey, select them and press INCLUDE.",
                      "Durchsucht alle Videos des Kanals nach diesem Text (mehrere Begriffe mit Komma: einer davon genügt). "
                      "Videos, die noch nicht in der Liste sind, sind grau: markieren und AUFNEHMEN drücken."))
        self.filter_var.trace_add("write", lambda *a: self.schedule())

        tools = tk.Frame(body, bg=BG)
        tools.pack(side="bottom", fill="x", pady=(px(6), 0))
        self.excl_btn = CutButton(tools, L("EXCLUDE SELECTED", "AUSSCHLIESSEN"), self.exclude_selected, width=190)
        self.excl_btn.pack(side="left")
        self.incl_btn = CutButton(tools, L("INCLUDE SELECTED", "AUFNEHMEN"), self.include_selected, width=190)
        self.incl_btn.pack(side="left", padx=(px(6), 0))
        self.add_var = tk.StringVar()
        ae = tk.Entry(tools, textvariable=self.add_var, bg=BG2, fg=CYAN, insertbackground=CYAN, relief="flat",
                      font=F("mono", 12, False), highlightthickness=1, highlightcolor=CYAN,
                      highlightbackground=mix(CYAN, BG, 0.4), selectbackground=mix(CYAN, BG, 0.3), width=22)
        CutButton(tools, L("ADD VIDEO", "VIDEO HINZU"), self.add_video, width=140).pack(side="right")
        ae.pack(side="right", padx=(0, px(6)), ipady=px(5))
        Tooltip(ae, L("Paste a YouTube link or video ID of a video of this channel to always include it.",
                      "YouTube-Link oder Video-ID eines Videos dieses Kanals einfügen, um es immer aufzunehmen."))
        ae.bind("<Return>", lambda e: (self.add_video(), "break")[1])

        wrap = tk.Frame(body, bg=BG)
        wrap.pack(fill="both", expand=True)
        sb = tk.Scrollbar(wrap, orient="vertical")
        self.lb = tk.Listbox(wrap, bg=BG2, fg=TEXT, selectbackground=mix(CYAN, BG, 0.3), selectforeground=TEXT,
                             font=F("mono", 11, False), relief="flat", highlightthickness=1,
                             highlightbackground=mix(CYAN, BG, 0.3), activestyle="none", exportselection=False,
                             selectmode="extended", yscrollcommand=sb.set)
        sb.configure(command=self.lb.yview)
        sb.pack(side="right", fill="y")
        self.lb.pack(side="left", fill="both", expand=True)
        self.lb.bind("<<ListboxSelect>>", lambda e: self.update_tool())
        self.lb.bind("<Control-a>", lambda e: (self.lb.selection_set(0, "end"), self.update_tool(), "break")[2])

        for v in (self.name_var, self.any_var, self.all_var, self.none_var, self.from_var, self.to_var):
            v.trace_add("write", lambda *a: self.schedule())
        self.bind("<Escape>", lambda e: self.destroy())
        self.set_view("hits")
        style_window(self)
        self.update_idletasks()
        x = app.winfo_rootx() + (app.winfo_width() - self.winfo_width()) // 2
        y = app.winfo_rooty() + max(0, (app.winfo_height() - self.winfo_height()) // 3)
        self.geometry(f"+{max(0, x)}+{max(0, y)}")
        self.grab_set()
        self.focus_force()

    def thumb_refresh(self):
        t = (load(thumbs_file(), {}).get(str(self.thumb_key)) or {}) if self.thumb_key else {}
        if self.thumb_pending:
            txt, col, has = L("NEW // ", "NEU // ") + Path(self.thumb_pending).name, CYAN, True
        elif self.thumb_pending == "":
            txt, col, has = L("WILL BE REMOVED", "WIRD ENTFERNT"), RED, False
        elif t:
            st = {"queued": L("QUEUED", "WARTET"), "done": L("UPLOADED", "HOCHGELADEN"), "error": L("ERROR", "FEHLER")}.get(t.get("status"), "?")
            txt, col, has = f"{st} // {t.get('file', '')}", {"done": GREEN, "error": RED}.get(t.get("status"), YELLOW), True
        else:
            txt, col, has = L("NONE", "KEINS"), TEXT_MUTED, False
        if len(txt) > 40:
            txt = txt[:18] + "…" + txt[-18:]
        self.thumb_info.configure(text=txt, fg=col)
        self.thumb_rm.set_enabled(has)

    def thumb_choose(self):
        f = filedialog.askopenfilename(parent=self, title="Thumbnail",
                                       filetypes=[(L("Images", "Bilder"), "*.jpg *.jpeg *.png"),
                                                  (L("All files", "Alle Dateien"), "*.*")])
        if not f:
            return
        p_ = Path(f)
        if p_.suffix.lower() not in (".jpg", ".jpeg", ".png"):
            self.note.configure(text=L("THUMBNAIL: ONLY JPG OR PNG.", "THUMBNAIL: NUR JPG ODER PNG."), fg=RED)
            return
        if p_.stat().st_size > THUMB_MAX_BYTES:
            self.note.configure(text=L("THUMBNAIL: FILE TOO LARGE (MAX 2 MB).", "THUMBNAIL: DATEI ZU GROSS (MAX 2 MB)."), fg=RED)
            return
        self.thumb_pending = str(p_)
        self.thumb_refresh()

    def thumb_remove(self):
        self.thumb_pending = ""
        self.thumb_refresh()

    def destroy(self):
        parent = getattr(self, "parent_dlg", None)
        super().destroy()
        if parent is not None:
            try:
                if parent.winfo_exists():
                    parent.grab_set()
                    parent.focus_force()
            except tk.TclError:
                pass

    def field(self, parent, label, var):
        tk.Label(parent, text=label, bg=BG, fg=TEXT_MUTED, font=F("mono", 11), anchor="w").pack(fill="x")
        e = tk.Entry(parent, textvariable=var, bg=BG2, fg=CYAN, insertbackground=CYAN, relief="flat",
                     font=F("mono", 13, False), highlightthickness=1, highlightcolor=CYAN,
                     highlightbackground=mix(CYAN, BG, 0.4), selectbackground=mix(CYAN, BG, 0.3))
        e.pack(fill="x", pady=(px(3), px(8)), ipady=px(5))
        return e

    def current(self):
        def year_of(var):
            m_ = re.fullmatch(r"\s*(\d{4})\s*", var.get())
            return int(m_.group(1)) if m_ else None
        return {"any": parse_terms(self.any_var.get()), "all": parse_terms(self.all_var.get()),
                "none": parse_terms(self.none_var.get()), "from": year_of(self.from_var), "to": year_of(self.to_var),
                "add": sorted(self.add), "skip": sorted(self.skip)}

    def schedule(self):
        if self.after_id:
            self.after_cancel(self.after_id)
        self.after_id = self.after(220, self.refresh)

    def line(self, v):
        d = v.get("published")
        when = fmt_day(d) if d else str(v["year"])
        return f"{when:>11}  {self.titles.get(v['id']) or v['id']}"

    def state_of(self, v, cur):
        """('hit' | 'add' | 'skip' | 'out', passt_per_regel) – ob das Video in der Liste ist."""
        vid = v["id"]
        rule = in_years(cur, v["year"]) and title_matches(cur, self.titles.get(vid, ""))
        if vid in self.skip:
            return "skip", rule
        if vid in self.add:
            return "add", rule
        return ("hit" if rule else "out"), rule

    def set_view(self, view):
        self.view = view
        for v_, b in self.view_btns.items():
            b.set_color(GREEN if v_ == view else YELLOW)
            b.set_text(("● " if v_ == view else "") + {"hits": L("IN THE LIST", "IN DER LISTE"),
                                                     "all": L("ALL VIDEOS", "ALLE VIDEOS")}[v_])
        self.lb.selection_clear(0, "end")
        self.refresh()

    def refresh(self):
        self.after_id = None
        cur = self.current()
        sel = {self.shown[i][1] for i in self.lb.curselection() if i < len(self.shown)}
        top = self.lb.yview()[0]
        self.lb.delete(0, "end")
        self.shown = []
        qs = parse_terms(self.filter_var.get())           # mehrere Begriffe mit Komma = einer davon genügt
        show_all = self.view == "all" or bool(qs)          # mit Filter wird immer alles durchsucht (auch Nicht-Treffer)
        self.lb.configure(fg=TEXT_MUTED if show_all else TEXT)
        self.show_all = show_all
        hits = skipped = 0
        capped = False
        skips = []
        for v in self.pool:
            st, _ = self.state_of(v, cur)
            if st in ("hit", "add"):
                hits += 1
            elif st == "skip":
                skipped += 1
            if qs:
                t_ = norm_text(self.titles.get(v["id"], ""))
                if not any(q in t_ or q == v["id"].lower() for q in qs):
                    continue
            if not show_all:
                if st == "skip":
                    skips.append((st, v))
                    continue
                if st == "out":
                    continue
            if len(self.shown) >= self.SHOW_MAX:
                capped = True
                continue
            self.add_line(st, v)
        for st, v in skips:                       # ausgeschlossene stehen in dieser Ansicht am Ende
            if len(self.shown) < self.SHOW_MAX:
                self.add_line(st, v)
        miss = len(missing_titles(self.pool))
        span = sum(1 for v in self.pool if in_years(cur, v["year"]))
        self.count.configure(text=L(f"{hits} OF {span} VIDEOS MATCH", f"{hits} VON {span} VIDEOS PASSEN")
                             + (L(f"  //  {skipped} EXCLUDED", f"  //  {skipped} AUSGESCHLOSSEN") if skipped else ""))
        notes = []
        if not self.pool:
            notes.append(L("NO VIDEOS FOUND YET. RUN THE YEARS FIRST.", "NOCH KEINE VIDEOS GEFUNDEN. ZUERST DIE JAHRE LAUFEN LASSEN."))
        elif miss:
            notes.append(L(f"{miss} TITLES ARE STILL MISSING AND ARE LOADED ON START. THE PREVIEW IS INCOMPLETE UNTIL THEN.",
                           f"{miss} TITEL FEHLEN NOCH UND WERDEN BEIM START NACHGELADEN. BIS DAHIN IST DIE VORSCHAU UNVOLLSTÄNDIG."))
        if qs and self.view == "hits":
            notes.append(L("THE FILTER SEARCHES ALL VIDEOS // GREY = NOT IN THE LIST YET, SELECT AND PRESS INCLUDE.",
                           "DER FILTER DURCHSUCHT ALLE VIDEOS // GRAU = NOCH NICHT IN DER LISTE, MARKIEREN UND AUFNEHMEN."))
        if capped:
            notes.append(L(f"ONLY THE FIRST {self.SHOW_MAX} ARE SHOWN // USE THE FILTER.", f"ES WERDEN NUR DIE ERSTEN {self.SHOW_MAX} GEZEIGT // NUTZE DEN FILTER."))
        if hits > SHOW_LIMIT_NOTE:
            notes.append(L(f"MORE THAN {PART_MAX} VIDEOS ARE SPLIT INTO SEVERAL PLAYLIST PARTS.",
                           f"AB {PART_MAX} VIDEOS WIRD DIE PLAYLIST IN TEILE AUFGETEILT."))
        self.note.configure(text="\n".join(notes), fg=YELLOW)
        for i, (_, vid) in enumerate(self.shown):
            if vid in sel:
                self.lb.selection_set(i)
        self.lb.yview_moveto(top)
        self.update_tool()

    def add_line(self, st, v):
        mark = {"hit": "  ", "add": "+ ", "skip": "✕ ", "out": "  "}[st]
        self.lb.insert("end", mark + self.line(v))
        col = {"add": CYAN, "skip": RED, "hit": TEXT if self.show_all else None}.get(st)
        if col:
            self.lb.itemconfig("end", fg=col)
        self.shown.append((st, v["id"]))

    def picked(self):
        return [self.shown[i] for i in self.lb.curselection() if i < len(self.shown)]

    def update_tool(self):
        pick = self.picked()
        ex = [p for p in pick if p[0] in ("hit", "add")]
        inc = [p for p in pick if p[0] in ("out", "skip")]
        self.excl_btn.set_enabled(bool(pick))         # nie "tot": bei unpassender Auswahl erklärt eine Meldung, warum nichts passiert
        self.incl_btn.set_enabled(bool(pick))
        self.excl_btn.set_text(L("EXCLUDE SELECTED", "AUSSCHLIESSEN") + (f" ({len(ex)})" if len(ex) > 1 else ""))
        self.incl_btn.set_text(L("INCLUDE SELECTED", "AUFNEHMEN") + (f" ({len(inc)})" if len(inc) > 1 else ""))

    def exclude_selected(self):
        cur = self.current()
        by_id = {v["id"]: v for v in self.pool}
        if not any(st in ("hit", "add") for st, _ in self.picked()):
            self.note.configure(text=L("NOTHING TO EXCLUDE: THE SELECTED VIDEOS ARE NOT IN THE LIST.",
                                       "NICHTS ZUM AUSSCHLIESSEN: DIE MARKIERTEN VIDEOS SIND NICHT IN DER LISTE."), fg=YELLOW)
            return
        for st, vid in self.picked():
            if st not in ("hit", "add"):
                continue
            self.add.discard(vid)
            if self.state_of(by_id[vid], cur)[1]:
                self.skip.add(vid)             # passt nach den Regeln -> ausdrücklich ausschließen
        self.refresh()

    def include_selected(self):
        cur = self.current()
        by_id = {v["id"]: v for v in self.pool}
        if not any(st in ("out", "skip") for st, _ in self.picked()):
            self.note.configure(text=L("NOTHING TO INCLUDE: THE SELECTED VIDEOS ARE ALREADY IN THE LIST. "
                                       "TO ADD OTHERS, FIND THEM WITH THE FILTER OR IN ALL VIDEOS (GREY = NOT IN THE LIST).",
                                       "NICHTS ZUM AUFNEHMEN: DIE MARKIERTEN VIDEOS SIND SCHON IN DER LISTE. ANDERE FINDEST DU "
                                       "MIT DEM FILTER ODER IN ALLE VIDEOS (GRAU = NICHT IN DER LISTE)."), fg=YELLOW)
            return
        for st, vid in self.picked():
            if st not in ("out", "skip"):
                continue
            self.skip.discard(vid)
            if not self.state_of(by_id[vid], cur)[1]:
                self.add.add(vid)              # passt nicht nach den Regeln -> fest aufnehmen
        self.refresh()

    def add_video(self):
        raw = self.add_var.get().strip()
        m_ = re.search(r"(?:v=|youtu\.be/|shorts/|live/)([\w-]{11})", raw) or re.fullmatch(r"([\w-]{11})", raw)
        vid = m_.group(1) if m_ else None
        if not vid or vid not in {v["id"] for v in self.pool}:
            self.note.configure(text=L("THAT VIDEO IS NOT AMONG THE CHANNEL'S FOUND VIDEOS (SHORTS ARE NOT INCLUDED).",
                                       "DIESES VIDEO GEHÖRT NICHT ZU DEN GEFUNDENEN VIDEOS DES KANALS (SHORTS SIND NICHT DABEI)."), fg=RED)
            return
        self.note.configure(fg=YELLOW)
        self.skip.discard(vid)
        self.add.add(vid)
        self.add_var.set("")
        self.refresh()

    def save(self):
        name = self.name_var.get().strip()
        cur = self.current()
        if not name:
            self.note.configure(text=L("PLEASE ENTER A NAME.", "BITTE EINEN NAMEN EINGEBEN."), fg=RED)
            return
        if not (cur["any"] or cur["all"]):
            self.note.configure(text=L("PLEASE ENTER AT LEAST ONE KEYWORD.", "BITTE MINDESTENS EIN STICHWORT EINGEBEN."), fg=RED)
            return
        lists = load_lists()
        if self.lst_id:
            lst = next((x for x in lists if x["id"] == self.lst_id), None)
        else:
            lst = None
        if lst is None:
            lst = {"id": self.lst_id or new_list_id()}
            lists.append(lst)
        lst.update(name=name, **cur)
        save_lists(lists)
        key = list_key(lst)
        if self.thumb_pending is not None:
            try:
                store_thumbnail(key, self.thumb_pending or None)
            except OSError as e:
                self.app.write_log(L(f"THUMBNAIL NOT SAVED // {e}", f"THUMBNAIL NICHT GESPEICHERT // {e}"), "err")
        self.app.lists_changed(select_key=key if not self.lst_id else None)
        if self.app.worker and self.app.worker.is_alive():
            self.app.write_log(L("LIST CHANGES APPLY FROM THE NEXT START", "LISTENÄNDERUNGEN GELTEN AB DEM NÄCHSTEN START"), "info")
        self.destroy()

    def delete(self):
        if not self.confirm_delete:
            self.confirm_delete = True
            self.del_btn.set_text(L("SURE? CLICK AGAIN", "SICHER? NOCHMAL KLICKEN"))
            return
        lists = [x for x in load_lists() if x["id"] != self.lst_id]
        save_lists(lists)
        key = "k_" + self.lst_id
        pids = part_ids(load(state_file(key), {}))
        for f in (videos_file(key), state_file(key)):
            try:
                f.unlink()
            except OSError:
                pass
        self.app.lists_changed(removed_key=key)
        self.app.write_log(L("LIST DELETED // ITS PLAYLIST STAYS ON YOUTUBE UNTIL YOU DELETE IT THERE",
                             "LISTE GELÖSCHT // IHRE PLAYLIST BLEIBT AUF YOUTUBE, BIS DU SIE DORT LÖSCHST") +
                           (" // " + " ".join(f"https://www.youtube.com/playlist?list={p}" for p in pids) if pids else ""), "warn")
        self.destroy()


SHOW_LIMIT_NOTE = PART_MAX


class LoginPanel(Panel):
    def __init__(self, master, on_url, on_copy):
        super().__init__(master, 116, state="frame")
        self.url, self.code, self.on_url, self.on_copy = "", "", on_url, on_copy

    def draw_content(self, x0, y0, x1, y1, c, role):
        x = x0 + px(14)
        self.create_rectangle(x, y0 + px(14), x + px(6), y0 + px(20), fill=RED, outline="")
        self.create_text(x + px(14), y0 + px(17), text=L("GOOGLE AUTH // LINK REQUIRED", "GOOGLE-ANMELDUNG // LINK ÖFFNEN"), anchor="w",
                         font=F("ui", 15), fill=RED)
        self.create_text(x, y0 + px(46), text=L("OPEN", "ÖFFNEN"), anchor="w", font=F("mono", 11), fill=TEXT_MUTED)
        link = self.create_text(x + px(70), y0 + px(46), text=self.url, anchor="w", font=F("mono", 12),
                                fill=CYAN)
        self.tag_bind(link, "<Button-1>", lambda e: self.on_url())
        self.tag_bind(link, "<Enter>", lambda e: (self.configure(cursor="hand2"),
                                                 self.itemconfigure(link, fill=CYAN_HOT)))
        self.tag_bind(link, "<Leave>", lambda e: (self.configure(cursor=""),
                                                 self.itemconfigure(link, fill=CYAN)))
        self.create_text(x, y0 + px(80), text=L("ENTER CODE", "CODE"), anchor="w", font=F("mono", 11), fill=TEXT_MUTED)
        code = self.create_text(x + px(110), y0 + px(80), text=self.code, anchor="w", font=F("mono", 24),
                                fill=YELLOW)
        hint = self.create_text(self.bbox(code)[2] + px(14), y0 + px(82), text=L("CLICK TO COPY", "KLICKEN ZUM KOPIEREN"), anchor="w",
                                font=F("mono", 10, False), fill=FLUFF)
        for item in (code, hint):
            self.tag_bind(item, "<Button-1>", lambda e: self.copied(hint))
            self.tag_bind(item, "<Enter>", lambda e: (self.configure(cursor="hand2"),
                                                     self.itemconfigure(code, fill=mix("#ffffff", YELLOW, 0.35))))
            self.tag_bind(item, "<Leave>", lambda e: (self.configure(cursor=""),
                                                     self.itemconfigure(code, fill=YELLOW)))

    def copied(self, hint):
        self.on_copy()
        self.itemconfigure(hint, text=L("COPIED ✓", "KOPIERT ✓"), fill=GREEN)
        self.after(1500, lambda: self.itemconfigure(hint, text=L("CLICK TO COPY", "KLICKEN ZUM KOPIEREN"), fill=FLUFF))


class CutButton(tk.Canvas):
    """Eckiger Button mit einer abgeschnittenen Ecke unten rechts (wie die Panels)."""

    def __init__(self, master, text, command, width=0, height=36, size=12):
        f = tkfont.Font(family=FONT_MONO, size=-px(size), weight="bold")
        w = max(px(width), f.measure(text) + px(36))
        super().__init__(master, width=w, height=px(height), bg=BG, highlightthickness=0, bd=0, cursor="hand2")
        self.text, self.command, self.size = text, command, size
        self.enabled, self.hover = True, False
        self.color = YELLOW      # Aktions-Knöpfe sind gelb
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
        m, c = px(3), px(10) if h > px(50) else px(7)
        x0, y0, x1, y1 = m, m, w - m - 1, h - m - 1
        col = self.color
        if not self.enabled:
            fill, edge, tcol = mix(col, BG, 0.06), mix(col, BG, 0.35), mix(col, BG, 0.5)
        elif self.hover:
            fill, edge, tcol = mix(col, BG, 0.22), mix("#ffffff", col, 0.25), mix("#ffffff", col, 0.25)
            for d, a in ((1, 0.35), (2, 0.15)):
                o = shape(x0 - d, y0 - d, x1 + d, y1 + d, c + 0.586 * d)
                self.create_polygon(flat(o), fill="", outline=mix(col, BG, a))
        else:
            fill, edge, tcol = mix(col, BG, 0.20), mix(col, BG, 0.9), col
        self.create_polygon(flat(shape(x0, y0, x1, y1, c)), fill=fill, outline=edge)
        self.create_text(w / 2, h / 2, text=self.text, font=F("mono", self.size), fill=tcol)


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


class YearSection(Section):
    """Abschnitt JAHRE mit Gesamtfortschritt und ALLE / KEINE zum Markieren."""

    def __init__(self, master, text, on_all, on_none):
        super().__init__(master, text)
        self.on_all, self.on_none = on_all, on_none
        self.total = (0, 0)

    def set_total(self, done, total):
        if (done, total) != self.total:
            self.total = (done, total)
            self.redraw()

    def link(self, x, y, text, cb):
        t = self.create_text(x, y, text=text, anchor="e", font=F("mono", 11), fill=YELLOW)
        self.tag_bind(t, "<Button-1>", lambda e: cb())
        self.tag_bind(t, "<Enter>", lambda e: (self.configure(cursor="hand2"), self.itemconfigure(t, fill="#ffffff")))
        self.tag_bind(t, "<Leave>", lambda e: (self.configure(cursor=""), self.itemconfigure(t, fill=YELLOW)))
        return self.bbox(t)[0]

    def redraw(self):
        self.delete("all")
        w = self.winfo_width()
        y = px(13)
        self.create_rectangle(0, y - px(3), px(6), y + px(3), fill=RED, outline="")
        t = self.create_text(px(14), y, text=self.text, anchor="w", font=F("mono", 12), fill=RED)
        x = self.bbox(t)[2] + px(10)
        right = w - px(4)
        right = self.link(right, y, L("NONE", "KEINE"), self.on_none) - px(6)
        sep = self.create_text(right, y, text="·", anchor="e", font=F("mono", 11), fill=FLUFF)
        right = self.bbox(sep)[0] - px(6)
        right = self.link(right, y, L("ALL", "ALLE"), self.on_all) - px(16)
        done, total = self.total
        if total:
            pct = done * 100 / total
            ptxt = (f"{pct:.1f}".replace(".", "," if LANG == "de" else ".")) + " %"
            p = self.create_text(right, y, text=ptxt, anchor="e", font=F("mono", 12), fill=CYAN)
            right = self.bbox(p)[0] - px(8)
            bw = px(90)
            if right - bw > x + px(60):
                self.create_rectangle(right - bw, y - px(2), right, y + px(2), fill=mix(CYAN, BG, 0.2), outline="")
                self.create_rectangle(right - bw, y - px(2), right - bw + bw * done / total, y + px(2),
                                      fill=CYAN, outline="")
                right -= bw + px(8)
            g = self.create_text(right, y, text=L("TOTAL", "GESAMT"), anchor="e", font=F("mono", 10, False), fill=FLUFF)
            right = self.bbox(g)[0] - px(10)
        if right > x:
            self.create_line(x, y, right, y, fill=RED_MILD)


class Header(tk.Canvas):
    def __init__(self, master):
        super().__init__(master, height=px(88), bg=BG, highlightthickness=0, bd=0)
        self.chip = StatusChip(self)
        self.api_btn = None      # wird von der App gesetzt
        self.help_btn = None
        self.upd_btn = None      # grüner UPDATE-Knopf, von der App gesetzt
        self.update = None       # (version, url) wenn eine neuere Release existiert
        self.title_end = 0
        self.bind("<Configure>", lambda e: self.redraw())

    def redraw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        lsz = px(54)
        draw_logo(self, px(18), px(18), lsz)
        x, y = px(18) + lsz + px(16), px(34)
        title = "\u200a".join("CHRONOLIST")
        f = F("ui", 26)
        t = self.create_text(x, y, text=title, anchor="w", font=f, fill=RED, tags="title")
        # Versions-Tag rechts neben dem Titel
        vx0 = self.bbox(t)[2] + px(14)
        vtxt = f"v{VERSION}"
        vf = tkfont.Font(family=FONT_MONO, size=-px(12), weight="bold")
        vx1 = vx0 + vf.measure(vtxt) + px(18)
        vy0, vy1, vc = y - px(11), y + px(11), px(6)
        self.create_polygon(flat(shape(vx0, vy0, vx1, vy1, vc)), fill=mix(YELLOW, BG, 0.12),
                            outline=mix(YELLOW, BG, 0.75))
        self.create_text((vx0 + vx1) / 2, y, text=vtxt, font=vf, fill=YELLOW)
        bt = self.create_text(vx1 + px(10), y, text=f"BUILD {BUILD:04d}", anchor="w",
                              font=F("mono", 10, False), fill=FLUFF, tags="buildtxt")
        self.title_end = self.bbox(bt)[2] + px(20)
        tag_end = vx1 + px(16)       # weiter links dürfen die Knöpfe nie
        sub_t = self.create_text(x, px(64), text=f"{L('PLAYLIST UPLINK', 'PLAYLIST-UPLINK')} // {(CHANNEL.get('handle') or L('NO TARGET', 'KEIN ZIEL')).upper()} // {L('UPLOAD ORDER', 'UPLOAD-REIHENFOLGE')}", anchor="w",
                         font=F("mono", 11), fill=TEXT_MUTED)
        self.title_end = max(self.title_end, self.bbox(sub_t)[2] + px(20))

        # Platz prüfen: passt alles in voller Größe? Sonst Status schmaler und HILFE nur als "?"
        upd = self.upd_btn if self.update else None
        gap = px(8)
        full_help = L("HELP", "HILFE")

        def need(chip_w, help_w):
            n = chip_w + px(12) + help_w + px(16)
            if self.api_btn is not None:
                n += self.api_btn.winfo_reqwidth() + gap
            if upd is not None:
                n += upd.full_w + gap
            return n
        help_full_w = px(86)
        compact = upd is not None and w - px(18) - need(px(210), help_full_w) < tag_end
        if upd is not None:
            upd.configure(width=upd.full_w)
            upd.set_text(upd.full_text)
            if compact and w - px(18) - need(px(150), px(40)) < tag_end:
                upd.configure(width=upd.short_w)
                upd.set_text(upd.short_text)
        if self.help_btn is not None:
            if compact:
                self.help_btn.configure(width=px(40))
                self.help_btn.set_text("?")
            else:
                self.help_btn.configure(width=max(help_full_w, self.help_btn.winfo_reqwidth()))
                self.help_btn.set_text(full_help)
        chip_w = px(150) if compact else px(210)
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
            bw_ = int(self.help_btn.cget("width"))
            self.create_window(bx, px(46), window=self.help_btn, anchor="e", width=bw_)
            bx -= bw_ + gap
        if upd is not None:
            bw_ = int(upd.cget("width"))
            self.create_window(bx, px(46), window=upd, anchor="e", width=bw_)
            bx -= bw_ + gap
        bx -= px(8)
        # BUILD-Hinweis weglassen, wenn er sonst unter die Knöpfe läuft
        bb = self.bbox("buildtxt")
        if bb and bb[2] > bx + px(8):
            self.delete("buildtxt")
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
        self._is_scrollarea = True
        root = self.winfo_toplevel()._root()
        if not getattr(root, "_wheel_dispatch", False):
            # Ein zentraler Mausrad-Handler: scrollt die ScrollArea, über der sich die Maus gerade befindet –
            # egal über welchem Kind-Element (Label, Knopf, Karte).
            root._wheel_dispatch = True
            root.bind_all("<MouseWheel>", lambda e: ScrollArea._dispatch(e, int(-e.delta / 120)), add="+")
            root.bind_all("<Button-4>", lambda e: ScrollArea._dispatch(e, -1), add="+")
            root.bind_all("<Button-5>", lambda e: ScrollArea._dispatch(e, 1), add="+")

    @staticmethod
    def _dispatch(e, steps):
        try:
            w = e.widget.winfo_containing(e.x_root, e.y_root)
        except (tk.TclError, AttributeError, KeyError):
            return
        while w is not None:
            if getattr(w, "_is_scrollarea", False):
                if steps:
                    w.canvas.yview_scroll(steps, "units")
                return
            w = getattr(w, "master", None)

    def wheel(self, widget):
        """Nicht mehr nötig (zentraler Handler), bleibt für bestehende Aufrufe."""


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


def style_window(win):
    """Dunkle Titelleiste und kein durchscheinender Hintergrund – auch nach dem Anzeigen noch einmal setzen."""
    style_titlebar(win)
    for ms in (50, 300, 1000, 2500):
        win.after(ms, lambda: win.winfo_exists() and style_titlebar(win))


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
        self.restore_window()
        global LANG
        lang = self.settings.get("language")
        LANG = lang if lang in LANGUAGES else detect_language()
        if "language" not in self.settings and self.settings.get("channel"):
            # Bestehende Installation von vor der Sprachauswahl: bisherige (deutsche) Standard-Vorlagen festschreiben,
            # damit neue Playlists genauso heißen wie die schon vorhandenen.
            self.settings.setdefault("playlist_template", "{channel} {year} in Upload-Reihenfolge")
            self.settings.setdefault("description_template", "Alle Videos von {channel} aus {year} in Upload-Reihenfolge.")
            self.settings["language"] = LANG
            save(SETTINGS_FILE, self.settings)
        apply_templates(self.settings)
        set_channel(self.settings.get("channel"))
        if CHANNEL and not CHANNEL.get("id") and known_channel_id():
            # ältere Installationen: Kanal-ID aus vorhandenen Scan-Daten nachtragen
            CHANNEL["id"] = known_channel_id()
            self.settings["channel"]["id"] = CHANNEL["id"]
            save(SETTINGS_FILE, self.settings)
        self.out = queue.Queue()
        self.worker = None
        self.code = ""
        self.login_url = ""

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
        Section(left, L("TARGET CHANNEL", "ZIELKANAL"), "YOUTUBE // HANDLE").grid(row=0, column=0, sticky="ew", pady=(0, px(6)))
        self.channel_card = ChannelCard(left, self.edit_channel)
        self.channel_card.grid(row=1, column=0, sticky="ew", pady=(0, px(16)))
        self.channel_card.update_state()
        self.year_section = YearSection(left, L("TARGETS", "ZIELE"), lambda: self.select_all(True),
                                        lambda: self.select_all(False))
        self.year_section.grid(row=2, column=0, sticky="ew", pady=(0, px(6)))
        self.scroll = ScrollArea(left)
        self.scroll.grid(row=3, column=0, sticky="nsew")
        # Kanalkarte und Überschrift enden rechts bündig mit den Jahreskarten (nicht mit der Scrollleiste)
        sbw = self.scroll.sb.winfo_reqwidth() + px(6)
        self.channel_card.grid_configure(padx=(0, sbw))
        self.year_section.grid_configure(padx=(0, sbw))
        self.new_list_btn = CutButton(left, L("+  CUSTOM LIST", "+  EIGENE LISTE"), self.new_list, height=px(36))
        self.new_list_btn.set_color(CYAN)
        self.new_list_btn.grid(row=4, column=0, sticky="ew", pady=(px(8), 0), padx=(0, sbw))
        Tooltip(self.new_list_btn, L("Your own playlist besides the years: collects all videos whose title contains your keywords (e.g. a series), in upload order.",
                                     "Eine eigene Playlist neben den Jahren: sammelt alle Videos, in deren Titel deine Stichwörter vorkommen (z. B. eine Serie), in Upload-Reihenfolge."))
        self.rows = {}
        self.order = []
        self.build_rows()

        # --- rechts: Steuerung, Optionen, Login, Log
        right = tk.Frame(body, bg=BG)
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(6, weight=1)

        Section(right, L("CONTROL", "STEUERUNG"), "HUD // STATUS").grid(row=0, column=0, sticky="ew", pady=(0, px(6)))
        btns = tk.Frame(right, bg=BG)
        btns.grid(row=1, column=0, sticky="ew", pady=(0, px(16)))
        for i in range(4):
            btns.columnconfigure(i, weight=1, uniform="ctl")
        self.start_btn = CutButton(btns, "▶  START", self.start, height=DECK_H, size=15)
        self.pause_btn = CutButton(btns, L("❚❚  PAUSE", "❚❚  PAUSE"), self.toggle_pause, height=DECK_H, size=15)
        self.pause_btn.set_enabled(False)
        self.stop_btn = CutButton(btns, L("■  STOP", "■  STOPP"), self.stop, height=DECK_H, size=15)
        self.check_btn = CutButton(btns, L("✓  CHECK", "✓  PRÜFEN"), self.run_check, height=DECK_H, size=15)
        for i, b in enumerate((self.start_btn, self.pause_btn, self.stop_btn, self.check_btn)):
            b.configure(width=1)
            b.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else px(3), 0 if i == 3 else px(3)))
        for b in (self.start_btn, self.pause_btn, self.stop_btn, self.check_btn):
            b.set_color(YELLOW)   # Hauptsteuerung in Cyberpunk-Gelb
        self.setup_btn = CutButton(self.header, L("API // MISSING", "API // ANMELDEN"), self.edit_credentials, width=150)
        self.header.api_btn = self.setup_btn
        self.help_btn = CutButton(self.header, L("HELP", "HILFE"), self.show_help, width=86)
        self.header.help_btn = self.help_btn
        self.header.redraw()
        self.api_ok = None   # None = unbekannt, True = Login/Aufruf erfolgreich, False = Fehler
        self.update_api_button()
        self.stop_btn.set_enabled(False)

        Section(right, L("OPTIONS", "OPTIONEN"), "RUN // SYSTEM").grid(row=3, column=0, sticky="ew", pady=(0, px(6)))
        opts = tk.Frame(right, bg=BG)
        opts.grid(row=4, column=0, sticky="ew", pady=(0, px(16)))
        opts.columnconfigure(0, weight=1, uniform="opt")
        opts.columnconfigure(1, weight=1, uniform="opt")
        # Zeile 1: Ablauf
        self.fallback = bool(self.settings.get("browser_fallback", False))
        global FALLBACK_ON
        FALLBACK_ON = self.fallback
        for r in self.rows.values():
            r.refresh_blocked()
        self.fallback_card = ToggleCard(opts, "FALLBACK", L("EDGE AFTER QUOTA", "EDGE BEI LIMIT"),
                                        self.fallback, self.toggle_fallback, compact=True)
        self.fallback_card.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, px(4)))
        self.skip_ahead = True                       # immer: nächstes bereites markiertes Jahr vorziehen
        self.autorun = bool(self.settings.get("autorun", False))
        self.win_on = get_windows_autostart()
        self.app_card = SettingsCard(opts, self.edit_app_settings, lambda: L("PROGRAM", "PROGRAMM"),
                                     lambda: " // ".join([LANGUAGES[LANG]]
                                                         + ([L("AUTO-RUN", "AUTOSTART")] if self.autorun else [])
                                                         + (["BOOT"] if self.win_on else [])), icon="gear")
        self.app_card.grid(row=1, column=0, sticky="ew", padx=(0, px(3)))
        self.settings_card = SettingsCard(opts, self.edit_settings, lambda: "PLAYLISTS",
                                          lambda: f"{privacy_label(PRIVACY)} // "
                                                  + fill_template(PLAYLIST_TEMPLATE, YEARS[0] if YEARS
                                                                  else datetime.date.today().year - 1))
        self.settings_card.grid(row=1, column=1, sticky="ew", padx=(px(3), 0))

        self.login = tk.Frame(right, bg=BG)
        self.login.columnconfigure(0, weight=1)
        self.login_panel = LoginPanel(self.login, self.open_login, self.copy_code)
        self.login_panel.grid(row=0, column=0, sticky="ew")
        lb = tk.Frame(self.login, bg=BG)
        lb.grid(row=1, column=0, sticky="w", pady=(px(4), px(8)))
        b_open = CutButton(lb, L("OPEN LINK", "LINK ÖFFNEN"), self.open_login)
        b_open.pack(side="left")
        b_copy = CutButton(lb, L("COPY CODE", "CODE KOPIEREN"), self.copy_code)
        b_copy.pack(side="left", padx=(px(8), 0))
        Tooltip(b_open, L("Opens the Google sign-in page in your browser.", "Öffnet die Google-Anmeldeseite im Browser."))
        Tooltip(b_copy, L("Copies the sign-in code to the clipboard. Paste it on the Google page and confirm.",
                          "Kopiert den Anmeldecode in die Zwischenablage. Auf der Google-Seite einfügen und bestätigen."))

        logwrap = tk.Frame(right, bg=BG)
        logwrap.grid(row=6, column=0, sticky="nsew")
        logwrap.columnconfigure(0, weight=1)
        logwrap.rowconfigure(1, weight=1)
        Section(logwrap, L("SYSTEM LOG", "PROTOKOLL"), "NET // 0x7F3A").grid(row=0, column=0, sticky="ew", pady=(0, px(6)))
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
                         ("err", RED), ("sep", RED), ("leaf", GREEN), ("loglink", YELLOW)):
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
        self.write_log(L("CHRONOLIST ONLINE // SYSTEMS NOMINAL", "CHRONOLIST ONLINE // ALLE SYSTEME BEREIT"), "info")
        self.show_post_update()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(200, self.poll)
        self.after(40, self.animate)
        if self.settings.get("check_updates", True):
            threading.Thread(target=check_for_update, args=(self.out,), daemon=True).start()
        self.cleanup_tries = 0
        self.after(2000, self.cleanup_tick)
        if self.autorun:
            self.after(1500, self.start)

    # --- Einstellungen
    def selected_years(self):
        """Markierte Jahre in der Reihenfolge der Liste (per Drag & Drop änderbar)."""
        return [y for y in self.order if self.rows[y].selected]

    def save_settings(self):
        self.settings.update(years=self.selected_years(), year_order=list(self.order), autorun=self.autorun,
                             browser_fallback=getattr(self, "fallback", False),
                             )
        save(SETTINGS_FILE, self.settings)

    def new_list(self):
        ListDialog(self, None)

    def edit_list(self, key):
        if find_list(key):
            ListDialog(self, key)

    def lists_changed(self, select_key=None, removed_key=None):
        """Nach Anlegen/Ändern/Löschen einer Liste die Karten neu aufbauen (Auswahl bleibt)."""
        sel = set(self.selected_years())
        if select_key:
            sel.add(select_key)
        sel.discard(removed_key)
        self.settings["years"] = [y for y in self.order if y in sel] + [y for y in sel if y not in self.order]
        self.settings["year_order"] = [y for y in self.order if y != removed_key]
        save(SETTINGS_FILE, self.settings)
        self.build_rows()
        for r in self.rows.values():
            r.refresh_blocked()
        self.save_settings()

    def toggle_year(self, y):
        row = self.rows[y]
        row.refresh(selected=not row.selected)
        self.save_settings()

    def select_all(self, on):
        for r in self.rows.values():
            if r.selected != on:
                r.refresh(selected=on)
        self.save_settings()
        if self.worker and self.worker.is_alive():
            self.write_log(L("SELECTION APPLIES FROM THE NEXT START", "AUSWAHL GILT AB DEM NÄCHSTEN START"), "info")

    def update_total(self):
        done = total = 0
        for y, r in self.rows.items():
            if is_list_key(y):
                continue                 # Listen wiederholen Videos der Jahre, zählen nicht in die Gesamtzahl
            done += r.info.get("done", 0)
            total += r.info.get("total", 0)
        if hasattr(self, "year_section"):
            self.year_section.set_total(done, total)

    def set_autorun(self, on):
        self.autorun = bool(on)
        self.save_settings()

    def edit_app_settings(self):
        AppSettingsDialog(self)          # auch während eines Laufs (Sprachwechsel startet erst danach neu)

    def edit_settings(self):
        SettingsDialog(self)             # auch während eines Laufs (gilt für ab dann angelegte Playlists)

    def toggle_fallback(self):
        global FALLBACK_ON
        self.fallback = not self.fallback
        FALLBACK_ON = self.fallback
        for r in self.rows.values():
            r.refresh_blocked()
        self.fallback_card.update_value(self.fallback)
        self.save_settings()
        if self.worker and self.worker.is_alive():
            self.worker.fallback = self.fallback

    def set_windows(self, on):
        if bool(on) == self.win_on:
            return
        try:
            set_windows_autostart(bool(on))
        except OSError as e:
            self.write_log(L(f"BOOT SEQUENCE UPDATE FAILED // {e}", f"START MIT WINDOWS KONNTE NICHT GEÄNDERT WERDEN // {e}"), "err")
            return
        self.win_on = bool(on)

    # --- Status
    def set_status(self, key):
        text, role, state, blink = {
            "idle": (L("STANDBY", "BEREIT"), CYAN, "inactive", False),
            "running": (L("RUNNING", "LÄUFT"), GREEN, "running", False),
            "browser": (L("BROWSER UPLINK", "BROWSER-MODUS"), GREEN, "running", False),
            "browser_login": (L("SIGN IN // EDGE", "ANMELDEN // EDGE"), YELLOW, "active", True),
            "checking": (L("PLAYLIST CHECK...", "PRÜFE PLAYLIST..."), CYAN, "active", True),
            "stopping": (L("STOPPING...", "WIRD GESTOPPT..."), CYAN, "inactive", True),
            "fertig": (L("COMPLETE", "FERTIG"), CYAN, "active", False),
            "gestoppt": (L("HALTED", "ANGEHALTEN"), RED, "frame", False),
            "quota": (L("DAILY QUOTA REACHED", "TAGESLIMIT ERREICHT"), YELLOW, "active", False),
            "fehler": (L("ERROR // SEE LOG", "FEHLER // SIEHE PROTOKOLL"), RED, "frame", False),
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
        allowed = list(YEARS) + [list_key(lst) for lst in load_lists()]
        saved = [y for y in self.settings.get("year_order", []) if y in allowed]
        self.order = saved + [y for y in allowed if y not in saved]
        for y in self.order:
            row = YearRow(self.scroll.inner, y, command=lambda y=y: self.toggle_year(y), pause_command=self.toggle_pause)
            Tooltip(row, lambda r=row: self.year_tip(r))
            row.pack(fill="x", pady=(0, px(4)))
            row.refresh(selected=y in selected)
            self.bind_drag(row)
            if is_list_key(y):
                row.bind("<Button-3>", lambda e, k=y: self.edit_list(k), add="+")
            self.rows[y] = row
        self.scroll.canvas.yview_moveto(0)
        self.update_total()

    # --- Reihenfolge per Drag & Drop
    def bind_drag(self, row):
        row.bind("<ButtonPress-1>", lambda e, r=row: self.drag_press(r, e), add="+")
        row.bind("<B1-Motion>", lambda e, r=row: self.drag_motion(r, e), add="+")
        row.bind("<ButtonRelease-1>", lambda e, r=row: self.drag_release(r, e), add="+")

    def drag_press(self, row, e):
        self.drag = {"row": row, "y0": e.y_root, "on": False, "to": None}

    def drag_target(self, y_root):
        """Index, vor dem eingefügt wird, und die y-Position der Markierung (im inneren Frame)."""
        inner = self.scroll.inner
        rel = y_root - inner.winfo_rooty()
        rows = [self.rows[y] for y in self.order]
        for i, r in enumerate(rows):
            if rel < r.winfo_y() + r.winfo_height() / 2:
                return i, max(0, r.winfo_y() - px(3))
        last = rows[-1]
        return len(rows), last.winfo_y() + last.winfo_height() + px(1)

    def drag_motion(self, row, e):
        d = getattr(self, "drag", None)
        if not d or d["row"] is not row:
            return
        if not d["on"]:
            if abs(e.y_root - d["y0"]) < px(8):
                return
            d["on"] = True
            row.dragging = True
            self.drag_line = tk.Frame(self.scroll.inner, bg=YELLOW, height=max(2, px(3)))
            row.set(role=YELLOW, state="active")
            self.configure(cursor="sb_v_double_arrow")
        # am Rand automatisch scrollen
        c = self.scroll.canvas
        cy = e.y_root - c.winfo_rooty()
        if cy < px(30):
            c.yview_scroll(-1, "units")
        elif cy > c.winfo_height() - px(30):
            c.yview_scroll(1, "units")
        idx, ly = self.drag_target(e.y_root)
        d["to"] = idx
        self.drag_line.place(x=0, y=ly, relwidth=1)
        self.drag_line.lift()

    def drag_release(self, row, e):
        d = getattr(self, "drag", None)
        self.drag = None
        if not d or not d["on"]:
            return
        row.dragging = False
        self.configure(cursor="")
        if getattr(self, "drag_line", None):
            self.drag_line.destroy()
            self.drag_line = None
        idx = d["to"]
        y = row.year
        old = self.order.index(y)
        if idx is not None and idx not in (old, old + 1):
            self.order.remove(y)
            self.order.insert(idx - 1 if idx > old else idx, y)
            for yy in self.order:
                self.rows[yy].pack_forget()
            for yy in self.order:
                self.rows[yy].pack(fill="x", pady=(0, px(4)))
            self.save_settings()
            if self.worker and self.worker.is_alive():
                self.write_log(L("NEW ORDER APPLIES FROM THE NEXT START", "NEUE REIHENFOLGE GILT AB DEM NÄCHSTEN START"),
                               "info")
        row.refresh()

    def busy(self):
        if self.worker and self.worker.is_alive():
            self.write_log(L("STOP THE CURRENT RUN FIRST", "ERST DEN LAUFENDEN DURCHLAUF STOPPEN"), "err")
            return True
        return False

    def edit_credentials(self):
        if self.busy():
            return
        cid, sec = get_creds()
        NCDialog(self, L("API Setup", "API-Zugang"), [
            ("client_id", "CLIENT ID", cid, False),
            ("client_secret", "CLIENT SECRET", sec, True),
        ], L("GOOGLE CLOUD CONSOLE // YOUTUBE DATA API V3 // OAUTH CLIENT TYPE: TVS AND LIMITED INPUT DEVICES. "
             "STORED LOCALLY IN CREDENTIALS.JSON NEXT TO THE PROGRAM.",
             "GOOGLE CLOUD CONSOLE // YOUTUBE DATA API V3 // OAUTH-CLIENT-TYP: TVS AND LIMITED INPUT DEVICES. "
             "WIRD NUR LOKAL IN CREDENTIALS.JSON NEBEN DEM PROGRAMM GESPEICHERT."), self.save_credentials)

    def year_tip(self, row):
        if is_list_key(row.year):
            return L("Keyword list. Click to select it, right-click to edit it. ",
                     "Stichwortliste. Klick wählt sie aus, Rechtsklick bearbeitet sie. ") + \
                f"{row.info['done']}/{row.info['total']}"
        if row.selected:
            sel = L("Selected, will be processed on START. Click again to deselect. Drag to change the order.",
                    "Markiert, wird beim START bearbeitet. Erneut klicken zum Abwählen. Ziehen ändert die "
                    "Reihenfolge.")
        else:
            sel = L("Click to select this year.", "Klicken, um dieses Jahr auszuwählen.")
        info = row.info
        state = {"none": L("Videos not searched yet.", "Videos noch nicht gesucht."),
                 "scanning": L("Search started but not finished.", "Suche angefangen, aber noch nicht fertig."),
                 "ready": L(f"{info['total']} videos found, playlist still empty.",
                            f"{info['total']} Videos gefunden, Playlist noch leer."),
                 "progress": L(f"{info['done']} of {info['total']} videos added.",
                               f"{info['done']} von {info['total']} Videos eingefügt."),
                 "done": L(f"Done, all {info['total']} videos are in the playlist.",
                           f"Fertig, alle {info['total']} Videos sind in der Playlist.")}.get(info["code"], "")
        return f"{row.year}: {state}\n{sel}"

    def add_tooltips(self):
        Tooltip(self.help_btn, L("Opens the detailed help for all features.",
                                 "Öffnet die ausführliche Hilfe zu allen Funktionen."))
        Tooltip(self.setup_btn, lambda: {
            YELLOW: L("The daily quota is used up. Until the reset, Chronolist won't ask again and goes straight to "
                      "the fallback if it is switched on.",
                      "Das Tageskontingent ist aufgebraucht. Bis zum Reset fragt Chronolist nicht mehr nach und nimmt "
                      "direkt den Fallback, wenn er eingeschaltet ist.")
                    + (f" Reset: {fmt_reset(quota_blocked('units'))}." if quota_blocked("units") else ""),
            RED: L("YouTube access is missing or was refused. Click to enter your Client ID and secret.",
                   "Zugang zu YouTube fehlt oder wurde abgelehnt. Klicken, um Client ID und Secret einzutragen."),
            GREEN: L("YouTube access works. Click to change your credentials.",
                     "Zugang zu YouTube funktioniert. Klicken, um die Zugangsdaten zu ändern."),
        }.get(self.setup_btn.color, L("Credentials are set. You'll sign in to Google on the next START.",
                                      "Zugangsdaten sind eingetragen, die Anmeldung bei Google folgt beim nächsten START.")))
        Tooltip(self.header.chip, L("Shows what Chronolist is doing right now. During pauses a countdown runs here.",
                                    "Zeigt, was Chronolist gerade macht. Bei Pausen läuft hier ein Countdown."))
        Tooltip(self.channel_card, L("The channel whose videos are collected. Use EDIT to enter another channel "
                                     "(@name, link or channel ID).",
                                     "Der Kanal, dessen Videos gesammelt werden. Über ÄNDERN einen anderen Kanal "
                                     "eingeben (@Name, Link oder Kanal-ID)."))
        Tooltip(self.start_btn, L("Works through the years selected on the left, one after another, in the order "
                                  "of the list.",
                                  "Arbeitet die links markierten Jahre nacheinander ab, in der Reihenfolge der "
                                  "Liste."))
        Tooltip(self.pause_btn, L("Pauses after the current step. RESUME continues right there.",
                                  "Hält nach dem aktuellen Schritt an. WEITER macht genau dort weiter."))
        Tooltip(self.stop_btn, L("Ends the run. Before that, the current playlist is checked for duplicates once more. "
                                 "Your progress is saved.",
                                 "Beendet den Lauf. Vorher wird die aktuelle Playlist noch einmal auf Doppelte "
                                 "geprüft. Der Fortschritt bleibt gespeichert."))
        Tooltip(self.check_btn, L("Checks the playlists of the selected years: removes duplicate videos and syncs "
                                  "the progress.",
                                  "Prüft die Playlists der ausgewählten Jahre: entfernt doppelte Videos und gleicht "
                                  "den Fortschritt ab."))
        Tooltip(self.fallback_card, L("When the daily YouTube quota is used up, continue in a separate Edge window. "
                                      "Warning: this breaks YouTube's rules and puts your account at risk.",
                                      "Wenn das tägliche YouTube-Kontingent leer ist, über ein eigenes Edge-Fenster "
                                      "weitermachen. Achtung: verstößt gegen die YouTube-Regeln, Risiko für dein "
                                      "Konto."))
        Tooltip(self.app_card, L("Program settings: language, auto-run, start with Windows and update check.",
                                 "Programm-Einstellungen: Sprache, Autostart, Start mit Windows und Update-Prüfung."))
        Tooltip(self.settings_card, L("Playlist title and description with placeholders, visibility and custom "
                                      "thumbnails per year.",
                                      "Playlist-Titel und Beschreibung mit Platzhaltern, Sichtbarkeit und eigene "
                                      "Thumbnails pro Jahr."))
        Tooltip(self.login_panel, L("Google sign-in: open the link, enter the code, confirm. Clicking the code copies it.",
                                    "Google-Anmeldung: Link öffnen, Code eingeben, bestätigen. Klick auf den Code "
                                    "kopiert ihn."))

    def api_tick(self):
        self.update_api_button()
        for r in self.rows.values():
            r.refresh_blocked()
        self.after(60000, self.api_tick)

    def show_removed(self, year=None, owner=None):
        w = getattr(self, "removed_win", None)
        if w is not None and w.winfo_exists():
            w.destroy()
        if owner is not None:
            owner.grab_release()            # sonst blockiert der offene Einstellungsdialog dieses Fenster
        self.removed_win = RemovedWindow(self, year)
        if owner is not None:
            def back(e, o=owner):
                if e.widget is self.removed_win:
                    try:
                        if o.winfo_exists():
                            o.grab_set()
                    except tk.TclError:
                        pass
            self.removed_win.bind("<Destroy>", back, add="+")

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
            self.setup_btn.set_text(L("API // MISSING", "API // FEHLT") if not all(get_creds())
                                    else L("API // ERROR", "API // FEHLER"))
        elif quota_blocked("units"):
            self.setup_btn.set_color(YELLOW)
            self.setup_btn.set_text("API // LIMIT")
        elif self.api_ok or TOKEN_FILE.exists():
            self.setup_btn.set_color(GREEN)
            self.setup_btn.set_text("API // ONLINE")
        else:
            self.setup_btn.set_color(CYAN)
            self.setup_btn.set_text(L("API // SIGN IN", "API // ANMELDEN"))

    def save_credentials(self, v):
        if not v["client_id"] or not v["client_secret"]:
            self.write_log(L("CLIENT ID AND SECRET REQUIRED", "CLIENT ID UND SECRET NÖTIG"), "err")
            return False
        if (v["client_id"], v["client_secret"]) != get_creds():
            save(CREDENTIALS_FILE, v)
            if TOKEN_FILE.exists():
                TOKEN_FILE.unlink()   # alter Login gehört zu den alten Zugangsdaten
            if QUOTA_FILE.exists():
                QUOTA_FILE.unlink()   # Limit gilt pro Google-Projekt
            self.write_log(L("API CREDENTIALS UPDATED // NEW GOOGLE SIGN-IN ON NEXT START", "ZUGANGSDATEN GESPEICHERT // NEUE GOOGLE-ANMELDUNG BEIM NÄCHSTEN START"), "ok")
            self.api_ok = None
            self.update_api_button()

    def edit_channel(self):
        if self.busy():
            return
        if not all(get_creds()):
            self.write_log(L("SET API CREDENTIALS FIRST", "ZUERST DIE API-ZUGANGSDATEN EINTRAGEN"), "warn")
            self.edit_credentials()
            return
        NCDialog(self, L("Target Channel", "Zielkanal"), [
            ("channel", L("@HANDLE, CHANNEL URL OR CHANNEL ID", "@HANDLE, KANAL-LINK ODER KANAL-ID"),
             CHANNEL.get("handle") or CHANNEL.get("id", ""), False),
        ], L("EXAMPLES: @CHANNELNAME // YOUTUBE.COM/@CHANNELNAME // UC...",
             "BEISPIELE: @KANALNAME // YOUTUBE.COM/@KANALNAME // UC..."), self.save_channel)

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
        # neuer Kanal: alle abgeschlossenen Jahre markiert, chronologisch
        self.settings["years"] = list(YEARS)
        self.settings["year_order"] = list(YEARS)
        save(SETTINGS_FILE, self.settings)
        self.build_rows()
        self.channel_card.update_state(resolving=False)
        self.header.redraw()
        self.footer.redraw()
        if hasattr(self, "settings_card"):
            self.settings_card.redraw()

    def start(self):
        if self.worker and self.worker.is_alive():
            return
        if not all(get_creds()):
            self.write_log(L("NO API CREDENTIALS // OPEN API SETUP", "KEINE API-ZUGANGSDATEN // API-KNOPF OBEN ÖFFNEN"), "err")
            self.edit_credentials()
            return
        if not CHANNEL:
            self.write_log(L("NO TARGET CHANNEL // CLICK THE CHANNEL CARD", "KEIN ZIELKANAL // OBEN LINKS AUF ÄNDERN KLICKEN"), "err")
            self.edit_channel()
            return
        years = self.selected_years()
        if not years:
            self.write_log(L("NO TARGET SELECTED // CLICK A CARD ON THE LEFT", "KEIN ZIEL AUSGEWÄHLT // LINKS EINE KARTE ANKLICKEN"), "err")
            return
        self.save_settings()
        self.write_log(f"START // {', '.join(map(year_label, years))}", "sep")
        self.worker = Worker(years, 0, self.out, fallback=self.fallback)
        self.worker.skip_ahead = self.skip_ahead
        self.run_mode = "running"
        self.pause_info = None
        self.worker.start()
        self.start_btn.set_enabled(False)
        self.check_btn.set_enabled(False)
        self.stop_btn.set_enabled(True)
        self.pause_btn.set_enabled(True)
        self.pause_btn.set_text(L("❚❚  PAUSE", "❚❚  PAUSE"))
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
            self.write_log(L("NO TARGET SELECTED // CLICK A CARD ON THE LEFT", "KEIN ZIEL AUSGEWÄHLT // LINKS EINE KARTE ANKLICKEN"), "err")
            return
        names = ", ".join(map(year_label, years))
        self.write_log(L(f"PLAYLIST CHECK // {names}", f"PLAYLIST-PRÜFUNG // {names}"), "sep")
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
            self.pause_btn.set_text(L("❚❚  PAUSE", "❚❚  PAUSE"))
        else:
            self.worker.pause_event.set()
            self.pause_btn.set_text(L("▶  RESUME", "▶  WEITER"))

    def stop(self):
        if self.worker and self.worker.is_alive():
            self.pause_btn.set_enabled(False)
            self.worker.stop_event.set()
            self.stop_btn.set_enabled(False)
            self.set_status("stopping")

    def show_post_update(self):
        """Nach einem Update einmalig die Neuerungen ins Protokoll schreiben."""
        pu = self.settings.pop("post_update", None)
        if not pu:
            return
        save(SETTINGS_FILE, self.settings)
        self.write_log(L(f"UPDATED TO v{pu.get('version', VERSION)} // WHAT'S NEW",
                         f"AKTUALISIERT AUF v{pu.get('version', VERSION)} // DAS IST NEU"), "ok")
        for line in (pu.get("notes") or "").splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("#"):
                self.write_log("// " + line.lstrip("#").strip().upper(), "sep")
            else:
                self.write_log("• " + line.lstrip("-* ").strip(), "info")

    def cleanup_tick(self):
        # alte exe nach einem Update löschen; sie ist ggf. noch ein paar Sekunden gesperrt
        self.cleanup_tries += 1
        if not cleanup_update() and self.cleanup_tries < 10:
            self.after(3000, self.cleanup_tick)

    def update_click(self):
        if not self.header.update:
            return
        if not (can_self_update() and getattr(self, "update_zip", None)):
            webbrowser.open(self.header.update[1])
            return
        if getattr(self, "updating", False) or self.busy():
            return
        UpdateDialog(self, self.header.update[0], self.header.update[1])

    def run_update(self):
        self.updating = True
        self.write_log(L(f"DOWNLOADING UPDATE v{self.header.update[0]}", f"LADE UPDATE v{self.header.update[0]}"),
                       "info")
        threading.Thread(target=download_update, args=(self.update_zip, self.out), daemon=True).start()

    def finish_update(self):
        if self.worker and self.worker.is_alive():
            self.updating = False
            self.write_log(L("UPDATE PAUSED // STOP THE RUN AND CLICK UPDATE AGAIN",
                             "UPDATE ANGEHALTEN // DURCHLAUF STOPPEN UND NOCHMAL AUF UPDATE KLICKEN"), "err")
            return
        err = install_update()
        if err:
            self.updating = False
            self.write_log(L(f"UPDATE FAILED // {err} // THE CURRENT VERSION STAYS",
                             f"UPDATE FEHLGESCHLAGEN // {err} // DIE AKTUELLE VERSION BLEIBT"), "err")
            return
        # Changelog einmalig nach dem Neustart im Protokoll zeigen
        self.settings["post_update"] = {"version": self.header.update[0],
                                        "notes": getattr(self, "update_notes", "")}
        save(SETTINGS_FILE, self.settings)
        self.write_log(L("UPDATE INSTALLED // RESTARTING", "UPDATE INSTALLIERT // NEUSTART"), "ok")
        self.after(600, self.restart)

    def restart(self):
        """Programm neu starten (z. B. nach Sprachwechsel oder Update)."""
        import subprocess
        if getattr(sys, "frozen", False):
            args = [sys.executable]
        else:
            exe = Path(sys.executable)
            pyw = exe.with_name("pythonw.exe")
            args = [str(pyw if pyw.exists() else exe), str(Path(__file__).resolve())]
        env = dict(os.environ)
        # PyInstaller: neue, eigenständige Instanz statt Kindprozess der alten (entpackt sich neu)
        env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        try:
            subprocess.Popen(args, cwd=str(HERE), env=env)
        except OSError as e:
            self.write_log(L(f"RESTART FAILED // {e} // PLEASE RESTART MANUALLY",
                             f"NEUSTART FEHLGESCHLAGEN // {e} // BITTE VON HAND NEU STARTEN"), "err")
            return
        self.on_close()

    # --- Fensterposition merken
    def restore_window(self):
        """Letzte Größe/Position (und maximiert) wiederherstellen, wenn der Monitor noch da ist."""
        self._normal_geom = None
        win = self.settings.get("window") or {}
        rect = win.get("placement")
        if os.name == "nt" and rect and len(rect) == 4:
            l, t, r, b = rect
            if point_on_screen((l + r) // 2, t + 20):
                # erst nach dem ersten Anzeigen setzen, sonst platziert Windows das Fenster selbst
                self.after(30, lambda: set_placement(self, rect, bool(win.get("zoomed"))))
            self.bind("<Configure>", self._track_geometry, add="+")
            return
        g = win.get("geometry")
        m = re.match(r"^(\d+)x(\d+)([+-]-?\d+)([+-]-?\d+)$", g or "")
        if m:
            w, h = int(m.group(1)), int(m.group(2))
            x, y = int(m.group(3).replace("+-", "-")), int(m.group(4).replace("+-", "-"))
            if point_on_screen(x + w // 2, y + 20):
                self.geometry(g)
                self._normal_geom = g
        if win.get("zoomed") and (self._normal_geom or not m):
            def zoom():
                try:
                    self.state("zoomed")
                except tk.TclError:
                    try:
                        self.attributes("-zoomed", True)
                    except tk.TclError:
                        pass
            self.after(60, zoom)
        self.bind("<Configure>", self._track_geometry, add="+")

    def _track_geometry(self, e):
        if e.widget is self:
            try:
                if self.state() == "normal":
                    self._normal_geom = self.geometry()
            except tk.TclError:
                pass

    def save_window(self):
        pl = get_placement(self)
        if pl:
            rect, zoomed = pl
            self.settings["window"] = {"placement": rect, "zoomed": zoomed}
            save(SETTINGS_FILE, self.settings)
            return
        try:
            zoomed = self.state() == "zoomed" or bool(self.attributes("-zoomed") if os.name != "nt" else False)
        except tk.TclError:
            zoomed = False
        self.settings["window"] = {"geometry": self._normal_geom or self.geometry(), "zoomed": zoomed}
        save(SETTINGS_FILE, self.settings)

    def on_close(self):
        try:
            self.save_window()
        except Exception:  # noqa: BLE001
            pass
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
        self.write_log(L("CODE COPIED TO CLIPBOARD", "CODE IN DIE ZWISCHENABLAGE KOPIERT"), "info")

    # --- Nachrichten vom Worker
    def write_log(self, text, level="info"):
        stamp = datetime.datetime.now().strftime("%H:%M:%S")
        self.logbox.config(state="normal")
        self.logbox.insert("end", f"[{stamp}] ", "ts")
        m = re.match(r"^((?:REPAIR|NACHTRAG) \d+ // )(420)(/\d+ (?:MOVED|VERSCHOBEN))$", text)
        r = re.match(r"^(\d{4}) // .*SHORTS.*(?:REMOVED|ENTFERNT)", text)
        if r:
            y = int(r.group(1))
            tag = f"rm{y}_{self.logbox.index('end')}"
            self.logbox.insert("end", text, level)
            self.logbox.insert("end", L("  // SHOW ›", "  // ANZEIGEN ›") + "\n", ("loglink", tag))
            self.logbox.tag_bind(tag, "<Button-1>", lambda e, y=y: self.show_removed(y))
            self.logbox.tag_bind(tag, "<Enter>", lambda e: self.logbox.configure(cursor="hand2"))
            self.logbox.tag_bind(tag, "<Leave>", lambda e: self.logbox.configure(cursor=""))
        elif m:
            # kleines Easter Egg
            self.logbox.insert("end", m.group(1), level)
            self.logbox.insert("end", m.group(2), "leaf")
            self.logbox.insert("end", f"{m.group(3)}\n", level)
        else:
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
                    y = msg[1]
                    if y in self.rows:
                        self.rows[y].refresh(info=year_info(y))
                    self.update_total()
                elif kind == "checking":
                    if msg[1] in self.rows:
                        self.rows[msg[1]].set_checking(msg[2])
                    self.set_status("checking" if msg[2] else getattr(self, "run_mode", "running"))
                elif kind == "api":
                    self.api_ok = msg[1]
                    self.update_api_button()
                    for r in self.rows.values():
                        r.refresh_blocked()
                elif kind == "channel":
                    self.apply_channel(msg[1])
                elif kind == "channel_id":
                    if self.settings.get("channel"):
                        self.settings["channel"]["id"] = msg[1]
                        save(SETTINGS_FILE, self.settings)
                    self.channel_card.update_state()
                elif kind == "update":
                    self.header.update = (msg[1], msg[2])
                    self.update_zip = msg[3] if len(msg) > 3 else None
                    self.update_notes = msg[4] if len(msg) > 4 else ""
                    if self.header.upd_btn is None:
                        b = CutButton(self.header, f"▲  UPDATE v{msg[1]}", self.update_click)
                        b.full_text, b.full_w = f"▲  UPDATE v{msg[1]}", b.winfo_reqwidth()
                        b.short_text, b.short_w = "▲  UPDATE", px(104)
                        b.set_color(GREEN)
                        Tooltip(b, L(f"Version {msg[1]} is available. Click to update.",
                                     f"Version {msg[1]} ist verfügbar. Klicken zum Updaten."))
                        self.header.upd_btn = b
                    self.header.redraw()
                    self.write_log(L(f"UPDATE AVAILABLE // v{msg[1]} // CLICK THE GREEN UPDATE BUTTON AT THE TOP", f"UPDATE VERFÜGBAR // v{msg[1]} // OBEN AUF DEN GRÜNEN UPDATE-KNOPF KLICKEN"), "ok")
                elif kind == "upd_progress":
                    self.write_log(L(f"DOWNLOADING UPDATE // {msg[1]} %", f"UPDATE WIRD GELADEN // {msg[1]} %"), "info")
                elif kind == "upd_failed":
                    self.updating = False
                    self.write_log(L(f"UPDATE FAILED // {msg[1]} // THE CURRENT VERSION STAYS",
                                     f"UPDATE FEHLGESCHLAGEN // {msg[1]} // DIE AKTUELLE VERSION BLEIBT"), "err")
                elif kind == "upd_ready":
                    self.finish_update()
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
                elif kind == "activity":
                    if msg[1] in self.rows:
                        self.rows[msg[1]].set_activity(msg[2])
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
                    self.pause_btn.set_text(L("❚❚  PAUSE", "❚❚  PAUSE"))
                    self.pause_info = None
                    self.set_status("idle" if msg[1] == "resolved" else msg[1])
                    self.update_api_button()
                    if msg[1] != "resolved":
                        self.channel_card.update_state(resolving=False)
                    for y, r in self.rows.items():
                        r.refresh(info=year_info(y))
        except queue.Empty:
            pass
        except Exception as e:  # noqa: BLE001
            # ein fehlerhafte Meldung darf die Anzeige nicht stilllegen (sonst bleiben Log und Knöpfe stehen)
            try:
                self.write_log(f"UI ERROR // {type(e).__name__}: {e}", "err")
            except Exception:  # noqa: BLE001
                pass
        self.after(200, self.poll)


if __name__ == "__main__":
    App().mainloop()
