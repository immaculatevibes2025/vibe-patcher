"""Updates for the ROM hacks the user has patched.

Each time a hack is patched, where it came from is remembered with a version stamp:
- RAPatches: the file's Git fingerprint (it changes whenever the patch is replaced)
- GitHub: the release tag the file came from
- RomhackPlaza: the file's name (new versions are uploaded under new names)
The weekly check compares those stamps with what the sources have now. romhacking.net's
2024 archive never changes, so hacks from it aren't tracked."""
import re
import time

import requests

import sources


def stamp(result, finfo):
    """{source, stamp, ...} for a patched file, or None if its source can't be tracked."""
    src = (result or {}).get("source")
    if src == "RAPatches":
        sha = next((e.get("sha") for e in sources.rapatches_index()
                    if e["path"] == result.get("rap_path")), None)
        return {"source": src, "stamp": sha, "rap_path": result.get("rap_path")} if sha \
            else None
    if src == "GitHub" and "/releases/download/" in finfo.get("url", ""):
        tag = finfo["url"].split("/releases/download/")[1].split("/")[0]
        return {"source": src, "stamp": requests.utils.unquote(tag), "repo": result["repo"]}
    if src == "RomhackPlaza":
        return {"source": src, "stamp": finfo.get("name", ""), "page": result.get("url")}
    return None


def record(installed, result, finfo, game, platform):
    """Add / refresh this hack in the installed list (a list kept in the settings)."""
    st = stamp(result, finfo)
    if not st:
        return installed
    key = f"{st['source']}|{result.get('rap_path') or result.get('repo') or result['url']}"
    installed = [e for e in installed if e.get("key") != key]
    installed.append({"key": key, "title": result["title"], "game": game["name"],
                      "game_id": game["id"], "platform": platform, "url": result.get("url"),
                      "file": finfo.get("name", ""), "when": time.strftime("%Y-%m-%d"),
                      **st})
    return installed


def _github_latest(repo):
    r = requests.get(f"https://api.github.com/repos/{repo}/releases/latest",
                     headers=sources._gh_headers(), timeout=20)
    if r.ok:
        return r.json().get("tag_name")
    page = requests.get(f"https://github.com/{repo}/releases/latest",
                        headers=sources.BROWSER_HEADERS, timeout=20)
    m = re.search(r"/releases/tag/([^/?#]+)$", page.url)
    return requests.utils.unquote(m.group(1)) if m else None


def check(installed, progress=lambda *a: None):
    """[{title, game, game_id, platform, source, url, new}] - hacks with a newer version."""
    found = []
    rap = None
    for e in installed:
        try:
            if e["source"] == "RAPatches":
                if rap is None:
                    progress("Checking RAPatches for updated hacks…")
                    rap = {x["path"]: x.get("sha") for x in sources.rapatches_index(force=True)}
                now = rap.get(e["rap_path"])
                if now and now != e["stamp"]:
                    found.append({**e, "new": "a new version of the patch on RAPatches"})
            elif e["source"] == "GitHub":
                progress(f"Checking {e['repo']} on GitHub…")
                tag = _github_latest(e["repo"])
                if tag and tag != e["stamp"]:
                    found.append({**e, "new": f"release {tag} (you have {e['stamp']})"})
            elif e["source"] == "RomhackPlaza":
                progress(f"Checking {e['title']} on RomhackPlaza…")
                names = [f["name"] for f in sources.list_plaza_files({"url": e["page"]})
                         if not f.get("browser")]
                if names and e["stamp"] not in names:
                    found.append({**e, "new": f"new file {names[0]}"})
        except Exception:
            continue  # a site being down isn't an update
    return found
