#!/usr/bin/env python3
# nvopen-tools -- SASS reverse-engineering tooling.  Built on the public CUDA
# Driver API + ptxas/nvdisasm/cuobjdump.
"""Coupled-math producer->consumer stall prober for sm_89.

Triangulates, per coupled scalar-GPR hazard pair (producer-class ->
consumer-class), three independent values:

  1. ptxas-emitted   -- the usched stall ptxas-13.1 puts on the producer of an
                        immediate (gap=1) dependent edge, decoded from the
                        control word (sass_ctrl_decode, usched bits 105-109).
  2. gpu-min         -- the GOLD STANDARD.  Load the compiled cubin on the sm_89
                        GPU, read back + hash; then PATCH the producer-edge stall
                        DOWN in the .text and re-launch, sweeping until the
                        read-back hash diverges (a stale operand is read).  The
                        smallest still-correct stall is the exact hardware
                        requirement.  gpu-min < emitted  =>  ptxas over-stalls.

PROBE SHAPE (why a chain, not a single pair)
--------------------------------------------
A lone producer->consumer pair is unobservable on a single warp: the consumer's
*other* operand is usually a fresh global load, whose memory-scoreboard wait
dwarfs and masks the producer's few-cycle coupled stall, so dropping that stall
changes nothing.  Instead each probe emits a STRICT DEPENDENT CHAIN of the
producer/consumer ops:

    r = OP(r, k);  r = OP(r, k);  r = OP(r, k);  ... ; store r

where every link reads ONLY the previous link's result (plus a long-settled
register), so ptxas emits the coupled stall with an EMPTY wait mask on every
internal edge -- nothing but that usched stall gates the next op.  A data-
dependent operand defeats constant-folding so the chain survives to SASS.  The
chain ends in a global store, so a single stale read at any link corrupts the
hash.  We patch the stall on every internal (empty-wait) edge to S at once and
sweep S down; the floor is the smallest S that still reproduces the reference.

Each launch is process-isolated (sass_launch.launch_isolated) so a racing patch
faults only its own child.  The reference is taken at the ptxas-emitted stall
(silicon runs it -> correct); a patched variant matching it bit-for-bit over a
spread of synthesized inputs is correct at that lower stall.
"""
from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import sass_ctrl_decode as CD  # noqa: E402
import sass_launch as SL       # noqa: E402

PTXAS = "/usr/local/cuda-13.1/bin/ptxas"
NVDISASM = "/usr/local/cuda-13.1/bin/nvdisasm"
ARCH = "sm_89"
TMP = Path("/tmp")

# usched_info occupies bits 105-109 of the 128-bit instruction word.
USCHED_LO = 105
USCHED_HI = 109

# Distinct non-zero scalar inputs: a racing schedule reading a stale operand
# diverges on at least one.  The combined read-back hash must match for ALL.
SEEDS = (1, 2, 3, 5, 7, 11, 13)


# --------------------------------------------------------------------------- #
# Probe-chain definition.                                                      #
# --------------------------------------------------------------------------- #
@dataclass
class Probe:
    name: str
    prod_pipe: str
    cons_pipe: str
    # PTX `body` is a strict dependent chain over %ra (input a), %rb (input b),
    # accumulating into %r and finally `mov.b32 %ro,...` for the store.  Extra
    # typed register declarations go in `setup`.
    setup: str
    body: str
    # SASS mnemonic prefixes whose internal (empty-wait) edges form the chain we
    # sweep.  edge_mnems[0] is the producer class, [-1] the consumer class.
    edge_mnems: tuple[str, ...]


