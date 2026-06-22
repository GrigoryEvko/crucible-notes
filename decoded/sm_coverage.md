# SM-version coverage matrix — sm_110 / sm_120 / sm_121

Coverage audit of every per-architecture table across all `decoded/` directories
against the three newest Blackwell-class targets: **sm_110** (Thor / GB10-class,
sched-gen 9), **sm_120** and **sm_121** (consumer Blackwell, sched-gen 8).

## Method / ground truth

- Binary: `ptxas` CUDA **13.0.88** (`ptxas/ptxas`, sha256
  `daba837a…b849f2`, 37.74 MB x86-64 ELF). Cross-checked with `nvdisasm`/`cuobjdump`
  from CUDA 13.1.115. `.text`/`.rodata` use VMA == file_offset + 0x400000.
- All three SM targets (and their `a`/`f` variants) are present in this ptxas: it
  compiles `sm_120a` and emits SASS for `sm_110`/`sm_120`/`sm_121`. sm_110 requires
  PTX `.version 9.0`; sm_120/121 accept `8.8`.
- **sm_120 ≡ sm_121** for SASS/encoding (byte-identical `.text`), and `sm_120a`
  shares `sm_120`'s tables. They differ only in the ELF container: the real-SM byte
  in `e_flags` and one `.nv.compat` finalizer field (see ptxas-elf-output below).
- **sm_110 is distinct** at the container level: it carries a GB10B silicon
  workaround (`__nv_reservedSMEM_gb10b_war_var` + `.nv.shared.reserved.0`).

Status legend: **covered** = already present before this pass; **extracted-now** =
added in this pass; **arch-independent** = data is not keyed on SM version (PTX-level,
ptxas-internal, or family-keyed in a way that already subsumes 110/120/121); **family**
= keyed by SM *family* (sm_10x) which already includes all three.

## Matrix (all decoded dirs × {sm_110, sm_120, sm_121})

| Directory | Per-arch keyed? | sm_110 | sm_120 | sm_121 | Highest SM / note |
|---|---|---|---|---|---|
| **ptxas-elf-output** (mine) | yes (container fields) | **extracted-now** | **extracted-now** | **extracted-now** | new `per_arch_sm110_120_121.tsv` + `e_flags` row fixed (was missing sm_110) |
| **ptxas-gap-closure** (mine) | yes (version-code model) | covered | covered | covered | F5-Targets: gen-9 var0=sm_100, var1=sm_110, var3=sm_103, var4=sm_120, var5=sm_121 |
| ptxas-knobs-builtins (mine) | family (sm7x/sm10x cols) | family | family | family | `opcode_master_canonical.tsv` `pipeline_flags_sm10x` subsumes all gen≥100 |
| ptxas-ir (mine) | no (sm_gen = opcode-intro era) | arch-independent | arch-independent | arch-independent | opcode enum is a single table; `sm_gen` labels first-appearance generation |
| ptxas-fp-debug (mine) | no | arch-independent | arch-independent | arch-independent | FP fold engine / DWARF / SASS printer — 0 SM mentions |
| ptxas-driver (mine) | no | arch-independent | arch-independent | arch-independent | driver/PhaseManager/O0-O5/recipe DSL — 0 SM mentions |
| ptxas-passes-detail (mine) | no | arch-independent | arch-independent | arch-independent | 14 phase deep-dives — 0 SM mentions |
| ptxas-passes (mine) | no | arch-independent | arch-independent | arch-independent | 159-phase pipeline — phase identity, not arch |
| ptxas-targets | yes (per-SM enum) | covered | covered | covered | `sm_id_enumeration` / `supported_targets` / `sm_scheduling_seeds`: 110/Thor/gen9, 120/121/Blackwell/gen8. (`sm_version_codes.tsv` stops at sm_90f.) |
| nvdisasm-sass-isa | yes (one file per SM) | covered | covered | covered | `sass_isa_SM110/SM120/SM121.txt`; SM120≡SM121 byte-identical |
| sass-tools | partly (stall matrix) | family | covered | covered | `coupled_stall_matrix.tsv`: sm10x family incl 110; explicit sm120/sm121 override rows |
| ptxas-encoding-full | per-family band | **MISSING** | covered | covered | `tier2_modifiers.tsv` has consumer band sm_120-121; no Thor band; `per_sm_handler_dispatch` stops at sm100 |
| ptxas-sched-full | yes (per-SM/family) | **MISSING** | **MISSING** | **MISSING** | stops at sm_103; sm_10x family table (VMA 0x226C880) currently = sm_100/103 only |
| ptxas-isel | yes (sm_gen col) | **MISSING** | **MISSING** | **MISSING** | `opcode_to_encoding.tsv` sm_gen stops at sm_90 |
| ptxas-regalloc | yes (family col) | family | family | family | `register_classes.tsv` sm_generation = {sm_7x, sm_8x, sm_10x}; sm10x subsumes all three |
| ptxas-scheduling | no (representative) | arch-independent | arch-independent | arch-independent | single sched-class table (sm_8x family) at VMA 0x2297C00 |
| ptxas-mercury | no (container/reloc) | arch-independent | arch-independent | arch-independent | Mercury capsule / R_MERCURY relocs / finalizer; SM-aware mechanism but no per-SM table |
| ptxas-instr-defs | no | arch-independent | arch-independent | arch-independent | instruction registry; no arch/capability column |
| ptxas-messages | no | arch-independent | arch-independent | arch-independent | diagnostic catalog keyed by msg id |
| ptxas-tokens | no | arch-independent | arch-independent | arch-independent | PTX lexer vocabulary |
| ptxas-pseudo-instructions | family | family | family | family | `macro_catalog.tsv` arch col = sm10x (Blackwell), not per-SM |
| ptxas-ptx-macro-pool | no (single pool) | n/a | n/a | n/a | one arch-independent lowering pool; inline tokens top out at sm_100 / family sm_10x |
| nvlink-ptx-macro-pool | no (single pool) | n/a | n/a | n/a | byte-similar to ptxas pool; same SM-token set |
| cicc-tables | no | n/a | n/a | n/a | LLVM/Clang/PTX name dictionaries; no SM keying |
| reference | no (3rd-party) | n/a | n/a | n/a | redplait extractor + decrypted pool snapshot; not redistributed |

