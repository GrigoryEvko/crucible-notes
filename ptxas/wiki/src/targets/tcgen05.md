# TCGen05 — 5th Generation Tensor Cores

> *All addresses in this page apply to ptxas v13.0.88 (CUDA 13.0). Other versions will differ.*

TCGen05 is the Blackwell-generation tensor core instruction family introduced with SM 100. It replaces Hopper's WGMMA with a descriptor-based programming model that operates on Tensor Memory (TMEM) — a dedicated register-file-like storage visible only to the tensor core. ptxas implements TCGen05 as 13 PTX instruction mnemonics (plus 8 debug guardrails), backed by a 90KB MMA codegen function, 11 SASS opcode groups (28 encoding variants), and a set of compiler-inserted validation hooks. TCGen05 is absent on sm_120/sm_121 (consumer Blackwell).

| | |
|---|---|
| **Target architectures** | sm_100, sm_100a, sm_100f, sm_103, sm_103a, sm_103f, sm_110, sm_110a, sm_110f |
| **NOT available** | sm_120, sm_121 (consumer/DGX Spark) — gated by SM version checks |
| **Capability check** | `sub_70FA00(*, 29)` — returns true for tcgen05-capable targets |
| **PTX instructions** | 13: alloc, dealloc, relinquish_alloc_permit, ld, ld.red, st, commit, cp, shift, fence, wait, mma, mma.ws |
| **Guardrail instructions** | 8: is_phase_valid, are_columns_allocated, is_current_warp_valid_owner, in_physical_bounds, allocation_granularity, datapath_alignment, sp_consistency_across_idesc_mod, check_sparse_usage |
| **SASS opcode range** | Opcodes 122--139 (TMEM operations), 213--221 (TCGEN05_MMA/FENCE, TMEM extended), 342--372 (TCGEN05 control) |
| **Codegen factory** | 36864 (9 << 12) — shared across all Blackwell targets |
| **MMA codegen** | `sub_5BBC30` (90KB) |
| **PTX validator** | `sub_4C5FB0` (28KB — shared MMA/WMMA/tcgen05 validator) |
| **Intrinsic handler** | `sub_6D7AF0` (19KB — TCGen05 MMA handler) |
| **Intrinsic validator** | `sub_6D69B0` (12KB — TCGen05 MMA validator) |
| **EIATTR markers** | `EIATTR_TCGEN05_1CTA_USED`, `EIATTR_TCGEN05_2CTA_USED` |
| **Version constraint** | Objects using tcgen05 from CUDA 12.x cannot link with 13.0+; must rebuild |

## Architecture Overview

### Descriptor-Based Model

TCGen05 abandons the register-operand model of previous tensor core generations (WMMA, HMMA, WGMMA) in favor of descriptors. The instruction descriptor (`idesc`) encodes the matrix operation configuration — dimensions, data types, data path width, sparsity, and layout. The descriptor is passed as an operand to `tcgen05.mma` rather than encoded in the instruction mnemonic.

This design decouples the instruction encoding from the operation specification. Where WGMMA required hundreds of distinct intrinsic hash entries to cover every shape/type/layout combination, tcgen05 uses a single instruction with different descriptor values. The ~400 numeric MMA hash entries in the intrinsic dispatch table (at `a1+816` in `sub_5D4190`) map WGMMA variants; tcgen05 replaces that complexity with descriptor-driven dispatch.

### Tensor Memory (TMEM)

TMEM is a dedicated storage region private to the tensor core unit. It is not part of the general register file and is not directly addressable by non-tensor-core instructions. TMEM is organized into **columns** that are allocated, used, and deallocated explicitly by the programmer.

Key properties from binary analysis:

- **Column-based allocation**: `tcgen05.alloc` reserves columns; `tcgen05.dealloc` releases them
- **Two CTA granularities**: Operations execute at `.cta_group::1` (single CTA) or `.cta_group::2` (CTA pair) granularity. A function cannot mix both — ptxas enforces: *"Function '%s' uses single CTA(.cta_group::1) and CTA pair granularity(.cta_group::2) and that is not allowed."*
- **Allocation tracking**: The compiler inserts reserved shared memory variables to track allocation state:
  - `__nv_reservedSMEM_tcgen05_partition` — partition identifier
  - `__nv_reservedSMEM_allocation_phase` — current allocation phase
  - `__nv_reservedSMEM_allocation_mask` — bitmask of allocated columns
  - `__nv_reservedSMEM_tmem_allocation_pipeline_mbarrier` — mbarrier for allocation pipeline
  - `__nv_reservedSMEM_tmem_allocation_pipeline_mbarrier_parity` — parity tracking

