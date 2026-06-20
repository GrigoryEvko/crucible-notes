#!/usr/bin/env python3
"""De-obfuscate ptxas's numeric pseudo-instruction handler names.

ptxas registers PTX pseudo-instruction expansion handlers in two dictionaries
(see sub_5D4190 in CUDA 13.0.88): one keyed by human names (membar, div, cvt,
wmma.*, tcgen05.*, ...), and one keyed by *decimal-number* strings such as
"1030557441". Those numbers are not encrypted and are not operand-type codes —
each is the **Adler-32 hash of a `__cuda_*` macro-builtin name**, rendered as a
decimal string. NVIDIA's release build strips the symbolic macro name and keeps
only adler32(name) as the registry key.

This script recovers the original name for each numeric key by harvesting the
`__cuda_*` identifiers present in the binary (the decoded macro-expansion pool
and the string tables) and matching adler32(name) against the keys.

Usage:
    python3 decode_pseudo_names.py <numeric_key>...
    python3 decode_pseudo_names.py --all < keys.txt
"""
import re, sys, zlib

POOL = "../ptxas-ptx-macro-pool/ptxas_ptx_macro_pool.decoded.bin"

def harvest_names(*paths):
    names = set()
    for p in paths:
        try:
            blob = open(p, "rb").read().decode("latin-1")
        except OSError:
            continue
        names |= set(re.findall(r"__cuda_[A-Za-z0-9_]{2,90}", blob))
    return names

def build_map(names):
    m = {}
    for n in names:
        m.setdefault(zlib.adler32(n.encode("latin-1")) & 0xFFFFFFFF, n)
    return m

def main(argv):
    names = harvest_names(POOL, "../../ptxas/ptxas_strings.json",
                          "../../ptxas/ptxas_names.json")
    table = build_map(names)
    keys = []
    if argv and argv[0] == "--all":
        keys = [int(x) for x in sys.stdin.read().split()]
    else:
        keys = [int(x) for x in argv]
    for k in keys:
        print(f"{k}\t0x{k:08X}\t{table.get(k & 0xFFFFFFFF, '(unresolved)')}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
