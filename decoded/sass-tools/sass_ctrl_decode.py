#!/usr/bin/env python3
"""
SASS control-word decoder — binary-derived from ptxas-emitted cubins.

Reads `nvdisasm -c -hex <cubin>` output and reconstructs, for each instruction,
the scheduling-control word that ptxas computed. Field bit-positions are the
decoded SASS-table layout (bits 105-124 of the 128-bit instruction word):

    usched_info  = bits 105-109   (5-bit stall/yield enum, 0-27)
    dst_wr_sb    = bits 110-112   (write scoreboard the producer sets; 7=none)
    src_rel_sb   = bits 113-115   (read scoreboard the producer releases; 7=none)
    req_bit_set  = bits 116-121   (6-bit wait mask over SB0..SB5)
    batch_t      = bits 122-124   (co-issue/batch control)
    opex         = batch_t<<5 | usched_info

Verified producer->consumer scoreboard pairing on real ptxas output: a LDG that
sets dst_wr_sb=k is matched by the consuming op carrying bit k in req_bit_set.

usched_info decode (per the SASS-table enum):
    0            -> DRAIN  (yield; stall 0, prefer another warp)
    1..15        -> stall n, end the co-issue group  (Wn_EG)
    17..27       -> stall n-16, no group-end          (Wn)
    16           -> reserved/unused in practice

This is a facts extractor: no NVIDIA table text is reproduced; only the bit
positions (already documented in the wiki) and the decoded per-instruction
control values from compiler output are emitted.
"""
from __future__ import annotations
import re
import subprocess
from dataclasses import dataclass, asdict
from pathlib import Path

NVDISASM = "/usr/local/cuda-13.1/bin/nvdisasm"

_OFFS = re.compile(r"/\*([0-9a-fA-F]{4,})\*/")
_HEX = re.compile(r"/\*\s*(0x[0-9a-fA-F]{16})\s*\*/")
_INSTR = re.compile(r"/\*[0-9a-fA-F]{4,}\*/\s*(.*?);")


def _bits(w: int, hi: int, lo: int) -> int:
    return (w >> lo) & ((1 << (hi - lo + 1)) - 1)


@dataclass
class Ctrl:
    offset: int            # byte offset of the instruction
    text: str              # disassembled mnemonic+operands
    mnem: str              # leading opcode token (e.g. FFMA, LDG)
    usched: int            # 0-27 stall/yield enum
    stall: int             # decoded cycle stall implied by usched
    yield_: bool           # usched==0 (DRAIN)
    end_group: bool        # usched in 1..15
    batch_t: int           # 0..7 co-issue control
    dst_wr: int            # write scoreboard set (7=none)
    src_rel: int           # read scoreboard released (7=none)
    wait_mask: int         # 6-bit req_bit_set
    reuse: bool            # operand-reuse flag present in text (.reuse)
    opex: int              # batch_t<<5 | usched
    pred: str              # predicate guard (e.g. "@P0", "@!P0") or ""
    lo64: int
    hi64: int

    @property
    def wait_sbs(self) -> list[int]:
        return [b for b in range(6) if (self.wait_mask >> b) & 1]


def usched_to_stall(usched: int) -> tuple[int, bool, bool]:
    """Return (stall_cycles, is_yield, ends_group)."""
    if usched == 0:
        return 0, True, False
    if 1 <= usched <= 15:
        return usched, False, True
    if 17 <= usched <= 27:
        return usched - 16, False, False
    return usched, False, False  # 16/reserved: treat raw


def decode_word(offset: int, text: str, lo64: int, hi64: int) -> Ctrl:
    w = lo64 | (hi64 << 64)
    usched = _bits(w, 109, 105)
    dst_wr = _bits(w, 112, 110)
    src_rel = _bits(w, 115, 113)
    wait = _bits(w, 121, 116)
    batch = _bits(w, 124, 122)
    stall, yld, eg = usched_to_stall(usched)
    mnem = text.strip().split()[0] if text.strip() else ""
    # strip a leading predicate guard like @P0 or @!P0
    toks = text.strip().split()
    pred = ""
    if toks and toks[0].startswith("@"):
        pred = toks[0]
        mnem = toks[1] if len(toks) > 1 else toks[0]
    else:
        mnem = toks[0] if toks else ""
    reuse = ".reuse" in text
    return Ctrl(offset=offset, text=text.strip(), mnem=mnem, usched=usched,
                stall=stall, yield_=yld, end_group=eg, batch_t=batch,
                dst_wr=dst_wr, src_rel=src_rel, wait_mask=wait, reuse=reuse,
                opex=(batch << 5) | usched, pred=pred, lo64=lo64, hi64=hi64)


def parse_disasm(text: str) -> list[Ctrl]:
    """Parse `nvdisasm -c -hex` output into a list of decoded Ctrl records."""
    out: list[Ctrl] = []
    lines = text.splitlines()
    i = 0
    pending = None  # (offset, instr_text, lo64)
    for ln in lines:
        # instruction line carries offset, text, and the FIRST (lo64) hex word
        m_off = _OFFS.search(ln)
        hexes = _HEX.findall(ln)
        if m_off and "/*" in ln and ";" in ln and hexes:
            mi = _INSTR.search(ln)
            instr_text = mi.group(1).strip() if mi else ""
            # skip non-instruction directives
            off = int(m_off.group(1), 16)
            lo64 = int(hexes[-1], 16)
            pending = (off, instr_text, lo64)
            continue
        # the continuation line carries the hi64 word alone
        if pending is not None and hexes and ";" not in ln and "/*" in ln:
            # ensure this line has ONLY a hex (the hi64), no offset
            if not _OFFS.search(ln) or ".byte" in ln:
                off, instr_text, lo64 = pending
                hi64 = int(hexes[-1], 16)
                out.append(decode_word(off, instr_text, lo64, hi64))
                pending = None
    return out


def disasm_cubin(cubin: str) -> list[Ctrl]:
    r = subprocess.run([NVDISASM, "-c", "-hex", cubin],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"nvdisasm failed: {r.stderr[:400]}")
    return parse_disasm(r.stdout)


if __name__ == "__main__":
    import sys, json
    recs = disasm_cubin(sys.argv[1])
    for c in recs:
        print(f"/*{c.offset:04x}*/ {c.mnem:14s} usched={c.usched:2d} "
              f"stall={c.stall:2d} bt={c.batch_t} wr={c.dst_wr} rd={c.src_rel} "
              f"wait={c.wait_sbs} reuse={int(c.reuse)}  | {c.text}")
