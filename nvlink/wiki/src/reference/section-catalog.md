# NVIDIA ELF Section Catalog

Complete alphabetical catalog of every ELF section name recognized by nvlink v13.0.88. Sections are grouped by category. The `sh_type` column shows the ELF section header type value used in the output cubin. For detailed semantics, see [NVIDIA Section Types](../elf/nvidia-sections.md).

## Code Sections

| Name | sh_type | Purpose |
|---|---|---|
| `.text.<funcname>` | `SHT_PROGBITS` | SASS machine code for a single kernel or device function |

## Info and Metadata

| Name | sh_type | Purpose |
|---|---|---|
| `.nv.callgraph` | `0x70000002` | Caller-callee edge table for DCE and stack propagation |
| `.nv.compat` | `0x70000006` | Forward/backward compatibility attribute table |
| `.nv.info` | `0x70000001` | Global (module-level) EIATTR metadata records |
| `.nv.info.<funcname>` | `0x70000001` | Per-kernel EIATTR metadata (regcount, stack sizes, barriers, params) |
| `.nv.metadata` | `0x7000000B` | Module metadata (`__nv_module_id`, version info) |
| `.nv.prototype` | `0x70000003` | Kernel launch prototype descriptors (parameter layout) |
| `.nv.ptx.const0.size` | (metadata key) | Constant bank 0 size record from PTX compilation |

## Relocation Sections

| Name | sh_type | Purpose |
|---|---|---|
| `.nv.rel.action` | `0x70000004` | CUDA relocation action table (multi-step relocation recipes) |
| `.nv.resolvedrela` | `0x70000005` | Preserved relocations for driver-side patching (`--preserve-relocs`) |
| `.nv.uft.rel` | `SHT_RELA` | Relocation table for Unified Function Table jump slots |
| `.rela.<secname>` | `SHT_RELA` | Standard ELF relocation sections (generated as `.rela%s`) |

## Global Memory

| Name | sh_type | Purpose |
|---|---|---|
| `.nv.global` | `0x70000007` | Uninitialized global device memory (`__device__` BSS) |
| `.nv.global.init` | `0x70000008` | Initialized global device memory (`__device__` with initializer) |
| `.nv.host` | `SHT_PROGBITS` | Host-visible data section |

## Local Memory (Per-Thread)

| Name | sh_type | Purpose |
|---|---|---|
| `.nv.local.<funcname>` | `0x70000009` | Per-kernel thread-private storage (register spills, local arrays) |

## Shared Memory (Per-CTA)

| Name | sh_type | Purpose |
|---|---|---|
| `.nv.shared.<funcname>` | `0x7000000A` | Per-kernel shared memory (`__shared__` variables) |
| `.nv.reservedSmem` | `0x70000015` | Base reserved shared memory section |
| `.nv.reservedSmem.begin` | `0x70000015` | Start address marker for reserved region |
| `.nv.reservedSmem.cap` | `0x70000015` | Capacity limit of reserved region |
| `.nv.reservedSmem.end` | `0x70000015` | End address marker for reserved region |
| `.nv.reservedSmem.offset0` | `0x70000015` | First reserved offset slot |
| `.nv.reservedSmem.offset1` | `0x70000015` | Second reserved offset slot |

## Constant Memory (Banks 0--17)

| Name | sh_type | Purpose |
|---|---|---|
| `.nv.constant0` | `0x70000064` | Constant bank 0 (kernel parameters, driver-managed) |
| `.nv.constant1` | `0x70000065` | Constant bank 1 |
| `.nv.constant2` | `0x70000066` | Constant bank 2 |
| `.nv.constant3` | `0x70000067` | Constant bank 3 |
| `.nv.constant4` | `0x70000068` | Constant bank 4 |
| `.nv.constant5` | `0x70000069` | Constant bank 5 |
| `.nv.constant6` | `0x7000006A` | Constant bank 6 |
| `.nv.constant7` | `0x7000006B` | Constant bank 7 |
| `.nv.constant8` | `0x7000006C` | Constant bank 8 |
| `.nv.constant9` | `0x7000006D` | Constant bank 9 |
| `.nv.constant10` | `0x7000006E` | Constant bank 10 |
| `.nv.constant11` | `0x7000006F` | Constant bank 11 |
| `.nv.constant12` | `0x70000070` | Constant bank 12 |
| `.nv.constant13` | `0x70000071` | Constant bank 13 |
| `.nv.constant14` | `0x70000072` | Constant bank 14 |
| `.nv.constant15` | `0x70000073` | Constant bank 15 |
| `.nv.constant16` | `0x70000074` | Constant bank 16 |
| `.nv.constant17` | `0x70000075` | Constant bank 17 |

