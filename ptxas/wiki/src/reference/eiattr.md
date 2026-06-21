# EIATTR Attribute Catalog

> *All addresses in this page apply to ptxas v13.0.88 (CUDA 13.0). Other versions will differ.*

EIATTR (ELF Info ATTRibute) is NVIDIA's proprietary metadata system embedded in `.nv.info` ELF sections within CUBIN files. Every CUDA kernel carries EIATTR records that tell the GPU driver how many registers to allocate, how much shared memory to reserve, what barriers the kernel uses, and dozens of other resource descriptors. Without this metadata, the driver cannot launch the kernel — it has no way to determine the kernel's hardware resource footprint.

ptxas v13.0.88 defines 97 EIATTR codes, numbered 0 through 96 (`0x00`--`0x60`). The code-to-name mapping was extracted from the pointer table at VA `0x23FDC20` in the ptxas binary (16-byte entries: 8-byte string pointer + 8-byte metadata word, indexed by code number). The string names reside at `0x23FC6C7`--`0x23FD040`. Code assignments were cross-verified against the nvlink v13.0.88 pointer table at `0x1D37D60`, confirming identical enumeration across both tools.

| | |
|---|---|
| **ELF section type** | `SHT_CUDA_INFO` = `0x70000000` |
| **Section name (global)** | `.nv.info` |
| **Section name (per-function)** | `.nv.info.<function_name>` |
| **Record format** | Type-Length-Value (TLV), 4-byte aligned |
| **Known attribute count** | 97 codes: 0–96 (v13.0.88) |
| **Name table VA** | `0x23FDC20` (97 entries x 16 bytes = 1,552 bytes) |
| **EIATTR builder function** | `sub_1CC9800` (14,764 B native / 86 KB decomp — third largest in output range) |
| **Barrier/register propagator** | `sub_1CC8950` (2,634 bytes, propagates counts across call graph) |
| **TLV record emitter** | `sub_1CC85F0` (44 lines, writes individual EIATTR records) |
| **SM-version gating** | `sub_1C97840` (checks whether an EIATTR code is valid for a given SM version) |

## TLV Record Format

Each `.nv.info` section contains a flat sequence of 4-byte-aligned TLV records. There is no section header or record count — the parser walks from byte 0 to `sh_size`, consuming records sequentially.

### Record Layout

```text
Offset  Size  Field
------  ----  -----
0x00    1     format      Format byte (determines payload structure)
0x01    1     attr_code   EIATTR type code (0x00–0x60)
0x02    2     size        Payload size in bytes (little-endian uint16)
0x04    var   payload     Attribute-specific data (size bytes)
```

Total record size = 4 + `size`, padded up to 4-byte alignment. The minimum record is 4 bytes (format + code + size=0, no payload).

### Format Byte

The format byte at offset 0 controls how the payload is interpreted. NVIDIA's canonical names for these formats are `EIFMT_NVAL`, `EIFMT_BVAL`, `EIFMT_HVAL`, `EIFMT_SVAL`; this wiki also uses the descriptive names in the "Descriptive" column to describe the payload layout actually observed in ptxas output:

| Format | Canonical | Descriptive | Payload structure | Typical use |
|---:|---|---|---|---|
| `0x01` | `EIFMT_NVAL` | Free | No value at this format level — trailing bytes depend entirely on attribute code | Offset tables, parameter info |
| `0x02` | `EIFMT_BVAL` | Value | Byte value (trailing byte extended to 32-bit in cubin TLV) | Global flags |
| `0x03` | `EIFMT_HVAL` | Sized | Half value (16-bit, padded) | Counts, sizes |
| `0x04` | `EIFMT_SVAL` | Indexed | Sized value: 16-bit size prefix followed by that many bytes — for per-symbol attributes the payload is `[sym_index:4][value:4]` | Per-kernel resources |

The names `NVAL`/`BVAL`/`HVAL`/`SVAL` stand for "no value", "byte value", "half value", "sized value" and describe only the format-level encoding. The actual payload convention (e.g. symbol index in the first 4 bytes of an `SVAL` payload) is a per-attribute convention, not a property of the format byte itself.

Format `0x04` (`EIFMT_SVAL`) is the most common for per-function attributes. The 4-byte symbol index at payload offset 0 identifies which function the attribute applies to. The linker uses this index for symbol remapping during merge and for per-function property extraction during finalization.

### Binary Evidence — `sub_1CC85F0`

The TLV record emitter function directly confirms the encoding:

```c
// sub_1CC85F0 -- simplified from decompilation
// a2 = attr_code, a3 = 16-bit value/size, a4 = payload data, a5 = symbol index
void emit_eiattr(void* elfw, uint8_t attr_code, int16_t size, void* data, uint32_t sym_idx) {
    if (!is_valid_for_sm(attr_code, elfw->sm_version))
        return;

    int section_index = get_nvinfo_section(elfw, sym_idx);

    // Allocate 16-byte record buffer
    uint8_t* record = pool_alloc(16);

    // TLV header
    record[0] = 0x04;               // format = Indexed
    record[1] = attr_code;           // EIATTR type code
    *(uint16_t*)(record + 2) = size; // payload size
    *(uint32_t*)(record + 4) = section_index; // symbol index

    // Append to .nv.info section's linked list
    list_append(record, &elfw->nvinfo_list);

    // Overwrite size field with actual value for indexed format
    *(uint16_t*)(record + 2) = size;
    *(uint64_t*)(record + 8) = data;
}
```

### Parsing Pseudocode

```c
uint8_t *ptr = section_data;
uint8_t *end = section_data + section_size;

while (ptr < end) {
    uint8_t  format    = ptr[0];
    uint8_t  attr_code = ptr[1];
    uint16_t size      = *(uint16_t *)(ptr + 2);

    if (format == 0x04) {
        // Indexed: first 4 bytes of payload = symbol index
        uint32_t sym_idx = *(uint32_t *)(ptr + 4);
        uint32_t value   = *(uint32_t *)(ptr + 8);
        process_indexed_attribute(attr_code, sym_idx, value);
    } else if (format == 0x02) {
        // Value: single 32-bit immediate
        uint32_t value = *(uint32_t *)(ptr + 4);
        process_global_attribute(attr_code, value);
    } else {
        // Free/sized: attribute-specific handling
        process_raw_attribute(attr_code, ptr + 4, size);
    }

    ptr += 4 + ALIGN_UP(size, 4);
}
```

## Section Variants

A cubin contains two kinds of `.nv.info` sections:

**Global `.nv.info`** — A single section named `.nv.info` with `sh_link = 0` (no associated symbol). Contains attributes that apply to the entire compilation unit: CUDA API version, compatibility flags, and shared metadata not specific to any one kernel.

**Per-function `.nv.info.<name>`** — One section per kernel or device function, named `.nv.info.<function_name>` with `sh_link` pointing to the corresponding symbol table entry. Carries per-kernel resource descriptors: register count, barrier count, stack sizes, parameter bank layout, and instruction-offset tables.

Both section variants use `sh_type = SHT_CUDA_INFO` (`0x70000000`). The ELF section type is the authoritative way to identify `.nv.info` sections; the name is only a convention.

## Complete Code Table

All 97 EIATTR codes in numeric order. Extracted from the ptxas pointer table at VA `0x23FDC20`. The "Format" column reflects the typical TLV format byte used when emitting that attribute. The "Meta" column shows the metadata word from the pointer table (lo word encodes minimum toolkit version compatibility, hi word encodes flags).

