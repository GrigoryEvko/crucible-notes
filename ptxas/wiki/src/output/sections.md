# Section Catalog & EIATTR

A CUDA cubin is a standard ELF container with NVIDIA-proprietary extensions. ptxas v13.0.88 populates it with approximately 4*(N+M) sections minimum for a program with N entry kernels and M device functions. Each section carries a specific kind of data -- SASS instructions, constant bank contents, relocation entries, per-kernel resource metadata (EIATTR), shared memory layout, debug information, or Mercury-encoded streams for deferred finalization. This page catalogs every section type ptxas can emit, the NVIDIA-specific ELF section types used, the section ordering rules, and the complete EIATTR attribute encoding.

| | |
|---|---|
| **Section attribute builder** | `sub_60FBF0` (76 KB decompiled -- per-kernel section config + codegen launch) |
| **Section creator** | `sub_1CB3570` (1,963 bytes, 44 call sites) |
| **Text section creator** | `sub_1CB42D0` (SHF_ALLOC \| SHF_EXECINSTR) |
| **nvinfo section creator** | `sub_1CC7FB0` (creates `.nv.info` / `.nv.info.<func>`) |
| **EIATTR record emitter** | `sub_1CC85F0` (emits one TLV record) |
| **EIATTR builder** | `sub_1CC9800` (14,764 bytes, 90 KB decompiled, 2,786 lines) |
| **EIATTR propagator** | `sub_1CC8950` (2,634 bytes -- barrier/register propagation) |
| **.nv.compat handler** | `sub_1CC93A0` (`.nv.compat` attribute processor) |
| **Call graph builder** | `sub_1CBE1B0` (`.nv.callgraph` section) |
| **Layout calculator** | `sub_1C9DC60` (5,663 bytes -- offset assignment) |
| **Master section allocator** | `sub_1CABD60` (11,856 bytes -- shared/constant/local addresses) |
| **SHT_CUDA_INFO** | `0x70000000` (1,879,048,192) |
| **SHT_CUDA_CALLGRAPH** | `0x70000064` (1,879,048,292) |
| **.nv.compat section type** | `0x70000086` (1,879,048,326) |

## NVIDIA-Specific Section Types

Beyond standard ELF section types (`SHT_PROGBITS`, `SHT_STRTAB`, `SHT_SYMTAB`, `SHT_RELA`, `SHT_NOTE`), ptxas uses NVIDIA-defined types in the `SHT_LOPROC`--`SHT_HIPROC` range (`0x70000000`--`0x7FFFFFFF`):

| Constant | Value | Decimal | Used by |
|---|---|---|---|
| `SHT_CUDA_INFO` | `0x70000000` | 1,879,048,192 | `.nv.info`, `.nv.info.<func>` |
| `SHT_CUDA_CALLGRAPH` | `0x70000064` | 1,879,048,292 | `.nv.callgraph` |
| `SHT_CUDA_COMPAT` | `0x70000086` | 1,879,048,326 | `.nv.compat` |

The section creator `sub_1CB3570` contains a range check on CUDA-specific types:

```c
// sub_1CB3570 -- section type validation
if (elf_mode != 1 && is_relocatable
    && ((sh_type - 0x70000064) <= 0x1A || sh_type == 0x70000006)) {
    // These CUDA section types require special handling in relocatable mode
}
```

This tells us that NVIDIA reserves the range `0x70000064`--`0x7000007E` (27 types) plus `0x70000006` for CUDA-specific sections that receive special treatment in relocatable object mode.

## Complete Section Catalog

### Standard ELF Infrastructure Sections

Created unconditionally by the ELFW constructor (`sub_1CB53A0`). These form the skeleton of every cubin.

| Section | Type | Flags | Purpose |
|---|---|---|---|
| (null) | `SHT_NULL` | -- | Required ELF null section (index 0) |
| `.shstrtab` | `SHT_STRTAB` | -- | Section name string table |
| `.strtab` | `SHT_STRTAB` | -- | Symbol name string table |
| `.symtab` | `SHT_SYMTAB` | -- | Symbol table |
| `.symtab_shndx` | `SHT_SYMTAB_SHNDX` | -- | Extended section indices (when section count > 65,280) |

### NVIDIA Note Sections

Created unconditionally. Carry module-level metadata the CUDA driver reads before launching any kernel.

| Section | Type | Flags | Purpose |
|---|---|---|---|
| `.note.nv.tkinfo` | `SHT_NOTE` | -- | Toolkit info: version string, build ID, CLI arguments |
| `.note.nv.cuinfo` | `SHT_NOTE` | -- | CUDA info: SM version, feature flags |
| `.note.nv.cuver` | `SHT_NOTE` | -- | CUDA version note |

### Per-Kernel Code Sections

Created by `sub_1CB42D0`, one set per kernel entry and device function:

| Section | Type | Flags | sh_link | Purpose |
|---|---|---|---|---|
| `.text.<func>` | `SHT_PROGBITS` | `SHF_ALLOC \| SHF_EXECINSTR` (0x6) | -- | SASS instruction bytes |
| `.rela.text.<func>` | `SHT_RELA` | -- | `.symtab` index | Relocations for the code section |

The `.rela` companion is auto-created by the section creator when `SHF_EXECINSTR` is set. The assertion `"adding function section after callgraph completed"` fires if a code section is added after call graph analysis.

### Per-Kernel Metadata Sections

| Section | Type | Flags | sh_link | Purpose |
|---|---|---|---|---|
| `.nv.info.<func>` | `SHT_CUDA_INFO` | `SHF_LINK_ORDER` (0x40) | `.text.<func>` symbol | EIATTR TLV records for this kernel |
| `.nv.constant0.<func>` | `SHT_PROGBITS` | `SHF_ALLOC` | -- | Constant bank 0: kernel params + literal constants |
| `.nv.shared.<func>` | `SHT_NOBITS` | `SHF_ALLOC \| SHF_WRITE` | -- | Shared memory layout (size only, no file data) |
| `.nv.local.<func>` | `SHT_NOBITS` | `SHF_ALLOC \| SHF_WRITE` | -- | Local (spill) memory layout |

The `.nv.info.<func>` section uses `SHF_LINK_ORDER` (flag `0x40`) to declare its association with the function's symbol. The `SHT_CUDA_INFO` type value `0x70000000` is used; note that the nvlink wiki previously documented `0x70000064` for this -- the discrepancy arises because nvlink uses a different constant in its own emitter. Binary evidence from ptxas shows `sub_1CC7FB0` consistently passes `1879048192` (`0x70000000`).

### Global Metadata Sections

| Section | Type | Flags | Purpose |
|---|---|---|---|
| `.nv.info` | `SHT_CUDA_INFO` | -- | Global EIATTR attributes (sh_link = 0, not per-function) |
| `.nv.compat` | `SHT_CUDA_COMPAT` | -- | Forward-compatibility attributes (sm version negotiation) |
| `.nv.metadata` | `SHT_PROGBITS` | -- | Module-level metadata |
| `.nv.callgraph` | `SHT_CUDA_CALLGRAPH` | -- | Inter-function call edges (relocatable mode, `-c`) |
| `.nv.prototype` | `SHT_PROGBITS` | -- | Prototype information for cross-module linking |
| `.nv.rel.action` | `SHT_PROGBITS` | -- | Relocation action table |
| `.nv.resolvedrela` | `SHT_PROGBITS` | -- | Resolved relocations (post-linking) |
| `.nv.host` | `SHT_PROGBITS` | -- | Host-side interop data |

### Constant Banks

CUDA supports up to 18 numbered constant banks (0--17) plus named constant sections:

| Section | Purpose |
|---|---|
| `.nv.constant0` | Merged constant bank 0 (whole-program mode) |
| `.nv.constant0.<func>` | Per-function constant bank 0 (kernel params + compiler constants) |
| `.nv.constant1` -- `.nv.constant17` | User-declared `__constant__` variables |
| `.nv.constant.entry_params` | Entry point parameter block |
| `.nv.constant.entry_image_header_indices` | Texture/surface header index table |
| `.nv.constant.driver` | Driver-injected constants |
| `.nv.constant.optimizer` | Optimizer-generated constants (OCG) |
| `.nv.constant.user` | User-specified constants |
| `.nv.constant.pic` | Position-independent code constants |
| `.nv.constant.tools_data` | Tools/debugger-injected data |

The layout calculator `sub_1C9DC60` skips `.nv.constant0` sections during offset assignment because their addresses are managed by the OCG constant bank allocator, not the ELF layout engine.

### Shared Memory Sections

| Section | Purpose |
|---|---|
| `.nv.shared.<func>` | Per-kernel shared memory (size declaration, no data) |
| `.nv.shared.reserved.` | Reserved shared memory for runtime allocation |
| `.nv.reservedSmem` | Reserved shared memory master section |
| `.nv.reservedSmem.begin` | Start offset of reserved region |
| `.nv.reservedSmem.cap` | Capacity of reserved region |
| `.nv.reservedSmem.offset0` | Offset within reserved region 0 |
| `.nv.global.init` | Initialized global variables |

The master section allocator `sub_1CABD60` assigns addresses to shared, constant, and local memory sections. The layout calculator skips `.nv.reservedSmem` for the same reason it skips `.nv.constant0` -- its address comes from the shared memory master allocator.

### Unified Function/Data Tables

| Section | Purpose |
|---|---|
| `.nv.uft` | Unified Function Table (indirect call dispatch) |
| `.nv.uft.entry` | UFT entry point table |
| `.nv.udt` | Unified Data Table |
| `.nv.udt.entry` | UDT entry point table |

The error `"Number of .nv.uft jump slots != Number of entries"` fires when the UFT and entry tables are inconsistent. `"missing nv.uft.entry"` fires when the required entry table section was never created.

### DWARF Debug Sections

Generated when `--device-debug` or `--generate-line-info` is active:

| Section | Content |
|---|---|
| `.debug_info` | DWARF DIE tree (compilation units, types, variables) |
| `.debug_abbrev` | DWARF abbreviation table |
| `.debug_line` | Source-to-address line number mapping |
| `.debug_frame` | Call frame information for unwinding |
| `.debug_loc` | Location lists for variables |
| `.debug_str` | DWARF string table |
| `.debug_ranges` | Address ranges |
| `.debug_aranges` | Address range lookup table |
| `.debug_pubnames` | Public name index |
| `.debug_pubtypes` | Public type index |

### NVIDIA Debug Extensions

| Section | Content |
|---|---|
| `.nv_debug_ptx_txt` | Embedded PTX source text |
| `.nv_debug_line_sass` | SASS-level line number mapping |
| `.nv_debug_info_reg_sass` | Register allocation debug info |
| `.nv_debug_info_reg_type` | Register type information |
| `.nv_debug.shared` | Shared memory debug layout |

### Mercury / Capsule Mercury Sections (SM 100+)

For Capsule Mercury output (Blackwell and later), the cubin contains a parallel set of `.nv.merc.*` sections carrying Mercury-encoded instruction streams plus all metadata needed for deferred finalization:

| Section | Purpose |
|---|---|
| `.nv.capmerc` | Capsule Mercury descriptor |
| `.nv.merc.symtab_shndx` | Extended section index table (Mercury copy) |
| `.nv.merc.nv.shared.reserved` | Shared memory reservation metadata |
| `.nv.merc.rela<secname>` | Per-section relocation tables |
| `.nv.merc.debug_abbrev` | Cloned DWARF abbreviation table |
| `.nv.merc.debug_info` | Cloned DWARF info |
| `.nv.merc.debug_line` | Cloned DWARF line table |
| `.nv.merc.debug_frame` | Cloned DWARF frame info |
| `.nv.merc.debug_loc` | Cloned DWARF locations |
| `.nv.merc.debug_str` | Cloned DWARF string table |
| `.nv.merc.debug_ranges` | Cloned DWARF ranges |
| `.nv.merc.debug_aranges` | Cloned DWARF address ranges |
| `.nv.merc.debug_pubnames` | Cloned DWARF public names |
| `.nv.merc.debug_pubtypes` | Cloned DWARF public types |
| `.nv.merc.debug_macinfo` | Cloned DWARF macro info |
| `.nv.merc.nv_debug_ptx_txt` | Embedded PTX source text |
| `.nv.merc.nv_debug_line_sass` | SASS-level line mapping |
| `.nv.merc.nv_debug_info_reg_sass` | Register allocation debug info |
| `.nv.merc.nv_debug_info_reg_type` | Register type debug info |

The Mercury section cloner (`sub_1CA2E40`) iterates all sections and duplicates constant, global, shared, and local sections into the `.nv.merc.*` namespace, creating corresponding `.nv.merc.rela` sections for relocations.

## Global vs Per-Kernel Sections

The `.nv.info` / `.nv.info.<func>` split is the primary distinction between global and per-kernel metadata:

**Global** `.nv.info` (one per cubin):
- `sh_link = 0` (no associated symbol)
- Contains module-wide EIATTR records: `EIATTR_CUDA_API_VERSION`, `EIATTR_STATISTICS`, `EIATTR_HAS_PRE_V10_OBJECT`, `EIATTR_MERCURY_ISA_VERSION`
- Created by `sub_1CC7FB0(elfw, 0)` -- the zero argument selects global mode

**Per-kernel** `.nv.info.<func>` (one per kernel):
- Section name: `sprintf(".nv.info.%s", func_name)` (visible in `sub_1CC7FB0`)
- `sh_link` points to the symbol table entry for the function
- `sh_flags` includes `SHF_LINK_ORDER` (0x40) to declare its association
- Contains per-kernel EIATTR records: `EIATTR_REGCOUNT`, `EIATTR_NUM_BARRIERS`, `EIATTR_FRAME_SIZE`, etc.
- Created by `sub_1CC7FB0(elfw, sym_idx)` when `sym_idx != 0`

The `.nv.info` section creator (`sub_1CC7FB0`) first searches for an existing section of type `0x70000000` with the appropriate name. If none exists, it creates one. The per-function variant links the new section to the function's `.text` section via `sub_1CB4180`.

## Section Ordering

During finalization, sections are sorted into 8 priority buckets that determine their order in the output ELF:

| Bucket | Priority | Contents |
|---|---|---|
| 0 | Highest | ELF header pseudo-section, `.shstrtab` |
| 1 | | `.strtab`, `.symtab`, `.symtab_shndx` |
| 2 | | `.note.nv.tkinfo`, `.note.nv.cuinfo` |
| 3 | | `.text.<func>` code sections |
| 4 | | `.nv.constant0.*`, `.nv.shared.*`, `.nv.local.*` data sections |
| 5 | | `.rela.*`, `.rel.*` relocation sections |
| 6 | | `.nv.info.*` EIATTR metadata sections |
| 7 | Lowest | `.debug_*`, `.nv.merc.*` debug and Mercury metadata |

Within each bucket, sections appear in creation order. Section file offsets are assigned by `sub_1C9DC60` walking the sorted list with alignment padding. The `.debug_line` section receives special alignment padding for DWARF line table requirements.

### Offset Assignment

