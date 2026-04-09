# NVIDIA Section Types

CUDA device ELF objects (cubins) use processor-specific ELF section types, flags, and naming conventions that extend the standard ELF format to describe GPU memory spaces, kernel metadata, constant banks, shared memory, unified descriptor/function tables, and debug information. This page catalogs every NVIDIA-specific section name identified in nvlink v13.0.88, organized by functional category, with section type codes and the linker operations that consume them.

## Section Type Constants

CUDA defines custom section types in the `SHT_LOPROC`--`SHT_HIPROC` range (`0x70000000`--`0x7FFFFFFF`). nvlink uses these types internally after reclassifying input sections based on their names. The reclassification happens in `merge_elf` (`sub_45E7D0`): input sections arrive with standard ELF types (`SHT_PROGBITS`, `SHT_NOBITS`), and the linker maps them to CUDA-specific types for dispatch.

| Type constant | Value | Description |
|---|---|---|
| `SHT_CUDA_INFO` | `0x70000000` | `.nv.info` and `.nv.info.*` per-kernel metadata (EIATTR attributes). Created by `sub_4504B0:46,63`. |
| `SHT_CUDA_CALLGRAPH` | `0x70000001` | `.nv.callgraph` -- call edge table for dead code elimination and stack propagation. Created by `sub_44D200:102`. |
| `SHT_CUDA_PROTOTYPE` | `0x70000002` | `.nv.prototype` -- kernel launch prototype descriptors. Created by `sub_44D9D0:25`. |
| `SHT_CUDA_RESOLVED_RELA` | `0x70000003` | `.nv.resolvedrela` -- relocations preserved after linking (`--preserve-relocs`). Created by `sub_469230:151`. |
| `SHT_CUDA_METADATA` | `0x70000004` | `.nv.metadata` -- linker metadata (module IDs, version info). Created by `sub_43D6B0:31`. |
| `SHT_CUDA_GLOBAL` | `0x70000007` | `.nv.global` -- uninitialized global device memory (`__device__` BSS). Created by `sub_436410:128`, `sub_439830:494`. |
| `SHT_CUDA_GLOBAL_INIT` | `0x70000008` | `.nv.global.init` -- initialized global device memory (`__device__` with initializer). Created by `sub_436740:105`. |
| `SHT_CUDA_LOCAL` | `0x70000009` | `.nv.local.*` -- per-kernel local (thread-private) memory. Created by `sub_436310:31`. |
| `SHT_CUDA_SHARED` | `0x7000000A` | `.nv.shared.*` -- per-kernel shared memory. Created by `sub_436A80:41`. |
| `SHT_CUDA_RELOCINFO` | `0x7000000B` | `.nv.rel.action` -- CUDA-specific relocation action table. Created by `sub_469D60:913`. |
| `SHT_CUDA_UFT` | `0x7000000E` | `.nv.uft` -- Unified Function Table jump slots (and per-kernel `.nv.uft.rel.*` reloc variants). Created by `sub_442820:73`. |
| `SHT_CUDA_UFT_ENTRY` | `0x70000011` | `.nv.uft.entry` -- UFT entry metadata. Created by `sub_4438F0:579`, `sub_464240:15`. |
| `SHT_CUDA_UDT` | `0x70000012` | `.nv.udt` -- Unified Descriptor Table. Created by `sub_436740:76`, `sub_436410:94`. |
| `SHT_CUDA_UDT_ENTRY` | `0x70000014` | `.nv.udt.entry` -- UDT entry metadata. Created by `sub_464320:15`. |
| `SHT_CUDA_SHARED_RESERVED` | `0x70000015` | `.nv.reservedSmem.*` -- compiler-reserved shared memory regions. Created by `sub_4379A0:49`, `sub_437BB0:70`. |
| `SHT_CUDA_CONSTANT0` | `0x70000064` | `.nv.constant0` -- constant bank 0 (kernel parameters, driver-managed) |
| `SHT_CUDA_CONSTANT1` | `0x70000065` | `.nv.constant1` |
| ... | ... | Banks 2--16 follow sequentially |
| `SHT_CUDA_CONSTANT17` | `0x70000075` | `.nv.constant17` -- highest numbered constant bank |
| `SHT_CUDA_COMPAT` | `0x70000086` | `.nv.compat` -- forward/backward compatibility attribute table. Created by `sub_451BA0:64`, `sub_451920:113`. |
| `SHT_CUDA_HOST` | `0x70000087` | `.nv.host` -- host-visible data section. Created by `sub_435B60:110`. |

