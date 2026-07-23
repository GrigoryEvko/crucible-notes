#!/usr/bin/env python3
# nvopen-tools -- SASS reverse-engineering tooling (MIT-style).
# Built on the public CUDA Driver API and `cuobjdump`.
"""
Generic CUDA cubin **introspect + launch** primitive (pure-Python / ctypes).

The SASS scheduler's correctness gate is V1(ptxas) vs V2(reschedule) on the
SAME synthesized inputs -> bit-identical output proves the reschedule preserves
semantics.  Running that gate on arbitrary cubins (e.g. real CUTLASS kernels,
not a hand-wired corpus) requires (1) discovering any kernel's parameter-buffer
layout, (2) synthesizing inputs that let it launch without faulting, (3) launching
it with a watchdog and per-kernel error capture, and (4) reading the result back
and hashing it.  This module is that reusable primitive.

ctypes->libcuda (not the C harness):
The Driver API (`cuModuleLoadData` + `cuLaunchKernel`) needs no host-side device
code, so there is nothing for nvcc/gcc to compile -- it sidesteps the gcc-16 vs
nvcc host-header clash entirely.  `libcuda.so` is bound directly with ctypes.

INTROSPECTION (where the param layout comes from)
-------------------------------------------------
Kernel parameters live in constant bank 0.  ptxas records the layout in the
per-entry `.nv.info.<entry>` attribute stream as three public attributes
(all readable with `cuobjdump --dump-elf`):

  * EIATTR_PARAM_CBANK    {sym, u16 base_offset, u16 total_bytes}  -- the cbank
                          window the param buffer occupies (base + size).
  * EIATTR_CBANK_PARAM_SIZE   u16  -- total param-buffer size in bytes.
  * EIATTR_KPARAM_INFO    one 12-byte record per parameter, carrying the
                          parameter Ordinal, its Offset within the buffer, and
                          its Size (4 = a 32-bit scalar slot, 8 = a 64-bit slot
                          / pointer, etc.).

`cuLaunchKernel`'s parameter buffer is exactly that cbank window with the base
subtracted off: a flat byte buffer of `CBANK_PARAM_SIZE` bytes where parameter
`k` lives at its KPARAM_INFO `Offset` for `Size` bytes.  We parse the layout from
`cuobjdump --dump-elf` (its human-readable Ordinal/Offset/Size decode), and
cross-check the buffer size against `cuFuncGetAttribute`.

INPUT SYNTHESIS (the arena trick)
---------------------------------
We allocate one large zeroed device "arena" and fill the param buffer so that:
  * every 8-byte-aligned 8-byte slot  = the arena base address  (so every
    pointer parameter is a *valid* device pointer into zeroed memory -- reads
    return 0, writes land in the captured arena, no illegal-address fault);
  * every 4-byte scalar slot          = a small constant (1)    (a benign size /
    count / stride that does not send the kernel off the end of the arena).
This makes most "load -> compute -> store" kernels launch and write into the
arena.  We then read the arena back and hash it -- that hash is what the V1/V2
gate compares.

FAULT HANDLING
--------------
Each launch runs in a watchdog thread; a CUDA error (illegal address, launch
failure) or a timeout is caught and turned into a per-kernel verdict
(`launchable | crashed | timed_out | needs_cluster | skipped`) instead of
aborting the batch.  After any sticky error the CUDA context is torn down and
rebuilt so the next kernel starts clean.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
from ctypes.util import find_library
from dataclasses import dataclass, field
from pathlib import Path

# --------------------------------------------------------------------------- #
# Driver-API constants (public; from cuda.h).                                 #
# --------------------------------------------------------------------------- #
CUDA_SUCCESS = 0
CUDA_ERROR_DEINITIALIZED = 4
CUDA_ERROR_NOT_FOUND = 500
CUDA_ERROR_ILLEGAL_ADDRESS = 700
CUDA_ERROR_LAUNCH_FAILED = 719
CUDA_ERROR_LAUNCH_OUT_OF_RESOURCES = 701
CUDA_ERROR_LAUNCH_TIMEOUT = 702
CUDA_ERROR_COOPERATIVE_LAUNCH_TOO_LARGE = 720
CUDA_ERROR_INVALID_VALUE = 1

# cuLaunchKernel `extra` keys (passing the param buffer as one opaque blob).
CU_LAUNCH_PARAM_END = ctypes.c_void_p(0)
CU_LAUNCH_PARAM_BUFFER_POINTER = ctypes.c_void_p(1)
CU_LAUNCH_PARAM_BUFFER_SIZE = ctypes.c_void_p(2)

# cuFuncGetAttribute attribute ids.
CU_FUNC_ATTRIBUTE_MAX_THREADS_PER_BLOCK = 0
CU_FUNC_ATTRIBUTE_SHARED_SIZE_BYTES = 1
CU_FUNC_ATTRIBUTE_CONST_SIZE_BYTES = 2
CU_FUNC_ATTRIBUTE_NUM_REGS = 4
CU_FUNC_ATTRIBUTE_REQUIRED_CLUSTER_WIDTH = 12
CU_FUNC_ATTRIBUTE_REQUIRED_CLUSTER_HEIGHT = 13
CU_FUNC_ATTRIBUTE_REQUIRED_CLUSTER_DEPTH = 14

CUOBJDUMP = "/usr/local/cuda-13.1/bin/cuobjdump"

# Arena sizing.  Large enough that a kernel indexing by blockIdx/threadIdx with a
# small synthesized stride stays in-bounds; small enough to allocate instantly.
DEFAULT_ARENA_BYTES = 64 * 1024 * 1024     # 64 MiB zeroed device arena
DEFAULT_TIMEOUT_S = 8.0
DEFAULT_BLOCK = 32                          # one warp when nvinfo gives no hint
DEFAULT_GRID = 1


# --------------------------------------------------------------------------- #
# Parsed kernel metadata.                                                      #
# --------------------------------------------------------------------------- #
@dataclass
class Param:
    """One kernel parameter as recovered from EIATTR_KPARAM_INFO."""
    ordinal: int
    offset: int                 # byte offset within the launch param buffer
    size: int                   # bytes (4 = scalar slot, 8 = pointer/u64 slot)


@dataclass
class KernelInfo:
    """Everything needed to synthesize inputs for and launch one kernel."""
    entry: str
    param_buffer_size: int                  # CBANK_PARAM_SIZE (bytes)
    cbank_base: int                         # PARAM_CBANK base offset in cbank0
    params: list[Param] = field(default_factory=list)
    static_smem: int = 0                    # EIATTR shared-scratch / SHARED bytes
    req_block: tuple[int, int, int] | None = None      # REQNTID / MAX_THREADS
    cluster: tuple[int, int, int] | None = None        # CTA_PER_CLUSTER (if any)

    @property
    def n_pointer_slots(self) -> int:
        return sum(1 for p in self.params if p.size == 8)

    @property
    def n_scalar_slots(self) -> int:
        return sum(1 for p in self.params if p.size == 4)


@dataclass
class LaunchResult:
    """Outcome of a generic test-launch."""
    entry: str
    verdict: str                # launchable | crashed | timed_out |
                                # needs_cluster | skipped | introspect_failed
    detail: str = ""
    out_hash: str | None = None         # sha256 of the read-back arena
    grid: tuple[int, int, int] = (0, 0, 0)
    block: tuple[int, int, int] = (0, 0, 0)
    param_buffer_size: int = 0
    n_params: int = 0


# --------------------------------------------------------------------------- #
# Introspection: parse cuobjdump --dump-elf for the param-buffer layout.       #
# --------------------------------------------------------------------------- #
_KPARAM_RE = re.compile(
    r"Ordinal\s*:\s*(0x[0-9a-fA-F]+|\d+)\s+"
    r"Offset\s*:\s*(0x[0-9a-fA-F]+|\d+)\s+"
    r"Size\s*:\s*(0x[0-9a-fA-F]+|\d+)")


def _toint(s: str) -> int:
    s = s.strip()
    return int(s, 16) if s.lower().startswith("0x") else int(s)


def list_entries(cubin: str | Path) -> list[str]:
    """Every kernel entry name in the cubin (the `.text.<entry>` sections)."""
    out = subprocess.run([CUOBJDUMP, "-elf", str(cubin)],
                         capture_output=True, text=True)
    if out.returncode != 0:
        return []
    names: list[str] = []
    seen: set[str] = set()
    for m in re.finditer(r"\.text\.(\S+)", out.stdout):
        name = m.group(1)
        if name not in seen:
            seen.add(name)
            names.append(name)
    return names


def _parse_param_cbank(value_line: str) -> tuple[int, int]:
    """Decode an EIATTR_PARAM_CBANK value line.

    cuobjdump prints it as two hex words, e.g. `0x4 0x200160`.  The second word
    packs `[u16 total_bytes][u16 base_offset]` (little-endian halves of the u32):
    base_offset = low 16 bits, total_bytes = high 16 bits.  Returns
    (base_offset, total_bytes); (0, 0) if it cannot be parsed.
    """
    hexes = re.findall(r"0x[0-9a-fA-F]+", value_line)
    if len(hexes) < 2:
        return 0, 0
    packed = int(hexes[1], 16)
    base_offset = packed & 0xFFFF
    total_bytes = (packed >> 16) & 0xFFFF
    return base_offset, total_bytes


def introspect(cubin: str | Path, entry: str) -> KernelInfo | None:
    """Recover one kernel's param-buffer layout from `cuobjdump --dump-elf`.

    Returns None if the entry is not found or its `.nv.info.<entry>` carries no
    parameter records (e.g. a no-arg kernel still yields an empty-param
    KernelInfo, but a missing entry returns None).
    """
    r = subprocess.run([CUOBJDUMP, "-elf", str(cubin)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return None
    text = r.stdout

    # Isolate the per-entry `.nv.info.<entry>` attribute block.  cuobjdump prints
    # the layout twice: once as a row in the section-index table (`<idx> <off>
    # <size> ... .nv.info.<entry>`) and once as a *standalone* dump header --
    # a line that is exactly `.nv.info.<entry>` followed by the `Attribute:`
    # records.  We anchor on the standalone header (start-of-line, nothing else
    # on the line) and run the block to the next `.nv.`/`.section` header or EOF.
    hdr = f".nv.info.{entry}"
    hdr_re = re.compile(r"(?m)^" + re.escape(hdr) + r"\s*$")
    mh = hdr_re.search(text)
    if mh is None:
        # No per-entry attribute block -> a kernel with no params (or a stub).
        # Still return an empty descriptor if the entry exists at all.
        if f".text.{entry}" not in text:
            return None
        return KernelInfo(entry=entry, param_buffer_size=0, cbank_base=0)
    rest = text[mh.end():]
    nxt = re.search(r"(?m)^(?:\.nv\.\S+|\.section\b)", rest)
    block = rest[:nxt.start()] if nxt else rest

    # CBANK_PARAM_SIZE -> total param-buffer bytes.
    param_size = 0
    m = re.search(r"EIATTR_CBANK_PARAM_SIZE.*?Value:\s*(0x[0-9a-fA-F]+|\d+)",
                  block, re.S)
    if m:
        param_size = _toint(m.group(1))

    # PARAM_CBANK -> base offset (+ a second size we cross-check).
    cbank_base = 0
    m = re.search(r"EIATTR_PARAM_CBANK.*?Value:\s*([^\n]+)", block, re.S)
    if m:
        cbank_base, cb_bytes = _parse_param_cbank(m.group(1))
        if param_size == 0:
            param_size = cb_bytes

    # KPARAM_INFO -> per-parameter ordinal/offset/size.
    params: list[Param] = []
    for m in _KPARAM_RE.finditer(block):
        ordn = _toint(m.group(1))
        off = _toint(m.group(2))
        sz = _toint(m.group(3))
        params.append(Param(ordinal=ordn, offset=off, size=sz))
    params.sort(key=lambda p: p.ordinal)

    # If CBANK_PARAM_SIZE was absent, derive the buffer size from the params.
    if param_size == 0 and params:
        last = max(params, key=lambda p: p.offset)
        param_size = last.offset + last.size

    # REQNTID / MAX_THREADS -> a launch-block hint (x[,y,z]).
    req_block = None
    m = re.search(r"EIATTR_(?:REQNTID|MAX_THREADS).*?Value:\s*([^\n]+)",
                  block, re.S)
    if m:
        dims = [_toint(x) for x in re.findall(r"0x[0-9a-fA-F]+|\d+", m.group(1))]
        if dims:
            dims = (dims + [1, 1, 1])[:3]
            req_block = (max(1, dims[0]), max(1, dims[1]), max(1, dims[2]))

    # CTA_PER_CLUSTER / EXPLICIT_CLUSTER -> kernel needs a cluster launch.
    cluster = None
    m = re.search(r"EIATTR_CTA_PER_CLUSTER.*?Value:\s*([^\n]+)", block, re.S)
    if m:
        dims = [_toint(x) for x in re.findall(r"0x[0-9a-fA-F]+|\d+", m.group(1))]
        if dims:
            dims = (dims + [1, 1, 1])[:3]
            cluster = (dims[0], dims[1], dims[2])
    elif "EIATTR_EXPLICIT_CLUSTER" in block:
        cluster = (0, 0, 0)     # present but dims unknown -> still cluster-only

    return KernelInfo(entry=entry, param_buffer_size=param_size,
                      cbank_base=cbank_base, params=params,
                      req_block=req_block, cluster=cluster)


# --------------------------------------------------------------------------- #
# ctypes binding to libcuda.                                                   #
# --------------------------------------------------------------------------- #
class CudaError(RuntimeError):
    def __init__(self, code: int, what: str, msg: str = ""):
        self.code = code
        self.what = what
        super().__init__(f"{what}: CUDA error {code} ({msg})" if msg
                         else f"{what}: CUDA error {code}")


class Cuda:
    """Thin ctypes wrapper over the subset of libcuda the launcher needs.

    One instance owns one context.  `reset_context()` tears down and rebuilds it
    after a sticky device error so subsequent launches start from a clean state.
    """

    def __init__(self, device: int = 0):
        libname = (find_library("cuda") or "libcuda.so.1")
        try:
            self.lib = ctypes.CDLL(libname)
        except OSError:
            self.lib = ctypes.CDLL("libcuda.so.1")
        self._bind()
        self.device = device
        self.poisoned = False        # set True after an unrecoverable device error
        self._dev = ctypes.c_int()
        self._ctx = ctypes.c_void_p()
        self.chk(self.lib.cuInit(0), "cuInit")
        self.chk(self.lib.cuDeviceGet(ctypes.byref(self._dev), device),
                 "cuDeviceGet")
        self._make_context()

    # --- binding / prototypes ------------------------------------------------
    def _bind(self) -> None:
        L = self.lib
        L.cuInit.argtypes = [ctypes.c_uint]
        L.cuDeviceGet.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_int]
        # CUDA 13.x cuCtxCreate maps to _v4 (ctx, params, flags, dev).  Bind by
        # the versioned name to be unambiguous; fall back to the plain symbol.
        self._ctxcreate = getattr(L, "cuCtxCreate_v4", None) or L.cuCtxCreate
        self._ctxcreate.argtypes = [ctypes.POINTER(ctypes.c_void_p),
                                     ctypes.c_void_p, ctypes.c_uint,
                                     ctypes.c_int]
        L.cuCtxDestroy_v2.argtypes = [ctypes.c_void_p]
        L.cuCtxSetCurrent.argtypes = [ctypes.c_void_p]
        L.cuCtxSynchronize.argtypes = []
        L.cuModuleLoadData.argtypes = [ctypes.POINTER(ctypes.c_void_p),
                                       ctypes.c_void_p]
        L.cuModuleUnload.argtypes = [ctypes.c_void_p]
        L.cuModuleGetFunction.argtypes = [ctypes.POINTER(ctypes.c_void_p),
                                          ctypes.c_void_p, ctypes.c_char_p]
        L.cuFuncGetAttribute.argtypes = [ctypes.POINTER(ctypes.c_int),
                                         ctypes.c_int, ctypes.c_void_p]
        L.cuMemAlloc_v2.argtypes = [ctypes.POINTER(ctypes.c_ulonglong),
                                    ctypes.c_size_t]
        L.cuMemFree_v2.argtypes = [ctypes.c_ulonglong]
        L.cuMemsetD8_v2.argtypes = [ctypes.c_ulonglong, ctypes.c_ubyte,
                                    ctypes.c_size_t]
        L.cuMemcpyHtoD_v2.argtypes = [ctypes.c_ulonglong, ctypes.c_void_p,
                                      ctypes.c_size_t]
        L.cuMemcpyDtoH_v2.argtypes = [ctypes.c_void_p, ctypes.c_ulonglong,
                                      ctypes.c_size_t]
        L.cuLaunchKernel.argtypes = [
            ctypes.c_void_p,                                  # f
            ctypes.c_uint, ctypes.c_uint, ctypes.c_uint,      # gridDim
            ctypes.c_uint, ctypes.c_uint, ctypes.c_uint,      # blockDim
            ctypes.c_uint,                                    # sharedMemBytes
            ctypes.c_void_p,                                  # hStream
            ctypes.POINTER(ctypes.c_void_p),                  # kernelParams
            ctypes.POINTER(ctypes.c_void_p)]                  # extra
        L.cuGetErrorString.argtypes = [ctypes.c_int,
                                       ctypes.POINTER(ctypes.c_char_p)]

    def errstr(self, code: int) -> str:
        s = ctypes.c_char_p()
        self.lib.cuGetErrorString(code, ctypes.byref(s))
        return s.value.decode() if s.value else f"code {code}"

    def chk(self, code: int, what: str) -> None:
        if code != CUDA_SUCCESS:
            raise CudaError(code, what, self.errstr(code))

    # --- context lifecycle ---------------------------------------------------
    def _make_context(self) -> None:
        self._ctx = ctypes.c_void_p()
        self.chk(self._ctxcreate(ctypes.byref(self._ctx), None, 0,
                                 self._dev.value), "cuCtxCreate")

    def reset_context(self) -> bool:
        """Try to destroy and recreate the context after a device error.

        Returns True if a clean context was rebuilt, False if the device is
        poisoned.  An ILLEGAL_ADDRESS / LAUNCH_FAILED error is *sticky for the
        whole process* in the CUDA Driver API: even cuCtxDestroy + cuCtxCreate
        keep returning the error.  We therefore never let the rebuild raise --
        callers that need true isolation run each launch in its own subprocess
        (see `launch_isolated`), which the process model can always recover from.
        """
        try:
            if self._ctx:
                self.lib.cuCtxDestroy_v2(self._ctx)
        except Exception:
            pass
        self._ctx = ctypes.c_void_p()
        try:
            self._make_context()
            self.poisoned = False
            return True
        except CudaError:
            self.poisoned = True
            return False

    def close(self) -> None:
        try:
            if self._ctx:
                self.lib.cuCtxDestroy_v2(self._ctx)
        except Exception:
            pass
        self._ctx = ctypes.c_void_p()

    # --- module / memory -----------------------------------------------------
    def load_module(self, cubin_bytes: bytes) -> ctypes.c_void_p:
        mod = ctypes.c_void_p()
        buf = ctypes.create_string_buffer(cubin_bytes, len(cubin_bytes))
        self.chk(self.lib.cuModuleLoadData(ctypes.byref(mod),
                                           ctypes.cast(buf, ctypes.c_void_p)),
                 "cuModuleLoadData")
        return mod

    def unload_module(self, mod: ctypes.c_void_p) -> None:
        try:
            self.lib.cuModuleUnload(mod)
        except Exception:
            pass

    def get_function(self, mod: ctypes.c_void_p, name: str) -> ctypes.c_void_p:
        fn = ctypes.c_void_p()
        self.chk(self.lib.cuModuleGetFunction(ctypes.byref(fn), mod,
                                              name.encode()),
                 "cuModuleGetFunction")
        return fn

    def func_attr(self, fn: ctypes.c_void_p, attr: int) -> int:
        v = ctypes.c_int()
        rc = self.lib.cuFuncGetAttribute(ctypes.byref(v), attr, fn)
        if rc != CUDA_SUCCESS:
            return -1
        return v.value

    def mem_alloc(self, nbytes: int) -> int:
        p = ctypes.c_ulonglong()
        self.chk(self.lib.cuMemAlloc_v2(ctypes.byref(p), nbytes), "cuMemAlloc")
        return p.value

    def mem_free(self, ptr: int) -> None:
        try:
            self.lib.cuMemFree_v2(ptr)
        except Exception:
            pass

    def memset_d8(self, ptr: int, value: int, nbytes: int) -> None:
        self.chk(self.lib.cuMemsetD8_v2(ptr, value & 0xFF, nbytes),
                 "cuMemsetD8")

    def memcpy_htod(self, dst: int, host: bytes) -> None:
        buf = ctypes.create_string_buffer(host, len(host))
        self.chk(self.lib.cuMemcpyHtoD_v2(dst, ctypes.cast(buf, ctypes.c_void_p),
                                          len(host)), "cuMemcpyHtoD")

    def memcpy_dtoh(self, src: int, nbytes: int) -> bytes:
        buf = ctypes.create_string_buffer(nbytes)
        self.chk(self.lib.cuMemcpyDtoH_v2(ctypes.cast(buf, ctypes.c_void_p),
                                          src, nbytes), "cuMemcpyDtoH")
        return buf.raw

    def synchronize(self) -> None:
        self.chk(self.lib.cuCtxSynchronize(), "cuCtxSynchronize")

    def launch(self, fn: ctypes.c_void_p, grid: tuple[int, int, int],
               block: tuple[int, int, int], smem: int,
               param_buf: bytes) -> None:
        """Launch via the opaque param-buffer `extra` ABI.

        extra = {CU_LAUNCH_PARAM_BUFFER_POINTER, buf,
                 CU_LAUNCH_PARAM_BUFFER_SIZE, &size, CU_LAUNCH_PARAM_END}
        """
        cbuf = ctypes.create_string_buffer(param_buf, len(param_buf))
        size = ctypes.c_size_t(len(param_buf))
        extra = (ctypes.c_void_p * 5)(
            CU_LAUNCH_PARAM_BUFFER_POINTER,
            ctypes.cast(cbuf, ctypes.c_void_p),
            CU_LAUNCH_PARAM_BUFFER_SIZE,
            ctypes.cast(ctypes.byref(size), ctypes.c_void_p),
            CU_LAUNCH_PARAM_END)
        rc = self.lib.cuLaunchKernel(
            fn, grid[0], grid[1], grid[2], block[0], block[1], block[2],
            smem, None, None, extra)
        # keep `cbuf`/`size` alive until the driver has copied the params
        self.chk(rc, "cuLaunchKernel")
        del cbuf, size


# --------------------------------------------------------------------------- #
# Input synthesis + bounded launch.                                            #
# --------------------------------------------------------------------------- #
def build_param_buffer(ki: KernelInfo, arena_ptr: int,
                       scalar_value: int = 1) -> bytes:
    """Synthesize a param buffer for `ki`.

    8-byte slots -> arena base address (a valid, zeroed device pointer);
    4-byte slots -> `scalar_value` (a benign small count/stride);
    other sizes  -> zeroed (struct-by-value params; benign for the arena trick).
    The buffer is exactly `param_buffer_size` bytes so its layout matches the
    cbank window cuLaunchKernel expects.
    """
    size = ki.param_buffer_size
    if size <= 0:
        # Derive a minimal buffer from the params if CBANK_PARAM_SIZE was absent.
        size = max((p.offset + p.size for p in ki.params), default=0)
    buf = bytearray(size)
    for p in ki.params:
        if p.offset + p.size > len(buf):
            # Grow defensively if a param record extends past the recorded size.
            buf.extend(b"\x00" * (p.offset + p.size - len(buf)))
        if p.size == 8:
            buf[p.offset:p.offset + 8] = int(arena_ptr).to_bytes(8, "little")
        elif p.size == 4:
            buf[p.offset:p.offset + 4] = int(scalar_value).to_bytes(4, "little")
        elif p.size == 2:
            buf[p.offset:p.offset + 2] = int(scalar_value).to_bytes(2, "little")
        elif p.size == 1:
            buf[p.offset] = scalar_value & 0xFF
        # larger by-value structs are left zeroed
    return bytes(buf)


def _choose_block(ki: KernelInfo, fn_max_threads: int) -> tuple[int, int, int]:
    """Pick a launch block.  Prefer nvinfo's required/expected block; clamp to
    the function's max-threads-per-block; else a single warp."""
    if ki.req_block:
        bx, by, bz = ki.req_block
        total = bx * by * bz
        if fn_max_threads > 0 and total > fn_max_threads:
            return (min(bx, fn_max_threads), 1, 1)
        return (bx, by, bz)
    if fn_max_threads > 0:
        return (min(DEFAULT_BLOCK, fn_max_threads), 1, 1)
    return (DEFAULT_BLOCK, 1, 1)


