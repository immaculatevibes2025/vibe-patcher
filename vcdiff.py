"""A pure-Python decoder for VCDIFF (RFC 3284) - the format xdelta3 writes - so .xdelta
patches apply without installing xdelta3. Supports xdelta3's extras (application
header, per-window Adler-32 checksums); patches made with secondary compression
(xdelta3 -S djw/lzma) aren't supported and raise a clear error."""
import zlib

VCD_SOURCE, VCD_TARGET, VCD_ADLER32 = 0x01, 0x02, 0x04
NOOP, ADD, RUN, COPY = 0, 1, 2, 3


class VCDiffError(Exception):
    pass


def _default_code_table():
    """RFC 3284 section 5.6: 256 entries of (inst1, size1, mode1, inst2, size2, mode2)."""
    t = [(RUN, 0, 0, NOOP, 0, 0)]
    t += [(ADD, s, 0, NOOP, 0, 0) for s in range(0, 18)]          # ADD size 0, 1..17
    for mode in range(9):                                          # COPY size 0, 4..18
        t.append((COPY, 0, mode, NOOP, 0, 0))
        t += [(COPY, s, mode, NOOP, 0, 0) for s in range(4, 19)]
    for mode in range(6):                                          # ADD 1-4 + COPY 4-6
        for a in range(1, 5):
            for c in range(4, 7):
                t.append((ADD, a, 0, COPY, c, mode))
    for mode in range(6, 9):                                       # ADD 1-4 + COPY 4
        for a in range(1, 5):
            t.append((ADD, a, 0, COPY, 4, mode))
    for mode in range(9):                                          # COPY 4 + ADD 1
        t.append((COPY, 4, mode, ADD, 1, 0))
    assert len(t) == 256
    return t


CODE_TABLE = _default_code_table()


class _Reader:
    def __init__(self, data, pos=0):
        self.d, self.p = data, pos

    def byte(self):
        if self.p >= len(self.d):
            raise VCDiffError("Patch file ends unexpectedly")
        b = self.d[self.p]
        self.p += 1
        return b

    def varint(self):
        """Big-endian base-128 integer, high bit = more bytes follow."""
        v = 0
        for _ in range(10):
            b = self.byte()
            v = (v << 7) | (b & 0x7F)
            if not b & 0x80:
                return v
        raise VCDiffError("Bad number in patch file")

    def take(self, n):
        if self.p + n > len(self.d):
            raise VCDiffError("Patch file ends unexpectedly")
        out = self.d[self.p:self.p + n]
        self.p += n
        return out


def _adler32(data):
    return zlib.adler32(data) & 0xFFFFFFFF


def decode(source: bytes, patch: bytes) -> bytes:
    """Apply a VCDIFF / xdelta3 patch to source and return the result."""
    r = _Reader(patch)
    if r.take(3) != b"\xd6\xc3\xc4":
        raise VCDiffError("Not an xdelta / VCDIFF patch")
    r.byte()  # version (0 for RFC 3284; xdelta3 also writes 0)
    hdr = r.byte()
    if hdr & 0x01:
        raise VCDiffError("This xdelta patch uses secondary compression, which isn't "
                          "supported - install xdelta3.exe to apply it")
    if hdr & 0x02:
        raise VCDiffError("This xdelta patch uses a custom code table (not supported)")
    if hdr & 0x04:  # xdelta3 application header (file names) - skip
        r.take(r.varint())

    out = bytearray()
    while r.p < len(patch):
        win = r.byte()
        seg, seg_pos = b"", 0
        if win & (VCD_SOURCE | VCD_TARGET):
            size, seg_pos = r.varint(), r.varint()
            base = source if win & VCD_SOURCE else out
            if seg_pos + size > len(base):
                raise VCDiffError("The patch doesn't fit this ROM - it was probably made "
                                  "for a different version")
            seg = bytes(base[seg_pos:seg_pos + size])
        r.varint()                       # length of the delta encoding
        target_len = r.varint()
        delta_ind = r.byte()
        if delta_ind:
            raise VCDiffError("This xdelta patch uses secondary compression, which isn't "
                              "supported - install xdelta3.exe to apply it")
        data_len, inst_len, addr_len = r.varint(), r.varint(), r.varint()
        checksum = None
        if win & VCD_ADLER32:
            checksum = int.from_bytes(r.take(4), "big")
        data = _Reader(r.take(data_len))
        inst = _Reader(r.take(inst_len))
        addr = _Reader(r.take(addr_len))

        target = bytearray()
        near = [0] * 4                   # address caches (s_near=4, s_same=3)
        same = [0] * (3 * 256)
        near_i = 0
        S = len(seg)

        def decode_addr(mode, here):
            nonlocal near_i
            if mode == 0:
                a = addr.varint()
            elif mode == 1:
                a = here - addr.varint()
            elif mode < 6:
                a = near[mode - 2] + addr.varint()
            else:
                a = same[(mode - 6) * 256 + addr.byte()]
            near[near_i] = a
            near_i = (near_i + 1) % 4
            same[a % (3 * 256)] = a
            return a

        def run(op, size, mode):
            if op == NOOP:
                return
            if size == 0:
                size = inst.varint()
            if op == ADD:
                target.extend(data.take(size))
            elif op == RUN:
                target.extend(data.take(1) * size)
            else:  # COPY from the source segment followed by the target so far
                a = decode_addr(mode, S + len(target))
                if a < S:
                    end = a + size
                    if end <= S:
                        target.extend(seg[a:end])
                        return
                    target.extend(seg[a:S])
                    size, a = end - S, S
                a -= S
                for _ in range(size):  # may overlap the bytes being written
                    target.append(target[a])
                    a += 1

        while inst.p < len(inst.d):
            i1, s1, m1, i2, s2, m2 = CODE_TABLE[inst.byte()]
            run(i1, s1, m1)
            run(i2, s2, m2)
        if len(target) != target_len:
            raise VCDiffError("Patch decoding went wrong (window size mismatch)")
        if checksum is not None and _adler32(bytes(target)) != checksum:
            raise VCDiffError("Wrong ROM: the patched result doesn't match the patch's "
                              "checksum - it was made for a different ROM version")
        out.extend(target)
    return bytes(out)
