# Constant Banks (.nv.constant)

CUDA GPU architectures provide a hierarchy of constant memory banks accessible through dedicated hardware. In the CUDA device ELF format, each constant bank is represented as a distinct section with a naming convention rooted in the `.nv.constant` prefix. The nvlink device linker manages 18 numbered banks (0--17), plus 7 named specialized banks, each serving a distinct role in the GPU execution model. This page covers the bank numbering scheme, ELF section type encoding, per-kernel section splitting, specialized bank assignments, the name-to-index mapping table, the constant deduplication and optimization pipeline, relocation resolution, and the hardware size limits that constrain constant bank usage.

| | |
|---|---|
| **Section prefix** | `.nv.constant` |
| **Numbered banks** | `.nv.constant0` through `.nv.constant17` |
| **ELF type base** | `SHT_CUDA_CONSTANT0` = `0x70000064` |
| **ELF type range** | `0x70000064` (bank 0) through `0x70000075` (bank 17) |
| **Generic base type** | `SHT_CUDA_CONSTANT` = `0x70000006` |
| **Name table address** | `0x1D3A8E0` (18 + 7 pointer entries) |
| **Merge function** | `sub_438640` at `0x438640` (4,043 bytes) |
| **Overlap merge** | `sub_4343C0` at `0x4343C0` (11,838 bytes) |
| **Dedup engine** | `sub_4339A0` at `0x4339A0` (13,199 bytes) |
| **Typical bank size limit** | 64 KB per bank (architecture-dependent, vtable offset +32) |

## Bank Numbering

CUDA constant memory is organized into 18 hardware-addressable banks, numbered 0 through 17. Each bank is an independent address space from the hardware perspective, accessed through distinct constant cache ports. The ISA references banks as `c[N][offset]` (e.g., `c[0][0x140]` reads 4 bytes at offset 0x140 from bank 0).

In the ELF representation, each bank gets its own section and section type:

| Bank | Section name | ELF type | Hex type | Role |
|---|---|---|---|---|
| 0 | `.nv.constant0` | `SHT_CUDA_CONSTANT0` | `0x70000064` | User `__constant__` variables (default) |
| 1 | `.nv.constant1` | `SHT_CUDA_CONSTANT1` | `0x70000065` | Reserved |
| 2 | `.nv.constant2` | `SHT_CUDA_CONSTANT2` | `0x70000066` | Compiler-generated (OCG) constants |
| 3 | `.nv.constant3` | `SHT_CUDA_CONSTANT3` | `0x70000067` | Bindless texture descriptors |
| 4 | `.nv.constant4` | `SHT_CUDA_CONSTANT4` | `0x70000068` | Reserved |
| 5 | `.nv.constant5` | `SHT_CUDA_CONSTANT5` | `0x70000069` | Reserved |
| 6 | `.nv.constant6` | `SHT_CUDA_CONSTANT6` | `0x7000006A` | Reserved |
| 7 | `.nv.constant7` | `SHT_CUDA_CONSTANT7` | `0x7000006B` | Reserved |
| 8 | `.nv.constant8` | `SHT_CUDA_CONSTANT8` | `0x7000006C` | Reserved |
| 9 | `.nv.constant9` | `SHT_CUDA_CONSTANT9` | `0x7000006D` | Reserved |
| 10 | `.nv.constant10` | `SHT_CUDA_CONSTANT10` | `0x7000006E` | Reserved |
| 11 | `.nv.constant11` | `SHT_CUDA_CONSTANT11` | `0x7000006F` | Reserved |
| 12 | `.nv.constant12` | `SHT_CUDA_CONSTANT12` | `0x70000070` | Reserved |
| 13 | `.nv.constant13` | `SHT_CUDA_CONSTANT13` | `0x70000071` | Reserved |
| 14 | `.nv.constant14` | `SHT_CUDA_CONSTANT14` | `0x70000072` | Reserved |
| 15 | `.nv.constant15` | `SHT_CUDA_CONSTANT15` | `0x70000073` | Reserved |
| 16 | `.nv.constant16` | `SHT_CUDA_CONSTANT16` | `0x70000074` | Reserved |
| 17 | `.nv.constant17` | `SHT_CUDA_CONSTANT17` | `0x70000075` | Reserved |