| Code | Hex | Name | Format | Meta | Category |
|---:|---:|---|---|---:|---|
| 0 | `0x00` | `EIATTR_ERROR` | — | 1 | Sentinel |
| 1 | `0x01` | `EIATTR_PAD` | — | 1 | Sentinel |
| 2 | `0x02` | `EIATTR_IMAGE_SLOT` | Indexed | 1 | Texture |
| 3 | `0x03` | `EIATTR_JUMPTABLE_RELOCS` | Free | 1 | Metadata |
| 4 | `0x04` | `EIATTR_CTAIDZ_USED` | Indexed | 1 | Metadata |
| 5 | `0x05` | `EIATTR_MAX_THREADS` | Indexed | 1 | Resource |
| 6 | `0x06` | `EIATTR_IMAGE_OFFSET` | Indexed | 1 | Texture |
| 7 | `0x07` | `EIATTR_IMAGE_SIZE` | Indexed | 1 | Texture |
| 8 | `0x08` | `EIATTR_TEXTURE_NORMALIZED` | Indexed | 1 | Texture |
| 9 | `0x09` | `EIATTR_SAMPLER_INIT` | Indexed | 1 | Texture |
| 10 | `0x0A` | `EIATTR_PARAM_CBANK` | Indexed | 1 | Param |
| 11 | `0x0B` | `EIATTR_SMEM_PARAM_OFFSETS` | Free | 1 | Param |
| 12 | `0x0C` | `EIATTR_CBANK_PARAM_OFFSETS` | Free | 1 | Param |
| 13 | `0x0D` | `EIATTR_SYNC_STACK` | Indexed | 1 | Metadata |
| 14 | `0x0E` | `EIATTR_TEXID_SAMPID_MAP` | Free | 1 | Texture |
| 15 | `0x0F` | `EIATTR_EXTERNS` | Free | 1 | Metadata |
| 16 | `0x10` | `EIATTR_REQNTID` | Indexed | 1 | Resource |
| 17 | `0x11` | `EIATTR_FRAME_SIZE` | Indexed | 1 | Resource |
| 18 | `0x12` | `EIATTR_MIN_STACK_SIZE` | Indexed | 1 | Resource |
| 19 | `0x13` | `EIATTR_SAMPLER_FORCE_UNNORMALIZED` | Indexed | 1 | Texture |
| 20 | `0x14` | `EIATTR_BINDLESS_IMAGE_OFFSETS` | Free | 1 | Texture |
| 21 | `0x15` | `EIATTR_BINDLESS_TEXTURE_BANK` | Indexed | 1 | Texture |
| 22 | `0x16` | `EIATTR_BINDLESS_SURFACE_BANK` | Indexed | 1 | Texture |
| 23 | `0x17` | `EIATTR_KPARAM_INFO` | Free | 1 | Param |
| 24 | `0x18` | `EIATTR_SMEM_PARAM_SIZE` | Indexed | 1 | Param |
| 25 | `0x19` | `EIATTR_CBANK_PARAM_SIZE` | Sized | 1 | Param |
| 26 | `0x1A` | `EIATTR_QUERY_NUMATTRIB` | Indexed | 1 | Metadata |
| 27 | `0x1B` | `EIATTR_MAXREG_COUNT` | Sized | 1 | Resource |
| 28 | `0x1C` | `EIATTR_EXIT_INSTR_OFFSETS` | Free | 1 | Offsets |
| 29 | `0x1D` | `EIATTR_S2RCTAID_INSTR_OFFSETS` | Free | 1 | Offsets |
| 30 | `0x1E` | `EIATTR_CRS_STACK_SIZE` | Indexed | 1 | Resource |
| 31 | `0x1F` | `EIATTR_NEED_CNP_WRAPPER` | Indexed | 1 | Metadata |
| 32 | `0x20` | `EIATTR_NEED_CNP_PATCH` | Indexed | 1 | Metadata |
| 33 | `0x21` | `EIATTR_EXPLICIT_CACHING` | Indexed | 1 | Metadata |
| 34 | `0x22` | `EIATTR_ISTYPEP_USED` | Indexed | 1 | Metadata |
| 35 | `0x23` | `EIATTR_MAX_STACK_SIZE` | Indexed | 1 | Resource |
| 36 | `0x24` | `EIATTR_SUQ_USED` | Indexed | 1 | Metadata |
| 37 | `0x25` | `EIATTR_LD_CACHEMOD_INSTR_OFFSETS` | Free | 1 | Offsets |
| 38 | `0x26` | `EIATTR_LOAD_CACHE_REQUEST` | Indexed | 1 | Metadata |
| 39 | `0x27` | `EIATTR_ATOM_SYS_INSTR_OFFSETS` | Free | 1 | Offsets |
| 40 | `0x28` | `EIATTR_COOP_GROUP_INSTR_OFFSETS` | Free | 1 | Offsets |
| 41 | `0x29` | `EIATTR_COOP_GROUP_MASK_REGIDS` | Indexed | 1 | Cluster |
| 42 | `0x2A` | `EIATTR_SW1850030_WAR` | Free | 1 | WAR |
| 43 | `0x2B` | `EIATTR_WMMA_USED` | Indexed | 2 | Metadata |
| 44 | `0x2C` | `EIATTR_HAS_PRE_V10_OBJECT` | Value | 3 | Metadata |
| 45 | `0x2D` | `EIATTR_ATOMF16_EMUL_INSTR_OFFSETS` | Free | 3 | Offsets |
| 46 | `0x2E` | `EIATTR_ATOM16_EMUL_INSTR_REG_MAP` | Free | 5 | Offsets |
| 47 | `0x2F` | `EIATTR_REGCOUNT` | Indexed | 5 | Resource |
| 48 | `0x30` | `EIATTR_SW2393858_WAR` | Free | 5 | WAR |
| 49 | `0x31` | `EIATTR_INT_WARP_WIDE_INSTR_OFFSETS` | Free | 5 | Offsets |
| 50 | `0x32` | `EIATTR_SHARED_SCRATCH` | Indexed | 5 | Shared |
| 51 | `0x33` | `EIATTR_STATISTICS` | Free | 5 | Metadata |
| 52 | `0x34` | `EIATTR_INDIRECT_BRANCH_TARGETS` | Free | 5 | Offsets |
| 53 | `0x35` | `EIATTR_SW2861232_WAR` | Free | 5 | WAR |
| 54 | `0x36` | `EIATTR_SW_WAR` | Free | 5 | WAR |
| 55 | `0x37` | `EIATTR_CUDA_API_VERSION` | Indexed | 5 | Metadata |
| 56 | `0x38` | `EIATTR_NUM_MBARRIERS` | Indexed | 5 | Resource |
| 57 | `0x39` | `EIATTR_MBARRIER_INSTR_OFFSETS` | Free | 5 | Offsets |
| 58 | `0x3A` | `EIATTR_COROUTINE_RESUME_OFFSETS` | Free | 5 | Offsets |
| 59 | `0x3B` | `EIATTR_SAM_REGION_STACK_SIZE` | Indexed | 5 | Resource |
| 60 | `0x3C` | `EIATTR_PER_REG_TARGET_PERF_STATS` | Free | 5 | Metadata |
| 61 | `0x3D` | `EIATTR_CTA_PER_CLUSTER` | Indexed | 5 | Cluster |
| 62 | `0x3E` | `EIATTR_EXPLICIT_CLUSTER` | Indexed | 5 | Cluster |
| 63 | `0x3F` | `EIATTR_MAX_CLUSTER_RANK` | Indexed | 5 | Cluster |
| 64 | `0x40` | `EIATTR_INSTR_REG_MAP` | Free | 5 | Metadata |
| 65 | `0x41` | `EIATTR_RESERVED_SMEM_USED` | Indexed | 5 | Shared |
| 66 | `0x42` | `EIATTR_RESERVED_SMEM_0_SIZE` | Indexed | 5 | Shared |
| 67 | `0x43` | `EIATTR_UCODE_SECTION_DATA` | Free | 5 | Metadata |
| 68 | `0x44` | `EIATTR_UNUSED_LOAD_BYTE_OFFSET` | Free | 5 | Offsets |
| 69 | `0x45` | `EIATTR_KPARAM_INFO_V2` | Free | 5 | Param |
| 70 | `0x46` | `EIATTR_SYSCALL_OFFSETS` | Free | 5 | Offsets |
| 71 | `0x47` | `EIATTR_SW_WAR_MEMBAR_SYS_INSTR_OFFSETS` | Free | 5 | WAR |
| 72 | `0x48` | `EIATTR_GRAPHICS_GLOBAL_CBANK` | Indexed | 5 | Graphics |
| 73 | `0x49` | `EIATTR_SHADER_TYPE` | Indexed | 5 | Graphics |
| 74 | `0x4A` | `EIATTR_VRC_CTA_INIT_COUNT` | Indexed | 5 | Graphics |
| 75 | `0x4B` | `EIATTR_TOOLS_PATCH_FUNC` | Indexed | 5 | Metadata |
| 76 | `0x4C` | `EIATTR_NUM_BARRIERS` | Indexed | 5 | Resource |
| 77 | `0x4D` | `EIATTR_TEXMODE_INDEPENDENT` | Indexed | 5 | Texture |
| 78 | `0x4E` | `EIATTR_PERF_STATISTICS` | Free | 5 | Metadata |
| 79 | `0x4F` | `EIATTR_AT_ENTRY_FRAGEMENTS` | Free | 5 | Blackwell |
| 80 | `0x50` | `EIATTR_SPARSE_MMA_MASK` | Free | 5 | Blackwell |
| 81 | `0x51` | `EIATTR_TCGEN05_1CTA_USED` | Indexed | 5 | Blackwell |
| 82 | `0x52` | `EIATTR_TCGEN05_2CTA_USED` | Indexed | 5 | Blackwell |
| 83 | `0x53` | `EIATTR_GEN_ERRBAR_AT_EXIT` | Indexed | 5 | Blackwell |
| 84 | `0x54` | `EIATTR_REG_RECONFIG` | Indexed | 5 | Blackwell |
| 85 | `0x55` | `EIATTR_ANNOTATIONS` | Free | 5 | Metadata |
| 86 | `0x56` | `EIATTR_UNKNOWN` | — | 5 | Sentinel |
| 87 | `0x57` | `EIATTR_STACK_CANARY_TRAP_OFFSETS` | Free | 5 | Offsets |
| 88 | `0x58` | `EIATTR_STUB_FUNCTION_KIND` | Indexed | 5 | Metadata |
| 89 | `0x59` | `EIATTR_LOCAL_CTA_ASYNC_STORE_OFFSETS` | Free | 5 | Offsets |
| 90 | `0x5A` | `EIATTR_MERCURY_FINALIZER_OPTIONS` | Free | 5 | Mercury |
| 91 | `0x5B` | `EIATTR_BLOCKS_ARE_CLUSTERS` | Indexed | 5 | Cluster |
| 92 | `0x5C` | `EIATTR_SANITIZE` | Indexed | 5 | Blackwell |
| 93 | `0x5D` | `EIATTR_SYSCALLS_FALLBACK` | Free | 5 | Metadata |
| 94 | `0x5E` | `EIATTR_CUDA_REQ` | Free | 5 | Metadata |
| 95 | `0x5F` | `EIATTR_MERCURY_ISA_VERSION` | Sized | 5 | Mercury |
| 96 | `0x60` | `EIATTR_ERROR_LAST` | — | 5 | Sentinel |

### Metadata Word Encoding

Each entry in the pointer table carries an 8-byte metadata word alongside the string pointer. The low 32 bits encode the minimum toolkit version required to parse this attribute. The high 32 bits encode flags (0 = legacy, 1 = internal-only, 2 = standard).

| Meta lo | Interpretation |
|---:|---|
| 1 | Legacy attribute, present since earliest CUDA versions |
| 2 | Introduced in CUDA ~7.0 era (Volta) |
| 3 | Introduced in CUDA ~9.0 era (Turing) |
| 5 | Introduced in CUDA ~11.0+ era (Ampere and later) |

Codes 0–42 all carry `meta=1` (legacy). The boundary at code 43 (`EIATTR_WMMA_USED`) marks the Volta-era expansion. Codes 46+ carry `meta_lo=5`, indicating the major expansion that happened with Ampere and continued through Blackwell.

## Attribute Categories

### Resource Allocation (GPU Driver Critical)

These attributes directly control how the GPU driver allocates hardware resources for kernel launch. Incorrect values cause silent performance degradation or launch failure.

| Code | Hex | Name | Format | Description |
|---:|---:|---|---|---|
| 47 | `0x2F` | `EIATTR_REGCOUNT` | Indexed | Physical register count per thread. The GPU driver computes `max_warps_per_SM = total_registers / (regcount * warp_size)`. Single most important occupancy-determining attribute. |
| 5 | `0x05` | `EIATTR_MAX_THREADS` | Indexed | Maximum threads per block (from `.maxntid` PTX directive). |
| 16 | `0x10` | `EIATTR_REQNTID` | Indexed | Required thread count per dimension (from `.reqntid`). |
| 17 | `0x11` | `EIATTR_FRAME_SIZE` | Indexed | Per-thread local memory frame size in bytes. |
| 18 | `0x12` | `EIATTR_MIN_STACK_SIZE` | Indexed | Minimum stack size per thread (non-recursive case). |
| 35 | `0x23` | `EIATTR_MAX_STACK_SIZE` | Indexed | Maximum stack size per thread (recursive case, computed via call graph propagation). |
| 30 | `0x1E` | `EIATTR_CRS_STACK_SIZE` | Indexed | Call-Return-Stack size for nested function calls. |
| 59 | `0x3B` | `EIATTR_SAM_REGION_STACK_SIZE` | Indexed | SAM (Streaming Asynchronous Memory) region stack size. |
| 76 | `0x4C` | `EIATTR_NUM_BARRIERS` | Indexed | Number of named barriers used (max 16 on most architectures). Propagated from callees to entry points by `sub_1CC8950`. |
| 56 | `0x38` | `EIATTR_NUM_MBARRIERS` | Indexed | Number of memory barriers (mbarrier objects) used. |
| 27 | `0x1B` | `EIATTR_MAXREG_COUNT` | Sized | Maximum register count hint (from `--maxrregcount` or `.maxnreg`). |
| 84 | `0x54` | `EIATTR_REG_RECONFIG` | Indexed | Dynamic register reconfiguration support (`setmaxnreg` instruction, sm_100+). |

### Parameter Bank Layout

Describes how kernel parameters are laid out in constant memory bank 0 (`c[0x0]`).

| Code | Hex | Name | Format | Description |
|---:|---:|---|---|---|
| 10 | `0x0A` | `EIATTR_PARAM_CBANK` | Indexed | Constant bank number and offset for kernel parameters. |
| 25 | `0x19` | `EIATTR_CBANK_PARAM_SIZE` | Sized | Size of the parameter constant bank in bytes. |
| 24 | `0x18` | `EIATTR_SMEM_PARAM_SIZE` | Indexed | Size of shared memory parameter region. |
| 11 | `0x0B` | `EIATTR_SMEM_PARAM_OFFSETS` | Free | Offsets of parameters within shared memory. |
| 12 | `0x0C` | `EIATTR_CBANK_PARAM_OFFSETS` | Free | Offsets of parameters within constant bank. |
| 23 | `0x17` | `EIATTR_KPARAM_INFO` | Free | Kernel parameter metadata (types, sizes, alignments). |
| 69 | `0x45` | `EIATTR_KPARAM_INFO_V2` | Free | Extended kernel parameter info (v2 format with additional fields, no metadata version constraint). |

### Instruction Offset Tables

Record byte offsets of specific instruction types within the kernel's `.text` section, enabling the driver and tools to locate and patch instructions at load time.

