#!/usr/bin/env python3
# nvopen-tools -- self-test for the generic cubin launcher (sass_launch.py).
"""
Build a corpus of PTX kernels with VARIED signatures, compile them to sm_89
cubins, and exercise the generic launcher end-to-end:

  * introspection recovers the right param count / offsets / sizes;
  * the arena trick lets pointer/scalar/multi-buffer/shared-mem kernels launch
    and read back a hash;
  * a deliberate null-deref kernel is CAUGHT (verdict=crashed), not fatal;
  * the V1/V2 gate (recompose -> patch -> launch both -> bit-compare) MATCHES
    for a faithful recompose and DIFFERS when a scoreboard wait is removed.

Run on a machine with an sm_89 (or any pre-Blackwell) GPU + CUDA 13.x.  No GPU?
The introspection assertions still run; launch assertions are skipped.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import sass_launch as SL  # noqa: E402

PTXAS = "/usr/local/cuda-13.1/bin/ptxas"
ARCH = "sm_89"

# (name, ptx body, expected (n_params, n_pointer_slots, n_scalar_slots))
KERNELS: list[tuple[str, str, tuple[int, int, int]]] = [
    ("t_single", """
.visible .entry t_single( .param .u64 p0 ) {
  .reg .b32 %r<3>; .reg .b64 %rd<4>;
  ld.param.u64 %rd1,[p0]; cvta.to.global.u64 %rd2,%rd1;
  mov.u32 %r1,%tid.x; mul.wide.u32 %rd3,%r1,4; add.s64 %rd2,%rd2,%rd3;
  st.global.u32 [%rd2],%r1; ret; }
""", (1, 1, 0)),
    ("t_scalarptr", """
.visible .entry t_scalarptr( .param .u32 n, .param .u64 p0 ) {
  .reg .b32 %r<3>; .reg .b64 %rd<4>;
  ld.param.u32 %r1,[n]; ld.param.u64 %rd1,[p0]; cvta.to.global.u64 %rd2,%rd1;
  mov.u32 %r2,%tid.x; mul.wide.u32 %rd3,%r2,4; add.s64 %rd2,%rd2,%rd3;
  st.global.u32 [%rd2],%r1; ret; }
""", (2, 1, 1)),
    ("t_threebuf", """
.visible .entry t_threebuf( .param .u64 a, .param .u64 b, .param .u64 c ) {
  .reg .b32 %r<6>; .reg .b64 %rd<10>;
  ld.param.u64 %rd1,[a]; ld.param.u64 %rd2,[b]; ld.param.u64 %rd3,[c];
  cvta.to.global.u64 %rd4,%rd1; cvta.to.global.u64 %rd5,%rd2; cvta.to.global.u64 %rd6,%rd3;
  mov.u32 %r1,%tid.x; mul.wide.u32 %rd7,%r1,4;
  add.s64 %rd4,%rd4,%rd7; add.s64 %rd5,%rd5,%rd7; add.s64 %rd6,%rd6,%rd7;
  ld.global.u32 %r2,[%rd4]; ld.global.u32 %r3,[%rd5]; add.s32 %r4,%r2,%r3;
  st.global.u32 [%rd6],%r4; ret; }
""", (3, 3, 0)),
    ("t_shared", """
.visible .entry t_shared( .param .u64 p0 ) {
  .reg .b32 %r<5>; .reg .b64 %rd<5>; .shared .align 4 .b8 smem[128];
  ld.param.u64 %rd1,[p0]; cvta.to.global.u64 %rd2,%rd1;
  mov.u32 %r1,%tid.x; and.b32 %r2,%r1,31; mul.wide.u32 %rd3,%r2,4;
  mov.u64 %rd4,smem; add.s64 %rd4,%rd4,%rd3; st.shared.u32 [%rd4],%r1; bar.sync 0;
  ld.shared.u32 %r3,[%rd4]; add.s64 %rd2,%rd2,%rd3; st.global.u32 [%rd2],%r3; ret; }
""", (1, 1, 0)),
    ("t_mixed", """
.visible .entry t_mixed( .param .u64 src, .param .u64 dst, .param .u32 n,
                         .param .u32 stride, .param .u64 aux ) {
  .reg .b32 %r<8>; .reg .b64 %rd<8>;
  ld.param.u64 %rd1,[src]; ld.param.u64 %rd2,[dst];
  ld.param.u32 %r1,[n]; ld.param.u32 %r2,[stride]; ld.param.u64 %rd3,[aux];
  cvta.to.global.u64 %rd4,%rd1; cvta.to.global.u64 %rd5,%rd2;
  mov.u32 %r3,%tid.x; mul.lo.s32 %r4,%r3,%r2; mul.wide.u32 %rd6,%r4,4;
  add.s64 %rd4,%rd4,%rd6; ld.global.u32 %r5,[%rd4]; add.s32 %r6,%r5,%r1;
  mul.wide.u32 %rd7,%r3,4; add.s64 %rd5,%rd5,%rd7; st.global.u32 [%rd5],%r6; ret; }
""", (5, 3, 2)),
    ("t_nullderef", """
