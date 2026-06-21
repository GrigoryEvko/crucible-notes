#!/usr/bin/env python3
# nvopen-tools -- SASS reverse-engineering tooling.  Our code; the scheduling
# model is recovered from static + differential analysis of the CUDA 13.1
# ptxas / nvdisasm binaries plus live-GPU measurement.
"""
Basic-block partitioning of a compiled SASS kernel, for the reorder scheduler.

A *basic block* is a maximal run of instructions with a single entry (its first
instruction is the only branch target inside it) and a single exit (only the
last instruction may transfer control).  The reorder scheduler is sound only
WITHIN a basic block: permuting instructions across a branch target or past a
control-transfer would change which instructions execute, so we split first and
schedule each block independently.

BB boundaries are recovered from `nvdisasm -c` directly:

  * a label line (`.L_x_0:`) starts a new block -- it is a branch target, so the
    instruction it precedes is a block leader.
  * any control-transfer instruction (BRA/BRX/JMP/CALL/RET/EXIT/BSSY/BSYNC/
    BREAK/BMOV/WARPSYNC/CALL/BPT/RTT/KILL/NANOSLEEP/YIELD) ENDS the current block;
    the next instruction is a leader.
  * BAR.* (barrier sync) and any instruction nvdisasm marks as a branch target
    also force a split.

For each block we also record whether it is "reorder-eligible":

  * it must contain no internal branch target (asserted: only the first instr is
    a target),
  * it must contain no control-transfer except possibly as its last instruction,
  * it must contain no barrier instruction whose arm/wait state straddles the
    block boundary (BSSY/BSYNC/BAR/DEPBAR live-in/out): those pin scoreboard /
    barrier state we cannot prove local, so such blocks are scheduled in place
    (Stage-1 tighten only, never reordered).

This module is purely structural: it consumes the same Ctrl stream the rest of
the toolchain uses (sass_ctrl_decode) and an offset->is_label map parsed from
nvdisasm, and emits block spans as (start_idx, end_idx) half-open ranges into
that stream.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from sass_ctrl_decode import Ctrl

NVDISASM = "/usr/local/cuda-13.1/bin/nvdisasm"

# Control-transfer / block-terminating mnemonics (base form, uppercase).
TERMINATORS = {
    "BRA", "BRX", "JMP", "JMX", "CALL", "CALLR", "RET", "EXIT", "BREAK",
    "BMOV", "WARPSYNC", "BPT", "RTT", "KILL", "NANOSLEEP", "YIELD", "RPCMOV",
}
# Barrier / synchronization ops whose state may straddle a block boundary; a
# block containing one is NOT reorder-eligible (we keep ptxas's order there and
# only run the Stage-1 stall-tightener on it).
BARRIER_OPS = {
    "BSSY", "BSYNC", "BAR", "DEPBAR", "MEMBAR", "ERRBAR", "BMSK.BAR",
    "ELECT", "VOTE.SYNC", "MATCH.SYNC", "REDUX.SYNC", "LDGSTS",
}


def _base(mnem: str) -> str:
    return mnem.split(".")[0] if mnem else ""


@dataclass
class Block:
    """A basic block: a half-open [start, end) range into the Ctrl stream."""
    bid: int
    start: int                 # first instruction index (inclusive)
    end: int                   # one-past-last index (exclusive)
    start_off: int             # byte offset of the first instruction
    reorder_ok: bool           # safe to permute instructions within this block
    reason: str                # why not reorder-eligible (if reorder_ok False)
    is_target: bool = False    # block leader is an explicit branch target
    # cross-block (loop-carried / fall-through) scoreboard state we must preserve
    # verbatim through a reorder.  live_in_waits[idx] = set of SBs the consumer
    # at `idx` waits on that were armed in an EARLIER block; pin_arms = SB ids
    # armed in this block that are still live at block exit (consumed later).
    live_in_waits: dict[int, set[int]] = field(default_factory=dict)
    pinned: list[int] = field(default_factory=list)  # indices pinned in place

    @property
    def size(self) -> int:
        return self.end - self.start

    def indices(self) -> range:
        return range(self.start, self.end)


_LABEL_RE = re.compile(r"^\s*(\.[A-Za-z_]\w*):\s*$")
_OFF_RE = re.compile(r"/\*([0-9a-fA-F]{4,})\*/")


def label_offsets(cubin: str) -> set[int]:
    """Byte offsets that are branch targets (immediately follow a label line).

    nvdisasm prints `.L_x_0:` on its own line just before the instruction at the
    target offset; the next instruction line carries that offset in `/*hhhh*/`.
    """
    r = subprocess.run([NVDISASM, "-c", cubin], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"nvdisasm failed: {r.stderr[:300]}")
    targets: set[int] = set()
    pending_label = False
    for ln in r.stdout.splitlines():
        if _LABEL_RE.match(ln):
            pending_label = True
            continue
        m = _OFF_RE.search(ln)
        if m and ";" in ln:
            if pending_label:
                targets.add(int(m.group(1), 16))
                pending_label = False
    return targets


def split_blocks(ctrls: list[Ctrl], targets: set[int]) -> list[Block]:
    """Partition the Ctrl stream into basic blocks.

    Leaders are: index 0, any instruction at a branch-target offset, and any
    instruction immediately following a terminator.  A block ends at the first
    terminator at/after its leader, or at the next leader.
    """
    n = len(ctrls)
    if n == 0:
        return []
    leader = [False] * n
    leader[0] = True
    for i, c in enumerate(ctrls):
        if c.offset in targets:
            leader[i] = True
        if _base(c.mnem) in TERMINATORS and i + 1 < n:
            leader[i + 1] = True

    blocks: list[Block] = []
    bid = 0
    i = 0
    while i < n:
        start = i
        j = i + 1
        # extend until a terminator (inclusive) or the next leader
        while j < n and not leader[j] and _base(ctrls[j - 1].mnem) not in TERMINATORS:
            j += 1
        # if the previous instr was a terminator we already stopped; otherwise
        # j sits on the next leader.  Either way [start, j) is the block.
        end = j
        blk = Block(bid, start, end, ctrls[start].offset, True, "")
        _classify_block(blk, ctrls, targets)
        blocks.append(blk)
        blk.is_target = ctrls[start].offset in targets
        bid += 1
        i = j
    return blocks


def _classify_block(blk: Block, ctrls: list[Ctrl], targets: set[int]) -> None:
    """Decide whether a block is reorder-eligible and record the reason if not.

    Hard disqualifiers (block kept in ptxas order): an internal branch target, a
    mid-block control transfer, or a barrier op whose state straddles the block
    boundary.  Cross-block scoreboard state (a loop-carried / fall-through wait
    armed in an earlier block, or an arm still live at block exit) is NOT a
    disqualifier -- we RECORD it and pin the affected instruction so the reorder
    preserves exactly that cross-block hazard (see live_in_waits / pinned).
    """
    # an internal branch target (other than the leader) would mean a hidden
    # entry edge -- never reorder across it.
    for k in range(blk.start + 1, blk.end):
        if ctrls[k].offset in targets:
            blk.reorder_ok = False
            blk.reason = f"internal branch target @{ctrls[k].offset:#06x}"
            return
    # a control transfer anywhere but the last slot splits control flow.
    for k in range(blk.start, blk.end - 1):
        if _base(ctrls[k].mnem) in TERMINATORS:
            blk.reorder_ok = False
            blk.reason = f"mid-block control transfer #{k} {ctrls[k].mnem}"
            return
    # a barrier op pins cross-block scoreboard / barrier state we cannot model.
    for k in range(blk.start, blk.end):
        if _base(ctrls[k].mnem) in BARRIER_OPS:
            blk.reorder_ok = False
            blk.reason = f"barrier op #{k} {ctrls[k].mnem} (state straddles BB)"
            return
    # Record cross-block scoreboard hazards so the reorder can preserve them:
    #   * a wait on an SB not armed earlier in THIS block is a live-in wait (the
    #     producer is in a previous block / a previous loop iteration).  The
    #     consumer keeps that wait bit verbatim; we additionally pin its index so
    #     it cannot float ahead of where the live value is guaranteed ready.
    #   * the producer/consumer roles for in-block-armed SBs are re-derived by the
    #     composer, so they need no pinning.
    armed_in_block: set[int] = set()
    for k in blk.indices():
        c = ctrls[k]
        live_in = {sb for sb in c.wait_sbs if sb not in armed_in_block}
        if live_in:
            blk.live_in_waits[k] = live_in
            blk.pinned.append(k)
        if c.dst_wr != 7:
            armed_in_block.add(c.dst_wr)
        if c.src_rel != 7:
            armed_in_block.add(c.src_rel)


@dataclass
class Partition:
    ctrls: list[Ctrl]
    blocks: list[Block]
    targets: set[int]

    def reorderable(self) -> list[Block]:
        return [b for b in self.blocks if b.reorder_ok and b.size >= 2]

    def summary(self) -> str:
        rb = self.reorderable()
        return (f"{len(self.blocks)} blocks, {len(rb)} reorder-eligible "
                f"(sizes {sorted(b.size for b in rb)})")


def partition_cubin(cubin: str, ctrls: list[Ctrl]) -> Partition:
    """Top-level: partition a decoded cubin into basic blocks."""
    targets = label_offsets(cubin)
    blocks = split_blocks(ctrls, targets)
    return Partition(ctrls, blocks, targets)


if __name__ == "__main__":
    import sys
    from sass_ctrl_decode import disasm_cubin
    cub = sys.argv[1]
    ctrls = [c for c in disasm_cubin(cub)
             if c.mnem and not c.mnem.startswith(".")]
    part = partition_cubin(cub, ctrls)
    print(f"# {Path(cub).name}: {part.summary()}")
    for b in part.blocks:
        tag = "REORDER" if b.reorder_ok else f"PINNED ({b.reason})"
        extra = ""
        if b.live_in_waits:
            extra = f"  live-in waits: {b.live_in_waits}"
        print(f"  BB{b.bid:<2} [{b.start:>3}..{b.end:<3}) "
              f"off={b.start_off:#06x} size={b.size:<3} {tag}{extra}")
        for k in b.indices():
            print(f"       #{k:<3} {ctrls[k].offset:#06x} {ctrls[k].mnem}")