| Code | Hex | Name | Format | Description |
|---:|---:|---|---|---|
| 28 | `0x1C` | `EIATTR_EXIT_INSTR_OFFSETS` | Free | Byte offsets of all `EXIT` instructions. |
| 29 | `0x1D` | `EIATTR_S2RCTAID_INSTR_OFFSETS` | Free | Offsets of `S2R` instructions reading `SR_CTAID` (CTA ID). Used for cluster launch CTA-ID remapping. |
| 37 | `0x25` | `EIATTR_LD_CACHEMOD_INSTR_OFFSETS` | Free | Offsets of load instructions with explicit cache modifier. |
| 39 | `0x27` | `EIATTR_ATOM_SYS_INSTR_OFFSETS` | Free | Offsets of atomic instructions with `.sys` scope. |
| 40 | `0x28` | `EIATTR_COOP_GROUP_INSTR_OFFSETS` | Free | Offsets of cooperative group instructions. |
| 45 | `0x2D` | `EIATTR_ATOMF16_EMUL_INSTR_OFFSETS` | Free | Offsets of emulated FP16 atomic instructions. |
| 46 | `0x2E` | `EIATTR_ATOM16_EMUL_INSTR_REG_MAP` | Free | Register map for 16-bit atomic emulation. |
| 49 | `0x31` | `EIATTR_INT_WARP_WIDE_INSTR_OFFSETS` | Free | Offsets of integer warp-wide instructions. |
| 52 | `0x34` | `EIATTR_INDIRECT_BRANCH_TARGETS` | Free | Valid targets of indirect branches (for control flow integrity). |
| 57 | `0x39` | `EIATTR_MBARRIER_INSTR_OFFSETS` | Free | Offsets of `MBAR` (memory barrier) instructions. |
| 58 | `0x3A` | `EIATTR_COROUTINE_RESUME_OFFSETS` | Free | Resume point offsets for device-side coroutines. Variant name `EIATTR_COROUTINE_RESUME_ID_OFFSETS` at `0x24064D8`. |
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
| 62 | `0x3E` | `EIATTR_EXPLICIT_CLUSTER` | Indexed | Kernel uses explicit cluster dimensions. |
| 63 | `0x3F` | `EIATTR_MAX_CLUSTER_RANK` | Indexed | Maximum cluster rank for scheduling. |
| 91 | `0x5B` | `EIATTR_BLOCKS_ARE_CLUSTERS` | Indexed | CTA blocks are clusters flag. |

### Shared Memory and Reserved Resources

| Code | Hex | Name | Format | Description |
|---:|---:|---|---|---|
| 50 | `0x32` | `EIATTR_SHARED_SCRATCH` | Indexed | Shared memory scratch space for register spilling. |
| 65 | `0x41` | `EIATTR_RESERVED_SMEM_USED` | Indexed | Whether reserved shared memory is used. |
| 66 | `0x42` | `EIATTR_RESERVED_SMEM_0_SIZE` | Indexed | Size of reserved shared memory partition 0. |

### Software Workarounds

Hardware errata requiring instruction-level patching by the driver. Each WAR attribute carries a list of instruction byte offsets that the driver must modify at kernel load time.

| Code | Hex | Name | Format | Description |
|---:|---:|---|---|---|
| 42 | `0x2A` | `EIATTR_SW1850030_WAR` | Free | Workaround for HW bug 1850030. |
| 48 | `0x30` | `EIATTR_SW2393858_WAR` | Free | Workaround for HW bug 2393858. |
| 53 | `0x35` | `EIATTR_SW2861232_WAR` | Free | Workaround for HW bug 2861232. |
| 54 | `0x36` | `EIATTR_SW_WAR` | Free | Generic software workaround container. |
| 71 | `0x47` | `EIATTR_SW_WAR_MEMBAR_SYS_INSTR_OFFSETS` | Free | Offsets of `MEMBAR.SYS` instructions needing software workaround. |

### Graphics-Specific

| Code | Hex | Name | Format | Description |
|---:|---:|---|---|---|
| 72 | `0x48` | `EIATTR_GRAPHICS_GLOBAL_CBANK` | Indexed | Global constant bank for graphics shaders. |
| 73 | `0x49` | `EIATTR_SHADER_TYPE` | Indexed | Shader type (vertex, fragment, compute, etc.). |
| 74 | `0x4A` | `EIATTR_VRC_CTA_INIT_COUNT` | Indexed | Virtual Register Count CTA init count. |

### Blackwell+ Features (sm_100+)

| Code | Hex | Name | Format | Description |
|---:|---:|---|---|---|
| 79 | `0x4F` | `EIATTR_AT_ENTRY_FRAGEMENTS` | Free | Fragment descriptors at function entry. Note: "FRAGEMENTS" is a typo preserved in the binary; corrected variant `EIATTR_AT_ENTRY_FRAGMENTS` exists at `0x2405DA1`. |
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
| 55 | `0x37` | `EIATTR_CUDA_API_VERSION` | Indexed | CUDA API version the kernel was compiled for. |
| 60 | `0x3C` | `EIATTR_PER_REG_TARGET_PERF_STATS` | Free | Per-register-target performance statistics. |
| 64 | `0x40` | `EIATTR_INSTR_REG_MAP` | Free | Instruction-to-register mapping for profiling. |
| 67 | `0x43` | `EIATTR_UCODE_SECTION_DATA` | Free | Microcode section data (internal). |
| 75 | `0x4B` | `EIATTR_TOOLS_PATCH_FUNC` | Indexed | Function patching descriptor for CUDA tools (cuda-gdb, Nsight). |
| 78 | `0x4E` | `EIATTR_PERF_STATISTICS` | Free | Performance statistics for the profiler. |
| 85 | `0x55` | `EIATTR_ANNOTATIONS` | Free | General-purpose annotation data. |
| 88 | `0x58` | `EIATTR_STUB_FUNCTION_KIND` | Indexed | Stub function classification. |
| 93 | `0x5D` | `EIATTR_SYSCALLS_FALLBACK` | Free | Syscall fallback mechanism offsets. |
| 94 | `0x5E` | `EIATTR_CUDA_REQ` | Free | CUDA requirements descriptor. |

### Sentinel and Error

| Code | Hex | Name | Format | Description |
|---:|---:|---|---|---|
| 0 | `0x00` | `EIATTR_ERROR` | — | Invalid/error sentinel. Never emitted in valid cubins. |
| 1 | `0x01` | `EIATTR_PAD` | — | Padding record (ignored by parser). |
| 86 | `0x56` | `EIATTR_UNKNOWN` | — | Unknown attribute placeholder. |
| 96 | `0x60` | `EIATTR_ERROR_LAST` | — | Upper bound sentinel for the enum range. Code 96 is never emitted; it serves as a bound check (`if (attr_code > 0x2F)` at line 760 of the builder). |

## Payload Format Reference (Codes 0–32)

Per-attribute wire-format documentation derived from `sub_1CC9800` (master EIATTR builder), `sub_1CC86D0` (per-entry stack emitter), `sub_1CC8950` (barrier/register propagator), and `sub_1CC85F0` (TLV record emitter). Payload layouts describe the bytes that follow the 4-byte TLV header.

For Indexed-format (`0x04`) attributes the first 4 payload bytes are always a u32 symbol index. The remaining bytes (if any) carry the value. For Sized-format (`0x03`) attributes the value is encoded directly in the 16-bit size field of the TLV header — there are no additional payload bytes.

### Sentinel Codes (0–1)

| Code | Hex | Name | Payload |
|---:|---:|---|---|
| 0 | `0x00` | `EIATTR_ERROR` | None. Never emitted. |
| 1 | `0x01` | `EIATTR_PAD` | None. Padding, ignored by parser. |

### Texture and Image Binding (2, 6–9, 14, 19–22)

All Indexed attributes in this group share the same 8-byte payload layout: `[sym_index:4][value:4]`. The builder's first switch (line 722) routes all of these through the same symbol-index resolution path.

```text
Offset  Size  Field
------  ----  -----
0x00    4     sym_index     Per-function symbol table index
0x04    4     value         Attribute-specific (see per-code table)
```

| Code | Hex | Name | `value` field semantics |
|---:|---:|---|---|
| 2 | `0x02` | `EIATTR_IMAGE_SLOT` | Image slot number (texture unit binding point) |
| 6 | `0x06` | `EIATTR_IMAGE_OFFSET` | Byte offset within image descriptor table |
| 7 | `0x07` | `EIATTR_IMAGE_SIZE` | Image descriptor size in bytes |
| 8 | `0x08` | `EIATTR_TEXTURE_NORMALIZED` | `0` = unnormalized, `1` = normalized coordinates |
| 9 | `0x09` | `EIATTR_SAMPLER_INIT` | Packed sampler initialization parameters |
| 19 | `0x13` | `EIATTR_SAMPLER_FORCE_UNNORMALIZED` | Sampler ID to force unnormalized |
| 21 | `0x15` | `EIATTR_BINDLESS_TEXTURE_BANK` | Constant bank ID for bindless texture descriptors |
| 22 | `0x16` | `EIATTR_BINDLESS_SURFACE_BANK` | Constant bank ID for bindless surface descriptors |

**Code 14 (`0x0E`) — `EIATTR_TEXID_SAMPID_MAP`**: Free format. Variable-length array of u32 pairs mapping texture IDs to sampler IDs.

```text
Payload: repeating [tex_id:4][samp_id:4] pairs
Size:    N * 8 bytes (N = number of tex-sampler bindings)
```

**Code 20 (`0x14`) — `EIATTR_BINDLESS_IMAGE_OFFSETS`**: Free format. Array of u32 byte offsets for bindless image descriptor references in the kernel's constant bank. Each u32 is a symbol index that gets resolved during link.

```text
Payload: u32[] symbol indices (resolved to byte offsets at link)
Size:    N * 4 bytes
```

### Jump Table Relocations (3)

**Code 3 (`0x03`) — `EIATTR_JUMPTABLE_RELOCS`**: Free format. Array of u32 byte offsets into the `.text` section where jump table relocations are needed.

```text
Payload: u32[] byte offsets into .text
Size:    N * 4 bytes
```

### CTAIDZ Flag (4)

**Code 4 (`0x04`) — `EIATTR_CTAIDZ_USED`**: Indexed format, zero-value flag attribute. Presence of the record signals the kernel reads `%ctaid.z`. SM-version gated via `sub_1C97840(0x04, sm_version)`.

```text
Offset  Size  Field
------  ----  -----
0x00    4     sym_index     Per-function symbol
(no value field -- presence is the signal)
```

The builder creates this record with two different format bytes depending on context: `0x04` (Indexed) via the TLV emitter, or `0x01` (Free) via inline construction (magic `0x0401`). Both encode the same semantic: flag-only, no value.

### Resource Allocation (5, 16–18, 25, 27, 30)

**Codes 5, 16, 17, 18** — Indexed, 8-byte payload `[sym_index:4][value:4]`:

| Code | Hex | Name | `value` field semantics |
|---:|---:|---|---|
| 5 | `0x05` | `EIATTR_MAX_THREADS` | Maximum threads per block (from `.maxntid`) |
| 16 | `0x10` | `EIATTR_REQNTID` | Required thread count per dimension (from `.reqntid`) |
| 17 | `0x11` | `EIATTR_FRAME_SIZE` | Per-thread local memory frame size in bytes |
| 18 | `0x12` | `EIATTR_MIN_STACK_SIZE` | Minimum per-thread stack size in bytes |

`EIATTR_FRAME_SIZE` is weak-symbol filtered: dropped when a weak function is replaced by a stronger definition (bitmask `0x800800020000`).

`EIATTR_MIN_STACK_SIZE` is emitted by `sub_1CC86D0` with `sub_1CC85F0(a1, 0x12, 8, buf, 0)` where `buf` is `[sym_index:4][min_stack:4]`. A sentinel value of `-1` in `min_stack` means "not yet computed." When `sm_version == 0xFF00` (Mercury), the record is suppressed.