**Value gaps:** Identifiers `0x70000005`, `0x70000006`, `0x7000000C`, `0x7000000D`, `0x7000000F`, `0x70000010`, `0x70000013` are not used in any observed `sub_441AC0` call site in nvlink v13.0.88. `0x70000006` does appear in filter bitmasks (e.g., `sub_441AC0:190` range check) as a generic "constant" placeholder that is reclassified to a specific bank type, but no section is ever created with that type as its final value. `.nv.compat` and `.nv.host` jump to the `0x70000086`--`0x70000087` block; the reason for the jump is not clear from the binary.

The constant bank type for bank N is `0x70000064 + N`. The bank number is parsed from the section name suffix by `strtol(name + 12, NULL, 10)`, so `.nv.constant0` maps to `0x70000064` and `.nv.constant17` to `0x70000075`.

### Section Flags

In addition to standard ELF flags (`SHF_WRITE`, `SHF_ALLOC`, `SHF_EXECINSTR`), CUDA uses the `SHF_MASKPROC` range:

| Flag | Value | Meaning |
|---|---|---|
| `SHF_CUDA_MERCURY` | `0x10000000` | Mercury-format section (sm100+). Bit 28 of `sh_flags`. Merge phase skips these sections and defers to FNLZR. |

## Code Sections

| Section name | sh_type | Description |
|---|---|---|
| `.text.<funcname>` | `SHT_PROGBITS` | Machine code (SASS) for a single kernel or device function. Each function gets its own `.text.<name>` section, unlike host ELF which uses a monolithic `.text`. The function name is the mangled CUDA symbol. For Mercury targets (sm100+), the FNLZR replaces the Mercury instruction stream with final SASS. |

The linker processes `.text` sections during merge by copying them into the output ELF via `elfw_copy_section` (`sub_4411F0`). Dead code elimination (`sub_44AD40`) removes `.text` sections for unreachable functions, printing `"removed un-used section %s (%d)"` for each.

## Info and Metadata Sections

These sections carry structured metadata about kernels and the compilation unit.

| Section name | sh_type | Description |
|---|---|---|
| `.nv.info` | `SHT_CUDA_INFO` | Global (non-per-kernel) CUDA metadata. Contains EIATTR attribute records that apply to the entire module -- CUDA API version requirements, compatibility flags, etc. |
| `.nv.info.<funcname>` | `SHT_CUDA_INFO` | Per-kernel metadata. Each entry function and device function can have an associated `.nv.info.<name>` section carrying attributes like register count (`EIATTR_REGCOUNT`), stack sizes (`EIATTR_MAX_STACK_SIZE`, `EIATTR_MIN_STACK_SIZE`, `EIATTR_CRS_STACK_SIZE`), barrier counts (`EIATTR_NUM_BARRIERS`), parameter info (`EIATTR_KPARAM_INFO`), and dozens more. The linker reads and propagates these during `compute_entry_properties` (`sub_451D80`). |
| `.nv.metadata` | `SHT_CUDA_METADATA` | Module-level metadata. Carries the `__nv_module_id` string that identifies the compilation unit for CUDA registration. Extracted by `register_module_for_linking` (`sub_42A680`). |
| `.nv.callgraph` | `SHT_CUDA_CALLGRAPH` | Call edge table. Records caller-callee relationships between device functions. Built by `build_callgraph_section` (`sub_44D200`). The linker uses this for dead code elimination and for propagating register counts and stack sizes from callees to caller entry points. |
| `.nv.prototype` | `SHT_CUDA_PROTOTYPE` | Kernel launch prototype information. Describes the parameter layout and launch configuration for each `__global__` function. |
| `.nv.compat` | `SHT_CUDA_COMPAT` | Compatibility attribute table. Contains forward/backward compatibility flags that the driver checks at load time. The linker validates these with string `"unknown .nv.compat attribute (%x) encoutered"` (sic -- the typo is in the binary). |
| `.nv.rel.action` | `SHT_CUDA_RELOCINFO` | CUDA relocation action table. Encodes complex multi-step relocation recipes that go beyond what standard ELF `Rela` records can express. Used for bindless texture/surface offset patching and other GPU-specific relocation patterns. |
| `.nv.resolvedrela` | `SHT_CUDA_RESOLVED_RELA` | Resolved relocations preserved for the driver. When `--preserve-relocs` is specified, the linker emits this section containing relocations that have been partially or fully resolved but must be visible to the CUDA runtime for driver-side patching. Created by `emit_resolved_relocations` (`sub_46ADC0`). |

