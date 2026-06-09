# Tensor Core / MMA Builtins

Tensor core builtins implement the Warp Matrix Multiply-Accumulate (WMMA) and Warp Group MMA (WGMMA) interfaces, spanning IDs 678–770 across four SM generations. Each generation added new data types and matrix shapes, resulting in 91 registered builtins that cover half-precision, integer, binary, double-precision, TF32, BF16, and FP8 matrix operations. SM 100 (Blackwell) adds a fifth generation — tcgen05 — documented in [Tensor / MMA Codegen](../llvm/mma-codegen.md).

## Key Facts

| Property | Value |
|---|---|
| Builtin IDs | 678–770 (93 entries) |
| WGMMA handler (IDs 753–768) | ~800 lines in `sub_12B3FD0` / `sub_955A70` |
| LLVM intrinsic range (WGMMA) | 5304–5447 (144-entry 5-D grid) plus 10654–10779 (N-dimension table) |
| NVVM lowering | `sub_955A70` (24 KB native), `sub_12B3FD0` (22 KB native) |
| Backend emission | `sub_21E74C0` (PTX builder), `sub_36E9630` (tcgen05 ISD selection) |
| SM gates | SM 70+ HMMA, SM 72+ IMMA, SM 75+ BMMA, SM 80+ DMMA/TF32/BF16, SM 90+ WGMMA |

## WMMA Architecture Evolution

| SM Generation | Feature | ID Range | Count |
|---|---|---|---|
| SM 70 (Volta) | HMMA: FP16 tensor core | 678–707 | 30 |
| SM 75 (Turing) | IMMA: INT8/INT4, BMMA: binary | 708–745 | 38 |
| SM 80 (Ampere) | DMMA: FP64, TF32, BF16 | 746–764 | 19 |
| SM 90 (Hopper) | WGMMA: warp-group MMA, FP8 | 765–768 | 4 |
| SM 100 (Blackwell) | tcgen05: MX formats, block-scale, sparsity | (intrinsic path) | — |

## HMMA — Half-Precision (IDs 678–707, SM 70+)

The original tensor core builtins provide 16-bit floating-point matrix multiply for three tile shapes. Each shape has 10 operations: load A, load B, load C (f16 and f32 accumulators), store C (f16 and f32), and four MMA variants for input/output precision combinations.

| ID Range | Shape | Builtin Prefix |
|---|---|---|
| 678–687 | 16x16x16 | `__hmma_m16n16k16_*` |
| 688–697 | 32x8x16 | `__hmma_m32n8k16_*` |
| 698–707 | 8x32x16 | `__hmma_m8n32k16_*` |

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

## IMMA — Integer MMA (IDs 708–739, SM 75+)

Integer tensor core operations for INT8 and INT4 data types.

### INT8 (IDs 708–731)

Three shapes (16x16x16, 32x8x16, 8x32x16), each with 8 operations:

| Suffix | Description |
|---|---|
| `ld_a_s8` / `ld_a_u8` | Load A fragment (signed/unsigned INT8) |
| `ld_b_s8` / `ld_b_u8` | Load B fragment (signed/unsigned INT8) |
| `ld_c` | Load accumulator (INT32) |
| `st_c_i32` | Store result (INT32) |
| `mma_s8` / `mma_u8` | INT8 MMA (signed/unsigned) |

### INT4 (IDs 732–739)

Single shape (8x8x32) with the same operation set but `_s4` / `_u4` type suffixes.

## BMMA — Binary MMA (IDs 740–745, SM 75+)

Binary (1-bit) matrix multiply with XOR-POPC and AND-POPC accumulation modes. Single shape: 8x8x128.

| ID | Builtin | Description |
|---|---|---|
| 740 | `__bmma_m8n8k128_ld_a_b1` | Load A fragment (binary) |
| 741 | `__bmma_m8n8k128_ld_b_b1` | Load B fragment (binary) |
| 742 | `__bmma_m8n8k128_ld_c` | Load accumulator |
| 743 | `__bmma_m8n8k128_st_c_i32` | Store result |
| 744 | `__bmma_m8n8k128_mma_xor_popc_b1` | Binary MMA (XOR + popcount) |
| 745 | `__bmma_m8n8k128_mma_and_popc_b1` | Binary MMA (AND + popcount) |

## Extended Tensor Core (IDs 746–764, SM 80+)

SM 80 (Ampere) added double-precision, TF32, and BF16 tensor operations.

### DMMA — Double Precision (IDs 746, 751–754)

