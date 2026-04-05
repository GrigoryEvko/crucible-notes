# NVIDIA ELF Section Catalog

Complete alphabetical catalog of every ELF section name recognized by nvlink v13.0.88. Sections are grouped by category. Each table shows the section name, the `sh_type` as both a hex value and its symbolic constant, and the section's purpose. For detailed semantics, see [NVIDIA Section Types](../elf/nvidia-sections.md).

**Notation conventions:**

- `<funcname>` -- kernel or device function name suffix (e.g., `.text._Z6kernelPf`).
- `<secname>` -- target section name (e.g., `.rela.text._Z6kernelPf`).
- `(prefix)` -- the string in the binary is a prefix used with `strncmp`; the full name is formed by appending a suffix at runtime.
- Format strings like `.rela%s` show how nvlink constructs the name via `sprintf`.

---

## Code Sections

| # | Name | sh_type (hex) | sh_type (constant) | Purpose |
|---|---|---|---|---|
| 1 | `.text.<funcname>` | `0x01` | `SHT_PROGBITS` | SASS machine code for a single kernel or device function. Each `__global__` or `__device__` function produces a `.text.<mangled_name>` section. |

## Info and Metadata

| # | Name | sh_type (hex) | sh_type (constant) | Purpose |
|---|---|---|---|---|
| 2 | `.nv.callgraph` | `0x70000002` | `SHT_CUDA_CALLGRAPH` | Caller-callee edge table for dead code elimination and stack size propagation. Built by `build_callgraph_section` (`sub_44D200`). |
| 3 | `.nv.compat` | `0x70000006` | `SHT_CUDA_COMPAT` | Forward/backward compatibility attribute table. Validated at merge time (error string contains typo: `"encoutered"`). |
| 4 | `.nv.info` | `0x70000001` | `SHT_CUDA_INFO` | Global (module-level) EIATTR metadata records. See [.nv.info Metadata](../elf/nv-info.md). |
| 5 | `.nv.info.<funcname>` | `0x70000001` | `SHT_CUDA_INFO` | Per-kernel EIATTR metadata (regcount, stack sizes, barriers, params, etc.). `sh_link` references the owning function symbol. |
| 6 | `.nv.metadata` | `0x7000000B` | `SHT_CUDA_METADATA` | Module metadata (`__nv_module_id`, version info). Extracted by `register_module_for_linking` (`sub_42A680`). |
| 7 | `.nv.prototype` | `0x70000003` | `SHT_CUDA_PROTOTYPE` | Kernel launch prototype descriptors (parameter layout). |
| 8 | `.nv.ptx.const0.size` | -- | (metadata key) | Constant bank 0 size record from PTX compilation. Not a real ELF section; a metadata key string. |

## Relocation Sections

| # | Name | sh_type (hex) | sh_type (constant) | Purpose |
|---|---|---|---|---|
| 9 | `.nv.rel.action` | `0x70000004` | `SHT_CUDA_RELOCINFO` | CUDA relocation action table (multi-step relocation recipes for bindless textures, etc.). |
| 10 | `.nv.resolvedrela` | `0x70000005` | `SHT_CUDA_RESOLVED_RELA` | Preserved relocations for driver-side patching (`--preserve-relocs`). Created by `emit_resolved_relocations` (`sub_46ADC0`). |
| 11 | `.nv.uft.rel` | `0x04` | `SHT_RELA` | Relocation table for Unified Function Table jump slots. |
| 12 | `.rela.<secname>` | `0x04` | `SHT_RELA` | Standard ELF relocation sections. Generated via format string `.rela%s`. |
| 13 | `.rel.<secname>` | `0x09` | `SHT_REL` | Standard ELF REL sections (without addend). Generated via format string `.rel%s`. Rejected by nvlink with `"unsupported REL section"`. |

## Global Memory

| # | Name | sh_type (hex) | sh_type (constant) | Purpose |
|---|---|---|---|---|
| 14 | `.nv.global` | `0x70000007` | `SHT_CUDA_GLOBAL` | Uninitialized global device memory (`__device__` BSS). Input arrives as `SHT_NOBITS`, reclassified by linker. |
| 15 | `.nv.global.init` | `0x70000008` | `SHT_CUDA_GLOBAL_INIT` | Initialized global device memory (`__device__` with initializer). Carries `SHT_PROGBITS` data. |
| 16 | `.nv.host` | `0x01` | `SHT_PROGBITS` | Host-visible data section. |