The type encoding formula is:

```
SHT_CUDA_CONSTANT0 + bank_number = 0x70000064 + N
```

The bank number is extracted from the section name during the merge phase by parsing the numeric suffix:

```c
bank_number = strtol(section_name + 12, NULL, 10);
// ".nv.constant0"  -> offset 12 = "0"  -> bank 0
// ".nv.constant17" -> offset 12 = "17" -> bank 17
```

There is also a generic base type `SHT_CUDA_CONSTANT` (`0x70000006`) used for the unindexed `.nv.constant` prefix when the bank number has not yet been determined or when referring to the constant memory space generically.

## Name-to-Index Mapping Table

nvlink maintains a static pointer table at address `0x1D3A8E0` that maps bank indices to their section name strings. The table contains 18 entries for the numbered banks at 8-byte stride, followed by entries for the specialized banks:

```
Address      Index  String pointer target
---------    -----  ----------------------
0x1D3A8E0    [0]    -> "0x1D3A4C0: .nv.constant0"
0x1D3A8E8    [1]    -> "0x1D3A4CE: .nv.constant1"
0x1D3A8F0    [2]    -> "0x1D3A4DC: .nv.constant2"
0x1D3A8F8    [3]    -> "0x1D3A4EA: .nv.constant3"
0x1D3A900    [4]    -> "0x1D3A4F8: .nv.constant4"
0x1D3A908    [5]    -> "0x1D3A506: .nv.constant5"
0x1D3A910    [6]    -> "0x1D3A514: .nv.constant6"
0x1D3A918    [7]    -> "0x1D3A522: .nv.constant7"
0x1D3A920    [8]    -> "0x1D3A530: .nv.constant8"
0x1D3A928    [9]    -> "0x1D3A53E: .nv.constant9"
0x1D3A930    [10]   -> "0x1D3A54C: .nv.constant10"
0x1D3A938    [11]   -> "0x1D3A55B: .nv.constant11"
0x1D3A940    [12]   -> "0x1D3A56A: .nv.constant12"
0x1D3A948    [13]   -> "0x1D3A579: .nv.constant13"
0x1D3A950    [14]   -> "0x1D3A588: .nv.constant14"
0x1D3A958    [15]   -> "0x1D3A597: .nv.constant15"
0x1D3A960    [16]   -> "0x1D3A5A6: .nv.constant16"
0x1D3A968    [17]   -> "0x1D3A5B5: .nv.constant17"
--- gap (indices 18-19, 16 bytes zero/reserved) ---
0x1D3A980    [20]   -> "0x1D3A5C4: .nv.constant.entry_params"
0x1D3A988    [21]   -> "0x1D3A880: .nv.constant.entry_image_header_indices"
0x1D3A990    [22]   -> "0x1D3A5DE: .nv.constant.driver"
0x1D3A998    [23]   -> "0x1D3A5F2: .nv.constant.optimizer"
0x1D3A9A0    [24]   -> "0x1D3A609: .nv.constant.user"
0x1D3A9A8    [25]   -> "0x1D3A61B: .nv.constant.pic"
0x1D3A9B0    [26]   -> "0x1D3A62C: .nv.constant.tools_data"
```

The table is followed immediately by system function name pointers (vprintf at `0x1D3A9C0`, malloc at `0x1D3A9C8`, etc.) used for device-side system call resolution. This suggests the entire region `0x1D3A8E0`--`0x1D3AA20` is a combined constant-bank-and-syscall lookup structure, with the constant bank names occupying the first 27 slots.

## Specialized Banks

Beyond the 18 numbered banks, nvlink recognizes 7 named specialized banks. These do not use the numeric suffix convention; instead they use a dotted-name suffix after `.nv.constant.`:

### `.nv.constant.entry_params` -- Kernel Parameter Bank

Holds the kernel launch parameters (the arguments passed to `<<<...>>>` or `cuLaunchKernel`). The CUDA runtime copies kernel arguments into this bank before launch. The nvinfo attribute `EIATTR_PARAM_CBANK` (`0x235`) specifies which constant bank and offset range contain the parameters, and `EIATTR_CBANK_PARAM_SIZE` (`0x185`) records the total parameter size. The compiler references this bank as `sw-kernel-params-bank` internally.