| ID | Builtin | Description |
|---|---|---|
| 746 | `__dmma_m8n8k4_mma_f64` | FP64 MMA |
| 751 | `__dmma_m8n8k4_st_c_f64` | Store FP64 result |
| 752–754 | `__dmma_m8n8k4_{ld_a,ld_b,ld_c}` | Load fragments |

### TF32 (IDs 747, 755–757)

| ID | Builtin | Description |
|---|---|---|
| 747 | `__mma_tf32_m16n16k8_mma_f32` | TF32 MMA producing FP32 |
| 755–757 | `__mma_tf32_m16n16k8_{ld_a,ld_b,ld_c}` | Load fragments |

### BF16 (IDs 748–750, 758–764)

| ID | Builtin | Description |
|---|---|---|
| 748 | `__mma_bf16_m16n16k16_mma_f32` | BF16 16x16x16 MMA |
| 749 | `__mma_bf16_m32n8k16_mma_f32` | BF16 32x8x16 MMA |
| 750 | `__mma_bf16_m8n32k16_mma_f32` | BF16 8x32x16 MMA |
| 758–764 | `__mma_bf16_m*_{ld_a,ld_b}` | Load fragments for each shape |

## WMMA Lowering Details

### Three-Table Lookup

WMMA builtins use a three-table structure for mapping builtin IDs to LLVM intrinsic IDs:

| Table | Address (NVVM) | ID Range | Description |
|---|---|---|---|
| `dword_3F14840` | Entries 0–29 | 678–707 | HMMA (first-generation, FP16) |
| `dword_3F147E0` | Entries 0–23 | 708–731 | IMMA (INT8) |
| `dword_3F147A0` | Entries 0–12 | 732–744 | BMMA (binary) / INT4 |

The EDG-side parallel tables live at `dword_42810C0` (678–709), `dword_4281060` (708–731), `dword_4281020` (732–744), addressed from `sub_12AC1A0`.

### Fragment Size Determination

The number of register-level fragments varies by operation and data type:

| Condition | Fragment Count | Example |
|---|---|---|
| First-gen WMMA, BF16, store | 4 | BF16 store_c |
| First-gen WMMA, default | 8 | FP16 mma |
| IMMA, intrinsic 8914/8280 | 2 | INT8 ld_a compact |
| BMMA | 2 | Binary operations |
| IMMA intrinsic 0x22BB/0x22BC/0x22C5/0x22C6 | 4 | INT4 load A/B |
| IMMA intrinsic 0x22BD/0x22BE/0x22C3/0x22C4/0x22CB–0x22CE | 1 | Sub-byte single-element |
| IMMA intrinsic 0x22B7/0x22BF/0x22C7 | 8 | INT8 full-width |

### MMA Codegen Flow

The MMA handler (`sub_94E0D0` / `sub_12AC5F0`) processes 5 input operands:

1. **dest_ptr** — Pointer to output fragment storage
2. **A_fragment** — Matrix A input (loaded `v100` times)
3. **B_fragment** — Matrix B input (loaded `v95` times)
4. **C_fragment** — Accumulator input (loaded `v101` times)
5. **rowcol** — Layout operand (validated 0–3 for MMA)

An optional **satf** flag (saturation, validated 0–1) is consumed for most intrinsics except ID 8279.

The handler emits the MMA call via `sub_921880` and scatters results back to the destination fragment through `v103` iterations of element-wise stores.

**Fragment iteration counts per family** (NVVM path, `sub_94E0D0`):

| Family | v95 (load B) | v100 (load A) | v101 (load C) | v103 (store D) |
|---|---|---|---|---|
| BMMA (b1) | 1 | 1 | 2 | 2 |
| IMMA (0x22C0-0x22C1) | 1 | 4 | 8 | 8 |
| IMMA (0x22B8-0x22B9 = 8888-8889) | 2 | 2 | 8 | 8 |
| IMMA (0x22C8-0x22C9 = 8904-8905) | 4 | 1 | 8 | 8 |
| HMMA (default, first-gen) | 8 | 8 | varies | varies (4 or 8) |

The output fragment count is determined by bit-test: `(0x300C003 >> (intrinsic_id + 127)) & 1` selects 4 vs 8 fragments.

### Architecture Gating — Exact Thresholds

The architecture version is stored at `*(target_info + 252)` as a DWORD.

