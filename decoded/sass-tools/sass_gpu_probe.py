#!/usr/bin/env python3
# nvopen-tools -- SASS reverse-engineering tooling.  Built on the public CUDA
# Driver API + ptxas/nvdisasm/cuobjdump.
"""GPU differential-test harness for SASS integer/logic functional models.

The functional model in `sass_sem_int` claims a closed-form bit-exact result for
each integer/logic SASS instruction.  A model that is not checked against the
silicon is a guess; this module is the arbiter.  For one target SASS op it:

  1. Emits a tiny single-thread-per-element PTX kernel whose body is a PTX op
     that ptxas lowers to *exactly* the SASS instruction we want (global loads
     defeat constant-folding so the op survives to SASS).
  2. Compiles with the real `ptxas -arch sm_89` and disassembles with
     `cuobjdump -sass` to CONFIRM the intended SASS mnemonic+modifiers were
     emitted (a model is only meaningful if the op it models is the op that ran).
  3. Uploads input vectors, launches over the Driver API (via `sass_launch`'s
     ctypes binding), reads the output back, and compares the device result to
     the model's prediction BIT-FOR-BIT over a spread of edge-case inputs.

The kernel takes up to four global u32 (or u64) arrays a,b,c and writes out[i];
each thread handles one element, so a single launch covers a whole input batch.

Local device is sm_89 (Ada).  The integer/logic datapath (IADD3/IMAD/LOP3/SHF/
PRMT/ISETP/...) is Volta-class and unchanged across Volta..Ada, so an sm_89
ground-truth is authoritative for every arch that shares this encoder family.
"""
from __future__ import annotations

import struct
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sass_launch as SL  # noqa: E402  (ctypes Driver-API launcher)

PTXAS = "/usr/local/cuda-13.1/bin/ptxas"
CUOBJDUMP = "/usr/local/cuda-13.1/bin/cuobjdump"
ARCH = "sm_89"
M32 = 0xFFFFFFFF
M64 = 0xFFFFFFFFFFFFFFFF
_TMP = Path("/tmp")


# --------------------------------------------------------------------------- #
# PTX kernel templates.                                                        #
# --------------------------------------------------------------------------- #
def kernel_u32(body: str, *, n_out: int = 1) -> str:
    """Single-thread-per-element kernel over four u32 input arrays a,b,c,d.

    `body` is PTX operating on source regs %ra,%rb,%rc,%rd and producing %ro
    (and %ro1 when `n_out`==2, stored at out[i] and out2[i]).  Five pointer
    params: pa,pb,pc,pd,po (+po2 if n_out==2).  Global loads/stores keep the
    modeled op out of the constant-folder so it survives to SASS.
    """
    out2_param = ", .param .u64 po2" if n_out == 2 else ""
    out2_setup = (
        "ld.param.u64 %do2,[po2]; cvta.to.global.u64 %do2,%do2;\n"
        "  add.s64 %do2,%do2,%off;\n" if n_out == 2 else "")
    out2_store = "st.global.b32 [%do2],%ro1;\n" if n_out == 2 else ""
    return f""".version 8.5
.target {ARCH}
.address_size 64
.visible .entry probe(.param .u64 pa, .param .u64 pb, .param .u64 pc,
                      .param .u64 pd, .param .u64 po{out2_param}){{
  .reg .b32 %ra,%rb,%rc,%rd,%ro,%ro1,%idx,%t0,%t1,%t2,%t3;
  .reg .b64 %da,%db,%dc,%dd,%do,%do2,%off,%q0,%q1,%q2;
  .reg .pred %p0,%p1,%p2,%p3;
  ld.param.u64 %da,[pa]; cvta.to.global.u64 %da,%da;
  ld.param.u64 %db,[pb]; cvta.to.global.u64 %db,%db;
  ld.param.u64 %dc,[pc]; cvta.to.global.u64 %dc,%dc;
  ld.param.u64 %dd,[pd]; cvta.to.global.u64 %dd,%dd;
  ld.param.u64 %do,[po]; cvta.to.global.u64 %do,%do;
  mov.u32 %idx,%tid.x;
  mul.wide.u32 %off,%idx,4;
  add.s64 %da,%da,%off; add.s64 %db,%db,%off;
  add.s64 %dc,%dc,%off; add.s64 %dd,%dd,%off;
  add.s64 %do,%do,%off;
  {out2_setup}
  ld.global.b32 %ra,[%da];
  ld.global.b32 %rb,[%db];
  ld.global.b32 %rc,[%dc];
  ld.global.b32 %rd,[%dd];
  {body}
  st.global.b32 [%do],%ro;
  {out2_store}
  ret;
}}
"""