def build_kernel(p: Probe) -> str:
    return f""".version 8.5
.target {ARCH}
.address_size 64
.visible .entry probe(.param .u64 pa, .param .u64 pb, .param .u64 po){{
  .reg .b32 %ra,%rb,%r,%ro,%idx;
  .reg .b64 %da,%db,%do,%off;
  .reg .pred %pp;
  .reg .f32 %fa,%fb,%f;
  {p.setup}
  ld.param.u64 %da,[pa]; cvta.to.global.u64 %da,%da;
  ld.param.u64 %db,[pb]; cvta.to.global.u64 %db,%db;
  ld.param.u64 %do,[po]; cvta.to.global.u64 %do,%do;
  mov.u32 %idx,%tid.x;
  mul.wide.u32 %off,%idx,4;
  add.s64 %da,%da,%off; add.s64 %db,%db,%off; add.s64 %do,%do,%off;
  ld.global.b32 %ra,[%da];
  ld.global.b32 %rb,[%db];
  {p.body}
  st.global.b32 [%do],%ro;
  ret;
}}
"""


def compile_probe(p: Probe) -> tuple[str, list[CD.Ctrl]] | None:
    ptx = build_kernel(p)
    src = TMP / f"cstall_{p.name}.ptx"
    cub = TMP / f"cstall_{p.name}.cubin"
    src.write_text(ptx)
    r = subprocess.run([PTXAS, "-arch", ARCH, "-O3", str(src), "-o", str(cub)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"  [{p.name}] ptxas failed: {r.stderr.strip()[:200]}")
        return None
    return str(cub), CD.disasm_cubin(str(cub))


def _dst_reg(text: str) -> str | None:
    import re
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        return None
    m = re.search(r"\bR\d+\b", parts[1])
    return m.group(0) if m else None


def _src_regs(text: str) -> set[str]:
    """Source registers of an instruction (every R# operand after the dest)."""
    import re
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        return set()
    regs = re.findall(r"\bR\d+\b", parts[1])
    return set(regs[1:])              # drop the destination (first operand)


def store_is_on_chain(ctrls: list[CD.Ctrl], edges: list[CD.Ctrl]) -> bool:
    """True if the stored value transitively depends on the swept chain edges.

    Walks the def-use chain backward from the STG's stored register: an
    instruction is "live" if it writes a register a live op reads.  If any swept
    edge is live, patching its stall can change the output -> the measurement is
    valid.  A detached store (ptxas computed the value via a parallel path) makes
    the sweep meaningless.
    """
    import re
    sweep_offs = {e.offset for e in edges}
    stg = next((c for c in ctrls if c.mnem.startswith("STG")), None)
    if stg is None:
        return False
    m = re.search(r"STG\S*\s+\[[^]]+\],\s*(R\d+)", stg.text)
    if not m:
        return False
    # Backward walk over the instructions preceding the store (in program order).
    pre = [c for c in ctrls if c.offset < stg.offset]
    live_regs = {m.group(1)}
    for c in reversed(pre):
        dst = _dst_reg(c.text)
        if dst and dst in live_regs:
            if c.offset in sweep_offs:
                return True
            live_regs.discard(dst)
            live_regs |= _src_regs(c.text)
    return False


def chain_edges(ctrls: list[CD.Ctrl], edge_mnems: tuple[str, ...]
                ) -> list[CD.Ctrl]:
    """The internal dependent edges to sweep: instructions whose mnemonic is in
    `edge_mnems`, carry no scoreboard wait (so only their usched gates the next
    op), and are followed by another chain op reading their destination.

    The trailing chain op (read by the store) is included as a producer too; the
    very first edge that still waits on the input LDG is EXCLUDED (its stall is
    masked by the memory scoreboard, not a coupled forwarding floor)."""
    mn = set(edge_mnems)
    idxs: list[int] = []
    for i, c in enumerate(ctrls):
        base = c.mnem.split(".")[0]
        if base in mn and not c.wait_sbs:
            idxs.append(i)
    return [ctrls[i] for i in idxs]


# --------------------------------------------------------------------------- #
# .text patching.                                                              #
# --------------------------------------------------------------------------- #
def _text_section(cubin: bytes) -> tuple[int, int]:
    import struct as _s
    assert cubin[:4] == b"\x7fELF" and cubin[4] == 2, "expected ELF64 cubin"
    e_shoff = _s.unpack_from("<Q", cubin, 0x28)[0]
    e_shentsize = _s.unpack_from("<H", cubin, 0x3A)[0]
    e_shnum = _s.unpack_from("<H", cubin, 0x3C)[0]
    e_shstrndx = _s.unpack_from("<H", cubin, 0x3E)[0]

    def sh(i):
        base = e_shoff + i * e_shentsize
        name, _t, _f, _a, off, size = _s.unpack_from("<IIQQQQ", cubin, base)
        return name, off, size

    strtab_off = sh(e_shstrndx)[1]

    def name_at(o):
        end = cubin.index(b"\x00", strtab_off + o)
        return cubin[strtab_off + o:end].decode("latin1")

    for i in range(e_shnum):
        nm, off, size = sh(i)
        if name_at(nm).startswith(".text."):
            return off, size
    raise RuntimeError("no .text.* section in cubin")


def _encode_usched(stall: int) -> int:
    """W-form (no group end), no yield: usched = 16 + stall for 1..11.

    We never emit usched 0: that is the DRAIN/yield enum, which tells the warp
    scheduler to PREFER ANOTHER WARP -- it gives a dependent chain *more* slack,
    not less, so it can never expose a forwarding race.  The smallest meaningful
    non-yield stall is 1 (usched 17 = issue next cycle, no group end)."""
    s = max(1, min(stall, 11))
    return 16 + s


def patch_stalls(cubin_in: str, cubin_out: str, offsets: list[int],
                 new_stall: int) -> None:
    """Rewrite the usched stall of every instruction at the given .text byte
    offsets (the nvdisasm `/*NNNN*/` addresses) to `new_stall`."""
    data = bytearray(Path(cubin_in).read_bytes())
    toff, _ = _text_section(bytes(data))
    new_u = _encode_usched(new_stall)
    mask = ((1 << (USCHED_HI - USCHED_LO + 1)) - 1) << USCHED_LO
    for ioff in offsets:
        fo = toff + ioff
        w = int.from_bytes(data[fo:fo + 16], "little")
        w = (w & ~mask) | ((new_u << USCHED_LO) & mask)
        data[fo:fo + 16] = w.to_bytes(16, "little")
    Path(cubin_out).write_bytes(bytes(data))


# --------------------------------------------------------------------------- #
# GPU launch over the seed corpus -> a stable read-back hash.                  #
# --------------------------------------------------------------------------- #
def combined_hash(cubin: str, block: int = 128) -> tuple[str | None, str]:
    import hashlib
    h = hashlib.sha256()
    for sv in SEEDS:
        r = SL.launch_isolated(cubin, "probe", block=(block, 1, 1),
                               scalar_value=sv, timeout_s=6.0)
        if r.verdict != "launchable" or not r.out_hash:
            return None, f"{r.verdict}@sv{sv}"
        h.update(bytes.fromhex(r.out_hash))
    return h.hexdigest(), "launchable"


def probe_pair(p: Probe, verbose: bool = False) -> dict:
    res = {"name": p.name, "pp": p.prod_pipe, "cp": p.cons_pipe,
           "emitted": None, "gpu_min": None, "n_edges": 0, "note": ""}
    out = compile_probe(p)
    if out is None:
        res["note"] = "compile failed"
        return res
    cub, ctrls = out
    edges = chain_edges(ctrls, p.edge_mnems)
    if len(edges) < 2:
        res["note"] = (f"chain too short ({len(edges)} {p.edge_mnems} edges) "
                       "-- folded or rescheduled")
        return res
    if not store_is_on_chain(ctrls, edges):
        res["n_edges"] = len(edges)
        res["note"] = "store detached from swept chain -- measurement invalid"
        return res
    res["n_edges"] = len(edges)
    # The representative emitted coupled stall is the modal stall over the
    # INTERIOR edges (drop the final edge: ptxas pads the one feeding the store
    # with the store's own operand-collect slot, which is not the steady-state
    # forwarding band).  Interior edges all carry an empty wait, so their stall
    # is purely the producer->consumer forwarding ptxas computed.
    interior = [e.stall for e in edges[:-1]] or [e.stall for e in edges]
    res["emitted"] = max(set(interior), key=interior.count)
    res["edge_stalls"] = [e.stall for e in edges]
    if verbose:
        for e in edges:
            print(f"      edge @{e.offset:#04x} stall={e.stall} "
                  f"wait={e.wait_sbs}  {e.text}")

    offsets = [e.offset for e in edges]
    ref, verdict = combined_hash(cub)
    if ref is None:
        res["note"] = f"V1 not launchable ({verdict})"
        return res

    # Sweep DOWN from emitted-1 to 1 (1 = minimum non-yield stall).  The floor
    # is the smallest stall that still reproduces the reference; a floor of 1
    # means no coupled stall is required for same-warp correctness (the warp
    # issue cadence alone covers the forwarding) and ptxas over-stalls fully.
    floor = res["emitted"]
    for s in range(res["emitted"] - 1, 0, -1):
        patched = TMP / f"cstall_{p.name}_s{s}.cubin"
        patch_stalls(cub, str(patched), offsets, s)
        h, _ = combined_hash(str(patched))
        if h == ref:
            floor = s
        else:
            break
    res["gpu_min"] = floor
    if floor < res["emitted"]:
        res["note"] = f"ptxas over-stalls by {res['emitted'] - floor}"
    return res


# --------------------------------------------------------------------------- #
# The coupled-pair corpus (strict dependent chains).                           #
# --------------------------------------------------------------------------- #
def _intchain(name, pp, cp, op_a, op_b, edges):
    """Two alternating INT-class ops in a strict dependent chain."""
    steps = "\n  ".join(
        (op_a if k % 2 == 0 else op_b).format(d="%r", s="%r")
        for k in range(8))
    body = ("shf.l.wrap.b32 %r,%ra,%ra,%rb;\n  " + steps +
            "\n  mov.b32 %ro,%r;")
    return Probe(name, pp, cp, "", body, edges)


def corpus() -> list[Probe]:
    P: list[Probe] = []

    # INT ALU same-pipe: SHF <-> LOP3 alternating (both FXU/INT).
    body_int = (
        "shf.l.wrap.b32 %r,%ra,%ra,%rb;\n  "
        "xor.b32 %r,%r,%rb;\n  shf.l.wrap.b32 %r,%r,%r,%rb;\n  "
        "xor.b32 %r,%r,%rb;\n  shf.l.wrap.b32 %r,%r,%r,%rb;\n  "
        "xor.b32 %r,%r,%rb;\n  shf.l.wrap.b32 %r,%r,%r,%rb;\n  "
        "xor.b32 %r,%r,%rb;\n  mov.b32 %ro,%r;")
    P.append(Probe("int_int", "INT", "INT", "", body_int, ("SHF", "LOP3")))

    # Integer multiply same-pipe: IMAD chain (r = r*rb + 1).
    body_imul = (
        "mad.lo.s32 %r,%ra,%rb,1;\n  "
        "mad.lo.s32 %r,%r,%rb,1;\n  mad.lo.s32 %r,%r,%rb,1;\n  "
        "mad.lo.s32 %r,%r,%rb,1;\n  mad.lo.s32 %r,%r,%rb,1;\n  "
        "mad.lo.s32 %r,%r,%rb,1;\n  mad.lo.s32 %r,%r,%rb,1;\n  "
        "mov.b32 %ro,%r;")
    P.append(Probe("imul_imul", "IMUL", "IMUL", "", body_imul, ("IMAD",)))

    # INT -> IMUL cross-pipe: alternate LOP3 (logic) and IMAD (multiply).
    body_int_imul = (
        "xor.b32 %r,%ra,%rb;\n  "
        "mad.lo.s32 %r,%r,%rb,1;\n  xor.b32 %r,%r,%rb;\n  "
        "mad.lo.s32 %r,%r,%rb,1;\n  xor.b32 %r,%r,%rb;\n  "
        "mad.lo.s32 %r,%r,%rb,1;\n  xor.b32 %r,%r,%rb;\n  "
        "mad.lo.s32 %r,%r,%rb,1;\n  xor.b32 %r,%r,%rb;\n  "
        "mov.b32 %ro,%r;")
    P.append(Probe("int_imul", "INT", "IMUL", "", body_int_imul,
                   ("LOP3",)))

    # IMUL -> INT cross-pipe: alternate IMAD (multiply) and LOP3 (logic).
    body_imul_int = (
        "mad.lo.s32 %r,%ra,%rb,1;\n  "
        "xor.b32 %r,%r,%rb;\n  mad.lo.s32 %r,%r,%rb,1;\n  "
        "xor.b32 %r,%r,%rb;\n  mad.lo.s32 %r,%r,%rb,1;\n  "
        "xor.b32 %r,%r,%rb;\n  mad.lo.s32 %r,%r,%rb,1;\n  "
        "xor.b32 %r,%r,%rb;\n  mov.b32 %ro,%r;")
    P.append(Probe("imul_int", "IMUL", "INT", "", body_imul_int,
                   ("IMAD", "LOP3")))

    # INT -> FMA cross-pipe: LOP3 result reinterpreted as float feeds an FADD
    # each step, then back to int (bit-reinterpret keeps the chain unfoldable).
    body_int_fma = (
        "xor.b32 %r,%ra,%rb; mov.b32 %f,%r;\n  "
        "add.f32 %f,%f,%f; mov.b32 %r,%f; xor.b32 %r,%r,%rb; mov.b32 %f,%r;\n  "
        "add.f32 %f,%f,%f; mov.b32 %r,%f; xor.b32 %r,%r,%rb; mov.b32 %f,%r;\n  "
        "add.f32 %f,%f,%f; mov.b32 %r,%f; xor.b32 %r,%r,%rb; mov.b32 %f,%r;\n  "
        "add.f32 %f,%f,%f; mov.b32 %r,%f;\n  "
        "mov.b32 %ro,%r;")
    P.append(Probe("int_fma", "INT", "FMA", "", body_int_fma, ("LOP3",)))

    # FMA same-pipe: FFMA chain  f = f*fb + fa, integer-bit-preserved store.
    body_fma = (
        "cvt.rn.f32.s32 %fa,%ra; cvt.rn.f32.s32 %fb,%rb;\n  "
        "fma.rn.f32 %f,%fa,%fb,%fa;\n  "
        "fma.rn.f32 %f,%f,%fb,%fa;\n  fma.rn.f32 %f,%f,%fb,%fa;\n  "
        "fma.rn.f32 %f,%f,%fb,%fa;\n  fma.rn.f32 %f,%f,%fb,%fa;\n  "
        "fma.rn.f32 %f,%f,%fb,%fa;\n  mov.b32 %ro,%f;")
    P.append(Probe("fma_fma", "FMA", "FMA", "", body_fma, ("FFMA",)))

    # FMA -> INT cross-pipe: FADD result feeds a LOP3 each step (bit-reinterp).
    body_fma_int = (
        "cvt.rn.f32.s32 %fa,%ra; cvt.rn.f32.s32 %fb,%rb;\n  "
        "add.f32 %f,%fa,%fb; mov.b32 %r,%f;\n  "
        "xor.b32 %r,%r,%rb; mov.b32 %f,%r; add.f32 %f,%f,%fb; mov.b32 %r,%f;\n  "
        "xor.b32 %r,%r,%rb; mov.b32 %f,%r; add.f32 %f,%f,%fb; mov.b32 %r,%f;\n  "
        "xor.b32 %r,%r,%rb; mov.b32 %f,%r; add.f32 %f,%f,%fb; mov.b32 %r,%f;\n  "
        "mov.b32 %ro,%r;")
    P.append(Probe("fma_int", "FMA", "INT", "", body_fma_int,
                   ("FADD", "LOP3")))

    # INT -> coupled int->float conversion (I2FP).  Each step:  r = r & rb (INT,
    # the producer);  I2FP r->f (the coupled convert reading the producer back-
    # to-back, the swept edge);  F2I f->r (decoupled, closes an integer loop so
    # the chain stays integer + unfoldable and the accumulator feeds the store).
    body_int_cvt = (
        "and.b32 %r,%ra,%rb;\n  "
        "cvt.rn.f32.s32 %f,%r; cvt.rzi.s32.f32 %r,%f; and.b32 %r,%r,%rb;\n  "
        "cvt.rn.f32.s32 %f,%r; cvt.rzi.s32.f32 %r,%f; and.b32 %r,%r,%rb;\n  "
        "cvt.rn.f32.s32 %f,%r; cvt.rzi.s32.f32 %r,%f; and.b32 %r,%r,%rb;\n  "
        "cvt.rn.f32.s32 %f,%r; cvt.rzi.s32.f32 %r,%f;\n  "
        "mov.b32 %ro,%r;")
    P.append(Probe("int_cvt", "INT", "CVT", "", body_int_cvt, ("I2FP",)))

    # CC / predicate producer (ISETP) gating a predicated IADD3 each step.  The
    # predicate is recomputed from the running accumulator so the ISETP cannot
    # be hoisted out of the loop, and the guarded add keeps it on the chain.
    body_cc = (
        "and.b32 %r,%ra,%rb;\n  "
        "setp.lt.s32 %pp,%r,%rb; @%pp add.s32 %r,%r,%rb; @!%pp xor.b32 %r,%r,%rb;\n  "
        "setp.lt.s32 %pp,%r,%rb; @%pp add.s32 %r,%r,%rb; @!%pp xor.b32 %r,%r,%rb;\n  "
        "setp.lt.s32 %pp,%r,%rb; @%pp add.s32 %r,%r,%rb; @!%pp xor.b32 %r,%r,%rb;\n  "
        "setp.lt.s32 %pp,%r,%rb; @%pp add.s32 %r,%r,%rb; @!%pp xor.b32 %r,%r,%rb;\n  "
        "mov.b32 %ro,%r;")
    P.append(Probe("cc_int", "CC", "INT", "", body_cc, ("ISETP",)))

    return P


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if not a.startswith("-")]
    verbose = "-v" in argv
    only = args[0] if args else None
    print("=" * 78)
    print("COUPLED-MATH STALL TRIANGULATION  arch=sm_89  (ptxas-13.1 + GPU min)")
    print("=" * 78)
    print(f"{'pair':<12}{'pp':<7}{'cp':<6}{'edges':>6}{'emitted':>9}"
          f"{'gpu_min':>9}  note")
    print("-" * 78)
    rows = []
    for p in corpus():
        if only and p.name != only:
            continue
        r = probe_pair(p, verbose=verbose)
        rows.append(r)
        em = "" if r["emitted"] is None else str(r["emitted"])
        gm = "" if r["gpu_min"] is None else str(r["gpu_min"])
        print(f"{r['name']:<12}{r['pp']:<7}{r['cp']:<6}{r['n_edges']:>6}"
              f"{em:>9}{gm:>9}  {r['note']}")
    print("-" * 78)
    over = [r for r in rows if r["gpu_min"] is not None
            and r["emitted"] is not None and r["gpu_min"] < r["emitted"]]
    print(f"  pairs probed: {len(rows)}  ptxas over-stalls: {len(over)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
