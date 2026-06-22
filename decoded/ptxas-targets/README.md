# ptxas Target / SM-version tables — decoded data

White-hat RE of NVIDIA **ptxas** (CUDA **13.0.88**, binary at `ptxas/ptxas`,
sha256 `daba837a68265cae38c832d13399b61dab811891de9b8914defddef143b849f2`). This directory documents
how ptxas identifies and gates GPU targets: the SM enumeration, the per-arch version codes, the
SASS-ELF flag encoding, the scheduling-generation seeds, and instruction/feature legality gating.

All findings are **binary-derived** (addresses are ptxas v13.0.88 VMAs; `.text`/`.rodata` VMA = file
offset + 0x400000). Numeric facts cross-checked against the toolkit where possible (e.g. `__CUDA_ARCH__`
values confirmed independently via nvcc 13.1).

## Two distinct "version code" encodings

ptxas carries **two** unrelated numeric encodings of a target. Do not conflate them:

1. **Legacy 16-bit hash-slot code** (`sm_version_codes.tsv`) — a sparse hash-slot array at
   **VMA 0x2020620** (stride 2). Code = `(sm_num/10 << 12) | variant_nibble` (variant: 0=plain,
   1=`a`, 3=`c`, 5=`f`). E.g. sm_80 = 0x8000, sm_90 = 0x9000, sm_90a = 0x9001, sm_90f = 0x9005.
   **This table ends at 0x9005 (sm_90f).** The scheme structurally cannot encode sm_103/sm_121
   (non-multiples of 10), so the newer families are **absent** from it by design.

2. **`__CUDA_ARCH__` decimal code** (`sm_target_properties.tsv`) — the canonical code for the modern
   families, stored as `-D__CUDA_ARCH__=<n>` descriptor strings at **VMA 0x2027a08+**:
   sm_100 = 1000, sm_103 = 1030, sm_110 = 1100, sm_120 = 1200, sm_121 = 1210; the `a`/`f` variants
   append `a0`/`f0` (100a0, 120a0, 121a0, 100f0, …). Confirmed via `nvcc -arch=sm_NN`.

## Finalized SASS-ELF flag encoding (`sass_elf_eflags.tsv`)

The SM identity of an emitted SASS cubin lives in the ELF header `e_flags`:
`EF_CUDA_SM = (sm_number << 8) | tag`, where `sm_number` is the full decimal (100, 103, 110, 120, 121),
i.e. byte 1 = 0x64/0x67/0x6e/0x78/0x79, with `EF_CUDA_VIRTUAL_SM = 0x0600`. The `a` variant does **not**
change `e_flags` (sm_120a ≡ sm_120 = 0x06007802); the `a` ISA-class is carried in `.nv.info`.
**sm_120 (0x78) and sm_121 (0x79) are distinct** at the ELF level, but their SASS is byte-identical
otherwise (verified: nvdisasm output for sm_120 vs sm_121 differs only in the `.target` label).

## Files

| File | Contents |
|---|---|
| `sm_id_enumeration.tsv` | table_index / arch_index / sm_id arch enumeration (incl. sm_110/120/121) |
| `supported_targets.tsv` | sm_id → arch_family / version_code / sched_gen (sm_101 noted as NOT PRESENT in 13.0.88) |
| `sm_version_codes.tsv` | legacy 16-bit hash-slot code table @0x2020620 (ends at sm_90f) + provenance note |
| `sm_target_properties.tsv` | `__CUDA_ARCH__` decimal codes + codename / lto / isaClass tokens @0x2027a08 (sm_75..sm_121f) |
| `sass_elf_eflags.tsv` | finalized SASS-ELF `e_flags` / EF_CUDA_SM encoding per arch (sm_90..sm_121) |
| `sm_scheduling_seeds.tsv` | per-sm scheduling-generation codes (sm_110 = gen9; sm_100/103/120/121 = gen8) |
| `gating_diagnostics.tsv` | target/feature-legality diagnostic format strings |
| `instruction_legality.tsv` | per-instruction × per-target legality matrix |

## New-architecture coverage (this update)

sm_110 (Jetson Thor), sm_120 (consumer RTX 50xx / Pro), sm_121 (DGX Spark) — all present in 13.0.88
ptxas (the `gpu-name` parser accepts `sm_110/120/121` + `a`/`f` variants and compiles real kernels).
**sm_101 does NOT exist** in this build (no arch string; `-arch=sm_101` is rejected). sm_120 ≡ sm_121
in code generation; they differ only by the e_flags SM byte (0x78 vs 0x79).

## Confidence

- **`__CUDA_ARCH__` codes, e_flags encoding, sm_101 absence, sm_120≡sm_121:** HIGH — byte-exact binary
  decode plus live compile/disasm round-trips, nvcc cross-check.
- **Legacy 16-bit table termination at sm_90f:** HIGH — exhaustive `.rodata` search found no ≥sm_100 entry.