def kernel_u64(body: str) -> str:
    """Kernel over three u64 input arrays a,b,c -> one u64 out (for .WIDE etc.).

    `body` operates on %ra,%rb,%rc (.b64) -> %ro (.b64).  Stride is 8 bytes.
    """
    return f""".version 8.5
.target {ARCH}
.address_size 64
.visible .entry probe(.param .u64 pa, .param .u64 pb, .param .u64 pc,
                      .param .u64 po){{
  .reg .b32 %idx,%t0,%t1;
  .reg .b64 %ra,%rb,%rc,%ro,%da,%db,%dc,%do,%off,%q0,%q1;
  .reg .pred %p0,%p1;
  ld.param.u64 %da,[pa]; cvta.to.global.u64 %da,%da;
  ld.param.u64 %db,[pb]; cvta.to.global.u64 %db,%db;
  ld.param.u64 %dc,[pc]; cvta.to.global.u64 %dc,%dc;
  ld.param.u64 %do,[po]; cvta.to.global.u64 %do,%do;
  mov.u32 %idx,%tid.x;
  mul.wide.u32 %off,%idx,8;
  add.s64 %da,%da,%off; add.s64 %db,%db,%off;
  add.s64 %dc,%dc,%off; add.s64 %do,%do,%off;
  ld.global.b64 %ra,[%da];
  ld.global.b64 %rb,[%db];
  ld.global.b64 %rc,[%dc];
  {body}
  st.global.b64 [%do],%ro;
  ret;
}}
"""


# --------------------------------------------------------------------------- #
# Compile + SASS confirmation.                                                 #
# --------------------------------------------------------------------------- #
@dataclass
class Compiled:
    cubin: str
    sass: str
    ok: bool
    err: str = ""


