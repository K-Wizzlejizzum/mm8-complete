#!/usr/bin/env python3
"""
mm8tool.py - Mega Man 8 - Complete toolkit (single file): Saturn->PS1 ports and the PS1 image build.

Requires: python3, pycdlib, capstone>=6 (SuperH), numpy
    pip install pycdlib "capstone>=6.0.0a1" --pre --break-system-packages
    build / selftest-cc: xdelta3, gcc-mipsel-linux-gnu, binutils-mipsel-linux-gnu (apt)
    difftest-m2: unicorn; audio-compare / calibrate: scipy, ffmpeg (+ mednafen, xvfb, xdotool, BIOS files)
    A host C compiler (gcc/cc), if present, is used for the ADPCM encoder (same output, ~50x faster).

Workspace root: $MM8_WORK (default ./mm8work)
    sat/files, sat/pac/<PAC>/<type>.bin   (Saturn)
    ps1/files, ps1/pac/<PAC>/<type>.bin   (PS1)
helper_map.txt / globals_map.txt are read from this file's directory.

Commands:
    setup --saturn SAT_TRACK1.bin --ps1 PS1_TRACK1.bin   extract both discs (.bin raw or .iso)
    release [--ps1 P] [--sat P] [--out DIR] [--keep-work] the release builder (v0.2.0 = Phases 1-2, no debug options): finds the
                                                         US PS1 / US Saturn disc (.cue or folder; default discs/ps1, discs/sat),
                                                         checks every track against the Redump md5s (Track 1 must
                                                         match; audio tracks give a note), extracts them into $MM8_WORK, builds
                                                         and checks the image, writes a full bin/cue into --out. The
                                                         Windows bundle runs it with its own GCC 12.4.0 (MM8_CROSS = toolchain
                                                         prefix, default mipsel-linux-gnu-)
    verify                                               re-check every key finding (PASS/FAIL)
    build --ps1 TRACK1.bin --out OUT.bin [--xdelta P] [--quick-start 8|9 [--checkpoint 5]] [--keep-c DIR]
                                                         current patch: track 1 extended; restored Search Man stage
                                                         (map, sprites, text, doors, placements) + Wood Man/leaf lifted
                                                         to C in the STAGE08 overlay + PBGM46/PBGM47; restored Duo
                                                         mini-stage (hidden room, arena, placements) + Cut Man and the
                                                         P5 0x34 alarm lifted to C in the STAGE09 overlay + PBGM48/PBGM49;
                                                         play_bgm hook + sound guard; M7a: title menu with BONUS MODE as
                                                         4th item (EXE + DEMO.BIN hook) + bonus overlay at 0x801E4690; M7b:
                                                         the Saturn bonus menu lifted to C (m7b_lift) + converted SLABO.PAC
                                                         (m7b_slabo_pac); M7c-M7e/M7g sub-screens; then check_build (sectors,
                                                         EDC/ECC, music entries, DEMO/bonus/SLABO, xdelta). --quick-start/--checkpoint = debug only
    MednafenLive(bin, home)                              interactive headless session for menus (key, snap, state)
    difftest [--boss wood|cut|bonus] [-n 300] [--kinds wood,leaf | cut,alarm | p4_41,p4_40,spawn,menu] [--frames 10]
                                                         differential test of the lifted code (SH-2 interpreter vs
                                                         unicorn MIPS); difftest-m2 = the same command
    calibrate --saturn SAT_TRACK1.bin --ps1 PS1_TRACK1.bin [--bank WOOD2.PAC|CUT2.PAC] [--workdir calib] [--sat-meas F]
                                                         emulator calibration of a converted bank (~4-9 min; run it in
                                                         the background). Whole songs: sat_music_image /
                                                         ps1_title_music_image + mednafen_session + song_compare.
                                                         In-game headless runs: patch_test_start (scratch images only)
    audio-compare --saturn REC --ps1 REC [--bank WOOD2.PAC]
                                                         per-(channel, velocity) levels of two recordings of a bank's song
    map-helpers [--out FILE] / map-globals [--out FILE]  rebuild helper_map.txt / globals_map.txt
    catalog LO HI                                        everything Saturn code [LO,HI) references, with PS1 mappings
    refs LO HI [--in EXE,STAGE08|all]                    PS1 code that uses a fixed address in [LO,HI)
    dis-sat ADDR [N] / dis-ps1 ADDR [N] [--ovl STAGE09]  disassembly (Saturn SH-2 / PS1 MIPS)
    pac PATH / places STAGE [sat|ps1]                    list PAC chunks / dump a placement list (chunk 0x0A)
    selftest-cc                                          compile a test handler with mipsel GCC + R3000 check
    pre-release checks:
    ending-image --ps1 TRACK1.bin --out OUT.bin          scratch: play_bgm -> ending song (CD track 2 via entry 138);
                                                         run as a full disc (full_disc_cue) and locate the recording in
                                                         Track 2 with cd_track_position
    stack-report [STATE.mcN ...]                         lifted code's C frames + SH-2 stack bound; stack watermarks of
                                                         Mednafen save states (mdfn_ram, stack_watermark)
    difftest ... --uninit                                + MIPS stack reads before write / reads outside loaded memory
    regress --orig T1.bin --rel T1.bin --out DIR [--configs 8,8:2,...]   headless runs of every stage, both images
    regress-compare DIR                                  sound + save-state comparison of the regress runs
    setup --jp JP_TRACK1.bin                             also extract the JP PS1 disc (Rockman 8) into jp/ (reference;
                                                         enables the JP checks in verify)
    region-compare --us US_TRACK1.bin --jp JP_TRACK1.bin  US vs JP PS1 disc, file by file (region-select survey)
    m8-compare                                           US Saturn vs US PS1 (Phase 3 catalogue, M8a): shared PACs chunk by
                                                         chunk, every stage's placements, sound-ID tables
"""
import os, sys, re, struct, hashlib, argparse, collections, random, functools

WORK = os.path.abspath(os.environ.get('MM8_WORK', './mm8work'))
SAT_FILES = os.path.join(WORK, 'sat', 'files')
PS1_FILES = os.path.join(WORK, 'ps1', 'files')
JP_FILES = os.path.join(WORK, 'jp', 'files')      # JP PS1 (Rockman 8, SLPS-00630), optional: setup --jp

KNOWN_MD5 = {
    ('sat', '2NDREAD.PRG'): '2be6fd7b4cc6fa250c129caba96d5cae',
    ('sat', '1STREAD.PRG'): 'dd8007babc65f4d4ce46aecf65eccf41',
    ('ps1', 'SLUS_004.53'): '4254a7176d09d5ad58bfc7c4eefbe20a',
    ('ps1', 'OVL/STAGE08.BIN'): '7147c0c48c431bc7be2206b398925181',
    ('ps1', 'OVL/STAGE09.BIN'): 'f0bca16e43a42a4cbedc7a552ec938b3',
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

# Copyright guard (03 §7): the tool holds no game code or data. Game code and data are compared through fp()
# fingerprints, and words the tool writes are assembled (m_i / m_r, jumps from addresses). tool_game_words() lists every
# hex literal of 5+ digits in this file that is an aligned 32-bit word of the PS1 EXE / overlays or the Saturn programs;
# addresses, round numbers (low 16 bits zero) and the generic masks / encodings below are allowed.
TOOL_GENERIC_WORDS = {0xFFFFFFFF, 0xFFFF0000, 0x3FFFFFF, 0x1FFFFFFF, 0x1FFFFF, 0xFFFFF, 0xFFFFFF, 0x1FFFF, 0x7FFFFFFF,
                      0x03E00008, 0x0000000D, 0x40000000, 0x60000000, 0x808080, 0x1E000, 0x9F000, 0x113000, 0x26000}

def _addr_word(v): return (0x80000000 <= v < 0x80200000 or 0x06000000 <= v < 0x06100000 or 0x00200000 <= v < 0x00300000
                          or 0x25000000 <= v < 0x26100000 or 0x1F800000 <= v < 0x1F810000)

def tool_game_words(path=None):
    """[(line, value)] of literals in this file that equal a word of either game's code files (see above)."""
    ps1 = [os.path.join(dp, f) for dp, _, fs in os.walk(PS1_FILES) for f in fs
           if f == 'SLUS_004.53' or os.path.basename(dp) == 'OVL' or re.fullmatch(r'STAGE[0-9A-D]{2}B?', f)]
    sat = [os.path.join(dp, f) for dp, _, fs in os.walk(SAT_FILES) for f in fs if re.search(r'\.(PRG|BIN)$', f)]
    words = set()
    for f in ps1:
        d = open(f, 'rb').read(); words.update(struct.unpack('<%dI' % (len(d) // 4), d[:len(d) // 4 * 4]))
    for f in sat:
        d = open(f, 'rb').read()
        for o in (0, 2): words.update(struct.unpack('>%dI' % ((len(d) - o) // 4), d[o:o + (len(d) - o) // 4 * 4]))
    hits = []
    for ln, line in enumerate(open(path or os.path.abspath(__file__), encoding='utf-8'), 1):
        for m in re.finditer(r'\b0x([0-9A-Fa-f_]{5,10})\b', line):
            v = int(m.group(1).replace('_', ''), 16)
            if v in words and v & 0xFFFF and not _addr_word(v) and v not in TOOL_GENERIC_WORDS: hits.append((ln, v))
    return hits

def place_rec(side, stage, i):
    """Record i of a stage's placement list (chunk 0x0A), read from the disc, in the PS1 byte layout."""
    t = placements(side, stage)[i]
    return struct.pack('>2H', t[0], t[1]) + struct.pack('<2H', t[2], t[3])

def rec_tuple(r): return struct.unpack('>2H', r[:4]) + struct.unpack('<2H', r[4:8])

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

def _edc(data):
    e = 0
    for b in data: e = (e >> 8) ^ _EDC[(e ^ b) & 0xFF]
    return e

def _ecc_part(s, major_count, minor_count, major_mult, minor_inc, dest):
    size = major_count * minor_count
    for major in range(major_count):
        index = (major >> 1) * major_mult + (major & 1); a = b = 0
        for _ in range(minor_count):
            t = s[0xC + index]; index += minor_inc
            if index >= size: index -= size
            a ^= t; b ^= t; a = _ECC_F[a]
        a = _ECC_B[_ECC_F[a] ^ b]
        s[dest + major] = a; s[dest + major + major_count] = a ^ b

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

def fix_m1(s):
    """Recompute EDC + ECC of one raw 2352-byte Mode 1 sector (Saturn; bytearray, in place; header included in ECC)."""
    s[0x810:0x814] = _edc(s[0:0x810]).to_bytes(4, 'little'); s[0x814:0x81C] = bytes(8)
    _ecc_part(s, 86, 24, 2, 86, 0x81C); _ecc_part(s, 52, 43, 86, 88, 0x8C8)
    return s

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

class SatRawDisc(RawDisc):
    """Raw MODE1/2352 Saturn track 1 (user data at +16; LBA n = sector n of the file)."""
    UD = 16
    def finish(self):
        for sec in self.touched:
            s = bytearray(self.d[sec * 2352:(sec + 1) * 2352]); assert s[15] == 1
            self.d[sec * 2352:(sec + 1) * 2352] = fix_m1(s)

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

# ---- stage PAC loading. The streamer (0x800FB9C0 start, 0x800FBB18 per-sector callback) reads the
# header sector (u32 count, u32 file size, count x (u32 type, u32 size)), then for each entry copies exactly `size`
# bytes (min(left, 0x800) per sector, 0x800FC068) to RAM dest_table[type & 0x7F]; each chunk starts on a new sector.
# Types >= 0x100 go to VRAM (handler table 0x80137600 indexed by type >> 8). It stops after the file-table size;
# the header count/size words are written but never read. Stage PACs use dest table 0x80137850.
PS1_PAC_DEST = 0x80137850
PS1_SCREEN_STAGING, PS1_SCREEN_LIVE, PS1_SCREEN_AREA = 0x80190040, 0x80171C3C, 0x1E000   # copied after load (0x800FF3A4)

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

def pac_ram(entries, dest):
    """Simulate the streamer: {RAM address: byte} written by the non-VRAM chunks. dest(type) -> address."""
    ram = {}
    for t, b in entries:
        if t >= 0x100: continue
        a = dest(t & 0x7F)
        for i in range(0, len(b) + (-len(b) % 4)):        # copies are word-rounded (sector padding is zero)
            ram[a + i] = b[i] if i < len(b) else 0
    return ram

# MIPS encoders (only what the patches need)
def m_i(op, rs, rt, imm): return op << 26 | rs << 21 | rt << 16 | (imm & 0xFFFF)
def m_r(rs, rt, rd, sa, fn): return rs << 21 | rt << 16 | rd << 11 | sa << 6 | fn
def hi_lo(addr):
    lo = addr & 0xFFFF; return ((addr + 0x8000) >> 16) & 0xFFFF, lo - 0x10000 if lo & 0x8000 else lo
def patch_lui_addiu(word_lo, word_hi_insn, addr):
    """Keep registers of an existing lui/addiu pair, replace the address."""
    h, l = hi_lo(addr); return (word_hi_insn & 0xFFFF0000) | h, (word_lo & 0xFFFF0000) | (l & 0xFFFF)

# Debug quick start (testing only, never in a release): the progress sequencer 0x80100A34, phase 0 (new game),
# stores stage 0 (intro) into 0x801C336E. Rewritten to store `stage` instead, keeping the mode-byte store
# (0x801CF840 = 3) by moving it into the delay slot of the following `j`.
QS_ADDR = 0x80100A8C
QS_ORIG_FP = '6160d95dbb7c'                 # fingerprint of the 6 original words (patch site check)

def quick_start_words(stage):
    return [m_i(0x0D, 0, 8, stage),            # ori  t0, zero, stage
            m_i(0x0F, 0, 1, 0x801C),           # lui  at, 0x801C
            m_i(0x28, 1, 8, 0x336E),           # sb   t0, 0x336E(at)   -> current stage
            m_i(0x0F, 0, 1, 0x801D),           # lui  at, 0x801D
            2 << 26 | (0x80100AE8 >> 2) & 0x3FFFFFF,   # j 0x80100AE8
            m_i(0x28, 1, 2, -0x7C0)]           # sb   v0, -0x7C0(at)   (delay slot; v0 = 3)

# Debug quick-start checkpoint: stage start (0x80100B14) clears the checkpoint index 0x801C3371, which selects the start
# record table 0x80138288[stage][checkpoint] (0x18 bytes: 8 flag bytes, player X/Y, camera X/Y, camera bounds; applied
# by 0x80101814). Stage 8 checkpoint 5 = (0x1180, 0x400), camera locked at (0x10E0, 0x400): the corridor between Wood
# Man's two gates (same record on the Saturn, 0x0027D4C0 + 5 * 0x18).
QS_CP_ADDR = 0x80100B14
QS_CP_ORIG_FP = '244a6352c275'             # fingerprint of: lui at, 0x801C; sb zero, 0x3371(at)

def patch_quick_start(disc, stage, checkpoint=None):
    assert fp(disc.exe_words(QS_ADDR, 6)) == QS_ORIG_FP, 'unexpected code at 0x80100A8C'
    disc.exe_write_words(QS_ADDR, quick_start_words(stage))
    if checkpoint is not None:
        assert fp(disc.exe_words(QS_CP_ADDR, 2)) == QS_CP_ORIG_FP
        # s0 = 0x801C336C in this function (set at 0x80100A3C, callee-saved); t0 is dead
        disc.exe_write_words(QS_CP_ADDR, [m_i(0x0D, 0, 8, checkpoint), m_i(0x28, 16, 8, 5)])   # ori t0, zero, cp; sb t0, 5(s0)

def patch_test_start(disc, stage, checkpoint, player, camera, bounds_entry):
    """Test images only (never in a release): rewrite start record 0x80138288[stage][checkpoint] so the
    stage starts at `player` (x, y) with the camera at `camera` and the camera bounds of table entry `bounds_entry`
    (0x8013E020, 8 bytes). Stage 9: player (0x6A0, 0x4B0), camera (0x600, 0x400), entry 0x7B = Cut Man's hidden room;
    stage 5 cp 2: player (0xD40, 0xA40), camera (0xD00, 0xA00), entry 0x21 = the room of bolt 19's floor spot. Also used by
    `build --test-start` for test images (never in a release)."""
    tab = disc.exe_words(0x80138288 + 4 * stage, 1)[0]
    rec = disc.exe_words(tab + 4 * checkpoint, 1)[0]
    b = disc.exe_read(0x8013E020 + 8 * bounds_entry, 8)
    disc.exe_write(rec + 8, struct.pack('<4H', *player, *camera) + b)
    return rec

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

SAT_MASTER = 0x27D728          # 317 handler pointers: P0@0 P1@20 P2@28 P3@129 P4@181 P5@247
SAT_P2 = 0x27D798              # enemy table, 101 entries (0x00-0x64)
CUT, WOOD, T64 = 0x257E60, 0x258F00, 0x259B7C

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
PS1_P4_TABLE, PS1_P4_LEN = 0x8015029C, 64   # EXE; entries outside the EXE are stage-specific overlay functions
PS1_P5_TABLE, PS1_P5_LEN = 0x8015039C, 52   # EXE; e.g. P5[0x30] = 0x801E0C6C in STAGE08 (PS1-only Wood Man stand-in)
PS1_POOL_ALLOC = 0x80138474                 # 6 allocator pointers: pool byte 0/1 = P2, 2 = P3, 3 = P4, 4 = P5
PS1_TASK_BASE = 0x801FC000       # task scheduler: 4 slots x 0x50 @0x801FC000, task stacks 4 x 0x400 @0x801FEC00

def ps1_absrefs(ps, img):
    """Fixed addresses built by `lui r,H` + addiu/ori/load/store on r within 12 insns.
    Returns [(insn_addr, target, mnemonic)]. Misses addresses read from data tables or computed at runtime."""
    key = (img.b, img.d)
    if key in _ABSREFS: return _ABSREFS[key]
    ins = list(ps.md.disasm_lite(img.d, img.b)); out = []
    for k, (_, _, mn, ops_s) in enumerate(ins):
        if mn != 'lui': continue
        r, h = ops_s.split(', '); h = int(h, 16); rp = f'({r})'
        for ja, _, jm, jo in ins[k + 1:k + 12]:
            ops = jo.split(', ')
            if jm in ('addiu', 'ori') and len(ops) == 3 and ops[1] == r:
                v = int(ops[2], 0); out.append((ja, ((h << 16) + (v if jm == 'addiu' else v & 0xFFFF)) & 0xFFFFFFFF, jm))
                if ops[0] == r: break
            elif rp in jo:
                out.append((ja, ((h << 16) + int(ops[-1].split('(')[0], 0)) & 0xFFFFFFFF, jm))
            elif ops and ops[0] == r and jm not in ('sw', 'sh', 'sb'): break
    _ABSREFS[key] = out
    return out
_ABSREFS = {}

def ps1_images(ps, which='all'):
    names = ['EXE'] + sorted(f[:-4] for f in os.listdir(os.path.join(PS1_FILES, 'OVL')) if f.upper().endswith('.BIN')) if which == 'all' else which.split(',')
    return [(n, ps.exe if n == 'EXE' else ps.ovl(n)) for n in names]

def overlay_tables(ps):
    """Per stage overlay with an enemy table: (img, table, 150 entries, stub filler, sorted handler starts + table)."""
    for st in [f'STAGE0{c}' for c in '0123456789ABCD']:
        img = ps.ovl(st); t = ps.find_enemy_table(img)
        if t is None: continue
        tab = [img.u32(t + 4 * k) for k in range(150)]
        filler = collections.Counter(tab[:PS1_P2_LEN]).most_common(1)[0][0]
        yield img, t, tab, filler, sorted(set(v for v in tab if img.b <= v < img.e)) + [t]

# ----------------------------------------------------------------- helper mapping
def _align(a, b, mp):
    n, m, G = len(a), len(b), -0.3
    D = [[0.0] * (m + 1) for _ in range(n + 1)]; P = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1): D[i][0], P[i][0] = G * i, 1
    for j in range(1, m + 1): D[0][j], P[0][j] = G * j, 2
    mapped = set(mp.values())
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            x, y = a[i - 1], b[j - 1]
            s = (1.0 if mp[x] == y else -1.0) if x in mp else (0.1 if y not in mapped else -0.5)
            best = (D[i - 1][j - 1] + s, 0)
            if D[i - 1][j] + G > best[0]: best = (D[i - 1][j] + G, 1)
            if D[i][j - 1] + G > best[0]: best = (D[i][j - 1] + G, 2)
            D[i][j], P[i][j] = best
    i, j, out = n, m, []
    while i > 0 or j > 0:
        p = P[i][j]
        if i > 0 and j > 0 and p == 0: out.append((a[i - 1], b[j - 1])); i -= 1; j -= 1
        elif i > 0 and (p == 1 or j == 0): i -= 1
        else: j -= 1
    return out

SEED = {0x060167D4: 0x80109080, 0x0601454C: 0x8010544C, 0x06014342: 0x80105120, 0x060140C2: 0x80104C34}
# Hand-verified rows. They override the vote, and any auto row with the same PS1 target is dropped.
MANUAL = {
    0x00278ED0: 0x8011C5D4,   # set camera bounds: table[idx*8] -> camera +0x12..+0x18
    0x06012E70: 0x80102E84,   # store arg in mode byte, schedule task slot 2
    0x06016AA6: 0x80101BF0,   # rect around camera (-48 / +0x170 / +0x120) -> inner call
    0x0600C2B8: 0x800FCD84,   # sound-driver call at boss-fight start (6 bosses agree)
    0x060081B4: 0x800F7F58,   # 1STREAD task routine, called from 0x06012E70
    0x0600813E: 0x800F7E08,   # 1STREAD schedule task (slot, handler)
    0x0600E298: 0x80109CB8,   # task handler passed by 0x06012E70 (function pointer, not called)
}
# Saturn compiler runtime: no PS1 function, MIPS does it inline. NOTE args are r1 (dividend), r0 (divisor).
INLINE = {0x060196A4: 'signed divide r1/r0 -> PS1 inline div+mflo', 0x06019800: 'signed modulo r1%r0 -> PS1 inline div+mfhi',
          0x06019A98: 'variable shift via jump table (r1 = count) -> PS1 inline sllv/srav'}
BOSS_HELPERS = [0x0020e8aa, 0x0020e90a, 0x0020ec0c, 0x0026aad8, 0x00278ed0, 0x0600c2b8, 0x0600c358, 0x0600c610,
    0x06012e70, 0x0601346c, 0x06013ea0, 0x06013ed2, 0x06013f66, 0x06013fcc, 0x060140c2, 0x06014110, 0x0601412a,
    0x06014134, 0x06014166, 0x060141a8, 0x06014324, 0x06014342, 0x0601454c, 0x06014582, 0x06014624, 0x060146ae,
    0x0601470c, 0x06014788, 0x06014d80, 0x06015700, 0x060158b0, 0x060166b4, 0x060166e2, 0x060167d4, 0x0601682e,
    0x06016aa6, 0x060196a4, 0x06019800]

def build_helper_map(iters=6):
    sat, ps = Sat(), Ps1()
    satT = [sat.u32(SAT_MASTER + 4 * k) for k in range(317)]
    ends = sorted(set(satT)) + [Sat.CODE_END]
    region = lambda a: (a, min(x for x in ends if x > a))
    pairs = []
    for img, t, tab, filler, hs in overlay_tables(ps):
        for k in range(PS1_P2_LEN):
            v = tab[k]
            if v == filler or not img.b <= v < img.e: continue
            a = sat.calls(*region(satT[28 + k])); b = ps.calls(img, v, min(x for x in hs if x > v))
            if a and b: pairs.append((a, b))
        for k in range(52):                                   # P3: Saturn master[129:]
            v = tab[PS1_P2_LEN + k]
            if not img.b <= v < img.e: continue
            a = sat.calls(*region(satT[129 + k])); b = ps.calls(img, v, min(x for x in hs if x > v))
            if a and b and len(b) >= len(a) // 2: pairs.append((a, b))   # skip delete-only stubs
    ps4 = [ps.exe.u32(PS1_P4_TABLE + 4 * k) for k in range(PS1_P4_LEN)]   # P4 table (EXE) vs Saturn master[181:]
    ex = sorted(set(ps4))
    for k in range(PS1_P4_LEN):
        v = ps4[k]; nx = [x for x in ex if x > v]
        a = sat.calls(*region(satT[181 + k])); b = ps.calls(ps.exe, v, nx[0] if nx else v + 0x400)
        if a and b: pairs.append((a, b))
    mp = dict(SEED); votes = {}
    for _ in range(iters):
        votes = collections.defaultdict(collections.Counter)
        for a, b in pairs:
            for x, y in _align(a, b, mp): votes[x][y] += 1
        new = {}
        for x, c in votes.items():
            (y, n), = c.most_common(1)
            if n >= 2 and n / sum(c.values()) >= 0.6: new[x] = y
        new.update(SEED)
        if new == mp: break
        mp = new
    # overlay addresses differ per stage, so they are not global helpers
    mp = {x: y for x, y in mp.items() if y < Ps1.OVL_BASE}
    taken = set(MANUAL.values())
    mp = {x: y for x, y in mp.items() if y not in taken or MANUAL.get(x) == y}
    mp.update(MANUAL)
    return mp, votes, len(pairs)

# ----------------------------------------------------------------- global (RAM variable) map (M2 prep)
SAT_RAM = (0x06020000, 0x06100000)
_SZ = {'b': 1, 'w': 2, 'l': 4}
_PSZ = {'lb': 1, 'lbu': 1, 'sb': 1, 'lh': 2, 'lhu': 2, 'sh': 2, 'lw': 4, 'sw': 4}

def sat_events(sat, lo, hi):
    """Ordered calls and global-memory accesses in Saturn code [lo,hi).
    Globals: @rN / @(d,rN) / @(r0,rN) where rN holds a literal RAM address (r0 from a preceding mov #imm,r0)."""
    ev = []; regs = {}; r0c = None
    isg = lambda v: SAT_RAM[0] <= v < SAT_RAM[1] and not sat.has(v)
    for ln in sat.dis_lines(lo, (hi - lo) // 2):
        a = int(ln[:8], 16); body = ln[16:].split(';')[0].strip(); parts = body.split(None, 1)
        mn = parts[0] if parts else ''; ops = parts[1] if len(parts) > 1 else ''
        m = re.search(r'; =([0-9a-f]{8})', ln)
        if mn == 'mov.l' and m and ops.startswith('0x'):
            regs[ops.split(',')[-1]] = int(m.group(1), 16); continue
        if mn == 'mov' and re.match(r'#-?\d+,r0$', ops): r0c = int(ops[1:].split(',')[0])
        mm = re.search(r'(jsr|jmp) @(r\d+)', body)
        if mm: ev.append(('call', a, regs.get(mm.group(2)))); continue
        if mn.startswith('mov.') and '@' in ops:
            if ops.startswith('@('): src, dst = ops[:ops.index(')') + 1], ops[ops.index(')') + 2:]
            else: src, dst = ops.split(',', 1)
            mem = (src if '@' in src else dst).strip(); rw = 'r' if '@' in src else 'w'; addr = None
            m1 = re.match(r'@(r\d+)$', mem); m2 = re.match(r'@\((-?\d+),(r\d+)\)$', mem); m3 = re.match(r'@\(r0,(r\d+)\)$', mem)
            if m1 and m1.group(1) in regs: addr = regs[m1.group(1)]
            elif m2 and m2.group(2) in regs: addr = regs[m2.group(2)] + int(m2.group(1))
            elif m3 and m3.group(1) in regs and r0c is not None: addr = regs[m3.group(1)] + r0c
            if addr is not None and isg(addr): ev.append(('mem', a, (addr, _SZ[mn[-1]], rw)))
        if ops and not ops.endswith(')'):
            d = ops.split(',')[-1].strip()
            if re.match(r'r\d+$', d) and not (mn.startswith('mov.') and '@' in d):
                regs.pop(d, None)
                if d == 'r0' and not (mn == 'mov' and ops.startswith('#')): r0c = None
    return ev

def ps1_events(ps, img, lo, hi, cache):
    if id(img) not in cache: cache[id(img)] = {a: (t, m) for a, t, m in ps1_absrefs(ps, img)}
    ar = cache[id(img)]; ev = []
    for ia, _, mn, ops in ps.md.disasm_lite(img.d[lo - img.b:hi - img.b], lo):
        if mn == 'jal': ev.append(('call', ia, int(ops, 16)))
        elif ia in ar and ar[ia][1] in _PSZ:
            t, m = ar[ia]
            if 0x80100000 <= t < 0x80200000: ev.append(('mem', ia, (t, _PSZ[m], 'r' if m[0] == 'l' else 'w')))
    return ev

def shared_pairs(sat, ps, mp):
    """(sat_lo, sat_hi, ps1_img, ps1_lo, ps1_hi) for mapped helpers, EXE P4 handlers and overlay P2/P3 handlers."""
    satT = [sat.u32(SAT_MASTER + 4 * k) for k in range(317)]
    starts = sorted(set(list(mp) + satT + [Sat.CODE_END]))
    se = lambda a: min([x for x in starts if x > a] or [a + 0x400])
    ps4 = [ps.exe.u32(PS1_P4_TABLE + 4 * k) for k in range(PS1_P4_LEN)]
    exe_starts = sorted(set(list(mp.values()) + [v for v in ps4 if ps.exe.has(v)]))
    pe = lambda b: min([x for x in exe_starts if x > b] or [b + 0x400])
    out = [(a, se(a), ps.exe, b, pe(b)) for a, b in mp.items()]
    out += [(satT[181 + k], se(satT[181 + k]), ps.exe, ps4[k], pe(ps4[k])) for k in range(PS1_P4_LEN) if ps.exe.has(ps4[k])]
    for img, t, tab, filler, hs in overlay_tables(ps):
        for k in range(150):
            v = tab[k]
            if v == filler or not img.b <= v < img.e: continue
            sa = satT[28 + k] if k < PS1_P2_LEN else satT[129 + k - PS1_P2_LEN]
            out.append((sa, se(sa), img, v, min(x for x in hs if x > v)))
    return out

def load_helper_map(path=None):
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'helper_map.txt')
    mp = {}
    for ln in open(path):
        if ln.startswith('#'): continue
        p = ln.split()
        if len(p) > 1 and p[1] != '--------': mp[int(p[0], 16)] = int(p[1], 16)
    return mp

def build_global_map(mp=None):
    """Saturn->PS1 RAM variable map. In each shared function pair, align the call sequences (helper map),
    then pair global accesses between matching calls when count and access sizes agree. Vote over all pairs.
    Returns {sat_addr: (ps1_addr, votes, total)}."""
    import difflib
    sat, ps = Sat(), Ps1(); mp = mp or load_helper_map(); cache = {}; votes = collections.defaultdict(collections.Counter); seen = set()
    for sa, send, img, pa, pend in shared_pairs(sat, ps, mp):
        if (sa, pa) in seen or send - sa > 0x4000 or pend - pa > 0x8000: continue
        seen.add((sa, pa))
        E = sat_events(sat, sa, send); F = ps1_events(ps, img, pa, pend, cache)
        sc = [i for i, e in enumerate(E) if e[0] == 'call']; pc = [i for i, e in enumerate(F) if e[0] == 'call']
        A = [mp.get(E[i][2], ('?', E[i][2])) for i in sc]; B = [F[i][2] for i in pc]
        anchors = [(-1, -1)]
        for blk in difflib.SequenceMatcher(None, A, B, autojunk=False).get_matching_blocks():
            anchors += [(sc[blk.a + t], pc[blk.b + t]) for t in range(blk.size)]
        anchors.append((len(E), len(F)))
        for (i0, j0), (i1, j1) in zip(anchors, anchors[1:]):
            x = [e[2] for e in E[i0 + 1:i1] if e[0] == 'mem']; y = [e[2] for e in F[j0 + 1:j1] if e[0] == 'mem']
            if x and len(x) == len(y) and [q[1:] for q in x] == [q[1:] for q in y]:
                for p, q in zip(x, y): votes[p[0]][q[0]] += 1
    return {x: (c.most_common(1)[0][0], c.most_common(1)[0][1], sum(c.values())) for x, c in votes.items()}

def load_global_map(path=None):
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'globals_map.txt')
    gm = {}
    if os.path.exists(path):
        for ln in open(path):
            if ln.startswith('#'): continue
            p = ln.split(); gm[int(p[0], 16)] = int(p[1], 16)
    return gm

# Hand-verified globals (override / add to the voted map)
GLOBALS_MANUAL = {0x0602DAE7: 0x8016D2FB, 0x0602DAE9: 0x8016D2FD, 0x0602DAEB: 0x8016D2FF, 0x0602DAF1: 0x8015D225,
                  0x0602FEF0: 0x801D2914, 0x0602FF98: 0x8015E23C, 0x0602DA86: 0x801C336E, 0x0602DA84: 0x801C336C,
                  0x06036230: 0x8016DC08}   # current weapon: same place in the shared boss intros

def infer_global(v, gm, radius=0x10, need=3):
    """Block inference for an unmapped Saturn global: if >= `need` mapped neighbours within +-radius share one
    Saturn->PS1 offset, return ('block', v + offset). A hypothesis, not a verified mapping."""
    d = collections.Counter(gm[k] - k for k in gm if abs(k - v) <= radius and k != v)
    if d:
        off, n = d.most_common(1)[0]
        if n >= need: return ('block', (v + off) & 0xFFFFFFFF)
    return None

# ----------------------------------------------------------------- Saturn code catalogue
def catalog(lo, hi, mp=None, gm=None):
    """Classify everything Saturn code [lo,hi) references. Returns dict of lists."""
    sat = Sat(); mp = mp or load_helper_map(); gm = gm if gm is not None else load_global_map()
    lines = sat.dis_lines(lo, (hi - lo) // 2); lit = collections.defaultdict(list); calls = set(); regs = {}; bsr = set()
    for ln in lines:
        a = int(ln[:8], 16); m = re.search(r'mov\.l 0x([0-9a-f]+),(r\d+)\s+; =([0-9a-f]{8})', ln)
        if m: v = int(m.group(3), 16); regs[m.group(2)] = v; lit[v].append(a); continue
        m = re.search(r'(jsr|jmp) @(r\d+)', ln)
        if m and m.group(2) in regs: calls.add(regs.pop(m.group(2)))
        m = re.search(r'\bbsr 0x([0-9a-f]+)', ln)
        if m: bsr.add(int(m.group(1), 16))
    out = collections.defaultdict(list)
    for v, where in sorted(lit.items()):
        if v in calls and sat.has(v) and (v < Sat.CODE_END or v >= Sat.BASE1) and not lo <= v < hi:
            out['helpers'].append((v, len(where), mp.get(v, INLINE.get(v, 'UNMAPPED'))))
        elif lo <= v < hi: out['internal'].append((v, len(where)))
        elif 0x0027C000 <= v < 0x00300000: out['data'].append((v, len(where)))
        elif 0x00200000 <= v < 0x0027C000: out['code_ptrs'].append((v, len(where)))
        elif SAT_RAM[0] <= v < SAT_RAM[1] and not sat.has(v):
            out['globals'].append((v, len(where), GLOBALS_MANUAL.get(v, gm.get(v)) or infer_global(v, gm)))
    out['bsr'] = sorted(bsr)
    return out

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

# ----------------------------------------------------------------- stage render / palette animation (M3)
# Live palettes: chunk 9 is kept at 0x8015A064 and copied to the working palettes 0x80158F64 (PS1, Mednafen states);
# palette animation (0x80103540, 4 channels at 0x801B28FC) copies 16-colour frames from chunk 0xB (RAM 0x801AE840) into
# palette `dest`: script entries (dest, frame, palettes, frames to hold), 0xFF = loop back. Scripts per stage section:
# 0x8013DFE8[stage] -> [section byte 0x801C3376] -> 4 script pointers.
def stage_palettes(chunk9, side):
    """chunk 9 -> numpy (128, 16, 3) RGB888 (STP bit ignored)."""
    import numpy as np
    v = struct.unpack(('>' if side == 'sat' else '<') + '%dH' % (len(chunk9) // 2), chunk9)
    return np.array([[(c & 31) * 255 // 31, (c >> 5 & 31) * 255 // 31, (c >> 10 & 31) * 255 // 31] for c in v], np.uint8).reshape(-1, 16, 3)

def render_stage(side, chunks, rows, cols, pal=None):
    """Render grid cells (rows x cols of 256x256 screens) of a stage from its chunks {0, 3, 4, 9, 0x103} (PS1 restored
    chunks work too). Block byte 2 & 0x3F + 0x40 = palette. Returns (RGB array, {palette: set of (row, col)})."""
    import numpy as np
    e = '>' if side == 'sat' else '<'
    grid, scr, blk, til = chunks[0], chunks[3], chunks[4], chunks[0x103]
    P = stage_palettes(chunks[9], side) if pal is None else pal
    img = np.zeros((len(rows) * 256, len(cols) * 256, 3), np.uint8); used = {}
    for ri, r in enumerate(rows):
        for ci, c in enumerate(cols):
            sn = grid[r * 32 + c]
            if not sn: continue
            for k, ent in enumerate(struct.unpack(e + '256H', scr[sn * 512:sn * 512 + 512])):
                b = ent & 0xFFF; by, bx = divmod(k, 16)
                if side == 'sat': t = struct.unpack('>H', blk[b * 4:b * 4 + 2])[0] >> 2; tb = sat_tile(til, t)
                else: t = struct.unpack('<H', blk[b * 4:b * 4 + 2])[0]; tb = ps1_tile(til, t)
                pi = (blk[b * 4 + 2] & 0x3F) + 0x40; used.setdefault(pi, set()).add((r, c))
                px = np.frombuffer(tb, np.uint8); idx = np.empty(256, np.uint8); idx[0::2] = px & 15; idx[1::2] = px >> 4
                img[ri * 256 + by * 16:ri * 256 + by * 16 + 16, ci * 256 + bx * 16:ci * 256 + bx * 16 + 16] = P[pi][idx.reshape(16, 16)]
    return img, used

def ps1_palette_scripts(stage):
    """{section: [script entries (dest, frame, palettes, hold) per channel]} of a stage (EXE 0x8013DFE8)."""
    ex = Ps1().exe; u32 = ex.u32
    tabs = [u32(0x8013DFE8 + 4 * s) for s in range(14)]
    t = tabs[stage]; nxt = min([x for x in tabs if x > t] or [t + 4]); out = {}
    for sec in range((nxt - t) // 4):
        chans = []
        for k in range(4):
            a = u32(u32(t + 4 * sec) + 4 * k); ents = []
            while True:
                e = bytes(ex.d[a - ex.b:a - ex.b + 4]); a += 4
                if e[0] == 0xFF: break
                ents.append(tuple(e))
            chans.append(ents)
        out[sec] = chans
    return out

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

SELFTEST_C = r"""
typedef unsigned char u8; typedef short s16; typedef int s32; typedef unsigned int u32;
typedef struct Obj { u8 active, phase, state, sub, f4, f5, type, variant; void *rec; s32 x, y, vx, vy;
                     u8 pad[0x25 - 0x1C]; u8 facing, hp; } Obj;
#define XI(o) (((s16*)&(o)->x)[1])
#define YI(o) (((s16*)&(o)->y)[1])
extern void obj_set_anim(Obj*, int); extern void obj_face_player(Obj*); extern void obj_delete(Obj*);
extern void enemy39(Obj*); extern u32 m1_counter;
void m1_handler(Obj *o) { m1_counter++; o->type = 0x39; enemy39(o); }
static void st0(Obj *o) { obj_set_anim(o, 14); o->vy = -8 << 16; o->state = 1; }
static void st1(Obj *o) { o->y += o->vy; if (YI(o) > 0x4B0) { YI(o) = 0x4B0; o->state = 2; } }
static void st2(Obj *o) { obj_face_player(o); o->sub = (u8)(o->hp % 6); if (!o->hp) obj_delete(o); }
void (*const states[])(Obj*) = { st0, st1, st2 };
void sample_handler(Obj *o) { states[o->state](o); o->f4 = (u8)((XI(o) - 0x12F0) / 55); }
"""

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

# ---------------------------------------------------------------- reference interpreter (differential testing)
class Halt(Exception): pass

class Interp:
    """Minimal SH-2 interpreter, big-endian. mem: object with r8/r16/r32/w8/w16/w32 on Saturn addresses.
    hooks: {addr: fn(interp)} called instead of executing code at addr (engine helpers); fn sets r0 and the
    interpreter then returns to PR."""
    RET_MAGIC = 0x0BADC0DE
    def __init__(self, code, mem, hooks):
        self.code, self.mem, self.hooks = code, mem, hooks
        self.r = [0] * 16; self.T = 0; self.pr = 0; self.macl = 0; self.mach = 0; self.steps = 0; self.valid = None; self.cov = None
    def call(self, pc, args=(), sp=0x060FFF00, max_steps=200000):
        r = self.r
        for k, v in enumerate(args): r[4 + k] = v & 0xFFFFFFFF
        r[15] = sp; self.pr = self.RET_MAGIC
        self.pc = pc
        while self.pc != self.RET_MAGIC:
            self.steps += 1
            if self.steps > max_steps: raise Halt('step limit')
            if self.pc in self.hooks:
                self.hooks[self.pc](self); self.pc = self.pr; continue
            if self.valid is not None and not self.valid(self.pc): raise Halt(f'left known code at {self.pc:#x}')
            if self.cov is not None: self.cov.add(self.pc)
            self.exec_at(self.pc)
        return r[0]
    def exec_at(self, a, in_delay=False):
        i = self.code.ins(a)
        if in_delay and self.cov is not None: self.cov.add(a)
        nxt = self._do(i, in_delay)
        if not in_delay: self.pc = nxt
    def _ld(self, size, addr):
        v = {1: self.mem.r8, 2: self.mem.r16, 4: self.mem.r32}[size](addr & 0xFFFFFFFF)
        return (s8(v) if size == 1 else s16(v) if size == 2 else v) & 0xFFFFFFFF
    def _st(self, size, addr, v):
        {1: self.mem.w8, 2: self.mem.w16, 4: self.mem.w32}[size](addr & 0xFFFFFFFF, v & ((1 << (8 * size)) - 1))
    def _do(self, i, in_delay):
        r = self.r; mn, n, m = i.mn, i.n, i.m; a = i.a; M32 = 0xFFFFFFFF
        nxt = a + 2
        SZ = {'b': 1, 'w': 2, 'l': 4}
        def delay():
            self.exec_at(a + 2, True)
        if mn == 'nop': pass
        elif mn == 'mov #imm': r[n] = i.imm & M32
        elif mn == 'mov.l @(disp,PC),Rn': r[n] = self.code.u32(i.pc)
        elif mn == 'mov.w @(disp,PC),Rn': r[n] = s16(self.code.u16(i.pc)) & M32
        elif mn == 'mova': r[0] = i.pc
        elif mn == 'mov': r[n] = r[m]
        elif mn.startswith('mov.') and '@' in mn:
            s = SZ[mn[4]]
            if mn.endswith(',@(R0,Rn)'): self._st(s, r[0] + r[n], r[m])
            elif mn.endswith('@(R0,Rm),Rn'): r[n] = self._ld(s, r[0] + r[m])
            elif mn == 'mov.l Rm,@(disp,Rn)': self._st(4, r[n] + i.disp, r[m])
            elif mn == 'mov.l @(disp,Rm),Rn': r[n] = self._ld(4, r[m] + i.disp)
            elif mn.endswith('R0,@(disp,Rn)'): self._st(s, r[n] + i.disp, r[0])
            elif mn.endswith('@(disp,Rm),R0'): r[0] = self._ld(s, r[m] + i.disp)
            elif mn.endswith('Rm,@-Rn'): v = r[m]; r[n] = (r[n] - s) & M32; self._st(s, r[n], v)
            elif mn.endswith('@Rm+,Rn'):
                v = self._ld(s, r[m])
                if n != m: r[m] = (r[m] + s) & M32
                r[n] = v
            elif mn.endswith('Rm,@Rn'): self._st(s, r[n], r[m])
            elif mn.endswith('@Rm,Rn'): r[n] = self._ld(s, r[m])
            else: raise ValueError(mn)
        elif mn == 'sts.l pr,@-Rn': r[n] = (r[n] - 4) & M32; self._st(4, r[n], self.pr)
        elif mn == 'lds.l @Rm+,pr': self.pr = self._ld(4, r[n]); r[n] = (r[n] + 4) & M32
        elif mn == 'sts.l macl,@-Rn': r[n] = (r[n] - 4) & M32; self._st(4, r[n], self.macl)
        elif mn == 'lds.l @Rm+,macl': self.macl = self._ld(4, r[n]); r[n] = (r[n] + 4) & M32
        elif mn == 'add #imm': r[n] = (r[n] + i.imm) & M32
        elif mn == 'add': r[n] = (r[n] + r[m]) & M32
        elif mn == 'sub': r[n] = (r[n] - r[m]) & M32
        elif mn == 'neg': r[n] = (-r[m]) & M32
        elif mn == 'not': r[n] = (~r[m]) & M32
        elif mn == 'and': r[n] &= r[m]
        elif mn == 'or': r[n] |= r[m]
        elif mn == 'xor': r[n] ^= r[m]
        elif mn == 'and #imm': r[0] &= i.imm
        elif mn == 'or #imm': r[0] |= i.imm
        elif mn == 'xor #imm': r[0] ^= i.imm
        elif mn == 'tst': self.T = int((r[n] & r[m]) == 0)
        elif mn == 'tst #imm': self.T = int((r[0] & i.imm) == 0)
        elif mn == 'cmp/eq': self.T = int(r[n] == r[m])
        elif mn == 'cmp/eq #imm': self.T = int(r[0] == (i.imm & M32))
        elif mn == 'cmp/hs': self.T = int(r[n] >= r[m])
        elif mn == 'cmp/hi': self.T = int(r[n] > r[m])
        elif mn == 'cmp/ge': self.T = int(s32(r[n]) >= s32(r[m]))
        elif mn == 'cmp/gt': self.T = int(s32(r[n]) > s32(r[m]))
        elif mn == 'cmp/pz': self.T = int(s32(r[n]) >= 0)
        elif mn == 'cmp/pl': self.T = int(s32(r[n]) > 0)
        elif mn == 'extu.b': r[n] = r[m] & 0xFF
        elif mn == 'extu.w': r[n] = r[m] & 0xFFFF
        elif mn == 'exts.b': r[n] = s8(r[m]) & M32
        elif mn == 'exts.w': r[n] = s16(r[m]) & M32
        elif mn in ('shll', 'shal'): self.T = r[n] >> 31; r[n] = (r[n] << 1) & M32
        elif mn == 'shlr': self.T = r[n] & 1; r[n] >>= 1
        elif mn == 'shar': self.T = r[n] & 1; r[n] = (s32(r[n]) >> 1) & M32
        elif mn in ('shll2', 'shll8', 'shll16'): r[n] = (r[n] << int(mn[4:])) & M32
        elif mn in ('shlr2', 'shlr8', 'shlr16'): r[n] >>= int(mn[4:])
        elif mn == 'rotl': self.T = r[n] >> 31; r[n] = ((r[n] << 1) | self.T) & M32
        elif mn == 'rotr': self.T = r[n] & 1; r[n] = (r[n] >> 1) | (self.T << 31)
        elif mn == 'rotcl': t = r[n] >> 31; r[n] = ((r[n] << 1) | self.T) & M32; self.T = t
        elif mn == 'rotcr': t = r[n] & 1; r[n] = (r[n] >> 1) | (self.T << 31); self.T = t
        elif mn == 'dt': r[n] = (r[n] - 1) & M32; self.T = int(r[n] == 0)
        elif mn == 'movt': r[n] = self.T
        elif mn == 'clrt': self.T = 0
        elif mn == 'sett': self.T = 1
        elif mn == 'mul.l': self.macl = (r[n] * r[m]) & M32
        elif mn == 'mulu.w': self.macl = (r[n] & 0xFFFF) * (r[m] & 0xFFFF)
        elif mn == 'muls.w': self.macl = (s16(r[n]) * s16(r[m])) & M32
        elif mn == 'dmulu.l': p = r[n] * r[m]; self.macl = p & M32; self.mach = p >> 32
        elif mn == 'dmuls.l': p = (s32(r[n]) * s32(r[m])) & ((1 << 64) - 1); self.macl = p & M32; self.mach = p >> 32
        elif mn == 'sts macl': r[n] = self.macl
        elif mn == 'sts mach': r[n] = self.mach
        elif mn == 'lds macl': self.macl = r[n]
        elif mn == 'sts pr': r[n] = self.pr
        elif mn == 'lds pr': self.pr = r[n]
        elif mn == 'swap.b': r[n] = (r[m] & 0xFFFF0000) | ((r[m] & 0xFF) << 8) | ((r[m] >> 8) & 0xFF)
        elif mn == 'swap.w': r[n] = ((r[m] << 16) | (r[m] >> 16)) & M32
        elif mn == 'xtrct': r[n] = ((r[n] >> 16) | (r[m] << 16)) & M32
        elif mn == 'negc': t = (0 - r[m] - self.T); self.T = int(t < 0); r[n] = t & M32
        elif mn == 'subc': t = r[n] - r[m] - self.T; self.T = int(t < 0); r[n] = t & M32
        elif mn == 'addc': t = r[n] + r[m] + self.T; self.T = int(t > M32); r[n] = t & M32
        elif mn == 'div0u': self.M = self.Q = 0; self.T = 0
        elif mn == 'div0s':
            self.Q = r[n] >> 31; self.M = r[m] >> 31; self.T = int(self.Q != self.M)
        elif mn == 'div1':
            oq = self.Q; self.Q = r[n] >> 31; r[n] = ((r[n] << 1) | self.T) & M32
            tmp0 = r[n]
            if oq == self.M:
                r[n] = (r[n] - r[m]) & M32; tmp1 = int(r[n] > tmp0)
            else:
                r[n] = (r[n] + r[m]) & M32; tmp1 = int(r[n] < tmp0)
            q = self.Q
            if (oq, self.M) in ((0, 0), (1, 0)): self.Q = tmp1 if q == 0 else 1 - tmp1
            else: self.Q = 1 - tmp1 if q == 0 else tmp1
            self.T = int(self.Q == self.M)
        elif mn in ('bt', 'bf'):
            if (self.T == 1) == (mn == 'bt'): nxt = i.tgt
        elif mn in ('bt/s', 'bf/s'):
            c = (self.T == 1) == (mn == 'bt/s'); delay(); nxt = i.tgt if c else a + 4
        elif mn == 'bra': delay(); nxt = i.tgt
        elif mn == 'bsr': self.pr = a + 4; delay(); nxt = i.tgt
        elif mn == 'jmp': t = r[n]; delay(); nxt = t
        elif mn == 'jsr': t = r[n]; self.pr = a + 4; delay(); nxt = t
        elif mn == 'rts': t = self.pr; delay(); nxt = t
        elif mn == 'braf': t = a + 4 + r[n]; delay(); nxt = t & M32
        elif mn == 'bsrf': t = a + 4 + r[n]; self.pr = a + 4; delay(); nxt = t & M32
        else: raise ValueError(f'{a:08x}: interp: {mn}')
        return nxt

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

def WoodPort(): return BossPort(WOOD_SPEC)

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

# ---- differential test (needs: pip install unicorn)
DT_S_P2, DT_P_P2 = 0x06031CE0, 0x8015B174
DT_S_P3, DT_P_P3 = 0x060334E0, 0x801B1EEC
DT_S_P4, DT_P_P4 = 0x06033EE0, 0x801CF848
DT_SLOT = {2: 0x60, 3: 0x50, 4: 0x40}
DT_ALLOC = {0x06014624: 2, 0x060146AE: 3, 0x0601470C: 4}
DT_HALF_FIELDS = (0x2C, 0x2E, 0x34, 0x36, 0x4A)

DT_S_P5, DT_P_P5 = 0x06035EE0, 0x801CF540
P5_WORD_FIELDS = (0x08, 0x0C, 0x10)          # P5 objects (0x18 bytes): record pointer, X, Y are 16.16 words; +0x14.. bytes

def obj_layout(kind):
    """list of (offset, size) covering an object of this kind ('p5': 0x18 bytes, others 0x60) for comparing/converting."""
    if kind == 'p5': words, n = set(P5_WORD_FIELDS), 0x18
    else: words, n = set(OBJ_WORD_FIELDS) | ({0x58} if kind == 'leaf' else {0x54} if kind == 'cut' else set()), 0x60
    out, o = [], 0
    while o < n:
        if o in words: out.append((o, 4)); o += 4
        elif o in DT_HALF_FIELDS and kind != 'p5': out.append((o, 2)); o += 2
        else: out.append((o, 1)); o += 1
    return out

class SatMem:
    def __init__(s, code): s.code, s.d = code, {}
    def r8(s, a):
        if a in s.d: return s.d[a]
        try: return s.code.u8(a)
        except KeyError: return 0
    def w8(s, a, v):
        if 0x00200000 <= a < 0x00300000: raise RuntimeError(f'Saturn write to program area {a:#x}')
        s.d[a] = v & 0xFF
    def r16(s, a): return s.r8(a) << 8 | s.r8(a + 1)
    def r32(s, a): return s.r16(a) << 16 | s.r16(a + 2)
    def w16(s, a, v): s.w8(a, v >> 8); s.w8(a + 1, v)
    def w32(s, a, v): s.w16(a, v >> 16); s.w16(a + 2, v)
    def put(s, a, v, size): {1: s.w8, 2: s.w16, 4: s.w32}[size](a, v)
    def get(s, a, size): return {1: s.r8, 2: s.r16, 4: s.r32}[size](a)

class BossHarness:
    """Differential test of a lifted port spec: the Saturn handler in the SH-2 interpreter and the compiled MIPS in
    unicorn run on the same randomised object / pool / global state with identical stubbed engine helpers; helper
    calls (+ arguments) and all object/global memory are compared after every frame. spec['trials'][kind] = dict(
    fn (C function), entry (Saturn), pool ('P2' or 'P5'), layout (obj_layout kind), state(rnd) -> {offset: byte},
    children (P2 types for slots 1-7), player_x (lo, hi), cam(rnd) -> [(camera offset, value)])."""
    def __init__(self, spec, blob, base, syms, hm, used_helpers):
        self.spec = spec
        self.sat = Sat(); self.code = Code([(self.sat.BASE2, self.sat.p2), (self.sat.BASE1, self.sat.p1)])
        self.blob, self.base, self.syms = blob, base, syms
        self.hm = dict(hm)
        for h in spec.get('custom', {}): self.hm[h] = syms[f'h_{h:08x}']
        self.exe = open(os.path.join(PS1_FILES, 'SLUS_004.53'), 'rb').read()[0x800:]
        self.data_ps1 = syms['sat_data']; self.lo, self.hi = spec['data']
        self.helpers = sorted(set(used_helpers) | set(spec.get('custom', {})))
        self.valid_code = set(); self.cov = set()
        for name, e, typ, tabkeys, words in spec['handlers']:
            ins, _, _ = discover(self.code, [e], {k: spec['code_tables'][k] for k in tabkeys}, None); self.valid_code |= set(ins)
    # ---- address correspondence for comparing pointer-valued arguments / memory
    def s2p_arg(self, v):
        for nm, (b, n, p) in WM_STRUCTS.items():
            if b <= v < b + n: return p + v - b
        return self.s2p(v)
    def s2p(self, v):
        for sb, pb, n in self.pools:
            if sb <= v < sb + n: return pb + (v - sb)
        if self.lo <= v < self.hi: return self.data_ps1 + v - self.lo
        return v
    def trial(self, seed, kind, frames=10, verbose=False):
        from unicorn import Uc, UC_ARCH_MIPS, UC_MODE_MIPS32, UC_MODE_LITTLE_ENDIAN, UC_HOOK_CODE, UcError
        from unicorn.mips_const import UC_MIPS_REG_A0, UC_MIPS_REG_A1, UC_MIPS_REG_A2, UC_MIPS_REG_A3, UC_MIPS_REG_V0, UC_MIPS_REG_RA, UC_MIPS_REG_SP, UC_MIPS_REG_PC
        cfg = self.spec['trials'][kind]
        rnd = random.Random(seed)
        self.pools = [(DT_S_P2, DT_P_P2, 0x60 * 16), (DT_S_P3, DT_P_P3, 0x50 * 8), (DT_S_P4, DT_P_P4, 0x40 * 8)]
        if cfg['pool'] == 'P5': self.pools.append((DT_S_P5, DT_P_P5, 0x18 * 8))
        sm = SatMem(self.code)
        uc = Uc(UC_ARCH_MIPS, UC_MODE_MIPS32 | UC_MODE_LITTLE_ENDIAN)
        uc.mem_map(0, 0x200000)
        uc.mem_write((0x800C0000) & 0x1FFFFFFF, self.exe[:0x113000])
        uc.mem_write((self.base) & 0x1FFFFFFF, self.blob)
        for h in self.helpers: uc.mem_write((self.hm[h]) & 0x1FFFFFFF, struct.pack('<2I', 0x03E00008, 0))
        STOP = 0x80001000; uc.mem_write((STOP) & 0x1FFFFFFF, struct.pack('<2I', 0x0000000D, 0))   # break
        def both(sa, pa, v, size, le=True):
            sm.put(sa, v, size)
            uc.mem_write((pa) & 0x1FFFFFFF, v.to_bytes(size, 'little'))
        lay = obj_layout(cfg['layout'])
        def rand_obj(slot_s, slot_p, k, phase_state=None, layout=None):
            for o, s in (layout or lay):
                r = rnd.random()
                if s == 1: v = rnd.choice([0, 0, 1, 2, 3, 4, 0x40, 0x80, rnd.randrange(256)]) if r < 0.85 else rnd.randrange(256)
                elif s == 2: v = rnd.choice([0, 1, rnd.randrange(0x10000), 0x8000 | rnd.randrange(0x8000)])
                else:
                    v = rnd.choice([0, rnd.randrange(1 << 32), rnd.randrange(-0x40000, 0x40000) & 0xFFFFFFFF, rnd.randrange(0x400, 0x1600) << 16])
                    if _addr_like(v): v ^= 0x40000000      # keep random words out of the address ranges the comparison maps
                both(slot_s + o, slot_p + o, v, s)
            both(slot_s + 0, slot_p + 0, 1, 1)
            if phase_state:
                for o, v in phase_state.items(): both(slot_s + o, slot_p + o, v, 1)
        # this object, parent (for the leaf), children slots
        if cfg['pool'] == 'P5': obj_s, obj_p = DT_S_P5, DT_P_P5
        else: obj_s, obj_p = DT_S_P2, DT_P_P2
        rand_obj(obj_s, obj_p, 0, cfg['state'](rnd))
        k0 = 0 if cfg['pool'] == 'P5' else 1
        for k in range(k0, 8): rand_obj(DT_S_P2 + 0x60 * k, DT_P_P2 + 0x60 * k, k, {6: rnd.choice(cfg['children']), 7: rnd.choice([0, 1, 2, 3])},
                                        None if cfg['pool'] == 'P2' else obj_layout('wood'))
        if cfg['pool'] == 'P5':
            for k in range(1, 8):
                rand_obj(DT_S_P5 + 0x18 * k, DT_P_P5 + 0x18 * k, k, {0: rnd.choice([0, 1]), 6: rnd.choice([0x34, 0x34, 0x11, 0x04])})
        par_s, par_p = DT_S_P2 + 0x60 * 5, DT_P_P2 + 0x60 * 5
        if cfg['pool'] != 'P5':
            for f in cfg.get('ptr_fields', (0x4C,)):        # pointer fields -> the "parent" slot 5 (s2p maps them)
                both(obj_s + f, obj_p + f, par_s, 4)
                uc.mem_write((obj_p + f) & 0x1FFFFFFF, par_p.to_bytes(4, 'little'))
            for f in cfg.get('child_ptr_fields', ()):       # children slots 1-7 point back at this object
                for k in range(1, 8):
                    sm.put(DT_S_P2 + 0x60 * k + f, obj_s, 4); uc.mem_write((DT_P_P2 + 0x60 * k + f) & 0x1FFFFFFF, obj_p.to_bytes(4, 'little'))
        for k in range(8):
            for o, s in obj_layout('wood'):
                if o + s <= 0x50: both(DT_S_P3 + 0x50 * k + o, DT_P_P3 + 0x50 * k + o, rnd.choice([0, 0, rnd.randrange(1 << (8 * s))]), s)
                if o + s <= 0x40: both(DT_S_P4 + 0x40 * k + o, DT_P_P4 + 0x40 * k + o, rnd.choice([0, 0, rnd.randrange(1 << (8 * s))]), s)
        # globals (bytes) + camera/player fields
        glob = [(sa, pa, 1) for sa, pa in self.spec['globals'].items() if sa != 0x0602DA98]
        glob += [(0x0602DA98 + k, 0x801B297C + k, 1) for k in range(4, 0x2C)]
        glob += [(0x0602E108 + k, 0x80159F64 + k, 1) for k in range(0xAC)]
        glob += [(0x0602DAE7 + k, 0x8016D2FB + k, 1) for k in range(9)]
        glob += [(sa + k, pa + k, 1) for sa, n, pa in self.spec.get('glob_bytes', []) for k in range(n)]
        cam_s, cam_p = WM_STRUCTS['camera'][0], WM_STRUCTS['camera'][2]
        pl_s, pl_p = WM_STRUCTS['player'][0], WM_STRUCTS['player'][2]
        glob += [(cam_s + o, cam_p + o, 2) for o in range(0, 0x30, 2)]
        glob += [(pl_s + 0x0C, pl_p + 0x0C, 4), (pl_s + 0x10, pl_p + 0x10, 4)] + [(pl_s + o, pl_p + o, 1) for o in range(0x60, 0xA8)]
        for sa, pa, s in glob:
            v = rnd.choice([0, 0, 1, 2, 3, 4, 5, 0x80, rnd.randrange(1 << (8 * s))]) if s < 4 else rnd.randrange(0x800, 0x1600) << 16 | rnd.randrange(0x10000)
            if sa == 0x0602DA98: continue
            both(sa, pa, v, s)
        both(0x0602DA98, 0x801B297C, 0, 4)
        # player X near the boss; camera position near the arena
        both(pl_s + 0x0C, pl_p + 0x0C, (rnd.randrange(*cfg['player_x']) << 16), 4)
        for o, v in cfg['cam'](rnd):
            both(cam_s + o, cam_p + o, v, 2)
        glob_cmp = glob + [(0x0602DA98, 0x801B297C, 4)]
        # ---- helper stubs (same return sequence on both sides)
        calls_s, calls_p = [], []
        rets = []
        def ret_for(k, h):
            while len(rets) <= k:
                rets.append((rnd.random(), rnd.choice([0, 0, 1, 1, 2, 3, 4, 0xFFFFFFFF, rnd.randrange(16)])))
            u, v = rets[k]
            if h in DT_ALLOC:
                pool = DT_ALLOC[h]
                if u < 0.15: return 0, 0
                idx = 1 + (k % 4)
                sb, pb = {2: (DT_S_P2, DT_P_P2), 3: (DT_S_P3, DT_P_P3), 4: (DT_S_P4, DT_P_P4)}[pool]
                return sb + DT_SLOT[pool] * idx, pb + DT_SLOT[pool] * idx
            return v, v
        arity = M2_HELPER_ARGS
        def sat_hook(h):
            def f(it):
                k = len(calls_s); calls_s.append((h, tuple(it.r[4:4 + arity.get(h, 1)])))
                it.r[0] = ret_for(k, h)[0]
            return f
        hooks = {h: sat_hook(h) for h in self.helpers}
        p2h = {self.hm[h]: h for h in self.helpers}
        def p_hook(uc, addr, size, ud):
            if addr in p2h:
                h = p2h[addr]; k = len(calls_p)
                args = tuple(uc.reg_read(r) for r in (UC_MIPS_REG_A0, UC_MIPS_REG_A1, UC_MIPS_REG_A2, UC_MIPS_REG_A3)[:arity.get(h, 1)])
                calls_p.append((h, args)); uc.reg_write(UC_MIPS_REG_V0, ret_for(k, h)[1])
        uc.hook_add(UC_HOOK_CODE, p_hook, begin=min(p2h), end=max(p2h) + 4)
        entry_s, entry_p = cfg['entry'], self.syms[cfg['fn']]
        for fr in range(frames):
            ns, np_ = len(calls_s), len(calls_p)
            if verbose:
                print('frame', fr, 'sat', bytes(sm.r8(obj_s + o) for o in range(0x60)).hex())
                print('frame', fr, 'ps1', bytes(uc.mem_read(obj_p & 0x1FFFFFFF, 0x60)).hex())
            it = Interp(self.code, sm, hooks)
            it.cov = self.cov
            it.valid = lambda pc: pc in self.valid_code or 0x06019600 <= pc < 0x06019C00
            err = None
            try: it.call(entry_s, (obj_s,))
            except Halt as e: return dict(ok=None, frame=fr, why=f'invalid trial (saturn {e})')
            except Exception as e: err = f'saturn: {e}'
            uc.reg_write(UC_MIPS_REG_A0, obj_p); uc.reg_write(UC_MIPS_REG_RA, STOP); uc.reg_write(UC_MIPS_REG_SP, 0x801FF000)
            try: uc.emu_start(entry_p, STOP, count=2000000)
            except UcError as e: err = (err or '') + f' ps1: {e} @ {uc.reg_read(UC_MIPS_REG_PC):#x}'
            if err:
                return dict(ok=None, frame=fr, why=err)
            # compare calls
            cs, cp = calls_s[ns:], calls_p[np_:]
            if len(cs) != len(cp) or any(a[0] != b[0] or tuple(self.s2p_arg(x) for x in a[1]) != b[1] for a, b in zip(cs, cp)):
                return dict(ok=False, frame=fr, why='calls', sat=[(hex(h), [hex(x) for x in a]) for h, a in cs],
                            ps1=[(hex(h), [hex(x) for x in a]) for h, a in cp])
            # compare memory: all pool slots + globals
            diffs = []
            for sb, pb, n in self.pools:
                stride = {DT_S_P2: 0x60, DT_S_P3: 0x50, DT_S_P4: 0x40, DT_S_P5: 0x18}[sb]
                for slot in range(n // stride):
                    if sb == DT_S_P5: lay2 = obj_layout('p5')
                    elif sb == DT_S_P2: lay2 = lay if cfg['pool'] == 'P2' else obj_layout('wood')
                    else: lay2 = [(o, s) for o, s in obj_layout('wood') if o + s <= stride]
                    for o, s in lay2:
                        vs = sm.get(sb + slot * stride + o, s)
                        vp = int.from_bytes(uc.mem_read((pb + slot * stride + o) & 0x1FFFFFFF, s), 'little')
                        if self.s2p(vs) != vp if s == 4 else vs != vp: diffs.append((hex(sb), slot, hex(o), s, hex(vs), hex(vp)))
            for sa, pa, s in glob_cmp:
                vs = sm.get(sa, s); vp = int.from_bytes(uc.mem_read((pa) & 0x1FFFFFFF, s), 'little')
                if (self.s2p(vs) if s == 4 else vs) != vp: diffs.append(('glob', hex(sa), hex(pa), s, hex(vs), hex(vp)))
            if diffs: return dict(ok=False, frame=fr, why='memory', diffs=diffs[:12], calls=[(hex(h), [hex(x) for x in a]) for h, a in cs])
        return dict(ok=True, calls=len(calls_s))

def M2Harness(blob, base, syms, hm, used_helpers): return BossHarness(WOOD_SPEC, blob, base, syms, hm, used_helpers)

M2_HELPER_ARGS = {}

def helper_arity(hm, helpers):
    import capstone as c
    ps = Ps1(); e = ps.exe
    md = c.Cs(c.CS_ARCH_MIPS, c.CS_MODE_MIPS32 | c.CS_MODE_LITTLE_ENDIAN)
    out = {}
    for h in helpers:
        a = hm[h]; read = set(); written = set(); n = 0
        while n < 600:
            l = list(md.disasm(e.d[a - e.b:a - e.b + 4], a))
            if not l: break
            i = l[0]; n += 1
            ops = i.op_str.replace(' ', '').split(',')
            if i.mnemonic in ('sw', 'sh', 'sb', 'beq', 'bne', 'jr', 'jalr', 'mult', 'multu', 'div', 'divu', 'mthi', 'mtlo', 'blez', 'bgtz', 'bltz', 'bgez', 'beqz', 'bnez'):
                rr, ww = [o.split('(')[-1].rstrip(')') for o in ops], []
            elif ops and ops[0].startswith('$'):
                ww, rr = [ops[0]], [o.split('(')[-1].rstrip(')') for o in ops[1:]]
            else: rr, ww = [], []
            for r in rr:
                r = r.lstrip('$')
                if r in ('a0', 'a1', 'a2', 'a3') and r not in written: read.add(r)
            for r in ww: written.add(r.lstrip('$'))
            if i.mnemonic == 'jal': written |= {'a0', 'a1', 'a2', 'a3'}
            if i.mnemonic == 'jr' and ops[0] == '$ra': break
            a += 4
        out[h] = max([0] + [int(r[1]) + 1 for r in read])
    return out

# ----------------------------------------------------------------- M2: Wood Man in the STAGE08 overlay
WM_HANDLERS = (('wood_man', 0x258F00, 0x63), ('wood_leaf', 0x259B7C, 0x64))
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

def wm_lift():
    """Lift Wood Man + leaf to C. Returns (C source, WoodPort with used_helpers)."""
    return boss_lift(WOOD_SPEC)

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
M5_STAGES = ('STAGE05', 'STAGE05B')

def remove_placement(chunk, rec):
    """Chunk 0x0A with the one record equal to `rec` removed: the following records and the terminator move up 8
    bytes and the freed 8 bytes at the end are zero (same size)."""
    t = placement_terminator(chunk)
    offs = [i for i in range(0, t, 8) if chunk[i:i + 8] == rec]
    assert len(offs) == 1, f'record {rec.hex()} found {len(offs)} times'
    i = offs[0]
    assert chunk[-8:] == bytes(8)
    return chunk[:i] + chunk[i + 8:] + bytes(8)

def m5_placements(chunk): return remove_placement(chunk, m5_bolt19())

def patch_m5(disc):
    """Remove bolt 19's floor record from STAGE05.PAC and STAGE05B.PAC in place (chunk 0x0A). Returns info."""
    info = {}
    for st in M5_STAGES:
        ext, sz, chunks = pac_locate(disc, f'STDATA/{st}.PAC')
        assert disc.read(ext, sz) == _file(os.path.join(PS1_FILES, 'STDATA', st + '.PAC')), f'{st}.PAC already patched?'
        t, off, n = next(c for c in chunks if c[0] == 0xA)
        disc.write(ext, off, m5_placements(disc.read(ext, n, off)))
        info[st.lower() + '_lba'] = ext
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

def build_image(src_bin, out_bin, quick_stage=None, workdir=None, checkpoint=None, test_start=None):
    """The current patch on a raw PS1 track 1 (all in place, sector EDC/ECC fixed):
      - track 1 extended by PS1_EXTEND_SECTORS empty sectors (extend_track1);
      - STAGE08 overlay with the relocated enemy table + lifted Wood Man / leaf code (build_m2_overlay) in the free
        sectors, followed by PBGM46 (WOOD1 on Capcom's PBGM29, build_boss_intro_bank) and PBGM47 (WOOD2 converted
        whole, build_music_bank); music 0x46/0x47 reach them through virtual file entries in the overlay;
      - STAGE09 overlay with Cut Man (P2 0x62) and the alarm (P5 0x34) lifted to C (build_m4_overlay) and the
        restored STAGE09.PAC (patch_m4a), both moved to the free sectors, with PBGM48 (CUT1 on PBGM29) and PBGM49
        (CUT2 converted whole) between them; music 0x48/0x49 reach them through the overlay's virtual file entries;
      - play_bgm hook (patch_music_hook), sound bank-mismatch guard (patch_sound_guard);
      - STAGE08.PAC rebuilt in place + STAGE08B placements + Wood Man's door lists (patch_m2a);
      - bolt 19's floor record removed from STAGE05/STAGE05B.PAC in place (patch_m5; Wood Man drops it);
      - quick_stage / checkpoint: debug quick start (test builds only, never in a release);
      - test_start = (px, py, cx, cy, bounds_entry): debug start record for quick_stage/checkpoint (patch_test_start;
        test builds only, never in a release)."""
    disc = RawDisc(src_bin)
    extend_track1(disc)
    if quick_stage is not None: patch_quick_start(disc, quick_stage, checkpoint)
    if test_start is not None:
        assert quick_stage is not None and checkpoint is not None, 'test_start needs quick_stage and checkpoint'
        px, py, cx, cy, be = test_start
        patch_test_start(disc, quick_stage, checkpoint, (px, py), (cx, cy), be)
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
    m7 = patch_m7a(disc, nxt, workdir, info['vf0']); nxt += m7['m7_sectors']          # M7a/M7b: title BONUS MODE, bonus overlay, SLABO
    info.update({'m7_' + k: v for k, v in m7.items()})
    patch_music_hook(disc, info['vf0'])
    patch_sound_guard(disc)
    info.update(patch_m2a(disc))
    info.update(patch_m5(disc))
    disc.save(out_bin)
    info.update(sectors_changed=len(disc.touched), free_lba=nxt, free_sectors_left=disc.free[1] - nxt)
    return info

def check_build(orig_bin, new_bin, xdelta=None):
    """Independent check of a build: every sector that differs from the original (after the track-1 insertion) has a
    valid MSF header for its position, Mode 2 Form 1 and valid EDC/ECC (inserted sectors included; track 1's moved last
    sector unchanged apart from its header);
    the stage overlays' music entries 0x46-0x49 point at files equal to freshly built PBGM46-49; optional xdelta
    round trip. Returns a dict (raises AssertionError on a failure)."""
    import subprocess, tempfile
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
    # full-disc layout: with separate track files track 2 (END1.DA) starts after the new track 1 plus its
    # 150-sector pregap; entry 138, both ISO extents and the PVD size moved by the inserted sectors
    od = RawDisc(orig_bin); pv = lambda d: struct.unpack('<I', d.read(16, 84)[80:84])[0]
    lba138 = disc.file_entry(PS1_END1_FILE)[0]
    assert lba138 == len(n) // S + 150 == disc.iso_record('END1.DA')[2] == od.file_entry(PS1_END1_FILE)[0] + ins, 'END1.DA position'
    assert disc.iso_record('ZNULL.DAT')[2] == od.iso_record('ZNULL.DAT')[2] + ins and pv(disc) == pv(od) + ins, 'track 3 / PVD size'
    out['end1_lba'] = lba138
    for st in M5_STAGES:                                 # M5: only chunk 0x0A changed, bolt 19 gone
        ext, sz, chunks = pac_locate(disc, f'STDATA/{st}.PAC'); oe, osz, _ = pac_locate(od, f'STDATA/{st}.PAC')
        t, off, cn = next(c for c in chunks if c[0] == 0xA)
        new_pac, old_pac = disc.read(ext, sz), od.read(oe, osz)
        assert (ext, sz) == (oe + 0, osz) and new_pac[:off] == old_pac[:off] and new_pac[off + cn:] == old_pac[off + cn:], st
        assert new_pac[off:off + cn] == m5_placements(old_pac[off:off + cn]) and m5_bolt19() not in new_pac[off:off + cn], st
    out['m5_bolt19_removed'] = True
    # M7a: DEMO.BIN rebuilt in its own slot; its virtual entry points at the bonus overlay
    e8, o8 = disc.file_entry(PS1_DEMO_FILE), od.file_entry(PS1_DEMO_FILE)
    assert e8[0] == o8[0] and e8[2] == o8[2] and e8[1] <= PS1_DEMO_SLOT * 2048, 'DEMO.BIN entry'
    assert disc.iso_record('OVL/DEMO.BIN')[2:] == (e8[0], e8[1]), 'DEMO.BIN ISO record'
    want_demo, want_bonus, i7 = build_m7_overlays(od.read(*o8[:2]))
    demo = disc.read(*e8[:2]); vl, vs, vt = struct.unpack('<3I', demo[i7['vf_off']:i7['vf_off'] + 12])
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
    if xdelta:
        with tempfile.TemporaryDirectory() as d:
            subprocess.run(['xdelta3', '-d', '-f', '-s', orig_bin, xdelta, os.path.join(d, 'r.bin')], check=True)
            out['xdelta_roundtrip'] = open(os.path.join(d, 'r.bin'), 'rb').read() == n
            assert out['xdelta_roundtrip']
    return out

def difftest_boss(spec, n=300, kinds=None, frames=10, workdir=None, uninit=False):
    """Differential test of a lifted port spec (see BossHarness). Returns {kind: Counter}, first failures, coverage
    (+ with uninit=True a 4th value from _uninit_hooks: MIPS reads of stack words not written during the same handler
    call, and reads outside the EXE image / compiled code / stack, check 3)."""
    if uninit:
        rep, restore = _uninit_hooks(0x801E1400, 0x801E1400 + len(boss_compile(spec, 0x801E1400, workdir)[0]))
        try: return difftest_boss(spec, n, kinds, frames, workdir) + (rep,)
        finally: restore()
    data, sy, port, path = boss_compile(spec, 0x801E1400, workdir)
    hm = load_helper_map()
    H = BossHarness(spec, data, 0x801E1400, sy, hm, port.used_helpers)
    M2_HELPER_ARGS.update(helper_arity(hm, [h for h in H.helpers if h in hm]))
    M2_HELPER_ARGS.update(spec.get('custom_arity', {}))
    res, first = {}, {}
    for kind in kinds or list(spec['trials']):
        c = collections.Counter()
        for seed in range(n):
            r = H.trial(seed, kind, frames)
            key = 'ok' if r['ok'] else ('invalid' if r['ok'] is None else 'FAIL-' + r['why'])
            c[key] += 1
            if key.startswith('FAIL') and key not in first: first[(kind, key)] = (seed, r)
        res[kind] = c
    sat = Sat(); code = Code([(sat.BASE2, sat.p2), (sat.BASE1, sat.p1)])
    cov = {}
    for name, entry, typ, tabkeys, words in spec['handlers']:
        ins, _, _ = discover(code, [entry], {k: spec['code_tables'][k] for k in tabkeys}, None)
        cov[name] = (len(set(ins) & H.cov), len(ins))
    return res, first, cov

def _uninit_hooks(blob_lo, blob_hi, stk_lo=0x801FC000, stk_hi=0x801FF000, sentinel=False):
    """Install read/write hooks on every unicorn machine started from now on (the BossHarness MIPS side, SP 0x801FF000).
    Reports stack reads before a write in the same emu_start (uninitialised locals / stk[] slots) and reads outside the
    EXE image 0x800C0000-0x801D3000 (all loaded from disc: on hardware these hold the same values as in an emulator),
    the compiled blob and the stack. Returns (report dict, restore function). sentinel=True (M7f, bonus harness): no
    write hook (with unicorn 2.1.4 any MEM_WRITE hook broke the bonus harness's stubbed helper returns, PC 0): the stack is
    filled with 0xA5 before each run and a stack read that returns only 0xA5 bytes counts as a read before write."""
    import unicorn
    from unicorn import UC_HOOK_MEM_READ, UC_HOOK_MEM_WRITE
    from unicorn.mips_const import UC_MIPS_REG_PC
    rep = dict(calls=0, stack_read_before_write=collections.Counter(), outside=collections.Counter())
    written = set(); orig = unicorn.Uc.emu_start
    def emu_start(self, begin, until, timeout=0, count=0):
        if not getattr(self, '_mm8_uninit', False):
            self._mm8_uninit = True
            def wr(uc, acc, addr, size, val, ud):
                a = addr | 0x80000000
                if stk_lo <= a < stk_hi: written.update(range(a, a + size))
            def rd(uc, acc, addr, size, val, ud):
                a = addr | 0x80000000
                if stk_lo <= a < stk_hi:
                    if (all(v == 0xA5 for v in uc.mem_read(addr, size)) if sentinel else any(x not in written for x in range(a, a + size))):
                        rep['stack_read_before_write'][(uc.reg_read(UC_MIPS_REG_PC), a - stk_hi)] += 1
                elif not (0x800C0000 <= a < 0x801D3000 or blob_lo <= a < blob_hi or 0x80001000 <= a < 0x80001008):
                    rep['outside'][a & ~0xF] += 1
            if not sentinel: self.hook_add(UC_HOOK_MEM_WRITE, wr)
            self.hook_add(UC_HOOK_MEM_READ, rd)
        if sentinel: self.mem_write(stk_lo & 0x1FFFFFFF, b'\xa5' * (stk_hi - stk_lo))
        written.clear(); rep['calls'] += 1
        return orig(self, begin, until, timeout, count)
    unicorn.Uc.emu_start = emu_start
    def restore(): unicorn.Uc.emu_start = orig
    return rep, restore

def difftest_m2(n=300, kinds=('wood', 'leaf'), frames=10, workdir=None):
    """Differential test of the lifted Wood Man / leaf code. Returns {kind: Counter}, coverage."""
    return difftest_boss(WOOD_SPEC, n, kinds, frames, workdir)

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
PS1_PBGM_FILE0 = 23                   # file-table entry of PBGM00
PS1_BGM_FILE_SITES = (0x800FCABC, 0x800FCC70)            # sll/lui/addu/lw a0,0x7714(at) ... jal 0x800FB9C0; nop
PS1_BGM_LOOP_SITES = ((0x800FCB2C, 2), (0x800FCC0C, 3), (0x800FCD28, 3))   # (site, index register: 2 = v0, 3 = v1)
PS1_BGM_FIRST_NEW = 0x46              # music numbers >= this use our virtual file entries

def vag_decode(b):
    """PS1 SPU ADPCM -> list of 16-bit samples (16-byte blocks of 28 samples)."""
    out = []; s1 = s2 = 0
    for i in range(0, len(b) - 15, 16):
        sh, f = b[i] & 15, b[i] >> 4
        f0, f1 = VAG_FILTERS[f] if f < 5 else (0, 0)
        for j in range(28):
            n = (b[i + 2 + j // 2] >> (4 * (j & 1))) & 15
            if n >= 8: n -= 16
            v = ((n << 12) >> sh if sh <= 12 else 0) + ((s1 * f0 + s2 * f1 + 32) >> 6)
            v = max(-32768, min(32767, v)); out.append(v); s2, s1 = s1, v
    return out

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
    cc (ctl, val), prog (p), press (v), bend (v), mark (step byte of an 82 event)."""
    res, nt, do, lo = struct.unpack('>4H', s[:8])
    tempos = [struct.unpack('>II', s[8 + 8 * k:16 + 8 * k]) for k in range(nt)]
    ev, T = [], [0]
    def run(i, lim=None, depth=0):
        n = 0
        while lim is None or n < lim:
            b = s[i]
            if b < 0x80:
                assert not b & 0x10, 'note flag 0x10 not handled'
                key, vel, gate, dt = s[i + 1:i + 5]
                gate += (0x100 if b & 0x40 else 0) + (0x200 if b & 0x20 else 0)
                T[0] += dt; ev.append((T[0], 'note', b & 15, key, vel, gate)); i += 5
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

def sony_seq_parse(d):
    """Sony SEQ ('pQES') -> dict(res, tempo, rhythm, ev [(t, kind, ch, a, b)]); note-on with velocity 0 = 'off'."""
    assert d[:4] == b'pQES'
    res = struct.unpack('>H', d[8:10])[0]; tempo = int.from_bytes(d[10:13], 'big')
    i, t, rs, ev = 15, 0, None, []
    while i < len(d):
        dt = 0
        while True:
            b = d[i]; i += 1; dt = (dt << 7) | (b & 0x7F)
            if not b & 0x80: break
        t += dt
        if d[i] & 0x80: rs = d[i]; i += 1
        if rs == 0xFF:
            ty, ln = d[i], d[i + 1]; ev.append((t, 'meta', ty, d[i + 2:i + 2 + ln], 0)); i += 2 + ln; rs = None
            if ty == 0x2F: break
            continue
        k, ch = rs >> 4, rs & 15
        if k in (0x8, 0x9, 0xA, 0xB, 0xE): a, b = d[i], d[i + 1]; i += 2
        else: a, b = d[i], 0; i += 1
        kind = {0x8: 'off', 0x9: 'on', 0xA: 'pat', 0xB: 'cc', 0xC: 'prog', 0xD: 'press', 0xE: 'bend'}[k]
        if kind == 'on' and b == 0: kind = 'off'
        ev.append((t, kind, ch, a, b))
    return dict(res=res, tempo=tempo, rhythm=d[13:15], ev=ev, size=i)

def sega_to_sony_seq(s, prog_map=None, drop_cc=(0x20,), vel_map=None, cc7_map=None, key_map=None, tempo_scale=1.0):
    """Convert one Sega SEQ song to a Sony SEQ (libsnd): same resolution, times and channels; notes become note-on /
    note-on velocity 0 after the gate (clipped at the loop end); program numbers through prog_map (default: same);
    pitch bend 7-bit -> MSB; bank select (CC 0x20) dropped; velocity / CC 7 through vel_map / cc7_map when given
    (build_music_bank passes the measured Saturn laws); tempo changes as FF 51; loop = CC 99 = 20 + CC 6 = 127 at the
    loop point and CC 99 = 30 at the end (Capcom's markers). Returns (bytes, info)."""
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
    for t, kind, ch, a, b, c in A['ev']:
        if kind == 'note':
            off = t + c
            if looped and off > end_t: off = end_t; clipped += 1
            if c == 0: continue
            if vel_map: b = vel_map(cprog.get(ch, 0), b)
            if key_map: a = key_map(cprog.get(ch, 0), a)
            add(t, 6, 0x90 | ch, (a, b)); add(off, 4 if off > t else 7, 0x90 | ch, (a, 0)); nnotes += 1
        elif kind == 'cc':
            if a in drop_cc: continue
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
    body = bytearray(b'pQES' + struct.pack('>IH', 1, A['res']) + u0.to_bytes(3, 'big') + b'\x04\x02')
    t, rs = 0, None
    for et, order, _, st, data in out:
        body += _vlq(et - t); t = et
        if st == 0xFF: body += b'\xff' + data; rs = None
        else:
            if st != rs: body.append(st); rs = st
            body += data
    body += _vlq(max(0, end_t - t)) + b'\xff\x2f\x00'
    info = dict(res=A['res'], tempo=u0, loop_t=loop_t, end_t=end_t, marks=marks, notes=nnotes, clipped=clipped,
                channels=sorted({e[2] for e in A['ev'] if e[1] == 'note'}), size=len(body))
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
    """SCSP EG (AR, D1R, DL, D2R, RR, key scaling at `octave`) -> PS1 (adsr1, adsr2). Attack: instant (all WOOD2 layers
    use AR 0x1F). Decay 1 -> SPU decay at the same dB rate down to Sl (DL in 3 dB steps; the SPU's lowest sustain level is
    -24 dB, so a deeper DL continues at the D1R slope in the sustain phase instead of D2R); D2R -> sustain decrease;
    RR -> release (SPU exponential)."""
    import math
    assert lay['ar'] == 0x1F and not lay['eghold'] and lay['krs'] != 15
    base = octave + 2 * lay['krs']
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
    adsr1 = 0x0000 | (0 << 8) | (Dr << 4) | Sl           # linear attack, rate 0 (instant)
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
    if sh == 128: c, sh = c + 1, 0
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
        gain_db={0: -0.8, 1: -3.7, 2: -2.7, 3: -0.7, 4: -0.9, 5: -0.8, 6: 0.0, 7: 0.2,
                 (8, 45): -1.3, (8, 67): 1.7, (8, 51): -0.3, (8, 75): 0.3, (8, 78): 0.2, (8, 77): 1.4},
        decay_dbps={3: -10.84, 4: -2.93, 5: -2.93, 6: -8.19, 7: -8.19},  # envelope slope to set (sample's own change removed)
        upsample={0: 2, 1: 4, 2: 4, 3: 2, 4: 0.5, 5: 0.5, 6: 2, 7: 2, 8: 2}),   # voice 0 = the one-shot sounds;
        # 4/5 at half rate: keys 80-88 asked the SPU for 4.2-6.7x and were capped at 4x (up to 9 semitones flat)
    'CUT2.PAC': dict(       # calibration rounds in Mednafen (02 §Sound "PBGM49")
        gain_db={0: -0.4, 1: 1.1, 2: -0.7, 3: 3.8, 4: 1.0, 5: -1.5, 6: -1.3, 7: 4.0, 8: 4.7, 9: 1.5, 10: 1.5,
                 (11, 47): 0.3, (11, 51): 1.7, (11, 77): 4.4, (11, 78): 6.5},
        decay_dbps={2: -12.2, 3: -16.2, 4: -16.2, 5: -24.25, 6: -24.25, 7: -8.1, 8: -8.1, 9: -64.8, 10: -64.8},
        decay_rate={9: 48, 10: 48},   # chosen by test (rates 47-50): the SPU's fast rates aren't in the measured table
        upsample={0: 2, 2: 2, 3: 0.5, 4: 0.5, 5: 2, 6: 2, 7: 2, 8: 2, 9: 2, 10: 2, 11: 2}),   # 0.5: stored at half rate (SPU 4x pitch cap)
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
def build_music_bank(sat_name, tpl_n=0x2A, sfx_tpl_n=0x29, seq_song=None, tune=None, cal_keys=False):
    """New PS1 music bank (PBGM47 from WOOD2, later PBGM49 from CUT2) converted from a whole Saturn sound bank:
      - SEQ: song 0 -> Sony SEQ (sega_to_sony_seq; channels and program numbers unchanged).
      - VAB: Saturn voice v (music, 1..15) -> program v, one tone per TON layer (key range, base note + fine -> centre +
        shift, pitch-bend range from the voice header, TL + DISDL -> volume normalised to 127 for the loudest layer,
        DIPAN -> pan, EG -> ADSR (scsp_to_spu_adsr at the voice's median key), loops resampled so their length is a
        multiple of 28 (vag_encode_loop)); program 0 = Capcom's dummy; voice 0 (one-shot sounds) -> program 16 like
        build_boss_intro_bank (tone k -> key 60 + k, muted level-0 layers left out).
      - sound-ID table: the PS1 entry rule (reference §Sound).
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
    progs = [bytearray(tv['progs'][6]) for _ in range(17)]
    tones = [[bytearray(empty_tone) for _ in range(16)] for _ in range(17)]
    for p in range(17):
        for k in range(16): tones[p][k][20:22] = struct.pack('<h', p)
    vags, vag_of = [], {}
    info = dict(bank=bank, layers=[], muted=[], sfx=[])
    # ---- music voices
    att = lambda L: (7 - L['disdl']) * 6.0 + L['tl'] * 0.375
    music = [(v, hdr, [ton_layer(r) for r in raws]) for v, (hdr, raws) in enumerate(voices) if v > 0]
    used = [L for v, hdr, ls in music if v in keys_of for L in ls if L['disdl']]
    a_ref = min(att(L) for L in used)
    want = {}
    for v, hdr, ls in music:
        if v not in keys_of: continue                     # voice not played by the song
        assert v < 16 and len(ls) <= 16
        mk = statistics.median_low(keys_of[v])
        n_t = 0
        for L in ls:
            assert L['ssctl'] == 0 and L['lpctl'] in (0, 1) and not any(L['mod']) and not any(L['extra']), (v, L)
            if L['disdl'] == 0: info['muted'].append((v, L['lo'])); continue
            pcm = ton_pcm(t, L)
            key44 = L['base'] - L['fine'] / 256.0          # key at which the sample plays at 44.1 kHz
            up = tune.get('upsample', {}).get(v, 1)
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
            vk = (L['sa'], L['lsa'], L['lea'], L['lpctl'], L['pcm8'], up, down)
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
            dd = tune.get('decay_dbps', {}).get(v)
            if dd is not None:                                     # measured: one exponential slope from key-on
                sr = tune.get('decay_rate', {}).get(v, _best(SPU_SR_BEST, dd / 44100))   # or an SPU rate chosen by test
                a1 = 0x00FF; a2 = 0xC000 | (sr << 6) | 0x20 | (a2 & 0x1F)
            tn = bytearray(empty_tone)
            tn[0] = 0; tn[1] = 0
            g = tune.get('gain_db', {}); g = g.get((v, L['lo']), g.get(v, 0.0))
            want[(v, n_t)] = 127 * 10 ** (-(att(L) - a_ref) / 20) * 10 ** (g / 40)
            tn[2] = max(1, min(127, round(want[(v, n_t)])))
            tn[3] = scsp_pan(L['dipan']); tn[4] = c; tn[5] = sh; tn[6] = L['lo']; tn[7] = L['hi']
            tn[8:12] = bytes(4); tn[12] = tn[13] = hdr[0]
            tn[16:24] = struct.pack('<2H2h', a1, a2, v, vi)
            tones[v][n_t] = tn; n_t += 1
            info['layers'].append(dict(voice=v, keys=(L['lo'], L['hi']), vag=vi, loop=L['lpctl'], vol=tn[2], pan=tn[3],
                                       center=c, shift=sh, adsr=(a1, a2), pb=hdr[0], median_key=mk))
        progs[v] = bytearray(tv['progs'][0]); progs[v][0] = n_t
    mx = max(want.values())
    if mx > 127:                          # tuned volumes above 127: scale the music tones down, program volume up
        mvol = min(127, round(tv['progs'][0][1] * mx / 127))
        for (vv, n), w in want.items(): tones[vv][n][2] = max(1, min(127, round(w * 127 / mx)))
        for vv in {vv for vv, n in want}: progs[vv][1] = mvol
        info['mvol'] = mvol
    # ---- voice 0: one-shot sounds -> program 16 (as build_boss_intro_bank)
    hdr0, raws0 = voices[0]
    tone_of = {}
    for r in raws0:
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
    progs[16] = bytearray(tv['progs'][16]); progs[16][0] = len(tone_of)
    drums = {vv for vv, hdr, ls in music if vv in keys_of and not all(L['lo'] == 0 for L in ls)}
    vel_db = tune.get('vel_db', {'melodic': SAT_VEL_DB}); k7 = tune.get('cc7_db_per_step', SAT_CC7_DB_PER_STEP)
    vel_map = lambda p, vel: _libsnd_level(_interp_db(vel_db.get('drum' if p in drums else 'melodic', vel_db['melodic']), vel))
    cc7_map = lambda c: _libsnd_level(-k7 * (127 - c))
    seq, sinfo = sega_to_sony_seq(seq_song or song, cc7_map=cc7_map, vel_map=vel_map, tempo_scale=tune.get('tempo_factor', 1.0),
                                  prog_map={0: 16} if cal_keys else None,
                                  key_map=(lambda p, k: 60 + tone_of.get(k, 0) if p == 0 else k) if cal_keys else None)
    info['seq'] = sinfo
    head = bytearray(tv['head'])
    allp = [bytearray(16) for _ in range(128)]
    for p in range(17): allp[p] = progs[p]
    for p in range(17, 128): allp[p] = bytearray(tv['progs'][p])
    head[0x14:0x16] = struct.pack('<H', sum(pr[0] for pr in progs))
    v = dict(head=head, progs=allp, tones=tones, vags=vags)
    vhdr, body = vab_join(v)
    pac = pac_build([(5, sound_id_table(sat_name, len(tpl[5]), tone_of)), (4, vhdr), (0x201, body), (1, seq)])
    info.update(hdr=len(vhdr), body=len(body), seq_size=len(seq), size=len(pac), vags=len(vags), tones=dict(
        (p, progs[p][0]) for p in range(17) if progs[p][0] and progs[p][1]))
    return pac, info

def music_bank_report(pac, sat_name):
    """Independent re-check of a bank from build_music_bank: re-parse the PAC, compare every tone's playback rate with
    the Saturn layer's (cents; loops: through the VAG's loop length vs the Saturn loop), the SEQ notes with the decoded
    Sega song (round trip) and the ID table. Returns a dict."""
    import math
    ch = {t: b for t, o, b in unpac_bytes(pac)}
    v = vab_split(ch[4], ch[0x201])
    t, voices = ton_voices_raw(sat_name)
    cents, layers = [], 0
    for p in list(range(1, 16)) + [16]:
        src = [ton_layer(r) for r in voices[p if p < 16 else 0][1]] if (p if p < 16 else 0) < len(voices) else []
        src = [L for L in src if L['disdl']]
        for ti in range(v['progs'][p][0] if v['progs'][p][1] else 0):
            tn = v['tones'][p][ti]; L = src[ti]; layers += 1
            g = v['vags'][struct.unpack('<h', tn[22:24])[0] - 1]
            fl = [g[i + 1] for i in range(0, len(g), 16)]
            q = ((len(fl) - fl.index(6)) * 28) / (L['lea'] - L['lsa']) if L['lpctl'] else max(1, round((len(fl) - 2) * 28 / L['lea']))
            K = tn[6]
            ksat = L['lo'] if p == 16 else K
            rsat = 44100 * 2 ** ((ksat - L['base']) / 12 + L['fine'] / 256 / 12)
            rps = 44100 * 2 ** ((K - tn[4] + tn[5] / 128) / 12) / q
            cents.append(1200 * math.log2(rps / rsat))
            assert (tn[6], tn[7]) == ((L['lo'], L['hi']) if p < 16 else (K, K))
    A = sega_seq_decode(sat_bank_song(sat_name, 0))
    # highest SPU pitch any song note asks for (with the largest upward bend used on its channel); the SPU caps at 4.0
    prog, bend_up, max_ratio = {}, collections.Counter(), 0.0
    for e in A['ev']:
        if e[1] == 'bend': bend_up[e[2]] = max(bend_up[e[2]], e[3] - 64)
    for e in A['ev']:
        if e[1] == 'prog': prog[e[2]] = e[3]
        elif e[1] == 'note' and 0 < prog.get(e[2], 0) < 16:
            p_ = prog[e[2]]
            for ti in range(v['progs'][p_][0] if v['progs'][p_][1] else 0):
                tn = v['tones'][p_][ti]
                if tn[6] <= e[3] <= tn[7]:
                    up_st = bend_up[e[2]] / 64 * tn[12]
                    max_ratio = max(max_ratio, 2 ** ((e[3] + up_st - tn[4] + tn[5] / 128) / 12))
    P = sony_seq_parse(ch[1])
    on, got = collections.defaultdict(list), []
    for e in P['ev']:
        if e[1] == 'on': on[(e[2], e[3])].append((e[0], e[4]))
        elif e[1] == 'off': s0 = on[(e[2], e[3])].pop(0); got.append((s0[0], e[2], e[3], s0[1], e[0] - s0[0]))
    exp = [(tt, c, k, vel, min(g, A['end_t'] - tt)) for tt, kind, c, k, vel, g in A['ev'] if kind == 'note' and g]
    got = [(x[0], x[1], x[2], x[4]) for x in got]; exp = [(x[0], x[1], x[2], x[4]) for x in exp]   # velocities are re-curved
    loops = [(e[0], e[4]) for e in P['ev'] if e[1] == 'cc' and e[3] == 99]
    ids = {i: ch[5][4 * i:4 * i + 4] for i in range(len(ch[5]) // 4) if any(ch[5][4 * i:4 * i + 4])}
    return dict(layers=layers, max_cents=max(abs(c) for c in cents), seq_notes_equal=sorted(got) == sorted(exp), max_pitch=max_ratio,
                notes=len(got), loops=loops, end_t=A['end_t'], ids=ids, progs=v['progs'][:17], size=len(pac))

# ----------------------------------------------------------------- recording comparison
# Recordings of the same music on both systems are compared through a render of the decoded Saturn song (Saturn
# rules: base note, loops, TL/DISDL, D1R/DL, pan, gates; velocity/CC 7 left out so that the fit measures them):
# per (channel, velocity) group the rendered power spectrogram in semitone bins is fitted to each recording (NNLS;
# frames with excess energy = sound effects are dropped iteratively), giving the level of every group in each
# recording. ps1 - saturn per group = how much louder that part is on the PS1. Tempo/offset are found by matching.
def render_sat_song(name, loops=2, step=None, group=None, raw_levels=False, sr=44100):
    """Approximate render of a Saturn bank's song 0 (stereo float array). group = set of (channel, velocity) to keep."""
    import math, numpy as np
    A = sega_seq_decode(sat_bank_song(name, 0)); t, voices = ton_voices_raw(name)
    step = step or A['tempos'][0][1] / 1e6 / A['res']
    L, E = A['loop_t'], A['end_t']
    out = np.zeros((int((L + (E - L) * loops) * step * sr) + 2 * sr, 2))
    prog, vol = {}, collections.defaultdict(lambda: 127)
    evs = [(e[0] + k * (E - L),) + e[1:] for k in range(loops) for e in A['ev'] if k == 0 or e[0] >= L]
    for tt, kind, ch, a, b, c in evs:
        if kind == 'prog': prog[ch] = a
        elif kind == 'cc' and a == 7: vol[ch] = b
        elif kind == 'note' and (group is None or (ch, b) in group):
            for raw in voices[prog.get(ch, 0)][1]:
                Ly = ton_layer(raw)
                if not Ly['lo'] <= a <= Ly['hi'] or Ly['disdl'] == 0: continue
                pcm = np.array(ton_pcm(t, Ly), float) / 32768
                rate = 44100 * 2 ** ((a - Ly['base']) / 12 + Ly['fine'] / 3072)
                n = int((c * step + 0.006) * sr); pos = np.arange(n) * rate / sr
                if Ly['lpctl']: pos = np.where(pos >= Ly['lea'], Ly['lsa'] + np.mod(pos - Ly['lsa'], Ly['lea'] - Ly['lsa']), pos)
                else: n = min(n, int(len(pcm) * sr / rate)); pos = pos[:n]
                if n <= 0: continue
                y = np.interp(pos, np.arange(len(pcm)), pcm)
                env = np.ones(n)
                if Ly['d1r'] and Ly['dl']:
                    r = scsp_rate_db_per_sample(Ly['d1r'], math.floor(math.log2(rate / 44100)) + 2 * Ly['krs'])
                    db = r * np.arange(n); env = np.where(db < -3 * Ly['dl'], 0, 10 ** (db / 20))
                k0 = int(c * step * sr)
                if k0 < n: env[k0:] *= np.linspace(1, 0, n - k0)
                g = 10 ** (-((7 - Ly['disdl']) * 6 + Ly['tl'] * 0.375) / 20) * (1 if raw_levels else (b / 127) * (vol[ch] / 127))
                ldb, rdb = scsp_pan_db(Ly['dipan']); lg, rg = 10 ** (ldb / 20), 10 ** (rdb / 20)
                i0 = int(tt * step * sr); seg = out[i0:i0 + n]; m = len(seg)
                seg[:, 0] += y[:m] * env[:m] * g * lg; seg[:, 1] += y[:m] * env[:m] * g * rg
    return out

def semitone_power(mono, sr=44100, hop=441, lo=16, hi=112):
    import numpy as np, scipy.signal as sg
    f, t, S = sg.stft(mono, sr, nperseg=8192, noverlap=8192 - hop); A = np.abs(S) ** 2
    midi = 69 + 12 * np.log2(np.maximum(f, 1) / 440); out = np.zeros((hi - lo, A.shape[1]))
    for k in range(lo, hi):
        sel = (midi >= k - 0.5) & (midi < k + 0.5)
        if sel.any(): out[k - lo] = A[sel].sum(0)
    return out

def _load_mono(path):
    import numpy as np, subprocess, tempfile, scipy.io.wavfile as wv
    with tempfile.TemporaryDirectory() as d:
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-i', path, '-ar', '44100', '-ac', '1', os.path.join(d, 'a.wav')], check=True)
        sr, x = wv.read(os.path.join(d, 'a.wav'))
    x = x.astype(float); return x / (np.abs(x).max() + 1e-9)

def _match(rec, F, lags):
    import numpy as np
    z = lambda q: (lambda a: (a - a.mean(0)) / (a.std(0) + 1e-9))(np.log1p(q * 1e3))
    a = z(rec); n = a.shape[1]; b = z(F)
    return max(((a * b[:, L:L + n]).mean(), L) for L in lags if L + n <= b.shape[1])

def audio_compare(name, sat_path, ps1_path, q=0.5):
    """Per-(channel, velocity) levels of a Saturn and a PS1 recording of song 0 of bank `name` (see above).
    Returns dict(tempo factors, groups [(group, sat dB, ps1 dB)] relative to the loudest-in-both reference group)."""
    import numpy as np, scipy.optimize as so
    A = sega_seq_decode(sat_bank_song(name, 0)); step0 = A['tempos'][0][1] / 1e6 / A['res']
    res = {}
    groups = sorted({(e[2], e[4]) for e in A['ev'] if e[1] == 'note'})
    for side, path in (('sat', sat_path), ('ps1', ps1_path)):
        rec = semitone_power(_load_mono(path))
        mix0 = semitone_power(render_sat_song(name, 3).mean(1))
        # tempo: best match of two windows 20 s apart
        w = 500; i1, i2 = 1500, min(rec.shape[1] - w - 1, 3500)
        o1 = _match(rec[:, i1:i1 + w], mix0, range(0, mix0.shape[1] - w))[1]
        o2 = _match(rec[:, i2:i2 + w], mix0, range(o1 + int((i2 - i1) * 0.9), min(mix0.shape[1] - w, o1 + int((i2 - i1) * 1.1))))[1]
        fac = (i2 - i1) / max(1, o2 - o1)                    # recording frames per nominal frame
        step = step0 * fac
        F = [semitone_power(render_sat_song(name, 3, step, {g}, True).mean(1)) for g in groups]
        mix = sum(F); n = min(rec.shape[1] - 20, mix.shape[1] - 1)
        c, lag = _match(rec[:, :min(n, 3000)], mix, range(0, mix.shape[1] - min(n, 3000)))
        n = min(n, mix.shape[1] - lag)
        X = np.stack([f[:, lag:lag + n].ravel() for f in F], 1); y = rec[:, :n].ravel(); wts = np.ones(n)
        for it in range(6):
            W = np.repeat(wts[None, :], rec.shape[0], 0).ravel()
            g, _ = so.nnls(X * W[:, None] ** 0.5, y * W ** 0.5)
            pred = (X @ g).reshape(rec[:, :n].shape); r = (rec[:, :n].sum(0) - pred.sum(0)) / (pred.sum(0) + 1e-12)
            wts = (r < np.quantile(r, q)).astype(float)
        res[side] = dict(gain=g, tempo_factor=fac, match=c, lag=lag)
    gs, gp = res['sat']['gain'], res['ps1']['gain']
    cnt = collections.Counter((e[2], e[4]) for e in A['ev'] if e[1] == 'note')
    ds, dp = 10 * np.log10(gs / gs.max() + 1e-12), 10 * np.log10(gp / gp.max() + 1e-12)
    ok = [i for i, gr in enumerate(groups) if cnt[gr] >= 20 and ds[i] > -60 and dp[i] > -60]
    off = float(np.median([dp[i] - ds[i] for i in ok]))     # overall level difference (median over the main groups)
    return dict(tempo=(res['sat']['tempo_factor'], res['ps1']['tempo_factor']), match=(res['sat']['match'], res['ps1']['match']),
                ref='median of %d groups' % len(ok), groups=[(gr, cnt[gr], ds[i], dp[i] - off) for i, gr in enumerate(groups)])

# ----------------------------------------------------------------- emulator calibration
# The user's BIOS files let Mednafen (1.29, headless: Xvfb + SDL dummy audio, -soundrecord) run both discs here. Saturn: the
# calibration song replaces a stage's music bank (the Saturn title plays CD audio, missing from a track-1 image); PS1:
# every PBGM file entry points at the test bank, so the title screen plays it.
def sat_music_image(src_bin, out_bin, bank_name='WOOD2.PAC', target='ST00A.PAC', song0=None):
    """Saturn test image: the bank file `target` (default ST00A.PAC = the intro stage's music, reached from Game Start)
    is overwritten in place with `bank_name`'s sound data: ID table (bank bytes renumbered to the target's bank, padded
    to the target's table size) + TON/SEQ chunk, optionally with song 0 replaced by `song0` (raw Sega SEQ bytes that
    fit the original song 0's space). The file keeps its size and place. (The title screen plays CD audio on the
    Saturn, so the test music has to be a stage's.)"""
    disc = SatRawDisc(src_bin)
    _, _, ext, size = disc.iso_record(target)
    tgt = disc.read(ext, size)
    te = unpac_bytes(tgt); tbank = [b for t, o, b in te if t == 0x201][0][0]
    sc = {t: b for t, o, b in unpac(os.path.join(SAT_FILES, bank_name))}
    tab = bytearray(sc[0x201]); ton = bytearray(sc[0x203])
    for i in range(0, len(tab), 8):
        if any(tab[i:i + 8]): tab[i] = tbank
    tsz = [len(b) for t, o, b in te if t == 0x201][0]
    assert len(tab) <= tsz and len(ton) == [len(b) for t, o, b in te if t == 0x203][0]
    tab += bytes(tsz - len(tab))
    info = dict(bank=bank_name, target=target, target_bank=hex(tbank))
    if song0 is not None:
        so = SAT_SEQ_OFFSET
        n = struct.unpack('>H', ton[so:so + 2])[0]
        offs = [struct.unpack('>I', ton[so + 2 + 4 * k:so + 6 + 4 * k])[0] for k in range(n)]
        room = offs[1] - offs[0]; assert len(song0) <= room, (len(song0), room)
        ton[so + offs[0]:so + offs[0] + len(song0)] = song0
        info.update(song0=len(song0), room=room)
    head = bytearray(tgt[:0x800])
    body = bytes(tab) + bytes(-len(tab) % 0x800) + bytes(ton) + bytes(-len(ton) % 0x800)
    new = bytes(head) + body
    assert len(new) == size, (len(new), size)
    disc.write(ext, 0, new); disc.save(out_bin)
    info['sectors'] = len(disc.touched)
    return info

def ps1_title_music_image(src_bin, out_bin, pac):
    """PS1 test image: every PBGM file entry (title, demo, stages) loads `pac` from the free sectors, so the title
    screen (music 0x10) plays it."""
    disc = RawDisc(src_bin)
    lba = PS1_FREE_LBA[0]; nsec = place_file(disc, lba, pac)
    old = disc.file_entry(PS1_PBGM_FILE0 + 0x10); assert old[2] == 4
    for n in range(PS1_BGM_FIRST_NEW): disc.set_file_entry(PS1_PBGM_FILE0 + n, lba, len(pac), 4)
    disc.save(out_bin)
    return dict(lba=lba, sectors=nsec, old_entry=old)

def sega_seq_encode(events, res=48, usec=361445):
    """Write a one-tempo Sega SEQ song from [(t, kind, ch, a, b, c)] (kinds as sega_seq_decode: note (key, vel, gate
    <= 0x1FF), cc, prog). Gaps longer than 255 steps are bridged with pitch-bend-centre events (E0 40) on channel 15.
    (Not bank select: a CC 0x20 without a following program change silences the channel on the Saturn - found by the
    calibration.) Returns bytes; decodes back to the same events (asserted)."""
    events = sorted(events, key=lambda e: e[0])
    data = bytearray(); t = 0
    for e in events:
        tt, kind, ch, a, b, c = e
        dt = tt - t
        while dt > 255:
            data += bytes([0xEF, 0x40, 255]); dt -= 255
        if kind == 'note':
            assert c < 0x200 and c > 0
            data += bytes([(ch & 15) | (0x40 if c & 0x100 else 0), a, b, c & 0xFF, dt])
        elif kind == 'cc': data += bytes([0xB0 | ch, a, b, dt])
        elif kind == 'prog': data += bytes([0xC0 | ch, a, dt])
        else: raise ValueError(kind)
        t = tt
    end = max(tt + (c if kind == 'note' else 0) for tt, kind, ch, a, b, c in events) + 48
    dt = end - t
    while dt > 255: data += bytes([0xEF, 0x40, 255]); dt -= 255
    data += bytes([0xEF, 0x40, dt, 0x83])
    head = struct.pack('>4H', res, 1, 16, 8) + struct.pack('>II', end, usec)
    song = head + bytes(data)
    D = sega_seq_decode(song)
    got = [(x[0],) + x[1:] for x in D['ev'] if not (x[1] == 'cc' and x[3] == 0x20) and x[1] != 'bend']
    assert sorted(got) == sorted((x[0], x[1], x[2], x[3], x[4], x[5] if x[1] == 'note' else 0) for x in events if not (x[1] == 'cc' and x[3] == 0x20)), 'encode/decode mismatch'
    assert D['last_t'] == D['end_t'] == end
    return song

CAL_GATE, CAL_REST, CAL_LONG = 48, 24, 240
CAL_VELS = (127, 122, 120, 115, 110, 100, 90, 86, 80, 72, 64, 50, 40)
CAL_CC7 = (120, 115, 108, 96, 80)

def calibration_song(name='WOOD2.PAC'):
    """Calibration song for a bank (round 2): every music voice alone on its own channel at its median key: the
    velocities CAL_VELS (CC 7 = 127), CC 7 = CAL_CC7 (velocity 127), one long note (gate 240) and one note an octave up;
    every drum key at all CAL_VELS; every one-shot sound (voice 0, velocity 127); then a gate-0x140 note (note bit 0x40)
    followed by a note on the same channel (Saturn note-bit check). Returns (song bytes, schedule [(t, gate, voice, key,
    vel, cc7, label)])."""
    import statistics
    A = sega_seq_decode(sat_bank_song(name, 0))
    prog, keys = {}, collections.defaultdict(list)
    for e in A['ev']:
        if e[1] == 'prog': prog[e[2]] = e[3]
        elif e[1] == 'note': keys[prog[e[2]]].append(e[3])
    t_, voices = ton_voices_raw(name)
    ev, sched = [], []
    t = 24; ch = 0
    def setup(v):
        nonlocal t
        ev.extend([(t, 'cc', ch, 0x20, 1, 0), (t, 'prog', ch, v, 0, 0), (t, 'cc', ch, 7, 127, 0)]); t += 12
    def note(v, key, vel, gate, cc7, label):
        nonlocal t
        ev.append((t, 'note', ch, key, vel, gate)); sched.append((t, gate, v, key, vel, cc7, label)); t += gate + CAL_REST
    melodic = [v for v in sorted(keys) if all(ton_layer(r)['lo'] == 0 for r in voices[v][1])]
    drums = [v for v in sorted(keys) if v not in melodic]
    for v in melodic:
        k = statistics.median_low(keys[v]); setup(v)
        for vel in CAL_VELS: note(v, k, vel, CAL_GATE, 127, 'vel')
        for c7 in CAL_CC7:
            ev.append((t - 6, 'cc', ch, 7, c7, 0)); note(v, k, 127, CAL_GATE, c7, 'cc7')
        ev.append((t - 6, 'cc', ch, 7, 127, 0))
        note(v, k, 127, CAL_LONG, 127, 'long')
        note(v, k + 12, 127, CAL_GATE, 127, 'octave')
        ch += 1
    for v in drums:
        setup(v)
        for raw in voices[v][1]:
            L = ton_layer(raw)
            if L['disdl'] == 0: continue
            for vel in CAL_VELS: note(v, L['lo'], vel, CAL_GATE, 127, 'drum')
        ch += 1
    setup(0)
    for raw in voices[0][1]:
        L = ton_layer(raw)
        if L['disdl'] == 0: continue
        n = len(ton_pcm(t_, L)) / (44100 * 2 ** ((L['lo'] - L['base']) / 12)) / (A['tempos'][0][1] / 1e6 / A['res'])
        note(0, L['lo'], 127, min(240, int(n) + 4), 127, 'sfx')
    ch += 1
    v = melodic[0]; setup(v); k = statistics.median_low(keys[v])
    note(v, k, 127, 0x140, 127, 'bit40'); note(v, k + 7, 127, CAL_GATE, 127, 'after40')
    return sega_seq_encode(ev, A['res'], A['tempos'][0][1]), sched

# ----------------------------------------------------------------- emulator sessions + calibration analysis
SAT_KEYS_TO_STAGE = (20, 40, 60, 75, 88)   # Return presses: skip intro movie, open menu, Game Start, skip story movie
PS1_KEYS_TO_TITLE = (20, 40, 60)           # Return presses: skip logos/intro movie, open the title menu (music keeps playing)

def mednafen_session(bin_path, wav_path, seconds, mode, keys=(), snaps_every=0, display=77, home=None):
    """Run a one-track image in Mednafen on a private Xvfb display, press Return at the given seconds (xdotool),
    optionally take screenshots (F9) every `snaps_every` s, and record the sound to `wav_path` (RIFF sizes fixed).
    bin_path ending in .cue = a full multi-track disc, used as is (mode ignored).
    Needs Xvfb, xdotool and the BIOS files in ~/.mednafen/firmware. Returns (seconds recorded, home dir)."""
    import subprocess, tempfile, time, shutil
    d = home or tempfile.mkdtemp()
    os.makedirs(d, exist_ok=True)
    fw = os.path.join(d, 'firmware')
    if not os.path.exists(fw): os.symlink(os.path.expanduser('~/.mednafen/firmware'), fw)
    if bin_path.lower().endswith('.cue'):
        cue = os.path.abspath(bin_path)
    else:
        cue = os.path.join(d, 'disc.cue'); link = os.path.join(d, 'disc.bin')
        if os.path.lexists(link): os.remove(link)
        os.symlink(os.path.abspath(bin_path), link)
        open(cue, 'w').write(f'FILE "disc.bin" BINARY\n  TRACK 01 {mode}\n    INDEX 01 00:00:00\n')
    if os.path.exists(wav_path): os.remove(wav_path)
    env = dict(os.environ, SDL_AUDIODRIVER='dummy', MEDNAFEN_HOME=d, DISPLAY=f':{display}')
    lock = f'/tmp/.X{display}-lock'
    if os.path.exists(lock): os.remove(lock)
    xv = subprocess.Popen(['Xvfb', f':{display}', '-screen', '0', '1280x1024x24'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1); t0 = time.time()
    mp = subprocess.Popen(['/usr/games/mednafen', '-sound.driver', 'sdl', '-sound.rate', '44100', '-video.driver', 'softfb',
                           '-soundrecord', os.path.abspath(wav_path), cue], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    wid = ''
    for _ in range(80):
        r = subprocess.run(['xdotool', 'search', '--pid', str(mp.pid)], env=env, capture_output=True, text=True)
        if r.stdout.strip(): wid = r.stdout.split()[-1]; break
        time.sleep(0.25)
    # keys: seconds (press Return) or (seconds, xdotool key, hold seconds). PS1 pad defaults: Return = Start, w/a/s/d =
    # up/left/down/right, KP_2 = cross, KP_6 = circle, KP_4 = square.
    events = sorted([(k, 'Return', 0.15) if isinstance(k, (int, float)) else tuple(k) for k in keys]
                    + ([(t, 'F9', 0.15) for t in range(6, int(seconds), snaps_every)] if snaps_every else []))
    for t, k, hold in events:
        dt = t - (time.time() - t0)
        if dt > 0: time.sleep(dt)
        if wid:
            subprocess.run(['xdotool', 'windowfocus', wid], env=env, capture_output=True)
            subprocess.run(['xdotool', 'keydown', '--window', wid, k], env=env, capture_output=True); time.sleep(hold)
            subprocess.run(['xdotool', 'keyup', '--window', wid, k], env=env, capture_output=True)
    rest = seconds - (time.time() - t0)
    if rest > 0: time.sleep(rest)
    mp.terminate(); mp.wait(10); xv.terminate()
    b = bytearray(open(wav_path, 'rb').read()); n = (len(b) - 44) & ~3; b = b[:44 + n]
    b[4:8] = struct.pack('<I', 36 + n); b[40:44] = struct.pack('<I', n); open(wav_path, 'wb').write(b)
    return n // 4 / 44100, d

class MednafenLive:
    """Interactive headless Mednafen session for menus: one key at a time, screenshots (F9, native
    resolution, into home/snaps) and save states (F5 into slot 0 -> home/mcs) on request. Same keys as mednafen_session.
    with MednafenLive(bin_or_cue, home, display=81) as m: m.wait(30); m.key('Return'); m.snap('title')"""
    def __init__(self, bin_path, home, display=81, mode='MODE2/2352', record=None, video=None, vcodec='png', state=None):
        import subprocess, time
        self.sp, self.time = subprocess, time
        self.home = os.path.abspath(home); os.makedirs(self.home, exist_ok=True)
        fw = os.path.join(self.home, 'firmware')
        if not os.path.exists(fw): os.symlink(os.path.expanduser('~/.mednafen/firmware'), fw)
        if bin_path.lower().endswith('.cue'): cue = os.path.abspath(bin_path)
        else:
            cue = os.path.join(self.home, 'disc.cue'); link = os.path.join(self.home, 'disc.bin')
            if os.path.lexists(link): os.remove(link)
            os.symlink(os.path.abspath(bin_path), link)
            open(cue, 'w').write(f'FILE "disc.bin" BINARY\n  TRACK 01 {mode}\n    INDEX 01 00:00:00\n')
        self.env = dict(os.environ, SDL_AUDIODRIVER='dummy', MEDNAFEN_HOME=self.home, DISPLAY=f':{display}')
        lock = f'/tmp/.X{display}-lock'
        if os.path.exists(lock): os.remove(lock)
        self.xv = subprocess.Popen(['Xvfb', f':{display}', '-screen', '0', '1280x1024x24'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1); self.t0 = time.time()
        args = ['/usr/games/mednafen', '-sound.driver', 'sdl', '-sound.rate', '44100', '-video.driver', 'softfb']
        if record: args += ['-soundrecord', os.path.abspath(record)]
        if video: args += ['-qtrecord', os.path.abspath(video), '-qtrecord.vcodec', vcodec]   # every frame (ffmpeg reads it; cscd is fastest)
        self.mp = subprocess.Popen(args + [cue], env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.wid = ''
        for _ in range(80):
            r = subprocess.run(['xdotool', 'search', '--pid', str(self.mp.pid)], env=self.env, capture_output=True, text=True)
            if r.stdout.strip(): self.wid = r.stdout.split()[-1]; break
            time.sleep(0.25)
        self.snaps = []
        if state:                                       # (name_in_mcs, src): load a slot-0 save state right after boot (F7)
            os.makedirs(os.path.join(self.home, 'mcs'), exist_ok=True)
            import shutil; shutil.copy(state[1], os.path.join(self.home, 'mcs', state[0])); time.sleep(3); self.key('F7', after=1.0)
    def __enter__(self): return self
    def __exit__(self, *a): self.close()
    def elapsed(self): return self.time.time() - self.t0
    def wait(self, s): self.time.sleep(s)
    def until(self, t):
        dt = t - self.elapsed()
        if dt > 0: self.time.sleep(dt)
    def key(self, k, hold=0.12, after=0.6):
        x = lambda *a: self.sp.run(['xdotool', *a], env=self.env, capture_output=True)
        x('windowfocus', self.wid); x('keydown', '--window', self.wid, k); self.time.sleep(hold); x('keyup', '--window', self.wid, k)
        self.time.sleep(after)
    def _listing(self, sub):                        # {name: mtime}: a save state overwrites its slot file (same name)
        d = os.path.join(self.home, sub)
        return {f: os.path.getmtime(os.path.join(d, f)) for f in os.listdir(d)} if os.path.isdir(d) else {}
    def _newest(self, sub, before):
        d = os.path.join(self.home, sub)
        if not isinstance(before, dict): before = {f: None for f in before}
        for _ in range(40):
            now = self._listing(sub)
            new = sorted((f for f, t in now.items() if f not in before or (before[f] is not None and t > before[f])), key=now.get)
            if new: return os.path.join(d, new[-1])
            self.time.sleep(0.1)
        return None
    def snap(self, name=None):
        d = os.path.join(self.home, 'snaps'); before = set(os.listdir(d)) if os.path.isdir(d) else set()
        self.key('F9', after=0.3); p = self._newest('snaps', before)
        if p and name:
            q = os.path.join(d, name + '.png'); os.replace(p, q); p = q
        self.snaps.append(p); return p
    def state(self, name=None):
        d = os.path.join(self.home, 'mcs'); before = self._listing('mcs')
        self.key('F5', after=0.5); p = self._newest('mcs', before)
        if p and name:
            q = os.path.join(d, name + '.mc0'); self.time.sleep(0.3); os.replace(p, q); p = q
        return p
    def close(self):
        try: self.mp.terminate(); self.mp.wait(10)
        finally: self.xv.terminate()

def _wav(path):
    import numpy as np, scipy.io.wavfile as wv
    sr, x = wv.read(path); assert sr == 44100
    return x.astype(float) / 32768

def _env_db(x, hop=441):
    import numpy as np
    m = (x ** 2).sum(1); n = len(m) // hop
    return 10 * np.log10(m[:n * hop].reshape(n, hop).mean(1) + 1e-12)

def cal_locate(x, sched, nominal_step):
    """Find the calibration song in a recording: best match of the note on/off pattern over start and tempo."""
    import numpy as np
    act = (_env_db(x) > -55).astype(float) * 2 - 1; best = (-1e9, 0, 0)
    for f in np.arange(0.98, 1.04, 0.0005):
        st = nominal_step * f
        mask = -np.ones(int((sched[-1][0] + sched[-1][1]) * st * 100) + 50)
        for t, g, *_ in sched: mask[int(t * st * 100):int((t + g) * st * 100)] = 1
        if len(mask) >= len(act): continue
        c = np.correlate(act, mask, 'valid'); o = int(np.argmax(c))
        if c[o] > best[0]: best = (c[o], o, f)
    return best[1] / 100.0, nominal_step * best[2], best[2]

def cal_measure(x, sched, t0, step):
    """Per scheduled note: level (20-150 ms after key-on, dB), L-R balance, spectrum, 50 ms level slices."""
    import numpy as np
    SR = 44100; out = []
    rms = lambda s: 10 * np.log10((s ** 2).mean() + 1e-15)
    for t, g, v, k, vel, c7, lab in sched:
        a = int((t0 + t * step) * SR); seg = x[a:a + int(g * step * SR)]
        w = seg[int(0.02 * SR):int(0.15 * SR)]
        s = seg[int(0.02 * SR):int(0.02 * SR) + 8192].mean(1)
        if len(s) < 8192: s = np.pad(s, (0, 8192 - len(s)))
        S = np.abs(np.fft.rfft(s * np.hanning(len(s)))) ** 2; f = np.fft.rfftfreq(len(s), 1 / SR)
        sl = [rms(seg[i:i + int(0.05 * SR)].mean(1)) for i in range(int(0.02 * SR), len(seg) - int(0.05 * SR), int(0.05 * SR))]
        out.append(dict(t=t, v=v, k=k, vel=vel, c7=c7, lab=lab, lvl=rms(w.sum(1) / 2), pan=rms(w[:, 0]) - rms(w[:, 1]),
                        spec=(f, S), slices=sl))
    return out

def cal_compare(sched, S, P):
    """Saturn vs PS1 calibration measurements -> dict of the comparisons (levels at velocity 127 relative to the median
    offset, velocity/CC 7 curves, decay slopes, pan, pitch offset in cents, band energy shares)."""
    import numpy as np
    def logspec(m):
        f, Sp = m['spec']; lf = np.linspace(np.log2(40), np.log2(8000), 6000)
        v = np.interp(lf, np.log2(np.maximum(f, 1)), np.log10(Sp + 1e-12)); return lf, v - v.mean()
    def pitch(a, b):
        lf, x = logspec(a); _, y = logspec(b)
        best = max(((np.dot(x[200 + s:-200 + s], y[200:-200]), s) for s in range(-150, 151)))
        return -best[1] * (lf[1] - lf[0]) * 1200
    def slope(m):
        s = np.array(m['slices']); t = np.arange(len(s)) * 0.05 + 0.045
        k = (t >= 0.1) & (t <= 1.7) & (s > s[min(2, len(s) - 1)] - 35)     # fast decays: stop above the noise floor
        return float(np.polyfit(t[k], s[k], 1)[0]) if k.sum() > 4 else float('nan')
    def bands(m):
        f, Sp = m['spec']; tot = Sp.sum()
        return [10 * np.log10(Sp[(f >= a) & (f < b)].sum() / tot + 1e-15) for a, b in ((0, 500), (500, 2000), (2000, 5000), (5000, 10000), (10000, 20000))]
    rows = list(zip(sched, S, P))
    ref = {}
    for r, a, b in rows:
        if r[4] == 127 and r[5] == 127 and r[6] in ('vel', 'drum', 'sfx'): ref.setdefault((r[2], r[3]), (a, b))
    musical = {(r[2], r[3]) for r in sched if r[6] == 'vel'}           # melodic voices (not drums, not one-shots)
    off = float(np.median([b['lvl'] - a['lvl'] for vk, (a, b) in ref.items() if vk in musical]))
    res = dict(offset=off, level={}, vel={}, cc7={}, decay={}, bands={})
    for (v, k), (a, b) in ref.items():
        res['level'][(v, k)] = (a['lvl'], b['lvl'] - off, b['lvl'] - off - a['lvl'], a['pan'], b['pan'], pitch(a, b))
        res['bands'][(v, k)] = [y - x for x, y in zip(bands(a), bands(b))]
    for r, a, b in rows:
        base = ref.get((r[2], r[3]))
        if not base: continue
        if r[6] in ('vel', 'drum') and r[4] != 127:
            res['vel'].setdefault((r[2], r[3]), []).append((r[4], a['lvl'] - base[0]['lvl'], b['lvl'] - base[1]['lvl']))
        if r[6] == 'cc7':
            res['cc7'].setdefault((r[2], r[3]), []).append((r[5], a['lvl'] - base[0]['lvl'], b['lvl'] - base[1]['lvl']))
        if r[6] == 'long': res['decay'][r[2]] = (slope(a), slope(b))
    return res

def song_compare(sat_wav, ps1_wav, sat_from, ps1_from, dur=88, ratio=1.0125 / 1.0090, search=None):
    """Whole-song comparison of two clean recordings of the same music: aligns the PS1 recording to the Saturn one
    (spectral features; PS1 time-scaled by `ratio`), returns band energy shares, frame-wise spectral correlation, L/R."""
    import numpy as np, scipy.signal as sg
    SR = 44100; a = _wav(sat_wav); b = _wav(ps1_wav)
    def feat(m):
        f, t, S = sg.stft(m, SR, nperseg=4096, noverlap=4096 - 441); X = np.log1p(200 * np.abs(S[:400])); return (X - X.mean(0)) / (X.std(0) + 1e-9)
    FA = feat(a[int(sat_from * SR):int((sat_from + 20) * SR)].mean(1)); FB = feat(b[int(ps1_from * SR):int((ps1_from + search) * SR) if search else None].mean(1)); W = FA.shape[1]   # search: seconds to search (a loop)
    sb = ps1_from + int(np.argmax([(FA * FB[:, o:o + W]).mean() for o in range(0, FB.shape[1] - W)])) / 100
    A = a[int(sat_from * SR):int((sat_from + dur) * SR)]; n = len(A)
    B = np.stack([np.interp(np.arange(n) / ratio, np.arange(len(b) - int(sb * SR)), b[int(sb * SR):, c]) for c in range(2)], 1)
    f, t, SA = sg.stft(A.mean(1), SR, nperseg=4096, noverlap=4096 - 441); _, _, SB = sg.stft(B.mean(1), SR, nperseg=4096, noverlap=4096 - 441)
    PA, PB = np.abs(SA) ** 2, np.abs(SB) ** 2
    band = [(20, 60), (60, 120), (120, 250), (250, 500), (500, 1000), (1000, 2000), (2000, 4000), (4000, 8000), (8000, 16000)]
    tot = lambda P: P[(f >= 20) & (f < 16000)].sum()
    sh = lambda P: [10 * np.log10(P[(f >= x) & (f < y)].sum() / tot(P)) for x, y in band]
    edges = np.geomspace(40, 16000, 65)
    lb = lambda P: np.log10(np.stack([P[(f >= edges[i]) & (f < edges[i + 1])].sum(0) for i in range(64)]) + 1e-9)
    XA, XB = lb(PA), lb(PB); m = min(XA.shape[1], XB.shape[1]); cz = lambda X: (X - X.mean(0)) / (X.std(0) + 1e-9)
    return dict(start=(sat_from, sb), bands=list(zip(band, sh(PA), sh(PB))),
                frame_corr=float(np.median((cz(XA[:, :m]) * cz(XB[:, :m])).mean(0))),
                lr=(10 * np.log10((A[:, 0] ** 2).mean() / (A[:, 1] ** 2).mean()), 10 * np.log10((B[:, 0] ** 2).mean() / (B[:, 1] ** 2).mean())))

def calibrate(sat_bin, ps1_bin, name='WOOD2.PAC', workdir='calib', tune=None, sat_meas=None, displays=(77, 78)):
    """Full emulator calibration of a converted bank: build the Saturn and PS1 calibration images (calibration_song),
    record both in Mednafen (Saturn: the song replaces the intro stage's music, reached with SAT_KEYS_TO_STAGE; PS1: the
    title music, PS1_KEYS_TO_TITLE), measure and compare. sat_meas = reuse a saved Saturn measurement (the Saturn side
    only changes when calibration_song does). Returns (comparison dict, saturn measurement, ps1 measurement)."""
    import pickle
    os.makedirs(workdir, exist_ok=True)
    song, sched = calibration_song(name)
    A = sega_seq_decode(song); step = A['tempos'][0][1] / 1e6 / A['res']
    secs = (sched[-1][0] + sched[-1][1]) * step * 1.02
    if sat_meas is None:
        sat_music_image(sat_bin, os.path.join(workdir, 'sat_cal.bin'), name, song0=song)
        mednafen_session(os.path.join(workdir, 'sat_cal.bin'), os.path.join(workdir, 'sat_cal.wav'), 105 + secs, 'MODE1/2352',
                         SAT_KEYS_TO_STAGE, display=displays[0])
        x = _wav(os.path.join(workdir, 'sat_cal.wav')); t0, st, f = cal_locate(x, sched, step)
        sat_meas = dict(t0=t0, tempo=f, notes=cal_measure(x, sched, t0, st))
        pickle.dump(sat_meas, open(os.path.join(workdir, 'sat_meas.pkl'), 'wb'))
    pac, info = build_music_bank(name, seq_song=song, cal_keys=True, tune=tune)
    ps1_title_music_image(ps1_bin, os.path.join(workdir, 'ps1_cal.bin'), pac)
    mednafen_session(os.path.join(workdir, 'ps1_cal.bin'), os.path.join(workdir, 'ps1_cal.wav'), 55 + secs, 'MODE2/2352',
                     PS1_KEYS_TO_TITLE, display=displays[1])
    x = _wav(os.path.join(workdir, 'ps1_cal.wav')); t0, st, f = cal_locate(x, sched, step)
    ps1_meas = dict(t0=t0, tempo=f, notes=cal_measure(x, sched, t0, st))
    return cal_compare(sched, sat_meas['notes'], ps1_meas['notes']), sat_meas, ps1_meas


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

def ps1_frame_emulator(ram, stubs=(0x800F88FC, 0x800F96C0, 0x800FA588, 0x800FCB80, 0x800FCA4C, 0x800FCD84,
                                   0x800F7E08, 0x800F7ECC, 0x800F7F58)):
    """Unicorn machine loaded with a 2 MB PS1 RAM image (e.g. ducc_state()['ram']) that runs the stage frame function
    0x80100E00 (objects, camera, player; no input) one frame per call of the returned step(). Rendering, music loading
    and task switches are stubbed; sound effects run. Returns (uc, step)."""
    from unicorn import Uc, UC_ARCH_MIPS, UC_MODE_MIPS32, UC_MODE_LITTLE_ENDIAN
    from unicorn.mips_const import UC_MIPS_REG_SP, UC_MIPS_REG_RA
    uc = Uc(UC_ARCH_MIPS, UC_MODE_MIPS32 | UC_MODE_LITTLE_ENDIAN)
    uc.mem_map(0, 0x200000); uc.mem_write(0, bytes(ram))
    uc.mem_map(0x1F800000, 0x3000)                         # scratchpad + I/O registers as plain memory
    stop = 0x80000100
    for a in stubs: uc.mem_write(a & 0x1FFFFFFF, struct.pack('<2I', 0x03E00008, 0x24020000))
    uc.mem_write(stop & 0x1FFFFFFF, struct.pack('<2I', 0x0000000D, 0))
    def step():
        uc.reg_write(UC_MIPS_REG_SP, 0x801FFE00); uc.reg_write(UC_MIPS_REG_RA, stop)
        uc.emu_start(0x80100E00, stop, count=20_000_000)
    return uc, step

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

# ----------------------------------------------------------------- DuckStation save states
def ducc_state(path, ovl_bytes=None):
    """DuckStation .sav ('DUCC' header + zstd frames: 256x192 RGBA screenshot, then the state). Returns dict(shot, state,
    ram). RAM (2 MB) is located through the M2 overlay marker (pass the build's STAGE08 overlay bytes); for VRAM use
    ducc_vram."""
    import zstandard
    d = open(path, 'rb').read()
    assert d[:4] == b'DUCC'
    frames = [i for i in range(len(d) - 4) if d[i:i + 4] == b'\x28\xb5\x2f\xfd']
    dec = [zstandard.ZstdDecompressor().decompressobj().decompress(d[i:]) for i in frames[:2]]
    shot, st = dec[0], dec[-1]
    j = st.find(M2_MAGIC)
    out = dict(shot=shot, state=st, ram=None)
    if j >= 0 and ovl_bytes is not None:
        k = ovl_bytes.find(M2_MAGIC)
        base = j - (0x801D8000 + k - 0x80000000)
        out['ram'] = st[base:base + 0x200000]
    return out

def ducc_vram(state, tiles103):
    """Locate VRAM in a DuckStation state from STAGE08's map tiles (page 0 at VRAM (0x200, 0x100))."""
    i = state.find(tiles103[5 * 128:6 * 128])
    assert i >= 0
    base = i - 5 * 2048 - (0x100 * 2048 + 0x200 * 2)
    return state[base:base + 0x100000]

# ----------------------------------------------------------------- Saturn Bonus Mode survey
# Title menu entry 4 on the Saturn (always available). Main loop 0x27B440 (state block 0x002C2E00, state table 0x29B490:
# 0 init 0x27B4F4, 1 menu 0x27B60A, 2 0x27B71E, 3 0x27B86A, 4 exit 0x27B946); file table 0x29B300 = SLABO.PAC, LETTER.PAC,
# then the BONUS/ILLnn.PAC names (13 bytes apart, 0x29B308).
SAT_BONUS_SOUND_TEST = 0x27CDAC     # 32 music numbers (Title ... Woodman stage), same numbers as play_bgm
SAT_BONUS_VOICE_TEST = 0x27CDCC     # (u16 music bank to load, u16 sound number) x 100 on the US disc (0xFF = COMMON);
SAT_BONUS_VOICES_US = 100           # entries 100+ = Cut Man (0x48/0x49) and Wood Man (0x46/0x47) lines, not listed in the US
SAT_BONUS_ILL = 30

def sat_bonus_tables():
    """Saturn Bonus Mode tables: (sound-test music numbers, voice-test (music, sound) pairs as listed on the US disc)."""
    sat = Sat()
    st = list(sat.raw(SAT_BONUS_SOUND_TEST, 32))
    vt = [(sat.u16(SAT_BONUS_VOICE_TEST + 4 * k), sat.u16(SAT_BONUS_VOICE_TEST + 4 * k + 2)) for k in range(SAT_BONUS_VOICES_US)]
    return st, vt

def sat_ill_image(n):
    """Official illustration n (BONUS/ILLnn.PAC): (256 BGR555 colours, 256x256 index array). Chunk 9 = 1024-colour CRAM
    image whose bank 2 (entries 512-767) holds the picture's colours; chunk 0x102 = 256 characters of 16x16 8-bpp pixels
    (linear, 16 bytes per row) in a 16 x 16 grid."""
    import numpy as np
    ch = {t: b for t, o, b in unpac(os.path.join(SAT_FILES, 'BONUS', f'ILL{n:02d}.PAC'))}
    pal = struct.unpack('>256H', ch[9][0x400:0x600])
    px = np.frombuffer(ch[0x102], np.uint8).reshape(16, 16, 16, 16).transpose(0, 2, 1, 3).reshape(256, 256)
    return pal, px

def bgr555_rgb(pal):
    import numpy as np
    return np.array([[(v & 31) * 255 // 31, (v >> 5 & 31) * 255 // 31, (v >> 10 & 31) * 255 // 31] for v in pal], np.uint8)

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
M7_BONUS_MAGIC = b'MM8R-B7\0'
M7_BONUS_BASE = 0x801E4690           # bonus overlay, loaded by the title hook above the resident DEMO overlay. M7e: moved up
                                     # from 0x801E0000 (M7a-M7d) so that the music virtual entries 0x801E4658-0x801E4687
                                     # (patch_music_hook, one vf0 for every overlay) stay free: the sound test plays music
                                     # 0x47 / 0x49 (PBGM47 / PBGM49) through them; bonus_main copies them there
M7E_MUSIC_VF = 0x801E4658            # = PS1_FILETAB + 12 * (vf0 + 0x46); entries for music 0x46-0x49 (checked in patch_m7a)
M7_BONUS_LIMIT = 0x801F8000          # nominal stack reserve (02 §PS1 RAM above the overlay)
PS1_TITLE_CURSOR, PS1_TITLE_SUB, PS1_TITLE_FLAG = 0x801D284C, 0x801CF844, 0x801D1848
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
M7_BONUS_C = r'''
extern void yield_(int n);                    /* 0x800F7ECC */
extern void stop_bgm(void);                   /* 0x800FCD84 */
#define PAD_NEW (*(volatile unsigned short *)0x801B2958)    /* buttons pressed this frame */
#define PAD_TRIANGLE 0x10                                   /* cancel / back, as on the PS1's own screens */
/* M7a: an empty Bonus Mode (black screen, no music); triangle returns to the title. */
void bonus_main(void)
{
    stop_bgm();
    do yield_(1); while (!(PAD_NEW & PAD_TRIANGLE));
}
'''
M7_SYMS = dict(cont_screen=0x8011F0A8, title_rebuild=0x800FF1B8, file_load=0x800FB7A4, confirm_sound=0x800FD100,
               load_done=0x800FC1E0, yield_=0x800F7ECC, stop_bgm=0x800FCD84, flush_cache=0x800D78EC)

def sat_title_records(first=0, n=21):
    """Saturn title-object records (0x295714, big-endian): [(set, pal_add, step, f26, x, y)]."""
    sat = Sat()
    return [struct.unpack('>BBBBhh', sat.raw(SAT_TITLE_RECS + 8 * v, 8)) for v in range(first, first + n)]

def ps1_title_records(exe_img, first=0, n=20):
    return [struct.unpack('<BBBBhh', exe_img.d[PS1_TITLE_RECS + 8 * v - exe_img.b:][:8]) for v in range(first, first + n)]

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
M7B_WAIT = (0x06008160, 0x800F7ECC)          # wait n frames; the replacement also translates the pad (below)
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
M7B_HALF_DATA = [(a + 4, 2) for k in range(6) for a in []]   # filled by m7b_spawn_lists() (x, y of every record)
M7B_STUB_SUBSCREENS = {}   # all lifted: ANIMATION 0x27AFFA (M7g), illustrations 0x27B440 (M7c), postcards 0x27B1C0 (M7d), sound/voice test 0x27B0F8 (M7e)
# M7g: the ANIMATION screen 0x27AFFA plays ROCK8_n.CPK through the Saturn movie player 0x06017FFC(name);
# the PS1 has the same five movies as MOVIE/ROCK8_n.STR (file-table entries 3 + n; frames compared, same lengths) and
# plays them with 0x800FD444(entry) (boot: 3 = opening; the game: 4-7), its own skip rule (table 0x801378F4).
M7B_MOVIE_NAMES, M7B_MOVIE_FIRST, M7B_PS1_MOVIE = 0x0029B2E4, 3, 0x800FD444
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


# ---- M7b differential test: the lifted bonus code against the Saturn code, like the boss difftests
class _BonusSatMem(SatMem):
    """SatMem that also lets the bonus code write its 2NDREAD variables (state blocks, auto-repeat counters)."""
    W_OK = ((0x002C2DF4, 0x002C2E20), (0x0029B2F8, 0x0029B300))
    def w8(s, a, v):
        if 0x00200000 <= a < 0x00300000 and not any(lo <= a < hi for lo, hi in s.W_OK): raise RuntimeError(f'Saturn write to program area {a:#x}')
        s.d[a] = v & 0xFF

class _StopRun(Exception): pass

M7B_P4_LAYOUT = None
def _p4_layout():
    words = [w for w in OBJ_WORD_FIELDS if w + 4 <= 0x40]; out, o = [], 0
    while o < 0x40:
        if o in words: out.append((o, 4)); o += 4
        elif o in (0x2C, 0x2E, 0x34, 0x36): out.append((o, 2)); o += 2
        else: out.append((o, 1)); o += 1
    return out

M7B_DT_ARITY = {0x06008160: 1, 0x00210DA4: 0, 0x0027B3D2: 1, 0x0027B1C0: 1, 0x0027B0F8: 1, 0x0027B440: 0, 0x002761DC: 1, 0x06017FFC: 1,
                0x060099B0: 0, 0x0600A6C8: 3, 0x0600A760: 2, 0x0600A928: 0, 0x0600A646: 0, 0x0600B50C: 1, 0x0600B54C: 1, 0x0600A204: 2,
                0x0600E0A8: 1, 0x06010CA8: 0, 0x0027BB28: 2, 0x0027BA94: 0, 0x06012E70: 1,
                # M7e: player and the sound-driver / CD glue
                0x00209088: 2, 0x06017804: 2, 0x060171C0: 1, 0x06017AB8: 1, 0x0600C85E: 2, 0x0600BEB6: 3, 0x0600BC8C: 0,
                0x0600C610: 1, 0x0600C516: 0, 0x0600A956: 0, 0x060081B4: 1}

class BonusHarness:
    """Saturn code (SH-2 interpreter) vs the compiled bonus overlay (unicorn) on identical random memory: P4 pool (16
    slots), DA98 block, chunk 7/8 tables, layer structs, fade block, bonus variables; every engine helper and glue
    function stubbed with the same return values. Kinds: 'p4_41' / 'p4_40' (one P4 object, `frames` calls), 'spawn'
    (spawner, screen 0-5), 'menu' (the menu loop from its entry for `frames` frames, random pad words per frame)."""
    def __init__(self, workdir=None):
        import tempfile
        wd = workdir or tempfile.mkdtemp(prefix='mm8m7bdt_')
        self.blob, self.info = build_m7b_bonus(wd)
        self.base = M7_BONUS_BASE
        csrc, self.port = m7b_lift()
        self.syms = {}
        for ln in open(os.path.join(wd, 'm7b_bonus', 'out.map')):
            p = ln.split()
            if len(p) == 2 and p[0].startswith('0x') and re.match(r'^[A-Za-z_]\w*$', p[1]): self.syms[p[1]] = int(p[0], 16)
        self.sat = Sat(); self.code = Code([(self.sat.BASE2, self.sat.p2), (self.sat.BASE1, self.sat.p1)])
        self.exe = open(os.path.join(PS1_FILES, 'SLUS_004.53'), 'rb').read()[0x800:]
        hm = dict(load_helper_map()); hm.update(M7B_FRONTEND_HELPERS)
        self.hm = {h: hm[h] for h in self.port.used_helpers}
        for h in M7B_CUSTOM: self.hm[h] = self.syms[f'h_{h:08x}']
        self.arity = dict(helper_arity(hm, list(self.port.used_helpers))); self.arity.update(M7B_DT_ARITY)
        self.valid = set(); self.cov = set()
        for name, e, typ, tabkeys, words, stops in M7B_SPEC['handlers']:
            ins, _, _ = discover(self.code, [e], {k: M7B_CODE_TABLES[k] for k in tabkeys}, None, set(stops)); self.valid |= set(ins)
        lay = _p4_layout()
        self.variants = sorted({r[0] for recs in m7b_spawn_lists(self.sat).values() for p, r in recs if r[0] != 0xFF} | {0x87})   # 0x87: spawner sets bit 7 on variant 7 of screen 4
        self.regions = [(DT_S_P4 + 0x40 * k, DT_P_P4 + 0x40 * k, lay, True) for k in range(16)]
        self.regions += [(0x0602DA98, 0x801B297C, [(o, 1) for o in range(0x2C)], True),
                         (0x0602DF08, 0x80170348, [(o, 2) for o in range(0, 0x200, 2)], False),
                         (0x0602E108, 0x80159F64, [(o, 1) for o in range(0xAC)], False),
                         (0x0602FEEC, 0x8016CA90, [(0, 1)], True), (0x0602DAF0, 0x8015D224, [(o, 1) for o in range(12)], True),
                         (0x0602E20A, 0x8016EC0C, [(0, 2)], True), (0x0602E20C, 0x8016EC10, [(0, 2)], True),
                         (0x06073356, 0x801CF53C, [(0, 1)], True), (0x060733CE, 0x801D28F8, [(0, 1)], True)]   # M7e: sound busy flag, loaded bank
        for k in range(3):
            self.regions.append((0x0602FEF0 + 0x30 * k, 0x801D2914 + 0x30 * k, [(o, 1) for o in range(6)] + [(o, 2) for o in range(6, 0x30, 2)], True))
        rb = self.syms['bonus_ram']
        for lo, hi, off in self.port.ram:
            sz = 2 if lo in (0x0602DA40, 0x060673B4) else 4 if lo == 0x0605F070 else 1
            lay = [(o, sz) for o in range(0, hi - lo, sz)]
            if lo == 0x002C2DF4:      # illustration block 0x2C2E00: halfwords +2 (pad copy), +4/+6/+8 (saved VDP2 registers)
                hw = {0x2C2E02 - lo + k for k in (0, 2, 4, 6)}
                lay = [(o, 2 if o in hw else 1) for o in range(hi - lo) if o - 1 not in hw]
            self.regions.append((lo, rb + off, lay, True))
        sd = self.syms['sat_data']; data, _ = m7b_data_bytes(self.sat, self.port)
        for t, n in M7B_WORD_DATA:                      # the only Saturn data the code writes: auto-repeat counters
            self.regions.append((t, self.s2p(t), [(4 * k, 4) for k in range(n)], False))
    def s2p(self, v):
        if DT_S_P4 <= v < DT_S_P4 + 0x2000: return DT_P_P4 + v - DT_S_P4
        P = self.port
        for t, e, off in P.ptrs:
            if t <= v < e: return self.syms['sat_ptrs'] + off + v - t
        for lo, hi, off in P.regions:
            if lo <= v < hi: return self.syms['sat_data'] + off + v - lo
        for lo, hi, off in P.ram:
            if lo <= v < hi: return self.syms['bonus_ram'] + off + v - lo
        for lo, hi, pa, nm in P.mem:
            if lo <= v < hi: return pa + v - lo
        for lo, hi, pa, nm in P.blocks:
            if lo <= v < hi: return pa + v - lo
        return v
    def trial(self, seed, kind, frames=6):
        from unicorn import Uc, UC_ARCH_MIPS, UC_MODE_MIPS32, UC_MODE_LITTLE_ENDIAN, UC_HOOK_CODE, UcError
        from unicorn.mips_const import UC_MIPS_REG_A0, UC_MIPS_REG_A1, UC_MIPS_REG_A2, UC_MIPS_REG_A3, UC_MIPS_REG_V0, UC_MIPS_REG_RA, UC_MIPS_REG_SP, UC_MIPS_REG_PC
        rnd = random.Random(seed)
        sm = _BonusSatMem(self.code)
        uc = Uc(UC_ARCH_MIPS, UC_MODE_MIPS32 | UC_MODE_LITTLE_ENDIAN); uc.mem_map(0, 0x200000)
        uc.mem_write(0xC0000, self.exe[:0x113000]); uc.mem_write(self.base & 0x1FFFFFFF, self.blob)
        for h, a in self.hm.items(): uc.mem_write(a & 0x1FFFFFFF, struct.pack('<2I', 0x03E00008, 0))
        STOP = 0x80001000; uc.mem_write(STOP & 0x1FFFFFFF, struct.pack('<2I', 0x0000000D, 0))
        def both(sa, pa, v, s): sm.put(sa, v, s); uc.mem_write(pa & 0x1FFFFFFF, v.to_bytes(s, 'little'))
        for sa, pa, lay, rand in self.regions:
            if not rand: continue
            for o, s in lay:
                v = rnd.choice([0, 0, 1, 2, 3, 5, 0x80, rnd.randrange(1 << (8 * s))]) if s < 4 else rnd.choice([0, rnd.randrange(0x400, 0x1600) << 16])
                if s == 4 and _addr_like(v): v ^= 0x40000000     # keep random words out of the mapped address ranges
                both(sa + o, pa + o, v, s)
        if kind in ('p4_41', 'p4_40'):
            slot = rnd.randrange(16); os_, op = DT_S_P4 + 0x40 * slot, DT_P_P4 + 0x40 * slot
            v = rnd.choice(self.variants)        # state 0 = init, which moves to state (variant & 15) + 1 (0x275E82)
            st = {0: 1, 1: rnd.choice([0, (v & 15) + 1]) if kind == 'p4_41' else rnd.randrange(3), 6: 0x41 if kind == 'p4_41' else 0x40, 7: v}
            for o, v in st.items(): both(os_ + o, op + o, v, 1)
            both(0x0602DABD, 0x801B29A1, rnd.randrange(6), 1); both(0x0602DABE, 0x801B29A2, rnd.randrange(30), 1)
            both(0x0602DABF, 0x801B29A3, rnd.randrange(100), 1)
            args_s, args_p = (os_,), (op,)
            entry_s, entry_p = (0x275E58 if kind == 'p4_41' else 0x275E00), self.syms['bonus_' + kind]
        elif kind == 'spawn':
            n = rnd.randrange(6); args_s = args_p = (n,); entry_s, entry_p = 0x2761DC, self.syms['bonus_spawn']
            both(0x0602DABC, 0x801B29A0, rnd.choice([0, 0, 1]), 1)
        elif kind == 'pc':      # M7d: the postcard screen, called once per frame with the menu's state block
            args_s = (0x002C2DF4,); args_p = (self.s2p(0x002C2DF4),); entry_s, entry_p = 0x27B1C0, self.syms['bonus_pc']
            both(0x0602DABD, 0x801B29A1, rnd.choice([0, 0, 1]), 1)
            for a, pa in ((0x0602DABE, 0x801B29A2), (0x0602DABF, 0x801B29A3), (0x002C2DFA, self.s2p(0x002C2DFA))):
                both(a, pa, rnd.choice([0, 1, 23, 24, 29, rnd.randrange(30)]), 1)
        elif kind == 'st':      # M7e: the sound / voice test screen, once per frame with the menu's state block
            args_s = (0x002C2DF4,); args_p = (self.s2p(0x002C2DF4),); entry_s, entry_p = 0x27B0F8, self.syms['bonus_st']
            both(0x0602DABD, 0x801B29A1, rnd.choice([0, 0, 1]), 1)
            both(0x002C2DF8, self.s2p(0x002C2DF8), rnd.choice([2, 2, 3, rnd.randrange(256)]), 1)       # +4 = item (2 sound, 3 voice)
            both(0x002C2DFA, self.s2p(0x002C2DFA), rnd.choice([0, 1, 30, 31, 98, 99, rnd.randrange(100)]), 1)
            both(0x06073356, 0x801CF53C, rnd.choice([0, 0, 1]), 1)
        elif kind == 'sndplay': # M7e: the player 0x209088(entry, 0 sound / 1 voice), including out-of-range entries
            n, v = rnd.choice([rnd.randrange(32), rnd.randrange(100), rnd.randrange(0x100), 27, 96, 0x1F, 0x20, 0xA5, 0xA6]), rnd.choice([0, 1, rnd.randrange(256)])
            args_s, args_p = (n, v), (n, v); entry_s, entry_p = 0x209088, self.syms['bonus_sndplay']
        elif kind == 'ill':     # M7c: the illustration viewer loop from its entry (init, picture load, input, swaps, exit)
            args_s = args_p = (0,); entry_s, entry_p = 0x27B440, self.syms['bonus_ill']
        else:
            args_s = args_p = (0,); entry_s, entry_p = 0x27AAD0, self.syms['bonus_menu']
        calls_s, calls_p, rets = [], [], []
        pads = [[rnd.choice([0, 0, 0x1000, 0x2000, 0x400, 0x100, 0x800, 0x8000, rnd.randrange(0x10000)]) for _ in range(3)] for _ in range(frames + 1)]
        if kind == 'anim':      # menu loop with ANIMATION shown: Down x4, A (enter it), then a movie or random input
            both(0x0602DABC, 0x801B29A0, 1, 1); both(0x0602DAF2, 0x8015D226, 0, 1)
            script = [0, 0, 0, 0x2000, 0, 0x2000, 0, 0x2000, 0, 0x2000, 0, 0x400] + [0] * 6 + [rnd.choice([0x2000, 0x1000, 0])] * rnd.randrange(4) + [rnd.choice([0x400, 0x800, 0x100, 0x200])]
            for i, v in enumerate(script[:frames + 1]): pads[i] = [v, 0, v]
        if kind == 'ill':
            for i in range(frames + 1):
                v = 0 if i < 3 else rnd.choice([0, 0, 0, 0x8000, 0x4000, 0x400, 0x200, 0x100, 0x800, 0x88, rnd.randrange(0x10000)])
                pads[i] = [v | rnd.choice([0, 0, 0x88]), 0, v]
        def ret_for(k, h):
            while len(rets) <= k: rets.append((rnd.random(), rnd.choice([0, 0, 1, 1, 2, 3, 0xFFFFFFFF, rnd.randrange(16)])))
            u, v = rets[k]
            if h == 0x0601470C:
                if u < 0.15: return 0, 0
                i = 1 + (k % 12); return DT_S_P4 + 0x40 * i, DT_P_P4 + 0x40 * i
            return v, v
        nframe = [0, 0]
        def pad_write(side, fr):
            for i, v in enumerate(pads[min(fr, frames)]):
                if side == 0: sm.put(0x0602DA40 + 2 * i, v, 2)
                else: uc.mem_write((self.syms['bonus_ram'] + self.port.ram[1][2] + 2 * i) & 0x1FFFFFFF, v.to_bytes(2, 'little'))
        def sat_hook(h):
            def f(it):
                k = len(calls_s); calls_s.append((h, tuple(it.r[4:4 + self.arity.get(h, 1)]))); it.r[0] = ret_for(k, h)[0]
                if h == 0x06008160 and kind in ('menu', 'anim', 'ill'):
                    nframe[0] += 1; pad_write(0, nframe[0])
                    if nframe[0] >= frames: raise _StopRun()
            return f
        hooks = {h: sat_hook(h) for h in self.hm if h != entry_s}
        p2h = {a: h for h, a in self.hm.items()}
        def p_hook(uc, addr, size, ud):
            if addr in p2h:
                h = p2h[addr]; k = len(calls_p)
                calls_p.append((h, tuple(uc.reg_read(r) for r in (UC_MIPS_REG_A0, UC_MIPS_REG_A1, UC_MIPS_REG_A2, UC_MIPS_REG_A3)[:self.arity.get(h, 1)])))
                uc.reg_write(UC_MIPS_REG_V0, ret_for(k, h)[1])
                if h == 0x06008160 and kind in ('menu', 'anim', 'ill'):
                    nframe[1] += 1; pad_write(1, nframe[1])
                    if nframe[1] >= frames: uc.emu_stop()
        uc.hook_add(UC_HOOK_CODE, p_hook)
        if kind in ('menu', 'anim', 'ill'): pad_write(0, 0); pad_write(1, 0)
        runs = frames if kind in ('p4_41', 'p4_40', 'pc', 'st') else 1
        for fr in range(runs):
            ns, np_ = len(calls_s), len(calls_p)
            if kind in ('pc', 'st'):
                v = rnd.choice([0, 0, 0x1000, 0x2000, 0x4000, 0x8000, 0x400, 0x200, 0x100, 0x08, 0x80, rnd.randrange(0x10000)])
                pads[0] = [v, 0, v]; pad_write(0, 0); pad_write(1, 0)
            it = Interp(self.code, sm, hooks); it.valid = lambda pc: pc in self.valid or 0x06019600 <= pc < 0x06019C00
            it.cov = self.cov
            err = None
            try: it.call(entry_s, args_s, max_steps=400000)
            except _StopRun: pass
            except Halt as e: return dict(ok=None, why=f'invalid trial (saturn {e})')
            except Exception as e: err = f'saturn: {e}'
            uc.reg_write(UC_MIPS_REG_A0, args_p[0]); uc.reg_write(UC_MIPS_REG_A1, args_p[1] if len(args_p) > 1 else 0); uc.reg_write(UC_MIPS_REG_RA, STOP); uc.reg_write(UC_MIPS_REG_SP, 0x801FF000)
            try: uc.emu_start(entry_p, STOP, count=4000000)
            except UcError as e: err = (err or '') + f' ps1: {e} @ {uc.reg_read(UC_MIPS_REG_PC):#x} ra {uc.reg_read(UC_MIPS_REG_RA):#x}'
            if err: return dict(ok=None, why=err)
            cs, cp = calls_s[ns:], calls_p[np_:]
            if len(cs) != len(cp) or any(a[0] != b[0] or tuple(self.s2p(x) for x in a[1]) != b[1] for a, b in zip(cs, cp)):
                return dict(ok=False, why='calls', sat=[(hex(h), [hex(x) for x in a]) for h, a in cs][:20], ps1=[(hex(h), [hex(x) for x in a]) for h, a in cp][:20])
            diffs = []
            for sa, pa, lay, rand in self.regions:
                for o, s in lay:
                    vs = sm.get(sa + o, s); vp = int.from_bytes(uc.mem_read((pa + o) & 0x1FFFFFFF, s), 'little')
                    if (self.s2p(vs) if s == 4 else vs) != vp: diffs.append((hex(sa + o), s, hex(vs), hex(vp)))
            if diffs: return dict(ok=False, why='memory', diffs=diffs[:12], calls=[(hex(h), [hex(x) for x in a]) for h, a in cs][:12])
        return dict(ok=True, calls=len(calls_s))

def difftest_bonus(n=300, kinds=('p4_41', 'p4_40', 'spawn', 'menu', 'anim', 'ill', 'pc', 'st', 'sndplay'), frames=6, workdir=None, uninit=False):
    """(results per kind, first failures, coverage[, uninit report]). uninit=True (M7f): MIPS stack reads before a write
    in the same call and reads outside the EXE image, the bonus overlay and the stack (_uninit_hooks)."""
    H = BonusHarness(workdir); res, first = {}, {}
    rep = restore = None
    if uninit: rep, restore = _uninit_hooks(H.base, H.base + len(H.blob), sentinel=True)
    for kind in kinds:
        c = collections.Counter()
        for seed in range(n):
            r = H.trial(seed, kind, frames)
            key = 'ok' if r['ok'] else ('invalid' if r['ok'] is None else 'FAIL-' + r['why'])
            c[key] += 1
            if key != 'ok' and (kind, key) not in first: first[(kind, key)] = (seed, r)
        res[kind] = c
    if uninit: restore(); return res, first, (len(H.cov & H.valid), len(H.valid)), rep
    return res, first, (len(H.cov & H.valid), len(H.valid))

# ----------------------------------------------------------------- verify
def verify():
    res = []
    def check(name, cond, info=''):
        res.append(cond); print(('PASS ' if cond else 'FAIL ') + name + (f'  [{info}]' if info else ''))
    for (side, fn), h in KNOWN_MD5.items():
        p = os.path.join(SAT_FILES if side == 'sat' else PS1_FILES, fn)
        check(f'md5 {side}:{fn}', os.path.exists(p) and md5(p) == h)
    sat = Sat()
    check('Saturn P2[0x62] = Cut Man handler 0x257E60', sat.u32(SAT_P2 + 4 * 0x62) == CUT)
    check('Saturn P2[0x63] = Wood Man handler 0x258F00', sat.u32(SAT_P2 + 4 * 0x63) == WOOD)
    check('Saturn P2[0x64] = 0x259B7C (unknown, Saturn-only)', sat.u32(SAT_P2 + 4 * 0x64) == T64)
    check('Saturn dispatcher uses P2 table (literal @0x209340)', sat.u32(0x209340) == SAT_P2)
    check('Cut Man placed in Saturn STAGE09', any(fp(r) == '8a06b59123f9' for r in placements('sat', 'STAGE09')))
    check('Wood Man placed in Saturn STAGE08', any(fp(r) == 'dee1c9152813' for r in placements('sat', 'STAGE08')))
    check('Cut Man bolt check 0x258ED0 reads 0x0602DAEB', sat.u32(0x258EF8) == 0x0602DAEB)
    ps = Ps1()
    hdr = open(os.path.join(PS1_FILES, 'SLUS_004.53'), 'rb').read(0x20)
    check('PS1 EXE loads at 0x800C0000', struct.unpack('<I', hdr[0x18:0x1C])[0] == 0x800C0000)
    for st, (tab, lui_p2, lui_p3) in PS1_TABLE.items():
        img = ps.ovl(st)
        check(f'PS1 {st} enemy table found at {tab:#x}', ps.find_enemy_table(img) == tab)
        check(f'PS1 {st} P2 dispatcher -> table', Ps1.lui_addiu(img, lui_p2) == tab)
        check(f'PS1 {st} P3 table starts at index 98', Ps1.lui_addiu(img, lui_p3) == tab + 4 * PS1_P2_LEN)
    check('PS1 STAGE09 has no type-0x62 placement', not any(t == 0x62 and f & 0xFF == 1 for t, f, x, y in placements('ps1', 'STAGE09')))
    check('PS1 STAGE08 has no type-0x63 placement', not any(t == 0x63 and f & 0xFF == 1 for t, f, x, y in placements('ps1', 'STAGE08')))
    s5 = pac_chunks('sat', 'STAGE09')[5]; p5 = pac_chunks('ps1', 'STAGE09')[5]
    sw32 = swap32(s5)
    check('STAGE09 chunk5: PS1 == byteswapped Saturn prefix (+0xBA8 Saturn-only tail)', sw32[:len(p5)] == p5 and len(s5) - len(p5) == 0xBA8)
    s5 = pac_chunks('sat', 'STAGE08')[5]; p5 = pac_chunks('ps1', 'STAGE08')[5]
    sw32 = swap32(s5)
    check('STAGE08 chunk5: PS1 == byteswapped Saturn prefix (+0x1138 Saturn-only tail)', sw32[:len(p5)] == p5 and len(s5) - len(p5) == 0x1138)
    check('PS1 0x80105864 = delete object (sb zero 0..3($a0))', fp([ps.exe.u32(0x80105864 + 4 * i) for i in range(4)]) == '80e9b7777f92')
    check('PS1 0x8010910C = face player (reads player X @0x8015E248)', fp(ps.exe.u32(0x8010910C)) == '961606b366f0' and fp(ps.exe.u32(0x80109110) & 0xFFFF) == '438664df057c')
    # ---- Saturn / PS1 basics
    p9 = placements('ps1', 'STAGE09')
    check('PS1 placement parser: STAGE09 bolt 39 record == Saturn record', any(fp(r) == '79653df28bb9' for r in p9) and any(fp(r) == '79653df28bb9' for r in placements('sat', 'STAGE09')))
    check('Saturn 0x060196A4 / 0x06019800 = compiler divide / modulo (tst r0,r0 + div1 loop)',
          all(fp(sat.u16(a)) == '5fd9b6333fae' and fp(sat.u16(a + 0x14)) == '2550f3df0ab7' for a in (0x060196A4,)) and fp(sat.u16(0x06019800)) == '5fd9b6333fae' and fp(sat.u16(0x06019818)) == '2550f3df0ab7')
    w = lambda a: ps.exe.u32(a)
    check('PS1 0x8011C5D4 = set camera bounds (clears 0x801D2917, reads table 0x8013E020)',
          fp(w(0x8011C5DC)) == '3baea8af3ab4' and fp(w(0x8011C5E0) & 0xFFFF) == 'eb0a4921b181' and fp(w(0x8011C5E4)) == '6549dc060b03' and fp(w(0x8011C5EC) & 0xFFFF) == '43e295a9866b' and fp(w(0x8011C5F4) & 0xFFFF) == '244299ef1051')
    sb = [tuple(sat.s16(0x29ABC0 + 8 * i + 2 * k) for k in range(4)) for i in range(0x7E)]
    pb = [struct.unpack('<4h', ps.exe.d[0x8013E020 - ps.exe.b + 8 * i:0x8013E020 - ps.exe.b + 8 * i + 8]) for i in range(0x7E)]
    check('Camera bounds tables equal for entries 0x00-0x7D (Saturn 0x29ABC0 / PS1 0x8013E020)', sb == pb)
    check('Camera bounds entry 0x7C (Cut Man arena) on PS1 = the Saturn arena bounds', fp(pb[0x7C]) == '07d0fbc4c5ae')
    check('PS1 0x80101BF0 reads camera +6/+8 (0x801D291A/0x801D291C)', fp(w(0x80101BF0)) == 'ff26a5b52cd6' and fp(w(0x80101BF4) & 0xFFFF) == 'd098284fa376' and fp(w(0x80101BFC) & 0xFFFF) == '6171317cb568')
    check('PS1 0x80102E84 stores arg to 0x8015D225 (Saturn 0x0602DAF1)', fp(w(0x80102E94)) == 'cb5ebe7a1afc' and fp(w(0x80102E98)) == 'fdf86ed8badf')
    check('PS1 0x800FCD84 checks sound handle 0x80170548', fp(w(0x800FCD88)) == 'e975c229489d' and fp(w(0x800FCD8C) & 0xFFFF) == '9d551151d715')
    off = 0x80139544 - ps.exe.b
    check('Bit-mask table: Saturn 0x00296C84 / PS1 0x80139548 (01..80)', fp(ps.exe.d[off:off + 12]) == '91afb394fe09'
          and sat.raw(0x296C80, 12) == ps.exe.d[off:off + 12])
    check('Saturn bolt array 0x0602DAE7: P2 0x0C test literal @0x218CBC', sat.u32(0x218CBC) == 0x0602DAE7)
    check('Cut Man = bolt 38 (0x0602DAEB & mask[6]); Wood Man = bolt 19 (0x0602DAE9 & mask[3], 0x259B56)',
          sat.u32(0x258EF8) == 0x0602DAE7 + (38 >> 3) and sat.u32(0x258EFC) == 0x296C84 + (38 & 7)
          and sat.u32(0x259B78) == 0x0602DAE7 + (19 >> 3) and sat.u32(0x259B74) == 0x296C84 + (19 & 7))
    check('PS1 bolt object P3[0x25] = 0x80129684, tests array 0x8016D2FB (lui 0x8017 / -0x2D05)',
          all(ps.ovl(st).u32(PS1_TABLE[st][0] + 4 * (PS1_P2_LEN + 0x25)) == 0x80129684 for st in PS1_TABLE)
          and fp(w(0x801296CC)) == '8604f0a83c55' and fp(w(0x801296D4) & 0xFFFF) == 'bc5f560532cd')
    bolts = lambda side, st: {f >> 8: (x, y) for t, f, x, y in placements(side, st) if t == 0x25 and f & 0xFF == 2 and f >> 8}
    check('Bolt 38: not placed on Saturn STAGE09; on PS1 STAGE09 floor @(0x620,0x578)', 38 not in bolts('sat', 'STAGE09') and fp(bolts('ps1', 'STAGE09').get(38)) == 'b01053d8ccf6')
    check('Bolt 19: not placed on Saturn STAGE05; on PS1 STAGE05 floor @(0xDC0,0xAD8)', 19 not in bolts('sat', 'STAGE05') and fp(bolts('ps1', 'STAGE05').get(19)) == '8ea28ee6c92a')
    # ---- free RAM after the STAGE08 overlay
    s8 = ps.ovl('STAGE08'); s8_end = s8.b + len(s8.d)
    used = {n: sorted(set(t for a, t, m in ps1_absrefs(ps, img) if s8_end <= t < PS1_TASK_BASE and not (n != 'EXE' and img.b <= t < img.b + len(img.d))))
            for n, img in ps1_images(ps, 'EXE,STAGE08')}
    check('No fixed EXE/STAGE08 refs in 0x801E1394-0x801FBFFF (free RAM after STAGE08)', s8_end == 0x801E1394 and not any(used.values()), str({k: [hex(x) for x in v] for k, v in used.items() if v} or ''))
    check('Task scheduler init at 0x800F82CC uses 0x801FC000 slots / 0x801FEC00 stacks', fp(w(0x800F82E0)) == '942999a63c6b' and fp(w(0x800F82E4) & 0xFFFF) == '24826c5b7156' and fp(w(0x800F82DC) & 0xFFFF) == '8d42b9fe493d')
    # ---- M1: file table, overlay loader, placement terminator, M1 overlay builder
    fte = lambda i: tuple(w(PS1_FILETAB + 12 * i + 4 * k) for k in range(3))
    check('PS1 file table 0x80136F7C: #17 = STAGE08 (LBA 126370, 0x9394, type 0xE), #18 = STAGE09', fte(17) == (126370, 0x9394, 0xE) and fte(18) == (126389, 0x3B44, 0xF))
    check('Overlay loader 0x800FC648: dest 0x801D8000 (lui @0x800FC650, ori @0x800FC688), file = 0x8013761C[n], STAGE08 n=9 -> #17',
          fp(w(0x800FC650)) == 'ff26a5b52cd6' and fp(w(0x800FC688)) == 'b3831f36ee2f' and w(0x8013761C + 4 * 9) == 17 and w(0x800FC684) == 0x0C000000 | (0x800FB7A4 >> 2) & 0x3FFFFFF)
    b8 = pac_chunks('ps1', 'STAGE08')[0xA]
    check('PS1 STAGE08 placement list: 109 records, terminator 00 00 00 FF', len(placements('ps1', 'STAGE08')) == 109 and placement_terminator(b8) == 109 * 8 and b8[109 * 8:109 * 8 + 4] == b'\0\0\0\xff')
    new, ntab, nstub = relocate_enemy_table(s8.d, 'STAGE08', [None, 0x801E2000, 0x801E3000])
    ni = Img(bytes(new), Ps1.OVL_BASE)
    check('Enemy-table relocation (STAGE08): both lui/addiu pairs repointed, P2 0x00-0x61 and P3 preserved, 0x62 = stub, 0x63/0x64 = new handlers',
          Ps1.lui_addiu(ni, 0x801D81B4) == ntab and Ps1.lui_addiu(ni, 0x801D823C) == ntab + 101 * 4
          and [ni.u32(ntab + 4 * k) for k in range(98)] == [s8.u32(0x801E0CEC + 4 * k) for k in range(98)]
          and [ni.u32(ntab + 4 * (101 + k)) for k in range(52)] == [s8.u32(0x801E0CEC + 4 * (98 + k)) for k in range(52)]
          and [ni.u32(ntab + 4 * k) for k in (0x62, 0x63, 0x64)] == [nstub, 0x801E2000, 0x801E3000]
          and sum(1 for i in range(0, len(s8.d), 4) if new[i:i + 4] != s8.d[i:i + 4]) == 2)
    check('Progress sequencer 0x80100A34 phase 0 stores stage 0 at 0x80100A90 (quick-start patch site intact)', fp([w(QS_ADDR + 4 * i) for i in range(6)]) == QS_ORIG_FP)
    cp8 = w(0x80138288 + 4 * 8); r5 = w(cp8 + 4 * 5)
    check('Stage start clears checkpoint 0x801C3371 at 0x80100B14; stage 8 checkpoint 5 = player (0x1180, 0x400), camera locked (0x10E0, 0x400), same record on the Saturn',
          fp([w(QS_CP_ADDR), w(QS_CP_ADDR + 4)]) == QS_CP_ORIG_FP and fp(w(0x8010183C)) == '5b79bd07d930' and
          bytes(ps.exe.d[r5 - ps.exe.b:r5 - ps.exe.b + 0x18]) == bytes(sat.p2[0x27D4C0 + 5 * 0x18 - sat.BASE2:0x27D4C0 + 6 * 0x18 - sat.BASE2])[:8] + struct.pack('<8H', 0x1180, 0x400, 0x10E0, 0x400, 0x10E0, 0x10E0, 0x400, 0x400))
    # ---- M2 prep: Wood Man structure, pools, PS1 stand-in object, global map
    check('Wood Man jump tables: phase 0x2933DC (4), intro 0x2933EC (5), fight 0x293400 (8), defeat 0x293440',
          [sat.u32(0x2933DC + 4 * i) for i in range(4)] == [0x258F14, 0x258F70, 0x2592D6, 0x2598A2]
          and sat.u32(0x2933EC) == 0x258F84 and sat.u32(0x293400) == 0x2593A6 and sat.u32(0x293420) == 0x259476
          and sat.u32(0x293430) == 0x2595FC and sat.u32(0x293440) == 0x2598D0)
    check('Wood Man defeat: spawns P3 0x25 (bolt) variant 19, sets 0x0602DAB1 = 0x80; spawn_leaf 0x259AF4 makes type 0x64',
          fp(sat.u16(0x259A9A)) == '77dc3bf9f6f2' and fp(sat.u16(0x259A9E)) == '607345224ca6' and sat.u32(0x259AE8) == 0x060146AE
          and sat.u32(0x259AEC) == 0x0602DAB1 and fp(sat.u16(0x259AC0)) == 'dbf8690fe1ed' and fp(sat.u16(0x259B16)) == '7bd4b0973344')
    check('PS1 pool allocators 0x80138474: pool byte 2 = P3, 3 = P4, 4 = P5 (32 x 0x18 @0x801CF540)',
          [w(PS1_POOL_ALLOC + 4 * i) for i in range(2, 5)] == [0x801056F4, 0x80105770, 0x801057EC]
          and fp(w(0x801057EC)) == 'ea28f131ad38' and fp(w(0x801057F0) & 0xFFFF) == '5a7333d04f65' and fp(w(0x801057F4) & 0xFFFF) == 'f7b856c054de')
    check('PS1 STAGE08 P5 loop uses pool 0x801CF540 / table 0x8015039C; P5[0x30] = 0x801E0C6C',
          Ps1.lui_addiu(s8, 0x801D8354) == 0x801CF540 and Ps1.lui_addiu(s8, 0x801D835C) == PS1_P5_TABLE
          and w(PS1_P5_TABLE + 4 * 0x30) == 0x801E0C6C)
    check('PS1-only P5 0x30 (STAGE08 0x801E0C6C): when player X passes it, 0x801B2995 = 0x80 and player+0x73 = 1 (Wood Man defeat effects)',
          fp(s8.u32(0x801E0C94)) == 'c3ba9dbb6135' and fp(s8.u32(0x801E0C98)) == 'f6657ef8cb20' and fp(s8.u32(0x801E0C9C)) == '5a1f9d247ca5'
          and fp(s8.u32(0x801E0CA4)) == 'cb5ebe7a1afc' and fp(s8.u32(0x801E0CA8)) == 'e7aeb0a58c19')
    p8, q8 = placements('sat', 'STAGE08'), placements('ps1', 'STAGE08')
    check('STAGE08: Saturn gates P3 0x22 @0x10F0/0x1210 + P5 0x04/0x0A + Wood Man replaced on PS1 by one P5 0x30 @(0x10E0,0x480)',
          set(['58d0808c2137', '69cf2d37a32c', '7a8f278b6614', 'b12134bbecc8']) <= {fp(r) for r in p8}
          and not any(t == 0x22 and x in (0x10F0, 0x1210) for t, f, x, y in q8)
          and fp([r for r in q8 if r[0] == 0x30 and r[1] & 0xFF == 4]) == 'a593f28148f2')
    # ---- Search Man's stage map (PS1 loads past the first gate)
    g_s, g_p = pac_chunks('sat', 'STAGE08')[0], pac_chunks('ps1', 'STAGE08')[0]
    cells = {(4, 0x11): 0x3E, (4, 0x12): 0x3F, (4, 0x13): 0x40, (3, 0x12): 0x21, (3, 0x13): 0x43}
    check('STAGE08 chunk 0 (32x32 screen grid): Wood Man hallway/arena cells present on Saturn, zero on PS1, rest identical',
          all(g_s[r * 32 + c] == v and g_p[r * 32 + c] == 0 for (r, c), v in cells.items())
          and sum(1 for i in range(1024) if g_s[i] != g_p[i]) == 5)
    c3s, c3p = pac_chunks('sat', 'STAGE08')[3], pac_chunks('ps1', 'STAGE08')[3]
    c4s, c4p = pac_chunks('sat', 'STAGE08')[4], pac_chunks('ps1', 'STAGE08')[4]
    S3 = [struct.unpack('>256H', c3s[k * 512:k * 512 + 512]) for k in range(len(c3s) // 512)]
    P3 = [struct.unpack('<256H', c3p[k * 512:k * 512 + 512]) for k in range(len(c3p) // 512)]
    bm = collections.defaultdict(collections.Counter)
    for i in range(len(P3)):
        for x, y in zip(S3[i], P3[i]): bm[x][y] += 1
    fmap = {x: c.most_common(1)[0][0] for x, c in bm.items()}
    badscr = sorted(set(i for i in range(len(P3)) for x, y in zip(S3[i], P3[i]) if fmap[x] != y))
    check('STAGE08 screens (chunk 3, 512 B each): Saturn 68, PS1 62 (0x3E-0x43 cut); blocks (chunk 4, 4 B each) 1921 vs 1818; '
          'PS1 = Saturn with blocks renumbered, except edited screens 0x16 (first-gate screen) and 0x1A',
          len(S3) == 68 and len(P3) == 62 and len(c4s) == 1921 * 4 and len(c4p) == 1818 * 4 and badscr == [0x16, 0x1A])
    # ---- block / graphics formats
    s8b = {c: pac_chunks('sat', 'STAGE08B')[c] for c in (4, 0x102, 0x103)}; p8b = {c: pac_chunks('ps1', 'STAGE08B')[c] for c in (4, 0x102, 0x103)}
    check('Block defs (chunk 4) Saturn->PS1 rule exact on STAGE08B and STAGE01 (tile = hi>>2, attr & ~0x40)',
          blocks_sat_to_ps1(s8b[4]) == p8b[4] and blocks_sat_to_ps1(pac_chunks('sat', 'STAGE01')[4]) == pac_chunks('ps1', 'STAGE01')[4])
    check('Graphics 0x103 (8x8-cell tiles) and 0x102 (linear tiles) convert byte-exact to PS1 texture pages (STAGE08B)',
          gfx_sat_to_ps1(s8b[0x103], True) == p8b[0x103] and gfx_sat_to_ps1(s8b[0x102], False) == p8b[0x102])
    fm8, _, _ = stage_block_map('STAGE08')
    a4, b4, a3g, b3g = c4s, c4p, pac_chunks('sat', 'STAGE08')[0x103], pac_chunks('ps1', 'STAGE08')[0x103]
    tm8 = {struct.unpack('>H', a4[4 * x:4 * x + 2])[0] >> 2: struct.unpack('<H', b4[4 * y:4 * y + 2])[0] for x, y in fm8.items() if 4 * x < len(a4) and 4 * y < len(b4)}
    used_s = set(struct.unpack('>H', a4[i:i + 2])[0] >> 2 for i in range(0, len(a4), 4))
    gone = sorted(used_s - set(tm8)); used_p = set(struct.unpack('<H', b4[i:i + 2])[0] for i in range(0, len(b4), 4))
    pst = {ps1_tile(b3g, t) for t in range(2048)}
    check('STAGE08: the 78 arena-only tiles (Saturn 0x715-0x762) are absent from PS1 0x103; PS1 tiles 0x6FE-0x7FF free',
          len(gone) == 78 and gone[0] == 0x715 and gone[-1] == 0x762 and not any(sat_tile(a3g, t) in pst for t in gone)
          and max(used_p) == 0x6FD and not any(any(ps1_tile(b3g, t)) for t in range(0x6FE, 0x800)))
    g9s, g9p = pac_chunks('sat', 'STAGE09')[0], pac_chunks('ps1', 'STAGE09')[0]
    cut9 = {(4, 5): 0x10, (4, 6): 0x18, (4, 7): 0x19, (4, 0xC): 0x1A, (4, 0xD): 0x1B, (4, 0xE): 0x1C}
    check('STAGE09: Cut Man hidden area (row 4, cols 5-7) and arena (cols 0xC-0xE) cut on PS1; screens 29 -> 24, blocks 1392 -> 1244',
          all(g9s[r * 32 + c] == v and g9p[r * 32 + c] == 0 for (r, c), v in cut9.items()) and sum(1 for i in range(1024) if g9s[i] != g9p[i]) == 6
          and len(pac_chunks('sat', 'STAGE09')[3]) == 29 * 512 and len(pac_chunks('ps1', 'STAGE09')[3]) == 24 * 512
          and len(pac_chunks('sat', 'STAGE09')[4]) == 1392 * 4 and len(pac_chunks('ps1', 'STAGE09')[4]) == 1244 * 4)
    # ---- stage PAC loading
    ex = ps.exe
    pdest = lambda t: ex.u32(PS1_PAC_DEST + 4 * t)
    check('Stage PAC load: 0x8016D294 = dest table 0x80137850, then streamer 0x800FB9C0(file 0x8013794C[stage])',
          Ps1.lui_addiu(ex, 0x800FF2AC) == PS1_PAC_DEST and fp(ex.u32(0x800FF2C8)) == '14ceca091a19' and fp(ex.u32(0x800FF2DC)) == '926c3991090f')
    check('Stage PAC dests: chunks 0/1/2 = 0x8016EF34/F334/F734 (contiguous), 3 = 0x80190040, 4 = 0x8015EA88, 0xA = 0x801C2B3C',
          [pdest(t) for t in (0, 1, 2, 3, 4, 0xA)] == [0x8016EF34, 0x8016F334, 0x8016F734, PS1_SCREEN_STAGING, 0x8015EA88, 0x801C2B3C])
    check('Streamer: new chunk -> dest[type & 0x7F] (0x800FBD60); copies min(left, 0x800) bytes per sector (0x800FC084); RAM handler 0x800FBD40',
          fp(ex.u32(0x800FBD60)) == '0c268d455239' and fp(ex.u32(0x800FBD64)) == '96762afc74e9' and fp(ex.u32(0x800FC084)) == '02fa6858c0c3'
          and ex.u32(0x80137600) == 0x800FBD40)
    check('Screens: after load, 0x1E000 bytes (240 screens) copied 0x80190040 -> 0x80171C3C (0x800FF3A4)',
          Ps1.lui_addiu(ex, 0x800FF390) == PS1_SCREEN_LIVE and Ps1.lui_addiu(ex, 0x800FF398) == PS1_SCREEN_STAGING
          and fp(ex.u32(0x800FF3A4)) == '6f0ad319c650' and fp(ex.u32(0x800FF3A8)) == '73f512313013')
    e08 = pac_entries(os.path.join(PS1_FILES, 'STDATA', 'STAGE08.PAC'))
    m08 = pac_merge_grids(e08)
    check('pac_build rebuilds PS1 STAGE08.PAC byte-exact; merging grid chunks 0-2 gives the same RAM and frees one sector',
          pac_build(e08) == open(os.path.join(PS1_FILES, 'STDATA', 'STAGE08.PAC'), 'rb').read()
          and pac_ram(m08, pdest) == pac_ram(e08, pdest) and len(pac_build(m08)) == len(pac_build(e08)) - 0x800)
    # ---- M2a map restoration
    rnew, rinfo = restore_stage08_map()
    s08 = pac_chunks('sat', 'STAGE08')
    def _cell(ch, scr, sat_side):
        e = struct.unpack(('>' if sat_side else '<') + '256H', ch[3][scr * 512:scr * 512 + 512]); out = []
        for b in e:
            blk = ch[4][4 * b:4 * b + 4]
            if sat_side: out.append((sat_tile(ch[0x103], struct.unpack('>H', blk[:2])[0] >> 2), block_sat_to_ps1(blk)[2:]))
            else: out.append((ps1_tile(ch[0x103], struct.unpack('<H', blk[:2])[0]), blk[2:]))
        return out
    on_grid = sorted({v for g in (0, 1) for v in rnew[g] if v})
    check('M2a: restored STAGE08 map = Saturn on every grid cell (tile pixels + block bytes 2/3); 68 screens, 1952 blocks (32 door blocks at 1920), 116 new tiles 0x6FE-0x771 (no reuse of other PS1 tile slots)',
          rnew[0] == s08[0] and rnew[1] == s08[1] and all(_cell(rnew, k, False) == _cell(s08, k, True) for k in on_grid)
          and (rinfo['screens'], rinfo['blocks'], rinfo['new_tiles'], rinfo['tile_range'], rinfo['door_blocks']) == (68, 1952, 116, (0x6FE, 0x771), 1920))
    p08 = pac_chunks('ps1', 'STAGE08'); bm08, _, _ = stage_block_map('STAGE08')
    tm08 = {struct.unpack('>H', s08[4][4 * x:4 * x + 2])[0] >> 2: struct.unpack('<H', p08[4][4 * y:4 * y + 2])[0]
            for x, y in bm08.items() if 4 * x < len(s08[4]) and 4 * y < len(p08[4])}
    okt = all((lambda tt, st: tt >= M2A_TILE_FIRST or tm08.get(st) == tt)(struct.unpack('<H', rnew[4][4 * y:4 * y + 2])[0],
              struct.unpack('>H', s08[4][4 * x:4 * x + 2])[0] >> 2) for x, y in rinfo['bmap'].items() if y >= len(p08[4]) // 4)
    okt = okt and all(rnew[4][4 * (1920 + k) + 2:4 * (1920 + k) + 4] == block_sat_to_ps1(s08[4][4 * (k + 1):4 * (k + 2)])[2:] for k in range(32))
    check('M2a: new blocks only use fresh tile slots (>= 0x6FE) or the tile the PS1 itself uses for the same Saturn tile '
          '(reusing other pixel-identical PS1 slots garbled the gates: run-time overwritten tiles)', okt)
    e08r = pac_merge_grids([(t, rnew.get(t, b)) for t, b in e08])
    ram0 = pac_ram(e08, pdest)
    check('M2a: restored STAGE08.PAC (grids merged) is 0x9F000 bytes = original slot; RAM differs only in grids, screens, blocks',
          len(pac_build(e08r)) == 0x9F000 and {a for a, v in pac_ram(e08r, pdest).items() if ram0.get(a) != v}
          <= set(range(0x8016EF34, 0x8016F734)) | set(range(PS1_SCREEN_STAGING, PS1_SCREEN_STAGING + 68 * 512)) | set(range(0x8015EA88, 0x8015EA88 + 1952 * 4)))
    mx = {}
    for nm in ('STAGE01', 'STAGE04', 'STAGE05B', 'STAGE08B'):
        b = pac_chunks('ps1', nm)[4]; mx[nm] = max(struct.unpack('<H', b[i:i + 2])[0] for i in range(0, len(b), 4))
    check('Map tiles 0x6FE-0x7FF are drawn by other stages (STAGE01/04/05B/08B use tiles up to 0x7FB/0x7FD/0x7FE/0x7BA)',
          mx == {'STAGE01': 0x7FB, 'STAGE04': 0x7FD, 'STAGE05B': 0x7FE, 'STAGE08B': 0x7BA})
    c0a = pac_chunks('ps1', 'STAGE08')[0xA]; r0a = m2a_placements(c0a)
    check('M2a placements: stand-in P5 0x30 removed, 2 gates + P5 0x04/0x0A + Wood Man = the Saturn records',
          rec_tuple(m2a_standin()) not in [tuple(struct.unpack('>2H', r0a[i:i + 4]) + struct.unpack('<2H', r0a[i + 4:i + 8])) for i in range(0, placement_terminator(r0a), 8)]
          and all(struct.unpack('>2H', r[:4]) + struct.unpack('<2H', r[4:]) in placements('sat', 'STAGE08') for r in m2a_records())
          and placement_terminator(r0a) == placement_terminator(c0a) + 4 * 8)
    # ---- gate sprite / Wood Man sprite data cut with the arena
    sp, si = restore_stage08_sprites()
    vv = [ex.u32(0x801292C4 + 8 * k) & 0xFFFF for k in range(7)]
    check('Shared gate code: variants 5/0x86/0x91/0x17/0x98/0x19/0x9B (incl. Wood Man\'s 0x19/0x9B) use sprite set 0xA8 (chunk-7 RAM 0x80170498); PS1 STAGE08 chunk 7 stops at 0x9E entries',
          fp(vv) == 'd43bd2948c15' and fp(ex.u32(0x80129300)) == 'b5e477ded3b5' and fp(ex.u32(0x80129308)) == '8eb47175424f'
          and fp(ex.u32(0x8012930C)) == 'ef947f3fb1e7' and pdest(7) + 2 * 0xA8 == 0x80170498 and len(pac_chunks('ps1', 'STAGE08')[7]) == 0x9E * 2)
    check('STAGE08 sprite restore: 0x102 tiles 0x330-0x5FF (545) blank on PS1; chunk 5/6/7/8 Saturn tails (14 sprite sets) convert onto the PS1 prefixes',
          si['sprite_tiles'] == 545 and si['tile_span'] == (0x330, 0x5FF) and si['sizes'] == {5: (51704, 56112), 6: (632, 688), 7: (316, 344), 8: (158, 172)}
          and struct.unpack('<H', sp[7][2 * 0xA8:2 * 0xA8 + 2])[0] == 0xB30)
    check('STAGE08 palettes 0x30-0x33 are all zero on PS1 (Saturn: Wood Man/leaf colours; chunk 8 gives set 0xAB palette 0x30); restored from the Saturn',
          si['palettes'] == [0x30, 0x31, 0x32, 0x33] and pac_chunks('sat', 'STAGE08')[8][0xAB] == 0x30
          and [k for k in range(128) if struct.unpack('>16H', pac_chunks('sat', 'STAGE08')[9][32 * k:32 * k + 32]) != tuple(v & 0x7FFF for v in struct.unpack('<16H', pac_chunks('ps1', 'STAGE08')[9][32 * k:32 * k + 32]))] == [0x30, 0x31, 0x32, 0x33])
    # ---- M2 Wood Man port
    ib = 0x801393B4
    check('PS1 object-init table 0x801393B4 (EXE 0x80109080, indexed by type +6) keeps the Saturn records for 0x62-0x64 (sprite sets 0xAA/0xAB/0xAB)',
          fp(ex.u32(0x80109084)) == 'a13abf3fe2dc' and fp(ex.u32(0x80109088)) == '8ccac2a18190'
          and all(struct.pack('<I', ex.u32(ib + 4 * k)) == bytes(sat.p2[0x296AF0 + 4 * k - 0x200000:0x296AF0 + 4 * k - 0x200000 + 4]) for k in range(0x65)))
    csrc, wport = wm_lift()
    sc = Code([(sat.BASE2, sat.p2), (sat.BASE1, sat.p1)])
    nins = [len(discover(sc, [e], {k: v for k, v in WM_CODE_TABLES.items() if (k < 0x29344C) == (ty == 0x63)}, None)[0]) for _, e, ty in WM_HANDLERS]
    check('SH-2 lifter: Wood Man (1373 instructions) and leaf (1023) lift to C; all 29 engine calls are mapped helpers',
          nins == [1373, 1023] and len(wport.used_helpers) == 29 and 'UNTRANSLATED' not in csrc)
    ok = True
    for x, y in ((100, 6), (-100, 6), (7, -3), (12345, 55), (-1, 7)):
        it = Interp(sc, SatMem(sc), {}); it.r[1] = x & 0xFFFFFFFF; it.r[0] = y & 0xFFFFFFFF
        ok &= s32(it.call(0x06019800, sp=0x060FFF00)) == (abs(x) % abs(y)) * (1 if x >= 0 else -1)
    for c in (-1, 0, 1, 5, 31, 32, 40):
        it = Interp(sc, SatMem(sc), {}); it.r[1] = c & 0xFFFFFFFF; it.r[0] = 0x9ABCDEF1
        ok &= it.call(0x06019A98, sp=0x060FFF00) == (0x9ABCDEF1 if c < 0 else 0 if c >= 32 else (0x9ABCDEF1 << c) & 0xFFFFFFFF)
    check('SH-2 interpreter runs the Saturn runtime modulo (0x06019800) and shift (0x06019A98) = the lifter\'s inline C semantics', ok)
    p012 = pac_strings(pac_chunks('ps1', 'STAGE09')[0x12]); s012 = pac_strings(pac_chunks('sat', 'STAGE09')[0x12])
    check('Stage text (chunk 0x12 -> RAM 0x800A0000): PS1 STAGE08 lost entry 17 (Wood Man\'s talk); STAGE09 keeps all 36 incl. Cut Man\'s talks, same format',
          restore_stage08_text()[1]['text_entries'] == (17, 18) and p012 == s012 and len(p012) == 36
          and ex.u32(PS1_PAC_DEST + 4 * 0x12) == 0x800A0000)
    u16e = lambda a: struct.unpack('<H', bytes(ex.d[a - ex.b:a - ex.b + 2]))[0]
    st8 = ex.u32(0x8013C620 + 4 * 8); door = []
    for pid in range(4, 8):
        a = ex.u32(st8 + 4 * pid)
        while u16e(a) != 0xFFFF:
            lid, cnt = u16e(a), ex.d[a + 3 - ex.b]; door.append((lid, struct.unpack('<hh', bytes(ex.d[a + 4 - ex.b:a + 8 - ex.b])),
                                                                 [u16e(ex.u32(PS1_BLOCK_LISTS + 4 * lid) + 2 * k) for k in range(cnt)])); a += 8
    users = collections.Counter()
    for stg in range(0x0E):
        st = ex.u32(0x8013C620 + 4 * stg)
        if not ex.has(st): continue
        for pid in range(64):
            pp = ex.u32(st + 4 * pid)
            if not ex.has(pp): break
            while u16e(pp) != 0xFFFF: users[u16e(pp)] += 1; pp += 8
    check('Stage 8 block patches 4-7 = Wood Man\'s doors at (0x10E0,0x420)/(0x1200,0x490): EXE lists 176-183 (used nowhere else) hold Saturn block numbers 0x01-0x20, which PS1 STAGE08 renumbered (Saturn 0x21 = PS1 0x01)',
          [d[0] for d in door] == WM_DOOR_LISTS and sorted(v for d in door for v in d[2]) == list(range(1, 0x21))
          and {d[1] for d in door} == {(4320, 1056), (4336, 1056), (4608, 1168), (4624, 1168)} and all(users[l] == 1 for l in WM_DOOR_LISTS)
          and stage_block_map('STAGE08')[0][0x21] == 1)
    sb = {n: sat_sound_bank(n) for n in ('WOOD1.PAC', 'WOOD2.PAC', 'CUT1.PAC', 'CUT2.PAC', 'BS08B.PAC', 'ST08A.PAC')}
    ftp = [struct.unpack('<3I', bytes(ex.d[PS1_FILETAB - ex.b + 12 * i:PS1_FILETAB - ex.b + 12 * i + 12])) for i in range(139)]
    check('Sound banks: WOOD1/WOOD2/CUT1/CUT2 = Saturn banks 0x47-0x4A (music 0x46-0x49, Sega SEQ at 0x26000); PS1 PBGM00-45 = file-table entries 23-92 (type 4), none for music >= 0x46',
          [sb[n]['bank'] for n in ('WOOD1.PAC', 'WOOD2.PAC', 'CUT1.PAC', 'CUT2.PAC')] == [0x47, 0x48, 0x49, 0x4A]
          and all(sb[n]['songs'] for n in sb) and sorted(sb['WOOD2.PAC']['ids'])[1:15] == list(range(2, 0x10))
          and [i for i, e in enumerate(ftp) if e[2] == 4] == list(range(23, 93)))
    j = sat_bank_song('WOOD1.PAC', 0)
    raw8 = {t: b for t, o, b in unpac(os.path.join(SAT_FILES, 'BS08B.PAC'))}[0x203]
    check('Boss intro jingle: WOOD1/CUT1 song 0 == every robot-master B bank song 0 (BS08B); WOOD1 music voices 1-3 samples also in BS08B; WOOD1 voice-0 notes 0/1 (IDs 1-3) unique',
          all(sat_bank_song(n, 0) == j for n in ('CUT1.PAC', 'BS08B.PAC', 'BS01B.PAC', 'DUO00.PAC')) and len(j) == 3048
          and all(lay[0][5] in raw8 for lay in sat_ton_voices('WOOD1.PAC')[1:])
          and all(sat_ton_voices('WOOD1.PAC')[0][k][5] not in raw8 for k in (0, 1)))
    # ---- PS1 sound banks, PBGM46, music hook
    rt_ok = True
    for n in range(0x46):
        pth = os.path.join(PS1_FILES, 'SOUND', f'PBGM{n:02X}.PAC'); ch = unpac(pth); cc = {t: b for t, o, b in ch}
        hh, bb = vab_join(vab_split(cc[4], cc[0x201]))
        rt_ok &= hh == cc[4] and bb == cc[0x201] and pac_build([(t, b) for t, o, b in ch]) == open(pth, 'rb').read()
    v29 = vab_split(ps1_pbgm(0x29)[4], ps1_pbgm(0x29)[0x201])
    check('PS1 sound banks: VAB/PAC writers reproduce all 70 PBGM files; VAG encoder re-encodes Capcom PBGM29 VAGs 1/4/7/8 byte for byte',
          rt_ok and all(vag_encode(vag_decode(v29['vags'][k])[28:-28]) == v29['vags'][k] for k in (0, 3, 6, 7)))
    vc = _vag_c(); rnd = random.Random(8); xs = [([rnd.randrange(-32768, 32768) >> rnd.randrange(16) for _ in range(28)], rnd.randrange(-9000, 9000), rnd.randrange(-9000, 9000)) for _ in range(400)]
    check('ADPCM block search: C accelerator and Python reference agree (400 random blocks)' if vc else 'ADPCM block search: no host C compiler, Python reference used',
          not vc or all(_vag_block(*x) == _vag_block_py(*x) for x in xs))
    c29 = ps1_pbgm(0x29)[5]
    check('PS1 sound-ID entry rule (index = Saturn ID + 0x50: bank|0x80, 0x10 + note//15, note%15 << 4 | pri, 0x10 | voice) reproduces PBGM29 from BS08B',
          all(c29[4 * (i + 0x50):4 * (i + 0x50) + 4] == bytes([0xAA, 0x10, (nt << 4) | (e[4] & 15), 0x10 | e[1]]) for i, e, pg, nt in sat_bank_sounds('BS08B.PAC'))
          and sum(1 for k in range(0, len(c29), 4) if any(c29[k:k + 4])) == len(sat_bank_sounds('BS08B.PAC')))
    ftab = [ps.exe.u32(0x80137714 + 4 * n) for n in range(0x48)]
    check('PS1 play_bgm: file number table 0x80137714[n] = 0x17 + n for n < 0x46 (then other data); loop flags 0x801376D4 has 0x40 entries; hook sites unpatched',
          ftab[:0x46] == [0x17 + n for n in range(0x46)] and ftab[0x46] == 0x1E
          and all(fp(ps.exe.u32(a + 12)) == 'a984665d2fa6' and fp(ps.exe.u32(a + 32)) == '926c3991090f' for a in PS1_BGM_FILE_SITES)
          and all(fp(ps.exe.u32(a + 8)) == '641a930ca317' and fp(ps.exe.u32(a + 52)) == '860648abcb66' for a, x in PS1_BGM_LOOP_SITES))
    check('PS1 sound play: bank-mismatch branch 0x800FCF14 -> 0x800FCFE0 (uses the caller\'s a2 as voice; guard target epilogue 0x800FD0CC); 0x8010544C passes the ID as a2',
          fp(ps.exe.u32(PS1_SND_MISMATCH)) == PS1_SND_MISMATCH_ORIG_FP and fp(ps.exe.u32(0x800FD0CC)) == '4c8a54aedfb2'
          and fp(ps.exe.u32(0x80105474)) == 'faeb92e4bb28' and fp(ps.exe.u32(0x8010549C)) == '8f183a27d9c6')
    pac46, i46 = build_boss_intro_bank('WOOD1.PAC')
    c46 = {t: b for t, o, b in unpac_bytes(pac46)}
    v46 = vab_split(c46[4], c46[0x201])
    rate = lambda t, up: 44100 * 2 ** ((t[6] - t[4]) / 12) / up
    check('PBGM46 (WOOD1 on PBGM29): muted note 0 (sound 0x51, level 0 = silent on the US Saturn) left out; IDs 0x52/0x53/0x13E bank 0xC7, 3 tones at the Saturn pitch; SEQ + music VAGs = PBGM29',
          [k // 4 for k in range(0, len(c46[5]), 4) if any(c46[5][k:k + 4])] == [0x52, 0x53, 0x13E] and c46[1] == ps1_pbgm(0x29)[1]
          and v46['vags'][:2] == v29['vags'][:2] and i46['reused'] == [(3, 17791)] and i46['muted'] == [(0, 10239)] and c46[4][0x20 + 16 * 16] == 3
          and all(abs(rate(v46['tones'][16][t], up) - 44100 * 2 ** ((k - b) / 12)) < 1 for t, k, b, up in ((0, 1, 24, 1), (1, 2, 27, 1), (2, 3, 27, 2))))
    gmv = load_global_map()
    # ---- Sega SEQ -> Sony SEQ, TON -> VAB (WOOD2 -> PBGM47)
    ok_len = bad_len = 0
    for nm in sorted(os.listdir(SAT_FILES)):
        if not nm.endswith('.PAC'): continue
        try: songs = sat_sound_bank(nm)['songs']
        except Exception: continue
        if not songs: continue
        try: A = sega_seq_decode(sat_bank_song(nm, 0))
        except (NotImplementedError, AssertionError): continue
        if len(A['tempos']) == 2: ok_len += A['last_t'] == A['end_t']; bad_len += A['last_t'] != A['end_t']
    s8c = sat_bank_song('BS08C.PAC', 0); seq8c, i8c = sega_to_sony_seq(s8c)
    ours = set((e[0], e[3]) for e in sony_seq_parse(seq8c)['ev'] if e[1] == 'on')
    cap = [(e[0] + 10, e[2], e[3]) for e in sony_seq_parse(ps1_pbgm(0x2A)[1])['ev'] if e[1] == 'on']
    kept = [x for x in cap if x[1] in (0, 1, 2, 4)]
    check('Sega SEQ: step sum = tempo-track length on looped songs 0 (WOOD2, all BSxxB/C ...); BS08C -> Sony SEQ: every note of Capcom\'s PBGM2A channels 0/1/2/4 at the same key, 10 steps earlier',
          ok_len >= 30 and i8c['loop_t'] == 797 and i8c['end_t'] == 3486 and all((tt, k) in ours for tt, c, k in kept) and len(kept) == 790,
          f'{ok_len} songs match, {bad_len} differ (extension codes / unusual loops); {len(kept)} Capcom notes checked')
    p47, i47 = build_music_bank('WOOD2.PAC'); r47 = music_bank_report(p47, 'WOOD2.PAC')
    check('PBGM47 (WOOD2 converted): SEQ = Saturn song 0 note for note (12 channels, loop 29-6174); 26 tones (14 music, 12 sounds) at the Saturn pitch (< 1 cent); muted notes 8/9/12 (IDs 0x5A/0x5B/0x5E) left out; 12 IDs bank 0xC8',
          r47['seq_notes_equal'] and r47['notes'] == 1723 and r47['loops'] == [(29, 20), (6174, 30)] and r47['layers'] == 26
          and r47['max_cents'] < 1 and sorted(i47['muted']) == [(0, 8), (0, 9), (0, 12)]
          and sorted(r47['ids']) == [0x52, 0x53, 0x54, 0x55, 0x56, 0x57, 0x58, 0x59, 0x5C, 0x5D, 0x5F, 0x141]
          and all(e[0] == 0xC8 and e[1] == 0x10 for e in r47['ids'].values()), f"{r47['size']} bytes, max {r47['max_cents']:.2f} cents")
    # ---- Cut Man's banks (PBGM48 / PBGM49), SPU pitch cap
    pac48, i48 = build_boss_intro_bank('CUT1.PAC')
    c48 = {t: b for t, o, b in unpac_bytes(pac48)}; v48 = vab_split(c48[4], c48[0x201])
    rate48 = lambda t, up: 44100 * 2 ** ((t[6] - t[4] + t[5] / 128) / 12) / up
    check('PBGM48 (CUT1 on PBGM29): muted notes 0/1 (sounds 0x60/0x61) left out; IDs 0x62/0x63/0x13E bank 0xC9; 3 tones at the Saturn pitch incl. fine tune 0x7E (notes 2/3); SEQ + music VAGs = PBGM29',
          [k // 4 for k in range(0, len(c48[5]), 4) if any(c48[5][k:k + 4])] == [0x62, 0x63, 0x13E] and c48[1] == ps1_pbgm(0x29)[1]
          and all(c48[5][4 * i] == 0xC9 for i in (0x62, 0x63, 0x13E)) and v48['vags'][:2] == v29['vags'][:2]
          and i48['muted'] == [(0, 38915), (1, 31763)] and i48['reused'] == [(4, 17791)] and c48[4][0x20 + 16 * 16] == 3
          and all(abs(rate48(v48['tones'][16][t], up) / (44100 * 2 ** ((k - b + f / 256) / 12)) - 1) < 1e-3
                  for t, k, b, f, up in ((0, 2, 8, 126, 1), (1, 3, 9, 126, 1), (2, 4, 28, 0, 2))), f"{len(pac48)} bytes")
    p49, i49 = build_music_bank('CUT2.PAC'); r49 = music_bank_report(p49, 'CUT2.PAC')
    check('PBGM49 (CUT2 converted): SEQ = Saturn song 0 note for note (14 channels, loop 221-4830); 22 tones (14 music, 8 sounds) at the Saturn pitch (< 1 cent); muted notes 0/3/4 (sounds 0x64/0x67/0x68) left out; 8 IDs bank 0xCA',
          r49['seq_notes_equal'] and r49['notes'] == 1338 and r49['loops'] == [(221, 20), (4830, 30)] and r49['layers'] == 22
          and r49['max_cents'] < 1 and sorted(i49['muted']) == [(0, 0), (0, 3), (0, 4)]
          and sorted(r49['ids']) == [0x65, 0x66, 0x69, 0x6A, 0x6B, 0x6C, 0x6D, 0x141]
          and all(e[0] == 0xCA and e[1] == 0x10 for e in r49['ids'].values()), f"{r49['size']} bytes, max {r49['max_cents']:.2f} cents")
    check('SPU pitch cap (4x 44.1 kHz; measured in Mednafen: CUT2 voices 3/4 at 4.5x sounded -200 cents): no song note of PBGM47/PBGM49 asks for more (voices stored at half rate where needed)',
          r47['max_pitch'] < 4 and r49['max_pitch'] < 4, f"max {r47['max_pitch']:.2f}x / {r49['max_pitch']:.2f}x")
    # ---- emulator calibration tooling (the measurements themselves need Mednafen + BIOS: `calibrate`)
    cs, csch = calibration_song('WOOD2.PAC'); cd = sega_seq_decode(cs)
    ch47 = {t: b for t, o, b in unpac_bytes(p47)}; sq = sony_seq_parse(ch47[1])
    v47 = vab_split(ch47[4], ch47[0x201])
    check('Calibration tooling: Sega SEQ writer round-trips the calibration song (232 notes, every bank select followed by a program change); PBGM47 uses the measured tuning (libsnd velocity/CC 7 curves, Saturn pan sides, no Dr-0 envelope step, upsampled drums/leads/one-shots)',
          len(csch) == 232 and all(any(f[1] == 'prog' and f[0] == e[0] and f[2] == e[2] for f in cd['ev']) for e in cd['ev'] if e[1] == 'cc' and e[3] == 0x20)
          and sorted({e[4] for e in sq['ev'] if e[1] == 'on' and e[2] == 3}) == [_libsnd_level(_interp_db(SAT_VEL_DB, v)) for v in (110, 115)]
          and [e[4] for e in sq['ev'] if e[1] == 'cc' and e[3] == 7][:5] == [127, 127, _libsnd_level(-0.735 * 7), _libsnd_level(-0.735 * 12), _libsnd_level(-0.735 * 19)]
          and scsp_pan(0x02) > 64 > scsp_pan(0x11)
          and all(t[16] != 0x0F for p in range(1, 17) for t in v47['tones'][p][:v47['progs'][p][0]]), f"{len(p47)} bytes")
    check('Global map (globals_map.txt) reproduces independently verified variables',
          bool(gmv) and gmv.get(0x0602DA86) == 0x801C336E and gmv.get(0x0602DAC1) == 0x801B29A5 and gmv.get(0x0602FF06) == 0x801D292A
          and gmv.get(0x0602DAB1) == 0x801B2995, '' if gmv else 'globals_map.txt missing next to mm8tool.py')
    # ---- track-1 extension (extend_track1)
    exe_w = [ps.exe.u32(a) for a in range(ps.exe.b, ps.exe.e - 3, 4)]
    e138 = struct.unpack('<3I', ps.exe.d[PS1_FILETAB + 12 * PS1_END1_FILE - ps.exe.b:][:12])
    check('END1.DA (CD audio) is found only through file-table entry 138: two `ori a0,zero,0x8A` + jal 0x800FC238 at 0x800FC78C/0x800FC7A8, LBA -> CdIntToPos 0x800CF31C; entry 138 = (142246, size, 0)',
          sum(fp(x) == '7cd58bac9f26' for x in exe_w) == 2 and fp(w(0x800FC78C)) == '7cd58bac9f26' and fp(w(0x800FC790)) == '54b3922b605c' and fp(w(0x800FC7A8)) == '7cd58bac9f26'
          and fp(w(0x800FC7D0)) == '54b3922b605c' and fp(w(0x800FC7DC)) == 'c6fdc10ad3ba' and e138[0] == 142246 and e138[2] == 0
          and not any(PS1_FILETAB + 12 * PS1_END1_FILE <= t < PS1_FILETAB + 12 * 139 for n_, img_ in ps1_images(ps) for _, t, _ in ps1_absrefs(ps, img_)))
    # ---- M4 Cut Man (STAGE09)
    s09, p09 = pac_chunks('sat', 'STAGE09'), pac_chunks('ps1', 'STAGE09')
    r09, i09 = restore_stage_map('STAGE09', M4_GRID_CELLS, M4_NEW_SCREENS, M4_REPLACED_SCREENS, M4_TILE_FIRST)
    def _cell9(ch, scr, sat_side):
        e = struct.unpack(('>' if sat_side else '<') + '256H', ch[3][scr * 512:scr * 512 + 512]); out = []
        for v in e:
            b = v & 0xFFF; blk = ch[4][4 * b:4 * b + 4]
            if sat_side: out.append((v >> 12, sat_tile(ch[0x103], struct.unpack('>H', blk[:2])[0] >> 2), block_sat_to_ps1(blk)[2:]))
            else: out.append((v >> 12, ps1_tile(ch[0x103], struct.unpack('<H', blk[:2])[0]), blk[2:]))
        return out
    on9 = sorted({v for v in r09[0] if v})
    cut_scr = set(M4_NEW_SCREENS) | set(M4_REPLACED_SCREENS)
    check('M4 map: restored STAGE09 grids = Saturn; hidden room / arena screens 0x15, 0x18-0x1C decode to the Saturn tiles + block bytes 2/3 (flags 0x1000 kept); every other grid screen = the PS1 original (PS1 tile edits in 0x08/0x0D kept); 29 screens, 1391 blocks, 136 new tiles 0x4A6-0x52D',
          all(r09[g] == s09[g] for g in (0, 1, 2)) and all(_cell9(r09, k, False) == _cell9(s09, k, True) for k in cut_scr)
          and all(_cell9(r09, k, False) == _cell9(p09, k, False) for k in on9 if k not in cut_scr)
          and (i09['screens'], i09['blocks'], i09['new_tiles'], i09['tile_range']) == (29, 1391, 136, (0x4A6, 0x52D)))
    sp9, si9 = restore_stage_sprites('STAGE09')
    check('M4 sprites: STAGE09 sets 0xA9-0xAA (Cut Man = 0xAA: chunk-7 base 0xD00, palette 0x30), 0x102 tiles 0x500-0x67F (380, blank on PS1), palettes 0x30-0x32 (zeroed) and 0x5B/0x61 (filler ramp on PS1, used only by screens 0x15/0x19) restored',
          si9['sprite_tiles'] == 380 and si9['tile_span'] == (0x500, 0x67F) and si9['palettes'] == [0x30, 0x31, 0x32, 0x5B, 0x61]
          and struct.unpack('<H', sp9[7][2 * 0xAA:2 * 0xAA + 2])[0] == 0xD00 and sp9[8][0xAA] == 0x30 and ex.u32(0x801393B4 + 4 * 0x62) & 0xFF == 0xAA)
    r0a9 = m4_placements(p09[0xA])
    recs9 = [tuple(struct.unpack('>2H', r0a9[i:i + 4]) + struct.unpack('<2H', r0a9[i + 4:i + 8])) for i in range(0, placement_terminator(r0a9), 8)]
    check('M4 placements: STAGE09 bolt 38 floor record removed; Cut Man + 3 P5 0x34 + P5 0x11 v0x0B = the Saturn-only records (73 records)',
          len(recs9) == 73 and rec_tuple(m4_bolt38()) not in recs9
          and all(struct.unpack('>2H', r[:4]) + struct.unpack('<2H', r[4:]) in placements('sat', 'STAGE09') for r in m4_records()))
    m5c = {st: (pac_chunks('ps1', st)[0xA], m5_placements(pac_chunks('ps1', st)[0xA])) for st in M5_STAGES}
    m5r = lambda c: [c[i:i + 8] for i in range(0, placement_terminator(c), 8)]
    check('M5 placements: bolt 19 floor record (P3 0x25 v19) removed from STAGE05 and STAGE05B chunk 0x0A; the other 193 records keep their order, terminator moved up, size unchanged; bolt 19 then has no placement on either system (Wood Man drops it)',
          all(len(n) == len(o) and m5r(n) == [r for r in m5r(o) if r != m5_bolt19()] and len(m5r(n)) == 193
              and n[placement_terminator(n):placement_terminator(n) + 8] == o[placement_terminator(o):placement_terminator(o) + 8]
              and set(n[placement_terminator(n) + 8:]) == {0} for o, n in m5c.values())
          and not any(t == 0x25 and f == 0x1302 for st in ('STAGE05', 'STAGE05B') for side in ('sat', 'ps1')
                      for t, f, x, y in (placements('sat', st) if side == 'sat' else [tuple(struct.unpack('>2H', r[:4]) + struct.unpack('<2H', r[4:])) for r in m5r(m5c[st][1])])))
    sc9 = ps1_palette_scripts(9)
    anim9 = sorted({e[0] for ch in sc9[0] for e in ch})
    sat_scr = sat.p2[0x29A0A8 - sat.BASE2:0x29A0A8 - sat.BASE2 + 4 * (len(sc9[0][0]) + 1 + len(sc9[0][1]) + 1)]
    ps_scr = bytes(b for ch in sc9[0][:2] for e in ch + [(0xFF, len(ch), 0, 0)] for b in e)
    _cb = lambda side: [c & 0x7FFF for c in struct.unpack(('>' if side == 'sat' else '<') + '256H', pac_chunks(side, 'STAGE09')[0xB])]
    _c9 = lambda side: [c & 0x7FFF for c in struct.unpack(('>' if side == 'sat' else '<') + '2048H', pac_chunks(side, 'STAGE09')[9])]
    c9s, c9p = _c9('sat'), _c9('ps1')
    check('M3 STAGE09 colours: one animation section (palettes 0x4E, 0x54 from chunk 0xB; scripts identical to Saturn 0x29A0A8, chunk 0xB identical); background palettes differing Saturn/PS1 = 0x48, 0x4D, 0x54 (= anim frame 0x0A), 0x5A',
          list(sc9) == [0] and anim9 == [0x4E, 0x54] and sat_scr == ps_scr and _cb('sat') == _cb('ps1')
          and sorted({i // 16 for i in range(0x400, 0x800) if c9s[i] != c9p[i]} - {0x5B, 0x61}) == [0x48, 0x4D, 0x54, 0x5A]
          and c9p[0x54 * 16:0x55 * 16] == _cb('ps1')[0xA * 16:0xB * 16])
    sat_p5 = SAT_MASTER + 4 * 247
    p5img = ps.ovl('STAGE09')
    check('P5 type 0x34 (Saturn 0x2083E0, red alarm flash + sound 0x18F) is the 53rd Saturn P5 entry, missing from the PS1 EXE table (52); STAGE09 dispatches P5 through lui/addiu 0x801D8340; PBGM32 (stage 9 music) keeps ID 0x18F',
          sat.u32(sat_p5 + 4 * 0x34) == 0x2083E0 and Ps1.lui_addiu(p5img, PS1_P5_LUI['STAGE09']) == PS1_P5_TABLE
          and any(fp(unpac_bytes(open(os.path.join(PS1_FILES, 'SOUND', 'PBGM32.PAC'), 'rb').read())[k][2][0x63C:0x640]) == 'a8ccec3c4171' for k in range(len(unpac_bytes(open(os.path.join(PS1_FILES, 'SOUND', 'PBGM32.PAC'), 'rb').read())))))
    tsat = struct.unpack('>24h', bytes(sat.p2[0x27C1E8 - 0x200000:0x27C1E8 - 0x200000 + 48]))
    tps = struct.unpack('<24h', ex.d[0x8015233C - ex.b:0x8015233C - ex.b + 48])
    check('P5 0x11 (camera trigger, PS1 0x80132404) bounds table: Saturn 0x27C1E8 = PS1 0x8015233C; variant 0x0B -> entries 0x7B (hidden room, camera (0x600, 0x400)) / 0x4A',
          tsat == tps and tps[22:24] == (0x7B, 0x4A) and fp(ex.u32(0x80132654)) == 'cb4204245eb3')
    check('Fade block: Saturn 0x0602DAF0 (+2 busy, +6/+8/+0xA) = PS1 0x8015D224 (fade task 0x0600E298 / 0x80109CB8); Cut Man writes +3..+5 like PS1 0x80100DB8 (0x8015D227-9)',
          sat.u32(0x0600E31C) == 0x0602DAF0 and fp([sat.u16(0x0600E2B2), sat.u16(0x0600E2BA), sat.u16(0x0600E2BC), sat.u16(0x0600E2BE)]) == 'f41b31091afa'
          and Ps1.lui_addiu(ex, 0x80109CD0) == 0x8015D224 and fp(ex.u32(0x80109CE0)) == 'c54198b8777d'
          and fp([ex.u32(a) & 0xFFFF for a in (0x80109CEC, 0x80109CF4, 0x80109CFC)]) == 'ed966ab656d2'
          and fp([ex.u32(a) & 0xFFFF for a in (0x80100DB8, 0x80100DC0, 0x80100DC8)]) == '0c8cd6c6235a')
    # brightness: the custom masked helper follows the PS1's own level law (unicorn: 0x8010BBE4 vs compiled h_06010b50)
    try:
        from unicorn import Uc, UC_ARCH_MIPS, UC_MODE_MIPS32, UC_MODE_LITTLE_ENDIAN
        from unicorn.mips_const import UC_MIPS_REG_A0, UC_MIPS_REG_A1, UC_MIPS_REG_RA, UC_MIPS_REG_SP
        import tempfile
        wd = tempfile.mkdtemp(prefix='mm8br_'); src = os.path.join(wd, 'b.c')
        open(src, 'w').write('typedef signed char s8; typedef unsigned char u8; typedef unsigned int u32;\n' + CUT_BRIGHT_C)
        bd, bs = compile_c([src], 0x801E0000, {}, workdir=wd)
        exe_raw = open(os.path.join(PS1_FILES, 'SLUS_004.53'), 'rb').read()[0x800:]
        okb = fp(ex.u32(0x8010BC40)) == '1442c9712ba0' and ex.u32(0x8010BC44) == 0
        for lv in range(-8, 9):
            got = []
            for fn, args in ((0x8010BBE4, (lv & 0xFF,)), (bs['h_06010b50'], (lv & 0xFF, 7)), (bs['h_06010b50'], (lv & 0xFF, 1))):
                uc = Uc(UC_ARCH_MIPS, UC_MODE_MIPS32 | UC_MODE_LITTLE_ENDIAN); uc.mem_map(0, 0x200000)
                uc.mem_write(0xC0000, exe_raw[:0x113000]); uc.mem_write(0x1E0000, bd)
                uc.mem_write(0x1000, struct.pack('<2I', 0x0000000D, 0))
                for r, v in zip((UC_MIPS_REG_A0, UC_MIPS_REG_A1), args): uc.reg_write(r, v)
                uc.reg_write(UC_MIPS_REG_RA, 0x80001000); uc.reg_write(UC_MIPS_REG_SP, 0x801FF000)
                uc.emu_start(fn, 0x80001000, count=10000)
                got.append(bytes(uc.mem_read(0x1B299B, 3)))
            okb = okb and got[0] == got[1] and got[2] == got[0][:1] + b'\x80\x80'
    except ImportError: okb = False
    check('Masked brightness (Saturn 0x06010B50, emptied on PS1 at 0x8010BC40): the port helper gives the PS1 all-channel law (0x8010BBE4) on the masked channels, levels -8..8 (unicorn)', okb)
    csrc9, cport = boss_lift(CUT_SPEC)
    sc9 = Code([(sat.BASE2, sat.p2), (sat.BASE1, sat.p1)])
    n9 = [len(discover(sc9, [e], {k: CUT_CODE_TABLES[k] for k in tk}, None)[0]) for _, e, _, tk, _ in CUT_SPEC['handlers']]
    check('SH-2 lifter: Cut Man (1825 instructions, variants 0-2, 7 jump tables) and P5 0x34 (126) lift to C; all 36 engine calls mapped (+ the masked-brightness helper)',
          n9 == [1825, 126] and len(cport.used_helpers) == 36 and cport.used_custom == {0x06010B50} and 'UNTRANSLATED' not in csrc9)
    # ---- pre-release checks
    exe = open(os.path.join(PS1_FILES, 'SLUS_004.53'), 'rb').read()
    ew = lambda a: struct.unpack_from('<I', exe, exe_file_off(a))[0]
    jal_to = lambda t: [0x800C0000 + i - 0x800 for i in range(0x800, len(exe) - 3, 4) if struct.unpack_from('<I', exe, i)[0] == 0x0C000000 | ((t >> 2) & 0x3FFFFFF)]
    check('Ending song: 0x800FC69C (CdlPlay of entry 138) is called only at 0x80121F2C with a0 = 0x7F (debug hook ending_song_image)',
          jal_to(PS1_ENDING_PLAYER) == [0x80121F2C] and fp(ew(0x80121F30)) == '8fa6d03d44ba' and fp(ew(0x800FC78C)) == '7cd58bac9f26')
    check('Thread stacks: thread k starts at SP 0x801FEC00 + 0x400k (0x800F82D8, OpenThread 0x800F7D58 sp = slot +0xC); game loop 0x801008C8 (frame 0x18) runs mode 7 = stage frame 0x80100E00 (table 0x80137B04)',
          fp([ew(0x800F82D8), ew(0x800F82DC), ew(0x800F82F0), ew(0x800F7D50), ew(0x80137B04 + 4 * 7), ew(0x801008C8)]) == '8032630b5093')
    fr, sh = lifted_stack_usage()
    check('Lifted code stack: C frames <= 272 bytes; SH-2 stack (sum of every entry\'s deepest r15 offset) fits u32 stk[48] (192 bytes); thread 0 has 10,944 bytes below 0x801FEC00',
          max(fr.values()) <= 272 and all(v['sum_bound'] <= 192 and v['unknown_r15'] == 0 for v in sh.values()),
          ', '.join(f'{k} {v}' for k, v in fr.items()) + '; SH-2 ' + ', '.join(f'{k} {v["sum_bound"]}' for k, v in sh.items()))
    # ---- Saturn Bonus Mode survey
    st, vt = sat_bonus_tables()
    pbgm = set(os.listdir(os.path.join(PS1_FILES, 'SOUND')))
    check('Bonus sound test (Saturn 0x27CDAC): 32 music numbers, Title = 0x10 ... Cutman 0x49, Woodman 0x47; every n < 0x46 has a PS1 PBGM file, 0x47/0x49 = our PBGM47/49',
          len(st) == 32 and st[0] == 0x10 and st[-2:] == [0x49, 0x47] and all(f'PBGM{n:02X}.PAC' in pbgm for n in st if n < 0x46) and {n for n in st if n >= 0x46} == {0x47, 0x49})
    def _ps1_id_ok(m, sid):
        t = pac_chunks('ps1', os.path.join('..', 'SOUND', f'PBGM{m:02X}'))[5]
        return t[4 * sid:4 * sid + 1] == bytes([0x80 | (m + 1)])
    check('Bonus voice test (Saturn 0x27CDCC): 100 (bank, sound) pairs on the US disc (2 COMMON); each of the other 98 has a PS1 sound-ID entry of that bank in PBGM(bank) (index = sound number)',
          len(vt) == 100 and sum(m == 0xFF for m, s in vt) == 2 and all(_ps1_id_ok(m, s) for m, s in vt if m != 0xFF)
          and sat.u16(SAT_BONUS_VOICE_TEST + 400) == 0x48)
    ills = [unpac(os.path.join(SAT_FILES, 'BONUS', f'ILL{n:02d}.PAC')) for n in range(SAT_BONUS_ILL)]
    okill = all([(t, len(b)) for t, o, b in c] == [(9, 0x800), (0x102, 0x10000)] for c in ills)
    okill = okill and all(not any(struct.unpack('>256H', c[0][2][:0x200])) and any(struct.unpack('>256H', c[0][2][0x400:0x600])) for c in ills)
    check('Bonus illustrations: BONUS/ILL00-29.PAC = chunk 9 (1024 colours, picture in bank 2) + chunk 0x102 (256 x 256 8-bpp, 16x16 characters); names table 0x29B308',
          okill and sat.u32(0x29B300) == 0x27BEB8 and sat.u32(0x29B304) == 0x27BEC4 and fp(sat.raw(0x29B308 + 13 * 29, 9)) == '9fdd1fab9128')
    let = unpac(os.path.join(SAT_FILES, 'LETTER.PAC'))
    check('Bonus postcards: LETTER.PAC = chunk 0x11 (30 palettes x 256 colours) + chunk 0x100 (0x50000 bytes of 16x16 8-bpp characters)',
          [(t, len(b)) for t, o, b in let] == [(0x11, 30 * 0x200), (0x100, 0x50000)])
    # ---- M7a title menu (BONUS MODE as the 4th item)
    E = ps.exe
    t_sub = [E.u32(0x80137930 + 4 * i) for i in range(7)]
    t_code = all(fp(E.u32(a)) == ofp for a, ofp, new in M7_TITLE_CODE)
    p_recs = ps1_title_records(E, 12, 7); s_recs = sat_title_records(12, 7)
    p_cur = struct.unpack('<8h', E.d[PS1_TITLE_CURPOS - E.b:][:16]); s_cur = struct.unpack('>8h', sat.raw(SAT_TITLE_CURPOS, 16))
    check('PS1 title (0x800FEA40 loop, sub-states 0x80137930 = setup 0x800FEBEC / menu 0x800FEF94 / options-continue 0x800FF148): the M7a patch sites hold the expected code (confirm, wraps, item spawn loop, continue call)',
          t_sub == [0x800FEBEC, 0x800FED58, 0x800FEF94, 0x800FF0F4, 0x800FF148, 0x800FF238, 0x800FF47C] and t_code
          and E.u32(0x8015029C + 4 * 0x0B) == 0x801348E4 and fp(list(E.d[PS1_TITLE_GROUPS - E.b:][:8])) == 'cb118e431aec')
    check('Title objects (P4 0x0B; PS1 records 0x801527C4, Saturn 0x295714): Saturn has 4 items at y 127/143/159/175 (record 18 = set 0x70 step 11 = BONUS MODE), cursor table 4 entries; PS1 3 items at y 133/151/169, records 12/14-17 differ only in y; PS1 record 18 = the JP GAME INFORMATION item (step 4)',
          fp(s_cur) == '136a53c4c128' and fp(p_cur) == '510b41516904'
          and fp([r[5] for r in s_recs[2:7]]) == '9a00d9c613db' and fp(s_recs[6]) == 'c4531270aaf1'
          and fp([r[5] for r in p_recs[3:6]]) == '53f93beed8ed' and fp(p_recs[6]) == '36526a686ee2'
          and all(p[:5] == s[:5] for p, s in zip(p_recs[2:6], s_recs[2:6])) and p_recs[0] == s_recs[0])
    lab_s = {t: b for t, o, b in unpac(os.path.join(SAT_FILES, 'LABO.PAC'))}
    lab_p = {t: b for t, o, b in unpac(os.path.join(PS1_FILES, 'STDATA', 'LABO.PAC'))}
    off70 = struct.unpack('<I', lab_p[6][0x70 * 4:0x70 * 4 + 4])[0]
    f20 = struct.unpack('<HH', lab_p[5][off70 + 4 * 20:off70 + 4 * 20 + 4])
    st0 = E.u32(E.u32(0x8014FF74 + 4 * 0x70))
    check('LABO.PAC: sprite chunks 5/6/8 = the Saturn\'s (byte-swapped) and 0x102 tiles identical, so set 0x70 already holds BONUS MODE (anim 0 step 11 = frame 20, 5 pieces) next to GAME START/CONTINUE/OPTION MODE (steps 1-3)',
          swap32(lab_s[5]) == lab_p[5] and swap32(lab_s[6]) == lab_p[6] and lab_s[8] == lab_p[8] and f20[0] == 5
          and gfx_sat_to_ps1(lab_s[0x102], False) == lab_p[0x102] and struct.unpack('<H', E.d[st0 + 4 * 11 - E.b:][:2])[0] == 20)
    used_t = [hex(t) for n_, img_ in (('EXE', E), ('DEMO', ps.ovl('DEMO'))) for _, t, _ in ps1_absrefs(ps, img_) if 0x801DC1C8 <= t < 0x801FC000]
    check('No fixed EXE/DEMO refs in 0x801DC1C8-0x801FBFFF (RAM above the title overlay; the bonus overlay loads at M7_BONUS_BASE)', not used_t, str(used_t[:8]))
    d7, b7, i7 = build_m7_overlays(open(os.path.join(PS1_FILES, 'OVL', 'DEMO.BIN'), 'rb').read())
    check('M7a: DEMO.BIN + title hook fits its 9-sector slot, bonus overlay at M7_BONUS_BASE, both R3000-clean; virtual entry inside DEMO.BIN',
          len(d7) <= PS1_DEMO_SLOT * 2048 and i7['title_hook'] > Ps1.OVL_BASE + 0x41C8 and M7B_BONUS_MAGIC in b7
          and Ps1.OVL_BASE <= PS1_FILETAB + 12 * i7['vf'] < Ps1.OVL_BASE + len(d7), f"DEMO {len(d7):#x}, bonus {len(b7):#x}")
    # ---- M7b: bonus menu lifted from the Saturn
    fm = dict(load_helper_map()); fm.update(M7B_FRONTEND_HELPERS); fm[M7B_P4_LOOP[0]] = M7B_P4_LOOP[1]; fm[M7B_WAIT[0]] = M7B_WAIT[1]
    sc = [t for k, a, t in sat_events(sat, 0x279978, 0x2799C6) if k == 'call' and t]
    pc = ps.calls(ps.exe, 0x8011F0A8, 0x8011F184)
    check('M7b: Saturn continue loop 0x279978 and PS1 0x8011F0A8 call the same engine functions in order (front-end helper rows)',
          [fm.get(t) for t in sc] == pc[:len(sc)] and len(sc) == 5, ' '.join(f'{t:08x}' for t in sc))
    sc2 = [t for k, a, t in sat_events(sat, 0x2799D0, 0x279A38) if k == 'call' and t]
    check('M7b: continue init 0x2799D0 / 0x8011F184: clear objects, 2 layer inits, fade (0x060147D8, 0x00210DFA x2, 0x06012E70)',
          [fm.get(t) for t in sc2 if t != 0x0601470C] == ps.calls(ps.exe, 0x8011F184, 0x8011F25C)[:4], ' '.join(f'{t:08x}' for t in sc2))
    E = ps.exe
    t_ok = (fp(E.u32(0x800FEC60)) == 'f103cd140115' and fp(E.u32(0x800FEC64)) == '9a0090d29231' and sat.u32(0x2096D4) == 0x0600C0A8
            and fp(E.u32(0x800FEC7C)) == '6f0ad319c650' and sat.u32(0x2096E4) == 0x06014D64 and sat.u32(0x2096DC) == 0x002A26A0
            and sat.u32(0x2096E0) == 0x06036438 and sat.u32(0x2096E8) == 0x0029E5A0 and sat.u32(0x2096EC) == 0x0602EACC
            and fp(E.u32(0x800FECA8)) == '963ba252afdf' and sat.u32(0x2096F4) == 0x060147D8 and sat.u32(0x2096F0) == 0x0602FEEC)
    check('M7b: title setup 0x2095xx / 0x800FEBEC: music 0x10 (0x0600C0A8 = 0x800FCA4C), screens/palettes copied by 0x06014D64 = 0x801061DC, CRAM dirty 0x0602FEEC = 0x8016CA90, 0x060147D8 = 0x801058F0', t_ok)
    S = {t: b for t, o, b in unpac(os.path.join(SAT_FILES, 'LABO.PAC'))}
    P = {t: b for t, o, b in unpac(os.path.join(PS1_FILES, 'STDATA', 'LABO.PAC'))}
    exact = all(front_pac_sat_to_ps1(t, S[t]) == P[t] for t in (0, 1, 2, 3, 4, 5, 6, 7, 8, 0x12, 0x100, 0x102, 0x103))
    g = front_pac_sat_to_ps1(0x101, S[0x101]); same = sum(ps1_tile(g, k) == ps1_tile(P[0x101], k) for k in range(768))
    c9 = front_pac_sat_to_ps1(9, S[9]); colours = sum((a & 0x7FFF) == (b & 0x7FFF) for a, b in zip(struct.unpack('<2048H', c9), struct.unpack('<2048H', P[9])))
    check('M7b: front-end PAC conversion (SLABO.PAC) reproduces PS1 LABO.PAC from the Saturn one: chunks 0-8, 0x12, 0x100, 0x102, 0x103 exact, 0x101 745/768 tiles',
          exact and same == 745, f'0x101 {same}/768, chunk 9 colours {colours}/2048')
    sl = {t: b for t, o, b in unpac(os.path.join(SAT_FILES, 'SLABO.PAC'))}
    fo = struct.unpack_from('>I', sl[6], 4 * M7B_PROMPT_SET)[0]
    cnt = struct.unpack('<HH', swap32(sl[5])[fo + 4 * M7B_PROMPT_FRAME:fo + 4 * M7B_PROMPT_FRAME + 4])[0]
    check('M7b: SLABO set 0xA9 frame 0x11 = the 21-glyph Saturn prompt; tile 767 of 0x101 blank (PS1 circled-x glyph goes there)',
          cnt == 21 and not any(sl[0x101][128 * M7B_XGLYPH_TILE:128 * (M7B_XGLYPH_TILE + 1)]) and len(m7b_prompt_pieces()) == 19)
    csrc, port7 = m7b_lift()
    check('M7b: lifted bonus menu (0x27AAD0 + states 0x29B2D0, spawner 0x2761DC, P4 0x40/0x41) has no untranslated address and calls music 0x14 (0x27AC1C)',
          'UNTRANSLATED' not in csrc and sat.u32(0x27ACE0) == 0x0600C0A8 and fp(sat.u16(0x27AC20)) == '1d7318f45fb2'
          and sorted(port7.used_custom) == sorted(M7B_CUSTOM), f'{len(csrc.splitlines())} lines, {len(port7.used_helpers)} engine helpers')
    E = ps.exe
    mcalls = {}
    for a, _, mn, ops in ps.md.disasm_lite(E.d, E.b):
        if mn == 'jal' and ops == hex(M7B_PS1_MOVIE): mcalls[a] = E.u32(a + 4)
    tab = [tuple(E.d[0x801378F4 - E.b + 4 * k:0x801378F4 - E.b + 4 * k + 2]) for k in range(5)]
    check('M7g: PS1 movie player 0x800FD444(entry) plays file-table entries 3-7 (boot 3 = opening, game 4-7) = MOVIE/ROCK8_0-4.STR (same names as the Saturn CPKs); skip masks at 0x800FD4C0 (Start/x) / 0x800FD4C4 (any), FlushCache wrapper 0x800D78EC',
          sorted(v & 0xFFFF for v in mcalls.values()) == [3, 4, 5, 6, 7] and [t[0] for t in tab] == [3, 4, 5, 6, 7]
          and fp(E.u32(0x800FD4C0)) == '8255f432331b' and fp(E.u32(0x800FD4C4)) == '3b788e449e1a'
          and fp(E.u32(0x800D78EC)) == '45fa6d457134' and fp(E.u32(0x800D78F4)) == '743f1bc64afb'
          and [sat.u32(M7B_MOVIE_NAMES + 4 * k) for k in range(5)] == [0x27BE7C + 12 * k for k in range(5)]
          and all(sat.raw(0x27BE7C + 12 * k, 11) == f'ROCK8_{k}.CPK'.encode() for k in range(5)),
          ' '.join(f'{t[0]}:{t[1]}' for t in tab))
    check('M7g: Saturn title (0x20989A) sets 0x0602DABC when BONUS MODE (cursor 3) is confirmed with Start + L + R held (& 0x888); PS1 confirm-sound call 0x800FEFD4 is the hook site',
          fp(sat.u16(0x209920)) == '8e4e975f9024' and sat.u32(0x209948) == 0x0602DA98 and fp(E.u32(0x800FEFD4)) == 'f2cd1272b0f6' and fp(E.u32(0x800FEFD8)) == 'f1bff64ab7c7')
    tsl = i7['title_skip_load'] - Ps1.OVL_BASE
    check('Bonus exit: the Saturn reruns its title setup after the bonus loop (0x2099CC: sub-state 0x0602DA5B = 0); PS1 sub-state 0 loads DEMO.BIN with jal 0x800FC648 (a0 = 0) at 0x800FEC18, which title_skip_load restores; loader bookkeeping 0x801C738C / 0x801C73D0',
          sat.u32(0x209A20) == 0x0602DA5B and fp(sat.u16(0x2099D0)) == 'e095c84c6e05' and fp(sat.u16(0x2099D2)) == 'de3daca6a937'
          and fp(E.u32(0x800FEC18)) == '846a2d156d02' and fp(E.u32(0x800FEC1C)) == '5041936c9c92'
          and fp(E.u32(0x800FC674)) == '9ef0af4c7f3f' and fp(E.u32(0x800FC678)) == 'f9c7aba2bdec' and fp(E.u32(0x800FC680)) == 'b20ac1d63674'
          and fp(struct.unpack('<2I', d7[tsl + 36:tsl + 44])) == 'cc7223e14825', f"title_skip_load {i7['title_skip_load']:#x}")
    # M7c: OFFICIAL ILLUSTRATIONS
    sl = {t: b for t, o, b in unpac(os.path.join(SAT_FILES, 'SLABO.PAC'))}
    fo = struct.unpack_from('>I', sl[6], 4 * 0xA3)[0]; off, cnt = struct.unpack_from('>HH', sl[5], fo)
    pcs = [struct.unpack_from('>I', sl[5], fo + off + 4 * k)[0] for k in range(cnt)]
    grid = all((v & 0x3FF) == 16 * (k // 13) + k % 13 and ((v >> 16) & 0xFF) == (16 * (k % 13) - 104) & 0xFF
               and (v >> 24) == (16 * (k // 13) - 104) & 0xFF for k, v in enumerate(pcs))
    check('M7c: Saturn picture = P4 0x40 (0x27BA94 spawns it at (0xCC, 0x78); set 0xA3, palette chunk8 | 0x80); set 0xA3 frame 0 = 13 x 13 pieces of tiles 16 r + c; base 0x300 -> PS1 0x800 = VRAM (512, 0) = the 0x102 rect',
          sat.u16(0x27BB58) == 0xCC and fp(sat.u16(0x27BAB4)) == 'c436a49a5695' and sat.u16(0x275E44) == 0xA3 and fp(sat.u16(0x275E1E)) == '396beb8ac2a1'
          and cnt == 169 and grid and struct.unpack_from('>H', sl[7], 2 * 0xA3)[0] == 0x300 and sprite_base_sat_to_ps1(0xA3, 0x300) == 0x800)
    import numpy as np
    f0 = m7c_ill_file(0); pal0, px0 = sat_ill_image(0)
    check('M7c: PS1 ILL file = chunk 9 (picture CLUT = entries 512-767 = CLUT row 482 for palette byte 0xA0; entry 512 transparent, STP on the rest) + 256 x 256 8-bpp texture equal to the Saturn picture; LoadImage = 0x800D3E84 (its debug name string 0x800C0988)',
          np.array_equal(np.frombuffer(f0[2048:], np.uint8).reshape(256, 256), px0)
          and list(struct.unpack('<256H', f0[0x400:0x600]))[1:] == [v | 0x8000 for v in pal0[1:]] and fp(f0[0x400:0x402]) == '6768905d2fd9'
          and fp(E.u32(0x800D3E98)) == 'af060cc6e40b' and fp(E.u32(0x800D3E9C)) == '287cbb677cf5' and fp(E.d[0x800C0988 - E.b:0x800C0991 - E.b]) == 'edcb38fcc7bd'
          and 0x7800 + (0xA0 & 0xF) + ((0xA0 & 0x30) << 2) == 0x7800 + 2 * 64)
    sd = {t: b for t, o, b in unpac_bytes(m7b_slabo_pac())}; s5, s6 = sd[5], sd[6]; fa = struct.unpack_from('<I', s6, 4 * 0xA9)[0]
    def frame_pcs(fr):
        cnt, po = struct.unpack_from('<HH', s5, fa + 4 * fr)
        return [(struct.unpack_from('<H', s5, fa + po + 4 * k)[0] & 0x3FF, struct.unpack_from('<b', s5, fa + po + 4 * k + 2)[0],
                 struct.unpack_from('<b', s5, fa + po + 4 * k + 3)[0]) for k in range(cnt)]
    sp = m7b_spawn_lists(sat)[2]
    check('M7c: illustration screen labels = set 0xA9 frames 0x34-0x37 (spawn list 2), postcards 8-11; "A.C...CHOOSE" / "B...EXIT" become cross / white triangle (tile 766 from 198) ... CHOOSE / EXIT',
          [r[1] for p, r in sp if r[0] == 3 and r[3] == 0xFF][-4:] == [0x34, 0x35, 0x36, 0x37]
          and all(frame_pcs(fr) == M7C_LABELS[fr] for fr in M7C_LABELS)
          and ps1_tile(sd[0x101], M7C_TRI_TILE) == bytes(M7C_TRI_MAP.get(b & 15, b & 15) | M7C_TRI_MAP.get(b >> 4, b >> 4) << 4 for b in ps1_tile(sd[0x101], M7C_TRI_SRC)))
    check('M7c fix: PS1 fade task 0x80109CB8 sets busy 0x8015D226 = 1 while it runs and clears it at the end, and AddPrims its darkening quads to *0x80170334 + 0x94 each frame (so frames outside a running fade are not darkened: h_06010ca8 blacks out finished fade-outs and requested fade-ins)',
          fp(E.u32(0x80109CDC)) == 'cb5ebe7a1afc' and fp(E.u32(0x80109CE0)) == 'c54198b8777d' and fp(E.u32(0x80109D40)) == '4ff156f6256c'
          and fp(E.u32(0x80109DE8)) == 'e975c229489d' and fp(E.u32(0x80109DEC)) == '277cf2ac773a' and fp(E.u32(0x80109E08)) == '998aa69c4136' and fp(E.u32(0x80109E0C)) == '18af1d0cd47e')
    check('M7c read stall: the loader ends a read with CdControlB(CdlPause) at 0x800FC1A4 (jal 0x800CF14C, in the ready callback); CdControl 0x800CEEE8 takes the same arguments (both libcd prologues: com in s4, param s1, result s2)',
          fp(E.u32(0x800FC1A4)) == '1292dd32ff74' and fp(E.u32(0x800FC19C)) == 'b5899e18e756' and fp(E.u32(0x800FC1AC)) == 'dd325f5c9d40'
          and all(fp([E.u32(f + o) for o in (4, 8, 0x10, 0x18)]) == 'f6b4aa2a3020' for f in (0x800CEEE8, 0x800CF14C)))
    # ---- M7d: BOSS CHARACTER POSTCARD
    check('M7d: Saturn postcard screen 0x27B1C0: A/C on NO. shows the number with sound 0x189 / 0x18A (RNG 0x06014166), B or EXIT leaves (sound 0x38, +7 = 1), L/R (0x88) change number and card; box cursor 0x0602DABD, card 0x0602DABE, number 0x0602DABF',
          fp(sat.u16(0x27B27C)) == '99a0b871c904' and fp(sat.u16(0x27B27E)) == '04d19fde0a08' and sat.u32(0x27B29C) == 0x06014166 and sat.u16(0x27B280) == 0x88
          and sat.u32(0x27B298) == 0x0602DA98 and fp(sat.u16(0x27B1F4)) == '205e5aea5fb2' and fp(sat.u16(0x27B226)) == 'cee8a6a9686f')
    check('M7d: Saturn card object (P4 0x41 state 6 = 0x276140): card > 23 -> set 0x98 frame card - 24, else set 0xA2; palette 0x260C0000 + 0x200 card -> CRAM 0x0602EECC (256 colours), dirty flag; Wily comment (state 5 = 0x2760F8) = frame 0x16 + card',
          sat.u32(0x2966AC + 4 * 6) == 0x276140 and fp(sat.u16(0x276142)) == '47f864636214' and sat.u16(0x2761B6) == M7D_CARD_SETS[1] and sat.u16(0x2761B8) == M7D_CARD_SETS[0]
          and sat.u32(0x2761CC) == 0x260C0000 and sat.u32(0x2761D0) == 0x0602EECC and fp(sat.u16(0x2761BA)) == '94f8607915df' and fp(sat.u16(0x2760FE)) == '4860fb4eca2b')
    ex = open(os.path.join(PS1_FILES, 'SLUS_004.53'), 'rb').read()[0x800:]; ew = lambda a: struct.unpack_from('<I', ex, a - 0x800C0000)[0]
    check('M7d: PS1 sprite renderer 8-bit path (palette bit 7, 0x800F92B8): page = (base >> 8) + 2 (tile >> 8) | 0x80, u/v from (base + tile) & 0xFF; DrawSync = 0x800D3C5C (debug name 0x800C0944)',
          fp([ew(0x800F92B8 + 4 * k) for k in (0, 2, 3, 6)]) == 'f1e2c2b5e22c'
          and fp(ew(0x800D3C7C)) == 'af060cc6e40b' and fp(ew(0x800D3C80)) == '77e7a53d9b73' and fp(ex[0x944:0x950]) == '1f1cef18ff86' and fp(ew(0x800D3C64)) == '7e39afd7e325')
    import numpy as np
    lf, lsp = m7d_letter(); vram = np.zeros((512, 2048), np.uint8)       # VRAM as 8-bit texels (2 per halfword)
    for f, off, x, y, w, h in lsp:
        vram[y:y + h, 2 * x:2 * x + 2 * w] = np.frombuffer(lf[f][off:off + 2 * w * h], np.uint8).reshape(h, 2 * w)
    Ls = {t: b for t, o, b in unpac(os.path.join(SAT_FILES, 'LETTER.PAC'))}; ss = unpac_dict_sat_slabo()
    sd7 = {t: b for t, o, b in unpac_bytes(m7b_slabo_pac())}; ok_cards = True
    for card, pcs in enumerate(m7d_card_chars(ss)):
        st_, fr = (M7D_CARD_SETS[0], card) if card < M7D_SPLIT else (M7D_CARD_SETS[1], card - M7D_SPLIT)
        base = struct.unpack_from('<H', sd7[7], 2 * st_)[0]; fo = struct.unpack_from('<I', sd7[6], 4 * st_)[0]
        cnt, po = struct.unpack_from('<HH', sd7[5], fo + 4 * fr)
        for k, (c, x, y) in enumerate(pcs):
            t = struct.unpack_from('<H', sd7[5], fo + po + 4 * k)[0] & 0x3FF; sm_ = base + t
            page = (base >> 8) + 2 * (t >> 8); u, v = (sm_ & 15) * 16, sm_ & 0xF0
            ps1px = vram[256 * (page >> 4) + v:256 * (page >> 4) + v + 16, 128 * (page & 15) + u:128 * (page & 15) + u + 16]
            satpx = np.frombuffer(Ls[0x100][256 * c:256 * c + 256], np.uint8).reshape(16, 16)
            ok_cards &= cnt == len(pcs) and np.array_equal(ps1px, satpx) and tuple(sd7[5][fo + po + 4 * k + 2:fo + po + 4 * k + 4]) == (x & 0xFF, y & 0xFF)
    pals_ok = all(struct.unpack_from('<256H', lf[0], 512 * n) == tuple(0 if k == 0 else v | 0x8000 for k, v in enumerate(struct.unpack_from('>256H', Ls[0x11], 512 * n)))
                  for n in range(M7D_CARDS))
    check('M7d: PS1 LETTER: every piece of all 30 cards (converted sets 0xA2 / 0x98, bases 0x500 / 0xD00) reads the Saturn character through the 8-bit path from the uploaded spans; palettes = LETTER chunk 0x11 (entry 0 transparent, STP on the rest); each file fits the 0x80048000 buffer',
          ok_cards and pals_ok and all(len(f) <= M7D_BUF for f in lf) and m7d_ps1_base(0) == 0x500 and m7d_ps1_base(0x800) == 0xD00,
          f'{len(lf)} files, {sum(len(f) for f in lf)} bytes, {len(lsp)} spans')
    pb30 = {t: b for t, o, b in unpac(os.path.join(PS1_FILES, 'SOUND', 'PBGM30.PAC'))}[5]
    check('M7d: postcard sounds 0x188 (entry), 0x189 / 0x18A (A/C) have entries in PS1 PBGM30 (music 0x30, bank 0xB1)',
          all(pb30[4 * i] == 0xB1 for i in (0x188, 0x189, 0x18A)))
    # ---- M7e: SOUND TEST / VOICE TEST (02 §M7b "M7e")
    check('M7e: Saturn test screen 0x27B0F8 tail-jumps to the player 0x209088 (bounds 31 / 99); the player reads music numbers 0x27CDAC and voice entries 0x27CDCC (bound 0xA6) and calls play_bgm 0x0600C610 / bank sound 0x0600C85E',
          sat.u32(0x27B284) == 0x209088 and fp(sat.u16(0x27B1AC)) == '3ae480feceb0' and fp(sat.u16(0x27B1AE)) == 'b19576ae39d8' and sat.u32(0x209134) == 0x27CDAC
          and sat.u32(0x20913C) == 0x27CDCC and sat.u16(0x20911A) == 0xA6 and sat.u32(0x209138) == 0x0600C610 and sat.u32(0x209140) == 0x0600C85E)
    check('M7e: Saturn play_bgm 0x0600C610 and bank sound 0x0600C85E both return at once while 0x06073356 (busy) is set, load PAC 0x0602B074[n] and keep the bank in 0x060733CE; the load task 0x0600CAD8 clears the flag, then plays 0x060733C8',
          sat.u32(0x600C678) == sat.u32(0x600C8BC) == sat.u32(0x600CB40) == 0x06073356 and sat.u32(0x600C8B4) == sat.u32(0x600CA94) == 0x0602B074
          and sat.u32(0x600C8B0) == sat.u32(0x600CA90) == 0x060733CE and sat.u32(0x600CAA0) == 0x0600CAD8 and sat.u32(0x600CB30) == 0x060733C8)
    check('M7e: PS1 busy flag 0x801CF53C (= Saturn 0x06073356 in the globals map): async play_bgm 0x800FCB80 sets it (0x800FCCA4/A8 lui at,0x801D; sb v0,-0xAC4(at)) before starting task 1, the task 0x800FCCD4 clears it after the load (0x800FCD64/68 sb zero)',
          WM_GLOBALS[0x06073356] == 0x801CF53C
          and fp([ps.exe.u32(a) for a in (0x800FCCA4, 0x800FCCA8, 0x800FCD64, 0x800FCD68)]) == '756b1d721cf7')
    smus = sat.raw(0x27CDAC, 32)
    check('M7e: every sound-test music number has a PS1 bank (PBGM file for n < 0x46, our PBGM47 / PBGM49 for 0x47 / 0x49)',
          all(n < PS1_BGM_FIRST_NEW and os.path.exists(os.path.join(PS1_FILES, 'SOUND', f'PBGM{n:02X}.PAC')) or n in (0x47, 0x49) for n in smus), smus.hex())
    check('M7e: the bonus overlay starts above the music virtual entries 0x801E4658-0x801E4687 (play_bgm hook, one vf0 for every overlay) and carries their copy in its file',
          (M7E_MUSIC_VF - PS1_FILETAB) % 12 == 0 and M7_BONUS_BASE >= M7E_MUSIC_VF + 48 and 0 < i7['music_vf_off'] <= len(b7) - 48,
          f'base {M7_BONUS_BASE:#x}, entries at file offset {i7["music_vf_off"]:#x}')
    # ---- M7f: stack bounds of the bonus code (02 §Stacks)
    bfr, bsh = bonus_stack_usage()
    check('M7f: bonus code SH-2 stacks fit stk[96] (sum of the deepest offsets per handler <= 384 bytes, every r15 known); C frames all static',
          all(v['sum_bound'] <= 384 and v['unknown_r15'] == 0 for v in bsh.values()) and all(k == 'static' for n, k in bfr.values()),
          'max sum %d, largest C frame %d' % (max(v['sum_bound'] for v in bsh.values()), max(n for n, k in bfr.values())))
    import tempfile as _tf
    _wd = _tf.mkdtemp(prefix='mm8m7f_'); _bb, _bi = build_m7b_bonus(_wd)
    _nm = subprocess_run_nm(os.path.join(_wd, 'm7b_bonus', 'out.elf'))
    _exe = open(os.path.join(PS1_FILES, 'SLUS_004.53'), 'rb').read()[0x800:0x800 + 0x113000]
    _p4 = [struct.unpack_from('<I', _exe, 0x8015029C - 0x800C0000 + 4 * k)[0] for k in range(64)] + [_nm['bonus_p4_40'], _nm['bonus_p4_41']]
    _ind = {M7_BONUS_BASE + o: _p4 for o in range(0, len(_bb) - 3, 4) if struct.unpack_from('<I', _bb, o)[0] & 0xFC1FFFFF == 0x0000F809}
    _d, _path, _unres, _rec = static_stack_depth([(0x800C0000, _exe), (M7_BONUS_BASE, _bb)], _nm['bonus_main'], _ind)
    check('M7f: static worst-case stack below bonus_main (EXE calls followed, P4 loop through the 66-entry table) < 4096 of thread 0\'s 10,944 bytes',
          _d < 4096, f'{_d} bytes; {len(_unres)} library jalr sites not followed')
    check('M7b: bonus overlay fits below 0x801F4000 with its SLABO virtual entry', M7_BONUS_BASE + len(b7) <= M7B_BONUS_LIMIT
          and (M7_BONUS_BASE + i7['slabo_vf_off'] - PS1_FILETAB) % 12 == 0, f"{len(b7):#x} bytes")
    # ---- M8a catalogue (Saturn vs PS1; 02 §Saturn vs PS1 compared)
    dead = m8_inactive_types(); r5s = place_records(pac_chunks('sat', 'STAGE05')[0xA], 'sat'); r5p = place_records(pac_chunks('ps1', 'STAGE05')[0xA], 'ps1')
    check('M8a: STAGE05 P2 type 0x22 (sprite set 0x27): Saturn places 17, the PS1 keeps 5 records but its STAGE05 overlay entry is the stub (deletes the object at spawn)',
          sum(r[0] == 0x22 and r[2] < 2 for r in r5s) == 17 and sum(r[0] == 0x22 and r[2] < 2 for r in r5p) == 5 and 0x22 in dead[5]
          and Ps1().exe.u32(0x801393B4 + 4 * 0x22) & 0xFF == 0x27)
    p29 = {st: sum(r[0] == 0x29 and r[2] == 4 for r in place_records(pac_chunks(sd, st)[0xA], sd)) for sd in ('sat',) for st in M8_STAGES}
    p29p = sum(r[0] == 0x29 and r[2] == 4 for st in M8_STAGES for r in place_records(pac_chunks('ps1', st)[0xA], 'ps1'))
    check('M8a: P5 type 0x29 placed only on the Saturn (stage 0: 1, 6: 5, 7: 1, 0xD: 1; PS1 none); its handler 0x204E0C stores to 0x0606744E',
          p29 == {**{st: 0 for st in M8_STAGES}, 'STAGE00': 1, 'STAGE06': 5, 'STAGE07': 1, 'STAGE0D': 1} and p29p == 0
          and Sat().u32(0x27DB04 + 4 * 0x29) == 0x204E0C and Sat().u32(0x204F50) == 0x0606744E)
    sid = {f: (b, n) for f, b, n, so, po in m8_sound_ids()}
    check('M8a: Saturn BLS00 / BLS01 / WILY4 = banks 0x36 / 0x3F / 0x44 = PS1 PBGM35 / PBGM3E / PBGM43',
          sid.get('BLS00.PAC') == (0x36, 0x35) and sid.get('BLS01.PAC') == (0x3F, 0x3E) and sid.get('WILY4.PAC') == (0x44, 0x43))
    g0s, g0p = pac_chunks('sat', 'STAGE00')[0], pac_chunks('ps1', 'STAGE00')[0]
    check('M8a: STAGE00 grid cells (row 2, cols 8/9) = screen 0x2D on the Saturn, empty on the PS1; the rest of chunk 0 identical',
          g0s[2 * 32 + 8] == g0s[2 * 32 + 9] == 0x2D and g0p[2 * 32 + 8] == g0p[2 * 32 + 9] == 0
          and all(x == y for i, (x, y) in enumerate(zip(g0s, g0p)) if i not in (72, 73)))
    sp2 = Sat()
    check('M8a part 2: Saturn sequencer quick-start points 0x209FC2 / 0x209FE6 = mov r13,r0 (stage / checkpoint 0); stage-0 start records = the PS1\'s (cp0 at 0x27CF9C)',
          fp(sp2.p2[SAT_QS_STAGE - 0x200000:SAT_QS_STAGE - 0x1FFFFE]) == '91f63d952d62' and fp(sp2.p2[SAT_QS_CP - 0x200000:SAT_QS_CP - 0x1FFFFE]) == '91f63d952d62'
          and sat_start_record(0, 0) == 0x27CF9C and sat_start_record(5, 4) == 0x27D30C)
    # ---- JP PS1 disc (only with a JP workspace: setup --jp; reference 02 §JP PS1 disc compared)
    if os.path.isdir(JP_FILES):
        jx = open(os.path.join(JP_FILES, 'SLPS_006.30'), 'rb').read()
        ju = lambda a: struct.unpack('<I', jx[0x800 + a - 0x800C0000:0x804 + a - 0x800C0000])[0]
        jft = [struct.unpack('<3I', jx[0x800 + JP_FILETAB - 0x800C0000 + 12 * i:0x80C + JP_FILETAB - 0x800C0000 + 12 * i]) for i in range(JP_FT_N)]
        ux = open(os.path.join(PS1_FILES, 'SLUS_004.53'), 'rb').read()
        uft = [struct.unpack('<3I', ux[0x800 + PS1_FILETAB - 0x800C0000 + 12 * i:0x80C + PS1_FILETAB - 0x800C0000 + 12 * i]) for i in range(139)]
        check('JP file table 0x80136F84: #8 ROCKBC.STR, #9 DEMO.BIN, #139 BNW.DA (CD track 2), #140 STAFROLL.DA (track 3, its type word is unrelated data); types #0-#139 = US #0-#138 with ROCKBC inserted',
              jft[8][0] == 125969 and jft[9] == (157985, 16804, 5) and jft[139][0] == 174119 and jft[140][0] == 182564
              and [e[2] for e in jft[:8] + jft[9:140]] == [e[2] for e in uft[:139]])
        check('JP ending song 0x800FC6D0: end position from entry 0x8C (STAFROLL.DA), CdlSetloc from entry 0x8B (BNW.DA) -> plays CD tracks 2+3 as one piece (US: entry 0x8A only)',
              fp(ju(0x800FC7C0)) == 'e348e63125bb' and fp(ju(0x800FC7C4)) == '196b1407bc32' and fp(ju(0x800FC7DC)) == '60d7955f1828' and fp(ju(0x800FC804)) == '196b1407bc32'
              and fp(ju(0x800FC80C)) == '2320ac53bf0c' and fp(ju(0x800FC810)) == 'c6fdc10ad3ba')
        snd = [n for n in sorted(os.listdir(os.path.join(PS1_FILES, 'SOUND')))
               if open(os.path.join(PS1_FILES, 'SOUND', n), 'rb').read() != open(os.path.join(JP_FILES, 'SOUND', n), 'rb').read()]
        def bank_ok(n):
            a = {t: x for t, _, x in unpac(os.path.join(PS1_FILES, 'SOUND', n))}; b = {t: x for t, _, x in unpac(os.path.join(JP_FILES, 'SOUND', n))}
            hk = 3 if 3 in a else 4; bk = 0x200 if 0x200 in a else 0x201
            return (sorted(a) == sorted(b) and all(a[t] == b[t] for t in a if t not in (hk, bk)) and len(a[hk]) == len(b[hk])
                    and len(vab_split(a[hk], a[bk])['vags']) == len(vab_split(b[hk], b[bk])['vags']))
        check('JP sound: 43 PBGM banks + PCOMMON differ, only in VAB header/body (same VAG count; sound-ID table and SEQ identical)',
              len(snd) == 44 and 'PCOMMON.PAC' in snd and all(bank_ok(n) for n in snd), f'{len(snd)} files')
        def chunk_diff(n):
            a = unpac(os.path.join(PS1_FILES, 'STDATA', n)); b = unpac(os.path.join(JP_FILES, 'STDATA', n))
            return [t for (t, _, x), (u, _, y) in zip(a, b) if x != y] if [t for t, _, _ in a] == [t for t, _, _ in b] else None
        st = [n for n in os.listdir(os.path.join(PS1_FILES, 'STDATA')) if re.match(r'STAGE[0-9A-D]{2}B?\.PAC$', n)]
        check('JP stage PACs: only chunks 5/6 (sprite sets), 9 (palette 0xD), 0x12 (text) differ (+0x0A in STAGE08/08B: the US has one extra P5 record); tiles 0x101-0x103 identical',
              all(set(chunk_diff(n)) <= {5, 6, 9, 0x12, 0x0A} and (0x0A not in chunk_diff(n) or n.startswith('STAGE08')) for n in st)
              and all({i // 16 for i, (x, y) in enumerate(zip(struct.unpack('<2048H', pac_chunks('ps1', n[:-4])[9]), struct.unpack('<2048H', open(os.path.join(WORK, 'jp', 'pac', n[:-4], '009.bin'), 'rb').read()))) if x != y} == {0xD} for n in st), f'{len(st)} files')
        mt = lambda x, a, n: [(x[0x800 + a - 0x800C0000 + 4 * k], x[0x801 + a - 0x800C0000 + 4 * k], struct.unpack('<H', x[0x802 + a - 0x800C0000 + 4 * k:0x804 + a - 0x800C0000 + 4 * k])[0]) for k in range(n)]
        check('Movie tables (u8 file, u8 skip flag, u16 last frame = frames - 2): US 0x801378F4 ROCK8_0 1341 (1343 frames); JP 0x80137904 ROCK8_0 1328 (1330 frames), ROCKBC (#8) 3009, ROCK8_1-4 the same as the US',
              fp(mt(ux, 0x801378F4, 5)) == '4d3f1675e2d3'
              and fp(mt(jx, 0x80137904, 6)) == '06a6d9296abd')
        check('JP LABO.PAC: same chunk list; title logo tiles (0x102) differ (ROCKMAN vs MEGAMAN), with menu font/text tiles',
              chunk_diff('LABO.PAC') is not None and 0x102 in chunk_diff('LABO.PAC'))
    else:
        print('SKIP JP checks (no JP workspace: setup --jp)')
    check('Release: audio tracks of both discs checked against their Redump md5s (per-track and whole-disc .bin; a different track gives a note, a missing track stops)',
          _selftest_audio_check())
    hits = tool_game_words()
    check('Copyright guard: no word-sized literal in mm8tool.py equals a code/data word of either game (PS1 EXE + overlays, Saturn PRG/BIN; 03 §7)',
          not hits, ', '.join(f'line {ln}: {v:#x}' for ln, v in hits[:10]))
    print(f'\n{sum(res)}/{len(res)} checks passed')
    return all(res)

# ----------------------------------------------------------------- pre-release checks
# Full disc. The Redump layout (the MM8 PS1/ folder): Track 1 MODE2/2352, Tracks 2-3 audio files that each start
# with a 150-sector pregap (INDEX 00 00:00:00, INDEX 01 00:02:00). A patched (longer) track 1 next to the original
# Track 2/3 files and the original .cue puts track 2's INDEX 01 at track-1 sectors + 150 = entry 138 (check_build).
PS1_TRACK_SECTORS = (142096, 19996, 15900)      # original Track 1/2/3 files (2 and 3 include their pregap)
PS1_ENDING_PLAYER = 0x800FC69C                  # (a0 = CD volume) set mode, CdlSetloc/CdlPlay entry 138 = END1.DA;
                                                # called only at 0x80121F2C (a0 = 0x7F) in the ending

def ending_song_image(src_bin, out_bin):
    """Scratch image for headless tests (never delivered): both play_bgm entry points (0x800FCA4C, 0x800FCB80) jump to
    the ending-song player 0x800FC69C(0x7F), so the title screen's first music call plays CD track 2 via entry 138."""
    d = RawDisc(src_bin)
    for a in (0x800FCA4C, 0x800FCB80): d.exe_write_words(a, [0x08000000 | ((PS1_ENDING_PLAYER >> 2) & 0x3FFFFFF), m_i(0x0D, 0, 4, 0x7F)])
    d.save(out_bin)

def full_disc_cue(cue_path, track_files, single=False):
    """Write a .cue for a 3-track disc. track_files = [track1, track2, track3]; separate files = Redump layout (track 2/3
    with their pregap: INDEX 00 00:00:00, INDEX 01 00:02:00); single=True: track_files[0] is one concatenated image
    and the other two are only used for their sizes (INDEX times = file positions)."""
    msf = lambda n: '%02d:%02d:%02d' % (n // 4500, n // 75 % 60, n % 75)
    if not single:
        t = ''.join(f'FILE "{os.path.basename(f)}" BINARY\n  TRACK {k + 1:02d} {"MODE2/2352" if k == 0 else "AUDIO"}\n'
                    + ('    INDEX 01 00:00:00\n' if k == 0 else '    INDEX 00 00:00:00\n    INDEX 01 00:02:00\n')
                    for k, f in enumerate(track_files))
    else:
        n = [os.path.getsize(f) // 2352 for f in track_files]
        t = (f'FILE "{os.path.basename(track_files[0])}" BINARY\n  TRACK 01 MODE2/2352\n    INDEX 01 00:00:00\n'
             f'  TRACK 02 AUDIO\n    INDEX 00 {msf(n[0])}\n    INDEX 01 {msf(n[0] + 150)}\n'
             f'  TRACK 03 AUDIO\n    INDEX 00 {msf(n[0] + n[1])}\n    INDEX 01 {msf(n[0] + n[1] + 150)}\n')
    open(cue_path, 'w').write(t)
    return cue_path

def cd_track_position(wav_path, track_bin, windows=(2, 10, 20, 30), search_s=110):
    """Locate windows of a game recording (Mednafen -soundrecord, 44.1 kHz) in a CD-DA track file (raw 16-bit stereo):
    returns [(recording s, track-file s, correlation)]. A constant recording - file offset with correlation ~1 means
    the game played the track from the expected place (1024 sectors = 13.65 s)."""
    import numpy as np, scipy.signal as sg
    ref = np.frombuffer(open(track_bin, 'rb').read(), '<i2').reshape(-1, 2).astype(float).mean(1)[:int(search_s * 44100)]
    x = _wav(wav_path).mean(1); env = np.convolve(np.abs(x), np.ones(441) / 441, 'same')
    out = []
    for w in windows:
        a = int(w * 44100); seg = x[a:a + 2 * 44100]
        if len(seg) < 2 * 44100 or not seg.any(): continue
        c = sg.fftconvolve(ref, seg[::-1], 'valid'); k = int(np.argmax(c)); r = ref[k:k + len(seg)]
        out.append((w, k / 44100, float(np.dot(r, seg) / np.sqrt(np.dot(r, r) * np.dot(seg, seg) + 1e-12))))
    return out

def mdfn_ram(state_path):
    """PS1 main RAM (2 MB) from a Mednafen 1.29 save state (.mcN = gzip; record 'MainRAM.data8' + u32 size).
    Mednafen powers on with zeroed RAM (untouched areas read 0)."""
    import gzip
    r = gzip.decompress(open(state_path, 'rb').read())
    i = r.index(b'MainRAM.data8') + 13; assert struct.unpack_from('<I', r, i)[0] == 0x200000
    return r[i + 4:i + 4 + 0x200000]

def mdfn_sat_ram(state_path):
    """Saturn work RAM from a Mednafen 1.29 save state: {0x00200000: Low WRAM, 0x06000000: High WRAM}, 1 MB each,
    big-endian bytes (the state stores 16-bit words little-endian: records 'WorkRAML' / 'WorkRAMH' + u32 size)."""
    import gzip
    r = gzip.decompress(open(state_path, 'rb').read()); out = {}
    for name, base in ((b'WorkRAML', 0x00200000), (b'WorkRAMH', 0x06000000)):
        i = r.index(b'\x08' + name) + 9; assert struct.unpack_from('<I', r, i)[0] == 0x100000
        d = bytearray(r[i + 4:i + 4 + 0x100000]); d[0::2], d[1::2] = d[1::2], d[0::2]; out[base] = bytes(d)
    return out

# Threads (BIOS OpenThread at 0x800F7D58: pc = slot +4, sp = slot +0xC, gp = slot +0x44; slots 4 x 0x50 at 0x801FC000).
# Thread k starts with SP 0x801FEC00 + 0x400k (0x800F82D8) and grows down. Thread 0 = game loop 0x801008C8 (frame 0x18),
# which calls the mode handler 0x80137B04[mode 0x801CF840]: mode 7 = stage frame function 0x80100E00. So the stage,
# its overlay objects and the lifted bosses run on thread 0's stack, below 0x801FEC00 down to the slot table end
# 0x801FC140 (10,944 bytes; no fixed-address users, `refs`). Main stack: 0x801FFFF0 down.
PS1_THREAD_SP = [0x801FEC00 + 0x400 * k for k in range(4)]
PS1_STACK_REGIONS = [('thread 0 (game loop 0x801008C8)', 0x801FC140, 0x801FEC00), ('thread 1', 0x801FEC00, 0x801FF000),
                     ('thread 2', 0x801FF000, 0x801FF400), ('thread 3', 0x801FF400, 0x801FF800), ('main', 0x801FF800, 0x801FFFF0)]

def stack_watermark(ram):
    """Deepest use of each stack region in a RAM image whose stacks started zeroed (Mednafen): bytes from the region's
    top to the lowest nonzero word (a lower bound: zero-valued words at the very bottom are not seen)."""
    out = []
    for name, lo, hi in PS1_STACK_REGIONS:
        b = ram[lo & 0x1FFFFF:hi & 0x1FFFFF]
        k = next((i for i in range(0, len(b), 4) if b[i:i + 4] != bytes(4)), None)
        out.append((name, 0 if k is None else hi - lo - k, hi - lo))
    return out

def lifted_stack_usage():
    """Stack use of the lifted code: per C function the frame size from mipsel GCC -fstack-usage (same CFLAGS as the
    build), and per handler the deepest SH-2 stack offset (r15 below entry) of every entry the lifter analyses (main
    entry, jump-table targets, bsr subroutines); their sum bounds the SH-2 stack the C keeps in u32 stk[48] (192 bytes)."""
    import subprocess, tempfile, glob
    frames, sh = {}, {}
    for spec in (WOOD_SPEC, CUT_SPEC):
        wd = tempfile.mkdtemp(prefix='mm8su_')
        csrc, port = boss_lift(spec); c = os.path.join(wd, spec['cfile']); open(c, 'w').write(csrc)
        subprocess.run([CC] + CFLAGS + ['-fstack-usage', '-c', c, '-o', c + '.o'], check=True, cwd=wd)
        for ln in open(glob.glob(os.path.join(wd, '*.su'))[0]):
            f, n, kind = ln.split('\t'); frames[f.split(':')[-1]] = int(n)
        sat = Sat(); code = Code([(sat.BASE2, sat.p2), (sat.BASE1, sat.p1)])
        for name, entry, typ, tabkeys, words in spec['handlers']:
            tables = {k: spec['code_tables'][k] for k in tabkeys}
            ins, ents, calls = discover(code, [entry], tables, None)
            tabs = {t: [code.u32(t + 4 * k) for k in range(n)] for t, n in tables.items()}
            fes = [entry] + sorted(calls); res = analyse(code, ins, fes, tabs)
            more = set()                                    # jsr targets (code tables, constants), as lift() adds them
            for fe, sts in res.items():
                for a, st in sts.items():
                    if ins[a].mn == 'jsr':
                        v = st.r[ins[a].n]
                        if v and v[0] == 'tab': more |= set(tabs[v[1]])
                        elif v and v[0] == 'c' and v[1] in ins: more.add(v[1])
            fes += sorted(more - set(fes)); res = analyse(code, ins, fes, tabs)
            per = {fe: min((st.r[15][1] for st in sts.values() if st.r[15] and st.r[15][0] == 'sp'), default=0) for fe, sts in res.items()}
            unknown = sum(1 for sts in res.values() for st in sts.values() if not (st.r[15] and st.r[15][0] == 'sp'))
            sh[name] = dict(entries=len(per), deepest_entry=-min(per.values()), sum_bound=-sum(per.values()), unknown_r15=unknown)
    return frames, sh

def bonus_stack_usage():
    """M7f: the same two figures for the bonus overlay (glue + lifted bonus code): C frames from -fstack-usage of the
    whole overlay source, and per lifted handler the deepest SH-2 stack offset per analysed entry and their sum (the
    bonus code keeps its SH-2 stack in u32 stk[96] = 384 bytes)."""
    import subprocess, tempfile, glob
    wd = tempfile.mkdtemp(prefix='mm8bsu_'); csrc, port = m7b_lift(); c = os.path.join(wd, 'm7b_bonus.c'); open(c, 'w').write(csrc)
    subprocess.run([CC] + CFLAGS + ['-fstack-usage', '-c', c, '-o', c + '.o'], check=True, cwd=wd)
    frames = {}
    for ln in open(glob.glob(os.path.join(wd, '*.su'))[0]):
        f, n, kind = ln.split('\t'); frames[f.split(':')[-1]] = (int(n), kind.strip())
    sat = Sat(); code = Code([(sat.BASE2, sat.p2), (sat.BASE1, sat.p1)]); sh = {}
    for name, entry, typ, tabkeys, words, stops in M7B_SPEC['handlers']:
        tables = {k: M7B_CODE_TABLES[k] for k in tabkeys}
        ins, ents, calls = discover(code, [entry], tables, None, set(stops))
        tabs = {t: [code.u32(t + 4 * k) for k in range(n)] for t, n in tables.items()}
        res = analyse(code, ins, [entry] + sorted(calls), tabs, set(stops))
        per = {fe: min((st.r[15][1] for st in sts.values() if st.r[15] and st.r[15][0] == 'sp'), default=0) for fe, sts in res.items()}
        unknown = sum(1 for sts in res.values() for st in sts.values() if not (st.r[15] and st.r[15][0] == 'sp'))
        sh[name] = dict(entries=len(per), deepest_entry=-min(per.values()), sum_bound=-sum(per.values()), unknown_r15=unknown)
    return frames, sh

def subprocess_run_nm(elf):
    """{function symbol: address} (types T/t) of a linked ELF (mipsel nm)."""
    d = open(elf, 'rb').read()
    shoff, = struct.unpack_from('<I', d, 0x20); shentsize, shnum = struct.unpack_from('<2H', d, 0x2E)
    sh = [struct.unpack_from('<10I', d, shoff + k * shentsize) for k in range(shnum)]
    out = {}
    for _, typ, _, _, off, size, link, _, _, ent in sh:
        if typ != 2: continue
        stro = sh[link][4]
        for q in range(off, off + size, ent):
            name, value, _, info, _, shndx = struct.unpack_from('<3I2BH', d, q)
            if name and 0 < shndx < 0xFF00 and sh[shndx][2] & 4:        # defined in an executable section (nm T / t)
                out[d[stro + name:d.index(b'\0', stro + name)].decode()] = value
    return out

def static_stack_depth(images, entry, indirect=None, max_ins=8000):
    """M7f: static worst-case stack below `entry` (bytes): each function's frame (its first `addiu sp, sp, -N` in the first
    16 instructions) plus the deepest callee (jal; j outside the function = tail call, counted like a call, which only
    over-estimates; jalr -> the addresses in indirect[site], else reported as unresolved). A function ends at the first
    `jr ra` (+ delay slot) that no branch inside it jumps past. BIOS vectors (0xA0/0xB0/0xC0) count 0.
    images = [(base, bytes)]. Returns (depth, path [(function, frame)], unresolved jalr sites, recursion sites)."""
    import capstone as c
    md = c.Cs(c.CS_ARCH_MIPS, c.CS_MODE_MIPS32 | c.CS_MODE_LITTLE_ENDIAN)
    def word(a):
        for b, d in images:
            if b <= a < b + len(d) - 3: return struct.unpack_from('<I', d, a - b)[0]
        return None
    def func(a):
        ins, frame, k, far, end = [], 0, a, a, None
        while len(ins) < max_ins:
            w = word(k)
            if w is None: break
            i = next(md.disasm_lite(struct.pack('<I', w), k), None)
            if i is None: break
            ins.append(i); mn, ops = i[2], i[3]
            if len(ins) <= 16 and mn == 'addiu' and ops.startswith('$sp, $sp, -') and not frame: frame = int(ops.split('-')[1], 0)
            if mn.startswith('b') and mn not in ('break',):
                t = ops.split(', ')[-1]
                if t.startswith('0x'): far = max(far, int(t, 16))
            if end is not None and k >= end: break
            if mn == 'jr' and ops == '$ra' and k + 4 > far: end = k + 4
            k += 4
        lo, hi = a, k + 4
        callees, unres = [], []
        for addr, size, mn, ops in ins:
            if mn == 'jal': callees.append(int(ops, 16))
            elif mn == 'j':
                t = int(ops, 16)
                if not lo <= t < hi: callees.append(t)
            elif mn == 'jalr':
                if indirect and addr in indirect: callees += list(indirect[addr])
                else: unres.append(addr)
        return frame, callees, unres
    memo, unres_all, rec = {}, set(), set()
    def depth(a, stack):
        if a in (0xA0, 0xB0, 0xC0) or word(a) is None: return 0, [(a, 0)]
        if a in memo: return memo[a]
        if a in stack: rec.add(a); return 0, [(a, 0)]
        frame, callees, unres = func(a); unres_all.update(unres)
        best, bp = 0, []
        for t in callees:
            d, p = depth(t, stack | {a})
            if d > best: best, bp = d, p
        memo[a] = (frame + best, [(a, frame)] + bp); return memo[a]
    d, path = depth(entry, frozenset())
    return d, path, sorted(unres_all), sorted(rec)

REGRESS_CONFIGS_TXT = '0,1,1:2,2,2:3,3,3:3,4,4:3,5,5:3,6,6:2,7,7:2,8,8:2,9,10,11,12,13'   # stage start + one second-half checkpoint

def regression_runs(orig_bin, rel_bin, outdir, configs, workers=2, seconds=88):
    """Check 4: headless Mednafen runs of every (stage, checkpoint) in `configs` on the original and the patched track 1,
    both with the same debug quick start (scratch images, deleted after the run). Keys: Return at 30..50 s (title, Game
    Start), save state (slot 1) at 84 s; screenshots every 4 s; sound recorded. Results in outdir/<side>_sSS_cpC*."""
    import threading, queue
    os.makedirs(outdir, exist_ok=True); q = queue.Queue()
    for st, cp in configs:
        for side, src in (('orig', orig_bin), ('rel', rel_bin)): q.put((st, cp, side, src))
    def run(st, cp, side, src, disp):
        t = f'{side}_s{st:02d}_cp{"x" if cp is None else cp}'; img = os.path.join(outdir, t + '.bin')
        if os.path.exists(os.path.join(outdir, t + '.done')): return
        d = RawDisc(src); patch_quick_start(d, st, cp); d.save(img)
        n, h = mednafen_session(img, os.path.join(outdir, t + '.wav'), seconds, 'MODE2/2352', display=disp, snaps_every=4,
                                keys=[30, 34, 38, 42, 46, 50, (84, '1', 0.15), (84.6, 'F5', 0.2)], home=os.path.join(outdir, t + '_home'))
        os.remove(img); open(os.path.join(outdir, t + '.done'), 'w').write(str(n))
    def worker(disp):
        while True:
            try: job = q.get_nowait()
            except queue.Empty: return
            run(*job, disp)
    ts = [threading.Thread(target=worker, args=(90 + k,)) for k in range(workers)]
    [t.start() for t in ts]; [t.join() for t in ts]

def regression_compare(outdir):
    """Compare each original/patched run pair of regression_runs: sound (56-82 s, aligned by cross-correlation:
    waveform and frame-wise spectral correlation, level difference) and the final save state (stage, music number,
    loaded bank, overlay, checkpoint, thread-0 stack watermark, the RAM the restored STAGE08 chunks 6/7 grow into)."""
    import glob, numpy as np, scipy.signal as sg
    def spec(x): return np.log10(np.abs(sg.stft(x, 44100, nperseg=2048, noverlap=1024)[2]) ** 2 + 1e-10)
    def state(home):
        st = sorted(glob.glob(os.path.join(home, 'mcs', '*.mc*')))
        if not st: return None
        ram = mdfn_ram(st[-1]); r = lambda a, n: ram[a & 0x1FFFFF:(a & 0x1FFFFF) + n]
        return dict(stage=r(0x801C336E, 1)[0], music=r(0x801D28F8, 1)[0], bank=r(0x8015E9C4, 1)[0], ovl=r(0x801C738C, 1)[0],
                    cp=r(0x801C3371, 1)[0], thread0=stack_watermark(ram)[0][1],
                    grow_ram_zero=not any(r(0x8015D0AC, 0x14) + r(0x801704A0, 0x10)))
    rows = []
    for p in sorted(glob.glob(os.path.join(outdir, 'orig_*.done'))):
        t = os.path.basename(p)[5:-5]
        if not os.path.exists(os.path.join(outdir, f'rel_{t}.done')): continue
        a, b = (_wav(os.path.join(outdir, f'{s}_{t}.wav')) for s in ('orig', 'rel'))
        A = a[56 * 44100:82 * 44100]; seg = A.mean(1)[5 * 44100:20 * 44100]; Bm = b.mean(1)[52 * 44100:86 * 44100]
        k = int(np.argmax(sg.fftconvolve(Bm, seg[::-1], 'valid'))); lag = (k - 9 * 44100) / 44100
        B = b[int((56 + lag) * 44100):][:len(A)]; A = A[:len(B)]
        am, bm = A.mean(1), B.mean(1)
        wc = float(np.dot(am, bm) / np.sqrt(np.dot(am, am) * np.dot(bm, bm) + 1e-12))
        SA, SB = spec(am), spec(bm)
        cs = [np.corrcoef(SA[:, i], SB[:, i])[0, 1] for i in range(min(SA.shape[1], SB.shape[1])) if SA[:, i].std() > 1e-6 and SB[:, i].std() > 1e-6]
        rows.append(dict(run=t, lag=lag, wave_corr=wc, spec_corr=float(np.mean(cs)) if cs else float('nan'),
                         level_db=float(10 * np.log10((B ** 2).mean() / ((A ** 2).mean() + 1e-12) + 1e-12)),
                         orig=state(os.path.join(outdir, f'orig_{t}_home')), rel=state(os.path.join(outdir, f'rel_{t}_home'))))
    return rows


# ---- US vs JP PS1 disc (region-select survey; reference 02 §JP PS1 disc compared)
JP_FILETAB = 0x80136F84          # JP EXE SLPS_006.30 file table: 141 x (LBA, size, type); US #k = JP #k (k <= 7), US #k = JP #k+1 after
JP_FT_N = 141
JP_FT = {'ROCKBC.STR': 8, 'DEMO.BIN': 9, 'BNW.DA': 139, 'STAFROLL.DA': 140}

def iso_listing(iso_path):
    """[(path, extent, size)] of an ISO9660 image (2048-byte sectors), sorted by extent; sizes as signed 32-bit like pycdlib."""
    import pycdlib
    iso = pycdlib.PyCdlib(); iso.open(iso_path); out = []
    for root, dirs, files in iso.walk(iso_path='/'):
        for fn in files:
            q = root.rstrip('/') + '/' + fn; r = iso.get_record(iso_path=q)
            out.append((q.split(';')[0].lstrip('/'), r.extent_location(), r.data_length))
    iso.close()
    return sorted(out, key=lambda x: x[1])

def raw_iso_listing(bin_path):
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        iso = os.path.join(d, 't.iso'); raw_to_iso(bin_path, iso); return iso_listing(iso)

def raw_payloads(f, lba, n):
    """Position-independent payload of n raw MODE2 sectors (subheader + user data; header/EDC/ECC dropped)."""
    f.seek(lba * 2352); out = []
    for _ in range(n):
        s = f.read(2352); out.append(s[16:24] + (s[24:2348] if s[18] & 0x20 else s[24:2072]))
    return out

def str_parts(f, lba, n):
    """STR movie: ({frame: video bytes without the 32-byte sector headers}, [audio sector payloads], other sector count)."""
    f.seek(lba * 2352); vid, aud, other = collections.defaultdict(bytes), [], 0
    for _ in range(n):
        s = f.read(2352); sm = s[18]
        if sm & 4: aud.append(s[24:2348])
        elif sm & 2: p = s[24:2072]; vid[struct.unpack('<I', p[8:12])[0]] += p[32:]
        else: other += 1
    return dict(vid), aud, other

def region_compare(us_bin, jp_bin):
    """US (SLUS-00453) vs JP (SLPS-00630) PS1 track 1: every ISO file compared by raw sector payload; STR movies split into
    video / audio; PAC files chunk by chunk; sound banks VAG by VAG; JP-only files and their sectors."""
    ul, jl = raw_iso_listing(us_bin), raw_iso_listing(jp_bin)
    un, jn = {r[0]: r for r in ul}, {r[0]: r for r in jl}
    sec = lambda b: (b + 2047) // 2048
    rep = dict(us_only=[r[0] for r in ul if r[0] not in jn], jp_only=[(r[0], r[1], r[2]) for r in jl if r[0] not in un],
               same=[], diff=[], movies={}, pacs={}, banks={})
    uf, jf = open(us_bin, 'rb'), open(jp_bin, 'rb')
    for name, lba, size in ul:
        if name not in jn or size < 0: continue
        _, jlba, jsize = jn[name]
        if name.endswith('.STR'):
            uv, ua, uo = str_parts(uf, lba, sec(size)); jv, ja, jo = str_parts(jf, jlba, sec(jsize))
            rep['movies'][name] = dict(us_sectors=sec(size), jp_sectors=sec(jsize), us_frames=len(uv), jp_frames=len(jv),
                                       video_same=uv == jv, audio_same=ua == ja,
                                       shared_frames=len({hashlib.md5(v).digest() for v in uv.values()} & {hashlib.md5(v).digest() for v in jv.values()}))
        same = raw_payloads(uf, lba, sec(size)) == raw_payloads(jf, jlba, sec(jsize))
        (rep['same'] if same else rep['diff']).append(name)
    for name in rep['diff']:
        if not name.endswith('.PAC'): continue
        a = unpac(os.path.join(PS1_FILES, name)); b = unpac(os.path.join(JP_FILES, name))
        da, db = {t: x for t, _, x in a}, {t: x for t, _, x in b}
        rep['pacs'][name] = dict(types_same=[t for t, _, _ in a] == [t for t, _, _ in b],
                                 chunks=[(t, len(da[t]), len(db.get(t, b''))) for t in da if da[t] != db.get(t)])
        if name.startswith('SOUND/'):
            hk = 3 if 3 in da else 4; bk = 0x200 if 0x200 in da else 0x201
            va, vb = vab_split(da[hk], da[bk]), vab_split(db[hk], db[bk])
            d = [i for i in range(len(va['vags'])) if va['vags'][i] != vb['vags'][i]]
            progs = sorted({p for p, tl in enumerate(va['tones']) for tn in tl
                            if struct.unpack('<H', tn[22:24])[0] - 1 in d and struct.unpack('<H', tn[22:24])[0]})
            rep['banks'][name] = dict(vags=(len(va['vags']), len(vb['vags'])), diff_vags=d, programs=progs,
                                      jp_bytes=sum(len(vb['vags'][i]) for i in d), jp_sectors=sec(jn[name][2]))
    rep['jp_listing'], rep['us_listing'] = jl, ul
    return rep

def region_report(rep):
    sec = lambda b: (b + 2047) // 2048
    print('US-only files:', ', '.join(rep['us_only'])); print('JP-only files:', ', '.join(f'{n} (LBA {l})' for n, l, s in rep['jp_only']))
    print(f"identical: {len(rep['same'])}  different: {len(rep['diff'])}")
    print('identical files:', ' '.join(rep['same']))
    for n, m in rep['movies'].items():
        print(f"{n}: sectors US {m['us_sectors']} JP {m['jp_sectors']}, frames {m['us_frames']}/{m['jp_frames']}, video "
              f"{'same' if m['video_same'] else 'DIFFERENT'} (shared frames {m['shared_frames']}), audio {'same' if m['audio_same'] else 'different'}")
    for n, p in rep['pacs'].items():
        if n.startswith('SOUND/'): continue
        print(f"{n}: " + ' '.join(f'{t:#x}({a}/{b})' for t, a, b in p['chunks']))
    tot = 0
    for n, b in rep['banks'].items():
        tot += b['jp_sectors']; print(f"{n}: VAGs {b['vags']}, differing {b['diff_vags']} (programs {b['programs']}), JP {b['jp_bytes']} bytes")
    print(f"differing sound files: {len(rep['banks'])}, {tot} sectors as JP files")

# ----------------------------------------------------------------- M8a: US Saturn vs US PS1 catalogue (Phase 3)
# Every shared PAC compared chunk by chunk after the format converters, placement lists record by record, sound banks by
# their sound-ID tables. Graphics chunks with no known PS1 layout (0x10, 0x11, 0x15, PLAYER 1/3) are compared as the
# multiset of 8-pixel rows (4 bytes; PS1 nibble-swapped): equal multisets = the same pixels re-laid out.
M8_STAGES = [f'STAGE0{c}' for c in '0123456789ABCD']

def _pal_diff(s, p):
    a = struct.unpack('>%dH' % (len(s) // 2), s); b = struct.unpack('<%dH' % (len(p) // 2), p)
    return [i for i, (x, y) in enumerate(zip(a, b)) if (x & 0x7FFF) != (y & 0x7FFF)]

def _row_multiset(b, nibswap=False):
    if nibswap: b = bytes(((x >> 4) | (x << 4)) & 0xFF for x in b)
    return collections.Counter(b[i:i + 4] for i in range(0, len(b) - 3, 4) if any(b[i:i + 4]))

def place_records(chunk, side):
    """Chunk 0x0A -> [(type, variant, pool, x, y)] up to the terminator."""
    e = '>' if side == 'sat' else '<'; out = []
    for i in range(0, len(chunk), 8):
        r = chunk[i:i + 8]
        if r[3] == 0xFF: break
        out.append((r[1], r[2], r[3]) + struct.unpack(e + 'HH', r[4:8]))
    return out

def m8_chunk_compare(name):
    """{chunk type: (verdict, detail)} for a PAC present on both discs. Verdict 'same' / 'same pixels' / text."""
    sd = {t: b for t, o, b in unpac(os.path.join(SAT_FILES, name))}
    pd = {t: b for t, o, b in unpac(os.path.join(PS1_FILES, 'STDATA', name))}
    out = {}
    for t in sorted(set(sd) | set(pd)):
        if t not in sd or t not in pd: out[t] = ('only on ' + ('PS1' if t in pd else 'Saturn'), None); continue
        s, p = sd[t], pd[t]
        if name == 'PLAYER.PAC' and t == 2: d = _pal_diff(s, p); out[t] = ('same' if not d else f'{len(d)} colours', d); continue
        if name == 'PLAYER.PAC' or t in (0x10, 0x11, 0x15):
            out[t] = ('same pixels' if _row_multiset(s) == _row_multiset(p, True) else 'graphics differ', None); continue
        if t in (0, 1, 2, 8, 0x12):
            out[t] = ('same' if s == p else (f'{sum(x != y for x, y in zip(s, p))} cells' if t < 3 else f'differs ({len(s)}/{len(p)} bytes)'), None); continue
        if t == 3:
            c = b''.join(s[i:i + 2][::-1] for i in range(0, len(s), 2)); n = min(len(c), len(p)) // 512
            d = [k for k in range(n) if c[512 * k:512 * k + 512] != p[512 * k:512 * k + 512]]
            out[t] = ('same' if not d and len(s) == len(p) else f'screens {len(s) // 512}/{len(p) // 512}, {len(d)} differ', d); continue
        if t == 4:
            c = blocks_sat_to_ps1(s); n = min(len(c), len(p)) // 4
            d = [k for k in range(n) if c[4 * k:4 * k + 4] != p[4 * k:4 * k + 4]]
            out[t] = ('same' if not d and len(s) == len(p) else f'blocks {len(s) // 4}/{len(p) // 4}, {len(d)} differ', d); continue
        if t in (5, 6, 7):
            try: c = front_pac_sat_to_ps1(t, s)
            except AssertionError: c = None
            out[t] = ('same' if c == p else 'PS1 = Saturn minus a tail' if c and c[:len(p)] == p else 'differs', None); continue
        if t in (9, 0xB, 0xF, 0x14):
            d = _pal_diff(s, p); out[t] = ('same' if not d else f'{len(d)} colours', d); continue
        if t in (0xC, 0xD, 0xE):
            c = bytes(x & ~0x40 if i % 2 else x for i, x in enumerate(s)); out[t] = ('same' if c == p else 'differs', None); continue
        if t == 0x0A:
            a, b = place_records(s, 'sat'), place_records(p, 'ps1')
            out[t] = ('same' if a == b else f'records {len(a)}/{len(b)}', None); continue
        if t == 0x13: out[t] = ('same' if swap32(s) == p else 'differs', None); continue
        if t in (0x100, 0x101, 0x102, 0x103):
            c = gfx_sat_to_ps1(s, t == 0x103); n = min(len(c), len(p)) // 128
            d = [k for k in range(n) if ps1_tile(c, k) != ps1_tile(p, k)]
            out[t] = ('same' if not d and len(s) == len(p) else f'{len(d)} of {len(p) // 128} tiles', d); continue
        out[t] = ('same' if s == p else 'differs (no converter)', None)
    return out

def m8_placement_diff(stage):
    """Saturn vs PS1 chunk 0x0A of a stage: (sat records, ps1 records, moved [(sat, ps1)], Saturn-only, PS1-only).
    'Moved' pairs a Saturn-only and a PS1-only record of the same type/variant/pool within 0x100 pixels (Manhattan)."""
    a = place_records(pac_chunks('sat', stage)[0xA], 'sat'); b = place_records(pac_chunks('ps1', stage)[0xA], 'ps1')
    ca, cb = collections.Counter(a), collections.Counter(b)
    so, po = sorted((ca - cb).elements()), sorted((cb - ca).elements()); moved, rem = [], []
    for r in so:
        c = [q for q in po if q[:3] == r[:3] and abs(q[3] - r[3]) + abs(q[4] - r[4]) <= 0x100]
        if c:
            q = min(c, key=lambda q: abs(q[3] - r[3]) + abs(q[4] - r[4])); moved.append((r, q)); po.remove(q)
        else: rem.append(r)
    return a, b, moved, rem, po

def m8_inactive_types(ps=None):
    """{stage index: set of P2 types whose PS1 overlay entry is the stub (the object deletes itself at spawn)}."""
    ps = ps or Ps1(); out = {}
    for img, t, tab, filler, _ in overlay_tables(ps):
        out[img.u32(img.b) - 6] = {k for k in range(PS1_P2_LEN) if tab[k] == filler}
    return out

def m8_sound_ids():
    """[(Saturn bank file, bank, PBGM n, Saturn-only IDs, PS1-only IDs)] (ID 0 = the bank's song, left out)."""
    out = []
    for f in sorted(os.listdir(SAT_FILES)):
        if not f.endswith('.PAC'): continue
        tab = pac_chunks('sat', f).get(0x201)
        if not tab: continue
        ids = {k for k in range(len(tab) // 8) if any(tab[8 * k:8 * k + 8])} - {0}
        bank = collections.Counter(tab[8 * k] for k in ids).most_common(1)[0][0] if ids else tab[0]
        p = os.path.join(PS1_FILES, 'SOUND', f'PBGM{bank - 1:02X}.PAC')
        if not os.path.exists(p): out.append((f, bank, None, sorted(ids), [])); continue
        t5 = {t: b for t, o, b in unpac(p)}[5]
        pids = {k - 0x50 for k in range(0x51, len(t5) // 4) if t5[4 * k]}
        out.append((f, bank, bank - 1, sorted(ids - pids), sorted(pids - ids)))
    return out

def m8_report():
    shared = sorted(f for f in os.listdir(os.path.join(PS1_FILES, 'STDATA')) if os.path.exists(os.path.join(SAT_FILES, f)))
    for n in shared:
        r = {t: v for t, (v, d) in m8_chunk_compare(n).items() if v not in ('same', 'same pixels')}
        print(f'{n}: ' + ('same' if not r else ' '.join(f'{t:#x}[{v}]' for t, v in r.items())))
    dead = m8_inactive_types()
    for i, st in enumerate(M8_STAGES):
        a, b, mv, so, po = m8_placement_diff(st)
        en = lambda L: sum(r[2] < 2 for r in L)
        off = sum(r[2] < 2 and r[0] in dead.get(i, ()) for r in b)
        print(f'{st}: records {len(a)}/{len(b)}, enemies {en(a)}/{en(b)} ({off} inactive on PS1), moved {len(mv)}, Saturn-only {len(so)}, PS1-only {len(po)}')
        for r in so + po:
            if r[2] >= 2: print(f"    {'Saturn' if r in so else 'PS1'}-only pool {r[2]} type {r[0]:#x} v{r[1]:#x} ({r[3]:#x}, {r[4]:#x})")
    for f, bank, n, so, po in m8_sound_ids():
        if n is None: print(f'{f} (bank {bank:#x}): no PS1 bank'); continue
        if so or po: print(f'{f} (bank {bank:#x}, PBGM{n:02X}): Saturn-only IDs {[hex(x) for x in so]}, PS1-only {[hex(x) for x in po]}')

# ---- M8a part 2: side-by-side test starts on both systems (scratch images and states only, never a release)
# Saturn sequencer 0x209FA4 (phase byte 0x0602DA84; = PS1 0x80100A34): phase 0 stores stage 0 through `mov r13,r0`
# (r13 = 0) at 0x209FC2 and, with the stage-load mode, checkpoint 0 through the same instruction at 0x209FE6 -> a
# one-instruction quick start each (`mov #n,r0`). Saturn top mode 0x0602DA5A (table 0x27DD0C: 0 title/new game 0x209F28,
# 2 = 0x209F96 = the sequencer, 3 = stage 0x20A054 with sub-mode 0x0602DA5B, table 0x27DD24: 0 load, 3 frame 0x20A3BC).
# Stage start records are the PS1's (0x80138288) byte for byte with big-endian halfwords (stage 0 cp0 at 0x27CF9C).
SAT_QS_STAGE, SAT_QS_CP = 0x209FC2, 0x209FE6

def sat_quick_start(disc, stage, cp=0):
    """Saturn debug quick start (scratch images only): Game Start goes to `stage` at checkpoint `cp`."""
    _, _, ext, _ = disc.iso_record('2NDREAD.PRG')
    for a, v in ((SAT_QS_STAGE, stage), (SAT_QS_CP, cp)):
        assert fp(disc.read(ext, 2, a - 0x200000)) == '91f63d952d62', hex(a)
        disc.write(ext, a - 0x200000, bytes([0xE0, v]))

def sat_start_record(stage, cp):
    """Address of the Saturn copy of PS1 start record 0x80138288[stage][cp] (found by its bytes)."""
    ex = Ps1().exe; rec = ex.u32(ex.u32(0x80138288 + 4 * stage) + 4 * cp)
    raw = b''.join(ex.u32(rec + 4 * i).to_bytes(4, 'little') for i in range(6))
    pat = raw[:8] + struct.pack('>8H', *struct.unpack('<8H', raw[8:]))
    p2 = Sat().p2; i = p2.find(pat); assert i >= 0 and p2.count(pat) == 1
    return 0x200000 + i

def test_start_pair(ps1_src, sat_src, ps1_out, sat_out, stage, cp, player=None, camera=None, bounds=None):
    """Scratch images for a side-by-side run: both systems start `stage` at `cp`; optionally with that start record
    rewritten (player x, y; camera x, y; bounds xmin, xmax, ymin, ymax)."""
    d = RawDisc(ps1_src); patch_quick_start(d, stage, cp)
    s = SatRawDisc(sat_src); sat_quick_start(s, stage, cp)
    if player:
        vals = list(player) + list(camera) + list(bounds)
        tab = d.exe_words(0x80138288 + 4 * stage, 1)[0]; rec = d.exe_words(tab + 4 * cp, 1)[0]
        d.exe_write(rec + 8, struct.pack('<8H', *vals))
        _, _, ext, _ = s.iso_record('2NDREAD.PRG'); s.write(ext, sat_start_record(stage, cp) + 8 - 0x200000, struct.pack('>8H', *vals))
    d.save(ps1_out); s.save(sat_out)

def mdfn_state_poke(src, dst, pokes):
    """Copy a Mednafen 1.29 save state with RAM bytes changed ({addr: bytes}): PS1 0x80000000-0x801FFFFF (MainRAM),
    Saturn 0x06000000-0x060FFFFF (WorkRAMH) / 0x00200000-0x002FFFFF (WorkRAML), big-endian addresses (stored as LE
    words). Used for the Saturn stage select: a state during the stage load after Game Start with 0x0602DA84 = 1 (phase),
    0x0602DA5A = 2 (sequencer), 0x0602DA5B = 0 opens the stage select."""
    import gzip
    r = bytearray(gzip.decompress(open(src, 'rb').read()))
    for a, v in pokes.items():
        for k, b in enumerate(v):
            x = a + k
            if x >= 0x80000000: r[r.index(b'MainRAM.data8') + 17 + (x & 0x1FFFFF)] = b
            else: r[r.index(b'\x08WorkRAMH' if x >= 0x06000000 else b'\x08WorkRAML') + 13 + ((x & 0xFFFFF) ^ 1)] = b
    open(dst, 'wb').write(gzip.compress(bytes(r)))

# ----------------------------------------------------------------- CLI

# ----------------------------------------------------------------- release builder (M6, v0.2.0: Phases 1 and 2)
# The user's discs are checked against the Redump dumps, every track of both discs: each Track 1 (RELEASE_IN_MD5) and
# the audio tracks (RELEASE_AUDIO_MD5; each counted from its INDEX 00, as Redump's per-track files). Users give the
# complete disc: a missing track stops the builder. A Track 1 that differs stops it; an audio track that differs only
# gives a note, because most rippers don't correct the drive's read offset (the audio is shifted by a few samples,
# inaudible, but the md5 differs); the PS1 audio tracks are copied into the output as the user gave them. The md5
# of the image the tested toolchain produces is RELEASE_OUT_MD5; another compiler build may give a different (still valid) image.
RELEASE_VERSION = '0.2.0'
RELEASE_IN_MD5 = {'ps1': '4e9e6dcf27ea0e15c5f4d1f93c800378', 'sat': 'a4d575cd681b7d7c88b45d639ed7ffb7'}
RELEASE_AUDIO_MD5 = {'ps1': {2: 'a75dcbb5c831a966c47563e7ea4150c9', 3: '2d7b5e8e94a91bf5423b2356f6a34863'},
                     'sat': {2: 'a2201327867523e4e31de62c445e2996'}}
RELEASE_OUT_MD5 = 'b3b27b25a65dd1d746fbd5cb02db01aa'
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

def _selftest_audio_check():
    """check_audio_tracks on small made-up discs: one .bin per track and one whole-disc .bin pass with no note; a
    changed audio byte gives a note (no stop); a missing file and a missing track stop."""
    import tempfile, shutil
    d = tempfile.mkdtemp(prefix='mm8aud_'); S = 2352
    try:
        data = {n: bytes([n]) * (S * (150 + 4 * n)) for n in (1, 2, 3)}
        exp = {n: hashlib.md5(data[n]).hexdigest() for n in (2, 3)}
        for n in data: open(os.path.join(d, f't{n}.bin'), 'wb').write(data[n])
        open(os.path.join(d, 'all.bin'), 'wb').write(data[1] + data[2] + data[3])
        tf = lambda b: '%02d:%02d:%02d' % (b // S // 75 // 60, b // S // 75 % 60, b // S % 75)
        per = 'FILE "t1.bin" BINARY\n  TRACK 01 MODE2/2352\n    INDEX 01 00:00:00\n' + ''.join(
            f'FILE "t{n}.bin" BINARY\n  TRACK 0{n} AUDIO\n    INDEX 00 00:00:00\n    INDEX 01 00:02:00\n' for n in (2, 3))
        o2, o3 = len(data[1]), len(data[1]) + len(data[2])
        whole = ('FILE "all.bin" BINARY\n  TRACK 01 MODE2/2352\n    INDEX 01 00:00:00\n'
                 f'  TRACK 02 AUDIO\n    INDEX 00 {tf(o2)}\n    INDEX 01 {tf(o2 + 150 * S)}\n'
                 f'  TRACK 03 AUDIO\n    INDEX 00 {tf(o3)}\n    INDEX 01 {tf(o3 + 150 * S)}\n')
        short = per.split('FILE "t3.bin"')[0]
        def run(text, name):
            c = os.path.join(d, name); open(c, 'w').write(text)
            try: return 'note' if check_audio_tracks(c, cue_tracks(c), exp) else 'ok'
            except SystemExit: return 'stop'
        ok = run(per, 'per.cue') == 'ok' and run(whole, 'whole.cue') == 'ok' and run(short, 'short.cue') == 'stop'
        b = bytearray(data[3]); b[-1] ^= 1; open(os.path.join(d, 't3.bin'), 'wb').write(b)
        ok = ok and run(per, 'bad.cue') == 'note'
        os.remove(os.path.join(d, 't3.bin'))
        return ok and run(per, 'miss.cue') == 'stop'
    finally:
        shutil.rmtree(d, ignore_errors=True)

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
    if sys.argv[1:2] == ['release']:            # the release builder has its own options
        release_main(sys.argv[2:]); return
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest='cmd', required=True)
    s = sp.add_parser('setup'); s.add_argument('--saturn'); s.add_argument('--ps1'); s.add_argument('--jp')
    sp.add_parser('verify')
    s = sp.add_parser('map-helpers'); s.add_argument('--out', default=os.path.join(WORK, 'helper_map.txt'))
    s = sp.add_parser('dis-sat'); s.add_argument('addr'); s.add_argument('n', nargs='?', type=int, default=40)
    s = sp.add_parser('dis-ps1'); s.add_argument('addr'); s.add_argument('n', nargs='?', type=int, default=40); s.add_argument('--ovl')
    s = sp.add_parser('pac'); s.add_argument('path')
    s = sp.add_parser('places'); s.add_argument('stage'); s.add_argument('side', nargs='?', default='sat')
    s = sp.add_parser('refs'); s.add_argument('lo'); s.add_argument('hi'); s.add_argument('--in', dest='where', default='all')
    s = sp.add_parser('map-globals'); s.add_argument('--out', default=os.path.join(WORK, 'globals_map.txt'))
    s = sp.add_parser('catalog'); s.add_argument('lo'); s.add_argument('hi')
    sp.add_parser('selftest-cc')
    s = sp.add_parser('build'); s.add_argument('--ps1', required=True); s.add_argument('--out', required=True); s.add_argument('--xdelta')
    s.add_argument('--quick-start', type=lambda x: int(x, 0), help='debug: new game starts in this stage (8 = Search Man)')
    s.add_argument('--checkpoint', type=lambda x: int(x, 0), help="debug: with --quick-start, start at this checkpoint (stage 8: 5 = corridor before Wood Man's second gate)")
    s.add_argument('--test-start', help='debug: px,py,cx,cy,bounds_entry = start record for --quick-start/--checkpoint (stage 5 cp 2: 0xD40,0xA40,0xD00,0xA00,0x21 = bolt 19 floor room)')
    s.add_argument('--keep-c', help='directory for the generated C / build files')
    s = sp.add_parser('audio-compare'); s.add_argument('--bank', default='WOOD2.PAC'); s.add_argument('--saturn', required=True); s.add_argument('--ps1', required=True)
    s = sp.add_parser('calibrate'); s.add_argument('--saturn', required=True); s.add_argument('--ps1', required=True); s.add_argument('--bank', default='WOOD2.PAC'); s.add_argument('--workdir', default='calib'); s.add_argument('--sat-meas', help='reuse a saved Saturn measurement (sat_meas.pkl)')
    for nm in ('difftest', 'difftest-m2'):
        s = sp.add_parser(nm); s.add_argument('-n', type=int, default=300); s.add_argument('--kinds'); s.add_argument('--frames', type=int, default=10)
        s.add_argument('--boss', default='wood', choices=['wood', 'cut', 'bonus'])
        s.add_argument('--uninit', action='store_true', help='also report MIPS stack reads before write and reads outside loaded memory')
    s = sp.add_parser('ending-image'); s.add_argument('--ps1', required=True); s.add_argument('--out', required=True)
    s = sp.add_parser('stack-report'); s.add_argument('states', nargs='*', help='Mednafen save states (.mcN): stack watermarks')
    s = sp.add_parser('regress'); s.add_argument('--orig', required=True); s.add_argument('--rel', required=True); s.add_argument('--out', required=True)
    s.add_argument('--configs', default=REGRESS_CONFIGS_TXT, help='stage[:checkpoint],... (hex or decimal)'); s.add_argument('--workers', type=int, default=2)
    sp.add_parser('m8-compare')
    s = sp.add_parser('release'); s.add_argument('rest', nargs=argparse.REMAINDER)
    s = sp.add_parser('region-compare'); s.add_argument('--us', required=True); s.add_argument('--jp', required=True)
    s = sp.add_parser('regress-compare'); s.add_argument('dir')
    a = ap.parse_args()
    if a.cmd == 'release':
        release_main(a.rest); return
    if a.cmd == 'setup':
        for side, src in (('sat', a.saturn), ('ps1', a.ps1), ('jp', a.jp)):
            if not src: continue
            d = os.path.join(WORK, side); os.makedirs(d, exist_ok=True)
            iso = os.path.join(d, 'track1.iso')
            kind = raw_to_iso(src, iso)
            if (kind == 'saturn') != (side == 'sat'): raise SystemExit(f'{src} looks like {kind}, not {side}')
            n = extract_iso(iso, os.path.join(d, 'files'))
            print(f'{side}: extracted {n} files from {src}')
            pacroot = os.path.join(d, 'pac')
            for root, _, fs in os.walk(os.path.join(d, 'files')):
                for fn in fs:
                    if fn.upper().endswith('.PAC'):
                        od = os.path.join(pacroot, fn.split('.')[0]); os.makedirs(od, exist_ok=True)
                        for t, o, b in unpac(os.path.join(root, fn)):
                            open(os.path.join(od, f'{t:03x}.bin'), 'wb').write(b)
            os.remove(iso)
        print('done. run: python3 mm8tool.py verify')
    elif a.cmd == 'verify':
        sys.exit(0 if verify() else 1)
    elif a.cmd == 'map-helpers':
        mp, votes, npairs = build_helper_map()
        with open(a.out, 'w') as f:
            f.write(f'# saturn_addr ps1_addr votes  (from {npairs} shared handler pairs)\n')
            f.write('# votes column: n/total from alignment, "manual" = hand-verified (mm8tool MANUAL), "inline" = no PS1 function\n')
            for x in sorted(set(mp) | set(INLINE)):
                if x in INLINE: f.write(f'{x:08x} -------- inline  {INLINE[x]}\n'); continue
                c = votes.get(x, {})
                f.write(f'{x:08x} {mp[x]:08x} ' + ('manual' if x in MANUAL else f'{c.get(mp[x], 0)}/{sum(c.values()) if c else 0}') + '\n')
        got = [x for x in BOSS_HELPERS if x in mp or x in INLINE]
        print(f'{len(mp)} helpers mapped from {npairs} pairs; boss helpers mapped {len(got)}/{len(BOSS_HELPERS)}')
        print('unmapped boss helpers:', ' '.join(f'{x:08x}' for x in BOSS_HELPERS if x not in mp and x not in INLINE) or 'none')
        print('wrote', a.out)
    elif a.cmd == 'dis-sat':
        print(Sat().dis(int(a.addr, 16), a.n))
    elif a.cmd == 'dis-ps1':
        ps = Ps1(); img = ps.ovl(a.ovl) if a.ovl else ps.exe
        print(ps.dis(img, int(a.addr, 16), a.n))
    elif a.cmd == 'pac':
        for t, o, b in unpac(a.path): print(f'type {t:#05x} off {o:#08x} size {len(b):#08x}')
    elif a.cmd == 'map-globals':
        gm = build_global_map()
        with open(a.out, 'w') as f:
            f.write('# saturn_addr ps1_addr votes  (access-level alignment of shared code; rows with total>votes disagree)\n')
            f.write('# Hand-verified rows are in mm8tool GLOBALS_MANUAL and override this file.\n')
            for x in sorted(gm): y, n, t = gm[x]; f.write(f'{x:08x} {y:08x} {n}/{t}\n')
        print(len(gm), 'globals mapped; wrote', a.out)
    elif a.cmd == 'catalog':
        out = catalog(int(a.lo, 16), int(a.hi, 16))
        for v, n, t in out['helpers']: print(f'helper  {v:08x} x{n:<3} -> ' + (f'{t:08x}' if isinstance(t, int) else t))
        for v, n, t in out['globals']: print(f'global  {v:08x} x{n:<3} -> ' + (f'{t:08x}' if isinstance(t, int) else (f'{t[1]:08x} (block-inferred, hypothesis)' if t else 'UNMAPPED')))
        for v, n in out['data']: print(f'data    {v:08x} x{n}')
        for v, n in out['code_ptrs']: print(f'codeptr {v:08x} x{n}')
        print('bsr targets:', ' '.join(f'{x:08x}' for x in out['bsr']))
    elif a.cmd == 'selftest-cc':
        import tempfile
        wd = tempfile.mkdtemp(prefix='mm8cc_'); src = os.path.join(wd, 't.c')
        open(src, 'w').write(SELFTEST_C)
        syms = {'obj_set_anim': 0x8010488C, 'obj_face_player': 0x8010910C, 'obj_delete': 0x80105864, 'enemy39': 0x801D9468, 'm1_counter': 0x801E1648}
        data, sy = compile_c([src], 0x801E1610, syms, entry_first='m1_handler', workdir=wd)
        probs = check_r3000(data, 0x801E1610, sy.get('states'))
        print(f'compiled {len(data)} bytes at 0x801E1610; m1_handler @{sy["m1_handler"]:#x}; R3000 check:', 'OK' if not probs else probs)
        sys.exit(1 if probs else 0)
    elif a.cmd == 'build':
        if a.keep_c: os.makedirs(a.keep_c, exist_ok=True)
        ts = tuple(int(v, 0) for v in a.test_start.split(',')) if a.test_start else None
        info = build_image(a.ps1, a.out, a.quick_start, a.keep_c, a.checkpoint, ts)
        for k, v in info.items():
            if k not in ('bmap', 'tmap'): print(f'{k}: {v:#x}' if isinstance(v, int) else f'{k}: {v}')
        if a.xdelta:
            import subprocess
            subprocess.run(['xdelta3', '-e', '-9', '-f', '-s', a.ps1, a.out, a.xdelta], check=True)
            print('wrote', a.xdelta, os.path.getsize(a.xdelta), 'bytes')
        print('check_build:', check_build(a.ps1, a.out, a.xdelta))
    elif a.cmd == 'calibrate':
        import pickle
        sm = pickle.load(open(a.sat_meas, 'rb')) if a.sat_meas else None
        r, sm, pm = calibrate(a.saturn, a.ps1, a.bank, a.workdir, sat_meas=sm)
        pickle.dump((r, sm, pm), open(os.path.join(a.workdir, 'calibration.pkl'), 'wb'))
        print('tempo factor Saturn %.4f PS1 %.4f; PS1 level offset %.1f dB' % (sm['tempo'], pm['tempo'], r['offset']))
        for k, v in sorted(r['level'].items()):
            print('voice %d key %3d: level PS1-Saturn %+5.1f dB  pan L-R %+5.1f / %+5.1f  pitch %+4.0f cents  bands %s' % (*k, v[2], v[3], v[4], v[5], ' '.join('%+5.1f' % x for x in r['bands'][k])))
        for k, l in sorted(r['vel'].items()): print('velocity voice %d key %d: ' % k + ' '.join('%d:%+.1f/%+.1f' % x for x in l))
        for k, l in sorted(r['cc7'].items()): print('CC 7 voice %d: ' % k[0] + ' '.join('%d:%+.1f/%+.1f' % x for x in l))
        for k, (s_, p_) in sorted(r['decay'].items()): print('decay voice %d: Saturn %+.2f PS1 %+.2f dB/s' % (k, s_, p_))
    elif a.cmd == 'audio-compare':
        r = audio_compare(a.bank, a.saturn, a.ps1)
        print('tempo factor (recording / nominal): Saturn %.4f  PS1 %.4f; match %.2f / %.2f; PS1 levels aligned on the %s' % (*r['tempo'], *r['match'], r['ref']))
        for gr, n_, s_, p_ in r['groups']: print('ch %2d vel %3d (%3d notes)   Saturn %7.1f dB   PS1 %7.1f dB   PS1 - Saturn %+6.1f' % (*gr, n_, s_, p_, p_ - s_))
    elif a.cmd in ('difftest', 'difftest-m2') and a.boss == 'bonus':
        r = difftest_bonus(a.n, a.kinds.split(',') if a.kinds else ('p4_41', 'p4_40', 'spawn', 'menu', 'anim', 'ill', 'pc', 'st', 'sndplay'), a.frames, uninit=a.uninit)
        res, first, cov = r[:3]
        if a.uninit:
            u = r[3]; print(f"uninit: {u['calls']} runs; stack reads before write: {dict(u['stack_read_before_write']) or 'none'}; "
                            f"reads outside EXE image / overlay / stack: {len(u['outside'])} 16-byte lines")
            for k, v in sorted(u['outside'].items()): print(f'  outside {k:#010x}: {v} reads')
        for k, c in res.items(): print(k, dict(c))
        for k, (seed, r) in first.items(): print('first', k, 'seed', seed, r)
        print(f'coverage: {cov[0]}/{cov[1]} Saturn instructions')
        sys.exit(0 if all(set(c) <= {'ok', 'invalid'} for c in res.values()) else 1)
    elif a.cmd in ('difftest', 'difftest-m2'):
        r = difftest_boss(WOOD_SPEC if a.boss == 'wood' else CUT_SPEC, a.n, a.kinds.split(',') if a.kinds else None, a.frames, uninit=a.uninit)
        res, first, cov = r[:3]
        if a.uninit:
            print(f"uninit: {r[3]['calls']} handler calls; stack reads before write: {dict(r[3]['stack_read_before_write']) or 'none'}; "
                  f"reads outside EXE image/code/stack: {({hex(k): v for k, v in r[3]['outside'].items()}) or 'none'}")
        for k, c in res.items(): print(k, dict(c))
        for k, (seed, r) in first.items(): print('first', k, 'seed', seed, r)
        for k, (c, n) in cov.items(): print(f'coverage {k}: {c}/{n} Saturn instructions')
        sys.exit(0 if all(set(c) <= {'ok', 'invalid'} for c in res.values()) else 1)
    elif a.cmd == 'refs':
        ps = Ps1(); lo, hi = int(a.lo, 16), int(a.hi, 16)
        for n, img in ps1_images(ps, a.where):
            hits = sorted(set((ad, t, m) for ad, t, m in ps1_absrefs(ps, img) if lo <= t < hi))
            for ad, t, m in hits:
                own = n != 'EXE' and img.b <= t < img.b + len(img.d)
                print(f'{n:8s} {ad:08x} -> {t:08x} {m}' + ('   (inside own image)' if own else ''))
    elif a.cmd == 'ending-image':
        ending_song_image(a.ps1, a.out); print('wrote', a.out, '(scratch: play_bgm -> ending song; never distribute)')
    elif a.cmd == 'stack-report':
        fr, sh = lifted_stack_usage()
        for k, v in fr.items(): print(f'C frame {k}: {v} bytes')
        for k, v in sh.items(): print(f'SH-2 stack {k}: {v}  (stk[48] = 192 bytes)')
        for st in a.states:
            print(st); [print(f'  {n:34s} deepest {d:5d} of {size} bytes') for n, d, size in stack_watermark(mdfn_ram(st))]
    elif a.cmd == 'regress':
        cf = [(int(x.split(':')[0], 0), int(x.split(':')[1], 0) if ':' in x else None) for x in a.configs.split(',')]
        regression_runs(a.orig, a.rel, a.out, cf, a.workers)
    elif a.cmd == 'm8-compare':
        m8_report()
    elif a.cmd == 'region-compare':
        region_report(region_compare(a.us, a.jp))
    elif a.cmd == 'regress-compare':
        for r in regression_compare(a.dir):
            print(f"{r['run']}: sound lag {r['lag']:+.2f} s wave corr {r['wave_corr']:.3f} spectral corr {r['spec_corr']:.3f} level {r['level_db']:+.2f} dB")
            print(f"    orig {r['orig']}\n    rel  {r['rel']}")
    elif a.cmd == 'places':
        for r in placements(a.side, a.stage.upper()): print('type %#04x flags %#06x x %#06x y %#06x' % r)

if __name__ == '__main__':
    main()