| Function | Gate Expression | Minimum SM | Notes |
|---|---|---|---|
| `sub_21DFBF0` hmmastc | `v8 > 0x45` | SM 70 | FP16 store |
| `sub_21E0360` hmmaldab | `v8 > 0x45` | SM 70 | FP16 load A/B |
| `sub_21E0870` hmmamma | `v8 > 0x45` | SM 70 | FP16 MMA |
| `sub_21E1280` immaldab | `v8 > 0x47` | SM 72 | INT load; `v8==72 && variant>1` rejected |
| `sub_21E1D20` immamma | `v8 > 0x47` | SM 72 | INT MMA; `variant>1 && v8==72` rejected |
| `sub_21E2280` bmmamma | `v8 > 0x48` | SM 73/75 | Binary MMA |
| `sub_36E9630` tcgen05 | `arch >= 0x3E8` | SM 100 | Blackwell only |

SM 72 (Xavier) has a unique partial IMMA implementation: only variant 0/1 shapes are supported, with explicit gating that blocks higher variants. This matches hardware reality where Xavier had limited INT8 tensor cores.

## WGMMA — Warp Group MMA (SM 90+ Hopper)

WGMMA operates on an entire warp group (4 warps, 128 threads) rather than a single warp. The system is split across four builtin IDs, 20 auxiliary IDs for fence/store/load operations, and two massive handler blocks totaling ~800 lines of lowering logic.

### Builtin Registration

Four builtins are registered in `sub_90AEE0` (NVVM) and `sub_126A910` (EDG):

| ID | Builtin | Data Type | Lowering Case |
|---|---|---|---|
| 765 (0x2FD) | `__wgmma_mma_async_f16` | FP16 | Full operand set (6 chained: A, B, C, scale, negate, sparsity) |
| 766 (0x2FE) | `__wgmma_mma_async_bf16` | BF16 | 2-operand (no scale/negate) |
| 767 (0x2FF) | `__wgmma_mma_async_tf32` | TF32 | Reduced operand set |
| 768 (0x300) | `__wgmma_mma_async_f8` | FP8 (SM 90a+) | Minimal (2 scale operands only) |

### WGMMA ID Space Overview

The full WGMMA ID range spans 745–770, subdivided into four functional groups:

| ID Range | Function | Handler |
|---|---|---|
| 745–750 (0x2E9–0x2EE) | Fence / commit / wait | `sub_12B1C20` / `sub_953BA0` |
| 751–752 (0x2EF–0x2F0) | Store | `sub_12B27B0` / `sub_954350` |
| 753–764 (0x2F1–0x2FC) | MMA async load (12 variants) | inline / `sub_9547E0` |
| 765–768 (0x2FD–0x300) | MMA async compute (4 type builtins) | inline ~800 lines / `sub_12B2E10` |
| 769–770 (0x301–0x302) | Warp-group barrier | inline IR via `sub_127FC40` |

### WGMMA Fence / Commit / Wait (IDs 745–750)

`sub_953BA0` (NVVM) / `sub_12B1C20` (EDG) builds a red-black tree on first call with 7 entries keyed by builtin ID. Each entry packs:

```c
struct wgmma_fence_entry {
    uint32_t id;           // builtin ID (745–751)
    uint32_t trans_a;      // transpose A flag
    uint32_t shape;        // shape code (0 or 1)
    uint32_t trans_b;      // transpose B flag
    uint32_t a_nregs;      // register count for A fragment
    uint32_t b_nregs;      // register count for B fragment
    uint32_t padding;      // unused alignment
    llvm_type *a_type;     // LLVM type for A (i64, i32, i16x2, i32x4)
    llvm_type *b_type;     // LLVM type for B
    llvm_type *c_type;     // LLVM type for C (i32x2, i32x8)
};
```

Decoded entries from local variables v47–v106:

| ID | trans_a | shape | trans_b | a_nregs | b_nregs | A type | B type | C type |
|---|---|---|---|---|---|---|---|---|
| 745 | 0 | 1 | 5 | 1 | 1 | i64 | i64 | — |
| 746 | 1 | 0 | 1 | 9 | 9 | i32 | i32 | i32x2 |
| 747 | 0 | 0 | 25 | 8 | 8 | i16x2 | i16x2 | — |
| 748 | 0 | 0 | 23 | 7 | 7 | i32x4 | i32x4 | i32x8 |
| 749 | 0 | 0 | 24 | 7 | 7 | i32x4 | i32x4 | i32x8 |
| 750 | 0 | 0 | 6 | 7 | 7 | i64 | i32x2 | i32x8 |

Output packed encoding (`*a4`, 64-bit):

