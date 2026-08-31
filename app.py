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
import sqlite3
import json
import os
import time
import datetime
import csv
import io

# ---------------------------------------------------------------------------
# CONFIG / CONSTANTS
# ---------------------------------------------------------------------------

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ecc_worship.db")

ACCENT = "#C8A24A"          # warm gold — ECC accent
BG = "#0B0C0F"               # near-black
CARD = "#15171C"             # charcoal card
CARD_BORDER = "#24262C"
TEXT_PRIMARY = "#F4F3EF"
TEXT_MUTED = "#9A9CA3"

THEMES = {
    "Minimal Dark": {"bg": "#000000", "fg": "#FFFFFF", "sub": "#C9C9C9", "font": "'Inter', sans-serif"},
    "Cinematic": {"bg": "#0A0A0A", "fg": "#FFFFFF", "sub": "#D8D2C4", "font": "'Manrope', sans-serif"},
    "Elegant": {"bg": "linear-gradient(160deg,#1a1710,#0a0908)", "fg": "#F5EFE0", "sub": "#C8A24A", "font": "Georgia, serif"},
    "Modern Worship": {"bg": "#0D1117", "fg": "#FFFFFF", "sub": f"{ACCENT}", "font": "'Inter', sans-serif"},
    "Classic": {"bg": "#03122B", "fg": "#FFFFFF", "sub": "#9FB6D9", "font": "Georgia, serif"},
}

SONG_CATEGORIES = ["Worship", "Praise", "Hymns", "Contemporary"]

# A small public-domain (KJV) Bible sample. Real deployments should load a
# properly licensed / public-domain translation file — see note in the
# Bible tab. Keeping this list short is intentional (see item 23 of spec:
# do not assume every translation may be legally bundled).
BIBLE_SAMPLE = {
    "John": {
        3: {
            16: "For God so loved the world, that he gave his only begotten Son, that whosoever believeth in him should not perish, but have everlasting life.",
            17: "For God sent not his Son into the world to condemn the world; but that the world through him might be saved.",
            18: "He that believeth on him is not condemned: but he that believeth not is condemned already, because he hath not believed in the name of the only begotten Son of God.",
        }
    },
    "Psalm": {
        23: {
            1: "The LORD is my shepherd; I shall not want.",
            2: "He maketh me to lie down in green pastures: he leadeth me beside the still waters.",
            3: "He restoreth my soul: he leadeth me in the paths of righteousness for his name's sake.",
            4: "Yea, though I walk through the valley of the shadow of death, I will fear no evil: for thou art with me; thy rod and thy staff they comfort me.",
        }
    },
    "Romans": {
        8: {
            1: "There is therefore now no condemnation to them which are in Christ Jesus, who walk not after the flesh, but after the Spirit.",
            28: "And we know that all things work together for good to them that love God, to them who are the called according to his purpose.",
        }
    },
    "Philippians": {
        4: {
            6: "Be careful for nothing; but in every thing by prayer and supplication with thanksgiving let your requests be made known unto God.",
            7: "And the peace of God, which passeth all understanding, shall keep your hearts and minds through Christ Jesus.",
            13: "I can do all things through Christ which strengtheneth me.",
        }
    },
}
BIBLE_TRANSLATION_LABEL = "KJV (Public Domain)"