**Code 25 (`0x19`) — `EIATTR_CBANK_PARAM_SIZE`**: Sized format (`0x03`). Value encoded directly in the 16-bit size field. No separate payload bytes.

```text
TLV header: [fmt=0x03][code=0x19][param_bank_size:2]
Total record: 4 bytes (header only)
```

**Code 27 (`0x1B`) — `EIATTR_MAXREG_COUNT`**: Sized format (`0x03`). Value encoded in the low byte of the 16-bit size field (range 0–255). Per-compilation-unit hint, not per-function. Set by `--maxrregcount` CLI flag or `.maxnreg` PTX directive.

```text
TLV header: [fmt=0x03][code=0x1B][maxreg:2]
Total record: 4 bytes (header only)
Effective range: low byte only (0–255), high byte 0
```

Binary evidence: second switch case `0x1B` (line 1094) reads `*(u8*)(v150+2)` — the low byte of the size field — as the register count value.

**Code 30 (`0x1E`) — `EIATTR_CRS_STACK_SIZE`**: Indexed format, 4-byte value payload. Emitted by `sub_1CC86D0` with `sub_1CC85F0(a1, 0x1E, 4, buf, sym_index)`.

```text
Offset  Size  Field
------  ----  -----
0x00    4     sym_index     Per-function symbol
0x04    4     crs_bytes     Call-Return-Stack size in bytes
```

Total record: 12 bytes (4 header + 8 payload). Diagnostic `"conflicting crs_stack attribute"` fires when two records target the same function.

### Parameter Bank Layout (10–12, 23–24)

**Code 10 (`0x0A`) — `EIATTR_PARAM_CBANK`**: Indexed format, packed value.

```text
Offset  Size  Field
------  ----  -----
0x00    4     sym_index
0x04    4     cbank_desc    lo16 = bank number, hi16 = byte offset
```

Typical value: bank=0, offset=`0x160` (standard CUDA kernel parameter ABI).

**Codes 11 (`0x0B`) and 12 (`0x0C`)** — Free format, variable-length u32 arrays:

`EIATTR_SMEM_PARAM_OFFSETS` (`0x0B`):
```text
Payload: u32[] byte offsets within shared memory, one per parameter
Size:    N * 4 bytes
```

`EIATTR_CBANK_PARAM_OFFSETS` (`0x0C`):
```text
Payload: u32[] packed entries, one per parameter
         Each u32: lo16 = byte offset in cbank, hi16 = parameter size
Size:    N * 4 bytes
```

**Code 23 (`0x17`) — `EIATTR_KPARAM_INFO`**: Free format, complex per-parameter descriptors. This is the only attribute in codes 0–32 with a multi-field sub-record structure.

```text
Payload: repeating 12-byte per-parameter entries:
  Offset  Size  Field
  ------  ----  -----
  0x00    4     param_index       Ordinal position (0-based)
  0x04    4     param_offset      Byte offset in constant bank
  0x08    2     param_size        Size in bytes
  0x0A    1     log_alignment     log2(alignment)
  0x0B    1     flags             Bit flags (pointer, ordinal, etc.)
Size: N * 12 bytes
```

Special behavior: the builder exempts `KPARAM_INFO` from being zeroed when its symbol index resolves to 0 (line 755: `(_BYTE)v5 == 23` check). This allows global-scope parameter info records.

**Code 24 (`0x18`) — `EIATTR_SMEM_PARAM_SIZE`**: Indexed, `[sym_index:4][smem_param_bytes:4]`.

### Synchronization (13)

**Code 13 (`0x0D`) — `EIATTR_SYNC_STACK`**: Indexed format.

```text
Offset  Size  Field
------  ----  -----
0x00    4     sym_index
0x04    4     sync_depth    lo16 = stack depth (u16), hi16 = 0
```

Binary evidence: case `0x0D` (line 1038) reads `*(u16**)(v150+8)` as a pointer to a u16 value. The depth value (`v343`) is a 16-bit unsigned integer. Used with `sub_1CBD8F0` for sync stack tracking.

### External Symbol References (15)

**Code 15 (`0x0F`) — `EIATTR_EXTERNS`**: Free format, most complex processing of any attribute in the 0–32 range.

```text
Payload: u32[] symbol table indices
Size:    N * 4 bytes (N = size_field / 4)
```

The builder handles EXTERNS in both switches:
- First switch (line 779): iterates the u32 array, resolving each symbol index through the link-time symbol table. Dead symbols (resolved to 0) are zeroed in-place.
- Second switch (line 1054): collects extern refs into a set (`v643`) for the current function.
- Emission (line 1706): `sub_1CC85F0(a1, 0x0F, 4*count, buf, sym_index)` emits the final record.
- The size field encodes `N * 4` and the element count is recovered as `size >> 2`.

### Metadata Query (26)

**Code 26 (`0x1A`) — `EIATTR_QUERY_NUMATTRIB`**: Indexed, `[sym_index:4][num_attributes:4]`.

### Instruction Offset Tables (28–29)

Both attributes are Free format carrying arrays of u32 byte offsets into the `.text` section.

**Code 28 (`0x1C`) — `EIATTR_EXIT_INSTR_OFFSETS`**:
```text
Payload: u32[] byte offsets of EXIT instructions
Size:    N * 4 bytes
```

Confirmed by the builder's loop (line 2011): code 28 is explicitly checked and skipped past the symbol-resolution path, confirming the payload is a simple offset array with no embedded symbol indices.

**Code 29 (`0x1D`) — `EIATTR_S2RCTAID_INSTR_OFFSETS`**:
```text
Payload: u32[] byte offsets of S2R SR_CTAID.* instructions
Size:    N * 4 bytes
```

At line 2001, code 29 triggers CNP (CUDA Nested Parallelism) wrapper generation. The symbol index from the record is added to the CNP wrapper list, driving emission of `NEED_CNP_WRAPPER` (code 31) and `NEED_CNP_PATCH` (code 32) records.

### CUDA Nested Parallelism Flags (31–32)

Both are Indexed-format flag attributes with no value payload. They are always emitted as a pair.

**Code 31 (`0x1F`) — `EIATTR_NEED_CNP_WRAPPER`**:
```text
Offset  Size  Field
------  ----  -----
0x00    4     sym_index
(no value -- flag only, presence is the signal)
```

SM-version gated: `sub_1C97840(0x1F, sm_version)`. Builder constructs with internal format `0x01` (magic `0x1F01` = 7937). Emitted for every function that the S2RCTAID analysis identified as needing a CNP wrapper.

**Code 32 (`0x20`) — `EIATTR_NEED_CNP_PATCH`**:
```text
Offset  Size  Field
------  ----  -----
0x00    4     sym_index
(no value -- flag only, presence is the signal)
```

SM-version gated: `sub_1C97840(0x20, sm_version)`. Builder constructs with internal format `0x01` (magic `0x2001` = 8193). Emitted for every function in the CNP call tree.

### Payload Format Summary (Codes 0–32)

| Code | Name | Wire Fmt | Payload size | Payload layout |
|---:|---|---:|---:|---|
| 0 | `ERROR` | — | 0 | none |
| 1 | `PAD` | — | 0 | none |
| 2 | `IMAGE_SLOT` | `0x04` | 8 | `[sym:4][slot_id:4]` |
| 3 | `JUMPTABLE_RELOCS` | `0x01` | N*4 | `u32[]` byte offsets |
| 4 | `CTAIDZ_USED` | `0x04` | 4 | `[sym:4]` flag-only |
| 5 | `MAX_THREADS` | `0x04` | 8 | `[sym:4][max_threads:4]` |
| 6 | `IMAGE_OFFSET` | `0x04` | 8 | `[sym:4][offset:4]` |
| 7 | `IMAGE_SIZE` | `0x04` | 8 | `[sym:4][size:4]` |
| 8 | `TEXTURE_NORMALIZED` | `0x04` | 8 | `[sym:4][normalized:4]` |
| 9 | `SAMPLER_INIT` | `0x04` | 8 | `[sym:4][params:4]` |
| 10 | `PARAM_CBANK` | `0x04` | 8 | `[sym:4][lo16=bank,hi16=off:4]` |
| 11 | `SMEM_PARAM_OFFSETS` | `0x01` | N*4 | `u32[]` param offsets |
| 12 | `CBANK_PARAM_OFFSETS` | `0x01` | N*4 | `u32[]` `lo16=off,hi16=size` |
| 13 | `SYNC_STACK` | `0x04` | 8 | `[sym:4][depth_u16:4]` |
| 14 | `TEXID_SAMPID_MAP` | `0x01` | N*8 | `[tex_id:4][samp_id:4]` pairs |
| 15 | `EXTERNS` | `0x01` | N*4 | `u32[]` symbol indices |
| 16 | `REQNTID` | `0x04` | 8 | `[sym:4][reqntid:4]` |
| 17 | `FRAME_SIZE` | `0x04` | 8 | `[sym:4][frame_bytes:4]` |
| 18 | `MIN_STACK_SIZE` | `0x04` | 8 | `[sym:4][stack_bytes:4]` |
| 19 | `SAMPLER_FORCE_UNNORM` | `0x04` | 8 | `[sym:4][sampler_id:4]` |
| 20 | `BINDLESS_IMAGE_OFFSETS` | `0x01` | N*4 | `u32[]` sym indices |
| 21 | `BINDLESS_TEXTURE_BANK` | `0x04` | 8 | `[sym:4][bank_id:4]` |
| 22 | `BINDLESS_SURFACE_BANK` | `0x04` | 8 | `[sym:4][bank_id:4]` |
| 23 | `KPARAM_INFO` | `0x01` | N*12 | 12B per-param descriptors |
| 24 | `SMEM_PARAM_SIZE` | `0x04` | 8 | `[sym:4][size_bytes:4]` |
| 25 | `CBANK_PARAM_SIZE` | `0x03` | 0 | value in TLV size field |
| 26 | `QUERY_NUMATTRIB` | `0x04` | 8 | `[sym:4][count:4]` |
| 27 | `MAXREG_COUNT` | `0x03` | 0 | value in TLV size field (u8) |
| 28 | `EXIT_INSTR_OFFSETS` | `0x01` | N*4 | `u32[]` .text byte offsets |
| 29 | `S2RCTAID_INSTR_OFFSETS` | `0x01` | N*4 | `u32[]` .text byte offsets |
| 30 | `CRS_STACK_SIZE` | `0x04` | 8 | `[sym:4][crs_bytes:4]` |
| 31 | `NEED_CNP_WRAPPER` | `0x04` | 4 | `[sym:4]` flag-only |
| 32 | `NEED_CNP_PATCH` | `0x04` | 4 | `[sym:4]` flag-only |

## Payload Format Reference (Codes 33–64)

Continuation of the per-attribute wire-format documentation. Same sources and conventions as the 0–32 section above.

### Metadata Flags (33–34, 36, 43)

**Code 33 (`0x21`) — `EIATTR_EXPLICIT_CACHING`**: Indexed format, flag-only. Signals the kernel uses explicit cache control directives (`ld.ca`, `ld.cg`, etc.). SM-gated via `sub_1C97840(0x21, sm_version)`.

```text
Offset  Size  Field
------  ----  -----
0x00    4     sym_index
(no value -- flag only)
```

Binary evidence: magic `0x2101` (line 1733). Emitted when cache-on flag (`v648`) is set. When both cache-on and cache-off flags are set simultaneously (conflicting directives), `sub_1CC8100` (cache conflict resolver) is called instead of emitting this record. The diagnostic `"Turning caching %s for entry '%s' as per its request"` logs cache resolution decisions.

**Code 34 (`0x22`) — `EIATTR_ISTYPEP_USED`**: Indexed format, flag-only. Signals the kernel uses `isspacep` (type predicate) instructions.