def compile_ptx(ptx: str, must_contain: list[str] | str,
                tag: str = "probe") -> Compiled:
    """Compile `ptx` and confirm every token in `must_contain` is in the SASS.

    `must_contain` is a SASS substring (or list of them) that the disassembly
    must include -- this is the proof the kernel actually runs the op we model.
    """
    if isinstance(must_contain, str):
        must_contain = [must_contain]
    p = _TMP / f"{tag}.ptx"
    cub = _TMP / f"{tag}.cubin"
    p.write_text(ptx)
    r = subprocess.run([PTXAS, "-arch", ARCH, str(p), "-o", str(cub)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return Compiled("", "", False, err=r.stderr.strip())
    sass = subprocess.run([CUOBJDUMP, "-sass", str(cub)],
                          capture_output=True, text=True).stdout
    missing = [tok for tok in must_contain if tok not in sass]
    if missing:
        return Compiled("", sass, False, err=f"SASS missing {missing}")
    return Compiled(str(cub), sass, True)


def sass_ops(sass: str) -> list[str]:
    """The decoded instruction mnemonic lines (for reporting which op ran)."""
    out = []
    for ln in sass.splitlines():
        s = ln.strip()
        if "/*" in s and "*/" in s and ";" in s:
            # strip the leading address comment and trailing hex comment
            body = s.split("*/", 1)[1].split(";")[0].strip()
            if body and not body.startswith("0x"):
                out.append(body)
    return out


# --------------------------------------------------------------------------- #
# GPU launch + differential compare.                                           #
# --------------------------------------------------------------------------- #
@dataclass
class DiffResult:
    name: str
    n: int
    passed: int
    failures: list[tuple] = field(default_factory=list)   # (inputs..., got, exp)

    @property
    def all_pass(self) -> bool:
        return self.passed == self.n


class GpuRunner:
    """Owns one CUDA context; runs probe kernels and reads results back.

    Reused across many ops so we pay context-creation once.  Device arrays are
    re-allocated per call (cheap for the small batches a differential test uses).
    """

    def __init__(self, device: int = 0):
        self.cu = SL.Cuda(device)

    def close(self):
        self.cu.close()

    def run_u32(self, cubin: str, a: list[int], b: list[int], c: list[int],
                d: list[int], *, n_out: int = 1) -> list[list[int]]:
        """Launch the u32 probe over input vectors; return [out] (or [out,out2])."""
        n = len(a)
        sz = n * 4
        cu = self.cu

        def up(vec):
            ptr = cu.mem_alloc(sz)
            cu.memcpy_htod(ptr, b"".join(struct.pack("<I", x & M32) for x in vec))
            return ptr
        da, db, dc, dd = up(a), up(b), up(c), up(d)
        do = cu.mem_alloc(sz); cu.memset_d8(do, 0, sz)
        ptrs = [da, db, dc, dd, do]
        do2 = None
        if n_out == 2:
            do2 = cu.mem_alloc(sz); cu.memset_d8(do2, 0, sz); ptrs.append(do2)
        try:
            mod = cu.load_module(Path(cubin).read_bytes())
            fn = cu.get_function(mod, "probe")
            pbuf = struct.pack("<" + "Q" * len(ptrs), *ptrs)
            cu.launch(fn, (1, 1, 1), (n, 1, 1), 0, pbuf)
            cu.synchronize()
            outs = [[struct.unpack_from("<I", cu.memcpy_dtoh(do, sz), 4 * i)[0]
                     for i in range(n)]]
            if n_out == 2:
                raw2 = cu.memcpy_dtoh(do2, sz)
                outs.append([struct.unpack_from("<I", raw2, 4 * i)[0]
                             for i in range(n)])
            cu.unload_module(mod)
            return outs
        finally:
            for ptr in ptrs:
                cu.mem_free(ptr)

    def run_u64(self, cubin: str, a: list[int], b: list[int],
                c: list[int]) -> list[int]:
        n = len(a)
        sz = n * 8
        cu = self.cu

        def up(vec):
            ptr = cu.mem_alloc(sz)
            cu.memcpy_htod(ptr, b"".join(struct.pack("<Q", x & M64) for x in vec))
            return ptr
        da, db, dc = up(a), up(b), up(c)
        do = cu.mem_alloc(sz); cu.memset_d8(do, 0, sz)
        ptrs = [da, db, dc, do]
        try:
            mod = cu.load_module(Path(cubin).read_bytes())
            fn = cu.get_function(mod, "probe")
            cu.launch(fn, (1, 1, 1), (n, 1, 1), 0,
                      struct.pack("<QQQQ", *ptrs))
            cu.synchronize()
            raw = cu.memcpy_dtoh(do, sz)
            cu.unload_module(mod)
            return [struct.unpack_from("<Q", raw, 8 * i)[0] for i in range(n)]
        finally:
            for ptr in ptrs:
                cu.mem_free(ptr)


# Standard 32-bit edge-case corpus (the boundaries that break naive models).
EDGE32 = [0x00000000, 0x00000001, 0xFFFFFFFF, 0x80000000, 0x7FFFFFFF,
          0x00000002, 0xFFFFFFFE, 0x0000001F, 0x00000020, 0x00000021,
          0x000000FF, 0xFF000000, 0x12345678, 0xDEADBEEF, 0xAAAAAAAA,
          0x55555555, 0xCAFEBABE, 0x80000001, 0x7FFFFFFE, 0x40000000]
EDGE64 = [0, 1, M64, 1 << 63, (1 << 63) - 1, 0x1122334455667788,
          0xFFFFFFFF, 0x100000000, 0xDEADBEEFCAFEBABE, 0xFFFFFFFF00000000]
