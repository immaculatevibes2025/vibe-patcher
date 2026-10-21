"""Patch sources: GitHub and archive.org. Only patch files and archives are offered,
never ROM images."""
import json
import logging
import os
import re
import shutil
import unicodedata
import time
import zipfile

import requests
from bs4 import BeautifulSoup

from patcher import PATCH_EXTS
from paths import CACHE_DIR, DOWNLOAD_DIR

log = logging.getLogger(__name__)
ARCHIVE_EXTS = (".zip", ".7z", ".rar")
ROM_EXTS = (".sfc", ".smc", ".fig", ".swc", ".bin", ".rom", ".gba", ".nes", ".gb", ".gbc",
            ".nds", ".sms", ".md", ".gen", ".smd", ".32x", ".z64", ".n64", ".v64", ".gg")
MAX_ARCHIVE = 25 * 1024 * 1024  # skip big archives (usually ROM sets)
# repos / pages that are tools or PC ports, not patches for the console game
NOT_A_PATCH = re.compile(
    r"\b(pc[- ]port|native port|port(ed)? (to|for) (pc|windows|mac|linux|android|switch)|"
    r"decomp(ilation)?|recomp(ilation|iled)?|reverse[- ]engineer\w*|source port|"
    r"engine|editor|tool(s|kit|set)?|utilit(y|ies)|patcher|disassembl\w*|assembler|"
    r"compiler|emulator|tracker|save[- ]editor|library|sdk|documentation|docs|yaml|"
    r"companion|launcher|installer|plugin|framework|scripts?|discord|bot|"
    r"windows|macos|mac os|linux|android|ios|extract(or|ion|ing)?|ripper|converter|"
    r"viewer|inspector|dumper|unpacker|packer|compressor|decompressor|texture|model|"
    r"sprite sheet|font tool|music editor|level editor|map editor|debug(ger|ging| menu)?|"
    r"mod manager|mod loader|mod organi[sz]er|modding|cheat engine|trainer|"
    r"fps unlock|dx11|dx12|directx|vulkan|steam|pixel remaster|godot|unity|unreal|"
    r"base ?rom|template|boilerplate|uberasm|retry system|resources?|"
    # security / bug-bounty material - whole phrases only, so game titles with
    # "bug", "bounty" or "hunter" in them (Bounty Hunter, Bug!) are left alone
    r"bug ?bount(y|ies)|bounty hunters? (methodology|tips|tools|reports?)|hackerone|"
    r"bugcrowd|pen ?test(ing|er)?|penetration test\w*|red team\w*|osint|recon(naissance)?|"
    r"subdomains?|xss|sqli|sql injection|cve-?\d*|exploit(s|ation)?|vulnerabilit(y|ies)|"
    r"payloads?|wordlists?|ctf|infosec|cyber ?security|disclosed reports?|"
    r"sap applications?|command snippets|"
    # code collections for hack makers - not "ASM hack", which is a playable hack
    r"(asm|assembly) (patches|routines|library|snippets|code collection|source code)|"
    r"asm patch collection|patches for hackers|"
    # "contract" only as smart-contract / legal material, never a game title word
    r"smart contracts?|solidity|blockchain|ethereum|nft|"
    # machine-learning / NLP projects that use "translation" in another sense
    r"machine translation|neural|contrastive|transformer|dataset|pytorch|tensorflow)\b",
    re.I)


def is_tool_or_port(text):
    """Check names like 'WindWakerDebugMenu' or 'retry-system' word by word."""
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    return bool(NOT_A_PATCH.search(re.sub(r"[-_.]+", " ", text)))
# downloadable files that are PC builds or tools rather than patches
PORT_FILE = re.compile(
    r"(windows|win32|win64|\bwin\b|macos|\bmac\b|osx|linux|x86|x64|amd64|arm64|android|"
    r"\.exe$|\.dmg$|\.appimage$|\.apk$|\.msi$|\.deb$|source ?code|src\.zip$|"
    r"extractor|ripper|converter|viewer|editor|tool)", re.I)


def is_patch_file(name):
    return not PORT_FILE.search(os.path.basename(name))


HACK_WORDS = ("hack", "patch", "translat", "msu", "romhack", "restoration", "fix",
              "improve", "redux", "mod ", "remix")
HEADERS = {"User-Agent": "Vibe-Patcher/1.0"}
BROWSER_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Vibe-Patcher/1.0"}


def _clean_title(name):
    # "Final Fantasy III" -> keep main words, drop subtitles/punctuation
    name = re.split(r"[:(\[]", name)[0]
    return re.sub(r"[^\w\s']", " ", name).strip()


def _search_phrase(name):
    """What to search for: the subtitle when there is one ("The Legend of Zelda: A Link
    to the Past" -> "A Link to the Past"), since the series name alone matches every
    game in the series."""
    name = re.sub(r"[\(\[][^\)\]]*[\)\]]", "", name)
    parts = [p.strip() for p in name.split(":") if p.strip()]
    # use the subtitle alone only if it says something by itself: "A Link to the Past"
    # yes, a lone word like "Hunters" (Metroid Prime: Hunters) no - that matches anything
    sub_words = [w for w in re.findall(r"[a-z0-9]+", parts[-1].lower()) if w not in
                 ("the", "of", "a", "an", "and", "to", "in", "on", "for")] if parts else []
    phrase = parts[-1] if len(parts) > 1 and len(sub_words) >= 2 else " ".join(parts)
    return re.sub(r"[^\w\s']", " ", phrase).strip()


ROMAN = {"ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6", "vii": "7", "viii": "8",
         "ix": "9", "x": "10"}
GENERIC = {"the", "of", "a", "an", "and", "to", "in", "on", "for", "version", "edition"}


def _split_words(text):
    """Words of a repo name / description: splits camelCase, dashes and underscores."""
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    words = set(re.findall(r"[a-z0-9]+", text.lower()))
    return words | {ROMAN[w] for w in words if w in ROMAN}


def about_game(text, game_name):
    """Does this result actually name the game? It must contain every word of the title
    (the subtitle, for "Series: Subtitle" names) or its usual abbreviation (alttp, ff6).
    Keeps out other games of a series - Wind Waker results for A Link to the Past."""
    words = _split_words(text)
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    phrase = _search_phrase(game_name).lower()
    phrase = unicodedata.normalize("NFKD", phrase).encode("ascii", "ignore").decode()
    # a numbered sequel isn't the game: "Super Mario World 2" for "Super Mario World"
    title_words = re.findall(r"[a-z0-9]+", phrase)
    if title_words and re.search(r"\b" + r"\W+".join(title_words) +
                                 r"\W+(\d|ii|iii|iv|vi|vii|viii)\b", plain):
        return False
    need = [ROMAN.get(w, w) for w in title_words if w not in GENERIC]
    # a title word may also be two neighbouring words written apart ("FireRed" vs
    # "Fire Red"), but never part of a longer word ("Contra" is not "contrastive")
    order = re.findall(r"[a-z0-9]+", re.sub(r"([a-z])([A-Z])", r"\1 \2", text).lower())
    pairs = {a + b for a, b in zip(order, order[1:])}
    if need and all(w in words or w in pairs for w in need):
        return True
    every = title_words
    abbrevs = {"".join(w[0] for w in every), "".join(w[0] for w in every if w not in GENERIC)}
    if every and every[0] in ("a", "an", "the"):  # "A Link to the Past" -> lttp too
        abbrevs.add("".join(w[0] for w in every[1:]))
    full = re.findall(r"[a-z0-9]+", re.sub(r"[\(\[][^\)\]]*[\)\]]", "", game_name.lower()))
    if full and full[-1] in ROMAN:  # "final fantasy vi" -> ff6 / ffvi
        stem = "".join(w[0] for w in full[:-1])
        abbrevs |= {stem + full[-1], stem + ROMAN[full[-1]]}
    abbrevs = {a for a in abbrevs if len(a) >= 3}
    return any(any(w.startswith(a) for w in words) for a in abbrevs)


GITHUB_TOKEN = ""  # set from Settings


def classify(entry):
    """Sort a result into 'msu1', 'translation' or 'hack'."""
    text = f"{entry['title']} {entry.get('desc', '')}".lower()
    # MSU-1 only when the title says so; many hacks merely mention optional MSU-1 support
    if re.search(r"msu[-_ ]?1|\bmsu\b", entry["title"].lower()):
        return "msu1"
    if re.search(r"translat|english|\bfan[- ]?tl\b|\(en\b|spanish|french|german|"
                 r"portugu|italian|russian|retransl|\bt-en\b", text):
        return "translation"
    return "hack"


def _gh_headers():
    h = {"Accept": "application/vnd.github+json", **HEADERS}
    tok = GITHUB_TOKEN or os.environ.get("GITHUB_TOKEN")
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


BIG_PATCH_CONSOLES = ("n64", "nds")  # their patches (xdelta of a 32-512 MB ROM) are big


def _is_offerable(name, size=0, platform=None):
    n = name.lower()
    if n.endswith(ROM_EXTS):
        return False
    if n.endswith(PATCH_EXTS):
        return True
    limit = 250 * 1024 * 1024 if platform in BIG_PATCH_CONSOLES + ("github",) \
        else MAX_ARCHIVE
    return n.endswith(ARCHIVE_EXTS) and (not size or size <= limit)


# words that name a console: a repo for another console's game isn't ours
SYSTEM_WORDS = {
    "snes": r"snes|super nintendo|super famicom|sfc|msu", "gba": r"gba|game ?boy advance",
    "nes": r"nes|famicom|nintendo entertainment system", "gb": r"game ?boy|\bgb\b",
    "gbc": r"game ?boy colou?r|gbc", "nds": r"nds|nintendo ds|\bds\b",
    "sms": r"master system|sms", "genesis": r"genesis|mega ?drive|32x",
    "n64": r"n64|nintendo 64|ultra ?64", "gg": r"game ?gear",
}
OTHER_SYSTEMS = (r"\b(switch|nintendo switch|gamecube|game cube|\bgcn?\b|wii|wii ?u|3ds|"
                 r"ps[1-5x]|psp|playstation|xbox|dreamcast|saturn|steam|pc version)\b")
# a subtitle after the title means another game of the series ("Super Smash Bros. Melee")
SEQUEL_WORDS = r"(ultimate|melee|brawl|for wii|for 3ds|deluxe|odyssey|sunshine|galaxy|3d land|" \
               r"3d world|wonder|origins|remastered|hd|dx)"


def other_game_or_console(text, game_name, platform):
    """True if a result is about another console or another game of the series."""
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    own = SYSTEM_WORDS.get(platform)
    if re.search(OTHER_SYSTEMS, plain) and not (own and re.search(rf"\b({own})\b", plain)):
        return True
    words = re.findall(r"[a-z0-9]+", _search_phrase(game_name).lower())
    if words and SEQUEL_WORDS:
        game_low = game_name.lower()
        for m in re.finditer(r"\b" + r"\W+".join(words) + r"\W+" + SEQUEL_WORDS + r"\b",
                             plain):
            if m.group(1) not in game_low:
                return True
    return False


# ---------------- GitHub ----------------

def search_github(game_name, platform="snes"):
    extra = {"snes": "msu1", "gba": "gba", "nes": "nes", "gb": "gameboy", "gbc": "gbc",
             "nds": "nds", "sms": "sms", "genesis": "genesis", "n64": "n64",
             "gg": "gamegear"}.get(platform, "rom")
    q = (f'"{_search_phrase(game_name)}" hack OR translation OR patch OR romhack '
         f'OR randomizer OR {extra} in:name,description')
    r = requests.get("https://api.github.com/search/repositories",
                     params={"q": q, "per_page": 30}, headers=_gh_headers(), timeout=20)
    if r.status_code == 403:
        raise RuntimeError("GitHub rate limit hit - add a GitHub token in Settings to raise it")
    r.raise_for_status()
    out = []
    for repo in r.json().get("items", []):
        text = f"{repo['full_name']} {repo.get('description') or ''}".lower()
        if not any(k in text for k in HACK_WORDS):
            continue
        if is_tool_or_port(f"{repo['name']} {repo.get('description') or ''}"):
            continue  # a tool or PC port, not a patch
        if not about_game(f"{repo['name']} {repo.get('description') or ''}", game_name):
            continue  # another game (often another game of the same series)
        if other_game_or_console(f"{repo['name']} {repo.get('description') or ''} "
                                 f"{' '.join(repo.get('topics') or [])}", game_name, platform):
            continue  # e.g. a Smash Ultimate (Switch) mod for Super Smash Bros. (N64)
        out.append(_gh_result(repo, platform))
    return out


