#!/usr/bin/env python3
# nvopen-tools -- SASS reverse-engineering tooling.  Reproduces the per-space /
# per-cache-tier memory load-use latencies in memory_latency.tsv on an sm_89 GPU.
"""sm_89 memory load-use latency microbench (clock64 dependent-chase, 1 warp).

Each probe builds a strictly data-dependent chain of memory ops (the address of
op k+1 comes from the value loaded by op k) so the SM cannot overlap them; the
clock64 delta divided by the op count is the real load-use latency for that
memory space / cache tier.  clock64 (SR_CLOCKLO) counts SM cycles regardless of
the boost clock, so the figures are clock-invariant.

Global cache tiers are isolated by sizing the chase working set against the Ada
hierarchy (L1 <= 128 KiB/SM, L2 = 24 MiB on the laptop Ada part):
  2 KiB set  -> stays in L1   (L1-hit tier)
  1 MiB set  -> overflows L1, fits L2  (L2 tier)
  96 MiB set -> overflows L2  (DRAM-miss tier)

The companion table also records the scoreboard mechanism each op uses: a load
arms a write scoreboard (SASS dst_wr_sb) and the dependent consumer blocks on it
via its read-barrier wait mask (req_bit_set); the static usched stall is an
issue-throughput hint, not the latency.  Decode the control words with
../sass-tools/sass_ctrl_decode.py on any cubin this script emits to /tmp.

Usage:  python3 memory_latency_probe.py        # runs every probe, prints a table
Requires a CUDA 13.x toolchain (ptxas/nvdisasm) and an sm_89 device.
"""
from __future__ import annotations

import struct
import subprocess
import sys
from pathlib import Path

# the ctypes Driver-API launcher lives in ../sass-tools
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "sass-tools"))
import sass_launch as SL  # noqa: E402

PTXAS = "/usr/local/cuda-13.1/bin/ptxas"
ARCH = "sm_89"
TMP = Path("/tmp")
REP = 7
NITER = 4096


def compile_ptx(ptx: str, tag: str) -> str:
    """Compile a PTX string to a cubin under /tmp; raise on a ptxas error."""
    src = TMP / f"{tag}.ptx"
    cub = TMP / f"{tag}.cubin"
    src.write_text(ptx)
    r = subprocess.run([PTXAS, "-arch", ARCH, str(src), "-o", str(cub)],
                       capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f"ptxas {tag}: {r.stderr.strip()}")
    return str(cub)


