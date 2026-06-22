# ptxas SASS scheduling model — binary-derived artifacts

Two complementary tables ptxas uses to schedule SASS, recovered purely from the
`ptxas` binary (CUDA 13.0.88). No machine-description source is consulted.

| File | What it is | Origin in binary |
|---|---|---|
| `sched_class_table.txt` | 256 per-scheduling-class descriptors (pipe-eligibility masks, throughput class, max-stall, …) | static `.rodata` table at VMA `0x2297C00`, 72-byte stride |
| `scalar_latency_oracle.txt` | per-Ori-opcode latency bands (6 / 13 / 24 / 30 / 300) | built at runtime by `sub_738E20`, queried via `sub_8BF3A0` (`oracle+744`) |
| `extract_sched_tables.py` | reproducible extractor for both | reads `ptxas_rodata.bin` |

## sched_class_table.txt columns
`class  pipeA(hex,8B)  pipeB(hex,8B)  p0 p1 p2 p3 p4 p5 p6 p7 p8 p9 p10 p11`

Confidently-named fields: **p1** = throughput class (`{0,1,2,4,132}`),
**p5** = max-stall cycles (`{0..7}`), **p7** = the class id itself (self-reference).
`pipeA` is the per-pipe eligibility byte vector (`0xFF` = pipe N/A); `pipeB` is the
dual-issue-eligibility vector. The remaining params (p0,p2,p3,p4,p6,p8–p11) are
emitted as observed columns; their exact semantics are not asserted.

## Arch coverage of the oracle

`scalar_latency_oracle.txt` is built by `sub_738E20`, which has **no SM-version
branching** (verified: zero `cmp $0x3xxx/0x4xxx/0x5xxx/0x9xxx` in the function
body) — it produces a single flat per-Ori-opcode band table that serves the
whole sm_9x/Blackwell family, **including sm_120 / sm_121 (consumer Blackwell)
and sm_110 (Jetson Thor)**. The Ori-opcode space is shared across the lineup, so
the oracle needs no per-arch extension for the newer Blackwell targets; their
per-SM hazard nuances live in the dependency-rule / scoreboard tables (see
`decoded/ptxas-sched-full/` — sm_110/120/121 share the sm_103 Blackwell set,
sm_120 ≡ sm_121).

## Reproduce

The extractor reads a raw `.rodata` dump. Regenerate it from **your own** CUDA 13.0
`ptxas` (no NVIDIA bytes are shipped in this repo) and run:

```
objcopy -O binary --only-section=.rodata "$(command -v ptxas)" ptxas_rodata.bin
python3 extract_sched_tables.py ptxas_rodata.bin > sched_class_table.txt
```

The table lives at `.rodata` VMA `0x2297C00` in ptxas 13.0.88; `extract_sched_tables.py`
maps that to the section file offset via the section base `0x1CE2E00`. For a different
build, adjust `RODATA_BASE_VMA` / `SCHED_TBL_VMA` at the top of the script.
