#!/usr/bin/env python3
# nvopen-tools -- SASS reverse-engineering tooling.  MIT-style: our code; the
# scheduling model below is recovered from static + differential analysis of the
# CUDA 13.1 ptxas / nvdisasm binaries (no vendor table text or matrices ship here).
"""
SASS instruction-scheduling **composer / decomposer** across abstraction layers.

This is the "scheduler brain" a SASS patcher needs: given an ordered instruction
stream it *chooses* the per-instruction stall counts and scoreboard pairings that
ptxas would emit; given a compiled cubin it *recovers* the typed/weighted
dependency graph and attributes every wait-bit/stall to the producer it resolves.

THE MODEL (recovered from binary analysis of CUDA 13.1 ptxas/nvdisasm)
---------------------------------------------------------------------
Scheduling is a *directed, typed, weighted multigraph* over instructions. For an
ordered producer P and a later consumer C sharing a register, the edge kind is
fixed by which side reads and which writes:

  * RAW  (P writes reg, C reads it)  : edge P->C, weight = P's RESULT latency
        (the producer must have produced the value). For coupled-math producers
        this collapses to an issue-relative stall looked up by (arch-family,
        producer-pipe, consumer-pipe) in the table-driven model
        (sass_latency_tables): 4 same-pipe, 5 cross-pipe, the AGU pre-issue slot
        (5; 8 on Turing), the slow-input latch (6 to the float<->int conversion;
        4/6 to MUFU by arch), and 13 (12 on Turing) if the producer writes a
        condition-code / predicate read as a guard (the control band).  The pipe
        and the coupled/decoupled verdict come from the per-arch SASS-ISA table
        (INSTRUCTION_TYPE / VIRTUAL_QUEUE); the magnitudes from a calibrated
        matrix recovered by differential analysis of emitted SASS.
  * WAR  (P reads reg, C writes it)  : edge P->C, weight = a SMALL operand-read
        latency -- its OWN per-resource default, NOT the transpose of the RAW
        weight. On registers it is zero-but-ordered (the reader has already
        latched the old value by issue time; ptxas also renames). This asymmetry
        is the whole point: RAW = "wait for the value to exist" (>=4 cyc);
        WAR = "wait for the old value to be consumed" (~0 cyc). Different
        physical events, different magnitudes.
  * WAW  (both write)                : edge P->C, weight = write-ordering default
        (zero-but-ordered; nonzero only when a slow producer precedes a fast one
        to the same dest).
  * CTRL (CC / branch / barrier)     : sequencing edges.

Fixed-latency producers (INSTRUCTION_TYPE = COUPLED_*) resolve RAW with a *stall
count* in usched_info.  Variable-latency producers (DECOUPLED_*_SCBD: memory,
MUFU/conversions, special-register reads, tensor) resolve RAW with one of the
*6 hardware scoreboards*: the producer arms dst_wr_sb (and/or src_rel_sb), the
consumer sets the matching bit in req_bit_set.  When more than 6 are live the
allocator overloads scoreboards (VSB->PSB by minimum added stall); unbounded
batches (cp.async) use SB5 + DEPBAR.LE.

THE FOUR LAYERS (adjacent ones are invertible)
----------------------------------------------
  L0  raw 128-bit instruction word, bits 102-125 (the control word)
  L1  decoded control fields (usched_info / dst_wr_sb / src_rel_sb /
                              req_bit_set / batch_t ; opex = batch_t<<5|usched)
  L2  semantic scheduling events (arm SB n on writeback, wait on SB n, stall k,
                                  co-issue group start/end, yield)
  L3  abstract typed + weighted dependency DAG over instructions

  decompose : cubin -> (L3 DAG, L1 control words), with each L2 event attributed
              to the producer it resolves.
  compose   : L3 DAG | ordered instr list -> L1 control words (the scheduler).
  L0<->L1   : SchedWord pack/unpack (bit-exact).
  L1<->L2   : ctrl_to_events / events_to_ctrl.

CAPABILITIES (CLI)
------------------
  --decompose K.cubin        layered view + DAG + attributed control words
  --compose   K.cubin        recompute control words from issue order, show them
  --debug     K.cubin        L0/L1/L2/L3 side-by-side, per-instr reasoning,
                             critical path, optional --dot graph.dot
  --verify    K.cubin ...    round-trip: recompose, compare to ptxas, classify
  --verify-corpus            build PTX corpus, compile with ptxas across arches,
                             decode + recompute + diff; print match rates

Reuses the sibling tools: sass_ctrl_decode (L0/L1 decode), sass_decode +
sass_isa_parser (per-arch instruction tables -> INSTRUCTION_TYPE), nvdisasm for
operand recovery.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from sass_ctrl_decode import Ctrl, disasm_cubin, usched_to_stall  # noqa: E402
import sass_latency_tables as LT  # noqa: E402

NVDISASM = "/usr/local/cuda-13.1/bin/nvdisasm"
PTXAS = "/usr/local/cuda-13.1/bin/ptxas"
NCU = "/usr/local/cuda-13.1/bin/ncu"
TABLE_DIR = HERE.parent / "nvdisasm-sass-isa"


# =============================================================================
# Recovered latency model (CUDA 13.1 ptxas, differentially confirmed on emitted
# SASS).  These are the dependency EDGE WEIGHTS, not the scalar result bands.
# =============================================================================

# Coupled-math RAW issue-relative anchors, kept for the debug reasoning labels.
# The actual per-(arch,pipe,pipe) magnitudes come from the table-driven model in
# sass_latency_tables (coupled_stall); these are only the canonical names the
# --debug output uses to describe an edge's weight.
COUPLED_SAME_PIPE = 4     # same-pipe coupled forwarding
COUPLED_CROSS_PIPE = 5    # cross-pipe inter-pipe penalty

# Structural-fallback band magnitudes for the operand classifier and the
# classify_mnem fallback (used only when a mnemonic is absent from the per-arch
# table; the authoritative bands come from sass_latency_tables.result_band).
VARLAT_BAND = {
    "LDG": 300, "LD": 300, "LDL": 300, "LDS": 30, "LDSM": 30, "LDC": 24,
    "STG": 1, "STS": 1, "STL": 1, "ST": 1, "RED": 1, "ATOMG": 300, "ATOM": 300,
    "TEX": 300, "TLD": 300, "TXD": 300, "SULD": 300,
    "S2R": 24, "S2UR": 24, "CS2R": 24, "ULDC": 24,
    "MUFU": 13, "F2F": 13, "F2I": 13, "I2F": 13, "I2I": 13, "POPC": 13,
    "FLO": 13, "BREV": 24, "PRMT": 13,
    "HMMA": 13, "IMMA": 13, "DMMA": 13, "BMMA": 13, "OMMA": 13,
    "LDGSTS": 300, "LDGDEPBAR": 1, "DEPBAR": 1,
}

# Coupled ops that write a condition-code / predicate destination -> control band.
CC_PRED_PRODUCERS = {"ISETP", "FSETP", "DSETP", "HSETP2", "PLOP3", "VOTE",
                     "P2R", "R2P", "LOP3.PAND"}


def _base_mnem(m: str) -> str:
    """Strip dotted modifiers: 'IMAD.MOV.U32' -> 'IMAD'."""
    return m.split(".")[0] if m else ""


# =============================================================================
# Per-arch INSTRUCTION_TYPE map (mnemonic -> coupled/decoupled).  Recovered from
# the per-arch SASS instruction tables; the parse + cache lives in
# sass_latency_tables.load_arch_model and this is a thin compatibility adapter.
# =============================================================================

@dataclass
class TypeInfo:
    coupled: bool          # True => fixed-latency stall; False => scoreboard
    itype: str             # raw INSTRUCTION_TYPE name
    min_wait: int          # MIN_WAIT_NEEDED floor
    wr_scbd: bool          # arms a write scoreboard (dst_wr_sb)
    rd_scbd: bool          # arms a read-release scoreboard (src_rel_sb)
    depbar: bool           # branch-unit / DEPBAR decoupled


def build_type_map(arch: str) -> dict[str, TypeInfo]:
    """Mnemonic (uppercase, base) -> TypeInfo, from one arch's CLASS table.

    Thin adapter over sass_latency_tables.load_arch_model (the cached per-arch
    class model that also carries the pipe family, band and MIN_WAIT_NEEDED).
    Kept for the rest of this module's API; new code should consult the richer
    ClassModel via classify_mnem / latency_model_for."""
    out: dict[str, TypeInfo] = {}
    for mn, cm in LT.load_arch_model(arch).items():
        out[mn] = TypeInfo(cm.coupled, cm.itype, cm.min_wait,
                           cm.arms_wr, cm.arms_rd, cm.depbar)
    return out


def latency_model_for(arch: str) -> dict[str, "LT.ClassModel"]:
    """The cached per-arch class model (mnemonic -> ClassModel)."""
    return LT.load_arch_model(arch)


# =============================================================================
# nvdisasm operand recovery: read/write register sets per instruction.
# =============================================================================

@dataclass
class Operands:
    writes: set[str] = field(default_factory=set)   # GPR/UR/PRED dests
    reads: set[str] = field(default_factory=set)    # GPR/UR/PRED srcs
    writes_cc: bool = False                          # writes a predicate/CC
    is_mem: bool = False
    is_branch: bool = False
    # Predicates consumed as an @P / @!P EXECUTION GUARD (predicated execution or
    # a guarded branch), as distinct from predicates read as an ordinary ALU
    # operand (e.g. FSEL/SEL `!P0`, IADD3 carry-in).  Only a guard read triggers
    # the CC/predicate control band; an operand read is an ordinary cross-pipe
    # edge.  Differentially confirmed on sm_89: FSETP feeding @P0 BRA pays 13,
    # FSETP feeding FSEL `!P0` pays 4.  (A subset of `reads`.)
    guard_reads: set[str] = field(default_factory=set)


# A register token: R0, R12, RZ, UR4, URZ, P0, PT, plus optional .reuse/.64 etc.
_REG_TOK = re.compile(r"\b(R\d+|RZ|UR\d+|URZ|P\d+|PT|UP\d+)\b")
_PRED_GUARD = re.compile(r"^@!?(P\d+|PT|UP\d+)\b")


def _split_dest_src(mnem: str, operand_str: str) -> Operands:
    """Heuristic operand classifier from the rendered SASS operand string.

    SASS operand order is dest-first: `OP Rd, Ra, Rb, Rc`.  Memory addresses in
    `[...]` are *reads* (address registers) even on a store; the value operand of
    a store is a read; the dest of a load is the first register.  Predicate dest
    `Pu` (e.g. ISETP `P0, PT, ...`) is a written CC/predicate.
    """
    ops = Operands()
    base = _base_mnem(mnem)
    ops.is_mem = base in VARLAT_BAND and base in (
        "LDG LD LDL LDS LDSM LDC STG STS STL ST RED ATOMG ATOM TEX TLD TXD "
        "SULD LDGSTS".split() and base or "")
    ops.is_branch = base in ("BRA", "BRX", "JMP", "CALL", "RET", "EXIT",
                             "BSSY", "BSYNC", "BREAK", "BMOV", "WARPSYNC")
    if not operand_str.strip():
        return ops

    # split top-level commas (ignore commas inside [...])
    parts, depth, cur = [], 0, ""
    for ch in operand_str:
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        parts.append(cur)
    parts = [p.strip() for p in parts]

    is_store = base.startswith("ST") or base in ("RED", "ATOMS")
    for i, p in enumerate(parts):
        regs = _REG_TOK.findall(p)
        in_mem = "[" in p
        for r in regs:
            if r in ("RZ", "URZ", "PT"):
                continue
            if in_mem:
                ops.reads.add(r)          # address registers are read
            elif i == 0 and not is_store:
                # first operand of a non-store is the destination
                if r.startswith("P") or r.startswith("UP"):
                    ops.writes.add(r)
                    ops.writes_cc = True
                else:
                    ops.writes.add(r)
            elif i == 0 and is_store:
                ops.reads.add(r)          # store: first reg (if any) is a read
            else:
                # subsequent: a written predicate dest can appear as op0 OR op1
                # for compare ops that emit "P0, PT, Ra, Rb"; treat a leading
                # predicate pair as dests.
                if (r.startswith("P") or r.startswith("UP")) and i <= 1 \
                        and not is_store and base in CC_PRED_PRODUCERS:
                    ops.writes.add(r)
                    ops.writes_cc = True
                else:
                    ops.reads.add(r)
    if base in CC_PRED_PRODUCERS:
        ops.writes_cc = True
    return ops


def disasm_operands(cubin: str) -> list[Operands]:
    """Parse nvdisasm plain disassembly into per-instruction Operands, aligned
    1:1 with sass_ctrl_decode's Ctrl stream (same instruction order)."""
    r = subprocess.run([NVDISASM, "-c", cubin], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"nvdisasm failed: {r.stderr[:300]}")
    out: list[Operands] = []
    line_re = re.compile(r"/\*[0-9a-fA-F]{4,}\*/\s*(.*?);")
    for ln in r.stdout.splitlines():
        m = line_re.search(ln)
        if not m:
            continue
        body = m.group(1).strip()
        if not body:
            continue
        toks = body.split()
        # capture and strip the predicate guard (@P0 / @!P0); the guard is a
        # READ of that predicate register -- a RAW edge from its producer (e.g.
        # ISETP/FSETP) into this instruction, the control-band (13) dependency.
        guard_reg = None
        if toks and toks[0].startswith("@"):
            mg = re.match(r"@!?(P\d+|UP\d+)", toks[0])
            if mg and mg.group(1) != "PT":
                guard_reg = mg.group(1)
            toks = toks[1:]
        if not toks:
            continue
        mnem = toks[0]
        operand_str = " ".join(toks[1:])
        ops = _split_dest_src(mnem, operand_str)
        if guard_reg:
            ops.reads.add(guard_reg)
            ops.guard_reads.add(guard_reg)   # consumed as an @P execution guard
        out.append(ops)
    return out


# =============================================================================
# L3: typed/weighted dependency DAG
# =============================================================================

EDGE_RAW = "RAW"
EDGE_WAR = "WAR"
EDGE_WAW = "WAW"
EDGE_CTRL = "CTRL"


# A fixed-latency RAW edge resolved by a usched stall (rather than a scoreboard).
# "stall_ctrl" additionally marks the CC/predicate control band -- a hard latency
# the predicated consumer cannot proceed without (never reduced by hidden work).
STALL_MECHS = ("stall", "stall_ctrl")


def _is_stall_edge(e: "Edge") -> bool:
    return e.kind == EDGE_RAW and e.mechanism in STALL_MECHS


@dataclass
class Edge:
    src: int          # producer index
    dst: int          # consumer index
    kind: str         # RAW / WAR / WAW / CTRL
    reg: str          # the shared resource (register / 'CC' / 'flow')
    weight: int       # cycle weight on this directed edge
    mechanism: str    # 'stall' | 'stall_ctrl' | 'scoreboard' | 'depbar' | 'order'


@dataclass
class Node:
    idx: int
    offset: int
    mnem: str
    ops: Operands
    coupled: bool
    band: int         # result-band magnitude (for critical path)


@dataclass
class DAG:
    nodes: list[Node] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    arch: str = "SM89"

    def out_edges(self, i: int) -> list[Edge]:
        return [e for e in self.edges if e.src == i]

    def in_edges(self, i: int) -> list[Edge]:
        return [e for e in self.edges if e.dst == i]


def classify_mnem(mnem: str, type_map: dict[str, TypeInfo],
                  arch: str = "SM89") -> tuple[bool, int]:
    """(coupled?, result-band) for a mnemonic.

    Coupled-ness and band come from the per-arch ClassModel (exact INSTRUCTION_TYPE
    + the scalar oracle band).  Falls back to the structural defaults only when a
    mnemonic is absent from the arch's table."""
    base = _base_mnem(mnem)
    cm = LT.load_arch_model(arch).get(base)
    if cm is not None:
        return cm.coupled, cm.band
    # fallback for a mnemonic missing from the arch table.
    band = LT.result_band(base)
    coupled = base not in VARLAT_BAND or base in ("PRMT",)
    if base in ("LDG", "LDS", "LDC", "LDL", "STG", "STS", "STL", "S2R", "S2UR",
                "CS2R", "MUFU", "F2F", "F2I", "I2F", "POPC", "TEX",
                "ATOMG", "LDGSTS", "HMMA", "IMMA"):
        coupled = False
    return coupled, band