### TMEM Address Computation

Tensor memory addresses are computed through a standardized pattern visible in the TMEM address generator functions (`sub_70E740`, `sub_70E940`, `sub_70EB00`):

```ptx
cvt.u32.u64 __cuda_sm_100_tcgen05_tmem_addr_base, %s;
add.u32 %s, __cuda_sm_100_tcgen05_tmem_addr_base, %s;
```

Five named TMEM address roles exist for MMA operations:

| Address Role | Intrinsic Name | Purpose |
|---|---|---|
| D (destination) | `__cuda_sm10x_tcgen05_mma_tmemD` | Accumulator/output matrix |
| A (input) | `__cuda_sm10x_tcgen05_mma_tmemA` | Left input matrix |
| Scale A | `__cuda_sm10x_tcgen05_mma_scaleTmemA` | Scale factors for A |
| Scale B | `__cuda_sm10x_tcgen05_mma_scaleTmemB` | Scale factors for B |
| Sparse Meta | `__cuda_sm10x_tcgen05_mma_spMetaTmem` | Sparsity metadata |

Constraint from the binary: `"URa must be uint32 when URa is TMEM"` — uniform registers addressing TMEM must use 32-bit unsigned integers. When addressing a global descriptor: `"URa must be uint64 when URa is GDESC"`.

## PTX Instruction Set

### Lifecycle Instructions

| PTX Instruction | Formatter Address | Size | Purpose |
|---|---|---|---|
| `tcgen05.alloc` | `0x526370` | 1,287 B | Allocate TMEM columns for tensor core use |
| `tcgen05.dealloc` | `0x574050` | 2,130 B | Release allocated TMEM columns |
| `tcgen05.relinquish_alloc_permit` | `0x58C7F0` | 4,282 B | Relinquish allocation permit (multi-CTA coordination) |

The alloc instruction has two CTA-granularity variants visible in the prototype strings:

- `__cuda_sm10x_tcgen05_alloc_one_sm` — single-SM allocation (`.cta_group::1`)
- `__cuda_sm10x_tcgen05_alloc_two_sm` — two-SM allocation (`.cta_group::2`)

Both take a destination pointer argument (`__cuda_sm10x_tc_alloc_dst_ptr_arg`) and a column count (`__cuda_sm10x_tc_alloc_num_cols_arg`).

### Data Movement Instructions

| PTX Instruction | Formatter Address | Size | Purpose |
|---|---|---|---|
| `tcgen05.ld` | `0x578DB0` | 2,466 B | Load data into TMEM from shared/global memory |
| `tcgen05.ld.red` | `0x571FE0` | 2,066 B | Load with reduction (accumulate into TMEM) |
| `tcgen05.st` | `0x56C190` | 1,842 B | Store data from TMEM to shared/global memory |
| `tcgen05.cp` | `0x5427F0` | 903 B | Copy between TMEM regions (intra-tensor-core) |

Three intrinsic helper arrays support the ld/st/ld.red operations:

| Helper | Purpose |
|---|---|
| `__cuda_sm_100_tcgen05_ld_funcRetArr` | Return array descriptor for loads |
| `__cuda_sm_100_tcgen05_ld_red_funcRetArr` | Return array descriptor for load-reduce |
| `__cuda_sm_100_tcgen05_st_funcInputArr` | Input array descriptor for stores |

Each has a corresponding `immhalfSplitOff` parameter controlling split behavior:

- `__cuda_sm_100_tcgen05_ld_immhalfSplitOff`
- `__cuda_sm_100_tcgen05_ld_red_immhalfSplitOff`
- `__cuda_sm_100_tcgen05_st_immhalfSplitOff`

### Synchronization Instructions

| PTX Instruction | Formatter Address | Size | Purpose |
|---|---|---|---|
| `tcgen05.commit` | `0x5427F0` | 1,575 B | Commit pending tensor core operations |
| `tcgen05.fence` | (inline) | — | Fence preventing reordering of tcgen05 operations |
| `tcgen05.wait` | (inline) | — | Wait for committed tcgen05 operations to complete |
| `tcgen05.shift` | `0x58FA20` | 4,604 B | Shift accumulator data within TMEM (shared formatter with mma) |

### Compute Instructions

