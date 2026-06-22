#!/usr/bin/env python3
# nvopen-tools -- SASS reverse-engineering tooling.  Our own extractor code; the
# numeric facts are read straight from the ptxas binary's .rodata.  No NVIDIA
# source, machine-description, or internal symbol is consulted.
"""
Extract the sm_11x (Jetson Thor) / sm_12x (consumer Blackwell) per-SM scheduling
tables from ptxas (CUDA 13.0.88, sha256 daba837a..849f2).

Why this script exists
----------------------
The prior full-coverage pass (render_sched_full.py + sm_coverage_summary.tsv)
stopped at sm_103.  This script closes the gap for sm_110 / sm_120 / sm_121.

What the binary actually contains (verified by exhaustive .rodata scan + by
disassembling the per-SM table installer at .text 0xABF590):

  * .rodata holds EXACTLY three 72-B latency-descriptor tables (sm_7x @0x2245060,
    sm_8x @0x2297C00, sm_10x @0x226C880), eleven 40-B dependency-rule tables, and
    seven 88-B scoreboard-config tables.  A full-rodata scan finds NO additional
    >=200-entry 72-B or 40-B table and NO additional scoreboard table -- there is
    no dedicated sm_11x/sm_12x table byte-region.

  * The installer `sub_ABF590` dispatches on an internal family-enum and, in its
    Blackwell branch, hard-codes only two cases:
        enum 0x4000 -> lat10x(0x226C880) + dep_sm100(0x2268440) + sb_sm100(0x2266A60)
        enum 0x4001 -> lat10x(0x226C880) + dep_sm103(0x2262720) + sb_sm103(0x2261740)
    (decoded byte-exact from the function body; see README).

  * Empirically (this exact ptxas, nvdisasm 13.1, identical PTX): sm_120 and
    sm_121 emit BYTE-IDENTICAL SASS (only the .target label differs); their FP64
    ops (DFMA/DADD) are DECOUPLED onto a hardware dependency barrier like sm_103
    and the dependent-DFMA chain is rate-gated to one issue per 0x50 -- whereas
    sm_100 keeps DFMA/DADD COUPLED and issues the chain back-to-back at 0x10.
    So sm_120/sm_121 resolve to the **sm_103** (enum 0x4001) Blackwell tables,
    not sm_100.  sm_110 (Thor) likewise decouples FP64 like sm_103 and resolves
    to the sm_103 dep/scoreboard set; it routes through the same Blackwell lat10x
    latency family but makes different scheduling/reg-alloc choices (same SASS
    length, different order).  See blackwell_consumer_stall_deltas.tsv /
    measure_blackwell_deltas.py for the reorder-invariant per-arch divergences.

Therefore sm_110/sm_120/sm_121 do not own new bytes; they SHARE the sm_103
Blackwell hardware tables.  This script re-reads those exact bytes from .rodata
and emits them under the sm_110/sm_120/sm_121 names so downstream tools have a
direct per-arch file, with a provenance header recording the sharing.

Reproduce
---------
    objcopy -O binary --only-section=.rodata "$(command -v ptxas)" ptxas_rodata.bin
    python3 extract_blackwell_consumer.py ptxas_rodata.bin

For a different ptxas build, re-confirm the VMAs from the installer at 0xABF590
and adjust the constants below.
"""
from __future__ import annotations

import os
import struct
import sys

# ---------------------------------------------------------------- VMAs / layout
RODATA_BASE_VMA = 0x1CE2E00          # .rodata section VMA (== file offset 0 of the
                                     # objcopy'd section dump)

# Blackwell shared tables that sm_120/sm_121 (and, for the latency family, sm_110)
# resolve to.  enum 0x4001 case of the installer sub_ABF590.
LAT10X_VMA       = 0x226C880         # 72-B latency descriptors, 430 classes
LAT10X_COUNT     = 430
LAT_STRIDE       = 72

DEP_SM103_VMA    = 0x2262720         # 40-B dependency rules, 430 classes
DEP_SM103_COUNT  = 430
DEP_STRIDE       = 40

SB_SM103_VMA     = 0x2261740         # 88-B scoreboard configs, 46 entries
SB_SM103_COUNT   = 46
SB_STRIDE        = 88

# Arches that share these bytes (verified above).  sm_120 == sm_121 byte-identical;
# both == sm_103 hardware tables.  sm_110 shares the lat10x latency family.
SHARED_ARCHES    = ("sm_110", "sm_120", "sm_121")

OUT = os.path.dirname(os.path.abspath(__file__))


def _off(vma: int) -> int:
    return vma - RODATA_BASE_VMA


# --------------------------------------------------------------- latency table
def read_latency(rodata: bytes) -> list[tuple]:
    """430 x 72-B sched-class descriptors: (idx, class_id, pipeA_hex, pipeB_hex, p0..p11)."""
    rows = []
    base = _off(LAT10X_VMA)
    for i in range(LAT10X_COUNT):
        r = rodata[base + i * LAT_STRIDE: base + (i + 1) * LAT_STRIDE]
        class_id = struct.unpack_from("<I", r, 0)[0]
        pipeA = r[8:16].hex()
        pipeB = r[16:24].hex()
        # sched params kept as signed i32 -- matches the convention in
        # latency_table_sm10x.tsv (p0 is a packed flag word whose high bit shows
        # as a negative value); the bits are identical to the unsigned reading.
        params = list(struct.unpack_from("<12i", r, 24))
        rows.append((i, class_id, pipeA, pipeB, *params))
    return rows


# ------------------------------------------------------------- dependency rules
DEP_HDR = ["idx", "unit_id", "rule_type", "latency", "throughput_inv",
           "barrier_latency", "barrier_throughput", "read_latency",
           "write_latency", "stall_cycles", "issue_slots"]


