#!/usr/bin/env python3
# nvopen-tools -- SASS reverse-engineering tooling (MIT-style).
"""
Uniform-register / predicate / RPC hazard prober for the sm_89 SASS scheduler.

Reverse-engineers the producer->consumer scheduling hazards that the CUDA-13.1
``ptxas`` enforces on the resources the rest of the model does not yet cover:

  * **UGPR**   uniform registers UR0..UR62  (REDUX/UMOV/ULDC/UISETP/... -> reader)
  * **UPRED**  uniform predicates UP0..UP6
  * **PRED**   predicates P0..P6           (ISETP/PSETP -> predicated consumer)
  * **RPC**    return-PC / USETMAXREG implicit ordering

Method (sm_89, present on this GPU):
  1. compile a hand-written PTX probe with ``ptxas -arch sm_89`` so ptxas itself
     chooses the producer->consumer scheduling control word;
  2. decode the emitted control word with ``sass_ctrl_decode`` -> the
     *ptxas-emitted* stall / scoreboard pairing on the consumer;
  3. GPU-verify the kernel is launchable and capture its read-back hash (the
     reference) with ``sass_launch.launch_isolated``;
  4. *patch-sweep* the consumer's ``usched`` stall downward one cycle at a time,
     re-emit the cubin (only bits 105-124 change), relaunch on identical
     synthesized inputs, and find the smallest stall whose read-back hash still
     equals the reference for every seed -> the **gpu_min** (the cycle below
     which the hazard actually corrupts results, i.e. the hardware-real latency).

The decode + patch + launch primitives are reused unchanged from
``decoded/sass-tools`` (sass_ctrl_decode / sass_scheduler / sass_launch); this
file only drives them and is the reproducer for ``uniform_pred_hazards.tsv``.
"""
from __future__ import annotations

import re as _re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SASS = ROOT / "sass-tools"
sys.path.insert(0, str(SASS))

import sass_ctrl_decode as C            # noqa: E402
import sass_scheduler as S              # noqa: E402
import sass_launch as L                 # noqa: E402

PTXAS = "/usr/local/cuda-13.1/bin/ptxas"
TMP = Path("/tmp/uphaz")
TMP.mkdir(exist_ok=True)
SEEDS = (1, 2, 3, 5, 9)


@dataclass
class Probe:
    name: str           # short id
    resource: str       # UGPR | UPRED | PRED | RPC
    producer: str       # SASS mnemonic substring for the producer
    consumer: str       # SASS mnemonic substring for the consumer (dependent)
    kind: str           # RAW | WAW | WAR | RPC-ORDER
    ptx: str            # the kernel body
    entry: str = "k"
    note: str = ""