| PTX Instruction | Formatter Address | Size | Purpose |
|---|---|---|---|
| `tcgen05.mma` | `0x5BBC30` (codegen) / `0x58FA20` (formatter) | 90KB / 4,604 B | Matrix multiply-accumulate |
| `tcgen05.mma.ws` | `0x4DA720` (formatter) | 343 B | Warp-specialized MMA variant |

## TCGen05.MMA — Matrix Multiply-Accumulate

### Codegen Function: `sub_5BBC30` (90KB)

The largest per-instruction codegen function for TCGen05. Registered as the `"tcgen05.mma"` handler in `sub_5D4190` (the intrinsic dispatch table builder). The function:

1. Allocates a 50,000-byte working buffer
2. Queries `sub_70FA00(*, 29)` to validate tcgen05 capability on the current target
3. Processes the instruction descriptor to determine operation parameters
4. Generates tensor memory addressing code for all operands (D, A, scaleA, scaleB, sparsity meta)
5. Emits the final MMA instruction encoding

### MMA Modifiers

The binary reveals a rich set of MMA modifiers extracted by functions in the `sub_70D1F0`--`sub_70D410` cluster:

| Modifier | String | Purpose |
|---|---|---|
| `.o128` | `".o128"` | 128-bit output size |
| `.transA` | `".transA"` | Transpose A matrix |
| `.transB` | `".transB"` | Transpose B matrix |
| `.negA` | `"_negA"` | Negate A matrix |
| `.negB` | `"_negB"` | Negate B matrix |
| `_expand16bit` | `"_expand16bit"` | 16-bit expansion mode |
| `_pack16bit` | `"_pack16bit"` | 16-bit packing mode |
| `_maxabs` | `"_maxabs"` | Maximum absolute value reduction |
| `_minabs` | `"_minabs"` | Minimum absolute value reduction |
| `_fused` | `"_fused"` | Fused operation mode |
| `_blockscale` | `"_blockscale"` | Block scaling (MX format support) |
| `_ashift` | `"_ashift"` | A-matrix shift |
| `_areuse` | `"_areuse"` | A-matrix register reuse |
| `_akeep` | `"_akeep"` | A-matrix keep (preserve for reuse) |

### Data Path Configurations

The MMA data path width determines the number of elements processed per cycle and the accumulator layout. Six configurations exist:

| Data Path | String | Interpretation |
|---|---|---|
| `_4dp256bit` | 4 data paths, 256 bits each | |
| `_16dp32bit` | 16 data paths, 32 bits each (two sub-variants: `t0t15`, `t16t31`) |  |
| `_32dp32bit` | 32 data paths, 32 bits each | |
| `_16dp256bit` | 16 data paths, 256 bits each | |
| `_128dp256bit` | 128 data paths, 256 bits each | |

Constraint: `"fused and l16dp32bit must be specified together"` — the fused mode requires the 16dp32bit data path.

### Block Scaling (MX Format)

TCGen05 adds native block scaling support for microscaling (MX) format operations, visible through the `tcmma` prefix strings:

- `"tcmma_*_o must be specified with blockscale"` — output modifier requires blockscale
- `"uri width for tcmma_*_o must be 2"` — output uniform register index width must be 2
- `"tcmma_*_q with blockscale must have uri width of 2"` — quantization with blockscale
- `"tcmma_*_mxq must be specified with blockscale"` — MX quantization requires blockscale

### Warp-Specialized MMA (`.ws`)

The `.ws` modifier enables warp-specialized execution where different warps in a warpgroup contribute to different phases of the MMA pipeline. Constraints from the binary:

- `"When using buffer1-3, WS modifier must be specified"` — triple buffering requires `.ws`
- `"ws opcode modifier not allowed with .2CTA"` — warp specialization is single-CTA only
- `"ws opcode modifier not allowed with areuse or akeep"` — `.ws` incompatible with A-matrix reuse
- `"ws opcode modifier not allowed with ashift"` — `.ws` incompatible with A-matrix shift

Triple-buffer register reuse strings for `.ws` mode:

| Buffer | Variant |
|---|---|
| `_breuse_bkeep_buffer1` | B-reuse + B-keep, buffer 1 |
| `_breuse_buffer1` | B-reuse, buffer 1 |
| `_breuse_bkeep_buffer2` | B-reuse + B-keep, buffer 2 |
| `_breuse_buffer2` | B-reuse, buffer 2 |
| `_breuse_bkeep_buffer3` | B-reuse + B-keep, buffer 3 |
| `_breuse_buffer3` | B-reuse, buffer 3 |

### Sparsity Support