## Local Memory (Per-Thread)

| # | Name | sh_type (hex) | sh_type (constant) | Purpose |
|---|---|---|---|---|
| 17 | `.nv.local.<funcname>` | `0x70000009` | `SHT_CUDA_LOCAL` | Per-kernel thread-private storage (register spills, local arrays). Input type `SHT_NOBITS`, reclassified. Merged by `merge_overlapping_local_data` (`sub_437E20`). |

String in binary: `.nv.local.` (prefix; function name appended at runtime).

## Shared Memory (Per-CTA)

| # | Name | sh_type (hex) | sh_type (constant) | Purpose |
|---|---|---|---|---|
| 18 | `.nv.shared.<funcname>` | `0x7000000A` | `SHT_CUDA_SHARED` | Per-kernel shared memory (`__shared__` variables). Input `SHT_NOBITS`. Layout optimized by `shared_memory_optimizer` (`sub_436BD0`). |
| 19 | `.nv.reservedSmem` | `0x70000015` | `SHT_CUDA_SHARED_RESERVED` | Base reserved shared memory section. |
| 20 | `.nv.reservedSmem.begin` | `0x70000015` | `SHT_CUDA_SHARED_RESERVED` | Start address marker for reserved region. |
| 21 | `.nv.reservedSmem.cap` | `0x70000015` | `SHT_CUDA_SHARED_RESERVED` | Capacity limit of reserved region. |
| 22 | `.nv.reservedSmem.end` | `0x70000015` | `SHT_CUDA_SHARED_RESERVED` | End address marker for reserved region. |
| 23 | `.nv.reservedSmem.offset0` | `0x70000015` | `SHT_CUDA_SHARED_RESERVED` | First reserved offset slot. |
| 24 | `.nv.reservedSmem.offset1` | `0x70000015` | `SHT_CUDA_SHARED_RESERVED` | Second reserved offset slot. |

String in binary: `.nv.shared.` (prefix; function name appended at runtime).

## Constant Memory (Banks 0--17)

| # | Name | sh_type (hex) | sh_type (constant) | Purpose |
|---|---|---|---|---|
| 25 | `.nv.constant0` | `0x70000064` | `SHT_CUDA_CONSTANT0` | Constant bank 0 (kernel parameters, driver-managed). |
| 26 | `.nv.constant1` | `0x70000065` | `SHT_CUDA_CONSTANT1` | Constant bank 1. |
| 27 | `.nv.constant2` | `0x70000066` | `SHT_CUDA_CONSTANT2` | Constant bank 2 (compiler-generated OCG constants). |
| 28 | `.nv.constant3` | `0x70000067` | `SHT_CUDA_CONSTANT3` | Constant bank 3 (bindless texture descriptors). |
| 29 | `.nv.constant4` | `0x70000068` | `SHT_CUDA_CONSTANT4` | Constant bank 4. |
| 30 | `.nv.constant5` | `0x70000069` | `SHT_CUDA_CONSTANT5` | Constant bank 5. |
| 31 | `.nv.constant6` | `0x7000006A` | `SHT_CUDA_CONSTANT6` | Constant bank 6. |
| 32 | `.nv.constant7` | `0x7000006B` | `SHT_CUDA_CONSTANT7` | Constant bank 7. |
| 33 | `.nv.constant8` | `0x7000006C` | `SHT_CUDA_CONSTANT8` | Constant bank 8. |
| 34 | `.nv.constant9` | `0x7000006D` | `SHT_CUDA_CONSTANT9` | Constant bank 9. |
| 35 | `.nv.constant10` | `0x7000006E` | `SHT_CUDA_CONSTANT10` | Constant bank 10. |
| 36 | `.nv.constant11` | `0x7000006F` | `SHT_CUDA_CONSTANT11` | Constant bank 11. |
| 37 | `.nv.constant12` | `0x70000070` | `SHT_CUDA_CONSTANT12` | Constant bank 12. |
| 38 | `.nv.constant13` | `0x70000071` | `SHT_CUDA_CONSTANT13` | Constant bank 13. |
| 39 | `.nv.constant14` | `0x70000072` | `SHT_CUDA_CONSTANT14` | Constant bank 14. |
| 40 | `.nv.constant15` | `0x70000073` | `SHT_CUDA_CONSTANT15` | Constant bank 15. |
| 41 | `.nv.constant16` | `0x70000074` | `SHT_CUDA_CONSTANT16` | Constant bank 16. |
| 42 | `.nv.constant17` | `0x70000075` | `SHT_CUDA_CONSTANT17` | Constant bank 17. |

