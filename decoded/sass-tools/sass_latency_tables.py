#!/usr/bin/env python3
# nvopen-tools -- SASS reverse-engineering tooling.  The scheduling model below is
# recovered from static + differential analysis of the CUDA 13.1 ptxas / nvdisasm
# binaries.  This module loads the decoded tables at runtime.
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
Turing pre-AGU slot / 13 CC-pred) is not a single table cell -- ptxas's OCG
scheduler derives it from the producer's result band and the consumer's
operand-collect timing.  Those constants come from differential analysis of
emitted SASS, held as a per-(family, prod-pipe, cons-pipe) matrix
(coupled_stall_matrix.tsv).

Result-band provenance.  The completion-latency bands are GPU-measured on sm_89
(the one part we can run); ``band_provenance(mnem, arch)`` labels how each band
maps to another arch -- "gpu" on sm_89, "family" within its sm_8x descriptor
family (sm_80/86/90/90a), "inherited" for the sm_7x / sm_10x families (ptxas's
scalar band model is family-invariant there and no GPU is available to refine
the silicon value), "binary-derived" for the arch-invariant oracle band / static
tensor-async rows, "anchor" for the ALU default.  The tensor band is the one
that genuinely shifts by arch (HMMA/IMMA coupled on sm_7x/8x vs the
wgmma/tcgen05 group-async path on sm_90a/sm_10x), keyed by op name.  The band
accessors take an optional ``arch`` (default sm_89): the returned cycle count is
the sm_89-silicon measurement (the family anchor); use ``band_provenance`` to
know whether that value is measured or inherited for the target arch.

Everything is cached per arch.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

HERE = Path(__file__).resolve().parent
ISA_DIR = HERE.parent / "nvdisasm-sass-isa"
SCHED_DIR = HERE.parent / "ptxas-sched-full"
STALL_MATRIX = HERE / "coupled_stall_matrix.tsv"


# Pipe families.  A coupled-math op resolves a RAW with a fixed issue-relative
# stall whose magnitude depends on the producer pipe and the consumer pipe.
# Membership is derived from the SASS-table mnemonic / INSTRUCTION_TYPE and the
# oracle band.

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
PIPE_VINT = "VINT"      # secondary integer adder (VIADD / Blackwell IADD)
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
    "VOTE": PIPE_INT, "IMNMX": PIPE_INT, "ICMP": PIPE_INT,
    "FLO": PIPE_INT,  # (decoupled in tables, but if seen coupled -> int)
    # secondary integer adder pipe (cross-pipe to the main IADD3 ALU)
    "VIADD": PIPE_VINT, "IADD": PIPE_VINT,
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


# Arch family (the latency tables are shared per family).

def arch_family(arch: str) -> str:
    """Latency-descriptor family for an arch: sm_7x / sm_8x / sm_10x.

    The decoded tables share one 72-B latency-descriptor table per family
    (sm_7x = sm_60/70/72/75; sm_8x = sm_80/86/89/90/90a; sm_10x = sm_100/103).
    The newer Blackwell-class arches resolve into the sm_10x family too: ptxas's
    per-SM table installer (sub_ABF590) hard-codes only the lat10x descriptor
    table (.rodata VMA 0x226C880) for the whole Blackwell branch, and an
    exhaustive .rodata scan finds no additional 72-B descriptor table.  sm_120
    and sm_121 are byte-identical and resolve to the sm_103 dependency-rule +
    scoreboard tables (installer enum 0x4001); sm_110 (Jetson Thor, a distinct
    codegen generation) routes through the same lat10x latency family.  See
    decoded/ptxas-sched-full/ for the per-arch TSVs and the empirically measured
    consumer/Thor-vs-datacenter stall deltas."""
    n = _arch_num(arch)
    if n < 80:
        return "sm7x"
    if n < 100:
        return "sm8x"
    return "sm10x"


def _arch_num(arch: str) -> int:
    m = re.search(r"(\d+)", arch)
    return int(m.group(1)) if m else 89


# Scalar latency oracle: Ori-opcode -> result band {6,13,24,30,300}.

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


