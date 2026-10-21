"""romhacking.net's own database (August 2024), read from the dump on archive.org.

romhacking.sql.zip sits next to the file dump in the sign-in-only archive.org item. It has
every hack and translation with its game, version, release date, downloads, description
and "rominfo" - which ROM it's for, often with the No-Intro name and CRC32 / SHA-1. That
makes the 2024 Archive tab complete (the public backup only saved some listing pages)
and lets Vibe Patcher check a ROM against what a patch needs."""
import html
import io
import json
import os
import re
import zipfile

from paths import CACHE_DIR

SQL_URL = "https://archive.org/download/romhacking.net-20240801/romhacking.sql.zip"
DB_CACHE = os.path.join(CACHE_DIR, "rhn_db.json")
# romhacking.net console ids for Vibe Patcher's consoles (32X is filed with Genesis)
CONSOLE_IDS = {"snes": (9,), "gba": (10,), "nes": (1,), "gb": (8,), "gbc": (8,),
               "nds": (23,), "sms": (22,), "genesis": (11, 31), "n64": (27,), "gg": (12,)}
TOKEN = re.compile(r"'((?:[^'\\]|\\.)*)'|(NULL)|(-?\d+(?:\.\d+)?)|(\()|(\))", re.S)
UNESCAPE = {"n": "\n", "r": "\r", "t": "\t", "0": "\0", "'": "'", '"': '"', "\\": "\\",
            "Z": "\x1a"}

_db = None


def _rows(sql, table):
    """Every row of every INSERT INTO `table` (...) VALUES ... as a {column: value} dict."""
    head = f"INSERT INTO `{table}` ("
    start = 0
    while True:
        i = sql.find(head, start)
        if i < 0:
            return
        v = sql.find(") VALUES", i)
        cols = re.findall(r"`([^`]+)`", sql[i + len(head):v])
        row, depth, start = None, 0, v + 8
        for m in TOKEN.finditer(sql, v + 8):
            s, null, num, op, cl = m.groups()
            if op:
                depth += 1
                if depth == 1:
                    row = []
            elif cl:
                depth -= 1
                if depth == 0:
                    yield dict(zip(cols, row))
                    k = m.end()  # ",(" continues this INSERT; ";" ends it
                    while k < len(sql) and sql[k] in " \r\n\t":
                        k += 1
                    start = k
                    if k >= len(sql) or sql[k] != ",":
                        break
            elif row is not None:
                if s is not None:
                    row.append(re.sub(r"\\(.)", lambda e: UNESCAPE.get(e.group(1),
                                                                          e.group(1)), s))
                elif null:
                    row.append(None)
                else:
                    row.append(num)
        else:
            return


def _int(v, default=0):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def build(session):
    """Download and read the database; returns {platform: [entry]} and caches it."""
    r = session.get(SQL_URL, timeout=180)
    r.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        sql = z.read(z.namelist()[0]).decode("utf-8", errors="replace")
    games = {_int(g["gamekey"]): html.unescape(g["gametitle"] or "")
             for g in _rows(sql, "gamedata")}
    cats = {_int(c["categorykey"]): c["catname"] for c in _rows(sql, "Hackscat")}
    langs = {_int(l["id"]): l["name"] for l in _rows(sql, "language")}
    by_console = {}

    def add(console, e):
        for k in ("title", "game", "desc", "rominfo", "category", "language"):
            if e.get(k):  # the database stores text HTML-escaped (Diddy&#039;s)
                e[k] = html.unescape(html.unescape(e[k]))
        for p, ids in CONSOLE_IDS.items():
            if console in ids:
                by_console.setdefault(p, []).append(e)

    for h in _rows(sql, "Hacks"):
        if _int(h.get("nofile")) == 1:
            continue
        add(_int(h["consolekey"]), {
            "kind": "hack", "id": _int(h["hackkey"]), "title": h["hacktitle"],
            "game": games.get(_int(h["gamekey"]), ""),
            "category": cats.get(_int(h.get("category")), ""),
            "version": h.get("version") or "", "date": h.get("reldate") or "",
            "lastmod": h.get("lastmod") or "", "downloads": _int(h.get("downloads")),
            "desc": (h.get("description") or "")[:4000], "rominfo": h.get("rominfo") or "",
            "file": h.get("filename") or "",
            "url": f"https://www.romhacking.net/hacks/{h['hackkey']}/"})
    for t in _rows(sql, "transdata"):
        if _int(t.get("nofile")) == 1:
            continue
        game = games.get(_int(t["gamekey"]), "")
        lang = langs.get(_int(t.get("language"), 12), "")
        add(_int(t["consolekey"]), {
            "kind": "translation", "id": _int(t["transkey"]),
            "title": f"{game} - {lang} translation" if lang else game,
            "game": game, "category": "Translation", "language": lang,
            "version": t.get("patchver") or "", "date": t.get("patchrel") or "",
            "lastmod": t.get("lastmod") or "", "downloads": _int(t.get("downloads")),
            "desc": (t.get("description") or "")[:4000], "rominfo": t.get("rominfo") or "",
            "file": t.get("patchfile") or "",
            "url": f"https://www.romhacking.net/translations/{t['transkey']}/"})
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(DB_CACHE, "w", encoding="utf-8") as f:
        json.dump(by_console, f, ensure_ascii=False)
    global _db
    _db = by_console
    return by_console


def load():
    """The cached database ({platform: [entry]}), or None if not built yet."""
    global _db
    if _db is None:
        try:
            with open(DB_CACHE, encoding="utf-8") as f:
                _db = json.load(f)
        except (OSError, ValueError):
            return None
    return _db
