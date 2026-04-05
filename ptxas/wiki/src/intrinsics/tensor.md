# Tensor Core Intrinsics

ptxas supports five generations of tensor core operations spanning SM 70 through SM 100. The binary contains three major codegen handlers -- `sub_5C7A50` (173KB, WMMA), `sub_5C10A0` (120KB, MMA), and `sub_5BBC30` (90KB, tcgen05.mma) -- plus four WGMMA handlers, eleven tcgen05 instruction handlers, and ~400 numeric MMA hash table entries. Together these constitute ~500KB of code generation logic dedicated to tensor core instructions, making this the single largest functional subsystem in ptxas.

| | |
|---|---|
| **WMMA codegen** | `sub_5C7A50` (173KB) -- wmma.mma instruction code generation |
| **MMA codegen** | `sub_5C10A0` (120KB) -- mma.sync instruction code generation |
| **TCGen05 MMA codegen** | `sub_5BBC30` (90KB) -- tcgen05.mma instruction code generation |
| **WMMA load/store** | `sub_5A0EA0` (7.8KB), `sub_5A8E40` (9.8KB), `sub_5A6BD0` (8.8KB), `sub_5A2D10` (8.1KB) |
| **WGMMA handlers** | `sub_50AC70` (1.3KB), `sub_4DA380` (295B), `sub_4DA4B0` (295B), `sub_4DA5E0` (311B) |
| **MMA validator** | `sub_4C2FD0` (12.2KB), `sub_49BBA0` (11.4KB), `sub_4BFED0` (10.3KB) |
| **Numeric MMA hash** | ~400 entries at compilation context offset `a1+816` |
| **Prototype generator** | `sub_5FF700` (354KB) -- generates `.weak .func` PTX declarations |
| **SASS MMA encoders** | `sub_6D4350`, `sub_6D7AF0`, `sub_6D5CB0`, `sub_6D69B0` |

## Tensor Core Generations

ptxas tracks five distinct tensor core generations, each introducing new SASS opcodes, data types, and matrix shapes. The internal scheduling counters (visible in DUMPIR statistics output) reveal the SASS-level taxonomy.

| Gen | SM | SASS Opcodes | PTX API | Key Addition |
|---|---|---|---|---|
| 1st | 75 (Turing) | HMMA (m16n8k8, m8n8k16) | `wmma.mma` | FP16 tensor cores; INT8/INT4/B1 WMMA |
| 2nd | 80 (Ampere) | HMMA (m16n8k16), IMMA, DMMA, BMMA | `wmma.mma`, `mma.sync` | BF16, TF32, FP64, structured sparsity |
| 3rd | 89 (Ada) | HMMA (extended) | `mma.sync` | FP8 (E4M3, E5M2), block-scale MMA |
| 4th | 90 (Hopper) | WGMMA | `wgmma.mma_async` | Warpgroup MMA, async pipeline, sub-byte sparse |
| 5th | 100 (Blackwell) | UTCHMMA, UTCIMMA, tcmma | `tcgen05.mma`, `tcgen05.mma.ws` | Tensor memory (TMEM), warp-shared, block-scale |

### Scheduling Unit Names

The binary's statistics printer functions (clones at 0x700-byte intervals from `sub_ABBA50`) emit per-unit throughput counters using the internal SASS operation classification:

| Counter | SASS Operation | Matrix Shape | Description |
|---|---|---|---|
| `hmma1688` | HMMA m16n8k8 | 16x8x8 | 1st-gen FP16 MMA (Turing) |
| `hmma1688f16` | HMMA m16n8k8 (f16 accum) | 16x8x8 | FP16 accumulation variant |
| `hmma16816` | HMMA m16n8k16 | 16x8x16 | 2nd-gen FP16 MMA (Ampere+) |
| `hmma16816f16` | HMMA m16n8k16 (f16 accum) | 16x8x16 | FP16 accumulation variant |
| `hmmaSp1688` | HMMA.SP m16n8k8 | 16x8x(8*2) | Sparse FP16 (2:4 sparsity) |
| `hmmaSp1688f16` | HMMA.SP m16n8k8 (f16 accum) | 16x8x(8*2) | Sparse FP16, f16 accum |
| `imma16816` | IMMA m16n8k16 | 16x8x16 | Integer MMA (INT8) |
| `imma16832` | IMMA m16n8k32 | 16x8x32 | Integer MMA (INT4/sub-byte) |
| `immaSp8832` | IMMA.SP m8n8k32 | 8x8x(32*2) | Sparse integer (m8 variant) |
| `immaSp16832` | IMMA.SP m16n8k32 | 16x8x(32*2) | Sparse integer (m16 variant) |
| `dmma` | DMMA | 8x8x4 | FP64 tensor MMA |
| `fma64` | FMA64 | -- | FP64 FMA (non-tensor) |

Format strings from the binary:

```
# [est hmma1688=%d] [est hmma1688f16=%d] [est hmmaSp1688=%d] [est hmmaSp1688f16=%d]
# [est hmma16816=%d] [est hmma16816f16=%d]
# [est imma16816=%d] [est imma16832=%d] [est immaSp8832=%d] [est immaSp16832=%d]
# [est dmma=%d] [est fma64=%d]
# [hmma1688 thru=%f] [hmma1688f16 thru=%f] [hmmaSp1688 thru=%f] [hmmaSp1688f16 thru=%f]
# [hmma16816 thru=%f] [hmma16816f16 thru=%f]
# [imma16816 thru=%f] [imma16832 thru=%f] [immaSp8832 thru=%f] [immaSp16832 thru=%f]
# [dmma thru=%f] [fma64 thru=%f]
```

These counters are emitted by the post-scheduling statistics pass. The scheduler treats each counter as a separate functional unit throughput class, with dedicated latency table entries:

| Ori Opcode | Latency Class | Description |
|---|---|---|
| `0x144` | 600 | Tensor fence |
| `0x145`--`0x146` | 759 | HMMA/BMMA tensor core operations |
| `0x147`--`0x148` | 757 or 761 | Narrow/wide DP tensor operations |
| `0x149` | 604 | Tensor sync |

## PTX-Level Instruction Lowering

### WMMA Instructions

The WMMA (Warp Matrix Multiply-Accumulate) API is the oldest tensor core interface. Five PTX instructions are registered in the opcode dispatch table at `sub_5D4190`:

| PTX Instruction | Codegen Handler | Size | Purpose |
|---|---|---|---|
| `wmma.load.a` | `sub_5A0EA0` | 7,779B | Load matrix fragment A |
| `wmma.load.b` | `sub_5A8E40` | 9,757B | Load matrix fragment B |
| `wmma.load.c` | `sub_5A6BD0` | 8,813B | Load accumulator fragment C |
| `wmma.store.d` | `sub_5A2D10` | 8,074B | Store result fragment D |
| `wmma.mma` | `sub_5C7A50` | 173KB | Matrix multiply-accumulate |

All five handlers allocate a 50,000-byte code generation buffer. The load/store handlers are ~8--10KB each and cover the combinatorial product of shape, layout, data type, and address space. The `wmma.mma` handler at 173KB is the largest single codegen handler in ptxas.

Instruction property accessors used by WMMA codegen:

| Accessor | Purpose |
|---|---|
| `sub_7075C0` | Get instruction flag A (layout/type encoding) |
| `sub_707BC0` | Get instruction flag B (variant/mode encoding) |
| `sub_7075E0` | Get layout string (row/col) |
| `sub_707BE0` | Get shape string (m16n16k16 etc.) |
| `sub_70A810` | Get scale string (satfinite etc.) |

### WMMA Shapes and Types

**sm_70 (Volta/Turing) -- 1st generation:**

| Shape | Data Types | Accumulator | Regs (A) | Regs (B) | Regs (C/D) |
|---|---|---|---|---|---|
| m16n16k16 | f16 | f16 or f32 | 8 x b32 | 8 x b32 | 4 x b32 (f16) or 8 x b32 (f32) |
| m32n8k16 | f16 | f16 or f32 | 8 x b32 | 8 x b32 | 4 x b32 (f16) or 8 x b32 (f32) |
| m8n32k16 | f16 | f16 or f32 | 8 x b32 | 8 x b32 | 4 x b32 (f16) or 8 x b32 (f32) |

**sm_72 (Turing) -- integer WMMA extension:**

| Shape | Data Types | Accumulator | Regs (A) | Regs (B) | Regs (C/D) |
|---|---|---|---|---|---|
| m16n16k16 | s8, u8 | s32 | 2 x b32 | 2 x b32 | 8 x b32 |
| m32n8k16 | s8, u8 | s32 | 4 x b32 | 1 x b32 | 8 x b32 |
| m8n32k16 | s8, u8 | s32 | 1 x b32 | 4 x b32 | 8 x b32 |
| m8n8k32 | s4, u4 (sub-byte) | s32 | 1 x b32 | 1 x b32 | 2 x b32 |
| m8n8k128 | b1 (1-bit) | s32 | 1 x b32 | 1 x b32 | 2 x b32 |

**sm_80 (Ampere) -- 2nd generation extensions:**

| Shape | Data Types | Accumulator | Notes |
|---|---|---|---|
| m16n16k16 | bf16 | f32 | New BF16 support, 4 x b32 per fragment |
| m16n16k8 | tf32 | f32 | New TF32 support, 4 x b32 per fragment |
| m8n8k4 | f64 | f64 | Double-precision MMA, 1 x f64 per A/B, 2 x f64 per C/D |

All WMMA shapes support three address spaces (generic, global, shared) and optional descriptor-based addressing (`_desc` variants). The binary contains separate intrinsic registrations for each combination, with the full prototype available in the string table.

### MMA (mma.sync) Instructions

The `mma.sync` API uses a single PTX opcode `mma` dispatched to `sub_5C10A0` (120KB). Unlike WMMA, it uses asymmetric matrix shapes with M and N decoupled from K, and operates at a single-warp granularity.

