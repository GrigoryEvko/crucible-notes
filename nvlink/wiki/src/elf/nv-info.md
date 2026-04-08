# .nv.info Metadata

The `.nv.info` section is NVIDIA's proprietary ELF metadata format that encodes per-kernel resource requirements and compilation artifacts. Every CUDA kernel carries a `.nv.info` section (or a per-function variant) that tells the GPU driver how many registers to allocate, how much shared memory to reserve, what barriers the kernel uses, and dozens of other resource descriptors. Without this metadata, the driver cannot launch the kernel -- it would have no way to know the kernel's hardware resource footprint.

nvlink both reads and writes `.nv.info` sections. During the merge phase it parses incoming `.nv.info` records to extract register counts for weak symbol resolution. During finalization it encodes computed properties (propagated register counts, barrier counts, stack sizes) back into `.nv.info` records in the output cubin. The embedded ptxas compiler produces `.nv.info` through a massive emission subsystem spanning ~190 functions across 1 MB of code.

## Key Facts

| Property | Value |
|---|---|
| ELF section type | `SHT_CUDA_INFO` = `0x70000000` (1,879,048,192) |
| Section name (global) | `.nv.info` |
| Section name (per-function) | `.nv.info.<function_name>` |
| Record format | Type-Length-Value (TLV), 4-byte aligned |
| Known attribute count | 97 EIATTR codes: 0--96 (v13.0.88) |
| Parser function | `sub_44E8B0` (`parse_nvinfo_section`, 4,780 bytes) |
| Single-attribute parser | `sub_44E590` (`parse_nvinfo_attribute`, 4,555 bytes) |
| Encoder function | `sub_468760` (`nvinfo_encode`, 14,322 bytes) |
| Master emission function | `sub_15C58F0` (78,811 bytes -- largest nv.info emitter) |
| Emission function count | ~190 functions at `0x15CF070`--`0x160FFFF` |
| Validation error | `"Invalid section type in .nv.info section header"` |

## Section Variants

A cubin contains two kinds of `.nv.info` sections, distinguished by name:

### Global `.nv.info`

A single section named `.nv.info` with `sh_link = 0` (no associated symbol). This contains attributes that apply to the entire compilation unit -- CUDA API version, compatibility flags, and shared metadata that is not specific to any one kernel.

### Per-Function `.nv.info.<name>`

One section per kernel or device function, named `.nv.info.<function_name>` with `sh_link` pointing to the symbol table entry for that function. These sections carry per-kernel resource descriptors: register count, barrier count, stack sizes, parameter bank layout, and instruction-offset tables for various runtime patching needs.

During the merge phase (`sub_45E7D0`), nvlink identifies `.nv.info` sections by checking `sh_type == 0x70000000`. The `sh_link` field determines whether a record is global (link=0) or per-function (link = symbol index). The merge function translates symbol indices from input-local to output-global using its mapping tables.

## TLV Record Format

Each `.nv.info` section contains a flat sequence of 4-byte-aligned TLV (Type-Length-Value) records. There is no section header or record count -- the parser walks from byte 0 to `sh_size`, consuming records sequentially.

### Record Layout

```
Offset  Size  Field
------  ----  -----
0x00    1     format      Format byte (determines payload structure)
0x01    1     attr_code   EIATTR type code (identifies the attribute)
0x02    2     size        Payload size in bytes (little-endian)
0x04    var   payload     Attribute-specific data (size bytes)
```

Total record size = 4 + `size`, padded to 4-byte alignment.

### Format Byte

The format byte at offset 0 controls how the payload is interpreted:

| Format | Name | Payload structure |
|---|---|---|
| `0x01` | Free format | Raw bytes, attribute-specific layout |
| `0x02` | Value format | Single 32-bit value (no symbol index) |
| `0x03` | Sized format | 16-bit value + padding |
| `0x04` | Indexed format | `[sym_index:4] [value:4]` -- per-symbol attribute |

Format `0x04` (indexed) is the most common for per-function attributes. The 4-byte symbol index at payload offset 0 identifies which function the attribute applies to. The linker uses this index for symbol remapping during merge and for extracting per-function properties during finalization.

### Parsing Pseudocode

From the decompiled `parse_nvinfo_section` and the `.nv.info` scan in `merge_weak_function`:

```c
uint8_t *ptr = section_data;
uint8_t *end = section_data + section_size;

while (ptr < end) {
    uint8_t  format    = ptr[0];
    uint8_t  attr_code = ptr[1];
    uint16_t size      = *(uint16_t *)(ptr + 2);

    if (format == 0x04) {
        // Indexed format: first 4 bytes of payload = symbol index
        uint32_t sym_idx = *(uint32_t *)(ptr + 4);
        uint32_t value   = *(uint32_t *)(ptr + 8);
        process_indexed_attribute(attr_code, sym_idx, value);
    } else if (format == 0x02) {
        // Value format: single 32-bit immediate
        uint32_t value = *(uint32_t *)(ptr + 4);
        process_global_attribute(attr_code, value);
    } else {
        // Free/sized format: attribute-specific handling
        process_raw_attribute(attr_code, ptr + 4, size);
    }

    ptr += 4 + ALIGN_UP(size, 4);  // advance to next record
}
```

## EIATTR Attribute Catalog

