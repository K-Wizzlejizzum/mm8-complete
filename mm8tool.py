#!/usr/bin/env python3
"""
mm8tool.py - Mega Man 8 - Complete builder: ports the Saturn-only content into the US PlayStation version and builds
the patched PlayStation image from the user's own discs. Everything game-derived is read from the discs at build time.

Requires: python3, pycdlib, capstone>=6 (MIPS + SuperH), numpy; a mipsel-linux-gnu GCC + ld (MM8_CROSS = toolchain
prefix, default mipsel-linux-gnu-). A host C compiler (gcc/cc), if present, is used for the ADPCM encoder (same output).
helper_map.txt is read from this file's directory.

Usage:
    mm8tool.py [release] [--ps1 P] [--sat P] [--out DIR] [--keep-work]
        finds the US PlayStation / US Saturn disc (.cue or folder; default discs/ps1, discs/sat), checks every track
        against the Redump md5s (Track 1 must match; audio tracks give a note), extracts them into $MM8_WORK (default
        ./mm8work), builds and checks the image, and writes a full bin/cue into --out (default ./output).

Contents of the build (build_image): track 1 extended; restored Search Man stage (map, sprites, text, doors,
placements) + Wood Man/leaf lifted to C in the STAGE08 overlay + PBGM46/PBGM47; restored Duo mini-stage (hidden room,
arena, placements) + Cut Man and the P5 0x34 alarm lifted to C in the STAGE09 overlay + PBGM48/PBGM49; play_bgm hook +
sound guard; the title menu with BONUS MODE as its 4th item and the Saturn bonus menu lifted to C with every
sub-screen; the Saturn placement list in every stage PAC + the Saturn bat (P2 0x22) lifted to C in the STAGE05
overlay; the credits hint; the boss-intro name in the Saturn's colors and letters; the HUD head icon and lives digit at the
Saturn's positions; the Saturn's Tengu Man stage song converted to a second bank for music 5. check_build then re-checks the image (sectors, EDC/ECC, music entries, DEMO/bonus/SLABO, SELECT.PAC, HUD list).
"""
import os, sys, re, struct, hashlib, argparse, collections, random, functools


WORK = os.path.abspath(os.environ.get('MM8_WORK', './mm8work'))
SAT_FILES = os.path.join(WORK, 'sat', 'files')
PS1_FILES = os.path.join(WORK, 'ps1', 'files')

KNOWN_MD5 = {
    ('sat', '2NDREAD.PRG'): '2be6fd7b4cc6fa250c129caba96d5cae',
    ('sat', '1STREAD.PRG'): 'dd8007babc65f4d4ce46aecf65eccf41',
    ('ps1', 'SLUS_004.53'): '4254a7176d09d5ad58bfc7c4eefbe20a',
    ('ps1', 'OVL/STAGE08.BIN'): '7147c0c48c431bc7be2206b398925181',
    ('ps1', 'OVL/STAGE09.BIN'): 'f0bca16e43a42a4cbedc7a552ec938b3',
    ('ps1', 'OVL/STAGE05.BIN'): '899e7882145ad499100923cb2c5a1fdb',
}

# ----------------------------------------------------------------- disc / PAC
def raw_to_iso(src, dst):
    """Convert a raw 2352-byte-sector track (or pass through a 2048 .iso). Returns 'saturn'|'ps1'."""
    size = os.path.getsize(src)
    with open(src, 'rb') as f:
        head = f.read(2352 * 17)
    if size % 2352 == 0 and head[:12] == b'\x00' + b'\xff' * 10 + b'\x00':
        if head[16:32] == b'SEGA SEGASATURN ':
            off, kind = 16, 'saturn'          # MODE1/2352
        elif head[16 * 2352 + 24 + 1:16 * 2352 + 24 + 6] == b'CD001':
            off, kind = 24, 'ps1'             # MODE2/2352 form 1
        else:
            raise SystemExit(f'{src}: unrecognised raw track')
        import numpy as np
        with open(src, 'rb') as f, open(dst, 'wb') as o:
            while True:
                s = f.read(2352 * 4096)
                n = len(s) // 2352
                if n: o.write(np.frombuffer(s, np.uint8, n * 2352).reshape(n, 2352)[:, off:off + 2048].tobytes())
                if len(s) < 2352 * 4096: break
        return kind
    # assume cooked ISO
    kind = 'saturn' if head[:16] == b'SEGA SEGASATURN ' else 'ps1'
    if os.path.abspath(src) != os.path.abspath(dst):
        import shutil; shutil.copyfile(src, dst)
    return kind

def extract_iso(iso_path, outdir):
    import pycdlib
    iso = pycdlib.PyCdlib(); iso.open(iso_path)
    n = 0
    for root, dirs, files in iso.walk(iso_path='/'):
        for fn in files:
            p = root.rstrip('/') + '/' + fn
            out = os.path.join(outdir, p.split(';')[0].lstrip('/'))
            os.makedirs(os.path.dirname(out), exist_ok=True)
            try:
                iso.get_file_from_iso(out, iso_path=p); n += 1
            except Exception as e:
                print('  skip', p, e)       # PS1 XA/STR streams etc.
    iso.close()
    return n

def unpac_bytes(d):
    """PAC: u32 count, u32 total, count*(u32 type,u32 size); data from 0x800, each chunk padded to 0x800.
    Saturn = big-endian, PS1 = little-endian (detected: the count's low byte comes first on PS1).
    Returns [(type, offset, bytes)]."""
    e = '<' if d[0] != 0 else '>'
    n = struct.unpack(e + 'I', d[:4])[0]
    off, out = 0x800, []
    for i in range(n):
        t, s = struct.unpack(e + 'II', d[8 + i * 8:16 + i * 8])
        out.append((t, off, d[off:off + s])); off += (s + 0x7ff) & ~0x7ff
    return out

@functools.lru_cache(maxsize=None)
def _file(path): return open(path, 'rb').read()

def unpac(path): return unpac_bytes(_file(path))

def pac_chunks(side, name):
    """{type: bytes} of a stage/bank PAC from the extracted workspace (file reads are cached)."""
    base = SAT_FILES if side == 'sat' else os.path.join(PS1_FILES, 'STDATA')
    return {t: b for t, o, b in unpac(os.path.join(base, name if name.endswith('.PAC') else name + '.PAC'))}

def placements(side, stage):
    """Chunk 0x0A records: (type, flags, x, y). flags low byte = pool (1 = enemy pool P2)."""
    # Bytes 0-3 are single bytes (0, type, variant, pool) in the same order on both systems;
    # only x/y are byte-swapped halfwords. flags = variant << 8 | pool.
    b = pac_chunks(side, stage)[0xA]
    e = '>' if side == 'sat' else '<'
    # The list ends at the first record whose pool byte is 0xFF (`00 00 00 FF`); the game copies the
    # whole 0x800-byte chunk to RAM 0x801C2B3C and scans until that terminator.
    out = []
    for i in range(0, len(b) - 7, 8):
        if b[i + 3] == 0xFF: break
        out.append(struct.unpack('>2H', b[i:i + 4]) + struct.unpack(e + '2H', b[i + 4:i + 8]))
    return out

def place_rec(side, stage, i):
    """Record i of a stage's placement list (chunk 0x0A), read from the disc, in the PS1 byte layout."""
    t = placements(side, stage)[i]
    return struct.pack('>2H', t[0], t[1]) + struct.pack('<2H', t[2], t[3])

def fp(v):
    """Short fingerprint (SHA-256 of repr) of a value read from a disc. The tool checks game code and data against
    fingerprints, never against copies of the bytes: no game-derived literals in the tool (03 §7)."""
    return hashlib.sha256(repr(v).encode()).hexdigest()[:12]

def placement_terminator(chunk):
    """Byte offset of the `00 00 00 FF` terminator record in a chunk-0x0A blob."""
    for i in range(0, len(chunk) - 7, 8):
        if chunk[i + 3] == 0xFF: return i
    raise ValueError('no terminator')

def md5(path): return hashlib.md5(open(path, 'rb').read()).hexdigest()
def md5_bytes(b): return hashlib.md5(b).hexdigest()

# ----------------------------------------------------------------- build cache
# The release builds the image and then checks it in the same run. check_build rebuilds the music banks, the bonus
# overlays and the converted Bonus Mode files to compare them with the disc; inside `build_cache()` those builders
# return the result of their first call (a copy) instead of building twice. The developer `build` command keeps the
# independent rebuild.
_MEMO = None

def _memo(fn):
    import inspect
    sig = inspect.signature(fn)
    @functools.wraps(fn)
    def w(*args, **kw):
        if _MEMO is None: return fn(*args, **kw)
        import copy
        ba = sig.bind(*args, **kw); ba.apply_defaults()
        key = (fn.__name__,) + tuple(v for k, v in ba.arguments.items() if k != 'workdir')
        if key not in _MEMO: _MEMO[key] = fn(*args, **kw)
        return copy.deepcopy(_MEMO[key])
    return w

class build_cache:
    def __enter__(self):
        global _MEMO
        _MEMO = {}
    def __exit__(self, *exc):
        global _MEMO
        _MEMO = None

# ----------------------------------------------------------------- raw PS1 disc patching
# Mode 2 Form 1 EDC/ECC (ECMA-130). Reproduces the original disc's sectors byte for byte.
_EDC = []
for _i in range(256):
    _e = _i
    for _ in range(8): _e = (_e >> 1) ^ (0xD8018001 if _e & 1 else 0)
    _EDC.append(_e)
_ECC_F = [((i << 1) ^ (0x11D if i & 0x80 else 0)) & 0xFF for i in range(256)]
_ECC_B = [0] * 256
for _i in range(256): _ECC_B[_i ^ _ECC_F[_i]] = _i

def fix_m2f1(s):
    """Recompute EDC + ECC of one raw 2352-byte Mode 2 Form 1 sector (bytearray, in place)."""
    s[:] = fix_m2f1_many([bytes(s)])[0]
    return s

_ECC_TABLES = None
def _ecc_tables():
    """numpy tables for fix_m2f1_many: EDC table, GF(2^8) tables and the P / Q byte index matrices."""
    global _ECC_TABLES
    if _ECC_TABLES is None:
        import numpy as np
        def idx(major_count, minor_count, major_mult, minor_inc):
            size = major_count * minor_count
            return np.array([[((m >> 1) * major_mult + (m & 1) + k * minor_inc) % size for k in range(minor_count)]
                             for m in range(major_count)], np.intp) + 0xC
        _ECC_TABLES = (np.array(_EDC, np.uint32), np.array(_ECC_F, np.uint8), np.array(_ECC_B, np.uint8),
                       idx(86, 24, 2, 86), idx(52, 43, 86, 88))
    return _ECC_TABLES

def fix_m2f1_many(sectors):
    """EDC + ECC of many raw Mode 2 Form 1 sectors at once (numpy; same bytes as the ECMA-130 loops _edc /
    _ecc_part). `sectors` = list of 2352-byte strings; returns a list of fixed bytes."""
    import numpy as np
    if not sectors: return []
    T, F, B, IP, IQ = _ecc_tables()
    a = np.frombuffer(b''.join(sectors), np.uint8).reshape(len(sectors), 2352).copy()
    d = np.ascontiguousarray(a[:, 0x10:0x818].T); e = np.zeros(len(sectors), np.uint32)
    for row in d: e = (e >> 8) ^ T[(e ^ row) & 0xFF]
    a[:, 0x818:0x81C] = e.astype('<u4').view(np.uint8).reshape(-1, 4)
    hdr = a[:, 0xC:0x10].copy(); a[:, 0xC:0x10] = 0
    for I, dest in ((IP, 0x81C), (IQ, 0x8C8)):
        v = a[:, I]; x = np.zeros(v.shape[:2], np.uint8); y = np.zeros(v.shape[:2], np.uint8)
        for k in range(I.shape[1]):
            t = v[:, :, k]; x ^= t; y ^= t; x = F[x]
        x = B[F[x] ^ y]; n = I.shape[0]
        a[:, dest:dest + n] = x; a[:, dest + n:dest + 2 * n] = x ^ y
    a[:, 0xC:0x10] = hdr
    return [r.tobytes() for r in a]

class RawDisc:
    """Raw MODE2/2352 track 1 image held in memory (PS1: user data at +24). LBA n = sector n of the file."""
    UD = 24
    def __init__(self, path):
        self.d = bytearray(os.path.getsize(path))
        with open(path, 'rb') as f: f.readinto(self.d)
        self.touched = set(); self.free = PS1_FREE_LBA
    def read(self, lba, n, off=0):
        """n bytes at byte `off` of the file that starts at `lba`."""
        out = bytearray(); lba += off // 2048; off %= 2048
        for k in range((off + n + 2047) // 2048):
            o = (lba + k) * 2352 + self.UD; out += self.d[o:o + 2048]
        return bytes(out[off:off + n])
    def write(self, lba, off, data):
        """Write `data` at byte `off` of the file starting at `lba`; finish() fixes EDC/ECC of every touched sector."""
        p = 0
        while p < len(data):
            sec, so = lba + (off + p) // 2048, (off + p) % 2048
            n = min(2048 - so, len(data) - p); o = sec * 2352 + self.UD + so
            self.d[o:o + n] = data[p:p + n]; self.touched.add(sec); p += n
    def finish(self):
        secs = sorted(self.touched)
        raw = [bytes(self.d[sec * 2352:(sec + 1) * 2352]) for sec in secs]
        for sec, r in zip(secs, raw): assert r[18] & 0x20 == 0, f'sector {sec} is not Form 1'
        for sec, r in zip(secs, fix_m2f1_many(raw)): self.d[sec * 2352:(sec + 1) * 2352] = r
    def save(self, path):
        self.finish(); open(path, 'wb').write(self.d)
    def iso_record(self, path):
        """(record_lba, record_offset_in_sector, extent, size) of an ISO9660 file, e.g. 'OVL/STAGE08.BIN'."""
        pvd = self.read(16, 2048); ext, size = struct.unpack('<I', pvd[158:162])[0], struct.unpack('<I', pvd[166:170])[0]
        parts = path.upper().split('/')
        for k, name in enumerate(parts):
            want = (name + (';1' if k == len(parts) - 1 else '')).encode()
            for s in range((size + 2047) // 2048):
                sec = self.read(ext + s, 2048); i = 0; hit = None
                while i < 2048 and sec[i]:
                    L = sec[i]; nl = sec[i + 32]
                    if sec[i + 33:i + 33 + nl] == want: hit = (ext + s, i, struct.unpack('<I', sec[i + 2:i + 6])[0], struct.unpack('<I', sec[i + 10:i + 14])[0])
                    i += L
                if hit: break
            if not hit: raise KeyError(path)
            ext, size = hit[2], hit[3]
        return hit
    # ---- PS1 EXE helpers
    def exe_read(self, addr, n): return self.read(PS1_EXE_LBA, n, exe_file_off(addr))
    def exe_words(self, addr, n): return list(struct.unpack('<%dI' % n, self.exe_read(addr, 4 * n)))
    def exe_write(self, addr, data): self.write(PS1_EXE_LBA, exe_file_off(addr), data)
    def exe_write_words(self, addr, words): self.exe_write(addr, struct.pack('<%dI' % len(words), *words))
    def file_entry(self, i):
        """EXE file-table entry i: (LBA, size, type)."""
        return tuple(self.exe_words(PS1_FILETAB + 12 * i, 3))
    def set_file_entry(self, i, lba, size, typ=None):
        self.exe_write_words(PS1_FILETAB + 12 * i, [lba, size] + ([typ] if typ is not None else []))

# PS1 file table in the EXE: 139 x (u32 LBA, u32 size, u32 type) at 0x80136F7C, indexed by file number.
PS1_FILETAB = 0x80136F7C
PS1_EXE_LBA = 23
PS1_FREE_LBA = (141946, 142095)      # empty Form-1 sectors at the end of track 1 (142095 has a ripper-quirk ECC: not used)
PS1_TRACK1_SECTORS = 142096
PS1_EXTEND_SECTORS = 2048             # sectors added to track 1 by extend_track1 (1024 for Cut Man + 1024 for Bonus Mode)
PS1_END1_FILE = 138                   # file-table entry of END1.DA (CD audio track 2); played via 0x800FC78C only

def _bcd(v): return (v // 10) << 4 | v % 10
def m2_header(lba):
    """Sector header bytes 12..15 (BCD MSF of lba + 150, mode 2)."""
    a = lba + 150
    return bytes([_bcd(a // 4500), _bcd(a // 75 % 60), _bcd(a % 75), 2])

def extend_track1(disc, n=PS1_EXTEND_SECTORS):
    """Grow PS1 track 1 by n empty Form-1 sectors, inserted before its last sector (LBA 142095, kept byte for byte
    apart from its header address) so the free area stays one range: disc.free = (141946, 142095 + n). Mode 2 EDC/ECC
    don't cover the header, so the copied empty sector only needs a new address. Everything after the data moves by n:
    PVD volume size, the ISO records of END1.DA / ZNULL.DAT (CD audio tracks 2/3) and file-table entry 138, which is
    how the game finds END1.DA (0x800FC78C: entry 138 -> CdIntToPos -> CdlPlay; no other reference, `verify`).
    Must run first, on an unmodified image. With separate track files the .cue needs no change; a single-file
    multi-track image would need the sectors inserted the same way at the end of track 1."""
    assert len(disc.d) == PS1_TRACK1_SECTORS * 2352 and not disc.touched and disc.free == PS1_FREE_LBA
    q = PS1_FREE_LBA[1]
    tmpl = bytearray(disc.d[PS1_FREE_LBA[0] * 2352:(PS1_FREE_LBA[0] + 1) * 2352])
    assert not any(tmpl[16:2072]) and fix_m2f1(bytearray(tmpl)) == tmpl
    new = bytearray()
    for k in range(n):
        tmpl[12:16] = m2_header(q + k); new += tmpl
    disc.d[q * 2352:q * 2352] = new
    disc.d[(q + n) * 2352 + 12:(q + n) * 2352 + 16] = m2_header(q + n)
    pvd = disc.read(16, 2048); vs = struct.unpack('<I', pvd[80:84])[0]
    disc.write(16, 80, struct.pack('<I', vs + n) + struct.pack('>I', vs + n))
    for path in ('END1.DA', 'ZNULL.DAT'):
        rl, ro, ext, sz = disc.iso_record(path)
        disc.write(rl, ro + 2, struct.pack('<I', ext + n) + struct.pack('>I', ext + n))
    lba, size, typ = disc.file_entry(PS1_END1_FILE)
    assert lba == disc.iso_record('END1.DA')[2] - n
    disc.set_file_entry(PS1_END1_FILE, lba + n, size)
    disc.free = (PS1_FREE_LBA[0], q + n)
    return disc.free

def exe_file_off(addr): return 0x800 + addr - 0x800C0000

def pac_build(entries):
    """(type, data) list -> PAC bytes in the original layout (header sector, chunks 0x800-aligned, zero padding)."""
    body, hdr = b'', b''
    for t, b in entries:
        hdr += struct.pack('<2I', t, len(b)); body += b + b'\0' * (-len(b) % 0x800)
    total = 0x800 + len(body)
    head = struct.pack('<2I', len(entries), total) + hdr
    assert len(head) <= 0x800
    return head + b'\0' * (0x800 - len(head)) + body

def pac_entries(path): return [(t, b) for t, o, b in unpac(path)]

def pac_merge_grids(entries):
    """Merge chunks 0, 1, 2 (dests 0x8016EF34/F334/F734, contiguous, 0x400 each) into one type-0 chunk:
    same RAM contents, one sector less. Frees the sector chunk 3 needs to grow from 64 to 68 screens."""
    d = {t: b for t, b in entries}
    assert [t for t, _ in entries[:3]] == [0, 1, 2] and len(d[0]) == len(d[1]) == 0x400
    return [(0, d[0] + d[1] + d[2])] + entries[3:]

# MIPS encoders (only what the patches need)
def m_i(op, rs, rt, imm): return op << 26 | rs << 21 | rt << 16 | (imm & 0xFFFF)
def m_r(rs, rt, rd, sa, fn): return rs << 21 | rt << 16 | rd << 11 | sa << 6 | fn
def hi_lo(addr):
    lo = addr & 0xFFFF; return ((addr + 0x8000) >> 16) & 0xFFFF, lo - 0x10000 if lo & 0x8000 else lo
def patch_lui_addiu(word_lo, word_hi_insn, addr):
    """Keep registers of an existing lui/addiu pair, replace the address."""
    h, l = hi_lo(addr); return (word_hi_insn & 0xFFFF0000) | h, (word_lo & 0xFFFF0000) | (l & 0xFFFF)

# ----------------------------------------------------------------- Saturn image
class Sat:
    BASE2, BASE1 = 0x00200000, 0x06008000
    CODE_END = 0x0027C000
    def __init__(self):
        import capstone as c
        self.p2 = open(os.path.join(SAT_FILES, '2NDREAD.PRG'), 'rb').read()
        self.p1 = open(os.path.join(SAT_FILES, '1STREAD.PRG'), 'rb').read()
        self.md = c.Cs(c.CS_ARCH_SH, c.CS_MODE_SH2 | c.CS_MODE_BIG_ENDIAN)
    def _seg(self, a):
        if self.BASE2 <= a < self.BASE2 + len(self.p2): return self.p2, self.BASE2
        if self.BASE1 <= a < self.BASE1 + len(self.p1): return self.p1, self.BASE1
        return None, None
    def has(self, a): return self._seg(a)[0] is not None
    def raw(self, a, n): d, b = self._seg(a); return d[a - b:a - b + n]
    def u32(self, a): d, b = self._seg(a); return struct.unpack('>I', d[a - b:a - b + 4])[0]
    def u16(self, a): d, b = self._seg(a); return struct.unpack('>H', d[a - b:a - b + 2])[0]
    def s16(self, a): d, b = self._seg(a); return struct.unpack('>h', d[a - b:a - b + 2])[0]
    def dis_lines(self, a, n):
        d, b = self._seg(a); out = []
        for _ in range(n):
            try:
                _, _, mn, ops = next(self.md.disasm_lite(d[a - b:a - b + 2], a))
                s = f"{a:08x}: {self.u16(a):04x}  {mn} {ops}"
                if ops.startswith('0x') and mn in ('mov.l', 'mov.w'):
                    t = int(ops.split(',')[0], 16)
                    if self.has(t):
                        s += f"    ; ={self.u32(t):08x}" if mn == 'mov.l' else f"    ; ={self.s16(t):#x}"
            except StopIteration:
                s = f"{a:08x}: {self.u16(a):04x}  .word"
            out.append(s); a += 2
        return out
    def dis(self, a, n=40): return '\n'.join(self.dis_lines(a, n))
    def calls(self, lo, hi):
        """Ordered external call targets (jsr/jmp via literal pool) in [lo,hi)."""
        out, regs = [], {}
        for ln in self.dis_lines(lo, (hi - lo) // 2):
            m = re.search(r'mov\.l 0x([0-9a-f]+),(r\d+)\s+; =([0-9a-f]{8})', ln)
            if m: regs[m.group(2)] = int(m.group(3), 16)
            m = re.search(r'(jsr|jmp) @(r\d+)', ln)
            if m and m.group(2) in regs:
                t = regs[m.group(2)]
                if (self.BASE1 <= t < self.BASE1 + len(self.p1)) or (self.BASE2 <= t < self.CODE_END and not lo <= t < hi):
                    out.append(t)
        return out

# ----------------------------------------------------------------- PS1 image
class Img:
    def __init__(s, data, base): s.d, s.b, s.e = data, base, base + len(data)
    def has(s, a): return s.b <= a < s.e - 3
    def u32(s, a): return struct.unpack('<I', s.d[a - s.b:a - s.b + 4])[0]

class Ps1:
    EXE_BASE, OVL_BASE = 0x800C0000, 0x801D8000
    def __init__(self):
        import capstone as c
        raw = open(os.path.join(PS1_FILES, 'SLUS_004.53'), 'rb').read()
        self.exe = Img(raw[0x800:], self.EXE_BASE)
        self.md = c.Cs(c.CS_ARCH_MIPS, c.CS_MODE_MIPS32 | c.CS_MODE_LITTLE_ENDIAN); self.md.skipdata = True
    def ovl(self, name):
        return Img(open(os.path.join(PS1_FILES, 'OVL', name + '.BIN'), 'rb').read(), self.OVL_BASE)
    def dis(self, img, a, n=40):
        return '\n'.join(f"{i.address:08x}: {i.mnemonic:8s} {i.op_str}" for i in self.md.disasm(img.d[a - img.b:a - img.b + 4 * n], a))
    def calls(self, img, lo, hi):
        out = []
        for _, _, mn, ops in self.md.disasm_lite(img.d[lo - img.b:hi - img.b], lo):
            if mn == 'jal':
                t = int(ops, 16)
                if not lo <= t < hi: out.append(t)
        return out
    @staticmethod
    def lui_addiu(img, a):
        """Decode `lui rX,H` at a followed by `addiu rX,rX,L` at a+4 -> full address."""
        x, y = img.u32(a), img.u32(a + 4)
        assert x >> 26 == 0x0F and y >> 26 == 0x09, f'not lui/addiu at {a:#x}'
        lo = y & 0xFFFF; lo -= 0x10000 if lo & 0x8000 else 0
        return (((x & 0xFFFF) << 16) + lo) & 0xFFFFFFFF
    def find_enemy_table(self, img):
        """Each overlay holds 150 handler ptrs: P2 (98, types 0x00-0x61) then P3 (52). Unused = stub filler."""
        w = [struct.unpack('<I', img.d[i:i + 4])[0] for i in range(0, len(img.d) - 3, 4)]
        ok = lambda v: self.exe.b <= v < self.exe.e or img.b <= v < img.e
        for i in range(len(w) - 150):
            seg = w[i:i + 98]
            if all(ok(v) for v in seg):
                c = collections.Counter(seg).most_common(1)[0]
                if c[1] >= 30 and img.b <= c[0] < img.e:
                    return img.b + 4 * i
        return None

PS1_P2_LEN = 98                             # P2 entries per overlay table (types 0x00-0x61), then 52 P3
PS1_TABLE = {'STAGE09': (0x801DB784, 0x801D8198, 0x801D8220), 'STAGE08': (0x801E0CEC, 0x801D81B4, 0x801D823C)}
PS1_P5_TABLE, PS1_P5_LEN = 0x8015039C, 52   # EXE; e.g. P5[0x30] = 0x801E0C6C in STAGE08 (PS1-only Wood Man stand-in)

def load_helper_map(path=None):
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'helper_map.txt')
    mp = {}
    for ln in open(path):
        if ln.startswith('#'): continue
        p = ln.split()
        if len(p) > 1 and p[1] != '--------': mp[int(p[0], 16)] = int(p[1], 16)
    return mp

# ----------------------------------------------------------------- stage map / graphics formats
# chunk 4 block = 4 bytes. Saturn (BE word): tile*4 (8x8-cell number) in the high halfword, attr byte (bit 6 set), byte 3.
# PS1: u16 LE tile, attr byte with bit 6 cleared, byte 3 unchanged. Exact for every unedited stage (STAGE03 has 13 blocks
# with Saturn bit 6 = 0 that convert differently: not studied).
def block_sat_to_ps1(b4):
    hi = struct.unpack('>H', b4[0:2])[0]
    return struct.pack('<H', hi >> 2) + bytes([b4[2] & 0xBF, b4[3]])

def blocks_sat_to_ps1(chunk): return b''.join(block_sat_to_ps1(chunk[i:i + 4]) for i in range(0, len(chunk), 4))

# Graphics chunks 0x102 / 0x103: 16x16 tiles, 4 bpp, 128 bytes each; pixel pairs nibble-swapped between systems.
# Saturn 0x103 (map tiles): each tile = four 8x8 cells (TL, TR, BL, BR; 32 B, 4 B/row). Saturn 0x102: linear (8 B/row).
# PS1 (both): 8 texture pages of 256x256 px (128 B/line, 32 KB); tile t -> page t//256, tile row (t%256)//16, column t%16.
_NSW = bytes(((v & 15) << 4) | (v >> 4) for v in range(256))
_CELL_ORDER = [((y // 8) * 2 + (xb // 4)) * 32 + (y % 8) * 4 + (xb % 4) for y in range(16) for xb in range(8)]
def sat_tile(chunk, t, cells=True):
    """Saturn tile t -> 128 bytes in PS1 order (linear rows, nibbles swapped)."""
    seg = chunk[t * 128:t * 128 + 128]
    if cells: seg = bytes(seg[i] for i in _CELL_ORDER)
    return bytes(seg).translate(_NSW)
def ps1_tile_off(t, y):
    page, tt = divmod(t, 256); return page * 32768 + ((tt // 16) * 16 + y) * 128 + (tt % 16) * 8
def ps1_tile(chunk, t): return b''.join(chunk[ps1_tile_off(t, y):ps1_tile_off(t, y) + 8] for y in range(16))
def ps1_put_tile(buf, t, tile):
    for y in range(16): o = ps1_tile_off(t, y); buf[o:o + 8] = tile[y * 8:y * 8 + 8]
def gfx_sat_to_ps1(chunk, cells):
    out = bytearray(len(chunk))
    for t in range(len(chunk) // 128): ps1_put_tile(out, t, sat_tile(chunk, t, cells))
    return bytes(out)

def stage_block_map(stage):
    """Saturn->PS1 block-number map from aligning chunk-3 screens (same screen numbers on both systems)."""
    c3s, c3p = pac_chunks('sat', stage)[3], pac_chunks('ps1', stage)[3]
    S3 = [struct.unpack('>256H', c3s[k * 512:k * 512 + 512]) for k in range(len(c3s) // 512)]
    P3 = [struct.unpack('<256H', c3p[k * 512:k * 512 + 512]) for k in range(len(c3p) // 512)]
    bm = collections.defaultdict(collections.Counter)
    for i in range(min(len(S3), len(P3))):
        for x, y in zip(S3[i], P3[i]): bm[x][y] += 1
    return {x: c.most_common(1)[0][0] for x, c in bm.items()}, S3, P3

# ----------------------------------------------------------------- M2a: restore Wood Man's hallway + arena
# Saturn STAGE08 grid cells cut on PS1 (chunk, row, col) -> screen; screens 0x3E-0x43 deleted; screen 0x16 edited.
M2A_GRID_CELLS = {(0, 3, 0x12): 0x21, (0, 3, 0x13): 0x43, (0, 4, 0x11): 0x3E, (0, 4, 0x12): 0x3F, (0, 4, 0x13): 0x40,
                  (1, 4, 0x12): 0x41, (1, 4, 0x13): 0x42}
M2A_NEW_SCREENS = list(range(0x3E, 0x44))
M2A_REPLACED_SCREENS = [0x16]
M2A_TILE_FIRST = 0x6FE            # first blank PS1 STAGE08 0x103 tile slot (0x6FE-0x7FF free)
# Placement records, read from the discs by position (PS1 byte layout): the PS1 stand-in P5 0x30 (PS1 STAGE08 record
# 108) removed, the five Saturn records restored (Saturn STAGE08 records 96, 100, 98, 97, 101: two gates, P5 0x04,
# P5 0x0A, Wood Man; 03 §3).
M2A_STANDIN_IDX, M2A_SAT_IDX, M2A_FP = 108, (96, 100, 98, 97, 101), 'a2d3f8e4fdd6'
def m2a_standin(): return place_rec('ps1', 'STAGE08', M2A_STANDIN_IDX)
def m2a_records(): return [place_rec('sat', 'STAGE08', i) for i in M2A_SAT_IDX]

def restore_stage_map(stage, cells, new_screens, replaced_screens, tile_first, door_blocks=0):
    """Rebuild a PS1 stage PAC's chunks 0, 1, 3, 4, 0x103 with screens the PS1 cut restored from the Saturn.
    cells = {(chunk, row, col): screen} grid cells to set back; new_screens = Saturn screens appended (in order, at
    their own numbers); replaced_screens = PS1 screens replaced by the Saturn version. Returns ({type: bytes}, info).
    Screen entries = block index (low 12 bits) | flags (bits 12-15, e.g. 0x1000 in STAGE09; kept as they are).
    A Saturn block is reused only as the PS1's own equivalent block (block map, same attribute bytes, pixel-identical
    tile); a new block uses the PS1's own equivalent tile (tile map from the block map) when pixel-identical, otherwise
    a fresh copy in the blank, unused 0x103 slots from tile_first (the chunk grows by whole tile rows = sectors if the
    slots lie past its end). door_blocks = n: Saturn blocks 1..n are appended consecutively (info['door_blocks'] =
    index of the first) for block patches whose numbers the PS1 renumbered (STAGE08).
    Reusing ANY pixel-identical PS1 tile is wrong: some of those slots (e.g. 0xB2, 0x1C7, 0x1BD, 0x1BE) are
    overwritten at run time (animated tiles), which garbles the background at both gates."""
    s, p = pac_chunks('sat', stage), pac_chunks('ps1', stage)
    bm_full, S3, P3 = stage_block_map(stage)
    bm = {}
    for x, y in bm_full.items():
        assert x & 0xF000 == y & 0xF000, (stage, hex(x), hex(y))
        bm.setdefault(x & 0xFFF, y & 0xFFF)
    sblk = lambda n: s[4][4 * n:4 * n + 4]
    stile = lambda n: struct.unpack('>H', sblk(n)[:2])[0] >> 2
    spx = lambda t: sat_tile(s[0x103], t, True)
    g103 = bytearray(p[0x103]); blocks = bytearray(p[4])
    nb0 = len(blocks) // 4
    ptile = lambda y: struct.unpack('<H', p[4][4 * y:4 * y + 2])[0]
    used = {ptile(y) for y in range(nb0)}
    tmap = {}
    for x, y in bm.items():                               # Saturn tile -> the PS1's own tile for it
        if 4 * x < len(s[4]) and y < nb0: tmap.setdefault(stile(x), ptile(y))
    bidx = {}
    nxt_tile = tile_first; new_tiles = {}; new_blocks = {}; reused = 0
    def ps1_tile_for(t):
        nonlocal nxt_tile
        if t in tmap and ps1_tile(g103, tmap[t]) == spx(t): return tmap[t]
        if t in new_tiles: return new_tiles[t]
        assert nxt_tile < 0x800, 'out of tile slots'
        while len(g103) < ps1_tile_off(nxt_tile, 15) + 8: g103.extend(bytes(0x800))
        assert nxt_tile not in used and not any(ps1_tile(g103, nxt_tile)), f'tile slot {nxt_tile:#x} in use'
        ps1_put_tile(g103, nxt_tile, spx(t)); new_tiles[t] = nxt_tile; nxt_tile += 1
        return nxt_tile - 1
    bmap = {}
    def ps1_block_for(v):
        nonlocal reused
        x = v & 0xFFF
        if x in bmap: return bmap[x] | (v & 0xF000)
        conv = block_sat_to_ps1(sblk(x))
        want = struct.pack('<H', ps1_tile_for(stile(x))) + conv[2:]
        y = bm.get(x)
        if y is not None and y < nb0 and blocks[4 * y + 2:4 * y + 4] == conv[2:] and \
           ps1_tile(g103, struct.unpack('<H', blocks[4 * y:4 * y + 2])[0]) == spx(stile(x)):
            reused += 1
        elif want in bidx: y = bidx[want]
        else:
            y = len(blocks) // 4; blocks.extend(want); bidx[want] = y; new_blocks[x] = y
        bmap[x] = y; return y | (v & 0xF000)
    scr = bytearray(p[3])
    for k in replaced_screens:
        scr[k * 512:k * 512 + 512] = struct.pack('<256H', *(ps1_block_for(x) for x in S3[k]))
    for k in new_screens:
        assert len(scr) == k * 512
        scr += struct.pack('<256H', *(ps1_block_for(x) for x in S3[k]))
    door0 = len(blocks) // 4
    for x in range(1, door_blocks + 1):
        conv = block_sat_to_ps1(sblk(x))
        blocks.extend(struct.pack('<H', ps1_tile_for(stile(x))) + conv[2:])
    grids = {0: bytearray(p[0]), 1: bytearray(p[1]), 2: bytearray(p[2])}
    for (c, r, col), v in cells.items():
        assert grids[c][r * 32 + col] == 0 and s[c][r * 32 + col] == v
        grids[c][r * 32 + col] = v
    assert len(blocks) <= 4 * 2048 and len(scr) <= 240 * 512
    info = dict(screens=len(scr) // 512, blocks=len(blocks) // 4, new_blocks=len(new_blocks), reused_blocks=reused,
                new_tiles=len(new_tiles), tile_range=(tile_first, nxt_tile - 1), bmap=bmap, tmap=new_tiles,
                door_blocks=door0, tiles_chunk=len(g103) // 128)
    return {0: bytes(grids[0]), 1: bytes(grids[1]), 2: bytes(grids[2]), 3: bytes(scr), 4: bytes(blocks), 0x103: bytes(g103)}, info

def restore_stage08_map():
    """Wood Man's hallway and arena (M2a): 7 grid cells, screens 0x3E-0x43 appended, screen 0x16 (the PS1 closed the
    corridor there) replaced; new tiles from slot 0x6FE; the 32 door blocks appended (patch_wood_door_lists)."""
    return restore_stage_map('STAGE08', M2A_GRID_CELLS, M2A_NEW_SCREENS, M2A_REPLACED_SCREENS, M2A_TILE_FIRST, 0x20)

def swap32(b):
    assert len(b) % 4 == 0
    return b''.join(b[i:i + 4][::-1] for i in range(0, len(b), 4))

def sprite_base_sat_to_ps1(i, v):
    """Chunk 7 (u16 per sprite set) Saturn -> PS1. Rule from every stage pair: 0 -> 0 (entry 2: 0 -> 0x500),
    0x0B0-0xAD0 -> +0x500, 0xB40-0xD00 -> +0xA00. The Saturn sprite tiles from 0xB00 on are the 0x101 chunk
    (LABO / SLABO: 768 tiles, Saturn 0xB00-0xDFF -> PS1 VRAM (0x140, 0x100) = tile 0x1500), so 0xB00 -> +0xA00 too
    (SLABO's text set 0xA9 = 0xB00; LABO 0xC00 -> 0x1600 on the PS1 disc)."""
    if v == 0: return 0x500 if i == 2 else 0
    assert v <= 0xAD0 or v >= 0xB00, hex(v)
    return v + (0x500 if v < 0xB00 else 0xA00)

def restore_stage08_sprites():
    """PS1 STAGE08 lost 14 sprite-set entries (0x9E-0xAB) with Wood Man: chunk 5 tail (sprite definitions), chunk 6/7/8
    tails, and 545 sprite tiles of 0x102 (0x330-0x5FF, blank on PS1). Entry 0xA8 is the tall boss-gate sprite read by
    the shared gate code (EXE 0x80129308: chunk-7 RAM 0x80170498), so restored gates need it. Returns {type: bytes}, info."""
    return restore_stage_sprites('STAGE08')

def restore_stage_sprites(stage):
    """Sprite sets the PS1 cut off the end of a stage's chunks 5/6/7/8 (Saturn tails converted onto the PS1 prefixes),
    0x102 sprite tiles blank on the PS1 (converted Saturn chunk; every differing tile must be blank on the PS1) and
    palettes the PS1 zeroed (restored with STP clear). STAGE08: Wood Man, leaves, tall gate; STAGE09: Cut Man (set 0xAA)."""
    s, p = pac_chunks('sat', stage), pac_chunks('ps1', stage)
    out = {}
    c5 = swap32(s[5]); assert c5[:len(p[5])] == p[5]; out[5] = c5
    c6 = swap32(s[6]); assert c6[:len(p[6])] == p[6]; out[6] = c6
    n7 = len(s[7]) // 2; a7 = struct.unpack('>%dH' % n7, s[7])
    c7 = struct.pack('<%dH' % n7, *(sprite_base_sat_to_ps1(i, v) for i, v in enumerate(a7))); assert c7[:len(p[7])] == p[7]; out[7] = c7
    assert s[8][:len(p[8])] == p[8]; out[8] = s[8]
    g = gfx_sat_to_ps1(s[0x102], False)
    diff = [t for t in range(2048) if ps1_tile(g, t) != ps1_tile(p[0x102], t)]
    assert all(not any(ps1_tile(p[0x102], t)) for t in diff)
    out[0x102] = g
    # palettes: the PS1 zeroed STAGE08 chunk-9 palettes 0x30-0x33 (Wood Man's set 0xAB uses chunk-8 palette 0x30),
    # so the sprites drew fully transparent (only the hit flash showed). Colours are BGR555 on both systems; restored
    # with the STP bit clear, like the PS1's other sprite palettes copied unchanged from the Saturn (e.g. 0x2D).
    # STAGE09: the PS1 zeroed Cut Man's sprite palettes 0x30-0x32 and replaced background palettes 0x5B / 0x61 (used
    # only by the cut screens 0x15 (Saturn version) / 0x19) with the filler ramp both systems keep in unused slots
    # (colours 1-15 = xx03); PS1 background palettes (>= 0x40) have STP set on every non-zero colour, sprite palettes not.
    # Shared background palettes the PS1 recoloured (STAGE09 0x48/0x4D/0x54/0x5A) keep the PS1 colours.
    a9 = struct.unpack('>2048H', s[9]); p9 = list(struct.unpack('<2048H', p[9])); pals = []
    filler = lambda pal: all(v & 0x3FF == 3 for v in pal[1:])
    for k in range(128):
        sp, pp = a9[16 * k:16 * k + 16], p9[16 * k:16 * k + 16]
        if any(sp) and (not any(pp) or (filler(pp) and not filler(sp))):
            p9[16 * k:16 * k + 16] = [(v | 0x8000) if v and k >= 0x40 else v & 0x7FFF for v in sp]; pals.append(k)
    out[9] = struct.pack('<2048H', *p9)
    return out, dict(sprite_tiles=len(diff), tile_span=(diff[0], diff[-1]) if diff else None, palettes=pals,
                     sizes={t: (len(p[t]), len(out[t])) for t in (5, 6, 7, 8)})

def pac_strings(c):
    """Chunk 0x12 (stage text, RAM 0x800A0000): u16 LE offset table (first offset = table size) + strings."""
    n = struct.unpack('<H', c[:2])[0] // 2
    o = list(struct.unpack('<%dH' % n, c[:2 * n])) + [len(c)]
    return [c[o[k]:o[k + 1]] for k in range(n)]

def restore_stage08_text():
    """The PS1 cut text entry 17 of STAGE08 chunk 0x12: Wood Man's conversation (h_0601346C(17) starts talk 17).
    Same text format on both systems (STAGE09 keeps Cut Man's talks unchanged), so the Saturn chunk is used as is."""
    s, p = pac_chunks('sat', 'STAGE08')[0x12], pac_chunks('ps1', 'STAGE08')[0x12]
    ss, ps = pac_strings(s), pac_strings(p)
    assert len(ss) == 18 and len(ps) == 17 and ss[:17] == ps and b'Woodman' in ss[17]
    return {0x12: s}, dict(text_entries=(17, 18))

def pac_trim_vram(entries, typ, nsec):
    """Drop the last nsec sectors of a VRAM chunk (they must be zero). The VRAM streamer (0x800FBD94) uploads one
    64x16 rect per sector, column by column, and simply stops early: the trimmed VRAM keeps old contents (only safe
    for unused tile slots)."""
    out = []
    for t, b in entries:
        if t == typ:
            assert len(b) % 0x800 == 0 and not any(b[len(b) - nsec * 0x800:]); b = b[:len(b) - nsec * 0x800]
        out.append((t, b))
    return out

def m2a_placements(chunk):
    """Chunk 0x0A with the PS1 stand-in removed and the Saturn gate/trigger/Wood Man records inserted before
    the terminator."""
    recs = [chunk[i:i + 8] for i in range(0, placement_terminator(chunk), 8)]
    standin, new = m2a_standin(), m2a_records()
    assert fp(standin + b''.join(new)) == M2A_FP, 'unexpected STAGE08 placement records'
    assert recs.count(standin) == 1
    recs.remove(standin)
    out = b''.join(recs + new) + bytes([0, 0, 0, 0xFF]) + bytes(4)
    assert len(out) <= len(chunk)
    return out + bytes(len(chunk) - len(out))

def pac_locate(disc, path):
    """(lba, size, [(type, offset_in_file, size)]) of a PAC on the raw disc."""
    rl, ro, ext, sz = disc.iso_record(path)
    head = disc.read(ext, 0x800); n = struct.unpack('<I', head[:4])[0]; off = 0x800; out = []
    for i in range(n):
        t, s = struct.unpack('<II', head[8 + 8 * i:16 + 8 * i]); out.append((t, off, s)); off += (s + 0x7FF) & ~0x7FF
    return ext, sz, out

# Block patches: write_patch(id, x, y) at EXE 0x80102EF4 takes per-stage patch lists (0x8013C620[stage] ->
# [id] -> 8-byte records u16 list, u8 flags, u8 count, s16 dx, s16 dy; end 0xFFFF) and writes the block numbers of EXE
# list 0x8013C33C[list] into the live screen (vertical, or horizontal if flags & 4). Stage 8 patches 4-7 = Wood Man's
# doors (closed / open, at (0x10E0, 0x420) and (0x1200, 0x490)), lists 176-183, Saturn block numbers 0x01-0x20.
WM_DOOR_LISTS = list(range(176, 184))
PS1_BLOCK_LISTS = 0x8013C33C

def patch_wood_door_lists(disc, door0):
    """Repoint the block numbers of EXE lists 176-183 (only used by Wood Man's doors) from Saturn numbering
    (0x01-0x20, which the PS1 STAGE08 renumbered) to the appended door blocks door0 + n - 1."""
    for lid in WM_DOOR_LISTS:
        lp = disc.exe_words(PS1_BLOCK_LISTS + 4 * lid, 1)[0]
        cur = struct.unpack('<4H', disc.exe_read(lp, 8))
        assert all(1 <= v <= 0x20 for v in cur), (lid, cur)
        disc.exe_write(lp, struct.pack('<4H', *(door0 + v - 1 for v in cur)))
    return door0

def patch_m2a(disc):
    """Rebuild STAGE08.PAC in place (same LBA and size): hallway/arena map, cut sprite sets, palettes, Wood Man's
    talk, Saturn placement records, door blocks; patch STAGE08B.PAC's placement chunk in place."""
    ext, sz, _ = pac_locate(disc, 'STDATA/STAGE08.PAC')
    path = os.path.join(PS1_FILES, 'STDATA', 'STAGE08.PAC')
    assert disc.read(ext, sz) == _file(path), 'STAGE08.PAC already patched?'
    ents = pac_entries(path)
    new, info = restore_stage08_map()
    spr, sinfo = restore_stage08_sprites(); new.update(spr); info.update(sinfo)
    txt, tinfo = restore_stage08_text(); new.update(txt); info.update(tinfo)
    ents = [(t, new.get(t, b)) for t, b in ents]
    ents = [(t, m2a_placements(b) if t == 0xA else b) for t, b in ents]
    ents = pac_merge_grids(ents)
    over = (len(pac_build(ents)) - sz) // 0x800
    if over > 0:                                   # make room: trim blank sectors at the end of the map tiles
        ents = pac_trim_vram(ents, 0x103, over); info['trimmed_0x103_sectors'] = over
    pac = pac_build(ents)
    assert len(pac) == sz, f'restored STAGE08.PAC is {len(pac):#x} bytes, slot is {sz:#x}'
    disc.write(ext, 0, pac)
    patch_wood_door_lists(disc, info['door_blocks'])
    ext_b, sz_b, chunks_b = pac_locate(disc, 'STDATA/STAGE08B.PAC')
    t, off, s = next(c for c in chunks_b if c[0] == 0xA)
    disc.write(ext_b, off, m2a_placements(disc.read(ext_b, s, off)))
    info.update(pac_lba=ext, pac_size=sz, pac08b_lba=ext_b)
    return info

# ----------------------------------------------------------------- C toolchain for PS1 code (M2 prep)
_CROSS = os.environ.get('MM8_CROSS', 'mipsel-linux-gnu-')   # toolchain prefix (the Windows bundle ships its own mipsel-linux-gnu GCC)
CC = _CROSS + 'gcc'; LD = _CROSS + 'ld'; OBJCOPY = _CROSS + 'objcopy'

CFLAGS = ['-march=r3000', '-mabi=32', '-mfp32', '-msoft-float', '-EL', '-O2', '-G0', '-mno-abicalls', '-fno-pic',
          '-mno-check-zero-division', '-fno-builtin', '-ffreestanding', '-nostdlib', '-ffunction-sections',
          '-fno-asynchronous-unwind-tables']

def compile_c(sources, base, symbols, entry_first=None, workdir=None, include=None):
    """Compile C sources for the R3000 and link at `base`. `symbols` = {name: absolute address} for engine
    helpers / globals. Returns (bytes, {symbol: address})."""
    import subprocess, tempfile
    wd = workdir or tempfile.mkdtemp(prefix='mm8cc_')
    ld = os.path.join(wd, 'link.ld')
    with open(ld, 'w') as f:
        for k, v in symbols.items(): f.write(f'{k} = {v:#x};\n')
        first = f'*(.text.{entry_first}) ' if entry_first else ''
        f.write('SECTIONS {\n  . = %#x;\n  .text : { %s*(.text*) }\n  .rodata : { *(.rodata*) }\n  .data : { *(.data*) *(.sdata*) }\n'
                '  .bss : { *(.bss*) *(.sbss*) *(COMMON) }\n  /DISCARD/ : { *(.MIPS.*) *(.reginfo) *(.pdr) *(.comment) *(.gnu.attributes) *(.note*) *(.mdebug*) }\n}\n' % (base, first))
    objs = [os.path.join(wd, os.path.basename(src) + '.o') for src in sources]
    def cc(src, o): subprocess.run([CC] + CFLAGS + (['-I', include] if include else []) + ['-c', src, '-o', o], check=True)
    if len(sources) > 1:                                  # independent compiles run side by side
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(min(len(sources), os.cpu_count() or 2)) as ex: list(ex.map(cc, sources, objs))
    else:
        for src, o in zip(sources, objs): cc(src, o)
    elf = os.path.join(wd, 'out.elf'); binf = os.path.join(wd, 'out.bin')
    subprocess.run([LD, '-EL', '-T', ld, '-Map', os.path.join(wd, 'out.map')] + objs + ['-o', elf], check=True)
    data, syms = elf_image(elf)
    with open(binf, 'wb') as f: f.write(data)
    return data, syms

def elf_image(elf):
    """(binary, {symbol: address}) of a linked little-endian ELF32, read directly (what `objcopy -O binary` and `nm`
    give: the allocated sections with contents from the lowest address, gaps zero-filled; every defined symbol except
    file and section symbols). Saves two process starts per link."""
    d = open(elf, 'rb').read()
    shoff, = struct.unpack_from('<I', d, 0x20); shentsize, shnum = struct.unpack_from('<2H', d, 0x2E)
    sh = [struct.unpack_from('<10I', d, shoff + k * shentsize) for k in range(shnum)]
    load = [(addr, d[off:off + size]) for _, typ, flags, addr, off, size, *_ in sh if flags & 2 and typ != 8 and size]
    base = min(a for a, _ in load); out = bytearray(max(a + len(b) for a, b in load) - base)
    for a, b in load: out[a - base:a - base + len(b)] = b
    syms = {}
    for _, typ, _, _, off, size, link, _, _, ent in sh:
        if typ != 2: continue                                   # SHT_SYMTAB
        stro = sh[link][4]
        for q in range(off, off + size, ent):
            name, value, _, info, _, shndx = struct.unpack_from('<3I2BH', d, q)
            if not name or shndx == 0 or info & 15 in (3, 4): continue     # unnamed, undefined, section, file
            syms[d[stro + name:d.index(b'\0', stro + name)].decode()] = value
    return bytes(out), syms

MIPS1_BAD = {'beql', 'bnel', 'beqzl', 'bnezl', 'blezl', 'bgtzl', 'bltzl', 'bgezl', 'bltzall', 'bgezall', 'll', 'sc', 'sync', 'teq', 'tne',
             'tge', 'tgeu', 'tlt', 'tltu', 'teqi', 'tnei', 'movn', 'movz', 'mul', 'madd', 'maddu', 'msub', 'msubu', 'clz', 'clo',
             'ldc1', 'sdc1', 'lwc1', 'swc1'}
_LOADS = {'lb', 'lbu', 'lh', 'lhu', 'lw', 'lwl', 'lwr'}

def check_r3000(data, base, code_end=None):
    """Static R3000 check of compiled code: no MIPS II+ opcodes, load-delay slot not reading the loaded
    register, no mult/div within 2 instructions after mfhi/mflo. Returns a list of problems (empty = OK)."""
    import capstone as c
    md = c.Cs(c.CS_ARCH_MIPS, c.CS_MODE_MIPS32 | c.CS_MODE_LITTLE_ENDIAN); md.detail = True
    ins = list(md.disasm(data[:(code_end - base) if code_end else len(data)], base)); bad = []
    for k, i in enumerate(ins):
        if i.mnemonic in MIPS1_BAD: bad.append(f'{i.address:#x}: {i.mnemonic} is not MIPS I')
        if i.mnemonic in _LOADS and k + 1 < len(ins):
            dst = i.op_str.split(',')[0].strip(); nxt = ins[k + 1]
            if dst != '$zero' and dst in [o.strip().split('(')[-1].rstrip(')') for o in nxt.op_str.split(',')[1:]] + ([nxt.op_str.split(',')[0].strip()] if nxt.mnemonic in ('sw', 'sh', 'sb', 'jr', 'jalr') else []):
                bad.append(f'{nxt.address:#x}: uses {dst} in the load delay slot of {i.address:#x}')
        if i.mnemonic in ('mfhi', 'mflo'):
            for j in ins[k + 1:k + 3]:
                if j.mnemonic in ('mult', 'multu', 'div', 'divu', 'mthi', 'mtlo'): bad.append(f'{j.address:#x}: {j.mnemonic} too close to {i.mnemonic}')
    return bad

def helper_symbols(mp=None):
    """Linker symbols h_<satAddr> = PS1 address for every mapped helper (plus MANUAL names)."""
    mp = mp or load_helper_map()
    return {f'h_{k:08x}': v for k, v in mp.items()}

# ----------------------------------------------------------------- SH-2 -> C lifter (M2)
# Wood Man (0x63) and his leaf (0x64) are ported by lifting the Saturn SH-2 code to C (one C function per handler,
# registers as locals, memory accesses translated at lift time) and compiling it with mipsel GCC; the result is checked
# against the original Saturn code by a differential test (SH-2 interpreter vs unicorn MIPS, `difftest-m2`).

def s8(v): v &= 0xFF; return v - 0x100 if v & 0x80 else v
def s12(v): v &= 0xFFF; return v - 0x1000 if v & 0x800 else v
def s16(v): v &= 0xFFFF; return v - 0x10000 if v & 0x8000 else v

DELAYED = {'bra', 'bsr', 'jmp', 'jsr', 'rts', 'bt/s', 'bf/s', 'braf', 'bsrf'}

class Ins:
    __slots__ = ('a', 'op', 'mn', 'n', 'm', 'imm', 'disp', 'tgt', 'pc')
    def __init__(s, a, op, mn, n=None, m=None, imm=None, disp=None, tgt=None, pc=None):
        s.a, s.op, s.mn, s.n, s.m, s.imm, s.disp, s.tgt, s.pc = a, op, mn, n, m, imm, disp, tgt, pc
    def __repr__(s):
        f = [f'r{s.n}' if s.n is not None else None, f'r{s.m}' if s.m is not None else None,
             f'#{s.imm}' if s.imm is not None else None, f'd{s.disp}' if s.disp is not None else None,
             f'->{s.tgt:#x}' if s.tgt is not None else None, f'@pc{s.pc:#x}' if s.pc is not None else None]
        return f'{s.a:08x} {s.mn} ' + ','.join(x for x in f if x)

def decode(op, a):
    n, m = (op >> 8) & 15, (op >> 4) & 15
    h, l = op >> 12, op & 15
    I = lambda mn, **k: Ins(a, op, mn, **k)
    if h == 0:
        if op == 0x0009: return I('nop')
        if op == 0x000B: return I('rts')
        if op == 0x0008: return I('clrt')
        if op == 0x0018: return I('sett')
        if op == 0x0019: return I('div0u')
        if op == 0x0028: return I('clrmac')
        if l == 4: return I('mov.b Rm,@(R0,Rn)', n=n, m=m)
        if l == 5: return I('mov.w Rm,@(R0,Rn)', n=n, m=m)
        if l == 6: return I('mov.l Rm,@(R0,Rn)', n=n, m=m)
        if l == 7: return I('mul.l', n=n, m=m)
        if l == 0xC: return I('mov.b @(R0,Rm),Rn', n=n, m=m)
        if l == 0xD: return I('mov.w @(R0,Rm),Rn', n=n, m=m)
        if l == 0xE: return I('mov.l @(R0,Rm),Rn', n=n, m=m)
        lo = op & 0xFF
        if lo == 0x29: return I('movt', n=n)
        if lo == 0x0A: return I('sts mach', n=n)
        if lo == 0x1A: return I('sts macl', n=n)
        if lo == 0x2A: return I('sts pr', n=n)
        if lo == 0x03: return I('bsrf', n=n)
        if lo == 0x23: return I('braf', n=n)
    elif h == 1: return I('mov.l Rm,@(disp,Rn)', n=n, m=m, disp=l * 4)
    elif h == 2:
        t = {0: 'mov.b Rm,@Rn', 1: 'mov.w Rm,@Rn', 2: 'mov.l Rm,@Rn', 4: 'mov.b Rm,@-Rn', 5: 'mov.w Rm,@-Rn', 6: 'mov.l Rm,@-Rn',
             7: 'div0s', 8: 'tst', 9: 'and', 0xA: 'xor', 0xB: 'or', 0xD: 'xtrct', 0xE: 'mulu.w', 0xF: 'muls.w'}.get(l)
        if t: return I(t, n=n, m=m)
    elif h == 3:
        t = {0: 'cmp/eq', 2: 'cmp/hs', 3: 'cmp/ge', 4: 'div1', 5: 'dmulu.l', 6: 'cmp/hi', 7: 'cmp/gt', 8: 'sub', 0xA: 'subc',
             0xC: 'add', 0xD: 'dmuls.l', 0xE: 'addc'}.get(l)
        if t: return I(t, n=n, m=m)
    elif h == 4:
        lo = op & 0xFF
        t = {0x00: 'shll', 0x10: 'dt', 0x20: 'shal', 0x01: 'shlr', 0x11: 'cmp/pz', 0x21: 'shar', 0x04: 'rotl', 0x24: 'rotcl',
             0x05: 'rotr', 0x15: 'cmp/pl', 0x25: 'rotcr', 0x08: 'shll2', 0x18: 'shll8', 0x28: 'shll16', 0x09: 'shlr2',
             0x19: 'shlr8', 0x29: 'shlr16', 0x0B: 'jsr', 0x2B: 'jmp', 0x22: 'sts.l pr,@-Rn', 0x26: 'lds.l @Rm+,pr',
             0x12: 'sts.l macl,@-Rn', 0x16: 'lds.l @Rm+,macl', 0x1A: 'lds macl', 0x0A: 'lds mach', 0x2A: 'lds pr'}.get(lo)
        if t: return I(t, n=n)
    elif h == 5: return I('mov.l @(disp,Rm),Rn', n=n, m=m, disp=l * 4)
    elif h == 6:
        t = {0: 'mov.b @Rm,Rn', 1: 'mov.w @Rm,Rn', 2: 'mov.l @Rm,Rn', 3: 'mov', 4: 'mov.b @Rm+,Rn', 5: 'mov.w @Rm+,Rn',
             6: 'mov.l @Rm+,Rn', 7: 'not', 8: 'swap.b', 9: 'swap.w', 0xA: 'negc', 0xB: 'neg', 0xC: 'extu.b', 0xD: 'extu.w',
             0xE: 'exts.b', 0xF: 'exts.w'}[l]
        return I(t, n=n, m=m)
    elif h == 7: return I('add #imm', n=n, imm=s8(op))
    elif h == 8:
        if n == 0: return I('mov.b R0,@(disp,Rn)', n=m, disp=l)
        if n == 1: return I('mov.w R0,@(disp,Rn)', n=m, disp=l * 2)
        if n == 4: return I('mov.b @(disp,Rm),R0', m=m, disp=l)
        if n == 5: return I('mov.w @(disp,Rm),R0', m=m, disp=l * 2)
        if n == 8: return I('cmp/eq #imm', imm=s8(op))
        if n in (9, 0xB, 0xD, 0xF):
            return I({9: 'bt', 0xB: 'bf', 0xD: 'bt/s', 0xF: 'bf/s'}[n], tgt=a + 4 + s8(op) * 2)
    elif h == 9: return I('mov.w @(disp,PC),Rn', n=n, pc=a + 4 + (op & 0xFF) * 2)
    elif h == 0xA: return I('bra', tgt=a + 4 + s12(op) * 2)
    elif h == 0xB: return I('bsr', tgt=a + 4 + s12(op) * 2)
    elif h == 0xC:
        t = {8: 'tst #imm', 9: 'and #imm', 0xA: 'xor #imm', 0xB: 'or #imm'}.get(n)
        if t: return I(t, imm=op & 0xFF)
        if n == 7: return I('mova', pc=((a + 4) & ~3) + (op & 0xFF) * 4)
    elif h == 0xD: return I('mov.l @(disp,PC),Rn', n=n, pc=((a + 4) & ~3) + (op & 0xFF) * 4)
    elif h == 0xE: return I('mov #imm', n=n, imm=s8(op))
    raise ValueError(f'{a:08x}: unsupported SH-2 opcode {op:04x}')

class Code:
    """Read-only big-endian code/data image: dict of (base, bytes)."""
    def __init__(self, segs): self.segs = segs
    def _f(self, a, n):
        for b, d in self.segs:
            if b <= a and a + n <= b + len(d): return d[a - b:a - b + n]
        raise KeyError(f'{a:#x}')
    def u8(self, a): return self._f(a, 1)[0]
    def u16(self, a): return struct.unpack('>H', self._f(a, 2))[0]
    def u32(self, a): return struct.unpack('>I', self._f(a, 4))[0]
    def ins(self, a): return decode(self.u16(a), a)

def successors(i):
    """Control-flow successors of a branch/jump instruction (excluding its delay slot), as (kind, target)."""
    mn = i.mn
    if mn in ('bt', 'bf'): return [('br', i.tgt), ('ft', i.a + 2)]
    if mn in ('bt/s', 'bf/s'): return [('br', i.tgt), ('ft', i.a + 4)]
    if mn == 'bra': return [('br', i.tgt)]
    if mn == 'bsr': return [('call', i.tgt), ('ft', i.a + 4)]
    return None

def discover(code, entries, tables, helpers_ok, stops=()):
    """Find all reachable instructions from `entries`. tables: {table_addr: n_entries} of code-pointer tables
    (their entries become entries too). helpers_ok(addr) -> True for engine helpers (not followed). stops = Saturn code
    addresses that are external functions (bsr / bra to them = call / tail call of a replacement; M7b).
    Returns (insns {addr: Ins}, entry set, call targets set)."""
    seen, work = {}, list(entries)
    ents = set(entries)
    for t, n in tables.items():
        for k in range(n):
            e = code.u32(t + 4 * k); ents.add(e); work.append(e)
    calls = set()
    # PC-relative literal for jsr/jmp targets: resolved in analyse(); here follow constant targets loaded right before
    while work:
        a = work.pop()
        while a not in seen:
            i = code.ins(a); seen[a] = i
            mn = i.mn
            if mn in DELAYED:
                d = code.ins(a + 2); seen[a + 2] = d
                if d.mn in DELAYED: raise ValueError(f'{a:08x}: branch in delay slot')
            succ = successors(i)
            if mn == 'rts' or mn in ('jmp', 'braf'):
                break
            if mn == 'jsr' or mn == 'bsrf':
                a += 4; continue
            if succ is None:
                a += 2; continue
            for k, t in succ:
                if t in stops:
                    assert k in ('call', 'br') and (k == 'call' or mn == 'bra'), f'{a:08x}: conditional branch to external {t:08x}'
                    continue
                if k == 'call': calls.add(t); ents.add(t); work.append(t)
                elif k == 'br': work.append(t)
            ft = [t for k, t in succ if k == 'ft']
            if not ft: break
            a = ft[0]
    return seen, ents, calls

# ---------------------------------------------------------------- abstract interpretation
CALLER_SAVED = range(0, 8)

def _addr_like(v):
    return 0x00200000 <= v < 0x00300000 or 0x06000000 <= v < 0x06100000

class State:
    __slots__ = ('r', 'slots')
    def __init__(s, r, slots): s.r, s.slots = r, slots
    def copy(s): return State(list(s.r), dict(s.slots))
    def join(s, o):
        ch = False
        for k in range(16):
            if s.r[k] != o.r[k] and s.r[k] is not None: s.r[k] = None; ch = True
        for k in list(s.slots):
            if o.slots.get(k) != s.slots[k]: del s.slots[k]; ch = True
        return ch
    def key(s): return (tuple(s.r), tuple(sorted(s.slots.items())))

def _c(v): return ('c', v & 0xFFFFFFFF)

def combine(x, y):
    """Abstract sum of two values. Kinds: ('c', v) constant; ('sp', off) stack address; ('rt', k) runtime value + small
    constant k (an object pointer when used as a base); ('cx', B) runtime index + address-like constant B; None unknown."""
    if x is None and y is None: return None
    if y is not None and y[0] == 'c' and x is not None and x[0] == 'c': return _c(x[1] + y[1])
    for a, b in ((x, y), (y, x)):
        if a is not None and a[0] == 'c':
            v = a[1]; big = _addr_like(v)
            if b is None: return ('cx', v) if big else ('rt', s32(v))
            if b[0] == 'sp': return ('sp', b[1] + s32(v)) if not big else None
            if b[0] == 'rt': return ('cx', (v + b[1]) & 0xFFFFFFFF) if big else ('rt', b[1] + s32(v))
            if b[0] == 'cx': return None if big else ('cx', (b[1] + v) & 0xFFFFFFFF)
            if b[0] == 'tab': return None
    return None

def mem_ops(i):
    """(kind, size, base_reg, disp, index_by_r0, value_reg) for loads ('ld') / stores ('st'); else None.
    Post-increment / pre-decrement are reported with disp 0 / -size (the base update is separate)."""
    mn, n, m = i.mn, i.n, i.m
    sz = {'b': 1, 'w': 2, 'l': 4}
    if mn.startswith('mov.') and '@' in mn and 'PC' not in mn:
        s = sz[mn[4]]
        if mn.endswith(',@(R0,Rn)'): return ('st', s, n, 0, True, m)
        if mn.endswith('@(R0,Rm),Rn'): return ('ld', s, m, 0, True, n)
        if mn == 'mov.l Rm,@(disp,Rn)': return ('st', 4, n, i.disp, False, m)
        if mn == 'mov.l @(disp,Rm),Rn': return ('ld', 4, m, i.disp, False, n)
        if mn.endswith('R0,@(disp,Rn)'): return ('st', s, n, i.disp, False, 0)
        if mn.endswith('@(disp,Rm),R0'): return ('ld', s, m, i.disp, False, 0)
        if mn.endswith('Rm,@-Rn'): return ('st', s, n, -s, False, m)
        if mn.endswith('@Rm+,Rn'): return ('ld', s, m, 0, False, n)
        if mn.endswith('Rm,@Rn'): return ('st', s, n, 0, False, m)
        if mn.endswith('@Rm,Rn'): return ('ld', s, m, 0, False, n)
    if mn == 'sts.l pr,@-Rn' or mn == 'sts.l macl,@-Rn': return ('st', 4, n, -4, False, None)
    if mn == 'lds.l @Rm+,pr' or mn == 'lds.l @Rm+,macl': return ('ld', 4, n, 0, False, None)
    return None

def step(code, i, st, tables):
    """Abstract transfer of one instruction (no control flow). Returns new State."""
    s = st.copy(); r = s.r; mn, n, m = i.mn, i.n, i.m
    def setr(k, v): r[k] = v
    mo = mem_ops(i)
    if mo:
        kind, size, b, disp, idx, vr = mo
        base = r[b]
        addr = None
        if base and base[0] == 'sp' and not idx: addr = base[1] + disp
        if kind == 'st':
            if addr is not None: s.slots[addr] = r[vr] if (vr is not None and size == 4) else None
            # stores through non-stack pointers don't affect tracked slots
        else:
            if vr is not None:
                if addr is not None and size == 4: setr(vr, s.slots.get(addr))
                elif idx and base and base[0] == 'c' and base[1] in tables and size == 4: setr(vr, ('tab', base[1]))
                else: setr(vr, None)
        # pre-decrement / post-increment update the base
        if mn.endswith('@-Rn'):
            r[b] = ('sp', base[1] - size) if base and base[0] == 'sp' else None
            if r[b] and r[b][0] == 'sp': s.slots[r[b][1]] = r[vr] if (vr is not None and size == 4) else None
        if mn.endswith('@Rm+,Rn') or mn.startswith('lds.l'):
            if vr is None or vr != b: r[b] = ('sp', base[1] + size) if base and base[0] == 'sp' else None
        return s
    if mn == 'mov #imm': setr(n, _c(i.imm))
    elif mn == 'mov.l @(disp,PC),Rn': setr(n, _c(code.u32(i.pc)))
    elif mn == 'mov.w @(disp,PC),Rn': setr(n, _c(s16(code.u16(i.pc))))
    elif mn == 'mova': setr(0, _c(i.pc))
    elif mn == 'mov': setr(n, r[m])
    elif mn == 'add #imm': setr(n, combine(r[n], _c(i.imm)))
    elif mn == 'add': setr(n, combine(r[n], r[m]))
    elif mn == 'sub':
        a, b = r[n], r[m]
        setr(n, _c(a[1] - b[1]) if a and b and a[0] == 'c' and b[0] == 'c' else None)
    elif mn in ('shll', 'shll2', 'shll8', 'shll16', 'shlr', 'shlr2', 'shlr8', 'shlr16', 'extu.b', 'extu.w', 'exts.b', 'exts.w', 'neg', 'not'):
        src = r[n] if mn.startswith('sh') else r[m]
        if src and src[0] == 'c':
            v = src[1]
            f = {'shll': lambda v: v << 1, 'shll2': lambda v: v << 2, 'shll8': lambda v: v << 8, 'shll16': lambda v: v << 16,
                 'shlr': lambda v: v >> 1, 'shlr2': lambda v: v >> 2, 'shlr8': lambda v: v >> 8, 'shlr16': lambda v: v >> 16,
                 'extu.b': lambda v: v & 0xFF, 'extu.w': lambda v: v & 0xFFFF, 'exts.b': lambda v: s8(v), 'exts.w': lambda v: s16(v),
                 'neg': lambda v: -v, 'not': lambda v: ~v}[mn]
            setr(n, _c(f(v)))
        else: setr(n, None)
    elif mn in ('and #imm', 'or #imm', 'xor #imm'):
        v = r[0]
        if v and v[0] == 'c': setr(0, _c({'and #imm': v[1] & i.imm, 'or #imm': v[1] | i.imm, 'xor #imm': v[1] ^ i.imm}[mn]))
        else: setr(0, None)
    elif mn in ('and', 'or', 'xor'):
        a, b = r[n], r[m]
        if a and b and a[0] == 'c' and b[0] == 'c':
            setr(n, _c({'and': a[1] & b[1], 'or': a[1] | b[1], 'xor': a[1] ^ b[1]}[mn]))
        else: setr(n, None)
    elif mn in ('movt', 'sts macl', 'sts mach', 'sts pr', 'rotcl', 'rotcr', 'rotl', 'rotr', 'shar', 'shal', 'dt', 'negc',
                'subc', 'addc', 'swap.b', 'swap.w', 'xtrct', 'div1', 'div0s'):
        setr(n, None)
    elif mn in ('jsr', 'bsr', 'bsrf'):
        for k in CALLER_SAVED: r[k] = None
    # compares, tst, nop, branches, mul (macl), clrt/sett, lds: no register change
    return s

def s32(v): v &= 0xFFFFFFFF; return v - (1 << 32) if v & 0x80000000 else v

def analyse(code, insns, func_entries, tables, stops=()):
    """Per-function dataflow. Returns {func_entry: {addr: State-before}} for every instruction reachable from that
    entry without following calls (jmp through a known table and plain branches are followed)."""
    res = {}
    for fe in func_entries:
        states = {fe: State([None] * 15 + [('sp', 0)], {})}
        dstates = {}
        work = [fe]
        while work:
            a = work.pop()
            st = states[a]; i = insns[a]
            mn = i.mn
            nxt = []
            if mn in DELAYED:
                d = insns[a + 2]
                # target register value read before the delay slot
                tv = st.r[i.n] if mn in ('jmp', 'jsr', 'braf', 'bsrf') else None
                s1 = step(code, d, st, tables)
                if a + 2 in dstates: dstates[a + 2].join(st)
                else: dstates[a + 2] = st.copy()
                if mn == 'rts': pass
                elif mn == 'bra':
                    if i.tgt not in stops: nxt.append((i.tgt, s1))      # bra to an external function = tail call
                elif mn in ('bt/s', 'bf/s'): nxt += [(i.tgt, s1), (a + 4, s1)]
                elif mn == 'bsr': nxt.append((a + 4, step(code, i, s1, tables)))
                elif mn == 'jsr': nxt.append((a + 4, step(code, i, s1, tables)))
                elif mn == 'jmp':
                    if tv and tv[0] == 'tab':
                        for e in tables[tv[1]]: nxt.append((e, s1))
                    elif tv and tv[0] == 'c' and tv[1] in insns: nxt.append((tv[1], s1))
                    # jmp to helper constant = tail call: function ends
                else: raise ValueError(f'{a:08x}: {mn} not handled')
            elif mn in ('bt', 'bf'):
                nxt += [(i.tgt, st), (a + 2, st)]
            else:
                nxt.append((a + 2, step(code, i, st, tables)))
            for t, s in nxt:
                if t not in insns: raise ValueError(f'{a:08x}: successor {t:08x} not discovered')
                _put(states, work, t, s)
        for k, v in dstates.items():
            if k in states: raise ValueError(f'{k:08x}: delay slot also reached directly')
            states[k] = v
        res[fe] = states
    return res

def _put(states, work, a, s):
    if a not in states: states[a] = s.copy(); work.append(a)
    elif states[a].join(s): work.append(a)

def classify_mem(i, st):
    """Classify a memory instruction given the abstract state before it.
    Returns dict(kind, size, how, sat=, field=, base_expr=) where how is:
      'stack' : native access through r15-derived pointer
      'const' : absolute Saturn address `sat`
      'cx'    : Saturn address `sat` (= B + disp) plus a runtime index; runtime base expression already holds T(B)+idx
      'obj'   : object field `field` (= k + disp) of the runtime pointer; base expression holds obj + k
    """
    mo = mem_ops(i)
    kind, size, b, disp, idx, vr = mo
    base = st.r[b]
    expr = f'r{b}'
    if idx:
        base = combine(base, st.r[0]); expr = f'(r{b} + r0)'
        if base is None and st.r[b] is None and st.r[0] is None:
            return dict(kind=kind, size=size, how='unknown', expr=expr, vr=vr, mo=mo)
    pre = i.mn.endswith('@-Rn') or i.mn.startswith('sts.l')
    eff_disp = -size if pre else disp
    d = dict(kind=kind, size=size, expr=expr, vr=vr, mo=mo, disp=eff_disp)
    if base is None: base = ('rt', 0)
    k = base[0]
    if k == 'sp': d.update(how='stack')
    elif k == 'c': d.update(how='const', sat=(base[1] + eff_disp) & 0xFFFFFFFF)
    elif k == 'cx': d.update(how='cx', sat=(base[1] + eff_disp) & 0xFFFFFFFF, B=base[1])
    elif k == 'rt': d.update(how='obj', field=base[1] + eff_disp, k=base[1])
    else: d.update(how='unknown')
    return d

# ---------------------------------------------------------------- lifter
INLINE_HELPERS = {
    0x06019800: 'r0 = (r0 == 0) ? 0 : (u32)((s32)r1 % (s32)r0);',                                  # SH runtime: signed r1 % r0
    0x060196A4: 'r0 = (r0 == 0) ? 0 : (u32)((s32)r1 / (s32)r0);',                                  # SH runtime: signed r1 / r0
    0x06019A98: 'r0 = ((s32)r1 < 0) ? r0 : (((s32)r1 >= 32) ? 0 : (r0 << r1));',                   # SH runtime: r0 << r1
}

def _ctype(size, signed=True):
    return {1: 's8' if signed else 'u8', 2: 's16' if signed else 'u16', 4: 'u32'}[size]

def lift(code, name, entry, tables, port, extra_entries=(), stops=(), stack_words=48, nargs=1):
    """Lift the handler at `entry` into a C function `void name(u32 obj)`. tables: {addr: n}. stops = Saturn functions
    reached by bsr / bra that are not lifted into this function but called as port.helper(addr) (another lifted
    function or a hand-written replacement; M7b). Returns C source."""
    stops = set(stops)
    ins, ents, calls = discover(code, [entry] + list(extra_entries), tables, None, stops)
    tabs = {t: [code.u32(t + 4 * k) for k in range(n)] for t, n in tables.items()}
    fes = [entry] + sorted(calls) + list(extra_entries)
    res = analyse(code, ins, fes, tabs, stops)
    more = set()
    for fe, st in res.items():
        for a, s in st.items():
            i = ins[a]
            if i.mn == 'jsr':
                v = s.r[i.n]
                if v and v[0] == 'tab': more |= set(tabs[v[1]])
                elif v and v[0] == 'c' and v[1] in ins: more.add(v[1])
    fes = fes + sorted(more - set(fes))
    res = analyse(code, ins, fes, tabs, stops)
    # merge contexts: per address list of states
    ctx = {}
    for fe, st in res.items():
        for a, s in st.items(): ctx.setdefault(a, []).append(s)
    targets = set(fes)
    for a, i in ins.items():
        if i.tgt is not None and i.tgt not in stops: targets.add(i.tgt)
        if i.mn in ('bt/s', 'bf/s', 'jsr', 'bsr'): targets.add(a + 4)
        if i.mn == 'bt' or i.mn == 'bf': targets.add(a + 2)
    for t in tabs.values(): targets |= set(t)
    retids = {}
    out = []
    E = out.append
    def one(a, what):
        vals = set()
        for s in ctx[a]: vals.add(what(s))
        if len(vals) != 1: raise ValueError(f'{a:08x}: context-dependent translation {vals}')
        return vals.pop()
    def cval(v, a):
        return port.value(v, a)
    def mem(i, a):
        d0 = None
        for s in ctx[a]:
            d = classify_mem(i, s)
            key = (d['how'], d.get('sat'), d.get('field'), d.get('k'))
            if d0 is None: d0 = (key, d)
            elif d0[0] != key: raise ValueError(f'{a:08x}: memory classification differs by context {d0[0]} {key}')
        d = d0[1]
        how, size, kind = d['how'], d['size'], d['kind']
        if how == 'stack': addr = f'({d["expr"]} + ({d["disp"]}))' if not i.mn.endswith('@-Rn') else None
        elif how == 'const':
            addr = port.const_addr(d['sat'], size, kind, a)
        elif how == 'cx':
            addr = port.cx_addr(d['B'], d['sat'], size, kind, a, d['expr'], d['disp'])
        elif how == 'obj':
            f = port.field(d['field'], size, a)
            addr = f'({d["expr"]} + ({f - d["k"]}))'
        else: raise ValueError(f'{a:08x}: cannot classify memory access {i}')
        return d, addr
    def emit_ins(i, a, ind='  '):
        mn, n, m = i.mn, i.n, i.m
        R = lambda k: f'r{k}'
        if mem_ops(i):
            kind, size, b, disp, idx, vr = mem_ops(i)
            if mn.startswith('sts.l'):
                E(f'{ind}r{b} -= 4; *(u32 *)r{b} = 0;'); return
            if mn.startswith('lds.l'):
                if mn == 'lds.l @Rm+,macl': E(f'{ind}macl = *(u32 *)r{b}; r{b} += 4;')
                else: E(f'{ind}r{b} += 4;')
                return
            d, addr = mem(i, a)
            ty = _ctype(size)
            if mn.endswith('@-Rn'):
                assert d['how'] == 'stack', f'{a:08x}: pre-decrement through non-stack pointer'
                E(f'{ind}r{b} -= {size}; *({ty} *)r{b} = ({ty})r{vr};'); return
            if kind == 'ld':
                E(f'{ind}r{vr} = (u32)(s32)*(volatile {ty} *){addr};')
                if mn.endswith('@Rm+,Rn') and vr != b: E(f'{ind}r{b} += {size};')
            else:
                if addr is None: E(f'{ind}/* {a:08x}: store dropped (no PS1 counterpart) */'); return
                E(f'{ind}*(volatile {ty} *){addr} = ({ty})r{vr};')
            return
        if mn == 'mov #imm': E(f'{ind}r{n} = {i.imm & 0xFFFFFFFF:#x};')
        elif mn in ('mov.l @(disp,PC),Rn', 'mov.w @(disp,PC),Rn', 'mova'):
            v = code.u32(i.pc) if mn.startswith('mov.l') else (s16(code.u16(i.pc)) & 0xFFFFFFFF if mn.startswith('mov.w') else i.pc)
            E(f'{ind}r{0 if mn == "mova" else n} = {cval(v, a)};')
        elif mn == 'mov': E(f'{ind}r{n} = r{m};')
        elif mn == 'add #imm':
            res_c = one(a, lambda s: step(code, i, s, tabs).r[n])
            if res_c and res_c[0] == 'c' and _addr_like(res_c[1]): E(f'{ind}r{n} = {cval(res_c[1], a)};')
            else: E(f'{ind}r{n} += {i.imm & 0xFFFFFFFF:#x};')
        elif mn in ('add', 'sub'):
            res_c = one(a, lambda s: step(code, i, s, tabs).r[n])
            if res_c and res_c[0] == 'c' and _addr_like(res_c[1]): E(f'{ind}r{n} = {cval(res_c[1], a)};')
            else: E(f'{ind}r{n} {"+" if mn == "add" else "-"}= r{m};')
        elif mn == 'neg': E(f'{ind}r{n} = -r{m};')
        elif mn == 'not': E(f'{ind}r{n} = ~r{m};')
        elif mn in ('and', 'or', 'xor'): E(f'{ind}r{n} {dict(and_="&", or_="|", xor_="^")[mn + "_"]}= r{m};')
        elif mn in ('and #imm', 'or #imm', 'xor #imm'): E(f'{ind}r0 {dict(a="&", o="|", x="^")[mn[0]]}= {i.imm:#x};')
        elif mn == 'tst': E(f'{ind}T = (r{n} & r{m}) == 0;')
        elif mn == 'tst #imm': E(f'{ind}T = (r0 & {i.imm:#x}) == 0;')
        elif mn == 'cmp/eq': E(f'{ind}T = r{n} == r{m};')
        elif mn == 'cmp/eq #imm': E(f'{ind}T = r0 == {i.imm & 0xFFFFFFFF:#x};')
        elif mn == 'cmp/hs': E(f'{ind}T = r{n} >= r{m};')
        elif mn == 'cmp/hi': E(f'{ind}T = r{n} > r{m};')
        elif mn == 'cmp/ge': E(f'{ind}T = (s32)r{n} >= (s32)r{m};')
        elif mn == 'cmp/gt': E(f'{ind}T = (s32)r{n} > (s32)r{m};')
        elif mn == 'cmp/pz': E(f'{ind}T = (s32)r{n} >= 0;')
        elif mn == 'cmp/pl': E(f'{ind}T = (s32)r{n} > 0;')
        elif mn == 'extu.b': E(f'{ind}r{n} = r{m} & 0xFF;')
        elif mn == 'extu.w': E(f'{ind}r{n} = r{m} & 0xFFFF;')
        elif mn == 'exts.b': E(f'{ind}r{n} = (u32)(s32)(s8)r{m};')
        elif mn == 'exts.w': E(f'{ind}r{n} = (u32)(s32)(s16)r{m};')
        elif mn in ('shll', 'shal'): E(f'{ind}T = r{n} >> 31; r{n} <<= 1;')
        elif mn == 'shlr': E(f'{ind}T = r{n} & 1; r{n} >>= 1;')
        elif mn == 'shar': E(f'{ind}T = r{n} & 1; r{n} = (u32)((s32)r{n} >> 1);')
        elif mn in ('shll2', 'shll8', 'shll16'): E(f'{ind}r{n} <<= {mn[4:]};')
        elif mn in ('shlr2', 'shlr8', 'shlr16'): E(f'{ind}r{n} >>= {mn[4:]};')
        elif mn == 'rotl': E(f'{ind}T = r{n} >> 31; r{n} = (r{n} << 1) | T;')
        elif mn == 'rotr': E(f'{ind}T = r{n} & 1; r{n} = (r{n} >> 1) | ((u32)T << 31);')
        elif mn == 'rotcl': E(f'{ind}{{ int t2 = r{n} >> 31; r{n} = (r{n} << 1) | T; T = t2; }}')
        elif mn == 'rotcr': E(f'{ind}{{ int t2 = r{n} & 1; r{n} = (r{n} >> 1) | ((u32)T << 31); T = t2; }}')
        elif mn == 'dt': E(f'{ind}r{n} -= 1; T = r{n} == 0;')
        elif mn == 'movt': E(f'{ind}r{n} = T;')
        elif mn == 'clrt': E(f'{ind}T = 0;')
        elif mn == 'sett': E(f'{ind}T = 1;')
        elif mn == 'mul.l': E(f'{ind}macl = r{n} * r{m};')
        elif mn == 'mulu.w': E(f'{ind}macl = (u32)(u16)r{n} * (u32)(u16)r{m};')
        elif mn == 'muls.w': E(f'{ind}macl = (u32)((s32)(s16)r{n} * (s32)(s16)r{m});')
        elif mn == 'sts macl': E(f'{ind}r{n} = macl;')
        elif mn == 'lds macl': E(f'{ind}macl = r{n};')
        elif mn == 'swap.b': E(f'{ind}r{n} = (r{m} & 0xFFFF0000) | ((r{m} & 0xFF) << 8) | ((r{m} >> 8) & 0xFF);')
        elif mn == 'swap.w': E(f'{ind}r{n} = (r{m} << 16) | (r{m} >> 16);')
        elif mn == 'xtrct': E(f'{ind}r{n} = (r{n} >> 16) | (r{m} << 16);')
        elif mn == 'negc': E(f'{ind}{{ u32 t1 = -r{m}; u32 t2 = t1 - T; T = (r{m} != 0) || (t1 < t2); r{n} = t2; }}')
        elif mn == 'subc': E(f'{ind}{{ u32 t1 = r{n} - r{m}; u32 t2 = t1 - T; T = (r{n} < t1) || (t1 < t2); r{n} = t2; }}')
        elif mn == 'addc': E(f'{ind}{{ u32 t1 = r{n} + r{m}; u32 t2 = t1 + T; T = (t1 < r{n}) || (t2 < t1); r{n} = t2; }}')
        elif mn == 'nop': pass
        else: raise ValueError(f'{a:08x}: no C for {mn}')
    def call_target(i, a):
        v = one(a, lambda s: s.r[i.n])
        return v
    def emit_call(v, a, tail):
        if v and v[0] == 'c' and v[1] in INLINE_HELPERS:
            E('  ' + INLINE_HELPERS[v[1]])
            E('  goto RET;' if tail else '')
            return
        if v and v[0] == 'c' and v[1] not in ins:
            h = port.helper(v[1], a)
            E(f'  r0 = {h}(r4, r5, r6, r7);')
            if tail: E('  goto RET;')
            return
        if v and v[0] == 'c':
            tgt_list = [v[1]]
        elif v and v[0] == 'tab':
            tgt_list = None
        else: raise ValueError(f'{a:08x}: unknown call/jump target {v}')
        if not tail:
            rid = retids.setdefault(a + 4, len(retids) + 1)
            E(f'  RS[rsp++] = {rid};')
        if tgt_list: E(f'  goto L_{tgt_list[0]:06x};')
        else:
            E(f'  switch (r{i.n}) {{')
            for e in sorted(set(tabs[v[1]])): E(f'    case {e:#x}: goto L_{e:06x};')
            E('    default: goto BAD;\n  }')
    addrs = sorted(ins)
    skip = set()
    body_start = len(out)
    for a in addrs:
        if a in skip: continue
        i = ins[a]
        if a in targets: E(f'L_{a:06x}:')
        mn = i.mn
        if mn in DELAYED:
            d = ins[a + 2]; skip.add(a + 2)
            if mn in ('bt/s', 'bf/s'):
                E(f'  {{ int c = {"T" if mn == "bt/s" else "!T"};')
                emit_ins(d, a + 2, '    ')
                E(f'    if (c) goto L_{i.tgt:06x}; }}')
            elif mn == 'bra' and i.tgt in stops:
                emit_ins(d, a + 2); E(f'  r0 = {port.helper(i.tgt, a)}(r4, r5, r6, r7);'); E('  goto RET;')
            elif mn == 'bsr' and i.tgt in stops:
                emit_ins(d, a + 2); E(f'  r0 = {port.helper(i.tgt, a)}(r4, r5, r6, r7);')
            elif mn == 'bra':
                emit_ins(d, a + 2); E(f'  goto L_{i.tgt:06x};')
            elif mn == 'bsr':
                emit_ins(d, a + 2)
                rid = retids.setdefault(a + 4, len(retids) + 1)
                E(f'  RS[rsp++] = {rid}; goto L_{i.tgt:06x};')
            elif mn in ('jsr', 'jmp'):
                v = call_target(i, a)
                if v and v[0] == 'tab' or (v and v[0] == 'c' and v[1] in ins):
                    E(f'  tgt = r{i.n};') if v[0] == 'tab' else None
                    emit_ins(d, a + 2)
                    if v[0] == 'tab':
                        if mn == 'jsr':
                            rid = retids.setdefault(a + 4, len(retids) + 1); E(f'  RS[rsp++] = {rid};')
                        E('  switch (tgt) {')
                        for e in sorted(set(tabs[v[1]])): E(f'    case {e:#x}: goto L_{e:06x};')
                        E('    default: goto BAD;\n  }')
                    else:
                        if mn == 'jsr':
                            rid = retids.setdefault(a + 4, len(retids) + 1); E(f'  RS[rsp++] = {rid};')
                        E(f'  goto L_{v[1]:06x};')
                else:
                    emit_ins(d, a + 2)
                    emit_call(v, a, mn == 'jmp')
            elif mn == 'rts':
                emit_ins(d, a + 2); E('  goto RET;')
            else: raise ValueError(f'{a:08x}: {mn}')
            continue
        if mn in ('bt', 'bf'):
            E(f'  if ({"T" if mn == "bt" else "!T"}) goto L_{i.tgt:06x};'); continue
        emit_ins(i, a)
    body = out[body_start:]
    del out[body_start:]
    hdr = [f'void {name}(u32 obj{", u32 a1" if nargs > 1 else ""})', '{',      # nargs 2 (M7e player): r5 = second argument
           f'  u32 r0 = 0, r1 = 0, r2 = 0, r3 = 0, r4 = obj, r5 = {"a1" if nargs > 1 else "0"}, r6 = 0, r7 = 0, r8 = 0, r9 = 0, r10 = 0, r11 = 0, r12 = 0, r13 = 0, r14 = 0, r15;',
           f'  u32 macl = 0, tgt = 0; int T = 0; u32 stk[{stack_words}]; unsigned char RS[16]; int rsp = 0;',
           f'  r15 = (u32)&stk[{stack_words}];', f'  goto L_{entry:06x};']
    tail = ['RET:', '  if (rsp == 0) return;', '  switch (RS[--rsp]) {']
    for ra, rid in sorted(retids.items(), key=lambda x: x[1]): tail.append(f'    case {rid}: goto L_{ra:06x};')
    tail += ['  }', 'BAD:', '  return;', '}']
    # labels referenced but at addresses that were skipped (delay slots) are an error
    return '\n'.join(hdr + body + tail) + '\n', dict(insns=len(ins), funcs=[hex(x) for x in fes], returns=len(retids))

# ---- Wood Man port: address translation, data region

OBJ_WORD_FIELDS = (0x08, 0x0C, 0x10, 0x14, 0x18, 0x1C, 0x20, 0x28, 0x38, 0x3C, 0x4C)
def wm_field(off, size, a=None, words=OBJ_WORD_FIELDS):
    for w in words:
        if w <= off < w + 4 and size < 4:
            return w + (4 - (off - w) - size)
    return off

WM_DATA_LO, WM_DATA_HI = 0x2933B8, 0x2934E0
WM_STRUCTS = {'camera': (0x0602FEF0, 0xA8, 0x801D2914), 'player': (0x0602FF98, 0xA8, 0x8015E23C)}
WM_GLOBALS = {0x0602DA73: 0x80170574, 0x0602DA97: 0x801C337F, 0x0602DA98: 0x801B297C, 0x0602DAA7: 0x801B298B,
           0x0602DAA8: 0x801B298C, 0x0602DAAE: 0x801B2992, 0x0602DAAF: 0x801B2993, 0x0602DAB0: 0x801B2994,
           0x0602DAB1: 0x801B2995, 0x0602DABA: 0x801B299E, 0x0602DAC1: 0x801B29A5, 0x0602DAE9: 0x8016D2FD,
           0x0602E1B3: 0x8015A00F, 0x0603622E: 0x80158F5C, 0x0605F088: 0x801C73E8, 0x06073356: 0x801CF53C,
           0x06036230: 0x8016DC08}   # current weapon (without it the weapon bar stays on)
WM_DROP = set()                       # Saturn globals with no PS1 counterpart (stores dropped); none left
WM_BITMASK = (0x00296C84, 8, 0x80139548)
# camera halfword fields, same offsets on both systems (globals_map.txt votes: +6 85/85, +8 65/67, +0x12 6/6, +0x14 123/125,
# +0x16 90/92, +0x18 14/14, +0x1A..+0x20 1-2 each, +0x22 62/62, +0x24 5/5, +0x26 23/23, +0x28 7/7; +6/+0x14/+0x16 also by gate code)
WM_CAMERA_HALF = {o: 0x801D2914 + o for o in (0x06, 0x08, 0x12, 0x14, 0x16, 0x18, 0x1A, 0x1C, 0x1E, 0x20, 0x22, 0x24, 0x26, 0x28)}

# Saturn -> PS1 address blocks for lifted code (value / const_addr): (sat_lo, sat_hi, ps1_lo, label)
PORT_BLOCKS = [(0x0602DA98, 0x0602DAC4, 0x801B297C, 'DA98 block'), (0x0602E108, 0x0602E1B4, 0x80159F64, 'chunk 8')]

class BossPort:
    """Saturn -> PS1 translation for the SH-2 lifter. spec keys: data (lo, hi) = Saturn data region copied into the
    overlay as sat_data; globals {sat: ps1} (byte globals, plus 0x0602DA98 as a word); blocks = extra PORT_BLOCKS
    entries; custom {sat helper: C source} = helpers with no PS1 function, implemented in the generated C file.
    word_fields = object fields that are 32-bit words (sub-word accesses are byte-swapped), set per handler."""
    def __init__(self, spec):
        self.spec = spec; self.lo, self.hi = spec['data']; self.globals = spec['globals']
        self.blocks = spec.get('blocks', []) + PORT_BLOCKS; self.custom = spec.get('custom', {})
        self.hm = load_helper_map()
        self.used_helpers = set(); self.used_custom = set(); self.word_fields = OBJ_WORD_FIELDS
    def helper(self, sat, a):
        if sat in self.custom: self.used_custom.add(sat); return f'h_{sat:08x}'
        if sat not in self.hm: raise ValueError(f'{a:08x}: helper {sat:08x} not mapped')
        self.used_helpers.add(sat); return f'h_{sat:08x}'
    def value(self, v, a):
        if not _addr_like(v): return f'{v:#x}'
        if self.lo <= v < self.hi: return f'((u32)sat_data + {v - self.lo:#x})'
        if v in self.hm or v in INLINE_HELPERS or v in self.custom or 0x00200000 <= v < 0x00280000: return f'{v:#x} /* code */'
        if v in WM_DROP: return f'{v:#x} /* dropped global, only used as a store base */'
        if WM_BITMASK[0] <= v < WM_BITMASK[0] + WM_BITMASK[1]: return f'{WM_BITMASK[2] + v - WM_BITMASK[0]:#x}'
        for nm, (b, n, p) in WM_STRUCTS.items():
            if b <= v < b + n: return f'{p + (v - b):#x} /* {nm} */'
        if v in self.globals: return f'{self.globals[v]:#x}'
        for lo, hi, p, nm in self.blocks:
            if lo <= v < hi: return f'{p + v - lo:#x} /* {nm} */'
        return f'{v:#x} /* UNTRANSLATED */'
    def const_addr(self, sat, size, kind, a):
        if sat in WM_DROP:
            if kind == 'st': return None
            raise ValueError(f'{a:08x}: load from dropped {sat:08x}')
        if self.lo <= sat < self.hi: return f'((u32)sat_data + {sat - self.lo:#x})'
        for nm, (b, n, p) in WM_STRUCTS.items():
            if b <= sat < b + n:
                off = sat - b
                if nm == 'camera':           # camera struct: halfword fields at the same offsets (02 §Fixed objects)
                    assert size == 2 and off in WM_CAMERA_HALF, f'{a:08x}: camera +{off:#x} size {size}'
                    return f'{p + off:#x}'
                return f'{p + wm_field(off, size):#x}'
        if size == 1 and sat in self.globals: return f'{self.globals[sat]:#x}'
        if size == 4 and sat == 0x0602DA98: return f'{self.globals[sat]:#x}'
        if WM_BITMASK[0] <= sat < WM_BITMASK[0] + WM_BITMASK[1] and size == 1: return f'{WM_BITMASK[2] + sat - WM_BITMASK[0]:#x}'
        if size == 1:
            for lo, hi, p, nm in self.blocks:
                if lo <= sat < hi: return f'{p + sat - lo:#x}'
        raise ValueError(f'{a:08x}: no PS1 address for {sat:08x} size {size}')
    def cx_addr(self, B, sat, size, kind, a, expr, disp):
        if self.lo <= B < self.hi and self.lo <= sat < self.hi: return f'({expr} + ({disp}))'
        if size == 1 and any(lo <= B < hi and lo <= sat < hi for lo, hi, p, nm in self.blocks):
            return f'({expr} + ({disp}))'          # byte table in a block (e.g. chunk 8 [+0x27]); expr holds the PS1 base
        raise ValueError(f'{a:08x}: indexed access outside data region {B:08x}/{sat:08x}')
    def field(self, off, size, a):
        if a in self.spec.get('raw_at', ()): return off    # not an object (e.g. a data record), no word-field rule
        return wm_field(off, size, words=self.word_fields)

WM_HALF_TABLES = [(0x29347A, 4)]                    # (addr, count) halfword tables read by the leaf
WM_CODE_TABLES = {0x2933DC: 4, 0x2933EC: 5, 0x293400: 8, 0x293420: 4, 0x293430: 4, 0x293440: 3,
               0x2934A0: 5, 0x2934B4: 3, 0x2934C0: 3, 0x2934CC: 5}
def _wood_state(rnd):
    ph = rnd.randrange(4); st = {0: 0, 1: rnd.randrange(5), 2: rnd.randrange(8), 3: rnd.randrange(3)}[ph]
    return {1: ph, 2: st, 3: rnd.randrange(4), 6: 0x63, 7: 0, 0x54: rnd.randrange(5)}
def _leaf_state(rnd):
    ph = rnd.randrange(5); st = {0: 0, 1: rnd.randrange(3), 2: rnd.randrange(3), 3: rnd.randrange(3), 4: rnd.randrange(5)}[ph]
    return {1: ph, 2: st, 3: rnd.randrange(4), 6: 0x64, 7: rnd.randrange(4), 0x53: rnd.randrange(4), 0x32: rnd.randrange(15)}
def _wood_cam(rnd):
    cx = rnd.choice([0x1200, 0x1210, 0x1100, rnd.randrange(0x1100, 0x1300)])
    return [(6, cx), (0x14, rnd.choice([0x1200, cx])), (0x16, 0x400)]
WOOD_TRIALS = {'wood': dict(fn='wood_man', entry=0x258F00, pool='P2', layout='wood', state=_wood_state, children=[0x63, 0x64, 0x39],
                            player_x=(0x1180, 0x1400), cam=_wood_cam),
               'leaf': dict(fn='wood_leaf', entry=0x259B7C, pool='P2', layout='leaf', state=_leaf_state, children=[0x63, 0x64, 0x39],
                            player_x=(0x1180, 0x1400), cam=_wood_cam)}
WOOD_SPEC = dict(name='wood', data=(WM_DATA_LO, WM_DATA_HI), globals=WM_GLOBALS, code_tables=WM_CODE_TABLES, trials=WOOD_TRIALS,
                 half_tables=WM_HALF_TABLES, cfile='m2_wood.c',
                 handlers=[('wood_man', 0x258F00, 0x63, [k for k in WM_CODE_TABLES if k < 0x29344C], OBJ_WORD_FIELDS),
                           ('wood_leaf', 0x259B7C, 0x64, [k for k in WM_CODE_TABLES if k >= 0x29344C], OBJ_WORD_FIELDS)])

# ---- M4: Cut Man (P2 type 0x62, variants 0 boss / 1 / 2) and the Saturn-only P5 type 0x34 of STAGE09
CUT_DATA = (0x29331C, 0x2933B8)                 # Saturn data used by Cut Man (byte data + 7 code-pointer tables)
CUT_CODE_TABLES = {0x293330: 3, 0x29333C: 4, 0x29334C: 6, 0x293364: 7, 0x293380: 5, 0x293394: 5, 0x2933A8: 4}
CUT_GLOBALS = {**WM_GLOBALS, 0x0602DAEB: 0x8016D2FF, 0x0602E1B2: 0x8015A00E}
CUT_BLOCKS = [(0x0602DA84, 0x0602DA98, 0x801C336C, 'DA84 block'),            # 02 §Global map (stage, phase, ...)
              (0x0602DAF0, 0x0602DAFC, 0x8015D224, 'DAF0 block (fade)'),     # same struct: fade task 0x0600E298 / 0x80109CB8
              (0x06035EE0, 0x060361E1, 0x801CF540, 'P5 pool (+ end pointer)')]  # 32 x 0x18 (02 §Pools)
CUT_BRIGHT_C = r"""
/* Saturn 0x06010B50(level, mask): colour offset on the channels in mask (1 = red, 2 = green, 4 = blue). The PS1 kept
   only the all-channel version (0x06010A24 = 0x8010BB88 -> 0x8010BBE4: brightness bytes 0x801B299B/C/D = R/G/B of the
   textured-primitive colour, 0x80 = neutral, level a -> 0x80 + 4(a - 1) for a > 0, 0x80 + 4a for a < 0, dirty flag
   0x8016CA90) and emptied this one (0x8010BC40 = jr ra). Same level law, on the masked channels; the others neutral. */
__attribute__((noinline, noipa)) u32 h_06010b50(u32 level, u32 mask, u32 c, u32 d)
{
  int a = (s8)level; u8 v = a == 0 ? 0x80 : a > 0 ? 0x80 + 4 * (a - 1) : 0x80 + 4 * a;
  *(volatile u8 *)0x801B299B = (mask & 1) ? v : 0x80;
  *(volatile u8 *)0x801B299C = (mask & 2) ? v : 0x80;
  *(volatile u8 *)0x801B299D = (mask & 4) ? v : 0x80;
  *(volatile u8 *)0x8016CA90 = 1;
  return 0;
}
"""
def _cut_state(rnd):
    v = rnd.choice([0, 0, 0, 1, 1, 2])
    if v == 0:
        ph = rnd.randrange(4); st = rnd.randrange((4, 6, 5, 5)[ph]); sub = rnd.randrange(7 if (ph, st) == (1, 3) else 4)
    elif v == 1: ph, st, sub = rnd.randrange(4), rnd.randrange(4), rnd.randrange(4)
    else: ph, st, sub = rnd.randrange(256), rnd.randrange(4), rnd.randrange(4)
    return {1: ph, 2: st, 3: sub, 6: 0x62, 7: v}
def _cut_cam(rnd):
    cx = rnd.choice([0xD00, 0x700, 0x600, rnd.randrange(0x600, 0xE00)])
    return [(6, cx), (8, rnd.choice([0x400, 0x500])), (0x14, rnd.choice([0xD00, cx])), (0x16, 0x400)]
def _alarm_state(rnd):
    return {1: rnd.randrange(2), 6: 0x34, 7: 0, 0x14: rnd.randrange(4), 0x15: rnd.randrange(10), 0x16: rnd.randrange(2)}
def _alarm_cam(rnd):
    return [(6, rnd.choice([0x8B0, 0xD00, rnd.randrange(0x600, 0xE00)])), (8, rnd.choice([0xA00, 0x400, rnd.randrange(0x400, 0xB00)]))]
CUT_WORD_FIELDS = OBJ_WORD_FIELDS + (0x54,)     # Cut Man <-> his variant-1 child (Rolling Cutter?) pointer at +0x54 (0x258E90 / 0x258E92)
CUT_TRIALS = {'cut': dict(fn='cut_man', entry=0x257E60, pool='P2', layout='cut', state=_cut_state, children=[0x62, 0x62, 0x39],
                          player_x=(0x600, 0xE00), cam=_cut_cam, ptr_fields=(0x4C, 0x54), child_ptr_fields=(0x54,)),
              'alarm': dict(fn='p5_alarm', entry=0x2083E0, pool='P5', layout='p5', state=_alarm_state, children=[0x62, 0x39],
                            player_x=(0x600, 0xE00), cam=_alarm_cam)}
CUT_SPEC = dict(name='cut', data=CUT_DATA, globals=CUT_GLOBALS, code_tables=CUT_CODE_TABLES, half_tables=[], cfile='m4_cut.c',
                blocks=CUT_BLOCKS, custom={0x06010B50: CUT_BRIGHT_C}, custom_arity={0x06010B50: 2}, trials=CUT_TRIALS,
                glob_bytes=[(0x0602DA84, 0x14, 0x801C336C), (0x0602DAF0, 6, 0x8015D224)],
                handlers=[('cut_man', 0x257E60, 0x62, list(CUT_CODE_TABLES), CUT_WORD_FIELDS),
                          ('p5_alarm', 0x2083E0, 0x34, [], (0x08, 0x0C, 0x10))])

def wm_data_bytes(sat, spec=None):
    spec = spec or WOOD_SPEC; lo, hi = spec['data']
    d = bytearray(sat.p2[lo - sat.BASE2:hi - sat.BASE2])
    for t, n in spec['code_tables'].items():
        for k in range(n):
            o = t - lo + 4 * k; d[o:o + 4] = d[o:o + 4][::-1]
    for t, n in spec.get('half_tables', []):
        for k in range(n):
            o = t - lo + 2 * k; d[o:o + 2] = d[o:o + 2][::-1]
    return bytes(d)

def wm_c_file(sat, port, bodies):
    hm = port.hm; lo, hi = port.spec['data']
    out = ['/* Generated by mm8tool (SH-2 lifter) from Saturn 2NDREAD.PRG - do not edit. */',
           'typedef signed char s8; typedef unsigned char u8; typedef short s16; typedef unsigned short u16;',
           'typedef int s32; typedef unsigned int u32;']
    for h in sorted(port.used_helpers): out.append(f'extern u32 h_{h:08x}(u32, u32, u32, u32);   /* PS1 {hm[h]:#010x} */')
    for h in sorted(port.used_custom): out.append(port.custom[h].strip())
    d = wm_data_bytes(sat, port.spec)
    out.append(f'const u8 sat_data[{len(d)}] __attribute__((aligned(4))) = {{  /* Saturn {lo:#x}-{hi - 1:#x}, tables converted */')
    for k in range(0, len(d), 16): out.append('  ' + ', '.join(f'{b:#04x}' for b in d[k:k + 16]) + ',')
    out.append('};')
    return '\n'.join(out) + '\n\n' + '\n'.join(bodies)
M2_MAGIC = b'MM8R-M2\0'

def boss_lift(spec):
    """Lift every handler of a port spec to C. Returns (C source, BossPort with used_helpers)."""
    sat = Sat(); code = Code([(sat.BASE2, sat.p2), (sat.BASE1, sat.p1)])
    port = BossPort(spec); bodies = []
    for name, entry, typ, tabkeys, words in spec['handlers']:
        port.word_fields = words
        src, _ = lift(code, name, entry, {k: spec['code_tables'][k] for k in tabkeys}, port); bodies.append(src)
    port.word_fields = OBJ_WORD_FIELDS
    return wm_c_file(sat, port, bodies), port

def boss_compile(spec, base, workdir=None):
    import tempfile
    wd = workdir or tempfile.mkdtemp(prefix='mm8' + spec['name'] + '_')
    csrc, port = boss_lift(spec)
    path = os.path.join(wd, spec['cfile']); open(path, 'w').write(csrc)
    data, sy = compile_c([path], base, helper_symbols(), workdir=wd)
    probs = check_r3000(data, base, sy['sat_data'])
    assert not probs, probs[:5]
    return data, sy, port, path

def wm_compile(base, workdir=None): return boss_compile(WOOD_SPEC, base, workdir)

def relocate_enemy_table(ovl, stage, extra):
    """Append a 153-entry copy of the overlay's P2/P3 table (16-byte aligned): P2 0x00-0x61, then `extra` = handlers
    for types 0x62-0x64 (None = the overlay's stub), then the 52 P3 entries; repoint the two lui/addiu pairs.
    Returns (bytearray, new table address, stub)."""
    base, (tab, lui2, lui3) = Ps1.OVL_BASE, PS1_TABLE[stage]
    img = Img(ovl, base)
    old = [img.u32(tab + 4 * k) for k in range(150)]
    stub = collections.Counter(old[:PS1_P2_LEN]).most_common(1)[0][0]
    end = base + len(ovl); new_tab = (end + 0xF) & ~0xF
    entries = old[:PS1_P2_LEN] + [stub if e is None else e for e in extra] + old[PS1_P2_LEN:]
    out = bytearray(ovl) + bytes(new_tab - end) + struct.pack('<153I', *entries)
    for lui_at, target in ((lui2, new_tab), (lui3, new_tab + 101 * 4)):
        o = lui_at - base
        hi_w, lo_w = struct.unpack('<2I', out[o:o + 8])
        out[o:o + 8] = struct.pack('<2I', *patch_lui_addiu(lo_w, hi_w, target))
    return out, new_tab, stub

def build_m2_overlay(ovl, workdir=None, n_vfiles=0):
    """STAGE08.BIN + 153-entry P2/P3 table (0x63 = Wood Man, 0x64 = leaf, 0x62 = stub) + marker + lifted code + data,
    then n_vfiles zeroed 'virtual' file-table entries for music 0x46, 0x47, ... at an address E with
    (E - PS1_FILETAB) % 12 == 0 (see patch_music_hook): info['vf0'] = (E - PS1_FILETAB) / 12 - 0x46, info['vf_off'] =
    their offset in the returned bytearray (the caller fills them in once the LBAs are known)."""
    base = Ps1.OVL_BASE
    new_tab = (base + len(ovl) + 0xF) & ~0xF
    magic = new_tab + 153 * 4
    code_base = (magic + len(M2_MAGIC) + 0xF) & ~0xF
    data, sy, port, path = wm_compile(code_base, workdir)
    out, tab, stub = relocate_enemy_table(ovl, 'STAGE08', [None, sy['wood_man'], sy['wood_leaf']])
    assert tab == new_tab
    out += M2_MAGIC + bytes(code_base - (magic + len(M2_MAGIC))) + data
    while len(out) % 4: out.append(0)
    vf0 = vf_off = None
    if n_vfiles:
        while (base + len(out) - PS1_FILETAB) % 12: out += bytes(4)
        vf_off = len(out); vf0 = (base + vf_off - PS1_FILETAB) // 12 - PS1_BGM_FIRST_NEW
        assert 0 <= vf0 <= 0xFFFF
        out += bytes(12 * n_vfiles)
    end2 = base + len(out)
    assert end2 <= 0x801F8000, f'overlay ends at {end2:#x}'
    return out, dict(table=new_tab, code=code_base, wood_man=sy['wood_man'], wood_leaf=sy['wood_leaf'], sat_data=sy['sat_data'],
                     code_bytes=len(data), end=end2, stub=stub, csrc=path, helpers=len(port.used_helpers), vf0=vf0, vf_off=vf_off)

def place_file(disc, lba, data):
    """Write a new file into the free sectors at `lba` (must be empty); returns the sector count."""
    nsec = (len(data) + 2047) // 2048
    assert disc.free[0] <= lba and lba + nsec <= disc.free[1]
    assert disc.read(lba, nsec * 2048) == bytes(nsec * 2048), 'free area not empty'
    disc.write(lba, 0, data + bytes(nsec * 2048 - len(data)))
    return nsec

def place_overlay(disc, idx, new, iso_path, lba):
    """Write a grown overlay into the free sectors at `lba`, repoint file-table entry `idx` and the ISO record."""
    nsec = place_file(disc, lba, new)
    disc.set_file_entry(idx, lba, len(new))
    rl, ro, ext, sz = disc.iso_record(iso_path)
    disc.write(rl, ro + 2, struct.pack('<I', lba) + struct.pack('>I', lba) + struct.pack('<I', len(new)) + struct.pack('>I', len(new)))
    return nsec

def place_moved_file(disc, idx, data, iso_path, lba):
    """Write a grown file (overlay or PAC) into the free sectors at `lba`, repoint file-table entry `idx` and its
    ISO record (the game reads files only through the EXE file table; the ISO record is kept consistent)."""
    nsec = place_file(disc, lba, data)
    disc.set_file_entry(idx, lba, len(data))
    rl, ro, ext, sz = disc.iso_record(iso_path)
    disc.write(rl, ro + 2, struct.pack('<I', lba) + struct.pack('>I', lba) + struct.pack('<I', len(data)) + struct.pack('>I', len(data)))
    return nsec

# ----------------------------------------------------------------- M4: Cut Man in STAGE09
# Map (02 §PS1 data compared, STAGE09): grid row 4 cols 5-7 / 0xC-0xE zeroed on PS1; screens 0x18-0x1C deleted; screen
# 0x15 (row 5 col 6, under the hidden room) edited by the PS1 (ceiling closed); screen 0x10 kept (also at row 5 col 5).
M4_GRID_CELLS = {(0, 4, 5): 0x10, (0, 4, 6): 0x18, (0, 4, 7): 0x19, (0, 4, 0xC): 0x1A, (0, 4, 0xD): 0x1B, (0, 4, 0xE): 0x1C}
M4_NEW_SCREENS = list(range(0x18, 0x1D))
M4_REPLACED_SCREENS = [0x15]
M4_TILE_FIRST = 0x4A6            # PS1 STAGE09 0x103 slots 0x4A6-0x4AF blank and unused; the chunk (0x4B0 tiles) grows after
# Placements (PS1 byte layout): bolt 38's floor record removed (Cut Man drops it, 0x258E9E); the five Saturn-only records:
# Cut Man, three P5 0x34 (red alarm flash + siren, ported: p5_alarm) and the P5 0x11 camera trigger v0x0B (bounds 0x7B/0x4A)
# (read from the discs by position: PS1 STAGE09 record 58 = bolt 38; Saturn STAGE09 records 59, 69, 70, 71, 72)
M4_BOLT38_IDX, M4_SAT_IDX, M4_FP = 58, (59, 69, 70, 71, 72), '6f6288ec00d4'
def m4_bolt38(): return place_rec('ps1', 'STAGE09', M4_BOLT38_IDX)
def m4_records(): return [place_rec('sat', 'STAGE09', i) for i in M4_SAT_IDX]
PS1_STAGE09_FILE, PS1_STAGE09_ENTRY = 18, None
PS1_STAGE09_PAC_FILE = 131
M4_MAGIC = b'MM8R-M4\0'
PS1_P5_LUI = {'STAGE09': 0x801D8340}       # lui s1,0x8015 / addiu s1,s1,0x39C: the overlay's P5 loop -> EXE table

def m4_placements(chunk):
    """Chunk 0x0A of STAGE09 with bolt 38's floor record removed and the Saturn records inserted before the terminator."""
    recs = [chunk[i:i + 8] for i in range(0, placement_terminator(chunk), 8)]
    bolt, new = m4_bolt38(), m4_records()
    assert fp(bolt + b''.join(new)) == M4_FP, 'unexpected STAGE09 placement records'
    assert recs.count(bolt) == 1
    recs.remove(bolt)
    out = b''.join(recs + new) + bytes([0, 0, 0, 0xFF]) + bytes(4)
    assert len(out) <= len(chunk)
    return out + bytes(len(chunk) - len(out))

def restore_stage09():
    """STAGE09.PAC entries with Cut Man's hidden room and arena (map, sprite set 0xA9-0xAA, 0x102 tiles 0x500-0x67F,
    palettes 0x30-0x32 / 0x5B / 0x61) and placements restored; grids merged. Text: unchanged (the PS1 kept talks 31-35)."""
    new, info = restore_stage_map('STAGE09', M4_GRID_CELLS, M4_NEW_SCREENS, M4_REPLACED_SCREENS, M4_TILE_FIRST)
    spr, sinfo = restore_stage_sprites('STAGE09'); new.update(spr); info.update(sinfo)
    assert pac_chunks('sat', 'STAGE09')[0x12] == pac_chunks('ps1', 'STAGE09')[0x12]
    ents = [(t, new.get(t, b)) for t, b in pac_entries(os.path.join(PS1_FILES, 'STDATA', 'STAGE09.PAC'))]
    ents = [(t, m4_placements(b) if t == 0xA else b) for t, b in ents]
    return pac_merge_grids(ents), info

def patch_m4a(disc, lba):
    """Rebuild STAGE09.PAC (restore_stage09) and move it to the free sectors at `lba` (it grows by ~10 sectors and its
    neighbour STAGE09 file follows directly): file-table entry 131 + ISO record repointed. Returns info."""
    ext, sz, _ = pac_locate(disc, 'STDATA/STAGE09.PAC')
    assert disc.read(ext, sz) == _file(os.path.join(PS1_FILES, 'STDATA', 'STAGE09.PAC')), 'STAGE09.PAC already patched?'
    assert disc.file_entry(PS1_STAGE09_PAC_FILE)[:2] == (ext, sz)
    ents, info = restore_stage09()
    pac = pac_build(ents)
    info.update(pac09_lba=lba, pac09_size=len(pac), pac09_sectors=place_moved_file(disc, PS1_STAGE09_PAC_FILE, pac, 'STDATA/STAGE09.PAC', lba))
    return info

# ----------------------------------------------------------------- M5: floor bolt 19
# The PS1 placed bolt 19 on a floor of Sword Man's stage (STAGE05 and STAGE05B share the same chunk 0x0A); on the
# Saturn it is Wood Man's drop (0x259B56). Removing the record the same way as bolt 38 (M4a) leaves Wood Man as its
# only source. Both PACs keep their size and LBA: the chunk is patched in place.
M5_BOLT19_IDX, M5_FP = 12, 'd65cff91fc59'           # PS1 STAGE05 record 12 = P3 0x25 variant 19 (read from the disc)
def m5_bolt19():
    r = place_rec('ps1', 'STAGE05', M5_BOLT19_IDX); assert fp(r) == M5_FP, 'unexpected STAGE05 record 12'; return r

# ---- Credits hint (Phase 3 item 11): the Saturn's last credits page adds "BONUS MODE (L+R+START BUTTON)" under "AND
# CAPCOM ALL STAFF". ENDING.PAC (same chunk 5 on both discs) holds the line as frame 0xC7 of the credits text sets
# 0xA3-0xA6 (one frame table; 26 pieces of the 16 x 16 credits font, 4 bytes each: glyph, palette bits, x, y); the Saturn's
# page builder 0x2750F0 spawns it as a second P4 0x3A text object at (0xC0, 0x88) when the page's text frame is 0x91,
# the PS1's builder doesn't. The PS1 port changes data only: frame 0x91 (only used by that page; its object stands at
# (0xBC, 0x60)) gets the 0xC7 pieces appended, moved by (+4, +0x28), with "1" (glyph 40) after L and R (the port's
# L1 + R1 + Start) and the rest of the line moved right to make room. The new piece list goes to the end of chunk 5
# (still inside the chunk's sectors, so ENDING.PAC keeps its size and LBA; the chunk-5 buffer 0x80078000 takes
# STAGE0D's 135,884 bytes).
M8C_SET, M8C_PAGE_FRAME, M8C_HINT_FRAME = 0xA6, 0x91, 0xC7
M8C_PAGE_FP, M8C_HINT_FP = '60f8d90e831a', '11fd30bbff8a'
M8C_HINT_DX, M8C_HINT_DY = 0xC0 - 0xBC, 0x88 - 0x60
M8C_GLYPH_ONE, M8C_GLYPH_L, M8C_GLYPH_R = 40, 11, 18

def m8c_hint_pieces(pcs):
    """The hint frame's pieces [(glyph, pal, x, y)] -> the PS1 wording: "1" after the first L and the first R of the
    second line (L+R -> L1+R1; START keeps its R). The second line grows by two glyphs, so it starts 16 px further left
    and ends where the Saturn's ends (any further right runs into the page's frame)."""
    line2 = [p for p in pcs if p[3]]
    x0 = -8 * 2
    out, shift, todo = [], x0, [M8C_GLYPH_L, M8C_GLYPH_R]
    for g, pal, x, y in pcs:
        if not y: out.append((g, pal, x, y)); continue
        out.append((g, pal, x + shift, y))
        if todo and g == todo[0]:
            todo.pop(0); shift += 8; out.append((M8C_GLYPH_ONE, pal, x + shift, y))
    assert not todo and len(out) == len(pcs) + 2 and max(p[2] for p in out if p[3]) == max(p[2] for p in line2)
    return out

def m8c_ending_pac(pac):
    """ENDING.PAC bytes (PS1) -> the same file with the credits hint on the last page (above)."""
    ent = [(t, b) for t, o, b in unpac_bytes(pac)]
    d = dict(ent)
    c5 = bytearray(d[5]); fo = struct.unpack_from('<I', d[6], 4 * M8C_SET)[0]
    s8 = lambda v: v - 256 if v > 127 else v
    def frame(f):
        cnt, po = struct.unpack_from('<HH', c5, fo + 4 * f)
        raw = bytes(c5[fo + po:fo + po + 4 * cnt])
        return raw, [(raw[k], raw[k + 1], s8(raw[k + 2]), s8(raw[k + 3])) for k in range(0, len(raw), 4)]
    page_raw, page = frame(M8C_PAGE_FRAME); hint_raw, hint = frame(M8C_HINT_FRAME)
    assert fp(page_raw) == M8C_PAGE_FP and fp(hint_raw) == M8C_HINT_FP, 'unexpected ENDING.PAC credits frames'
    new = page + [(g, p, x + M8C_HINT_DX, y + M8C_HINT_DY) for g, p, x, y in m8c_hint_pieces(hint)]
    assert all(-128 <= x < 128 and -128 <= y < 128 for g, p, x, y in new)
    while len(c5) % 4: c5.append(0)
    at = len(c5)
    c5 += b''.join(bytes([g, p, x & 0xFF, y & 0xFF]) for g, p, x, y in new)
    assert at - fo < 0x10000
    struct.pack_into('<HH', c5, fo + 4 * M8C_PAGE_FRAME, len(new), at - fo)
    out = pac_build([(t, bytes(c5) if t == 5 else b) for t, b in ent])
    assert len(out) == len(pac), 'ENDING.PAC would grow'
    return out

def patch_m8_credits(disc):
    """Credits hint on the PS1's last credits page: ENDING.PAC rewritten in place. Returns info."""
    ext, sz, chunks = pac_locate(disc, 'STDATA/ENDING.PAC')
    old = disc.read(ext, sz)
    assert old == _file(os.path.join(PS1_FILES, 'STDATA', 'ENDING.PAC')), 'ENDING.PAC already patched?'
    new = m8c_ending_pac(old)
    disc.write(ext, 0, new)
    return dict(ending_lba=ext, ending_size=sz)

# ---- README item 4 (Phase 3): the boss-intro name ("FROST MAN") as on the Saturn. The name is drawn with the big italic
# font of SELECT.PAC on both systems: glyph tiles 0x1A0-0x1BE of chunk 0x102, one sprite frame per letter A-Z (chunk 5,
# from offset M8N_FRAMES_AT), palette 15 of chunk 9. Two differences, both ported:
#  - colors: entries 1-8 of palette 15 (the font's light-to-dark ramp, outline and shadow) are orange-gold on the
#    Saturn and pink-white on the PS1; entries 0 and 9-15 are 0 on both, and the other 127 palettes are the same.
#    The Saturn's 15-bit colors are written into palette 15, keeping each PS1 entry's bit 15 (STP; 0 in every entry).
#  - letters: the Saturn's glyphs are bolder, with a heavy outline and two-tile M, Q and W; it has only the letters the
#    eight names use (the other slots hold a crossed filler, the only pixels in color 9) where the PS1 has a redrawn
#    thin A-Z. The Saturn's 31 tiles (the only tiles of the chunk that differ after gfx_sat_to_ps1) and its letter
#    frames (chunk 5 after front_pac_sat_to_ps1 differs only from M8N_FRAMES_AT on, 8 bytes longer: the 2-piece
#    M, Q, W) replace the PS1's. Chunks 6-8 stay the PS1's (chunk 7's one differing word is not one of the font's sets).
# Every chunk keeps its sector count, so SELECT.PAC keeps its size and LBA.
M8N_PAL, M8N_TILES, M8N_FRAMES_AT = 15, range(0x1A0, 0x1BF), 0x53E0
M8N_SAT_FP, M8N_PS1_FP, M8N_FRAME_DIFF_FP = 'ccbd688d2917', 'ee28ea1158e3', '25307111fb0d'

def m8n_palette():
    """(Saturn, PS1) palette M8N_PAL of SELECT.PAC chunk 9, 16 entries each, read from the discs."""
    s = pac_chunks('sat', 'SELECT')[9][32 * M8N_PAL:32 * M8N_PAL + 32]
    p = pac_chunks('ps1', 'SELECT')[9][32 * M8N_PAL:32 * M8N_PAL + 32]
    return list(struct.unpack('>16H', s)), list(struct.unpack('<16H', p))

def m8n_select_pac(pac):
    """SELECT.PAC bytes (PS1) -> the same file with the Saturn's boss-name colors, glyphs and letter frames (above)."""
    sp, pp = m8n_palette()
    assert fp(sp) == M8N_SAT_FP and fp(pp) == M8N_PS1_FP, 'unexpected SELECT.PAC palette 15'
    ent = [(t, b) for t, o, b in unpac_bytes(pac)]
    d = dict(ent); sat = pac_chunks('sat', 'SELECT')
    c9 = bytearray(d[9])
    assert c9[32 * M8N_PAL:32 * M8N_PAL + 32] == struct.pack('<16H', *pp)
    struct.pack_into('<16H', c9, 32 * M8N_PAL, *[(s & 0x7FFF) | (p & 0x8000) for s, p in zip(sp, pp)])
    c102 = gfx_sat_to_ps1(sat[0x102], False)
    assert len(c102) == len(d[0x102]) and [t for t in range(len(c102) // 128) if ps1_tile(c102, t) != ps1_tile(d[0x102], t)] == list(M8N_TILES), 'SELECT.PAC font tiles'
    c5 = front_pac_sat_to_ps1(5, sat[5])
    diff = [i for i in range(0, len(d[5]), 4) if c5[i:i + 4] != d[5][i:i + 4]]
    assert fp(diff) == M8N_FRAME_DIFF_FP and diff[0] == M8N_FRAMES_AT and len(c5) == len(d[5]) + 8, 'SELECT.PAC letter frames'
    assert (len(c5) + 0x7FF) // 0x800 == (len(d[5]) + 0x7FF) // 0x800
    rep = {9: bytes(c9), 0x102: c102, 5: c5}
    out = pac_build([(t, rep.get(t, b)) for t, b in ent])
    assert len(out) == len(pac), 'SELECT.PAC would change size'
    return out

def patch_m8_name_color(disc):
    """Boss-intro name as on the Saturn (colors, glyphs, letter frames): SELECT.PAC rewritten in place. Returns info."""
    ext, sz, chunks = pac_locate(disc, 'STDATA/SELECT.PAC')
    old = disc.read(ext, sz)
    assert old == _file(os.path.join(PS1_FILES, 'STDATA', 'SELECT.PAC')), 'SELECT.PAC already patched?'
    disc.write(ext, 0, m8n_select_pac(old))
    return dict(select_lba=ext, select_size=sz)

# ---- README item 5 (Phase 3): the HUD's Mega Man head icon at the Saturn's position. The life-bar panel is drawn from a
# list of 8-byte sprite records (u16 x, u16 y, then tile and palette bytes; terminator byte 7 = 0xFF): PS1 EXE
# 0x80136DB0 (drawn by 0x800FADD8 from the life-bar code 0x800FA6AC), Saturn 1STREAD 0x0602B284 (drawn by the HUD
# 0x06011280). Both hold the same nine pieces: the frame pieces at the same positions, the head icon at y 0x53 (first
# on the PS1, last on the Saturn), its x one pixel further left on the PS1. The lives digit is a separate sprite: x set by
# `ori v0, zero, x` at PS1 0x800FA7D8 and by `mov #x, r3` at Saturn 0x060113BA (y 0x53 on both), one pixel further right
# on the PS1. So on the Saturn the head and the digit sit a pixel closer together. The port takes both x values from
# the Saturn disc: the head record's x and the digit's immediate.
M8H_PS1_LIST, M8H_SAT_LIST, M8H_PS1_HEAD, M8H_SAT_HEAD = 0x80136DB0, 0x0602B284, 0, 7    # list addresses, head record
M8H_PS1_FP, M8H_SAT_FP = '1d9882304234', 'bb04e6119ed1'                                    # the 9-record lists (72 bytes)
M8H_PS1_DIGIT, M8H_SAT_DIGIT = 0x800FA7D8, 0x060113BA                                      # lives digit x instructions
M8H_PS1_DIGIT_FP, M8H_SAT_DIGIT_FP = 'bb6ee4f21a5f', '62f16d7ea0ca'

def m8h_digit_x():
    """The Saturn's lives-digit x: the immediate of its `mov #x, r3` (read from the Saturn disc)."""
    w = Sat().u16(M8H_SAT_DIGIT)
    assert fp(w) == M8H_SAT_DIGIT_FP, 'unexpected Saturn HUD code'
    return w & 0xFF

def m8h_head_x():
    """The Saturn's head-icon x, read from the Saturn disc (record M8H_SAT_HEAD of the list at M8H_SAT_LIST)."""
    sat = Sat()
    assert fp(sat.raw(M8H_SAT_LIST, 72)) == M8H_SAT_FP, 'unexpected Saturn HUD list'
    return sat.u16(M8H_SAT_LIST + 8 * M8H_SAT_HEAD)

def patch_m8_hud_head(disc):
    """HUD head icon and lives digit at the Saturn's positions: the head record's x in the PS1 EXE's life-bar panel list
    and the digit's `ori` immediate. Returns info."""
    assert fp(disc.exe_words(M8H_PS1_LIST, 18)) == M8H_PS1_FP, 'unexpected PS1 HUD list (already patched?)'
    a = M8H_PS1_LIST + 8 * M8H_PS1_HEAD
    old = struct.unpack('<H', disc.exe_read(a, 2))[0]; new = m8h_head_x()
    assert new == old + 1, 'Saturn and PS1 head-icon x not one pixel apart'
    disc.exe_write(a, struct.pack('<H', new))
    w = disc.exe_words(M8H_PS1_DIGIT, 1)[0]
    assert fp(w) == M8H_PS1_DIGIT_FP, 'unexpected PS1 lives-digit code'
    dx = m8h_digit_x(); assert dx == (w & 0xFFFF) - 1, 'Saturn and PS1 lives-digit x not one pixel apart'
    disc.exe_write_words(M8H_PS1_DIGIT, [(w & 0xFFFF0000) | dx])
    return dict(hud_head_x=(old, new), hud_digit_x=(w & 0xFFFF, dx))

# ---- Phase 3 items 1 + 2: the Saturn's enemy and item placements in every stage, with Sword Man's bats.
# Every STAGExx(B).PAC gets the Saturn's chunk 0x0A (records in the PS1 byte layout, Saturn order; same 0x800-byte chunk,
# so every PAC keeps its size and LBA). This supersedes the Phase-1 placement edits (the Saturn lists hold Wood Man,
# Cut Man, their gates / triggers / alarms, and neither floor bolt 19 nor 38). Left out: P5 0x29 (the Saturn's line-scroll
# water, item 3, not ported; the PS1 P5 table entry points into overlay code that is not a handler outside STAGE00).
# The attract demos (PDEMO00-04) keep the PS1 lists: their recorded input was made for them. Every other type the Saturn
# places in a stage the PS1 already places in that stage, except Sword Man's bat (P2 0x22): the PS1 STAGE05 overlay
# table points it at the stub, so the Saturn handler 0x22442C is lifted to C (like the bosses) and appended to
# STAGE05.BIN (moved to the free sectors), with table entry 0x22 pointing at it.
M8P_SKIP = {(0x29, 4)}                                # (type, pool) not carried over
M8P_STAGES = [f'STAGE0{c}{h}' for c in '0123456789ABCD' for h in ('', 'B')]   # filtered to the PACs on both discs
M8P_MAGIC = b'MM8R-M8\0'
PS1_STAGE05_FILE = 14
M8_ABS_C = """/* Saturn 0x06019C50 = abs(r4) (C library; cmp/pl, neg). */
__attribute__((noinline, noipa)) u32 h_06019c50(u32 a, u32 b, u32 c, u32 d) { s32 x = (s32)a; return (u32)(x > 0 ? x : -x); }"""
def _bat_state(rnd): return {1: rnd.randrange(3), 2: rnd.randrange(4), 3: rnd.randrange(8), 6: 0x22, 7: rnd.choice([0, 1])}
def _bat_cam(rnd):
    cx = rnd.randrange(0x800, 0x1000)
    return [(6, cx), (8, rnd.choice([0xA00, 0x1400, rnd.randrange(0x1400, 0x1700)])), (0x14, cx), (0x16, 0x1400)]
BAT_CODE_TABLES = {0x290CF8: 3, 0x290D04: 4}          # phase table (3), state table of phase 1 (4)
BAT_TRIALS = {'bat': dict(fn='bat', entry=0x22442C, pool='P2', layout='wood', state=_bat_state, children=[0x22, 0x22, 0x39],
                          player_x=(0x800, 0x1000), cam=_bat_cam)}
BAT_SPEC = dict(name='bat', data=(0x290CEC, 0x290D14), globals=CUT_GLOBALS, code_tables=BAT_CODE_TABLES, half_tables=[],
                cfile='m8_bat.c', blocks=CUT_BLOCKS, custom={0x06019C50: M8_ABS_C}, custom_arity={0x06019C50: 1},
                trials=BAT_TRIALS, handlers=[('bat', 0x22442C, 0x22, list(BAT_CODE_TABLES), OBJ_WORD_FIELDS)])

def m8p_stages():
    """Stage PACs present on both discs (STAGE09 and STAGE0D have no B half)."""
    return [st for st in M8P_STAGES if os.path.exists(os.path.join(SAT_FILES, st + '.PAC'))
            and os.path.exists(os.path.join(PS1_FILES, 'STDATA', st + '.PAC'))]

def m8p_chunk(stage):
    """The PS1 chunk 0x0A for `stage` built from the Saturn's list (PS1 byte layout, M8P_SKIP left out), then the PS1's
    own terminator record and zero padding to the PS1 chunk size."""
    sc, pc = pac_chunks('sat', stage)[0xA], pac_chunks('ps1', stage)[0xA]
    recs = [sc[i:i + 4] + struct.pack('<2H', *struct.unpack('>2H', sc[i + 4:i + 8]))
            for i in range(0, placement_terminator(sc), 8) if (sc[i + 1], sc[i + 3]) not in M8P_SKIP]
    t = placement_terminator(pc)
    assert set(pc[t + 8:]) <= {0} and len(recs) < 255, stage
    out = b''.join(recs) + pc[t:t + 8]
    return out + bytes(len(pc) - len(out))

def build_m8_bat_overlay(ovl, workdir=None):
    """STAGE05.BIN + marker + lifted bat code + data; enemy-table entry 0x22 -> the bat (in place). Returns (bytes, info)."""
    base = Ps1.OVL_BASE; img = Img(bytes(ovl), base)
    tab = Ps1().find_enemy_table(img)
    stub = collections.Counter(img.u32(tab + 4 * k) for k in range(PS1_P2_LEN)).most_common(1)[0][0]
    assert img.u32(tab + 4 * 0x22) == stub, 'STAGE05 entry 0x22 is not the stub'
    magic = (base + len(ovl) + 0xF) & ~0xF
    code_base = (magic + len(M8P_MAGIC) + 0xF) & ~0xF
    data, sy, port, path = boss_compile(BAT_SPEC, code_base, workdir)
    out = bytearray(ovl) + bytes(magic - base - len(ovl)) + M8P_MAGIC + bytes(code_base - magic - len(M8P_MAGIC)) + data
    out[tab - base + 4 * 0x22:tab - base + 4 * 0x22 + 4] = struct.pack('<I', sy['bat'])
    assert base + len(out) <= 0x801F8000
    return bytes(out), dict(table=tab, bat=sy['bat'], code=code_base, code_bytes=len(data), end=base + len(out), csrc=path,
                            helpers=len(port.used_helpers))

def patch_m8_placements(disc, lba, workdir=None):
    """Saturn chunk 0x0A in every stage PAC (in place) + the bat overlay (STAGE05.BIN moved to `lba`). Returns info."""
    info = {}
    for st in m8p_stages():
        ext, sz, chunks = pac_locate(disc, f'STDATA/{st}.PAC')
        t, off, n = next(c for c in chunks if c[0] == 0xA)
        new = m8p_chunk(st); assert len(new) == n, st
        disc.write(ext, off, new)
    info['stages'] = len(m8p_stages())
    e05 = disc.file_entry(PS1_STAGE05_FILE); ovl = disc.read(e05[0], e05[1])
    assert md5_bytes(ovl) == KNOWN_MD5[('ps1', 'OVL/STAGE05.BIN')], 'unexpected STAGE05 overlay (already patched?)'
    new5, i5 = build_m8_bat_overlay(ovl, workdir)
    info.update({'bat_' + k: v for k, v in i5.items()}, ovl05_lba=lba, ovl05_size=len(new5),
                ovl05_sectors=place_moved_file(disc, PS1_STAGE05_FILE, new5, 'OVL/STAGE05.BIN', lba))
    return info

def relocate_p5_table(out, stage, extra, at):
    """Append a copy of the EXE P5 table (52 entries) + `extra` handlers (types 0x34...) at overlay offset `at` (out is
    extended as needed) and repoint the overlay's P5 loop (lui/addiu)."""
    base = Ps1.OVL_BASE
    exe = Ps1().exe
    tab = [exe.u32(PS1_P5_TABLE + 4 * k) for k in range(PS1_P5_LEN)] + list(extra)
    assert len(out) <= at
    out += bytes(at - len(out)) + struct.pack('<%dI' % len(tab), *tab)
    o = PS1_P5_LUI[stage] - base
    hi_w, lo_w = struct.unpack('<2I', out[o:o + 8])
    assert Ps1.lui_addiu(Img(bytes(out), base), PS1_P5_LUI[stage]) == PS1_P5_TABLE
    out[o:o + 8] = struct.pack('<2I', *patch_lui_addiu(lo_w, hi_w, base + at))
    return base + at

def build_m4_overlay(ovl, vf_addr, workdir=None):
    """STAGE09.BIN + 153-entry P2/P3 table (0x62 = Cut Man) + marker + lifted Cut Man / P5 0x34 code + data + 53-entry
    P5 table (0x34 = p5_alarm), then 4 virtual file entries for music 0x46-0x49 at vf_addr (= the address STAGE08's
    overlay uses, since patch_music_hook has one vf0 for every stage; the caller fills them in)."""
    base = Ps1.OVL_BASE
    new_tab = (base + len(ovl) + 0xF) & ~0xF
    magic = new_tab + 153 * 4
    code_base = (magic + len(M4_MAGIC) + 0xF) & ~0xF
    data, sy, port, path = boss_compile(CUT_SPEC, code_base, workdir)
    out, tab, stub = relocate_enemy_table(ovl, 'STAGE09', [sy['cut_man'], None, None])
    assert tab == new_tab
    out += M4_MAGIC + bytes(code_base - (magic + len(M4_MAGIC))) + data
    p5 = relocate_p5_table(out, 'STAGE09', [sy['p5_alarm']], (len(out) + 0xF) & ~0xF)
    assert base + len(out) <= vf_addr, f'STAGE09 overlay ends at {base + len(out):#x}, past the music entries at {vf_addr:#x}'
    vf_off = vf_addr - base
    out += bytes(vf_off - len(out)) + bytes(12 * 4)
    return out, dict(table=new_tab, code=code_base, cut_man=sy['cut_man'], p5_alarm=sy['p5_alarm'], p5_table=p5,
                     sat_data=sy['sat_data'], code_bytes=len(data), end=base + len(out), stub=stub, csrc=path,
                     helpers=len(port.used_helpers), vf_off=vf_off)

PS1_STAGE08_FILE, PS1_STAGE08_ENTRY = 17, (126370, 0x9394, 0xE)   # file-table index and original (LBA, size, type)
sectors = lambda n: (n + 2047) // 2048

def build_image(src_bin, out_bin, workdir=None, extra_patches=()):
    """The current patch on a raw PS1 track 1 (all in place, sector EDC/ECC fixed):
      - track 1 extended by PS1_EXTEND_SECTORS empty sectors (extend_track1);
      - STAGE08 overlay with the relocated enemy table + lifted Wood Man / leaf code (build_m2_overlay) in the free
        sectors, followed by PBGM46 (WOOD1 on Capcom's PBGM29, build_boss_intro_bank) and PBGM47 (WOOD2 converted
        whole, build_music_bank); music 0x46/0x47 reach them through virtual file entries in the overlay;
      - STAGE09 overlay with Cut Man (P2 0x62) and the alarm (P5 0x34) lifted to C (build_m4_overlay) and the
        restored STAGE09.PAC (patch_m4a), both moved to the free sectors, with PBGM48 (CUT1 on PBGM29) and PBGM49
        (CUT2 converted whole) between them; music 0x48/0x49 reach them through the overlay's virtual file entries;
      - the Saturn Tengu Man stage song (ST03A converted, PS1 stage sounds kept) in the free sectors
        (patch_m8_tengu_bank); music 5 still loads the PS1's PBGM05 until the switch rewrites its file-table entry;
      - play_bgm hook (patch_music_hook), sound bank-mismatch guard (patch_sound_guard);
      - STAGE08.PAC rebuilt in place + STAGE08B placements + Wood Man's door lists (patch_m2a);
      - every stage's placement list = the Saturn's (patch_m8_placements; also drops floor bolts 19 and 38) and the
        STAGE05 overlay with the lifted Saturn bat (P2 0x22), moved to the free sectors;
      - the last credits page gets "BONUS MODE (L1+R1+START BUTTON)" (ENDING.PAC in place, patch_m8_credits);
      - the boss-intro name as on the Saturn: orange-gold, the Saturn's letters (SELECT.PAC in place, patch_m8_name_color);
      - the HUD's head icon and lives digit at the Saturn's positions, each moved 1 px (EXE, patch_m8_hud_head);
      - extra_patches: callables f(disc, next_free_lba, workdir) -> (sectors used, info dict), applied last, after
        every port (none in the release build)."""
    disc = RawDisc(src_bin)
    extend_track1(disc)
    assert disc.file_entry(PS1_STAGE08_FILE) == PS1_STAGE08_ENTRY, 'unexpected STAGE08 table entry (already patched?)'
    nxt = disc.free[0]
    pbgm46, binfo = build_boss_intro_bank('WOOD1.PAC')
    pbgm47, finfo = build_music_bank('WOOD2.PAC')
    new, info = build_m2_overlay(disc.read(*PS1_STAGE08_ENTRY[:2]), workdir, 2)
    lba46 = nxt + sectors(len(new)); lba47 = lba46 + sectors(len(pbgm46))
    vf = [(lba46, len(pbgm46), 4), (lba47, len(pbgm47), 4)]                                  # music 0x46/0x47
    new[info['vf_off']:info['vf_off'] + 24] = struct.pack('<6I', *vf[0], *vf[1])
    info['overlay_sectors'] = place_overlay(disc, PS1_STAGE08_FILE, bytes(new), 'OVL/STAGE08.BIN', nxt)
    info.update(new_lba=nxt, new_size=len(new), pbgm46_lba=lba46, pbgm46_size=len(pbgm46),
                pbgm46_sectors=place_file(disc, lba46, pbgm46), **{'bank_' + k: v for k, v in binfo.items()})
    info.update(pbgm47_lba=lba47, pbgm47_size=len(pbgm47), pbgm47_sectors=place_file(disc, lba47, pbgm47),
                **{'bank47_' + k: v for k, v in finfo.items() if k not in ('layers', 'sfx')})
    nxt = lba47 + info['pbgm47_sectors']
    # ---- M4: Cut Man (STAGE09)
    e09 = disc.file_entry(PS1_STAGE09_FILE)
    ovl09 = disc.read(e09[0], e09[1])
    assert md5_bytes(ovl09) == KNOWN_MD5[('ps1', 'OVL/STAGE09.BIN')], 'unexpected STAGE09 overlay (already patched?)'
    new9, info9 = build_m4_overlay(ovl09, Ps1.OVL_BASE + info['vf_off'], workdir)
    pbgm48, i48 = build_boss_intro_bank('CUT1.PAC')       # music 0x48: CUT1 on PBGM29
    pbgm49, i49 = build_music_bank('CUT2.PAC')            # music 0x49: CUT2 converted whole
    lba48 = nxt + sectors(len(new9)); lba49 = lba48 + sectors(len(pbgm48))
    vf += [(lba48, len(pbgm48), 4), (lba49, len(pbgm49), 4)]                                 # music 0x48/0x49
    new9[info9['vf_off']:info9['vf_off'] + 48] = struct.pack('<12I', *[x for e in vf for x in e])
    info.update({'ovl09_' + k: v for k, v in info9.items()}, ovl09_lba=nxt, ovl09_size=len(new9),
                ovl09_sectors=place_moved_file(disc, PS1_STAGE09_FILE, bytes(new9), 'OVL/STAGE09.BIN', nxt))
    assert lba48 == nxt + info['ovl09_sectors']
    info.update(pbgm48_lba=lba48, pbgm48_size=len(pbgm48), pbgm48_sectors=place_file(disc, lba48, pbgm48),
                pbgm49_lba=lba49, pbgm49_size=len(pbgm49), pbgm49_sectors=place_file(disc, lba49, pbgm49),
                **{'bank48_' + k: v for k, v in i48.items()}, **{'bank49_' + k: v for k, v in i49.items() if k not in ('layers', 'sfx')})
    nxt = lba49 + info['pbgm49_sectors']
    m4 = patch_m4a(disc, nxt); nxt += m4['pac09_sectors']
    info.update({k if k.startswith('pac09') else 'm4_' + k: v for k, v in m4.items()})
    m8t = patch_m8_tengu_bank(disc, nxt); nxt += m8t['tengu_sectors']               # M8t3: the Saturn Tengu Man bank
    info.update(m8t)
    m7 = patch_m7a(disc, nxt, workdir, info['vf0']); nxt += m7['m7_sectors']          # M7a/M7b: title BONUS MODE, bonus overlay, SLABO
    info.update({'m7_' + k: v for k, v in m7.items()})
    patch_music_hook(disc, info['vf0'])
    patch_sound_guard(disc)
    info.update(patch_m2a(disc))
    m8p = patch_m8_placements(disc, nxt, workdir); nxt += m8p['ovl05_sectors']    # supersedes patch_m5 (bolt 19)
    info.update({'m8p_' + k: v for k, v in m8p.items()})
    info.update(patch_m8_credits(disc))
    info.update(patch_m8_name_color(disc))
    info.update(patch_m8_hud_head(disc))
    m8k = patch_m8_ticker(disc, nxt, (m8t['tengu_lba'], m8t['tengu_size']), workdir); nxt += m8k['tk_sectors']   # M8t4
    info.update({'m8k_' + k: v for k, v in m8k.items()})
    for f in extra_patches:
        used, more = f(disc, nxt, workdir); nxt += used; info.update(more)
    disc.save(out_bin)
    info.update(sectors_changed=len(disc.touched), free_lba=nxt, free_sectors_left=disc.free[1] - nxt)
    return info

def check_build(orig_bin, new_bin, demo_tail=None):
    """Independent check of a build: every sector that differs from the original (after the track-1 insertion) has a
    valid MSF header for its position, Mode 2 Form 1 and valid EDC/ECC (inserted sectors included; track 1's moved last
    sector unchanged apart from its header);
    the stage overlays' music entries 0x46-0x49 point at files equal to freshly built PBGM46-49. demo_tail(disc,
    want_len, demo) -> info: accepts code appended to DEMO.BIN (none in the release build). Returns a dict (raises
    AssertionError on a failure)."""
    o = open(orig_bin, 'rb').read(); n = open(new_bin, 'rb').read(); S = 2352
    last = len(o) // S - 1; ins = PS1_EXTEND_SECTORS
    assert len(n) == len(o) + ins * S
    bcd = lambda v: (v // 10) << 4 | v % 10
    changed = 0; to_fix = []
    for k in range(len(n) // S):
        sec = n[k * S:(k + 1) * S]; m = k + 150
        src = o[k * S:(k + 1) * S] if k < last else (None if k < last + ins else o[(k - ins) * S:(k - ins + 1) * S])
        if k < last and sec == src: continue
        changed += 1
        assert sec[:12] == b'\x00' + b'\xff' * 10 + b'\x00' and sec[12:16] == bytes([bcd(m // 4500), bcd(m // 75 % 60), bcd(m % 75), 2]), k
        if src is not None and k >= last:
            assert sec[16:] == src[16:], k              # track 1's last sector (ripper-quirk ECC): moved unchanged
            continue
        assert sec[18] & 0x20 == 0, f'Form 2 sector {k}'
        to_fix.append((k, sec))
    for (k, sec), fixed in zip(to_fix, fix_m2f1_many([x for _, x in to_fix])):
        assert fixed == sec, f'EDC/ECC sector {k}'
    disc = RawDisc(new_bin)
    vf = {}
    for idx in (PS1_STAGE08_FILE, PS1_STAGE09_FILE):
        lba, size, typ = disc.file_entry(idx); ovl = disc.read(lba, size)
        k = ovl.find(M2_MAGIC if idx == PS1_STAGE08_FILE else M4_MAGIC); assert k > 0
        vf0 = disc.exe_words(PS1_BGM_FILE_SITES[0] + 12, 1)[0] & 0xFFFF    # ori a0, zero, vf0
        for m in range(0x46, 0x4A if idx == PS1_STAGE09_FILE else 0x48):
            a = PS1_FILETAB + 12 * (vf0 + m) - Ps1.OVL_BASE
            vf.setdefault(m, set()).add(struct.unpack('<3I', ovl[a:a + 12]))
    want = {0x46: build_boss_intro_bank('WOOD1.PAC')[0], 0x47: build_music_bank('WOOD2.PAC')[0],
            0x48: build_boss_intro_bank('CUT1.PAC')[0], 0x49: build_music_bank('CUT2.PAC')[0]}
    for m, es in vf.items():
        assert len(es) == 1, (hex(m), es)          # one vf0 for every stage: same entries in both overlays
        lba, size, typ = next(iter(es))
        assert typ == 4 and size == len(want[m]) and disc.read(lba, size) == want[m], hex(m)
    out = dict(sectors_checked=changed, music={hex(m): next(iter(e)) for m, e in sorted(vf.items())})
    tg = find_m8_tengu_bank(disc)                    # M8t3: the Saturn Tengu Man bank = a fresh build; the music-5
    assert tg is not None, 'Saturn Tengu Man bank'   # entry is the PS1's (default) or points at that bank
    e5 = disc.file_entry(PS1_TENGU_FILE)
    assert e5 == RawDisc(orig_bin).file_entry(PS1_TENGU_FILE) or e5 == (*tg, 4), 'music 5 file entry'
    out['tengu_bank'] = tg; out['tengu_saturn'] = e5[:2] == tg
    # full-disc layout: with separate track files track 2 (END1.DA) starts after the new track 1 plus its
    # 150-sector pregap; entry 138, both ISO extents and the PVD size moved by the inserted sectors
    od = RawDisc(orig_bin); pv = lambda d: struct.unpack('<I', d.read(16, 84)[80:84])[0]
    lba138 = disc.file_entry(PS1_END1_FILE)[0]
    assert lba138 == len(n) // S + 150 == disc.iso_record('END1.DA')[2] == od.file_entry(PS1_END1_FILE)[0] + ins, 'END1.DA position'
    assert disc.iso_record('ZNULL.DAT')[2] == od.iso_record('ZNULL.DAT')[2] + ins and pv(disc) == pv(od) + ins, 'track 3 / PVD size'
    out['end1_lba'] = lba138
    for st in m8p_stages():                              # M8 items 1/2: chunk 0x0A = the Saturn's list; other chunks
        ext, sz, chunks = pac_locate(disc, f'STDATA/{st}.PAC'); oe, osz, _ = pac_locate(od, f'STDATA/{st}.PAC')
        t, off, cn = next(c for c in chunks if c[0] == 0xA)
        new_pac = disc.read(ext, sz)
        assert new_pac[off:off + cn] == m8p_chunk(st), st
        assert m5_bolt19() not in new_pac[off:off + cn] and not any(r[0] == 0x29 and r[2] == 4 for r in place_records(new_pac[off:off + cn], 'ps1')), st
        if st[:7] not in ('STAGE08', 'STAGE09'):          # (STAGE08/09.PAC are rebuilt by M2a / M4a)
            old_pac = od.read(oe, osz)
            assert (ext, sz) == (oe, osz) and new_pac[:off] == old_pac[:off] and new_pac[off + cn:] == old_pac[off + cn:], st
    e5 = disc.file_entry(PS1_STAGE05_FILE)
    want5, i5 = build_m8_bat_overlay(od.read(*od.file_entry(PS1_STAGE05_FILE)[:2]))
    assert disc.read(e5[0], e5[1]) == want5 and disc.iso_record('OVL/STAGE05.BIN')[2:] == (e5[0], e5[1]), 'STAGE05 overlay (bat)'
    out['m8_placements'] = len(m8p_stages()); out['m8_bat'] = (e5[0], e5[1])
    # M7a: DEMO.BIN rebuilt in its own slot; its virtual entry points at the bonus overlay
    e8, o8 = disc.file_entry(PS1_DEMO_FILE), od.file_entry(PS1_DEMO_FILE)
    assert e8[0] == o8[0] and e8[2] == o8[2] and e8[1] <= PS1_DEMO_SLOT * 2048, 'DEMO.BIN entry'
    assert disc.iso_record('OVL/DEMO.BIN')[2:] == (e8[0], e8[1]), 'DEMO.BIN ISO record'
    want_demo, want_bonus, i7 = build_m7_overlays(od.read(*o8[:2]))
    demo = disc.read(*e8[:2])
    m7len = len(want_demo)                               # M8t4: tk_load + the ticker overlay's entry follow the M7 code
    _, ti = m8k_demo_tail(m7len, 0, 0)
    tl, ts, tt = struct.unpack('<3I', demo[ti['tk_vf_off']:ti['tk_vf_off'] + 12])
    tail, ti = m8k_demo_tail(m7len, tl, ts)
    assert demo[m7len:m7len + len(tail)] == tail, 'DEMO.BIN: M8t4 tk_load'
    assert disc.read(tl, ts) == m8k_overlay(od.file_entry(PS1_TENGU_FILE)[:2], tg)[0], 'M8t4 ticker overlay'
    assert disc.exe_words(M8K_INIT_CALL, 1)[0] == 3 << 26 | (ti['tk_load'] >> 2) & 0x3FFFFFF, 'stage-select init call (tk_load)'
    assert disc.exe_words(M8K_RENDER_CALL, 1)[0] == 3 << 26 | (M8K_BASE >> 2) & 0x3FFFFFF, 'stage-select render call (tk_frame)'
    out['m8_ticker'] = (tl, ts)
    full = m7len + len(tail)
    if demo_tail is not None and len(demo) > full: out.update(demo_tail(disc, full, demo))
    else: assert len(demo) == full, 'DEMO.BIN size'
    demo = demo[:m7len]
    vl, vs, vt = struct.unpack('<3I', demo[i7['vf_off']:i7['vf_off'] + 12])
    want_demo[i7['vf_off']:i7['vf_off'] + 12] = struct.pack('<3I', vl, vs, vt)
    bonus = disc.read(vl, vs); so = i7['slabo_vf_off']; sl, ss, stp = struct.unpack('<3I', bonus[so:so + 12])
    io = i7['ill_vf_off']; ill = [struct.unpack('<3I', bonus[io + 12 * k:io + 12 * k + 12]) for k in range(M7C_ILL_COUNT)]
    lo_ = i7['letter_vf_off']; lfiles = m7d_letter()[0]
    let = [struct.unpack('<3I', bonus[lo_ + 12 * k:lo_ + 12 * k + 12]) for k in range(len(lfiles))]
    want_bonus = bytearray(want_bonus); want_bonus[so:so + 12] = bonus[so:so + 12]
    want_bonus[io:io + 12 * M7C_ILL_COUNT] = bonus[io:io + 12 * M7C_ILL_COUNT]
    want_bonus[lo_:lo_ + 12 * len(lfiles)] = bonus[lo_:lo_ + 12 * len(lfiles)]
    mo = i7['music_vf_off']                       # M7e: the overlay's copy of the music 0x46-0x49 entries = the stage overlays'
    assert [struct.unpack_from('<3I', bonus, mo + 12 * k) for k in range(4)] == [next(iter(vf[m])) for m in range(0x46, 0x4A)], 'bonus music entries'
    want_bonus[mo:mo + 48] = bonus[mo:mo + 48]
    assert demo == bytes(want_demo) and bonus == bytes(want_bonus), 'DEMO.BIN / bonus overlay'
    assert sl == vl + (vs + 2047) // 2048 and disc.read(sl, ss) == m7b_slabo_pac(), 'converted SLABO.PAC'
    il0 = sl + (ss + 2047) // 2048                       # M7c: ILL00-29 follow SLABO, 33 sectors each
    for k, (l, sz, t) in enumerate(ill):
        assert (l, sz) == (il0 + 33 * k, M7C_ILL_BYTES) and disc.read(l, sz) == m7c_ill_file(k), f'ILL{k:02d}'
    out['m7c_ill'] = (il0, 33 * M7C_ILL_COUNT)
    l0 = lb = il0 + 33 * M7C_ILL_COUNT                    # M7d: the LETTER files follow ILL29
    for k, (l, sz, t) in enumerate(let):
        assert (l, sz, t) == (lb, len(lfiles[k]), 0x14) and disc.read(l, sz) == lfiles[k], f'LETTER file {k}'
        lb += (sz + 2047) // 2048
    out['m7d_letter'] = (l0, lb - l0)
    assert disc.exe_words(0x800FF1B0, 1)[0] == 2 << 26 | (i7['title_hook'] >> 2) & 0x3FFFFFF, 'title hook'
    assert disc.exe_words(0x800FEFD4, 1)[0] == 3 << 26 | (i7['title_confirm'] >> 2) & 0x3FFFFFF, 'title confirm (ANIMATION input)'
    out['m7_bonus'] = (vl, vs); out['m7b_slabo'] = (sl, ss)
    ext, sz, _ = pac_locate(disc, 'STDATA/SELECT.PAC'); oe, osz, _ = pac_locate(od, 'STDATA/SELECT.PAC')
    assert (ext, sz) == (oe, osz) and disc.read(ext, sz) == m8n_select_pac(od.read(oe, osz)), 'SELECT.PAC (boss-intro name)'
    out['m8_name_color'] = (ext, sz)
    hx = M8H_PS1_LIST + 8 * M8H_PS1_HEAD                  # README item 5: the head icon's x = the Saturn's, the rest of the list as on the disc
    assert struct.unpack('<H', disc.exe_read(hx, 2))[0] == m8h_head_x() and disc.exe_read(M8H_PS1_LIST, 72)[2:] == od.exe_read(M8H_PS1_LIST, 72)[2:], 'HUD head icon'
    ow, nw = od.exe_words(M8H_PS1_DIGIT, 1)[0], disc.exe_words(M8H_PS1_DIGIT, 1)[0]
    assert nw == (ow & 0xFFFF0000) | m8h_digit_x(), 'HUD lives digit'
    out['m8_hud'] = (m8h_head_x(), m8h_digit_x())
    return out

# ----------------------------------------------------------------- sound banks (audio survey)
# Saturn "sound bank" PACs (STxx, BSxxB/C, WOOD1/2, CUT1/2, ...): chunk 0x201 = sound-ID table (8 bytes per ID; byte 0 =
# bank number, byte 3 = sequence number in the bank) and chunk 0x203 = Sega TON (tones; sample addresses relative to
# the chunk) + a Sega SEQ bank at offset 0x26000 (u16 song count, u32 song offsets). COMMON.PAC: 0x200 table + 0x202.
# Music command n (h_0600C610 / PS1 0x800FCB80) loads bank n+1; sound IDs >= 0x50 (PS1: >= 0x51) index the current
# bank's table at id - 0x50. PS1: SOUND/PBGMnn.PAC = Saturn bank n+1 (chunks 5 table, 4 VAB header, 0x201 VAB body,
# 1 SEQ), file-table entries 23 + n (70 files: n = 0x00-0x45). WOOD1/WOOD2/CUT1/CUT2 (banks 0x47-0x4A = music
# 0x46-0x49) have no PS1 counterpart.
SAT_SEQ_OFFSET = 0x26000

def sat_bank_chunks(name):
    """(ID table chunk 0x201/0x200, TON+SEQ chunk 0x203/0x202) of a Saturn sound bank."""
    ch = pac_chunks('sat', name)
    return ch.get(0x201) or ch.get(0x200), ch.get(0x203) or ch.get(0x202)

@functools.lru_cache(maxsize=None)
def sat_sound_bank(name):
    tab, ton = sat_bank_chunks(name)
    ids = {k: tab[8 * k:8 * k + 8] for k in range(len(tab) // 8) if any(tab[8 * k:8 * k + 8])}
    seq = None
    if ton and len(ton) > SAT_SEQ_OFFSET + 2:
        n = struct.unpack('>H', ton[SAT_SEQ_OFFSET:SAT_SEQ_OFFSET + 2])[0]
        seq = [struct.unpack('>I', ton[SAT_SEQ_OFFSET + 2 + 4 * k:SAT_SEQ_OFFSET + 6 + 4 * k])[0] for k in range(n)] if 0 < n < 64 else None
    return dict(bank=tab[0], ids=ids, songs=seq, sample_bytes=len(ton[:SAT_SEQ_OFFSET].rstrip(b'\0')) if ton else 0)

def sat_ton_voices(name):
    """Saturn TON voices of a sound bank: list per voice of layers (lo_note, hi_note, sa, nbytes, is_8bit, sample_bytes).
    Layer = 0x20 bytes: note range, u16 flags (bit 4 = 8-bit PCM, bits 0-3 = SA 19:16), SA, LSA, LEA (sample count)."""
    t = sat_bank_chunks(name)[1]
    o = struct.unpack('>H', t[0:2])[0]
    hdr = [struct.unpack('>H', t[i:i + 2])[0] for i in range(0, o, 2)]
    out = []
    for v in hdr[4:]:
        lay = []
        for l in range(t[v + 2] + 1):
            L = t[v + 4 + 0x20 * l:v + 4 + 0x20 * (l + 1)]
            fl, sa, lsa, lea = struct.unpack('>4H', L[2:10]); sa |= (fl & 0xF) << 16
            nb = lea * (1 if fl & 0x10 else 2)
            lay.append((L[0], L[1], sa, nb, bool(fl & 0x10), t[sa:sa + nb]))
        out.append(lay)
    return out

def sat_bank_song(name, k):
    """Raw Sega SEQ bytes of song k in a Saturn sound bank (up to the next song's offset)."""
    t = sat_bank_chunks(name)[1]; songs = sat_sound_bank(name)['songs']
    end = songs[k + 1] if k + 1 < len(songs) else len(t) - SAT_SEQ_OFFSET
    return t[SAT_SEQ_OFFSET + songs[k]:SAT_SEQ_OFFSET + end]

# ----------------------------------------------------------------- PS1 sound banks (PBGM46 / music hook)
# PS1 PBGMnn.PAC (file-table entry 23 + n, loaded by play_bgm 0x800FCB80 / 0x800FCA4C into dest table 0x801378BC):
#   chunk 5 -> 0x8016FB34 sound-ID table, 4 bytes per ID (index = sound ID; Saturn bank ID i -> index i + 0x50):
#             [bank | 0x80, 0x10 + note // 15 (VAB program; bit 7 = take pan from the tone), (note % 15) << 4 | priority,
#              0x10 | Saturn byte 1 (SPU voice, & 0x1F)]; play 0x800FCEB4 keys on tone (program, note % 15) with
#              SsUtKeyOnV(voice, vab, prog, tone, key = tone min key (= 60 + tone), fine = tone shift, vol = tone vol).
#   chunk 4 -> 0x801BE9AC VAB header ('pBAV' v7), chunk 0x201 -> SPU (VAB body), chunk 1 -> 0x801B29AC SEQ ('pQES').
# Saturn TON voice 0 = the bank's one-shot sounds (note k played by the one-sound song of each ID); its layer byte 25
# = base note B: Saturn rate = 44100 * 2^((k - B) / 12). Capcom upsampled these 2x (Saturn 8-bit signed -> 16-bit).
VAG_FILTERS = ((0, 0), (60, 0), (115, -52), (98, -55), (122, -60))
PS1_BGM_FILE_SITES = (0x800FCABC, 0x800FCC70)            # sll/lui/addu/lw a0,0x7714(at) ... jal 0x800FB9C0; nop
PS1_BGM_LOOP_SITES = ((0x800FCB2C, 2), (0x800FCC0C, 3), (0x800FCD28, 3))   # (site, index register: 2 = v0, 3 = v1)
PS1_BGM_FIRST_NEW = 0x46              # music numbers >= this use our virtual file entries

def vag_encode(pcm):
    """16-bit samples -> one-shot PS1 ADPCM in Capcom's layout: a zero block, data blocks (first flagged 4, last
    flagged 1 = end + mute), then the silent block 00 07 77..77."""
    pcm = list(pcm) + [0] * (-len(pcm) % 28)
    out = bytearray(16); s1 = s2 = 0
    nblk = len(pcm) // 28
    for b in range(nblk):
        err, f, sh, nib, s1, s2 = _vag_block(pcm[28 * b:28 * b + 28], s1, s2)
        flag = 4 if b == 0 else (1 if b == nblk - 1 else 0)
        if nblk == 1: flag = 5
        blk = bytearray([(f << 4) | sh, flag]) + bytes(((nib[2 * k] & 15) | ((nib[2 * k + 1] & 15) << 4)) for k in range(14))
        out += blk
    out += bytes([0, 7]) + bytes([0x77] * 14)
    return bytes(out)

def ps1_pbgm(n):
    return {t: b for t, o, b in unpac(os.path.join(PS1_FILES, 'SOUND', f'PBGM{n:02X}.PAC'))}

def sound_id_table(sat_name, size, tone_of):
    """PS1 chunk-5 sound-ID table for a converted bank (index = Saturn ID + 0x50; reference §Sound): one-shot note ->
    program 16 + tone // 15, tone % 15; muted notes (not in tone_of) get no entry (play 0x800FCEB4 ignores them)."""
    bank = sat_sound_bank(sat_name)['bank']; ids = bytearray(size)
    for i, e, prog, note in sat_bank_sounds(sat_name):
        assert prog == 0 and 4 * (i + 0x50) + 4 <= size
        if note not in tone_of: continue
        k = tone_of[note]
        ids[4 * (i + 0x50):4 * (i + 0x50) + 4] = bytes([bank | 0x80, 0x10 + k // 15, ((k % 15) << 4) | (e[4] & 15), 0x10 | e[1]])
    return bytes(ids)

def vab_split(hdr, body):
    """VAB header + body -> (fields, programs[128] (16 bytes), tones[ps][16] (32 bytes), vag list)."""
    magic, ver, vid, fsize, res, ps, ts, vs = struct.unpack('<4sIIIHHHH', hdr[:0x18])
    assert magic == b'pBAV' and ver in (6, 7)
    progs = [bytearray(hdr[0x20 + 16 * i:0x30 + 16 * i]) for i in range(128)]
    tones = [[bytearray(hdr[0x820 + 512 * p + 32 * t:0x820 + 512 * p + 32 * t + 32]) for t in range(16)] for p in range(ps)]
    so = 0x820 + 512 * ps
    sizes = [8 * v for v in struct.unpack('<256H', hdr[so:so + 512])]
    vags, off = [], 0
    for k in range(1, vs + 1): vags.append(body[off:off + sizes[k]]); off += sizes[k]
    assert off == len(body)
    return dict(head=bytearray(hdr[:0x20]), progs=progs, tones=tones, vags=vags)

def vab_join(v):
    """Inverse of vab_split (recomputes ps / ts / vs / file size / VAG size table)."""
    ps = len(v['tones']); vs = len(v['vags'])
    assert all(len(g) % 16 == 0 for g in v['vags']) and vs < 255
    sizes = [0] + [len(g) // 8 for g in v['vags']] + [0] * (255 - vs)
    ts = struct.unpack('<H', v['head'][0x14:0x16])[0]     # kept as given (PBGM3E's program 0 count is 0 with ts 1)
    body = b''.join(v['vags'])
    hsize = 0x820 + 512 * ps + 512
    h = bytearray(v['head']); h[12:16] = struct.pack('<I', hsize + len(body)); h[0x12:0x18] = struct.pack('<3H', ps, ts, vs)
    out = h + b''.join(v['progs']) + b''.join(b''.join(t) for t in v['tones']) + struct.pack('<256H', *sizes)
    assert len(out) == hsize
    return bytes(out), body

def sat_bank_sounds(name):
    """Saturn bank IDs (other than 0 = music) -> (id, entry, program, note) from their one-sound songs."""
    out = []
    for i, e in sorted(sat_sound_bank(name)['ids'].items()):
        if i == 0: continue
        s = sat_bank_song(name, e[3])
        assert s[0x10:0x14] == b'\xb0\x20\x01\x00' and s[0x14] == 0xC0 and s[0x1C:0x1E] == b'\x83\x00', (name, hex(i), s.hex())
        out.append((i, e, s[0x15], s[0x18]))
    return out

@_memo
def build_boss_intro_bank(sat_name, tpl_n=0x29, tpl_sat='BS08B.PAC'):
    """New PS1 boss-intro bank (PBGM46 from WOOD1, later PBGM48 from CUT1) built on Capcom's own conversion of a
    robot master's boss-intro bank (PBGM29 = BS08B): its jingle SEQ and music programs 0-15 stay as they are (identical
    on the Saturn, reference §Sound); program 16 gets the Saturn bank's voice-0 sounds. A sample Capcom already converted
    (same bytes in the template's voice 0) reuses Capcom's VAG and tone; the others are encoded at their original rate
    with center = 60 + base note (no resampling). Returns (pac bytes, info)."""
    bank = sat_sound_bank(sat_name)['bank']
    pc = ps1_pbgm(tpl_n)
    v = vab_split(pc[4], pc[0x201])
    tpl_tones = v['tones'][16]
    # template: Saturn note k (BS08B voice 0) -> PS1 tone k (keys 60 + k), VAG number
    tsat = sat_ton_voices(tpl_sat)[0]
    tpl_by_sample = {}
    for lay in tsat:
        k = lay[0]; t = tpl_tones[k]
        tpl_by_sample[lay[5]] = (k, lay, bytes(t))
    music_vags = v['vags'][:2]                      # VAG 1 (drums), 2 (instrument) used by programs 0-15
    assert all(struct.unpack('<H', t[0x16:0x18])[0] in (0, 1, 2) for p in range(16) for t in v['tones'][p])
    vags = list(music_vags); vag_of = {}
    new_tones = [bytearray(32) for _ in range(16)]
    lays = sat_ton_voices(sat_name)[0]
    assert len(lays) <= 15
    info = dict(reused=[], encoded=[], muted=[])
    tone_of = {}
    for lay in lays:
        k, hi, sa, nb, is8, smp = lay
        assert k == hi and is8
        ton = sat_layer_raw(sat_name, 0, k)
        if ton[24] >> 5 == 0:                         # direct level 0: silent on the US Saturn (Japanese voice lines)
            info['muted'].append((k, len(smp))); continue
        B = ton[25]; fine = ton[26] - 256 if ton[26] > 127 else ton[26]   # CUT1 notes 2/3: fine 0x7E
        tone_of[k] = len(tone_of)
        key = 60 + tone_of[k]
        if smp in tpl_by_sample:                     # Capcom converted this exact sample: reuse VAG + tone
            tk, tlay, traw = tpl_by_sample[smp]
            tB = sat_layer_raw(tpl_sat, 0, tk)[25]
            if sa not in vag_of:
                vags.append(v['vags'][struct.unpack('<H', traw[0x16:0x18])[0] - 1]); vag_of[sa] = len(vags)
            t = bytearray(traw)
            t[4] = key - (k - B) - ((traw[6] - traw[4]) - (tk - tB))   # Capcom's resampling offset kept
            info['reused'].append((k, len(smp)))
        else:
            if sa not in vag_of:
                pcm = [(x - 256 if x > 127 else x) << 8 for x in smp]
                vags.append(vag_encode(pcm)); vag_of[sa] = len(vags)
                info['encoded'].append((k, len(smp), len(vags[-1])))
            t = bytearray(tpl_tones[0]); t[2] = 127; t[3] = 64
            t[4], t[5] = _center_shift(key + B - k - fine / 256.0)    # = key + B - k, shift 0 when fine = 0 (PBGM46)
        t[6] = t[7] = key
        t[0x14:0x18] = struct.pack('<2H', 16, vag_of[sa])
        new_tones[tone_of[k]] = t
    v['tones'][16] = new_tones
    n_old, n_new = v['progs'][16][0], sum(1 for t in new_tones if any(t))
    v['progs'][16][0] = n_new
    v['head'][0x14:0x16] = struct.pack('<H', struct.unpack('<H', v['head'][0x14:0x16])[0] - n_old + n_new)
    v['vags'] = vags
    hdr, body = vab_join(v)
    pac = pac_build([(5, sound_id_table(sat_name, len(pc[5]), tone_of)), (4, hdr), (0x201, body), (1, pc[1])])
    info.update(bank=bank, hdr=len(hdr), body=len(body), seq=len(pc[1]), size=len(pac), vags=len(vags))
    return pac, info

def sat_layer_raw(name, voice, note):
    """Raw 0x20-byte TON layer of `voice` that covers `note`."""
    t = sat_bank_chunks(name)[1]
    v = struct.unpack('>H', t[8 + 2 * voice:10 + 2 * voice])[0]
    for l in range(t[v + 2] + 1):
        L = t[v + 4 + 0x20 * l:v + 4 + 0x20 * (l + 1)]
        if L[0] <= note <= L[1]: return L
    raise KeyError((name, voice, note))

# ----------------------------------------------------------------- Sega SEQ -> Sony SEQ
# Sega SEQ song (big-endian): u16 resolution, u16 tempo-event count, u16 data offset, u16 tempo loop offset (byte offset of
# the tempo event the loop returns to), then tempo events (u32 steps, u32 microseconds per beat), then the event stream.
# Every event ends with a step byte = the time SINCE THE PREVIOUS EVENT (it is added before the event takes effect):
#   00-7F  note: ch = low nibble; key, velocity, gate, step. Bit 0x40 = gate + 0x100 (*0x20 = gate + 0x200, hypothesis*;
#          bit 0x10 and the 0x88-0x8F gate/step extensions are not used by WOOD2 and not handled).
#   Bn c v step (control), Cn p step (program = TON voice), Dn v step (pressure), En v step (pitch bend, 7-bit, 64 = centre)
#   81 u16 off, u8 n   play n events at data offset `off` (a reference; not nested)
#   82 step            no-op with a step (the songs put one at the loop point and one at the end)
#   83                 end of track
# Checked on all 72 Saturn songs 0: the step sum equals the tempo-track length on every song that uses no extension codes,
# and the decoded BS08C theme equals Capcom's PS1 SEQ (PBGM2A) note for note on its kept channels (Capcom moved it 10
# steps earlier, merged/dropped doubling channels and raised some bass voices an octave).

def sega_seq_decode(s):
    """-> dict(res, tempos [(steps, usec)], loop_t, end_t, ev [(t, kind, ch, a, b, c)]); kind note (key, vel, gate),
    cc (ctl, val), prog (p), press (v), bend (v), mark (step byte of an 82 event).
    Note status 0x00-0x7F (as the Saturn sound driver reads it): channel = bits 0-4 (16-31 not handled), bit 0x40 =
    gate + 0x100, bit 0x20 = step + 0x100. One-byte prefixes 0x88-0x8B add 0x200 / 0x800 / 0x1000 / 0x2000 to the
    next note's gate, 0x8C-0x8F add 0x100 / 0x200 / 0x800 / 0x1000 to the step (they accumulate)."""
    res, nt, do, lo = struct.unpack('>4H', s[:8])
    tempos = [struct.unpack('>II', s[8 + 8 * k:16 + 8 * k]) for k in range(nt)]
    ev, T, G = [], [0], [0]
    def run(i, lim=None, depth=0):
        n = 0
        while lim is None or n < lim:
            b = s[i]
            if b < 0x80:
                assert not b & 0x10, 'channels 16-31 not handled'
                key, vel, gate, dt = s[i + 1:i + 5]
                gate += (0x100 if b & 0x40 else 0) + G[0]; G[0] = 0
                dt += 0x100 if b & 0x20 else 0
                T[0] += dt; ev.append((T[0], 'note', b & 15, key, vel, gate)); i += 5
            elif 0x88 <= b <= 0x8F:                       # gate / step prefix of the next note
                x = (0x200, 0x800, 0x1000, 0x2000, 0x100, 0x200, 0x800, 0x1000)[b - 0x88]
                if b < 0x8C: G[0] += x
                else: T[0] += x
                i += 1; continue
            elif b >> 4 in (0xA, 0xB):
                assert b >> 4 == 0xB, 'polyphonic pressure not handled'
                T[0] += s[i + 3]; ev.append((T[0], 'cc', b & 15, s[i + 1], s[i + 2], 0)); i += 4
            elif b >> 4 in (0xC, 0xD, 0xE):
                T[0] += s[i + 2]; ev.append((T[0], {0xC: 'prog', 0xD: 'press', 0xE: 'bend'}[b >> 4], b & 15, s[i + 1], 0, 0)); i += 3
            elif b == 0x81:
                assert depth == 0
                run(do + struct.unpack('>H', s[i + 1:i + 3])[0], s[i + 3], 1); i += 4
            elif b == 0x82:
                T[0] += s[i + 1]; ev.append((T[0], 'mark', 0, s[i + 1], 0, 0)); i += 2
            elif b == 0x83:
                return i
            else:
                raise NotImplementedError(f'Sega SEQ event {b:#x} at {i:#x}')
            n += 1
        return i
    run(do)
    li = (lo - 8) // 8
    loop_t = sum(d for d, u in tempos[:li]); end_t = sum(d for d, u in tempos)
    return dict(res=res, tempos=tempos, loop_t=loop_t, end_t=end_t, ev=ev, last_t=T[0])

def _vlq(v):
    out = [v & 0x7F]; v >>= 7
    while v: out.append(0x80 | (v & 0x7F)); v >>= 7
    return bytes(out[::-1])

def sega_to_sony_seq(s, prog_map=None, drop_cc=(0x20,), vel_map=None, cc7_map=None, key_map=None, tempo_scale=1.0,
                     lfo=None, lfo_level=None, res_scale=1, gate_frames=None, time_scale=1.0):
    """Convert one Sega SEQ song to a Sony SEQ (libsnd): same resolution, times and channels; notes become note-on /
    note-on velocity 0 after the gate (clipped at the loop end); program numbers through prog_map (default: same);
    pitch bend 7-bit -> MSB; bank select (CC 0x20) dropped; velocity / CC 7 through vel_map / cc7_map when given
    (build_music_bank passes the measured Saturn laws); tempo changes as FF 51; loop = CC 99 = 20 + CC 6 = 127 at the
    loop point and CC 99 = 30 at the end (Capcom's markers).
    lfo = {Saturn program: scsp_lfo(...)} imitates the SCSP's LFO on channels playing those programs (the SPU has none):
    pitch bend (in steps of the tone's bend range / 64) and CC 7 (lfo_level(Saturn CC 7, LFO dB) replaces cc7_map
    there) on a grid of ~1/32 LFO period, emitted only when the value changes, while the channel's notes sound (+ 0.25 s;
    gaps under 1 s bridged) and again at the loop point. The LFO restarts at every note-on on the channel (measured on
    the Saturn: the vibrato starts at the same point on every note).
    gate_frames = video frames per second: every note's length becomes a whole number of frames (at least one), so that
    libsnd, which acts once per frame, plays each note the same length (otherwise a short note loses up to a frame).
    res_scale: the Sony SEQ's resolution and every time are multiplied by it (libsnd rounds its playback rate in coarse
    steps; a finer resolution brings a song's tempo closer). time_scale stretches every event time by that factor at
    the finer resolution (an exact tempo correction where libsnd's own tempo steps miss; libsnd keeps a whole number of
    beats per minute). Returns (bytes, info)."""
    A = sega_seq_decode(s)
    loop_t, end_t = A['loop_t'], A['end_t']
    assert A['last_t'] == end_t, ('song length != tempo track', A['last_t'], end_t)
    marks = [e[0] for e in A['ev'] if e[1] == 'mark']
    looped = len(A['tempos']) > 1 or loop_t > 0
    out = []                                  # (t, order, bytes-without-status..., status)
    def add(t, order, st, data): out.append((t, order, len(out), st, bytes(data)))
    # tempo changes (the header carries the first)
    A['tempos'] = [(d, round(u * tempo_scale)) for d, u in A['tempos']]     # tempo_scale: measured playback-rate match
    tt = 0; cur = A['tempos'][0][1]
    for d, u in A['tempos']:
        if u != cur: add(tt, 0, 0xFF, b'\x51\x03' + u.to_bytes(3, 'big')); cur = u
        tt += d
    if looped:
        if len(set(u for d, u in A['tempos'])) > 1:   # tempo at the loop point must be restored on every pass
            li = next(k for k in range(len(A['tempos'])) if sum(d for d, u in A['tempos'][:k]) == loop_t)
            add(loop_t, 0, 0xFF, b'\x51\x03' + A['tempos'][li][1].to_bytes(3, 'big'))
        add(loop_t, 1, 0xB0, (99, 20)); add(loop_t, 2, 0xB0, (6, 127))
        add(end_t, 9, 0xB0, (99, 30))
    nnotes = clipped = 0; cprog = {}
    lfo_ch = set()
    if lfo:
        step_at = []                                    # (first step, seconds per step) per tempo segment
        tt = 0
        for d, u in A['tempos']: step_at.append((tt, u / 1e6 / A['res'])); tt += d
        def sec(x):
            r = 0.0
            for k, (t0, sp) in enumerate(step_at):
                t1 = step_at[k + 1][0] if k + 1 < len(step_at) else None
                if t1 is None or x < t1: return r + (x - t0) * sp
                r += (t1 - t0) * sp
            return r
        sp0 = min(sp for t0, sp in step_at)
        span = collections.defaultdict(list); cc7 = collections.defaultdict(list); pp = {}; ons = collections.defaultdict(list)
        for t, kind, ch, a, b, c in A['ev']:
            if kind == 'prog': pp[ch] = a
            elif kind == 'cc' and a == 7: cc7[ch].append((t, b))
            elif kind == 'note' and c and pp.get(ch) in lfo:
                span[ch].append((t, min(end_t, t + c + round(0.25 / sp0)), pp[ch])); ons[ch].append(t)
        for ch, sp in span.items():
            lfo_ch.add(ch)
            merged = []
            for a0, b0, pr in sorted(sp):
                if merged and a0 - merged[-1][1] < 1.0 / sp0 and merged[-1][2] == pr:
                    merged[-1][1] = max(merged[-1][1], b0)
                else: merged.append([a0, b0, pr])
            def c7_at(x):
                v = 127
                for t, b in cc7[ch]:
                    if t <= x: v = b
                return v
            for t, b in cc7[ch]:                         # the song's own CC 7 outside the LFO spans
                if not any(a0 <= t <= b0 for a0, b0, pr in merged): add(t, 5, 0xB0 | ch, (7, lfo_level(b, 0.0)))
            for a0, b0, pr in merged:
                L = lfo[pr]; g = max(1, round(1.0 / (32 * L['hz']) / sp0))
                on_ch = sorted(ons[ch]); on_set = set(on_ch)
                pts = set(range(a0, b0 + 1, g)) | {t for t, b in cc7[ch] if a0 <= t <= b0} | {t for t in on_ch if a0 <= t <= b0}
                if a0 <= loop_t <= b0: pts.add(loop_t)
                last = None
                import bisect
                for x in sorted(pts):
                    t_on = on_ch[max(0, bisect.bisect_right(on_ch, x) - 1)]   # the LFO restarts at each note-on
                    i = int(256 * (((sec(x) - sec(t_on)) * L['hz']) % 1.0))
                    p = i if i < 128 else i - 256                          # MAME's saw waveforms
                    bend = max(0, min(127, 64 + round(L['cents'] * p / 128 / (L['pb'] * 100 / 64))))
                    lev = lfo_level(c7_at(x), -L['db'] * (255 - i) / 256)
                    if x == a0 or x == loop_t or x in on_set: last = None
                    if L['cents'] and (last is None or bend != last[0]): add(x, 5, 0xE0 | ch, (0, bend))
                    if L['db'] and (last is None or lev != last[1]): add(x, 5, 0xB0 | ch, (7, lev))
                    last = (bend, lev)
    for t, kind, ch, a, b, c in A['ev']:
        if kind == 'note':
            off = t + c
            if gate_frames and c:
                fr = 1.0 / gate_frames / (A['tempos'][0][1] / 1e6 / A['res'])          # one frame in steps
                off = t + max(1, round(c / fr)) * fr
            if looped and off > end_t: off = end_t; clipped += 1
            if c == 0: continue
            if vel_map: b = vel_map(cprog.get(ch, 0), b)
            if key_map: a = key_map(cprog.get(ch, 0), a)
            add(t, 6, 0x90 | ch, (a, b)); add(off, 4 if off > t else 7, 0x90 | ch, (a, 0)); nnotes += 1
        elif kind == 'cc':
            if a in drop_cc: continue
            if a == 7 and ch in lfo_ch: continue        # written with the LFO above
            if a == 7 and cc7_map: b = cc7_map(b)
            add(t, 5, 0xB0 | ch, (a, b))
        elif kind == 'prog':
            cprog[ch] = a
            add(t, 3, 0xC0 | ch, ((prog_map or {}).get(a, a),))
        elif kind == 'press':
            add(t, 5, 0xD0 | ch, (a,))
        elif kind == 'bend':
            add(t, 5, 0xE0 | ch, (0, a))
    out.sort(key=lambda e: (e[0], e[1], e[2]))
    u0 = A['tempos'][0][1]
    body = bytearray(b'pQES' + struct.pack('>IH', 1, A['res'] * res_scale) + u0.to_bytes(3, 'big') + b'\x04\x02')
    t, rs = 0, None
    for et, order, _, st, data in out:
        body += _vlq(round(et * res_scale * time_scale) - round(t * res_scale * time_scale)); t = et
        if st == 0xFF: body += b'\xff' + data; rs = None
        else:
            if st != rs: body.append(st); rs = st
            body += data
    body += _vlq(max(0, round(end_t * res_scale * time_scale) - round(t * res_scale * time_scale))) + b'\xff\x2f\x00'
    info = dict(res=A['res'], tempo=u0, loop_t=loop_t, end_t=end_t, marks=marks, notes=nnotes, clipped=clipped,
                channels=sorted({e[2] for e in A['ev'] if e[1] == 'note'}), size=len(body), lfo_channels=sorted(lfo_ch))
    return bytes(body), info

# ----------------------------------------------------------------- Saturn TON -> PS1 VAB
# TON layer (0x20 bytes, BE; SCSP slot registers from byte 2): 0 lo key, 1 hi key, 2-3 [SSCTL 7-8 | LPCTL 5-6 | PCM8B 4 |
# SA 19:16], 4-5 SA, 6-7 LSA, 8-9 LEA (samples), 0x0A-0B [D2R 15-11 | D1R 10-6 | EGHOLD 5 | AR 4-0], 0x0C-0D
# [LPSLNK 14 | KRS 13-10 | DL 9-5 | RR 4-0], 0x0E-0F [STWINH | SDIR | TL 7-0], 0x10-0x17 modulation/LFO/DSP input (all 0 in
# WOOD2), 0x18 DISDL << 5 | DIPAN, 0x19 base note B (at key B the sample plays at 44.1 kHz), 0x1A fine tune (signed,
# *hypothesis: 1/256 semitone*; Capcom's conversions of fine 0x7E samples agree within 5 cents). Voice header (4 bytes):
# byte 0 = pitch-bend range in semitones (BS08C voice 1 = 12 = Capcom's pbmin/pbmax), byte 2 = layers - 1.
SCSP_DR_MS = (1e9, 1e9, 118200.0, 101300.0, 88600.0, 70900.0, 59100.0, 50700.0, 44300.0, 35500.0, 29600.0, 25300.0,
              22200.0, 17700.0, 14800.0, 12700.0, 11100.0, 8900.0, 7400.0, 6300.0, 5500.0, 4400.0, 3700.0, 3200.0, 2800.0,
              2200.0, 1800.0, 1600.0, 1400.0, 1100.0, 920.0, 790.0, 690.0, 550.0, 460.0, 390.0, 340.0, 270.0, 230.0,
              200.0, 170.0, 140.0, 110.0, 98.0, 85.0, 68.0, 57.0, 49.0, 43.0, 34.0, 28.0, 25.0, 22.0, 18.0, 14.0, 12.0,
              11.0, 8.5, 7.1, 6.1, 5.4, 4.3, 3.6, 3.1)      # ms for the full 96 dB envelope range (MAME scsp.cpp)

SCSP_AR_MS = (1e9, 1e9, 8100.0, 6900.0, 6000.0, 4800.0, 4000.0, 3400.0, 3000.0, 2400.0, 2000.0, 1700.0, 1500.0, 1200.0,
              1000.0, 860.0, 760.0, 600.0, 500.0, 430.0, 380.0, 300.0, 250.0, 220.0, 190.0, 150.0, 130.0, 110.0, 95.0, 76.0,
              63.0, 55.0, 47.0, 38.0, 31.0, 27.0, 24.0, 19.0, 15.0, 13.0, 12.0, 9.4, 7.9, 6.8, 6.0, 4.7, 3.8, 3.4, 3.0, 2.4,
              2.0, 1.8, 1.6, 1.3, 1.1, 0.93, 0.85, 0.65, 0.53, 0.44, 0.40, 0.35, 0.0, 0.0)  # attack ms (MAME scsp.cpp)
# SCSP LFO (slot register 0x12 = TON layer bytes 0x14-0x15: LFORE 15, LFOF 14-10, PLFOWS 9-8, PLFOS 7-5, ALFOWS 4-3,
# ALFOS 2-0): frequency (Hz), pitch depth (cents) and amplitude depth (dB) per setting (MAME scsp.cpp / scsplfo.hxx).
SCSP_LFO_HZ = (0.17, 0.19, 0.23, 0.27, 0.34, 0.39, 0.45, 0.55, 0.68, 0.78, 0.92, 1.10, 1.39, 1.60, 1.87, 2.27, 2.73,
               3.13, 3.65, 4.43, 5.46, 6.28, 7.33, 8.89, 10.97, 12.60, 14.69, 17.84, 21.82, 25.15, 29.40, 35.17)
SCSP_PLFO_CENTS = tuple(round(c * 0.437, 2) for c in (0.0, 7.0, 13.5, 27.0, 55.0, 112.0, 230.0, 494.0))
# (MAME's pitch depths scaled to the Saturn as measured in Mednafen: PLFOS 2 swings 11.2-12.6 cents peak to peak, not
# MAME's +-13.5; the other settings are scaled the same way, *hypothesis*)
SCSP_ALFO_DB = (0.0, 0.4, 0.8, 1.5, 3.0, 6.0, 12.0, 24.0)
assert len(SCSP_AR_MS) == 64 and len(SCSP_LFO_HZ) == 32

def scsp_lfo(w, pb):
    """SCSP LFO register word + the voice's pitch-bend range (semitones) -> dict for sega_to_sony_seq's lfo (sawtooth
    waveforms only, the only ones the US banks use with a depth)."""
    pws, ps, aws, as_ = (w >> 8) & 3, (w >> 5) & 7, (w >> 3) & 3, w & 7
    assert (pws == 0 or ps == 0) and (aws == 0 or as_ == 0) and not w & 0x8000, ('LFO waveform / reset not handled', hex(w))
    return dict(hz=SCSP_LFO_HZ[(w >> 10) & 31], cents=SCSP_PLFO_CENTS[ps], db=SCSP_ALFO_DB[as_], pb=pb)

def ton_layer(L):
    """Decode a raw 0x20-byte TON layer."""
    w = lambda o: struct.unpack('>H', L[o:o + 2])[0]
    f0, eg1, eg2, tl = w(2), w(0x0A), w(0x0C), w(0x0E)
    return dict(lo=L[0], hi=L[1], ssctl=(f0 >> 7) & 3, lpctl=(f0 >> 5) & 3, pcm8=bool(f0 & 0x10),
                sa=((f0 & 0xF) << 16) | w(4), lsa=w(6), lea=w(8),
                d2r=eg1 >> 11, d1r=(eg1 >> 6) & 31, eghold=(eg1 >> 5) & 1, ar=eg1 & 31,
                lpslnk=(eg2 >> 14) & 1, krs=(eg2 >> 10) & 15, dl=(eg2 >> 5) & 31, rr=eg2 & 31,
                tl=tl & 0xFF, mod=L[0x10:0x18], disdl=L[0x18] >> 5, dipan=L[0x18] & 31, base=L[0x19],
                fine=L[0x1A] - 256 if L[0x1A] > 127 else L[0x1A], extra=L[0x1B:0x20])

def ton_voices_raw(name):
    """[(voice header 4 bytes, [raw layers])] of a Saturn bank's TON."""
    t = sat_bank_chunks(name)[1]
    o = struct.unpack('>H', t[0:2])[0]
    hdr = [struct.unpack('>H', t[i:i + 2])[0] for i in range(0, o, 2)]
    return t, [(t[v:v + 4], [t[v + 4 + 0x20 * l:v + 4 + 0x20 * (l + 1)] for l in range(t[v + 2] + 1)]) for v in hdr[4:]]

def ton_pcm(t, lay):
    """Layer sample (up to LEA) as a list of 16-bit values (8-bit samples are signed, << 8)."""
    n = lay['lea']
    if lay['pcm8']: return [(x - 256 if x > 127 else x) << 8 for x in t[lay['sa']:lay['sa'] + n]]
    return list(struct.unpack('>%dh' % n, t[lay['sa']:lay['sa'] + 2 * n]))

def scsp_rate_db_per_sample(R, base):
    """SCSP decay/release rate register R (0-31) at key-scaling base -> dB per 44.1 kHz sample (MAME's table)."""
    if R == 0: return 0.0
    r = max(0, min(63, base + 2 * R))
    return -96.0 / (SCSP_DR_MS[r] * 44.1)

def spu_rate_db_per_sample(shift, step):
    """PS1 SPU exponential decrease: level -= (step << max(0, 11 - shift)) * level / 0x8000 every 1 << max(0, shift - 11)
    samples (psx-spx). -> dB per sample."""
    import math
    p = (step * (1 << max(0, 11 - shift)) / 32768.0) / (1 << max(0, shift - 11))
    return 20 * math.log10(max(1e-9, 1 - p)) if p < 1 else -200.0

def _best(cands, want):
    import math
    if want == 0: return max(cands, key=lambda c: (c[1] == 0, -abs(c[1])))[0]
    return min(cands, key=lambda c: abs(math.log(max(1e-12, -c[1]) / -want)) if c[1] < 0 else 1e9)[0]

SPU_DR = [(d, spu_rate_db_per_sample(d, 8)) for d in range(16)]
SPU_SR = [(r, spu_rate_db_per_sample(r >> 2, 8 - (r & 3))) for r in range(127)]
SPU_RR = [(r, spu_rate_db_per_sample(r, 8)) for r in range(32)]
# Sustain-decrease rates measured in Mednafen (a 112-sample sine tone held 2.1 s per rate), dB per second.
# The model above is 7-24 % off (and the steps between shift groups are uneven), so these replace it where measured.
SPU_SR_MEASURED_DBPS = {52: -41.6, 53: -32.4, 54: -31.5, 55: -27.0, 56: -14.46, 57: -12.60, 58: -10.84, 59: -9.05,
                        60: -6.51, 61: -5.73, 62: -4.94, 63: -4.16, 64: -3.16, 65: -2.78, 66: -2.43, 67: -2.04,
                        68: -1.57, 69: -1.38, 70: -1.21, 71: -1.01, 72: -0.78}
SPU_SR_BEST = [(r, SPU_SR_MEASURED_DBPS[r] / 44100 if r in SPU_SR_MEASURED_DBPS else d) for r, d in SPU_SR]

def scsp_to_spu_adsr(lay, octave):
    """SCSP EG (AR, D1R, DL, D2R, RR, key scaling at `octave`) -> PS1 (adsr1, adsr2). Attack: AR 0x1F = instant (all
    WOOD2 / CUT2 layers); a slower AR -> the SPU linear attack rate whose rise time is nearest the SCSP's (MAME's
    table). Decay 1 -> SPU decay at the same dB rate down to Sl (DL in 3 dB steps; the SPU's lowest sustain level is
    -24 dB, so a deeper DL continues at the D1R slope in the sustain phase instead of D2R); D2R -> sustain decrease;
    RR -> release (SPU exponential)."""
    import math
    assert lay['ar'] >= 1 and not lay['eghold'] and lay['krs'] != 15
    base = octave + 2 * lay['krs']
    Ar = 0
    if lay['ar'] < 0x1F:
        ms = SCSP_AR_MS[max(0, min(63, base + 2 * lay['ar']))]
        if ms > 0:
            n = ms * 44.1                                # samples to full level
            rise = lambda r: 32767 / ((7 - (r & 3)) << max(0, 11 - (r >> 2))) * (1 << max(0, (r >> 2) - 11))
            Ar = min(range(128), key=lambda r: abs(math.log(rise(r) / n)))
    d1 = scsp_rate_db_per_sample(lay['d1r'], base)
    d2 = scsp_rate_db_per_sample(lay['d2r'], base)
    rr = scsp_rate_db_per_sample(lay['rr'], base)
    if lay['dl'] == 0 or lay['d1r'] == 0:          # never leaves decay 1 (or no decay at all): hold, then D2R
        Dr, Sl = 15, 15        # Dr 15, not 0: the SPU applies one decay step before testing Sl (Dr 0 = -6 dB at once)
        sus = d2 if lay['dl'] == 0 else 0.0
    else:
        Dr = _best(SPU_DR, d1)
        a = 10 ** (-3 * lay['dl'] / 20)
        Sl = max(0, min(15, round(a * 16) - 1))
        sus = d1 if (Sl + 1) / 16 > a else d2       # SPU can't go as deep as DL: keep the D1R slope below -24 dB
    Sr = 0x7F if sus == 0 else _best(SPU_SR, sus)
    Rr = _best(SPU_RR, rr) if rr < 0 else 31
    adsr1 = 0x0000 | (Ar << 8) | (Dr << 4) | Sl          # linear attack (rate 0 = instant)
    adsr2 = 0x8000 | 0x4000 | (Sr << 6) | 0x20 | Rr       # exponential sustain decrease, exponential release
    return adsr1, adsr2

def resample_loop(pcm, lsa, lea, new_len, taps=24):
    """Windowed-sinc resample so the loop [lsa, lea) becomes exactly new_len samples (loop treated as periodic, so the
    seam stays seamless). Returns (out, out_loop_start) with out[out_loop_start:] = the loop."""
    import numpy as np
    x = np.asarray(pcm[:lea], float); L = lea - lsa; q = new_len / L
    n_ls = int(np.ceil(lsa * q))
    n = np.arange(n_ls + new_len)
    tau = lsa + (n - n_ls) / q                          # input time of each output sample
    k0 = np.floor(tau).astype(int)
    out = np.zeros(len(n))
    fc = min(1.0, q)                                    # anti-alias when shrinking
    for j in range(-taps + 1, taps + 1):
        idx = k0 + j
        d = tau - idx
        w = fc * np.sinc(fc * d) * (0.5 + 0.5 * np.cos(np.pi * d / taps))
        src = np.where(idx >= lea, lsa + (idx - lsa) % L, idx)
        val = np.where((src >= 0) & (src < lea), x[np.clip(src, 0, lea - 1)], 0.0)
        out += w * val
    return [int(max(-32768, min(32767, round(v)))) for v in out], n_ls

def _vag_block_py(x, s1, s2, filters=range(5)):
    """Best (err, filter, shift, nibbles, s1, s2) for one 28-sample block: the first (filter, shift) in search order
    with the smallest squared error (reference implementation; _vag_block uses the C version when it can)."""
    best = None
    for f in filters:
        f0, f1 = VAG_FILTERS[f]
        for sh in range(13):
            a1, a2, err, nib = s1, s2, 0, []
            for v in x:
                p = (a1 * f0 + a2 * f1 + 32) >> 6
                q = (v - p) * (1 << sh)
                n = (q + 2048) >> 12 if q >= 0 else -((-q + 2047) >> 12)
                n = max(-8, min(7, n))
                d = max(-32768, min(32767, ((n << 12) >> sh) + p))
                err += (v - d) ** 2
                if best is not None and err >= best[0]: break
                nib.append(n); a2, a1 = a1, d
            else:
                best = (err, f, sh, nib, a1, a2)
    return best

_VAG_C = r"""
typedef long long i64;
static const int F0[5] = {0, 60, 115, 98, 122}, F1[5] = {0, 0, -52, -55, -60};
/* same search as _vag_block_py: first (f, sh) with the smallest error, early exit once err >= best */
int vag_block(const int *x, int s1, int s2, int nf, i64 *err_out, int *res)
{
    i64 best = -1; int bf = 0, bsh = 0, b1 = 0, b2 = 0;
    for (int f = 0; f < nf; f++) for (int sh = 0; sh < 13; sh++) {
        int a1 = s1, a2 = s2, k; i64 err = 0;
        for (k = 0; k < 28; k++) {
            int p = (a1 * F0[f] + a2 * F1[f] + 32) >> 6;
            i64 q = (i64)(x[k] - p) * (1 << sh);
            int n = q >= 0 ? (int)((q + 2048) >> 12) : -(int)((-q + 2047) >> 12);
            if (n > 7) n = 7; if (n < -8) n = -8;
            int d = ((n * 4096) >> sh) + p;
            if (d > 32767) d = 32767; if (d < -32768) d = -32768;
            i64 e = x[k] - d; err += e * e;
            if (best >= 0 && err >= best) break;
            res[4 + k] = n; a2 = a1; a1 = d;
        }
        if (k == 28) { best = err; bf = f; bsh = sh; b1 = a1; b2 = a2;
                       for (int j = 0; j < 28; j++) res[32 + j] = res[4 + j]; }
    }
    *err_out = best; res[0] = bf; res[1] = bsh; res[2] = b1; res[3] = b2;
    return 0;
}
"""
_vag_lib = None

def _vag_c():
    """ctypes handle of the C block search, compiled once with the host compiler into ~/.cache/mm8tool (None if no
    compiler: the Python version is used; both give the same bytes, `verify` checks it)."""
    global _vag_lib
    if _vag_lib is None:
        _vag_lib = False
        try:
            import ctypes, subprocess, shutil
            cc = shutil.which('gcc') or shutil.which('cc')
            if cc:
                d = os.path.join(os.path.expanduser('~'), '.cache', 'mm8tool'); os.makedirs(d, exist_ok=True)
                so = os.path.join(d, 'vag_%s.so' % hashlib.md5(_VAG_C.encode()).hexdigest()[:10])
                if not os.path.exists(so):
                    c = so[:-3] + '.c'; open(c, 'w').write(_VAG_C)
                    subprocess.run([cc, '-O2', '-shared', '-fPIC', '-fwrapv', c, '-o', so], check=True, capture_output=True)
                lib = ctypes.CDLL(so)
                lib.vag_block.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                          ctypes.POINTER(ctypes.c_longlong), ctypes.POINTER(ctypes.c_int)]
                _vag_lib = (lib, ctypes)
        except Exception:
            _vag_lib = False
    return _vag_lib or None

def _vag_block(x, s1, s2, filters=range(5)):
    """Best (err, filter, shift, nibbles, s1, s2) for one 28-sample block (the search vag_encode uses)."""
    c = _vag_c()
    if c is None or filters != range(5) or len(x) != 28: return _vag_block_py(x, s1, s2, filters)
    lib, ct = c
    xa = (ct.c_int * 28)(*x); err = ct.c_longlong(); res = (ct.c_int * 60)()
    lib.vag_block(xa, s1, s2, 5, ct.byref(err), res)
    return (err.value, res[0], res[1], list(res[32:60]), res[2], res[3])

def vag_encode_loop(pcm, loop_start):
    """Looping PS1 ADPCM in Capcom's layout: zero block (flag 0), data blocks flagged 2, the loop-start block 6 (encoded
    with filter 0 so the seam doesn't depend on predictor history), the last block 3 (= jump to the loop start).
    len(pcm) and loop_start must be multiples of 28."""
    assert len(pcm) % 28 == 0 and loop_start % 28 == 0 and loop_start < len(pcm)
    nblk = len(pcm) // 28; lb = loop_start // 28
    # Calibration: forcing filter 0 on the loop-start block put audible noise into short loops (a 112-sample
    # sine loop measured 40 dB more energy above 4 kHz than the Saturn). Now the loop body is encoded with the predictor
    # history it really has on every repeat (the end of the loop; iterated until stable); only the first pass into the
    # loop (history = the pre-loop samples) can differ slightly.
    def blocks(b0, b1, s1, s2):
        out = bytearray()
        for b in range(b0, b1):
            err, f, sh, nib, s1, s2 = _vag_block(pcm[28 * b:28 * b + 28], s1, s2)
            flag = 6 if b == lb else (3 if b == nblk - 1 else 2)
            if b == lb == nblk - 1: flag = 7
            out += bytes([(f << 4) | sh, flag]) + bytes(((nib[2 * k] & 15) | ((nib[2 * k + 1] & 15) << 4)) for k in range(14))
        return bytes(out), (s1, s2)
    pre = bytes(16) + blocks(0, lb, 0, 0)[0]          # the blocks before the loop don't depend on the loop history
    def enc(h_loop):
        body, h = blocks(lb, nblk, *h_loop)
        return pre + body, h
    h = (0, 0)
    for _ in range(6):
        out, h2 = enc(h)
        if h2 == h: break
        h = h2
    return out

def _center_shift(key_at_44k):
    """VAB centre note + fine shift (1/128 semitone, raises pitch) so that key `key_at_44k` (may be fractional) plays the
    sample at 44.1 kHz."""
    import math
    c = math.ceil(key_at_44k - 1e-9)
    sh = round((c - key_at_44k) * 128)
    if sh == 128: c, sh = c - 1, 0       # a full semitone of shift = the note below, unshifted
    return c, sh

def scsp_pan_db(dipan):
    """SCSP DIPAN -> (left dB, right dB). Measured in Mednafen (calibration): bits 0-3 = n x 3 dB on one side
    (15 = off); bit 4 = 0 attenuates the LEFT side (0xA2 = right louder by 6 dB), bit 4 = 1 the right side."""
    n = dipan & 15
    a = -200.0 if n == 15 else -3.0 * n
    return (a, 0.0) if not dipan & 0x10 else (0.0, a)

def scsp_pan(dipan):
    """SCSP DIPAN -> libsnd pan 0-127. libsnd (measured): pan p < 64 scales the right side by (p/64)^2,
    p > 64 the left side by ((127-p)/63)^2."""
    l, r = scsp_pan_db(dipan)
    if l == r == 0: return 64
    if l < 0: return min(127, 127 - round(63 * 10 ** (l / 40)))       # left quieter -> towards the right
    return max(0, round(64 * 10 ** (r / 40)))

# Saturn driver laws measured with the emulator calibration (Mednafen; general, not per bank): velocity -> dB
# (all voices, drums included; the curve has a step near 93) and CC 7 = -0.735 dB per step below 127. libsnd (PS1)
# scales velocity, CC 7, tone and program volume as amplitude^2 (40 log10, _libsnd_level).
SAT_VEL_DB = [(0, -90.0), (40, -48.6), (50, -41.2), (64, -24.1), (72, -18.0), (80, -12.0), (86, -7.2),
              (90, -4.3), (100, -6.0), (110, -1.9), (115, -0.6), (120, 0.0), (127, 0.0)]
SAT_CC7_DB_PER_STEP = 0.735
# Per-bank tuning from `calibrate`: gain_db / decay_dbps per voice (or (voice, key) for drum keys), upsampling factors.
# A bank without an entry gets the general laws above and no per-voice corrections.
MUSIC_TUNE = {
    'WOOD2.PAC': dict(
        gain_db={0: -0.8, 1: -3.7, 2: -2.7, 3: 0.6, 4: -0.9, 5: -0.8, 6: 0.0, 7: -1.1,
                 (8, 45): -1.3, (8, 67): 1.7, (8, 51): -0.3, (8, 75): 0.3, (8, 78): 0.2, (8, 77): 1.4},
        decay_dbps={3: -9.2, 4: -2.93, 5: -2.93, 6: -8.19, 7: -8.19},  # envelope slope to set (sample's own change removed)
        upsample={0: 2, 1: 4, 2: 4, 3: 4, 4: 0.5, 5: 0.5, 6: 2, 7: 2, 8: 2},   # voice 0 = the one-shot sounds;
        # 4/5 at half rate: keys 80-88 asked the SPU for 4.2-6.7x and were capped at 4x (up to 9 semitones flat)
        shelf_db={6: (9000, 3.0), 7: (9000, 3.0)},   # treble the SPU's interpolation dulls (calibration band shares)
        seq_res_scale=10, time_scale=1.008,   # tempo: the Saturn's exact speed (libsnd alone played it 0.35 % fast)
        key_split={1: [(0, 52, {'upsample': 8}), (53, 127, {})],      # low bass notes: the Saturn's images need 8x
                   3: [(0, 77, {}), (78, 127, {'gain_db': -1.8})]},      # its top notes 1.8 dB loud
        gate_frames=59.94, level_db=5.6),     # whole-frame note lengths; +3.5 dB over the Saturn, like Capcom's
                                              # own conversions (PS1 PBGM2A = Saturn BS08C + 3.6 dB)
    'CUT2.PAC': dict(       # calibration rounds in Mednafen (02 §Sound "PBGM49")
        gain_db={0: -0.4, 1: 1.1, 2: -0.7, 3: 3.8, 4: 1.0, 5: -0.3, 6: -0.1, 7: 4.0, 8: 4.7, 9: 1.5, 10: 1.5,
                 (11, 47): 0.3, (11, 51): 1.7, (11, 77): 4.4, (11, 78): 6.5},
        decay_dbps={2: -12.2, 3: -16.2, 4: -16.2, 5: -24.25, 6: -24.25, 7: -8.1, 8: -8.1, 9: -64.8, 10: -64.8},
        decay_rate={9: 49, 10: 49},   # chosen by test (rates 47-50): the SPU's fast rates aren't in the measured table
        upsample={0: 2, 1: 4, 2: 4, 3: 0.5, 4: 0.5, 5: 2, 6: 2, 7: 2, 8: 2, 9: 4, 10: 4, 11: 4},   # 0.5: stored at half rate (SPU 4x pitch cap)
        seq_res_scale=10, time_scale=1.002,   # tempo: the Saturn's exact speed (libsnd's 150 BPM step played it 0.2 % fast)
        key_split={1: [(0, 48, {}), (49, 127, {'upsample': 2})],      # bass: 4x only where its low notes need it
                   3: [(0, 71, {'decay_dbps': -11.6}), (72, 127, {'decay_dbps': -17.4})],   # the Saturn's decay is
                   4: [(0, 71, {'decay_dbps': -14.8}), (72, 127, {'decay_dbps': -23.0})],   # faster from key 72 up
                   7: [(0, 61, {'upsample': 4}), (62, 127, {})], 8: [(0, 61, {'upsample': 4}), (62, 127, {})]},
        gate_frames=59.94, level_db=6.5),     # whole-frame note lengths; +4.1 dB over the Saturn (see WOOD2)
    'ST03A.PAC': dict(      # Tengu Man stage (Saturn); 3 calibration rounds in Mednafen (reference 02b, ST03A)
        gain_db={1: 3.8, 2: 5.6, 3: -0.4, 4: -0.4, 5: -6.0, 6: -5.1, 7: 0.2, 8: -0.4, 9: 0.0, 10: 0.0, 11: 0.7, 12: 1.3,
                 13: -6.6, 14: -6.9, 15: -6.6, (16, 45): -0.6, (16, 67): -2.4, (16, 77): -4.9, (16, 79): 1.1,
                 (16, 86): -0.5, (16, 87): -0.7, (16, 88): -0.4},
        decay_dbps={2: -4.1, 3: -4.1, 4: -4.1, 5: -1.9, 6: -25.1, 7: -17.5, 8: -17.5, 9: -12.4, 10: -12.4, 11: -1.8,
                    12: -1.8, 13: -97.0, 14: -97.0, 15: -97.0},
        upsample={1: 2, 2: 2, 3: 2, 4: 2, 5: 2, 6: 2, 11: 2, 12: 2, 16: 2},   # within the SPU room (reference 02b)
        shelf_db={2: (8000, 2.5), 3: (8000, 2.5), 4: (8000, 2.5), 5: (10000, 6.0), 6: (9000, 4.0), 7: (9000, 5.0),
                  8: (9000, 5.0), 9: (10000, 6.0), 10: (10000, 6.0), 11: (6000, 1.0), 12: (6000, 1.0), 13: (8000, 2.0),
                  14: (8000, 2.0), 15: (8000, 2.0), 16: (3000, 5.0), (16, 45): None, (16, 79): None,
                  (16, 86): (5000, 3.0), (16, 87): (5000, 3.0), (16, 88): (5000, 3.0)},
        # treble lifts (Hz, dB) at the played pitch for what the SPU's interpolation dulls; set from the calibration's
        # band shares, no SPU room needed (voice 1: its sample has no top end to lift; the Saturn's comes from
        # interpolation images, which only a higher stored rate gives)
        reverb=False,       # the Saturn's DSP effect on voices 13-15 is inaudible (tail 56 dB down); the PS1 Room
                            # reverb made them 17 dB brighter
        seq_res_scale=10,   # libsnd's rate steps: resolution 48 played 1.6 % fast, 96 0.45 % slow, 480 = the Saturn's speed
        gate_frames=59.94,  # note lengths in whole frames (libsnd's step): short bass notes kept their length
        key_split={**{v: [(0, 76, {'decay_rate': 47}), (77, 127, {'decay_rate': 46})] for v in (13, 14, 15)},
                   11: [(0, 69, {}), (70, 127, {'gain_db': 1.4})]},
        # 13-15: the Saturn decays them at -98 dB/s, -129 from key 77; SPU rates 47 / 46 measure -92 / -135 (the
        # rate model's choice, 44, decayed at -490); 11: its top notes 1.4 dB quiet
        release_rate={2: 7, 3: 7, 4: 7},   # their note-end fade: 20 ms on the Saturn, 10 ms with rate 6
        level_db=11.0,      # as loud as the PS1's own Tengu Man music (PBGM05; a straight conversion was 11.8 dB
                            # quieter); the most the samples allow without changing the balance between voices
        vel_db={'melodic': SAT_VEL_DB,   # drum kit: libsnd fell 0.4-1.8 dB short of the Saturn at velocities 64-115
                'drum': [(0, -90.0), (40, -48.6), (50, -41.2), (64, -23.4), (72, -17.0), (80, -11.3), (86, -6.6),
                         (90, -3.4), (100, -4.2), (110, -0.7), (115, -0.2), (120, 0.0), (127, 0.0)]}),
    # Tempo: the Saturn plays 1.25 % below the SEQ's nominal tempo, libsnd 0.90 %; libsnd's tempo is quantised (a 0.35 %
    # change of the SEQ tempo didn't change its speed), so the 0.35 % difference stays.
}

def _interp_db(curve, v):
    xs, ys = zip(*curve)
    if v >= xs[-1]: return ys[-1]
    for (x0, y0), (x1, y1) in zip(curve, curve[1:]):
        if x0 <= v <= x1: return y0 + (y1 - y0) * (v - x0) / (x1 - x0)
    return ys[0]

def _upsample_linear(pcm, up):
    """x up with linear interpolation (keeps the images the SCSP's linear interpolation makes; see build_music_bank)."""
    n = len(pcm)
    if not isinstance(up, int) or n == 0:
        return [round(pcm[i // up] + (pcm[min(i // up + 1, n - 1)] - pcm[i // up]) * (i % up) / up) for i in range(n * up)]
    import numpy as np                  # same float64 operations in the same order, round half to even like round()
    a = np.asarray(pcm, np.int64); b = np.append(a[1:], a[-1])
    r = np.arange(up, dtype=np.int64)
    v = a[:, None] + ((b - a)[:, None] * r[None, :]) / up
    return np.rint(v).astype(np.int64).ravel().tolist()

def _libsnd_level(db):
    """dB (<= 0) -> libsnd 0-127 value with that attenuation (libsnd: amplitude ~ (value/127)^2)."""
    return max(1, min(127, round(127 * 10 ** (db / 40))))

@_memo
def build_music_bank(sat_name, tpl_n=0x2A, sfx_tpl_n=0x29, seq_song=None, tune=None, cal_keys=False, ps1_sfx=False):
    """New PS1 music bank (PBGM47 from WOOD2, later PBGM49 from CUT2) converted from a whole Saturn sound bank:
      - SEQ: song 0 -> Sony SEQ (sega_to_sony_seq; channels and program numbers unchanged).
      - VAB: Saturn voice v (music, 1..15) -> program v, one tone per TON layer (key range, base note + fine -> centre +
        shift, pitch-bend range from the voice header, TL + DISDL -> volume normalised to 127 for the loudest layer,
        DIPAN -> pan, EG -> ADSR (scsp_to_spu_adsr at the voice's median key), loops resampled so their length is a
        multiple of 28 (vag_encode_loop)); program 0 = Capcom's dummy; voice 0 (one-shot sounds) -> program 16 like
        build_boss_intro_bank (tone k -> key 60 + k, muted level-0 layers left out).
      - sound-ID table: the PS1 entry rule (reference §Sound).
      ps1_sfx: keep the PS1's own stage sounds instead (a bank that replaces an existing PS1 song, ST03A for PBGM05):
        program 16 = that PBGM's program 16 (tones and ADPCM copied) and its sound-ID table as it is; a music voice 16
        then goes to program 17.
    Program/tone attribute filler bytes are copied from Capcom's PBGM2A (tpl_n). Returns (pac bytes, info)."""
    import math, statistics
    bank = sat_sound_bank(sat_name)['bank']
    t, voices = ton_voices_raw(sat_name)
    tpl = ps1_pbgm(tpl_n); tv = vab_split(tpl[4], tpl[0x201])
    stpl = ps1_pbgm(sfx_tpl_n); sv = vab_split(stpl[4], stpl[0x201])
    assert tv['progs'][0][1] == 0x5A and tv['progs'][16][1] == 0x7F and tv['progs'][6][:2] == b'\x01\x00'
    song = sat_bank_song(sat_name, 0)
    A = sega_seq_decode(song)
    keys_of = collections.defaultdict(list); prog_of = {}
    for e in A['ev']:
        if e[1] == 'prog': prog_of[e[2]] = e[3]
        elif e[1] == 'note' and e[2] in prog_of: keys_of[prog_of[e[2]]].append(e[3])
    tune = MUSIC_TUNE.get(sat_name, {}) if tune is None else tune
    empty_tone = bytes(tv['tones'][6][0])
    NP = 18 if 16 in keys_of else 17                   # a music voice 16 (ST03A's drum kit) -> program 17
    pv_of = lambda v: 17 if v == 16 else v
    progs = [bytearray(tv['progs'][6]) for _ in range(NP)]
    tones = [[bytearray(empty_tone) for _ in range(16)] for _ in range(NP)]
    for p in range(NP):
        for k in range(16): tones[p][k][20:22] = struct.pack('<h', p)
    vags, vag_of = [], {}
    info = dict(bank=bank, layers=[], muted=[], sfx=[])
    # ---- music voices
    att = lambda L: (7 - L['disdl']) * 6.0 + L['tl'] * 0.375
    music = [(v, hdr, [ton_layer(r) for r in raws]) for v, (hdr, raws) in enumerate(voices) if v > 0]
    used = [L for v, hdr, ls in music if v in keys_of for L in ls if L['disdl']]
    a_ref = min(att(L) for L in used)
    # level_db (tune): raise the whole bank by that many dB (Capcom's PS1 banks are louder than a straight conversion):
    # program volume 127 instead of the template's, the rest as sample gain per sample (S, within its peak headroom),
    # so the tone volumes stay <= 127 and the calibrated balance between voices is kept
    LV = tune.get('level_db'); S_of = {}; pv_up = 0.0
    if LV is not None:
        pv_up = 40 * math.log10(127 / tv['progs'][0][1])
        for v, hdr, ls in music:
            if v not in keys_of: continue
            up_ = tune.get('upsample', {}).get(v, 1)
            for L in ls:
                if L['disdl'] == 0: continue
                g = tune.get('gain_db', {}); g = g.get((v, L['lo']), g.get(v, 0.0))
                T = -2 * (att(L) - a_ref) + g + LV            # output dB re tone 127 (libsnd: 40 log10 of the tone value)
                key = (L['sa'], L['lsa'], L['lea'], L['lpctl'], L['pcm8'], up_)
                peak = max(1, max(abs(x) for x in ton_pcm(t, L)))
                head = 20 * math.log10(32767 / peak) - 0.5
                S_of[key] = max(0.0, min(head, max(S_of.get(key, 0.0), T - pv_up)))
    want = {}; lfo = {}
    pp = {}; bent = set()
    for e in A['ev']:
        if e[1] == 'prog': pp[e[2]] = e[3]
        elif e[1] == 'bend' and e[2] in pp: bent.add(pp[e[2]])
    for v, hdr, ls in music:
        if v not in keys_of: continue                     # voice not played by the song
        assert v < 17 and len(ls) <= 16
        mk = statistics.median_low(keys_of[v])
        n_t = 0
        lw = {L['mod'][4:6] for L in ls if L['disdl']}
        assert len(lw) <= 1, (v, 'layers with different LFOs')
        if lw and any(next(iter(lw))):
            pb_v = 1 if v not in bent else hdr[0]   # never bent by the song: 1 semitone, finer steps for the LFO
            lfo[v] = scsp_lfo(struct.unpack('>H', next(iter(lw)))[0], pb_v)
        segs = []           # key_split (tune): {voice: [(lo, hi, overrides)]} -> one tone per layer and key range, with
        for L in ls:        # its own gain_db / decay_dbps / decay_rate / upsample (the Saturn's level, decay and top end
            sp = tune.get('key_split', {}).get(v)          # change with pitch; one PS1 tone can't follow that)
            if not sp or L['lo'] == L['hi']: segs.append((L, {})); continue
            for a_, b_, ov in sp:
                lo_, hi_ = max(L['lo'], a_), min(L['hi'], b_)
                if lo_ <= hi_: segs.append((dict(L, lo=lo_, hi=hi_), ov))
        for L, ov in segs:
            # modulation bytes: only the LFO word (0x14-0x15) and the DSP send IMXL (0x17 bits 0-2, ISEL 0) handled
            assert L['ssctl'] == 0 and L['lpctl'] in (0, 1) and not any(L['mod'][:4]) and L['mod'][6] == 0 \
                and not L['mod'][7] & 0xF8 and not any(L['extra']), (v, L)
            if L['disdl'] == 0: info['muted'].append((v, L['lo'])); continue
            pcm = ton_pcm(t, L)
            key44 = L['base'] - L['fine'] / 256.0          # key at which the sample plays at 44.1 kHz
            up = ov.get('upsample', tune.get('upsample', {}).get(v, 1))
            sh_ = tune.get('shelf_db', {}); sh_ = sh_.get((v, L['lo']), sh_.get(v))   # (Hz, dB): treble lift above Hz at the played pitch (per drum key or voice; None = none)
            if sh_:
                import numpy as np
                kk = L['lo'] if L['lo'] == L['hi'] else mk                    # drum key, or the voice's median key
                fs = 44100 * 2 ** ((kk - L['base'] + L['fine'] / 256) / 12)    # output rate of this sample there
                def lift(a):
                    X = np.fft.rfft(np.asarray(a, float)); f = np.fft.rfftfreq(len(a), 1 / fs)
                    return np.fft.irfft(X * (1 + (10 ** (sh_[1] / 20) - 1) * f ** 2 / (f ** 2 + sh_[0] ** 2)), len(a))
                if L['lpctl'] == 0: y = lift(pcm)
                else:                    # loop kept seamless: filter attack + 4 loop passes, keep a steady pass
                    n0, n1 = L['lsa'], L['lea']; lp = list(pcm[n0:n1]); e = lift(list(pcm[:n0]) + lp * 4)
                    y = np.concatenate([e[:n0], e[n0 + 2 * len(lp):n0 + 3 * len(lp)]])
                pcm = [int(max(-32768, min(32767, round(x)))) for x in y]
            S = S_of.get((L['sa'], L['lsa'], L['lea'], L['lpctl'], L['pcm8'], up), 0.0)
            if S: pcm = [max(-32768, min(32767, round(x * 10 ** (S / 20)))) for x in pcm]
            down = 1.0
            if up < 1:
                # stored at a lower rate (looped voices only, through the loop resampler): the SPU's pitch is capped at
                # 4x 44.1 kHz (0x4000), so a voice played higher than that sounds flat (calibration: CUT2 voices 3/4 up
                # to 6x, -200 cents); key44 is corrected through the loop ratio below
                assert L['lpctl'] == 1
                down, up = up, 1
            if up > 1:
                # x up with LINEAR interpolation: keeps the images the SCSP's linear interpolation produces above the
                # sample's Nyquist, which the SPU's Gaussian interpolation would otherwise remove (calibration: the
                # Saturn's drums/leads had 6-11 dB more above 10 kHz)
                pcm = _upsample_linear(pcm, up)
                L = dict(L, lsa=L['lsa'] * up, lea=L['lea'] * up)
                key44 -= 12 * math.log2(up)   # more samples per period: same pitch needs a higher rate
            vk = (L['sa'], L['lsa'], L['lea'], L['lpctl'], L['pcm8'], up, down)   # (a shared sample: lifted at its first key's pitch)
            if vk not in vag_of:
                if L['lpctl'] == 1:
                    ln = L['lea'] - L['lsa']
                    nl = max(28, 28 * round(ln * down / 28))
                    rs, nls = resample_loop(pcm, L['lsa'], L['lea'], nl)
                    pad = 28 - nls % 28 if nls % 28 else 0
                    data = [0] * pad + rs
                    vags.append(vag_encode_loop(data, pad + nls)); ratio = nl / ln
                else:
                    vags.append(vag_encode(pcm)); ratio = 1.0
                vag_of[vk] = (len(vags), ratio)
            vi, ratio = vag_of[vk]
            c, sh = _center_shift(key44 - 12 * math.log2(ratio))  # resampled by `ratio`: same pitch at a lower key
            rate_at_mk = 44100 * 2 ** ((mk - key44) / 12)
            octave = math.floor(math.log2(rate_at_mk / 44100))
            a1, a2 = scsp_to_spu_adsr(L, octave)
            dd = ov.get('decay_dbps', tune.get('decay_dbps', {}).get(v))
            if dd is not None or 'decay_rate' in ov:               # measured: one exponential slope from key-on
                sr = ov.get('decay_rate', tune.get('decay_rate', {}).get(v) if 'decay_dbps' not in ov else None)
                if sr is None: sr = _best(SPU_SR_BEST, dd / 44100)  # or an SPU rate chosen by test
                a1 = (a1 & 0x7F00) | 0x00FF; a2 = 0xC000 | (sr << 6) | 0x20 | (a2 & 0x1F)   # keeps the attack
            if v in tune.get('release_rate', {}): a2 = (a2 & ~0x1F) | tune['release_rate'][v]   # measured note-end fade
            tn = bytearray(empty_tone)
            tn[0] = 0; tn[1] = 4 if L['mod'][7] & 7 and tune.get('reverb', True) else 0   # DSP send -> libsnd reverb (the game's Room reverb)
            g = tune.get('gain_db', {}); g = g.get((v, L['lo']), g.get(v, 0.0)) + ov.get('gain_db', 0.0)
            want[(v, n_t)] = 127 * 10 ** (-(att(L) - a_ref) / 20) * 10 ** (g / 40)
            if LV is not None: want[(v, n_t)] *= 10 ** ((LV - pv_up - S) / 40)    # the rest of level_db after S
            tn[2] = max(1, min(127, round(want[(v, n_t)])))
            tn[3] = scsp_pan(L['dipan']); tn[4] = c; tn[5] = sh; tn[6] = L['lo']; tn[7] = L['hi']
            tn[8:12] = bytes(4); tn[12] = tn[13] = lfo[v]['pb'] if v in lfo else hdr[0]
            tn[16:24] = struct.pack('<2H2h', a1, a2, pv_of(v), vi)
            tones[pv_of(v)][n_t] = tn; n_t += 1
            info['layers'].append(dict(voice=v, keys=(L['lo'], L['hi']), vag=vi, loop=L['lpctl'], vol=tn[2], pan=tn[3],
                                       center=c, shift=sh, adsr=(a1, a2), pb=hdr[0], median_key=mk))
        progs[pv_of(v)] = bytearray(tv['progs'][0]); progs[pv_of(v)][0] = n_t
    mx = max(want.values())
    if LV is not None:
        for vv in {vv for vv, n in want}: progs[pv_of(vv)][1] = 127
        info['level'] = dict(level_db=LV, prog=pv_up, sample_db=sorted(S_of.values()), short_db=max(0.0, 40 * math.log10(mx / 127)))
        for (vv, n), w in want.items(): tones[pv_of(vv)][n][2] = max(1, min(127, round(w)))
    elif mx > 127:                          # tuned volumes above 127: scale the music tones down, program volume up
        mvol = min(127, round(tv['progs'][0][1] * mx / 127))
        for (vv, n), w in want.items(): tones[pv_of(vv)][n][2] = max(1, min(127, round(w * 127 / mx)))
        for vv in {vv for vv, n in want}: progs[pv_of(vv)][1] = mvol
        info['mvol'] = mvol
    # ---- voice 0: one-shot sounds -> program 16 (as build_boss_intro_bank), or the PS1 bank's own
    hdr0, raws0 = voices[0]
    tone_of = {}
    if ps1_sfx:
        own = ps1_pbgm(bank - 1); ov = vab_split(own[4], own[0x201]); new_vi = {}
        for k in range(ov['progs'][16][0]):
            tn = bytearray(ov['tones'][16][k]); vi = struct.unpack('<h', tn[22:24])[0]
            if vi not in new_vi: vags.append(ov['vags'][vi - 1]); new_vi[vi] = len(vags)
            tn[22:24] = struct.pack('<h', new_vi[vi]); tones[16][k] = tn
        progs[16] = bytearray(ov['progs'][16])
        info['sfx'] = [('ps1', bank - 1, k) for k in range(ov['progs'][16][0])]
    for r in ([] if ps1_sfx else raws0):
        L = ton_layer(r); k = L['lo']
        assert L['lo'] == L['hi'] and L['lpctl'] == 0 and L['pcm8']
        if L['disdl'] == 0: info['muted'].append((0, k)); continue
        assert len(tone_of) < 15
        tone_of[k] = len(tone_of); key = 60 + tone_of[k]
        vk = (L['sa'], L['lsa'], L['lea'], 0, True)
        up = tune.get('upsample', {}).get(0, 1)
        if vk not in vag_of:
            pcm = ton_pcm(t, L)
            if up > 1: pcm = _upsample_linear(pcm, up)
            vags.append(vag_encode(pcm)); vag_of[vk] = (len(vags), 1.0)
        c, sh = _center_shift(key + L['base'] - k - L['fine'] / 256.0 - 12 * math.log2(up))
        tn = bytearray(sv['tones'][16][0]); tn[3] = scsp_pan(L['dipan']); tn[4] = c; tn[5] = sh
        tn[2] = _libsnd_level(tune.get('gain_db', {}).get(0, 0.0))     # calibration: one-shots 0.8 dB loud at 127
        tn[6] = tn[7] = key; tn[20:24] = struct.pack('<2h', 16, vag_of[vk][0])
        tones[16][tone_of[k]] = tn
        info['sfx'].append((k, key, c, sh, len(ton_pcm(t, L))))
    if not ps1_sfx: progs[16] = bytearray(tv['progs'][16]); progs[16][0] = len(tone_of)
    drums = {vv for vv, hdr, ls in music if vv in keys_of and not all(L['lo'] == 0 for L in ls)}
    vel_db = tune.get('vel_db', {'melodic': SAT_VEL_DB}); k7 = tune.get('cc7_db_per_step', SAT_CC7_DB_PER_STEP)
    vel_map = lambda p, vel: _libsnd_level(_interp_db(vel_db.get('drum' if p in drums else 'melodic', vel_db['melodic']), vel))
    cc7_map = lambda c: _libsnd_level(-k7 * (127 - c))
    pm = {**({0: 16} if cal_keys else {}), **({16: 17} if NP == 18 else {})}
    seq, sinfo = sega_to_sony_seq(seq_song or song, cc7_map=cc7_map, vel_map=vel_map, tempo_scale=tune.get('tempo_factor', 1.0),
                                  prog_map=pm or None, lfo=lfo or None, res_scale=tune.get('seq_res_scale', 1),
                                  gate_frames=tune.get('gate_frames'), time_scale=tune.get('time_scale', 1.0),
                                  lfo_level=lambda c, db: _libsnd_level(-k7 * (127 - c) + db),
                                  key_map=(lambda p, k: 60 + tone_of.get(k, 0) if p == 0 else k) if cal_keys else None)
    info['seq'] = sinfo
    head = bytearray(tv['head'])
    allp = [bytearray(16) for _ in range(128)]
    for p in range(NP): allp[p] = progs[p]
    for p in range(NP, 128): allp[p] = bytearray(tv['progs'][p])
    head[0x14:0x16] = struct.pack('<H', sum(pr[0] for pr in progs))
    v = dict(head=head, progs=allp, tones=tones, vags=vags)
    vhdr, body = vab_join(v)
    ids = own[5] if ps1_sfx else sound_id_table(sat_name, len(tpl[5]), tone_of)
    pac = pac_build([(5, ids), (4, vhdr), (0x201, body), (1, seq)])
    info.update(hdr=len(vhdr), body=len(body), seq_size=len(seq), size=len(pac), vags=len(vags), lfo=lfo, tones=dict(
        (p, progs[p][0]) for p in range(NP) if progs[p][0] and progs[p][1]))
    return pac, info


# Sound play 0x800FCEB4(id, pan, voice): an ID >= 0x51 whose table entry's bank differs from the loaded bank
# (0x8015E9C4) branches (0x800FCF14) into the key-on path with the caller's a2 as the SPU voice; 0x8010544C (play at
# object) passes the sound ID as a2, so it stores the ID at 0x801D2860 + 2 * id and a priority at 0x801D28FC + id
# (id 0x5E -> camera Y 0x801D291C = 0x5E for the rest of the frame -> on-screen objects despawn). Never reached in the
# original game (IDs always match their bank); reached by Wood Man's fight sounds while music 0x47 is a placeholder
# bank. Guard: a mismatched ID returns (epilogue 0x800FD0CC). Found by emulating frames from a tester's save state.
PS1_SND_MISMATCH = 0x800FCF14
PS1_SND_MISMATCH_ORIG_FP = '32dce18ae906'       # fingerprint of: bne s6, v1, 0x800FCFE0
PS1_SND_MISMATCH_TO = 0x800FD0CC                 # new branch target (the epilogue)

def patch_sound_guard(disc):
    old = disc.exe_words(PS1_SND_MISMATCH, 1)[0]
    assert fp(old) == PS1_SND_MISMATCH_ORIG_FP
    disc.exe_write_words(PS1_SND_MISMATCH, [old & 0xFFFF0000 | (PS1_SND_MISMATCH_TO - PS1_SND_MISMATCH - 4) >> 2 & 0xFFFF])

def patch_music_hook(disc, vf0):
    """play_bgm for music n >= 0x46: file number = vf0 + n (a 'virtual' file-table entry 0x80136F7C + 12 * (vf0 + n) that
    lives in the stage overlay, see build_image), and always loop like the other boss songs (the loop-flag table
    0x801376D4 has only 0x40 entries; n >= 0x46 would read file-number bytes). n < 0x46 behaves exactly as before."""
    rd = lambda a: disc.exe_words(a, 1)[0]
    wr = disc.exe_write_words
    V0, V1, A0, A1, A2, AT, S0 = 2, 3, 4, 5, 6, 1, 16
    assert 0 <= vf0 <= 0xFFFF
    for a in PS1_BGM_FILE_SITES:
        old = [rd(a + 4 * i) for i in range(10)]
        assert fp(old) == '6a4e099b02f6', hex(a)
        wr(a, [m_i(0x0B, S0, V0, PS1_BGM_FIRST_NEW),          # sltiu v0, s0, 0x46
               m_i(0x05, V0, 0, 3),                            # bnez  v0, +3 (to lui v0)
               m_i(0x09, S0, A0, 0x17),                        # addiu a0, s0, 0x17   (original table value)
               m_i(0x0D, 0, A0, vf0),                          # ori   a0, zero, vf0
               m_r(A0, S0, A0, 0, 0x21),                       # addu  a0, a0, s0
               old[4], old[5], old[6],                         # (moved) lui v0; addiu v0, 0x78BC; lui at, 0x8017
               old[8], old[7]])                                # (moved) jal 0x800FB9C0; sw v0, -0x2D6C(at) (delay slot)
    for a, x in PS1_BGM_LOOP_SITES:
        old = [rd(a + 4 * i) for i in range(14)]
        assert fp([old[0], old[2], old[13]]) == '0bb56b1f4e77' and old[1] == m_r(AT, x, AT, 0, 0x21), hex(a)
        wr(a, [m_i(0x0B, x, A2, PS1_BGM_FIRST_NEW),            # sltiu a2, x, 0x46
               old[0], old[1], old[2],                         # (kept) lui at; addu at, at, x; lbu v0, 0x76D4(at)
               m_i(0x0E, A2, A2, 1),                           # xori a2, a2, 1    (1 if n >= 0x46)
               m_r(V0, A2, V0, 0, 0x25),                       # or   v0, v0, a2
               m_r(0, A0, A0, 16, 0), m_r(0, A0, A0, 16, 3),   # sll a0, a0, 16; sra a0, a0, 16
               m_i(0x0B, V0, A2, 1),                           # sltiu a2, v0, 1   (loop count: 0 = forever)
               m_i(0x0D, 0, A1, 1), 0, 0, 0])                  # ori a1, zero, 1; nops; (jal 0x800C6474 kept)

# ----------------------------------------------------------------- M8t3: Saturn Tengu Man stage music (README item 7)
# Both versions of Tengu Man's stage song are on the disc: the PS1's PBGM05 (file-table entry 0x17 + 5) and the Saturn's
# ST03A converted to a PS1 bank with the PS1 stage sounds kept (build_music_bank ps1_sfx). The switch is the file-table
# entry itself: the table in the EXE is read-only to the game (only 0x800FC238 reads it, copying LBA / size / type into a
# load request) and the EXE is loaded once at boot, so an entry rewritten at run time stays until power-off or reset, and
# play_bgm keeps music number 5 (the stage's sound IDs need bank byte = music + 1). Default after boot: the PS1's song.
PS1_TENGU_MUSIC = 5                   # music number of Tengu Man's stage (PBGM05)
PS1_TENGU_FILE = 0x17 + PS1_TENGU_MUSIC   # its file-table entry (play_bgm: file = music + 0x17)

def m8t_tengu_bank():
    """The Saturn Tengu Man stage bank (ST03A converted, PS1 stage sounds kept): (bytes, info)."""
    return build_music_bank('ST03A.PAC', ps1_sfx=True)

def patch_m8_tengu_bank(disc, lba):
    """Write the converted Saturn Tengu Man bank into the free sectors at `lba` (no file-table entry of its own: the
    switch rewrites entry PS1_TENGU_FILE at run time)."""
    bank, binfo = m8t_tengu_bank()
    return dict(tengu_lba=lba, tengu_size=len(bank), tengu_sectors=place_file(disc, lba, bank),
                **{'tengu_bank_' + k: v for k, v in binfo.items() if k not in ('layers', 'sfx')})

def find_m8_tengu_bank(disc, bank=None):
    """(LBA, size) of the Saturn Tengu Man bank in the track-1 extension of a built image (None if absent)."""
    bank = bank if bank is not None else m8t_tengu_bank()[0]
    for lba in range(PS1_FREE_LBA[0], PS1_FREE_LBA[1] + PS1_EXTEND_SECTORS):
        if disc.read(lba, 2048) == bank[:2048] and disc.read(lba, len(bank)) == bank:
            return lba, len(bank)
    return None

# 5x7 glyphs (8x8 cells), ASCII 0x20-0x5F, drawn for this tool (rows separated by |).
TEXT_FONT = {
 ' ': '.....|.....|.....|.....|.....|.....|.....', '!': '..#..|..#..|..#..|..#..|..#..|.....|..#..',
 '(': '...#.|..#..|.#...|.#...|.#...|..#..|...#.', ')': '.#...|..#..|...#.|...#.|...#.|..#..|.#...',
 '+': '.....|..#..|..#..|#####|..#..|..#..|.....', ',': '.....|.....|.....|.....|.....|..#..|.#...',
 '-': '.....|.....|.....|#####|.....|.....|.....', '.': '.....|.....|.....|.....|.....|.....|..#..',
 '/': '....#|...#.|...#.|..#..|.#...|.#...|#....', ':': '.....|..#..|.....|.....|.....|..#..|.....',
 '<': '...#.|..#..|.#...|#....|.#...|..#..|...#.', '>': '.#...|..#..|...#.|....#|...#.|..#..|.#...',
 '=': '.....|.....|#####|.....|#####|.....|.....', '?': '.###.|#...#|....#|...#.|..#..|.....|..#..',
 '0': '.###.|#...#|#..##|#.#.#|##..#|#...#|.###.', '1': '..#..|.##..|..#..|..#..|..#..|..#..|.###.',
 '2': '.###.|#...#|....#|...#.|..#..|.#...|#####', '3': '#####|...#.|..#..|...#.|....#|#...#|.###.',
 '4': '...#.|..##.|.#.#.|#..#.|#####|...#.|...#.', '5': '#####|#....|####.|....#|....#|#...#|.###.',
 '6': '..##.|.#...|#....|####.|#...#|#...#|.###.', '7': '#####|....#|...#.|..#..|.#...|.#...|.#...',
 '8': '.###.|#...#|#...#|.###.|#...#|#...#|.###.', '9': '.###.|#...#|#...#|.####|....#|...#.|.##..',
 'A': '.###.|#...#|#...#|#####|#...#|#...#|#...#', 'B': '####.|#...#|#...#|####.|#...#|#...#|####.',
 'C': '.###.|#...#|#....|#....|#....|#...#|.###.', 'D': '###..|#..#.|#...#|#...#|#...#|#..#.|###..',
 'E': '#####|#....|#....|####.|#....|#....|#####', 'F': '#####|#....|#....|####.|#....|#....|#....',
 'G': '.###.|#...#|#....|#.###|#...#|#...#|.####', 'H': '#...#|#...#|#...#|#####|#...#|#...#|#...#',
 'I': '.###.|..#..|..#..|..#..|..#..|..#..|.###.', 'J': '..###|...#.|...#.|...#.|...#.|#..#.|.##..',
 'K': '#...#|#..#.|#.#..|##...|#.#..|#..#.|#...#', 'L': '#....|#....|#....|#....|#....|#....|#####',
 'M': '#...#|##.##|#.#.#|#.#.#|#...#|#...#|#...#', 'N': '#...#|#...#|##..#|#.#.#|#..##|#...#|#...#',
 'O': '.###.|#...#|#...#|#...#|#...#|#...#|.###.', 'P': '####.|#...#|#...#|####.|#....|#....|#....',
 'Q': '.###.|#...#|#...#|#...#|#.#.#|#..#.|.##.#', 'R': '####.|#...#|#...#|####.|#.#..|#..#.|#...#',
 'S': '.####|#....|#....|.###.|....#|....#|####.', 'T': '#####|..#..|..#..|..#..|..#..|..#..|..#..',
 'U': '#...#|#...#|#...#|#...#|#...#|#...#|.###.', 'V': '#...#|#...#|#...#|#...#|#...#|.#.#.|..#..',
 'W': '#...#|#...#|#...#|#.#.#|#.#.#|#.#.#|.#.#.', 'X': '#...#|#...#|.#.#.|..#..|.#.#.|#...#|#...#',
 'Y': '#...#|#...#|.#.#.|..#..|..#..|..#..|..#..', 'Z': '#####|....#|...#.|..#..|.#...|#....|#####',
 '[': '.###.|.#...|.#...|.#...|.#...|.#...|.###.', ']': '.###.|...#.|...#.|...#.|...#.|...#.|.###.',
 '^': '..#..|.#.#.|#...#|.....|.....|.....|.....', '_': '.....|.....|.....|.....|.....|.....|#####',
}

# ----------------------------------------------------------------- M8t4: Tengu Man music toggle on the stage select
# Stage select (02b §Stage select): mode function 0x800FF8C4, state struct 0x8017054C (+0 state, 1 = waiting for input;
# +4 = stage under the cursor, Tengu Man = 3). Its init 0x800FF948 reloads DEMO.BIN (overlay 0) when another overlay
# is loaded, then streams file 0x72 (`jal 0x800FB9C0` at 0x800FF9C8, a0 = 0x72) and waits for it; every frame after
# that it renders through `jal 0x800F96C0` at 0x800FF92C. M8t4: the init's call reads the ticker overlay into the RAM
# that is free at the stage select (0x801DD000-0x801E9FFF, zero in Mednafen) first (tk_load in DEMO.BIN, a virtual
# entry there), then streams file 0x72 as before; the render call goes through tk_frame (first word of the ticker
# overlay). With the cursor on Tengu Man, tk_frame draws the pedestal ticker ("♪ PLAYSTATION" / "♪ SATURN", from
# entry 28's LBA) and the blinking hint under the platform ("- [SELECT] : CHANGE MUSIC -") while the stage select is on
# screen: state 1 (waiting for input), 3 (boss chosen) and 4 up to step 1 (the banner sliding in; step 3 = the white
# flash and the boss-intro screen, +1 = step). In state 1 Select rewrites entry 28's LBA and size (the PS1's, read from
# the user's disc, or the converted bank's) and plays the toggle sound. Everything is drawn on the fade's OT entry with
# 4-bpp sprites from VRAM x 960-1023, y 256-290 (unused there).
M8K_BASE = 0x801DD000                 # ticker overlay (stage-select time: free RAM)
M8K_PRIMS = 0x801E8000                # 2 x 0x800 bytes of primitives (free RAM at the stage select)
M8K_INIT_CALL, M8K_INIT_FP = 0x800FF9C8, '926c3991090f'     # jal 0x800FB9C0 (stream file 0x72), a0 set in the delay slot
M8K_RENDER_CALL, M8K_RENDER_FP = 0x800FF92C, 'd6e9aa9f4173' # jal 0x800F96C0 (render the rest)
M8K_TENGU = 3                         # stage under the cursor: Tengu Man
M8K_SELECT = 0x100                    # pad bit of Select (pad pressed this frame 0x801B2958)
M8K_SOUND = 0x0E                      # common-bank sound (PCOMMON, loaded everywhere; chosen for the toggle)
M8K_TICK_GAP = 40                     # px between ticker passes; 1 px every 2 frames (30 px/s)
M8K_BLINK = (90, 60)                  # hint shown / hidden, frames
M8K_HINT_XY = (202, 220)              # hint's top-left on screen (its shadow adds a row and a column): centred
                                      # under the platform, its last row inside 8 lines of overscan
M8K_INK = 0x57F1                      # ticker letters: the panel's screen green
M8K_HINT_CLUT = (0, 0x7FFF, 0x1442, 0x5AB5)   # transparent, white, dark shadow, light grey (button corners)
M8K_SPANS = ((209, 286), (208, 287), (208, 287), (207, 288), (207, 288), (207, 288), (210, 285))  # panel rows y 204-210
M8K_NOTE = ('..#..', '..##.', '..#.#', '..#..', '.##..', '###..', '.#...')
M8K_SMALL = {'S': '###|#..|###|..#|###', 'E': '###|#..|##.|#..|###', 'L': '#..|#..|#..|#..|###',
             'C': '.##|#..|#..|#..|.##', 'T': '###|.#.|.#.|.#.|.#.', 'H': '#.#|#.#|###|#.#|#.#',
             'A': '.#.|#.#|###|#.#|#.#', 'N': '#..#|##.#|#.##|#..#|#..#', 'G': '.##|#..|#.#|#.#|.##',
             'M': '#...#|##.##|#.#.#|#...#|#...#', 'U': '#.#|#.#|#.#|#.#|###', 'I': '###|.#.|.#.|.#.|###',
             '-': '...|...|###|...|...', ':': '.|#|.|#|.'}   # the hint's small letters (5 rows, own widths)

def m8k_text(word, note=False):
    """Columns (lists of 7 bools) of `word` in TEXT_FONT, 1-px letter gap; `note` puts the tool's note and 4 px first."""
    cols = [[r[x] == '#' for r in M8K_NOTE] for x in range(5)] + [[False] * 7] * 4 if note else []
    for k, ch in enumerate(word):
        g = TEXT_FONT[ch].split('|')
        cols += [[g[y][x] == '#' for y in range(7)] for x in range(5)]
        if k < len(word) - 1: cols.append([False] * 7)
    return cols

def m8k_4bpp(rows, hw):
    """4-bpp image (u16 list, `hw` halfwords per row) of index rows."""
    out = []
    for r in rows:
        for x4 in range(hw):
            out.append(sum((r[4 * x4 + k] if 4 * x4 + k < len(r) else 0) << (4 * k) for k in range(4)))
    return out

def m8k_small(text):
    """Columns (lists of 5 bools) of `text` in the small letters, 1-px gap; a space is 2 px."""
    out = []
    for k, ch in enumerate(text):
        if ch == ' ': out += [[False] * 5] * 2; continue
        g = M8K_SMALL[ch].split('|'); out += [[g[y][x] == '#' for y in range(5)] for x in range(len(g[0]))]
        if k < len(text) - 1: out.append([False] * 5)
    return out

def m8k_hint():
    """The hint line as index rows (1 white, 2 shadow, 3 grey corner): '- ', the button (15 x 4 over the small
    'SELECT'), ' : CHANGE MUSIC -', all in the small letters; 10 rows of glyphs (text on rows 3-7), the shadow one
    row and column further."""
    lab = m8k_small('SELECT'); H = 10
    btn = [[(4 <= x < 19 and y < 4) or (y >= 5 and lab[x][y - 5]) for y in range(H)] for x in range(len(lab))]
    corner = {(x, y) for x in (4, 18) for y in (0, 3)}
    lift = lambda cs: [[False] * 3 + c + [False] * 2 for c in cs]
    left = lift(m8k_small('- ')); right = lift(m8k_small(' : CHANGE MUSIC -'))
    cols = left + [[False] * H] + btn + right
    o = len(left) + 1
    w, h = len(cols) + 1, H + 1
    img = [[0] * w for _ in range(h)]
    ink = lambda x, y: 0 <= x < len(cols) and 0 <= y < H and cols[x][y]
    for x in range(len(cols)):
        for y in range(H):
            if not cols[x][y]: continue
            for dx, dy in ((1, 1), (1, 0), (0, 1)):
                if not ink(x + dx, y + dy): img[y + dy][x + dx] = 2
    for x in range(len(cols)):
        for y in range(H):
            if cols[x][y]: img[y][x] = 3 if (x - o, y) in corner else 1
    return img

M8K_C = r"""
typedef unsigned char u8; typedef unsigned short u16; typedef unsigned int u32; typedef signed short s16;
extern void render_rest(void);                 /* 0x800F96C0 */
extern void add_prim(void *ot, void *p);       /* 0x800D313C: libgpu AddPrim */
extern void load_image(void *rect, void *src); /* 0x800D3E84: libgpu LoadImage */
extern void sound_centred(int id, int a);      /* 0x800FD100 */
#define PAD_NEW    (*(volatile u16 *)0x801B2958)
#define DRAWBUF    (*(volatile u32 *)0x80162A8C)
#define DRAWSTRUCT (*(volatile u32 *)0x80170334)
#define SS_STATE   (*(volatile u8 *)0x8017054C)
#define SS_STEP    (*(volatile u8 *)0x8017054D)
#define SS_CUR     (*(volatile u8 *)0x80170550)
#define ENTRY      ((volatile u32 *)@ENTRY@)
#define PRIMS      ((u32 *)@PRIMS@)
/* filled in by the build: entry 28's (LBA, size) on the user's disc and the converted bank's; the images */
volatile u32 ent_ps[2] __attribute__((section(".data"))) = {1, 1};
volatile u32 ent_sat[2] __attribute__((section(".data"))) = {1, 1};
u16 img_ps[64 * 7] __attribute__((aligned(4), section(".data"))) = {1};
u16 img_sat[64 * 7] __attribute__((aligned(4), section(".data"))) = {1};
u16 img_hint[@HINT_HW@ * @HINT_H@] __attribute__((aligned(4), section(".data"))) = {1};
static const u16 tk_l[7] = @SPAN_L@, tk_r[7] = @SPAN_R@;
static const s16 r_ps[4] = {960, 256, 64, 7}, r_sat[4] = {960, 264, 64, 7}, r_clut[4] = {960, 272, 16, 1};
static const s16 r_hint[4] = {960, 280, @HINT_HW@, @HINT_H@}, r_hclut[4] = {976, 272, 16, 1};
static const u16 clut_tk[16] = {0, @INK@}, clut_hint[16] = @HINT_CLUT@;
static u32 frames __attribute__((section(".data"))) = 0, blink __attribute__((section(".data"))) = 0;
static u8 was __attribute__((section(".data"))) = 0;
void tk_frame(void)
{
  /* shown while the stage select is on screen: waiting for input (1), the boss chosen (3) and the banner sliding
     in (4, step 1); gone from the white flash on (4, step 2+) */
  u32 st = SS_STATE, on = st == 1 || st == 3 || (st == 4 && SS_STEP <= 1);
  if (on && SS_CUR == @TENGU@) {
    u32 *p = PRIMS + (DRAWBUF & 1) * 0x200;
    int sat = ENTRY[0] == ent_sat[0], j;
    u32 tw, vb, P, pos, y;
    if (!was) { load_image((void *)r_ps, img_ps); load_image((void *)r_sat, img_sat); load_image((void *)r_clut, (void *)clut_tk);
                load_image((void *)r_hint, img_hint); load_image((void *)r_hclut, (void *)clut_hint); was = 1; frames = 0; blink = 0; }
    if (st == 1 && (PAD_NEW & @SELECT@)) {
      sat ^= 1;
      ENTRY[0] = sat ? ent_sat[0] : ent_ps[0]; ENTRY[1] = sat ? ent_sat[1] : ent_ps[1];
      sound_centred(@SOUND@, 0); frames = 0;
    }
    tw = sat ? @PX_SAT@ : @PX_PS@; vb = sat ? 8 : 0; P = tw + @GAP@; pos = (frames >> 1) % P;
    for (y = 0; y < 7; y++) {
      int l = tk_l[y], r = tk_r[y];
      for (j = (frames >> 1) >= P ? -1 : 0; j <= 1; j++) {   /* no earlier pass right after a (re)start */
        int xc = 289 - (int)pos + j * (int)P, lo = xc > l ? xc : l, hi = xc + (int)tw - 1 < r ? xc + (int)tw - 1 : r;
        if (lo > hi) continue;
        p[0] = 4 << 24; p[1] = 0x65u << 24 | 0x808080;
        p[2] = (204 + y) << 16 | lo;
        p[3] = (u32)(272 << 6 | 960 >> 4) << 16 | (vb + y) << 8 | (lo - xc);
        p[4] = 1 << 16 | (hi - lo + 1);
        add_prim((void *)(DRAWSTRUCT + 0x94), p); p += 5;
      }
    }
    if (blink < @BLINK_ON@) {
      p[0] = 4 << 24; p[1] = 0x65u << 24 | 0x808080;
      p[2] = @HINT_Y@ << 16 | @HINT_X@;
      p[3] = (u32)(272 << 6 | 976 >> 4) << 16 | 24 << 8 | 0;
      p[4] = @HINT_H@ << 16 | @HINT_PX@;
      add_prim((void *)(DRAWSTRUCT + 0x94), p); p += 5;
    }
    if (++blink >= @BLINK_ON@ + @BLINK_OFF@) blink = 0;
    frames++;
    p[0] = 1 << 24; p[1] = 0xE1u << 24 | 1 << 10 | 1 << 4 | 15;     /* draw mode: texture page x 960, y 256, 4 bpp */
    add_prim((void *)(DRAWSTRUCT + 0x94), p);
  } else was = 0;
  render_rest();
}
"""
M8K_STUB_C = r"""
extern char tk_vf[];                          /* linker symbol = virtual file number of the ticker overlay */
extern void file_load(int idx, void *dst);    /* 0x800FB7A4 */
extern int load_done(void);                   /* 0x800FC1E0 */
extern void yield_(int n);                    /* 0x800F7ECC */
extern void ss_wait(void);                    /* 0x8011C858: what the init's own wait loops call */
extern void flush_cache(void);                /* 0x800D78EC: BIOS FlushCache */
extern void stream_file(int f);               /* 0x800FB9C0 */
void tk_load(int f)
{
  file_load((int)tk_vf, (void *)TK_BASE_);
  while (!load_done()) { ss_wait(); yield_(1); }
  flush_cache();
  stream_file(f);                             /* the call this replaces (file 0x72) */
}
"""
M8K_STUB_SYMS = dict(file_load=0x800FB7A4, load_done=0x800FC1E0, yield_=0x800F7ECC, ss_wait=0x8011C858,
                     flush_cache=0x800D78EC, stream_file=0x800FB9C0)

@_memo
def m8k_images():
    """(ticker 'PLAYSTATION' u16 list, its width, 'SATURN' list, width, hint u16 list, hint width px, hint hw)."""
    def strip(word):
        cols = m8k_text(word, note=True); w = len(cols); assert w <= 256
        return m8k_4bpp([[1 if cols[x][y] else 0 for x in range(w)] for y in range(7)], 64), w
    ps, wp = strip('PLAYSTATION'); sa, ws = strip('SATURN')
    h = m8k_hint(); hpx = len(h[0]); hhw = (hpx + 3) // 4
    return ps, wp, sa, ws, m8k_4bpp(h, hhw), hpx, hhw

def m8k_overlay(ent_ps, ent_sat, workdir=None):
    """The ticker overlay (bytes, symbols), linked at M8K_BASE; ent_ps / ent_sat = (LBA, size) of music 5's file."""
    ps, wp, sa, ws, hint, hpx, hhw = m8k_images()
    src = M8K_C
    for k, v in dict(ENTRY_=f'{PS1_FILETAB + 12 * PS1_TENGU_FILE:#x}', PRIMS_=f'{M8K_PRIMS:#x}', HINT_HW_=str(hhw), HINT_H_=str(len(m8k_hint())),
                     SPAN_L_='{' + ','.join(str(l) for l, r in M8K_SPANS) + '}', SPAN_R_='{' + ','.join(str(r) for l, r in M8K_SPANS) + '}',
                     INK_=f'{M8K_INK:#x}', HINT_CLUT_='{' + ','.join(f'{c:#x}' for c in M8K_HINT_CLUT) + '}', TENGU_=str(M8K_TENGU),
                     SELECT_=f'{M8K_SELECT:#x}', SOUND_=f'{M8K_SOUND:#x}', PX_SAT_=str(ws), PX_PS_=str(wp), GAP_=str(M8K_TICK_GAP),
                     BLINK_ON_=str(M8K_BLINK[0]), BLINK_OFF_=str(M8K_BLINK[1]), HINT_Y_=str(M8K_HINT_XY[1]),
                     HINT_X_=str(M8K_HINT_XY[0]), HINT_PX_=str(hpx)).items():
        src = src.replace('@' + k[:-1] + '@', v)
    assert '@' not in src
    data, sy = m7_compile([('ticker.c', src)], 'ticker', M8K_BASE,
                          dict(render_rest=0x800F96C0, add_prim=0x800D313C, load_image=0x800D3E84, sound_centred=0x800FD100),
                          'tk_frame', workdir and os.path.join(workdir, 'm8k_ticker'))
    assert sy['tk_frame'] == M8K_BASE and M8K_BASE + len(data) <= M8K_PRIMS, 'ticker overlay'
    out = bytearray(data)
    def put(sym, words, fmt):
        o = sy[sym] - M8K_BASE; b = struct.pack(f'<{len(words)}{fmt}', *words); out[o:o + len(b)] = b
    put('ent_ps', ent_ps, 'I'); put('ent_sat', ent_sat, 'I')
    put('img_ps', ps, 'H'); put('img_sat', sa, 'H'); put('img_hint', hint, 'H')
    return bytes(out), sy

def m8k_demo_tail(demo_len, lba, size, workdir=None):
    """What M8t4 appends to DEMO.BIN (`demo_len` bytes so far): padding, the ticker overlay's virtual entry, tk_load.
    Returns (bytes, info)."""
    base = Ps1.OVL_BASE; out = bytearray()
    while (demo_len + len(out)) % 4: out.append(0)
    while (base + demo_len + len(out) - PS1_FILETAB) % 12: out += bytes(4)
    vf_off = demo_len + len(out); vf = (base + vf_off - PS1_FILETAB) // 12
    out += struct.pack('<3I', lba, size, 5)
    while (demo_len + len(out)) % 16: out.append(0)
    code_base = base + demo_len + len(out)
    data, sy = m7_compile([('tk_load.c', M8K_STUB_C.replace('TK_BASE_', f'{M8K_BASE:#x}'))], 'tkload', code_base,
                          dict(M8K_STUB_SYMS, tk_vf=vf), 'tk_load', workdir and os.path.join(workdir, 'm8k_stub'))
    assert sy['tk_load'] == code_base
    out += data
    return bytes(out), dict(tk_vf=vf, tk_vf_off=vf_off, tk_load=code_base)

def patch_m8_ticker(disc, lba, bank, workdir=None):
    """M8t4: the ticker overlay into the free sectors at `lba` (bank = (LBA, size) of the converted Tengu Man bank),
    tk_load appended to DEMO.BIN (after the M7 code; still in its slot), the two stage-select calls repointed."""
    rd = lambda a: disc.exe_words(a, 1)[0]
    assert fp(rd(M8K_INIT_CALL)) == M8K_INIT_FP and fp(rd(M8K_RENDER_CALL)) == M8K_RENDER_FP, 'stage-select calls'
    e28 = disc.file_entry(PS1_TENGU_FILE)
    ovl, sy = m8k_overlay(e28[:2], bank, workdir)
    nsec = place_file(disc, lba, ovl)
    e = disc.file_entry(PS1_DEMO_FILE)
    demo = disc.read(e[0], e[1])
    tail, info = m8k_demo_tail(len(demo), lba, len(ovl), workdir)
    demo = demo + tail
    assert len(demo) <= PS1_DEMO_SLOT * 2048, f'DEMO.BIN grows to {len(demo):#x}'
    disc.write(e[0], 0, demo + bytes(-len(demo) % 2048))
    disc.set_file_entry(PS1_DEMO_FILE, e[0], len(demo))
    rl, ro, ext, sz = disc.iso_record('OVL/DEMO.BIN')
    disc.write(rl, ro + 10, struct.pack('<I', len(demo)) + struct.pack('>I', len(demo)))
    disc.exe_write_words(M8K_INIT_CALL, [3 << 26 | (info['tk_load'] >> 2) & 0x3FFFFFF])
    disc.exe_write_words(M8K_RENDER_CALL, [3 << 26 | (M8K_BASE >> 2) & 0x3FFFFFF])
    info.update(tk_lba=lba, tk_size=len(ovl), tk_sectors=nsec, tk_demo_size=len(demo))
    return info

def sat_ill_image(n):
    """Official illustration n (BONUS/ILLnn.PAC): (256 BGR555 colours, 256x256 index array). Chunk 9 = 1024-colour CRAM
    image whose bank 2 (entries 512-767) holds the picture's colours; chunk 0x102 = 256 characters of 16x16 8-bpp pixels
    (linear, 16 bytes per row) in a 16 x 16 grid."""
    import numpy as np
    ch = {t: b for t, o, b in unpac(os.path.join(SAT_FILES, 'BONUS', f'ILL{n:02d}.PAC'))}
    pal = struct.unpack('>256H', ch[9][0x400:0x600])
    px = np.frombuffer(ch[0x102], np.uint8).reshape(16, 16, 16, 16).transpose(0, 2, 1, 3).reshape(256, 256)
    return pal, px

# ----------------------------------------------------------------- M7: Bonus Mode
# PS1 title (02 §PS1 title menu): front-end loop 0x800FEA40, mode 1 = title with sub-states 0x80137930[0x801CF844]:
# 0 setup 0x800FEBEC (DEMO overlay n = 0, LABO.PAC, music 0x10), 2 menu 0x800FEF94 (cursor 0x801D284C), 4 = fade out ->
# continue 0x8011F0A8(1) / options 0x8011DC78 -> menu again. Menu items are P4 type 0x0B title objects (handler
# 0x801348E4; records 0x801527C4, 8 bytes: set, palette add, step, +0x26, x, y; cursor positions 0x801527B4), all
# frames from LABO.PAC sprite set 0x70, which already holds the Saturn's BONUS MODE frame (step 11 = frame 20).
PS1_DEMO_FILE = 8                    # OVL/DEMO.BIN (title overlay n = 0) at LBA 126122; STAGE00.BIN follows at 126131
PS1_DEMO_SLOT = 9                    # sectors of DEMO.BIN's slot (0x4800 bytes; the original is 0x41C8)
PS1_DEMO_MD5 = '1bfa7492b3fc60f97a1aac7a7c492bbf'
M7_MAGIC = b'MM8R-M7\0'
M7_BONUS_BASE = 0x801E4690           # bonus overlay, loaded by the title hook above the resident DEMO overlay. M7e: moved up
                                     # from 0x801E0000 (M7a-M7d) so that the music virtual entries 0x801E4658-0x801E4687
                                     # (patch_music_hook, one vf0 for every overlay) stay free: the sound test plays music
                                     # 0x47 / 0x49 (PBGM47 / PBGM49) through them; bonus_main copies them there
M7E_MUSIC_VF = 0x801E4658            # = PS1_FILETAB + 12 * (vf0 + 0x46); entries for music 0x46-0x49 (checked in patch_m7a)
PS1_TITLE_RECS, PS1_TITLE_CURPOS, PS1_TITLE_GROUPS = 0x801527C4, 0x801527B4, 0x80137968
SAT_TITLE_RECS, SAT_TITLE_CURPOS = 0x295714, 0x295704       # the same tables on the Saturn (P4 0x0B = 0x26C564)
M7_TITLE_CODE = [                    # (address, fingerprint of the original word, change): the menu gets a 4th item
    (0x800FF024, '6f2dca0550c3', 4),       # confirm: cursor 1..3 -> sub-state 4 (slti v0, v0, 3 -> immediate 4)
    (0x800FF060, '51b2bf988493', 3),       # Down wraps after item 3 (slti v0, v0, 2 -> immediate 3)
    (0x800FF0B8, '3f66bfbb7b6b', 3),       # Up from item 0 goes to item 3 (ori v0, zero, 2 -> immediate 3)
    (0x80134CF4, 'e1c66a0f7c87', 0x13),    # the menu box spawns items 14..18 (slti v0, s0, 0x12 -> immediate 0x13)
    (0x800FF1B0, 'a74c827d0100', None),    # jal 0x8011F0A8 (continue screen) -> j title_hook (DEMO overlay)
    (0x800FEFD4, 'f2cd1272b0f6', 'title_confirm'),  # jal 0x800FD100 (confirm sound 0x15A) -> jal title_confirm (M7g)
]
M7_TITLE_HOOK_S = r'''
    .set noreorder
    .section .text.title_hook,"ax",@progbits
    .globl title_hook
# Jumped to from sub-state 4 (0x800FF1B0, a0 = 1 set in the delay slot) once the screen has faded out, inside that
# function's stack frame. Otherwise the continue screen as before and back into the original code. Cursor 3 = BONUS
# MODE: run it, then return from the title's frame function 0x800FEB44 itself (its frame: 0x20 bytes, ra at +0x18,
# no saved registers; sub-state 4's: 0x18 bytes, ra only), so this frame is not drawn: drawing the old title objects
# with no fade running flashed the title for one frame (M7a test). Sub-state 0 sets the title up next frame and fades
# it in, exactly as at boot.
title_hook:
    lui   $t0, 0x801d
    lb    $t0, 0x284c($t0)
    li    $t1, 3
    beq   $t0, $t1, 1f
    nop
    jal   cont_screen
    nop
    j     title_rebuild
    nop
1:  jal   bonus_enter
    nop
    lw    $ra, 0x30($sp)        # 0x18 (sub-state 4 frame) + 0x18 (ra in the title frame function's frame)
    nop
    jr    $ra
    addiu $sp, $sp, 0x38

# M7g: the Saturn title (0x20989A) shows the bonus menu's hidden ANIMATION item when BONUS MODE is confirmed
# while Start + L + R are held (held pad & 0x888; Start may be the confirming press), by setting 0x0602DABC (= PS1
# 0x801B29A0) to 1, else 0. Here: called instead of the confirm sound (a0 = 0x15A), cursor 3 = BONUS MODE, held pad
# 0x801B2954 & (Start 0x800 | L1 4 | R1 8) (L1 + R1 + Start), then the sound as before.
    .globl title_confirm
title_confirm:
    lui   $t0, 0x801d
    lb    $t0, 0x284c($t0)
    li    $t1, 3
    bne   $t0, $t1, 2f
    lui   $t2, 0x801b
    lhu   $t0, 0x2954($t2)
    li    $t1, 0x80c
    and   $t0, $t0, $t1
    xor   $t0, $t0, $t1
    sltiu $t0, $t0, 1
    sb    $t0, 0x29a0($t2)
2:  j     confirm_sound
    nop

# Bonus exit timing (chase the PS1's 9 extra black frames): coming back from Bonus Mode, sub-state 0
# would reload DEMO.BIN, which is still resident and byte-identical to the disc copy (Mednafen states at the bonus
# menu; the bonus code never writes 0x801D8000-0x801DFFFF). bonus_enter points sub-state 0's `jal 0x800FC648` (overlay
# loader, 0x800FEC18) here for that one call; this does the loader's bookkeeping without the read (0x801C738C = n,
# 0x801C73D0 = 0x80137658[n]), puts the original jal back and flushes the instruction cache.
    .globl title_skip_load
title_skip_load:
    lui   $t0, 0x801c
    sb    $a0, 0x738c($t0)
    sll   $t1, $a0, 2
    lui   $t2, 0x8013
    addu  $t2, $t2, $t1
    lw    $t2, 0x7658($t2)
    nop
    sw    $t2, 0x73d0($t0)
    lui   $t0, 0x8010
    lui   $t1, 0x0c03
    ori   $t1, $t1, 0xf192      # jal 0x800FC648
    sw    $t1, -0x13e8($t0)     # 0x800FEC18
    j     flush_cache
    nop
'''
M7_BONUS_ENTER_C = r'''
typedef signed char s8;
extern char bonus_vf[];                       /* linker symbol = virtual file number of the bonus overlay */
extern void file_load(int idx, void *dst);    /* 0x800FB7A4: start reading file idx to dst */
extern int load_done(void);                   /* 0x800FC1E0: 1 when the read has finished */
extern void yield_(int n);                    /* 0x800F7ECC: wait n frames (task switch) */
extern void flush_cache(void);                /* 0x800D78EC: BIOS FlushCache */
extern void title_skip_load(int n);
#define CURSOR   (*(volatile s8 *)0x801D284C)
#define SUBSTATE (*(volatile s8 *)0x801CF844)
#define MENUFLAG (*(volatile s8 *)0x801D1848)
void bonus_enter(void)
{
    file_load((int)bonus_vf, (void *)BONUS_BASE);
    while (!load_done()) yield_(1);
    ((void (*)(void))BONUS_BASE)();
    /* DEMO.BIN is still resident and unchanged: skip its reload in sub-state 0 (title_skip_load puts the jal back) */
    *(volatile unsigned *)0x800FEC18 = 0x0C000000 | (((unsigned)title_skip_load >> 2) & 0x3FFFFFF);
    flush_cache();
    yield_(3);           /* black 3 frames more: the gap to the title then matches the Saturn's 186 frames (Mednafen) */
    CURSOR = 3;          /* back to the title: sub-state 0 sets the title up again with the menu open */
    MENUFLAG = 0;
    SUBSTATE = 0;
}
'''
M7_SYMS = dict(cont_screen=0x8011F0A8, title_rebuild=0x800FF1B8, file_load=0x800FB7A4, confirm_sound=0x800FD100,
               load_done=0x800FC1E0, yield_=0x800F7ECC, stop_bgm=0x800FCD84, flush_cache=0x800D78EC)

def sat_title_records(first=0, n=21):
    """Saturn title-object records (0x295714, big-endian): [(set, pal_add, step, f26, x, y)]."""
    sat = Sat()
    return [struct.unpack('>BBBBhh', sat.raw(SAT_TITLE_RECS + 8 * v, 8)) for v in range(first, first + n)]

def m7_compile(src_text, name, base, symbols, entry, workdir=None):
    import tempfile
    wd = workdir or tempfile.mkdtemp(prefix='mm8m7_')
    os.makedirs(wd, exist_ok=True)
    srcs = []
    for k, (fn, txt) in enumerate(src_text):
        p = os.path.join(wd, fn); open(p, 'w').write(txt); srcs.append(p)
    data, sy = compile_c(srcs, base, symbols, entry_first=entry, workdir=wd)
    bad = check_r3000(data, base)
    assert not bad, bad
    # nothing may live past the file (.bss is not loaded: the overlay loader reads the file size only)
    past = [k for k, v in sy.items() if base + len(data) <= v < base + 0x100000 and k not in symbols]
    assert not past, f'symbols outside the loaded image (.bss?): {past}'
    return data, sy

@_memo
def build_m7_overlays(demo, workdir=None):
    """M7a: (new DEMO.BIN, BONUS overlay, info). DEMO.BIN (the title overlay, resident while the title runs) gets
    marker + one virtual file entry (the bonus overlay; the caller fills in its LBA/size) + the title hook and
    bonus_enter; it must still fit its 9-sector slot. The bonus overlay is linked at M7_BONUS_BASE."""
    assert md5_bytes(demo) == PS1_DEMO_MD5, 'unexpected DEMO.BIN (already patched?)'
    base = Ps1.OVL_BASE
    out = bytearray(demo)
    while len(out) % 4: out.append(0)
    out += M7_MAGIC
    while (base + len(out) - PS1_FILETAB) % 12: out += bytes(4)
    vf_off = len(out); vf = (base + vf_off - PS1_FILETAB) // 12
    out += bytes(12)
    while len(out) % 16: out.append(0)
    code_base = base + len(out)
    syms = dict(M7_SYMS, bonus_vf=vf)
    wd = workdir and os.path.join(workdir, 'm7_demo')
    data, sy = m7_compile([('title_hook.S', M7_TITLE_HOOK_S), ('bonus_enter.c', M7_BONUS_ENTER_C.replace('BONUS_BASE', f'{M7_BONUS_BASE:#x}'))],
                          'demo', code_base, syms, 'title_hook', wd)
    assert sy['title_hook'] == code_base
    out += data
    assert len(out) <= PS1_DEMO_SLOT * 2048, f'DEMO.BIN grows to {len(out):#x}'
    bonus, binfo = build_m7b_bonus(workdir)            # M7b: the Saturn bonus menu, lifted
    return out, bonus, dict(demo_size=len(out), vf=vf, vf_off=vf_off, title_hook=sy['title_hook'], title_confirm=sy['title_confirm'],
                            title_skip_load=sy['title_skip_load'], bonus_enter=sy['bonus_enter'], **binfo)

def patch_title_menu(disc, title_hook, title_confirm):
    """EXE: the title menu gets BONUS MODE as a 4th item, laid out as on the Saturn (cursor positions and item records
    14-18 from the Saturn tables; item 18 = set 0x70 step 11 = the BONUS MODE frame LABO.PAC already holds); cursor 3
    confirmed -> sub-state 4 -> title_hook. The PS1's own prompt line (record 13, PS1 buttons) is kept."""
    rd = lambda a: disc.exe_words(a, 1)[0]
    # title_hook unwinds sub-state 4's and the title frame function's stack frames: check their prologues
    assert fp([rd(0x800FEB4C), rd(0x800FEB50), rd(0x800FF150), rd(0x800FF158)]) == '767b38c7fa35'
    for a, ofp, new in M7_TITLE_CODE:
        old = rd(a)
        assert fp(old) == ofp, f'{a:#x}: unexpected code'
        if new == 'title_confirm': new = 3 << 26 | (title_confirm >> 2) & 0x3FFFFFF      # jal
        elif new is None: new = 2 << 26 | (title_hook >> 2) & 0x3FFFFFF                  # j
        else: new = old & 0xFFFF0000 | new                                                # new immediate
        disc.exe_write_words(a, [new])
    sat = Sat()
    cur = struct.unpack('>8h', sat.raw(SAT_TITLE_CURPOS, 16))
    assert fp(struct.unpack('<8h', disc.exe_read(PS1_TITLE_CURPOS, 16))) == '510b41516904'
    disc.exe_write(PS1_TITLE_CURPOS, struct.pack('<8h', *cur))
    recs = sat_title_records(14, 5)
    disc.exe_write(PS1_TITLE_RECS + 8 * 14, b''.join(struct.pack('<BBBBhh', *r) for r in recs))
    g = disc.exe_read(PS1_TITLE_GROUPS, 8)
    assert fp(list(g)) == 'cb118e431aec'
    disc.exe_write(PS1_TITLE_GROUPS + 7, bytes([18]))   # group 3 (menu shown at once, e.g. back from Bonus Mode): 12..18
    return dict(title_cursor=cur, title_recs=recs)

def patch_m7a(disc, lba, workdir=None, vf0=None):
    """M7a: DEMO.BIN rebuilt in its own slot (file-table entry 8 + ISO record: new size), the bonus overlay written to
    the free sectors at `lba` (reached through the virtual entry in DEMO.BIN), title-menu EXE patches. Returns info."""
    e = disc.file_entry(PS1_DEMO_FILE)
    demo = disc.read(e[0], e[1])
    new, bonus, info = build_m7_overlays(demo, workdir)
    slabo = m7b_slabo_pac(); slabo_lba = lba + (len(bonus) + 2047) // 2048
    bonus = bytearray(bonus); bonus[info['slabo_vf_off']:info['slabo_vf_off'] + 12] = struct.pack('<3I', slabo_lba, len(slabo), 0x14)
    ill_lba0 = slabo_lba + (len(slabo) + 2047) // 2048; ill_sec = (M7C_ILL_BYTES + 2047) // 2048
    for n in range(M7C_ILL_COUNT):                  # M7c: ILL00-29 after SLABO (reached through the overlay's entries)
        o = info['ill_vf_off'] + 12 * n
        bonus[o:o + 12] = struct.pack('<3I', ill_lba0 + ill_sec * n, M7C_ILL_BYTES, 0x14)
    lfiles, _ = m7d_letter()                        # M7d: the LETTER files after ILL29
    let_lba = ill_lba0 + ill_sec * M7C_ILL_COUNT; info['letter_lba'] = let_lba; lb = let_lba
    for k, f in enumerate(lfiles):
        o = info['letter_vf_off'] + 12 * k
        bonus[o:o + 12] = struct.pack('<3I', lb, len(f), 0x14); lb += (len(f) + 2047) // 2048
    # M7e: the music 0x46-0x49 entries, copied from the STAGE09 overlay (already placed by build_image; its entries sit at
    # the play_bgm hook's address M7E_MUSIC_VF, which bonus_main fills with these at entry)
    if vf0 is None: vf0 = disc.exe_words(PS1_BGM_FILE_SITES[0] + 12, 1)[0] & 0xFFFF      # once patch_music_hook has run
    assert PS1_FILETAB + 12 * (vf0 + PS1_BGM_FIRST_NEW) == M7E_MUSIC_VF
    l9, s9, _ = disc.file_entry(PS1_STAGE09_FILE); o9 = disc.read(l9, s9)[M7E_MUSIC_VF - Ps1.OVL_BASE:][:48]
    assert len(o9) == 48 and all(struct.unpack_from('<3I', o9, 12 * k)[2] == 4 for k in range(4))
    bonus[info['music_vf_off']:info['music_vf_off'] + 48] = o9
    bonus = bytes(bonus)
    new[info['vf_off']:info['vf_off'] + 12] = struct.pack('<3I', lba, len(bonus), 5)
    assert disc.read(e[0] + 0, PS1_DEMO_SLOT * 2048)[e[1]:] == bytes(PS1_DEMO_SLOT * 2048 - e[1])
    disc.write(e[0], 0, bytes(new))
    disc.set_file_entry(PS1_DEMO_FILE, e[0], len(new))
    rl, ro, ext, sz = disc.iso_record('OVL/DEMO.BIN')
    assert ext == e[0]
    disc.write(rl, ro + 10, struct.pack('<I', len(new)) + struct.pack('>I', len(new)))
    info['bonus_lba'] = lba; info['bonus_sectors'] = place_file(disc, lba, bonus)
    assert lba + info['bonus_sectors'] == slabo_lba
    info['slabo_lba'] = slabo_lba; info['slabo_size'] = len(slabo); info['slabo_sectors'] = place_file(disc, slabo_lba, slabo)
    assert slabo_lba + info['slabo_sectors'] == ill_lba0
    info['ill_lba'] = ill_lba0; info['ill_sectors'] = 0
    for n in range(M7C_ILL_COUNT):
        f = m7c_ill_file(n); assert len(f) == M7C_ILL_BYTES
        info['ill_sectors'] += place_file(disc, ill_lba0 + ill_sec * n, f)
    info['letter_sectors'] = 0
    for f in lfiles: info['letter_sectors'] += place_file(disc, let_lba + info['letter_sectors'], f)
    info['m7_sectors'] = info['bonus_sectors'] + info['slabo_sectors'] + info['ill_sectors'] + info['letter_sectors']
    info.update(patch_title_menu(disc, info['title_hook'], info['title_confirm']))
    return info

# ----------------------------------------------------------------- M7b: Bonus Mode menu
# The Saturn bonus menu (02 §Saturn Bonus Mode, "M7b") is lifted from 2NDREAD with the SH-2 lifter: the menu loop
# 0x27AAD0 (state block 0x2C2DF4, states 0x29B2D0: 0 init 0x27AB98, 1 input 0x27AC50, 2 sub-screen 0x27AD5E, 3 exit
# 0x27AE6A, 4 enter sub-screen 0x27AE9A), the object spawner 0x2761DC (lists 0x296694) and the P4 handlers 0x40/0x41
# (0x275E00 / 0x275E58, states 0x2966AC). The menu runs on the PS1 engine exactly like the PS1's own continue screen
# (Saturn 0x279978 <-> PS1 0x8011F0A8 call for call), so its engine calls map to the PS1 functions below. Sub-screens
# (postcards 0x27B1C0, illustrations 0x27B440, sound/voice test 0x27B0F8, animation 0x27AFFA) are external functions,
# replaced by stubs until M7c-M7e.
# Saturn engine function -> PS1, verified by aligned code: continue loop 0x279978 / 0x8011F0A8 (state fn,
# 0x060140AA, 0x00210DA4, 0x00210E4C, 0x06010CA8, 0x06011BC8, sound, 0x06008160(1)); continue init 0x2799D0 /
# 0x8011F184 (0x060147D8, 0x00210DFA x2 on the layer structs +0x30/+0x60, 0x06012E70(2)); title setup 0x2095xx /
# 0x800FEBEC (stream LABO.PAC, 0x0600C0A8(0x10) / 0x800FCA4C(0x10), 0x06014D64 = 0x801061DC(dst, src, n) for the
# screens and palettes, CRAM dirty 0x0602FEEC = 0x8016CA90, 0x060147D8, E20C/E20A = 0x8016EC10/0x8016EC0C); title
# demo end 0x209C64 / 0x800FF528 (stop BGM 0x0600C2B8 = 0x800FCD84, then 0x0600C3CE = 0x800FCDE4 = key off the SE voices).
# Bodies compared too: 0x060140AA / 0x80104C04 clear 13 bytes (0x0602E20E / 0x8015C978); 0x00210DFA / 0x8011EFE0 set the
# same layer fields; 0x00210DA4 / 0x8011EF28 = P4 loop (counter 0x0603622F / 0x801B291C, +0x34/+0x36 <- X/Y).
M7B_FRONTEND_HELPERS = {0x060140AA: 0x80104C04, 0x00210E4C: 0x8011F024, 0x06010CA8: 0x800F88FC, 0x06011BC8: 0x800F96C0,
                        0x060147D8: 0x801058F0, 0x00210DFA: 0x8011EFE0, 0x06014D64: 0x801061DC, 0x0600C0A8: 0x800FCA4C,
                        0x0600C3CE: 0x800FCDE4,
                        0x060081B4: 0x800F7F58}   # M7e: end task n (02 §Engine helpers, 0x06012E70 row)
M7B_P4_LOOP = (0x00210DA4, 0x8011EF28)       # replaced by a copy with the 66-entry table (types 0x40/0x41 added)
M7B_P4_TABLE, M7B_P4_LOOP_FP = 0x8015029C, 'a244f619c456'

def m7b_p4_loop_asm():
    """Top-level asm for the generated bonus C: ps1_p4_loop = the PS1 P4 loop 0x8011EF28 copied from the user's disc (up
    to its `jr ra` + delay slot), with the lui/addiu pair that loads the P4 table 0x8015029C pointed at m7b_p4tab. The
    function has only PC-relative branches and absolute data addresses, so the copy runs anywhere."""
    e = Ps1().exe; a = M7B_P4_LOOP[1]; words = []
    while not words or words[-2:-1] != [0x03E00008]:          # jr ra, then its delay slot
        words.append(e.u32(a + 4 * len(words))); assert len(words) < 64
    k = next(i for i in range(len(words) - 1) if words[i] >> 26 == 0x0F and words[i + 1] >> 26 == 0x09
             and Ps1.lui_addiu(e, a + 4 * i) == M7B_P4_TABLE)
    r = words[k] >> 16 & 31
    assert fp(words[:k] + words[k + 2:]) == M7B_P4_LOOP_FP, 'unexpected code at 0x8011EF28'
    assert not any(w >> 26 in (2, 3) for w in words), 'absolute jump in the copied function'
    body = [f'.word {w:#010x}' for w in words[:k]] + [f'lui ${r}, %hi(m7b_p4tab)', f'addiu ${r}, ${r}, %lo(m7b_p4tab)'] + \
           [f'.word {w:#010x}' for w in words[k + 2:]]
    return ('__asm__(".section .text.ps1_p4_loop,\\"ax\\",@progbits\\n.set push\\n.set noreorder\\n.set noat\\n.globl ps1_p4_loop\\n'
            'ps1_p4_loop:\\n' + '\\n'.join(body) + '\\n.set pop\\n.previous");')
# Saturn pad words (verified, Mednafen: key held, save state): 0x0602DA40 held, +2 previous, +4 pressed this frame (pad 2:
# +6..+0xA; 0x0602DA4A <-> 0x801B2960 in globals_map). Bits: Up 0x1000, Down 0x2000, Left 0x4000, Right 0x8000, Start 0x800,
# A 0x400, C 0x200, B 0x100, X 0x40, Y 0x20, Z 0x10, L 8, R 0x80. PS1 0x801B2954 held / 56 previous / 58 pressed: Up 0x1000,
# Right 0x2000, Down 0x4000, Left 0x8000, Start 0x800, x 0x40, circle 0x20, triangle 0x10, L1 4, R1 8. Decision:
# x = confirm (Saturn A), triangle = cancel (Saturn B); circle and square do nothing on the bonus screens.
M7B_PAD_MAP = [(0x1000, 0x1000), (0x4000, 0x2000), (0x8000, 0x4000), (0x2000, 0x8000), (0x0800, 0x0800),
               (0x0040, 0x0400), (0x0010, 0x0100), (0x0004, 0x0008), (0x0008, 0x0080)]   # (PS1 bit, Saturn bit)
# Saturn RAM -> PS1 RAM with the same layout (any access size): PAC load buffers by chunk type (Saturn dest table
# 0x0602C3B0 read from a Mednafen state <-> PS1 0x80137850), live screens, working palettes, layer structs, P4 pool.
M7B_MEM = [(0x0602FEF0, 0x0602FF80, 0x801D2914, 'BG layer structs 0-2 (camera)'),
           (0x0602EACC, 0x0602FACC, 0x80158F64, 'working palettes (CRAM shadow)'),
           (0x0029E5A0, 0x0029F5A0, 0x8015A064, 'chunk 9 load buffer'),
           (0x0029F5A0, 0x0029F6E0, 0x801AE840, 'chunk 0xB load buffer'),
           (0x002A26A0, 0x002C06A0, 0x80190040, 'chunk 3 load buffer'),
           (0x06036438, 0x06054438, 0x80171C3C, 'live screens'),
           (0x06033EE0, 0x06035EE1, 0x801CF848, 'P4 pool (+ end pointer: the spawner loops to 0x06035EE0)'),
           (0x0602DF08, 0x0602E108, 0x80170348, 'chunk 7 (sprite-set bases)'),
           (0x0602E108, 0x0602E208, 0x80159F64, 'chunk 8'),
           (0x0602E20A, 0x0602E20C, 0x8016EC0C, 'BG X copy'), (0x0602E20C, 0x0602E20E, 0x8016EC10, 'BG Y copy'),
           (0x0602FEEC, 0x0602FEED, 0x8016CA90, 'palette dirty flag'),
           (0x260C0000, 0x260C3C00, 0x801F4000, 'LETTER palettes (M7d; reserved)'),
           (0x26080000, 0x26091000, 0x80048000, 'ILLnn.PAC whole-file load buffer (M7c) = PS1 movie buffer area (free outside movies)'),
           (0x060733CE, 0x060733CF, 0x801D28F8, 'M7e: music bank loaded by the sound driver = PS1 music number (both play_bgm and 0x0600C85E load file table[n])')]
# Saturn-only variables -> bonus_ram (in the overlay, zeroed at each load): menu / illustration state blocks (2NDREAD
# bss), the Saturn-format pad words, the VDP2 register shadow (display enable bit; no PS1 counterpart needed: the
# screen is faded out whenever the code touches it).
M7B_RAM = [(0x002C2DF4, 0x002C2E20, 'menu/illustration state blocks'), (0x0602DA40, 0x0602DA4C, 'Saturn pad words'),
           (0x060673B4, 0x060674C4, 'VDP2 register shadow (M7c: sprite colour calculation read by h_06010ca8)'),
           (0x0602D91C, 0x0602D920, 'colour offset enable'),
           (0x0602FEED, 0x0602FEF0, 'VDP update flags next to the palette dirty flag (Saturn only; M7c)'),
           (0x0605F070, 0x0605F080, 'PAC dest-table pointers (M7c: the glue loads ILL files itself)'),
           (0x0602C3B0, 0x0602C460, 'Saturn PAC dest tables (only their addresses are stored, M7c)'),
           (0x060733C4, 0x060733C8, 'M7e: Saturn CD-DA status word (0x06017804 writes it; the PS1 front end plays no CD audio)')]
M7B_DATA = [(0x002964EC, 0x00296700), (0x0029B2D0, 0x0029B300), (0x0029B490, 0x0029B4A4),   # P4 data incl. the y table 0x2966DA[6 * (variant >> 4) + cursor]
            (0x0027CDAC, 0x0027CDCC + 4 * 0xA6)]   # M7e: sound-test music numbers (32 bytes) + voice-test entries (u16 bank, u16 sound; 0xA6 = the player's bound)
M7E_HALF_DATA = [(0x0027CDCC, 2 * 0xA6)]   # u16 halfwords swapped to LE
M7B_PTR_TABLES = [(0x00296694, 6)]           # spawn-list pointers (lists inside M7B_DATA)
M7B_CODE_TABLES = {0x0029B2D0: 5, 0x002966AC: 9, 0x0029B490: 5}
M7B_WORD_DATA = [(0x0029B2F8, 2), (0x0029B2E4, 5)]   # u32 auto-repeat counters; movie-name pointers (ROCK8_0-4.CPK)
M7C_ILL_NAMES, M7C_ILL_COUNT, M7C_ILL_BYTES = 0x0029B308, 30, 2048 + 65536    # M7c: ILLnn.PAC names; PS1 file = chunk 9 + texture
M7B_GLUE_C = r'''
/* Hand-written glue for the lifted bonus menu (M7b). */
extern void ps1_p4_exe_table(void);
extern void ps1_yield(u32 n);                       /* 0x800F7ECC: wait n frames */
extern void ps1_stream(u32 file);                   /* 0x800FB9C0: stream a PAC through the dest table at 0x8016D294 */
extern u32 ps1_load_done(void);                     /* 0x800FC1E0: nonzero when the read has finished */
extern char bonus_slabo_vf[];                       /* linker symbol = virtual file number of the converted SLABO.PAC */
extern void bonus_p4_40(u32), bonus_p4_41(u32), bonus_spawn(u32);
void (*m7b_p4tab[66])(u32) __attribute__((section(".data"))) = {0};   /* read by ps1_p4_loop (below) */
u8 bonus_ram[BONUS_RAM_SIZE] __attribute__((aligned(4), section(".data"))) = {0};
static const u16 padmap[][2] = PAD_MAP;
#define ILL_BUF_ 0x80048000                         /* movie buffer area: ILL / LETTER file loads (Saturn 0x26080000) */
static u16 sat_pad(u16 v) { u32 k, r = 0; for (k = 0; k < sizeof padmap / sizeof padmap[0]; k++) if (v & padmap[k][0]) r |= padmap[k][1]; return r; }
/* Saturn 0x06008160(n) = wait n frames (PS1 0x800F7ECC); then the PS1 pad (0x801B2954 held / 56 previous / 58 pressed,
   updated once per frame) is translated into the Saturn pad words the lifted code reads. */
__attribute__((noinline, noipa)) u32 h_06008160(u32 n, u32 b, u32 c, u32 d)
{
  volatile u16 *pp = (volatile u16 *)0x801B2954, *sp = (volatile u16 *)(bonus_ram + PAD_OFF);
  ps1_yield(n);
  sp[0] = sat_pad(pp[0]); sp[1] = sat_pad(pp[1]); sp[2] = sat_pad(pp[2]);
  return 0;
}
/* Saturn 0x00210DA4 = PS1 0x8011EF28: run every P4 object. ps1_p4_loop = a copy of the PS1 function taken from the
   user's disc at build time (m7b_p4_loop_asm), with its table pointer (the EXE's 64 entries at 0x8015029C) moved to
   m7b_p4tab: the EXE's 64 + the Saturn-only types 0x40/0x41. */
extern void ps1_p4_loop(void);
__attribute__((noinline, noipa)) u32 h_00210da4(u32 a, u32 b, u32 c, u32 d) { ps1_p4_loop(); return 0; }
/* Saturn 0x27B3D2(n) = load bonus PAC n (0 SLABO.PAC, 1 LETTER.PAC) through the stage dest tables and wait. PS1: the
   converted SLABO.PAC through a virtual file entry, like the PS1 title loads LABO.PAC (same dest tables: 0x8016D294 =
   0x80137850, 0x801B1240 = 0x801378A8 = Saturn 0x0605F070 / 0x0605F07C); LETTER (M7d) = letter_load below. */
static void letter_load(void);
extern void ps1_draw_sync(u32);                     /* 0x800D3C5C: libgpu DrawSync */
__attribute__((noinline, noipa)) u32 h_0027b3d2(u32 n, u32 b, u32 c, u32 d)
{
  if (n == 1) { letter_load(); return 0; }
  if (n != 0) return 0;
  *(volatile u32 *)0x8016D294 = 0x80137850; *(volatile u32 *)0x801B1240 = 0x801378A8;
  ps1_stream((u32)bonus_slabo_vf);
  while (!ps1_load_done()) ps1_yield(1);
  ps1_yield(1);   /* M7d fix: like the PS1 title after LABO.PAC (0x800FEC58): the streamer's VRAM sectors go through its
                     sector buffers at 0x80011000 and the GPU queue; without this frame (and the DrawSync) the next stream
                     (music 0x14) reused those buffers before the last uploads were done, and SLABO's last 0x103 tiles (the
                     postcard shadows 605-623) kept old VRAM contents (garbage in DuckStation; Mednafen lost tiles 624-639) */
  ps1_draw_sync(0);
  return 0;
}
/* M7d: BOSS CHARACTER POSTCARD. The menu jumps to 0x27B1C0 (lifted: bonus_pc) with its state block. LETTER (m7d_letter):
   the card palettes go to 0x801F4000 (Saturn 0x260C0000), the cards' 8-bpp characters to the 8-bit texture pages 5-13
   where the converted sets 0xA2 / 0x98 point (Saturn block b -> VRAM (320 + 128 b, 0)), one rectangle per character-row
   span; the files are read one after another into the movie buffer (DrawSync before it is reused). */
extern void bonus_pc(u32);
extern char bonus_letter_vf[];
extern void ps1_draw_sync(u32);                     /* 0x800D3C5C: libgpu DrawSync */
extern void ps1_file_load(u32 file, u32 dst);       /* 0x800FB7A4 (below) */
extern void ps1_load_image(void *rect, void *src);  /* 0x800D3E84 (below) */
static const s16 letter_rect[][4] = LETTER_RECTS;
static const u32 letter_off[] = LETTER_OFFS;        /* file << 24 | offset in the file */
static void letter_load(void)
{
  u32 f, k;
  for (f = 0; f < LETTER_NFILES; f++) {
    ps1_file_load((u32)bonus_letter_vf + f, ILL_BUF_);
    while (!ps1_load_done()) ps1_yield(1);
    ps1_yield(1);                                   /* as after the SLABO stream (above) */
    if (f == 0) { volatile u32 *s = (volatile u32 *)ILL_BUF_, *d = (volatile u32 *)0x801F4000; for (k = 0; k < 30 * 512 / 4; k++) d[k] = s[k]; }
    for (k = 0; k < sizeof letter_off / sizeof letter_off[0]; k++)
      if (letter_off[k] >> 24 == f) ps1_load_image((void *)letter_rect[k], (void *)(ILL_BUF_ + (letter_off[k] & 0xFFFFFF)));
    ps1_draw_sync(0);
  }
}
__attribute__((noinline, noipa)) u32 h_0027b1c0(u32 st, u32 b, u32 c, u32 d) { bonus_pc(st); return 0; }
/* M7e: SOUND TEST / VOICE TEST. The menu jumps to 0x27B0F8 (lifted: bonus_st) with its state block; on x it tail-jumps
   to the player 0x209088 (lifted: bonus_sndplay; r4 = entry, r5 = 0 sound test / 1 voice test). The player's engine
   calls are Saturn sound-driver and CD functions; the glue below does the same on the PS1 sound code (02 §M7b "M7e").
   Busy flag: Saturn 0x06073356 = PS1 0x801CF53C (globals map; set by an async music load, cleared by task 1). */
extern void bonus_st(u32), bonus_sndplay(u32, u32);
extern void ps1_seq_close(s32);                     /* 0x800C5A44 (play_bgm closes the open sequence with it) */
extern void ps1_vab_close(s32);                     /* 0x800C699C */
extern void ps1_se_release(void);                   /* 0x800FD240: wait for bank sounds (ID >= 0x50) on voices 0x10-0x17 to end */
extern void ps1_c0bf8(u32, u32);                    /* 0x800C0BF8: called by play_bgm before a bank load, same arguments */
extern void ps1_bgm_async(u32);                     /* 0x800FCB80: play_bgm, async (task 1 opens and starts the song) */
extern void ps1_sound(u32, u32);                    /* 0x800FD100: play sound centred */
extern void ps1_task_start(u32, void (*)(void));    /* 0x800F7E08 */
extern void ps1_task_exit(void);                    /* 0x800F7F00: end the calling task */
#define SEQ_HANDLE (*(volatile s16 *)0x80170548)    /* open sequence, 0xFF = none */
#define BANK_NUM   (*(volatile u8 *)0x801D28F8)     /* loaded music number (Saturn 0x060733CE) */
#define BANK_ID    (*(volatile u8 *)0x8015E9C4)     /* loaded bank = music number + 1 (sound IDs >= 0x51 check it) */
#define SND_BUSY   (*(volatile u8 *)0x801CF53C)
static u16 st_pending __attribute__((section(".data"))) = 0;   /* Saturn 0x060733C8: the sound to play once the bank is in */
__attribute__((noinline, noipa)) u32 h_0027b0f8(u32 st, u32 b, u32 c, u32 d) { bonus_st(st); return 0; }
__attribute__((noinline, noipa)) u32 h_00209088(u32 n, u32 v, u32 c, u32 d) { bonus_sndplay(n, v); return 0; }
/* Saturn CD-DA status / stop (0x06017804 reads the CD status word into r4; 0x060171C0 / 0x06017AB8 stop CD-DA play when
   it is nonzero). Nothing in the PS1 front end plays CD audio (the title music is sequence 0x10), so the status is 0. */
__attribute__((noinline, noipa)) u32 h_06017804(u32 p, u32 b, u32 c, u32 d) { *(u16 *)p = 0; return 0; }
__attribute__((noinline, noipa)) u32 h_060171c0(u32 a, u32 b, u32 c, u32 d) { return 0; }
__attribute__((noinline, noipa)) u32 h_06017ab8(u32 a, u32 b, u32 c, u32 d) { return 0; }
/* Saturn 0x0600BEB6(0, id, 16) + 0x0600BC8C() = queue and send "play sound id" (exactly Saturn 0x0600C358, play sound
   centred = PS1 0x800FD100); used by the player for the two COMMON voice entries. */
__attribute__((noinline, noipa)) u32 h_0600beb6(u32 a, u32 id, u32 c, u32 d) { ps1_sound(id & 0xFFFF, 0); return 0; }
__attribute__((noinline, noipa)) u32 h_0600bc8c(u32 a, u32 b, u32 c, u32 d) { return 0; }
/* Saturn 0x0600C610(n) = play_bgm, ignored while a load is busy; PS1 0x800FCB80 has no such check (the game never asks
   during a load), so the glue adds the Saturn's. */
__attribute__((noinline, noipa)) u32 h_0600c610(u32 n, u32 b, u32 c, u32 d) { if (!SND_BUSY) ps1_bgm_async(n & 0xFF); return 0; }
/* Saturn 0x0600C516 = stop every song slot; the player's exit calls it only while a load is busy. Every PS1 load that sets
   the busy flag (0x800FCB80, voice_bank_task below) has already closed the sequence, so only the handle is cleared. */
__attribute__((noinline, noipa)) u32 h_0600c516(u32 a, u32 b, u32 c, u32 d) { SEQ_HANDLE = 0xFF; return 0; }
/* Saturn 0x0600A956 = abort the file read. The PS1 streamer has no abort: wait until it has finished (the menu's next
   play_bgm starts a new stream). */
__attribute__((noinline, noipa)) u32 h_0600a956(u32 a, u32 b, u32 c, u32 d) { while (!ps1_load_done()) ps1_yield(1); return 0; }
/* Saturn task 0x0600CAD8 (started by 0x0600C85E): wait for the bank, clear the busy flag, play the sound, end. */
static void voice_bank_task(void)
{
  while (!ps1_load_done()) ps1_yield(1);
  SND_BUSY = 0;
  ps1_sound(st_pending, 0);
  ps1_task_exit();
}
/* Saturn 0x0600C85E(bank, sound) = play a sound of music bank `bank`: ignored while busy; stop the sound effects; if the
   bank is loaded, play the sound at once; otherwise stop the songs, load the bank's sound data (not its song) in the
   background (busy flag, task 1) and play the sound when it is in. PS1: the bank load of play_bgm 0x800FCB80 (close the
   sequence, 0x800FD240, 0x800C0BF8, close the VAB, music number / bank ID, stream PBGMnn through the sound dest table
   0x801378BC) with voice_bank_task in place of the song start. The sound effects were already keyed off by the player
   (0x0600C3CE = 0x800FCDE4). */
__attribute__((noinline, noipa)) u32 h_0600c85e(u32 bank, u32 snd, u32 c, u32 d)
{
  bank &= 0xFF;
  if (SND_BUSY) return 0;
  st_pending = snd & 0xFFFF;
  if (BANK_NUM == bank) { ps1_sound(st_pending, 0); return 0; }
  if (SEQ_HANDLE != 0xFF) { ps1_seq_close(SEQ_HANDLE); SEQ_HANDLE = 0xFF; }
  ps1_se_release();
  ps1_c0bf8(0, 0x1FFFF);
  BANK_NUM = bank; BANK_ID = bank + 1;
  ps1_vab_close(*(volatile s16 *)0x801C3358);
  *(volatile u32 *)0x8016D294 = 0x801378BC;
  ps1_stream(((volatile u32 *)0x80137714)[bank]);
  SND_BUSY = 1;
  ps1_task_start(1, voice_bank_task);
  return 0;
}
__attribute__((noinline, noipa)) /* Saturn 0x06017FFC(name) = play a Cinepak movie (blocking). The ANIMATION screen passes 0x29B2E4[cursor] = the names
   ROCK8_0-4.CPK at 0x27BE7C + 12 n; the PS1 plays the same movies as file-table entries 3 + n with 0x800FD444. */
/* Skip rule: the Saturn player stops on Start only (Mednafen: A, B, C do nothing); the PS1 player takes the button mask
   from its movie table (0x801378F4: Start/x for 4-7 at 0x800FD4C0, any button for the opening at 0x800FD4C4). For the
   bonus screen both immediates become Start (ori s2, zero, 0x800) while the movie plays, then are restored; the
   instruction cache is flushed after each change (BIOS FlushCache wrapper 0x800D78EC). */
extern void ps1_movie(u32 entry);
extern void ps1_flush_cache(void);
#define SKIP_ANY  ((volatile u32 *)0x800FD4C0)
#define SKIP_OPEN ((volatile u32 *)0x800FD4C4)
__attribute__((noinline, noipa)) u32 h_06017ffc(u32 name, u32 b, u32 c, u32 d)
{
  u32 n = (name - 0x27BE7C) / 12, s1, s2;
  if (name < 0x27BE7C || n >= 5 || name != 0x27BE7C + 12 * n) return 0;
  s1 = *SKIP_ANY; s2 = *SKIP_OPEN;
  *SKIP_ANY = 0x34120800; *SKIP_OPEN = 0x34120800; ps1_flush_cache();
  ps1_movie(3 + n);
  *SKIP_ANY = s1; *SKIP_OPEN = s2; ps1_flush_cache();
  return 0;
}
/* M7c: OFFICIAL ILLUSTRATIONS. The menu calls the viewer loop 0x27B440 (lifted: bonus_ill). Its pictures are a P4 type
   0x40 object of set 0xA3 (13 x 13 pieces of 8-bpp 16 x 16 tiles, palette byte 0xA0 = CRAM bank 2 = PS1 CLUT row 482);
   ILLnn.PAC holds the palette (chunk 9) and the tiles (chunk 0x102). PS1 files ILL00-29 (converted at build time,
   m7c_ill_file): 2048 bytes of chunk 9, then the 256 x 256 8-bpp texture for VRAM (512, 0) = the 0x102 dest, 33 sectors,
   reached through 30 virtual file entries in this overlay (bonus_ill_vf = the first). Loads go to 0x80048000 (Saturn
   0x26080000, M7B_MEM). */
extern void bonus_ill(u32);
extern void ps1_file_load(u32 file, u32 dst);       /* 0x800FB7A4: start a read (waits while the CD is busy) */
extern void ps1_load_image(void *rect, void *src);  /* 0x800D3E84: libgpu LoadImage */
extern void ps1_render_sprites(void);               /* 0x800F88FC: sprite render (Saturn 0x06010CA8) */
extern char bonus_ill_vf[];                         /* linker symbol = virtual file number of ILL00 */
#define ILL_BUF 0x80048000
#define ILL_NAMES 0x29B308                          /* Saturn: "ILLnn.PAC" names, 13 bytes apart */
static u8 ill_pending __attribute__((section(".data"))) = 0;
static u8 ill_cur __attribute__((section(".data"))) = 0;
static s16 ill_rect[4] __attribute__((section(".data"))) = {512, 0, 128, 256};
/* The swap blend (h_06010ca8) needs the shown picture's card rectangle (all 30 cards are exact rectangles inside the
   13 x 13 tiles: x0, y0, x1, y1, from m7c_ill_file) and two 256-entry CLUTs, all black and all white (STP set), at VRAM
   (704, 510) / (704, 511) (free: no title, menu or illustration data there). */
static const u8 ill_card[30][4] = ILL_CARDS;
static u8 ill_shown __attribute__((section(".data"))) = 0;
static u16 ill_bw[512] __attribute__((section(".data"))) = {0};
static s16 ill_bw_rect[4] __attribute__((section(".data"))) = {704, 510, 256, 2};
static void ill_distribute(void)                    /* Saturn 0x0600B54C: the chunks to their dests */
{
  u32 k; volatile u32 *s = (volatile u32 *)ILL_BUF, *d = (volatile u32 *)0x8015A064;
  for (k = 0; k < 512; k++) d[k] = s[k];             /* chunk 9 -> chunk-9 buffer (Saturn 0x29E5A0) */
  ps1_load_image(ill_rect, (void *)(ILL_BUF + 2048)); /* chunk 0x102 -> VRAM */
  for (k = 0; k < 512; k++) ill_bw[k] = k < 256 ? 0x8000 : 0xFFFF;
  ps1_load_image(ill_bw_rect, ill_bw);
  ill_shown = ill_cur;
}
/* Read stall (M7c v2 test: the screen paused briefly before each new picture). When a read ends, the loader's ready
   callback (interrupt time) stops the drive with CdControlB(CdlPause) at 0x800FC1A4 and waits for the pause to complete,
   a few frames in which no frame is shown (Mednafen: 3-4 identical frames before the swap; the game loop itself keeps
   running). The game only reads behind black screens; the Saturn viewer reads while the picture is up. For the
   illustration reads only, that call becomes CdControl (0x800CEEE8, same arguments: returns once the drive has
   acknowledged the command); the next read already waits for the drive (0x800FB7A4: status, then CdSync). Restored when
   the read has finished. */
extern void ps1_flush_cache(void);
#define PAUSE_CALL (*(volatile u32 *)0x800FC1A4)
#define JAL_CDCONTROLB (0x0C000000u | (0x800CF14Cu >> 2 & 0x3FFFFFF))   /* jal 0x800CF14C */
#define JAL_CDCONTROL  (0x0C000000u | (0x800CEEE8u >> 2 & 0x3FFFFFF))   /* jal 0x800CEEE8 */
static u32 ill_start(u32 name)
{
  u32 n = (name - ILL_NAMES) / 13;
  if (name < ILL_NAMES || n >= 30 || name != ILL_NAMES + 13 * n) return 0;
  PAUSE_CALL = JAL_CDCONTROL; ps1_flush_cache();
  ps1_file_load((u32)bonus_ill_vf + n, ILL_BUF);
  ill_cur = n;
  return 1;
}
__attribute__((noinline, noipa)) u32 h_0027b440(u32 a, u32 b, u32 c, u32 d) { bonus_ill(a); return 0; }
__attribute__((noinline, noipa)) u32 h_060099b0(u32 a, u32 b, u32 c, u32 d) { return 0; }   /* VBlank: skip VDP updates (0x0602D913) */
__attribute__((noinline, noipa)) u32 h_0600a6c8(u32 a, u32 b, u32 c, u32 d) { return 0; }   /* CD file system mode */
__attribute__((noinline, noipa)) u32 h_0600a646(u32 a, u32 b, u32 c, u32 d) { return 0; }   /* end of a file read */
__attribute__((noinline, noipa)) u32 h_0600a204(u32 a, u32 b, u32 c, u32 d) { return 0; }   /* change directory ("BONUS" / root) */
__attribute__((noinline, noipa)) u32 h_0600e0a8(u32 a, u32 b, u32 c, u32 d) { return 0; }   /* clear VDP2 maps (the menu sets its layers up again) */
/* 0x0600A760(name, 0x26080000): read a whole file; 0x0600B50C(name): stream a PAC to its dests; 0x0600A928(): busy. */
__attribute__((noinline, noipa)) u32 h_0600a760(u32 name, u32 b, u32 c, u32 d) { ill_start(name); return 0; }
__attribute__((noinline, noipa)) u32 h_0600b50c(u32 name, u32 b, u32 c, u32 d) { ill_pending = ill_start(name); return 0; }
__attribute__((noinline, noipa)) u32 h_0600a928(u32 a, u32 b, u32 c, u32 d)
{
  if (!ps1_load_done()) return 1;
  if (PAUSE_CALL != JAL_CDCONTROLB) { PAUSE_CALL = JAL_CDCONTROLB; ps1_flush_cache(); }
  if (ill_pending) { ill_pending = 0; ill_distribute(); }
  return 0;
}
__attribute__((noinline, noipa)) u32 h_0600b54c(u32 buf, u32 b, u32 c, u32 d) { ill_distribute(); return 0; }
/* 0x27BA94: the screen's objects (spawner, screen 2), then the picture object: P4 type 0x40 at (0xCC, 0x78). Written out:
   the new object comes back from the engine's spawn function, so the lifter cannot see that the X / Y stores are object
   fields (Saturn integer halfwords +0x0C / +0x10 = PS1 +0x0E / +0x12, the high halves of the LE fixed-point words). */
extern u32 h_0601470c(u32, u32, u32, u32);
__attribute__((noinline, noipa)) u32 h_0027ba94(u32 a, u32 b, u32 c, u32 d)
{
  u8 *o;
  h_002761dc(2, 0, 0, 0);
  o = (u8 *)h_0601470c(0, 0, 0, 0);
  if (o) { o[0] = 1; o[6] = 0x40; *(u16 *)(o + 0x0E) = 0xCC; *(u16 *)(o + 0x12) = 0x78; o[1] = 0; }
  return (u32)o;
}
/* 0x27BB28(layer struct, +5): the viewer's layer set-up (called with each of the three structs; same layout on the PS1,
   M7B_MEM), written out because the lifter sees its pointer argument both as a constant and as a parameter. */
__attribute__((noinline, noipa)) u32 h_0027bb28(u32 p, u32 v, u32 c, u32 d)
{
  u8 *b = (u8 *)p; u16 *h = (u16 *)p; u32 k;
  b[0] = 1; b[5] = v; b[1] = 1;
  h[4] = h[3] = 0x100;
  for (k = 9; k <= 15; k++) h[k] = 0x100;            /* +0x12 .. +0x1E */
  h[20] = 1; h[19] = 1; h[8] = 0; h[7] = 0;          /* +0x28, +0x26, +0x10, +0x0E */
  return 0;
}
/* Saturn 0x06012E70(mode) = PS1 0x80102E84: fade request; the fade task (slot 2) starts on the next frame. */
extern void ps1_fade(u32 mode);
extern void ps1_add_prim(void *ot, void *prim);     /* 0x800D313C: libgpu AddPrim */
static u8 fade_req __attribute__((section(".data"))) = 0;
__attribute__((noinline, noipa)) u32 h_06012e70(u32 mode, u32 b, u32 c, u32 d) { fade_req = 1; ps1_fade(mode); return 0; }
#define FADE_MODE (*(volatile u8 *)0x8015D225)
#define FADE_BUSY (*(volatile u8 *)0x8015D226)
#define DB (*(volatile s8 *)0x80162A8C)
static u32 prims[2][7][10] __attribute__((section(".data"))) = {{{0}}};
static u32 dark_tile[2][4] __attribute__((section(".data"))) = {{0}};
static void ft4(u32 *p, u32 rgb, u32 x, u32 y, u32 w, u32 h, u32 clut, u32 tpage)
{
  p[0] = 9 << 24; p[1] = 0x2E000000 | rgb;          /* POLY_FT4, semi-transparent, modulated */
  p[2] = y << 16 | x;           p[3] = clut << 16;
  p[4] = y << 16 | (x + w);     p[5] = tpage << 16 | w;
  p[6] = (y + h) << 16 | x;     p[7] = h << 8;
  p[8] = (y + h) << 16 | (x + w); p[9] = h << 8 | w;
}
/* Saturn 0x06010CA8 = PS1 0x800F88FC (sprite render), plus two things the Saturn's VDPs do for free:
   1. Fades. The Saturn keeps its colour offset at black after a fade-out until the next fade-in runs. The PS1 fade task
      draws its darkening quads only while it runs (0x8015D226 busy), so a frame drawn after a finished fade-out, or in
      the frame a fade-in is requested (the task starts one frame later), showed at full brightness (M7c test: the screen
      flashed entering / leaving the illustrations; the menu requests the next fade-in while the screen is black). Such
      frames get an opaque black tile in the fade's own OT entry (*0x80170334 + 0x94).
   2. The picture swap (illustrations): sprite colour calculation CCCTL (VDP2 shadow +0xEC) bit 6, ratio r = CCRSA (+0x100)
      = picture weight a = (31 - r)/32 against the background (0x27BAEE, 16 frames each way). The PS1 has no blend ratio,
      so: n black half-transparent quads over the card (mode 0: background x 1/2 each), n the largest with 2^-n >= 1 - a,
      one subtractive quad of the background's mean colour x (2^-n - (1 - a)) (mode 2; background mean (204, 113, 32)
      measured in Mednafen under the card area, std about 20), then the picture's own pieces additive (mode 1) at colour a.
      Background weight is then exactly 1 - a in its mean (its texture contrast is 2^-n), the picture weight exactly a.
      The quads use the picture's texture page with the all-black / all-white CLUTs and go to the head of the OT entry
      holding the picture's pieces (found by walking the OT entries of this frame's draw struct *0x80170334; Mednafen
      state: the sprite pieces hang off +0x74, the fade quads +0x94). If none is found the pieces stay opaque. */
__attribute__((noinline, noipa)) u32 h_06010ca8(u32 a, u32 b, u32 c, u32 d)
{
  volatile u16 *vdp = (volatile u16 *)(bonus_ram + VDP_OFF);
  u32 n, k, r, w, q, nd, kq, db = DB, *ot = 0, e;
  s32 minx = 0x7FFF, miny = 0x7FFF;
  u8 *p, *first = 0;
  ps1_render_sprites();
  if (FADE_BUSY) fade_req = 0;
  if (fade_req ? FADE_MODE == 2 || FADE_MODE == 8 : !FADE_BUSY && (FADE_MODE == 3 || FADE_MODE == 9)) {
    u32 *t = dark_tile[db];
    t[0] = 3 << 24; t[1] = 0x60000000; t[2] = 0; t[3] = 240 << 16 | 320;
    ps1_add_prim((void *)(*(volatile u32 *)0x80170334 + 0x94), t);
    return 0;
  }
  if (!(vdp[0xEC / 2] & 0x40)) return 0;
  r = vdp[0x100 / 2] & 31; w = 31 - r; q = r + 1;   /* weights in 1/32: picture w (Saturn: 31 - r), background q */
  if (r == 0) return 0;
  n = *(volatile u16 *)0x1F800012;
  p = (u8 *)(0x80162A90 + 0x5000 * db);
  for (k = 0; k < n; k++, p += 40) {
    u16 tp = *(u16 *)(p + 0x16);
    if ((tp & 0x9F) != 0x88) continue;
    if (!first) first = p;
    if (*(s16 *)(p + 8) < minx) minx = *(s16 *)(p + 8);
    if (*(s16 *)(p + 10) < miny) miny = *(s16 *)(p + 10);
  }
  if (!first) return 0;
  {                                                  /* the OT entry whose chain holds the picture */
    u32 base = *(volatile u32 *)0x80170334;          /* this frame's draw struct (0x801CF3FC + 0xA0 x buffer) */
    for (e = 0x70 / 4; e < 0xA0 / 4 && !ot; e++) {   /* the OT entries (+0x70..+0x9C, Mednafen state) */
      u32 *x = (u32 *)(base + 4 * e), steps;
      for (steps = 0; steps < 2048; steps++) {
        x = (u32 *)((*x & 0xFFFFFF) | 0x80000000);
        if ((u32)x - base < 0xA0 || (u32)x >= 0x80200000) break;   /* reached the next entry / end tag */
        if ((u8 *)x == first) { ot = (u32 *)(base + 4 * e); break; }
      }
    }
  }
  if (!ot) return 0;
  for (p = first, k = 0; k < n; k++, p += 40) {      /* the picture's pieces: additive at weight a */
    u16 tp = *(u16 *)(p + 0x16);
    if ((u32)(p - (u8 *)(0x80162A90 + 0x5000 * db)) >= 40 * n) break;
    if ((tp & 0x9F) != 0x88) continue;
    p[7] |= 2; *(u16 *)(p + 0x16) = (tp & ~0x60) | 0x20;
    p[4] = p[5] = p[6] = (u8)(w * 4);
  }
  for (nd = 0; (32u >> nd) >= q * 2 && nd < 5; nd++) ;  /* largest n with 2^-n >= q/32 */
  kq = (32u >> nd) - q;                              /* subtract (kq/32) x mean background */
  {
    const u8 *cd = ill_card[ill_shown];
    u32 x = minx + cd[0], y = miny + cd[1], ww = cd[2] - cd[0] + 1, hh = cd[3] - cd[1] + 1, i = 0;
    if (kq) {                                        /* white CLUT, colour = mean x kq/32 (5-bit: 248 x m / 128) */
      u32 cr = kq * 204 / 62, cg = kq * 113 / 62, cb = kq * 32 / 62;
      ft4(prims[db][i], cb << 16 | cg << 8 | cr, x, y, ww, hh, (511 << 6) | (704 >> 4), 0x88 | 0x40);
      ps1_add_prim(ot, prims[db][i]); i++;
    }
    for (k = 0; k < nd; k++, i++) {                  /* black CLUT, half-transparent */
      ft4(prims[db][i], 0x808080, x, y, ww, hh, (510 << 6) | (704 >> 4), 0x88);
      ps1_add_prim(ot, prims[db][i]);
    }
  }
  return 0;
}
__attribute__((noinline, noipa)) u32 h_002761dc(u32 a, u32 b, u32 c, u32 d) { bonus_spawn(a); return 0; }
extern void bonus_menu(u32);
extern void ps1_stop_bgm(void);
extern char bonus_music_vf[];                       /* linker symbol: address of the music entries in this overlay's file */
void bonus_main(void)
{
  u32 k;
  ps1_stop_bgm();                              /* the Saturn title stops its music (0x0600C2B8) before 0x27AAD0 (0x2099C0) */
  for (k = 0; k < 64; k++) m7b_p4tab[k] = ((void (**)(u32))0x8015029C)[k];
  m7b_p4tab[0x40] = bonus_p4_40; m7b_p4tab[0x41] = bonus_p4_41;
  /* M7e: the music virtual entries 0x46-0x49 (the same as the stage overlays'; filled in by the build) where the play_bgm
     hook reads them, for the sound test's CUTMAN STAGE / WOODMAN STAGE (music 0x49 / 0x47) */
  for (k = 0; k < 12; k++) ((volatile u32 *)0x801E4658)[k] = ((volatile u32 *)bonus_music_vf)[k];
  bonus_menu(0);                               /* Saturn 0x27AAD0: runs until its state 5 (EXIT) */
}
'''
M7B_GLUE_SYMS = dict(ps1_yield=0x800F7ECC, ps1_stream=0x800FB9C0, ps1_load_done=0x800FC1E0, ps1_stop_bgm=0x800FCD84, ps1_movie=0x800FD444, ps1_flush_cache=0x800D78EC,
                     ps1_file_load=0x800FB7A4, ps1_load_image=0x800D3E84, ps1_render_sprites=0x800F88FC,
                     ps1_fade=0x80102E84, ps1_add_prim=0x800D313C, ps1_draw_sync=0x800D3C5C,
                     # M7e: the PS1 sound calls of play_bgm 0x800FCB80 and the task functions
                     ps1_seq_close=0x800C5A44, ps1_vab_close=0x800C699C, ps1_se_release=0x800FD240, ps1_c0bf8=0x800C0BF8,
                     ps1_bgm_async=0x800FCB80, ps1_sound=0x800FD100, ps1_task_start=0x800F7E08, ps1_task_exit=0x800F7F00)
M7B_CUSTOM = (0x06008160, 0x00210DA4, 0x0027B3D2, 0x0027B1C0, 0x0027B0F8, 0x0027B440, 0x002761DC, 0x06017FFC,
              # M7c: Saturn system functions of the illustration viewer, and the sprite render with the picture's colour calc
              0x060099B0, 0x0600A6C8, 0x0600A760, 0x0600A928, 0x0600A646, 0x0600B50C, 0x0600B54C, 0x0600A204, 0x0600E0A8,
              0x06010CA8, 0x0027BB28, 0x0027BA94,
              0x06012E70,   # fade request (M7c fix: no full-brightness frame between a finished fade and the next)
              # M7e: the sound/voice-test player 0x209088 (lifted, reached by a tail jump) and the Saturn sound-driver /
              # CD calls it and the test screen make (glue below)
              0x00209088, 0x06017804, 0x060171C0, 0x06017AB8, 0x0600C85E, 0x0600BEB6, 0x0600BC8C, 0x0600C610, 0x0600C516, 0x0600A956)

def m7b_spawn_lists(sat=None):
    """Spawn lists of 0x2761DC: pointer table 0x296694 (6 screens) -> 8-byte records (variant, +0x39, +0x3A, +0x38,
    s16 x, s16 y), ended by variant 0xFF. Returns {screen: [(addr, record bytes)]}."""
    sat = sat or Sat(); out = {}
    for k in range(6):
        p = sat.u32(0x296694 + 4 * k); recs = []
        while True:
            r = sat.raw(p, 8); recs.append((p, r))
            if r[0] == 0xFF: break
            p += 8
        out[k] = recs
    return out

class BonusPort(BossPort):
    """BossPort for the bonus menu: several Saturn data regions (concatenated into sat_data, non-const), pointer tables
    (sat_ptrs, PS1 addresses), Saturn-only variables (bonus_ram) and same-layout RAM blocks (M7B_MEM), any access size."""
    def __init__(self, spec):
        BossPort.__init__(self, dict(spec, data=(0, 0)))
        self.regions, off = [], 0
        for lo, hi in spec['regions']: self.regions.append((lo, hi, off)); off += (hi - lo + 3) & ~3
        self.data_len = off
        self.ptrs = []; off = 0
        for t, n in spec['ptr_tables']: self.ptrs.append((t, t + 4 * n, off)); off += 4 * n
        self.ram = []; off = 0
        for lo, hi, nm in spec['ram']: self.ram.append((lo, hi, off)); off += (hi - lo + 3) & ~3
        self.ram_len = off
        self.mem = spec['mem']
        self.hm = dict(self.hm); self.hm.update(spec['frontend'])
        self.custom = {a: '' for a in spec['custom']}
        self.raw_values = set(spec.get('raw_values', ()))
    def _sym(self, v):
        for t, e, off in self.ptrs:
            if t <= v < e: return f'((u32)sat_ptrs + {off + v - t:#x})'
        for lo, hi, off in self.regions:
            if lo <= v < hi: return f'((u32)sat_data + {off + v - lo:#x})'
        for lo, hi, off in self.ram:
            if lo <= v < hi: return f'((u32)bonus_ram + {off + v - lo:#x})'
        for lo, hi, p, nm in self.mem:
            if lo <= v < hi: return f'{p + v - lo:#x}'
        return None
    def region_of(self, v):
        for k, (t, e, off) in enumerate(self.ptrs):
            if t <= v < e: return ('p', k)
        for k, (lo, hi, off) in enumerate(self.regions):
            if lo <= v < hi: return ('d', k)
        for k, (lo, hi, off) in enumerate(self.ram):
            if lo <= v < hi: return ('r', k)
        for k, (lo, hi, p, nm) in enumerate(self.mem):
            if lo <= v < hi: return ('m', k)
        return None
    def value(self, v, a):
        if v in self.raw_values: return f'{v:#x} /* Saturn address kept: decoded by the glue */'
        r = self._sym(v) if _addr_like(v) or 0x26000000 <= v < 0x26100000 else None
        return r if r is not None else BossPort.value(self, v, a)
    def const_addr(self, sat, size, kind, a):
        r = self._sym(sat)
        return r if r is not None else BossPort.const_addr(self, sat, size, kind, a)
    def cx_addr(self, B, sat, size, kind, a, expr, disp):
        rb, rs = self.region_of(B), self.region_of(sat)
        if rb is not None and rb == rs: return f'({expr} + ({disp}))'
        return BossPort.cx_addr(self, B, sat, size, kind, a, expr, disp)
    def helper(self, sat, a):
        if sat in self.custom: self.used_custom.add(sat); return f'h_{sat:08x}'
        return BossPort.helper(self, sat, a)

M7B_SPEC = dict(name='bonus', regions=M7B_DATA, ptr_tables=M7B_PTR_TABLES, ram=M7B_RAM, mem=M7B_MEM,
                frontend=M7B_FRONTEND_HELPERS, custom=M7B_CUSTOM, code_tables=M7B_CODE_TABLES, globals=WM_GLOBALS,
                blocks=CUT_BLOCKS, cfile='m7b_bonus.c',
                # spawner 0x2761DC: r14 walks the 8-byte spawn records; in the delay slot of 0x27627A it points 8 bytes
                # further, so @(3,r14) at 0x276286 reaches record field 3 at offset 11, which is not an object word field
                raw_at={0x276286},
                raw_values={M7C_ILL_NAMES},      # the viewer passes 0x29B308 + 13 n ("ILLnn.PAC") to the file functions
                handlers=[('bonus_menu', 0x27AAD0, None, [0x0029B2D0], (), (0x27B3D2, 0x27B440, 0x2761DC, 0x27B1C0, 0x27B0F8)),
                          ('bonus_st', 0x27B0F8, None, [], (), (0x209088,)),    # M7e: sound / voice test screen (state block in r4)
                          ('bonus_sndplay', 0x209088, None, [], (), ()),        # M7e: play test entry r4 (r5 = 0 sound test, 1 voice test)
                          ('bonus_pc', 0x27B1C0, None, [], (), ()),          # M7d: postcard screen (state block in r4)
                          ('bonus_ill', 0x27B440, None, [0x0029B490], (), (0x2761DC, 0x27BB28, 0x27BA94)),
                          ('bonus_spawn', 0x2761DC, None, [], OBJ_WORD_FIELDS, ()),
                          ('bonus_p4_40', 0x275E00, 0x40, [], OBJ_WORD_FIELDS, ()),
                          ('bonus_p4_41', 0x275E58, 0x41, [0x002966AC], OBJ_WORD_FIELDS, ())])

def m7b_data_bytes(sat, port):
    """sat_data: the M7B_DATA regions (code tables and u32 words swapped to LE, spawn-record x/y halfwords swapped);
    sat_ptrs: the pointer tables as PS1 offsets into sat_data (+ sat_data's address at link time)."""
    d = bytearray()
    for lo, hi, off in port.regions:
        d += sat.raw(lo, hi - lo); d += bytes(((hi - lo + 3) & ~3) - (hi - lo))
    def at(v):
        for lo, hi, off in port.regions:
            if lo <= v < hi: return off + v - lo
        raise ValueError(f'{v:#x} outside the data regions')
    for t, n in list(M7B_CODE_TABLES.items()) + M7B_WORD_DATA:
        for k in range(n): o = at(t + 4 * k); d[o:o + 4] = d[o:o + 4][::-1]
    for t, n in M7E_HALF_DATA:
        for k in range(n): o = at(t + 2 * k); d[o:o + 2] = d[o:o + 2][::-1]
    done = set()                      # screens 3 and 4 (sound / voice test) share one list: swap each record once
    for scr, recs in m7b_spawn_lists(sat).items():
        for p, r in recs:
            if r[0] == 0xFF or p in done: continue
            done.add(p)
            for f in (4, 6): o = at(p + f); d[o:o + 2] = d[o:o + 2][::-1]
    ptr_offs = [at(sat.u32(t + 4 * k)) for t, n in M7B_PTR_TABLES for k in range(n)]
    return bytes(d), ptr_offs

def m7b_lift():
    """Lift the bonus menu, spawner and P4 0x40/0x41 to C. Returns (C source, BonusPort)."""
    sat = Sat(); code = Code([(sat.BASE2, sat.p2), (sat.BASE1, sat.p1)])
    port = BonusPort(M7B_SPEC); bodies = []
    for name, entry, typ, tabkeys, words, stops in M7B_SPEC['handlers']:
        port.word_fields = words
        src, _ = lift(code, name, entry, {k: M7B_CODE_TABLES[k] for k in tabkeys}, port, stops=stops, stack_words=96,
                      nargs=2 if name == 'bonus_sndplay' else 1)
        bodies.append(src)
    data, ptr_offs = m7b_data_bytes(sat, port)
    hm = port.hm
    out = ['/* Generated by mm8tool (SH-2 lifter) from Saturn 2NDREAD.PRG: Bonus Mode menu (M7b) - do not edit. */',
           'typedef signed char s8; typedef unsigned char u8; typedef short s16; typedef unsigned short u16;',
           'typedef int s32; typedef unsigned int u32;']
    for h in sorted(port.used_helpers): out.append(f'extern u32 h_{h:08x}(u32, u32, u32, u32);   /* PS1 {hm[h]:#010x} */')
    out.append(f'u8 sat_data[{len(data)}] __attribute__((aligned(4), section(".data"))) = {{  /* Saturn data regions, tables converted */')
    for k in range(0, len(data), 16): out.append('  ' + ', '.join(f'{b:#04x}' for b in data[k:k + 16]) + ',')
    out.append('};')
    out.append(f'const u32 sat_ptrs[{len(ptr_offs)}] = {{ ' + ', '.join(f'(u32)sat_data + {o:#x}' for o in ptr_offs) + ' };')
    for h in sorted(port.used_custom): out.append(f'u32 h_{h:08x}(u32, u32, u32, u32);')
    pad_off = next(off for lo, hi, off in port.ram if lo == 0x0602DA40)
    vdp_off = next(off for lo, hi, off in port.ram if lo == 0x060673B4)
    cards = '{' + ', '.join('{%d, %d, %d, %d}' % m7c_card(n) for n in range(M7C_ILL_COUNT)) + '}'
    lfiles, lspans = m7d_letter()
    glue = (M7B_GLUE_C.replace('LETTER_RECTS', '{' + ', '.join('{%d, %d, %d, %d}' % sp[2:] for sp in lspans) + '}')
            .replace('LETTER_OFFS', '{' + ', '.join(f'{sp[0] << 24 | sp[1]:#x}' for sp in lspans) + '}').replace('LETTER_NFILES', str(len(lfiles)))
            .replace('BONUS_RAM_SIZE', str(port.ram_len)).replace('PAD_OFF', str(pad_off)).replace('VDP_OFF', str(vdp_off)).replace('ILL_CARDS', cards)
            .replace('PAD_MAP', '{' + ', '.join(f'{{{p:#x}, {s:#x}}}' for p, s in M7B_PAD_MAP) + '}'))
    return '\n'.join(out) + '\n' + glue + '\n' + m7b_p4_loop_asm() + '\n' + '\n'.join(bodies), port



# ---- M7b build: converted SLABO.PAC and the bonus overlay
M7B_BONUS_MAGIC = b'MM8R-B7b'
M7B_BONUS_LIMIT = 0x801F4000                # LETTER palettes are reserved from here (M7B_MEM)

def front_pac_sat_to_ps1(t, b):
    """One chunk of a Saturn front-end PAC (LABO / SLABO layout) -> PS1. Checked on LABO.PAC (verify): chunks 0-8, 0x12,
    0x100, 0x102, 0x103 reproduce the PS1 file byte for byte; 0x101 in 745 of 768 tiles (PS1-edited glyphs); chunk 9 in
    colours (the PS1 set STP on a few LABO sprite palettes). STP: background palettes (>= 0x40) on every non-zero colour,
    sprite palettes clear (the stage rule)."""
    if t in (0, 1, 2, 8, 0x12, 0x13, 0x14): return b
    if t == 3: return b''.join(b[i:i + 2][::-1] for i in range(0, len(b), 2))
    if t == 4: return blocks_sat_to_ps1(b)
    if t in (5, 6): return swap32(b)
    if t == 7:
        n = len(b) // 2; a = struct.unpack('>%dH' % n, b)
        return struct.pack('<%dH' % n, *(sprite_base_sat_to_ps1(i, v) for i, v in enumerate(a)))
    if t == 9:
        a = struct.unpack('>%dH' % (len(b) // 2), b)
        return struct.pack('<%dH' % len(a), *[(v | 0x8000) if v and k // 16 >= 0x40 else v & 0x7FFF for k, v in enumerate(a)])
    if t == 0xB:                          # palette frames: SLABO's colour schemes go to background palettes 0x40/0x41
        a = struct.unpack('>%dH' % (len(b) // 2), b)
        return struct.pack('<%dH' % len(a), *[(v | 0x8000) if v else 0 for v in a])
    if t in (0x100, 0x101, 0x102): return gfx_sat_to_ps1(b, False)
    if t == 0x103: return gfx_sat_to_ps1(b, True)
    raise ValueError(f'chunk {t:#x}')

# PS1 button labels on the bonus screens: the menu prompt (set 0xA9 frame 0x11, "PRESS START, A OR C
# BUTTON") becomes "PRESS START OR (x) BUTTON", the PS1 title's own wording, set from SLABO's small font (0x101 tiles:
# letters 212-240, relative to the set base 0xB00). The font's small circled-x glyph (tile 301, drawn for another palette:
# in the text palette 3 its ring colours 8-15 are 0x0000 = transparent on the PS1) is recoloured into the blank last tile
# 767 (ring 15/14/12 -> 3/6/5, cross 7/8 -> 1/2: a light disc with a dark x, like the PS1 title's).
M7B_PROMPT_SET, M7B_PROMPT_FRAME, M7B_PROMPT_TEXT = 0xA9, 0x11, 'PRESS START OR @ BUTTON'
M7B_FONT = {'P': 235, 'R': 237, 'E': 224, 'S': 238, 'T': 239, 'A': 212, 'O': 234, 'C': 214, 'B': 213, 'U': 240, 'N': 233}
M7B_XGLYPH_SRC, M7B_XGLYPH_TILE, M7B_XGLYPH_MAP = 301, 767, {15: 3, 14: 6, 12: 5, 7: 1, 8: 2}

def m7b_prompt_pieces(text=M7B_PROMPT_TEXT):
    """Pieces (tile, x, y) of the PS1 prompt line, centred where the Saturn's line is (x -120..+87 -> centre -16)."""
    x, out = 0, []
    for c in text:
        if c == ' ': x += 8
        elif c == '@': out.append((M7B_XGLYPH_TILE, x - 3, -12)); x += 11      # glyph pixels 3..12 of its tile
        else: out.append((M7B_FONT[c], x, -8)); x += 8
    x0 = -16 - x // 2
    return [(t, px + x0, y) for t, px, y in out]

# M7c: the button labels "A.C...CHOOSE" / "B...EXIT" (set 0xA9 frames 0x36 / 0x37 on the illustration screen, 10 / 11 on
# the postcard screen; small label font: letters 11 px tall in tile rows 5-15, 8 px apart) get the PS1 buttons from the
# same font (x = confirm, triangle = back): tile 199 = cross, 198 = triangle (14 x 14 in rows 1-14; y -6 centres them
# on the letters), left-aligned with the other lines at -120, then the ellipsis 221 and the word with the Saturn's gaps.
M7C_LAB_FONT = {'C': 162, 'H': 167, 'O': 206, 'S': 178, 'E': 164, 'X': 183, 'I': 200, 'T': 179}
def _m7c_label(btn, word):
    return [(btn, -121, -6), (221, -103, -8)] + [(M7C_LAB_FONT[c], -92 + 8 * k, -8) for k, c in enumerate(word)]
# The font's triangle (tile 198) is drawn in the label palette's mid tones 3/4/5 (the cross in white 7/8): a white copy in
# the blank tile 766 (3, 4 -> 7, 5 -> 6) matches the letters. The glyphs' disc colours (12-15) are transparent there.
M7C_TRI_SRC, M7C_TRI_TILE, M7C_TRI_MAP = 198, 766, {3: 7, 4: 7, 5: 6}
M7C_LABELS = {0x36: _m7c_label(199, 'CHOOSE'), 0x37: _m7c_label(M7C_TRI_TILE, 'EXIT'), 10: _m7c_label(199, 'CHOOSE'), 11: _m7c_label(M7C_TRI_TILE, 'EXIT')}

def unpac_dict_sat_slabo():
    return {t: b for t, o, b in unpac(os.path.join(SAT_FILES, 'SLABO.PAC'))}

@_memo
def m7b_slabo_pac():
    """SLABO.PAC (Saturn only: bonus backgrounds, fonts, cursors) converted to a PS1 PAC, loaded like LABO.PAC; prompt
    line with the PS1 button (above)."""
    ch = [(t, bytearray(front_pac_sat_to_ps1(t, b))) for t, o, b in unpac(os.path.join(SAT_FILES, 'SLABO.PAC'))]
    d = dict(ch)
    c5, c6 = d[5], d[6]
    fo = struct.unpack_from('<I', c6, 4 * M7B_PROMPT_SET)[0]
    cnt, po = struct.unpack_from('<HH', c5, fo + 4 * M7B_PROMPT_FRAME)
    pcs = m7b_prompt_pieces()
    assert cnt == 21 and len(pcs) <= cnt and all(c5[fo + po + 4 * k + 1] & 0xFC == 0 for k in range(cnt))
    for k, (t, x, y) in enumerate(pcs): c5[fo + po + 4 * k:fo + po + 4 * k + 4] = bytes([t & 0xFF, t >> 8, x & 0xFF, y & 0xFF])
    struct.pack_into('<H', c5, fo + 4 * M7B_PROMPT_FRAME, len(pcs))
    for fr, pcs in M7C_LABELS.items():             # M7c: "x...CHOOSE" / "triangle...EXIT" (PS1 buttons)
        cnt, po = struct.unpack_from('<HH', c5, fo + 4 * fr)
        assert len(pcs) <= cnt and all(c5[fo + po + 4 * k + 1] & 0xFC == 0 for k in range(cnt))
        for k, (t, x, y) in enumerate(pcs): c5[fo + po + 4 * k:fo + po + 4 * k + 4] = bytes([t & 0xFF, t >> 8, x & 0xFF, y & 0xFF])
        struct.pack_into('<H', c5, fo + 4 * fr, len(pcs))
    for s_ in M7D_CARD_SETS:                       # M7d: the 8-bpp card sets point at LETTER's characters (pages 5-13)
        sb = struct.unpack_from('>H', unpac_dict_sat_slabo()[7], 2 * s_)[0]
        struct.pack_into('<H', d[7], 2 * s_, m7d_ps1_base(sb))
    g = d[0x101]
    assert not any(ps1_tile(g, M7B_XGLYPH_TILE))
    src = ps1_tile(g, M7B_XGLYPH_SRC)
    nib = lambda v: M7B_XGLYPH_MAP.get(v, v) if v else 0
    ps1_put_tile(g, M7B_XGLYPH_TILE, bytes(nib(b & 15) | nib(b >> 4) << 4 for b in src))
    assert not any(ps1_tile(g, M7C_TRI_TILE))
    tri = lambda v: M7C_TRI_MAP.get(v, v)
    ps1_put_tile(g, M7C_TRI_TILE, bytes(tri(b & 15) | tri(b >> 4) << 4 for b in ps1_tile(g, M7C_TRI_SRC)))
    return pac_build([(t, bytes(b)) for t, b in ch])

def build_m7b_bonus(workdir=None):
    """Bonus overlay (linked at M7_BONUS_BASE, entry bonus_main): glue + lifted menu, then marker and one virtual file
    entry for the converted SLABO.PAC (the caller fills in LBA/size). Two passes: the entry's file number is a linker
    symbol and its address follows the code."""
    import tempfile
    wd = workdir and os.path.join(workdir, 'm7b_bonus') or tempfile.mkdtemp(prefix='mm8m7b_')
    os.makedirs(wd, exist_ok=True)
    csrc, port = m7b_lift()
    nletter = len(m7d_letter()[0])
    syms = dict(helper_symbols(), **M7B_GLUE_SYMS, **{f'h_{k:08x}': v for k, v in M7B_FRONTEND_HELPERS.items()})
    for k in M7B_CUSTOM: syms.pop(f'h_{k:08x}', None)  # defined in the glue
    vf = 0
    for _ in range(3):
        mvf = M7_BONUS_BASE + 12 * (vf + 1 + M7C_ILL_COUNT + nletter) - (M7_BONUS_BASE - PS1_FILETAB) if vf else 0
        data, sy = m7_compile([('m7b_bonus.c', csrc)], 'bonus', M7_BONUS_BASE, dict(syms, bonus_slabo_vf=vf, bonus_ill_vf=vf + 1,
                                                                                    bonus_letter_vf=vf + 1 + M7C_ILL_COUNT,
                                                                                    bonus_music_vf=mvf),
                              'bonus_main', wd)
        assert sy['bonus_main'] == M7_BONUS_BASE
        out = bytearray(data)
        while len(out) % 4: out.append(0)
        out += M7B_BONUS_MAGIC
        while (M7_BONUS_BASE + len(out) - PS1_FILETAB) % 12: out += bytes(4)
        vf_off = len(out); new_vf = (M7_BONUS_BASE + vf_off - PS1_FILETAB) // 12
        out += bytes(12 * (1 + M7C_ILL_COUNT + nletter))   # SLABO, then ILL00-29 (M7c), then the LETTER files (M7d)
        music_off = len(out); out += bytes(12 * 4)          # M7e: copies of the music 0x46-0x49 entries (bonus_main moves them)
        if new_vf == vf: break
        vf = new_vf
    assert vf == new_vf and M7_BONUS_BASE + len(out) <= M7B_BONUS_LIMIT, hex(M7_BONUS_BASE + len(out))
    assert M7_BONUS_BASE + music_off == mvf and M7_BONUS_BASE >= M7E_MUSIC_VF + 48
    return bytes(out), dict(slabo_vf=vf, slabo_vf_off=vf_off, ill_vf=vf + 1, ill_vf_off=vf_off + 12, bonus_size=len(out),
                            letter_vf=vf + 1 + M7C_ILL_COUNT, letter_vf_off=vf_off + 12 * (1 + M7C_ILL_COUNT), letter_files=nletter,
                            music_vf_off=music_off,
                            csrc=os.path.join(wd, 'm7b_bonus.c'), bonus_menu=sy['bonus_menu'], bonus_ill=sy['bonus_ill'],
                            sat_data=sy['sat_data'], bonus_ram=sy['bonus_ram'])

# ---- M7c: OFFICIAL ILLUSTRATIONS. Saturn BONUS/ILLnn.PAC = chunk 9 (1024 colours, the picture in CRAM bank 2 =
# entries 512-767) + chunk 0x102 (256 16 x 16 8-bpp characters, 16 bytes per row, in a 16 x 16 grid; set 0xA3 draws 13 x 13
# of them). PS1 file: chunk 9 as LE BGR555 (entry 512 = 0 = transparent; the other picture colours get STP so the
# viewer's semi-transparent swap applies to them; the zero entries stay zero like the Saturn's), then the 256 x 256 texture
# (one byte per texel, 256 bytes per line) that LoadImage puts at VRAM (512, 0) as 128 x 256 halfwords: the renderer's
# 8-bit path reads tile t of page 8 at u = (t & 15) x 16, v = t & 0xF0 (02 §Sprite renderer).

def m7c_card(n):
    """Card rectangle (x0, y0, x1, y1) of illustration n inside set 0xA3's 208 x 208 tiles: every texel inside is opaque
    and none outside (all 30 cards are exact rectangles; asserted)."""
    import numpy as np
    pal, px = sat_ill_image(n); v = px != 0
    ys, xs = np.where(v); y0, y1, x0, x1 = int(ys.min()), int(ys.max()), int(xs.min()), int(xs.max())
    assert v[y0:y1 + 1, x0:x1 + 1].all() and v.sum() == (y1 - y0 + 1) * (x1 - x0 + 1) and x1 < 208 and y1 < 208, n
    return x0, y0, x1, y1

@_memo
def m7c_ill_file(n):
    ch = {t: b for t, o, b in unpac(os.path.join(SAT_FILES, 'BONUS', f'ILL{n:02d}.PAC'))}
    assert sorted(ch) == [9, 0x102] and len(ch[9]) == 2048 and len(ch[0x102]) == 65536
    pal = list(struct.unpack('>1024H', ch[9]))
    out = [0 if k == 512 else (v | 0x8000) if 512 < k < 768 else v & 0x7FFF for k, v in enumerate(pal)]
    g = ch[0x102]; tex = bytearray(65536)
    for t in range(256):
        u0, v0 = (t & 15) * 16, (t >> 4) * 16
        for y in range(16): tex[(v0 + y) * 256 + u0:(v0 + y) * 256 + u0 + 16] = g[256 * t + 16 * y:256 * t + 16 * y + 16]
    return struct.pack('<1024H', *out) + bytes(tex)

# ---- M7d: BOSS CHARACTER POSTCARD. Saturn screen 0x27B1C0 (lifted: bonus_pc; state block +6 = number in the
# box, +7 = leave, +8 = sound; 0x0602DABD = box cursor NO./EXIT, DABE = card shown, DABF = number shown): Right/Left change
# the number (Up/Down move the box cursor), L/R change number and card at once, A/C on NO. show that card with sound
# 0x189 or 0x18A (RNG), B or A/C on EXIT leave (sound 0x38). The card is a P4 0x41 object (variant 5, state 6 = 0x276140):
# card k < 24 = set 0xA2 frame k, k >= 24 = set 0x98 frame k - 24, palette byte chunk8 | 0x80 (8-bit), and on each change
# it copies LETTER palette k (0x260C0000 + 0x200 k, 256 colours) to CRAM palettes 0x20-0x2F (0x0602EECC) and sets the
# dirty flag. Wily's comment = a P4 0x41 object (variant 4, state 5 = 0x2760F8) showing set 0xA9 frame 0x16 + k (SLABO's
# own font: already converted in M7b). LETTER.PAC chunk 0x100 = 8-bpp 16 x 16 characters in 64 KB blocks (256 chars in a
# 16 x 16 grid, 224 used per block); a piece's character = sprite VRAM byte base x 128 + tile x 256 (all 30 cards decode
# with this rule). PS1: the renderer's 8-bit path takes the texture page from (base >> 8) + 2 x (tile >> 8) and u/v from
# (base + tile) & 0xFF (0x800F92B8), i.e. one 8-bpp page per 256 characters, the Saturn's block layout. Saturn sprite VRAM
# offset 0 = PS1 page 5 (as for the illustrations: 0x300 -> 0x800), so block b -> page 5 + 2 b = VRAM (320 + 128 b, 0)
# (pages 5-13: LABO.PAC's 0x100/0x102 area, unused by every bonus screen; the title reloads LABO afterwards). Only the
# characters the cards use are uploaded (per character row the span of used columns), from files read into the movie
# buffer 0x80048000 (192 KB, so the data is split into files of at most M7D_BUF bytes); LETTER's palettes go to
# 0x801F4000 (M7B_MEM). Loaded by 0x27B3D2(1) whenever the menu's +9 flag is clear (after the illustrations too, which
# overwrite pages 8-9).
M7D_CARD_SETS = (0xA2, 0x98)           # read by 0x276140: card < 24 -> 0xA2 frame card, else 0x98 frame card - 24
M7D_CARDS, M7D_SPLIT = 30, 24
M7D_PAGE0 = 5                          # PS1 texture page of Saturn sprite VRAM offset 0
M7D_BUF = 0x30000                      # load buffer 0x80048000-0x80078000

def m7d_ps1_base(b):
    """Saturn sprite-set base (128-byte units) of an 8-bpp set -> PS1 base for the 8-bit path (page = base >> 8)."""
    c = b // 2                          # first 8-bpp character (256 bytes)
    assert b % 2 == 0 and c & 0xFF == 0
    return (M7D_PAGE0 + 2 * (c >> 8)) << 8

def m7d_card_chars(sat_slabo=None):
    """Per card: [(Saturn character number, x, y)] of its frame (character = base / 2 + tile)."""
    ch = sat_slabo or {t: b for t, o, b in unpac(os.path.join(SAT_FILES, 'SLABO.PAC'))}
    c5, c6 = swap32(ch[5]), swap32(ch[6]); out = []
    for card in range(M7D_CARDS):
        s, f = (M7D_CARD_SETS[0], card) if card < M7D_SPLIT else (M7D_CARD_SETS[1], card - M7D_SPLIT)
        base = struct.unpack_from('>H', ch[7], 2 * s)[0]
        fo = struct.unpack_from('<I', c6, 4 * s)[0]; cnt, po = struct.unpack_from('<HH', c5, fo + 4 * f)
        pcs = []
        for k in range(cnt):
            p = c5[fo + po + 4 * k:fo + po + 4 * k + 4]
            assert p[1] & 0xFC == 0                # no flips on the cards
            pcs.append((base // 2 + (p[0] | (p[1] & 3) << 8), struct.unpack('b', p[2:3])[0], struct.unpack('b', p[3:4])[0]))
        out.append(pcs)
    return out

def m7d_char_vram(c):
    """PS1 VRAM position (halfword x, line y) of Saturn 8-bpp character c."""
    return 64 * (M7D_PAGE0 + 2 * (c >> 8)) + 8 * (c & 15), 16 * ((c >> 4) & 15)

@_memo
def m7d_letter():
    """LETTER.PAC for the PS1: (files, spans). File 0 starts with the 30 card palettes (256 colours each, LE; entry 0 = 0
    transparent, the rest with STP so black stays opaque, like the illustrations), then the character data; each span =
    (file, offset, x, y, w, h): the used columns of one character row of a block, 16 lines of w halfwords."""
    L = {t: b for t, o, b in unpac(os.path.join(SAT_FILES, 'LETTER.PAC'))}
    pal, g = L[0x11], L[0x100]
    assert len(pal) == 512 * M7D_CARDS
    pals = b''.join(struct.pack('<256H', *[0 if k == 0 else v | 0x8000 for k, v in enumerate(struct.unpack('>256H', pal[512 * n:512 * n + 512]))])
                    for n in range(M7D_CARDS))
    used = {c for pcs in m7d_card_chars() for c, x, y in pcs}
    rows = sorted({c >> 4 for c in used})
    files, spans, cur = [], [], bytearray(pals)
    for r in rows:
        cols = [c & 15 for c in used if c >> 4 == r]; c0, c1 = min(cols), max(cols)
        data = b''.join(g[256 * (16 * r + c) + 16 * y:256 * (16 * r + c) + 16 * y + 16] for y in range(16) for c in range(c0, c1 + 1))
        if len(cur) + len(data) > M7D_BUF: files.append(bytes(cur)); cur = bytearray()
        x, y = m7d_char_vram(16 * r + c0)
        spans.append((len(files), len(cur), x, y, 8 * (c1 - c0 + 1), 16)); cur += data
    files.append(bytes(cur))
    return files, spans

def place_records(chunk, side):
    """Chunk 0x0A -> [(type, variant, pool, x, y)] up to the terminator."""
    e = '>' if side == 'sat' else '<'; out = []
    for i in range(0, len(chunk), 8):
        r = chunk[i:i + 8]
        if r[3] == 0xFF: break
        out.append((r[1], r[2], r[3]) + struct.unpack(e + 'HH', r[4:8]))
    return out

# ----------------------------------------------------------------- CLI

# ----------------------------------------------------------------- release builder (M6; v0.4.0: v0.3.0 + the Saturn boss-intro names and HUD positions)
# The user's discs are checked against the Redump dumps, every track of both discs: each Track 1 (RELEASE_IN_MD5) and
# the audio tracks (RELEASE_AUDIO_MD5; each counted from its INDEX 00, as Redump's per-track files). Users give the
# complete disc: a missing track stops the builder. A Track 1 that differs stops it; an audio track that differs only
# gives a note, because most rippers don't correct the drive's read offset (the audio is shifted by a few samples,
# inaudible, but the md5 differs); the PS1 audio tracks are copied into the output as the user gave them. The md5
# of the image the tested toolchain produces is RELEASE_OUT_MD5; another compiler build may give a different (still valid) image.
RELEASE_VERSION = '0.4.0'
RELEASE_IN_MD5 = {'ps1': '4e9e6dcf27ea0e15c5f4d1f93c800378', 'sat': 'a4d575cd681b7d7c88b45d639ed7ffb7'}
RELEASE_AUDIO_MD5 = {'ps1': {2: 'a75dcbb5c831a966c47563e7ea4150c9', 3: '2d7b5e8e94a91bf5423b2356f6a34863'},
                     'sat': {2: 'a2201327867523e4e31de62c445e2996'}}
RELEASE_OUT_MD5 = '9ed2525bb1b0f0da500fa6b1cae39532'
RELEASE_NAME = 'Mega Man 8 - Complete'
_RAW_SYNC = b'\x00' + b'\xff' * 10 + b'\x00'

def cue_tracks(cue):
    """Tracks of a .cue sheet as [{'no', 'mode', 'file', 'start', 'end', 'pregap'}] with byte offsets in their
    file: a track's data starts at its INDEX 00 (Redump keeps a pregap with the track that follows it) and ends where
    the next track in the same file starts. One file per track and one file for the whole disc both work."""
    import shlex
    base = os.path.dirname(os.path.abspath(cue)); S = 2352
    msf = lambda t: (lambda m, s, f: (m * 60 + s) * 75 + f)(*map(int, t.split(':')))
    tracks, cur_file = [], None
    for ln in open(cue, encoding='utf-8', errors='replace').read().splitlines():
        w = ln.strip()
        if not w: continue
        key = w.split()[0].upper()
        if key == 'FILE':
            name = shlex.split(w[4:].strip(), posix=True)[0] if '"' in w else w.split()[1]
            cur_file = os.path.join(base, name)
        elif key == 'TRACK':
            p = w.split(); tracks.append({'no': int(p[1]), 'mode': p[2].upper(), 'file': cur_file, 'idx': {}})
        elif key == 'INDEX' and tracks:
            p = w.split(); tracks[-1]['idx'][int(p[1])] = msf(p[2]) * S
    if not tracks: raise SystemExit(f'{cue}: no tracks found')
    for k, t in enumerate(tracks):
        t['start'] = t['idx'].get(0, t['idx'].get(1, 0))
        t['pregap'] = t['idx'].get(1, t['start']) - t['start']
        nxt = tracks[k + 1] if k + 1 < len(tracks) and tracks[k + 1]['file'] == t['file'] else None
        t['end'] = nxt['idx'].get(0, nxt['idx'].get(1)) if nxt else (os.path.getsize(t['file']) if os.path.exists(t['file']) else None)
    return tracks

def _copy_range(src, start, end, dst, h=None):
    with open(src, 'rb') as f, open(dst, 'wb') as o:
        f.seek(start); left = end - start
        while left > 0:
            b = f.read(min(left, 1 << 22))
            if not b: break
            o.write(b); left -= len(b)
            if h: h.update(b)

def _md5_range(path, start, end):
    h = hashlib.md5()
    with open(path, 'rb') as f:
        f.seek(start); left = end - start
        while left > 0:
            b = f.read(min(left, 1 << 22))
            if not b: break
            h.update(b); left -= len(b)
    return h.hexdigest()

def disc_kind(track):
    """'ps1' / 'sat' / None from the first sectors of a raw track 1."""
    with open(track['file'], 'rb') as f:
        f.seek(track['start']); head = f.read(2352 * 17)
    if head[:12] != _RAW_SYNC: return None
    if head[16:32] == b'SEGA SEGASATURN ': return 'sat'
    if head[16 * 2352 + 25:16 * 2352 + 30] == b'CD001' and b'PLAYSTATION' in head[16 * 2352 + 24:16 * 2352 + 64]: return 'ps1'
    return None

RELEASE_NOTES = []                     # audio tracks that differ from the Redump dump (find_disc), repeated at the end

RELEASE_DISC_NAMES = {'ps1': 'Mega Man 8 (USA) for PlayStation (SLUS-00453)', 'sat': 'Mega Man 8 (USA) for Sega Saturn (T-1216H)'}

def find_disc(kind, path):
    """The US disc of `kind` ('ps1' / 'sat') in `path`: a .cue file, or a folder holding its .cue and .bin files
    (searched with subfolders). Returns (cue, tracks); every problem ends with a message for the user."""
    other = {'ps1': 'sat', 'sat': 'ps1'}[kind]; label = {'ps1': 'PlayStation', 'sat': 'Saturn'}
    if not os.path.exists(path):
        raise SystemExit(f'Folder not found: {path}\n  Copy the folder holding the {label[kind]} disc (its .cue and .bin files) into '
                         f'"{os.path.dirname(path) or "."}" and rename it to "{os.path.basename(path)}".')
    if os.path.isdir(path):
        cues = [os.path.join(r, f) for r, _, fs in sorted(os.walk(path)) for f in sorted(fs) if f.lower().endswith('.cue')]
    elif path.lower().endswith('.cue'): cues = [path]
    else: raise SystemExit(f"{path}: give the disc's .cue file (or a folder holding it), not the .bin")
    if not cues:
        raise SystemExit(f'No .cue file in {path}\n  Put the {label[kind]} disc there as BIN/CUE (the .cue together with its .bin files).')
    notes = []
    for c in cues:
        try: tr = cue_tracks(c)
        except SystemExit as e: notes.append(f'  {c}: {e}'); continue
        if not os.path.exists(tr[0]['file']):
            notes.append(f'  {c}: its Track 1 file "{os.path.basename(tr[0]["file"])}" is not next to it'); continue
        k = disc_kind(tr[0])
        if k is None: notes.append(f'  {c}: not a PlayStation or Saturn disc image (raw BIN/CUE needed)'); continue
        if k == other:
            notes.append(f'  {c}: this is a {label[other]} disc; it belongs in the "{other}" folder'); continue
        print(f'  checking {os.path.basename(c)} ...', flush=True)
        m = _md5_range(tr[0]['file'], tr[0]['start'], tr[0]['end'])
        if m == RELEASE_IN_MD5[kind]:
            notes = check_audio_tracks(c, tr, RELEASE_AUDIO_MD5[kind])
            print(f'  found {RELEASE_DISC_NAMES[kind]} (Track 1 md5 OK' + (', audio tracks OK)' if not notes else ')'))
            for n in notes:
                print(f'  note: {n}'); RELEASE_NOTES.append(f'{RELEASE_DISC_NAMES[kind]}: {n}')
            return c, tr
        notes.append(f'  {c}: Track 1 md5 is {m}, expected {RELEASE_IN_MD5[kind]}: a different version or region, '
                     'or a modified/bad dump')
    raise SystemExit('\n'.join([f'\nCould not find {RELEASE_DISC_NAMES[kind]} in {path}:'] + notes +
                                ['  The builder needs a Redump-style BIN/CUE of the US disc (see the instructions).']))

def check_audio_tracks(cue, tr, expect):
    """Check a disc's audio tracks against the Redump tracks (`expect` = {track number: md5}, from RELEASE_AUDIO_MD5).
    A missing or extra track stops the builder (it needs the complete disc); a track whose md5 differs returns a note
    (usually a rip without read-offset correction: the same audio a few samples shifted). Returns the notes."""
    nos = [t['no'] for t in tr[1:]]; stop, notes = [], []
    if sorted(nos) != sorted(expect): stop.append(f'  the disc has tracks {[1] + nos}, expected {[1] + sorted(expect)}')
    for t in tr[1:]:
        if t['no'] not in expect: continue
        if not os.path.exists(t['file']):
            stop.append(f'  Track {t["no"]} ("{os.path.basename(t["file"])}") is missing'); continue
        m = _md5_range(t['file'], t['start'], t['end'])
        if m != expect[t['no']]: notes.append(f'Track {t["no"]} md5 is {m}, the Redump dump has {expect[t["no"]]}')
    if stop:
        raise SystemExit('\n'.join([f'\n{cue}: Track 1 is right, but the disc is not complete:'] + stop +
                                    ['  The builder needs the complete disc: the .cue together with every track\'s .bin '
                                     '(one .bin per track, or one .bin for the whole disc).']))
    return notes

def release_build(discs, out_dir, keep_work=False):
    """The release build (no debug options): extracts the user's discs into $MM8_WORK, builds the patched PS1
    Track 1, checks it (check_build) and writes a complete bin/cue (Track 1 new, Tracks 2-3 copied) into out_dir."""
    import shutil, tempfile
    pcue, ptr = discs['ps1']; scue, st = discs['sat']
    for t in ptr[1:]:
        if not os.path.exists(t['file']):
            raise SystemExit(f'{pcue}: Track {t["no"]} ("{os.path.basename(t["file"])}") is missing; the PlayStation disc needs every track')
    os.makedirs(out_dir, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix='mm8rel_', dir=WORK if os.path.isdir(WORK) else None)
    try:
        def track1(tr, name):              # Track 1 as its own file (a whole-disc .bin is cut at Track 2)
            t = tr[0]
            if t['start'] == 0 and t['end'] == os.path.getsize(t['file']): return t['file']
            p = os.path.join(tmp, name); _copy_range(t['file'], t['start'], t['end'], p); return p
        ps1_t1, sat_t1 = track1(ptr, 'ps1_track1.bin'), track1(st, 'sat_track1.bin')
        print('[1/4] Reading the discs ...', flush=True)
        for side, src in (('sat', sat_t1), ('ps1', ps1_t1)):
            d = os.path.join(WORK, side); os.makedirs(d, exist_ok=True)
            iso = os.path.join(d, 'track1.iso')
            raw_to_iso(src, iso)
            extract_iso(iso, os.path.join(d, 'files'))
            pacroot = os.path.join(d, 'pac')
            for root, _, fs in os.walk(os.path.join(d, 'files')):
                for fn in fs:
                    if fn.upper().endswith('.PAC'):
                        od = os.path.join(pacroot, fn.split('.')[0]); os.makedirs(od, exist_ok=True)
                        for t, o, b in unpac(os.path.join(root, fn)):
                            with open(os.path.join(od, f'{t:03x}.bin'), 'wb') as fo: fo.write(b)
            os.remove(iso)
        _file.cache_clear()
        print('[2/4] Porting the Saturn content and building the PlayStation image ...', flush=True)
        t1_name = f'{RELEASE_NAME} (Track 1).bin'
        out_t1 = os.path.join(out_dir, t1_name)
        tempfile.tempdir = tmp             # the compiler's scratch folders go into tmp and are removed with it
        with build_cache():
            build_image(ps1_t1, out_t1)
            print('[3/4] Checking the new image ...', flush=True)
            check_build(ps1_t1, out_t1)
        print('[4/4] Writing the other tracks and the .cue ...', flush=True)
        lines = [f'FILE "{t1_name}" BINARY', f'  TRACK 01 {ptr[0]["mode"]}', '    INDEX 01 00:00:00']
        tf = lambda b: '%02d:%02d:%02d' % (b // 2352 // 75 // 60, b // 2352 // 75 % 60, b // 2352 % 75)
        for t in ptr[1:]:
            name = f'{RELEASE_NAME} (Track {t["no"]}).bin'
            _copy_range(t['file'], t['start'], t['end'], os.path.join(out_dir, name))
            lines += [f'FILE "{name}" BINARY', f'  TRACK {t["no"]:02d} {t["mode"]}']
            for i, b in sorted(t['idx'].items()): lines.append(f'    INDEX {i:02d} {tf(b - t["start"])}')
        with open(os.path.join(out_dir, f'{RELEASE_NAME}.cue'), 'w', newline='\r\n') as f: f.write('\n'.join(lines) + '\n')
        with open(out_t1, 'rb') as f:
            h = hashlib.md5()
            for b in iter(lambda: f.read(1 << 22), b''): h.update(b)
        return h.hexdigest()
    finally:
        tempfile.tempdir = None
        shutil.rmtree(tmp, ignore_errors=True)
        if not keep_work:
            for side in ('sat', 'ps1'): shutil.rmtree(os.path.join(WORK, side), ignore_errors=True)

def release_main(argv=None):
    """Entry point of the release builder (also `mm8tool.py release`)."""
    ap = argparse.ArgumentParser(prog='mm8tool.py release', description=f'{RELEASE_NAME} v{RELEASE_VERSION} builder')
    dd = os.environ.get('MM8_DISCS', 'discs')
    ap.add_argument('--ps1', default=os.path.join(dd, 'ps1'), help='US PlayStation disc: its .cue or a folder holding it (default: discs/ps1)')
    ap.add_argument('--sat', default=os.path.join(dd, 'sat'), help='US Saturn disc: its .cue or a folder holding it (default: discs/sat)')
    ap.add_argument('--out', default=os.environ.get('MM8_OUTPUT', 'output'), help='folder for the finished bin/cue (default: ./output)')
    ap.add_argument('--keep-work', action='store_true', help='keep the extracted disc files in $MM8_WORK')
    a = ap.parse_args(argv)
    print(f'{RELEASE_NAME} v{RELEASE_VERSION} builder\n')
    print('Looking for the discs ...', flush=True)
    discs = {'ps1': find_disc('ps1', a.ps1), 'sat': find_disc('sat', a.sat)}
    if os.path.isdir(os.path.join(WORK, 'sat')) or os.path.isdir(os.path.join(WORK, 'ps1')):
        raise SystemExit(f'{WORK} already holds extracted discs; give MM8_WORK an empty folder')
    os.makedirs(WORK, exist_ok=True)
    md5 = release_build(discs, a.out, a.keep_work)
    print(f'\nDone. Your image is in: {os.path.abspath(a.out)}')
    print(f'  Open "{RELEASE_NAME}.cue" in your emulator (or burn the .cue).')
    print(f'  Track 1 md5: {md5}' + ('  (matches the tested build)' if md5 == RELEASE_OUT_MD5 else
          '\n  Note: this differs from the tested build (another compiler/library version made slightly different code). '
          'The image passed its checks, but please mention this md5 if you report a problem.'))
    if RELEASE_NOTES:
        print('\n  Note: these audio tracks are not byte-identical to the Redump dump:')
        for n in RELEASE_NOTES: print(f'    {n}')
        print('  This usually means the ripping program did not correct the drive\'s read offset: the music is the same, '
              'shifted by a fraction of a millisecond, and the build is fine. The PlayStation audio tracks were copied as '
              'you gave them. For an exact match, dump the discs with redumper, DiscImageCreator or MPF.')

def main():
    argv = sys.argv[1:]
    if argv[:1] == ['release']: argv = argv[1:]
    release_main(argv)

if __name__ == '__main__':
    main()
