# Tensor Core / MMA Builtins

Tensor core builtins implement the Warp Matrix Multiply-Accumulate (WMMA) and Warp Group MMA (WGMMA) interfaces, spanning IDs 678--768 across four SM generations. Each generation added new data types and matrix shapes, resulting in 91 registered builtins that cover half-precision, integer, binary, double-precision, TF32, BF16, and FP8 matrix operations.

## WMMA Architecture Evolution

| SM Generation | Feature | ID Range | Count |
|---|---|---|---|
| SM 70 (Volta) | HMMA: FP16 tensor core | 678--707 | 30 |
| SM 75 (Turing) | IMMA: INT8/INT4, BMMA: binary | 708--745 | 38 |
| SM 80 (Ampere) | DMMA: FP64, TF32, BF16 | 746--764 | 19 |
| SM 90 (Hopper) | WGMMA: warp-group MMA, FP8 | 765--768 | 4 |

## HMMA -- Half-Precision (IDs 678--707, SM 70+)

The original tensor core builtins provide 16-bit floating-point matrix multiply for three tile shapes. Each shape has 10 operations: load A, load B, load C (f16 and f32 accumulators), store C (f16 and f32), and four MMA variants for input/output precision combinations.

| ID Range | Shape | Builtin Prefix |
|---|---|---|
| 678--687 | 16x16x16 | `__hmma_m16n16k16_*` |
| 688--697 | 32x8x16 | `__hmma_m32n8k16_*` |
| 698--707 | 8x32x16 | `__hmma_m8n32k16_*` |

Per-shape operations (10 each):

| Suffix | Operation | Description |
|---|---|---|
| `ld_a` | Load A fragment | Load matrix A tile from memory |
| `ld_b` | Load B fragment | Load matrix B tile from memory |
| `ld_c_f16` | Load C (f16) | Load accumulator as half-precision |
| `ld_c_f32` | Load C (f32) | Load accumulator as single-precision |
| `st_c_f16` | Store C (f16) | Store result as half-precision |
| `st_c_f32` | Store C (f32) | Store result as single-precision |
| `mma_f16f16` | MMA f16->f16 | FP16 input, FP16 accumulator |
| `mma_f32f16` | MMA f16->f32 | FP16 input, FP32 accumulator |
| `mma_f16f32` | MMA f32->f16 | FP32 accumulator, FP16 output |
| `mma_f32f32` | MMA f32->f32 | FP32 input and accumulator |

## IMMA -- Integer MMA (IDs 708--739, SM 75+)

Integer tensor core operations for INT8 and INT4 data types.

### INT8 (IDs 708--731)

Three shapes (16x16x16, 32x8x16, 8x32x16), each with 8 operations:

| Suffix | Description |
|---|---|
| `ld_a_s8` / `ld_a_u8` | Load A fragment (signed/unsigned INT8) |
| `ld_b_s8` / `ld_b_u8` | Load B fragment (signed/unsigned INT8) |
| `ld_c` | Load accumulator (INT32) |
| `st_c_i32` | Store result (INT32) |
| `mma_s8` / `mma_u8` | INT8 MMA (signed/unsigned) |

### INT4 (IDs 732--739)

Single shape (8x8x32) with the same operation set but `_s4` / `_u4` type suffixes.

## BMMA -- Binary MMA (IDs 740--745, SM 75+)

Binary (1-bit) matrix multiply with XOR-POPC and AND-POPC accumulation modes. Single shape: 8x8x128.

| ID | Builtin | Description |
|---|---|---|
| 740 | `__bmma_m8n8k128_ld_a_b1` | Load A fragment (binary) |
| 741 | `__bmma_m8n8k128_ld_b_b1` | Load B fragment (binary) |
| 742 | `__bmma_m8n8k128_ld_c` | Load accumulator |
| 743 | `__bmma_m8n8k128_st_c_i32` | Store result |
| 744 | `__bmma_m8n8k128_mma_xor_popc_b1` | Binary MMA (XOR + popcount) |
| 745 | `__bmma_m8n8k128_mma_and_popc_b1` | Binary MMA (AND + popcount) |

## Extended Tensor Core (IDs 746--764, SM 80+)

SM 80 (Ampere) added double-precision, TF32, and BF16 tensor operations.

### DMMA -- Double Precision (IDs 746, 751--754)

| ID | Builtin | Description |
|---|---|---|
| 746 | `__dmma_m8n8k4_mma_f64` | FP64 MMA |
| 751 | `__dmma_m8n8k4_st_c_f64` | Store FP64 result |
| 752--754 | `__dmma_m8n8k4_{ld_a,ld_b,ld_c}` | Load fragments |

### TF32 (IDs 747, 755--757)

| ID | Builtin | Description |
|---|---|---|
| 747 | `__mma_tf32_m16n16k8_mma_f32` | TF32 MMA producing FP32 |
| 755--757 | `__mma_tf32_m16n16k8_{ld_a,ld_b,ld_c}` | Load fragments |

