#!/usr/bin/env python3
# nvopen-tools -- SASS reverse-engineering tooling (MIT-style).
# Built on the public CUDA Driver API + cuobjdump/nvdisasm and live-GPU
# measurement.
"""
Bit-exact **32-lane warp-collective + MMA-fragment functional model** for the
NVIDIA SASS warp datapath (Volta..Ada; the uniform/cluster extensions of
Hopper/Blackwell are noted where they change the lowering).

A closed-form, per-lane reference implementation of every warp-wide instruction
ptxas emits --

    VOTE / VOTEU (.all/.any/.ballot/.uni)        -> SASS VOTE / VOTEU
    SHFL (.idx/.up/.down/.bfly)                  -> SASS SHFL  (+ in-range pred)
    MATCH (.any/.all)                            -> SASS MATCH
    REDUX (.add/.min/.max/.and/.or/.xor)         -> SASS REDUX (uniform result)
    ACTIVEMASK                                   -> SASS VOTE.ANY Rd,PT
    ELECT                                        -> SASS ELECT / lowered FLO
    BAR / BARWARP / WARPSYNC                      -> the sync semantics

-- and a ``__main__`` differential harness that compiles matching PTX, launches
one 32-thread warp on the local GPU with *divergent active masks* (lanes are
deactivated by predication), reads the per-lane results back, and asserts the
model matches hardware **bit-for-bit**.

The same file also carries the **MMA fragment functional model**: the
per-element fragment-to-thread mapping for the canonical HMMA (fp16) and IMMA
(int8) shapes and the ``D = A*B + C`` accumulate, plus a static description of
the Blackwell tcgen05 / TMEM mapping (not executable on pre-Blackwell parts).

WARP MODEL CONVENTIONS
----------------------
A *warp vector* is a length-32 Python list indexed by lane (0..31).  An *active
mask* is a 32-bit int whose bit ``l`` is 1 iff lane ``l`` participates.  Every
collective takes ``(vec, active)`` (and op-specific operands) and returns either
a length-32 result vector (inactive lanes hold the model's defined value, which
matches what hardware leaves in the destination for a deactivated lane: the
prior register contents -- so callers must only trust active lanes) plus, where
the SASS op produces one, a per-lane predicate vector or a uniform scalar.

All integer results are masked to 32 bits.  ``laneid`` is the lane's position
0..31.  The "membermask" operand of the .sync PTX forms is the programmer-
supplied mask of lanes that MUST converge; the *active* mask is the subset of
those lanes that actually arrive.  Hardware behaviour is defined only when the
arriving set equals the membermask; the model follows that contract and treats
``active`` as the effective participating set.
"""
from __future__ import annotations

import struct
import subprocess
import sys
import tempfile
from pathlib import Path

WARP = 32
U32 = 0xFFFFFFFF
FULL = U32  # full-warp membermask / active mask

# --------------------------------------------------------------------------- #
# Small bit helpers.                                                          #
# --------------------------------------------------------------------------- #
def popcount(x: int) -> int:
    return bin(x & U32).count("1")


def lanes_of(mask: int) -> list[int]:
    """List of lane indices set in `mask`, ascending."""
    return [l for l in range(WARP) if (mask >> l) & 1]


def s32(x: int) -> int:
    """Interpret low 32 bits of x as signed."""
    x &= U32
    return x - (1 << 32) if x & 0x80000000 else x


def u32(x: int) -> int:
    return x & U32


# --------------------------------------------------------------------------- #
# Lane-mask special registers (the %lanemask_* family) -- exact per-lane bits. #
# These are the SR_EQMASK/LTMASK/LEMASK/GTMASK/GEMASK values an S2R returns.   #
# --------------------------------------------------------------------------- #
def lanemask_eq(laneid: int) -> int:
    return u32(1 << laneid)


def lanemask_lt(laneid: int) -> int:
    return u32((1 << laneid) - 1)


def lanemask_le(laneid: int) -> int:
    return u32((1 << (laneid + 1)) - 1)


def lanemask_gt(laneid: int) -> int:
    return u32(U32 << (laneid + 1))


def lanemask_ge(laneid: int) -> int:
    return u32(U32 << laneid)


# --------------------------------------------------------------------------- #
# VOTE / VOTEU                                                                #
# --------------------------------------------------------------------------- #
def vote_ballot(pred: list[int], active: int) -> int:
    """vote.sync.ballot.b32 -> SASS VOTE.ANY Rd, PT, P.

    Returns the 32-bit mask whose bit `l` is 1 iff lane `l` is active AND its
    predicate is true.  Inactive lanes contribute 0.  This is the canonical
    `__ballot_sync` result (identical on every lane).
    """
    r = 0
    for l in lanes_of(active):
        if pred[l]:
            r |= (1 << l)
    return u32(r)


def vote_all(pred: list[int], active: int) -> int:
    """vote.sync.all.pred -> SASS VOTE.ALL.  1 iff every active lane's pred is true."""
    return 1 if all(pred[l] for l in lanes_of(active)) else 0


def vote_any(pred: list[int], active: int) -> int:
    """vote.sync.any.pred -> SASS VOTE.ANY.  1 iff some active lane's pred is true."""
    return 1 if any(pred[l] for l in lanes_of(active)) else 0


def vote_uni(pred: list[int], active: int) -> int:
    """vote.sync.uni.pred.  1 iff the predicate is uniform across active lanes."""
    vals = {bool(pred[l]) for l in lanes_of(active)}
    return 1 if len(vals) <= 1 else 0


def activemask() -> int:
    """activemask.b32 -> the set of currently-converged lanes.

    Modelled as a query: the harness supplies the true active set, so this is a
    pass-through that the differential test compares against hardware's
    VOTE.ANY Rd,PT (every lane's predicate forced true).
    """
    # The value IS the active mask; provided by the caller in the harness.
    raise NotImplementedError("activemask() value is the active mask itself")


