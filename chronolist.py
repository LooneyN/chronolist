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
import subprocess
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
# Platzhalter: {channel} {handle} {year} {count}, für eigene Listen zusätzlich {name}
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


def default_list_title_template():
    return "{name}"


def default_list_description_template():
    return L("{name}: {count} videos by {channel} in upload order.",
             "{name}: {count} Videos von {channel} in Upload-Reihenfolge.")


PLAYLIST_TEMPLATE = "{channel} {year} in upload order"
DESCRIPTION_TEMPLATE = "All videos by {channel} from {year} in upload order."
LIST_TITLE_TEMPLATE = "{name}"   # eigene Listen: Titel- und Beschreibungs-Vorlage (eigener Reiter LISTS)
LIST_DESCRIPTION_TEMPLATE = "{name}: {count} videos by {channel} in upload order."
PRIVACY = "public"              # public / unlisted / private
PRIVACY_OPTIONS = ("public", "unlisted", "private")
TEMPLATE_FIELDS = ("channel", "handle", "year", "count")
APP_NAME = "Chronolist"
VERSION = "3.3.0"
BUILD = 144
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


def mark_quota(kind, by="api"):
    """Limit für heute merken. by = 'api' (YouTube hat abgelehnt) oder 'reserve' (Chronolist hält die Reserve frei)."""
    with _quota_lock:
        q = load(QUOTA_FILE, {})
        q[kind] = next_quota_reset().timestamp()
        if kind == "units":
            q["units_by"] = by
        save(QUOTA_FILE, q)


AUTO_INTERVALS = (30, 60, 120, 360)     # Voreinstellungen (Minuten zwischen zwei automatischen Prüfungen)
AUTO_INTERVAL_DEFAULT = 60
AUTO_MIN, AUTO_MAX, AUTO_STEP = 15, 1440, 5      # eigenes Intervall: mindestens 15 Min, nur in 5er-Schritten


def clean_interval(v):
    """Eingabe auf ein erlaubtes Intervall bringen (Vielfaches von 5, 15 Minuten bis 24 Stunden)."""
    try:
        v = int(round(float(str(v).strip().replace(",", ".")) / AUTO_STEP)) * AUTO_STEP
    except ValueError:
        return None
    return max(AUTO_MIN, min(AUTO_MAX, v))


def interval_label(m):
    if m < 60:
        return f"{m} MIN"
    return f"{m // 60} H" if m % 60 == 0 else f"{m // 60} H {m % 60} MIN"


AUTO_TIME_SLOTS = 4      # so viele feste Uhrzeiten lassen sich eintragen


def parse_time(v):
    """'8', '08', '8:30', '08.30', '0830' -> '08:30'. Ungültig -> None, leer -> ''."""
    v = str(v).strip().replace(".", ":")
    if not v:
        return ""
    if ":" in v:
        h, _, m = v.partition(":")
    elif len(v) in (3, 4):
        h, m = v[:-2], v[-2:]
    else:
        h, m = v, "0"
    if not (h.isdigit() and m.isdigit()):
        return None
    h, m = int(h), int(m)
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return f"{h:02d}:{m:02d}"


def next_time_at(times, now=None):
    """Zeitpunkt (Unix-Zeit) der nächsten der festen Uhrzeiten nach `now` (Ortszeit)."""
    now = now or time.time()
    base = datetime.datetime.fromtimestamp(now)
    best = None
    for t in times:
        h, m = map(int, t.split(":"))
        cand = base.replace(hour=h, minute=m, second=0, microsecond=0)
        if cand.timestamp() <= now + 1:
            cand += datetime.timedelta(days=1)
        if best is None or cand < best:
            best = cand
    return best.timestamp() if best else now + 3600


AUTO_LAST = {}           # letzte Auto-Prüfung pro Karte (Schlüssel als Text): {"t": Unix-Zeit, "new": neue Videos}


def load_auto_last():
    AUTO_LAST.clear()
    d = load(autocheck_file(), {})
    if isinstance(d, dict):
        AUTO_LAST.update(d)


def flash_taskbar(win):
    """Taskleisten-Symbol blinken lassen, bis das Fenster wieder vorne ist (nur Windows)."""
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes

        class FLASHWINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("hwnd", wintypes.HWND), ("dwFlags", wintypes.DWORD),
                        ("uCount", wintypes.UINT), ("dwTimeout", wintypes.DWORD)]
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        fi = FLASHWINFO(ctypes.sizeof(FLASHWINFO), hwnd, 0x2 | 0xC, 0, 0)    # FLASHW_TRAY | FLASHW_TIMERNOFG
        ctypes.windll.user32.FlashWindowEx(ctypes.byref(fi))
    except Exception:  # noqa: BLE001
        pass


def windows_toast(title, text):
    """Windows-Benachrichtigung ohne Zusatzbibliothek (über die Windows-PowerShell). Fehler werden ignoriert."""
    if os.name != "nt":
        return

    def q(v):
        return str(v).replace("'", "''")
    ps = ("$null=[Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,ContentType=WindowsRuntime];"
          "$x=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
          "[Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
          "$t=$x.GetElementsByTagName('text');"
          f"$null=$t.Item(0).AppendChild($x.CreateTextNode('{q(title)}'));"
          f"$null=$t.Item(1).AppendChild($x.CreateTextNode('{q(text)}'));"
          "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
          "'{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe')"
          ".Show([Windows.UI.Notifications.ToastNotification]::new($x))")

    def run():
        try:
            subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                            "-Command", ps], creationflags=0x08000000, timeout=30,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:  # noqa: BLE001
            pass
    threading.Thread(target=run, daemon=True).start()


AUTO_SHOW = False        # AUTO-CHECK eingeschaltet: Karten zeigen den AUTO-Schalter
AUTO_YEARS = set()       # Jahre, die der AUTO-CHECK mit aktuell hält (Listen: Feld "auto" in der Liste)


def auto_on(key):
    if is_list_key(key):
        return bool((find_list(key) or {}).get("auto"))
    return key in AUTO_YEARS


def set_auto(key, on):
    if is_list_key(key):
        lists = load_lists()
        for x in lists:
            if list_key(x) == key:
                x["auto"] = bool(on)
        save_lists(lists)
    elif on:
        AUTO_YEARS.add(key)
    else:
        AUTO_YEARS.discard(key)


QUOTA_DAILY = 10000      # Standard-Tageskontingent eines Google-Projekts
QUOTA_RESERVE = 20       # bleibt immer übrig, damit ein Kanal hinzugefügt oder getauscht werden kann
_quota_lock = threading.Lock()


def api_cost(method, path):
    """Geschätzte Kosten eines API-Aufrufs in Einheiten (Lesen 1, Schreiben 50, Suche 100)."""
    if path == "search":
        return 100
    return 1 if method == "GET" else 50


def quota_used():
    """Heute (seit dem letzten Reset) von Chronolist verbrauchte Einheiten. Nur eine Schätzung:
    andere Programme im selben Google-Projekt zählen nicht mit."""
    q = load(QUOTA_FILE, {})
    return q.get("used", 0) if q.get("day") == next_quota_reset().timestamp() else 0


def add_quota_used(units):
    with _quota_lock:
        q = load(QUOTA_FILE, {})
        day = next_quota_reset().timestamp()
        if q.get("day") != day:
            q["day"], q["used"] = day, 0
        q["used"] = q.get("used", 0) + units
        save(QUOTA_FILE, q)


def reserve_only():
    """Nur die Reserve ist gesperrt (YouTube selbst hat noch nicht abgelehnt)?"""
    return bool(quota_blocked("units")) and load(QUOTA_FILE, {}).get("units_by") == "reserve"


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
def autocheck_file(): return CHAN_DIR / "autocheck.json"   # letzte Auto-Prüfung pro Karte: {key: {t, new}}
def view_file(): return CHAN_DIR / "view.json"     # Kanal-Info und Ansicht (Auswahl, Reihenfolge, HIDE DONE)


def known_channel(target):
    """Schon einmal benutzter Kanal (gleicher @handle oder gleiche ID)? Dann seine gespeicherte Info, sonst None.
    So lässt sich zu einem bekannten Kanal zurückwechseln, ohne die API zu fragen."""
    h = (target.get("handle") or "").lower()
    cid = target.get("id")
    for f in (DATA_DIR.glob("*/view.json") if DATA_DIR.exists() else []):
        info = (load(f, {}) or {}).get("channel") or {}
        if not info.get("first_year"):
            continue
        if (h and (info.get("handle") or "").lower() == h) or (cid and info.get("id") == cid):
            return info
    return None

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


SERIES_MIN = 5          # ab so vielen Videos mit gleichem Titelanfang gilt etwas als Serie
_EP_MARK = re.compile(r"(?:#|\b(?:folge|teil|episode|ep|part|e|nr)\.?\s*#?)\s*\d{1,4}\b", re.I)
_EP_TAIL = re.compile(r"^(.*?[^\W\d_].*?)[\s\-\u2013|:]+\d{1,3}\s*$")


def series_stem(title):
    """Titelanfang vor der Folgennummer ("Minecraft Season 04 #12 - Haus" -> "Minecraft Season 04"), sonst None."""
    t = (title or "").strip()
    m = _EP_MARK.search(t)
    stem = t[:m.start()] if m else (_EP_TAIL.match(t).group(1) if _EP_TAIL.match(t) else "")
    stem = stem.strip(" \t-\u2013|:·•/[](){}")
    return stem if len(stem) >= 3 and re.search(r"[^\W\d_]", stem) else None


def suggest_series(pool=None, titles=None, lists=None, minimum=SERIES_MIN):
    """Serien aus wiederkehrenden Titelanfängen: [{'name','count','from','to'}], größte zuerst.
    Serien, die eine vorhandene Liste schon zu mindestens 80 % abdeckt, werden weggelassen."""
    pool = pool if pool is not None else all_year_videos()
    titles = titles if titles is not None else load(titles_file(), {})
    lists = lists if lists is not None else load_lists()
    groups = {}
    for v in pool:
        stem = series_stem(titles.get(v["id"], ""))
        if stem:
            g = groups.setdefault(norm_text(stem), {"name": stem, "ids": set(), "years": []})
            g["ids"].add(v["id"])
            g["years"].append(v["year"])
    covered = [{x["id"] for x in list_videos(lst, titles, pool)} for lst in lists]
    out = []
    for g in groups.values():
        if len(g["ids"]) < minimum:
            continue
        if any(len(g["ids"] & c) >= 0.8 * len(g["ids"]) for c in covered):
            continue
        out.append({"name": g["name"], "count": len(g["ids"]), "from": min(g["years"]), "to": max(g["years"])})
    return sorted(out, key=lambda x: (-x["count"], x["name"].lower()))


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
            if vids is None and find_list(y):
                vids = list_videos(find_list(y))       # noch nicht abgeglichen: Treffer jetzt zählen
            count = len(vids) if vids is not None else "?"
        name = list_name(y)
        return {"channel": channel_name(), "handle": CHANNEL.get("handle", ""), "name": name, "year": name,
                "count": count}
    if count is None:
        vids = load(videos_file(y), None)
        count = len(vids) if vids is not None else "?"
    return {"channel": channel_name(), "handle": CHANNEL.get("handle", ""), "year": y, "name": y, "count": count}


def fill_template(t, y, count=None):
    try:
        return t.format(**template_values(y, count))
    except (KeyError, IndexError, ValueError, AttributeError):
        return t


def playlist_title(y):
    """Jahre: Jahres-Vorlage. Eigene Listen: Listen-Vorlage (Standard: nur der Listenname)."""
    if is_list_key(y):
        return fill_template(LIST_TITLE_TEMPLATE, y).strip() or list_name(y)
    return fill_template(PLAYLIST_TEMPLATE, y)


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
def playlist_description(y):
    """Jahre: Jahres-Vorlage. Eigene Listen: eigene Beschreibung (Platzhalter erlaubt), sonst die Listen-Vorlage."""
    if is_list_key(y):
        own = ((find_list(y) or {}).get("desc") or "").strip()
        return fill_template(own or LIST_DESCRIPTION_TEMPLATE, y)
    return fill_template(DESCRIPTION_TEMPLATE, y)


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
def autostart_command(minimized=False):
    extra = " --minimized" if minimized else ""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"{extra}'
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    return f'"{pyw if pyw.exists() else exe}" "{Path(__file__).resolve()}"{extra}'


def get_windows_autostart():
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP_NAME)
            return True
    except OSError:
        return False


def set_windows_autostart(on, minimized=False):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, autostart_command(minimized))
        else:
            try:
                winreg.DeleteValue(k, APP_NAME)
            except FileNotFoundError:
                pass


def _template_ok(t):
    try:
        t.format(channel="X", handle="@x", year=2000, name="X", count=1)
        return True
    except (KeyError, IndexError, ValueError, AttributeError):
        return False


def apply_templates(settings):
    """Playlist-Titel und -Beschreibung aus settings.json übernehmen (Platzhalter: {channel}, {year})."""
    global PLAYLIST_TEMPLATE, DESCRIPTION_TEMPLATE, LIST_TITLE_TEMPLATE, LIST_DESCRIPTION_TEMPLATE, PRIVACY
    pt = settings.get("playlist_template") or default_playlist_template()
    dt = settings.get("description_template", default_description_template())   # "" = keine Beschreibung
    PLAYLIST_TEMPLATE = pt if isinstance(pt, str) and _template_ok(pt) else default_playlist_template()
    DESCRIPTION_TEMPLATE = dt if isinstance(dt, str) and _template_ok(dt) else default_description_template()
    lt = settings.get("list_title_template") or default_list_title_template()
    ld = settings.get("list_description_template", default_list_description_template())
    LIST_TITLE_TEMPLATE = lt if isinstance(lt, str) and _template_ok(lt) else default_list_title_template()
    LIST_DESCRIPTION_TEMPLATE = ld if isinstance(ld, str) and _template_ok(ld) else default_list_description_template()
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