Bank type formula: `SHT_CUDA_CONSTANT0 + N` = `0x70000064 + N`. The bank number is parsed from the section name suffix by `strtol(name + 12, NULL, 10)`. Per-entry constant sections use the naming convention `<bank>.<funcname>` (e.g., `.nv.constant0.my_kernel`).

The generic prefix `.nv.constant` (without a bank digit) also appears in the binary and is matched by `strncmp`. See [Constant Banks](../elf/constant-banks.md).

## Named Constant Aliases

These are logical names for constant memory regions within specific banks. They appear as section name suffixes (e.g., `.nv.constant0.entry_params`) or as standalone lookup keys.

| # | Name | sh_type (hex) | sh_type (constant) | Purpose |
|---|---|---|---|---|
| 43 | `.nv.constant.driver` | `0x70000064`+ | (bank alias) | Driver-injected constants (grid dims, thread counts). |
| 44 | `.nv.constant.entry_image_header_indices` | `0x70000064`+ | (bank alias) | Maps entry points to image header array positions. |
| 45 | `.nv.constant.entry_params` | `0x70000064`+ | (bank alias) | Kernel launch parameters written by driver before each launch. |
| 46 | `.nv.constant.optimizer` | `0x70000064`+ | (bank alias) | Compiler-generated optimizer constants (`__ocg_const`). |
| 47 | `.nv.constant.pic` | `0x70000064`+ | (bank alias) | Position-independent code trampoline data. |
| 48 | `.nv.constant.tools_data` | `0x70000064`+ | (bank alias) | Profiling/debugging tool-injected constants. |
| 49 | `.nv.constant.user` | `0x70000064`+ | (bank alias) | User-defined `__constant__` variables. |

## Unified Tables (UFT/UDT/UIDX)

| # | Name | sh_type (hex) | sh_type (constant) | Purpose |
|---|---|---|---|---|
| 50 | `.nv.udt` | `0x01` | `SHT_PROGBITS` | Unified Descriptor Table (cross-module texture/surface access). |
| 51 | `.nv.udt.entry` | `0x01` | `SHT_PROGBITS` | UDT entry metadata (UUID-to-slot mapping). |
| 52 | `.nv.uft` | `0x01` | `SHT_PROGBITS` | Unified Function Table (cross-module indirect call jump slots). |
| 53 | `.nv.uft.entry` | `0x01` | `SHT_PROGBITS` | UFT entry metadata (UUID-to-slot mapping). |
| 54 | `.nv.uidx` | `0x01` | `SHT_PROGBITS` | Unified index table (external UUID-to-slot file via `--uidx-file`). |

## Texture and Surface Descriptors

| # | Name | sh_type (hex) | sh_type (constant) | Purpose |
|---|---|---|---|---|
| 55 | `.nv.independent.samplerrefDescSize` | `0x01` | `SHT_PROGBITS` | Descriptor size for independent-mode sampler references. |
| 56 | `.nv.independent.texrefDescSize` | `0x01` | `SHT_PROGBITS` | Descriptor size for independent-mode texture references. |
| 57 | `.nv.surfrefDescSize` | `0x01` | `SHT_PROGBITS` | Descriptor size for surface references. |
| 58 | `.nv.unified.texrefDescSize` | `0x01` | `SHT_PROGBITS` | Descriptor size for unified-mode texture references. |

## Note Sections