### BF16 (IDs 748--750, 758--764)

| ID | Builtin | Description |
|---|---|---|
| 748 | `__mma_bf16_m16n16k16_mma_f32` | BF16 16x16x16 MMA |
| 749 | `__mma_bf16_m32n8k16_mma_f32` | BF16 32x8x16 MMA |
| 750 | `__mma_bf16_m8n32k16_mma_f32` | BF16 8x32x16 MMA |
| 758--764 | `__mma_bf16_m*_{ld_a,ld_b}` | Load fragments for each shape |

## WGMMA -- Warp Group MMA (IDs 765--768, SM 90+ Hopper)

WGMMA operates on an entire warp group (4 warps, 128 threads) rather than a single warp. Only four builtin IDs are registered, but they expand to over 150 LLVM intrinsic variants through compile-time dimension and type dispatch.

| ID | Builtin | Data Type |
|---|---|---|
| 765 | `__wgmma_mma_async_f16` | FP16 |
| 766 | `__wgmma_mma_async_bf16` | BF16 |
| 767 | `__wgmma_mma_async_tf32` | TF32 |
| 768 | `__wgmma_mma_async_f8` | FP8 (SM 90a+) |

### WGMMA Dimension Dispatch

The N dimension is extracted from the first constant argument and must be a power of two. Each dimension maps to a unique LLVM intrinsic ID in the range 10654--10779 (a dense table of ~126 entries):

| N Dimension | Integer Type Intrinsic | Float Type Intrinsic |
|---|---|---|
| 8 | 10774 | 10775 |
| 16 | 10690 | 10691 |
| 32 | 10742 | 10743 |
| 64 | 10758 | 10759 |
| 128 | 10666 | 10667 |
| 256 | 10738 | 10739 |

The even/odd intrinsic ID pairing encodes the distinction between integer-element and float-element variants.

### WGMMA Config Bit Packing

Multiple boolean arguments are packed into a single configuration word:

| Bit | Field | Source |
|---|---|---|
| 0 | Saturation flag | Final constant operand |
| 1 | ScaleD flag | `v445` constant |
| 2 | Layout flag | `v81` constant |
| 3 | Sign bit for B | `v427` constant (if present) |
| 4 | Additional mode | `v80` constant (if present) |

### WGMMA Validation

All constant arguments pass through `sub_620FD0`, which extracts the integer value and sets an overflow flag. If overflow is detected, the compiler emits:

```
"unexpected constant overflow in __wgmma_mma_async operand"
```

The N dimension is validated: `(N & (N - 1)) != 0` triggers:

```
"N only supported for powers of two"
```

## WMMA Lowering Details

### Three-Table Lookup

WMMA builtins use a three-table structure for mapping builtin IDs to LLVM intrinsic IDs:

| Table | Address (NVVM) | ID Range | Description |
|---|---|---|---|
| `dword_3F14840` | Entries 0--29 | 678--707 | HMMA (first-generation, FP16) |
| `dword_3F147E0` | Entries 0--23 | 708--731 | IMMA (INT8) |
| `dword_3F147A0` | Entries 0--12 | 732--744 | BMMA (binary) / INT4 |

### Fragment Size Determination

The number of register-level fragments varies by operation and data type:

| Condition | Fragment Count | Example |
|---|---|---|
| First-gen WMMA, BF16, store | 4 | BF16 store_c |
| First-gen WMMA, default | 8 | FP16 mma |
| IMMA, intrinsic 8914/8280 | 2 | INT8 ld_a compact |
| BMMA | 2 | Binary operations |

### MMA Codegen Flow

The MMA handler (`sub_94E0D0` / `sub_12AC5F0`) processes 5 input operands:

1. **dest_ptr** -- Pointer to output fragment storage
2. **A_fragment** -- Matrix A input (loaded `v100` times)
3. **B_fragment** -- Matrix B input (loaded `v95` times)
4. **C_fragment** -- Accumulator input (loaded `v101` times)
5. **rowcol** -- Layout operand (validated 0--3 for MMA)

An optional **satf** flag (saturation, validated 0--1) is consumed for most intrinsics except ID 8279.

The handler emits the MMA call via `sub_921880` and scatters results back to the destination fragment through `v103` iterations of element-wise stores.

### WGMMA Support Functions

| Function | Purpose |
|---|---|
| `sub_953BA0` | WGMMA parameter lookup (fence_aligned), builds packed 64-bit encoding |
| `sub_9547E0` | WGMMA MMA async load parameter lookup, 12-entry red-black tree |
| `sub_954350` | WGMMA store variant parameter lookup |
| `sub_94B510` | Prepare fragment operand for WGMMA call |
| `sub_94B940` | Scatter MMA results back to fragment outputs |

The WGMMA fence/commit/wait operations (IDs 745--750 mapped via `sub_12B1C20`) validate the `rowcol` operand (must be 0--3) and emit 4-argument calls to intrinsic 9062 (`llvm.nvvm.wgmma.fence.aligned`) with 3 type overloads.