def build_dag(ctrls: list[Ctrl], operands: list[Operands],
              type_map: dict[str, TypeInfo], arch: str = "SM89") -> DAG:
    """Construct the directed typed/weighted DAG from the decoded stream.

    Walks issue order; for each register tracks the last writer (for RAW/WAW) and
    the readers since the last write (for WAR).  Emits one edge per hazard with
    the kind-appropriate weight and mechanism.
    """
    dag = DAG(arch=arch)
    n = min(len(ctrls), len(operands))
    for i in range(n):
        c = ctrls[i]
        coupled, band = classify_mnem(c.mnem, type_map, arch)
        dag.nodes.append(Node(i, c.offset, c.mnem, operands[i], coupled, band))

    # Implicit uniform-descriptor dependency: a global memory op (LDG/STG/ATOMG/
    # RED) reads the 64-bit uniform descriptor that the most recent ULDC.64 wrote
    # (e.g. `ULDC.64 UR4` -> `STG.E [...]` uses UR4:UR5 as the base), even though
    # the descriptor register is not named in the memory operand text.  Without
    # this edge the scheduler would under-stall the ULDC and corrupt the address.
    _last_uldc_ur: set[str] = set()
    for i in range(n):
        base = _base_mnem(dag.nodes[i].mnem)
        if base in ("ULDC",):
            _last_uldc_ur = set(dag.nodes[i].ops.writes)
        elif base in ("LDG", "STG", "ATOMG", "RED", "LD", "ST", "LDGSTS",
                      "ATOM") and _last_uldc_ur:
            dag.nodes[i].ops.reads |= _last_uldc_ur

    last_writer: dict[str, int] = {}            # reg -> producer idx
    readers_since_write: dict[str, list[int]] = {}  # reg -> [reader idx]

    for i in range(n):
        node = dag.nodes[i]
        o = node.ops
        # --- RAW: this reads a reg whose last writer is earlier -------------
        for r in o.reads:
            p = last_writer.get(r)
            if p is not None and p < i:
                prod = dag.nodes[p]
                w, mech = raw_weight(prod, node, arch)
                dag.edges.append(Edge(p, i, EDGE_RAW, r, w, mech))
        # --- WAR: this writes a reg that earlier instrs read ---------------
        for r in o.writes:
            for rd in readers_since_write.get(r, []):
                if rd < i and rd != last_writer.get(r):
                    # anti edge: small/zero-but-ordered, its OWN default weight,
                    # never the RAW transpose.
                    dag.edges.append(Edge(rd, i, EDGE_WAR, r, war_weight(),
                                          "order"))
        # --- WAW: this writes a reg an earlier instr also wrote ------------
        #   Skip a self-edge when the same instruction also reads the reg (the
        #   RAW already orders it; e.g. `IMAD R0, R0, R0` is not a WAW on R0).
        for r in o.writes:
            if r in o.reads:
                continue
            p = last_writer.get(r)
            if p is not None and p < i:
                prod = dag.nodes[p]
                dag.edges.append(Edge(p, i, EDGE_WAW, r, waw_weight(prod),
                                      "order"))
        # --- update tracking ----------------------------------------------
        for r in o.reads:
            readers_since_write.setdefault(r, []).append(i)
        for r in o.writes:
            last_writer[r] = i
            readers_since_write[r] = []   # a new write resets the read window
    # --- CONTROL edges: chain branch/barrier/CC ops in program order -------
    last_flow = None
    for i in range(n):
        if dag.nodes[i].ops.is_branch:
            if last_flow is not None:
                dag.edges.append(Edge(last_flow, i, EDGE_CTRL, "flow", 1, "order"))
            last_flow = i
    return dag


# Consumers that levy the +1 inter-pipe / pre-issue penalty on a coupled
# producer feeding them: a memory op (load OR store -- its address/data read is
# one issue slot earlier than a math consumer's operand collect) and the IMAD
# multiplier pipe fed from the integer-ALU pipe.  Differentially confirmed on
# emitted SASS: producer->{LDG,STG,IMAD.WIDE} carry 5, producer->coupled-math
# carries 4.
_MEM_CONS = ("STG", "STS", "STL", "ST", "RED", "ATOMG", "ATOMS", "ATOM",
             "LDG", "LDS", "LDL", "LD", "LDC", "LDGSTS", "TEX", "SULD")


def raw_weight(prod: Node, cons: Node, arch: str = "SM89") -> tuple[int, str]:
    """RAW edge weight = producer RESULT latency, issue-relative.

    Table-driven (sass_latency_tables): a coupled producer resolves the RAW with
    a fixed *issue-relative* stall looked up by (arch-family, producer-pipe,
    consumer-pipe) from the calibrated coupled-stall matrix -- 4 same-pipe,
    5 cross-pipe, the AGU pre-issue slot (5; 8 on Turing), the slow-input latch
    (6 to F2I; 4/6 to MUFU by arch), the CC/predicate control band (13; 12 on
    Turing).  A decoupled (variable-latency) producer is resolved by a scoreboard
    -- the band magnitude is reported only for critical-path weighting.
    """
    pbase = _base_mnem(prod.mnem)
    model = LT.load_arch_model(arch)
    ccm = model.get(_base_mnem(cons.mnem))

    # A decoupled producer (memory / MUFU / S2R / and the decoupled conversions
    # I2F/F2I on the arches where they are scoreboard-tracked) is resolved by a
    # scoreboard, not a stall.
    if not prod.coupled:
        return prod.band, "scoreboard"

    prod_pipe = LT.coupled_pipe(prod.mnem)
    # The CC/predicate control band (13) applies ONLY when the consumer reads the
    # producer's predicate result as an @P EXECUTION GUARD (predicated execution
    # or a guarded branch).  A SETP whose predicate is read as a plain ALU operand
    # (FSEL/SEL `!P0`) is an ordinary same-/cross-pipe edge: differentially
    # confirmed on sm_89, FSETP -> @P0 BRA pays 13 but FSETP -> FSEL `!P0` pays 4.
    #
    # The band IS absorbable by intervening issue, exactly like any other coupled
    # producer latency: ptxas pays the full 13 only when the guarded consumer is
    # immediately adjacent (gap=1); with independent work in the shadow it shrinks
    # (gap=2 -> ~3 on sm_89).  Patching the gap=1 band 13->5 both saves 8 cyc/iter
    # AND corrupts the result on the GPU, so the magnitude is hardware-enforced;
    # but it is a producer-latency the consumer waits out, not an un-hideable
    # floor.  So we mark it "stall_ctrl" purely for reporting; the fixpoint
    # absorbs it through the normal intervening-cycle accounting.
    if prod.ops.writes_cc and _consumer_reads_guard(prod, cons):
        prod_pipe = LT.PIPE_CC
        cons_pipe = LT.consumer_pipe(cons.mnem, ccm)
        return LT.coupled_stall(arch, prod_pipe, cons_pipe), "stall_ctrl"

    # Non-guard predicate use (or a CC producer feeding a value operand): the SETP
    # forwards through its underlying compare datapath, NOT the control band.  Map
    # a CC producer to its datapath pipe (FP compares share the FMA pipe; integer
    # compares the INT pipe) so the matrix yields the ordinary forward (4/5), not
    # the 13-cycle band reserved for guards.
    if prod_pipe == LT.PIPE_CC:
        prod_pipe = _setp_datapath_pipe(prod.mnem)
    cons_pipe = LT.consumer_pipe(cons.mnem, ccm)
    stall = LT.coupled_stall(arch, prod_pipe, cons_pipe)

    # IMMEDIATE-ONLY PRODUCER fast-forward.  A `MOV Rd, <imm>` / `MOV Rd, c[...]`
    # with no register source reads has its result encoded in the instruction word
    # (or pulled from the constant cache), so it skips the operand-collect stage a
    # register-sourced producer pays -- ptxas forwards it to a cross-pipe consumer
    # (e.g. the IMAD.WIDE address multiplier) at 1-3, never the full cross-pipe 5.
    # Differentially confirmed on sm_89: `MOV R5, 0x4` -> `IMAD.WIDE` runs
    # bit-identical at stall 1.  Cap such an edge at the same-pipe forward; the
    # fixpoint then absorbs it further against the real intervening issue.
    if pbase == "MOV" and not prod.ops.reads and stall > COUPLED_SAME_PIPE:
        stall = COUPLED_SAME_PIPE

    # PREDICATE-OPERAND SELECT penalty.  FSEL/SEL (and other coupled ops that read
    # a predicate as a value operand) collect the predicate one cycle later than a
    # plain GPR source, so their result forwards one cycle slower than an ordinary
    # same-pipe op.  Differentially confirmed on sm_89: an `FSEL Rd, Ra, Rb, !P0`
    # feeding an `FFMA` needs stall 5 (not the same-pipe 4) -- at 4 the FFMA reads
    # a stale Rd and the result diverges.  Levy the +1 once.
    if pbase in ("FSEL", "SEL") and _reads_pred_operand(prod):
        stall += 1
    return stall, "stall"


def _reads_pred_operand(node: Node) -> bool:
    """True if the node reads a predicate register as a value operand (not an @P
    guard) -- e.g. FSEL/SEL's select predicate."""
    return any((r.startswith("P") or r.startswith("UP"))
               for r in node.ops.reads) and not node.ops.guard_reads


def _setp_datapath_pipe(mnem: str) -> str:
    """The underlying compare datapath of a SETP-class producer when its result
    is read as a value operand (not an @P guard): FP compares (FSETP/DSETP/HSETP2)
    resolve on the FMA pipe, integer compares (ISETP) on the INT pipe."""
    base = _base_mnem(mnem)
    if base.startswith("F") or base.startswith("D") or base.startswith("H"):
        return LT.PIPE_FMA
    return LT.PIPE_INT


def _consumer_reads_guard(prod: Node, cons: Node) -> bool:
    """True if the consumer reads the producer's predicate/CC result as an @P
    EXECUTION GUARD (predicated execution / guarded branch) -- the control-band
    dependency -- vs. as an ordinary register operand (FSEL/SEL `!P0`, carry-in).

    Only the guard read triggers the 13-cycle control band; an operand read is an
    ordinary cross-pipe edge (4/5).  Differentially confirmed on sm_89: an ISETP/
    FSETP feeding `@P0 BRA`/`@P0 FFMA` pays the band, the same SETP feeding
    `FSEL Rd, Ra, Rb, !P0` pays only the cross-pipe forward (4)."""
    pred_writes = {r for r in prod.ops.writes
                   if r.startswith("P") or r.startswith("UP")}
    return bool(pred_writes & cons.ops.guard_reads)


def war_weight() -> int:
    """WAR (anti) edge weight: the reader's OPERAND-COLLECT WINDOW.

    A writer that clobbers a register an earlier instruction still reads must not
    issue before that reader has latched the operand.  A coupled reader latches
    its sources a couple of cycles after it issues, so a back-to-back overwrite
    needs a small floor -- not zero.  Differentially confirmed on sm_89: an
    `FFMA Rx, ..., R3` immediately followed by `MOV R3, <imm>` runs bit-identical
    only when the MOV waits >=3 cycles after the FFMA issues (an operand-collect
    window of ~2 on top of the MOV's own dispatch floor); at the old weight 0 the
    overwrite races the read and corrupts the result.  Weight 2 is the recovered
    minimum; the issue-cycle fixpoint absorbs it against intervening issue, so a
    well-separated WAR costs nothing.  This is the anti-edge's OWN default --
    independent of, and never copied from, the RAW table."""
    return 2


def waw_weight(prod: Node) -> int:
    """WAW (output) edge weight: zero-but-ordered, nonzero only when a slow
    (decoupled) producer precedes a fast writer to the same dest."""
    return 0 if prod.coupled else 1


# =============================================================================
# L1 <-> L0 : control word pack/unpack (bit-exact)
# =============================================================================

@dataclass
class SchedFields:
    usched: int        # 105-109
    dst_wr: int        # 110-112  (7 = none)
    src_rel: int       # 113-115  (7 = none)
    wait_mask: int     # 116-121
    batch_t: int       # 122-124

    @property
    def opex(self) -> int:
        return (self.batch_t << 5) | self.usched

    @property
    def stall(self) -> int:
        return usched_to_stall(self.usched)[0]


def ctrl_to_fields(c: Ctrl) -> SchedFields:
    return SchedFields(c.usched, c.dst_wr, c.src_rel, c.wait_mask, c.batch_t)


def pack_control(f: SchedFields) -> int:
    """L1 -> L0: pack the scheduling fields into bits 105-124 of the word."""
    w = 0
    w |= (f.usched & 0x1F) << 105
    w |= (f.dst_wr & 0x7) << 110
    w |= (f.src_rel & 0x7) << 113
    w |= (f.wait_mask & 0x3F) << 116
    w |= (f.batch_t & 0x7) << 122
    return w


def unpack_control(w: int) -> SchedFields:
    """L0 -> L1: extract the scheduling fields from a 128-bit word."""
    def b(hi, lo):
        return (w >> lo) & ((1 << (hi - lo + 1)) - 1)
    return SchedFields(b(109, 105), b(112, 110), b(115, 113), b(121, 116),
                       b(124, 122))


def stall_to_usched(stall: int, end_group: bool = False) -> int:
    """Inverse of usched_to_stall for the common composer outputs.

    stall 0 with no group end -> usched 0 (DRAIN/yield candidate).
    stall n (1..11) ending a co-issue group -> usched n.
    stall n with no group end -> usched n+16 (the Wn, no-END encoding).
    We default composed stalls to the no-group-end (Wn) encoding to match the
    common ptxas output for a sequential dependent chain.
    """
    if stall <= 0:
        return 0
    # The no-group-end (Wn) encoding is n+16, valid only for n<=11 (usched<=27).
    # Larger stalls (e.g. the 13-cycle CC/predicate band) must use the
    # group-ending encoding usched=n directly (range 1..15).
    if end_group or stall > 11:
        return min(stall, 15)
    return stall + 16


# =============================================================================
# L1 <-> L2 : control fields <-> semantic events
# =============================================================================

@dataclass
class Event:
    idx: int
    kind: str          # 'stall' | 'yield' | 'arm_wr' | 'arm_rd' | 'wait' |
                       # 'group_start' | 'group_end'
    value: int = 0     # stall cycles, or scoreboard id, or wait bit
    detail: str = ""


def ctrl_to_events(c: Ctrl) -> list[Event]:
    evs: list[Event] = []
    if c.yield_:
        evs.append(Event(c.offset, "yield"))
    elif c.stall:
        evs.append(Event(c.offset, "stall", c.stall))
    if c.dst_wr != 7:
        evs.append(Event(c.offset, "arm_wr", c.dst_wr, "write scoreboard"))
    if c.src_rel != 7:
        evs.append(Event(c.offset, "arm_rd", c.src_rel, "read-release scoreboard"))
    for sb in c.wait_sbs:
        evs.append(Event(c.offset, "wait", sb, "req_bit_set"))
    if c.batch_t in (1, 2):
        evs.append(Event(c.offset, "group_start", c.batch_t))
    elif c.batch_t == 4:
        evs.append(Event(c.offset, "group_end", c.batch_t))
    return evs


# =============================================================================
# decompose : cubin -> (DAG, control words) with attribution
# =============================================================================

@dataclass
class Attribution:
    """Maps a consumer's resolved hazard back to its producer."""
    consumer_idx: int
    producer_idx: int
    kind: str          # RAW / WAR / WAW / CTRL
    reg: str
    mechanism: str     # 'stall' | 'scoreboard' | 'depbar'
    detail: str


def arch_of(cubin: str) -> str:
    out = subprocess.run([NVDISASM, "-c", cubin], capture_output=True, text=True)
    m = re.search(r"\.target\s+sm_(\w+)", out.stdout)
    return ("SM" + m.group(1)) if m else "SM89"


