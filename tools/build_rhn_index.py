"""Build assets/rhn_<console>.json: every hack and English translation listed on
romhacking.net for a console, read from the public archive.org backup of the site
(Aug 2024).

The live site sits behind a Cloudflare bot check, so we never touch it here.
Run:  python tools/build_rhn_index.py snes      (or gba, nes, n64, gb, gbc, nds, sms, genesis, gg)
      python tools/build_rhn_index.py snes --downloads   (also read each hack's page
      for its download count - slower, ~1 request per entry)
"""
import json
import os
import re
import sys
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

import requests
from bs4 import BeautifulSoup

ZIP = "https://archive.org/download/romhackingbackup08102024/www.romhacking.net.zip/"
ASSETS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets")
# romhacking.net platform id, and how many listing pages the backup holds
# per console: romhacking.net sections as (platform id, hack pages, translation pages)
# that the backup holds. 0 = everything fits on one page, None = no listing.
CONSOLES = {
    "snes": [(9, 94, 22)],
    "gba": [(10, 29, 3)],
    "nes": [(1, 167, 32)],
    "gb": [(8, 26, 8)],        # romhacking.net files Game Boy Color under Game Boy
    "gbc": [(8, 26, 8)],
    "nds": [(23, 11, 3)],
    "sms": [(22, 4, 0)],
    "genesis": [(11, 42, 3), (31, 0, None)],  # Genesis / Mega Drive + 32X
    "n64": [(27, 11, 2)],
    "gg": [(12, 0, 2)],
}
SEP = "\uf03f"  # how the backup's file names write "?"


def lists(console):
    """[(kind, listing page path)] for every page of every section."""
    jobs = []
    for pid, hack_pages, tl_pages in CONSOLES[console]:
        for kind, page, n in (("hack", f"page=hacks&platform={pid}", hack_pages),
                              ("translation",
                               f"page=translations&platform={pid}&languageid=12", tl_pages)):
            if n is None:
                continue
            base = f"www.romhacking.net/index.html{SEP}{page}&dir=0&order=Date"
            if n == 0:
                jobs.append((kind, base + ".html"))
            else:
                jobs += [(kind, f"{base}&startpage={i}.html") for i in range(1, n + 1)]
    return jobs


def fetch(path):
    url = ZIP + urllib.parse.quote(path, safe="")
    for _ in range(3):
        try:
            r = requests.get(url, timeout=60)
            if r.ok:
                try:  # pages are a mix of UTF-8 and Windows-1252
                    return r.content.decode("utf-8")
                except UnicodeDecodeError:
                    return r.content.decode("cp1252", errors="replace")
        except requests.RequestException:
            pass
    return ""


def parse(html, kind):
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for tr in soup.select("table tr"):
        cells = tr.find_all("td")
        link = tr.find("a", href=re.compile(r"(hacks|translations)/\d+/"))
        if len(cells) < 6 or not link:
            continue
        rid = re.search(r"(hacks|translations)/(\d+)/", link["href"])
        text = [c.get_text(" ", strip=True) for c in cells]
        rows.append({
            "kind": kind,
            "id": int(rid.group(2)),
            "title": text[0],
            "author": text[1],
            "game": text[2],
            "category": text[6] if kind == "hack" and len(text) > 6 else "Translation",
            "version": text[-2],
            "date": text[-1],
            "url": f"https://www.romhacking.net/{rid.group(1)}/{rid.group(2)}/",
        })
    return rows


def add_downloads(rows):
    """Each hack's own page lists how many times it was downloaded."""
    def one(row):
        kind = "hacks" if row["kind"] == "hack" else "translations"
        html = fetch(f"www.romhacking.net/{kind}/{row['id']}/index.html")
        m = re.search(r"Downloads</th>\s*<td[^>]*>\s*([\d,]+)", html)
        row["downloads"] = int(m.group(1).replace(",", "")) if m else None
    with ThreadPoolExecutor(6) as pool:
        for i, _ in enumerate(pool.map(one, rows), 1):
            if i % 200 == 0:
                print(f"  {i}/{len(rows)} pages read", flush=True)


def main(console, downloads=False):
    jobs = lists(console)
    out, seen = [], set()
    with ThreadPoolExecutor(4) as pool:
        for kind, html in zip([k for k, _ in jobs], pool.map(lambda j: fetch(j[1]), jobs)):
            for row in parse(html, kind):
                key = (row["kind"], row["id"])
                if key not in seen:
                    seen.add(key)
                    out.append(row)
    if downloads:
        add_downloads(out)
    out_path = os.path.join(ASSETS, f"rhn_{console}.json")
    os.makedirs(ASSETS, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=0)
    print(len(out), "entries ->", out_path)


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    main(args[0] if args else "snes", "--downloads" in sys.argv)
