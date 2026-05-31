# ELF/Cubin Output

> *All addresses in this page apply to ptxas v13.0.88 (CUDA 13.0). Other versions will differ.*

After all per-kernel SASS encoding completes, ptxas enters the ELF output phase -- the final stage of the compilation pipeline. This phase transforms the accumulated per-kernel SASS bytes, relocation metadata, constant bank data, shared memory layouts, and debug information into a complete NVIDIA CUBIN file. The CUBIN is a standard ELF container with NVIDIA-proprietary extensions: machine type `EM_CUDA` (`0xBE`), non-standard ELF class bytes, CUDA-specific section types, and a rich per-entry metadata system called EIATTR. The output pipeline is a custom implementation with no libelf dependency -- ptxas constructs every byte of the ELF from scratch, including headers, section tables, symbol tables, string tables, relocations, and program headers.

The output phase handles three binary kinds: **SASS** (raw resolved SASS, legacy default), **Mercury** (SM 75--99 default), and **Capsule Mercury** (SM 100+ default, supporting deferred finalization). All three produce a valid CUBIN ELF; the difference is whether the `.text` sections contain final SASS bytes or Mercury-encoded streams that a downstream finalizer resolves at link or load time.

| | |
|---|---|
| **Cubin entry point** | `sub_612DE0` (47 KB, called from `sub_446240`) |
| **ELFW constructor** | `sub_1CB53A0` (3,480 bytes, 672-byte central object) |
| **Section creator** | `sub_1CB3570` (1,963 bytes, 44 call sites) |
| **Symbol table builder** | `sub_1CB68D0` (9,578 bytes, ~1,700 decompiled lines) |
| **Master ELF emitter** | `sub_1C9F280` (15,263 B native / 97 KB decomp -- largest function in output range) |
| **Section layout calculator** | `sub_1C9DC60` (5,663 B native) |
| **Master section allocator** | `sub_1CABD60` (11,856 B native / 66 KB decomp -- shared/constant/local addresses) |
| **nvinfo/EIATTR builder** | `sub_1CC9800` (14,764 B native / 86 KB decomp) |
| **Master relocation resolver** | `sub_1CD48C0` (4,184 B native / 20 KB decomp) |
| **File serializer** | `sub_1CD13A0` (2,541 B native / 11 KB decomp, writes final bytes to disk) |
| **ELF machine type** | `EM_CUDA` = `0xBE` (190) |
| **CUDA section type** | `SHT_CUDA_INFO` = `0x70000064` |
| **ELF timing** | `"ELF-time : %.3f ms (%.2f%%)"` in `--compiler-stats` output |
| **Peak ELF memory** | `"PeakELFMemoryUsage : %.3lf KB"` |

## Pipeline Overview

The ELF output pipeline runs as a single-threaded sequence after all per-kernel OCG passes have completed (multi-kernel compilation may be parallel, but ELF emission is serialized). The flow is orchestrated by `sub_612DE0`, which reads compilation flags, constructs the ELFW central object, then drives 11 phases to produce the final `.cubin` or `.o` file.