| # | Name | sh_type (hex) | sh_type (constant) | Purpose |
|---|---|---|---|---|
| 59 | `.note.nv.cuinfo` | `0x07` | `SHT_NOTE` | CUDA compilation info (target arch, CUDA version, ABI-affecting options). |
| 60 | `.note.nv.cuver` | `0x07` | `SHT_NOTE` | CUDA toolkit version stamp. |
| 61 | `.note.nv.tkinfo` | `0x07` | `SHT_NOTE` | Extended toolkit metadata (controllable with `--verbose-tkinfo`). |

## Debug Sections -- NVIDIA Proprietary

| # | Name | sh_type (hex) | sh_type (constant) | Purpose |
|---|---|---|---|---|
| 62 | `.nv_debug.shared` | `0x08` | `SHT_NOBITS` | Debug-mode shared memory for instrumentation (`-g` builds only). |
| 63 | `.nv_debug_info_ptx` | `0x01` | `SHT_PROGBITS` | Embedded PTX source text for source-level debugging. |
| 64 | `.nv_debug_info_reg_sass` | `0x01` | `SHT_PROGBITS` | Per-instruction register liveness at SASS level. |
| 65 | `.nv_debug_info_reg_type` | `0x01` | `SHT_PROGBITS` | Register type annotations (data types to physical registers). |
| 66 | `.nv_debug_line_sass` | `0x01` | `SHT_PROGBITS` | SASS-level line number table. Constructed at runtime from prefix `.nv_debug_` + `line_sass`. See [Line Tables](../debug/line-tables.md). |

The prefix `.nv_debug_` appears in the binary as a string used with `strncmp` to recognize sections. The linker constructs `.nv_debug_line_sass` programmatically via `section_create`.

## Debug Sections -- Standard DWARF

The linker recognizes any section with prefix `.debug_` as a standard DWARF section. Common instances in cubins:

| # | Name | sh_type (hex) | sh_type (constant) | Purpose |
|---|---|---|---|---|
| 67 | `.debug_abbrev` | `0x01` | `SHT_PROGBITS` | DWARF abbreviation table. |
| 68 | `.debug_aranges` | `0x01` | `SHT_PROGBITS` | DWARF address range table. |
| 69 | `.debug_frame` | `0x01` | `SHT_PROGBITS` | DWARF call frame information. |
| 70 | `.debug_info` | `0x01` | `SHT_PROGBITS` | DWARF compilation unit info. |
| 71 | `.debug_line` | `0x01` | `SHT_PROGBITS` | DWARF line number program. |
| 72 | `.debug_loc` | `0x01` | `SHT_PROGBITS` | DWARF location lists. |
| 73 | `.debug_macinfo` | `0x01` | `SHT_PROGBITS` | DWARF macro information. |
| 74 | `.debug_pubnames` | `0x01` | `SHT_PROGBITS` | DWARF public name lookup table. |
| 75 | `.debug_pubtypes` | `0x01` | `SHT_PROGBITS` | DWARF public type lookup table. |
| 76 | `.debug_ranges` | `0x01` | `SHT_PROGBITS` | DWARF non-contiguous address ranges. |
| 77 | `.debug_str` | `0x01` | `SHT_PROGBITS` | DWARF string table. |

The string `.debug_` appears as a prefix matcher. The variant `.debug_str+` also appears in the binary (used internally for merged/concatenated debug string pools).

## Hash Relocation Sections (Incremental Linking)

These sections support incremental linking through hash-based relocation tracking. Each letter encodes the content type: **K**ey, **C**ode, **D**ata; and the scope: **E**xternal, **I**nternal. Processed by `hrk_section_process` (`sub_4AF3C0`) and `hrc_hrd_section_process` (`sub_4B02A0`).

| # | Name | sh_type (hex) | sh_type (constant) | Purpose |
|---|---|---|---|---|
| 78 | `.nvHRCE` | `0x01` | `SHT_PROGBITS` | Hash Relocation Code External -- external code hash entries. |
| 79 | `.nvHRCI` | `0x01` | `SHT_PROGBITS` | Hash Relocation Code Internal -- internal code hash entries. |
| 80 | `.nvHRDE` | `0x01` | `SHT_PROGBITS` | Hash Relocation Data External -- external data hash entries. |
| 81 | `.nvHRDI` | `0x01` | `SHT_PROGBITS` | Hash Relocation Data Internal -- internal data hash entries. |
| 82 | `.nvHRKE` | `0x01` | `SHT_PROGBITS` | Hash Relocation Key External -- external key hash entries. |
| 83 | `.nvHRKI` | `0x01` | `SHT_PROGBITS` | Hash Relocation Key Internal -- internal key hash entries. |