# Canonical book order (standard 66-book Protestant order) used to line up the
# same passage across translations that name books differently (e.g. an
# Arabic file whose book names are in Arabic script can still be matched to
# an English translation by this shared number). Only covers the books used
# by the built-in sample; imported files bring their own numbers with them.
BIBLE_BOOK_NUMBERS = {"Psalm": 19, "John": 43, "Romans": 45, "Philippians": 50}
BIBLE_NUMBER_TO_BOOK = {v: k for k, v in BIBLE_BOOK_NUMBERS.items()}

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
        church_name TEXT, default_theme TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS presentation_state(
        id INTEGER PRIMARY KEY CHECK (id=1),
        service_id INTEGER, item_index INTEGER, slide_index INTEGER,
        black INTEGER, cleared INTEGER, live INTEGER, theme TEXT, updated_at TEXT
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

    if c.execute("SELECT COUNT(*) FROM settings").fetchone()[0] == 0:
        c.execute("INSERT INTO settings(id, church_name, default_theme) VALUES (1, 'ECC', 'Modern Worship')")
    if c.execute("SELECT COUNT(*) FROM presentation_state").fetchone()[0] == 0:
        c.execute("""INSERT INTO presentation_state(id, service_id, item_index, slide_index, black, cleared, live, theme, updated_at)
                     VALUES (1, NULL, 0, 0, 0, 1, 1, 'Modern Worship', ?)""", (now(),))
    conn.commit()

    if c.execute("SELECT COUNT(*) FROM songs").fetchone()[0] == 0:
        seed_songs(conn)
    if c.execute("SELECT COUNT(*) FROM templates").fetchone()[0] == 0:
        c.execute("INSERT INTO templates(name, structure) VALUES (?, ?)", (
            "Sunday Worship",
            json.dumps(["Welcome", "Song", "Song", "Song", "Scripture Reading", "Sermon", "Closing Song"])
        ))
    if c.execute("SELECT COUNT(*) FROM bible_verses").fetchone()[0] == 0:
        for book, chapters in BIBLE_SAMPLE.items():
            for chapter, verses in chapters.items():
                for verse, text in verses.items():
                    c.execute(
                        "INSERT OR IGNORE INTO bible_verses(book, chapter, verse, text, translation, book_number) VALUES (?,?,?,?,?,?)",
                        (book, chapter, verse, text, BIBLE_TRANSLATION_LABEL, BIBLE_BOOK_NUMBERS.get(book))
                    )
    conn.commit()
    conn.close()


def seed_songs(conn):
    demo = [
        ("Amazing Grace", "Traditional", "Hymns",
         "Amazing grace, how sweet the sound\nThat saved a wretch like me\n\nI once was lost, but now am found\nWas blind but now I see\n\n'Twas grace that taught my heart to fear\nAnd grace my fears relieved"),
        ("How Great Thou Art", "Traditional", "Hymns",
         "O Lord my God, when I in awesome wonder\nConsider all the worlds Thy hands have made\n\nThen sings my soul, my Savior God, to Thee\nHow great Thou art, how great Thou art"),
        ("Holy Forever", "Chris Tomlin", "Contemporary",
         "The sun will rise and the sun will set\nBut Your love, Your love won't run out\n\nHoly, holy, holy is the Lord\nWe worship You forever"),
        ("Build My Life", "Housefires", "Worship",
         "Worthy of every song we could ever sing\nWorthy of all the praise we could ever bring\n\nHoly, there is no one like You\nThere is none besides You"),
        ("What A Beautiful Name", "Hillsong Worship", "Praise",
         "You were the Word at the beginning\nOne with God the Lord Most High\n\nWhat a beautiful Name it is\nWhat a beautiful Name it is"),
    ]
    for title, artist, category, lyrics in demo:
        slides = [s.strip() for s in lyrics.split("\n\n") if s.strip()]
        conn.execute(
            "INSERT INTO songs(title, artist, category, tags, slides, favorite, last_used) VALUES (?,?,?,?,?,0,?)",
            (title, artist, category, "", json.dumps(slides), now())
        )
    conn.commit()


def now():
    return datetime.datetime.now().isoformat()


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


def get_song(song_id):
    conn = get_conn()
    r = conn.execute("SELECT * FROM songs WHERE id=?", (song_id,)).fetchone()
    conn.close()
    return r


def add_song(title, artist, category, tags, lyrics):
    slides = [s.strip() for s in lyrics.split("\n\n") if s.strip()] or ["(empty)"]
    conn = get_conn()
    conn.execute(
        "INSERT INTO songs(title, artist, category, tags, slides, favorite, last_used) VALUES (?,?,?,?,?,0,?)",
        (title, artist, category, tags, json.dumps(slides), now())
    )
    conn.commit()
    conn.close()


def update_song_slides(song_id, slides):
    conn = get_conn()
    conn.execute("UPDATE songs SET slides=? WHERE id=?", (json.dumps(slides), song_id))
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


# ---------------- Services ----------------

def create_service(name, service_date, service_time, items=None):
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO services(name, service_date, service_time, items, created_at) VALUES (?,?,?,?,?)",
        (name, service_date, service_time, json.dumps(items or []), now())
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
    conn.execute("UPDATE services SET items=? WHERE id=?", (json.dumps(items), service_id))
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

def item_slides(item):
    """Return list of (reference_or_none, text, secondary_text_or_None) for a service item."""
    if item["type"] == "song":
        return [(None, s, None) for s in item["slides"]]
    if item["type"] == "bible":
        return [(v["ref"], v["text"], v.get("text2")) for v in item["slides"]]
    if item["type"] in ("custom", "announcement"):
        return [(None, item["slides"][0] if item["slides"] else "", None)]
    return [(None, "", None)]


def make_song_item(song_row):
    return {
        "type": "song",
        "ref_id": song_row["id"],
        "title": song_row["title"],
        "slides": json.loads(song_row["slides"]),
    }


def make_bible_item(book, chapter, verse_nums, translation=None, secondary_translation=None):
    """
    Build a Bible service item. If secondary_translation is given, each slide
    also carries the same verse's text in that translation (looked up by the
    shared canonical book number when available, so an Arabic and an English
    translation can still be lined up even though they name books
    differently) — this is what powers the bilingual split-screen display.
    """
    verses = get_bible_verses(book, chapter, translation)
    book_number = get_book_number(book, translation) if translation else None
    slides = []
    for v in verse_nums:
        slide = {"ref": f"{book} {chapter}:{v}", "text": verses.get(v, "")}
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
            # Nested list-of-books shape
            for book_entry in data:
                book, book_number = _extract_book(book_entry, NAME_KEYS, NUMBER_KEYS)
                chapters = _first_key(book_entry, ["chapters"])
                for ch_entry in chapters:
                    chapter = _first_key(ch_entry, CHAPTER_KEYS)
                    verses = _first_key(ch_entry, ["verses"])
                    for v_entry in verses:
                        verse = _first_key(v_entry, VERSE_KEYS)
                        text = _first_key(v_entry, TEXT_KEYS)
                        if book and chapter is not None and verse is not None and text:
                            rows.append((str(book), book_number, int(chapter), int(verse), text))
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
            "INSERT INTO songs(title, artist, category, tags, slides, favorite, last_used) VALUES (?,?,?,?,?,0,?)",
            (title, artist, category, tags, json.dumps(slides), now())
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
            "INSERT INTO songs(title, artist, category, tags, slides, favorite, last_used) VALUES (?,?,?,?,?,0,?)",
            (title, artist, category, tags, json.dumps(slides), now())
        )
        count += 1
    conn.commit()
    conn.close()
    return count