```text
Offset  Size  Field
------  ----  -----
0x00    4     sym_index
(no value -- flag only)
```

No special builder logic — passes through the default path.

**Code 36 (`0x24`) — `EIATTR_SUQ_USED`**: Indexed format, flag-only. Signals the kernel uses surface query instructions.

```text
Offset  Size  Field
------  ----  -----
0x00    4     sym_index
(no value -- flag only)
```

No special builder logic.

**Code 43 (`0x2B`) — `EIATTR_WMMA_USED`**: Indexed format, flag-only. Signals the kernel uses Warp Matrix Multiply-Accumulate instructions. First attribute introduced in the Volta era (`meta=2`).

```text
Offset  Size  Field
------  ----  -----
0x00    4     sym_index
(no value -- flag only)
```

No special builder logic.

### Resource Allocation (35, 47, 50, 55–56, 59)

**Code 35 (`0x23`) — `EIATTR_MAX_STACK_SIZE`**: Indexed format, 4-byte value. Maximum per-thread stack size for recursive call chains, computed via call-graph propagation in `sub_1CC8950`.

```text
Offset  Size  Field
------  ----  -----
0x00    4     sym_index
0x04    4     max_stack_bytes   Maximum stack size in bytes
```

Binary evidence: second switch case `0x23` (line 1128) reads `v354[1]` as the stack size value and stores it in the per-entry array `s[]`. Weak-symbol filtered: bitmask `0x800800060000` includes this code. Mercury suppression: when `sm_version == 0xFF00`, the code byte is zeroed, dropping the record.

**Code 47 (`0x2F`) — `EIATTR_REGCOUNT`**: Indexed format, 4-byte value. Physical register count per thread. The single most important attribute for GPU occupancy: `max_warps_per_SM = total_registers / (regcount * warp_size)`.

```text
Offset  Size  Field
------  ----  -----
0x00    4     sym_index
0x04    4     regcount          Physical registers per thread
```

Binary evidence: second switch case `0x2F` (line 1176) resolves the symbol and stores the record pointer in `v642[]` (per-entry regcount array). Diagnostic `"invalid index"` (line 1180) fires if the symbol resolves to null. Weak-symbol filtered: bitmask `0x800800060000` includes this code.

**Code 50 (`0x32`) — `EIATTR_SHARED_SCRATCH`**: Indexed format, 4-byte value. Shared memory scratch space allocated for register spilling.

```text
Offset  Size  Field
------  ----  -----
0x00    4     sym_index
0x04    4     scratch_bytes     Shared scratch size in bytes
```

No special builder logic.

**Code 55 (`0x37`) — `EIATTR_CUDA_API_VERSION`**: Indexed format, 4-byte value. Records the CUDA API version the kernel was compiled for.

```text
Offset  Size  Field
------  ----  -----
0x00    4     sym_index
0x04    4     api_version       CUDA API version number
```

No special builder logic — passes through the default path.

**Code 56 (`0x38`) — `EIATTR_NUM_MBARRIERS`**: Sized format (`0x03`), value encoded in the TLV size field. Number of memory barrier (`mbarrier`) objects used by the kernel.

```text
TLV header: [fmt=0x03][code=0x38][mbar_count:2]
Total record: 4 bytes (header only)
```

Binary evidence: magic `0x3803` (14339) at lines 1664 and 2446. The mbarrier count is stored in the 16-bit size field: `*((_WORD *)v511 + 1) = v651` (line 1669). SM-gated via `sub_1C97840(0x38, sm_version)` at lines 1654 and 2436.

Accumulative semantics: the builder sums mbarrier counts from callees during call-graph propagation (second switch case `0x38` at line 1183, falling through to LABEL\_331). If any callee reports `-1` (unknown), the sum stays `-1` (lines 1255–1256). The emission loop at lines 2407–2454 propagates the count to all entry points that call the function.

**Code 59 (`0x3B`) — `EIATTR_SAM_REGION_STACK_SIZE`**: Indexed format, 8-byte payload. SAM (Streaming Asynchronous Memory) region stack size.

```text
Offset  Size  Field
------  ----  -----
0x00    4     sym_index
0x04    4     sam_stack_bytes   SAM region stack size in bytes
```

Binary evidence: emitted by `sub_1CC86D0` at line 114: `sub_1CC85F0(a1, 0x3B, 8, buf, 0)` where buf is `[sym_index:4][sam_stack:4]`. Only emitted when `sub_1CBD9E0(a1, a2)` returns nonzero, indicating the kernel actually uses SAM regions. Second switch case `0x3B` (line 1186) calls `sub_1CBD940(a1, sym, value)` to record the SAM stack size.

### Cache Control (38)

**Code 38 (`0x26`) — `EIATTR_LOAD_CACHE_REQUEST`**: Indexed format, 4-byte value. Per-kernel cache mode configuration. Controls whether the driver enables explicit caching for this kernel.

```text
Offset  Size  Field
------  ----  -----
0x00    4     sym_index
0x04    4     cache_mode        0 = off, nonzero = on
```

Binary evidence: second switch case `0x26` (line 1134) is the most complex handler in this range. The builder first checks the function kind: if `(byte & 3) == 1` (device function), the record is dropped by zeroing the code byte (line 1141). For entry-point kernels, the verbose trace `"Turning caching %s for entry '%s' as per its request"` is emitted (line 1153), where `%s` is either `"OFF"` or `"ON"`. When cache\_mode is nonzero: adds the symbol to the caching-on list (`v639[]`) and sets the per-entry status to 2. When cache\_mode is zero: sets status to 1 (off). The `v648` and `v655` flags track the presence of on/off requests for conflict detection.

### Global Flags (44)

**Code 44 (`0x2C`) — `EIATTR_HAS_PRE_V10_OBJECT`**: Value format (`0x02`), global scope. Signals the compilation unit contains pre-CUDA 10 compiled code.

```text
TLV header: [fmt=0x02][code=0x2C][size:2]
Payload:    [flags:4]
Total record: 8 bytes
```

Binary evidence: top-level gating at line 686–688 checks three conditions: link mode (`v609 == 2`), toolkit version (`> 0x63`), and SM compatibility (`sub_1C97840(0x2C, sm_version)`). The magic `0x2C01` at line 709 constructs the record with internal format byte 0x01, which the emitter translates to Value format (0x02) for the wire encoding since the record is global scope. This is the only Value-format attribute in the 33–64 range.

### Instruction Offset Tables (37, 39–40, 45–46, 48–49, 52, 57–58)

All attributes in this group use Free format (`0x01`) carrying variable-length arrays of u32 byte offsets into the kernel's `.text` section. None have explicit switch cases in the builder — they pass through the default path. The payload layout for all is identical:

```text
Payload: u32[] byte offsets into .text section
Size:    N * 4 bytes (N = size_field / 4)
```

| Code | Hex | Name | Offset semantics |
|---:|---:|---|---|
| 37 | `0x25` | `LD_CACHEMOD_INSTR_OFFSETS` | Load instructions with explicit cache modifier |
| 39 | `0x27` | `ATOM_SYS_INSTR_OFFSETS` | Atomic instructions with `.sys` scope |
| 40 | `0x28` | `COOP_GROUP_INSTR_OFFSETS` | Cooperative group instructions |
| 45 | `0x2D` | `ATOMF16_EMUL_INSTR_OFFSETS` | Emulated FP16 atomic instructions |
| 48 | `0x30` | `SW2393858_WAR` | HW bug 2393858 patch locations |
| 49 | `0x31` | `INT_WARP_WIDE_INSTR_OFFSETS` | Integer warp-wide instructions |
| 52 | `0x34` | `INDIRECT_BRANCH_TARGETS` | Valid targets of indirect branches |
| 57 | `0x39` | `MBARRIER_INSTR_OFFSETS` | Memory barrier instructions |
| 58 | `0x3A` | `COROUTINE_RESUME_OFFSETS` | Coroutine resume point offsets |

**Code 46 (`0x2E`) — `EIATTR_ATOM16_EMUL_INSTR_REG_MAP`**: Free format, but NOT a simple offset array. Carries a register map for 16-bit atomic emulation with a structured per-entry layout rather than flat offsets. The exact sub-record layout is not fully determined from the builder alone (constructed by a separate pass).

```text
Payload: structured register-map entries (not flat u32[] offsets)
Size:    variable
```

### Software Workarounds (42, 48, 53–54)

All use Free format (`0x01`) with u32 offset arrays. The driver patches the instructions at the listed byte offsets during kernel load.

| Code | Hex | Name |
|---:|---:|---|
| 42 | `0x2A` | `SW1850030_WAR` |
| 48 | `0x30` | `SW2393858_WAR` |
| 53 | `0x35` | `SW2861232_WAR` |
| 54 | `0x36` | `SW_WAR` |

`SW_WAR` (`0x36`) is a generic container — unlike the numbered WAR attributes, its payload format may include sub-type discriminators, though the builder treats it as a flat pass-through.

### Cluster and Cooperative Launch (41, 61–63)

**Code 41 (`0x29`) — `EIATTR_COOP_GROUP_MASK_REGIDS`**: Indexed, 4-byte value.

```text
Offset  Size  Field
------  ----  -----
0x00    4     sym_index
0x04    4     mask_regids       Register IDs for cooperative group masks
```

**Code 61 (`0x3D`) — `EIATTR_CTA_PER_CLUSTER`**: Indexed, 4-byte value.

```text
Offset  Size  Field
------  ----  -----
0x00    4     sym_index
0x04    4     ctas_per_cluster  Number of CTAs per cluster (Hopper sm_90+)
```

**Code 62 (`0x3E`) — `EIATTR_EXPLICIT_CLUSTER`**: Indexed, flag-only.

```text
Offset  Size  Field
------  ----  -----
0x00    4     sym_index
(no value -- flag only, presence signals explicit cluster dimensions)
```

**Code 63 (`0x3F`) — `EIATTR_MAX_CLUSTER_RANK`**: Indexed, 4-byte value.

```text
Offset  Size  Field
------  ----  -----
0x00    4     sym_index
0x04    4     max_rank          Maximum cluster rank for scheduling
```

### Compilation Metadata (51, 60, 64)

**Code 51 (`0x33`) — `EIATTR_STATISTICS`**: Free format. Variable-length compilation statistics (instruction counts, etc.). Internal diagnostic data not consumed by the GPU driver.

```text
Payload: structured statistics data (format varies)
Size:    variable
```

**Code 60 (`0x3C`) — `EIATTR_PER_REG_TARGET_PERF_STATS`**: Free format. Per-register-target performance statistics for the profiler.

```text
Payload: structured performance data (format varies)
Size:    variable
```

**Code 64 (`0x40`) — `EIATTR_INSTR_REG_MAP`**: Free format. Instruction-to-register mapping for profiling and debugging tools.

```text
Payload: structured register-map data
Size:    variable
```

### Payload Format Summary (Codes 33–64)

