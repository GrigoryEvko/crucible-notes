#!/usr/bin/env python3
"""Extract per-arch SASS-encoding dispatch tables from the NVIDIA ptxas binary.

Static reverse-engineering, interoperability research (DMCA 1201(f)) -- facts
extracted from the stripped binary only, no NVIDIA source consulted.

The ptxas per-arch SASS-encoding dispatch region lives in .rodata between
VMA 0x22E7AD0 and 0x23EFB60.  It is a packed sequence of SEVEN arch "encoding
blocks".  Each block begins at a "lead" main table whose first 16 bytes are a
header:  qword0 = 0x200 (capacity), qword1 = the block's lead/default .text
handler.

The dispatch payload is a packed run of 24-byte (3-qword) slots:

    [ key_word , handler_ptr , pad ]
      key_word    = (format_id << 8) | minor_opcode   (< 0x10000)
      handler_ptr = a .text VMA  (0x403520 .. 0x1CE2DE2)
      pad         = 0, or another .text pointer

Sub-tables inside a block are separated by 16-byte zero/header gaps that are
NOT 24-byte aligned, so the walk is 8-byte granular: on a valid slot advance 24
bytes; on a miss advance 8 bytes and count it as a non-slot group.  The walk
stops at the next block's lead VMA or after a run of >8 consecutive non-slot
groups (block boundary).  Re-syncing by 8 bytes is phantom-safe: the qword that
follows a real handler is itself a .text pointer (>= 0x403520, so it fails the
"key < 0x10000" test) and the qword after a pad is the next key (< 0x10000 but
not a .text pointer, so it fails the handler test) -- only true slot starts
validate.

Usage:  python3 extract_arch_blocks.py /path/to/ptxas
Reads .rodata via objcopy, or the pre-dumped /tmp/ptxas_work/rodata.bin if it
already exists.  Writes rows to /tmp/ptxas_work/arch_blocks.tsv and prints a
SUMMARY to stdout.
"""

from __future__ import annotations

import struct
import subprocess
import sys
import tempfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants -- all from prior static analysis of ptxas 13.0.88.
# ---------------------------------------------------------------------------
RODATA_VMA = 0x1CE2E00          # .rodata virtual address
RODATA_FILE_OFF = 0x18E2E00     # .rodata file offset (delta 0x400000)

TEXT_LO = 0x403520              # lowest observed .text handler VMA
TEXT_HI = 0x1CE2DE2            # highest observed .text handler VMA

KEY_MAX = 0x10000               # key_word upper bound (exclusive)

SLOT_SIZE = 24                  # bytes per dispatch slot (3 qwords)
RESYNC_STEP = 8                 # advance on a non-slot position
MAX_NON_SLOT_GROUPS = 8         # >8 consecutive non-slot groups => block end

PRE_DUMPED = Path("/tmp/ptxas_work/rodata.bin")
OUT_TSV = Path("/tmp/ptxas_work/arch_blocks.tsv")

# Block-lead main-table VMAs and the stop VMA for each block's walk.
# (stop = next block's lead, except block7 which stops at the region end.)
BLOCK_LEADS: list[int] = [
    0x22E7AD0,  # block1  lead handler 0xEA7440  (classic: sm50_7x)
    0x2348FB0,  # block2  lead handler 0xEA7440  (classic: sm75)
    0x236CD10,  # block3  lead handler 0xEA7440  (classic: sm100)
    0x238C9B0,  # block4  lead handler 0xEA7440  (classic: sm80_8x)
    0x23A8090,  # block5  lead handler 0xEA7440  (classic: sm86_89)
    0x23BA8A0,  # block6  lead handler 0x18F0540 (NEW: lean ~71KB emitter -> sm_110 Jetson Thor)
    0x23DB120,  # block7  lead handler 0x1AEE1D0 (NEW: ~1MB emitter -> sm_120/121 consumer Blackwell)
]
BLOCK7_END = 0x23EFB60          # explicit stop for the final block


def load_rodata(binary_path: str) -> bytes:
    """Return the raw .rodata bytes, preferring the pre-dumped copy.

    Falls back to invoking objcopy on the supplied binary if the pre-dumped
    /tmp/ptxas_work/rodata.bin is absent.
    """
    if PRE_DUMPED.is_file():
        return PRE_DUMPED.read_bytes()

    with tempfile.NamedTemporaryFile(suffix=".bin", delete=True) as tmp:
        subprocess.run(
            ["objcopy", "-O", "binary", "--only-section=.rodata",
             binary_path, tmp.name],
            check=True,
        )
        return Path(tmp.name).read_bytes()


class Rodata:
    """Random-access view of .rodata addressed by virtual address."""

    __slots__ = ("_data",)

    def __init__(self, data: bytes) -> None:
        self._data = data

    def qword(self, va: int) -> int:
        """Read a little-endian u64 at the given VMA."""
        off = va - RODATA_VMA
        if off < 0 or off + 8 > len(self._data):
            raise IndexError(f"VMA {va:#x} outside .rodata")
        return struct.unpack_from("<Q", self._data, off)[0]

    def end_va(self) -> int:
        return RODATA_VMA + len(self._data)


def is_text_ptr(value: int) -> bool:
    """True if value falls in the observed .text handler range."""
    return TEXT_LO <= value <= TEXT_HI


