#!/usr/bin/env python3
# nvopen-tools -- SASS reverse-engineering tooling.  Our code; the scheduling
# model is recovered from static + differential analysis of the CUDA 13.1
# ptxas / nvdisasm binaries plus live-GPU measurement.
"""
Whole-program SMT-LIB2 instruction scheduler, discharged to a standalone Z3.

Rather than solve each basic block in isolation, this encodes the ENTIRE kernel
as one optimisation problem in SMT-LIB2 and hands it to an external Z3 binary
(Z3_BIN, default a local optimisation-tuned build).  The single global model lets
the solver co-optimise across every reorderable block at once and minimise the
whole-kernel issue makespan, while hard control-flow fences keep each basic block
contiguous and in program order (so no instruction ever crosses a branch).

THE WHOLE-PROGRAM MODEL
-----------------------
One integer issue cycle t[i] for every instruction i in the kernel:

  * GLOBAL ORDER FENCE: for adjacent basic blocks B_k, B_{k+1}, every instruction
    of B_k issues strictly before every instruction of B_{k+1}.  We encode this
    compactly with a per-block base offset: t[i] = base[block(i)] + rank[i], where
    rank is a block-local issue position in [0, size).  Blocks therefore never
    interleave -- control flow is preserved exactly.
  * REORDERABLE block: rank[i] is a free permutation variable (Distinct ranks),
    constrained by the block's latency/order edges (RAW coupled stall; WAR/WAW/
    CTRL/decoupled-RAW order floor) expressed on the GLOBAL t.  The terminator is
    pinned to the last rank; pinned (live-in) instructions keep their rank lower
    bound.
  * PINNED block (barrier / control / internal target): rank[i] is FIXED to the
    instruction's original position -- the block keeps ptxas's order verbatim.

  * OBJECTIVE: minimise the global makespan = max over i of t[i] + 1.  Because
    blocks are fenced and contiguous, this equals sum of per-block makespans plus
    the fixed inter-block offsets, i.e. minimising it minimises every block at
    once.

The encoding is emitted as text, piped to the Z3 binary with `(minimize)` +
`(check-sat)` + `(get-model)`, and the returned model is parsed back into a
per-block permutation.  Falls back to the in-process per-block solver
(sass_optsched) if the binary is unavailable or returns unknown.

CORRECTNESS: identical to the per-block path -- the SMT result is only a proposed
ORDER; the toolchain still recomposes control words and GPU-gates every block
bit-identical (and cycle-non-regressing) before accepting it.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import sass_scheduler as S  # noqa: E402
import sass_blocks as B  # noqa: E402
import sass_optsched as OS  # noqa: E402

# External Z3 binary (an optimisation-tuned local build is preferred; falls back
# to whatever `z3` is on PATH).  Override with the SASS_Z3 env var.
def _find_z3() -> str | None:
    cand = os.environ.get("SASS_Z3")
    if cand and Path(cand).exists():
        return cand
    local = Path.home() / ".local/bin/z3"
    if local.exists():
        return str(local)
    return shutil.which("z3")


Z3_BIN = _find_z3()
SMT_TIMEOUT_S = 30          # whole-program solve budget (seconds)


@dataclass
class SmtSchedule:
    perm_by_block: dict[int, list[int]]   # bid -> GLOBAL issue order for that block
    reordered_bids: set[int]
    solver: str                           # 'z3-smt' | 'fallback'
    objective: int | None                 # solved global makespan (if known)


def _emit_smtlib(d: "S.Decomposition", part: "B.Partition") -> tuple[str, dict]:
    """Build the whole-program SMT-LIB2 text + the metadata to decode the model.

    Returns (smt_text, meta) where meta maps each reorderable block to its local
    model (BlockModel) and global index base so the parsed ranks can be turned
    back into a permutation.
    """
    lines: list[str] = []
    lines.append("(set-option :produce-models true)")
    # box priority lets the optimiser handle the single objective directly.
    lines.append("(set-option :opt.priority lex)")

    meta: dict = {"blocks": {}}
    # We model the ISSUE CYCLE t of each instruction directly (not its position):
    # latency edges constrain cycles, which include stall gaps and so can exceed
    # the instruction count.  A single front-end issues one instruction per cycle,
    # so all cycles within a block are DISTINCT; the instruction ORDER is then
    # read off by sorting the solved cycles.  Each block is solved over its own
    # cycle variables (blocks are fenced + contiguous, so they do not interact);
    # the global objective is the sum of per-block makespans.
    obj_terms: list[str] = []

    for blk in part.blocks:
        size = blk.size
        base = blk.start            # contiguous block base (program order)
        if not blk.reorder_ok or size < 2:
            continue                # pinned: fixed order, no variables
        m = OS.build_block_model(d.dag, blk, d.arch)
        # an upper bound on any issue cycle: the unit makespan plus the worst-case
        # latency slack (sum of all edge weights) -- generous so a model exists.
        upper = size + sum(w for u in range(size) for _, w in m.lat[u]) + 2
        meta["blocks"][blk.bid] = {"model": m, "base": base, "blk": blk,
                                   "upper": upper}
        # issue-cycle variables t_b_i in [0, upper); single-issue -> distinct.
        tvars = [f"t_{blk.bid}_{i}" for i in range(size)]
        for tv in tvars:
            lines.append(f"(declare-const {tv} Int)")
            lines.append(f"(assert (>= {tv} 0))")
            lines.append(f"(assert (< {tv} {upper}))")
        lines.append(f"(assert (distinct {' '.join(tvars)}))")
        # pin (live-in waits): issue cycle >= original position lower bound.
        for li, pr in m.pin_rank.items():
            lines.append(f"(assert (>= t_{blk.bid}_{li} {pr}))")
        # terminator issues strictly last.
        if m.last_fixed >= 0:
            for i in range(size):
                if i != m.last_fixed:
                    lines.append(
                        f"(assert (> t_{blk.bid}_{m.last_fixed} t_{blk.bid}_{i}))")
        # latency / order edges on issue cycles: t_v >= t_u + w.
        for u in range(size):
            for v, w in m.lat[u]:
                lines.append(
                    f"(assert (>= t_{blk.bid}_{v} (+ t_{blk.bid}_{u} {w})))")
        # block makespan = the last issue cycle + 1.
        mk = f"mk_{blk.bid}"
        lines.append(f"(declare-const {mk} Int)")
        for i in range(size):
            lines.append(f"(assert (>= {mk} (+ t_{blk.bid}_{i} 1)))")
        obj_terms.append(mk)

    if not obj_terms:
        return "", meta            # nothing to optimise

    # global objective = sum of per-block makespans (each block fenced, so the
    # total program length is the sum of block lengths + fixed inter-block gaps).
    lines.append("(declare-const TOTAL Int)")
    lines.append(f"(assert (= TOTAL (+ {' '.join(obj_terms)})))")
    lines.append("(minimize TOTAL)")
    lines.append("(check-sat)")
    lines.append("(get-objectives)")
    lines.append("(get-model)")
    return "\n".join(lines) + "\n", meta


_DEF_RE = re.compile(r"\(define-fun\s+(\w+)\s*\(\)\s+Int\s+(-?\d+)\)")
_OBJ_RE = re.compile(r"\(TOTAL\s+(-?\d+)\)")


def _parse_model(out: str) -> tuple[dict[str, int], int | None]:
    """Parse Z3's (get-model) / (get-objectives) text into {var: value}, total."""
    vals: dict[str, int] = {}
    for m in _DEF_RE.finditer(out):
        vals[m.group(1)] = int(m.group(2))
    obj = None
    mo = _OBJ_RE.search(out)
    if mo:
        obj = int(mo.group(1))
    return vals, obj