# Measured-latency tables: the GPU-measurement TSVs under ../ptxas-sched-full/
# are the SINGLE SOURCE OF TRUTH for the result bands.  The four loaders below
# parse them lazily (each cached) and the band-derivation maps the per-op rows
# into one critical-path weight per base mnemonic, honoring the measurement
# caveats documented inline.  No band number is duplicated in code; the .py
# carries only the *selection* logic over the measured data.

DECOUPLED_TSV = SCHED_DIR / "decoupled_scalar_latency.tsv"
MEMORY_TSV = SCHED_DIR / "memory_latency.tsv"
TENSOR_TSV = SCHED_DIR / "tensor_latency.tsv"
UPRED_TSV = SCHED_DIR / "uniform_pred_hazards.tsv"
ASYNC_TSV = SCHED_DIR / "async_scoreboards.tsv"

_BAND_WEIGHT_RE = re.compile(r"\bband=(\d+)")
_FIRST_INT_RE = re.compile(r"(\d+)")


def _read_tsv_rows(path: Path) -> list[list[str]]:
    """Tab-split every non-comment, non-blank line of a TSV; drop the one
    column-header line (the first non-`#` row).  Robust to per-file schemas."""
    rows: list[list[str]] = []
    if not path.exists():
        return rows
    header_seen = False
    with path.open(errors="replace") as fh:
        for ln in fh:
            if ln.startswith("#"):
                continue
            ln = ln.rstrip("\n")
            if not ln.strip():
                continue
            if not header_seen:        # the first non-comment line is the header
                header_seen = True
                continue
            rows.append(ln.split("\t"))
    return rows


# -- decoupled scalar completion latency (SFU / MIO / FMA64-conv / coupled CS2R).

@lru_cache(maxsize=1)
def _load_decoupled() -> dict[str, tuple[str, int]]:
    """base-mnemonic -> (scoreboard, gpu_measured_latency cycles).

    Columns: op sets_scoreboard source_model ptxas_emitted_wait
             gpu_measured_latency note.  Many opcodes have several variant rows
             (MUFU has eight transcendentals); keep the LONGEST measured latency
             as the conservative critical-path weight for the base mnemonic.
    The scoreboard is "-coupled" for the coupled exceptions (CS2R, I2FP)."""
    out: dict[str, tuple[str, int]] = {}
    for cols in _read_tsv_rows(DECOUPLED_TSV):
        if len(cols) < 5:
            continue
        base = _base_mnem(cols[0]).upper()
        scbd = cols[1].strip()
        try:
            lat = int(cols[4].strip())
        except ValueError:
            continue
        prev = out.get(base)
        if prev is None or lat > prev[1]:
            out[base] = (scbd, lat)
    return out


def decoupled_latency(mnem: str, arch: str = "SM89") -> tuple[str, int]:
    """(scoreboard, cycles) for a decoupled scalar op, from
    decoupled_scalar_latency.tsv.  `scoreboard` is the SBn the producer arms
    ("SB0".."SB5") or "-coupled" for the coupled exceptions (CS2R, I2FP).
    `cycles` is the sm_89-measured completion latency; `arch` is accepted for API
    symmetry -- pair with band_provenance(mnem, arch) for the measured-vs-derived
    label on a non-Ada target.  Raises KeyError if the op has no measured row."""
    return _load_decoupled()[_base_mnem(mnem).upper()]


# -- memory load/store latency, per space and global cache tier.

@lru_cache(maxsize=1)
def _load_memory() -> dict[str, list[dict]]:
    """base-mnemonic -> list of per-(space,tier) rows.

    Each row dict: {space, tier, scoreboard, weight (the band=N critical-path
    weight ptxas assigns), measured (gpu_measured_latency)}.  Columns: op space
    cache_tier sets_scoreboard source_model gpu_measured_latency note."""
    out: dict[str, list[dict]] = {}
    for cols in _read_tsv_rows(MEMORY_TSV):
        if len(cols) < 6:
            continue
        base = _base_mnem(cols[0]).upper()
        wm = _BAND_WEIGHT_RE.search(cols[4])
        try:
            measured = int(cols[5].strip())
        except ValueError:
            continue
        out.setdefault(base, []).append({
            "space": cols[1].strip(),
            "tier": cols[2].strip(),
            "scoreboard": cols[3].strip(),
            "weight": int(wm.group(1)) if wm else measured,
            "measured": measured,
        })
    return out