nvlink v13.0.88 defines 97 EIATTR (ELF Info ATTRibute) codes, numbered 0 through 96. The name-to-code mapping was extracted directly from the pointer table at VA `0x1D37D60` in the nvlink binary (16-byte entries: 8-byte string pointer + 8-byte metadata, indexed by code). The string names reside at `0x1D36819`--`0x1D37170`. Codes were verified against cubin TLV records produced by ptxas/nvcc v13.1 and cross-checked against the `compute_entry_properties` (`sub_451D80`) dispatch table. The following tables list all 97 attributes organized by functional category.

### Complete Code Table (Sequential)

All 97 codes in numeric order. Use this as the authoritative reference when parsing `.nv.info` TLV records.

| Code | Hex | Name | Format | Type |
|---:|---:|---|---|---|
| 0 | `0x00` | `EIATTR_ERROR` | -- | Sentinel |
| 1 | `0x01` | `EIATTR_PAD` | -- | Sentinel |
| 2 | `0x02` | `EIATTR_IMAGE_SLOT` | Indexed | Texture |
| 3 | `0x03` | `EIATTR_JUMPTABLE_RELOCS` | Free | Metadata |
| 4 | `0x04` | `EIATTR_CTAIDZ_USED` | Indexed | Metadata |
| 5 | `0x05` | `EIATTR_MAX_THREADS` | Indexed | Resource |
| 6 | `0x06` | `EIATTR_IMAGE_OFFSET` | Indexed | Texture |
| 7 | `0x07` | `EIATTR_IMAGE_SIZE` | Indexed | Texture |
| 8 | `0x08` | `EIATTR_TEXTURE_NORMALIZED` | Indexed | Texture |
| 9 | `0x09` | `EIATTR_SAMPLER_INIT` | Indexed | Texture |
| 10 | `0x0A` | `EIATTR_PARAM_CBANK` | Indexed | Param |
| 11 | `0x0B` | `EIATTR_SMEM_PARAM_OFFSETS` | Free | Param |
| 12 | `0x0C` | `EIATTR_CBANK_PARAM_OFFSETS` | Free | Param |
| 13 | `0x0D` | `EIATTR_SYNC_STACK` | Indexed | Metadata |
| 14 | `0x0E` | `EIATTR_TEXID_SAMPID_MAP` | Free | Texture |
| 15 | `0x0F` | `EIATTR_EXTERNS` | Free | Metadata |
| 16 | `0x10` | `EIATTR_REQNTID` | Indexed | Resource |
| 17 | `0x11` | `EIATTR_FRAME_SIZE` | Indexed | Resource |
| 18 | `0x12` | `EIATTR_MIN_STACK_SIZE` | Indexed | Resource |
| 19 | `0x13` | `EIATTR_SAMPLER_FORCE_UNNORMALIZED` | Indexed | Texture |
| 20 | `0x14` | `EIATTR_BINDLESS_IMAGE_OFFSETS` | Free | Texture |
| 21 | `0x15` | `EIATTR_BINDLESS_TEXTURE_BANK` | Indexed | Texture |
| 22 | `0x16` | `EIATTR_BINDLESS_SURFACE_BANK` | Indexed | Texture |
| 23 | `0x17` | `EIATTR_KPARAM_INFO` | Free | Param |
| 24 | `0x18` | `EIATTR_SMEM_PARAM_SIZE` | Indexed | Param |
| 25 | `0x19` | `EIATTR_CBANK_PARAM_SIZE` | Sized | Param |
| 26 | `0x1A` | `EIATTR_QUERY_NUMATTRIB` | Indexed | Metadata |
| 27 | `0x1B` | `EIATTR_MAXREG_COUNT` | Sized | Resource |
| 28 | `0x1C` | `EIATTR_EXIT_INSTR_OFFSETS` | Free | Offsets |
| 29 | `0x1D` | `EIATTR_S2RCTAID_INSTR_OFFSETS` | Free | Offsets |
| 30 | `0x1E` | `EIATTR_CRS_STACK_SIZE` | Indexed | Resource |
| 31 | `0x1F` | `EIATTR_NEED_CNP_WRAPPER` | Indexed | Metadata |
| 32 | `0x20` | `EIATTR_NEED_CNP_PATCH` | Indexed | Metadata |
| 33 | `0x21` | `EIATTR_EXPLICIT_CACHING` | Indexed | Metadata |
| 34 | `0x22` | `EIATTR_ISTYPEP_USED` | Indexed | Metadata |
| 35 | `0x23` | `EIATTR_MAX_STACK_SIZE` | Indexed | Resource |
| 36 | `0x24` | `EIATTR_SUQ_USED` | Indexed | Metadata |
| 37 | `0x25` | `EIATTR_LD_CACHEMOD_INSTR_OFFSETS` | Free | Offsets |
| 38 | `0x26` | `EIATTR_LOAD_CACHE_REQUEST` | Indexed | Metadata |
| 39 | `0x27` | `EIATTR_ATOM_SYS_INSTR_OFFSETS` | Free | Offsets |
| 40 | `0x28` | `EIATTR_COOP_GROUP_INSTR_OFFSETS` | Free | Offsets |
| 41 | `0x29` | `EIATTR_COOP_GROUP_MASK_REGIDS` | Indexed | Cluster |
| 42 | `0x2A` | `EIATTR_SW1850030_WAR` | Free | WAR |
| 43 | `0x2B` | `EIATTR_WMMA_USED` | Indexed | Metadata |
| 44 | `0x2C` | `EIATTR_HAS_PRE_V10_OBJECT` | Value | Metadata |
| 45 | `0x2D` | `EIATTR_ATOMF16_EMUL_INSTR_OFFSETS` | Free | Offsets |
| 46 | `0x2E` | `EIATTR_ATOM16_EMUL_INSTR_REG_MAP` | Free | Offsets |
| 47 | `0x2F` | `EIATTR_REGCOUNT` | Indexed | Resource |
| 48 | `0x30` | `EIATTR_SW2393858_WAR` | Free | WAR |
| 49 | `0x31` | `EIATTR_INT_WARP_WIDE_INSTR_OFFSETS` | Free | Offsets |
| 50 | `0x32` | `EIATTR_SHARED_SCRATCH` | Indexed | Shared |
| 51 | `0x33` | `EIATTR_STATISTICS` | Free | Metadata |
| 52 | `0x34` | `EIATTR_INDIRECT_BRANCH_TARGETS` | Free | Offsets |
| 53 | `0x35` | `EIATTR_SW2861232_WAR` | Free | WAR |
| 54 | `0x36` | `EIATTR_SW_WAR` | Free | WAR |
| 55 | `0x37` | `EIATTR_CUDA_API_VERSION` | Indexed | Metadata |
| 56 | `0x38` | `EIATTR_NUM_MBARRIERS` | Indexed | Resource |
| 57 | `0x39` | `EIATTR_MBARRIER_INSTR_OFFSETS` | Free | Offsets |
| 58 | `0x3A` | `EIATTR_COROUTINE_RESUME_OFFSETS` | Free | Offsets |
| 59 | `0x3B` | `EIATTR_SAM_REGION_STACK_SIZE` | Indexed | Resource |
| 60 | `0x3C` | `EIATTR_PER_REG_TARGET_PERF_STATS` | Free | Metadata |
| 61 | `0x3D` | `EIATTR_CTA_PER_CLUSTER` | Indexed | Cluster |
| 62 | `0x3E` | `EIATTR_EXPLICIT_CLUSTER` | Indexed | Cluster |
| 63 | `0x3F` | `EIATTR_MAX_CLUSTER_RANK` | Indexed | Cluster |
| 64 | `0x40` | `EIATTR_INSTR_REG_MAP` | Free | Metadata |
| 65 | `0x41` | `EIATTR_RESERVED_SMEM_USED` | Indexed | Shared |
| 66 | `0x42` | `EIATTR_RESERVED_SMEM_0_SIZE` | Indexed | Shared |
| 67 | `0x43` | `EIATTR_UCODE_SECTION_DATA` | Free | Metadata |
| 68 | `0x44` | `EIATTR_UNUSED_LOAD_BYTE_OFFSET` | Free | Offsets |
| 69 | `0x45` | `EIATTR_KPARAM_INFO_V2` | Free | Param |
| 70 | `0x46` | `EIATTR_SYSCALL_OFFSETS` | Free | Offsets |
| 71 | `0x47` | `EIATTR_SW_WAR_MEMBAR_SYS_INSTR_OFFSETS` | Free | WAR |
| 72 | `0x48` | `EIATTR_GRAPHICS_GLOBAL_CBANK` | Indexed | Graphics |
| 73 | `0x49` | `EIATTR_SHADER_TYPE` | Indexed | Graphics |
| 74 | `0x4A` | `EIATTR_VRC_CTA_INIT_COUNT` | Indexed | Graphics |
| 75 | `0x4B` | `EIATTR_TOOLS_PATCH_FUNC` | Indexed | Metadata |
| 76 | `0x4C` | `EIATTR_NUM_BARRIERS` | Indexed | Resource |
| 77 | `0x4D` | `EIATTR_TEXMODE_INDEPENDENT` | Indexed | Texture |
| 78 | `0x4E` | `EIATTR_PERF_STATISTICS` | Free | Metadata |
| 79 | `0x4F` | `EIATTR_AT_ENTRY_FRAGEMENTS` | Free | Blackwell |
| 80 | `0x50` | `EIATTR_SPARSE_MMA_MASK` | Free | Blackwell |
| 81 | `0x51` | `EIATTR_TCGEN05_1CTA_USED` | Indexed | Blackwell |
| 82 | `0x52` | `EIATTR_TCGEN05_2CTA_USED` | Indexed | Blackwell |
| 83 | `0x53` | `EIATTR_GEN_ERRBAR_AT_EXIT` | Indexed | Blackwell |
| 84 | `0x54` | `EIATTR_REG_RECONFIG` | Indexed | Blackwell |
| 85 | `0x55` | `EIATTR_ANNOTATIONS` | Free | Metadata |
| 86 | `0x56` | `EIATTR_UNKNOWN` | -- | Sentinel |
| 87 | `0x57` | `EIATTR_STACK_CANARY_TRAP_OFFSETS` | Free | Offsets |
| 88 | `0x58` | `EIATTR_STUB_FUNCTION_KIND` | Indexed | Metadata |
| 89 | `0x59` | `EIATTR_LOCAL_CTA_ASYNC_STORE_OFFSETS` | Free | Offsets |
| 90 | `0x5A` | `EIATTR_MERCURY_FINALIZER_OPTIONS` | Free | Mercury |
| 91 | `0x5B` | `EIATTR_BLOCKS_ARE_CLUSTERS` | Indexed | Cluster |
| 92 | `0x5C` | `EIATTR_SANITIZE` | Indexed | Blackwell |
| 93 | `0x5D` | `EIATTR_SYSCALLS_FALLBACK` | Free | Metadata |
| 94 | `0x5E` | `EIATTR_CUDA_REQ` | Free | Metadata |
| 95 | `0x5F` | `EIATTR_MERCURY_ISA_VERSION` | Sized | Mercury |
| 96 | `0x60` | `EIATTR_ERROR_LAST` | -- | Sentinel |

