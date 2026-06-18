#!/usr/bin/env python3
"""
nvdisasm SASS-ISA decode-table extractor (CUDA 13.1, nvdisasm V13.1.115).

The 3.68 MB blob at .data VMA 0x343000 (file off 0x143000) is a pack of
per-architecture SASS ISA description tables.  Each table is:

    on-disk bytes  --(stream cipher, fn 0x87620)-->  LZ4 block  --(LZ4)-->  plaintext

Cipher (fn 0x87620, per byte i over the table, MSB-first LCG keystream):
    M = 0x41C64E6D, INC = 0x3039           (the well-known glibc/MS LCG constants)
    state = key                            # 32-bit, key from descriptor [rbp+0x10]
    al    = (~key) & 0xFF                   # chaining register, init = bitwise-not(key)
    cnt   = 0                               # keystream-byte counter
    for each cipher byte c:
        if cnt == 0:                        # consume next 32-bit LCG word, MSB first
            state = (state*M + INC) & 0xFFFFFFFF
            ks    = state
            cnt   = 4
        else:
            ks >>= 8
        cnt -= 1
        plain = SBOX[(al ^ c) & 0xFF] ^ (ks & 0xFF)
        al    = c                           # chain on the *cipher* byte
        emit plain
    # plaintext is then LZ4-block-decompressed.

SBOX: 256-byte permutation at .rodata file offset 0xF4680.
The encrypt twin (fn 0x87590) uses a different SBOX at 0xF4780.

Descriptor table (one per architecture), captured at runtime:
    {blob_offset, comp_size, key}
"""
import struct
import lz4.block

NVDISASM = "/usr/local/cuda-13.1/bin/nvdisasm"
BLOB     = "/tmp/rp/blob_disk.bin"          # .data+0x2000 .. end (file off 0x143000)
SBOX_OFF = 0xF4680                           # .rodata file offset (VMA==fileoff here)
M, INC   = 0x41C64E6D, 0x3039

# arch -> (blob_offset, comp_size, key)  [recovered via descriptor capture]
TABLES = {
    "SM75":  (0xdde20,  285764, 0x0b4a),
    "SM80":  (0x1b0ba0, 323934, 0x8416),
    "SM86":  (0x2ccfe0, 338988, 0x8416),
    "SM87":  (0x2ccfe0, 338988, 0x8416),
    "SM88":  (0x2ccfe0, 338988, 0x8416),
    "SM89":  (0x206cc0, 357485, 0x1684),
    "SM90":  (0x6b460,  447245, 0x3927),
    "SM90a": (0x6b460,  447245, 0x3927),
    "SM100": (0x2673a0, 388538, 0x9327),
    "SM103": (0x92a0,   366845, 0x9327),
    "SM110": (0x328c60, 368416, 0x9327),
    "SM120": (0x12cf00, 512673, 0x9327),
    "SM121": (0x12cf00, 512673, 0x9327),
}


def load_sbox(path=NVDISASM):
    with open(path, "rb") as f:
        f.seek(SBOX_OFF)
        return f.read(256)


def decipher(buf, key, sbox):
    """Invert fn 0x87620: stream cipher -> LZ4 block bytes."""
    state = key & 0xFFFFFFFF
    al = (~key) & 0xFF
    ks = 0
    cnt = 0
    out = bytearray(len(buf))
    for i, c in enumerate(buf):
        if cnt == 0:
            state = (state * M + INC) & 0xFFFFFFFF
            ks = state
            cnt = 4
        else:
            ks >>= 8
        cnt -= 1
        out[i] = sbox[(al ^ c) & 0xFF] ^ (ks & 0xFF)
        al = c
    return bytes(out)


def decode_table(blob, off, comp_size, key, sbox, dst_cap=16 << 20):
    cipher = blob[off:off + comp_size]
    lz4_block = decipher(cipher, key, sbox)
    plain = lz4.block.decompress(lz4_block, uncompressed_size=dst_cap)
    return lz4_block, plain


def main():
    sbox = load_sbox()
    blob = open(BLOB, "rb").read()
    assert sorted(sbox) == list(range(256)), "SBOX not a permutation"
    total_plain = 0
    for arch, (off, csz, key) in TABLES.items():
        lz4b, plain = decode_table(blob, off, csz, key, sbox)
        head = plain[:40].split(b"\n", 1)[0].decode("latin1")
        out = f"/tmp/rp/table_{arch}.txt"
        open(out, "wb").write(plain)
        total_plain += len(plain)
        print(f"{arch:6s} off={off:#08x} comp={csz:7d} -> plain={len(plain):8d}  '{head}'")
    print(f"\nTotal plaintext decoded: {total_plain} bytes across {len(TABLES)} tables")


if __name__ == "__main__":
    main()
