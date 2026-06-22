#!/usr/bin/env python3
"""
SASS warp-issue / timing simulator.

Given a decoded SASS stream (opcode + control word per instruction, as produced
by `sass_ctrl_decode.py`), this replays the per-warp issue model documented in
the SASS execution-model wiki page:

    1. Wait-mask gate     -- block until every scoreboard in req_bit_set has
                             reached its target (its arming producer has retired).
    2. Dispatch stall     -- honour the usched stall before the next issue.
    3. Yield on DRAIN     -- usched==0 lets the scheduler prefer another warp
                             (single-warp model: no effect on cycle count, flagged).
    4. batch_t co-issue   -- BATCH_START..BATCH_END issue together in one decision
                             (modelled as a single issue slot for the group).
    5. Scoreboard arm     -- a producer with dst_wr/src_rel != 7 arms that
                             scoreboard; it clears `compl_latency` cycles later.

This is a SINGLE-WARP issue/timing model: it produces the lower-bound cycle cost
of one warp running a basic block back-to-back with no other warp to hide behind.
Real throughput on a full SM overlaps many warps; the per-warp trace is the unit
the compiler schedules against.

Variable-latency completion (scoreboard clear) uses a small, configurable
latency map keyed by the producer mnemonic family; these are *modelling
defaults*, not recovered constants -- the validated facts are the control-word
fields themselves (stall, wait-mask, scoreboard ids), which the simulator
consumes as-is from real ptxas output.

Usage:
    python3 sass_sched_sim.py <cubin> [--func NAME] [--json]
    # or feed a decoded list programmatically via simulate(ctrls)
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sass_ctrl_decode import Ctrl, disasm_cubin  # noqa: E402


# Modelling defaults for variable-latency scoreboard clear (cycles from issue to
# scoreboard release). These are coarse families; the simulator only needs them
# to time when a wait-mask gate opens. They are NOT recovered constants.
DEFAULT_COMPL_LATENCY = {
    "LDG": 200, "LD": 200, "LDL": 200, "LDS": 30, "LDSM": 30,
    "STG": 1, "STS": 1, "STL": 1, "ST": 1,
    "ATOM": 200, "ATOMG": 200, "RED": 1,
    "TEX": 200, "TLD": 200, "SULD": 200,
    "S2R": 20, "S2UR": 20, "CS2R": 20,
    "LDC": 20, "ULDC": 20,
    "HMMA": 13, "IMMA": 13, "DMMA": 13, "BMMA": 13,
    "MUFU": 13, "F2F": 13, "F2I": 13, "I2F": 13, "I2FP": 13,
    "BAR": 20, "DEPBAR": 1,
}


def _compl_latency(mnem: str) -> int:
    base = mnem.split(".")[0]
    if base in DEFAULT_COMPL_LATENCY:
        return DEFAULT_COMPL_LATENCY[base]
    # family fallbacks
    for k, v in DEFAULT_COMPL_LATENCY.items():
        if base.startswith(k):
            return v
    return 50  # generic decoupled default


@dataclass
class IssueEvent:
    idx: int
    offset: int
    mnem: str
    issue_cycle: int
    waited: int            # cycles spent in the wait-mask gate
    stalled: int           # cycles of dispatch stall after issue
    wait_sbs: list         # scoreboards waited on
    armed_sb: int          # scoreboard armed by this op (-1 if none)
    batch_pos: str         # "" | "start" | "mid" | "end"
    yielded: bool


@dataclass
class SimResult:
    events: list = field(default_factory=list)
    total_cycles: int = 0
    n_issue_slots: int = 0   # batch groups count as one slot
    n_instrs: int = 0
    n_waits: int = 0
    n_yields: int = 0

    def trace(self) -> str:
        rows = [f"{'cyc':>6} {'idx':>4} {'off':>6}  {'mnem':<16} "
                f"{'wait':>5} {'stall':>5}  notes"]
        for e in self.events:
            notes = []
            if e.wait_sbs:
                notes.append(f"WAIT SB{e.wait_sbs}")
            if e.armed_sb >= 0:
                notes.append(f"arm SB{e.armed_sb}")
            if e.batch_pos:
                notes.append(f"batch:{e.batch_pos}")
            if e.yielded:
                notes.append("YIELD")
            rows.append(f"{e.issue_cycle:>6} {e.idx:>4} {e.offset:#06x}  "
                        f"{e.mnem:<16} {e.waited:>5} {e.stalled:>5}  "
                        f"{' '.join(notes)}")
        rows.append(f"\nTOTAL: {self.total_cycles} cycles, "
                    f"{self.n_instrs} instrs, {self.n_issue_slots} issue slots, "
                    f"{self.n_waits} wait-gates, {self.n_yields} yields")
        return "\n".join(rows)


def _pair_batches(ctrls: list[Ctrl]) -> set[int]:
    """Return the set of instruction indices that belong to a *real* co-issue
    group, i.e. a BATCH_START(1/2) closed by a later BATCH_END(4). A lone
    BATCH_START with no closing END is a degenerate 1-instruction group and is
    NOT included (it issues as its own slot)."""
    grouped: set[int] = set()
    open_start = None
    span: list[int] = []
    for i, c in enumerate(ctrls):
        if c.batch_t in (1, 2):
            open_start = i
            span = [i]
        elif open_start is not None:
            span.append(i)
            if c.batch_t == 4:
                grouped.update(span)
                open_start = None
                span = []
    return grouped


def simulate(ctrls: list[Ctrl], stop_at_first_exit: bool = True) -> SimResult:
    """Replay the per-warp issue model over a decoded SASS stream."""
    res = SimResult()
    cyc = 0
    # scoreboard -> cycle at which it next becomes 'clear' (count reaches target).
    # We model depth-1 per SB (the common case); a second arm before clear
    # pushes the clear time out (in-order completion within a queue).
    sb_clear = {i: 0 for i in range(6)}
    grouped = _pair_batches(ctrls)
    in_batch = False

    for idx, c in enumerate(ctrls):
        mnem = c.mnem
        if mnem == "" or mnem.startswith("."):
            continue
        # 1. WAIT-MASK GATE -------------------------------------------------
        waited = 0
        if c.wait_mask:
            target = max((sb_clear[b] for b in c.wait_sbs), default=cyc)
            if target > cyc:
                waited = target - cyc
                cyc = target
            res.n_waits += 1

        issue_cycle = cyc

        # batch position bookkeeping (co-issue: the group consumes one slot).
        # Only honour START/END that form a real (closed) group; a lone
        # BATCH_START issues as its own slot.
        batch_pos = ""
        if idx in grouped:
            if c.batch_t in (1, 2):
                in_batch = True
                batch_pos = "start"
            elif c.batch_t == 4:
                batch_pos = "end"
            elif in_batch:
                batch_pos = "mid"

        new_slot = not (batch_pos in ("mid", "end"))
        if new_slot:
            res.n_issue_slots += 1

        # 5. SCOREBOARD ARM -------------------------------------------------
        armed = -1
        if c.dst_wr != 7:
            armed = c.dst_wr
            sb_clear[armed] = max(sb_clear[armed], issue_cycle) + _compl_latency(mnem)
        if c.src_rel != 7:
            # read-release scoreboard: arms on operand collection, clears sooner
            sb_clear[c.src_rel] = max(sb_clear[c.src_rel], issue_cycle) + 8

        # 3. YIELD ----------------------------------------------------------
        yielded = c.yield_
        if yielded:
            res.n_yields += 1

        # 2. DISPATCH STALL -------------------------------------------------
        # The usched stall is the minimum cycles before the NEXT issue.
        # A batch member (start/mid) does not advance the issue clock; the
        # group's stall is taken at BATCH_END (or on the lone start if no end).
        stalled = c.stall
        if batch_pos in ("start", "mid"):
            advance = 0
        else:
            advance = max(stalled, 1)
        cyc = issue_cycle + advance

        if batch_pos == "end":
            in_batch = False

        res.events.append(IssueEvent(
            idx=idx, offset=c.offset, mnem=mnem, issue_cycle=issue_cycle,
            waited=waited, stalled=stalled, wait_sbs=list(c.wait_sbs),
            armed_sb=armed, batch_pos=batch_pos, yielded=yielded))
        res.n_instrs += 1

        # Stop only on an UNCONDITIONAL EXIT (predicated EXIT is a side path).
        if stop_at_first_exit and mnem == "EXIT" and not c.pred:
            break

    res.total_cycles = cyc
    return res


def validate_stream(ctrls: list[Ctrl]) -> dict:
    """Cross-check the issue model against the real control words:
      * every consumer wait-bit pairs with an earlier producer that armed it
      * the opex invariant opex == batch_t<<5 | usched
      * a co-issue batch never carries a group-ending stall (usched 1..15)
    Returns a dict of counts and any violations found.
    """
    armed_by: dict[int, int] = {}
    pairs = unmatched = 0
    opex_bad = 0
    batch_with_eg_stall = 0
    for i, c in enumerate(ctrls):
        if c.opex != ((c.batch_t << 5) | c.usched):
            opex_bad += 1
        if c.batch_t in (1, 2, 4) and 1 <= c.usched <= 15:
            batch_with_eg_stall += 1
        for sb in c.wait_sbs:
            if sb in armed_by:
                pairs += 1
            else:
                unmatched += 1
        if c.dst_wr != 7:
            armed_by[c.dst_wr] = i
        if c.src_rel != 7:
            armed_by[c.src_rel] = i
    return {"barrier_pairs": pairs, "unmatched_waits": unmatched,
            "opex_violations": opex_bad,
            "batch_with_group_end_stall": batch_with_eg_stall,
            "n_instrs": len(ctrls)}


def _select_func(ctrls: list[Ctrl], func: str | None) -> list[Ctrl]:
    # the disasm of a single-kernel cubin is already one stream; func filtering
    # would need section parsing. For the validation kernels we assemble one
    # entry per cubin, so just return the whole stream.
    return ctrls


def main(argv: list[str]) -> int:
    cubin = argv[1]
    func = None
    if "--func" in argv:
        func = argv[argv.index("--func") + 1]
    ctrls = _select_func(disasm_cubin(cubin), func)
    if "--validate" in argv:
        import json
        print(json.dumps(validate_stream(ctrls), indent=2))
        return 0
    res = simulate(ctrls)
    if "--json" in argv:
        import json
        print(json.dumps({
            "total_cycles": res.total_cycles,
            "n_instrs": res.n_instrs,
            "n_issue_slots": res.n_issue_slots,
            "n_waits": res.n_waits,
            "n_yields": res.n_yields,
            "events": [vars(e) for e in res.events],
        }, indent=2))
    else:
        print(res.trace())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