### Resource Allocation (GPU Driver Critical)

These attributes directly control how the GPU driver allocates hardware resources for kernel launch. Incorrect values cause silent performance degradation or launch failure.

| Code | Hex | Name | Format | Description |
|---:|---:|---|---|---|
| 47 | `0x2F` | `EIATTR_REGCOUNT` | Indexed | Physical register count per thread. The GPU driver computes `max_warps_per_SM = total_registers / (regcount * warp_size)`. This is the single most important occupancy-determining attribute. |
| 5 | `0x05` | `EIATTR_MAX_THREADS` | Indexed | Maximum threads per block (from `.maxntid` PTX directive). |
| 16 | `0x10` | `EIATTR_REQNTID` | Indexed | Required thread count per dimension (from `.reqntid`). |
| 17 | `0x11` | `EIATTR_FRAME_SIZE` | Indexed | Per-thread local memory frame size in bytes. |
| 18 | `0x12` | `EIATTR_MIN_STACK_SIZE` | Indexed | Minimum stack size per thread (non-recursive case). |
| 35 | `0x23` | `EIATTR_MAX_STACK_SIZE` | Indexed | Maximum stack size per thread (recursive case). |
| 30 | `0x1E` | `EIATTR_CRS_STACK_SIZE` | Indexed | Call-Return-Stack size for nested function calls. |
| 59 | `0x3B` | `EIATTR_SAM_REGION_STACK_SIZE` | Indexed | SAM (Streaming Asynchronous Memory) region stack size. |
| 76 | `0x4C` | `EIATTR_NUM_BARRIERS` | Indexed | Number of named barriers used (max 16 on most architectures). |
| 56 | `0x38` | `EIATTR_NUM_MBARRIERS` | Indexed | Number of memory barriers (mbarrier objects) used. |
| 27 | `0x1B` | `EIATTR_MAXREG_COUNT` | Sized | Maximum register count hint (from `--maxrregcount` or `.maxnreg`). |

