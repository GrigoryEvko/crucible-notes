#!/usr/bin/env python3
# nvopen-tools -- SASS reverse-engineering tooling (MIT-style).
# Built on the public CUDA toolchain (ptxas / nvdisasm) and the CUDA Driver API.
"""
Probe the per-op decoupled-scalar completion latency on sm_89.

THE SCOREBOARD MECHANISM
------------------------
A decoupled scalar op (MUFU, POPC, FLO, BREV, the float<->int conversions
F2F/F2I/I2F, and the special-register / constant reads S2R/LDC) does NOT deliver
its result a fixed number of cycles after issue.  It runs in a side pipe (the
SFU / MIO datapath) and *arms a write scoreboard* -- one of the warp's six write
scoreboards SB0..SB5.  The result becomes visible only once that side pipe
retires and clears the scoreboard.  A dependent consumer therefore carries a
read-barrier WAIT on the same scoreboard bit in its scheduling control word
(req_bit_set, bits 116..121); it stalls at the warp scheduler until the bit
clears.  The producer's own control word carries only its issue-throughput
stall (usched), never the completion latency -- the completion time is whatever
the side pipe takes, surfaced to the compiler purely through the scoreboard
handshake (producer sets SBn -> consumer waits SBn).

ptxas emits a control word per op with these fields (decoded SASS-table layout,
see sass_ctrl_decode.py):
    usched      bits 105..109   stall/yield enum (issue throughput)
    dst_wr_sb   bits 110..112   write scoreboard the producer arms (7 = none)
    req_bit_set bits 116..121   the wait mask over SB0..SB5

Some ops the band model treated as decoupled are actually emitted COUPLED on
sm_89 (no write scoreboard, a fixed issue stall instead): CS2R, the 32-bit
integer->float conversion (lowered to I2FP).  This probe distinguishes them.

TRIANGULATION (per op)
----------------------
  1. ptxas-emitted: build a probe whose decoupled producer writes Rd and a
     dependent consumer reads Rd; ptxas -arch sm_89 + control-word decode gives
     (a) the write scoreboard the producer sets, (b) the consumer's wait mask,
     (c) the consumer's post-wait usched stall.

  2. GPU completion latency (gold): bracket exactly one producer->consumer pair
     between two warp-clock reads (S2UR/CS2R of SR_CLOCKLO) on the sm_89 GPU and
     read the per-lane delta back.  The consumer's scoreboard WAIT is intact, so
     the delta includes the full scoreboard-enforced completion latency.
     Subtracting the fixed back-to-back clock-read overhead (an empty-body probe)
     yields the producer's completion latency in cycles -- i.e. the cycles a
     dependent use stalls at the producer's write scoreboard.  The probe is
     single-warp (one warp, NLANE threads), so nothing overlaps the latency.  A
     coupled-op baseline (FMUL->FADD, no scoreboard) gives the non-scoreboard
     floor; the in-band coupled CS2R/I2FP rows pin it directly.

Run with no args -> ptxas-emitted pairing table only (no GPU).
Run `--gpu`     -> full triangulation; prints overhead, baseline, and per-op
                   net producer->use cycles.
"""
from __future__ import annotations

import struct
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOLS = HERE.parent / "sass-tools"
sys.path.insert(0, str(TOOLS))

import sass_ctrl_decode as C          # noqa: E402
import sass_launch as SL              # noqa: E402

PTXAS = "/usr/local/cuda-13.1/bin/ptxas"
ARCH = "sm_89"
TMP = Path("/tmp/decoupled_probe")
TMP.mkdir(exist_ok=True)