## Mercury Sections (sm100+)

All Mercury sections carry `SHF_CUDA_MERCURY` (`0x10000000`) in `sh_flags`. The merge phase skips them; FNLZR processes them after finalizing Mercury instructions into SASS. See [Mercury ELF Sections](../mercury/elf-sections.md).

### Mercury Container

| # | Name | sh_type (hex) | sh_type (constant) | Purpose |
|---|---|---|---|---|
| 84 | `.nv.merc` | `0x01` | `SHT_PROGBITS` | Top-level Mercury container holding the encoded Mercury instruction stream for a single kernel. FNLZR reads, rewrites (opex expansion, scheduling, register assignment), and replaces with final SASS `.text`. |
| 85 | `.nv.merc.tmp` | `0x01` | `SHT_PROGBITS` | Temporary Mercury section used during FNLZR rewrite passes. |

### Mercury DWARF Debug

| # | Name | sh_type (hex) | sh_type (constant) | Purpose |
|---|---|---|---|---|
| 86 | `.nv.merc.debug_abbrev` | `0x01` | `SHT_PROGBITS` | Mercury DWARF abbreviation table. |
| 87 | `.nv.merc.debug_aranges` | `0x01` | `SHT_PROGBITS` | Mercury DWARF address range table. |
| 88 | `.nv.merc.debug_frame` | `0x01` | `SHT_PROGBITS` | Mercury DWARF call frame information. |
| 89 | `.nv.merc.debug_info` | `0x01` | `SHT_PROGBITS` | Mercury DWARF compilation unit info. |
| 90 | `.nv.merc.debug_line` | `0x01` | `SHT_PROGBITS` | Mercury DWARF line number program. |
| 91 | `.nv.merc.debug_loc` | `0x01` | `SHT_PROGBITS` | Mercury DWARF location lists. |
| 92 | `.nv.merc.debug_macinfo` | `0x01` | `SHT_PROGBITS` | Mercury DWARF macro information. |
| 93 | `.nv.merc.debug_pubnames` | `0x01` | `SHT_PROGBITS` | Mercury DWARF public name table. |
| 94 | `.nv.merc.debug_pubtypes` | `0x01` | `SHT_PROGBITS` | Mercury DWARF public type table. |
| 95 | `.nv.merc.debug_ranges` | `0x01` | `SHT_PROGBITS` | Mercury DWARF non-contiguous address ranges. |
| 96 | `.nv.merc.debug_str` | `0x01` | `SHT_PROGBITS` | Mercury DWARF string table. |

### Mercury NVIDIA Debug

| # | Name | sh_type (hex) | sh_type (constant) | Purpose |
|---|---|---|---|---|
| 97 | `.nv.merc.nv_debug_info_reg_sass` | `0x01` | `SHT_PROGBITS` | Mercury register liveness at SASS level. |
| 98 | `.nv.merc.nv_debug_info_reg_type` | `0x01` | `SHT_PROGBITS` | Mercury register type annotations. |
| 99 | `.nv.merc.nv_debug_line_sass` | `0x01` | `SHT_PROGBITS` | Mercury SASS-level line table. |
| 100 | `.nv.merc.nv_debug_ptx_txt` | `0x01` | `SHT_PROGBITS` | Mercury embedded PTX source text. |

### Mercury Structural

| # | Name | sh_type (hex) | sh_type (constant) | Purpose |
|---|---|---|---|---|
| 101 | `.nv.merc.nv.shared.reserved.` | `0x08` | `SHT_NOBITS` | Mercury reserved shared memory region. Prefix -- suffix is the reservation ID (e.g., `tcgen05_partition`). |
| 102 | `.nv.merc.rela` | `0x04` | `SHT_RELA` | Mercury relocation section. Contains `Elf64_Rela` records referencing the Mercury symbol table. |
| 103 | `.nv.merc.symtab_shndx` | `0x12` | `SHT_SYMTAB_SHNDX` | Mercury extended section index table (for >65535 sections). |