| Bits | Field | Source |
|---|---|---|
| [3:0] | trans_a | `*(entry+40)` |
| [7:4] | shape | `*(entry+48) << 4` |
| [15:8] | a_nregs | `*(entry+64) << 8` |
| [27:16] | b_nregs | `*(entry+72) << 16` |
| [31:28] | padding | `*(entry+80) << 28` |
| [63:32] | trans_b | `*(entry+56) << 32` |
| [25] | rowcol bit 1 | `(rowcol & 2) == 0 ? 0x2000000 : 0x1000000` |
| [27:26] | rowcol bit 0 | `((rowcol & 1) + 1) << 26` |

The fence dispatch validates the `rowcol` operand (must be 0–3) and emits a 4-argument call to intrinsic 9062 (`llvm.nvvm.wgmma.fence.aligned`) with 3 type overloads. Fragment operands are prepared via `sub_94B510`.

### WGMMA Store (IDs 751–752)

`sub_954350` / `sub_12B27B0` builds a separate parameter lookup tree. Store operations validate `rowcol` (0 or 1) and emit a 5-argument call using intrinsic 9145 (`llvm.nvvm.wgmma.store`) with 2 type overloads. Operands: `{constant, B_fragment, descriptor, rowcol, zero}`.

### WGMMA MMA Async Load (IDs 753–764)

`sub_9547E0` (NVVM) / `sub_12B2E10` (EDG) builds a 12-entry red-black tree at `ctx+656`:

| ID | Shape | nregs | Variant | Fragment Type |
|---|---|---|---|---|
| 753 | 1 | 9 | 0 | — |
| 754 | 1 | 9 | 1 | — |
| 755 | 1 | 9 | 2 | i16x2 |
| 756 | 25 | 8 | 0 | — |
| 757 | 25 | 8 | 1 | — |
| 758 | 25 | 10 | 2 | i32x8 |
| 759 | 23 | 7 | 0 | i32x4 |
| 760 | 23 | 7 | 1 | i32x4 |
| 761 | 24 | 7 | 0 | i32x4 |
| 762 | 24 | 7 | 1 | i32x4 |
| 763 | 6 | 7 | 0 | i32x2/i64 |
| 764 | 6 | 7 | 1 | i32x2/i64 |

Output packed encoding (`*a4`, 64-bit):

| Bits | Field |
|---|---|
| [63:32] | `*(entry+40) << 32` |
| [31:4] | `*(entry+48) << 4 \| rowcol` |
| [1] | `*(entry+56) << 1` |

Emits intrinsic 9067 (`llvm.nvvm.wgmma.mma.async`) with 2 type overloads. Arguments: `{constant, B_fragment, rowcol_value, zero_constant}`. Results scattered via `sub_94B940`.

### WGMMA MMA Async Compute — The 800-Line Handler (IDs 765–768)

This is the primary WGMMA lowering path. It lives inline in the mega-switch of `sub_955A70` (NVVM, lines ~2850–3138) and `sub_12B3FD0` (EDG, lines ~2270–3138). The handler implements two completely different intrinsic selection strategies depending on which builtin ID triggered entry.

#### Argument Extraction

The handler walks the argument chain 7 levels deep from the call expression:

```text
v263 = M dimension              (first constant argument)
v512 = accumulator fragments    (pointer to fragment array)
v528 = A descriptor             (64-bit matrix descriptor or register fragments)
v524 = B descriptor             (64-bit matrix descriptor)
v519 = scale factors            (A and D scale constants)
v264 = layout params            (rowcol encoding)
v516, v265 = shape params       (additional dimension info)
v540 = element type info        (integer type tag from AST)
```

Each constant argument is validated through `sub_620FD0` (EDG) / `sub_620FD0` (shared), which extracts the integer value and sets an overflow flag. On overflow:

```text
"unexpected constant overflow in __wgmma_mma_async operand"
```

This check is applied 5 times: once for N dimension, once for each scale factor, and once for each negate/saturation bit.

#### Per-Builtin Argument Layouts

| ID | Builtin | Operand Chain |
|---|---|---|
| 765 (0x2FD) | `_f16` | 6 chained: A, B, C, scaleA, scaleD, negate/saturation |
| 766 (0x2FE) | `_bf16` | Separate branch (LABEL_56 path), 2-operand (no scale/negate) |
| 767 (0x2FF) | `_tf32` | Rearranged arguments, fewer config bits |
| 768 (0x300) | `_f8` | Simplest form, 2 matrix descriptors + config |

#### Strategy 1: N-Dimension Dispatch (IDs 765–768, inner path)

When the element type is checked and the first argument yields an N dimension, the handler enters a 33-entry switch mapping N values to LLVM intrinsic IDs in the range 10654–10779:

| N | Integer-type Intrinsic | Float-type Intrinsic |
|---|---|---|
| 8 | 10774 | 10775 |
| 16 | 10690 | 10691 |
| 24 | 10734 | 10735 |
| 32 | 10742 | 10743 |
| 40 | 10746 | 10747 |
| 48 | 10750 | 10751 |
| 56 | 10754 | 10755 |
| 64 | 10758 | 10759 |
| 72 | 10762 | 10763 |
| 80 | 10766 | 10767 |
| 88 | 10770 | 10771 |
| 96 | 10778 | 10779 |
| 104 | 10654 | 10655 |
| 112 | 10658 | 10659 |
| 120 | 10662 | 10663 |
| 128 | 10666 | 10667 |
| 136 | 10670 | 10671 |
| 144 | 10674 | 10675 |
| 152 | 10678 | 10679 |
| 160 | 10682 | 10683 |
| 168 | 10686 | 10687 |
| 176 | 10694 | 10695 |
| 184 | 10698 | 10699 |
| 192 | 10702 | 10703 |
| 200 | 10706 | 10707 |
| 208 | 10710 | 10711 |
| 216 | 10714 | 10715 |
| 224 | 10718 | 10719 |
| 232 | 10722 | 10723 |
| 240 | 10726 | 10727 |
| 248 | 10730 | 10731 |
| 256 | 10738 | 10739 |

The even/odd intrinsic ID pairing encodes the distinction between integer-element and float-element variants. Type discrimination uses the AST element type: if the element type is integer with width 10 (i.e., a 10-bit integer signaling bf16/tf32 internal encoding), the even (integer) intrinsic is selected; otherwise the odd (float) intrinsic.

**N dimension validation:**

```c
if ((N & (N - 1)) != 0)
    error("N only supported for powers of two");
```

This is applied when the N value does not match any case in the 33-entry switch. The N values 8, 16, 32, 64, 128, 256 are powers of two; the intermediate values (24, 40, 48, ..., 248) are non-power-of-two multiples of 8 that are still valid WGMMA dimensions.

#### Strategy 2: 5-Dimensional Intrinsic Grid (IDs 753–764 path, shared)

For the full WGMMA async variants (handled through `sub_12B2E10`), the handler selects from a 144-entry intrinsic table spanning IDs 5304–5447, organized as a 5-dimensional grid:

| Dimension | Values | Description |
|---|---|---|
| 1. N | {16, 32, 64, 128} | Output column dimension |
| 2. B_shared | {false, true} | Is B operand from shared memory? (`sub_12A71A0 != 0`) |
| 3. is_s64 | {false, true} | Is accumulator type s64/int? (type tag 2, subtype 10) |
| 4. scale/negate | varies | A scale nonzero? D scale nonzero? |
| 5. variant | {0x2FD, 0x2FE, 0x2FF, 0x300} | Which builtin triggered entry |

Base addresses and stride:

| N | Base ID | Stride per N |
|---|---|---|
| 128 | 5304 | 24 variants |
| 64 | ~5328 | 24 |
| 32 | ~5352 | 24 |
| 16 | ~5376 | 24 |
| overflow | ~5400–5447 | remaining |

Size-based opcode selection (for f16, ID 765):

| Accumulator Size | Opcode (integer) | Opcode (float) |
|---|---|---|
| 16 | 5332 | 5333 |
| 32 | 5380 | 5381 |
| 64 | 5404 | 5405 |
| 128 | 5308 | 5309 |
| other | 5356/5428 | 5357/5429 |

The mapping formula: `base + N_offset + shared_offset + type_offset + variant_offset`. The accumulator size is extracted by `sub_12A71A0(expr)` from the expression type chain.

### WGMMA Config Bit Packing

Multiple boolean arguments are packed into a single configuration word passed to the final intrinsic call:

| Bit | Field | Source | Value Semantics |
|---|---|---|---|
| 0 | Accumulate / saturation flag | Final constant operand (`v433`) | 1 = accumulate into D, 0 = overwrite |
| 1 | ScaleD / transpose flag | `v445` constant | 1 = transpose B descriptor |
| 2 | Negate-C / layout flag | `v81` / `v433` constant | 1 = negate accumulator input |
| 3 | Sign bit for B | `v427` constant (if present) | Reserved / sign extension |
| 4 | Negate-A / additional mode | `v80` / `v427` constant (if present) | 1 = negate A operand |

Combined via: `v79 = bit0 | (bit1 << 1) | (bit2 << 2) | (bit4 << 4)`.