```c
// sub_1C9DC60 -- simplified layout algorithm
uint64_t offset = elf_header_size;
for (int i = 0; i < section_count; i++) {
    section_t* sec = sorted_sections[i];
    if (is_virtual(sec))          continue;  // flag & 4 -> no file data
    if (is_nv_constant0(sec))     continue;  // OCG allocator manages these
    if (is_nv_reservedSmem(sec))  continue;  // shared memory allocator manages these

    if (sec->sh_addralign > 1)
        offset = (offset + sec->sh_addralign - 1) & ~(sec->sh_addralign - 1);
    sec->sh_offset = offset;
    offset += sec->sh_size;
}
```

Three section types are skipped during offset assignment:
1. **Virtual sections** (flag bit 2 set) -- have no file data, only metadata
2. **`.nv.constant0`** -- address assigned by the OCG constant bank allocator
3. **`.nv.reservedSmem`** -- address assigned by the shared memory master allocator `sub_1CABD60`

## EIATTR Encoding

Each `.nv.info` section contains a flat sequence of EIATTR (Entry Information Attribute) records. There is no section header or record count -- the parser walks from byte 0 to `sh_size`, consuming records sequentially. The EIATTR builder is `sub_1CC9800` (14,764 binary bytes, 90 KB decompiled) -- one of the three largest functions in the output pipeline.

### TLV Record Format

```
Offset  Size  Field
------  ----  -----
0x00    1     format      Format byte (determines payload structure)
0x01    1     attr_code   EIATTR type code (identifies the attribute)
0x02    2     size        Payload size in bytes (little-endian uint16)
0x04    var   payload     Attribute-specific data (size bytes)
```

Total record size = `4 + ALIGN_UP(size, 4)`. Records are 4-byte aligned.

### Format Byte

| Format | Name | Payload structure |
|---|---|---|
| `0x01` | Free | Raw bytes, attribute-specific layout |
| `0x02` | Value | Single 32-bit value (no symbol index) |
| `0x03` | Sized | 16-bit value + padding |
| `0x04` | Indexed | `[sym_index:4][value:4]` -- per-symbol attribute |

Format `0x04` (indexed) is the most common for per-function attributes. The 4-byte symbol index at payload offset 0 identifies which function the attribute applies to, enabling the linker to remap symbol indices during merge.

### Parsing Pseudocode

```c
uint8_t *ptr = section_data;
uint8_t *end = section_data + section_size;

while (ptr < end) {
    uint8_t  format    = ptr[0];
    uint8_t  attr_code = ptr[1];
    uint16_t size      = *(uint16_t *)(ptr + 2);

    switch (format) {
    case 0x04:  // Indexed
        uint32_t sym_idx = *(uint32_t *)(ptr + 4);
        uint32_t value   = *(uint32_t *)(ptr + 8);
        process_indexed(attr_code, sym_idx, value);
        break;
    case 0x02:  // Value
        uint32_t value = *(uint32_t *)(ptr + 4);
        process_global(attr_code, value);
        break;
    default:    // Free / Sized
        process_raw(attr_code, ptr + 4, size);
        break;
    }
    ptr += 4 + ALIGN_UP(size, 4);
}
```

### EIATTR Record Emitter -- `sub_1CC85F0`

The low-level function that writes one EIATTR TLV record. Called from the builder and propagator with parameters:

```c
// sub_1CC85F0 -- emit one EIATTR record
void emit_eiattr(
    ELFW*    elfw,       // a1: ELFW object
    uint8_t  attr_code,  // a2: EIATTR type code (e.g., 0x2F for REGCOUNT)
    int16_t  size,       // a3: payload size in bytes
    void*    payload,    // a4: pointer to payload data
    uint32_t sym_idx     // a5: symbol index (0 = global)
);
```

Before emitting, it calls `sub_1C97840` to check whether the attribute code is supported on the current SM architecture. If not supported, the record is silently skipped. It then calls `sub_1CC7FB0` to obtain or create the appropriate `.nv.info` section, allocates a 16-byte record descriptor, fills the format byte and attribute code, and appends it to the section's linked list (offset `+392` in the ELFW object).

## EIATTR Attribute Catalog

ptxas v13.0.88 defines 97 EIATTR codes numbered 0 through 96 (plus the `EIATTR_ERROR_LAST` sentinel at 96). The complete catalog below is cross-referenced against the nvlink v13.0.88 name table (extracted from pointer table at VA `0x1D37D60`) and verified against EIATTR codes observed in the ptxas EIATTR builder (`sub_1CC9800` switch cases and `sub_1CC85F0` call sites).

### Complete Code Table

