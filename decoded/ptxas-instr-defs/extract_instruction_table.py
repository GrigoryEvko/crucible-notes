#!/usr/bin/env python3
"""Parse ptxas PTX-instruction registrations from the decompiled C.

Two registration sites share one constructor body:
  site1: sub_46E000 -> sub_46BED0  (dict at a1+2472)  [STANDARD forms]
  site2: 269 tiny fns -> sub_465030 (dict at a1+2480) [EXTENDED forms]

Each registration is emitted as a block of the shape:
    <tmp> = 0;                          # reset 16-byte attr mask
    <tmp>.m128i_iW[k] = V;              # byte/word/... writes (little-endian)
    *(__intW *)((char *)&<tmp>.m128i_iW[k] + off) = V;   # unaligned writes
    <snap> = _mm_load_si128(&<tmp>);    # snapshot
    sub_46BED0/sub_465030(a1, "<opsig>", (..)"<name>", "<datatype>", <idx>, ...);

The locally-built mask (stored on the stack arg area and read back by the
constructor) is the 16-byte attribute mask.

Full width: the mask is 17 significant bytes held in a 20-byte (dword-aligned)
field. The constructor stores the 16-byte __m128i at descriptor offset +12 and an
extra dword (arg a9) at offset +28 (contiguous: 12 + 16 = 28). Byte 16 of the mask
= the low byte of that dword (descriptor +28 = bits 128..135); bytes 17..19 are
always padding (never written at any call site). The Hex-Rays C models a9 as
`<snap>.m128i_i32[0]`, so byte 16 comes from the call-site machine code, where it
is built as `movb $V,0x40(%rsp); mov 0x40(%rsp),%eax; mov %eax,0x10(%rsp)`. Only
8 STANDARD forms set a nonzero byte 16 (bits 128..131); these are injected below
by (name, datatype_sig) key.
"""
import re, sys, glob, struct

WIDTH = {'i8': 1, 'i16': 2, 'i32': 4, 'i64': 8}

# byte 16 (descriptor +28 = bits 128..135), recovered from the call-site machine
# code of the STANDARD registration site (sub_46E000 -> sub_46BED0). Keyed by
# (name, datatype_sig). Every form not listed has byte 16 == 0 (all 269 EXTENDED
# forms and all but 8 STANDARD forms write `movl $0,0x10(%rsp)`).
BYTE16 = {
    ('tcgen05.wait', ''): 0x01,                                          # bit 128
    ('_tcgen05.guardrails.are_columns_allocated', 'du'): 0x02,           # bit 129
    ('_tcgen05.guardrails.are_columns_allocated', 'duu'): 0x06,          # bits 129,130
    ('_tcgen05.guardrails.in_physical_bounds', 'du'): 0x02,              # bit 129
    ('_tcgen05.guardrails.in_physical_bounds', 'duu'): 0x06,             # bits 129,130
    ('_tcgen05.guardrails.datapath_alignment', 'duuC'): 0x06,            # bits 129,130
    ('cp.async.bulk', 'MMux'): 0x08,                                     # bit 131
    ('cp.async.bulk', 'MMuUx'): 0x08,                                    # bit 131
}

# aligned:   v10.m128i_i16[5] = 1090;
RE_ALIGNED = re.compile(
    r'\.m128i_(i8|i16|i32|i64)\[(\d+)\]\s*=\s*(-?\d+|0x[0-9A-Fa-f]+)\s*;')
# unaligned: *(__int16 *)((char *)&v2297.m128i_i16[5] + 1) = 14340;
RE_UNALIGNED = re.compile(
    r'\*\(__int(8|16|32|64)\s*\*\)\(\(char\s*\*\)&\w+\.m128i_(i8|i16|i32|i64)\[(\d+)\]\s*\+\s*(\d+)\)\s*=\s*(-?\d+|0x[0-9A-Fa-f]+)\s*;')
# reset:     v2297 = 0;   (the BYREF __m128i temp)
# snapshot:  v1181 = _mm_load_si128(&v2297);
RE_SNAP = re.compile(r'=\s*_mm_load_si128\(&(\w+)\)')
# the registration call
RE_CALL = re.compile(
    r'sub_46(?:BED0|5030)\(\s*a1\s*,\s*\(pthread_mutexattr_t\s*\*\)"((?:[^"\\]|\\.)*)"\s*,'
    r'\s*\(__int64\)"((?:[^"\\]|\\.)*)"\s*,\s*"((?:[^"\\]|\\.)*)"\s*,\s*(\d+)\s*,')
