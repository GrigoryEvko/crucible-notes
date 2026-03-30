# Tensor Core / MMA Code Generation

> **NVIDIA-modified pass.** See [Differences from Upstream](#differences-from-upstream-llvm) for GPU-specific changes.

CICC v13.0 contains a complete tensor core code generation pipeline spanning five SM generations (Volta through Blackwell), three distinct MMA instruction families (HMMA/IMMA/BMMA), the SM 90 Warp Group MMA (WGMMA) system, and the SM 100 Tensor Core Generation 5 (tcgen05) engine. The pipeline transforms NVVM intrinsic calls through two parallel lowering paths -- one in the NVVM IR lowering layer (`sub_955A70`) and one in the SelectionDAG backend (`sub_33B0210`) -- before reaching a common PTX instruction emission layer that constructs MMA instructions from packed 64-bit descriptors encoding shape, type, layout, rounding, and saturation.

This page documents the code generation mechanics: how MMA operations flow from source-level `__hmma_*` / `__wmma_*` / `__wgmma_*` builtins through LLVM intrinsic selection, SelectionDAG lowering, and PTX string emission. For the builtin-to-intrinsic mapping and per-ID reference, see [Tensor / MMA Builtins](../builtins/tensor-mma.md). For the SelectionDAG infrastructure that hosts this lowering, see [SelectionDAG](selectiondag.md).

| | |
|---|---|
| **NVVM builtin dispatch** | `sub_955A70` (105KB) -- main NVVM builtin lowering dispatcher |
| **SelectionDAG intrinsic switch** | `sub_33B0210` (343KB, 9,518 lines) -- intrinsic lowering mega-switch, CAT-17 |
| **SelectionDAG MMA handler** | `sub_33A64B0` -- WMMA/MMA DAG node construction (95 intrinsic IDs) |
| **WMMA load handler** | `sub_94CAB0` / `sub_94DCB0` -- fragment load codegen |
| **WMMA MMA handler** | `sub_94E0D0` -- matrix multiply-accumulate codegen |
| **MMA PTX string builder** | `sub_21E74C0` (AsmPrinter) / `sub_35F3E90` (backend) |
| **tcgen05.mma lowering** | `sub_304E6C0` (SelectionDAG) / `sub_36E9630` (instruction emission) |
| **tcgen05 infrastructure** | `sub_30462A0` -- fence/wait/alloc/dealloc/cp/commit |
| **Address range** | `0x21D0000`--`0x21F0000` (AsmPrinter MMA), `0x304xxxx`--`0x36Fxxxx` (backend) |
| **Upstream** | `lib/Target/NVPTX/NVPTXISelLowering.cpp` (no upstream MMA; entirely NVIDIA-proprietary) |

## Pipeline Overview

MMA code generation follows a three-stage pipeline. The first two stages exist in parallel copies; the third is shared.

```
CUDA source:  __hmma_m16n16k16_mma_f32f32(d, a, b, c, 0)
                │
    ┌───────────┴───────────┐
    │ NVVM builtin lowering │ SelectionDAG intrinsic lowering
    │ (sub_955A70)          │ (sub_33B0210, CAT-17)
    │                       │
    │ 3-table lookup:       │ sub_33A64B0 -> SDNode construction
    │ dword_3F14840/7E0/7A0 │ 95 case labels (0xA4-0xA8, 0x194-0x1EC)
    │                       │
    │ sub_94E0D0 (MMA)      │
    │ sub_94CAB0 (load)     │
    │ sub_9493D0 (store)    │
    └───────────┬───────────┘
                │
    ┌───────────┴───────────┐
    │ PTX Instruction Emit  │
    │ sub_21E74C0 (printer) │
    │ sub_1D23DE0 (emitter) │
    └───────────────────────┘
```

The NVVM builtin lowering path handles builtins that arrive as direct function calls from the EDG frontend. The SelectionDAG path handles the same operations when they arrive as LLVM intrinsic calls (the normal path when CUDA C++ compiles through Clang-style IR generation). Both paths converge at the PTX string builder, which reads a packed 64-bit descriptor word and emits text like `mma.sync.aligned.m16n8k16.row.col.f32.bf16.bf16.f32`.

## Packed MMA Descriptor

All MMA operations are encoded as a single 64-bit descriptor word stored at `*(QWORD*)(*(QWORD*)(a1+16) + 16*a2 + 8)`. The PTX string builder (`sub_21E74C0`) queries this descriptor through a string-keyed interface. The caller passes a query string (e.g., `"shape"`, `"ety"`, `"mid"`), and the builder extracts the relevant bits and emits the corresponding PTX text.

### Bit Layout

```
Bits     Field       Query key   Values
───────  ──────────  ─────────   ──────
[0]      rowcol      "rowcol"    0=row, 1=col
[2:1]    mid         "mid"       0=a, 1=b, 2=c, 3=d
[7:4]    opc         "opc"       0=default, 1=.and.popc, 2=.xor.popc
[2:0]    rnd         "rnd"       0=none, 1=.rn, 2=.rm, 3=.rp, 4=.rz
[15:8]   aty         "aty"       A element type enum (see below)
[23:16]  bty         "bty"       B element type enum
[25:24]  al          "al"        A layout: 0=row, nonzero=col
[27:26]  bl          "bl"        B layout: 0=row, nonzero=col
[28]     satf        "satf"      0=off, 1=.satfinite
[39:32]  shape       "shape"     Shape enum (see below)
```

The `"ety"` query reads the result/accumulator element type from bits `[27:24]`, sharing bit positions with `al`/`bl` in a context-dependent manner -- the builder dispatches on the query string to select the correct extraction mask.

### Type Enum

| Value | Type | Bits | PTX string |
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

Any other value triggers the fatal error `"Wrong MMA element type"`.

### Shape Enum

| Value | Shape | PTX string | Notes |
|---|---|---|---|
| 0x01 | m8n8k4 | `"m8n8k4"` | Original Volta HMMA |
| 0x02 | m8n8k16 | `"m8n8k16"` | Integer MMA (s8/u8) |
| 0x03 | m8n8k32 | `"m8n8k32"` | Sub-byte (s4/u4) |
| 0x04 | m8n8k64 | `"m8n8k64"` | Extended sub-byte |
| 0x05 | m8n8k128 | `"m8n8k128"` | Binary MMA (b1) |
| 0x06 | m8n32k16 | `"m8n32k16"` | Appears unused in standard paths |
| 0x10 | m16n8k4 | `"m16n8k4"` | Turing HMMA, Ampere f64 |
| 0x11 | m16n8k8 | `"m16n8k8"` | Turing/Ampere HMMA |
| 0x12 | m16n8k16 | `"m16n8k16"` | Ampere (bf16, tf32) |
| 0x13 | m16n8k32 | `"m16n8k32"` | Ampere integer |
| 0x14 | m16n8k64 | `"m16n8k64"` | Sub-byte integer |
| 0x15 | m16n8k128 | `"m16n8k128"` | Extended sub-byte |
| 0x16 | m16n8k256 | `"m16n8k256"` | Largest shape (binary/sub-byte) |
| 0x17 | m16n16k16 | `"m16n16k16"` | Square shape (Hopper+) |
| 0x18 | m32n8k16 | `"m32n8k16"` | Tall shape |
| 0x19 | m16n16k8 | `"m16n16k8"` | WMMA f16 path |

Unrecognized shape values hit the default branch and trigger `BUG()` abort.

### PTX String Emission

The string builder uses an optimized emission pattern: short constant strings are stored as integer literals for single-store writes. For example, `"m16n8k16"` is emitted as:

```c
*(QWORD*)ptr = 0x36316B386E36316DLL;  // "m16n8k16" in little-endian
```

When the output buffer has sufficient remaining capacity, the builder writes directly via `DWORD`/`WORD`/`BYTE` stores. On buffer overflow, it falls back to `sub_16E7EE0` (slow-path string append).

## HMMA / IMMA / BMMA Lowering (SM 70--89)

The pre-Hopper MMA families share a common architecture: a three-table builtin-to-intrinsic lookup, per-family handler functions for load/store/MMA, and a consistent operand processing pattern.

### Three-Table Intrinsic Lookup

| Table | Address | ID Range | Description |
|---|---|---|---|
| `dword_3F14840` | Entries 0--29 | 678--707 | HMMA (FP16, first-gen) |
| `dword_3F147E0` | Entries 0--23 | 708--731 | IMMA (INT8) |
| `dword_3F147A0` | Entries 0--12 | 732--744 | BMMA (binary) / INT4 |

Each table maps `(builtin_id - base)` to an LLVM intrinsic ID. The first table additionally sets a `v43=1` flag indicating "first generation WMMA", which affects fragment size determination.

### HMMA Handler Family (SM >= 70)

Four functions implement half-precision MMA operations. All share a common pattern:

1. Architecture gate: `*(target_info + 252) > 0x45` (SM >= 70)
2. Fetch debug location
3. Validate `rowcol` operand is constant (opcode 10 or 32 check)
4. Resolve address space via `sub_21DEF90`
5. Build operands via `sub_1D38BB0` calls
6. Emit instruction via `sub_1D23DE0`

| Function | Address | Operation | Operand Count |
|---|---|---|---|
| `sub_21E0360` | `0x21E0360` | hmmaldab (load A/B) | 6 |
| `sub_21E0630` | `0x21E0630` | hmmaldc (load C) | 5 |
| `sub_21DFBF0` | `0x21DFBF0` | hmmastc (store C/D) | 9 or 13 (shape-dependent) |
| `sub_21E0870` | `0x21E0870` | hmmamma (MMA) | 19 or 23 + 1 metadata |

For hmmastc, the operand count depends on the accumulator width: 9 operands for narrow accumulators, 13 for wide (when the `a2` shape flag is set).

For hmmamma, the handler loads A fragments (`v100` iterations), B fragments (`v95` iterations), C fragments (`v101` iterations), emits the MMA call via `sub_921880`, then scatters results through `v103` iterations of element-wise stores.

### IMMA Handler Family (SM >= 72)

Integer MMA follows the same pattern but with additional SM 72 (Xavier) restrictions:

| Function | Address | Operation | SM Gate |
|---|---|---|---|
| `sub_21E1280` | `0x21E1280` | immaldab (load A/B) | SM > 0x47 (>= 72) |
| `sub_21E15D0` | `0x21E15D0` | immaldc (load C) | SM > 0x47 |
| `sub_21E1830` | `0x21E1830` | immastc (store C) | SM > 0x47 |
| `sub_21E1D20` | `0x21E1D20` | immamma (MMA + saturation) | SM > 0x47 |

**SM 72 special case.** Xavier's tensor cores support only basic IMMA shapes (variant 0 or 1). The gate check is:

```c
if (sm_version <= 0x47 || (sm_version == 72 && shape_variant > 1))
    fatal_error("not supported on this architecture");
```

For immaldc at SM 72, certain intrinsic opcodes (610, 611, 179, 180) are explicitly blocked:

```c
if (sm_version <= 0x47 || ((opcode-610 <= 1 || opcode-179 <= 1) && sm_version == 72))
    fatal_error(...);
```

The immamma handler includes an explicit `satf` (saturation-to-finite) constant extraction. The `.satfinite` modifier is appended to the PTX instruction when bit 28 of the descriptor is set. This clamps infinities and NaNs to the largest representable finite value.

IMMA operand counts vary by opcode:

| Opcode | Fragment Count | Shape |
|---|---|---|
| 584 | 12 | Large integer shape |
| 609 | 4 | Compact integer shape |
| other | 13 | Default |

### BMMA Handler (SM >= 73/75)

Binary MMA (`sub_21E2280`, `0x21E2280`) handles b1 operations with XOR-POPC and AND-POPC modes. Gate: SM > 0x48 (>= 73, in practice SM 75). The handler takes 8+ operands.

### Fragment Size Determination

Fragment size (the number of register-width elements per warp fragment) is computed differently per family:

**WMMA (first-gen, v43=1):**

| Condition | Fragment Count |
|---|---|
| BF16, store operation (`a6==1 && !a5`) | 4 |
| Default first-gen | 8 |
| Intrinsic 8914 or 8280 | 2 |

**IMMA (v43=0):**

| Intrinsic IDs | Fragment Count |
|---|---|
| 0x22B3--0x22B6, 0x22CF | 2 |
| 0x22BB--0x22BC, 0x22C5--0x22C6 | 4 |
| 0x22BD--0x22BE, 0x22C3--0x22C4, 0x22CB--0x22CE | 1 |
| 0x22B7, 0x22BF, 0x22C7 | 8 |

**BMMA:** Always 2 fragments, with `v101=2`, `v95=1`, `v100=1`.

### MMA Codegen (sub_94E0D0)

The WMMA multiply-accumulate handler processes five input operands:

1. `v102` -- destination fragment pointer (output)
2. `v7` -- A matrix fragment pointer
3. `v93` -- B matrix fragment pointer
4. `v92` -- C accumulator fragment pointer
5. `v8` -- `rowcol` operand (validated range: 0--3 for MMA)
6. `v9` -- `satf` flag (validated: 0 or 1; skipped for intrinsic 8279)

Fragment counts for the MMA operation itself:

| Family | v95 (A frags) | v100 (B frags) | v101 (C frags) | v103 (D frags) |
|---|---|---|---|---|
| BMMA | 1 | 1 | 2 | 2 |
| IMMA 0x22C0--0x22C1 | 1 | 4 | 8 | 8 |
| IMMA 0x22B8--0x22B9 | 2 | 2 | 8 | 8 |
| IMMA 0x22C8--0x22C9 | 4 | 1 | 8 | 8 |
| WMMA (default) | 8 | 8 | varies | 4 or 8 |

For first-gen WMMA, `v103` (D fragment count) is determined by a bit test:

```c
if ((0x300C003 >> (intrinsic_id + 127)) & 1)
    v103 = 4;
else
    v103 = 8;
```

The code generation sequence is:

```
1. LOAD  A fragments: v100 iterations of sub_94B510 (extract from ptr v7)
2. LOAD  B fragments: v95  iterations (extract from ptr v93)
3. LOAD  C fragments: v101 iterations (extract from ptr v92)
4. EMIT  MMA call:    sub_90A810(tables, intrinsic_id, 0, 0) -> sub_921880
5. STORE D fragments: v103 iterations of sub_94B940 (scatter to ptr v102)
```

### Address Space Resolution (sub_21DEF90)

MMA load/store operations resolve the target memory address space through `sub_21DEF90`, which checks the instruction opcode at offset `+24`:

| Opcode Range | Condition | Address Space |
|---|---|---|
| 185--237 | Bit test against 0x3FFFFD00000003 | varies |
| 44--45 | Bit 1 of byte at offset +26 | varies |
| >= 659 | unconditional | accepted |
| default | | generic (0) |

Return values: 0=generic, 1=global, 2=shared, 3=local, 4=constant, 5=special, 404=special (from value 101).

## SelectionDAG Path (sub_33B0210 / sub_33A64B0)

In the SelectionDAG intrinsic lowering mega-switch (`sub_33B0210`), 95 consecutive case labels (IDs 0xA4--0xA8 and 0x194--0x1EC, corresponding to LLVM intrinsic IDs 164--168 and 404--492) all dispatch to a single helper: `sub_33A64B0`.

This function handles every WMMA/MMA SelectionDAG intrinsic for SM 70--89:
- `wmma.load.a` / `wmma.load.b` / `wmma.load.c`
- `wmma.store.d`
- `wmma.mma` for all shape/type combinations
- `mma.sync` (SM 70+), `mma.sp` (SM 80+, structured sparsity), `mma.f64` (SM 80+)

The SelectionDAG path constructs NVPTXISD target-specific DAG nodes that are later matched by the instruction selection tables. The intrinsic IDs from the mega-switch are distinct from the builtin IDs used in the NVVM path -- the mega-switch IDs are LLVM intrinsic table indices, not CUDA builtin numbers.

## WGMMA -- Warp Group MMA (SM 90 Hopper)

WGMMA operates on a warp group (4 warps, 128 threads) instead of a single warp. Four builtin IDs (765--768) expand to over 150 LLVM intrinsic variants through compile-time dimension and type dispatch.

### Builtin-to-Intrinsic Expansion

| Builtin ID | Builtin | Variants |
|---|---|---|
| 765 (0x2FD) | `__wgmma_mma_async_f16` | Full 6-operand set (a, b, c, scale, negate, sparsity) |
| 766 (0x2FE) | `__wgmma_mma_async_bf16` | 2-operand (no scale/negate) |
| 767 (0x2FF) | `__wgmma_mma_async_tf32` | Reduced operand set |
| 768 (0x300) | `__wgmma_mma_async_f8` | Minimal (2 scale operands only) |

The lowering handler (in `sub_955A70`, cases 0x2FD--0x300, ~800 lines) extracts 7 levels of chained operands:

```
v263 -- M dimension (constant)
v512 -- accumulator fragments
v528 -- A descriptor
v524 -- B descriptor
v519 -- scale factors
v264 -- layout params
v540 -- element type info
```

### Dimension-to-Intrinsic Mapping

The N dimension (extracted via `sub_620FD0` as a constant integer) maps to one of 144 LLVM intrinsic IDs spanning 10654--10779. The mapping forms a dense table with stride 4 per N step:

| N | Integer-type Intrinsic | Float-type Intrinsic |
|---|---|---|
| 8 | 10774 | 10775 |
| 16 | 10690 | 10691 |
| 32 | 10742 | 10743 |
| 64 | 10758 | 10759 |
| 128 | 10666 | 10667 |
| 256 | 10738 | 10739 |

For intermediate N values (multiples of 8 from 8 to 256), the mapping continues at stride +4 per N increment. Even intrinsic IDs encode integer-element variants; odd IDs encode float-element variants. The element type is determined by checking whether the LLVM type is an integer with width 10 (i.e., tf32 or bf16 packed as i10 -- a quirk of the NVVM type system).

If constant extraction overflows, the compiler emits:
```
"unexpected constant overflow in __wgmma_mma_async operand"
```

If N is not a power of two: `(N & (N - 1)) != 0` triggers:
```
"N only supported for powers of two"
```

### WGMMA 5-Dimensional Intrinsic Grid

The full WGMMA intrinsic table (`sub_12B2E10`) uses a 144-entry grid spanning IDs 5304--5447:

| Dimension | Values | Count |
|---|---|---|
| N | 16, 32, 64, 128 | 4 |
| B_shared | false, true | 2 |
| is_s64 | false, true | 2 |
| A_scale/negate | combo | varies |
| case variant | 0x2FD--0x300 | 4 |

Each WGMMA call packs mode bits into a single integer:

```
bit 0:  accumulate flag     (from operand v433)
bit 1:  transpose flag      (from operand v445)
bit 2:  negate-C flag       (from operand v433)
bit 3:  reserved
bit 4:  negate-A flag       (from operand v427)
```

Combined: `v79 = bit0 | (bit1 << 1) | (bit2 << 2) | (bit4 << 4)`.

### WGMMA Parameter Lookup (sub_953BA0)

On first call, `sub_953BA0` lazily initializes a red-black tree at `ctx+560` with 7 entries encoding per-ID shape, transpose, register count, and type information:

| ID | trans_a | shape | a_nregs | b_nregs | a_type | b_type | c_type |
|---|---|---|---|---|---|---|---|
| 745 | 0 | 1 | 1 | 1 | i64 | i64 | -- |
| 746 | 1 | 0 | 9 | 9 | i32 | i32 | i32x2 |
| 747 | 0 | 0 | 8 | 8 | i16x2 | i16x2 | -- |
| 748 | 0 | 0 | 7 | 7 | i32x4 | i32x4 | i32x8 |
| 749 | 0 | 0 | 7 | 7 | i32x4 | i32x4 | i32x8 |
| 750 | 0 | 0 | 7 | 7 | i64 | i32x2 | i32x8 |

The output is packed into a 64-bit value:

```
bits[3:0]    = trans_a
bits[7:4]    = shape << 4
bits[15:8]   = a_nregs << 8
bits[27:16]  = b_nregs << 16
bits[31:28]  = padding << 28
bits[63:32]  = trans_b << 32
bit[25]      = ((rowcol & 2)==0) ? 0x2000000 : 0x1000000
bits[27:26]  = ((rowcol & 1)+1) << 26
```

### WGMMA MMA Async Load (sub_9547E0)

A second red-black tree at `ctx+656` holds 12 entries for MMA async load parameters:

| ID | Shape | NRegs | Variant | Fragment Type |
|---|---|---|---|---|
| 753 | 1 | 9 | 0 | -- |
| 754 | 1 | 9 | 1 | -- |
| 755 | 1 | 9 | 2 | i16x2 |
| 756 | 25 | 8 | 0 | -- |
| 757 | 25 | 8 | 1 | -- |
| 758 | 25 | 10 | 2 | i32x8 |
| 759 | 23 | 7 | 0 | i32x4 |
| 760 | 23 | 7 | 1 | i32x4 |
| 761 | 24 | 7 | 0 | i32x4 |
| 762 | 24 | 7 | 1 | i32x4 |
| 763 | 6 | 7 | 0 | i32x2/i64 |
| 764 | 6 | 7 | 1 | i32x2/i64 |

### WGMMA Fence/Store Dispatch

| IDs | Operation | Intrinsic | Handler |
|---|---|---|---|
| 745--750 | fence_aligned | 9062 (3 type overloads) | `sub_953BA0` -> `sub_94B510` x3 -> `sub_94B940` |
| 751--752 | store | 9145 (2 type overloads) | `sub_954350` |
| 753--764 | mma_async load | 9067 (2 type overloads) | `sub_9547E0` |

The fence operations pack A/B/C fragment operands via `sub_94B510` and scatter results via `sub_94B940` with name hint `"mmafrag"`.

## tcgen05 -- Tensor Core Generation 5 (SM 100 Blackwell)

SM 100 introduces tcgen05, a completely new tensor core instruction family with support for MX floating-point formats (MXF4, MXF8F6F4), structured sparsity, weight stationary mode, block scaling, and scaled input accumulators. The tcgen05 system includes both computation (tcgen05.mma) and lifecycle management (alloc, dealloc, fence, wait, commit, cp, relinquish) instructions.

### Architecture Gate

All tcgen05 operations require SM >= 100. The gate check reads two architecture fields:

```c
v1 = *(int*)(arch_struct + 340);  // arch_value: 1000=sm100, 1030=sm103, 1200=sm120
v2 = *(int*)(arch_struct + 336);  // ptx_version

// Family-conditional: ptx >= 86
// Arch-conditional: ptx >= 88
if (v1 <= 0x3E8 && v1 <= 0x408)  // neither sm_100 nor sm_103
    fatal_error("tcgen05.mma supported only on arch-conditional "
                "or family-conditional variants from SM100 onwards.");
```

### tcgen05 Infrastructure Operations

All handled by `sub_30462A0`:

| Operation | Intrinsic Opcode | ISD Opcode | Operands |
|---|---|---|---|
| tcgen05.alloc | 10080 | 4765 | basic allocation |
| tcgen05.alloc (multicast) | 10083 | 4770/4771 | 32-bit flag variant |
| tcgen05.dealloc | 10140 | 4827 | 4 operands |
| tcgen05.commit | 10090 | 4772--4777 | multicast mask variants |
| tcgen05.fence | 10143 | 4830 | 2 operands |
| tcgen05.wait | 10351 | 5020 | 2 operands |
| tcgen05.relinquish.alloc | 10311 | 4941 | 2 operands |
| tcgen05.cp.* | 10101 | 4790 | 4 operands |

Commit operations validate multicast mask size -- only 16-bit and 32-bit masks are supported:

```
"tcgen05.commit.* supports only 16-bit and 32-bit multicast mask size."
```

### tcgen05.mma Data Types

The "kind" field occupies bits `[8:6]` of the packed operand word:

| Value | Kind | Description |
|---|---|---|
| 0 | mxf4nvf4 | MX FP4 with NV FP4 |
| 1 | f8f6f4 | FP8/FP6/FP4 standard |
| 2 | mxf8f6f4 | MX variant of f8f6f4 |
| 3 | f16 | Half precision |
| 4 | i8 | 8-bit integer (**arch-conditional only**) |
| 5 | tf32 | TensorFloat-32 |
| 7 | mxf4 | MX FP4 |

### tcgen05.mma Modifiers

**Scale vector size** (bits `[3:2]`):

| Value | Modifier | Constraints |
|---|---|---|
| 0/1 | `.scale_vec::1X` | Cannot use for mxf4nvf4 type |
| 2 | `.scale_vec::2X` | Cannot use for mxf8f6f4 type |
| 3 | `.scale_vec::4X` | Cannot use for mxf8f6f4 or mxf4 type |

**Block scale alias** (bits `[10:9]`):

| Value | Modifier | Constraint |
|---|---|---|
| 0 | `.block16` | Not supported for f16, tf32, f8f6f4, i8 |
| 1 | `.block32` | Same constraint |

**Weight stationary** (bit 0): `.ws` flag. Not compatible with `cta_group::2`, mxf8f6f4, or fp4 types.

**CTA group** (bits `[1:0]`): `.cta_group::1` (bit 1 clear) or `.cta_group::2` (bit 1 set).

**Sparsity** (bit 5): Adds one extra operand. Restricted for MXF4 and MXF4NVF4 types to arch-conditional variants only.

**Scale input accumulator** (bit 4): Only usable with f16 and tf32 types. Not supported on sm_100a (v=1001) or sm_103a (v=1033), but supported on sm_100 (v=1000), sm_103 (v=1030), and sm_120+ (v>=1101).

**Collector modes** (emitted by `sub_35F38B0`):

| Value | PTX modifier |
|---|---|
| 1 | `.collector::a::lastuse` |
| 2 | `.collector::a::fill` |
| 3 | `.collector::a::use` |

Cannot use `collector::a::use` or `collector::a::fill` with ashift.

### tcgen05.mma ISD Opcode Selection (sub_36E9630)

The intrinsic lowering handler (`sub_304E6C0`) maps 10 shape cases (intrinsic opcodes 10299--10308) to ISD opcodes 4905--4940:

| Case | Shape Class | Base ISD | +scaleD | +sparsity | +ws | +scaleInputAccum |
|---|---|---|---|---|---|---|
| 10299 | Small | 4906 | -- | 4907 | -- | -- |
| 10300 | Small v2 | 4908 | -- | 4909 | -- | -- |
| 10301 | Medium | 4905 | 4910 | 4911/4912 | 4937/4938 | yes |
| 10302 | Medium v2 | 4913 | 4914 | 4915/4916 | -- | yes |
| 10303 | Large | 4917 | 4918 | 4919/4920 | -- | yes |
| 10304 | Block-scale small | 4922 | -- | 4923 | -- | -- |
| 10305 | Block-scale small v2 | 4924 | -- | 4925 | -- | -- |
| 10306 | Block-scale medium | 4921 | 4926 | 4927/4928 | 4939/4940 | yes |
| 10307 | Block-scale medium v2 | 4929 | 4930 | 4931/4932 | -- | -- |
| 10308 | Block-scale large | 4933 | 4934 | 4935/4936 | -- | -- |

Operand count varies by variant: small shapes take 5--6 base operands plus optional sparsity operand; medium shapes take 6 base plus optional scale factor; large shapes iterate over additional operands spanning offsets 440--600 (or 440--760 on sm_103 extended variants).

### tcgen05.mma Validation Errors

The full set of compile-time validation errors (emitted via `sub_C64ED0`):

| Error Message | Condition |
|---|---|
| `"INT8 type is supported only on arch-conditional variants."` | kind==i8 on family-conditional SM100 |
| `"MXF4 and MXF4NVF4 types with Sparsity are supported only on arch-conditional variants."` | (type+7)%8 > 5 AND sparsity set, on family-conditional |
| `"Explicit scale vector size is supported only on arch-conditional variants."` | scale_vec_size 1--3 on family-conditional |
| `"Scale input accumulator can only be used with f16 and tf32 types"` | bit 4 set but kind not f16 or tf32 |
| `"Scale input accumulator is not supported on this architecture."` | scaleInputAccum on sm_100a or sm_103a |
| `"Block scale is not supported for f16, tf32, f8f6f4 and i8 types"` | block_scale with incompatible type |
| `"ashift is not supported with tcgen05.mma.block_scale variants"` | ashift + block_scale |
| `"cta_group::2 is not supported with weight stationary"` | cta_group::2 + .ws |
| `"Cannot use weight stationary with mxf8f6f4 and fp4 types"` | .ws + mxf8f6f4 or fp4 |
| `"Cannot use collector::a::use or colletor::a::fill with ashift"` | [sic] collector + ashift |
| `"Cannot use 2X or 4X as scale vector size for mxf8f6f4 type"` | scale_vec >= 2X + mxf8f6f4 |
| `"Cannot use 1X as scale vector size for mxf4nvf4 type"` | scale_vec 1X + mxf4nvf4 |
| `"Cannot use 1X or 4X as scale vector size for mxf4 type"` | scale_vec 1X or 4X + mxf4 |

Note the typo `"colletor"` (missing 'c') in the binary -- this is a genuine NVIDIA binary string, not a transcription error.

### tcgen05 Scaled MMA Operand Builder

Two identical copies exist for the tcgen05 scaled MMA descriptor:

| Copy | Address | Layer |
|---|---|---|
| `sub_21E8CD0` | `0x21E8CD0` | AsmPrinter / PTX emission |
| `sub_35F3E90` | `0x35F3E90` | NVPTX backend / SelectionDAG |

The packed descriptor encodes Blackwell-specific modifiers:

| Bit | Query | Set Value | Clear Value | Semantics |
|---|---|---|---|---|
| 0 | `"scaleD"` | `"1"` | `"0"` | Scale output accumulator |
| 1 | `"negA"` | `"-1"` | `"1"` | Negate A matrix |
| 2 | `"negB"` | `"-1"` | `"1"` | Negate B matrix |
| 3 | `"transA"` | `"1"` | `"0"` | Transpose A |
| 4 | `"transB"` | `"1"` | `"0"` | Transpose B |

scaleD and transA/transB emit boolean `"0"`/`"1"` strings. negA and negB emit sign multiplier strings `"-1"`/`"1"` because PTX applies negation as a multiplication factor.

### tcgen05.cp Copy Operations

Shape variants (bits `[3:1]`):

| Value | PTX shape |
|---|---|
| 0 | `.128x256b` |
| 1 | `.4x256b` |
| 2 | `.128x128b` |
| 3 | `.64x128b` |
| 4 | `.32x128b` |

Destination format variants:

| Condition | PTX format |
|---|---|
| default | `.b8x16` |
| bit 7 = 0 | `.b6x16_p32` |
| bit 7 = 1 | `.b4x16_p64` |
| bit 8 set | error: `"Unsupported tcgen05.cp destination format"` |

Multicast modes:

| Type | PTX modifier |
|---|---|
| type 1, shape 3 | `.warpx2::02_13` |
| type 2, shape 3 | `.warpx2::01_23` |
| type 3, shape 4 | `.warpx4` |

## Duplicate Backend Copies

Several MMA functions exist as near-identical pairs -- one in the AsmPrinter emission layer (`0x21Dxxxx`--`0x21Exxxx`) and one in the NVPTX backend layer (`0x36Exxxx`). The difference is limited to error reporting and reference counting functions:

| AsmPrinter Copy | Backend Copy | Operation |
|---|---|---|
| `sub_21DFBF0` | `sub_36E91F0` | hmmastc |
| `sub_21E0360` | `sub_36E72A0` | hmmaldab |
| `sub_21E0630` | `sub_36E7580` | hmmaldc |
| `sub_21E0870` | `sub_36E77C0` | hmmamma |
| `sub_21E1280` | `sub_36E7B50` | immaldab |
| `sub_21E15D0` | `sub_36E7EA0` | immaldc |
| `sub_21E1830` | `sub_36E8110` | immastc |
| `sub_21E1D20` | `sub_36E8630` | immamma |
| `sub_21E2280` | `sub_36E8BD0` | bmmamma |
| `sub_21E8CD0` | `sub_35F3E90` | tcgen05 scaled MMA |

AsmPrinter copies use `sub_16BD130` for errors; backend copies use `sub_C64ED0`. AsmPrinter copies use `sub_1623A60`/`sub_161E7C0` for refcounting; backend copies use `sub_B96E90`/`sub_B91220`.

## Shape x Type x Architecture Matrix

| Shape | A/B Types | Accumulator | Min SM | Notes |
|---|---|---|---|---|
| m8n8k4 | f16 | f16, f32 | SM 70 | Original Volta |
| m16n8k4 | f64 | f64 | SM 80 | Ampere double precision |
| m16n8k8 | f16 | f16, f32 | SM 75 | Turing+ |
| m16n8k16 | f16, bf16, tf32 | f16, f32 | SM 80 | Ampere+ |
| m16n16k8 | f16 | f16, f32 | SM 70 | WMMA path |
| m16n16k16 | f16, bf16 | f16, f32 | SM 90 | Hopper+ |
| m32n8k16 | f16, bf16 | f16, f32 | SM 80 | Tall shape |
| m8n8k16 | s8, u8 | s32 | SM 72 | Integer MMA |
| m16n8k16 | s8, u8 | s32 | SM 75 | Turing+ integer |
| m16n8k32 | s8, u8 | s32 | SM 75 | Turing+ integer |
| m8n8k32 | s4, u4 | s32 | SM 75 | Sub-byte |
| m16n8k64 | s4, u4 | s32 | SM 75 | Sub-byte |
| m8n8k64 | s4, u4 | s32 | SM 75 | Extended sub-byte |
| m16n8k128 | s4, u4 | s32 | SM 75 | Extended sub-byte |
| m8n8k128 | b1 | s32 | SM 75 | Binary (.and.popc / .xor.popc) |
| m16n8k256 | b1 | s32 | SM 75 | Binary extended |
| tcgen05 (10 variants) | mxf4nvf4, f8f6f4, mxf8f6f4, f16, tf32, i8, mxf4 | varies | SM 100 | +block_scale, +sparsity, +ws |

## LLVM Intrinsic ID Reference

Key intrinsic IDs used in the MMA code generation pipeline:

| Intrinsic ID | Symbol | Usage |
|---|---|---|
| 8181 | `llvm.nvvm.wmma.store` (complex) | WMMA complex store |
| 8210 | `llvm.nvvm.wmma.store` | WMMA store |
| 8279 | (special) | IMMA MMA without satf |
| 8280 | (special) | Fragment count = 2 trigger |
| 8914 | (special) | Fragment count = 2 trigger |
| 9062 | `llvm.nvvm.wgmma.fence.aligned` | WGMMA fence (3 type overloads) |
| 9067 | `llvm.nvvm.wgmma.mma.async` | WGMMA MMA async (2 type overloads) |
| 9145 | `llvm.nvvm.wgmma.store` | WGMMA store |
| 10654--10779 | `llvm.nvvm.wgmma.mma.async.*` | Per-dimension WGMMA variants (144 entries) |
| 5304--5447 | (WGMMA grid) | 5-dimensional intrinsic grid for WGMMA |

## Error Handling

Two error-reporting functions serve the two layers:

| Function | Address | Layer | Behavior |
|---|---|---|---|
| `sub_16BD130` | `0x16BD130` | AsmPrinter / PTX emission | Fatal (severity=1 -> abort) |
| `sub_C64ED0` | `0xC64ED0` | NVPTX backend / SelectionDAG | Fatal (severity=1 -> abort) |

Error categories:

1. **Architecture not supported:** `"X is not supported on this architecture"` -- SM gate failure
2. **Constant validation:** `"rowcol not constant"`, `"satf not constant"` -- non-constant operand
3. **Type restrictions:** `"Wrong MMA element type"` -- invalid type enum
4. **Feature combination:** `"ashift is not supported with tcgen05.mma.block_scale"` -- conflicting modifiers
5. **Scale restrictions:** `"Cannot use N as scale vector size for X type"` -- type/scale mismatch

## Differences from Upstream LLVM

Upstream LLVM's NVPTX backend has **no MMA code generation**. The entire MMA pipeline -- builtin tables, three-table lookup, fragment size computation, WGMMA dimension dispatch, tcgen05 lowering, packed descriptor encoding, and all shape/type validation -- is NVIDIA-proprietary code with no upstream equivalent.

Upstream LLVM handles MMA operations at the PTX level only: the upstream `NVPTXAsmPrinter` can print PTX `mma.sync` instructions, but the instruction selection, intrinsic lowering, and code generation logic that produces them exists only in NVIDIA's cicc binary. An open-source reimplementation would need to build the entire pipeline from the WMMA/MMA intrinsic definitions through SelectionDAG lowering and PTX emission.

## Cross-References

- [Tensor / MMA Builtins](../builtins/tensor-mma.md) -- per-builtin-ID reference table and validation rules
- [SelectionDAG & ISel](selectiondag.md) -- DAG infrastructure hosting MMA lowering
- [ISel Pattern Matching](isel-patterns.md) -- downstream pattern matcher consuming MMA DAG nodes
- [SM 90 -- Hopper](../targets/sm90-hopper.md) -- WGMMA feature gate details
- [SM 100 -- Blackwell](../targets/sm100-blackwell.md) -- tcgen05 feature gate details
- [SM 120](../targets/sm120.md) -- Blackwell consumer variant features
- [NVPTX Machine Opcodes](../reference/nvptx-opcodes.md) -- ISD opcode reference
- [Register Classes](../reference/register-classes.md) -- fragment register allocation
- [PTX Emission](../pipeline/emission.md) -- downstream PTX text generation