The numeric MMA hash table at `a1+816` collapses the full variant space (shape + type + layout) into ~400 hash entries. Each entry maps a numeric string key (e.g., `"2644314910"`) to a specific codegen handler function pointer, avoiding multi-dimensional dispatch.

**sm_80+ MMA intrinsics (IDs `0x209`--`0x22F`):**

The 39 intrinsics registered under `__cuda_sm_8x_mma_*` cover:

| Intrinsic Pattern | Layout | Types (D, C, A, B) | Regs |
|---|---|---|---|
| `mma_row_col_f16_f16_f16_f16` | row x col | f16, f16, f16, f16 | D: 4 x b32 |
| `mma_row_col_f32_f16_f16_f16` | row x col | f32, f16, f16, f16 | D: 8 x b32 |
| `mma_row_col_f32_f16_f16_f32` | row x col | f32, f32, f16, f16 | D: 8 x b32, C: 8 x b32 |
| `mma_col_col_*` | col x col | same set | same |
| `mma_row_row_*` | row x row | same set | same |
| `mma_col_row_*` | col x row | same set | same |
| `mma_shfl_f16` | -- | shuffle f16 | D: 2 x b32 |
| `mma_shfl_f32` | -- | shuffle f32 | D: 4 x b32 |

Prototype examples from the binary:

```
.weak .func (.param .align 16 .b32 mma_dst[4])
    __cuda_sm_8x_mma_row_col_f16_f16_f16_f16
    (.reg .b32 a0, .reg .b32 a1, .reg .b32 b0, .reg .b32 b1,
     .reg .b32 c0, .reg .b32 c1, .reg .b32 c2, .reg .b32 c3);

.weak .func (.param .align 16 .b32 mma_dst[8])
    __cuda_sm_8x_mma_row_col_f32_f16_f16_f32
    (.reg .b32 a0, .reg .b32 a1, .reg .b32 b0, .reg .b32 b1,
     .reg .b32 c0, .reg .b32 c1, .reg .b32 c2, .reg .b32 c3,
     .reg .b32 c4, .reg .b32 c5, .reg .b32 c6, .reg .b32 c7);
```

Note the `.param .align 16 .b32 mma_dst[N]` return convention -- MMA results are returned through aligned parameter space, not registers, because the warp-cooperative nature of the operation means each thread holds only a fragment.

### MMA Shape Summary Across Generations

| Shape | Types | SM Floor | SASS Opcode | Notes |
|---|---|---|---|---|
| m8n8k4 | f64 | 80 | DMMA | Double-precision |
| m8n8k16 | f16 | 75 | HMMA | Original Turing shape |
| m8n8k32 | s4/u4 | 75 | IMMA | Sub-byte integer |
| m8n8k128 | b1 | 75 | BMMA | 1-bit (XOR/AND pop) |
| m16n8k8 | f16 | 75 | HMMA | Asymmetric Turing shape |
| m16n8k16 | f16, bf16, s8/u8 | 80 | HMMA, IMMA | Primary Ampere shape |
| m16n8k32 | s8/u8, s4/u4 | 80/90 | IMMA | Integer, sub-byte at sm_90 |
| m16n8k64 | s4/u4 | 90 | IMMA | Hopper sub-byte extension |
| m16n8k128 | s4/u4 (sparse), b1 | 90 | IMMA, BMMA | Hopper sparse sub-byte |
| m16n8k256 | b1 | 90/100 | BMMA | Extended 1-bit MMA |

### MMA Validation

Three validator functions gate MMA features by SM version:

**`sub_4C2FD0` (12.2KB) -- WMMA/MMA master validator:**

Performs three-way version checks:
- SM 75: base WMMA (f16)
- SM 80: extended types (BF16, TF32, FP64 -- `"MMA with double types"`)
- SM 90: WGMMA features
- FP8: `"mma with FP8 floating point type"` (gated by sm_89+)

**`sub_49BBA0` (11.4KB) -- MMA type/scale validator:**

Validates FP8 and block-scale configurations:
- `"mma with FP8 floating point type"` -- sm_89+ gate
- `"mma with FP8 floating point type and FP16 accumulation"` -- additional FP16 accum check
- `"mma with FP8 floating point type and .m16n8k16 shape"` -- shape/type cross-validation
- `"Sparse mma with block scale"` -- block-scale + sparsity interaction
- `.block_scale` modifier validation

**`sub_4BFED0` (10.3KB) -- WMMA shape validator:**

Validates WMMA-specific shapes and the `.aligned` modifier:
- `".aligned modifier for wmma"` -- alignment enforcement
- SM 75/80 version checks for shape legality

**`sub_490F90` -- Integer MMA validator:**

Checks integer MMA shape validity: `"Integer MMA with shape "` -- validates m/n/k dimensions against the SM-level capability set.

**`sub_494210` (2.3KB) -- Sparse GMMA validator:**

Validates sparse MMA metadata: `"Sparse GMMA with "` -- checks 2:4 sparsity pattern encoding.