# numeric-name variant: a3 is a bare integer token instead of (__int64)"name"
RE_CALL_NUM = re.compile(
    r'sub_46(?:BED0|5030)\(\s*a1\s*,\s*\(pthread_mutexattr_t\s*\*\)"((?:[^"\\]|\\.)*)"\s*,'
    r'\s*(\d+)\s*,\s*"((?:[^"\\]|\\.)*)"\s*,\s*(\d+)\s*,')
# integer name-token -> resolved mnemonic by shared index (filled in main)
TOKEN_NAME = {}

# Some registration calls pass the mnemonic as a bare integer token rather than a
# `(__int64)"name"` string pointer. When such a token shares no index with any
# string-named registration, the index-sharing resolver leaves the row with a raw
# `#<token>` placeholder; the table below supplies the name directly.
#
#   Token 37741882 (index 84, opsig "B[32|64]", datatype "00") is `brev`.
#       The slot sits between bfind(83) and bfe(85) in the bit-op registration
#       cluster (popc/clz/bfind/brev/bfe/bfi/prmt). `brev` is the one bitwise PTX
#       mnemonic with no string-named row; ptxas accepts `brev.b32`/`brev.b64`
#       and emits the native BREV SASS opcode (32-bit reverse; the 64-bit form
#       lowers to two BREV on swapped halves), confirmed on sm_89.
KNOWN_TOKEN_NAME = {
    37741882: "brev",
}

def find_byref_temps(text):
    """Names of the BYREF __m128i temp(s) used to build masks."""
    temps = set()
    for m in re.finditer(r'__m128i\s+(\w+);.*BYREF', text):
        temps.add(m.group(1))
    return temps

def parse_file(path):
    text = open(path).read()
    temps = find_byref_temps(text)
    rows = []
    buf = bytearray(16)        # current mask under construction
    snapshots = {}             # snapvar -> 16-byte bytes
    lines = text.split('\n')
    for ln in lines:
        s = ln.strip()
        # reset of a BYREF temp:  "vNNNN = 0;"
        mreset = re.match(r'(\w+)\s*=\s*0\s*;$', s)
        if mreset and mreset.group(1) in temps:
            buf = bytearray(16)
            continue
        # aligned write into a temp
        if '.m128i_' in s and '_mm_load_si128' not in s and not s.startswith('*('):
            ma = RE_ALIGNED.search(s)
            # ensure it targets a BYREF temp
            lead = s.split('.m128i_', 1)[0]
            tgt = re.search(r'(\w+)$', lead)
            if ma and tgt and tgt.group(1) in temps:
                w = WIDTH[ma.group(1)]; k = int(ma.group(2))
                val = int(ma.group(3), 0)
                off = k * w
                write_le(buf, off, w, val)
                continue
        # unaligned write
        if s.startswith('*('):
            mu = RE_UNALIGNED.search(s)
            if mu:
                vbits = int(mu.group(1)); wfield = WIDTH[mu.group(2)]
                k = int(mu.group(3)); addoff = int(mu.group(4))
                val = int(mu.group(5), 0)
                w = vbits // 8
                off = k * wfield + addoff
                write_le(buf, off, w, val)
                continue
        # snapshot
        msnap = RE_SNAP.search(s)
        if msnap:
            # snapshot var = LHS of assignment
            lhs = s.split('=')[0].strip()
            snapshots[lhs] = bytes(buf)
            continue
        # registration call (string name)
        mc = RE_CALL.search(s)
        if mc:
            opsig, name, dtype, idx = mc.group(1), mc.group(2), mc.group(3), int(mc.group(4))
            rows.append((idx, name, opsig, dtype, full_mask(s, buf, snapshots, name, dtype), 'str'))
            continue
        # registration call (numeric name-token)
        mn = RE_CALL_NUM.search(s)
        if mn:
            opsig, tok, dtype, idx = mn.group(1), int(mn.group(2)), mn.group(3), int(mn.group(4))
            name = TOKEN_NAME.get(tok, f"#{tok}")
            rows.append((idx, name, opsig, dtype, full_mask(s, buf, snapshots, name, dtype), 'tok'))
            continue
    return rows

