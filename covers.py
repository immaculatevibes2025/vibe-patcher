"""Game lists + front box covers from the LaunchBox Games Database.

The database is a free ~100 MB Metadata.zip (no account needed). It is downloaded
once, the entries for each supported console are extracted to small JSON caches,
and the zip is deleted.
"""
import io
import threading
import json
import os
import zipfile
import xml.etree.ElementTree as ET

import requests

from paths import CACHE_DIR, COVER_CACHE

METADATA_URL = "https://gamesdb.launchbox-app.com/Metadata.zip"
IMAGE_URL = "https://images.launchbox-app.com/"
# console tab -> the LaunchBox platform(s) it shows
PLATFORMS = {
    "snes": ["Super Nintendo Entertainment System"],
    "gba": ["Nintendo Game Boy Advance"],
    "nes": ["Nintendo Entertainment System"],
    "gb": ["Nintendo Game Boy"],
    "gbc": ["Nintendo Game Boy Color"],
    "nds": ["Nintendo DS"],
    "sms": ["Sega Master System"],
    "genesis": ["Sega Genesis", "Sega 32X"],
    "n64": ["Nintendo 64"],
    "gg": ["Sega Game Gear"],
}
COVER_DIR = os.path.join(CACHE_DIR, "covers")
REGION_PREF = ["North America", "United States", "World", None, "Europe", "Japan"]
HEADERS = {"User-Agent": "Vibe-Patcher/1.0"}


def games_json(platform):
    return os.path.join(CACHE_DIR, f"{platform}_games.json")


def load_games(platform="snes"):
    if os.path.exists(games_json(platform)):
        with open(games_json(platform), encoding="utf-8") as f:
            return json.load(f)
    return None


def needs_rebuild(games):
    """Older caches miss fields newer features need (release type, date, votes)."""
    return bool(games) and any(k not in games[0] for k in ("type", "votes", "rating"))


_build_lock = threading.Lock()  # one LaunchBox download / rebuild at a time


ALT_MARKER = os.path.join(CACHE_DIR, "lists_have_alt_names")


def build_game_list(progress=lambda msg: None, local_zip=None, platform="snes"):
    """Build the lists for every supported console; return the one for platform."""
    with _build_lock:
        return _build_game_list(progress, local_zip, platform)


def _build_game_list(progress, local_zip, platform):
    os.makedirs(CACHE_DIR, exist_ok=True)
    zip_path = os.path.join(CACHE_DIR, "Metadata.zip")
    if local_zip:
        zip_path = local_zip
    else:
        _download(zip_path, progress)
    return _parse(zip_path, progress, delete=not local_zip)[platform]