def slot_at(rd: Rodata, va: int) -> tuple[int, int, int] | None:
    """Return (key, handler, pad) if a valid 24-byte slot starts at va, else None.

    A slot is valid when key < 0x10000, handler is a .text pointer, and pad is
    either zero or another .text pointer.
    """
    key = rd.qword(va)
    if key >= KEY_MAX:
        return None
    handler = rd.qword(va + 8)
    if not is_text_ptr(handler):
        return None
    pad = rd.qword(va + 16)
    if pad != 0 and not is_text_ptr(pad):
        return None
    return key, handler, pad


def walk_block(rd: Rodata, lead_va: int, stop_va: int) -> list[tuple]:
    """Walk one block, returning a list of (slot_va, key, handler, pad) tuples.

    Greedy 24-byte slot consumption with 8-byte resync on misses; terminates at
    stop_va or after MAX_NON_SLOT_GROUPS consecutive non-slot groups.
    """
    rows: list[tuple] = []
    va = lead_va
    consecutive_misses = 0
    while va + SLOT_SIZE <= stop_va:
        slot = slot_at(rd, va)
        if slot is not None:
            key, handler, pad = slot
            rows.append((va, key, handler, pad))
            consecutive_misses = 0
            va += SLOT_SIZE
        else:
            consecutive_misses += 1
            if consecutive_misses > MAX_NON_SLOT_GROUPS:
                break
            va += RESYNC_STEP
    return rows


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: extract_arch_blocks.py /path/to/ptxas", file=sys.stderr)
        return 2

    rd = Rodata(load_rodata(sys.argv[1]))

    # Per-block stop VMAs: next lead, with explicit end for the last block.
    stops = BLOCK_LEADS[1:] + [BLOCK7_END]

    # block_index -> list of (slot_va, key, handler, pad)
    per_block: dict[int, list[tuple]] = {}
    # block_index -> set of distinct handler VMAs
    handlers: dict[int, set[int]] = {}

    tsv_lines = [
        "block_index\tlead_va\tslot_va\tkey_hex\tformat_id\tminor\thandler_va\tpad_va"
    ]

    for idx, lead_va in enumerate(BLOCK_LEADS, start=1):
        rows = walk_block(rd, lead_va, stops[idx - 1])
        per_block[idx] = rows
        handlers[idx] = {h for (_, _, h, _) in rows}
        for (slot_va, key, handler, pad) in rows:
            tsv_lines.append(
                f"{idx}\t{lead_va:#x}\t{slot_va:#x}\t{key:#06x}\t"
                f"{key >> 8:#x}\t{key & 0xFF:#x}\t{handler:#x}\t{pad:#x}"
            )

    OUT_TSV.write_text("\n".join(tsv_lines) + "\n")

    # ---------------------------------------------------------------------
    # SUMMARY
    # ---------------------------------------------------------------------
    print("=" * 78)
    print("PTXAS PER-ARCH SASS-ENCODING DISPATCH BLOCKS -- SUMMARY")
    print("ptxas 13.0.88  .rodata region 0x22E7AD0 .. 0x23EFB60")
    print("=" * 78)
    print()
    print(f"{'blk':>3} {'lead_va':>11} {'lead_handler':>13} "
          f"{'slots':>7} {'distinct_h':>11} {'span_bytes':>11}")
    print("-" * 62)
    for idx, lead_va in enumerate(BLOCK_LEADS, start=1):
        rows = per_block[idx]
        lead_handler = rd.qword(lead_va + 8)
        span = rows[-1][0] + SLOT_SIZE - lead_va if rows else 0
        print(f"{idx:>3} {lead_va:#011x} {lead_handler:#013x} "
              f"{len(rows):>7} {len(handlers[idx]):>11} {span:>11}")
    print()

    # block1-vs-blockN shared / unique handler matrix.
    baseline = handlers[1]
    print("Handler overlap vs block1 (classic baseline):")
    print(f"{'pair':>12} {'blkN_slots':>10} {'blkN_h':>8} "
          f"{'shared_w_b1':>12} {'unique_to_blkN':>15} {'%unique':>8}")
    print("-" * 70)
    for idx in range(2, 8):
        hN = handlers[idx]
        shared = len(hN & baseline)
        unique = len(hN - baseline)
        pct = (100.0 * unique / len(hN)) if hN else 0.0
        print(f"  b1 vs b{idx:<2} {len(per_block[idx]):>10} {len(hN):>8} "
              f"{shared:>12} {unique:>15} {pct:>7.1f}%")
    print(f"\n  (block1 baseline: {len(baseline)} distinct handlers, "
          f"{len(per_block[1])} slots)")
    print()

    # Hypothesis test: blocks 1-5 share most handlers; blocks 6-7 are distinct.
    classic_union: set[int] = set()
    for idx in range(1, 6):
        classic_union |= handlers[idx]
    print("Hypothesis test (classic family 1-5 vs new blocks 6-7):")
    print(f"  union of classic handlers (blocks 1-5): {len(classic_union)}")
    for idx in (6, 7):
        hN = handlers[idx]
        shared = len(hN & classic_union)
        unique = len(hN - classic_union)
        pct = (100.0 * unique / len(hN)) if hN else 0.0
        print(f"  block{idx}: {len(hN)} handlers -- shared w/ classic-union "
              f"{shared}, unique {unique} ({pct:.1f}% block-family-unique)")
    print()
    print(f"rows written: {OUT_TSV}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