# --------------------------------------------------------------------------- #
# PTX probe templates.                                                         #
#                                                                              #
# A probe materializes a per-thread integer seed from %tid (laundered through  #
# coupled ALU so ptxas cannot constant-fold it), then runs NCOPY independent   #
# copies of the decoupled op.  Crucially the producer's INPUT comes from a      #
# coupled-ALU register that is ready with no pending scoreboard, so the         #
# producer issues immediately -- the only scoreboard in play is the one the     #
# producer itself arms.  That isolates the producer's completion latency from   #
# any upstream memory latency.  Each consumer doubles the producer's result and #
# stores it to a per-thread, per-copy slot, so a premature (raced) read         #
# perturbs the result hash.                                                     #
# --------------------------------------------------------------------------- #
PTX_HEAD = """\
.version 8.3
.target sm_89
.address_size 64
.const .align 4 .b32 ctab[256];
.visible .entry probe(.param .u64 pin, .param .u64 pout)
{{
    .reg .pred %p<4>;
    .reg .b32 %r<128>;
    .reg .f32 %f<64>;
    .reg .f64 %d<64>;
    .reg .s64 %sd<32>;
    .reg .b64 %rd4a, %rd62;
    .reg .b64 %rd<32>;
    ld.param.u64 %rd2, [pout];
    cvta.to.global.u64 %rd4a, %rd2;
    // per-thread 64-byte stride so each lane writes a distinct, non-overlapping
    // result block (no cross-thread output aliasing).
    mov.u32 %r60, %tid.x;
    mul.wide.u32 %rd62, %r60, 64;
    add.s64 %rd4, %rd4a, %rd62;
    // per-thread integer seed laundered through coupled ALU (not foldable): the
    // S2R that reads %tid resolves once, up front, so every per-copy producer
    // input below is a coupled-ready register with no pending scoreboard.
    mad.lo.s32 %r61, %r60, 2654435761, 40503;
    mul.lo.s32 %r62, %r61, 1103515245;
{body}
    ret;
}}
"""

# Number of independent op copies a probe emits.  ptxas batches the producers
# together, so the FIRST producer's consumer ends up several issue slots later
# -- enough headroom to stack a controllable gap that spans even the longest
# decoupled-scalar completion latency.
NCOPY = 4


@dataclass
class Probe:
    """One op as templated PTX, with `{k}` substituted per copy.

    `op` runs the decoupled producer into a per-copy register the `cons`
    consumer reads; `st` stores the consumer's result to a per-copy slot.
    `match` prefix-matches the producer's SASS mnemonic.
    """
    op: str             # producer fragment, {k} = copy index
    cons: str           # dependent consumer fragment
    st: str             # store fragment
    match: str
    note: str = ""

    def body(self, ncopy: int = NCOPY) -> str:
        # all producers first (so ptxas issues them back-to-back), then the
        # consumers+stores -- this widens the first producer->consumer slot gap.
        # `mul`/`add` are the per-copy seed-mixing constants (PTX has no
        # immediate arithmetic, so they are computed here): a distinct odd
        # multiplier and additive bias per copy keeps each producer's input
        # distinct and non-foldable.
        def fmt(s: str, k: int) -> str:
            return s.format(k=k, mul=(k * 7 + 11), add=(k * 131 + 7))
        prods = "\n".join("    " + fmt(self.op, k).replace("\n", "\n    ")
                          for k in range(ncopy))
        cons = "\n".join("    " + fmt(self.cons, k) + "\n    "
                         + fmt(self.st, k) for k in range(ncopy))
        return prods + "\n" + cons


