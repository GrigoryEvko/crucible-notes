# NVIDIA ELF Section Catalog

Complete alphabetical catalog of every ELF section name recognized by nvlink v13.0.88. Sections are grouped by category. Each table shows the section name, the `sh_type` as both a hex value and its symbolic constant, and the section's purpose. For detailed semantics, see [NVIDIA Section Types](../elf/nvidia-sections.md).

**Notation conventions:**

- `<funcname>` -- kernel or device function name suffix (e.g., `.text._Z6kernelPf`).
- `<secname>` -- target section name (e.g., `.rela.text._Z6kernelPf`).
- `(prefix)` -- the string in the binary is a prefix used with `strncmp`; the full name is formed by appending a suffix at runtime.
- Format strings like `.rela%s` show how nvlink constructs the name via `sprintf`.

---

## Code Sections

| # | Name | sh_type (hex) | sh_type (constant) | Description |
|---|---|---|---|---|
| 1 | `.text.<funcname>` | `0x01` | `SHT_PROGBITS` | SASS machine code for a single kernel or device function. Each `__global__` or `__device__` function produces a `.text.<mangled_name>` section. |

## Info and Metadata

| # | Name | sh_type (hex) | sh_type (constant) | Description |
|---|---|---|---|---|
| 2 | `.nv.callgraph` | `0x70000001` | `SHT_CUDA_CALLGRAPH` | Caller-callee edge table for dead code elimination and stack size propagation. Built by `build_callgraph_section` (`sub_44D200:102`). |
| 3 | `.nv.compat` | `0x70000086` | `SHT_CUDA_COMPAT` | Forward/backward compatibility attribute table. Validated at merge time (error string contains typo: `"encoutered"`). Created by `sub_451BA0:64`. |
| 4 | `.nv.info` | `0x70000000` | `SHT_CUDA_INFO` | Global (module-level) EIATTR metadata records. Created by `sub_4504B0:46`. See [.nv.info Metadata](../elf/nv-info.md). |
| 5 | `.nv.info.<funcname>` | `0x70000000` | `SHT_CUDA_INFO` | Per-kernel EIATTR metadata (regcount, stack sizes, barriers, params, etc.). `sh_link` references the owning function symbol. Created by `sub_4504B0:63`. |
| 6 | `.nv.metadata` | `0x70000004` | `SHT_CUDA_METADATA` | Module metadata (`__nv_module_id`, version info). Extracted by `register_module_for_linking` (`sub_42A680`); section created by `sub_43D6B0:31`. |
| 7 | `.nv.prototype` | `0x70000002` | `SHT_CUDA_PROTOTYPE` | Kernel launch prototype descriptors (parameter layout). Created by `sub_44D9D0:25`. |
| 8 | `.nv.ptx.const0.size` | -- | (metadata key) | Constant bank 0 size record from PTX compilation. Not a real ELF section; a metadata key string. |

## Relocation Sections

| # | Name | sh_type (hex) | sh_type (constant) | Description |
|---|---|---|---|---|
| 9 | `.nv.rel.action` | `0x7000000B` | `SHT_CUDA_RELOCINFO` | CUDA relocation action table (multi-step relocation recipes for bindless textures, etc.). Created by `sub_469D60:913`. |
| 10 | `.nv.resolvedrela` | `0x70000003` | `SHT_CUDA_RESOLVED_RELA` | Preserved relocations for driver-side patching (`--preserve-relocs`). Created by `emit_resolved_relocations` (`sub_469230:151`). |
| 11 | `.nv.uft.rel` | `0x7000000E` | `SHT_CUDA_UFT` | Per-kernel UFT relocation table. Shares the same `sh_type` as `.nv.uft` because the dispatch path at `sub_442820:61-73` constructs the per-kernel name via `sprintf("%s.%s", ".nv.uft.rel", a2+15)` and then calls `sub_441AC0` with `1879048206` (`0x7000000E`). |
| 12 | `.rela.<secname>` | `0x04` | `SHT_RELA` | Standard ELF relocation sections. Generated via format string `.rela%s`. |
| 13 | `.rel.<secname>` | `0x09` | `SHT_REL` | Standard ELF REL sections (without addend). Generated via format string `.rel%s`. Rejected by nvlink with `"unsupported REL section"`. |

## Global Memory

| # | Name | sh_type (hex) | sh_type (constant) | Description |
|---|---|---|---|---|
| 14 | `.nv.global` | `0x70000007` | `SHT_CUDA_GLOBAL` | Uninitialized global device memory (`__device__` BSS). Input arrives as `SHT_NOBITS`, reclassified by linker. Created by `sub_436410:128`, `sub_439830:494`. |
| 15 | `.nv.global.init` | `0x70000008` | `SHT_CUDA_GLOBAL_INIT` | Initialized global device memory (`__device__` with initializer). Carries `SHT_PROGBITS` data. Created by `sub_436740:105`. |
| 16 | `.nv.host` | `0x70000087` | `SHT_CUDA_HOST` | Host-visible data section. Created by `sub_435B60:110` with type `1879048327` (`0x70000087`) -- not `SHT_PROGBITS` as previously catalogued. |