## What this pass changed (my scope only)

- **ptxas-elf-output/header_notes_compat.tsv** — added `sm_110->0x6e(110)` to the
  `e_flags` real-SM row and `sm_110` to the Blackwell-variant (`0x02`) row (both were
  omitting sm_110).
- **ptxas-elf-output/per_arch_sm110_120_121.tsv** — new file: every container field
  measured per arch (`e_flags`, `.note.nv.cuinfo`, the full `.nv.compat` attribute
  values, the GB10B reserved-SMEM WAR symbol, symtab counts, min PTX version).
- **ptxas-elf-output/README.md** — documented the two distinguishing findings:
  `CAN_FASTPATH_FINALIZE` = 0x50 on sm_120/120a but 0x00 on sm_121/sm_110 (so the
  sm_120≡sm_121 alias holds for SASS but *not* for the `.nv.compat` finalizer record),
  and the sm_110-only GB10B workaround symbol.

## Genuinely arch-independent directories (no per-arch gap)

Within my edit scope: **ptxas-fp-debug, ptxas-driver, ptxas-passes-detail, ptxas-passes,
ptxas-ir** are arch-independent (PTX-level / ptxas-internal — phases, FP folding, DWARF,
opcode enum). **ptxas-knobs-builtins** is family-keyed (the `pipeline_flags_sm10x` column
already covers gen≥100). No per-arch sm_110/120/121 files were fabricated for these.

Across the read-only dirs, arch-independent: ptxas-mercury, ptxas-instr-defs,
ptxas-messages, ptxas-tokens, cicc-tables, the three PTX-macro pools, and reference.

## Remaining gaps (NOT in my edit scope — owned by sibling agents)

- **ptxas-sched-full**: sm_110/120/121 absent (stops at sm_103); the sm_10x family
  latency/sched-class table only covers sm_100/103 today.
- **ptxas-encoding-full**: no Thor/sm_110 band (consumer sm_120-121 band exists).
- **ptxas-isel**: sm_gen stops at sm_90.
- **ptxas-regalloc**: only family granularity (sm_10x) — adequate if family-keying is
  intended, otherwise a per-SM refinement gap.
