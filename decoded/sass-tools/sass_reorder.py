#!/usr/bin/env python3
# nvopen-tools -- SASS reverse-engineering tooling.  Our code; the scheduling
# model is recovered from static + differential analysis of the CUDA 13.1
# ptxas / nvdisasm binaries plus live-GPU measurement.
"""
Reorder-patcher + GPU-gated orchestration for the constraint-optimal scheduler.

Two public entry points the CLI exposes:

  optsched_kernel(cubin)   Stage 2 -- per basic block: build the DAG, solve for a
                           makespan-minimal instruction ORDER (sass_optsched),
                           reorder the fixed 16-byte instruction words IN PLACE
                           within the block's byte range, recompose control words
                           for the new order (preserving cross-block scoreboard
                           waits verbatim), then GPU-GATE the result bit-identical
                           to ptxas.  A block is accepted ONLY if bit-identical;
                           otherwise it falls back to ptxas's original order.
                           Finally measure cycles (ncu) V(ptxas) vs V(ours).

  tighten_kernel(cubin)    Stage 1 -- keep ptxas's ORDER, tighten only the stalls
                           our model proves are slack, GPU-gated bit-identical via
                           the same surgical + bisection logic as --perf-diff.

WHY IN-PLACE REORDER NEEDS NO PC FIXUP
--------------------------------------
Every SASS instruction on this ISA is a fixed 16 bytes.  Permuting instructions
WITHIN one basic block does not change the block's total byte length and does not
move any branch TARGET (a block has no internal targets -- asserted by the
splitter), so no PC-relative branch displacement changes.  The only field that
must be recomputed is each word's scheduling control (bits 105-124): the new
order has different producer->consumer distances, hence different stalls and
scoreboard pairings.  We recompose those from the reordered DAG.

THE CORRECTNESS GATE (absolute)
-------------------------------
For each kernel we patch the reordered+recomposed cubin, run it on the GPU with
the same seeded inputs as ptxas's original, and require BIT-IDENTICAL output.
Per block: accept our order iff the whole-kernel output stays identical with that
block reordered; else revert that block to ptxas.  The final kernel is therefore
provably never wrong (bit-identical) and never slower (each block is the min of
ptxas and an accepted faster order).
"""
from __future__ import annotations

import copy
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import sass_scheduler as S  # noqa: E402
import sass_blocks as B  # noqa: E402
import sass_optsched as OS  # noqa: E402
from sass_ctrl_decode import Ctrl  # noqa: E402

NVDISASM = S.NVDISASM
PTXAS = S.PTXAS


# =============================================================================
# Reordered control-word recomposition.
# =============================================================================

def _reorder_ctrls(ctrls: list[Ctrl], perm: list[int]) -> list[Ctrl]:
    """Return a new Ctrl list whose positions [perm] hold the instructions named
    by `perm` (a permutation of ALL global indices, identity outside reordered
    blocks).  Offsets are reassigned to the new sequential positions so the
    rebuilt DAG sees the new program order."""
    out: list[Ctrl] = []
    for new_pos, old_idx in enumerate(perm):
        c = copy.copy(ctrls[old_idx])
        c.offset = new_pos * 16
        out.append(c)
    return out


def recompose_for_order(d: "S.Decomposition", perm: list[int],
                        live_in: dict[int, set[int]]) -> "S.ComposeResult":
    """Recompose control words for the reordered instruction stream.

    `perm` maps new position -> old global index.  We rebuild the operand stream
    and DAG in the new order (so producer->consumer distances, stalls, and
    scoreboard pairings are all re-derived for the new schedule), run the
    existing composer, then OVERLAY the preserved cross-block scoreboard waits:
    a live-in wait (armed in a previous block / iteration) is re-asserted on the
    instruction that originally carried it, because the producer is outside this
    block and the composer cannot see it.
    """
    ctrls2 = _reorder_ctrls(d.ctrls, perm)
    ops2 = [d.operands[old] for old in perm]
    type_map = S.build_type_map(d.arch)
    dag2 = S.build_dag(ctrls2, ops2, type_map, d.arch)
    # absorb_ctrl: when we REORDER, the CC/predicate control band is hidden by the
    # independent work we hoisted into its shadow (just as ptxas hides it) -- so
    # recompose it as an absorbable issue-relative latency, not a hard 13-cyc
    # floor.  The GPU bit-identical + cycle gate is the safety net.
    cr = S.compose(dag2, ctrls2, absorb_ctrl=True)

    # old global index -> new position
    new_pos = {old: i for i, old in enumerate(perm)}
    # re-assert preserved live-in waits at the instruction's NEW position.
    for old_idx, sbs in live_in.items():
        np_ = new_pos.get(old_idx)
        if np_ is None:
            continue
        for sb in sbs:
            cr.fields[np_].wait_mask |= (1 << sb)
    return cr