### .nv.info Attribute Format

Each attribute record in a `.nv.info` section is encoded as a `(format, attribute_id, size)` triple followed by the payload. The linker logs these as:

```
nvinfo <fmt=%d,attr=%d,size=%d>, secidx=%d
```

The `fmt` field encodes the payload format (EIFMT). The `attr` field is one of the `EIATTR_*` constants. nvlink v13.0.88 recognizes 90+ distinct EIATTR constants -- see the [NVIDIA Info Attributes](nv-info.md) page for the complete catalog.

## Memory Space Sections

These sections represent the four GPU memory address spaces: global, local, shared, and constant.

### Global Memory

| Section name | sh_type | Description |
|---|---|---|
| `.nv.global` | `SHT_CUDA_GLOBAL` (`0x70000007`) | Uninitialized global device memory. BSS-equivalent for `__device__` variables without initializers. Section type is `SHT_NOBITS` in input, reclassified to `SHT_CUDA_GLOBAL` by the linker. Multiple definitions of the same global are merged by `merge_overlapping_global_data` (`sub_432B10`), which validates byte-for-byte identity of overlapping regions. |
| `.nv.global.init` | `SHT_CUDA_GLOBAL_INIT` (`0x70000008`) | Initialized global device memory. Contains initial values for `__device__` variables with initializers. Carries `SHT_PROGBITS` data. |
| `.nv.host` | `SHT_CUDA_HOST` (`0x70000087`) | Host-visible data section. Used for data that must be accessible from both host and device code paths. Created by `sub_435B60:110` with type `1879048327` -- the section gets its own NVIDIA-specific `sh_type` rather than plain `SHT_PROGBITS`. Merged by `merge_overlapping_host_data` (`sub_435B60`). |

### Local Memory (Per-Thread)

| Section name | sh_type | Description |
|---|---|---|
| `.nv.local.<funcname>` | `SHT_CUDA_LOCAL` (`0x70000009`) | Per-kernel local memory. Thread-private storage for register spills and local arrays. Each kernel function gets its own `.nv.local.<name>` section. Dead code elimination removes these when the parent function is unreachable. Input type is `SHT_NOBITS`, reclassified to `SHT_CUDA_LOCAL`. Merged by `merge_overlapping_local_data` (`sub_437E20`). |

### Shared Memory (Per-CTA)

| Section name | sh_type | Description |
|---|---|---|
| `.nv.shared.<funcname>` | `SHT_CUDA_SHARED` (`0x7000000A`) | Per-kernel shared memory. Cooperative storage shared among threads in a CTA (thread block). Each kernel's `__shared__` variables live in a separate section. The layout engine (`sub_439830`) performs overlap analysis via `shared_memory_optimizer` (`sub_436BD0`) to pack non-overlapping shared variables. Input type is `SHT_NOBITS`. |
| `.nv_debug.shared` | `SHT_NOBITS` | Debug-only shared memory. Present only in debug builds (`-g`). Provides additional shared memory for debug instrumentation. |

### Reserved Shared Memory

The compiler reserves fixed shared memory regions for hardware features (tensor core guards, memory barriers, TMEM allocation). These sections use the prefix `.nv.reservedSmem` and type `SHT_CUDA_SHARED_RESERVED` (`0x70000015`).

| Section name | sh_type | Description |
|---|---|---|
| `.nv.reservedSmem` | `0x70000015` | Base reserved shared memory section. |
| `.nv.reservedSmem.begin` | `0x70000015` | Start address marker for reserved region. |
| `.nv.reservedSmem.end` | `0x70000015` | End address marker for reserved region. |
| `.nv.reservedSmem.cap` | `0x70000015` | Capacity limit of reserved region. |
| `.nv.reservedSmem.offset0` | `0x70000015` | First reserved offset slot. |
| `.nv.reservedSmem.offset1` | `0x70000015` | Second reserved offset slot. |

Associated symbols expose the reserved shared memory allocations to device code:

| Symbol | Description |
|---|---|
| `__nv_reservedSMEM_allocation_mask` | Bitmask controlling which reservation slots are active |
| `__nv_reservedSMEM_allocation_phase` | Phase counter for multi-phase allocation |
| `__nv_reservedSMEM_offset_0_alias` | Alias for offset slot 0 |
| `__nv_reservedSMEM_tcgen05_partition` | Tensor Core Gen05 partition offset (sm100+) |
| `__nv_reservedSMEM_tmem_allocation_pipeline_mbarrier` | TMEM allocation pipeline memory barrier |
| `__nv_reservedSMEM_tmem_allocation_pipeline_mbarrier_parity` | TMEM barrier parity toggle |
| `__nv_reservedSMEM_gb10b_war_var` | Blackwell workaround variable (sm100) |

