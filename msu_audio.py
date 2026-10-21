"""MSU-1 music: find the .pcm audio tracks the user downloaded (loose, or inside a
.zip / .7z / .rar) and install them next to the patched ROM with the names MSU-1
needs: <rom name>-<track>.pcm, plus an <rom name>.msu file.

MSU-1 packs often ship the patch and the music as separate downloads, so this saves
copying and renaming dozens of tracks by hand."""
import os
import re
import shutil
import tempfile
import time
import zipfile

ARCHIVES = (".zip", ".7z", ".rar")
TRACK = re.compile(r"-(\d+)\.pcm$", re.I)   # name-12.pcm -> track 12


def _words(text):
    stop = {"the", "of", "a", "and", "msu", "msu1", "1", "pcm", "audio", "music", "pack",
            "tracks", "snes", "sfc", "zip", "7z", "rar", "v1", "final"}
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in stop and len(w) > 1}


def _pcm_members(path):
    """.pcm file names inside an archive ([] if none / unreadable)."""
    try:
        if path.lower().endswith(".zip"):
            with zipfile.ZipFile(path) as z:
                names = z.namelist()
        else:
            import archive_tools
            names = archive_tools.list_members(path)
    except Exception:
        return []
    return [n for n in names if n.lower().endswith(".pcm")]


def find_audio(folders, hints, since=None, max_age_hours=72):
    """The best audio pack in the folders: {'path', 'kind' ('archive'|'loose'), 'tracks'}.
    Prefers packs whose name shares words with the hints (game / hack / patch names),
    then the newest. Only looks at files newer than `since` (if given) or than
    max_age_hours, so old downloads aren't picked up by mistake."""
    want = set()
    for h in hints:
        want |= _words(h or "")
    cutoff = since if since else time.time() - max_age_hours * 3600
    best = None
    for folder in folders:
        if not folder or not os.path.isdir(folder):
            continue
        loose = {}
        for entry in os.scandir(folder):
            if not entry.is_file():
                continue
            name = entry.name
            low = name.lower()
            try:
                mtime = entry.stat().st_mtime
            except OSError:
                continue
            if mtime < cutoff:
                continue
            if low.endswith(".pcm"):  # loose tracks: group by their common prefix
                prefix = TRACK.sub("", name)
                loose.setdefault(prefix, []).append((entry.path, mtime))
                continue
            if low.endswith(ARCHIVES) and not low.endswith((".crdownload", ".part")):
                tracks = _pcm_members(entry.path)
                if tracks:
                    score = len(want & _words(name + " " + " ".join(tracks[:3])))
                    cand = (score, mtime, {"path": entry.path, "kind": "archive",
                                           "tracks": len(tracks)})
                    best = max(best, cand, key=lambda c: (c[0], c[1])) if best else cand
        for prefix, files in loose.items():
            score = len(want & _words(prefix))
            mtime = max(m for _p, m in files)
            cand = (score, mtime, {"path": [p for p, _m in files], "kind": "loose",
                                   "tracks": len(files)})
            best = max(best, cand, key=lambda c: (c[0], c[1])) if best else cand
    if best is None:
        return None
    # with nothing in the name to go on, only trust something downloaded very recently
    if best[0] == 0 and best[1] < time.time() - 6 * 3600:
        return None
    return best[2]


def install(audio, msu_folder, rom_path):
    """Put the tracks into msu_folder named after the patched ROM. Returns the number of
    tracks installed."""
    stem = os.path.splitext(os.path.basename(rom_path))[0]
    tmp = None
    try:
        if audio["kind"] == "archive":
            tmp = tempfile.mkdtemp(prefix="vibepatcher_msu_")
            src = audio["path"]
            if src.lower().endswith(".zip"):
                with zipfile.ZipFile(src) as z:
                    for n in z.namelist():
                        if n.lower().endswith((".pcm", ".msu")):
                            out = os.path.join(tmp, os.path.basename(n))
                            with z.open(n) as fsrc, open(out, "wb") as fdst:
                                shutil.copyfileobj(fsrc, fdst, 1 << 20)
            else:
                import archive_tools
                archive_tools.extract(src, tmp)
            files = [os.path.join(r, f) for r, _d, fs in os.walk(tmp) for f in fs]
        else:
            files = list(audio["path"])
        count = 0
        for f in files:
            name = os.path.basename(f)
            m = TRACK.search(name)
            if name.lower().endswith(".pcm") and m:
                dest = os.path.join(msu_folder, f"{stem}-{int(m.group(1))}.pcm")
                if audio["kind"] == "archive":
                    shutil.move(f, dest)
                else:
                    shutil.copyfile(f, dest)
                count += 1
        msu = os.path.join(msu_folder, stem + ".msu")
        if count and not os.path.exists(msu):
            # the data file can be empty for music-only MSU-1 hacks; use the pack's if any
            packed = next((f for f in files if f.lower().endswith(".msu")
                           and os.path.exists(f)), None)
            if packed:
                shutil.copyfile(packed, msu)
            else:
                open(msu, "wb").close()
        return count
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


def has_audio(folder):
    return any(f.lower().endswith(".pcm") for f in os.listdir(folder))