# ---------------------------------------------------------------------------
# STYLES
# ---------------------------------------------------------------------------

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


def inject_css():
    render_html(f"""
    <style>    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=Manrope:wght@400;500;700&display=swap');

    html, body, [class*="css"]  {{
        font-family: 'Inter', -apple-system, sans-serif;
    }}
    .stApp {{
        background: {BG};
        color: {TEXT_PRIMARY};
    }}
    section[data-testid="stSidebar"] {{
        background: #0E0F13;
        border-right: 1px solid {CARD_BORDER};
    }}
    /* Hide the hamburger menu and "Made with Streamlit" footer, but keep the
       header bar itself — it's what holds the arrow to re-open the sidebar
       once it's been collapsed, so hiding it entirely locks that control away. */
    #MainMenu, footer {{visibility: hidden;}}
    header {{background: transparent;}}
    header [data-testid="stToolbar"] {{visibility: hidden;}}

    .ecc-wordmark {{
        font-weight: 800; font-size: 1.4rem; letter-spacing: 0.02em;
        color: {TEXT_PRIMARY};
    }}
    .ecc-wordmark span {{ color: {ACCENT}; }}

    .ecc-card {{
        background: {CARD};
        border: 1px solid {CARD_BORDER};
        border-radius: 16px;
        padding: 1.4rem 1.6rem;
        margin-bottom: 1rem;
        transition: all .15s ease;
    }}
    .ecc-card:hover {{ border-color: {ACCENT}55; }}
    .ecc-hero {{
        background: linear-gradient(135deg, #17140B, #0E0F13 70%);
        border: 1px solid {ACCENT}33;
        border-radius: 20px;
        padding: 2rem 2.2rem;
        margin-bottom: 1.5rem;
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
        transition: all .12s ease;
    }}
    .stButton>button:hover {{ border-color: {ACCENT}; color: {ACCENT}; }}
    .ecc-primary button {{
        background: {ACCENT} !important; color: #1A1400 !important; border: none !important;
    }}
    div[data-testid="stVerticalBlockBorderWrapper"] {{ border-radius: 14px; }}
    hr {{ border-color: {CARD_BORDER}; }}
    .slide-thumb {{
        border: 1px solid {CARD_BORDER}; border-radius: 10px; padding: 0.7rem;
        background: {CARD}; margin-bottom: 0.5rem; font-size: 0.82rem; color: {TEXT_MUTED};
    }}
    .slide-thumb.active {{ border-color: {ACCENT}; color: {TEXT_PRIMARY}; background: {ACCENT}14; }}
    </style>
    """)