### Parameter Bank Layout

These describe how kernel parameters are laid out in constant memory bank 0 (`c[0x0]`).

| Code | Hex | Name | Format | Description |
|---:|---:|---|---|---|
| 10 | `0x0A` | `EIATTR_PARAM_CBANK` | Indexed | Constant bank number and offset for kernel parameters. |
| 25 | `0x19` | `EIATTR_CBANK_PARAM_SIZE` | Sized | Size of the parameter constant bank in bytes. |
| 24 | `0x18` | `EIATTR_SMEM_PARAM_SIZE` | Indexed | Size of shared memory parameter region. |
| 11 | `0x0B` | `EIATTR_SMEM_PARAM_OFFSETS` | Free | Offsets of parameters within shared memory. |
| 12 | `0x0C` | `EIATTR_CBANK_PARAM_OFFSETS` | Free | Offsets of parameters within constant bank. |
| 23 | `0x17` | `EIATTR_KPARAM_INFO` | Free | Kernel parameter metadata (types, sizes, alignments). |
| 69 | `0x45` | `EIATTR_KPARAM_INFO_V2` | Free | Extended kernel parameter info (v2 format with additional fields). |

### Instruction Offset Tables

These record byte offsets of specific instruction types within the kernel's `.text` section, enabling the driver and tools to locate and patch instructions at load time.