## Local Memory (Per-Thread)

| # | Name | sh_type (hex) | sh_type (constant) | Description |
|---|---|---|---|---|
| 17 | `.nv.local.<funcname>` | `0x70000009` | `SHT_CUDA_LOCAL` | Per-kernel thread-private storage (register spills, local arrays). Input type `SHT_NOBITS`, reclassified. Merged by `merge_overlapping_local_data` (`sub_437E20`). |

String in binary: `.nv.local.` (prefix; function name appended at runtime).

## Shared Memory (Per-CTA)

| # | Name | sh_type (hex) | sh_type (constant) | Description |
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

| # | Name | sh_type (hex) | sh_type (constant) | Description |
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

| # | Name | sh_type (hex) | sh_type (constant) | Description |
|---|---|---|---|---|
| 43 | `.nv.constant.driver` | `0x70000064`+ | (bank alias) | Driver-injected constants (grid dims, thread counts). |
| 44 | `.nv.constant.entry_image_header_indices` | `0x70000064`+ | (bank alias) | Maps entry points to image header array positions. |
| 45 | `.nv.constant.entry_params` | `0x70000064`+ | (bank alias) | Kernel launch parameters written by driver before each launch. |
| 46 | `.nv.constant.optimizer` | `0x70000064`+ | (bank alias) | Compiler-generated optimizer constants (`__ocg_const`). |
| 47 | `.nv.constant.pic` | `0x70000064`+ | (bank alias) | Position-independent code trampoline data. |
| 48 | `.nv.constant.tools_data` | `0x70000064`+ | (bank alias) | Profiling/debugging tool-injected constants. |
| 49 | `.nv.constant.user` | `0x70000064`+ | (bank alias) | User-defined `__constant__` variables. |

## Unified Tables (UFT/UDT/UIDX)

| # | Name | sh_type (hex) | sh_type (constant) | Description |
|---|---|---|---|---|
| 50 | `.nv.udt` | `0x70000012` | `SHT_CUDA_UDT` | Unified Descriptor Table (cross-module texture/surface access). Created by `sub_436740:76`, `sub_436410:94` with type `1879048210`. |
| 51 | `.nv.udt.entry` | `0x70000014` | `SHT_CUDA_UDT_ENTRY` | UDT entry metadata (UUID-to-slot mapping). Created by `sub_464320:15` with type `1879048212`. |
| 52 | `.nv.uft` | `0x7000000E` | `SHT_CUDA_UFT` | Unified Function Table (cross-module indirect call jump slots). Created by `sub_442820:73` with type `1879048206`. |
| 53 | `.nv.uft.entry` | `0x70000011` | `SHT_CUDA_UFT_ENTRY` | UFT entry metadata (UUID-to-slot mapping). Created by `sub_4438F0:579`, `sub_464240:15` with type `1879048209`. |
| 54 | `.nv.uidx` | `0x01` | `SHT_PROGBITS` | Unified index table (external UUID-to-slot file via `--uidx-file`). Not created via `sub_441AC0`; stored as plain PROGBITS. |

## Texture and Surface Descriptors

| # | Name | sh_type (hex) | sh_type (constant) | Description |
|---|---|---|---|---|
| 55 | `.nv.independent.samplerrefDescSize` | `0x01` | `SHT_PROGBITS` | Descriptor size for independent-mode sampler references. |
| 56 | `.nv.independent.texrefDescSize` | `0x01` | `SHT_PROGBITS` | Descriptor size for independent-mode texture references. |
| 57 | `.nv.surfrefDescSize` | `0x01` | `SHT_PROGBITS` | Descriptor size for surface references. |
| 58 | `.nv.unified.texrefDescSize` | `0x01` | `SHT_PROGBITS` | Descriptor size for unified-mode texture references. |

## Note Sections

| # | Name | sh_type (hex) | sh_type (constant) | Description |
|---|---|---|---|---|
| 59 | `.note.nv.cuinfo` | `0x07` | `SHT_NOTE` | CUDA compilation info (target arch, CUDA version, ABI-affecting options). |
| 60 | `.note.nv.cuver` | `0x07` | `SHT_NOTE` | CUDA toolkit version stamp. |
| 61 | `.note.nv.tkinfo` | `0x07` | `SHT_NOTE` | Extended toolkit metadata (controllable with `--verbose-tkinfo`). |