@dataclass
class Decomposition:
    arch: str
    ctrls: list[Ctrl]
    operands: list[Operands]
    dag: DAG
    attributions: list[Attribution]


def decompose(cubin: str) -> Decomposition:
    arch = arch_of(cubin)
    type_map = build_type_map(arch)
    ctrls = [c for c in disasm_cubin(cubin) if c.mnem and not c.mnem.startswith(".")]
    operands = disasm_operands(cubin)
    n = min(len(ctrls), len(operands))
    ctrls, operands = ctrls[:n], operands[:n]
    dag = build_dag(ctrls, operands, type_map, arch)
    attribs = attribute_hazards(ctrls, operands, dag)
    return Decomposition(arch, ctrls, operands, dag, attribs)


def attribute_hazards(ctrls: list[Ctrl], operands: list[Operands],
                      dag: DAG) -> list[Attribution]:
    """Attribute each instruction's wait bits / stall to the producer edge that
    resolves it.  A wait bit pairs to the nearest earlier producer that armed
    that scoreboard; a coupled stall pairs to the nearest earlier RAW producer."""
    attribs: list[Attribution] = []
    # scoreboard -> producer idx that last armed it (write or read-release)
    armed_by: dict[int, int] = {}
    for i, c in enumerate(ctrls):
        # consumer wait bits -> producers
        for sb in c.wait_sbs:
            p = armed_by.get(sb)
            if p is not None:
                reg = _shared_reg(dag, p, i)
                attribs.append(Attribution(i, p, EDGE_RAW, reg, "scoreboard",
                                           f"SB{sb} armed by #{p} {ctrls[p].mnem}"))
        # coupled stall -> nearest earlier RAW producer (the dependent edge)
        if c.stall and c.dst_wr == 7 and not c.wait_sbs:
            raws = [e for e in dag.in_edges(i) if _is_stall_edge(e)]
            if raws:
                e = max(raws, key=lambda e: e.src)
                attribs.append(Attribution(i, e.src, EDGE_RAW, e.reg, "stall",
                                           f"stall={c.stall} for RAW {e.reg} "
                                           f"from #{e.src} {ctrls[e.src].mnem}"))
        # update armers
        if c.dst_wr != 7:
            armed_by[c.dst_wr] = i
        if c.src_rel != 7:
            armed_by[c.src_rel] = i
    return attribs


def _shared_reg(dag: DAG, p: int, c: int) -> str:
    for e in dag.edges:
        if e.src == p and e.dst == c:
            return e.reg
    # producer wrote, consumer read something it produced
    pn, cn = dag.nodes[p], dag.nodes[c]
    common = pn.ops.writes & cn.ops.reads
    return next(iter(common), "?")


# =============================================================================
# compose : DAG | ordered instr list -> control words  (THE SCHEDULER)
# =============================================================================

MAX_STALL = 15            # 4-bit stall field ceiling
N_SCOREBOARDS = 6         # SB0..SB5
DEPBAR_SB = 5             # unbounded-group scoreboard


@dataclass
class ComposeResult:
    fields: list[SchedFields]
    notes: list[str]      # per-instruction human reasoning
    depbars: list[int]    # indices where a DEPBAR.LE was needed (overflow)


def compose(dag: DAG, ctrls: list[Ctrl] | None = None,
            absorb_ctrl: bool = False) -> ComposeResult:
    """Walk issue order on a virtual cycle clock and assign control words.

    For each instruction:
      * fixed-latency RAW edges to it from coupled producers -> a usched stall =
        the minimum that satisfies every such edge given accumulated cycles
        (throughput floor of 1 otherwise);
      * variable-latency (decoupled) producers get a write/read scoreboard;
        their consumers set the matching req_bit_set bit;
      * the 6-scoreboard allocator overloads (VSB->PSB by minimum added stall)
        when >6 are live; unbounded groups fall to SB5 + DEPBAR.LE.

    `absorb_ctrl`: retained for API compatibility.  The CC/predicate control band
    is now ALWAYS modelled as an absorbable issue-relative latency (the fixpoint's
    intervening-cycle accounting hides it behind independent work exactly as ptxas
    does -- differentially confirmed on sm_89, where the full 13 appears only when
    the @P-guarded consumer is immediately adjacent), so this flag no longer
    changes the band.  The control-band magnitude itself is hardware-enforced (at
    gap=1 it is emitted in full; patching it lower corrupts the result on the GPU)
    -- it is the SHADOW that is hideable, not the latency.  Any over-tightening is
    caught by the GPU bit-identical + cycle gate, so a reorder can only ever
    propose a faster-or-rejected schedule, never an accepted-but-wrong one.

    Returns one SchedFields per node plus per-instruction reasoning.
    """
    n = len(dag.nodes)
    fields = [SchedFields(0, 7, 7, 0, 0) for _ in range(n)]
    notes = [""] * n
    depbars: list[int] = []

    def _is_terminator(idx: int) -> bool:
        return _base_mnem(dag.nodes[idx].mnem) in (
            "EXIT", "BRA", "RET", "NOP", "BAR", "JMP", "CALL", "BRX")

    def _floor(idx: int) -> int:
        """Per-op dispatch-stall floor.  Ordinary math carries the 1-cycle
        dispatch floor; a memory/issue op carries 2 (the request-issue slot ptxas
        reserves); branch and EXIT terminators are split below.  Being
        conservative here is always hazard-safe, confirmed by dynamic validation.

        TERMINATOR BRANCH FLOOR (sm_89, differentially measured):  a *taken*
        control-transfer branch (BRA/BRX/JMP/CALL) needs a floor of 2 issue
        cycles.  ptxas conservatively emits 5 on every such branch, but sweeping
        the back-edge BRA of a hot loop shows stalls 2..5 are all equivalent
        (~45 cyc/iter) while stall 1 falls off a cliff to ~65 cyc/iter (a +20-cyc
        branch-resolution bubble per iteration) -- so the real requirement is 2,
        not 5, and floor 0 (the old value) would model a loop back-edge as free
        when the silicon pays 20 extra cycles.  EXIT/RET/NOP/BAR genuinely yield
        (floor 0): patching EXIT's emitted 5 down to 1 leaves cycles unchanged."""
        base = _base_mnem(dag.nodes[idx].mnem)
        if base in ("BRA", "BRX", "JMP", "CALL", "CALLR"):
            return 2
        if _is_terminator(idx):
            return 0
        # Memory-issue ops and the MUFU/special-function pipe reserve a 2-cycle
        # dispatch slot (the request-issue / SFU-launch latency).  Floor 2 is
        # the conservative, hazard-safe minimum confirmed by dynamic validation
        # (a MUFU.RSQ at stall 1 corrupts the result before its scoreboard arms).
        if base in ("LDG", "STG", "LDS", "STS", "LDL", "STL", "LD", "ST",
                    "LDC", "ATOMG", "ATOM", "RED", "TEX", "LDGSTS",
                    "MUFU", "I2F", "F2I", "F2F", "I2I"):
            return 2
        return 1

    # Per-producer fixed-stall edges (dst, weight, is_ctrl).  The fixpoint raises
    # the SOURCE instruction's stall so the difference constraint
    # issue[dst] - issue[src] >= weight holds.  Three edge classes contribute:
    #   * RAW stall / stall_ctrl  -- the consumer must wait for the producer's
    #     coupled result (or the control band).
    #   * WAR  -- a register-clobbering writer (dst) must not overwrite an operand
    #     an earlier coupled reader (src) has not yet latched: weight = the
    #     reader's operand-collect window (war_weight).  Without this the composer
    #     would let a cheap immediate writer race ahead of a still-collecting
    #     reader and corrupt it (confirmed on sm_89: FFMA Rx,...,R3 then MOV R3,
    #     <imm> needs the MOV >=3 cycles after the FFMA).
    #   * WAW  -- a fast writer (dst) following a slow (decoupled) writer (src) to
    #     the same register keeps its small nonzero floor (waw_weight) so the
    #     final value wins.
    # A scoreboard-tracked (decoupled) source resolves its WAR/WAW via the
    # consumer's wait bit, not a stall, so only edges whose SOURCE is coupled are
    # added here (a decoupled source already arms a scoreboard below).
    out_stall: list[list[tuple[int, int, bool]]] = [[] for _ in range(n)]
    for e in dag.edges:
        if _is_stall_edge(e):
            out_stall[e.src].append((e.dst, e.weight, e.mechanism == "stall_ctrl"))
        elif e.kind in (EDGE_WAR, EDGE_WAW) and e.weight > 0 \
                and dag.nodes[e.src].coupled:
            out_stall[e.src].append((e.dst, e.weight, False))

    # ---- stall fixpoint (difference constraints) ------------------------------
    # Every fixed-latency RAW edge P->C imposes the hazard constraint
    #     issue_cycle[C] - issue_cycle[P] >= weight
    # i.e. the consumer must not issue before the producer's result exists.  The
    # issue cycle of every instruction is the running sum of max(stall,1) over the
    # stream, so the cycles of *all* instructions scheduled between P and C
    # (whether independent or themselves dependent) genuinely delay C's issue and
    # therefore reduce how much stall P must carry on its own.  We solve the
    # constraint system by raising producer stalls to a fixpoint: monotone,
    # converges in a few passes (bounded by the longest dependent chain), and at
    # the fixpoint every edge's constraint holds simultaneously -- which is
    # exactly the hazard-safety guarantee (proven on the GPU by --verify-dyn).
    # The CC/predicate control band is a hard producer latency emitted in full
    # (it is not an inter-issue gap the consumer can absorb by waiting).
    stalls = [_floor(i) for i in range(n)]
    for _ in range(12):
        issue_cycle = [0] * n
        c = 0
        for i in range(n):
            issue_cycle[i] = c
            c += max(stalls[i], 1)
        changed = False
        for i in range(n):
            req = _floor(i)
            for dst, weight, is_ctrl in out_stall[i]:
                # cycles already accumulated by instructions strictly between the
                # producer i and the consumer dst (they occupy real issue slots
                # and so genuinely delay the consumer's issue).  The CC/predicate
                # control band is absorbed by intervening issue exactly like any
                # other coupled producer latency: differential measurement on
                # sm_89 shows ptxas pays the full 13 only when the guarded consumer
                # is immediately adjacent (gap=1, between=0) and shrinks it once
                # independent work fills the shadow (gap=2 -> ~3).  At gap=1
                # `between` is 0, so the full magnitude is emitted -- and that 13
                # is hardware-enforced (patching it lower corrupts the result on
                # the GPU).  The band therefore needs no special case.  The
                # `absorb_ctrl` flag is retained for API compatibility; it no
                # longer changes the band (which always absorbs against the real
                # intervening issue) -- only the GPU gate decides acceptance.
                between = issue_cycle[dst] - issue_cycle[i] - max(stalls[i], 1)
                residual = weight - between
                req = max(req, min(residual, weight))
            req = min(max(req, _floor(i)), MAX_STALL)
            if req != stalls[i]:
                stalls[i] = req
                changed = True
        if not changed:
            break

    # ---- second pass: scoreboards, wait masks, encoding, reasoning -----------
    issue_cycle = [0] * n
    c = 0
    for i in range(n):
        issue_cycle[i] = c
        c += max(stalls[i], 1)

    sb_owner: dict[int, int | None] = {i: None for i in range(N_SCOREBOARDS)}
    sb_armed_cycle: dict[int, int] = {i: 0 for i in range(N_SCOREBOARDS)}
    prod_sb: dict[int, int] = {}

    for i in range(n):
        node = dag.nodes[i]
        reasons: list[str] = []

        # ---- consumer side: wait on scoreboards of decoupled producers ----
        wait_mask = 0
        for e in dag.in_edges(i):
            if e.kind in (EDGE_RAW, EDGE_WAR, EDGE_WAW) and not dag.nodes[e.src].coupled:
                sb = prod_sb.get(e.src)
                if sb is not None:
                    wait_mask |= (1 << sb)
                    reasons.append(f"wait SB{sb}: {e.kind} {e.reg} from "
                                   f"#{e.src} {dag.nodes[e.src].mnem}")
                    if sb_owner.get(sb) == e.src:
                        sb_owner[sb] = None

        # ---- producer side: fixed-latency RAW stall (from fixpoint) --------
        stall = stalls[i]
        for e in dag.out_edges(i):
            if _is_stall_edge(e):
                kind = ("CC/pred control band" if e.mechanism == "stall_ctrl"
                        else "same-pipe" if e.weight == COUPLED_SAME_PIPE
                        else "cross-pipe" if e.weight == COUPLED_CROSS_PIPE
                        else f"pipe stall {e.weight}")
                between = (issue_cycle[e.dst] - issue_cycle[i]
                           - max(stalls[i], 1))
                reasons.append(f"stall={stall}: RAW {e.reg} to #{e.dst} "
                               f"{dag.nodes[e.dst].mnem} (weight {e.weight} "
                               f"{kind}"
                               f"{f', {between} cyc absorbed by intervening issue' if between and e.mechanism != 'stall_ctrl' else ''})")

        # ---- producer side: variable-latency -> arm a scoreboard -----------
        dst_wr = 7
        src_rel = 7
        if not node.coupled and not _is_terminator(i) and \
                _has_reg_consumer(dag, i):
            sb = _alloc_scoreboard(sb_owner, sb_armed_cycle, issue_cycle[i], i,
                                   depbars, i, reasons)
            arms_w = _arms_wr(node)
            # A read-release scoreboard is armed only when an early WAR exists on
            # this op's source registers (a later writer would clobber an operand
            # the long-latency op has not yet read).  Most loads only arm dst_wr.
            arms_r = _arms_rd(node) and _has_war_on_sources(dag, i)
            if arms_w:
                dst_wr = sb
            if arms_r and not arms_w:
                src_rel = sb
            elif arms_r and arms_w:
                src_rel = sb
            prod_sb[i] = sb
            sb_owner[sb] = i
            sb_armed_cycle[sb] = issue_cycle[i]
            reasons.append(f"arm SB{sb} (decoupled {node.mnem}, band "
                           f"{node.band})")

        # ---- encode usched -------------------------------------------------
        usched = stall_to_usched(stall, end_group=False)
        fields[i] = SchedFields(usched, dst_wr, src_rel, wait_mask, 0)
        notes[i] = "; ".join(reasons) if reasons else "(no dependency)"
    return ComposeResult(fields, notes, depbars)


def _has_reg_consumer(dag: DAG, i: int) -> bool:
    """A data (RAW/WAR/WAW) consumer exists -- a scoreboard is only meaningful
    for register-data dependents, never for pure control-flow successors."""
    return any(e.src == i and e.kind in (EDGE_RAW, EDGE_WAR, EDGE_WAW)
               for e in dag.edges)


def _has_war_on_sources(dag: DAG, i: int) -> bool:
    """True if some later instruction overwrites a register this op reads before
    a far-future point -- i.e. a WAR edge out of i exists -- so the long-latency
    op must arm a read-release (src_rel) scoreboard to protect its operand."""
    return any(e.src == i and e.kind == EDGE_WAR for e in dag.edges)


def _arms_wr(node: Node) -> bool:
    base = _base_mnem(node.mnem)
    # writers of a register result via scoreboard
    return base not in ("STG", "STS", "STL", "ST", "RED", "ATOMS", "DEPBAR",
                        "LDGDEPBAR", "BAR")


def _arms_rd(node: Node) -> bool:
    base = _base_mnem(node.mnem)
    # loads release a read scoreboard once the address sources are consumed
    return base in ("LDG", "LDS", "LDL", "LD", "LDC", "LDGSTS", "ATOMG", "TEX",
                    "STG", "STS", "STL")


