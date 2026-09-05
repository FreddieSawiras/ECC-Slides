"""
ECC Worship — Premium Church Presentation Platform
====================================================

Run with:
    pip install streamlit
    streamlit run app.py

HOW THE "PROJECTOR / EXTENDED DISPLAY" WORKS
---------------------------------------------
Streamlit apps live in a single browser tab, so true OS-level "extended
display" control isn't available to a pure web app. This app simulates it
the way real worship software architectures do it under the hood: all
presentation state (current service, current item, current slide, black
screen, theme) lives in a small SQLite database. The operator's browser tab
edits that state. A second, completely separate browser tab/window — opened
on the projector/monitor and pointed at the same app URL with
`?display=projector` appended — polls that state and renders ONLY the slide,
full-bleed, with no chrome. Click "Open Presentation Display" in the app to
get the exact link to open on the second screen, then drag that browser
window onto your extended monitor and press F for fullscreen.

Everything else (songs, Bible, services, custom slides) is stored in the
same local SQLite file so nothing is lost between sessions.
"""

import streamlit as st
import streamlit.components.v1 as components
import sqlite3
import json
import os
import time
import datetime
import csv
import io
import re
import base64
import requests
import shutil

try:
    from PIL import Image, ImageFilter, ImageEnhance
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

try:
    import fitz  # PyMuPDF — used to rasterize Google Slides PDF exports into per-slide images
    PYMUPDF_AVAILABLE = True
except ImportError:
    PYMUPDF_AVAILABLE = False

# ---------------------------------------------------------------------------
# CONFIG / CONSTANTS
# ---------------------------------------------------------------------------

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ecc_worship.db")

# ---------------------------------------------------------------------------
# TURSO (optional cloud backup) — SQLite stays the only thing anything reads
# from during normal use; Turso is write-only and only touched on an
# explicit click (Save Song / Sync to Turso), never on page load or in any
# polling loop, so it can never slow down browsing/presenting.
#
# Set these in .streamlit/secrets.toml (or your host's "Secrets" settings):
#   TURSO_DATABASE_URL = "libsql://your-db-org.turso.io"
#   TURSO_AUTH_TOKEN   = "..."
# Get both with the Turso CLI: `turso db show <name> --url` and
# `turso db tokens create <name>`.
# ---------------------------------------------------------------------------

def _turso_config():
    try:
        url = st.secrets.get("TURSO_DATABASE_URL") or os.environ.get("TURSO_DATABASE_URL")
        token = st.secrets.get("TURSO_AUTH_TOKEN") or os.environ.get("TURSO_AUTH_TOKEN")
    except Exception:
        url = os.environ.get("TURSO_DATABASE_URL")
        token = os.environ.get("TURSO_AUTH_TOKEN")
    return url, token


def turso_configured():
    url, token = _turso_config()
    return bool(url and token)


def _turso_arg(value):
    if value is None:
        return {"type": "null", "value": None}
    if isinstance(value, bool):
        return {"type": "integer", "value": str(int(value))}
    if isinstance(value, int):
        return {"type": "integer", "value": str(value)}
    if isinstance(value, float):
        return {"type": "float", "value": value}
    return {"type": "text", "value": str(value)}