def read_dependency(rodata: bytes) -> list[tuple]:
    """430 x 40-B dependency rules (10 i32 each)."""
    rows = []
    base = _off(DEP_SM103_VMA)
    for i in range(DEP_SM103_COUNT):
        r = rodata[base + i * DEP_STRIDE: base + (i + 1) * DEP_STRIDE]
        vals = struct.unpack_from("<10i", r, 0)
        rows.append((i, *vals))
    return rows


# ------------------------------------------------------------ scoreboard configs
SB_HDR = ["idx", "triplet_count", "sb_id", "threshold", "mask"]


def read_scoreboard(rodata: bytes) -> list[tuple]:
    """46 x 88-B scoreboard configs: 7 triplets {sb_id, threshold, mask} + count@+84.

    Flattened to one row per active triplet (count rows per entry); the Blackwell
    sm_103 schema uses a single triplet per config with a wider pipe mask."""
    rows = []
    base = _off(SB_SM103_VMA)
    for i in range(SB_SM103_COUNT):
        r = rodata[base + i * SB_STRIDE: base + (i + 1) * SB_STRIDE]
        words = struct.unpack_from("<22i", r, 0)
        count = words[21]                      # +84
        triplets = words[:21]                  # 7 x {sb_id, threshold, mask}
        n = max(0, min(count, 7))
        if n == 0:
            rows.append((i, count, "", "", ""))
        for t in range(n):
            sb_id = triplets[t * 3 + 0]
            thr = triplets[t * 3 + 1]
            mask = triplets[t * 3 + 2]
            rows.append((i, count, sb_id, thr, mask))
    return rows


# ---------------------------------------------------------------------- output
def _write(name: str, header: list[str], rows: list[tuple], note: str) -> None:
    path = os.path.join(OUT, name)
    with open(path, "w") as f:
        f.write(note.rstrip("\n") + "\n")
        f.write("\t".join(header) + "\n")
        for r in rows:
            f.write("\t".join(str(c) for c in r) + "\n")
    print(f"  wrote {name:34s} {len(rows):4d} rows")


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else "ptxas_rodata.bin"
    rodata = open(path, "rb").read()

    lat = read_latency(rodata)
    dep = read_dependency(rodata)
    sb = read_scoreboard(rodata)

    lat_hdr = ["idx", "class_id", "pipeA_hex", "pipeB_hex",
               "p0_flags", "p1_tput", "p2", "p3", "p4", "p5_maxstall",
               "p6", "p7_self", "p8", "p9", "p10", "p11"]

    # The sm_12x latency descriptors ARE the sm_10x family table (lat10x @0x226C880);
    # sm_120 == sm_121 and both resolve to it.  Emitted once under the sm_12x name.
    _write(
        "latency_table_sm12x.tsv", lat_hdr, lat,
        f"# sm_12x (consumer Blackwell: sm_120/sm_121) 72-B sched-class descriptors\n"
        f"# == the sm_10x latency family @ .rodata VMA 0x{LAT10X_VMA:X} ({LAT10X_COUNT} classes).\n"
        f"# Binary-derived (ptxas 13.0.88).  sm_120 and sm_121 install this exact table\n"
        f"# (installer sub_ABF590 enum 0x4001, shared with sm_100/sm_103); sm_120 == sm_121\n"
        f"# byte-identical SASS verified empirically.  Identical bytes to latency_table_sm10x.tsv.")

    # sm_11x (Thor) shares the same Blackwell latency family for the latency model.
    _write(
        "latency_table_sm11x.tsv", lat_hdr, lat,
        f"# sm_11x (Jetson Thor: sm_110) 72-B sched-class descriptors\n"
        f"# == the sm_10x latency family @ .rodata VMA 0x{LAT10X_VMA:X} ({LAT10X_COUNT} classes).\n"
        f"# Binary-derived (ptxas 13.0.88).  sm_110 (gen 'Thor') routes through the Blackwell\n"
        f"# lat10x latency family via the installer; its codegen is a distinct generation but\n"
        f"# it adds no new 72-B latency table.  Identical bytes to latency_table_sm10x.tsv.")

    for arch in SHARED_ARCHES:
        _write(
            f"dependency_rules_{arch}.tsv", DEP_HDR, dep,
            f"# {arch} 40-B dependency rules ({DEP_SM103_COUNT} classes).\n"
            f"# Resolved to the sm_103 Blackwell dependency-rule table @ .rodata VMA\n"
            f"# 0x{DEP_SM103_VMA:X} (installer sub_ABF590 enum 0x4001).  Binary-derived\n"
            f"# (ptxas 13.0.88).  sm_120 == sm_121 byte-identical; both resolve to sm_103\n"
            f"# (FP64 DFMA/DADD decoupled like sm_103 -- not coupled like sm_100;\n"
            f"# dependent-DFMA issue gap 0x50 not 0x10 -- verified empirically).\n"
            f"# Identical bytes to dependency_rules_sm_103.tsv.")
        _write(
            f"scoreboard_configs_{arch}.tsv", SB_HDR, sb,
            f"# {arch} 88-B scoreboard configs ({SB_SM103_COUNT} entries).\n"
            f"# Resolved to the sm_103 Blackwell scoreboard-config table @ .rodata VMA\n"
            f"# 0x{SB_SM103_VMA:X} (installer sub_ABF590 enum 0x4001).  Binary-derived\n"
            f"# (ptxas 13.0.88).  Single-triplet {{sb_id, threshold, mask}} per config\n"
            f"# (sm_103 Blackwell schema).  Identical bytes to scoreboard_configs_sm_103.tsv.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