def pick_mask(s, buf, snapshots):
    for sv, sb in snapshots.items():
        if re.search(r'\b' + re.escape(sv) + r'\b', s):
            return sb
    return bytes(buf)

def full_mask(s, buf, snapshots, name, dtype):
    """16-byte mask (bytes 0..15 from C) + byte 16 (bits 128..135) from BYTE16
    + bytes 17..19 padding -> 17 significant bytes returned (byte 16 included)."""
    lo = pick_mask(s, buf, snapshots)         # 16 bytes
    b16 = BYTE16.get((name, dtype), 0)
    return bytes(lo) + bytes([b16])           # 17 bytes; bytes 17..19 are always 0

def write_le(buf, off, width, val):
    if off < 0 or off + width > 16:
        return
    val &= (1 << (width * 8)) - 1
    for i in range(width):
        buf[off + i] = (val >> (8 * i)) & 0xFF

def main():
    site1 = 'ptxas/decompiled/sub_46E000_0x46e000.c'
    # site2 EXTENDED registration sites: the 269 functions that call sub_465030
    # (excluding the constructor sub_465030 itself). Sorted so the EXT row order
    # is deterministic.
    site2_list = sorted(l.strip() for l in open('/tmp/site2_files.txt') if l.strip())
    files = [('STD', site1)] + [('EXT', f) for f in site2_list]

    # ---- pass 1: collect numeric tokens & their indices, and string names per index ----
    text1 = open(site1).read()
    idx2name = {}
    for m in RE_CALL.finditer(text1):
        idx2name.setdefault(int(m.group(4)), m.group(2))
    # also from site2
    for f in site2_list:
        for m in RE_CALL.finditer(open(f).read()):
            idx2name.setdefault(int(m.group(4)), m.group(2))
    # resolve tokens by shared (STD-site) index
    for m in RE_CALL_NUM.finditer(text1):
        tok = int(m.group(2)); idx = int(m.group(4))
        if idx in idx2name:
            TOKEN_NAME[tok] = idx2name[idx]
    # apply directly-named tokens (e.g. brev) that share no index with any
    # string-named registration.
    TOKEN_NAME.update(KNOWN_TOKEN_NAME)

    # ---- pass 2: full parse ----
    all_rows = []
    counts = {'STD': 0, 'EXT': 0}
    for flags, f in files:
        for idx, name, opsig, dtype, mask, kind in parse_file(f):
            all_rows.append((flags, idx, name, opsig, dtype, mask, kind))
            counts[flags] += 1
    print(f"site1(STD) rows={counts['STD']} site2(EXT) rows={counts['EXT']} total={len(all_rows)}", file=sys.stderr)

    with open('/tmp/instr_table.tsv', 'w') as out:
        out.write("index\tname\toperand_type_signature\tdatatype_sig\tattribute_mask_hex\tflags\tname_kind\n")
        for flags, idx, name, opsig, dtype, mask, kind in all_rows:
            out.write(f"{idx}\t{name}\t{opsig}\t{dtype}\t{mask.hex()}\t{flags}\t{kind}\n")

    names = set(r[2] for r in all_rows if not r[2].startswith('#'))
    toks_unres = set(r[2] for r in all_rows if r[2].startswith('#'))
    nz = sum(1 for r in all_rows if any(r[5]))
    # OR-union of all masks to see which bits are ever used
    union = 0
    for r in all_rows:
        union |= int.from_bytes(r[5], 'little')
    nbits = bin(union).count('1')
    bits = [b for b in range(160) if (union >> b) & 1]
    print(f"unique names(str+resolved)={len(names)} unresolved-token-names={len(toks_unres)}", file=sys.stderr)
    print(f"nonzero masks={nz}; distinct bits set across all masks={nbits}", file=sys.stderr)
    print(f"bits>=128 used: {[b for b in bits if b >= 128]}", file=sys.stderr)
    # 17 significant bytes (byte 16 = bits 128..135) in a 20-byte field
    open('/tmp/union_mask.txt','w').write(
        union.to_bytes(17, 'little').hex() + "\n" + f"{union:0136b}\n")

if __name__ == '__main__':
    main()