After intrinsic selection, the handler:
1. Converts the accumulator pointer to a vector pointer (`.asvecptr` tag)
2. Extracts bitfield from constant operands for mode flags
3. Calls `sub_1285290` / `sub_921880` with name hint `"mmafrag"`
4. Scatters results via `sub_94B940` / `sub_1280F50` (size 4 = float elements)

### WGMMA Validation Summary

All constant arguments pass through `sub_620FD0`, which extracts the integer value and sets an overflow flag.

| Check | Error Message | Condition |
|---|---|---|
| Constant overflow | `"unexpected constant overflow in __wgmma_mma_async operand"` | Any integer operand overflows extraction (5 occurrences) |
| N power-of-two | `"N only supported for powers of two"` | `(N & (N - 1)) != 0` and N not in the 33-entry switch |
| rowcol range (fence) | `"'rowcol' operand can be 0 or 1 only"` | rowcol > 1 for load/store |
| rowcol range (MMA) | (implicit — validated 0–3) | rowcol > 3 for MMA operations |

## WGMMA Support Functions

| Function | Address | EDG Parallel | Purpose |
|---|---|---|---|
| `sub_953BA0` | 0x953BA0 | `sub_12B1C20` | Fence/commit/wait parameter lookup, builds packed 64-bit encoding |
| `sub_9547E0` | 0x9547E0 | `sub_12B2E10` | MMA async load parameter lookup, 12-entry red-black tree |
| `sub_954350` | 0x954350 | `sub_12B27B0` | Store variant parameter lookup |
| `sub_94B510` | 0x94B510 | — | Prepare fragment operand for WGMMA call |
| `sub_94B940` | 0x94B940 | `sub_1280F50` | Scatter MMA results back to fragment outputs |
| `sub_94B2B0` | 0x94B2B0 | — | Extract fragment element at index (WMMA shared) |
| `sub_12A71A0` | 0x12A71A0 | — | Extract size/dimension from expression type (EDG-only) |
| `sub_12A6F10` | 0x12A6F10 | — | Validate constant integer in range (EDG-only) |
| `sub_620FD0` | 0x620FD0 | — | Extract constant integer with overflow detection (shared) |

## Packed MMA Descriptor Word

The MMA PTX string builder at `sub_21E74C0` (AsmPrinter) / `sub_35F_range` (NVPTX backend) reads a packed 64-bit descriptor for all MMA instruction emission. The descriptor is stored at:

```c
v22 = *(QWORD *)(*(QWORD *)(a1 + 16) + 16 * a2 + 8)
```

| Bits | Field | Query Key | Values |
|---|---|---|---|
| [0] | Row/col layout | `"rowcol"` | 0=row, 1=col |
| [2:1] | Matrix ID | `"mid"` | 0=a, 1=b, 2=c, 3=d |
| [7:4] | Binary opcode | `"opc"` | 0=default, 1=`.and.popc`, 2=`.xor.popc` |
| [2:0] | Rounding mode | `"rnd"` | 0=none, 1=`.rn`, 2=`.rm`, 3=`.rp`, 4=`.rz` |
| [15:8] | A element type | `"aty"` | Type enum 1–11 |
| [23:16] | B element type | `"bty"` | Type enum 1–11 |
| [25:24] | A layout | `"al"` | 0=row, nonzero=col |
| [27:26] | B layout | `"bl"` | 0=row, nonzero=col |
| [28] | Saturation | `"satf"` | 1=`.satfinite` |
| [39:32] | Shape enum | `"shape"` | 0x01–0x19, 18 entries |

### Shape Enum

| Enum | Shape | PTX String | Min SM | Notes |
|---|---|---|---|---|
| 0x01 | m8n8k4 | `"m8n8k4"` | SM 70 | Original Volta HMMA |
| 0x02 | m8n8k16 | `"m8n8k16"` | SM 72 | Integer MMA (s8/u8) |
| 0x03 | m8n8k32 | `"m8n8k32"` | SM 75 | Sub-byte (s4/u4) |
| 0x04 | m8n8k64 | `"m8n8k64"` | SM 75 | Extended sub-byte |
| 0x05 | m8n8k128 | `"m8n8k128"` | SM 75 | Binary MMA (b1) |
| 0x06 | m8n32k16 | `"m8n32k16"` | SM 70 | Volta/Turing WMMA f16/bf16; 30+ matching builtins in `cicc_strings.json` |
| 0x10 | m16n8k4 | `"m16n8k4"` | SM 75 | Turing HMMA, f64 on Ampere |
| 0x11 | m16n8k8 | `"m16n8k8"` | SM 75 | Turing/Ampere HMMA |
| 0x12 | m16n8k16 | `"m16n8k16"` | SM 80 | Ampere HMMA (bf16, tf32) |
| 0x13 | m16n8k32 | `"m16n8k32"` | SM 75 | Ampere integer |
| 0x14 | m16n8k64 | `"m16n8k64"` | SM 75 | Sub-byte integer |
| 0x15 | m16n8k128 | `"m16n8k128"` | SM 75 | Extended sub-byte |
| 0x16 | m16n8k256 | `"m16n8k256"` | SM 75 | Binary/sub-byte (largest) |
| 0x17 | m16n16k16 | `"m16n16k16"` | SM 90 | Square shape, Hopper+ |
| 0x18 | m32n8k16 | `"m32n8k16"` | SM 80 | Tall shape |
| 0x19 | m16n16k8 | `"m16n16k8"` | SM 70 | WMMA f16 path |