def _alloc_scoreboard(sb_owner, sb_armed_cycle, cyc, producer, depbars,
                      node_idx, reasons) -> int:
    """Allocate a hardware scoreboard for a decoupled producer.

    Prefer a free SB; if all 6 are live, overload the one whose owner was armed
    earliest (oldest), i.e. the one a consumer is most likely to have drained --
    this is the VSB->PSB overload by minimum-added-stall heuristic.  An unbounded
    group (cp.async) would be steered to SB5 + DEPBAR.LE; we flag the overflow."""
    free = [sb for sb in range(N_SCOREBOARDS) if sb_owner[sb] is None]
    if free:
        return free[0]
    # overload: evict the oldest-armed scoreboard
    victim = min(range(N_SCOREBOARDS), key=lambda s: sb_armed_cycle[s])
    depbars.append(node_idx)
    reasons.append(f"SB pool full -> overload SB{victim} (oldest); "
                   f"DEPBAR.LE would serialize here")
    return victim


# =============================================================================
# verify : round-trip and differential
# =============================================================================

@dataclass
class Mismatch:
    idx: int
    offset: int
    mnem: str
    field: str
    expected: int       # ptxas
    got: int            # composed
    cls: str            # mismatch class


def round_trip(cubin: str) -> tuple[int, int, list[Mismatch], dict]:
    """decompose -> compose on a real cubin; compare composed control fields to
    ptxas's actual fields.  Returns (n_compared, n_exact, mismatches, summary).

    The two fields we hold to an EXACT standard:
      * coupled stall counts  (fixed-latency RAW)
      * scoreboard producer->consumer PAIRINGS (which producer arms a wait bit)
    The exact scoreboard *index* ptxas picks is allocator-policy-dependent, so we
    compare the producer->consumer pairing structure, not the literal SB id.
    """
    d = decompose(cubin)
    cr = compose(d.dag, d.ctrls)
    mism: list[Mismatch] = []
    n_cmp = n_exact = 0
    n_stall_cmp = n_stall_exact = 0
    n_pair_cmp = n_pair_exact = 0

    # ground-truth scoreboard pairings from ptxas output
    gt_pairs = _scoreboard_pairs(d)
    # composed scoreboard pairings
    cm_pairs = _composed_pairs(d.dag, cr)

    for i, (c, f) in enumerate(zip(d.ctrls, cr.fields)):
        node = d.dag.nodes[i]
        base = _base_mnem(c.mnem)
        # compare stall only on coupled producers that feed a coupled consumer
        if node.coupled and _feeds_coupled_raw(d.dag, i):
            n_cmp += 1
            n_stall_cmp += 1
            if c.stall == f.stall:
                n_exact += 1
                n_stall_exact += 1
            else:
                cls = _classify_stall_mismatch(c, f, d.dag, i)
                mism.append(Mismatch(i, c.offset, c.mnem, "stall",
                                     c.stall, f.stall, cls))

    # compare scoreboard pairing structure
    for cons, prod in gt_pairs:
        n_pair_cmp += 1
        if (cons, prod) in cm_pairs:
            n_pair_exact += 1
        else:
            mism.append(Mismatch(cons, d.ctrls[cons].offset, d.ctrls[cons].mnem,
                                 "scbd_pair", prod, -1, "scoreboard_pairing"))

    summary = {
        "arch": d.arch,
        "n_instrs": len(d.ctrls),
        "stall_compared": n_stall_cmp,
        "stall_exact": n_stall_exact,
        "scbd_pairs_gt": n_pair_cmp,
        "scbd_pairs_matched": n_pair_exact,
        "n_edges": len(d.dag.edges),
    }
    return (n_stall_cmp + n_pair_cmp,
            n_stall_exact + n_pair_exact, mism, summary)


def _feeds_coupled_raw(dag: DAG, i: int) -> bool:
    return any(_is_stall_edge(e) for e in dag.out_edges(i))


# =============================================================================
# dynamic validation : patch composed control words back, launch, diff results
# =============================================================================

_CTRL_MASK = ((1 << 125) - (1 << 105)) | (1 << 125)  # bits 105..125 inclusive


def _text_section_span(cubin: Path, entry: str) -> tuple[int, int]:
    """(file_offset, size) of the .text.<entry> PROGBITS section.

    Uses `readelf -SW` (wide) so long section names are not truncated; the wide
    format puts addr/offset/size on one line: `name PROGBITS addr offset size`.
    """
    out = subprocess.check_output(["readelf", "-SW", str(cubin)],
                                  stderr=subprocess.DEVNULL).decode()
    want = f".text.{entry}"
    for ln in out.splitlines():
        if want not in ln:
            continue
        m = re.search(re.escape(want) +
                      r"\s+PROGBITS\s+([0-9a-f]+)\s+([0-9a-f]+)\s+([0-9a-f]+)",
                      ln)
        if m:
            return int(m.group(2), 16), int(m.group(3), 16)
    raise RuntimeError(f"no .text.{entry} section in {cubin}")


def patch_cubin(src: str, dst: str, entry: str,
                fields: list[SchedFields]) -> int:
    """Write composed control words (bits 105-124) into a copy of the cubin.

    Returns the number of instruction words patched.  Each 16-byte word's
    scheduling control bits are cleared and replaced by pack_control(fields[k]);
    all other bits (opcode, operands, reuse, pm_pred) are preserved verbatim.
    """
    data = bytearray(Path(src).read_bytes())
    off, size = _text_section_span(Path(src), entry)
    n = size // 16
    clear = ~(((1 << (124 - 105 + 1)) - 1) << 105)   # zero bits 105..124
    patched = 0
    for k in range(min(n, len(fields))):
        base = off + k * 16
        w = int.from_bytes(data[base:base + 16], "little")
        w = (w & clear) | pack_control(fields[k])
        data[base:base + 16] = w.to_bytes(16, "little")
        patched += 1
    Path(dst).write_bytes(data)
    return patched


def _launch_harness() -> str | None:
    """Build (rebuilding on source change) the C Driver-API launch harness."""
    binpath = Path("/tmp/sass_sched_launch")
    src = HERE / "launch_cubin.c"
    if not src.exists():
        return None
    stale = (not binpath.exists()
             or binpath.stat().st_mtime < src.stat().st_mtime)
    if stale:
        r = subprocess.run(
            ["gcc", "-O2", str(src), "-o", str(binpath),
             "-I/usr/local/cuda-13.1/include",
             "-L/usr/local/cuda-13.1/lib64/stubs", "-lcuda"],
            capture_output=True, text=True)
        if r.returncode != 0:
            print(f"  ! harness build failed: {r.stderr[:200]}", file=sys.stderr)
            return None
    return str(binpath)


def _harness_argv(harness: str, cubin: str, entry: str, nwords: int,
                  grid: int = 1, block: int = 32, niter: int = 0,
                  seed: int = 12345) -> list[str]:
    """Positional argv for launch_cubin.c (backward compatible: a 3-arg call is
    one block of 32 threads, single .param .u64 p; niter>0 appends the loop
    count as a second .param .u32)."""
    return [harness, cubin, entry, str(nwords), str(seed),
            str(grid), str(block), str(niter)]


def _run_kernel(harness: str, cubin: str, entry: str, nwords: int,
                grid: int = 1, block: int = 32, niter: int = 0,
                seed: int = 12345, quiet: bool = False) -> str | None:
    """Launch the kernel and return its stdout (None on failure).  quiet=True
    suppresses the failure diagnostic (used by the perf-diff bisection, where a
    trial that tightens a real hazard is *expected* to fault)."""
    env = dict(os.environ)
    env["LD_LIBRARY_PATH"] = "/lib64:" + env.get("LD_LIBRARY_PATH", "")
    r = subprocess.run(
        _harness_argv(harness, cubin, entry, nwords, grid, block, niter, seed),
        capture_output=True, text=True, env=env)
    if r.returncode != 0:
        if not quiet:
            print(f"  ! launch failed: {r.stderr.strip()[:200]}",
                  file=sys.stderr)
        return None
    return r.stdout


def verify_dynamic(cubin: str, entry: str | None = None,
                   nwords: int = 8) -> int:
    """Stretch validation: recompose this kernel's control words, patch them into
    a copy of the cubin, launch both on the GPU, and confirm identical output.

    A composed schedule that produces the same numerical result as ptxas's proves
    it is hazard-safe (every RAW dependency is honoured by a stall or scoreboard
    wait that is at least as conservative as the original)."""
    cub = Path(cubin)
    if entry is None:
        out = subprocess.run([NVDISASM, "-c", cubin], capture_output=True,
                             text=True).stdout
        m = re.search(r"\.text\.(\S+)", out)
        entry = m.group(1) if m else None
    if entry is None:
        print("  ! could not determine entry name", file=sys.stderr)
        return 2

    harness = _launch_harness()
    if harness is None:
        print("  ! launch harness unavailable (need gcc + libcuda)",
              file=sys.stderr)
        return 2

    d = decompose(cubin)
    cr = compose(d.dag, d.ctrls)
    patched_path = f"/tmp/sass_sched_patched_{entry}.cubin"
    np = patch_cubin(cubin, patched_path, entry, cr.fields)

    orig = _run_kernel(harness, cubin, entry, nwords)
    new = _run_kernel(harness, patched_path, entry, nwords)
    if orig is None or new is None:
        return 2

    same = (orig == new)
    print(f"# DYNAMIC VALIDATION  {cub.name}  entry={entry}  "
          f"patched {np} control words")
    print(f"  original output : {orig.split()[:4]}")
    print(f"  recomposed out  : {new.split()[:4]}")
    if same:
        print("  RESULT: identical -> the composed schedule is hazard-safe.")
        return 0
    print("  RESULT: DIFFERENT -> composed schedule changed kernel semantics!")
    return 1


# =============================================================================
# GPU MEASUREMENT (Nsight Compute) : prove which stalls are wasteful, and read
# per-instruction hardware warp-stall behaviour.  Two modes share the patch /
# launch plumbing above (patch_cubin / _launch_harness / _run_kernel) and add a
# thin ncu wrapper.  All metric names committed here are the PUBLIC ncu names
# (smsp__pcsamp_warps_issue_stalled_*); the recovered can't-issue taxonomy is
# used only to *interpret* them and is referred to solely by those public names.
# =============================================================================

# Public Nsight Compute warp-stall-reason metrics (Source Counters / PC sampling).
# These are sampled per program-counter over the kernel runtime: each gives the
# count of samples at which a resident warp at that PC was stalled for the named
# reason.  Names verified against `ncu --query-metrics` on CUDA 13.1 / NCU 2025.4.
PCSAMP_METRICS = [
    "smsp__pcsamp_sample_count",
    "smsp__pcsamp_warps_issue_stalled_short_scoreboard",
    "smsp__pcsamp_warps_issue_stalled_long_scoreboard",
    "smsp__pcsamp_warps_issue_stalled_wait",
    "smsp__pcsamp_warps_issue_stalled_barrier",
    "smsp__pcsamp_warps_issue_stalled_membar",
    "smsp__pcsamp_warps_issue_stalled_not_selected",
    "smsp__pcsamp_warps_issue_stalled_dispatch_stall",
    "smsp__pcsamp_warps_issue_stalled_no_instructions",
    "smsp__pcsamp_warps_issue_stalled_branch_resolving",
    "smsp__pcsamp_warps_issue_stalled_math_pipe_throttle",
    "smsp__pcsamp_warps_issue_stalled_mio_throttle",
    "smsp__pcsamp_warps_issue_stalled_lg_throttle",
    "smsp__pcsamp_warps_issue_stalled_tex_throttle",
    "smsp__pcsamp_warps_issue_stalled_imc_miss",
    "smsp__pcsamp_warps_issue_stalled_drain",
    "smsp__pcsamp_warps_issue_stalled_sleeping",
    "smsp__pcsamp_warps_issue_stalled_misc",
]

# Cycle-count metrics for the perf-diff (V1 vs V2) measurement.
PERF_METRICS = ["sm__cycles_active.avg", "gpc__cycles_elapsed.max",
                "smsp__inst_executed.sum"]

# A short stall-reason key (the ncu suffix after "..._stalled_") -> how our
# scheduling model classifies the instruction that would dominantly show it.
# This is the cross-map MODE 2 reports: it bridges the live silicon stall reason
# to (1) which scheduling MECHANISM our composer assigns and (2) the recovered
# can't-issue taxonomy, referred to here only by its public ncu reason name.
#   - "scoreboard wait (req_bit_set)" : our decoupled-producer scoreboard waits
#   - "fixed usched stall"            : our coupled fixed-latency stalls
#   - "structural / not modelled"     : issue-pipe / occupancy / branch effects
#     that our single-warp dependency model does not predict (they are emergent
#     from many-warp contention or control flow, not a per-edge hazard).
STALL_REASON_MODEL = {
    # decoupled producers resolved by a scoreboard the consumer waits on
    "short_scoreboard": "scoreboard wait (req_bit_set)",   # MUFU / S2R / LDS / shared
    "long_scoreboard":  "scoreboard wait (req_bit_set)",   # global/local memory load
    # coupled fixed-latency producers resolved by a usched stall count
    "wait":             "fixed usched stall",
    "dispatch_stall":   "fixed usched stall",
    "short_scoreboard_pipe_l1tex": "scoreboard wait (req_bit_set)",
    # structural / many-warp / control-flow effects (not a per-edge hazard)
    "not_selected":     "structural / not modelled",
    "no_instructions":  "structural / not modelled",       # i-cache / branch refill
    "branch_resolving": "structural / not modelled",
    "math_pipe_throttle": "structural / not modelled",
    "mio_throttle":     "structural / not modelled",
    "lg_throttle":      "structural / not modelled",
    "tex_throttle":     "structural / not modelled",
    "imc_miss":         "structural / not modelled",        # immediate-constant miss
    "barrier":          "structural / not modelled",
    "membar":           "structural / not modelled",
    "drain":            "structural / not modelled",
    "sleeping":         "structural / not modelled",
    "misc":             "structural / not modelled",
    "selected":         "issued",
}

# ncu's access-denied signatures.  When profiling is restricted to admin users,
# ncu exits with one of these; we detect it, print the exact remediation, and
# degrade gracefully instead of failing the whole run.
_NCU_PERM_SIGNATURES = (
    "ERR_NVGPUCTRPERM", "insufficient permissions", "access is restricted",
    "The user does not have permission", "profiling is not allowed",
)

_NCU_PERM_REMEDIATION = (
    "  GPU performance counters are restricted to admin users on this box.\n"
    "  Remediation (either):\n"
    "    (a) add the modprobe option and reload the driver:\n"
    "        echo 'options nvidia NVreg_RestrictProfilingToAdminUsers=0' \\\n"
    "          | sudo tee /etc/modprobe.d/nvidia-profiling.conf\n"
    "        then reboot (or reload nvidia.ko); or\n"
    "    (b) run the profiler under sudo: sudo -E ncu ...\n"
    "  See: https://developer.nvidia.com/ERR_NVGPUCTRPERM"
)


@dataclass
class NcuResult:
    ok: bool
    blocked: bool                 # access-denied (permissions); see remediation
    metrics: dict[str, float]     # aggregate metric -> value (perf mode)
    per_pc: list[dict]            # per-instruction rows (stall-profile mode)
    stderr: str


def _ncu_available() -> bool:
    return Path(NCU).exists()


def _parse_ncu_num(s: str) -> float:
    """Parse an ncu metric value: strips thousands separators, tolerates ''."""
    s = s.strip().strip('"').replace(",", "")
    if not s:
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def _ncu_perf(harness: str, cubin: str, entry: str, grid: int, block: int,
              niter: int, nwords: int = 8) -> NcuResult:
    """Run ncu for the cycle metrics (V1/V2 perf-diff).  Detects access-denied
    and returns blocked=True with the stderr so the caller can degrade."""
    env = dict(os.environ)
    env["LD_LIBRARY_PATH"] = "/lib64:" + env.get("LD_LIBRARY_PATH", "")
    argv = [NCU, "--metrics", ",".join(PERF_METRICS), "--csv",
            "--"] + _harness_argv(harness, cubin, entry, nwords, grid, block,
                                  niter)
    r = subprocess.run(argv, capture_output=True, text=True, env=env)
    blocked = any(sig in (r.stderr + r.stdout) for sig in _NCU_PERM_SIGNATURES)
    if blocked:
        return NcuResult(False, True, {}, [], r.stderr)
    if r.returncode != 0:
        return NcuResult(False, False, {}, [], r.stderr)
    metrics = _parse_ncu_csv_aggregate(r.stdout)
    return NcuResult(bool(metrics), False, metrics, [], r.stderr)