```text
sub_446240 (compilation driver -- "real main")
  |  all per-kernel OCG passes complete
  v
sub_612DE0 (cubin entry, 47KB)
  |  reads: deviceDebug, lineInfo, optLevel, IsCompute, IsPIC
  |  establishes setjmp/longjmp error recovery
  |  writes "Cuda compilation tools, release 13.0, V13.0.88"
  |         "Build cuda_13.0.r13.0/compiler.36424714_0"
  |
  |-- Phase 1: ELFW construction
  |     sub_1CB53A0 -- create 672-byte ELFW object, 7 standard sections
  |
  |-- Phase 2: Per-kernel section creation
  |     sub_1CB42D0 x N -- .text.<func>, .rela.text.<func> (one per kernel)
  |     sub_1CB3570 x 44 -- .nv.constant0.<func>, .nv.shared.<func>, etc.
  |
  |-- Phase 3: Call graph analysis
  |     sub_1CBB920 -- recursion detection (DFS)
  |     sub_1CBC090 -- dead function elimination
  |     sub_1CBE1B0 -- .nv.callgraph section builder
  |
  |-- Phase 4: Symbol fixup
  |     sub_1CB2CA0 -- renumber symbols after dead code elimination
  |     sub_1C99BB0 -- remap .symtab_shndx extended indices
  |
  |-- Phase 5: Memory allocation
  |     sub_1CABD60 -- assign addresses: shared, constant, local memory
  |     sub_1CA92F0 -- shared memory interference graph
  |     sub_1CA6890 -- constant bank deduplication
  |
  |-- Phase 6: nvinfo/EIATTR generation
  |     sub_1CC9800 -- build .nv.info.<func> sections (EIATTR attributes)
  |     sub_1CC8950 -- propagate barrier/register counts across call graph
  |
  |-- Phase 7: Symbol table construction
  |     sub_1CB68D0 -- build .symtab, handle SHN_XINDEX overflow
  |
  |-- Phase 8: Section layout
  |     sub_1C9DC60 -- compute file offsets with alignment padding
  |
  |-- Phase 9: Relocation resolution
  |     sub_1CD48C0 -- resolve all R_CUDA_* relocations
  |     sub_1CD5920 -- write .nv.resolvedrela sections
  |
  |-- Phase 10: Capsule Mercury embedding (SM 100+ only)
  |     sub_1C9B110 -- create .nv.merc.* section namespace
  |     sub_1CA2E40 -- clone memory-space sections into merc namespace
  |     sub_1C9C300 -- build 328-byte capsule descriptors per function
  |
  |-- Phase 11: Final assembly & write
  |     sub_1C9F280 -- master ELF emitter (assemble complete CUBIN)
  |     sub_1CD13A0 -- file serializer (write to disk)
  |
  v
OUTPUT: .cubin / .o file
```

## Custom ELF Emitter

ptxas builds the entire ELF output without libelf. The custom implementation spans approximately 20 functions in the `0x1C99`--`0x1CD6` address range (~300 KB of binary code). At the center is the **ELFW** ("ELF world") object -- a 672-byte structure that owns all sections, symbols, and string tables for a single compilation unit.

### ELFW Object Layout

The ELFW constructor `sub_1CB53A0` allocates 672 bytes from the pool allocator `sub_424070`, creates a dedicated `"elfw memory space"` pool (4,096-byte initial allocation), writes the ELF header, and initializes 7 mandatory sections:

| Index | Section | Type | Purpose |
|---|---|---|---|
| 0 | (null) | `SHT_NULL` | Required ELF null section |
| 1 | `.shstrtab` | `SHT_STRTAB` | Section name string table |
| 2 | `.strtab` | `SHT_STRTAB` | Symbol name string table |
| 3 | `.symtab` | `SHT_SYMTAB` | Symbol table |
| 4 | `.symtab_shndx` | `SHT_SYMTAB_SHNDX` | Extended section indices (for >65,280 sections) |
| 5 | `.note.nv.tkinfo` | `SHT_NOTE` | NVIDIA toolkit info (version, build ID, CLI args) |
| 6 | `.note.nv.cuinfo` | `SHT_NOTE` | NVIDIA CUDA info (SM version, features) |
| 7 | `.nv.uft.entry` | `SHT_PROGBITS` | Unified Function Table entries |

### ELF Header

The ELF header uses standard structure with NVIDIA-specific overrides:

| Offset | Size | Field | CUDA Value |
|---|---|---|---|
| `0x00` | 4 | `e_ident[EI_MAG0..3]` | `0x7F 'E' 'L' 'F'` (magic `0x464C457F`) |
| `0x04` | 1 | `e_ident[EI_CLASS]` | `0x33` (`'3'`, 32-bit) or `0x41` (`'A'`, 64-bit) |
| `0x05` | 1 | `e_ident[EI_DATA]` | `0x01` (little-endian) |
| `0x06` | 1 | `e_ident[EI_VERSION]` | `0x01` (`EV_CURRENT`) |
| `0x07` | 1 | `e_ident[EI_OSABI]` | CUDA ABI version |
| `0x12` | 2 | `e_machine` | `0x00BE` (`EM_CUDA` = 190) |
| `0x24` | 4 | `e_flags` | SM version bits `[7:0]` + CUDA control flags |

Standard ELF uses `ELFCLASS32` = 1 and `ELFCLASS64` = 2. CUDA cubins use non-standard values `'3'` (`0x33`) and `'A'` (`0x41`), which the CUDA driver recognizes as the cubin signature. Any tool using standard libelf will reject these as invalid, which is one reason ptxas uses a custom emitter.