### `.nv.constant.driver` -- Driver-Managed Bank

Reserved for the CUDA driver to inject runtime constants. The driver uses this bank for values that must be available in constant memory but are not known at compile time (e.g., grid dimensions, device properties). The content is populated by the driver at kernel launch time, not by the linker.

### `.nv.constant.optimizer` -- Compiler-Generated Constants

Contains constants materialized by the compiler's optimization passes, distinct from user-declared `__constant__` variables. When the OCG (Object Code Generator) back-end promotes immediates to constant memory loads for better encoding or register pressure, the values land here. This bank is the target of the `__ocg_const` symbol resolution at `sub_1625E40` and the `sw-compiler-bank` memory space reference.

### `.nv.constant.user` -- User `__constant__` Variables

The primary bank for user-declared `__constant__` variables in CUDA C++. When a programmer writes `__constant__ float table[256];`, the data is placed in this bank (or in `.nv.constant0`, depending on compilation mode). The distinction between `.nv.constant.user` and `.nv.constant0` depends on whether the compiler uses named-bank or numbered-bank encoding for the user constant space.

### `.nv.constant.pic` -- Position-Independent Code Tables

Contains the PIC (Position-Independent Code) jump tables and function pointer tables used in relocatable compilation. When indirect function calls need to be resolved at link time, the linker places the indirect function address tables (`__funcAddrTab_c` and `__funcAddrTab_g`) in this bank via `sub_162C8B0`. These tables enable the device-side function pointer mechanism.

### `.nv.constant.tools_data` -- Profiling/Debugging Tool Data

Reserved for NVIDIA profiling and debugging tools (Nsight Compute, Nsight Systems, CUDA-MEMCHECK). Tool instrumentation passes inject metadata and configuration data into this bank. The sanitizer instrumentation at `sub_1CAE000` uses related mechanisms for memcheck callbacks.

### `.nv.constant.entry_image_header_indices` -- Image Header Indices

Contains per-entry indices into the image header array. This bank maps kernel entry points to their positions in the cubin's image header table, enabling the driver to look up per-kernel metadata (register counts, shared memory sizes, etc.) by entry point index.

## Per-Entry Constant Sections

Constant bank sections can be either global (shared across all kernels in the linked cubin) or per-entry (specific to a single kernel entry point). Per-entry sections follow a naming convention that appends the kernel name:

```
<bank_section_name>.<kernel_name>
```

Examples:
- `.nv.constant0.my_kernel` -- bank 0 constants specific to `my_kernel`
- `.nv.constant2.matmul_f32` -- bank 2 (OCG) constants for `matmul_f32`
- `.nv.constant3.tex_sample` -- bank 3 (bindless) descriptors for `tex_sample`

The section name is constructed via `sprintf("%s.%s", bank_name, entry_name)`. The merge function `sub_438640` handles both global and per-entry cases:

```c
// sub_438640 -- merge_constant_bank_data
// Address: 0x438640, 4,043 bytes
//
// a1:  elfw*           -- linker context
// a2:  section*        -- source section
// a3:  uint32_t        -- symbol binding (1=GLOBAL, other=per-entry)
// a4:  uint32_t        -- symbol index
// a5:  uint64_t        -- data offset within source section
// a6:  uint64_t        -- alignment
// n:   uint64_t        -- data size
// s:   void*           -- source data pointer
// a9:  uint32_t        -- constant bank type (0x70000064 + bank_number)
// a10: uint32_t        -- entry function section index (0 for global)
```

For per-entry constants (`a10 != 0`):

1. Construct the composite section name: `sprintf(buf, "%s.%s", bank_name, entry_name)`
2. Look up the section by name in the output ELF
3. If not found, create a new section and register it in the per-entry constant list at `elfw+272`
4. Merge data via the overlap merge function `sub_4343C0`

Validation rules:
- Per-entry data must not have GLOBAL binding: `"entry data cannot be GLOBAL"`
- Per-entry data must have an explicit offset: `"entry data should have offset"`
- The section type must be in the constant bank range: `"bank SHT not CUDA_CONSTANT_?"`

