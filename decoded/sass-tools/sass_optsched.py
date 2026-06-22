#!/usr/bin/env python3
# nvopen-tools -- SASS reverse-engineering tooling.  The scheduling model is
# recovered from static + differential analysis of the CUDA 13.1 ptxas /
# nvdisasm binaries plus live-GPU measurement.
"""
Constraint-optimal per-basic-block SASS instruction-reorder scheduler.

Given a basic block's slice of the typed/weighted dependency DAG (sass_scheduler)
this finds an instruction ORDER that minimises the block's issue makespan while
honouring every hazard, then the toolchain recomposes control words for that
order and proves the result bit-identical on the GPU before accepting it.

THE MODEL (per block)
---------------------
Variables: a per-instruction issue cycle t[i] >= 0 (the makespan is max t[i]+1)
and, implicitly, the issue ORDER (the permutation by t).  Constraints:

  * ORDER / latency (RAW):  for a coupled fixed-latency producer P feeding C,
        t[C] >= t[P] + lat(P->C)
    where lat is the recovered issue-relative coupled stall (sass_latency_tables).
    For a decoupled producer (memory / MUFU / S2R / conversion) the consumer
    instead WAITS on the producer's scoreboard, which absorbs the variable
    latency at run time; statically this reduces to an ORDER edge
        t[C] >= t[P] + 1
    plus a small scoreboard-ready floor so the reorder does not bunch a long
    load immediately before its use (the wait still serialises at run time, but
    we keep the producer earlier so independent work can fill its shadow).
  * ORDER (WAR/WAW/CTRL):   t[C] >= t[P] + 1  (anti / output / control edges are
        ordering-only: the false dependency from the FIXED register allocation
        must be preserved -- we never rename, so every WAR/WAW edge is a hard
        ordering constraint and caps how much reordering is possible).
  * SINGLE-ISSUE:           at most one instruction issues per cycle (the
        per-warp front-end issues one instr/cycle); enforced by requiring all
        t[i] distinct -- the list scheduler issues exactly one per step, and the
        Z3 model adds Distinct(t).
  * PINNED:                 a cross-block-hazard instruction (a live-in scoreboard
        wait consumer) keeps its relative position -- it may not move earlier
        than any earlier-block producer guarantees its operand ready.  We pin it
        to its original RANK within the block (an order constraint against every
        instruction originally before it that it still depends on transitively),
        which conservatively preserves the cross-block wait semantics.

OBJECTIVE: minimise the makespan = the last issue cycle.  Fewer total cycles for
the block means the kernel reaches the next block sooner.  Because the schedule
is then GPU-gated bit-identical, an incorrect latency estimate can only ever
make us *fall back* to ptxas, never produce a wrong-but-accepted schedule.

SOLVERS
-------
  * list scheduler (always): a cycle-stepped critical-path list scheduler -- the
    standard, near-optimal workhorse.  Ready set = instructions whose every
    predecessor has issued and whose latency has elapsed; tie-break by longest
    remaining critical path, then by original order (stable).
  * Z3-Opt (blocks <= Z3_MAX_INSTR): an exact makespan minimiser
    (Optimize().minimize(makespan)) over integer issue cycles with Distinct and
    the latency/order constraints.  Used to prove the list result optimal (or to
    beat it).  Falls back silently to the list result if Z3 is unavailable.

We keep the BEST of {ptxas-original order, list, optimal} by makespan; ties go to
ptxas (zero reorder risk).  The chosen order is returned as a permutation of the
block's instruction indices.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import sass_scheduler as S  # noqa: E402
from sass_blocks import Block  # noqa: E402

# Z3 is optional; we degrade to the list scheduler if it is missing.
try:
    import z3  # type: ignore
    _HAVE_Z3 = True
except Exception:  # pragma: no cover - exercised when z3 absent
    _HAVE_Z3 = False

Z3_MAX_INSTR = 32        # only run the exact solver on blocks this small
Z3_TIMEOUT_MS = 8000     # per-block Z3 budget


# Per-block edge model: order edges + latency edges, restricted to a block.

@dataclass
class BlockModel:
    """A block's scheduling constraints, in BLOCK-LOCAL indices [0, size)."""
    size: int
    g_idx: list[int]                       # local -> global instruction index
    lat: list[list[tuple[int, int]]]       # lat[u] = [(v, weight)] order+latency
    crit: list[int]                        # remaining critical-path length per node
    pin_rank: dict[int, int]               # local idx -> minimum issue rank (pinned)
    last_fixed: int = -1                   # local idx that MUST issue last (-1 none)


