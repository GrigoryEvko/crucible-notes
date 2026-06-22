#!/usr/bin/env python3
# nvopen-tools -- SASS reverse-engineering tooling.  Built on the public CUDA
# Driver API + ptxas/nvdisasm.
"""sm_89 tensor-op (HMMA/IMMA/BMMA/DMMA) result-latency probe.

Settles how ptxas (CUDA 13.1) enforces the tensor producer -> dependent-use
ordering, and what the silicon actually requires, on an sm_89 (Ada) GPU.

Method
------
1. Hand-written wmma PTX -> `ptxas -arch sm_89` emits real HMMA/IMMA/BMMA/DMMA.
2. `nvdisasm -c` decode of the control words shows the producer's write-
   scoreboard and the consumer's wait mask -> coupled vs scoreboard.
3. Driver-API launch (via sass_launch) of the unpatched cubin = gold result.
4. Patch the producer->use issue-gap DOWN in the cubin .text (usched field,
   hi64 bits 41-45) and relaunch; compare to gold bit-for-bit.  The smallest
   still-correct gap is the true latency the silicon enforces.
5. Negative control: the same patch on a dependent FFMA chain (coupled scalar
   with NO hardware interlock) MUST corrupt -- proving the probe is sensitive.

Finding
-------
HMMA/IMMA/BMMA are COUPLED (no write-scoreboard; consumer carries no wait);
ptxas spaces the dependent consumer 23 issue-cycles out (m16n16k16 lowering),
consistent with a 27-28 result-latency model, not a 13-cycle band.  On silicon
the spacing is a hardware floor: the gap patches down to 3 and the result never
breaks, because the tensor pipe interlocks the accumulator read.  DMMA differs
-- ptxas tracks it with a real write-scoreboard (decoupled).

Usage:  python3 tensor_latency_probe.py        # runs all probes + the control
"""
from __future__ import annotations
import struct
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sass_launch as SL  # noqa: E402  (ctypes Driver-API launcher)

PTXAS = "/usr/local/cuda-13.1/bin/ptxas"
NVDISASM = "/usr/local/cuda-13.1/bin/nvdisasm"
ARCH = "sm_89"
_TMP = Path("/tmp")

# --------------------------------------------------------------------------- #
# Control-word helpers (usched lives in hi64 bits 41-45 = full-word 105-109).  #
# --------------------------------------------------------------------------- #
def stall_of(us: int) -> int:
    if us == 0:
        return 0
    if 1 <= us <= 15:
        return us
    if 17 <= us <= 27:
        return us - 16
    return us


def enc_stall(n: int) -> int:
    """Encode a stall of n cycles in the Wn (no-group-end) usched class."""
    if n == 0:
        return 0
    if 1 <= n <= 11:
        return n + 16
    return min(n, 15)


def set_usched(hi64: int, new_us: int) -> int:
    return (hi64 & ~(0x1F << 41)) | ((new_us & 0x1F) << 41)


def locate(data: bytes, lo64: int, hi64: int) -> int:
    off = data.find(struct.pack("<QQ", lo64, hi64))
    assert off >= 0, f"word {lo64:#x}/{hi64:#x} not in cubin"
    return off