| Code | Name | Wire Fmt | Payload size | Payload layout |
|---:|---|---:|---:|---|
| 33 | `EXPLICIT_CACHING` | `0x04` | 4 | `[sym:4]` flag-only |
| 34 | `ISTYPEP_USED` | `0x04` | 4 | `[sym:4]` flag-only |
| 35 | `MAX_STACK_SIZE` | `0x04` | 8 | `[sym:4][max_stack_bytes:4]` |
| 36 | `SUQ_USED` | `0x04` | 4 | `[sym:4]` flag-only |
| 37 | `LD_CACHEMOD_INSTR_OFFSETS` | `0x01` | N*4 | `u32[]` .text byte offsets |
| 38 | `LOAD_CACHE_REQUEST` | `0x04` | 8 | `[sym:4][cache_mode:4]` |
| 39 | `ATOM_SYS_INSTR_OFFSETS` | `0x01` | N*4 | `u32[]` .text byte offsets |
| 40 | `COOP_GROUP_INSTR_OFFSETS` | `0x01` | N*4 | `u32[]` .text byte offsets |
| 41 | `COOP_GROUP_MASK_REGIDS` | `0x04` | 8 | `[sym:4][mask_regids:4]` |
| 42 | `SW1850030_WAR` | `0x01` | N*4 | `u32[]` .text byte offsets |
| 43 | `WMMA_USED` | `0x04` | 4 | `[sym:4]` flag-only |
| 44 | `HAS_PRE_V10_OBJECT` | `0x02` | 4 | `[flags:4]` global |
| 45 | `ATOMF16_EMUL_INSTR_OFFSETS` | `0x01` | N*4 | `u32[]` .text byte offsets |
| 46 | `ATOM16_EMUL_INSTR_REG_MAP` | `0x01` | var | structured register map |
| 47 | `REGCOUNT` | `0x04` | 8 | `[sym:4][regcount:4]` |
| 48 | `SW2393858_WAR` | `0x01` | N*4 | `u32[]` .text byte offsets |
| 49 | `INT_WARP_WIDE_INSTR_OFFSETS` | `0x01` | N*4 | `u32[]` .text byte offsets |
| 50 | `SHARED_SCRATCH` | `0x04` | 8 | `[sym:4][scratch_bytes:4]` |
| 51 | `STATISTICS` | `0x01` | var | structured stats data |
| 52 | `INDIRECT_BRANCH_TARGETS` | `0x01` | N*4 | `u32[]` .text byte offsets |
| 53 | `SW2861232_WAR` | `0x01` | N*4 | `u32[]` .text byte offsets |
| 54 | `SW_WAR` | `0x01` | var | generic WAR data |
| 55 | `CUDA_API_VERSION` | `0x04` | 8 | `[sym:4][api_version:4]` |
| 56 | `NUM_MBARRIERS` | `0x03` | 0 | value in TLV size field (u16) |
| 57 | `MBARRIER_INSTR_OFFSETS` | `0x01` | N*4 | `u32[]` .text byte offsets |
| 58 | `COROUTINE_RESUME_OFFSETS` | `0x01` | N*4 | `u32[]` .text byte offsets |
| 59 | `SAM_REGION_STACK_SIZE` | `0x04` | 8 | `[sym:4][sam_stack_bytes:4]` |
| 60 | `PER_REG_TARGET_PERF_STATS` | `0x01` | var | structured perf data |
| 61 | `CTA_PER_CLUSTER` | `0x04` | 8 | `[sym:4][ctas:4]` |
| 62 | `EXPLICIT_CLUSTER` | `0x04` | 4 | `[sym:4]` flag-only |
| 63 | `MAX_CLUSTER_RANK` | `0x04` | 8 | `[sym:4][max_rank:4]` |
| 64 | `INSTR_REG_MAP` | `0x01` | var | structured register map |

## Payload Format Reference (Codes 65–96)

Continuation of the per-attribute wire-format documentation. Same sources and conventions as the 0–64 sections above. Codes 65–96 represent the newest EIATTR additions (Ampere through Blackwell era). All require SM-version gating via `sub_1C97840` before emission. Many have dedicated switch cases in the master builder for call-graph propagation.

### Shared Memory (65–66)

**Code 65 (`0x41`) — `EIATTR_RESERVED_SMEM_USED`**: Indexed format, flag-only. Signals the kernel uses reserved shared memory. SM-gated via `sub_1C97840(0x41, sm_version)`.

```text
Offset  Size  Field
------  ----  -----
0x00    4     sym_index
(no value -- flag only, presence is the signal)
```

Binary evidence: magic `0x4101` (16641) at lines 1511 and 2219 of `sub_1CC9800`. The builder tracks this attribute in the `v615[]` per-entry array and propagates it to callee entry points during the second pass (lines 2186–2229). When an entry point does not already have this record, the builder creates one using `sub_1CC7FB0` for symbol resolution.

**Code 66 (`0x42`) — `EIATTR_RESERVED_SMEM_0_SIZE`**: Indexed format, 4-byte value. Size of reserved shared memory partition 0 in bytes.

```text
Offset  Size  Field
------  ----  -----
0x00    4     sym_index
0x04    4     rsmem_bytes       Reserved shared memory size in bytes
```

No explicit switch case in the builder — passes through the default path.

### Microcode Section (67)

**Code 67 (`0x43`) — `EIATTR_UCODE_SECTION_DATA`**: Free format. Opaque microcode section data for internal use. Payload format is architecture-specific and not decoded by the builder.

```text
Payload: opaque byte array
Size:    variable
```

### Instruction Offset Tables (68, 70–71, 87, 89)

All attributes in this group use Free format (`0x01`) carrying variable-length arrays of u32 byte offsets into the kernel's `.text` section.

```text
Payload: u32[] byte offsets into .text section
Size:    N * 4 bytes (N = size_field / 4)
```

| Code | Hex | Name | Offset semantics | Emitter |
|---:|---:|---|---|---|
| 68 | `0x44` | `UNUSED_LOAD_BYTE_OFFSET` | Unused load instructions | `sub_60BCF0` (code 70 pattern) |
| 70 | `0x46` | `SYSCALL_OFFSETS` | `__cuda_syscall` invocations | `sub_60BCF0` |
| 71 | `0x47` | `SW_WAR_MEMBAR_SYS_INSTR_OFFSETS` | `MEMBAR.SYS` instructions needing WAR | `sub_60BDC0` |
| 87 | `0x57` | `STACK_CANARY_TRAP_OFFSETS` | Stack canary trap instructions | `sub_60BEA0` |
| 89 | `0x59` | `LOCAL_CTA_ASYNC_STORE_OFFSETS` | CTA-local async store instructions | default path |

Binary evidence for `sub_60BCF0` (code 70): allocates `4 * count` bytes, copies offsets from the instruction table at `struct+40`, then calls `sub_1CC85F0(a2, 70, (unsigned __int16)count, buf, a4)`. Emission gated by `*(a1+25)` flag and `count > 0`.

Binary evidence for `sub_60BDC0` (code 71) and `sub_60BEA0` (code 87): identical structure to `sub_60BCF0`, differing only in the attribute code passed to `sub_1CC85F0`.

### Kernel Parameter Info V2 (69)

**Code 69 (`0x45`) — `EIATTR_KPARAM_INFO_V2`**: Free format, 12-byte per-parameter entries. Extended version of `KPARAM_INFO` (code 23) with additional type encoding. Emitted by `sub_7FD2B0`.

```text
Payload: repeating 12-byte per-parameter entries:
  Offset  Size  Field
  ------  ----  -----
  0x00    4     param_index       Ordinal position (0-based)
  0x04    4     param_offset      Byte offset in constant bank
  0x08    2     param_size        Size in bytes
  0x0A    1     log_alignment     log2(alignment)
  0x0B    1     flags             Packed nibbles:
                                    lo4 = param_type (from lookup table at 0x21D2E60)
                                    bit4 = is_pointer flag
                                    hi3 = reserved
Size: N * 12 bytes
```

Binary evidence: `sub_7FD2B0` at line 116 calls `sub_1CC85F0(a3, 69, 12, v16, a4)`. The flags byte at offset 0x0B is assembled from two sources: the low nibble is looked up from `dword_21D2E60` indexed by `param_type - 1` (line 110), and bit 4 is set when the parameter is a pointer (line 115: `16 * (*(_BYTE *)(v20 + 25) & 1)`).

First-switch handling: code 69 (`0x45`) appears in the first switch at line 737 alongside texture and resource codes, meaning KPARAM_INFO_V2 records undergo symbol-index resolution during the first pass.

### Graphics-Specific (72–74)

**Code 72 (`0x48`) — `EIATTR_GRAPHICS_GLOBAL_CBANK`**: Indexed format, 4-byte value. Global constant bank descriptor for graphics shaders.

```text
Offset  Size  Field
------  ----  -----
0x00    4     sym_index
0x04    4     cbank_desc        Global constant bank descriptor
```

**Code 73 (`0x49`) — `EIATTR_SHADER_TYPE`**: Indexed format, 4-byte value. Shader type classification.

```text
Offset  Size  Field
------  ----  -----
0x00    4     sym_index
0x04    4     shader_type       Shader type enum (vertex, fragment, compute, etc.)
```

**Code 74 (`0x4A`) — `EIATTR_VRC_CTA_INIT_COUNT`**: Constructed with internal format byte `0x02` (magic `0x4A02` = 18946), but the value is stored in the TLV size field byte, making the wire behavior Sized-like. The builder takes the maximum across all callees.

```text
TLV header: [fmt=0x02][code=0x4A][vrc_count:2]
Payload:    [sym_index:4]
Total record: 8 bytes
```

Binary evidence: magic 18946 at lines 1532 and 2344. The maximum-across-callees logic at lines 1214–1215: `if (v675 < *(v150+2)) v328 = *(v150+2); v675 = v328`. The final value is written back at line 1538: `*((_BYTE *)v196 + 2) = v675`. The `v617[]` per-entry array tracks this attribute for propagation. SM-gated via `sub_1C97840(0x4A, sm_version)`.

### Tools Patching (75)

**Code 75 (`0x4B`) — `EIATTR_TOOLS_PATCH_FUNC`**: Indexed format, 4-byte value. Function patching descriptor for CUDA debugging tools (cuda-gdb, Nsight Compute).

```text
Offset  Size  Field
------  ----  -----
0x00    4     sym_index
0x04    4     patch_info        Patch descriptor for tool instrumentation
```

No explicit switch case — passes through the default path.

### Barrier Count (76)

**Code 76 (`0x4C`) — `EIATTR_NUM_BARRIERS`**: Constructed with internal format byte `0x02` (magic `0x4C02` = 19458), with the barrier count stored in the TLV size field. This is one of the most complex attributes in the 65–96 range, with two distinct code paths.

```text
TLV header: [fmt=0x02][code=0x4C][bar_count:2]
Payload:    [sym_index:4]
Total record: 8 bytes
```

**Dual-path behavior** controlled by `*(a1+101)`:

- **Per-SM tracking mode** (when `*(a1+101)` is set, line 1223): reads barrier count from the size field byte. Takes the maximum across all callees: `if (n < *(v150+2)) v323 = *(v150+2); n = v323`. The `v628[]` per-entry array tracks records. SM-gated via `sub_1C97840(0x4C, sm_version)`.

- **Accumulative mode** (when `*(a1+101)` is clear, falls through to `LABEL_331`): sums barrier counts from callees with -1 sentinel handling (lines 1251–1257): `v298 = v297 + v651; if (v297 == -1) v298 = -1`. The sentinel `-1` means "unknown count" and poisons the sum.

Propagation in `sub_1CC8950`: the barrier/register propagator (2,634 bytes) also creates `NUM_BARRIERS` records during barrier count migration from section flags to `.nv.info` records.

### Texture Mode (77)

**Code 77 (`0x4D`) — `EIATTR_TEXMODE_INDEPENDENT`**: Indexed format, flag-only. Signals the kernel uses independent texture mode.

```text
Offset  Size  Field
------  ----  -----
0x00    4     sym_index
(no value -- flag only)
```

No explicit switch case — passes through the default path.

### Performance Statistics (78)

**Code 78 (`0x4E`) — `EIATTR_PERF_STATISTICS`**: Free format. Performance statistics for the profiler.

```text
Payload: structured performance data
Size:    variable
```

No explicit switch case — passes through the default path. Internal profiler data, not consumed by the GPU driver.

### Fragment Descriptors at Entry (79)