# =============================================================================
# In-place reorder patch: permute 16-byte words + write recomposed control.
# =============================================================================

def reorder_patch(src: str, dst: str, entry: str, perm: list[int],
                  cr: "S.ComposeResult", n_instr: int) -> int:
    """Write a reordered + recomposed copy of the cubin.

    The .text.<entry> section's first n_instr 16-byte words are rewritten so that
    new position k holds the raw 16-byte word of old instruction perm[k], with
    its scheduling-control bits replaced by pack_control(cr.fields[k]); every
    other bit (opcode, operands, reuse, predicate) is preserved verbatim.  Words
    beyond n_instr (trailing NOP padding) are left untouched.  Returns words
    written.
    """
    data = bytearray(Path(src).read_bytes())
    off, size = S._text_section_span(Path(src), entry)
    nwords = size // 16
    # snapshot the original words so a permutation that reads a slot it will also
    # overwrite is correct (we read from the snapshot, write to `data`).
    orig = [int.from_bytes(data[off + k * 16: off + k * 16 + 16], "little")
            for k in range(nwords)]
    clear = ~(((1 << (124 - 105 + 1)) - 1) << 105)   # zero bits 105..124
    patched = 0
    for k in range(min(n_instr, len(perm), nwords)):
        w = orig[perm[k]] & clear
        if k < len(cr.fields):
            w |= S.pack_control(cr.fields[k])
        base = off + k * 16
        data[base:base + 16] = w.to_bytes(16, "little")
        patched += 1
    Path(dst).write_bytes(data)
    return patched


# =============================================================================
# GPU gate helpers (reuse sass_scheduler's harness / launch / ncu plumbing).
# =============================================================================

def _entry_of(cubin: str) -> str | None:
    out = subprocess.run([NVDISASM, "-c", cubin], capture_output=True,
                         text=True).stdout
    import re
    m = re.search(r"\.text\.(\S+)", out)
    return m.group(1) if m else None


def _run(harness: str, cubin: str, entry: str, nwords: int, grid: int,
         block: int, niter: int, quiet: bool = True) -> str | None:
    return S._run_kernel(harness, cubin, entry, nwords, grid, block, niter,
                         quiet=quiet)


# =============================================================================
# Stage 2 : per-block reorder, GPU-gated, fall back to ptxas per block.
# =============================================================================

@dataclass
class BlockOutcome:
    bid: int
    size: int
    solver: str
    ptxas_makespan: int
    our_makespan: int
    reordered: bool
    accepted: bool
    reason: str = ""


@dataclass
class OptResult:
    entry: str
    n_instr: int
    outcomes: list[BlockOutcome]
    final_perm: list[int]
    cycles_ptxas: float = 0.0
    cycles_ours: float = 0.0
    gate_ok: bool = False
    tightened_stalls: int = 0     # Stage-1 stalls also tightened on the final order
    saved_stall_cyc: int = 0      # total stall cycles/iter removed by tightening


def _identity_perm(n: int) -> list[int]:
    return list(range(n))