## Fatbin Sections (Host ELF)

These sections appear in the host-side ELF, not in the device cubin. The linker script template embeds them:
```
SECTIONS {
    .nvFatBinSegment : { *(.nvFatBinSegment) }
    __nv_relfatbin   : { *(__nv_relfatbin) }
    .nv_fatbin       : { *(.nv_fatbin) }
}
```

| # | Name | sh_type (hex) | sh_type (constant) | Purpose |
|---|---|---|---|---|
| 104 | `.nv_fatbin` | `0x01` | `SHT_PROGBITS` | Raw fat binary payload containing cubins for all target architectures. |
| 105 | `.nvFatBinSegment` | `0x01` | `SHT_PROGBITS` | Primary fat binary segment (`__cudaFatBinaryData` structure). |
| 106 | `__nv_relfatbin` | `0x01` | `SHT_PROGBITS` | Relocatable fat binary references needing host-side relocation. |

## Standard ELF Infrastructure

| # | Name | sh_type (hex) | sh_type (constant) | Purpose |
|---|---|---|---|---|
| 107 | `.shstrtab` | `0x03` | `SHT_STRTAB` | Section header string table (section name strings). |
| 108 | `.strtab` | `0x03` | `SHT_STRTAB` | String table for symbol names. |
| 109 | `.symtab` | `0x02` | `SHT_SYMTAB` | Symbol table (CUDA-specific and standard ELF symbols). |

---

## Section Type Summary

### NVIDIA CUDA-Specific Types (`0x70000001`--`0x70000075`)

| sh_type (hex) | Constant | Section(s) |
|---|---|---|
| `0x70000001` | `SHT_CUDA_INFO` | `.nv.info`, `.nv.info.<func>` |
| `0x70000002` | `SHT_CUDA_CALLGRAPH` | `.nv.callgraph` |
| `0x70000003` | `SHT_CUDA_PROTOTYPE` | `.nv.prototype` |
| `0x70000004` | `SHT_CUDA_RELOCINFO` | `.nv.rel.action` |
| `0x70000005` | `SHT_CUDA_RESOLVED_RELA` | `.nv.resolvedrela` |
| `0x70000006` | `SHT_CUDA_COMPAT` | `.nv.compat` |
| `0x70000007` | `SHT_CUDA_GLOBAL` | `.nv.global` |
| `0x70000008` | `SHT_CUDA_GLOBAL_INIT` | `.nv.global.init` |
| `0x70000009` | `SHT_CUDA_LOCAL` | `.nv.local.<func>` |
| `0x7000000A` | `SHT_CUDA_SHARED` | `.nv.shared.<func>` |
| `0x7000000B` | `SHT_CUDA_METADATA` | `.nv.metadata` |
| `0x70000015` | `SHT_CUDA_SHARED_RESERVED` | `.nv.reservedSmem`, `.nv.reservedSmem.begin`, `.nv.reservedSmem.cap`, `.nv.reservedSmem.end`, `.nv.reservedSmem.offset0`, `.nv.reservedSmem.offset1` |
| `0x70000064` | `SHT_CUDA_CONSTANT0` | `.nv.constant0` |
| `0x70000065` | `SHT_CUDA_CONSTANT1` | `.nv.constant1` |
| `0x70000066` | `SHT_CUDA_CONSTANT2` | `.nv.constant2` |
| `0x70000067` | `SHT_CUDA_CONSTANT3` | `.nv.constant3` |
| `0x70000068` | `SHT_CUDA_CONSTANT4` | `.nv.constant4` |
| `0x70000069` | `SHT_CUDA_CONSTANT5` | `.nv.constant5` |
| `0x7000006A` | `SHT_CUDA_CONSTANT6` | `.nv.constant6` |
| `0x7000006B` | `SHT_CUDA_CONSTANT7` | `.nv.constant7` |
| `0x7000006C` | `SHT_CUDA_CONSTANT8` | `.nv.constant8` |
| `0x7000006D` | `SHT_CUDA_CONSTANT9` | `.nv.constant9` |
| `0x7000006E` | `SHT_CUDA_CONSTANT10` | `.nv.constant10` |
| `0x7000006F` | `SHT_CUDA_CONSTANT11` | `.nv.constant11` |
| `0x70000070` | `SHT_CUDA_CONSTANT12` | `.nv.constant12` |
| `0x70000071` | `SHT_CUDA_CONSTANT13` | `.nv.constant13` |
| `0x70000072` | `SHT_CUDA_CONSTANT14` | `.nv.constant14` |
| `0x70000073` | `SHT_CUDA_CONSTANT15` | `.nv.constant15` |
| `0x70000074` | `SHT_CUDA_CONSTANT16` | `.nv.constant16` |
| `0x70000075` | `SHT_CUDA_CONSTANT17` | `.nv.constant17` |

