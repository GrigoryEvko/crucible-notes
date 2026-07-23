#!/usr/bin/env python3
"""
Table-driven 128-bit SASS instruction decoder.

Decodes a raw 128-bit NVIDIA SASS instruction word (Volta..Blackwell) into
opcode + operands + the scheduling-control word, using the BITS_* field model
recovered from the per-architecture instruction tables in nvdisasm V13.1.115.
The field map is derived from the parsed encoding tables (sass_isa_parser.py).

Encoding model (128-bit little-endian word = lo64 | hi64<<64):

  opcode      {91} ++ {11:0}    13-bit (12-bit, no bit91, on 81 SM75 legacy classes)
  Pg / Pg_not 14:12 / 15        guard predicate + inversion
  operand region 16:91          register ports / immediates / const-bank / modifiers
  scheduling-control word (Volta classic decomposition, equivalent to BITS_ model):
     stall   105:108  (4)   |
     yield   109      (1)   |-> opex low 5 bits  ({109:105})  = usched_info
     dst_wr  110:112  (3)        write scoreboard      (BITS dst_wr_sb, 7=none)
     src_rel 113:115  (3)        read/release scoreboard (BITS src_rel_sb, 7=none)
     req     116:121  (6)        scoreboard wait mask    (BITS req_bit_set)
     reuse   122:125  (4)   ->   opex high 3 bits ({124:122}) = batch_t/reuse arity
     pm_pred 102:103  (2)        perf-monitor predicate (SM90+)

  opex (BITS field {124:122}++{109:105}) = reuse<<5 | yield<<4 | stall, i.e.
  opex = batch_t<<5 | usched_info, where usched_info = {yield,stall}.

Usage:
    sass_decode.py --cubin FILE.cubin [--section .text.NAME]   # decode a real cubin
    sass_decode.py --word 0xHI 0xLO  [--arch SM90]             # decode one raw word
    sass_decode.py --selftest <dir-with-sass_isa_SM*.txt>      # round-trip harness

(c) nvopen-tools.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from sass_isa_parser import parse_file, IsaModel, InstrClass, BitField
except Exception:  # pragma: no cover - parser is a sibling tool
    parse_file = None


# --- bit helpers -------------------------------------------------------------

def bits(word: int, hi: int, lo: int) -> int:
    """Extract inclusive bit range [lo..hi] from a 128-bit word."""
    return (word >> lo) & ((1 << (hi - lo + 1)) - 1)


def reg_name(n: int) -> str:
    return "RZ" if n == 255 else f"R{n}"


def ureg_name(n: int) -> str:
    return "URZ" if n == 63 else f"UR{n}"


def pred_name(n: int) -> str:
    return "PT" if n == 7 else f"P{n}"


# --- scheduling-control word -------------------------------------------------

@dataclass
class SchedWord:
    stall: int          # 105:108  fixed issue stall (cycles)
    yield_: int         # 109      yield hint
    dst_wr_sb: int      # 110:112  write scoreboard (7 = none)
    src_rel_sb: int     # 113:115  read/release scoreboard (7 = none)
    req_bit_set: int    # 116:121  scoreboard wait mask
    reuse: int          # 122:125  operand reuse flags (slot a=bit0 ..)
    pm_pred: int        # 102:103  perf-monitor predicate
    opex: int           # {124:122}++{109:105} packed field

    @classmethod
    def decode(cls, w: int) -> "SchedWord":
        opex = (bits(w, 124, 122) << 5) | bits(w, 109, 105)
        return cls(
            stall=bits(w, 108, 105),
            yield_=bits(w, 109, 109),
            dst_wr_sb=bits(w, 112, 110),
            src_rel_sb=bits(w, 115, 113),
            req_bit_set=bits(w, 121, 116),
            reuse=bits(w, 125, 122),
            pm_pred=bits(w, 103, 102),
            opex=opex,
        )

    def text(self) -> str:
        """Render the maxas-style control annotation, the de-facto SASS notation."""
        parts = []
        # yield bit: in this convention bit set => '.', clear => 'Y' (yield)
        parts.append("--:" if self.yield_ else "Y:")
        wt = "-" if self.dst_wr_sb == 7 else str(self.dst_wr_sb)
        rd = "-" if self.src_rel_sb == 7 else str(self.src_rel_sb)
        parts.append(f"B{self.req_bit_set:06b}")  # wait mask (which barriers)
        ru = "".join(c for c, b in zip("abcd", range(4)) if (self.reuse >> b) & 1) or "-"
        return (f"[stall={self.stall} yield={self.yield_} "
                f"wr={wt} rd={rd} wait={self.req_bit_set:#08b} "
                f"reuse={ru} pm={self.pm_pred} opex={self.opex}]")


# --- opcode/operand model ----------------------------------------------------

@dataclass
class DecodedInsn:
    raw_lo: int
    raw_hi: int
    opcode13: int       # {91}++{11:0}
    opcode12: int       # {11:0}
    has_bit91: bool
    Pg: int
    Pg_not: int
    sched: SchedWord
    fields: dict        # named BITS_ fields decoded by value
    klass: str | None   # matched class name (best effort)
    mnemonic: str | None


class Decoder:
    """Builds an opcode -> candidate-(class, form) index from a parsed IsaModel
    and decodes raw words.  A class holds several operand sub-forms; each form
    pairs one opcode bit-pattern with its own BITS_* field layout, so the
    word's opcode selects both the mnemonic and the right operand map."""

    def __init__(self, model: "IsaModel"):
        self.model = model
        # Tier-1: primary (canonical) opcode -> class.  Near-bijective: each
        # class's last-form opcode uniquely names a mnemonic, so this resolves
        # the opcode->mnemonic ambiguity that plagues sub-form indexing.
        self.primary: dict[int, list] = {}
        # Tier-2: every sub-form opcode -> (class, form), used (a) to recover
        # non-primary forms (e.g. the LOP3 immediate opcode 0x812 that no class
        # exposes as primary) and (b) to pick the operand-field layout.
        self.by_form: dict[int, list] = {}
        self.legacy12: set[int] = set()
        for c in model.classes:
            ob = c.primary_opcode()
            if ob:
                self.primary.setdefault(int(ob, 2), []).append(c)
            for fm in (getattr(c, "forms", None) or []):
                fob = fm.opcode_bits()
                if not fob:
                    continue
                self.by_form.setdefault(int(fob, 2), []).append((c, fm))
                for f in fm.fields:
                    if f.name == "opcode" and f.width == 12:
                        self.legacy12.add(int(fob, 2))

    def _form_fields(self, c, fm) -> list:
        return fm.fields if fm is not None else c.fields

    def _const_ok(self, w: int, fields) -> tuple[bool, int]:
        """(all fixed-const fields satisfied?, number of const constraints)."""
        n = 0
        for f in fields:
            if not f.ranges:
                continue
            m = re.match(r"^\*?\s*(\d+)$", f.source.strip())
            if f.is_const and m:
                n += 1
                if self._field_val(w, f) != int(m.group(1)):
                    return False, n
        return True, n

    def _pick_form(self, c, w: int, opc):
        """Within a chosen class, select the sub-form whose const fields fully
        match the word; among those, the most-constrained (most specific)."""
        best, best_n = None, -1
        forms = [fm for fm in (getattr(c, "forms", None) or [])
                 if fm.opcode_bits() and int(fm.opcode_bits(), 2) == opc]
        forms = forms or (getattr(c, "forms", None) or [])
        for fm in forms:
            ok, n = self._const_ok(w, fm.fields)
            if ok and n > best_n:
                best_n, best = n, fm
        return best

    def _match(self, w: int, opc13: int, opc12: int):
        """Resolve (class, form). Tier-1 primary opcode names the class (clean
        mnemonic); tier-2 sub-form opcodes recover non-primary forms."""
        # Tier-1: canonical class by primary opcode.
        for opc in (opc13, opc12):
            cls = self.primary.get(opc)
            if cls:
                c = cls[0]
                return c, self._pick_form(c, w, opc)
        # Tier-2: a non-primary sub-form (e.g. immediate-only opcode). Among
        # candidates pick the one whose const fields fully match, most specific.
        for opc in (opc13, opc12):
            cands = self.by_form.get(opc)
            if not cands:
                continue
            best, best_n = None, -1
            for c, fm in cands:
                ok, n = self._const_ok(w, fm.fields)
                if ok and n > best_n:
                    best_n, best = n, (c, fm)
            if best:
                return best
            return cands[0]
        return None, None

    @staticmethod
    def _field_val(w: int, f) -> int:
        """Reconstruct a (possibly split) field's value, MSB-first over ranges."""
        val = 0
        for hi, lo in f.ranges:
            val = (val << (hi - lo + 1)) | bits(w, hi, lo)
        return val

    def decode(self, lo: int, hi: int) -> DecodedInsn:
        w = (hi << 64) | lo
        opc12 = bits(w, 11, 0)
        bit91 = bits(w, 91, 91)
        opc13 = (bit91 << 12) | opc12
        sched = SchedWord.decode(w)

        # match (class, form)
        c, fm = self._match(w, opc13, opc12)
        mnem = None
        fields: dict = {}
        if c:
            if fm is not None:
                mnem = fm.mnemonic()
            else:
                mnem = (c.primary_mnemonic() if hasattr(c, "primary_mnemonic")
                        else next((m for m in c.opcodes if not m.endswith("_pipe")), None))
            for f in self._form_fields(c, fm):
                if f.name in ("opcode",) or not f.ranges:
                    continue
                fields[f.name] = (self._field_val(w, f), f.source)

        return DecodedInsn(
            raw_lo=lo, raw_hi=hi, opcode13=opc13, opcode12=opc12,
            has_bit91=bool(bit91), Pg=bits(w, 14, 12), Pg_not=bits(w, 15, 15),
            sched=sched, fields=fields, klass=(c.name if c else None), mnemonic=mnem,
        )


