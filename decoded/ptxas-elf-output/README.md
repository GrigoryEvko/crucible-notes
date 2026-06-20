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

See the final report (returned in the analysis conversation) for the full
emitter walkthrough, confidence levels, and the proposed wiki outline.