# Cache tiers (ordered cheapest -> dearest) used to pick a representative tier
# when the caller does not name one: the first present in this order wins.
_TIER_PREFERENCE = ("smem", "const_cache_hit", "L1_hit", "L2", "L1_writeback",
                    "smem_rmw", "L2_rmw", "async_copy_L2", "DRAM_miss")


def memory_latency(op: str, space: str | None = None,
                   tier: str | None = None, arch: str = "SM89") -> int:
    """Measured load-use latency (cycles) of a memory op from
    memory_latency.tsv.

    With no `space`/`tier` the cheapest representative tier is returned (the
    on-chip / L1-hit common case); pass `space=` and/or `tier=` to select a
    specific row (e.g. memory_latency("LDG", tier="DRAM_miss") -> 606).  All
    tiers stay exposed -- the global-load tiers measured on sm_89 are
    L1_hit 35 / L2 152 / DRAM_miss 606.  `arch` is accepted for API symmetry; the
    value is the sm_89 measurement (the silicon tier latencies on another arch --
    e.g. Blackwell HBM -- are not measurable here, so band_provenance reports them
    "inherited" off sm_89).  Raises KeyError for an unknown op."""
    rows = _load_memory()[_base_mnem(op).upper()]
    cand = [r for r in rows
            if (space is None or r["space"] == space)
            and (tier is None or r["tier"] == tier)]
    if not cand:
        raise KeyError(f"no memory_latency row for op={op} space={space} "
                       f"tier={tier}")
    if len(cand) == 1 or tier is not None:
        return cand[0]["measured"]
    order = {t: i for i, t in enumerate(_TIER_PREFERENCE)}
    cand.sort(key=lambda r: order.get(r["tier"], len(order)))
    return cand[0]["measured"]


# -- tensor (MMA) result latency (arch-aware: HMMA/IMMA/BMMA/DMMA inline on
# sm_7x/sm_8x vs the warpgroup / Blackwell group-async path on sm_90a/sm_10x).

@lru_cache(maxsize=1)
def _load_tensor() -> dict[str, dict]:
    """base-mnemonic -> {arch, mode, band}.

    `mode` is the TSV's coupled_or_scoreboard: "coupled" (HMMA/IMMA/BMMA, no
    write scoreboard), "scoreboard" (DMMA, arms a real scoreboard), or "group_sb"
    (the warpgroup/tcgen05 async ops).  `band` is the COMPLETION latency from the
    source_model column ("27-28" -> 27 for HMMA/IMMA/BMMA, "25-26" -> 26 for
    DMMA), NOT the gpu_min_required hardware-interlock floor (3).  For the
    group_sb rows source_model is "static" -> band None (the accumulator read is
    gated by wgmma.wait_group / tcgen05.wait, not a scalar band).  `arch` is the
    arch the row was measured/derived on: sm_89 coupled/scoreboard ops are
    GPU-measured; the sm_90a / sm_100 group_sb rows are static binary analysis."""
    out: dict[str, dict] = {}
    for cols in _read_tsv_rows(TENSOR_TSV):
        if len(cols) < 4:
            continue
        base = _base_mnem(cols[0]).upper()
        m = _FIRST_INT_RE.search(cols[3])
        out.setdefault(base, {
            "arch": cols[1].strip(),
            "mode": cols[2].strip(),
            "band": int(m.group(1)) if m else None,
        })
    return out