.visible .entry t_nullderef( .param .u64 p0 ) {
  .reg .b32 %r<3>; .reg .b64 %rd<3>;
  mov.u64 %rd1,0; mov.u32 %r1,%tid.x; st.global.u32 [%rd1],%r1; ret; }
""", (1, 1, 0)),
]

PTX_HEAD = ".version 8.3\n.target sm_89\n.address_size 64\n"


def _have_gpu() -> bool:
    try:
        SL.Cuda(0).close()
        return True
    except Exception:
        return False


def main() -> int:
    if not Path(PTXAS).exists():
        print(f"SKIP: ptxas not found at {PTXAS}")
        return 0
    work = Path(tempfile.mkdtemp(prefix="sass_launch_selftest_"))
    fails = 0
    try:
        cubins: dict[str, Path] = {}
        for name, body, _ in KERNELS:
            ptx = work / f"{name}.ptx"
            ptx.write_text(PTX_HEAD + body.strip() + "\n")
            cub = work / f"{name}.cubin"
            r = subprocess.run([PTXAS, "-arch", ARCH, "-O3", "-o", str(cub),
                                str(ptx)], capture_output=True, text=True)
            if r.returncode != 0:
                print(f"FAIL build {name}: {r.stderr.strip()[:120]}")
                fails += 1
                continue
            cubins[name] = cub

        # --- introspection assertions (no GPU needed) ---
        print("== introspection ==")
        for name, _, (np, nptr, nsc) in KERNELS:
            if name not in cubins:
                continue
            ki = SL.introspect(cubins[name], name)
            ok = (ki is not None and len(ki.params) == np
                  and ki.n_pointer_slots == nptr and ki.n_scalar_slots == nsc)
            got = (f"{len(ki.params)}/{ki.n_pointer_slots}/{ki.n_scalar_slots}"
                   if ki else "None")
            print(f"  {name:<14} expect {np}/{nptr}/{nsc}  got {got}  "
                  f"{'OK' if ok else 'FAIL'}")
            fails += 0 if ok else 1

        if not _have_gpu():
            print("\nSKIP: no usable GPU/libcuda -- launch assertions skipped")
            return 1 if fails else 0

        # --- launch assertions ---
        print("\n== launch (arena trick) ==")
        for name in ("t_single", "t_scalarptr", "t_threebuf", "t_shared",
                     "t_mixed"):
            if name not in cubins:
                continue
            r = SL.launch_isolated(cubins[name], name)
            ok = (r.verdict == "launchable" and r.out_hash is not None)
            print(f"  {name:<14} {r.verdict:<12} "
                  f"hash={(r.out_hash or '')[:12]}  {'OK' if ok else 'FAIL'}")
            fails += 0 if ok else 1

        # null-deref must be CAUGHT, not fatal
        r = SL.launch_isolated(cubins["t_nullderef"], "t_nullderef")
        ok = (r.verdict == "crashed")
        print(f"  {'t_nullderef':<14} {r.verdict:<12} "
              f"(must be 'crashed')  {'OK' if ok else 'FAIL'}")
        fails += 0 if ok else 1

        # --- V1/V2 gate: faithful recompose MATCHES ---
        print("\n== V1/V2 gate ==")
        rc = SL.verify_dyn_generic(cubins["t_threebuf"], entry="t_threebuf")
        print(f"  faithful recompose -> rc={rc} (0=match)  "
              f"{'OK' if rc == 0 else 'FAIL'}")
        fails += 0 if rc == 0 else 1

        # --- V1/V2 gate: a removed scoreboard wait DIFFERS ---
        import copy
        import sass_scheduler as S
        d = S.decompose(str(cubins["t_threebuf"]))
        fields = [S.ctrl_to_fields(c) for c in d.ctrls]
        # find the dependent consumer that waits on a scoreboard, drop its wait
        bad = copy.deepcopy(fields)
        dropped = False
        for f in bad:
            if f.wait_mask:
                f.wait_mask = 0
                dropped = True
        v2bad = work / "t_threebuf_BAD.cubin"
        S.patch_cubin(str(cubins["t_threebuf"]), str(v2bad), "t_threebuf", bad)
        if dropped:
            r1 = SL.launch_isolated(cubins["t_threebuf"], "t_threebuf",
                                    block=(256, 1, 1), grid=(64, 1, 1))
            r2 = SL.launch_isolated(v2bad, "t_threebuf",
                                    block=(256, 1, 1), grid=(64, 1, 1))
            differs = (r2.verdict != "launchable"
                       or r1.out_hash != r2.out_hash)
            print(f"  removed scoreboard wait -> "
                  f"V1={(r1.out_hash or '')[:8]} V2={(r2.out_hash or r2.verdict)[:8]}"
                  f"  differs={differs}  {'OK' if differs else 'FAIL'}")
            fails += 0 if differs else 1
        else:
            print("  (no scoreboard wait to drop; negative test skipped)")

        print("\n" + ("ALL TESTS PASSED" if fails == 0
                      else f"{fails} TEST(S) FAILED"))
        return 1 if fails else 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
