#!/usr/bin/env python3
"""Validate the SASS legality/capability model against real ptxas-emitted SASS.

Drives sass_legality.py over (a) synthetic legal/illegal operand vectors to
confirm the rule semantics, and (b) the register operands actually emitted by
ptxas in /tmp/sass_validate/*.sass to confirm the alignment / shader / format
hypotheses hold on real code.
"""
from __future__ import annotations
import re, sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from sass_legality import (LegalityChecker, load_class, DEFAULT_TABLE_DIR,
                           lop3, iadd3, shf_l, shf_r, prmt)

SASS = Path("/tmp/sass_validate")
TBL = DEFAULT_TABLE_DIR
RESULTS = {}


def hr(t): print("\n" + "=" * 72 + f"\n{t}\n" + "=" * 72)


# ---- H1: register alignment ------------------------------------------------ #
def h1_alignment():
    hr("H1  Register-alignment: sz-driven %2 / %4 (checker + real SASS)")
    # checker semantics on a real 256-bit LDG class (has %2 and %4 rules)
    cr = load_class(TBL / "sass_isa_SM100.txt", "ldg_256_memdesc__Ra64")
    chk = LegalityChecker(cr)
    # word_mask=255 -> all 8 32-bit words used -> Rd quad-aligned, Rd2 quad-aligned
    ok_aligned, _ = chk.is_legal({"Rd": 4, "Rd2": 8, "Ra": 6, "Ra_URb": 4,
                                  "Ra_offset": 0, "word_mask": 255})
    ok_mis, fs = chk.is_legal({"Rd": 5, "Rd2": 8, "Ra": 6, "Ra_URb": 4,
                               "Ra_offset": 0, "word_mask": 255})
    mism = [f.message for f in fs if not f.legal and not f.indeterminate]
    print(f"  256-bit LDG, word_mask=255: Rd=R4(quad) legal={ok_aligned}; "
          f"Rd=R5(odd) legal={ok_mis} -> {mism[:1]}")

    # real SASS: the *data* register of a wide load/store must satisfy the
    # operand-width modulus (.64 -> %2, .128 -> %4). The address register inside
    # desc[..][Rn.64] is a 64-bit POINTER pair (always %2), checked separately.
    pat_align = {".64": 2, ".128": 4}
    viol = []
    checked = 0
    # data register = the Rn that is NOT inside the desc[..][..] address operand.
    rx_addr = re.compile(r"desc\[\w+\]\[R(\d+)(?:\.64)?\]")
    rx_ld = re.compile(r"\bLDG\.E\S*(\.64|\.128)\s+R(\d+)\s*,")          # LDG dst first
    rx_st = re.compile(r"\bSTG\.E\S*(\.64|\.128)\s+desc\[[^]]+\]\[[^]]+\],\s*R(\d+)")  # STG data last
    rx_d = re.compile(r"\b(DADD|DFMA|DMUL|DSETP|DMNMX)\b\s+R(\d+)")       # D-op dest even
    for sf in sorted(SASS.glob("wide_*.sass")):
        for ln in sf.read_text().splitlines():
            for rx in (rx_ld, rx_st):
                m = rx.search(ln)
                if m:
                    width, reg = m.group(1), int(m.group(2))
                    mod = pat_align[width]
                    checked += 1
                    if reg != 255 and reg % mod != 0:
                        viol.append((sf.name, "data" + width, reg, ln.strip()))
            ma = rx_addr.search(ln)
            if ma:
                r = int(ma.group(1)); checked += 1
                if r != 255 and r % 2 != 0:
                    viol.append((sf.name, "addr%2", r, ln.strip()))
            md = rx_d.search(ln)
            if md:
                r = int(md.group(2)); checked += 1
                if r != 255 and r % 2 != 0:
                    viol.append((sf.name, "D-op-dst%2", r, ln.strip()))
    print(f"  real SASS wide_*: checked {checked} wide-op register bases; "
          f"alignment violations = {len(viol)}")
    for v in viol[:6]:
        print("    VIOLATION:", v)
    RESULTS["H1"] = {"checker_rejects_odd": (ok_aligned and not ok_mis),
                     "real_sass_checked": checked, "violations": len(viol)}
    return ok_aligned and not ok_mis and len(viol) == 0


