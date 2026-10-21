import os
import sys

# The portable .exe keeps everything in a "data" folder next to itself, so the whole
# folder can be zipped, copied or run from a USB stick. Run from source, settings live
# in the user's profile as before.
PORTABLE = getattr(sys, "frozen", False)
APP_BASE = os.path.dirname(sys.executable if PORTABLE else os.path.abspath(__file__))

if PORTABLE:
    APP_DIR = os.path.join(APP_BASE, "data")
    COVER_CACHE = os.path.join(APP_DIR, "cache", "covers")
else:
    APP_DIR = os.path.join(os.path.expanduser("~"), ".vibe_patcher")
    _OLD_DIR = os.path.join(os.path.expanduser("~"), ".snes_patch_browser")
    if os.path.isdir(_OLD_DIR) and not os.path.exists(APP_DIR):
        os.rename(_OLD_DIR, APP_DIR)  # keep the cache from before the rename
    COVER_CACHE = os.path.join(APP_BASE, "cache", "covers")

CACHE_DIR = os.path.join(APP_DIR, "cache")
DOWNLOAD_DIR = os.path.join(APP_DIR, "patches")
SETTINGS = os.path.join(APP_DIR, "settings.json")
LOG_FILE = os.path.join(APP_DIR, "vibe_patcher.log")
