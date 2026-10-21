"""Open .7z and .rar archives with whatever can read them, trying each tool in turn:

- UnRAR.exe (bundled with the portable build, or from an installed WinRAR) - reads
  every RAR, including old RAR files that use "filters"
- 7-Zip (if installed)
- Windows 10/11's built-in tar (libarchive) - reads most .7z / .rar, but not RAR
  filters ("Parsing filters is unsupported")

UnRAR may be redistributed on its own: WinRAR's licence (3a) allows distributing
"the UnRAR components" separately."""
import os
import shutil
import subprocess
import sys

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _here(*parts):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, *parts)


def _tools(path):
    """[(kind, exe)] that exist, in the order to try them for this archive."""
    win = os.environ.get("SystemRoot", r"C:\Windows")
    unrar = [_here("assets", "tools", "UnRAR.exe"),
             r"C:\Program Files\WinRAR\UnRAR.exe", r"C:\Program Files (x86)\WinRAR\UnRAR.exe"]
    seven = [r"C:\Program Files\7-Zip\7z.exe", r"C:\Program Files (x86)\7-Zip\7z.exe",
             shutil.which("7z") or ""]
    tar = [os.path.join(win, "System32", "tar.exe")]
    if path.lower().endswith(".rar"):
        order = [("unrar", unrar), ("7z", seven), ("tar", tar)]
    else:  # .7z (and anything else): tar handles it well, 7-Zip as backup
        order = [("tar", tar), ("7z", seven)]
    found = []
    for kind, exes in order:
        exe = next((e for e in exes if e and os.path.exists(e)), None)
        if exe:
            found.append((kind, exe))
    return found


def _cmd(kind, exe, path, dest, member=None):
    if kind == "unrar":
        return [exe, "x", "-y", "-idq", "-o+", path] + ([member] if member else []) + \
            [dest + os.sep]
    if kind == "7z":
        return [exe, "x", "-y", f"-o{dest}", path] + ([member] if member else [])
    return [exe, "-xf", path, "-C", dest] + ([member] if member else [])


def extract(path, dest, member=None):
    """Extract the archive (or one member) into dest, trying each tool until one works.
    Raises RuntimeError with every tool's complaint if none can."""
    tools = _tools(path)
    if not tools:
        raise RuntimeError("Opening .7z / .rar files needs Windows 10/11, 7-Zip or WinRAR")
    errors = []
    for kind, exe in tools:
        shutil.rmtree(dest, ignore_errors=True)
        os.makedirs(dest, exist_ok=True)
        r = subprocess.run(_cmd(kind, exe, path, dest, member), capture_output=True,
                           text=True, errors="replace", creationflags=NO_WINDOW)
        got_files = any(files for _r, _d, files in os.walk(dest))
        if r.returncode == 0 and got_files:
            return kind
        errors.append(f"{kind}: {(r.stderr or r.stdout).strip()[:120] or 'failed'}")
    raise RuntimeError(f"Couldn't open {os.path.basename(path)} - " + "; ".join(errors))


def list_members(path):
    """File names inside a .7z / .rar without extracting (tar or 7-Zip / UnRAR)."""
    for kind, exe in _tools(path):
        if kind == "tar":
            cmd = [exe, "-tf", path]
        elif kind == "7z":
            cmd = [exe, "l", "-ba", "-slt", path]
        else:
            cmd = [exe, "lb", path]
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", creationflags=NO_WINDOW)
        if r.returncode != 0:
            continue
        lines = [l.strip() for l in r.stdout.splitlines() if l.strip()]
        if kind == "7z":
            lines = [l[7:] for l in lines if l.startswith("Path = ")]
        if lines:
            return lines
    return []