def tensor_latency(mnem: str, arch: str = "SM89") -> tuple[bool, int]:
    """(coupled, cycles) for a tensor MMA op from tensor_latency.tsv.

    For the inline ops (HMMA/IMMA/BMMA coupled, DMMA scoreboard) `cycles` is the
    result-completion band (~27 / ~26), used for critical-path weighting --
    distinct from the gpu_min_required hardware floor (3).  For the warpgroup /
    Blackwell async ops (group_sb: HGMMA/IGMMA/tcgen05) there is no inline band;
    the accumulator read is gated by a group scoreboard, so `cycles` is that
    group scoreboard's arming latency (from async_scoreboards) and `coupled` is
    False.  `arch` is accepted for API symmetry / provenance pairing -- the
    tensor ops are arch-unique by name, so the row is op-keyed; use
    band_provenance for the gpu/derived label.  Raises KeyError for unknown op."""
    row = _load_tensor()[_base_mnem(mnem).upper()]
    if row["band"] is not None:
        return row["mode"] == "coupled", row["band"]
    for sb in ("WGMMA_GROUP_SB", "TCGEN05_TMEM", "WGMMA_ACCUM_SB"):
        try:
            return False, async_scoreboard(sb)[0]
        except KeyError:
            continue
    return False, 0


# -- uniform-register / predicate / return-PC hazards.

@lru_cache(maxsize=1)
def _load_upred() -> list[dict]:
    """Rows of uniform_pred_hazards.tsv as dicts.

    Columns: producer consumer kind source_model ptxas_emitted gpu_min note.
    Producer/consumer are slash-joined opcode-class lists; matching is done by
    substring against those lists in `uniform_pred_hazard`."""
    out: list[dict] = []
    for cols in _read_tsv_rows(UPRED_TSV):
        if len(cols) < 6:
            continue
        out.append({
            "producer": cols[0].strip(), "consumer": cols[1].strip(),
            "kind": cols[2].strip(), "source_model": cols[3].strip(),
            "ptxas_emitted": cols[4].strip(), "gpu_min": cols[5].strip(),
        })
    return out


def _hazard_cycles(row: dict) -> int | None:
    """Best available cycle count for a uniform-hazard row: GPU-verified gpu_min
    first, then ptxas_emitted, else the source-model number; None if the row is
    a pure scoreboard/sequencing handshake (no sweepable stall).

    A cycle count is a STANDALONE integer column ("4", "13"); the non-numeric
    tokens ("SB0"/"SB" scoreboard arm-or-wait, "seq" sequencing, "-" static,
    "scoreboard(seed 2)", "(per UGPR row)") are not stalls and yield None -- so
    the stray digits inside "SB0" / "scoreboard(seed 2)" are never mistaken for
    a cycle count."""
    for col in ("gpu_min", "ptxas_emitted", "source_model"):
        val = row[col].strip()
        if val.isdigit():
            return int(val)
    return None


def uniform_pred_hazard(producer: str, consumer: str
                        ) -> tuple[str, int | None]:
    """(kind, cycles) for a uniform-datapath / predicate / return-PC hazard from
    uniform_pred_hazards.tsv.

    `producer`/`consumer` are matched as substrings of the TSV's slash-joined
    opcode-class columns (e.g. uniform_pred_hazard("UMOV", "IMAD")).  `cycles`
    is the GPU-verified gpu_min where present, else the ptxas-emitted stall, else
    the source-model band; None when the hazard is a scoreboard/sequencing
    handshake rather than a fixed stall.  Raises KeyError if no row matches."""
    pu, cu = producer.upper(), consumer.upper()
    for row in _load_upred():
        if pu in row["producer"].upper() and cu in row["consumer"].upper():
            return row["kind"], _hazard_cycles(row)
    raise KeyError(f"no uniform_pred_hazard row for {producer!r}->{consumer!r}")


# -- async warp-state scoreboards (wgmma / tcgen05 / cluster, newer arches).