# --- cubin section extraction ------------------------------------------------

def extract_text_sections(cubin: Path) -> list[tuple[str, bytes]]:
    """Return [(section_name, raw_bytes)] for every .text.* section in a cubin,
    read straight from the ELF file via readelf section offsets (objcopy can't
    parse EM_CUDA)."""
    out = subprocess.check_output(["readelf", "-S", str(cubin)],
                                  stderr=subprocess.DEVNULL).decode()
    lines = out.splitlines()
    data = cubin.read_bytes()
    res = []
    for i, ln in enumerate(lines):
        m = re.search(r"\.text\.(\S+)\s+PROGBITS\s+[0-9a-f]+\s+([0-9a-f]+)", ln)
        if not m:
            continue
        name = m.group(1)
        off = int(m.group(2), 16)
        sm = re.search(r"([0-9a-f]+)\s+[0-9a-f]+\s+AX", lines[i + 1])
        if not sm:
            continue
        size = int(sm.group(1), 16)
        res.append((name, data[off:off + size]))
    return res


def arch_from_cubin(cubin: Path) -> str:
    """Read the SM arch from nvdisasm's .target directive."""
    try:
        out = subprocess.check_output(["nvdisasm", "-c", str(cubin)],
                                      stderr=subprocess.DEVNULL).decode()
        m = re.search(r"\.target\s+sm_(\w+)", out)
        if m:
            return "SM" + m.group(1)
    except Exception:
        pass
    return "SM90"