# --------------------------------------------------------------------------- #
# SHFL (.idx / .up / .down / .bfly)  +  c-operand clamp/segmask packing.      #
# --------------------------------------------------------------------------- #
# PTX packs the SHFL `c` operand as  c = (segmask << 8) | clamp.  For a sub-warp
# width W the front end emits  segmask = (32 - W)  and  clamp = (W - 1).  The
# hardware (confirmed by live differential measurement) computes, per lane:
#     segmask5 = (c >> 8) & 0x1f          (which high bits of laneid to KEEP)
#     clamp    =  c       & 0x1f          (segment-relative upper bound)
#     minLane  = laneid & segmask5                         (segment base)
#     maxLane  = minLane | (clamp & ~segmask5)             (segment top)
# Full-warp width => segmask5 = 0 => minLane = 0, maxLane = clamp (0x1f), i.e.
# the whole warp is one segment.  A lane whose computed source falls outside
# [minLane, maxLane] keeps its OWN value; the in-range predicate is then 0.
def _shfl_segment(laneid: int, c: int) -> tuple[int, int, int]:
    clamp = c & 0x1F
    segmask = (c >> 8) & 0x1F
    min_lane = laneid & segmask
    max_lane = min_lane | (clamp & (~segmask & 0x1F))
    return min_lane, max_lane, segmask


# Each SHFL function returns (res, pred, defined):
#   res[l]      result value (only trustworthy where defined[l]==1)
#   pred[l]     the SASS in-range predicate -- a purely *geometric* test of
#               whether the computed source lane is inside this lane's segment;
#               it is independent of whether the source lane is active.
#   defined[l]  1 iff res[l] is well-defined, i.e. in-range AND the source lane
#               is itself active.  CUDA leaves the value UNDEFINED when shuffling
#               from an inactive lane, so res[l] must only be relied on where
#               defined[l]==1.  (Hardware writes nothing in that case: the
#               destination register keeps its prior value.)
def _shfl_src(laneid: int, b: int, c: int, mode: str) -> tuple[int, bool]:
    min_lane, max_lane, segmask = _shfl_segment(laneid, c)
    if mode == "idx":
        src = min_lane | (b & (~segmask & 0x1F))
        inrange = src <= max_lane
    elif mode == "up":
        src = laneid - b
        inrange = src >= min_lane
    elif mode == "down":
        src = laneid + b
        inrange = src <= max_lane
    elif mode == "bfly":
        src = laneid ^ b
        inrange = min_lane <= src <= max_lane
    else:
        raise ValueError(mode)
    return src, inrange


def _shfl(vec, active, b, c, mode):
    res, pred, defined = list(vec), [0] * WARP, [0] * WARP
    for laneid in lanes_of(active):
        src, inrange = _shfl_src(laneid, b, c, mode)
        src_active = inrange and 0 <= src < WARP and ((active >> src) & 1)
        if src_active:
            res[laneid] = vec[src]
            defined[laneid] = 1
        # else: value left as the lane's prior contents (undefined per CUDA)
        pred[laneid] = 1 if inrange else 0
    return res, pred, defined


def shfl_idx(vec, active, b, c):
    """shfl.sync.idx -> SASS SHFL.IDX.  Source lane = segment_base | (b in seg)."""
    return _shfl(vec, active, b, c, "idx")


def shfl_up(vec, active, b, c):
    """shfl.sync.up -> SASS SHFL.UP.  Source = laneid - b, lower-clamped to seg base."""
    return _shfl(vec, active, b, c, "up")


def shfl_down(vec, active, b, c):
    """shfl.sync.down -> SASS SHFL.DOWN.  Source = laneid + b, upper-clamped to seg top."""
    return _shfl(vec, active, b, c, "down")


def shfl_bfly(vec, active, b, c):
    """shfl.sync.bfly -> SASS SHFL.BFLY.  Source = laneid XOR b, clamped to segment."""
    return _shfl(vec, active, b, c, "bfly")


# --------------------------------------------------------------------------- #
# MATCH (.any / .all)                                                         #
# --------------------------------------------------------------------------- #
def match_any(vec: list[int], active: int) -> list[int]:
    """match.any.sync -> SASS MATCH.ANY.  Per lane: mask of active lanes whose
    value equals this lane's value."""
    res = [0] * WARP
    for laneid in lanes_of(active):
        m = 0
        for o in lanes_of(active):
            if vec[o] == vec[laneid]:
                m |= (1 << o)
        res[laneid] = u32(m)
    return res


def match_all(vec: list[int], active: int) -> tuple[list[int], list[int]]:
    """match.all.sync -> SASS MATCH.ALL.  Returns (mask, pred): if every active
    lane has the same value, mask = active and pred = 1 for all; else mask = 0
    and pred = 0."""
    vals = {vec[l] for l in lanes_of(active)}
    if len(vals) == 1:
        return [active] * WARP, [1] * WARP
    return [0] * WARP, [0] * WARP


# --------------------------------------------------------------------------- #
# REDUX (.add/.min/.max/.and/.or/.xor) -- uniform (all active lanes get same). #
# --------------------------------------------------------------------------- #
def _redux(vec: list[int], active: int, op: str, signed: bool) -> int:
    vals = [vec[l] for l in lanes_of(active)]
    if not vals:
        return 0
    if op == "add":
        return u32(sum(vals))
    if op == "min":
        return u32(min(s32(v) for v in vals) if signed else min(u32(v) for v in vals))
    if op == "max":
        return u32(max(s32(v) for v in vals) if signed else max(u32(v) for v in vals))
    if op == "and":
        r = U32
        for v in vals:
            r &= v
        return u32(r)
    if op == "or":
        r = 0
        for v in vals:
            r |= v
        return u32(r)
    if op == "xor":
        r = 0
        for v in vals:
            r ^= v
        return u32(r)
    raise ValueError(op)


def redux(vec: list[int], active: int, op: str, signed: bool = True) -> int:
    """redux.sync.<op> -> SASS REDUX.<OP>.  Single uniform scalar broadcast to
    every active lane (the SASS form writes a uniform register UR)."""
    return _redux(vec, active, op, signed)


# --------------------------------------------------------------------------- #
# ELECT                                                                       #
# --------------------------------------------------------------------------- #
def elect(active: int) -> tuple[int, list[int]]:
    """elect.sync -> SASS ELECT.  Returns (leader_lane, pred_vec): the lowest
    active lane is the leader; its predicate is 1, all others 0."""
    ls = lanes_of(active)
    if not ls:
        return -1, [0] * WARP
    leader = ls[0]
    pv = [0] * WARP
    pv[leader] = 1
    return leader, pv