| Code | Hex | Name | Fmt | Category |
|---:|---:|---|---|---|
| 0 | `0x00` | `EIATTR_ERROR` | -- | Sentinel |
| 1 | `0x01` | `EIATTR_PAD` | -- | Sentinel |
| 2 | `0x02` | `EIATTR_IMAGE_SLOT` | Idx | Texture |
| 3 | `0x03` | `EIATTR_JUMPTABLE_RELOCS` | Free | Metadata |
| 4 | `0x04` | `EIATTR_CTAIDZ_USED` | Idx | Metadata |
| 5 | `0x05` | `EIATTR_MAX_THREADS` | Idx | Resource |
| 6 | `0x06` | `EIATTR_IMAGE_OFFSET` | Idx | Texture |
| 7 | `0x07` | `EIATTR_IMAGE_SIZE` | Idx | Texture |
| 8 | `0x08` | `EIATTR_TEXTURE_NORMALIZED` | Idx | Texture |
| 9 | `0x09` | `EIATTR_SAMPLER_INIT` | Idx | Texture |
| 10 | `0x0A` | `EIATTR_PARAM_CBANK` | Idx | Param |
| 11 | `0x0B` | `EIATTR_SMEM_PARAM_OFFSETS` | Free | Param |
| 12 | `0x0C` | `EIATTR_CBANK_PARAM_OFFSETS` | Free | Param |
| 13 | `0x0D` | `EIATTR_SYNC_STACK` | Idx | Metadata |
| 14 | `0x0E` | `EIATTR_TEXID_SAMPID_MAP` | Free | Texture |
| 15 | `0x0F` | `EIATTR_EXTERNS` | Free | Metadata |
| 16 | `0x10` | `EIATTR_REQNTID` | Idx | Resource |
| 17 | `0x11` | `EIATTR_FRAME_SIZE` | Idx | Resource |
| 18 | `0x12` | `EIATTR_MIN_STACK_SIZE` | Idx | Resource |
| 19 | `0x13` | `EIATTR_SAMPLER_FORCE_UNNORMALIZED` | Idx | Texture |
| 20 | `0x14` | `EIATTR_BINDLESS_IMAGE_OFFSETS` | Free | Texture |
| 21 | `0x15` | `EIATTR_BINDLESS_TEXTURE_BANK` | Idx | Texture |
| 22 | `0x16` | `EIATTR_BINDLESS_SURFACE_BANK` | Idx | Texture |
| 23 | `0x17` | `EIATTR_KPARAM_INFO` | Free | Param |
| 24 | `0x18` | `EIATTR_SMEM_PARAM_SIZE` | Idx | Param |
| 25 | `0x19` | `EIATTR_CBANK_PARAM_SIZE` | Sized | Param |
| 26 | `0x1A` | `EIATTR_QUERY_NUMATTRIB` | Idx | Metadata |
| 27 | `0x1B` | `EIATTR_MAXREG_COUNT` | Sized | Resource |
| 28 | `0x1C` | `EIATTR_EXIT_INSTR_OFFSETS` | Free | Offsets |
| 29 | `0x1D` | `EIATTR_S2RCTAID_INSTR_OFFSETS` | Free | Offsets |
| 30 | `0x1E` | `EIATTR_CRS_STACK_SIZE` | Idx | Resource |
| 31 | `0x1F` | `EIATTR_NEED_CNP_WRAPPER` | Idx | Metadata |
| 32 | `0x20` | `EIATTR_NEED_CNP_PATCH` | Idx | Metadata |
| 33 | `0x21` | `EIATTR_EXPLICIT_CACHING` | Idx | Metadata |
| 34 | `0x22` | `EIATTR_ISTYPEP_USED` | Idx | Metadata |
| 35 | `0x23` | `EIATTR_MAX_STACK_SIZE` | Idx | Resource |
| 36 | `0x24` | `EIATTR_SUQ_USED` | Idx | Metadata |
| 37 | `0x25` | `EIATTR_LD_CACHEMOD_INSTR_OFFSETS` | Free | Offsets |
| 38 | `0x26` | `EIATTR_LOAD_CACHE_REQUEST` | Idx | Metadata |
| 39 | `0x27` | `EIATTR_ATOM_SYS_INSTR_OFFSETS` | Free | Offsets |
| 40 | `0x28` | `EIATTR_COOP_GROUP_INSTR_OFFSETS` | Free | Offsets |
| 41 | `0x29` | `EIATTR_COOP_GROUP_MASK_REGIDS` | Idx | Cluster |
| 42 | `0x2A` | `EIATTR_SW1850030_WAR` | Free | WAR |
| 43 | `0x2B` | `EIATTR_WMMA_USED` | Idx | Metadata |
| 44 | `0x2C` | `EIATTR_HAS_PRE_V10_OBJECT` | Val | Metadata |
| 45 | `0x2D` | `EIATTR_ATOMF16_EMUL_INSTR_OFFSETS` | Free | Offsets |
| 46 | `0x2E` | `EIATTR_ATOM16_EMUL_INSTR_REG_MAP` | Free | Offsets |
| 47 | `0x2F` | `EIATTR_REGCOUNT` | Idx | Resource |
| 48 | `0x30` | `EIATTR_SW2393858_WAR` | Free | WAR |
| 49 | `0x31` | `EIATTR_INT_WARP_WIDE_INSTR_OFFSETS` | Free | Offsets |
| 50 | `0x32` | `EIATTR_SHARED_SCRATCH` | Idx | Shared |
| 51 | `0x33` | `EIATTR_STATISTICS` | Free | Metadata |
| 52 | `0x34` | `EIATTR_INDIRECT_BRANCH_TARGETS` | Free | Offsets |
| 53 | `0x35` | `EIATTR_SW2861232_WAR` | Free | WAR |
| 54 | `0x36` | `EIATTR_SW_WAR` | Free | WAR |
| 55 | `0x37` | `EIATTR_CUDA_API_VERSION` | Idx | Metadata |
| 56 | `0x38` | `EIATTR_NUM_MBARRIERS` | Idx | Resource |
| 57 | `0x39` | `EIATTR_MBARRIER_INSTR_OFFSETS` | Free | Offsets |
| 58 | `0x3A` | `EIATTR_COROUTINE_RESUME_OFFSETS` | Free | Offsets |
| 59 | `0x3B` | `EIATTR_SAM_REGION_STACK_SIZE` | Idx | Resource |
| 60 | `0x3C` | `EIATTR_PER_REG_TARGET_PERF_STATS` | Free | Metadata |
| 61 | `0x3D` | `EIATTR_CTA_PER_CLUSTER` | Idx | Cluster |
| 62 | `0x3E` | `EIATTR_EXPLICIT_CLUSTER` | Idx | Cluster |
| 63 | `0x3F` | `EIATTR_MAX_CLUSTER_RANK` | Idx | Cluster |
| 64 | `0x40` | `EIATTR_INSTR_REG_MAP` | Free | Metadata |
| 65 | `0x41` | `EIATTR_RESERVED_SMEM_USED` | Idx | Shared |
| 66 | `0x42` | `EIATTR_RESERVED_SMEM_0_SIZE` | Idx | Shared |
| 67 | `0x43` | `EIATTR_UCODE_SECTION_DATA` | Free | Metadata |
| 68 | `0x44` | `EIATTR_UNUSED_LOAD_BYTE_OFFSET` | Free | Offsets |
| 69 | `0x45` | `EIATTR_KPARAM_INFO_V2` | Free | Param |
| 70 | `0x46` | `EIATTR_SYSCALL_OFFSETS` | Free | Offsets |
| 71 | `0x47` | `EIATTR_SW_WAR_MEMBAR_SYS_INSTR_OFFSETS` | Free | WAR |
| 72 | `0x48` | `EIATTR_GRAPHICS_GLOBAL_CBANK` | Idx | Graphics |
| 73 | `0x49` | `EIATTR_SHADER_TYPE` | Idx | Graphics |
| 74 | `0x4A` | `EIATTR_VRC_CTA_INIT_COUNT` | Idx | Graphics |
| 75 | `0x4B` | `EIATTR_TOOLS_PATCH_FUNC` | Idx | Metadata |
| 76 | `0x4C` | `EIATTR_NUM_BARRIERS` | Idx | Resource |
| 77 | `0x4D` | `EIATTR_TEXMODE_INDEPENDENT` | Idx | Texture |
| 78 | `0x4E` | `EIATTR_PERF_STATISTICS` | Free | Metadata |
| 79 | `0x4F` | `EIATTR_AT_ENTRY_FRAGMENTS` | Free | Blackwell |
| 80 | `0x50` | `EIATTR_SPARSE_MMA_MASK` | Free | Blackwell |
| 81 | `0x51` | `EIATTR_TCGEN05_1CTA_USED` | Idx | Blackwell |
| 82 | `0x52` | `EIATTR_TCGEN05_2CTA_USED` | Idx | Blackwell |
| 83 | `0x53` | `EIATTR_GEN_ERRBAR_AT_EXIT` | Idx | Blackwell |
| 84 | `0x54` | `EIATTR_REG_RECONFIG` | Idx | Blackwell |
| 85 | `0x55` | `EIATTR_ANNOTATIONS` | Free | Metadata |
| 86 | `0x56` | `EIATTR_UNKNOWN` | -- | Sentinel |
| 87 | `0x57` | `EIATTR_STACK_CANARY_TRAP_OFFSETS` | Free | Offsets |
| 88 | `0x58` | `EIATTR_STUB_FUNCTION_KIND` | Idx | Metadata |
| 89 | `0x59` | `EIATTR_LOCAL_CTA_ASYNC_STORE_OFFSETS` | Free | Offsets |
| 90 | `0x5A` | `EIATTR_MERCURY_FINALIZER_OPTIONS` | Free | Mercury |
| 91 | `0x5B` | `EIATTR_BLOCKS_ARE_CLUSTERS` | Idx | Cluster |
| 92 | `0x5C` | `EIATTR_SANITIZE` | Idx | Blackwell |
| 93 | `0x5D` | `EIATTR_SYSCALLS_FALLBACK` | Free | Metadata |
| 94 | `0x5E` | `EIATTR_CUDA_REQ` | Free | Metadata |
| 95 | `0x5F` | `EIATTR_MERCURY_ISA_VERSION` | Sized | Mercury |
| 96 | `0x60` | `EIATTR_ERROR_LAST` | -- | Sentinel |

**Fmt** column: Idx = format `0x04` (indexed, per-symbol), Free = format `0x01` (raw bytes), Val = format `0x02` (single 32-bit value), Sized = format `0x03` (16-bit value).

### EIATTR Codes Confirmed in ptxas Builder

The following codes appear as explicit `case` labels in the `sub_1CC9800` switch statement or as arguments to `sub_1CC85F0`:

| Code | Hex | Confirmed via |
|---:|---:|---|
| 4 | `0x04` | `case 0x4` in builder -- CTAIDZ_USED |
| 13 | `0x0D` | `case 0xD` -- SYNC_STACK |
| 15 | `0x0F` | `case 0xF` + `sub_1CC85F0(_, 0xF, ...)` -- EXTERNS |
| 17 | `0x11` | `case 0x11` -- FRAME_SIZE |
| 18 | `0x12` | `case 0x12` + `sub_1CC85F0(_, 0x12, ...)` -- MIN_STACK_SIZE |
| 27 | `0x1B` | `case 0x1B` -- MAXREG_COUNT |
| 30 | `0x1E` | `case 0x1E` + `sub_1CC85F0(_, 0x1E, ...)` -- CRS_STACK_SIZE |
| 35 | `0x23` | `case 0x23` -- MAX_STACK_SIZE |
| 38 | `0x26` | `case 0x26` -- LOAD_CACHE_REQUEST |
| 47 | `0x2F` | `case 0x2F` + `sub_1CC85F0(_, 0x2F, ...)` -- REGCOUNT |
| 56 | `0x38` | `case 0x38` -- NUM_MBARRIERS |
| 59 | `0x3B` | `case 0x3B` + `sub_1CC85F0(_, 0x3B, ...)` -- SAM_REGION_STACK_SIZE |
| 65 | `0x41` | `case 0x41` -- RESERVED_SMEM_USED |
| 74 | `0x4A` | `case 0x4A` -- VRC_CTA_INIT_COUNT |
| 76 | `0x4C` | `case 0x4C` -- NUM_BARRIERS |
| 79 | `0x4F` | `case 0x4F` + `sub_1CC85F0(_, 0x4F, ...)` -- AT_ENTRY_FRAGMENTS |
| 80 | `0x50` | `case 0x50` + `sub_1C97840(0x50, ...)` -- SPARSE_MMA_MASK |
| 81 | `0x51` | `case 0x51` -- TCGEN05_1CTA_USED |
| 82 | `0x52` | `case 0x52` -- TCGEN05_2CTA_USED |
| 84 | `0x54` | `case 0x54` -- REG_RECONFIG |

The builder's first pass uses a switch with cases `0x04`, `0x0D`, `0x0F`, `0x11`, `0x12`, `0x1B`, `0x1E`, `0x23`, `0x26`, `0x2F`, `0x38`, `0x3B`, `0x41`, `0x4A`, `0x4C`, `0x4F`, `0x50`, `0x51`, `0x52`, `0x54` to sort EIATTR records into per-entry arrays. A second pass emits the final records via `sub_1CC85F0` and `sub_1CC86D0`.

### Symbol Index Resolution Pass

Before the main builder runs, the EIATTR builder performs a symbol index resolution pass (lines 700--884 in the decompiled builder). This pass walks all pre-existing EIATTR records and resolves symbol indices through the linker's mapping tables:

```c
// Simplified from sub_1CC9800 lines ~716-824
for (record in eiattr_list) {
    switch (record->attr_code) {
    case 0x02: case 0x06: case 0x07: case 0x08: case 0x09:
    case 0x0A: case 0x11: case 0x12: case 0x13: case 0x14:
    case 0x17: case 0x23: case 0x26: case 0x2F: case 0x3B:
    case 0x45:
        // Indexed format: resolve sym_idx through mapping table
        int32_t *sym_ptr = (int32_t *)record->payload;
        if (mapping_table && *sym_ptr != 0) {
            if (*sym_ptr < 0)
                *sym_ptr = negative_mapping[-(*sym_ptr)];
            else
                *sym_ptr = mapping_table[*sym_ptr];
        }
        if (*sym_ptr == 0 && attr_code != 0x45 && attr_code != 0x17) {
            record->attr_code = 0;  // disable record
        }
        break;
    case 0x0F:
        // EXTERNS: resolve each 4-byte symbol index in the array
        int count = record->size / 4;
        for (int i = 0; i < count; i++) {
            resolve_sym(&payload[i], mapping_table, negative_mapping);
        }
        break;
    }
}
```

The bitmask `0x800800060000` (seen at line 716) encodes which attribute codes use the simple indexed-resolve path: it selects codes 2, 6, 7, 8, 9, 10, 17, 18, 19, 20, 23, 38, 47, 59, 69.

### Barrier and Register Propagation -- `sub_1CC8950`

When a device function uses barriers or a high register count, those requirements must propagate upward through the call graph to each entry kernel. The propagator `sub_1CC8950` handles this:

```
"Creating new EIATTR_NUM_BARRIERS and moving barcount %d
    from section flags of %s to nvinfo for entry symbol %s"
"Propagating higher barcount %d to the section flags
    of %s of entry symbol %s"
"regcount %d for %s propagated to entry %s"
```

The propagator emits `EIATTR_REGCOUNT` (0x2F) records via `sub_1CC85F0(_, 0x2F, 8, ...)` and handles `EIATTR_NUM_BARRIERS` (0x4C) through the `sub_1CC7FB0` path. Barrier counts are extracted from the section flags field at bit offset 20 (7-bit field, mask `0x7F`), then cleared from the section flags (`&= 0xF80FFFFF`) after being moved into an EIATTR record.

### EIATTR Categories by Function

**Resource allocation** (GPU driver reads these to configure hardware at launch):

| Code | Name | Description |
|---:|---|---|
| `0x2F` | REGCOUNT | Physical register count per thread (primary occupancy determinant) |
| `0x05` | MAX_THREADS | Maximum threads per block (`.maxntid`) |
| `0x10` | REQNTID | Required block dimensions (`.reqntid`, 3x uint32) |
| `0x11` | FRAME_SIZE | Per-thread local memory frame size (bytes) |
| `0x12` | MIN_STACK_SIZE | Minimum call stack (non-recursive) |
| `0x23` | MAX_STACK_SIZE | Maximum call stack (recursive) |
| `0x1E` | CRS_STACK_SIZE | Call-Return-Sync stack |
| `0x3B` | SAM_REGION_STACK_SIZE | SAM (Streaming Async Memory) region stack |
| `0x4C` | NUM_BARRIERS | Named barrier count (0--16) |
| `0x38` | NUM_MBARRIERS | Memory barrier (mbarrier) object count |
| `0x1B` | MAXREG_COUNT | Register count hint (`--maxrregcount` / `.maxnreg`) |

**Parameter bank**:

| Code | Name | Description |
|---:|---|---|
| `0x0A` | PARAM_CBANK | Constant bank number + offset for parameters |
| `0x19` | CBANK_PARAM_SIZE | Parameter constant bank size |
| `0x18` | SMEM_PARAM_SIZE | Shared memory parameter region size |
| `0x0B` | SMEM_PARAM_OFFSETS | Per-parameter shared memory offsets |
| `0x0C` | CBANK_PARAM_OFFSETS | Per-parameter constant bank offsets |
| `0x17` | KPARAM_INFO | Per-parameter metadata (v1) |
| `0x45` | KPARAM_INFO_V2 | Per-parameter metadata (v2, extended) |

**Instruction offset tables** (driver/tools locate and patch instructions at load time):