def schedule_program(cubin: str, d: "S.Decomposition",
                     part: "B.Partition") -> SmtSchedule:
    """Solve the whole kernel as ONE SMT-LIB2 optimisation, discharged to Z3_BIN.

    Returns the proposed per-block permutation (GLOBAL indices).  On any failure
    (no binary / unknown / timeout) returns the per-block in-process result so the
    pipeline always has a schedule to gate.
    """
    smt, meta = _emit_smtlib(d, part)
    if not smt or Z3_BIN is None:
        return _fallback(d, part)

    try:
        r = subprocess.run([Z3_BIN, f"-T:{SMT_TIMEOUT_S}", "-smt2", "-in"],
                           input=smt, capture_output=True, text=True,
                           timeout=SMT_TIMEOUT_S + 10)
    except (subprocess.TimeoutExpired, OSError):
        return _fallback(d, part)
    out = r.stdout
    if "unsat" in out.split("\n")[0] or "sat" not in out:
        return _fallback(d, part)
    vals, obj = _parse_model(out)
    if not vals:
        return _fallback(d, part)

    perm_by_block: dict[int, list[int]] = {}
    reordered: set[int] = set()
    for bid, info in meta["blocks"].items():
        m = info["model"]
        base = info["base"]
        cycles = []
        ok = True
        for i in range(m.size):
            key = f"t_{bid}_{i}"
            if key not in vals:
                ok = False
                break
            cycles.append((vals[key], i))
        if not ok:
            continue
        cycles.sort()                     # by issue cycle -> program order
        local_order = [i for _, i in cycles]
        glob = [m.g_idx[li] for li in local_order]
        perm_by_block[bid] = glob
        if local_order != list(range(m.size)):
            reordered.add(bid)
    if not perm_by_block:
        return _fallback(d, part)
    return SmtSchedule(perm_by_block, reordered, "z3-smt", obj)


def _fallback(d: "S.Decomposition", part: "B.Partition") -> SmtSchedule:
    """Per-block in-process schedule (sass_optsched) when the SMT path is N/A."""
    perm_by_block: dict[int, list[int]] = {}
    reordered: set[int] = set()
    for blk in part.blocks:
        if not blk.reorder_ok or blk.size < 2:
            continue
        res = OS.schedule_block(d.dag, blk, d.arch)
        perm_by_block[blk.bid] = res.perm_global
        if res.reordered:
            reordered.add(blk.bid)
    return SmtSchedule(perm_by_block, reordered, "fallback", None)


def solver_banner() -> str:
    if Z3_BIN is None:
        return "z3-smt: NO external binary (per-block fallback)"
    try:
        v = subprocess.run([Z3_BIN, "--version"], capture_output=True,
                           text=True, timeout=5).stdout.strip()
    except Exception:
        v = "?"
    return f"z3-smt: {Z3_BIN}  ({v})"


if __name__ == "__main__":
    cub = sys.argv[1]
    d = S.decompose(cub)
    part = B.partition_cubin(cub, d.ctrls)
    print(f"# {Path(cub).name}  {solver_banner()}")
    sched = schedule_program(cub, d, part)
    print(f"# solver={sched.solver}  objective={sched.objective}  "
          f"reordered blocks={sorted(sched.reordered_bids)}")
    for bid in sorted(sched.perm_by_block):
        if bid in sched.reordered_bids:
            glob = sched.perm_by_block[bid]
            print(f"  BB{bid}: {[d.ctrls[g].mnem for g in glob]}")