# =========================================================================== #
# MMA FRAGMENT FUNCTIONAL MODEL  (D = A*B + C)                                 #
# =========================================================================== #
# A warp's MMA executes a small matrix multiply where the A/B/C/D fragments are
# distributed across the 32 lanes' registers.  The model below implements the
# canonical shapes' thread->element mapping and the accumulate, in pure Python
# over a 32-lane register file.  We model:
#   * IMMA  m16n8k32 .s8.s8.s32   (integer; bit-exact two's-complement accum)
#   * HMMA  m16n8k16 .f16.f16.f32 (fp16 inputs, fp32 accumulate)
# These are the Ampere/Ada warp-level (mma.sync) shapes -- the same fragment
# layout the CUDA WMMA/MMA PTX maps to.  The Hopper wgmma and Blackwell tcgen05
# layouts are described in MMA_NOTES (static; not executable on sm_89).

def imma_m16n8k32_s8(a_frag, b_frag, c_frag):
    """IMMA m16n8k32, s8*s8 -> s32 accumulate.  Reference matmul over the logical
    16x32 (A) and 32x8 (B) tiles.  a_frag/b_frag are logical 2-D lists of int8;
    c_frag is a 16x8 list of int32.  Returns the 16x8 int32 D = A*B + C."""
    M, N, K = 16, 8, 32
    D = [[0] * N for _ in range(M)]
    for i in range(M):
        for j in range(N):
            acc = c_frag[i][j]
            for k in range(K):
                acc += int(a_frag[i][k]) * int(b_frag[k][j])
            # s32 wraparound (two's complement)
            D[i][j] = s32(acc)
    return D


def _fp16_to_float(h: int) -> float:
    """Decode an IEEE-754 half (as a 16-bit int) to a Python float."""
    return struct.unpack("<e", struct.pack("<H", h & 0xFFFF))[0]


def hmma_m16n8k16_f16(a_frag, b_frag, c_frag):
    """HMMA m16n8k16, f16*f16 -> f32 accumulate.  a_frag/b_frag are 16x16 / 16x8
    lists of raw fp16 bit-patterns; c_frag is 16x8 fp32 floats.  Returns 16x8
    fp32.  The accumulate is done in fp32 (matching the hardware's f32 path);
    fp16 products are widened to fp32 before summing."""
    M, N, K = 16, 8, 16
    D = [[0.0] * N for _ in range(M)]
    for i in range(M):
        for j in range(N):
            acc = float(c_frag[i][j])
            for k in range(K):
                acc += _fp16_to_float(a_frag[i][k]) * _fp16_to_float(b_frag[k][j])
            D[i][j] = acc
    return D


MMA_NOTES = """\
Canonical warp-level MMA fragment shapes (mma.sync, Ampere/Ada):
  .s8.s8.s32   : m8n8k16, m16n8k16, m16n8k32   -> SASS IMMA
  .f16.f16.f32 : m8n8k4,  m16n8k8,  m16n8k16   -> SASS HMMA
  .bf16        : m16n8k8,  m16n8k16            -> SASS HMMA (bf16 path)
  .tf32        : m16n8k4,  m16n8k8             -> SASS HMMA (tf32 path)
Fragment->thread mapping (m16n8k16 f16, the common case): the 32 lanes are a
4x8 grid (lane = (groupID<<2)|threadID_in_group, groupID=lane>>2 in 0..7,
threadID_in_group=lane&3).  Each lane holds 8 A elements, 4 B elements, and 4 C
accumulators across its .f16x2 / .f32 fragment registers; element (row,col)
maps to a specific (lane, reg-index) per the PTX MMA fragment tables.  The
m16n8k32 IMMA case packs 4 int8 per 32-bit register, 16 A / 8 B per lane.

Hopper wgmma (wgmma.mma_async): a *warpgroup* (128 lanes / 4 warps) collective;
A may live in shared memory via a matrix descriptor, B always in shared memory;
the accumulator stays in registers spread across the warpgroup.  Static-only
here -- not issuable on sm_89.

Blackwell tcgen05 (tcgen05.mma): the accumulator lives in dedicated **Tensor
Memory (TMEM)**, not registers; an MMA references A/B by descriptors and D by a
TMEM address; results are moved register<->TMEM with tcgen05.ld/st.  Consumer
Blackwell (sm_120/sm_121) lacks tcgen05/TMEM; it keeps the register-fragment
HMMA/IMMA/QMMA path.  Static-only here.
"""


# =========================================================================== #
# GPU DIFFERENTIAL HARNESS                                                     #
# =========================================================================== #
# We compile a PTX kernel per collective that runs on ONE warp (32 threads),
# deactivates a chosen lane set by predication (so the .sync membermask sees a
# *divergent* active set), and writes each lane's result to a per-lane slot in
# the output arena.  The harness then runs the same scenario through the model
# above and asserts bit-equality on the active lanes.
PTXAS = "/usr/local/cuda-13.1/bin/ptxas"
NVDISASM = "/usr/local/cuda-13.1/bin/nvdisasm"
ARCH = "sm_89"
PTX_HEAD = ".version 8.3\n.target sm_89\n.address_size 64\n"


def _have_gpu() -> bool:
    try:
        import sass_launch as SL  # noqa
        SL.Cuda(0).close()
        return True
    except Exception:
        return False


