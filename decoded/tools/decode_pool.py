#!/usr/bin/env python3
"""Reproduce ptxas PTX macro/template string pool from the on-disk encrypted blob.

Cipher (recovered from sub_430710 @ 0x430710, init sub_4305D0 @ 0x4305d0):
  state: seed (LCG), v3 (current 32-bit LCG word, consumed byte-by-byte),
         v4 (#bytes left in v3), prev (previous CIPHERTEXT byte, feedback).
  init for key K=0x5389A4F8: seed=K, v3=0, v4=1, prev=(~K)&0xFF.
  per ciphertext byte c:
     v4 -= 1
     if v4==0: seed = (1103515245*seed + 12345) & 0xFFFFFFFF; v3 = seed; v4 = 4
     else:     v3 >>= 8
     p = v3 & 0xFF                         # current LCG keystream byte
     s = SBOX[c ^ prev]                    # substitution on (cipher XOR prev plaintext-feedback byte)
     prev = c                              # feedback uses the CIPHERTEXT byte
     plaintext = p ^ s
"""
import os, sys, struct

PTXAS_130 = "/home/grigory/iprit/nvopen-tools/ptxas/ptxas"
OUT_DEFAULT = "/tmp/rp/decoded_pool.bin"
KEY = 0x5389A4F8
RODATA_DELTA = 0x400000           # VMA - file_offset for .rodata in 13.0
SBOX_VMA  = 0x1CE3340
BLOB_VMA  = 0x1D50340
POOL_LEN  = 0x1C46B8              # malloc size observed at runtime (13.0)

def decode(blob, sbox, key=KEY):
    seed = key & 0xFFFFFFFF
    v3 = 0
    v4 = 1
    prev = (~key) & 0xFF
    out = bytearray(len(blob))
    for i, c in enumerate(blob):
        v4 -= 1
        if v4 == 0:
            seed = (1103515245 * seed + 12345) & 0xFFFFFFFF
            v3 = seed
            v4 = 4
        else:
            v3 >>= 8
        p = v3 & 0xFF
        s = sbox[(c ^ prev) & 0xFF]
        prev = c
        out[i] = p ^ s
    return bytes(out)

def main():
    binpath = sys.argv[1] if len(sys.argv) > 1 else PTXAS_130
    outpath = sys.argv[2] if len(sys.argv) > 2 else OUT_DEFAULT
    data = open(binpath, "rb").read()
    sbox_fo = SBOX_VMA - RODATA_DELTA
    blob_fo = BLOB_VMA - RODATA_DELTA
    sbox = data[sbox_fo:sbox_fo + 256]
    blob = data[blob_fo:blob_fo + POOL_LEN]
    pool = decode(blob, sbox)
    os.makedirs(os.path.dirname(outpath) or ".", exist_ok=True)
    open(outpath, "wb").write(pool)
    print(f"decoded {len(pool)} bytes -> {outpath}")
    print("decoded[:48] =", pool[:48])

    # verify against the live-dumped pool (13.0)
    try:
        live = open("/tmp/rp/livepool.bin", "rb").read()
        n = min(len(live), len(pool))
        eq = sum(1 for a, b in zip(pool[:n], live[:n]) if a == b)
        print(f"vs livepool.bin: {eq}/{n} bytes equal = {100*eq/n:.3f}%")
    except FileNotFoundError:
        pass

if __name__ == "__main__":
    main()