def median(xs: list[float]) -> float:
    xs = sorted(xs)
    m = len(xs)
    return xs[m // 2] if m % 2 else (xs[m // 2 - 1] + xs[m // 2]) / 2


# --------------------------------------------------------------------------- #
# PTX probes (one per space / op).  Every loop is a dependent chain so the     #
# unrolled SASS is a back-to-back chain of the target memory op.              #
# --------------------------------------------------------------------------- #
GLOBAL = r"""
.version 8.5
.target sm_89
.address_size 64
.visible .entry chase(.param .u64 pchain, .param .u64 pout, .param .u32 niter){
  .reg .b64 %base,%out,%p,%t0,%t1,%dt; .reg .b32 %n,%i,%lo,%s; .reg .pred %pd;
  ld.param.u64 %base,[pchain]; cvta.to.global.u64 %base,%base;
  ld.param.u64 %out,[pout];    cvta.to.global.u64 %out,%out;
  ld.param.u32 %n,[niter];
  mov.u64 %p,%base; mov.u32 %i,0;
W: setp.ge.u32 %pd,%i,%n; @%pd bra WE;
   ld.global.u64 %p,[%p]; add.u32 %i,%i,1; bra W;
WE: mov.u64 %p,%base; mov.u64 %t0,%clock64; mov.u32 %i,0;
T: setp.ge.u32 %pd,%i,%n; @%pd bra D;
   ld.global.u64 %p,[%p]; add.u32 %i,%i,1; bra T;
D: mov.u64 %t1,%clock64; sub.u64 %dt,%t1,%t0; cvt.u32.u64 %lo,%dt;
   st.global.u32 [%out],%lo; cvt.u32.u64 %s,%p; st.global.u32 [%out+8],%s; ret;
}
"""

SHARED = r"""
.version 8.5
.target sm_89
.address_size 64
.visible .entry chase_s(.param .u64 pseed, .param .u64 pout,
                        .param .u32 niter, .param .u32 nslot){
  .reg .b64 %seed,%out,%t0,%t1,%dt,%a; .reg .b32 %n,%i,%lo,%ns,%cur,%nxt,%base,%addr,%sink; .reg .pred %pd;
  .shared .align 8 .b32 smem[8192];
  ld.param.u64 %seed,[pseed]; cvta.to.global.u64 %seed,%seed;
  ld.param.u64 %out,[pout];   cvta.to.global.u64 %out,%out;
  ld.param.u32 %n,[niter];    ld.param.u32 %ns,[nslot];
  mov.u32 %base,smem; mov.u32 %i,0;
CP: setp.ge.u32 %pd,%i,%ns; @%pd bra CPE;
    mul.wide.u32 %a,%i,4; cvt.u32.u64 %addr,%a; add.s64 %a,%seed,%a; ld.global.u32 %nxt,[%a];
    shl.b32 %nxt,%nxt,2; add.u32 %nxt,%nxt,%base; add.u32 %addr,%addr,%base; st.shared.u32 [%addr],%nxt;
    add.u32 %i,%i,1; bra CP;
CPE: bar.sync 0; mov.u32 %cur,%base; mov.u32 %i,0;
W: setp.ge.u32 %pd,%i,%n; @%pd bra WE; ld.shared.u32 %cur,[%cur]; add.u32 %i,%i,1; bra W;
WE: mov.u32 %cur,%base; mov.u64 %t0,%clock64; mov.u32 %i,0;
T: setp.ge.u32 %pd,%i,%n; @%pd bra D; ld.shared.u32 %cur,[%cur]; add.u32 %i,%i,1; bra T;
D: mov.u64 %t1,%clock64; sub.u64 %dt,%t1,%t0; cvt.u32.u64 %lo,%dt;
   st.global.u32 [%out],%lo; st.global.u32 [%out+8],%cur; ret;
}
"""

CONST = r"""
.version 8.5
.target sm_89
.address_size 64
.const .align 4 .b32 ctab[4096];
.visible .entry chase_c(.param .u64 pout, .param .u32 niter){
  .reg .b64 %out,%t0,%t1,%dt,%ad; .reg .b32 %n,%i,%lo,%idx,%v,%cbase; .reg .pred %pd;
  ld.param.u64 %out,[pout]; cvta.to.global.u64 %out,%out; ld.param.u32 %n,[niter];
  mov.u32 %cbase,ctab; mov.u32 %idx,0; mov.u64 %t0,%clock64; mov.u32 %i,0;
T: setp.ge.u32 %pd,%i,%n; @%pd bra D;
   add.u32 %v,%cbase,%idx; cvt.u64.u32 %ad,%v; ld.const.u32 %v,[%ad];
   and.b32 %idx,%v,0x3ffc; add.u32 %i,%i,1; bra T;
D: mov.u64 %t1,%clock64; sub.u64 %dt,%t1,%t0; cvt.u32.u64 %lo,%dt;
   st.global.u32 [%out],%lo; st.global.u32 [%out+8],%idx; ret;
}
"""

LOCAL = r"""
.version 8.5
.target sm_89
.address_size 64
.visible .entry chase_l(.param .u64 pseed, .param .u64 pout, .param .u32 niter, .param .u32 nslot){
  .local .align 4 .b32 lmem[2048];
  .reg .b64 %seed,%out,%t0,%t1,%dt,%a; .reg .b32 %n,%i,%lo,%ns,%cur,%nxt,%off,%lbase,%addr; .reg .pred %pd;
  ld.param.u64 %seed,[pseed]; cvta.to.global.u64 %seed,%seed;
  ld.param.u64 %out,[pout]; cvta.to.global.u64 %out,%out;
  ld.param.u32 %n,[niter]; ld.param.u32 %ns,[nslot];
  mov.u32 %lbase,lmem; mov.u32 %off,0; mov.u32 %i,0;
F: setp.ge.u32 %pd,%i,%ns; @%pd bra FE;
   mul.wide.u32 %a,%i,4; add.s64 %a,%seed,%a; ld.global.u32 %nxt,[%a];
   shl.b32 %nxt,%nxt,2; add.u32 %addr,%lbase,%off; st.local.u32 [%addr],%nxt;
   add.u32 %off,%off,4; add.u32 %i,%i,1; bra F;
FE: mov.u32 %cur,0; mov.u32 %i,0;
W: setp.ge.u32 %pd,%i,%n; @%pd bra WE; add.u32 %addr,%lbase,%cur; ld.local.u32 %cur,[%addr]; add.u32 %i,%i,1; bra W;
WE: mov.u32 %cur,0; mov.u64 %t0,%clock64; mov.u32 %i,0;
T: setp.ge.u32 %pd,%i,%n; @%pd bra D; add.u32 %addr,%lbase,%cur; ld.local.u32 %cur,[%addr]; add.u32 %i,%i,1; bra T;
D: mov.u64 %t1,%clock64; sub.u64 %dt,%t1,%t0; cvt.u32.u64 %lo,%dt;
   st.global.u32 [%out],%lo; st.global.u32 [%out+8],%cur; ret;
}
"""

ATOMG = r"""
.version 8.5
.target sm_89
.address_size 64
.visible .entry atomp(.param .u64 pbuf, .param .u64 pout, .param .u32 niter){
  .reg .b64 %buf,%out,%a,%t0,%t1,%dt; .reg .b32 %n,%i,%lo,%v,%idx; .reg .pred %pd;
  ld.param.u64 %buf,[pbuf]; cvta.to.global.u64 %buf,%buf;
  ld.param.u64 %out,[pout]; cvta.to.global.u64 %out,%out; ld.param.u32 %n,[niter];
  mov.u32 %idx,0; mov.u64 %t0,%clock64; mov.u32 %i,0;
T: setp.ge.u32 %pd,%i,%n; @%pd bra D;
   mul.wide.u32 %a,%idx,4; add.s64 %a,%buf,%a; atom.global.add.u32 %v,[%a],1;
   and.b32 %idx,%v,0x3f; add.u32 %i,%i,1; bra T;
D: mov.u64 %t1,%clock64; sub.u64 %dt,%t1,%t0; cvt.u32.u64 %lo,%dt;
   st.global.u32 [%out],%lo; st.global.u32 [%out+8],%idx; ret;
}
"""

ATOMS = r"""
.version 8.5
.target sm_89
.address_size 64
.visible .entry atoms(.param .u64 pout, .param .u32 niter){
  .reg .b64 %out,%t0,%t1,%dt; .reg .b32 %n,%i,%lo,%v,%idx,%base,%addr; .reg .pred %pd;
  .shared .align 4 .b32 sm[256];
  ld.param.u64 %out,[pout]; cvta.to.global.u64 %out,%out; ld.param.u32 %n,[niter];
  mov.u32 %base,sm; mov.u32 %idx,0; mov.u64 %t0,%clock64; mov.u32 %i,0;
T: setp.ge.u32 %pd,%i,%n; @%pd bra D;
   shl.b32 %addr,%idx,2; add.u32 %addr,%addr,%base; atom.shared.add.u32 %v,[%addr],1;
   and.b32 %idx,%v,0x3f; add.u32 %i,%i,1; bra T;
D: mov.u64 %t1,%clock64; sub.u64 %dt,%t1,%t0; cvt.u32.u64 %lo,%dt;
   st.global.u32 [%out],%lo; st.global.u32 [%out+8],%idx; ret;
}
"""

STG = r"""
.version 8.5
.target sm_89
.address_size 64
.visible .entry stgrt(.param .u64 pbuf, .param .u64 pout, .param .u32 niter){
  .reg .b64 %buf,%out,%t0,%t1,%dt; .reg .b32 %n,%i,%lo,%v; .reg .pred %pd;
  ld.param.u64 %buf,[pbuf]; cvta.to.global.u64 %buf,%buf;
  ld.param.u64 %out,[pout]; cvta.to.global.u64 %out,%out; ld.param.u32 %n,[niter];
  mov.u32 %v,0; mov.u32 %i,0; mov.u64 %t0,%clock64;
T: setp.ge.u32 %pd,%i,%n; @%pd bra D;
   st.global.u32 [%buf],%v; ld.global.u32 %v,[%buf]; add.u32 %i,%i,1; bra T;
D: mov.u64 %t1,%clock64; sub.u64 %dt,%t1,%t0; cvt.u32.u64 %lo,%dt;
   st.global.u32 [%out],%lo; st.global.u32 [%out+8],%v; ret;
}
"""

RED = r"""
.version 8.5
.target sm_89
.address_size 64
.visible .entry redp(.param .u64 pbuf, .param .u64 pout, .param .u32 niter){
  .reg .b64 %buf,%out,%t0,%t1,%dt; .reg .b32 %n,%i,%lo; .reg .pred %pd;
  ld.param.u64 %buf,[pbuf]; cvta.to.global.u64 %buf,%buf;
  ld.param.u64 %out,[pout]; cvta.to.global.u64 %out,%out; ld.param.u32 %n,[niter];
  mov.u32 %i,0; mov.u64 %t0,%clock64;
T: setp.ge.u32 %pd,%i,%n; @%pd bra D;
   red.global.add.u32 [%buf],1; add.u32 %i,%i,1; bra T;
D: mov.u64 %t1,%clock64; sub.u64 %dt,%t1,%t0; cvt.u32.u64 %lo,%dt;
   st.global.u32 [%out],%lo; ret;
}
"""

LDGSTS = r"""
.version 8.5
.target sm_89
.address_size 64
.visible .entry cpa(.param .u64 pbuf, .param .u64 pout, .param .u32 niter){
  .reg .b64 %buf,%out,%t0,%t1,%dt; .reg .b32 %n,%i,%lo,%base,%v; .reg .pred %pd;
  .shared .align 16 .b32 sm[64];
  ld.param.u64 %buf,[pbuf]; cvta.to.global.u64 %buf,%buf;
  ld.param.u64 %out,[pout]; cvta.to.global.u64 %out,%out; ld.param.u32 %n,[niter];
  mov.u32 %base,sm; mov.u32 %i,0; mov.u64 %t0,%clock64;
T: setp.ge.u32 %pd,%i,%n; @%pd bra D;
   cp.async.cg.shared.global [%base],[%buf],16; cp.async.commit_group; cp.async.wait_group 0;
   add.u32 %i,%i,1; bra T;
D: mov.u64 %t1,%clock64; sub.u64 %dt,%t1,%t0; cvt.u32.u64 %lo,%dt;
   ld.shared.u32 %v,[%base]; st.global.u32 [%out],%lo; st.global.u32 [%out+8],%v; ret;
}
"""


# --------------------------------------------------------------------------- #
# Launch helpers.  Each returns cycles-per-op (clock64 delta / op count).      #
# --------------------------------------------------------------------------- #
def _chain_abs(cu: SL.Cuda, nslot: int, stride: int) -> int:
    """Allocate and fill a u64 chase buffer of absolute next-slot addresses."""
    buf = cu.mem_alloc(nslot * 8)
    host = bytearray(nslot * 8)
    for i in range(nslot):
        struct.pack_into("<Q", host, i * 8, buf + ((i + stride) % nslot) * 8)
    cu.memcpy_htod(buf, bytes(host))
    return buf


def _chain_idx(cu: SL.Cuda, nslot: int, stride: int) -> int:
    """Allocate a u32 seed of next-slot indices (for smem/local refill)."""
    seed = cu.mem_alloc(nslot * 4)
    cu.memcpy_htod(seed, b"".join(struct.pack("<I", (i + stride) % nslot)
                                  for i in range(nslot)))
    return seed


def run_global(cu, cub, nslot, stride):
    buf = _chain_abs(cu, nslot, stride)
    out = cu.mem_alloc(16); cu.memset_d8(out, 0, 16)
    try:
        mod = cu.load_module(Path(cub).read_bytes()); fn = cu.get_function(mod, "chase")
        cu.launch(fn, (1, 1, 1), (1, 1, 1), 0, struct.pack("<QQI", buf, out, NITER))
        cu.synchronize()
        ticks = struct.unpack_from("<I", cu.memcpy_dtoh(out, 16), 0)[0]
        cu.unload_module(mod); return ticks / NITER
    finally:
        cu.mem_free(buf); cu.mem_free(out)


def run_seeded(cu, cub, entry, nslot, stride):
    seed = _chain_idx(cu, nslot, stride)
    out = cu.mem_alloc(16); cu.memset_d8(out, 0, 16)
    try:
        mod = cu.load_module(Path(cub).read_bytes()); fn = cu.get_function(mod, entry)
        cu.launch(fn, (1, 1, 1), (1, 1, 1), 0, struct.pack("<QQII", seed, out, NITER, nslot))
        cu.synchronize()
        ticks = struct.unpack_from("<I", cu.memcpy_dtoh(out, 16), 0)[0]
        cu.unload_module(mod); return ticks / NITER
    finally:
        cu.mem_free(seed); cu.mem_free(out)


def run_buf(cu, cub, entry, buf_bytes, block=(1, 1, 1)):
    buf = cu.mem_alloc(buf_bytes); cu.memset_d8(buf, 0, buf_bytes)
    out = cu.mem_alloc(16); cu.memset_d8(out, 0, 16)
    try:
        mod = cu.load_module(Path(cub).read_bytes()); fn = cu.get_function(mod, entry)
        cu.launch(fn, (1, 1, 1), block, 0, struct.pack("<QQI", buf, out, NITER))
        cu.synchronize()
        ticks = struct.unpack_from("<I", cu.memcpy_dtoh(out, 16), 0)[0]
        cu.unload_module(mod); return ticks / NITER
    finally:
        cu.mem_free(buf); cu.mem_free(out)


def run_outonly(cu, cub, entry, block=(1, 1, 1)):
    out = cu.mem_alloc(16); cu.memset_d8(out, 0, 16)
    try:
        mod = cu.load_module(Path(cub).read_bytes()); fn = cu.get_function(mod, entry)
        cu.launch(fn, (1, 1, 1), block, 0, struct.pack("<QI", out, NITER))
        cu.synchronize()
        ticks = struct.unpack_from("<I", cu.memcpy_dtoh(out, 16), 0)[0]
        cu.unload_module(mod); return ticks / NITER
    finally:
        cu.mem_free(out)


def best(fn, *a, rep=REP):
    return median([fn(*a) for _ in range(rep)])


def main() -> int:
    cu = SL.Cuda(0)
    g = compile_ptx(GLOBAL, "ml_global")
    s = compile_ptx(SHARED, "ml_shared")
    c = compile_ptx(CONST, "ml_const")
    lo = compile_ptx(LOCAL, "ml_local")
    ag = compile_ptx(ATOMG, "ml_atomg")
    ats = compile_ptx(ATOMS, "ml_atoms")
    st = compile_ptx(STG, "ml_stg")
    rd = compile_ptx(RED, "ml_red")
    cp = compile_ptx(LDGSTS, "ml_ldgsts")

    rows = []
    rows.append(("LDG", "global/L1_hit", best(run_global, cu, g, 256, 16)))
    rows.append(("LDG", "global/L2", best(run_global, cu, g, 131072, 257)))
    rows.append(("LDG", "global/DRAM_miss", best(run_global, cu, g, 12 * 1024 * 1024, 2049)))
    rows.append(("LDL", "local/L1_hit", best(run_seeded, cu, lo, "chase_l", 256, 7)))
    rows.append(("LDS", "shared/smem", best(run_seeded, cu, s, "chase_s", 1024, 7)))
    rows.append(("LDC", "const/cache_hit", best(run_outonly, cu, c, "chase_c")))
    rows.append(("STG", "global/L1_writeback", best(run_buf, cu, st, "stgrt", 64)))
    rows.append(("ATOMG", "global/L2_rmw", best(run_buf, cu, ag, "atomp", 256)))
    rows.append(("ATOMS", "shared/smem_rmw", best(run_outonly, cu, ats, "atoms", (1, 1, 1))))
    rows.append(("RED", "global/L2_rmw", best(run_buf, cu, rd, "redp", 64)))
    rows.append(("LDGSTS", "global/async_copy_L2", best(run_buf, cu, cp, "cpa", 1024)))
    cu.close()

    print(f"{'op':8s} {'space/tier':24s} {'cyc/op':>8s}")
    print("-" * 44)
    for op, tier, cyc in rows:
        print(f"{op:8s} {tier:24s} {cyc:8.1f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