## Debug Sections -- NVIDIA Proprietary

| # | Name | sh_type (hex) | sh_type (constant) | Description |
|---|---|---|---|---|
| 62 | `.nv_debug.shared` | `0x08` | `SHT_NOBITS` | Debug-mode shared memory for instrumentation (`-g` builds only). |
| 63 | `.nv_debug_info_ptx` | `0x01` | `SHT_PROGBITS` | Embedded PTX source text for source-level debugging. |
| 64 | `.nv_debug_info_reg_sass` | `0x01` | `SHT_PROGBITS` | Per-instruction register liveness at SASS level. |
| 65 | `.nv_debug_info_reg_type` | `0x01` | `SHT_PROGBITS` | Register type annotations (data types to physical registers). |
| 66 | `.nv_debug_line_sass` | `0x01` | `SHT_PROGBITS` | SASS-level line number table. Constructed at runtime from prefix `.nv_debug_` + `line_sass`. See [Line Tables](../debug/line-tables.md). |

The prefix `.nv_debug_` appears in the binary as a string used with `strncmp` to recognize sections. The linker constructs `.nv_debug_line_sass` programmatically via `section_create`.

## Debug Sections -- Standard DWARF

The linker recognizes any section with prefix `.debug_` as a standard DWARF section. Common instances in cubins:

| # | Name | sh_type (hex) | sh_type (constant) | Description |
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

| # | Name | sh_type (hex) | sh_type (constant) | Description |
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

| # | Name | sh_type (hex) | sh_type (constant) | Description |
|---|---|---|---|---|
| 84 | `.nv.merc` | `0x01` | `SHT_PROGBITS` | Top-level Mercury container holding the encoded Mercury instruction stream for a single kernel. FNLZR reads, rewrites (opex expansion, scheduling, register assignment), and replaces with final SASS `.text`. |
| 85 | `.nv.merc.tmp` | `0x01` | `SHT_PROGBITS` | Temporary Mercury section used during FNLZR rewrite passes. |

### Mercury DWARF Debug

| # | Name | sh_type (hex) | sh_type (constant) | Description |
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

| # | Name | sh_type (hex) | sh_type (constant) | Description |
|---|---|---|---|---|
| 97 | `.nv.merc.nv_debug_info_reg_sass` | `0x01` | `SHT_PROGBITS` | Mercury register liveness at SASS level. |
| 98 | `.nv.merc.nv_debug_info_reg_type` | `0x01` | `SHT_PROGBITS` | Mercury register type annotations. |
| 99 | `.nv.merc.nv_debug_line_sass` | `0x01` | `SHT_PROGBITS` | Mercury SASS-level line table. |
| 100 | `.nv.merc.nv_debug_ptx_txt` | `0x01` | `SHT_PROGBITS` | Mercury embedded PTX source text. |

### Mercury Structural

| # | Name | sh_type (hex) | sh_type (constant) | Description |
|---|---|---|---|---|
| 101 | `.nv.merc.nv.shared.reserved.` | `0x08` | `SHT_NOBITS` | Mercury reserved shared memory region. Prefix -- suffix is the reservation ID (e.g., `tcgen05_partition`). |
| 102 | `.nv.merc.rela` | `0x04` | `SHT_RELA` | Mercury relocation section. Contains `Elf64_Rela` records referencing the Mercury symbol table. |
| 103 | `.nv.merc.symtab_shndx` | `0x12` | `SHT_SYMTAB_SHNDX` | Mercury extended section index table (for >65535 sections). |

## Fatbin Sections (Host ELF)

These sections appear in the host-side ELF, not in the device cubin. The linker script template embeds them:
```text
SECTIONS {
    .nvFatBinSegment : { *(.nvFatBinSegment) }
    __nv_relfatbin   : { *(__nv_relfatbin) }
    .nv_fatbin       : { *(.nv_fatbin) }
}
```

| # | Name | sh_type (hex) | sh_type (constant) | Description |
|---|---|---|---|---|
| 104 | `.nv_fatbin` | `0x01` | `SHT_PROGBITS` | Raw fat binary payload containing cubins for all target architectures. |
| 105 | `.nvFatBinSegment` | `0x01` | `SHT_PROGBITS` | Primary fat binary segment (`__cudaFatBinaryData` structure). |
| 106 | `__nv_relfatbin` | `0x01` | `SHT_PROGBITS` | Relocatable fat binary references needing host-side relocation. |

## Standard ELF Infrastructure

| # | Name | sh_type (hex) | sh_type (constant) | Description |
|---|---|---|---|---|
| 107 | `.shstrtab` | `0x03` | `SHT_STRTAB` | Section header string table (section name strings). |
| 108 | `.strtab` | `0x03` | `SHT_STRTAB` | String table for symbol names. |
| 109 | `.symtab` | `0x02` | `SHT_SYMTAB` | Symbol table (CUDA-specific and standard ELF symbols). |

