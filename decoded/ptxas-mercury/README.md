# ptxas Mercury / capmerc / Finalizer — decoded data

White-hat RE of NVIDIA **ptxas** (CUDA **13.0.88**, binary at `ptxas/ptxas`). This directory documents the
**Mercury** SASS container, the **capsule / capmerc** packaging layout, the **R_MERCURY** relocation kinds,
the **finalizer (FNLZR)** stage, and the **section content-equality dedup** mechanism.

All findings are **binary-derived**. Where older CUDA toolkit binaries corroborate the binary, that is noted;
on any mismatch the **binary wins** and the drift is flagged. Addresses are ptxas v13.0.88 VMAs.

## The chain: encoder → capsule → finalizer

```
PTX → ptxas Mercury encoder ──▶ capmerc ELF (EF_CUDA_MERCURY set) ──▶ FNLZR ──▶ native SASS ELF
                                  │                                     │
                                  ├─ .text.<fn>  = Mercury packet stream (MPE, 32B/packet)
                                  ├─ .nv.merc.*  = cloned debug/symtab/rela (type SHT_CUDA_MERCURY)
                                  ├─ .mercury_to_sass_map = Mercury↔SASS offset state-machine
                                  ├─ SHT_RELA    = R_MERCURY_* relocations (0x10000-base)
                                  └─ .nv.info    = EIATTR_MERCURY_FINALIZER_OPTIONS (0x5A), _ISA_VERSION (0x5F)
```

1. **Encoder.** ptxas emits a Mercury ELF: each kernel `.text.<fn>` holds a Mercury **MPE packet stream**
   (32-byte packets), not yet SASS. The file is marked `EF_CUDA_MERCURY (0x80000000)` with a container version in
   bits 24–30 (`EF_CUDA_MERCURY_VERSION_MASK = 0x7F000000`). Mode is chosen by `--binary-kind {mercury,capmerc,sass}`
   (CLI parser `sub_703AB0`); SM > 99 auto-selects **capmerc**.

2. **Capsule (capmerc).** The capsule is a Mercury ELF that additionally retains, per original section, a `.nv.merc.<name>`
   **clone** tagged `SHT_CUDA_MERCURY (LOPROC+12)`, plus a `.mercury_to_sass_map` table (`SHT_CUDA_MERCURY_SASS_MAP`,
   LOPROC+13) per text section, plus Mercury constant banks (`SHT_MERCURY_CONSTANT_* `, LOPROC+120..126). This lets a
   downstream tool reconstitute/finalize for a **different SM** than originally compiled — "opportunistic finalization"
   (`--opportunistic-finalization-lvl`, runtime field ctx+120, validated ≤ 4).

3. **Finalizer (FNLZR).** Turns the Mercury packet stream into native SASS (see `finalizer_pipeline.tsv`). It decodes
   MPE → Mercury IR, runs Expansion → Opex → WARs → SASS emit, builds an instruction-offset map, then resolves/translates
   relocations and emits the SASS ELF with `EF_CUDA_MERCURY` cleared and `abiVersion = ELFOSABIV_LATEST (7)`.

## R_MERCURY relocations (`r_mercury_relocs.tsv`, `nvrs_symbol_value_actions.tsv`)

- The `mercury_reloc_info[]` table lives in `.rodata` at **VMA 0x23ff080**, stride **0x40** (64 B/entry), with **64 named
  entries** (`r_type 0x10000..0x1003F`, base `R_MERCURY_NONE = 0x10000`). Entry 64 (`0x10040`) is the first entry of the
  **contiguous** `sass_reloc_info[]` (`R_CUDA_*`) table — the two reloc tables are adjacent in `.rodata`.
- Each entry: `{ const char *name; uint32 handler; action[3]×{start_bit, num_bits, sym_value, src_start_bit} }`, decoded
  byte-exact from the binary. `sym_value` integer codes resolve to the `NVRS_*` symbol-value enum (see actions TSV).
- **Handler split** (the `NVRH_*` handler enum): `NVRH_FINALIZER` relocs are resolved by FNLZR; everything else is deferred to
  driver/linker. The decision is literally `mercury_reloc_info[rtype - 0x10000].handler == NVRH_FINALIZER`. Only the
  **PROG_REL** family is `NVRH_FINALIZER`: `PROG_REL64/32/32_LO/32_HI` and the byte-lane `PROG_REL8_0..56`.
- **CUDA-13 deltas vs earlier toolchains:** older builds ended the table at `R_MERCURY_ABS_PROG_REL64 = 0x1003D`
  (+ sentinel `0x1003E`). The binary **appends** `R_MERCURY_UNIFIED32_LO (0x1003E)` / `UNIFIED32_HI (0x1003F)` using
  new sym_value codes 55/56 (`NVRS_LOUADDR` / `NVRS_HIUADDR`). All other 62 entries are byte-identical to earlier builds.

## Capsule layout (`capsule_section_layout.tsv`, `merc_section_names.tsv`)

- Section taxonomy: `SHT_CUDA_MERCURY = LOPROC+12 (0x7000000C)`, `SHT_CUDA_MERCURY_SASS_MAP = LOPROC+13`,
  `SHT_MERCURY_CONSTANT_PARAMS..TOOLS = LOPROC+120..126`. 19 distinct `.nv.merc.*` clone suffixes are present in the binary
  string pool. **Naming drift:** earlier toolchains prefixed clones with `.merc`; the CUDA-13 binary uses `.nv.merc`.