The `e_flags` field packs the SM architecture version in the low byte (e.g., 100 for `sm_100`) along with flags for relocatable vs. executable mode and address size. The mask `0x7FFFBFFF` (clears bits 14 and 31) is applied during finalization to strip internal control flags that must not appear in the output cubin.

## Section Generation

Each kernel/function produces a set of ELF sections. For a program with N entry functions and M device functions, the CUBIN contains on the order of 4*(N+M) sections minimum. The section creator `sub_1CB3570` (44 call sites) handles the generic case; `sub_1CB42D0` is specialized for `.text.<funcname>` code sections.

### Per-Kernel Sections

For each kernel entry `my_kernel`, the output pipeline generates:

| Section | Type | Content |
|---|---|---|
| `.text.my_kernel` | `SHT_PROGBITS` | SASS instruction bytes (SHF_ALLOC \| SHF_EXECINSTR) |
| `.rela.text.my_kernel` | `SHT_RELA` | Relocation entries for the code section |
| `.nv.info.my_kernel` | `SHT_CUDA_INFO` (`0x70000064`) | EIATTR metadata (register count, barriers, stack sizes, etc.) |
| `.nv.constant0.my_kernel` | `SHT_PROGBITS` | Constant bank 0 data (kernel parameters + literal constants) |

Additional per-kernel sections are generated as needed:

| Section | Condition |
|---|---|
| `.nv.shared.my_kernel` | Kernel uses shared memory |
| `.nv.local.my_kernel` | Kernel uses local (spill) memory |
| `.nv.global.init` | Program uses initialized global variables |
| `.nv.callgraph` | Relocatable object mode (`-c`) |
| `.nv.prototype` | Prototype information for cross-module linking |

### Global Sections

| Section | Purpose |
|---|---|
| `.nv.info` | Global EIATTR attributes (not per-kernel) |
| `.nv.constant0` | Merged constant bank (whole-program mode) |
| `.nv.reservedSmem` | Reserved shared memory for runtime (tmem allocation, mbarrier parity) |
| `.nv.metadata` | Module-level metadata |
| `.nv.compat` | Forward-compatibility attributes |
| `.note.nv.cuver` | CUDA version note |
| `.nv.uft` | Unified Function Table (indirect call support) |
| `.nv.udt` | Unified Data Table |
| `.nv.uft.entry` | UFT entry point table |
| `.nv.udt.entry` | UDT entry point table |
| `.nv.rel.action` | Relocation action table |
| `.nv.resolvedrela` | Resolved relocations (post-linking) |
| `.nv.host` | Host-side interop data |

### Constant Banks

CUDA supports up to 18 numbered constant banks (0--17) plus 6 named banks:

| Bank | Name | Purpose |
|---|---|---|
| 0 | `.nv.constant0` | Kernel parameters + compiler constants (per-entry) |
| 1--17 | `.nv.constant1`--`.nv.constant17` | User-declared `__constant__` variables |
| -- | `.nv.constant.entry_params` | Entry point parameter block |
| -- | `.nv.constant.entry_image_header_indices` | Texture/surface header index table |
| -- | `.nv.constant.driver` | Driver-injected constants |
| -- | `.nv.constant.optimizer` | Optimizer-generated constants (OCG) |
| -- | `.nv.constant.user` | User-specified constants |
| -- | `.nv.constant.pic` | Position-independent code constants |
| -- | `.nv.constant.tools_data` | Tools/debugger-injected data |

### Section Ordering

During finalization, sections are sorted into 8 priority buckets that determine their order in the output ELF:

| Bucket | Contents |
|---|---|
| 0 (highest) | ELF header pseudo-section, `.shstrtab` |
| 1 | `.strtab`, `.symtab`, `.symtab_shndx` |
| 2 | `.note.nv.tkinfo`, `.note.nv.cuinfo` |
| 3 | `.text.<funcname>` (code sections) |
| 4 | `.nv.constant0.*`, `.nv.shared.*`, `.nv.local.*` (data sections) |
| 5 | `.rela.*`, `.rel.*` (relocation sections) |
| 6 | `.nv.info.*` (EIATTR metadata sections) |
| 7 (lowest) | `.debug_*`, `.nv.merc.*` (debug and Mercury metadata) |

