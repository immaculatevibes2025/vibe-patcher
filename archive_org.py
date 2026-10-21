"""archive.org sign-in, and reading single files out of the romhacking.net file dump.

romhacking.net's own dump of every hack and translation (rhdn_20240801.zip, 12.5 GB)
is only served to signed-in archive.org users. Signing in goes through archive.org's
login API (the one its `ia` command-line tool uses); only the keys and session cookies
it returns are kept - never the password.

The dump is read with HTTP range requests: just the zip's table of contents at the end,
then the bytes of the one file wanted, so nothing close to 12.5 GB is downloaded."""
import logging
import re
import threading
import zipfile

import requests

log = logging.getLogger(__name__)

RHN_DUMP = "https://archive.org/download/romhacking.net-20240801/rhdn_20240801.zip"
LOGIN_URL = "https://archive.org/services/xauthn/?op=login"
HEADERS = {"User-Agent": "VibePatcher/1.0 (+https://github.com/immaculatevibes2025)"}

CREDS = {}  # {"access", "secret", "cookies", "user"} - set by the app from its settings


class NotSignedIn(RuntimeError):
    pass


def login(email, password):
    """Sign in. Returns the credentials to store (keys + cookies, no password)."""
    r = requests.post(LOGIN_URL, data={"email": email, "password": password},
                      headers=HEADERS, timeout=30)
    try:
        data = r.json()
    except ValueError:
        raise RuntimeError(f"archive.org didn't answer the sign-in (HTTP {r.status_code}).")
    if not data.get("success"):
        reason = data.get("values", {}).get("reason") or data.get("error") or ""
        raise RuntimeError("archive.org sign-in failed"
                           + (f": {reason.replace('_', ' ')}" if reason else
                              " - check the email and password."))
    v = data["values"]
    return {"access": v["s3"]["access"], "secret": v["s3"]["secret"],
            "cookies": v.get("cookies", {}), "user": v.get("screenname") or email}


def session():
    if not CREDS.get("access"):
        raise NotSignedIn("Sign in to archive.org in Settings > Accounts to download "
                          "romhacking.net files.")
    s = requests.Session()
    s.headers.update(HEADERS)
    s.headers["Authorization"] = f"LOW {CREDS['access']}:{CREDS['secret']}"
    for k, v in (CREDS.get("cookies") or {}).items():
        s.cookies.set(k, v, domain=".archive.org")
    return s


class RangeFile:
    """A read-only, seekable file backed by HTTP range requests (1 MB blocks, cached)."""
    BLOCK = 1 << 20

    def __init__(self, url, sess):
        self.url, self.s, self.pos, self.blocks = url, sess, 0, {}
        r = self.s.get(url, headers={"Range": "bytes=0-0"}, stream=True, timeout=60)
        r.close()
        if r.status_code in (401, 403):
            raise NotSignedIn("archive.org refused the download - sign in again in "
                              "Settings > Accounts.")
        m = re.search(r"/(\d+)$", r.headers.get("Content-Range", ""))
        if r.status_code != 206 or not m:
            raise RuntimeError(f"archive.org can't serve parts of that file (HTTP {r.status_code}).")
        self.size = int(m.group(1))
        self.url = r.url  # the data server it redirected to - skips the redirect next time

    def seekable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, off, whence=0):
        self.pos = off if whence == 0 else self.pos + off if whence == 1 else self.size + off
        return self.pos

    def _block(self, i):
        if i not in self.blocks:
            if len(self.blocks) > 64:  # keep at most ~64 MB in memory
                self.blocks.pop(next(iter(self.blocks)))
            start = i * self.BLOCK
            end = min(self.size, start + self.BLOCK) - 1
            for attempt in range(3):
                try:
                    r = self.s.get(self.url, headers={"Range": f"bytes={start}-{end}"},
                                   timeout=60)
                    if r.status_code == 206:
                        self.blocks[i] = r.content
                        break
                except requests.RequestException:
                    if attempt == 2:
                        raise
            else:
                raise RuntimeError(f"archive.org stopped serving the file (HTTP {r.status_code}).")
        return self.blocks[i]

    def read(self, n=-1):
        if n is None or n < 0:
            n = self.size - self.pos
        n = max(0, min(n, self.size - self.pos))
        out = bytearray()
        while n > 0:
            b = self._block(self.pos // self.BLOCK)
            off = self.pos % self.BLOCK
            chunk = b[off:off + n]
            if not chunk:
                break
            out += chunk
            self.pos += len(chunk)
            n -= len(chunk)
        return bytes(out)

    def close(self):
        self.blocks.clear()


_zip, _zip_lock = None, threading.Lock()


def rhn_zip():
    """The dump opened as a zip (table of contents read once per run)."""
    global _zip
    with _zip_lock:
        if _zip is None:
            _zip = zipfile.ZipFile(RangeFile(RHN_DUMP, session()))
            names = _zip.namelist()
            log.info("romhacking.net dump: %d files, e.g. %s", len(names), names[:15])
        return _zip


def reset():
    """Forget the open dump (after signing in or out)."""
    global _zip, _rhn_index
    with _zip_lock:
        _zip, _rhn_index = None, None


_rhn_index = None
PATCH_FILE = re.compile(r"^(hacks|translations)/[^/]+/patches/\[(\d+)\]")


def rhn_members(kind, rid):
    """The dump's patch files for a romhacking.net hack / translation id. They're
    stored as hacks/<console>/patches/[1204]SuperMetroid-HallowEve.rar."""
    global _rhn_index
    z = rhn_zip()
    if _rhn_index is None:
        idx = {}
        for info in z.infolist():
            m = PATCH_FILE.match(info.filename)
            if m and not info.is_dir():
                key = ("hack" if m.group(1) == "hacks" else "translation", m.group(2))
                idx.setdefault(key, []).append(info)
        _rhn_index = idx
    return _rhn_index.get((kind, str(rid)), [])


def display_name(filename):
    """'hacks/snes/patches/[1204]SuperMetroid-HallowEve.rar' -> 'SuperMetroid-HallowEve.rar'"""
    return re.sub(r"^\[\d+\]", "", filename.rsplit("/", 1)[-1])


def extract_member(info, dest_path, progress=lambda *a: None):
    """Copy one file out of the dump to dest_path."""
    z = rhn_zip()
    done, total = 0, info.file_size
    with z.open(info) as src, open(dest_path, "wb") as out:
        while chunk := src.read(1 << 18):
            out.write(chunk)
            done += len(chunk)
            progress(f"Downloading {info.filename.rsplit('/', 1)[-1]}… "
                     f"{done / 1048576:.1f} of {total / 1048576:.1f} MB", done, total)
    return dest_path
