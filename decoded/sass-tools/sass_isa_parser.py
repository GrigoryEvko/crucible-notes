#!/usr/bin/env python3
# nvopen-tools -- SASS reverse-engineering tooling.
"""
SASS-ISA table parser for NVIDIA nvdisasm-decoded instruction databases.

Parses the per-arch SASS instruction database (sass_isa_SM*.txt) into a
structured model: top-level header, RELOCATORS, REGISTERS token tables, and a
list of CLASS instruction blocks each with FORMAT / CONDITIONS / PROPERTIES /
PREDICATES / OPCODES / ENCODING sub-sections, reconstructing each instruction's
bit-field layout from the BITS_* directives.

Bit-field directive grammar (the encoding axis):

    BITS_<width>_<hi>_<lo>_<name> = <source>                 # contiguous span
    BITS_<width>_<hi1>_<lo1>_<hi2>_<lo2>_<name> = <source>   # split span (2 ranges)

  - <width>  : total bit count (sum of all sub-ranges)
  - each (hi,lo) pair : an inclusive bit range [lo..hi] in the 128-bit word
  - <name>   : field role (opcode, Pg, Rd, Ra, Rb, Rc, opex, ...)
  - <source> : RHS expression -- the operand/value driving the field
               (e.g. `Rd`, `Pg@not`, `*7` = fixed const, TABLES_*(...) = table lookup)

Usage:
    python3 sass_isa_parser.py <file.txt> [--json] [--summary]
    python3 sass_isa_parser.py --validate-all <dir>
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional


# --- BITS_* field directive --------------------------------------------------

# matches BITS_<w>_<n0>_<n1>[_<n2>_<n3>...]_<name> = <rhs>
# The numeric run after BITS_ is: width, then pairs of (hi,lo).
_BITS_RE = re.compile(r"^\s*BITS_((?:\d+_)+)([A-Za-z][\w]*?)\s*=\*?\s*(.+?);?\s*$")
_OPCODE_RE = re.compile(r"^\s*([A-Za-z_][\w.]*)\s*=\s*(0b[01]+)\s*;?\s*$")
_CLASS_RE = re.compile(r'^CLASS\s+"([^"]+)"')


@dataclass
class BitField:
    """One BITS_* encoding directive: a named field placed at one or more
    bit ranges in the instruction word, driven by an RHS source expression."""
    name: str
    width: int                       # declared total width
    ranges: list[tuple[int, int]]    # list of (hi, lo) inclusive ranges, MSB-first
    source: str                      # RHS expression
    is_const: bool                   # RHS was `*<const>` (fixed bits)

    @property
    def span_width(self) -> int:
        """Sum of (hi-lo+1) over all ranges -- should equal declared width."""
        return sum(hi - lo + 1 for hi, lo in self.ranges)

    def covers(self) -> set[int]:
        bits: set[int] = set()
        for hi, lo in self.ranges:
            bits.update(range(lo, hi + 1))
        return bits


@dataclass
class Form:
    """One operand sub-form of a CLASS: a single OPCODES block paired with the
    ENCODING block that immediately follows it.  A CLASS can hold many forms
    (e.g. register vs immediate vs const-bank), each with its own opcode bits
    and its own bit-field layout."""
    opcodes: dict[str, str] = field(default_factory=dict)   # mnemonic -> 0b...
    fields: list[BitField] = field(default_factory=list)

    def opcode_bits(self) -> Optional[str]:
        for m, b in self.opcodes.items():
            if not m.endswith("_pipe"):
                return b
        return next(iter(self.opcodes.values()), None)

    def mnemonic(self) -> Optional[str]:
        for m in self.opcodes:
            if not m.endswith("_pipe"):
                return m
        return next(iter(self.opcodes), None)


@dataclass
class InstrClass:
    name: str
    fmt_mnemonic: Optional[str] = None       # leading token of FORMAT line
    format_lines: list[str] = field(default_factory=list)
    properties: dict[str, str] = field(default_factory=dict)
    predicates: dict[str, str] = field(default_factory=dict)
    forms: list["Form"] = field(default_factory=list)        # per-sub-form opcode+fields
    opcodes: dict[str, str] = field(default_factory=dict)   # mnemonic -> 0b... (last sub-form)
    fields: list[BitField] = field(default_factory=list)
    condition_kinds: list[str] = field(default_factory=list)
    # A CLASS can hold several OPCODES sub-blocks (one per operand sub-form,
    # e.g. RuIR vs RIR with different immediate/const-bank opcodes).  Collapsing
    # them into the `opcodes` dict loses every form but the last; keep them all
    # here so the decoder indexes every legal opcode bit-pattern of the class.
    all_opcode_bits: set[str] = field(default_factory=set)   # set of 0b... strings
    mnemonics: set[str] = field(default_factory=set)         # non-_pipe mnemonic names

    def primary_opcode(self) -> Optional[str]:
        # The non-_pipe mnemonic is the canonical SASS opcode; both map to same bits.
        for m, bits in self.opcodes.items():
            if not m.endswith("_pipe"):
                return bits
        return next(iter(self.opcodes.values()), None)

    def primary_mnemonic(self) -> Optional[str]:
        for m in self.opcodes:
            if not m.endswith("_pipe"):
                return m
        return next(iter(self.opcodes), None)


@dataclass
class IsaModel:
    arch: str
    header: dict[str, str] = field(default_factory=dict)
    relocators: list[dict] = field(default_factory=list)
    classes: list[InstrClass] = field(default_factory=list)


# --- parsing -----------------------------------------------------------------

def _parse_bits_directive(line: str) -> Optional[BitField]:
    m = _BITS_RE.match(line)
    if not m:
        return None
    nums_blob, name, rhs = m.group(1), m.group(2), m.group(3).strip()
    nums = [int(x) for x in nums_blob.rstrip("_").split("_") if x != ""]
    if not nums:
        return None
    width = nums[0]
    coords = nums[1:]
    # coords are (hi, lo) pairs whose spans must sum to <width>.  Some field
    # names embed trailing numbers (e.g. input_reg_sz_32_dist) which the regex
    # may have peeled into <coords>.  Consume pairs left-to-right only until the
    # running span equals the declared width; leftover numbers belong to name.
    ranges: list[tuple[int, int]] = []
    tot = 0
    i = 0
    while i + 1 < len(coords) and tot < width:
        hi, lo = coords[i], coords[i + 1]
        if hi < lo:          # not a real (hi,lo) range -> stop
            break
        ranges.append((hi, lo))
        tot += hi - lo + 1
        i += 2
        if tot == width:
            break
    is_const = rhs.startswith("*") or bool(re.match(r"^\*?\s*\d+$", rhs))
    return BitField(name=name, width=width, ranges=ranges, source=rhs, is_const=is_const)


def parse_file(path: Path) -> IsaModel:
    text = path.read_text(errors="replace")
    lines = text.splitlines()
    arch = path.stem.replace("sass_isa_", "")
    model = IsaModel(arch=arch)

    # header keys (top of file, before RELOCATORS)
    for ln in lines[:40]:
        hm = re.match(r"^\s*(PROCESSOR_ID|ISSUE_SLOTS|WORD_SIZE|BRANCH_DELAY|"
                      r"ELF_ID|ELF_ABI|ELF_ABI_VERSION|ELF_VERSION)\s+(.+?);?\s*$", ln)
        if hm:
            model.header[hm.group(1)] = hm.group(2).strip()
        am = re.match(r'^ARCHITECTURE\s+"([^"]+)"', ln)
        if am:
            model.header["ARCHITECTURE"] = am.group(1)

    # relocators: lines of form { "R_CUDA_...", ... { { lo, width }, ... } }
    in_reloc = False
    reloc_buf = ""
    for ln in lines:
        s = ln.strip()
        if s == "RELOCATORS":
            in_reloc = True
            continue
        if in_reloc:
            if s.startswith("REGISTERS") or s == "REGISTERS":
                break
            if not s:
                continue
            reloc_buf += " " + s
            if s.endswith("}") and reloc_buf.count("{") <= reloc_buf.count("}"):
                rm = re.search(r'"(R_CUDA_[^"]+)"', reloc_buf)
                if rm:
                    # extract the trailing {lo,width} placement pairs
                    placements = re.findall(r"\{\s*(\d+)\s*,\s*(\d+)\s*\}", reloc_buf)
                    # last group is the {{lo,w},...}; drop a spurious leading one if it is the outer brace pair count
                    model.relocators.append({
                        "name": rm.group(1),
                        "placements": [(int(a), int(b)) for a, b in placements],
                    })
                reloc_buf = ""

    # CLASS blocks
    idxs = [i for i, ln in enumerate(lines) if _CLASS_RE.match(ln)]
    idxs.append(len(lines))
    for k in range(len(idxs) - 1):
        block = lines[idxs[k]:idxs[k + 1]]
        model.classes.append(_parse_class(block))
    return model


def _parse_class(block: list[str]) -> InstrClass:
    name = _CLASS_RE.match(block[0]).group(1)
    ic = InstrClass(name=name)
    section = None
    cur_form: Optional[Form] = None      # the form currently being filled
    for ln in block[1:]:
        s = ln.strip()
        if s == "CONDITIONS":
            section = "cond"; continue
        if s == "PROPERTIES":
            section = "props"; continue
        if s == "PREDICATES":
            section = "preds"; continue
        if s == "OPCODES":
            section = "ops"
            cur_form = Form()           # each OPCODES block starts a fresh form
            ic.forms.append(cur_form)
            continue
        if s == "ENCODING":
            section = "enc"; continue
        if s.startswith("FORMAT"):
            section = "fmt"
            ic.format_lines.append(s)
            fm = re.match(r"^FORMAT(?:\s+PREDICATE)?\s+(\S+)", s)
            # mnemonic token after the predicate guard
            ic.fmt_mnemonic = None
            continue
        if section == "fmt":
            ic.format_lines.append(s)
        elif section == "cond":
            if re.match(r"^[A-Z_]+(ERROR|WARNING)$", s):
                ic.condition_kinds.append(s)
        elif section == "props":
            pm = re.match(r"^(\w+)\s*=\s*(.+?)\s*;?\s*$", s)
            if pm:
                ic.properties[pm.group(1)] = pm.group(2)
        elif section == "preds":
            pm = re.match(r"^(\w+)\s*=\s*(.+?)\s*;?\s*$", s)
            if pm:
                ic.predicates[pm.group(1)] = pm.group(2)
        elif section == "ops":
            om = _OPCODE_RE.match(s)
            if om and cur_form is not None:
                mnem, bitstr = om.group(1), om.group(2)
                cur_form.opcodes[mnem] = bitstr
                ic.opcodes[mnem] = bitstr
                ic.all_opcode_bits.add(bitstr)
                if not mnem.endswith("_pipe"):
                    ic.mnemonics.add(mnem)
        elif section == "enc":
            target = cur_form.fields if cur_form is not None else ic.fields
            # Some lines join multiple BITS_* LHS with commas, sharing one RHS:
            #   BITS_5_58_54_Sb_bank,BITS_14_53_40_Sb_offset = ConstBankAddress2(...)
            # Split into the individual LHS directives (each keeps the shared RHS).
            if "=" in s and s.count("BITS_") > 1 and "BITS_" in s.split("=")[0]:
                lhs, _, rhs = s.partition("=")
                for part in lhs.split(","):
                    part = part.strip()
                    if part.startswith("BITS_"):
                        bf = _parse_bits_directive(part + " = " + rhs)
                        if bf:
                            target.append(bf)
                            ic.fields.append(bf)
                continue
            bf = _parse_bits_directive(s)
            if bf:
                target.append(bf)
                ic.fields.append(bf)
    return ic


# --- reporting ---------------------------------------------------------------

def summarize(model: IsaModel) -> dict:
    n_op = sum(len(c.opcodes) for c in model.classes)
    n_primary = sum(1 for c in model.classes if c.primary_opcode())
    field_names: dict[str, int] = {}
    for c in model.classes:
        for f in c.fields:
            field_names[f.name] = field_names.get(f.name, 0) + 1
    return {
        "arch": model.arch,
        "architecture_label": model.header.get("ARCHITECTURE"),
        "word_size_header": model.header.get("WORD_SIZE"),
        "elf_id": model.header.get("ELF_ID"),
        "n_classes": len(model.classes),
        "n_opcode_entries": n_op,
        "n_classes_with_opcode": n_primary,
        "n_relocators": len(model.relocators),
        "n_distinct_bitfields": len(field_names),
    }


def main(argv: list[str]) -> int:
    if "--validate-all" in argv:
        d = Path(argv[argv.index("--validate-all") + 1])
        files = sorted(d.glob("sass_isa_SM*.txt"))
        print(f"{'arch':<8}{'classes':>9}{'opcodes':>9}{'relocs':>8}{'fields':>8}  label")
        for f in files:
            m = parse_file(f)
            s = summarize(m)
            print(f"{s['arch']:<8}{s['n_classes']:>9}{s['n_opcode_entries']:>9}"
                  f"{s['n_relocators']:>8}{s['n_distinct_bitfields']:>8}  {s['architecture_label']}")
        return 0
    path = Path(argv[1])
    model = parse_file(path)
    if "--summary" in argv:
        print(json.dumps(summarize(model), indent=2))
    elif "--json" in argv:
        print(json.dumps({
            "arch": model.arch,
            "header": model.header,
            "n_classes": len(model.classes),
            "classes": [asdict(c) for c in model.classes[:5]],
        }, indent=2, default=str))
    else:
        print(json.dumps(summarize(model), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