| Code | Name | Description |
|---:|---|---|
| `0x1C` | EXIT_INSTR_OFFSETS | Byte offsets of `EXIT` instructions |
| `0x1D` | S2RCTAID_INSTR_OFFSETS | Offsets of `S2R SR_CTAID.*` instructions |
| `0x25` | LD_CACHEMOD_INSTR_OFFSETS | Load instructions with cache modifier |
| `0x27` | ATOM_SYS_INSTR_OFFSETS | Atomic instructions with `.sys` scope |
| `0x28` | COOP_GROUP_INSTR_OFFSETS | Cooperative group instructions |
| `0x2D` | ATOMF16_EMUL_INSTR_OFFSETS | Emulated FP16 atomics |
| `0x2E` | ATOM16_EMUL_INSTR_REG_MAP | Register map for 16-bit atomic emulation |
| `0x31` | INT_WARP_WIDE_INSTR_OFFSETS | Integer warp-wide instructions |
| `0x34` | INDIRECT_BRANCH_TARGETS | Valid indirect branch targets (CFI) |
| `0x39` | MBARRIER_INSTR_OFFSETS | `MBAR` memory barrier instructions |
| `0x3A` | COROUTINE_RESUME_OFFSETS | Device coroutine resume points |
| `0x44` | UNUSED_LOAD_BYTE_OFFSET | Unused load instruction byte offset |
| `0x46` | SYSCALL_OFFSETS | `__cuda_syscall` invocation offsets |
| `0x57` | STACK_CANARY_TRAP_OFFSETS | Stack canary trap instructions |
| `0x59` | LOCAL_CTA_ASYNC_STORE_OFFSETS | CTA-local async store instructions |

**Texture and surface**:

| Code | Name | Description |
|---:|---|---|
| `0x02` | IMAGE_SLOT | Texture/surface image slot assignment |
| `0x06` | IMAGE_OFFSET | Image descriptor table offset |
| `0x07` | IMAGE_SIZE | Image descriptor size |
| `0x08` | TEXTURE_NORMALIZED | Normalized texture coordinates flag |
| `0x09` | SAMPLER_INIT | Sampler initialization data |
| `0x0E` | TEXID_SAMPID_MAP | Texture-to-sampler mapping table |
| `0x13` | SAMPLER_FORCE_UNNORMALIZED | Force unnormalized sampler |
| `0x14` | BINDLESS_IMAGE_OFFSETS | Bindless texture/surface offsets |
| `0x15` | BINDLESS_TEXTURE_BANK | Constant bank for bindless textures |
| `0x16` | BINDLESS_SURFACE_BANK | Constant bank for bindless surfaces |
| `0x4D` | TEXMODE_INDEPENDENT | Independent texture mode |

**Cluster and cooperative launch (sm_90+)**:

| Code | Name | Description |
|---:|---|---|
| `0x29` | COOP_GROUP_MASK_REGIDS | Cooperative group mask register IDs |
| `0x3D` | CTA_PER_CLUSTER | CTAs per cluster (Hopper+) |
| `0x3E` | EXPLICIT_CLUSTER | Explicit cluster dimensions |
| `0x3F` | MAX_CLUSTER_RANK | Maximum cluster rank |
| `0x5B` | BLOCKS_ARE_CLUSTERS | CTA blocks are clusters flag |

**Shared memory**:

| Code | Name | Description |
|---:|---|---|
| `0x32` | SHARED_SCRATCH | Shared memory scratch for register spilling |
| `0x41` | RESERVED_SMEM_USED | Reserved shared memory in use |
| `0x42` | RESERVED_SMEM_0_SIZE | Reserved shared memory partition 0 size |

**Hardware workarounds**:

| Code | Name | Description |
|---:|---|---|
| `0x2A` | SW1850030_WAR | HW bug 1850030 workaround |
| `0x30` | SW2393858_WAR | HW bug 2393858 workaround |
| `0x35` | SW2861232_WAR | HW bug 2861232 workaround |
| `0x36` | SW_WAR | Generic workaround container |
| `0x47` | SW_WAR_MEMBAR_SYS_INSTR_OFFSETS | `MEMBAR.SYS` workaround offsets |

**Blackwell+ (sm_100+)**:

| Code | Name | Description |
|---:|---|---|
| `0x4F` | AT_ENTRY_FRAGMENTS | Fragment descriptors at function entry |
| `0x50` | SPARSE_MMA_MASK | Structured sparsity mask for MMA |
| `0x51` | TCGEN05_1CTA_USED | 5th-gen tensor core (single-CTA mode) |
| `0x52` | TCGEN05_2CTA_USED | 5th-gen tensor core (two-CTA mode) |
| `0x53` | GEN_ERRBAR_AT_EXIT | Generate error barrier at kernel exit |
| `0x54` | REG_RECONFIG | Dynamic register reconfiguration (setmaxnreg) |
| `0x5C` | SANITIZE | Address sanitizer instrumentation present |

**Mercury**:

| Code | Name | Description |
|---:|---|---|
| `0x5A` | MERCURY_FINALIZER_OPTIONS | Options for Mercury FNLZR post-link pass |
| `0x5F` | MERCURY_ISA_VERSION | Mercury ISA version for shader binary |

**Graphics-specific**:

| Code | Name | Description |
|---:|---|---|
| `0x48` | GRAPHICS_GLOBAL_CBANK | Global constant bank for graphics shaders |
| `0x49` | SHADER_TYPE | Shader type (vertex, fragment, compute, etc.) |
| `0x4A` | VRC_CTA_INIT_COUNT | Virtual Register Count CTA init count |

## .nv.compat Section

The `.nv.compat` section (`SHT_CUDA_COMPAT` = `0x70000086`) stores forward-compatibility attributes. Its records use a different format from EIATTR -- each is a small TLV with:

```
Offset  Size  Field
------  ----  -----
0x00    1     format (always 0x02 = value)
0x01    1     compat_code
0x02    1     value
```

The `sub_1CC93A0` handler processes these with a switch over compat codes 2--6:

| Code | Behavior |
|---|---|
| 2 | Max of existing and new value (keeps higher) |
| 3 | OR existing with new value (accumulate flags) |
| 4 | Reset to zero |
| 5 | Per-nibble max (two 2-bit fields) |
| 6 | Set to 1 if values differ (conflict detection) |

The guard `*(_DWORD *)(a1 + 72) <= 0x59` (SM version <= 89 decimal) means compat processing only applies to SM 90 (Hopper) and later. Unknown compat codes trigger: `"unknown .nv.compat attribute (%x) encoutered with value %x."` (note the typo "encoutered" in the binary string).

## Architecture-Gated EIATTR Emission

Not all EIATTR codes are valid on all SM architectures. The function `sub_1C97840` performs architecture checks before emitting a record. Observed gates:

| EIATTR Code | Gate | Meaning |
|---|---|---|
| `0x04` (CTAIDZ_USED) | Always emitted | |
| `0x41` (RESERVED_SMEM_USED) | `sub_1C97840(0x41, sm)` | SM-version dependent |
| `0x4C` (NUM_BARRIERS) | `sub_1C97840(0x4C, sm)` | SM-version dependent |
| `0x50` (SPARSE_MMA_MASK) | `sub_1C97840(0x50, sm)` | SM 100+ (Blackwell) |
| `0x51` (TCGEN05_1CTA) | `sub_1C97840(0x51, sm)` implicit | SM 100+ |
| `0x52` (TCGEN05_2CTA) | `sub_1C97840(0x52, sm)` implicit | SM 100+ |
| `0x54` (REG_RECONFIG) | `sub_1C97840(0x54, sm)` implicit | SM 100+ |

The `sub_1C97840` function takes an EIATTR code and the SM version from the ELFW object's field at offset 624, returning a boolean. This prevents older EIATTR codes from appearing in Blackwell cubins and prevents Blackwell-only codes from appearing in Hopper cubins.

## Constant Bank Optimization

The master section allocator `sub_1CABD60` (11,856 bytes) performs two major space optimizations during address assignment: **constant value deduplication** within `.nv.constant0` banks, and **shared memory interference-graph coloring** for extern shared variables. Both run before final offset assignment.

### Constant Value Deduplication -- `sub_1CA6890`