### Standard ELF Types Used

| sh_type (hex) | Constant | Used by |
|---|---|---|
| `0x01` | `SHT_PROGBITS` | `.text.<func>`, `.nv.host`, `.nv.uft`, `.nv.udt`, `.nv.uidx`, `.nv.uft.entry`, `.nv.udt.entry`, descriptor sizes, `.nv_fatbin`, `.nvFatBinSegment`, `__nv_relfatbin`, `.nvHR*`, `.nv.merc.*` (code/debug), all `.debug_*`, all NVIDIA debug |
| `0x02` | `SHT_SYMTAB` | `.symtab` |
| `0x03` | `SHT_STRTAB` | `.strtab`, `.shstrtab` |
| `0x04` | `SHT_RELA` | `.rela.<secname>`, `.nv.uft.rel`, `.nv.merc.rela` |
| `0x07` | `SHT_NOTE` | `.note.nv.cuinfo`, `.note.nv.cuver`, `.note.nv.tkinfo` |
| `0x08` | `SHT_NOBITS` | `.nv_debug.shared`, `.nv.merc.nv.shared.reserved.` |
| `0x09` | `SHT_REL` | `.rel.<secname>` (rejected: `"unsupported REL section"`) |
| `0x12` | `SHT_SYMTAB_SHNDX` | `.nv.merc.symtab_shndx` |

## Section Flag Summary

| Flag (hex) | Constant | Meaning |
|---|---|---|
| `0x10000000` | `SHF_CUDA_MERCURY` | Mercury-format section (sm100+); merge phase skips, FNLZR processes. |

## String-to-Section Cross-Reference

Every section-name string found in the nvlink v13.0.88 binary that matches ELF section patterns, listed alphabetically. This serves as a completeness check against the catalog above.

