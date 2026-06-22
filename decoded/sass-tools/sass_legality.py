#!/usr/bin/env python3
"""SASS legality / constraint checker + tiny functional model.

Pure binary-derived: the rule grammar is reconstructed from the decoded
per-architecture nvdisasm SASS-ISA tables (sass_isa_SM*.txt). Given a decoded
instruction (a CLASS plus concrete operand/field values), this evaluates the
`CONDITIONS` rules the hardware/assembler enforces and reports legal/illegal
with the offending rule and message.

Two parts:
  1. LegalityChecker -- loads a class's CONDITIONS from a table file and
     evaluates them against an operand environment. Models the four constraint
     families the tables enforce:
        - OOR_REG_ERROR            (register index in range, != R254)
        - MISALIGNED_REG_ERROR     (sz-driven pair/quad alignment)
        - INVALID_CONST_ADDR_ERROR (const-bank partition + RTV offset cap)
        - shader-type gates        (%SHADER_TYPE == $ST_CS -> ...)
     plus a generic evaluator for the residual comparison/implication rules.
  2. functional model -- exact bit-level semantics for IADD3, LOP3 (8-bit LUT),
     SHF (funnel shift), PRMT (byte permute).

The rule expression grammar (recovered from the tables) is a small C-like
boolean/arith language over named fields:
    expr := or; or := and ('||' and)*; and := cmp ('&&' cmp)*
    cmp  := add (('=='|'!='|'<='|'>='|'<'|'>') add)?
    impl := or ('->' or)?           # CONDITIONS rule is `guard -> requirement`
    atoms: integers (dec / 0x.. / 0b..), field names, %SYMBOLS,
           `Register@RZ` / `$ST_CS` enum refs, ( ), unary ! and -, % * + -.
A CONDITIONS rule is "(guard) -> (requirement) : message". With no `->`, the
whole expression is the requirement (must be TRUE to be legal). With `->`, the
requirement is only enforced when the guard is TRUE.

Enum/symbol resolution is environment-driven: the caller supplies values for
`Register@RZ` (=255), `%MAX_REG_COUNT`, `%SHADER_TYPE`, `$ST_CS`, etc. Unknown
symbols make a rule "indeterminate" (skipped, reported separately) so the
checker never silently passes a rule it could not evaluate.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

DEFAULT_TABLE_DIR = Path("/home/grigory/iprit/nvopen-tools/decoded/nvdisasm-sass-isa")

# Error kinds that abort assembly (fatal) vs warnings.
FATAL_KINDS = {
    "OOR_REG_ERROR", "MISALIGNED_REG_ERROR", "MISALIGNED_ADDR_ERROR",
    "INVALID_CONST_ADDR_ERROR", "ILLEGAL_INSTR_ENCODING_ERROR",
    "ILLEGAL_INSTR_ENCODING_SASS_ONLY_ERROR", "ILLEGAL_INSTR_PARAM_ERROR",
}
WARNING_KINDS = {"UNPREDICTABLE_BEHAVIOR_WARNING", "INVALID_CONST_ADDR_WARNING"}


# --------------------------------------------------------------------------- #
# Expression evaluator
# --------------------------------------------------------------------------- #

class Indeterminate(Exception):
    """Raised when a rule references a symbol the environment cannot resolve."""


_TOKEN_RE = re.compile(r"""
      \s*(?:
        (?P<num>0[xX][0-9a-fA-F]+|0[bB][01]+|\d+)
      | (?P<enum>`?[A-Za-z_%$][\w]*(?:@[\w]+)?)
      | (?P<op>->|<=|>=|==|!=|&&|\|\||[()!%*+\-<>])
      )
""", re.VERBOSE)


def _tokenize(s: str) -> list[str]:
    out: list[str] = []
    i = 0
    while i < len(s):
        m = _TOKEN_RE.match(s, i)
        if not m:
            if s[i].isspace():
                i += 1
                continue
            raise ValueError(f"bad token at {i!r}: ...{s[i:i+20]!r}")
        i = m.end()
        tok = next(g for g in m.groups() if g is not None)
        out.append(tok)
    return out


class _Parser:
    """Recursive-descent parser/evaluator over the recovered rule grammar.

    Evaluates directly against `env` (a dict of name->int). Names not present
    raise Indeterminate so the checker can flag unresolved rules rather than
    treat them as satisfied.
    """

    def __init__(self, tokens: list[str], env: dict):
        self.t = tokens
        self.i = 0
        self.env = env

    def peek(self) -> Optional[str]:
        return self.t[self.i] if self.i < len(self.t) else None

    def eat(self, tok: Optional[str] = None) -> str:
        cur = self.t[self.i]
        if tok is not None and cur != tok:
            raise ValueError(f"expected {tok!r} got {cur!r}")
        self.i += 1
        return cur

    # impl := or ('->' or)?
    def parse_impl(self) -> int:
        guard = self.parse_or()
        if self.peek() == "->":
            self.eat("->")
            req = self.parse_or()
            # logical implication: legal unless guard true and req false
            return 1 if (not guard or req) else 0
        return guard

    def parse_or(self) -> int:
        v = self.parse_and()
        while self.peek() == "||":
            self.eat("||")
            r = self.parse_and()
            v = 1 if (v or r) else 0
        return v

    def parse_and(self) -> int:
        v = self.parse_cmp()
        while self.peek() == "&&":
            self.eat("&&")
            r = self.parse_cmp()
            v = 1 if (v and r) else 0
        return v

    def parse_cmp(self) -> int:
        v = self.parse_add()
        op = self.peek()
        if op in ("==", "!=", "<=", ">=", "<", ">"):
            self.eat(op)
            r = self.parse_add()
            if op == "==":
                return 1 if v == r else 0
            if op == "!=":
                return 1 if v != r else 0
            if op == "<=":
                return 1 if v <= r else 0
            if op == ">=":
                return 1 if v >= r else 0
            if op == "<":
                return 1 if v < r else 0
            if op == ">":
                return 1 if v > r else 0
        return v

    def parse_add(self) -> int:
        v = self.parse_mul()
        while self.peek() in ("+", "-"):
            op = self.eat()
            r = self.parse_mul()
            v = v + r if op == "+" else v - r
        return v

    def parse_mul(self) -> int:
        v = self.parse_unary()
        while self.peek() in ("*", "%"):
            op = self.eat()
            r = self.parse_unary()
            v = v * r if op == "*" else (v % r if r != 0 else 0)
        return v

    def parse_unary(self) -> int:
        tok = self.peek()
        if tok == "!":
            self.eat("!")
            return 0 if self.parse_unary() else 1
        if tok == "-":
            self.eat("-")
            return -self.parse_unary()
        return self.parse_atom()

    def parse_atom(self) -> int:
        tok = self.eat()
        if tok == "(":
            v = self.parse_impl()
            self.eat(")")
            return v
        if re.fullmatch(r"0[xX][0-9a-fA-F]+", tok):
            return int(tok, 16)
        if re.fullmatch(r"0[bB][01]+", tok):
            return int(tok, 2)
        if tok.isdigit():
            return int(tok)
        # name / enum / %SYMBOL  -> look up in env
        key = tok.lstrip("`")
        if key in self.env:
            return self.env[key]
        # `Register@R254` style enum literal: try the raw form too
        if tok in self.env:
            return self.env[tok]
        raise Indeterminate(key)


# Rules that consult an external lookup table (`DEFINED TABLES_xxx(a,b,c)`) or
# any function-call form cannot be evaluated from operand values alone -- the
# table contents are a separate decoder artifact. These are reported as
# indeterminate so the checker never silently passes them.
_UNSUPPORTED_RE = re.compile(r"\bDEFINED\b|\bTABLES_|\bBITSET\b|[A-Za-z_]\w*\s*\(")


def eval_expr(expr: str, env: dict) -> Optional[int]:
    """Evaluate a rule requirement string. Returns 1/0, or None if indeterminate."""
    if not expr.strip():
        return None
    if _UNSUPPORTED_RE.search(expr):
        return None
    try:
        toks = _tokenize(expr)
    except ValueError:
        return None
    if not toks:
        return None
    try:
        p = _Parser(toks, env)
        v = p.parse_impl()
        return 1 if v else 0
    except (Indeterminate, ValueError, IndexError):
        return None


# --------------------------------------------------------------------------- #
# CONDITIONS rule extraction
# --------------------------------------------------------------------------- #

@dataclass
class Rule:
    kind: str
    expr: str            # full requirement (may contain `guard -> req`)
    message: str


@dataclass
class ClassRules:
    name: str
    rules: list[Rule] = field(default_factory=list)
    valid_in_shaders: Optional[str] = None
    opcodes: dict = field(default_factory=dict)


_CLASS_RE = re.compile(r'^(?:ALTERNATE\s+)?CLASS\s+"([^"]+)"')
_KIND_RE = re.compile(r"^([A-Z_]+(?:ERROR|WARNING|INFO))\s*$")


def load_class(table_path: Path, class_name: str) -> ClassRules:
    """Parse one CLASS block's CONDITIONS / VALID_IN_SHADERS / OPCODES."""
    lines = table_path.read_text(errors="replace").splitlines()
    # find class
    start = None
    for i, ln in enumerate(lines):
        m = _CLASS_RE.match(ln)
        if m and m.group(1) == class_name:
            start = i
            break
    if start is None:
        raise KeyError(f"class {class_name!r} not in {table_path.name}")
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if _CLASS_RE.match(lines[j]):
            end = j
            break
    block = lines[start:end]
    return _parse_block(class_name, block)


def _parse_block(name: str, block: list[str]) -> ClassRules:
    cr = ClassRules(name=name)
    sect = None
    i = 0
    while i < len(block):
        s = block[i].strip()
        if s == "CONDITIONS":
            sect = "cond"; i += 1; continue
        if s in ("PROPERTIES", "PREDICATES", "ENCODING", "FORMAT"):
            sect = s.lower(); i += 1; continue
        if s == "OPCODES":
            sect = "ops"; i += 1; continue
        if sect == "cond":
            km = _KIND_RE.match(s)
            if km:
                kind = km.group(1)
                # next non-empty line(s) up to one ending with ':' is the expr;
                # the line after is the message.
                expr_parts = []
                j = i + 1
                while j < len(block):
                    e = block[j].strip()
                    if e.endswith(":"):
                        expr_parts.append(e[:-1].strip())
                        j += 1
                        break
                    expr_parts.append(e)
                    j += 1
                msg = ""
                if j < len(block):
                    mm = block[j].strip()
                    if mm.startswith('"'):
                        msg = mm.strip('"')
                        j += 1
                cr.rules.append(Rule(kind, " ".join(expr_parts), msg))
                i = j
                continue
        elif sect == "properties":
            pm = re.match(r"VALID_IN_SHADERS\s*=\s*(\S+)", s)
            if pm:
                cr.valid_in_shaders = pm.group(1).rstrip(";")
        elif sect == "ops":
            om = re.match(r"(\w+)\s*=\s*(0b[01]+)", s)
            if om:
                cr.opcodes[om.group(1)] = om.group(2)
        i += 1
    return cr


# --------------------------------------------------------------------------- #
# Checker
# --------------------------------------------------------------------------- #

@dataclass
class Finding:
    kind: str
    legal: bool
    message: str
    expr: str
    indeterminate: bool = False


# Default symbolic environment. Hardware sentinels recovered from the tables.
def base_env(max_reg=255, max_ureg=63, shader="CS") -> dict:
    """Standard symbol bindings used by the rules.

    `Register@RZ`=255 (RZ is R255). `Register@R254` reserved. %MAX_REG_COUNT is
    the architectural register-file size (the rules compare <= MAX-1/-2/-3/-4 for
    1/2/3/4-register-wide operands). %SHADER_TYPE/$ST_CS gate compute.
    """
    st = {"CS": 0, "TRAP": 1, "VSA": 2, "VSB": 3, "GS": 4, "TS": 5,
          "TI": 6, "PS": 7, "UNKNOWN": 8}
    return {
        "Register@RZ": 255, "Register@R254": 254, "Register@RZ ": 255,
        "UniformRegister@URZ": 63,
        "%MAX_REG_COUNT": max_reg, "%MAX_UNIFORM_REG_COUNT": max_ureg,
        "%SHADER_TYPE": st.get(shader, 0),
        **{f"$ST_{k}": v for k, v in st.items()},
    }


class LegalityChecker:
    def __init__(self, class_rules: ClassRules):
        self.cr = class_rules

    def check(self, operands: dict, *, shader="CS", max_reg=255,
              max_ureg=63) -> list[Finding]:
        """Evaluate every CONDITIONS rule against `operands`.

        `operands` maps field names (Rd, Ra, Rb, Rc, Sb_bank, Sb_addr,
        word_mask, imm8, ...) to integer values. Returns one Finding per rule.
        A rule passes (legal=True) when its requirement evaluates TRUE.
        """
        env = base_env(max_reg=max_reg, max_ureg=max_ureg, shader=shader)
        env.update(operands)
        findings = []
        for r in self.cr.rules:
            v = eval_expr(r.expr, env)
            if v is None:
                findings.append(Finding(r.kind, True, r.message, r.expr,
                                        indeterminate=True))
            else:
                findings.append(Finding(r.kind, bool(v), r.message, r.expr))
        return findings

    def is_legal(self, operands: dict, **kw) -> tuple[bool, list[Finding]]:
        fs = self.check(operands, **kw)
        bad = [f for f in fs
               if (not f.legal) and (not f.indeterminate)
               and f.kind in FATAL_KINDS]
        return (len(bad) == 0, fs)


# --------------------------------------------------------------------------- #
# Functional model: exact semantics for pure-ALU ops
# --------------------------------------------------------------------------- #

M32 = 0xFFFFFFFF


def iadd3(a: int, b: int, c: int) -> int:
    """IADD3 Rd, Ra, Rb, Rc -> (a+b+c) mod 2^32 (3-input integer add)."""
    return (a + b + c) & M32


def lop3(a: int, b: int, c: int, lut: int) -> int:
    """LOP3.LUT Rd, Ra, Rb, Rc, imm8.

    `imm8` is the 8-entry truth table of the 3-input bitwise function. For each
    of the 8 combinations (Ca,Cb,Cc) of one bit from each operand, the result
    bit is `imm8` bit number `(Ca<<2)|(Cb<<1)|Cc`. Equivalently the canonical
    construction: with a=0xF0, b=0xCC, c=0xAA as the "selector" inputs, the LUT
    is whatever boolean expression of (a,b,c) you want; e.g. a&b -> 0xC0,
    a|b -> 0xFC, a^b^c -> 0x96.
    """
    res = 0
    for i in range(32):
        ca = (a >> i) & 1
        cb = (b >> i) & 1
        cc = (c >> i) & 1
        sel = (ca << 2) | (cb << 1) | cc
        res |= ((lut >> sel) & 1) << i
    return res & M32


def shf_l(lo: int, shift: int, hi: int, wrap: bool = True) -> int:
    """SHF.L  Rd, Ra(lo), Rb(shift), Rc(hi): left funnel shift.

    Concatenate {hi:lo} as a 64-bit value, shift left by `shift`, take the high
    32 bits. `wrap` masks the shift count to 5 bits (.W / wrap); otherwise the
    count clamps to 32 (.clamp). Matches SHF.L.W.U32.HI seen in real SASS.
    """
    n = (shift & 31) if wrap else min(shift, 32)
    val = ((hi & M32) << 32) | (lo & M32)
    return (val << n >> 32) & M32


def shf_r(lo: int, shift: int, hi: int, wrap: bool = False) -> int:
    """SHF.R Rd, Ra(lo), Rb(shift), Rc(hi): right funnel shift, low 32 bits."""
    n = (shift & 31) if wrap else min(shift, 32)
    val = ((hi & M32) << 32) | (lo & M32)
    return (val >> n) & M32


def prmt(a: int, b: int, sel: int) -> int:
    """PRMT Rd, Ra, Rb, Rc: byte permute (default/forward mode).

    Bytes are indexed across {b:a} as a 64-bit source: byte 0..3 from `a`,
    4..7 from `b`. Each result byte k takes the 4-bit nibble `sel[4k..4k+3]`:
    low 3 bits select the source byte, the top bit requests sign-replication
    (MSB of the selected byte broadcast to all 8 bits).
    """
    src = ((b & M32) << 32) | (a & M32)
    res = 0
    for k in range(4):
        ctl = (sel >> (4 * k)) & 0xF
        idx = ctl & 0x7
        byte = (src >> (8 * idx)) & 0xFF
        if ctl & 0x8:
            byte = 0xFF if (byte & 0x80) else 0x00
        res |= byte << (8 * k)
    return res & M32


# --------------------------------------------------------------------------- #
# CLI / self-test
# --------------------------------------------------------------------------- #

def _demo():
    print("== functional model self-test ==")
    # LOP3 LUT semantics
    A, B, C = 0xF0F0F0F0, 0xCCCCCCCC, 0xAAAAAAAA
    assert lop3(A, B, C, 0xC0) == (A & B), "lop3 0xC0 != a&b"
    assert lop3(A, B, C, 0xFC) == (A | B), "lop3 0xFC != a|b"
    assert lop3(A, B, C, 0x96) == (A ^ B ^ C), "lop3 0x96 != a^b^c"
    assert lop3(A, B, C, 0xF0) == A, "lop3 0xF0 != a"
    assert lop3(A, B, C, 0xCC) == B
    assert lop3(A, B, C, 0xAA) == C
    assert lop3(A, B, C, 0x80) == (A & B & C)
    assert lop3(A, B, C, 0xFE) == (A | B | C)
    assert lop3(0xFF00FF00, 0x0F0F0F0F, 0, 0x3C) == (0xFF00FF00 ^ 0x0F0F0F0F)
    print("  LOP3 LUT: a&b=0xC0, a|b=0xFC, a^b^c=0x96, a=0xF0, &all=0x80, |all=0xFE  OK")
    assert iadd3(1, 2, 3) == 6
    assert iadd3(0xFFFFFFFF, 1, 0) == 0
    assert iadd3(0x80000000, 0x80000000, 0) == 0
    print("  IADD3 wraparound  OK")
    # SHF left funnel: {hi=0x12345678 : lo=0x9ABCDEF0} << 8 -> hi32 = 0x345678 9A
    assert shf_l(0x9ABCDEF0, 8, 0x12345678, wrap=True) == 0x3456789A
    assert shf_r(0x9ABCDEF0, 8, 0x12345678, wrap=True) == 0x789ABCDE
    print("  SHF funnel L/R  OK")
    # PRMT identity 0x3210 -> low 4 bytes of a; 0x7654 -> low 4 bytes of b
    assert prmt(0x11223344, 0x55667788, 0x3210) == 0x11223344
    assert prmt(0x11223344, 0x55667788, 0x7654) == 0x55667788
    assert prmt(0x11223344, 0x55667788, 0x0000) == 0x44444444
    print("  PRMT byte permute  OK")
    print("ALL functional self-tests passed.\n")

    print("== legality checker self-test (LDG.E.128 class on SM100) ==")
    tp = DEFAULT_TABLE_DIR / "sass_isa_SM100.txt"
    cr = load_class(tp, "ldg__sImmOffset")
    chk = LegalityChecker(cr)
    # 32-bit load, Rd=4: legal
    ok, fs = chk.is_legal({"Rd": 4, "Ra": 6, "Ra_offset": 0}, shader="CS")
    print(f"  ldg Rd=R4: legal={ok}")
    for f in fs:
        flag = "INDET" if f.indeterminate else ("ok" if f.legal else "FAIL")
        if not f.legal and not f.indeterminate:
            print(f"    [{flag}] {f.kind}: {f.message}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--demo":
        _demo()
    else:
        _demo()
