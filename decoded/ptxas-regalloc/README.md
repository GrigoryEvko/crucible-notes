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

This **corrects** the positional generation labels that the legacy `register_classes.tsv` /
`register_class_summary.tsv` originally carried (they had `sm_7x`↔`sm_10x` **swapped**: 0x21FB680 mislabeled
"sm_7x", 0x224FE80 mislabeled "sm_10x"). The binary's dispatch — verified by disassembling `sub_ABF590`
(which writes the class-table pointer to `ctx+0x98`) and reading the per-arch vtable selector thunks at
0xb08050–0xb082e0 — shows the **entire consumer+datacenter Blackwell family shares the 0x21FB680 class
table** (selectors 0x5000–0x5005), while sm_90 alone uses 0x224FE80 and Ampere/Ada (sm_8x) uses 0x2274180.
**Both data files have now been relabeled** to the proven binding (0x224FE80→`sm_90`, 0x2274180→`sm_8x`,
0x21FB680→`sm_blackwell`).

Disasm-proven `ctx+0x98` (class-table) bindings, exhaustive over `sub_ABF590`:

| selector(s) | class table @ctx+0x98 | covers |
|---|---|---|
| 0x3003 / 0x3004 / 0x3005 | 0x2274180 (24 rec) | Ampere/Ada (sm_8x) |
| 0x4000 / 0x4001 | 0x224FE80 (97 rec) | Hopper (sm_90) |
| 0x5000 / 0x5001 / 0x5003 / 0x5004 / 0x5005 | 0x21FB680 (150 rec) | Blackwell family (sm_100/101/103/120/121) |

Every vtable thunk return was byte-verified: 0xb08050→0x4000, 0xb08140→0x5000, 0xb081c0→0x5001,
0xb08210→0x5003, 0xb08290→0x5004, 0xb082e0→0x5005.

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
the second limit field raised to **255** (vs 63 on Hopper/Ampere). The values above are byte-exact from the
`sub_ABF250` disassembly (`mov $imm, 0x68/0x60/0x64(%rsi)` per selector). The primary GPR *budget* is
unchanged across the family — confirmed empirically: across sm_90 / sm_100 / sm_103 / sm_110 / sm_120 /
sm_121 `ptxas -v` reports identical register counts on the same kernel and identical zero-spill behavior
under `--maxrregcount=16` (0 bytes stack frame / 0 spill on every one). The only `ptxas -v` divergence
observed was a *scheduled* register-count difference on a wide reduction kernel (sm_90/100/103 → 32 regs,
sm_110/120/121 → 38 regs at default budget), which collapses to an identical 23 regs once the budget is
clamped — a scheduling artifact, not a register-file capacity difference. See `per_arch_regalloc_binding.tsv`.

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
rate-limited parts pad ~4 stall slots between dependent doubles. Re-measured with a pure 64-deep
dependent-DFMA chain finalized to SASS: the **FAST** parts (sm_90, sm_100) emit 88 instructions / 12 NOPs
with a **0x10** byte gap between consecutive DFMAs (back-to-back); the **RATE-LIMITED** parts (sm_103,
sm_110, sm_120, sm_121) emit 336 instructions / 260 NOPs with a **0x50** gap (1 DFMA + 4 NOPs). The DFMA
offset deltas were read directly from nvdisasm output and are byte-exact 0x10 vs 0x50. sm_120 and sm_121
are byte-identical in this respect (nvdisasm encoding diff = 0); sm_110 and sm_103 are likewise
encoding-identical for this kernel.

## Files

| File | Contents |
|---|---|
| `register_classes.tsv` | per-arch register-class table dump (sm_90 / sm_8x / sm_blackwell), 0x40-stride records — labels corrected to the disasm-proven binding |
| `register_class_summary.tsv` | per-arch class-count summary (table_va + selectors + coverage) — labels corrected |
| `register_file_config.tsv` | occupancy/reg-budget curve descriptors (0x21CE6A0 width-64, 0x21CEE60 width-256) |
| `register_file_limits.tsv` | register-file header constants (gpr/predicate/uniform/barriers) |
| `register_id_arrays.tsv` | physical register-id range arrays (bank<<16\|reg) |
| `operand_regcount_matrix.tsv` | operand cost / base-size matrices used by the allocator |
| `abi.tsv` | calling-convention constraints (reserved R0-R3, param/retaddr/scratch rules) |
| `occupancy_constants.tsv` | sm90 regfile/granularity/barrier param structs |
| `per_arch_regalloc_binding.tsv` | per-arch class-table + regfile-param binding (sm_90..sm_121) |
| `fp64_throughput_class.tsv` | per-arch FP64 throughput class + dependent-DFMA spacing |

## Confidence

- **Uniform register budget across Blackwell+Thor, class-table binding, FP64 throughput split:** HIGH —
  empirical `ptxas -v` / nvdisasm measurements plus the disasm-decoded class-table dispatch (`sub_ABF590`)
  and regfile-param selector (`sub_ABF250`); FP64 0x10-vs-0x50 DFMA gap measured directly from SASS.
- **Internal selector-code constants (0x4000/0x4001/0x5000–0x5005):** HIGH — byte-verified: every per-arch
  vtable selector thunk (0xb08050–0xb082e0) returns its exact 16-bit code, and every code's `ctx+0x98`
  class-table store and `ctx+0x60/0x64/0x68` regfile params were traced through both dispatch functions.