The `--disable-smem-reservation` flag prevents the linker from generating reserved shared memory sections. The `--enable-extended-smem` flag extends the shared memory layout to support larger reservations.

## Constant Memory Sections

CUDA provides 18 constant memory banks (0--17), each mapped to a hardware constant cache slot accessible via the `LDC` (load constant) instruction.

### Numbered Banks

| Section name | sh_type | Bank | Description |
|---|---|---|---|
| `.nv.constant0` | `0x70000064` | 0 | Primary constant bank. Holds kernel parameters (`__constant__` variables) and driver-injected constants. This is the only bank guaranteed to be present. |
| `.nv.constant1` | `0x70000065` | 1 | General-purpose constant bank. |
| `.nv.constant2` | `0x70000066` | 2 | — |
| `.nv.constant3` | `0x70000067` | 3 | — |
| `.nv.constant4` | `0x70000068` | 4 | — |
| `.nv.constant5` | `0x70000069` | 5 | — |
| `.nv.constant6` | `0x7000006A` | 6 | — |
| `.nv.constant7` | `0x7000006B` | 7 | — |
| `.nv.constant8` | `0x7000006C` | 8 | — |
| `.nv.constant9` | `0x7000006D` | 9 | — |
| `.nv.constant10` | `0x7000006E` | 10 | — |
| `.nv.constant11` | `0x7000006F` | 11 | — |
| `.nv.constant12` | `0x70000070` | 12 | — |
| `.nv.constant13` | `0x70000071` | 13 | — |
| `.nv.constant14` | `0x70000072` | 14 | — |
| `.nv.constant15` | `0x70000073` | 15 | — |
| `.nv.constant16` | `0x70000074` | 16 | — |
| `.nv.constant17` | `0x70000075` | 17 | Highest constant bank. |

Constant banks are per-entry: the naming convention `<bank>.<funcname>` (e.g., `.nv.constant0.my_kernel`) creates entry-specific constant sections. The merge primitive `merge_constant_bank_data` (`sub_438640`) handles these, using the pattern `sprintf("%s.%s", bank_type_name, entry_name)`. It validates with the assertion `"bank SHT not CUDA_CONSTANT_?"`.

The constant deduplication pass `optimize_constant_dedup` (`sub_4339A0`) finds identical constant values across sections and aliases them, logging `"found duplicate value 0x%x, alias %s to %s"`.

### Named Constant Sections

Several constant banks have named aliases for specific purposes:

| Section name | Description |
|---|---|
| `.nv.constant.entry_params` | Kernel launch parameters. The driver writes actual argument values here before each kernel launch. Maps to constant bank 0. |
| `.nv.constant.driver` | Driver-injected constants. Contains values the driver sets at load time (grid dimensions, thread counts, etc.). |
| `.nv.constant.optimizer` | Compiler optimizer constants. Holds values generated by the optimizer (`__ocg_const`). Can be disabled with `--Xptxas --disable-optimizer-constants`. Overflow produces: `"Entry function '%s' uses too much data for compiler-generated constants"`. |
| `.nv.constant.user` | User-defined `__constant__` variables. The default bank for explicit constant memory declarations. |
| `.nv.constant.pic` | Position-independent code constants. Contains PIC trampoline data. Generated when PIC mode is active (`IsPIC`). |
| `.nv.constant.tools_data` | Profiling/debugging tool constants. Data injected by NVIDIA development tools (Nsight Compute, etc.). |
| `.nv.constant.entry_image_header_indices` | Image header index table. Maps entry points to their positions in the cubin image header array. |
| `.nv.ptx.const0.size` | Not a section per se but a metadata key encoding the size of constant bank 0 for the PTX compilation unit. Referenced during merge to validate constant bank sizing. |

## Unified Table Sections (UFT/UDT/UIDX)

The Unified Function Table (UFT) and Unified Descriptor Table (UDT) enable indirect calls and texture/surface access across compilation units. The UIDX (Unified Index) file is an external index that maps UUIDs to table slots.