The per-entry list at `elfw+272` is consumed later by the layout phase (Phase 9) to assign final addresses within each bank.

## `.nv.ptx.const0.size`

A special pseudo-section `.nv.ptx.const0.size` appears during the merge phase. It does not hold data; instead, it records the total size of the PTX-level constant bank 0 as declared in the PTX source. During merge, if two input objects declare different `const0.size` values, nvlink diagnoses the conflict. This section is created as a local symbol with `st_shndx == 0` and resolved by `sub_165E960`.

## Constant Access via Relocations

GPU instructions that load from constant memory encode the bank index and byte offset within the instruction word. At link time, the constant bank sections may be relocated (e.g., because multiple TUs contribute constants to the same bank and the linker must assign non-overlapping offsets). nvlink uses the `R_CUDA_CONST_FIELD*` relocation family to patch constant memory offsets into instruction encoding fields:

| Reloc type | Width | Bit pos | Architectures |
|---|---|---|---|
| `R_CUDA_CONST_FIELD19_20` | 19 | 20 | All |
| `R_CUDA_CONST_FIELD19_23` | 19 | 23 | All |
| `R_CUDA_CONST_FIELD19_26` | 19 | 26 | All |
| `R_CUDA_CONST_FIELD19_28` | 19 | 28 | All |
| `R_CUDA_CONST_FIELD19_40` | 19 | 40 | All |
| `R_CUDA_CONST_FIELD21_20` | 21 | 20 | sm_75+ |
| `R_CUDA_CONST_FIELD21_23` | 21 | 23 | sm_75+ |
| `R_CUDA_CONST_FIELD21_26` | 21 | 26 | sm_75+ |
| `R_CUDA_CONST_FIELD21_38` | 21 | 38 | sm_75+ |
| `R_CUDA_CONST_FIELD22_37` | 22 | 37 | sm_75+ |

The naming convention `R_CUDA_CONST_FIELD<W>_<B>` means: extract `W` bits starting at bit position `B` from the instruction word, and write the constant bank offset (byte offset divided by 4, i.e., dword-addressed) into that field.

The 19-bit variants address up to 512 K dwords = 2 MB of byte-addressable constant memory per bank. The 21-bit and 22-bit variants, introduced with sm_75 (Turing), expand the addressable range to 8 MB and 16 MB respectively. In practice, the hardware bank size limit is 64 KB for most architectures, so the larger fields provide headroom for future expansion or for the compiler to encode additional information in the upper bits.

For Mercury (sm_100+) architectures, constant bank relocations are part of the broader `R_CUDA_CONST_FIELD*` family (10 types in the Mercury relocation table) but are resolved through the FNLZR post-link transformation rather than by nvlink directly.

## Overlap Merge and Validation

When multiple translation units contribute data to the same constant bank, nvlink must merge them into a single output section. The overlap merge function `sub_4343C0` (11,838 bytes) handles this for constant sections, mirroring the logic used for global data (`sub_432B10`) and local data (`sub_437E20`):

1. For each symbol in the source section, compute its target offset in the output section.
2. If the target range overlaps with existing data, call `memcmp` to verify the overlapping bytes are identical.
3. If the overlap contains non-identical data, emit a fatal error: `"overlapping non-identical data"`.
4. If the overlap spans beyond expected bounds, emit: `"overlapping data spans too much"`.
5. If the overlap is valid (identical data), the symbol is aliased to the existing copy.

This overlap validation catches the common CUDA programming error of defining the same `__constant__` variable with different initializers in different translation units. Standard host linkers would silently pick one; nvlink detects and rejects the inconsistency.

## OCG Constant Optimization

The OCG (Object Code Generator) produces per-kernel constant sections (typically in bank 2) containing compiler-materialized constants such as promoted immediates, lookup tables, and branch targets. When linking multiple kernels, these per-kernel constant sections can grow large enough to exceed the hardware bank size limit. nvlink provides an automatic deduplication and compaction pass to address this.

The optimization is orchestrated by the layout phase (`sub_439830`) and executed by the dedup engine (`sub_4339A0`, 13,199 bytes). It operates in two modes:

### Phase 9c: Standard Constant Merging

