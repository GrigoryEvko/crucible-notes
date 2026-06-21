#!/usr/bin/env python3
# nvopen-tools -- SASS reverse-engineering tooling.  MIT-style: our code; the
# scheduling model below is recovered from static + differential analysis of the
# CUDA 13.1 ptxas / nvdisasm binaries.  This module loads the *local* decoded
# tables at runtime (no vendor table text or matrices are duplicated here).
"""
Per-(arch, SASS-class) scheduling model -- the lookup the scheduler consumes.

Bridges two binary-derived keyings into one cached per-arch model:

  * the per-arch SASS instruction tables (decoded/nvdisasm-sass-isa/sass_isa_SM*.txt)
    give, per CLASS: INSTRUCTION_TYPE (coupled vs decoupled_*_scbd), MIN_WAIT_NEEDED,
    SIDL_NAME, VIRTUAL_QUEUE (the functional-unit/pipe for decoupled ops),
    MEM_SCBD / MEM_SCBD_TYPE (Blackwell) -- i.e. the exact coupled/decoupled
    classification, the per-class minimum wait, and the decoupled pipe.

  * the ptxas scheduling tables (decoded/ptxas-sched-full/) give the scalar
    latency oracle (per-Ori-opcode result band {6,13,24,30,300}) and the per-SM
    dependency-rule table (40-B records: latency / throughput_inv / barrier_*
    / read_latency / write_latency / stall_cycles / issue_slots).

The join key from a SASS mnemonic to the oracle's Ori-opcode band is the
mnemonic itself (the oracle's best-effort name column).  The coupled-math
issue-relative stall (4 same-pipe / 5 cross-pipe / 6 to a slow input / 8 the
Turing pre-AGU slot / 13 CC-pred) is NOT a single table cell -- ptxas's OCG
scheduler derives it from the producer's result band and the consumer's
operand-collect timing.  We recover those constants by differential analysis of
emitted SASS and ship them as a small per-(family, prod-pipe, cons-pipe) matrix
(coupled_stall_matrix.tsv) -- our own result, not vendor bytes.

Everything is cached per arch.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

HERE = Path(__file__).resolve().parent
ISA_DIR = HERE.parent / "nvdisasm-sass-isa"
SCHED_DIR = HERE.parent / "ptxas-sched-full"
STALL_MATRIX = HERE / "coupled_stall_matrix.tsv"


# =============================================================================
# Pipe families.  A coupled-math op resolves a RAW with a fixed issue-relative
# stall whose magnitude depends on the producer pipe and the consumer pipe.
# These family names are our classification; the *membership* is derived from
# the SASS-table mnemonic / INSTRUCTION_TYPE and the oracle band.
# =============================================================================

# Coupled-math pipe family by mnemonic (the in-order math pipes; the SASS tables
# leave VIRTUAL_QUEUE unset for coupled ops, so the pipe is derived here from the
# mnemonic's functional unit).
PIPE_INT = "INT"        # integer ALU: IADD3 LOP3 LEA SHF SEL BMSK SGXT PRMT ...
PIPE_IMUL = "IMUL"      # integer multiply pipe: IMAD / IMAD.WIDE
PIPE_FMA = "FMA"        # FP32 FMA pipe: FFMA FADD FMUL FMNMX FSWZADD MOV(f) ...
PIPE_FP16 = "FP16"      # packed FP16x2: HADD2 HMUL2 HFMA2 HSETP2
PIPE_CC = "CC"          # writes a condition-code / predicate: ISETP FSETP ...
PIPE_CVT = "CVT"        # coupled cross-domain conversion: I2FP F2FP I2I I2IP
PIPE_UNIFORM = "UNIFORM"  # uniform datapath: ULDC and U-prefixed coupled ops
PIPE_AGU = "AGU"        # decoupled memory address-generation (consumer side)
PIPE_SFU = "SFU"        # decoupled MUFU transcendental input (consumer side)
PIPE_CVTI = "CVTI"      # decoupled float<->int conversion input (I2F/F2I/F2F)
PIPE_OTHER = "OTHER"

# Mnemonic -> coupled-math pipe family.  Restricted to the in-order coupled ops;
# decoupled ops get their pipe from VIRTUAL_QUEUE instead.
_COUPLED_PIPE = {
    # integer ALU
    "IADD3": PIPE_INT, "LOP3": PIPE_INT, "LEA": PIPE_INT, "SHF": PIPE_INT,
    "SEL": PIPE_INT, "BMSK": PIPE_INT, "SGXT": PIPE_INT, "PRMT": PIPE_INT,
    "IABS": PIPE_INT, "PLOP3": PIPE_INT, "P2R": PIPE_INT, "R2P": PIPE_INT,
    "VOTE": PIPE_INT, "IMNMX": PIPE_INT, "ICMP": PIPE_INT, "VIADD": PIPE_INT,
    "FLO": PIPE_INT,  # (decoupled in tables, but if seen coupled -> int)
    # integer multiply
    "IMAD": PIPE_IMUL, "IMUL": PIPE_IMUL, "IDP": PIPE_IMUL, "VIMNMX": PIPE_IMUL,
    # FP32 FMA pipe
    "FFMA": PIPE_FMA, "FADD": PIPE_FMA, "FMUL": PIPE_FMA, "FMNMX": PIPE_FMA,
    "FSEL": PIPE_FMA, "FSET": PIPE_FMA, "MOV": PIPE_FMA, "FSWZADD": PIPE_FMA,
    "FCHK": PIPE_FMA, "RRO": PIPE_FMA,
    # packed FP16x2
    "HADD2": PIPE_FP16, "HMUL2": PIPE_FP16, "HFMA2": PIPE_FP16,
    "HSETP2": PIPE_FP16, "HMNMX2": PIPE_FP16,
    # condition-code / predicate producers (control band)
    "ISETP": PIPE_CC, "FSETP": PIPE_CC, "DSETP": PIPE_CC, "HSETP2": PIPE_CC,
    # coupled cross-domain conversions
    "I2FP": PIPE_CVT, "F2FP": PIPE_CVT, "I2I": PIPE_CVT, "I2IP": PIPE_CVT,
    "F2IP": PIPE_CVT,
    # uniform datapath
    "ULDC": PIPE_UNIFORM, "UMOV": PIPE_UNIFORM, "UIADD3": PIPE_UNIFORM,
    "ULEA": PIPE_UNIFORM, "ULOP3": PIPE_UNIFORM, "UISETP": PIPE_UNIFORM,
}

# A consumer's pipe as seen by a producer's RAW edge.  Memory ops collect their
# address/data operands one issue-slot early -> the AGU consumer pipe; the
# SFU/MUFU and the decoupled conversions present an SFU input.
_CONSUMER_PIPE = {
    # memory address-generation (the producer feeds an address or store value)
    "STG": PIPE_AGU, "STS": PIPE_AGU, "STL": PIPE_AGU, "ST": PIPE_AGU,
    "RED": PIPE_AGU, "ATOMG": PIPE_AGU, "ATOMS": PIPE_AGU, "ATOM": PIPE_AGU,
    "LDG": PIPE_AGU, "LDS": PIPE_AGU, "LDL": PIPE_AGU, "LD": PIPE_AGU,
    "LDC": PIPE_AGU, "LDGSTS": PIPE_AGU, "TEX": PIPE_AGU, "SULD": PIPE_AGU,
    "SUST": PIPE_AGU, "SUATOM": PIPE_AGU,
    # transcendental SFU input latch (MUFU): forwards at 4 on sm_8x+, 6 on sm_7x
    "MUFU": PIPE_SFU, "POPC": PIPE_SFU, "FLO": PIPE_SFU, "BREV": PIPE_SFU,
    # decoupled float<->int conversion input: the coupled feeder pays the full
    # result band (6) before the decoupled conversion can issue, on every arch.
    "I2F": PIPE_CVTI, "F2I": PIPE_CVTI, "F2F": PIPE_CVTI,
}


def _base_mnem(m: str) -> str:
    return m.split(".")[0] if m else ""


def coupled_pipe(mnem: str) -> str:
    """Producer pipe family of a coupled-math mnemonic."""
    return _COUPLED_PIPE.get(_base_mnem(mnem), PIPE_INT)


def consumer_pipe(mnem: str, cm: "ClassModel | None" = None) -> str:
    """Consumer pipe family as a RAW producer sees it.

    A memory/SFU consumer presents a distinct operand-collect timing (AGU/SFU);
    otherwise the consumer's own coupled pipe family is used.  When a ClassModel
    is available its decoupled VIRTUAL_QUEUE refines the AGU/SFU split."""
    base = _base_mnem(mnem)
    if base in _CONSUMER_PIPE:
        return _CONSUMER_PIPE[base]
    if cm is not None and not cm.coupled:
        vq = cm.vqueue
        if vq.startswith("AGU") or vq in ("TEX", "SUST", "SUATOM"):
            return PIPE_AGU
        if vq == "MUFU":
            return PIPE_SFU
        if vq == "FMA64":          # the decoupled float<->int conversion queue
            return PIPE_CVTI
    return coupled_pipe(mnem)


# =============================================================================
# Arch family (the latency tables are shared per family).
# =============================================================================

def arch_family(arch: str) -> str:
    """Latency-descriptor family for an arch: sm_7x / sm_8x / sm_10x.

    The decoded tables share one 72-B latency-descriptor table per family
    (sm_7x = sm_60/70/72/75; sm_8x = sm_80/86/89/90/90a; sm_10x = sm_100/103).
    Newer Blackwell-class arches (sm_110/120/121) follow the sm_10x schema."""
    n = _arch_num(arch)
    if n < 80:
        return "sm7x"
    if n < 100:
        return "sm8x"
    return "sm10x"


def _arch_num(arch: str) -> int:
    m = re.search(r"(\d+)", arch)
    return int(m.group(1)) if m else 89


# =============================================================================
# Scalar latency oracle: Ori-opcode -> result band {6,13,24,30,300}.
# =============================================================================

@lru_cache(maxsize=1)
def _load_oracle() -> dict[str, int]:
    """mnemonic (best-effort) -> result-latency band, from the scalar oracle.

    The oracle is keyed by Ori opcode; its best-effort mnemonic column is the
    join key.  Default ALU band = 6, default memory band = 300; explicit rows
    carry {13,24,30,300}."""
    out: dict[str, int] = {}
    path = SCHED_DIR / "scalar_latency_oracle.tsv"
    if not path.exists():
        return out
    with path.open() as fh:
        next(fh, None)  # header
        for ln in fh:
            cols = ln.rstrip("\n").split("\t")
            if len(cols) < 3:
                continue
            lat, mnem = cols[1].strip(), cols[2].strip()
            if not mnem:
                continue
            if lat:
                try:
                    out[mnem.upper()] = int(lat)
                except ValueError:
                    pass
    return out


# The default result bands when the oracle has no explicit row: 6 for ALU/math,
# the variable bands for memory / SFU resolved through scoreboards.  These are
# the binary-confirmed anchors (ALU=6, long-memory=300).
_DEFAULT_BAND = {
    "LDG": 300, "LD": 300, "LDL": 300, "LDGSTS": 300, "ATOMG": 300, "ATOM": 300,
    "TEX": 300, "TLD": 300, "TXD": 300, "SULD": 300,
    "LDS": 30, "LDSM": 30, "STS": 1, "STG": 1, "STL": 1, "ST": 1, "RED": 1,
    "LDC": 24, "ULDC": 24, "S2R": 24, "S2UR": 24, "CS2R": 24, "BREV": 24,
    "MUFU": 24, "F2F": 13, "F2I": 13, "I2F": 13, "I2I": 13, "POPC": 13,
    "FLO": 13, "PRMT": 6,
    "HMMA": 13, "IMMA": 13, "DMMA": 13, "BMMA": 13, "OMMA": 13,
}


def result_band(mnem: str) -> int:
    """Result-latency band of an op (for critical-path weighting / debug)."""
    base = _base_mnem(mnem)
    o = _load_oracle()
    if base in o:
        return o[base]
    if base in _DEFAULT_BAND:
        return _DEFAULT_BAND[base]
    return 6   # default ALU band (binary anchor)


# =============================================================================
# Per-arch class model: mnemonic -> {coupled, itype, min_wait, scbd arming,
# pipe, band}.  Parsed from the local decoded SASS-ISA table.
# =============================================================================

@dataclass
class ClassModel:
    mnem: str
    coupled: bool
    itype: str            # raw INSTRUCTION_TYPE (without INST_TYPE_ prefix)
    min_wait: int         # MIN_WAIT_NEEDED floor
    arms_wr: bool         # arms a write scoreboard
    arms_rd: bool         # arms a read-release scoreboard
    depbar: bool          # branch-unit / DEPBAR decoupled
    vqueue: str           # VIRTUAL_QUEUE (decoupled pipe); "-" if none
    sidl: str             # SIDL_NAME
    pipe: str             # producer pipe family (coupled) or AGU/SFU (decoupled)
    band: int             # result-latency band
    mem_scbd: str         # MEM_SCBD (Blackwell); "" if absent


_CLASS_RE = re.compile(r'^CLASS\s+"([^"]+)"')
_CLASS_MNEM_RE = re.compile(r"^([a-z0-9]+)")
_PROP_RES = {
    "itype": re.compile(r"INSTRUCTION_TYPE\s*=\s*INST_TYPE_(\w+)"),
    "min_wait": re.compile(r"MIN_WAIT_NEEDED\s*=\s*(\d+)"),
    "sidl": re.compile(r"SIDL_NAME\s*=\s*`?(?:SIDL_NAMES@)?(\S+)"),
    "vqueue": re.compile(r"VIRTUAL_QUEUE\s*=\s*\$?(?:VQ_)?(\S+)"),
    "mem_scbd": re.compile(r"MEM_SCBD\s*=\s*(\S+)"),
}


def _itype_props(itype: str) -> tuple[bool, bool, bool, bool]:
    """(coupled, arms_wr, arms_rd, is_depbar) from an INSTRUCTION_TYPE name."""
    coupled = itype.startswith("COUPLED")
    wr = ("WR_SCBD" in itype) or ("RD_WR_SCBD" in itype)
    rd = ("RD_SCBD" in itype) or ("RD_WR_SCBD" in itype) or ("RD_NOREQ" in itype)
    depbar = "DEPBAR" in itype
    return coupled, wr, rd, depbar


def _isa_path(arch: str) -> Path | None:
    """Resolve the local SASS-ISA table path for an arch label."""
    cand = ISA_DIR / f"sass_isa_{arch}.txt"
    if cand.exists():
        return cand
    n = arch.upper().lstrip("SM_").lstrip("sm")
    for stem in (f"SM{n}", f"SM{n}a"):
        cand = ISA_DIR / f"sass_isa_{stem}.txt"
        if cand.exists():
            return cand
    return None


@lru_cache(maxsize=None)
def load_arch_model(arch: str) -> dict[str, ClassModel]:
    """mnemonic (uppercase base) -> ClassModel for one arch, cached.

    Many CLASS forms share a mnemonic; when forms disagree on coupled-ness keep
    the *decoupled* verdict (a scoreboard is the conservative resolution)."""
    path = _isa_path(arch)
    out: dict[str, ClassModel] = {}
    if path is None:
        return out
    cur_mnem: str | None = None
    props: dict[str, str] = {}

    def _flush(cur_mnem: str | None, props: dict[str, str]) -> None:
        if not cur_mnem or "itype" not in props:
            return
        itype = props["itype"]
        coupled, wr, rd, depbar = _itype_props(itype)
        vq = props.get("vqueue", "-")
        band = result_band(cur_mnem)
        if coupled:
            pipe = _COUPLED_PIPE.get(cur_mnem, PIPE_INT)
        else:
            if vq.startswith("AGU") or vq in ("TEX", "SUST", "SUATOM"):
                pipe = PIPE_AGU
            elif vq == "MUFU":
                pipe = PIPE_SFU
            elif vq == "FMA64":
                pipe = PIPE_CVTI
            else:
                pipe = PIPE_OTHER
        cm = ClassModel(
            mnem=cur_mnem, coupled=coupled, itype=itype,
            min_wait=int(props.get("min_wait", "0")),
            arms_wr=wr, arms_rd=rd, depbar=depbar, vqueue=vq,
            sidl=props.get("sidl", ""), pipe=pipe, band=band,
            mem_scbd=props.get("mem_scbd", ""),
        )
        prev = out.get(cur_mnem)
        # prefer the decoupled verdict if forms disagree; otherwise keep the
        # first (which carries the most representative properties).
        if prev is None or (prev.coupled and not coupled):
            out[cur_mnem] = cm

    with path.open(errors="replace") as fh:
        for ln in fh:
            mc = _CLASS_RE.match(ln)
            if mc:
                _flush(cur_mnem, props)
                mm = _CLASS_MNEM_RE.match(mc.group(1))
                cur_mnem = mm.group(1).upper() if mm else None
                props = {}
                continue
            for key, rgx in _PROP_RES.items():
                m = rgx.search(ln)
                if m:
                    props[key] = m.group(1).rstrip(";").strip()
        _flush(cur_mnem, props)
    return out


# =============================================================================
# Per-arch dependency-rule join (optional richer model).
# =============================================================================

@lru_cache(maxsize=None)
def load_dependency_rules(arch: str) -> list[dict]:
    """Load the per-SM 40-B dependency-rule rows (if present).

    Keyed by unit_id; carries latency / throughput_inv / barrier_* /
    read_latency / write_latency / stall_cycles / issue_slots.  Returned as a
    list of dicts (small)."""
    n = _arch_num(arch)
    for name in (f"dependency_rules_sm_{n}.tsv", f"dependency_rules_sm_{n}a.tsv"):
        path = SCHED_DIR / name
        if path.exists():
            break
    else:
        return []
    rows: list[dict] = []
    with path.open() as fh:
        hdr = next(fh).rstrip("\n").split("\t")
        for ln in fh:
            vals = ln.rstrip("\n").split("\t")
            if len(vals) != len(hdr):
                continue
            rows.append({k: int(v) for k, v in zip(hdr, vals)})
    return rows


# =============================================================================
# Coupled-stall matrix: (family, prod_pipe, cons_pipe) -> issue-relative stall.
# Loaded from the local TSV (our own differential-analysis result).
# =============================================================================

@lru_cache(maxsize=1)
def _load_stall_matrix() -> dict[tuple[str, str, str], int]:
    out: dict[tuple[str, str, str], int] = {}
    if not STALL_MATRIX.exists():
        return out
    with STALL_MATRIX.open() as fh:
        for ln in fh:
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            cols = ln.split("\t")
            if len(cols) < 4:
                continue
            fam, pp, cp, st = cols[0], cols[1], cols[2], cols[3]
            try:
                out[(fam, pp, cp)] = int(st)
            except ValueError:
                pass
    return out


def coupled_stall(arch: str, prod_pipe: str, cons_pipe: str) -> int:
    """Issue-relative stall a coupled producer in `prod_pipe` owes a consumer in
    `cons_pipe`, for the arch's family.

    Resolution is most-specific first, and within a tie a *producer*-pipe rule
    outranks a *consumer*-pipe rule -- a CC/predicate producer always emits the
    control band regardless of the consumer, and a cross-domain conversion
    producer levies its own penalty.  Family-specific rows outrank the family-`*`
    rows.  Falls through to the same-pipe (4) / cross-pipe (5) / CC (13) /
    AGU (5) / SFU (6) structural anchors."""
    fam = arch_family(arch)
    m = _load_stall_matrix()
    # (family-specificity, then producer-then-consumer wildcard specificity)
    for f in (fam, "*"):
        if (f, prod_pipe, cons_pipe) in m:        # both exact
            return m[(f, prod_pipe, cons_pipe)]
    for f in (fam, "*"):
        if (f, prod_pipe, "*") in m:              # producer-specific (outranks)
            return m[(f, prod_pipe, "*")]
    for f in (fam, "*"):
        if (f, "*", cons_pipe) in m:              # consumer-specific
            return m[(f, "*", cons_pipe)]
    # structural defaults (binary anchors)
    if prod_pipe == PIPE_CC:
        return 13
    if cons_pipe == PIPE_CVTI:
        return 6
    if cons_pipe == PIPE_SFU:
        return 4
    if cons_pipe == PIPE_AGU:
        return 5
    if prod_pipe == cons_pipe:
        return 4
    return 5


if __name__ == "__main__":
    import sys
    arch = sys.argv[1] if len(sys.argv) > 1 else "SM89"
    model = load_arch_model(arch)
    print(f"# {arch}: {len(model)} classes")
    for mn in ("IADD3", "IMAD", "FFMA", "FMUL", "ISETP", "FSETP", "I2F",
               "I2FP", "F2I", "MUFU", "LDG", "STG", "ULDC", "S2R"):
        cm = model.get(mn)
        if cm:
            print(f"  {mn:8s} coupled={int(cm.coupled)} pipe={cm.pipe:8s} "
                  f"mw={cm.min_wait} band={cm.band} vq={cm.vqueue} "
                  f"it={cm.itype}")
    print("\n# stall matrix sample:")
    for pp in (PIPE_INT, PIPE_FMA, PIPE_IMUL, PIPE_CC):
        for cp in (PIPE_INT, PIPE_FMA, PIPE_IMUL, PIPE_AGU, PIPE_SFU):
            print(f"  {arch} {pp:6s}->{cp:6s} = "
                  f"{coupled_stall(arch, pp, cp)}")