def test_launch(cu: Cuda, cubin_bytes: bytes, ki: KernelInfo,
                arena_ptr: int, arena_bytes: int,
                grid: tuple[int, int, int] | None = None,
                block: tuple[int, int, int] | None = None,
                smem_extra: int = 0, timeout_s: float = DEFAULT_TIMEOUT_S,
                scalar_value: int = 1,
                readback_bytes: int | None = None) -> LaunchResult:
    """Introspect-driven, bounded, fault-catching test launch of one kernel.

    Re-zeroes the arena, builds the synthesized param buffer, launches with a
    watchdog, syncs, and (on success) reads the arena back and hashes it.  Any
    CUDA error or timeout becomes a verdict; the context is reset on a sticky
    error so the next kernel starts clean.
    """
    res = LaunchResult(entry=ki.entry, verdict="skipped",
                       param_buffer_size=ki.param_buffer_size,
                       n_params=len(ki.params))

    if ki.cluster is not None:
        res.verdict = "needs_cluster"
        res.detail = "kernel requires a thread-block cluster launch (cooperative)"
        return res

    mod = None
    try:
        mod = cu.load_module(cubin_bytes)
        fn = cu.get_function(mod, ki.entry)
    except CudaError as e:
        res.verdict = "crashed"
        res.detail = f"load/get_function: {e}"
        cu.unload_module(mod) if mod else None
        cu.reset_context()
        return res

    fn_max = cu.func_attr(fn, CU_FUNC_ATTRIBUTE_MAX_THREADS_PER_BLOCK)
    fn_smem = cu.func_attr(fn, CU_FUNC_ATTRIBUTE_SHARED_SIZE_BYTES)
    blk = block or _choose_block(ki, fn_max)
    grd = grid or (DEFAULT_GRID, 1, 1)
    smem = max(fn_smem, 0) + max(smem_extra, 0)
    res.grid, res.block = grd, blk

    pbuf = build_param_buffer(ki, arena_ptr, scalar_value)

    # Re-zero the arena so the read-back hash reflects only this launch's writes.
    try:
        cu.memset_d8(arena_ptr, 0, arena_bytes)
    except CudaError as e:
        res.verdict = "crashed"
        res.detail = f"arena memset: {e}"
        cu.unload_module(mod)
        cu.reset_context()
        return res

    # Watchdog: run launch+sync on a worker thread; a hang shows as a timeout.
    box: dict[str, object] = {}

    def _go():
        # CUDA contexts are per-thread in the Driver API: a freshly spawned
        # worker thread has no current context, so bind this thread to ours
        # before launching.  (cuCtxSetCurrent is idempotent and cheap.)
        try:
            cu.chk(cu.lib.cuCtxSetCurrent(cu._ctx), "cuCtxSetCurrent")
            cu.launch(fn, grd, blk, smem, pbuf)
            cu.synchronize()
            box["ok"] = True
        except CudaError as e:        # noqa: PERF203 -- caught per launch
            box["err"] = e

    t = threading.Thread(target=_go, daemon=True)
    t.start()
    t.join(timeout_s)

    if t.is_alive():
        # The launch/sync is wedged: the kernel is still running and the daemon
        # thread is blocked inside cuCtxSynchronize.  We CANNOT cleanly tear the
        # context down here -- cuCtxDestroy would itself block behind the running
        # kernel.  Mark the context poisoned and return; the only reliable
        # recovery for a wedged kernel is ending the process (which the isolated
        # subprocess model does for us -- the parent sees a timed_out verdict and
        # the OS reclaims the GPU work when the child exits).
        res.verdict = "timed_out"
        res.detail = f"no completion within {timeout_s:.1f}s (watchdog)"
        cu.poisoned = True
        return res

    if "err" in box:
        e: CudaError = box["err"]            # type: ignore[assignment]
        if e.code in (CUDA_ERROR_ILLEGAL_ADDRESS, CUDA_ERROR_LAUNCH_FAILED):
            res.verdict = "crashed"
        elif e.code == CUDA_ERROR_LAUNCH_TIMEOUT:
            res.verdict = "timed_out"
        elif e.code in (CUDA_ERROR_LAUNCH_OUT_OF_RESOURCES,
                        CUDA_ERROR_COOPERATIVE_LAUNCH_TOO_LARGE):
            res.verdict = "crashed"
        else:
            res.verdict = "crashed"
        res.detail = str(e)
        cu.unload_module(mod)
        cu.reset_context()         # any of these may leave the ctx in error state
        return res

    # Success: read back the arena (or a prefix) and hash it.
    rb = readback_bytes if readback_bytes else min(arena_bytes, 1 << 20)
    try:
        data = cu.memcpy_dtoh(arena_ptr, rb)
        res.out_hash = hashlib.sha256(data).hexdigest()
        res.verdict = "launchable"
        res.detail = (f"params={len(ki.params)} "
                      f"(ptr={ki.n_pointer_slots} scalar={ki.n_scalar_slots}) "
                      f"buf={ki.param_buffer_size}B "
                      f"grid={grd} block={blk} smem={smem}")
    except CudaError as e:
        res.verdict = "crashed"
        res.detail = f"readback: {e}"
        cu.reset_context()
    finally:
        cu.unload_module(mod)
    return res


