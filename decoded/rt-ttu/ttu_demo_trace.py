#!/usr/bin/env python3
"""Raw-SASS TTU (RT core) execution on sm_89 — no PTX RT intrinsics, no OptiX.

Injects hand-assembled TTU instructions (ttu_asm.py) into a ptxas-built cubin and
runs them on the GPU through sass_launch.

`demo_cctl()` is the verified result: a hand-assembled TTUCCTL executes on the RT
core. A PTX kernel writes a before-marker, runs the injected TTU instruction, then
writes an after-marker; both markers appearing in the output proves control flowed
through the injected instruction without faulting.

`build_trace_cubin()` stages the trace (TTUST ray+root -> TTUOPEN -> TTUGO ->
TTULD[0x300]) over the driver-built BVH in driver_bvh_1tri.bin. The instruction
encodings are in hand; what it needs is the per-instruction register wiring under a
controlled allocation plus the root-node-ref / scoreboard-handshake encodings,
which require the driver's own emitted TTU sequence as the reference.
"""
from __future__ import annotations

import struct
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "sass-tools"))
sys.path.insert(0, str(HERE))
import sass_launch as SL          # noqa: E402
import ttu_asm as A               # noqa: E402

PTXAS = "/usr/local/cuda-13.1/bin/ptxas"

_CCTL_PTX = """
.version 8.0
.target sm_89
.address_size 64
.visible .entry ttu_cctl_probe(.param .u64 outp)
{
    .reg .b32 %r<6>; .reg .b64 %rd<3>;
    ld.param.u64 %rd1,[outp]; cvta.to.global.u64 %rd2,%rd1;
    mov.u32 %r1,0xB0; st.global.volatile.u32 [%rd2],%r1;
    mov.u32 %r2,0x71717171;                       // injection slot -> TTUCCTL
    st.global.volatile.u32 [%rd2+8],%r2;
    mov.u32 %r3,0xA0; st.global.volatile.u32 [%rd2+4],%r3;
    ret;
}
"""


def _ptxas(ptx: str, out: Path) -> None:
    src = out.with_suffix(".ptx")
    src.write_text(ptx)
    subprocess.run([PTXAS, "-arch", "sm_89", "-O0", str(src), "-o", str(out)],
                   check=True)


def inject(cubin: bytearray, find_imm: bytes, instr: bytes) -> int:
    """Overwrite the 16-byte instruction containing `find_imm` with `instr`."""
    idx = cubin.find(find_imm)
    if idx < 0:
        raise ValueError("injection marker not found")
    slot = idx & ~0xF
    cubin[slot:slot + 16] = instr
    return slot


def demo_cctl() -> bool:
    """Inject + run a hand-assembled TTUCCTL. Returns True if it executed."""
    cubin_path = HERE.parent.parent / "workspace" / "ttu_trace" / "cctl.cubin"
    cubin_path.parent.mkdir(parents=True, exist_ok=True)
    _ptxas(_CCTL_PTX, cubin_path)
    cb = bytearray(cubin_path.read_bytes())
    inject(cb, bytes.fromhex("71717171"), A.ttucctl())

    ki = SL.introspect(str(cubin_path), "ttu_cctl_probe")
    cu = SL.Cuda(0)
    mod = cu.load_module(bytes(cb))
    fn = cu.get_function(mod, "ttu_cctl_probe")
    out = cu.mem_alloc(64)
    cu.memset_d8(out, 0, 64)
    buf = bytearray(ki.param_buffer_size or 8)
    buf[ki.params[0].offset:ki.params[0].offset + 8] = struct.pack("<Q", out)
    cu.launch(fn, (1, 1, 1), (1, 1, 1), 0, bytes(buf))
    cu.synchronize()
    res = cu.memcpy_dtoh(out, 64)
    before = res.find(b"\xb0\x00\x00\x00") >= 0
    after = res.find(b"\xa0\x00\x00\x00") >= 0
    print(f"TTUCCTL: before={before} after={after} -> "
          f"{'executed' if before and after else 'faulted'}")
    return before and after


if __name__ == "__main__":
    sys.exit(0 if demo_cctl() else 1)