| Binary string | Catalog entry | Notes |
|---|---|---|
| `.debug_` | #67--77 | Prefix matcher for all DWARF sections |
| `.debug_str+` | -- | Internal variant for merged debug string pools |
| `.merc.tmp` | #85 | Appears as `.nv.merc.tmp` after prefix construction |
| `.note.nv.cuinfo` | #59 | |
| `.note.nv.cuver` | #60 | |
| `.note.nv.tkinfo` | #61 | |
| `.nv.callgraph` | #2 | |
| `.nv.compat` | #3 | |
| `.nv.constant` | #25--42 | Generic prefix (no bank digit) for `strncmp` matching |
| `.nv.constant.driver` | #43 | |
| `.nv.constant.entry_image_header_indices` | #44 | |
| `.nv.constant.entry_params` | #45 | |
| `.nv.constant.optimizer` | #46 | |
| `.nv.constant.pic` | #47 | |
| `.nv.constant.tools_data` | #48 | |
| `.nv.constant.user` | #49 | |
| `.nv.constant0` | #25 | |
| `.nv.constant1` | #26 | |
| `.nv.constant2` | #27 | |
| `.nv.constant3` | #28 | |
| `.nv.constant4` | #29 | |
| `.nv.constant5` | #30 | |
| `.nv.constant6` | #31 | |
| `.nv.constant7` | #32 | |
| `.nv.constant8` | #33 | |
| `.nv.constant9` | #34 | |
| `.nv.constant10` | #35 | |
| `.nv.constant11` | #36 | |
| `.nv.constant12` | #37 | |
| `.nv.constant13` | #38 | |
| `.nv.constant14` | #39 | |
| `.nv.constant15` | #40 | |
| `.nv.constant16` | #41 | |
| `.nv.constant17` | #42 | |
| `.nv.global` | #14 | |
| `.nv.global.init` | #15 | |
| `.nv.host` | #16 | |
| `.nv.independent.samplerrefDescSize` | #55 | |
| `.nv.independent.texrefDescSize` | #56 | |
| `.nv.info` | #4 | |
| `.nv.local.` | #17 | Prefix; `<funcname>` appended |
| `.nv.merc` | #84 | |
| `.nv.merc.` | #84 | Prefix form (with trailing dot) |
| `.nv.merc.debug_abbrev` | #86 | |
| `.nv.merc.debug_aranges` | #87 | |
| `.nv.merc.debug_frame` | #88 | |
| `.nv.merc.debug_info` | #89 | |
| `.nv.merc.debug_line` | #90 | |
| `.nv.merc.debug_loc` | #91 | |
| `.nv.merc.debug_macinfo` | #92 | |
| `.nv.merc.debug_pubnames` | #93 | |
| `.nv.merc.debug_pubtypes` | #94 | |
| `.nv.merc.debug_ranges` | #95 | |
| `.nv.merc.debug_str` | #96 | |
| `.nv.merc.nv.shared.reserved.` | #101 | Prefix; reservation ID appended |
| `.nv.merc.nv_debug_info_reg_sass` | #97 | |
| `.nv.merc.nv_debug_info_reg_type` | #98 | |
| `.nv.merc.nv_debug_line_sass` | #99 | |
| `.nv.merc.nv_debug_ptx_txt` | #100 | |
| `.nv.merc.rela` | #102 | |
| `.nv.merc.symtab_shndx` | #103 | |
| `.nv.metadata` | #6 | |
| `.nv.prototype` | #7 | |
| `.nv.ptx.const0.size` | #8 | Metadata key, not a real section |
| `.nv.rel.action` | #9 | |
| `.nv.reservedSmem` | #19 | |
| `.nv.reservedSmem.begin` | #20 | |
| `.nv.reservedSmem.cap` | #21 | |
| `.nv.reservedSmem.end` | #22 | |
| `.nv.reservedSmem.offset0` | #23 | |
| `.nv.reservedSmem.offset1` | #24 | |
| `.nv.resolvedrela` | #10 | |
| `.nv.shared.` | #18 | Prefix; `<funcname>` appended |
| `.nv.surfrefDescSize` | #57 | |
| `.nv.udt` | #50 | |
| `.nv.udt.entry` | #51 | |
| `.nv.uft` | #52 | |
| `.nv.uft.entry` | #53 | |
| `.nv.uft.rel` | #11 | |
| `.nv.uidx` | #54 | |
| `.nv.unified.texrefDescSize` | #58 | |
| `.nvFatBinSegment` | #105 | |
| `.nvHRCE` | #78 | |
| `.nvHRCI` | #79 | |
| `.nvHRDE` | #80 | |
| `.nvHRDI` | #81 | |
| `.nvHRKE` | #82 | |
| `.nvHRKI` | #83 | |
| `.nv_debug.shared` | #62 | |
| `.nv_debug_` | #62--66 | Prefix matcher for NVIDIA debug sections |
| `.nv_debug_info_ptx` | #63 | |
| `.nv_debug_info_reg_sass` | #64 | |
| `.nv_debug_info_reg_type` | #65 | |
| `.nv_fatbin` | #104 | |
| `.rela%s` | #12 | Format string; `%s` = target section name |
| `.rel%s` | #13 | Format string; `%s` = target section name |
| `.shstrtab` | #107 | |
| `.strtab` | #108 | |
| `.symtab` | #109 | |
| `.text.` | #1 | Prefix; `<funcname>` appended |
| `__nv_relfatbin` | #106 | |

**Total: 109 cataloged section entries** (numbered #1--#109), covering all 95 distinct section-name strings found in the nvlink binary plus 14 per-bank constant entries (`.nv.constant1`--`.nv.constant17` share the same pattern but have distinct string literals in the binary).