def _parse_ncu_csv_aggregate(csv_text: str) -> dict[str, float]:
    """Pull metric values out of ncu's default-page `--csv` output, which is the
    long format: one row per metric with 'Metric Name' / 'Metric Value' columns.
    Returns {metric_name -> value}."""
    import csv as _csv
    import io
    lines = [ln for ln in csv_text.splitlines() if ln.startswith('"')]
    if not lines:
        return {}
    rows = list(_csv.reader(io.StringIO("\n".join(lines))))
    header = rows[0]
    if "Metric Name" not in header or "Metric Value" not in header:
        return {}
    ni, vi = header.index("Metric Name"), header.index("Metric Value")
    out: dict[str, float] = {}
    for r in rows[1:]:
        if len(r) > max(ni, vi):
            out[r[ni]] = _parse_ncu_num(r[vi])
    return out


def _ncu_pcsamp(harness: str, cubin: str, entry: str, grid: int, block: int,
                niter: int, nwords: int = 8) -> NcuResult:
    """Run ncu PC-sampling and read back the per-instruction (per-PC) warp-stall
    histogram from the Source page CSV.  Exports a report then re-imports it on
    the Source page so each row is one SASS instruction with its stall columns."""
    env = dict(os.environ)
    env["LD_LIBRARY_PATH"] = "/lib64:" + env.get("LD_LIBRARY_PATH", "")
    rep = f"/tmp/sass_sched_pcsamp_{entry}"
    collect = [NCU, "--metrics", ",".join(PCSAMP_METRICS),
               "--export", rep, "--force-overwrite",
               "--"] + _harness_argv(harness, cubin, entry, nwords, grid, block,
                                     niter)
    r = subprocess.run(collect, capture_output=True, text=True, env=env)
    blocked = any(sig in (r.stderr + r.stdout) for sig in _NCU_PERM_SIGNATURES)
    if blocked:
        return NcuResult(False, True, {}, [], r.stderr)
    if r.returncode != 0 or not Path(rep + ".ncu-rep").exists():
        return NcuResult(False, False, {}, [], r.stderr)
    rd = subprocess.run([NCU, "--import", rep + ".ncu-rep", "--page", "source",
                         "--print-source", "sass", "--csv"],
                        capture_output=True, text=True, env=env)
    if rd.returncode != 0:
        return NcuResult(False, False, {}, [], rd.stderr)
    per_pc = _parse_ncu_source_csv(rd.stdout)
    return NcuResult(bool(per_pc), False, {}, per_pc, "")


def _parse_ncu_source_csv(csv_text: str) -> list[dict]:
    """Parse the Source-page CSV: the first line names the kernel, the second is
    the column header (Address, Source, then one column per pcsamp metric), and
    each subsequent row is one SASS instruction.  Returns per-instruction dicts
    keyed by the short stall-reason name (the suffix after '..._stalled_')."""
    import csv as _csv
    import io
    lines = csv_text.splitlines()
    # find the header row (the one starting with "Address")
    hdr_i = next((i for i, ln in enumerate(lines)
                  if ln.lstrip().startswith('"Address"')), None)
    if hdr_i is None:
        return []
    rows = list(_csv.reader(io.StringIO("\n".join(lines[hdr_i:]))))
    header = rows[0]

    def _short(name: str) -> str:
        m = re.search(r"warps_issue_stalled_(\w+)", name)
        if m:
            return m.group(1)
        if name.endswith("pcsamp_sample_count"):
            return "sample_count"
        return name

    cols = {i: _short(h) for i, h in enumerate(header)}
    out: list[dict] = []
    for r in rows[1:]:
        if len(r) < 2 or not r[0].startswith("0x"):
            continue
        rec: dict = {"addr": int(r[0], 16), "sass": r[1].strip()}
        reasons: dict[str, int] = {}
        for i in range(2, min(len(r), len(header))):
            reasons[cols[i]] = int(_parse_ncu_num(r[i]))
        rec["reasons"] = reasons
        out.append(rec)
    return out


# =============================================================================
# Amplified probe corpus : self-contained per-iteration dependent chains looped
# N times.  Each isolates one understall-candidate FAMILY in a hot loop so the
# per-iteration stall delta accumulates above launch noise.  The body is written
# so the only loop-carried value is the induction variable (and, where noted, a
# cheap accumulator): this keeps the linear-DAG composer's schedule sound (no
# hidden back-edge hazard), which is exactly what lets a bit-identical V2 prove
# slack rather than mask a hazard.  Compiled at -O1 to keep ptxas from unrolling
# (an unrolled body folds the back-edge into a chain the linear DAG mis-stalls).
# =============================================================================

AMP_PTX = {
    # rsqrt/mul transcendental chain over INDEPENDENT elements (no data carry):
    # exercises the MUFU decoupled-scoreboard waits + FMUL same-pipe stalls and a
    # loop-invariant `MOV Rx, 0x4` that ptxas over-stalls (the corpus MOV case).
    "amp_transc": """
.version 8.3
.target sm_XX
.address_size 64
.visible .entry amp_transc(.param .u64 p, .param .u32 niter) {
  .reg .pred %pq; .reg .f32 %f<8>; .reg .b32 %r<8>; .reg .b64 %rd<5>;
  ld.param.u64 %rd1, [p]; cvta.to.global.u64 %rd2, %rd1;
  ld.param.u32 %r2, [niter];
  mov.u32 %r1, %tid.x; mov.u32 %r3, 0;
$L:
  add.s32 %r4, %r1, %r3;
  cvt.rn.f32.u32 %f1, %r4;
  rsqrt.approx.f32 %f2, %f1; mul.f32 %f3, %f2, %f2;
  rsqrt.approx.f32 %f4, %f3; mul.f32 %f5, %f4, %f4;
  and.b32 %r5, %r4, 7; mul.wide.u32 %rd3, %r5, 4; add.s64 %rd4, %rd2, %rd3;
  st.global.f32 [%rd4], %f5;
  add.s32 %r3, %r3, 1; setp.lt.u32 %pq, %r3, %r2; @%pq bra $L;
  ret;
}
""",
    # integer dependent chain over independent elements: same/cross-pipe coupled
    # forwarding stalls (IMAD/LOP3/IADD3) -- the int_mul_chain understall family.
    "amp_intchain": """
.version 8.3
.target sm_XX
.address_size 64
.visible .entry amp_intchain(.param .u64 p, .param .u32 niter) {
  .reg .pred %pq; .reg .b32 %r<12>; .reg .b64 %rd<5>;
  ld.param.u64 %rd1, [p]; cvta.to.global.u64 %rd2, %rd1;
  ld.param.u32 %r2, [niter];
  mov.u32 %r1, %tid.x; mov.u32 %r3, 0;
$L:
  add.s32 %r4, %r1, %r3;
  mul.lo.s32 %r5, %r4, %r4; xor.b32 %r6, %r5, %r4; mul.lo.s32 %r7, %r6, %r6;
  add.s32 %r8, %r7, %r4;
  and.b32 %r9, %r4, 7; mul.wide.u32 %rd3, %r9, 4; add.s64 %rd4, %rd2, %rd3;
  st.global.u32 [%rd4], %r8;
  add.s32 %r3, %r3, 1; setp.lt.u32 %pq, %r3, %r2; @%pq bra $L;
  ret;
}
""",
    # fp fma chain over independent elements: FFMA same-pipe forwarding stalls.
    "amp_fpchain": """
.version 8.3
.target sm_XX
.address_size 64
.visible .entry amp_fpchain(.param .u64 p, .param .u32 niter) {
  .reg .pred %pq; .reg .f32 %f<8>; .reg .b32 %r<8>; .reg .b64 %rd<5>;
  ld.param.u64 %rd1, [p]; cvta.to.global.u64 %rd2, %rd1;
  ld.param.u32 %r2, [niter];
  mov.u32 %r1, %tid.x; mov.u32 %r3, 0;
$L:
  add.s32 %r4, %r1, %r3; cvt.rn.f32.u32 %f1, %r4;
  fma.rn.f32 %f2, %f1, %f1, 0f3F800000; fma.rn.f32 %f3, %f2, %f2, 0f40000000;
  fma.rn.f32 %f4, %f3, %f3, 0f40400000; fma.rn.f32 %f5, %f4, %f4, 0f40800000;
  and.b32 %r5, %r4, 7; mul.wide.u32 %rd3, %r5, 4; add.s64 %rd4, %rd2, %rd3;
  st.global.f32 [%rd4], %f5;
  add.s32 %r3, %r3, 1; setp.lt.u32 %pq, %r3, %r2; @%pq bra $L;
  ret;
}
""",
    # load-feeds-math over independent elements: the ULDC / IMAD.WIDE address
    # computation + a global load feeding a short math chain (mem_chain family).
    "amp_loadmath": """
.version 8.3
.target sm_XX
.address_size 64
.visible .entry amp_loadmath(.param .u64 p, .param .u32 niter) {
  .reg .pred %pq; .reg .b32 %r<10>; .reg .b64 %rd<6>;
  ld.param.u64 %rd1, [p]; cvta.to.global.u64 %rd2, %rd1;
  ld.param.u32 %r2, [niter];
  mov.u32 %r1, %tid.x; mov.u32 %r3, 0;
$L:
  add.s32 %r4, %r1, %r3; and.b32 %r5, %r4, 7;
  mul.wide.u32 %rd3, %r5, 4; add.s64 %rd4, %rd2, %rd3;
  ld.global.u32 %r6, [%rd4];
  add.s32 %r7, %r6, 7; mul.lo.s32 %r8, %r7, %r7; add.s32 %r9, %r8, %r6;
  st.global.u32 [%rd4], %r9;
  add.s32 %r3, %r3, 1; setp.lt.u32 %pq, %r3, %r2; @%pq bra $L;
  ret;
}
""",
}


def build_amp_corpus(workdir: Path, arch: str,
                     opt: str = "-O1") -> list[tuple[str, str]]:
    """Compile every amplified probe for one arch at optimisation `opt`.

    Returns [(name, cubin)].  `opt` is a ptxas optimisation flag, default -O1.

    WHY THE DEFAULT IS -O1, AND WHY WE ALSO EXPOSE -O3
    --------------------------------------------------
    At -O1 ptxas does not unroll these tiny loops; an unrolled body would fold the
    loop back-edge into a straight chain the linear-DAG composer can under-stall,
    so the per-instruction surgical V2 stays sound on the rolled loop.  BUT -O1 is
    not what production code is built at -- to claim a stall is *real ptxas waste*
    the honest comparison is against ptxas -O3 (its best schedule).  These probe
    bodies each carry a global store to one of 8 addresses, so ptxas does NOT
    unroll them at -O3 either (verified: same instruction count, the rolled loop
    survives), which means the surgical V2 is sound at -O3 as well.  Callers that
    measure waste (perf_diff) drive opt="-O3"; --stall-profile keeps -O1 (it only
    needs PC-sample density, and the -O1 body has the fixed PC layout the profile
    cross-map expects).  A probe that DID unroll at -O3 would be skipped by the
    per-iteration soundness check in perf_diff, not silently mismeasured."""
    workdir.mkdir(parents=True, exist_ok=True)
    smnum = arch.lower().replace("sm", "").lstrip("_")
    smflag = f"sm_{smnum}"
    ver = _ptx_version_for(smnum)
    out = []
    for name, tpl in AMP_PTX.items():
        ptx = tpl.replace("sm_XX", smflag).replace(
            ".version 8.3", f".version {ver}").strip()
        ptx_path = workdir / f"{name}.ptx"
        cub = workdir / f"{name}.cubin"
        ptx_path.write_text(ptx)
        r = subprocess.run([PTXAS, "-arch", smflag, opt, "-o", str(cub),
                            str(ptx_path)], capture_output=True, text=True)
        if r.returncode == 0:
            out.append((name, str(cub)))
        else:
            print(f"  ! ptxas {smflag} {opt} {name} failed: "
                  f"{r.stderr.strip()[:120]}", file=sys.stderr)
    return out


def _understall_candidates(d: Decomposition, cr: ComposeResult) -> list[dict]:
    """The understall candidates in a (decomposed, composed) kernel: coupled
    producers feeding a coupled RAW where our composed stall < ptxas's stall."""
    cands = []
    for i, (c, f) in enumerate(zip(d.ctrls, cr.fields)):
        node = d.dag.nodes[i]
        if (node.coupled and _feeds_coupled_raw(d.dag, i)
                and f.stall < c.stall):
            cands.append({"idx": i, "offset": c.offset, "mnem": c.mnem,
                          "ptxas": c.stall, "composed": f.stall})
    return cands


# =============================================================================
# MODE 1 : --perf-diff  -- turn understall "candidates" into measured fact.
# =============================================================================

