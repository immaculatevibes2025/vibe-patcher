"""Scan folders for the user's own ROMs (SNES: .sfc / .smc, GBA: .gba, or a .zip
holding one) and match them to LaunchBox games by name."""
import os
import re
import tempfile
import unicodedata
import zipfile

PLATFORM_EXTS = {
    "snes": (".sfc", ".smc"), "gba": (".gba",), "nes": (".nes",), "gb": (".gb",),
    "gbc": (".gbc",), "nds": (".nds",), "sms": (".sms",),
    "genesis": (".md", ".gen", ".bin", ".smd", ".32x"),
    "n64": (".z64", ".n64", ".v64"), "gg": (".gg",),
}
ROM_EXTS = tuple(e for exts in PLATFORM_EXTS.values() for e in exts)


def norm(name):
    """'Legend of Zelda, The - A Link to the Past (USA) [!]' -> 'legendofzeldaalinktothepast'"""
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()  # é -> e
    name = re.sub(r"[\(\[][^\)\]]*[\)\]]", "", name)  # drop (USA), [!], (Rev 1)...
    name = re.sub(r",\s*the\b", "", name, flags=re.I)  # "Zelda, The"
    name = re.sub(r"^the\s+", "", name.strip(), flags=re.I)
    name = name.replace("&", "and")
    return re.sub(r"[^a-z0-9]", "", name.lower())


ARCHIVES = (".zip", ".7z", ".rar")


def archive_members(path):
    """File names inside a .zip / .7z / .rar (7z and rar are listed with Windows'
    built-in tar, without unpacking)."""
    low = path.lower()
    try:
        if low.endswith(".zip"):
            with zipfile.ZipFile(path) as z:
                return z.namelist()
        if low.endswith((".7z", ".rar")):
            import archive_tools
            return archive_tools.list_members(path)
    except (zipfile.BadZipFile, OSError):
        pass
    return []


def scan(folders, games, progress=lambda msg: None, platform="snes"):
    """Return {game_id: rom_path} for every ROM of this console that matches a game."""
    exts = PLATFORM_EXTS[platform]
    by_name = {}
    for g in games:
        by_name.setdefault(norm(g["name"]), g["id"])
    owned = {}
    count = 0
    for folder in folders:
        for root, _dirs, files in os.walk(folder):
            for f in files:
                low = f.lower()
                if not low.endswith(exts + ARCHIVES):
                    continue
                path = os.path.join(root, f)
                names = [os.path.splitext(f)[0]]
                if low.endswith(ARCHIVES):
                    roms = [m for m in archive_members(path) if m.lower().endswith(exts)]
                    if not roms:
                        continue  # an archive without a ROM for this console
                    # the ROM's own name inside is a second chance to match
                    names.append(os.path.splitext(os.path.basename(roms[0]))[0])
                count += 1
                if count % 50 == 0:
                    progress(f"Scanning… {count} ROMs found")
                gid = None
                for n in names:
                    gid = by_name.get(norm(n))
                    if gid is None and " - " in n:  # "Title - Subtitle" vs "Title: Subtitle"
                        gid = by_name.get(norm(n.replace(" - ", " ")))
                    if gid:
                        break
                if gid and gid not in owned:
                    owned[gid] = path
    progress(f"Scan done: {count} ROMs, {len(owned)} matched to games")
    return owned


def related_ids(owned_ids, games):
    """Owned games plus hack entries built on them (e.g. 'Super Metroid Redux' when
    you own 'Super Metroid')."""
    owned_names = [norm(g["name"]) for g in games if g["id"] in owned_ids]
    owned_names = [n for n in owned_names if len(n) >= 4]
    keep = set(owned_ids)
    for g in games:
        n = norm(g["name"])
        if any(n.startswith(o) for o in owned_names):
            keep.add(g["id"])
    return keep


def unpack_rom(path):
    """If path is a .zip / .7z / .rar, extract the ROM inside to a temp folder and
    return its path; otherwise return path unchanged."""
    low = path.lower()
    if not low.endswith(ARCHIVES):
        return path
    member = next(n for n in archive_members(path) if n.lower().endswith(ROM_EXTS))
    tmp = tempfile.mkdtemp(prefix="vibepatcher_")
    out = os.path.join(tmp, os.path.basename(member))
    if low.endswith(".zip"):
        with zipfile.ZipFile(path) as z, z.open(member) as src, open(out, "wb") as dst:
            dst.write(src.read())
    else:
        import archive_tools
        try:
            archive_tools.extract(path, tmp, member)
        except RuntimeError:
            archive_tools.extract(path, tmp)  # some tools want the whole archive
        found = [os.path.join(rt, f) for rt, _d, fs in os.walk(tmp) for f in fs
                 if f.lower().endswith(ROM_EXTS)]
        if not found:
            raise OSError(f"Couldn't unpack the ROM from {os.path.basename(path)}")
        out = found[0]
    return out