def _download(zip_path, progress):
    progress("Downloading LaunchBox database...")
    with requests.get(METADATA_URL, stream=True, timeout=60, headers=HEADERS) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length", 0))
        done = 0
        with open(zip_path, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
                done += len(chunk)
                if total:
                    progress(f"Downloading LaunchBox database... {done * 100 // total}%")


def _parse(zip_path, progress, delete=True):
    progress("Reading the game lists...")
    by_name = {name: key for key, names in PLATFORMS.items() for name in names}
    games, covers, alts = {}, {}, {}
    with zipfile.ZipFile(zip_path) as z, z.open("Metadata.xml") as xf:
        for _ev, el in ET.iterparse(io.BufferedReader(xf)):
            if el.tag == "Game":
                plat = by_name.get(el.findtext("Platform"))
                if plat:
                    gid = el.findtext("DatabaseID")
                    games[gid] = {"id": gid, "name": el.findtext("Name") or "?",
                                  "platform": plat,
                                  "year": (el.findtext("ReleaseDate") or "")[:4],
                                  "date": (el.findtext("ReleaseDate") or "")[:10],
                                  "votes": int(el.findtext("CommunityRatingCount") or 0),
                                  "rating": round(float(el.findtext("CommunityRating") or 0), 2),
                                  "type": el.findtext("ReleaseType") or ""}
                el.clear()
            elif el.tag == "GameAlternateName":  # "Final Fantasy III" for FF VI etc.
                gid, alt = el.findtext("DatabaseID"), el.findtext("AlternateName")
                if gid and alt:
                    alts.setdefault(gid, []).append(alt)
                el.clear()
            elif el.tag == "GameImage":
                if el.findtext("Type") == "Box - Front":
                    gid = el.findtext("DatabaseID")
                    region = el.findtext("Region")
                    rank = REGION_PREF.index(region) if region in REGION_PREF else 99
                    best = covers.get(gid)
                    if best is None or rank < best[0]:
                        covers[gid] = (rank, el.findtext("FileName"))
                el.clear()
    if delete:
        os.remove(zip_path)

    result = {p: [] for p in PLATFORMS}
    for gid, g in games.items():
        if gid in covers:
            g["cover"] = covers[gid][1]
        if gid in alts:
            g["alt"] = sorted(set(alts[gid]) - {g["name"]})
        result[g["platform"]].append(g)
    for plat, lst in result.items():
        lst.sort(key=lambda g: g["name"].lower())
        with open(games_json(plat), "w", encoding="utf-8") as f:
            json.dump(lst, f)
    with open(ALT_MARKER, "w", encoding="utf-8") as f:
        f.write("1")
    return result


def has_alt_names():
    """Were the lists made with alternate names? (older lists need one rebuild)"""
    return os.path.exists(ALT_MARKER)


# covers live next to the app (rompatcher/cache/covers), shrunk to the largest size the
# app shows - far smaller than LaunchBox's originals and quicker to load
APP_COVER_DIR = COVER_CACHE
OLD_COVER_DIR = COVER_DIR  # where full-size covers used to be saved
MAX_COVER = (600, 800)


def _save_small(src_bytes_or_path, dest):
    from PIL import Image
    im = Image.open(src_bytes_or_path)
    im = im.convert("RGB")
    im.thumbnail(MAX_COVER)
    tmp = dest + ".part"
    im.save(tmp, "JPEG", quality=90, optimize=True)
    os.replace(tmp, dest)


def cover_path(game):
    """Local path to the game's front cover - cached in the app folder, downloaded and
    shrunk the first time (or None)."""
    fn = game.get("cover")
    if not fn:
        return None
    os.makedirs(APP_COVER_DIR, exist_ok=True)
    cid = game.get("cover_id", game["id"])
    local = os.path.join(APP_COVER_DIR, cid + ".jpg")
    if os.path.exists(local):
        return local
    old = os.path.join(OLD_COVER_DIR, cid + os.path.splitext(fn)[1])
    try:
        if os.path.exists(old):  # convert an already-downloaded full-size cover
            _save_small(old, local)
            os.remove(old)
            return local
        # a LaunchBox image file name, or a full address (e.g. a hack's own box art)
        r = requests.get(fn if fn.startswith("http") else IMAGE_URL + fn, timeout=20,
                         headers=HEADERS)
        if r.status_code != 200:
            return None
        _save_small(io.BytesIO(r.content), local)
    except Exception:
        return None
    return local


def migrate_old_covers(progress=lambda *a: None):
    """Shrink and move every cover from the old cache in one background pass."""
    if not os.path.isdir(OLD_COVER_DIR):
        return 0
    os.makedirs(APP_COVER_DIR, exist_ok=True)
    names = os.listdir(OLD_COVER_DIR)
    done = 0
    for i, n in enumerate(names):
        src = os.path.join(OLD_COVER_DIR, n)
        dest = os.path.join(APP_COVER_DIR, os.path.splitext(n)[0] + ".jpg")
        try:
            if not os.path.exists(dest):
                _save_small(src, dest)
            os.remove(src)
            done += 1
        except Exception:
            pass
        if i % 200 == 0:
            progress(f"Moving the cover cache… {i} of {len(names)}")
    try:
        os.rmdir(OLD_COVER_DIR)
    except OSError:
        pass
    return done


ETAG_FILE = os.path.join(CACHE_DIR, "launchbox_etag.txt")


def update_if_changed(progress=lambda *a: None):
    """Rebuild the game lists only when LaunchBox has published a new database (checked
    with its ETag, so an unchanged database costs one tiny request, not 100 MB).
    Returns {platform: number of new games} or None if nothing changed."""
    head = requests.head(METADATA_URL, timeout=30, headers=HEADERS)
    tag = head.headers.get("ETag") or head.headers.get("Last-Modified") or ""
    try:
        old = open(ETAG_FILE, encoding="utf-8").read().strip()
    except OSError:
        old = ""
    if tag and tag == old:
        return None
    if not all(load_games(p) for p in PLATFORMS):
        return None  # first run: the start-up build is fetching LaunchBox already
    if not old:
        # first check with lists already built: remember the version, no 100 MB download
        with open(ETAG_FILE, "w", encoding="utf-8") as f:
            f.write(tag)
        return None
    before = {p: {g["id"] for g in (load_games(p) or [])} for p in PLATFORMS}
    build_game_list(progress)
    with open(ETAG_FILE, "w", encoding="utf-8") as f:
        f.write(tag)
    if not old:  # first check: the lists were just built from scratch, nothing "new"
        return {}
    return {p: len({g["id"] for g in load_games(p) or []} - before[p]) for p in PLATFORMS}