# --------------------------------------------------------------------------- #
# High-level convenience: launch one named kernel and return its arena hash.   #
# This is the primitive the V1/V2 gate calls (same inputs -> same hash).       #
# --------------------------------------------------------------------------- #
def launch_and_hash(cu: Cuda, cubin_path: str | Path, entry: str,
                    grid: tuple[int, int, int] | None = None,
                    block: tuple[int, int, int] | None = None,
                    arena_ptr: int | None = None,
                    arena_bytes: int = DEFAULT_ARENA_BYTES,
                    scalar_value: int = 1,
                    timeout_s: float = DEFAULT_TIMEOUT_S) -> LaunchResult:
    """Introspect, synthesize inputs, launch `entry`, return the read-back hash.

    If `arena_ptr` is given the caller owns the arena (so V1 and V2 share the
    *exact* same input memory); otherwise a private arena is allocated and freed.
    """
    cubin_bytes = Path(cubin_path).read_bytes()
    ki = introspect(cubin_path, entry)
    if ki is None:
        return LaunchResult(entry=entry, verdict="introspect_failed",
                            detail="no .nv.info.<entry> / entry not found")
    own = arena_ptr is None
    if own:
        arena_ptr = cu.mem_alloc(arena_bytes)
        cu.memset_d8(arena_ptr, 0, arena_bytes)
    try:
        return test_launch(cu, cubin_bytes, ki, arena_ptr, arena_bytes,
                           grid=grid, block=block, scalar_value=scalar_value,
                           timeout_s=timeout_s)
    finally:
        if own:
            cu.mem_free(arena_ptr)