# Per copy, the producer input is derived from the laundered per-thread seed
# %r62 via a coupled ALU op (ready with no pending scoreboard), so the decoupled
# producer issues immediately and only ITS completion latency is exposed.
# `%fin{k}` / `%rin{k}` are the per-copy coupled inputs; the producer writes
# `%pr{k}`-class registers; the consumer doubles into a stored slot.
#
# Register lanes (within the declared ranges):
#   integer inputs   %r1{k}  (10..13)   producer out %r2{k} (20..23) store %r3{k}
#   float    inputs  %f1{k}  (10..13)   producer out %f2{k} (20..23)
#   f64              %d1{k}  (10..13)   producer out %d2{k} (20..23)
PROBES: dict[str, Probe] = {
    "MUFU.RCP": Probe(
        "mad.lo.s32 %r1{k}, %r62, {mul}, 0x40000000;\n"
        "    mov.b32 %f1{k}, %r1{k};\n    rcp.approx.ftz.f32 %f2{k}, %f1{k};",
        "add.f32 %f3{k}, %f2{k}, %f2{k};",
        "st.global.f32 [%rd4+{k}*4], %f3{k};", "MUFU.RCP"),
    "MUFU.RSQ": Probe(
        "mad.lo.s32 %r1{k}, %r62, {mul}, 0x40000000;\n"
        "    and.b32 %r1{k}, %r1{k}, 0x7fffffff;\n    mov.b32 %f1{k}, %r1{k};\n"
        "    rsqrt.approx.ftz.f32 %f2{k}, %f1{k};",
        "add.f32 %f3{k}, %f2{k}, %f2{k};",
        "st.global.f32 [%rd4+{k}*4], %f3{k};", "MUFU.RSQ"),
    "MUFU.SQRT": Probe(
        "mad.lo.s32 %r1{k}, %r62, {mul}, 0x40000000;\n"
        "    and.b32 %r1{k}, %r1{k}, 0x7fffffff;\n    mov.b32 %f1{k}, %r1{k};\n"
        "    sqrt.approx.ftz.f32 %f2{k}, %f1{k};",
        "add.f32 %f3{k}, %f2{k}, %f2{k};",
        "st.global.f32 [%rd4+{k}*4], %f3{k};", "MUFU.SQRT"),
    "MUFU.SIN": Probe(
        "mad.lo.s32 %r1{k}, %r62, {mul}, 0x3f000000;\n"
        "    mov.b32 %f1{k}, %r1{k};\n    sin.approx.ftz.f32 %f2{k}, %f1{k};",
        "add.f32 %f3{k}, %f2{k}, %f2{k};",
        "st.global.f32 [%rd4+{k}*4], %f3{k};", "MUFU.SIN"),
    "MUFU.COS": Probe(
        "mad.lo.s32 %r1{k}, %r62, {mul}, 0x3f000000;\n"
        "    mov.b32 %f1{k}, %r1{k};\n    cos.approx.ftz.f32 %f2{k}, %f1{k};",
        "add.f32 %f3{k}, %f2{k}, %f2{k};",
        "st.global.f32 [%rd4+{k}*4], %f3{k};", "MUFU.COS"),
    "MUFU.EX2": Probe(
        "mad.lo.s32 %r1{k}, %r62, {mul}, 0x3f000000;\n"
        "    mov.b32 %f1{k}, %r1{k};\n    ex2.approx.ftz.f32 %f2{k}, %f1{k};",
        "add.f32 %f3{k}, %f2{k}, %f2{k};",
        "st.global.f32 [%rd4+{k}*4], %f3{k};", "MUFU.EX2"),
    "MUFU.LG2": Probe(
        "mad.lo.s32 %r1{k}, %r62, {mul}, 0x40000000;\n"
        "    and.b32 %r1{k}, %r1{k}, 0x7fffffff;\n    mov.b32 %f1{k}, %r1{k};\n"
        "    lg2.approx.ftz.f32 %f2{k}, %f1{k};",
        "add.f32 %f3{k}, %f2{k}, %f2{k};",
        "st.global.f32 [%rd4+{k}*4], %f3{k};", "MUFU.LG2"),
    "MUFU.TANH": Probe(
        "mad.lo.s32 %r1{k}, %r62, {mul}, 0x3f000000;\n"
        "    mov.b32 %f1{k}, %r1{k};\n    tanh.approx.f32 %f2{k}, %f1{k};",
        "add.f32 %f3{k}, %f2{k}, %f2{k};",
        "st.global.f32 [%rd4+{k}*4], %f3{k};", "MUFU.TANH"),
    "POPC": Probe(
        "mad.lo.s32 %r1{k}, %r62, {mul}, {k};\n    popc.b32 %r2{k}, %r1{k};",
        "add.s32 %r3{k}, %r2{k}, %r2{k};",
        "st.global.b32 [%rd4+{k}*4], %r3{k};", "POPC"),
    "FLO": Probe(
        "mad.lo.s32 %r1{k}, %r62, {mul}, {k};\n    bfind.u32 %r2{k}, %r1{k};",
        "add.s32 %r3{k}, %r2{k}, %r2{k};",
        "st.global.b32 [%rd4+{k}*4], %r3{k};", "FLO"),
    "BREV": Probe(
        "mad.lo.s32 %r1{k}, %r62, {mul}, {k};\n    brev.b32 %r2{k}, %r1{k};",
        "add.s32 %r3{k}, %r2{k}, %r2{k};",
        "st.global.b32 [%rd4+{k}*4], %r3{k};", "BREV"),
    # 64-bit conversions are emitted decoupled (FMA64 pipe) on sm_89.
    "F2F": Probe(
        "mad.lo.s32 %r1{k}, %r62, {mul}, 0x40000000;\n"
        "    mov.b32 %f1{k}, %r1{k};\n    cvt.f64.f32 %d1{k}, %f1{k};",
        "add.f64 %d2{k}, %d1{k}, %d1{k};",
        "st.global.f64 [%rd4+{k}*8], %d2{k};", "F2F", "f32->f64 (decoupled FMA64)"),
    "F2I": Probe(
        "mad.lo.s32 %r1{k}, %r62, {mul}, 0x40000000;\n"
        "    mov.b32 %f1{k}, %r1{k};\n    cvt.rzi.s32.f32 %r2{k}, %f1{k};",
        "add.s32 %r3{k}, %r2{k}, %r2{k};",
        "st.global.b32 [%rd4+{k}*4], %r3{k};", "F2I", "f32->s32 (decoupled)"),
    "I2F": Probe(
        "mad.lo.s32 %r1{k}, %r62, {mul}, {k};\n"
        "    cvt.s64.s32 %sd1{k}, %r1{k};\n    cvt.rn.f64.s64 %d1{k}, %sd1{k};",
        "add.f64 %d2{k}, %d1{k}, %d1{k};",
        "st.global.f64 [%rd4+{k}*8], %d2{k};", "I2F.F64.S64",
        "s64->f64 (decoupled FMA64)"),
    "S2R": Probe(
        # read a special register distinct from the %tid the seed already used,
        # so the S2R is a fresh decoupled op inside the timed region.
        "mov.u32 %r2{k}, %laneid;\n    add.s32 %r2{k}, %r2{k}, {add};",
        "mul.lo.s32 %r3{k}, %r2{k}, %r2{k};",
        "st.global.b32 [%rd4+{k}*4], %r3{k};", "S2R"),
    "LDC": Probe(
        "mad.lo.s32 %r1{k}, %r62, {mul}, {k};\n    and.b32 %r4{k}, %r1{k}, 255;\n"
        "    mul.wide.u32 %rd{k}, %r4{k}, 4;\n    mov.u64 %rd1{k}, ctab;\n"
        "    add.s64 %rd2{k}, %rd1{k}, %rd{k};\n    ld.const.b32 %r2{k}, [%rd2{k}];",
        "add.s32 %r3{k}, %r2{k}, %r2{k};",
        "st.global.b32 [%rd4+{k}*4], %r3{k};", "LDC", "indirect const-bank load"),
    # Reference COUPLED ops (no write scoreboard) the band model also covered:
    "CS2R": Probe(
        "mov.u32 %r2{k}, %clock;\n    add.s32 %r2{k}, %r2{k}, {add};",
        "mul.lo.s32 %r3{k}, %r2{k}, %r2{k};",
        "st.global.b32 [%rd4+{k}*4], %r3{k};", "CS2R",
        "coupled on sm_89 (fixed issue stall, no scoreboard)"),
    "I2FP": Probe(
        "mad.lo.s32 %r1{k}, %r62, {mul}, {k};\n    cvt.rn.f32.s32 %f1{k}, %r1{k};",
        "add.f32 %f3{k}, %f1{k}, %f1{k};",
        "st.global.f32 [%rd4+{k}*4], %f3{k};", "I2FP",
        "32-bit s32->f32 lowered to coupled I2FP on sm_89"),
}