def load_model(arch: str, table_dir: Path) -> "IsaModel":
    if parse_file is None:
        raise RuntimeError("sass_isa_parser not importable")
    # normalize: SM90 -> sass_isa_SM90.txt
    cand = table_dir / f"sass_isa_{arch}.txt"
    if not cand.exists():
        cand = table_dir / f"sass_isa_SM{arch.lstrip('SMsm')}.txt"
    return parse_file(cand)


# --- CLI ---------------------------------------------------------------------

# Decoded per-arch instruction tables (sass_isa_SM*.txt) live one level up.
TABLE_DIR = Path(__file__).resolve().parent.parent / "nvdisasm-sass-isa"


def cmd_cubin(args):
    cubin = Path(args.cubin)
    arch = args.arch or arch_from_cubin(cubin)
    model = load_model(arch, Path(args.table_dir))
    dec = Decoder(model)
    print(f"# {cubin.name}  arch={arch}  ({len(model.classes)} classes)")
    for name, raw in extract_text_sections(cubin):
        if args.section and args.section.lstrip(".text.") not in name:
            continue
        print(f"\n## .text.{name}  ({len(raw)//16} insns)")
        for off in range(0, len(raw) - 15, 16):
            lo = int.from_bytes(raw[off:off + 8], "little")
            hi = int.from_bytes(raw[off + 8:off + 16], "little")
            d = dec.decode(lo, hi)
            ops = describe_operands(d)
            print(f"/*{off:04x}*/ {d.mnemonic or '?':<10} {ops:<40} "
                  f"op={d.opcode13:#06x} {d.sched.text()}")


def describe_operands(d: DecodedInsn) -> str:
    """Human-readable operand summary from the decoded named fields."""
    f = d.fields
    out = []
    if "Rd" in f:
        out.append(f"Rd={reg_name(f['Rd'][0])}")
    if "Ra" in f:
        out.append(f"Ra={reg_name(f['Ra'][0])}")
    if "Rb" in f:
        out.append(f"Rb={reg_name(f['Rb'][0])}")
    if "Rc" in f:
        out.append(f"Rc={reg_name(f['Rc'][0])}")
    if "Pu" in f:
        out.append(f"Pu={pred_name(f['Pu'][0])}")
    if "cop" in f and f["cop"][1] in ("Pv",):
        out.append(f"Pv={pred_name(f['cop'][0])}")
    if d.Pg != 7 or d.Pg_not:
        out.append(f"@{'!' if d.Pg_not else ''}{pred_name(d.Pg)}")
    return " ".join(out)


def cmd_word(args):
    hi = int(args.word[0], 0)
    lo = int(args.word[1], 0)
    model = load_model(args.arch or "SM90", Path(args.table_dir))
    dec = Decoder(model)
    d = dec.decode(lo, hi)
    print(f"raw   = hi:0x{hi:016x} lo:0x{lo:016x}")
    print(f"opcode= {d.opcode13:#06x} (13b={d.opcode13:013b}) bit91={int(d.has_bit91)} op12={d.opcode12:#05x}")
    print(f"class = {d.klass}  mnem={d.mnemonic}")
    print(f"guard = @{'!' if d.Pg_not else ''}{pred_name(d.Pg)}")
    print(f"sched = {d.sched.text()}")
    print("fields:")
    for k, (v, src) in sorted(d.fields.items()):
        print(f"  {k:<22} = {v:<6} (src {src})")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="128-bit SASS instruction decoder")
    ap.add_argument("--table-dir", default=str(TABLE_DIR))
    ap.add_argument("--arch")
    ap.add_argument("--cubin")
    ap.add_argument("--section")
    ap.add_argument("--word", nargs=2, metavar=("HI", "LO"))
    args = ap.parse_args()
    if args.cubin:
        cmd_cubin(args)
    elif args.word:
        cmd_word(args)
    else:
        ap.print_help()