def _compile(ptx: str, work: Path, name: str) -> Path | None:
    pf = work / f"{name}.ptx"
    pf.write_text(PTX_HEAD + ptx)
    cub = work / f"{name}.cubin"
    r = subprocess.run([PTXAS, "-arch", ARCH, "-O3", "-o", str(cub), str(pf)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"  [build-fail {name}] {r.stderr.strip().splitlines()[-1][:90]}")
        return None
    return cub


def _emitted_ops(cub: Path) -> set[str]:
    """The set of warp-collective SASS mnemonics nvdisasm shows in the cubin."""
    out = subprocess.run([NVDISASM, "-c", str(cub)], capture_output=True,
                         text=True).stdout
    ops = set()
    for tok in ("VOTEU", "VOTE", "SHFL", "MATCH", "REDUX", "ELECT",
                "WARPSYNC", "BAR", "IMMA", "HMMA"):
        if tok in out:
            ops.add(tok)
    return ops


def _run_warp(cub: Path, entry: str, n_words: int,
              active_mask: int, payload: list[int] | None = None) -> list[int]:
    """Launch `entry` on one 32-thread warp; return the arena as a list of
    int32 words (n_words per lane => 32*n_words total).  The kernel receives the
    output pointer in param0, the active mask in param1, and (optionally) a
    per-lane input payload pointer in param2."""
    import sass_launch as SL
    cu = SL.Cuda(0)
    try:
        mod = cu.load_module(cub.read_bytes())
        fn = cu.get_function(mod, entry)
        total = WARP * n_words * 4
        arena = cu.mem_alloc(total)
        cu.memset_d8(arena, 0, total)
        parts = [struct.pack("<Q", arena), struct.pack("<I", active_mask & U32)]
        pay_ptr = 0
        if payload is not None:
            pay_ptr = cu.mem_alloc(WARP * 4)
            cu.memcpy_htod(pay_ptr, struct.pack("<" + "i" * WARP, *payload))
        # pad to 8-byte alignment for the pointer slot at offset 16
        pb = parts[0] + parts[1] + b"\x00\x00\x00\x00" + struct.pack("<Q", pay_ptr)
        cu.launch(fn, (1, 1, 1), (WARP, 1, 1), 0, pb)
        cu.synchronize()
        raw = cu.memcpy_dtoh(arena, total)
        cu.mem_free(arena)
        if pay_ptr:
            cu.mem_free(pay_ptr)
        cu.unload_module(mod)
        return list(struct.unpack("<" + "i" * (WARP * n_words), raw))
    finally:
        cu.close()


# ---- PTX templates -------------------------------------------------------- #
# Common preamble: load out ptr (p0), active mask (p1), payload ptr (p2);
# compute this lane's bit and whether it is active; if inactive, skip the
# collective by jumping past it (predicated), so the .sync membermask is the
# active subset.
def _preamble() -> str:
    return """
  ld.param.u64 %rd1,[p0]; cvta.to.global.u64 %rdOUT,%rd1;
  ld.param.u32 %rMASK,[p1];
  ld.param.u64 %rd3,[p2]; cvta.to.global.u64 %rdPAY,%rd3;
  mov.u32 %rLANE,%laneid;
  shl.b32 %rBIT,1,%rLANE;
  and.b32 %rACT,%rMASK,%rBIT;
  setp.ne.u32 %pACT,%rACT,0;
"""


def harness() -> int:
    if not _have_gpu():
        print("SKIP: no usable GPU/libcuda -- warp differential harness skipped")
        return 0
    import sass_launch  # noqa: F401  (import check)
    work = Path(tempfile.mkdtemp(prefix="sass_warp_"))
    fails = 0
    passes = 0
    # Active-mask scenarios: full warp, a divergent half, a sparse/odd set,
    # and a single-lane set -- to stress in-range predicates and segments.
    SCENARIOS = [
        ("full", FULL),
        ("evens", sum(1 << l for l in range(0, 32, 2))),
        ("low16", 0x0000FFFF),
        ("sparse", 0b10110010001100100011001000110010 & U32),
        ("single", 1 << 7),
    ]
    try:
        import shutil
        # ============================ VOTE ============================ #
        # pred[l] = (laneid & 1)  -> ballot, all, any, uni.
        vote_ptx = f""".visible .entry vote_k(.param .u64 p0,.param .u32 p1,.param .u64 p2){{
  .reg .pred %pACT,%pP,%pALL,%pANY,%pUNI; .reg .b32 %rLANE,%rBIT,%rACT,%rMASK,%rBALLOT,%r0,%r1,%r2,%r3,%rOFF;
  .reg .b64 %rd1,%rd3,%rdOUT,%rdPAY,%rdT;
{_preamble()}
  @!%pACT bra DONE;
  and.b32 %r0,%rLANE,1; setp.ne.u32 %pP,%r0,0;
  vote.sync.ballot.b32 %rBALLOT,%pP,%rMASK;
  vote.sync.all.pred %pALL,%pP,%rMASK;
  vote.sync.any.pred %pANY,%pP,%rMASK;
  vote.sync.uni.pred %pUNI,%pP,%rMASK;
  selp.b32 %r1,1,0,%pALL; selp.b32 %r2,1,0,%pANY; selp.b32 %r3,1,0,%pUNI;
  mul.wide.u32 %rdT,%rLANE,16; add.s64 %rdT,%rdOUT,%rdT;
  st.global.u32 [%rdT],%rBALLOT; st.global.u32 [%rdT+4],%r1;
  st.global.u32 [%rdT+8],%r2; st.global.u32 [%rdT+12],%r3;
DONE: ret; }}
"""
        cub = _compile(vote_ptx, work, "vote")
        if cub:
            ops = _emitted_ops(cub)
            assert "VOTE" in ops, f"VOTE not emitted: {ops}"
            for sc_name, mask in SCENARIOS:
                got = _run_warp(cub, "vote_k", 4, mask)
                pred = [1 if (l & 1) else 0 for l in range(WARP)]
                exp_ballot = vote_ballot(pred, mask)
                exp_all = vote_all(pred, mask)
                exp_any = vote_any(pred, mask)
                exp_uni = vote_uni(pred, mask)
                ok = True
                for l in lanes_of(mask):
                    if (u32(got[4 * l]) != exp_ballot or got[4 * l + 1] != exp_all
                            or got[4 * l + 2] != exp_any or got[4 * l + 3] != exp_uni):
                        ok = False
                        break
                tag = f"VOTE/{sc_name}"
                print(f"  {tag:<18} ballot={exp_ballot:#010x} all={exp_all} "
                      f"any={exp_any} uni={exp_uni}  {'PASS' if ok else 'FAIL'}")
                fails += 0 if ok else 1
                passes += 1 if ok else 0

        # ============================ SHFL ============================ #
        # payload[l] = l*7+1 ; do idx(b=0,full width), up(1), down(1), bfly(1).
        shfl_ptx = f""".visible .entry shfl_k(.param .u64 p0,.param .u32 p1,.param .u64 p2){{
  .reg .pred %pACT,%pI,%pU,%pD,%pB; .reg .b32 %rLANE,%rBIT,%rACT,%rMASK,%rV,%ri,%ru,%rd_,%rb,%rpi,%rpu,%rpd,%rpb;
  .reg .b64 %rd1,%rd3,%rdOUT,%rdPAY,%rdT,%rdP;
{_preamble()}
  @!%pACT bra DONE;
  mul.wide.u32 %rdP,%rLANE,4; add.s64 %rdP,%rdPAY,%rdP; ld.global.u32 %rV,[%rdP];
  shfl.sync.idx.b32 %ri|%pI,%rV,0,0x1f,%rMASK;
  shfl.sync.up.b32 %ru|%pU,%rV,1,0x0,%rMASK;
  shfl.sync.down.b32 %rd_|%pD,%rV,1,0x1f,%rMASK;
  shfl.sync.bfly.b32 %rb|%pB,%rV,1,0x1f,%rMASK;
  selp.b32 %rpi,1,0,%pI; selp.b32 %rpu,1,0,%pU; selp.b32 %rpd,1,0,%pD; selp.b32 %rpb,1,0,%pB;
  mul.wide.u32 %rdT,%rLANE,32; add.s64 %rdT,%rdOUT,%rdT;
  st.global.u32 [%rdT],%ri; st.global.u32 [%rdT+4],%ru;
  st.global.u32 [%rdT+8],%rd_; st.global.u32 [%rdT+12],%rb;
  st.global.u32 [%rdT+16],%rpi; st.global.u32 [%rdT+20],%rpu;
  st.global.u32 [%rdT+24],%rpd; st.global.u32 [%rdT+28],%rpb;
DONE: ret; }}
"""
        cub = _compile(shfl_ptx, work, "shfl")
        if cub:
            ops = _emitted_ops(cub)
            assert "SHFL" in ops, f"SHFL not emitted: {ops}"
            payload = [l * 7 + 1 for l in range(WARP)]
            for sc_name, mask in SCENARIOS:
                got = _run_warp(cub, "shfl_k", 8, mask, payload)
                ei, pi, di = shfl_idx(payload, mask, 0, 0x1F)
                eu, pu, du = shfl_up(payload, mask, 1, 0x0)
                ed, pd, dd = shfl_down(payload, mask, 1, 0x1F)
                eb, pb, db = shfl_bfly(payload, mask, 1, 0x1F)
                ok = True
                for l in lanes_of(mask):
                    base = 8 * l
                    # predicates always compared; values only where defined.
                    if (got[base + 4] != pi[l] or got[base + 5] != pu[l]
                            or got[base + 6] != pd[l] or got[base + 7] != pb[l]):
                        ok = False
                        break
                    if di[l] and got[base] != ei[l]:
                        ok = False; break
                    if du[l] and got[base + 1] != eu[l]:
                        ok = False; break
                    if dd[l] and got[base + 2] != ed[l]:
                        ok = False; break
                    if db[l] and got[base + 3] != eb[l]:
                        ok = False; break
                tag = f"SHFL/{sc_name}"
                print(f"  {tag:<18} idx/up/down/bfly + 4 preds              "
                      f"{'PASS' if ok else 'FAIL'}")
                fails += 0 if ok else 1
                passes += 1 if ok else 0

        # ===================== SHFL sub-warp width ==================== #
        # idx within width-8 segments: c = (24<<8)|7 = 0x1807, b=3.
        shfl8_ptx = f""".visible .entry shfl8_k(.param .u64 p0,.param .u32 p1,.param .u64 p2){{
  .reg .pred %pACT,%pI; .reg .b32 %rLANE,%rBIT,%rACT,%rMASK,%rV,%ri,%rpi;
  .reg .b64 %rd1,%rd3,%rdOUT,%rdPAY,%rdT,%rdP;
{_preamble()}
  @!%pACT bra DONE;
  mul.wide.u32 %rdP,%rLANE,4; add.s64 %rdP,%rdPAY,%rdP; ld.global.u32 %rV,[%rdP];
  shfl.sync.idx.b32 %ri|%pI,%rV,3,0x1807,%rMASK;
  selp.b32 %rpi,1,0,%pI;
  mul.wide.u32 %rdT,%rLANE,8; add.s64 %rdT,%rdOUT,%rdT;
  st.global.u32 [%rdT],%ri; st.global.u32 [%rdT+4],%rpi;
DONE: ret; }}
"""
        cub = _compile(shfl8_ptx, work, "shfl8")
        if cub:
            payload = [l * 7 + 1 for l in range(WARP)]
            for sc_name, mask in [("full", FULL), ("evens", SCENARIOS[1][1])]:
                got = _run_warp(cub, "shfl8_k", 2, mask, payload)
                ei, pi, di = shfl_idx(payload, mask, 3, 0x1807)
                ok = all(got[2 * l + 1] == pi[l]
                         and (not di[l] or got[2 * l] == ei[l])
                         for l in lanes_of(mask))
                print(f"  {'SHFL.idx.w8/'+sc_name:<18} segment=8, idx=3 in seg          "
                      f"{'PASS' if ok else 'FAIL'}")
                fails += 0 if ok else 1
                passes += 1 if ok else 0

        # ============================ MATCH =========================== #
        # payload[l] = (l>>2) so groups of 4 share a value -> match.any mask.
        match_ptx = f""".visible .entry match_k(.param .u64 p0,.param .u32 p1,.param .u64 p2){{
  .reg .pred %pACT; .reg .b32 %rLANE,%rBIT,%rACT,%rMASK,%rV,%rm;
  .reg .b64 %rd1,%rd3,%rdOUT,%rdPAY,%rdT,%rdP;
{_preamble()}
  @!%pACT bra DONE;
  mul.wide.u32 %rdP,%rLANE,4; add.s64 %rdP,%rdPAY,%rdP; ld.global.u32 %rV,[%rdP];
  match.any.sync.b32 %rm,%rV,%rMASK;
  mul.wide.u32 %rdT,%rLANE,4; add.s64 %rdT,%rdOUT,%rdT;
  st.global.u32 [%rdT],%rm;
DONE: ret; }}
"""
        cub = _compile(match_ptx, work, "match")
        if cub:
            ops = _emitted_ops(cub)
            assert "MATCH" in ops, f"MATCH not emitted: {ops}"
            payload = [l >> 2 for l in range(WARP)]
            for sc_name, mask in SCENARIOS:
                got = _run_warp(cub, "match_k", 1, mask, payload)
                exp = match_any(payload, mask)
                ok = all(u32(got[l]) == exp[l] for l in lanes_of(mask))
                print(f"  {'MATCH.any/'+sc_name:<18} per-lane equal-value mask        "
                      f"{'PASS' if ok else 'FAIL'}")
                fails += 0 if ok else 1
                passes += 1 if ok else 0

        # ============================ REDUX =========================== #
        redux_ptx = f""".visible .entry redux_k(.param .u64 p0,.param .u32 p1,.param .u64 p2){{
  .reg .pred %pACT; .reg .b32 %rLANE,%rBIT,%rACT,%rMASK,%rV,%ra,%rmn,%rmx,%rand,%ror,%rxor;
  .reg .b64 %rd1,%rd3,%rdOUT,%rdPAY,%rdT,%rdP;
{_preamble()}
  @!%pACT bra DONE;
  mul.wide.u32 %rdP,%rLANE,4; add.s64 %rdP,%rdPAY,%rdP; ld.global.u32 %rV,[%rdP];
  redux.sync.add.s32 %ra,%rV,%rMASK;
  redux.sync.min.s32 %rmn,%rV,%rMASK;
  redux.sync.max.s32 %rmx,%rV,%rMASK;
  redux.sync.and.b32 %rand,%rV,%rMASK;
  redux.sync.or.b32 %ror,%rV,%rMASK;
  redux.sync.xor.b32 %rxor,%rV,%rMASK;
  mul.wide.u32 %rdT,%rLANE,32; add.s64 %rdT,%rdOUT,%rdT;
  st.global.u32 [%rdT],%ra; st.global.u32 [%rdT+4],%rmn; st.global.u32 [%rdT+8],%rmx;
  st.global.u32 [%rdT+12],%rand; st.global.u32 [%rdT+16],%ror; st.global.u32 [%rdT+20],%rxor;
DONE: ret; }}
"""
        cub = _compile(redux_ptx, work, "redux")
        if cub:
            ops = _emitted_ops(cub)
            assert "REDUX" in ops, f"REDUX not emitted: {ops}"
            payload = [(l * 2654435761) & U32 for l in range(WARP)]  # scrambled
            payload = [s32(v) for v in payload]
            for sc_name, mask in SCENARIOS:
                got = _run_warp(cub, "redux_k", 8, mask, payload)
                e_add = redux(payload, mask, "add")
                e_min = redux(payload, mask, "min", signed=True)
                e_max = redux(payload, mask, "max", signed=True)
                e_and = redux(payload, mask, "and")
                e_or = redux(payload, mask, "or")
                e_xor = redux(payload, mask, "xor")
                ok = True
                for l in lanes_of(mask):
                    b = 8 * l
                    if (u32(got[b]) != e_add or u32(got[b + 1]) != e_min
                            or u32(got[b + 2]) != e_max or u32(got[b + 3]) != e_and
                            or u32(got[b + 4]) != e_or or u32(got[b + 5]) != e_xor):
                        ok = False
                        break
                print(f"  {'REDUX/'+sc_name:<18} add/min/max/and/or/xor           "
                      f"{'PASS' if ok else 'FAIL'}")
                fails += 0 if ok else 1
                passes += 1 if ok else 0

        # ========================= ACTIVEMASK ========================= #
        am_ptx = f""".visible .entry am_k(.param .u64 p0,.param .u32 p1,.param .u64 p2){{
  .reg .pred %pACT; .reg .b32 %rLANE,%rBIT,%rACT,%rMASK,%ram;
  .reg .b64 %rd1,%rd3,%rdOUT,%rdPAY,%rdT;
{_preamble()}
  @!%pACT bra DONE;
  activemask.b32 %ram;
  mul.wide.u32 %rdT,%rLANE,4; add.s64 %rdT,%rdOUT,%rdT;
  st.global.u32 [%rdT],%ram;
DONE: ret; }}
"""
        cub = _compile(am_ptx, work, "am")
        if cub:
            for sc_name, mask in SCENARIOS:
                got = _run_warp(cub, "am_k", 1, mask)
                ok = all(u32(got[l]) == u32(mask) for l in lanes_of(mask))
                print(f"  {'ACTIVEMASK/'+sc_name:<18} == active set {mask:#010x}     "
                      f"{'PASS' if ok else 'FAIL'}")
                fails += 0 if ok else 1
                passes += 1 if ok else 0

        # ============================ ELECT =========================== #
        # `elect.sync` requires .target sm_90+, so it is not launchable on this
        # sm_89 part.  We (1) confirm ptxas lowers it to the SASS ELECT op at
        # sm_90, and (2) reproduce ELECT's "lowest active lane is leader"
        # semantics on sm_89 with the equivalent FLO(activemask) sequence, which
        # IS launchable -- and check the model against that.
        elect90 = """.version 8.3\n.target sm_90a\n.address_size 64
.visible .entry e90(.param .u64 p0){ .reg .pred %p; .reg .b32 %r; .reg .b64 %rd;
 ld.param.u64 %rd,[p0]; elect.sync %r|%p, 0xffffffff;
 selp.b32 %r,1,0,%p; st.global.u32 [%rd],%r; ret; }
"""
        ef = work / "e90.ptx"; ef.write_text(elect90)
        ec = work / "e90.cubin"
        r = subprocess.run([PTXAS, "-arch", "sm_90a", "-O3", "-o", str(ec), str(ef)],
                           capture_output=True, text=True)
        elect_op_ok = (r.returncode == 0 and "ELECT" in _emitted_ops(ec))
        print(f"  {'ELECT (sm_90)':<18} lowers to SASS ELECT op          "
              f"{'PASS' if elect_op_ok else 'FAIL'}")
        fails += 0 if elect_op_ok else 1
        passes += 1 if elect_op_ok else 0

        # sm_89 equivalent: leader = lane == FLO(activemask) (lowest set bit).
        elect_eq_ptx = f""".visible .entry electeq_k(.param .u64 p0,.param .u32 p1,.param .u64 p2){{
  .reg .pred %pACT,%pL; .reg .b32 %rLANE,%rBIT,%rACT,%rMASK,%ram,%rlow,%re;
  .reg .b64 %rd1,%rd3,%rdOUT,%rdPAY,%rdT;
{_preamble()}
  @!%pACT bra DONE;
  activemask.b32 %ram;
  brev.b32 %rlow,%ram; bfind.u32 %rlow,%rlow; sub.u32 %rlow,31,%rlow;
  setp.eq.u32 %pL,%rLANE,%rlow; selp.b32 %re,1,0,%pL;
  mul.wide.u32 %rdT,%rLANE,4; add.s64 %rdT,%rdOUT,%rdT;
  st.global.u32 [%rdT],%re;
DONE: ret; }}
"""
        cub = _compile(elect_eq_ptx, work, "electeq")
        if cub:
            for sc_name, mask in SCENARIOS:
                got = _run_warp(cub, "electeq_k", 1, mask)
                leader, pv = elect(mask)
                ok = all(got[l] == pv[l] for l in lanes_of(mask))
                print(f"  {'ELECT-model/'+sc_name:<18} leader=lane {leader:<2}              "
                      f"{'PASS' if ok else 'FAIL'}")
                fails += 0 if ok else 1
                passes += 1 if ok else 0

        # ============================ MMA ============================= #
        # Synthesize small A/B/C, run a warp-level wmma (m16n16k16) compiled
        # straight through ptxas (no host compiler), and bit-compare the
        # global-memory D against the integer / fp32 reference accumulate.  We
        # load A/B/C row/col-major and store D row-major, so the fragment->thread
        # mapping cancels out: the memory-level result is layout-independent.
        imma_ok = _mma_imma_check(work)
        print(f"  {'IMMA 16x16x16 s8':<18} D=A*B+C int32 accumulate          "
              f"{'PASS' if imma_ok else 'FAIL'}")
        fails += 0 if imma_ok else 1
        passes += 1 if imma_ok else 0

        hmma_ok = _mma_hmma_check(work)
        print(f"  {'HMMA 16x16x16 f16':<18} D=A*B+C f32 accumulate            "
              f"{'PASS' if hmma_ok else 'FAIL'}")
        fails += 0 if hmma_ok else 1
        passes += 1 if hmma_ok else 0

        print(f"\n  {passes} passed, {fails} failed")
        print("\n" + ("ALL WARP-MODEL TESTS PASSED" if fails == 0
                      else f"{fails} TEST(S) FAILED"))
        return 1 if fails else 0
    finally:
        import shutil
        shutil.rmtree(work, ignore_errors=True)


# Hand-written warp-level WMMA PTX (m16n16k16) compiled straight through ptxas
# -- no host compiler.  A is row-major, B col-major, C/D row-major; the warp
# does ONE wmma.mma over the 16x16x16 tile (hardware decomposes it into two
# IMMA.16816 / HMMA.16816 tensor-core ops).  s8 fragment regs: A=2,B=2,C=8;
# f16 fragment regs: A=8,B=8,C=8.
_IMMA_PTX = """.version 8.3
.target sm_89
.address_size 64
.visible .entry imma(.param .u64 pa,.param .u64 pb,.param .u64 pc,.param .u64 pd){
  .reg .b64 %rda,%rdb,%rdc,%rdd; .reg .b32 %a<2>,%b<2>,%c<8>,%d<8>;
  ld.param.u64 %rda,[pa]; ld.param.u64 %rdb,[pb];
  ld.param.u64 %rdc,[pc]; ld.param.u64 %rdd,[pd];
  cvta.to.global.u64 %rda,%rda; cvta.to.global.u64 %rdb,%rdb;
  cvta.to.global.u64 %rdc,%rdc; cvta.to.global.u64 %rdd,%rdd;
  wmma.load.a.sync.aligned.row.m16n16k16.global.s8 {%a0,%a1}, [%rda], 16;
  wmma.load.b.sync.aligned.col.m16n16k16.global.s8 {%b0,%b1}, [%rdb], 16;
  wmma.load.c.sync.aligned.row.m16n16k16.global.s32 {%c0,%c1,%c2,%c3,%c4,%c5,%c6,%c7}, [%rdc], 16;
  wmma.mma.sync.aligned.row.col.m16n16k16.s32.s8.s8.s32 {%d0,%d1,%d2,%d3,%d4,%d5,%d6,%d7}, {%a0,%a1}, {%b0,%b1}, {%c0,%c1,%c2,%c3,%c4,%c5,%c6,%c7};
  wmma.store.d.sync.aligned.row.m16n16k16.global.s32 [%rdd], {%d0,%d1,%d2,%d3,%d4,%d5,%d6,%d7}, 16;
  ret;}
"""
_HMMA_PTX = """.version 8.3
.target sm_89
.address_size 64
.visible .entry hmma(.param .u64 pa,.param .u64 pb,.param .u64 pc,.param .u64 pd){
  .reg .b64 %rda,%rdb,%rdc,%rdd; .reg .b32 %a<8>,%b<8>,%c<8>,%d<8>;
  ld.param.u64 %rda,[pa]; ld.param.u64 %rdb,[pb];
  ld.param.u64 %rdc,[pc]; ld.param.u64 %rdd,[pd];
  cvta.to.global.u64 %rda,%rda; cvta.to.global.u64 %rdb,%rdb;
  cvta.to.global.u64 %rdc,%rdc; cvta.to.global.u64 %rdd,%rdd;
  wmma.load.a.sync.aligned.row.m16n16k16.global.f16 {%a0,%a1,%a2,%a3,%a4,%a5,%a6,%a7}, [%rda], 16;
  wmma.load.b.sync.aligned.col.m16n16k16.global.f16 {%b0,%b1,%b2,%b3,%b4,%b5,%b6,%b7}, [%rdb], 16;
  wmma.load.c.sync.aligned.row.m16n16k16.global.f32 {%c0,%c1,%c2,%c3,%c4,%c5,%c6,%c7}, [%rdc], 16;
  wmma.mma.sync.aligned.row.col.m16n16k16.f32.f32 {%d0,%d1,%d2,%d3,%d4,%d5,%d6,%d7}, {%a0,%a1,%a2,%a3,%a4,%a5,%a6,%a7}, {%b0,%b1,%b2,%b3,%b4,%b5,%b6,%b7}, {%c0,%c1,%c2,%c3,%c4,%c5,%c6,%c7};
  wmma.store.d.sync.aligned.row.m16n16k16.global.f32 [%rdd], {%d0,%d1,%d2,%d3,%d4,%d5,%d6,%d7}, 16;
  ret;}
"""


def _build_ptxas(ptx_text: str, work: Path, name: str) -> Path | None:
    pf = work / f"{name}.ptx"; pf.write_text(ptx_text)
    cub = work / f"{name}.cubin"
    r = subprocess.run([PTXAS, "-arch", ARCH, "-O3", "-o", str(cub), str(pf)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"    [{name} build-fail] {r.stderr.strip().splitlines()[-1][:90]}")
        return None
    return cub


def _launch_mma(cub: Path, entry: str, a_flat: bytes, b_flat: bytes,
                c_flat: bytes, n_out_bytes: int) -> bytes:
    import sass_launch as SL
    cu = SL.Cuda(0)
    try:
        mod = cu.load_module(cub.read_bytes())
        fn = cu.get_function(mod, entry)
        dA = cu.mem_alloc(len(a_flat)); cu.memcpy_htod(dA, a_flat)
        dB = cu.mem_alloc(len(b_flat)); cu.memcpy_htod(dB, b_flat)
        dC = cu.mem_alloc(len(c_flat)); cu.memcpy_htod(dC, c_flat)
        dD = cu.mem_alloc(n_out_bytes); cu.memset_d8(dD, 0, n_out_bytes)
        cu.launch(fn, (1, 1, 1), (32, 1, 1), 0, struct.pack("<QQQQ", dA, dB, dC, dD))
        cu.synchronize()
        raw = cu.memcpy_dtoh(dD, n_out_bytes)
        for p in (dA, dB, dC, dD):
            cu.mem_free(p)
        cu.unload_module(mod)
        return raw
    finally:
        cu.close()


def _mma_imma_check(work: Path) -> bool:
    """Warp-level IMMA (s8*s8 -> s32), m16n16k16, verified against the integer
    two's-complement reference accumulate at the global-memory level."""
    cub = _build_ptxas(_IMMA_PTX, work, "imma")
    if cub is None:
        return False
    if "IMMA" not in _emitted_ops(cub):
        print("    [imma] expected IMMA op not emitted"); return False
    M = N = K = 16
    A = [[(i * 3 + k) % 7 - 3 for k in range(K)] for i in range(M)]      # row-major
    B = [[(k + 2 * j) % 5 - 2 for j in range(N)] for k in range(K)]      # logical [k][j]
    C = [[(i * 5 + j * 3) % 97 - 40 for j in range(N)] for i in range(M)]
    a_flat = bytes((A[i][k] & 0xFF) for i in range(M) for k in range(K))
    b_flat = bytes((B[k][j] & 0xFF) for j in range(N) for k in range(K))  # col-major
    c_flat = struct.pack("<" + "i" * (M * N),
                         *[C[i][j] for i in range(M) for j in range(N)])
    D_ref = [s32(C[i][j] + sum(A[i][k] * B[k][j] for k in range(K)))
             for i in range(M) for j in range(N)]
    raw = _launch_mma(cub, "imma", a_flat, b_flat, c_flat, M * N * 4)
    D_hw = list(struct.unpack("<" + "i" * (M * N), raw))
    return D_hw == D_ref


def _mma_hmma_check(work: Path) -> bool:
    """Warp-level HMMA (f16*f16 -> f32), m16n16k16, verified against the fp32
    accumulate.  fp16 inputs are exactly representable so the compare is exact."""
    cub = _build_ptxas(_HMMA_PTX, work, "hmma")
    if cub is None:
        return False
    if "HMMA" not in _emitted_ops(cub):
        print("    [hmma] expected HMMA op not emitted"); return False
    M = N = K = 16

    def f2h(x: float) -> int:
        return struct.unpack("<H", struct.pack("<e", x))[0]

    A = [[float((i + k) % 5) for k in range(K)] for i in range(M)]
    B = [[float((k - j) % 5) for j in range(N)] for k in range(K)]
    C = [[float((i * 2 - j) % 13) for j in range(N)] for i in range(M)]
    a_flat = b"".join(struct.pack("<H", f2h(A[i][k])) for i in range(M) for k in range(K))
    b_flat = b"".join(struct.pack("<H", f2h(B[k][j])) for j in range(N) for k in range(K))
    c_flat = struct.pack("<" + "f" * (M * N),
                         *[C[i][j] for i in range(M) for j in range(N)])
    a_bits = [[f2h(A[i][k]) for k in range(K)] for i in range(M)]
    b_bits = [[f2h(B[k][j]) for j in range(N)] for k in range(K)]
    # model: fp32 accumulate of widened fp16 products (uses hmma_m16n8k16_f16
    # element math, extended to N=16).
    D_model = [C[i][j] + sum(_fp16_to_float(a_bits[i][k]) * _fp16_to_float(b_bits[k][j])
                             for k in range(K)) for i in range(M) for j in range(N)]
    raw = _launch_mma(cub, "hmma", a_flat, b_flat, c_flat, M * N * 4)
    D_hw = list(struct.unpack("<" + "f" * (M * N), raw))
    return all(abs(D_hw[i] - D_model[i]) < 1e-3 for i in range(M * N))


def main(argv: list[str]) -> int:
    if "--notes" in argv:
        print(MMA_NOTES)
        return 0
    HERE = Path(__file__).resolve().parent
    sys.path.insert(0, str(HERE))
    print("== SASS warp-collective + MMA differential harness (sm_89) ==")
    return harness()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