**`sub_495900` -- WMMA floating-point validator:**

Checks: `"'wmma.mma' with floating point type"` -- validates FP type compatibility with the target shape.

**`sub_4428E0` -- FP64 MMA validator:**

Validates: `"mma with .f64 type"` -- gates double-precision MMA on sm_80+.

## sm_90+ Sub-Byte MMA Intrinsics (IDs `0x23A`--`0x25F`)

Hopper introduces 38 sub-byte MMA intrinsics covering s4/u4 sparse operations at warp granularity. These are distinct from the WGMMA API and provide backward-compatible sub-byte operations through the classical `mma.sync` interface.

**Dense sub-byte (m8n8k32, m16n8k32, m16n8k64):**

| Shape | Type Combinations | Variants | Count |
|---|---|---|---|
| m8n8k32 | s4xs4, s4xu4, u4xs4, u4xu4 | plain + satfinite | 8 |
| m16n8k32 | s4xs4, s4xu4, u4xs4, u4xu4 | plain + satfinite | 8 |
| m16n8k64 | s4xs4, s4xu4, u4xs4, u4xu4 | plain + satfinite | 8 |

**Sparse sub-byte (m16n8k64, m16n8k128):**

| Shape | Type Combinations | Variants | Count |
|---|---|---|---|
| m16n8k64 (sparse) | s4xs4, s4xu4, u4xs4, u4xu4 | plain + satfinite, split _0/_1 | 16 |
| m16n8k128 (sparse) | s4xs4, s4xu4, u4xs4, u4xu4 | plain + satfinite | 8 |

The `_0` and `_1` suffixes on sparse m16n8k64 represent the two halves of a split operation -- the K dimension is decomposed into two steps for the sparsity pattern. The sparse variants take an additional `e` (metadata) operand encoding the 2:4 sparsity pattern.

**Bit-operations (b1):**

| Shape | Operation | SM | Intrinsic |
|---|---|---|---|
| m8n8k128 | XOR | 90 | `__cuda_sm_9x_mma_bit_internal_xor_m8n8k128` |
| m16n8k128 | XOR | 90 | `__cuda_sm_9x_mma_bit_internal_xor_m16n8k128` |
| m16n8k256 | XOR | 90 | `__cuda_sm_9x_mma_bit_internal_xor_m16n8k256` |

Prototype example (sparse m16n8k128):

```
.weak .func (.param .align 16 .b32 mma_dst[4])
    __cuda_sm_9x_mma_sub_byte_internal_sparse_m16n8k128_s4_s4
    (.reg .b32 a0, .reg .b32 a1, .reg .b32 a2, .reg .b32 a3,
     .reg .b32 b0, .reg .b32 b1, .reg .b32 b2, .reg .b32 b3,
     .reg .b32 c0, .reg .b32 c1, .reg .b32 c2, .reg .b32 c3,
     .reg .b32 e);
```

The sparse m16n8k128 variant takes 4 A-operands, 4 B-operands (double-width due to sparsity encoding), 4 C-operands, and 1 metadata operand `e`.

## sm_100 Blackwell MMA (IDs `0x230`--`0x239`)

Blackwell introduces 10 intrinsics for extended MMA operations:

**HMMA/IMMA metadata helpers (`__cuda_sm_10x_*_mdata_*`):**

| Intrinsic | Shape | Returns | Inputs |
|---|---|---|---|
| `__cuda_sm_10x_hmma_mdata_m16n8k16` | m16n8k16 | `ret_dst[3]` | a0, a1, e, f_temp |
| `__cuda_sm_10x_hmma_mdata_m16n8k32` | m16n8k32 | `ret_dst[5]` | a0, a1, a2, a3, e, f_temp |
| `__cuda_sm_10x_imma_mdata_m16n8k32` | m16n8k32 | `ret_dst[3]` | a0, a1, e, f_temp |
| `__cuda_sm_10x_imma_mdata_m16n8k64` | m16n8k64 | `ret_dst[5]` | a0, a1, a2, a3, e, f_temp |

These `mdata` functions compute sparse metadata for Blackwell's 5th-gen tensor cores. The `e` parameter is the sparsity selector, `f_temp` is a scratch register. The return array includes the transformed A-operands plus the computed metadata word.

**Bit MMA (AND + XOR for sm_100):**

| Intrinsic | Shape | Operation | Regs |
|---|---|---|---|
| `__cuda_sm_10x_mma_bit_internal_and_m8n8k128` | m8n8k128 | AND | D: 2, A: 1, B: 1, C: 2 |
| `__cuda_sm_10x_mma_bit_internal_and_m16n8k128` | m16n8k128 | AND | D: 4, A: 2, B: 1, C: 4 |
| `__cuda_sm_10x_mma_bit_internal_and_m16n8k256` | m16n8k256 | AND | D: 4, A: 4, B: 2, C: 4 |
| `__cuda_sm_10x_mma_bit_internal_xor_m8n8k128` | m8n8k128 | XOR | same as AND |
| `__cuda_sm_10x_mma_bit_internal_xor_m16n8k128` | m16n8k128 | XOR | same as AND |
| `__cuda_sm_10x_mma_bit_internal_xor_m16n8k256` | m16n8k256 | XOR | same as AND |