def projector_css(theme_name):
    t = THEMES.get(theme_name, THEMES["Modern Worship"])
    render_html(f"""
    <style>
    #MainMenu, footer, header {{visibility: hidden;}}
    section[data-testid="stSidebar"] {{display:none;}}
    .block-container {{ padding: 0 !important; max-width: 100% !important; }}
    .stApp {{ background: {t['bg']}; cursor: none; }}
    .proj-wrap {{
        height: 100vh; width: 100vw; display:flex; flex-direction:column;
        align-items:center; justify-content:center; text-align:center; padding: 4vw;
    }}
    .proj-ref {{
        font-family: {t['font']}; color: {t['sub']}; letter-spacing:0.15em;
        text-transform: uppercase; font-size: clamp(1rem, 2.2vw, 2rem);
        margin-bottom: 2vh; font-weight:600;
    }}
    .proj-text {{
        font-family: {t['font']}; color: {t['fg']}; font-size: clamp(2.2rem, 5.4vw, 5.5rem);
        line-height: 1.35; font-weight: 700; white-space: pre-line;
    }}
    .proj-split {{
        height: 100vh; width: 100vw; display:flex; flex-direction:column;
    }}
    .proj-half {{
        flex: 1; display:flex; flex-direction:column; align-items:center;
        justify-content:center; text-align:center; padding: 2.5vw; overflow:hidden;
    }}
    .proj-half-top {{ border-bottom: 1px solid {t['sub']}44; }}
    .proj-text-secondary {{
        font-family: {t['font']}; color: {t['fg']}; font-size: clamp(1.6rem, 4vw, 3.6rem);
        line-height: 1.35; font-weight: 700; white-space: pre-line;
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
    state = get_state()
    projector_css(state["theme"] or "Modern Worship")

    text, ref, text2 = "", None, None
    if state["cleared"] or not state["live"]:
        text = ""
    elif state["black"]:
        text = ""
    elif state["service_id"]:
        service = get_service(state["service_id"])
        if service:
            items = json.loads(service["items"])
            idx = state["item_index"]
            if 0 <= idx < len(items):
                slides = item_slides(items[idx])
                si = state["slide_index"]
                if 0 <= si < len(slides):
                    ref, text, text2 = slides[si]

    if text2:
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
    time.sleep(1)
    st.rerun()


# ---------------------------------------------------------------------------
# SIDEBAR
# ---------------------------------------------------------------------------

def sidebar():
    with st.sidebar:
        st.markdown('<div class="ecc-wordmark">ECC <span>Worship</span></div>', unsafe_allow_html=True)
        st.caption("Prepare. Present. Worship.")
        st.markdown("###### MAIN")
        for label in ["Dashboard", "Today's Service", "Songs", "Bible", "Service Builder", "Presentation"]:
            if st.button(label, key=f"nav_{label}", use_container_width=True):
                st.session_state.page = label
        st.markdown("###### LIBRARY")
        for label in ["Song Library", "Saved Services"]:
            if st.button(label, key=f"nav_{label}", use_container_width=True):
                st.session_state.page = label
        st.markdown("###### SETTINGS")
        for label in ["Church Settings", "Display Settings"]:
            if st.button(label, key=f"nav_{label}", use_container_width=True):
                st.session_state.page = label

        st.markdown("---")
        base_url = st.session_state.get("_base_url", "")
        st.caption("Projector / extended display")
        st.code((base_url or "http://localhost:8501") + "?display=projector", language=None)
        st.caption("Open this URL in a second window on your projector, then press fullscreen.")


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
        st.session_state.page = "Songs"; st.session_state.show_add_song = True; st.rerun()
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

    with st.expander("➕ Add a song manually", expanded=st.session_state.get("show_add_song", False)):
        st.caption("Enter lyrics you or your church have the rights to use. Separate slides with a blank line.")
        t = st.text_input("Title")
        a = st.text_input("Artist")
        cat_new = st.selectbox("Category", SONG_CATEGORIES)
        tags = st.text_input("Tags (comma separated)")
        lyrics = st.text_area("Lyrics (blank line = new slide)", height=150)
        if st.button("Save Song"):
            if t.strip():
                add_song(t, a, cat_new, tags, lyrics)
                st.session_state.show_add_song = False
                st.success(f"Added '{t}'")
                st.rerun()
            else:
                st.warning("Give the song a title first.")

    st.write("")
    songs = get_songs(search, cat)
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


def page_song_workspace():
    song_id = st.session_state.get("selected_song_id")
    song = get_song(song_id) if song_id else None
    if not song:
        st.warning("No song selected."); return
    slides = json.loads(song["slides"])

    st.markdown(f"### {song['title']}")
    st.caption(f"{song['artist']} · {song['category']}")

    if st.button("← Back to Songs"):
        st.session_state.page = "Songs"; st.rerun()

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
        if st.button("🗑 Delete Slide") and len(slides) > 1:
            slides.pop(sel)
            update_song_slides(song_id, max(0, min(sel, len(slides)-1)) and slides or slides)
            st.rerun()
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
        st.markdown("**Verses**")
        chosen = st.session_state.setdefault("bible_selected_verses", [])
        for vnum, text in verses.items():
            checked = vnum in chosen
            if st.checkbox(f"{vnum}. {text}", value=checked, key=f"v_{book}_{chapter}_{vnum}"):
                if vnum not in chosen:
                    chosen.append(vnum)
            else:
                if vnum in chosen:
                    chosen.remove(vnum)

    with right:
        st.markdown("**Selected**")
        chosen_sorted = sorted(st.session_state.get("bible_selected_verses", []))
        book_number = get_book_number(book, translation) if secondary_translation else None
        if chosen_sorted:
            for v in chosen_sorted:
                st.markdown(f"**{book} {chapter}:{v}**")
                st.caption(verses[v])
                if secondary_translation:
                    st.caption("↳ " + get_verse_in_translation(book, chapter, v, secondary_translation, book_number))
        else:
            st.caption("Select verses on the left.")

        st.write("")
        if st.button("+ Add to Service", disabled=not chosen_sorted, use_container_width=True):
            st.session_state.setdefault("bible_staging", [])
            st.session_state.bible_staging.append((book, chapter, tuple(chosen_sorted), translation, secondary_translation))
            st.session_state.bible_selected_verses = []
            st.success("Added to staging — attach it in Service Builder.")
            st.rerun()

        staging = st.session_state.get("bible_staging", [])
        if staging:
            st.markdown("**Staged passages**")
            for i, (b, c, vs, tr, tr2) in enumerate(staging):
                label = f"{b} {c}:{vs[0]}" + (f"-{vs[-1]}" if len(vs) > 1 else "") + f" ({tr}" + (f" + {tr2})" if tr2 else ")")
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
            conn.execute("UPDATE services SET name=?, service_date=?, service_time=? WHERE id=?", (name, date, time_, sid))
            conn.commit(); conn.close()
            st.rerun()

    st.markdown("#### Add to the service")
    a1, a2, a3, a4 = st.columns(4)
    with a1.popover("🎵 Add Song"):
        song_options = {s["id"]: dict(s) for s in get_songs()}
        if song_options:
            pick_id = st.selectbox("Song", list(song_options.keys()),
                                    format_func=lambda i: song_options[i]["title"], key="pick_song")
            if st.button("Add", key="add_song_btn"):
                items.append(make_song_item(song_options[pick_id]))
                update_service_items(sid, items); st.rerun()
        else:
            st.caption("No songs yet — add one from the Songs tab.")
    with a2:
        staging = st.session_state.get("bible_staging", [])
        if st.button(f"📖 Add Staged Bible ({len(staging)})", disabled=not staging, use_container_width=True):
            for (b, c, vs, tr, tr2) in staging:
                items.append(make_bible_item(b, c, list(vs), tr, tr2))
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
    for i, item in enumerate(items):
        icon = {"song": "🎵", "bible": "📖", "custom": "🖼", "announcement": "📣"}.get(item["type"], "•")
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
                if b3.button("🗑", key=f"del_{i}"):
                    items.pop(i)
                    update_service_items(sid, items); st.rerun()

    if items:
        st.write("")
        st.markdown('<div class="ecc-primary">', unsafe_allow_html=True)
        if st.button("▶ START SERVICE", use_container_width=True):
            set_state(service_id=sid, item_index=0, slide_index=0, black=0, cleared=0, live=1,
                      theme=get_settings()["default_theme"])
            st.session_state.page = "Presentation"
            st.rerun()
        st.markdown('</div>', unsafe_allow_html=True)


def page_presentation():
    sid = st.session_state.get("active_service_id") or ensure_active_service()
    if not sid:
        st.info("No active service. Build one in Service Builder first.")
        return
    service = get_service(sid)
    items = json.loads(service["items"])
    state = get_state()
    if state["service_id"] != sid:
        set_state(service_id=sid, item_index=0, slide_index=0, black=0, cleared=1, live=1)
        state = get_state()

    st.markdown(f"### {service['name']}")
    st.caption(f"{service['service_date']} · {service['service_time'] or ''}")

    left, center, right = st.columns([1.2, 2.4, 1])

    with left:
        st.markdown("**Service Order**")
        for i, item in enumerate(items):
            icon = {"song": "🎵", "bible": "📖", "custom": "🖼", "announcement": "📣"}.get(item["type"], "•")
            active = " active" if i == state["item_index"] else ""
            if st.button(f"{i+1:02d} {icon} {item['title']}", key=f"go_{i}", use_container_width=True):
                set_state(item_index=i, slide_index=0, cleared=0, black=0)
                st.rerun()

    item_index = state["item_index"]
    item = items[item_index] if 0 <= item_index < len(items) else None
    slides = item_slides(item) if item else []
    slide_index = max(0, min(state["slide_index"], max(len(slides) - 1, 0)))

    with center:
        st.markdown("**Current — shown on projector**")
        theme = state["theme"] or "Modern Worship"
        t = THEMES[theme]
        cur_ref, cur_text, cur_text2 = slides[slide_index] if slides else (None, "Nothing selected", None)
        hidden = state["black"] or state["cleared"]
        if cur_text2 and not hidden:
            top_dir = "rtl" if _looks_arabic(cur_text) else "ltr"
            bottom_dir = "rtl" if _looks_arabic(cur_text2) else "ltr"
            render_html(
                f"""<div style="background:{t['bg']};border-radius:16px;overflow:hidden;
                min-height:260px;border:1px solid {CARD_BORDER};display:flex;flex-direction:column;">
                <div dir="{top_dir}" style="flex:1;padding:1.2rem;display:flex;flex-direction:column;
                align-items:center;justify-content:center;text-align:center;border-bottom:1px solid {CARD_BORDER};">
                {f'<div style="color:{t["sub"]};letter-spacing:.1em;text-transform:uppercase;margin-bottom:0.6rem;font-family:{t["font"]};font-size:0.8rem;">{cur_ref}</div>' if cur_ref else ''}
                <div style="color:{t['fg']};font-family:{t['font']};font-size:1.2rem;font-weight:700;white-space:pre-line;line-height:1.4;">{cur_text}</div>
                </div>
                <div dir="{bottom_dir}" style="flex:1;padding:1.2rem;display:flex;align-items:center;justify-content:center;text-align:center;">
                <div style="color:{t['fg']};font-family:{t['font']};font-size:1.1rem;font-weight:700;white-space:pre-line;line-height:1.4;">{cur_text2}</div>
                </div>
                </div>"""
            )
            st.caption("Bilingual split screen — top/bottom shown exactly as on the projector.")
        else:
            render_html(
                f"""<div style="background:{t['bg']};border-radius:16px;padding:2.4rem 1.6rem;
                min-height:260px;display:flex;flex-direction:column;align-items:center;justify-content:center;
                text-align:center;border:1px solid {CARD_BORDER};">
                {f'<div style="color:{t["sub"]};letter-spacing:.1em;text-transform:uppercase;margin-bottom:1rem;font-family:{t["font"]};">{cur_ref}</div>' if cur_ref else ''}
                <div style="color:{t['fg']};font-family:{t['font']};font-size:1.5rem;font-weight:700;white-space:pre-line;line-height:1.4;">{cur_text if not hidden else "(hidden from projector)"}</div>
                </div>"""
            )
        st.markdown("**Up Next**")
        nxt_ref, nxt_text, nxt_text2 = (None, "—", None)
        if slides and slide_index + 1 < len(slides):
            nxt_ref, nxt_text, nxt_text2 = slides[slide_index + 1]
        elif item_index + 1 < len(items):
            nslides = item_slides(items[item_index + 1])
            if nslides:
                nxt_ref, nxt_text, nxt_text2 = nslides[0]
            nxt_text = f"(Next item) {items[item_index + 1]['title']} — {nxt_text}"
        st.markdown(f'<div class="ecc-card">{(nxt_ref + " — ") if nxt_ref else ""}{nxt_text}{(" / " + nxt_text2) if nxt_text2 else ""}</div>', unsafe_allow_html=True)

    with right:
        st.markdown("**Controls**")
        st.caption(f"Slide {slide_index + 1 if slides else 0} / {len(slides)}")
        cp, cn = st.columns(2)
        if cp.button("◀ PREV", use_container_width=True) and slide_index > 0:
            set_state(slide_index=slide_index - 1, cleared=0); st.rerun()
        if cn.button("NEXT ▶", use_container_width=True):
            if slide_index < len(slides) - 1:
                set_state(slide_index=slide_index + 1, cleared=0); st.rerun()
            elif item_index + 1 < len(items):
                set_state(item_index=item_index + 1, slide_index=0, cleared=0); st.rerun()
        st.write("")
        if st.button("🖥 Present", use_container_width=True):
            set_state(cleared=0, black=0, live=1); st.rerun()
        if st.button("⛶ Open Presentation Display", use_container_width=True):
            st.info("Open the projector link shown in the sidebar in a second window, then drag it to your projector and press F for fullscreen there.")
        if st.button("⬛ Black Screen", use_container_width=True):
            set_state(black=1 if not state["black"] else 0); st.rerun()
        if st.button("Clear Screen", use_container_width=True):
            set_state(cleared=1); st.rerun()
        if st.button("✕ Exit Presentation", use_container_width=True):
            set_state(cleared=1, live=0)
            st.session_state.page = "Dashboard"
            st.rerun()
        st.write("")
        st.selectbox("Theme", list(THEMES.keys()), index=list(THEMES.keys()).index(theme), key="live_theme",
                     on_change=lambda: set_state(theme=st.session_state.live_theme))


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
            st.caption(f"{sum(1 for i in items if i['type']=='song')} songs · {sum(1 for i in items if i['type']=='bible')} Bible passages")
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
        st.success("Saved.")
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
            st.success(f"Imported {n} verses as '{translation_name}'. Find them in the Bible tab.")
        except Exception as e:
            st.error(f"Couldn't import that file: {e}")

    st.write("")
    st.markdown("#### Import Songs (bulk)")
    st.caption(
        "Upload a JSON or CSV file of songs you/your church have the rights to use.\n\n"
        "**JSON:** a list of `{\"title\":..., \"artist\":..., \"category\":..., \"tags\":..., \"lyrics\":\"verse one\\n\\nverse two\"}` "
        "objects (or `\"slides\": [...]` instead of `lyrics`).\n\n"
        "**CSV:** columns `title, artist, category, tags, lyrics` — since commas/newlines are awkward in CSV, "
        "separate slides inside the `lyrics` cell with `||`, e.g. `Verse line one||Verse line two||Chorus`."
    )
    song_file = st.file_uploader("Songs file (.json or .csv)", type=["json", "csv"], key="song_uploader")
    if song_file is not None and st.button("Import Songs"):
        try:
            if song_file.name.lower().endswith(".json"):
                data = json.load(song_file)
                n = import_songs_json(data)
            else:
                text = io.StringIO(song_file.getvalue().decode("utf-8"))
                n = import_songs_csv(csv.DictReader(text))
            st.success(f"Imported {n} songs. Find them in the Songs tab.")
        except Exception as e:
            st.error(f"Couldn't import that file: {e}")


def page_display_settings():
    st.markdown("### Display Settings")
    settings = get_settings()
    theme = st.selectbox("Default presentation theme", list(THEMES.keys()),
                          index=list(THEMES.keys()).index(settings["default_theme"]))
    if st.button("Save Default Theme"):
        set_settings(default_theme=theme)
        st.success("Saved.")
    st.write("")
    st.markdown("#### Preview")
    t = THEMES[theme]
    render_html(
        f"""<div style="background:{t['bg']};border-radius:16px;padding:3rem;text-align:center;">
        <div style="color:{t['sub']};text-transform:uppercase;letter-spacing:.1em;margin-bottom:.8rem;font-family:{t['font']};">JOHN 3:16</div>
        <div style="color:{t['fg']};font-size:1.6rem;font-weight:700;font-family:{t['font']};">For God so loved the world...</div>
        </div>"""
    )


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    st.set_page_config(page_title="ECC Worship", page_icon="✝", layout="wide")
    init_db()

    qp = st.query_params
    if qp.get("display") == "projector":
        render_projector()
        return

    inject_css()

    if "page" not in st.session_state:
        st.session_state.page = "Dashboard"
    if "logged_in" not in st.session_state:
        st.session_state.logged_in = False

    if not st.session_state.logged_in:
        render_login()
        return

    sidebar()

    pages = {
        "Dashboard": page_dashboard,
        "Today's Service": page_presentation,
        "Songs": page_songs,
        "Song Workspace": page_song_workspace,
        "Bible": page_bible,
        "Service Builder": page_service_builder,
        "Presentation": page_presentation,
        "Song Library": page_song_library,
        "Saved Services": page_saved_services,
        "Church Settings": page_church_settings,
        "Display Settings": page_display_settings,
    }
    pages.get(st.session_state.page, page_dashboard)()


def render_login():
    render_html(
        f"""
        <div style="min-height:80vh;display:flex;flex-direction:column;align-items:center;justify-content:center;">
        <div style="font-weight:800;font-size:2.4rem;margin-bottom:0.2rem;">ECC <span style="color:{ACCENT}">Worship</span></div>
        <div style="color:{TEXT_MUTED};margin-bottom:2rem;">Welcome to ECC — Prepare. Present. Worship.</div>
        </div>
        """
    )
    c1, c2, c3 = st.columns([1, 1, 1])
    with c2:
        st.text_input("Email", key="login_email")
        st.text_input("Password", type="password", key="login_password")
        st.markdown('<div class="ecc-primary">', unsafe_allow_html=True)
        if st.button("Sign In", use_container_width=True):
            st.session_state.logged_in = True
            st.rerun()
        st.markdown('</div>', unsafe_allow_html=True)
        st.caption("Forgot password?")


if __name__ == "__main__":
    main()

# git status ; git add . ; git commit -m "Your commit message" ; git push