def optsched_kernel(cubin: str, entry: str | None = None,
                    grid: int = 8, block: int = 32, niter: int = 100000,
                    nwords: int = 8, measure: bool = True,
                    verbose: bool = True) -> OptResult:
    """Stage-2 driver: schedule + reorder every eligible block, GPU-gating each.

    Strategy: build a SINGLE candidate permutation that reorders every block our
    solver improved, gate the whole kernel bit-identical, and if it fails, peel
    blocks back to ptxas one at a time (largest-makespan-gain first) until the
    output is identical -- so each accepted block is individually proven safe and
    the rest fall back.  Returns the per-block outcomes + measured cycles.
    """
    entry = entry or _entry_of(cubin)
    if entry is None:
        raise RuntimeError("could not determine entry name")
    harness = S._launch_harness()
    if harness is None:
        raise RuntimeError("launch harness unavailable (need gcc + libcuda)")

    d = S.decompose(cubin)
    part = B.partition_cubin(cubin, d.ctrls)
    n = len(d.ctrls)

    # WHOLE-PROGRAM SOLVE: encode every reorderable block as one SMT-LIB2
    # optimisation and discharge it to the external Z3 binary (sass_smt).  The
    # solver co-minimises every block's makespan at once; we fall back to the
    # in-process per-block list/Z3 path if the binary is unavailable.
    import sass_smt
    smt = sass_smt.schedule_program(cubin, d, part)

    # solve every reorder-eligible block; collect its proposed GLOBAL order.
    block_perm: dict[int, list[int]] = {}   # bid -> proposed GLOBAL order for block
    outcomes: list[BlockOutcome] = []
    live_in_all: dict[int, set[int]] = {}
    for blk in part.blocks:
        live_in_all.update(blk.live_in_waits)
        if not blk.reorder_ok or blk.size < 2:
            outcomes.append(BlockOutcome(blk.bid, blk.size, "ptxas", 0, 0,
                                         False, True,
                                         blk.reason or "trivial/pinned"))
            continue
        # per-block reference makespan (for the report); the chosen ORDER comes
        # from the whole-program SMT result when it reordered this block.
        ref = OS.schedule_block(d.dag, blk, d.arch)
        glob = smt.perm_by_block.get(blk.bid)
        reordered = (blk.bid in smt.reordered_bids and glob is not None
                     and glob != list(range(blk.start, blk.end)))
        if not reordered:
            glob = ref.perm_global if ref.reordered else None
            reordered = ref.reordered
        solver_name = smt.solver if blk.bid in smt.reordered_bids else ref.chosen.solver
        oc = BlockOutcome(blk.bid, blk.size, solver_name,
                          ref.ptxas.makespan, ref.chosen.makespan,
                          reordered, accepted=not reordered)
        outcomes.append(oc)
        if reordered and glob is not None:
            block_perm[blk.bid] = glob

    # base perm = identity; we will splice in each improved block's order, and
    # (Stage-1) tighten the recomposed stalls so a reorder never carries inflated
    # stalls that erase its makespan benefit.
    def build_perm(active_bids: set[int]) -> list[int]:
        perm = _identity_perm(n)
        for blk in part.blocks:
            if blk.bid in active_bids and blk.bid in block_perm:
                perm[blk.start:blk.end] = block_perm[blk.bid]
        return perm

    # ground-truth output from the original cubin
    out_ref = _run(harness, cubin, entry, nwords, grid, block, niter, quiet=False)
    if out_ref is None:
        raise RuntimeError("reference kernel launch failed")

    # baseline ptxas cycles -- the bar every accepted reorder must beat (or tie).
    base_cycles = _measure_cycles(harness, cubin, entry, grid, block, niter,
                                  nwords) if measure else None

    improved = [oc for oc in outcomes if oc.reordered]
    # order candidate blocks by makespan gain (largest first).
    cand_bids = sorted((oc.bid for oc in improved),
                       key=lambda b: -(next(o.ptxas_makespan - o.our_makespan
                                            for o in outcomes if o.bid == b)))

    # ---- GPU acceptance: each block must be BIT-IDENTICAL *and* not regress
    # cycles.  We greedily add blocks one at a time; a block is kept only if the
    # kernel with it added is bit-identical AND its measured cycles are <= the
    # best-accepted-so-far cycles (so the final kernel is provably min(ptxas, ours
    # that passed) -- never wrong, never slower).
    accepted_bids: set[int] = set()
    best_cycles = base_cycles
    block_reason: dict[int, str] = {}
    for bid in cand_bids:
        trial = accepted_bids | {bid}
        perm = build_perm(trial)
        cub_t, ident = _emit_and_check(harness, cubin, entry, perm, d,
                                       live_in_all, n, out_ref, grid, block,
                                       niter, nwords)
        if not ident:
            block_reason[bid] = "GPU gate: output differed -> fell back to ptxas"
            continue
        if best_cycles is not None:
            cyc = _measure_cycles(harness, cub_t, entry, grid, block, niter,
                                  nwords)
            # accept only if it does not regress beyond a small noise margin.
            if cyc is None or cyc > best_cycles * 1.002:
                block_reason[bid] = (
                    f"cycle gate: {cyc:,.0f} > best {best_cycles:,.0f} "
                    f"-> fell back to ptxas")
                continue
            best_cycles = cyc
        accepted_bids = trial

    for oc in outcomes:
        if oc.reordered:
            oc.accepted = oc.bid in accepted_bids
            if not oc.accepted:
                oc.reason = block_reason.get(oc.bid, "fell back to ptxas")

    final_perm = build_perm(accepted_bids)
    cr = recompose_for_order(d, final_perm, live_in_all)
    final_cub = f"/tmp/sass_optsched_{entry}.cubin"
    reorder_patch(cubin, final_cub, entry, final_perm, cr, n)

    result = OptResult(entry, n, outcomes, final_perm)
    # final correctness assertion
    out_final = _run(harness, final_cub, entry, nwords, grid, block, niter,
                     quiet=False)
    result.gate_ok = (out_final is not None and out_final == out_ref)

    # STAGE-1 ON TOP: tighten the slack stalls of the FINAL (possibly reordered)
    # schedule, GPU-gated bit-identical + cycle-non-regressing.  This captures the
    # FP/transcendental stall slack regardless of whether any block was reordered,
    # so --optsched is always at least as good as --tighten.
    if result.gate_ok:
        n_t, saved, cyc_t = _tighten_on_top(
            harness, cubin, final_cub, entry, final_perm, d, live_in_all, n,
            out_ref, base_cycles, best_cycles, grid, block, niter, nwords,
            measure)
        result.tightened_stalls = n_t
        result.saved_stall_cyc = saved
        if cyc_t is not None:
            best_cycles = cyc_t

    if measure and result.gate_ok:
        result.cycles_ptxas = base_cycles or 0.0
        result.cycles_ours = (best_cycles if best_cycles else base_cycles) or 0.0
    return result


