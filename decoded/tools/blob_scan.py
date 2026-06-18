#!/usr/bin/env python3
"""
blob_scan.py — generic detector for obfuscated/encrypted data blobs in ELF binaries.

Scans data-class sections (.rodata, .data, .data.rel.ro, custom PROGBITS) of an ELF,
computes a sliding-window Shannon byte-entropy map, and flags contiguous high-entropy
regions that look like ciphertext rather than known-compressed data / GOT-reloc noise.

Usage: blob_scan.py <elf_path> [--win 4096] [--thresh 7.2] [--min 2048]
"""
import sys, struct, math, argparse
from collections import Counter

DATA_SECTIONS = {'.rodata', '.data', '.data.rel.ro', '.rodata.cst', '.data.rel'}
# compression magics (skip these — genuine compressed data, not concealed cipher)
COMP_MAGICS = [
    (b'\x28\xb5\x2f\xfd', 'zstd'),
    (b'\x1f\x8b', 'gzip'),
    (b'\x78\x01', 'zlib-low'), (b'\x78\x9c', 'zlib-def'), (b'\x78\xda', 'zlib-best'),
    (b'BZh', 'bzip2'),
    (b'\xfd7zXZ', 'xz'),
    (b'\x04\x22\x4d\x18', 'lz4'),
    (b'PK\x03\x04', 'zip'),
]

def shannon(buf):
    if not buf:
        return 0.0
    c = Counter(buf)
    n = len(buf)
    h = 0.0
    for v in c.values():
        p = v / n
        h -= p * math.log2(p)
    return h

def parse_elf_sections(path):
    """Minimal ELF64 section parser. Returns list of dicts and (e_type)."""
    with open(path, 'rb') as f:
        data = f.read()
    assert data[:4] == b'\x7fELF', 'not ELF'
    ei_class = data[4]
    assert ei_class == 2, 'only ELF64 supported'
    le = data[5] == 1
    en = '<' if le else '>'
    e_type, = struct.unpack_from(en+'H', data, 16)
    e_shoff, = struct.unpack_from(en+'Q', data, 40)
    e_shentsize, = struct.unpack_from(en+'H', data, 58)
    e_shnum, = struct.unpack_from(en+'H', data, 60)
    e_shstrndx, = struct.unpack_from(en+'H', data, 62)
    secs = []
    for i in range(e_shnum):
        off = e_shoff + i*e_shentsize
        name_off, s_type, s_flags, s_addr, s_offset, s_size, s_link, s_info, s_align, s_entsize = \
            struct.unpack_from(en+'IIQQQQIIQQ', data, off)
        secs.append(dict(name_off=name_off, type=s_type, flags=s_flags, addr=s_addr,
                         offset=s_offset, size=s_size, entsize=s_entsize))
    # resolve names from shstrtab
    sh = secs[e_shstrndx]
    strtab = data[sh['offset']:sh['offset']+sh['size']]
    for s in secs:
        end = strtab.find(b'\x00', s['name_off'])
        s['name'] = strtab[s['name_off']:end].decode('latin1')
    return data, e_type, secs

def is_comp_magic(buf):
    for magic, name in COMP_MAGICS:
        if buf.startswith(magic):
            return name
    return None

def looks_like_record_table(buf):
    """Heuristic: dense (small_count, file_offset) record table — e.g. nvdisasm SASS
    encoding tables. High byte-entropy but structured: many 8-byte values where the
    high 5 bytes are zero (small ints / 24-bit offsets). Returns fraction structured."""
    if len(buf) < 64:
        return 0.0
    n = len(buf) // 8
    structured = 0
    for i in range(min(n, 1024)):
        v = struct.unpack_from('<Q', buf, i*8)[0]
        if (v >> 24) == 0:  # fits in 24 bits -> small count or local offset
            structured += 1
    return structured / min(n, 1024)