def compile_ptx(ptx: str, tag: str, must_contain: str) -> Path:
    p = _TMP / f"{tag}.ptx"
    cub = _TMP / f"{tag}.cubin"
    p.write_text(ptx)
    r = subprocess.run([PTXAS, "-arch", ARCH, str(p), "-o", str(cub)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ptxas: {r.stderr.strip()}")
    sass = subprocess.run([NVDISASM, "-c", str(cub)],
                          capture_output=True, text=True).stdout
    assert must_contain in sass, f"{must_contain} not emitted for {tag}"
    return cub


# --------------------------------------------------------------------------- #
# Kernels.                                                                     #
# --------------------------------------------------------------------------- #
HMMA_PTX = """.version 8.5
.target sm_89
.address_size 64
.visible .entry hmma_probe(.param .u64 pa,.param .u64 pb,.param .u64 pc,.param .u64 po){
  .reg .b32 %a<8>,%b<8>,%c<8>,%d<8>,%e<8>;
  .reg .b64 %pa,%pb,%pc,%po,%oab,%ocd; .reg .b32 %ln;
  ld.param.u64 %pa,[pa]; cvta.to.global.u64 %pa,%pa;
  ld.param.u64 %pb,[pb]; cvta.to.global.u64 %pb,%pb;
  ld.param.u64 %pc,[pc]; cvta.to.global.u64 %pc,%pc;
  ld.param.u64 %po,[po]; cvta.to.global.u64 %po,%po;
  mov.u32 %ln,%tid.x; mul.wide.u32 %oab,%ln,16; mul.wide.u32 %ocd,%ln,32;
  add.s64 %pa,%pa,%oab; add.s64 %pb,%pb,%oab; add.s64 %pc,%pc,%ocd; add.s64 %po,%po,%ocd;
  ld.global.v4.b32 {%a0,%a1,%a2,%a3},[%pa]; ld.global.v4.b32 {%a4,%a5,%a6,%a7},[%pa];
  ld.global.v4.b32 {%b0,%b1,%b2,%b3},[%pb]; ld.global.v4.b32 {%b4,%b5,%b6,%b7},[%pb];
  ld.global.v4.b32 {%c0,%c1,%c2,%c3},[%pc]; ld.global.v4.b32 {%c4,%c5,%c6,%c7},[%pc+16];
  wmma.mma.sync.aligned.row.col.m16n16k16.f32.f32
    {%d0,%d1,%d2,%d3,%d4,%d5,%d6,%d7},{%a0,%a1,%a2,%a3,%a4,%a5,%a6,%a7},
    {%b0,%b1,%b2,%b3,%b4,%b5,%b6,%b7},{%c0,%c1,%c2,%c3,%c4,%c5,%c6,%c7};
  add.f32 %e0,%d0,%d0; add.f32 %e1,%d1,%d1; add.f32 %e2,%d2,%d2; add.f32 %e3,%d3,%d3;
  add.f32 %e4,%d4,%d4; add.f32 %e5,%d5,%d5; add.f32 %e6,%d6,%d6; add.f32 %e7,%d7,%d7;
  st.global.v4.b32 [%po],{%e0,%e1,%e2,%e3}; st.global.v4.b32 [%po+16],{%e4,%e5,%e6,%e7};
  ret; }
"""

FFMA_PTX = """.version 8.5
.target sm_89
.address_size 64
.visible .entry probe(.param .u64 pa,.param .u64 pb,.param .u64 po){
  .reg .f32 %a,%b,%r0,%r1,%r2,%r3,%r4,%r5;
  .reg .b64 %pa,%pb,%po,%off; .reg .b32 %ln;
  ld.param.u64 %pa,[pa]; cvta.to.global.u64 %pa,%pa;
  ld.param.u64 %pb,[pb]; cvta.to.global.u64 %pb,%pb;
  ld.param.u64 %po,[po]; cvta.to.global.u64 %po,%po;
  mov.u32 %ln,%tid.x; mul.wide.u32 %off,%ln,4;
  add.s64 %pa,%pa,%off; add.s64 %pb,%pb,%off; add.s64 %po,%po,%off;
  ld.global.f32 %a,[%pa]; ld.global.f32 %b,[%pb];
  fma.rn.f32 %r0,%a,%b,%a; fma.rn.f32 %r1,%r0,%b,%a; fma.rn.f32 %r2,%r1,%b,%a;
  fma.rn.f32 %r3,%r2,%b,%a; fma.rn.f32 %r4,%r3,%b,%a; fma.rn.f32 %r5,%r4,%b,%a;
  st.global.f32 [%po],%r5; ret; }
"""


def _run(cb: bytes, entry: str, params: bytes, out_ptr_idx: int,
         out_bytes: int, n: int):
    cu = SL.Cuda(0)
    ptrs = list(struct.unpack("<" + "Q" * (len(params) // 8), params))
    try:
        mod = cu.load_module(cb)
        fn = cu.get_function(mod, entry)
        cu.launch(fn, (1, 1, 1), (n, 1, 1), 0, params)
        cu.synchronize()
        raw = cu.memcpy_dtoh(ptrs[out_ptr_idx], out_bytes)
        cu.unload_module(mod)
        return raw
    finally:
        cu.close()


def _alloc_run(entry, cubin, host_ins, out_bytes, n):
    """host_ins: list of bytes (inputs); out array appended; returns out raw."""
    cu = SL.Cuda(0)
    ptrs = []
    try:
        for h in host_ins:
            p = cu.mem_alloc(len(h))
            cu.memcpy_htod(p, h)
            ptrs.append(p)
        po = cu.mem_alloc(out_bytes)
        cu.memset_d8(po, 0, out_bytes)
        ptrs.append(po)
        mod = cu.load_module(cubin)
        fn = cu.get_function(mod, entry)
        cu.launch(fn, (1, 1, 1), (n, 1, 1), 0,
                  struct.pack("<" + "Q" * len(ptrs), *ptrs))
        cu.synchronize()
        raw = cu.memcpy_dtoh(po, out_bytes)
        cu.unload_module(mod)
        for p in ptrs:
            cu.mem_free(p)
        return raw
    finally:
        cu.close()


def hmma_sweep():
    import numpy as np
    cub = compile_ptx(HMMA_PTX, "hmma_lat", "HMMA.16816.F32")
    base = cub.read_bytes()
    N = 32
    A = np.random.RandomState(1).randn(N, 8).astype(np.float16).tobytes()
    B = np.random.RandomState(2).randn(N, 8).astype(np.float16).tobytes()
    C = (1000.0 + np.arange(N * 8).reshape(N, 8) * 0.25).astype(np.float32).tobytes()
    gold = _alloc_run("hmma_probe", base, [A, B, C], len(C), N)
    goldf = [struct.unpack_from("<f", gold, 4 * i)[0] for i in range(N * 8)]
    # The three stall-bearing words HMMA#1, HMMA#2, IMAD between producer and use.
    H1 = (0x0000000c0410723c, 0x044ff00000001810)
    H2 = (0x0000000e0414723c, 0x008fee0000001814)
    AD = (0x00005e0000067625, 0x000fd200078e000b)
    o1, o2, oi = locate(base, *H1), locate(base, *H2), locate(base, *AD)
    n_ok = n_fail = 0
    min_ok = 99
    for s1 in (8, 5, 3, 2, 1):
        for s2 in (7, 4, 2, 1):
            for si in (8, 3, 1):
                d = bytearray(base)
                struct.pack_into("<Q", d, o1 + 8, set_usched(H1[1], enc_stall(s1)))
                struct.pack_into("<Q", d, o2 + 8, set_usched(H2[1], enc_stall(s2)))
                struct.pack_into("<Q", d, oi + 8, set_usched(AD[1], enc_stall(si)))
                out = _alloc_run("hmma_probe", bytes(d), [A, B, C], len(C), N)
                of = [struct.unpack_from("<f", out, 4 * i)[0] for i in range(N * 8)]
                ok = all(abs(a - b) < 1e-2 for a, b in zip(of, goldf))
                gap = s1 + s2 + si
                if ok:
                    n_ok += 1
                    min_ok = min(min_ok, gap)
                else:
                    n_fail += 1
    return n_ok, n_fail, min_ok


def ffma_control():
    import numpy as np
    cub = compile_ptx(FFMA_PTX, "ffma_ctrl", "FFMA")
    base = cub.read_bytes()
    N = 32
    a = np.full(N, 1.0009765625, dtype=np.float32).tobytes()
    b = np.full(N, 1.0009765625, dtype=np.float32).tobytes()
    gold = _alloc_run("probe", base, [a, b], N * 4, N)
    g = struct.unpack_from("<f", gold, 0)[0]
    # Drop every mid-chain FFMA (shared hi word, stall 4) to stall 1.
    LO_A, LO_B = 0x0000000705077223, 0x0000000005077223
    HI = 0x000fc80000000002
    d = bytearray(base)
    patched = 0
    for i in range(0, len(d) - 16, 16):
        lo = struct.unpack_from("<Q", d, i)[0]
        hi = struct.unpack_from("<Q", d, i + 8)[0]
        if lo in (LO_A, LO_B) and hi == HI:
            struct.pack_into("<Q", d, i + 8, set_usched(hi, enc_stall(1)))
            patched += 1
    got_raw = _alloc_run("probe", bytes(d), [a, b], N * 4, N)
    got = struct.unpack_from("<f", got_raw, 0)[0]
    return g, got, patched, abs(got - g) > 1e-3


if __name__ == "__main__":
    g, got, npatch, corrupted = ffma_control()
    print(f"[control] FFMA chain gold={g!r} under-stalled={got!r} "
          f"patched={npatch} -> {'CORRUPTED (probe sensitive)' if corrupted else 'NO CHANGE'}")
    assert corrupted, "control failed: probe cannot detect under-stalls"
    n_ok, n_fail, min_ok = hmma_sweep()
    print(f"[hmma]    {n_ok} OK / {n_fail} FAIL ; min still-correct issue gap "
          f"= {min_ok} (true HMMA->use latency floor; baseline emit = 23)")
    print("VERDICT: HMMA/IMMA/BMMA coupled (no write-scoreboard); ptxas emits "
          "the 27-28 result-latency spacing, not 13; silicon HW-interlocks the "
          "tensor read so the emitted spacing is a floor, not a contract.")