def hash_arena_for(cu: Cuda, cubin_path: str | Path, entry: str,
                   arena_ptr: int, arena_bytes: int,
                   grid: tuple[int, int, int] | None = None,
                   block: tuple[int, int, int] | None = None,
                   scalar_value: int = 1,
                   timeout_s: float = DEFAULT_TIMEOUT_S
                   ) -> tuple[str | None, LaunchResult]:
    """V1/V2-gate helper: launch `entry` from `cubin_path` into the *caller's*
    shared arena and return (hash, full LaunchResult).  V1 and V2 call this with
    the same arena_ptr / grid / block / scalar_value -> identical inputs, so a
    matching hash proves the reschedule preserved semantics."""
    cubin_bytes = Path(cubin_path).read_bytes()
    ki = introspect(cubin_path, entry)
    if ki is None:
        return None, LaunchResult(entry=entry, verdict="introspect_failed")
    r = test_launch(cu, cubin_bytes, ki, arena_ptr, arena_bytes,
                    grid=grid, block=block, scalar_value=scalar_value,
                    timeout_s=timeout_s)
    return r.out_hash, r


# Process-isolated launch.
# An illegal-address / launch-failed error is STICKY for the whole process in
# the CUDA Driver API -- even cuCtxDestroy + cuCtxCreate keep returning it. The
# only reliable recovery is a fresh process, so each kernel launches in its own
# subprocess (and the V1/V2 gate, where one variant may legitimately fault): a
# poisoning kernel kills only that child, and the parent reads its verdict + hash
# from one line of stdout. A child that segfaults / is killed by the driver shows
# up as a non-zero exit -> `crashed`.
def launch_isolated(cubin_path: str | Path, entry: str,
                    grid: tuple[int, int, int] | None = None,
                    block: tuple[int, int, int] | None = None,
                    arena_bytes: int = DEFAULT_ARENA_BYTES,
                    scalar_value: int = 1,
                    timeout_s: float = DEFAULT_TIMEOUT_S,
                    device: int = 0) -> LaunchResult:
    """Run a single introspect+launch in a fresh subprocess and parse the result.

    The V1/V2 gate uses this primitive: call it on V1 and V2 with
    identical (grid, block, scalar_value, arena_bytes) -> the synthesized inputs
    are identical, so a matching `out_hash` proves the reschedule is semantics-
    preserving.  A faulting variant returns verdict=crashed instead of poisoning
    the parent.
    """
    cubin_path = str(Path(cubin_path).resolve())
    payload = {
        "cubin": cubin_path, "entry": entry,
        "grid": list(grid) if grid else None,
        "block": list(block) if block else None,
        "arena_bytes": arena_bytes, "scalar_value": scalar_value,
        "device": device,
        # the child's own watchdog is shorter so the parent's hard kill is a
        # fallback, not the primary timeout path
        "timeout_s": timeout_s,
    }
    proc_to = timeout_s + 30.0          # generous parent-side hard ceiling
    try:
        r = subprocess.run(
            [sys.executable, __file__, "--worker", json.dumps(payload)],
            capture_output=True, text=True, timeout=proc_to)
    except subprocess.TimeoutExpired:
        return LaunchResult(entry=entry, verdict="timed_out",
                            detail=f"subprocess exceeded {proc_to:.0f}s")
    # The worker prints exactly one JSON line on stdout on a clean exit.  A
    # child killed by the driver (segfault / device-assert abort) leaves no
    # JSON and a non-zero return code -> crashed.
    line = ""
    for ln in r.stdout.splitlines():
        if ln.startswith("{") and ln.rstrip().endswith("}"):
            line = ln
    if not line:
        det = (r.stderr.strip().splitlines() or [""])[-1][:120]
        return LaunchResult(entry=entry, verdict="crashed",
                            detail=f"worker exit {r.returncode}: {det}")
    try:
        d = json.loads(line)
    except json.JSONDecodeError:
        return LaunchResult(entry=entry, verdict="crashed",
                            detail="worker produced no parseable result")
    return LaunchResult(
        entry=d.get("entry", entry), verdict=d.get("verdict", "crashed"),
        detail=d.get("detail", ""), out_hash=d.get("out_hash"),
        grid=tuple(d.get("grid", (0, 0, 0))),
        block=tuple(d.get("block", (0, 0, 0))),
        param_buffer_size=d.get("param_buffer_size", 0),
        n_params=d.get("n_params", 0))