- Header constants: `EM_CUDA = 190 (0xBE)`; accepted `e_type ∈ {ET_REL=1, ET_EXEC=2, ET_EWP=0xFF00}`; input
  `abiVersion == ELFMERCABIV_ABI (0)`. On output the finalizer clears `EF_CUDA_MERCURY`, sets `abiVersion=7`, sets
  `EF_CUDA_64BIT_ADDRESS`, demotes shared/global/local to `SHT_NOBITS` for `ET_EXEC`, and zeroes `phoff/phnum`.

## Mercury↔SASS map (`merc_sass_map.tsv`)

DWARF-line-program-style byte-code (`MSM_*` opcodes 0x00–0x0B) in the `.mercury_to_sass_map` section. 16-byte header
`{version=1, headerLength=16, sassInstSize=16, mercInitialInstsize=32}` — i.e. **MPE = 32 B**, **SASS inst = 16 B**.

## Content-equality dedup (`dedup_functions.tsv`)

**Binary-only (CUDA-13).** Earlier toolchains had **no** whole-section dedup — they *duplicate* (emit `.nv.merc.*` clones).
The only content-equality merge in earlier builds was a constant-pool overlap primitive in the generic ELF writer,
gated on `memcmp` with the FATAL `"overlapping non-identical data"`. That primitive **evolved** into the CUDA-13
emitter-side dedup: driver `sub_1CB8E40` runs a content-dedup stage (`sub_1CABD60 → sub_1CA6890/sub_1CA6760`, logging
`"found duplicate value 0x%x, alias %s to %s"`, `"found duplicate 64bit value 0x%llx…"`, `"found duplicate %d byte value…"`)
followed by symbol/name dedup (`sub_1CB68D0 → sub_1CB3EB0/sub_1CB3FD0`, `"set duplicate name for %s(%d) to %d"`). The
**same** `"overlapping non-identical data"` FATAL survives in the binary (`sub_1CA5A00`) — the bridge between the two eras.
Dedup is **conservative**: two slices are folded only if their backing bytes are byte-identical (`memcmp == 0`), never by
name alone; a single differing reloc entry blocks the fold.

## Files

| File | Contents |
|---|---|
| `r_mercury_relocs.tsv` | 64-entry R_MERCURY catalog, binary-decoded (name/handler/bit-action/sym_value/VMA) + version provenance |
| `nvrs_symbol_value_actions.tsv` | `NVRS_*` symbol-value codes → patch semantics |
| `capsule_section_layout.tsv` | SHT_CUDA_MERCURY* / SHT_MERCURY_CONSTANT_* / EF_*/ET_* container constants |
| `merc_section_names.tsv` | 19 `.nv.merc.*` clone families |
| `merc_sass_map.tsv` | `.mercury_to_sass_map` header + MSM_* opcode state machine |
| `finalizer_pipeline.tsv` | ordered FNLZR stages (lib driver + GenerateSASS phases + reloc/dedup/self-check) |
| `reloc_conversion_chain.tsv` | R_MERCURY ↔ RK_MERCURY ↔ R_CUDA conversion round-trips |
| `dedup_functions.tsv` | content + name dedup function map (binary VMAs + FATAL strings) |
| `finalizer_functions.tsv` | FNLZR / capmerc binary functions |
| `finalizer_attributes.tsv` | EIATTR_MERCURY_* / EICOMPAT_* nvinfo attributes |
| `arch_compat_capbits.tsv` | per-SM off-target finalizer capability bits (`sub_60F290` jump table @0x2020030) + family-normalize rules |

## Per-arch off-target finalization (the only arch-parameterized Mercury logic)

Most of the Mercury layer is **SM-version-independent** (the R_MERCURY catalog, capsule
container constants, MSM map, dedup, and reloc-conversion tables are identical across all targets;
the only ISA-conditioned conversion steps are the `vSM<70` yield-reloc insertion and the
Hopper-only `RK_SM90_IMM55_ABS → R_CUDA_ABS55_16_34`). The one place where the SM number drives a
per-arch decision is the **off-target family-compatibility check** in the fast-path finalizer
`sub_60F290` (`arch_compat_capbits.tsv`):

- It reads the `CAN_FINALIZE_DEBUG` env (`getenv`/`strtol`), **normalizes** both the self- and
  target-SM (`104→120`, `130→107`, `101→110`), then dispatches on `(normalized_sm − 100)` through a
  22-entry jump table at **VMA 0x2020030** to set a single **capability bit**, which is ANDed against
  the capsule's capability mask.
- Recovered cap-bits (CUDA-13 has these for the Blackwell/Thor families): **sm_100 = 0x01**,
  **sm_103 = 0x08**, **sm_110 = 0x02** (also reached by the `sm_101→110` normalize), the
  **sm_104/sm_120 family slot = 0x10**, **sm_121 = 0x40**. The raw `sm_120` index (0x14) returns 0
  because sm_120 is reached through the `104↔120` family alias. `sm_101` is a phantom (no real arch in
  13.0.88) that exists only as a normalize source folding into sm_110.

## Confidence

- **R_MERCURY catalog, capsule constants, MSM map, dedup function map:** HIGH — byte-exact binary decode, string-verified.
- **Reloc conversion chain, finalizer pipeline ordering:** MEDIUM-HIGH — pipeline order and conversion switches recovered
  from the binary; corroborated at the data-section switch (`.nv.merc.rela`), the master resolver index math
  (`r_type − 0x10000`, table stride `<<6`), and the bit-patcher (extract/insert with cross-qword `& 0x3F` spill).
- **EICOMPAT_* codes:** LOW — most are name-only string tokens with no backing table recoverable from the binary.
