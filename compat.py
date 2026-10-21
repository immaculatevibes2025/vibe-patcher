"""Will a patch work with the user's ROM?

romhacking.net lists, for most patches, the ROM they were made for: a No-Intro name
("Donkey Kong Country 2 - Diddy's Kong Quest (USA) (Rev 1)") and often its CRC32 /
SHA-1. Descriptions and readmes mention it in words ("needs the US v1.1 ROM"), and BPS /
UPS patches store the CRC32 of the exact ROM they expect. This compares all of that with
the user's ROM (its checksums and No-Intro name)."""
import hashlib
import os
import re
import zlib

import rom_id

REGIONS = [  # (key, pattern) - the first match wins
    ("USA", r"\busa\b|\bus\b|\bntsc\b(?![- ]?j)|\(u\)|\[u\]|\(ju\)|\(uj\)|\(u,\s*j\)|ntsc[- ]?u|north america|\bnes\b(?=.*\(u)"),
    ("Europe", r"\beurope\b|\beu\b|\(e\)|\[e\]|\bpal\b|\beur\b"),
    ("Japan", r"\bjapan\b|\bjp\b|\bjpn\b|\(j\)|\[j\]|ntsc[- ]?j|super famicom"),
    ("World", r"\bworld\b|\(w\)"),
]
REQUIRE = re.compile(r"requir|support|\bneeds?\b|\broms?\b|\bstock\b|vanilla|"
                     r"unmodified|\bclean\b|patch(es)? for|\bfor (the )?(pal|ntsc|usa?|eu|"
                     r"europe|japan|\(u\)|\(e\)|\(j\))|apply (it |this |the patch )?(to|on)|patch(ed)? "
                     r"(to|against|onto)|compatib|works? (with|on)|\brom\b|\bcrc|"
                     r"no-intro|headered|headerless|\bdump\b", re.I)
VERSION = re.compile(r"\b(?:rev\.?\s*([0-9a-c])|v(?:ersion)?\s*([01]\.[0-9]))\b", re.I)


def _hexes(text, length):
    return {h.lower() for h in re.findall(rf"(?<![0-9a-f])([0-9a-f]{{{length}}})(?![0-9a-f])",
                                          text, re.I)}


def requirements(text, rominfo=""):
    """What a patch needs. rominfo (romhacking.net's ROM info) is all about the ROM;
    in description / readme text only sentences about patching count for region words."""
    text = "\n".join(x for x in (rominfo, text) if x)
    req = {"crc": set(), "sha1": set(), "md5": set(), "names": [], "region": None,
           "version": None}
    if not text:
        return req
    for line in text.splitlines():
        low = line.lower()
        if "crc" in low:
            req["crc"] |= _hexes(line, 8)
        if "sha-1" in low or "sha1" in low:
            req["sha1"] |= _hexes(line, 40)
        if "md5" in low:
            req["md5"] |= _hexes(line, 32)
        m = re.search(r"(?:database match|no-intro name|rom name|game name)\s*:\s*(.+)",
                      line, re.I)
        if not m:  # "Super Donkey Kong 2 - Dixie & Diddy (Japan).sfc - NOINTRO"
            m = re.match(r"\s*(.+?\([^)]*\).*?)\.(sfc|smc|nes|gba|gbc|gb|nds|z64|n64|v64|"
                         r"md|gen|bin|sms|gg)", line, re.I)
        if m:
            req["names"].append(re.sub(r"\s*-\s*(headerless|headered).*$", "", m.group(1),
                                       flags=re.I).strip())
    if req["names"]:
        words = " ".join(req["names"])
    else:  # free text: only sentences about what to patch ("requires the US v1.1 ROM")
        sentences = re.split(r"(?<=[.!?])\s+|\n", text[len(rominfo):])
        words = (rominfo or "") + " " + " ".join(x for x in sentences if REQUIRE.search(x))
    req["region"], req["version"] = region_version(words)
    if not req["names"]:  # "supports V1.2 (USA), V1.1(E), (Europe+Kremlings Revenge)"
        opts = []
        for part in re.split(r",|;|/|\bor\b|\band\b|\n", words, flags=re.I):
            rv = region_version(part, assume_first=False)
            if (rv[0] or rv[1]) and rv not in opts:
                opts.append(rv)
        if len(opts) > 1:
            req["options"] = opts
    return req


def region_version(text, assume_first=True):
    """('USA', '1.1') from a ROM name or a sentence; None where it doesn't say.
    assume_first: a No-Intro name without "(Rev ...)" means v1.0."""
    low = text.lower()
    region = next((k for k, pat in REGIONS if re.search(pat, low)), None)
    m = None
    for cand in VERSION.finditer(text):
        before = text[max(0, cand.start() - 14):cand.start()].lower()
        if not re.search(r"patch|pack|msu|hack|update|release|build|beta|mod\b", before):
            m = cand
            break
    version = None
    if m:
        if m.group(1):  # No-Intro "(Rev 1)" / "(Rev A)" = v1.1
            rev = m.group(1).lower()
            version = f"1.{int(rev) if rev.isdigit() else ord(rev) - 96}"
        else:
            version = m.group(2)
    elif re.search(r"\bstock\b|vanilla|unmodified|\bclean\b|original (rom|game|cart)",
                   low):
        version = "1.0"  # "Patch v11 (stock rom)": the first, unrevised release
    elif assume_first and region and re.search(r"\(\s*(usa|europe|japan|world)[^)]*\)", low):
        version = "1.0"  # a No-Intro name without "(Rev ...)" is the first release
    return region, version