def looks_like_pointers(buf):
    """Heuristic: GOT/reloc noise — many 8-byte little-endian values in a code/data VMA
    range (high bytes 0x00 or 0x55/0x7f). Returns fraction that look like pointers."""
    if len(buf) < 64:
        return 0.0
    n = len(buf) // 8
    ptrish = 0
    for i in range(n):
        v = struct.unpack_from('<Q', buf, i*8)[0]
        hi = v >> 40
        if hi == 0 or v == 0:
            ptrish += 1
    return ptrish / max(n, 1)

def scan(path, win=4096, thresh=7.2, minsize=2048, step=None):
    data, e_type, secs = parse_elf_sections(path)
    if step is None:
        step = win
    candidates = []
    for s in secs:
        nm = s['name']
        if s['type'] != 1:  # PROGBITS only
            continue
        if nm in ('.text', '.init', '.plt', '.fini', '.eh_frame', '.eh_frame_hdr',
                  '.gcc_except_table', '.comment', '.note.ABI-tag', '.interp'):
            continue
        # only data/rodata-class
        if not (nm in DATA_SECTIONS or nm.startswith('.rodata') or nm.startswith('.data')
                or nm.startswith('.nv') or 'crypt' in nm.lower() or nm.startswith('.gnu')):
            continue
        sdata = data[s['offset']:s['offset']+s['size']]
        if not sdata:
            continue
        # sliding window entropy
        runs = []  # (start_in_sec, end_in_sec, mean_entropy)
        cur_start = None
        ent_acc = []
        i = 0
        while i < len(sdata):
            w = sdata[i:i+win]
            h = shannon(w)
            if h >= thresh:
                if cur_start is None:
                    cur_start = i
                    ent_acc = []
                ent_acc.append(h)
            else:
                if cur_start is not None:
                    runs.append((cur_start, i, sum(ent_acc)/len(ent_acc)))
                    cur_start = None
            i += step
        if cur_start is not None:
            runs.append((cur_start, len(sdata), sum(ent_acc)/len(ent_acc)))
        for (rs, re, mh) in runs:
            rsize = re - rs
            if rsize < minsize:
                continue
            blob = sdata[rs:re]
            comp = is_comp_magic(blob)
            ptr_frac = looks_like_pointers(blob[:8192])
            rec_frac = looks_like_record_table(blob[:8192])
            if comp:
                cls = f'compressed:{comp}'
            elif ptr_frac > 0.45:
                cls = 'GOT/ptr-noise'
            elif rec_frac > 0.40:
                cls = 'structured-table(not-cipher)'
            else:
                cls = 'CIPHER-candidate'
            candidates.append(dict(
                section=nm, file_off=s['offset']+rs, vma=s['addr']+rs,
                size=rsize, entropy=round(mh, 4), ptr_frac=round(ptr_frac, 3),
                rec_frac=round(rec_frac, 3), cls=cls))
    return e_type, candidates

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('elf')
    ap.add_argument('--win', type=int, default=4096)
    ap.add_argument('--thresh', type=float, default=7.2)
    ap.add_argument('--min', type=int, default=2048, dest='minsize')
    ap.add_argument('--step', type=int, default=None)
    args = ap.parse_args()
    e_type, cands = scan(args.elf, args.win, args.thresh, args.minsize, args.step)
    print(f"# {args.elf}  e_type={e_type}")
    print(f"{'section':<16}{'file_off':>12}{'vma':>14}{'size':>12}{'entropy':>9}{'ptrf':>7}{'recf':>7}  class")
    cands.sort(key=lambda c: -c['size'])
    for c in cands:
        print(f"{c['section']:<16}{c['file_off']:>#12x}{c['vma']:>#14x}{c['size']:>12}"
              f"{c['entropy']:>9}{c['ptr_frac']:>7}{c['rec_frac']:>7}  {c['cls']}")
    if not cands:
        print("# no high-entropy data-section candidates")

if __name__ == '__main__':
    main()