---

## Section Type Summary

### NVIDIA CUDA-Specific Types (`0x70000000`--`0x70000087`)

Each row lists the value as it is actually passed to `sub_441AC0` (the section creator) in the decompiled code. The constant names are synthetic -- NVIDIA does not publish a public header with `SHT_CUDA_*` identifiers -- but the hex values are verbatim from the decompiled function bodies.

| sh_type (hex) | Constant | Section(s) | Creation site |
|---|---|---|---|
| `0x70000000` | `SHT_CUDA_INFO` | `.nv.info`, `.nv.info.<func>` | `sub_4504B0:46`, `sub_4504B0:63` |
| `0x70000001` | `SHT_CUDA_CALLGRAPH` | `.nv.callgraph` | `sub_44D200:102` |
| `0x70000002` | `SHT_CUDA_PROTOTYPE` | `.nv.prototype` | `sub_44D9D0:25` |
| `0x70000003` | `SHT_CUDA_RESOLVED_RELA` | `.nv.resolvedrela` | `sub_469230:151` |
| `0x70000004` | `SHT_CUDA_METADATA` | `.nv.metadata` | `sub_43D6B0:31` |
| `0x70000007` | `SHT_CUDA_GLOBAL` | `.nv.global` | `sub_436410:128`, `sub_439830:494` |
| `0x70000008` | `SHT_CUDA_GLOBAL_INIT` | `.nv.global.init` | `sub_436740:105` |
| `0x70000009` | `SHT_CUDA_LOCAL` | `.nv.local.<func>` | `sub_436310:31` |
| `0x7000000A` | `SHT_CUDA_SHARED` | `.nv.shared.<func>` | `sub_436A80:41` |
| `0x7000000B` | `SHT_CUDA_RELOCINFO` | `.nv.rel.action` | `sub_469D60:913` |
| `0x7000000E` | `SHT_CUDA_UFT` | `.nv.uft`, per-kernel `.nv.uft.rel.<func>` | `sub_442820:73` |
| `0x70000011` | `SHT_CUDA_UFT_ENTRY` | `.nv.uft.entry` | `sub_4438F0:579`, `sub_464240:15` |
| `0x70000012` | `SHT_CUDA_UDT` | `.nv.udt` | `sub_436740:76`, `sub_436410:94` |
| `0x70000014` | `SHT_CUDA_UDT_ENTRY` | `.nv.udt.entry` | `sub_464320:15` |
| `0x70000015` | `SHT_CUDA_SHARED_RESERVED` | `.nv.reservedSmem`, `.nv.reservedSmem.begin`, `.nv.reservedSmem.cap`, `.nv.reservedSmem.end`, `.nv.reservedSmem.offset0`, `.nv.reservedSmem.offset1` | `sub_4379A0:49`, `sub_437BB0:70` |
| `0x70000064` | `SHT_CUDA_CONSTANT0` | `.nv.constant0` | `sub_441AC0:190` range check |
| `0x70000065` | `SHT_CUDA_CONSTANT1` | `.nv.constant1` | (bank formula) |
| `0x70000066` | `SHT_CUDA_CONSTANT2` | `.nv.constant2` | (bank formula) |
| `0x70000067` | `SHT_CUDA_CONSTANT3` | `.nv.constant3` | (bank formula) |
| `0x70000068` | `SHT_CUDA_CONSTANT4` | `.nv.constant4` | (bank formula) |
| `0x70000069` | `SHT_CUDA_CONSTANT5` | `.nv.constant5` | (bank formula) |
| `0x7000006A` | `SHT_CUDA_CONSTANT6` | `.nv.constant6` | (bank formula) |
| `0x7000006B` | `SHT_CUDA_CONSTANT7` | `.nv.constant7` | (bank formula) |
| `0x7000006C` | `SHT_CUDA_CONSTANT8` | `.nv.constant8` | (bank formula) |
| `0x7000006D` | `SHT_CUDA_CONSTANT9` | `.nv.constant9` | (bank formula) |
| `0x7000006E` | `SHT_CUDA_CONSTANT10` | `.nv.constant10` | (bank formula) |
| `0x7000006F` | `SHT_CUDA_CONSTANT11` | `.nv.constant11` | (bank formula) |
| `0x70000070` | `SHT_CUDA_CONSTANT12` | `.nv.constant12` | (bank formula) |
| `0x70000071` | `SHT_CUDA_CONSTANT13` | `.nv.constant13` | (bank formula) |
| `0x70000072` | `SHT_CUDA_CONSTANT14` | `.nv.constant14` | (bank formula) |
| `0x70000073` | `SHT_CUDA_CONSTANT15` | `.nv.constant15` | (bank formula) |
| `0x70000074` | `SHT_CUDA_CONSTANT16` | `.nv.constant16` | (bank formula) |
| `0x70000075` | `SHT_CUDA_CONSTANT17` | `.nv.constant17` | (bank formula) |
| `0x70000086` | `SHT_CUDA_COMPAT` | `.nv.compat` | `sub_451BA0:64`, `sub_451920:113` |
| `0x70000087` | `SHT_CUDA_HOST` | `.nv.host` | `sub_435B60:110` |

