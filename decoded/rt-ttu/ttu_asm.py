#!/usr/bin/env python3
"""TTU (RT core) instruction encoder.

Assembles the seven TTU SASS instructions to their 128-bit encodings from the
field map decoded for sm_75..sm_89. Field positions (absolute bit indices in the
128-bit instruction, low word = bits[63:0], high word = bits[127:64]):

    Pg          [14:12]      guard predicate (7 = PT)
    opcode      [91, 11:0]   13-bit opcode; all TTU ops have bit91 = 0
    ttuAddr     [55:40]      ImmU16, byte offset into the TTU state file (TTUST/TTULD)
    Rb          [39:32]      source register (TTUST)
    Rc          [71:64]      source reg (TTUST) / Rd2 hi-result (TTULD)
    Rd          [23:16]      dest register (TTULD)
    Pu          [83:81]      hit/miss predicate (TTULD)
    dual        [72]         TTUOPEN /DUAL
    close       [74]         TTULD /CLOSE  (TTUCLOSE)
    ivall       [80]         TTUCCTL /IVALLONLY
    req_bit_set [121:116]    request scoreboard mask (TTUOPEN/TTUCCTL)
    src_rel_sb  [115:113]    read scoreboard (released)
    dst_wr_sb   [112:110]    write scoreboard (armed by TTULD)
    pm_pred     [103:102]
    opex        [124:122, 109:105]   scheduling (batch_t, usched_info)

opcodes: TTUOPEN 0x3d0, TTUST 0x3d1, TTULD/TTUCLOSE 0x3d2, TTUGO 0x3d3,
TTUCCTL 0x3d5, TTUMACROFUSE 0x9d4.

nvdisasm recognizes the TTU opclasses (ttuopen_/ttust_/ttuld_/ttugo_/ttucctl_)
but does not render the instructions, so `--selfcheck` verifies each opcode via
the opclass-recognition path (an out-of-range opex makes nvdisasm name the
opclass it matched).
"""
from __future__ import annotations

import struct
import subprocess
import sys
import tempfile
from pathlib import Path

NVDISASM = "/usr/local/cuda-13.1/bin/nvdisasm"

OPCODES = {
    "TTUOPEN": 0x3d0, "TTUST": 0x3d1, "TTULD": 0x3d2, "TTUCLOSE": 0x3d2,
    "TTUGO": 0x3d3, "TTUCCTL": 0x3d5, "TTUMACROFUSE": 0x9d4,
}
OPCLASS = {
    0x3d0: "ttuopen_", 0x3d1: "ttust_", 0x3d2: "ttuld_",
    0x3d3: "ttugo_", 0x3d5: "ttucctl_", 0x9d4: "ttumacrofuse_",
}


def _set(word: int, value: int, hi: int, lo: int) -> int:
    mask = (1 << (hi - lo + 1)) - 1
    return word | ((value & mask) << lo)


def encode(opcode: int, *, opex: int, pg: int = 7, ttu_addr: int = 0,
           rb: int = 0, rc: int = 0, rd: int = 0, pu: int = 0,
           req: int = 0, src_rel_sb: int = 7, dst_wr_sb: int = 7,
           dual: int = 0, close: int = 0, ivall: int = 0) -> bytes:
    """Encode one TTU instruction to its 16 little-endian bytes."""
    w = _set(0, pg, 14, 12)
    w = _set(w, opcode & 0xFFF, 11, 0)
    w = _set(w, (opcode >> 12) & 1, 91, 91)
    w = _set(w, ttu_addr, 55, 40)
    w = _set(w, rb, 39, 32)
    w = _set(w, rc, 71, 64)
    w = _set(w, rd, 23, 16)
    w = _set(w, pu, 83, 81)
    w = _set(w, dual, 72, 72)
    w = _set(w, close, 74, 74)
    w = _set(w, ivall, 80, 80)
    w = _set(w, req, 121, 116)
    w = _set(w, src_rel_sb, 115, 113)
    w = _set(w, dst_wr_sb, 112, 110)
    w = _set(w, (opex >> 5) & 7, 124, 122)
    w = _set(w, opex & 0x1F, 109, 105)
    return struct.pack("<QQ", w & (2**64 - 1), (w >> 64) & (2**64 - 1))


# Named constructors (opex is the scheduling field; 0x11 = stall-1 non-yield).
def ttust(ttu_addr: int, rb: int, rc: int, opex: int = 0x11) -> bytes:
    return encode(OPCODES["TTUST"], opex=opex, ttu_addr=ttu_addr, rb=rb, rc=rc)


def ttuld(ttu_addr: int, rd: int, rd2: int, pu: int = 0, close: int = 0,
          opex: int = 0x11) -> bytes:
    return encode(OPCODES["TTULD"], opex=opex, ttu_addr=ttu_addr, rd=rd, rc=rd2,
                  pu=pu, close=close)


def ttuopen(dual: int = 1, req: int = 0, opex: int = 0x11) -> bytes:
    return encode(OPCODES["TTUOPEN"], opex=opex, dual=dual, req=req)


def ttugo(opex: int = 0x11) -> bytes:
    return encode(OPCODES["TTUGO"], opex=opex)


def ttucctl(opex: int = 0x11) -> bytes:
    return encode(OPCODES["TTUCCTL"], opex=opex, ivall=1)


def _opclass_of(raw16: bytes) -> str:
    """Return the opclass nvdisasm matches for a TTU encoding (via opex=0)."""
    with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as f:
        f.write(raw16)
        path = f.name
    out = subprocess.run([NVDISASM, "--binary", "SM89", path],
                         capture_output=True, text=True)
    Path(path).unlink()
    text = out.stdout + out.stderr
    for line in text.splitlines():
        if "Opclass" in line and "'" in line:
            return line.split("'", 2)[1]
    return ""


# Ops whose opex field uses TABLES_opex_10: an out-of-range opex (0) makes
# nvdisasm name the matched opclass. The TABLES_opex_0 ops accept opex 0, so this
# path cannot trigger their name; they encode from the same field map.
_OPEX10 = {0x3d1, 0x3d2, 0x3d3}


def selfcheck() -> int:
    """Verify the opex_10 opcodes are recognized by nvdisasm as the expected
    opclass. The opex_0 opcodes are not separately checkable via this path."""
    ok = True
    for op, opcode in OPCODES.items():
        if op == "TTUCLOSE":
            continue
        if opcode not in _OPEX10:
            print(f"  {op:13s} 0x{opcode:03x} -> opex_0 op (not opclass-checkable via opex=0)")
            continue
        cls = _opclass_of(encode(opcode, opex=0))
        good = cls.startswith(OPCLASS[opcode].rstrip("_"))
        print(f"  {op:13s} 0x{opcode:03x} -> opclass {cls or '(none)':16s} {'OK' if good else 'MISMATCH'}")
        ok = ok and good
    print("SELFCHECK OK" if ok else "SELFCHECK FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(selfcheck() if "--selfcheck" in sys.argv else 0)