**Code 79 (`0x4F`) — `EIATTR_AT_ENTRY_FRAGEMENTS`**: Free format. The most complex handler in the 65–96 range. Carries fragment offset arrays that describe function entry point fragments. Note: "FRAGEMENTS" is a typo preserved in the binary; corrected variant `EIATTR_AT_ENTRY_FRAGMENTS` exists at `0x2405DA1`.

```text
Payload: u32[] fragment offsets
Size:    N * 4 bytes
```

Binary evidence: emitted via `sub_1CC85F0(a1, 0x4F, 4*count, buf, sym)` at lines 1774 and 2539. The builder uses a set data structure (`v644`) to collect fragment offsets from callees, then merges and deduplicates them:

1. Line 1749: collects total fragment count from `v644` set.
2. Lines 1762–1772: iterates set entries, extracting each offset via `sub_42F060`.
3. Line 1774: emits the merged offset array.
4. Lines 2460–2548: callee propagation loop. For each callee, if an existing entry has fragments, the builder extends the array and deduplicates offsets. If no existing entry, creates a new record.

The deduplication logic (lines 2503–2525) does an O(N*M) scan: for each new offset, checks all existing offsets for duplicates before appending.

Cross-function ownership: when `*(a1+568) != srca` (the current entry's symbol differs from the fragment source), the code byte is zeroed (line 1290: `*(_BYTE *)(v150+1)=0`), suppressing the record for non-owning functions.

### Sparse MMA Mask (80)

**Code 80 (`0x50`) — `EIATTR_SPARSE_MMA_MASK`**: Sized format (`0x03`). Sparsity bitmask for structured-sparse MMA (Matrix Multiply-Accumulate) operations on Blackwell. SM-gated via `sub_1C97840(0x50, sm_version)`.

```text
TLV header: [fmt=0x03][code=0x50][mask_bits:2]
Total record: 4 bytes (header only)
```

Binary evidence: magic `0x5003` (20483) at lines 2085 and 1433. The mask value is stored in the TLV size field. During propagation, the builder OR's mask bits from all callees (line 1407: `v158 |= *(_WORD *)(v162 + 2)`). New entry-point records are initialized with bit 15 set (line 1436: `*((_WORD *)v598 + 1) = 0x8000`; line 1438: `v158 |= 0x8000u`). The `v632[]` per-entry array tracks records.

The `.nv.uft` section emission (lines 2068–2090) also creates SPARSE_MMA_MASK records, gated on `*(a1+240)` (UFT presence flag).

### Tensor Core Gen05 (81–82)

These two codes are mutually exclusive. The builder enforces that a function cannot use both 1-CTA and 2-CTA tensor core modes simultaneously.

**Code 81 (`0x51`) — `EIATTR_TCGEN05_1CTA_USED`**: Indexed format, flag-only. Signals the kernel uses 5th-generation tensor cores in single-CTA mode. SM-gated via `sub_1C97840(0x51, sm_version)` AND requires `v673 > 0x81` (SM code > 129, i.e., sm_130+ / Blackwell).

```text
Offset  Size  Field
------  ----  -----
0x00    4     sym_index
(no value -- flag only)
```

Binary evidence: magic `0x5101` (20737) at lines 1559 and 2259. Tracked in `v614[]` per-entry array. The `v668` flag indicates any tcgen05\_1CTA record was seen. The SM architecture threshold `v673 > 0x81` (line 1543) gates emission: only architectures above 0x81 support tcgen05.

**Code 82 (`0x52`) — `EIATTR_TCGEN05_2CTA_USED`**: Indexed format, flag-only. Signals the kernel uses 5th-generation tensor cores in two-CTA collaborative mode. SM-gated via `sub_1C97840(0x52, sm_version)` AND requires `v673 > 0x81`.

```text
Offset  Size  Field
------  ----  -----
0x00    4     sym_index
(no value -- flag only)
```

Binary evidence: magic `0x5201` (20993) at lines 1582 and 2300. Tracked in `v610[]` per-entry array. The `v674` flag indicates any tcgen05\_2CTA record was seen.

**Mutual exclusion enforcement**: during callee propagation (lines 2264–2266 and 2304–2307), if a function already has a TCGEN05\_1CTA record and the builder attempts to add a TCGEN05\_2CTA record (or vice versa), `sub_42F590` fires a diagnostic warning with the function name. This catches conflicting tensor core mode usage across the call graph.

### Error Barrier at Exit (83)

**Code 83 (`0x53`) — `EIATTR_GEN_ERRBAR_AT_EXIT`**: Indexed format, flag-only. Instructs the driver to generate an error barrier at kernel exit.

```text
Offset  Size  Field
------  ----  -----
0x00    4     sym_index
(no value -- flag only)
```

No explicit switch case in the builder — passes through the default path.

### Register Reconfiguration (84)

**Code 84 (`0x54`) — `EIATTR_REG_RECONFIG`**: Indexed format, flag-only with optional value. Signals the kernel uses dynamic register reconfiguration (`setmaxnreg` instruction, sm_100+). SM-gated via `sub_1C97840(0x54, sm_version)`.

```text
Offset  Size  Field
------  ----  -----
0x00    4     sym_index
0x02    1     reconfig_value    (in TLV size field lo byte, optional)
```

Binary evidence: magic `0x5401` (21505) at lines 1637 and 2395. Tracked in `v616[]` per-entry array with the `v666` flag. During callee propagation (lines 2364–2405), if a callee has a reconfig value (`ii = *(v230+2)`), it is written into the target record's size field byte: `*(_BYTE *)(v417 + 2) = ii` (line 2403). The value propagates from callee to entry point.

### Annotations (85)

**Code 85 (`0x55`) — `EIATTR_ANNOTATIONS`**: Free format with nested TLV-within-TLV sub-records. Emitted by `sub_60C580`. General-purpose annotation container for arbitrary metadata.

```text
Payload: sequence of sub-records, each starting with a type byte:
  Type 0: [type:4]                                  -- 4 bytes
  Type 1: [type:4][value:4]                         -- 8 bytes
  Type 2: [type:4][key:4][len:4][data:len]          -- 12+len bytes, 4-byte aligned
  Type 3: [type:4][len:4][data:len]                 -- 8+len bytes, 4-byte aligned
Size:    sum of all sub-record sizes
```

Binary evidence from `sub_60C580`:
- Line 47: type 2 records copy `key` (4 bytes) + `len` (4 bytes) + `len` bytes of data (line 51–53: `memcpy(v17+3, v7+3, v22)`). Alignment: `(len + 11) & ~3` + 4 (line 55).
- Line 63: type 3 records copy `len` (4 bytes) + `len` bytes (line 66–67: `memcpy(v17+2, v7+2, v26)`). Alignment: `(len + 7) & ~3` + 4 (line 68).
- Line 71: type 1 records are 8 bytes (`v19 = 8; v17[1] = v7[1]`).
- Line 79: type 0 (default) records are 4 bytes.

Total allocation: `257 * entry_count` dwords (line 29: `v8 = 257LL * count`), providing generous headroom for variable-length sub-records.

### Sentinel (86)

**Code 86 (`0x56`) — `EIATTR_UNKNOWN`**: Never emitted. Placeholder in the enum, analogous to `EIATTR_ERROR` (code 0).

### Stub Function Kind (88)

**Code 88 (`0x58`) — `EIATTR_STUB_FUNCTION_KIND`**: Indexed format, 4-byte value. Classifies the type of stub function.

```text
Offset  Size  Field
------  ----  -----
0x00    4     sym_index
0x04    4     stub_kind         Stub function classification enum
```

No explicit switch case — passes through the default path.

### Mercury Finalizer Options (90)

**Code 90 (`0x5A`) — `EIATTR_MERCURY_FINALIZER_OPTIONS`**: Free format. Options for the Mercury FNLZR post-link pass. Emitted by `sub_462220`. Contains null-terminated key-value string pairs with a trailing CRC hash.

```text
Payload: sequence of key-value entries followed by a hash:
  Per-entry:
    Offset  Size    Field
    ------  ----    -----
    0x00    2       key_len     strlen(key) + 1 (includes null terminator)
    0x02    2       val_len     strlen(val) + 1 (includes null terminator)
    0x04    key_len key_str     Null-terminated key string
    0x04+   val_len val_str     Null-terminated value string
            key_len

  Trailer: CRC/hash (computed by sub_4305D0)
Size:    sum of all entries + hash
```

Binary evidence: `sub_462220` at line 656 calls `sub_1CC85F0(v7, 90, v234, v225, *a5)`. Lines 640–647 show the key-value pair packing: `strlen` of key and value, packed as u16 lengths, followed by `strcpy` of both strings. The hash is computed at line 653 via `sub_4305D0(0x123456, ...)`.

### Cluster Configuration (91)

**Code 91 (`0x5B`) — `EIATTR_BLOCKS_ARE_CLUSTERS`**: Indexed format, flag-only. Signals that CTA blocks are clusters (every block is its own cluster).

```text
Offset  Size  Field
------  ----  -----
0x00    4     sym_index
(no value -- flag only)
```

No explicit switch case — passes through the default path.

### Address Sanitizer (92)

**Code 92 (`0x5C`) — `EIATTR_SANITIZE`**: Indexed format, flag-only. Signals the kernel has been instrumented with address sanitizer.

```text
Offset  Size  Field
------  ----  -----
0x00    4     sym_index
(no value -- flag only)
```

No explicit switch case — passes through the default path.

### Syscall Fallback (93)

**Code 93 (`0x5D`) — `EIATTR_SYSCALLS_FALLBACK`**: Free format. Syscall fallback mechanism data.

```text
Payload: structured syscall fallback data
Size:    variable
```

No explicit switch case — passes through the default path.

### CUDA Requirements (94)

**Code 94 (`0x5E`) — `EIATTR_CUDA_REQ`**: Free format. CUDA requirements descriptor specifying minimum runtime capabilities.

```text
Payload: structured requirements data
Size:    variable
```

No explicit switch case — passes through the default path.

### Mercury ISA Version (95)

**Code 95 (`0x5F`) — `EIATTR_MERCURY_ISA_VERSION`**: Sized format (`0x03`). Mercury ISA version encoded in the TLV size field.

```text
TLV header: [fmt=0x03][code=0x5F][isa_version:2]
Total record: 4 bytes (header only)
```

### Error Last Sentinel (96)

**Code 96 (`0x60`) — `EIATTR_ERROR_LAST`**: Never emitted. Upper bound sentinel for the enum range. Used for bound checks in the builder: `if (attr_code > 0x2F)` at line 760.

### Payload Format Summary (Codes 65–96)

| Code | Name | Wire Fmt | Payload size | Payload layout |
|---:|---|---:|---:|---|
| 65 | `RESERVED_SMEM_USED` | `0x04` | 4 | `[sym:4]` flag-only |
| 66 | `RESERVED_SMEM_0_SIZE` | `0x04` | 8 | `[sym:4][rsmem_bytes:4]` |
| 67 | `UCODE_SECTION_DATA` | `0x01` | var | opaque byte array |
| 68 | `UNUSED_LOAD_BYTE_OFFSET` | `0x01` | N*4 | `u32[]` .text byte offsets |
| 69 | `KPARAM_INFO_V2` | `0x01` | N*12 | 12B per-param descriptors |
| 70 | `SYSCALL_OFFSETS` | `0x01` | N*4 | `u32[]` .text byte offsets |
| 71 | `SW_WAR_MEMBAR_SYS_OFFSETS` | `0x01` | N*4 | `u32[]` .text byte offsets |
| 72 | `GRAPHICS_GLOBAL_CBANK` | `0x04` | 8 | `[sym:4][cbank_desc:4]` |
| 73 | `SHADER_TYPE` | `0x04` | 8 | `[sym:4][shader_type:4]` |
| 74 | `VRC_CTA_INIT_COUNT` | `0x02` | 4 | `[sym:4]` count in TLV size byte |
| 75 | `TOOLS_PATCH_FUNC` | `0x04` | 8 | `[sym:4][patch_info:4]` |
| 76 | `NUM_BARRIERS` | `0x02` | 4 | `[sym:4]` count in TLV size byte |
| 77 | `TEXMODE_INDEPENDENT` | `0x04` | 4 | `[sym:4]` flag-only |
| 78 | `PERF_STATISTICS` | `0x01` | var | structured perf data |
| 79 | `AT_ENTRY_FRAGEMENTS` | `0x01` | N*4 | `u32[]` fragment offsets |
| 80 | `SPARSE_MMA_MASK` | `0x03` | 0 | bitmask in TLV size field (u16) |
| 81 | `TCGEN05_1CTA_USED` | `0x04` | 4 | `[sym:4]` flag-only |
| 82 | `TCGEN05_2CTA_USED` | `0x04` | 4 | `[sym:4]` flag-only |
| 83 | `GEN_ERRBAR_AT_EXIT` | `0x04` | 4 | `[sym:4]` flag-only |
| 84 | `REG_RECONFIG` | `0x04` | 4 | `[sym:4]` value in TLV size byte |
| 85 | `ANNOTATIONS` | `0x01` | var | nested TLV sub-records |
| 86 | `UNKNOWN` | — | 0 | none (never emitted) |
| 87 | `STACK_CANARY_TRAP_OFFSETS` | `0x01` | N*4 | `u32[]` .text byte offsets |
| 88 | `STUB_FUNCTION_KIND` | `0x04` | 8 | `[sym:4][stub_kind:4]` |
| 89 | `LOCAL_CTA_ASYNC_STORE_OFFSETS` | `0x01` | N*4 | `u32[]` .text byte offsets |
| 90 | `MERCURY_FINALIZER_OPTIONS` | `0x01` | var | key-value pairs + hash |
| 91 | `BLOCKS_ARE_CLUSTERS` | `0x04` | 4 | `[sym:4]` flag-only |
| 92 | `SANITIZE` | `0x04` | 4 | `[sym:4]` flag-only |
| 93 | `SYSCALLS_FALLBACK` | `0x01` | var | structured syscall data |
| 94 | `CUDA_REQ` | `0x01` | var | structured requirements |
| 95 | `MERCURY_ISA_VERSION` | `0x03` | 0 | value in TLV size field (u16) |
| 96 | `ERROR_LAST` | — | 0 | none (never emitted) |

## Generation Pipeline

EIATTR attributes are generated during Phase 6 of the ELF output pipeline, after all per-kernel SASS encoding and memory allocation have completed. The generation is orchestrated by two functions working in sequence.

### Barrier/Register Propagation — `sub_1CC8950`

Before per-entry attribute emission begins, `sub_1CC8950` (2,634 bytes, called once per entry point) propagates resource requirements from callees to entry kernels via the call graph:

1. **Register count propagation**: Walks the call graph DFS, finding the maximum register count among all callees. The verbose trace `"regcount %d for %s propagated to entry %s"` logs this.

2. **Barrier count creation**: When a kernel's section flags contain a barrier count (bits 20–26 of `section_header + 8`) but no `EIATTR_NUM_BARRIERS` record exists, creates one and clears the section flag bits:

```text
Creating new EIATTR_NUM_BARRIERS and moving barcount %d
from section flags of %s to nvinfo for entry symbol %s
```

3. **SM-version gating**: Uses `sub_1C97840` to check whether `EIATTR_NUM_BARRIERS` (`0x4C`) and `EIATTR_NUM_MBARRIERS` (`0x38`) are valid for the target SM version before emitting.

### Master EIATTR Builder — `sub_1CC9800`

The main builder function (14,764 bytes binary, 90 KB decompiled — third largest function in the output range) constructs the complete set of `.nv.info.<func>` sections. It has 51 callees and is called once per compilation unit.

The builder iterates over every entry point and device function, emitting the applicable EIATTR records for each. The SM-version gating function `sub_1C97840` is called before emitting each attribute to check compatibility. Observed EIATTR code checks in the builder:

| Hex code | EIATTR name | Gating condition |
|---:|---|---|
| `0x04` | `CTAIDZ_USED` | SM-version check |
| `0x21` | `EXPLICIT_CACHING` | SM-version check |
| `0x1F` | `NEED_CNP_WRAPPER` | SM-version check |
| `0x20` | `NEED_CNP_PATCH` | SM-version check |
| `0x2C` | `HAS_PRE_V10_OBJECT` | SM-version check |
| `0x38` | `NUM_MBARRIERS` | SM-version check |
| `0x41` | `RESERVED_SMEM_USED` | SM-version check |
| `0x4A` | `VRC_CTA_INIT_COUNT` | SM-version check |
| `0x4C` | `NUM_BARRIERS` | SM-version check |
| `0x50` | `SPARSE_MMA_MASK` | SM-version check |
| `0x51` | `TCGEN05_1CTA_USED` | SM-version check |
| `0x52` | `TCGEN05_2CTA_USED` | SM-version check |
| `0x54` | `REG_RECONFIG` | SM-version check |

The SM version comes from offset `+624` of the compilation state object, consistent with the SM version field at `a1 + 624` observed throughout ptxas.

### Weak Symbol Filtering

During linking (nvlink), three specific EIATTR codes are treated specially during weak symbol resolution. When a weak function is replaced by a stronger definition, records for these three codes are dropped using the bitmask `0x800800020000`:

- Code 17 (`0x11`) — `EIATTR_FRAME_SIZE`
- Code 35 (`0x23`) — `EIATTR_MAX_STACK_SIZE`
- Code 47 (`0x2F`) — `EIATTR_REGCOUNT`

The rationale: when a weak function is replaced, its resource descriptors must not contaminate the replacement's resource accounting.

## Consumer Tools

### cuobjdump

`cuobjdump --dump-elf-section=.nv.info` dumps raw hex bytes of the global `.nv.info` section. With `--dump-resource-usage`, it decodes EIATTR records into human-readable resource summaries (register count, shared memory, stack sizes).

### nvdisasm

`nvdisasm -nvi` decodes `.nv.info` sections into named EIATTR records with decoded values. This is the primary tool for inspecting EIATTR content without writing a custom parser.

### cuda-gdb

The debugger uses `EIATTR_TOOLS_PATCH_FUNC` (code 75, `0x4B`) to locate patchable function entry points for breakpoint insertion and instrumentation.

## How EIATTR Drives GPU Resource Allocation

The `.nv.info` section is not just metadata for tools — it is the primary input to the GPU driver's kernel launch resource allocator:

1. **Register allocation**: `EIATTR_REGCOUNT` (`0x2F`) tells the driver how many registers each thread needs. The driver computes `max_warps_per_SM = total_registers / (regcount * warp_size)`.

2. **Shared memory reservation**: `EIATTR_SMEM_PARAM_SIZE` (`0x18`) and `EIATTR_RESERVED_SMEM_0_SIZE` (`0x42`) determine how much shared memory to carve out before dynamic shared memory allocation.

3. **Stack allocation**: `EIATTR_CRS_STACK_SIZE` (`0x1E`) and `EIATTR_MAX_STACK_SIZE` (`0x23`) determine per-thread stack allocation. Too small causes memory corruption; too large reduces occupancy.

4. **Barrier reservation**: `EIATTR_NUM_BARRIERS` (`0x4C`) reserves named barrier slots. Hardware supports 16 barriers per CTA on most architectures.

5. **Instruction patching**: Offset tables (`EXIT_INSTR_OFFSETS`, `S2RCTAID_INSTR_OFFSETS`, `SW*_WAR`) tell the driver which instruction words to patch at load time. This enables hardware workarounds and CTA-ID remapping for cluster launch without recompilation.

6. **Cluster configuration**: `EIATTR_CTA_PER_CLUSTER` (`0x3D`) and `EIATTR_EXPLICIT_CLUSTER` (`0x3E`) control the cluster launch hardware on sm_90+, determining how many CTAs share distributed shared memory.

7. **Tensor core mode**: `EIATTR_TCGEN05_1CTA_USED` (`0x51`) and `EIATTR_TCGEN05_2CTA_USED` (`0x52`) inform the driver about 5th-gen tensor core usage modes on sm_100+.

## Binary Artifacts

### Pointer Table Layout

The EIATTR name table at VA `0x23FDC20` consists of 97 entries of 16 bytes each (1,552 bytes total):

```text
Offset  Size  Field
------  ----  -----
0x00    8     name_ptr     Pointer to null-terminated EIATTR name string
0x08    4     meta_lo      Minimum toolkit version compatibility
0x0C    4     meta_hi      Flags (0=legacy, 1=internal, 2=standard)
```

The table is indexed directly by EIATTR code number: `entry = table_base + code * 16`.

### Typos Preserved in the Binary

| String in binary | Correct spelling | Address |
|---|---|---|
| `EIATTR_AT_ENTRY_FRAGEMENTS` | `EIATTR_AT_ENTRY_FRAGMENTS` | `0x23FCCBD` (code 79 name) |

A corrected variant `EIATTR_AT_ENTRY_FRAGMENTS` exists at `0x2405DA1`, and `EIATTR_COROUTINE_RESUME_ID_OFFSETS` at `0x24064D8` is an alternate name for code 58, both outside the main table.

### Diagnostic Strings

```text
"Creating new EIATTR_NUM_BARRIERS and moving barcount %d
 from section flags of %s to nvinfo for entry symbol %s"       (0x2406960)

"Creating new EIATTR_NUM_BARRIERS and moving barcount %d
 from section flags of %s to nvinfo for non-entry symbol %s"   (0x24069D0)

"Creating new EIATTR_NUM_BARRIERS and propagating higher
 barcount %d from section flags of %s to nvinfo
 for entry symbol %s"                                          (0x2406B10)

"conflicting crs_stack attribute"                               (sub_1CC9800 evidence)

"Turning caching %s for entry '%s' as per its request"          (sub_1CC9800 evidence)

"regcount %d for %s propagated to entry %s"                     (sub_1CC8950 evidence)

"no regcount?"                                                  (sub_1CC8950 evidence)
```

### Key Functions

| Address | Size (native) | Identity | Role |
|---|---|---|---|
| `sub_1CC9800` | 14,764 B | Master EIATTR builder | Constructs all `.nv.info.<func>` sections (86 KB decomp, 51 callees) |
| `sub_1CC8950` | 2,634 B | Barrier/register propagator | Propagates resource counts across call graph |
| `sub_1CC85F0` | ~180 B | TLV record emitter | Writes individual EIATTR records to the nvinfo linked list |
| `sub_1C97840` | ~100 B | SM-version gate | Checks if an EIATTR code is valid for a given SM target |
| `sub_1CC86D0` | ~600 B | Per-entry stack emitter | Emits `MIN_STACK_SIZE` (0x12), `CRS_STACK_SIZE` (0x1E), `SAM_REGION_STACK_SIZE` (0x3B) per function |
| `sub_1CC84A0` | ~400 B | EIATTR helper | Attribute lookup helper |
| `sub_1CC83F0` | ~200 B | EIATTR helper | Section flag extractor |
| `sub_1CC8100` | ~1 KB | Cache conflict resolver | Resolves conflicting cache preference attributes |

## Cross-References

- [ELF/Cubin Output](../pipeline/output.md) — Phase 6 in the 11-phase output pipeline
- [Custom ELF Emitter](../output/elf-emitter.md) — Section creation and layout
- [Synchronization & Barriers](../passes/sync-barriers.md) — Barrier count source
- [Register Allocation](../regalloc/overview.md) — REGCOUNT source