def turso_pipeline(statements, timeout=10):
    """
    statements: list of (sql, args) tuples. Sends them all as ONE HTTP
    request to Turso's /v2/pipeline endpoint (a single round trip no matter
    how many statements), then closes the connection. Returns the parsed
    JSON response, or raises if not configured / the request fails —
    callers should wrap this in try/except so a Turso hiccup never breaks
    the local save that already succeeded.
    """
    url, token = _turso_config()
    if not url or not token:
        raise RuntimeError("Turso isn't configured — add TURSO_DATABASE_URL and TURSO_AUTH_TOKEN to secrets.")
    http_url = url.replace("libsql://", "https://").replace("turso://", "https://").rstrip("/") + "/v2/pipeline"
    requests_payload = []
    for sql, args in statements:
        stmt = {"sql": sql}
        if args:
            stmt["args"] = [_turso_arg(a) for a in args]
        requests_payload.append({"type": "execute", "stmt": stmt})
    requests_payload.append({"type": "close"})
    resp = requests.post(http_url, json={"requests": requests_payload},
                          headers={"Authorization": f"Bearer {token}"}, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


TURSO_SONGS_SCHEMA = """CREATE TABLE IF NOT EXISTS songs(
    id INTEGER PRIMARY KEY, title TEXT, artist TEXT, category TEXT,
    tags TEXT, slides TEXT, updated_at TEXT
)"""

TURSO_SERVICES_SCHEMA = """CREATE TABLE IF NOT EXISTS services(
    id INTEGER PRIMARY KEY, name TEXT, service_date TEXT, service_time TEXT,
    items TEXT, updated_at TEXT
)"""

TURSO_SETTINGS_SCHEMA = """CREATE TABLE IF NOT EXISTS settings(
    id INTEGER PRIMARY KEY CHECK (id=1), church_name TEXT, default_theme TEXT,
    default_background TEXT, custom_background_data TEXT, updated_at TEXT
)"""

TURSO_SLIDE_DECKS_SCHEMA = """CREATE TABLE IF NOT EXISTS slide_decks(
    id INTEGER PRIMARY KEY, title TEXT, source TEXT, slides TEXT, updated_at TEXT
)"""

TURSO_BIBLE_VERSES_SCHEMA = """CREATE TABLE IF NOT EXISTS bible_verses(
    id INTEGER PRIMARY KEY, book TEXT, chapter INTEGER, verse INTEGER,
    text TEXT, translation TEXT, book_number INTEGER,
    UNIQUE(book, chapter, verse, translation)
)"""


def turso_push_song(song_id, title, artist, category, tags, slides_json):
    """One explicit push for one song — this is the only network call that
    happens when you click Save on a song. Uses the local row's own id, so
    editing/re-saving the same song later overwrites its Turso copy rather
    than duplicating it."""
    turso_pipeline([
        (TURSO_SONGS_SCHEMA, None),
        ("INSERT OR REPLACE INTO songs(id, title, artist, category, tags, slides, updated_at) "
         "VALUES (?,?,?,?,?,?,?)", (song_id, title, artist, category, tags, slides_json, now())),
    ])
    _mark_synced("songs", song_id)


def turso_push_all_songs():
    """Bulk push (Church Settings → 'Sync all songs to Turso', and the
    master Sync All button) — batches every CHANGED song into ONE HTTP
    request via the pipeline's multi-statement support. Only sends songs
    whose updated_at is newer than their synced_at (or that have never
    been synced) — a song that hasn't changed since its last successful
    push is skipped, instead of being unconditionally re-sent every time
    this runs. Returns the number actually pushed (not the total count in
    the library)."""
    rows = _rows_needing_sync("songs", ["id", "title", "artist", "category", "tags", "slides"])
    if not rows:
        return 0
    statements = [(TURSO_SONGS_SCHEMA, None)]
    push_ts = now()
    for r in rows:
        statements.append((
            "INSERT OR REPLACE INTO songs(id, title, artist, category, tags, slides, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (r["id"], r["title"], r["artist"], r["category"], r["tags"], r["slides"], push_ts)
        ))
    turso_pipeline(statements, timeout=30)
    conn = get_conn()
    conn.executemany("UPDATE songs SET synced_at=? WHERE id=?", [(push_ts, r["id"]) for r in rows])
    conn.commit()
    conn.close()
    return len(rows)


def turso_push_service(service_id, name, service_date, service_time, items_json):
    """One explicit push for one saved service — fires only when you click
    'Save Service to Cloud', never on every add/reorder/delete while
    you're still building it. Overwrites that service's own Turso row by
    id, so re-saving updates it rather than duplicating it."""
    turso_pipeline([
        (TURSO_SERVICES_SCHEMA, None),
        ("INSERT OR REPLACE INTO services(id, name, service_date, service_time, items, updated_at) "
         "VALUES (?,?,?,?,?,?)", (service_id, name, service_date, service_time, items_json, now())),
    ])
    _mark_synced("services", service_id)


def turso_pull_all_songs():
    """Read-only pull, used ONLY once at cold-start and only when the local
    songs table is completely empty (see init_db) — restores your library
    after a host wipes the local filesystem on redeploy/sleep. Never called
    during normal use, so it can't slow anything down."""
    result = turso_pipeline([(TURSO_SONGS_SCHEMA, None), ("SELECT id, title, artist, category, tags, slides FROM songs", None)])
    rows = result["results"][1]["response"]["result"]["rows"]
    songs = []
    for row in rows:
        vals = [cell.get("value") for cell in row]
        songs.append(dict(zip(["id", "title", "artist", "category", "tags", "slides"], vals)))
    return songs


def turso_pull_all_services():
    """Same idea as turso_pull_all_songs, for saved services — a single
    read-only call, only at cold-start, only when the local services table
    is empty."""
    result = turso_pipeline([(TURSO_SERVICES_SCHEMA, None),
                             ("SELECT id, name, service_date, service_time, items FROM services", None)])
    rows = result["results"][1]["response"]["result"]["rows"]
    services = []
    for row in rows:
        vals = [cell.get("value") for cell in row]
        services.append(dict(zip(["id", "name", "service_date", "service_time", "items"], vals)))
    return services


def turso_pull_all_bible_verses():
    """Same idea as turso_pull_all_songs/services, for Bible verses — but
    paginated with LIMIT/OFFSET, since a single full translation is
    typically ~31,000 rows and pulling that as one HTTP response risked the
    exact same size/timeout problem already fixed on the PUSH side (see
    turso_push_bible_verses). Only runs once at cold-start, only when the
    local bible_verses table is completely empty — this is what was
    MISSING before: songs and services already restored themselves from
    Turso after a Streamlit restart wiped the local database, but Bible
    verses had no equivalent path at all, so an imported-and-synced
    translation would vanish on every restart even though it was safely
    sitting in Turso the whole time."""
    PAGE = 2000
    offset = 0
    all_verses = []
    while True:
        result = turso_pipeline([
            (TURSO_BIBLE_VERSES_SCHEMA, None),
            (f"SELECT book, chapter, verse, text, translation, book_number FROM bible_verses "
             f"ORDER BY id LIMIT {PAGE} OFFSET {offset}", None)
        ], timeout=30)
        rows = result["results"][1]["response"]["result"]["rows"]
        if not rows:
            break
        for row in rows:
            vals = [cell.get("value") for cell in row]
            all_verses.append(dict(zip(["book", "chapter", "verse", "text", "translation", "book_number"], vals)))
        if len(rows) < PAGE:
            break
        offset += PAGE
    return all_verses


def turso_push_background(default_theme, default_background, custom_background_data, church_name):
    """One explicit push of the display settings/background — fires only
    from 'Process & Use as Background' or the master sync-all button, never
    automatically on every upload."""
    turso_pipeline([
        (TURSO_SETTINGS_SCHEMA, None),
        ("INSERT OR REPLACE INTO settings(id, church_name, default_theme, default_background, "
         "custom_background_data, updated_at) VALUES (1,?,?,?,?,?)",
         (church_name, default_theme, default_background, custom_background_data, now())),
    ])


def turso_push_slide_deck(deck_id, title, source, slides_json):
    """One explicit push for one imported slide deck — same pattern as
    turso_push_song: fires on that deck's own Save/Sync click, and
    INSERT OR REPLACE means re-syncing updates rather than duplicates it."""
    turso_pipeline([
        (TURSO_SLIDE_DECKS_SCHEMA, None),
        ("INSERT OR REPLACE INTO slide_decks(id, title, source, slides, updated_at) "
         "VALUES (?,?,?,?,?)", (deck_id, title, source, slides_json, now())),
    ])
    _mark_synced("slide_decks", deck_id)


def turso_push_bible_verses(progress_cb=None):
    """Pushes locally-stored Bible verses to Turso, but only for
    translations that aren't already marked synced (see
    synced_translations, checked in init_db's migration block) — a
    translation's verses don't change after import, only whole
    translations get added or removed, so "already synced" is tracked per
    translation rather than per verse. Batched into chunks — a full
    translation can be tens of thousands of verses (a complete Bible is
    roughly 31,000), too many to safely send as one request, and each
    batch is its own sequential HTTP round trip — this is genuinely the
    slowest part of a sync, which is why it's the one piece that reports
    progress: if progress_cb is given, it's called after every batch as
    progress_cb(done, total) so the caller can show real, moving feedback
    instead of a single spinner that gives no sign anything is happening
    for however long this actually takes."""
    conn = get_conn()
    already_synced = {r["translation"] for r in conn.execute("SELECT translation FROM synced_translations").fetchall()}
    rows = conn.execute(
        "SELECT book, chapter, verse, text, translation, book_number FROM bible_verses"
    ).fetchall()
    conn.close()
    rows = [r for r in rows if r["translation"] not in already_synced]
    if not rows:
        if progress_cb:
            progress_cb(0, 0)
        return 0
    BATCH = 300
    total = len(rows)
    for i in range(0, total, BATCH):
        batch = rows[i:i + BATCH]
        statements = [(TURSO_BIBLE_VERSES_SCHEMA, None)] if i == 0 else []
        for r in batch:
            statements.append((
                "INSERT OR REPLACE INTO bible_verses(book, chapter, verse, text, translation, book_number) "
                "VALUES (?,?,?,?,?,?)",
                (r["book"], r["chapter"], r["verse"], r["text"], r["translation"], r["book_number"])
            ))
        # Note on retries: if this raises partway through (a slow/timed-out
        # batch), NONE of this translation gets marked synced below, even
        # though earlier batches in this same run did make it to Turso —
        # INSERT OR REPLACE means those are safe, harmless no-ops to resend,
        # so clicking sync again simply redoes the whole translation rather
        # than corrupting anything. Slower than true resume-from-where-it-
        # failed would be, but correct.
        turso_pipeline(statements, timeout=30)
        if progress_cb:
            progress_cb(min(i + BATCH, total), total)
    conn = get_conn()
    push_ts = now()
    newly_synced_translations = {r["translation"] for r in rows}
    conn.executemany(
        "INSERT OR REPLACE INTO synced_translations(translation, synced_at) VALUES (?,?)",
        [(t, push_ts) for t in newly_synced_translations]
    )
    conn.commit()
    conn.close()
    return len(rows)


def turso_sync_all(progress_cb=None):
    """The single 'master sync' button: pushes every CHANGED song, saved
    service, and imported slide deck (skipping anything already synced
    since its last edit — see _rows_needing_sync), any Bible translation
    not already synced, and the current background/display settings
    (always — settings has no per-row tracking since it's a single row).
    Still entirely explicit — only runs when the user clicks it, never on a
    timer or on page load. Returns counts of what was ACTUALLY pushed, not
    the total library size, since unchanged items are now skipped.

    progress_cb, if given, is called as progress_cb(stage_label, done, total)
    — stage_label names which of the 5 stages is running (songs, services,
    background, slide decks, Bible verses), done/total are only meaningful
    within the Bible-verses stage (the one slow enough, and granular
    enough, to report real sub-progress on a translation that can be tens
    of thousands of rows split across many sequential Turso requests); for
    every other stage done/total are just 0/1 so the caller can still show
    which stage is active even though that stage itself isn't chunked."""
    def _report(stage, done=0, total=1):
        if progress_cb:
            progress_cb(stage, done, total)

    _report("Songs")
    n_songs = turso_push_all_songs()

    _report("Saved services")
    service_rows = _rows_needing_sync("services", ["id", "name", "service_date", "service_time", "items"])
    if service_rows:
        statements = [(TURSO_SERVICES_SCHEMA, None)]
        push_ts = now()
        for s in service_rows:
            statements.append((
                "INSERT OR REPLACE INTO services(id, name, service_date, service_time, items, updated_at) "
                "VALUES (?,?,?,?,?,?)",
                (s["id"], s["name"], s["service_date"], s["service_time"], s["items"], push_ts)
            ))
        turso_pipeline(statements, timeout=30)
        conn = get_conn()
        conn.executemany("UPDATE services SET synced_at=? WHERE id=?", [(push_ts, s["id"]) for s in service_rows])
        conn.commit()
        conn.close()
    n_services = len(service_rows)

    _report("Background/display settings")
    settings = get_settings()
    turso_push_background(settings["default_theme"], settings.get("default_background"),
                           settings.get("custom_background_data"), settings.get("church_name"))

    _report("Slide decks")
    deck_rows = _rows_needing_sync("slide_decks", ["id", "title", "source", "slides"])
    if deck_rows:
        statements = [(TURSO_SLIDE_DECKS_SCHEMA, None)]
        push_ts = now()
        for d in deck_rows:
            statements.append((
                "INSERT OR REPLACE INTO slide_decks(id, title, source, slides, updated_at) "
                "VALUES (?,?,?,?,?)",
                (d["id"], d["title"], d["source"], d["slides"], push_ts)
            ))
        turso_pipeline(statements, timeout=30)
        conn = get_conn()
        conn.executemany("UPDATE slide_decks SET synced_at=? WHERE id=?", [(push_ts, d["id"]) for d in deck_rows])
        conn.commit()
        conn.close()
    n_decks = len(deck_rows)

    _report("Bible verses", 0, 1)
    n_verses = turso_push_bible_verses(
        progress_cb=(lambda done, total: _report("Bible verses", done, max(total, 1))) if progress_cb else None
    )

    return n_songs, n_services, n_decks, n_verses


def _is_turso_space_error(e):
    """True if an exception from a Turso call looks like the database ran
    out of storage space, as opposed to a network hiccup, bad auth, etc."""
    msg = str(e).lower()
    return any(s in msg for s in (
        "disk is full", "sqlite_full", "database or disk is full",
        "no space", "quota", "storage limit", "database size limit",
    ))


def turso_delete_oldest_deck():
    """Deletes the single oldest imported slide deck from Turso (by
    created_at, ascending) to free up space — local copy is left untouched.
    Returns the deleted deck's title, or None if there were no decks."""
    decks = get_slide_decks()  # local rows, ordered by created_at DESC
    if not decks:
        return None
    oldest = decks[-1]
    turso_pipeline([(TURSO_SLIDE_DECKS_SCHEMA, None), ("DELETE FROM slide_decks WHERE id=?", (oldest["id"],))])
    return oldest["title"]


def turso_delete_oldest_service():
    """Deletes the single oldest saved service from Turso (by service_date,
    ascending) to free up space — local copy is left untouched. Returns the
    deleted service's name, or None if there were no services."""
    services = get_services()  # local rows, ordered by service_date DESC, id DESC
    if not services:
        return None
    oldest = services[-1]
    turso_pipeline([(TURSO_SERVICES_SCHEMA, None), ("DELETE FROM services WHERE id=?", (oldest["id"],))])
    return oldest["name"]


def turso_delete_bible_translation(translation):
    """Deletes every verse of one translation from Turso — local copy is
    left untouched (call delete_bible_translation separately for that).
    Fires only when explicitly requested from the Bible translation delete
    button, same as every other Turso write in this app."""
    turso_pipeline([(TURSO_BIBLE_VERSES_SCHEMA, None), ("DELETE FROM bible_verses WHERE translation=?", (translation,))])


ACCENT = "#C8A24A"          # warm gold — ECC accent, reserved for brand/primary actions

# Semantic status colors — kept separate from ACCENT so gold stays reserved
# for brand/primary actions and doesn't double as a state indicator.
LIVE_GREEN = "#3FAE6A"      # on air / live
BLACK_RED = "#B0463F"       # black screen / hidden
PAUSE_AMBER = "#D19A3D"     # paused / standby

# Basic shared login. This is a single shared password (not per-user
# accounts), so treat it as a light front-door lock rather than real
# security — anyone with the app's URL and this password gets full access.
LOGIN_USERNAME = "ECC"
LOGIN_PASSWORD = "5015"

_DB_INITIALIZED = False  # see main() — makes init_db() run once per process, not once per click
BG = "#0D0C0A"               # warm near-black (cinematic warm dark vs. cold tech dark)
CARD = "#17161A"             # charcoal card, warmed to match BG
CARD_BORDER = "#26252A"
TEXT_PRIMARY = "#F4F3EF"
TEXT_MUTED = "#9A9CA3"

THEMES = {
    "Minimal Dark": {"bg": "#000000", "fg": "#FFFFFF", "sub": "#C9C9C9", "font": "'Inter', sans-serif"},
    "Cinematic": {"bg": "#0A0A0A", "fg": "#FFFFFF", "sub": "#D8D2C4", "font": "'Manrope', sans-serif"},
    "Elegant": {"bg": "linear-gradient(160deg,#1a1710,#0a0908)", "fg": "#F5EFE0", "sub": "#C8A24A", "font": "Georgia, serif"},
    "Modern Worship": {"bg": "#0D1117", "fg": "#FFFFFF", "sub": f"{ACCENT}", "font": "'Inter', sans-serif"},
    "Classic": {"bg": "#03122B", "fg": "#FFFFFF", "sub": "#9FB6D9", "font": "Georgia, serif"},
}

# Preset projector backgrounds — original CSS gradients (no stock photos, so
# nothing to license and nothing that breaks if you're offline), styled after
# the ambient motion-background look apps like ProPresenter ship with. Each
# one overrides the theme's flat background color; text color/font still
# come from the selected theme. "anim" is an optional slow background-drift
# animation name (defined once in projector_css); leave it out for a static
# background.
BACKGROUNDS = {
    "None (theme color)": None,
    "Warm Bokeh": {
        "css": "radial-gradient(circle at 20% 30%, rgba(255,180,120,0.35), transparent 40%),"
               "radial-gradient(circle at 80% 70%, rgba(255,140,90,0.30), transparent 45%),"
               "radial-gradient(circle at 50% 50%, rgba(255,210,150,0.18), transparent 60%),"
               "#1a120a",
        "size": "220% 220%", "anim": "eccDrift 20s ease-in-out infinite alternate",
        "swatch": "linear-gradient(135deg,#3a2414,#7a3f1d,#c9863f)",
    },
    "Aurora Glow": {
        "css": "radial-gradient(circle at 30% 20%, rgba(80,220,180,0.30), transparent 45%),"
               "radial-gradient(circle at 70% 60%, rgba(120,90,220,0.30), transparent 50%),"
               "radial-gradient(circle at 50% 90%, rgba(60,160,220,0.25), transparent 55%),"
               "#05070d",
        "size": "220% 220%", "anim": "eccDrift 24s ease-in-out infinite alternate",
        "swatch": "linear-gradient(135deg,#063a2e,#2d1f6e,#0d4f7a)",
    },
    "Golden Rays": {
        "css": "conic-gradient(from 0deg at 50% 50%, rgba(200,162,74,0.24), rgba(11,9,4,0) 20%,"
               "rgba(200,162,74,0.16) 40%, rgba(11,9,4,0) 60%, rgba(200,162,74,0.20) 80%,"
               "rgba(11,9,4,0) 100%), #0b0904",
        "size": "100% 100%", "anim": None,
        "swatch": "conic-gradient(from 0deg, #c8a24a, #241c0e, #c8a24a, #241c0e)",
    },
    "Deep Space": {
        "css": "radial-gradient(1.5px 1.5px at 12% 22%, #fff, transparent),"
               "radial-gradient(1px 1px at 78% 38%, #fff, transparent),"
               "radial-gradient(1.5px 1.5px at 48% 78%, #fff, transparent),"
               "radial-gradient(2px 2px at 32% 58%, #fff, transparent),"
               "radial-gradient(1px 1px at 88% 88%, #fff, transparent),"
               "radial-gradient(1px 1px at 64% 14%, #fff, transparent),"
               "radial-gradient(1.5px 1.5px at 6% 68%, #fff, transparent),"
               "#05060b",
        "size": "100% 100%", "anim": "eccTwinkle 5s ease-in-out infinite alternate",
        "swatch": "radial-gradient(circle at 30% 30%, #fff 1px, transparent 2px) 0 0/12px 12px, #05060b",
    },
    "Ocean Waves": {
        "css": "radial-gradient(circle at 30% 100%, rgba(30,120,180,0.35), transparent 55%),"
               "radial-gradient(circle at 70% 100%, rgba(20,80,140,0.35), transparent 55%),"
               "linear-gradient(180deg,#020814,#03101f)",
        "size": "200% 200%", "anim": "eccDrift 16s ease-in-out infinite alternate",
        "swatch": "linear-gradient(180deg,#03101f,#0f4c75,#1e78b0)",
    },
    "Soft Clouds": {
        "css": "radial-gradient(circle at 25% 30%, rgba(255,255,255,0.10), transparent 45%),"
               "radial-gradient(circle at 75% 65%, rgba(255,255,255,0.08), transparent 50%),"
               "linear-gradient(160deg,#20242c,#0d0f13)",
        "size": "220% 220%", "anim": "eccDrift 26s ease-in-out infinite alternate",
        "swatch": "linear-gradient(160deg,#3a3f47,#20242c,#0d0f13)",
    },
}

SONG_CATEGORIES = ["Worship", "Praise", "Hymns", "Contemporary"]

# Intentionally empty — this app no longer ships any built-in sample Bible
# text. Load a translation your church has the rights to use from Church
# Settings → Import Bible Text.
BIBLE_SAMPLE = {}
BIBLE_TRANSLATION_LABEL = "KJV (Public Domain)"  # legacy label — used only to clean up old sample data below

# Canonical book order (standard 66-book Protestant order) used to line up the
# same passage across translations that name books differently (e.g. an
# Arabic file whose book names are in Arabic script can still be matched to
# an English translation by this shared number). Only covers the books used
# by the built-in sample; imported files bring their own numbers with them.
BIBLE_BOOK_NUMBERS = {"Psalm": 19, "John": 43, "Romans": 45, "Philippians": 50}
BIBLE_NUMBER_TO_BOOK = {v: k for k, v in BIBLE_BOOK_NUMBERS.items()}

# Standard canonical book titles by language, keyed by the same 1-66 book
# number used above. These are just conventional book titles (facts, not
# copyrighted creative text — unlike the verse text itself), used so a
# projector heading can show "التكوين" for an Arabic-language passage even
# when the imported file only labeled that book in English (as ar_svd.json
# does). Falls back to whatever name the source file used if a book number
# isn't available or isn't in this table.
LOCALIZED_BOOK_NAMES = {
    1: {"en": "Genesis", "ar": "التكوين"}, 2: {"en": "Exodus", "ar": "الخروج"},
    3: {"en": "Leviticus", "ar": "اللاويين"}, 4: {"en": "Numbers", "ar": "العدد"},
    5: {"en": "Deuteronomy", "ar": "التثنية"}, 6: {"en": "Joshua", "ar": "يشوع"},
    7: {"en": "Judges", "ar": "القضاة"}, 8: {"en": "Ruth", "ar": "راعوث"},
    9: {"en": "1 Samuel", "ar": "صموئيل الأول"}, 10: {"en": "2 Samuel", "ar": "صموئيل الثاني"},
    11: {"en": "1 Kings", "ar": "الملوك الأول"}, 12: {"en": "2 Kings", "ar": "الملوك الثاني"},
    13: {"en": "1 Chronicles", "ar": "أخبار الأيام الأول"}, 14: {"en": "2 Chronicles", "ar": "أخبار الأيام الثاني"},
    15: {"en": "Ezra", "ar": "عزرا"}, 16: {"en": "Nehemiah", "ar": "نحميا"},
    17: {"en": "Esther", "ar": "أستير"}, 18: {"en": "Job", "ar": "أيوب"},
    19: {"en": "Psalm", "ar": "المزامير"}, 20: {"en": "Proverbs", "ar": "الأمثال"},
    21: {"en": "Ecclesiastes", "ar": "الجامعة"}, 22: {"en": "Song of Solomon", "ar": "نشيد الأنشاد"},
    23: {"en": "Isaiah", "ar": "إشعياء"}, 24: {"en": "Jeremiah", "ar": "إرميا"},
    25: {"en": "Lamentations", "ar": "مراثي إرميا"}, 26: {"en": "Ezekiel", "ar": "حزقيال"},
    27: {"en": "Daniel", "ar": "دانيال"}, 28: {"en": "Hosea", "ar": "هوشع"},
    29: {"en": "Joel", "ar": "يوئيل"}, 30: {"en": "Amos", "ar": "عاموس"},
    31: {"en": "Obadiah", "ar": "عوبديا"}, 32: {"en": "Jonah", "ar": "يونان"},
    33: {"en": "Micah", "ar": "ميخا"}, 34: {"en": "Nahum", "ar": "ناحوم"},
    35: {"en": "Habakkuk", "ar": "حبقوق"}, 36: {"en": "Zephaniah", "ar": "صفنيا"},
    37: {"en": "Haggai", "ar": "حجي"}, 38: {"en": "Zechariah", "ar": "زكريا"},
    39: {"en": "Malachi", "ar": "ملاخي"}, 40: {"en": "Matthew", "ar": "متى"},
    41: {"en": "Mark", "ar": "مرقس"}, 42: {"en": "Luke", "ar": "لوقا"},
    43: {"en": "John", "ar": "يوحنا"}, 44: {"en": "Acts", "ar": "أعمال الرسل"},
    45: {"en": "Romans", "ar": "رومية"}, 46: {"en": "1 Corinthians", "ar": "كورنثوس الأولى"},
    47: {"en": "2 Corinthians", "ar": "كورنثوس الثانية"}, 48: {"en": "Galatians", "ar": "غلاطية"},
    49: {"en": "Ephesians", "ar": "أفسس"}, 50: {"en": "Philippians", "ar": "فيلبي"},
    51: {"en": "Colossians", "ar": "كولوسي"}, 52: {"en": "1 Thessalonians", "ar": "تسالونيكي الأولى"},
    53: {"en": "2 Thessalonians", "ar": "تسالونيكي الثانية"}, 54: {"en": "1 Timothy", "ar": "تيموثاوس الأولى"},
    55: {"en": "2 Timothy", "ar": "تيموثاوس الثانية"}, 56: {"en": "Titus", "ar": "تيطس"},
    57: {"en": "Philemon", "ar": "فليمون"}, 58: {"en": "Hebrews", "ar": "العبرانيين"},
    59: {"en": "James", "ar": "يعقوب"}, 60: {"en": "1 Peter", "ar": "بطرس الأولى"},
    61: {"en": "2 Peter", "ar": "بطرس الثانية"}, 62: {"en": "1 John", "ar": "يوحنا الأولى"},
    63: {"en": "2 John", "ar": "يوحنا الثانية"}, 64: {"en": "3 John", "ar": "يوحنا الثالثة"},
    65: {"en": "Jude", "ar": "يهوذا"}, 66: {"en": "Revelation", "ar": "الرؤيا"},
}

# ---------------------------------------------------------------------------
# DATABASE
# ---------------------------------------------------------------------------

def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS songs(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT, artist TEXT, category TEXT, tags TEXT,
        slides TEXT, favorite INTEGER DEFAULT 0, last_used TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS custom_slides(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT, body TEXT, created_at TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS slide_decks(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT, source TEXT, slides TEXT, created_at TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS services(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT, service_date TEXT, service_time TEXT,
        items TEXT, created_at TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS templates(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT, structure TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS settings(
        id INTEGER PRIMARY KEY CHECK (id=1),
        church_name TEXT, default_theme TEXT, default_background TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS presentation_state(
        id INTEGER PRIMARY KEY CHECK (id=1),
        service_id INTEGER, item_index INTEGER, slide_index INTEGER,
        black INTEGER, cleared INTEGER, live INTEGER, theme TEXT, background TEXT, font_scale REAL, updated_at TEXT,
        adhoc_active INTEGER, adhoc_slides TEXT, adhoc_index INTEGER
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS bible_verses(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        book TEXT, chapter INTEGER, verse INTEGER, text TEXT, translation TEXT,
        book_number INTEGER,
        UNIQUE(book, chapter, verse, translation)
    )""")
    conn.commit()
    try:
        c.execute("ALTER TABLE bible_verses ADD COLUMN book_number INTEGER")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # column already exists (older database)
    # Ad-hoc "Present Now" support (Bible tab: present a verse straight to the
    # projector without adding it to a service first). Older databases won't
    # have these columns yet, so add them if missing.
    for col, coltype in [("adhoc_active", "INTEGER"), ("adhoc_slides", "TEXT"), ("adhoc_index", "INTEGER"),
                         ("background", "TEXT"), ("font_scale", "REAL")]:
        try:
            c.execute(f"ALTER TABLE presentation_state ADD COLUMN {col} {coltype}")
            conn.commit()
        except sqlite3.OperationalError:
            pass  # column already exists (older database)
    try:
        c.execute("ALTER TABLE settings ADD COLUMN default_background TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # column already exists (older database)
    try:
        c.execute("ALTER TABLE settings ADD COLUMN custom_background_data TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # column already exists (older database)
    try:
        c.execute("ALTER TABLE settings ADD COLUMN meeting_type TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # column already exists (older database)

    # Incremental-sync support: each of these gets an updated_at (stamped on
    # every local write — see _touch_song/_touch_service/_touch_deck below)
    # plus a synced_at that's set the moment that exact row is successfully
    # pushed to Turso. "Sync All to Cloud" then only sends rows where
    # updated_at is newer than synced_at (or synced_at is still NULL,
    # meaning it's never been pushed) instead of unconditionally re-pushing
    # everything every single time, which is what it did before.
    for tbl in ("songs", "services", "slide_decks"):
        for col in ("updated_at", "synced_at"):
            try:
                c.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} TEXT")
                conn.commit()
            except sqlite3.OperationalError:
                pass  # column already exists (older database)
    # Bible verses have no natural per-row "last edited" moment (an
    # imported verse's text never changes after import — only whole
    # translations get added or removed), so tracking "already synced" per
    # translation name is the right grain here rather than per verse.
    c.execute("""CREATE TABLE IF NOT EXISTS synced_translations(
        translation TEXT PRIMARY KEY, synced_at TEXT
    )""")
    conn.commit()
    # Anything written before this migration existed has updated_at=NULL,
    # which the "needs sync" check below treats as "never synced, please
    # push it" — the correct, safe default (better to push once more than
    # necessary than to silently skip data that's actually never made it to
    # Turso).

    # One-time cleanup: remove any demo songs / sample Bible verses that a
    # previous version of this app seeded into an already-existing database.
    # This never touches songs/verses you added or imported yourself.
    if LEGACY_DEMO_SONG_TITLES:
        c.execute(
            f"DELETE FROM songs WHERE title IN ({','.join('?' for _ in LEGACY_DEMO_SONG_TITLES)}) AND artist='Traditional' "
            f"OR (title IN ({','.join('?' for _ in LEGACY_DEMO_SONG_TITLES)}) AND artist IN "
            f"('Chris Tomlin','Housefires','Hillsong Worship'))",
            LEGACY_DEMO_SONG_TITLES + LEGACY_DEMO_SONG_TITLES
        )
    c.execute("DELETE FROM bible_verses WHERE translation=?", (BIBLE_TRANSLATION_LABEL,))
    conn.commit()

    if c.execute("SELECT COUNT(*) FROM settings").fetchone()[0] == 0:
        c.execute("INSERT INTO settings(id, church_name, default_theme, default_background) "
                  "VALUES (1, 'ECC', 'Modern Worship', 'None (theme color)')")
    if c.execute("SELECT COUNT(*) FROM presentation_state").fetchone()[0] == 0:
        c.execute("""INSERT INTO presentation_state(id, service_id, item_index, slide_index, black, cleared, live, theme, background, font_scale, updated_at, adhoc_active, adhoc_slides, adhoc_index)
                     VALUES (1, NULL, 0, 0, 0, 1, 1, 'Modern Worship', 'None (theme color)', 1.0, ?, 0, NULL, 0)""", (now(),))
    conn.commit()

    if c.execute("SELECT COUNT(*) FROM songs").fetchone()[0] == 0:
        # One-time, one-request check — only fires when the local songs
        # table is genuinely empty (e.g. a host wiped the filesystem on
        # redeploy). Restores from Turso if you've synced there before;
        # otherwise falls back to the small built-in seed set. Either way
        # this is a single network call at cold-start, not a recurring one.
        restored = 0
        if turso_configured():
            try:
                remote_songs = turso_pull_all_songs()
                for r in remote_songs:
                    # Restored straight from Turso, so it's already synced —
                    # stamp updated_at AND synced_at to the same value so
                    # the very next sync click doesn't immediately re-push
                    # every song it just pulled down.
                    restore_ts = now()
                    c.execute(
                        "INSERT INTO songs(id, title, artist, category, tags, slides, favorite, last_used, "
                        "updated_at, synced_at) VALUES (?,?,?,?,?,?,0,?,?,?)",
                        (r["id"], r["title"], r["artist"], r["category"], r["tags"], r["slides"], now(),
                         restore_ts, restore_ts)
                    )
                conn.commit()
                restored = len(remote_songs)
            except Exception:
                restored = 0  # Turso unreachable/misconfigured — fall through to local seed below
        if restored == 0:
            seed_songs(conn)
    if c.execute("SELECT COUNT(*) FROM services").fetchone()[0] == 0 and turso_configured():
        # Same one-time, one-request pattern as songs above — only checks
        # Turso when local services are completely empty (a fresh/wiped
        # filesystem). If you've never synced a service, this just finds
        # nothing and moves on; no seed data needed here either way.
        try:
            remote_services = turso_pull_all_services()
            for r in remote_services:
                # Same reasoning as songs above: pulled straight from
                # Turso, so already synced — stamp both timestamps now.
                restore_ts = now()
                c.execute(
                    "INSERT INTO services(id, name, service_date, service_time, items, created_at, "
                    "updated_at, synced_at) VALUES (?,?,?,?,?,?,?,?)",
                    (r["id"], r["name"], r["service_date"], r["service_time"], r["items"], now(),
                     restore_ts, restore_ts)
                )
            conn.commit()
        except Exception:
            pass  # Turso unreachable/misconfigured — no local services is an OK starting state
    if c.execute("SELECT COUNT(*) FROM templates").fetchone()[0] == 0:
        c.execute("INSERT INTO templates(name, structure) VALUES (?, ?)", (
            "Sunday Worship",
            json.dumps(["Welcome", "Song", "Song", "Song", "Scripture Reading", "Sermon", "Closing Song"])
        ))
    if c.execute("SELECT COUNT(*) FROM bible_verses").fetchone()[0] == 0:
        # THE FIX: this used to just reseed the small built-in BIBLE_SAMPLE
        # set unconditionally, with no attempt to restore whatever
        # translation you'd actually imported and synced — so a full
        # imported Bible would vanish every time Streamlit restarted the
        # app (which wipes local disk), even though it was sitting safely
        # in Turso the whole time. Now this checks Turso FIRST, exactly
        # like songs and services already do, and only falls back to the
        # tiny sample set if there's nothing to restore (Turso not
        # configured, or genuinely never synced).
        restored_verses = 0
        if turso_configured():
            try:
                remote_verses = turso_pull_all_bible_verses()
                if remote_verses:
                    restore_ts = now()
                    c.executemany(
                        "INSERT OR IGNORE INTO bible_verses(book, chapter, verse, text, translation, book_number) "
                        "VALUES (?,?,?,?,?,?)",
                        [(v["book"], v["chapter"], v["verse"], v["text"], v["translation"], v["book_number"])
                         for v in remote_verses]
                    )
                    conn.commit()
                    # Restored straight from Turso, so every translation
                    # that came back is already synced — mark them so the
                    # next "Sync All" doesn't immediately re-push everything
                    # it just pulled down (same reasoning as songs/services
                    # above).
                    restored_translations = {v["translation"] for v in remote_verses}
                    c.executemany(
                        "INSERT OR REPLACE INTO synced_translations(translation, synced_at) VALUES (?,?)",
                        [(t, restore_ts) for t in restored_translations]
                    )
                    conn.commit()
                    restored_verses = len(remote_verses)
            except Exception:
                restored_verses = 0  # Turso unreachable/misconfigured — fall through to the sample seed below
        if restored_verses == 0:
            for book, chapters in BIBLE_SAMPLE.items():
                for chapter, verses in chapters.items():
                    for verse, text in verses.items():
                        c.execute(
                            "INSERT OR IGNORE INTO bible_verses(book, chapter, verse, text, translation, book_number) VALUES (?,?,?,?,?,?)",
                            (book, chapter, verse, text, BIBLE_TRANSLATION_LABEL, BIBLE_BOOK_NUMBERS.get(book))
                        )
    conn.commit()
    conn.close()


# Legacy demo song titles — kept only so init_db can clean out any of these
# that were seeded into an older local database; no longer seeded fresh.
LEGACY_DEMO_SONG_TITLES = [
    "Amazing Grace", "How Great Thou Art", "Holy Forever", "Build My Life", "What A Beautiful Name",
]


def seed_songs(conn):
    demo = []  # intentionally empty — no sample songs ship with this app anymore
    for title, artist, category, lyrics in demo:
        slides = [s.strip() for s in lyrics.split("\n\n") if s.strip()]
        conn.execute(
            "INSERT INTO songs(title, artist, category, tags, slides, favorite, last_used) VALUES (?,?,?,?,?,0,?)",
            (title, artist, category, "", json.dumps(slides), now())
        )
    conn.commit()


def now():
    return datetime.datetime.now().isoformat()


def _touch_song(song_id):
    """Stamps a song's updated_at to right now — call this after ANY local
    write to that song (new song, lyric edit, slide reorder, favorite
    toggle, etc.) so the incremental sync below can tell it's changed since
    its last push. Deliberately separate from synced_at, which only moves
    when the row is actually confirmed pushed to Turso."""
    conn = get_conn()
    conn.execute("UPDATE songs SET updated_at=? WHERE id=?", (now(), song_id))
    conn.commit()
    conn.close()


def _touch_service(service_id):
    """Same idea as _touch_song, for services — call after any items/name/
    date/time change to that saved service."""
    conn = get_conn()
    conn.execute("UPDATE services SET updated_at=? WHERE id=?", (now(), service_id))
    conn.commit()
    conn.close()


def _touch_deck(deck_id):
    """Same idea as _touch_song, for imported slide decks."""
    conn = get_conn()
    conn.execute("UPDATE slide_decks SET updated_at=? WHERE id=?", (now(), deck_id))
    conn.commit()
    conn.close()


def _mark_synced(table, row_id):
    """Stamps a row's synced_at to right now, right after a successful push
    of that exact row — this is what "already synced" actually checks
    against on the next sync."""
    conn = get_conn()
    conn.execute(f"UPDATE {table} SET synced_at=? WHERE id=?", (now(), row_id))
    conn.commit()
    conn.close()


def _rows_needing_sync(table, columns):
    """Returns every row from `table` whose updated_at is newer than its
    synced_at (or that has never been synced at all — synced_at IS NULL).
    This is the actual "only sync stuff that isn't already synced" check —
    everything else already matching is skipped instead of re-pushed."""
    conn = get_conn()
    col_list = ", ".join(columns)
    rows = conn.execute(
        f"SELECT {col_list} FROM {table} "
        f"WHERE synced_at IS NULL OR (updated_at IS NOT NULL AND updated_at > synced_at)"
    ).fetchall()
    conn.close()
    return rows


# ---------------- Songs ----------------

def get_songs(search="", category="All Songs"):
    conn = get_conn()
    rows = conn.execute("SELECT * FROM songs ORDER BY title").fetchall()
    conn.close()
    results = []
    for r in rows:
        if category == "Favorites" and not r["favorite"]:
            continue
        if category not in ("All Songs", "Favorites", "Recently Used") and r["category"] != category:
            continue
        if search:
            hay = f"{r['title']} {r['artist']} {r['tags']} {r['slides']}".lower()
            if search.lower() not in hay:
                continue
        results.append(r)
    if category == "Recently Used":
        results = sorted(results, key=lambda r: r["last_used"] or "", reverse=True)
    return results


def delete_song(song_id):
    conn = get_conn()
    conn.execute("DELETE FROM songs WHERE id=?", (song_id,))
    conn.commit()
    conn.close()


def find_duplicate_song_ids():
    """
    Groups songs by (title, artist) case-insensitively and returns the ids
    of every duplicate EXCEPT the one kept per group — the newest-added
    (highest id), since that's usually the most complete re-import.
    """
    conn = get_conn()
    rows = conn.execute("SELECT id, title, artist FROM songs ORDER BY id").fetchall()
    conn.close()
    groups = {}
    for r in rows:
        key = (r["title"].strip().lower(), (r["artist"] or "").strip().lower())
        groups.setdefault(key, []).append(r["id"])
    to_delete = []
    for ids in groups.values():
        if len(ids) > 1:
            to_delete.extend(ids[:-1])  # keep the highest id (most recent)
    return to_delete


def delete_songs(song_ids):
    if not song_ids:
        return
    conn = get_conn()
    conn.executemany("DELETE FROM songs WHERE id=?", [(sid,) for sid in song_ids])
    conn.commit()
    conn.close()


def get_song(song_id):
    conn = get_conn()
    r = conn.execute("SELECT * FROM songs WHERE id=?", (song_id,)).fetchone()
    conn.close()
    return r


def add_song(title, artist, category, tags, lyrics):
    slides = [s.strip() for s in lyrics.split("\n\n") if s.strip()] or ["(empty)"]
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO songs(title, artist, category, tags, slides, favorite, last_used, updated_at) VALUES (?,?,?,?,?,0,?,?)",
        (title, artist, category, tags, json.dumps(slides), now(), now())
    )
    song_id = cur.lastrowid
    conn.commit()
    conn.close()
    return song_id, slides


def get_song_by_title(title):
    """Case-insensitive exact-title lookup — used so adding a song that
    already exists overwrites it instead of erroring or duplicating it."""
    conn = get_conn()
    r = conn.execute("SELECT * FROM songs WHERE LOWER(title)=LOWER(?)", (title.strip(),)).fetchone()
    conn.close()
    return r


def upsert_song(title, artist, category, tags, slides):
    """Save a song by its already-split slides. If a song with the same
    title (case-insensitive) already exists, its slides/artist/category/tags
    are overwritten in place instead of erroring or creating a duplicate row.
    Returns (song_id, slides, was_overwrite)."""
    slides = slides or ["(empty)"]
    existing = get_song_by_title(title)
    conn = get_conn()
    if existing:
        conn.execute(
            "UPDATE songs SET artist=?, category=?, tags=?, slides=?, updated_at=? WHERE id=?",
            (artist, category, tags, json.dumps(slides), now(), existing["id"])
        )
        song_id = existing["id"]
        was_overwrite = True
    else:
        cur = conn.execute(
            "INSERT INTO songs(title, artist, category, tags, slides, favorite, last_used, updated_at) VALUES (?,?,?,?,?,0,?,?)",
            (title, artist, category, tags, json.dumps(slides), now(), now())
        )
        song_id = cur.lastrowid
        was_overwrite = False
    conn.commit()
    conn.close()
    return song_id, slides, was_overwrite


def add_song_with_slides(title, artist, category, tags, slides):
    """Like add_song, but takes already-split slides directly instead of
    re-splitting a raw lyrics blob on blank lines — used by the paste-lyrics
    importer, whose parser already decides where each slide breaks."""
    slides = slides or ["(empty)"]
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO songs(title, artist, category, tags, slides, favorite, last_used, updated_at) VALUES (?,?,?,?,?,0,?,?)",
        (title, artist, category, tags, json.dumps(slides), now(), now())
    )
    song_id = cur.lastrowid
    conn.commit()
    conn.close()
    return song_id, slides


# Max characters packed onto one slide — sized to stay comfortably readable
# on a regular TV/projector at normal font size. Lines are packed onto a
# slide until the NEXT line would push the slide over this budget; that line
# starts a new slide instead. A line is never split mid-way — if a single
# line alone is longer than the budget, it still gets its own whole slide
# rather than being cut.
MAX_SLIDE_CHARS = 220


def parse_pasted_lyrics(raw, max_slide_chars=MAX_SLIDE_CHARS):
    """Parses the standard lyrics-site paste format:

        Song Title
        Song by Artist Name ‧ Year

        Overview
        Lyrics
        <actual lyrics...>

    Strips the title/artist/year header and the "Overview"/"Lyrics" section
    labels, then packs the remaining lines onto slides using a character
    budget sized for a TV/projector (see MAX_SLIDE_CHARS) instead of a fixed
    line count — a new slide starts whenever the next line would overflow
    the budget, and a stanza (blank-line) break always starts a fresh slide
    too. Lines are never split mid-line: if one line alone exceeds the
    budget, it still becomes its own slide in full. Returns
    (title, artist, year, slides)."""
    lines = raw.splitlines()

    while lines and not lines[0].strip():
        lines.pop(0)

    title = lines.pop(0).strip() if lines else "Untitled"

    artist, year = "", ""
    if lines and re.match(r"^song by\s+", lines[0].strip(), re.IGNORECASE):
        m = re.match(r"^song by\s+(.*?)(?:\s*[‧·]\s*(\d{4}))?\s*$", lines[0].strip(), re.IGNORECASE)
        if m:
            artist = m.group(1).strip()
            year = m.group(2) or ""
        lines.pop(0)

    # Skip blank lines and the "Overview"/"Lyrics" section labels that sit
    # before the actual lyrics on most lyrics sites.
    while lines and (not lines[0].strip() or lines[0].strip().lower() in ("overview", "lyrics")):
        lines.pop(0)

    body_lines = [l.rstrip() for l in lines]
    while body_lines and not body_lines[-1].strip():
        body_lines.pop()

    body_lines = strip_musixmatch_footer(body_lines)

    stanzas, current = [], []
    for l in body_lines:
        if not l.strip():
            if current:
                stanzas.append(current)
                current = []
        else:
            current.append(l)
    if current:
        stanzas.append(current)

    slides = []
    for stanza in stanzas:
        current_lines, current_len = [], 0
        for line in stanza:
            line_len = len(line) + (1 if current_lines else 0)  # +1 for the joining newline
            if current_lines and current_len + line_len > max_slide_chars:
                slides.append("\n".join(current_lines))
                current_lines, current_len = [line], len(line)
            else:
                current_lines.append(line)
                current_len += line_len
        if current_lines:
            slides.append("\n".join(current_lines))

    if not slides:
        slides = ["(empty)"]
    return title, artist, year, slides


MUSIXMATCH_FOOTER_PATTERNS = [
    re.compile(r"^\s*source\s*:\s*musixmatch\s*$", re.IGNORECASE),
    re.compile(r"^\s*songwriters?\s*:", re.IGNORECASE),
    re.compile(r"^\s*writers?\s*:", re.IGNORECASE),
    re.compile(r"lyrics\s*©", re.IGNORECASE),
    re.compile(r"^\s*©", re.IGNORECASE),
]


def strip_musixmatch_footer(body_lines):
    """
    Musixmatch (and similar lyrics-site) pastes often end with an
    attribution block like:

        Source: Musixmatch
        Songwriters: Name One / Name Two
        Song Title Lyrics © Publisher A, Publisher B

    None of that is part of the song and it must never end up on a slide.
    Once any footer-marker line is found, that line and everything after it
    is dropped — the block is always trailing, never in the middle of a
    stanza the way real lyrics repeats can be.
    """
    cut = len(body_lines)
    for i, line in enumerate(body_lines):
        if any(p.search(line) for p in MUSIXMATCH_FOOTER_PATTERNS):
            cut = i
            break
    trimmed = body_lines[:cut]
    while trimmed and not trimmed[-1].strip():
        trimmed.pop()
    return trimmed


def update_song_slides(song_id, slides):
    conn = get_conn()
    conn.execute("UPDATE songs SET slides=?, updated_at=? WHERE id=?", (json.dumps(slides), now(), song_id))
    conn.commit()
    conn.close()


def toggle_favorite(song_id):
    conn = get_conn()
    r = conn.execute("SELECT favorite FROM songs WHERE id=?", (song_id,)).fetchone()
    conn.execute("UPDATE songs SET favorite=? WHERE id=?", (0 if r["favorite"] else 1, song_id))
    conn.commit()
    conn.close()


# ---------------- Custom slides ----------------

def add_custom_slide(title, body):
    conn = get_conn()
    conn.execute("INSERT INTO custom_slides(title, body, created_at) VALUES (?,?,?)", (title, body, now()))
    conn.commit()
    conn.close()


def get_custom_slides():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM custom_slides ORDER BY created_at DESC").fetchall()
    conn.close()
    return rows


# ---------------- Slide decks (imported Google Slides PDFs) ----------------

def add_slide_deck(title, source, slide_data_uris):
    """slide_data_uris: list of base64 data-URI strings, one per slide image."""
    conn = get_conn()
    conn.execute("INSERT INTO slide_decks(title, source, slides, created_at, updated_at) VALUES (?,?,?,?,?)",
                 (title, source, json.dumps(slide_data_uris), now(), now()))
    conn.commit()
    conn.close()


def get_slide_decks():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM slide_decks ORDER BY created_at DESC").fetchall()
    conn.close()
    return rows


def get_slide_deck(deck_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM slide_decks WHERE id=?", (deck_id,)).fetchone()
    conn.close()
    return row


def delete_slide_deck(deck_id):
    conn = get_conn()
    conn.execute("DELETE FROM slide_decks WHERE id=?", (deck_id,))
    conn.commit()
    conn.close()


def make_deck_item(deck_row):
    """Turns an imported slide deck into a service item — each page becomes
    an image slide, shown full-bleed on the projector."""
    images = json.loads(deck_row["slides"])
    return {"type": "imagedeck", "ref_id": deck_row["id"], "title": deck_row["title"], "images": images}


# ---------------- Services ----------------

def create_service(name, service_date, service_time, items=None):
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO services(name, service_date, service_time, items, created_at, updated_at) VALUES (?,?,?,?,?,?)",
        (name, service_date, service_time, json.dumps(items or []), now(), now())
    )
    conn.commit()
    sid = cur.lastrowid
    conn.close()
    return sid


def get_services():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM services ORDER BY service_date DESC, id DESC").fetchall()
    conn.close()
    return rows


def get_service(service_id):
    conn = get_conn()
    r = conn.execute("SELECT * FROM services WHERE id=?", (service_id,)).fetchone()
    conn.close()
    return r


def update_service_items(service_id, items):
    conn = get_conn()
    conn.execute("UPDATE services SET items=?, updated_at=? WHERE id=?", (json.dumps(items), now(), service_id))
    conn.commit()
    conn.close()


def duplicate_service(service_id):
    s = get_service(service_id)
    return create_service(f"{s['name']} (Copy)", s["service_date"], s["service_time"], json.loads(s["items"]))


# ---------------- Presentation state (shared between operator + projector) ----------------

def get_state():
    conn = get_conn()
    r = conn.execute("SELECT * FROM presentation_state WHERE id=1").fetchone()
    conn.close()
    return dict(r)


def set_state(**kwargs):
    kwargs["updated_at"] = now()
    conn = get_conn()
    cols = ", ".join(f"{k}=?" for k in kwargs)
    conn.execute(f"UPDATE presentation_state SET {cols} WHERE id=1", tuple(kwargs.values()))
    conn.commit()
    conn.close()


def present_adhoc_now(slides):
    """
    Push slides straight to the projector without needing a saved service —
    used by the Bible tab's "Present Now" buttons. `slides` is a list of
    (ref, text, text2_or_None) tuples, same shape as item_slides() returns.
    Any in-progress service navigation (item_index/slide_index) is left
    untouched so resuming the service afterward picks up where it left off;
    adhoc_active just tells the projector/operator view to show this instead.
    """
    set_state(
        adhoc_active=1,
        adhoc_slides=json.dumps(slides),
        adhoc_index=0,
        black=0, cleared=0, live=1,
    )


def exit_adhoc_present():
    """Return the projector to whatever the normal service state points at."""
    set_state(adhoc_active=0, adhoc_slides=None, adhoc_index=0)


def get_settings():
    conn = get_conn()
    r = conn.execute("SELECT * FROM settings WHERE id=1").fetchone()
    conn.close()
    return dict(r)


def set_settings(**kwargs):
    conn = get_conn()
    cols = ", ".join(f"{k}=?" for k in kwargs)
    conn.execute(f"UPDATE settings SET {cols} WHERE id=1", tuple(kwargs.values()))
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# ITEM / SLIDE HELPERS
# ---------------------------------------------------------------------------

IMG_SLIDE_PREFIX = "\x00IMG\x00"  # sentinel: marks a slide's "text" as an image data-URI, not words

# Lines-per-slide baseline at font_scale=1.0, tuned to stay comfortably on a
# regular TV/projector. As the operator increases font size, this is divided
# down so each slide holds fewer lines/words instead of overflowing and
# forcing a scrollbar — the extra lines spill onto additional slides rather
# than ever being cut mid-line.
BASE_MAX_LINES_PER_SLIDE = 6


def _wrap_text_lines(text, max_lines):
    """Split `text` into chunks of at most max_lines lines each, always
    breaking on a full line — never mid-line. Returns a list of chunk
    strings (length 1 if it already fits)."""
    lines = text.split("\n")
    if len(lines) <= max_lines:
        return [text]
    return ["\n".join(lines[i:i + max_lines]) for i in range(0, len(lines), max_lines)]


def item_slides(item, font_scale=1.0):
    """Return list of (reference_or_none, text, secondary_text_or_None) for a service item.
    For imported slide decks (Google Slides PDF import), each "text" is an
    image data-URI prefixed with IMG_SLIDE_PREFIX — renderers check for that
    prefix and draw an <img> full-bleed instead of styled text.

    font_scale reflows plain-text slides (song lyrics, custom slides,
    announcements, single-language Bible verses) so they keep fitting on
    screen as the live font size grows: instead of overflowing and causing a
    scrollbar, a slide with too many lines for the current font size is
    split into consecutive slides — a full line is always kept together,
    never cut in half. Image slides and bilingual split-screen slides are
    left as-is (their own layouts already handle sizing)."""
    if item["type"] == "song":
        raw = [(None, s, None) for s in item["slides"]]
    elif item["type"] == "bible":
        raw = [(v["ref"], v["text"], v.get("text2")) for v in item["slides"]]
    elif item["type"] in ("custom", "announcement"):
        raw = [(None, item["slides"][0] if item["slides"] else "", None)]
    elif item["type"] == "imagedeck":
        return [(None, IMG_SLIDE_PREFIX + img, None) for img in item.get("images", [])]
    else:
        return [(None, "", None)]

    font_scale = font_scale or 1.0
    if font_scale <= 1.0:
        return raw

    max_lines = max(1, round(BASE_MAX_LINES_PER_SLIDE / font_scale))
    expanded = []
    for ref, text, text2 in raw:
        if text2 or not text or text.startswith(IMG_SLIDE_PREFIX):
            expanded.append((ref, text, text2))
            continue
        for chunk in _wrap_text_lines(text, max_lines):
            expanded.append((ref, chunk, None))
    return expanded


def make_song_item(song_row):
    return {
        "type": "song",
        "ref_id": song_row["id"],
        "title": song_row["title"],
        "slides": json.loads(song_row["slides"]),
    }


def localized_book_name(book, translation, sample_text=""):
    """
    Pick the right-language heading for a book, using the canonical book
    number to cross-reference LOCALIZED_BOOK_NAMES — so the projector can
    show "التكوين" for an Arabic passage even if the source file only ever
    labeled that book "Genesis". Falls back to whatever name the source
    file used if there's no book number or no entry for the detected
    language.
    """
    book_number = get_book_number(book, translation)
    lang = "ar" if _looks_arabic(sample_text) else "en"
    names = LOCALIZED_BOOK_NAMES.get(book_number) if book_number else None
    return (names.get(lang) or book) if names else book


def make_bible_item(book, chapter, verse_nums, translation=None, secondary_translation=None, combine=False):
    """
    Build a Bible service item. If secondary_translation is given, each slide
    also carries the same verse's text in that translation (looked up by the
    shared canonical book number when available, so an Arabic and an English
    translation can still be lined up even though they name books
    differently) — this is what powers the bilingual split-screen display.

    If combine=True, all the requested verses are merged into a single slide
    (e.g. selecting verses 1,2,3 shows them together, referenced as
    "Genesis 1:1-3") instead of one slide per verse.
    """
    verses = get_bible_verses(book, chapter, translation)
    book_number = get_book_number(book, translation) if translation else None
    heading_book = localized_book_name(book, translation, next(iter(verses.values()), "")) if translation else book

    if combine:
        combined_text = "\n".join(f"{v} {verses.get(v, '')}".strip() for v in verse_nums)
        ref = f"{heading_book} {chapter}:{verse_nums[0]}" if len(verse_nums) == 1 else \
            f"{heading_book} {chapter}:{verse_nums[0]}-{verse_nums[-1]}"
        slide = {"ref": ref, "text": combined_text}
        if secondary_translation:
            combined_text2 = "\n".join(
                f"{v} {get_verse_in_translation(book, chapter, v, secondary_translation, book_number)}".strip()
                for v in verse_nums
            )
            slide["text2"] = combined_text2
        slides = [slide]
    else:
        slides = []
        for v in verse_nums:
            slide = {"ref": f"{heading_book} {chapter}:{v}", "text": verses.get(v, "")}
            if secondary_translation:
                slide["text2"] = get_verse_in_translation(book, chapter, v, secondary_translation, book_number)
            slides.append(slide)

    label = f"{book} {chapter}:{verse_nums[0]}" if len(verse_nums) == 1 else \
        f"{book} {chapter}:{verse_nums[0]}-{verse_nums[-1]}"
    return {"type": "bible", "ref_id": None, "title": label, "slides": slides}


def make_custom_item(title, body):
    return {"type": "custom", "ref_id": None, "title": title, "slides": [body]}


def make_announcement_item(title):
    return {"type": "announcement", "ref_id": None, "title": title, "slides": [title]}


# ---------------------------------------------------------------------------
# BIBLE DATA ACCESS (backed by the bible_verses table, seeded from BIBLE_SAMPLE)
# ---------------------------------------------------------------------------

def get_bible_translations():
    conn = get_conn()
    rows = conn.execute("SELECT DISTINCT translation FROM bible_verses ORDER BY translation").fetchall()
    conn.close()
    return [r["translation"] for r in rows] or [BIBLE_TRANSLATION_LABEL]


def delete_bible_translation(translation):
    """Deletes every verse of one translation from the local database, and
    clears its synced_translations record too — otherwise a re-import of
    the same translation name would look "already synced" (see
    turso_push_bible_verses) and silently never get pushed back to Turso.
    Returns the number of verses deleted."""
    conn = get_conn()
    n = conn.execute("SELECT COUNT(*) FROM bible_verses WHERE translation=?", (translation,)).fetchone()[0]
    conn.execute("DELETE FROM bible_verses WHERE translation=?", (translation,))
    conn.execute("DELETE FROM synced_translations WHERE translation=?", (translation,))
    conn.commit()
    conn.close()
    return n


def get_bible_books(translation=None):
    conn = get_conn()
    if translation:
        rows = conn.execute(
            "SELECT DISTINCT book FROM bible_verses WHERE translation=? ORDER BY id", (translation,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT DISTINCT book FROM bible_verses ORDER BY id").fetchall()
    conn.close()
    return [r["book"] for r in rows]


def get_bible_chapters(book, translation=None):
    conn = get_conn()
    if translation:
        rows = conn.execute(
            "SELECT DISTINCT chapter FROM bible_verses WHERE book=? AND translation=? ORDER BY chapter",
            (book, translation)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT DISTINCT chapter FROM bible_verses WHERE book=? ORDER BY chapter", (book,)
        ).fetchall()
    conn.close()
    return [r["chapter"] for r in rows]


def get_bible_verses(book, chapter, translation=None):
    """Returns an ordered dict-like list of (verse_number, text)."""
    conn = get_conn()
    if translation:
        rows = conn.execute(
            "SELECT verse, text FROM bible_verses WHERE book=? AND chapter=? AND translation=? ORDER BY verse",
            (book, chapter, translation)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT verse, text FROM bible_verses WHERE book=? AND chapter=? ORDER BY verse",
            (book, chapter)
        ).fetchall()
    conn.close()
    return {r["verse"]: r["text"] for r in rows}


def get_verse_text(book, chapter, verse, translation=None):
    verses = get_bible_verses(book, chapter, translation)
    return verses.get(verse, "")


def get_book_number(book, translation):
    """Look up the canonical book_number stored against a book in a given translation."""
    conn = get_conn()
    r = conn.execute(
        "SELECT book_number FROM bible_verses WHERE book=? AND translation=? AND book_number IS NOT NULL LIMIT 1",
        (book, translation)
    ).fetchone()
    conn.close()
    return r["book_number"] if r else None


def get_verse_in_translation(book, chapter, verse, translation, book_number=None):
    """
    Look up the same verse in another translation. Tries matching by the
    shared canonical book_number first (works even if the two translations
    name the book differently, e.g. an Arabic book name vs. an English one),
    then falls back to matching by the literal book name (works when both
    translations use the same naming, e.g. two English translations).
    Returns "" if no match is found.
    """
    conn = get_conn()
    if book_number is not None:
        r = conn.execute(
            "SELECT text FROM bible_verses WHERE book_number=? AND chapter=? AND verse=? AND translation=? LIMIT 1",
            (book_number, chapter, verse, translation)
        ).fetchone()
        if r:
            conn.close()
            return r["text"]
    r = conn.execute(
        "SELECT text FROM bible_verses WHERE book=? AND chapter=? AND verse=? AND translation=? LIMIT 1",
        (book, chapter, verse, translation)
    ).fetchone()
    conn.close()
    return r["text"] if r else ""


def parse_bible_reference(text, translation):
    """
    Parses a typed reference like "John 3:16", "Genesis 1:1-3", or "1 John 2"
    into (book, chapter, verse_numbers) for the quick-jump search box. Book
    matching is case/spacing-insensitive and tries an exact match first,
    then a prefix match, against whatever books actually exist for this
    translation. Returns None if it can't confidently parse/match.
    """
    m = re.match(r"^\s*(\d?\s*[A-Za-z][A-Za-z .]*?)\s+(\d+)\s*(?::\s*(\d+)(?:\s*-\s*(\d+))?)?\s*$", text.strip())
    if not m:
        return None
    book_query, chapter_str, v1, v2 = m.groups()
    chapter = int(chapter_str)
    norm = lambda s: re.sub(r"\s+", "", s).lower()
    query_norm = norm(book_query)
    books = get_bible_books(translation)
    match = next((b for b in books if norm(b) == query_norm), None)
    if not match:
        match = next((b for b in books if norm(b).startswith(query_norm) or query_norm.startswith(norm(b))), None)
    if not match:
        return None
    if chapter not in get_bible_chapters(match, translation):
        return None
    verse_nums = []
    if v1:
        start = int(v1)
        end = int(v2) if v2 else start
        verse_nums = [v for v in get_bible_verses(match, chapter, translation).keys() if start <= v <= end]
    return match, chapter, verse_nums


def _first_key(d, candidates):
    """Return the value of the first matching key (case-insensitive) found in d, else None."""
    lower_map = {k.lower(): k for k in d.keys()}
    for cand in candidates:
        if cand in lower_map:
            return d[lower_map[cand]]
    return None


def _extract_book(entry, name_keys, number_keys):
    """
    Pull both a display name and a canonical numeric book id off a row,
    whatever the source calls them. Needed because a scrollmapper-style
    export has both 'book_name' (text, e.g. Arabic script) and 'book'
    (a shared numeric id, e.g. 1=Genesis) — the numeric id is what lets us
    line up the same verse across two differently-named translations later.
    """
    name = _first_key(entry, name_keys)
    num_raw = _first_key(entry, number_keys)
    number = None
    if num_raw is not None:
        try:
            number = int(num_raw)
        except (TypeError, ValueError):
            number = None
    if name is None:
        if number is not None and number in BIBLE_NUMBER_TO_BOOK:
            name = BIBLE_NUMBER_TO_BOOK[number]
        elif num_raw is not None and number is None:
            name = str(num_raw)  # the "book" field turned out to hold a text name, not a number
    return name, number


def _flatten_bible_rows(data):
    """
    Normalize many common 'bible JSON dump' shapes into a flat list of
    (book, book_number_or_None, chapter, verse, text) tuples. Handles, among
    others:
      - Flat list with keys book/chapter/verse/text (any common naming variant,
        e.g. book_name, chapter_number, verse_number, verse_text — this covers
        the popular scrollmapper/bible_databases exports like 'ar_svd.json',
        which also carry a numeric book id alongside the text name)
      - A wrapper dict with the real list under 'verses', 'data', or 'rows'
      - Nested dict: {book: {chapter: {verse: text}}}
      - Nested list-of-books: [{"name"/"book": "Genesis", "chapters": [{"chapter":1,"verses":[{"verse":1,"text":"..."}]}]}]
    """
    NAME_KEYS = ["book_name", "bookname", "name"]
    NUMBER_KEYS = ["book_number", "booknumber", "book_id", "bookid", "book", "b", "number", "num"]
    CHAPTER_KEYS = ["chapter", "chapter_number", "chapternumber", "c"]
    VERSE_KEYS = ["verse", "verse_number", "versenumber", "v"]
    TEXT_KEYS = ["text", "verse_text", "versetext", "t", "content"]

    # Unwrap common wrapper dicts
    if isinstance(data, dict):
        for wrapper_key in ("verses", "data", "rows", "results"):
            if wrapper_key in data and isinstance(data[wrapper_key], list):
                data = data[wrapper_key]
                break

    rows = []

    if isinstance(data, list) and data and isinstance(data[0], dict):
        sample = data[0]
        has_chapters_list = "chapters" in {k.lower() for k in sample.keys()}
        if has_chapters_list:
            # Nested list-of-books shape. "chapters" itself comes in two
            # common shapes depending on the source dataset:
            #   (a) a list of {"chapter":.., "verses":[{"verse":..,"text":..}, ...]} dicts
            #   (b) a list of plain lists of verse strings, e.g.
            #       [["In the beginning...", "And the earth..."], [...]]
            #       (used by the popular scrollmapper/bible_databases exports,
            #       including ar_svd.json) — chapter/verse numbers here are
            #       just the 1-based position in each list, since the file
            #       doesn't label them explicitly.
            for book_entry in data:
                book, book_number = _extract_book(book_entry, NAME_KEYS, NUMBER_KEYS)
                chapters = _first_key(book_entry, ["chapters"])
                for chapter_idx, ch_entry in enumerate(chapters, start=1):
                    if isinstance(ch_entry, list):
                        # Shape (b): a bare list of verse strings for this chapter
                        chapter = chapter_idx
                        for verse_idx, text in enumerate(ch_entry, start=1):
                            if book and text:
                                rows.append((str(book), book_number, chapter, verse_idx, text))
                    elif isinstance(ch_entry, dict):
                        # Shape (a): an explicit {"chapter":.., "verses":[...]} dict
                        chapter = _first_key(ch_entry, CHAPTER_KEYS)
                        if chapter is None:
                            chapter = chapter_idx
                        verses = _first_key(ch_entry, ["verses"])
                        for verse_idx, v_entry in enumerate(verses, start=1):
                            if isinstance(v_entry, dict):
                                verse = _first_key(v_entry, VERSE_KEYS)
                                text = _first_key(v_entry, TEXT_KEYS)
                            else:
                                # verses list of bare strings inside a chapter dict
                                verse, text = None, v_entry
                            if verse is None:
                                verse = verse_idx
                            if book and chapter is not None and verse is not None and text:
                                rows.append((str(book), book_number, int(chapter), int(verse), text))
                    else:
                        raise ValueError(
                            f"Unrecognized chapter entry in '{book}': expected a list of verses "
                            f"or a chapter dict, got {type(ch_entry).__name__}."
                        )
        else:
            # Flat list of verse rows, whatever the exact field names are
            for entry in data:
                book, book_number = _extract_book(entry, NAME_KEYS, NUMBER_KEYS)
                chapter = _first_key(entry, CHAPTER_KEYS)
                verse = _first_key(entry, VERSE_KEYS)
                text = _first_key(entry, TEXT_KEYS)
                if book is None or chapter is None or verse is None or text is None:
                    missing = [n for n, v in [("book", book), ("chapter", chapter), ("verse", verse), ("text", text)] if v is None]
                    raise ValueError(
                        f"Row is missing {', '.join(missing)}. Fields found on that row: {list(entry.keys())}"
                    )
                rows.append((str(book), book_number, int(chapter), int(verse), text))

    elif isinstance(data, dict):
        # Nested dict: {book: {chapter: {verse: text}}}
        for book, chapters in data.items():
            book_number = BIBLE_BOOK_NUMBERS.get(book)
            for chapter, verses in chapters.items():
                for verse, text in verses.items():
                    rows.append((str(book), book_number, int(chapter), int(verse), text))
    else:
        raise ValueError("Unrecognized Bible JSON shape — see the format examples above.")

    return rows


def import_bible_json(data, translation, replace_translation=False):
    """
    Import Bible text from parsed JSON. Accepts the two documented shapes
    (nested book/chapter/verse dict, or a flat list with book/chapter/verse/text
    keys) plus several common variants used by public Bible JSON datasets
    (different key names, a wrapper object, or a nested list-of-books/chapters
    structure). When the source includes a numeric book id (most datasets do),
    it's stored too, so this translation can be paired with another one for
    bilingual/split-screen display even if the two name books differently.
    Only import text you or your church have the legal right to use — public
    domain translations (e.g. KJV, WEB, ASV) or ones you're properly licensed
    for. Returns the number of verses imported.
    """
    rows = _flatten_bible_rows(data)
    if not rows:
        raise ValueError("No verses were found in that file.")

    conn = get_conn()
    if replace_translation:
        conn.execute("DELETE FROM bible_verses WHERE translation=?", (translation,))
    conn.executemany(
        "INSERT OR REPLACE INTO bible_verses(book, chapter, verse, text, translation, book_number) VALUES (?,?,?,?,?,?)",
        [(b, c, v, t, translation, bn) for (b, bn, c, v, t) in rows]
    )
    conn.commit()
    conn.close()
    return len(rows)


# ---------------------------------------------------------------------------
# SONG IMPORT (bulk)
# ---------------------------------------------------------------------------

def import_songs_json(data):
    """
    Accepts a list of song dicts, either:
      {"title": "...", "artist": "...", "category": "...", "tags": "...", "lyrics": "verse1\\n\\nverse2"}
    or:
      {"title": "...", "artist": "...", "category": "...", "tags": "...", "slides": ["verse1", "verse2"]}
    Only import lyrics you/your church have the rights to use (see item 24 of the
    original spec) — enter manually or import from a source you're licensed for.
    Returns the number of songs imported.
    """
    conn = get_conn()
    count = 0
    for entry in data:
        title = entry.get("title", "").strip()
        if not title:
            continue
        artist = entry.get("artist", "")
        category = entry.get("category", "Worship")
        tags = entry.get("tags", "")
        if "slides" in entry and entry["slides"]:
            slides = entry["slides"]
        else:
            slides = [s.strip() for s in entry.get("lyrics", "").split("\n\n") if s.strip()] or ["(empty)"]
        conn.execute(
            "INSERT INTO songs(title, artist, category, tags, slides, favorite, last_used, updated_at) VALUES (?,?,?,?,?,0,?,?)",
            (title, artist, category, tags, json.dumps(slides), now(), now())
        )
        count += 1
    conn.commit()
    conn.close()
    return count


def import_songs_csv(rows):
    """
    Accepts an iterable of csv.DictReader rows with columns:
      title, artist, category, tags, lyrics
    Because commas/newlines are awkward in CSV, use '||' inside the `lyrics`
    column to separate slides, e.g.: "Verse one line||Verse one line two||Chorus line"
    """
    conn = get_conn()
    count = 0
    for row in rows:
        title = (row.get("title") or "").strip()
        if not title:
            continue
        artist = row.get("artist", "")
        category = row.get("category", "Worship")
        tags = row.get("tags", "")
        lyrics = row.get("lyrics", "")
        slides = [s.strip() for s in lyrics.split("||") if s.strip()] or ["(empty)"]
        conn.execute(
            "INSERT INTO songs(title, artist, category, tags, slides, favorite, last_used, updated_at) VALUES (?,?,?,?,?,0,?,?)",
            (title, artist, category, tags, json.dumps(slides), now(), now())
        )
        count += 1
    conn.commit()
    conn.close()
    return count


# ---------------------------------------------------------------------------
# STYLES
# ---------------------------------------------------------------------------

def render_status_badge(state):
    """
    Small Live / Black / Paused pill for the top of the Presentation page,
    using the semantic status colors (kept separate from ACCENT so gold
    stays reserved for brand/primary actions).
    """
    if bool(state.get("black")):
        cls, label = "ecc-status-black", "Black"
    elif bool(state.get("cleared")):
        cls, label = "ecc-status-paused", "Paused"
    else:
        cls, label = "ecc-status-live", "Live"
    render_html(f'<span class="ecc-status {cls}">{label}</span>')


def render_html(html_str: str):
    """
    Render a (possibly multi-line) raw HTML string with st.markdown.

    Python triple-quoted strings written with normal source-code indentation
    keep that indentation as literal leading whitespace on every line after
    the first. Streamlit's markdown parser treats any line indented 4+ spaces
    as a preformatted code block, so without this the HTML tags print as
    literal text instead of rendering (this is what caused the projector
    display and slide previews to show raw <div> tags). Stripping each
    line's leading whitespace before handing it to st.markdown avoids that.
    """
    st.markdown("\n".join(line.lstrip() for line in html_str.split("\n")), unsafe_allow_html=True)


def _render_splash_screen():
    """A one-time (per session) full-screen splash: a CSS-built ECC emblem
    holds for ~1.75s, then fades out over the remaining ~0.75s (2.5s total
    on-screen, once it actually appears), while the app content underneath
    fades in on a matching delay — so the reveal feels like a cross-fade
    rather than a hard cut. Pure CSS/HTML, no image asset required, so
    there's nothing external to host or break.

    This renders through components.html() (its own iframe) instead of
    render_html()/st.markdown(), and its JS immediately injects the splash
    markup into the REAL page (window.parent.document), not just its own
    iframe. That's not cosmetic — it's the actual fix for "black screen for
    5+ seconds, logo only shows up at the end": st.markdown() content has to
    wait for Streamlit to finish reconciling the ENTIRE rest of the page's
    component tree before it commits to the DOM, so on a script this size
    the splash was invisible for however long that reconciliation took.
    A components.html() iframe mounts and runs independently of its
    siblings — it doesn't wait on them — so injecting the splash from
    inside it shows the logo essentially as soon as Streamlit can render
    anything at all, instead of only once the whole page has caught up.
    The 2.5s duration here is purely the ON-SCREEN hold+fade time — it
    starts counting from whenever this component actually mounts, not from
    page load, so it doesn't include (and can't control) whatever gap
    Streamlit's own boot takes before that.
    """
    components.html(
        f"""
        <script>
        (function() {{
            const doc = window.parent.document;
            if (doc.getElementById('ecc-splash')) return;  // already injected this session

            const style = doc.createElement('style');
            style.textContent = `
                html, body {{ background:{BG} !important; }}
                @keyframes eccSplashHold {{
                    0%   {{ opacity: 1; }}
                    70%  {{ opacity: 1; }}
                    100% {{ opacity: 0; }}
                }}
                @keyframes eccLogoPulse {{
                    0%, 100% {{ transform: scale(1); opacity: 0.92; }}
                    50%      {{ transform: scale(1.05); opacity: 1; }}
                }}
                @keyframes eccLoadBar {{
                    0%   {{ width: 0%; }}
                    100% {{ width: 100%; }}
                }}
                @keyframes eccAppReveal {{
                    0%   {{ opacity: 0; }}
                    100% {{ opacity: 1; }}
                }}
                #ecc-splash {{
                    position: fixed; inset: 0; z-index: 999999;
                    background: radial-gradient(circle at 50% 40%, #17190F 0%, {BG} 72%);
                    display: flex; flex-direction: column; align-items: center; justify-content: center;
                    animation: eccSplashHold 2.5s ease forwards;
                    pointer-events: none;
                }}
                #ecc-splash .ecc-splash-badge {{
                    width: 96px; height: 96px; border-radius: 50%;
                    border: 2px solid {ACCENT}; display:flex; align-items:center; justify-content:center;
                    margin-bottom: 1.5rem; animation: eccLogoPulse 1.8s ease-in-out infinite;
                    box-shadow: 0 0 44px {ACCENT}33;
                }}
                #ecc-splash .ecc-splash-badge svg {{ width: 44px; height: 44px; }}
                #ecc-splash .ecc-splash-word {{
                    font-family: 'Inter', sans-serif; font-weight: 800; font-size: 2.1rem;
                    letter-spacing: 0.14em; color: {TEXT_PRIMARY};
                }}
                #ecc-splash .ecc-splash-word span {{ color: {ACCENT}; }}
                #ecc-splash .ecc-splash-tag {{
                    font-family: 'Inter', sans-serif; font-size: 0.76rem; letter-spacing: 0.3em;
                    text-transform: uppercase; color: {TEXT_MUTED}; margin-top: 0.55rem;
                }}
                #ecc-splash .ecc-splash-bar {{
                    width: 160px; height: 2px; background: {CARD_BORDER}; margin-top: 1.7rem;
                    border-radius: 2px; overflow: hidden;
                }}
                #ecc-splash .ecc-splash-bar-fill {{
                    height: 100%; background: {ACCENT}; animation: eccLoadBar 2.2s ease forwards;
                }}
                [data-testid="stAppViewContainer"] {{ animation: eccAppReveal 0.9s ease 1.75s both; }}
            `;
            doc.head.appendChild(style);

            const splash = doc.createElement('div');
            splash.id = 'ecc-splash';
            splash.innerHTML = `
                <div class="ecc-splash-badge">
                    <svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
                        <path d="M12 2V22M4 9H20" stroke="{ACCENT}" stroke-width="1.6" stroke-linecap="round"/>
                        <circle cx="12" cy="12" r="9.3" stroke="{ACCENT}" stroke-width="1" opacity="0.4"/>
                    </svg>
                </div>
                <div class="ecc-splash-word">ECC <span>WORSHIP</span></div>
                <div class="ecc-splash-tag">Prepare · Present · Worship</div>
                <div class="ecc-splash-bar"><div class="ecc-splash-bar-fill"></div></div>
            `;
            doc.body.appendChild(splash);

            // Belt-and-suspenders: also remove the element once its
            // animation ends, in case something re-renders the page under
            // it later in the same session and the CSS class match reruns.
            setTimeout(function() {{
                if (splash && splash.parentNode) splash.parentNode.removeChild(splash);
            }}, 2700);
        }})();
        </script>
        """,
        height=0,
    )


def inject_css():
    render_html(f"""
    <style>    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=Manrope:wght@400;500;700&display=swap');

    html, body, [class*="css"]  {{
        font-family: 'Inter', -apple-system, sans-serif;
    }}
    html, body {{ background: {BG} !important; }}
    .stApp {{
        background: {BG};
        color: {TEXT_PRIMARY};
    }}
    section[data-testid="stSidebar"] {{
        background: linear-gradient(180deg, #101116 0%, #0A0B0E 100%);
        border-right: 1px solid {CARD_BORDER};
        min-width: 290px !important;
        max-width: 290px !important;
        margin-left: 0px !important;
        transform: none !important;
        visibility: visible !important;
    }}
    /* The sidebar is meant to stay permanently open — hide every version of
       Streamlit's collapse/expand controls so there's no button to click,
       and hide the header/menu/footer chrome around it entirely. */
    #MainMenu, footer, header {{visibility: hidden; display: none;}}
    [data-testid="stSidebarCollapseButton"],
    [data-testid="collapsedControl"],
    [data-testid="stSidebarCollapsedControl"],
    button[title="Collapse sidebar"],
    button[title="Expand sidebar"] {{
        display: none !important;
        visibility: hidden !important;
    }}

    /* ---- Premium console chrome: typography, scrollbars, alerts ---- */
    h1, h2, h3 {{ letter-spacing: -0.01em; font-weight: 800 !important; color: {TEXT_PRIMARY}; }}
    h4, h5, h6 {{ letter-spacing: 0.01em; font-weight: 700 !important; color: {TEXT_PRIMARY}; }}
    p, span, label, .stMarkdown {{ color: {TEXT_PRIMARY}; }}
    [data-testid="stCaptionContainer"], .stCaption {{ color: {TEXT_MUTED} !important; }}
    ::-webkit-scrollbar {{ width: 10px; height: 10px; }}
    ::-webkit-scrollbar-track {{ background: {BG}; }}
    ::-webkit-scrollbar-thumb {{ background: {CARD_BORDER}; border-radius: 8px; }}
    ::-webkit-scrollbar-thumb:hover {{ background: {ACCENT}66; }}
    * {{ scrollbar-width: thin; scrollbar-color: {CARD_BORDER} {BG}; }}

    div[data-testid="stAlert"] {{
        background: {CARD}; border: 1px solid {CARD_BORDER}; border-radius: 12px;
        color: {TEXT_PRIMARY};
    }}

    /* Inputs — dark fields with a gold focus ring instead of Streamlit's default red */
    div[data-baseweb="input"], div[data-baseweb="textarea"], div[data-baseweb="select"] > div {{
        background: {CARD} !important; border: 1px solid {CARD_BORDER} !important; border-radius: 10px !important;
        color: {TEXT_PRIMARY} !important; transition: border-color .15s ease, box-shadow .15s ease;
    }}
    div[data-baseweb="input"]:focus-within, div[data-baseweb="textarea"]:focus-within,
    div[data-baseweb="select"]:focus-within > div {{
        border-color: {ACCENT} !important; box-shadow: 0 0 0 2px {ACCENT}22 !important;
    }}
    input, textarea {{ color: {TEXT_PRIMARY} !important; }}
    input::placeholder, textarea::placeholder {{ color: {TEXT_MUTED} !important; opacity: 0.75; }}
    div[data-baseweb="popover"] {{ background: {CARD} !important; }}
    ul[role="listbox"] {{ background: {CARD} !important; border: 1px solid {CARD_BORDER} !important; }}
    li[role="option"]:hover {{ background: {ACCENT}22 !important; }}

    /* Tabs, expanders, popovers — flatten Streamlit's default look into the console theme */
    button[data-baseweb="tab"] {{ color: {TEXT_MUTED} !important; font-weight: 600; }}
    button[data-baseweb="tab"][aria-selected="true"] {{ color: {ACCENT} !important; }}
    div[data-baseweb="tab-highlight"] {{ background-color: {ACCENT} !important; }}
    div[data-baseweb="tab-border"] {{ background-color: {CARD_BORDER} !important; }}
    details, div[data-testid="stExpander"] {{
        background: {CARD}; border: 1px solid {CARD_BORDER} !important; border-radius: 14px !important;
        overflow: hidden;
    }}
    summary {{ color: {TEXT_PRIMARY} !important; font-weight: 600; }}
    [data-testid="stPopoverBody"] {{
        background: {CARD} !important; border: 1px solid {CARD_BORDER} !important; border-radius: 14px !important;
    }}
    [data-testid="stCheckbox"] label, [data-testid="stRadio"] label {{ color: {TEXT_PRIMARY} !important; }}

    .ecc-wordmark {{
        font-weight: 800; font-size: 1.4rem; letter-spacing: 0.02em;
        color: {TEXT_PRIMARY};
    }}
    .ecc-wordmark span {{ color: {ACCENT}; }}

    .ecc-card {{
        background: linear-gradient(160deg, {CARD} 0%, #131217 100%);
        border: 1px solid {CARD_BORDER};
        border-radius: 16px;
        padding: 1.4rem 1.6rem;
        margin-bottom: 1rem;
        box-shadow: 0 6px 24px rgba(0,0,0,0.25);
        transition: all .15s ease;
    }}
    .ecc-card:hover {{ border-color: {ACCENT}55; }}
    .ecc-hero {{
        background: linear-gradient(135deg, #17140B, #0E0F13 70%);
        border: 1px solid {ACCENT}33;
        border-radius: 20px;
        padding: 2rem 2.2rem;
        margin-bottom: 1.5rem;
        box-shadow: 0 10px 36px rgba(0,0,0,0.35);
    }}
    .ecc-muted {{ color: {TEXT_MUTED}; font-size: 0.88rem; }}
    .ecc-label {{
        text-transform: uppercase; letter-spacing: .12em; font-size: 0.72rem;
        color: {ACCENT}; font-weight: 700; margin-bottom: 0.3rem;
    }}
    .ecc-pill {{
        display:inline-block; padding: 0.15rem 0.6rem; border-radius: 999px;
        background: {ACCENT}22; color: {ACCENT}; font-size: 0.72rem; font-weight:600;
        margin-right: 0.3rem;
    }}
    .stButton>button {{
        border-radius: 10px; border: 1px solid {CARD_BORDER};
        background: {CARD}; color: {TEXT_PRIMARY}; font-weight: 600;
        transition: all .12s ease; box-shadow: 0 1px 3px rgba(0,0,0,0.2);
    }}
    .stButton>button:hover {{ border-color: {ACCENT}; color: {ACCENT}; box-shadow: 0 0 0 3px {ACCENT}18; }}
    .stButton>button:active {{ transform: translateY(1px); }}
    button[kind="primary"], .ecc-primary button {{
        background: linear-gradient(160deg, {ACCENT}, #A9803A) !important; color: #1A1400 !important; border: none !important;
        font-weight: 700 !important; box-shadow: 0 4px 14px {ACCENT}33 !important;
    }}
    button[kind="primary"]:hover {{ filter: brightness(1.08); }}

    /* Destructive buttons — wrap the button in
       st.container(key="ecc-danger-...") (any key starting with
       "ecc-danger-" or "del_wrap_") so it reads distinctly from ordinary
       secondary buttons. Streamlit mirrors the container key onto the
       wrapping div as a "st-key-<key>" class. */
    div[class*="st-key-ecc-danger-"] .stButton>button,
    div[class*="st-key-del_wrap_"] .stButton>button,
    .ecc-danger .stButton>button {{
        background: {BLACK_RED}18 !important; border: 1px solid {BLACK_RED}66 !important; color: {BLACK_RED} !important;
    }}
    div[class*="st-key-ecc-danger-"] .stButton>button:hover,
    div[class*="st-key-del_wrap_"] .stButton>button:hover,
    .ecc-danger .stButton>button:hover {{
        background: {BLACK_RED}2A !important; border-color: {BLACK_RED} !important; color: #FFFFFF !important;
        box-shadow: 0 0 0 3px {BLACK_RED}22 !important;
    }}

    /* Active nav item — sidebar() adds .ecc-nav-active to the button
       wrapper for whichever label matches st.session_state.page. */
    .ecc-nav-active .stButton>button {{
        background: {ACCENT}16 !important; border-color: {ACCENT} !important; color: {ACCENT} !important;
        font-weight: 700 !important;
    }}
    section[data-testid="stSidebar"] .stMarkdown h6 {{
        letter-spacing: 0.16em; font-size: 0.68rem; color: {TEXT_MUTED} !important;
        text-transform: uppercase; margin-top: 0.6rem;
    }}

    /* Semantic status badges — Live / Black / Paused. Apply via
       <span class="ecc-status ecc-status-live">Live</span> etc. */
    .ecc-status {{
        display:inline-flex; align-items:center; gap:0.35rem; padding: 0.2rem 0.7rem;
        border-radius: 999px; font-size: 0.72rem; font-weight: 700; letter-spacing: 0.04em;
        text-transform: uppercase;
    }}
    .ecc-status::before {{ content: ""; width: 7px; height: 7px; border-radius: 50%; background: currentColor; }}
    .ecc-status-live {{ background: {LIVE_GREEN}1E; color: {LIVE_GREEN}; border: 1px solid {LIVE_GREEN}55; }}
    .ecc-status-black {{ background: {BLACK_RED}1E; color: {BLACK_RED}; border: 1px solid {BLACK_RED}55; }}
    .ecc-status-paused {{ background: {PAUSE_AMBER}1E; color: {PAUSE_AMBER}; border: 1px solid {PAUSE_AMBER}55; }}
    div[data-testid="stVerticalBlockBorderWrapper"] {{
        border-radius: 14px !important; border-color: {CARD_BORDER} !important;
        background: {CARD};
    }}
    hr {{ border-color: {CARD_BORDER}; }}
    .slide-thumb {{
        border: 1px solid {CARD_BORDER}; border-radius: 10px; padding: 0.7rem;
        background: linear-gradient(160deg, {CARD} 0%, #101116 100%); margin-bottom: 0.35rem;
        font-size: 0.82rem; color: {TEXT_MUTED};
        transition: border-color .12s ease, box-shadow .12s ease;
        position: relative; /* anchors the invisible full-card click target below */
    }}
    .slide-thumb.active {{
        border-color: {ACCENT}; color: {TEXT_PRIMARY}; background: {ACCENT}14;
        box-shadow: 0 0 0 1px {ACCENT}55, 0 4px 16px {ACCENT}22;
    }}
    .slide-thumb:hover {{ border-color: {ACCENT}99; cursor: pointer; }}
    .slide-thumb-img {{
        width: 100%; height: 100%; object-fit: cover; border-radius: 6px;
        position: absolute; inset: 0; z-index: 0;
    }}
    .slide-thumb-imgwrap {{ position: relative; width: 100%; height: 100%; border-radius: 6px; overflow: hidden; }}
    /* The whole card is the click target now — one real Streamlit button,
       stretched invisibly over the entire card via negative margin +
       absolute positioning, instead of a separate small "Select" button
       underneath. Clicking anywhere on the card (text, image, badge — all
       of it) fires this same button. */
    .ecc-grid-select {{ position: relative; margin-top: calc(-1 * var(--ecc-thumb-h, 84px) - 0.35rem); height: var(--ecc-thumb-h, 84px); margin-bottom: 0.35rem; }}
    .ecc-grid-select .stButton {{ height: 100%; }}
    .ecc-grid-select .stButton>button {{
        width: 100%; height: 100%; opacity: 0; cursor: pointer;
    }}
    </style>
    """)


def save_custom_background(uploaded_file, blur_radius=10, dim_factor=0.55):
    """
    Takes an uploaded photo, resizes it down to a sane display size, blurs
    it, and dims it — the "blurred landscape, dimmed" look — then encodes
    it as a data: URI stored directly in settings. Baking the blur into the
    image itself (rather than a CSS filter) means the blur can never
    accidentally smear the slide text on top of it, since text isn't part
    of this image at all. Returns the data URI, or None if Pillow isn't
    installed.
    """
    if not PIL_AVAILABLE:
        return None
    img = Image.open(uploaded_file).convert("RGB")
    max_w = 1600
    if img.width > max_w:
        ratio = max_w / img.width
        img = img.resize((max_w, int(img.height * ratio)))
    img = img.filter(ImageFilter.GaussianBlur(radius=blur_radius))
    img = ImageEnhance.Brightness(img).enhance(dim_factor)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=78)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{b64}"


CUSTOM_BACKGROUND_KEY = "Custom Photo (uploaded)"


def projector_css(theme_name, background_key=None, font_scale=1.0):
    t = THEMES.get(theme_name, THEMES["Modern Worship"])
    bg_size_rule, bg_anim_rule = "", ""
    if background_key == CUSTOM_BACKGROUND_KEY:
        data_uri = get_settings().get("custom_background_data")
        bg_def = {"css": f"url('{data_uri}') center/cover no-repeat"} if data_uri else None
        app_bg = bg_def["css"] if bg_def else t["bg"]
    else:
        bg_def = BACKGROUNDS.get(background_key) if background_key else None
        app_bg = bg_def["css"] if bg_def else t["bg"]
        bg_size_rule = f"background-size: {bg_def['size']};" if bg_def else ""
        bg_anim_rule = f"animation: {bg_def['anim']};" if bg_def and bg_def.get("anim") else ""
    font_scale = font_scale or 1.0
    render_html(f"""
    <style>
    html, body {{ overflow: hidden !important; height: 100vh; width: 100vw; margin:0; padding:0; }}
    #MainMenu, footer, header {{visibility: hidden;}}
    section[data-testid="stSidebar"] {{display:none;}}
    /* Lock every ancestor Streamlit wraps content in to exactly the
       viewport, not just html/body and .stApp — otherwise one of these
       (stAppViewContainer, stMain, the vertical block, or the
       block-container's own padding) ends up a hair taller than 100vh,
       which is what let the projector view scroll slightly instead of
       being truly full-screen. */
    [data-testid="stAppViewContainer"], [data-testid="stMain"],
    [data-testid="stAppViewContainer"] > .main,
    section.main, div[data-testid="stVerticalBlock"],
    div[data-testid="stAppViewBlockContainer"] {{
        height: 100vh !important; max-height: 100vh !important; width: 100vw !important;
        overflow: hidden !important; margin: 0 !important;
    }}
    .block-container {{ padding: 0 !important; margin: 0 !important; max-width: 100% !important; height: 100vh !important; overflow: hidden !important; }}
    .stApp {{ background: {app_bg}; {bg_size_rule} {bg_anim_rule} cursor: none; overflow: hidden !important; height: 100vh !important; width: 100vw !important; }}
    ::-webkit-scrollbar {{ display: none; width: 0; height: 0; }}
    * {{ scrollbar-width: none; }}
    @keyframes eccDrift {{
        0% {{ background-position: 0% 50%; }}
        50% {{ background-position: 100% 50%; }}
        100% {{ background-position: 0% 50%; }}
    }}
    @keyframes eccTwinkle {{
        0% {{ filter: brightness(0.85); }}
        100% {{ filter: brightness(1.15); }}
    }}
    @keyframes eccFadeIn {{
        0% {{ opacity: 0; }}
        100% {{ opacity: 1; }}
    }}
    .proj-wrap {{
        height: 100vh; width: 100vw; display:flex; flex-direction:column;
        align-items:center; justify-content:center; text-align:center; padding: 4vw;
        animation: eccFadeIn 0.45s ease; overflow: hidden;
    }}
    .proj-ref {{
        font-family: {t['font']}; color: {t['sub']}; letter-spacing:0.15em;
        text-transform: uppercase; font-size: clamp(1rem, 2.2vw, 2rem);
        margin-bottom: 2vh; font-weight:600;
    }}
    .proj-text {{
        font-family: {t['font']}; color: {t['fg']}; font-size: calc(clamp(2.2rem, 5.4vw, 5.5rem) * {font_scale});
        line-height: 1.35; font-weight: 700; white-space: pre-line;
        text-shadow: {"0 2px 18px rgba(0,0,0,0.55)" if bg_def else "none"};
    }}
    .proj-split {{
        height: 100vh; width: 100vw; display:flex; flex-direction:column;
        animation: eccFadeIn 0.45s ease; overflow: hidden;
    }}
    .proj-half {{
        flex: 1; display:flex; flex-direction:column; align-items:center;
        justify-content:center; text-align:center; padding: 2.5vw; overflow:hidden;
    }}
    .proj-half-top {{ border-bottom: 1px solid {t['sub']}44; }}
    .proj-text-secondary {{
        font-family: {t['font']}; color: {t['fg']}; font-size: calc(clamp(1.6rem, 4vw, 3.6rem) * {font_scale});
        line-height: 1.35; font-weight: 700; white-space: pre-line;
        text-shadow: {"0 2px 18px rgba(0,0,0,0.55)" if bg_def else "none"};
    }}
    [dir="rtl"] .proj-text, [dir="rtl"] .proj-text-secondary {{
        font-family: 'Traditional Arabic', 'Noto Naskh Arabic', 'Segoe UI', Tahoma, sans-serif;
    }}
    </style>
    """)


def _looks_arabic(text):
    """True if the text contains Arabic-script characters, so we can set text
    direction/font automatically without the operator having to configure it."""
    return any("\u0600" <= ch <= "\u06FF" for ch in (text or ""))


# ---------------------------------------------------------------------------
# PROJECTOR VIEW (opened in a second browser tab / window)
# ---------------------------------------------------------------------------

def render_projector():
    def _render_body():
        state = get_state()
        projector_css(state["theme"] or "Modern Worship", state.get("background"), state.get("font_scale") or 1.0)

        text, ref, text2 = "", None, None
        if state["cleared"] or not state["live"]:
            text = ""
        elif state["black"]:
            text = ""
        elif state.get("adhoc_active"):
            # "Present Now" from the Bible tab — bypasses the saved-service
            # lookup entirely and reads straight from the ad-hoc slide list.
            slides = json.loads(state["adhoc_slides"]) if state.get("adhoc_slides") else []
            si = state.get("adhoc_index") or 0
            if 0 <= si < len(slides):
                ref, text, text2 = slides[si]
        elif state["service_id"]:
            service = get_service(state["service_id"])
            if service:
                items = json.loads(service["items"])
                idx = state["item_index"]
                if 0 <= idx < len(items):
                    slides = item_slides(items[idx], state.get("font_scale") or 1.0)
                    si = state["slide_index"]
                    if 0 <= si < len(slides):
                        ref, text, text2 = slides[si]

        if text.startswith(IMG_SLIDE_PREFIX):
            img_src = text[len(IMG_SLIDE_PREFIX):]
            render_html(
                f"""<div style="height:100vh;width:100vw;display:flex;align-items:center;
                justify-content:center;background:#000;animation: eccFadeIn 0.45s ease;">
                <img src="{img_src}" style="max-width:100%;max-height:100%;object-fit:contain;" />
                </div>"""
            )
        elif text2:
            top_dir = "rtl" if _looks_arabic(text) else "ltr"
            bottom_dir = "rtl" if _looks_arabic(text2) else "ltr"
            render_html(
                f"""<div class="proj-split">
                <div class="proj-half proj-half-top" dir="{top_dir}">
                {f'<div class="proj-ref">{ref}</div>' if ref else ''}
                <div class="proj-text">{text}</div>
                </div>
                <div class="proj-half proj-half-bottom" dir="{bottom_dir}">
                <div class="proj-text-secondary">{text2}</div>
                </div>
                </div>"""
            )
        else:
            render_html(
                f"""<div class="proj-wrap">
                {f'<div class="proj-ref">{ref}</div>' if ref else ''}
                <div class="proj-text">{text}</div>
                </div>"""
            )

    # Auto-refreshing fragment (only this output re-renders per tick, no
    # full-page reload) needs Streamlit >= 1.33. Older installs don't have
    # st.fragment at all — calling it would throw and blank the whole page,
    # which is worse than the small extra lag, so detect it and fall back
    # to the previous sleep-and-rerun loop instead of hard-requiring it.
    if hasattr(st, "fragment"):
        st.fragment(run_every=0.35)(_render_body)()
    else:
        _render_body()
        time.sleep(1)
        st.rerun()

    # Real browser fullscreen, two ways in:
    #  1. Press "F" anywhere on this page (old docstring promised this but
    #     nothing ever listened for it — F alone does nothing by default,
    #     only F11 does natively; this wires it up).
    #  2. The FIRST click or keypress on this page fullscreens it
    #     automatically, with no need to know about "F" at all. This is the
    #     fallback for when the auto-open button's cross-window
    #     requestFullscreen() call gets silently ignored by the browser
    #     (common — it only reliably fires when called synchronously off
    #     the very gesture that opened the window, and a lot of browsers
    #     just refuse it from a different window's script no matter what).
    #     A single click anywhere the operator would naturally make once
    #     the display is up covers that gap.
    components.html(
        """
        <script>
        (function() {
            const doc = window.parent.document;
            if (doc._eccFsBound) return;
            doc._eccFsBound = true;

            function toggleFs() {
                if (!doc.fullscreenElement) {
                    doc.documentElement.requestFullscreen().catch(() => {});
                } else {
                    doc.exitFullscreen().catch(() => {});
                }
            }
            doc.addEventListener('keydown', function(e) {
                if (e.key.toLowerCase() === 'f' && !e.ctrlKey && !e.metaKey && !e.altKey) {
                    toggleFs();
                }
            });

            // One-shot: the very first click or keypress (other than the
            // "f" case above, already handled) fullscreens the display and
            // then stops listening — doesn't fight the operator if they
            // later exit fullscreen on purpose.
            function firstGestureFs(e) {
                if (e.type === 'keydown' && e.key.toLowerCase() === 'f') return;
                if (!doc.fullscreenElement) {
                    doc.documentElement.requestFullscreen().catch(() => {});
                }
                doc.removeEventListener('click', firstGestureFs);
                doc.removeEventListener('keydown', firstGestureFs);
            }
            doc.addEventListener('click', firstGestureFs);
            doc.addEventListener('keydown', firstGestureFs);
        })();
        </script>
        """,
        height=0,
    )


def _stage_slide_info(state):
    """Shared by Stage Display and the phone Remote: figures out the current
    slide and a preview of what's coming next, from whichever mode is
    active (ad-hoc Bible verse vs. a saved service).

    cur_text/cur_text2 can be an image slide — in which case the raw value
    is the \\x00IMG\\x00<data-uri> sentinel form, never meant to be printed
    as text. Every caller must check IMG_SLIDE_PREFIX before rendering, the
    same way the operator's own "Current" panel and the projector already
    do — that check used to be missing here entirely, which is what dumped
    raw base64 image data onto the phone remote and stage display as
    literal garbled text.

    nxt_label is always a short TEXT label (safe to print directly).
    nxt_img is either None (next slide is text, or there is no next slide)
    or the actual \\x00IMG\\x00<data-uri> string for the next slide's real
    image — callers that want a real thumbnail preview of the next slide
    (not just a "🖼️ Image slide" placeholder label) should check nxt_img
    the same way they check cur_text, and render it as an <img> when it's
    set. Before this, "next" only ever got a text placeholder even when the
    current slide's own image WAS rendered properly — this is what fixed
    that gap.
    """
    cur_ref, cur_text, cur_text2 = None, "", None
    nxt_label, nxt_img = "—", None
    if state.get("adhoc_active"):
        slides = json.loads(state["adhoc_slides"]) if state.get("adhoc_slides") else []
        si = state.get("adhoc_index") or 0
        if 0 <= si < len(slides):
            cur_ref, cur_text, cur_text2 = slides[si]
        if si + 1 < len(slides):
            nxt_ref_or_text = slides[si + 1][1]
            if nxt_ref_or_text.startswith(IMG_SLIDE_PREFIX):
                nxt_label, nxt_img = "Image slide", nxt_ref_or_text
            else:
                nxt_label = slides[si + 1][0] or nxt_ref_or_text[:40]
    elif state.get("service_id"):
        service = get_service(state["service_id"])
        if service:
            items = json.loads(service["items"])
            idx = state["item_index"]
            if 0 <= idx < len(items):
                slides = item_slides(items[idx], state.get("font_scale") or 1.0)
                si = state["slide_index"]
                if 0 <= si < len(slides):
                    cur_ref, cur_text, cur_text2 = slides[si]
                if si + 1 < len(slides):
                    nxt_ref_or_text = slides[si + 1][1]
                    if nxt_ref_or_text.startswith(IMG_SLIDE_PREFIX):
                        nxt_label, nxt_img = "Image slide", nxt_ref_or_text
                    else:
                        nxt_label = slides[si + 1][0] or nxt_ref_or_text[:40]
                elif idx + 1 < len(items):
                    nslides = item_slides(items[idx + 1], state.get("font_scale") or 1.0)
                    if nslides:
                        n0 = nslides[0][1]
                        if n0.startswith(IMG_SLIDE_PREFIX):
                            nxt_label, nxt_img = f"(Next) {items[idx + 1]['title']}", n0
                        else:
                            n0_preview = n0[:30] if not nslides[0][0] else nslides[0][0]
                            nxt_label = f"(Next) {items[idx + 1]['title']} — {n0_preview}"
                    else:
                        nxt_label = f"(Next) {items[idx + 1]['title']}"
    hidden = bool(state.get("black") or state.get("cleared") or not state.get("live"))
    return cur_ref, cur_text, cur_text2, nxt_label, nxt_img, hidden


def render_stage_display():
    """A separate backstage-only view (open ?display=stage on a second
    laptop/tablet) showing the current slide, what's coming up next, and a
    clock — so whoever's operating always knows what's about to happen
    without needing to peek at the projector or guess."""
    def _tick():
        state = get_state()
        cur_ref, cur_text, cur_text2, nxt_label, nxt_img, hidden = _stage_slide_info(state)
        cur_is_img = (cur_text or "").startswith(IMG_SLIDE_PREFIX)
        cur_img_src = cur_text[len(IMG_SLIDE_PREFIX):] if cur_is_img else ""
        # NOTE: render_html() goes through st.markdown(unsafe_allow_html=True),
        # which injects raw HTML but does NOT execute <script> tags — so the
        # clock script here never actually ran, no matter what it computed.
        # The div/CSS still render fine through render_html; the clock itself
        # is wired up separately below via components.html (a real iframe,
        # where scripts DO execute), which reaches into the parent document
        # to update the #ecc-stage-clock element render_html created.
        current_html = (
            f'<img src="{cur_img_src}" class="stage-current-img" />' if (cur_is_img and not hidden)
            else ("(hidden from projector)" if hidden else (cur_text or "Nothing live"))
        )
        # Same idea for "next": if the next slide is a real imported image
        # (e.g. a Google Slides PDF page), show the ACTUAL thumbnail of that
        # page instead of a generic "🖼️ Image slide" text placeholder —
        # this used to only apply to the "Now" side, leaving "Up Next"
        # showing a placeholder even for slides whose real image was
        # readily available.
        next_html = (
            f'<div class="stage-next-img-wrap"><img src="{nxt_img}" class="stage-next-img" onerror="this.replaceWith(Object.assign(document.createElement(\'div\'),{{textContent:\'(image failed to load)\',style:\'color:#B0463F;font-size:0.9rem;\'}}))" /></div>' if nxt_img
            else nxt_label
        )
        render_html(f"""
        <style>
        #MainMenu, footer, header {{visibility: hidden;}}
        section[data-testid="stSidebar"] {{display:none;}}
        .block-container {{ padding: 1.5vw 2vw !important; max-width: 100% !important; }}
        .stApp {{ background: #0B0C0F; }}
        .stage-clock {{ color: #C8A24A; font-family:'Inter',sans-serif; font-size: clamp(1.2rem,2.4vw,2.2rem);
                        font-weight:700; text-align:right; margin-bottom: 1.5vh; }}
        .stage-label {{ color:#8A8D93; font-family:'Inter',sans-serif; letter-spacing:.15em; text-transform:uppercase;
                        font-size: clamp(0.75rem,1.2vw,1rem); margin-bottom: 0.6vh; }}
        .stage-current {{ color:#FFFFFF; font-family:'Inter',sans-serif; font-weight:700;
                          font-size: clamp(1.6rem,4vw,3.4rem); line-height:1.35; white-space:pre-line;
                          padding-bottom: 3vh; border-bottom: 1px solid #24262C; margin-bottom: 3vh; }}
        .stage-current-img {{ display:block; width:100%; max-height:34vh; object-fit:contain; border-radius:8px; }}
        .stage-next {{ color:#C9CBD1; font-family:'Inter',sans-serif; font-size: clamp(1rem,2vw,1.6rem);
                      line-height:1.4; }}
        .stage-next-img-wrap {{
            width:100%; max-width:640px; border:1px solid #24262C; border-radius:10px;
            padding:0.6rem; background:#111218; box-sizing:border-box;
        }}
        .stage-next-img {{ display:block; width:100%; max-height:18vh; object-fit:contain; border-radius:6px; }}
        </style>
        <div class="stage-clock" id="ecc-stage-clock">--:--</div>
        <div class="stage-label">Now</div>
        <div class="stage-current">{current_html}</div>
        <div class="stage-label">Up Next</div>
        <div class="stage-next" style="width:100%;">{next_html}</div>
        """)
        components.html(
            """
            <script>
            (function() {
                const doc = window.parent.document;
                function eccUpdateStageClock() {
                    const el = doc.getElementById('ecc-stage-clock');
                    if (!el) return;
                    // Always show regular New Jersey / US Eastern time, regardless
                    // of what timezone the viewing device itself is set to.
                    const parts = new Date().toLocaleTimeString('en-US', {
                        timeZone: 'America/New_York', hour: 'numeric', minute: '2-digit', hour12: true
                    });
                    el.textContent = parts + ' ET';
                }
                eccUpdateStageClock();
                if (doc._eccStageClockInterval) clearInterval(doc._eccStageClockInterval);
                doc._eccStageClockInterval = setInterval(eccUpdateStageClock, 1000);
            })();
            </script>
            """,
            height=0,
        )

    if hasattr(st, "fragment"):
        st.fragment(run_every=0.5)(_tick)()
    else:
        _tick()
        time.sleep(1)
        st.rerun()


def render_remote():
    """A stripped-down mobile control view (open ?display=remote on a
    phone) — big Prev/Next/Black buttons so any volunteer can advance
    slides without needing the full operator screen. Also offers a
    full-catalog Slide Grid view (see render_remote_grid) for browsing every
    slide in the active service without needing to pick a song/verse first."""
    st.session_state.setdefault("remote_grid_mode", False)
    if st.session_state["remote_grid_mode"]:
        render_remote_grid()
        return

    state = get_state()
    cur_ref, cur_text, cur_text2, nxt_label, nxt_img, hidden = _stage_slide_info(state)

    render_html("""
    <style>
    #MainMenu, footer, header {visibility: hidden;}
    section[data-testid="stSidebar"] {display:none;}
    .block-container { padding: 3vw 4vw !important; max-width: 100% !important; }
    div[data-testid="stButton"] button { font-size: 1.3rem !important; padding: 1.2rem !important; font-weight:700 !important; }
    </style>
    """)
    render_status_badge(state)
    cur_is_img = (cur_text or "").startswith(IMG_SLIDE_PREFIX)
    if hidden:
        st.markdown("**Now:** (hidden)")
    elif cur_is_img:
        st.markdown("**Now:**")
        render_html(f'<img src="{cur_text[len(IMG_SLIDE_PREFIX):]}" style="display:block;width:100%;max-height:22vh;object-fit:contain;border-radius:8px;" />')
    else:
        st.markdown(f"**Now:** {cur_text[:80] or 'Nothing live'}")
    # Same real-thumbnail treatment for "Up Next" as "Now" above — before,
    # an imported PDF/image slide up next only ever showed a "🖼️ Image
    # slide" text placeholder, never the actual page.
    if nxt_img:
        st.caption("Up next:")
        render_html(f'<img src="{nxt_img}" style="display:block;width:100%;max-height:14vh;object-fit:contain;border-radius:6px;" onerror="this.replaceWith(Object.assign(document.createElement(\'div\'),{{textContent:\'(image failed to load)\',style:\'color:#B0463F;font-size:0.85rem;\'}}))" />')
    else:
        st.caption(f"Up next: {nxt_label}")
    st.write("")

    adhoc = bool(state.get("adhoc_active"))
    if adhoc:
        slides = json.loads(state["adhoc_slides"]) if state.get("adhoc_slides") else []
        si = state.get("adhoc_index") or 0
    else:
        service = get_service(state["service_id"]) if state.get("service_id") else None
        items = json.loads(service["items"]) if service else []
        idx = state.get("item_index") or 0
        slides = item_slides(items[idx], state.get("font_scale") or 1.0) if 0 <= idx < len(items) else []
        si = state.get("slide_index") or 0

    c1, c2 = st.columns(2)
    if c1.button("◀ PREV", use_container_width=True, key="remote_prev"):
        if si > 0:
            if adhoc:
                set_state(adhoc_index=si - 1, cleared=0)
            else:
                set_state(slide_index=si - 1, cleared=0)
            st.rerun()
        elif not adhoc and idx > 0:
            # Was on the first slide of this item — cross back into the
            # PREVIOUS item's last slide, mirroring what NEXT already does
            # going forward. Without this, PREV silently did nothing the
            # moment you crossed into a new item, which looked broken.
            prev_slides = item_slides(items[idx - 1], state.get("font_scale") or 1.0)
            set_state(item_index=idx - 1, slide_index=max(0, len(prev_slides) - 1), cleared=0)
            st.rerun()
    if c2.button("NEXT ▶", use_container_width=True, key="remote_next"):
        if si < len(slides) - 1:
            if adhoc:
                set_state(adhoc_index=si + 1, cleared=0)
            else:
                set_state(slide_index=si + 1, cleared=0)
            st.rerun()
        elif not adhoc and idx + 1 < len(items):
            set_state(item_index=idx + 1, slide_index=0, cleared=0)
            st.rerun()
    st.write("")
    is_black = bool(state.get("black"))
    black_label = "🔆 Show Display (currently Black)" if is_black else "⬛ Black Screen"
    if st.button(black_label, use_container_width=True, key="remote_black"):
        set_state(black=0 if is_black else 1)
        st.rerun()
    st.write("")
    if st.button("🎬 Slide Grid", use_container_width=True, key="remote_open_grid"):
        st.session_state["remote_grid_mode"] = True
        st.rerun()


def render_remote_grid():
    """Slide browser for the phone remote — shows the service's order of
    events (songs, Bible passages, imported slide decks, etc.) as a picker;
    tapping one filters the grid below to ONLY that item's slides, instead
    of dumping every slide from the whole service into one giant grid.
    Controls are packed tightly and horizontally at the top (Black Screen,
    Prev, Next, Back) instead of the normal remote's big stacked buttons,
    since this view is about scanning/tapping slides, not one-handed
    operation.

    Browsing an item here is separate from what's actually LIVE on the
    projector — picking "Song 2" just filters what this grid shows you;
    tapping an actual slide thumbnail is what changes the live output
    (same as it always did). This means you can look ahead at an upcoming
    song's slides without it affecting what the congregation currently
    sees."""
    state = get_state()
    render_html("""
    <style>
    #MainMenu, footer, header {visibility: hidden;}
    section[data-testid="stSidebar"] {display:none;}
    .block-container { padding: 2vw 3vw !important; max-width: 100% !important; }
    </style>
    """)

    service = get_service(state["service_id"]) if state.get("service_id") else None
    items = json.loads(service["items"]) if service else []
    adhoc = bool(state.get("adhoc_active"))

    top1, top2, top3, top4 = st.columns(4)
    with top1:
        is_black = bool(state.get("black"))
        if st.button("⬛" if not is_black else "🔆", use_container_width=True, key="rgrid_black",
                     help="Black Screen"):
            set_state(black=0 if is_black else 1)
            st.rerun()
    with top2:
        if st.button("◀", use_container_width=True, key="rgrid_prev", help="Previous slide"):
            if adhoc:
                si = state.get("adhoc_index") or 0
                if si > 0:
                    set_state(adhoc_index=si - 1, cleared=0)
            else:
                idx = state.get("item_index") or 0
                si = state.get("slide_index") or 0
                if si > 0:
                    set_state(slide_index=si - 1, cleared=0)
                elif idx > 0:
                    prev_slides = item_slides(items[idx - 1], state.get("font_scale") or 1.0)
                    set_state(item_index=idx - 1, slide_index=max(0, len(prev_slides) - 1), cleared=0)
            st.rerun()
    with top3:
        if st.button("▶", use_container_width=True, key="rgrid_next", help="Next slide"):
            if adhoc:
                adhoc_slides = json.loads(state["adhoc_slides"]) if state.get("adhoc_slides") else []
                si = state.get("adhoc_index") or 0
                if si < len(adhoc_slides) - 1:
                    set_state(adhoc_index=si + 1, cleared=0)
            else:
                idx = state.get("item_index") or 0
                si = state.get("slide_index") or 0
                cur_slides = item_slides(items[idx], state.get("font_scale") or 1.0) if 0 <= idx < len(items) else []
                if si < len(cur_slides) - 1:
                    set_state(slide_index=si + 1, cleared=0)
                elif idx + 1 < len(items):
                    set_state(item_index=idx + 1, slide_index=0, cleared=0)
            st.rerun()
    with top4:
        if st.button("✕ Back", use_container_width=True, key="rgrid_back"):
            st.session_state["remote_grid_mode"] = False
            st.rerun()

    st.write("")
    if adhoc:
        st.caption("Presenting a Bible verse directly — not part of a saved service.")
        state = get_state()
        adhoc_slides_raw = json.loads(state["adhoc_slides"]) if state.get("adhoc_slides") else []
        entries = _slide_grid_entries([], True, adhoc_slides_raw, 0, state.get("font_scale") or 1.0)
        theme = state.get("theme") or "Modern Worship"
        t = THEMES.get(theme, {})
        thumb_px = st.slider("Card size", min_value=50, max_value=160, value=st.session_state.get("rgrid_thumb_px", 84),
                              step=6, key="rgrid_thumb_px", label_visibility="collapsed")
        _render_slide_grid(entries, adhoc=True, item_index=0, slide_index=state.get("adhoc_index") or 0,
                            cols_per_row=3, compact=True, key_prefix="rgrid_", thumb_px=thumb_px,
                            theme_bg=t.get("bg"), theme_fg=t.get("fg"))
        return

    if not items:
        st.caption("No active service — nothing to browse yet.")
        return

    # Which item this phone is currently BROWSING — defaults to whatever's
    # actually live, but tapping a different item in the picker below only
    # changes what this grid shows, not what's on the projector.
    st.session_state.setdefault("rgrid_browse_idx", state.get("item_index") or 0)
    if st.session_state["rgrid_browse_idx"] >= len(items):
        st.session_state["rgrid_browse_idx"] = 0

    st.markdown("**Order of Service** — tap one to see its slides")
    icon_map = {"song": "🎵", "bible": "📖", "custom": "🖼", "announcement": "📣", "imagedeck": "🖼"}
    picker_cols = st.columns(min(4, len(items)) or 1)
    for i, it in enumerate(items):
        is_live_item = (not adhoc and i == (state.get("item_index") or 0))
        is_browsing = (i == st.session_state["rgrid_browse_idx"])
        icon = icon_map.get(it["type"], "•")
        label = f"{'● ' if is_live_item else ''}{icon} {it['title'][:14]}"
        with picker_cols[i % len(picker_cols)]:
            if st.button(label, key=f"rgrid_pick_item_{i}", use_container_width=True,
                         type="primary" if is_browsing else "secondary"):
                st.session_state["rgrid_browse_idx"] = i
                st.rerun()

    st.write("")
    state = get_state()  # re-fetch: the control row above may have just changed it
    thumb_px = st.slider("Card size", min_value=50, max_value=160, value=st.session_state.get("rgrid_thumb_px", 84),
                          step=6, key="rgrid_thumb_px", label_visibility="collapsed")
    theme = state.get("theme") or "Modern Worship"
    t = THEMES.get(theme, {})
    browse_idx = st.session_state["rgrid_browse_idx"]
    entries = _slide_grid_entries(items, False, None, browse_idx, state.get("font_scale") or 1.0, extend=False)
    _render_slide_grid(entries, adhoc=False, item_index=state.get("item_index") or 0,
                        slide_index=state.get("slide_index") or 0, cols_per_row=3,
                        compact=True, key_prefix="rgrid_", thumb_px=thumb_px,
                        theme_bg=t.get("bg"), theme_fg=t.get("fg"))


# ---------------------------------------------------------------------------
# SIDEBAR
# ---------------------------------------------------------------------------

def render_display_open_widget(compact=False):
    """
    Renders the projector-link UI: a clickable link, a copy button, and an
    "open on second screen automatically" button (Window Management API —
    Chrome/Edge only). Used in both the sidebar (full version, with the
    visible link) and inline on the Presentation page (compact — just the
    auto-open button, since the link already lives in the sidebar).
    """
    link_row = "" if compact else """
              <div style="display:flex;align-items:center;gap:0.4rem;
                          background:#15171C;color:#F4F3EF;border:1px solid #24262C;
                          border-radius:6px;padding:0.5rem 0.7rem;">
                <a id="ecc-display-link" href="#" onclick="return eccOpenDisplayLink(event)"
                   style="flex:1;color:#C8A24A;text-decoration:underline;
                          word-break:break-all;font-family:monospace;font-size:0.82rem;cursor:pointer;">
                  computing…
                </a>
                <button id="ecc-copy-btn" onclick="eccCopyDisplayUrl()"
                        style="flex-shrink:0;background:#24262C;color:#F4F3EF;border:none;
                               border-radius:4px;padding:0.3rem 0.6rem;font-size:0.75rem;
                               cursor:pointer;">Copy</button>
              </div>
              <div id="ecc-copy-msg" style="color:#9A9CA3;font-size:0.72rem;margin-top:0.2rem;"></div>
    """
    components.html(
        f"""
        <div style="font-family:'Inter',sans-serif;font-size:0.85rem;">
          {link_row}
          <button id="ecc-auto-btn" onclick="eccAutoPresent()"
                  style="width:100%;margin-top:0.4rem;background:#C8A24A;color:#0B0C0F;
                         border:none;border-radius:4px;padding:0.5rem 0.6rem;font-size:0.85rem;
                         font-weight:700;cursor:pointer;">
            ⛶ Open Presentation Display
          </button>
          <div id="ecc-auto-msg" style="color:#9A9CA3;font-size:0.72rem;margin-top:0.2rem;"></div>
        </div>
        <script>
          // NOTE: this snippet runs inside a sandboxed iframe (Streamlit's
          // components.html), so window.location here refers to the iframe
          // itself (origin "null", path "srcdoc") — not the real page.
          // window.parent.location is the actual browser tab's address.
          const linkEl = document.getElementById("ecc-display-link");
          let displayUrl = null;
          try {{
            displayUrl = window.parent.location.origin + window.parent.location.pathname + "?display=projector";
            if (linkEl) {{ linkEl.href = displayUrl; linkEl.innerText = displayUrl; }}
          }} catch (e) {{
            if (linkEl) {{
              linkEl.innerText = "Couldn't detect the URL automatically — copy it from your browser's address bar and add ?display=projector to the end.";
              linkEl.removeAttribute("href");
            }}
          }}
          function eccCopyDisplayUrl() {{
            if (!displayUrl) return;
            navigator.clipboard.writeText(displayUrl).then(() => {{
              document.getElementById("ecc-copy-msg").innerText = "Copied!";
              setTimeout(() => {{ document.getElementById("ecc-copy-msg").innerText = ""; }}, 1500);
            }});
          }}
          // The plain <a> above lives inside this sandboxed iframe, so a
          // normal target="_blank" click can get redirected into
          // navigating your CURRENT tab instead of opening a new one.
          // Calling open() on window.PARENT instead runs it in the real
          // page's own context, which reliably opens a fresh tab. The
          // empty-string target name (rather than a reused name like
          // "ecc_projector") guarantees a brand new tab every click,
          // never overwriting one that's already open.
          function eccOpenDisplayLink(e) {{
            e.preventDefault();
            if (displayUrl) window.parent.open(displayUrl, "_blank");
            return false;
          }}
          // Uses the Window Management API (Chrome/Edge only, needs HTTPS
          // or localhost). It lists connected monitors, opens the display
          // link positioned exactly on whichever one isn't this window,
          // and tries to fullscreen it — same trick apps like Canva/Slides
          // use to make "extending" feel automatic. First use will prompt
          // for a one-time permission ("Window Management" / "Manage
          // Windows"). Fullscreen-on-open isn't guaranteed by every
          // browser without an extra click in that new window — if it
          // doesn't go fullscreen by itself, press "F" once it's open.
          async function eccAutoPresent() {{
            const msg = document.getElementById("ecc-auto-msg");
            if (!displayUrl) {{ msg.innerText = "URL not ready yet."; return; }}
            if (!window.parent.getScreenDetails) {{
              msg.innerText = "Your browser doesn't support auto multi-screen (needs Chrome or Edge). Use the link above instead.";
              return;
            }}
            try {{
              const details = await window.parent.getScreenDetails();
              const current = details.currentScreen;
              const other = details.screens.find(s => s !== current) || current;
              const w = window.parent.open(
                displayUrl, "ecc_projector",
                `left=${{other.availLeft}},top=${{other.availTop}},width=${{other.availWidth}},height=${{other.availHeight}}`
              );
              if (!w) {{
                msg.innerText = "Popup was blocked — allow popups for this site and try again.";
                return;
              }}
              // A cross-window requestFullscreen() call only has a chance of
              // being honored if it runs synchronously off the same user
              // gesture that opened the window — wrapping it in setTimeout()
              // (the old code) breaks that activation chain and browsers
              // silently ignore it, which is why the display could still
              // end up not actually fullscreen. Calling it immediately here
              // is the best shot from this side. As a second line of
              // defense, the new window also arms its OWN listener (see the
              // ?display=projector page) so the very next click or keypress
              // inside that window fullscreens it too, with no extra step
              // needed beyond just clicking on the projector window once.
              try {{ w.document.documentElement.requestFullscreen(); }} catch (e) {{}}
              msg.innerText = other === current
                ? "Only one screen detected — opened here. Connect a projector/monitor first for auto-positioning."
                : "Opened on the second screen. If it isn't fullscreen, click anywhere on it once.";
            }} catch (e) {{
              // getScreenDetails was blocked — almost always because the
              // browser's "Window Management" permission was denied (or
              // never granted) for this site. Rather than just failing,
              // fall back to a plain new window so the button still does
              // something useful, and explain how to actually fix it.
              const w = window.parent.open(displayUrl, "ecc_projector");
              if (w) {{
                msg.innerText = "Multi-screen permission isn't granted, so this opened in a normal window instead — drag it to your projector manually. To enable auto-positioning: click the padlock/site-info icon in your browser's address bar → Site settings → allow \\"Window management\\" (or \\"Additional permissions\\") → reload this page and try again.";
              }} else {{
                msg.innerText = "Permission needed for auto-positioning, and the popup was also blocked. Allow popups for this site in your browser settings, or just click the link above instead.";
              }}
            }}
          }}
        </script>
        """,
        height=(115 if not compact else 65),
    )


def sidebar():
    with st.sidebar:
        st.markdown('<div class="ecc-wordmark">ECC <span>Worship</span></div>', unsafe_allow_html=True)
        st.caption("Prepare. Present. Worship.")
        current_page = st.session_state.get("page")

        def nav_button(label):
            wrapper_class = "ecc-nav-active" if label == current_page else "ecc-nav-inactive"
            st.markdown(f'<div class="{wrapper_class}">', unsafe_allow_html=True)
            clicked = st.button(label, key=f"nav_{label}", use_container_width=True)
            st.markdown('</div>', unsafe_allow_html=True)
            if clicked:
                st.session_state.page = label

        st.markdown("###### MAIN")
        for label in ["Dashboard", "Service Builder", "Presentation"]:
            nav_button(label)
        st.markdown("###### LIBRARY")
        for label in ["Song Library", "Import Slides", "Bible", "Saved Services"]:
            nav_button(label)
        st.markdown("###### SETTINGS")
        for label in ["Church Settings", "Display Settings", "Database"]:
            nav_button(label)

        st.markdown("---")
        st.caption("Projector / extended display")
        # We can't reliably ask the Streamlit server for its own public URL
        # (it may be behind a different host/port than the one the browser
        # used — e.g. deployed on Streamlit Cloud, a reverse proxy, or just a
        # different port locally). "http://localhost:8501" only ever works
        # when the projector browser is the *same machine* as this one, so
        # hardcoding it as a fallback silently gave a broken link whenever
        # that wasn't true. Instead, ask the browser itself (via JS) what
        # origin it's actually using right now, and build the link from that.
        render_display_open_widget(compact=False)
        st.caption(
            "Click the link (or copy it) and open it in a second window on your projector, "
            "then press fullscreen. If you're running this app remotely, that link already "
            "reflects the real address you're using — no need to type 'localhost'."
        )

        st.write("")
        st.caption("Stage Display / Remote (open on another device)")
        components.html(
            """
            <div style="font-family:'Inter',sans-serif;font-size:0.82rem;display:flex;flex-direction:column;gap:0.5rem;">
              <div style="display:flex;align-items:center;gap:0.4rem;">
                <a id="ecc-stage-link" href="#" onclick="return eccOpenLink(event, this)"
                   style="flex:1;color:#C8A24A;text-decoration:underline;cursor:pointer;">🖥 Stage Display (current + next slide, clock)</a>
                <button onclick="eccCopyLink('ecc-stage-link', this)"
                        style="flex-shrink:0;background:#24262C;color:#F4F3EF;border:none;
                               border-radius:4px;padding:0.3rem 0.6rem;font-size:0.72rem;cursor:pointer;">Copy</button>
              </div>
              <div style="display:flex;align-items:center;gap:0.4rem;">
                <a id="ecc-remote-link" href="#" onclick="return eccOpenLink(event, this)"
                   style="flex:1;color:#C8A24A;text-decoration:underline;cursor:pointer;">📱 Phone Remote (Next/Prev/Black)</a>
                <button onclick="eccCopyLink('ecc-remote-link', this)"
                        style="flex-shrink:0;background:#24262C;color:#F4F3EF;border:none;
                               border-radius:4px;padding:0.3rem 0.6rem;font-size:0.72rem;cursor:pointer;">Copy</button>
              </div>
            </div>
            <script>
              // Same fix as the main projector link: these anchors live in a
              // sandboxed iframe, so opening via window.PARENT (not a plain
              // target="_blank" click) is what reliably opens a fresh tab
              // instead of navigating away from the page you're on.
              try {
                const base = window.parent.location.origin + window.parent.location.pathname;
                document.getElementById("ecc-stage-link").dataset.url = base + "?display=stage";
                document.getElementById("ecc-remote-link").dataset.url = base + "?display=remote";
              } catch (e) {}
              function eccOpenLink(e, el) {
                e.preventDefault();
                if (el.dataset.url) window.parent.open(el.dataset.url, "_blank");
                return false;
              }
              function eccCopyLink(linkId, btn) {
                const el = document.getElementById(linkId);
                if (!el || !el.dataset.url) return;
                navigator.clipboard.writeText(el.dataset.url).then(() => {
                  const orig = btn.innerText;
                  btn.innerText = "Copied!";
                  setTimeout(() => { btn.innerText = orig; }, 1500);
                });
              }
            </script>
            """,
            height=80,
        )


# ---------------------------------------------------------------------------
# PAGES
# ---------------------------------------------------------------------------

def page_dashboard():
    settings = get_settings()
    st.markdown(f"### Good morning, {settings['church_name']}.")

    services = get_services()
    upcoming = services[0] if services else None

    st.markdown('<div class="ecc-hero">', unsafe_allow_html=True)
    if upcoming:
        items = json.loads(upcoming["items"])
        n_songs = sum(1 for i in items if i["type"] == "song")
        n_bible = sum(1 for i in items if i["type"] == "bible")
        st.markdown('<div class="ecc-label">Today\'s Service</div>', unsafe_allow_html=True)
        st.markdown(f"## {upcoming['name']}")
        st.markdown(
            f'<span class="ecc-muted">{upcoming["service_date"]} · {upcoming["service_time"] or "TBD"} · '
            f'{n_songs} songs prepared · {n_bible} passages prepared · '
            f'{"Ready to present" if items else "Not yet built"}</span>',
            unsafe_allow_html=True,
        )
        st.write("")
        col1, _ = st.columns([1, 3])
        with col1:
            st.markdown('<div class="ecc-primary">', unsafe_allow_html=True)
            if st.button("Open Service", use_container_width=True):
                st.session_state.active_service_id = upcoming["id"]
                st.session_state.page = "Presentation"
                st.rerun()
            st.markdown('</div>', unsafe_allow_html=True)
    else:
        st.markdown('<div class="ecc-label">Today\'s Service</div>', unsafe_allow_html=True)
        st.markdown("## No service prepared yet")
        st.caption("Create one to get started.")
    st.markdown('</div>', unsafe_allow_html=True)

    st.markdown("#### Quick Actions")
    c1, c2, c3, c4 = st.columns(4)
    if c1.button("➕ Create Service", use_container_width=True):
        st.session_state.page = "Service Builder"; st.rerun()
    if c2.button("🎵 Add Song", use_container_width=True):
        st.session_state.page = "Song Library"; st.session_state.show_add_song = True; st.rerun()
    if c3.button("📖 Find Bible Verse", use_container_width=True):
        st.session_state.page = "Bible"; st.rerun()
    if c4.button("▶ Start Presentation", use_container_width=True):
        st.session_state.page = "Presentation"; st.rerun()

    st.markdown("#### Recent Services")
    if not services:
        st.caption("Nothing here yet — your saved services will appear as cards.")
    for s in services[:5]:
        items = json.loads(s["items"])
        n_songs = sum(1 for i in items if i["type"] == "song")
        n_bible = sum(1 for i in items if i["type"] == "bible")
        with st.container(border=True):
            st.markdown(f"**{s['name']}**")
            st.markdown(f'<span class="ecc-muted">{s["service_date"]} · {n_songs} songs · {n_bible} Bible passages</span>', unsafe_allow_html=True)
            b1, b2, b3 = st.columns(3)
            if b1.button("Open", key=f"open_{s['id']}"):
                st.session_state.active_service_id = s["id"]; st.session_state.page = "Presentation"; st.rerun()
            if b2.button("Duplicate", key=f"dup_{s['id']}"):
                duplicate_service(s["id"]); st.rerun()
            if b3.button("Edit", key=f"edit_{s['id']}"):
                st.session_state.active_service_id = s["id"]; st.session_state.page = "Service Builder"; st.rerun()


def page_songs():
    st.markdown("### Songs")
    st.caption("Find, organize, and prepare worship songs for your service.")

    search = st.text_input("Search songs...", key="song_search", label_visibility="collapsed", placeholder="Search by title, artist, lyrics, or tag")
    tabs = ["All Songs", "Worship", "Praise", "Hymns", "Contemporary", "Recently Used", "Favorites"]
    cat = st.radio("Filter", tabs, horizontal=True, label_visibility="collapsed")

    with st.expander("➕ Import Songs", expanded=st.session_state.get("show_add_song", False)):
        st.caption(
            "Paste lyrics straight from a lyrics site — same importer as the Import Lyrics tab, right "
            "here so you don't have to leave this page. Slides are packed to a character budget sized "
            "for a TV screen, and a line is never cut mid-way (see Import Lyrics for the format)."
        )
        render_import_lyrics_form(key_prefix="songlib_import")

    st.write("")
    songs = get_songs(search, cat)
    dupe_ids = find_duplicate_song_ids()
    top_l, top_r = st.columns([3, 2])
    with top_r:
        if dupe_ids:
            st.markdown('<div class="ecc-danger">', unsafe_allow_html=True)
            if st.button(f"🗑 Delete Duplicates ({len(dupe_ids)})", use_container_width=True, key="delete_dupes"):
                delete_songs(dupe_ids)
                st.toast(f"Removed {len(dupe_ids)} duplicate song(s).", icon="✅")
                st.rerun()
            st.markdown('</div>', unsafe_allow_html=True)
    if not songs:
        st.caption("No songs match — try a different search or filter.")
    for s in songs:
        slides = json.loads(s["slides"])
        with st.container(border=True):
            c1, c2 = st.columns([4, 1])
            with c1:
                fav = "★ " if s["favorite"] else ""
                st.markdown(f"**{fav}{s['title']}**")
                st.markdown(f'<span class="ecc-pill">{s["category"]}</span> <span class="ecc-muted">{s["artist"]} · {len(slides)} slides</span>', unsafe_allow_html=True)
            with c2:
                if st.button("Open", key=f"opensong_{s['id']}"):
                    st.session_state.selected_song_id = s["id"]
                    st.session_state.page = "Song Workspace"
                    st.rerun()
                if st.button("☆ Favorite" if not s["favorite"] else "★ Unfavorite", key=f"fav_{s['id']}"):
                    toggle_favorite(s["id"]); st.rerun()
                if st.button("➕ Add to Service", key=f"addtoservice_{s['id']}"):
                    sid = ensure_active_service()
                    if not sid:
                        st.warning("No active service yet — create one in Service Builder first.")
                    else:
                        service = get_service(sid)
                        service_items = json.loads(service["items"])
                        service_items.append(make_song_item(s))
                        update_service_items(sid, service_items)
                        st.toast(f"Added '{s['title']}' to the current service.", icon="✅")
                with st.container(key=f"del_wrap_song_{s['id']}"):
                    if st.button("🗑 Delete", key=f"delsong_{s['id']}", use_container_width=True):
                        delete_song(s["id"])
                        st.toast(f"Deleted \"{s['title']}\".", icon="✅")
                        st.rerun()


def page_song_workspace():
    song_id = st.session_state.get("selected_song_id")
    song = get_song(song_id) if song_id else None
    if not song:
        st.warning("No song selected."); return
    slides = json.loads(song["slides"])

    st.markdown(f"### {song['title']}")
    st.caption(f"{song['artist']} · {song['category']}")

    if st.button("← Back to Songs"):
        st.session_state.page = "Song Library"; st.rerun()

    left, center, right = st.columns([1.1, 2.4, 1])

    with left:
        st.markdown("**Slides**")
        sel = st.session_state.get(f"slide_sel_{song_id}", 0)
        for i, sl in enumerate(slides):
            active = " active" if i == sel else ""
            preview = sl.split("\n")[0][:40]
            if st.button(f"{i+1}. {preview}", key=f"thumb_{song_id}_{i}", use_container_width=True):
                st.session_state[f"slide_sel_{song_id}"] = i
                st.rerun()
        c1, c2 = st.columns(2)
        if c1.button("➕ Add Slide"):
            slides.append("New slide")
            update_song_slides(song_id, slides); st.rerun()
        if c2.button("⧉ Duplicate") and slides:
            slides.insert(sel + 1, slides[sel])
            update_song_slides(song_id, slides); st.rerun()
        st.markdown('<div class="ecc-danger">', unsafe_allow_html=True)
        if st.button("🗑 Delete Slide") and len(slides) > 1:
            slides.pop(sel)
            update_song_slides(song_id, max(0, min(sel, len(slides)-1)) and slides or slides)
            st.rerun()
        st.markdown('</div>', unsafe_allow_html=True)
        c3, c4 = st.columns(2)
        if c3.button("↑ Move Up") and sel > 0:
            slides[sel-1], slides[sel] = slides[sel], slides[sel-1]
            st.session_state[f"slide_sel_{song_id}"] = sel - 1
            update_song_slides(song_id, slides); st.rerun()
        if c4.button("↓ Move Down") and sel < len(slides) - 1:
            slides[sel+1], slides[sel] = slides[sel], slides[sel+1]
            st.session_state[f"slide_sel_{song_id}"] = sel + 1
            update_song_slides(song_id, slides); st.rerun()

    sel = st.session_state.get(f"slide_sel_{song_id}", 0)
    sel = max(0, min(sel, len(slides) - 1))

    with center:
        theme = st.session_state.get("preview_theme", "Modern Worship")
        t = THEMES[theme]
        st.markdown("**Preview (what the projector will show)**")
        render_html(
            f"""<div style="background:{t['bg']};border-radius:16px;padding:3rem 2rem;
            min-height:320px;display:flex;align-items:center;justify-content:center;
            text-align:center;border:1px solid {CARD_BORDER};">
            <div style="color:{t['fg']};font-family:{t['font']};font-size:1.7rem;
            font-weight:700;white-space:pre-line;line-height:1.4;">{slides[sel]}</div>
            </div>"""
        )
        edited = st.text_area("Edit this slide", value=slides[sel], height=120, key=f"edit_{song_id}_{sel}")
        if edited != slides[sel]:
            slides[sel] = edited
            update_song_slides(song_id, slides)

    with right:
        st.markdown("**Theme**")
        st.selectbox("Theme", list(THEMES.keys()), key="preview_theme", label_visibility="collapsed")
        st.markdown("**Presentation Controls**")
        st.caption(f"Slide {sel+1} / {len(slides)}")
        cp, cn = st.columns(2)
        if cp.button("◀ Prev") and sel > 0:
            st.session_state[f"slide_sel_{song_id}"] = sel - 1; st.rerun()
        if cn.button("Next ▶") and sel < len(slides) - 1:
            st.session_state[f"slide_sel_{song_id}"] = sel + 1; st.rerun()
        st.write("")
        if st.button("🔴 Present This Song", use_container_width=True):
            # quick-present outside of a full service
            temp_service_id = st.session_state.get("_quickpresent_id")
            item = make_song_item(song)
            if not temp_service_id:
                temp_service_id = create_service("Quick Present", str(datetime.date.today()), "", [item])
                st.session_state["_quickpresent_id"] = temp_service_id
            else:
                update_service_items(temp_service_id, [item])
            set_state(service_id=temp_service_id, item_index=0, slide_index=sel, black=0, cleared=0, live=1, theme=theme)
            st.session_state.active_service_id = temp_service_id
            st.session_state.page = "Presentation"
            st.rerun()


def page_bible():
    st.markdown("### Bible")

    translations = get_bible_translations()
    top1, top2 = st.columns([1, 1])
    with top1:
        translation = st.selectbox("Translation", translations, key="bible_translation")
    with top2:
        bilingual = st.checkbox("Bilingual (split screen)", key="bible_bilingual",
                                 disabled=len(translations) < 2,
                                 help="Shows a second translation stacked underneath the first on the projector — e.g. Arabic on top, English on the bottom.")
    secondary_translation = None
    if bilingual:
        other_options = [t for t in translations if t != translation] or translations
        secondary_translation = st.selectbox("Second translation (shown on the bottom half)", other_options, key="bible_secondary_translation")
    st.caption(f"Browsing {translation}" + (f" · paired with {secondary_translation}" if secondary_translation else "") +
               ". Import more (public-domain or licensed) in Church Settings.")

    with st.form("bible_jump_form"):
        jc1, jc2 = st.columns([4, 1])
        jump_text = jc1.text_input("Quick jump", placeholder='e.g. "John 3:16" or "Genesis 1:1-3"',
                                    label_visibility="collapsed")
        jump_go = jc2.form_submit_button("Go →", use_container_width=True)
    if jump_go and jump_text.strip():
        parsed = parse_bible_reference(jump_text, translation)
        if parsed:
            jb, jc, jverses = parsed
            st.session_state.bible_book = jb
            st.session_state.bible_chapter = jc
            st.session_state.bible_nav_key = (jb, jc, translation)
            st.session_state.bible_selected_verses = jverses
            st.rerun()
        else:
            st.warning(f"Couldn't find \"{jump_text}\" — try a format like \"Book Chapter:Verse\".")

    books = get_bible_books(translation)
    if not books:
        st.warning("No Bible text loaded for this translation yet.")
        return
    left, center, right = st.columns([1, 1.3, 1.3])

    with left:
        st.markdown("**Books**")
        book = st.radio("Books", books, label_visibility="collapsed", key="bible_book")

    with center:
        chapters = get_bible_chapters(book, translation)
        st.markdown("**Chapter**")
        chapter = st.selectbox("Chapter", chapters, key="bible_chapter", label_visibility="collapsed")
        verses = get_bible_verses(book, chapter, translation)

        # If book/chapter/translation changed since the selection was made,
        # old verse numbers might not exist in this new set at all — that
        # mismatch is what caused the KeyError crash. Clear the stale
        # selection instead of trying to carry it across.
        nav_key = (book, chapter, translation)
        if st.session_state.get("bible_nav_key") != nav_key:
            st.session_state.bible_nav_key = nav_key
            st.session_state.bible_selected_verses = []

        st.markdown("**Verses**")
        chosen = st.session_state.setdefault("bible_selected_verses", [])
        for vnum, text in verses.items():
            checked = vnum in chosen
            vcol, pcol = st.columns([0.87, 0.13])
            with vcol:
                if st.checkbox(f"{vnum}. {text}", value=checked, key=f"v_{book}_{chapter}_{vnum}"):
                    if vnum not in chosen:
                        chosen.append(vnum)
                else:
                    if vnum in chosen:
                        chosen.remove(vnum)
            with pcol:
                # One click, no service needed — builds the same slide shape
                # a service item would use and pushes it straight to the
                # projector via present_adhoc_now().
                if st.button("▶", key=f"present_{book}_{chapter}_{vnum}",
                             help=f"Present {book} {chapter}:{vnum} now"):
                    item = make_bible_item(book, chapter, [vnum], translation, secondary_translation)
                    present_adhoc_now(item_slides(item))
                    st.toast(f"Presenting {book} {chapter}:{vnum}")
                    st.rerun()

    with right:
        st.markdown("**Selected**")
        # Only keep verse numbers that actually exist in the currently
        # displayed chapter — belt-and-suspenders alongside the nav_key
        # reset above, in case selection state gets out of sync some other way.
        chosen_sorted = sorted(v for v in st.session_state.get("bible_selected_verses", []) if v in verses)
        book_number = get_book_number(book, translation) if secondary_translation else None
        if chosen_sorted:
            for v in chosen_sorted:
                st.markdown(f"**{book} {chapter}:{v}**")
                st.caption(verses.get(v, ""))
                if secondary_translation:
                    st.caption("↳ " + get_verse_in_translation(book, chapter, v, secondary_translation, book_number))
        else:
            st.caption("Select verses on the left.")

        combine = st.checkbox(
            "Combine into one slide", value=True, key="bible_combine",
            help="Selected verses show together on a single slide, referenced as e.g. \"Genesis 1:1-3\", "
                 "instead of one slide per verse."
        )

        st.write("")
        scol1, scol2 = st.columns(2)
        with scol1:
            if st.button("▶ Present Now", disabled=not chosen_sorted, use_container_width=True,
                         help="Show these verses on the projector immediately — no service needed."):
                item = make_bible_item(book, chapter, chosen_sorted, translation, secondary_translation, combine=combine)
                present_adhoc_now(item_slides(item))
                label = f"{book} {chapter}:{chosen_sorted[0]}" + (f"-{chosen_sorted[-1]}" if len(chosen_sorted) > 1 else "")
                st.session_state.bible_selected_verses = []
                st.toast(f"Presenting {label}")
                st.rerun()
        with scol2:
            if st.button("+ Add to Service", disabled=not chosen_sorted, use_container_width=True):
                st.session_state.setdefault("bible_staging", [])
                st.session_state.bible_staging.append((book, chapter, tuple(chosen_sorted), translation, secondary_translation, combine))
                st.session_state.bible_selected_verses = []
                st.toast("Added to staging — attach it in Service Builder.", icon="✅")
                st.rerun()

        staging = st.session_state.get("bible_staging", [])
        if staging:
            st.markdown("**Staged passages**")
            for i, (b, c, vs, tr, tr2, cmb) in enumerate(staging):
                label = f"{b} {c}:{vs[0]}" + (f"-{vs[-1]}" if len(vs) > 1 else "") + f" ({tr}" + (f" + {tr2})" if tr2 else ")") + (" [combined]" if cmb else "")
                st.caption(label)
            if st.button("Go to Service Builder →"):
                st.session_state.page = "Service Builder"; st.rerun()


def ensure_active_service():
    if not st.session_state.get("active_service_id"):
        services = get_services()
        if services:
            st.session_state.active_service_id = services[0]["id"]
    return st.session_state.get("active_service_id")


def page_service_builder():
    st.markdown("### Service Builder")
    services = get_services()

    top1, top2, top3 = st.columns([2, 1, 1])
    with top1:
        options = {s["id"]: f"{s['name']} — {s['service_date']}" for s in services}
        current = st.session_state.get("active_service_id")
        if options:
            chosen = st.selectbox("Active service", list(options.keys()),
                                   format_func=lambda i: options[i],
                                   index=list(options.keys()).index(current) if current in options else 0)
            st.session_state.active_service_id = chosen
    with top2:
        if st.button("➕ New Service", use_container_width=True):
            sid = create_service("New Service", str(datetime.date.today()), "10:00 AM")
            st.session_state.active_service_id = sid
            st.rerun()
    with top3:
        templates = get_conn().execute("SELECT * FROM templates").fetchall()
        tnames = {t["id"]: t["name"] for t in templates}
        if tnames and st.button("From Template", use_container_width=True):
            t = templates[0]
            structure = json.loads(t["structure"])
            items = []
            for step in structure:
                if step == "Song":
                    items.append(make_announcement_item("(choose a song)"))
                elif step == "Scripture Reading":
                    items.append(make_announcement_item("(choose a passage)"))
                else:
                    items.append(make_custom_item(step, step))
            sid = create_service(t["name"], str(datetime.date.today()), "10:00 AM", items)
            st.session_state.active_service_id = sid
            st.rerun()

    sid = ensure_active_service()
    if not sid:
        st.info("Create a service to begin building today's plan.")
        return

    service = get_service(sid)
    items = json.loads(service["items"])

    with st.form("service_meta"):
        c1, c2, c3 = st.columns(3)
        name = c1.text_input("Service name", value=service["name"])
        date = c2.text_input("Date", value=service["service_date"])
        time_ = c3.text_input("Time", value=service["service_time"] or "")
        if st.form_submit_button("Save details"):
            conn = get_conn()
            conn.execute("UPDATE services SET name=?, service_date=?, service_time=?, updated_at=? WHERE id=?", (name, date, time_, now(), sid))
            conn.commit(); conn.close()
            st.rerun()

    if turso_configured():
        if st.button("☁️ Save Service to Cloud", help="Pushes this service to Turso so it survives an app restart — only runs when you click it."):
            try:
                turso_push_service(sid, name, date, time_, json.dumps(items))
                st.toast(f"Saved \"{name}\" to Turso.", icon="☁️")
            except Exception as e:
                st.error(f"Cloud save failed: {e}")
    else:
        st.caption("Set up Turso (Church Settings) to make saved services survive an app restart.")

    st.markdown("#### Add to the service")
    a1, a2, a3, a4 = st.columns(4)
    with a1.popover("🎵 Add Song"):
        song_search = st.text_input("Search songs", key="add_song_search", placeholder="Search by title, artist, or lyrics")
        song_options = {s["id"]: dict(s) for s in get_songs(search=song_search)}
        if song_options:
            pick_id = st.selectbox(
                "Song", list(song_options.keys()),
                format_func=lambda i: f"{song_options[i]['title']} — {song_options[i]['artist']}"
                if song_options[i]['artist'] else song_options[i]['title'],
                key="pick_song")
            picked = song_options[pick_id]
            if picked.get("artist"):
                st.caption(f"by {picked['artist']}")
            if st.button("Add", key="add_song_btn", use_container_width=True):
                items.append(make_song_item(picked))
                update_service_items(sid, items)
                st.toast(f"Added \"{picked['title']}\" — pick another or close when done.", icon="✅")
                st.rerun()
        elif song_search:
            st.caption("No songs match that search.")
        else:
            st.caption("No songs yet — add one from the Songs tab.")
    with a2:
        staging = st.session_state.get("bible_staging", [])
        if st.button(f"📖 Add Staged Bible ({len(staging)})", disabled=not staging, use_container_width=True):
            for (b, c, vs, tr, tr2, cmb) in staging:
                items.append(make_bible_item(b, c, list(vs), tr, tr2, combine=cmb))
            st.session_state.bible_staging = []
            update_service_items(sid, items); st.rerun()
    with a3.popover("📣 Add Announcement"):
        title = st.text_input("Title", key="ann_title")
        if st.button("Add", key="add_ann_btn") and title:
            items.append(make_announcement_item(title))
            update_service_items(sid, items); st.rerun()
    with a4.popover("🖼 Add Custom Slide"):
        title = st.text_input("Title", key="cust_title")
        body = st.text_area("Text", key="cust_body")
        if st.button("Add", key="add_cust_btn") and title:
            items.append(make_custom_item(title, body))
            update_service_items(sid, items); st.rerun()

    st.markdown("#### Order of Service")
    if not items:
        st.caption("Nothing added yet — build the flow above.")
    else:
        st.caption("Use ↑/↓ to reorder — instant, reliable, no dragging required.")
        with st.container(key="service_items"):
            for i, item in enumerate(items):
                icon = {"song": "🎵", "bible": "📖", "custom": "🖼", "announcement": "📣", "imagedeck": "🖼"}.get(item["type"], "•")
                with st.container(border=True):
                    c1, c2 = st.columns([5, 2])
                    c1.markdown(f"**{i+1:02d} — {icon} {item['title']}**")
                    with c2:
                        b1, b2, b3 = st.columns(3)
                        if b1.button("↑", key=f"up_{i}") and i > 0:
                            items[i-1], items[i] = items[i], items[i-1]
                            update_service_items(sid, items); st.rerun()
                        if b2.button("↓", key=f"down_{i}") and i < len(items) - 1:
                            items[i+1], items[i] = items[i], items[i+1]
                            update_service_items(sid, items); st.rerun()
                        with b3.container(key=f"del_wrap_{i}"):
                            if st.button("🗑", key=f"del_{i}"):
                                items.pop(i)
                                update_service_items(sid, items); st.rerun()

    if items:
        st.write("")
        st.markdown('<div class="ecc-primary">', unsafe_allow_html=True)
        if st.button("▶ START SERVICE", use_container_width=True):
            set_state(service_id=sid, item_index=0, slide_index=0, black=0, cleared=0, live=1,
                      theme=get_settings()["default_theme"], background=get_settings().get("default_background"))
            st.session_state.page = "Presentation"
            st.rerun()
        st.markdown('</div>', unsafe_allow_html=True)


def _render_service_order_panel(items, state, sid, adhoc, key_prefix=""):
    """The 'Service Order' list — factored out so it can be reused unchanged
    in both the normal Presentation layout and the full-screen Slide Grid
    layout (which keeps this exact panel on the left)."""
    st.markdown("**Service Order**")
    st.caption("Use ▲/▼ to reorder — the change applies immediately.")
    if adhoc:
        st.caption("Paused while presenting a verse directly.")
        if st.button("↩ Return to service", use_container_width=True, disabled=not sid, key=f"{key_prefix}return_adhoc"):
            exit_adhoc_present()
            st.rerun()
    for i, item in enumerate(items):
        icon = {"song": "🎵", "bible": "📖", "custom": "🖼", "announcement": "📣", "imagedeck": "🖼"}.get(item["type"], "•")
        is_current = (not adhoc and i == state["item_index"])
        row_del, row_go, row_up, row_down = st.columns([1, 4, 1, 1])
        with row_del.container(key=f"{key_prefix}del_wrap_order_{i}"):
            if st.button("🗑", key=f"{key_prefix}pres_del_{i}", use_container_width=True):
                items.pop(i)
                update_service_items(sid, items)
                if not adhoc:
                    # Keep pointing at the same logical item after the list
                    # shifts — same idea as the up/down handlers below.
                    if state["item_index"] > i:
                        set_state(item_index=state["item_index"] - 1)
                    elif state["item_index"] == i:
                        set_state(item_index=min(i, len(items) - 1) if items else 0, slide_index=0, cleared=1)
                st.rerun()
        label = f"{'▶ ' if is_current else ''}{i+1:02d} {icon} {item['title']}"
        if row_go.button(label, key=f"{key_prefix}go_{i}", use_container_width=True,
                          type="primary" if is_current else "secondary"):
            set_state(item_index=i, slide_index=0, cleared=0, black=0, adhoc_active=0)
            st.rerun()
        if row_up.button("▲", key=f"{key_prefix}pres_up_{i}", use_container_width=True, disabled=(i == 0)):
            items[i - 1], items[i] = items[i], items[i - 1]
            update_service_items(sid, items)
            if not adhoc:
                if state["item_index"] == i:
                    set_state(item_index=i - 1)
                elif state["item_index"] == i - 1:
                    set_state(item_index=i)
            st.rerun()
        if row_down.button("▼", key=f"{key_prefix}pres_down_{i}", use_container_width=True, disabled=(i == len(items) - 1)):
            items[i + 1], items[i] = items[i], items[i + 1]
            update_service_items(sid, items)
            if not adhoc:
                if state["item_index"] == i:
                    set_state(item_index=i + 1)
                elif state["item_index"] == i + 1:
                    set_state(item_index=i)
            st.rerun()


def _slide_grid_entries_all(items, font_scale, cap=120):
    """Like _slide_grid_entries, but flattens EVERY item in the service into
    one grid regardless of which one is currently selected — used by the
    phone remote's full-catalog slide browser (#10), where there's no
    "current song" to scope from; the whole service's slides are the point."""
    entries = []
    for ix, it in enumerate(items):
        for j, (ref, text, text2) in enumerate(item_slides(it, font_scale)):
            entries.append({"item_idx": ix, "slide_idx": j, "ref": ref, "text": text, "text2": text2,
                             "item_title": it["title"]})
            if len(entries) >= cap:
                return entries
    return entries


def _slide_grid_entries(items, adhoc, adhoc_slides, item_index, font_scale, extend=False, min_count=15, cap=60):
    """Builds the flat list of slides to show in the ProPresenter-style
    grid. Normally just the CURRENT item's own slides, in order. When
    extend=True (full-screen mode), keeps pulling in whole subsequent
    service items — never a partial item — until at least min_count slides
    are queued up (capped at `cap` so a huge service doesn't render
    thousands of thumbnails at once)."""
    entries = []
    if adhoc:
        for i, s in enumerate(adhoc_slides or []):
            ref, text, text2 = s[0], s[1], s[2] if len(s) > 2 else None
            entries.append({"item_idx": None, "slide_idx": i, "ref": ref, "text": text, "text2": text2,
                             "item_title": "Verse"})
        return entries

    if not items or not (0 <= item_index < len(items)):
        return entries

    def add_item(ix):
        it = items[ix]
        for j, (ref, text, text2) in enumerate(item_slides(it, font_scale)):
            entries.append({"item_idx": ix, "slide_idx": j, "ref": ref, "text": text, "text2": text2,
                             "item_title": it["title"]})

    add_item(item_index)
    if extend:
        nxt = item_index + 1
        while len(entries) < min_count and nxt < len(items) and len(entries) < cap:
            add_item(nxt)
            nxt += 1
    return entries


def _render_slide_grid(entries, adhoc, item_index, slide_index, cols_per_row=4, compact=False,
                        key_prefix="grid", thumb_px=None, theme_bg=None, theme_fg=None):
    """The ProPresenter-style thumbnail grid itself. Each cell is a visual
    'slide' card — a real rendered image for imported PDF/image slides, or
    the live theme's background + text color for lyric/verse slides, so the
    grid actually looks like the projector instead of generic dark boxes.
    Clicking anywhere on the card selects it (a real Streamlit button is
    stretched invisibly over the whole card via CSS — see .ecc-grid-select
    — rather than a separate small "Select" button underneath it). The
    currently-live slide gets a gold highlighted border via .slide-thumb.active
    so it's obvious at a glance which slide is on the projector right now.

    thumb_px overrides the card height in pixels (from the size slider on
    the caller's page); theme_bg/theme_fg are the live theme's CSS
    background and text color, used behind text-only slides so the grid
    matches the actual presentation display instead of always showing flat
    dark cards regardless of the live background/theme."""
    if not entries:
        st.caption("No slides to show yet — pick a song or Bible passage to see its slides here.")
        return
    thumb_h_px = thumb_px or (64 if compact else 84)
    thumb_h = f"{thumb_h_px}px"
    card_bg = theme_bg or None
    card_fg = theme_fg or None
    idx = 0
    while idx < len(entries):
        row = entries[idx:idx + cols_per_row]
        cols = st.columns(cols_per_row)
        for col, entry in zip(cols, row):
            n = idx + row.index(entry) + 1
            is_active = (
                (adhoc and entry["item_idx"] is None and entry["slide_idx"] == slide_index)
                or (not adhoc and entry["item_idx"] == item_index and entry["slide_idx"] == slide_index)
            )
            text = entry["text"] or ""
            is_img = text.startswith(IMG_SLIDE_PREFIX)
            badge = entry["ref"] or entry["item_title"]
            active_class = "slide-thumb active" if is_active else "slide-thumb"
            with col:
                if is_img:
                    img_src = text[len(IMG_SLIDE_PREFIX):]
                    render_html(
                        f'''<div class="{active_class}" style="min-height:{thumb_h};height:{thumb_h};padding:0;overflow:hidden;">
                        <div class="slide-thumb-imgwrap">
                        <img class="slide-thumb-img" src="{img_src}" />
                        <div style="position:absolute;top:0;left:0;right:0;z-index:1;background:linear-gradient(180deg,rgba(0,0,0,0.65),transparent);
                        font-size:0.62rem;text-transform:uppercase;letter-spacing:.06em;color:#fff;padding:0.3rem 0.4rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">
                        {"● LIVE · " if is_active else ""}{n:02d} · {badge}</div>
                        </div>
                        </div>'''
                    )
                else:
                    preview_txt = text.replace("\n", "  ·  ")
                    if len(preview_txt) > (56 if compact else 78):
                        preview_txt = preview_txt[:(56 if compact else 78)] + "…"
                    if not preview_txt.strip():
                        preview_txt = "(blank)"
                    bg_style = f"background:{card_bg};" if card_bg and not is_active else ""
                    fg_style = f"color:{card_fg};" if card_fg and not is_active else ""
                    render_html(
                        f'''<div class="{active_class}" style="min-height:{thumb_h};height:{thumb_h};{bg_style}display:flex;flex-direction:column;justify-content:space-between;">
                        <div style="font-size:0.66rem;text-transform:uppercase;letter-spacing:.06em;opacity:0.8;margin-bottom:0.3rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;{fg_style}">
                        {"● LIVE · " if is_active else ""}{n:02d} · {badge}</div>
                        <div style="font-size:{'0.72rem' if compact else '0.78rem'};line-height:1.3;{fg_style}">{preview_txt}</div>
                        </div>'''
                    )
                st.markdown(f'<div class="ecc-grid-select" style="--ecc-thumb-h:{thumb_h};">', unsafe_allow_html=True)
                if st.button("select", key=f"{key_prefix}sel_{entry['item_idx']}_{entry['slide_idx']}_{idx}",
                             use_container_width=True, disabled=is_active):
                    if adhoc:
                        set_state(adhoc_index=entry["slide_idx"], cleared=0)
                    else:
                        set_state(item_index=entry["item_idx"], slide_index=entry["slide_idx"], cleared=0, black=0)
                    st.rerun()
                st.markdown('</div>', unsafe_allow_html=True)
        idx += cols_per_row


def _render_operator_keyboard_shortcuts():
    st.caption("⌨️ Shortcuts: Space, → or ↑ = Next · ← or ↓ = Prev · B = toggle Black screen")
    components.html(
        """
        <script>
        (function() {
            const doc = window.parent.document;
            if (doc._eccOperatorKeysBound) return;
            doc._eccOperatorKeysBound = true;
            // Matches by a stable SUBSTRING rather than the full label,
            // since the Black button's text now toggles between "Black
            // Screen" and "Show Display" depending on state — an exact
            // match would silently stop working the moment it toggled.
            function eccClickButtonContaining(substr) {
                const buttons = doc.querySelectorAll('button');
                for (const b of buttons) {
                    if (b.innerText.trim().toLowerCase().includes(substr.toLowerCase())) { b.click(); return true; }
                }
                return false;
            }
            doc.addEventListener('keydown', function(e) {
                const tag = (doc.activeElement && doc.activeElement.tagName) || '';
                if (tag === 'INPUT' || tag === 'TEXTAREA') return;  // don't hijack typing
                if (e.ctrlKey || e.metaKey || e.altKey) return;
                const k = e.key;
                if (k === 'ArrowRight' || k === 'ArrowUp' || k === ' ') {
                    e.preventDefault();
                    eccClickButtonContaining('NEXT');
                } else if (k === 'ArrowLeft' || k === 'ArrowDown') {
                    e.preventDefault();
                    eccClickButtonContaining('PREV');
                } else if (k.toLowerCase() === 'b') {
                    e.preventDefault();
                    eccClickButtonContaining('Black');
                }
            });
        })();
        </script>
        """,
        height=0,
    )


def page_presentation():
    adhoc = bool(get_state().get("adhoc_active"))
    sid = st.session_state.get("active_service_id") or ensure_active_service()
    if not sid and not adhoc:
        st.info("No active service. Build one in Service Builder first — or present a Bible verse directly from the Bible tab.")
        return

    service = get_service(sid) if sid else None
    items = json.loads(service["items"]) if service else []
    state = get_state()
    if service and state["service_id"] != sid and not adhoc:
        set_state(service_id=sid, item_index=0, slide_index=0, black=0, cleared=1, live=1)
        state = get_state()

    title_col, status_col = st.columns([4, 1])
    with title_col:
        if adhoc:
            st.markdown("### Presenting a verse")
            st.caption("Presented directly from the Bible tab — not part of a saved service.")
        elif service:
            st.markdown(f"### {service['name']}")
            st.caption(f"{service['service_date']} · {service['service_time'] or ''}")
    with status_col:
        render_status_badge(state)

    # Shared slide-position math — needed by both the normal layout and the
    # full-screen grid layout, so it's computed once up front rather than
    # duplicated in each branch.
    if adhoc:
        adhoc_slides = json.loads(state["adhoc_slides"]) if state.get("adhoc_slides") else []
        adhoc_index = max(0, min(state.get("adhoc_index") or 0, max(len(adhoc_slides) - 1, 0)))
        slides = adhoc_slides
        slide_index = adhoc_index
        item_index, item = 0, None
    else:
        item_index = state["item_index"]
        item = items[item_index] if 0 <= item_index < len(items) else None
        slides = item_slides(item, state.get("font_scale") or 1.0) if item else []
        slide_index = max(0, min(state["slide_index"], max(len(slides) - 1, 0)))

    st.session_state.setdefault("presentation_grid_fullscreen", False)

    # ---------------------------------------------------------------
    # FULL-SCREEN SLIDE GRID — Service Order stays on the left; the whole
    # right half becomes a dense ProPresenter-style grid sized to fit at
    # least the next 15 upcoming slides (spilling into following service
    # items, never cutting one in half, when the current item runs short).
    # ---------------------------------------------------------------
    if st.session_state["presentation_grid_fullscreen"]:
        fs_left, fs_grid = st.columns([1.1, 3.3])
        with fs_left:
            _render_service_order_panel(items, state, sid, adhoc, key_prefix="fs_")
        with fs_grid:
            top_l, top_r = st.columns([4, 1.3])
            with top_l:
                st.markdown("**🎬 Slide Grid — Full Screen**")
                if slides:
                    live_ref, live_text, _ = slides[slide_index]
                    live_label = live_ref or (live_text[:60] + "…" if len(live_text) > 60 else live_text)
                else:
                    live_label = "Nothing selected"
                st.caption(f"● Live: {live_label}")
            with top_r:
                if st.button("✕ Exit Full Screen", use_container_width=True, key="grid_exit_fs"):
                    st.session_state["presentation_grid_fullscreen"] = False
                    st.rerun()
            fs_thumb_px = st.slider("Card size", min_value=60, max_value=200,
                                     value=st.session_state.get("fsgrid_thumb_px", 84),
                                     step=8, key="fsgrid_thumb_px", label_visibility="collapsed")
            fs_theme = state.get("theme") or "Modern Worship"
            fs_t = THEMES.get(fs_theme, {})
            fs_entries = _slide_grid_entries(items, adhoc, slides if adhoc else None, item_index,
                                              state.get("font_scale") or 1.0, extend=True, min_count=15)
            _render_slide_grid(fs_entries, adhoc, item_index, slide_index, cols_per_row=5,
                                compact=True, key_prefix="fsgrid_", thumb_px=fs_thumb_px,
                                theme_bg=fs_t.get("bg"), theme_fg=fs_t.get("fg"))
        _render_operator_keyboard_shortcuts()
        return

    # ---------------------------------------------------------------
    # NORMAL LAYOUT — unchanged Service Order / Current / Controls panels,
    # with the Slide Grid added as a new full-width section underneath.
    # ---------------------------------------------------------------
    left, center, right = st.columns([1.2, 2.4, 1])

    with left:
        _render_service_order_panel(items, state, sid, adhoc)

    with center:
        st.markdown("**Current — shown on projector**")
        theme = state["theme"] or "Modern Worship"
        t = THEMES[theme]
        cur_ref, cur_text, cur_text2 = slides[slide_index] if slides else (None, "Nothing selected", None)
        hidden = state["black"] or state["cleared"]

        # Match the real projector background — a preset gradient, a custom
        # uploaded photo, or the flat theme color if none is set — instead of
        # always showing flat theme color here, which didn't match what was
        # actually live on the projector.
        bg_key = state.get("background")
        if bg_key == CUSTOM_BACKGROUND_KEY:
            custom_data = get_settings().get("custom_background_data")
            card_bg = f"center/cover no-repeat url('{custom_data}')" if custom_data else t["bg"]
        else:
            bg_def = BACKGROUNDS.get(bg_key) if bg_key else None
            card_bg = bg_def["css"] if bg_def else t["bg"]
        text_shadow = "text-shadow:0 2px 18px rgba(0,0,0,0.55);" if bg_key and bg_key != "None (theme color)" else ""
        live_scale = state.get("font_scale") or 1.0

        # Locked to the projector's actual 16:9 aspect ratio (not just an
        # arbitrary fixed height) so this box is genuinely the same shape as
        # the real display, not just "some rectangle" that happened to be
        # 320px tall — a fixed height with a flexible width drifts out of
        # sync with 16:9 the moment the browser window is narrower or wider,
        # which is what caused the preview to visibly not match the TV.
        # overflow:hidden is still the hard backstop against content
        # overflow; the font-scale-aware slide splitting in item_slides()
        # is what keeps content actually fitting inside it.
        PREVIEW_BOX = "aspect-ratio:16/9;width:100%;max-height:360px;overflow:hidden;"

        if cur_text.startswith(IMG_SLIDE_PREFIX) and not hidden:
            img_src = cur_text[len(IMG_SLIDE_PREFIX):]
            render_html(
                f"""<div style="background:#000;border-radius:16px;{PREVIEW_BOX}
                border:1px solid {CARD_BORDER};display:flex;align-items:center;justify-content:center;">
                <img src="{img_src}" style="max-width:100%;max-height:100%;object-fit:contain;" />
                </div>"""
            )
        elif cur_text2 and not hidden:
            top_dir = "rtl" if _looks_arabic(cur_text) else "ltr"
            bottom_dir = "rtl" if _looks_arabic(cur_text2) else "ltr"
            render_html(
                f"""<div style="background:{card_bg};border-radius:16px;{PREVIEW_BOX}
                border:1px solid {CARD_BORDER};display:flex;flex-direction:column;">
                <div dir="{top_dir}" style="flex:1;padding:1.2rem;display:flex;flex-direction:column;overflow:hidden;
                align-items:center;justify-content:center;text-align:center;border-bottom:1px solid {CARD_BORDER};">
                {f'<div style="color:{t["sub"]};letter-spacing:.1em;text-transform:uppercase;margin-bottom:0.6rem;font-family:{t["font"]};font-size:0.8rem;{text_shadow}">{cur_ref}</div>' if cur_ref else ''}
                <div style="color:{t['fg']};font-family:{t['font']};font-size:1.2rem;font-weight:700;white-space:pre-line;line-height:1.4;{text_shadow}">{cur_text}</div>
                </div>
                <div dir="{bottom_dir}" style="flex:1;padding:1.2rem;display:flex;align-items:center;justify-content:center;text-align:center;overflow:hidden;">
                <div style="color:{t['fg']};font-family:{t['font']};font-size:1.1rem;font-weight:700;white-space:pre-line;line-height:1.4;{text_shadow}">{cur_text2}</div>
                </div>
                </div>"""
            )
            st.caption("Bilingual split screen — top/bottom shown exactly as on the projector.")
        else:
            display_text = "(hidden from projector)" if hidden else (
                "(image slide)" if cur_text.startswith(IMG_SLIDE_PREFIX) else cur_text
            )
            render_html(
                f"""<div style="background:{card_bg};border-radius:16px;padding:2.4rem 1.6rem;{PREVIEW_BOX}
                display:flex;flex-direction:column;align-items:center;justify-content:center;
                text-align:center;border:1px solid {CARD_BORDER};">
                {f'<div style="color:{t["sub"]};letter-spacing:.1em;text-transform:uppercase;margin-bottom:1rem;font-family:{t["font"]};{text_shadow}">{cur_ref}</div>' if cur_ref else ''}
                <div style="color:{t['fg']};font-family:{t['font']};font-size:calc(1.5rem * {live_scale});font-weight:700;white-space:pre-line;line-height:1.4;{text_shadow}">{display_text}</div>
                </div>"""
            )
        st.caption("This mirrors the projector exactly, including the live background and font size — locked to a fixed size, never scrolls.")
        st.markdown("**Up Next**")
        nxt_ref, nxt_text, nxt_text2 = (None, "—", None)
        nxt_item_title = None
        if slides and slide_index + 1 < len(slides):
            nxt_ref, nxt_text, nxt_text2 = slides[slide_index + 1]
        elif not adhoc and item_index + 1 < len(items):
            nslides = item_slides(items[item_index + 1], state.get("font_scale") or 1.0)
            if nslides:
                nxt_ref, nxt_text, nxt_text2 = nslides[0]
            nxt_item_title = items[item_index + 1]['title']
        nxt_is_img = (nxt_text or "").startswith(IMG_SLIDE_PREFIX)
        if nxt_is_img:
            # Real thumbnail of the actual next slide (an imported PDF/Google
            # Slides page, etc.) instead of a "(image slide)" text
            # placeholder — this mirrors the fix already applied to the
            # Stage Display and phone Remote's own "Up Next" sections.
            prefix_label = f'<div style="color:{t["sub"]};font-size:0.8rem;margin-bottom:0.5rem;">{"(Next item) " + nxt_item_title if nxt_item_title else ""}</div>' if nxt_item_title else ""
            st.markdown(
                f'<div class="ecc-card" style="padding:0.6rem;">{prefix_label}'
                f'<img src="{nxt_text[len(IMG_SLIDE_PREFIX):]}" style="width:100%;max-height:160px;object-fit:contain;border-radius:8px;" onerror="this.replaceWith(Object.assign(document.createElement(&quot;div&quot;),{{textContent:&quot;(image failed to load)&quot;,style:&quot;color:#B0463F;font-size:0.85rem;&quot;}}))" /></div>',
                unsafe_allow_html=True
            )
        else:
            nxt_display = nxt_text
            if nxt_item_title:
                nxt_display = f"(Next item) {nxt_item_title} — {nxt_text}"
            st.markdown(f'<div class="ecc-card">{(nxt_ref + " — ") if nxt_ref else ""}{nxt_display}{(" / " + nxt_text2) if nxt_text2 else ""}</div>', unsafe_allow_html=True)

    with right:
        st.markdown("**Controls**")
        st.caption(f"Slide {slide_index + 1 if slides else 0} / {len(slides)}")
        cp, cn = st.columns(2)
        if cp.button("◀ PREV", use_container_width=True) and slide_index > 0:
            if adhoc:
                set_state(adhoc_index=slide_index - 1, cleared=0)
            else:
                set_state(slide_index=slide_index - 1, cleared=0)
            st.rerun()
        if cn.button("NEXT ▶", use_container_width=True):
            if slide_index < len(slides) - 1:
                if adhoc:
                    set_state(adhoc_index=slide_index + 1, cleared=0)
                else:
                    set_state(slide_index=slide_index + 1, cleared=0)
                st.rerun()
            elif not adhoc and item_index + 1 < len(items):
                set_state(item_index=item_index + 1, slide_index=0, cleared=0)
                st.rerun()
        st.write("")
        if st.button("🖥 Present", use_container_width=True):
            set_state(cleared=0, black=0, live=1); st.rerun()
        # This used to be a plain st.button that only showed a text tip —
        # a regular Streamlit button can't itself call browser JS, so it
        # could never actually open anything. This renders the real
        # clickable button (Window Management API auto-open) in its place.
        render_display_open_widget(compact=True)
        is_black = bool(state["black"])
        toggled_black = st.toggle("⬛ Black Screen", value=is_black, key="op_black_toggle")
        if toggled_black != is_black:
            set_state(black=1 if toggled_black else 0); st.rerun()
        st.write("")
        st.selectbox("Theme", list(THEMES.keys()), index=list(THEMES.keys()).index(theme), key="live_theme",
                     on_change=lambda: set_state(theme=st.session_state.live_theme))

        st.write("")
        st.markdown("**Font Size**")
        cur_scale = state.get("font_scale") or 1.0
        fm, fp, fpct = st.columns([1, 1, 1.4])
        if fm.button("➖", use_container_width=True, key="font_minus", help="Smaller text on the projector"):
            set_state(font_scale=round(max(0.5, cur_scale - 0.1), 2))
            st.rerun()
        if fp.button("➕", use_container_width=True, key="font_plus", help="Bigger text on the projector"):
            set_state(font_scale=round(min(2.0, cur_scale + 0.1), 2))
            st.rerun()
        fpct.markdown(f"<div style='text-align:center;padding-top:0.4rem;'>{int(cur_scale*100)}%</div>", unsafe_allow_html=True)
        st.caption("The \"Current\" panel in the center already mirrors the projector at this size and background — no separate preview needed.")

    st.markdown("---")
    grid_l, grid_r = st.columns([4, 1.3])
    with grid_l:
        st.markdown("**🎬 Slide Grid**")
        st.caption("ProPresenter-style — every slide of the current item, in order. Click a thumbnail to jump straight to it.")
    with grid_r:
        if st.button("⛶ Full Screen", use_container_width=True, key="grid_enter_fs"):
            st.session_state["presentation_grid_fullscreen"] = True
            st.rerun()
    grid_thumb_px = st.slider("Card size", min_value=60, max_value=200,
                               value=st.session_state.get("grid_thumb_px", 84),
                               step=8, key="grid_thumb_px", label_visibility="collapsed")
    grid_entries = _slide_grid_entries(items, adhoc, slides if adhoc else None, item_index,
                                        state.get("font_scale") or 1.0, extend=False)
    _render_slide_grid(grid_entries, adhoc, item_index, slide_index, cols_per_row=4, compact=False,
                        key_prefix="grid_", thumb_px=grid_thumb_px, theme_bg=card_bg, theme_fg=t.get("fg"))

    _render_operator_keyboard_shortcuts()


def render_import_lyrics_form(key_prefix="lyrics"):
    """The paste-lyrics importer: paste, click Apply to parse it into slides
    (packed to a character budget sized for a TV screen — see
    MAX_SLIDE_CHARS — never splitting a line mid-way), review, then Save.
    Shared as-is by the Import Lyrics page and the Song Library's inline
    "Import Songs" section, so there's exactly one text box and one set of
    rules to keep in sync.

    Uses a counter-suffixed widget key instead of ever writing back into
    st.session_state[<the textarea's own key>] after the widget has been
    instantiated — that pattern is exactly what raises
    StreamlitWidgetAlreadyInstantiatedError. Bumping the counter after Save
    gives the textarea a fresh key next run, which clears it safely.
    """
    counter_key = f"{key_prefix}_counter"
    st.session_state.setdefault(counter_key, 0)
    text_key = f"{key_prefix}_input_{st.session_state[counter_key]}"

    raw = st.text_area("Paste lyrics here", height=320, key=text_key,
                        placeholder="Fall On Me\nSong by NEEDTOBREATHE ‧ 2023\n\nOverview\nLyrics\nYou were there to pick me up\n...")

    category = st.selectbox("Category", ["Worship", "Hymn", "Christmas", "Youth", "Other"], key=f"{key_prefix}_category")
    tags = st.text_input("Tags (optional)", key=f"{key_prefix}_tags")

    if st.button("✅ Apply", key=f"{key_prefix}_apply", use_container_width=True, disabled=not raw.strip()):
        title, artist, year, slides = parse_pasted_lyrics(raw)
        st.session_state[f"{key_prefix}_parsed"] = {"title": title, "artist": artist, "year": year, "slides": slides}

    parsed = st.session_state.get(f"{key_prefix}_parsed")
    if parsed:
        st.write("")
        st.markdown(f"**{parsed['title']}**" + (f" — {parsed['artist']}" if parsed['artist'] else "") + (f" ({parsed['year']})" if parsed['year'] else ""))
        st.caption(f"{len(parsed['slides'])} slide(s) — packed to fit a TV screen, lines are never cut mid-way.")
        with st.expander("Preview slides", expanded=True):
            for i, s in enumerate(parsed["slides"]):
                st.markdown(f'<div class="ecc-card"><b>Slide {i+1}</b><br>{s}</div>'.replace("\n", "<br>"), unsafe_allow_html=True)

        if st.button("💾 Save Song", key=f"{key_prefix}_save", use_container_width=True, type="primary"):
            song_id, saved_slides, was_overwrite = upsert_song(parsed["title"], parsed["artist"], category, tags, parsed["slides"])
            if was_overwrite:
                st.toast(f"\"{parsed['title']}\" was already in your library — overwritten with this version ({len(saved_slides)} slides).", icon="✅")
            else:
                st.toast(f"Saved \"{parsed['title']}\" with {len(saved_slides)} slides to your library.", icon="✅")
            if turso_configured():
                try:
                    turso_push_song(song_id, parsed["title"], parsed["artist"], category, tags, json.dumps(saved_slides))
                except Exception as e:
                    st.warning(f"Saved locally, but the Turso sync failed: {e}")
            st.session_state.pop(f"{key_prefix}_parsed", None)
            st.session_state[counter_key] += 1
            st.rerun()
    else:
        st.info("Paste lyrics above, then click Apply to see the slide preview.")


def page_import_slides():
    st.markdown("### Import Slides (Google Slides PDF)")
    st.caption(
        "Export your Google Slides deck as a PDF (File → Download → PDF), then upload it here — each "
        "page becomes its own full-bleed slide, in the original slide's look, ready to add to a service."
    )
    if not PYMUPDF_AVAILABLE:
        st.warning("This needs the `PyMuPDF` package — add `PyMuPDF` to requirements.txt to enable it.")
        return

    pdf_file = st.file_uploader("Google Slides PDF export", type=["pdf"], key="slides_pdf_uploader")
    deck_title = st.text_input("Deck title", value=(pdf_file.name.rsplit(".", 1)[0] if pdf_file else ""),
                                key="slides_pdf_title")

    if pdf_file is not None:
        if st.button("Process PDF into slides", use_container_width=True):
            try:
                pdf_bytes = pdf_file.read()
                doc = fitz.open(stream=pdf_bytes, filetype="pdf")
                images = []
                for page in doc:
                    pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))  # 2x for a crisp projector image
                    png_bytes = pix.tobytes("png")
                    data_uri = "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")
                    images.append(data_uri)
                doc.close()
                st.session_state["pending_deck_images"] = images
                st.session_state["pending_deck_title"] = deck_title or "Untitled Deck"
                st.toast(f"Separated into {len(images)} slide(s). Review below, then save.", icon="✅")
            except Exception as e:
                st.error(f"Couldn't process that PDF: {e}")

    pending = st.session_state.get("pending_deck_images")
    if pending:
        st.write("")
        st.markdown(f"**Preview — {len(pending)} slide(s)**")
        cols = st.columns(4)
        for i, img in enumerate(pending):
            with cols[i % 4]:
                st.image(img, caption=f"Slide {i+1}", use_container_width=True)
        st.write("")
        if st.button("💾 Save Deck to Library", use_container_width=True, type="primary"):
            title = st.session_state.get("pending_deck_title") or "Untitled Deck"
            add_slide_deck(title, "google_slides_pdf", pending)
            st.toast(f"Saved \"{title}\" to your library.", icon="✅")
            st.session_state.pop("pending_deck_images", None)
            st.session_state.pop("pending_deck_title", None)
            st.rerun()

    st.write("")
    st.markdown("#### Imported decks")
    decks = get_slide_decks()
    if not decks:
        st.caption("No slide decks imported yet.")
    for d in decks:
        images = json.loads(d["slides"])
        with st.container(border=True):
            st.markdown(f"**{d['title']}** — {len(images)} slide(s)")
            with st.popover("✏️ Rename"):
                new_title = st.text_input("New title", value=d["title"], key=f"deck_rename_{d['id']}")
                if st.button("Save name", key=f"deck_rename_save_{d['id']}") and new_title.strip():
                    conn = get_conn()
                    conn.execute("UPDATE slide_decks SET title=?, updated_at=? WHERE id=?", (new_title.strip(), now(), d["id"]))
                    conn.commit(); conn.close()
                    if turso_configured():
                        try:
                            turso_push_slide_deck(d["id"], new_title.strip(), d["source"], d["slides"])
                        except Exception:
                            pass
                    st.rerun()
            b1, b2, b3 = st.columns(3)
            if b1.button("Add to today's service", key=f"deck_add_{d['id']}", use_container_width=True):
                sid = ensure_active_service()
                service = get_service(sid) if sid else None
                items = json.loads(service["items"]) if service else []
                items.append(make_deck_item(d))
                update_service_items(sid, items)
                st.toast(f"Added \"{d['title']}\" to today's service.", icon="✅")
            if turso_configured():
                if b2.button("☁️ Sync", key=f"deck_sync_{d['id']}", use_container_width=True):
                    try:
                        turso_push_slide_deck(d["id"], d["title"], d["source"], d["slides"])
                        st.toast("Synced.", icon="☁️")
                    except Exception as e:
                        st.error(f"Sync failed: {e}")
            with b3.container(key=f"del_wrap_deck_{d['id']}"):
                if st.button("🗑 Delete", key=f"deck_del_{d['id']}", use_container_width=True):
                    delete_slide_deck(d["id"])
                    st.rerun()


def page_song_library():
    st.markdown("### Song Library")
    st.caption("Your full catalog — the same songs available from the Songs tab.")
    page_songs()


def page_saved_services():
    st.markdown("### Saved Services")
    services = get_services()
    if not services:
        st.caption("No saved services yet.")
    for s in services:
        items = json.loads(s["items"])
        with st.container(border=True):
            st.markdown(f"**{s['name']}** — {s['service_date']}")
            n_songs = sum(1 for i in items if i["type"] == "song")
            n_bible = sum(1 for i in items if i["type"] == "bible")
            n_ann = sum(1 for i in items if i["type"] == "announcement")
            n_custom = sum(1 for i in items if i["type"] == "custom")
            n_deck = sum(1 for i in items if i["type"] == "imagedeck")
            parts = []
            if n_songs: parts.append(f"{n_songs} song{'s' if n_songs != 1 else ''}")
            if n_bible: parts.append(f"{n_bible} Bible passage{'s' if n_bible != 1 else ''}")
            if n_ann: parts.append(f"{n_ann} announcement{'s' if n_ann != 1 else ''}")
            if n_custom: parts.append(f"{n_custom} custom slide{'s' if n_custom != 1 else ''}")
            if n_deck: parts.append(f"{n_deck} imported deck{'s' if n_deck != 1 else ''}")
            st.caption(" · ".join(parts) if parts else "Empty service")
            b1, b2 = st.columns(2)
            if b1.button("Open in Builder", key=f"sb_{s['id']}"):
                st.session_state.active_service_id = s["id"]; st.session_state.page = "Service Builder"; st.rerun()
            if b2.button("Start", key=f"sp_{s['id']}"):
                set_state(service_id=s["id"], item_index=0, slide_index=0, black=0, cleared=0, live=1)
                st.session_state.active_service_id = s["id"]; st.session_state.page = "Presentation"; st.rerun()


def page_church_settings():
    st.markdown("### Church Settings")
    settings = get_settings()
    name = st.text_input("Church name", value=settings["church_name"])
    if st.button("Save"):
        set_settings(church_name=name)
        st.toast("Saved.", icon="✅")
    st.write("")
    st.markdown("#### Bible & Song Licensing")
    st.caption(
        "This app ships with a small public-domain (KJV) Bible sample and a handful of demo songs "
        "for evaluation only. Before using this in a real service, load a translation your church has "
        "rights to display, and add songs manually or via a properly licensed import — never scrape "
        "copyrighted lyrics automatically."
    )

    st.write("")
    st.markdown("#### Import Bible Text")
    st.caption(
        "Upload a JSON file of a translation you or your church have the right to use — a public-domain "
        "translation (e.g. KJV, WEB, ASV) or one you're properly licensed for. Two accepted shapes:\n\n"
        "**Nested:** `{\"John\": {\"3\": {\"16\": \"For God so loved...\"}}}`\n\n"
        "**Flat list:** `[{\"book\": \"John\", \"chapter\": 3, \"verse\": 16, \"text\": \"For God so loved...\"}]`"
    )
    bcol1, bcol2 = st.columns([2, 1])
    translation_name = bcol1.text_input("Translation name", value="My Translation", key="bible_import_name")
    replace_existing = bcol2.checkbox("Replace if it already exists", value=False, key="bible_import_replace")
    bible_file = st.file_uploader("Bible JSON file", type=["json"], key="bible_uploader")
    if bible_file is not None and st.button("Import Bible"):
        try:
            data = json.load(bible_file)
            n = import_bible_json(data, translation_name.strip() or "Imported", replace_existing)
            st.toast(f"Imported {n} verses as '{translation_name}'.", icon="✅")
        except Exception as e:
            st.error(f"Couldn't import that file: {e}")

    st.write("")
    st.markdown("#### Delete a Translation")
    existing_translations = get_bible_translations()
    if not existing_translations:
        st.caption("No translations imported yet.")
    else:
        st.caption(
            "Removes every verse of the chosen translation from your library. Any Bible slides "
            "already saved into a service keep their text as-is — deleting a translation here doesn't "
            "change services you've already built."
        )
        del_col1, del_col2 = st.columns([2, 1])
        translation_to_delete = del_col1.selectbox("Translation", existing_translations, key="bible_delete_pick")
        also_delete_remote = del_col2.checkbox("Also delete from Turso", value=False, key="bible_delete_remote",
                                                disabled=not turso_configured(),
                                                help="Unchecked, this only removes it locally." if turso_configured()
                                                else "Set up Turso in Church Settings to enable this.")
        st.markdown('<div class="ecc-danger">', unsafe_allow_html=True)
        delete_disabled = len(existing_translations) <= 1
        if st.button(f"🗑 Delete \"{translation_to_delete}\"", key="bible_delete_btn",
                     use_container_width=True, disabled=delete_disabled):
            n_deleted = delete_bible_translation(translation_to_delete)
            if also_delete_remote and turso_configured():
                try:
                    turso_delete_bible_translation(translation_to_delete)
                    st.toast(f"Deleted \"{translation_to_delete}\" ({n_deleted} verses) locally and from Turso.", icon="✅")
                except Exception as e:
                    st.toast(f"Deleted \"{translation_to_delete}\" locally, but the Turso delete failed: {e}", icon="⚠️")
            else:
                st.toast(f"Deleted \"{translation_to_delete}\" ({n_deleted} verses).", icon="✅")
            st.rerun()
        st.markdown('</div>', unsafe_allow_html=True)
        if delete_disabled:
            st.caption("This is your only translation, so it can't be deleted — import another one first if you want to remove it.")

    st.write("")
    st.markdown("#### ☁️ Turso Cloud Save")
    if not turso_configured():
        st.caption(
            "Not set up yet. Add `TURSO_DATABASE_URL` and `TURSO_AUTH_TOKEN` to your "
            "`.streamlit/secrets.toml` (or your host's Secrets settings) to enable this — "
            "get both from the Turso CLI: `turso db show <name> --url` and "
            "`turso db tokens create <name>`."
        )
    else:
        n_songs_pending = len(_rows_needing_sync("songs", ["id"]))
        n_services_pending = len(_rows_needing_sync("services", ["id"]))
        n_decks_pending = len(_rows_needing_sync("slide_decks", ["id"]))
        _conn = get_conn()
        _already_synced_translations = {r[0] for r in _conn.execute("SELECT translation FROM synced_translations").fetchall()}
        n_verses_pending = _conn.execute(
            "SELECT COUNT(*) FROM bible_verses WHERE translation NOT IN ({})".format(
                ",".join("?" * len(_already_synced_translations)) or "''"
            ), tuple(_already_synced_translations)
        ).fetchone()[0] if _already_synced_translations else _conn.execute("SELECT COUNT(*) FROM bible_verses").fetchone()[0]
        _conn.close()
        total_items = n_songs_pending + n_services_pending + n_decks_pending + 1 + (n_verses_pending / 50.0)  # +1 for background/display settings; verses are cheap in bulk
        est_seconds = max(2, round(total_items * 0.3))
        if total_items <= 1:  # nothing but the always-sent background/settings row
            st.caption("Everything is already synced — clicking will just refresh the background/display settings.")
        else:
            st.caption(
                f"Pushes everything that's changed since your last sync to Turso in one go. Right now "
                f"that's {n_songs_pending} song(s), {n_services_pending} service(s), "
                f"{n_decks_pending} slide deck(s), and {n_verses_pending} Bible verse(s) not yet synced — "
                f"usually about {est_seconds}s. Nothing syncs automatically; this button is the only "
                f"thing that triggers it."
            )
        if st.button("☁️ Save Everything to Turso", use_container_width=True, type="primary"):
            progress_bar = st.progress(0.0)
            status_line = st.empty()
            start_time = time.time()
            # Rough weight per stage so the bar moves at a believable pace —
            # Bible verses is the only stage broken into real sub-steps
            # (see turso_push_bible_verses' progress_cb), so it gets most of
            # the bar's width; the others just tick forward stage-by-stage.
            STAGE_WEIGHTS = {"Songs": 0.15, "Saved services": 0.15, "Background/display settings": 0.05,
                             "Slide decks": 0.15, "Bible verses": 0.50}
            STAGE_ORDER = list(STAGE_WEIGHTS.keys())

            def on_progress(stage, done, total):
                stage_idx = STAGE_ORDER.index(stage)
                completed_weight = sum(STAGE_WEIGHTS[s] for s in STAGE_ORDER[:stage_idx])
                frac_within_stage = (done / total) if total else 0
                overall = completed_weight + STAGE_WEIGHTS[stage] * frac_within_stage
                progress_bar.progress(min(overall, 1.0))
                elapsed = time.time() - start_time
                if stage == "Bible verses" and total > 1:
                    status_line.caption(f"{stage} — {done}/{total} verses ({elapsed:.0f}s elapsed)")
                else:
                    status_line.caption(f"{stage}… ({elapsed:.0f}s elapsed)")

            try:
                ns, nsv, nd, nv = turso_sync_all(progress_cb=on_progress)
                elapsed = time.time() - start_time
                progress_bar.progress(1.0)
                status_line.empty()
                progress_bar.empty()
                if ns + nsv + nd + nv == 0:
                    st.toast(f"Already up to date in {elapsed:.1f}s — nothing new to sync.", icon="☁️")
                else:
                    st.toast(f"Saved changes to Turso in {elapsed:.1f}s — {ns} song(s), "
                              f"{nsv} service(s), the background, {nd} slide deck(s), and {nv} Bible verse(s).", icon="☁️")
            except Exception as e:
                progress_bar.empty()
                elapsed = time.time() - start_time
                status_line.empty()
                if _is_turso_space_error(e):
                    freed = []
                    deck_name = turso_delete_oldest_deck()
                    if deck_name:
                        freed.append(f"slide deck \"{deck_name}\"")
                    service_name = turso_delete_oldest_service()
                    if service_name:
                        freed.append(f"service \"{service_name}\"")
                    if freed:
                        st.warning(f"Turso ran out of space — automatically deleted the oldest "
                                   f"{' and the oldest '.join(freed)} from the cloud to free room. "
                                   f"Click Save Everything again to finish.")
                    else:
                        st.error("Turso ran out of space, and there was nothing old enough in the "
                                 "cloud to automatically remove.")
                elif isinstance(e, requests.exceptions.Timeout):
                    st.error(
                        f"Timed out after {elapsed:.0f}s waiting on Turso — the connection to Turso itself "
                        f"is slow or unresponsive right now (each batch of ~300 Bible verses gets 30s "
                        f"before this happens). This is on Turso's end, not something stuck in the app — "
                        f"try again in a bit, or check Turso's status page if it keeps happening."
                    )
                elif isinstance(e, requests.exceptions.ConnectionError):
                    st.error(
                        f"Couldn't reach Turso after {elapsed:.0f}s — check your internet connection, "
                        f"and confirm TURSO_DATABASE_URL in your secrets is correct."
                    )
                else:
                    st.error(f"Save failed after {elapsed:.0f}s: {e}")


def _render_bg_preview(theme, current_bg, settings):
    st.markdown("**Preview**")
    t = THEMES[theme]
    if current_bg == CUSTOM_BACKGROUND_KEY and settings.get("custom_background_data"):
        preview_bg = f"center/cover no-repeat url('{settings['custom_background_data']}')"
        preview_shadow = "text-shadow:0 2px 18px rgba(0,0,0,0.55);"
    else:
        bg_def = BACKGROUNDS.get(current_bg)
        preview_bg = bg_def["css"] if bg_def else t["bg"]
        preview_shadow = "text-shadow:0 2px 18px rgba(0,0,0,0.55);" if bg_def else ""
    render_html(
        f"""<div style="background:{preview_bg};border-radius:16px;padding:3rem;text-align:center;position:relative;overflow:hidden;">
        <div style="position:absolute;inset:0;background:radial-gradient(circle, transparent 35%, rgba(0,0,0,0.45) 100%);"></div>
        <div style="position:relative;color:{t['sub']};text-transform:uppercase;letter-spacing:.1em;margin-bottom:.8rem;font-family:{t['font']};{preview_shadow}">JOHN 3:16</div>
        <div style="position:relative;color:{t['fg']};font-size:1.6rem;font-weight:700;font-family:{t['font']};{preview_shadow}">For God so loved the world...</div>
        </div>"""
    )


def page_display_settings():
    st.markdown("### Display Settings")
    settings = get_settings()
    theme = st.selectbox("Default presentation theme", list(THEMES.keys()),
                          index=list(THEMES.keys()).index(settings["default_theme"]))
    if st.button("Save Default Theme"):
        set_settings(default_theme=theme)
        st.toast("Saved.", icon="✅")

    st.write("")
    st.markdown("#### Background")
    st.caption(
        "Original animated/gradient backgrounds — no stock photos, so there's nothing to license "
        "and they still render if you're ever offline. New services pick up whatever's saved here; "
        "you can also override it per-service from Service Builder before you hit Start."
    )
    current_bg = settings.get("default_background") or "None (theme color)"
    bg_names = list(BACKGROUNDS.keys())
    swatch_cols = st.columns(len(bg_names))
    for col, name in zip(swatch_cols, bg_names):
        with col:
            bg_def = BACKGROUNDS[name]
            swatch_css = bg_def["swatch"] if bg_def else THEMES[theme]["bg"]
            render_html(
                f"""<div style="background:{swatch_css};height:70px;border-radius:8px;
                border:2px solid {'#C8A24A' if name == current_bg else 'transparent'};"></div>"""
            )
            if st.button(name, key=f"bg_pick_{name}", use_container_width=True):
                set_settings(default_background=name)
                # Also push it to the live presentation_state, not just the
                # default for future services — otherwise this only takes
                # effect the next time you hit "Start Service", and the
                # projector you're currently looking at doesn't change,
                # which is the bug you were hitting.
                set_state(background=name)
                st.rerun()

    st.write("")
    st.markdown("#### Custom Photo Background")
    st.caption(
        "Upload your own landscape photo — it's automatically blurred and dimmed (the same "
        "\"blurred, dim\" look as the built-in options) so slide text stays readable on top of it. "
        "I can't source real stock photos myself, but this lets you use your own."
    )
    if not PIL_AVAILABLE:
        st.warning("This needs the `Pillow` package — add `Pillow` to requirements.txt to enable it.")
    else:
        upcol1, upcol2 = st.columns([2, 1])
        with upcol1:
            photo = st.file_uploader("Landscape photo", type=["jpg", "jpeg", "png"], key="bg_photo_uploader",
                                      label_visibility="collapsed")
        with upcol2:
            blur = st.slider("Blur", 0, 25, 10, key="bg_blur")
        dim = st.slider("Dim (lower = darker)", 0.2, 1.0, 0.55, key="bg_dim")

        # Show the raw upload immediately, before anything is saved to the
        # database — nothing is written yet at this point, this is purely a
        # "here's what you just picked" confirmation.
        if photo is not None:
            st.markdown("**Preview (not saved yet)**")
            st.image(photo, caption="Your upload — click below to process and save it", width=300)

        if photo is not None and st.button("💾 Process & Save as Background", use_container_width=True):
            data_uri = save_custom_background(photo, blur_radius=blur, dim_factor=dim)
            if data_uri:
                set_settings(custom_background_data=data_uri, default_background=CUSTOM_BACKGROUND_KEY)
                set_state(background=CUSTOM_BACKGROUND_KEY)
                st.toast("Saved locally and set as the live background.", icon="✅")
                if turso_configured():
                    try:
                        settings_now = get_settings()
                        turso_push_background(settings_now["default_theme"], CUSTOM_BACKGROUND_KEY,
                                               data_uri, settings_now.get("church_name"))
                        st.toast("Also synced to Turso.", icon="☁️")
                    except Exception as e:
                        st.warning(f"Saved locally, but the Turso sync failed: {e}")
                st.rerun()
            else:
                st.error("Couldn't process that image.")

        if not settings.get("custom_background_data"):
            st.write("")
            _render_bg_preview(theme, current_bg, settings)

        if settings.get("custom_background_data"):
            st.write("")
            st.markdown("**Saved background**")
            st.image(settings["custom_background_data"], caption="Current custom background (processed)", width=300)
            is_active_custom = current_bg == CUSTOM_BACKGROUND_KEY
            if is_active_custom:
                st.success("✅ Currently active")
            if st.button("Use this custom photo now", key="use_custom_bg", disabled=is_active_custom):
                set_settings(default_background=CUSTOM_BACKGROUND_KEY)
                set_state(background=CUSTOM_BACKGROUND_KEY)
                st.rerun()

            st.write("")
            _render_bg_preview(theme, current_bg, settings)

    st.write("")
    st.markdown("#### ☁️ Cloud Sync")
    if not turso_configured():
        st.caption("Set up Turso (Church Settings) to enable cloud sync and backups.")
    else:
        st.caption(
            "Pushes anything changed since your last sync — songs, saved services, the current "
            "background, any imported slide decks, and any new Bible translations — to Turso in one "
            "go. Already-synced items are skipped. Nothing syncs automatically; this button is the "
            "only thing that triggers it."
        )
        if st.button("☁️ Sync All to Cloud", use_container_width=True, type="primary"):
            try:
                n_songs, n_services, n_decks, n_verses = turso_sync_all()
                if n_songs + n_services + n_decks + n_verses == 0:
                    st.toast("Already up to date — nothing new to sync.", icon="☁️")
                else:
                    st.toast(f"Synced {n_songs} song(s), {n_services} service(s), the background, "
                              f"{n_decks} slide deck(s), and {n_verses} Bible verse(s).", icon="☁️")
            except Exception as e:
                st.error(f"Sync failed: {e}")


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    st.set_page_config(page_title="ECC Worship", page_icon="✝", layout="wide")

    qp = st.query_params
    if qp.get("display") == "projector":
        render_projector()
        return
    if qp.get("display") == "stage":
        render_stage_display()
        return
    if qp.get("display") == "remote":
        render_remote()
        return

    # The splash used to render AFTER init_db() and inject_css() — both real
    # work, not instant — so the browser sat on a blank/white tab for
    # however long that took, and the ECC logo only ever appeared already
    # partway through its own animation, right as the real app was about to
    # replace it. This is now the very first thing main() renders (before
    # any DB setup or CSS injection), so the logo is the first paint the
    # browser produces, full stop — nothing blocks it anymore.
    show_splash = not st.session_state.get("_ecc_splash_shown")
    if show_splash:
        _render_splash_screen()
        st.session_state["_ecc_splash_shown"] = True

    # init_db() does several CREATE TABLE / ALTER TABLE / SELECT COUNT checks
    # — cheap once, but it was previously re-run on literally every click
    # across the whole app (Streamlit reruns main() top-to-bottom on every
    # interaction). A module-level flag survives across reruns within the
    # same running process, so this now only actually runs once per app
    # start instead of once per click — a real, always-on speed win for
    # every button everywhere, including the phone remote and Presentation.
    global _DB_INITIALIZED
    if not _DB_INITIALIZED:
        init_db()
        _DB_INITIALIZED = True

    inject_css()

    if "page" not in st.session_state:
        st.session_state.page = "Dashboard"
    if "logged_in" not in st.session_state:
        st.session_state.logged_in = False

    if not st.session_state.logged_in:
        render_login()
        return

    # After sign-in, the meeting-select screen takes over the whole page
    # (its own centered layout, same as the login page) until a meeting is
    # picked — the dashboard doesn't render underneath it at all yet, since
    # there's nothing meeting-specific loaded before that choice is made.
    if st.session_state.get("_ecc_meeting_select_pending"):
        render_meeting_select()
        return

    # Fires exactly once, right after a meeting is picked (the flag is set
    # in render_meeting_select() and cleared here immediately) — never
    # again for the rest of the session, so navigating between pages
    # afterward doesn't keep re-showing the expand transition.
    if st.session_state.get("_ecc_meeting_transition_pending"):
        _render_meeting_transition(st.session_state.get("_ecc_meeting_transition_name") or "")
        st.session_state["_ecc_meeting_transition_pending"] = False

    sidebar()

    pages = {
        "Dashboard": page_dashboard,
        "Song Workspace": page_song_workspace,
        "Bible": page_bible,
        "Service Builder": page_service_builder,
        "Presentation": page_presentation,
        "Song Library": page_song_library,
        "Import Slides": page_import_slides,
        "Saved Services": page_saved_services,
        "Church Settings": page_church_settings,
        "Display Settings": page_display_settings,
        "Database": page_database_stats,
    }
    pages.get(st.session_state.page, page_dashboard)()


DATABASE_TAB_PASSWORD = "2009"


def page_database_stats():
    """A password-locked tab (separate from the main sign-in) showing how
    much is in the local database and how much local disk space is left.
    The password only gates this tab for the current session — it isn't
    tied to the login system at all, so it re-locks every time the app
    restarts, same as everything else in session state."""
    st.markdown("### 📊 Database")
    if not st.session_state.get("_db_tab_unlocked"):
        st.caption("This tab is locked.")
        pw = st.text_input("Password", type="password", key="db_tab_password")
        if st.button("Unlock", key="db_tab_unlock_btn"):
            if pw == DATABASE_TAB_PASSWORD:
                st.session_state["_db_tab_unlocked"] = True
                st.rerun()
            else:
                st.error("Incorrect password.")
        return

    conn = get_conn()
    tables = ["songs", "custom_slides", "slide_decks", "services", "templates",
              "bible_verses", "synced_translations"]
    counts = {}
    for t in tables:
        try:
            counts[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except sqlite3.OperationalError:
            counts[t] = None  # table doesn't exist on this install yet
    translations = conn.execute(
        "SELECT translation, COUNT(*) as n FROM bible_verses GROUP BY translation ORDER BY translation"
    ).fetchall()
    conn.close()

    st.markdown("#### What's in your library")
    LABELS = {
        "songs": "🎵 Songs", "custom_slides": "🖼 Custom slides", "slide_decks": "📑 Imported slide decks",
        "services": "📅 Saved services", "templates": "🧩 Service templates",
        "bible_verses": "📖 Bible verses (all translations combined)",
        "synced_translations": "☁️ Translations marked synced to Turso",
    }
    cols = st.columns(3)
    for i, t in enumerate(tables):
        with cols[i % 3]:
            n = counts[t]
            st.metric(LABELS.get(t, t), "—" if n is None else f"{n:,}")

    if translations:
        st.markdown("#### Bible verses by translation")
        for r in translations:
            st.write(f"**{r['translation']}** — {r['n']:,} verses")

    st.markdown("---")
    st.markdown("#### Storage")
    # Local disk: real numbers, straight from the filesystem the SQLite
    # file actually lives on.
    try:
        db_size_bytes = os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0
        total, used, free = shutil.disk_usage(os.path.dirname(DB_PATH) or ".")
        c1, c2, c3 = st.columns(3)
        c1.metric("Local database file size", _format_bytes(db_size_bytes))
        c2.metric("Free disk space", _format_bytes(free))
        c3.metric("Total disk space", _format_bytes(total))
        st.caption(
            "This is the disk of whatever machine is currently running the app. On Streamlit Community "
            "Cloud specifically, this disk is EPHEMERAL — it's wiped on every restart/redeploy, which is "
            "why Turso sync exists at all. Free space here isn't a long-term concern the way it would be "
            "on a normal server; what matters is whether your data is actually synced to Turso."
        )
    except Exception as e:
        st.caption(f"Couldn't read local disk usage: {e}")

    st.write("")
    if turso_configured():
        st.caption(
            "Turso cloud storage: there's no query this app can run to ask Turso how much of your plan's "
            "storage quota is left — that's only visible from Turso's own dashboard "
            "(turso.tech → your database → Usage), not through the database connection itself. What this "
            "app CAN tell you is what's synced (the counts above) versus what's still pending — check "
            "Church Settings → Cloud Sync for that."
        )
    else:
        st.caption("Turso isn't configured, so there's no cloud storage to report on — everything above is local-only.")

    st.write("")
    if st.button("Lock this tab", key="db_tab_lock_btn"):
        st.session_state["_db_tab_unlocked"] = False
        st.rerun()


def _format_bytes(n):
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if abs(n) < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def render_login():
    # The old version put the logo/tagline in its own min-height:80vh
    # centered div, then rendered the actual username/password/button form
    # as a SEPARATE block below it in normal document flow — so the two
    # stacked on top of each other were taller than the viewport, which is
    # exactly what produced the scrollbar. This locks the whole page
    # (html/body and every Streamlit wrapper div, not just .stApp) to
    # 100vh/overflow:hidden the same way the projector view is locked down,
    # and centers the logo + form together as one flex column so the form
    # is genuinely inside the centered area instead of trailing below it.
    render_html(f"""
    <style>
    html, body {{ overflow: hidden !important; height: 100vh; width: 100vw; margin:0; padding:0; }}
    [data-testid="stAppViewContainer"], [data-testid="stMain"],
    [data-testid="stAppViewContainer"] > .main,
    section.main, div[data-testid="stVerticalBlock"],
    div[data-testid="stAppViewBlockContainer"] {{
        height: 100vh !important; max-height: 100vh !important; width: 100vw !important;
        overflow: hidden !important; margin: 0 !important;
        display: flex !important; flex-direction: column !important; justify-content: center !important;
    }}
    .block-container {{
        padding: 0 !important; margin: 0 auto !important; max-width: 420px !important;
        height: auto !important; overflow: hidden !important;
    }}
    .stApp {{ background: {BG}; overflow: hidden !important; height: 100vh !important; width: 100vw !important; }}
    #MainMenu, footer, header {{visibility: hidden;}}
    section[data-testid="stSidebar"] {{display:none;}}
    .ecc-login-header {{ text-align:center; margin-bottom: 1.6rem; }}
    .ecc-login-title {{ font-weight:800; font-size:2.2rem; margin-bottom:0.2rem; color:{TEXT_PRIMARY}; }}
    .ecc-login-title span {{ color:{ACCENT}; }}
    .ecc-login-sub {{ color:{TEXT_MUTED}; font-size:0.9rem; }}
    </style>
    <div class="ecc-login-header">
        <div class="ecc-login-title">ECC <span>Worship</span></div>
        <div class="ecc-login-sub">Welcome to ECC — Prepare. Present. Worship.</div>
    </div>
    """)
    st.text_input("Username", key="login_username")
    st.text_input("Password", type="password", key="login_password")
    st.markdown('<div class="ecc-primary">', unsafe_allow_html=True)
    if st.button("Sign In", use_container_width=True):
        if st.session_state.login_username == LOGIN_USERNAME and st.session_state.login_password == LOGIN_PASSWORD:
            st.session_state.logged_in = True
            # Ask which meeting this is right after sign-in, instead of a
            # generic "Welcome, <name>" — this app has one shared
            # church-wide login (not individual accounts), so greeting by
            # whatever was typed into Username just said "Welcome, ECC"
            # every time, which wasn't useful. Greeting by meeting instead
            # gives every sign-in a real, meaningful answer, and doubles as
            # the place meeting-specific preferences (set later) attach to.
            st.session_state["_ecc_meeting_select_pending"] = True
            st.rerun()
        else:
            st.error("Incorrect username or password.")
    st.markdown('</div>', unsafe_allow_html=True)
    st.caption("Forgot password? Contact your church admin.")


MEETING_OPTIONS = ["Sunday", "Saturday", "Sanctuary Arabic", "Sanctuary English"]


def render_meeting_select():
    """Shown once, right after a successful sign-in and before the
    dashboard: asks which of the four regular meetings this session is for.
    The choice is saved to settings.meeting_type (so it persists as the
    church's current default across sign-ins/devices, same as the other
    settings columns) AND to session state (so the rest of THIS session can
    read it immediately without a DB round-trip). Meeting-specific behavior
    beyond just remembering the choice — different themes, defaults, etc.
    per meeting — isn't wired up yet; that comes later once it's decided
    what each meeting should actually change."""
    render_html(f"""
    <style>
    html, body {{ overflow: hidden !important; height: 100vh; width: 100vw; margin:0; padding:0; }}
    [data-testid="stAppViewContainer"], [data-testid="stMain"],
    [data-testid="stAppViewContainer"] > .main,
    section.main, div[data-testid="stVerticalBlock"],
    div[data-testid="stAppViewBlockContainer"] {{
        height: 100vh !important; max-height: 100vh !important; width: 100vw !important;
        overflow: hidden !important; margin: 0 !important;
        display: flex !important; flex-direction: column !important; justify-content: center !important;
    }}
    .block-container {{
        padding: 0 !important; margin: 0 auto !important; max-width: 460px !important;
        height: auto !important; overflow: hidden !important;
    }}
    .stApp {{ background: {BG}; overflow: hidden !important; height: 100vh !important; width: 100vw !important; }}
    #MainMenu, footer, header {{visibility: hidden;}}
    section[data-testid="stSidebar"] {{display:none;}}
    .ecc-meeting-header {{ text-align:center; margin-bottom: 1.8rem; }}
    .ecc-meeting-title {{ font-weight:800; font-size:1.7rem; margin-bottom:0.3rem; color:{TEXT_PRIMARY}; }}
    .ecc-meeting-sub {{ color:{TEXT_MUTED}; font-size:0.85rem; }}
    /* Premium-gold meeting buttons — deliberately heavier than the app's
       normal primary-gold button (see .ecc-primary/button[kind="primary"]
       in inject_css()): a richer gradient, a soft gold glow at rest (not
       just on hover), and a slightly taller tap target, since these are
       the very first real choice in the whole app rather than an
       in-context action. */
    .ecc-meeting-btn .stButton>button {{
        background: linear-gradient(160deg, #E8C878 0%, {ACCENT} 45%, #9C7A32 100%) !important;
        color: #1A1400 !important; border: 1px solid #F0D48E !important; font-weight: 800 !important;
        font-size: 1.02rem !important; padding: 0.9rem !important; border-radius: 12px !important;
        box-shadow: 0 4px 18px {ACCENT}4D, inset 0 1px 0 #FFF6DD88 !important;
        transition: transform .12s ease, box-shadow .12s ease !important;
    }}
    .ecc-meeting-btn .stButton>button:hover {{
        transform: translateY(-1px) scale(1.01) !important;
        box-shadow: 0 6px 24px {ACCENT}66, inset 0 1px 0 #FFF6DD !important;
    }}
    </style>
    <div class="ecc-meeting-header">
        <div class="ecc-meeting-title">Which meeting is this?</div>
        <div class="ecc-meeting-sub">Your choice is saved as the default for next time.</div>
    </div>
    """)
    for meeting in MEETING_OPTIONS:
        st.markdown('<div class="ecc-meeting-btn">', unsafe_allow_html=True)
        if st.button(meeting, use_container_width=True, key=f"meeting_pick_{meeting}"):
            set_settings(meeting_type=meeting)
            st.session_state["meeting_type"] = meeting
            st.session_state["_ecc_meeting_select_pending"] = False
            st.session_state["_ecc_meeting_transition_pending"] = True
            st.session_state["_ecc_meeting_transition_name"] = meeting
            st.rerun()
        st.markdown('</div>', unsafe_allow_html=True)
        st.write("")


def _render_meeting_transition(meeting_name):
    """The "expand and take over the screen" transition after picking a
    meeting on render_meeting_select(): a rounded, premium-gold panel starts
    small and centered (matching the button that was just pressed) and
    rapidly scales up to cover the entire screen — rounded corners
    unrounding as it grows, per the "overlay the whole screen with rounded
    edges" + "expand quick" ask — holds briefly showing the meeting name,
    then fades out to reveal the dashboard underneath. Same components.html()
    injection technique as _render_splash_screen/_render_welcome_screen (see
    that docstring for why: it paints immediately instead of waiting on the
    rest of the page's component tree to reconcile) — this one is one-time
    per sign-in, not per session, since it should replay every time a
    meeting is (re)selected."""
    safe_name = meeting_name.replace("`", "").replace("</", "<\\/")
    components.html(
        f"""
        <script>
        (function() {{
            const doc = window.parent.document;
            if (doc.getElementById('ecc-meeting-transition')) return;

            const style = doc.createElement('style');
            style.textContent = `
                @keyframes eccMeetingExpand {{
                    0%   {{ width: 220px; height: 64px; border-radius: 14px; opacity: 1; }}
                    55%  {{ width: 100vw; height: 100vh; border-radius: 0px; opacity: 1; }}
                    100% {{ width: 100vw; height: 100vh; border-radius: 0px; opacity: 1; }}
                }}
                @keyframes eccMeetingFade {{
                    0%   {{ opacity: 1; }}
                    100% {{ opacity: 0; }}
                }}
                @keyframes eccMeetingTextIn {{
                    0%   {{ opacity: 0; transform: scale(0.9); }}
                    100% {{ opacity: 1; transform: scale(1); }}
                }}
                @keyframes eccMeetingAppReveal {{
                    0%   {{ opacity: 0; }}
                    100% {{ opacity: 1; }}
                }}
                #ecc-meeting-transition {{
                    position: fixed; inset: 0; z-index: 999999;
                    display: flex; align-items: center; justify-content: center;
                    pointer-events: none;
                }}
                #ecc-meeting-panel {{
                    background: linear-gradient(160deg, #E8C878 0%, {ACCENT} 45%, #9C7A32 100%);
                    box-shadow: 0 8px 40px {ACCENT}66, inset 0 1px 0 #FFF6DD88;
                    display: flex; align-items: center; justify-content: center;
                    animation: eccMeetingExpand 0.45s cubic-bezier(0.22, 1, 0.36, 1) forwards,
                               eccMeetingFade 0.6s ease 1.3s forwards;
                }}
                #ecc-meeting-panel .ecc-meeting-transition-text {{
                    font-family: 'Inter', sans-serif; font-weight: 800; font-size: 1.9rem;
                    color: #1A1400; text-align: center; padding: 0 1.5rem;
                    opacity: 0; animation: eccMeetingTextIn 0.4s ease 0.4s forwards;
                    white-space: nowrap;
                }}
                [data-testid="stAppViewContainer"] {{ animation: eccMeetingAppReveal 0.7s ease 1.35s both; }}
            `;
            doc.head.appendChild(style);

            const wrap = doc.createElement('div');
            wrap.id = 'ecc-meeting-transition';
            wrap.innerHTML = `
                <div id="ecc-meeting-panel">
                    <div class="ecc-meeting-transition-text">{safe_name}</div>
                </div>
            `;
            doc.body.appendChild(wrap);

            setTimeout(function() {{
                if (wrap && wrap.parentNode) wrap.parentNode.removeChild(wrap);
            }}, 2100);
        }})();
        </script>
        """,
        height=0,
    )


if __name__ == "__main__":
    main()

#git status ; git add . ; git commit -m "Your commit message" ; git push
