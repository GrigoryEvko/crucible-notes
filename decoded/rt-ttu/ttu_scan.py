#!/usr/bin/env python3
"""Scan a cubin (or an OptiX cache blob) for TTU instructions and decode them.

Each 128-bit SASS instruction is read little-endian; the opcode field is
`{bit91, bits[11:0]}`. TTU opcodes: 0x3d0 TTUOPEN, 0x3d1 TTUST, 0x3d2 TTULD/CLOSE,
0x3d3 TTUGO, 0x3d5 TTUCCTL, 0x9d4 TTUMACROFUSE. Operand fields (absolute bit
positions): ttuAddr[55:40], Rb[39:32], Rc[71:64], Rd[23:16], Pu[83:81], Pg[14:12],
dual[72], close[74], wr_sb[112:110], rd_sb[115:113], req[121:116].

The retail nvdisasm does not render TTU operands, so this decodes the raw words
directly. Usage: ttu_scan.py <cubin|blob> ...
"""
import struct
import sys

TTU = {0x3d0: "TTUOPEN", 0x3d1: "TTUST", 0x3d2: "TTULD/CLOSE",
       0x3d3: "TTUGO", 0x3d5: "TTUCCTL", 0x9d4: "TTUMACROFUSE"}


def _f(v: int, hi: int, lo: int) -> int:
    return (v >> lo) & ((1 << (hi - lo + 1)) - 1)


def scan_bytes(b: bytes):
    """Yield (byte_offset, mnem, decoded_fields) for every TTU instruction."""
    for j in range(0, len(b) - 15, 16):
        v = int.from_bytes(b[j:j + 16], "little")
        op = _f(v, 11, 0) | (_f(v, 91, 91) << 12)
        if op in TTU:
            yield j, TTU[op], dict(
                ttu=_f(v, 55, 40), Rb=_f(v, 39, 32), Rc=_f(v, 71, 64),
                Rd=_f(v, 23, 16), Pu=_f(v, 83, 81), Pg=_f(v, 14, 12),
                dual=_f(v, 72, 72), close=_f(v, 74, 74),
                wr=_f(v, 112, 110), rd_sb=_f(v, 115, 113), req=_f(v, 121, 116))


def _text_sections(data: bytes):
    """Yield (name, bytes) for each .text.* PROGBITS section of an ELF cubin."""
    shoff = struct.unpack_from("<Q", data, 0x28)[0]
    es = struct.unpack_from("<H", data, 0x3a)[0]
    n = struct.unpack_from("<H", data, 0x3c)[0]
    sx = struct.unpack_from("<H", data, 0x3e)[0]
    sh = lambda i: struct.unpack_from("<IIQQQQ", data, shoff + i * es)
    stab = sh(sx)[4]
    nm = lambda x: data[stab + x:data.index(b"\0", stab + x)].decode()
    for i in range(n):
        name, typ, _, _, off, size = sh(i)
        if typ == 1 and nm(name).startswith(".text."):
            yield nm(name), data[off:off + size]


def main(argv):
    for path in argv:
        data = open(path, "rb").read()
        total = 0
        try:
            sections = list(_text_sections(data))
        except Exception:
            sections = [(path, data)]   # raw blob, not an ELF
        for name, b in sections:
            hits = list(scan_bytes(b))
            if not hits:
                continue
            total += len(hits)
            print(f"\n{path}:{name}  ({len(hits)} TTU)")
            for off, mnem, d in hits:
                print(f"  +0x{off:05x} {mnem:13s} ttu=0x{d['ttu']:04x} "
                      f"Rb=R{d['Rb']} Rc=R{d['Rc']} Rd=R{d['Rd']} Pu=P{d['Pu']} "
                      f"@P{d['Pg']} dual={d['dual']} close={d['close']} "
                      f"wr={d['wr']} req=0x{d['req']:x}")
        print(f"{path}: {total} TTU instruction(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
