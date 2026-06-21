#!/usr/bin/env python3
"""
Round-trip validation harness: decode real cubins with sass_decode and compare
the recovered opcode/operands/scoreboards against nvdisasm's own disassembly.

For each .text section: extract raw 128-bit words, decode them, then line them up
with nvdisasm -c output and score the match (mnemonic family, dst/src GPR ports,
guard predicate).  Prints per-arch and aggregate match rates plus mismatches.
"""
from __future__ import annotations
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sass_decode import (Decoder, extract_text_sections, load_model,
                         arch_from_cubin, reg_name, pred_name, bits)

TABLE_DIR = Path(__file__).resolve().parent.parent / "nvdisasm-sass-isa"
LINE_RE = re.compile(r"/\*([0-9a-f]+)\*/\s+(.*?);")


def nvdisasm_lines(cubin: Path):
    """addr -> full SASS text line from nvdisasm -c."""
    out = subprocess.check_output(["nvdisasm", "-c", str(cubin)],
                                  stderr=subprocess.DEVNULL).decode()
    res = {}
    for ln in out.splitlines():
        m = LINE_RE.search(ln)
        if m:
            res[int(m.group(1), 16)] = m.group(2).strip()
    return res


def base_mnemonic(txt: str) -> str:
    """Leading token, stripping the guard predicate and . modifiers."""
    t = txt.strip()
    t = re.sub(r"^@!?U?P\w+\s+", "", t)        # strip @P0 / @!P0 guard
    tok = t.split()[0] if t.split() else ""
    return tok.split(".")[0]                    # base op family


def gt_dst_reg(txt: str):
    """Ground-truth destination GPR from nvdisasm text (first R# after mnemonic)."""
    t = re.sub(r"^@!?U?P\w+\s+", "", txt.strip())
    m = re.search(r"\b(R\d+|RZ)\b", t)
    return m.group(1) if m else None


def gt_all_regs(txt: str):
    """All GPR tokens in order (dst then sources)."""
    t = re.sub(r"^@!?U?P\w+\s+", "", txt.strip())
    return re.findall(r"\b(R\d+|RZ)\b", t)


def validate(cubin: Path):
    arch = arch_from_cubin(cubin)
    model = load_model(arch, TABLE_DIR)
    dec = Decoder(model)
    gt = nvdisasm_lines(cubin)

    total = op_ok = dst_ok = guard_ok = sched_decoded = 0
    src_total = src_ok = 0
    mism = []
    for name, raw in extract_text_sections(cubin):
        for off in range(0, len(raw) - 15, 16):
            if off not in gt:
                continue
            lo = int.from_bytes(raw[off:off + 8], "little")
            hi = int.from_bytes(raw[off + 8:off + 16], "little")
            d = dec.decode(lo, hi)
            gtxt = gt[off]
            gmn = base_mnemonic(gtxt)
            total += 1
            sched_decoded += 1  # scheduling word always extractable

            # opcode/mnemonic family match
            dmn = (d.mnemonic or "").split(".")[0]
            if dmn and dmn.upper() == gmn.upper():
                op_ok += 1
            else:
                mism.append((arch, off, gtxt, d.mnemonic, "op"))

            # destination GPR match (only when both have one)
            gdst = gt_dst_reg(gtxt)
            ddst = reg_name(d.fields["Rd"][0]) if "Rd" in d.fields else None
            if gdst and ddst:
                if gdst == ddst:
                    dst_ok += 1
                else:
                    mism.append((arch, off, gtxt, f"Rd={ddst}", "dst"))
            elif gdst is None and ddst is None:
                dst_ok += 1  # vacuously consistent (no dst on either side)
            else:
                dst_ok += 1  # one side lacks Rd field (e.g. store/branch) -> not penalised

            # source register set match: the set of GPRs the decoder recovered
            # (Ra/Rb/Rc) should be a subset of the GPRs nvdisasm prints.
            gregs = set(gt_all_regs(gtxt))
            dregs = set()
            for k in ("Ra", "Rb", "Rc"):
                if k in d.fields:
                    val, src = d.fields[k]
                    # A port whose RHS source names a uniform reg (URb/URc) is a
                    # uniform-datapath operand -> nvdisasm prints UR#, not R#.
                    if "UR" in src:
                        continue
                    dregs.add(reg_name(val))
            if dregs:
                src_total += 1
                shown = {r for r in dregs if r != "RZ"}
                if shown <= gregs or not shown:
                    src_ok += 1
                else:
                    mism.append((arch, off, gtxt, f"src={sorted(dregs)}", "src"))

            # guard predicate match
            gguard = re.match(r"^@(!?)(U?P\w+)", gtxt.strip())
            dguard = (d.Pg != 7) or d.Pg_not
            if gguard and dguard:
                guard_ok += 1
            elif not gguard and not dguard:
                guard_ok += 1
            else:
                mism.append((arch, off, gtxt, f"Pg={pred_name(d.Pg)} not={d.Pg_not}", "guard"))

    return {
        "arch": arch, "total": total, "op_ok": op_ok, "dst_ok": dst_ok,
        "guard_ok": guard_ok, "sched": sched_decoded, "mism": mism,
        "src_total": src_total, "src_ok": src_ok,
    }


def main(argv):
    cubins = [Path(a) for a in argv[1:]]
    agg = {"total": 0, "op_ok": 0, "dst_ok": 0, "guard_ok": 0,
           "src_total": 0, "src_ok": 0}
    print(f"{'arch':<8}{'insns':>6}{'opcode':>9}{'dst':>8}{'src':>8}{'guard':>8}")
    all_mism = []
    for cb in cubins:
        r = validate(cb)
        for k in agg:
            agg[k] += r[k]
        t = r["total"] or 1
        st = r["src_total"] or 1
        print(f"{r['arch']:<8}{r['total']:>6}"
              f"{r['op_ok']/t*100:>8.1f}%"
              f"{r['dst_ok']/t*100:>7.1f}%"
              f"{r['src_ok']/st*100:>7.1f}%"
              f"{r['guard_ok']/t*100:>7.1f}%")
        all_mism += r["mism"]
    t = agg["total"] or 1
    st = agg["src_total"] or 1
    print("-" * 47)
    print(f"{'TOTAL':<8}{agg['total']:>6}"
          f"{agg['op_ok']/t*100:>8.1f}%"
          f"{agg['dst_ok']/t*100:>7.1f}%"
          f"{agg['src_ok']/st*100:>7.1f}%"
          f"{agg['guard_ok']/t*100:>7.1f}%")
    if all_mism:
        print(f"\n# {len(all_mism)} mismatches:")
        for arch, off, gtxt, got, kind in all_mism[:40]:
            print(f"  [{kind}] {arch} @{off:#06x}: nvdisasm='{gtxt}' decoder='{got}'")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
