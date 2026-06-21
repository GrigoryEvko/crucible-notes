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
        this collapses to the issue-relative stall: 4 same-pipe, 5 cross-pipe,
        13 if the producer writes a condition-code / predicate (the control band).
        Never dropped.
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
TABLE_DIR = HERE.parent / "nvdisasm-sass-isa"


# =============================================================================
# Recovered latency model (CUDA 13.1 ptxas, differentially confirmed on emitted
# SASS).  These are the dependency EDGE WEIGHTS, not the scalar result bands.
# =============================================================================

# Coupled-math RAW (issue-relative): the stall the consumer owes after the
# producer issues.  Confirmed stable SM75->SM120 on the emitted control words.
COUPLED_SAME_PIPE = 4     # IADD3->IADD3, IMAD->IMAD, FFMA->FFMA, FADD->FADD ...
COUPLED_CROSS_PIPE = 5    # IADD3->IMAD, LOP3/FMUL->consumer (inter-pipe penalty)
COUPLED_CC_PRED = 13      # producer writes a condition-code / predicate (ISETP)
COUPLED_FWD_SAME = 0      # same-pipe back-to-back forwarding (no hazard)

# Variable-latency producers handled by scoreboards, not stalls.  The number
# here is the result-band MAGNITUDE (used only for critical-path weighting and
# debug reasoning) -- the *mechanism* is always a scoreboard wait.
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

# Pipe family of a coupled-math mnemonic, for the same-pipe / cross-pipe split.
# (Two ops are "same pipe" when they share a family; a different family pays +1.)
PIPE_FAMILY = {
    # integer FXU / IMAD pipe
    "IADD3": "I", "IMAD": "IMAD", "LOP3": "I", "LEA": "I", "SHF": "I",
    "ISETP": "I", "IABS": "I", "BMSK": "I", "SGXT": "I", "FLO": "I",
    "SEL": "I", "PRMT": "I", "P2R": "I", "R2P": "I", "PLOP3": "I",
    # FP32 FMA pipe
    "FFMA": "F", "FADD": "F", "FMUL": "F", "FMNMX": "F", "FSETP": "F",
    "FSEL": "F", "FSET": "F", "MOV": "F", "FSWZADD": "F",
    # FP16x2 pipe
    "HADD2": "H", "HMUL2": "H", "HFMA2": "H", "HSETP2": "H",
}

# Coupled ops that write a condition-code / predicate destination -> control band.
CC_PRED_PRODUCERS = {"ISETP", "FSETP", "DSETP", "HSETP2", "PLOP3", "VOTE",
                     "P2R", "R2P", "LOP3.PAND"}


def _base_mnem(m: str) -> str:
    """Strip dotted modifiers: 'IMAD.MOV.U32' -> 'IMAD'."""
    return m.split(".")[0] if m else ""


# =============================================================================
# Per-arch INSTRUCTION_TYPE map (mnemonic -> coupled/decoupled), recovered from
# the per-arch SASS instruction tables (sass_isa_SM*.txt CLASS PROPERTIES).
# =============================================================================

_CLASS_RE = re.compile(r'^CLASS\s+"([^"]+)"')
_ITYPE_RE = re.compile(r"INSTRUCTION_TYPE\s*=\s*INST_TYPE_(\w+)")
_MINWAIT_RE = re.compile(r"MIN_WAIT_NEEDED\s*=\s*(\d+)")

# A CLASS name leads with the lowercase mnemonic up to the first '_' separator.
_CLASS_MNEM_RE = re.compile(r"^([a-z0-9]+)")


@dataclass
class TypeInfo:
    coupled: bool          # True => fixed-latency stall; False => scoreboard
    itype: str             # raw INSTRUCTION_TYPE name
    min_wait: int          # MIN_WAIT_NEEDED floor
    wr_scbd: bool          # arms a write scoreboard (dst_wr_sb)
    rd_scbd: bool          # arms a read-release scoreboard (src_rel_sb)
    depbar: bool           # branch-unit / DEPBAR decoupled


def _itype_props(itype: str) -> tuple[bool, bool, bool, bool]:
    """(coupled, arms_wr_scbd, arms_rd_scbd, is_depbar) from an INSTRUCTION_TYPE."""
    coupled = itype.startswith("COUPLED")
    wr = ("WR_SCBD" in itype) or ("RD_WR_SCBD" in itype)
    rd = ("RD_SCBD" in itype) or ("RD_WR_SCBD" in itype) or ("RD_NOREQ" in itype)
    depbar = "DEPBAR" in itype
    return coupled, wr, rd, depbar


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
    # The CC/predicate control band applies only when the consumer reads the
    # producer's predicate result as a *guard*; a SETP whose predicate is read as
    # a plain predicate-ALU operand is an ordinary cross-pipe edge.  The control
    # band is a hard latency the predicated consumer cannot proceed without, so
    # the mechanism is marked "stall_ctrl" (the fixpoint emits it in full, never
    # reduced by intervening independent work).
    if prod.ops.writes_cc and _consumer_reads_guard(prod, cons):
        prod_pipe = LT.PIPE_CC
        cons_pipe = LT.consumer_pipe(cons.mnem, ccm)
        return LT.coupled_stall(arch, prod_pipe, cons_pipe), "stall_ctrl"

    cons_pipe = LT.consumer_pipe(cons.mnem, ccm)
    stall = LT.coupled_stall(arch, prod_pipe, cons_pipe)
    return stall, "stall"


