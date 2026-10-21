"""Vibe Patcher - browse SNES and GBA games by cover, find ROM hacks / translations,
download them and apply them to your own ROM."""
import collections
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import tkinter as tk
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk

import archive_org
import compat
import covers
import updates
import library
import msu_audio
import rom_id
import sources
from patcher import PatchError, apply_patch_file
from paths import APP_DIR, DOWNLOAD_DIR, LOG_FILE, SETTINGS


def _setup_log():
    """Errors go to %USERPROFILE%\\.vibe_patcher\\vibe_patcher.log (kept to ~3 MB)."""
    import logging.handlers
    os.makedirs(APP_DIR, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=1_000_000,
                                                   backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger = logging.getLogger("vibe_patcher")
    if not logger.handlers:
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
    return logger


log = _setup_log()

ASSETS =os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
APP_VERSION = "1.0"
TILE_W, TILE_H = 132, 180
CATEGORIES = [("hack", "Rom Hacks"), ("translation", "Translations"), ("msu1", "MSU-1"),
              ("pokemon", "Pokémon"), ("retro", "RetroAchievements"),
              ("archive", "2024 Archive")]
# which result list each tab shows in the side panel
RESULT_CATEGORY = {"hack": "hack", "translation": "translation", "msu1": "msu1",
                   "pokemon": "hack", "retro": "hack", "archive": "archive"}
SORTS = [("popular", "Popularity"), ("rating", "Rating"), ("newest", "Newest"),
         ("oldest", "Oldest")]
CONSOLES = {
    "snes": {"label": "SNES", "name": "Super Nintendo", "patterns": "*.sfc *.smc *.fig *.swc",
             "exts": (".sfc", ".smc"), "out": ".sfc", "tabs": ("hack", "translation", "msu1", "retro", "archive")},
    "gba": {"label": "GBA", "name": "Game Boy Advance", "patterns": "*.gba",
            "exts": (".gba",), "out": ".gba", "tabs": ("hack", "translation", "pokemon", "retro", "archive")},
    "nes": {"label": "NES", "name": "NES", "patterns": "*.nes",
            "exts": (".nes",), "out": ".nes", "tabs": ("hack", "translation", "retro", "archive")},
    "gb": {"label": "Game Boy", "name": "Game Boy", "patterns": "*.gb",
           "exts": (".gb",), "out": ".gb", "tabs": ("hack", "translation", "pokemon", "retro", "archive")},
    "gbc": {"label": "GBC", "name": "Game Boy Color", "patterns": "*.gbc *.gb",
            "exts": (".gbc", ".gb"), "out": ".gbc",
            "tabs": ("hack", "translation", "pokemon", "retro", "archive")},
    "nds": {"label": "DS", "name": "Nintendo DS", "patterns": "*.nds",
            "exts": (".nds",), "out": ".nds", "tabs": ("hack", "translation", "pokemon", "retro", "archive")},
    "sms": {"label": "Master System", "name": "Master System", "patterns": "*.sms",
            "exts": (".sms",), "out": ".sms", "tabs": ("hack", "translation", "retro", "archive")},
    "genesis": {"label": "Genesis", "name": "Genesis / Mega Drive / 32X",
                "patterns": "*.md *.gen *.bin *.smd *.32x",
                "exts": (".md", ".gen", ".bin", ".smd", ".32x"), "out": ".md",
                "tabs": ("hack", "translation", "retro", "archive")},
    "n64": {"label": "N64", "name": "Nintendo 64", "patterns": "*.z64 *.n64 *.v64",
            "exts": (".z64", ".n64", ".v64"), "out": ".z64",
            "tabs": ("hack", "translation", "retro", "archive")},
    "gg": {"label": "Game Gear", "name": "Game Gear", "patterns": "*.gg",
           "exts": (".gg",), "out": ".gg", "tabs": ("hack", "translation", "retro", "archive")},
}
# console tabs left to right until the user drags them into another order
DEFAULT_CONSOLE_ORDER = ["nes", "snes", "n64", "gb", "gbc", "gba", "nds", "sms", "genesis",
                         "gg"]

# name shown in Settings -> (regular family, semibold family) as Windows registers them
FONTS = {
    "Rubik": ("Rubik Light", "Rubik Light SemiBold"),
    "Inter": ("Inter", "Inter SemiBold"),
    "Exo 2": ("Exo 2", "Exo 2 SemiBold"),
    "Nunito": ("Nunito ExtraLight", "Nunito ExtraLight SemiBold"),
    "Segoe UI": ("Segoe UI", "Segoe UI Semibold"),
}


def load_bundled_fonts():
    """Register the fonts in assets/fonts for this process only (no install needed)."""
    folder = os.path.join(ASSETS, "fonts")
    try:
        import ctypes
        for f in os.listdir(folder):
            if f.lower().endswith(".ttf"):
                ctypes.windll.gdi32.AddFontResourceExW(os.path.join(folder, f), 0x10, 0)
    except Exception:
        pass


THEMES = {
    "dark": {"bg": "#16181d", "panel": "#1f2229", "card": "#272b33", "fg": "#e6e8ec",
             "muted": "#9097a3", "accent": "#7c8cff", "sel": "#3b4470", "border": "#343944",
             "pick": "#2f6bff"},
    "light": {"bg": "#f3f4f7", "panel": "#ffffff", "card": "#ffffff", "fg": "#1c1f26",
              "muted": "#5f6672", "accent": "#4255e0", "sel": "#d9defc", "border": "#d6d9e0",
              "pick": "#2f6bff"},
}


def load_settings():
    try:
        with open(SETTINGS) as f:
            return json.load(f)
    except Exception:
        return {}


def save_settings(s):
    os.makedirs(APP_DIR, exist_ok=True)
    with open(SETTINGS, "w") as f:
        json.dump(s, f)


GENERIC = {"patch", "hack", "rom", "ips", "bps", "ups", "main", "release", "update", "fix"}


def hack_name(patch_path, result, game_name, patch_only=True):
    """Pick a file name for the patched ROM from the patch / result name. With
    patch_only (the default setting) it's just the patch's name - "CT+Final2.1",
    not "Chrono Trigger - CT+Final2.1"."""
    stem = os.path.splitext(os.path.basename(patch_path))[0]
    stem = re.sub(r"[_]+", " ", stem).strip()
    if len(stem) < 4 or stem.lower() in GENERIC or not re.search(r"[A-Za-z]", stem):
        title = (result or {}).get("title", "") or game_name
        stem = re.sub(r"\s*\(MSU-1\)$", "", title.split("/")[-1])
        stem = re.sub(r"[-_]+", " ", stem).strip()
    if not patch_only and game_name.lower().split(":")[0] not in stem.lower() \
            and len(stem) < 40:
        stem = f"{game_name} - {stem}"
    return re.sub(r'[<>:"/\\|?*]', "", stem).strip() or "Patched ROM"


def same_file(a, b):
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def unique_path(path, avoid):
    """Add (2), (3)... so we never overwrite the original or an existing file."""
    base, ext = os.path.splitext(path)
    n = 1
    while os.path.exists(path) or same_file(path, avoid):
        n += 1
        path = f"{base} ({n}){ext}"
    return path


def _default_browser_exe():
    """(path to the default browser's .exe, its ProgId) from the Windows registry."""
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\Shell"
                        r"\Associations\UrlAssociations\https\UserChoice") as k:
        prog = winreg.QueryValueEx(k, "ProgId")[0]
    with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, prog + r"\shell\open\command") as k:
        cmd = winreg.QueryValueEx(k, "")[0]
    m = re.match(r'\s*"([^"]+)"', cmd) or re.match(r"\s*(\S+)", cmd)
    return m.group(1), prog


def search_in_browser(query, fallback_url):
    """Search with the default browser's own search engine (whatever the user set in
    it): Chrome / Edge / Brave / Opera / Vivaldi search for '? words', Firefox has
    -search. Anything else gets the plain Google link."""
    try:
        exe, prog = _default_browser_exe()
        name = os.path.basename(exe).lower()
        if "firefox" in name or prog.lower().startswith("firefox"):
            subprocess.Popen([exe, "-search", query])
            return
        if name in ("chrome.exe", "msedge.exe", "brave.exe", "opera.exe", "launcher.exe",
                    "vivaldi.exe", "chromium.exe"):
            subprocess.Popen([exe, "? " + query])
            return
    except (OSError, ImportError, AttributeError):
        pass
    open_in_browser(fallback_url, search=False)


def open_in_browser(url, search=True):
    """Open a link in the user's default web browser. Google search links become a
    search with the browser's own search engine instead."""
    from urllib.parse import parse_qs, urlparse
    u = urlparse(url)
    if search and u.netloc.endswith("google.com") and u.path == "/search":
        q = parse_qs(u.query).get("q")
        if q:
            search_in_browser(q[0], url)
            return
    try:
        os.startfile(url)  # Windows: whatever browser is set as default
    except (AttributeError, OSError):
        webbrowser.open(url)


def parse_rhn_date(text):
    """'11 Mar 1998' -> '1998-03-11' (romhacking.net's date format)."""
    import datetime
    try:
        return datetime.datetime.strptime(text.strip(), "%d %b %Y").strftime("%Y-%m-%d")
    except ValueError:
        return ""


OTHER_LANGUAGES = re.compile(
    r"\b(spanish|espa[nñ]ol|french|fran[cç]ais|german|deutsch|italian|italiano|portuguese|"
    r"portugu[eê]s|brazil|russian|polish|dutch|swedish|finnish|norwegian|danish|greek|"
    r"turkish|arabic|hebrew|chinese|korean|japanese|catalan|galician|basque|romanian|"
    r"hungarian|czech|ukrainian|vietnamese|indonesian|\(es\)|\(fr\)|\(de\)|\(it\)|"
    r"\(pt\)|\(ru\)|\(pt-br\))", re.I)


def translation_rank(result):
    """0 = English, 1 = language not stated, 2 = another language."""
    text = f"{result.get('title', '')} {result.get('desc', '')}"
    if re.search(r"\benglish\b|\beng\b|\(en\)|\bt-en\b", text, re.I):
        return 0
    if result.get("source") == "Romhacking.net":
        return 0  # the bundled romhacking.net index only has English translations
    if re.search(r"[Ѐ-ӿͰ-Ͽ֐-ۿ぀-ヿ一-鿿가-힯]",
                 text):
        return 2  # Cyrillic, Greek, Hebrew/Arabic, Japanese, Chinese or Korean text
    return 2 if OTHER_LANGUAGES.search(text) else 1


class SelectableText(tk.Text):
    """Read-only text you can highlight and copy (Ctrl+C), standing in for a Label.
    configure(text=...) works like a Label's."""

    def __init__(self, parent, height=2, center=False, **kw):
        super().__init__(parent, height=height, wrap="word", bd=0, highlightthickness=0,
                         cursor="xterm", padx=0, pady=0, **kw)
        self.tag_configure("all", justify="center" if center else "left")
        self.configure(state="disabled")
        self.bind("<Control-c>", lambda e: self.event_generate("<<Copy>>"))

    def configure(self, cnf=None, **kw):
        if "text" in kw:
            text = kw.pop("text")
            tk.Text.configure(self, state="normal")
            self.delete("1.0", "end")
            self.insert("1.0", text, "all")
            tk.Text.configure(self, state="disabled")
        kw.pop("wraplength", None)
        if kw or cnf:
            return tk.Text.configure(self, cnf, **kw)

    config = configure


REGIONS = ["USA", "Europe", "Japan", "World", "Australia", "Korea", "China", "Asia", "Brazil",
           "Canada", "France", "Germany", "Spain", "Italy", "Netherlands", "Sweden", "Taiwan",
           "Hong Kong", "Scandinavia", "UK"]
REGION_CODES = {"U": "USA", "E": "Europe", "J": "Japan", "W": "World", "UE": "USA, Europe",
                "JU": "Japan, USA", "EU": "Europe"}


def rom_region_version(path):
    """Region and version from a ROM's file name, e.g.
    'Super Metroid (Japan, USA) (Rev 1).sfc' -> ('Japan, USA', 'Rev 1');
    'Chrono Trigger (U) [!].smc' -> ('USA', '')."""
    name = os.path.splitext(os.path.basename(path))[0]
    region, version = "", ""
    for tag in re.findall(r"[\(\[]([^\)\]]+)[\)\]]", name):
        t = tag.strip()
        parts = [x.strip() for x in t.split(",")]
        if not region and any(x in REGIONS for x in parts):
            region = ", ".join(x for x in parts if x in REGIONS)
        elif not region and t in REGION_CODES:
            region = REGION_CODES[t]
        elif not version and re.match(r"^(rev\s*\w+|v\s*\d+(\.\d+)*\w*|version\s*\S+)$",
                                      t, re.I):
            version = t
    return region, version


def strip_brand(name):
    """"Disney's Aladdin" -> "Aladdin", so names match across databases."""
    return re.sub(r"^(disney|sega|capcom|konami|namco|hudson|nintendo|taito|"
                  r"jaleco|tecmo)'?s?\s+", "", name.strip(), flags=re.I)


def is_hack(game):
    """LaunchBox marks ROM hacks with ReleaseType 'ROM Hack'."""
    return game.get("type") == "ROM Hack"