TCGen05 supports structured sparsity through the sparsity metadata TMEM address (`spMetaTmem`). The `_ashift` modifier is constrained: `"Ashift can only be specific when URa is in TMEM"`.

## SASS Encoding

### Opcode Map

TCGen05 SASS instructions span three opcode regions in the SM 100 SASS ISA. The encoding information comes from the latency model tables (`sub_8E8A90` for sm_100) and the master instruction encoder (`sub_6D9690`, 94KB).

#### TMEM Operations (Opcodes 122--139)

| Opcode | Variants | Category | Encoding Class | Operands |
|---|---|---|---|---|
| 122 | 2 | TMEM_OP / new ISA | F1F08, F1C60 | 3-op, reg10 |
| 123 | 6 | TMEM_LD (tensor mem load) | F1F08, F1DF8 | 2--3 op |
| 125 | 6 | TMEM_ST (tensor mem store) | F1F08, F1DF8 | 2--3 op |
| 127 | 9 | TMEM_ALLOC / FENCE | F1F08..F29A8 | 3--6 op |
| 129 | 3 | TMEM extended | F1F08 | 2 op |
| 130 | 26 | EXTENDED_MOV / TMEM_MVA | F1F08..F2678 | 2--9 op |
| 131 | 3 | EXTENDED_ALU / UTMA | F21B0 | 4--5 op |
| 133 | 1 | UTMA variant | F21B0 | 4 op |
| 139 | 4 | TCGEN05 operations | F21B0, F2568 | 4--8 op |

#### TCGEN05 MMA/FENCE (Opcodes 213--221)

| Opcode | Variants | Category | Encoding Class | Operands |
|---|---|---|---|---|
| 213 | 6 | TCGEN05_MMA | F2678 | 5--7 op |
| 216 | 2 | TCGEN05_FENCE | F2678 | 3--4 op |
| 219 | 6 | TMEM_LD extended | F1C60..F2810 | 3--7 op |
| 220 | 1 | TMEM_ST extended | F1C60 | 3 op |
| 221 | 1 | TMEM_PREFETCH | F1C60 | 3 op |
| 255 | 1 | SETSTMEMADDR | F1F08 | 1 op |
| 269 | 4 | TMEM_ALLOC_FENCE ext | F2018, F1DF8 | 2--3 op |

#### TCGEN05 Control (Opcodes 342--372)

28 encoding variants across 10 opcodes. These are the primary tensor core pipeline control instructions:

| Opcode | Variants | Category | Encoding Class | Operands |
|---|---|---|---|---|
| 342 | 1 | TCGEN05 ctrl A | F1F08 | 0 op (scheduling marker) |
| 343 | 1 | TCGEN05 ctrl B | F1F08 | 0 op (scheduling marker) |
| 344 | 14 | TCGEN05 execute | F1F08..F3008 | 2--7 op |
| 346 | 4 | TCGEN05 commit | F1F08, F2018 | 2--3 op |
| 349 | 1 | TCGEN05 sync | F1D70 | 0 op |
| 359 | 3 | TCGEN05 alloc | F1D70, F1F08 | 0--2 op |
| 369 | 1 | TCGEN05 dealloc | F1F08 | 0 op |
| 370 | 1 | TCGEN05 release A | F1D70 | 0 op |
| 371 | 1 | TCGEN05 release B | F1D70 | 0 op |
| 372 | 1 | TCGEN05 release C | F1D70 | 0 op |

**Opcode 344 (TCGEN05 execute)** has the most variants (14), spanning encoding classes from F1F08 to F3008 with 2 to 7 operands. This is the actual MMA dispatch instruction — the wide encoding range reflects the variety of descriptor configurations, operand modes, and data path widths.

### Encoding Class Distribution

The encoding classes used by TCGen05 SASS instructions:

| Class | Hex | Usage |
|---|---|---|
| F1D70 | Control/sync | alloc (0-op), sync, release A/B/C |
| F1F08 | General | ctrl markers, execute, commit, alloc, dealloc, TMEM ops |
| F1C60 | Extended | TMEM_LD/ST extended, TMEM_PREFETCH |
| F1DF8 | Standard | TMEM_LD/ST, TMEM_ALLOC_FENCE ext |
| F2018 | Commit ext | TCGEN05 commit, TMEM_ALLOC_FENCE ext |
| F21B0 | ALU | TCGEN05 operations, UTMA |
| F2568 | TCGEN05 ops | TCGEN05 operations |
| F2678 | MMA/FENCE | TCGEN05_MMA, TCGEN05_FENCE |
| F29A8 | TMEM_ALLOC | TMEM_ALLOC/FENCE |
| F2810 | Extended | TMEM_LD extended |
| F3008 | Execute max | TCGEN05 execute (high-operand-count) |