def build_block_model(dag: "S.DAG", blk: Block, arch: str) -> BlockModel:
    """Slice the global DAG down to one block and turn it into latency edges.

    Every global edge whose BOTH endpoints lie in the block becomes a local
    constraint u->v with a weight: the RAW coupled stall for a coupled producer,
    or 1 (ordering floor) for a decoupled-producer RAW (scoreboard-absorbed) and
    for every WAR/WAW/CTRL edge.  Edges that cross the block boundary are handled
    by the pin ranks (the live-in waits) -- they are not reorderable anyway.
    """
    lo, hi = blk.start, blk.end
    size = hi - lo
    g_idx = list(range(lo, hi))
    lat: list[list[tuple[int, int]]] = [[] for _ in range(size)]
    seen: set[tuple[int, int]] = set()
    for e in dag.edges:
        if not (lo <= e.src < hi and lo <= e.dst < hi):
            continue
        u, v = e.src - lo, e.dst - lo
        if u == v:
            continue
        if e.kind == S.EDGE_RAW and S._is_stall_edge(e):
            w = max(1, e.weight)           # coupled fixed-latency RAW
        elif e.kind in (S.EDGE_WAR, S.EDGE_WAW) and e.weight > 0 \
                and dag.nodes[e.src].coupled:
            # Anti/output edge from a coupled source: the reader's operand-collect
            # window (WAR) or the fast-writer floor (WAW).  Use the real edge
            # weight so the block makespan the solver MINIMISES matches what the
            # composer will EMIT for this order -- otherwise the model would
            # under-count these gaps (weight 1) and propose an order whose
            # recomposed makespan is larger than the model predicted (a model-win
            # that is not a silicon-win).
            w = max(1, e.weight)
        else:
            # decoupled-producer RAW (scoreboard) or zero-weight WAR/WAW/CTRL:
            # order-only.
            w = 1
        key = (u, v)
        if key in seen:
            # keep the tightest (largest) latency on a duplicated edge
            for k, (vv, ww) in enumerate(lat[u]):
                if vv == v:
                    lat[u][k] = (v, max(ww, w))
                    break
            continue
        seen.add(key)
        lat[u].append((v, w))

    # ---- hard structural ordering constraints --------------------------------
    # (1) the block terminator (a control transfer in the last slot) MUST issue
    #     last -- moving it changes which instructions execute.  Add an order
    #     edge from every other node to it.
    last_fixed = -1
    if size and S._base_mnem(_block_mnem(dag, lo + size - 1)) in _TERM_BASES:
        last_fixed = size - 1
        for u in range(size):
            if u != last_fixed:
                _add_order(lat, seen, u, last_fixed)
    # (2) MEMORY ORDERING: side-effecting memory ops (stores/atomics/loads) can
    #     alias through pointers the register DAG does not see, so we preserve
    #     their RELATIVE program order with a chain of order edges.  This is the
    #     conservative, correctness-first choice (a true alias analysis could
    #     relax it, but the GPU gate would reject any unsafe relaxation anyway).
    mem_ops = [u for u in range(size)
               if S._base_mnem(_block_mnem(dag, lo + u)) in _MEM_BASES]
    for a, b in zip(mem_ops, mem_ops[1:]):
        _add_order(lat, seen, a, b)

    crit = _critical_lengths(size, lat)
    # Pin ranks: a pinned instruction (live-in scoreboard wait) must not issue
    # before any instruction originally ahead of it in the block.  Conservative:
    # pin it to its ORIGINAL local position as a lower bound on its issue rank.
    pin_rank: dict[int, int] = {}
    for gk in blk.pinned:
        pin_rank[gk - lo] = gk - lo
    return BlockModel(size, g_idx, lat, crit, pin_rank, last_fixed)


# Control-transfer mnemonics that must stay last in a block.
_TERM_BASES = {"BRA", "BRX", "JMP", "JMX", "CALL", "CALLR", "RET", "EXIT",
               "BREAK", "BMOV", "WARPSYNC", "BPT", "RTT", "KILL", "NANOSLEEP",
               "YIELD", "BSSY", "BSYNC", "NOP"}
# Side-effecting memory ops whose relative order we preserve (alias-safe).
_MEM_BASES = {"STG", "STS", "STL", "ST", "RED", "ATOMG", "ATOMS", "ATOM",
              "LDG", "LDS", "LDL", "LD", "LDC", "LDGSTS", "TEX", "SULD",
              "SUST", "SUATOM"}


