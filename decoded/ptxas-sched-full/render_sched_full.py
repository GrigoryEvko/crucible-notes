#!/usr/bin/env python3
"""
Render the FULL per-SM ptxas SASS scheduling model as clean TSV artifacts.

Binary-derived (ptxas v13.0.88, CUDA 13.0.88). All inputs are the JSON tables
extracted from the ptxas binary's .rodata; this script only reshapes them into
flat, diff-able TSV. No external machine-description source is consulted.

Produces, in the same directory:
  latency_table_sm7x.tsv      619 classes  (sm_60/70/72/75)
  latency_table_sm8x.tsv      256 classes  (sm_80/86/89/90/90a)
  latency_table_sm10x.tsv     430 classes  (sm_100/103)
  dependency_rules_<sm>.tsv   one file per SM (11 SMs)
  dependency_rules_all.tsv    long-form union, sm column prepended
  scoreboard_configs_<sm>.tsv one file per SM (7 SMs)
  opcode_pipeline_map.tsv     Ori opcode -> pipeline flags (sm_7x, sm_10x)
  scalar_latency_oracle.tsv   per-Ori-opcode latency band (binary-corrected)

Usage:
    python3 render_sched_full.py [/path/to/ptxas/extracted]
"""
import json, os, sys

EXTRACT = sys.argv[1] if len(sys.argv) > 1 else \
    os.path.join(os.path.dirname(__file__), "..", "..", "ptxas", "extracted")
OUT = os.path.dirname(os.path.abspath(__file__))


def load(name):
    with open(os.path.join(EXTRACT, name)) as f:
        return json.load(f)


def w(name, header, rows):
    path = os.path.join(OUT, name)
    with open(path, "w") as f:
        f.write("\t".join(header) + "\n")
        for r in rows:
            f.write("\t".join(str(c) for c in r) + "\n")
    print(f"  wrote {name:34s} {len(rows):4d} rows")


# ---------------------------------------------------------------- latency tables
def render_latency():
    d = load("per_sm_latency_tables.json")["per_sm_latency_tables"]
    out = {"sm_7x_shared": "latency_table_sm7x.tsv",
           "sm_8x_shared": "latency_table_sm8x.tsv",
           "sm_10x_shared": "latency_table_sm10x.tsv"}
    hdr = ["idx", "class_id", "pipeA_hex", "pipeB_hex",
           "p0_flags", "p1_tput", "p2", "p3", "p4", "p5_maxstall",
           "p6", "p7_self", "p8", "p9", "p10", "p11"]
    for grp, fn in out.items():
        info = d[grp]
        rows = []
        for e in info["entries"]:
            pa = bytes(e["pipe_masks_a"]).hex()
            pb = bytes(e["pipe_masks_b"]).hex()
            rows.append([e["index"], e["unit_id"], pa, pb] + e["sched_params"])
        w(fn, hdr, rows)


# ------------------------------------------------------------- dependency rules
DEP_HDR = ["idx", "unit_id", "rule_type", "latency", "throughput_inv",
           "barrier_latency", "barrier_throughput", "read_latency",
           "write_latency", "stall_cycles", "issue_slots"]
DEP_FIELDS = DEP_HDR[1:]


def render_dep():
    d = load("per_sm_dependency_rules.json")["per_sm_dependency_rules"]
    allrows = []
    for sm, info in d.items():
        rows = []
        for e in info["entries"]:
            row = [e["index"]] + [e[f] for f in DEP_FIELDS]
            rows.append(row)
            allrows.append([sm] + row)
        w(f"dependency_rules_{sm}.tsv", DEP_HDR, rows)
    w("dependency_rules_all.tsv", ["sm"] + DEP_HDR, allrows)


# ------------------------------------------------------------ scoreboard configs
def render_scoreboard():
    d = load("per_sm_scoreboard_configs.json")["per_sm_scoreboard_configs"]
    hdr = ["idx", "triplet_count", "sb_id", "threshold", "mask"]
    for sm, info in d.items():
        rows = []
        for e in info["entries"]:
            tc = e["triplet_count"]
            if not e["triplets"]:
                rows.append([e["index"], tc, "", "", ""])
            for t in e["triplets"]:
                rows.append([e["index"], tc, t["scoreboard_id"],
                             t["threshold"], t["mask"]])
        w(f"scoreboard_configs_{sm}.tsv", hdr, rows)


# ----------------------------------------------------------- opcode pipeline map
def render_pipeline():
    d = load("opcode_pipeline_map.json")["opcode_pipeline_map"]
    rows = []
    for blk in d:
        gen = blk["sm_generation"]
        for e in blk["entries"]:
            rows.append([gen, e["opcode_id"], e["pipeline_flags"]])
    w("opcode_pipeline_map.tsv", ["sm_gen", "ori_opcode", "pipeline_flags"], rows)


# ----------------------------------------------------------- scalar oracle (corrected)
# Binary-correct bands transcribed from sub_738E20 switch (result = idx-16):
ORACLE = {
    300: [16, 223, 228],
    24:  [17, 42, 53, 55, 91, 183, 195],
    13:  [38, 59, 60, 62, 67, 78, 79, 106, 162, 180, 182, 192, 194, 199, 215, 221],
    30:  [88, 89],
}
ORACLE_DEFAULT_ALU = 6
ORACLE_DEFAULT_MEM = 300        # opcodes with property bit 0x40 set
ORACLE_SCOREBOARD = 5           # oracle+2212[op] when property bit 0x02 set
ORACLE_N = 355


def render_oracle():
    m = load("opcode_master.json")["opcode_master"]["entries"]
    idx2 = {e["index"]: e["mnemonic"] for e in m}
    band = {}
    for lat, ops in ORACLE.items():
        for o in ops:
            band[o] = lat
    rows = []
    for op in range(ORACLE_N):
        lat = band.get(op)
        if lat is None:
            note = "default (ALU=6 / MEM=300 by property bit 0x40)"
            latcol = ""
        else:
            note = ""
            latcol = lat
        rows.append([op, latcol, idx2.get(op, ""), note])
    w("scalar_latency_oracle.tsv",
      ["ori_opcode", "latency_cycles", "mnemonic_best_effort", "note"], rows)


if __name__ == "__main__":
    print("Rendering full per-SM ptxas scheduling model -> %s" % OUT)
    render_latency()
    render_dep()
    render_scoreboard()
    render_pipeline()
    render_oracle()
    print("done.")