| Code | Hex | Name | Format | Description |
|---:|---:|---|---|---|
| 28 | `0x1C` | `EIATTR_EXIT_INSTR_OFFSETS` | Free | Byte offsets of all `EXIT` instructions. |
| 29 | `0x1D` | `EIATTR_S2RCTAID_INSTR_OFFSETS` | Free | Offsets of `S2R` instructions reading `SR_CTAID` (CTA ID). |
| 37 | `0x25` | `EIATTR_LD_CACHEMOD_INSTR_OFFSETS` | Free | Offsets of load instructions with cache modifier. |
| 39 | `0x27` | `EIATTR_ATOM_SYS_INSTR_OFFSETS` | Free | Offsets of atomic instructions with `.sys` scope. |
| 40 | `0x28` | `EIATTR_COOP_GROUP_INSTR_OFFSETS` | Free | Offsets of cooperative group instructions. |
| 45 | `0x2D` | `EIATTR_ATOMF16_EMUL_INSTR_OFFSETS` | Free | Offsets of emulated FP16 atomic instructions. |
| 46 | `0x2E` | `EIATTR_ATOM16_EMUL_INSTR_REG_MAP` | Free | Register map for 16-bit atomic emulation. |
| 49 | `0x31` | `EIATTR_INT_WARP_WIDE_INSTR_OFFSETS` | Free | Offsets of integer warp-wide instructions. |
| 52 | `0x34` | `EIATTR_INDIRECT_BRANCH_TARGETS` | Free | Valid targets of indirect branches (for CFI). |
| 57 | `0x39` | `EIATTR_MBARRIER_INSTR_OFFSETS` | Free | Offsets of `MBAR` (memory barrier) instructions. |
| 58 | `0x3A` | `EIATTR_COROUTINE_RESUME_OFFSETS` | Free | Resume point offsets for device-side coroutines. |
| 68 | `0x44` | `EIATTR_UNUSED_LOAD_BYTE_OFFSET` | Free | Byte offset of unused load instruction. |
| 70 | `0x46` | `EIATTR_SYSCALL_OFFSETS` | Free | Offsets of `__cuda_syscall` invocations. |
| 87 | `0x57` | `EIATTR_STACK_CANARY_TRAP_OFFSETS` | Free | Offsets of stack canary trap instructions (stack protector). |
| 89 | `0x59` | `EIATTR_LOCAL_CTA_ASYNC_STORE_OFFSETS` | Free | Offsets of CTA-local async store instructions. |

### Texture and Surface Binding

| Code | Hex | Name | Format | Description |
|---:|---:|---|---|---|
| 2 | `0x02` | `EIATTR_IMAGE_SLOT` | Indexed | Texture/surface image slot assignment. |
| 6 | `0x06` | `EIATTR_IMAGE_OFFSET` | Indexed | Offset within the image descriptor table. |
| 7 | `0x07` | `EIATTR_IMAGE_SIZE` | Indexed | Size of the image descriptor. |
| 8 | `0x08` | `EIATTR_TEXTURE_NORMALIZED` | Indexed | Whether texture coordinates are normalized. |
| 9 | `0x09` | `EIATTR_SAMPLER_INIT` | Indexed | Sampler initialization parameters. |
| 14 | `0x0E` | `EIATTR_TEXID_SAMPID_MAP` | Free | Texture ID to sampler ID mapping table. |
| 19 | `0x13` | `EIATTR_SAMPLER_FORCE_UNNORMALIZED` | Indexed | Force unnormalized sampler coordinates. |
| 20 | `0x14` | `EIATTR_BINDLESS_IMAGE_OFFSETS` | Free | Offsets for bindless image references. |
| 21 | `0x15` | `EIATTR_BINDLESS_TEXTURE_BANK` | Indexed | Constant bank used for bindless texture descriptors. |
| 22 | `0x16` | `EIATTR_BINDLESS_SURFACE_BANK` | Indexed | Constant bank used for bindless surface descriptors. |
| 77 | `0x4D` | `EIATTR_TEXMODE_INDEPENDENT` | Indexed | Independent texture mode flag. |

### Cluster and Cooperative Launch (sm_90+)

| Code | Hex | Name | Format | Description |
|---:|---:|---|---|---|
| 41 | `0x29` | `EIATTR_COOP_GROUP_MASK_REGIDS` | Indexed | Register IDs used for cooperative group masks. |
| 61 | `0x3D` | `EIATTR_CTA_PER_CLUSTER` | Indexed | Number of CTAs per cluster (Hopper cluster launch). |
| 62 | `0x3E` | `EIATTR_EXPLICIT_CLUSTER` | Indexed | Whether kernel uses explicit cluster dimensions. |
| 63 | `0x3F` | `EIATTR_MAX_CLUSTER_RANK` | Indexed | Maximum cluster rank for scheduling. |
| 91 | `0x5B` | `EIATTR_BLOCKS_ARE_CLUSTERS` | Indexed | CTA blocks are clusters flag. |

### Shared Memory and Reserved Resources

| Code | Hex | Name | Format | Description |
|---:|---:|---|---|---|
| 50 | `0x32` | `EIATTR_SHARED_SCRATCH` | Indexed | Shared memory scratch space for register spilling. |
| 65 | `0x41` | `EIATTR_RESERVED_SMEM_USED` | Indexed | Whether reserved shared memory is used. |
| 66 | `0x42` | `EIATTR_RESERVED_SMEM_0_SIZE` | Indexed | Size of reserved shared memory partition 0. |

### Software Workarounds

Hardware errata requiring instruction-level patching by the driver.

| Code | Hex | Name | Format | Description |
|---:|---:|---|---|---|
| 42 | `0x2A` | `EIATTR_SW1850030_WAR` | Free | Workaround for HW bug 1850030. |
| 48 | `0x30` | `EIATTR_SW2393858_WAR` | Free | Workaround for HW bug 2393858. |
| 53 | `0x35` | `EIATTR_SW2861232_WAR` | Free | Workaround for HW bug 2861232. |
| 54 | `0x36` | `EIATTR_SW_WAR` | Free | Generic software workaround container. |
| 71 | `0x47` | `EIATTR_SW_WAR_MEMBAR_SYS_INSTR_OFFSETS` | Free | Offsets of `MEMBAR.SYS` instructions needing software workaround. |

### Compilation Metadata