def _block_mnem(dag: "S.DAG", g: int) -> str:
    return dag.nodes[g].mnem


def _add_order(lat: list[list[tuple[int, int]]], seen: set, u: int, v: int) -> None:
    """Add (or strengthen to) a weight-1 order edge u->v, dedup-aware."""
    if u == v:
        return
    if (u, v) in seen:
        return
    seen.add((u, v))
    lat[u].append((v, 1))


def _critical_lengths(size: int, lat: list[list[tuple[int, int]]]) -> list[int]:
    """Remaining longest-weighted-path length from each node to a sink.

    crit[u] = max over successors v of (weight(u,v) + crit[v]); a sink has the
    node's own unit cost.  Used as the list-scheduler priority (schedule the
    instruction on the longest remaining chain first)."""
    # topological order via Kahn on the order graph
    indeg = [0] * size
    for u in range(size):
        for v, _ in lat[u]:
            indeg[v] += 1
    from collections import deque
    order: list[int] = []
    dq = deque(u for u in range(size) if indeg[u] == 0)
    while dq:
        u = dq.popleft()
        order.append(u)
        for v, _ in lat[u]:
            indeg[v] -= 1
            if indeg[v] == 0:
                dq.append(v)
    crit = [1] * size
    for u in reversed(order):
        best = 1
        for v, w in lat[u]:
            best = max(best, w + crit[v])
        crit[u] = best
    return crit


# List scheduler.

@dataclass
class Schedule:
    order: list[int]          # block-local indices in issue order
    issue: list[int]          # issue[local_idx] = issue cycle
    makespan: int
    solver: str               # 'ptxas' | 'list' | 'optimal'


def list_schedule(m: BlockModel) -> Schedule:
    """Cycle-stepped critical-path list scheduler.

    At each step the ready set is every un-issued instruction all of whose
    predecessors have issued AND whose latency has elapsed (ready_cycle reached).
    We issue ONE per cycle (single front-end issue), choosing the ready node with
    the greatest remaining critical-path length; ties break to the smaller pin
    rank then to the original order (stable, ptxas-like).
    """
    size = m.size
    preds: list[list[tuple[int, int]]] = [[] for _ in range(size)]
    for u in range(size):
        for v, w in m.lat[u]:
            preds[v].append((u, w))
    indeg = [len(preds[v]) for v in range(size)]

    issue = [-1] * size
    order: list[int] = []
    cycle = 0
    remaining = size
    # ready_at[v]: earliest cycle v may issue given already-issued preds.
    ready_at = [0] * size
    for v in range(size):
        ready_at[v] = m.pin_rank.get(v, 0)
    issued = [False] * size

    while remaining:
        ready = [v for v in range(size)
                 if not issued[v] and indeg[v] == 0 and ready_at[v] <= cycle]
        if not ready:
            cycle += 1
            continue
        # priority: longest remaining critical path, then pin rank, then order
        ready.sort(key=lambda v: (-m.crit[v], m.pin_rank.get(v, 0), v))
        v = ready[0]
        issue[v] = cycle
        issued[v] = True
        order.append(v)
        remaining -= 1
        for w_, ww in [(s[0], s[1]) for s in m.lat[v]]:
            indeg[w_] -= 1
            ready_at[w_] = max(ready_at[w_], cycle + ww)
        cycle += 1
    makespan = max((issue[v] + 1 for v in range(size)), default=0)
    return Schedule(order, issue, makespan, "list")


def ptxas_schedule(m: BlockModel) -> Schedule:
    """The reference schedule = ptxas's original order (identity permutation),
    timed under the SAME latency model so makespans are comparable."""
    size = m.size
    order = list(range(size))
    issue = [0] * size
    ready_at = [m.pin_rank.get(v, 0) for v in range(size)]
    preds: list[list[tuple[int, int]]] = [[] for _ in range(size)]
    for u in range(size):
        for v, w in m.lat[u]:
            preds[v].append((u, w))
    cycle = 0
    for v in order:
        cycle = max(cycle, ready_at[v])
        issue[v] = cycle
        for w_, ww in m.lat[v]:
            ready_at[w_] = max(ready_at[w_], cycle + ww)
        cycle += 1
    makespan = max((issue[v] + 1 for v in range(size)), default=0)
    return Schedule(order, issue, makespan, "ptxas")


# Z3 exact makespan minimiser.

