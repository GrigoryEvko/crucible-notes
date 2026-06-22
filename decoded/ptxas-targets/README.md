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

All six e_flags were round-trip verified by assembling a probe kernel with this ptxas (13.0.88) **and**
system ptxas 13.1.115 and reading the raw `e_flags` dword at file offset 0x30: sm_90=0x06005a04,
sm_100=0x06006402, sm_103=0x06006702, sm_110=0x06006e02, sm_120=0x06007802, sm_121=0x06007902 — byte-exact.

## `.nv.compat` CAN_FASTPATH_FINALIZE mask (`sass_elf_eflags.tsv`, `nv_compat_fastpath` column)

Every emitted cubin carries a `.nv.compat` (`SHT_CUDA_COMPAT_INFO`) TLV stream. Its last attribute is an
8-byte `EICOMPAT_ATTR_CAN_FASTPATH_FINALIZE` value (TLV tag 0x0b, `EIFMT_SVAL`) — the per-arch capability
mask consumed by the Mercury off-target fast-path finalizer (`sub_60F290`, see `../ptxas-mercury`). Decoded
from the cubins of **both** toolkits (13.0.88 + 13.1.115), the mask is **per-arch**, not a simple on/off:
**sm_100 = 0x09**, **sm_120 = 0x50**, and **sm_90 / sm_103 / sm_110 / sm_121 = 0x00**. (The earlier
"0x50 vs 0x00" framing missed the distinct sm_100 = 0x09 value.) The mask is ANDed against the off-target
capability bits from the Mercury cap-bit table when deciding whether a fast-path off-target finalize is legal.

## Minimum PTX `.version` per target (`sass_elf_eflags.tsv`, `min_ptx_version` column)

The PTX front end version-gates each `.target`. Probed by binary-searching `.version` per `-arch`
(identical on 13.0.88 and 13.1.115): sm_90 = **8.0**, sm_100 = **8.6**, sm_120 = **8.7**,
sm_103 = **8.8**, sm_121 = **8.8**, sm_110 = **9.0**. sm_110 (Thor) is the only one of the modern
families that requires PTX 9.0; a sub-9.0 `.version` is rejected with
`PTX .version X does not support .target sm_110`.

## Files

| File | Contents |
|---|---|
| `sm_id_enumeration.tsv` | table_index / arch_index / sm_id arch enumeration (incl. sm_110/120/121) |
| `supported_targets.tsv` | sm_id → arch_family / version_code / sched_gen (sm_101 = internal enum slot only, CLI-rejected) |
| `sm_version_codes.tsv` | legacy 16-bit hash-slot code table @0x2020620 (ends at sm_90f) + provenance note |
| `sm_target_properties.tsv` | `__CUDA_ARCH__` decimal codes + codename / lto / isaClass tokens @0x2027a08 (sm_75..sm_121f) |
| `sass_elf_eflags.tsv` | finalized SASS-ELF `e_flags` / EF_CUDA_SM + `.nv.compat` fast-path mask + min PTX `.version` per arch (sm_90..sm_121) |
| `sm_scheduling_seeds.tsv` | per-sm scheduling-generation codes (sm_110 = gen9; sm_100/103/120/121 = gen8) |
| `gating_diagnostics.tsv` | target/feature-legality diagnostic format strings |
| `instruction_legality.tsv` | per-instruction × per-target legality matrix |

## New-architecture coverage (this update)

sm_110 (Jetson Thor), sm_120 (consumer RTX 50xx / Pro), sm_121 (DGX Spark) — all present in 13.0.88
ptxas (the `gpu-name` parser accepts `sm_110/120/121` + `a`/`f` variants and compiles real kernels).
sm_120 ≡ sm_121 in code generation; they differ only by the e_flags SM byte (0x78 vs 0x79) and the
`.nv.compat` fast-path mask (0x50 vs 0x00) — their SASS is otherwise byte-identical (nvdisasm diff = 0).

**sm_101 is NOT a usable target** in this build: `-arch=sm_101` is rejected by the `gpu-name` parser
(`Value 'sm_101' is not defined for option 'gpu-name'`). It nevertheless appears as an *internal* enum
slot: the sm-id enumeration array (`sm_id_enumeration.tsv`, VMA 0x1ce7fa8) carries `100, 101, 110, 0, 103,
120, 121` at arch-index 28–34, and a register-class arch class with selector **0x5001** exists (vtable
thunk @0xb081c0). So sm_101 occupies an enum/class slot but has no `__CUDA_ARCH__` descriptor string and
is unreachable from the CLI — a phantom, consistent with the Mercury normalize rule that folds
`sm_101 → sm_110`.

## Confidence

- **`__CUDA_ARCH__` codes (+ descriptor VMAs), e_flags encoding, sm_101 CLI-rejection, sm_120≡sm_121:** HIGH —
  byte-exact binary decode (all 23 descriptor strings + VMAs verified, all 6 e_flags read at file-offset 0x30)
  plus live ptxas assemble/finalize/disasm round-trips on **both** toolkit 13.0.88 and 13.1.115.
- **Legacy 16-bit table termination at sm_90f:** HIGH — exhaustive `.rodata` search found no ≥sm_100 entry.
- **`.nv.compat` CAN_FASTPATH_FINALIZE masks (sm_100=0x09, sm_120=0x50, others=0x00):** HIGH — decoded from
  the `.nv.compat` TLV of probe cubins on both toolkits; identical across 13.0.88 and 13.1.115.
- **Min PTX `.version` per target:** HIGH — binary-searched per `-arch`, identical on both toolkits.