| Section name | sh_type | Description |
|---|---|---|
| `.nv.uft` | `SHT_PROGBITS` | Unified Function Table. Jump slot array for cross-module indirect function calls. Each slot holds a branch instruction targeting the resolved function. The linker validates: `"Number of .nv.uft jump slots != Number of entries in .nv.uft.entry"`. |
| `.nv.uft.entry` | `SHT_PROGBITS` | UFT entry metadata. Maps each UFT slot to its UUID pair and target symbol. Entries are 128-bit UUID pairs logged as `"uft uuid = <%016llx,%016llx>, offset = %llx"`. |
| `.nv.uft.rel` | `SHT_RELA` | UFT relocation table. Relocations for patching UFT jump slots with final function addresses. |
| `.nv.udt` | `SHT_PROGBITS` | Unified Descriptor Table. Descriptor array for cross-module texture and surface access. Each entry holds a resource descriptor. Aligned with: `"udt size %lld needs aligning"`. |
| `.nv.udt.entry` | `SHT_PROGBITS` | UDT entry metadata. Maps each UDT slot to its UUID pair and target symbol, parallel to `.nv.uft.entry`. |
| `.nv.uidx` | `SHT_PROGBITS` | Unified index table. Loaded from an external file specified by `--uidx-file`. Contains the pre-computed UUID-to-slot mapping. Validated with `"malformed uidx input"`, `"size of uidx window != nv.uft"`, `"size of uidx window != nv.udt"`. |

The UFT/UDT management functions (`sub_4637B0`, `sub_463F70`) reorder entries and resolve UUID-based lookups. The linker generates stub functions for unified calls using the template:

```
.func .attribute(.unified_func_stub)  __cuda_uf_stub_<name>( ) { ... }
```

Unified table relocations use dedicated relocation types: `R_CUDA_UNIFIED`, `R_CUDA_UNIFIED_32`, `R_CUDA_UNIFIED_8_0` through `R_CUDA_UNIFIED_8_56`, and the Mercury equivalents `R_MERCURY_UNIFIED*`. The synthetic symbols `__UFT_OFFSET`, `__UDT_OFFSET`, `__UFT_CANONICAL`, `__UDT_CANONICAL`, `__UFT_END`, and `__UDT_END` mark the table boundaries in the final ELF.

## Note Sections

Standard ELF `SHT_NOTE` sections carry CUDA compilation metadata consumed by the driver and runtime.

| Section name | sh_type | Description |
|---|---|---|
| `.note.nv.cuinfo` | `SHT_NOTE` | CUDA compilation info. Contains key-value pairs describing the compilation (target architecture, CUDA version, compiler options that affect ABI). Created during `elfw_create` (`sub_4438F0`). |
| `.note.nv.cuver` | `SHT_NOTE` | CUDA version stamp. Records the CUDA toolkit version used to compile the cubin. |
| `.note.nv.tkinfo` | `SHT_NOTE` | Toolkit info. Extended toolkit metadata. Controllable with `--verbose-tkinfo`. |

All three are created at ELF wrapper initialization time and are always present in the output cubin.

## Texture and Surface Reference Sections

These carry descriptor size information for the texture/surface hardware units.

| Section name | Description |
|---|---|
| `.nv.unified.texrefDescSize` | Descriptor size for unified-mode texture references. Unified texture mode combines texture and sampler into a single descriptor. |
| `.nv.independent.texrefDescSize` | Descriptor size for independent-mode texture references. Independent mode uses separate texture and sampler objects. |
| `.nv.independent.samplerrefDescSize` | Descriptor size for independent-mode sampler references. |
| `.nv.surfrefDescSize` | Descriptor size for surface references. |

The texture mode affects how `tex` and `suld`/`sust` instructions are lowered. The linker checks `"unexpected usage of non-unified surface descriptors"` when modes are mixed.

## Fatbin Sections (Host ELF)

These sections appear in the **host** ELF (not the device cubin) and contain embedded device code for lazy JIT compilation.

| Section name | Description |
|---|---|
| `.nvFatBinSegment` | Primary fat binary segment. Contains the `__cudaFatBinaryData` structure with embedded cubins for all target architectures. |
| `__nv_relfatbin` | Relocatable fat binary section. Contains position-dependent references into the fat binary that need host-side relocation. |
| `.nv_fatbin` | Fat binary data section. Raw fat binary payload referenced by `.nvFatBinSegment`. |

The linker generates a host linker script to ensure these sections are placed correctly:

```
SECTIONS
{
    .nvFatBinSegment : { *(.nvFatBinSegment) }
    __nv_relfatbin : { *(__nv_relfatbin) }
    .nv_fatbin : { *(.nv_fatbin) }
}
```

## Debug Sections

### Standard NVIDIA Debug Sections