def z3_schedule(m: BlockModel, upper: int) -> Schedule | None:
    """Exact integer-cycle makespan minimiser via Z3-Opt.

    t[i] in [0, upper); Distinct(t) (one issue per cycle); t[v] >= t[u] + w for
    every latency edge u->v; t[v] >= pin_rank[v]; minimise max(t).  Returns the
    optimal Schedule, or None if Z3 is unavailable / times out / is no better."""
    if not _HAVE_Z3 or m.size > Z3_MAX_INSTR:
        return None
    size = m.size
    opt = z3.Optimize()
    opt.set("timeout", Z3_TIMEOUT_MS)
    t = [z3.Int(f"t{i}") for i in range(size)]
    for i in range(size):
        opt.add(t[i] >= 0, t[i] < upper)
        if i in m.pin_rank:
            opt.add(t[i] >= m.pin_rank[i])
    opt.add(z3.Distinct(t))           # single-issue: distinct cycles
    for u in range(size):
        for v, w in m.lat[u]:
            opt.add(t[v] >= t[u] + w)
    # the block terminator must issue strictly after every other instruction.
    if m.last_fixed >= 0:
        for i in range(size):
            if i != m.last_fixed:
                opt.add(t[m.last_fixed] > t[i])
    mk = z3.Int("makespan")
    for i in range(size):
        opt.add(mk >= t[i] + 1)
    opt.minimize(mk)
    if opt.check() != z3.sat:
        return None
    model = opt.model()
    issue = [model.evaluate(t[i]).as_long() for i in range(size)]
    order = sorted(range(size), key=lambda i: issue[i])
    makespan = max(iv + 1 for iv in issue)
    return Schedule(order, issue, makespan, "optimal")


# Per-block driver: pick the best schedule.

@dataclass
class BlockResult:
    blk: Block
    model: BlockModel
    chosen: Schedule
    ptxas: Schedule
    candidates: dict[str, int]   # solver -> makespan
    reordered: bool              # chosen order != identity

    @property
    def perm_global(self) -> list[int]:
        """The chosen order as GLOBAL instruction indices (issue order)."""
        return [self.model.g_idx[lv] for lv in self.chosen.order]


def schedule_block(dag: "S.DAG", blk: Block, arch: str) -> BlockResult:
    """Solve one block: list + (optionally) Z3, keep the best vs ptxas."""
    m = build_block_model(dag, blk, arch)
    ref = ptxas_schedule(m)
    cands: dict[str, Schedule] = {"ptxas": ref}
    lst = list_schedule(m)
    cands["list"] = lst
    # give Z3 a generous upper bound (list makespan + slack) so a valid model
    # always exists; it then minimises below it.
    upper = max(ref.makespan, lst.makespan) + m.size + 2
    z = z3_schedule(m, upper)
    if z is not None:
        cands["optimal"] = z

    # choose the smallest makespan; ties -> ptxas (no reorder), else list.
    best = ref
    for name in ("optimal", "list"):
        s = cands.get(name)
        if s is not None and s.makespan < best.makespan:
            best = s
    # if a non-ptxas winner ties ptxas, prefer ptxas (zero risk).
    if best.makespan == ref.makespan:
        best = ref
    reordered = (best.solver != "ptxas" and best.order != list(range(m.size)))
    return BlockResult(blk, m, best, ref,
                       {k: v.makespan for k, v in cands.items()}, reordered)


if __name__ == "__main__":
    import sass_blocks as B
    from sass_ctrl_decode import disasm_cubin
    cub = sys.argv[1]
    d = S.decompose(cub)
    ctrls = d.ctrls
    part = B.partition_cubin(cub, ctrls)
    print(f"# {Path(cub).name}  arch={d.arch}  {part.summary()}  "
          f"z3={'yes' if _HAVE_Z3 else 'NO (list-only)'}")
    for blk in part.blocks:
        if not blk.reorder_ok or blk.size < 2:
            continue
        res = schedule_block(d.dag, blk, d.arch)
        tag = "REORDERED" if res.reordered else "already-optimal"
        print(f"  BB{blk.bid} size={blk.size:<3} makespans={res.candidates} "
              f"-> {res.chosen.solver} ({res.chosen.makespan})  {tag}")
        if res.reordered:
            orig = [ctrls[blk.start + lv].mnem for lv in range(blk.size)]
            new = [ctrls[g].mnem for g in res.perm_global]
            print(f"     orig: {orig}")
            print(f"     new : {new}")
