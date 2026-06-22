# ptxas Register Allocation / occupancy tables — decoded data

White-hat RE of NVIDIA **ptxas** (CUDA **13.0.88**, binary at `ptxas/ptxas`,
sha256 `daba837a68265cae38c832d13399b61dab811891de9b8914defddef143b849f2`). This directory documents
the register-allocator's per-architecture register-class tables, the register-file / occupancy budget
descriptors, the calling-convention (ABI) constraints, and the FP64 throughput class that drives
dependent-op scheduling.

All findings are **binary-derived** (VMAs are ptxas v13.0.88; `.text`/`.rodata` VMA = file offset + 0x400000),
corroborated with live `ptxas -v` / nvdisasm round-trips.

## Register-class generations and per-arch binding

ptxas keeps **three** register-class tables, selected by the table-population function `sub_ABF590` from
an internal 16-bit arch-selector code (returned by each per-target class' vtable slot-0 thunk):

| table VMA | records | stride | selector codes | covers |
|---|---|---|---|---|
| 0x2274180 | 24 | 0x40 | 0x3001–0x3005 | Ampere / Ada (sm_8x) |
| 0x224FE80 | 97 | 0x40 | 0x4000 / 0x4001 | **Hopper (sm_90 only)** |
| 0x21FB680 | 150 | 0x40 | 0x5000 / 0x5001 / 0x5003 / 0x5004 / 0x5005 | **Blackwell family: sm_100 / sm_101 / sm_103 / sm_120 / sm_121** |

This **corrects** the positional generation labels in the legacy `register_classes.tsv` (which guessed
0x21FB680 = "sm_7x"): the binary's dispatch (verified by disassembling `sub_ABF590` and reading the per-arch
vtable selector thunks at 0xb08050–0xb082e0) shows the **entire consumer+datacenter Blackwell family shares
the 0x21FB680 class table** (selectors 0x5000–0x5005), while sm_90 alone uses 0x224FE80.

- sm_120 → selector **0x5004**, sm_121 → **0x5005** (vtable thunks @0xb08290 / 0xb082e0) — both bind 0x21FB680.
- **sm_110 (Thor) has no dedicated arch class** in this build: it is recognized as an input
  (`__CUDA_ARCH__=1100`, `(profile_sm_110)->isaClass` assert, off-target cap-bit 0x02) but is **not** served by
  `sub_ABF590`/`sub_ABF250` — it instantiates by aliasing an existing Blackwell-family class. There is no
  separate register-class or register-file table for Thor.

### Register-file params — the concrete consumer-Blackwell difference (`sub_ABF250`)

The register-file param selector `sub_ABF250` writes `mode_flag@0x68`, `reg_limit@0x60`, `reg_limit2@0x64`:

| arch group | selector | mode_flag | reg_limit (0x60) | reg_limit2 (0x64) |
|---|---|---|---|---|
| sm_90 (Hopper) + sm_8x | 0x4000/0x4001/0x3xxx | 1 | 0xff (255) | **0x3f (63)** |
| Blackwell family (sm_100/101/103/120/121) | 0x5000–0x5005 | **2** | 0xff (255) | **0xff (255)** |

So consumer Blackwell sm_120/121 (and the rest of the family) run the register file in **mode_flag = 2** with
the second limit field raised to **255** (vs 63 on Hopper/Ampere). The primary GPR budget is unchanged —
confirmed empirically: across sm_90 / sm_100 / sm_103 / sm_110 / sm_120 / sm_121 `ptxas -v` reports the same
max 255 GPR, lower bound 24, allocation granularity 4, and identical spill behavior (`--maxrregcount=16` →
96-byte stack frame on every one). See `per_arch_regalloc_binding.tsv`.

## What actually differs across Blackwell: FP64 throughput, not the register file

The register *budget* is uniform, but the **FP64 throughput class** differs and drives the scheduler's
dependent-op spacing (`fp64_throughput_class.tsv`). Probed with an FP64-heavy kernel
(66 DFMA / 19 DMUL / 5 DADD), measuring the minimum offset gap between dependent DFMAs:

| arch | min dependent-DFMA gap | class |
|---|---|---|
| sm_90 (Hopper) | 0x10 (back-to-back) | FAST — full-rate FP64 |
| sm_100 (datacenter Blackwell) | 0x10 | FAST — full-rate FP64 |
| sm_103 (Blackwell Ultra) | 0x50 (4 stall slots) | RATE-LIMITED FP64 |
| sm_110 (Jetson Thor) | 0x50 | RATE-LIMITED FP64 |
| sm_120 (consumer RTX 50xx / Pro) | 0x50 | RATE-LIMITED FP64 |
| sm_121 (DGX Spark) | 0x50 | RATE-LIMITED FP64 |

Instruction *selection* is identical on all six (native DMUL/DADD/DFMA — no FP64 emulation), but the
rate-limited parts pad ~4 stall slots between dependent doubles (the FP64-heavy kernel finalizes to
~624–632 SASS instructions with ~311–314 NOPs, vs 328 instructions / 10 NOPs on the fast parts).
sm_120 and sm_121 are byte-identical in this respect.

## Files

| File | Contents |
|---|---|
| `register_classes.tsv` | per-generation register-class table dump (sm_7x / sm_8x / sm_10x), 0x40-stride records |
| `register_class_summary.tsv` | per-generation class-count summary |
| `register_file_config.tsv` | occupancy/reg-budget curve descriptors (0x21CE6A0 width-64, 0x21CEE60 width-256) |
| `register_file_limits.tsv` | register-file header constants (gpr/predicate/uniform/barriers) |
| `register_id_arrays.tsv` | physical register-id range arrays (bank<<16\|reg) |
| `operand_regcount_matrix.tsv` | operand cost / base-size matrices used by the allocator |
| `abi.tsv` | calling-convention constraints (reserved R0-R3, param/retaddr/scratch rules) |
| `occupancy_constants.tsv` | sm90 regfile/granularity/barrier param structs |
| `per_arch_regalloc_binding.tsv` | per-arch class-table + regfile-param binding (sm_90..sm_121) |
| `fp64_throughput_class.tsv` | per-arch FP64 throughput class + dependent-DFMA spacing |

## Confidence

- **Uniform register budget across Blackwell+Thor, sm_10x table binding, FP64 throughput split:** HIGH —
  empirical `ptxas -v` / nvdisasm measurements plus the recovered class-table dispatch.
- **Internal selector-code constants (0x4000/0x4001):** MEDIUM-HIGH — recovered from the population
  function and per-arch vtable thunks; the exact selector value per new sub-arch is generation-shared.