| Section name | Description |
|---|---|
| `.nv_debug_info_ptx` | Embedded PTX source text for source-level debugging. |
| `.nv_debug_info_reg_sass` | Per-instruction register liveness at the SASS level. Used by cuda-gdb for variable inspection at arbitrary breakpoints. |
| `.nv_debug_info_reg_type` | Register type annotations associating data types with physical registers. |
| `.nv_debug.shared` | Debug-mode shared memory. Extra shared memory reserved for debug instrumentation when `-g` is active. |

### Standard DWARF Debug Sections

Cubins include standard DWARF sections (`.debug_abbrev`, `.debug_info`, `.debug_line`, `.debug_str`, etc.) with CUDA extensions. The linker validates their presence with `"skipping .debug_info section due to missing .debug_abbrev section"` and processes them during the merge phase. NVIDIA adds custom DWARF attributes such as `DW_AT_NV_general_flags`.

### Mercury Debug Sections

Mercury targets (sm100+) wrap their debug data in the `.nv.merc.*` namespace. These 19 sections (11 standard DWARF mirrors + 4 NVIDIA-specific + structural) are documented in full on the [Mercury ELF Sections](../mercury/elf-sections.md) page. The key difference is that Mercury debug sections carry the `0x10000000` flag in `sh_flags`, causing the merge phase to skip them and defer processing to FNLZR.

## Standard ELF Infrastructure Sections

Every cubin also contains standard ELF sections used by the linker infrastructure:

| Section name | sh_type | Description |
|---|---|---|
| `.symtab` | `SHT_SYMTAB` | Symbol table. Contains both CUDA-specific and standard ELF symbols. |
| `.strtab` | `SHT_STRTAB` | String table for symbol names. |
| `.shstrtab` | `SHT_STRTAB` | Section header string table. Contains all section name strings. |
| `.rela.<secname>` | `SHT_RELA` | Standard ELF relocation sections. The linker generates these via `elfw_add_reloc_section` (`sub_441AC0`) using the pattern `.rela%s` from the parent section name. |

## Section Name Dispatch in merge_elf

The `merge_elf` function (`sub_45E7D0`, 89,156 bytes) is the central section classifier. It uses `strncmp`-based prefix matching on section names to route each input section to the correct merge handler:

| Prefix match | Handler | Memory space |
|---|---|---|
| `.nv.global` (exact) | `merge_overlapping_global_data` | Global BSS |
| `.nv.global.init` | `merge_overlapping_data_variant` | Global initialized |
| `.nv.local.` | `merge_overlapping_local_data` | Per-thread local |
| `.nv.shared.` | overlap analysis + layout | Per-CTA shared |
| `.nv.constant` | `merge_constant_bank_data` | Constant banks |
| `.nv.info` | nvinfo parser | Metadata |
| `.nv.compat` | compatibility check | Compatibility |
| `.nv.host` | `merge_overlapping_host_data` | Host-visible |
| `.nv.merc.` | skip (deferred to FNLZR) | Mercury code/debug |
| `.text.` | `elfw_copy_section` | Code |

All five overlap merge functions (`sub_432B10`, `sub_437E20`, `sub_4343C0`, `sub_434BC0`, `sub_435390`) share the same validation logic: they compare overlapping regions byte-for-byte and fatal on mismatch with `"overlapping non-identical data"`.

## Section Lifecycle

A section progresses through the following stages in the nvlink pipeline:

1. **Parse**: Input ELF sections are read by the ELF parser, producing section records with raw `sh_type` values.

2. **Classify**: `merge_elf` reclassifies sections based on name prefix, assigning CUDA-specific `sh_type` values.

3. **Create/Find**: The output section is found by name hash lookup or created by `section_create` (`sub_441AC0`), which allocates a 104-byte section record and registers it in both hash tables.

4. **Accumulate**: `section_data_copy` (`sub_433760`) appends data contributions to a linked list. No final layout yet -- just a chain of (source_ptr, size, alignment) nodes.

5. **Layout**: The layout engine (`sub_439830`) calls `section_layout_engine` (`sub_4325A0`) to sort symbols by alignment and assign offsets within each section.

6. **Relocate**: `apply_relocations` (`sub_469D60`) resolves all relocations against the final section addresses.

7. **Finalize**: `finalize_elf` (`sub_445000`) applies final patches, generates `.nv.resolvedrela` if needed, and prepares the output.

8. **Emit**: `write_elf_to_buffer` (`sub_45BF00`) serializes all sections into the output ELF, validating sizes with `"section size mismatch"`.

