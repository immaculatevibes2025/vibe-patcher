"""Identify the user's ROMs by checksum against the No-Intro databases (via the
libretro-database project), to learn their exact region and version even when the file
name doesn't say - e.g. Donkey Kong Country (USA) (Rev 2), i.e. v1.2.

Also gives each ROM's MD5 the way RetroAchievements hashes it, so achievement support
can be matched exactly."""
import hashlib
import json
import os
import re
import shutil
import tempfile
import zipfile
import zlib

import requests

from paths import CACHE_DIR

DAT_URL = ("https://raw.githubusercontent.com/libretro/libretro-database/master/metadat/"
           "no-intro/{}.dat")
DATS = {
    "nes": ["Nintendo - Nintendo Entertainment System"],
    "snes": ["Nintendo - Super Nintendo Entertainment System"],
    "n64": ["Nintendo - Nintendo 64"],
    "gb": ["Nintendo - Game Boy"],
    "gbc": ["Nintendo - Game Boy Color"],
    "gba": ["Nintendo - Game Boy Advance"],
    "nds": ["Nintendo - Nintendo DS"],
    "sms": ["Sega - Master System - Mark III"],
    "genesis": ["Sega - Mega Drive - Genesis", "Sega - 32X"],
    "gg": ["Sega - Game Gear"],
}
ROM_EXTS = (".sfc", ".smc", ".nes", ".z64", ".n64", ".v64", ".gb", ".gbc", ".gba", ".nds",
            ".sms", ".md", ".gen", ".bin", ".smd", ".32x", ".gg")
IDS_FILE = os.path.join(CACHE_DIR, "rom_ids.json")
_dats = {}


def load_dat(platform):
    """{crc: name} and {md5: name} for a console, downloaded once (~1 MB) and cached."""
    if platform in _dats:
        return _dats[platform]
    cache = os.path.join(CACHE_DIR, f"nointro_{platform}.json")
    if os.path.exists(cache):
        with open(cache, encoding="utf-8") as f:
            _dats[platform] = json.load(f)
        return _dats[platform]
    by_crc, by_md5 = {}, {}
    for dat in DATS.get(platform, []):
        r = requests.get(DAT_URL.format(requests.utils.quote(dat)), timeout=60)
        r.raise_for_status()
        # one block per game; file names contain "(USA)" etc, so read each block whole
        for block in re.split(r"\ngame \(", r.text)[1:]:
            name = re.search(r'name "([^"]+)"', block)
            if not name:
                continue
            for crc, md5 in re.findall(r"crc ([0-9A-Fa-f]{8})(?:\s+md5 ([0-9A-Fa-f]{32}))?",
                                       block):
                by_crc.setdefault(crc.lower(), name.group(1))
                if md5:
                    by_md5.setdefault(md5.lower(), name.group(1))
    os.makedirs(CACHE_DIR, exist_ok=True)
    _dats[platform] = {"crc": by_crc, "md5": by_md5}
    with open(cache, "w", encoding="utf-8") as f:
        json.dump(_dats[platform], f)
    return _dats[platform]


def _rom_bytes(path):
    """The ROM's bytes - from inside a .zip / .7z / .rar if needed."""
    low = path.lower()
    if low.endswith(".zip"):
        with zipfile.ZipFile(path) as z:
            member = next(n for n in z.namelist() if n.lower().endswith(ROM_EXTS))
            return z.read(member)
    if low.endswith((".7z", ".rar")):
        import archive_tools
        tmp = tempfile.mkdtemp(prefix="vibepatcher_id_")
        try:
            archive_tools.extract(path, tmp)
            for root, _d, files in os.walk(tmp):
                for f in files:
                    if f.lower().endswith(ROM_EXTS):
                        with open(os.path.join(root, f), "rb") as fh:
                            return fh.read()
            raise OSError("no ROM inside")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    with open(path, "rb") as f:
        return f.read()


def _normalise(data, platform):
    """What No-Intro and RetroAchievements checksum: no copier / iNES header, N64 in
    big-endian (.z64) byte order."""
    if platform == "snes" and len(data) % 1024 == 512:
        data = data[512:]
    elif platform == "nes" and data[:4] == b"NES\x1a":
        data = data[16:]
    elif platform == "n64":
        from patcher import to_z64
        data = to_z64(data)[0]
    return data


def version_from_name(name):
    """'Donkey Kong Country (USA) (Rev 2)' -> ('USA', 'Rev 2 (v1.2)')."""
    region, version = "", ""
    for tag in re.findall(r"\(([^)]+)\)", name):
        if not region and re.search(r"USA|Europe|Japan|World|Australia|Korea|Brazil|Asia|"
                                    r"France|Germany|Spain|Italy|China|Taiwan", tag):
            region = tag
        m = re.match(r"Rev (\d+)$", tag)
        if m and not version:
            version = f"Rev {m.group(1)} (v1.{m.group(1)})"
        elif re.match(r"v\d+(\.\d+)*$", tag) and not version:
            version = tag
    return region, version or "Original release (v1.0)"


def identify(path, platform):
    """{'crc', 'md5', 'name', 'region', 'version'} for one ROM (name '' if unknown)."""
    data = _normalise(_rom_bytes(path), platform)
    crc = f"{zlib.crc32(data) & 0xFFFFFFFF:08x}"
    md5 = hashlib.md5(data).hexdigest()
    dat = load_dat(platform)
    name = dat["crc"].get(crc) or dat["md5"].get(md5) or ""
    info = {"crc": crc, "md5": md5, "name": name}
    if name:
        info["region"], info["version"] = version_from_name(name)
    return info


def load_ids():
    try:
        with open(IDS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def identify_all(owned_by_platform, progress=lambda *a: None):
    """Identify every owned ROM not identified yet (keyed by path, size and date, so
    changed files are redone). Returns {path: info}."""
    ids = load_ids()
    todo = []
    for platform, owned in owned_by_platform.items():
        for path in owned.values():
            try:
                st = os.stat(path)
            except OSError:
                continue
            key = f"{st.st_size}:{int(st.st_mtime)}"
            if ids.get(path, {}).get("key") != key:
                todo.append((platform, path, key))
    for i, (platform, path, key) in enumerate(todo, 1):
        try:
            info = identify(path, platform)
            info["key"] = key
            ids[path] = info
        except Exception:
            continue
        if i % 10 == 0:
            progress(f"Checking your ROMs' versions… {i} of {len(todo)}")
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(IDS_FILE, "w", encoding="utf-8") as f:
        json.dump(ids, f)
    return ids