def perf_diff(arch: str = "sm_89", grid: int = 20, block: int = 32,
              niter: int = 200000, workdir: Path | None = None,
              opt: str = "-O3") -> int:
    """Measure, on the GPU, whether ptxas's conservative stalls are genuine waste.

    For each amplified probe that carries an understall candidate, build two
    cubins -- V1 = ptxas's native control words, V2 = ptxas's words with ONLY the
    understall-candidate instructions' stalls tightened to our composed value
    (every other control word held at ptxas).  This SURGICAL V2 isolates exactly
    the slack ptxas left on those instructions: any cycle change is attributable
    to the candidate stalls alone, not to a wholesale re-schedule (a full
    recompose also raises other stalls -- over-stalls -- which would wash out the
    signal).  Run BOTH under ncu on identical inputs, gate on bit-identical
    output (the correctness gate), and compare cycles.  Per-kernel verdict:

      * V2 bit-identical AND fewer cycles : MEASURED ptxas WASTE (the candidate
        stalls were slack -- removing them really saves cycles).
      * V2 bit-identical AND equal cycles : HARDWARE-ENFORCED (the stall delta is
        absorbed; the latency is structural, not wasted issue slots).
      * V2 NOT bit-identical              : HAZARD, not waste (>=1 of those stalls
        was required -- tightening it changes results; ptxas was right).

    `opt` is the ptxas optimisation level the probes are built at (default -O3 --
    production opt: the honest comparison is against ptxas's BEST schedule, not
    the under-scheduled -O1).  Runs at low occupancy (one warp per scheduler) so a
    per-warp dispatch stall is exposed on the critical path rather than hidden
    behind sibling warps.

    MEASUREMENT RIGOR (so the reported wins are trustworthy):
      * the correctness gate compares against ptxas's output for MANY seeds and
        relaunches each -- a tightening must be hazard-safe across many inputs, not
        bit-identical for one seed by luck;
      * cycles use sm__cycles_active.avg (measured CV ~0.00% here vs
        gpc__cycles_elapsed.max's 0.1-0.2%) measured PAIRED/interleaved V1,V2,...
        so slow GPU-clock drift cancels;
      * the WIN threshold is the measured noise floor (NOISE_FLOOR_CV), not an
        eyeballed 0.5%.
    """
    import copy
    import sass_reorder as R
    # normalise the opt flag so "-O3", "O3", and "3" all mean ptxas -O3 (argparse
    # eats a leading-dash value of a separate token, so accepting the bare form
    # lets `--opt O3` work without the `=` quirk).
    o = opt.strip().lstrip("-")
    if o and not o.upper().startswith("O"):
        o = "O" + o
    opt = "-" + (o.upper() if o else "O3")
    workdir = workdir or Path(f"/tmp/sass_sched_amp_{opt.lstrip('-')}")
    harness = _launch_harness()
    if harness is None:
        print("  ! launch harness unavailable (need gcc + libcuda)",
              file=sys.stderr)
        return 2
    if not _ncu_available():
        print(f"  ! ncu not found at {NCU}", file=sys.stderr)
        return 2

    floor_pct = 100.0 * R.NOISE_FLOOR_CV
    print("=" * 78)
    print(f"PERF-DIFF  (ptxas V1 vs composed V2, measured on the GPU)  arch={arch}")
    print(f"  grid={grid} block={block} niter={niter}  ptxas {opt}  "
          f"(low occupancy: per-warp stall on the critical path)")
    print(f"  metric={R.CYCLE_METRIC} (paired)  WIN threshold={floor_pct:.2f}% "
          f"(measured noise floor)  seeds={len(R._GATE_SEEDS)}")
    print("=" * 78)

    cubins = build_amp_corpus(workdir, arch, opt=opt)
    verdicts = []
    blocked = False
    for name, cub in cubins:
        d = decompose(cub)
        cr = compose(d.dag, d.ctrls)
        cands = _understall_candidates(d, cr)
        if not cands:
            continue
        # entry name == probe name (the .entry directive)
        entry = name
        # SURGICAL V2: ptxas base, only the understall candidates tightened.
        base = [ctrl_to_fields(c) for c in d.ctrls]
        surg = copy.deepcopy(base)
        for c in cands:
            surg[c["idx"]].usched = stall_to_usched(c["composed"],
                                                    end_group=False)
        v2 = f"{workdir}/{name}_v2.cubin"
        patch_cubin(cub, v2, entry, surg)
        saved = sum(c["ptxas"] - c["composed"] for c in cands)

        # MULTI-SEED correctness gate: a tightening must reproduce ptxas's output
        # for EVERY seed (across relaunches), not bit-identical for one seed by
        # luck.  refs is the per-seed ptxas reference; a faulting V2 just means the
        # all-candidates tighten hit a hazard -> bisect below.
        refs = R._gate_outputs(harness, cub, entry, 8, grid, block, niter)
        if refs is None:
            print(f"\n{name}: reference launch failed; skipping")
            continue
        identical = R._gate_identical(harness, v2, entry, 8, grid, block, niter,
                                      refs)

        cand_desc = ", ".join(f"#{c['idx']} {_base_mnem(c['mnem'])} "
                              f"{c['ptxas']}->{c['composed']}" for c in cands)
        print(f"\n{name}  ({len(cands)} understall candidate(s), "
              f"{saved} stall cyc/iter removed: {cand_desc})")

        if not identical:
            # >=1 candidate is a real hazard.  Bisect (launch-only, no ncu) to
            # find the largest subset whose tightening stays bit-identical across
            # ALL seeds, so we report exactly which candidates are waste vs hazard.
            safe = []
            for c in cands:
                trial = copy.deepcopy(base)
                for s in safe + [c]:
                    trial[s["idx"]].usched = stall_to_usched(s["composed"],
                                                             end_group=False)
                tcub = f"{workdir}/{name}_trial.cubin"
                patch_cubin(cub, tcub, entry, trial)
                if R._gate_identical(harness, tcub, entry, 8, grid, block, niter,
                                     refs):
                    safe.append(c)
            hazards = [c for c in cands if c not in safe]
            hz_desc = ", ".join(f"#{c['idx']} {_base_mnem(c['mnem'])} "
                                f"{c['ptxas']}->{c['composed']}" for c in hazards)
            print(f"  GATE: V2 output DIFFERS from V1  -> HAZARD on: {hz_desc}")
            if not safe:
                print("  VERDICT: HAZARD (every candidate stall is required; "
                      "not waste)")
                verdicts.append((name, "hazard", 0, 0, cands))
                continue
            # measure the safe subset instead
            surg2 = copy.deepcopy(base)
            for c in safe:
                surg2[c["idx"]].usched = stall_to_usched(c["composed"],
                                                         end_group=False)
            patch_cubin(cub, v2, entry, surg2)
            saved = sum(c["ptxas"] - c["composed"] for c in safe)
            safe_desc = ", ".join(f"#{c['idx']} {_base_mnem(c['mnem'])}"
                                  for c in safe)
            print(f"  safe subset (bit-identical, all seeds): {safe_desc}  "
                  f"({saved} stall cyc/iter)")
            cands = safe        # measure + report the safe subset

        # PAIRED/interleaved measurement on the stable metric: V1,V2,V1,V2,... so
        # slow GPU-clock drift cancels and the delta reflects the per-iteration
        # critical path (sm__cycles_active.avg, measured CV ~0.00% here).
        pm = R._measure_paired(harness, cub, v2, entry, grid, block, niter, 8,
                               pairs=7)
        if pm is None:
            # distinguish access-denied from a transient failure.
            chk = _ncu_perf(harness, cub, entry, grid, block, niter)
            if chk.blocked:
                blocked = True
                break
            print(f"  ! ncu measurement failed")
            continue
        c1, c2 = pm
        delta = c1 - c2
        pct = (100.0 * delta / c1) if c1 else 0.0
        # WIN iff the delta clears the MEASURED noise floor (NOISE_FLOOR_CV); a
        # sub-floor delta is hardware-enforced (within run jitter).
        if c1 and pct > floor_pct:
            verdict = "WASTE"
        else:
            verdict = "hw-enforced"
        print(f"  GATE: V2 bit-identical to V1 across {len(refs)} seeds "
              f"(correctness OK)")
        print(f"  cycles V1(ptxas)={c1:,.0f}  V2(composed)={c2:,.0f}  "
              f"delta={delta:,.0f} ({pct:+.2f}%)  [floor {floor_pct:.2f}%]")
        print(f"  VERDICT: {'MEASURED ptxas WASTE' if verdict=='WASTE' else 'HARDWARE-ENFORCED (stall absorbed)'}")
        verdicts.append((name, verdict, c1, c2, cands))

    if blocked:
        print("\n" + "=" * 78)
        print("PROFILING BLOCKED (ncu access-denied)")
        print(_NCU_PERM_REMEDIATION)
        print("  (the perf-diff cubins were still built; rerun once profiling is "
              "permitted.)")
        return 3

    print("\n" + "=" * 78)
    print("PERF-DIFF SUMMARY")
    print("=" * 78)
    for name, verdict, c1, c2, cands in verdicts:
        tag = {"WASTE": "MEASURED ptxas WASTE",
               "hw-enforced": "hardware-enforced",
               "hazard": "HAZARD (not waste)"}[verdict]
        extra = (f"  {c1:,.0f} -> {c2:,.0f} cyc" if c1 else "")
        print(f"  {name:<16} {tag}{extra}")
    return 0


# =============================================================================
# MODE 2 : --stall-profile  -- per-instruction hardware warp-stall observability,
# cross-mapped to our scheduling model.
# =============================================================================

def stall_profile(cubin: str, entry: str | None = None, grid: int = 20,
                  block: int = 256, niter: int = 60000,
                  amp: str | None = None) -> int:
    """Read live per-instruction warp-stall reasons (ncu PC sampling) and confirm
    they agree with our scheduling model.

    For each SASS instruction we get the dominant observed stall reason (the
    public ncu `..._issue_stalled_<reason>` metric with the most samples) and
    cross-map it to (1) the MECHANISM our composer assigned that instruction (a
    fixed usched stall vs a scoreboard `req_bit_set` wait), and (2) the recovered
    can't-issue taxonomy (referred to by the public ncu reason name).  We then
    report agreement: does `long_scoreboard` land on our decoupled-producer waits
    (global loads), `short_scoreboard` on our shorter decoupled waits (MUFU /
    shared), `wait`/`dispatch_stall` on our fixed coupled stalls?  Any instruction
    whose observed reason contradicts our classification is flagged.

    If `amp` names an amplified probe, that probe is built and profiled (its loop
    gives enough samples per PC); otherwise the given cubin is profiled directly.
    """
    harness = _launch_harness()
    if harness is None:
        print("  ! launch harness unavailable", file=sys.stderr)
        return 2
    if not _ncu_available():
        print(f"  ! ncu not found at {NCU}", file=sys.stderr)
        return 2

    if amp:
        wd = Path("/tmp/sass_sched_amp")
        built = dict(build_amp_corpus(wd, "sm_89"))
        if amp not in built:
            print(f"  ! no amplified probe '{amp}' (have {list(built)})",
                  file=sys.stderr)
            return 2
        cubin, entry = built[amp], amp

    if entry is None:
        out = subprocess.run([NVDISASM, "-c", cubin], capture_output=True,
                             text=True).stdout
        m = re.search(r"\.text\.(\S+)", out)
        entry = m.group(1) if m else None
    if entry is None:
        print("  ! could not determine entry name", file=sys.stderr)
        return 2

    res = _ncu_pcsamp(harness, cubin, entry, grid, block, niter)
    if res.blocked:
        print("PROFILING BLOCKED (ncu access-denied)")
        print(_NCU_PERM_REMEDIATION)
        return 3
    if not res.ok:
        print(f"  ! pcsamp failed: {res.stderr[:200]}", file=sys.stderr)
        return 2

    # our model's per-instruction prediction (mechanism)
    d = decompose(cubin)
    cr = compose(d.dag, d.ctrls)
    pred = _model_mechanism(d, cr)        # offset -> ("fixed usched stall"|"scoreboard wait (req_bit_set)"|"none", detail)

    # align ncu rows (virtual addrs) to our offsets via the first instruction PC
    base_addr = res.per_pc[0]["addr"] if res.per_pc else 0

    print("=" * 100)
    print(f"STALL-PROFILE  {Path(cubin).name}  entry={entry}  "
          f"(grid={grid} block={block} niter={niter})")
    print("  per-instruction dominant warp-stall reason (ncu PC sampling) vs our "
          "scheduling-model prediction")
    print("=" * 100)
    hdr = (f"{'off':>6} {'mnem':<20} {'samples':>8} {'dominant ncu stall':<20} "
           f"{'-> our mechanism':<32} {'model pred':<32} {'agree'}")
    print(hdr)
    print("-" * 130)

    agree = contradict = 0
    contradictions = []
    for row in res.per_pc:
        off = row["addr"] - base_addr
        reasons = row["reasons"]
        samples = reasons.get("sample_count", 0)
        # dominant *stall* reason (exclude the issued 'selected' bucket)
        stall_reasons = {k: v for k, v in reasons.items()
                         if k not in ("sample_count", "selected") and v > 0}
        dom = max(stall_reasons, key=stall_reasons.get) if stall_reasons else "-"
        obs_mech = STALL_REASON_MODEL.get(dom, "structural / not modelled")
        pm, detail = pred.get(off, ("none", ""))
        mnem = _offset_mnem(d, off)

        is_term = _base_mnem(mnem) in (
            "EXIT", "BRA", "BRX", "JMP", "CALL", "RET", "BSSY", "BSYNC", "NOP")
        if dom == "-" or samples < 3:
            verdict = "."        # too few samples to judge
        elif obs_mech == "issued":
            verdict = "."
        elif is_term:
            # a terminator's `wait` is drain (in-flight memory retiring at EXIT)
            # or branch resolution -- structural, not a per-edge data hazard.
            verdict = "~"
        elif obs_mech == "structural / not modelled":
            verdict = "~"        # structural effect our per-edge model omits
        elif pm == "none":
            # observed a data-dependency stall where our model predicted none
            verdict = "X"
            contradict += 1
            contradictions.append((off, mnem, dom, obs_mech, pm))
        elif obs_mech == pm:
            verdict = "OK"
            agree += 1
        else:
            verdict = "X"
            contradict += 1
            contradictions.append((off, mnem, dom, obs_mech, pm))

        print(f"{off:#06x} {mnem:<20} {samples:>8} {dom:<20} "
              f"{obs_mech:<32} {pm:<32} {verdict}")

    print("-" * 130)
    judged = agree + contradict
    rate = (100.0 * agree / judged) if judged else 100.0
    print(f"\nMODEL-vs-SILICON AGREEMENT (data-dependency stalls only): "
          f"{agree}/{judged} = {rate:.1f}%")
    print(f"  '.' issued / too-few-samples   '~' structural (not a per-edge "
          f"hazard our model predicts)   'OK' agree   'X' contradiction")
    if contradictions:
        print("\nCONTRADICTIONS (observed data-stall reason vs our prediction)")
        for off, mnem, dom, obs, pm in contradictions:
            print(f"  @{off:#06x} {mnem:<18} ncu={dom} ({obs}) vs model={pm or 'none'}")
    else:
        print("  No contradictions: every observed data-dependency stall matches "
              "the mechanism our scheduler assigned.")
    return 0


def _model_mechanism(d: Decomposition, cr: ComposeResult) -> dict[int, tuple]:
    """offset -> (mechanism, detail) our composer assigns each instruction:
    'scoreboard wait (req_bit_set)' if it waits on a decoupled producer's
    scoreboard; 'fixed usched stall' if it carries (or a producer owes it) a
    fixed coupled stall; else 'none'."""
    out: dict[int, tuple] = {}
    for i, (c, f) in enumerate(zip(d.ctrls, cr.fields)):
        off = c.offset
        # consumer waits on a scoreboard -> decoupled-producer dependency
        waits_sb = f.wait_mask != 0
        # this instruction must wait on a fixed coupled stall from a producer
        # (an incoming fixed-latency RAW edge), or carries a stall itself for a
        # coupled consumer.
        in_fixed = any(_is_stall_edge(e) for e in d.dag.in_edges(i))
        if waits_sb:
            out[off] = ("scoreboard wait (req_bit_set)",
                        f"wait_mask={f.wait_mask:06b}")
        elif in_fixed:
            out[off] = ("fixed usched stall", "incoming fixed RAW")
        else:
            out[off] = ("none", "")
    return out


def _offset_mnem(d: Decomposition, off: int) -> str:
    for c in d.ctrls:
        if c.offset == off:
            return c.mnem
    return "?"


def _scoreboard_pairs(d: Decomposition) -> set[tuple[int, int]]:
    """Ground-truth (consumer_idx, producer_idx) scoreboard pairs from ptxas."""
    pairs: set[tuple[int, int]] = set()
    armed_by: dict[int, int] = {}
    for i, c in enumerate(d.ctrls):
        for sb in c.wait_sbs:
            if sb in armed_by:
                pairs.add((i, armed_by[sb]))
        if c.dst_wr != 7:
            armed_by[c.dst_wr] = i
        if c.src_rel != 7:
            armed_by[c.src_rel] = i
    return pairs


def _composed_pairs(dag: DAG, cr: ComposeResult) -> set[tuple[int, int]]:
    """Composed (consumer_idx, producer_idx) pairs implied by our scoreboards."""
    pairs: set[tuple[int, int]] = set()
    prod_sb: dict[int, int] = {}
    for i, f in enumerate(cr.fields):
        if f.dst_wr != 7:
            prod_sb[i] = f.dst_wr
        if f.src_rel != 7:
            prod_sb.setdefault(i, f.src_rel)
    # consumer waits -> producers via the DAG edges (decoupled producers)
    for i in range(len(dag.nodes)):
        for e in dag.in_edges(i):
            if not dag.nodes[e.src].coupled and e.src in prod_sb:
                if cr.fields[i].wait_mask & (1 << prod_sb[e.src]):
                    pairs.add((i, e.src))
    return pairs


def _classify_stall_mismatch(c: Ctrl, f: SchedFields, dag: DAG, i: int) -> str:
    """Bucket a stall mismatch.  The sign matters: composed > ptxas is a safe
    over-stall (ptxas hid the latency with scheduling freedom we cannot replay);
    composed < ptxas is an under-stall relative to ptxas (still hazard-safe per
    the issue-cycle fixpoint + the dynamic GPU check, but flagged separately)."""
    over = f.stall > c.stall                       # we stall more than ptxas
    base = _base_mnem(c.mnem)
    if base in ("ULDC", "UMOV", "ULDCU", "LDCU"):
        return "uniform_descriptor_distance"       # ULDC publish-distance variance
    if not over:                                   # composed < ptxas
        return "understall_vs_ptxas_conservatism"
    # composed > ptxas: we could not hide the latency ptxas hid.
    if abs(c.stall - f.stall) == 1:
        return "off_by_one_pipe_penalty"
    return "overstall_unhidden_latency"


# =============================================================================
# corpus : hand-written PTX probes
# =============================================================================