def rom_info(path, platform):
    """Checksums (header removed, and of the whole file) and No-Intro name of a ROM."""
    raw = rom_id._rom_bytes(path)
    data = rom_id._normalise(raw, platform)
    info = rom_id.identify(path, platform)
    info["sha1"] = hashlib.sha1(data).hexdigest()
    info["raw_crc"] = f"{zlib.crc32(raw) & 0xFFFFFFFF:08x}"
    info["region"], info["version"] = region_version(info.get("name") or "")
    base = re.split(r"\s*\(", info.get("name") or "")[0].lower()
    names = set(rom_id.load_dat(platform)["crc"].values()) if base else set()
    info["has_usa"] = any(n.lower().startswith(base + " (") and "(usa" in n.lower()
                          for n in names) if base else True
    return info


def patch_source_crc(path):
    """The CRC32 of the ROM a BPS / UPS patch was made from (stored in its last bytes)."""
    try:
        with open(path, "rb") as f:
            magic = f.read(4)
            if magic not in (b"BPS1", b"UPS1"):
                return None
            f.seek(-12, os.SEEK_END)
            return f"{int.from_bytes(f.read(4), 'little'):08x}"
    except OSError:
        return None


def _default_region(rom):
    return "USA" if rom.get("has_usa", True) else (rom.get("region") or "Europe")


def check(req, rom):
    """('ok' | 'maybe' | 'bad' | 'unknown', short text for the badge, longer text)."""
    if req.get("version") and not req.get("region") and not req["names"]:
        req = dict(req, region=_default_region(rom))
    if req.get("options"):
        req = dict(req, options=[(r or (_default_region(rom) if v else None), v)
                                 for r, v in req["options"]])
    have = rom.get("name") or "your ROM"
    hashes = req["crc"] | req["sha1"] | req["md5"]
    mine = {rom.get("crc"), rom.get("raw_crc"), rom.get("sha1"), rom.get("md5")}
    need = req["names"][0] if req["names"] else None

    def mismatch():
        """What differs in region / version ([] if nothing known differs)."""
        bad = []
        if req["region"] and rom.get("region") and req["region"] != rom["region"] and \
                "World" not in (req["region"], rom["region"]):
            bad.append(req["region"])
        if req["version"] and rom.get("version") and req["version"] != rom["version"]:
            bad.append(f"v{req['version']}")
        return bad

    if hashes and hashes & mine:
        return "ok", "Works with your ROM", f"Your ROM ({have}) is the exact version this " \
                                            "patch was made for."
    if req.get("options"):
        listed = ", ".join(" ".join(x for x in (f"({r})" if r else "", f"v{v}" if v else "")
                                    if x) for r, v in req["options"])

        def fits(region, version):
            ok_region = not region or not rom.get("region") or region == rom["region"] or \
                "World" in (region, rom.get("region"))
            ok_version = not version or not rom.get("version") or version == rom["version"]
            return ok_region and ok_version
        if not rom.get("name"):
            return "unknown", "Version not checked", \
                f"This patch supports {listed}; your ROM isn't in the No-Intro list, so its " \
                "version is unknown."
        if any(fits(r, v) for r, v in req["options"]):
            return "ok", "Should work with your ROM", \
                f"This patch supports {listed}, and your ROM is {have}."
        return "bad", "Needs another version", \
            f"This patch supports {listed}; your ROM is {have}."
    if req["region"] or req["version"]:
        if mismatch():
            return "bad", f"Needs {short(need, req)}", \
                f"This patch needs {need or short(None, req)}; your ROM is {have}."
        if hashes:  # same region / version, but another dump (scene release, overdump...)
            return "maybe", "Probably works (different dump)", \
                f"This patch lists {need or 'a ' + short(None, req) + ' ROM'} - the same " \
                f"region and version as yours ({have}), but a different copy of it. It " \
                "usually still works; Vibe Patcher checks the patch itself after download."
        if rom.get("name"):
            return "ok", "Should work with your ROM", \
                f"Your ROM ({have}) matches the version this patch asks for."
    if hashes:
        return "maybe", "Can't confirm version", \
            f"This patch lists a ROM checksum that isn't your copy ({have}), and it doesn't say " \
            "which version that is (often a headered or modified ROM). Vibe Patcher checks " \
            "the patch itself after download."
    return "unknown", "Version not listed", \
        "The patch doesn't say which ROM version it needs - Vibe Patcher checks again after " \
        "downloading (BPS / UPS patches record the exact ROM they were made for)."


def short(name, req):
    """'(USA) v1.1' style summary of what a patch needs."""
    parts = [f"({req['region']})" if req["region"] else "",
             f"v{req['version']}" if req["version"] else ""]
    return " ".join(p for p in parts if p) or (name or "another version")