| Code | Hex | Name | Format | Description |
|---:|---:|---|---|---|
| 3 | `0x03` | `EIATTR_JUMPTABLE_RELOCS` | Free | Jump table relocation entries. |
| 4 | `0x04` | `EIATTR_CTAIDZ_USED` | Indexed | Whether kernel uses `%ctaid.z` (3D grid). |
| 13 | `0x0D` | `EIATTR_SYNC_STACK` | Indexed | Synchronization stack depth. |
| 15 | `0x0F` | `EIATTR_EXTERNS` | Free | External symbol references list. |
| 26 | `0x1A` | `EIATTR_QUERY_NUMATTRIB` | Indexed | Number of queryable attributes. |
| 31 | `0x1F` | `EIATTR_NEED_CNP_WRAPPER` | Indexed | Kernel needs CUDA Nested Parallelism wrapper. |
| 32 | `0x20` | `EIATTR_NEED_CNP_PATCH` | Indexed | Kernel needs CNP patching at load time. |
| 33 | `0x21` | `EIATTR_EXPLICIT_CACHING` | Indexed | Explicit cache control directives present. |
| 34 | `0x22` | `EIATTR_ISTYPEP_USED` | Indexed | `isspacep` instruction used. |
| 36 | `0x24` | `EIATTR_SUQ_USED` | Indexed | Surface query instruction used. |
| 38 | `0x26` | `EIATTR_LOAD_CACHE_REQUEST` | Indexed | Load cache request configuration. |
| 43 | `0x2B` | `EIATTR_WMMA_USED` | Indexed | Warp Matrix Multiply-Accumulate instructions used. |
| 44 | `0x2C` | `EIATTR_HAS_PRE_V10_OBJECT` | Value | Object contains pre-CUDA 10 compiled code. |
| 51 | `0x33` | `EIATTR_STATISTICS` | Free | Compilation statistics (instruction counts, etc.). |
| 55 | `0x37` | `EIATTR_CUDA_API_VERSION` | Indexed | CUDA API version the kernel was compiled for. Value `0x83` = CUDA 13.1. |
| 60 | `0x3C` | `EIATTR_PER_REG_TARGET_PERF_STATS` | Free | Per-register-target performance statistics. |
| 64 | `0x40` | `EIATTR_INSTR_REG_MAP` | Free | Instruction-to-register mapping for profiling. |
| 67 | `0x43` | `EIATTR_UCODE_SECTION_DATA` | Free | Microcode section data (internal). |
| 75 | `0x4B` | `EIATTR_TOOLS_PATCH_FUNC` | Indexed | Function patching descriptor for CUDA tools. |
| 78 | `0x4E` | `EIATTR_PERF_STATISTICS` | Free | Performance statistics for the profiler. |
| 85 | `0x55` | `EIATTR_ANNOTATIONS` | Free | General-purpose annotation data. |
| 88 | `0x58` | `EIATTR_STUB_FUNCTION_KIND` | Indexed | Stub function classification. |
| 93 | `0x5D` | `EIATTR_SYSCALLS_FALLBACK` | Free | Syscall fallback mechanism offsets. |
| 94 | `0x5E` | `EIATTR_CUDA_REQ` | Free | CUDA requirements descriptor. |

### Graphics-Specific

| Code | Hex | Name | Format | Description |
|---:|---:|---|---|---|
| 72 | `0x48` | `EIATTR_GRAPHICS_GLOBAL_CBANK` | Indexed | Global constant bank for graphics shaders. |
| 73 | `0x49` | `EIATTR_SHADER_TYPE` | Indexed | Shader type (vertex, fragment, compute, etc.). |
| 74 | `0x4A` | `EIATTR_VRC_CTA_INIT_COUNT` | Indexed | Virtual Register Count CTA init count. |

### Blackwell+ Features (sm_100+)

| Code | Hex | Name | Format | Description |
|---:|---:|---|---|---|
| 79 | `0x4F` | `EIATTR_AT_ENTRY_FRAGEMENTS` | Free | Fragment descriptors at function entry (note: "FRAGEMENTS" is a typo in the binary; corrected variant `EIATTR_AT_ENTRY_FRAGMENTS` at `0x245E8D9`). |
| 80 | `0x50` | `EIATTR_SPARSE_MMA_MASK` | Free | Sparsity mask for structured-sparse MMA operations. |
| 81 | `0x51` | `EIATTR_TCGEN05_1CTA_USED` | Indexed | tcgen05 (5th-gen tensor core) single-CTA mode used. |
| 82 | `0x52` | `EIATTR_TCGEN05_2CTA_USED` | Indexed | tcgen05 two-CTA mode used. |
| 83 | `0x53` | `EIATTR_GEN_ERRBAR_AT_EXIT` | Indexed | Generate error barrier at kernel exit. |
| 84 | `0x54` | `EIATTR_REG_RECONFIG` | Indexed | Dynamic register reconfiguration (`setmaxnreg`). |
| 92 | `0x5C` | `EIATTR_SANITIZE` | Indexed | Address sanitizer instrumentation present. |

### Mercury-Specific

| Code | Hex | Name | Format | Description |
|---:|---:|---|---|---|
| 90 | `0x5A` | `EIATTR_MERCURY_FINALIZER_OPTIONS` | Free | Options for the Mercury FNLZR post-link pass. |
| 95 | `0x5F` | `EIATTR_MERCURY_ISA_VERSION` | Sized | Mercury ISA version for the shader binary. |