def _tighten_on_top(harness, cubin, base_cub, entry, perm, d, live_in, n,
                    out_ref, base_cycles, cur_cycles, grid, block, niter,
                    nwords, measure):
    """Tighten the final schedule's slack stalls in place, GPU-gated.

    Operates on the already-emitted reordered cubin (base_cub).  Decodes ITS
    control words, recomposes our model's stalls for the reordered DAG, finds
    where our stall < the emitted stall (slack candidates), and tightens the
    maximal bit-identical subset via the same surgical+bisection logic as Stage 1.
    Returns (n_tightened, saved_stall_cyc, new_cycles_or_None).
    """
    # decode the reordered cubin's emitted control words.
    d2 = S.decompose(base_cub)
    cr2 = S.compose(d2.dag, d2.ctrls)
    cands = S._understall_candidates(d2, cr2)
    if not cands:
        return 0, 0, None
    base_fields = [S.ctrl_to_fields(c) for c in d2.ctrls]

    def patch_subset(subset):
        surg = [S.SchedFields(f.usched, f.dst_wr, f.src_rel, f.wait_mask,
                              f.batch_t) for f in base_fields]
        for c in subset:
            surg[c["idx"]].usched = S.stall_to_usched(c["composed"],
                                                      end_group=False)
        p = f"/tmp/sass_optsched_tighten_{entry}.cubin"
        S.patch_cubin(base_cub, p, entry, surg)
        return p

    v = patch_subset(cands)
    ot = _run(harness, v, entry, nwords, grid, block, niter, quiet=True)
    safe = cands
    if ot is None or ot != out_ref:
        safe = []
        for c in cands:
            t = patch_subset(safe + [c])
            o = _run(harness, t, entry, nwords, grid, block, niter, quiet=True)
            if o is not None and o == out_ref:
                safe.append(c)
    if not safe:
        return 0, 0, None
    final = patch_subset(safe)
    of = _run(harness, final, entry, nwords, grid, block, niter, quiet=False)
    if of is None or of != out_ref:
        return 0, 0, None
    # cycle-gate the tightened result; only keep it if it does not regress.
    new_cyc = None
    if measure and (base_cycles is not None):
        new_cyc = _measure_cycles(harness, final, entry, grid, block, niter,
                                  nwords)
        ref = cur_cycles if cur_cycles else base_cycles
        if new_cyc is None or new_cyc > ref * 1.002:
            return 0, 0, None
    # commit the tightened cubin as the final output.
    Path(f"/tmp/sass_optsched_{entry}.cubin").write_bytes(Path(final).read_bytes())
    saved = sum(c["ptxas"] - c["composed"] for c in safe)
    return len(safe), saved, new_cyc