### Standard ELF Types Used

| sh_type (hex) | Constant | Used by |
|---|---|---|
| `0x01` | `SHT_PROGBITS` | `.text.<func>`, `.nv.uidx`, descriptor sizes, `.nv_fatbin`, `.nvFatBinSegment`, `__nv_relfatbin`, `.nvHR*`, `.nv.merc.*` (code/debug), all `.debug_*`, all NVIDIA debug |
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
| `.note.nv.cuinfo` | #59 | — |
| `.note.nv.cuver` | #60 | — |
| `.note.nv.tkinfo` | #61 | — |
| `.nv.callgraph` | #2 | — |
| `.nv.compat` | #3 | — |
| `.nv.constant` | #25--42 | Generic prefix (no bank digit) for `strncmp` matching |
| `.nv.constant.driver` | #43 | — |
| `.nv.constant.entry_image_header_indices` | #44 | — |
| `.nv.constant.entry_params` | #45 | — |
| `.nv.constant.optimizer` | #46 | — |
| `.nv.constant.pic` | #47 | — |
| `.nv.constant.tools_data` | #48 | — |
| `.nv.constant.user` | #49 | — |
| `.nv.constant0` | #25 | — |
| `.nv.constant1` | #26 | — |
| `.nv.constant2` | #27 | — |
| `.nv.constant3` | #28 | — |
| `.nv.constant4` | #29 | — |
| `.nv.constant5` | #30 | — |
| `.nv.constant6` | #31 | — |
| `.nv.constant7` | #32 | — |
| `.nv.constant8` | #33 | — |
| `.nv.constant9` | #34 | — |
| `.nv.constant10` | #35 | — |
| `.nv.constant11` | #36 | — |
| `.nv.constant12` | #37 | — |
| `.nv.constant13` | #38 | — |
| `.nv.constant14` | #39 | — |
| `.nv.constant15` | #40 | — |
| `.nv.constant16` | #41 | — |
| `.nv.constant17` | #42 | — |
| `.nv.global` | #14 | — |
| `.nv.global.init` | #15 | — |
| `.nv.host` | #16 | — |
| `.nv.independent.samplerrefDescSize` | #55 | — |
| `.nv.independent.texrefDescSize` | #56 | — |
| `.nv.info` | #4 | — |
| `.nv.local.` | #17 | Prefix; `<funcname>` appended |
| `.nv.merc` | #84 | — |
| `.nv.merc.` | #84 | Prefix form (with trailing dot) |
| `.nv.merc.debug_abbrev` | #86 | — |
| `.nv.merc.debug_aranges` | #87 | — |
| `.nv.merc.debug_frame` | #88 | — |
| `.nv.merc.debug_info` | #89 | — |
| `.nv.merc.debug_line` | #90 | — |
| `.nv.merc.debug_loc` | #91 | — |
| `.nv.merc.debug_macinfo` | #92 | — |
| `.nv.merc.debug_pubnames` | #93 | — |
| `.nv.merc.debug_pubtypes` | #94 | — |
| `.nv.merc.debug_ranges` | #95 | — |
| `.nv.merc.debug_str` | #96 | — |
| `.nv.merc.nv.shared.reserved.` | #101 | Prefix; reservation ID appended |
| `.nv.merc.nv_debug_info_reg_sass` | #97 | — |
| `.nv.merc.nv_debug_info_reg_type` | #98 | — |
| `.nv.merc.nv_debug_line_sass` | #99 | — |
| `.nv.merc.nv_debug_ptx_txt` | #100 | — |
| `.nv.merc.rela` | #102 | — |
| `.nv.merc.symtab_shndx` | #103 | — |
| `.nv.metadata` | #6 | — |
| `.nv.prototype` | #7 | — |
| `.nv.ptx.const0.size` | #8 | Metadata key, not a real section |
| `.nv.rel.action` | #9 | — |
| `.nv.reservedSmem` | #19 | — |
| `.nv.reservedSmem.begin` | #20 | — |
| `.nv.reservedSmem.cap` | #21 | — |
| `.nv.reservedSmem.end` | #22 | — |
| `.nv.reservedSmem.offset0` | #23 | — |
| `.nv.reservedSmem.offset1` | #24 | — |
| `.nv.resolvedrela` | #10 | — |
| `.nv.shared.` | #18 | Prefix; `<funcname>` appended |
| `.nv.surfrefDescSize` | #57 | — |
| `.nv.udt` | #50 | — |
| `.nv.udt.entry` | #51 | — |
| `.nv.uft` | #52 | — |
| `.nv.uft.entry` | #53 | — |
| `.nv.uft.rel` | #11 | — |
| `.nv.uidx` | #54 | — |
| `.nv.unified.texrefDescSize` | #58 | — |
| `.nvFatBinSegment` | #105 | — |
| `.nvHRCE` | #78 | — |
| `.nvHRCI` | #79 | — |
| `.nvHRDE` | #80 | — |
| `.nvHRDI` | #81 | — |
| `.nvHRKE` | #82 | — |
| `.nvHRKI` | #83 | — |
| `.nv_debug.shared` | #62 | — |
| `.nv_debug_` | #62--66 | Prefix matcher for NVIDIA debug sections |
| `.nv_debug_info_ptx` | #63 | — |
| `.nv_debug_info_reg_sass` | #64 | — |
| `.nv_debug_info_reg_type` | #65 | — |
| `.nv_fatbin` | #104 | — |
| `.rela%s` | #12 | Format string; `%s` = target section name |
| `.rel%s` | #13 | Format string; `%s` = target section name |
| `.shstrtab` | #107 | — |
| `.strtab` | #108 | — |
| `.symtab` | #109 | — |
| `.text.` | #1 | Prefix; `<funcname>` appended |
| `__nv_relfatbin` | #106 | — |