When multiple kernels in the same compilation unit use identical constant values, the OCG constant bank can contain duplicates. `sub_1CA6890` (454 lines decompiled) eliminates them by value-matching, reducing `.nv.constant0` section size.

The algorithm dispatches on constant value width:

| Value Width | Dedup Strategy | Data Structure |
|---|---|---|
| 4 bytes | Hash map lookup (`sub_426D60`) | Hash table keyed on 32-bit value |
| 8 bytes | Hash map lookup (separate table) | Hash table keyed on 64-bit value |
| 12, 16, 20, 24, 32, 48, 64 bytes | Linear scan with `memcmp` (`sub_1CA6760`) | Per-width linked list |
| Other | No deduplication | Direct append |

For each constant data node in the section's linked list (at section+72):

1. Extract the value bytes (node+0), alignment (node+16), and size (node+24).
2. Look up the value in the appropriate dedup structure.
3. **If duplicate found**: alias the current symbol's offset to the existing symbol's offset. Debug output: `"found duplicate value 0x%x, alias %s to %s"` (32-bit) or `"found duplicate 64bit value 0x%llx, alias %s to %s"` (64-bit) or `"found duplicate %d byte value, alias %s to %s"` (N-byte via `sub_1CA6760`).
4. **If not found**: align the section cursor to the required alignment, append the data via `sub_1CA6650`, and insert into the dedup structure.

After aliasing, the function rewrites pending relocations that targeted the now-eliminated range:

```c
// Simplified relocation rewriting after dedup alias
for (reloc in pending_relocs) {
    if (reloc.section == target_section
        && reloc.offset >= old_data_offset
        && reloc.offset <  old_data_offset + old_data_size) {
        reloc.offset = reloc.offset + alias_target_offset - old_data_offset;
        // "optimize ocg constant reloc offset from %lld to %lld"
        unlink(reloc);  // remove from pending list
    }
}
```

Special cases:
- **Zero-valued constants**: A "seen set" (parameter a15) prevents distinct zero-valued symbols from being aliased to each other, since different `__constant__` variables may legitimately hold zero but need separate addresses.
- **Redirect mode**: When parameter a13 is set and `sub_1CB15C0` returns true for a symbol, the constant is redirected to its defining section rather than deduplicated.

The caller `sub_1CABD60` wraps this in an optimization check: `"optimize OCG constants for %s, old size = %lld"`. If dedup does not reduce the section size, it reverts: `"ocg const optimization didn't help so give up"`.

### Shared Memory Interference Graph -- `sub_1CA92F0`

When a CUDA program declares multiple `extern __shared__` variables used by different kernels, they can potentially share the same memory if no single kernel uses both simultaneously. `sub_1CA92F0` (585 lines decompiled) builds an interference graph and performs greedy graph coloring to pack shared objects into minimum total space.

**Phase 1 -- Build usage sets** (which kernels reference each shared object):

For each global shared object, walk all referencing functions. A kernel "uses" a shared object if it directly references it or transitively calls a device function that does (traced via `sub_1CBD800`). Objects used by exactly one kernel are **privatized** -- moved into a per-entry `.nv.shared.<func>` section. Unused objects are removed entirely (symbol flags set to mark deleted).

```
"global shared %s only used in entry %d"    -- privatize
"remove unused global shared %s"             -- delete
```

**Phase 2 -- Build interference edges**:

For each pair of remaining shared objects (i, j), test whether their usage sets intersect (via `sub_42E460` set membership). If any kernel uses both, they **interfere** -- they cannot overlap in memory. Edges are stored as linked lists per object.

**Phase 3 -- Greedy graph coloring**:

Objects are processed in sorted order. For each object:

1. Mark all colors used by interfering neighbors as unavailable.
2. Assign the lowest available color (starting from 1).
3. Update the color's alignment requirement (max of all objects in that color group).
4. Update the color's size requirement (max of all objects in that color group).

```
"  allocate to group %d"    -- color assignment
```

**Phase 4 -- Compute group offsets**:

Groups are laid out sequentially with alignment padding:

```c
group_offset[1] = align_up(base, group_align[1]);
for (g = 2; g <= num_groups; g++)
    group_offset[g] = align_up(group_offset[g-1] + group_size[g-1], group_align[g]);
total_size = group_offset[last] + group_size[last];
```

Each shared object's final offset is `group_offset[its_color]`. The total extern shared size is written to the section descriptor. Per-entry shared sections are expanded if a referenced object's offset + size exceeds their current size.

```
"esh %s size = %lld"
"for shared object (%d) %s:"
"  offset = 0x%llx, size = 0x%llx"
"  edge to %d"
"  allocate to group %d"
```

### Constant Bank Optimization Functions

| Address | Size | Purpose |
|---|---|---|
| `sub_1CA6890` | 454 lines | Constant value deduplication (32/64-bit hash, N-byte memcmp) |
| `sub_1CA6760` | 57 lines | N-byte value dedup helper (12--64 byte constants) |
| `sub_1CA6650` | 65 lines | Constant data node appender (40-byte node, alignment + append) |
| `sub_1CA92F0` | 585 lines | Shared memory interference graph + greedy coloring |
| `sub_1CA91A0` | -- | Per-entry shared section creator (`.nv.shared.<func>`) |
| `sub_1CA5360` | -- | Shared object comparison function (sort key) |
| `sub_1CA5A00` | -- | Shared memory data copier (offset overlap check) |

## Section Attribute Builder -- `sub_60FBF0`

The per-kernel section attribute builder `sub_60FBF0` (76 KB decompiled, 2,541 lines, VA `0x60FBF0`) runs once for each kernel entry point and device function. It assembles the full per-function section configuration object (648 bytes), parses compile option overrides, remaps PTX memory space codes to ELF section type IDs, conditionally creates three section types, then invokes the Mercury codegen pipeline (`sub_6F52F0`, DecodePipeline::RunStages).

### Inputs

The function takes three parameters:

| Parameter | Content |
|---|---|
| `a1` | Per-function descriptor: SM version (`a1[0..1]`), key-value option list (`a1+38`), assembler flags (`a1+39`), global/extern symbol lists (`a1+6`, `a1+7`), boolean flags (`a1+180..182`) |
| `a2` | Compilation context: config base (`a2+248`), function list (`a2+136`), optional symbol tables for textures (`a2+112`), surfaces (`a2+120`), globals (`a2+72`), sass_map flag (`a2+232`), mutex (`a2+240`), ELFW object (`a2+32`), target descriptor (`a2+56`) |
| `a3` | Output handle (released and reallocated at function entry) |

### Option Parsing

The function iterates the key-value list at `a1+38` and matches five string keys by character-by-character comparison:

```c
// Simplified from sub_60FBF0 lines ~638-812
for (int i = 0; i < list_length(a1->options); i++) {
    const char** kv = list_get(a1->options, i);
    if (strcmp(kv[0], "deviceDebug") == 0)
        config->deviceDebug = 1;                // config+24
    else if (strcmp(kv[0], "lineInfo") == 0)
        config->lineInfo = 1;                   // config+25
    else if (strcmp(kv[0], "optLevel") == 0) {
        if (!config->optLevel_locked)            // config+108
            config->optLevel = strtol(kv[1], ...);  // config+104
    }
    else if (strcmp(kv[0], "IsCompute") == 0)
        config->isCompute = (strcmp(kv[1], "True") == 0);  // config+184
    else if (strcmp(kv[0], "IsPIC") == 0)
        config->isPIC = (strcmp(kv[1], "True") == 0);      // config+191
}
```

Additional inputs read from `a1` and `a2`:

| Source | Config field | Description |
|---|---|---|
| `a2+408` | config+44 | Optimization level from compilation context |
| `a1+180` | config+190 | Negated boolean flag |
| `a1+181` | config+188 | Boolean flag |
| `a1+182` | config+189 | Boolean flag |
| `word_2020620[a2+64 - 20]` | `v31+4` | SM version lookup table (SM 20..121 range) |

### Memory Space Type Remapping

PTX internal memory space type codes in the `0x10000` range are remapped to compact ELF section type IDs by the helper `sub_60DA40` (and inline copies). This remapping is applied to every symbol in the global, extern, texture, surface, and per-function symbol lists:

| PTX Code | Hex | Section Type ID | Memory Space |
|---:|---|---:|---|
| 65538 | `0x10002` | 83 | `.nv.shared` (per-kernel) |
| 65539 | `0x10003` | 80 | `.nv.constant0` (kernel params) |
| 65540 | `0x10004` | 84 | `.nv.local` (spill memory) |
| 65541 | `0x10005` | 81 | `.nv.constant1` |
| 65542 | `0x10006` | 82 | `.nv.constant2` |
| 65544 | `0x10008` | 85 | `.nv.constant3` |
| 65545 | `0x10009` | 86 | `.nv.constant4` |
| 65546 | `0x1000A` | 87 | `.nv.constant5` |
| 65576 | `0x10028` | 88 | `.nv.global.init` |
| 65577 | `0x10029` | 89 | `.nv.global` |
| 65586 | `0x10032` | 93 | High constant bank |
| 65587 | `0x10033` | 90 | High constant bank |
| 65598 | `0x1003E` | 91 | Texture/surface descriptor |
| 65599 | `0x1003F` | 92 | Texture/surface descriptor |

Special handling: when the space code is `0x10003` (constant0) and the compilation mode is relocatable (`*(a3+48) == 12`), the descriptor's needs_reloc flag (byte 33) is set to 1, indicating the constant0 section requires special relocation handling during linking.

The value `65596` (`0x1003C`) serves as a threshold -- symbols with `(space_type - 0x1003C) < 2` are counted into the texture/surface allocation arrays.

### Conditional Section Creation

Three per-kernel sections are conditionally created:

**`.sass_map.<func>`** -- created when `*(a2+232) != 0` (sass_map generation enabled):

```c
if (context->sass_map_enabled) {                 // a2+232
    descriptor = alloc(64);                       // 64-byte section descriptor
    memset(descriptor, 0, 64);
    pthread_mutex_lock(context->mutex);           // a2+240
    // Allocate instruction tree node and connect to codegen state
    name = sprintf(".sass_map%s", func_name);     // ".sass_map" + func_name
    descriptor->name = name;
    pthread_mutex_unlock(context->mutex);
}
```

**`.nv.local.<func>`** -- created when the register spill size (config+112) is non-zero:

```c
if (config->spill_size != 0) {                   // config+112
    descriptor = alloc(64);
    descriptor->size = config->spill_size;
    // Name: ".nv.local." + bare_func_name (skip ".text." prefix)
    name = sprintf(".nv.local.%s", func_name + 6);
}
```

The spill size at config+112 is set from the sum of the register spill count and frame size when the spill flag is non-zero.

**`.nv.constant<N>.<func>`** -- created when:
1. The compilation mode field equals 2 (`*(a1->target+48) == 2`)
2. No pre-existing constant section exists (`*(a1+172) == 0`)
3. The function's symbol list is empty

```c
if (mode == 2 && !has_constant_section && no_symbols) {
    descriptor = alloc(64);
    int bank = target->get_section_type() - 0x70000064;
    int size;
    if (func_const_size <= target->get_min_const_size())
        size = target->get_min_const_size();     // vtable+80
    else
        size = target->get_max_const_size();     // vtable+88
    descriptor->size = size + func_const_size;
    name = sprintf(".nv.constant%d.%s", bank, bare_func_name);
    descriptor->data = calloc(descriptor->size);
}
```

### Assembler Flag Processing

The assembler flag list at `a1+39` is iterated. Each entry's value string (at offset +8) is split on spaces via `strtok_r`. Each token is validated by `sub_60F790`, which constructs a temporary 656-byte object to test the flag. Valid tokens are concatenated with spaces and appended to config+48 (the toolkit info string that ends up in `.note.nv.tkinfo`).

### Codegen Pipeline Invocation

After configuration, the function calls `sub_6F52F0` (DecodePipeline::RunStages) with 18 parameters including the configuration object, all 7 descriptor arrays, the ELFW context, and the function name. The return code is mapped:

| `sub_6F52F0` return | `sub_60FBF0` return | Meaning |
|---|---|---|
| 0 | 0 | Success |
| 1 | 14 | Mercury encode failure |
| 2 | 22 | Mercury decode failure |

### Post-Pipeline Section Registration

After the Mercury pipeline returns successfully:

1. Calls `sub_60DD30` twice for pre/post code region finalization
2. Calls `sub_60DBE0` for each optional symbol table (texture, surface, global) to register their sections with the ELFW emitter
3. Calls `sub_1CB9C30` on the ELFW object (`a2+32`) to commit all sections
4. If SM version <= `0x45` (SM 69): creates UFT/UDT entries (section types 68/69) for each resolved symbol
5. Under mutex lock, ORs the per-function WAR bitmask (`config+232..240`) into the global accumulator at `a2+504`

### Thread Safety

All shared state modifications are protected by the mutex at `a2+240`:
- String length accumulator updates (`a2+296`, `a2+304`) for string table pre-allocation
- WAR bitmask accumulation (`a2+504`)
- `.sass_map` section setup (instruction tree access)
- Instruction merge from secondary codegen contexts (`a2+80`, `a2+88`)

## Key Functions

| Address | Size | Purpose |
|---|---|---|
| `sub_60FBF0` | ~76 KB decompiled | Per-kernel section attribute builder (section above) |
| `sub_1CC9800` | 14,764 B (90 KB decompiled) | EIATTR builder -- master nvinfo section constructor |
| `sub_1CC8950` | 2,634 B | EIATTR propagator -- barrier/register cross-function propagation |
| `sub_1CC85F0` | ~200 B | EIATTR record emitter -- writes one TLV record |
| `sub_1CC86D0` | ~500 B | Per-entry EIATTR emission (MIN_STACK_SIZE, CRS_STACK_SIZE, SAM_REGION_STACK_SIZE) |
| `sub_1CC7FB0` | ~200 B | .nv.info section creator/finder |
| `sub_1CC93A0` | ~500 B | .nv.compat attribute processor |
| `sub_1CB3570` | 1,963 B | Generic section creator (44 call sites) |
| `sub_1CB42D0` | -- | .text.\<func\> section creator |
| `sub_1C9DC60` | 5,663 B | Section layout calculator (offset assignment) |
| `sub_1CABD60` | 11,856 B | Master section allocator (shared/constant/local addresses) |
| `sub_1CBE1B0` | ~10 KB | .nv.callgraph section builder |
| `sub_1C97840` | -- | Architecture-gated EIATTR check |
| `sub_1CA6890` | 454 lines | Constant bank value deduplication |
| `sub_1CA92F0` | 585 lines | Shared memory interference graph + coloring |

## Cross-References

- [Custom ELF Emitter](elf-emitter.md) -- ELFW object, section ordering, ELF header
- [Relocations & Symbols](relocations.md) -- relocation resolution, UFT/UDT management
- [Debug Information](debug-info.md) -- DWARF generation and `.debug_*` sections
- [Mercury Encoder](../codegen/mercury.md) -- Mercury encoding that feeds `.nv.merc.*` sections
- [Capsule Mercury](../codegen/capmerc.md) -- SM 100+ capsule and `.nv.capmerc` sections
- [Pipeline Overview](../pipeline/overview.md) -- where section emission fits in the pipeline