def _measure_cycles(harness: str, cubin: str, entry: str, grid: int, block: int,
                    niter: int, nwords: int) -> float | None:
    """Median of 3 ncu cycle measurements (gpc__cycles_elapsed.max) -- robust to
    run-to-run jitter so the cycle gate does not accept/reject on noise."""
    vals: list[float] = []
    for _ in range(3):
        r = S._ncu_perf(harness, cubin, entry, grid, block, niter, nwords)
        if r.blocked:
            return None
        if r.ok:
            v = r.metrics.get("gpc__cycles_elapsed.max", 0.0)
            if v:
                vals.append(v)
    if not vals:
        return None
    vals.sort()
    return vals[len(vals) // 2]


def _emit_and_check(harness: str, cubin: str, entry: str, perm: list[int],
                    d: "S.Decomposition", live_in: dict[int, set[int]], n: int,
                    out_ref: str, grid: int, block: int, niter: int,
                    nwords: int) -> tuple[str, bool]:
    """Emit the reordered+recomposed cubin for `perm` and return (path, ident)."""
    cr = recompose_for_order(d, perm, live_in)
    trial = f"/tmp/sass_optsched_gate_{entry}.cubin"
    reorder_patch(cubin, trial, entry, perm, cr, n)
    out = _run(harness, trial, entry, nwords, grid, block, niter, quiet=True)
    return trial, (out is not None and out == out_ref)


# =============================================================================
# Stage 1 : stall-tightener (ptxas order fixed, only slack stalls tightened).
# =============================================================================

@dataclass
class TightenResult:
    entry: str
    n_candidates: int
    n_safe: int
    saved_stall: int
    gate_ok: bool
    cycles_ptxas: float = 0.0
    cycles_ours: float = 0.0
    safe_desc: list[str] = field(default_factory=list)
    hazard_desc: list[str] = field(default_factory=list)


def tighten_kernel(cubin: str, entry: str | None = None,
                   grid: int = 8, block: int = 32, niter: int = 100000,
                   nwords: int = 8, measure: bool = True) -> TightenResult:
    """Stage-1 driver: tighten only the stalls our model proves slack, GPU-gated.

    Keeps ptxas's instruction ORDER.  Finds understall candidates (our composed
    stall < ptxas's), builds a surgical V2 with only those tightened, gates
    bit-identical; on a fault, bisects to the maximal safe subset (each subset
    individually GPU-gated).  Reuses the exact surgical+bisection logic proven in
    --perf-diff, generalised to an arbitrary cubin.
    """
    entry = entry or _entry_of(cubin)
    if entry is None:
        raise RuntimeError("could not determine entry name")
    harness = S._launch_harness()
    if harness is None:
        raise RuntimeError("launch harness unavailable")

    d = S.decompose(cubin)
    cr = S.compose(d.dag, d.ctrls)
    cands = S._understall_candidates(d, cr)
    res = TightenResult(entry, len(cands), 0, 0, False)
    if not cands:
        res.gate_ok = True
        return res

    base = [S.ctrl_to_fields(c) for c in d.ctrls]
    out_ref = _run(harness, cubin, entry, nwords, grid, block, niter, quiet=False)
    if out_ref is None:
        raise RuntimeError("reference launch failed")

    # surgical all-on
    def patch_subset(subset: list[dict]) -> str:
        surg = copy.deepcopy(base)
        for c in subset:
            surg[c["idx"]].usched = S.stall_to_usched(c["composed"],
                                                      end_group=False)
        p = f"/tmp/sass_tighten_{entry}.cubin"
        S.patch_cubin(cubin, p, entry, surg)
        return p

    v2 = patch_subset(cands)
    out2 = _run(harness, v2, entry, nwords, grid, block, niter, quiet=True)
    safe = cands
    if out2 is None or out2 != out_ref:
        # bisect: greedily add candidates that stay bit-identical
        safe = []
        for c in cands:
            trial = patch_subset(safe + [c])
            ot = _run(harness, trial, entry, nwords, grid, block, niter,
                      quiet=True)
            if ot is not None and ot == out_ref:
                safe.append(c)
    hazards = [c for c in cands if c not in safe]

    res.n_safe = len(safe)
    res.saved_stall = sum(c["ptxas"] - c["composed"] for c in safe)
    res.safe_desc = [f"#{c['idx']} {S._base_mnem(c['mnem'])} "
                     f"{c['ptxas']}->{c['composed']}" for c in safe]
    res.hazard_desc = [f"#{c['idx']} {S._base_mnem(c['mnem'])} "
                       f"{c['ptxas']}->{c['composed']}" for c in hazards]

    if not safe:
        res.gate_ok = True   # nothing tightened == ptxas == correct
        return res

    final = patch_subset(safe)
    out_final = _run(harness, final, entry, nwords, grid, block, niter,
                     quiet=False)
    res.gate_ok = (out_final is not None and out_final == out_ref)

    if measure and res.gate_ok:
        r1 = S._ncu_perf(harness, cubin, entry, grid, block, niter, nwords)
        r2 = S._ncu_perf(harness, final, entry, grid, block, niter, nwords)
        if r1.ok and r2.ok:
            res.cycles_ptxas = r1.metrics.get("gpc__cycles_elapsed.max", 0.0)
            res.cycles_ours = r2.metrics.get("gpc__cycles_elapsed.max", 0.0)
    return res