Unknown shape codes hit the default branch and abort via `BUG()`. String emission uses fast-path integer stores: `*(QWORD *)ptr = 0x36316B386E36316DLL` emits `"m16n8k16"` as a single 8-byte write.

### Type Enum

| Enum | Type | Bits | PTX String |
|---|---|---|---|
| 1 | b1 | 1 | `"b1"` |
| 2 | s4 | 4 | `"s4"` |
| 3 | u4 | 4 | `"u4"` |
| 4 | s8 | 8 | `"s8"` |
| 5 | u8 | 8 | `"u8"` |
| 6 | f16 | 16 | `"f16"` |
| 7 | bf16 | 16 | `"bf16"` |
| 8 | tf32 | 19 | `"tf32"` |
| 9 | f64 | 64 | `"f64"` |
| 10 | f32 | 32 | `"f32"` |
| 11 | s32 | 32 | `"s32"` |

Any other type code produces fatal error: `"Wrong MMA element type"`.

## Shape x Type x Architecture Summary

| Shape | A/B Types | Acc Types | Min SM | Notes |
|---|---|---|---|---|
| m8n8k4 | f16 | f16, f32 | SM 70 | Original Volta |
| m16n8k4 | f64 | f64 | SM 80 | Ampere f64 |
| m16n8k8 | f16 | f16, f32 | SM 75 | Turing+ |
| m16n8k16 | f16, bf16, tf32 | f16, f32 | SM 80 | Ampere+ |
| m16n16k8 | f16 | f16, f32 | SM 70 | WMMA path |
| m16n16k16 | f16, bf16 | f16, f32 | SM 90 | Hopper+ |
| m32n8k16 | f16, bf16 | f16, f32 | SM 80 | Tall shape |
| m8n8k16 | s8, u8 | s32 | SM 72 | Integer MMA |
| m16n8k16 | s8, u8 | s32 | SM 75 | Turing+ |
| m16n8k32 | s8, u8 | s32 | SM 75 | Turing+ |
| m8n8k32 | s4, u4 | s32 | SM 75 | Sub-byte |
| m16n8k64 | s4, u4 | s32 | SM 75 | Sub-byte |
| m8n8k64 | s4, u4 | s32 | SM 75 | Extended sub-byte |
| m16n8k128 | s4, u4 | s32 | SM 75 | Extended sub-byte |
| m8n8k128 | b1 | s32 | SM 75 | Binary (`.and.popc`, `.xor.popc`) |
| m16n8k256 | b1 | s32 | SM 75 | Binary extended |
| WGMMA (N=8..256) | f16, bf16, tf32, f8 | f16, f32 | SM 90 | Warp-group, 33 N values |
| tcgen05 (10 variants) | mxf8f6f4, mxf4, mxf4nvf4, f16, bf16, tf32, i8, fp4 | varies | SM 100 | See [mma-codegen](../llvm/mma-codegen.md) |

## tcgen05 Blackwell Overview (SM 100+)

Full tcgen05 documentation lives in [Tensor / MMA Codegen](../llvm/mma-codegen.md). Key points summarized here for cross-reference:

**Data type kinds** (bits [8:6] of the tcgen05 operand, emitted by `sub_35F3330`):

| Value | Kind | Notes |
|---|---|---|
| 0 | `mxf4nvf4` | MX FP4 with NV FP4 |
| 1 | `f8f6f4` | FP8/FP6/FP4 standard |
| 2 | `mxf8f6f4` | MX variant of f8f6f4 |
| 3 | `f16` | Half precision |
| 4 | `i8` | 8-bit integer (arch-conditional only) |
| 5 | `tf32` | TensorFloat-32 |
| 7 | `mxf4` | MX FP4 |

**Modifier fields:**

