# ptxas device-ELF (cubin) output emitter — decoded reference

What ptxas (CUDA **13.0.88**) writes when it finishes assembling PTX into a GPU
cubin: the ELF container, its CUDA-specific section types, the `.nv.info` /
`.nv.compat` attribute streams, the relocation tables, the symbol table, the
program headers, and the NVIDIA NOTE sections.

Everything here was obtained by **binary analysis only**: emitting real cubins
with the V13.0.88 `ptxas` binary and reading the raw ELF bytes, cross-checked
with `cuobjdump --dump-elf` and the publicly distributed CUDA toolchain. Numeric
enums (EIATTR codes, relocation types, section types) were re-extracted directly
from the binary's `.rodata` name-pointer tables. Where older CUDA toolkit
binaries expose a different constant, that difference is noted as version drift;
on any conflict the **emitted bytes win** (the binary is 13.0.88; several fields
have drifted relative to earlier builds).

## How the ground truth was produced

```
ptxas -arch=sm_90  minimal.ptx -o final.cubin     # ET_EXEC final cubin
ptxas -c -arch=sm_90 minimal.ptx -o reloc.cubin   # ET_REL relocatable object
cuobjdump --dump-elf final.cubin                  # symbolic cross-check
```

The raw section/header/symbol bytes were then decoded with a small Python reader
(no external ELF library) so every field is traced to an exact file offset.

## Files

| file | contents |
|---|---|
| `section_catalog.tsv` | every section type ptxas emits — `SHT_CUDA_*` numeric values, `sh_flags`, `STT_CUDA_*`/`STO_CUDA_*` symbol encodings, special symbol names. Notes which types appear only in relocatable `-c` output vs the final cubin. |
| `eiattr_codes.tsv` | the full `.nv.info` EIATTR enumeration (codes 0..96), re-extracted from the binary name-pointer table at VMA `0x23FDC20`, with per-entry format words. |
| `eicompat_codes.tsv` | the `.nv.compat` EICOMPAT enumeration — recovered by pairing on-wire attribute bytes against `cuobjdump`'s names (the binary has no name-pointer table for these). |
| `reloc_types.tsv` | `R_CUDA_*` (117 types) and `R_MERCURY_*` (65 types) with per-reloc bit-field actions and resolver/handler class, re-extracted from the binary action-record tables. |
| `nvinfo_wire_groundtruth.tsv` | byte-level `.nv.info` / `.nv.callgraph` / `.note` / `RELA` wire layouts, with a fully decoded `EIATTR_KPARAM_INFO` bitfield. |
| `header_notes_compat.tsv` | ELF header (`e_ident`/`e_flags`), the 6 program headers, and the two NVIDIA NOTE sections, with measured values per architecture. |
| `per_arch_sm110_120_121.tsv` | the per-architecture container facts for the three newest Blackwell-class targets — `e_flags`, `.note.nv.cuinfo`, the full `.nv.compat` attribute values, and the GB10B reserved-SMEM workaround symbol — measured field-by-field for sm_110 / sm_120 / sm_121. |

## Key structural facts

- **Two output modes, two section-type vocabularies.** In relocatable (`-c`,
  `ET_REL`) objects the memory-space sections keep their `SHT_CUDA_*` LOPROC
  types (`.nv.constant0`=`0x70000064`, `.nv.global`=`0x70000007`,
  `.nv.shared`=`0x7000000a`, …). In the **final cubin** (`ET_EXEC`) the same
  sections are lowered to standard `PROGBITS`/`NOBITS`. Only `.nv.info`,
  `.nv.compat`, and `.nv.callgraph` keep LOPROC types in the final cubin.
- **`.nv.info` / `.nv.compat` are attribute streams** of `[format, attr, value]`
  triples (`EIFMT` = NVAL/BVAL/HVAL/SVAL). `.nv.compat` (`0x70000086`) is new in
  13.0.88 and carries the architecture-variant ("a"/"f") and Mercury-finalizer
  capability info.
- **Mercury finalizer is the default path in 13.0.88.** A plain `sm_90` kernel
  already emits `EIATTR_MERCURY_ISA_VERSION` and an `.nv.compat` Mercury ISA
  version; both are absent on `sm_75`.
- **Drifts from earlier toolchains**: OS/ABI byte `0x33`→`0x41`, ABI
  version `7`→`8`, real-SM moved from `e_flags` low byte to bits 8–15, the
  `SHF_BARRIER_MASK` section flag is no longer populated (barrier count moved to
  `EIATTR_NUM_BARRIERS`), the reloc handler enum gained a `FINALIZER` value, the
  callgraph gained a 4th marker, and a new `0xa0` symbol memory-space appeared.

## Per-architecture container facts (sm_110 / sm_120 / sm_121)

The SASS body ptxas emits for these three Blackwell-class targets is byte-identical
(same encoder/scheduler family; `sm_120a` shares `sm_120`'s tables too). The only
architecture-dependent output is in the ELF container — captured field-by-field in
`per_arch_sm110_120_121.tsv`. Two findings are worth calling out:

- **`EICOMPAT_ATTR_CAN_FASTPATH_FINALIZE` (attr 11) splits sm_120 from sm_121.** Its
  8-byte `.nv.compat` value is `0x50` for `sm_120` (and `sm_120a`) but `0x00` for both
  `sm_121` and `sm_110`. The split is stable across `-O0`/`-O3` and kernel content —
  a deliberate finalizer-capability descriptor, not noise. So the alias rule "sm_120 ≡
  sm_121" holds for *SASS/encoding* but **not** for the `.nv.compat` finalizer record:
  the two cubins differ in this one wire field (plus the real-SM byte in `e_flags`).
- **sm_110 (Thor / GB10-class) emits a GB10B silicon workaround.** A plain kernel adds
  two extra symbols absent on sm_120/121: `__nv_reservedSMEM_gb10b_war_var` (literal
  string in ptxas `.rodata`) and its `.nv.shared.reserved.0` backing section. This shifts
  sm_110's symbol-table indices relative to sm_120/121.

`e_flags` differs only in the real-SM byte (bits 8–15): `0x6e`/`0x78`/`0x79` for
sm_110/120/121; the variant nibble (bits 0–7) is `0x02` on all three (Blackwell-class).
`.note.nv.cuinfo` virtualSM = the **`-arch` SM number** (sm_110→110/0x6e, sm_120→120/0x78,
sm_121→121/0x79) — cuobjdump's "CUDA Virtual SM" line confirms on both 13.0.88 and 13.1.115;
its CUDA-API word is `130` on 13.0.88 (toolkit-keyed, not arch-keyed; system 13.1.115 emits
`131`). sm_110 requires PTX `.version 9.0`;
sm_120/121 accept `8.8`. The `.text` is byte-identical for `sm_120`/`sm_121` generally
(and `sm_120a`); `sm_110` matches only trivial samples — arith/isel kernels diverge
(`sm_110 IADD3` vs `sm_120 IADD`).

See the final report (returned in the analysis conversation) for the full
emitter walkthrough, confidence levels, and the proposed wiki outline.