@lru_cache(maxsize=1)
def _load_async() -> dict[str, dict]:
    """named-barrier -> {latency, readers, writers, arch, note}, from
    async_scoreboards.tsv.  Columns: scoreboard readers writers latency arch
    note.  A name may appear twice (Hopper + Blackwell rows); keep the first."""
    out: dict[str, dict] = {}
    for cols in _read_tsv_rows(ASYNC_TSV):
        if len(cols) < 5:
            continue
        name = cols[0].strip()
        try:
            lat = int(cols[3].strip())
        except ValueError:
            continue
        out.setdefault(name, {
            "latency": lat, "readers": cols[1].strip(),
            "writers": cols[2].strip(),
            "arch": cols[4].strip() if len(cols) > 4 else "",
        })
    return out


def async_scoreboard(name: str) -> tuple[int, str, str]:
    """(latency, readers, writers) for a named async warp-state barrier from
    async_scoreboards.tsv (WGMMA_ACCUM_SB / WGMMA_GROUP_SB / CGA_BARRIER /
    TCGEN05_TMEM / RPC).  Raises KeyError for an unknown barrier."""
    row = _load_async()[name]
    return row["latency"], row["readers"], row["writers"]


# -- the result-band map assembled from the measured TSVs (single source of
# truth).  GPU-measured completion latency wins; the few caveat overrides below
# select the RIGHT measured number when a raw row is contaminated or tier-split.

# Fire-and-forget / async-issue ops carry the ptxas critical-path WEIGHT, not
# their measured RAW-forward / round-trip latency (a store is not on a RAW
# completion path; its scoreboard is for ordering/WAR only).
_MEM_WEIGHT_OPS = {"STG", "STS", "STL", "ST", "RED"}
# Global-load tiered ops: the measured latency is tier-dependent (L1/L2/DRAM),
# so the result band uses the representative critical-path weight (300); the
# per-tier numbers stay reachable via memory_latency().
_MEM_REPRESENTATIVE_OPS = {"LDG", "LDL", "LD", "ATOMG", "ATOM", "LDGSTS"}


@lru_cache(maxsize=1)
def _measured_band() -> dict[str, int]:
    """base-mnemonic -> result band, derived purely from the measured TSVs.

    Resolution per op: tensor result band, then decoupled-scalar measured, then
    memory (weight for stores / tiered global loads, measured otherwise), with
    the documented caveat overrides applied last.  Nothing here is a literal
    cycle count -- every number is read out of a TSV row."""
    band: dict[str, int] = {}

    # tensor MMA: the inline completion band (~27 / ~26), not the HW-interlock
    # floor.  group_sb async ops (band None) carry no scalar band.
    for base, row in _load_tensor().items():
        if row["band"] is not None:
            band[base] = row["band"]

    # decoupled scalar: the measured completion latency (SFU/MIO/FMA64).  The
    # coupled exceptions are handled by class: CS2R (a clock read) keeps its
    # measured coupled floor as its band, but I2FP (the 32-bit s32->f32 coupled
    # conversion) forwards like ALU math -- its measured number is the clock-read
    # bracket floor (an artifact of the measurement harness, shared with CS2R),
    # NOT a conversion completion latency, so it is left to the ALU anchor (6).
    for base, (scbd, lat) in _load_decoupled().items():
        if scbd == "-coupled" and base == "I2FP":
            continue
        band[base] = lat

    # memory: stores/RED take the weight; tiered global loads the representative
    # weight; everything else (LDS/LDC/ATOMS/...) its measured latency.
    mem = _load_memory()
    for base, rows in mem.items():
        if base in _MEM_WEIGHT_OPS:
            band[base] = max(r["weight"] for r in rows)
        elif base in _MEM_REPRESENTATIVE_OPS:
            band[base] = max(r["weight"] for r in rows)
        else:
            band[base] = memory_latency(base)

    # -- caveat overrides (still TSV-sourced numbers, cross-referenced):
    #  * F2F (f32->f64) and I2F (s64->f64) measured 57/71 INCLUDE an FP64
    #    consumer (DADD) that inflates them; the pure conversion band is F2I's
    #    clean s32 conversion latency.  Pin F2F/I2F to the F2I measured row.
    dec = _load_decoupled()
    if "F2I" in dec:
        f2i = dec["F2I"][1]
        band["F2F"] = f2i
        band["I2F"] = f2i
    return band