class NoVideosFound(BrowserFailed):
    """Die Kanalseite zeigt für dieses Jahr keine Videos (Jahr ist schlicht leer)."""


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
    def __init__(self, years, limit, out, fallback=False, resolve=None, check_only=False, scan_only=False, auto=False):
        super().__init__(daemon=True)
        self.auto = auto             # automatische Prüfung im Hintergrund
        self.resolve_target = resolve
        self.check_only = check_only
        self.scan_only = scan_only
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
            if self.auto and not load(TOKEN_FILE, None):
                self.log(L("AUTO-CHECK SKIPPED // NOT SIGNED IN YET, START ONCE BY HAND", "AUTO-PRÜFUNG ÜBERSPRUNGEN // NOCH NICHT ANGEMELDET, EINMAL VON HAND STARTEN"), "warn")
                return
            self.tok = load(TOKEN_FILE, None) or self.device_login()
            if self.auto:
                self.auto_run()
                return
            if self.resolve_target is not None:
                reason = "resolved"
                self.emit("channel", self.resolve_channel(self.resolve_target))
                return
            if self.check_only:
                self.manual_check()
                return
            if self.scan_only:
                self.scan_archive()
                if self.fallback:
                    # Archiv-Scan braucht API; was fehlt, kommt (nur mit FALLBACK) über die Kanalseite, ohne Kontingent
                    for y in YEARS:
                        if load(videos_file(y), None) is None:
                            self.check()
                            self.emit("current", y)
                            try:
                                self.collect(y)
                            except NoVideosFound:
                                self.empty_year(y)
                self.fetch_missing_titles()
                this_year = datetime.date.today().year
                if any(load(videos_file(y), None) is None for y in YEARS if y < this_year):
                    self.log(L(f"SCAN INCOMPLETE // CLICK SCAN AGAIN AFTER THE RESET {quota_reset_local()}",
                               f"SCAN UNVOLLSTÄNDIG // NACH DEM RESET {quota_reset_local()} NOCHMAL SCAN KLICKEN"), "warn")
                else:
                    self.log(L("SCAN COMPLETE // VIDEOS AND TITLES STORED // NO PLAYLIST CHANGED",
                               "SCAN FERTIG // VIDEOS UND TITEL GESPEICHERT // KEINE PLAYLIST GEÄNDERT"), "ok")
                return
            for kind, label in (("units", L("API QUOTA", "API-KONTINGENT")), ("search", L("SEARCH LIMIT", "SUCHLIMIT"))):
                r = quota_blocked(kind)
                if r:
                    self.log(L(f"{label} ALREADY USED UP TODAY // SKIPPING IT UNTIL RESET {fmt_reset(r)}", f"{label} HEUTE SCHON AUFGEBRAUCHT // WIRD BIS ZUM RESET {fmt_reset(r)} ÜBERSPRUNGEN"), "warn")
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
                except NoVideosFound:
                    self.empty_year(y)
                    continue
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

    def auto_run(self):
        """Automatische Prüfung: laufendes Jahr nach neuen Videos absuchen (nur dort, wo schon ein Stand
        gespeichert ist), dann das Jahr (wenn gewählt) und die Listen mit Auto-Haken auffüllen."""
        if quota_blocked("units"):
            self.log(L(f"AUTO-CHECK SKIPPED // API QUOTA USED UP UNTIL {quota_reset_local()}",
                       f"AUTO-PRÜFUNG ÜBERSPRUNGEN // KONTINGENT BIS {quota_reset_local()} AUFGEBRAUCHT"), "warn")
            return
        keys = [y for y in self.years if is_list_key(y)]
        before = {}
        for k in self.years:
            v = load(videos_file(k), None)
            before[k] = None if v is None else len(v)      # nie abgeglichene Liste: nichts als "neu" zählen
        for y in YEARS:
            if not growing(y) or load(videos_file(y), None) is None:
                continue
            self.check()
            self.emit("current", y)
            try:
                videos = self.collect(y)
            except NoVideosFound:
                continue
            if y in self.years:
                self.fill(y, videos)
        self.run_lists(keys)
        result = {}
        for k in self.years:
            after = len(load(videos_file(k), None) or [])
            result[k] = 0 if before[k] is None else max(0, after - before[k])
        self.emit("auto_result", result)
        total = sum(result.values())
        self.log(L(f"AUTO-CHECK DONE // {total} NEW VIDEO{'S' if total != 1 else ''}",
                   f"AUTO-PRÜFUNG FERTIG // {total} NEUE{'S' if total == 1 else ''} VIDEO{'S' if total != 1 else ''}"),
                 "ok")

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
        cost = api_cost(method, path)
        exempt = method == "GET" and path == "channels"   # Kanalabfrage darf in die Reserve
        if quota_blocked("units") and not (exempt and reserve_only()):
            raise QuotaExceeded()
        if not exempt and quota_used() + cost > QUOTA_DAILY - QUOTA_RESERVE:
            mark_quota("units", "reserve")
            self.log(L(f"API QUOTA ALMOST USED UP // {QUOTA_RESERVE} UNITS KEPT FOR CHANNEL CHANGES",
                       f"API-KONTINGENT FAST AUFGEBRAUCHT // {QUOTA_RESERVE} EINHEITEN BLEIBEN FÜR KANALWECHSEL"), "warn")
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
            add_quota_used(cost)
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

    def empty_year(self, y):
        """Jahr ohne Videos: als leer merken, abwählen und weitermachen."""
        save(videos_file(y), [])
        self.scanning = None
        self.log(L(f"{year_label(y)} // NO VIDEOS // SKIPPED AND DESELECTED", f"{year_label(y)} // KEINE VIDEOS // ÜBERSPRUNGEN UND ABGEWÄHLT"), "warn")
        self.emit("select", None, y)
        self.emit("progress", y)

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
            raise NoVideosFound(L(f"{year_label(year)} // NO VIDEOS FOUND ON THE CHANNEL PAGE", f"{year_label(year)} // KEINE VIDEOS AUF DER KANALSEITE GEFUNDEN"))
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
        return parts[k]

    def ensure_playlist(self, year):
        sf = state_file(year)
        state = load(sf, {})
        if not state.get("playlist_id"):
            pid = self.create_playlist(playlist_title(year), playlist_description(year))
            state = {"playlist_id": pid, "parts": [pid], "next": 0, "skipped": []}
            save(sf, state)
            self.log(L(f"PLAYLIST CREATED // {playlist_title(year)}", f"PLAYLIST ANGELEGT // {playlist_title(year)}"), "ok")
        return state

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

S = 1.0                 # Skalierung = Windows-DPI * eingestellte Textgröße
UI_SCALES = (90, 95, 100, 105, 110)
UI_SCALE_DEFAULT = 100
UI_SCALE_BASE = 1.1  # 100 % in the settings = 110 % of the Windows scale
FONT_UI = "Segoe UI"
FONT_MONO = "Consolas"


def px(n):
    return max(1, int(round(n * S)))


def F(kind, size, bold=True):
    return (FONT_UI if kind == "ui" else FONT_MONO, -px(size), "bold" if bold else "normal")


def fit(w, h):
    """Geometrie-String, höchstens so groß wie der Bildschirm (die Textgröße macht Fenster größer)."""
    root = tk._default_root
    if root is not None:
        w, h = min(w, int(root.winfo_screenwidth() * 0.95)), min(h, root.winfo_screenheight() - px(110))
    return f"{w}x{h}"


def fit_min(w, h):
    root = tk._default_root
    if root is not None:
        w, h = min(w, int(root.winfo_screenwidth() * 0.9)), min(h, root.winfo_screenheight() - px(160))
    return w, h


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
# Das Logo steckt als kleine PNGs in verschiedenen Größen direkt im Programm (Base64), damit nichts nachgeladen werden muss.
LOGO_PNG = {
    16: (
        "iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAACs0lEQVR42p2Ty2tdZRTFf+s73znnvtI0pik2CKVgiw8UQehEqSAE"
        "lA6sg9RJhY6K/gNCoXoHDkV04KCCIg6K2FR8gFIhVJuJgo8KFUFKlUqgSbg1vbXNzT3nfHs7SOvIUTd7sAdrsWDttQDoQ+AuR3eO"
        "C8/OPTGqrD3ZkwOQABogbgEyuDxu2Kgb7SpkB75a+kaSy+fns9PLf736w8awP7BNhsOMEIUj3BL4lkoN7G8X9PKMYYzsbucfHp3b"
        "cyyym5lzPw5fOVcs+8MP2PizL4oAm44n0eoFHDAD4Oci+lQefNBYfOG+HYe52hyPG8vjMN3WrWt/lu0vryiW+Zg9x15mc2WVK2fO"
        "0Iqlsk5bnhJr5qyMk6eUQm3cJEYPa2VjU1mm7blQMFVVzb1PHdDBhVPhyQ/eV773fr91Y92sbsglWpYIQBABOlvuJ3eSg+3b65qc"
        "pBoOGQH7jh7RoaXF8MiJE7Lt28ymtrk/9KB7u4Wbi6JR6ACSwAxdH0JRELJAAG4O1rFuj6dff03PfP6JvGqcS5dFsi0OEDfu/DIE"
        "GAzEaOQOGNC5Z4rQNPx06mP/7Z2TrrqGbhv+vo7fDkCcocvI3R0R3GlSQ97u0AUufXvef3nzbV/9epE8JcWJnnBMEgJRJ8VuF1br"
        "RiaIAouRaxd/9cXvvuf3d9+zbHMcOr2ukkNTN4RqKANvzJ0mKQJEydyhTolQlLr4xltOtellqxPU7crMyCUmWzl5CLayMc5mWkVE"
        "TR0pCo3Ny8rNJmKWMkllkXuc6KgQnkuUEmUWKIK8ROHIrmkOzk6/pJOfrkWq6p+9vfYfh2Z3PNYKKgAkEMJxbi8AtTk7WznPzU6/"
        "+PjZ8x95vx8EcPX5uZ03kj26PqrMgv9vMz3hRR6CO4P9Z5cunJ6fzw4vLKS7qrD3+/+J/Au6gT4/pd1GxQAAAABJRU5ErkJggg=="
    ),
    32: (
        "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"
    ),
    48: (
        "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"
    ),
    64: (
        "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"
    ),
    80: (
        "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"
    ),
    96: (
        "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"
        "RU5ErkJggg=="
    ),
    128: (
        "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"
        "q60gcqxQVgAAAABJRU5ErkJggg=="
    ),
    160: (
        "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"
    ),
}


def logo_image(root, size):
    """Logo als PhotoImage in der Größe, die `size` (Pixel) am nächsten kommt."""
    key = min(LOGO_PNG, key=lambda k: abs(k - size))
    return tk.PhotoImage(master=root, data=LOGO_PNG[key])