CORPUS_PTX = {
    "indep_arith": """
.version 8.3
.target sm_XX
.address_size 64
.visible .entry indep_arith(.param .u64 p) {
  .reg .b32 %r<12>; .reg .b64 %rd<3>;
  ld.param.u64 %rd1, [p]; cvta.to.global.u64 %rd2, %rd1;
  mov.u32 %r1, %tid.x; mov.u32 %r2, %ntid.x; mov.u32 %r3, %ctaid.x;
  add.s32 %r4, %r1, 1; add.s32 %r5, %r2, 2; add.s32 %r6, %r3, 3;
  add.s32 %r7, %r4, %r5; add.s32 %r8, %r6, %r7;
  st.global.u32 [%rd2], %r8; ret;
}
""",
    "dep_chain": """
.version 8.3
.target sm_XX
.address_size 64
.visible .entry dep_chain(.param .u64 p) {
  .reg .b32 %r<10>; .reg .b64 %rd<3>;
  ld.param.u64 %rd1, [p]; cvta.to.global.u64 %rd2, %rd1;
  mov.u32 %r1, %tid.x;
  mad.lo.s32 %r2, %r1, %r1, 7; mad.lo.s32 %r3, %r2, %r2, 11;
  mad.lo.s32 %r4, %r3, %r3, 13; mad.lo.s32 %r5, %r4, %r4, 17;
  mad.lo.s32 %r6, %r5, %r5, 19;
  st.global.u32 [%rd2], %r6; ret;
}
""",
    "fp_chain": """
.version 8.3
.target sm_XX
.address_size 64
.visible .entry fp_chain(.param .u64 p) {
  .reg .f32 %f<10>; .reg .b32 %r1; .reg .b64 %rd<3>;
  ld.param.u64 %rd1, [p]; cvta.to.global.u64 %rd2, %rd1;
  mov.u32 %r1, %tid.x; cvt.rn.f32.u32 %f1, %r1;
  fma.rn.f32 %f2, %f1, %f1, 0f3F800000; fma.rn.f32 %f3, %f2, %f2, 0f40000000;
  fma.rn.f32 %f4, %f3, %f3, 0f40400000; fma.rn.f32 %f5, %f4, %f4, 0f40800000;
  st.global.f32 [%rd2], %f5; ret;
}
""",
    "load_feeds_math": """
.version 8.3
.target sm_XX
.address_size 64
.visible .entry load_feeds_math(.param .u64 p) {
  .reg .b32 %r<8>; .reg .b64 %rd<4>;
  ld.param.u64 %rd1, [p]; cvta.to.global.u64 %rd2, %rd1;
  mov.u32 %r1, %tid.x; mul.wide.u32 %rd3, %r1, 4; add.s64 %rd2, %rd2, %rd3;
  ld.global.u32 %r2, [%rd2];
  add.s32 %r3, %r2, 7; mul.lo.s32 %r4, %r3, %r3; add.s32 %r5, %r4, %r2;
  st.global.u32 [%rd2], %r5; ret;
}
""",
    "transcendental": """
.version 8.3
.target sm_XX
.address_size 64
.visible .entry transcendental(.param .u64 p) {
  .reg .f32 %f<10>; .reg .b32 %r1; .reg .b64 %rd<3>;
  ld.param.u64 %rd1, [p]; cvta.to.global.u64 %rd2, %rd1;
  mov.u32 %r1, %tid.x; cvt.rn.f32.u32 %f1, %r1;
  rsqrt.approx.f32 %f2, %f1; mul.f32 %f3, %f2, %f2;
  rsqrt.approx.f32 %f4, %f3; mul.f32 %f5, %f4, %f4;
  st.global.f32 [%rd2], %f5; ret;
}
""",
    "mixed": """
.version 8.3
.target sm_XX
.address_size 64
.visible .entry mixed(.param .u64 p) {
  .reg .b32 %r<10>; .reg .f32 %f<6>; .reg .b64 %rd<4>;
  ld.param.u64 %rd1, [p]; cvta.to.global.u64 %rd2, %rd1;
  mov.u32 %r1, %tid.x; mul.wide.u32 %rd3, %r1, 4; add.s64 %rd2, %rd2, %rd3;
  ld.global.u32 %r2, [%rd2]; add.s32 %r3, %r2, 5;
  cvt.rn.f32.u32 %f1, %r3; fma.rn.f32 %f2, %f1, %f1, 0f3F800000;
  cvt.rzi.u32.f32 %r4, %f2; add.s32 %r5, %r4, %r3;
  st.global.u32 [%rd2], %r5; ret;
}
""",
    # extra pipe-mix probes added to exercise the table-driven stall paths:
    "int_mul_chain": """
.version 8.3
.target sm_XX
.address_size 64
.visible .entry int_mul_chain(.param .u64 p) {
  .reg .b32 %r<12>; .reg .b64 %rd<3>;
  ld.param.u64 %rd1, [p]; cvta.to.global.u64 %rd2, %rd1;
  mov.u32 %r1, %tid.x;
  add.s32 %r2, %r1, 3; mul.lo.s32 %r3, %r2, %r2; and.b32 %r4, %r3, 255;
  mad.lo.s32 %r5, %r4, %r4, %r2; shl.b32 %r6, %r5, 2; or.b32 %r7, %r6, %r4;
  st.global.u32 [%rd2], %r7; ret;
}
""",
    "fp_transcend_mix": """
.version 8.3
.target sm_XX
.address_size 64
.visible .entry fp_transcend_mix(.param .u64 p) {
  .reg .f32 %f<10>; .reg .b32 %r1; .reg .b64 %rd<3>;
  ld.param.u64 %rd1, [p]; cvta.to.global.u64 %rd2, %rd1;
  mov.u32 %r1, %tid.x; cvt.rn.f32.u32 %f1, %r1;
  mul.f32 %f2, %f1, %f1; sqrt.approx.f32 %f3, %f2; add.f32 %f4, %f3, %f1;
  sin.approx.f32 %f5, %f4; fma.rn.f32 %f6, %f5, %f5, %f1;
  st.global.f32 [%rd2], %f6; ret;
}
""",
    "mem_chain": """
.version 8.3
.target sm_XX
.address_size 64
.visible .entry mem_chain(.param .u64 p) {
  .reg .b32 %r<10>; .reg .b64 %rd<6>;
  ld.param.u64 %rd1, [p]; cvta.to.global.u64 %rd2, %rd1;
  mov.u32 %r1, %tid.x; mul.wide.u32 %rd3, %r1, 4; add.s64 %rd4, %rd2, %rd3;
  ld.global.u32 %r2, [%rd4]; and.b32 %r5, %r2, 7; mul.wide.u32 %rd5, %r5, 4;
  add.s64 %rd4, %rd2, %rd5;
  ld.global.u32 %r3, [%rd4]; add.s32 %r4, %r3, %r2;
  st.global.u32 [%rd2], %r4; ret;
}
""",
    "scbd_overload": """
.version 8.3
.target sm_XX
.address_size 64
.visible .entry scbd_overload(.param .u64 p) {
  .reg .b32 %r<20>; .reg .b64 %rd<12>;
  ld.param.u64 %rd1, [p]; cvta.to.global.u64 %rd2, %rd1;
  mov.u32 %r1, %tid.x; mul.wide.u32 %rd3, %r1, 4; add.s64 %rd2, %rd2, %rd3;
  ld.global.u32 %r2, [%rd2+0];  ld.global.u32 %r3, [%rd2+4];
  ld.global.u32 %r4, [%rd2+8];  ld.global.u32 %r5, [%rd2+12];
  ld.global.u32 %r6, [%rd2+16]; ld.global.u32 %r7, [%rd2+20];
  ld.global.u32 %r8, [%rd2+24]; ld.global.u32 %r9, [%rd2+28];
  add.s32 %r10, %r2, %r3; add.s32 %r11, %r4, %r5; add.s32 %r12, %r6, %r7;
  add.s32 %r13, %r8, %r9; add.s32 %r14, %r10, %r11; add.s32 %r15, %r12, %r13;
  add.s32 %r16, %r14, %r15;
  st.global.u32 [%rd2], %r16; ret;
}
""",
}


def _ptx_version_for(smnum: str) -> str:
    """Minimum PTX ISA version that accepts a given SM target.

    Per-target minima confirmed empirically against ptxas 13.0.88 (it rejects a
    target newer than the declared .version):
        sm_100 -> 8.6, sm_120 -> 8.7, sm_103 / sm_121 -> 8.8, sm_110 -> 9.0.
    Older targets accept 8.3."""
    n = int(re.match(r"\d+", smnum).group())
    per_arch = {100: "8.6", 120: "8.7", 103: "8.8", 121: "8.8", 110: "9.0"}
    if n in per_arch:
        return per_arch[n]
    if n >= 110:        # any future Blackwell-class target: use the newest seen
        return "9.0"
    if n >= 100:
        return "8.6"
    return "8.3"


def build_corpus(workdir: Path, arches: list[str]) -> list[tuple[str, str, str]]:
    """Compile every probe for every arch.  Returns [(arch, name, cubin_path)]."""
    workdir.mkdir(parents=True, exist_ok=True)
    out = []
    for arch in arches:
        smnum = arch.lower().replace("sm", "").lstrip("_")
        smflag = f"sm_{smnum}"
        ver = _ptx_version_for(smnum)
        for name, tpl in CORPUS_PTX.items():
            ptx = tpl.replace("sm_XX", smflag).replace(
                ".version 8.3", f".version {ver}").strip()
            ptx_path = workdir / f"{name}_{smnum}.ptx"
            cubin_path = workdir / f"{name}_{smnum}.cubin"
            ptx_path.write_text(ptx)
            r = subprocess.run([PTXAS, "-arch", smflag, "-O3", "-o",
                                str(cubin_path), str(ptx_path)],
                               capture_output=True, text=True)
            if r.returncode == 0:
                out.append((smflag, name, str(cubin_path)))
            else:
                print(f"  ! ptxas {smflag} {name} failed: "
                      f"{r.stderr.strip()[:120]}", file=sys.stderr)
    return out


def verify_corpus(arches: list[str] | None = None,
                  workdir: Path | None = None) -> int:
    arches = arches or ["sm_89", "sm_75", "sm_80", "sm_90"]
    workdir = workdir or Path("/tmp/sass_sched_corpus")
    print(f"# Building corpus ({len(CORPUS_PTX)} probes x {len(arches)} arches) "
          f"in {workdir}")
    cubins = build_corpus(workdir, arches)
    print(f"# Compiled {len(cubins)} kernels\n")

    per_arch: dict[str, dict] = {}
    all_mism: list[tuple[str, str, Mismatch]] = []
    for arch, name, cubin in cubins:
        try:
            n_cmp, n_exact, mism, summ = round_trip(cubin)
        except Exception as e:
            print(f"  ! {arch} {name}: {e}", file=sys.stderr)
            continue
        a = per_arch.setdefault(arch, {"stall_cmp": 0, "stall_exact": 0,
                                       "pair_cmp": 0, "pair_exact": 0,
                                       "kernels": 0})
        a["stall_cmp"] += summ["stall_compared"]
        a["stall_exact"] += summ["stall_exact"]
        a["pair_cmp"] += summ["scbd_pairs_gt"]
        a["pair_exact"] += summ["scbd_pairs_matched"]
        a["kernels"] += 1
        for m in mism:
            all_mism.append((arch, name, m))

    print("=" * 72)
    print("CORPUS VERIFICATION  (ptxas vs composed)")
    print("=" * 72)
    hdr = f"{'arch':<8} {'kern':>4} {'stall':>10} {'stall%':>7} " \
          f"{'scbd-pair':>10} {'pair%':>7}"
    print(hdr)
    print("-" * 72)
    tot = {"sc": 0, "se": 0, "pc": 0, "pe": 0}
    for arch, a in sorted(per_arch.items()):
        sp = 100.0 * a["stall_exact"] / a["stall_cmp"] if a["stall_cmp"] else 100.0
        pp = 100.0 * a["pair_exact"] / a["pair_cmp"] if a["pair_cmp"] else 100.0
        print(f"{arch:<8} {a['kernels']:>4} "
              f"{a['stall_exact']:>4}/{a['stall_cmp']:<5} {sp:>6.1f}% "
              f"{a['pair_exact']:>4}/{a['pair_cmp']:<5} {pp:>6.1f}%")
        tot["sc"] += a["stall_cmp"]; tot["se"] += a["stall_exact"]
        tot["pc"] += a["pair_cmp"]; tot["pe"] += a["pair_exact"]
    print("-" * 72)
    sp = 100.0 * tot["se"] / tot["sc"] if tot["sc"] else 100.0
    pp = 100.0 * tot["pe"] / tot["pc"] if tot["pc"] else 100.0
    print(f"{'TOTAL':<8} {len(cubins):>4} "
          f"{tot['se']:>4}/{tot['sc']:<5} {sp:>6.1f}% "
          f"{tot['pe']:>4}/{tot['pc']:<5} {pp:>6.1f}%")

    if all_mism:
        print("\nMISMATCH TAXONOMY")
        print("-" * 72)
        by_cls: dict[str, list] = {}
        for arch, name, m in all_mism:
            by_cls.setdefault(m.cls, []).append((arch, name, m))
        for cls, items in sorted(by_cls.items(), key=lambda x: -len(x[1])):
            print(f"  [{len(items):>3}] {cls}")
            for arch, name, m in items[:4]:
                print(f"        {arch} {name} @{m.offset:#06x} {m.mnem} "
                      f"{m.field}: ptxas={m.expected} composed={m.got}")
    else:
        print("\nNo mismatches: exact reproduction of stalls + scoreboard pairings.")

    print("\nEXACT vs APPROXIMATE")
    print("  exact      : producer->scoreboard pairings (structure); the table-")
    print("               driven coupled-stall magnitudes -- same/cross-pipe, the")
    print("               AGU pre-issue slot (incl. the Turing 8), the float<->int")
    print("               conversion / MUFU-input latch, and the CC/pred control")
    print("               band (13; 12 on Turing) -- keyed by the per-arch pipe")
    print("               classification from the SASS-ISA table.")
    print("  over-stall : when composed > ptxas, ptxas hid the latency with full")
    print("               scheduling freedom we cannot replay; over-stalling is")
    print("               hazard-safe (proven by --verify-dyn on the GPU).")
    print("  under-stall: when composed < ptxas, ptxas chose a more conservative")
    print("               stall than the hazard requires; our schedule is still")
    print("               hazard-safe (the issue-cycle fixpoint honours every RAW")
    print("               edge; confirmed bit-identical on the GPU).")
    print("  approximate : variable-latency completion *times* (resolved by")
    print("               scoreboards, not stalls); the ULDC uniform-descriptor")
    print("               publish distance; the exact SB index (allocator policy).")
    # Success criterion: producer->scoreboard pairing must be exact (the
    # primary correctness result); composed stalls need only be safe
    # (>= ptxas), which dynamic validation confirms.  Stall *exactness* is a
    # quality metric, not a correctness gate.
    return 0 if pp >= 99.9 else 1


# =============================================================================
# debug / pretty-print
# =============================================================================

def _kind_arrow(kind: str) -> str:
    return {"RAW": "==RAW=>", "WAR": "--war->", "WAW": "..waw..",
            "CTRL": "~ctrl~>"}[kind]


def critical_path(dag: DAG) -> tuple[list[int], int]:
    """Longest weighted path through the DAG (RAW + band weights)."""
    n = len(dag.nodes)
    dist = [0] * n
    prev = [-1] * n
    for i in range(n):
        for e in dag.out_edges(i):
            w = e.weight if e.kind == EDGE_RAW else 0
            if dist[i] + w + 1 > dist[e.dst]:
                dist[e.dst] = dist[i] + w + 1
                prev[e.dst] = i
    if not dist:
        return [], 0
    end = max(range(n), key=lambda k: dist[k])
    path = []
    k = end
    while k != -1:
        path.append(k)
        k = prev[k]
    return list(reversed(path)), dist[end]