Triggered when the `merge-constants` flag (`elfw+97`) is set. Creates a `TEMP_MERGED_CONSTANTS` temporary section and calls the dedup engine with `copy_all=1`, meaning all constants are copied (even unreferenced ones). This mode deduplicates the standard constant bank (`.nv.constant0`) contents:

```
Verbose: "layout and merge section %s"
         "found duplicate value 0x%x, alias %s to %s"
         "found duplicate 64bit value 0x%llx, alias %s to %s"
```

### Phase 9d: OCG Constant Optimization

Triggered automatically when any OCG constant section exceeds `max_constant_bank_size` (architecture vtable offset +32, typically 64 KB), or when the `--optimize-data-layout` flag is set. Creates a `TEMP_OCG_CONSTANTS` temporary section and calls the dedup engine with `copy_all=0`, enabling dead constant elimination:

1. Build a per-entry set of referenced constants via `sub_43FB70` (symbol reachability check).
2. For each OCG constant section with data, deduplicate by value:
   - 4-byte constants: hash table lookup (256 buckets, shift-xor hash)
   - 8-byte constants: hash table lookup (256 buckets, modular hash)
   - 12--64 byte constants: linked-list walk with `memcmp`
3. Rewrite relocations that target deduplicated constants: `"optimize ocg constant reloc offset from %lld to %lld"`.
4. If the optimized size fits within the bank limit, replace all OCG section contents.
5. If optimization does not help: `"ocg const optimization didn't help so give up"`.

```
Verbose: "optimize OCG constants for %s, old size = %lld"
         "new OCG constant size = %lld"
```

### Dead Constant Elimination

When a kernel is removed by dead code elimination (`sub_44AD40`), its per-entry constant sections are also removed. If the OCG constant section has multiple instances (the parent section has additional copies for other kernels), the pass iterates all sections to find every instance sharing the same parent:

```
Verbose: "dead ocg constant section %s has multiple instances"
         "removed un-used section %s (%d)"
```

During weak function resolution (`sub_45D180`), if a weak function is replaced by a preferred definition, its OCG constants are cleaned up:

```
Verbose: "remove weak ocg constants"
```

## Bank Size Limits

Each constant bank has a hardware-imposed maximum size that varies by architecture. The limit is queried through the architecture vtable at offset +32:

| Architecture | Typical bank 0 limit | Notes |
|---|---|---|
| sm_30--sm_70 | 64 KB | Fixed hardware limit |
| sm_75 (Turing) | 64 KB | Wider relocation fields (21/22-bit) |
| sm_80--sm_90 | 64 KB | Same hardware limit, better encoding |
| sm_100+ (Mercury) | 64 KB | Constant bank handling deferred to FNLZR |

When the merged constant bank exceeds its limit and optimization fails to shrink it below the threshold, the linker emits a diagnostic. The `--no-opt` flag disables all constant optimization, falling back to simple linear layout. The `--optimize-data-layout` flag forces optimization even when sections are within the limit.

The linker tracks per-bank sizes in the verbose output:

```
%lld bytes gmem, %lld bytes cmem[0], %lld bytes cmem[2], ...
```

This verbose dump iterates all 18 banks and prints non-zero sizes, appearing when `--verbose` is active.

## Constant Bank Usage in ptxas

Within the embedded ptxas assembler, constant bank references are handled as a distinct operand type. The instruction selection infrastructure (`sub_11194A0`) tests whether an operand is a constant bank reference, and the memory space classifier (`sub_1624FF0`) categorizes addresses as `c[N]` (numbered bank) or by named bank (`sw-kernel-params-bank`, `sw-bindless-tex-surf-table-bank`, `sw-compiler-bank`).

The IR node type 12 (`ConstBuf`) represents a constant buffer reference in the ptxas internal representation, encoding both the bank index and the byte offset. During code generation, these references are lowered to hardware constant cache load instructions with the appropriate bank encoding in the instruction word.

The setup function `sub_162C8B0` initializes constant bank sections using the `%s%d.%s` and `.nv.constant` format patterns, creating the ELF sections that will hold the constant data for each kernel entry point.

## Relocation Sections for Constant Banks