def result_band(mnem: str, arch: str = "SM89") -> int:
    """Result-latency band of an op (for critical-path weighting / debug).

    Resolution is measured-first: the GPU-measured completion latency (from the
    ../ptxas-sched-full/ measurement TSVs) wins, then the ptxas scalar oracle's
    coarse internal band, then the ALU anchor (6).  The oracle's per-op band is
    its own weight (e.g. MUFU 24, POPC 13) and underestimates the real
    completion (39 / 29 measured on sm_89), so the measured tables override it.
    Every numeric band lives in a TSV row; this function only selects among
    them.  The tiny code-level fallbacks below cover ops with no TSV row at all
    (warm const-cache LDC, the coupled I2I conversion, the secondary 64-bit
    load aliases) and are clearly marked.

    `arch` selects the target arch (default sm_89).  The returned cycle count is
    the sm_89-silicon measurement -- ptxas's scalar band model is family-invariant
    and no other GPU is available to refine the value, so scalar/memory bands are
    the same across arches; the tensor band is op-keyed and shifts by op (HMMA vs
    wgmma/tcgen05).  Call band_provenance(mnem, arch) for the measured-vs-derived
    label on the target arch."""
    base = _base_mnem(mnem)
    m = _measured_band()
    if base in m:
        return m[base]
    # code-level fallbacks for ops with NO measured TSV row (clearly marked):
    if base in _BAND_FALLBACK:
        return _BAND_FALLBACK[base]
    o = _load_oracle()
    if base in o:
        return o[base]
    return 6   # default ALU band (binary anchor)


# Code-level band fallback -- ONLY for ops that have no row in any measurement
# TSV.  These are aliases / variants whose silicon number is not separately
# measured but whose CLASS weight is known from the op's pipe; kept tiny and
# explicit so the measured TSVs remain the source of truth for everything that
# IS measured.  Each value mirrors the class of a measured sibling:
#   * the global/texture load family weights at the representative 300 (the
#     measured LDG tier weight) -- LD/ATOM and the texture ops (TEX/TLD/TXD/SULD)
#     are not separately pointer-chased on sm_89;
#   * the store/RED family is fire-and-forget (weight 1, like the measured STG);
#   * LDSM mirrors the measured LDS shared band (24);
#   * the uniform aliases ULDC/S2UR mirror the measured LDC(warm)/S2R;
#   * I2I is the coupled int->int narrow (oracle weight 35 is the throughput
#     band; its coupled forwarding sits in the control/CC class, 13);
#   * OMMA is an unmeasured tensor MMA variant -> the HMMA/IMMA result band 27.
_BAND_FALLBACK = {
    # global / texture load family (representative weight, like measured LDG)
    "LD": 300, "ATOM": 300, "TEX": 300, "TLD": 300, "TXD": 300, "SULD": 300,
    # store / reduction family: fire-and-forget (like measured STG)
    "STL": 1, "ST": 1,
    # shared-memory alias of the measured LDS
    "LDSM": 24,
    # uniform-datapath aliases of the measured LDC(warm)/S2R
    "ULDC": 30, "S2UR": 30,
    # coupled int->int narrow (oracle 35 is the throughput weight)
    "I2I": 13,
    # unmeasured tensor MMA variant -> HMMA/IMMA result band
    "OMMA": 27,
}


# -- provenance: which silicon/source a result band comes from for a given arch.
# sm_89 is the only GPU we can run; its measured bands hold directly for the
# sm_8x latency-descriptor family and are the representative estimate for the
# sm_7x / sm_10x families (no GPU there, and ptxas's scalar band model is
# family-invariant).  The labels keep "measured vs derived" honest.