## Quick Reference: All Section Names

Complete alphabetical list of every NVIDIA-specific section name found in nvlink v13.0.88:

```
.note.nv.cuinfo                         SHT_NOTE           Compilation info
.note.nv.cuver                          SHT_NOTE           CUDA version
.note.nv.tkinfo                         SHT_NOTE           Toolkit info
.nv.callgraph                           0x70000001         Call edge table
.nv.compat                              0x70000086         Compatibility attributes
.nv.constant0 .. .nv.constant17         0x70000064..75     Constant banks 0-17
.nv.constant.driver                     (bank alias)       Driver constants
.nv.constant.entry_image_header_indices (bank alias)       Image header indices
.nv.constant.entry_params               (bank alias)       Kernel parameters
.nv.constant.optimizer                  (bank alias)       Compiler-generated constants
.nv.constant.pic                        (bank alias)       PIC trampoline data
.nv.constant.tools_data                 (bank alias)       Tool-injected constants
.nv.constant.user                       (bank alias)       User __constant__ variables
.nv.global                              0x70000007         Global BSS
.nv.global.init                         0x70000008         Global initialized data
.nv.host                                0x70000087         Host-visible data
.nv.independent.samplerrefDescSize      SHT_PROGBITS       Sampler descriptor size
.nv.independent.texrefDescSize          SHT_PROGBITS       Texture descriptor size (indep)
.nv.info                                0x70000000         Global nvinfo attributes
.nv.info.<funcname>                     0x70000000         Per-kernel nvinfo attributes
.nv.local.<funcname>                    0x70000009         Per-kernel local memory
.nv.metadata                            0x70000004         Module metadata
.nv.merc.*                              (varies)           Mercury sections (19 total)
.nv.prototype                           0x70000002         Launch prototypes
.nv.ptx.const0.size                     (metadata)         Constant bank 0 size record
.nv.rel.action                          0x7000000B         Relocation action table
.nv.reservedSmem                        0x70000015         Reserved shared memory base
.nv.reservedSmem.begin                  0x70000015         Reserved region start
.nv.reservedSmem.cap                    0x70000015         Reserved region capacity
.nv.reservedSmem.end                    0x70000015         Reserved region end
.nv.reservedSmem.offset0                0x70000015         Reserved offset slot 0
.nv.reservedSmem.offset1                0x70000015         Reserved offset slot 1
.nv.resolvedrela                        0x70000003         Preserved relocations
.nv.shared.<funcname>                   0x7000000A         Per-kernel shared memory
.nv.surfrefDescSize                     SHT_PROGBITS       Surface descriptor size
.nv.udt                                 0x70000012         Unified Descriptor Table
.nv.udt.entry                           0x70000014         UDT entry metadata
.nv.uft                                 0x7000000E         Unified Function Table
.nv.uft.entry                           0x70000011         UFT entry metadata
.nv.uft.rel.<funcname>                  0x7000000E         Per-kernel UFT relocation slot (same sh_type as .nv.uft)
.nv.uidx                                SHT_PROGBITS       Unified index table
.nv.unified.texrefDescSize              SHT_PROGBITS       Texture descriptor size (unified)
.nv_debug_info_ptx                      SHT_PROGBITS       Embedded PTX source
.nv_debug_info_reg_sass                 SHT_PROGBITS       SASS register liveness
.nv_debug_info_reg_type                 SHT_PROGBITS       Register type annotations
.nv_debug.shared                        SHT_NOBITS         Debug shared memory
.nv_fatbin                              SHT_PROGBITS       Fat binary data (host ELF)
.nvFatBinSegment                        SHT_PROGBITS       Fat binary segment (host ELF)
__nv_relfatbin                          SHT_PROGBITS       Relocatable fatbin (host ELF)
.text.<funcname>                        SHT_PROGBITS       Kernel/function machine code
```

## Cross-References

**Internal (nvlink wiki):**