Section file offsets are assigned by `sub_1C9DC60`, walking the sorted list with alignment padding:

```c
uint64_t offset = elf_header_size;
for (int i = 0; i < section_count; i++) {
    section_t* sec = sorted_sections[i];
    if (sec->sh_addralign > 1)
        offset = (offset + sec->sh_addralign - 1) & ~(sec->sh_addralign - 1);
    sec->sh_offset = offset;
    offset += sec->sh_size;
}
```

Two section types receive special treatment during layout: `.nv.constant0` (address assigned by the OCG constant bank allocator, not the layout calculator) and `.nv.reservedSmem` (address assigned by the shared memory master allocator `sub_1CABD60`).

## EIATTR Metadata

Each kernel's `.nv.info.<funcname>` section contains a sequence of EIATTR (Entry Information Attribute) records. These encode per-kernel metadata that the CUDA driver reads at launch time to configure the hardware correctly. The EIATTR builder is `sub_1CC9800` (14,764 B native / 86 KB decomp, 51 callees) -- one of the largest functions in the output pipeline.

### EIATTR Encoding

Each EIATTR record is a TLV (type-length-value) structure:

```text
+--------+--------+------------------+
| type   | length | value            |
| 2 bytes| 2 bytes| length bytes     |
+--------+--------+------------------+
```

The type field is a 16-bit EIATTR code. The length field specifies the payload size. The value is type-dependent (scalar, array, or structured).

### EIATTR Catalog

ptxas v13.0.88 defines 98 EIATTR codes. The critical ones that every cubin emitter must produce:

| EIATTR | Purpose | Encoding |
|---|---|---|
| `EIATTR_REGCOUNT` | Register count for this kernel | 4-byte LE integer |
| `EIATTR_NUM_BARRIERS` | Hardware barrier count (0--16) | 4-byte LE integer |
| `EIATTR_FRAME_SIZE` | Per-thread stack frame size in bytes | 4-byte LE integer |
| `EIATTR_MIN_STACK_SIZE` | Minimum call stack size | 4-byte LE integer |
| `EIATTR_MAX_STACK_SIZE` | Maximum call stack size (recursive) | 4-byte LE integer |
| `EIATTR_CRS_STACK_SIZE` | Call/Return/Sync stack size | 4-byte LE integer |
| `EIATTR_EXIT_INSTR_OFFSETS` | Byte offsets of EXIT instructions in `.text` | Array of 4-byte offsets |
| `EIATTR_S2RCTAID_INSTR_OFFSETS` | Byte offsets of S2R SR_CTAID.* instructions | Array of 4-byte offsets |
| `EIATTR_CTAIDZ_USED` | Kernel reads CTA ID Z dimension | Flag (0-byte payload) |
| `EIATTR_REQNTID` | Required thread block dimensions | 3x 4-byte integers (X, Y, Z) |
| `EIATTR_MAX_THREADS` | Maximum threads per block | 4-byte LE integer |
| `EIATTR_PARAM_CBANK` | Constant bank for kernel parameters | 4-byte bank index + offset |
| `EIATTR_CBANK_PARAM_SIZE` | Size of parameter constant bank region | 4-byte LE integer |
| `EIATTR_KPARAM_INFO` | Per-parameter ordinal/offset/size/alignment | Structured (V1) |
| `EIATTR_KPARAM_INFO_V2` | Per-parameter info, extended format | Structured (V2) |
| `EIATTR_MAXREG_COUNT` | Maximum register count directive | 4-byte LE integer |
| `EIATTR_EXTERNS` | List of external symbol references | Array of symbol indices |

Additional EIATTR codes for textures/surfaces, barriers, cooperative groups, tensor cores, and hardware workarounds:

| EIATTR | Purpose |
|---|---|
| `EIATTR_IMAGE_SLOT` | Texture/surface image binding slot |
| `EIATTR_SAMPLER_INIT` | Sampler initialization data |
| `EIATTR_TEXID_SAMPID_MAP` | Texture-to-sampler mapping |
| `EIATTR_BINDLESS_IMAGE_OFFSETS` | Bindless texture/surface offset table |
| `EIATTR_SYNC_STACK` | Synchronization stack requirements |
| `EIATTR_COOP_GROUP_MASK_REGIDS` | Cooperative group register IDs |
| `EIATTR_NUM_MBARRIERS` | Number of mbarrier objects used |
| `EIATTR_MBARRIER_INSTR_OFFSETS` | mbarrier instruction locations |
| `EIATTR_WMMA_USED` | Kernel uses WMMA (Tensor Core) instructions |
| `EIATTR_TCGEN05_1CTA_USED` | Kernel uses 5th-gen Tensor Core (1-CTA mode) |
| `EIATTR_TCGEN05_2CTA_USED` | Kernel uses 5th-gen Tensor Core (2-CTA mode) |
| `EIATTR_SPARSE_MMA_MASK` | Structured sparsity mask for MMA |
| `EIATTR_CTA_PER_CLUSTER` | CTAs per cluster (SM 90+) |
| `EIATTR_EXPLICIT_CLUSTER` | Kernel requires explicit cluster launch |
| `EIATTR_MAX_CLUSTER_RANK` | Maximum cluster rank |
| `EIATTR_REG_RECONFIG` | Register reconfiguration (setmaxreg) |
| `EIATTR_SAM_REGION_STACK_SIZE` | SAM (Shared Address Mode) region stack |
| `EIATTR_RESERVED_SMEM_USED` | Kernel uses reserved shared memory |
| `EIATTR_RESERVED_SMEM_0_SIZE` | Size of reserved shared memory region 0 |
| `EIATTR_SW1850030_WAR` | Hardware workaround (bug SW-1850030) |
| `EIATTR_SW2393858_WAR` | Hardware workaround (bug SW-2393858) |
| `EIATTR_SW2861232_WAR` | Hardware workaround (bug SW-2861232) |
| `EIATTR_CUDA_API_VERSION` | Required CUDA API version |
| `EIATTR_MERCURY_ISA_VERSION` | Mercury ISA version for capmerc binaries |
| `EIATTR_MERCURY_FINALIZER_OPTIONS` | Finalizer tuning for capmerc |
| `EIATTR_SYSCALL_OFFSETS` | Byte offsets of syscall instructions |
| `EIATTR_INSTR_REG_MAP` | Instruction-to-register allocation map (debug) |
| `EIATTR_STATISTICS` | Per-kernel compilation statistics |
| `EIATTR_PERF_STATISTICS` | Performance statistics |

### Barrier and Register Count Propagation

The EIATTR builder runs a cross-function propagation pass via `sub_1CC8950` (2,634 bytes). When a device function uses barriers or a high register count, these requirements must propagate upward to the entry kernel that calls it:

```text
"regcount %d for %s propagated to entry %s"
"Creating new EIATTR_NUM_BARRIERS and moving barcount %d
    from section flags of %s to nvinfo for entry symbol %s"
"Propagating higher barcount %d to the section flags
    of %s of entry symbol %s"
```

This ensures that the CUDA driver allocates sufficient barriers and registers for the entry kernel, accounting for all callees.

## Relocation Processing

The relocation system handles symbol resolution for branch targets, constant bank references, function descriptors, texture/surface bindings, and address computations. The master relocation resolver is `sub_1CD48C0` (4,184 B native / 20 KB decomp). ptxas defines 117 CUDA-specific relocation types (`R_CUDA_NONE` through `R_CUDA_NONE_LAST`).

### Relocation Categories

| Category | Types | Purpose |
|---|---|---|
| Absolute address | `R_CUDA_32`, `R_CUDA_64`, `R_CUDA_ABS*` | Global memory addresses |
| Global address | `R_CUDA_G32`, `R_CUDA_G64`, `R_CUDA_G8_*` | Global-space addresses |
| PC-relative | `R_CUDA_PCREL_IMM24_26`, `R_CUDA_PCREL_IMM24_23` | Branch/call targets |
| Constant field | `R_CUDA_CONST_FIELD19_*`, `R_CUDA_CONST_FIELD21_*`, `R_CUDA_CONST_FIELD22_*` | Constant bank references |
| Function descriptor | `R_CUDA_FUNC_DESC*` | Indirect function call targets |
| Texture/surface | `R_CUDA_TEX_HEADER_INDEX`, `R_CUDA_SAMP_HEADER_INDEX`, `R_CUDA_SURF_*` | Texture/surface binding |
| Bindless | `R_CUDA_BINDLESSOFF*`, `R_CUDA_TEX_BINDLESSOFF*` | Bindless texture/surface offsets |
| Sub-byte patching | `R_CUDA_8_0` through `R_CUDA_8_56` | Individual byte within a 64-bit instruction |
| Unified address | `R_CUDA_UNIFIED`, `R_CUDA_UNIFIED_32`, `R_CUDA_UNIFIED_8_*` | Unified address space |
| Instruction-level | `R_CUDA_INSTRUCTION64`, `R_CUDA_INSTRUCTION128` | Whole-instruction replacement |
| Yield/NOP | `R_CUDA_YIELD_OPCODE9_0`, `R_CUDA_YIELD_CLEAR_PRED4_87` | YIELD-to-NOP patching |
| Cleanup | `R_CUDA_UNUSED_CLEAR32`, `R_CUDA_UNUSED_CLEAR64` | Zero out unused instruction fields |