Bank type formula: `0x70000064 + N`. Per-entry constant sections use the naming convention `<bank>.<funcname>` (e.g., `.nv.constant0.my_kernel`).

## Named Constant Aliases

| Name | sh_type | Purpose |
|---|---|---|
| `.nv.constant.driver` | (bank alias) | Driver-injected constants (grid dims, thread counts) |
| `.nv.constant.entry_image_header_indices` | (bank alias) | Maps entry points to image header array positions |
| `.nv.constant.entry_params` | (bank alias) | Kernel launch parameters written by driver before each launch |
| `.nv.constant.optimizer` | (bank alias) | Compiler-generated optimizer constants (`__ocg_const`) |
| `.nv.constant.pic` | (bank alias) | Position-independent code trampoline data |
| `.nv.constant.tools_data` | (bank alias) | Profiling/debugging tool-injected constants |
| `.nv.constant.user` | (bank alias) | User-defined `__constant__` variables |

## Unified Tables (UFT/UDT/UIDX)

| Name | sh_type | Purpose |
|---|---|---|
| `.nv.udt` | `SHT_PROGBITS` | Unified Descriptor Table (cross-module texture/surface access) |
| `.nv.udt.entry` | `SHT_PROGBITS` | UDT entry metadata (UUID-to-slot mapping) |
| `.nv.uft` | `SHT_PROGBITS` | Unified Function Table (cross-module indirect call jump slots) |
| `.nv.uft.entry` | `SHT_PROGBITS` | UFT entry metadata (UUID-to-slot mapping) |
| `.nv.uidx` | `SHT_PROGBITS` | Unified index table (external UUID-to-slot file via `--uidx-file`) |

## Texture and Surface Descriptors

| Name | sh_type | Purpose |
|---|---|---|
| `.nv.independent.samplerrefDescSize` | `SHT_PROGBITS` | Descriptor size for independent-mode sampler references |
| `.nv.independent.texrefDescSize` | `SHT_PROGBITS` | Descriptor size for independent-mode texture references |
| `.nv.surfrefDescSize` | `SHT_PROGBITS` | Descriptor size for surface references |
| `.nv.unified.texrefDescSize` | `SHT_PROGBITS` | Descriptor size for unified-mode texture references |

## Note Sections

| Name | sh_type | Purpose |
|---|---|---|
| `.note.nv.cuinfo` | `SHT_NOTE` | CUDA compilation info (target arch, CUDA version, ABI-affecting options) |
| `.note.nv.cuver` | `SHT_NOTE` | CUDA toolkit version stamp |
| `.note.nv.tkinfo` | `SHT_NOTE` | Extended toolkit metadata (controllable with `--verbose-tkinfo`) |

## Debug Sections -- NVIDIA

| Name | sh_type | Purpose |
|---|---|---|
| `.nv_debug.shared` | `SHT_NOBITS` | Debug-mode shared memory for instrumentation (`-g` builds only) |
| `.nv_debug_info_ptx` | `SHT_PROGBITS` | Embedded PTX source text for source-level debugging |
| `.nv_debug_info_reg_sass` | `SHT_PROGBITS` | Per-instruction register liveness at SASS level |
| `.nv_debug_info_reg_type` | `SHT_PROGBITS` | Register type annotations (data types to physical registers) |

## Debug Sections -- Standard DWARF

The linker recognizes any section with prefix `.debug_` as a standard DWARF section. Common instances in cubins:

| Name | sh_type | Purpose |
|---|---|---|
| `.debug_abbrev` | `SHT_PROGBITS` | DWARF abbreviation table |
| `.debug_aranges` | `SHT_PROGBITS` | DWARF address range table |
| `.debug_frame` | `SHT_PROGBITS` | DWARF call frame information |
| `.debug_info` | `SHT_PROGBITS` | DWARF compilation unit info |
| `.debug_line` | `SHT_PROGBITS` | DWARF line number program |
| `.debug_loc` | `SHT_PROGBITS` | DWARF location lists |
| `.debug_macinfo` | `SHT_PROGBITS` | DWARF macro information |
| `.debug_pubnames` | `SHT_PROGBITS` | DWARF public name lookup table |
| `.debug_pubtypes` | `SHT_PROGBITS` | DWARF public type lookup table |
| `.debug_ranges` | `SHT_PROGBITS` | DWARF non-contiguous address ranges |
| `.debug_str` | `SHT_PROGBITS` | DWARF string table |