Blackwell adds the AND reduction mode for 1-bit MMA (sm_90 only had XOR).

## WGMMA -- Warpgroup MMA (SM 90+)

WGMMA (Warp Group Matrix Multiply-Accumulate) operates at warpgroup granularity (4 warps, 128 threads) and uses an asynchronous pipeline protocol. Four PTX instructions are registered:

| PTX Instruction | Handler | Size | Role |
|---|---|---|---|
| `wgmma.mma_async` | `sub_50AC70` | 1,282B | Dispatch asynchronous MMA operation |
| `wgmma.fence` | `sub_4DA380` | 295B | Open pipeline stage |
| `wgmma.commit_group` | `sub_4DA4B0` | 295B | Close pipeline stage |
| `wgmma.wait_group` | `sub_4DA5E0` | 311B | Wait for N committed groups |

### WGMMA Pipeline Protocol

The hardware requires strict sequencing:

```
wgmma.fence              -- open pipeline stage
wgmma.mma_async  (1..N)  -- asynchronous MMA operations sharing accumulators
wgmma.commit_group       -- close pipeline stage
wgmma.wait_group N       -- wait for N outstanding groups to complete
```

Between fence and wait, strict register constraints apply:
1. No non-WGMMA definitions of accumulator registers
2. No non-WGMMA reads of accumulator registers
3. No non-WGMMA definitions of WGMMA input registers (including descriptors)

Violation of any constraint triggers pipeline serialization via `sub_AE47B0`, which collapses the pipeline to individual fence/mma/commit/wait per operation. The serialization reason is reported through warning codes `0x1D55`--`0x1D5E` (see [GMMA Pipeline](../passes/gmma-pipeline.md)).

### WGMMA Descriptor Format

The `wgmma.mma_async` handler (`sub_50AC70`, 1,282 bytes) encodes the operation's matrix dimensions, data types, layout, and scale factors into the instruction. The A operand can be either a register operand or a **descriptor** -- a 64-bit value encoding the matrix base address, leading dimension, stride, and swizzle pattern. The B operand is always descriptor-based.

The descriptor format allows the hardware to fetch matrix data directly from shared memory via the TMA (Tensor Memory Accelerator), bypassing register file involvement for the B matrix operand entirely.

### Ori Internal Encoding

| Constant | Value | Meaning |
|---|---|---|
| WGMMA opcode | 309 | Ori opcode for `wgmma.mma_async` |
| Arrive opcode (masked) | 271 | `opcode & 0xFFFFCFFF` for `_warpgroup.arrive/wait` |
| Commit opcode | 323 | Ori opcode for `_warpgroup.commit_batch` |
| Accum reg\_type | 6 | `vreg+64` value for tensor/accumulator registers |
| Accum src tag | `0x90000000` | High nibble tag for source accumulator set |
| Accum dst tag | `0x10000000` | High nibble tag for destination accumulator set |

### Compiler-Inserted Warpgroup Instructions

The GMMA pipeline passes (phases 85 and 87) insert three compiler-internal pseudo-operations prefixed with underscore:

| Pseudo-Op | SASS Output | Purpose |
|---|---|---|
| `_warpgroup.arrive` | `WARPSYNC` / `BAR.ARRIVE` | Warpgroup synchronization (arrive) |
| `_warpgroup.wait` | `WARPSYNC` / `BAR.WAIT` | Warpgroup synchronization (wait) |
| `_warpgroup.commit_batch` | `DEPBAR` variant | Warpgroup dependency barrier |

These are not directly written by the programmer. The compiler inserts them to manage register ownership transfer between the warpgroup's register file and the tensor core's accumulator pipeline.

## TCGen05 -- 5th Generation Tensor Cores (SM 100+)

Blackwell introduces TCGen05 (Tensor Core Generation 5), which operates through **tensor memory** (TMEM) -- a dedicated on-chip memory visible only to the tensor core engine, separate from the register file and shared memory. Eleven PTX instructions are registered:

| PTX Instruction | Handler | Size | Purpose |
|---|---|---|---|
| `tcgen05.mma` | `sub_5BBC30` | 90KB | Tensor core MMA from TMEM |
| `tcgen05.mma.ws` | `sub_58FA20` | 4,604B | Warp-shared MMA variant |
| `tcgen05.ld` | `sub_574050` | -- | Load data into TMEM |
| `tcgen05.ld.red` | `sub_578DB0` | -- | Load-reduce into TMEM |
| `tcgen05.st` | `sub_571FE0` | -- | Store from TMEM |
| `tcgen05.cp` | `sub_5427F0` | -- | Copy within TMEM |
| `tcgen05.commit` | `sub_56C190` | -- | Commit pending operations |
| `tcgen05.shift` | `sub_4F1A90` | -- | Shift TMEM contents |
| `tcgen05.alloc` | `sub_569180` | -- | Allocate TMEM columns |
| `tcgen05.dealloc` | `sub_58C7F0` | -- | Deallocate TMEM columns |
| `tcgen05.relinquish_alloc_permit` | `sub_526370` | -- | Release allocation rights |