def _worker_main(payload_json: str) -> int:
    """In-child entry: do one introspect+launch, print the result as JSON.

    Any uncaught exception still returns a JSON `crashed` line so the parent
    always has structured output for a recoverable failure; only a hard device
    abort (which terminates the process) yields no JSON.
    """
    try:
        p = json.loads(payload_json)
    except json.JSONDecodeError:
        print(json.dumps({"verdict": "crashed", "detail": "bad payload"}))
        return 2
    entry = p["entry"]
    try:
        cu = Cuda(p.get("device", 0))
    except (OSError, CudaError) as e:
        print(json.dumps({"entry": entry, "verdict": "crashed",
                          "detail": f"CUDA init: {e}"}))
        return 0
    arena = None
    try:
        ki = introspect(p["cubin"], entry)
        if ki is None:
            print(json.dumps({"entry": entry, "verdict": "introspect_failed",
                              "detail": "no .nv.info.<entry>"}))
            return 0
        arena = cu.mem_alloc(p["arena_bytes"])
        cu.memset_d8(arena, 0, p["arena_bytes"])
        cubin_bytes = Path(p["cubin"]).read_bytes()
        grid = tuple(p["grid"]) if p["grid"] else None
        block = tuple(p["block"]) if p["block"] else None
        r = test_launch(cu, cubin_bytes, ki, arena, p["arena_bytes"],
                        grid=grid, block=block,
                        scalar_value=p["scalar_value"],
                        timeout_s=p["timeout_s"])
        sys.stdout.write(json.dumps({
            "entry": r.entry, "verdict": r.verdict, "detail": r.detail,
            "out_hash": r.out_hash, "grid": list(r.grid),
            "block": list(r.block),
            "param_buffer_size": r.param_buffer_size,
            "n_params": r.n_params}) + "\n")
        sys.stdout.flush()
        if cu.poisoned:
            # A timed-out (wedged) kernel left a daemon thread blocked inside
            # cuCtxSynchronize and the context unsafe to destroy.  Skip the
            # blocking cleanup entirely and end the process now; the OS reclaims
            # the GPU work and the parent already has its verdict line.
            os._exit(0)
        return 0
    except Exception as e:                              # noqa: BLE001
        print(json.dumps({"entry": entry, "verdict": "crashed",
                          "detail": f"{type(e).__name__}: {e}"}))
        return 0
    finally:
        try:
            if arena is not None and not cu.poisoned:
                cu.mem_free(arena)
            cu.close()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# V1 vs V2 correctness gate on ARBITRARY cubins.                               #