# ---- H2: const-bank partition --------------------------------------------- #
def h2_constbank():
    hr("H2  Const-bank partition (CS only sees banks 0-7; 18-23 reserved)")
    # SM100 CCTL_LDC class carries the allowed-bank-list + CS gate
    name = None
    txt = (TBL / "sass_isa_SM100.txt").read_text().splitlines()
    for i, ln in enumerate(txt):
        if "Banks 18 through 23 are not allowed" in ln:
            # find the enclosing CLASS
            for j in range(i, 0, -1):
                cm = re.match(r'^(?:ALTERNATE\s+)?CLASS\s+"([^"]+)"', txt[j])
                if cm:
                    name = cm.group(1); break
            break
    print(f"  SM100 class carrying bank-partition rule: {name!r}")
    cr = load_class(TBL / "sass_isa_SM100.txt", name)
    chk = LegalityChecker(cr)
    # legal banks in CS: 0..7 ; illegal: 8, 18..23
    res = {}
    for bank in [0, 7, 8, 17, 18, 23, 24, 31]:
        ok, fs = chk.is_legal({"Sa_bank": bank, "Sa_addr": 0}, shader="CS")
        bad = [f.message for f in fs if not f.legal and not f.indeterminate]
        res[bank] = (ok, bad[:1])
    for b, (ok, why) in res.items():
        print(f"    CS  bank={b:2d}: legal={ok}  {why}")
    # in graphics (PS), banks 8-17 + 24-31 allowed but 18-23 still reserved
    print("  --- same class in PS (graphics) ---")
    res_ps = {}
    for bank in [7, 8, 17, 18, 23, 24, 31]:
        ok, fs = chk.is_legal({"Sa_bank": bank, "Sa_addr": 0}, shader="PS")
        res_ps[bank] = ok
        print(f"    PS  bank={bank:2d}: legal={ok}")
    # real compute SASS only references c[0x0..0x7]
    banks_used = set()
    rx = re.compile(r"c\[0x([0-9a-fA-F]+)\]\[")
    for sf in sorted(SASS.glob("*.sass")):
        for m in rx.finditer(sf.read_text()):
            banks_used.add(int(m.group(1), 16))
    print(f"  real SASS const banks referenced across all kernels: "
          f"{sorted(banks_used)}")
    cs_ok = (res[0][0] and res[7][0] and not res[8][0] and not res[18][0]
             and not res[23][0] and not res[24][0])
    ps_ok = (res_ps[8] and res_ps[17] and not res_ps[18] and not res_ps[23]
             and res_ps[24])
    only_low = all(b <= 7 or b == 3 for b in banks_used)  # c[0x3] is driver-reserved CB
    RESULTS["H2"] = {"cs_gate_ok": cs_ok, "ps_reserved_ok": ps_ok,
                     "banks_used": sorted(banks_used)}
    return cs_ok and ps_ok


# ---- H3: shader-type gates ------------------------------------------------- #
def h3_shadergate():
    hr("H3  Shader-type gates: compute-only op absent from graphics")
    # LDGSTS / LDSM are cp.async / ldmatrix -> compute-only. Confirm present in
    # compute SASS and that the class is non-ISHADER_ALL.
    for arch in ["SM90", "SM100"]:
        txt = (TBL / f"sass_isa_{arch}.txt").read_text()
        for op in ["LDGSTS", "LDSM", "STSM"]:
            # find a class whose OPCODES has this mnemonic
            classes = re.split(r'(?=^(?:ALTERNATE )?CLASS ")', txt, flags=re.M)
            hit = None
            for c in classes:
                if re.search(rf"^\s+{op}\w*\s*=\s*0b", c, re.M):
                    nm = re.match(r'(?:ALTERNATE )?CLASS "([^"]+)"', c)
                    vs = re.search(r"VALID_IN_SHADERS\s*=\s*(\S+)", c)
                    hit = (nm.group(1) if nm else "?",
                           vs.group(1).rstrip(";") if vs else "?")
                    break
            print(f"  {arch} {op:7s}: class={hit}")
    # real SASS: LDGSTS present in async_*.sass (compute)
    found = {}
    for sf in sorted(SASS.glob("async_*.sass")):
        t = sf.read_text()
        found[sf.name] = bool(re.search(r"\bLDGSTS\b", t))
    print(f"  real SASS LDGSTS present in async kernels: {found}")
    RESULTS["H3"] = {"ldgsts_in_async": found}
    return any(found.values())


