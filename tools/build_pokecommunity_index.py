"""Build assets/pokecommunity.json: every thread in PokéCommunity's Completed ROM Hacks
Showcase (base game, title, link, views). The app re-reads the newest pages weekly, so
this is only a starting list for offline use. Pages are fetched slowly to be polite."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sources  # noqa: E402

OUT = os.path.join(sources.ASSETS, "pokecommunity.json")


def main():
    entries = sources.pc_crawl(delay=1.0)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(sorted(entries, key=lambda e: -e["views"]), f, ensure_ascii=False, indent=0)
    print("wrote", OUT, len(entries))


if __name__ == "__main__":
    main()