class App(tk.Tk):
    def __init__(self):
        try:  # crisp text on high-DPI Windows displays
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
            # own taskbar icon instead of the generic Python one
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("VibePatcher.App")
        except Exception:
            pass
        load_bundled_fonts()
        super().__init__()
        # errors in button clicks etc. go to the log instead of vanishing
        self.report_callback_exception = lambda *exc: log.error(
            "Error in a click / key handler", exc_info=exc)
        global TILE_W, TILE_H
        self.k = k = max(1.0, self.winfo_fpixels("1i") / 96)  # DPI scale factor
        TILE_W, TILE_H = int(TILE_W * k), int(TILE_H * k)
        self.title("Vibe Patcher")
        try:
            self.iconbitmap(default=os.path.join(ASSETS, "icon.ico"))
        except tk.TclError:
            pass
        self.geometry(f"{int(1280 * k)}x{int(800 * k)}")
        self.minsize(int(900 * k), int(600 * k))
        self.settings = load_settings()
        self.theme = self.settings.get("theme", "dark")
        self.games, self.filtered = [], []
        self.images = {}
        self.selected = None
        self.platform = self.settings.get("platform", "snes")
        self._game_cache = {}
        self._tags, self._patch_cache = {}, {}
        self.results, self.files = [], []
        self.shown_results = []
        self.category = "hack"
        sources.GITHUB_TOKEN = self.settings.get("github_token", "")
        sources.RA_KEY = self.settings.get("ra_key", "")
        archive_org.CREDS = self.settings.get("archive_org", {})
        self._ra = {}  # platform -> {norm(name): RA entry} once loaded
        self._ra_originals = {}
        self._ra_hashes = {}
        self._rom_ids = rom_id.load_ids()
        self.ui_queue = queue.Queue()
        self.pool = ThreadPoolExecutor(8)
        self.style = ttk.Style(self)
        self.style.theme_use("clam")
        self._build()
        self.apply_theme()
        self.after(50, self._drain)
        self.after(100, self._load_games)
        self.after(1500, self._scan_on_start)
        self.after(900, self._welcome)
        self.after(8000, self._weekly_update_check)
        self.after(4000, lambda: self.run_bg(covers.migrate_old_covers, fail=lambda m: None))

    # ---------------- layout ----------------
    def _build(self):
        top = ttk.Frame(self, style="Panel.TFrame", padding=(14, 10))
        top.pack(fill="x")
        logo = Image.open(os.path.join(ASSETS, "logo.png"))
        h = int(30 * self.k)
        self.logo_img = ImageTk.PhotoImage(
            logo.resize((int(logo.width * h / logo.height), h), Image.LANCZOS))
        ttk.Label(top, image=self.logo_img, style="Title.TLabel").pack(side="left")
        self.theme_btn = ttk.Button(top, command=self.toggle_theme, width=12)
        self.theme_btn.pack(side="right")
        ttk.Button(top, text="⚙ Settings", command=self.open_settings).pack(side="right",
                                                                           padx=(0, 8))
        self.sort_var = tk.StringVar(value=self.settings.get("sort", "az"))
        self.sort_btn = ttk.Menubutton(top, text="⇅ Sort by", style="TMenubutton")
        self.sort_menu = tk.Menu(self.sort_btn, tearoff=False)
        self.sort_menu.add_command(command=self.sort_alpha)  # label set in _sort_label
        for key, label in SORTS:
            self.sort_menu.add_command(label=label, command=lambda k=key: self.set_sort(k))
        self.sort_btn["menu"] = self.sort_menu
        self.sort_btn.pack(side="right", padx=(0, 8))
        self.filter_btn = ttk.Menubutton(top, text="⏷ Filter", style="TMenubutton")
        self.filter_menu = tk.Menu(self.filter_btn, tearoff=False)
        self.owned_only = tk.BooleanVar(value=self.settings.get("owned_only", False))
        self.hacks_only = tk.BooleanVar(value=self.settings.get("hacks_only", False))
        self.filter_menu.add_checkbutton(label="Only Show Rom Hacks", variable=self.hacks_only,
                                         command=self.set_hacks_only)
        self.show_hidden = tk.BooleanVar(value=self.settings.get("show_hidden", False))
        self.filter_menu.add_checkbutton(label="Only Show Games I Own (And Their Hacks)",
                                         variable=self.owned_only,
                                         command=self.set_owned_only)
        self.direct_only = tk.BooleanVar(value=self.settings.get("direct_only", False))
        self.filter_menu.add_checkbutton(label="Only Show Games With Direct Downloads",
                                         variable=self.direct_only,
                                         command=self.set_direct_only)
        self.filter_menu.add_checkbutton(label="Show Hidden Games", variable=self.show_hidden,
                                         command=self.set_show_hidden)
        self._sort_label()
        self.filter_btn["menu"] = self.filter_menu
        self.filter_btn.pack(side="right", padx=(0, 8))
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self._schedule_filter())
        ent = ttk.Entry(top, textvariable=self.search_var, width=34, font=("Segoe UI Semibold", 12))
        ent.pack(side="right", padx=10)
        ttk.Label(top, text="Search Games", style="Search.TLabel").pack(side="right")

        consoles = ttk.Frame(self, style="Panel.TFrame", padding=(14, 0, 14, 6))
        consoles.pack(fill="x")
        self.platform_btns, self.platform_icons = {}, {}
        isz = int(22 * self.k)
        self.consoles_row = consoles
        order = [p for p in self.settings.get("console_order", []) if p in CONSOLES]
        order += [p for p in DEFAULT_CONSOLE_ORDER if p not in order]
        for p in order:
            info = CONSOLES[p]
            icon = Image.open(os.path.join(ASSETS, f"icon_{p}.png")).resize((isz, isz),
                                                                             Image.LANCZOS)
            self.platform_icons[p] = ImageTk.PhotoImage(icon)
            b = tk.Label(consoles, text=" " + info["label"], image=self.platform_icons[p],
                         compound="left", padx=int(14 * self.k), pady=int(5 * self.k),
                         cursor="hand2")
            b.pack(side="left", padx=(0, 6))
            b.bind("<ButtonPress-1>", lambda e, k=p: self._tab_press(e, k))
            b.bind("<B1-Motion>", self._tab_drag)
            b.bind("<ButtonRelease-1>", self._tab_release)
            self.platform_btns[p] = b

        tabs = ttk.Frame(self, style="Panel.TFrame", padding=(14, 0))
        tabs.pack(fill="x")
        # Scan Games: on the tab row, at the right, above the details panel
        folder = Image.open(os.path.join(ASSETS, "icon_folder.png")).resize(
            (int(22 * self.k), int(22 * self.k)), Image.LANCZOS)
        self.folder_icon = ImageTk.PhotoImage(folder)
        ttk.Button(tabs, text="  Scan Games", image=self.folder_icon, compound="left",
                   style="Big.TButton", width=16, command=self.open_scan_window).pack(
            side="right", pady=(0, int(4 * self.k)))
        # Updates (n): newer versions of hacks you've patched, found by the weekly check
        self.updates_btn = ttk.Button(tabs, text="", style="Big.TButton",
                                      command=self.open_updates_window)
        self.updates_btn.pack(side="right", padx=(0, 8), pady=(0, int(4 * self.k)))
        self._update_updates_btn()
        self.tab_btns = {}
        for key, label in CATEGORIES:
            b = tk.Label(tabs, text=label, padx=14, pady=8, cursor="hand2",
                         font=("Segoe UI", 10))
            b.bind("<Button-1>", lambda e, k=key: self.set_category(k))
            self.tab_btns[key] = b
        self._layout_category_tabs()
        self.tab_line = tk.Frame(self, height=1)
        self.tab_line.pack(fill="x")

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)

        # right: details panel
        self.side = ttk.Frame(body, style="Panel.TFrame", padding=14, width=int(420 * self.k))
        self.side.pack(side="right", fill="y")
        self.side.pack_propagate(False)
        self.side.bind("<Configure>", self._schedule_fit_cover)  # cover follows window size

        # thin strip with an arrow in the middle that hides / shows the panel
        self.side_strip = tk.Frame(body, width=int(22 * self.k), cursor="hand2")
        self.side_strip.pack(side="right", fill="y")
        self.side_arrow = tk.Label(self.side_strip, text="▶", cursor="hand2",
                                   font=("Segoe UI", 11, "bold"), padx=2, pady=int(22 * self.k))
        self.side_arrow.place(relx=0.5, rely=0.5, anchor="center")
        for w in (self.side_strip, self.side_arrow):
            w.bind("<Button-1>", lambda e: self.toggle_side())
        if self.settings.get("side_hidden"):
            self.side.pack_forget()
            self.side_arrow.configure(text="◀")
        self.big_cover = ttk.Label(self.side, style="Panel.TLabel")
        self.big_cover.pack()
        # "Works with your ROM" / "Needs (USA) v1.1" - placed to the right of the cover
        self.compat_lbl = tk.Label(self.side, text="", justify="left", anchor="nw", bd=0,
                                   padx=int(6 * self.k), pady=int(4 * self.k))
        self.compat_lbl.bind("<Enter>", lambda e: getattr(self, "_compat_long", "") and
                             self.status.configure(text=self._compat_long))
        self.game_title = SelectableText(self.side, height=2, center=True)
        self.game_title.configure(text="Pick a game")
        self.game_title.pack(pady=(8, 0))
        self.game_info = ttk.Label(self.side, style="PanelMuted.TLabel", anchor="center")
        self.game_info.pack(fill="x", pady=(0, 6))
        # small display filter, top-left next to the cover
        self.show_date = tk.BooleanVar(value=self.settings.get("show_date", True))
        self.show_region = tk.BooleanVar(value=self.settings.get("show_region", True))
        self.show_version = tk.BooleanVar(value=self.settings.get("show_version", True))
        # a plain square with the arrow centred (ttk's menubutton keeps room for its own
        # arrow, which pushed the symbol off-centre)
        from PIL import ImageDraw
        box = int(26 * self.k)
        arrow = Image.new("RGBA", (box * 4, box * 4), (0, 0, 0, 0))
        w = box * 4
        ImageDraw.Draw(arrow).polygon([(w * .30, w * .40), (w * .70, w * .40), (w * .50, w * .62)],
                                      fill="white")  # drawn, so it's exactly centred
        self.info_arrow = ImageTk.PhotoImage(arrow.resize((box, box), Image.LANCZOS))
        self.info_btn = tk.Label(self.side, image=self.info_arrow, cursor="hand2", bd=0)
        self.info_menu = tk.Menu(self.info_btn, tearoff=False)
        self.info_btn.bind("<Button-1>", lambda e: self.info_menu.tk_popup(
            self.info_btn.winfo_rootx(),
            self.info_btn.winfo_rooty() + self.info_btn.winfo_height()))
        for label, var, key in (("Show Game Date", self.show_date, "show_date"),
                                ("Show Game Region", self.show_region, "show_region"),
                                ("Show Game Version", self.show_version, "show_version")):
            self.info_menu.add_checkbutton(label=label, variable=var,
                                           command=lambda k=key, v=var: self._set_info(k, v))
        self.info_btn.place(x=0, y=0, anchor="nw")
        self.big_cover.bind("<Configure>", lambda e: self._place_info_btn())
        self.ra_row = ttk.Frame(self.side, style="Panel.TFrame")
        self.ra_row.pack(fill="x")
        trophy = Image.open(os.path.join(ASSETS, "icon_ra.png")).resize(
            (int(26 * self.k), int(26 * self.k)), Image.LANCZOS)
        self.ra_icon = ImageTk.PhotoImage(trophy)
        self.ra_btn = tk.Label(self.ra_row, image=self.ra_icon, cursor="hand2", bd=0)
        self.ra_btn.bind("<Button-1>", lambda e: self._open_ra_page())
        self.ra_btn.bind("<Button-3>", self._ra_menu)
        self.ra_btn.bind("<Enter>", lambda e: self._show_tip(self.ra_btn, "RetroAchievements"))
        self.ra_btn.bind("<Leave>", lambda e: self._hide_tip())
        self.find_btn = ttk.Button(self.side, text="FIND PATCHES", style="Red.TButton",
                                   command=self.find_patches, state="disabled")
        self.find_btn.pack(fill="x")

        ttk.Label(self.side, text="Results", style="PanelMuted.TLabel").pack(anchor="w", pady=(10, 2))
        res_box = ttk.Frame(self.side, style="Panel.TFrame")
        res_box.pack(fill="both", expand=True)
        self.res_tree = ttk.Treeview(res_box, columns=("src",), show="tree headings", height=3)
        self.res_tree.heading("#0", text="Title")
        self.res_tree.heading("src", text="Source")
        self.res_tree.column("#0", width=int(290 * self.k))
        self.res_tree.column("src", width=int(90 * self.k), anchor="center")
        res_scroll = ttk.Scrollbar(res_box, orient="vertical", command=self.res_tree.yview)
        self.res_tree.configure(yscrollcommand=res_scroll.set)
        res_scroll.pack(side="right", fill="y")
        self.res_tree.pack(side="left", fill="both", expand=True)
        self.res_tree.bind("<<TreeviewSelect>>", lambda e: self.result_selected())
        self.res_tree.bind("<Double-1>", lambda e: self.open_result_page())
        self.res_tree.bind("<Motion>", lambda e: self._row_tip(
            self.res_tree, e, lambda i: self.shown_results[i]["title"]))
        self.res_tree.bind("<Leave>", lambda e: (self._hide_row_tip(),
                                                    setattr(self, "_row_tip_at", None)))
        self.res_tree.bind("<Control-c>", lambda e: self._copy_row(self._current_result()))
        ttk.Label(self.side, text="Description", style="PanelMuted.TLabel").pack(
            anchor="w", pady=(8, 2))
        desc_box = ttk.Frame(self.side, style="Panel.TFrame")
        desc_box.pack(fill="both", expand=True)
        self.res_desc = SelectableText(desc_box, height=3)
        desc_scroll = ttk.Scrollbar(desc_box, orient="vertical", command=self.res_desc.yview)
        self.res_desc.configure(yscrollcommand=desc_scroll.set, padx=6, pady=4)
        desc_scroll.pack(side="right", fill="y")
        self.res_desc.pack(side="left", fill="both", expand=True)

        ttk.Label(self.side, text="Patch files", style="PanelMuted.TLabel").pack(anchor="w", pady=(6, 2))
        file_box = ttk.Frame(self.side, style="Panel.TFrame")
        file_box.pack(fill="x")
        self.file_tree = ttk.Treeview(file_box, columns=("size",), show="tree", height=5)
        self.file_tree.heading("#0", text="File")
        self.file_tree.heading("size", text="Size")
        self.file_tree.column("#0", width=int(300 * self.k))
        self.file_tree.column("size", width=int(80 * self.k), anchor="e")
        file_scroll = ttk.Scrollbar(file_box, orient="vertical", command=self.file_tree.yview)
        self.file_tree.configure(yscrollcommand=file_scroll.set)
        file_scroll.pack(side="right", fill="y")
        self.file_tree.pack(side="left", fill="x", expand=True)
        self.file_tree.bind("<<TreeviewSelect>>", lambda e: self.file_selected())
        # double-click = the button below: open a website link, or download & patch
        self.file_tree.bind("<Double-1>", lambda e: self.file_tree.identify_row(e.y) and
                            self._selected_file() and self.download_apply())
        self.file_tree.bind("<Motion>", lambda e: self._row_tip(
            self.file_tree, e, self._file_full_text))
        self.file_tree.bind("<Leave>", lambda e: (self._hide_row_tip(),
                                                    setattr(self, "_row_tip_at", None)))
        self.file_tree.bind("<Control-c>", lambda e: self._copy_row(self._selected_file()))
        self.apply_btn = ttk.Button(self.side, text="Download & apply to my ROM…",
                                    style="Accent.TButton", command=self.download_apply,
                                    state="disabled")
        self.apply_btn.pack(side="bottom", fill="x", pady=(8, 0), before=self.big_cover)
        self.local_btn = ttk.Button(self.side, text="Apply a patch I downloaded…",
                                    command=self.apply_local, state="disabled")
        self.local_btn.pack(side="bottom", fill="x", pady=(6, 0), before=self.apply_btn)

        # left: cover grid
        grid_wrap = ttk.Frame(body)
        grid_wrap.pack(side="left", fill="both", expand=True)
        self.canvas = tk.Canvas(grid_wrap, highlightthickness=0, bd=0, yscrollincrement=1)
        sb = ttk.Scrollbar(grid_wrap, orient="vertical", command=self._scrollbar)
        self.canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.bind("<Configure>", self._on_resize)
        self.canvas.bind("<Button-1>", self._grid_click)
        self.canvas.bind("<Double-Button-1>", self._grid_double)
        self.canvas.bind("<Button-3>", self._grid_menu)
        self._drawn, self._grid_h, self._scroll_target = {}, 0, 0
        self.thumbs = collections.OrderedDict()  # cover thumbnails, most recent last
        self._thumb_pending = set()
        self._wanted = set()
        self.bind_all("<MouseWheel>", self._on_wheel)
        self.bind_all("<Key>", self._on_letter, add="+")
        self.cols = 5

        status_row = ttk.Frame(self, style="Panel.TFrame")
        status_row.pack(fill="x", side="bottom")
        self.dl_bar = ttk.Progressbar(status_row, style="Download.Horizontal.TProgressbar",
                                      length=int(260 * self.k), maximum=100, mode="determinate")
        self.status = ttk.Label(status_row, text="", style="Status.TLabel", padding=(14, 4))
        self.status.pack(side="right", fill="x", expand=True)

    # ---------------- theme ----------------
    def apply_theme(self):
        c = THEMES[self.theme]
        s = self.style
        reg, semi = FONTS.get(self.settings.get("font", "Rubik"), FONTS["Rubik"])
        self.font_reg, self.font_semi = reg, semi
        self.option_add("*TCombobox*Listbox.font", (reg, 10))
        self.option_add("*TCombobox*Listbox.background", c["card"])
        self.option_add("*TCombobox*Listbox.foreground", c["fg"])
        self.option_add("*TCombobox*Listbox.selectBackground", c["sel"])
        self.option_add("*TCombobox*Listbox.selectForeground", c["fg"])
        s.configure(".", background=c["bg"], foreground=c["fg"], fieldbackground=c["card"],
                    bordercolor=c["border"], lightcolor=c["border"], darkcolor=c["border"],
                    troughcolor=c["bg"], font=(reg, 10))
        s.configure("TFrame", background=c["bg"])
        s.configure("Panel.TFrame", background=c["panel"])
        s.configure("Card.TFrame", background=c["card"])
        s.configure("TLabel", background=c["bg"], foreground=c["fg"])
        s.configure("Panel.TLabel", background=c["panel"])
        s.configure("Title.TLabel", background=c["panel"], font=(semi, 14))
        s.configure("Head.TLabel", background=c["panel"], font=(semi, 12))
        s.configure("Muted.TLabel", background=c["panel"], foreground=c["muted"])
        s.configure("Search.TLabel", background=c["panel"], foreground=c["fg"], font=(semi, 12))
        s.configure("PanelMuted.TLabel", background=c["panel"], foreground=c["muted"])
        s.configure("Card.TLabel", background=c["card"], foreground=c["fg"], font=(reg, 9))
        s.configure("CardSel.TLabel", background=c["pick"], foreground="#ffffff",
                    font=(semi, 9))
        s.configure("CardSel.TFrame", background=c["pick"])
        s.configure("Status.TLabel", background=c["panel"], foreground=c["muted"])
        s.configure("TButton", background=c["card"], foreground=c["fg"], padding=6)
        s.map("TButton", background=[("active", c["sel"]), ("disabled", c["panel"])],
              foreground=[("disabled", c["muted"])])
        s.configure("Small.TMenubutton", background=c["card"], foreground=c["fg"],
                    padding=(4, 1), arrowsize=0)
        s.map("Small.TMenubutton", background=[("active", c["sel"])])
        self.info_btn.configure(bg=c["card"], fg=c["fg"], highlightthickness=1,
                                highlightbackground=c["border"])
        self.info_menu.configure(bg=c["card"], fg=c["fg"], activebackground=c["sel"],
                                 activeforeground=c["fg"], selectcolor=c["accent"], bd=0,
                                 font=(reg, 10))
        s.configure("TMenubutton", background=c["card"], foreground=c["fg"], padding=6,
                    arrowcolor=c["card"])
        s.map("TMenubutton", background=[("active", c["sel"])])
        self.sort_menu.configure(bg=c["card"], fg=c["fg"], activebackground=c["sel"],
                                 activeforeground=c["fg"], bd=0, font=(reg, 10))
        self.filter_menu.configure(bg=c["card"], fg=c["fg"], activebackground=c["sel"],
                                   activeforeground=c["fg"], selectcolor=c["accent"],
                                   disabledforeground=c["muted"], bd=0, font=(reg, 10))
        s.configure("Big.TButton", background=c["card"], foreground=c["fg"],
                    font=(semi, 12), padding=(10, int(4 * self.k)))
        s.map("Big.TButton", background=[("active", c["sel"])])
        s.configure("Red.TButton", background="#ed1224", foreground="#ffffff",
                    font=(semi, 11), padding=8)
        s.map("Red.TButton", background=[("active", "#c50e1d"), ("disabled", c["border"])],
              foreground=[("disabled", c["muted"])])
        s.configure("Accent.TButton", background=c["accent"], foreground="#ffffff")
        s.map("Accent.TButton", background=[("active", c["accent"]), ("disabled", c["border"])],
              foreground=[("disabled", c["muted"])])
        s.configure("Panel.TRadiobutton", background=c["panel"], foreground=c["fg"],
                    indicatorbackground=c["card"])
        s.map("Panel.TRadiobutton", background=[("active", c["panel"])],
              indicatorbackground=[("selected", c["accent"])])
        s.configure("Settings.TNotebook", background=c["panel"], borderwidth=0,
                    tabmargins=(0, 0, 0, 0))
        s.configure("Settings.TNotebook.Tab", background=c["card"], foreground=c["muted"],
                    padding=(12, 6), font=(semi, 10), borderwidth=0)
        s.map("Settings.TNotebook.Tab", background=[("selected", c["pick"])],
              foreground=[("selected", "#ffffff")])
        s.configure("Panel.TCheckbutton", background=c["panel"], foreground=c["fg"],
                    indicatorbackground=c["card"])
        s.map("Panel.TCheckbutton", background=[("active", c["panel"])],
              indicatorbackground=[("selected", c["accent"])])
        self._big_toggles(c)
        s.configure("TEntry", foreground=c["fg"], insertcolor=c["fg"], padding=4)
        s.configure("Treeview", background=c["card"], fieldbackground=c["card"],
                    foreground=c["fg"], rowheight=int(26 * self.k))
        s.map("Treeview", background=[("selected", c["sel"])],
              foreground=[("selected", c["fg"])])
        s.configure("Treeview.Heading", background=c["panel"], foreground=c["muted"],
                    font=(semi, 9))
        s.configure("Treeview", font=(reg, 10))
        s.configure("Big.TCombobox", padding=6)
        s.configure("Download.Horizontal.TProgressbar", background="#2f6bff",
                    troughcolor=c["card"], bordercolor=c["border"], lightcolor="#2f6bff",
                    darkcolor="#2f6bff", thickness=int(16 * self.k))
        s.configure("Done.Horizontal.TProgressbar", background="#2fbf5a",  # patch finished
                    troughcolor=c["card"], bordercolor=c["border"], lightcolor="#2fbf5a",
                    darkcolor="#2fbf5a", thickness=int(16 * self.k))
        s.configure("TCombobox", fieldbackground=c["card"], background=c["card"],
                    foreground=c["fg"], arrowcolor=c["fg"], selectbackground=c["card"],
                    selectforeground=c["fg"])
        s.map("TCombobox", fieldbackground=[("readonly", c["card"])],
              background=[("readonly", c["card"]), ("active", c["sel"])],
              arrowcolor=[("readonly", c["fg"])],
              foreground=[("readonly", c["fg"])])
        # bigger scrollbar arrows, easier to click
        s.configure("Vertical.TScrollbar", background=c["card"], arrowcolor=c["fg"],
                    arrowsize=int(13 * self.k))
        s.configure("Horizontal.TScrollbar", background=c["card"], arrowcolor=c["fg"],
                    arrowsize=int(13 * self.k))
        s.configure("Link.TLabel", background=c["panel"], foreground=c["accent"],
                    font=(semi, 10, "underline"))
        self.canvas.configure(bg=c["bg"])
        self.tab_line.configure(bg=c["border"])
        self.game_title.configure(bg=c["panel"], fg=c["fg"], font=(semi, 12),
                                  selectbackground=c["sel"], selectforeground=c["fg"],
                                  insertbackground=c["panel"])
        self.res_desc.configure(bg=c["card"], fg=c["fg"], font=(reg, 9),
                                selectbackground=c["sel"], selectforeground=c["fg"],
                                insertbackground=c["panel"])
        self._style_platform_tabs()
        self.side_strip.configure(bg=c["bg"])
        self.side_arrow.configure(bg=c["accent"], fg="#ffffff")
        self._style_tabs()
        self.theme_btn.configure(text="☀ Light mode" if self.theme == "dark" else "☾ Dark mode")
        self._restyle_tiles()

    def _big_toggles(self, c):
        """Large round toggles for checkboxes and radio buttons: an outlined circle when
        off, a filled green circle with a white tick (or dot) when on."""
        from PIL import ImageDraw
        size = int(22 * self.k)
        big = size * 4  # draw large, scale down for smooth edges
        green = "#1fa64a"

        def circle(on, mark):
            im = Image.new("RGBA", (big, big), (0, 0, 0, 0))
            d = ImageDraw.Draw(im)
            w = big // 10
            if on:
                d.ellipse((w // 2, w // 2, big - w // 2, big - w // 2), fill=green)
                if mark == "tick":
                    d.line([(big * .28, big * .52), (big * .44, big * .68), (big * .73, big * .34)],
                           fill="white", width=int(big * .11), joint="curve")
                else:
                    r = big * .2
                    d.ellipse((big / 2 - r, big / 2 - r, big / 2 + r, big / 2 + r), fill="white")
            else:
                d.ellipse((w // 2, w // 2, big - w // 2, big - w // 2), fill=c["card"],
                          outline=c["muted"], width=w)
            gap = int(8 * self.k)  # space between the circle and its label
            canvas = Image.new("RGBA", (size + gap, size), (0, 0, 0, 0))
            canvas.alpha_composite(im.resize((size, size), Image.LANCZOS))
            return ImageTk.PhotoImage(canvas)

        key = self.theme
        if not hasattr(self, "_toggle_imgs"):
            self._toggle_imgs = {}
        if key not in self._toggle_imgs:
            imgs = {m: (circle(False, m), circle(True, m)) for m in ("tick", "dot")}
            self._toggle_imgs[key] = imgs
            for m, (off, on) in imgs.items():
                self.style.element_create(f"{key}.{m}.indicator", "image", off,
                                          ("selected", on), border=0, sticky="")
        for widget, m in (("Panel.TCheckbutton", "tick"), ("Panel.TRadiobutton", "dot")):
            self.style.layout(widget, [
                ("Checkbutton.padding", {"sticky": "nswe", "children": [
                    (f"{key}.{m}.indicator", {"side": "left", "sticky": ""}),
                    ("Checkbutton.focus", {"side": "left", "sticky": "w", "children": [
                        ("Checkbutton.label", {"sticky": "nswe"})]})]})])
            self.style.configure(widget, padding=(0, 3), indicatormargin=0)
        self.style.configure("Panel.TCheckbutton", font=(self.font_reg, 10))
        self.style.configure("Panel.TRadiobutton", font=(self.font_reg, 10))

    def toggle_theme(self):
        self.theme = "light" if self.theme == "dark" else "dark"
        self.settings["theme"] = self.theme
        save_settings(self.settings)
        self.apply_theme()

    # ---------------- helpers ----------------
    def set_status(self, msg, done=None, total=None):
        """Status text (safe from any thread). With done/total it also drives the blue
        download bar in the bottom-left."""
        now = time.time()
        if done is not None and total and done < total and \
                now - getattr(self, "_last_bar", 0) < 0.1:
            return  # plenty for a smooth bar without flooding the UI
        self._last_bar = now

        def show():
            self.status.configure(text=msg)
            if done is not None and total:
                if not self.dl_bar.winfo_ismapped():
                    self.dl_bar.pack(side="left", padx=(14, 0), pady=4, before=self.status)
                self.dl_bar.configure(value=min(100, done * 100 / total),
                                      style="Download.Horizontal.TProgressbar")
            elif self.dl_bar.winfo_ismapped():
                self.dl_bar.pack_forget()
        self.ui_queue.put(show)

    def _bar_done(self):
        """Fill the progress bar and turn it green when a patch finishes; it hides again
        after a few seconds (or when the next download starts)."""
        self.dl_bar.configure(value=100, style="Done.Horizontal.TProgressbar")
        if not self.dl_bar.winfo_ismapped():
            self.dl_bar.pack(side="left", padx=(14, 0), pady=4, before=self.status)
        if getattr(self, "_bar_job", None):
            self.after_cancel(self._bar_job)

        def hide():
            self._bar_job = None
            if str(self.dl_bar.cget("style")).startswith("Done"):
                self.dl_bar.pack_forget()
        self._bar_job = self.after(6000, hide)

    def run_bg(self, work, done=None, fail=None):
        def runner():
            try:
                res = work()
                if done:
                    self.ui_queue.put(lambda: done(res))
            except Exception as e:
                log.exception("Background task failed")
                err = str(e) or e.__class__.__name__
                self.ui_queue.put(lambda: (fail or self._error)(err))
        threading.Thread(target=runner, daemon=True).start()

    def _error(self, msg):
        self.status.configure(text="Error: " + msg)
        messagebox.showerror("Error", msg)

    def _drain(self):
        while True:
            try:
                job = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                job()
            except Exception:  # one bad callback must not stop covers / results for good
                log.exception("UI callback failed")
        self.after(50, self._drain)

    # ---------------- consoles ----------------
    def owned(self, platform=None):
        """{game_id: rom_path} of the user's ROMs for a console."""
        p = platform or self.platform
        return self.settings.get("owned" if p == "snes" else f"owned_{p}", {})

    def set_owned(self, platform, owned):
        self.settings["owned" if platform == "snes" else f"owned_{platform}"] = owned

    def _owned_summary(self):
        """'Matched:' then the consoles four to a line, so it doesn't run off the edge."""
        parts = [f"{CONSOLES[p]['label']}: {len(self.owned(p))}" for p in DEFAULT_CONSOLE_ORDER]
        lines = ["   ".join(parts[i:i + 4]) for i in range(0, len(parts), 4)]
        return "Matched games\n" + "\n".join(lines)

    def console(self):
        return CONSOLES[self.platform]

    def _tab_press(self, e, key):
        self._drag = {"key": key, "x": e.x_root, "moved": False}

    def _tab_drag(self, e):
        """Hold and drag a console tab sideways to move it."""
        d = getattr(self, "_drag", None)
        if not d:
            return
        if not d["moved"] and abs(e.x_root - d["x"]) < 8:
            return
        d["moved"] = True
        btn = self.platform_btns[d["key"]]
        btn.configure(cursor="fleur")
        order = [k for k, _ in sorted(self.platform_btns.items(),
                                      key=lambda kv: kv[1].winfo_x())]
        others = [k for k in order if k != d["key"]]
        x = e.x_root - self.consoles_row.winfo_rootx()
        # new slot = how many other tabs have their middle left of the pointer
        slot = sum(1 for k in others
                   if self.platform_btns[k].winfo_x() + self.platform_btns[k].winfo_width() / 2 < x)
        new = others[:slot] + [d["key"]] + others[slot:]
        if new != order:
            for k in new:
                self.platform_btns[k].pack_forget()
            for k in new:
                self.platform_btns[k].pack(side="left", padx=(0, 6))

    def _tab_release(self, e):
        d = getattr(self, "_drag", None)
        self._drag = None
        if not d:
            return
        self.platform_btns[d["key"]].configure(cursor="hand2")
        if d["moved"]:
            order = [k for k, _ in sorted(self.platform_btns.items(),
                                          key=lambda kv: kv[1].winfo_x())]
            self.settings["console_order"] = order
            save_settings(self.settings)
        else:
            self.set_platform(d["key"])  # a plain click switches console

    def set_platform(self, p):
        if p == self.platform:
            return
        self.stop_watching()
        self.platform = p
        self.settings["platform"] = p
        save_settings(self.settings)
        # forget everything tied to the old console's list
        self.selected = None
        self.results = []
        self.game_title.configure(text="Pick a game")
        self.big_cover.configure(image="")
        self.find_btn.configure(state="disabled")
        self.local_btn.configure(state="disabled")
        if self.category not in self.console()["tabs"]:
            self.category = "hack"
        for attr in ("_rhn_game", "_bases"):
            if hasattr(self, attr):
                delattr(self, attr)
        self._layout_category_tabs()
        self.show_results()
        self._style_platform_tabs()
        self.games = []
        self.apply_filter()
        self._load_games()

    def _layout_category_tabs(self):
        """Each console has its own tabs: MSU-1 only exists on the SNES, and the
        Pokémon tab is for GBA."""
        for key, _ in CATEGORIES:
            self.tab_btns[key].pack_forget()
        for key, _ in CATEGORIES:
            if key in self.console()["tabs"]:
                self.tab_btns[key].pack(side="left")

    def _style_platform_tabs(self):
        c = THEMES[self.theme]
        for p, b in self.platform_btns.items():
            on = p == self.platform
            b.configure(bg=c["pick"] if on else c["panel"], fg="#ffffff" if on else c["muted"],
                        font=(self.font_semi, 11),
                        activebackground=c["pick"] if on else c["sel"])

    # ---------------- what each tab shows ----------------
    @staticmethod
    def _is_msu(text):
        return bool(re.search(r"msu[-_ ]?1|\bmsu\b", text.lower()))

    def _patch_tags(self):
        """Names of games / hacks that have translation or MSU-1 patches, from the
        bundled romhacking.net index, RomhackPlaza and (SNES) Zeldix."""
        p = self.platform
        if p in self._tags:
            return self._tags[p]
        tags = {"translation": {"games": set(), "titles": set()},
                "msu1": {"games": set(), "titles": set()}}
        for e in sources.rhn_index(p):
            kinds = []
            if e["kind"] == "translation":
                kinds.append("translation")
            if self._is_msu(e["title"]):
                kinds.append("msu1")
            for k in kinds:
                tags[k]["games"].add(library.norm(e["game"]))
                tags[k]["titles"].add(library.norm(e["title"]))
        self._tags[p] = tags
        # online lists load in the background, then the tab refreshes
        self.run_bg(lambda: self._online_tags(p), lambda extra: self._merge_tags(p, extra),
                    fail=lambda m: None)
        return tags

    def _online_tags(self, p):
        extra = {"translation": set(), "msu1": set()}
        try:
            for e in sources.plaza_index():
                if e.get("platform") != p:
                    continue
                t = re.sub(r"\b(english|spanish|french|german|italian|portuguese|"
                           r"translation|patch|romhack)\b", "", e["title"], flags=re.I)
                if e["kind"] == "translation":
                    extra["translation"].add(library.norm(t))
                if self._is_msu(e["title"]):
                    extra["msu1"].add(library.norm(re.sub(r"msu[-_ ]?1", "", t, flags=re.I)))
        except Exception:
            pass
        if p == "snes":
            try:
                for title in sources._zeldix_index():
                    extra["msu1"].add(library.norm(title))
            except Exception:
                pass
        return extra

    def _merge_tags(self, p, extra):
        for k, names in extra.items():
            self._tags[p][k]["titles"] |= {n for n in names if len(n) >= 4}
        self._patch_cache = {}
        if self.platform == p and self.category in ("translation", "msu1"):
            self.apply_filter()

    def has_patch(self, game, kind):
        """Translations / MSU-1 tab: a game shows if a patch of that kind exists for it;
        a hack only if that hack itself has one."""
        key = (self.platform, game["id"], kind)
        if key in self._patch_cache:
            return self._patch_cache[key]
        tags = self._patch_tags()[kind]
        n = library.norm(game["name"])
        base = self.hack_of(game)[0] if is_hack(game) else None
        if base and library.norm(base["name"]) == n:
            ok = False  # e.g. "Chrono Trigger+": same letters as the original, so the
            # original's patches would match by mistake
        elif is_hack(game):
            ok = n in tags["titles"] or (len(n) >= 6 and any(n in t for t in tags["titles"]))
        else:
            main = library.norm(sources._clean_title(game["name"]))  # before the ":"
            ok = n in tags["games"] or n in tags["titles"] or \
                (len(n) >= 5 and any(t.startswith(n) for t in tags["titles"])) or \
                (main != n and len(main) >= 8 and main in tags["titles"])
        self._patch_cache[key] = ok
        return ok

    def _load_ra(self):
        p = self.platform
        if getattr(self, "_ra_loading", None) == p:
            return
        self._ra_loading = p
        self.status.configure(text="Loading RetroAchievements sets…")

        def done(entries):
            self._ra_loading = None
            table, originals, hashes = {}, {}, {}
            for e in entries:
                for h in e.get("hashes", []):
                    hashes.setdefault(h, e)
                title, hack = sources.ra_clean(e["title"])
                # "Kick & Rush | World Cup Soccer": every name after a | counts too
                names = [n.strip() for n in title.split("|") if n.strip()]
                for n in names:
                    for key in {library.norm(n), library.norm(strip_brand(n))}:
                        if hack:
                            table.setdefault(key, e)
                        elif "~" not in e["title"]:  # skip ~Homebrew~ / ~Prototype~
                            originals.setdefault(key, e)
            self._ra[p] = table
            self._ra_originals[p] = originals
            self._ra_hashes[p] = hashes
            if self.platform == p and self.category == "retro":
                self.apply_filter()
            self._update_ra_button()

        def fail(msg):
            self._ra_loading = None
            self._ra_error = msg
            self.status.configure(text=msg)
            if self.platform == p and self.category == "retro":
                self.apply_filter()
        self.run_bg(lambda: sources.ra_games(p), done, fail)

    def ra_entry(self, game):
        """The RetroAchievements hack set for this hack, if there is one."""
        table = self._ra.get(self.platform)
        if not table:
            return None
        n = library.norm(game["name"])
        if n in table:
            return table[n]
        base = self.hack_of(game)[0]
        if base:  # RA often writes "Base: Hack" where LaunchBox has just "Hack"
            e = table.get(library.norm(base["name"]) + n)
            if e:
                return e
        return None  # exact names only: an achievement set works with one specific hack

    def _rhn_stats(self):
        """romhacking.net downloads and release date by hack/translation title."""
        p = self.platform
        cache = getattr(self, "_rhn_stats_cache", {})
        if p not in cache:
            stats = {}
            for e in sources.rhn_index(p):
                key = library.norm(e["title"])
                if key and (key not in stats or (e.get("downloads") or 0) >
                            (stats[key][0] or 0)):
                    stats[key] = (e.get("downloads"), parse_rhn_date(e.get("date", "")))
            cache[p] = stats
            self._rhn_stats_cache = cache
        return cache[p]

    def _hack_dates(self):
        """{norm(hack title): date} from every source that dates its hacks: Hackdex
        (last update), RomhackPlaza (page date), PokéCommunity (thread start) and
        PokéHarbor (post date) - for tiles LaunchBox and romhacking.net don't date."""
        cache = self.__dict__.setdefault("_hack_dates_cache", {})
        if self.platform in cache:
            return cache[self.platform]
        if not getattr(self, "_lists_ready", False):
            return {}, {}  # still downloading in the background: re-sorted when it's done
        dates, hd_dl = {}, {}

        def put(title, date):
            if date and title:
                n = library.norm(title)
                for k in (n, n.replace("pokemon", "")):
                    if k and (k not in dates or date > dates[k]):
                        dates[k] = date
        plats = {"gb": {"gb", "gbc"}, "gbc": {"gb", "gbc"}}.get(self.platform, {self.platform})
        for fn in (lambda: [(e["title"], e.get("date", "")) for e in sources.plaza_index()
                            if e.get("platform") in plats],
                   lambda: [(e["title"], e.get("date", "")) for e in sources.pc_index()],
                   lambda: [(e["title"], e.get("date") or e.get("modified", ""))
                            for e in sources.pokeharbor_index() if e["platform"] in plats],
                   lambda: [(e["title"], sources.hd_date(e)) for e in sources.hackdex_index()
                            if e.get("platform") in plats]):
            try:
                for title, date in fn():
                    put(title, date)
            except Exception:
                pass
        try:  # Hackdex download counts: the popularity of Pokémon hacks
            for e in sources.hackdex_index():
                if e.get("platform") in plats and e.get("downloads"):
                    n = library.norm(e["title"])
                    for k in (n, n.replace("pokemon", "")):
                        hd_dl[k] = max(hd_dl.get(k, 0), e["downloads"])
        except Exception:
            pass
        cache[self.platform] = (dates, hd_dl)
        return cache[self.platform]

    def _sort_keys(self):
        """Popularity as a 0-1 percentile: Hackdex downloads first for Pokémon hacks,
        then romhacking.net downloads, then LaunchBox community rating counts (each
        scale ranked within its own group). Dates: LaunchBox / romhacking.net, then the
        other sources' dates for the hack's name."""
        stats = self._rhn_stats()
        other_dates, hd_dl = self._hack_dates()
        dls, votes, dates, hds = {}, {}, {}, {}
        for g in self.games:
            n = library.norm(g["name"])
            st = stats.get(n)
            dl = g.get("downloads") or (st[0] if st else None)
            if dl:
                dls[g["id"]] = dl
            if g.get("votes"):
                votes[g["id"]] = g["votes"]
            hd = hd_dl.get(n) or hd_dl.get(n.replace("pokemon", ""))
            if hd:
                hds[g["id"]] = hd
            dates[g["id"]] = g.get("date") or (st[1] if st and st[1] else "") or \
                other_dates.get(n) or other_dates.get(n.replace("pokemon", "")) or ""

        def pct(d):
            order = sorted(d, key=d.get)
            return {gid: (i + 1) / len(order) for i, gid in enumerate(order)}
        pd, pv, ph = pct(dls), pct(votes), pct(hds)
        # a Hackdex download count puts a Pokémon hack ahead of the other scales
        pop = {g["id"]: (1 + ph[g["id"]]) if g["id"] in ph else
               max(pd.get(g["id"], -1), pv.get(g["id"], -1)) for g in self.games}
        return pop, dates

    def _rating_scores(self):
        """LaunchBox community rating, weighted by how many people voted so a
        handful of 5-star votes doesn't outrank a classic with thousands
        (score = (v*R + m*C) / (v + m), C = the console's average rating)."""
        if getattr(self, "_rating_for", None) is self.games:
            return self._rating
        rated = [g for g in self.games if g.get("votes")]
        avg = sum(g["rating"] for g in rated) / len(rated) if rated else 3.5
        m = 25
        self._rating = {g["id"]: (g["votes"] * g.get("rating", 0) + m * avg) / (g["votes"] + m)
                        for g in rated}
        self._rating_for = self.games
        return self._rating

    def _sort(self, games):
        mode = self.sort_var.get()
        name = lambda g: g["name"].strip().lower()
        if mode in ("az", "za"):
            games.sort(key=name, reverse=mode == "za")
            return
        if getattr(self, "_keys_for", None) is not self.games:
            self._keys = self._sort_keys()
            self._keys_for = self.games
        pop, dates = self._keys
        if mode == "popular":
            games.sort(key=lambda g: (-pop.get(g["id"], -1), name(g)))
        elif mode == "rating":
            score = self._rating_scores()
            games.sort(key=lambda g: (-score.get(g["id"], -1), name(g)))
        else:  # newest / oldest; games without a date go last either way
            dated = [g for g in games if dates.get(g["id"])]
            undated = [g for g in games if not dates.get(g["id"])]
            dated.sort(key=lambda g: (dates[g["id"]], name(g)), reverse=mode == "newest")
            undated.sort(key=name)
            games[:] = dated + undated

    def is_pokemon_hack(self, game):
        """Pokémon tab: hacks with Pokémon in the name or built on a Pokémon game."""
        if not is_hack(game):
            return False
        if "pokemon" in library.norm(game["name"]) or "pokmon" in game["name"].lower():
            return True
        base = self.hack_of(game)[0]
        return bool(base) and "pokemon" in library.norm(base["name"])

    # ---------------- game list ----------------
    def _load_games(self):
        p = self.platform
        if p in self._game_cache:
            self._show_games(self._game_cache[p])
            return
        games = covers.load_games(p)
        if games and not covers.needs_rebuild(games):
            self._games_ready(games)
        else:
            self.status.configure(text=f"Building the {self.console()['name']} game list "
                                       "from LaunchBox (~100 MB, one time)...")
            self.run_bg(lambda: covers.build_game_list(self.set_status, platform=p),
                        self._games_ready)

    def _add_rhn_tiles(self, games):
        """Add a tile (with the base game's cover) for each complete hack on
        romhacking.net that the LaunchBox list doesn't already have."""
        by_norm = {}
        for g in games:
            if not is_hack(g):
                by_norm.setdefault(library.norm(g["name"]), g)
        known = {library.norm(g["name"]) for g in games}
        extra = []
        for e in sources.rhn_index(self.platform):
            if e["kind"] != "hack" or e["category"] != "Complete":
                continue
            base = by_norm.get(library.norm(e["game"]))
            t = library.norm(e["title"])
            if not base or not base.get("cover") or t in known or \
                    library.norm(base["name"]) + t in known:
                continue
            known.add(t)
            extra.append({"id": f"rhn-{e['id']}", "name": e["title"], "type": "ROM Hack",
                          "year": e["date"][-4:], "date": parse_rhn_date(e["date"]),
                          "downloads": e.get("downloads"), "cover": base["cover"],
                          "cover_id": base["id"], "base_id": base["id"]})
        return games + extra

    # small technical patches aren't games of their own: they stay in the results lists
    NOT_A_TILE = re.compile(r"fastrom|\bsram\b|\bntsc\b|\bpal\b|fix(es|ed)?\b|bug ?fix|"
                            r"crash|joypad|controller|\bmsu\b|msu ?1|widescreen|60 ?hz|"
                            r"\bpatch\b|\bsave\b|\bcheat|trainer|uncensor|\bram\b", re.I)

    def _more_tiles(self, games):
        """Hacks LaunchBox doesn't list, as tiles with their base game's cover: every
        RAPatches and RomhackPlaza hack, plus Pokémon hacks from romhacking.net's archive
        and PokéCommunity's showcase."""
        bases = [g for g in games if not is_hack(g) and g.get("cover")]
        if not bases:
            return []
        by_norm = {}
        for b in sorted(bases, key=lambda b: len(b["name"])):  # plain name before editions
            by_norm.setdefault(library.norm(b["name"]), b)
            for alt in b.get("alt", []):
                by_norm.setdefault(library.norm(alt), b)
        pokemon = [b for b in bases if "pokemon" in library.norm(b["name"])]
        prefixes = sorted(by_norm, key=len, reverse=True)
        # "Pokémon" and "Pokemon" are the same hack: compare names without the accent
        same = lambda t: library.norm(t).replace("pokmon", "pokemon")
        known = {same(g["name"]) for g in games}
        tiles = []
        art_names = {}  # hacks with their own box art (Hackdex)

        def base_named(name):
            """The base game for a game name (RAPatches folder, archive game)."""
            return by_norm.get(library.norm(name))

        def base_prefix(title):
            """The base game a hack title starts with ("Super Metroid Liberation")."""
            n = library.norm(title)
            for pre in prefixes:
                if len(pre) >= 6 and n.startswith(pre) and len(n) > len(pre) + 2:
                    return by_norm[pre], pre
            return None, None

        def pokemon_base(text):
            hits = [b for b in pokemon if sources._pc_matches(text, b["name"])]
            return min(hits, key=lambda b: len(b["name"])) if hits else None

        def add(title, base, src, key, **extra):
            n = same(title)
            if not base or not n or self.NOT_A_TILE.search(title):
                return
            short = n.replace(library.norm(base["name"]), "")
            if n in known or (short and short in known) or n.replace("pokemon", "") in known:
                return
            known.update({n, short} - {""})
            tiles.append({"id": f"x-{src}-{key}", "name": title, "type": "ROM Hack",
                          "cover": base["cover"], "cover_id": base["id"],
                          "base_id": base["id"], **extra})
            if extra.get("cover"):
                art_names[n] = (extra["cover"], extra["cover_id"])

        def nice(base, rest):
            """'Super Metroid' + 'Liberation' -> 'Super Metroid: Liberation'"""
            rest = rest.strip(" -:")
            if not rest or library.norm(base["name"]) in library.norm(rest):
                return rest or base["name"]
            return f"{base['name']}: {rest}"

        # RAPatches: Hacks/<Game>/<id>-<Abbrev>-<Name>.zip
        try:
            console = sources.RAP_CONSOLE.get(self.platform)
            ra_titles = {}
            if sources.RA_KEY:  # RetroAchievements' name for the hack, when there is one
                try:
                    ra_titles = {g["id"]: sources.ra_clean(g["title"])[0]
                                 for g in sources.ra_games(self.platform)}
                except Exception:
                    pass
            for e in sources.rapatches_index():
                if e["console"] != console or e.get("kind") != "hacks" or not e["folder"]:
                    continue
                folder = sources._rap_game_folder(e["folder"])
                base = base_named(folder)
                if "pok" in folder.lower():
                    base = base or pokemon_base(folder)
                words = e["name"].split("-")
                rest = sources._uncamel("-".join(words[1:]) if len(words) > 1 else e["name"])
                title = ra_titles.get(e["id"]) or (nice(base, rest) if base else rest)
                add(title, base, "rap", e["id"], own_result={
                    "source": "RAPatches", "title": title, "category": "hack",
                    "desc": "From RetroAchievements' RAPatches collection on GitHub.",
                    "url": f"https://github.com/{sources.RAP_REPO}/blob/main/"
                           + sources.requests.utils.quote(e["path"]),
                    "rap_path": e["path"], "size": e["size"]})
        except Exception as ex:  # offline / rate-limited: the other lists still work
            log.warning("RAPatches list unavailable: %s", ex)
        # RomhackPlaza: the title starts with the game's name
        try:
            # RomhackPlaza files Game Boy Color games (Pokemon Gold...) under Game Boy too
            plats = {"gb": {"gb", "gbc"}, "gbc": {"gb", "gbc"}}.get(self.platform,
                                                                    {self.platform})
            for e in sources.plaza_index():
                if e.get("platform") not in plats or e.get("kind") != "hack":
                    continue
                base, pre = base_prefix(e["title"])
                if not base and pokemon and "pok" in e["title"].lower():
                    # "Pokemon Fire Red Reignited": find the game by its version word
                    base = pokemon_base(sources._pc_guess_base(e["title"]) or "")
                    if base:
                        add(e["title"], base, "plaza", abs(hash(e["url"])), own_result={
                            "source": "RomhackPlaza", "title": e["title"],
                            "category": "hack", "desc": "Hack on RomhackPlaza.",
                            "url": e["url"]})
                    continue
                if base:
                    words = e["title"].split()
                    # drop as many leading words as make up the game's name
                    acc, cut = "", 0
                    for i, w in enumerate(words):
                        acc += library.norm(w)
                        if acc == pre:
                            cut = i + 1
                            break
                    title = nice(base, " ".join(words[cut:])) if cut else e["title"]
                    add(title, base, "plaza", abs(hash(e["url"])), own_result={
                        "source": "RomhackPlaza", "title": e["title"], "category": "hack",
                        "desc": "Hack on RomhackPlaza.", "url": e["url"]})
        except Exception as ex:
            log.warning("RomhackPlaza list unavailable: %s", ex)
        # Pokémon: romhacking.net's archive and PokéCommunity
        if "pokemon" in self.console()["tabs"] and pokemon:
            for e in sources.rhn_index(self.platform):
                if e["kind"] == "hack" and "pok" in e["game"].lower():
                    add(e["title"], pokemon_base(e["game"]), "rhn", e["id"],
                        downloads=e.get("downloads"), own_result=sources._rhn_result(e))
            try:
                for e in sources.pc_index():
                    add(e["title"], pokemon_base(e["base"]), "pc", abs(hash(e["url"])),
                        downloads=e.get("views"), own_result={
                            "source": "PokéCommunity", "title": e["title"], "url": e["url"],
                            "category": "hack", "desc": f"{e['base']} hack on PokéCommunity's "
                                                        "Completed ROM Hacks Showcase"})
            except Exception as ex:
                log.warning("PokéCommunity list unavailable: %s", ex)
            plats = {"gb": {"gb", "gbc"}, "gbc": {"gb", "gbc"}}.get(self.platform,
                                                                    {self.platform})
            try:  # Hackdex first: it has the exact base ROM and download counts
                for e in sources.hackdex_index():
                    if e.get("platform") in plats:
                        base = re.sub(r"\s*\([^)]*\)", "", e["base"])
                        art = {"cover": sources.hd_boxart(e),
                               "cover_id": "hd-" + e["url"].rstrip("/").rsplit("/", 1)[-1]} \
                            if sources.hd_boxart(e) else {}
                        add(e["title"], pokemon_base(base.replace("Pokémon", "")
                                                     .replace("Pokemon", "")),
                            "hd", abs(hash(e["url"])), downloads=e.get("downloads"),
                            own_result=sources._hd_result(e), **art)
            except Exception as ex:
                log.warning("Hackdex list unavailable: %s", ex)
            # Hackdex box art also replaces the base game's cover on hacks already listed
            try:
                for e in sources.hackdex_index():
                    if sources.hd_boxart(e):
                        art_names.setdefault(library.norm(e["title"]),
                                             (sources.hd_boxart(e), "hd-" + e["url"].rstrip("/")
                                              .rsplit("/", 1)[-1]))
                for g in games + tiles:
                    n = library.norm(g["name"])
                    art = art_names.get(n) or art_names.get(n.replace("pokemon", "")) or \
                        art_names.get("pokemon" + n)
                    # the hack's own box art beats the default (base game / LaunchBox) cover
                    if art and is_hack(g):
                        g["cover"], g["cover_id"] = art
            except Exception as ex:
                log.warning("Hackdex box art unavailable: %s", ex)
            try:
                for e in sources.pokeharbor_index():
                    if e["platform"] in plats and e["base"]:
                        # only a tile (when no other source has the hack): PokéHarbor's
                        # downloads are pre-patched ROMs, so nothing of it is in the results
                        add(e["title"], pokemon_base(e["base"]), "ph", abs(hash(e["url"])))
            except Exception as ex:
                log.warning("PokéHarbor list unavailable: %s", ex)
        return tiles

    def _games_ready(self, games):
        self.games = self._add_rhn_tiles(list(games))
        self._build_hack_index()
        # hacks LaunchBox has no cover for borrow their base game's cover
        for g in self.games:
            if not g.get("cover") and is_hack(g):
                base = self.hack_of(g)[0]
                if base and base.get("cover"):
                    g["cover"], g["cover_id"] = base["cover"], base["id"]
        self.games = [g for g in self.games if g.get("cover")]
        self._game_cache[self.platform] = self.games
        self._show_games(self.games)
        self._add_more_tiles_later(self.platform)

    # the online lists the extra tiles, sorting dates and the direct-downloads filter use
    def _fetch_lists(self, platform):
        jobs = [sources.rapatches_index, sources.plaza_index, sources.hackdex_index]
        if "pokemon" in CONSOLES[platform]["tabs"]:
            jobs += [sources.pc_index, sources.pokeharbor_index]
        if sources.RA_KEY:
            jobs.append(lambda: sources.ra_games(platform))
        if platform == "snes":
            jobs.append(sources._zeldix_index)
        for job in jobs:
            try:
                job()
            except Exception as e:  # offline / a site down: the others still help
                log.warning("Couldn't load a list for %s: %s", platform, e)

    def _add_more_tiles_later(self, platform):
        """Show the grid straight away, then download the online lists in the background
        (the first start has none cached) and add their tiles when they arrive - so the
        window never freezes waiting for websites."""
        done_for = self.__dict__.setdefault("_more_tiles_done", set())
        if platform in done_for:
            return

        def done(_):
            if self.platform != platform or platform in done_for:
                return  # switched console: added next time it's shown
            done_for.add(platform)
            self._lists_ready = True
            tiles = self._more_tiles(self.games)
            self.games = self.games + [t for t in tiles if t.get("cover")]
            self._game_cache[platform] = self.games
            for attr in ("_direct_cache", "_hack_dates_cache", "_keys_for"):
                self.__dict__.pop(attr, None)
            self._show_games(self.games)
        self.run_bg(lambda: self._fetch_lists(platform), done, lambda m: None)

    def _show_games(self, games):
        self.games = games
        for attr in ("_rhn_game", "_bases"):
            if hasattr(self, attr):
                delattr(self, attr)
        self._set_aliases()
        self._build_hack_index()
        if not covers.has_alt_names() and not getattr(self, "_alt_rebuild", False):
            # lists made before alternate names were kept: rebuild once, in the background
            self._alt_rebuild = True

            def done(_res):
                self._game_cache = {}
                self.__dict__.pop("_archive_extra", None)
                self.__dict__.pop("_hack_text", None)
                self._load_games()
            self.run_bg(lambda: covers.build_game_list(lambda m: None,
                                                       platform=self.platform),
                        done, lambda m: log.warning("Alternate-name rebuild failed: %s", m))
        self.status.configure(text=f"{len(self.games)} {self.console()['name']} games")
        self.apply_filter()

    def _schedule_filter(self):
        if hasattr(self, "_filter_job"):
            self.after_cancel(self._filter_job)
        self._filter_job = self.after(250, self.apply_filter)

    def apply_filter(self):
        q = self.search_var.get().lower().strip()
        self.filtered = self._games_for(self.category, q)
        self._sort(self.filtered)
        self._after_filter(q)

    def _games_for(self, category, q):
        """The games a tab shows for the current filters and search text."""
        games = self.games
        if not self.show_hidden.get():
            hidden = self.hidden()
            if hidden:
                games = [g for g in games if g["id"] not in hidden]
        if self.owned_only.get():
            owned = set(self.owned())
            keep = library.related_ids(owned, self.games)
            # plus every hack whose base game you own, even when its name doesn't
            # say so (e.g. MaternalBound Redux for EarthBound)
            for g in self.games:
                if is_hack(g):
                    base = self.hack_of(g)[0]
                    if base and base["id"] in owned:
                        keep.add(g["id"])
            games = [g for g in games if g["id"] in keep]
        if self.direct_only.get():
            games = [g for g in games if self._has_direct(g)]
        if self.hacks_only.get() and category not in ("translation", "msu1", "archive"):
            games = [g for g in games if is_hack(g)]
        if category == "hack" and "pokemon" in self.console()["tabs"]:
            # Pokémon hacks live in their own tab
            games = [g for g in games if not self.is_pokemon_hack(g)]
        if category == "translation":
            games = [g for g in games if self.has_patch(g, "translation")]
        elif category == "msu1":
            games = [g for g in games if self.has_patch(g, "msu1")]
        elif category == "pokemon":
            games = [g for g in games if self.is_pokemon_hack(g)]
        elif category == "retro":
            games = [g for g in games if is_hack(g) and self.ra_entry(g)]
        elif category == "archive":
            games = self._archive_games()

        hack_text = self._archive_hack_text() if category == "archive" and q else {}

        def matches(g):  # a hack also matches its base game's name
            if q in g["name"].lower():
                return True
            if hack_text and q in hack_text.get(sources._norm(g["name"]), ""):
                return True  # "maternalbound" finds EarthBound in the 2024 Archive
            base = self.hack_of(g)[0] if is_hack(g) else None
            return bool(base) and q in base["name"].lower()
        return [g for g in games if matches(g)] if q else list(games)

    def _archive_hack_text(self):
        """{norm(game name): all its archive hack / translation titles, lower case}"""
        cache = self.__dict__.setdefault("_hack_text", {})
        key = (self.platform, len(sources.rhn_index(self.platform)))
        if key not in cache:
            text = {}
            aliases = sources.GAME_ALIASES.get(self.platform, {})
            back = {}
            for name, others in aliases.items():
                for o in others:
                    back.setdefault(o, set()).add(name)
            for e in sources.rhn_index(self.platform):
                n = sources._norm(e.get("game", ""))
                for k in {n} | back.get(n, set()):
                    text[k] = text.get(k, "") + " " + e["title"].lower()
            cache[key] = text
        return cache[key]

    def _set_aliases(self):
        """Tell sources the other names (LaunchBox alternate names) of each game."""
        al = {}
        for g in self.games:
            if g.get("alt"):
                al[sources._norm(g["name"])] = {sources._norm(a) for a in g["alt"]} - {""}
        sources.GAME_ALIASES[self.platform] = al

    def _archive_games(self):
        """Every game in romhacking.net's 2024 archive for this console - the whole
        archive, so "only games I own" doesn't apply (hidden games stay hidden). Games
        with no LaunchBox entry get a plain tile of their own."""
        if self._archive_state() != "ready":
            return []  # the grid shows the sign-in / loading message instead
        names, hacks = {}, set()
        for e in sources.rhn_index(self.platform):
            if e.get("game"):
                names.setdefault(sources._norm(e["game"]), e["game"])
            if e.get("kind") == "hack":
                hacks.add(sources._norm(e["title"]))
        hidden = set() if self.show_hidden.get() else set(self.hidden())
        aliases = sources.GAME_ALIASES.get(self.platform, {})

        def in_archive(g):
            n = sources._norm(g["name"])
            if n in names or aliases.get(n, set()) & names.keys():
                return True
            return is_hack(g) and n in hacks  # e.g. MaternalBound Redux's own entry
        games = [g for g in self.games if g["id"] not in hidden and in_archive(g)]
        have = {sources._norm(g["name"]) for g in self.games}
        for g in self.games:  # names a LaunchBox game is also known by
            have |= aliases.get(sources._norm(g["name"]), set())
        # romhacking.net files Game Boy and Game Boy Color together: a game that's in the
        # other console's list belongs there, not here as a cover-less tile
        other = {"gb": "gbc", "gbc": "gb"}.get(self.platform)
        if other:
            have |= {sources._norm(g["name"]) for g in covers.load_games(other) or []}
        extra = self.__dict__.setdefault("_archive_extra", {})
        if not self.games:
            return []  # the console's list is still loading
        key = (self.platform, len(names), len(self.games))
        if key not in extra:  # made once, so the same tile stays selected
            extra[key] = [{"id": f"rhn:{n}", "name": title, "type": ""}
                          for n, title in names.items() if n and n not in have]
        return games + [g for g in extra[key] if g["id"] not in hidden]

    def _after_filter(self, q):
        self._style_tabs()
        self._reset_grid()
        note = ("  ·  the whole archive is shown here, even with \"only games I own\" on"
                if self.category == "archive" and self.owned_only.get() else "")
        self.status.configure(text=f"{len(self.filtered)} games{note}")
        if self.category == "retro" and self.platform not in self._ra and sources.RA_KEY \
                and not getattr(self, "_ra_error", None):
            self._load_ra()
        if not self.filtered:
            self._empty_message()

    # ---------------- cover grid (drawn on the canvas, visible rows only) ----------------
    # Each cover is a few canvas items instead of several widgets, and only the rows on
    # screen (plus a small margin) exist at any time - so thousands of games scroll as
    # smoothly as a dozen.

    def _cell(self):
        gap = int(14 * self.k)
        return TILE_W + int(16 * self.k) + gap, TILE_H + int(64 * self.k) + gap, gap

    def _reset_grid(self):
        self.canvas.delete("all")
        self._drawn = {}
        if getattr(self, "empty_box", None) is not None:
            self.empty_box.destroy()
            self.empty_box = None
        self._layout_grid()
        self._scroll_to(0, animate=False)

    def _layout_grid(self):
        cw, ch, gap = self._cell()
        width = max(self.canvas.winfo_width(), cw)
        self.cols = max(1, (width - gap) // cw)
        rows = -(-len(self.filtered) // self.cols)
        self._grid_h = rows * ch + gap
        self.canvas.configure(scrollregion=(0, 0, width, max(self._grid_h,
                                                             self.canvas.winfo_height())))

    def _visible_range(self):
        cw, ch, gap = self._cell()
        top = self.canvas.canvasy(0)
        bottom = top + self.canvas.winfo_height()
        first = max(0, int(top // ch) - 1) * self.cols
        last = min(len(self.filtered), (int(bottom // ch) + 2) * self.cols)
        return first, last

    def _render(self):
        """Draw the covers now on screen; drop ones well out of view."""
        if not self.filtered:
            return
        first, last = self._visible_range()
        keep_from, keep_to = first - self.cols * 2, last + self.cols * 2
        for i in [i for i in self._drawn if i < keep_from or i >= keep_to]:
            for item in self._drawn.pop(i)["items"]:
                self.canvas.delete(item)
        self._wanted = {self.filtered[i].get("cover_id", self.filtered[i]["id"])
                        for i in range(first, last)}  # covers worth loading right now
        for i in range(first, last):
            if i not in self._drawn:
                self._draw_tile(i)
            elif self.canvas.type(self._drawn[i]["img"]) == "text":
                self._request_thumb(self._drawn[i]["game"])  # skipped earlier: retry

    def _draw_tile(self, i):
        game = self.filtered[i]
        c = THEMES[self.theme]
        cw, ch, gap = self._cell()
        col, row = i % self.cols, i // self.cols
        x, y = gap + col * cw, gap + row * ch
        w, h = cw - gap, ch - gap
        sel = self.selected is game
        pad = int(8 * self.k)
        items = [self.canvas.create_rectangle(x, y, x + w, y + h, width=0,
                                              fill=c["pick"] if sel else c["card"])]
        key = game.get("cover_id", game["id"])
        photo = self.thumbs.get(key)
        if photo is not None:
            self.thumbs.move_to_end(key)
            img = self.canvas.create_image(x + w // 2, y + pad, image=photo, anchor="n")
        elif not game.get("cover"):
            img = self.canvas.create_text(x + w // 2, y + pad + TILE_H // 2, text="No cover",
                                          fill=c["muted"], font=(self.font_reg, 10))
        else:
            img = self.canvas.create_text(x + w // 2, y + pad + TILE_H // 2, text="…",
                                          fill=c["muted"], font=(self.font_reg, 12))
            self._request_thumb(game)
        items.append(img)
        label = game["name"] if len(game["name"]) <= 36 else game["name"][:34] + "…"
        if game["id"] in self.hidden():
            label += "  (hidden)"
        ty = y + pad + TILE_H + int(6 * self.k)
        text_w = w - 2 * pad
        cx = x + w // 2
        if game["id"] in self.owned():  # green tick to the left of the name
            t = int(16 * self.k)
            tx = x + pad
            items.append(self.canvas.create_rectangle(tx, ty + 1, tx + t, ty + t + 1,
                                                      fill="#1fa64a", width=0))
            items.append(self.canvas.create_text(tx + t // 2, ty + t // 2 + 1, text="✔",
                                                 fill="#ffffff",
                                                 font=("Segoe UI Symbol", 8, "bold")))
            text_w -= t + int(4 * self.k)
            cx += (t + int(4 * self.k)) // 2
        name = self.canvas.create_text(cx, ty, text=label, anchor="n", width=text_w,
                                       justify="center", fill="#ffffff" if sel else c["fg"],
                                       font=(self.font_semi if sel else self.font_reg, 9))
        items.append(name)
        self._drawn[i] = {"items": items, "img": img, "rect": items[0], "name": name,
                          "key": key, "game": game}

    def _request_thumb(self, game):
        key = game.get("cover_id", game["id"])
        if key in self._thumb_pending:
            return
        self._thumb_pending.add(key)
        self.pool.submit(self._load_cover, game)

    def _load_cover(self, game):
        key = game.get("cover_id", game["id"])
        if key not in self._wanted:  # scrolled past before its turn came: skip it
            self.ui_queue.put(lambda: self._thumb_pending.discard(key))
            return
        try:
            p = covers.cover_path(game)
            im = Image.open(p).convert("RGB") if p else None
            if im:
                im.thumbnail((TILE_W, TILE_H))
        except Exception:
            im = None

        def show():
            self._thumb_pending.discard(key)
            if im is None:
                return
            photo = ImageTk.PhotoImage(im)
            self.thumbs[key] = photo
            while len(self.thumbs) > 900:  # keep memory in check: drop the oldest
                self.thumbs.popitem(last=False)
            cw, ch, gap = self._cell()
            for i, d in self._drawn.items():
                if d["key"] == key and self.canvas.type(d["img"]) == "text":
                    x0, y0, x1, _ = self.canvas.coords(d["rect"])
                    self.canvas.delete(d["img"])
                    new = self.canvas.create_image((x0 + x1) / 2, y0 + int(8 * self.k),
                                                   image=photo, anchor="n")
                    d["items"][d["items"].index(d["img"])] = new
                    d["img"] = new
        self.ui_queue.put(show)

    def _layout_tiles(self):
        self._drawn_reset_keep_scroll()

    def _drawn_reset_keep_scroll(self):
        top = self.canvas.canvasy(0)
        self.canvas.delete("all")
        self._drawn = {}
        self._layout_grid()
        self._scroll_to(top, animate=False)
        box = getattr(self, "empty_box", None)
        if box is not None and box.winfo_exists() and not self.filtered:
            # resizing redraws the grid: put the same message back (keeps typed text)
            self.canvas.create_window(0, 0, window=box, anchor="nw", tags="empty",
                                      width=self.canvas.winfo_width())

    def _on_resize(self, e):
        cw, ch, gap = self._cell()
        cols = max(1, (e.width - gap) // cw)
        if cols != self.cols or not self._drawn:
            self._drawn_reset_keep_scroll()
        else:
            self._layout_grid()
            self._render()
        if getattr(self, "empty_box", None) is not None:
            self.canvas.itemconfigure("empty", width=e.width)

    # ---- smooth scrolling ----
    def _scroll_to(self, y, animate=True):
        """Glide (or jump) so the canvas top is at pixel y."""
        h = max(self._grid_h, self.canvas.winfo_height())
        max_y = max(0, h - self.canvas.winfo_height())
        self._scroll_target = min(max(0, y), max_y)
        if not animate:
            self.canvas.yview_moveto(self._scroll_target / h if h else 0)
            self._render()
            return
        if not getattr(self, "_scrolling", False):
            self._scrolling = True
            self._scroll_step()

    def _scroll_step(self):
        h = max(self._grid_h, self.canvas.winfo_height())
        cur = self.canvas.canvasy(0)
        diff = self._scroll_target - cur
        if abs(diff) < 1:
            self.canvas.yview_moveto(self._scroll_target / h)
            self._render()
            self._scrolling = False
            return
        step = diff * 0.35 if abs(diff) > 3 else diff  # ease out
        self.canvas.yview_moveto((cur + step) / h)
        self._render()
        self.after(12, self._scroll_step)

    def _on_wheel(self, e):
        w = self.winfo_containing(e.x_root, e.y_root)
        if not (w and str(w).startswith(str(self.canvas))):
            return
        cw, ch, gap = self._cell()
        start = self._scroll_target if getattr(self, "_scrolling", False) else \
            self.canvas.canvasy(0)
        self._scroll_to(start - (e.delta / 120) * ch * 0.6)

    def _scrollbar(self, *args):
        """Dragging the scrollbar: move straight there, then draw what's visible."""
        self._scrolling = False
        self.canvas.yview(*args)
        self._scroll_target = self.canvas.canvasy(0)
        self._render()

    # ---- clicks ----
    def _tile_at(self, e):
        cw, ch, gap = self._cell()
        x, y = self.canvas.canvasx(e.x) - gap, self.canvas.canvasy(e.y) - gap
        if x < 0 or y < 0 or x % cw > cw - gap or y % ch > ch - gap:
            return None
        col, row = int(x // cw), int(y // ch)
        i = row * self.cols + col
        return self.filtered[i] if col < self.cols and i < len(self.filtered) else None

    def _grid_click(self, e):
        g = self._tile_at(e)
        if g:
            self.select_game(g)

    def _grid_double(self, e):
        g = self._tile_at(e)
        if g:
            self._open_and_find(g)

    def _grid_menu(self, e):
        g = self._tile_at(e)
        if g:
            self._tile_menu(e, g)

    def _restyle_tiles(self):
        """Selection / theme change: recolour the covers on screen."""
        c = THEMES[self.theme]
        self.canvas.configure(bg=c["bg"])
        for d in self._drawn.values():
            sel = self.selected is d["game"]
            self.canvas.itemconfigure(d["rect"], fill=c["pick"] if sel else c["card"])
            self.canvas.itemconfigure(d["name"], fill="#ffffff" if sel else c["fg"],
                                      font=(self.font_semi if sel else self.font_reg, 9))

    def scroll_to_game(self, i):
        cw, ch, gap = self._cell()
        self._scroll_to(max(0, (i // self.cols) * ch - gap))

    def _empty_message(self):
        """Explain an empty grid in the grid itself, not just the status bar."""
        if getattr(self, "empty_box", None) is not None:
            self.empty_box.destroy()
        box = ttk.Frame(self.canvas, padding=int(30 * self.k))
        self.canvas.create_window(0, 0, window=box, anchor="nw", tags="empty",
                                  width=self.canvas.winfo_width())
        self.empty_box = box
        wrap = int(520 * self.k)
        if self.category == "archive" and self._archive_state() != "ready":
            self._archive_message(box, wrap)
        elif self.category == "retro" and not sources.RA_KEY:
            ttk.Label(box, text="RetroAchievements needs your free Web API key",
                      style="Title.TLabel").pack(anchor="w")
            ttk.Label(box, justify="left", text=(  # no wrapping: one line per step
                "1. Log in at retroachievements.org (or make a free account).\n"
                "2. Open Settings > Keys and copy your Web API key.\n"
                "3. Open Settings inside of Vibe Patcher > Accounts and paste it into the "
                "\"RetroAchievements Web API key\" box.\n"
                "    (Or use \"Add my key\")")).pack(anchor="w",
                                                                              pady=8)
            row = ttk.Frame(box)
            row.pack(anchor="w", pady=(int(12 * self.k), 0))  # a little gap below the steps
            ttk.Button(row, text="Get my key", style="Accent.TButton", command=lambda:
                       open_in_browser("https://retroachievements.org/settings")).pack(
                side="left")
            ttk.Button(row, text="Add my key", command=lambda: self.open_settings(
                "Accounts")).pack(side="left", padx=8)
            ttk.Button(row, text="Make a free account", command=lambda: open_in_browser(
                "https://retroachievements.org/createaccount.php")).pack(side="left")
        elif self.category == "retro" and getattr(self, "_ra_error", None):
            ttk.Label(box, text=self._ra_error, wraplength=wrap).pack(anchor="w")
            ttk.Button(box, text="⚙ Open Settings", command=self.open_settings).pack(
                anchor="w", pady=8)
        elif self.category == "retro" and self.platform not in self._ra:
            ttk.Label(box, text="Loading RetroAchievements sets…").pack(anchor="w")
        elif self.owned_only.get() and not self.owned():
            ttk.Label(box, wraplength=wrap, text=(
                f"No {self.console()['name']} games of yours have been found yet, and "
                "'Only Show Games I Own' is turned on (Filter menu).")).pack(anchor="w")
            ttk.Button(box, text="  Scan Games", image=self.folder_icon, compound="left",
                       command=self.open_scan_window).pack(anchor="w", pady=8)
        else:
            ttk.Label(box, text="No games match the current tab, search and filters.",
                      wraplength=wrap).pack(anchor="w")

    def _archive_message(self, box, wrap):
        """The 2024 Archive's sign-in (or loading message), shown in the grid."""
        ttk.Label(box, text="romhacking.net 2024 Archive", style="Title.TLabel").pack(
            anchor="w")
        if self._archive_state() == "loading":
            ttk.Label(box, text="Loading romhacking.net's 2024 database from archive.org…",
                      wraplength=wrap).pack(anchor="w", pady=8)
            return
        ttk.Label(box, wraplength=wrap, justify="left", text=(
            "This tab has all of romhacking.net up to August 2024 - every hack and "
            "translation, with direct downloads right here in Vibe Patcher, without the need "
            "to open a browser to romhacking.net.\n\n"
            "It comes from romhacking.net's own backup on archive.org, which needs a free "
            "archive.org account. Sign in once; your password isn't saved, only the access "
            "keys archive.org gives back.")).pack(anchor="w", pady=(8, 12))
        grid = ttk.Frame(box)
        grid.pack(anchor="w", pady=(int(10 * self.k), 0))  # a little gap below the text
        email_var, pw_var = tk.StringVar(), tk.StringVar()
        ttk.Label(grid, text="Email").grid(row=0, column=0, sticky="w")
        email = ttk.Entry(grid, textvariable=email_var, width=34)
        email.grid(row=0, column=1, padx=8, pady=2)
        ttk.Label(grid, text="Password").grid(row=1, column=0, sticky="w")
        pw = ttk.Entry(grid, textvariable=pw_var, show="\u2022", width=34)
        pw.grid(row=1, column=1, padx=8, pady=2)
        msg = ttk.Label(box, text=getattr(self, "_archive_error", None) or "",
                        wraplength=wrap)
        msg.pack(anchor="w", pady=(6, 0))

        def sign_in():
            e, p_ = email_var.get().strip(), pw_var.get()
            pw_var.set("")
            if not e or not p_:
                msg.configure(text="Enter your archive.org email and password.")
                return
            msg.configure(text="Signing in…")

            def ok(creds):
                self.settings["archive_org"] = creds
                save_settings(self.settings)
                archive_org.CREDS = creds
                archive_org.reset()
                self._archive_error = None
                self.apply_filter()  # -> loading, then the archive
            self.run_bg(lambda: archive_org.login(e, p_), ok,
                        lambda m: msg.winfo_exists() and msg.configure(text=m))
        row = ttk.Frame(box)
        row.pack(anchor="w", pady=(8, 0))
        ttk.Button(row, text="Sign in", style="Accent.TButton", command=sign_in).pack(
            side="left")
        ttk.Button(row, text="Make a free account", command=lambda: open_in_browser(
            "https://archive.org/account/signup")).pack(side="left", padx=8)
        pw.bind("<Return>", lambda e: sign_in())
        email.bind("<Return>", lambda e: pw.focus_set())

    # ---------------- details / patches ----------------
    def _copy_row(self, item):
        if item:
            self.clipboard_clear()
            title = item.get("title") or item.get("name", "")
            self.clipboard_append(f"{title}\n{item.get('url', '')}".strip())
            self.status.configure(text="Copied to clipboard")
        return "break"

    @staticmethod
    def _first_char(game):
        m = re.search(r"[A-Za-z0-9]", game["name"])
        return m.group(0).lower() if m else ""

    def _on_letter(self, e):
        """Press a letter / digit: jump to the next game starting with it."""
        if not e.char or not e.char.isalnum() or e.state & 0x4:  # ignore Ctrl+key
            return
        focus = self.focus_get()
        if isinstance(focus, (tk.Entry, ttk.Entry, tk.Text, ttk.Combobox, tk.Listbox)) or \
                (focus and focus.winfo_toplevel() is not self):
            return  # typing in a box or another window
        ch = e.char.lower()
        hits = [i for i, g in enumerate(self.filtered) if self._first_char(g) == ch]
        if not hits:
            return
        cur = self.filtered.index(self.selected) if self.selected in self.filtered else -1
        nxt = next((i for i in hits if i > cur), hits[0])  # press again for the next one
        self.select_game(self.filtered[nxt])
        self.scroll_to_game(nxt)

    # ---------- hiding games ----------
    def hidden(self):
        return set(self.settings.get("hidden", []))

    def toggle_hidden(self, game):
        h = self.hidden()
        h.symmetric_difference_update({game["id"]})
        self.settings["hidden"] = sorted(h)
        save_settings(self.settings)
        self.apply_filter()

    def _tile_menu(self, e, game):
        m = tk.Menu(self, tearoff=False)
        c = THEMES[self.theme]
        m.configure(bg=c["card"], fg=c["fg"], activebackground=c["sel"],
                    activeforeground=c["fg"], bd=0, font=(self.font_reg, 10))
        hid = game["id"] in self.hidden()
        m.add_command(label="Unhide Game" if hid else "Hide Game",
                      command=lambda: self.toggle_hidden(game))
        m.tk_popup(e.x_root, e.y_root)

    def set_show_hidden(self):
        self.settings["show_hidden"] = self.show_hidden.get()
        save_settings(self.settings)
        self.apply_filter()

    def ra_game(self, game):
        """The RetroAchievements set for any game (original or hack), if loaded: by your
        ROM's hash when you own it (exact), otherwise by name."""
        mine = self.owned().get(game["id"])
        md5 = self._rom_ids.get(mine, {}).get("md5") if mine else None
        if md5 and md5 in self._ra_hashes.get(self.platform, {}):
            return self._ra_hashes[self.platform][md5]
        if is_hack(game):
            return self.ra_entry(game)
        originals = self._ra_originals.get(self.platform, {})
        return originals.get(library.norm(game["name"])) or \
            originals.get(library.norm(strip_brand(game["name"])))

    def _update_ra_button(self):
        """Show the trophy only when the selected game has achievements."""
        entry = self.ra_game(self.selected) if self.selected else None
        if entry:
            c = THEMES[self.theme]
            self.ra_btn.configure(bg=c["panel"], activebackground=c["panel"])
            self.ra_btn.pack(side="right", pady=(0, 4))
            self.status.configure(text=f"RetroAchievements: {entry['achievements']} "
                                       "achievements - click the trophy for supported ROMs")
        else:
            self.ra_btn.pack_forget()

    def _open_ra_page(self, page="hashes"):
        """page 'hashes' = supported game hashes, '' = the game's own page."""
        entry = self.ra_game(self.selected) if self.selected else None
        if entry:
            suffix = f"/{page}" if page else ""
            open_in_browser(f"https://retroachievements.org/game/{entry['id']}{suffix}")

    def _ra_menu(self, e):
        self._hide_tip()
        c = THEMES[self.theme]
        m = tk.Menu(self, tearoff=False, bg=c["card"], fg=c["fg"], activebackground=c["sel"],
                    activeforeground=c["fg"], bd=0, font=(self.font_reg, 10))
        m.add_command(label="Go To Supported Game Hashes",
                      command=lambda: self._open_ra_page("hashes"))
        m.add_command(label="Go to RetroAchievements Game Page",
                      command=lambda: self._open_ra_page(""))
        m.tk_popup(e.x_root, e.y_root)

    def _show_tip(self, widget, text):
        """A small tooltip just below the widget."""
        self._hide_tip()
        c = THEMES[self.theme]
        tip = tk.Toplevel(self)
        tip.wm_overrideredirect(True)
        tk.Label(tip, text=text, bg=c["card"], fg=c["fg"], bd=1, relief="solid",
                 font=(self.font_reg, 9), padx=6, pady=2).pack()
        tip.update_idletasks()
        x = widget.winfo_rootx() + widget.winfo_width() - tip.winfo_reqwidth()
        tip.wm_geometry(f"+{x}+{widget.winfo_rooty() + widget.winfo_height() + 4}")
        self._tip = tip

    def _file_full_text(self, i):
        f = self.files[i]
        if f.get("full_name"):
            return f"{sources.KIND_LABEL.get(f.get('kind'), 'File')}: {f['full_name']}"
        return f["name"]

    def _row_tip(self, tree, event, full_text):
        """Hovering a row whose text is cut off shows the whole text in a tooltip."""
        row = tree.identify_row(event.y)
        if getattr(self, "_row_tip_at", None) == (tree, row):
            return
        self._hide_row_tip()
        self._row_tip_at = (tree, row)
        if not row or tree.identify_column(event.x) != "#0":
            return
        try:
            text = full_text(int(row))
        except (IndexError, ValueError, KeyError):
            return
        import tkinter.font as tkfont
        font = tkfont.Font(font=ttk.Style().lookup("Treeview", "font") or "TkDefaultFont")
        shown = tree.item(row, "text")
        room = tree.column("#0", "width") - int(30 * self.k)  # minus the indent
        if text == shown and font.measure(text) <= room:
            return  # fits - nothing hidden
        c = THEMES[self.theme]
        tip = tk.Toplevel(self)
        tip.wm_overrideredirect(True)
        tk.Label(tip, text=text, bg=c["card"], fg=c["fg"], bd=1, relief="solid",
                 font=(self.font_reg, 10), padx=6, pady=2, wraplength=int(600 * self.k),
                 justify="left").pack()
        bbox = tree.bbox(row) or (0, event.y, 0, 20)
        tip.wm_geometry(f"+{tree.winfo_rootx() + 8}+{tree.winfo_rooty() + bbox[1] + bbox[3] + 2}")
        self._row_tip_win = tip

    def _hide_row_tip(self):
        tip = getattr(self, "_row_tip_win", None)
        if tip is not None:
            tip.destroy()
            self._row_tip_win = None

    def _hide_tip(self):
        tip = getattr(self, "_tip", None)
        if tip is not None:
            tip.destroy()
            self._tip = None

    def _show_compat(self, state=None, short="", long=""):
        """The compatibility badge next to the cover (state None hides it)."""
        self._compat_state, self._compat_long = state, long
        if not state:
            self.compat_lbl.pack_forget()
            return
        c = THEMES[self.theme]
        colour = {"ok": "#2fbf5a", "bad": "#e5484d", "maybe": "#d08a1c"}.get(state,
                                                                             c["muted"])
        mark = {"ok": "\u2714 ", "bad": "\u2716 ", "maybe": "~ "}.get(state, "? ")
        if short.startswith("Checking"):
            mark = ""
        self.compat_lbl.configure(text=mark + short.replace("\n", " "),
                                  fg="#ffffff" if state != "unknown"
                                  else c["fg"], bg=colour if state != "unknown" else c["card"],
                                  font=(self.font_semi, 9))
        self._place_compat()

    def _place_compat(self):
        if not getattr(self, "_compat_state", None) or not self.big_cover.winfo_ismapped():
            self.compat_lbl.pack_forget()
            return
        if not self.compat_lbl.winfo_ismapped():
            self.compat_lbl.pack(after=self.big_cover, pady=(int(4 * self.k), 0))

    def _rom_info(self, path):
        """compat.rom_info, cached per file (hashing a ROM takes a moment)."""
        cache = self.__dict__.setdefault("_rom_infos", {})
        try:
            key = (path, os.path.getsize(path), int(os.path.getmtime(path)))
        except OSError:
            return None
        if key not in cache:
            cache[key] = compat.rom_info(path, self.platform)
        return cache[key]

    def _update_compat(self, r, extra=""):
        """Check the selected result against the user's scanned ROM for this game, from
        its ROM info and description (rechecked once the full description has loaded)."""
        if not extra:
            self._show_compat(None)
        game = self.selected
        text = "\n".join(x for x in (r.get("full_desc"), r.get("rhn_full"), r.get("desc"),
                                      extra) if x)
        rominfo = r.get("rominfo") or ""
        if not game:
            return
        mine = self.owned().get((self.hack_of(game)[0] or game)["id"])
        if not mine or not os.path.exists(mine):
            return
        if not extra:
            self._show_compat("unknown", "Checking…", "Checking which ROM version this "
                              "patch needs…")
        waiting = not extra and r.get("source") not in ("Romhacking.net",)

        def done(res):
            if self._current_result() is not r:
                return
            if waiting and res[0] == "unknown":
                return  # keep "Checking…" until the description has been read too
            self._show_compat(*res)
        self.run_bg(lambda: compat.check(compat.requirements(text, rominfo),
                                         self._rom_info(mine)),
                    done, lambda m: None)

    def _check_download(self, patch, folder, rom):
        """After downloading: the patch's own record of the ROM it expects (BPS / UPS),
        else its readme. Updates the badge; returns False if the user stops."""
        req = {"crc": set(), "sha1": set(), "md5": set(), "names": [], "region": None,
               "version": None}
        src = compat.patch_source_crc(patch)
        if src:
            req["crc"] = {src}
        else:
            text = ""
            for root, _d, files in os.walk(folder):
                for f in files:
                    if f.lower().endswith((".txt", ".nfo", ".md")):
                        try:
                            with open(os.path.join(root, f), encoding="utf-8",
                                      errors="replace") as fh:
                                text += fh.read(20000) + "\n"
                        except OSError:
                            pass
            req = compat.requirements(text)
        try:
            info = self._rom_info(rom)
        except Exception:
            return True
        if not info or not (req["crc"] or req["sha1"] or req["region"] or req["version"]):
            return True
        state, short, long = compat.check(req, info)
        if src and state == "bad":
            long = (f"This patch was made from a ROM with CRC32 {src.upper()}; your ROM "
                    f"({info.get('name') or os.path.basename(rom)}) is different, so the "
                    "patched game may not work.")
        self._show_compat(state, short, long)
        if state == "bad":
            return messagebox.askyesno("Different ROM version", long + "\n\nApply the "
                                       "patch anyway?", icon="warning")
        return True

    def _place_info_btn(self):
        """Keep the small filter button level with the top of the cover, at the left
        edge of the panel's content."""
        self.update_idletasks()
        y = self.big_cover.winfo_y() if self.big_cover.winfo_ismapped() else \
            self.game_title.winfo_y()
        self.info_btn.place_configure(x=0, y=max(0, y))

    def _set_info(self, key, var):
        self.settings[key] = var.get()
        save_settings(self.settings)
        if self.selected:
            self._show_title(self.selected)

    def _show_title(self, game):
        """Title (+ year), and for games you own the region / version of your ROM."""
        show_year = self.show_date.get() and game.get("year")
        self.game_title.configure(text=game["name"] + (f"  ({game['year']})" if show_year
                                                       else ""))
        bits = []
        mine = self.owned().get(game["id"])
        if mine:
            region, version = rom_region_version(mine)
            ident = self._rom_ids.get(mine, {})
            if ident.get("name"):  # identified by checksum: exact region and version
                region, version = ident.get("region") or region, ident.get("version", "")
            if self.show_region.get() and region:
                bits.append(f"Region: {region}")
            if self.show_version.get() and version:
                bits.append(f"Version: {version}")
            if not bits and (self.show_region.get() or self.show_version.get()):
                bits.append("Your ROM")
        self.game_info.configure(text="   ·   ".join(bits))

    def select_game(self, game):
        self.selected = game
        self._restyle_tiles()
        self._show_title(game)
        if sources.RA_KEY and self.platform not in self._ra and \
                not getattr(self, "_ra_error", None):
            self._load_ra()
        self._update_ra_button()
        self.find_btn.configure(state="normal")
        self.local_btn.configure(state="normal")
        self.results = []
        self.show_results()
        self.big_cover.configure(image="")

        def load():
            p = covers.cover_path(game)
            return Image.open(p).convert("RGB") if p else None

        def show(im):
            if im is not None and self.selected is game:
                self._cover_src = im
                self._fit_cover()
        self._cover_src = None
        self.run_bg(load, show, fail=lambda m: None)

    def _fit_cover(self, _event=None):
        """Size the big cover to the panel's height, so Results, Description and the
        buttons keep their room; on short windows the cover shrinks, then hides."""
        im = getattr(self, "_cover_src", None)
        if im is None:
            return
        # height the rest of the panel needs at minimum (title, buttons, boxes...)
        needed = int(470 * self.k)
        h = min(int(230 * self.k), self.side.winfo_height() - needed)
        if h < int(70 * self.k):
            self.big_cover.configure(image="")
            self.big_cover.pack_forget()
            return
        if not self.big_cover.winfo_ismapped():
            self.big_cover.pack(before=self.game_title)
        if getattr(self, "_cover_h", None) == (id(im), h):
            return
        self._cover_h = (id(im), h)
        thumb = im.copy()
        thumb.thumbnail((int(300 * self.k), h))
        photo = ImageTk.PhotoImage(thumb)
        self.images[("big", "current")] = photo
        self.big_cover.configure(image=photo)

    def _schedule_fit_cover(self, _event=None):
        if getattr(self, "_fit_job", None):
            self.after_cancel(self._fit_job)
        self._fit_job = self.after(120, self._fit_cover)

    def find_patches(self):
        game = self.selected
        self.find_btn.configure(state="disabled", text="LOADING…")
        self.status.configure(text=f"Searching for patches for {game['name']}…")

        def fail(msg):
            self.find_btn.configure(state="normal", text="FIND PATCHES")
            self._error(msg)

        def done(res):
            results, errors = res
            self.find_btn.configure(state="normal", text="FIND PATCHES")
            if self.selected is not game:
                return
            results = self._drop_other_games(results, game)
            own = game.get("own_result")  # a tile made from a source: its own entry first
            if own and not any(r.get("url") == own["url"] for r in results):
                results.insert(0, dict(own))
            self.results = results + sources.browser_links(game["name"], self.platform)
            self.show_results()
            msg = f"{len(results)} results"
            if errors:
                msg += "  ·  " + "; ".join(errors)
            self.status.configure(text=msg)

        platform = self.platform
        base, hack = self.hack_of(game)
        if hack:
            self.status.configure(text=f"Searching for {hack} ({base['name']} hack)…")

            def work():
                found, errors = sources.search_hack(base["name"], hack, platform)
                if found:
                    return found, errors
                # nothing for this hack: fall back to everything for the title
                found, errors = sources.search_all(game["name"], platform)
                return found, errors + [f"nothing specific to '{hack}' found, "
                                        "showing all results"]
            self.run_bg(work, done, fail)
        else:
            self.run_bg(lambda: sources.search_all(game["name"], platform), done, fail)

    def _build_hack_index(self):
        """norm(title) -> game, for titles that can be the base of a hack."""
        self._bases = {}
        for g in self.games:
            n = library.norm(g["name"])
            if is_hack(g) or "(" in g["name"] or len(n) < 6:
                continue  # only real games can be a base; skip "SUPER (Wagokoro)" etc.
            cur = self._bases.get(n)
            if cur is None or len(g["name"]) < len(cur["name"]):
                self._bases[n] = g
        self._hack_cache = {}

    def _open_and_find(self, game):
        """Double-click a cover: select it and run FIND PATCHES."""
        if self.selected is not game:
            self.select_game(game)
        if str(self.find_btn.cget("state")) != "disabled":
            self.find_patches()

    def _drop_other_games(self, results, game):
        """Drop results whose title names a different game of this console more fully -
        "Contra Force Spanish Translation" belongs to Contra Force, not Contra."""
        me = library.norm((self.hack_of(game)[0] or game)["name"])
        if not hasattr(self, "_name_index") or self._name_index[0] is not self.games:
            names = sorted({library.norm(g["name"]) for g in self.games if not is_hack(g)},
                           key=len, reverse=True)
            self._name_index = (self.games, [n for n in names if len(n) >= 4])
        keep = []
        for r in results:
            if r["source"] in ("Romhacking.net", "Website", "RetroAchievements", "romhack.ing"):
                keep.append(r)  # these already name their exact game
                continue
            t = library.norm(sources._result_name(r))
            # e.g. "Contra Force" or "Super Contra X" for Contra
            longer = next((n for n in self._name_index[1] if len(n) > len(me) and
                           me in n and t.startswith(n)), None)
            if longer is None:
                keep.append(r)
        return keep

    def hack_of(self, game):
        """If game is a hack entry in the list (e.g. '... Link to the Past Redux' or
        'Chrono Trigger+'), return (base_game, hack_part). Otherwise (None, None)."""
        if not hasattr(self, "_bases"):
            self._build_hack_index()
        if game["id"] in self._hack_cache:
            return self._hack_cache[game["id"]]
        res = self._find_base(game)
        self._hack_cache[game["id"]] = res
        return res

    def _base_from_rhn(self, game):
        """Hacks whose name doesn't include the game (e.g. 'MaternalBound Redux'):
        romhacking.net's index says which game they're built on."""
        if not hasattr(self, "_rhn_game"):
            self._rhn_game = {library.norm(e["title"]): library.norm(e["game"])
                              for e in sources.rhn_index(self.platform)
                              if e["kind"] == "hack"}
        base_norm = self._rhn_game.get(library.norm(game["name"]))
        base = self._bases.get(base_norm) if base_norm else None
        return (base, game["name"]) if base else (None, None)

    def _find_base(self, game):
        if game.get("base_id"):  # romhacking.net tile: we know its base game
            base = next((g for g in self.games if g["id"] == game["base_id"]), None)
            return (base, game["name"]) if base else (None, None)
        if not is_hack(game):
            return None, None
        me = library.norm(game["name"])
        same = self._bases.get(me)
        if same is not None and same is not game and len(game["name"]) > len(same["name"]):
            # same letters, extra symbols: "Chrono Trigger+" is a hack of "Chrono Trigger"
            extra = game["name"][len(same["name"]):].strip() if \
                game["name"].startswith(same["name"]) else ""
            return same, extra or game["name"]
        best = None
        for L in range(len(me) - 1, 5, -1):  # longest base title first
            g = self._bases.get(me[:L])
            if g is not None and g is not game:
                best = g
                break
        if not best:
            return self._base_from_rhn(game)
        # cut the base title off the front of the display name
        name, i = game["name"], 0
        target = library.norm(best["name"])
        for j in range(1, len(name) + 1):
            if library.norm(name[:j]) == target:
                i = j
        if i < len(name) and name[i - 1].isalnum() and name[i].isalnum():
            return None, None  # cut mid-word ("Super C" + "astlevania"): not a real base
        hack = name[i:].strip(" :-–—,")
        if not hack or re.match(r"(\d|(II|III|IV|V|VI|VII|VIII|IX|X|Jr)\b)", hack):
            return None, None  # a sequel like "Donkey Kong Country 2", not a hack
        return best, hack

    def _current_result(self):
        sel = self.res_tree.selection()
        return self.shown_results[int(sel[0])] if sel else None

    def open_result_page(self):
        r = self._current_result()
        if r:
            open_in_browser(r["url"])

    def result_selected(self):
        r = self._current_result()
        if not r:
            return
        self._update_compat(r)
        if r.get("rhn_url"):  # romhacking.net's description replaces the generic one
            self.res_desc.configure(text="Loading description…")
        else:
            self.res_desc.configure(text=(r["desc"] or "") + "\n\nLoading the full description…")
        self.res_desc.yview_moveto(0)

        def show_desc(res):
            text, links = res
            if self._current_result() is not r:
                return
            self.res_desc.configure(text=text)
            self._update_compat(r, text)
            self.res_desc.yview_moveto(0)
            for link in links:  # e.g. the hack's romhack.ing page, found on its page
                if any(x["url"] == link["url"] for x in self.results):
                    continue
                self.results.append(link)
                if RESULT_CATEGORY[self.category] == link["category"]:
                    self.shown_results.append(link)
                    self.res_tree.insert("", "end", iid=str(len(self.shown_results) - 1),
                                         text=link["title"], values=(link["source"],))
        self.run_bg(lambda: sources.fetch_description(r), show_desc,
                    fail=lambda m: (self.res_desc.configure(text=r["desc"] or ""),
                                    self._update_compat(r, r.get("desc") or " ")))
        self.file_tree.delete(*self.file_tree.get_children())
        self.files = []
        self.file_tree.insert("", "end", iid="loading", text="Loading patches…", values=("",))
        self.apply_btn.configure(state="disabled", text="Loading…")
        self.status.configure(text="Listing patch files…  (double-click a result to open "
                                   "its page, Ctrl+C copies its name and link)")

        def done(files):
            if self._current_result() is not r:
                return
            self.file_tree.delete(*self.file_tree.get_children())
            self.files = files
            for i, f in enumerate(files):
                kb = f.get("size", 0) / 1024
                size = "" if not kb else f"{kb / 1024:.1f} MB" if kb > 1000 else f"{kb:.0f} KB"
                self.file_tree.insert("", "end", iid=str(i), text=f["name"], values=(size,))
            if files:
                self.file_tree.selection_set("0")
            else:
                self.apply_btn.configure(text="Download & apply to my ROM…")
            self.status.configure(text=f"{len(files)} patch files"
                                  if files else "No patch files found in this result")
        if r["source"] == "Romhacking.net" and self.category != "archive" and \
                not r.get("live"):
            # the backup's files are in the 2024 Archive tab; here, the live page (newer)
            done([{"name": "Open on romhacking.net", "url": r["url"], "browser": True}])
            return
        def fail(msg):
            if self._current_result() is r:
                self.file_tree.delete(*self.file_tree.get_children())
                self.file_tree.insert("", "end", iid="loading",
                                      text="Couldn't load the patch files", values=("",))
                self.apply_btn.configure(text="Download & apply to my ROM…")
                self.status.configure(text="Error: " + msg)
        self.run_bg(lambda: sources.list_files(r), done, fail)

    def _choose_rom(self, game):
        """The user's ROM for this game (or the game a hack is built on). With
        'Use my scanned ROM automatically' on, an owned game's ROM is used without
        asking; otherwise the picker opens, pointed at it."""
        mine = self.owned().get((self.hack_of(game)[0] or game)["id"])
        if mine and self.settings.get("auto_rom", True) and os.path.exists(mine):
            self.status.configure(text=f"Using your ROM: {mine}")
            return mine
        return filedialog.askopenfilename(
            title=f"Choose your {self.console()['name']} ROM",
            initialdir=os.path.dirname(mine) if mine else self.settings.get("rom_dir"),
            initialfile=os.path.basename(mine) if mine else "",
            filetypes=[(f"{self.console()['label']} ROMs", self.console()["patterns"] + " *.zip *.7z *.rar"),
                       ("All files", "*.*")])

    def _selected_file(self):
        sel = self.file_tree.selection()
        if not sel or not sel[0].isdigit():  # nothing, or the "Loading patches…" row
            return None
        return self.files[int(sel[0])]

    def file_selected(self):
        f = self._selected_file()
        if f:
            self.apply_btn.configure(state="normal", text="Open download page in browser"
                                     if f.get("browser") else "Download & apply to my ROM…")
            if f.get("browser") and "zeldix" in f.get("url", ""):
                self.status.configure(text="Opens the Zeldix thread in your browser - some "
                                           "download links there need a free Zeldix login")
            elif f.get("browser") and "romhacking.net" in f.get("url", "") and                     not archive_org.CREDS.get("access"):
                self.status.configure(text="Tip: sign in to archive.org in Settings > Accounts "
                                           "to download romhacking.net patches directly")
            elif f.get("rhn_member"):
                size = f" ({f['size'] / 1024:.0f} KB)" if f.get("size") else ""
                self.status.configure(text=f"{f['full_name']}{size}  ·  from romhacking.net's "
                                           "file backup on archive.org")
            elif f.get("full_name"):
                what = {"music": "MSU-1 music (PCM tracks) - goes into your MSU-1 folder",
                        "patch": "MSU-1 patch - makes the MSU-1 folder with your patched ROM",
                        "pack": "MSU-1 pack"}.get(f.get("kind"), "File")
                size = f" ({f['size'] / 1048576:.0f} MB)" if f.get("size") else ""
                self.status.configure(text=f"{f['full_name']}{size}  ·  {what}")

    def download_apply(self):
        finfo = self._selected_file()
        if not finfo:
            return
        if finfo.get("browser"):
            open_in_browser(finfo["url"])
            result = self._current_result() or {}
            if "hackdex.app" in finfo["url"]:
                # Hackdex patches in the browser and saves the patched ROM: file it
                self.watch_downloads(self.selected, roms=True,
                                     hack=result.get("title") or self.selected["name"])
                return
            if result.get("source") in ("Romhacking.net", "RomhackPlaza", "Zeldix", "GitHub",
                                        "archive.org", "romhack.ing", "PokéCommunity",
                                        "PokéHarbor", "Hackdex"):
                self.watch_downloads(self.selected)
            return
        game = self.selected
        rom = self._choose_rom(game)
        if not rom:
            return
        original = rom
        self.settings["rom_dir"] = os.path.dirname(rom)
        save_settings(self.settings)
        try:
            rom = library.unpack_rom(rom)
        except (StopIteration, OSError, ValueError):
            self._error("That archive doesn't contain a ROM for this console.")
            return
        self.status.configure(text=f"Downloading {finfo['name']}…")
        ext = os.path.splitext(rom)[1]
        if ext.lower() not in self.console()["exts"]:
            ext = self.console()["out"]
        result = self._current_result()

        # an MSU-1 pack's folder is named after the patch (the result it came from)
        pack_label = sources._result_name(result) if result else game["name"]

        def downloaded(res):
            patches, folder = res
            msu = sources.find_msu_name(folder) if finfo.get("msu") else None
            if not patches and not msu:
                self._error("No IPS/UPS/BPS/xdelta patch was found in that download.")
                return
            if msu:
                # MSU-1: the ROM must sit next to the audio tracks with the same name
                msu_dir, msu_base = msu
                out = os.path.join(msu_dir, msu_base + ".sfc")
                if not patches:
                    # music only: add it to the game's existing MSU-1 folder if there is one
                    known = self.settings.get("msu_folders", {}).get(game["name"])
                    if known and os.path.isfile(known):
                        pcm = [os.path.join(r, f) for r, _d, fs in os.walk(msu_dir)
                               for f in fs if f.lower().endswith(".pcm")]
                        n = msu_audio.install({"kind": "loose", "path": pcm},
                                              os.path.dirname(known), known)
                        if n:
                            self._audio_watch = None
                            self._bar_done()
                            msg = f"Added {n} MSU-1 music tracks to your MSU-1 folder."
                            self.status.configure(text=msg + "  →  " + os.path.dirname(known))
                            if self.settings.get("done_popup", True) and messagebox.askyesno(
                                    "Done", f"{msg}\n\n{os.path.dirname(known)}\n\n"
                                    "Open the folder?"):
                                os.startfile(os.path.dirname(known))
                            return
                    shutil.copyfile(rom, out)
                    self._finish("Copied your ROM into the MSU-1 pack (no patch needed).",
                                 out, msu=True, original=original, label=pack_label)
                    return
            patch = patches[0] if len(patches) == 1 else self._pick_patch(patches)
            if not patch:
                return
            msu_result = finfo.get("msu") or (result or {}).get("category") == "msu1"
            if not msu:
                # new file named after the hack, next to the original - never over it
                name = hack_name(patch, result, game["name"],
                                 self.settings.get("name_patch_only", True))
                where = os.path.dirname(original)
                if msu_result:  # MSU-1 always gets a folder, even if it's only a patch
                    where = self._new_msu_folder(original, pack_label)
                out = unique_path(os.path.join(where, name + ext), avoid=original)
            self._pending_install = (result, finfo, game, self.platform)
            if not self._check_download(patch, folder, rom):
                self.status.configure(text="Not applied - the patch is for a different ROM "
                                           "version.")
                return
            if same_file(out, original):
                self._error("Refusing to overwrite your original ROM.")
                return
            try:
                msg = apply_patch_file(rom, patch, out)
            except PatchError as e:
                self._error(str(e))
                return
            self._record_install()
            if msu:
                self._finish(msg, out, msu=True, original=original, label=pack_label)
            elif msu_result:  # already in its own folder: don't move it, don't zip it
                self._finish(msg, out, msu=True)
            else:
                self._finish(msg, out, original=original, label=pack_label)

        dl_dir = self.settings.get("download_dir") or None

        def downloaded_then_tidy(res):
            downloaded(res)
            self._tidy_download(res[1], msu=finfo.get("msu", False))
        self.run_bg(lambda: sources.download(finfo, game["name"], dl_dir, self.set_status),
                    downloaded_then_tidy)

    # ---------------- Downloads folder watcher ----------------
    PARTIAL = (".crdownload", ".part", ".tmp", ".opdownload", ".download", ".partial")
    WANTED = (".ips", ".bps", ".ups", ".xdelta", ".vcdiff", ".zip", ".7z", ".rar")

    def downloads_dir(self):
        return self.settings.get("downloads_dir") or os.path.join(os.path.expanduser("~"),
                                                                  "Downloads")

    def watch_downloads(self, game, roms=False, hack=None):
        """After opening a download page in the browser, wait for the patch to land in
        the Downloads folder, then apply it automatically. roms=True (Hackdex, which
        patches in the browser) also takes a patched ROM and files it."""
        folder = self.downloads_dir()
        if not os.path.isdir(folder):
            return
        self.stop_watching()
        self._watch = {"game": game, "folder": folder, "start": time.time() - 2,
                       "sizes": {}, "job": None, "roms": roms, "hack": hack}
        self.status.configure(text=(
            "Patch it on Hackdex with your ROM - Vibe Patcher files the patched ROM when it "
            "lands in your Downloads folder…") if roms else
            "Waiting for the patch to arrive in your Downloads folder… (click Download on "
            "the web page)")
        self._watch_tick()

    def stop_watching(self, msg=None):
        w = getattr(self, "_watch", None)
        if w and w["job"]:
            self.after_cancel(w["job"])
        self._watch = None
        if msg:
            self.status.configure(text=msg)

    def _watch_tick(self):
        w = self._watch
        if not w:
            return
        if time.time() - w["start"] > 20 * 60:
            self.stop_watching("Stopped waiting for a download (20 minutes passed).")
            return
        try:
            names = os.listdir(w["folder"])
        except OSError:
            names = []
        rom_exts = tuple(self.console()["exts"]) + (".gba", ".nds", ".gb", ".gbc") \
            if w.get("roms") else ()
        for n in names:
            low = n.lower()
            if not low.endswith(self.WANTED + rom_exts) or low.endswith(self.PARTIAL):
                continue
            p = os.path.join(w["folder"], n)
            try:
                st = os.stat(p)
            except OSError:
                continue
            if st.st_mtime < w["start"] or st.st_size == 0:
                continue
            # wait until the size stops changing, so the browser has finished writing
            if w["sizes"].get(p) == st.st_size:
                game = w["game"]
                if rom_exts and low.endswith(rom_exts):
                    self.stop_watching(f"Found {n} - filing it…")
                    self._file_patched_rom(p, game, w.get("hack") or game["name"])
                    return
                self.stop_watching(f"Found {n} - applying it…")
                self.apply_patch_path(p, game)
                return
            w["sizes"][p] = st.st_size
        w["job"] = self.after(1000, self._watch_tick)

    def _file_patched_rom(self, path, game, hack):
        """A ROM the user patched on a website (Hackdex): move it next to their original
        ROM, named after the hack, and treat it like one Vibe Patcher patched."""
        mine = self.owned().get((self.hack_of(game)[0] or game)["id"]) if game else None
        where = os.path.dirname(mine) if mine and os.path.exists(mine) else \
            self.settings.get("rom_dir") or os.path.dirname(path)
        name = re.sub(r'[<>:"/\\|?*]', "", hack).strip() or os.path.splitext(
            os.path.basename(path))[0]
        out = unique_path(os.path.join(where, name + os.path.splitext(path)[1].lower()),
                          avoid=mine or "")
        try:
            shutil.move(path, out)
        except OSError as e:
            self._error(f"Couldn't move the patched ROM: {e}")
            return
        self._finish(f"Filed your patched ROM from Hackdex ({os.path.basename(path)}).", out)

    def apply_local(self):
        """For sites we can only open in the browser: apply a patch file (or a .zip/.7z
        holding one) that the user downloaded."""
        patch = filedialog.askopenfilename(
            title="Choose the patch you downloaded", initialdir=self.downloads_dir(),
            filetypes=[("Patches", "*.ips *.bps *.ups *.xdelta *.vcdiff *.zip *.7z *.rar"),
                       ("All files", "*.*")])
        if patch:
            self.apply_patch_path(patch, self.selected)

    def apply_patch_path(self, patch, game):
        """Apply a patch file (or a .zip/.7z holding one) to the user's ROM."""
        try:
            patches, _folder = sources.extract_patches(patch)
        except Exception as e:
            self._error(str(e))
            return
        if not patches:
            self._error("No IPS/UPS/BPS/xdelta patch was found in that file.")
            return
        patch = patches[0] if len(patches) == 1 else self._pick_patch(patches)
        if not patch:
            return
        rom = self._choose_rom(game)
        if not rom:
            return
        original = rom
        try:
            rom = library.unpack_rom(rom)
        except (StopIteration, OSError, ValueError):
            self._error("That archive doesn't contain a ROM for this console.")
            return
        ext = os.path.splitext(rom)[1]
        if ext.lower() not in self.console()["exts"]:
            ext = self.console()["out"]
        name = hack_name(patch, {"title": game["name"]}, (self.hack_of(game)[0] or game)["name"],
                         self.settings.get("name_patch_only", True))
        result = self._current_result() or {}
        msu_result = result.get("category") == "msu1" or self.category == "msu1"
        where = os.path.dirname(original)
        if msu_result:  # MSU-1 always gets its own folder
            where = self._new_msu_folder(original, sources._result_name(result) if result
                                         else game["name"])
        out = unique_path(os.path.join(where, name + ext), avoid=original)
        try:
            msg = apply_patch_file(rom, patch, out)
        except PatchError as e:
            self._error(str(e))
            return
        self._finish(msg, out, msu=msu_result, original=None if msu_result else original)

    def _tidy_download(self, folder, msu=False):
        """With 'Save ROM patches' off, remove what was downloaded once patching is done.
        Only ever touches the patch download folder."""
        if self.settings.get("keep_patches", False) or not folder:
            return
        if msu and not self.settings.get("msu_folder", True):
            return  # the patched ROM lives inside the pack's download folder
        root = os.path.realpath(self.settings.get("download_dir") or DOWNLOAD_DIR)
        target = os.path.realpath(folder)
        if not target.startswith(root + os.sep):
            return
        # the game's folder (patches/<game>) holding the download and its extracted copy
        rel = os.path.relpath(target, root).split(os.sep)[0]
        shutil.rmtree(os.path.join(root, rel), ignore_errors=True)

    def _add_history(self, out):
        """Remember every patched ROM for Settings > History (newest first, max 500)."""
        import datetime
        game = self.selected or {}
        hist = self.settings.get("history", [])
        hist.insert(0, {"date": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
                        "console": self.console()["label"], "game": game.get("name", ""),
                        "rom": os.path.basename(out), "path": out})
        self.settings["history"] = hist[:500]
        save_settings(self.settings)

    # ---------- MSU-1 music (.pcm) ----------
    def _audio_folders(self):
        return [self.downloads_dir(), self.settings.get("download_dir") or DOWNLOAD_DIR]

    def _add_msu_audio(self, rom_out, label):
        """After an MSU-1 patch: install the music pack from the Downloads folder, or
        watch for it. Returns a line for the result message."""
        folder = os.path.dirname(rom_out)
        try:
            if msu_audio.has_audio(folder):
                return ""  # the pack came with its music
            hints = [label, (self.selected or {}).get("name", ""), os.path.basename(rom_out)]
            audio = msu_audio.find_audio(self._audio_folders(), hints)
            if audio:
                n = msu_audio.install(audio, folder, rom_out)
                if n:
                    src = audio["path"] if audio["kind"] == "archive" else "Downloads"
                    return f"\nAdded {n} MSU-1 music tracks from {os.path.basename(src)}."
        except Exception as e:
            log.exception("Adding MSU-1 music failed")
            return f"\n(Couldn't add the MSU-1 music: {e})"
        self._watch_audio(folder, rom_out, hints)
        return ("\nNo MSU-1 music (.pcm) found yet - download the music pack and Vibe "
                "Patcher will add it to the folder (watching Downloads for 20 minutes).")

    def _watch_audio(self, folder, rom_out, hints):
        self._audio_watch = {"folder": folder, "rom": rom_out, "hints": hints,
                             "start": time.time() - 5, "sizes": {}}
        self.after(2000, self._audio_tick)

    def _audio_tick(self):
        w = getattr(self, "_audio_watch", None)
        if not w or not os.path.isdir(w["folder"]):
            return
        if time.time() - w["start"] > 20 * 60:
            self._audio_watch = None
            return
        try:
            audio = msu_audio.find_audio(self._audio_folders(), w["hints"], since=w["start"])
        except Exception:
            audio = None
        if audio:
            paths = audio["path"] if audio["kind"] == "loose" else [audio["path"]]
            sizes = tuple(os.path.getsize(p) for p in paths if os.path.exists(p))
            key = tuple(paths)
            if w["sizes"].get(key) == sizes:  # finished downloading
                self._audio_watch = None
                folder, rom = w["folder"], w["rom"]

                def work():
                    return msu_audio.install(audio, folder, rom)

                def done(n):
                    self.status.configure(text=f"Added {n} MSU-1 music tracks to "
                                               f"{os.path.basename(folder)} - ready to play!")
                self.status.configure(text="Adding the MSU-1 music…")
                self.run_bg(work, done)
                return
            w["sizes"][key] = sizes
        self.after(2000, self._audio_tick)

    def _new_msu_folder(self, original, label):
        """A new folder next to the user's ROM, named after the MSU-1 patch."""
        label = (label or "MSU-1").replace("-", " ").replace("_", " ").strip()
        if not re.search(r"msu", label, re.I):
            label += " (MSU-1)"
        name = re.sub(r'[<>:"/\\|?*]', "", label).strip()
        dest = os.path.join(os.path.dirname(original), name)
        n = 1
        while os.path.exists(dest):
            n += 1
            dest = os.path.join(os.path.dirname(original), f"{name} ({n})")
        os.makedirs(dest)
        return dest

    def _deliver(self, out, msu, original, label):
        """Apply the output settings: MSU-1 packs into their own folder, other patched
        ROMs optionally zipped. Returns the final path."""
        if msu and self.settings.get("msu_folder", True) and original:
            # move the whole pack (ROM + .msu + .pcm tracks) next to the user's ROM
            pack = os.path.dirname(out)
            label = label.replace("-", " ").replace("_", " ").strip() or "MSU-1 pack"
            if not re.search(r"msu", label, re.I):
                label += " (MSU-1)"
            name = re.sub(r'[<>:"/\\|?*]', "", label).strip()
            dest = os.path.join(os.path.dirname(original), name)
            n = 1
            while os.path.exists(dest):
                n += 1
                dest = os.path.join(os.path.dirname(original), f"{name} ({n})")
            shutil.move(pack, dest)
            return os.path.join(dest, os.path.basename(out))
        if not msu and self.settings.get("zip_output", False):
            import zipfile
            zpath = unique_path(os.path.splitext(out)[0] + ".zip", avoid=original or "")
            # plain Deflate at the default level: opens everywhere, rarely trips anything
            with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
                z.write(out, arcname=os.path.basename(out))
            with zipfile.ZipFile(zpath) as z:
                if z.testzip() is not None:
                    raise RuntimeError(f"The zip didn't verify; kept the unzipped ROM: {out}")
            os.remove(out)
            return zpath
        return out

    def _finish(self, msg, out, msu=False, original=None, label=""):
        try:
            out = self._deliver(out, msu, original, label)
        except Exception as e:
            log.exception("Saving the output failed")
            msg += f"\n(Couldn't apply the output settings: {e})"
        msg += "\nYour original ROM was not changed."
        if msu:
            msg += self._add_msu_audio(out, label)
            if self.selected and out.lower().endswith((".sfc", ".smc")):
                # remember it, so a music pack downloaded later goes in the same folder
                self.settings.setdefault("msu_folders", {})[self.selected["name"]] = out
                save_settings(self.settings)
        self._add_history(out)
        self._bar_done()
        self.status.configure(text=msg.replace("\n", "  ") + "  →  " + out)
        extra = ("\n\nMSU-1: keep the ROM in this folder with the audio tracks, and load it "
                 "in an MSU-1 capable emulator (e.g. snes9x, bsnes) or flash cart.") if msu else ""
        if not self.settings.get("done_popup", True):
            return  # the status bar already says where it went
        if messagebox.askyesno("Done", f"{msg}\n\nSaved to:\n{out}{extra}\n\nOpen the folder?"):
            subprocess.Popen(["explorer", "/select,", os.path.normpath(out)])

    def toggle_side(self):
        hidden = not self.side.winfo_ismapped()
        if hidden:
            self.side.pack(side="right", fill="y", before=self.side_strip)
        else:
            self.side.pack_forget()
        self.side_arrow.configure(text="▶" if hidden else "◀")
        self.settings["side_hidden"] = not hidden
        save_settings(self.settings)

    def sort_alpha(self):
        # clicking Alphabetical again flips between A-Z and Z-A
        self.set_sort("za" if self.sort_var.get() == "az" else "az")

    def set_sort(self, key):
        self.sort_var.set(key)
        self.settings["sort"] = key
        save_settings(self.settings)
        self._sort_label()
        self.apply_filter()

    def _sort_label(self):
        cur = self.sort_var.get()
        mark = lambda on: "✓ " if on else "    "
        order = "Z-A" if cur == "za" else "A-Z"
        self.sort_menu.entryconfigure(0, label=f"{mark(cur in ('az', 'za'))}Alphabetical "
                                               f"({order})  - click to flip")
        for i, (key, label) in enumerate(SORTS, 1):
            self.sort_menu.entryconfigure(i, label=mark(cur == key) + label)
        names = {"az": "A-Z", "za": "Z-A", **dict(SORTS)}
        self.sort_btn.configure(text=f"⇅ Sort: {names.get(cur, 'A-Z')}")

    def set_hacks_only(self):
        self.settings["hacks_only"] = self.hacks_only.get()
        save_settings(self.settings)
        self.apply_filter()

    def set_direct_only(self):
        self.settings["direct_only"] = self.direct_only.get()
        save_settings(self.settings)
        self.apply_filter()

    # sources Vibe Patcher downloads from itself (no browser needed)
    DIRECT_SOURCES = {"RAPatches", "RomhackPlaza", "Zeldix", "GitHub"}

    def _direct_names(self):
        """(names, sorted title prefixes) of games / hacks with a patch that downloads
        in-app: RAPatches, RomhackPlaza, Zeldix (SNES MSU-1), the 2024 Archive (with an
        archive.org sign-in) and Hackdex hacks whose own GitHub repo has releases."""
        import rhn_db
        # the archive downloads in-app only on its own tab (elsewhere romhacking.net opens
        # in the browser)
        signed = bool(archive_org.CREDS.get("access") and rhn_db.load()) and \
            self.category == "archive"
        key = (self.platform, signed)
        cache = self.__dict__.setdefault("_direct_cache", {})
        if key in cache:
            return cache[key]
        if not getattr(self, "_lists_ready", False):
            return set(), []  # still downloading in the background: refiltered when done
        n = library.norm
        names, prefixes = set(), []
        try:
            console = sources.RAP_CONSOLE.get(self.platform)
            for e in sources.rapatches_index():
                if e["console"] != console:
                    continue
                words = e["name"].split("-")
                if e["folder"]:
                    names.add(n(sources._rap_game_folder(e["folder"])))
                names.add(n(sources._uncamel(words[0])))
                names.add(n(sources._uncamel("-".join(words[1:]) if len(words) > 1
                                             else e["name"])))
        except Exception:
            pass
        plats = {"gb": {"gb", "gbc"}, "gbc": {"gb", "gbc"}}.get(self.platform, {self.platform})
        try:
            for e in sources.plaza_index():
                if e.get("platform") in plats:
                    names.add(n(e["title"]))
                    prefixes.append(n(e["title"]))
        except Exception:
            pass
        if signed:
            for e in sources.rhn_index(self.platform):
                names.update({n(e["game"]), n(e["title"])})
        if self.platform == "snes":
            try:
                for title in sources._zeldix_index():
                    names.add(n(title))
                    prefixes.append(n(title))
            except Exception:
                pass
        try:
            for e in sources.hackdex_index():
                if e.get("gh_direct") and e.get("platform") in plats:
                    names.update({n(e["title"]), n(e["title"]).replace("pokemon", "")})
        except Exception:
            pass
        names.discard("")
        cache[key] = (names, sorted(set(prefixes)))
        return cache[key]

    def _has_direct(self, game):
        """Does this game (or hack) have a patch Vibe Patcher can download itself?"""
        own = game.get("own_result") or {}
        if own.get("source") in self.DIRECT_SOURCES or \
                (own.get("source") == "Romhacking.net" and self.category == "archive" and
                 archive_org.CREDS.get("access")):
            return True
        if own.get("source") == "Hackdex":
            return any(e.get("gh_direct") and e["url"] == own.get("url")
                       for e in sources.hackdex_index())
        names, prefixes = self._direct_names()
        me = library.norm(game["name"])
        if me in names or me.replace("pokemon", "") in names:
            return True
        aliases = sources.GAME_ALIASES.get(self.platform, {}).get(sources._norm(game["name"]),
                                                                  set())
        if aliases & names:
            return True
        if len(me) >= 6:  # "Super Metroid" for RomhackPlaza's "Super Metroid Liberation"
            import bisect
            i = bisect.bisect_left(prefixes, me)
            if i < len(prefixes) and prefixes[i].startswith(me):
                return True
        return False

    def set_owned_only(self):
        self.settings["owned_only"] = self.owned_only.get()
        save_settings(self.settings)
        if self.owned_only.get() and not self.owned():
            messagebox.showinfo("No games scanned yet", "Add your ROM folder in "
                                "Settings → My games and click Scan first.")
        self.apply_filter()

    # ---------------- category tabs ----------------
    def set_category(self, key):
        self.category = key
        self._style_tabs()
        self.show_results()
        self.apply_filter()  # the tab also filters the game grid

    def _archive_state(self):
        """'signin' (no archive.org sign-in), 'loading' (reading the database) or 'ready'."""
        import rhn_db
        if not archive_org.CREDS.get("access"):
            return "signin"
        if rhn_db.load() is None:
            if not getattr(self, "_archive_loading", False):
                self._archive_loading = True
                self._build_archive_db()
            return "loading"
        return "ready"

    def _build_archive_db(self):
        import rhn_db
        self.status.configure(text="Loading romhacking.net's 2024 database from "
                                   "archive.org…")

        def fail(msg):
            self._archive_loading = False
            self._archive_error = msg
            self.status.configure(text="Error: " + msg)
            if self.category == "archive":
                self.apply_filter()

        def done(db):
            self._archive_loading = False
            self._archive_error = None
            sources._rhn.clear()
            n = sum(len(v) for v in db.values())
            self.status.configure(text=f"2024 Archive ready: {n:,} romhacking.net hacks and "
                                       "translations")
            self.apply_filter()
            if self.results:
                self.show_results()
        self.run_bg(lambda: rhn_db.build(archive_org.session()), done, fail)

    def _style_tabs(self):
        c = THEMES[self.theme]
        q = self.search_var.get().lower().strip()
        counts = {}
        if q and self.games:
            for key in self.console()["tabs"]:
                counts[key] = (len(self.filtered) if key == self.category
                               else len(self._games_for(key, q)))
        for key, label in CATEGORIES:
            on = key == self.category
            text = f"{label} ({counts[key]})" if key in counts else label
            self.tab_btns[key].configure(font=(self.font_semi, 10), text=text, bg=c["sel"] if on else c["panel"],
                                         fg=c["fg"] if on else c["muted"])

    def show_results(self):
        self._show_compat(None)
        want = RESULT_CATEGORY[self.category]
        if want == "archive":
            self.shown_results = [r for r in self.results if r["source"] == "Romhacking.net"
                                  and r.get("rhn_id")]
        else:
            self.shown_results = [r for r in self.results if r["category"] == want]
        if want == "translation":
            self.shown_results.sort(key=translation_rank)  # English first (stable)
        self.res_tree.delete(*self.res_tree.get_children())
        self.file_tree.delete(*self.file_tree.get_children())
        self.res_desc.configure(text="")
        self.apply_btn.configure(state="disabled")
        for i, r in enumerate(self.shown_results):
            self.res_tree.insert("", "end", iid=str(i), text=r["title"], values=(r["source"],))
        self._style_tabs()

    # ---------------- settings ----------------
    def open_settings(self, tab=None):
        c = THEMES[self.theme]
        win = tk.Toplevel(self)
        win.title("Settings")
        win.configure(bg=c["panel"])
        win.transient(self)
        win.resizable(True, True)
        win.minsize(int(480 * self.k), int(420 * self.k))
        outer = ttk.Frame(win, style="Panel.TFrame", padding=18)
        outer.pack(fill="both", expand=True)
        book = ttk.Notebook(outer, style="Settings.TNotebook")
        book.pack(fill="both", expand=True)
        pages = {}
        for name in ("General", "Appearance", "Accounts", "Folders", "History", "About"):
            pages[name] = ttk.Frame(book, style="Panel.TFrame", padding=(14, 6, 14, 14))
            book.add(pages[name], text=f"  {name}  ")
        tab = tab or self.settings.get("settings_tab", "General")
        book.select(pages.get(tab, pages["General"]))
        frm = None  # the page being filled; heading() and the widgets below use it

        def heading(text):
            ttk.Label(frm, text=text, style="Head.TLabel").pack(anchor="w", pady=(10, 4))

        frm = pages["Appearance"]
        heading("Theme")
        theme_var = tk.StringVar(value=self.theme)
        row = ttk.Frame(frm, style="Panel.TFrame")
        row.pack(anchor="w")
        for val, label in (("dark", "Dark"), ("light", "Light")):
            ttk.Radiobutton(row, text=label, value=val, variable=theme_var,
                            style="Panel.TRadiobutton").pack(side="left", padx=(0, 16))

        heading("Font")
        font_var = tk.StringVar(value=self.settings.get("font", "Rubik"))
        ttk.Combobox(frm, textvariable=font_var, values=list(FONTS), state="readonly",
                     width=18, style="Big.TCombobox",
                     font=(self.font_reg, 12)).pack(anchor="w", pady=(2, 0))

        frm = pages["Accounts"]
        heading("RetroAchievements Web API key (optional)")
        ttk.Label(frm, text="Powers the RetroAchievements tab. Free: log in at "
                            "retroachievements.org > Settings > Keys and copy the Web API key.",
                  style="PanelMuted.TLabel", wraplength=int(440 * self.k)).pack(anchor="w")
        ra_var = tk.StringVar(value=self.settings.get("ra_key", ""))
        ttk.Entry(frm, textvariable=ra_var, show="•", width=50).pack(anchor="w", pady=4)

        heading("archive.org account (optional)")
        ttk.Label(frm, text="Lets Vibe Patcher download romhacking.net patches straight from "
                            "romhacking.net's own file backup on archive.org, which needs a "
                            "free archive.org login. Your password isn't saved - only the "
                            "access keys archive.org gives back.",
                  style="PanelMuted.TLabel", wraplength=int(440 * self.k)).pack(anchor="w")
        ia_box = ttk.Frame(frm, style="Panel.TFrame")
        ia_box.pack(anchor="w", fill="x", pady=4)

        def ia_fill():
            for w in ia_box.winfo_children():
                w.destroy()
            creds = self.settings.get("archive_org", {})
            if creds.get("access"):
                ttk.Label(ia_box, text=f"Signed in as {creds.get('user', 'you')}",
                          style="Panel.TLabel").pack(side="left")

                def sign_out():
                    self.settings.pop("archive_org", None)
                    save_settings(self.settings)
                    archive_org.CREDS = {}
                    archive_org.reset()
                    ia_fill()
                ttk.Button(ia_box, text="Sign out", command=sign_out).pack(side="left",
                                                                           padx=(12, 0))
                return
            email_var, pw_var = tk.StringVar(), tk.StringVar()
            grid = ttk.Frame(ia_box, style="Panel.TFrame")
            grid.pack(anchor="w")
            ttk.Label(grid, text="Email", style="Panel.TLabel").grid(row=0, column=0, sticky="w")
            ttk.Entry(grid, textvariable=email_var, width=34).grid(row=0, column=1, padx=8,
                                                                   pady=2)
            ttk.Label(grid, text="Password", style="Panel.TLabel").grid(row=1, column=0,
                                                                        sticky="w")
            ttk.Entry(grid, textvariable=pw_var, show="•", width=34).grid(row=1, column=1,
                                                                          padx=8, pady=2)
            msg = ttk.Label(ia_box, text="", style="PanelMuted.TLabel",
                            wraplength=int(440 * self.k))

            def sign_in():
                email, pw = email_var.get().strip(), pw_var.get()
                pw_var.set("")
                if not email or not pw:
                    msg.configure(text="Enter your archive.org email and password.")
                    return
                msg.configure(text="Signing in…")

                def ok(creds):
                    self.settings["archive_org"] = creds
                    save_settings(self.settings)
                    archive_org.CREDS = creds
                    archive_org.reset()
                    if ia_box.winfo_exists():
                        ia_fill()
                self.run_bg(lambda: archive_org.login(email, pw), ok,
                            lambda m: msg.winfo_exists() and msg.configure(text=m))
            ttk.Button(grid, text="Sign in", command=sign_in).grid(row=1, column=2)
            msg.pack(anchor="w")
        ia_fill()

        heading("GitHub token (optional)")
        ttk.Label(frm, text="Raises GitHub's search limit. Create one at github.com > "
                            "Settings > Developer settings (no scopes needed).",
                  style="PanelMuted.TLabel", wraplength=int(440 * self.k)).pack(anchor="w")
        token_var = tk.StringVar(value=self.settings.get("github_token", ""))
        ttk.Entry(frm, textvariable=token_var, show="•", width=50).pack(anchor="w", pady=4)

        frm = pages["General"]
        heading("Patched ROMs")
        msu_var = tk.BooleanVar(value=self.settings.get("msu_folder", True))
        ttk.Checkbutton(frm, text="Put MSU-1 packs in their own folder, named after the "
                                  "MSU-1 patch", variable=msu_var,
                        style="Panel.TCheckbutton").pack(anchor="w")
        ttk.Label(frm, text="The patched ROM, .msu file and audio tracks go into one "
                            "folder.", style="PanelMuted.TLabel",
                  wraplength=int(440 * self.k)).pack(anchor="w", padx=(22, 0))
        name_var = tk.BooleanVar(value=self.settings.get("name_patch_only", True))
        ttk.Checkbutton(frm, text="Name patched ROMs after the patch only", variable=name_var,
                        style="Panel.TCheckbutton").pack(anchor="w", pady=(6, 0))
        ttk.Label(frm, text='e.g. "CT+Final2.1.sfc" instead of "Chrono Trigger - '
                            'CT+Final2.1.sfc".', style="PanelMuted.TLabel",
                  wraplength=int(440 * self.k)).pack(anchor="w", padx=(22, 0))
        auto_var = tk.BooleanVar(value=self.settings.get("auto_rom", True))
        ttk.Checkbutton(frm, text="Use my scanned ROM automatically", variable=auto_var,
                        style="Panel.TCheckbutton").pack(anchor="w", pady=(6, 0))
        ttk.Label(frm, text="For games you own (green tick), patches apply to your scanned "
                            "ROM without asking which file to use.",
                  style="PanelMuted.TLabel", wraplength=int(440 * self.k)).pack(
            anchor="w", padx=(22, 0))
        done_var = tk.BooleanVar(value=self.settings.get("done_popup", True))
        ttk.Checkbutton(frm, text="Show a message when patching finishes", variable=done_var,
                        style="Panel.TCheckbutton").pack(anchor="w", pady=(6, 0))
        ttk.Label(frm, text="When off, the status bar shows where the patched ROM was saved.",
                  style="PanelMuted.TLabel", wraplength=int(440 * self.k)).pack(
            anchor="w", padx=(22, 0))
        zip_var = tk.BooleanVar(value=self.settings.get("zip_output", False))
        ttk.Checkbutton(frm, text="Zip patched ROMs (.zip)", variable=zip_var,
                        style="Panel.TCheckbutton").pack(anchor="w", pady=(6, 0))
        ttk.Label(frm, text="Standard compression for ROMs. MSU-1 packs stay unzipped "
                            "since emulators need the audio tracks as files.",
                  style="PanelMuted.TLabel", wraplength=int(440 * self.k)).pack(
            anchor="w", padx=(22, 0))

        frm = pages["Folders"]
        heading("ROM patches")
        keep_var = tk.BooleanVar(value=self.settings.get("keep_patches", False))
        ttk.Checkbutton(frm, text="Save ROM patches", variable=keep_var,
                        style="Panel.TCheckbutton").pack(anchor="w")
        ttk.Label(frm, text="Keep downloaded patch files in the folder below. When off, "
                            "they're deleted once the patched ROM is saved.",
                  style="PanelMuted.TLabel", wraplength=int(440 * self.k)).pack(
            anchor="w", padx=(22, 0))
        heading("Patch download folder")
        dl_var = tk.StringVar(value=self.settings.get("download_dir") or DOWNLOAD_DIR)
        row = ttk.Frame(frm, style="Panel.TFrame")
        row.pack(anchor="w", fill="x")
        ttk.Entry(row, textvariable=dl_var, width=42).pack(side="left")

        def browse():
            d = filedialog.askdirectory(parent=win, initialdir=dl_var.get())
            if d:
                dl_var.set(d)

        def open_folder():
            os.makedirs(dl_var.get(), exist_ok=True)
            os.startfile(dl_var.get())
        ttk.Button(row, text="Browse…", command=browse).pack(side="left", padx=6)
        ttk.Button(frm, text="Open patch folder", command=open_folder).pack(anchor="w", pady=4)

        heading("Browser downloads folder")
        ttk.Label(frm, text="Where your web browser saves files. Vibe Patcher watches it "
                            "after opening a download page, and applies the patch for you.",
                  style="PanelMuted.TLabel", wraplength=int(440 * self.k)).pack(anchor="w")
        bdl_var = tk.StringVar(value=self.downloads_dir())
        row = ttk.Frame(frm, style="Panel.TFrame")
        row.pack(anchor="w", fill="x", pady=4)
        ttk.Entry(row, textvariable=bdl_var, width=42).pack(side="left")

        def browse_bdl():
            d = filedialog.askdirectory(parent=win, initialdir=bdl_var.get())
            if d:
                bdl_var.set(d)
        ttk.Button(row, text="Browse…", command=browse_bdl).pack(side="left", padx=6)

        frm = pages["General"]
        heading("Updates")
        upd_var = tk.BooleanVar(value=self.settings.get("update_check", True))
        ttk.Checkbutton(frm, text="Check for new rom hacks (It will check once a week)",
                        variable=upd_var, style="Panel.TCheckbutton").pack(anchor="w")
        import time as _t
        last = self.settings.get("last_update_check")
        last_txt = _t.strftime("%Y-%m-%d %H:%M", _t.localtime(last)) if last else "Never"
        upd_lbl = ttk.Label(frm, text=f"New hacks from Romhacking.net, Romhack Plaza, Zeldix "
                                      f"etc.\nAlso checks RetroAchievements.\n"
                                      f"Last checked: {last_txt}",
                            style="PanelMuted.TLabel", wraplength=int(440 * self.k))
        upd_lbl.pack(anchor="w", padx=(22, 0))

        def check_now():
            upd_lbl.configure(text="Checking…")
            self.check_for_updates(on_done=lambda m: upd_lbl.winfo_exists() and
                                   upd_lbl.configure(text=m))
        ttk.Button(frm, text="Check now", command=check_now).pack(anchor="w", pady=(4, 0))

        heading("Game list")

        def rebuild():
            if messagebox.askyesno("Rebuild game list", "Download the LaunchBox database "
                                   "again (~100 MB) to refresh the game list?", parent=win):
                win.destroy()
                self.status.configure(text="Rebuilding game list…")
                self.run_bg(lambda: covers.build_game_list(self.set_status), self._games_ready)
        ttk.Button(frm, text="Rebuild game list", command=rebuild).pack(anchor="w")

        heading("Troubleshooting")
        ttk.Label(frm, text=f"Errors are written to {LOG_FILE}", style="PanelMuted.TLabel",
                  wraplength=int(440 * self.k)).pack(anchor="w")

        def open_log():
            if not os.path.exists(LOG_FILE):
                open(LOG_FILE, "a").close()
            os.startfile(LOG_FILE)
        ttk.Button(frm, text="Open log file", command=open_log).pack(anchor="w", pady=4)

        # ---- About ----
        frm = pages["About"]
        logo = Image.open(os.path.join(ASSETS, "logo.png"))
        lh = int(46 * self.k)
        self._about_logo = ImageTk.PhotoImage(
            logo.resize((int(logo.width * lh / logo.height), lh), Image.LANCZOS))
        ttk.Label(frm, image=self._about_logo, style="Panel.TLabel").pack(anchor="w",
                                                                          pady=(10, 14))
        heading("Credits")
        row = ttk.Frame(frm, style="Panel.TFrame")
        row.pack(anchor="w")
        ttk.Label(row, text="Created by ", style="Panel.TLabel").pack(side="left")
        gh = ttk.Label(row, text="immaculatevibes2025", style="Link.TLabel", cursor="hand2")
        gh.pack(side="left")
        gh.bind("<Button-1>", lambda e: open_in_browser("https://github.com/immaculatevibes2025"))
        ttk.Label(row, text=" on GitHub", style="Panel.TLabel").pack(side="left")
        ttk.Label(frm, text="With contributions from Claude (Anthropic)",
                  style="Panel.TLabel").pack(anchor="w", pady=(4, 0))
        heading("Version")
        ttk.Label(frm, text=f"Vibe Patcher {APP_VERSION}", style="Panel.TLabel").pack(anchor="w")

        # ---- History: every patched ROM, newest first ----
        frm = pages["History"]
        heading("Patched ROMs")
        hist_box = ttk.Frame(frm, style="Panel.TFrame")
        hist_box.pack(fill="both", expand=True)
        hist = ttk.Treeview(hist_box, columns=("console", "game", "rom"), show="headings",
                            height=12)
        for col, title, w in (("console", "Console", 90), ("game", "Game", 190),
                              ("rom", "Patched ROM", 260)):
            hist.heading(col, text=title)
            hist.column(col, width=int(w * self.k), anchor="w")
        hist_scroll = ttk.Scrollbar(hist_box, orient="vertical", command=hist.yview)
        hist.configure(yscrollcommand=hist_scroll.set)
        hist_scroll.pack(side="right", fill="y")
        hist.pack(side="left", fill="both", expand=True)
        def fill_history():
            hist.delete(*hist.get_children())
            for i, e in enumerate(self.settings.get("history", [])):
                hist.insert("", "end", iid=str(i),
                            values=(e.get("console", ""), e.get("game", ""),
                                    f"{e.get('rom', '')}   ({e.get('date', '')})"))
        fill_history()

        def open_entry(_e=None):
            sel = hist.selection()
            if sel:
                path = self.settings.get("history", [])[int(sel[0])].get("path", "")
                if os.path.exists(path):
                    subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
                else:
                    messagebox.showinfo("Not found", "That file has been moved or deleted.",
                                        parent=win)
        hist.bind("<Double-1>", open_entry)
        ttk.Label(frm, text="Double-click an entry to show the file.",
                  style="PanelMuted.TLabel").pack(anchor="w", pady=(4, 0))

        def clear_history():
            if messagebox.askyesno("Clear history", "Clear the list of patched ROMs? "
                                   "(The ROM files themselves are kept.)", parent=win):
                self.settings["history"] = []
                save_settings(self.settings)
                fill_history()
        ttk.Button(frm, text="Clear history", command=clear_history).pack(anchor="w",
                                                                           pady=(8, 0))

        def save():
            if ra_var.get().strip() != self.settings.get("ra_key", ""):
                self.settings["ra_key"] = ra_var.get().strip()
                sources.RA_KEY = self.settings["ra_key"]
                self._ra = {}
                self._ra_error = None
                for p in CONSOLES:  # fetch fresh with the new key
                    try:
                        os.remove(os.path.join(covers.CACHE_DIR, f"retroachievements_{p}.json"))
                    except OSError:
                        pass
            self.settings["github_token"] = token_var.get().strip()
            sources.GITHUB_TOKEN = self.settings["github_token"]
            self.settings["download_dir"] = dl_var.get().strip()
            self.settings["downloads_dir"] = bdl_var.get().strip()
            self.settings["msu_folder"] = msu_var.get()
            self.settings["zip_output"] = zip_var.get()
            self.settings["done_popup"] = done_var.get()
            self.settings["auto_rom"] = auto_var.get()
            self.settings["keep_patches"] = keep_var.get()
            self.settings["update_check"] = upd_var.get()
            self.settings["name_patch_only"] = name_var.get()
            self.settings["font"] = font_var.get()
            self.settings["settings_tab"] = book.tab(book.select(), "text").strip()
            self.theme = theme_var.get()
            self.settings["theme"] = self.theme
            save_settings(self.settings)
            self.apply_theme()
            self.apply_filter()  # rebuild tiles so they pick up the new font
            win.destroy()
        btns = ttk.Frame(outer, style="Panel.TFrame")
        btns.pack(fill="x", pady=(14, 0))
        ttk.Button(btns, text="Save", style="Accent.TButton", command=save).pack(side="right")
        ttk.Button(btns, text="Cancel", command=win.destroy).pack(side="right", padx=8)
        win.grab_set()

    def identify_roms(self):
        """Read every owned ROM once (cached) and look it up in No-Intro by checksum."""
        owned = {p: self.owned(p) for p in CONSOLES}

        def done(ids):
            self._rom_ids = ids
            if self.selected:
                self._show_title(self.selected)
                self._update_ra_button()
        self.run_bg(lambda: rom_id.identify_all(owned, self.set_status), done,
                    fail=lambda m: log.warning("ROM identification failed: %s", m))

    def _record_install(self):
        """Remember a patched hack (and how new it is) for the Updates check."""
        pending = getattr(self, "_pending_install", None)
        self._pending_install = None
        if not pending:
            return
        result, finfo, game, platform = pending
        try:
            self.settings["installed"] = updates.record(
                self.settings.get("installed", []), result or {}, finfo, game, platform)
            # patching it again clears it from Updates
            key = next((e["key"] for e in self.settings["installed"]
                        if e["title"] == (result or {}).get("title")), None)
            self.settings["updates"] = [u for u in self.settings.get("updates", [])
                                        if u.get("key") != key]
            save_settings(self.settings)
            self._update_updates_btn()
        except Exception:
            log.exception("Couldn't record the patched hack")

    def _update_updates_btn(self):
        n = len(self.settings.get("updates", []))
        self.updates_btn.configure(text=f"\u27f3  Updates ({n})")

    def open_updates_window(self):
        """Newer versions of the hacks you've patched."""
        c = THEMES[self.theme]
        win = tk.Toplevel(self)
        win.title("Updates")
        win.configure(bg=c["panel"])
        win.transient(self)
        win.minsize(int(460 * self.k), int(300 * self.k))
        frm = ttk.Frame(win, style="Panel.TFrame", padding=18)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="Updates", style="Title.TLabel").pack(anchor="w")
        ttk.Label(frm, style="PanelMuted.TLabel", wraplength=int(520 * self.k),
                  text="Newer versions of the ROM hacks you've patched, checked once a week "
                       "on RAPatches, GitHub and RomhackPlaza.").pack(anchor="w", pady=(4, 8))
        box = ttk.Frame(frm, style="Panel.TFrame")
        box.pack(fill="both", expand=True)
        tree = ttk.Treeview(box, columns=("new",), show="tree headings", height=8)
        tree.heading("#0", text="Hack")
        tree.heading("new", text="What's new")
        tree.column("#0", width=int(240 * self.k))
        tree.column("new", width=int(280 * self.k))
        tree.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(box, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        status = ttk.Label(frm, text="", style="PanelMuted.TLabel")
        status.pack(anchor="w", pady=(6, 0))

        def fill():
            tree.delete(*tree.get_children())
            ups = self.settings.get("updates", [])
            for i, u in enumerate(ups):
                tree.insert("", "end", iid=str(i), text=f"{u['title']}  ({u['game']})",
                            values=(u.get("new", ""),))
            if not ups:
                tree.insert("", "end", iid="none", text="No updates found", values=("",))

        def go():
            sel = tree.selection()
            if not sel or not sel[0].isdigit():
                return
            u = self.settings["updates"][int(sel[0])]
            win.destroy()
            if self.platform != u["platform"]:
                self.set_platform(u["platform"])

            def find(tries=0):
                g = next((g for g in self.games if g["id"] == u["game_id"]), None)
                if g is None and tries < 20:
                    self.after(500, lambda: find(tries + 1))
                    return
                if g:
                    self.select_game(g)
                    self.find_patches()
            find()

        def dismiss():
            sel = tree.selection()
            if sel and sel[0].isdigit():
                del self.settings["updates"][int(sel[0])]
                save_settings(self.settings)
                self._update_updates_btn()
                fill()

        def check_now():
            status.configure(text="Checking…")

            def done(found):
                self.settings["updates"] = found
                save_settings(self.settings)
                self._update_updates_btn()
                if win.winfo_exists():
                    fill()
                    status.configure(text=f"{len(found)} update(s) found")
            self.run_bg(lambda: updates.check(self.settings.get("installed", []),
                                              self.set_status), done,
                        lambda m: win.winfo_exists() and status.configure(text=m))

        btns = ttk.Frame(frm, style="Panel.TFrame")
        btns.pack(fill="x", pady=(10, 0))
        ttk.Button(btns, text="Find patches", style="Accent.TButton", command=go).pack(
            side="left")
        ttk.Button(btns, text="Dismiss", command=dismiss).pack(side="left", padx=8)
        ttk.Button(btns, text="Check now", command=check_now).pack(side="right")
        tree.bind("<Double-1>", lambda e: go())
        fill()

    def _weekly_update_check(self):
        """On first start-up and then once a week: look for new hacks / sets."""
        import time as _t
        if not self.settings.get("update_check", True):
            return
        last = self.settings.get("last_update_check", 0)
        if _t.time() - last >= 7 * 86400:
            self.check_for_updates()

    def check_for_updates(self, on_done=None):
        """Refresh LaunchBox (only if it changed), RomhackPlaza, Zeldix and
        RetroAchievements, then say what's new."""
        import time as _t
        if getattr(self, "_updating", False):
            return
        self._updating = True

        def work():
            lb = None
            try:
                lb = covers.update_if_changed(self.set_status)
            except Exception as e:
                log.warning("LaunchBox update check failed: %s", e)
            online = sources.refresh_online(list(CONSOLES), self.set_status)
            online["updates"] = updates.check(self.settings.get("installed", []),
                                              self.set_status)
            return lb, online

        def done(res):
            self._updating = False
            lb, online = res
            self.settings["last_update_check"] = _t.time()
            save_settings(self.settings)
            bits = []
            if lb:
                n = sum(lb.values())
                if n:
                    bits.append(f"{n} new games / hacks in the lists")
                self._game_cache = {}  # reload the refreshed lists
                self._load_games()
            if online["plaza"]:
                bits.append(f"{online['plaza']} new on RomhackPlaza")
            if online["zeldix"]:
                bits.append(f"{online['zeldix']} new MSU-1 packs")
            self.settings["updates"] = online.get("updates", [])
            save_settings(self.settings)
            self._update_updates_btn()
            if online.get("updates"):
                bits.append(f"{len(online['updates'])} of your hacks updated")
            ra_new = sum(online["ra"].values())
            if ra_new:
                bits.append(f"{ra_new} new RetroAchievements sets")
            self._ra, self._ra_originals, self._ra_hashes = {}, {}, {}  # use fresh sets
            msg = ("Update check: " + ", ".join(bits)) if bits else \
                "Update check: everything is up to date"
            self.status.configure(text=msg)
            log.info(msg)
            if on_done:
                on_done(msg)

        def fail(m):
            self._updating = False
            log.warning("Update check failed: %s", m)
        self.run_bg(work, done, fail)

    def _welcome(self):
        """First start-up: say hello and suggest scanning the ROM folders."""
        if self.settings.get("welcomed") or self.settings.get("rom_folders"):
            return
        self.settings["welcomed"] = True
        save_settings(self.settings)
        c = THEMES[self.theme]
        win = tk.Toplevel(self)
        win.title("Welcome")
        win.configure(bg=c["panel"])
        win.transient(self)
        win.resizable(False, False)
        frm = ttk.Frame(win, style="Panel.TFrame", padding=(24, 20))
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="Welcome to Vibe Patcher!", style="Title.TLabel").pack(anchor="w")
        ttk.Label(frm, style="Panel.TLabel", wraplength=int(420 * self.k), justify="left",
                  text="Browse games by their covers, find ROM hacks, translations and "
                       "MSU-1 packs, and apply them to your own ROMs in one click.\n\n"
                       "We recommend scanning the folders where you keep your ROMs first. "
                       "Vibe Patcher can then automatically patch your games without "
                       "any extra steps!").pack(anchor="w", pady=(10, 16))
        btns = ttk.Frame(frm, style="Panel.TFrame")
        btns.pack(fill="x")

        def scan_now():
            win.destroy()
            self.open_scan_window()

        ttk.Button(btns, text="Scan Later", command=win.destroy).pack(side="right")
        ttk.Button(btns, text="Scan Now", style="Accent.TButton",
                   command=scan_now).pack(side="right", padx=(0, 8))
        win.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - win.winfo_reqwidth()) // 2
        y = self.winfo_rooty() + (self.winfo_height() - win.winfo_reqheight()) // 3
        win.geometry(f"+{max(0, x)}+{max(0, y)}")
        win.grab_set()
        win.focus_set()

    def _scan_on_start(self):
        """Quietly rescan the ROM folders at start-up so new games get their tick."""
        folders = self.settings.get("rom_folders", [])
        if not self.settings.get("scan_on_start", True) or not folders:
            return

        def work():
            return {p: library.scan(folders, covers.load_games(p) or [], platform=p)
                    for p in CONSOLES}

        def done(found):
            new = sum(len(set(found[p]) - set(self.owned(p))) for p in found)
            for p, owned in found.items():
                self.set_owned(p, owned)
            save_settings(self.settings)
            if new:
                self.status.configure(text=f"Start-up scan: {new} new game(s) of yours found")
                self.apply_filter()
            self.identify_roms()  # check versions of any new / changed ROMs
        self.run_bg(work, done, fail=lambda m: log.warning("Start-up scan failed: %s", m))

    def open_scan_window(self):
        """Pick ROM folders and scan them for the games you own (both consoles)."""
        c = THEMES[self.theme]
        win = tk.Toplevel(self)
        win.title("Scan Games")
        win.configure(bg=c["panel"])
        win.transient(self)
        win.resizable(True, True)
        win.minsize(int(420 * self.k), int(300 * self.k))
        frm = ttk.Frame(win, style="Panel.TFrame", padding=18)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="Scan Games", style="Title.TLabel").pack(anchor="w")
        ttk.Label(frm, text="Folders with your ROMs, e.g. SNES: .sfc / .smc, GBA: .gba, "
                            "Genesis: .md / .bin, or .zip / .7z. Subfolders are scanned too. "
                            "Games you own get a green tick.",
                  style="PanelMuted.TLabel", wraplength=int(460 * self.k)).pack(anchor="w",
                                                                               pady=(4, 8))
        folders = list(self.settings.get("rom_folders", []))
        lst_box = ttk.Frame(frm, style="Panel.TFrame")
        lst_box.pack(anchor="w", fill="both", expand=True)
        lst = tk.Listbox(lst_box, height=5, width=60, bg=c["card"], fg=c["fg"], bd=0,
                         highlightthickness=1, highlightbackground=c["border"],
                         selectbackground=c["sel"], selectforeground=c["fg"],
                         font=(self.font_reg, 10))
        for f in folders:
            lst.insert("end", f)
        lst_scroll = ttk.Scrollbar(lst_box, orient="vertical", command=lst.yview)
        lst.configure(yscrollcommand=lst_scroll.set)
        lst_scroll.pack(side="right", fill="y")
        lst.pack(side="left", fill="both", expand=True)
        row = ttk.Frame(frm, style="Panel.TFrame")
        row.pack(anchor="w", fill="x", pady=8)
        owned_lbl = ttk.Label(frm, style="PanelMuted.TLabel", text=self._owned_summary())

        def save_folders():
            self.settings["rom_folders"] = list(folders)
            save_settings(self.settings)

        def add_folder():
            d = filedialog.askdirectory(parent=win, title="Choose a ROM folder")
            if d and d not in folders:
                folders.append(d)
                lst.insert("end", d)
                save_folders()

        def remove_folder():
            for i in reversed(lst.curselection()):
                folders.pop(i)
                lst.delete(i)
            save_folders()

        def scan_now():
            if not folders:
                messagebox.showinfo("No folders", "Add a ROM folder first.", parent=win)
                return
            owned_lbl.configure(text="Scanning…")
            scan_btn.configure(state="disabled")

            def work():
                found = {}
                for p in CONSOLES:
                    games = covers.load_games(p) or []
                    found[p] = library.scan(folders, games, self.set_status, platform=p)
                return found

            def done(found):
                for p, owned in found.items():
                    self.set_owned(p, owned)
                save_folders()
                if owned_lbl.winfo_exists():
                    owned_lbl.configure(text=self._owned_summary())
                    scan_btn.configure(state="normal")
                self.apply_filter()
                self.identify_roms()
            self.run_bg(work, done)
        ttk.Button(row, text="Add folder…", command=add_folder).pack(side="left")
        ttk.Button(row, text="Remove", command=remove_folder).pack(side="left", padx=6)
        scan_btn = ttk.Button(row, text="Scan for games", style="Accent.TButton",
                              command=scan_now)
        scan_btn.pack(side="left")
        owned_lbl.pack(anchor="w")
        ttk.Checkbutton(frm, text="Only Show Games I Own (And Their Hacks)",
                        variable=self.owned_only, command=self.set_owned_only,
                        style="Panel.TCheckbutton").pack(anchor="w", pady=(8, 0))
        start_var = tk.BooleanVar(value=self.settings.get("scan_on_start", True))

        def set_start():
            self.settings["scan_on_start"] = start_var.get()
            save_settings(self.settings)
        ttk.Checkbutton(frm, text="Scan For New Games on Start-Up", variable=start_var,
                        command=set_start, style="Panel.TCheckbutton").pack(anchor="w",
                                                                            pady=(4, 0))
        ttk.Button(frm, text="Close", command=win.destroy).pack(anchor="e", pady=(12, 0))

    def _pick_patch(self, patches):
        win = tk.Toplevel(self)
        win.title("Choose a patch")
        win.configure(bg=THEMES[self.theme]["panel"])
        win.transient(self)
        win.resizable(True, True)
        win.minsize(int(360 * self.k), int(220 * self.k))
        ttk.Label(win, text="This download contains multiple patches:",
                  style="Panel.TLabel", padding=10).pack(anchor="w")
        box = ttk.Frame(win, style="Panel.TFrame")
        box.pack(padx=10, fill="both", expand=True)
        tree = ttk.Treeview(box, show="tree", height=min(12, len(patches)))
        tree.column("#0", width=int(480 * self.k))
        for i, p in enumerate(patches):
            tree.insert("", "end", iid=str(i), text=os.path.basename(p))
        tree.selection_set("0")
        scroll = ttk.Scrollbar(box, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        tree.pack(side="left", fill="both", expand=True)
        choice = {}

        def ok():
            choice["p"] = patches[int(tree.selection()[0])]
            win.destroy()
        ttk.Button(win, text="Use this patch", style="Accent.TButton",
                   command=ok).pack(pady=10)
        win.grab_set()
        self.wait_window(win)
        return choice.get("p")


if __name__ == "__main__":
    log.info("Vibe Patcher starting")
    try:
        App().mainloop()
    except Exception:
        log.exception("Vibe Patcher crashed")
        raise