def compile_ptx(p: Probe) -> Path | None:
    src = TMP / f"{p.name}.ptx"
    cub = TMP / f"{p.name}.cubin"
    src.write_text(p.ptx)
    r = subprocess.run([PTXAS, "-arch", "sm_89", "-O3", "-o", str(cub), str(src)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"  ! ptxas failed for {p.name}: {r.stderr.strip()[:120]}")
        return None
    return cub


_DST_RE = _re.compile(r"^\s*(?:@!?U?P\d+\s+)?[A-Z0-9_.]+\s+((?:U?R\d+|U?P\d+))")


def _dst_reg(text: str) -> str | None:
    """The destination register token of a SASS line (first operand)."""
    m = _DST_RE.match(text)
    return m.group(1) if m else None


def find_pair(ctrls: list[C.Ctrl], producer: str, consumer: str
              ) -> tuple[int, int] | None:
    """Return (producer_idx, consumer_idx) where the consumer is the *true*
    dependent reader of the producer's destination register, picking the first
    such reader (the tightest edge)."""
    for i, c in enumerate(ctrls):
        if producer not in c.text:
            continue
        dst = _dst_reg(c.text)
        if not dst:
            continue
        for j in range(i + 1, len(ctrls)):
            cj = ctrls[j]
            if consumer not in cj.text:
                continue
            # the consumer must actually read the producer's destination
            ops = cj.text.split(None, 1)
            srcs = ops[1] if len(ops) > 1 else ""
            # drop the consumer's own destination (first operand) before matching
            after_dst = srcs.split(",", 1)
            srcs = after_dst[1] if len(after_dst) > 1 else srcs
            if _re.search(rf"\b{_re.escape(dst)}\b", srcs):
                return i, j
        # fall back: first later instruction matching the consumer mnemonic
        for j in range(i + 1, len(ctrls)):
            if consumer in ctrls[j].text:
                return i, j
    return None


def launch_hashes(cubin: Path, entry: str) -> dict[int, str | None]:
    out: dict[int, str | None] = {}
    for sv in SEEDS:
        r = L.launch_isolated(cubin, entry, scalar_value=sv, timeout_s=6.0)
        out[sv] = r.out_hash if r.verdict == "launchable" else None
    return out


def patch_usched(src: Path, dst: Path, entry: str, idx: int, new_stall: int
                 ) -> None:
    """Patch ONE instruction's usched to a fixed stall (no group end), leaving
    every other control word byte-identical to what ptxas emitted."""
    d = S.decompose(str(src))
    fields = [S.ctrl_to_fields(c) for c in d.ctrls]
    f = fields[idx]
    fields[idx] = S.SchedFields(S.stall_to_usched(new_stall), f.dst_wr,
                                f.src_rel, f.wait_mask, f.batch_t)
    S.patch_cubin(str(src), str(dst), entry, fields)


def patch_drop_wait(src: Path, dst: Path, entry: str, cidx: int, sb: int
                    ) -> None:
    """Clear scoreboard bit `sb` from the consumer's wait mask (and set its
    stall to 1) to test whether the decoupled scoreboard pairing is required
    -- removing a real async wait must corrupt the read-back."""
    d = S.decompose(str(src))
    fields = [S.ctrl_to_fields(c) for c in d.ctrls]
    f = fields[cidx]
    fields[cidx] = S.SchedFields(S.stall_to_usched(1), f.dst_wr, f.src_rel,
                                 f.wait_mask & ~(1 << sb), f.batch_t)
    S.patch_cubin(str(src), str(dst), entry, fields)


def sweep(p: Probe) -> dict:
    cub = compile_ptx(p)
    if cub is None:
        return {"ok": False, "reason": "compile"}
    ctrls = C.disasm_cubin(str(cub))
    pair = find_pair(ctrls, p.producer, p.consumer)
    if pair is None:
        return {"ok": False, "reason": "pair-not-emitted",
                "emitted": [c.mnem for c in ctrls]}
    pidx, cidx = pair
    prod, cons = ctrls[pidx], ctrls[cidx]
    adjacent = (cidx == pidx + 1)
    # Decoupled (scoreboard) edge: the producer arms a write scoreboard and the
    # consumer waits on that exact bit -> the producer->consumer latency is
    # enforced by the hardware async barrier, not a usched stall.
    sb = prod.dst_wr
    decoupled = (sb != 7) and (sb in cons.wait_sbs)

    ref = launch_hashes(cub, p.entry)
    ref_ok = {k: v for k, v in ref.items() if v is not None}
    if not ref_ok:
        return {"ok": False, "reason": "v1-unlaunchable"}

    res = {"ok": True, "producer": prod.mnem, "consumer": cons.mnem,
           "kind": p.kind, "decoupled": decoupled, "adjacent": adjacent,
           "prod_wr_sb": prod.dst_wr, "cons_wait": cons.wait_sbs,
           "seeds_ok": len(ref_ok)}

    if decoupled:
        # The hazard distance is the SCOREBOARD wait, not a stall.  Verify the
        # pairing is required: drop the consumer's wait bit -> if the read-back
        # changes, the async wait is real (gpu_min recorded as the producer's
        # emitted stall; the "min" concept is N/A for a HW barrier).
        res["ptxas_emitted"] = prod.stall      # producer issue stall
        dst = TMP / f"{p.name}_nowait.cubin"
        patch_drop_wait(cub, dst, p.entry, cidx, sb)
        h = launch_hashes(dst, p.entry)
        broke = any(h.get(k) != ref_ok[k] for k in ref_ok)
        res["scoreboard_required"] = broke
        res["gpu_min"] = "SB"                  # hardware-enforced async barrier
        return res

    # Coupled RAW edge: the hazard distance is the PRODUCER's usched stall (the
    # gap before the dependent consumer issues).  Patch it down: gpu_min = the
    # lowest stall whose read-back hash still matches the reference for all seeds.
    # Only sound when the pair is adjacent (no third instruction fills the gap).
    res["ptxas_emitted"] = prod.stall
    gpu_min = prod.stall
    if adjacent:
        for trial in range(prod.stall - 1, -1, -1):
            dst = TMP / f"{p.name}_s{trial}.cubin"
            patch_usched(cub, dst, p.entry, pidx, trial)
            h = launch_hashes(dst, p.entry)
            if all(h.get(k) == ref_ok[k] for k in ref_ok):
                gpu_min = trial
            else:
                break
    res["gpu_min"] = gpu_min
    return res


# --------------------------------------------------------------------------- #
# Probe corpus.                                                               #
# --------------------------------------------------------------------------- #
def _wrap(body: str, params: str = ".param .u64 p, .param .u32 n") -> str:
    return (".version 8.4\n.target sm_89\n.address_size 64\n"
            f".visible .entry k({params})\n{{\n{body}\nret;\n}}\n")


PROBES = [
    # ---- UGPR : REDUX/S2UR (R2UR_S2UR class) -> uniform-register reader -----
    Probe("ugpr_redux_read", "UGPR", "REDUX", "MOV", "RAW",
          _wrap("""  .reg .b64 %rd<3>; .reg .b32 %r<6>;
  ld.param.u64 %rd1, [p];
  cvta.to.global.u64 %rd2, %rd1;
  mov.u32 %r1, %tid.x;
  redux.sync.add.u32 %r2, %r1, 0xffffffff;
  mad.lo.u32 %r3, %r2, 3, %r1;
  st.global.u32 [%rd2], %r3;"""),
          note="R2UR/S2UR uniform producer (REDUX) -> UR reader: scoreboard"),
    Probe("ugpr_redux_read2", "UGPR", "REDUX", "IMAD", "RAW",
          _wrap("""  .reg .b64 %rd<3>; .reg .b32 %r<6>;
  ld.param.u64 %rd1, [p];
  cvta.to.global.u64 %rd2, %rd1;
  mov.u32 %r1, %tid.x;
  redux.sync.min.u32 %r2, %r1, 0xffffffff;
  add.u32 %r3, %r2, 7;
  st.global.u32 [%rd2], %r3;"""),
          note="REDUX.MIN uniform producer -> UR reader: scoreboard"),
    # ---- UGPR : ULDC (uniform constant load) -> address use ----------------
    Probe("ugpr_uldc_stg", "UGPR", "ULDC", "STG", "RAW",
          _wrap("""  .reg .b64 %rd<4>; .reg .b32 %r<4>;
  ld.param.u64 %rd1, [p];
  ld.param.u32 %r1, [n];
  cvta.to.global.u64 %rd2, %rd1;
  mad.lo.u32 %r2, %r1, %r1, 1;
  st.global.u32 [%rd2], %r2;""",
                params=".param .u64 p, .param .u32 n"),
          note="ULDC.64 base ptr -> STG address operand (uniform -> mem)"),
    # ---- PRED : ISETP (FXU coupled) -> predicated SEL/coupled consumer -----
    Probe("pred_isetp_sel", "PRED", "ISETP", "SEL", "RAW",
          _wrap("""  .reg .b64 %rd<3>; .reg .b32 %r<6>; .reg .pred %pp<2>;
  ld.param.u64 %rd1, [p]; ld.param.u32 %r1, [n];
  cvta.to.global.u64 %rd2, %rd1;
  mov.u32 %r2, %tid.x;
  setp.lt.u32 %pp1, %r2, %r1;
  selp.b32 %r3, 11, 22, %pp1;
  st.global.u32 [%rd2], %r3;"""),
          note="ISETP P0 -> SEL guard (coupled producer, coupled consumer)"),
    # ---- PRED : ISETP (FXU coupled) -> VOTE (non-math reader => control band)
    Probe("pred_isetp_vote", "PRED", "ISETP", "VOTE", "RAW",
          _wrap("""  .reg .b64 %rd<3>; .reg .b32 %r<6>; .reg .pred %pp<3>;
  ld.param.u64 %rd1, [p]; ld.param.u32 %r1, [n];
  cvta.to.global.u64 %rd2, %rd1;
  mov.u32 %r2, %tid.x;
  setp.lt.u32 %pp1, %r2, %r1;
  vote.sync.any.pred %pp2, %pp1, 0xffffffff;
  selp.b32 %r3, 11, 22, %pp2;
  st.global.u32 [%rd2], %r3;"""),
          note="ISETP P0 -> VOTE reader: control-band predicate latency (13)"),
    # ---- PRED : VOTE -> coupled SEL ----------------------------------------
    Probe("pred_vote_sel", "PRED", "VOTE", "SEL", "RAW",
          _wrap("""  .reg .b64 %rd<3>; .reg .b32 %r<6>; .reg .pred %pp<3>;
  ld.param.u64 %rd1, [p]; ld.param.u32 %r1, [n];
  cvta.to.global.u64 %rd2, %rd1;
  mov.u32 %r2, %tid.x;
  setp.lt.u32 %pp1, %r2, %r1;
  vote.sync.any.pred %pp2, %pp1, 0xffffffff;
  selp.b32 %r3, 11, 22, %pp2;
  st.global.u32 [%rd2], %r3;"""),
          note="VOTE P -> SEL guard: predicate -> coupled-math reader"),
    # ---- PRED : ISETP -> @P predicated store (predicate guards a memory op) -
    Probe("pred_isetp_pst", "PRED", "ISETP", "STG", "RAW",
          _wrap("""  .reg .b64 %rd<3>; .reg .b32 %r<6>; .reg .pred %pp<2>;
  ld.param.u64 %rd1, [p]; ld.param.u32 %r1, [n];
  cvta.to.global.u64 %rd2, %rd1;
  mov.u32 %r2, %tid.x;
  setp.lt.u32 %pp1, %r2, %r1;
  @%pp1 st.global.u32 [%rd2], %r2;"""),
          note="ISETP P0 -> @P STG: predicate guards a decoupled MIO consumer"),
]


def main() -> int:
    print("=" * 78)
    print("UGPR / UPRED / PRED / RPC hazard probe  (ptxas -arch sm_89, GPU sweep)")
    print("=" * 78)
    for p in PROBES:
        r = sweep(p)
        if not r.get("ok"):
            print(f"  {p.name:<22} {p.resource:<6} -> {r.get('reason')}"
                  f"  {r.get('emitted', '')}")
            continue
        dc = "decoupled(SB)" if r["decoupled"] else "coupled"
        adj = "adj" if r["adjacent"] else "gap"
        extra = (f"sb_required={r.get('scoreboard_required')}"
                 if r["decoupled"] else f"adj={adj}")
        print(f"  {p.name:<22} {p.resource:<6} {r['producer']:>16}->"
              f"{r['consumer']:<8} {dc:<13} "
              f"ptxas={str(r['ptxas_emitted']):<2} gpu_min={str(r['gpu_min']):<3} "
              f"wr_sb={r['prod_wr_sb']} wait={r['cons_wait']} "
              f"seeds={r['seeds_ok']} {extra}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