### Resolution Logic

The resolver performs these operations for each relocation entry:

1. **Alias resolution** -- redirect relocations from alias symbols to their targets (`"change alias reloc %s to %s"`)
2. **Dead function filtering** -- skip relocations on eliminated functions (`"ignore reloc on dead func %s"`)
3. **UFT/UDT pseudo-relocation** -- handle `__UFT_OFFSET`, `__UFT_CANONICAL`, `__UDT_OFFSET`, `__UDT_CANONICAL` synthetic symbols
4. **PC-relative validation** -- ensure branch targets are in the same section (`"PC relative branch address should be in the same section"`)
5. **YIELD-to-NOP conversion** -- convert YIELD instructions to NOP when forward progress requirements prevent yielding
6. **Unified reloc replacement** -- convert type 103 (unified) to type 1 (absolute) for final resolution
7. **Address computation** -- compute final patched value from symbol address + addend

Output relocation sections (`.nv.resolvedrela`) are written by `sub_1CD5920`.

## Debug Information

When `--device-debug` or `--generate-line-info` is active, ptxas generates DWARF debug sections. The debug subsystem spans `0x1CBF`--`0x1CC9` and includes parsers, emitters, and dumpers for the standard DWARF sections plus NVIDIA-specific debug extensions.

### Debug Sections

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
| `.nv_debug_ptx_txt` | Embedded PTX source text |
| `.nv_debug_line_sass` | SASS-level line number mapping |
| `.nv_debug_info_reg_sass` | Register allocation debug info |
| `.nv_debug_info_reg_type` | Register type information |

Key debug infrastructure functions:

| Function | Purpose |
|---|---|
| `sub_1CBF820` | DWARF form name table (`DW_FORM_addr`, `DW_FORM_data4`, etc.) |
| `sub_1CBF9B0` | DWARF attribute name table (`DW_AT_producer`, `DW_AT_comp_dir`, etc.) |
| `sub_1CC0850` | `.debug_abbrev` parser/emitter (18 KB decompiled) |
| `sub_1CC4A40` | `.debug_info` DIE tree walker (28 KB decompiled) |
| `sub_1CC34E0` | DWARF location expression decoder (`DW_OP_*` operations) |
| `sub_1CC24C0` | `.debug_info` emission pass (18 KB decompiled) |
| `sub_1CC5EB0` | Compilation unit header parser |
| `sub_1C9D1F0` | Debug section classifier/mapper (16 KB decompiled) |

The `--suppress-debug-info` option (`sub_432A00`) disables debug section generation even when debug flags are present.

## Capsule Mercury Output Path

For SM 100+ targets (Blackwell, Jetson Thor, consumer RTX 50-series), the default output mode is Capsule Mercury. In this mode, the CUBIN ELF contains two layers of content: standard CUBIN sections and a parallel set of `.nv.merc.*` sections carrying Mercury-encoded instruction streams plus all metadata needed for deferred finalization.

### Mercury-Specific Sections

| Section | Purpose |
|---|---|
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
| `.nv.merc.symtab_shndx` | Extended section index table (merc copy) |
| `.nv.merc.nv.shared.reserved` | Shared memory reservation metadata |
| `.nv.merc.rela<secname>` | Per-section relocation tables |

### Capsule Mercury Construction

The capmerc path is integrated into the master ELF emitter. The sequence:

1. `sub_1C9B110` (23 KB decompiled) creates the `.nv.merc.*` section namespace
2. `sub_1CA2E40` (18 KB decompiled) clones constant/global/shared/local sections into the merc namespace, creating `.nv.merc.rela` relocation sections
3. `sub_1C9C300` (24 KB decompiled) processes `.nv.capmerc<funcname>` sections. Constructs a 328-byte capsule descriptor per function containing: Mercury-encoded instruction stream, relocation metadata, KNOBS configuration snapshot, and function-level metadata (register counts, barriers, shared memory usage)
4. `sub_1CA3A90` (45 KB decompiled) merges sections that have both merc and non-merc copies
5. `sub_1C99BB0` (25 KB decompiled) remaps section indices after merc section insertion

### Off-Target Finalization

The cubin entry `sub_612DE0` implements a "fastpath optimization" for off-target finalization:

```text
"[Finalizer] fastpath optimization applied for off-target %u -> %u finalization"
```

When a capmerc binary compiled for SM X is finalized for SM Y (within the same family), the fastpath patches the Mercury instruction stream directly without full recompilation. The compatibility checker `sub_60F290` determines whether fastpath is safe based on instruction set compatibility, register file layout, and memory model.

### Self-Check

The `--self-check` option performs roundtrip verification: generate capmerc, reconstitute SASS from the capsule, and compare section-by-section. The verifier uses a Flex SASS lexer (`sub_720F00`, 64 KB) and a comparator (`sub_729540`, 35 KB). Error string: `"Failure of '%s' section in self-check for capsule mercury"`.

## Multi-Kernel Output

A typical CUDA program compiles multiple kernels and device functions into a single CUBIN. The output pipeline handles this through per-function section isolation, combined with cross-function analysis for call graph construction, barrier propagation, and dead code elimination.

### Per-Function Section Layout

Each entry function and each device function gets its own `.text` section (the `-ffunction-sections` pattern). This enables:

- **Function-level dead code elimination** -- `sub_1CBC090` removes `.text`, `.rela.text`, `.nv.info`, and `.nv.constant0` sections for unreachable functions
- **Linker granularity** -- `nvlink` can select individual functions from relocatable objects
- **Driver loading** -- the CUDA runtime can load individual kernels by name

### Call Graph Construction

The call graph builder (`sub_1CBE1B0`) emits a `.nv.callgraph` section that encodes inter-function call edges. This section is present only in relocatable object mode (`-c`). The recursion detector (`sub_1CBB920`) performs a DFS traversal with manual 9-level unrolling, emitting `"recursion at function %d"` for each cycle found.

Dead functions are eliminated by `sub_1CBC090`:

```text
"dead function %d(%s)"
"removed un-used section %s (%d)"   (x8 -- once per section type)
"function %d(%s) has address taken but no call to it"
```

### Memory Allocation Across Kernels

The master section allocator `sub_1CABD60` (11,856 B native / 66 KB decomp, 69 callees) assigns addresses to all memory-space sections across all kernels. It runs a multi-pass algorithm:

1. **Global shared allocation** -- shared variables visible to multiple kernels
2. **Per-entry shared memory** -- shared variables private to each kernel
3. **Extern shared handling** -- dynamically-sized shared memory (`extern __shared__`)
4. **Reserved shared memory** -- runtime reservations (`.nv.reservedSmem.begin`, `.nv.reservedSmem.cap`, `.nv.reservedSmem.offset0`, `.nv.reservedSmem.offset1`)
5. **Local memory** -- per-thread spill storage
6. **Constant bank merging** -- merges constant bank data across kernels, with deduplication (`sub_1CA6890`: `"found duplicate value 0x%x, alias %s to %s"`)

The shared memory allocator `sub_1CA92F0` (2,804 bytes) builds an interference graph for shared objects and performs group allocation for non-overlapping variables.

## SHN_XINDEX Overflow

Large CUDA programs can exceed the ELF 65,280-section limit (`SHN_LORESERVE` = 0xFF00). Each kernel generates at minimum 4 sections (`.text`, `.rela.text`, `.nv.info`, `.nv.constant0`), so a program with 16,000+ kernels triggers the overflow mechanism:

1. `e_shnum` = 0 in the ELF header (signals overflow)
2. Section header `[0].sh_size` = real section count
3. `e_shstrndx` = `SHN_XINDEX` (0xFFFF)
4. Section header `[0].sh_link` = real `.shstrtab` index
5. Symbol `st_shndx` = `SHN_XINDEX` when real index >= 0xFF00
6. `.symtab_shndx` entries hold the actual section indices

This is standard ELF overflow handling, and it is production-critical -- `sub_1CB68D0` checks for it with `"overflow number of sections %d"`.

## Key Functions

| Address | Size (native) | Size (decomp) | Purpose |
|---|---|---|---|
| `sub_612DE0` | ~12 KB | 47 KB | Cubin generation entry point |
| `sub_1C9F280` | 15,263 B | 97 KB | Master ELF emitter |
| `sub_1CC9800` | 14,764 B | 86 KB | nvinfo/EIATTR section builder |
| `sub_1CABD60` | 11,856 B | 66 KB | Master section allocator (shared/const/local) |
| `sub_1CB68D0` | 9,578 B | 49 KB | Symbol table builder (.symtab) |
| `sub_1CA3A90` | 6,289 B | 45 KB | Section merger (merc + non-merc) |
| `sub_1C9DC60` | 5,663 B | 29 KB | Section layout calculator |
| `sub_1C99BB0` | 4,900 B | 25 KB | Section index remap (.symtab_shndx) |
| `sub_1C9C300` | 3,816 B | 24 KB | Capsule Mercury section processor |
| `sub_1C9B110` | 4,585 B | 23 KB | Mercury capsule builder |
| `sub_1CD48C0` | 4,184 B | 20 KB | Master relocation resolver |
| `sub_1CBC090` | 2,870 B | 20 KB | Dead function eliminator |
| `sub_1CA2E40` | 3,152 B | 18 KB | Mercury section cloner |
| `sub_1CA92F0` | 2,804 B | 16 KB | Shared memory interference graph |
| `sub_1C9D1F0` | 2,667 B | 16 KB | Debug section classifier |
| `sub_1CA6890` | 2,286 B | 15 KB | Constant bank deduplication |
| `sub_1CC8950` | 2,634 B | 15 KB | Barrier/register count propagator |
| `sub_1CBB920` | ~2,000 B | 14 KB | Recursion detector (DFS) |
| `sub_1CB91C0` | 2,668 B | 13 KB | ELF structure dumper (debug) |
| `sub_1CB53A0` | 3,480 B | 13 KB | ELFW constructor |
| `sub_1CAB300` | 2,157 B | 12 KB | Bindless texture/surface handler |
| `sub_1CD5920` | 1,985 B | 11 KB | Relocation writer (.nv.resolvedrela) |
| `sub_1CD13A0` | 2,541 B | 11 KB | File serializer (final disk write) |
| `sub_1CBE1B0` | 1,992 B | 10 KB | .nv.callgraph section builder |
| `sub_1CD22E0` | 1,979 B | 10 KB | UFT manager |
| `sub_1CB3570` | 1,963 B | 10 KB | Section creator (44 call sites) |
| `sub_1C98C60` | 1,755 B | 9 KB | Mercury debug section classifier |
| `sub_1CB2CA0` | 2,038 B | 8 KB | Symbol fixup (post-deletion) |
| `sub_1CC7FB0` | -- | -- | .nv.info section name formatter |
| `sub_1CB9FF0` | -- | -- | Section count accessor |
| `sub_1CB9C40` | -- | -- | Get section by index |

## Cross-References

- [Custom ELF Emitter](../output/elf-emitter.md) -- deep dive into ELFW object, header construction, section management, file serialization
- [Section Catalog & EIATTR](../output/sections.md) -- complete inventory of section types and EIATTR attribute encoding
- [Relocations & Symbols](../output/relocations.md) -- relocation resolution, UFT/UDT management, symbol table details
- [Debug Information](../output/debug-info.md) -- DWARF generation and `.debug_*` section handling
- [Capsule Mercury & Finalization](../codegen/capmerc.md) -- capmerc packaging format, off-target finalization, self-check
- [Mercury Encoder](../codegen/mercury.md) -- Mercury instruction encoding (phases 117--122) that feeds the ELF emitter
- [SASS Code Generation](codegen.md) -- the upstream per-kernel compilation that produces SASS bytes
- [Pipeline Overview](overview.md) -- where the ELF phase fits in the full PTX-to-SASS flow