#                                                                              #
# V1 = the cubin ptxas emitted; V2 = the same cubin with its scheduling control #
# words re-encoded by our composer (decompose -> compose -> patch the bits 105- #
# 124 back in place).  Launch BOTH on the SAME synthesized inputs and compare   #
# the read-back arena hash.  Bit-identical output proves the reschedule is      #
# hazard-safe (no ground-truth needed: V1 *is* the reference).  We launch each  #
# variant in its own subprocess so a V2 that introduces a real hazard faults    #
# cleanly (verdict=crashed) instead of poisoning the gate.                      #
# --------------------------------------------------------------------------- #
def verify_dyn_generic(cubin: str | Path, entry: str | None = None,
                       grid: tuple[int, int, int] | None = None,
                       block: tuple[int, int, int] | None = None,
                       arena_bytes: int = DEFAULT_ARENA_BYTES,
                       scalar_value: int = 1,
                       timeout_s: float = DEFAULT_TIMEOUT_S,
                       seeds: tuple[int, ...] = (1, 2, 3, 5)) -> int:
    """Recompose `entry`'s control words, launch V1 (ptxas) and V2 (composed) on
    identical synthesized inputs, and gate on a bit-identical read-back hash.

    `seeds` are the scalar-slot values used as distinct synthesized inputs: a
    reschedule must reproduce V1 for EVERY input, not match for one by luck.
    They are deliberately SMALL: a scalar slot is often a count / stride, and a
    large value would index past the arena and make V1 itself fault (the arena
    trick keeps benign inputs in-bounds).  An input where V1 faults is reported
    and skipped rather than counted against the gate.
    Returns 0 (identical for all inputs), 1 (a difference -> the recompose
    changed semantics), or 2 (could not run: no entry / V1 itself unlaunchable).
    """
    import sass_scheduler as S          # lazy: avoids an import cycle

    cubin = str(Path(cubin).resolve())
    if entry is None:
        ents = list_entries(cubin)
        entry = ents[0] if ents else None
    if entry is None:
        print("  ! could not determine an entry name", file=sys.stderr)
        return 2

    # Build V2: recompose this entry's control words and patch them in place.
    d = S.decompose(cubin)
    cr = S.compose(d.dag, d.ctrls)
    v2 = f"/tmp/sass_launch_v2_{entry}.cubin"
    npatch = S.patch_cubin(cubin, v2, entry, cr.fields)

    print("=" * 78)
    print(f"V1/V2 DYNAMIC GATE  {Path(cubin).name}  entry={entry}")
    print(f"  recomposed {npatch} control words; "
          f"V1=ptxas  V2=our-composed  (each launched isolated)")
    print(f"  inputs: {len(seeds)} synthesized scalar value(s) {list(seeds)}, "
          f"arena={arena_bytes >> 20} MiB")
    print("=" * 78)

    all_match = True
    any_run = False
    for sv in seeds:
        r1 = launch_isolated(cubin, entry, grid=grid, block=block,
                             arena_bytes=arena_bytes, scalar_value=sv,
                             timeout_s=timeout_s)
        if r1.verdict != "launchable":
            print(f"  scalar={sv:#x}: V1 not launchable ({r1.verdict}: "
                  f"{r1.detail[:60]}) -- skipping this input")
            continue
        any_run = True
        r2 = launch_isolated(v2, entry, grid=grid, block=block,
                             arena_bytes=arena_bytes, scalar_value=sv,
                             timeout_s=timeout_s)
        if r2.verdict != "launchable":
            print(f"  scalar={sv:#x}: V1 ok hash={(r1.out_hash or '?')[:16]}  "
                  f"V2 {r2.verdict} ({r2.detail[:50]})  -> HAZARD")
            all_match = False
            continue
        match = (r1.out_hash == r2.out_hash)
        print(f"  scalar={sv:#x}: V1={(r1.out_hash or '?')[:16]}  V2={(r2.out_hash or '?')[:16]}  "
              f"{'MATCH' if match else 'DIFFER'}")
        all_match = all_match and match

    print("-" * 78)
    if not any_run:
        print("  RESULT: V1 never launched (kernel needs real inputs / cluster) "
              "-- gate inconclusive")
        return 2
    if all_match:
        print("  RESULT: bit-identical for every input -> the recomposed "
              "schedule is hazard-safe.")
        return 0
    print("  RESULT: DIFFERENT output -> the recomposed schedule changed kernel "
          "semantics (a hazard).")
    return 1


