# Vibe Patcher

A Windows desktop app for browsing games by their covers, finding ROM hacks,
translations and MSU-1 packs, and applying them to **your own** ROMs in a click.

**Consoles:** NES, SNES, N64, Game Boy, Game Boy Color, GBA, DS, Master System,
Genesis / Mega Drive (with 32X) and Game Gear.

> Vibe Patcher never downloads ROMs - only patches. You supply your own game dumps,
> and your original ROM is never changed.

## Getting started

- **Portable build (easiest):** unzip `Vibe Patcher Portable.zip` and run
  `Vibe Patcher.exe`. Everything it saves (settings, caches, downloads) stays in a
  `data` folder beside it.
- **From source:** Python 3 with `requests`, `Pillow`, `beautifulsoup4` and
  `cryptography`, then run `python app.py` (or `Vibe Patcher.bat`). Settings live in
  `%USERPROFILE%\.vibe_patcher`.
- **First start:** a welcome message suggests scanning your ROM folders. The first run
  downloads the LaunchBox games database once (~100 MB) for names, dates and covers;
  other lists load in the background while you browse.
- **Build the portable app:** `tools/build_portable.bat` (needs PyInstaller). It also
  bundles UnRAR so every `.rar` opens.

## Browsing

- **Console tabs** under the logo - drag one sideways to reorder them. Each console
  has its own games, hacks, owned ROMs and searches.
- **Category tabs:**
  - **Rom Hacks** - every game and hack.
  - **Translations** - games with a translation patch (English first).
  - **MSU-1** (SNES) - games with an MSU-1 audio pack.
  - **Pokémon** (GBA, GB, GBC, DS) - Pokémon hacks.
  - **RetroAchievements** - hacks with an official achievement set (needs your free
    Web API key; the tab explains how to get it).
  - **2024 Archive** - all of romhacking.net up to August 2024, with downloads inside
    the app (free archive.org account; you sign in inside the tab).
- **Covers:** from LaunchBox. Hacks without their own use their base game's cover, or
  their box art from Hackdex. Games LaunchBox doesn't list still get a tile.
- **Search** matches game and hack names (e.g. "maternalbound" finds EarthBound and
  MaternalBound Redux). Press a letter to jump to games starting with it.
- **Sort:** A-Z / Z-A, Popularity (Hackdex downloads for Pokémon hacks, romhacking.net
  downloads, then LaunchBox votes), Rating (LaunchBox, weighted by number of votes),
  Newest and Oldest (dates from LaunchBox, romhacking.net, RomhackPlaza, Hackdex,
  PokéCommunity and PokéHarbor).
- **Filter:** Only Show Rom Hacks, Only Show Games I Own (and their hacks), Only Show
  Games With Direct Downloads, and Show Hidden Games (right-click a cover to hide it).
- **Updates (n)** next to Scan Games: newer versions of hacks you've patched, found by
  the weekly check on RAPatches, GitHub and RomhackPlaza.

## Finding and applying patches

Select a game and click **FIND PATCHES** (or double-click its cover). Pick a result,
then a file under **Patch files**, and click **Download & apply to my ROM** - or just
double-click the file.

- **Direct downloads** happen inside the app. Pages that need a browser open in your
  default browser, and Vibe Patcher watches your Downloads folder and applies the
  patch when it arrives.
- **Archives** (.zip / .7z / .rar, even archives inside archives) are unpacked
  automatically. When a download holds several patches (versions, PAL / NTSC...), you
  pick one.
- **Patch formats:** IPS, UPS, BPS and xdelta / VCDIFF, all built in. SNES copier
  headers and N64 byte orders (.v64 / .n64 to .z64) are handled automatically.
- **ROM version check:** a badge under the cover says whether the patch works with
  your ROM - from the ROM it lists (checksums or No-Intro name), its description, and
  after download the patch's own checksum or readme. You're asked before applying a
  patch made for a different version.
- **Your ROM:** for games you own, your scanned ROM is used automatically (or the app
  asks which file to use).
- **Patched ROMs** are saved as new files next to your original, named after the hack.
  MSU-1 games get their own folder, and Vibe Patcher installs the music (.pcm) from
  your Downloads folder - loose or zipped - renamed to match the ROM.
- **Download progress** shows in the bottom-left bar, which turns green when the patch
  is done.

## Where patches come from

Listed in this order, best source first:

- **RAPatches** (RetroAchievements' patch collection on GitHub) - hacks,
  translations, fixes and MSU-1 packs, usually the newest versions. In-app downloads.
- **GitHub** - repos found by game name and by hack names from other sources; only
  repos that really hold a patch are listed. A free GitHub token (Settings > Accounts)
  raises GitHub's search limits.
- **RomhackPlaza** - in-app downloads.
- **Hackdex** (Pokémon) - each hack's version, downloads, box art and the exact ROM
  it needs. Its downloads are for browsers, so "Open on Hackdex" opens the page and a
  ROM you patch there is filed next to your original automatically; a hack's own
  GitHub releases download in-app.
- **PokéCommunity** (Pokémon) - Mega / Drive / MediaFire / Dropbox files in the
  showcase threads download in-app.
- **romhacking.net** - the live site blocks apps; hacks up to August 2024 come from its
  backup on archive.org (2024 Archive tab).
- **Official hack sites and randomizers** listed in `assets/popular_hacks.json`.
- **Zeldix** (SNES MSU-1) - pack links download in-app, and each thread is read for the
  ROM versions it supports.
- **archive.org** - only items that contain a patch, and only when nothing else has one.
- **Search in your default browser for ...** - a web search for anything newer or
  missing.

PC ports, romhacking tools and pre-patched ROMs are left out. PokéHarbor is used only
to add tiles for Pokémon hacks no other source lists - its downloads are pre-patched
ROMs, so nothing from it is shown or downloaded.

## Your ROMs

- **Scan Games:** add your ROM folders (subfolders, .zip / .7z / .rar included).
  Owned games get a green tick, and new ROMs are picked up on start-up.
- Each ROM is identified by checksum against the No-Intro databases, so the side panel
  shows its real region and version (e.g. "Rev 1 (v1.1)").
- A gold trophy above FIND PATCHES means the game has RetroAchievements; click it for
  the supported ROM versions.

## Settings

- **General** - patched ROM options (name after the patch, MSU-1 folders, zip patched
  ROMs, use my scanned ROM automatically, finished message), update check, game list.
- **Appearance** - dark / light theme and font (Rubik, Inter, Exo 2, Nunito, Segoe UI;
  bundled under the SIL Open Font License).
- **Accounts** - GitHub token, RetroAchievements Web API key, archive.org sign-in.
  These stay on your PC; your archive.org password is never saved.
- **Folders** - ROM folders, patch download folder, browser Downloads folder, and
  whether to keep downloaded patches.
- **History** - every ROM you've patched (double-click to show the file).
- **About** - version and credits.

## Notes

- **Weekly update check** (on by default): refreshes the source lists, and LaunchBox
  only when it has published a new version.
- **Keyboard and copying:** Ctrl+C on a result or patch file copies its name and link;
  the title and description can be selected and copied.
- **Performance:** the cover grid only draws what's on screen, so even the 4,000-game
  NES list scrolls and filters instantly. Covers are cached small, in `cache/covers`.

## Credits

Created by [immaculatevibes2025](https://github.com/immaculatevibes2025), with Claude
contributing. Game data and covers: LaunchBox Games Database. ROM identification:
No-Intro (via libretro-database). Thanks to the ROM hacking community and every
source listed above.
