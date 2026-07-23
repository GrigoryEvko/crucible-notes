#!/usr/bin/env python3
"""Per-arch instruction-selection probe for ptxas (sm_100/103/110/120/121).

Reverse-engineering aid for documenting how ptxas lowers a fixed block of PTX
to SASS differently per SM target. This is a *behavioural* extractor: it drives
the freely-distributed ptxas + nvdisasm on a fixed probe and records the
uncopyrightable facts (which SASS opcode class ptxas selects for each PTX op,
per arch). No NVIDIA source or data table is read or redistributed; only the
observable input->output mapping is recorded. DMCA 17 U.S.C. 1201(f).

Usage:
    python3 extract_per_arch_isel.py /path/to/ptxas /path/to/nvdisasm [outdir]

The probe PTX is embedded below so the run is fully self-contained and
reproducible from any CUDA 13.x install that supports the Blackwell/Jetson
targets (sm_100..sm_121).
"""
from __future__ import annotations

import collections
import hashlib
import os
import re
import subprocess
import sys
import tempfile

# A fixed PTX program exercising the FP64 / FP32 / integer / half paths whose
# lowering is known to differ across the Blackwell-generation targets. Kept in
# .version 8.3 / .target sm_90 so a single source assembles for every target.
PROBE_PTX = r"""
.version 8.3
.target sm_90
.address_size 64
.visible .entry kall(.param .u64 p0, .param .u64 p1)
{
    .reg .b64 %rd<20>;
    .reg .f64 %fd<20>;
    .reg .f32 %f<30>;
    .reg .b32 %r<40>;
    .reg .b16 %h<20>;
    ld.param.u64 %rd1, [p0];
    ld.param.u64 %rd2, [p1];

    ld.f64 %fd1,[%rd1]; ld.f64 %fd2,[%rd1+8]; ld.f64 %fd3,[%rd1+16];
    fma.rn.f64 %fd4,%fd1,%fd2,%fd3;
    add.f64 %fd5,%fd1,%fd2;
    mul.f64 %fd6,%fd1,%fd2;
    div.rn.f64 %fd7,%fd1,%fd2;
    sqrt.rn.f64 %fd8,%fd1;
    min.f64 %fd9,%fd1,%fd2;
    max.f64 %fd10,%fd1,%fd2;
    st.f64 [%rd2],%fd4; st.f64 [%rd2+8],%fd5; st.f64 [%rd2+16],%fd6;
    st.f64 [%rd2+24],%fd7; st.f64 [%rd2+32],%fd8; st.f64 [%rd2+40],%fd9; st.f64 [%rd2+48],%fd10;

    ld.f32 %f1,[%rd1+64]; ld.f32 %f2,[%rd1+68]; ld.f32 %f3,[%rd1+72];
    fma.rn.f32 %f4,%f1,%f2,%f3;
    div.rn.f32 %f5,%f1,%f2;
    sqrt.rn.f32 %f6,%f1;
    rsqrt.approx.f32 %f7,%f1;
    sin.approx.f32 %f8,%f1;
    lg2.approx.f32 %f9,%f1;
    st.f32 [%rd2+64],%f4; st.f32 [%rd2+68],%f5; st.f32 [%rd2+72],%f6;
    st.f32 [%rd2+76],%f7; st.f32 [%rd2+80],%f8; st.f32 [%rd2+84],%f9;

    ld.s32 %r1,[%rd1+128]; ld.s32 %r2,[%rd1+132]; ld.s32 %r3,[%rd1+136];
    mad.lo.s32 %r4,%r1,%r2,%r3;
    mul.lo.s32 %r5,%r1,%r2;
    mul.hi.s32 %r6,%r1,%r2;
    div.s32 %r7,%r1,%r2;
    rem.s32 %r8,%r1,%r2;
    popc.b32 %r9,%r1;
    bfind.u32 %r10,%r1;
    mov.b32 %r11,%r1;
    add.s32 %r12,%r1,%r2;
    min.s32 %r13,%r1,%r2;
    st.s32 [%rd2+128],%r4; st.s32 [%rd2+132],%r5; st.s32 [%rd2+136],%r6;
    st.s32 [%rd2+140],%r7; st.s32 [%rd2+144],%r8; st.s32 [%rd2+148],%r9;
    st.s32 [%rd2+152],%r10; st.s32 [%rd2+156],%r11; st.s32 [%rd2+160],%r12; st.s32 [%rd2+164],%r13;

    ld.b16 %h1,[%rd1+192]; ld.b16 %h2,[%rd1+194];
    add.f16 %h3,%h1,%h2;
    mul.f16 %h4,%h1,%h2;
    st.b16 [%rd2+192],%h3; st.b16 [%rd2+194],%h4;
    ret;
}
"""

# Targets to probe. base/a/f variants of the same family alias to one another at
# the SASS level (verified), so the family base is the meaningful axis.
TARGETS = ["sm_90a", "sm_100", "sm_103", "sm_110", "sm_120", "sm_121"]