def perf_diff_generic(cubin: str | Path, entry: str | None = None,
                      grid: tuple[int, int, int] | None = None,
                      block: tuple[int, int, int] | None = None,
                      arena_bytes: int = DEFAULT_ARENA_BYTES,
                      scalar_value: int = 1,
                      metric: str = "sm__cycles_active.avg",
                      pairs: int = 5, floor_pct: float = 0.1) -> int:
    """V1 vs V2 on an arbitrary cubin: bit-gate, then paired cycle measurement.

    Recompose the control words (V2), gate bit-identical against ptxas (V1) on
    synthesized inputs, then measure both under ncu interleaved (V1,V2,V1,V2,...)
    so slow GPU-clock drift cancels.  A cycle delta above `floor_pct` is a
    measured win; below it the change is within run jitter.  Designed for real
    kernels (CUTLASS): no hand-wired signature, the arena trick feeds inputs.
    """
    import sass_scheduler as S

    cubin = str(Path(cubin).resolve())
    if entry is None:
        ents = list_entries(cubin)
        entry = ents[0] if ents else None
    if entry is None:
        print("  ! could not determine an entry name", file=sys.stderr)
        return 2
    if grid is None:
        grid = (16, 1, 1)         # fill several schedulers so a stall is visible
    if block is None:
        block = (256, 1, 1)

    d = S.decompose(cubin)
    cr = S.compose(d.dag, d.ctrls)
    v2 = f"/tmp/sass_launch_perf_v2_{entry}.cubin"
    npatch = S.patch_cubin(cubin, v2, entry, cr.fields)

    print("=" * 78)
    print(f"GENERIC PERF-DIFF  {Path(cubin).name}  entry={entry}")
    print(f"  recomposed {npatch} control words; grid={grid} block={block}  "
          f"metric={metric}")
    print("=" * 78)

    # Correctness gate first (multiple synthesized inputs).
    rc = verify_dyn_generic(cubin, entry=entry, grid=grid, block=block,
                            arena_bytes=arena_bytes)
    if rc != 0:
        print("  perf-diff aborted: V2 is not bit-identical to V1 (fix the "
              "schedule before measuring).")
        return rc

    # Paired cycle measurement (interleaved so clock drift cancels).
    v1s: list[float] = []
    v2s: list[float] = []
    blocked = False
    # one warmup pair (discarded) settles the clock out of idle
    ncu_cycles_isolated(cubin, entry, metric, grid, block, arena_bytes,
                        scalar_value)
    ncu_cycles_isolated(v2, entry, metric, grid, block, arena_bytes,
                        scalar_value)
    for _ in range(max(1, pairs)):
        a, ba, _ = ncu_cycles_isolated(cubin, entry, metric, grid, block,
                                       arena_bytes, scalar_value)
        b, bb, _ = ncu_cycles_isolated(v2, entry, metric, grid, block,
                                       arena_bytes, scalar_value)
        if ba or bb:
            blocked = True
            break
        if a:
            v1s.append(a)
        if b:
            v2s.append(b)
    if blocked:
        print("  PROFILING BLOCKED (ncu counters restricted to admin users).")
        print("  Remediation: set NVreg_RestrictProfilingToAdminUsers=0 and "
              "reload the driver, or run under sudo -E.")
        return 3
    if not v1s or not v2s:
        print("  ! ncu measurement produced no cycle samples.")
        return 2
    v1s.sort(); v2s.sort()
    c1, c2 = v1s[len(v1s) // 2], v2s[len(v2s) // 2]
    delta = c1 - c2
    pct = (100.0 * delta / c1) if c1 else 0.0
    print(f"  GATE: V2 bit-identical to V1 (correctness OK)")
    print(f"  cycles V1(ptxas)={c1:,.1f}  V2(composed)={c2:,.1f}  "
          f"delta={delta:,.1f} ({pct:+.2f}%)  [floor {floor_pct:.2f}%]")
    if pct > floor_pct:
        print("  VERDICT: MEASURED WIN (our schedule is faster on this kernel)")
    elif pct < -floor_pct:
        print("  VERDICT: our schedule is SLOWER (ptxas was tighter here)")
    else:
        print("  VERDICT: no measured cycle change (within run jitter / "
              "hardware-enforced)")
    return 0


# --------------------------------------------------------------------------- #
# ncu cycle measurement around the isolated launcher (V1/V2 perf-diff on any    #
# cubin).  ncu wraps the SAME `--worker` subprocess the gate uses, so the       #
# kernel runs with the identical synthesized inputs that were just bit-compared.#
# --------------------------------------------------------------------------- #
NCU = "/usr/local/cuda-13.1/bin/ncu"
_NCU_PERM_SIGNATURES = (
    "ERR_NVGPUCTRPERM", "insufficient permissions", "access is restricted",
    "The user does not have permission", "profiling is not allowed",
)


def ncu_cycles_isolated(cubin_path: str | Path, entry: str, metric: str,
                        grid: tuple[int, int, int] | None = None,
                        block: tuple[int, int, int] | None = None,
                        arena_bytes: int = DEFAULT_ARENA_BYTES,
                        scalar_value: int = 1, device: int = 0,
                        ncu: str = NCU) -> tuple[float | None, bool, str]:
    """Profile `metric` for one launch of `entry`, ncu wrapping the worker.

    Returns (value, blocked, stderr).  `blocked` is True when GPU counters are
    restricted to admins (the caller prints the remediation and degrades).  The
    worker uses the identical synthesized inputs the bit-gate used, so the cycle
    number measures exactly the schedule that was just proven correct.
    """
    payload = json.dumps({
        "cubin": str(Path(cubin_path).resolve()), "entry": entry,
        "grid": list(grid) if grid else None,
        "block": list(block) if block else None,
        "arena_bytes": arena_bytes, "scalar_value": scalar_value,
        "device": device, "timeout_s": DEFAULT_TIMEOUT_S})
    env = dict(os.environ)
    env["LD_LIBRARY_PATH"] = "/lib64:" + env.get("LD_LIBRARY_PATH", "")
    argv = [ncu, "--metrics", metric, "--csv", "--",
            sys.executable, __file__, "--worker", payload]
    r = subprocess.run(argv, capture_output=True, text=True, env=env)
    blob = r.stderr + r.stdout
    if any(sig in blob for sig in _NCU_PERM_SIGNATURES):
        return None, True, r.stderr
    # Parse ncu's long-format CSV: rows with "Metric Name"/"Metric Value".
    import csv as _csv
    import io
    lines = [ln for ln in r.stdout.splitlines() if ln.startswith('"')]
    if not lines:
        return None, False, r.stderr
    rows = list(_csv.reader(io.StringIO("\n".join(lines))))
    hdr = rows[0]
    if "Metric Name" not in hdr or "Metric Value" not in hdr:
        return None, False, r.stderr
    ni, vi = hdr.index("Metric Name"), hdr.index("Metric Value")
    for row in rows[1:]:
        if len(row) > max(ni, vi) and row[ni] == metric:
            try:
                return float(row[vi].replace(",", "")), False, r.stderr
            except ValueError:
                return None, False, r.stderr
    return None, False, r.stderr


# --------------------------------------------------------------------------- #
# CLI: introspect + test-launch every kernel in a cubin, print verdicts.       #
# --------------------------------------------------------------------------- #
def launch_report(cubin_path: str | Path, entry: str | None = None,
                  grid: tuple[int, int, int] | None = None,
                  block: tuple[int, int, int] | None = None,
                  arena_bytes: int = DEFAULT_ARENA_BYTES,
                  scalar_value: int = 1,
                  timeout_s: float = DEFAULT_TIMEOUT_S,
                  isolated: bool = True) -> int:
    """Introspect and test-launch each kernel; print one verdict line per entry.

    Returns 0 if every kernel was `launchable`, 1 otherwise (so it can gate a
    CI/batch run).  `crashed`/`timed_out`/`needs_cluster` are *reported*, never
    fatal -- the batch continues.

    With `isolated=True` (the default) each kernel runs in its own subprocess so
    a kernel that faults (illegal address etc.) cannot poison the rest of the
    batch -- an illegal-address error is sticky for the whole process in the
    CUDA Driver API.  `isolated=False` runs everything in-process (faster, but a
    single faulting kernel ends the run at that kernel).
    """
    cubin_path = Path(cubin_path)
    entries = [entry] if entry else list_entries(cubin_path)
    if not entries:
        print(f"  ! no kernel entries found in {cubin_path.name}")
        return 1

    print("=" * 78)
    print(f"GENERIC LAUNCH REPORT  {cubin_path.name}  ({len(entries)} kernel(s))"
          f"  [{'isolated' if isolated else 'in-process'}]")
    print("=" * 78)

    tags = {"launchable": "LAUNCHABLE", "crashed": "crashed",
            "timed_out": "TIMED_OUT", "needs_cluster": "needs_cluster",
            "skipped": "skipped", "introspect_failed": "introspect_failed"}

    # Introspection never needs the GPU -- always available for the layout view.
    if isolated:
        all_ok = True
        for name in entries:
            ki = introspect(cubin_path, name)
            np = len(ki.params) if ki else 0
            bsz = ki.param_buffer_size if ki else 0
            r = launch_isolated(cubin_path, name, grid=grid, block=block,
                                arena_bytes=arena_bytes,
                                scalar_value=scalar_value, timeout_s=timeout_s)
            hsh = f"  hash={r.out_hash[:16]}" if r.out_hash else ""
            print(f"  {name:<40} {tags.get(r.verdict, r.verdict):<14} "
                  f"params={np} buf={bsz}B{hsh}")
            if r.detail:
                print(f"      {r.detail}")
            if r.verdict != "launchable":
                all_ok = False
        print("-" * 78)
        print("  (launchable = ran + read back a hash; crashed/timed_out/"
              "needs_cluster are reported, not fatal; each kernel isolated in a "
              "subprocess)")
        return 0 if all_ok else 1

    # In-process path.
    try:
        cu = Cuda(0)
    except (OSError, CudaError) as e:
        print(f"  ! CUDA init failed: {e}")
        print("    (introspection still works; only the launch needs a GPU)")
        for name in entries:
            ki = introspect(cubin_path, name)
            if ki is None:
                print(f"  {name:<40} introspect_failed")
                continue
            print(f"  {name:<40} params={len(ki.params)} "
                  f"buf={ki.param_buffer_size}B "
                  f"ptr={ki.n_pointer_slots} scalar={ki.n_scalar_slots}")
        return 1

    arena = cu.mem_alloc(arena_bytes)
    cu.memset_d8(arena, 0, arena_bytes)
    all_ok = True
    try:
        cubin_bytes = cubin_path.read_bytes()
        for name in entries:
            if cu.poisoned:
                print(f"  {name:<40} skipped         "
                      f"(device poisoned by a prior fault; use isolated mode)")
                all_ok = False
                continue
            ki = introspect(cubin_path, name)
            if ki is None:
                print(f"  {name:<40} introspect_failed")
                all_ok = False
                continue
            r = test_launch(cu, cubin_bytes, ki, arena, arena_bytes,
                            grid=grid, block=block, scalar_value=scalar_value,
                            timeout_s=timeout_s)
            hsh = f"  hash={r.out_hash[:16]}" if r.out_hash else ""
            print(f"  {name:<40} {tags.get(r.verdict, r.verdict):<14} "
                  f"params={len(ki.params)} buf={ki.param_buffer_size}B"
                  f"{hsh}")
            if r.detail:
                print(f"      {r.detail}")
            if r.verdict != "launchable":
                all_ok = False
    finally:
        if not cu.poisoned:
            cu.mem_free(arena)
        cu.close()
    print("-" * 78)
    print("  (launchable = ran + read back a hash; crashed/timed_out/"
          "needs_cluster are reported, not fatal)")
    return 0 if all_ok else 1


def _parse_dim(s: str | None) -> tuple[int, int, int] | None:
    if not s:
        return None
    parts = [int(x) for x in s.split(",")]
    parts = (parts + [1, 1, 1])[:3]
    return (parts[0], parts[1], parts[2])


def main(argv: list[str]) -> int:
    import argparse
    # Internal subprocess worker mode: `--worker <json>` runs one isolated
    # introspect+launch and prints a JSON result line.  Handled before argparse
    # so the JSON payload (which may contain dashes) is not misread as a flag.
    if len(argv) >= 3 and argv[1] == "--worker":
        return _worker_main(argv[2])

    ap = argparse.ArgumentParser(
        description="Generic cubin introspect + test-launch (Driver API)")
    ap.add_argument("cubin", help="path to a .cubin")
    ap.add_argument("--entry", help="single kernel entry (default: all)")
    ap.add_argument("--grid", help="grid dims x[,y,z] (default 1,1,1)")
    ap.add_argument("--block", help="block dims x[,y,z] (default nvinfo/32)")
    ap.add_argument("--arena", type=int, default=DEFAULT_ARENA_BYTES,
                    help="device arena bytes (default 64 MiB)")
    ap.add_argument("--scalar", type=int, default=1,
                    help="value written into 4-byte scalar param slots")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S,
                    help="per-kernel launch watchdog seconds")
    ap.add_argument("--introspect-only", action="store_true",
                    help="parse param layout only; do not launch")
    ap.add_argument("--verify-dyn", action="store_true",
                    help="V1/V2 gate: recompose this entry's control words, "
                         "launch ptxas (V1) and composed (V2) on identical "
                         "synthesized inputs, gate on a bit-identical hash")
    ap.add_argument("--in-process", action="store_true",
                    help="launch in-process (faster; a faulting kernel ends the "
                         "run there).  Default is one subprocess per kernel.")
    args = ap.parse_args(argv[1:])

    if args.verify_dyn:
        return verify_dyn_generic(args.cubin, entry=args.entry,
                                  grid=_parse_dim(args.grid),
                                  block=_parse_dim(args.block),
                                  arena_bytes=args.arena,
                                  timeout_s=args.timeout)

    if args.introspect_only:
        entries = ([args.entry] if args.entry
                   else list_entries(args.cubin))
        for name in entries:
            ki = introspect(args.cubin, name)
            if ki is None:
                print(f"{name}: introspect_failed")
                continue
            print(f"{name}: buf={ki.param_buffer_size}B base=0x{ki.cbank_base:x} "
                  f"params={len(ki.params)} "
                  f"(ptr={ki.n_pointer_slots} scalar={ki.n_scalar_slots}) "
                  f"req_block={ki.req_block} cluster={ki.cluster}")
            for p in ki.params:
                kind = "ptr/u64" if p.size == 8 else (
                    "scalar32" if p.size == 4 else f"{p.size}B")
                print(f"    ord {p.ordinal:>2}  off=0x{p.offset:<4x} "
                      f"size={p.size}  ({kind})")
        return 0

    return launch_report(args.cubin, entry=args.entry,
                         grid=_parse_dim(args.grid),
                         block=_parse_dim(args.block),
                         arena_bytes=args.arena, scalar_value=args.scalar,
                         timeout_s=args.timeout,
                         isolated=not args.in_process)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