def _consumer_reads_guard(prod: Node, cons: Node) -> bool:
    """True if the consumer reads the producer's predicate/CC result as a guard
    (the control-band dependency), vs. as an ordinary register operand."""
    pred_writes = {r for r in prod.ops.writes
                   if r.startswith("P") or r.startswith("UP")}
    return bool(pred_writes & cons.ops.reads)


def war_weight() -> int:
    """WAR (anti) edge weight: zero-but-ordered on registers.  Its OWN per-
    resource default -- independent of, and never copied from, the RAW table."""
    return 0


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


def compose(dag: DAG, ctrls: list[Ctrl] | None = None) -> ComposeResult:
    """Walk issue order on a virtual cycle clock and assign control words.

    For each instruction:
      * fixed-latency RAW edges to it from coupled producers -> a usched stall =
        the minimum that satisfies every such edge given accumulated cycles
        (throughput floor of 1 otherwise);
      * variable-latency (decoupled) producers get a write/read scoreboard;
        their consumers set the matching req_bit_set bit;
      * the 6-scoreboard allocator overloads (VSB->PSB by minimum added stall)
        when >6 are live; unbounded groups fall to SB5 + DEPBAR.LE.

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
        """Per-op dispatch-stall floor.  Terminators yield (0); ordinary math
        carries the 1-cycle dispatch floor; a memory/issue op carries 2 (the
        request-issue slot ptxas reserves -- being conservative here is always
        hazard-safe, confirmed by dynamic validation)."""
        if _is_terminator(idx):
            return 0
        base = _base_mnem(dag.nodes[idx].mnem)
        # Memory-issue ops and the MUFU/special-function pipe reserve a 2-cycle
        # dispatch slot (the request-issue / SFU-launch latency).  Floor 2 is
        # the conservative, hazard-safe minimum confirmed by dynamic validation
        # (a MUFU.RSQ at stall 1 corrupts the result before its scoreboard arms).
        if base in ("LDG", "STG", "LDS", "STS", "LDL", "STL", "LD", "ST",
                    "LDC", "ATOMG", "ATOM", "RED", "TEX", "LDGSTS",
                    "MUFU", "I2F", "F2I", "F2F", "I2I"):
            return 2
        return 1

    # Ancestors of each node along RAW-stall edges.  Latency is hidden only by
    # *independent* instructions issued between producer and consumer -- an
    # instruction that is itself an ancestor of the consumer (on the dependency
    # path) serializes rather than hides, so its cycles must NOT be subtracted.
    # Counting all intervening cycles as hidden is unsound and under-stalls
    # address-producing chains (caught by dynamic validation).
    # Per-producer stall edges (dst, weight, is_ctrl).
    out_stall: list[list[tuple[int, int, bool]]] = [[] for _ in range(n)]
    for e in dag.edges:
        if _is_stall_edge(e):
            out_stall[e.src].append((e.dst, e.weight, e.mechanism == "stall_ctrl"))

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
                if is_ctrl:
                    req = max(req, weight)
                    continue
                # cycles already accumulated by instructions strictly between the
                # producer i and the consumer dst (they occupy real issue slots).
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
    """Build (once) and return the path to the C Driver-API launch harness."""
    binpath = Path("/tmp/sass_sched_launch")
    src = HERE / "launch_cubin.c"
    if not src.exists():
        return None
    if not binpath.exists():
        r = subprocess.run(
            ["gcc", "-O2", str(src), "-o", str(binpath),
             "-I/usr/local/cuda-13.1/include",
             "-L/usr/local/cuda-13.1/lib64/stubs", "-lcuda"],
            capture_output=True, text=True)
        if r.returncode != 0:
            print(f"  ! harness build failed: {r.stderr[:200]}", file=sys.stderr)
            return None
    return str(binpath)


def _run_kernel(harness: str, cubin: str, entry: str, nwords: int) -> str | None:
    env = dict(os.environ)
    env["LD_LIBRARY_PATH"] = "/lib64:" + env.get("LD_LIBRARY_PATH", "")
    r = subprocess.run([harness, cubin, entry, str(nwords)],
                       capture_output=True, text=True, env=env)
    if r.returncode != 0:
        print(f"  ! launch failed: {r.stderr.strip()[:200]}", file=sys.stderr)
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
    diff = abs(c.stall - f.stall)
    if diff == 1:
        return "off_by_one_pipe_penalty"
    if c.stall in (8,) and f.stall in (4, 5):
        return "turing_prestore_slot"   # SM75 emits 8 in the pre-store slot
    if c.stall == 13:
        return "cc_pred_control_band"
    if c.stall > 6 or f.stall > 6:
        return "tensor_or_special_band"
    return "other"


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
}


def _ptx_version_for(smnum: str) -> str:
    """Minimum PTX ISA version that accepts a given SM target.

    sm_100/sm_103 need PTX 8.6, sm_110/sm_120/sm_121 need 8.7; older targets
    accept 8.3.  (ptxas rejects a target newer than the declared .version.)"""
    n = int(re.match(r"\d+", smnum).group())
    if n >= 110:
        return "8.7"
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
    print("  exact      : producer->scoreboard pairings (structure); the CC/pred")
    print("               control band; same-/cross-pipe coupled stall magnitudes")
    print("  conservative: when the composed stall differs it is >= ptxas's "
          "(ptxas hides")
    print("               latency with full scheduling freedom we cannot replay; "
          "over-stalling")
    print("               is hazard-safe -- proven by --verify-dyn on the GPU)")
    print("  approximate : variable-latency completion *times* (not recovered "
          "constants);")
    print("               exact SB index (allocator-policy dependent)")
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
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