# Matches the disassembly line + (optionally) its trailing encoding word emitted
# by `nvdisasm -c [-hex]`, e.g.  /*0080*/ IADD3 R11, ... ;  /* 0x...7210 */
_LINE = re.compile(r"/\*[0-9a-f]{4}\*/\s*(?P<insn>[^;]+);")
_PRED = re.compile(r"^@?!?P[0-9T]*\s*")
_ENC = re.compile(r"/\* (0x[0-9a-f]{16}) \*/")


def run(cmd: list[str]) -> tuple[int, str, str]:
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


def disasm_lines(nvdisasm: str, cubin: str) -> list[str]:
    rc, out, _ = run([nvdisasm, "-c", "-hex", cubin])
    if rc != 0:
        rc, out, _ = run([nvdisasm, "-c", cubin])
    return out.splitlines()


def opcode_root(insn: str) -> str:
    """Mnemonic root of a SASS instruction (strips predicate + modifiers)."""
    body = _PRED.sub("", insn.strip())
    m = re.match(r"([A-Z][A-Z0-9]*)", body)
    return m.group(1) if m else body.split()[0] if body.split() else "?"


def opcode_full(insn: str) -> str:
    body = _PRED.sub("", insn.strip())
    m = re.match(r"([A-Z][A-Z0-9]*(?:\.[A-Z0-9_]+)*)", body)
    return m.group(1) if m else body


def analyse(lines: list[str]) -> dict:
    insns: list[str] = []
    encs: list[str] = []
    for ln in lines:
        m = _LINE.search(ln)
        if not m:
            continue
        insns.append(m.group("insn").strip())
        e = _ENC.search(ln)
        encs.append(e.group(1) if e else "")
    roots = collections.Counter(opcode_root(i) for i in insns)
    fulls = collections.Counter(opcode_full(i) for i in insns)
    # low encoding byte == SASS primary opcode field
    enc_ops = collections.Counter(
        e[-2:] for e in encs if e
    )
    canon = "\n".join(insns)
    return {
        "n_insn": len(insns),
        "roots": roots,
        "fulls": fulls,
        "enc_ops": enc_ops,
        "sass_sha": hashlib.sha256(canon.encode()).hexdigest()[:16],
    }


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    ptxas, nvdisasm = sys.argv[1], sys.argv[2]
    outdir = sys.argv[3] if len(sys.argv) > 3 else "."
    os.makedirs(outdir, exist_ok=True)

    with tempfile.TemporaryDirectory() as td:
        ptx = os.path.join(td, "probe.ptx")
        with open(ptx, "w") as fh:
            fh.write(PROBE_PTX)

        results: dict[str, dict] = {}
        for tgt in TARGETS:
            cubin = os.path.join(td, f"{tgt}.cubin")
            rc, _, err = run([ptxas, f"-arch={tgt}", "-O3", ptx, "-o", cubin])
            if rc != 0:
                print(f"# {tgt}: ptxas failed: {err.strip().splitlines()[-1:]}")
                continue
            results[tgt] = analyse(disasm_lines(nvdisasm, cubin))

        # Per-arch root-opcode histogram TSV.
        all_roots = sorted({r for res in results.values() for r in res["roots"]})
        with open(os.path.join(outdir, "per_arch_opcode_histogram.tsv"), "w") as fh:
            fh.write("sass_opcode\t" + "\t".join(results) + "\n")
            for op in all_roots:
                fh.write(op + "\t" + "\t".join(
                    str(results[t]["roots"].get(op, 0)) for t in results) + "\n")

        # Per-arch encoding primary-opcode-byte histogram TSV.
        all_enc = sorted({e for res in results.values() for e in res["enc_ops"]})
        with open(os.path.join(outdir, "per_arch_encoding_opbyte.tsv"), "w") as fh:
            fh.write("enc_op_byte\t" + "\t".join(results) + "\n")
            for e in all_enc:
                fh.write("0x" + e + "\t" + "\t".join(
                    str(results[t]["enc_ops"].get(e, 0)) for t in results) + "\n")

        # Summary.
        print("target\tn_insn\tsass_sha16\tIMAD.MOV*\tMOV\tVIADD*\tIADD3\tNOP")
        for t, r in results.items():
            f = r["fulls"]
            imad_mov = sum(v for k, v in f.items() if k.startswith("IMAD.MOV"))
            mov = sum(v for k, v in f.items() if k == "MOV" or k.startswith("MOV."))
            viadd = sum(v for k, v in f.items() if k.startswith("VIADD") or k.startswith("VIMNMX"))
            iadd3 = sum(v for k, v in f.items() if k.startswith("IADD3"))
            nop = r["roots"].get("NOP", 0)
            print(f"{t}\t{r['n_insn']}\t{r['sass_sha']}\t{imad_mov}\t{mov}\t{viadd}\t{iadd3}\t{nop}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