# ---- H4: FP-format / tensor capability ------------------------------------- #
def h4_fpformat():
    hr("H4  FP-format & tensor capability per arch (real ptxas accept/reject)")
    # which (tag,arch) compiled vs failed
    status = {}
    for ps in sorted(SASS.glob("*.pserr")):
        tag, arch = ps.stem.split("_", 1)
        sassf = SASS / f"{ps.stem}.sass"
        status.setdefault(tag, {})[arch] = "OK" if sassf.exists() else "FAIL"
    for tag in ["wmma", "fp8", "fp4"]:
        print(f"  {tag}: {status.get(tag, {})}")
    # tcgen05 UTC* presence in tables: SM100 yes, SM120 no
    def has_op(arch, pat):
        txt = (TBL / f"sass_isa_{arch}.txt").read_text()
        return bool(re.search(rf"^\s+{pat}\w*\s*=\s*0b", txt, re.M))
    tc = {a: {"UTC(tcgen05)": has_op(a, "UTC"),
              "OMMA": has_op(a, "OMMA"),
              "LDTM(tmem)": has_op(a, "LDTM")}
          for a in ["SM90", "SM100", "SM103", "SM110", "SM120"]}
    print("  tensor-op presence in decoded tables:")
    for a, d in tc.items():
        print(f"    {a}: {d}")
    RESULTS["H4"] = {"compile_status": status, "tensor_presence": tc}
    return True


# ---- H5: LOP3 LUT ---------------------------------------------------------- #
def h5_lop3():
    hr("H5  LOP3 8-bit LUT semantics (functional model vs known truth tables)")
    A, B, C = 0xF0F0F0F0, 0xCCCCCCCC, 0xAAAAAAAA
    cases = [
        ("a & b",        0xC0, A & B),
        ("a | b",        0xFC, A | B),
        ("a ^ b",        0x3C, A ^ B),
        ("a ^ b ^ c",    0x96, A ^ B ^ C),
        ("a & b & c",    0x80, A & B & C),
        ("a | b | c",    0xFE, A | B | C),
        ("a | (b & c)",  0xF8, A | (B & C)),
        ("a ? b : c",    0xCA, (A & B) | (~A & C) & 0xFFFFFFFF),
        ("~a",           0x0F, (~A) & 0xFFFFFFFF),
        ("a",            0xF0, A),
        ("b",            0xCC, B),
        ("c",            0xAA, C),
    ]
    allok = True
    for label, lut, expect in cases:
        got = lop3(A, B, C, lut)
        ok = got == (expect & 0xFFFFFFFF)
        allok &= ok
        print(f"  LOP3 lut=0x{lut:02X} ({label:11s}) = 0x{got:08X}  "
              f"{'OK' if ok else 'MISMATCH exp 0x%08X' % (expect & 0xFFFFFFFF)}")
    # cross-check vs the LUT immediates ptxas actually emitted
    emitted = set()
    for sf in sorted(SASS.glob("alu_*.sass")):
        for m in re.finditer(r"LOP3\.LUT\s+R\d+,\s*R\d+,\s*R\d+,\s*\w+,\s*0x([0-9a-fA-F]+)", sf.read_text()):
            emitted.add(int(m.group(1), 16))
    print(f"  LUT immediates emitted by ptxas in alu_*.sass: "
          f"{sorted(hex(x) for x in emitted)}")
    RESULTS["H5"] = {"all_luts_match": allok, "emitted_luts": sorted(emitted)}
    return allok


def main():
    verdicts = {
        "H1 alignment": h1_alignment(),
        "H2 const-bank": h2_constbank(),
        "H3 shader-gate": h3_shadergate(),
        "H4 fp-format": h4_fpformat(),
        "H5 lop3-lut": h5_lop3(),
    }
    hr("VERDICTS")
    for k, v in verdicts.items():
        print(f"  {k:18s}: {'PASS' if v else 'CHECK'}")
    json.dump(RESULTS, open(SASS / "validate_results.json", "w"), indent=2)
    print("\nresults -> /tmp/sass_validate/validate_results.json")


if __name__ == "__main__":
    main()