### TCGen05 MMA Codegen

The `tcgen05.mma` handler (`sub_5BBC30`, 90KB) is the third-largest codegen handler in ptxas. It:

1. Allocates a 50,000-byte code generation buffer
2. Validates tcgen05 capability via `sub_70FA00(*, 29)`
3. Handles standard, sparse (`.sp`), and warp-shared (`.ws`) variants
4. Extracts sparse metadata via `sub_70F0A0`
5. Generates TMEM address computation code

The `tcgen05.mma.ws` formatter (`sub_58FA20`, 4,604B, also used for `tcgen05.shift`) handles the warp-shared variant where multiple warps contribute to a single MMA operation.

### TCGen05 SASS Encoding

At the SASS level, TCGen05 operations are encoded by four specialized Mercury encoder functions:

| Address | Handler | Purpose |
|---|---|---|
| `sub_6D4350` | SASS MMA encoder | Primary MMA SASS emission |
| `sub_6D7AF0` | SASS MMA encoder | Alternate MMA variant |
| `sub_6D5CB0` | SASS MMA encoder | Additional MMA mode |
| `sub_6D69B0` | SASS MMA encoder | Additional MMA mode |

The SASS encoder at `sub_6D4350` references the `tcmma` operation namespace and validates block-scale configurations:

- `"tcmma_*_o must be specified with blockscale"` -- output operand requires block-scale modifier
- `"uri width for tcmma_*_o must be 2"` -- output URI width constraint
- `"tcmma_*_q with blockscale must have uri width of 2"` -- scale factor operand constraint
- `"tcmma_*_mxq must be specified with blockscale"` -- MX quantization operand constraint
- `"For UTCHMMA, #scaleU4 must be 0 in SPA 10.1."` -- SM 100 vs 103 compatibility

The string `"UTCHMMA"` (Unified Tensor Core HMMA) and `"tcmma"` (Tensor Core MMA) are the internal SASS-level names for Blackwell's tensor core operations.

### TCGen05 Guardrails

Blackwell includes a debug/validation mode activated by `--g-tensor-memory-access-check`. When enabled, ptxas wraps TMEM operations with guardrail trap functions. Ten guardrail intrinsics are registered (IDs `0x20`--`0x2A`):

| Intrinsic | Trap Condition |
|---|---|
| `tcgen05_guardrail_trap_phase_invalid_during_alloc` | TMEM allocation during invalid phase |
| `tcgen05_guardrail_trap_current_warp_owner_invalid` | Warp accessing TMEM it does not own |
| `tcgen05_guardrail_trap_unallocated_columns_access` | Access to unallocated TMEM columns |
| `tcgen05_guardrail_trap_unallocated_columns_being_dealloced` | Deallocation of unallocated columns |
| `tcgen05_guardrail_trap_col_being_dealloced_not_returned_by_alloc` | Dealloc of column not from alloc |
| `tcgen05_guardrail_trap_allocation_granularity_invalid` | Invalid allocation granularity |
| `tcgen05_guardrail_trap_access_out_of_physical_bounds` | Out-of-bounds TMEM access |
| `tcgen05_guardrail_trap_invalid_datapath_alignment` | TMEM datapath misalignment |
| `tcgen05_guardrail_trap_sparse_mismatch_between_idesc_mod` | Sparsity mismatch in instruction descriptor |
| `tcgen05_guardrail_trap_sp_used_in_unsupported_env` | Sparsity in unsupported config |

Eight guardrail PTX instructions are registered in the opcode dispatch table:

| PTX Instruction | Check |
|---|---|
| `_tcgen05.guardrails.is_phase_valid` | Phase validity for alloc |
| `_tcgen05.guardrails.is_current_warp_valid_owner` | Warp ownership |
| `_tcgen05.guardrails.are_columns_allocated` | Column allocation status |
| `_tcgen05.guardrails.in_physical_bounds` | Physical bounds check |
| `_tcgen05.guardrails.allocation_granularity` | Granularity validation |
| `_tcgen05.guardrails.datapath_alignment` | Alignment validation |
| `_tcgen05.guardrails.sp_consistency_across_idesc_mod` | Sparsity consistency |
| `_tcgen05.guardrails.check_sparse_usage` | Sparse usage validation |

The guardrail check functions are `.FORCE_INLINE` and return a boolean `retVal`:

```
.FORCE_INLINE .func (.reg .b32 retVal)
    __cuda_sm10x_tcgen05_guardrails_check_datapath_alignment
    (.reg .u32 tmemAddr, .reg .u32 iDesc, .reg .u32 cta_group,
     .reg .u32 hasWS, .reg .u32 hasSP, .reg .u32 matrix_kind);
```