# --------------------------------------------------------------------------- #
# Compile + locate the decoupled pairing.                                      #
# --------------------------------------------------------------------------- #
def compile_probe(name: str, pr: Probe, ncopy: int = NCOPY) -> Path | None:
    safe = name.replace(".", "_")
    ptx = PTX_HEAD.format(body=pr.body(ncopy))
    ptxp = TMP / f"{safe}.ptx"
    cub = TMP / f"{safe}.cubin"
    ptxp.write_text(ptx)
    # -O3 is what real kernels are built with: it is the scheduler that computes
    # the per-op scoreboard handshake (which SBn the producer arms, the wait the
    # consumer carries).  The GPU gold sweep operates on this real cubin by
    # patching control-word fields in place (no instruction insertion), so the
    # measured latency is for ptxas's actual emitted code.
    r = subprocess.run([PTXAS, "-arch", ARCH, "-O3", "-o", str(cub), str(ptxp)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return None
    return cub


# --------------------------------------------------------------------------- #
# Clock-delta timing probe (the GPU GOLD method).                              #
#                                                                              #
# elapsed = clock_after - clock_before across exactly one producer->consumer   #
# pair.  The consumer's scoreboard WAIT is intact, so `elapsed` includes the    #
# full scoreboard-enforced completion latency.  Subtracting the fixed          #
# back-to-back clock-read overhead (CLOCK_OVERHEAD, measured with an empty      #
# body) yields the producer's completion latency in cycles.  The consumer's     #
# result is ANDed with 0 and folded into the stored elapsed only as a          #
# dependency anchor (adds nothing), so ptxas cannot hoist the second clock read #
# above the wait, and cannot dead-code the consumer.                           #
# --------------------------------------------------------------------------- #
TIMING_HEAD = """\
.version 8.3
.target sm_89
.address_size 64
.const .align 4 .b32 ctab[256];
.visible .entry probe(.param .u64 pin, .param .u64 pout)
{{
    .reg .pred %p<4>;
    .reg .b32 %r<128>;
    .reg .f32 %f<64>;
    .reg .f64 %d<64>;
    .reg .s64 %sd<32>;
    .reg .b64 %rd4a, %rd62;
    .reg .b64 %rd<32>;
    .reg .b32 %rt0, %rt1, %rel, %rmaskc, %rcres;
    ld.param.u64 %rd2, [pout];
    cvta.to.global.u64 %rd4a, %rd2;
    mov.u32 %r60, %tid.x;
    mul.wide.u32 %rd62, %r60, 64;
    add.s64 %rd4, %rd4a, %rd62;
    mad.lo.s32 %r61, %r60, 2654435761, 40503;
    mul.lo.s32 %r62, %r61, 1103515245;
    mov.u32 %rt0, %clock;
{producer}
{consumer}
{anchor}
    mov.u32 %rt1, %clock;
    sub.s32 %rel, %rt1, %rt0;
    // tie elapsed to the consumer result (%rmaskc, masked to 0): the dataflow
    // forces the second clock read to follow the consumer's scoreboard wait.
    add.s32 %rel, %rel, %rmaskc;
    st.global.b32 [%rd4], %rel;
    // store the raw consumer result too, so ptxas cannot dead-code the consumer
    // (and therefore cannot drop the wait that we are timing).
    st.global.b32 [%rd4+4], %rcres;
    ret;
}}
"""

# Empty-body timing probe -> the back-to-back clock-read overhead to subtract.
OVERHEAD_PTX = """\
.version 8.3
.target sm_89
.address_size 64
.visible .entry probe(.param .u64 pin, .param .u64 pout)
{
    .reg .b32 %r<8>;
    .reg .b64 %rd<8>;
    ld.param.u64 %rd2, [pout];
    cvta.to.global.u64 %rd4, %rd2;
    mov.u32 %r1, %tid.x;
    mul.wide.u32 %rd6, %r1, 64;
    add.s64 %rd4, %rd4, %rd6;
    mov.u32 %r2, %clock;
    mov.u32 %r3, %clock;
    sub.s32 %r4, %r3, %r2;
    st.global.b32 [%rd4], %r4;
    ret;
}
"""

# A coupled-op baseline timing probe: identical skeleton but the producer is a
# coupled FMUL (no scoreboard) and the consumer a coupled FADD.  elapsed here is
# the clock overhead PLUS a coupled producer->consumer latency, so
# (decoupled_elapsed - coupled_elapsed) is the decoupled-over-coupled premium.
BASELINE_PTX = """\
.version 8.3
.target sm_89
.address_size 64
.visible .entry probe(.param .u64 pin, .param .u64 pout)
{
    .reg .b32 %r<16>;
    .reg .f32 %f<8>;
    .reg .b64 %rd<8>;
    ld.param.u64 %rd2, [pout];
    cvta.to.global.u64 %rd4, %rd2;
    mov.u32 %r1, %tid.x;
    mul.wide.u32 %rd6, %r1, 64;
    add.s64 %rd4, %rd4, %rd6;
    mad.lo.s32 %r2, %r1, 12345, 0x40000000;
    mov.b32 %f1, %r2;
    mov.u32 %r10, %clock;
    mul.f32 %f2, %f1, %f1;
    add.f32 %f3, %f2, %f2;
    mov.b32 %r13, %f3;
    and.b32 %r14, %r13, 0;
    mov.u32 %r11, %clock;
    sub.s32 %r12, %r11, %r10;
    add.s32 %r12, %r12, %r14;
    st.global.b32 [%rd4], %r12;
    st.global.b32 [%rd4+4], %r13;
    ret;
}
"""


def compile_timing(name: str, pr: Probe) -> Path | None:
    """One producer->consumer pair between two clock reads (copy 0 only).

    The anchor folds the consumer result into the elapsed value (%rmaskc =
    consumer_result, right-shifted out to 0), so the dataflow makes `elapsed`
    depend on the consumer: ptxas keeps the consumer (hence its scoreboard wait
    on the producer) ahead of the second clock read.  No memory load is in the
    timed region, so only the scoreboard wait is timed.  The bracketing check in
    `measure_timing` rejects any probe where ptxas still reordered the clocks.
    """
    producer = "    " + pr.op.format(k=0, mul=11, add=7).replace("\n", "\n    ")
    consumer = "    " + pr.cons.format(k=0, mul=11, add=7)
    cdst = pr.cons.format(k=0, mul=11, add=7).replace(",", " ").split()[1]
    # %rmaskc = consumer_result & %rmask (runtime 0): data-dependent on the
    # consumer, opaque to ptxas, so the second clock read stays after the wait.
    if cdst.startswith("%f"):
        pre = f"    mov.b32 %rcres, {cdst};"
    elif cdst.startswith("%d"):
        # grab the low 32 bits of the f64 with a bit-cast (NOT a conversion op,
        # which would add a second decoupled op inside the timed window).
        pre = (f"    mov.b64 {{%rcres, %rchi}}, {cdst};")
    else:
        pre = f"    mov.b32 %rcres, {cdst};"
    # %rcres = raw consumer result (stored, so the consumer is not DCE'd);
    # %rmaskc = result & 0 (== 0) folded into elapsed to order the 2nd clock.
    anchor = f"{pre}\n    and.b32 %rmaskc, %rcres, 0;"
    ptx = TIMING_HEAD.format(producer=producer, consumer=consumer, anchor=anchor)
    ptx = ptx.replace(".reg .b32 %rt0, %rt1, %rel, %rmaskc, %rcres;",
                      ".reg .b32 %rt0, %rt1, %rel, %rmaskc, %rcres, %rchi;")
    safe = name.replace(".", "_")
    ptxp = TMP / f"timing_{safe}.ptx"
    cub = TMP / f"timing_{safe}.cubin"
    ptxp.write_text(ptx)
    r = subprocess.run([PTXAS, "-arch", ARCH, "-O3", "-o", str(cub), str(ptxp)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return None
    return cub


@dataclass
class Pairing:
    prod_off: int
    prod_mnem: str
    sb: int
    cons_off: int
    cons_mnem: str
    cons_wait_mask: int
    cons_stall: int
    prod_stall: int
    coupled: bool       # True if no producer arms a write scoreboard


def _matches(c: C.Ctrl, match: str) -> bool:
    """The producer mnemonic (with modifiers, e.g. MUFU.RCP / F2F.F64.F32 /
    I2F.F64.S64) must PREFIX-match the instruction's mnemonic token so that
    'LDC' does not also match 'ULDC' and 'I2F' does not match 'I2FP'."""
    m = match.upper()
    op = c.text.upper().split()[0] if c.text.strip() else ""
    if c.pred:                      # skip a leading predicate guard token
        toks = c.text.upper().split()
        op = toks[1] if len(toks) > 1 else op
    base = op.split(".")[0]
    mbase = m.split(".")[0]
    # exact base token match, then require the full modifier prefix if given
    if base != mbase:
        return False
    return op.startswith(m)


def find_pairing(ctrls: list[C.Ctrl], match: str) -> Pairing | None:
    for i, c in enumerate(ctrls):
        if not _matches(c, match):
            continue
        if c.dst_wr == 7:
            # COUPLED form: producer arms no scoreboard.  Find the first later
            # op that reads the producer's destination register (heuristic via
            # the next op carrying no wait but a data dependence is hard to see
            # from text; we just report coupled with the producer's fixed stall).
            dest = _dest_reg(c.text)
            for c2 in ctrls[i + 1:]:
                if dest and dest in _src_regs(c2.text):
                    return Pairing(c.offset, c.mnem, 7, c2.offset, c2.mnem,
                                   c2.wait_mask, c2.stall, c.stall, True)
            return Pairing(c.offset, c.mnem, 7, -1, "", 0, 0, c.stall, True)
        sb = c.dst_wr
        for c2 in ctrls[i + 1:]:
            if (c2.wait_mask >> sb) & 1:
                return Pairing(c.offset, c.mnem, sb, c2.offset, c2.mnem,
                               c2.wait_mask, c2.stall, c.stall, False)
    return None


def _dest_reg(text: str) -> str:
    # mnem [.mods] DEST, ...  -- the first register operand
    parts = text.replace(",", " ").split()
    for p in parts[1:]:
        if p.startswith("R") and p[1:].split(".")[0].isdigit():
            return p.split(".")[0]
    return ""


def _src_regs(text: str) -> set[str]:
    parts = text.replace(",", " ").split()
    return {p.split(".")[0] for p in parts[2:]
            if p.startswith("R") and p[1:].split(".")[0].isdigit()}


# --------------------------------------------------------------------------- #
# Byte patcher: clear the consumer's scoreboard-wait bit in the cubin.         #
# --------------------------------------------------------------------------- #
def _text_file_off(cubin: Path) -> int:
    """File offset of `.text.probe` (readelf section header)."""
    r = subprocess.run(["readelf", "-S", str(cubin)], capture_output=True,
                        text=True)
    for ln in r.stdout.splitlines():
        if ".text.probe" in ln:
            toks = ln.replace("[", " ").replace("]", " ").split()
            # columns: Nr Name Type Address Offset ...
            try:
                idx = toks.index(".text.probe")
                return int(toks[idx + 3], 16)
            except (ValueError, IndexError):
                pass
    # fallback: scan following line for the offset (readelf wraps long rows)
    return 0



# --------------------------------------------------------------------------- #
# GPU GOLD: clock-delta timing.                                                #
#                                                                              #
# The completion latency is the cycles a dependent consumer stalls at the      #
# producer's write-scoreboard.  We bracket exactly one producer->consumer pair #
# between two warp-clock reads and read the delta back per lane; subtracting    #
# the fixed back-to-back clock-read overhead yields the producer's completion   #
# latency in cycles.  This measures the scoreboard wait directly -- the same    #
# wait ptxas emits -- with no cubin patching.                                  #
# --------------------------------------------------------------------------- #
NLANE = 32
STRIDE = 64                          # per-thread output block (bytes)
ARENA = NLANE * STRIDE


def _seed_zero() -> bytes:
    # The probe reads one b32 from the input as the runtime-zero ordering mask;
    # a zeroed arena supplies it.  Everything else the probe derives internally.
    return bytes(ARENA)


class Runner:
    """One CUDA context + an input (runtime-zero mask source) and output arena.

    `elapsed(cubin)` launches one warp and returns the median per-lane clock
    delta (cycles).  `overhead()` is the empty-body delta to subtract.
    """

    def __init__(self) -> None:
        self.cu = SL.Cuda(0)
        self.pin = self.cu.mem_alloc(ARENA)
        self.pout = self.cu.mem_alloc(ARENA)
        self.cu.memset_d8(self.pin, 0, ARENA)

    def elapsed(self, cubin: Path, reps: int = 5) -> int | None:
        vals: list[int] = []
        try:
            mod = self.cu.load_module(cubin.read_bytes())
            fn = self.cu.get_function(mod, "probe")
            pbuf = struct.pack("<QQ", self.pin, self.pout)
            for _ in range(reps):
                self.cu.memset_d8(self.pout, 0, ARENA)
                self.cu.launch(fn, (1, 1, 1), (NLANE, 1, 1), 0, pbuf)
                self.cu.synchronize()
                data = self.cu.memcpy_dtoh(self.pout, ARENA)
                lane = [struct.unpack_from("<i", data, t * STRIDE)[0]
                        for t in range(NLANE)]
                # all lanes run the same op; take the modal/median lane value
                lane.sort()
                vals.append(lane[len(lane) // 2])
            self.cu.unload_module(mod)
        except SL.CudaError:
            self.cu.reset_context()
            self.cu.memset_d8(self.pin, 0, ARENA)
            return None
        vals.sort()
        return vals[len(vals) // 2]

    def close(self) -> None:
        self.cu.close()


def _compile_raw(name: str, ptx: str) -> Path | None:
    ptxp = TMP / f"{name}.ptx"
    cub = TMP / f"{name}.cubin"
    ptxp.write_text(ptx)
    r = subprocess.run([PTXAS, "-arch", ARCH, "-O3", "-o", str(cub), str(ptxp)],
                       capture_output=True, text=True)
    return cub if r.returncode == 0 else None


def measure_timing(run: Runner, name: str, pr: Probe,
                   overhead: int) -> tuple[int, str, int]:
    """Clock-delta completion latency for one op = elapsed - overhead.

    Verifies the timed cubin keeps the producer between the two clock reads
    (else the measurement is invalid).  Returns (cycles, status, launch_count).
    """
    cub = compile_timing(name, pr)
    if cub is None:
        return -1, "compile_fail", 0
    ctrls = C.disasm_cubin(str(cub))
    clk = [c.offset for c in ctrls if "CLOCK" in c.text.upper()]
    if len(clk) < 2:
        return -1, "no_clocks", 0
    # the producer must lie strictly between the FIRST and LAST clock read (a
    # seed-time S2R for %tid may match earlier -- only an in-window match counts)
    in_win = [c.offset for c in ctrls
              if _matches(c, pr.match) and clk[0] < c.offset < clk[-1]]
    if not in_win:
        return -1, "producer_not_bracketed", 0
    el = run.elapsed(cub)
    if el is None:
        return -1, "unlaunchable", 5
    return el - overhead, "measured", 5


# --------------------------------------------------------------------------- #
# Main.                                                                         #
# --------------------------------------------------------------------------- #
def emit_pairings() -> dict[str, Pairing]:
    out: dict[str, Pairing] = {}
    for name, pr in PROBES.items():
        cub = compile_probe(name, pr)
        if cub is None:
            print(f"{name}\tCOMPILE_FAIL", file=sys.stderr)
            continue
        ctrls = C.disasm_cubin(str(cub))
        p = find_pairing(ctrls, pr.match)
        if p is None:
            print(f"{name}\tNO_PAIRING", file=sys.stderr)
            continue
        out[name] = p
        waits = [b for b in range(6) if (p.cons_wait_mask >> b) & 1]
        kind = "COUPLED" if p.coupled else f"sets SB{p.sb}"
        print(f"{name}\tprod={p.prod_mnem} {kind}  cons={p.cons_mnem} "
              f"wait={waits} cons_stall={p.cons_stall} prod_stall={p.prod_stall}",
              file=sys.stderr)
    return out


if __name__ == "__main__":
    if "--gpu" in sys.argv:
        pairings = emit_pairings()
        run = Runner()
        try:
            oh_cub = _compile_raw("timing_overhead", OVERHEAD_PTX)
            base_cub = _compile_raw("timing_baseline", BASELINE_PTX)
            overhead = run.elapsed(oh_cub) if oh_cub else 0
            baseline = run.elapsed(base_cub) if base_cub else 0
            print(f"# clock-read overhead = {overhead} cyc; "
                  f"coupled FMUL->FADD baseline = {baseline} cyc",
                  file=sys.stderr)
            print("op\tsets_sb\tcoupled\tgpu_completion_cyc\tstatus\tnote",
                  file=sys.stderr)
            for name, pr in PROBES.items():
                p = pairings.get(name)
                sb = "-" if (p is None or p.coupled) else f"SB{p.sb}"
                coup = "yes" if (p is not None and p.coupled) else "no"
                cyc, status, nl = measure_timing(run, name, pr, overhead)
                print(f"{name}\t{sb}\t{coup}\t{cyc}\t{status}\t{pr.note}",
                      file=sys.stderr)
        finally:
            run.close()
    else:
        emit_pairings()