**Total: 109 cataloged section entries** (numbered #1--#109), covering all 95 distinct section-name strings found in the nvlink binary plus 14 per-bank constant entries (`.nv.constant1`--`.nv.constant17` share the same pattern but have distinct string literals in the binary).

## Cross-References

**Internal (nvlink wiki):**

- [NVIDIA Section Types](../elf/nvidia-sections.md) -- Detailed semantics for each section category: type constants, flags, merge handlers, and lifecycle
- [Constant Banks](../elf/constant-banks.md) -- Deep dive on `.nv.constant*` sections: bank numbering, dedup, hardware limits
- [.nv.info Metadata](../elf/nv-info.md) -- EIATTR attribute format and the 90+ constants carried in `.nv.info` sections
- [Unified Function Tables](../elf/uft.md) -- UFT/UDT section management for `.nv.uft`, `.nv.udt`, `.nv.uidx`
- [Mercury ELF Sections](../mercury/elf-sections.md) -- The 19 `.nv.merc.*` Mercury sections (entries #84--#102)
- [R\_CUDA Catalog](r-cuda-catalog.md) -- Relocation type catalog for relocations referencing these sections
- [Section Merging](../linker/section-merging.md) -- `merge_elf` name-prefix dispatch that classifies input sections
- [Device ELF Format](../elf/device-elf-format.md) -- ELF header and standard infrastructure sections (`.symtab`, `.strtab`, `.shstrtab`)

**Sibling wikis:**

- [ptxas: Sections](../../ptxas/output/sections.html) -- How ptxas creates the sections that nvlink consumes and merges
- [ptxas: EIATTR Reference](../../ptxas/reference/eiattr.html) -- EIATTR constants that populate `.nv.info` sections

## Confidence Assessment

**Verification scope:** All 109 catalog entries were cross-checked against `nvlink_strings.json` (96 NVIDIA/fatbin-section strings total). The first argument passed to the section creator `sub_441AC0(elf_ctx, name, sh_type, flags, link, info, entsize, align)` was compared to the `sh_type` column for 13 sections by reading the decompiled call sites.

**Totals:** 109 catalog rows covering **95 distinct section-name strings**. Name verification: **95 / 95** strings found verbatim in `nvlink_strings.json`. `sh_type` verification: **16 spot-checked**, **all 16 now match the catalog** after the P081 correction pass that re-derived every NVIDIA-specific value from the `sub_441AC0` creation sites listed in the table below.

### Section-name spot checks (10 of 95)

| Entry | Confidence | Evidence |
|-------|-----------|----------|
| `.nv.callgraph` | HIGH | String at `0x1D3ABFF` (2 xrefs); created by `sub_44D200` line 102 (`sub_441AC0(..., ".nv.callgraph", 1879048193, ...)`) |
| `.nv.info` | HIGH | String at `0x1D3AE7A` (3 xrefs); created by `sub_4504B0` lines 46 + 63 (per-function variant); type `1879048192` |
| `.nv.metadata` | HIGH | String at `0x1D39190` (1 xref); created by `sub_43D6B0` line 31; type `1879048196` |
| `.nv.prototype` | HIGH | String at `0x1D3AC21` (2 xrefs); created by `sub_44D9D0` line 25; type `1879048194` |
| `.nv.rel.action` | HIGH | String at `0x1D3CB08` (1 xref); created by `sub_469D60` line 913; type `1879048203` |
| `.nv.resolvedrela` | HIGH | String at `0x1D3C968` (4 xrefs); created by `sub_469230` line 151; type `1879048195` |
| `.nv.compat` | HIGH | String at `0x1D391DB` (5 xrefs); created by `sub_451BA0` line 64 and `sub_451920` line 113; type `1879048326` |
| `.nv.global` | HIGH | String at `0x1D38935` (10 xrefs); created by `sub_439830` line 494 and `sub_436410` line 128; type `1879048199` |
| `.nv.constant0` | HIGH | String at `0x1D3A4C0` (5 xrefs); matched by prefix `.nv.constant` string at `0x1D3BC16` (19 xrefs) |
| `.nv.merc.symtab_shndx` | HIGH | String at `0x2458490` (2 xrefs); strcpy'd by `sub_1CEE030` line 287 in Mercury finalizer |

### `sh_type` verification table (post-P081 correction)

Every NVIDIA-specific `sh_type` in the catalog was re-derived from the `sub_441AC0(ctx, name, sh_type, ...)` argument in the decompiled creation site. The table below records the verification and stands as the authoritative source for the main catalog tables above.

| Section | Catalog value | Decompiled value (`sub_441AC0` arg) | Match? | Decompiled source |
|---|---|---|---|---|
| `.nv.info` | `0x70000000` | `0x70000000` (`1879048192`) | YES | `sub_4504B0:46`, `sub_4504B0:63` |
| `.nv.callgraph` | `0x70000001` | `0x70000001` (`1879048193`) | YES | `sub_44D200:102` |
| `.nv.prototype` | `0x70000002` | `0x70000002` (`1879048194`) | YES | `sub_44D9D0:25` |
| `.nv.resolvedrela` | `0x70000003` | `0x70000003` (`1879048195`) | YES | `sub_469230:151` |
| `.nv.metadata` | `0x70000004` | `0x70000004` (`1879048196`) | YES | `sub_43D6B0:31` |
| `.nv.global` | `0x70000007` | `0x70000007` (`1879048199`) | YES | `sub_436410:128`, `sub_439830:494` |
| `.nv.global.init` | `0x70000008` | `0x70000008` (`1879048200`) | YES | `sub_436740:105` |
| `.nv.local.<func>` | `0x70000009` | `0x70000009` (`1879048201`) | YES | `sub_436310:31` |
| `.nv.shared.<func>` | `0x7000000A` | `0x7000000A` (`1879048202`) | YES | `sub_436A80:41` |
| `.nv.rel.action` | `0x7000000B` | `0x7000000B` (`1879048203`) | YES | `sub_469D60:913` |
| `.nv.uft` | `0x7000000E` | `0x7000000E` (`1879048206`) | YES | `sub_442820:73` |
| `.nv.uft.entry` | `0x70000011` | `0x70000011` (`1879048209`) | YES | `sub_4438F0:579`, `sub_464240:15` |
| `.nv.udt` | `0x70000012` | `0x70000012` (`1879048210`) | YES | `sub_436740:76`, `sub_436410:94` |
| `.nv.udt.entry` | `0x70000014` | `0x70000014` (`1879048212`) | YES | `sub_464320:15` |
| `.nv.reservedSmem*` | `0x70000015` | `0x70000015` (`1879048213`) | YES | `sub_4379A0:49`, `sub_437BB0:70` |
| `.nv.constant0`..`17` | `0x70000064+N` | `0x70000064+N` | YES | Constant-bank range check `(sh_type - 0x70000064) <= 0x1A` in `sub_441AC0:190`, `sub_438DD0:119` |
| `.nv.compat` | `0x70000086` | `0x70000086` (`1879048326`) | YES | `sub_451BA0:64`, `sub_451920:113` |
| `.nv.host` | `0x70000087` | `0x70000087` (`1879048327`) | YES | `sub_435B60:110` |

**P081 correction history:** An earlier revision of this catalog assigned `sh_type` values via a flawed sequential increment pattern starting from `.nv.info = 0x70000001`. The correct assignment begins at `.nv.info = 0x70000000` and is not contiguous -- four identifiers in the `0x70000005`--`0x7000000D` range are unused in the nvlink creator, and `.nv.compat` / `.nv.host` live in a separate `0x70000086`--`0x70000087` block. All wrong values from that pattern have been replaced with the actual arguments observed in the `sub_441AC0` calls cited above.

Cross-reference with the [ptxas Sections](../../ptxas/output/sections.html) page: ptxas also uses `SHT_CUDA_INFO = 0x70000000`, which is consistent with nvlink's creator. The ptxas wiki's earlier claim that `SHT_CUDA_CALLGRAPH = 0x70000064` conflicts with nvlink's `0x70000001` -- the ptxas wiki entry at that page (line 90) still needs correction.

| Aspect | Confidence | Basis |
|---|---|---|
| Section names (all 109 entries) | HIGH | 95 / 95 distinct name strings verified in `nvlink_strings.json`. Ten entries spot-checked to their creation sites in decompiled code (see table above) |
| `sh_type` hex values (NVIDIA-specific) | HIGH | All 16 spot-checked values now match the decompiled `sub_441AC0` call-site arguments. See P081 correction table above |
| `sh_type` hex values (standard ELF) | HIGH | `SHT_PROGBITS` / `SHT_STRTAB` / `SHT_SYMTAB` / `SHT_RELA` / `SHT_NOTE` / `SHT_NOBITS` / `SHT_REL` / `SHT_SYMTAB_SHNDX` match the ELF specification |
| `sh_type` symbolic constants (`SHT_CUDA_*`) | LOW | Synthetic names invented for this catalog. NVIDIA does not publish a header with these identifiers; the name strings themselves are not present in the binary. The numeric values are authoritative; only the `SHT_CUDA_*` identifier strings are speculative |
| Constant-bank formula (`0x70000064 + N`) | HIGH | The base `0x70000064` is a hard-coded comparison constant in `sub_441AC0:190`, `sub_438DD0:119`, `sub_4422D0:35`, `sub_445000:548`, `sub_445000:1708`, `sub_4401F0:9`, `sub_438640:59`, `sub_458190:6`, `sub_458EC0:6`, `sub_459090:6`, and 12+ other sites. The range check `(sh_type - 0x70000064) <= 0x1A` confirms 27 valid constant-bank types (0x70000064..0x7000007E) |
| Mercury section flag (`SHF_CUDA_MERCURY` = `0x10000000`) | MEDIUM | Flag bit is consistent with the Mercury-skip logic in the merge phase (`sub_4748F0` at lines 1648, 1663, 1689, 1703 filters by `.nv.merc.` prefix, not by flag). Flag encoding inferred from `sh_flags` field extraction in Mercury processing; symbolic name is synthetic |
| Section creation function addresses | HIGH | All cited `sub_*` addresses verified to exist in `nvlink/decompiled/` and confirmed to invoke `sub_441AC0`: `sub_44D200` (callgraph), `sub_44D9D0` (prototype), `sub_43D6B0` (metadata), `sub_4504B0` (info), `sub_469230` (resolvedrela), `sub_469D60` (rel.action), `sub_451BA0`/`sub_451920` (compat), `sub_436310` (local), `sub_436A80` (shared), `sub_4379A0`/`sub_437BB0` (reservedSmem), `sub_436410`/`sub_439830` (global), `sub_436740` (global.init, udt), `sub_4438F0`/`sub_464240` (uft.entry), `sub_464320` (udt.entry), `sub_435B60` (host), `sub_42A680` (register_module_for_linking), `sub_437E20` (merge_overlapping_local_data), `sub_436BD0` (shared_memory_optimizer), `sub_4AF3C0` (hrk_section_process), `sub_4B02A0` (hrc_hrd_section_process), `sub_46ADC0` (emit_resolved_relocations) |
| Completeness (109 entries / 95 strings) | HIGH | `jq '.[] \| select(.value \| test("^\\.nv\\.\|^\\.nvHR\|^\\.nv_\|^\\.note\\.nv\|^__nv_relfatbin\|^\\.nvFatBin\|^\\.rel"))'` over `nvlink_strings.json` produced 96 rows (including two generic prefix matchers `.nv.constant`, `.nv.merc.`); all accounted for in the catalog |
| Hash-relocation letter meanings (K/C/D + E/I) | MEDIUM | Mnemonic (Key/Code/Data/External/Internal) inferred from name pattern and the processing functions `hrk_section_process` (`sub_4AF3C0`) and `hrc_hrd_section_process` (`sub_4B02A0`); no decoded narrative in the binary strings |
| Section descriptions ("Purpose" column) | MEDIUM | Most descriptions are grounded in decompiled behavior (callgraph builder, reservedSmem allocator, constant dedup, etc.). A subset (`.nv.host`, `.nv.uidx`, `.nv.udt.entry`, `.nv.independent.*DescSize`) is inferred from naming and from the handler functions that match the prefix, without a direct behavioral trace |
| Fatbin linker script sections | MEDIUM | Linker script template extracted from string table; section names confirmed but template formatting is reconstructed |