def band_provenance(mnem: str, arch: str = "SM89") -> str:
    """How result_band(mnem, arch)'s number is grounded for the target arch:

      "gpu"            measured on the sm_89 GPU (arch == sm_89).
      "family"         the sm_89 measurement applied within its own sm_8x
                       descriptor family (sm_80 / sm_86 / sm_90 / sm_90a).
      "inherited"      the sm_89 measurement used as the representative estimate
                       for an arch we cannot run (sm_7x / sm_10x).  The silicon
                       value may differ (e.g. Blackwell HBM vs Ada DRAM), but
                       ptxas's scheduling band model is family-invariant here.
      "binary-derived" the arch-invariant ptxas scalar-oracle band, a
                       class-derived fallback, or a static tensor-async row
                       (wgmma / tcgen05) -- read from the binaries, not the GPU.
      "anchor"         the structural ALU default (6).
    """
    base = _base_mnem(mnem)
    if base in _measured_band():            # an sm_89-silicon-measured band
        if _arch_num(arch) == 89:
            return "gpu"
        return "family" if arch_family(arch) == "sm8x" else "inherited"
    if base in _BAND_FALLBACK:
        return "binary-derived"
    if base in _load_oracle():
        return "binary-derived"
    t = _load_tensor().get(base.upper())
    if t is not None and t["band"] is None:  # group_sb async tensor (static)
        return "binary-derived"
    return "anchor"


# Per-arch class model: mnemonic -> {coupled, itype, min_wait, scbd arming,
# pipe, band}.  Parsed from the decoded SASS-ISA table.

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
        band = result_band(cur_mnem, arch)
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


# Per-arch dependency-rule join.

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
        # Skip leading comment lines (the sm_110/120/121 files carry a
        # `#`-prefixed header noting they share the sm_103 Blackwell table);
        # the first non-comment line is the column header.
        hdr: list[str] | None = None
        for ln in fh:
            if ln.startswith("#"):
                continue
            if hdr is None:
                hdr = ln.rstrip("\n").split("\t")
                continue
            vals = ln.rstrip("\n").split("\t")
            if len(vals) != len(hdr):
                continue
            rows.append({k: int(v) for k, v in zip(hdr, vals)})
    return rows