| Modifier | Bits | Description |
|---|---|---|
| Weight stationary (`.ws`) | bit 0 | NOT compatible with `cta_group::2`, `mxf8f6f4`, `fp4` |
| CTA group | bit 1 | `cta_group::1` (clear) or `cta_group::2` (set) |
| Scale vector size | [3:2] | `.scale_vec::1X`/`2X`/`4X` with per-type constraints |
| Scale input accumulator | bit 4 | f16/tf32 only; NOT on sm_100a/sm_103a |
| Sparsity | bit 5 | MXF4/MXF4NVF4 restricted to arch-conditional |
| Block scale alias | [10:9] | `.block16` (0) or `.block32` (1) |

**Collector modes** (emitted by `sub_35F38B0`):

| Value | Modifier | Constraint |
|---|---|---|
| 1 | `.collector::a::lastuse` | — |
| 2 | `.collector::a::fill` | Cannot combine with `.ashift` |
| 3 | `.collector::a::use` | Cannot combine with `.ashift` |

**tcgen05 scaled MMA operand builder** (`sub_21E8CD0` / `sub_35F3E90`):

| Bit | Query | Clear | Set |
|---|---|---|---|
| 0 | `"scaleD"` | `"0"` | `"1"` |
| 1 | `"negA"` | `"1"` (no negate) | `"-1"` (negate) |
| 2 | `"negB"` | `"1"` | `"-1"` |
| 3 | `"transA"` | `"0"` | `"1"` |
| 4 | `"transB"` | `"0"` | `"1"` |

Note the asymmetry: `scaleD`/`transA`/`transB` emit boolean `"0"`/`"1"` strings, while `negA`/`negB` emit sign multiplier `"1"`/`"-1"` strings. This reflects the PTX encoding where negation is a multiplication factor and transpose is a boolean flag.

## LLVM Intrinsic Reference

| Intrinsic ID | Name | Usage |
|---|---|---|
| 9062 | `llvm.nvvm.wgmma.fence.aligned` | WGMMA fence (3 type overloads) |
| 9067 | `llvm.nvvm.wgmma.mma.async` | WGMMA MMA async load (2 type overloads) |
| 9145 | `llvm.nvvm.wgmma.store` | WGMMA store (2 type overloads) |
| 10654–10779 | `llvm.nvvm.wgmma.mma.async.*` | Per-N-dimension variants (126 entries, even=int, odd=float) |
| 5304–5447 | (WGMMA 5-D grid) | Per-N x shared x type x scale x variant (144 entries) |
| 4905–4940 | (tcgen05 ISD opcodes) | tcgen05.mma variants (36 opcodes via 10-way shape switch) |

## NVPTX Backend Duplicate Functions

All MMA emission functions exist in two structurally identical copies:

| AsmPrinter (0x21Dxxxx) | NVPTX Backend (0x36Exxxx) | Function |
|---|---|---|
| `sub_21DFBF0` | `sub_36E91F0` | hmmastc (HMMA store C) |
| `sub_21E0360` | `sub_36E72A0` | hmmaldab (HMMA load A/B) |
| `sub_21E0630` | `sub_36E7580` | hmmaldc (HMMA load C) |
| `sub_21E0870` | `sub_36E77C0` | hmmamma (HMMA MMA) |
| `sub_21E1280` | `sub_36E7B50` | immaldab (IMMA load A/B) |
| `sub_21E15D0` | `sub_36E7EA0` | immaldc (IMMA load C) |
| `sub_21E1830` | `sub_36E8110` | immastc (IMMA store C) |
| `sub_21E1D20` | `sub_36E8630` | immamma (IMMA MMA) |
| `sub_21E2280` | `sub_36E8BD0` | bmmamma (Binary MMA) |
| `sub_21E8CD0` | `sub_35F3E90` | tcgen05 scaled MMA operands |

The pairs differ only in error reporting (`sub_16BD130` vs `sub_C64ED0`) and reference counting functions (`sub_1623A60`/`sub_161E7C0` vs `sub_B96E90`/`sub_B91220`).

## Cross-References

- [Tensor / MMA Codegen](../llvm/mma-codegen.md) — backend PTX emission, tcgen05 full detail
- [NVPTX Opcodes](../reference/nvptx-opcodes.md) — ISD opcode numbers
- [SM 90 (Hopper)](../targets/sm90-hopper.md) — WGMMA architecture context, TMA, cluster
- [SM 100 (Blackwell)](../targets/sm100-blackwell.md) — tcgen05 architecture context
- [Builtin System](./index.md) — hash table, registration, dispatch architecture