When linking in non-relocatable mode (`ctx+16 != 1`) with DCE enabled (`ctx+83` set), the merge phase creates relocation sections for constant bank data. For each constant bank section with type in the range `0x70000064`--`0x7000007E` or equal to `0x70000006`, a `.rela<name>` (or `.rel<name>`) section of type `SHT_RELA` (4) or `SHT_REL` (9) is created, linked back to the parent constant section.

These relocation sections hold the `R_CUDA_CONST_FIELD*` entries that the relocation engine (`sub_469D60`) processes during the relocate phase. The relocation engine resolves symbol addresses, computes the final byte offset within the merged bank, converts to dword offset, and patches the instruction encoding at the specified bit position and width.

## Key Functions

| Address | Name | Size | Description |
|---|---|---|---|
| `0x438640` | `merge_constant_bank_data` | 4,043 B | Merge data into constant bank (global or per-entry) |
| `0x4343C0` | `merge_overlapping_constant_data` | 11,838 B | Validate and merge overlapping constant bank data |
| `0x4339A0` | `constant_dedup` | 13,199 B | Deduplicate constant values across TUs |
| `0x4325A0` | `section_layout_engine` | ~1.4 KB | Sort symbols by alignment, assign offsets |
| `0x433760` | `section_data_copy` | ~600 B | Copy data node into section with aligned offset |
| `0x433870` | `dedup_memcmp` | ~300 B | Large-value dedup via linked-list memcmp |
| `0x43FB70` | `symbol_reachability` | ~150 B | Check if constant is referenced by live entry |
| `0x1CEC7E0` | `ELF_EmitConstantSection` | 3,742 B | Emit `.nv.constant0` section in ELF output |
| `0x1CEC660` | `constant_section_name_lookup` | ~200 B | Look up constant section name from `.nv.constant` prefix |
| `0x162C8B0` | `setup_constant_space` | ~3 KB | Initialize constant memory banks in ptxas |
| `0x165E960` | `resolve_const0_size` | ~500 B | Resolve `.nv.ptx.const0.size` pseudo-section |

## Diagnostic Strings

| String | Source | Meaning |
|---|---|---|
| `"bank SHT not CUDA_CONSTANT_?"` | `sub_438640` | Section type not in constant bank range |
| `"entry data cannot be GLOBAL"` | `sub_438640` | Per-entry constant has wrong binding |
| `"entry data should have offset"` | `sub_438640` | Per-entry constant missing offset |
| `"overlapping non-identical data"` | `sub_4343C0` | Same constant defined differently across TUs |
| `"overlapping data spans too much"` | `sub_4343C0` | Overlap exceeds expected bounds |
| `"found duplicate value 0x%x, alias %s to %s"` | `sub_4339A0` | 32-bit constant dedup hit |
| `"found duplicate 64bit value 0x%llx, alias %s to %s"` | `sub_4339A0` | 64-bit constant dedup hit |
| `"optimize ocg constant reloc offset from %lld to %lld"` | `sub_4339A0` | Relocation rewrite after dedup |
| `"dead ocg constant section %s has multiple instances"` | `sub_44AD40` | Multi-instance OCG cleanup |
| `"remove weak ocg constants"` | `sub_45D180` | Weak function OCG cleanup |
| `"ocg const optimization didn't help so give up"` | `sub_439830` | Dedup failed to meet bank limit |
| `"%lld bytes cmem[%d]"` | `sub_43D2A0` | Per-bank constant memory verbose |
| `"Constant register limit exceeded"` | `sub_16B4620` | Bank usage exceeds hardware limit |

## Related Pages

- [Section Merging](../linker/section-merging.md) -- full merge phase mechanics, per-entry section naming
- [Data Layout Optimization](../linker/data-layout-opt.md) -- OCG constant dedup engine in detail
- [R_CUDA Relocations](../linker/r-cuda-relocations.md) -- complete R_CUDA_CONST_FIELD* catalog
- [Bindless Relocations](../linker/bindless-relocations.md) -- constant bank 3 for bindless descriptors
- [Dead Code Elimination](../linker/dead-code-elimination.md) -- OCG constant section removal
- [Layout Phase](../pipeline/layout.md) -- Phase 9 constant bank layout orchestration
- [NVIDIA Sections](nvidia-sections.md) -- full CUDA section type catalog