### Latency Model

The sm_100 latency table (`sub_8E8A90`) uses a two-part structure: a 3.0KB base table covering standard instructions and a 949-byte supplement dedicated to TCGEN05 operations. The sm_120 consumer Blackwell table (`sub_8E9000` + `sub_8E92E0`, 5.5KB) is the largest individual table and does not include TCGEN05 entries (confirming the feature's absence on consumer silicon).

## CTA Granularity

TCGen05 operations specify whether they execute at single-CTA or CTA-pair granularity through the `.cta_group` modifier:

| Granularity | Modifier | EIATTR | ELF Marker |
|---|---|---|---|
| Single CTA | `.cta_group::1` | `EIATTR_TCGEN05_1CTA_USED` | `TC_1CTA` |
| CTA Pair | `.cta_group::2` | `EIATTR_TCGEN05_2CTA_USED` | `TC_2CTA` |

The compiler emits the appropriate EIATTR marker into the output cubin based on which granularity the kernel uses. The CUDA runtime uses this to configure the CTA launch parameters.

The binary enforces exclusivity: a single function cannot mix `.cta_group::1` and `.cta_group::2` operations. The error message is explicit: *"Function '%s' uses single CTA(.cta_group::1) and CTA pair granularity(.cta_group::2) and that is not allowed."*

## ELF/Cubin Markers

### EIATTR Entries

| EIATTR Name | Purpose |
|---|---|
| `EIATTR_TCGEN05_1CTA_USED` | Kernel uses tcgen05 at single-CTA granularity |
| `EIATTR_TCGEN05_2CTA_USED` | Kernel uses tcgen05 at CTA-pair granularity |

### EICOMPAT Attributes

| EICOMPAT Name | Purpose |
|---|---|
| `EICOMPAT_ATTR_INST_TCGEN05_MMA` | Kernel uses tcgen05.mma instructions |
| `EICOMPAT_ATTR_INST_TCGEN05_MMA_DEPRECATED` | Kernel uses deprecated (12.x-era) tcgen05.mma encoding |

### Entry Fragment Markers

TMEM usage per-CTA is recorded in entry fragment markers:

| Marker | Version | Purpose |
|---|---|---|
| `AT_ENTRY_FRAGMENT_TMEM_CTA1` | V1 | TMEM usage for single-CTA kernels |
| `AT_ENTRY_FRAGMENT_TMEM_CTA2` | V1 | TMEM usage for CTA-pair kernels |
| `AT_ENTRY_FRAGMENT_TMEM_CTA1_V2` | V2 | TMEM usage V2 format, single-CTA |
| `AT_ENTRY_FRAGMENT_TMEM_CTA2_V2` | V2 | TMEM usage V2 format, CTA-pair |

## Guardrail Debug Instrumentation

When compiling with `-g` (debug mode), ptxas inserts runtime validation checks around tcgen05 operations. These are controlled by the `--g-tensor-memory-access-check` / `--gno-tensor-memory-access-check` CLI options.

### Guardrail Check Functions

Eight `_tcgen05.guardrails.*` pseudo-instructions insert inline validation code:

| Guardrail | Formatter Address | Size | Validates |
|---|---|---|---|
| `is_phase_valid` | `0x4DDE70` | 775 B | Allocation phase is correct for the operation |
| `are_columns_allocated` | `0x4DBF20` | 599 B | Accessed columns are currently allocated |
| `is_current_warp_valid_owner` | `0x4DE180` | 791 B | Current warp owns the accessed TMEM region |
| `in_physical_bounds` | `0x4DB050` | 439 B | Column access is within physical TMEM bounds |
| `allocation_granularity` | `0x4F0960` | 839 B | Column count meets granularity requirements |
| `datapath_alignment` | `0x4DD580` | 735 B | TMEM address is aligned for the data path width |
| `sp_consistency_across_idesc_mod` | `0x500FA0` | 970 B | Sparsity settings in descriptor match modifier |
| `check_sparse_usage` | `0x4DDB80` | 743 B | Sparse mode usage is valid for the environment |

### Guardrail Trap Functions

When a guardrail check fails, it calls a trap function that reports the violation and terminates:

| Trap Intrinsic | Parameters |
|---|---|
| `__cuda_sm10x_tcgen05_guardrail_trap_phase_invalid_during_alloc` | `(.reg .b32 phase)` |
| `__cuda_sm10x_tcgen05_guardrail_trap_current_warp_owner_invalid` | `(.reg .b32 tmem_start_lane_accessed, .reg .b32 cur_warp_id, ...)` |
| `__cuda_sm10x_tcgen05_guardrail_trap_unallocated_columns_access` | `(.reg .b32 col_no_accessed, .reg .b32 alloced_mask, .reg .b32 instr_kind)` |
| `__cuda_sm10x_tcgen05_guardrail_trap_unallocated_columns_being_dealloced` | `(.reg .b32 col_no_being_dealloced, .reg .b32 alloced_mask)` |
| `__cuda_sm10x_tcgen05_guardrail_trap_col_being_dealloced_not_returned_by_alloc` | `(.reg .b32 col_no_being_dealloced_not_returned_by_alloc, ...)` |
| `__cuda_sm10x_tcgen05_guardrail_trap_allocation_granularity_invalid` | `(.reg .b32 nCols)` |
| `__cuda_sm10x_tcgen05_guardrail_trap_access_out_of_physical_bounds` | `(.reg .b32 oob_access_col_no, .reg .b32 instr_kind)` |
| `__cuda_sm10x_tcgen05_guardrail_trap_invalid_datapath_alignment` | `(.reg .b32 dp_lane, .reg .b32 matrix_kind, .reg .b32 valid_alignment_kind, ...)` |
| `__cuda_sm10x_tcgen05_guardrail_trap_sparse_mismatch_between_idesc_mod` | `(.reg .b32 idesc_sp_enabled, .reg .b32 mod_sp_enabled)` |
| `__cuda_sm10x_tcgen05_guardrail_trap_sp_used_in_unsupported_env` | `(.reg .b32 idesc_sp_enabled, .reg .b32 idesc, .reg .b32 mma_kind, .reg .b32 ptx_target, .reg .b32 is_family_portable)` |

These are intrinsic IDs `0x20`--`0x2A` (11 entries total including a mask creation helper) in the intrinsic table.

### Guardrail Check Wrappers

The compiler also generates `.FORCE_INLINE` wrapper functions that combine multiple checks:

| Wrapper | Parameters |
|---|---|
| `__cuda_sm10x_tcgen05_guardrails_check_phase_validity` | `(.reg .u32 dummyInp)` |
| `__cuda_sm10x_tcgen05_guardrails_check_column_allocation` | `(.reg .u32 start_col_num, .reg .u32 num_of_cols, ...)` |
| `__cuda_sm10x_tcgen05_guardrails_check_datapath_validity` | `(.reg .u32 tmem_addr, .reg .u32 ld_or_st)` |
| `__cuda_sm10x_tcgen05_guardrails_check_physical_bounds` | `(.reg .u32 start_col_num, .reg .u32 num_of_cols, ...)` |
| `__cuda_sm10x_tcgen05_guardrails_check_allocation_granularity` | `(.reg .u32 num_of_cols)` |
| `__cuda_sm10x_tcgen05_guardrails_check_datapath_alignment` | `(.reg .u32 tmemAddr, .reg .u32 iDesc, .reg .u32 cta_group, ...)` |

## Bulk Copy Operations (cp.async.bulk.tensor)

TCGen05 is complemented by asynchronous bulk copy operations for loading data into tensor memory. These are registered as separate intrinsic IDs (`0x2B`--`0x3C`, 18 entries) under the `__cuda_sm1xx_*` naming convention:

| Operation | Codegen Handler | Size |
|---|---|---|
| `cp.async.bulk.tensor` (1D--5D, tile/im2col, unicast/multicast) | `sub_5AB460` | 45KB |
| `cp.async.bulk` | `sub_593210` | — |
| `cp.async.mbarrier.arrive` | `sub_4DC180` | — |

The `cp.async.bulk.tensor` handler is 45KB and covers all dimensionality variants (1D through 5D), both tile and im2col access patterns, and unicast/multicast delivery modes.

## SM Availability Gating

### Capability Check

TCGen05 availability is gated by `sub_70FA00(*, 29)`, which checks the target SM version. The check returns true for sm_100, sm_103, and sm_110 (and their `a`/`f` sub-variants) and false for sm_120/sm_121.

### OCG Builtin Names

The OCG (Optimized Code Generation) layer uses short mnemonic names for tcgen05 operations visible in the builtin name lookup (`sub_6C9EB0`):

| OCG Name | Full Operation |
|---|---|
| `tcmma` | tcgen05.mma core multiply-accumulate |
| `tcshift` | tcgen05.shift accumulator data shift |
| `gdesc` | Global descriptor operations |
| `memclear` | Tensor memory clear |
| `sparsify` | Sparsity pattern application |

The `.tcgen05op` string identifies an Ori IR instruction as belonging to the tcgen05 family during the optimizer pipeline.

## Version Compatibility

### CUDA 12.x to 13.0 Breaking Change

ptxas v13.0.88 includes a linker-level version check for tcgen05 objects:

> *"Object '%s' cannot be linked due to version mismatch. Objects using tcgen05 in 12.x cannot be linked with 13.0 or later, they must be rebuilt with latest compiler"*

The `EICOMPAT_ATTR_INST_TCGEN05_MMA_DEPRECATED` attribute tags objects compiled with the 12.x-era tcgen05 encoding, which is binary-incompatible with the 13.0 encoding. The SASS instruction encoding for tcgen05.mma changed between CUDA 12.x and 13.0 — objects must be recompiled.

### SM 100 vs SM 103 Differences

Both sm_100 and sm_103 share the same tcgen05 instruction set and codegen factory (36864). They share all 7 dispatch-table handler functions. The differences between sm_100 and sm_103 are:

- Different Handler A and Handler B capability accessor functions (sm_100: `sub_609C30`/`sub_609BD0`; sm_103: `sub_608F20`/`sub_609D20`)
- Different intrinsic table initializers (sm_100: `sub_60A910`; sm_103: `sub_60A700`)
- sm_103 may expose additional capability flags for GB300-specific features

Both targets produce identical SASS for tcgen05 instructions. The `f` sub-variants (sm_100f, sm_103f) allow cross-compilation within the family: sm_100f code can run on sm_103 hardware.

## Compiler Pipeline

### PTX Parsing and Validation

1. **Lexer** (`sub_720F00`, 64KB): Recognizes `tcgen05.*` tokens during lexical analysis
2. **Validator** (`sub_4C5FB0`, 28KB): Shared MMA/WMMA/tcgen05 validation function. Checks instruction legality for the current SM target, validates operand types, descriptor fields, and modifier combinations
3. **Instruction table** (`sub_46E000`, 93KB): Registers tcgen05 instruction variants with their type combinations (e.g., `.tcgen05op`)

### Intrinsic Dispatch

The intrinsic dispatch table builder (`sub_5D4190`, 41KB) registers tcgen05 handlers:

| Registration | PTX Instruction | Handler | Size |
|---|---|---|---|
| Line 112 | `tcgen05.mma` | `sub_5BBC30` | 90KB |
| Lifecycle | `tcgen05.alloc` | `sub_569180` | — |
| Lifecycle | `tcgen05.relinquish_alloc_permit` | `sub_526370` | — |
| Lifecycle | `tcgen05.dealloc` | `sub_58C7F0` | — |
| Data | `tcgen05.ld` | `sub_574050` | — |
| Data | `tcgen05.ld.red` | `sub_578DB0` | — |
| Data | `tcgen05.st` | `sub_571FE0` | — |
| Sync | `tcgen05.commit` | `sub_56C190` | — |
| Copy | `tcgen05.cp` | `sub_5427F0` | — |
| Compute | `tcgen05.shift` | `sub_4F1A90` | — |
| Compute | `tcgen05.mma.ws` | `sub_58FA20` | — |

### Intrinsic Lowering

The TCGen05 MMA handler (`sub_6D7AF0`, 19KB) and validator (`sub_6D69B0`, 12KB) in the encoding zone handle the lowering from abstract intrinsic operations to concrete SASS encoding. The handler checks modifier consistency:

- *"fused and l16dp32bit must be specified together"*
- *"Inputs vector length is inconsistent with layout and num modifiers"*

### TMEM Address Generation

The TMEM address generator cluster (`sub_70E740`, `sub_70E940`, `sub_70EB00`) generates PTX parameter passing code for tensor memory addresses:

```ptx
st.param.b32 [%s + %d], %s;
ld.param.b32 %s, [%s + %d];
```

### SASS Encoding

The master instruction encoder (`sub_6D9690`, 94KB) handles the final binary encoding. TCGen05 instructions use the Mercury encoding pipeline (encoder factory 36864) with Blackwell-specific opcode tables.

## Function Map

| Address | Size | Identity | Confidence |
|---|---|---|---|
| `sub_4C5FB0` | 28KB | PTX validator (MMA/WMMA/tcgen05 shared) | HIGH |
| `sub_4DA720` | 343 B | tcgen05.mma.ws formatter | HIGH |
| `sub_4DB050` | 439 B | guardrails.in_physical_bounds formatter | HIGH |
| `sub_4DBF20` | 599 B | guardrails.are_columns_allocated formatter | HIGH |
| `sub_4DD580` | 735 B | guardrails.datapath_alignment formatter | HIGH |
| `sub_4DDB80` | 743 B | guardrails.check_sparse_usage formatter | HIGH |
| `sub_4DDE70` | 775 B | guardrails.is_phase_valid formatter | HIGH |
| `sub_4DE180` | 791 B | guardrails.is_current_warp_valid_owner formatter | HIGH |
| `sub_4F0960` | 839 B | guardrails.allocation_granularity formatter | HIGH |
| `sub_4F1A90` | 903 B | tcgen05.shift / tcgen05.cp formatter | HIGH |
| `sub_500FA0` | 970 B | guardrails.sp_consistency_across_idesc_mod formatter | HIGH |
| `sub_526370` | 1,287 B | tcgen05.alloc / tcgen05.relinquish_alloc_permit formatter | HIGH |
| `sub_5427F0` | 1,575 B | tcgen05.commit formatter | HIGH |
| `sub_569180` | — | tcgen05.alloc codegen handler | HIGH |
| `sub_56C190` | 1,842 B | tcgen05.st formatter | HIGH |
| `sub_571FE0` | 2,066 B | tcgen05.ld.red formatter | HIGH |
| `sub_574050` | 2,130 B | tcgen05.dealloc formatter | HIGH |
| `sub_578DB0` | 2,466 B | tcgen05.ld formatter | HIGH |
| `sub_58C7F0` | 4,282 B | tcgen05.relinquish_alloc_permit / tcgen05.dealloc formatter | HIGH |
| `sub_58FA20` | 4,604 B | tcgen05.shift + tcgen05.mma formatter | HIGH |
| `sub_593210` | — | cp.async.bulk codegen | HIGH |
| `sub_5AB460` | 45KB | cp.async.bulk.tensor codegen (1D--5D) | HIGH |
| `sub_5BBC30` | 90KB | tcgen05.mma codegen (main) | HIGH |
| `sub_6D69B0` | 12KB | TCGen05 MMA validator (encoding zone) | MED |
| `sub_6D7AF0` | 19KB | TCGen05 MMA handler (encoding zone) | HIGH |
| `sub_70BC30` | — | TCGen05 parameter helper | MED |
| `sub_70BCC0` | — | TCGen05 parameter helper | MED |
| `sub_70DEF0` | — | TCGen05 parameter helper | MED |
| `sub_70E0E0` | — | SM100 guardrail bounds-check code generator | MED |
| `sub_70E740` | — | TMEM address generator (tmemD) | MED |
| `sub_70E940` | — | TMEM address generator (tmemA) | MED |
| `sub_70EB00` | — | TMEM address generator (scaleTmemA/B, spMetaTmem) | MED |
| `sub_70FA00` | — | Instruction capability checker (29 = tcgen05) | HIGH |
| `sub_8E8A90` | 3.0KB + 949 B | SM 100 latency table (base + TCGEN05 supplement) | HIGH |

## Cross-References

- [Blackwell (SM 100--121)](blackwell.md) — Target-level architecture gating, codegen factory 36864
- [SM Architecture Map](index.md) — Complete SM table, capability dispatch infrastructure
- [GMMA/WGMMA Pipeline](../passes/gmma-pipeline.md) — Predecessor tensor core pipeline (sm_90), same warpgroup execution model
- [Intrinsic Table (608 Entries)](../intrinsics/index.md) — IDs 0x20--0x3C (tcgen05 guardrails + bulk copy)
- [Tensor Core Intrinsics](../intrinsics/tensor.md) — WMMA/MMA/tcgen05 intrinsic lowering detail
- [Late Expansion & Legalization](../passes/late-legalization.md) — tcgen05 guardrail insertion during late expansion
- [SASS Instruction Encoding](../codegen/encoding.md) — Mercury encoder, opcode tables
- [Latency Model & HW Profiles](../scheduling/latency-model.md) — SM 100 TCGEN05 supplement table
- [SASS Text Generation](../codegen/sass-printing.md) — TCGen05 instruction formatters
- [CLI Options](../config/cli-options.md) — `--g-tensor-memory-access-check` option