# Coupled-stall matrix: (family, prod_pipe, cons_pipe) -> issue-relative stall.
# Loaded from the TSV (differential-analysis result).

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
    `cons_pipe`.

    Resolution is most-specific first: the exact arch key (e.g. `sm80`) outranks
    the family key (`sm8x`) which outranks the family-`*` row.  Within a tie a
    *producer*-pipe rule outranks a *consumer*-pipe rule -- a CC/predicate
    producer always emits the control band regardless of the consumer, and a
    cross-domain conversion producer levies its own penalty.  Falls through to
    the same-pipe (4) / cross-pipe (5) / CC (13) / AGU (5) / CVTI (6) / SFU (4)
    structural anchors."""
    scope = (f"sm{_arch_num(arch)}", arch_family(arch), "*")
    m = _load_stall_matrix()
    # both exact, then producer-specific (outranks), then consumer-specific.
    for f in scope:
        if (f, prod_pipe, cons_pipe) in m:
            return m[(f, prod_pipe, cons_pipe)]
    for f in scope:
        if (f, prod_pipe, "*") in m:
            return m[(f, prod_pipe, "*")]
    for f in scope:
        if (f, "*", cons_pipe) in m:
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


def _selfcheck() -> int:
    """Assert the TSV-derived bands and the new accessors are sane.  Returns the
    number of failures (0 = OK).  Numbers are read from the measurement TSVs, so
    this also confirms the TSVs are present and parse."""
    fails = 0

    def chk(label: str, got, want) -> None:
        nonlocal fails
        ok = got == want
        fails += 0 if ok else 1
        print(f"  {'OK ' if ok else 'FAIL'} {label:34s} got={got!r:>14} "
              f"want={want!r}")

    print("# result_band (measured -> oracle -> anchor):")
    chk("result_band(IADD3) ALU anchor", result_band("IADD3"), 6)
    chk("result_band(MUFU) decoupled max", result_band("MUFU"), 39)
    chk("result_band(POPC) decoupled", result_band("POPC"), 29)
    chk("result_band(F2I) decoupled", result_band("F2I"), 34)
    chk("result_band(F2F) =F2I caveat", result_band("F2F"), 34)
    chk("result_band(I2F) =F2I caveat", result_band("I2F"), 34)
    chk("result_band(I2FP) ALU anchor", result_band("I2FP"), 6)
    chk("result_band(CS2R) coupled floor", result_band("CS2R"), 11)
    chk("result_band(LDG) representative", result_band("LDG"), 300)
    chk("result_band(LDS) measured", result_band("LDS"), 24)
    chk("result_band(STG) weight", result_band("STG"), 1)
    chk("result_band(HMMA) result band", result_band("HMMA"), 27)
    chk("result_band(IMMA) result band", result_band("IMMA"), 27)
    chk("result_band(BMMA) result band", result_band("BMMA"), 27)
    chk("result_band(DMMA) result band", result_band("DMMA"), 25)

    print("\n# new accessors:")
    # decoupled_latency aggregates a base mnemonic to its longest measured
    # variant: MUFU's eight transcendentals -> the 39-cyc RSQ/SQRT/LG2 worst.
    chk("decoupled_latency(MUFU.*)", decoupled_latency("MUFU.RCP"), ("SB0", 39))
    chk("decoupled_latency(CS2R)", decoupled_latency("CS2R"), ("-coupled", 11))
    chk("memory_latency(LDG) repr tier", memory_latency("LDG"), 35)
    chk("memory_latency(LDG,DRAM_miss)",
        memory_latency("LDG", tier="DRAM_miss"), 606)
    chk("memory_latency(LDG,L2)", memory_latency("LDG", tier="L2"), 152)
    chk("tensor_latency(HMMA)", tensor_latency("HMMA"), (True, 27))
    chk("tensor_latency(DMMA)", tensor_latency("DMMA"), (False, 25))
    k, c = uniform_pred_hazard("ISETP", "VOTE")
    chk("uniform_pred_hazard(ISETP,VOTE)", (k, c), ("RAW_coupled", 13))
    lat, _r, _w = async_scoreboard("CGA_BARRIER")
    chk("async_scoreboard(CGA_BARRIER).lat", lat, 6)

    print("\n# per-arch bands + provenance (sm_89 measured; family-invariant "
          "scalar/mem values, honest provenance off Ada):")
    # scalar/memory band values are family-invariant (sm_89 silicon, no other GPU)
    chk("result_band(MUFU,SM89)", result_band("MUFU", "SM89"), 39)
    chk("result_band(MUFU,SM100) value", result_band("MUFU", "SM100"), 39)
    chk("result_band(LDG,SM75) value", result_band("LDG", "SM75"), 300)
    # provenance differs by arch
    chk("provenance(MUFU,SM89) gpu", band_provenance("MUFU", "SM89"), "gpu")
    chk("provenance(MUFU,SM86) family", band_provenance("MUFU", "SM86"), "family")
    chk("provenance(MUFU,SM90) family", band_provenance("MUFU", "SM90"), "family")
    chk("provenance(MUFU,SM75) inherited",
        band_provenance("MUFU", "SM75"), "inherited")
    chk("provenance(MUFU,SM100) inherited",
        band_provenance("MUFU", "SM100"), "inherited")
    chk("provenance(LDS,SM89) gpu", band_provenance("LDS", "SM89"), "gpu")
    chk("provenance(LDG,SM100) inherited",
        band_provenance("LDG", "SM100"), "inherited")
    chk("provenance(HMMA,SM89) gpu", band_provenance("HMMA", "SM89"), "gpu")
    # tensor genuinely shifts by arch (inline coupled vs group-async)
    chk("tensor_latency(HMMA,SM89)", tensor_latency("HMMA", "SM89"), (True, 27))
    chk("provenance(TCGEN05,SM100) derived",
        band_provenance("TCGEN05", "SM100"), "binary-derived")
    chk("memory_latency(LDG,DRAM,SM100) val",
        memory_latency("LDG", tier="DRAM_miss", arch="SM100"), 606)

    print("\n" + ("SELFCHECK OK" if fails == 0 else f"{fails} CHECK(S) FAILED"))
    return fails


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--selfcheck":
        raise SystemExit(1 if _selfcheck() else 0)
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