### Sentinel and Error

| Code | Hex | Name | Format | Description |
|---:|---:|---|---|---|
| 0 | `0x00` | `EIATTR_ERROR` | -- | Invalid/error sentinel. |
| 1 | `0x01` | `EIATTR_PAD` | -- | Padding record (ignored by parser). |
| 86 | `0x56` | `EIATTR_UNKNOWN` | -- | Unknown attribute placeholder. |
| 96 | `0x60` | `EIATTR_ERROR_LAST` | -- | Upper bound sentinel for the main enum range. |

## How nvlink Processes .nv.info

### During Merge (Input Processing)

When `merge_elf` (`sub_45E7D0`) encounters a section with `sh_type == 0x70000000`, it enters the `.nv.info` processing path in Phase 5 (section header iteration). The merge function:

1. **Remaps symbol indices**: For format `0x04` (indexed) records, the 4-byte symbol index in the payload is translated from input-local to output-global using the `map_symbol_index` table.

2. **Skips weak-processed attributes**: Records whose symbol index appears in the `weak_processed` array are silently dropped for EIATTR codes where `attr_code <= 0x2F` and `_bittest64(0x800800020000, attr_code)` is true. The three codes matching this bitmask are 17 (`0x11`, FRAME_SIZE), 35 (`0x23`, MAX_STACK_SIZE), and 47 (`0x2F`, REGCOUNT). The rationale: when a weak function is replaced, its resource descriptors must not contaminate the replacement.

3. **Appends to output**: Surviving records are appended to the output ELF's `.nv.info` or `.nv.info.<name>` section.

### During Weak Symbol Resolution

`merge_weak_function` (`sub_45D180`) extracts the register count (EIATTR code 47) for competing weak definitions. It first checks a cached value at offset +47 of the symbol's nvinfo record. If zero, it falls back to scanning all `SHT_CUDA_INFO` sections:

```
for each section with sh_type == 0x70000000:
    walk TLV records looking for format=0x04, attr_code=0x2F, matching sym_index
    if found: return *(uint32_t*)(record + 8)
```

The register count determines which weak definition to keep -- fewer registers wins, maximizing occupancy.

### During Finalization (Output Generation)

`compute_entry_properties` (`sub_451D80`, 97,969 bytes -- the largest function in the linker) runs during the finalization phase. It computes derived properties for each kernel entry point:

1. **Register count propagation**: `propagate_register_counts` (`sub_450ED0`) walks the callgraph and propagates the maximum register count from callees to each entry kernel. The verbose trace `"regcount %d for %s propagated to entry %s"` logs this propagation.

2. **Barrier count creation**: When a kernel's section flags contain a barrier count but no `EIATTR_NUM_BARRIERS` record exists, the function creates one: `"Creating new EIATTR_NUM_BARRIERS and moving barcount %d from section flags of %s to nvinfo for entry symbol %s"`.

3. **Stack size computation**: Frame sizes and CRS stack sizes are propagated through the callgraph to compute per-entry worst-case stack requirements.

4. **Encoding**: `nvinfo_encode` (`sub_468760`, 14,322 bytes) serializes computed properties into TLV records using SSE2 intrinsics for efficient byte packing.

## Emission Subsystem (Embedded ptxas)

When nvlink performs LTO compilation, the embedded ptxas compiler generates `.nv.info` attributes through a dedicated emission subsystem at `0x15C5000`--`0x160FFFF`. This subsystem is the single largest code region dedicated to `.nv.info` processing in the entire binary.

### Architecture

The emission pipeline has three layers:

**Layer 1: SM dispatch** (`sub_15C0CE0`). A singleton initialization function registers per-SM callback tables for 12 architecture families (sm_75 through sm_121). Each SM gets an nv.info emitter callback looked up through map A8 via `sub_15C3DB0`. The callback creates a ~1,936-byte codegen state with architecture-specific constants at offsets 344 and 348 (compute capability encoding).

**Layer 2: Master emitters** (4 functions, 78--55 KB each). These are the top-level attribute-lowering functions that read compilation state and dispatch to per-attribute-type handlers:

| Address | Size | Identity | Specialty |
|---|---|---|---|
| `sub_15C4A70` | 23,547 B | `emit_nv_info_section_type1` | Core attributes |
| `sub_15C58F0` | 78,811 B | `emit_nv_info_section_type2` | Comprehensive lowering (largest) |
| `sub_15C8A80` | 40,921 B | `emit_nv_info_section_type3` | Texture/surface references |
| `sub_15CA450` | 54,675 B | `emit_nv_info_section_extended` | Extended attributes (sm_90+) |

The master emitters use an FNV-1a hash table (offset basis `0x811C9DC5`, prime 16,777,619) at `object+488` for O(1) function-ID-to-attribute lookup using 24-byte entries.

**Layer 3: Per-attribute handlers** (~190 functions at `0x15CF070`--`0x160FFFF`). Each function is 4--8 KB and handles exactly one EIATTR type. They follow a uniform template:

```
1. Read attribute descriptor pointer from a2
2. Read sub-attribute fields from known offsets (m128i-based, 32-byte descriptors)
3. Call sub_A4CBB0 to create attribute IR node
4. Call sub_A49120 to set EIATTR type code
5. Call sub_A49190/sub_A49140 for type validation
6. Write output via sub_4A3D60 (operand builder)
7. Return constructed attribute list
```

The uniformity across ~190 functions suggests they are generated from a data-driven table or macro expansion in NVIDIA's source code, one handler per EIATTR type.

### Supporting Functions

| Address | Size | Identity | Role |
|---|---|---|---|
| `sub_15CCEE0` | 12,668 B | `sort_and_merge_attribute_lists` | Orders nv.info entries before emission |
| `sub_15CD6A0` | 25,822 B | `merge_attribute_sections` | Merges nv.info from multiple compilation units |
| `sub_15CE650` | 14,296 B | `validate_and_emit_attributes` | Validates attributes before final emission |
| `sub_1631350` | 44,830 B | `process_kernel_attributes` | Master kernel attribute processor (shared memory, regs, stack) |
| `sub_16312F0` | small | `emit_reserved_smem_attributes` | Emits `.nv.reservedSmem.*` attribute records |
| `sub_1655A60` | 30,332 B | `lower_nv_info_to_codegen` | Converts nv.info symbol references to codegen operands |

## How .nv.info Drives GPU Resource Allocation

The `.nv.info` section is not just metadata for tools -- it is the primary input to the GPU driver's kernel launch resource allocator. The relationship is:

1. **Register allocation**: `EIATTR_REGCOUNT` tells the driver how many registers each thread needs. The driver computes: `max_warps_per_SM = total_registers / (regcount * warp_size)`. This is the single most important occupancy-determining attribute.

2. **Shared memory reservation**: `EIATTR_SMEM_PARAM_SIZE` and `EIATTR_RESERVED_SMEM_0_SIZE` determine how much shared memory to carve out before the kernel's dynamic shared memory allocation.

3. **Stack allocation**: `EIATTR_CRS_STACK_SIZE` and `EIATTR_MAX_STACK_SIZE` determine per-thread stack allocation. If the driver gets this wrong (too small), the kernel will corrupt memory; if too large, occupancy drops.

4. **Barrier reservation**: `EIATTR_NUM_BARRIERS` reserves named barrier slots. On most architectures the hardware supports 16 barriers per CTA. The driver must configure the barrier hardware before launch.

5. **Instruction patching**: The offset tables (`EIATTR_EXIT_INSTR_OFFSETS`, `EIATTR_S2RCTAID_INSTR_OFFSETS`, `EIATTR_SW*_WAR`) tell the driver which instruction words to patch. This enables hardware workarounds and CTA-ID remapping for cluster launch without recompilation.

6. **Cluster configuration**: `EIATTR_CTA_PER_CLUSTER` and `EIATTR_EXPLICIT_CLUSTER` (sm_90+) control the cluster launch hardware, determining how many CTAs share distributed shared memory.

## Binary Artifacts

### Typos Preserved in the Binary

| String in binary | Correct spelling | Location |
|---|---|---|
| `EIATTR_AT_ENTRY_FRAGEMENTS` | `EIATTR_AT_ENTRY_FRAGMENTS` | `0x1D36E0F` |
| `"Invalid section type in .nv.info section header"` | (correct) | `0x2460218` |

A corrected variant `EIATTR_AT_ENTRY_FRAGMENTS` also exists at `0x245E8D9`, suggesting awareness of the typo but preservation of the original for backward compatibility. Similarly, `EIATTR_COROUTINE_RESUME_ID_OFFSETS` at `0x245F010` is an alternate name for code 58 (`EIATTR_COROUTINE_RESUME_OFFSETS`), used in the embedded ptxas compiler.

### Name Table Structure

The EIATTR name table is a contiguous array of 97 16-byte entries at VA `0x1D37D60` in the nvlink binary. Each entry consists of an 8-byte pointer to a null-terminated string name and an 8-byte metadata word. The table is indexed by EIATTR code number (0 through 96). The metadata word encodes format hints and minimum toolkit version compatibility -- entries 0--42 have metadata `0x0000000000000001`, while entries 43+ carry version fields in their upper 32 bits.

### Diagnostic Strings

```
"no new register count found for %s, checking .nv.info"       (0x1D3BA68)
"no original register count found for %s, checking .nv.info"   (0x1D3BAA0)
"regcount %d for %s propagated to entry %s"                    (0x1D3B070)
"no regcount?"                                                 (0x1D3AF6C)
"entry function '%s' with max regcount of %d calls function
 '%s' with regcount of %d"                                     (0x1D39930)
```

These diagnostic messages appear when `--verbose` is active and reveal the register count propagation algorithm in action.

## Cross-References

- [Weak Symbol Handling](../linker/weak-symbols.md) -- Register count extraction from `.nv.info` during weak resolution
- [Section Merging](../linker/section-merging.md) -- `.nv.info` section merge with symbol index remapping
- [Finalization Phase](../pipeline/finalize.md) -- `compute_entry_properties` and `propagate_register_counts`
- [Architecture Dispatch](../ptxas/arch-dispatch.md) -- Per-SM nv.info emitter callback registration
- [Constant Banks](constant-banks.md) -- `EIATTR_PARAM_CBANK` and `EIATTR_CBANK_PARAM_SIZE` interaction