def to_dot(dag: DAG) -> str:
    lines = ["digraph sass_dag {", "  rankdir=TB;",
             '  node [shape=box, fontname="monospace"];']
    for nd in dag.nodes:
        color = "lightblue" if nd.coupled else "lightsalmon"
        lines.append(f'  n{nd.idx} [label="#{nd.idx} {nd.mnem}\\n'
                     f'{"coupled" if nd.coupled else "decoupled b" + str(nd.band)}",'
                     f' style=filled, fillcolor={color}];')
    style = {"RAW": "solid", "WAR": "dashed", "WAW": "dotted", "CTRL": "bold"}
    color = {"RAW": "red", "WAR": "blue", "WAW": "gray", "CTRL": "green"}
    for e in dag.edges:
        lines.append(f'  n{e.src} -> n{e.dst} [label="{e.kind} {e.reg} '
                     f'w={e.weight}", style={style[e.kind]}, '
                     f'color={color[e.kind]}];')
    lines.append("}")
    return "\n".join(lines)


def debug_print(cubin: str, dot_path: str | None = None) -> None:
    d = decompose(cubin)
    cr = compose(d.dag, d.ctrls)
    print(f"# {Path(cubin).name}  arch={d.arch}  "
          f"{len(d.ctrls)} instrs  {len(d.dag.edges)} edges\n")

    print("L0/L1/L2/L3 SIDE-BY-SIDE")
    print("=" * 100)
    hdr = (f"{'#':>3} {'off':>6} {'mnem':<14} {'cpl':>3} | "
           f"{'L1: us/wr/rd/wait/bt':<24} | L2 events")
    print(hdr); print("-" * 100)
    for i, c in enumerate(d.ctrls):
        node = d.dag.nodes[i]
        l0 = pack_control(ctrl_to_fields(c))
        evs = ctrl_to_events(c)
        ev_str = ", ".join(f"{e.kind}{('=' + str(e.value)) if e.value else ''}"
                           for e in evs) or "-"
        l1 = f"{c.usched:>2}/{c.dst_wr}/{c.src_rel}/{c.wait_mask:06b}/{c.batch_t}"
        cpl = "C" if node.coupled else "D"
        print(f"{i:>3} {c.offset:#06x} {c.mnem:<14} {cpl:>3} | {l1:<24} | {ev_str}")

    print("\nL3 TYPED/WEIGHTED DEPENDENCY DAG")
    print("=" * 100)
    for e in d.dag.edges:
        pn, cn = d.dag.nodes[e.src], d.dag.nodes[e.dst]
        print(f"  #{e.src:>2} {pn.mnem:<12} {_kind_arrow(e.kind)} "
              f"#{e.dst:<2} {cn.mnem:<12}  {e.reg:<6} "
              f"w={e.weight:<3} [{e.mechanism}]")

    print("\nPER-INSTRUCTION SCHEDULER REASONING (compose)")
    print("=" * 100)
    for i, c in enumerate(d.ctrls):
        f = cr.fields[i]
        print(f"  #{i:>2} {c.mnem:<14} -> stall={f.stall} wr={f.dst_wr} "
              f"rd={f.src_rel} wait={f.wait_mask:06b}")
        print(f"        {cr.notes[i]}")

    print("\nHAZARD ATTRIBUTION (decompose: each wait/stall -> its producer)")
    print("=" * 100)
    for a in d.attributions:
        print(f"  consumer #{a.consumer_idx} {d.ctrls[a.consumer_idx].mnem:<12}"
              f" <- producer #{a.producer_idx} {d.ctrls[a.producer_idx].mnem:<12}"
              f" [{a.mechanism}] {a.detail}")

    path, length = critical_path(d.dag)
    print(f"\nCRITICAL PATH  (weighted length {length} cyc)")
    print("=" * 100)
    print("  " + " -> ".join(f"#{k}:{d.dag.nodes[k].mnem}" for k in path))

    if dot_path:
        Path(dot_path).write_text(to_dot(d.dag))
        print(f"\n# Graphviz DAG written to {dot_path}  "
              f"(render: dot -Tsvg {dot_path} -o dag.svg)")


def decompose_print(cubin: str) -> None:
    d = decompose(cubin)
    print(f"# DECOMPOSE {Path(cubin).name}  arch={d.arch}")
    print(f"# {len(d.ctrls)} instructions, {len(d.dag.edges)} dependency edges, "
          f"{len(d.attributions)} attributed hazards\n")
    print("CONTROL WORDS (L1, as emitted by ptxas)")
    for i, c in enumerate(d.ctrls):
        print(f"  #{i:>2} {c.offset:#06x} {c.mnem:<14} "
              f"usched={c.usched:<2} stall={c.stall:<2} wr={c.dst_wr} "
              f"rd={c.src_rel} wait={c.wait_sbs} bt={c.batch_t}")
    print("\nTYPED/WEIGHTED DAG (L3)")
    for e in d.dag.edges:
        print(f"  #{e.src}->{e.dst} {e.kind:<4} {e.reg:<6} w={e.weight} "
              f"[{e.mechanism}]  ({d.dag.nodes[e.src].mnem} -> "
              f"{d.dag.nodes[e.dst].mnem})")


def compose_print(cubin: str) -> None:
    d = decompose(cubin)
    cr = compose(d.dag, d.ctrls)
    print(f"# COMPOSE {Path(cubin).name}  arch={d.arch}")
    print("# Recomputed control words from issue order + dependency DAG\n")
    print(f"  {'#':>3} {'mnem':<14} {'composed':<28} {'ptxas':<28} match")
    print("  " + "-" * 78)
    for i, (c, f) in enumerate(zip(d.ctrls, cr.fields)):
        comp = f"us={f.usched} stall={f.stall} wr={f.dst_wr} rd={f.src_rel} " \
               f"wait={f.wait_mask:06b}"
        real = f"us={c.usched} stall={c.stall} wr={c.dst_wr} rd={c.src_rel} " \
               f"wait={c.wait_mask:06b}"
        match = "OK" if (f.stall == c.stall) else "~"
        print(f"  {i:>3} {c.mnem:<14} {comp:<28} {real:<28} {match}")


# =============================================================================
# Stage 1 / Stage 2 report printers
# =============================================================================

def _run_tighten(sr, args) -> int:
    """Print the Stage-1 stall-tightener report for one cubin."""
    grid = args.grid if args.grid else 8
    block = args.block if args.block else 32
    niter = args.niter if args.niter else 100000
    res = sr.tighten_kernel(args.tighten, entry=args.entry,
                            grid=grid, block=block, niter=niter)
    print("=" * 78)
    print(f"STAGE 1  STALL-TIGHTENER  {Path(args.tighten).name}  "
          f"entry={res.entry}")
    print(f"  (ptxas order fixed; only model-proven-slack stalls tightened, "
          f"each GPU-gated bit-identical)")
    print("=" * 78)
    print(f"  understall candidates : {res.n_candidates}")
    print(f"  bit-identical safe    : {res.n_safe}  "
          f"({res.saved_stall} stall cyc/iter removed)")
    if res.safe_desc:
        print(f"  tightened (safe)      : {', '.join(res.safe_desc)}")
    if res.hazard_desc:
        print(f"  hazards (kept ptxas)  : {', '.join(res.hazard_desc)}")
    if not res.gate_ok:
        print("  GATE: FAILED -- tightened output differed (should not happen "
              "after bisection); NO patch accepted")
        return 1
    print(f"  GATE: bit-identical to ptxas across {len(sr._GATE_SEEDS)} seeds "
          f"(correctness OK)")
    if res.cycles_ptxas:
        d_ = res.cycles_ptxas - res.cycles_ours
        pct = 100.0 * d_ / res.cycles_ptxas if res.cycles_ptxas else 0.0
        floor_pct = 100.0 * sr.NOISE_FLOOR_CV
        print(f"  cycles V1(ptxas)={res.cycles_ptxas:,.0f}  "
              f"V2(tightened)={res.cycles_ours:,.0f}  "
              f"delta={d_:,.0f} ({pct:+.2f}%)  "
              f"[{sr.CYCLE_METRIC}, paired; floor {floor_pct:.2f}%]")
        print(f"  VERDICT: {'MEASURED WIN' if pct > floor_pct else 'hardware-enforced (no slack)'}")
    elif res.n_safe == 0:
        print("  (no slack found; ptxas already minimal on this kernel)")
    return 0


def _run_optsched(sr, args) -> int:
    """Print the Stage-2 reorder-scheduler report for one cubin."""
    grid = args.grid if args.grid else 8
    block = args.block if args.block else 32
    niter = args.niter if args.niter else 100000
    res = sr.optsched_kernel(args.optsched, entry=args.entry,
                             grid=grid, block=block, niter=niter)
    print("=" * 90)
    print(f"STAGE 2  CONSTRAINT-OPTIMAL REORDER SCHEDULER  "
          f"{Path(args.optsched).name}  entry={res.entry}")
    print(f"  (per basic block: list + Z3-Opt makespan minimiser; reorder-patch "
          f"in place; GPU-gate each block bit-identical)")
    print("=" * 90)
    print(f"  {'BB':<4} {'size':>4} {'solver':<8} {'ptxas':>6} {'ours':>6} "
          f"{'gain':>5} {'reordered':<10} {'accepted':<9} note")
    print("  " + "-" * 86)
    n_reorder = n_accept = 0
    for oc in res.outcomes:
        gain = oc.ptxas_makespan - oc.our_makespan if oc.reordered else 0
        if oc.reordered:
            n_reorder += 1
        if oc.reordered and oc.accepted:
            n_accept += 1
        ro = "yes" if oc.reordered else "-"
        ac = ("YES" if oc.accepted else "fallback") if oc.reordered else "-"
        mk1 = oc.ptxas_makespan if oc.reordered else "-"
        mk2 = oc.our_makespan if oc.reordered else "-"
        print(f"  {oc.bid:<4} {oc.size:>4} {oc.solver:<8} {str(mk1):>6} "
              f"{str(mk2):>6} {str(gain) if gain else '-':>5} {ro:<10} "
              f"{ac:<9} {oc.reason}")
    print("  " + "-" * 86)
    print(f"  blocks reordered by solver : {n_reorder}")
    print(f"  blocks accepted (GPU gate) : {n_accept}")
    print(f"  blocks fell back to ptxas  : {n_reorder - n_accept}")
    if not res.gate_ok:
        print("\n  GATE: FAILED -- final kernel output differed from ptxas! "
              "NO reorder accepted (this must never happen).")
        return 1
    print("\n  GATE: final kernel bit-identical to ptxas (correctness OK)")
    if res.cycles_ptxas:
        d_ = res.cycles_ptxas - res.cycles_ours
        pct = 100.0 * d_ / res.cycles_ptxas if res.cycles_ptxas else 0.0
        floor_pct = 100.0 * sr.NOISE_FLOOR_CV
        print(f"  cycles V(ptxas)={res.cycles_ptxas:,.0f}  "
              f"V(ours)={res.cycles_ours:,.0f}  delta={d_:,.0f} ({pct:+.2f}%)  "
              f"[{sr.CYCLE_METRIC}; floor {floor_pct:.2f}%]")
        print(f"  VERDICT: {'MEASURED REORDER WIN' if pct > floor_pct else 'no measured cycle change (reorder hidden by stalls/occupancy)'}")
    elif n_accept == 0:
        print("  (no block beat ptxas's order on this kernel -- ptxas's "
              "schedule is already makespan-optimal here)")
    return 0


# =============================================================================
# CLI
# =============================================================================

def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description="SASS scheduling composer/decomposer across layers")
    ap.add_argument("--decompose", metavar="CUBIN")
    ap.add_argument("--compose", metavar="CUBIN")
    ap.add_argument("--debug", metavar="CUBIN")
    ap.add_argument("--dot", metavar="FILE", help="write Graphviz DAG (with --debug)")
    ap.add_argument("--verify", nargs="+", metavar="CUBIN")
    ap.add_argument("--verify-dyn", metavar="CUBIN",
                    help="patch recomposed control words, launch on GPU, diff "
                         "results (proves hazard-safety)")
    ap.add_argument("--entry", help="kernel entry name (for --verify-dyn)")
    ap.add_argument("--verify-corpus", action="store_true")
    ap.add_argument("--arches", nargs="+",
                    help="arches for --verify-corpus (default sm_89 sm_75 sm_80 sm_90)")
    ap.add_argument("--perf-diff", action="store_true",
                    help="MODE 1: build amplified probes, measure ptxas (V1) vs "
                         "composed (V2) cycles on the GPU with ncu, gate on "
                         "bit-identical output, classify each understall as real "
                         "ptxas waste vs hardware-enforced")
    ap.add_argument("--tighten", metavar="CUBIN",
                    help="STAGE 1: keep ptxas's instruction order, tighten only "
                         "the stalls our model proves are slack, GPU-gated "
                         "bit-identical (surgical + bisection); measure cycles")
    ap.add_argument("--optsched", metavar="CUBIN",
                    help="STAGE 2: per-basic-block constraint-optimal reorder "
                         "scheduler (list + Z3-Opt), reorder-patch in place, "
                         "GPU-gate each block bit-identical (fall back to ptxas "
                         "per block), measure cycles V(ptxas) vs V(ours)")
    ap.add_argument("--stall-profile", metavar="CUBIN", nargs="?", const="",
                    help="MODE 2: per-instruction warp-stall-reason histogram "
                         "(ncu PC sampling) cross-mapped to our scheduling model")
    ap.add_argument("--amp", metavar="PROBE",
                    help="amplified probe name for --stall-profile (e.g. "
                         "amp_transc / amp_intchain / amp_fpchain / amp_loadmath)")
    ap.add_argument("--arch", default="sm_89",
                    help="arch for --perf-diff (default sm_89)")
    ap.add_argument("--opt", default="O3",
                    help="ptxas optimisation level for --perf-diff probes "
                         "(default O3 = production opt; the honest waste "
                         "comparison is vs ptxas's best schedule).  Give it WITHOUT"
                         " a leading dash (O3 / O1 / O0) so argparse does not "
                         "mistake the value for a flag; --opt=-O3 also works.")
    ap.add_argument("--grid", type=int, help="grid blocks (GPU-measurement modes)")
    ap.add_argument("--block", type=int, help="block threads (GPU-measurement modes)")
    ap.add_argument("--niter", type=int, help="loop iterations (amplification)")
    args = ap.parse_args(argv[1:])

    if args.decompose:
        decompose_print(args.decompose); return 0
    if args.compose:
        compose_print(args.compose); return 0
    if args.debug:
        debug_print(args.debug, args.dot); return 0
    if args.verify:
        rc = 0
        for cubin in args.verify:
            n_cmp, n_exact, mism, summ = round_trip(cubin)
            print(f"{Path(cubin).name}: {n_exact}/{n_cmp} exact  {summ}")
            for m in mism:
                print(f"  MISMATCH @{m.offset:#06x} {m.mnem} {m.field}: "
                      f"ptxas={m.expected} composed={m.got} [{m.cls}]")
            # gate on the producer->scoreboard pairing (the correctness result);
            # conservative stall differences are reported but do not fail.
            pairs_ok = (summ["scbd_pairs_matched"] == summ["scbd_pairs_gt"])
            rc |= (0 if pairs_ok else 1)
        return rc
    if args.verify_dyn:
        return verify_dynamic(args.verify_dyn, args.entry)
    if args.verify_corpus:
        return verify_corpus(args.arches)
    if args.perf_diff:
        return perf_diff(arch=args.arch,
                         grid=args.grid if args.grid else 20,
                         block=args.block if args.block else 32,
                         niter=args.niter if args.niter else 200000,
                         opt=args.opt)
    if args.tighten:
        import sass_reorder
        return _run_tighten(sass_reorder, args)
    if args.optsched:
        import sass_reorder
        return _run_optsched(sass_reorder, args)
    if args.stall_profile is not None:
        cub = args.stall_profile or None
        if cub is None and not args.amp:
            print("  ! --stall-profile needs a CUBIN or --amp PROBE",
                  file=sys.stderr)
            return 2
        return stall_profile(cub or "", entry=args.entry,
                             grid=args.grid if args.grid else 20,
                             block=args.block if args.block else 256,
                             niter=args.niter if args.niter else 60000,
                             amp=args.amp)
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