class Panel(tk.Canvas):
    """Kantiges Panel mit einer abgeschnittenen Ecke unten rechts, Verlauf, Scanlines, Glow."""

    STATES = {  # Rahmen, Verlauf, Glow
        "active": (1.00, 0.42, True),
        "running": (1.00, 0.30, True),
        "inactive": (0.50, 0.12, False),
        "frame": (0.85, 0.10, False),
        "unavailable": (0.45, 0.04, False),
        "faded": (0.28, 0.05, False),     # fertige Jahre: in den Hintergrund
        "dim": (0.30, 0.03, False),       # gesperrt, weil eine andere Einstellung aus ist: ausgegraut, nicht rot
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
        role = (GREEN if self.st == "running" else RED if self.st in ("frame", "unavailable")
                else NEUTRAL if self.st == "dim" else self.role)
        edge, tint, glow = self.STATES[self.st]
        if self.hover and self.st not in ("unavailable", "dim"):
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
        self.auto_geo = None       # (x0, y0, x1, y1) des AUTO-Schalters
        self.auto_command = None

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
        name_w = tx - px(14) - nx
        self.auto_geo = None
        if AUTO_SHOW:
            name_w -= self.draw_auto(tx - px(14), y0 + h * 0.32, on, faded) + px(10)
            rec = AUTO_LAST.get(str(self.year)) if auto_on(self.year) else None
            if rec and not self.running:
                # neben dem AUTO-Schalter, nur wenn der Name daneben noch ganz Platz hat
                need = tkfont.Font(font=F("ui", 16)).measure(year_label(self.year)) + px(16)
                used = self.draw_last_check(rec, nx + name_w, y0 + h * 0.32, nx + need, faded)
                if used:
                    name_w -= used + px(12)
        self.create_text(nx, y0 + h * 0.32, text=fit_text(year_label(self.year), F("ui", 16), name_w), anchor="w",
                         font=F("ui", 16),
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

    def draw_last_check(self, rec, xr, cy, min_x, faded):
        """Rechts in der Statuszeile: wann die Auto-Prüfung zuletzt lief und wie viele Videos dazukamen."""
        t = datetime.datetime.fromtimestamp(rec.get("t", 0))
        when = t.strftime("%H:%M") if t.date() == datetime.date.today() else t.strftime("%d.%m. %H:%M")
        new = int(rec.get("new", 0))
        fnt = F("mono", 10)
        f = tkfont.Font(font=fnt)
        main = L("LAST CHECK ", "ZULETZT ") + when
        extra = f"  //  +{new} NEW" if new else ""
        if LANG == "de" and new:
            extra = f"  //  +{new} NEU"
        width = f.measure(main + extra)
        if xr - width < min_x:
            return 0                        # kein Platz neben dem Namen
        dim = TEXT_MUTED if not faded else mix(TEXT_MUTED, BG, 0.7)
        if extra:
            e = self.create_text(xr, cy, text=extra, anchor="e", font=fnt, fill=GREEN if not faded else mix(GREEN, BG, 0.6))
            xr = self.bbox(e)[0]
        self.create_text(xr, cy, text=main, anchor="e", font=fnt, fill=dim)
        return width

    def draw_auto(self, xr, cy, on, faded):
        """AUTO-Schalter: kleines Feld mit LED und Text, rechts in der Namenszeile. An = cyan wie die Karte
        AUTO-CHECK, aus = gedämpft. Gibt die Breite zurück."""
        flag = auto_on(self.year)
        col = CYAN if flag else mix(TEXT_MUTED, BG, 0.8 if not faded else 0.65)
        fnt = F("mono", 10)
        tw = tkfont.Font(font=fnt).measure("AUTO")
        w, hh, cut = tw + px(30), px(20), px(5)
        x0, y0, x1, y1 = xr - w, cy - hh / 2, xr, cy + hh / 2
        self.create_polygon(x0, y0, x1, y0, x1, y1 - cut, x1 - cut, y1, x0, y1,
                            fill=mix(CYAN, BG, 0.16) if flag else BG, outline=col, width=1)
        k = px(6)
        lx = x0 + px(9)
        self.create_rectangle(lx - k / 2, cy - k / 2, lx + k / 2, cy + k / 2,
                              fill=CYAN if flag else BG, outline=col)
        self.create_text(lx + k / 2 + px(6), cy, text="AUTO", anchor="w", font=fnt, fill=col)
        self.auto_geo = (x0 - px(3), y0 - px(3), x1 + px(3), y1 + px(3))
        return w

    # --- Animation der aktiven Karte: nur Ladebalken + ruhiger Glow
    def _click(self, e):
        if getattr(self, "dragging", False):
            return
        a = self.auto_geo
        if AUTO_SHOW and a and a[0] <= e.x <= a[2] and a[1] <= e.y <= a[3]:
            if self.auto_command:
                self.auto_command(self.year)
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
    def __init__(self, master, name, sub, value, command, available=True, height=CARD_H, compact=False,
                 locked_sub=None):
        super().__init__(master, height, command=command if available else None,
                         state="inactive" if available else "unavailable")
        self.name, self.sub, self.value, self.available = name, sub, value, available
        self.compact = compact
        self.locked = False
        self.locked_sub = locked_sub       # Text, solange die Karte von einer anderen abhängt und gesperrt ist
        self.update_value(value)

    def set_locked(self, on):
        """Ausgegraut, solange eine andere Einstellung aus ist (z. B. MINIMIERT STARTEN ohne MIT WINDOWS STARTEN)."""
        self.locked = bool(on) and self.available
        self.update_value(self.value)

    def update_value(self, value):
        self.value = value
        if self.available:
            self.st = "dim" if self.locked else "active" if value else "inactive"
        self.redraw()

    def draw_content(self, x0, y0, x1, y1, c, role):
        on = self.value and self.available and not self.locked
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
        sub = (self.locked_sub or self.sub) if self.locked else self.sub if self.available else L("UNAVAILABLE", "NICHT VERFÜGBAR")
        self.create_text(nx, y0 + h * 0.66, text=fit_text(sub, fs, maxw), anchor="w",
                         font=fs, fill=role if on else TEXT_MUTED)
        mid = (y0 + y1 - c / 2) / 2
        tag = (L("ON", "AN") if self.value else L("OFF", "AUS")) if self.available and not self.locked else "--"
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
            tk.Label(self, text=hint, bg=BG, fg=TEXT_MUTED, font=F("mono", 11, False), justify="left",
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


class ChannelDialog(tk.Toplevel):
    """Zielkanal eingeben. Die manuellen Felder (ohne API) sind zugeklappt und öffnen sich von selbst,
    wenn das API-Kontingent aufgebraucht ist."""

    def __init__(self, app, initial, on_save):
        super().__init__(app, bg=BG)
        self.title(L("Target Channel", "Zielkanal"))
        self.resizable(False, False)
        self.transient(app)
        self.on_save = on_save
        self.vars = {}
        pad = px(18)
        Section(self, L("TARGET CHANNEL", "ZIELKANAL"), "INPUT // SECURE").pack(fill="x", padx=pad, pady=(pad, px(10)))
        self.field(self, "channel", L("@HANDLE, CHANNEL URL OR CHANNEL ID", "@HANDLE, KANAL-LINK ODER KANAL-ID"),
                   initial, pad)
        tk.Label(self, text=L("EXAMPLES: @CHANNELNAME // YOUTUBE.COM/@CHANNELNAME // UC...\n"
                              "A channel you used before is restored without the API.",
                              "BEISPIELE: @KANALNAME // YOUTUBE.COM/@KANALNAME // UC...\n"
                              "Ein schon benutzter Kanal wird ohne API wiederhergestellt."),
                 bg=BG, fg=TEXT_MUTED, font=F("mono", 11, False), justify="left", anchor="w",
                 wraplength=px(520)).pack(fill="x", padx=pad, pady=(0, px(12)))
        blocked = bool(quota_blocked("units")) and not reserve_only()
        self.toggle = tk.Label(self, bg=BG, fg=YELLOW if blocked else CYAN, font=F("mono", 11), anchor="w",
                               cursor="hand2")
        self.toggle.pack(fill="x", padx=pad, pady=(0, px(8)))
        self.toggle.bind("<Button-1>", lambda e: self.set_open(not self.open))
        self.holder = tk.Frame(self, bg=BG)          # bleibt immer gepackt, damit die Reihenfolge stimmt
        self.holder.pack(fill="x")
        self.manual = tk.Frame(self.holder, bg=BG)
        if blocked:
            tk.Label(self.manual, text=L("The API quota is used up for today, so a new channel can't be looked up. "
                                         "Enter it manually:",
                                         "Das API-Kontingent ist für heute aufgebraucht, ein neuer Kanal kann nicht "
                                         "nachgeschlagen werden. Trag ihn manuell ein:"),
                     bg=BG, fg=YELLOW, font=F("ui", 13, False), justify="left", anchor="w",
                     wraplength=px(520)).pack(fill="x", padx=pad, pady=(0, px(10)))
        self.field(self.manual, "m_id", L("CHANNEL ID (UC...)", "KANAL-ID (UC...)"), "", pad)
        tk.Label(self.manual, text=L("On the channel page: More about this channel -> Share channel -> Copy channel ID",
                                     "Auf der Kanalseite: More about this channel -> Share channel -> Copy channel ID"),
                 bg=BG, fg=TEXT_MUTED, font=F("mono", 11, False), justify="left", anchor="w",
                 wraplength=px(520)).pack(fill="x", padx=pad, pady=(0, px(12)))
        self.field(self.manual, "m_title", L("NAME (OPTIONAL)", "NAME (OPTIONAL)"), "", pad)
        self.field(self.manual, "m_year", L("FIRST YEAR OF THE CHANNEL", "STARTJAHR DES KANALS"), "", pad, width=8,
                   fill=None)
        row = tk.Frame(self, bg=BG)
        row.pack(fill="x", padx=pad, pady=(px(4), pad))
        CutButton(row, L("SAVE", "SPEICHERN"), self.save, width=120).pack(side="left")
        CutButton(row, L("CANCEL", "ABBRECHEN"), self.destroy, width=120).pack(side="left", padx=(px(8), 0))
        self.open = False
        self.set_open(blocked)
        self.bind("<Return>", lambda e: self.save())
        self.bind("<Escape>", lambda e: self.destroy())
        style_window(self)
        self.update_idletasks()
        x = app.winfo_rootx() + (app.winfo_width() - self.winfo_width()) // 2
        y = app.winfo_rooty() + (app.winfo_height() - self.winfo_height()) // 3
        self.geometry(f"+{max(0, x)}+{max(0, y)}")
        self.grab_set()
        self.focus_force()

    def field(self, parent, key, label, initial, pad, width=52, fill="x"):
        tk.Label(parent, text=label, bg=BG, fg=TEXT_MUTED, font=F("mono", 11), anchor="w").pack(fill="x", padx=pad)
        v = tk.StringVar(value=initial)
        e = tk.Entry(parent, textvariable=v, bg=BG2, fg=CYAN, insertbackground=CYAN, relief="flat",
                     font=F("mono", 13, False), width=width, highlightthickness=1, highlightcolor=CYAN,
                     highlightbackground=mix(CYAN, BG, 0.4), selectbackground=mix(CYAN, BG, 0.3))
        e.pack(fill=fill, anchor="w", padx=pad, pady=(px(4), px(12)), ipady=px(6))
        self.vars[key] = v

    def set_open(self, open_):
        self.open = open_
        self.toggle.configure(text=("▾  " if open_ else "▸  ")
                              + L("NO API QUOTA? ENTER THE CHANNEL MANUALLY", "KEIN API-KONTINGENT? KANAL MANUELL EINTRAGEN"))
        if open_:
            self.manual.pack(fill="x")
        else:
            self.manual.pack_forget()

    def save(self):
        v = {k: var.get().strip() for k, var in self.vars.items()}
        if not self.open:                        # zugeklappt = manuelle Felder gelten nicht
            v.update(m_id="", m_title="", m_year="")
        if self.on_save(v) is not False:
            self.destroy()


class SettingsDialog(tk.Toplevel):
    """Playlist-Einstellungen: je eine Vorlage für Jahre und für eigene Listen (Reiter), Sichtbarkeit für alle."""

    KINDS = ("years", "lists")
    CHIPS = {"years": ("{channel}", "{handle}", "{year}", "{count}"),
             "lists": ("{name}", "{channel}", "{handle}", "{count}")}
    KEYS = {"years": ("playlist_template", "description_template"),
            "lists": ("list_title_template", "list_description_template")}

    def __init__(self, app, kind="years"):
        super().__init__(app, bg=BG)
        self.app = app
        self.title(L("Playlist settings", "Playlist-Einstellungen"))
        self.transient(app)
        self.minsize(*fit_min(px(640), px(560)))
        self.geometry(fit(px(720), px(660)))
        pad = px(18)
        st = app.settings
        self.sample_year = (app.selected_years() and [y for y in app.selected_years() if not is_list_key(y)] or YEARS
                            or [datetime.date.today().year - 1])[0]
        lists = load_lists()
        self.sample_list = list_key(lists[0]) if lists else None

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
        tabs = tk.Frame(body, bg=BG)
        tabs.pack(fill="x", pady=(0, px(10)))
        self.tab_btns = {}
        for k, label in (("years", L("YEARS", "JAHRE")), ("lists", L("CUSTOM LISTS", "EIGENE LISTEN"))):
            b = CutButton(tabs, label, lambda k=k: self.show(k), width=170, pad=24)
            b.pack(side="left", padx=(0, px(6)))
            self.tab_btns[k] = b
        Tooltip(self.tab_btns["years"], L("Template for the year playlists.", "Vorlage für die Jahres-Playlists."))
        Tooltip(self.tab_btns["lists"], L("Template for your custom lists. A list's own description always wins.",
                                          "Vorlage für deine eigenen Listen. Die eigene Beschreibung einer Liste hat "
                                          "immer Vorrang."))

        self.page_holder = tk.Frame(body, bg=BG)
        self.page_holder.pack(fill="x")
        self.pages, self.title_vars, self.title_entries, self.descs, self.prevs = {}, {}, {}, {}, {}
        for k in self.KINDS:
            self.build_page(k, st)

        self.lbl(body, L("VISIBILITY  (YEARS AND LISTS)", "SICHTBARKEIT  (JAHRE UND LISTEN)"))
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
                 bg=BG, fg=TEXT_MUTED, font=F("mono", 11, False), anchor="w", justify="left",
                 wraplength=px(640)).pack(fill="x", pady=(px(2), px(12)))

        self.kind = None
        self.show(kind)
        self.bind("<Escape>", lambda e: self.destroy())
        style_window(self)
        self.update_idletasks()
        x = app.winfo_rootx() + (app.winfo_width() - self.winfo_width()) // 2
        y = app.winfo_rooty() + max(0, (app.winfo_height() - self.winfo_height()) // 3)
        self.geometry(f"+{max(0, x)}+{max(0, y)}")
        self.grab_set()
        self.focus_force()

    def build_page(self, k, st):
        page = tk.Frame(self.page_holder, bg=BG)
        self.pages[k] = page
        tkey, dkey = self.KEYS[k]
        dflt_t = default_playlist_template() if k == "years" else default_list_title_template()
        dflt_d = default_description_template() if k == "years" else default_list_description_template()
        self.lbl(page, L("TITLE", "TITEL"))
        var = tk.StringVar(value=st.get(tkey) or dflt_t)
        self.title_vars[k] = var
        e = self.entry(page, var)
        self.title_entries[k] = e
        e.bind("<FocusIn>", lambda ev, w=e: setattr(self, "focus_field", w))

        chips = tk.Frame(page, bg=BG)
        chips.pack(fill="x", pady=(px(2), px(6)))
        tk.Label(chips, text=L("INSERT:", "EINFÜGEN:"), bg=BG, fg=TEXT_MUTED, font=F("mono", 10)).pack(side="left", padx=(0, px(6)))
        tips = {"{channel}": L("channel name", "Name des Kanals"), "{handle}": L("@handle of the channel", "@Handle des Kanals"),
                "{year}": L("year of the playlist", "Jahr der Playlist"), "{name}": L("name of the list", "Name der Liste"),
                "{count}": L("number of videos in the playlist", "Anzahl der Videos in der Playlist")}
        names = {"{channel}": L("CHANNEL", "KANAL"), "{handle}": "HANDLE", "{year}": L("YEAR", "JAHR"),
                 "{name}": "NAME", "{count}": L("COUNT", "ANZAHL")}
        for tok in self.CHIPS[k]:
            b = CutButton(chips, names[tok], lambda t=tok: self.insert(t))
            b.pack(side="left", padx=(0, px(6)))
            Tooltip(b, L(f"Inserts {tok}: {tips[tok]}.", f"Fügt {tok} ein: {tips[tok]}."))

        self.lbl(page, L("DESCRIPTION (OPTIONAL, LEAVE EMPTY FOR NONE)", "BESCHREIBUNG (OPTIONAL, LEER = KEINE)")
                 if k == "years" else
                 L("DESCRIPTION (FOR LISTS WITHOUT AN OWN ONE, EMPTY = NONE)",
                   "BESCHREIBUNG (FÜR LISTEN OHNE EIGENE, LEER = KEINE)"))
        d = tk.Text(page, height=4, wrap="word", bg=BG2, fg=CYAN, insertbackground=CYAN, relief="flat",
                    font=F("mono", 12, False), highlightthickness=1, highlightcolor=CYAN,
                    highlightbackground=mix(CYAN, BG, 0.4), selectbackground=mix(CYAN, BG, 0.3),
                    padx=px(6), pady=px(6))
        d.insert("1.0", st.get(dkey, dflt_d))
        d.pack(fill="x", pady=(px(4), px(8)))
        d.bind("<FocusIn>", lambda ev, w=d: setattr(self, "focus_field", w))
        d.bind("<KeyRelease>", lambda ev: self.preview())
        self.descs[k] = d
        var.trace_add("write", lambda *a: self.preview())
        p = tk.Label(page, text="", bg=BG, fg=TEXT, font=F("mono", 11, False), justify="left", anchor="w",
                     wraplength=px(640))
        p.pack(fill="x", pady=(0, px(8)))
        self.prevs[k] = p

    def show(self, k):
        if self.kind:
            self.pages[self.kind].pack_forget()
        self.kind = k
        self.pages[k].pack(fill="x")
        for kk, b in self.tab_btns.items():
            b.set_color(GREEN if kk == k else YELLOW)
            b.set_text(("● " if kk == k else "") + (L("YEARS", "JAHRE") if kk == "years" else L("CUSTOM LISTS", "EIGENE LISTEN")))
        self.focus_field = self.title_entries[k]
        self.preview()

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
        w = self.focus_field or self.title_entries[self.kind]
        w.insert("insert", tok)
        w.focus_set()
        self.preview()

    def set_privacy(self, opt):
        self.privacy = opt
        for o, b in self.vis_btns.items():
            b.set_color(GREEN if o == opt else YELLOW)
            b.set_text(("● " if o == opt else "") + privacy_label(o))

    def sample_values(self, k):
        if k == "years":
            return year_label(self.sample_year), template_values(self.sample_year)
        if self.sample_list:
            return list_name(self.sample_list), template_values(self.sample_list)
        name = L("My list", "Meine Liste")
        return name, {"channel": channel_name(), "handle": CHANNEL.get("handle", ""), "name": name, "year": name,
                      "count": 25}

    def check(self, k):
        t, d = self.title_vars[k].get(), self.descs[k].get("1.0", "end").strip()
        return _template_ok(t), (_template_ok(d) if d else True)

    def preview(self):
        k = getattr(self, "kind", None)
        if not k:
            return True
        t, d = self.title_vars[k].get(), self.descs[k].get("1.0", "end").strip()
        ok_t, ok_d = self.check(k)
        label, vals = self.sample_values(k)
        lines = [f"{L('PREVIEW', 'VORSCHAU')} {label}: " + (t.format(**vals) if ok_t else L("INVALID PLACEHOLDER IN TITLE", "UNGÜLTIGER PLATZHALTER IM TITEL"))]
        if d:
            short = d.format(**vals) if ok_d else L("INVALID PLACEHOLDER IN DESCRIPTION", "UNGÜLTIGER PLATZHALTER IN DER BESCHREIBUNG")
            lines.append(L("DESCRIPTION: ", "BESCHREIBUNG: ") + (short if len(short) < 140 else short[:137] + "..."))
        else:
            lines.append(L("DESCRIPTION: (NONE)", "BESCHREIBUNG: (KEINE)"))
        self.prevs[k].configure(text="\n".join(lines), fg=TEXT if ok_t and ok_d else RED)
        return ok_t and ok_d

    def reset(self):
        """Setzt den gerade offenen Reiter zurück."""
        k = self.kind
        self.title_vars[k].set(default_playlist_template() if k == "years" else default_list_title_template())
        self.descs[k].delete("1.0", "end")
        self.descs[k].insert("1.0", default_description_template() if k == "years" else default_list_description_template())
        self.preview()

    def save(self):
        for k in self.KINDS:
            if not all(self.check(k)):
                self.show(k)
                self.preview()
                self.app.write_log(L("SETTINGS NOT SAVED // INVALID PLACEHOLDER (USE {channel} {handle} {year} {name} {count})",
                                     "NICHT GESPEICHERT // UNGÜLTIGER PLATZHALTER (ERLAUBT: {channel} {handle} {year} {name} {count})"),
                                   "err")
                return
        st = self.app.settings
        for k in self.KINDS:
            tkey, dkey = self.KEYS[k]
            dflt = default_playlist_template() if k == "years" else default_list_title_template()
            st[tkey] = self.title_vars[k].get().strip() or dflt
            st[dkey] = self.descs[k].get("1.0", "end").strip()
        st["privacy"] = self.privacy
        save(SETTINGS_FILE, st)
        apply_templates(st)
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


def notes_lines(notes):
    """Release-Text (Markdown) in Zeilen für das Neuigkeiten-Fenster: [(art, text)], art = h, li, p.
    Bilder (z. B. das Roadmap-Banner) und Links werden weggelassen bzw. auf ihren Text gekürzt."""
    out = []
    for raw in (notes or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("!["):
            continue
        line = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", line).replace("**", "").replace("`", "")
        if line.startswith("#"):
            out.append(("h", line.lstrip("#").strip()))
        elif line[:2] in ("- ", "* "):
            out.append(("li", line[2:].strip()))
        else:
            out.append(("p", line))
    return out


class WhatsNewDialog(tk.Toplevel):
    """Einmalig nach einem Update: was in der neuen Version neu ist."""

    def __init__(self, app, version, notes):
        super().__init__(app, bg=BG)
        self.title(L("What's new", "Was ist neu"))
        self.transient(app)
        self.geometry(fit(px(640), px(560)))
        self.minsize(*fit_min(px(420), px(320)))
        pad = px(18)
        Section(self, L("WHAT'S NEW", "NEU IN") + f" // v{version}", "CHANGELOG // UPDATE").pack(
            fill="x", padx=pad, pady=(pad, px(10)))
        foot = tk.Frame(self, bg=BG)
        foot.pack(side="bottom", fill="x", padx=pad, pady=(px(10), pad))
        CutButton(foot, "OK", self.destroy, width=100).pack(side="right")
        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=pad)
        base = "Segoe UI" if "Segoe UI" in tkfont.families(self) else "TkDefaultFont"     # wie die Hilfe: scharf lesbar
        txt = tk.Text(body, wrap="word", bg=BG2, fg=TEXT, bd=0, relief="flat", highlightthickness=1,
                      highlightbackground=mix(CYAN, BG, 0.3), padx=px(16), pady=px(12), cursor="arrow",
                      font=(base, -px(15)), spacing1=px(2), spacing3=px(5), selectbackground=mix(CYAN, BG, 0.3))
        sb = ttk.Scrollbar(body, orient="vertical", command=txt.yview, style="NC.Vertical.TScrollbar")
        txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        txt.pack(side="left", fill="both", expand=True)
        txt.tag_configure("h", font=(base, -px(15), "bold"), foreground=CYAN, spacing1=px(14), spacing3=px(4))
        txt.tag_configure("li", lmargin1=px(4), lmargin2=px(22))
        txt.tag_configure("p", foreground=TEXT_DIM)
        lines = notes_lines(notes) or [("p", L("No release notes available.", "Keine Release-Notizen vorhanden."))]
        for kind, text in lines:
            txt.insert("end", ("•  " + text if kind == "li" else text.upper() if kind == "h" else text) + "\n", kind)
        txt.configure(state="disabled")
        txt.bind("<MouseWheel>", lambda e: txt.yview_scroll(int(-e.delta / 120), "units"))
        self.bind("<Escape>", lambda e: self.destroy())
        self.bind("<Return>", lambda e: self.destroy())
        style_window(self)
        self.update_idletasks()
        x = app.winfo_rootx() + (app.winfo_width() - self.winfo_width()) // 2
        y = app.winfo_rooty() + max(0, (app.winfo_height() - self.winfo_height()) // 3)
        self.geometry(f"+{max(0, x)}+{max(0, y)}")
        self.focus_force()


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
        # scrollt, falls der Bildschirm zu niedrig ist (z. B. 1080p bei großer Textgröße)
        self.area = ScrollArea(self, autohide=True)
        self.area.pack(fill="both", expand=True, padx=(pad, pad - px(12)), pady=(pad, 0))
        body = self.area.inner

        W = 524                                   # Inhaltsbreite: alle Knopfreihen und Karten enden bündig

        def hint(text, last=False):
            tk.Label(body, text=text, bg=BG, fg=TEXT_MUTED, font=F("mono", 11, False), anchor="w", justify="left",
                     wraplength=px(W)).pack(fill="x", pady=(px(2), px(18 if last else 8)))

        def btn_row(n):
            fr = tk.Frame(body, bg=BG)
            fr.pack(fill="x", pady=(0, px(2)))
            return fr, (W - 6 * (n - 1)) // n

        Section(body, L("LANGUAGE", "SPRACHE"), "UI // HELP // LOG").pack(fill="x", pady=(0, px(8)))
        lang_row, bw = btn_row(len(LANGUAGES))
        self.language = LANG
        self.lang_btns = {}
        for k, (code, name) in enumerate(LANGUAGES.items()):
            b = CutButton(lang_row, name, lambda c=code: self.set_language(c), width=bw)
            b.pack(side="left", padx=(0, px(6)) if k < len(LANGUAGES) - 1 else 0)
            self.lang_btns[code] = b
        hint(L("INTERFACE, TOOLTIPS, HELP AND LOG. CHRONOLIST RESTARTS WHEN THE LANGUAGE CHANGES.",
               "OBERFLÄCHE, TOOLTIPS, HILFE UND PROTOKOLL. CHRONOLIST STARTET BEIM SPRACHWECHSEL NEU."))
        self.set_language(LANG)

        Section(body, L("TEXT SIZE", "TEXTGRÖSSE"), "UI // SCALE").pack(fill="x", pady=(0, px(8)))
        size_row, bw = btn_row(len(UI_SCALES))
        self.ui_scale = app.settings.get("ui_scale", UI_SCALE_DEFAULT)
        if self.ui_scale not in UI_SCALES:
            self.ui_scale = UI_SCALE_DEFAULT
        self.size_btns = {}
        for k, v in enumerate(UI_SCALES):
            b = CutButton(size_row, f"{v} %", lambda v=v: self.set_ui_scale(v), width=bw, size=11, pad=18)
            b.pack(side="left", padx=(0, px(6)) if k < len(UI_SCALES) - 1 else 0)
            self.size_btns[v] = b
        hint(L("SIZE OF ALL TEXT AND CONTROLS. CHRONOLIST RESTARTS WHEN THE SIZE CHANGES.",
               "GRÖSSE VON TEXT UND BEDIENELEMENTEN. CHRONOLIST STARTET BEI EINER ÄNDERUNG NEU."))
        self.set_ui_scale(self.ui_scale)

        Section(body, L("AUTO-CHECK", "AUTO-PRÜFUNG"), "BACKGROUND // SCHEDULE").pack(fill="x", pady=(0, px(8)))
        mode_row, bw = btn_row(2)
        self.auto_mode = app.auto_mode
        self.mode_btns = {}
        for k, (code, txt) in enumerate((("interval", L("INTERVAL", "INTERVALL")),
                                         ("times", L("FIXED TIMES", "FESTE UHRZEITEN")))):
            b = CutButton(mode_row, txt, lambda c=code: self.set_mode(c), width=bw)
            b.pack(side="left", padx=(0, px(6)) if k == 0 else 0)
            self.mode_btns[code] = b
        holder = tk.Frame(body, bg=BG)
        holder.pack(fill="x", pady=(px(10), 0))

        # Intervall
        self.iv_frame = tk.Frame(holder, bg=BG)
        iv_row = tk.Frame(self.iv_frame, bg=BG)
        iv_row.pack(fill="x")
        bw = (W - 6 * (len(AUTO_INTERVALS) - 1)) // len(AUTO_INTERVALS)
        self.auto_interval = app.auto_interval
        self.iv_btns = {}
        for k, m in enumerate(AUTO_INTERVALS):
            b = CutButton(iv_row, interval_label(m), lambda m=m: self.set_interval(m), width=bw, size=11, pad=18)
            b.pack(side="left", padx=(0, px(6)) if k < len(AUTO_INTERVALS) - 1 else 0)
            self.iv_btns[m] = b
        man = tk.Frame(self.iv_frame, bg=BG)
        man.pack(fill="x", pady=(px(6), 0))
        self.iv_var = tk.StringVar()
        self.iv_entry = self.small_entry(man, self.iv_var, 7)
        self.iv_entry.pack(side="left", ipady=px(5))
        tk.Label(man, text=L("MINUTES  //  OWN INTERVAL", "MINUTEN  //  EIGENES INTERVALL"), bg=BG, fg=TEXT_MUTED,
                 font=F("mono", 11, False)).pack(side="left", padx=(px(10), 0))

        # Feste Uhrzeiten
        self.tm_frame = tk.Frame(holder, bg=BG)
        tm_row = tk.Frame(self.tm_frame, bg=BG)
        tm_row.pack(fill="x")
        self.tm_vars = []
        self.tm_strict = False          # leere Felder erst beim Speichern als Fehler zeigen
        have = list(app.auto_times)
        for k in range(AUTO_TIME_SLOTS):
            tm_row.columnconfigure(k, weight=1, uniform="tm")
            v = tk.StringVar(value=have[k] if k < len(have) else "")
            e = self.small_entry(tm_row, v, 5)
            e.grid(row=0, column=k, sticky="ew", ipady=px(5), padx=(0, px(6)) if k < AUTO_TIME_SLOTS - 1 else 0)
            self.placeholder(e, v, "HH:MM")
            v.trace_add("write", lambda *a: self.show_interval_note())
            self.tm_vars.append(v)

        self.iv_var.trace_add("write", lambda *a: self.typed_interval())
        self.set_interval(self.auto_interval)
        self.iv_note = tk.Label(body, text="", bg=BG, fg=TEXT_MUTED, font=F("mono", 11, False), anchor="w",
                                justify="left", wraplength=px(W))
        self.iv_note.pack(fill="x", pady=(px(8), px(10)))
        self.set_mode(self.auto_mode)
        self.alerts = app.alerts
        self.alert_card = ToggleCard(body, L("NEW VIDEO ALERT", "HINWEIS BEI NEUEN VIDEOS"),
                                     L("FLASH THE TASKBAR AND SHOW A WINDOWS NOTIFICATION",
                                       "TASKLEISTE BLINKT, WINDOWS-BENACHRICHTIGUNG"),
                                     self.alerts, self.toggle_alerts, compact=True)
        self.alert_card.pack(fill="x", pady=(0, px(18)))
        Tooltip(self.alert_card, L("When the AUTO-CHECK has added new videos, the taskbar icon flashes. If Chronolist "
                                   "is minimized or in the background, Windows also shows a notification.",
                                   "Hat die AUTO-PRÜFUNG neue Videos ergänzt, blinkt das Taskleisten-Symbol. Ist "
                                   "Chronolist minimiert oder im Hintergrund, zeigt Windows zusätzlich eine "
                                   "Benachrichtigung."))

        Section(body, L("START", "START"), "SYSTEM // BOOT").pack(fill="x", pady=(0, px(8)))
        self.autorun = app.autorun
        self.auto_card = ToggleCard(body, L("AUTO-RUN", "AUTOSTART"), L("START WORKING WHEN CHRONOLIST OPENS",
                                                                       "BEIM ÖFFNEN SOFORT LOSLEGEN"),
                                    self.autorun, self.toggle_auto, compact=True)
        self.auto_card.pack(fill="x", pady=(0, px(6)))
        Tooltip(self.auto_card, L("Start working automatically when Chronolist opens.",
                                  "Beim Öffnen von Chronolist automatisch starten."))
        self.boot = app.win_on
        self.boot_card = ToggleCard(body, L("START ON BOOT", "MIT WINDOWS STARTEN"),
                                    L("LAUNCH CHRONOLIST WHEN WINDOWS STARTS", "CHRONOLIST BEIM HOCHFAHREN ÖFFNEN"),
                                    self.boot, self.toggle_boot, available=winreg is not None, compact=True)
        self.boot_card.pack(fill="x", pady=(0, px(6)))
        Tooltip(self.boot_card, L("Start Chronolist automatically with Windows. Together with AUTO-RUN everything "
                                  "runs by itself.",
                                  "Chronolist automatisch mit Windows starten. Zusammen mit AUTOSTART läuft alles "
                                  "von allein."))
        self.minimized = app.win_min
        self.min_card = ToggleCard(body, L("START MINIMIZED", "MINIMIERT STARTEN"),
                                   L("OPEN IN THE TASKBAR, NOT ON SCREEN", "IN DER TASKLEISTE, NICHT AUF DEM BILDSCHIRM"),
                                   self.minimized, self.toggle_minimized, available=winreg is not None, compact=True,
                                   locked_sub=L("TURN ON START ON BOOT FIRST", "ZUERST MIT WINDOWS STARTEN EINSCHALTEN"))
        self.min_card.pack(fill="x", pady=(0, px(18)))
        self.min_card.set_locked(not self.boot)
        Tooltip(self.min_card, L("When Chronolist starts with Windows, its window opens minimized. "
                                 "Together with AUTO-CHECK it keeps your playlists up to date in the background.",
                                 "Startet Chronolist mit Windows, öffnet sich das Fenster minimiert. "
                                 "Zusammen mit AUTO-CHECK hält es deine Playlists im Hintergrund aktuell."))

        Section(body, "UPDATES", "GITHUB // RELEASES").pack(fill="x", pady=(0, px(8)))
        self.updates = bool(app.settings.get("check_updates", True))
        self.upd_card = ToggleCard(body, L("CHECK FOR UPDATES", "NACH UPDATES SUCHEN"),
                                   L("ASK GITHUB WHEN CHRONOLIST STARTS", "BEIM START BEI GITHUB FRAGEN"),
                                   self.updates, self.toggle_updates, compact=True)
        self.upd_card.pack(fill="x", pady=(0, px(4)))
        Tooltip(self.upd_card, L("On start, Chronolist asks GitHub once whether a newer version exists. Nothing else "
                                 "is sent.",
                                 "Beim Start fragt Chronolist einmal bei GitHub, ob es eine neuere Version gibt. "
                                 "Sonst wird nichts übertragen."))

        row = tk.Frame(self, bg=BG)
        row.pack(side="bottom", fill="x", padx=pad, pady=pad, before=self.area)
        CutButton(row, L("SAVE", "SPEICHERN"), self.save, width=120).pack(side="left")
        CutButton(row, L("CANCEL", "ABBRECHEN"), self.destroy, width=120).pack(side="left", padx=(px(8), 0))
        self.bind("<Escape>", lambda e: self.destroy())
        self.bind("<Return>", lambda e: self.save())
        self.update_idletasks()
        need = body.winfo_reqheight() + row.winfo_reqheight() + 3 * pad
        self.geometry(fit(px(560), need))
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

    def set_ui_scale(self, v):
        self.ui_scale = v
        for c, b in self.size_btns.items():
            b.set_color(GREEN if c == v else YELLOW)
            b.set_text(("● " if c == v else "") + f"{c} %")

    def toggle_auto(self):
        self.autorun = not self.autorun
        self.auto_card.update_value(self.autorun)

    def toggle_minimized(self):
        if not self.boot:
            return
        self.minimized = not self.minimized
        self.min_card.update_value(self.minimized)

    def small_entry(self, parent, var, width):
        return tk.Entry(parent, textvariable=var, bg=BG2, fg=CYAN, insertbackground=CYAN, relief="flat",
                        width=width, justify="center", font=F("mono", 13, False), highlightthickness=1,
                        highlightcolor=CYAN, highlightbackground=mix(CYAN, BG, 0.4),
                        selectbackground=mix(CYAN, BG, 0.3))

    def placeholder(self, entry, var, text):
        """Schwacher Platzhaltertext in einem leeren Feld (verschwindet beim Hineinklicken)."""
        lab = tk.Label(entry, text=text, bg=BG2, fg=mix(TEXT_MUTED, BG2, 0.45), font=F("mono", 12, False), cursor="xterm")
        lab.bind("<Button-1>", lambda e: entry.focus_set())

        def update(*_):
            if var.get() or entry.focus_get() is entry:
                lab.place_forget()
            else:
                lab.place(relx=0.5, rely=0.5, anchor="center")
        var.trace_add("write", update)
        entry.bind("<FocusIn>", update, add="+")
        entry.bind("<FocusOut>", lambda e: entry.after(1, update), add="+")
        update()

    def set_mode(self, code):
        """Entweder Intervall oder feste Uhrzeiten."""
        self.auto_mode = code
        for c, b in self.mode_btns.items():
            b.set_color(GREEN if c == code else YELLOW)
            b.set_text(("● " if c == code else "") + (L("INTERVAL", "INTERVALL") if c == "interval"
                                                       else L("FIXED TIMES", "FESTE UHRZEITEN")))
        (self.tm_frame if code == "interval" else self.iv_frame).pack_forget()
        (self.iv_frame if code == "interval" else self.tm_frame).pack(fill="x")
        self.show_interval_note()

    def typed_times(self):
        """(gültige Uhrzeiten sortiert, ungültige Eingaben)"""
        ok, bad = set(), []
        for v in self.tm_vars:
            t = parse_time(v.get())
            if t is None:
                bad.append(v.get().strip())
            elif t:
                ok.add(t)
        return sorted(ok), bad

    def set_interval(self, m):
        self.auto_interval = m
        for v, b in self.iv_btns.items():
            b.set_color(GREEN if v == m else YELLOW)
            b.set_text(("● " if v == m else "") + interval_label(v))
        if self.iv_var.get() != str(m):
            self.iv_var.set(str(m))

    def typed_interval(self):
        """Eigenes Intervall getippt: gültige Werte (Vielfache von 5, mindestens 15) gelten sofort."""
        v = self.iv_var.get().strip()
        if v.isdigit() and int(v) % AUTO_STEP == 0 and AUTO_MIN <= int(v) <= AUTO_MAX:
            self.auto_interval = int(v)
            for k, b in self.iv_btns.items():
                b.set_color(GREEN if k == self.auto_interval else YELLOW)
                b.set_text(("● " if k == self.auto_interval else "") + interval_label(k))
        self.show_interval_note()

    def show_interval_note(self):
        if not hasattr(self, "iv_note"):
            return
        tail = L(" SWITCH AUTO-CHECK ON IN THE MAIN WINDOW, THEN SET THE AUTO SWITCH ON EACH CARD YOU WANT KEPT UP "
                 "TO DATE. A CHECK USES ONLY A FEW API UNITS AND IS SKIPPED WHEN THE QUOTA IS USED UP.",
                 " AUTO-CHECK IM HAUPTFENSTER EINSCHALTEN UND DANN BEI JEDER KARTE, DIE AKTUELL BLEIBEN SOLL, DEN "
                 "AUTO-SCHALTER SETZEN. EINE PRÜFUNG KOSTET NUR WENIGE API-EINHEITEN UND ENTFÄLLT, WENN DAS "
                 "KONTINGENT AUFGEBRAUCHT IST.")
        if self.auto_mode == "times":
            ok, bad = self.typed_times()
            if bad:
                self.iv_note.configure(fg=RED, text=L(f"INVALID TIME: {', '.join(bad)} // USE HH:MM, E.G. 08:00 OR 20:30.",
                                                      f"UNGÜLTIGE UHRZEIT: {', '.join(bad)} // FORMAT HH:MM, Z. B. 08:00 ODER 20:30."))
            elif not ok and self.tm_strict:
                self.iv_note.configure(fg=RED, text=L("ENTER AT LEAST ONE TIME, E.G. 08:00.",
                                                      "MINDESTENS EINE UHRZEIT EINTRAGEN, Z. B. 08:00."))
            else:
                self.iv_note.configure(fg=TEXT_MUTED, text=L(
                    f"UP TO {AUTO_TIME_SLOTS} TIMES PER DAY, EMPTY FIELDS ARE IGNORED. CHRONOLIST MUST BE OPEN AT THAT "
                    f"TIME; A TIME THAT FALLS INTO ANOTHER RUN IS DONE RIGHT AFTER IT.",
                    f"BIS ZU {AUTO_TIME_SLOTS} UHRZEITEN AM TAG, LEERE FELDER ZÄHLEN NICHT. CHRONOLIST MUSS ZU DER ZEIT "
                    f"OFFEN SEIN; FÄLLT EINE UHRZEIT IN EINEN ANDEREN LAUF, WIRD SIE DIREKT DANACH NACHGEHOLT.") + tail)
            return
        v = self.iv_var.get().strip()
        ok = v.isdigit() and int(v) % AUTO_STEP == 0 and AUTO_MIN <= int(v) <= AUTO_MAX
        self.iv_note.configure(
            text=L(f"MULTIPLES OF {AUTO_STEP}, FROM {AUTO_MIN} MINUTES UP TO 24 HOURS.",
                   f"VIELFACHE VON {AUTO_STEP}, VON {AUTO_MIN} MINUTEN BIS 24 STUNDEN.") + tail
            if ok or not v else L(f"INVALID // USE A MULTIPLE OF {AUTO_STEP} BETWEEN {AUTO_MIN} AND {AUTO_MAX}. "
                                  f"THE LAST VALID INTERVAL ({interval_label(self.auto_interval)}) APPLIES.",
                                  f"UNGÜLTIG // EIN VIELFACHES VON {AUTO_STEP} ZWISCHEN {AUTO_MIN} UND {AUTO_MAX} "
                                  f"VERWENDEN. ES GILT DAS LETZTE GÜLTIGE INTERVALL ({interval_label(self.auto_interval)})."),
            fg=TEXT_MUTED if ok or not v else RED)

    def toggle_alerts(self):
        self.alerts = not self.alerts
        self.alert_card.update_value(self.alerts)

    def toggle_boot(self):
        self.boot = not self.boot
        self.boot_card.update_value(self.boot)
        self.min_card.set_locked(not self.boot)

    def toggle_updates(self):
        self.updates = not self.updates
        self.upd_card.update_value(self.updates)

    def save(self):
        times, bad = self.typed_times()
        if self.auto_mode == "times" and (bad or not times):
            self.tm_strict = True
            self.show_interval_note()                # Fehler steht rot unter den Feldern
            return
        if not times:
            times = list(self.app.auto_times)
        st = self.app.settings
        st["check_updates"] = self.updates
        self.app.set_windows(self.boot, self.minimized and self.boot)
        self.app.autorun = self.autorun
        self.app.auto_interval = self.auto_interval
        self.app.auto_mode, self.app.auto_times = self.auto_mode, times
        self.app.auto_next = self.app.plan_auto(first=True)
        self.app.auto_card.sub = self.app.auto_sub()
        self.app.auto_card.redraw()
        self.app.alerts = self.alerts
        st.update(auto_interval=self.auto_interval, auto_mode=self.auto_mode, auto_times=times,
                  start_minimized=self.minimized, alerts=self.alerts)
        st["autorun"] = self.autorun
        lang_changed = self.language != LANG
        size_changed = self.ui_scale != st.get("ui_scale", UI_SCALE_DEFAULT)
        st["ui_scale"] = self.ui_scale
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
        if lang_changed or size_changed:
            if self.app.worker and self.app.worker.is_alive():
                self.app.write_log(L("THE CHANGE APPLIES AFTER THE NEXT START OF CHRONOLIST",
                                     "DIE ÄNDERUNG GILT AB DEM NÄCHSTEN START VON CHRONOLIST"), "info")
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
            "Zeit ist der API-Knopf gelb und zeigt LIMIT.\n"
            "Chronolist zählt die verbrauchten Einheiten mit und hört schon kurz vor dem Ende auf: 20 Einheiten bleiben "
            "immer übrig, damit du auch dann noch einen Kanal hinzufügen oder tauschen kannst. Das Mitzählen ist nur "
            "eine Schätzung, weil andere Programme im selben Google-Projekt nicht mitgezählt werden."),
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
    ("dt", "Kontingent"),
    ("dd", "API-KONTINGENT links neben HILFE: wie viele API-Einheiten Chronolist heute verbraucht hat, von 10000. Cyan bis 80 %, "
           "gelb darüber, rot, wenn das Kontingent aufgebraucht oder nur noch die Reserve übrig ist; dann steht dort, "
           "wann es zurückgesetzt wird. Das ist eine Schätzung: andere Programme im selben Google-Projekt zählen "
           "nicht mit."),
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
          "Die Vorschau zeigt sofort, wie viele Videos passen. Videos schließt du aus (mit Strg/Umschalt+Klick mehrere wählen, dann AUSSCHLIESSEN), und in der Ansicht ALLE CHANNEL-VIDEOS nimmst du auch Videos auf, die nicht passen "
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
           "Mit Windows starten: startet Chronolist zusammen mit Windows. Zusammen mit Autostart läuft dann alles von allein.\n"
           "Minimiert starten: Chronolist öffnet sich beim Start mit Windows minimiert in der Taskleiste. Nur wählbar, wenn Mit Windows starten an ist.\n"
           "Nach Updates suchen: ob Chronolist beim Start bei GitHub nach einer neuen Version fragt."),
    ("dt", "AUTO-CHECK"),
    ("dd", "Die Karte AUTO-CHECK im Hauptfenster schaltet die automatische Suche nach neuen Videos ein. Dann zeigt "
           "jede Karte (Jahr oder Liste) einen kleinen AUTO-Schalter. Nur Karten mit eingeschaltetem AUTO werden "
           "aktuell gehalten. Bei Jahren ist das nur beim laufenden Jahr sinnvoll, bei Listen kommt jedes neue "
           "passende Video dazu.\n"
           "Wann geprüft wird, stellst du unter PROGRAMM ein, entweder als Intervall oder zu festen Uhrzeiten. "
           "Intervall: 30 Minuten, 1, 2 oder 6 Stunden oder ein eigener Wert (Vielfache von 5, mindestens 15 Minuten). "
           "Der Abstand läuft ab dem Ende des letzten Durchlaufs, auch eines manuellen. Feste Uhrzeiten: bis zu "
           "vier Uhrzeiten am Tag (HH:MM). Chronolist muss dann offen sein; fällt eine Uhrzeit in einen anderen "
           "Lauf, wird die Prüfung direkt danach nachgeholt.\n"
           "Jede Prüfung kostet nur wenige API-Einheiten und entfällt, wenn das Kontingent aufgebraucht ist. Läuft "
           "gerade ein anderer Durchlauf, wartet sie. Sie öffnet nie den Browser (kein FALLBACK). Voraussetzung: "
           "Chronolist ist schon einmal angemeldet und die Videos des Jahres sind schon gesucht.\n"
           "Auf jeder Karte mit AUTO steht neben dem AUTO-Schalter, wann zuletzt geprüft wurde, und wie viele Videos dabei dazukamen "
           "(z. B. ZULETZT 14:02 // +2 NEU).\n"
           "Hinweis bei neuen Videos (unter PROGRAMM ein- und ausschaltbar): Hat die Prüfung etwas ergänzt, blinkt "
           "das Taskleisten-Symbol. Ist Chronolist minimiert oder im Hintergrund, zeigt Windows zusätzlich eine "
           "Benachrichtigung."),
    ("dt", "PLAYLISTS"),
    ("dd", "Titel und Beschreibung: Du kannst Platzhalter verwenden, die beim Anlegen ersetzt werden. {channel} wird "
           "zum Kanalnamen, {handle} zum @Handle, {year} zum Jahr und {count} zur Anzahl der Videos in diesem Jahr. "
           "Die Knöpfe unter dem Titelfeld fügen sie an der Stelle ein, an der der Cursor steht. Darunter siehst du "
           "eine Vorschau. Lässt du die Beschreibung leer, bekommt die Playlist keine.\n"
           "Zwei Reiter: JAHRE gilt für die Jahres-Playlists, EIGENE LISTEN für deine Listen. Bei Listen gibt es statt "
           "{year} den Platzhalter {name} für den Listennamen; der Titel ist standardmäßig nur der Name. Hat eine Liste "
           "im Listen-Editor eine eigene Beschreibung, gilt diese (auch dort gehen Platzhalter), sonst die Vorlage.\n"
           "Sichtbarkeit: öffentlich, nicht gelistet (nur mit Link) oder privat.\n"
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
          "pro Kanal und der Ordner browser_profile mit der Anmeldung im Edge-Fenster. "
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
            "yellow and shows LIMIT.\n"
            "Chronolist counts the units it uses and stops a little early: 20 units are always kept so you can still "
            "add or change a channel. The count is only an estimate, because other programs in the same Google "
            "project are not counted."),
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
    ("dt", "Quota"),
    ("dd", "API QUOTA left of HELP: how many API units Chronolist used today, out of 10000. Cyan up to 80 %, yellow above, red "
           "when the quota is used up or only the reserve is left; then it shows when it resets. This is an estimate: "
           "other programs in the same Google project are not counted."),
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
          "Exclude videos (select with Ctrl/Shift+click, then EXCLUDE); the ALL CHANNEL VIDEOS view lets you include videos that do not match or add them for good with a link or video ID.\n"
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
           "Start on boot: starts Chronolist together with Windows. Together with auto-run everything runs by itself.\n"
           "Start minimized: when Chronolist starts with Windows, its window opens minimized in the taskbar. Only available when Start on boot is on.\n"
           "Check for updates: whether Chronolist asks GitHub for a new version on start."),
    ("dt", "AUTO-CHECK"),
    ("dd", "The AUTO-CHECK card in the main window switches on the automatic search for new videos. Then every card "
           "(year or list) shows a small AUTO switch. Only cards with AUTO switched on are kept up to date. For years "
           "that only makes sense for the current year, for lists every new matching video is added.\n"
           "Under PROGRAM you choose when it checks: at an interval or at fixed times. Interval: 30 minutes, 1, 2 or 6 "
           "hours, or your own value (multiples of 5, at least 15 minutes). The wait starts when the last run ends, a "
           "manual one included. Fixed times: up to four times per day (HH:MM). Chronolist must be open then; if a "
           "time falls into another run, the check follows right after it.\n"
           "A check costs only a few API units and is skipped when the quota is used up. If another run is active it "
           "waits. It never opens the browser (no FALLBACK). It needs a finished sign-in and a year whose videos were "
           "already scanned.\n"
           "Every card with AUTO shows next to the AUTO switch when it was last checked and how many videos were added (e.g. "
           "LAST CHECK 14:02 // +2 NEW).\n"
           "New video alert (can be switched off under PROGRAM): when a check added videos, the taskbar icon flashes. "
           "If Chronolist is minimized or in the background, Windows also shows a notification."),
    ("dt", "PLAYLISTS"),
    ("dd", "Title and description: you can use placeholders that are filled in when the playlist is created. "
           "{channel} becomes the channel name, {handle} the @handle, {year} the year and {count} the number of "
           "videos in that year. The buttons below the title field insert them at the cursor. Below you see a "
           "preview. Leave the description empty for no description.\n"
           "Two tabs: YEARS is for the year playlists, CUSTOM LISTS for your lists. For lists, {name} (the list name) "
           "takes the place of {year}; by default the title is just the name. If a list has its own description in "
           "the list editor, that one is used (placeholders work there too), otherwise the template.\n"
           "Visibility: public, unlisted (link only) or private.\n"
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
          "(settings), quota.json (remembered daily limit), the data folder with progress per channel, "
          "and the browser_profile folder with the sign-in for the Edge window. None of it is uploaded "
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
        self.geometry(fit(px(720), px(760)))
        self.minsize(*fit_min(px(480), px(400)))
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
        self.geometry(fit(px(760), px(720)))
        self.minsize(*fit_min(px(480), px(360)))
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


class SuggestDialog(tk.Toplevel):
    """Serien-Vorschläge aus den Titeln: Auswahl öffnet den Listen-Editor mit ausgefüllten Feldern."""

    def __init__(self, app):
        super().__init__(app, bg=BG)
        self.app = app
        self.title(L("List suggestions", "Listen-Vorschläge"))
        self.transient(app)
        self.minsize(*fit_min(px(560), px(420)))
        self.geometry(fit(px(620), px(560)))
        pad = px(18)
        row = tk.Frame(self, bg=BG)
        row.pack(side="bottom", fill="x", padx=pad, pady=pad)
        self.make_btn = CutButton(row, L("CREATE LIST", "LISTE ANLEGEN"), self.make, width=170)
        self.make_btn.pack(side="left")
        CutButton(row, L("CLOSE", "SCHLIESSEN"), self.destroy, width=120).pack(side="left", padx=(px(8), 0))
        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=pad, pady=(pad, 0))
        Section(body, L("SERIES IN YOUR TITLES", "SERIEN IN DEINEN TITELN"), "SUGGESTIONS").pack(fill="x", pady=(0, px(8)))
        self.items = suggest_series()
        titles = load(titles_file(), {})
        if self.items:
            txt = L("TITLES THAT START THE SAME WAY AND END IN AN EPISODE NUMBER. LISTS YOU ALREADY HAVE ARE LEFT OUT. "
                    "PICK ONE: THE EDITOR OPENS WITH NAME AND KEYWORD FILLED IN.",
                    "TITEL, DIE GLEICH ANFANGEN UND MIT EINER FOLGENNUMMER WEITERGEHEN. VORHANDENE LISTEN FEHLEN HIER. "
                    "WÄHLE EINE AUS: DER EDITOR ÖFFNET SICH MIT NAME UND STICHWORT.")
        elif not titles:
            txt = L("NO TITLES STORED YET. THEY ARE LOADED ON THE NEXT START.", "NOCH KEINE TITEL GESPEICHERT. SIE WERDEN BEIM NÄCHSTEN START GELADEN.")
        else:
            txt = L("NO NEW SERIES FOUND.", "KEINE NEUEN SERIEN GEFUNDEN.")
        tk.Label(body, text=txt, bg=BG, fg=TEXT_MUTED, font=F("mono", 10, False), anchor="w", justify="left",
                 wraplength=px(560)).pack(fill="x", pady=(0, px(8)))
        fb = tk.Frame(body, bg=BG)
        fb.pack(fill="x", pady=(0, px(6)))
        tk.Label(fb, text=L("SEARCH", "SUCHE"), bg=BG, fg=TEXT_MUTED, font=F("mono", 11)).pack(side="left", padx=(0, px(8)))
        self.q = tk.StringVar()
        qe = tk.Entry(fb, textvariable=self.q, bg=BG2, fg=CYAN, insertbackground=CYAN, relief="flat",
                      font=F("mono", 12, False), highlightthickness=1, highlightcolor=CYAN,
                      highlightbackground=mix(CYAN, BG, 0.4), selectbackground=mix(CYAN, BG, 0.3))
        qe.pack(side="left", fill="x", expand=True, ipady=px(5))
        Tooltip(qe, L("Shows only series whose name contains this text (several terms separated by commas: any of them).",
                      "Zeigt nur Serien, deren Name diesen Text enthält (mehrere Begriffe mit Komma: einer davon genügt)."))
        self.q.trace_add("write", lambda *a: self.fill())
        wrap = tk.Frame(body, bg=BG)
        wrap.pack(fill="both", expand=True)
        sb = tk.Scrollbar(wrap, orient="vertical")
        self.lb = tk.Listbox(wrap, bg=BG2, fg=TEXT, selectbackground=mix(CYAN, BG, 0.3), selectforeground=TEXT,
                             font=F("mono", 11, False), relief="flat", highlightthickness=1,
                             highlightbackground=mix(CYAN, BG, 0.3), activestyle="none", exportselection=False,
                             selectmode="browse", yscrollcommand=sb.set)
        sb.configure(command=self.lb.yview)
        sb.pack(side="right", fill="y")
        self.lb.pack(side="left", fill="both", expand=True)
        self.shown = []
        self.fill()
        self.lb.bind("<Double-Button-1>", lambda e: self.make())
        self.lb.bind("<<ListboxSelect>>", lambda e: self.make_btn.set_enabled(bool(self.lb.curselection())))
        self.bind("<Escape>", lambda e: self.destroy())
        style_window(self)
        self.update_idletasks()
        x = app.winfo_rootx() + (app.winfo_width() - self.winfo_width()) // 2
        y = app.winfo_rooty() + max(0, (app.winfo_height() - self.winfo_height()) // 3)
        self.geometry(f"+{max(0, x)}+{max(0, y)}")
        self.grab_set()
        self.focus_force()

    def fill(self):
        qs = parse_terms(self.q.get())
        self.shown = [it for it in self.items if not qs or any(t in norm_text(it["name"]) for t in qs)]
        self.lb.delete(0, "end")
        for it in self.shown:
            yrs = str(it["from"]) if it["from"] == it["to"] else f"{it['from']}–{it['to']}"
            self.lb.insert("end", f"{it['count']:>5}  {it['name']}   ({yrs})")
        self.make_btn.set_enabled(False)

    def make(self):
        sel = self.lb.curselection()
        if not sel:
            return
        it = self.shown[sel[0]]
        app = self.app
        self.destroy()
        ListDialog(app, None, template={"name": it["name"], "any": [it["name"]]})


class ListDialog(tk.Toplevel):
    """Stichwortliste anlegen/bearbeiten: Suchbegriffe, Live-Vorschau, einzelne Videos ausschließen/hinzufügen."""
    SHOW_MAX = 4000

    def __init__(self, app, key, parent=None, template=None):
        super().__init__(app, bg=BG)
        self.app, self.key, self.parent_dlg = app, key, parent
        lst = (find_list(key) if key else None) or {}
        if template and not lst:
            # Vorlage: Name und Regeln übernehmen, aber nicht einzeln aufgenommene/ausgeschlossene Videos
            lst = {k: template[k] for k in ("name", "desc", "any", "all", "none", "from", "to") if k in template}
        self.lst_id = lst.get("id") or None
        self.add, self.skip = set(lst.get("add") or []), set(lst.get("skip") or [])
        self.title(L("Custom list", "Eigene Liste"))
        self.transient(parent or app)
        self.minsize(*fit_min(px(1000), px(600)))
        self.geometry(fit(px(1140), px(700)))
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
        left = tk.Frame(body, bg=BG, width=px(430))          # links: Einstellungen und Filter, rechts: Vorschau der Videos
        left.pack(side="left", fill="y", padx=(0, px(18)))
        left.pack_propagate(False)
        right = tk.Frame(body, bg=BG)
        right.pack(side="left", fill="both", expand=True)
        Section(left, L("LIST SETTINGS", "LISTEN-EINSTELLUNGEN"), "NAME").pack(fill="x", pady=(0, px(10)))

        templates = load_lists() if not self.lst_id else []
        if templates:
            tk.Label(left, text=L("TEMPLATE  (OPTIONAL, COPIES NAME AND FILTER)", "VORLAGE  (OPTIONAL, ÜBERNIMMT NAME UND FILTER)"),
                     bg=BG, fg=TEXT_MUTED, font=F("mono", 11), anchor="w").pack(fill="x")
            self.tpl_lb = tk.Listbox(left, bg=BG2, fg=TEXT, selectbackground=mix(CYAN, BG, 0.3), selectforeground=TEXT,
                                     font=F("mono", 11, False), relief="flat", highlightthickness=1,
                                     highlightbackground=mix(CYAN, BG, 0.3), activestyle="none", exportselection=False,
                                     height=min(len(templates) + 1, 3))
            self.tpl_lb.insert("end", L("— NONE (EMPTY LIST) —", "— KEINE (LEERE LISTE) —"))
            for t_ in templates:
                self.tpl_lb.insert("end", t_.get("name") or "?")
            self.tpl_lb.selection_set(0)
            self.tpl_lb.pack(fill="x", pady=(px(3), px(8)))
            self.tpl_lb.bind("<<ListboxSelect>>", lambda e, t=templates: self.pick_template(t))
        self.name_var = tk.StringVar(value=lst.get("name", ""))
        self.field(left, L("NAME  (THE PLAYLIST TITLE BY DEFAULT)", "NAME  (STANDARDMÄSSIG DER PLAYLIST-TITEL)"), self.name_var)
        self.lbl_desc = tk.Label(left, text=L("DESCRIPTION  (OPTIONAL, EMPTY = LISTS TEMPLATE)",
                                              "BESCHREIBUNG  (OPTIONAL, LEER = LISTEN-VORLAGE)"),
                                 bg=BG, fg=TEXT_MUTED, font=F("mono", 11), anchor="w")
        self.lbl_desc.pack(fill="x")
        self.desc_box = tk.Text(left, height=2, bg=BG2, fg=CYAN, insertbackground=CYAN, relief="flat", wrap="word",
                                font=F("mono", 12, False), highlightthickness=1, highlightcolor=CYAN,
                                highlightbackground=mix(CYAN, BG, 0.4), selectbackground=mix(CYAN, BG, 0.3), padx=px(6), pady=px(4))
        self.desc_box.insert("1.0", lst.get("desc", ""))
        Tooltip(self.desc_box, L("Own description for this list. Placeholders: {name} {channel} {handle} {count}. "
                                 "Empty = the CUSTOM LISTS template under PLAYLISTS.",
                                 "Eigene Beschreibung für diese Liste. Platzhalter: {name} {channel} {handle} {count}. "
                                 "Leer = die Vorlage im Reiter EIGENE LISTEN unter PLAYLISTS."))
        self.desc_box.pack(fill="x", pady=(px(3), px(12)))

        Section(left, L("FILTER", "FILTER"), "KEYWORDS // YEARS").pack(fill="x", pady=(0, px(10)))
        tk.Label(left, text=L("A VIDEO IS INCLUDED IF ITS TITLE CONTAINS ...", "EIN VIDEO KOMMT IN DIE LISTE, WENN SEIN TITEL ... ENTHÄLT"),
                 bg=BG, fg=CYAN, font=F("mono", 11), anchor="w").pack(fill="x", pady=(0, px(6)))
        self.any_var = tk.StringVar(value=", ".join(lst.get("any") or []))
        self.rule_field(left, L("ANY OF", "EINES VON"), L("at least one, comma = or, e.g. minecraft, zelda",
                                                          "mindestens eines, Komma = oder, z. B. minecraft, zelda"), self.any_var)
        self.all_var = tk.StringVar(value=", ".join(lst.get("all") or []))
        self.rule_field(left, L("ALL OF", "ALLE VON"), L("(optional) every one of them, e.g. season 1",
                                                         "(optional) jedes davon, z. B. season 1"), self.all_var)
        self.none_var = tk.StringVar(value=", ".join(lst.get("none") or []))
        self.rule_field(left, L("NONE OF", "KEINES VON"), L("(optional) not a single one, e.g. live",
                                                            "(optional) kein einziges davon, z. B. live"), self.none_var)
        yr = tk.Frame(left, bg=BG)
        yr.pack(fill="x", pady=(0, px(8)))
        tk.Label(yr, text=L("YEARS  (OPTIONAL)", "JAHRE  (OPTIONAL)"), bg=BG, fg=TEXT_MUTED, font=F("mono", 11),
                 anchor="w").pack(side="left")
        self.from_var = tk.StringVar(value=str(lst.get("from") or ""))
        self.to_var = tk.StringVar(value=str(lst.get("to") or ""))
        for var, txt in ((self.from_var, L("FROM", "VON")), (self.to_var, L("TO", "BIS"))):
            tk.Label(yr, text=txt, bg=BG, fg=TEXT_MUTED, font=F("mono", 11)).pack(side="left", padx=(px(14), px(6)))
            e = tk.Entry(yr, textvariable=var, bg=BG2, fg=CYAN, insertbackground=CYAN, relief="flat", width=6,
                         font=F("mono", 13, False), highlightthickness=1, highlightcolor=CYAN,
                         highlightbackground=mix(CYAN, BG, 0.4), selectbackground=mix(CYAN, BG, 0.3))
            e.pack(side="left", ipady=px(4))
        tk.Label(left, text=L("UPPER/LOWER CASE IS IGNORED. A TERM MATCHES ANYWHERE IN THE TITLE.",
                              "GROSS-/KLEINSCHREIBUNG ZÄHLT NICHT. EIN BEGRIFF PASST ÜBERALL IM TITEL."),
                 bg=BG, fg=TEXT_MUTED, font=F("mono", 11, False), anchor="w", justify="left", wraplength=px(420)).pack(fill="x", pady=(0, px(8)))

        self.rule_sum = tk.Label(left, text="", bg=BG, fg=TEXT_DIM, font=F("mono", 11, False), anchor="w", justify="left",
                                 wraplength=px(420))
        self.rule_sum.pack(fill="x", pady=(0, px(6)))
        Section(right, L("VIDEOS", "VIDEOS"), "PREVIEW").pack(fill="x", pady=(0, px(10)))
        self.count = tk.Label(right, text="", bg=BG, fg=CYAN, font=F("mono", 12), anchor="w")
        self.count.pack(fill="x")
        self.note = tk.Label(right, text="", bg=BG, fg=YELLOW, font=F("mono", 10, False), anchor="w", justify="left",
                             wraplength=px(700))
        self.note.pack(fill="x", pady=(0, px(4)))

        self.view = "hits"
        bar = tk.Frame(right, bg=BG)
        bar.pack(fill="x", pady=(0, px(6)))
        self.view_btns = {}
        for v_, txt_ in (("hits", L("FILTER RESULTS", "FILTERERGEBNISSE")), ("all", L("ALL CHANNEL VIDEOS", "ALLE CHANNEL-VIDEOS"))):
            b = CutButton(bar, txt_, lambda v_=v_: self.set_view(v_), width=190)
            b.pack(side="left", padx=(0, px(6)))
            self.view_btns[v_] = b
        Tooltip(self.view_btns["all"], L("Shows every video of the channel, also those that do not match, so you can add single ones.",
                                         "Zeigt alle Videos des Kanals, auch die, die nicht passen, damit du einzelne aufnehmen kannst."))
        self.filter_var = tk.StringVar()
        fe = tk.Entry(bar, textvariable=self.filter_var, bg=BG2, fg=CYAN, insertbackground=CYAN, relief="flat",
                      font=F("mono", 12, False), highlightthickness=1, highlightcolor=CYAN,
                      highlightbackground=mix(CYAN, BG, 0.4), selectbackground=mix(CYAN, BG, 0.3), width=16)
        fe.pack(side="right", ipady=px(5))
        tk.Label(bar, text=L("SEARCH", "SUCHE"), bg=BG, fg=TEXT_MUTED, font=F("mono", 11)).pack(side="right", padx=(0, px(6)))
        Tooltip(fe, L("Searches all videos of the channel for this text (several terms separated by commas: any of them). "
                      "Videos that are not in the list yet are grey, select them and press INCLUDE.",
                      "Durchsucht alle Videos des Kanals nach diesem Text (mehrere Begriffe mit Komma: einer davon genügt). "
                      "Videos, die noch nicht in der Liste sind, sind grau: markieren und AUFNEHMEN drücken."))
        self.filter_var.trace_add("write", lambda *a: self.schedule())

        tools = tk.Frame(right, bg=BG)
        tools.pack(side="bottom", fill="x", pady=(px(6), 0))
        self.excl_btn = CutButton(tools, L("EXCLUDE SELECTED", "AUSSCHLIESSEN"), self.exclude_selected, width=150)
        self.excl_btn.pack(side="left")
        self.incl_btn = CutButton(tools, L("INCLUDE SELECTED", "AUFNEHMEN"), self.include_selected, width=150)
        self.incl_btn.pack(side="left", padx=(px(6), 0))
        self.add_var = tk.StringVar()
        ae = tk.Entry(tools, textvariable=self.add_var, bg=BG2, fg=CYAN, insertbackground=CYAN, relief="flat",
                      font=F("mono", 12, False), highlightthickness=1, highlightcolor=CYAN,
                      highlightbackground=mix(CYAN, BG, 0.4), selectbackground=mix(CYAN, BG, 0.3), width=14)
        CutButton(tools, L("ADD VIDEO", "VIDEO HINZU"), self.add_video, width=120).pack(side="right")
        ae.pack(side="right", padx=(0, px(6)), ipady=px(5))
        Tooltip(ae, L("Paste a YouTube link or video ID of a video of this channel to always include it.",
                      "YouTube-Link oder Video-ID eines Videos dieses Kanals einfügen, um es immer aufzunehmen."))
        ae.bind("<Return>", lambda e: (self.add_video(), "break")[1])

        wrap = tk.Frame(right, bg=BG)
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
        if template and not self.lst_id:
            self.title(L("Custom list (copy)", "Eigene Liste (Kopie)"))
        style_window(self)
        self.update_idletasks()
        x = app.winfo_rootx() + (app.winfo_width() - self.winfo_width()) // 2
        y = app.winfo_rooty() + max(0, (app.winfo_height() - self.winfo_height()) // 3)
        self.geometry(f"+{max(0, x)}+{max(0, y)}")
        self.grab_set()
        self.focus_force()

    def pick_template(self, templates):
        sel = self.tpl_lb.curselection()
        if not sel or sel[0] == 0:
            return
        t = templates[sel[0] - 1]
        self.name_var.set(t.get("name", ""))
        self.desc_box.delete("1.0", "end")
        self.desc_box.insert("1.0", t.get("desc", ""))
        self.any_var.set(", ".join(t.get("any") or []))
        self.all_var.set(", ".join(t.get("all") or []))
        self.none_var.set(", ".join(t.get("none") or []))
        self.from_var.set(str(t.get("from") or ""))
        self.to_var.set(str(t.get("to") or ""))
        self.add, self.skip = set(), set()
        self.refresh()

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

    def rule_field(self, parent, label, hint, var):
        """Filterzeile: kurze Bezeichnung (ANY OF / ALL OF / NONE OF) plus grauer Hinweis, darunter das Eingabefeld."""
        fr = tk.Frame(parent, bg=BG)
        fr.pack(fill="x")
        tk.Label(fr, text=label, bg=BG, fg=TEXT, font=F("mono", 11), anchor="w").pack(side="left")
        tk.Label(fr, text=hint, bg=BG, fg=TEXT_MUTED, font=F("mono", 10, False), anchor="w").pack(side="left", padx=(px(12), 0))
        e = tk.Entry(parent, textvariable=var, bg=BG2, fg=CYAN, insertbackground=CYAN, relief="flat",
                     font=F("mono", 13, False), highlightthickness=1, highlightcolor=CYAN,
                     highlightbackground=mix(CYAN, BG, 0.4), selectbackground=mix(CYAN, BG, 0.3))
        e.pack(fill="x", pady=(px(3), px(8)), ipady=px(5))
        return e

    def rule_text(self, cur):
        """Die Regeln als lesbarer Satz, damit man sieht, was die Eingabe bedeutet."""
        parts = []
        if cur["any"]:
            parts.append("(" + " OR ".join(cur["any"]) + ")" if len(cur["any"]) > 1 else cur["any"][0])
        parts += list(cur["all"])
        txt = " AND ".join(parts) if parts else ""
        if not txt:
            return ""
        if cur["none"]:
            txt += L(", but NOT ", ", aber NICHT ") + " / ".join(cur["none"])
        if cur["from"] or cur["to"]:
            txt += L(f"  //  years {cur['from'] or '…'} to {cur['to'] or '…'}", f"  //  Jahre {cur['from'] or '…'} bis {cur['to'] or '…'}")
        return L("Matches: ", "Passt auf: ") + txt.replace(" OR ", L(" OR ", " ODER ")).replace(" AND ", L(" AND ", " UND "))

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
            b.set_text(("● " if v_ == view else "") + {"hits": L("FILTER RESULTS", "FILTERERGEBNISSE"),
                                                     "all": L("ALL CHANNEL VIDEOS", "ALLE CHANNEL-VIDEOS")}[v_])
        self.lb.selection_clear(0, "end")
        self.refresh()

    def refresh(self):
        self.after_id = None
        cur = self.current()
        self.rule_sum.configure(text=self.rule_text(cur))
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
            notes.append(L("THE SEARCH COVERS ALL CHANNEL VIDEOS // GREY = NOT IN THE LIST YET, SELECT AND PRESS INCLUDE.",
                           "DIE SUCHE DURCHSUCHT ALLE CHANNEL-VIDEOS // GRAU = NOCH NICHT IN DER LISTE, MARKIEREN UND AUFNEHMEN."))
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
                                       "TO ADD OTHERS, FIND THEM WITH THE SEARCH OR IN ALL CHANNEL VIDEOS (GREY = NOT IN THE LIST).",
                                       "NICHTS ZUM AUFNEHMEN: DIE MARKIERTEN VIDEOS SIND SCHON IN DER LISTE. ANDERE FINDEST DU "
                                       "MIT DER SUCHE ODER IN ALLE CHANNEL-VIDEOS (GRAU = NICHT IN DER LISTE)."), fg=YELLOW)
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
        lst.update(name=name, desc=self.desc_box.get("1.0", "end").strip(), **cur)
        save_lists(lists)
        key = list_key(lst)
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
                                font=F("mono", 11, False), fill=TEXT_MUTED)
        for item in (code, hint):
            self.tag_bind(item, "<Button-1>", lambda e: self.copied(hint))
            self.tag_bind(item, "<Enter>", lambda e: (self.configure(cursor="hand2"),
                                                     self.itemconfigure(code, fill=mix("#ffffff", YELLOW, 0.35))))
            self.tag_bind(item, "<Leave>", lambda e: (self.configure(cursor=""),
                                                     self.itemconfigure(code, fill=YELLOW)))

    def copied(self, hint):
        self.on_copy()
        self.itemconfigure(hint, text=L("COPIED ✓", "KOPIERT ✓"), fill=GREEN)
        self.after(1500, lambda: self.itemconfigure(hint, text=L("CLICK TO COPY", "KLICKEN ZUM KOPIEREN"), fill=TEXT_MUTED))


class CutButton(tk.Canvas):
    """Eckiger Button mit einer abgeschnittenen Ecke unten rechts (wie die Panels)."""

    def __init__(self, master, text, command, width=0, height=36, size=12, pad=36):
        f = tkfont.Font(family=FONT_MONO, size=-px(size), weight="bold")
        w = max(px(width), f.measure("● " + text) + px(pad) if pad < 36 else f.measure(text) + px(pad))
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

    def __init__(self, master, text, on_all, on_none, on_hide=None, hidden=False):
        super().__init__(master, text)
        self.on_all, self.on_none, self.on_hide, self.hidden = on_all, on_none, on_hide, hidden
        self.total = (0, 0)

    def set_hidden(self, hidden):
        self.hidden = hidden
        self.redraw()

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
        right = self.link(right, y, L("ALL", "ALLE"), self.on_all) - px(6)
        if self.on_hide:
            sep = self.create_text(right, y, text="·", anchor="e", font=F("mono", 11), fill=FLUFF)
            right = self.bbox(sep)[0] - px(6)
            right = self.link(right, y, L("SHOW DONE", "FERTIGE ZEIGEN") if self.hidden else L("HIDE DONE", "FERTIGE AUSBLENDEN"),
                              self.on_hide) - px(16)
        else:
            right -= px(10)
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


class QuotaMeter(tk.Canvas):
    """Kopfzeile: heute verbrauchtes API-Kontingent (Schätzung von Chronolist) als Zahl und dünner Balken."""
    W = 220

    def __init__(self, master):
        super().__init__(master, width=px(self.W), height=px(36), bg=BG, highlightthickness=0, bd=0)
        self.last = None
        Tooltip(self, self.tip)
        self.bind("<Configure>", lambda e: self.redraw(force=True))

    def redraw(self, force=False):
        used, b = quota_used(), quota_blocked("units")
        key = (used, b.timestamp() if b else None, LANG)
        if key == self.last and not force:
            return
        self.last = key
        self.delete("all")
        w = self.winfo_width()
        if w <= 1:
            w = px(self.W)
        ratio = min(1.0, used / QUOTA_DAILY)
        col = RED if b else YELLOW if ratio >= 0.8 else CYAN
        y = px(11)
        self.create_text(0, y, text=L("API QUOTA //", "API-KONTINGENT //"), anchor="w", font=F("mono", 10, False), fill=TEXT_MUTED)
        right = (L("RESET ", "RESET ") + b.astimezone().strftime("%H:%M")) if b else f"{used} / {QUOTA_DAILY}"
        self.create_text(w, y, text=right, anchor="e", font=F("mono", 11), fill=col)
        by = px(25)
        self.create_rectangle(0, by, w, by + px(3), fill=mix(col, BG, 0.22), outline="")
        fx = w if b else w * ratio
        if fx > 0:
            self.create_rectangle(0, by, fx, by + px(3), fill=col, outline="")

    def tip(self):
        reset = quota_reset_local()
        if quota_blocked("units"):
            return L(f"The daily API quota is used up (or only Chronolist's reserve of {QUOTA_RESERVE} units is left). "
                     f"It resets {reset}.",
                     f"Das tägliche API-Kontingent ist aufgebraucht (oder nur noch die Reserve von {QUOTA_RESERVE} "
                     f"Einheiten übrig). Es wird {reset} zurückgesetzt.")
        return L(f"API units Chronolist used today: about {quota_used()} of {QUOTA_DAILY}. This is an estimate: other "
                 f"programs in the same Google project are not counted. Resets {reset}.",
                 f"Heute von Chronolist verbrauchte API-Einheiten: etwa {quota_used()} von {QUOTA_DAILY}. Das ist "
                 f"eine Schätzung: andere Programme im selben Google-Projekt zählen nicht mit. Reset {reset}.")


class Header(tk.Canvas):
    def __init__(self, master):
        super().__init__(master, height=px(88), bg=BG, highlightthickness=0, bd=0)
        self.chip = StatusChip(self)
        self.quota = QuotaMeter(self)
        self.api_btn = None      # wird von der App gesetzt
        self.help_btn = None
        self.upd_btn = None      # grüner UPDATE-Knopf, von der App gesetzt
        self.update = None       # (version, url) wenn eine neuere Release existiert
        self.title_end = 0
        self.bind("<Configure>", lambda e: self.redraw())

    def redraw(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        self.logo_img = logo_image(self, px(60))        # Referenz halten, sonst verschwindet das Bild
        lsz = self.logo_img.width()
        self.create_image(px(18) - px(3), px(18) - px(3), image=self.logo_img, anchor="nw")
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
        # Kontingent-Anzeige (statt der früheren Deko), nur wenn neben dem Titel Platz ist
        qw = px(QuotaMeter.W)
        if bx - qw - px(10) > self.title_end:
            self.create_window(bx, px(46), window=self.quota, anchor="e", width=qw, height=px(36))
            self.quota.redraw(force=True)

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
    def __init__(self, master, autohide=False):
        super().__init__(master, bg=BG)
        self.autohide = autohide
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
        self.inner.bind("<Configure>", lambda e: (self.canvas.configure(scrollregion=self.canvas.bbox("all")),
                                                  self._autohide()))
        self.canvas.bind("<Configure>", lambda e: (self.canvas.itemconfigure(self.win, width=e.width),
                                                   self._autohide()))
        if autohide:
            # Platz für die Scrollbar bleibt immer frei, damit sich die Breite des Inhalts nie ändert
            self.columnconfigure(1, minsize=px(12))
        self._is_scrollarea = True
        root = self.winfo_toplevel()._root()
        if not getattr(root, "_wheel_dispatch", False):
            # Ein zentraler Mausrad-Handler: scrollt die ScrollArea, über der sich die Maus gerade befindet –
            # egal über welchem Kind-Element (Label, Knopf, Karte).
            root._wheel_dispatch = True
            root.bind_all("<MouseWheel>", lambda e: ScrollArea._dispatch(e, int(-e.delta / 120)), add="+")
            root.bind_all("<Button-4>", lambda e: ScrollArea._dispatch(e, -1), add="+")
            root.bind_all("<Button-5>", lambda e: ScrollArea._dispatch(e, 1), add="+")

    def _autohide(self):
        if not self.autohide:
            return
        if self.inner.winfo_reqheight() > self.canvas.winfo_height() + 1:
            self.sb.grid()
        else:
            self.sb.grid_remove()
            self.canvas.yview_moveto(0)

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
    ui = load(SETTINGS_FILE, {}).get("ui_scale", UI_SCALE_DEFAULT)
    ui = ui if ui in UI_SCALES else UI_SCALE_DEFAULT
    S = max(1.0, root.winfo_fpixels("1i") / 96) * ui / 100 * UI_SCALE_BASE
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
        self.geometry(fit(px(900), px(760)))
        self.minsize(*fit_min(px(900), px(640)))
        style_titlebar(self)
        try:
            self._icons = [logo_image(self, 64), logo_image(self, 32), logo_image(self, 16)]
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
                                        lambda: self.select_all(False), self.toggle_hide_done,
                                        bool(self.settings.get("hide_done")))
        self.year_section.grid(row=2, column=0, sticky="ew", pady=(0, px(6)))
        self.scroll = ScrollArea(left)
        self.scroll.grid(row=3, column=0, sticky="nsew")
        # Kanalkarte und Überschrift enden rechts bündig mit den Jahreskarten (nicht mit der Scrollleiste)
        sbw = self.scroll.sb.winfo_reqwidth() + px(6)
        self.channel_card.grid_configure(padx=(0, sbw))
        self.year_section.grid_configure(padx=(0, sbw))
        btn_row = tk.Frame(left, bg=BG)
        btn_row.grid(row=4, column=0, sticky="ew", pady=(px(8), 0), padx=(0, sbw))
        btn_row.columnconfigure(0, weight=3, uniform="lb")
        btn_row.columnconfigure(1, weight=2, uniform="lb")
        self.new_list_btn = CutButton(btn_row, L("+  CUSTOM LIST", "+  EIGENE LISTE"), self.new_list, height=px(36))
        self.new_list_btn.set_color(CYAN)
        self.new_list_btn.grid(row=0, column=0, sticky="ew", padx=(0, px(6)))
        self.suggest_btn = CutButton(btn_row, L("SUGGESTIONS", "VORSCHLÄGE"), self.suggest_lists, height=px(36))
        self.suggest_btn.set_color(CYAN)
        self.suggest_btn.grid(row=0, column=1, sticky="ew")
        Tooltip(self.suggest_btn, L("Finds series in your video titles (e.g. \"Minecraft Season 04 #12\") and offers to create a list for each.",
                                    "Findet Serien in deinen Videotiteln (z. B. \"Minecraft Season 04 #12\") und bietet an, für jede eine Liste anzulegen."))
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
        for i in range(5):
            btns.columnconfigure(i, weight=1, uniform="ctl")
        self.start_btn = CutButton(btns, "▶  START", self.start, height=DECK_H, size=15)
        self.pause_btn = CutButton(btns, L("❚❚  PAUSE", "❚❚  PAUSE"), self.toggle_pause, height=DECK_H, size=15)
        self.pause_btn.set_enabled(False)
        self.stop_btn = CutButton(btns, L("■  STOP", "■  STOPP"), self.stop, height=DECK_H, size=15)
        self.check_btn = CutButton(btns, L("✓  CHECK", "✓  PRÜFEN"), self.run_check, height=DECK_H, size=15)
        self.scan_btn = CutButton(btns, L("⌕  SCAN", "⌕  SCAN"), self.run_scan, height=DECK_H, size=15)
        for i, b in enumerate((self.start_btn, self.pause_btn, self.stop_btn, self.check_btn, self.scan_btn)):
            b.configure(width=1)
            b.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else px(3), 0 if i == 4 else px(3)))
        for b in (self.start_btn, self.pause_btn, self.stop_btn, self.check_btn, self.scan_btn):
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
        self.fallback_card.grid(row=0, column=1, sticky="ew", padx=(px(3), 0), pady=(0, px(4)))
        self.skip_ahead = True                       # immer: nächstes bereites markiertes Jahr vorziehen
        self.autorun = bool(self.settings.get("autorun", False))
        self.win_on = get_windows_autostart()
        self.win_min = bool(self.settings.get("start_minimized", False))
        self.auto_check = bool(self.settings.get("auto_check", False))
        self.auto_interval = self.settings.get("auto_interval", AUTO_INTERVAL_DEFAULT)
        if self.auto_interval not in AUTO_INTERVALS:
            self.auto_interval = AUTO_INTERVAL_DEFAULT
        self.auto_mode = "times" if self.settings.get("auto_mode") == "times" else "interval"
        self.auto_times = sorted({t for t in (parse_time(x) for x in self.settings.get("auto_times", [])) if t})
        if not self.auto_times:
            self.auto_mode = "interval"
        self.auto_next = self.plan_auto(first=True)
        global AUTO_SHOW
        AUTO_SHOW = self.auto_check
        AUTO_YEARS.clear()
        AUTO_YEARS.update(y for y in self.settings.get("auto_years", []) if isinstance(y, int))
        load_auto_last()
        self.alerts = bool(self.settings.get("alerts", True))
        self.auto_card = ToggleCard(opts, "AUTO-CHECK", self.auto_sub(), self.auto_check,
                                    self.toggle_auto_check, compact=True)
        self.auto_card.grid(row=0, column=0, sticky="ew", padx=(0, px(3)), pady=(0, px(4)))
        Tooltip(self.auto_card, L("Looks for new videos by itself while Chronolist is open. When on, every card "
                                  "shows an AUTO switch: only cards with AUTO on are kept up to date. The interval "
                                  "is set in the PROGRAM settings.",
                                  "Sucht von allein nach neuen Videos, solange Chronolist offen ist. Ist es an, "
                                  "zeigt jede Karte einen AUTO-Schalter: nur Karten mit AUTO werden aktuell "
                                  "gehalten. Das Intervall stellst du in den PROGRAMM-Einstellungen ein."))
        self.app_card = SettingsCard(opts, self.edit_app_settings, lambda: L("PROGRAM", "PROGRAMM"),
                                     lambda: " // ".join([LANGUAGES[LANG]]
                                                         + ([L("AUTO-RUN", "AUTOSTART")] if self.autorun else [])
                                                         + ([L("ON BOOT", "MIT WINDOWS")] if self.win_on else [])), icon="gear")
        self.app_card.grid(row=1, column=1, sticky="ew", padx=(px(3), 0))
        self.settings_card = SettingsCard(opts, self.edit_settings, lambda: "PLAYLISTS",
                                          lambda: f"{privacy_label(PRIVACY)} // "
                                                  + fill_template(PLAYLIST_TEMPLATE, YEARS[0] if YEARS
                                                                  else datetime.date.today().year - 1))
        self.settings_card.grid(row=1, column=0, sticky="ew", padx=(0, px(3)))

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
        self.logbox.tag_configure("urllink", underline=False)
        self.logbox.tag_bind("urllink", "<Enter>", lambda e: (self.logbox.configure(cursor="hand2"),
                                                               self.logbox.tag_configure("urllink", underline=True)))
        self.logbox.tag_bind("urllink", "<Leave>", lambda e: (self.logbox.configure(cursor=""),
                                                               self.logbox.tag_configure("urllink", underline=False)))
        self.logbox.tag_bind("urllink", "<Button-1>", self.copy_log_link)
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
        if "--minimized" in sys.argv:
            self.after(400, self.iconify)
        self.after(30000, self.auto_tick)
        self.after(2000, self.quota_tick)

    # --- Einstellungen
    def selected_years(self):
        """Markierte Jahre in der Reihenfolge der Liste (per Drag & Drop änderbar)."""
        return [y for y in self.order if self.rows[y].selected]

    def save_settings(self):
        self.settings.update(years=self.selected_years(), year_order=list(self.order), autorun=self.autorun,
                             auto_years=sorted(AUTO_YEARS),
                             browser_fallback=getattr(self, "fallback", False),
                             )
        save(SETTINGS_FILE, self.settings)
        self.save_view()

    def save_view(self):
        """Ansicht des aktuellen Kanals in data/<kanal>/view.json merken (für das Zurückwechseln)."""
        if not CHANNEL.get("first_year"):
            return
        save(view_file(), {"channel": dict(CHANNEL),
                           "years": list(self.settings.get("years", [])),
                           "year_order": list(self.settings.get("year_order", [])),
                           "hide_done": bool(self.settings.get("hide_done")),
                           "auto_years": sorted(AUTO_YEARS)})

    def new_list(self):
        ListDialog(self, None)

    def suggest_lists(self):
        SuggestDialog(self)

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
        self.save_view()
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

    def is_hidden_row(self, y):
        r = self.rows[y]
        # nur fertige UND abgewählte Karten; angehakte (z. B. laufendes Jahr, Listen) bleiben sichtbar, weil sie weiter abgeglichen werden
        return bool(self.settings.get("hide_done")) and r.info.get("code") == "done" and not r.running and not r.selected

    def repack(self):
        """Karten in der gespeicherten Reihenfolge anordnen; fertige ausblenden, wenn gewünscht."""
        for yy in self.order:
            self.rows[yy].pack_forget()
        for yy in self.order:
            if not self.is_hidden_row(yy):
                self.rows[yy].pack(fill="x", pady=(0, px(4)))
        self.hidden_now = {yy for yy in self.order if self.is_hidden_row(yy)}

    def watch_hidden(self):
        """Ändert sich, welche Karten fertig sind (oder laufen), die Ansicht nachziehen."""
        try:
            if not getattr(self, "drag", None) or not self.drag.get("on"):
                if {yy for yy in self.order if self.is_hidden_row(yy)} != getattr(self, "hidden_now", set()):
                    self.repack()
        finally:
            self.after(700, self.watch_hidden)

    def toggle_hide_done(self):
        self.settings["hide_done"] = not self.settings.get("hide_done")
        save(SETTINGS_FILE, self.settings)
        self.save_view()
        self.year_section.set_hidden(bool(self.settings["hide_done"]))
        self.repack()
        self.scroll.canvas.yview_moveto(0)

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

    def set_windows(self, on, minimized=False):
        if bool(on) == self.win_on and (not on or bool(minimized) == self.win_min):
            self.win_min = bool(minimized)
            return
        try:
            set_windows_autostart(bool(on), bool(minimized))
        except OSError as e:
            self.write_log(L(f"BOOT SEQUENCE UPDATE FAILED // {e}", f"START MIT WINDOWS KONNTE NICHT GEÄNDERT WERDEN // {e}"), "err")
            return
        self.win_on = bool(on)
        self.win_min = bool(minimized)

    # --- Automatische Prüfung
    def auto_sub(self):
        if not self.auto_check:
            return L("NEW VIDEOS IN THE BACKGROUND", "NEUE VIDEOS IM HINTERGRUND")
        if self.auto_mode == "times":
            return L("AT ", "UM ") + " · ".join(self.auto_times)
        return L("EVERY ", "ALLE ") + interval_label(self.auto_interval)

    def plan_auto(self, first=False):
        """Zeitpunkt der nächsten automatischen Prüfung: feste Uhrzeiten oder Intervall
        (beim Einschalten bzw. Programmstart im Intervall-Modus schon nach einer Minute)."""
        if self.auto_mode == "times" and self.auto_times:
            return next_time_at(self.auto_times)
        return time.time() + (60 if first else self.auto_interval * 60)

    def auto_next_text(self):
        if self.auto_mode == "times" and self.auto_times:
            return L("NEXT AT ", "NÄCHSTE UM ") + datetime.datetime.fromtimestamp(self.auto_next).strftime("%H:%M")
        return L("NEXT IN ", "NÄCHSTE IN ") + interval_label(self.auto_interval)

    def toggle_auto_check(self):
        global AUTO_SHOW
        self.auto_check = not self.auto_check
        AUTO_SHOW = self.auto_check
        self.settings["auto_check"] = self.auto_check
        self.auto_next = self.plan_auto(first=True)
        self.auto_card.sub = self.auto_sub()
        self.auto_card.update_value(self.auto_check)
        for r in self.rows.values():
            r.redraw()
        self.save_settings()

    def toggle_row_auto(self, key):
        set_auto(key, not auto_on(key))
        if key in self.rows:
            self.rows[key].redraw()
        self.save_settings()

    def auto_result(self, result):
        """Ergebnis einer Auto-Prüfung: pro Karte merken (LAST CHECK) und bei neuen Videos Bescheid geben."""
        now = time.time()
        for k, n in result.items():
            AUTO_LAST[str(k)] = {"t": now, "new": int(n)}
        save(autocheck_file(), AUTO_LAST)
        for k in result:
            if k in self.rows:
                self.rows[k].redraw()
        total = sum(result.values())
        if total and self.alerts:
            names = [year_label(k) for k, n in result.items() if n]
            text = L(f"{total} new video{'s' if total != 1 else ''} added: {', '.join(names)}",
                     f"{total} neue{'s' if total == 1 else ''} Video{'s' if total != 1 else ''} ergänzt: {', '.join(names)}")
            flash_taskbar(self)
            try:
                hidden = self.state() == "iconic" or self.focus_displayof() is None
            except Exception:  # noqa: BLE001
                hidden = True
            if hidden:
                windows_toast("Chronolist", text)

    def quota_tick(self):
        self.after(2000, self.quota_tick)
        try:
            if self.header.quota.winfo_ismapped():
                self.header.quota.redraw()
        except tk.TclError:
            pass

    def auto_tick(self):
        self.after(30000, self.auto_tick)
        if not self.auto_check or time.time() < self.auto_next:
            return
        if self.worker and self.worker.is_alive():
            return
        if not all(get_creds()) or not CHANNEL or not load(TOKEN_FILE, None):
            return
        self.run_auto()

    def run_auto(self):
        keys = [y for y in YEARS if y in AUTO_YEARS and growing(y)] + \
               [list_key(x) for x in load_lists() if x.get("auto")]
        keys = [k for k in keys if k in self.rows]
        self.auto_next = self.plan_auto()
        if not keys:
            return
        self.write_log(L("AUTO-CHECK", "AUTO-PRÜFUNG") + f" // {datetime.datetime.now():%H:%M} // " + self.auto_next_text(), "sep")
        self.worker = Worker(keys, 0, self.out, fallback=False, auto=True)
        self.run_mode = "running"
        self.pause_info = None
        self.worker.start()
        self.start_btn.set_enabled(False)
        self.check_btn.set_enabled(False)
        self.scan_btn.set_enabled(False)
        self.stop_btn.set_enabled(True)
        self.set_status("running")

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
            row.auto_command = self.toggle_row_auto
            row.refresh(selected=y in selected)
            self.bind_drag(row)
            if is_list_key(y):
                row.bind("<Button-3>", lambda e, k=y: self.edit_list(k), add="+")
            self.rows[y] = row
        self.repack()
        if not getattr(self, "watch_started", False):
            self.watch_started = True
            self.after(700, self.watch_hidden)
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
        vis = [y for y in self.order if not self.is_hidden_row(y)]
        for y in vis:
            r = self.rows[y]
            if rel < r.winfo_y() + r.winfo_height() / 2:
                return self.order.index(y), max(0, r.winfo_y() - px(3))
        last = self.rows[vis[-1]]
        return len(self.order), last.winfo_y() + last.winfo_height() + px(1)

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
            self.repack()
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
        Tooltip(self.scan_btn, L("Loads all videos and titles of the channel without creating or changing any playlist. "
                                 "Needed for SUGGESTIONS and custom lists, for example with a new channel.",
                                 "Lädt alle Videos und Titel des Kanals, ohne eine Playlist anzulegen oder zu ändern. "
                                 "Nötig für SUGGESTIONS und eigene Listen, zum Beispiel bei einem neuen Kanal."))
        Tooltip(self.fallback_card, L("When the daily YouTube quota is used up, continue in a separate Edge window. "
                                      "Warning: this breaks YouTube's rules and puts your account at risk.",
                                      "Wenn das tägliche YouTube-Kontingent leer ist, über ein eigenes Edge-Fenster "
                                      "weitermachen. Achtung: verstößt gegen die YouTube-Regeln, Risiko für dein "
                                      "Konto."))
        Tooltip(self.app_card, L("Program settings: language, auto-run, start with Windows and update check.",
                                 "Programm-Einstellungen: Sprache, Autostart, Start mit Windows und Update-Prüfung."))
        Tooltip(self.settings_card, L("Playlist title and description with placeholders and visibility.",
                                      "Playlist-Titel und Beschreibung mit Platzhaltern und Sichtbarkeit."))
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
        ChannelDialog(self, CHANNEL.get("handle") or CHANNEL.get("id", ""), self.save_channel)

    def save_channel(self, v):
        m_id, m_title, m_year = v.get("m_id", ""), v.get("m_title", ""), v.get("m_year", "")
        target = parse_channel(v["channel"]) or (parse_channel(m_id) if m_id else None)
        if not target:
            return False
        same_handle = target.get("handle") and target["handle"].lower() == (CHANNEL.get("handle") or "").lower()
        same_id = target.get("id") and target["id"] == CHANNEL.get("id")
        if CHANNEL.get("first_year") and (same_handle or same_id) and not (m_id or m_year):
            return None
        # 1) manuell: Kanal-ID und Startjahr angegeben -> ohne API
        if m_id or m_year:
            cid = re.search(r"UC[0-9A-Za-z_-]{22}", m_id or "")
            this_year = datetime.date.today().year
            year = int(m_year) if m_year.isdigit() else 0
            if not cid or not 2005 <= year <= this_year:
                self.write_log(L("MANUAL CHANNEL // CHANNEL ID (UC...) AND FIRST YEAR (2005 OR LATER) NEEDED",
                                 "KANAL MANUELL // KANAL-ID (UC...) UND STARTJAHR (AB 2005) NÖTIG"), "err")
                return False
            handle = target.get("handle") or ""
            info = {"id": cid.group(0), "handle": handle, "title": m_title or handle.lstrip("@") or cid.group(0),
                    "first_year": year}
            self.write_log(L(f"CHANNEL ENTERED MANUALLY // {info['title']} // SINCE {year}",
                             f"KANAL MANUELL EINGETRAGEN // {info['title']} // SEIT {year}"), "ok")
            self.apply_channel(info)
            return None
        # 2) schon einmal benutzt -> gespeicherte Info, ohne API
        info = known_channel(target)
        if info:
            self.write_log(L(f"CHANNEL RESTORED // {info.get('title') or info.get('handle')} // NO API NEEDED",
                             f"KANAL WIEDERHERGESTELLT // {info.get('title') or info.get('handle')} // OHNE API"), "ok")
            self.apply_channel(info)
            return None
        # 3) neu -> über die API auflösen
        if not all(get_creds()):
            self.write_log(L("SET API CREDENTIALS FIRST", "ZUERST DIE API-ZUGANGSDATEN EINTRAGEN"), "warn")
            self.edit_credentials()
            return False
        self.channel_card.update_state(resolving=True)
        self.worker = Worker([], 0, self.out, resolve=target)
        self.worker.start()
        self.start_btn.set_enabled(False)
        self.set_status("running")

    def apply_channel(self, info):
        if CHANNEL.get("first_year"):
            self.save_view()                     # Ansicht des bisherigen Kanals merken
        self.settings["channel"] = info
        set_channel(info)
        # Ansicht des neuen Kanals laden; beim ersten Mal: alle Jahre markiert, chronologisch
        view = load(view_file(), {}) or {}
        self.settings["years"] = view.get("years", list(YEARS))
        self.settings["year_order"] = view.get("year_order", list(YEARS))
        self.settings["hide_done"] = bool(view.get("hide_done", False))
        AUTO_YEARS.clear()
        AUTO_YEARS.update(y for y in view.get("auto_years", []) if isinstance(y, int))
        self.settings["auto_years"] = sorted(AUTO_YEARS)
        load_auto_last()
        save(SETTINGS_FILE, self.settings)
        self.build_rows()
        self.save_view()
        self.year_section.set_hidden(bool(self.settings["hide_done"]))
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
        self.scan_btn.set_enabled(False)
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
        self.scan_btn.set_enabled(False)
        self.stop_btn.set_enabled(True)
        self.set_status("checking")

    def run_scan(self):
        """Nur scannen: Archiv und Videotitel laden, ohne Playlists anzulegen oder zu ändern."""
        if self.busy():
            return
        if not all(get_creds()):
            self.edit_credentials()
            return
        if not CHANNEL:
            self.edit_channel()
            return
        self.write_log(L("SCAN ONLY // ARCHIVE AND TITLES, NO PLAYLISTS", "NUR SCANNEN // ARCHIV UND TITEL, KEINE PLAYLISTS"), "sep")
        self.worker = Worker([], 0, self.out, fallback=self.fallback, scan_only=True)
        self.run_mode = "running"
        self.pause_info = None
        self.worker.start()
        self.start_btn.set_enabled(False)
        self.check_btn.set_enabled(False)
        self.scan_btn.set_enabled(False)
        self.stop_btn.set_enabled(True)
        self.set_status("running")

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
        """Nach einem Update einmalig die Neuerungen in einem eigenen Fenster zeigen."""
        pu = self.settings.pop("post_update", None)
        if not pu:
            return
        save(SETTINGS_FILE, self.settings)
        version = pu.get("version", VERSION)
        self.write_log(L(f"UPDATED TO v{version}", f"AKTUALISIERT AUF v{version}"), "ok")
        self.after(400, lambda: self.winfo_exists() and WhatsNewDialog(self, version, pu.get("notes") or ""))

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
        # Changelog einmalig nach dem Neustart in einem Fenster zeigen
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

    def copy_log_link(self, e):
        """Klick auf einen Link im Log: in die Zwischenablage kopieren, kurzer Hinweis am Mauszeiger."""
        rng = self.logbox.tag_prevrange("urllink", f"@{e.x},{e.y}+1c")
        if not rng:
            return "break"
        url = self.logbox.get(*rng).strip()
        self.clipboard_clear()
        self.clipboard_append(url)
        tip = tk.Toplevel(self)
        tip.wm_overrideredirect(True)
        try:
            tip.attributes("-topmost", True)
        except tk.TclError:
            pass
        base = "Segoe UI" if "Segoe UI" in tkfont.families(self) else "TkDefaultFont"
        fr = tk.Frame(tip, bg=mix(GREEN, BG, 0.55), padx=1, pady=1)
        fr.pack()
        tk.Label(fr, text=L("LINK COPIED", "LINK KOPIERT"), bg="#1c1c28", fg=GREEN, font=(base, -px(13)),
                 padx=px(10), pady=px(6)).pack()
        tip.geometry(f"+{e.x_root + px(12)}+{e.y_root + px(18)}")
        tip.after(1300, tip.destroy)
        return "break"

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
            for part in re.split(r"(https?://\S+)", text):       # Links im Log: Klick kopiert sie
                if re.match(r"https?://", part):
                    self.logbox.insert("end", part, (level, "urllink"))
                elif part:
                    self.logbox.insert("end", part, level)
            self.logbox.insert("end", "\n", level)
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
                        self.save_view()
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
                elif kind == "auto_result":
                    self.auto_result(msg[1])
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
                    # Intervall: zählt ab dem Ende jedes Laufs. Feste Uhrzeiten: nur nach einer Auto-Prüfung neu planen
                    # (eine fällige Uhrzeit, die in einen manuellen Lauf fiel, wird danach nachgeholt)
                    if self.auto_mode != "times" or getattr(self.worker, "auto", False):
                        self.auto_next = self.plan_auto()
                    self.start_btn.set_enabled(True)
                    self.check_btn.set_enabled(True)
                    self.scan_btn.set_enabled(True)
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