The parameters reveal TMEM addressing structure: `tmemAddr` (TMEM base address), `iDesc` (instruction descriptor), `cta_group` (CTA group for cluster operations), `hasWS` (warp-shared flag), `hasSP` (sparse flag), `matrix_kind` (operand role).

### Block-Scale MMA

Block-scale MMA allows per-block scaling factors for mixed-precision computation. In ptxas, this is gated by the `.block_scale` modifier on the PTX `mma` instruction:

- Validator `sub_49BBA0` checks `".block_scale"` and `"Sparse mma with block scale"` (sparsity + block-scale interaction)
- Additional intrinsic suffix `__cuda_sm_100_tcgen05_ld_immhalfSplitOff` and variants handle block-scale-aware loads
- The `bf16x2.ue8m0x2` type string in the binary indicates UE8M0 (unsigned exponent-only) scale factor format for MX (microscaling) quantization

## Intrinsic Registration Summary

### Full Tensor Core Intrinsic ID Map

| ID Range | Count | SM | Category | SASS Target |
|---|---|---|---|---|
| `0x89`--`0x1FA` (subset) | ~200 | 70+ | `__cuda_sm70_wmma_*` -- WMMA load/store/mma (f16) | HMMA |
| `0x1FB`--`0x208` | 14 | 80 | `__cuda_sm80_*` -- bf16/tf32/s4/s8/b1 MMA, createpolicy | HMMA, IMMA, DMMA, BMMA |
| `0x209`--`0x22F` | 39 | 80+ | `__cuda_sm_8x_mma_*` -- direct MMA operations | HMMA, IMMA |
| `0x230`--`0x239` | 10 | 100 | `__cuda_sm_10x_*` -- hmma/imma mdata + bit MMA | UTCHMMA, UTCIMMA |
| `0x23A`--`0x25F` | 38 | 90 | `__cuda_sm_9x_mma_sub_byte_internal_*` -- sub-byte sparse | IMMA |

### TCGen05 Intrinsics (Not in Master ID Table)

TCGen05 operations are dispatched through the named opcode table, not the numeric ID table. The 11 `tcgen05.*` instructions are registered directly in the opcode hash map at `a1+808`:

| PTX Opcode | Codegen Handler |
|---|---|
| `tcgen05.alloc` | `sub_569180` |
| `tcgen05.relinquish_alloc_permit` | `sub_526370` |
| `tcgen05.dealloc` | `sub_58C7F0` |
| `tcgen05.ld` | `sub_574050` |
| `tcgen05.ld.red` | `sub_578DB0` |
| `tcgen05.st` | `sub_571FE0` |
| `tcgen05.commit` | `sub_56C190` |
| `tcgen05.cp` | `sub_5427F0` |
| `tcgen05.shift` | `sub_4F1A90` |
| `tcgen05.mma` | `sub_5BBC30` |
| `tcgen05.mma.ws` | `sub_58FA20` |

### OCG-Level MMA Operations

The OCG system at `sub_6C9EB0` registers additional SASS-level MMA operations for SM100+:

```
tcmma (2x64dp128bitlw02lw13, 2x64dp128bitlw01lw23, 4x32dp128bit)
tcbar, mmareadshma
16dp32bitt0t15, 16dp32bitt16t31
sparsify, spfactor2to4
tcshift, tcatomsws, tcldsws, tcstsws
```

These are internal Mercury-level operations that do not correspond 1:1 to PTX instructions. The `tcmma` variants encode the specific SASS datapath configuration: `2x64dp128bit` means 2 datapaths, 64-element wide, 128-bit lane width.

## Data Type Matrix

Complete data type support across tensor core generations as visible in the binary:

| Data Type | PTX Type | Width | SM Floor | Use |
|---|---|---|---|---|
| FP16 | `.f16` | 16b | 75 | Primary HMMA operand |
| BF16 | `.bf16` | 16b | 80 | HMMA alternate format |
| TF32 | `.tf32` | 19b (stored as 32b) | 80 | Reduced-precision FP32 |
| FP32 | `.f32` | 32b | 75 | HMMA accumulator |
| FP64 | `.f64` | 64b | 80 | DMMA (double-precision) |
| FP8 E4M3 | `.e4m3` | 8b | 89 | Ada/Hopper FP8 MMA |
| FP8 E5M2 | `.e5m2` | 8b | 89 | Ada/Hopper FP8 MMA |
| INT8 | `.s8`, `.u8` | 8b | 72 | IMMA integer MMA |
| INT4 | `.s4`, `.u4` | 4b | 72 | IMMA sub-byte MMA |
| INT1 | `.b1` | 1b | 75 | BMMA 1-bit MMA (XOR/AND) |
| UE8M0 | `.ue8m0x2` | 8b (packed) | 100 | Block-scale exponent factor |
| B1024 | `.b1024` | 1024b | 100 | TMEM-width operand |

## ELF Metadata

The cubin output includes tensor-core-specific EIATTR attributes:

| EIATTR | Purpose |
|---|---|
| `EIATTR_SPARSE_MMA_MASK` | Records which MMA operations use structured sparsity |
| `EIATTR_TCGEN05_1CTA_USED` | Kernel uses 1-CTA tcgen05 operations |
| `EIATTR_TCGEN05_2CTA_USED` | Kernel uses 2-CTA tcgen05 operations |

## Knobs and Options

| Knob / Option | Purpose |
|---|---|
| `suppress-sparse-mma-advisory-info` | Suppress advisory info for `mma.sp` operations |
| `--g-tensor-memory-access-check` | Enable tcgen05 guardrail instrumentation |

## Key Function Table

| Address | Size | Identity | Confidence |
|---|---|---|---|
| `0x5C7A50` | 173KB | WMMA.MMA codegen (all shapes/types/layouts) | 98% |
| `0x5C10A0` | 120KB | MMA.sync codegen (post-Volta shapes) | 98% |
| `0x5BBC30` | 90KB | TCGen05.MMA codegen (Blackwell TMEM-based) | 98% |
| `0x5A0EA0` | 7,779B | wmma.load.a formatter | 95% |
| `0x5A8E40` | 9,757B | wmma.load.b formatter | 95% |
| `0x5A6BD0` | 8,813B | wmma.load.c formatter | 95% |
| `0x5A2D10` | 8,074B | wmma.store.d formatter | 95% |
| `0x50AC70` | 1,282B | wgmma.mma_async handler | 99% |
| `0x4DA380` | 295B | wgmma.fence handler | 99% |
| `0x4DA4B0` | 295B | wgmma.commit_group handler | 99% |
| `0x4DA5E0` | 311B | wgmma.wait_group handler | 99% |
| `0x58FA20` | 4,604B | tcgen05.mma.ws / tcgen05.shift formatter | 95% |
| `0x4DA720` | 343B | tcgen05.mma.ws formatter | 90% |
| `0x569180` | -- | tcgen05.alloc handler | 90% |
| `0x526370` | -- | tcgen05.relinquish_alloc_permit handler | 90% |
| `0x58C7F0` | -- | tcgen05.dealloc handler | 90% |
| `0x574050` | -- | tcgen05.ld handler | 90% |
| `0x578DB0` | -- | tcgen05.ld.red handler | 90% |
| `0x571FE0` | -- | tcgen05.st handler | 90% |
| `0x56C190` | -- | tcgen05.commit handler | 90% |
| `0x5427F0` | -- | tcgen05.cp handler | 90% |
| `0x4F1A90` | -- | tcgen05.shift handler | 90% |
| `0x4C2FD0` | 12.2KB | WMMA/MMA master validator (sm_75/80/90) | 90% |
| `0x49BBA0` | 11.4KB | MMA type/scale validator (FP8, block-scale) | 90% |
| `0x4BFED0` | 10.3KB | WMMA shape validator | 90% |
| `0x490F90` | -- | Integer MMA shape validator | 85% |
| `0x494210` | 2,276B | Sparse GMMA validator | 85% |
| `0x495900` | -- | WMMA floating-point type validator | 85% |
| `0x496570` | -- | FP8 MMA shape validator | 85% |
| `0x4961F0` | -- | FP8 MMA accumulation validator | 85% |
| `0x4428E0` | -- | FP64 MMA type validator | 85% |
| `0x6D4350` | -- | SASS TCGen05 MMA encoder (UTCHMMA/tcmma) | 85% |
| `0x6D7AF0` | -- | SASS MMA encoder variant | 85% |
| `0x6D5CB0` | -- | SASS MMA encoder variant | 85% |
| `0x6D69B0` | -- | SASS MMA encoder variant | 85% |
| `0x50D4B0` | 1,187B | ldmatrix formatter | 90% |
| `0x4DAEA0` | -- | movmatrix formatter | 90% |
| `0x4F05D0` | -- | stmatrix formatter | 90% |
| `0x5D1660` | 46KB | Master intrinsic registration (608 entries) | 99% |
| `0x5D4190` | 41KB | Opcode dispatch (MMA hash table builder) | 99% |
| `0x5FF700` | 354KB | Prototype generator (`.weak .func` declarations) | 99% |

## Cross-References

- [Intrinsic Table Architecture](index.md) -- Master registration, ID ranges, opcode dispatch
- [GMMA/WGMMA Pipeline](../passes/gmma-pipeline.md) -- Phases 85/87, pipeline constraints, serialization
- [Ada & Hopper Targets](../targets/ada-hopper.md) -- SM 89/90 feature gates, WGMMA details
- [Turing & Ampere Targets](../targets/turing-ampere.md) -- SM 75--88 tensor core introduction
- [Latency Model](../scheduling/latency-model.md) -- HMMA/IMMA/DMMA functional unit scheduling
- [Register Model](../ir/registers.md) -- reg\_type 6 (tensor/accumulator, allocator class 6)
- [Mercury Encoder](../codegen/mercury.md) -- SASS encoding of MMA instructions
- [ELF Output](../output/sections.md) -- EIATTR_SPARSE_MMA_MASK, EIATTR_TCGEN05_*
