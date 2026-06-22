#!/usr/bin/env python3
# nvopen-tools -- SASS reverse-engineering tooling.  The scheduling model is
# recovered from static + differential analysis of the CUDA 13.1 ptxas /
# nvdisasm binaries plus live-GPU measurement.
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
  * any control-transfer instruction (BRA/BRX/JMP/CALL/RET/EXIT/BSYNC/BREAK/BMOV/
    WARPSYNC/BPT/RTT/KILL) ENDS the current block; the next instruction is a leader.
    BSYNC reconverges to a stacked PC and is itself a branch target, so it is a real
    control point.  BSSY, by contrast, only PUSHES a reconvergence PC (execution
    falls through), and YIELD/NANOSLEEP are scheduling hints -- none changes the PC,
    so none ends a block; each is instead pinned to its slot (PINNED_IN_PLACE) so it
    cannot float across the branch it guards while the rest of the block reorders.
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
#
# Only PC-changing ops belong here.  Two subtleties recovered from sm_89 -O3 output:
#
#   * BSYNC reconverges to a stacked PC and is, in every observed cubin, ITSELF a
#     branch target (so it already starts a block); it is a genuine control point
#     and correctly ends the divergent region's block -> keep it a terminator.
#   * BSSY only PUSHES a reconvergence PC onto the convergence stack; it does NOT
#     change the PC (execution falls through).  So the instruction after a BSSY is
#     NOT a basic-block leader -- making BSSY a terminator would create a false
#     leader and fragment a real block.  BSSY is therefore handled separately
#     (PINNED_IN_PLACE below): its block stays whole and reorderable, but the BSSY
#     itself is pinned to its slot so it never floats across the branch it guards.
#   * YIELD / NANOSLEEP are scheduling hints (no PC change) and must NOT be here for
#     the same reason; they too only pin their own slot.
TERMINATORS = {
    "BRA", "BRX", "JMP", "JMX", "CALL", "CALLR", "RET", "EXIT", "BREAK",
    "BMOV", "WARPSYNC", "BPT", "RTT", "KILL", "RPCMOV", "BSYNC",
}
# Ops that do NOT change the PC but whose program position is a control / scheduling
# fence we must not reorder across: a BSSY's reconvergence-PC arm must stay ahead of
# the divergent branch it guards; a YIELD/NANOSLEEP hint must stay where ptxas put
# it.  The containing block stays reorder-eligible (these are not state-straddling
# barriers), but each such instruction is pinned to its own slot.
PINNED_IN_PLACE = {"BSSY", "YIELD", "NANOSLEEP"}
# Barrier / synchronization ops whose state may straddle a block boundary; a
# block containing one is NOT reorder-eligible (we keep ptxas's order there and
# only run the Stage-1 stall-tightener on it).
BARRIER_OPS = {
    "BAR", "DEPBAR", "MEMBAR", "ERRBAR", "BMSK.BAR",
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
    # unchanged through a reorder.  live_in_waits[idx] = set of SBs the consumer
    # at `idx` waits on that were armed in an EARLIER block; live_out_arms = SB ids
    # armed in this block that are still live at block exit (consumed in a LATER
    # block), with their last-armer index pinned so the composer keeps the pairing.
    live_in_waits: dict[int, set[int]] = field(default_factory=dict)
    live_out_arms: dict[int, int] = field(default_factory=dict)  # SB -> armer idx
    pinned: list[int] = field(default_factory=list)  # indices pinned in place
    # internal: per-SB last in-block armer index, filled by _classify_block and
    # consumed by partition_cubin's global live-out pass.
    _last_armer: dict[int, int] = field(default_factory=dict)

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
    # PINNED_IN_PLACE ops (BSSY reconvergence-PC arm, YIELD/NANOSLEEP hints) do not
    # change the PC, so the block stays reorderable -- but each must stay in its slot
    # so it never floats across the divergent branch it guards (or, for a hint, away
    # from the issue point ptxas chose).  Pin its index; the block terminator is
    # already pinned last by the optsched model, so a lower-bound rank pin suffices.
    for k in range(blk.start, blk.end):
        if _base(ctrls[k].mnem) in PINNED_IN_PLACE:
            if k not in blk.pinned:
                blk.pinned.append(k)
    # Record cross-block scoreboard hazards so the reorder can preserve them:
    #   * a wait on an SB not armed earlier in THIS block is a live-in wait (the
    #     producer is in a previous block / a previous loop iteration).  The
    #     consumer keeps that wait bit unchanged; we additionally pin its index so
    #     it cannot float ahead of where the live value is guaranteed ready.
    #   * an SB armed in THIS block whose consumer is in a LATER block (a live-out
    #     arm -- e.g. a loop-invariant LDG hoisted into the prologue whose board the
    #     loop body waits on every iteration) must keep its producer's last-armer
    #     position: the composer re-derives in-block pairings but cannot see the
    #     out-of-block consumer, so a moved/re-numbered producer would orphan the
    #     downstream wait.  We mark the live-out armers here; partition_cubin's
    #     global pass (which can see the consumers) pins the ones that are truly
    #     live at block exit.  (In-block producer/consumer pairs need no pinning --
    #     the composer re-derives them; only the cross-block ones are pinned.)
    armed_in_block: set[int] = set()
    last_armer: dict[int, int] = {}          # SB -> last in-block index arming it
    for k in blk.indices():
        c = ctrls[k]
        live_in = {sb for sb in c.wait_sbs if sb not in armed_in_block}
        if live_in:
            blk.live_in_waits[k] = live_in
            blk.pinned.append(k)
        if c.dst_wr != 7:
            armed_in_block.add(c.dst_wr)
            last_armer[c.dst_wr] = k
        if c.src_rel != 7:
            armed_in_block.add(c.src_rel)
            last_armer[c.src_rel] = k
    # a PINNED_IN_PLACE op may also be a live-in consumer; dedup + keep sorted.
    blk.pinned = sorted(set(blk.pinned))
    # stash the per-SB last-armer index for the global live-out pass to consult.
    blk._last_armer = last_armer


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


def _pin_live_out_arms(ctrls: list[Ctrl], blocks: list[Block]) -> None:
    """Pin the producer of every scoreboard that is live at its block's exit.

    A scoreboard armed in block B is "live-out" when some later instruction WAITS
    on it before any instruction re-arms it.  The classic case is a loop-invariant
    `LDG` hoisted into the prologue: it arms SBk once, and the rolled loop body (a
    different block) waits on SBk every iteration.  The per-block composer re-derives
    only in-block producer->consumer pairings, so a live-out producer that the
    reorder moves or renumbers would orphan its out-of-block consumer.  We therefore
    pin that producer's last-armer index in place (added to `pinned`, recorded in
    `live_out_arms`) so the schedule keeps the cross-block arm where the consumer
    expects it.  The GPU bit-identical gate is still the final safety net; this pass
    just stops a correct schedule from being needlessly rejected.

    Re-arm before wait kills the liveness: we scan forward from each block's exit and
    take the FIRST event per SB (a re-arm makes the earlier arm dead; a wait makes it
    live).  Conservative on the loop back-edge: a block reachable from a later branch
    target is treated as a possible successor of every earlier block, so a wait
    anywhere after the producer (even across the back-edge) keeps the arm pinned.
    """
    n = len(ctrls)
    # For each SB, find for each instruction index whether the NEXT cross-stream
    # event is a wait (live) or a re-arm (dead).  We do a single backward sweep.
    # next_wait[sb] = nearest later index that WAITS on sb with no intervening
    # re-arm; we mark a producer live-out if such a wait exists in a LATER block.
    SB = 6
    next_event_is_wait = [[False] * SB for _ in range(n + 1)]
    # walk backward: at each index, propagate per-SB "next event after me is a wait"
    state = [False] * SB        # for sb: is the next downstream event a wait?
    for i in range(n - 1, -1, -1):
        c = ctrls[i]
        # snapshot AFTER i (what the producer at i sees downstream) is current state
        for sb in range(SB):
            next_event_is_wait[i][sb] = state[sb]
        # now fold i's own events into state (so earlier indices see i): a re-arm at
        # i resets liveness to dead unless i itself also waits; a wait at i sets live.
        waited = set(c.wait_sbs)
        armed = set()
        if c.dst_wr != 7:
            armed.add(c.dst_wr)
        if c.src_rel != 7:
            armed.add(c.src_rel)
        for sb in range(SB):
            if sb in waited:
                state[sb] = True       # a wait at i: producers before i are live
            elif sb in armed:
                state[sb] = False      # a re-arm at i with no wait: earlier arm dead
    # now pin each block's live-out armers.
    for blk in blocks:
        if not blk.reorder_ok:
            continue
        for sb, armer_idx in getattr(blk, "_last_armer", {}).items():
            # is the arm at armer_idx consumed by a wait AFTER the block ends?
            # next_event_is_wait[armer_idx][sb] is True iff the first downstream
            # event for sb (after armer_idx) is a wait with no intervening re-arm.
            if next_event_is_wait[armer_idx][sb]:
                # only a CROSS-BLOCK consumer matters: an in-block wait was already
                # handled by the composer (it re-derives in-block pairings).  Check
                # whether the resolving wait lies outside this block.
                resolved_in_block = any(
                    sb in ctrls[k].wait_sbs for k in range(armer_idx + 1, blk.end))
                if resolved_in_block:
                    continue
                blk.live_out_arms[sb] = armer_idx
                if armer_idx not in blk.pinned:
                    blk.pinned.append(armer_idx)
        blk.pinned.sort()


def partition_cubin(cubin: str, ctrls: list[Ctrl]) -> Partition:
    """Top-level: partition a decoded cubin into basic blocks."""
    targets = label_offsets(cubin)
    blocks = split_blocks(ctrls, targets)
    _pin_live_out_arms(ctrls, blocks)
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
            extra += f"  live-in waits: {b.live_in_waits}"
        if b.live_out_arms:
            extra += f"  live-out arms: {b.live_out_arms}"
        print(f"  BB{b.bid:<2} [{b.start:>3}..{b.end:<3}) "
              f"off={b.start_off:#06x} size={b.size:<3} {tag}{extra}")
        for k in b.indices():
            print(f"       #{k:<3} {ctrls[k].offset:#06x} {ctrls[k].mnem}")