def _gh_result(repo, platform):
    return {"source": "GitHub", "title": repo["full_name"],
            "desc": repo.get("description") or "",
            "url": repo["html_url"], "repo": repo["full_name"], "platform": platform,
            "branch": repo.get("default_branch", "main"),
            "updated": repo.get("pushed_at", "")}


def search_github_titles(titles, game_name, platform):
    """Hacks found on other sites, looked up on GitHub by name: many repos never mention
    the base game ('JSsixtyfour/smashremix' for Smash Remix has no description)."""
    out = []
    for title in list(dict.fromkeys(titles))[:3]:
        key = re.sub(r"[^a-z0-9]", "", title.lower())
        if len(key) < 5:
            continue
        found = {}
        # by name as one word too ("smashremix"): GitHub's search splits words differently
        for q in (f"{key} in:name",):  # one search per name: 10 a minute without a token
            r = requests.get("https://api.github.com/search/repositories",
                             params={"q": q, "per_page": 10}, headers=_gh_headers(),
                             timeout=20)
            if not r.ok:
                break  # rate limit: keep what we have
            for repo in r.json().get("items", []):
                name = re.sub(r"[^a-z0-9]", "", repo["name"].lower())
                text = f"{repo['name']} {repo.get('description') or ''}"
                if (name == key or key in name) and not repo.get("fork") and                         not is_tool_or_port(text) and                         not other_game_or_console(text, game_name, platform):
                    found[repo["full_name"]] = repo
        # the official repo is the most starred; copies with almost no stars are noise
        best = sorted(found.values(), key=lambda r: -r.get("stargazers_count", 0))
        if best:
            top = best[0].get("stargazers_count", 0)
            out += [_gh_result(r, platform) for r in best[:3]
                    if r is best[0] or r.get("stargazers_count", 0) >= max(10, top // 10)]
    return out


def verify_github(results):
    """Keep only GitHub results that really hold a patch (not an empty repo, a page with
    just an index file, or a pre-patched ROM). The files found are kept for the list."""
    gh = [r for r in results if r["source"] == "GitHub"]
    if not gh:
        return results
    from concurrent.futures import ThreadPoolExecutor

    def files(r):
        try:
            return _github_patch_files(r)
        except requests.RequestException:
            return None  # couldn't check: keep it
    with ThreadPoolExecutor(6) as pool:
        found = list(pool.map(files, gh))
    drop = set()
    for r, f in zip(gh, found):
        if f is None:
            continue
        if f:
            r["_files"] = f
        else:
            drop.add(id(r))
    return [r for r in results if id(r) not in drop]


def list_github_files(entry):
    """Release / patch files of a repo, always followed by an "Open on GitHub" row so
    the page can be opened even when nothing downloadable is found (or the API is
    rate-limited)."""
    page = {"name": "Open on GitHub", "url": entry["url"], "browser": True}
    try:
        return (entry.get("_files") or _github_patch_files(entry)) + [page]
    except requests.RequestException:
        return [page]


def _github_web_release_files(entry):
    """The latest release's files read from github.com's normal pages - used when the
    API's 60-requests-an-hour limit (without a token) is used up."""
    r = requests.get(f"https://github.com/{entry['repo']}/releases/latest",
                     headers=BROWSER_HEADERS, timeout=20)
    m = re.search(r"/releases/tag/([^/?#]+)$", r.url)
    if not r.ok or not m:
        return []
    page = requests.get(f"https://github.com/{entry['repo']}/releases/expanded_assets/"
                        f"{m.group(1)}", headers=BROWSER_HEADERS, timeout=20)
    files = []
    for href in re.findall(r'href="(/[^"]+/releases/download/[^"]+)"', page.text):
        name = requests.utils.unquote(href.rsplit("/", 1)[-1])
        if _is_offerable(name, 0, "github") and is_patch_file(name):
            files.append({"name": f"{m.group(1)}/{name}", "url": "https://github.com" + href,
                          "size": 0})
    return files


def _github_patch_files(entry):
    files = []
    r = requests.get(f"https://api.github.com/repos/{entry['repo']}/releases",
                     headers=_gh_headers(), timeout=20)
    if r.ok:
        for rel in r.json()[:5]:
            for a in rel.get("assets", []):
                if _is_offerable(a["name"], a["size"], "github") and \
                        is_patch_file(a["name"]):
                    files.append({"name": f"{rel.get('tag_name', '')}/{a['name']}",
                                  "url": a["browser_download_url"], "size": a["size"]})
    elif r.status_code in (403, 429):  # API limit used up: read the web pages instead
        files = _github_web_release_files(entry)
        if not files:  # no release - the source tree can't be checked without the API
            raise requests.RequestException("GitHub API limit reached")
        return files
    if files:  # proper releases: skip the work-in-progress patches in the source tree
        return files
    r = requests.get(f"https://api.github.com/repos/{entry['repo']}/git/trees/"
                     f"{entry['branch']}?recursive=1", headers=_gh_headers(), timeout=20)
    if r.status_code in (403, 429):
        raise requests.RequestException("GitHub API limit reached")
    if r.ok:
        for t in r.json().get("tree", []):
            if t["type"] == "blob" and t["path"].lower().endswith(PATCH_EXTS) \
                    and is_patch_file(t["path"]):
                files.append({"name": t["path"], "size": t.get("size", 0),
                              "url": f"https://github.com/{entry['repo']}/raw/"
                                     f"{entry['branch']}/{requests.utils.quote(t['path'])}"})
    return files


# ---------------- archive.org ----------------

ARCHIVE_WORDS = {
    "snes": '(snes OR "super nintendo" OR sfc OR "super famicom")',
    "gba": '(gba OR "game boy advance" OR gameboy)',
    "nes": '(nes OR "nintendo entertainment system" OR famicom)',
    "gb": '(gb OR "game boy" OR gameboy)',
    "gbc": '(gbc OR "game boy color" OR gameboy)',
    "nds": '(nds OR "nintendo ds")',
    "sms": '(sms OR "master system")',
    "genesis": '(genesis OR "mega drive" OR megadrive OR 32x)',
    "n64": '(n64 OR "nintendo 64")',
    "gg": '(gg OR "game gear")',
}


def search_archive(game_name, platform="snes"):
    title = _search_phrase(game_name)
    q = (f'title:("{title}") AND {ARCHIVE_WORDS[platform]} '
         f'AND (patch OR translation OR hack OR ips OR bps OR ups OR msu1 OR msu)')
    r = requests.get("https://archive.org/advancedsearch.php",
                     params={"q": q, "fl[]": ["identifier", "title", "description"],
                             "rows": 30, "output": "json"}, headers=HEADERS, timeout=20)
    r.raise_for_status()
    out = []
    for d in r.json()["response"]["docs"]:
        desc = d.get("description") or ""
        if isinstance(desc, list):
            desc = " ".join(desc)
        if not about_game(d.get("title", d["identifier"]), game_name) or \
                NOT_A_PATCH.search(d.get("title", "")) or \
                re.search(r"pre-?patched|patched rom", d.get("title", ""), re.I):
            continue
        out.append({"source": "archive.org", "title": d.get("title", d["identifier"]),
                    "desc": re.sub("<[^>]+>", "", desc)[:300],
                    "url": f"https://archive.org/details/{d['identifier']}",
                    "ident": d["identifier"]})
    return out


# a pre-patched ROM named like a ROM set entry: "Super Metroid (Japan, USA) (En,Ja) [Hack]"
ROM_NAME = re.compile(r"\((usa|europe|japan|world|en|ja|u|e|j)\b[^)]*\)|\[!\]|\[h\d*|"
                      r"pre-?patched|patched[ _-]?rom", re.I)


def _archive_patch_files(entry):
    r = requests.get(f"https://archive.org/metadata/{entry['ident']}/files",
                     headers=HEADERS, timeout=20)
    r.raise_for_status()
    files = []
    for f in r.json().get("result", []):
        size = int(f.get("size", 0) or 0)
        name = f["name"].lower()
        if f.get("source") != "original" or not _is_offerable(f["name"], size):
            continue
        if name.endswith(PATCH_EXTS):
            pass  # an actual patch
        elif ROM_NAME.search(f["name"]) or not any(k in name for k in HACK_WORDS +
                                                   ("ips", "bps", "ups", "xdelta")):
            continue  # an archive holding a (pre-patched) ROM, or nothing patch-like
        files.append({"name": f["name"], "size": size,
                      "url": f"https://archive.org/download/{entry['ident']}/{f['name']}"})
    return files


def list_archive_files(entry):
    files = entry.get("_files")
    if files is None:
        files = _archive_patch_files(entry)
    # always offer the item's page too
    return files + [{"name": "Open on archive.org", "url": entry["url"], "browser": True}]


def verify_archive(results):
    """archive.org items are often pre-patched ROMs (or unrelated uploads): keep only the
    ones that hold a patch file."""
    items = [r for r in results if r["source"] == "archive.org"]
    if not items:
        return results
    from concurrent.futures import ThreadPoolExecutor

    def files(r):
        try:
            return _archive_patch_files(r)
        except requests.RequestException:
            return None  # couldn't check: keep it
    with ThreadPoolExecutor(6) as pool:
        found = list(pool.map(files, items))
    drop = set()
    for r, f in zip(items, found):
        if f is None:
            continue
        if f:
            r["_files"] = f
        else:
            drop.add(id(r))
    return [r for r in results if id(r) not in drop]


# ---------------- Zeldix (MSU-1 packs) ----------------

ZELDIX = "https://www.zeldix.net"
ZELDIX_INDEX = ZELDIX + "/t2684-alphabetical-list-every-snes-msu1-hack"
ZELDIX_CACHE = os.path.join(CACHE_DIR, "zeldix_index.json")


def _zeldix_index(force=False):
    """{title: topic_url} for every SNES MSU-1 hack, cached for a week."""
    if not force and os.path.exists(ZELDIX_CACHE) and             time.time() - os.path.getmtime(ZELDIX_CACHE) < 7 * 86400:
        with open(ZELDIX_CACHE, encoding="utf-8") as f:
            return json.load(f)
    r = requests.get(ZELDIX_INDEX, headers=BROWSER_HEADERS, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    post = soup.select_one(".postbody .content") or soup
    index = {}
    for a in post.find_all("a", href=True):
        m = re.search(r"/t(\d+)-([a-z0-9-]+)", a["href"])
        if m and m.group(1) != "2684":
            title = m.group(2).replace("-", " ").title()
            index[title] = f"{ZELDIX}/t{m.group(1)}-{m.group(2)}"
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(ZELDIX_CACHE, "w", encoding="utf-8") as f:
        json.dump(index, f)
    return index


def _words(text):
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return set(re.findall(r"[a-z0-9]+", text.lower())) - {"the", "of", "a", "and", "msu", "1"}


def search_zeldix(game_name, platform="snes"):
    if platform != "snes":
        return []
    want = _words(_clean_title(game_name))
    if not want:
        return []
    out = []
    phrase = re.findall(r"[a-z0-9]+", _clean_title(game_name).lower())
    sequel = re.compile(r"\b" + r"\W+".join(phrase) + r"\W+(\d+|ii|iii|iv|v|vi|vii|viii|ix|x)\b")
    for title, url in _zeldix_index().items():
        words = _words(title)
        # Zeldix names a game by its main title ("Donkey Kong Country 2"), without the
        # subtitle ("Diddy's Kong Quest") about_game would look for. A number right after
        # the title means a sequel ("Donkey Kong Country 2" isn't Donkey Kong Country);
        # combined names ("Final Fantasy VI J Final Fantasy III US") are fine
        other_number = bool(phrase) and bool(sequel.search(title.lower()))
        if want <= words and not other_number:
            out.append(dict(extra=len(_words(title) - want), **{"source": "Zeldix", "title": title + " (MSU-1)",
                        "desc": "MSU-1 audio pack thread on zeldix.net", "url": url,
                        "category": "msu1"}))
    out.sort(key=lambda e: e.pop("extra"))  # closest title first
    return out


def _drive_id(url):
    m = re.search(r"drive\.google\.com/(?:open\?id=|file/d/|uc\?(?:export=download&)?id=)"
                  r"([\w-]{20,})", url)
    return m.group(1) if m else None


def _short(text, limit=34):
    """Keep file-list labels short enough to fit the Patch files box."""
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def list_zeldix_files(entry):
    r = requests.get(entry["url"], headers=BROWSER_HEADERS, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    files = thread_files(soup.select(".postbody .content") or [soup], msu=True)
    return files or [{"name": "Open on Zeldix", "url": entry["url"], "browser": True}]


def thread_files(posts, msu=False):
    """Downloadable files linked in forum posts: Mega / Google Drive / MediaFire files
    (also when pasted as plain text) and direct patch / archive links."""
    links = []  # (url, link text): clickable links, then URLs pasted as plain text
    for post in posts:  # every post - updated packs are often posted further down
        links += [(a["href"], a.get_text(strip=True)) for a in post.find_all("a", href=True)]
        for u in re.findall(r"(?:https?://)?(?:www\.)?(?:mega\.nz|mediafire\.com|"
                            r"drive\.google\.com|dropbox\.com)/[^\s\"<>]+", post.get_text(" ")):
            u = u if u.startswith("http") else "https://" + u
            links.append((u.rstrip(".,)"), u))
    files, seen = [], set()
    for href, label in links:
        if re.search(r"mediafire\.com/\?\w+$", href):  # old-style MediaFire link
            href = href.replace("mediafire.com/?", "mediafire.com/file/")
        if href in seen:
            continue
        seen.add(href)
        did = _drive_id(href)
        if did:
            files.append({"name": _short(f"Google Drive: {label if label != href else 'file'}"),
                          "url": "https://drive.usercontent.google.com/download?id="
                                 f"{did}&export=download&confirm=t", "msu": msu})
        elif mega_file_link(href):
            files.append({"name": _short(f"Mega: {label if label != href else 'file'}"),
                          "url": href, "mega": True, "msu": msu})
        elif re.search(r"mediafire\.com/(file|download)/", href):
            files.append({"name": _short(f"MediaFire: {label if label != href else 'file'}"),
                          "url": href, "mediafire": True, "msu": msu})
        elif re.search(r"dropbox\.com/(s|scl/fi)/[^?]+\.\w{2,5}(\?|$)", href):
            # a single Dropbox file: dl=1 makes it a direct download
            direct = re.sub(r"([?&])dl=0", r"\1dl=1", href)
            if "dl=1" not in direct:
                direct += ("&" if "?" in direct else "?") + "dl=1"
            name = requests.utils.unquote(href.split("?")[0].rsplit("/", 1)[-1])
            if direct in seen:
                continue
            seen.add(direct)
            files.append({"name": _short(name, 40), "full_name": name, "url": direct,
                          "msu": msu})
        elif any(h in href for h in ("mega.nz", "drive.google.com/drive/folders",
                                     "mediafire.com", "dropbox.com")):
            host = re.sub(r"^www\.", "", href.split("/")[2]) if "//" in href else href
            files.append({"name": f"Open on {host}", "url": href,
                          "browser": True})
        elif href.lower().split("?")[0].endswith(ARCHIVE_EXTS + PATCH_EXTS):
            files.append({"name": os.path.basename(href.split("?")[0]), "url": href,
                          "msu": msu})
    _describe_hosted(files, msu)
    unique, keys = [], set()
    for f in files:  # the same file linked twice (clickable + pasted) shows once
        key = (f.get("full_name", f["url"]).replace("+", " ").lower(), f.get("size"))
        if key in keys or f.get("full_name", "").lower().endswith((".txt", ".nfo")):
            continue
        keys.add(key)
        unique.append(f)
    return unique


def link_kind(name):
    """'music' for PCM audio packs, 'patch' for MSU-1 patches, 'pack' if unsure."""
    n = name.lower()
    if n.endswith(".pcm") or re.search(r"pcm|audio|music|soundtrack|\bost\b|orchestra|"
                                       r"symphon|remaster(ed)? (music|audio)", n):
        return "music"
    if n.endswith(PATCH_EXTS) or re.search(r"patch|\bips\b|\bbps\b|hack", n):
        return "patch"
    return "pack"


KIND_LABEL = {"music": "Music", "patch": "MSU-1 patch", "pack": "MSU-1 pack"}


def _hosted_info(f):
    """(file name, size) of a Mega / Google Drive / MediaFire file, without downloading
    it - Mega's API, a 1-byte Drive request, or MediaFire's page."""
    try:
        if f.get("mega"):
            import struct
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
            fid, key_b64 = mega_file_link(f["url"])
            a = struct.unpack(">8I", _b64url(key_b64))
            key = struct.pack(">4I", a[0] ^ a[4], a[1] ^ a[5], a[2] ^ a[6], a[3] ^ a[7])
            info = requests.post("https://g.api.mega.co.nz/cs", params={"id": 0},
                                 json=[{"a": "g", "p": fid}], timeout=15).json()[0]
            if isinstance(info, int):
                return "(removed from Mega)", 0
            dec = Cipher(algorithms.AES(key), modes.CBC(b"\0" * 16)).decryptor()
            attr = dec.update(_b64url(info["at"])) + dec.finalize()
            return json.loads(attr[4:].rstrip(b"\0").decode())["n"], int(info.get("s", 0))
        if "drive.usercontent.google.com" in f["url"]:
            r = requests.get(f["url"], headers={**HEADERS, "Range": "bytes=0-0"},
                             stream=True, timeout=15)
            if r.status_code in (403, 404):
                return "(removed from Google Drive)", 0
            m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)',
                          r.headers.get("Content-Disposition", ""))
            total = re.search(r"/(\d+)$", r.headers.get("Content-Range", ""))
            r.close()
            if m:
                return requests.utils.unquote(m.group(1)), int(total.group(1)) if total else 0
        if f.get("mediafire"):
            direct = mediafire_direct(f["url"])
            h = requests.head(direct, headers=HEADERS, timeout=15, allow_redirects=True)
            from urllib.parse import unquote_plus
            name = unquote_plus(direct.rsplit("/", 1)[-1])
            return name, int(h.headers.get("Content-Length", 0))
    except Exception:
        pass
    return None, 0


def _describe_hosted(files, msu=True):
    """Replace "Mega: file" / "Google Drive: DOWNLOAD" labels with what the file really
    is: 'PCM music: SM_Orchestral.zip' or 'MSU-1 patch: btbm_msu1.zip', plus its size."""
    hosted = [f for f in files if f.get("mega") or f.get("mediafire")
              or "drive.usercontent.google.com" in f.get("url", "")]
    if not hosted:
        return
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(6) as pool:
        infos = list(pool.map(_hosted_info, hosted))
    for f, (name, size) in zip(hosted, infos):
        if not name:
            if f["name"].rstrip().endswith(":"):  # "Google Drive: " with no link text
                f["name"] = f["name"].rstrip() + " file"
            continue
        kind = link_kind(name) if not name.startswith("(removed") else "pack"
        if kind == "pack" and size:  # patches are tiny, PCM tracks are huge
            kind = "music" if size > 20 * 1048576 else "patch" if size < 4 * 1048576 else kind
        f["kind"], f["full_name"], f["size"] = kind, name, size
        f["name"] = name if name.startswith("(removed") or not msu else \
            _short(f"{KIND_LABEL[kind]}: {name}", 40)
        if not msu:
            f.pop("kind", None)  # only MSU-1 rows say "Music" / "MSU-1 patch"


# ---------------- romhacking.net (bundled index from the archive.org backup) ----------

ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
_rhn = {}


def _norm(name):
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()  # é -> e
    name = re.sub(r"[\(\[][^\)\]]*[\)\]]", "", name)
    name = re.sub(r",\s*the\b", "", name, flags=re.I)
    name = re.sub(r"^the\s+", "", name.strip(), flags=re.I)
    return re.sub(r"[^a-z0-9]", "", name.lower().replace("&", "and"))


def rhn_index(platform="snes"):
    """romhacking.net's hacks and translations for a console: the full August 2024
    database once it's been read (needs an archive.org sign-in), else the smaller list
    from the public backup's pages that ships with the app."""
    import rhn_db
    db = rhn_db.load()
    if db and db.get(platform):
        return db[platform]
    if platform not in _rhn:
        try:
            with open(os.path.join(ASSETS, f"rhn_{platform}.json"), encoding="utf-8") as f:
                _rhn[platform] = json.load(f)
        except OSError:
            _rhn[platform] = []
    return _rhn[platform]


def _rhn_result(e):
    by = f" by {e['author']}" if e.get("author") else ""
    desc = f"{e.get('category') or 'Hack'} for {e['game']}{by} - v{e['version']}, {e['date']}"
    r = {"source": "Romhacking.net", "title": e["title"], "desc": desc, "url": e["url"],
         "rhn_id": e.get("id"), "rhn_kind": e.get("kind"),
         "rhn_version": e.get("version", ""), "rhn_date": e.get("date", ""),
         "downloads": e.get("downloads", 0), "rominfo": e.get("rominfo", ""),
         "full_desc": e.get("desc", "")}
    if e["kind"] == "translation":
        r["category"] = "translation"
    return r


GAME_ALIASES = {}  # {platform: {norm(name): {norm(other name), ...}}} - set by the app


def search_rhn(game_name, platform="snes"):
    want = {_norm(game_name)} | GAME_ALIASES.get(platform, {}).get(_norm(game_name), set())
    return [_rhn_result(e) for e in rhn_index(platform) if _norm(e["game"]) in want]


def list_rhn_files(entry):
    """With an archive.org sign-in, the hack's files come straight from romhacking.net's
    own file dump; otherwise (or if it isn't in there) the page opens in the browser."""
    import archive_org
    files = []
    if archive_org.CREDS.get("access") and entry.get("rhn_id"):
        try:
            for info in archive_org.rhn_members(entry.get("rhn_kind", "hack"), entry["rhn_id"]):
                name = archive_org.display_name(info.filename)
                files.append({"name": _short(name, 40), "full_name": name,
                              "size": info.file_size, "rhn_member": info.filename,
                              "url": archive_org.RHN_DUMP})
        except Exception as e:
            log.warning("romhacking.net dump lookup failed: %s", e)
    files.append({"name": "Open on romhacking.net", "url": entry["url"], "browser": True})
    return files


# ---------------- RAPatches (RetroAchievements' patch collection on GitHub) ----------------
# github.com/RetroAchievements/RAPatches keeps the patch versions RetroAchievements
# supports, usually the newest: <Console>/<Category>/[<Game>/]<RA game id>-<Name>.zip.
# The whole file list is one GitHub request, cached for a week.

RAP_REPO = "RetroAchievements/RAPatches"
RAP_CACHE = os.path.join(CACHE_DIR, "rapatches2.json")
RAP_CONSOLE = {"snes": "SNES", "nes": "NES", "gba": "GBA", "n64": "N64", "genesis": "MD",
               "nds": "NDS", "gb": "Game Boy", "gbc": "GBC", "sms": "Master System",
               "gg": "Game Gear"}
RAP_CATEGORY = {"hacks": "hack", "hack": "hack", "translation": "translation",
                "msu-1": "msu1", "improvement": "hack", "fix": "hack", "cosmetic": "hack",
                "undub": "translation"}  # Subset / Removed / conversions are left out
_rap = None


def rapatches_index(force=False):
    """[{path, size, sha, category, game, id, name}] for every patch in RAPatches."""
    global _rap
    if _rap is not None and not force:
        return _rap
    data = None
    try:
        if not force and time.time() - os.path.getmtime(RAP_CACHE) < 7 * 86400:
            with open(RAP_CACHE, encoding="utf-8") as f:
                data = json.load(f)
            if data and "sha" not in data[0]:
                data = None  # made before fingerprints were kept
    except (OSError, ValueError):
        pass
    if data is None:
        r = requests.get(f"https://api.github.com/repos/{RAP_REPO}/git/trees/main?recursive=1",
                         headers=_gh_headers(), timeout=60)
        r.raise_for_status()
        data = []
        for t in r.json().get("tree", []):
            parts = t["path"].split("/")
            if t["type"] != "blob" or len(parts) < 3:
                continue
            cat = RAP_CATEGORY.get(parts[1].lower())
            m = re.match(r"(\d+)-(.+)\.(zip|7z|rar|ips|bps|ups|xdelta)$", parts[-1], re.I)
            if not cat or not m:
                continue  # subsets, split archives (.001), readmes, images
            data.append({"path": t["path"], "size": t.get("size", 0), "sha": t.get("sha"),
                         "console": parts[0],
                         "category": cat,
                         "folder": parts[2] if len(parts) == 4 and cat != "translation" else "",
                         "lang": parts[2] if len(parts) == 4 and cat == "translation" else "",
                         "kind": parts[1].lower(),
                         "id": int(m.group(1)), "name": m.group(2)})
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(RAP_CACHE, "w", encoding="utf-8") as f:
            json.dump(data, f)
    _rap = data
    return data


def _uncamel(text):
    """'SmashRemix' -> 'Smash Remix', 'SM64-GlassSmash64' -> 'SM64 Glass Smash 64'"""
    text = re.sub(r"(?<=[a-z])(?=[A-Z0-9])|(?<=[0-9])(?=[A-Z][a-z])", " ", text)
    return re.sub(r"[-_]+", " ", text).strip()


def _rap_game_folder(folder):
    """'7th Saga, The' -> 'The 7th Saga'"""
    m = re.match(r"(.+), (The|A|An)$", folder)
    return f"{m.group(2)} {m.group(1)}" if m else folder


def search_rapatches(game_name, platform="snes"):
    console = RAP_CONSOLE.get(platform)
    if not console:
        return []
    ra_titles = {}
    if RA_KEY:  # the RA game id in each file name -> its RetroAchievements title
        try:
            ra_titles = {g["id"]: g["title"] for g in ra_games(platform)}
        except Exception:
            pass
    want = _norm(game_name)
    aliases = GAME_ALIASES.get(platform, {}).get(want, set())
    out = []
    for e in rapatches_index():
        if e["console"] != console:
            continue
        ra_title = ra_titles.get(e["id"], "")
        if e["folder"]:  # Hacks/<Game>/...: the folder names the base game
            if _norm(_rap_game_folder(e["folder"])) != want and \
                    not about_game(_rap_game_folder(e["folder"]), game_name):
                continue
        else:  # translations / fixes: the id is the base game's RetroAchievements id
            # RetroAchievements' title ("Trials of Mana") or the file's ("SeikenDensetsu3");
            # either may be the name in the game list, or one of its other names
            names = [_uncamel(e["name"].split("-")[0])]
            if ra_title:
                names.append(ra_clean(ra_title)[0])
            if not any(about_game(n, game_name) or _norm(n) in aliases for n in names):
                continue
        words = e["name"].split("-")
        if e.get("kind") in ("improvement", "fix", "cosmetic"):  # id = the base game's
            what = _uncamel("-".join(words[1:])) if len(words) > 1 else _uncamel(e["name"])
            title = f"{e.get('kind', 'fix').title()}: {what}"
        elif e["category"] == "hack":
            if ra_title and e["folder"]:
                title = ra_clean(ra_title)[0]
            else:
                title = _uncamel("-".join(words[1:]) if len(words) > 1 and e["folder"]
                                 else e["name"])
        elif e["category"] == "translation":
            lang = e.get("lang") or words[-1]
            game = ra_clean(ra_title)[0] if ra_title else _uncamel(words[0])
            title = f"{game} - {lang} translation"
        else:
            title = _uncamel(e["name"])
        out.append({
            "source": "RAPatches", "title": title, "category": e["category"],
            "desc": "From RetroAchievements' RAPatches collection on GitHub: the version of "
                    "this patch that RetroAchievements supports (it earns achievements), "
                    "usually the newest." + (f" RetroAchievements title: {ra_title}."
                                             if ra_title else ""),
            "url": f"https://github.com/{RAP_REPO}/blob/main/"
                   + requests.utils.quote(e["path"]),
            "rap_path": e["path"], "size": e["size"]})
    return out


def link_rapatches_to_rhn(results, game_name):
    """Give each RAPatches hack the romhacking.net page of the same hack (matched by
    name), so it can show romhacking.net's description."""
    key = lambda t: re.sub(r"[^a-z0-9]", "", t.lower())
    game = key(_search_phrase(game_name))
    rhn = [(key(r["title"]).replace(game, "") or key(r["title"]), r)
           for r in results if r["source"] == "Romhacking.net" and r.get("rhn_id")]
    for r in results:
        if r["source"] != "RAPatches":
            continue
        name = key(r["title"]).replace(game, "") or key(r["title"])
        for other, hit in rhn:
            if len(other) >= 4 and len(name) >= 4 and (other in name or name in other) \
                    and hit["category"] == r["category"]:
                r["rhn_url"], r["rhn_desc"] = hit["url"], hit.get("desc", "")
                r["rhn_full"], r["rominfo"] = hit.get("full_desc", ""), hit.get("rominfo", "")
                break


def list_rapatches_files(entry):
    name = entry["rap_path"].rsplit("/", 1)[-1]
    return [{"name": _short(name, 40), "full_name": name, "size": entry.get("size", 0),
             "url": f"https://github.com/{RAP_REPO}/raw/main/"
                    + requests.utils.quote(entry["rap_path"])},
            {"name": "Open on GitHub", "url": entry["url"], "browser": True}]


# ---------------- Hackdex (Pokémon ROM hack patches) ----------------
# hackdex.app hosts patches (BPS / xdelta) and says exactly which ROM each needs. Its
# robots.txt allows the /hack/ pages but not /api/ (where downloads live), so patches
# aren't downloaded from it here: Vibe Patcher lists the hack, uses its base ROM for the
# compatibility check, links its GitHub / PokéCommunity / Discord, and "Open on Hackdex"
# lets the Downloads watcher apply what's downloaded there. Hack pages are read from the
# sitemap; only new / changed pages are re-read (weekly).

HD = "https://www.hackdex.app"
HD_CACHE = os.path.join(CACHE_DIR, "hackdex.json")
HD_PLATFORM = {"game boy advance": "gba", "nintendo ds": "nds", "game boy color": "gbc",
               "game boy": "gb"}
_hd = None


def _hackdex_page(url):
    """{title, url, base, platform, version, patch, desc, downloads, updated, links} or None."""
    r = requests.get(url, headers=BROWSER_HEADERS, timeout=30)
    if not r.ok:
        return None
    soup = BeautifulSoup(r.text, "html.parser")
    text = soup.get_text(" ", strip=True)
    og = soup.find("meta", property="og:title")
    title = re.sub(r"\s*\|\s*ROM hack download\s*$", "", og["content"]) if og else ""
    m = re.search(r"Base ROM\s+(.+?)\s+Platform\s+(.+?)\s+(?:AI use|Updated|Language|$)", text)
    if not title or not m:
        return None
    desc = soup.find("meta", attrs={"name": "description"})
    img = soup.find("meta", property="og:image")
    alt = soup.find("meta", property="og:image:alt")
    boxart = img["content"] if img and alt and "box art" in alt.get("content", "").lower() \
        else ""
    ver = re.search(r"Version\s+(\S+)\s+Patch\s+(\w+)\s+file", text)
    dls = re.search(r"Downloads\s+([\d.,]+K?M?)", text)
    upd = re.search(r"Updated\s+([A-Z][a-z]{2,8}\s+\d{1,4})", text)
    n = (dls.group(1) if dls else "0").replace(",", "")
    mult = 1000 if n.endswith("K") else 1000000 if n.endswith("M") else 1
    try:
        downloads = int(float(n.rstrip("KM")) * mult)
    except ValueError:
        downloads = 0
    links = []
    for a in soup.find_all("a", href=True):
        href, label = a["href"], a.get_text(" ", strip=True)
        if re.search(r"pokecommunity\.com/threads/|github\.com/(?!orgs/|Hackdex-App/)[^/]+/[^/#?]+|"
                     r"discord\.(gg|com)/|reddit\.com/r/|whackahack\.com", href, re.I) and \
                href not in [l["url"] for l in links]:
            if not label or label.startswith("http"):
                label = re.sub(r"^www\.", "", href.split("/")[2]).split(".")[0].title()
            links.append({"label": label, "url": href})
    return {"title": title, "url": url, "base": m.group(1).strip(),
            "platform": HD_PLATFORM.get(m.group(2).strip().lower(), ""),
            "version": ver.group(1) if ver else "", "patch": ver.group(2) if ver else "",
            "desc": desc["content"] if desc else "", "downloads": downloads,
            "updated": upd.group(1) if upd else "", "links": links, "boxart": boxart}


def hackdex_index(force=False, progress=lambda *a: None):
    """[entry] for every hack on Hackdex (see _hackdex_page), cached; refreshed weekly by
    re-reading only the pages the sitemap says changed."""
    global _hd
    if _hd is not None and not force:
        return _hd
    cache = {}
    try:
        with open(HD_CACHE, encoding="utf-8") as f:
            cache = json.load(f)
    except (OSError, ValueError):
        try:  # the list that ships with the app
            with open(os.path.join(ASSETS, "hackdex.json"), encoding="utf-8") as f:
                cache = json.load(f)
        except (OSError, ValueError):
            pass
    fresh = cache.get("_checked", 0) > time.time() - 7 * 86400
    if force or not fresh:
        try:
            sm = requests.get(HD + "/sitemap.xml", headers=BROWSER_HEADERS, timeout=30).text
            pages = re.findall(r"<loc>(https://www\.hackdex\.app/hack/[^<]+)</loc>\s*"
                               r"(?:<lastmod>([^<]+)</lastmod>)?", sm)
            todo = [(u, lm) for u, lm in pages  # new, changed, or read before box art
                    if u not in cache or cache[u].get("_lastmod") != lm or
                    cache[u].get("boxart") is None]
            from concurrent.futures import ThreadPoolExecutor
            progress(f"Reading Hackdex ({len(todo)} new or updated hacks)…")
            with ThreadPoolExecutor(2) as pool:  # gently - pages are read in full
                for (u, lm), e in zip(todo, pool.map(lambda x: _safe_hd(x[0]), todo)):
                    if e:
                        e["_lastmod"] = lm
                        cache[u] = e
            for u, lm in todo:  # a second, one-at-a-time try for pages that failed
                if cache.get(u, {}).get("_lastmod") != lm or cache[u].get("boxart") is None:
                    e = _safe_hd(u)
                    if e:
                        e["_lastmod"] = lm
                        cache[u] = e
            for u, e in cache.items():  # does the hack's own GitHub repo have releases?
                if not u.startswith("_") and "gh_direct" not in e:
                    e["gh_direct"] = _hd_github_direct(e)
            live = {u for u, _ in pages}
            cache = {u: e for u, e in cache.items() if u.startswith("_") or u in live}
            cache["_checked"] = time.time()
            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(HD_CACHE, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False)
        except requests.RequestException:
            pass  # offline: use what we have
    _hd = [e for u, e in cache.items() if not u.startswith("_")]
    return _hd


def _safe_hd(url):
    try:
        return _hackdex_page(url)
    except Exception:
        return None


def hd_own_repo(e):
    """'owner/repo' of the hack's own GitHub repo (from its Links), or None."""
    words = {w for w in _words(e["title"]) if w not in ("pokemon", "pokmon")}
    for link in e.get("links", []):
        m = re.search(r"github\.com/([^/]+/[^/#?]+)", link["url"])
        if m and link["label"].strip().lower() == "github" and \
                words & _words(_uncamel(m.group(1).split("/")[1])):
            return m.group(1).removesuffix(".git")
    return None


def _hd_github_direct(e):
    """True if the hack's own repo has patch files in its releases (in-app download)."""
    repo = hd_own_repo(e)
    if not repo:
        return False
    try:
        return bool([f for f in list_github_files({"repo": repo, "branch": "main",
                                                   "url": f"https://github.com/{repo}",
                                                   "platform": "github"})
                     if not f.get("browser")])
    except Exception:
        return False


def hd_date(e):
    """A Hackdex hack's last update as YYYY-MM-DD ('' if unknown)."""
    import datetime
    text = (e.get("updated") or "").strip()
    today = datetime.date.today()
    for fmt in ("%b %Y", "%B %Y"):
        try:
            return datetime.datetime.strptime(text, fmt).strftime("%Y-%m-01")
        except ValueError:
            pass
    for fmt in ("%b %d", "%B %d"):
        try:
            d = datetime.datetime.strptime(text, fmt).date().replace(year=today.year)
            if d > today:  # "Dec 20" seen in January: last year
                d = d.replace(year=today.year - 1)
            return d.isoformat()
        except ValueError:
            pass
    return ""


def hd_boxart(e):
    """A Hackdex hack's box art, if it's a direct image (some are album pages)."""
    url = e.get("boxart") or ""
    return url if re.search(r"\.(jpe?g|png|webp|gif)(\?|$)|/storage/v1/object/", url, re.I)         else ""


def _hd_result(e):
    bits = [f"Pokémon hack on Hackdex for {e['base']}"]
    if e.get("version"):
        bits.append(f"{e['version']} ({e.get('patch') or 'patch'})")
    if e.get("downloads"):
        bits.append(f"{e['downloads']:,} downloads")
    return {"source": "Hackdex", "title": e["title"], "url": e["url"], "category": "hack",
            "desc": " - ".join(bits), "full_desc": e.get("desc", ""),
            # what Hackdex says the patch needs, for the ROM version check
            "rominfo": f"Requires a clean {e['base']} ROM.",
            "downloads": e.get("downloads", 0), "hd_links": e.get("links", [])}


def search_hackdex(game_name, platform="gba"):
    if "pokemon" not in _norm(game_name) and "pokmon" not in game_name.lower():
        return []
    out = []
    plain = lambda t: _norm(t).replace("pokemon", "").replace("pokmon", "")
    for e in hackdex_index():
        base = re.sub(r"\s*\([^)]*\)", "", e["base"])  # "Pokémon FireRed (Rev 0)"
        same_platform = e.get("platform") in (platform, *({"gb": ["gbc"], "gbc": ["gb"]}
                                                          .get(platform, [])))
        if not same_platform:
            continue
        if plain(e["title"]) and plain(e["title"]) == plain(game_name):
            out.insert(0, _hd_result(e))  # the hack itself
        elif _norm(base) == _norm(game_name) or \
                _pc_matches(base.replace("Pokémon", "").replace("Pokemon", ""), game_name):
            out.append(_hd_result(e))
    own = [r for r in out if plain(r["title"]) == plain(game_name)]
    rest = sorted((r for r in out if r not in own), key=lambda r: -r.get("downloads", 0))
    return own + rest


# the labels of a hack page's own "Links" section (credits links have other labels)
HD_OWN_LINKS = {"github", "pokécommunity", "pokecommunity", "reddit", "website", "itch.io",
                "youtube", "whack a hack", "whackahack", "twitter", "x", "bluesky"}


def list_hackdex_files(entry):
    """The hack's GitHub releases (downloaded in-app), its other official links, and its
    Hackdex page - Hackdex's own download links are off-limits to apps (robots.txt)."""
    files = []
    title_words = {w for w in _words(entry["title"]) if w not in ("pokemon", "pokmon")}
    for link in entry.get("hd_links", []):
        m = re.search(r"github\.com/([^/]+/[^/#?]+)", link["url"])
        # only the hack's own repo: its page may also link the engine it's built with
        # (e.g. Complete-Fire-Red-Upgrade), whose files aren't this hack's patch
        if m and title_words & _words(_uncamel(m.group(1).split("/")[1])):
            try:
                files += [f for f in list_github_files({"repo": m.group(1).removesuffix(".git"),
                                                        "url": link["url"], "branch": "main",
                                                        "platform": "github"})
                          if not f.get("browser")]
            except Exception:
                pass
    for link in entry.get("hd_links", []):
        if re.search(r"discord\.(gg|com)", link["url"], re.I):
            continue  # chat invites aren't a place to get the patch
        if link["label"].strip().lower() not in HD_OWN_LINKS:
            continue  # credits (resources, engines) - not this hack's own pages
        m = re.search(r"github\.com/([^/]+/[^/#?]+)", link["url"])
        label = f"GitHub ({m.group(1)})" if m else link["label"]
        files.append({"name": f"Open its {label}", "url": link["url"], "browser": True})
    files.append({"name": "Open on Hackdex (download there)", "url": entry["url"],
                  "browser": True})
    return files


# ---------------- PokéHarbor (Pokémon ROM hack blog, WordPress) ----------------
# robots.txt allows everything but the admin pages. Posts are tagged by console (gba /
# nds-roms / gbc) and usually by base game (pokemon-fire-red, emerald-hacks...); the list
# comes from WordPress' public post API, refreshed weekly. PokéHarbor's downloads are
# pre-patched ROMs, which Vibe Patcher never downloads: it's used as a catalogue (names,
# descriptions, base games), with links to a hack's official page when the post has one.

PH_API = "https://www.pokeharbor.com/wp-json/wp/v2/"
PH_CACHE = os.path.join(CACHE_DIR, "pokeharbor2.json")
PH_PLATFORM = {"gba": "gba", "nds-roms": "nds", "gbc": "gbc"}
PH_BASE = {"pokemon-fire-red": "FireRed", "emerald-hacks": "Emerald", "crystal-hacks": "Crystal",
           "ruby-hacks": "Ruby", "hgss-nds-roms": "HeartGold", "b-w2": "Black 2",
           "b-w": "Black", "dpp": "Platinum", "red": "Red", "yellow": "Yellow"}
PH_SKIP = {"rpgxp", "cheats", "guides", "news", "top5s", "randomizers", "documentation",
           "emulators", "pokemon-go", "unity", "unreal-engine", "anime", "pokemon-locations"}
_ph = None


def pokeharbor_index(force=False):
    """[{title, url, platform, base, modified}] for PokéHarbor's ROM hack posts."""
    global _ph
    if _ph is not None and not force:
        return _ph
    data = None
    try:
        if not force and time.time() - os.path.getmtime(PH_CACHE) < 7 * 86400:
            with open(PH_CACHE, encoding="utf-8") as f:
                data = json.load(f)
    except (OSError, ValueError):
        pass
    if data is None:
        import html as htmllib
        cats = {}
        for page in range(1, 5):
            r = requests.get(PH_API + "categories", headers=BROWSER_HEADERS, timeout=60,
                             params={"per_page": 100, "page": page, "_fields": "id,slug"})
            if not r.ok or not r.json():
                break
            cats.update({c["id"]: c["slug"] for c in r.json()})
        data = []
        for page in range(1, 40):
            r = requests.get(PH_API + "posts", headers=BROWSER_HEADERS, timeout=60,
                             params={"per_page": 100, "page": page,
                                     "_fields": "title,link,categories,modified,date"})
            if not r.ok:
                break
            posts = r.json()
            for p in posts:
                slugs = {cats.get(c, "") for c in p.get("categories", [])}
                plat = next((PH_PLATFORM[x] for x in slugs if x in PH_PLATFORM), None)
                if not plat or slugs & PH_SKIP:
                    continue
                title = htmllib.unescape(p["title"]["rendered"]).strip()
                base = next((PH_BASE[x] for x in slugs if x in PH_BASE), None) or \
                    _pc_guess_base(title)
                data.append({"title": title, "url": p["link"], "platform": plat,
                             "base": base or "", "modified": p.get("modified", "")[:10],
                             "date": p.get("date", "")[:10]})
            if len(posts) < 100:
                break
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(PH_CACHE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    _ph = data
    return data


def search_pokeharbor(game_name, platform="gba"):
    if "pokemon" not in _norm(game_name) and "pokmon" not in game_name.lower():
        return []
    out = []
    for e in pokeharbor_index():
        if e["base"] and _pc_matches(e["base"], game_name):
            out.append({"source": "PokéHarbor", "title": e["title"], "url": e["url"],
                        "category": "hack",
                        "desc": f"{e['base']} hack listed on PokéHarbor (updated "
                                f"{e['modified']}). PokéHarbor only offers pre-patched ROMs, so "
                                "Vibe Patcher links the hack's own page or searches for its "
                                "patch."})
    return out


_ph_pages = {}


def _ph_content(url):
    if url not in _ph_pages:
        r = requests.get(url, headers=BROWSER_HEADERS, timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        _ph_pages[url] = soup.select_one(".entry-content") or soup
    return _ph_pages[url]


OFFICIAL = re.compile(r"pokecommunity\.com/threads/|github\.com/[^/]+/[^/#?]+|"
                      r"whackahack\.com|reddit\.com/r/[^/]+/comments/|romhackplaza\.org/|"
                      r"romhacking\.net/hacks/", re.I)


def list_pokeharbor_files(entry):
    """The hack's official pages linked in the post (its author's PokéCommunity thread,
    GitHub, Reddit post...), and a web search for its patch - never PokéHarbor's own
    downloads, which are pre-patched ROMs."""
    files, seen = [], set()
    for a in _ph_content(entry["url"]).find_all("a", href=True):
        href = a["href"].split("#")[0]
        if OFFICIAL.search(href) and href not in seen:
            seen.add(href)
            host = re.sub(r"^www\.", "", href.split("/")[2])
            files.append({"name": f"Open its page on {host}", "url": href, "browser": True})
    q = requests.utils.quote(f'"{entry["title"]}" patch bps OR ips OR ups')
    files.append({"name": "Search the web for its patch", "browser": True,
                  "url": f"https://www.google.com/search?q={q}"})
    return files


# ---------------- PokéCommunity (Completed ROM Hacks Showcase) ----------------
# Thread titles carry the base game ("FireRed hack: Pokémon Gaia"). The full list ships
# in assets/pokecommunity.json (tools/build_pokecommunity_index.py); the newest pages
# are re-read weekly. Its robots.txt allows forum and thread pages but not attachments,
# so attachments open in the browser and only externally hosted files download here.

PC_FORUM = "https://www.pokecommunity.com/forums/completed-rom-hacks-showcase.184/"
PC_CACHE = os.path.join(CACHE_DIR, "pokecommunity2.json")
_pc = None
_pc_pages = {}


def pc_parse_listing(html):
    """[{base, title, url, views}] from one page of the showcase forum."""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for item in soup.select(".structItem--thread"):
        a = item.select_one(".structItem-title a[href*='/threads/']:not(.labelLink)")
        title = item.select_one(".structItem-title")
        if not a or not title:
            continue
        text = title.get_text(" ", strip=True)
        m = re.match(r"\s*(.{2,20}?)\s+hack:\s*(.+)", text, re.S | re.I)
        base, name = (m.group(1).strip(), m.group(2).strip()) if m else (_pc_guess_base(text), text)
        if not base:
            continue
        views = 0
        for dl in item.select(".structItem-cell--meta dl"):
            if "view" in dl.get_text(" ", strip=True).lower():
                dd = dl.find("dd")
                n = dd.get_text(strip=True).upper().replace(",", "") if dd else "0"
                mult = 1000 if n.endswith("K") else 1000000 if n.endswith("M") else 1
                try:
                    views = int(float(n.rstrip("KM")) * mult)
                except ValueError:
                    pass
        started = item.select_one(".structItem-startDate time")
        out.append({"date": (started.get("datetime") or "")[:10] if started else "",
                    "base": base, "title": name,
                    "url": requests.compat.urljoin(PC_FORUM, a["href"]).split("/unread")[0],
                    "views": views})
    return out


PC_BASES = ["HeartGold", "SoulSilver", "FireRed", "Fire Red", "LeafGreen", "Leaf Green",
            "Black 2", "White 2", "Emerald", "Ruby", "Sapphire", "Crystal", "Platinum",
            "Diamond", "Pearl", "Yellow", "Gold", "Silver", "Black", "White", "Red", "Blue",
            "Green"]


def _pc_guess_base(text):
    """Base game of an old thread without a 'FireRed hack:' prefix, e.g. '[Fire Red] ...'
    (bracketed names first, then anywhere in the title)."""
    text = re.sub(r"\bpoke(?=(red|blue|yellow|green|gold|silver|crystal|ruby|sapphire|"
                  r"emerald|firered|leafgreen)\b)", "", text, flags=re.I)
    if re.search(r"rgb\b", text, re.I) and not re.search(r"\bgold|silver|crystal", text, re.I):
        text += " Red"
    for part in re.findall(r"[\[(]([^\])]+)[\])]", text) + [text]:
        for b in PC_BASES:
            if re.search(rf"\b{b}\b", part, re.I):
                return b.replace("Fire Red", "FireRed").replace("Leaf Green", "LeafGreen")
    return None


def pc_crawl(delay=0.0):
    """Every thread in the showcase (it's only a few pages; past the last page the
    forum just repeats the last one)."""
    entries, page = {}, 1
    while page < 60:
        r = requests.get(PC_FORUM + (f"page-{page}" if page > 1 else ""),
                         headers=BROWSER_HEADERS, timeout=20)
        if not r.ok or (page > 1 and not r.url.rstrip("/").endswith(f"page-{page}")):
            break
        new = [e for e in pc_parse_listing(r.text) if e["url"] not in entries]
        if not new and page > 1:
            break
        for e in new:
            entries[e["url"]] = e
        page += 1
        time.sleep(delay)
    return list(entries.values())


def pc_index():
    global _pc
    if _pc is None:
        entries = {}
        try:
            with open(os.path.join(ASSETS, "pokecommunity.json"), encoding="utf-8") as f:
                for e in json.load(f):
                    entries[e["url"]] = e
        except OSError:
            pass
        fresh = None
        try:
            if time.time() - os.path.getmtime(PC_CACHE) < 7 * 86400:
                with open(PC_CACHE, encoding="utf-8") as f:
                    fresh = json.load(f)
        except (OSError, ValueError):
            pass
        if fresh is None:  # re-read weekly - the showcase is only a few pages long
            fresh = []
            try:
                fresh = pc_crawl()
                os.makedirs(CACHE_DIR, exist_ok=True)
                with open(PC_CACHE, "w", encoding="utf-8") as f:
                    json.dump(fresh, f)
            except requests.RequestException:
                pass
        for e in fresh:
            entries[e["url"]] = e
        _pc = list(entries.values())
    return _pc


def _pc_matches(base, game_name):
    """'FireRed' matches 'Pokemon - FireRed Version'; 'Black' doesn't match 'Black 2'."""
    want = [w for w in _tokens(base) if w != "pokemon"]
    have = [w for w in _tokens(game_name) if w != "pokemon"]
    if not want:
        return False
    joined = "".join(have)
    if not (all(w in have for w in want) or "".join(want) in have or
            "".join(want) == joined):
        return False
    return not any(w.isdigit() and w not in want for w in have)


def search_pokecommunity(game_name, platform="gba"):
    if "pokemon" not in _norm(game_name) and "pokmon" not in game_name.lower():
        return []
    hits = [e for e in pc_index() if _pc_matches(e["base"], game_name)]
    hits.sort(key=lambda e: -e.get("views", 0))
    return [{"source": "PokéCommunity", "title": e["title"], "url": e["url"],
             "desc": f"{e['base']} hack on PokéCommunity's Completed ROM Hacks Showcase"
                     + (f" - {e['views']:,} views" if e.get("views") else ""),
             "downloads": e.get("views", 0), "category": "hack"} for e in hits]


def _pc_first_post(url):
    if url not in _pc_pages:
        r = requests.get(url, headers=BROWSER_HEADERS, timeout=20)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        _pc_pages[url] = soup.select_one("article.message .bbWrapper") or soup
    return _pc_pages[url]


def list_pc_files(entry):
    files = thread_files([_pc_first_post(entry["url"])])
    files.append({"name": "Open the thread on PokéCommunity", "url": entry["url"],
                  "browser": True})
    return files


# ---------------- RomhackPlaza (sitemap index, refreshed weekly) ----------------

PLAZA_SITEMAP = "https://romhackplaza.org/sitemap.xml"
PLAZA_CACHE = os.path.join(CACHE_DIR, "romhackplaza_index.json")
PLAZA_SLUGS = {
    "snes": re.compile(r"-(snes|super-nintendo|super-famicom|sfc)(-romhack)?$"),
    "gba": re.compile(r"-(gba|game-boy-advance)(-romhack)?$"),
    "gbc": re.compile(r"-(gbc|game-boy-color)(-romhack)?$"),
    "gb": re.compile(r"-(gb|game-boy)(-romhack)?$"),
    "nds": re.compile(r"-(nds|nintendo-ds)(-romhack)?$"),
    "nes": re.compile(r"-(nes|nintendo-entertainment-system|famicom)(-romhack)?$"),
    "sms": re.compile(r"-(sms|master-system|sega-master-system)(-romhack)?$"),
    "genesis": re.compile(r"-(genesis|sega-genesis|mega-drive|megadrive|32x|sega-32x)"
                          r"(-romhack)?$"),
    "n64": re.compile(r"-(n64|nintendo-64)(-romhack)?$"),
    "gg": re.compile(r"-(gg|game-gear|sega-game-gear)(-romhack)?$"),
}


def plaza_index(force=False):
    if not force and os.path.exists(PLAZA_CACHE) and \
            time.time() - os.path.getmtime(PLAZA_CACHE) < 7 * 86400:
        with open(PLAZA_CACHE, encoding="utf-8") as f:
            data = json.load(f)
        if not data or "date" in data[0]:  # (lists made before dates were kept: rebuild)
            return data
    r = requests.get(PLAZA_SITEMAP, headers=BROWSER_HEADERS, timeout=30)
    r.raise_for_status()
    out = []
    for url, lastmod in re.findall(r"<loc>([^<]+)</loc>\s*(?:<lastmod>([^<]+)</lastmod>)?",
                                   r.text):
        parts = url.rstrip("/").split("/")
        if len(parts) < 5 or parts[3] not in ("romhacks", "translations"):
            continue
        slug = parts[4]
        plat = next((p for p, rx in PLAZA_SLUGS.items() if rx.search(slug)), None)
        if not plat:
            continue
        title = requests.utils.unquote(PLAZA_SLUGS[plat].sub("", slug)).replace("-", " ")
        out.append({"title": title.title(), "url": url, "platform": plat,
                    "kind": "translation" if parts[3] == "translations" else "hack",
                    "date": (lastmod or "")[:10]})
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(PLAZA_CACHE, "w", encoding="utf-8") as f:
        json.dump(out, f)
    return out


def search_plaza(game_name, platform="snes"):
    want = _words(_clean_title(game_name))
    if not want:
        return []
    out = []
    for e in plaza_index():
        if e.get("platform", "snes") == platform and want <= _words(e["title"]) \
                and about_game(e["title"], game_name):  # Lego Star Wars isn't Episode III
            r = {"source": "RomhackPlaza", "title": e["title"], "url": e["url"],
                 "desc": "Listed on romhackplaza.org"}
            if e["kind"] == "translation":
                r["category"] = "translation"
            out.append(r)
    return out


def list_plaza_files(entry):
    """Ask the page's file pop-up (a Livewire component) for its file list, which
    contains signed direct download links."""
    import html as htmllib
    fallback = [{"name": "Open on RomhackPlaza", "url": entry["url"], "browser": True}]
    sess = requests.Session()
    sess.headers.update(BROWSER_HEADERS)
    page = sess.get(entry["url"], timeout=20).text
    csrf = re.search(r'data-csrf="([^"]+)"', page)
    uri = re.search(r'data-update-uri="([^"]+)"', page)
    eid = re.search(r"entryOpenFilesModal', \{ entryId: (\d+)", page)
    snap = next((htmllib.unescape(m.group(1)) for m in
                 re.finditer(r'wire:snapshot="([^"]+)"', page)
                 if "entry-files-modal" in htmllib.unescape(m.group(1))), None)
    if not (csrf and uri and eid and snap):
        return fallback
    body = {"_token": csrf.group(1), "components": [{
        "snapshot": snap, "updates": {},
        "calls": [{"path": "", "method": "__dispatch",
                   "params": ["entryOpenFilesModal", {"entryId": int(eid.group(1))}]}]}]}
    r = sess.post(uri.group(1), json=body, headers={"X-Livewire": "1"}, timeout=20)
    if not r.ok:
        return fallback
    soup = BeautifulSoup(r.json()["components"][0]["effects"].get("html", ""), "html.parser")
    files = []
    for item in soup.select(".file-item"):
        link = item.find("a", href=re.compile(r"/api/fs/download/"))
        if not link:
            continue
        name_el = item.select_one(".file-name")
        name = (name_el.get("title") or name_el.get_text(strip=True)) if name_el else "file"
        meta = item.select_one(".file-meta")
        m = re.match(r"([\d.]+)\s*([KMG]B)", meta.get_text(strip=True) if meta else "")
        size = int(float(m.group(1)) * {"KB": 1 << 10, "MB": 1 << 20, "GB": 1 << 30}[m.group(2)])             if m else 0
        files.append({"name": name, "url": link["href"], "size": size})
    return files + fallback if files else fallback


# ---------------- curated (randomizers and other web-only hacks) ----------------

def search_curated(game_name, platform="snes"):
    try:
        with open(os.path.join(ASSETS, "popular_hacks.json"), encoding="utf-8") as f:
            items = json.load(f)
    except OSError:
        return []
    want = _norm(game_name)
    return [{"source": "Website", "title": e["title"], "desc": e["desc"], "url": e["url"],
             "scrape": e.get("scrape")}
            for e in items if _norm(e["game"]) == want
            and e.get("platform", "snes") == platform]


def list_romhack_ing(entry):
    return [{"name": "Search romhack.ing in your browser", "url": entry["url"],
             "browser": True}]


def list_curated_files(entry):
    """Official hack websites: offer the patch files linked on the page directly
    (their names often change per release), plus the page itself."""
    page = {"name": "Open the website", "url": entry["url"], "browser": True}
    if not entry.get("scrape"):
        return [page]
    from urllib.parse import urljoin
    r = requests.get(entry["scrape"], headers=BROWSER_HEADERS, timeout=20)
    if not r.ok:
        return [page]
    files = []
    for a in BeautifulSoup(r.text, "html.parser").find_all("a", href=True):
        url = urljoin(r.url, a["href"])
        name = os.path.basename(url.split("?")[0])
        if name.lower().endswith(PATCH_EXTS + ARCHIVE_EXTS) and is_patch_file(name):
            files.append({"name": name, "url": url})
    return files + [page]


# ---------------- RetroAchievements (official Web API, needs a free key) ----------------

RA_API = "https://retroachievements.org/API/API_GetGameList.php"
RA_CONSOLE = {"snes": 3, "gba": 5, "nes": 7, "gb": 4, "gbc": 6, "nds": 18, "sms": 11,
              "genesis": [1, 10],  # Genesis tab: Mega Drive + 32X
              "n64": 2, "gg": 15}
RA_KEY = ""  # set from Settings


def ra_games(platform, force=False):
    """Every game/hack with an achievement set for a console, cached for a week:
    [{"title", "id", "achievements"}]. Raises if no API key is set."""
    cache = os.path.join(CACHE_DIR, f"retroachievements2_{platform}.json")  # with hashes
    if not force and os.path.exists(cache) and time.time() - os.path.getmtime(cache) < 7 * 86400:
        with open(cache, encoding="utf-8") as f:
            return json.load(f)
    if not RA_KEY:
        raise RuntimeError("Add your RetroAchievements Web API key in Settings")
    ids = RA_CONSOLE[platform]
    out = []
    for cid in ids if isinstance(ids, list) else [ids]:
        r = requests.get(RA_API, params={"i": cid, "f": 1, "h": 1, "y": RA_KEY},
                         headers=HEADERS, timeout=30)
        if r.status_code == 401:
            raise RuntimeError("RetroAchievements didn't accept the API key - check it in "
                               "Settings")
        r.raise_for_status()
        out += [{"title": g.get("Title", ""), "id": g.get("ID"),
                 "achievements": g.get("NumAchievements", 0),
                 "hashes": [h.lower() for h in g.get("Hashes") or []]} for g in r.json()]
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(cache, "w", encoding="utf-8") as f:
        json.dump(out, f)
    return out


def ra_clean(title):
    """'~Hack~ Super Metroid: Redux' -> ('Super Metroid: Redux', True)"""
    tags = re.findall(r"~([^~]+)~", title)
    return re.sub(r"~[^~]+~\s*", "", title).strip(), "Hack" in tags


def ra_link(entry):
    return {"source": "RetroAchievements",
            "title": f"{entry['achievements']} achievements - supported patch versions",
            "desc": "RetroAchievements lists the exact ROM / patch versions that earn "
                    "achievements. Opens in your default browser.",
            "url": f"https://retroachievements.org/game/{entry['id']}/hashes",
            "category": "hack"}


def list_ra_files(entry):
    return [{"name": "Open RetroAchievements' supported versions page", "url": entry["url"],
             "browser": True}]


def romhack_ing_link(game_name):
    """romhack.ing asks bots not to crawl it, so we only offer a web search link,
    opened in the user's default browser."""
    q = requests.utils.quote(f"site:romhack.ing {game_name}")
    return {"source": "romhack.ing", "title": f"Search romhack.ing for {game_name}",
            "desc": "Opens a web search of romhack.ing in your default browser.",
            "url": f"https://www.google.com/search?q={q}", "category": "hack"}


RHN_PLATFORM = {"snes": 9, "gba": 10, "nes": 1, "gb": 8, "gbc": 8, "nds": 23, "sms": 22,
                "genesis": 11, "n64": 27, "gg": 12}


def browser_links(game_name, platform="snes"):
    """A web search row for each tab, opened with the default browser's own search engine:
    for hacks newer than the sources (or sites that block apps). The Downloads watcher
    applies what's downloaded from it."""
    links = []
    for cat, words in (("hack", "rom hack patch"), ("translation", "translation patch")):
        links.append({
            "source": "Web search", "category": cat, "live": True,
            "title": f"Search in your default browser for {game_name}",
            "desc": f"Searches the web for {game_name} {words}s in your default browser - "
                    "for anything newer than (or missing from) the other sources. Download the "
                    "patch there and Vibe Patcher applies it from your Downloads folder.",
            "url": "https://www.google.com/search?q=" + requests.utils.quote(
                f'"{game_name}" {words}')})
    return links


def search_all(game_name, platform="snes"):
    results, errors = [], []
    for fn in (search_rapatches, search_rhn, search_plaza, search_pokecommunity, search_hackdex, search_curated, search_github,
               search_zeldix, search_archive):
        try:
            results += fn(game_name, platform)
        except Exception as e:  # keep other sources working
            errors.append(f"{fn.__name__.replace('search_', '')}: {e}")
    try:  # hacks named on other sites, looked up on GitHub by name
        known = {r["repo"].lower() for r in results if r["source"] == "GitHub"}
        titles = [r["title"] for r in results if r["source"] in ("Romhacking.net",
                                                                 "RomhackPlaza")]
        for r in search_github_titles(titles, game_name, platform):
            if r["repo"].lower() not in known:
                known.add(r["repo"].lower())
                results.append(r)
        results = verify_github(results)
        results = verify_archive(results)
    except Exception as e:
        errors.append(f"github: {e}")
    for r in results:
        r.setdefault("category", classify(r))
    link_rapatches_to_rhn(results, game_name)
    return order_results(_archive_last_resort(results)), errors


SOURCE_ORDER = ["RAPatches", "GitHub", "RomhackPlaza", "Hackdex", "PokéCommunity", "PokéHarbor", "Romhacking.net", "Website", "Zeldix", "archive.org"]


def order_results(results):
    """Preferred sources first; within a source the original order is kept."""
    rank = {s: i for i, s in enumerate(SOURCE_ORDER)}
    # MSU-1: Zeldix is the home of MSU-1 packs, so its threads come first
    return sorted(results, key=lambda r: (not (r.get("category") == "msu1"
                                               and r["source"] == "Zeldix"),
                                          rank.get(r["source"], len(rank))))


def _archive_last_resort(results):
    """archive.org items are often stale: only keep them in a tab that has nothing else."""
    has_other = {r["category"] for r in results if r["source"] != "archive.org"}
    return [r for r in results if r["source"] != "archive.org" or r["category"] not in has_other]


STOP = {"the", "of", "a", "an", "and", "to", "in", "edition", "hack", "version", "v"}


def _tokens(text):
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return [w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOP]


def _mentions(text, phrase):
    """True if text mentions every word of phrase (or the phrase with spacing removed,
    so 'X-Fusion' matches 'xfusion')."""
    words = _tokens(phrase)
    if not words:
        return True
    toks = set(_tokens(text))
    return all(w in toks for w in words) or "".join(words) in "".join(_tokens(text))


COMMON_HACK_WORDS = {
    "multiplayer", "coop", "co", "op", "online", "netplay", "redux", "deluxe", "dx", "remix",
    "remastered", "remaster", "enhanced", "improved", "improvement", "restored",
    "restoration", "randomizer", "rando", "hard", "mode", "kaizo", "plus", "ex", "ultimate",
    "definitive", "complete", "expanded", "extended", "director", "cut", "fix", "fixed",
    "patch", "uncensored", "retranslation", "translation", "widescreen", "msu", "1",
    "edition", "version", "hack", "mod", "remake", "reborn", "reloaded", "revisited"}


def search_hack(base_name, hack, platform="snes"):
    """Search for one specific hack of a base game (e.g. 'Redux' of
    'The Legend of Zelda: A Link to the Past') and keep only results about it."""
    results, errors = search_all(base_name, platform)
    if not _tokens(hack):  # symbol-only name like "Chrono Trigger+": match it literally
        literal = re.sub(r"\s+", "", (base_name + hack).lower())
        found = [r for r in results if literal in re.sub(r"\s+", "", r["title"].lower())]
        return order_results(_archive_last_resort(found)), errors
    # match on titles only: descriptions say things like "Complete for Chrono Trigger"
    found = [r for r in results if _mentions(_result_name(r), hack)]
    # a second search by the hack's own name catches repos that don't name the game
    base_words = [w for w in _tokens(base_name) if len(w) > 3]
    try:
        extra, more_err = search_all(hack, platform)
        errors += more_err
    except Exception as e:
        extra, errors = [], errors + [str(e)]
    seen = {r["url"] for r in found}
    for r in extra:
        text = f"{r['title']} {r.get('desc', '')}"
        # a name like "MaternalBound Redux" identifies the hack by itself; a common
        # word like "Multiplayer" or "Redux" doesn't (Rimworld multiplayer mods...)
        hack_words = _tokens(hack)
        distinctive = (len(hack_words) >= 2 and len("".join(hack_words)) >= 8
                       and any(w not in COMMON_HACK_WORDS for w in hack_words)
                       and _mentions(r["title"], hack))
        names_base = all(w in _tokens(text) for w in base_words) or about_game(text, base_name)
        if r["url"] not in seen and _mentions(_result_name(r), hack) and \
                (distinctive or names_base):
            found.append(r)
            seen.add(r["url"])
    # results that are clearly this hack win; looser matches only if there are none
    exact = [r for r in found if is_this_hack(_result_name(r), base_name, hack)]
    return order_results(_archive_last_resort(exact or found)), errors


def _result_name(r):
    """The hack's name as a result shows it (GitHub: the repo name, not the owner)."""
    title = r["title"]
    if r["source"] == "GitHub":
        title = title.split("/", 1)[-1]
    return re.sub(r"\s*\(MSU-1\)$", "", title)


VERSIONISH = re.compile(
    r"^(the|a|an|v?\d+(\.\d+)*[a-z]?|rc\d*|beta|alpha|final|english|eng|en|translation|patch|"
    r"hack|romhack|snes|gba|sfc|super nintendo|ips|bps|ups|msu1?|fix(ed)?|version|"
    r"edition|update[d]?|official|release|repo(sitory)?|[\s\-_.:()\[\]'\"])*$", re.I)


def is_this_hack(title, base_name, hack):
    """Is this result the selected hack itself (not another hack of the same game)?
    True when the title is the hack's name - with or without the base game's name in
    front, and allowing versions / 'English' / 'patch' and such around it."""
    t = _norm(title)
    names = {_norm(hack), _norm(f"{base_name} {hack}")}
    names |= {_norm(f"{p} {hack}") for p in _search_phrase(base_name).split(":")}
    names.discard("")
    for n in names:
        if n not in t:
            continue
        # what's left around the name must only be version / language / "patch" words
        rest = _spaced_rest(title, n)
        if rest is not None and VERSIONISH.match(rest):
            return True
    return False


def _spaced_rest(title, name_norm):
    """The title with the matched name removed (None if the name isn't a run of whole
    words in it)."""
    words = re.findall(r"[A-Za-z0-9]+(?:\.[0-9]+)*", unicodedata.normalize(
        "NFKD", title).encode("ascii", "ignore").decode())
    # drop the shortest run of words whose letters spell the name
    for i in range(len(words)):
        acc = ""
        for j in range(i, len(words)):
            acc += re.sub(r"[^a-z0-9]", "", words[j].lower())
            if acc == name_norm:
                return " ".join(words[:i] + words[j + 1:])
            if len(acc) >= len(name_norm):
                break
    return None


_desc_cache = {}


def _clean_text(text, limit=6000):
    text = re.sub(r"\r", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()[:limit]


def fetch_description(entry):
    """The full description of a result, read from its page (cached).
    Returns (text, extra_links) - extra_links e.g. the hack's romhack.ing page."""
    key = entry.get("url")
    if key in _desc_cache:
        return _desc_cache[key]
    text, links = entry.get("desc", ""), []
    src = entry["source"]
    if src == "Romhacking.net" and entry.get("full_desc"):
        text = f"{entry['desc']}\n\n{entry['full_desc'].strip()}"
    elif src == "Romhacking.net":
        m = re.search(r"/(hacks|translations)/(\d+)/", entry["url"])
        if m:
            path = f"www.romhacking.net/{m.group(1)}/{m.group(2)}/index.html"
            r = requests.get("https://archive.org/download/romhackingbackup08102024/"
                             "www.romhacking.net.zip/" + requests.utils.quote(path, safe=""),
                             headers=HEADERS, timeout=30)
            if r.ok:
                soup = BeautifulSoup(r.content, "html.parser")
                h = soup.find(["h3", "h2"], string=re.compile("Description", re.I))
                body = h.find_next_sibling() if h else None
                if body:
                    text = body.get_text("\n", strip=True)
    elif src == "RAPatches" and entry.get("rhn_url"):
        # romhacking.net's description of the same hack (from its 2024 backup)
        text, links = fetch_description({"source": "Romhacking.net", "url": entry["rhn_url"],
                                         "desc": entry.get("rhn_desc") or text,
                                         "full_desc": entry.get("rhn_full", "")})
        text += "\n\n(Description from romhacking.net. Patch from RetroAchievements' RAPatches.)"
    elif src == "Hackdex":
        full = entry.get("full_desc")
        if not full:  # the bundled list has no descriptions: read the hack's page
            full = (_safe_hd(entry["url"]) or {}).get("desc", "")
        text = f"{entry['desc']}\n\n{full}".strip()
    elif src == "PokéHarbor":
        content = _ph_content(entry["url"])
        text = content.get_text("\n", strip=True)[:6000] or text
    elif src == "PokéCommunity":
        post = _pc_first_post(entry["url"])
        text = post.get_text("\n", strip=True)[:6000] or text
    elif src == "RomhackPlaza":
        r = requests.get(entry["url"], headers=BROWSER_HEADERS, timeout=20)
        if r.ok:
            soup = BeautifulSoup(r.text, "html.parser")
            descs = soup.select(".entry-description")
            if descs:
                text = descs[0].get_text("\n", strip=True)
    elif src == "GitHub":
        r = requests.get(f"https://api.github.com/repos/{entry['repo']}/readme",
                         headers={**_gh_headers(), "Accept": "application/vnd.github.raw"},
                         timeout=20)
        if r.ok:
            md = r.text
            md = re.sub(r"<[^>]+>", "", md)                     # html tags
            md = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", md)       # images
            md = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", md)   # links -> their text
            md = re.sub(r"^#+\s*", "", md, flags=re.M)         # heading marks
            md = re.sub(r"^\s*[-=*_]{3,}\s*$", "", md, flags=re.M)  # horizontal rules
            md = re.sub(r"(\*\*|__|`)", "", md)                 # bold / code marks
            md = re.sub(r"^\s*[*+-]\s+", "• ", md, flags=re.M)  # bullets
            text = ((entry.get("desc") or "") + "\n\n" + md).strip()
    elif src == "Zeldix":
        r = requests.get(entry["url"], headers=BROWSER_HEADERS, timeout=20)
        if r.ok:
            post = BeautifulSoup(r.text, "html.parser").select_one(".postbody .content")
            if post:
                text = post.get_text("\n", strip=True)
    result = (_clean_text(text or "No description available."), links)
    _desc_cache[key] = result
    return result


def list_files(entry):
    return {"GitHub": list_github_files, "archive.org": list_archive_files,
            "Zeldix": list_zeldix_files, "Romhacking.net": list_rhn_files,
            "RomhackPlaza": list_plaza_files, "Website": list_curated_files,
            "romhack.ing": list_romhack_ing, "PokéCommunity": list_pc_files,
            "RAPatches": list_rapatches_files, "PokéHarbor": list_pokeharbor_files,
            "Hackdex": list_hackdex_files,
            "Web search": lambda e: [{"name": "Open the search in your browser",
                                      "url": e["url"], "browser": True}],
            "RetroAchievements": list_ra_files}[entry["source"]](entry)


# ---------------- download + extract ----------------

# ---------------- MediaFire / Mega direct downloads ----------------

def mediafire_direct(page_url):
    """MediaFire file pages carry the real download link (sometimes base64-scrambled)."""
    import base64
    r = requests.get(page_url, headers=BROWSER_HEADERS, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    btn = soup.select_one("a#downloadButton") or soup.find(
        "a", href=re.compile(r"https?://download\d*\.mediafire\.com/"))
    if btn is not None:
        if btn.get("data-scrambled-url"):
            return base64.b64decode(btn["data-scrambled-url"]).decode()
        if btn.get("href", "").startswith("http"):
            return btn["href"]
    m = re.search(r'https?://download\d*\.mediafire\.com/[^"\'\s<>]+', r.text)
    if m:
        return m.group(0)
    raise RuntimeError("MediaFire didn't give a download link (the file may be removed). "
                       "Try opening it in your browser.")


def mega_file_link(url):
    """(file id, key) for a public Mega *file* link, else None (folders need the browser)."""
    m = re.search(r"mega(?:\.co)?\.nz/(?:file/|#!)([\w-]{8})[#!]([\w-]{43})", url)
    return (m.group(1), m.group(2)) if m else None


def _b64url(s):
    import base64
    s = s.replace("-", "+").replace("_", "/").replace(",", "")
    return base64.b64decode(s + "=" * (-len(s) % 4))


def mega_download(url, folder, progress=lambda *a: None):
    """Download and decrypt a public Mega file - the same steps Mega's own website does
    in the browser (AES-128-CTR, key and IV come from the link). Returns the local path."""
    import struct
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    fid, key_b64 = mega_file_link(url)
    raw = _b64url(key_b64)
    a = struct.unpack(">8I", raw)
    key = struct.pack(">4I", a[0] ^ a[4], a[1] ^ a[5], a[2] ^ a[6], a[3] ^ a[7])
    iv = struct.pack(">2I", a[4], a[5]) + b"\0" * 8
    r = requests.post("https://g.api.mega.co.nz/cs", params={"id": 0},
                      json=[{"a": "g", "g": 1, "p": fid}], headers=HEADERS, timeout=30)
    r.raise_for_status()
    info = r.json()[0]
    if isinstance(info, int):
        raise RuntimeError("Mega says that file isn't available (it may have been removed).")
    # the file name is in the encrypted attributes: "MEGA{...json...}"
    name = f"mega_{fid}"
    try:
        dec = Cipher(algorithms.AES(key), modes.CBC(b"\0" * 16)).decryptor()
        attr = dec.update(_b64url(info["at"])) + dec.finalize()
        name = json.loads(attr[4:].rstrip(b"\0").decode())["n"]
    except Exception:
        pass
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    local = os.path.join(folder, name)
    total = int(info.get("s", 0))
    dec = Cipher(algorithms.AES(key), modes.CTR(iv)).decryptor()
    done = 0
    with requests.get(info["g"], stream=True, timeout=60) as f, open(local, "wb") as out:
        f.raise_for_status()
        for chunk in f.iter_content(1 << 20):
            out.write(dec.update(chunk))
            done += len(chunk)
            progress(f"Downloading {name} from Mega… {done / 1048576:.1f} of "
                     f"{total / 1048576:.1f} MB", done, total)
        out.write(dec.finalize())
    return local


def download(file_info, game_name, download_dir=None, progress=lambda *a: None):
    """Download a patch or pack. Returns (patch_files, folder)."""
    folder = os.path.join(download_dir or DOWNLOAD_DIR, re.sub(r'[<>:"/\\|?*]', "_", game_name))
    os.makedirs(folder, exist_ok=True)
    if file_info.get("rhn_member"):  # from romhacking.net's dump on archive.org
        import archive_org
        member = next(i for i in archive_org.rhn_zip().infolist()
                      if i.filename == file_info["rhn_member"])
        name = re.sub(r'[<>:"/\\|?*]', "_", archive_org.display_name(member.filename))
        local = archive_org.extract_member(member, os.path.join(folder, name), progress)
        progress("Extracting…")
        return extract_patches(local, everything=file_info.get("msu", False))
    if file_info.get("mega"):
        local = mega_download(file_info["url"], folder, progress)
        progress("Extracting…")
        return extract_patches(local, everything=file_info.get("msu", False))
    url = mediafire_direct(file_info["url"]) if file_info.get("mediafire") else file_info["url"]
    with requests.get(url, stream=True, timeout=60, headers=HEADERS) as r:
        if r.status_code in (403, 404, 410):
            host = re.sub(r"^www\.|^drive\.usercontent\.", "", url.split("/")[2])
            raise RuntimeError(f"That file is no longer available on {host} (it was removed "
                               "or made private). Try another link in the thread.")
        r.raise_for_status()
        if "text/html" in r.headers.get("Content-Type", ""):
            raise RuntimeError("That link is a web page, not a file (it may be private "
                               "or need a login). Try opening it in your browser.")
        name = os.path.basename(file_info["name"])
        cd = r.headers.get("Content-Disposition", "")
        m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", cd)
        if m:
            name = requests.utils.unquote(m.group(1))
        name = re.sub(r'[<>:"/\\|?*]', "_", name)
        local = os.path.join(folder, name)
        total = int(r.headers.get("Content-Length", 0))
        done = 0
        with open(local, "wb") as f:
            for chunk in r.iter_content(1 << 16):
                f.write(chunk)
                done += len(chunk)
                mb = done / 1048576
                progress(f"Downloading {name}… {mb:.1f} MB"
                         + (f" of {total / 1048576:.1f} MB" if total else ""), done, total)
    progress("Extracting…")
    return extract_patches(local, everything=file_info.get("msu", False))


def _safe_target(dest, member):
    target = os.path.realpath(os.path.join(dest, member))
    if not target.startswith(os.path.realpath(dest) + os.sep):
        return None  # path traversal attempt
    return target


def extract_patches(path, everything=False):
    """Extract a download, then any archives inside it (a .zip inside a .rar, etc.),
    up to 3 levels deep. Returns (patch_files, folder)."""
    found, dest = _extract_once(path, everything)
    for _level in range(3):
        nested = [os.path.join(r, f) for r, _d, fs in os.walk(dest) for f in fs
                  if f.lower().endswith(ARCHIVE_EXTS)] if os.path.isdir(dest) else []
        if not nested:
            break
        for arc in nested:
            try:
                more, _sub = _extract_once(arc, everything, inner=True)
                found += more
            except Exception:
                log.warning("Couldn't open the archive inside the download: %s", arc)
            if os.path.exists(arc):
                os.remove(arc)  # unpacked (or unreadable) - don't loop on it
    if not found and getattr(_extract_once, "program", False):
        raise RuntimeError("That download is a program (a romhacking tool or PC "
                           "build), not a patch for your ROM.")
    return found, dest


def _extract_once(path, everything=False, inner=False):
    """Extract a download. Normally only patch files and readmes are kept; with
    everything=True (MSU-1 packs) all files except ROM images are kept.
    Returns (patch_files, folder)."""
    low = path.lower()
    if low.endswith(PATCH_EXTS):
        return [path], os.path.dirname(path)
    dest = os.path.splitext(path)[0]
    os.makedirs(dest, exist_ok=True)
    keep = lambda n: (not n.endswith(ROM_EXTS)) and (
        everything or n.endswith(PATCH_EXTS + ARCHIVE_EXTS + (".txt", ".md", ".nfo")))
    found = []
    if low.endswith(".zip"):
        try:
            with zipfile.ZipFile(path) as z:
                for m in z.infolist():
                    name = m.filename.lower()
                    if m.is_dir() or not keep(name):
                        continue
                    target = _safe_target(dest, m.filename if everything
                                          else os.path.basename(m.filename))
                    if not target:
                        continue
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    with z.open(m) as src, open(target, "wb") as out:
                        while chunk := src.read(1 << 20):
                            out.write(chunk)
                    if name.endswith(PATCH_EXTS):
                        found.append(target)
        except (zipfile.BadZipFile, zipfile.LargeZipFile, NotImplementedError, OSError,
                EOFError) as e:
            # Python is strict about odd zips (unusual headers, newer compression);
            # Windows' tar (libarchive) is more forgiving, so try that before giving up
            try:
                found = _extract_rar(path, dest, keep, everything)
            except Exception:
                raise RuntimeError(f"The download looks damaged or incomplete ({e}). "
                                   "Try downloading it again.")
    elif low.endswith((".7z", ".rar")):
        # Windows 10/11's built-in tar reads both (7-Zip / WinRAR as fallbacks)
        found = _extract_rar(path, dest, keep, everything)
    else:
        return [], os.path.dirname(path)  # e.g. a readme; nothing to extract
    if not inner:
        _extract_once.program = False
    if not found and low.endswith(".zip") and not inner:
        with zipfile.ZipFile(path) as z:  # checked once the nested archives are open too
            _extract_once.program = any(n.lower().endswith((".exe", ".dll", ".jar", ".app"))
                                        for n in z.namelist())
    if everything and not inner:
        os.remove(path)  # packs are big; keep only the extracted copy
    return found, dest


def _extract_rar(path, dest, keep, everything):
    """Unpack into a temp folder, then copy over only what we keep (never ROMs)."""
    import tempfile
    import archive_tools
    tmp = tempfile.mkdtemp(prefix="vibepatcher_rar_")
    try:
        archive_tools.extract(path, tmp)  # UnRAR, 7-Zip or tar - whichever can read it
        found = []
        for root, _dirs, files in os.walk(tmp):
            for f in files:
                rel = os.path.relpath(os.path.join(root, f), tmp)
                if not keep(f.lower()):
                    continue
                target = _safe_target(dest, rel if everything else f)
                if not target:
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                shutil.copyfile(os.path.join(root, f), target)
                if f.lower().endswith(PATCH_EXTS):
                    found.append(target)
        return found
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def find_msu_name(folder):
    """If folder holds an MSU-1 pack, return the base name the ROM must use."""
    for root, _dirs, files in os.walk(folder):
        for f in files:
            if f.lower().endswith(".msu"):
                return root, os.path.splitext(f)[0]
        for f in files:  # no .msu file: derive from track names like game-1.pcm
            m = re.match(r"(.+)-\d+\.pcm$", f, re.I)
            if m:
                return root, m.group(1)
    return None


# ---------------- weekly update check ----------------

def _cached(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def refresh_online(platforms, progress=lambda *a: None):
    """Re-download RomhackPlaza, Zeldix and (with a key) RetroAchievements, and report
    what's new since last time: {'plaza': n, 'zeldix': n, 'ra': {platform: n}}."""
    new = {"plaza": 0, "zeldix": 0, "ra": {}}
    progress("Checking RomhackPlaza for new hacks…")
    before = {e["url"] for e in (_cached(PLAZA_CACHE) or [])}
    try:
        after = plaza_index(force=True)
        new["plaza"] = len([e for e in after if e["url"] not in before]) if before else 0
    except Exception:
        pass
    progress("Checking Zeldix for new MSU-1 packs…")
    before = set((_cached(ZELDIX_CACHE) or {}).values())
    try:
        after = _zeldix_index(force=True)
        new["zeldix"] = len(set(after.values()) - before) if before else 0
    except Exception:
        pass
    if RA_KEY:
        for p in platforms:
            progress(f"Checking RetroAchievements ({p})…")
            cache = os.path.join(CACHE_DIR, f"retroachievements2_{p}.json")
            before = {e["id"] for e in (_cached(cache) or [])}
            try:
                after = ra_games(p, force=True)
                if before:
                    new["ra"][p] = len([e for e in after if e["id"] not in before])
            except Exception:
                pass
    return new