## Mercury Sections (sm100+)

All Mercury sections carry `SHF_CUDA_MERCURY` (`0x10000000`) in `sh_flags`. The merge phase skips them; FNLZR processes them after finalizing Mercury instructions into SASS.

### Mercury DWARF Debug

| Name | sh_type | Purpose |
|---|---|---|
| `.nv.merc.debug_abbrev` | `SHT_PROGBITS` | Mercury DWARF abbreviation table |
| `.nv.merc.debug_aranges` | `SHT_PROGBITS` | Mercury DWARF address range table |
| `.nv.merc.debug_frame` | `SHT_PROGBITS` | Mercury DWARF call frame information |
| `.nv.merc.debug_info` | `SHT_PROGBITS` | Mercury DWARF compilation unit info |
| `.nv.merc.debug_line` | `SHT_PROGBITS` | Mercury DWARF line number program |
| `.nv.merc.debug_loc` | `SHT_PROGBITS` | Mercury DWARF location lists |
| `.nv.merc.debug_macinfo` | `SHT_PROGBITS` | Mercury DWARF macro information |
| `.nv.merc.debug_pubnames` | `SHT_PROGBITS` | Mercury DWARF public name table |
| `.nv.merc.debug_pubtypes` | `SHT_PROGBITS` | Mercury DWARF public type table |
| `.nv.merc.debug_ranges` | `SHT_PROGBITS` | Mercury DWARF non-contiguous address ranges |
| `.nv.merc.debug_str` | `SHT_PROGBITS` | Mercury DWARF string table |

### Mercury NVIDIA Debug

| Name | sh_type | Purpose |
|---|---|---|
| `.nv.merc.nv_debug_info_reg_sass` | `SHT_PROGBITS` | Mercury register liveness at SASS level |
| `.nv.merc.nv_debug_info_reg_type` | `SHT_PROGBITS` | Mercury register type annotations |
| `.nv.merc.nv_debug_line_sass` | `SHT_PROGBITS` | Mercury SASS-level line table |
| `.nv.merc.nv_debug_ptx_txt` | `SHT_PROGBITS` | Mercury embedded PTX source text |

### Mercury Structural

| Name | sh_type | Purpose |
|---|---|---|
| `.nv.merc.nv.shared.reserved.` | `SHT_NOBITS` | Mercury reserved shared memory region |
| `.nv.merc.rela` | `SHT_RELA` | Mercury relocation section |
| `.nv.merc.symtab_shndx` | `SHT_SYMTAB_SHNDX` | Mercury extended section index table (>65535 sections) |

## Fatbin Sections (Host ELF)

These sections appear in the host-side ELF, not in the device cubin.

| Name | sh_type | Purpose |
|---|---|---|
| `.nv_fatbin` | `SHT_PROGBITS` | Raw fat binary payload containing cubins for all target architectures |
| `.nvFatBinSegment` | `SHT_PROGBITS` | Primary fat binary segment (`__cudaFatBinaryData` structure) |
| `__nv_relfatbin` | `SHT_PROGBITS` | Relocatable fat binary references needing host-side relocation |

## Standard ELF Infrastructure

| Name | sh_type | Purpose |
|---|---|---|
| `.shstrtab` | `SHT_STRTAB` | Section header string table (section name strings) |
| `.strtab` | `SHT_STRTAB` | String table for symbol names |
| `.symtab` | `SHT_SYMTAB` | Symbol table (CUDA-specific and standard ELF symbols) |

## Section Type Summary

| sh_type Value | Constant | Section(s) |
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
| `0x70000015` | `SHT_CUDA_SHARED_RESERVED` | `.nv.reservedSmem.*` |
| `0x70000064` | `SHT_CUDA_CONSTANT0` | `.nv.constant0` |
| `0x70000065`--`0x70000075` | `SHT_CUDA_CONSTANT1`--`17` | `.nv.constant1`--`.nv.constant17` |

## Section Flag Summary

| Flag Value | Constant | Meaning |
|---|---|---|
| `0x10000000` | `SHF_CUDA_MERCURY` | Mercury-format section (sm100+); merge phase skips, FNLZR processes |
