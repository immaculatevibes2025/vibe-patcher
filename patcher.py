"""Pure-Python IPS / UPS / BPS patch appliers, plus SNES header helpers."""
import os
import shutil
import subprocess
import zlib


class PatchError(Exception):
    pass


def crc32(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


# ---------- SNES copier header (512 bytes) ----------

def has_snes_header(data: bytes) -> bool:
    return len(data) % 1024 == 512


def strip_header(data: bytes) -> bytes:
    return data[512:] if has_snes_header(data) else data


# ---------- IPS ----------

def apply_ips(rom: bytes, patch: bytes) -> bytes:
    if patch[:5] != b"PATCH":
        raise PatchError("Not an IPS file")
    out = bytearray(rom)
    i = 5
    while True:
        if i + 3 > len(patch):
            raise PatchError("Truncated IPS file")
        if patch[i:i + 3] == b"EOF":
            i += 3
            if i + 3 <= len(patch):  # optional truncate extension
                del out[int.from_bytes(patch[i:i + 3], "big"):]
            break
        off = int.from_bytes(patch[i:i + 3], "big")
        size = int.from_bytes(patch[i + 3:i + 5], "big")
        i += 5
        if size == 0:  # RLE record
            run = int.from_bytes(patch[i:i + 2], "big")
            chunk = patch[i + 2:i + 3] * run
            i += 3
        else:
            chunk = patch[i:i + size]
            i += size
        end = off + len(chunk)
        if end > len(out):
            out.extend(b"\x00" * (end - len(out)))
        out[off:end] = chunk
    return bytes(out)


# ---------- UPS / BPS shared varint ----------

def _read_vlv(buf: bytes, pos: int):
    value, shift = 0, 1
    while True:
        b = buf[pos]
        pos += 1
        value += (b & 0x7F) * shift
        if b & 0x80:
            return value, pos
        shift <<= 7
        value += shift


def apply_ups(rom: bytes, patch: bytes) -> bytes:
    if patch[:4] != b"UPS1":
        raise PatchError("Not a UPS file")
    if crc32(patch[:-4]) != int.from_bytes(patch[-4:], "little"):
        raise PatchError("UPS patch file is corrupt")
    src_crc = int.from_bytes(patch[-12:-8], "little")
    dst_crc = int.from_bytes(patch[-8:-4], "little")
    pos = 4
    in_size, pos = _read_vlv(patch, pos)
    out_size, pos = _read_vlv(patch, pos)
    if crc32(rom) != src_crc:
        raise PatchError(f"Wrong ROM: expected CRC32 {src_crc:08X}, got {crc32(rom):08X}")
    out = bytearray(rom[:out_size].ljust(out_size, b"\x00"))
    p = 0
    end = len(patch) - 12
    while pos < end:
        skip, pos = _read_vlv(patch, pos)
        p += skip
        while patch[pos] != 0:
            if p < out_size:
                out[p] ^= patch[pos]
            p += 1
            pos += 1
        pos += 1
        p += 1
    if crc32(out) != dst_crc:
        raise PatchError("Output CRC mismatch after UPS patch")
    return bytes(out)


def apply_bps(rom: bytes, patch: bytes) -> bytes:
    if patch[:4] != b"BPS1":
        raise PatchError("Not a BPS file")
    if crc32(patch[:-4]) != int.from_bytes(patch[-4:], "little"):
        raise PatchError("BPS patch file is corrupt")
    src_crc = int.from_bytes(patch[-12:-8], "little")
    dst_crc = int.from_bytes(patch[-8:-4], "little")
    if crc32(rom) != src_crc:
        raise PatchError(f"Wrong ROM: expected CRC32 {src_crc:08X}, got {crc32(rom):08X}")
    pos = 4
    _src_size, pos = _read_vlv(patch, pos)
    dst_size, pos = _read_vlv(patch, pos)
    meta_size, pos = _read_vlv(patch, pos)
    pos += meta_size
    out = bytearray(dst_size)
    op = 0
    src_rel = dst_rel = 0
    end = len(patch) - 12
    while pos < end:
        data, pos = _read_vlv(patch, pos)
        cmd, length = data & 3, (data >> 2) + 1
        if cmd == 0:  # SourceRead
            out[op:op + length] = rom[op:op + length]
            op += length
        elif cmd == 1:  # TargetRead
            out[op:op + length] = patch[pos:pos + length]
            pos += length
            op += length
        else:
            d, pos = _read_vlv(patch, pos)
            delta = -(d >> 1) if d & 1 else d >> 1
            if cmd == 2:  # SourceCopy
                src_rel += delta
                out[op:op + length] = rom[src_rel:src_rel + length]
                src_rel += length
                op += length
            else:  # TargetCopy (may overlap, copy byte by byte)
                dst_rel += delta
                for _ in range(length):
                    out[op] = out[dst_rel]
                    op += 1
                    dst_rel += 1
    if crc32(out) != dst_crc:
        raise PatchError("Output CRC mismatch after BPS patch")
    return bytes(out)


def apply_xdelta(rom: bytes, patch: bytes) -> bytes:
    """xdelta3 / VCDIFF patches, decoded in Python (no xdelta3.exe needed). Falls back
    to xdelta3.exe for the rare patches made with secondary compression."""
    import vcdiff
    try:
        return vcdiff.decode(rom, patch)
    except vcdiff.VCDiffError as e:
        exe = shutil.which("xdelta3") or shutil.which("xdelta")
        if "secondary compression" not in str(e) or not exe:
            raise PatchError(str(e))
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        src, pat, out = (os.path.join(tmp, n) for n in ("rom", "patch", "out"))
        open(src, "wb").write(rom)
        open(pat, "wb").write(patch)
        r = subprocess.run([exe, "-d", "-f", "-s", src, pat, out], capture_output=True,
                           text=True)
        if r.returncode:
            raise PatchError(r.stderr.strip() or "xdelta3 failed")
        return open(out, "rb").read()


PATCH_EXTS = (".ips", ".ups", ".bps", ".xdelta", ".vcdiff")


N64_V64 = b"\x37\x80\x40\x12"  # byte-swapped (.v64)
N64_N64 = b"\x40\x12\x37\x80"  # little-endian (.n64)


def to_z64(rom: bytes):
    """N64 ROMs come in three byte orders; patches are made for big-endian .z64.
    Returns (rom, note) with .v64 / .n64 ROMs converted, other ROMs unchanged."""
    if rom[:4] == N64_V64:
        b = bytearray(rom)
        b[0::2], b[1::2] = rom[1::2], rom[0::2]
        return bytes(b), " (converted the N64 ROM from .v64 to .z64 byte order first)"
    if rom[:4] == N64_N64:
        b = bytearray(len(rom))
        for i in range(4):
            b[i::4] = rom[3 - i::4]
        return bytes(b), " (converted the N64 ROM from .n64 to .z64 byte order first)"
    return rom, ""


def apply_patch_file(rom_path: str, patch_path: str, out_path: str) -> str:
    """Apply a patch, retrying with the SNES header removed if the checksum fails.
    Returns a status message."""
    if os.path.normcase(os.path.abspath(rom_path)) == os.path.normcase(os.path.abspath(out_path)):
        raise PatchError("Output would overwrite the original ROM")
    ext = patch_path.lower().rsplit(".", 1)[-1]
    rom = open(rom_path, "rb").read()
    rom, note = to_z64(rom)
    patch = open(patch_path, "rb").read()
    fn = {"ips": apply_ips, "ups": apply_ups, "bps": apply_bps,
          "xdelta": apply_xdelta, "vcdiff": apply_xdelta}.get(ext)
    if not fn:
        raise PatchError(f"Unsupported patch type: .{ext}")
    try:
        result = fn(rom, patch)
    except PatchError as e:
        if ext != "ips" and has_snes_header(rom) and ("Wrong ROM" in str(e) or
                                                      "doesn't fit" in str(e)):
            result = fn(strip_header(rom), patch)
            note = " (removed the 512-byte copier header first)"
        else:
            raise
    with open(out_path, "wb") as f:
        f.write(result)
    return f"Patched successfully{note}."