- [Section Catalog](../reference/section-catalog.md) -- Alphabetical reference catalog of all 109 section entries with `sh_type` hex values
- [.nv.info Metadata](nv-info.md) -- EIATTR attribute format and the 90+ attribute constants carried in `.nv.info` / `.nv.info.<funcname>` sections
- [Constant Banks](constant-banks.md) -- Deep dive on `.nv.constant*` section numbering, dedup, and hardware size limits
- [Unified Function Tables](uft.md) -- UFT/UDT section management (`.nv.uft`, `.nv.udt`, `.nv.uidx`)
- [Mercury ELF Sections](../mercury/elf-sections.md) -- The 19 `.nv.merc.*` sections for Mercury targets (sm100+)
- [Section Merging](../linker/section-merging.md) -- `merge_elf` name-prefix dispatch table that classifies input sections
- [Dead Code Elimination](../linker/dead-code-elimination.md) -- How `.text.*` and associated `.nv.info.*` / `.nv.local.*` sections are removed
- [Device ELF Format](device-elf-format.md) -- ELF header encoding and how `e_type` / `e_flags` relate to section emission
- [Linker Scripts](../infra/linker-scripts.md) -- Host-side ELF sections (`.nvFatBinSegment`, `__nv_relfatbin`, `.nv_fatbin`) and the `SECTIONS` template
- [Program Headers](program-headers.md) -- How sections are classified into PT\_LOAD segments via the internal flags bitmask

**Sibling wikis:**

- [ptxas: Sections](../../ptxas/output/sections.html -- Section creation in ptxas: how `.text`, `.nv.info`, `.nv.constant*`, and debug sections are emitted
- [ptxas: EIATTR Reference](../../ptxas/reference/eiattr.html) -- EIATTR attribute codes that populate `.nv.info` sections
- [ptxas: Debug Info](../../ptxas/output/debug-info.html) -- How ptxas generates the NVIDIA debug sections (`.nv_debug_*`)

## Confidence Assessment

| Claim | Confidence | Evidence |
|-------|-----------|----------|
| merge_elf at sub_45E7D0 (89,156 bytes) | HIGH | Decompiled file sub_45E7D0_0x45e7d0.c exists |
| "removed un-used section %s (%d)" string | HIGH | String at 0x1D3AB28 confirmed in nvlink_strings.json, xref to sub_44AD40 |
| "overlapping non-identical data" string | HIGH | String at 0x1D387D8 confirmed in nvlink_strings.json, xref to sub_432B10 |
| "unknown .nv.compat attribute (%x) encoutered" (typo) | HIGH | String at 0x1D3B1B8 confirmed in nvlink_strings.json (typo "encoutered" preserved) |
| Dead code elimination sub_44AD40 | HIGH | Decompiled file exists |
| Section copy sub_4411F0 | HIGH | Decompiled file exists |
| Reloc section creator sub_441AC0 | HIGH | Decompiled file exists |
| merge_overlapping_global_data sub_432B10 | HIGH | Decompiled file exists, "overlapping non-identical data" xref confirmed |
| merge_overlapping_local_data sub_437E20 | HIGH | Decompiled file exists |
| Overlap merge functions sub_4343C0, sub_434BC0, sub_435390 | HIGH | All three decompiled files exist |
| Layout engine sub_439830 | HIGH | Decompiled file exists |
| Resolved relocations sub_46ADC0 | HIGH | Decompiled file exists |
| register_module_for_linking sub_42A680 | HIGH | Decompiled file exists |
| build_callgraph_section sub_44D200 | HIGH | Decompiled file exists |
| .nv.constant bank type formula 0x70000064 + N | HIGH | Verified in sub_438640: `a9 - 1879048292` = `a9 - 0x70000064`; "bank SHT not CUDA_CONSTANT_?" string confirmed |
| 18 constant banks (0-17) | HIGH | String table at 0x1D3A8E0 confirmed with 18 entries in nvlink_strings.json |
| .note.nv.tkinfo, .note.nv.cuinfo names | HIGH | Strings at 0x1D391CB and 0x1D391AC confirmed in nvlink_strings.json |
| "nvinfo <fmt=%d,attr=%d,size=%d>" log format | HIGH | String at 0x1D3A420 confirmed in nvlink_strings.json |
| SHT_CUDA_INFO section type code = 0x70000000 | HIGH | Confirmed in decompiled `sub_45E7D0`: `.nv.info` sections dispatch via `case 0x70000000` (implied by absence of explicit case, handled by default INFO path); 0x70000001 is `SHT_CUDA_CALLGRAPH` not INFO |
| SHF_CUDA_MERCURY = 0x10000000 (bit 28) | MEDIUM | Referenced in Mercury section handling; not individually verified in decompiled bitmask |
| Section lifecycle 8-stage pipeline | MEDIUM | Stages match observed function call order but reconstructed from multiple function call chains |
| Fatbin section names (.nvFatBinSegment etc) | MEDIUM | Referenced in host-side linker path; not primary nvlink device-link path |
| Reserved shared memory symbol names | MEDIUM | Inferred from string table patterns; not all individually traced to specific code paths |
